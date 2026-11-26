#!/usr/bin/env python3
"""
EmberJack Payload -- RSS News Reader
====================================
Author: 7h30th3r0n3

Fetches and displays RSS / Atom feed articles from configurable sources.
Split-pane Obsidian Ember headlines + wrapped article reader on widescreen,
and clean scrollable list + reader views on compact screens.

Controls:
  UP / DOWN           -- Navigate headlines (or scroll article when reader focused)
  PgUp / PgDn         -- Scroll article reader page-by-page
  ENTER / OK          -- Focus article reader (widescreen) / Open reader (compact)
  LEFT / RIGHT / TAB  -- Switch previous / next feed
  KEY2 / R            -- Refresh current feed
  KEY3 / ESC          -- Back / Exit
"""

import json
import os
import re
import signal
import sys
import time
import xml.etree.ElementTree as ET
from html import unescape
from urllib.error import URLError
from urllib.request import Request, urlopen

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import LCD_Config
from PIL import Image, ImageDraw

import rj_env
import rj_theme
from payloads._input_helper import (
    flush_input,
    format_footer_hint,
    is_action,
    is_back,
    is_confirm,
    poll_ui_input,
    text_input_mode,
)

ET_THEME = rj_theme.EMBER_THEME

PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}

CONFIG_DIR = os.path.join(rj_env.get_base_dir(), "loot", "News")
CONFIG_PATH = os.path.join(CONFIG_DIR, "feeds.json")

DEFAULT_FEEDS = [
    {"name": "HackerNews", "url": "https://feeds.feedburner.com/TheHackersNews"},
    {"name": "BleepComp", "url": "https://www.bleepingcomputer.com/feed/"},
    {"name": "Krebs", "url": "https://krebsonsecurity.com/feed/"},
]

running = True
feeds = list(DEFAULT_FEEDS)
feed_idx = 0
articles = []
article_idx = 0
scroll_offset = 0
view_mode = "list"  # "list" or "detail"
detail_scroll = 0


def cleanup(*_args):
    global running
    running = False


signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_feeds():
    global feeds
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, list) and loaded:
            feeds = loaded
    except Exception:
        feeds = list(DEFAULT_FEEDS)


def save_feeds():
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(feeds, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# RSS / Atom parsing
# ---------------------------------------------------------------------------

def strip_html(text):
    """Remove HTML tags and collapse extra whitespace."""
    cleaned = re.sub(r"<[^>]+>", " ", text or "")
    cleaned = unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def fetch_feed(url):
    """Fetch and parse RSS/Atom feed. Returns list of dicts with title/summary/date."""
    try:
        req = Request(url, headers={"User-Agent": "EmberJack/1.0"})
        with urlopen(req, timeout=15) as resp:
            data = resp.read()
    except (URLError, OSError) as exc:
        return [{"title": f"Error: {str(exc)[:80]}", "summary": "Check network connection and press R / K2 to retry.", "date": "", "error": True}]

    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return [{"title": "XML parse error", "summary": f"Could not parse feed XML from {url}", "date": "", "error": True}]

    items = []
    # Standard RSS 2.0: channel/item
    for item in root.iter("item"):
        title_el = item.find("title")
        desc_el = item.find("description")
        pub_el = item.find("pubDate")
        title = strip_html(title_el.text or "") if title_el is not None else "No title"
        summary = strip_html(desc_el.text or "") if desc_el is not None else ""
        pub = strip_html(pub_el.text or "")[:22] if pub_el is not None else ""
        items.append({"title": title, "summary": summary[:2500], "date": pub})

    # Atom fallback: feed/entry
    if not items:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            title_el = entry.find("atom:title", ns) or entry.find("{http://www.w3.org/2005/Atom}title")
            sum_el = (
                entry.find("atom:summary", ns)
                or entry.find("{http://www.w3.org/2005/Atom}summary")
                or entry.find("atom:content", ns)
                or entry.find("{http://www.w3.org/2005/Atom}content")
            )
            upd_el = entry.find("atom:updated", ns) or entry.find("{http://www.w3.org/2005/Atom}updated")
            title = strip_html(title_el.text or "") if title_el is not None else "No title"
            summary = strip_html(sum_el.text or "") if sum_el is not None else ""
            pub = strip_html(upd_el.text or "")[:19] if upd_el is not None else ""
            items.append({"title": title, "summary": summary[:2500], "date": pub})

    return items if items else [{"title": "No articles found", "summary": "", "date": ""}]


# ---------------------------------------------------------------------------
# Native Resolution-Adaptive Obsidian Ember Renderers
# ---------------------------------------------------------------------------

def _build_article_lines(draw, art, max_w, f):
    """Build wrapped display lines [(text, style)] for an article."""
    out = []
    title_lines = rj_theme.wrap_text(draw, art.get("title", "No title"), max_w, f["body_bold"])
    for tl in title_lines:
        out.append((tl, "title"))
    if art.get("date"):
        out.append((art["date"], "meta"))
    out.append(("", "sep"))
    summary = art.get("summary", "") or "No article summary available."
    for sl in rj_theme.wrap_text(draw, summary, max_w, f["body"]):
        out.append((sl, "body"))
    return out


def render_loading_frame(width, height, feed_name, url="", msg="Fetching RSS feed...", fonts=None):
    f = fonts or rj_theme.load_app_fonts(width, height)
    img = Image.new("RGB", (width, height), ET_THEME["bg_base"])
    d = ImageDraw.Draw(img)
    rj_theme.draw_app_header(d, width, "NEWS READER", f, badge=feed_name[:12])
    rj_theme.draw_app_footer(d, width, height, "EmberJack RSS Reader", f)
    rj_theme.draw_status_card(
        d, width, height,
        msg,
        sub=f"{feed_name}  •  {url}" if url else feed_name,
        kind="info",
        foot="Please wait...",
        fonts=f,
    )
    return img


def _layout_metrics(width, height, f):
    """Return 4-tier layout geometry (640x360, 320x170, 240x240, 128x128)."""
    wide = bool(f["wide"])
    large = bool(f["large"])
    is_240 = (not wide) and width >= 200
    if large:
        return {"wide": True, "large": True, "is_240": False, "hdr_h": 30, "tab_h": 22, "ftr_h": 22, "row_h": 46, "line_h": 18, "pad": 10, "v_top": 8, "v_bot": 6, "sb_w": 5}
    if wide:
        return {"wide": True, "large": False, "is_240": False, "hdr_h": 24, "tab_h": 16, "ftr_h": 18, "row_h": 26, "line_h": 13, "pad": 5, "v_top": 3, "v_bot": 3, "sb_w": 4}
    if is_240:
        return {"wide": False, "large": False, "is_240": True, "hdr_h": 22, "tab_h": 16, "ftr_h": 16, "row_h": 26, "line_h": 14, "pad": 5, "v_top": 4, "v_bot": 4, "sb_w": 3}
    return {"wide": False, "large": False, "is_240": False, "hdr_h": 18, "tab_h": 0, "ftr_h": 13, "row_h": 18, "line_h": 10, "pad": 3, "v_top": 2, "v_bot": 2, "sb_w": 2}


def render_news_frame(
    width,
    height,
    feed_list,
    cur_feed_idx,
    art_list,
    cur_art_idx,
    list_scroll,
    art_scroll,
    mode="list",
    fonts=None,
):
    """Pure PIL native resolution-adaptive Obsidian Ember News Reader renderer."""
    f = fonts or rj_theme.load_app_fonts(width, height)
    m = _layout_metrics(width, height, f)
    wide = m["wide"]
    large = m["large"]
    is_240 = m["is_240"]
    split = width >= 480

    img = Image.new("RGB", (width, height), ET_THEME["bg_base"])
    d = ImageDraw.Draw(img)

    cur_feed = feed_list[cur_feed_idx % max(1, len(feed_list))] if feed_list else {"name": "Feed"}
    feed_name = str(cur_feed.get("name", "Feed"))

    hdr_h = rj_theme.draw_app_header(
        d, width, "NEWS READER" if (wide or is_240) else "NEWS", f,
        badge=feed_name[:12],
        right_text=f"{len(art_list)} articles",
        right_pill=("READER" if mode == "detail" else "HEADLINES"),
        hdr_h=m["hdr_h"],
    )

    if mode == "detail" and not split:
        hint = format_footer_hint(
            width, height,
            hat_hint="U/D:Scr K2:Rf K3:Bk",
            hat_240_hint="U/D:Scroll  L/R:Feed  K2:Ref  K3:Back",
            cardputer_hint="U/D:Scroll  L/R:Feed  R:Ref  Esc:Back",
            hdmi_hint="Up/Dn/PgUp/PgDn:Scroll  L/R/Tab:Feed  R:Refresh  Esc:Headlines",
        )
    elif mode == "detail" and split:
        hint = format_footer_hint(
            width, height,
            hat_hint="U/D:Scr K3:Bk",
            hat_240_hint="U/D:Scroll K3:List",
            cardputer_hint="U/D:Scroll  L/R:Feed  R:Ref  Esc:List",
            hdmi_hint="Up/Dn/PgUp/PgDn:Scroll Article  •  Enter/Esc:Headlines  •  L/R/Tab:Feed  •  R:Refresh",
        )
    else:
        hint = format_footer_hint(
            width, height,
            hat_hint="OK:Rd L/R:Fd K2:Rf K3:X",
            hat_240_hint="OK:Read  L/R:Feed  K2:Ref  K3:Exit",
            cardputer_hint="Enter:Read  L/R/Tab:Feed  R:Refresh  Esc:Exit",
            hdmi_hint="Up/Dn:Article  •  PgUp/PgDn:Scroll  •  Enter:Reader  •  L/R/Tab:Feed  •  R:Refresh  •  Esc:Exit",
        )
    pos_txt = f"{min(cur_art_idx + 1, max(1, len(art_list)))}/{max(1, len(art_list))}"
    ftr_h = rj_theme.draw_app_footer(d, width, height, hint, f, right_text=pos_txt, ftr_h=m["ftr_h"])

    # Feed pill tabs on wide & 240x240 screens
    tab_h = m["tab_h"]
    if tab_h:
        d.rectangle((0, hdr_h, width - 1, hdr_h + tab_h - 1), fill=ET_THEME["bg_surface"])
        d.line([(0, hdr_h + tab_h - 1), (width - 1, hdr_h + tab_h - 1)], fill=ET_THEME["border_subtle"], width=1)
        tx = 8 if wide else 5
        for idx_f, fd in enumerate(feed_list):
            lbl = str(fd.get("name", f"Feed {idx_f + 1}"))[:(14 if wide else 10)]
            active = idx_f == cur_feed_idx
            pw, _ = rj_theme.draw_pill(
                d, tx, hdr_h + max(1, (tab_h - 13) // 2), lbl, f["badge"],
                bg=ET_THEME["select_bg"] if active else ET_THEME["bg_well"],
                fg=ET_THEME["accent_bright"] if active else ET_THEME["text_muted"],
                outline=ET_THEME["border_accent"] if active else ET_THEME["border_subtle"],
                pad_x=6 if wide else 4, pad_y=1, radius=3,
            )
            tx += pw + (6 if wide else 4)
            if tx > width - 32:
                break

    y0 = hdr_h + tab_h + m["v_top"]
    y1 = height - ftr_h - m["v_bot"]
    pad = m["pad"]

    if not art_list:
        empty_foot = format_footer_hint(
            width, height,
            hat_hint="K2:Retry  K3:Exit",
            hat_240_hint="K2: Refresh  L/R: Feed  K3: Exit",
            cardputer_hint="R: Refresh  L/R/Tab: Feed  Esc: Exit",
            hdmi_hint="R: Refresh  •  L/R/Tab: Switch Feed  •  Esc: Exit",
        )
        rj_theme.draw_status_card(
            d, width, height,
            "No Articles Loaded",
            sub="Refresh feed or switch source",
            kind="warn", foot=empty_foot, fonts=f,
        )
        return img, 1, 1

    # Compact full-screen article detail view
    if not split and mode == "detail":
        art = art_list[max(0, min(cur_art_idx, len(art_list) - 1))]
        rj_theme.draw_card(d, (pad, y0, width - pad, y1), fill=ET_THEME["bg_surface"], outline=ET_THEME["border_accent"], radius=5 if (wide or is_240) else 3)
        inner_x0 = pad + (8 if (wide or is_240) else 4)
        inner_w = max(40, width - pad * 2 - (20 if (wide or is_240) else 12))
        lines = _build_article_lines(d, art, inner_w, f)
        line_h = m["line_h"]
        inner_pad = 5 if (wide or is_240) else 3
        vis_lines = max(1, (y1 - y0 - inner_pad * 2) // line_h)
        max_sc = max(0, len(lines) - vis_lines)
        sc = max(0, min(art_scroll, max_sc))
        ty = y0 + inner_pad
        for txt, style in lines[sc:sc + vis_lines]:
            if ty + line_h > y1 - 2:
                break
            if style == "sep":
                d.line([(inner_x0, ty + line_h // 2), (width - pad - 10, ty + line_h // 2)], fill=ET_THEME["border_subtle"], width=1)
            elif style == "title":
                d.text((inner_x0, ty), txt, font=f["body_bold"], fill=ET_THEME["accent_bright"])
            elif style == "meta":
                d.text((inner_x0, ty), txt, font=f["sub"], fill=ET_THEME["text_muted"])
            else:
                d.text((inner_x0, ty), txt, font=f["body"], fill=ET_THEME["text_primary"])
            ty += line_h
        if len(lines) > vis_lines:
            rj_theme.draw_scrollbar(d, width - pad - (6 if (wide or is_240) else 4), y0 + 4, y1 - 4, sc, vis_lines, len(lines), width=m["sb_w"])
        return img, vis_lines, max_sc

    # Headlines list (left pane on widescreen, full screen on compact)
    sb_w = m["sb_w"]
    list_x0 = pad
    list_x1 = int(width * 0.44) - 4 if split else (width - pad - sb_w - 3)
    list_focused = (mode == "list")

    row_h = m["row_h"]
    vis_list = max(1, (y1 - y0) // row_h)
    st = max(0, min(list_scroll, max(0, len(art_list) - vis_list)))

    for slot in range(vis_list):
        idx = st + slot
        if idx >= len(art_list):
            break
        art = art_list[idx]
        is_sel = idx == cur_art_idx
        ry0 = y0 + slot * row_h
        ry1 = min(y1, ry0 + row_h - (5 if large else (3 if (wide or is_240) else 2)))
        if ry1 <= ry0 + 4:
            break

        rj_theme.draw_card(
            d, (list_x0, ry0, list_x1, ry1),
            fill=ET_THEME["select_bg"] if is_sel else ET_THEME["bg_surface"],
            outline=ET_THEME["border_accent"] if (is_sel and list_focused) else ET_THEME["border_subtle"],
            radius=6 if wide else (4 if is_240 else 3),
            accent_left=ET_THEME["accent"] if is_sel else None,
            accent_width=3 if wide else 2,
        )

        nx = list_x0 + (10 if wide else (7 if is_240 else 5))
        avail = max(32, list_x1 - nx - (8 if wide else 4))
        if large:
            wrapped = rj_theme.wrap_text(d, art["title"], avail, f["body_bold"] if is_sel else f["body"])
            for li, ln in enumerate(wrapped[:2]):
                if li == 1 and len(wrapped) > 2:
                    ln = rj_theme.truncate_text(d, ln + "...", avail, f["body_bold"] if is_sel else f["body"])
                d.text(
                    (nx, ry0 + 5 + li * 16),
                    ln,
                    font=f["body_bold"] if is_sel else f["body"],
                    fill=ET_THEME["text_bright"] if is_sel else ET_THEME["text_primary"],
                )
        else:
            use_bold = (wide or is_240) and is_sel
            title_s = rj_theme.truncate_text(d, art["title"], avail, f["body_bold"] if use_bold else f["body"])
            d.text(
                (nx, ry0 + (3 if (wide or is_240) else 2)),
                title_s,
                font=f["body_bold"] if use_bold else f["body"],
                fill=ET_THEME["text_bright"] if is_sel else ET_THEME["text_primary"],
            )
            if (wide or is_240) and art.get("date") and (ry1 - ry0) >= 20:
                d.text((nx, ry0 + 14), rj_theme.truncate_text(d, art["date"], avail, f["badge"]), font=f["badge"], fill=ET_THEME["text_dim"])

    if len(art_list) > vis_list:
        rj_theme.draw_scrollbar(d, list_x1 + 3, y0, y1, st, vis_list, len(art_list), width=sb_w)

    # Right split-pane wrapped article reader (W >= 480)
    reader_vis = 8
    reader_max_sc = 0
    if split and 0 <= cur_art_idx < len(art_list):
        art = art_list[cur_art_idx]
        px0 = list_x1 + 12
        px1 = width - pad
        reader_focused = (mode == "detail")
        rj_theme.draw_card(
            d, (px0, y0, px1, y1),
            fill=ET_THEME["bg_surface"],
            outline=ET_THEME["border_accent"] if reader_focused else ET_THEME["border_subtle"],
            radius=7,
            accent_left=ET_THEME["accent"] if reader_focused else None,
            accent_width=3,
        )
        inner_x0 = px0 + 12
        inner_w = max(80, (px1 - px0) - 30)
        lines = _build_article_lines(d, art, inner_w, f)
        line_h = m["line_h"]
        reader_vis = max(1, (y1 - y0 - 20) // line_h)
        reader_max_sc = max(0, len(lines) - reader_vis)
        sc = max(0, min(art_scroll, reader_max_sc))
        ty = y0 + 10
        for txt, style in lines[sc:sc + reader_vis]:
            if ty + line_h > y1 - 4:
                break
            if style == "sep":
                d.line([(inner_x0, ty + line_h // 2), (px1 - 14, ty + line_h // 2)], fill=ET_THEME["border_subtle"], width=1)
            elif style == "title":
                d.text((inner_x0, ty), txt, font=f["body_bold"], fill=ET_THEME["accent_bright"])
            elif style == "meta":
                d.text((inner_x0, ty), txt, font=f["sub"], fill=ET_THEME["text_muted"])
            else:
                d.text((inner_x0, ty), txt, font=f["body"], fill=ET_THEME["text_primary"])
            ty += line_h
        if len(lines) > reader_vis:
            rj_theme.draw_scrollbar(d, px1 - 8, y0 + 8, y1 - 8, sc, reader_vis, len(lines), width=4)

    return img, reader_vis, reader_max_sc


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global running, feed_idx, articles, article_idx
    global scroll_offset, view_mode, detail_scroll

    GPIO.setmode(GPIO.BCM)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    LCD_Config.GPIO_Init()
    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    lcd.LCD_Clear()
    W, H = lcd.width, lcd.height
    fonts = rj_theme.load_app_fonts(W, H)
    split = W >= 480

    load_feeds()
    save_feeds()

    def _load_current_feed(msg="Fetching feed..."):
        global articles, article_idx, scroll_offset, detail_scroll, view_mode
        fd = feeds[feed_idx % len(feeds)]
        lcd.LCD_ShowImage(render_loading_frame(W, H, fd.get("name", "Feed"), fd.get("url", ""), msg=msg, fonts=fonts), 0, 0)
        articles = fetch_feed(fd["url"])
        article_idx = 0
        scroll_offset = 0
        detail_scroll = 0
        view_mode = "list"

    _load_current_feed("Fetching RSS feed...")

    reader_vis = 8
    reader_max_sc = 0
    dirty = True

    try:
        with text_input_mode():
            flush_input()
            while running:
                m = _layout_metrics(W, H, fonts)
                vis_list = max(1, (H - m["hdr_h"] - m["tab_h"] - m["ftr_h"] - m["v_top"] - m["v_bot"]) // m["row_h"])

                if articles:
                    article_idx = max(0, min(article_idx, len(articles) - 1))
                    if article_idx < scroll_offset:
                        scroll_offset = article_idx
                    elif article_idx >= scroll_offset + vis_list:
                        scroll_offset = article_idx - vis_list + 1
                else:
                    article_idx = 0
                    scroll_offset = 0

                if dirty:
                    frame, reader_vis, reader_max_sc = render_news_frame(
                        W, H, feeds, feed_idx, articles, article_idx,
                        scroll_offset, detail_scroll, mode=view_mode, fonts=fonts,
                    )
                    lcd.LCD_ShowImage(frame, 0, 0)
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO)
                if act is None and ch is None:
                    time.sleep(0.035)
                    continue

                # PgUp / PgDn always scrolls the article reader directly!
                if act == "PAGEUP":
                    detail_scroll = max(0, detail_scroll - max(1, reader_vis - 1))
                    dirty = True
                    continue
                elif act == "PAGEDOWN":
                    detail_scroll = min(reader_max_sc, detail_scroll + max(1, reader_vis - 1))
                    dirty = True
                    continue

                if view_mode == "detail":
                    if is_back(act):
                        view_mode = "list"
                        if not split:
                            detail_scroll = 0
                        dirty = True
                    elif is_confirm(act) and split:
                        view_mode = "list"
                        dirty = True
                    elif act == "UP":
                        detail_scroll = max(0, detail_scroll - 1)
                        dirty = True
                    elif act == "DOWN":
                        detail_scroll = min(reader_max_sc, detail_scroll + 1)
                        dirty = True
                    elif act == "HOME":
                        detail_scroll = 0
                        dirty = True
                    elif act == "END":
                        detail_scroll = reader_max_sc
                        dirty = True
                    elif act == "LEFT":
                        feed_idx = (feed_idx - 1) % len(feeds)
                        _load_current_feed("Switching feed...")
                        flush_input()
                        dirty = True
                    elif act in ("RIGHT", "TAB"):
                        feed_idx = (feed_idx + 1) % len(feeds)
                        _load_current_feed("Switching feed...")
                        flush_input()
                        dirty = True
                    elif is_action(act, ch, "KEY2", "r"):
                        _load_current_feed("Refreshing feed...")
                        flush_input()
                        dirty = True
                    continue

                # view_mode == "list"
                if is_back(act):
                    break
                elif act == "UP" and articles:
                    article_idx = (article_idx - 1) % len(articles)
                    detail_scroll = 0
                    dirty = True
                elif act == "DOWN" and articles:
                    article_idx = (article_idx + 1) % len(articles)
                    detail_scroll = 0
                    dirty = True
                elif act == "HOME" and articles:
                    article_idx = 0
                    detail_scroll = 0
                    dirty = True
                elif act == "END" and articles:
                    article_idx = len(articles) - 1
                    detail_scroll = 0
                    dirty = True
                elif is_confirm(act) and articles:
                    view_mode = "detail"
                    dirty = True
                elif act in ("RIGHT", "TAB"):
                    feed_idx = (feed_idx + 1) % len(feeds)
                    _load_current_feed("Switching feed...")
                    flush_input()
                    dirty = True
                elif act == "LEFT":
                    feed_idx = (feed_idx - 1) % len(feeds)
                    _load_current_feed("Switching feed...")
                    flush_input()
                    dirty = True
                elif is_action(act, ch, "KEY2", "r"):
                    _load_current_feed("Refreshing feed...")
                    flush_input()
                    dirty = True
    finally:
        running = False
        try:
            lcd.LCD_Clear()
        except Exception:
            pass
        try:
            GPIO.cleanup()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
