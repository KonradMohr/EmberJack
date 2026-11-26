#!/usr/bin/env python3
"""
RaspyJack Payload -- Complete Interactive User Guide & Reference
================================================================
Obsidian Ember split-pane reference manual covering every RaspyJack menu,
built-in app, keyboard/mouse control, payload category, and Raspberry Pi tip.

Controls:
  UP / DOWN                 Select topic (List focus) OR scroll article (Reader focus)
  LEFT / RIGHT / TAB        Switch category tabs (ALL / START / APPS / NET / PAYLOADS / TIPS)
  1..6                      Jump directly to a tab
  ENTER / OK                Toggle focus between Topic List and Article Reader
  PgUp / PgDn  (or [ / ])   Scroll article smoothly (works from either focus!)
  /  or  F1  or  S          Full-text search across every topic & shortcut
  F2  or  F                 Toggle Full-Width Reader view (more reading room!)
  Esc / Backspace / Q / F3  Back to Topic List -> Clear search -> Exit Guide
"""

import os
import signal
import sys
import time
from typing import List, Optional, Tuple

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

from PIL import Image, ImageDraw

import rj_env
import rj_guide
import rj_theme


def _load_fonts(width: int, height: int) -> dict:
    wide = width >= 300
    large = width >= 540 and height >= 300
    if large:
        t_px, h_px, b_px, s_px, tag_px = 15, 13, 12, 11, 10
    elif wide:
        t_px, h_px, b_px, s_px, tag_px = 12, 11, 10, 9, 8
    elif width >= 200:
        t_px, h_px, b_px, s_px, tag_px = 11, 10, 9, 8, 8
    else:
        t_px, h_px, b_px, s_px, tag_px = 9, 8, 8, 7, 7
    return {
        "wide": wide,
        "large": large,
        "title": rj_env.load_font(t_px, bold=True),
        "heading": rj_env.load_font(h_px, bold=True),
        "body": rj_env.load_font(b_px, bold=False),
        "body_bold": rj_env.load_font(b_px, bold=True),
        "sub": rj_env.load_font(s_px, bold=False),
        "badge": rj_env.load_font(tag_px, bold=True, mono=True),
    }


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, max_w: int, font) -> List[str]:
    """Word-wrap *text* to fit within *max_w* pixels."""
    max_w = max(32, int(max_w))
    words = (text or "").split()
    if not words:
        return [""]
    lines: List[str] = []
    cur = ""
    for w in words:
        cand = (cur + " " + w).strip()
        if draw.textbbox((0, 0), cand, font=font)[2] <= max_w:
            cur = cand
        else:
            if cur:
                lines.append(cur)
                cur = w
            else:
                lines.append(rj_theme.truncate_text(draw, w, max_w, font))
                cur = ""
    if cur:
        lines.append(cur)
    return lines


_ARTICLE_LINE_CACHE: dict = {}


def build_article_lines(
    topic: dict,
    pane_w: int,
    fonts: dict,
) -> List[Tuple[str, object]]:
    """Pre-wrap a guide topic's structured blocks into uniform scrollable display rows (cached).

    Each returned row is a tuple `(kind, payload)`:
      - ("title", str)
      - ("summary", str)
      - ("spacer", None)
      - ("heading", str)
      - ("text", str)
      - ("bullet", str)
      - ("key", (key_str, desc_line, kw))
      - ("key_cont", (desc_line, kw))
      - ("tool_hdr", (tool_id, tool_title, hw_tags))
      - ("tool_desc", str)
      - ("tip", str)
      - ("warn", str)
    """
    cache_key = (topic.get("id", ""), int(pane_w), bool(fonts["wide"]), bool(fonts["large"]))
    cached = _ARTICLE_LINE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    dummy = Image.new("RGB", (1, 1))
    d = ImageDraw.Draw(dummy)
    inner_w = max(60, pane_w - (22 if fonts["wide"] else 8))
    rows: List[Tuple[str, object]] = []

    for ln in _wrap_text(d, topic.get("title", "Guide"), inner_w, fonts["title"]):
        rows.append(("title", ln))
    summary = topic.get("summary", "")
    if summary:
        for ln in _wrap_text(d, summary, inner_w, fonts["sub"]):
            rows.append(("summary", ln))
    rows.append(("spacer", None))

    for blk in topic.get("blocks", ()):
        kind = blk[0]
        if kind == "h":
            if rows and rows[-1][0] != "spacer":
                rows.append(("spacer", None))
            for ln in _wrap_text(d, blk[1], inner_w, fonts["heading"]):
                rows.append(("heading", ln))
        elif kind == "p":
            for ln in _wrap_text(d, blk[1], inner_w, fonts["body"]):
                rows.append(("text", ln))
        elif kind == "b":
            b_lines = _wrap_text(d, blk[1], inner_w - (14 if fonts["wide"] else 8), fonts["body"])
            for idx, ln in enumerate(b_lines):
                rows.append(("bullet" if idx == 0 else "bullet_cont", ln))
        elif kind == "k":
            key_lbl = str(blk[1])
            desc_txt = str(blk[2]) if len(blk) > 2 else ""
            if fonts["wide"]:
                kw = min(int(inner_w * 0.44), d.textbbox((0, 0), key_lbl, font=fonts["badge"])[2] + 12)
                desc_w = max(50, inner_w - kw - 8)
                d_lines = _wrap_text(d, desc_txt, desc_w, fonts["body"])
                rows.append(("key", (key_lbl, d_lines[0], kw)))
                for extra in d_lines[1:]:
                    rows.append(("key_cont", (extra, kw)))
            else:
                for ln in _wrap_text(d, f"[{key_lbl}] {desc_txt}", inner_w, fonts["sub"]):
                    rows.append(("text", ln))
        elif kind == "tool":
            t_id = str(blk[1])
            t_title = str(blk[2])
            t_desc = str(blk[3]) if len(blk) > 3 else ""
            hw_tags = tuple(blk[4]) if len(blk) > 4 else ()
            rows.append(("tool_hdr", (t_id, t_title, hw_tags)))
            indent = 10 if fonts["wide"] else 4
            for ln in _wrap_text(d, t_desc, inner_w - indent, fonts["sub"] if not fonts["large"] else fonts["body"]):
                rows.append(("tool_desc", ln))
        elif kind in ("tip", "warn"):
            prefix = "TIP: " if kind == "tip" else "NOTE: "
            c_lines = _wrap_text(d, prefix + blk[1], inner_w - (12 if fonts["wide"] else 4), fonts["sub"])
            for ln in c_lines:
                rows.append((kind, ln))

    if len(_ARTICLE_LINE_CACHE) > 96:
        _ARTICLE_LINE_CACHE.clear()
    _ARTICLE_LINE_CACHE[cache_key] = rows
    return rows


def find_first_match_row(rows: List[Tuple[str, object]], query: str) -> int:
    """Return the first row index inside *rows* matching *query* (for instant search jump)."""
    q = (query or "").strip().lower()
    if not q:
        return 0
    # Pass 1: exact/direct tool header or keybind match
    for idx, (kind, payload) in enumerate(rows):
        if kind == "tool_hdr":
            t_id, t_title, hw_tags = payload
            if q in f"{t_id} {t_title} {' '.join(hw_tags)}".lower():
                return max(0, idx - 1)
        elif kind == "key":
            if q in f"{payload[0]} {payload[1]}".lower():
                return max(0, idx - 1)
    # Pass 2: any text/description match
    for idx, (kind, payload) in enumerate(rows):
        if payload is None:
            continue
        blob = " ".join(str(x) for x in payload) if isinstance(payload, tuple) else str(payload)
        if q in blob.lower():
            return max(0, idx - 1)
    return 0


def render_guide_frame(
    width: int,
    height: int,
    topics: List[dict],
    tab: str = "all",
    query: str = "",
    cursor: int = 0,
    list_scroll: int = 0,
    article_scroll: int = 0,
    focus: str = "list",      # "list" | "reader"
    full_reader: bool = False,
    fonts: Optional[dict] = None,
) -> Tuple[Image.Image, int]:
    """Pure Obsidian Ember PIL renderer for the RaspyJack User Guide.

    Returns `(pil_image, max_article_scroll)` so callers can clamp scrolling accurately.
    """
    ET = rj_theme.EMBER_THEME
    f = fonts or _load_fonts(width, height)
    wide = f["wide"]
    large = f["large"]

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    hdr_h = 30 if large else (24 if wide else 18)
    tab_h = 24 if large else (18 if wide else 14)
    ftr_h = 22 if large else (18 if wide else 13)

    # 1. Header bar
    d.rectangle((0, 0, width - 1, hdr_h - 1), fill=ET["bg_elevated"])
    d.line([(0, hdr_h - 1), (width - 1, hdr_h - 1)], fill=ET["border_subtle"], width=1)
    hx = 8 if wide else 3
    title_txt = "EMBERJACK GUIDE" if large else "GUIDE"
    tb = d.textbbox((0, 0), title_txt, font=f["title"])
    d.text((hx, (hdr_h - (tb[3] - tb[1])) // 2 - tb[1]), title_txt, font=f["title"], fill=ET["accent_bright"])
    hx += (tb[2] - tb[0]) + (8 if wide else 4)

    filtered = rj_guide.filter_topics(topics, tab=tab, query=query)
    cw, _ = rj_theme.draw_pill(
        d, hx, max(2, (hdr_h - (16 if wide else 11)) // 2),
        f"{len(filtered)} TOPICS" if large else str(len(filtered)),
        f["badge"], bg=ET["select_bg"], fg=ET["accent_bright"],
        outline=ET["border_accent"], pad_x=5 if wide else 3, pad_y=1, radius=3,
    )
    hx += cw + 8

    if query and wide:
        q_lbl = rj_theme.truncate_text(d, f"/{query}", max(60, width // 3), f["badge"])
        rj_theme.draw_pill(
            d, hx, max(2, (hdr_h - 16) // 2), q_lbl, f["badge"],
            bg=ET["bg_well"], fg=ET["text_bright"], outline=ET["accent"],
            pad_x=5, pad_y=1, radius=3,
        )

    if wide:
        mode_lbl = "FULL READER" if full_reader else ("READING" if focus == "reader" else "TOPICS")
        mw = d.textbbox((0, 0), mode_lbl, font=f["badge"])[2] + 10
        rj_theme.draw_pill(
            d, width - mw - 6, max(2, (hdr_h - 16) // 2), mode_lbl, f["badge"],
            bg=ET["select_bg"] if focus == "reader" or full_reader else ET["bg_surface"],
            fg=ET["accent_bright"] if focus == "reader" or full_reader else ET["ok"],
            outline=ET["border_accent"] if focus == "reader" or full_reader else ET["border_subtle"],
            pad_x=5, pad_y=1, radius=3,
        )

    # 2. Category tabs bar
    d.rectangle((0, hdr_h, width - 1, hdr_h + tab_h - 1), fill=ET["bg_surface"])
    d.line([(0, hdr_h + tab_h - 1), (width - 1, hdr_h + tab_h - 1)], fill=ET["border_subtle"], width=1)
    tx = 6 if wide else 2
    for t_key in rj_guide.GUIDE_TABS:
        lbl = rj_guide.GUIDE_TAB_LABELS[t_key]
        active = (t_key == tab)
        tw, _ = rj_theme.draw_pill(
            d, tx, hdr_h + max(2, (tab_h - (16 if wide else 11)) // 2),
            lbl, f["badge"],
            bg=ET["select_bg"] if active else ET["bg_well"],
            fg=ET["accent_bright"] if active else ET["text_muted"],
            outline=ET["accent"] if active else ET["border_subtle"],
            pad_x=6 if large else (4 if wide else 2), pad_y=1, radius=4,
        )
        tx += tw + (6 if large else (4 if wide else 2))
        if tx > width - 20:
            break

    # 3. Main Viewport
    y0 = hdr_h + tab_h + (5 if wide else 2)
    y1 = height - ftr_h - (5 if wide else 2)
    max_art_scroll = 0

    if not filtered:
        cy = (y0 + y1) // 2
        m1 = f'No guide topics match "{query}"' if query else "No guide topics in this tab"
        m2 = "Press Esc / Backspace to clear search, or Tab to switch tabs" if wide else "Esc:Clear"
        d.text((width // 2, cy - (10 if wide else 6)), rj_theme.truncate_text(d, m1, width - 16, f["body_bold"]), font=f["body_bold"], fill=ET["text_primary"], anchor="mm")
        d.text((width // 2, cy + (10 if wide else 6)), rj_theme.truncate_text(d, m2, width - 16, f["sub"]), font=f["sub"], fill=ET["text_muted"], anchor="mm")
    else:
        cursor = max(0, min(cursor, len(filtered) - 1))
        split = (width >= 480) and (not full_reader)
        show_list = split or (focus == "list" and not full_reader)
        show_reader = split or (focus == "reader") or full_reader

        list_x0 = 8 if wide else 2
        list_x1 = int(width * 0.37) if split else (width - (10 if wide else 5))

        if show_list:
            row_h = 34 if large else (24 if wide else 18)
            vis = max(1, (y1 - y0) // row_h)
            st = max(0, min(list_scroll, max(0, len(filtered) - vis)))
            for slot in range(vis):
                idx = st + slot
                if idx >= len(filtered):
                    break
                tp = filtered[idx]
                sel = (idx == cursor)
                ry = y0 + slot * row_h
                ry1 = ry + row_h - (4 if wide else 2)
                rj_theme.draw_card(
                    d, (list_x0, ry, list_x1, ry1),
                    fill=ET["select_bg"] if sel else ET["bg_surface"],
                    outline=(ET["accent"] if (sel and focus == "list") else (ET["border_accent"] if sel else ET["border_subtle"])),
                    radius=6 if wide else 3,
                    accent_left=ET["accent"] if sel else None,
                    accent_width=3 if wide else 2,
                )
                rx = list_x1 - (6 if wide else 3)
                if wide:
                    tag = str(tp.get("tag", ""))
                    if tag:
                        tw_p = d.textbbox((0, 0), tag, font=f["badge"])[2] + 8
                        rj_theme.draw_pill(
                            d, rx - tw_p, ry + max(3, (ry1 - ry - 15) // 2),
                            tag, f["badge"], bg=ET["bg_well"],
                            fg=ET["accent_bright"] if sel else ET["text_muted"],
                            outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3,
                        )
                        rx -= tw_p + 5
                nx = list_x0 + (8 if wide else 4)
                nm = rj_theme.truncate_text(d, tp.get("title", "?"), max(30, rx - nx), f["body_bold"] if wide else f["body"])
                d.text((nx, ry + (3 if large else 2)), nm, font=f["body_bold"] if wide else f["body"], fill=ET["text_bright"] if sel else ET["text_primary"])
                if large:
                    sub = rj_theme.truncate_text(d, tp.get("summary", ""), max(30, rx - nx), f["sub"])
                    d.text((nx, ry + 17), sub, font=f["sub"], fill=ET["text_muted"])

            if len(filtered) > vis:
                rj_theme.draw_scrollbar(d, list_x1 + 2, y0, y1, st, vis, len(filtered), width=4 if wide else 2)

        if show_reader:
            rx0 = (list_x1 + 10) if split else (8 if wide else 2)
            rx1 = width - (8 if wide else 2)
            pane_w = max(80, rx1 - rx0)
            rj_theme.draw_card(
                d, (rx0, y0, rx1, y1),
                fill=ET["bg_surface"],
                outline=ET["accent"] if (focus == "reader" or full_reader) else ET["border_subtle"],
                radius=7 if wide else 3,
                accent_left=ET["accent"] if (focus == "reader" or full_reader) else None,
                accent_width=3 if wide else 2,
            )

            art_rows = build_article_lines(filtered[cursor], pane_w, f)
            line_h = 17 if large else (14 if wide else 11)
            pad_top = 8 if wide else 3
            vis_lines = max(1, (y1 - y0 - pad_top * 2) // line_h)
            max_art_scroll = max(0, len(art_rows) - vis_lines)
            asc = max(0, min(article_scroll, max_art_scroll))

            ix = rx0 + (12 if wide else 4)
            iw = pane_w - (24 if wide else 8)
            for s_i in range(vis_lines):
                r_idx = asc + s_i
                if r_idx >= len(art_rows):
                    break
                r_kind, payload = art_rows[r_idx]
                ly = y0 + pad_top + s_i * line_h
                if r_kind == "spacer":
                    continue
                elif r_kind == "title":
                    d.text((ix, ly), str(payload), font=f["title"], fill=ET["accent_bright"])
                elif r_kind == "summary":
                    d.text((ix, ly), str(payload), font=f["sub"], fill=ET["text_muted"])
                elif r_kind == "heading":
                    d.text((ix, ly + 1), str(payload), font=f["heading"], fill=ET["accent"])
                    d.line([(ix, ly + line_h - 1), (rx1 - 14, ly + line_h - 1)], fill=ET["border_subtle"], width=1)
                elif r_kind == "text":
                    d.text((ix, ly + 1), str(payload), font=f["body"], fill=ET["text_primary"])
                elif r_kind == "bullet":
                    d.ellipse((ix + 2, ly + line_h // 2 - 2, ix + 6, ly + line_h // 2 + 2), fill=ET["accent"])
                    d.text((ix + (12 if wide else 7), ly + 1), str(payload), font=f["body"], fill=ET["text_primary"])
                elif r_kind == "bullet_cont":
                    d.text((ix + (12 if wide else 7), ly + 1), str(payload), font=f["body"], fill=ET["text_primary"])
                elif r_kind == "key":
                    key_lbl, desc_ln, kw = payload
                    rj_theme.draw_pill(
                        d, ix, ly, key_lbl, f["badge"],
                        bg=ET["bg_well"], fg=ET["accent_bright"],
                        outline=ET["border_accent"], pad_x=4, pad_y=1, radius=3,
                    )
                    d.text((ix + kw + 6, ly + 1), desc_ln, font=f["body"], fill=ET["text_primary"])
                elif r_kind == "key_cont":
                    desc_ln, kw = payload
                    d.text((ix + kw + 6, ly + 1), desc_ln, font=f["body"], fill=ET["text_primary"])
                elif r_kind == "tool_hdr":
                    t_id, t_title, hw_tags = payload
                    q_low = (query or "").strip().lower()
                    is_hit = bool(q_low and q_low in f"{t_id} {t_title} {' '.join(hw_tags)}".lower())
                    px_t = ix
                    if wide:
                        pw_t, _ = rj_theme.draw_pill(
                            d, px_t, ly, t_id, f["badge"],
                            bg=ET["accent"] if is_hit else ET["select_bg"],
                            fg=ET["text_on_accent"] if is_hit else ET["accent_bright"],
                            outline=ET["accent"] if is_hit else ET["border_accent"],
                            pad_x=4, pad_y=1, radius=3,
                        )
                        px_t += pw_t + 6
                        # Reserve right side for compact hardware badge(s) if present
                        hw_rx = rx1 - 14
                        if hw_tags and large:
                            for hw in reversed(hw_tags[:2]):
                                hw_w = d.textbbox((0, 0), hw, font=f["badge"])[2] + 8
                                if hw_rx - hw_w > px_t + 48:
                                    rj_theme.draw_pill(
                                        d, hw_rx - hw_w, ly, hw, f["badge"],
                                        bg=ET["bg_well"], fg=ET["warn"],
                                        outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3,
                                    )
                                    hw_rx -= hw_w + 4
                        avail_t = max(30, hw_rx - px_t)
                        d.text(
                            (px_t, ly + 1),
                            rj_theme.truncate_text(d, t_title, avail_t, f["body_bold"]),
                            font=f["body_bold"],
                            fill=ET["accent_bright"] if is_hit else ET["text_bright"],
                        )
                    else:
                        d.text(
                            (ix, ly + 1),
                            rj_theme.truncate_text(d, f"{t_id}: {t_title}", iw, f["body_bold"]),
                            font=f["body_bold"],
                            fill=ET["accent_bright"] if is_hit else ET["text_bright"],
                        )
                elif r_kind == "tool_desc":
                    indent = 10 if wide else 4
                    d.text((ix + indent, ly + 1), str(payload), font=f["body"] if large else f["sub"], fill=ET["text_primary"] if large else ET["text_muted"])
                elif r_kind == "tip":
                    d.rectangle((ix, ly, ix + 3, ly + line_h - 2), fill=ET["ok"])
                    d.text((ix + 8, ly + 1), str(payload), font=f["sub"], fill=ET["ok"])
                elif r_kind == "warn":
                    d.rectangle((ix, ly, ix + 3, ly + line_h - 2), fill=ET["warn"])
                    d.text((ix + 8, ly + 1), str(payload), font=f["sub"], fill=ET["warn"])

            if len(art_rows) > vis_lines:
                rj_theme.draw_scrollbar(d, rx1 - 6, y0 + 6, y1 - 6, asc, vis_lines, len(art_rows), width=4 if wide else 2)

    # 4. Footer bar
    d.rectangle((0, height - ftr_h, width - 1, height - 1), fill=ET["bg_elevated"])
    d.line([(0, height - ftr_h), (width - 1, height - ftr_h)], fill=ET["border_subtle"], width=1)
    if large:
        if focus == "reader" or full_reader:
            hint = "Up/Dn/PgUp/PgDn:Scroll  F/F2:Split/Full  /:Search  Left/Esc/Bksp:Topics"
        else:
            hint = "Up/Dn:Topic  Enter:Read  PgUp/PgDn:Scroll  Tab/1-6:Section  /:Search  F:Full  Bksp/Esc:Exit"
    elif wide:
        hint = "Up/Dn:Nav  OK:Read  F1:Search  F2:Full  Bksp/Esc:Back"
    else:
        hint = "OK:Read K1:Find K3:Back"
    hb = d.textbbox((0, 0), hint, font=f["sub"])
    d.text((6 if wide else 3, height - ftr_h + (ftr_h - (hb[3] - hb[1])) // 2 - hb[1]), rj_theme.truncate_text(d, hint, width - 10, f["sub"]), font=f["sub"], fill=ET["text_muted"])

    return img, max_art_scroll


def main() -> int:
    import RPi.GPIO as GPIO
    import LCD_1in44
    from payloads._input_helper import poll_ui_input, flush_input, text_input_mode
    from payloads._keyboard_helper import lcd_keyboard

    PINS = {
        "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
        "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
    }
    GPIO.setmode(GPIO.BCM)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    W, H = lcd.width, lcd.height
    fonts = _load_fonts(W, H)

    running = True

    def _on_sig(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _on_sig)
    signal.signal(signal.SIGTERM, _on_sig)

    topics = rj_guide.get_all_topics()
    tab_idx = 0
    query = ""
    cursor = 0
    list_scroll = 0
    article_scroll = 0
    max_art_scroll = 0
    focus = "list"
    full_reader = False

    def _auto_scroll_for_query(filtered_list: List[dict]) -> int:
        if not query or not filtered_list:
            return 0
        idx = max(0, min(cursor, len(filtered_list) - 1))
        split_now = (W >= 480) and (not full_reader)
        pane_w = (W - int(W * 0.37) - 18) if split_now else (W - (16 if fonts["wide"] else 4))
        rows = build_article_lines(filtered_list[idx], max(80, pane_w), fonts)
        return find_first_match_row(rows, query)

    def _render() -> None:
        nonlocal max_art_scroll, article_scroll
        frame, max_art_scroll = render_guide_frame(
            W, H,
            topics=topics,
            tab=rj_guide.GUIDE_TABS[tab_idx],
            query=query,
            cursor=cursor,
            list_scroll=list_scroll,
            article_scroll=article_scroll,
            focus=focus,
            full_reader=full_reader,
            fonts=fonts,
        )
        article_scroll = max(0, min(article_scroll, max_art_scroll))
        lcd.LCD_ShowImage(frame, 0, 0)

    try:
        with text_input_mode():
            flush_input()
            dirty = True
            last_active_at = time.monotonic()

            while running:
                filtered = rj_guide.filter_topics(topics, tab=rj_guide.GUIDE_TABS[tab_idx], query=query)
                if filtered:
                    cursor = max(0, min(cursor, len(filtered) - 1))
                    row_h = 34 if fonts["large"] else (24 if fonts["wide"] else 18)
                    hdr_h = 30 if fonts["large"] else (24 if fonts["wide"] else 18)
                    tab_h = 24 if fonts["large"] else (18 if fonts["wide"] else 14)
                    ftr_h = 22 if fonts["large"] else (18 if fonts["wide"] else 13)
                    vis = max(1, (H - hdr_h - tab_h - ftr_h - 10) // row_h)
                    if cursor < list_scroll:
                        list_scroll = cursor
                    elif cursor >= list_scroll + vis:
                        list_scroll = cursor - vis + 1
                else:
                    cursor = 0
                    list_scroll = 0
                    article_scroll = 0
                    focus = "list"
                    full_reader = False

                if dirty:
                    _render()
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO)
                if act is None and ch is None:
                    time.sleep(0.025 if (time.monotonic() - last_active_at) < 1.5 else 0.05)
                    continue

                last_active_at = time.monotonic()
                kl = ch.lower() if isinstance(ch, str) else ""

                # Back / Exit hierarchy: Full Reader / Reader focus -> Topic List -> Clear Search -> Exit
                if act in ("KEY3", "ESCAPE", "BACKSPACE") or kl == "q":
                    if full_reader:
                        full_reader = False
                        focus = "list"
                        dirty = True
                    elif focus == "reader":
                        focus = "list"
                        dirty = True
                    elif query:
                        query = ""
                        cursor = 0
                        list_scroll = 0
                        article_scroll = 0
                        dirty = True
                    else:
                        break
                elif act == "KEY1" or ch == "/" or kl == "s":
                    res = lcd_keyboard(lcd, fonts["body"], PINS, GPIO, title="Search EmberJack Guide", default=query, charset="full", max_len=40)
                    if res is not None:
                        query = res.strip()
                        cursor = 0
                        list_scroll = 0
                        focus = "list"
                        full_reader = False
                        new_f = rj_guide.filter_topics(topics, tab=rj_guide.GUIDE_TABS[tab_idx], query=query)
                        article_scroll = _auto_scroll_for_query(new_f)
                    flush_input()
                    dirty = True
                elif act == "KEY2" or kl == "f":
                    if filtered:
                        full_reader = not full_reader
                        focus = "reader" if full_reader else "list"
                        dirty = True
                elif ch in ("1", "2", "3", "4", "5", "6"):
                    tab_idx = int(ch) - 1
                    cursor = 0
                    list_scroll = 0
                    article_scroll = 0
                    focus = "list"
                    full_reader = False
                    dirty = True
                elif act == "TAB":
                    tab_idx = (tab_idx + 1) % len(rj_guide.GUIDE_TABS)
                    cursor = 0
                    list_scroll = 0
                    article_scroll = 0
                    focus = "list"
                    full_reader = False
                    dirty = True
                elif act == "PAGEUP" or ch == "[":
                    article_scroll = max(0, article_scroll - 7)
                    dirty = True
                elif act == "PAGEDOWN" or ch == "]":
                    article_scroll = min(max_art_scroll, article_scroll + 7)
                    dirty = True
                elif act == "HOME":
                    if focus == "reader" or full_reader:
                        article_scroll = 0
                    else:
                        cursor = 0
                        article_scroll = 0
                    dirty = True
                elif act == "END":
                    if focus == "reader" or full_reader:
                        article_scroll = max_art_scroll
                    elif filtered:
                        cursor = len(filtered) - 1
                        article_scroll = 0
                    dirty = True
                elif act == "UP":
                    if focus == "reader" or full_reader:
                        article_scroll = max(0, article_scroll - 2)
                        dirty = True
                    elif filtered:
                        cursor = (cursor - 1) % len(filtered)
                        article_scroll = _auto_scroll_for_query(filtered)
                        dirty = True
                elif act == "DOWN":
                    if focus == "reader" or full_reader:
                        article_scroll = min(max_art_scroll, article_scroll + 2)
                        dirty = True
                    elif filtered:
                        cursor = (cursor + 1) % len(filtered)
                        article_scroll = _auto_scroll_for_query(filtered)
                        dirty = True
                elif act == "LEFT":
                    if focus == "reader" or full_reader:
                        full_reader = False
                        focus = "list"
                    else:
                        tab_idx = (tab_idx - 1) % len(rj_guide.GUIDE_TABS)
                        cursor = 0
                        list_scroll = 0
                        article_scroll = 0
                    dirty = True
                elif act == "RIGHT":
                    if focus == "reader" or full_reader:
                        article_scroll = min(max_art_scroll, article_scroll + 5)
                    else:
                        tab_idx = (tab_idx + 1) % len(rj_guide.GUIDE_TABS)
                        cursor = 0
                        list_scroll = 0
                        article_scroll = 0
                    dirty = True
                elif act in ("OK", "ENTER"):
                    if filtered:
                        if focus == "list":
                            focus = "reader"
                        else:
                            full_reader = not full_reader
                        dirty = True
    finally:
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
