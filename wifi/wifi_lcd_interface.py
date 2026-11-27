#!/usr/bin/env python3
"""
EmberJack WiFi LCD Interface — Obsidian Ember Resolution-Adaptive UI
====================================================================
Native resolution-adaptive WiFi manager supporting HDMI (640x360 split-pane),
CardputerZero (320x170), 240x240, and 128x128 displays with full physical
keyboard + GPIO + WebUI virtual button support.

Features:
  - Live WiFi scanning with signal bars, security badges & SSID inspector
  - Saved profile management (connect, delete, auto-connect)
  - Full physical/on-screen password entry via payloads._keyboard_helper
  - Dual-interface selection (eth0, wlan0, wlan1+) & live connection status
"""

import os
import sys
import time
from typing import Dict, List, Optional

_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

try:
    import rj_env
    import rj_theme
    from PIL import Image, ImageDraw
    _PIL_OK = True
    _T = rj_theme.EMBER_THEME
except Exception:
    rj_env = None
    rj_theme = None
    Image = None
    ImageDraw = None
    _PIL_OK = False
    _T = {}

try:
    from payloads._input_helper import (
        format_footer_hint,
        is_action,
        is_back,
        is_confirm,
    )
except Exception:
    def is_confirm(act) -> bool:
        return act in ("OK", "ENTER")

    def is_back(act) -> bool:
        return act in ("KEY3", "ESCAPE", "BACKSPACE", "DELETE")

    def is_action(act, ch, btn: str, key: str = "") -> bool:
        return act == btn or bool(key and isinstance(ch, str) and ch.lower() == key.lower())

    def format_footer_hint(width: int, height: int = 0, *, hat_hint: str = "", hat_240_hint: str = "", cardputer_hint: str = "", hdmi_hint: str = "", display_type: str = "") -> str:
        if width >= 480:
            return hdmi_hint or cardputer_hint or hat_240_hint or hat_hint
        if width >= 300:
            return cardputer_hint or hdmi_hint or hat_240_hint or hat_hint
        if width >= 200:
            return hat_240_hint or hat_hint or cardputer_hint or hdmi_hint
        return hat_hint or hat_240_hint or cardputer_hint or hdmi_hint

try:
    import LCD_1in44
    import RPi.GPIO as GPIO
    from payloads._input_helper import (
        poll_ui_input,
        flush_input,
        text_input_mode,
        get_virtual_button,
        _flip,
    )
    _HW_OK = True
except Exception as _hw_e:
    print(f"LCD/GPIO import failed: {_hw_e}")
    _HW_OK = False

try:
    from wifi.wifi_manager import WiFiManager
    _WM_OK = True
except Exception:
    try:
        from wifi_manager import WiFiManager  # type: ignore
        _WM_OK = True
    except Exception as _wm_e:
        print(f"WiFiManager import failed: {_wm_e}")
        _WM_OK = False

LCD_AVAILABLE = bool(_PIL_OK and _HW_OK and _WM_OK)


MAIN_MENU_ITEMS = [
    ("Scan Networks", "Scan nearby WiFi access points & connect", "\uf002"),
    ("Saved Profiles", "Connect or remove saved WiFi credentials", "\uf0c7"),
    ("Quick Connect", "Auto-connect to best saved WiFi network", "\uf0e8"),
    ("Disconnect", "Drop active WiFi connection cleanly", "\uf127"),
    ("Interface Config", "Switch active adapter (eth0 / wlan0 / wlan1)", "\uf085"),
    ("Status & Info", "Inspect IPv4, SSID, adapter list & signal", "\uf05a"),
    ("Repair WiFi Stack", "Fix Broadcom/NM drops, bgscan, powersave & conflicts", "\uf0ad"),
    ("Exit", "Return to EmberJack Main Menu", "\uf2f5"),
]


def render_wifi_screen(
    width: int,
    height: int,
    screen: str,
    menu_index: int = 0,
    active_iface: str = "wlan0",
    status: Optional[dict] = None,
    scanned_networks: Optional[List[dict]] = None,
    saved_profiles: Optional[List[dict]] = None,
    interfaces: Optional[List[str]] = None,
    iface_statuses: Optional[Dict[str, dict]] = None,
    wifi_interfaces: Optional[List[str]] = None,
    message: str = "",
    message_kind: str = "info",
    fonts: Optional[dict] = None,
    last_error: str = "",
):
    """Pure PIL resolution-adaptive Obsidian Ember renderer for all WiFi Manager views."""
    if not _PIL_OK:
        return None
    ET = rj_theme.EMBER_THEME
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = f["wide"]
    large = f["large"]
    mid = (not wide) and width >= 200
    split = width >= 480

    st = status or {"status": "disconnected", "ssid": None, "ip": None, "interface": active_iface}
    nets = scanned_networks or []
    profs = saved_profiles or []
    ifaces = interfaces or ["eth0", "wlan0"]
    ist_map = iface_statuses or {}
    w_ifaces = wifi_interfaces if wifi_interfaces is not None else [x for x in ifaces if x != "eth0"]

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    if screen == "message":
        rj_theme.draw_status_card(
            d, width, height,
            message or "WiFi Manager",
            sub=f"Adapter: {active_iface}",
            kind=message_kind,
            fonts=f,
        )
        return img

    is_conn = st.get("status") == "connected"
    conn_pill = (f"{st.get('interface') or active_iface}: {str(st.get('ssid') or '')[:14]}" if is_conn else f"{active_iface}: OFFLINE")
    titles = {
        "main": ("WIFI MANAGER", "WIFI"),
        "scan": ("SCAN NETWORKS", f"{len(nets)} AP"),
        "profiles": ("SAVED PROFILES", str(len(profs))),
        "interface": ("INTERFACES", str(len(ifaces))),
        "status": ("WIFI STATUS", "LINK" if is_conn else "DOWN"),
    }
    t_str, b_str = titles.get(screen, ("WIFI MANAGER", "WIFI"))
    hdr_h = rj_theme.draw_app_header(
        d, width, t_str, f, badge=b_str, right_text=conn_pill,
        hdr_h=(30 if large else (22 if wide else (22 if mid else 18))),
    )

    hint_specs = {
        "main": dict(
            hat_hint="OK:Sel K1:Auto K2:Scn K3:X",
            hat_240_hint="OK:Open K1:Auto K2:Scan K3:Exit",
            cardputer_hint="Ent:Open A:Auto R:Scan Esc:Exit",
            hdmi_hint="Up/Dn:Nav  Enter:Open  A:QuickConn  R:Scan  Esc:Exit",
        ),
        "scan": dict(
            hat_hint="OK:Con K2:Scn K3:Bk",
            hat_240_hint="OK:Conn K2:Scan K3:Back",
            cardputer_hint="Ent:Conn R:Rescan Esc:Back",
            hdmi_hint="Up/Dn:Pick  Enter:Connect  R:Rescan  Esc:Back",
        ),
        "profiles": dict(
            hat_hint="OK:Con K2:Del K3:Bk",
            hat_240_hint="OK:Conn K2:Del K3:Back",
            cardputer_hint="Ent:Conn D:Delete Esc:Back",
            hdmi_hint="Up/Dn:Pick  Enter:Connect  D:Delete  Esc:Back",
        ),
        "interface": dict(
            hat_hint="OK:Sel K3:Bk",
            hat_240_hint="OK:Select K3:Back",
            cardputer_hint="Ent:Select Esc:Back",
            hdmi_hint="Up/Dn:Pick  Enter:Set Active  Esc:Back",
        ),
        "status": dict(
            hat_hint="K2:Ref K3:Bk",
            hat_240_hint="K2:Refresh K3:Back",
            cardputer_hint="R:Refresh Esc:Back",
            hdmi_hint="R:Refresh  Esc:Back",
        ),
    }
    hint = format_footer_hint(
        width, height,
        **hint_specs.get(screen, dict(hat_hint="K3:Bk", hat_240_hint="K3:Back", cardputer_hint="Esc:Back", hdmi_hint="Esc:Back")),
    )
    ftr_h = rj_theme.draw_app_footer(
        d, width, height, hint, f,
        right_text=("ONLINE" if is_conn else "OFFLINE"),
        ftr_h=(22 if large else (16 if wide else (16 if mid else 13))),
    )

    y0 = hdr_h + (8 if large else (4 if (wide or mid) else 2))
    y1 = height - ftr_h - (6 if large else (3 if (wide or mid) else 2))
    pad = 10 if large else (6 if wide else (5 if mid else 3))

    def _draw_connection_card(px0: int, py0: int, px1: int, py1: int, extra_title: str = "", extra_rows=None):
        rj_theme.draw_card(d, (px0, py0, px1, py1), fill=ET["bg_surface"], outline=ET["border_subtle"], radius=7 if wide else 4)
        cpad = 10 if large else (8 if (wide or mid) else 5)
        pw = max(40, px1 - px0 - cpad * 2)
        dy = py0 + (12 if large else (6 if wide else (8 if mid else 4)))
        d.text((px0 + cpad, dy), rj_theme.truncate_text(d, extra_title or "CONNECTION", pw, f["title"]), font=f["title"], fill=ET["accent_bright"])
        dy += 22 if large else (15 if wide else (16 if mid else 12))
        rj_theme.draw_pill(
            d, px0 + cpad, dy,
            "CONNECTED" if is_conn else "DISCONNECTED", f["badge"],
            bg=ET["ok_bg"] if is_conn else ET["danger_bg"],
            fg=ET["ok"] if is_conn else ET["danger"],
            outline=None, pad_x=6 if large else 4, pad_y=2 if large else 1, radius=3,
        )
        dy += 24 if large else (16 if wide else (17 if mid else 13))
        d.line([(px0 + cpad, dy), (px1 - cpad, dy)], fill=ET["border_subtle"], width=1)
        dy += 9 if large else (5 if wide else (6 if mid else 4))
        rows = extra_rows if extra_rows is not None else [
            ("Adapter", str(st.get("interface") or active_iface or "none")),
            ("SSID", str(st.get("ssid") or "Not connected")),
            ("IPv4", str(st.get("ip") or "--")),
            ("Profiles", str(len(profs))),
            ("Scanned", f"{len(nets)} APs"),
            ("Radios", ", ".join(w_ifaces) if w_ifaces else "None"),
        ]
        row_step = 21 if large else (13 if wide else (16 if mid else 11))
        val_off = 74 if large else (58 if (wide or mid) else 44)
        val_font = f["body"] if (large or mid) else f["sub"]
        for rk, rv in rows:
            if dy + row_step - 2 > py1 - 4:
                break
            d.text((px0 + cpad, dy), f"{rk}:", font=f["sub"], fill=ET["text_muted"])
            d.text((px0 + val_off, dy), rj_theme.truncate_text(d, rv, max(30, pw - val_off + cpad - 4), val_font), font=val_font, fill=ET["text_primary"])
            dy += row_step

    # --- 1. MAIN MENU ---
    if screen == "main":
        list_x0 = pad
        list_x1 = int(width * 0.58) - 4 if split else (width - (pad + (6 if len(MAIN_MENU_ITEMS) * (22 if wide else (24 if mid else 16)) > (y1 - y0) else 0)))
        row_h = 36 if large else (22 if wide else (24 if mid else 16))
        vis = max(1, (y1 - y0) // row_h)
        start = max(0, min(menu_index - vis // 2, max(0, len(MAIN_MENU_ITEMS) - vis)))
        for slot in range(vis):
            i = start + slot
            if i >= len(MAIN_MENU_ITEMS):
                break
            lbl, desc, _ico = MAIN_MENU_ITEMS[i]
            ry0 = y0 + slot * row_h
            ry1 = ry0 + row_h - (4 if (wide or mid) else 2)
            sel = i == menu_index
            rj_theme.draw_card(
                d, (list_x0, ry0, list_x1, ry1),
                fill=ET["select_bg"] if sel else ET["bg_surface"],
                outline=ET["border_accent"] if sel else ET["border_subtle"],
                radius=6 if wide else (4 if mid else 3),
                accent_left=ET["accent"] if sel else None,
                accent_width=3 if (wide or mid) else 2,
            )
            tx = list_x0 + (12 if wide else (8 if mid else 6))
            d.text((tx, ry0 + (3 if large else (3 if mid else 2))), rj_theme.truncate_text(d, lbl, list_x1 - tx - 8, f["body_bold"] if (wide or mid) else f["body"]), font=f["body_bold"] if (wide or mid) else f["body"], fill=ET["accent_bright"] if sel else ET["text_primary"])
            if large and row_h >= 34:
                d.text((tx, ry0 + 18), rj_theme.truncate_text(d, desc, list_x1 - tx - 8, f["sub"]), font=f["sub"], fill=ET["text_muted"])
        if len(MAIN_MENU_ITEMS) > vis:
            rj_theme.draw_scrollbar(d, list_x1 + 2, y0, y1, start, vis, len(MAIN_MENU_ITEMS), width=4 if (wide or mid) else 2)
        if split:
            _draw_connection_card(list_x1 + 10, y0, width - pad, y1)
        return img

    # --- 2. SCANNED NETWORKS ---
    if screen == "scan":
        if not nets:
            empty_scan_sub = last_error or format_footer_hint(
                width, height,
                hat_hint="K2: Scan again",
                hat_240_hint="Press K2 to scan again",
                cardputer_hint="Press R to scan again",
                hdmi_hint="Press R to scan again",
            )
            rj_theme.draw_status_card(
                d, width, height,
                "No WiFi Networks Found", empty_scan_sub,
                kind="err" if last_error else "warn", fonts=f,
            )
            return img
        list_x0 = pad
        list_x1 = int(width * 0.62) - 4 if split else (width - (pad + 6))
        row_h = 42 if large else (26 if wide else (24 if mid else 16))
        vis = max(1, (y1 - y0) // row_h)
        start = max(0, min(menu_index - vis // 2, max(0, len(nets) - vis)))
        for slot in range(vis):
            idx = start + slot
            if idx >= len(nets):
                break
            net = nets[idx]
            sel = idx == menu_index
            ry0 = y0 + slot * row_h
            ry1 = ry0 + row_h - (4 if (wide or mid) else 2)
            rj_theme.draw_card(
                d, (list_x0, ry0, list_x1, ry1),
                fill=ET["select_bg"] if sel else ET["bg_surface"],
                outline=ET["border_accent"] if sel else ET["border_subtle"],
                radius=6 if wide else (4 if mid else 3),
                accent_left=ET["accent"] if sel else None,
                accent_width=3 if (wide or mid) else 2,
            )
            sig = int(net.get("signal", 0) or 0)
            enc = bool(net.get("encrypted", False))
            sec_txt = str(net.get("security") or ("WPA" if enc else "OPEN"))[:8]
            rx = list_x1 - (8 if (wide or mid) else 3)
            pct_s = f"{sig:2d}%"
            pw_p = rj_theme.measure_text(f["badge"], pct_s, d)[2]
            d.text((rx - pw_p, ry0 + (6 if large else (4 if mid else 2))), pct_s, font=f["badge"], fill=ET["accent_bright"] if sel else ET["text_muted"])
            rx -= pw_p + 6
            bar_h = 12 if large else (10 if (wide or mid) else 8)
            rj_theme.draw_signal_bars(d, rx - 18, ry0 + (5 if large else (4 if mid else 2)), bar_h, sig, active_color=ET["accent_bright"] if sel else ET["ok"])
            rx -= 24
            if wide:
                sw = rj_theme.measure_text(f["badge"], sec_txt, d)[2] + 8
                if rx - sw > list_x0 + 80:
                    rj_theme.draw_pill(
                        d, rx - sw, ry0 + (5 if large else 2), sec_txt, f["badge"],
                        bg=ET["bg_well"], fg=ET["warn"] if enc else ET["ok"],
                        outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3,
                    )
                    rx -= sw + 6
            tx = list_x0 + (10 if wide else (7 if mid else 5))
            ssid = str(net.get("ssid") or "Hidden")
            d.text((tx, ry0 + (4 if large else (3 if mid else 2))), rj_theme.truncate_text(d, ssid, max(36, rx - tx), f["body_bold"] if (wide or mid) else f["body"]), font=f["body_bold"] if (wide or mid) else f["body"], fill=ET["text_bright"] if sel else ET["text_primary"])
            if large:
                d.text((tx, ry0 + 22), rj_theme.truncate_text(d, str(net.get("bssid") or ""), list_x1 - tx - 10, f["sub"]), font=f["sub"], fill=ET["text_muted"])

        if len(nets) > vis:
            rj_theme.draw_scrollbar(d, list_x1 + 2, y0, y1, start, vis, len(nets), width=4 if (wide or mid) else 2)

        if split and 0 <= menu_index < len(nets):
            cur = nets[menu_index]
            _draw_connection_card(
                list_x1 + 10, y0, width - pad, y1,
                extra_title=str(cur.get("ssid") or "Access Point"),
                extra_rows=[
                    ("Signal", f"{int(cur.get('signal', 0) or 0)}%"),
                    ("Security", str(cur.get("security") or ("Encrypted" if cur.get("encrypted") else "Open"))),
                    ("BSSID", str(cur.get("bssid") or "N/A")),
                    ("Adapter", str(active_iface)),
                    ("Action", "Enter to connect"),
                ],
            )
        return img

    # --- 3. SAVED PROFILES ---
    if screen == "profiles":
        if not profs:
            rj_theme.draw_status_card(d, width, height, "No Saved Profiles", "Scan & connect to save a WiFi network", kind="info", fonts=f)
            return img
        list_x0 = pad
        list_x1 = int(width * 0.62) - 4 if split else (width - (pad + 6))
        row_h = 40 if large else (26 if wide else (24 if mid else 16))
        vis = max(1, (y1 - y0) // row_h)
        start = max(0, min(menu_index - vis // 2, max(0, len(profs) - vis)))
        for slot in range(vis):
            idx = start + slot
            if idx >= len(profs):
                break
            pr = profs[idx]
            sel = idx == menu_index
            ry0 = y0 + slot * row_h
            ry1 = ry0 + row_h - (4 if (wide or mid) else 2)
            rj_theme.draw_card(
                d, (list_x0, ry0, list_x1, ry1),
                fill=ET["select_bg"] if sel else ET["bg_surface"],
                outline=ET["border_accent"] if sel else ET["border_subtle"],
                radius=6 if wide else (4 if mid else 3),
                accent_left=ET["accent"] if sel else None,
                accent_width=3 if (wide or mid) else 2,
            )
            prio = f"P{pr.get('priority', 1)}"
            pw_p = rj_theme.measure_text(f["badge"], prio, d)[2] + 8
            rj_theme.draw_pill(
                d, list_x1 - pw_p - 6, ry0 + (5 if large else (3 if mid else 2)), prio, f["badge"],
                bg=ET["bg_well"], fg=ET["accent_bright"] if sel else ET["text_muted"],
                outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3,
            )
            tx = list_x0 + (10 if wide else (7 if mid else 5))
            d.text((tx, ry0 + (4 if large else (3 if mid else 2))), rj_theme.truncate_text(d, str(pr.get("ssid", "?")), list_x1 - tx - pw_p - 14, f["body_bold"] if (wide or mid) else f["body"]), font=f["body_bold"] if (wide or mid) else f["body"], fill=ET["text_bright"] if sel else ET["text_primary"])
            if large:
                sub = f"Interface: {pr.get('interface', 'auto')}  •  Auto: {'Yes' if pr.get('auto_connect', True) else 'No'}"
                d.text((tx, ry0 + 21), rj_theme.truncate_text(d, sub, list_x1 - tx - 10, f["sub"]), font=f["sub"], fill=ET["text_muted"])
        if len(profs) > vis:
            rj_theme.draw_scrollbar(d, list_x1 + 2, y0, y1, start, vis, len(profs), width=4 if (wide or mid) else 2)
        if split and 0 <= menu_index < len(profs):
            cur = profs[menu_index]
            _draw_connection_card(
                list_x1 + 10, y0, width - pad, y1,
                extra_title=str(cur.get("ssid", "Profile")),
                extra_rows=[
                    ("Priority", str(cur.get("priority", 1))),
                    ("Adapter", str(cur.get("interface", "auto"))),
                    ("Auto", "Enabled" if cur.get("auto_connect", True) else "Disabled"),
                    ("Last Used", str(cur.get("last_used") or "Never")[:16]),
                    ("Keys", "Enter:Conn  D:Del"),
                ],
            )
        return img

    # --- 4. INTERFACE CONFIG ---
    if screen == "interface":
        list_x0 = pad
        list_x1 = int(width * 0.62) - 4 if split else (width - (pad + 6))
        row_h = 40 if large else (26 if wide else (24 if mid else 16))
        vis = max(1, (y1 - y0) // row_h)
        start = max(0, min(menu_index - vis // 2, max(0, len(ifaces) - vis)))
        for slot in range(vis):
            i = start + slot
            if i >= len(ifaces):
                break
            ifc = ifaces[i]
            ry0 = y0 + slot * row_h
            ry1 = ry0 + row_h - (4 if (wide or mid) else 2)
            sel = i == menu_index
            is_act = (ifc == active_iface)
            ist = ist_map.get(ifc, {})
            ic = ist.get("status") == "connected"
            rj_theme.draw_card(
                d, (list_x0, ry0, list_x1, ry1),
                fill=ET["select_bg"] if sel else ET["bg_surface"],
                outline=ET["border_accent"] if sel else ET["border_subtle"],
                radius=6 if wide else (4 if mid else 3),
                accent_left=ET["accent"] if sel else None,
                accent_width=3 if (wide or mid) else 2,
            )
            rx = list_x1 - 8
            if is_act:
                aw = rj_theme.measure_text(f["badge"], "ACTIVE", d)[2] + 8
                rj_theme.draw_pill(d, rx - aw, ry0 + (5 if large else (3 if mid else 2)), "ACTIVE", f["badge"], bg=ET["ok_bg"], fg=ET["ok"], outline=None, pad_x=4, pad_y=1, radius=3)
                rx -= aw + 6
            tx = list_x0 + (10 if wide else (7 if mid else 5))
            d.text((tx, ry0 + (4 if large else (3 if mid else 2))), ifc, font=f["body_bold"] if (wide or mid) else f["body"], fill=ET["ok"] if ic else (ET["text_bright"] if sel else ET["text_primary"]))
            if large:
                sub = f"SSID: {ist['ssid']} ({ist.get('ip') or ''})" if ic else ("Wired Ethernet" if ifc == "eth0" else "Wireless adapter (idle)")
                d.text((tx, ry0 + 21), rj_theme.truncate_text(d, sub, list_x1 - tx - 10, f["sub"]), font=f["sub"], fill=ET["text_muted"])
        if len(ifaces) > vis:
            rj_theme.draw_scrollbar(d, list_x1 + 2, y0, y1, start, vis, len(ifaces), width=4 if (wide or mid) else 2)
        if split:
            _draw_connection_card(list_x1 + 10, y0, width - pad, y1)
        return img

    # --- 5. STATUS & INFO ---
    if split:
        mid = width // 2
        _draw_connection_card(pad, y0, mid - 5, y1, extra_title="ACTIVE LINK")
        rj_theme.draw_card(d, (mid + 5, y0, width - pad, y1), fill=ET["bg_surface"], outline=ET["border_subtle"], radius=7)
        dy = y0 + 12
        d.text((mid + 15, dy), f"WIFI ADAPTERS ({len(w_ifaces)})", font=f["title"], fill=ET["accent_bright"])
        dy += 26
        d.line([(mid + 15, dy), (width - pad - 10, dy)], fill=ET["border_subtle"], width=1)
        dy += 10
        if w_ifaces:
            for w_if in w_ifaces:
                if dy + 22 > y1 - 8:
                    break
                wst = ist_map.get(w_if, {})
                lbl = f"{w_if}  —  {wst.get('ssid') or 'idle'}"
                d.text((mid + 15, dy), rj_theme.truncate_text(d, lbl, (width - pad) - (mid + 25), f["body"]), font=f["body"], fill=ET["ok"] if wst.get("status") == "connected" else ET["text_primary"])
                dy += 24
        else:
            d.text((mid + 15, dy), "No WiFi adapters detected", font=f["body"], fill=ET["text_muted"])
    else:
        _draw_connection_card(pad, y0, width - pad, y1, extra_title="WIFI STATUS")
    return img


class WiFiLCDInterface:
    def __init__(self):
        if not LCD_AVAILABLE:
            raise Exception("LCD hardware not available")

        self.wifi_manager = WiFiManager()

        self.LCD = LCD_1in44.LCD()
        self.LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
        self.WIDTH = self.LCD.width
        self.HEIGHT = self.LCD.height
        self.fonts = rj_theme.load_app_fonts(self.WIDTH, self.HEIGHT)
        self.font = self.fonts["body"]
        self.font_big = self.fonts["title"]
        self.canvas = Image.new("RGB", (self.WIDTH, self.HEIGHT), _T["bg_base"])

        GPIO.setmode(GPIO.BCM)
        self.setup_buttons()

        self.current_menu = "main"
        self.menu_index = 0
        self.running = True

        self.scanned_networks: List[dict] = []
        self.saved_profiles: List[dict] = []
        self._status_cache: Dict[str, tuple] = {}
        self._active_iface_cache = (0.0, None)
        # Render immediately on launch (never hang 15-30s on a blank black screen!)
        self.show_message("Loading WiFi Manager...", duration=0.05)
        self.saved_profiles = self.wifi_manager.load_profiles()

    def _invalidate_status_cache(self):
        self._status_cache.clear()
        self._active_iface_cache = (0.0, None)

    def _get_cached_active_interface(self, ttl=2.5):
        now = time.monotonic()
        ts, val = self._active_iface_cache
        if val is not None and (now - ts) < ttl:
            return val
        val = self.wifi_manager.get_active_interface() or "wlan0"
        self._active_iface_cache = (now, val)
        return val

    def _get_cached_status(self, interface=None, ttl=2.5):
        now = time.monotonic()
        key = interface or "__active__"
        cached = self._status_cache.get(key)
        if cached is not None and (now - cached[0]) < ttl:
            return cached[1]
        st = self.wifi_manager.get_connection_status(interface) if interface else self.wifi_manager.get_connection_status()
        self._status_cache[key] = (now, st)
        return st

    def setup_buttons(self):
        self.buttons = {
            "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
            "CENTER": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
        }
        self._ui_pins = {
            "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
            "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
        }
        for pin in self.buttons.values():
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    def refresh_data(self, rescan: bool = True):
        self.wifi_manager.log("Refreshing WiFi data...")
        self._invalidate_status_cache()
        self.wifi_manager.wifi_interfaces = self.wifi_manager.detect_wifi_interfaces()
        self.scanned_networks = self.wifi_manager.scan_networks(rescan=rescan)
        self.saved_profiles = self.wifi_manager.load_profiles()

    def show_message(self, message, duration=1.6, kind="info"):
        low = str(message or "").lower()
        if kind == "info":
            if "fail" in low or "error" in low or "no " in low:
                kind = "err"
            elif "connected!" in low or "active:" in low or "deleted" in low:
                kind = "ok"
        frame = render_wifi_screen(
            self.WIDTH, self.HEIGHT, "message",
            active_iface=self._get_cached_active_interface(),
            message=str(message), message_kind=kind, fonts=self.fonts,
        )
        if frame is not None:
            self.canvas = frame
            self.LCD.LCD_ShowImage(frame, 0, 0)
        time.sleep(duration)

    def update_display(self):
        interfaces = ["eth0"] + list(self.wifi_manager.wifi_interfaces)
        iface_statuses = {ifc: self._get_cached_status(ifc) for ifc in interfaces} if self.current_menu in ("interface", "status") else {}
        frame = render_wifi_screen(
            self.WIDTH, self.HEIGHT,
            screen=self.current_menu,
            menu_index=self.menu_index,
            active_iface=self._get_cached_active_interface(),
            status=self._get_cached_status(),
            scanned_networks=self.scanned_networks,
            saved_profiles=self.saved_profiles,
            interfaces=interfaces,
            iface_statuses=iface_statuses,
            wifi_interfaces=list(self.wifi_manager.wifi_interfaces),
            fonts=self.fonts,
            last_error=self.wifi_manager.last_error,
        )
        if frame is not None:
            self.canvas = frame
            self.LCD.LCD_ShowImage(frame, 0, 0)

    def do_disconnect(self):
        iface = self.wifi_manager.get_active_wifi_interface()
        if not iface:
            self.show_message("No WiFi adapter found", kind="err")
            return
        self.show_message(f"Disconnecting {iface}...", duration=0.2)
        success = self.wifi_manager.disconnect(iface)
        self._invalidate_status_cache()
        err_msg = self.wifi_manager.last_error or "Disconnect failed"
        self.show_message(f"{iface} disconnected" if success else err_msg, kind="ok" if success else "err")

    def quick_connect(self):
        self.show_message("Auto-connecting...", duration=0.2)
        success = self.wifi_manager.auto_connect()
        self._invalidate_status_cache()
        err_msg = self.wifi_manager.last_error or "Connection failed"
        self.show_message("Connected!" if success else err_msg, kind="ok" if success else "err")

    def do_repair_wifi_stack(self):
        self.show_message("Repairing WiFi Stack...", duration=0.2)
        res = self.wifi_manager.repair_wifi_stack()
        self._invalidate_status_cache()
        self.saved_profiles = self.wifi_manager.load_profiles()
        msg = res.get("summary") or "WiFi Stack Repaired!"
        kind = "warn" if res.get("reboot_needed") else ("ok" if res.get("ok") else "err")
        self.show_message(msg, duration=2.4, kind=kind)

    def connect_to_scanned_network(self):
        if not (0 <= self.menu_index < len(self.scanned_networks)):
            return
        network = self.scanned_networks[self.menu_index]
        ssid = network.get("ssid")
        pw = None
        if network.get("encrypted", False):
            from payloads._keyboard_helper import lcd_keyboard
            flush_input()
            pw = lcd_keyboard(
                self.LCD, self.font, self._ui_pins, GPIO,
                title=f"WiFi Password: {ssid[:22]}",
                default="", charset="full", max_len=64,
            )
            flush_input()
            if pw is None:
                return
            if pw == "":
                pw = None
        self.show_message(f"Connecting to {ssid[:18]}...", duration=0.2)
        success = self.wifi_manager.connect_to_network(ssid, pw, security=network.get("security", ""))
        self._invalidate_status_cache()
        if success:
            if pw is not None:
                self.wifi_manager.save_profile(ssid, pw, "auto", 1, True)
            self.saved_profiles = self.wifi_manager.load_profiles()
            self.show_message(f"Connected: {ssid[:18]}", kind="ok")
            self.current_menu = "main"
            self.menu_index = 0
        else:
            err_msg = self.wifi_manager.last_error or "Connection failed"
            self.show_message(err_msg, duration=2.2, kind="err")

    def connect_to_saved_profile(self):
        if not (0 <= self.menu_index < len(self.saved_profiles)):
            return
        profile = self.saved_profiles[self.menu_index]
        ssid = profile.get("ssid", "")
        self.show_message(f"Connecting to {ssid[:18]}...", duration=0.2)
        success = self.wifi_manager.connect_to_profile(profile)
        self._invalidate_status_cache()
        # If a saved NM profile lost its secret (or had agent-owned psk-flags), prompt once for password!
        if (not success) and (not profile.get("password")) and ("secret" in (self.wifi_manager.last_error or "").lower()):
            from payloads._keyboard_helper import lcd_keyboard
            flush_input()
            pw = lcd_keyboard(
                self.LCD, self.font, self._ui_pins, GPIO,
                title=f"Password for {ssid[:20]}",
                default="", charset="full", max_len=64,
            )
            flush_input()
            if pw:
                self.show_message(f"Connecting to {ssid[:18]}...", duration=0.2)
                success = self.wifi_manager.connect_to_network(ssid, pw, nm_name=profile.get("nm_name"))
                self._invalidate_status_cache()
                if success:
                    self.wifi_manager.save_profile(ssid, pw, "auto", 1, True)
                    self.saved_profiles = self.wifi_manager.load_profiles()
        err_msg = self.wifi_manager.last_error or "Connection failed"
        self.show_message(f"Connected: {ssid[:18]}" if success else err_msg, duration=(1.6 if success else 2.2), kind="ok" if success else "err")

    def delete_profile(self):
        if not (0 <= self.menu_index < len(self.saved_profiles)):
            return
        prof = self.saved_profiles[self.menu_index]
        ssid = prof.get("ssid", "")
        success = self.wifi_manager.delete_profile(ssid, nm_name=prof.get("nm_name"))
        if success:
            self.saved_profiles = self.wifi_manager.load_profiles()
            self.menu_index = min(self.menu_index, max(0, len(self.saved_profiles) - 1))
            self.show_message(f"Deleted {ssid[:18]}", kind="ok")
        else:
            self.show_message("Delete failed", kind="err")

    def _poll_button(self) -> Optional[str]:
        act, _ch = poll_ui_input(self._ui_pins, GPIO)
        if act is None:
            return None
        if is_back(act):
            return "KEY3"
        if is_confirm(act):
            return "CENTER"
        if act in ("UP", "PAGEUP"):
            return "UP"
        if act in ("DOWN", "PAGEDOWN"):
            return "DOWN"
        return act

    def handle_main_menu(self, button, ch=None):
        n = len(MAIN_MENU_ITEMS)
        if button in ("UP", "PAGEUP"):
            self.menu_index = (self.menu_index - 1) % n
        elif button in ("DOWN", "PAGEDOWN"):
            self.menu_index = (self.menu_index + 1) % n
        elif is_back(button):
            self.running = False
        elif is_action(button, ch, "KEY1", "a"):
            self.quick_connect()
        elif is_action(button, ch, "KEY2", "r"):
            self.current_menu = "scan"
            self.menu_index = 0
            self.show_message("Scanning WiFi...", duration=0.2)
            self.refresh_data()
        elif is_confirm(button) or button == "CENTER":
            if self.menu_index == 0:
                self.current_menu = "scan"
                self.menu_index = 0
                self.show_message("Scanning WiFi...", duration=0.2)
                self.refresh_data()
            elif self.menu_index == 1:
                self.saved_profiles = self.wifi_manager.load_profiles()
                self.current_menu = "profiles"
                self.menu_index = 0
            elif self.menu_index == 2:
                self.quick_connect()
            elif self.menu_index == 3:
                self.do_disconnect()
            elif self.menu_index == 4:
                self.wifi_manager.wifi_interfaces = self.wifi_manager.detect_wifi_interfaces()
                self._invalidate_status_cache()
                self.current_menu = "interface"
                self.menu_index = 0
            elif self.menu_index == 5:
                self.wifi_manager.wifi_interfaces = self.wifi_manager.detect_wifi_interfaces()
                self._invalidate_status_cache()
                self.current_menu = "status"
            elif self.menu_index == 6:
                self.do_repair_wifi_stack()
            elif self.menu_index == 7:
                self.running = False

    def handle_scan_menu(self, button, ch=None):
        if button in ("UP", "PAGEUP") and self.scanned_networks:
            self.menu_index = (self.menu_index - 1) % len(self.scanned_networks)
        elif button in ("DOWN", "PAGEDOWN") and self.scanned_networks:
            self.menu_index = (self.menu_index + 1) % len(self.scanned_networks)
        elif (is_confirm(button) or button == "CENTER") and self.scanned_networks:
            self.connect_to_scanned_network()
        elif is_action(button, ch, "KEY2", "r"):
            self.show_message("Scanning WiFi...", duration=0.2)
            self.refresh_data()
        elif is_back(button):
            self.current_menu = "main"
            self.menu_index = 0

    def handle_profiles_menu(self, button, ch=None):
        if not self.saved_profiles:
            if is_back(button):
                self.current_menu = "main"
                self.menu_index = 0
            return
        if button in ("UP", "PAGEUP"):
            self.menu_index = (self.menu_index - 1) % len(self.saved_profiles)
        elif button in ("DOWN", "PAGEDOWN"):
            self.menu_index = (self.menu_index + 1) % len(self.saved_profiles)
        elif is_confirm(button) or button == "CENTER":
            self.connect_to_saved_profile()
        elif is_action(button, ch, "KEY2", "d"):
            self.delete_profile()
        elif is_back(button):
            self.current_menu = "main"
            self.menu_index = 0

    def handle_interface_menu(self, button, ch=None):
        interfaces = ["eth0"] + list(self.wifi_manager.wifi_interfaces)
        if button in ("UP", "PAGEUP"):
            self.menu_index = (self.menu_index - 1) % len(interfaces)
        elif button in ("DOWN", "PAGEDOWN"):
            self.menu_index = (self.menu_index + 1) % len(interfaces)
        elif is_confirm(button) or button == "CENTER":
            selected = interfaces[self.menu_index]
            self.wifi_manager.set_selected_interface(selected)
            self._invalidate_status_cache()
            self.show_message(f"Active: {selected}", duration=0.9, kind="ok")
        elif is_back(button):
            self.current_menu = "main"
            self.menu_index = 0

    def run(self):
        self.wifi_manager.log("Starting WiFi LCD interface")
        with text_input_mode():
            flush_input()
            self.update_display()
            last_status_sig = (
                self._get_cached_active_interface(),
                tuple(sorted(self._get_cached_status().items())),
            )
            last_update = time.monotonic()
            try:
                while self.running:
                    now = time.monotonic()
                    if (now - last_update) > 2.5:
                        last_update = now
                        cur_sig = (
                            self._get_cached_active_interface(ttl=0.0),
                            tuple(sorted(self._get_cached_status(ttl=0.0).items())),
                        )
                        if cur_sig != last_status_sig or self.current_menu == "status":
                            last_status_sig = cur_sig
                            self.update_display()

                    act, ch = poll_ui_input(self._ui_pins, GPIO)
                    if act is not None or ch is not None:
                        if self.current_menu == "main":
                            self.handle_main_menu(act, ch)
                        elif self.current_menu == "scan":
                            self.handle_scan_menu(act, ch)
                        elif self.current_menu == "profiles":
                            self.handle_profiles_menu(act, ch)
                        elif self.current_menu == "interface":
                            self.handle_interface_menu(act, ch)
                        elif self.current_menu == "status":
                            if is_action(act, ch, "KEY2", "r"):
                                self._invalidate_status_cache()
                            elif is_back(act):
                                self.current_menu = "main"
                                self.menu_index = 0
                        if self.running:
                            self.update_display()
                        last_status_sig = (
                            self._get_cached_active_interface(),
                            tuple(sorted(self._get_cached_status().items())),
                        )
                        last_update = time.monotonic()
                    time.sleep(0.035)
            except KeyboardInterrupt:
                pass
            finally:
                self.wifi_manager.log("WiFi LCD interface stopped")
                GPIO.cleanup()


def main():
    import traceback
    try:
        interface = WiFiLCDInterface()
        interface.run()
        return 0
    except Exception as e:
        print(f"WiFi Manager Error: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
