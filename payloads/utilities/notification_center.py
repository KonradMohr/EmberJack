#!/usr/bin/env python3
"""
EmberJack Payload -- Notification Center
========================================
Author: 7h30th3r0n3

Aggregates notifications from all payloads. Watches the loot directory
for new files and reads a structured notification log where payloads
append JSON-line events.

Controls:
  UP / DOWN / PgUp / PgDn  -- Scroll notifications (newest first)
  OK / ENTER               -- Toggle selected notification read / unread
  M                        -- Mark all notifications as read
  TAB / F                  -- Filter (All / Unread / Critical / Warning)
  KEY1 / C                 -- Clear all notifications (with confirmation)
  KEY2 / P                 -- Push unread notifications to enabled channels
  LEFT / W                 -- Open channel config (Discord / Slack / HTTP POST)
  R                        -- Force refresh feed
  KEY3 / ESC / Q           -- Back / Exit
"""

import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import LCD_Config
from PIL import Image, ImageDraw

import rj_env
import rj_theme
from payloads._input_helper import flush_input, poll_ui_input, text_input_mode
from payloads._keyboard_helper import lcd_keyboard

ET = rj_theme.EMBER_THEME

PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}

BASE_DIR = rj_env.get_base_dir()
LOOT_ROOT = os.path.join(BASE_DIR, "loot")
NOTIF_FILE = os.path.join(LOOT_ROOT, ".notifications.jsonl")
WEBHOOK_FILE = os.path.join(BASE_DIR, "discord_webhook.txt")
CHANNELS_DIR = os.path.join(LOOT_ROOT, "Notifications")
CHANNELS_FILE = os.path.join(CHANNELS_DIR, "channels.json")
POLL_INTERVAL = 10

FILTERS = ("all", "unread", "critical", "warning")
FILTER_LABELS = {"all": "ALL", "unread": "UNREAD", "critical": "CRIT", "warning": "WARN"}

lock = threading.Lock()
_running = True

notifications = []
read_set = set()


def _cleanup(*_):
    global _running
    _running = False


signal.signal(signal.SIGINT, _cleanup)
signal.signal(signal.SIGTERM, _cleanup)


# ---------------------------------------------------------------------------
# Notification loading
# ---------------------------------------------------------------------------

def _load_notifications():
    """Parse the JSONL file and return a list sorted newest-first."""
    items = []
    if not os.path.isfile(NOTIF_FILE):
        return items
    try:
        with open(NOTIF_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and "timestamp" in obj:
                        items.append(obj)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return items


def _scan_new_loot_files():
    """Find loot files created in the last 5 minutes and generate notifications."""
    now = time.time()
    new_items = []
    try:
        for root, _dirs, files in os.walk(LOOT_ROOT):
            for fname in files:
                if fname.startswith("."):
                    continue
                full = os.path.join(root, fname)
                try:
                    mtime = os.path.getmtime(full)
                except OSError:
                    continue
                if (now - mtime) < 300:
                    rel = os.path.relpath(full, LOOT_ROOT)
                    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(mtime))
                    new_items.append({
                        "timestamp": ts,
                        "source": "loot-watcher",
                        "message": f"New loot: {rel}",
                        "severity": "info",
                    })
    except OSError:
        pass
    return new_items


def _merge_notifications(base, extra):
    """Merge two notification lists, deduplicate by timestamp+message, newest first."""
    seen = set()
    merged = []
    for item in base + extra:
        key = (item.get("timestamp", ""), item.get("message", ""))
        if key not in seen:
            seen.add(key)
            merged.append(item)
    merged.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return merged


def _poll_thread():
    """Periodically reload notifications and scan for new loot."""
    global notifications
    while _running:
        jsonl_items = _load_notifications()
        loot_items = _scan_new_loot_files()
        merged = _merge_notifications(jsonl_items, loot_items)
        with lock:
            notifications = merged

        deadline = time.time() + POLL_INTERVAL
        while _running and time.time() < deadline:
            time.sleep(0.4)


# ---------------------------------------------------------------------------
# Multi-channel push (Discord / Slack / HTTP POST)
# ---------------------------------------------------------------------------

def _load_webhook_url():
    """Read legacy Discord webhook URL from file, or return None."""
    try:
        with open(WEBHOOK_FILE, "r", encoding="utf-8") as fh:
            url = fh.read().strip()
        if url.startswith("http"):
            return url
    except OSError:
        pass
    return None


_DEFAULT_CHANNELS = {
    "discord": {"enabled": False, "url": ""},
    "slack": {"enabled": False, "url": ""},
    "http_post": {"enabled": False, "url": ""},
}
CHANNEL_NAMES = ["discord", "slack", "http_post"]
CHANNEL_LABELS = {"discord": "Discord Webhook", "slack": "Slack Webhook", "http_post": "HTTP POST"}


def _load_channels():
    """Load channel configs from channels.json, merging with defaults."""
    channels = {k: dict(v) for k, v in _DEFAULT_CHANNELS.items()}
    legacy_url = _load_webhook_url()
    if legacy_url:
        channels["discord"] = {"enabled": True, "url": legacy_url}
    if os.path.isfile(CHANNELS_FILE):
        try:
            with open(CHANNELS_FILE, "r", encoding="utf-8") as fh:
                saved = json.loads(fh.read())
            if isinstance(saved, dict):
                for name in CHANNEL_NAMES:
                    if name in saved and isinstance(saved[name], dict):
                        channels[name] = {
                            "enabled": bool(saved[name].get("enabled", False)),
                            "url": str(saved[name].get("url", "") or ""),
                        }
        except (json.JSONDecodeError, OSError):
            pass
    return channels


def _save_channels(channels):
    """Persist channel configs to channels.json."""
    try:
        os.makedirs(CHANNELS_DIR, exist_ok=True)
        with open(CHANNELS_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(channels, indent=2))
    except OSError:
        pass


def _push_discord(items, url=None):
    url = url or _load_webhook_url()
    if not url:
        return "No webhook URL"
    lines = []
    for item in items[:20]:
        sev = str(item.get("severity", "info")).upper()
        src = str(item.get("source", "?"))[:16]
        msg = str(item.get("message", ""))[:80]
        lines.append(f"[{sev}] {src}: {msg}")
    payload = json.dumps({
        "content": f"**EmberJack Notifications** ({len(items)} unread)\n```\n"
                   + "\n".join(lines) + "\n```"
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
        return f"Discord: sent {len(items)}"
    except (urllib.error.URLError, OSError) as exc:
        return f"Discord err: {str(exc)[:24]}"


def _push_slack(items, url):
    lines = []
    for item in items[:20]:
        sev = str(item.get("severity", "info")).upper()
        src = str(item.get("source", "?"))[:16]
        msg = str(item.get("message", ""))[:80]
        lines.append(f"[{sev}] {src}: {msg}")
    payload = json.dumps({
        "text": f"*EmberJack Notifications* ({len(items)} unread)\n```\n"
                + "\n".join(lines) + "\n```"
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
        return f"Slack: sent {len(items)}"
    except (urllib.error.URLError, OSError) as exc:
        return f"Slack err: {str(exc)[:24]}"


def _push_http_post(items, url):
    payload = json.dumps({
        "source": "emberjack",
        "count": len(items),
        "notifications": [
            {
                "severity": item.get("severity", "info"),
                "source": item.get("source", "?"),
                "message": item.get("message", ""),
                "timestamp": item.get("timestamp", ""),
            }
            for item in items[:20]
        ],
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
        return f"HTTP: sent {len(items)}"
    except (urllib.error.URLError, OSError) as exc:
        return f"HTTP err: {str(exc)[:24]}"


def _push_all_channels(items):
    channels = _load_channels()
    results = []
    if channels["discord"].get("enabled") and channels["discord"].get("url"):
        results.append(_push_discord(items, channels["discord"]["url"]))
    if channels["slack"].get("enabled") and channels["slack"].get("url"):
        results.append(_push_slack(items, channels["slack"]["url"]))
    if channels["http_post"].get("enabled") and channels["http_post"].get("url"):
        results.append(_push_http_post(items, channels["http_post"]["url"]))
    if not results:
        return "No channels enabled"
    return "; ".join(results)


# ---------------------------------------------------------------------------
# Obsidian Ember Adaptive Renderers
# ---------------------------------------------------------------------------

def _sev_style(sev: str):
    s = str(sev or "info").lower()
    if s in ("critical", "crit", "error", "err", "high"):
        return ("CRIT", ET["danger_bg"], ET["danger"])
    if s in ("warning", "warn", "medium"):
        return ("WARN", ET["warn_bg"], ET["warn"])
    return ("INFO", ET["ok_bg"], ET["ok"])


def _notif_key(item: dict) -> str:
    return f"{item.get('timestamp', '')}|{item.get('message', '')}"


def _is_read(item: dict) -> bool:
    ts = item.get("timestamp", "")
    return (ts in read_set) or (_notif_key(item) in read_set)


def _filter_items(items, flt: str):
    if flt == "unread":
        return [n for n in items if not _is_read(n)]
    if flt == "critical":
        return [n for n in items if str(n.get("severity", "")).lower() in ("critical", "crit", "error", "err", "high")]
    if flt == "warning":
        return [n for n in items if str(n.get("severity", "")).lower() in ("warning", "warn", "medium")]
    return list(items)


def render_notif_frame(
    width,
    height,
    all_items,
    filtered_items,
    cursor=0,
    scroll=0,
    active_filter="all",
    mode="list",
    channel_idx=0,
    toast="",
    toast_kind="info",
    fonts=None,
):
    """Pure PIL native resolution-adaptive Obsidian Ember Notification Center renderer."""
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = f["wide"]
    large = f["large"]
    split = width >= 460

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    unread_cnt = sum(1 for n in all_items if not _is_read(n))
    hdr_h = rj_theme.draw_app_header(
        d, width, "NOTIFICATIONS" if wide else "NOTIFS", f,
        badge=f"{unread_cnt} NEW" if unread_cnt else "0 NEW",
        right_text=f"{len(all_items)} total",
        right_pill=FILTER_LABELS.get(active_filter, "ALL"),
    )

    if mode == "confirm_clear":
        rj_theme.draw_app_footer(d, width, height, "Enter/OK: Confirm Clear  •  Esc/Any: Cancel", f)
        rj_theme.draw_status_card(
            d, width, height,
            "Clear All Notifications?",
            sub="Removes all logged alerts from .notifications.jsonl",
            kind="err", foot="OK / Enter = Yes  •  Esc = Cancel", fonts=f,
        )
        return img

    if mode == "channel_config":
        channels = _load_channels()
        ftr_h = rj_theme.draw_app_footer(
            d, width, height,
            "Up/Dn:Channel  Enter/OK:Toggle ON/OFF  K2/E:Edit URL  Esc/K3:Back" if wide else "OK:Tog K2:URL K3:Bk",
            f, right_text=f"{channel_idx + 1}/{len(CHANNEL_NAMES)}",
        )
        pad = 10 if large else (6 if wide else 3)
        y0 = hdr_h + (8 if large else 4)
        y1 = height - ftr_h - (8 if large else 4)
        row_h = max(44, (y1 - y0 - 8) // len(CHANNEL_NAMES)) if wide else max(28, (y1 - y0 - 4) // len(CHANNEL_NAMES))
        for idx, cname in enumerate(CHANNEL_NAMES):
            ch = channels.get(cname, {})
            enabled = bool(ch.get("enabled"))
            url = ch.get("url", "")
            is_sel = idx == (channel_idx % len(CHANNEL_NAMES))
            ry0 = y0 + idx * row_h
            ry1 = min(y1, ry0 + row_h - (6 if wide else 3))
            rj_theme.draw_card(
                d, (pad, ry0, width - pad, ry1),
                fill=ET["select_bg"] if is_sel else ET["bg_surface"],
                outline=ET["border_accent"] if is_sel else ET["border_subtle"],
                radius=6 if wide else 3,
                accent_left=ET["ok"] if enabled else (ET["accent"] if is_sel else None),
                accent_width=3 if wide else 2,
            )
            st_lbl = "ENABLED" if enabled else "OFF"
            sbb = rj_theme.measure_text(f["badge"], st_lbl, d)
            spw = (sbb[2] - sbb[0]) + (10 if wide else 6)
            rj_theme.draw_pill(
                d, width - pad - spw - (8 if wide else 4), ry0 + (8 if large else 4),
                st_lbl, f["badge"],
                bg=ET["ok_bg"] if enabled else ET["danger_bg"],
                fg=ET["ok"] if enabled else ET["danger"],
                outline=None, pad_x=5 if wide else 3, pad_y=1, radius=3,
            )
            lbl = CHANNEL_LABELS.get(cname, cname)
            d.text((pad + (12 if wide else 6), ry0 + (8 if large else 4)), lbl, font=f["body_bold"], fill=ET["text_bright"] if is_sel else ET["text_primary"])
            url_txt = url if url else "Webhook URL not set (press K2 / E to configure)"
            d.text(
                (pad + (12 if wide else 6), ry0 + (28 if large else (19 if wide else 14))),
                rj_theme.truncate_text(d, url_txt, width - pad * 2 - 24, f["sub"]),
                font=f["sub"], fill=ET["text_muted"] if url else ET["text_dim"],
            )
        return img

    if large:
        hint = "Enter:Read  •  Tab:Filter  •  K1/C:Clear  •  K2/P:Push  •  L/W:Channels  •  M:ReadAll  •  Esc:Exit"
    elif wide:
        hint = "OK:Read Tab:Flt K1:Clr K2:Push L:Ch K3:Exit"
    else:
        hint = "OK:Rd K1:Clr K2:Psh L:Ch K3:X"
    pos_txt = f"{min(cursor + 1, max(1, len(filtered_items)))}/{max(1, len(filtered_items))}"
    ftr_h = rj_theme.draw_app_footer(d, width, height, hint, f, right_text=pos_txt)

    # Filter bar on wide screens
    tab_h = (22 if large else 17) if wide else 0
    if tab_h:
        d.rectangle((0, hdr_h, width - 1, hdr_h + tab_h - 1), fill=ET["bg_surface"])
        d.line([(0, hdr_h + tab_h - 1), (width - 1, hdr_h + tab_h - 1)], fill=ET["border_subtle"], width=1)
        tx = 8
        for fkey in FILTERS:
            cnt = len(_filter_items(all_items, fkey))
            lbl = f"{FILTER_LABELS[fkey]} ({cnt})"
            active = fkey == active_filter
            pw, _ = rj_theme.draw_pill(
                d, tx, hdr_h + max(2, (tab_h - 14) // 2), lbl, f["badge"],
                bg=ET["select_bg"] if active else ET["bg_well"],
                fg=ET["accent_bright"] if active else ET["text_muted"],
                outline=ET["border_accent"] if active else ET["border_subtle"],
                pad_x=5, pad_y=1, radius=3,
            )
            tx += pw + 6

    toast_h = (22 if large else 16) if toast else 0
    y0 = hdr_h + tab_h + (7 if large else (4 if wide else 2))
    y1 = height - ftr_h - toast_h - (6 if large else (3 if wide else 2))
    pad = 10 if large else (6 if wide else 3)

    if not filtered_items:
        rj_theme.draw_status_card(
            d, width, height,
            "No Notifications",
            sub="Events from payloads & loot watcher appear here" if active_filter == "all" else f"No '{active_filter}' notifications (press Tab)",
            kind="info", foot="R: Refresh  •  L/W: Channels  •  Esc: Exit", fonts=f,
        )
    elif not split and mode == "detail":
        item = filtered_items[max(0, min(cursor, len(filtered_items) - 1))]
        sev_lbl, sev_bg, sev_fg = _sev_style(item.get("severity", "info"))
        rj_theme.draw_card(d, (pad, y0, width - pad, y1), fill=ET["bg_surface"], outline=sev_fg, radius=5 if wide else 3, accent_left=sev_fg, accent_width=3)
        dy = y0 + (8 if wide else 4)
        pw, _ = rj_theme.draw_pill(d, pad + 8, dy, sev_lbl, f["badge"], bg=sev_bg, fg=sev_fg, outline=None, pad_x=4, pad_y=1, radius=3)
        d.text((pad + 12 + pw, dy + 1), rj_theme.truncate_text(d, str(item.get("source", "?")), width - pad * 2 - pw - 22, f["body_bold"]), font=f["body_bold"], fill=ET["text_bright"])
        dy += 18 if wide else 13
        d.text((pad + 8, dy), rj_theme.truncate_text(d, str(item.get("timestamp", "")), width - pad * 2 - 16, f["sub"]), font=f["sub"], fill=ET["text_muted"])
        dy += 15 if wide else 11
        for ln in rj_theme.wrap_text(d, str(item.get("message", "")), width - pad * 2 - 16, f["body"]):
            if dy + 12 > y1 - 4:
                break
            d.text((pad + 8, dy), ln, font=f["body"], fill=ET["text_primary"])
            dy += 14 if wide else 10
    else:
        sb_w = 5 if large else (4 if wide else 2)
        list_x0 = pad
        list_x1 = int(width * 0.56) - 4 if split else (width - pad - sb_w - 3)
        row_h = 46 if large else (30 if wide else 21)
        vis = max(1, (y1 - y0) // row_h)
        st = max(0, min(scroll, max(0, len(filtered_items) - vis)))

        for slot in range(vis):
            idx = st + slot
            if idx >= len(filtered_items):
                break
            item = filtered_items[idx]
            is_sel = idx == cursor
            read = _is_read(item)
            sev_lbl, sev_bg, sev_fg = _sev_style(item.get("severity", "info"))

            ry0 = y0 + slot * row_h
            ry1 = ry0 + row_h - (5 if large else (3 if wide else 2))
            rj_theme.draw_card(
                d, (list_x0, ry0, list_x1, ry1),
                fill=ET["select_bg"] if is_sel else ET["bg_surface"],
                outline=ET["border_accent"] if is_sel else ET["border_subtle"],
                radius=6 if wide else 3,
                accent_left=sev_fg if not read else (ET["accent_dim"] if is_sel else None),
                accent_width=3 if wide else 2,
            )

            # Top row: severity pill + source pill + timestamp
            tx = list_x0 + (10 if wide else 4)
            ty = ry0 + (5 if large else (3 if wide else 2))
            if not read and wide:
                d.ellipse((tx, ty + 4, tx + 5, ty + 9), fill=ET["accent_bright"])
                tx += 9

            spw, _ = rj_theme.draw_pill(
                d, tx, ty, sev_lbl, f["badge"],
                bg=ET["bg_well"] if read else sev_bg,
                fg=ET["text_dim"] if read else sev_fg,
                outline=None, pad_x=4 if wide else 2, pad_y=1, radius=3,
            )
            tx += spw + (5 if wide else 3)

            ts_raw = str(item.get("timestamp", ""))
            ts_short = ts_raw[-8:-3] if len(ts_raw) >= 8 else ts_raw[:5]
            ts_bb = rj_theme.measure_text(f["badge"], ts_short, d)
            ts_w = ts_bb[2] - ts_bb[0]
            d.text((list_x1 - ts_w - (8 if wide else 3), ty + 1), ts_short, font=f["badge"], fill=ET["text_dim"])

            src = str(item.get("source", "system"))
            src_avail = max(24, (list_x1 - ts_w - 14) - tx)
            d.text((tx, ty), rj_theme.truncate_text(d, src, src_avail, f["body_bold"] if wide else f["badge"]), font=f["body_bold"] if wide else f["badge"], fill=ET["text_dim"] if read else ET["text_bright"])

            # Message line
            msg = str(item.get("message", ""))
            msg_y = ry0 + (24 if large else (16 if wide else 11))
            msg_x = list_x0 + (10 if wide else 4)
            d.text(
                (msg_x, msg_y),
                rj_theme.truncate_text(d, msg, max(30, list_x1 - msg_x - 8), f["sub"] if wide else f["badge"]),
                font=f["sub"] if wide else f["badge"],
                fill=ET["text_dim"] if read else ET["text_primary"],
            )

        if len(filtered_items) > vis:
            rj_theme.draw_scrollbar(d, list_x1 + 3, y0, y1, st, vis, len(filtered_items), width=sb_w)

        # Widescreen Detail Inspector
        if split and 0 <= cursor < len(filtered_items):
            cur = filtered_items[cursor]
            sev_lbl, sev_bg, sev_fg = _sev_style(cur.get("severity", "info"))
            read = _is_read(cur)
            px0 = list_x1 + 12
            px1 = width - pad
            pw = px1 - px0 - 20

            rj_theme.draw_card(
                d, (px0, y0, px1, y1),
                fill=ET["bg_surface"], outline=ET["border_subtle"],
                radius=7, accent_left=sev_fg, accent_width=3,
            )
            dy = y0 + 10
            p1w, _ = rj_theme.draw_pill(d, px0 + 10, dy, sev_lbl, f["badge"], bg=sev_bg, fg=sev_fg, outline=None, pad_x=5, pad_y=1, radius=3)
            rj_theme.draw_pill(
                d, px0 + 16 + p1w, dy,
                "READ" if read else "UNREAD", f["badge"],
                bg=ET["bg_well"] if read else ET["select_bg"],
                fg=ET["text_muted"] if read else ET["accent_bright"],
                outline=ET["border_subtle"] if read else ET["border_accent"],
                pad_x=5, pad_y=1, radius=3,
            )
            dy += 22
            d.text((px0 + 10, dy), rj_theme.truncate_text(d, str(cur.get("source", "system")), pw, f["title"]), font=f["title"], fill=ET["accent_bright"])
            dy += 20
            d.text((px0 + 10, dy), rj_theme.truncate_text(d, str(cur.get("timestamp", "unknown time")), pw, f["mono"]), font=f["sub"], fill=ET["text_muted"])
            dy += 18
            d.line([(px0 + 10, dy), (px1 - 10, dy)], fill=ET["border_subtle"], width=1)
            dy += 10

            for ln in rj_theme.wrap_text(d, str(cur.get("message", "")), pw, f["body"]):
                if dy + 16 > y1 - 30:
                    break
                d.text((px0 + 10, dy), ln, font=f["body"], fill=ET["text_primary"])
                dy += 17

            # Enabled channels summary at bottom of inspector
            chs = _load_channels()
            en = [k.upper() for k, v in chs.items() if v.get("enabled") and v.get("url")]
            ch_str = f"Push targets: {', '.join(en)}" if en else "Push: none enabled (L/W to setup)"
            d.line([(px0 + 10, y1 - 24), (px1 - 10, y1 - 24)], fill=ET["border_subtle"], width=1)
            d.text((px0 + 10, y1 - 18), rj_theme.truncate_text(d, ch_str, pw, f["sub"]), font=f["sub"], fill=ET["text_muted"])

    if toast:
        ty0 = height - ftr_h - toast_h
        ty1 = height - ftr_h - 1
        t_bg = ET["ok_bg"] if toast_kind == "ok" else (ET["danger_bg"] if toast_kind == "err" else ET["select_bg"])
        t_fg = ET["ok"] if toast_kind == "ok" else (ET["danger"] if toast_kind == "err" else ET["accent_bright"])
        d.rectangle((0, ty0, width - 1, ty1), fill=t_bg)
        d.line([(0, ty0), (width - 1, ty0)], fill=ET["border_subtle"], width=1)
        d.text((8 if wide else 3, ty0 + max(1, (toast_h - 12) // 2)), rj_theme.truncate_text(d, toast, width - 16, f["sub"]), font=f["sub"], fill=t_fg)

    return img


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _running, notifications, read_set

    GPIO.setmode(GPIO.BCM)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    LCD_Config.GPIO_Init()
    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    lcd.LCD_Clear()
    W, H = lcd.width, lcd.height
    fonts = rj_theme.load_app_fonts(W, H)
    split = W >= 460

    initial = _load_notifications()
    loot_initial = _scan_new_loot_files()
    with lock:
        notifications = _merge_notifications(initial, loot_initial)

    poller = threading.Thread(target=_poll_thread, daemon=True)
    poller.start()

    cursor = 0
    scroll = 0
    filter_idx = 0
    mode = "list"  # list | detail | confirm_clear | channel_config
    channel_idx = 0
    toast = ""
    toast_kind = "info"
    toast_until = 0.0
    last_count = -1
    dirty = True

    try:
        with text_input_mode():
            flush_input()
            while _running:
                now_m = time.monotonic()
                with lock:
                    snap = list(notifications)
                if len(snap) != last_count:
                    last_count = len(snap)
                    dirty = True

                flt = FILTERS[filter_idx]
                filtered = _filter_items(snap, flt)

                row_h = 46 if fonts["large"] else (30 if fonts["wide"] else 21)
                hdr_h = 30 if fonts["large"] else (24 if fonts["wide"] else 18)
                tab_h = (22 if fonts["large"] else 17) if fonts["wide"] else 0
                ftr_h = 22 if fonts["large"] else (18 if fonts["wide"] else 13)
                vis = max(1, (H - hdr_h - tab_h - ftr_h - 14) // row_h)

                if filtered:
                    cursor = max(0, min(cursor, len(filtered) - 1))
                    if cursor < scroll:
                        scroll = cursor
                    elif cursor >= scroll + vis:
                        scroll = cursor - vis + 1
                else:
                    cursor = 0
                    scroll = 0

                active_toast = toast if (toast and now_m < toast_until) else ""
                if toast and not active_toast:
                    toast = ""
                    dirty = True

                if dirty:
                    frame = render_notif_frame(
                        W, H, snap, filtered,
                        cursor=cursor, scroll=scroll,
                        active_filter=flt, mode=mode, channel_idx=channel_idx,
                        toast=active_toast, toast_kind=toast_kind, fonts=fonts,
                    )
                    lcd.LCD_ShowImage(frame, 0, 0)
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO)
                if act is None and ch is None:
                    time.sleep(0.04)
                    continue

                kl = ch.lower() if isinstance(ch, str) else ""

                if mode == "confirm_clear":
                    if act in ("OK", "ENTER"):
                        with lock:
                            notifications = []
                        read_set = set()
                        try:
                            with open(NOTIF_FILE, "w", encoding="utf-8"):
                                pass
                        except OSError:
                            pass
                        cursor = 0
                        scroll = 0
                        mode = "list"
                        toast, toast_kind, toast_until = "Cleared all notifications", "ok", time.monotonic() + 3.0
                    else:
                        mode = "list"
                        toast, toast_kind, toast_until = "Cancelled", "info", time.monotonic() + 2.0
                    dirty = True
                    continue

                if mode == "channel_config":
                    if act in ("KEY3", "ESCAPE", "BACKSPACE") or kl == "q":
                        mode = "list"
                        dirty = True
                    elif act in ("UP", "LEFT"):
                        channel_idx = (channel_idx - 1) % len(CHANNEL_NAMES)
                        dirty = True
                    elif act in ("DOWN", "RIGHT"):
                        channel_idx = (channel_idx + 1) % len(CHANNEL_NAMES)
                        dirty = True
                    elif act in ("OK", "ENTER"):
                        chs = _load_channels()
                        cname = CHANNEL_NAMES[channel_idx]
                        chs[cname]["enabled"] = not bool(chs[cname].get("enabled"))
                        _save_channels(chs)
                        dirty = True
                    elif act == "KEY2" or kl == "e":
                        chs = _load_channels()
                        cname = CHANNEL_NAMES[channel_idx]
                        new_url = lcd_keyboard(
                            lcd, fonts["body"], PINS, GPIO,
                            title=f"{CHANNEL_LABELS.get(cname, cname)} URL",
                            default=chs[cname].get("url", ""),
                            charset="url", max_len=200,
                        )
                        if new_url is not None:
                            chs[cname]["url"] = new_url.strip()
                            if new_url.strip():
                                chs[cname]["enabled"] = True
                            _save_channels(chs)
                        flush_input()
                        dirty = True
                    continue

                if mode == "detail":
                    if act in ("KEY3", "ESCAPE", "BACKSPACE", "LEFT") or kl == "q":
                        mode = "list"
                        dirty = True
                    elif act in ("OK", "ENTER") and filtered:
                        item = filtered[cursor]
                        k = _notif_key(item)
                        if _is_read(item):
                            read_set.discard(k)
                            read_set.discard(item.get("timestamp", ""))
                        else:
                            read_set.add(k)
                        dirty = True
                    continue

                # mode == "list"
                if act in ("KEY3", "ESCAPE", "BACKSPACE") or kl == "q":
                    break
                elif act == "UP" and filtered:
                    cursor = (cursor - 1) % len(filtered)
                    dirty = True
                elif act == "DOWN" and filtered:
                    cursor = (cursor + 1) % len(filtered)
                    dirty = True
                elif act == "PAGEUP" and filtered:
                    cursor = max(0, cursor - vis)
                    dirty = True
                elif act == "PAGEDOWN" and filtered:
                    cursor = min(len(filtered) - 1, cursor + vis)
                    dirty = True
                elif act == "HOME" and filtered:
                    cursor = 0
                    dirty = True
                elif act == "END" and filtered:
                    cursor = len(filtered) - 1
                    dirty = True
                elif act == "RIGHT" and filtered and not split:
                    mode = "detail"
                    dirty = True
                elif act in ("OK", "ENTER") and filtered:
                    item = filtered[cursor]
                    k = _notif_key(item)
                    if _is_read(item):
                        read_set.discard(k)
                        read_set.discard(item.get("timestamp", ""))
                        toast = "Marked unread"
                    else:
                        read_set.add(k)
                        toast = "Marked read"
                    toast_kind, toast_until = "info", time.monotonic() + 2.0
                    dirty = True
                elif kl == "m":
                    for item in snap:
                        read_set.add(_notif_key(item))
                    toast, toast_kind, toast_until = "All notifications marked read", "ok", time.monotonic() + 2.5
                    dirty = True
                elif act == "TAB" or kl == "f":
                    filter_idx = (filter_idx + 1) % len(FILTERS)
                    cursor = 0
                    scroll = 0
                    dirty = True
                elif act == "LEFT" or kl == "w":
                    mode = "channel_config"
                    dirty = True
                elif act == "KEY1" or kl == "c":
                    mode = "confirm_clear"
                    dirty = True
                elif act == "KEY2" or kl == "p":
                    unread = [n for n in snap if not _is_read(n)]
                    if unread:
                        sending = render_notif_frame(
                            W, H, snap, filtered, cursor=cursor, scroll=scroll,
                            active_filter=flt, toast="Pushing unread notifications...", toast_kind="info", fonts=fonts,
                        )
                        lcd.LCD_ShowImage(sending, 0, 0)
                        result = _push_all_channels(unread)
                        toast = result
                        toast_kind = "err" if ("err" in result.lower() or "no channel" in result.lower()) else "ok"
                        toast_until = time.monotonic() + 4.5
                    else:
                        toast, toast_kind, toast_until = "No unread notifications to push", "warn", time.monotonic() + 3.0
                    dirty = True
                elif kl == "r":
                    merged = _merge_notifications(_load_notifications(), _scan_new_loot_files())
                    with lock:
                        notifications = merged
                    toast, toast_kind, toast_until = f"Refreshed ({len(merged)} items)", "info", time.monotonic() + 2.5
                    dirty = True
    finally:
        _running = False
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
