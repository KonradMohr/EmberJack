#!/usr/bin/env python3
"""
RaspyJack Payload -- Visual Network Topology Mapper
=====================================================
Author: 7h30th3r0n3

Combines ARP scan results and Nmap loot to render a simple network
topology on the LCD.  Gateway at top, hosts arranged by
subnet.  Nodes are small labelled boxes with lines to the gateway.

Controls:
  OK          -- Refresh scan (re-discover hosts)
  UP / DOWN   -- Scroll view vertically
  LEFT / RIGHT-- Zoom in / out
  KEY1        -- Toggle labels: IP / MAC / vendor
  KEY2        -- Export text map to loot
  KEY3        -- Exit
"""

import os
import sys
import json
import time
import subprocess
import re
import threading
from datetime import datetime

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44, LCD_Config
import rj_theme
from PIL import Image, ImageDraw, ImageFont
from payloads._display_helper import ScaledDraw, scaled_font
from payloads._input_helper import get_button
from payloads._iface_helper import select_interface

_T = rj_theme.EMBER_THEME

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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
NMAP_LOOT = "/root/Raspyjack/loot/Nmap"
LOOT_DIR = "/root/Raspyjack/loot/NetMap"
os.makedirs(LOOT_DIR, exist_ok=True)

IP_RE = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
LABEL_MODES = ["ip", "mac", "vendor"]

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
lock = threading.Lock()
busy = False
status_msg = "Idle"
scroll_y = 0
zoom = 1
label_mode_idx = 0
stop_flag = False

# {ip: {"mac": str, "vendor": str, "is_gw": bool, "ports": []}}
hosts = {}
gateway_ip = ""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _get_gateway():
    """Return default gateway IP."""
    try:
        out = subprocess.run(
            ["ip", "-4", "route", "show", "default"],
            capture_output=True, text=True, timeout=5,
        )
        for line in out.stdout.splitlines():
            parts = line.split()
            if "via" in parts:
                return parts[parts.index("via") + 1]
    except Exception:
        pass
    return ""


def _arp_scan():
    """Gather hosts from ARP table."""
    found = {}
    try:
        out = subprocess.run(
            ["arp", "-an"], capture_output=True, text=True, timeout=5,
        )
        for line in out.stdout.splitlines():
            m_ip = re.search(r"\((\d+\.\d+\.\d+\.\d+)\)", line)
            m_mac = re.search(r"(([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})", line)
            if m_ip:
                ip = m_ip.group(1)
                mac = m_mac.group(1).lower() if m_mac else ""
                if not ip.endswith(".255") and not ip.endswith(".0"):
                    found[ip] = {"mac": mac, "vendor": _vendor_prefix(mac), "is_gw": False, "ports": []}
    except Exception:
        pass
    return found


def _vendor_prefix(mac):
    """Crude vendor lookup from OUI prefix."""
    oui_map = {
        "dc:a6:32": "RasPi", "b8:27:eb": "RasPi", "e4:5f:01": "RasPi",
        "00:50:56": "VMware", "00:0c:29": "VMware", "08:00:27": "VBox",
        "00:1a:2b": "Cisco", "00:1b:44": "Cisco", "44:38:39": "Cumul",
        "f8:75:a4": "DELL", "d4:be:d9": "DELL", "00:25:b5": "HP",
    }
    prefix = mac[:8].lower() if mac else ""
    return oui_map.get(prefix, "")


def _enrich_from_nmap():
    """Add port info from Nmap loot files."""
    if not os.path.isdir(NMAP_LOOT):
        return
    with lock:
        current = dict(hosts)
    for fname in os.listdir(NMAP_LOOT):
        fpath = os.path.join(NMAP_LOOT, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, "r", errors="ignore") as f:
                content = f.read()
            for ip in current:
                if ip in content:
                    port_matches = re.findall(r"(\d+)/open", content)
                    if port_matches:
                        with lock:
                            if ip in hosts:
                                hosts[ip] = {**hosts[ip], "ports": port_matches[:6]}
        except Exception:
            pass


def _do_refresh():
    """Full discovery refresh."""
    global busy, status_msg, hosts, gateway_ip, stop_flag

    with lock:
        busy = True
        stop_flag = False
        status_msg = "Scanning..."

    gw = _get_gateway()
    with lock:
        gateway_ip = gw

    discovered = _arp_scan()

    if gw and gw not in discovered:
        discovered[gw] = {"mac": "", "vendor": "", "is_gw": True, "ports": []}
    if gw and gw in discovered:
        discovered[gw] = {**discovered[gw], "is_gw": True}

    with lock:
        hosts = discovered
        status_msg = f"Found {len(discovered)} hosts"

    _enrich_from_nmap()

    with lock:
        busy = False
        status_msg = f"{len(hosts)} hosts mapped"


def start_refresh():
    with lock:
        if busy:
            return
    threading.Thread(target=_do_refresh, daemon=True).start()


# ---------------------------------------------------------------------------
# Drawing (native resolution-adaptive Obsidian Ember)
# ---------------------------------------------------------------------------

_FONTS = rj_theme.load_app_fonts(WIDTH, HEIGHT)


def _node_label(info, ip):
    """Return label string based on current label mode."""
    mode = LABEL_MODES[label_mode_idx]
    if mode == "ip":
        return ip if _FONTS["wide"] else ".".join(ip.split(".")[-2:])
    elif mode == "mac":
        return (info["mac"].upper() if _FONTS["large"] else info["mac"][-8:].upper()) if info["mac"] else ip
    else:
        return info["vendor"] if info["vendor"] else ip


def draw_topology():
    """Render network topology on LCD using native Obsidian Ember layout."""
    wide, large = _FONTS["wide"], _FONTS["large"]
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ImageDraw.Draw(img)

    with lock:
        active = busy
        status = status_msg
        host_list = sorted(hosts.items(), key=lambda kv: kv[1]["is_gw"], reverse=True)
        gw = gateway_ip
        sy = scroll_y
        z = zoom

    mode_txt = LABEL_MODES[label_mode_idx].upper()
    hdr_h = rj_theme.draw_app_header(
        d, WIDTH, "NETWORK MAPPER", _FONTS,
        badge="SCAN" if active else f"{len(host_list)} HOSTS",
        right_text=status if wide else "",
        right_pill=f"Z:{z}x | {mode_txt}" if wide else "",
    )
    ftr_h = rj_theme.draw_app_footer(
        d, WIDTH, HEIGHT,
        "OK:Rescan   L/R:Zoom   Up/Dn:Pan   K1:Label   K2:Export   K3:Exit" if wide else f"Z:{z} {mode_txt} [{len(host_list)}] K1:Lbl",
        _FONTS,
        right_text=f"GW {gw}" if (large and gw) else "",
    )

    if not host_list:
        rj_theme.draw_status_card(
            d, WIDTH, HEIGHT,
            "Scanning Network..." if active else "No Hosts Mapped",
            sub=status,
            kind="warn" if active else "info",
            foot="OK: Refresh scan   K3: Exit",
            fonts=_FONTS,
        )
        LCD.LCD_ShowImage(img, 0, 0)
        return

    pad = 10 if large else (5 if wide else 3)
    y_top = hdr_h + pad
    y_bot = HEIGHT - ftr_h - pad

    gw_entry = None
    others = []
    for ip, info in host_list:
        if info["is_gw"]:
            gw_entry = (ip, info)
        else:
            others.append((ip, info))

    # Scale scroll step proportionally to display height
    pan_px = int(sy * (HEIGHT / 128.0))

    # Gateway node centered at top
    gw_w = (220 if large else (140 if wide else 72))
    gw_h = (44 if large else (28 if wide else 18))
    gw_cx = WIDTH // 2
    gw_y = y_top - pan_px
    gw_bot_y = gw_y + gw_h

    # Responsive host grid sized by zoom
    if large:
        cols = max(2, 5 - z)   # z=1 -> 4 cols, z=2 -> 3 cols, z=3 -> 2 cols
        gap_x, gap_y = 12, 16
        node_w = (WIDTH - pad * 2 - gap_x * (cols - 1)) // cols
        node_h = 34 + z * 12
    elif wide:
        cols = max(1, 4 - z)   # z=1 -> 3 cols, z=2 -> 2 cols, z=3 -> 1 col
        gap_x, gap_y = 8, 10
        node_w = (WIDTH - pad * 2 - gap_x * (cols - 1)) // cols
        node_h = 22 + z * 8
    else:
        cols = max(1, 4 - z) if WIDTH >= 200 else max(1, 3 - (z - 1))
        gap_x, gap_y = 5, 8
        node_w = (WIDTH - pad * 2 - gap_x * (cols - 1)) // cols
        node_h = 16 + z * 4

    hosts_start_y = gw_bot_y + (26 if large else (16 if wide else 12))

    # Draw hub-and-spoke topology links first (under cards)
    if gw_entry:
        for idx, (ip, info) in enumerate(others):
            col_i = idx % cols
            row_i = idx // cols
            nx = pad + col_i * (node_w + gap_x)
            ny = hosts_start_y + row_i * (node_h + gap_y)
            if ny + node_h < hdr_h or ny > y_bot:
                continue
            d.line([(gw_cx, max(hdr_h, gw_bot_y)), (nx + node_w // 2, ny)], fill=_T["border_accent"], width=1)

    # Draw Gateway hero card
    if gw_entry and gw_bot_y >= hdr_h and gw_y <= y_bot:
        gip, ginfo = gw_entry
        gx0 = gw_cx - gw_w // 2
        rj_theme.draw_card(
            d, (gx0, gw_y, gx0 + gw_w, gw_y + gw_h),
            fill=_T["select_bg"], outline=_T["accent"],
            radius=6 if wide else 3, accent_left=_T["accent_bright"], accent_width=4 if wide else 2,
        )
        glbl = _node_label(ginfo, gip)
        if large:
            rj_theme.draw_pill(d, gx0 + 10, gw_y + 6, "GW", _FONTS["badge"], bg=_T["accent"], fg=_T["text_on_accent"], outline=None, pad_x=5, pad_y=1)
            d.text((gx0 + 44, gw_y + 6), rj_theme.truncate_text(d, glbl, gw_w - 52, _FONTS["body_bold"]), font=_FONTS["body_bold"], fill=_T["text_bright"])
            sub_g = ginfo["mac"].upper() if ginfo.get("mac") else "Default Gateway"
            d.text((gx0 + 10, gw_y + 25), rj_theme.truncate_text(d, sub_g, gw_w - 18, _FONTS["sub"]), font=_FONTS["sub"], fill=_T["accent_bright"])
        else:
            d.text((gx0 + (8 if wide else 4), gw_y + (6 if wide else 3)), rj_theme.truncate_text(d, f"GW {glbl}", gw_w - 10, _FONTS["body_bold"] if wide else _FONTS["sub"]), font=_FONTS["body_bold"] if wide else _FONTS["sub"], fill=_T["accent_bright"])

    # Draw Host node cards
    for idx, (ip, info) in enumerate(others):
        col_i = idx % cols
        row_i = idx // cols
        nx = pad + col_i * (node_w + gap_x)
        ny = hosts_start_y + row_i * (node_h + gap_y)
        if ny + node_h < hdr_h or ny > y_bot:
            continue
        ports = info.get("ports", [])
        port_count = len(ports)
        acc = _T["ok"] if port_count > 3 else (_T["accent_bright"] if port_count > 0 else _T["text_muted"])
        rj_theme.draw_card(
            d, (nx, ny, nx + node_w, ny + node_h),
            fill=_T["bg_surface"], outline=_T["border_accent"] if port_count else _T["border_subtle"],
            radius=5 if wide else 3, accent_left=acc, accent_width=3 if wide else 2,
        )
        lbl = _node_label(info, ip)
        tx = nx + (9 if wide else 4)
        mw = node_w - (14 if wide else 6)
        d.text((tx, ny + (6 if large else (4 if wide else 2))), rj_theme.truncate_text(d, lbl, mw, _FONTS["body_bold"] if wide else _FONTS["sub"]), font=_FONTS["body_bold"] if wide else _FONTS["sub"], fill=_T["text_bright"])
        if wide and node_h >= 34:
            sub_parts = []
            if info.get("vendor") and LABEL_MODES[label_mode_idx] != "vendor":
                sub_parts.append(info["vendor"])
            if port_count:
                sub_parts.append(f"{port_count}p: " + ",".join(ports[:4]))
            elif info.get("mac") and LABEL_MODES[label_mode_idx] != "mac":
                sub_parts.append(info["mac"][-8:].upper())
            sub_s = "  ".join(sub_parts) or "Host alive"
            d.text((tx, ny + (24 if large else 18)), rj_theme.truncate_text(d, sub_s, mw, _FONTS["sub"]), font=_FONTS["sub"], fill=_T["ok"] if port_count else _T["text_muted"])

    # Re-draw header & footer clean on top so scrolled nodes clip cleanly
    rj_theme.draw_app_header(
        d, WIDTH, "NETWORK MAPPER", _FONTS,
        badge="SCAN" if active else f"{len(host_list)} HOSTS",
        right_text=status if wide else "",
        right_pill=f"Z:{z}x | {mode_txt}" if wide else "",
    )
    rj_theme.draw_app_footer(
        d, WIDTH, HEIGHT,
        "OK:Rescan   L/R:Zoom   Up/Dn:Pan   K1:Label   K2:Export   K3:Exit" if wide else f"Z:{z} {mode_txt} [{len(host_list)}] K1:Lbl",
        _FONTS,
        right_text=f"GW {gw}" if (large and gw) else "",
    )

    LCD.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# Text export
# ---------------------------------------------------------------------------

def export_text_map():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    lines = [f"Network Map - {ts}", "=" * 40]
    with lock:
        gw = gateway_ip
        hlist = dict(hosts)

    if gw:
        gw_info = hlist.get(gw, {})
        lines.append(f"[GATEWAY] {gw}  MAC:{gw_info.get('mac', '?')}")
        lines.append("  |")

    for ip, info in sorted(hlist.items()):
        if info.get("is_gw"):
            continue
        ports = ",".join(info.get("ports", []))
        lines.append(f"  +-- {ip}  MAC:{info['mac']}  Vendor:{info['vendor']}  Ports:[{ports}]")

    lines.append(f"\nTotal hosts: {len(hlist)}")
    path = os.path.join(LOOT_DIR, f"netmap_{ts}.txt")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


def _show_message(line1, line2=""):
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ImageDraw.Draw(img)
    rj_theme.draw_app_header(d, WIDTH, "NETWORK MAPPER", _FONTS, badge="EXPORT")
    rj_theme.draw_status_card(d, WIDTH, HEIGHT, line1, sub=line2, kind="ok", fonts=_FONTS)
    LCD.LCD_ShowImage(img, 0, 0)
    time.sleep(1.5)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global scroll_y, zoom, label_mode_idx, stop_flag

    selected_iface = select_interface(LCD, font, PINS, GPIO, iface_type="any")
    if not selected_iface:
        GPIO.cleanup()
        return 0

    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ImageDraw.Draw(img)
    rj_theme.draw_app_header(d, WIDTH, "NETWORK MAPPER", _FONTS, badge="INIT")
    rj_theme.draw_status_card(
        d, WIDTH, HEIGHT, "EmberJack Topology Mapper",
        sub="Discovering ARP neighbors & Nmap loot...",
        kind="info",
        foot="OK:Refresh  K1:Labels  L/R:Zoom  K2:Export",
        fonts=_FONTS,
    )
    LCD.LCD_ShowImage(img, 0, 0)
    time.sleep(0.3)

    start_refresh()

    try:
        while True:
            btn = get_button(PINS, GPIO)

            if btn == "KEY3":
                with lock:
                    stop_flag = True
                break

            if btn == "OK":
                start_refresh()
                time.sleep(0.3)

            elif btn == "KEY1":
                label_mode_idx = (label_mode_idx + 1) % len(LABEL_MODES)
                time.sleep(0.2)

            elif btn == "KEY2":
                if hosts:
                    path = export_text_map()
                    _show_message("Exported!", path[-20:])
                time.sleep(0.3)

            elif btn == "UP":
                scroll_y = max(0, scroll_y - 8)
                time.sleep(0.1)

            elif btn == "DOWN":
                scroll_y = scroll_y + 8
                time.sleep(0.1)

            elif btn == "LEFT":
                zoom = max(1, zoom - 1)
                time.sleep(0.2)

            elif btn == "RIGHT":
                zoom = min(3, zoom + 1)
                time.sleep(0.2)

            draw_topology()
            time.sleep(0.05)

    finally:
        with lock:
            stop_flag = True
        time.sleep(0.3)
        try:
            LCD.LCD_Clear()
        except Exception:
            pass
        GPIO.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
