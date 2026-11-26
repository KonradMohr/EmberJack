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
  OK / ENTER               -- Toggle read/unread (or open detail on compact screens)
  LEFT / RIGHT / TAB       -- Cycle filter (All / Unread / Critical / Warning)
  KEY1 / R                 -- Force refresh feed
  KEY2 / W                 -- Actions & Channels (Mark All Read / Push / Clear / Webhooks)
  M / P / C                -- Keyboard shortcuts: Mark All Read / Push Unread / Clear All
  KEY3 / ESC               -- Back / Exit
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
from payloads._input_helper import (
    flush_input,
    format_footer_hint,
    is_action,
    is_back,
    is_confirm,
    poll_ui_input,
    text_input_mode,
)
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
CHANNEL_MENU_ITEMS = [
    ("action", "mark_all", "Mark All Read", "Mark every notification as read"),
    ("action", "push_unread", "Push Unread", "Send unread alerts to enabled channels"),
    ("action", "clear_all", "Clear All Alerts", "Delete all logged notifications"),
    ("channel", "discord", "Discord Webhook", ""),
    ("channel", "slack", "Slack Webhook", ""),
    ("channel", "http_post", "HTTP POST", ""),
]


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


def _layout_metrics(width, height, f):
    """Return 4-tier layout geometry (640x360, 320x170, 240x240, 128x128)."""
    wide = bool(f["wide"])
    large = bool(f["large"])
    is_240 = (not wide) and width >= 200
    if large:
        return {"wide": True, "large": True, "is_240": False, "hdr_h": 30, "tab_h": 22, "ftr_h": 22, "row_h": 46, "pad": 10, "v_top": 7, "v_bot": 6, "sb_w": 5, "toast_h": 22}
    if wide:
        return {"wide": True, "large": False, "is_240": False, "hdr_h": 24, "tab_h": 16, "ftr_h": 18, "row_h": 26, "pad": 5, "v_top": 3, "v_bot": 3, "sb_w": 4, "toast_h": 15}
    if is_240:
        return {"wide": False, "large": False, "is_240": True, "hdr_h": 22, "tab_h": 16, "ftr_h": 16, "row_h": 32, "pad": 5, "v_top": 4, "v_bot": 4, "sb_w": 3, "toast_h": 16}
    return {"wide": False, "large": False, "is_240": False, "hdr_h": 18, "tab_h": 0, "ftr_h": 13, "row_h": 21, "pad": 3, "v_top": 2, "v_bot": 2, "sb_w": 2, "toast_h": 14}


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
    m = _layout_metrics(width, height, f)
    wide = m["wide"]
    large = m["large"]
    is_240 = m["is_240"]
    split = width >= 460

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    unread_cnt = sum(1 for n in all_items if not _is_read(n))
    hdr_h = rj_theme.draw_app_header(
        d, width, "NOTIFICATIONS" if (wide or is_240) else "NOTIFS", f,
        badge=f"{unread_cnt} NEW" if unread_cnt else "0 NEW",
        right_text=f"{len(all_items)} total",
        right_pill=FILTER_LABELS.get(active_filter, "ALL"),
        hdr_h=m["hdr_h"],
    )

    if mode == "confirm_clear":
        cfm_hint = format_footer_hint(
            width, height,
            hat_hint="OK:Yes  K3:Cancel",
            hat_240_hint="OK: Confirm Clear  K3: Cancel",
            cardputer_hint="Enter: Confirm Clear  Esc: Cancel",
            hdmi_hint="Enter: Confirm Clear  •  Esc: Cancel",
        )
        rj_theme.draw_app_footer(d, width, height, cfm_hint, f, ftr_h=m["ftr_h"])
        rj_theme.draw_status_card(
            d, width, height,
            "Clear All Notifications?",
            sub="Removes all logged alerts from .notifications.jsonl",
            kind="err", foot=cfm_hint, fonts=f,
        )
        return img

    if mode == "channel_config":
        channels = _load_channels()
        n_menu = len(CHANNEL_MENU_ITEMS)
        sel_m = channel_idx % n_menu
        sel_kind = CHANNEL_MENU_ITEMS[sel_m][0]
        if sel_kind == "channel":
            ch_hint = format_footer_hint(
                width, height,
                hat_hint="OK:Tog K2:URL K3:Bk",
                hat_240_hint="OK:Toggle  K2:Edit URL  K3:Back",
                cardputer_hint="Enter:Toggle  E:Edit URL  Esc:Back",
                hdmi_hint="Up/Dn:Select  Enter:Toggle ON/OFF  E:Edit URL  Esc:Back",
            )
        else:
            ch_hint = format_footer_hint(
                width, height,
                hat_hint="OK:Run  K3:Back",
                hat_240_hint="U/D:Select  OK:Run  K3:Back",
                cardputer_hint="U/D:Select  Enter:Run  Esc:Back",
                hdmi_hint="Up/Dn:Select  Enter:Run Action  Esc:Back",
            )
        ftr_h = rj_theme.draw_app_footer(d, width, height, ch_hint, f, right_text=f"{sel_m + 1}/{n_menu}", ftr_h=m["ftr_h"])
        pad = m["pad"]
        y0 = hdr_h + (8 if large else (4 if (wide or is_240) else 3))
        y1 = height - ftr_h - (8 if large else (4 if (wide or is_240) else 3))
        row_h = 44 if large else (29 if wide else (32 if is_240 else 24))
        vis_m = max(1, (y1 - y0) // row_h)
        st_m = max(0, min(sel_m - vis_m + 1, n_menu - vis_m)) if sel_m >= vis_m else 0
        sb_w = m["sb_w"]
        row_x1 = width - pad - (sb_w + 4 if n_menu > vis_m else 0)

        for slot in range(vis_m):
            idx = st_m + slot
            if idx >= n_menu:
                break
            kind_i, key_i, lbl, default_sub = CHANNEL_MENU_ITEMS[idx]
            is_sel = idx == sel_m
            ry0 = y0 + slot * row_h
            ry1 = min(y1, ry0 + row_h - (5 if large else (3 if (wide or is_240) else 2)))
            if ry1 <= ry0 + 6:
                break

            if kind_i == "channel":
                ch_cfg = channels.get(key_i, {})
                enabled = bool(ch_cfg.get("enabled"))
                url = ch_cfg.get("url", "")
                st_lbl = "ON" if (not large and enabled) else ("ENABLED" if enabled else "OFF")
                st_bg = ET["ok_bg"] if enabled else ET["danger_bg"]
                st_fg = ET["ok"] if enabled else ET["danger"]
                acc_col = ET["ok"] if enabled else (ET["accent"] if is_sel else None)
                sub_txt = url if url else ("Set URL (K2/E)" if not large else "Webhook URL not set (press E to configure)")
            else:
                st_lbl = "RUN"
                st_bg = ET["danger_bg"] if key_i == "clear_all" else ET["select_bg"]
                st_fg = ET["danger"] if key_i == "clear_all" else ET["accent_bright"]
                acc_col = ET["danger"] if key_i == "clear_all" else (ET["accent"] if is_sel else None)
                sub_txt = default_sub

            rj_theme.draw_card(
                d, (pad, ry0, row_x1, ry1),
                fill=ET["select_bg"] if is_sel else ET["bg_surface"],
                outline=ET["border_accent"] if is_sel else ET["border_subtle"],
                radius=6 if wide else (4 if is_240 else 3),
                accent_left=acc_col,
                accent_width=3 if wide else 2,
            )
            sbb = rj_theme.measure_text(f["badge"], st_lbl, d)
            spw = (sbb[2] - sbb[0]) + (10 if (wide or is_240) else 6)
            rj_theme.draw_pill(
                d, row_x1 - spw - (6 if (wide or is_240) else 3), ry0 + (6 if large else 3),
                st_lbl, f["badge"], bg=st_bg, fg=st_fg,
                outline=None, pad_x=5 if (wide or is_240) else 3, pad_y=1, radius=3,
            )
            lx = pad + (12 if wide else (8 if is_240 else 5))
            title_avail = max(24, row_x1 - spw - lx - 10)
            d.text((lx, ry0 + (6 if large else 3)), rj_theme.truncate_text(d, lbl, title_avail, f["body_bold"]), font=f["body_bold"], fill=ET["text_bright"] if is_sel else ET["text_primary"])
            sub_y = ry0 + (24 if large else (15 if (wide or is_240) else 12))
            if sub_y + 9 <= ry1 - 1:
                d.text(
                    (lx, sub_y),
                    rj_theme.truncate_text(d, sub_txt, max(24, row_x1 - lx - 8), f["sub"]),
                    font=f["sub"], fill=ET["text_muted"] if kind_i == "action" or (kind_i == "channel" and channels.get(key_i, {}).get("url")) else ET["text_dim"],
                )

        if n_menu > vis_m:
            rj_theme.draw_scrollbar(d, row_x1 + 3, y0, y1, st_m, vis_m, n_menu, width=sb_w)
        return img

    if not split and mode == "detail":
        hint = format_footer_hint(
            width, height,
            hat_hint="OK:Read/Unr  K3:Back",
            hat_240_hint="OK:Toggle Read  K3:Back",
            cardputer_hint="Enter:Toggle Read  Esc:Back",
            hdmi_hint="Enter:Toggle Read  •  Esc:Back",
        )
    else:
        hint = format_footer_hint(
            width, height,
            hat_hint="OK:Rd L/R:Flt K1:Rf K2:Act K3:X",
            hat_240_hint="OK:Open L/R:Flt K1:Ref K2:Act K3:X",
            cardputer_hint="Enter:Open L/R:Flt R:Ref M:All W:Act Esc:X",
            hdmi_hint="Enter:Read  •  L/R/Tab:Filter  •  R:Refresh  •  M:ReadAll  •  P:Push  •  C:Clear  •  W:Channels  •  Esc:Exit",
        )
    pos_txt = f"{min(cursor + 1, max(1, len(filtered_items)))}/{max(1, len(filtered_items))}"
    ftr_h = rj_theme.draw_app_footer(d, width, height, hint, f, right_text=pos_txt, ftr_h=m["ftr_h"])

    # Filter bar on wide & 240x240 screens
    tab_h = m["tab_h"]
    if tab_h:
        d.rectangle((0, hdr_h, width - 1, hdr_h + tab_h - 1), fill=ET["bg_surface"])
        d.line([(0, hdr_h + tab_h - 1), (width - 1, hdr_h + tab_h - 1)], fill=ET["border_subtle"], width=1)
        tx = 8 if wide else 4
        for fkey in FILTERS:
            cnt = len(_filter_items(all_items, fkey))
            lbl = f"{FILTER_LABELS[fkey]} ({cnt})" if wide else f"{FILTER_LABELS[fkey]}:{cnt}"
            active = fkey == active_filter
            pw, _ = rj_theme.draw_pill(
                d, tx, hdr_h + max(1, (tab_h - 13) // 2), lbl, f["badge"],
                bg=ET["select_bg"] if active else ET["bg_well"],
                fg=ET["accent_bright"] if active else ET["text_muted"],
                outline=ET["border_accent"] if active else ET["border_subtle"],
                pad_x=5 if wide else 3, pad_y=1, radius=3,
            )
            tx += pw + (6 if wide else 3)
            if tx > width - 24:
                break

    toast_h = m["toast_h"] if toast else 0
    y0 = hdr_h + tab_h + m["v_top"]
    y1 = height - ftr_h - toast_h - m["v_bot"]
    pad = m["pad"]

    if not filtered_items:
        empty_foot = format_footer_hint(
            width, height,
            hat_hint="L/R:Flt K1:Ref K2:Ch K3:X",
            hat_240_hint="L/R:Filter  K1:Ref  K2:Channels  K3:Exit",
            cardputer_hint="L/R:Filter  R:Ref  W:Channels  Esc:Exit",
            hdmi_hint="L/R/Tab: Filter  •  R: Refresh  •  W: Channels  •  Esc: Exit",
        )
        rj_theme.draw_status_card(
            d, width, height,
            "No Notifications",
            sub="Events from payloads & loot watcher appear here" if active_filter == "all" else f"No '{active_filter}' notifications",
            kind="info", foot=empty_foot, fonts=f,
        )
    elif not split and mode == "detail":
        item = filtered_items[max(0, min(cursor, len(filtered_items) - 1))]
        sev_lbl, sev_bg, sev_fg = _sev_style(item.get("severity", "info"))
        rj_theme.draw_card(d, (pad, y0, width - pad, y1), fill=ET["bg_surface"], outline=sev_fg, radius=5 if (wide or is_240) else 3, accent_left=sev_fg, accent_width=3)
        dy = y0 + (6 if (wide or is_240) else 4)
        pw, _ = rj_theme.draw_pill(d, pad + 8, dy, sev_lbl, f["badge"], bg=sev_bg, fg=sev_fg, outline=None, pad_x=4, pad_y=1, radius=3)
        d.text((pad + 12 + pw, dy + 1), rj_theme.truncate_text(d, str(item.get("source", "?")), width - pad * 2 - pw - 22, f["body_bold"]), font=f["body_bold"], fill=ET["text_bright"])
        dy += 17 if (wide or is_240) else 13
        d.text((pad + 8, dy), rj_theme.truncate_text(d, str(item.get("timestamp", "")), width - pad * 2 - 16, f["sub"]), font=f["sub"], fill=ET["text_muted"])
        dy += 14 if (wide or is_240) else 11
        line_h = 13 if wide else (14 if is_240 else 10)
        for ln in rj_theme.wrap_text(d, str(item.get("message", "")), width - pad * 2 - 16, f["body"]):
            if dy + line_h > y1 - 3:
                break
            d.text((pad + 8, dy), ln, font=f["body"], fill=ET["text_primary"])
            dy += line_h
    else:
        sb_w = m["sb_w"]
        list_x0 = pad
        list_x1 = int(width * 0.56) - 4 if split else (width - pad - sb_w - 3)
        row_h = m["row_h"]
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
            ry1 = min(y1, ry0 + row_h - (5 if large else (3 if (wide or is_240) else 2)))
            if ry1 <= ry0 + 6:
                break
            rj_theme.draw_card(
                d, (list_x0, ry0, list_x1, ry1),
                fill=ET["select_bg"] if is_sel else ET["bg_surface"],
                outline=ET["border_accent"] if is_sel else ET["border_subtle"],
                radius=6 if wide else (4 if is_240 else 3),
                accent_left=sev_fg if not read else (ET["accent_dim"] if is_sel else None),
                accent_width=3 if wide else 2,
            )

            # Top row: severity pill + source + timestamp
            tx = list_x0 + (10 if wide else (7 if is_240 else 4))
            ty = ry0 + (5 if large else (2 if wide else (3 if is_240 else 2)))
            if not read and (wide or is_240):
                d.ellipse((tx, ty + 3, tx + 4, ty + 7), fill=ET["accent_bright"])
                tx += 8

            spw, _ = rj_theme.draw_pill(
                d, tx, ty, sev_lbl, f["badge"],
                bg=ET["bg_well"] if read else sev_bg,
                fg=ET["text_dim"] if read else sev_fg,
                outline=None, pad_x=4 if (wide or is_240) else 2, pad_y=1, radius=3,
            )
            tx += spw + (5 if (wide or is_240) else 3)

            ts_raw = str(item.get("timestamp", ""))
            ts_short = ts_raw[-8:-3] if len(ts_raw) >= 8 else ts_raw[:5]
            ts_bb = rj_theme.measure_text(f["badge"], ts_short, d)
            ts_w = ts_bb[2] - ts_bb[0]
            d.text((list_x1 - ts_w - (8 if (wide or is_240) else 3), ty + 1), ts_short, font=f["badge"], fill=ET["text_dim"])

            src = str(item.get("source", "system"))
            src_avail = max(24, (list_x1 - ts_w - 14) - tx)
            d.text((tx, ty), rj_theme.truncate_text(d, src, src_avail, f["body_bold"] if (wide or is_240) else f["badge"]), font=f["body_bold"] if (wide or is_240) else f["badge"], fill=ET["text_dim"] if read else ET["text_bright"])

            # Message line
            msg = str(item.get("message", ""))
            msg_y = ry0 + (24 if large else (13 if wide else (16 if is_240 else 11)))
            msg_x = list_x0 + (10 if wide else (7 if is_240 else 4))
            if msg_y + 8 <= ry1:
                d.text(
                    (msg_x, msg_y),
                    rj_theme.truncate_text(d, msg, max(30, list_x1 - msg_x - 8), f["sub"] if (wide or is_240) else f["badge"]),
                    font=f["sub"] if (wide or is_240) else f["badge"],
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
            ch_str = f"Push targets: {', '.join(en)}" if en else "Push: none enabled (W to setup)"
            d.line([(px0 + 10, y1 - 24), (px1 - 10, y1 - 24)], fill=ET["border_subtle"], width=1)
            d.text((px0 + 10, y1 - 18), rj_theme.truncate_text(d, ch_str, pw, f["sub"]), font=f["sub"], fill=ET["text_muted"])

    if toast:
        ty0 = height - ftr_h - toast_h
        ty1 = height - ftr_h - 1
        t_bg = ET["ok_bg"] if toast_kind == "ok" else (ET["danger_bg"] if toast_kind == "err" else ET["select_bg"])
        t_fg = ET["ok"] if toast_kind == "ok" else (ET["danger"] if toast_kind == "err" else ET["accent_bright"])
        d.rectangle((0, ty0, width - 1, ty1), fill=t_bg)
        d.line([(0, ty0), (width - 1, ty0)], fill=ET["border_subtle"], width=1)
        d.text((8 if (wide or is_240) else 3, ty0 + max(1, (toast_h - 11) // 2)), rj_theme.truncate_text(d, toast, width - 16, f["sub"]), font=f["sub"], fill=t_fg)

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

                m = _layout_metrics(W, H, fonts)
                active_toast = toast if (toast and now_m < toast_until) else ""
                if toast and not active_toast:
                    toast = ""
                    dirty = True
                t_h = m["toast_h"] if active_toast else 0
                vis = max(1, (H - m["hdr_h"] - m["tab_h"] - m["ftr_h"] - t_h - m["v_top"] - m["v_bot"]) // m["row_h"])

                if filtered:
                    cursor = max(0, min(cursor, len(filtered) - 1))
                    if cursor < scroll:
                        scroll = cursor
                    elif cursor >= scroll + vis:
                        scroll = cursor - vis + 1
                else:
                    cursor = 0
                    scroll = 0

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

                def _do_push_unread():
                    nonlocal toast, toast_kind, toast_until
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

                if mode == "confirm_clear":
                    if is_confirm(act):
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
                    n_menu = len(CHANNEL_MENU_ITEMS)
                    sel_kind, sel_key, _, _ = CHANNEL_MENU_ITEMS[channel_idx % n_menu]
                    if is_back(act):
                        mode = "list"
                        dirty = True
                    elif act == "UP":
                        channel_idx = (channel_idx - 1) % n_menu
                        dirty = True
                    elif act == "DOWN":
                        channel_idx = (channel_idx + 1) % n_menu
                        dirty = True
                    elif is_confirm(act):
                        if sel_kind == "action":
                            if sel_key == "mark_all":
                                for item in snap:
                                    read_set.add(_notif_key(item))
                                mode = "list"
                                toast, toast_kind, toast_until = "All notifications marked read", "ok", time.monotonic() + 2.5
                            elif sel_key == "push_unread":
                                mode = "list"
                                _do_push_unread()
                            elif sel_key == "clear_all":
                                mode = "confirm_clear"
                        else:
                            chs = _load_channels()
                            chs[sel_key]["enabled"] = not bool(chs[sel_key].get("enabled"))
                            _save_channels(chs)
                        dirty = True
                    elif is_action(act, ch, "KEY2", "e") and sel_kind == "channel":
                        chs = _load_channels()
                        new_url = lcd_keyboard(
                            lcd, fonts["body"], PINS, GPIO,
                            title=f"{CHANNEL_LABELS.get(sel_key, sel_key)} URL",
                            default=chs[sel_key].get("url", ""),
                            charset="url", max_len=200,
                        )
                        if new_url is not None:
                            chs[sel_key]["url"] = new_url.strip()
                            if new_url.strip():
                                chs[sel_key]["enabled"] = True
                            _save_channels(chs)
                        flush_input()
                        dirty = True
                    continue

                if mode == "detail":
                    if is_back(act):
                        mode = "list"
                        dirty = True
                    elif is_confirm(act) and filtered:
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
                    continue

                # mode == "list"
                if is_back(act):
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
                elif act == "LEFT":
                    filter_idx = (filter_idx - 1) % len(FILTERS)
                    cursor = 0
                    scroll = 0
                    dirty = True
                elif act in ("RIGHT", "TAB"):
                    filter_idx = (filter_idx + 1) % len(FILTERS)
                    cursor = 0
                    scroll = 0
                    dirty = True
                elif is_confirm(act) and filtered:
                    item = filtered[cursor]
                    k = _notif_key(item)
                    if split:
                        if _is_read(item):
                            read_set.discard(k)
                            read_set.discard(item.get("timestamp", ""))
                            toast = "Marked unread"
                        else:
                            read_set.add(k)
                            toast = "Marked read"
                        toast_kind, toast_until = "info", time.monotonic() + 2.0
                    else:
                        read_set.add(k)
                        mode = "detail"
                    dirty = True
                elif is_action(act, ch, "KEY1", "r"):
                    merged = _merge_notifications(_load_notifications(), _scan_new_loot_files())
                    with lock:
                        notifications = merged
                    toast, toast_kind, toast_until = f"Refreshed ({len(merged)} items)", "info", time.monotonic() + 2.5
                    dirty = True
                elif is_action(act, ch, "KEY2", "w"):
                    mode = "channel_config"
                    dirty = True
                elif kl == "m":
                    for item in snap:
                        read_set.add(_notif_key(item))
                    toast, toast_kind, toast_until = "All notifications marked read", "ok", time.monotonic() + 2.5
                    dirty = True
                elif kl == "c":
                    mode = "confirm_clear"
                    dirty = True
                elif kl == "p":
                    _do_push_unread()
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
