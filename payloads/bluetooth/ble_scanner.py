#!/usr/bin/env python3
"""
RaspyJack Payload -- Continuous BLE Scanner Dashboard
=====================================================
Author: 7h30th3r0n3

Scans for BLE devices using hcitool lescan and tracks addresses,
names, RSSI, first/last seen timestamps and seen count.  Provides
a scrollable device list with detail view.

Setup / Prerequisites
---------------------
- Bluetooth adapter (hci0)
- hcitool / hciconfig (bluez package)
- Optional: bluetoothctl for service enumeration

Controls
--------
  OK         -- Start / stop scanning
  UP / DOWN  -- Scroll device list
  RIGHT      -- Show device details (services)
  KEY1       -- Toggle sort (RSSI / name / count)
  KEY2       -- Export JSON to loot
  KEY3       -- Exit

Loot: /root/Raspyjack/loot/BLEScan/
"""

import os
import sys
import time
import json
import re
import asyncio
import threading
import subprocess
from datetime import datetime

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import LCD_Config
import rj_theme
from PIL import Image, ImageDraw, ImageFont
from payloads._display_helper import ScaledDraw, scaled_font
from payloads._input_helper import get_button
from payloads._iface_helper import select_bt_interface

_T = rj_theme.EMBER_THEME

# ── Pin / LCD setup ──────────────────────────────────────────────────────────
PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}
GPIO.setmode(GPIO.BCM)
for pin in PINS.values():
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

LCD = LCD_1in44.LCD()
LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
WIDTH, HEIGHT = LCD.width, LCD.height
font = scaled_font()

# ── Constants ────────────────────────────────────────────────────────────────
HCI_DEV = None  # set in main() via select_bt_interface
LOOT_DIR = "/root/Raspyjack/loot/BLEScan"
ROWS_VISIBLE = 7
ROW_H = 12
SORT_MODES = ["rssi", "name", "count"]
VIEWS = ["list", "detail"]

# ── Shared state ─────────────────────────────────────────────────────────────
lock = threading.Lock()
devices = {}          # addr -> {addr, name, rssi, first_seen, last_seen, count}
scanning = False
scroll_pos = 0
selected_idx = 0
sort_idx = 0
view = "list"
detail_lines = []
detail_scroll = 0
status_msg = "Idle"
_running = True


# ── HCI helpers ──────────────────────────────────────────────────────────────

def _hci_up():
    subprocess.run(["sudo", "hciconfig", HCI_DEV, "up"],
                   capture_output=True, timeout=5)


def _hci_reset():
    subprocess.run(["sudo", "hciconfig", HCI_DEV, "reset"],
                   capture_output=True, timeout=5)


# ── Bleak BLE scan ──────────────────────────────────────────────────────────

async def _bleak_scan(timeout=5):
    """Discover BLE devices using bleak (async)."""
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=timeout)
    return [
        (d.address, d.name or "", getattr(d, "rssi", -99))
        for d in found
    ]


# ── Scan thread ──────────────────────────────────────────────────────────────

def _scan_loop():
    """Run bleak BLE scan in a loop, update device list."""
    global scan_proc, status_msg

    _hci_up()
    time.sleep(0.3)

    while True:
        with lock:
            if not scanning:
                break

        try:
            results = asyncio.run(_bleak_scan(5))
            now_str = datetime.now().strftime("%H:%M:%S")

            for addr, name, rssi in results:
                with lock:
                    if not scanning:
                        break
                addr = addr.upper()
                with lock:
                    if addr in devices:
                        prev = devices[addr]
                        devices[addr] = {
                            **prev,
                            "name": name or prev["name"],
                            "rssi": rssi,
                            "last_seen": now_str,
                            "count": prev["count"] + 1,
                        }
                    else:
                        devices[addr] = {
                            "addr": addr,
                            "name": name,
                            "rssi": rssi,
                            "first_seen": now_str,
                            "last_seen": now_str,
                            "count": 1,
                        }

        except Exception as exc:
            with lock:
                status_msg = str(exc)[:20]
            time.sleep(1)


# ── Service enumeration ─────────────────────────────────────────────────────

def _enumerate_services(addr):
    """Use gatttool to get primary services for a BLE device."""
    lines = [f"Device: {addr}"]
    try:
        # Connect and discover services
        proc = subprocess.run(
            ["sudo", "gatttool", "-b", addr, "--primary"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            for svc_line in proc.stdout.strip().split("\n"):
                lines.append(svc_line.strip()[:80])
        else:
            lines.append("No services found")
            if proc.stderr.strip():
                lines.append(proc.stderr.strip()[:80])
    except subprocess.TimeoutExpired:
        lines.append("Connection timeout")
    except Exception as exc:
        lines.append(f"Error: {str(exc)[:60]}")
    return lines


# ── Start / stop ─────────────────────────────────────────────────────────────

def _start_scan():
    global scanning, status_msg
    with lock:
        if scanning:
            return
        scanning = True
        status_msg = "Scanning..."
    threading.Thread(target=_scan_loop, daemon=True).start()


def _stop_scan():
    global scanning, status_msg
    with lock:
        scanning = False
        status_msg = "Stopped"
    time.sleep(0.3)
    _hci_reset()


# ── Sorted device list ──────────────────────────────────────────────────────

def _sorted_devices():
    with lock:
        items = list(devices.values())
        mode = SORT_MODES[sort_idx]
    if mode == "rssi":
        items.sort(key=lambda d: d["rssi"], reverse=True)
    elif mode == "name":
        items.sort(key=lambda d: d["name"].lower() if d["name"] else "zzz")
    elif mode == "count":
        items.sort(key=lambda d: d["count"], reverse=True)
    return items


# ── Export ───────────────────────────────────────────────────────────────────

def _export_json():
    os.makedirs(LOOT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(LOOT_DIR, f"ble_scan_{ts}.json")
    with lock:
        data = {"timestamp": ts, "devices": list(devices.values())}
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
    return path


# ── Drawing (native resolution-adaptive Obsidian Ember) ──────────────────────

_FONTS = rj_theme.load_app_fonts(WIDTH, HEIGHT)


def _rssi_pct(rssi):
    try:
        return max(0, min(100, int((int(rssi) + 100) * 100 / 60)))
    except Exception:
        return 0


def _rssi_col(rssi):
    try:
        val = int(rssi)
    except Exception:
        return _T["danger"]
    if val >= -60:
        return _T["ok"]
    if val >= -78:
        return _T["warn"]
    return _T["danger"]


def _draw_screen():
    wide, large = _FONTS["wide"], _FONTS["large"]
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ImageDraw.Draw(img)

    with lock:
        active = scanning
        msg = status_msg
        n_dev = len(devices)
        sp = scroll_pos
        sel = selected_idx
        srt = SORT_MODES[sort_idx]
        cur_view = view
        d_lines = list(detail_lines)
        d_scroll = detail_scroll

    title = "BLE SCANNER" if cur_view == "list" else "BLE GATT DETAIL"
    hdr_h = rj_theme.draw_app_header(
        d, WIDTH, title, _FONTS,
        badge="SCAN" if active else "IDLE",
        right_text=msg if wide else "",
        right_pill=f"{n_dev} DEVS | {srt.upper()}" if wide else "",
    )

    pad = 8 if large else (4 if wide else 2)

    if cur_view == "detail":
        ftr_h = rj_theme.draw_app_footer(
            d, WIDTH, HEIGHT,
            "Left / K3:Back   Up/Dn:Scroll GATT Services" if wide else "LEFT:Back U/D:Scrl",
            _FONTS,
        )
        y0, y1 = hdr_h + pad, HEIGHT - ftr_h - pad
        rj_theme.draw_card(d, (pad, y0, WIDTH - pad, y1), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=6 if wide else 3, accent_left=_T["accent"], accent_width=3 if wide else 2)
        row_h = 22 if large else (16 if wide else 12)
        vis = max(1, (y1 - y0 - 8) // row_h)
        start = max(0, min(d_scroll, max(0, len(d_lines) - vis)))
        y = y0 + 5
        for i in range(start, min(len(d_lines), start + vis)):
            col = _T["accent_bright"] if i == 0 else _T["text_primary"]
            d.text((pad + (10 if wide else 4), y), rj_theme.truncate_text(d, d_lines[i], WIDTH - pad * 2 - 18, _FONTS["mono"] if wide else _FONTS["sub"]), font=_FONTS["mono"] if wide else _FONTS["sub"], fill=col)
            y += row_h
        if len(d_lines) > vis:
            rj_theme.draw_scrollbar(d, WIDTH - 5, y0 + 4, y1 - 4, start, vis, len(d_lines), width=3)
    else:
        lbl = "OK:Stop" if active else "OK:Scan"
        ftr_h = rj_theme.draw_app_footer(
            d, WIDTH, HEIGHT,
            f"{lbl}   Right:GATT   K1:Sort({srt})   K2:Export   K3:Exit" if wide else f"{lbl} K1:Srt K3:X",
            _FONTS,
            right_text=HCI_DEV or "" if large else "",
        )
        y0, y1 = hdr_h + pad, HEIGHT - ftr_h - pad

        if not wide:
            d.text((pad + 2, y0), rj_theme.truncate_text(d, f"Devs:{n_dev} Sort:{srt} {msg}", WIDTH - pad * 2 - 4, _FONTS["sub"]), font=_FONTS["sub"], fill=_T["text_muted"])
            y0 += 13

        devs = _sorted_devices()
        if not devs:
            rj_theme.draw_status_card(
                d, WIDTH, HEIGHT,
                "Scanning BLE..." if active else "BLE Scanner Ready",
                sub=f"Adapter: {HCI_DEV or 'hci0'}  |  Press OK to {'stop' if active else 'start'}",
                kind="warn" if active else "info",
                fonts=_FONTS,
            )
            LCD.LCD_ShowImage(img, 0, 0)
            return

        split = WIDTH >= 480
        lx1 = int(WIDTH * 0.63) - 4 if split else WIDTH - pad - 5
        row_h = 28 if large else (21 if wide else (18 if HEIGHT >= 200 else 12))
        vis = max(1, (y1 - y0) // row_h)
        cur_sel = max(0, min(sel, len(devs) - 1))
        start = max(0, min(cur_sel - vis // 2, max(0, len(devs) - vis)))

        for idx_r, i in enumerate(range(start, min(len(devs), start + vis))):
            dev = devs[i]
            is_cur = i == cur_sel
            y = y0 + idx_r * row_h
            if is_cur:
                rj_theme.draw_card(d, (pad, y, lx1, y + row_h - 2), fill=_T["select_bg"], outline=_T["border_accent"], radius=4 if wide else 2, accent_left=_T["accent"], accent_width=3 if wide else 2)
            elif idx_r % 2 == 1 and wide:
                d.rectangle((pad, y, lx1, y + row_h - 2), fill=_T["bg_surface"])
            ty = y + max(1, (row_h - (14 if large else 9)) // 2)
            rx = lx1 - (8 if wide else 3)

            cnt_s = f"x{dev['count']}"
            cw = rj_theme.measure_text(_FONTS["badge"] if wide else _FONTS["sub"], cnt_s, d)[2] + (8 if wide else 2)
            if wide:
                rj_theme.draw_pill(d, rx - cw, y + max(1, (row_h - 15) // 2), cnt_s, _FONTS["badge"], bg=_T["bg_elevated"], fg=_T["text_muted"], outline=None, pad_x=4, pad_y=1)
            else:
                d.text((rx - cw + 2, ty), cnt_s, font=_FONTS["sub"], fill=_T["text_dim"])
            rx -= cw + (6 if wide else 3)

            rssi = dev["rssi"]
            rj_theme.draw_signal_bars(d, rx - 14, y + max(1, (row_h - (11 if large else 8)) // 2), 11 if large else 8, _rssi_pct(rssi), active_color=_rssi_col(rssi))
            rx -= 18
            db_s = f"{rssi}dB" if wide else f"{rssi}"
            dw = rj_theme.measure_text(_FONTS["sub"], db_s, d)[2]
            d.text((rx - dw, ty), db_s, font=_FONTS["sub"], fill=_rssi_col(rssi))
            rx -= dw + (8 if wide else 3)

            tx = pad + (8 if wide else 3)
            name_disp = dev["name"] or dev["addr"]
            if not wide and not dev["name"]:
                name_disp = dev["addr"][-8:]
            d.text((tx, ty), rj_theme.truncate_text(d, name_disp, max(16, rx - tx - 2), _FONTS["body_bold"] if (is_cur and wide) else _FONTS["sub"]), font=_FONTS["body_bold"] if (is_cur and wide) else (_FONTS["body"] if wide else _FONTS["sub"]), fill=_T["text_bright"] if is_cur else _T["text_primary"])

        if len(devs) > vis:
            rj_theme.draw_scrollbar(d, lx1 + 2 if split else WIDTH - 4, y0, y1, start, vis, len(devs), width=3)

        if split:
            cur = devs[cur_sel]
            px0 = lx1 + 10
            rj_theme.draw_card(d, (px0, y0, WIDTH - pad, y1), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=8, accent_left=_rssi_col(cur["rssi"]), accent_width=4)
            dx, dy = px0 + 12, y0 + 12
            dmw = WIDTH - pad - dx - 8
            d.text((dx, dy), "BLE DEVICE INSPECTOR", font=_FONTS["badge"], fill=_T["accent_bright"])
            dy += 20
            d.text((dx, dy), rj_theme.truncate_text(d, cur["name"] or "(Unnamed BLE)", dmw, _FONTS["title"]), font=_FONTS["title"], fill=_T["text_bright"])
            dy += 22
            d.text((dx, dy), cur["addr"], font=_FONTS["mono"], fill=_T["accent_bright"])
            dy += 22
            d.line([(dx, dy), (WIDTH - pad - 10, dy)], fill=_T["border_subtle"])
            dy += 10
            for k, v, c in [
                ("Signal", f"{cur['rssi']} dBm", _rssi_col(cur["rssi"])),
                ("Seen", f"{cur['count']} times", _T["warn"]),
                ("First Seen", cur["first_seen"], _T["text_muted"]),
                ("Last Seen", cur["last_seen"], _T["text_primary"]),
                ("Adapter", HCI_DEV or "hci0", _T["text_muted"]),
            ]:
                d.text((dx, dy), f"{k}:", font=_FONTS["sub"], fill=_T["text_muted"])
                d.text((dx + 72, dy), rj_theme.truncate_text(d, v, dmw - 72, _FONTS["body_bold"]), font=_FONTS["body_bold"], fill=c)
                dy += 22
            if dy + 24 <= y1:
                rj_theme.draw_pill(d, dx, y1 - 28, "RIGHT -> ENUMERATE GATT", _FONTS["badge"], bg=_T["select_bg"], fg=_T["accent_bright"], outline=_T["border_accent"], pad_x=6, pad_y=2)

    LCD.LCD_ShowImage(img, 0, 0)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    global scroll_pos, selected_idx, sort_idx, status_msg
    global view, detail_lines, detail_scroll, HCI_DEV

    HCI_DEV = select_bt_interface(LCD, font, PINS, GPIO)
    if not HCI_DEV:
        GPIO.cleanup()
        return 1

    # Splash
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ImageDraw.Draw(img)
    rj_theme.draw_app_header(d, WIDTH, "BLE SCANNER", _FONTS, badge=HCI_DEV)
    rj_theme.draw_status_card(
        d, WIDTH, HEIGHT, "EmberJack BLE Scanner",
        sub=f"Adapter: {HCI_DEV}  |  Continuous RSSI & GATT Explorer",
        kind="info",
        foot="OK:Scan  Right:GATT  K1:Sort  K2:Export",
        fonts=_FONTS,
    )
    LCD.LCD_ShowImage(img, 0, 0)
    time.sleep(1.0)

    try:
        while _running:
            btn = get_button(PINS, GPIO)

            if btn == "KEY3":
                if view == "detail":
                    view = "list"
                    time.sleep(0.25)
                else:
                    break

            elif view == "detail":
                if btn == "LEFT":
                    view = "list"
                    time.sleep(0.25)
                elif btn == "UP":
                    detail_scroll = max(0, detail_scroll - 1)
                    time.sleep(0.2)
                elif btn == "DOWN":
                    detail_scroll = min(detail_scroll + 1,
                                        max(0, len(detail_lines) - 8))
                    time.sleep(0.2)

            else:
                if btn == "OK":
                    with lock:
                        active = scanning
                    if active:
                        _stop_scan()
                    else:
                        _start_scan()
                    time.sleep(0.3)

                elif btn == "UP":
                    with lock:
                        selected_idx = max(0, selected_idx - 1)
                        if selected_idx < scroll_pos:
                            scroll_pos = selected_idx
                    time.sleep(0.2)

                elif btn == "DOWN":
                    devs = _sorted_devices()
                    with lock:
                        selected_idx = min(len(devs) - 1, selected_idx + 1)
                        if selected_idx >= scroll_pos + ROWS_VISIBLE:
                            scroll_pos = selected_idx - ROWS_VISIBLE + 1
                    time.sleep(0.2)

                elif btn == "RIGHT":
                    devs = _sorted_devices()
                    if devs and 0 <= selected_idx < len(devs):
                        addr = devs[selected_idx]["addr"]
                        with lock:
                            status_msg = "Connecting..."
                        detail_lines = _enumerate_services(addr)
                        detail_scroll = 0
                        view = "detail"
                        with lock:
                            status_msg = "Detail view"
                    time.sleep(0.3)

                elif btn == "KEY1":
                    with lock:
                        sort_idx = (sort_idx + 1) % len(SORT_MODES)
                        status_msg = f"Sort: {SORT_MODES[sort_idx]}"
                    time.sleep(0.25)

                elif btn == "KEY2":
                    path = _export_json()
                    with lock:
                        status_msg = "Exported"
                    time.sleep(0.3)

            _draw_screen()
            time.sleep(0.05)

    finally:
        _stop_scan()
        try:
            LCD.LCD_Clear()
        except Exception:
            pass
        GPIO.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
