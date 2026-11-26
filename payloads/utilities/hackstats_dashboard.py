#!/usr/bin/env python3
"""
EmberJack Payload -- HackStats Telemetry Dashboard
==================================================
Author: 7h30th3r0n3

Aggregates loot statistics from the EmberJack loot directory.
Displays multi-card telemetry for Handshakes, Nmap Scans, Credentials,
Wardriving captures, and Total Loot with progress bars, extension
breakdown, and a 24-hour activity timeline bar chart.

Controls:
  UP / DOWN / PgUp / PgDn  -- Scroll category & extension breakdown
  LEFT / RIGHT / TAB       -- Switch view tab (compact screens)
  KEY1 / R                 -- Refresh stats immediately
  KEY2 / E                 -- Export JSON summary to loot/Stats
  KEY3 / ESC               -- Exit
"""

import json
import os
import signal
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime

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
    poll_ui_input,
    text_input_mode,
)

ET = rj_theme.EMBER_THEME

PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}

LOOT_DIR = os.path.join(rj_env.get_base_dir(), "loot")
EXPORT_DIR = os.path.join(LOOT_DIR, "Stats")
COMPACT_TABS = ("overview", "breakdown", "activity")

_running = True
_lock = threading.Lock()
_stats_cache = None


def _cleanup(*_args):
    global _running
    _running = False


signal.signal(signal.SIGINT, _cleanup)
signal.signal(signal.SIGTERM, _cleanup)


# ---------------------------------------------------------------------------
# Stats collection
# ---------------------------------------------------------------------------

def _fmt_bytes(n: int) -> str:
    val = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024.0 or unit == "GB":
            return f"{int(val)} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024.0
    return "0 B"


def _walk_loot():
    """Walk loot directory and collect file counts, sizes, wardriving hits, and mtimes."""
    files_by_ext = defaultdict(int)
    timestamps = []
    total_files = 0
    total_bytes = 0
    wardriving_dir_files = 0

    if not os.path.isdir(LOOT_DIR):
        return dict(files_by_ext), timestamps, total_files, total_bytes, wardriving_dir_files

    for root, _dirs, filenames in os.walk(LOOT_DIR):
        rel_root = os.path.relpath(root, LOOT_DIR).lower()
        in_wardrive = any(tok in rel_root for tok in ("wardriv", "wigle", "kismet", "gps"))
        for fname in filenames:
            if fname.startswith("."):
                continue
            total_files += 1
            ext = os.path.splitext(fname)[1].lower()
            if ext:
                files_by_ext[ext] += 1
            if in_wardrive:
                wardriving_dir_files += 1
            fpath = os.path.join(root, fname)
            try:
                st = os.stat(fpath)
                timestamps.append(st.st_mtime)
                total_bytes += st.st_size
            except OSError:
                pass

    return dict(files_by_ext), timestamps, total_files, total_bytes, wardriving_dir_files


def _count_by_ext(files_by_ext, extensions):
    return sum(files_by_ext.get(e, 0) for e in extensions)


def _build_hourly_histogram(timestamps):
    """Build 24-element list of activity counts per hour (last 24h)."""
    now = time.time()
    cutoff = now - 86400
    histogram = [0] * 24
    for ts in timestamps:
        if ts >= cutoff:
            hour = int((ts - cutoff) / 3600)
            if 0 <= hour < 24:
                histogram[hour] += 1
    return histogram


def _gather_stats():
    """Gather all loot telemetry from the EmberJack loot directory."""
    files_by_ext, timestamps, total_files, total_bytes, wardrive_dir = _walk_loot()
    handshakes = _count_by_ext(files_by_ext, [".cap", ".pcap", ".pcapng", ".hccapx", ".hc22000"])
    credentials = _count_by_ext(files_by_ext, [".txt", ".creds", ".hash", ".potfile"])
    scans = _count_by_ext(files_by_ext, [".xml", ".nmap", ".gnmap"])
    wardrive_ext = _count_by_ext(files_by_ext, [".wiglecsv", ".kml", ".gpx", ".kismet", ".netxml"])
    hosts = _count_by_ext(files_by_ext, [".host", ".csv"])
    wardriving = max(wardrive_ext, wardrive_dir)
    histogram = _build_hourly_histogram(timestamps)

    categories = [
        ("Total Loot", total_files),
        ("Handshakes", handshakes),
        ("Nmap Scans", scans),
        ("Credentials", credentials),
        ("Wardriving", wardriving),
        ("Host Lists", hosts),
    ]

    top_exts = sorted(files_by_ext.items(), key=lambda kv: kv[1], reverse=True)[:10]
    ext_rows = [(ext, cnt) for ext, cnt in top_exts]

    return {
        "total_files": total_files,
        "total_bytes": total_bytes,
        "handshakes": handshakes,
        "scans": scans,
        "credentials": credentials,
        "wardriving": wardriving,
        "hosts": hosts,
        "categories": categories,
        "ext_rows": ext_rows,
        "histogram": histogram,
        "updated_at": datetime.now().strftime("%H:%M:%S"),
    }


def _export_summary(stats: dict):
    """Write a JSON summary to EXPORT_DIR."""
    os.makedirs(EXPORT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(EXPORT_DIR, f"stats_{ts}.json")
    data = {
        "timestamp": ts,
        "total_files": stats["total_files"],
        "total_bytes": stats["total_bytes"],
        "categories": {k: str(v) for k, v in stats["categories"]},
        "extensions": {k: v for k, v in stats["ext_rows"]},
        "hourly_activity_24h": stats["histogram"],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return os.path.basename(path)


# ---------------------------------------------------------------------------
# Background refresh
# ---------------------------------------------------------------------------

def _refresh_worker():
    global _stats_cache
    while _running:
        result = _gather_stats()
        with _lock:
            _stats_cache = result
        for _ in range(100):
            if not _running:
                return
            time.sleep(0.1)


# ---------------------------------------------------------------------------
# Native Resolution-Adaptive Obsidian Ember Dashboard Renderer
# ---------------------------------------------------------------------------

def _draw_histogram_panel(d, box, histogram, f, wide, large):
    """Draw a sleek 24-hour Obsidian Ember activity bar chart inside *box*."""
    x0, y0, x1, y1 = box
    rj_theme.draw_card(d, box, fill=ET["bg_surface"], outline=ET["border_subtle"], radius=7 if wide else 4)

    pad = 10 if large else (6 if wide else 4)
    total_24h = sum(histogram) if histogram else 0
    peak = max(histogram) if histogram else 0

    d.text((x0 + pad, y0 + (8 if large else 4)), "24H LOOT ACTIVITY", font=f["badge"], fill=ET["accent_bright"])
    stat_s = f"{total_24h} events (peak {peak}/h)" if wide else f"{total_24h} ev"
    sbb = rj_theme.measure_text(f["badge"], stat_s, d)
    d.text((x1 - pad - (sbb[2] - sbb[0]), y0 + (8 if large else 4)), stat_s, font=f["badge"], fill=ET["text_muted"])

    chart_x0 = x0 + pad
    chart_x1 = x1 - pad
    chart_y0 = y0 + (28 if large else (20 if wide else 16))
    chart_y1 = y1 - (20 if large else (15 if wide else 10))
    if chart_y1 <= chart_y0 + 8 or chart_x1 <= chart_x0 + 24:
        return

    # Subtle horizontal reference lines
    for frac in (0.0, 0.5, 1.0):
        gy = int(chart_y0 + (chart_y1 - chart_y0) * frac)
        d.line([(chart_x0, gy), (chart_x1, gy)], fill=ET["border_subtle"], width=1)

    n = len(histogram) if histogram else 24
    max_val = max(peak, 1)
    avail_w = chart_x1 - chart_x0
    gap = 3 if large else (2 if wide else 1)
    bar_w = max(2, (avail_w - gap * (n - 1)) // n)
    used_w = bar_w * n + gap * (n - 1)
    start_x = chart_x0 + max(0, (avail_w - used_w) // 2)
    ch_h = chart_y1 - chart_y0

    for i, count in enumerate(histogram or [0] * 24):
        bx0 = start_x + i * (bar_w + gap)
        bx1 = bx0 + bar_w
        if count > 0:
            bh = max(3, int(round((count / max_val) * ch_h)))
            col = ET["accent_bright"] if count >= peak else (ET["accent"] if i >= 18 else ET["accent_dim"])
            d.rectangle((bx0, chart_y1 - bh, bx1, chart_y1), fill=col)
        else:
            d.rectangle((bx0, chart_y1 - 1, bx1, chart_y1), fill=ET["bg_elevated"])

    # Axis ticks
    tick_y = chart_y1 + (4 if large else 2)
    d.text((chart_x0, tick_y), "-24h", font=f["badge"], fill=ET["text_dim"])
    mid_bb = rj_theme.measure_text(f["badge"], "-12h", d)
    d.text(((chart_x0 + chart_x1) // 2 - (mid_bb[2] - mid_bb[0]) // 2, tick_y), "-12h", font=f["badge"], fill=ET["text_dim"])
    now_bb = rj_theme.measure_text(f["badge"], "Now", d)
    d.text((chart_x1 - (now_bb[2] - now_bb[0]), tick_y), "Now", font=f["badge"], fill=ET["accent_bright"])


def render_dashboard_frame(
    width,
    height,
    stats,
    scroll_pos=0,
    compact_tab=0,
    toast="",
    toast_kind="info",
    fonts=None,
):
    """Pure PIL native resolution-adaptive Obsidian Ember HackStats renderer."""
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = f["wide"]
    large = f["large"]
    is_240 = (not wide) and width >= 200
    multi_card = width >= 480

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    total_files = stats["total_files"]
    size_str = _fmt_bytes(stats["total_bytes"])
    hdr_h = rj_theme.draw_app_header(
        d, width, "HACKSTATS" if not wide else "EMBERJACK HACKSTATS", f,
        badge=f"{total_files} LOOT",
        right_text=f"{size_str}  •  {stats['updated_at']}",
        hdr_h=30 if large else (24 if wide else (20 if is_240 else 18)),
    )

    hint = format_footer_hint(
        width, height,
        hat_hint="L/R:Tab K1:Ref K2:Exp K3:X",
        hat_240_hint="L/R:View U/D:Scr K1:Ref K2:Exp K3:Exit",
        cardputer_hint="L/R/Tab:View  U/D:Scr  R:Ref  E:Exp  Esc:Exit",
        hdmi_hint="Up/Down:Scroll Breakdown  •  R:Refresh  •  E:Export JSON  •  Esc:Exit",
    )
    ftr_h = rj_theme.draw_app_footer(
        d, width, height, hint, f,
        right_text=size_str if wide else "",
        ftr_h=22 if large else (18 if wide else (15 if is_240 else 13)),
    )

    toast_h = (22 if large else (16 if (wide or is_240) else 14)) if toast else 0
    y0 = hdr_h + (8 if large else (4 if (wide or is_240) else 2))
    y1 = height - ftr_h - toast_h - (6 if large else (3 if (wide or is_240) else 2))
    pad = 10 if large else (6 if wide else (5 if is_240 else 3))

    denom = max(1, total_files)
    kpis = [
        ("HANDSHAKES", stats["handshakes"], ET["accent_bright"], stats["handshakes"] / denom),
        ("NMAP SCANS", stats["scans"], ET["info"], stats["scans"] / denom),
        ("CREDENTIALS", stats["credentials"], ET["ok"], stats["credentials"] / denom),
        ("WARDRIVING", stats["wardriving"], ET["warn"], stats["wardriving"] / denom),
        ("TOTAL LOOT", total_files, ET["accent"], 1.0 if total_files > 0 else 0.0),
    ]

    if multi_card:
        # Top KPI multi-card row (5 cards across widescreen)
        kpi_h = 80 if large else 62
        gap = 8
        card_w = (width - pad * 2 - gap * (len(kpis) - 1)) // len(kpis)
        val_font = rj_env.load_font(22 if large else 16, bold=True, mono=True)

        for idx, (label, val, col, frac) in enumerate(kpis):
            cx0 = pad + idx * (card_w + gap)
            cx1 = (width - pad) if idx == len(kpis) - 1 else (cx0 + card_w)
            rj_theme.draw_card(
                d, (cx0, y0, cx1, y0 + kpi_h),
                fill=ET["bg_surface"], outline=ET["border_subtle"],
                radius=7, accent_left=col, accent_width=3,
            )
            d.text((cx0 + 10, y0 + 8), rj_theme.truncate_text(d, label, cx1 - cx0 - 14, f["badge"]), font=f["badge"], fill=ET["text_muted"])
            d.text((cx0 + 10, y0 + 25), str(val), font=val_font, fill=ET["text_bright"])
            bar_y0 = y0 + kpi_h - 14
            rj_theme.draw_progress_bar(
                d, (cx0 + 10, bar_y0, cx1 - 10, bar_y0 + 6),
                fraction=frac, fill=col, track=ET["bg_well"], outline=None, radius=3,
            )

        # Bottom Split: Left Breakdown with Progress Bars + Right 24h Histogram
        bot_y0 = y0 + kpi_h + 10
        left_x1 = int(width * 0.45)
        rj_theme.draw_card(d, (pad, bot_y0, left_x1, y1), fill=ET["bg_surface"], outline=ET["border_subtle"], radius=7)
        d.text((pad + 10, bot_y0 + 8), "LOOT BREAKDOWN", font=f["badge"], fill=ET["accent_bright"])

        rows = [("Host Files", stats["hosts"])] + list(stats["ext_rows"])
        if not rows:
            rows = [("No files in loot/", 0)]
        list_y0 = bot_y0 + 26
        list_y1 = y1 - 8
        row_h = 22
        vis = max(1, (list_y1 - list_y0) // row_h)
        st = max(0, min(scroll_pos, max(0, len(rows) - vis)))
        max_row = max([c for _, c in rows] + [1])

        for slot in range(vis):
            idx = st + slot
            if idx >= len(rows):
                break
            rlbl, rcnt = rows[idx]
            ry = list_y0 + slot * row_h
            d.text((pad + 10, ry + 2), rj_theme.truncate_text(d, rlbl, 86, f["sub"]), font=f["sub"], fill=ET["text_primary"])
            cnt_s = str(rcnt)
            cbb = rj_theme.measure_text(f["badge"], cnt_s, d)
            cw = cbb[2] - cbb[0]
            d.text((left_x1 - 14 - cw, ry + 3), cnt_s, font=f["badge"], fill=ET["accent_bright"])
            bar_x0 = pad + 98
            bar_x1 = left_x1 - 20 - cw
            if bar_x1 > bar_x0 + 16:
                rj_theme.draw_progress_bar(
                    d, (bar_x0, ry + 6, bar_x1, ry + 13),
                    fraction=rcnt / max_row, fill=ET["accent"], track=ET["bg_well"], outline=None, radius=3,
                )
        if len(rows) > vis:
            rj_theme.draw_scrollbar(d, left_x1 - 7, list_y0, list_y1, st, vis, len(rows), width=3)

        _draw_histogram_panel(d, (left_x1 + 10, bot_y0, width - pad, y1), stats["histogram"], f, wide, large)

    else:
        # Compact / 240x240 / Cardputer tabbed dashboard (Overview / Breakdown / 24h Chart)
        tab_key = COMPACT_TABS[compact_tab % len(COMPACT_TABS)]
        tab_h = 16 if (wide or is_240) else 13
        tx = pad
        for idx, tk in enumerate(COMPACT_TABS):
            lbl = tk.upper()[:5] if not (wide or is_240) else tk.upper()
            active = idx == (compact_tab % len(COMPACT_TABS))
            pw, _ = rj_theme.draw_pill(
                d, tx, y0, lbl, f["badge"],
                bg=ET["select_bg"] if active else ET["bg_well"],
                fg=ET["accent_bright"] if active else ET["text_muted"],
                outline=ET["border_accent"] if active else ET["border_subtle"],
                pad_x=4 if (wide or is_240) else 2, pad_y=1, radius=3,
            )
            tx += pw + 4
        cy0 = y0 + tab_h + 3

        if tab_key == "overview":
            row_h = max(12, (y1 - cy0) // len(kpis))
            for idx, (label, val, col, frac) in enumerate(kpis):
                ry0 = cy0 + idx * row_h
                ry1 = min(y1, ry0 + row_h - 2)
                if ry1 <= ry0 + 6:
                    break
                rj_theme.draw_card(
                    d, (pad, ry0, width - pad, ry1),
                    fill=ET["bg_surface"], outline=ET["border_subtle"],
                    radius=4 if is_240 else 3, accent_left=col, accent_width=2,
                )
                d.text((pad + 6, ry0 + (4 if is_240 else 2)), label if (wide or is_240) else label[:8], font=f["badge"], fill=ET["text_primary"])
                val_s = str(val)
                vbb = rj_theme.measure_text(f["badge"], val_s, d)
                vw = vbb[2] - vbb[0]
                d.text((width - pad - 5 - vw, ry0 + (4 if is_240 else 2)), val_s, font=f["badge"], fill=col)
                if wide:
                    bx0 = pad + 88
                    bx1 = width - pad - 12 - vw
                    if bx1 > bx0 + 20 and (ry1 - ry0) >= 13:
                        rj_theme.draw_progress_bar(d, (bx0, ry0 + 4, bx1, min(ry1 - 2, ry0 + 10)), fraction=frac, fill=col, track=ET["bg_well"], outline=None, radius=2)
                elif ry1 - ry0 >= 15:
                    rj_theme.draw_progress_bar(d, (pad + 6, ry1 - (6 if is_240 else 4), width - pad - 6, ry1 - 2), fraction=frac, fill=col, track=ET["bg_well"], outline=None, radius=2 if is_240 else 1)

        elif tab_key == "breakdown":
            rows = list(stats["categories"]) + list(stats["ext_rows"])
            row_h = 16 if wide else (18 if is_240 else 13)
            vis = max(1, (y1 - cy0) // row_h)
            st = max(0, min(scroll_pos, max(0, len(rows) - vis)))
            max_row = max([c for _, c in rows] + [1])
            for slot in range(vis):
                idx = st + slot
                if idx >= len(rows):
                    break
                rlbl, rcnt = rows[idx]
                ry = cy0 + slot * row_h
                if ry + 10 > y1:
                    break
                d.text((pad + 2, ry + 1), rj_theme.truncate_text(d, rlbl, 76 if (wide or is_240) else 56, f["sub"]), font=f["sub"], fill=ET["text_primary"])
                cs = str(rcnt)
                cbb = rj_theme.measure_text(f["badge"], cs, d)
                cw = cbb[2] - cbb[0]
                d.text((width - pad - 6 - cw, ry + 1), cs, font=f["badge"], fill=ET["accent_bright"])
                bx0 = pad + (84 if (wide or is_240) else 60)
                bx1 = width - pad - 12 - cw
                if bx1 > bx0 + 14:
                    rj_theme.draw_progress_bar(d, (bx0, ry + 4, bx1, ry + 9), fraction=rcnt / max_row, fill=ET["accent"], track=ET["bg_well"], outline=None, radius=2)
            if len(rows) > vis:
                rj_theme.draw_scrollbar(d, width - pad - 2, cy0, y1, st, vis, len(rows), width=2)

        else:
            _draw_histogram_panel(d, (pad, cy0, width - pad, y1), stats["histogram"], f, wide or is_240, large)

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
# Main loop
# ---------------------------------------------------------------------------

def main():
    global _running, _stats_cache

    GPIO.setmode(GPIO.BCM)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    LCD_Config.GPIO_Init()
    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    lcd.LCD_Clear()
    W, H = lcd.width, lcd.height
    fonts = rj_theme.load_app_fonts(W, H)

    initial = _gather_stats()
    with _lock:
        _stats_cache = initial

    worker = threading.Thread(target=_refresh_worker, daemon=True)
    worker.start()

    scroll_pos = 0
    compact_tab = 0
    toast = ""
    toast_kind = "info"
    toast_until = 0.0
    last_updated = ""
    dirty = True

    try:
        with text_input_mode():
            flush_input()
            while _running:
                now_m = time.monotonic()
                with _lock:
                    snap = dict(_stats_cache) if _stats_cache else initial

                if snap["updated_at"] != last_updated:
                    last_updated = snap["updated_at"]
                    dirty = True

                active_toast = toast if (toast and now_m < toast_until) else ""
                if toast and not active_toast:
                    toast = ""
                    dirty = True

                if dirty:
                    frame = render_dashboard_frame(
                        W, H, snap,
                        scroll_pos=scroll_pos, compact_tab=compact_tab,
                        toast=active_toast, toast_kind=toast_kind, fonts=fonts,
                    )
                    lcd.LCD_ShowImage(frame, 0, 0)
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO)
                if act is None and ch is None:
                    time.sleep(0.05)
                    continue

                max_items = len(snap["categories"]) + len(snap["ext_rows"])

                if is_back(act):
                    _cleanup()
                    break
                elif act == "UP":
                    scroll_pos = max(0, scroll_pos - 1)
                    dirty = True
                elif act == "DOWN":
                    scroll_pos = min(max(0, max_items - 3), scroll_pos + 1)
                    dirty = True
                elif act == "PAGEUP":
                    scroll_pos = max(0, scroll_pos - 4)
                    dirty = True
                elif act == "PAGEDOWN":
                    scroll_pos = min(max(0, max_items - 3), scroll_pos + 4)
                    dirty = True
                elif act == "LEFT":
                    compact_tab = (compact_tab - 1) % len(COMPACT_TABS)
                    scroll_pos = 0
                    dirty = True
                elif act in ("RIGHT", "TAB"):
                    compact_tab = (compact_tab + 1) % len(COMPACT_TABS)
                    scroll_pos = 0
                    dirty = True
                elif is_action(act, ch, "KEY1", "r"):
                    refreshed = _gather_stats()
                    with _lock:
                        _stats_cache = refreshed
                    scroll_pos = 0
                    toast = f"Stats updated ({refreshed['updated_at']})"
                    toast_kind = "info"
                    toast_until = time.monotonic() + 2.5
                    dirty = True
                elif is_action(act, ch, "KEY2", "e"):
                    try:
                        fname = _export_summary(snap)
                        toast = f"Exported {fname}"
                        toast_kind = "ok"
                    except OSError as exc:
                        toast = f"Export failed: {exc}"
                        toast_kind = "err"
                    toast_until = time.monotonic() + 3.5
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
