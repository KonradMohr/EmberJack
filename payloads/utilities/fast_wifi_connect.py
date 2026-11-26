#!/usr/bin/env python3
"""
EmberJack Payload -- Quick WiFi Connect
=======================================
Scan nearby WiFi networks with signal bars & security pills, auto-connect
to the strongest saved network, or select any network and enter its
passphrase using the shared Obsidian Ember keyboard.

Controls:
  UP / DOWN / PgUp / PgDn  -- Navigate WiFi networks
  ENTER / OK               -- Connect to selected network (prompts password if needed)
  KEY1 / A                 -- Auto-connect to strongest saved WiFi
  KEY2 / R                 -- Rescan WiFi networks
  P                        -- Enter / override password for selected SSID
  H                        -- Scan subnet hosts on active connection
  KEY3 / ESC               -- Exit
"""

import json
import os
import re
import signal
import subprocess
import sys
import time

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

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

PROFILES_DIR = os.path.join(rj_env.get_base_dir(), "wifi", "profiles")

LCD_OK = False
LCD = None
GPIO_MOD = None
WIDTH = 640
HEIGHT = 360
FONTS = None
_running = True


def _cleanup(*_args):
    global _running
    _running = False


signal.signal(signal.SIGINT, _cleanup)
signal.signal(signal.SIGTERM, _cleanup)


# ---------------------------------------------------------------------------
# Hardware initialization
# ---------------------------------------------------------------------------

def _init_lcd():
    global LCD_OK, LCD, GPIO_MOD, WIDTH, HEIGHT, FONTS
    try:
        import RPi.GPIO as GPIO  # type: ignore
        import LCD_1in44  # type: ignore
        import LCD_Config  # type: ignore

        GPIO.setmode(GPIO.BCM)
        for pin in PINS.values():
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        LCD_Config.GPIO_Init()
        LCD = LCD_1in44.LCD()
        LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
        LCD.LCD_Clear()
        WIDTH, HEIGHT = LCD.width, LCD.height
        FONTS = rj_theme.load_app_fonts(WIDTH, HEIGHT)
        GPIO_MOD = GPIO
        LCD_OK = True
    except Exception:
        LCD_OK = False


# ---------------------------------------------------------------------------
# WiFi backend helpers
# ---------------------------------------------------------------------------

def _run(cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def _unescape_nmcli_field(raw: str) -> str:
    return raw.replace(r"\:", ":").replace(r"\\", "\\")


def _split_nmcli_line(line: str):
    """Split a `nmcli -t` colon-delimited line honoring backslash-escaped colons."""
    parts = []
    cur = []
    escaped = False
    for ch in line:
        if escaped:
            cur.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _get_saved_wifi():
    """Get saved WiFi networks from both nmcli and EmberJack JSON profiles."""
    saved = {}  # ssid -> password (None if nmcli-only)

    res = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], timeout=8)
    if res.returncode == 0:
        for line in res.stdout.strip().splitlines():
            if not line:
                continue
            parts = _split_nmcli_line(line)
            if len(parts) < 2:
                continue
            name, ctype = parts[0], parts[1]
            if ctype in ("wifi", "802-11-wireless") and name:
                saved[name] = None

    if os.path.isdir(PROFILES_DIR):
        try:
            for fname in os.listdir(PROFILES_DIR):
                if not fname.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(PROFILES_DIR, fname), "r", encoding="utf-8") as fh:
                        profile = json.load(fh)
                    ssid = str(profile.get("ssid", "") or "")
                    password = str(profile.get("password", "") or "")
                    if ssid and ssid not in saved:
                        saved[ssid] = password or None
                    elif ssid and password and saved.get(ssid) is None:
                        saved[ssid] = password
                except Exception:
                    continue
        except Exception:
            pass

    return saved


def _save_wifi_profile(ssid: str, password: str, security: str = "") -> None:
    """Persist a WiFi profile JSON to PROFILES_DIR for future quick-connects."""
    if not ssid:
        return
    try:
        os.makedirs(PROFILES_DIR, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", ssid).strip("_") or "wifi"
        path = os.path.join(PROFILES_DIR, f"{safe}.json")
        data = {
            "ssid": ssid,
            "password": password,
            "security": security,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except Exception:
        pass


def _format_security_tag(security: str) -> str:
    s = str(security or "").upper().strip()
    if not s or s == "--":
        return "OPEN"
    if "WPA3" in s:
        return "WPA3"
    if "WPA2" in s:
        return "WPA2"
    if "WPA1" in s or "WPA" in s:
        return "WPA"
    if "WEP" in s:
        return "WEP"
    if "802.1X" in s:
        return "802.1X"
    return s.split()[0][:6]


def _scan_wifi(dev=None, rescan=False):
    """Scan nearby WiFi networks via nmcli, deduplicated by strongest signal."""
    cmd_base = ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list"]
    if dev:
        cmd_base += ["ifname", dev]

    if rescan:
        res = _run(cmd_base + ["--rescan", "yes"], timeout=18)
    else:
        res = _run(cmd_base + ["--rescan", "no"], timeout=10)
        if res.returncode != 0 or not res.stdout.strip():
            res = _run(cmd_base + ["--rescan", "yes"], timeout=18)

    if res.returncode != 0:
        return []

    by_ssid = {}
    for line in res.stdout.strip().splitlines():
        if not line:
            continue
        parts = _split_nmcli_line(line)
        if len(parts) < 4:
            continue
        in_use = parts[0].strip() == "*"
        ssid = parts[1].strip()
        if not ssid:
            continue
        try:
            sig = int(parts[2].strip())
        except ValueError:
            sig = 0
        sec = parts[3].strip()
        prev = by_ssid.get(ssid)
        if prev is None or sig > prev["signal"] or (in_use and not prev["active"]):
            by_ssid[ssid] = {
                "ssid": ssid,
                "signal": max(0, min(100, sig)),
                "security": sec,
                "sec_tag": _format_security_tag(sec),
                "active": in_use or (prev["active"] if prev else False),
            }
    return list(by_ssid.values())


def _get_wifi_device():
    res = _run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "dev"], timeout=8)
    if res.returncode != 0:
        return None
    for line in res.stdout.strip().splitlines():
        if not line:
            continue
        parts = _split_nmcli_line(line)
        if len(parts) >= 3:
            dev, dtype, state = parts[0], parts[1], parts[2]
            if dtype == "wifi" and state in ("connected", "connecting", "disconnected"):
                return dev
    return None


def _get_iface_ip(dev: str) -> str:
    ip_res = _run(["ip", "-4", "addr", "show", "dev", dev], timeout=5)
    if ip_res.returncode == 0:
        for line in ip_res.stdout.splitlines():
            s = line.strip()
            if s.startswith("inet "):
                return s.split()[1].split("/")[0]
    return ""


def _get_active_ssid(dev: str) -> str:
    res = _run(["nmcli", "-t", "-f", "ACTIVE,SSID,DEVICE", "dev", "wifi"], timeout=6)
    if res.returncode == 0:
        for line in res.stdout.splitlines():
            parts = _split_nmcli_line(line)
            if len(parts) >= 3 and parts[0] == "yes" and (not dev or parts[2] == dev):
                return parts[1]
    return ""


def _select_wifi_iface():
    """Let the user pick a WiFi interface on LCD, or auto-select if only one."""
    if not LCD_OK or GPIO_MOD is None:
        return _get_wifi_device()
    try:
        from payloads._iface_helper import select_interface

        return select_interface(
            LCD, FONTS["body"], PINS, GPIO_MOD,
            iface_type="wifi", title="CONNECT IFACE",
        )
    except Exception:
        return _get_wifi_device()


def _discover_hosts(iface: str, local_ip: str):
    """Quick host discovery using arp-scan or nmap ping scan."""
    if not local_ip or local_ip.count(".") != 3:
        return []
    parts = local_ip.split(".")
    subnet = f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
    found_ips = []

    try:
        res = _run(["sudo", "arp-scan", "-l", "-I", iface, "-q"], timeout=15)
        if res.returncode == 0:
            for line in res.stdout.strip().splitlines():
                line = line.strip()
                if not line or line.startswith("Interface") or line.startswith("Starting"):
                    continue
                ip_part = line.split("\t")[0].split()[0] if "\t" in line else line.split()[0]
                if ip_part.count(".") == 3 and ip_part not in found_ips:
                    found_ips.append(ip_part)
            if found_ips:
                return found_ips
    except Exception:
        pass

    try:
        res = _run(["nmap", "-sn", subnet, "-T4", "--max-retries", "1"], timeout=20)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                line = line.strip()
                if "Nmap scan report for" in line:
                    if "(" in line:
                        ip_part = line.split("(")[1].rstrip(")")
                    else:
                        ip_part = line.split()[-1]
                    if ip_part.count(".") == 3 and ip_part not in found_ips:
                        found_ips.append(ip_part)
    except Exception:
        pass

    return found_ips


# ---------------------------------------------------------------------------
# Native Resolution-Adaptive Obsidian Ember Renderers
# ---------------------------------------------------------------------------

def _show_progress(title, sub="", progress=None, detail_lines=None, kind="info", dev=""):
    """Display an Obsidian Ember progress/status screen (works on LCD + stdout)."""
    out = [title]
    if sub:
        out.append(sub)
    if detail_lines:
        out.extend(detail_lines)
    print(" | ".join(out))

    if not LCD_OK or LCD is None:
        return
    try:
        f = FONTS or rj_theme.load_app_fonts(WIDTH, HEIGHT)
        wide = f["wide"]
        large = f["large"]
        mid = (not wide) and WIDTH >= 200
        img = Image.new("RGB", (WIDTH, HEIGHT), ET["bg_base"])
        d = ImageDraw.Draw(img)

        hdr_h = rj_theme.draw_app_header(
            d, WIDTH, "QUICK WIFI", f, badge=dev or "WIFI",
            hdr_h=(30 if large else (22 if wide else (22 if mid else 18))),
        )
        ftr_h = rj_theme.draw_app_footer(
            d, WIDTH, HEIGHT, "EmberJack Wireless", f,
            ftr_h=(22 if large else (16 if wide else (16 if mid else 13))),
        )

        pad = 14 if large else (6 if wide else (8 if mid else 4))
        cy0 = hdr_h + pad
        cy1 = HEIGHT - ftr_h - pad
        cw = min(WIDTH - pad * 2, 460 if large else WIDTH - pad * 2)
        cx0 = (WIDTH - cw) // 2
        cx1 = cx0 + cw

        accent = (
            ET["ok"] if kind == "ok"
            else ET["danger"] if kind == "err"
            else ET["warn"] if kind == "warn"
            else ET["accent"]
        )
        rj_theme.draw_card(
            d, (cx0, cy0, cx1, cy1),
            fill=ET["bg_surface"], outline=accent,
            radius=8 if wide else (6 if mid else 4), accent_left=accent, accent_width=4 if wide else 2,
        )

        tx = cx0 + (16 if wide else (10 if mid else 8))
        avail_w = max(40, cw - (28 if wide else (18 if mid else 14)))
        y = cy0 + (14 if large else (7 if wide else (10 if mid else 5)))

        d.text((tx, y), rj_theme.truncate_text(d, title, avail_w, f["title"]), font=f["title"], fill=ET["accent_bright"] if kind == "info" else accent)
        y += 24 if large else (15 if wide else (16 if mid else 12))

        if sub:
            for ln in rj_theme.wrap_text(d, sub, avail_w, f["body"])[:2]:
                if y + 12 > cy1 - 18:
                    break
                d.text((tx, y), ln, font=f["body"], fill=ET["text_primary"])
                y += 18 if large else (12 if wide else (13 if mid else 10))

        if detail_lines:
            y += 4 if (wide or mid) else 2
            step = 16 if large else (11 if wide else (12 if mid else 9))
            bar_reserve = (26 if large else (18 if (wide or mid) else 14)) if progress is not None else 6
            for ln in detail_lines:
                if y + step > cy1 - bar_reserve:
                    break
                d.text((tx, y), rj_theme.truncate_text(d, ln, avail_w, f["sub"]), font=f["sub"], fill=ET["text_muted"])
                y += step

        if progress is not None:
            bh = 12 if large else (8 if (wide or mid) else 6)
            by1 = cy1 - (14 if large else (8 if (wide or mid) else 5))
            by0 = by1 - bh
            rj_theme.draw_progress_bar(
                d, (tx, by0, cx1 - (14 if wide else (10 if mid else 6)), by1),
                fraction=progress, fill=accent, track=ET["bg_well"],
                outline=ET["border_subtle"], radius=4 if wide else 2,
            )

        LCD.LCD_ShowImage(img, 0, 0)
    except Exception:
        pass


def render_wifi_frame(
    width,
    height,
    dev,
    networks,
    saved_map,
    sel=0,
    scroll=0,
    active_ssid="",
    active_ip="",
    hosts=None,
    toast="",
    toast_kind="info",
    fonts=None,
):
    """Pure PIL native resolution-adaptive Obsidian Ember WiFi Connect renderer."""
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = f["wide"]
    large = f["large"]
    mid = (not wide) and width >= 200
    split = width >= 480
    hosts = hosts or []

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    hdr_right = f"{dev} • {active_ip}" if active_ip else (dev or "No iface")
    hdr_h = rj_theme.draw_app_header(
        d, width, "QUICK WIFI", f,
        badge=f"{len(networks)}",
        right_text=hdr_right,
        right_pill=("LINK UP" if active_ip else ""),
        hdr_h=(30 if large else (22 if wide else (22 if mid else 18))),
    )

    hint = format_footer_hint(
        width, height,
        hat_hint="OK:Con K1:Auto K2:Scn K3:X",
        hat_240_hint="OK:Conn K1:Auto K2:Scan K3:Exit",
        cardputer_hint="Ent:Conn A:Auto R:Scan P:Pass Esc:Exit",
        hdmi_hint="Enter:Connect  A:Auto-Saved  R:Rescan  P:Pass  H:Hosts  Esc:Exit",
    )
    pos_txt = f"{min(sel + 1, max(1, len(networks)))}/{max(1, len(networks))}"
    ftr_h = rj_theme.draw_app_footer(
        d, width, height, hint, f, right_text=pos_txt,
        ftr_h=(22 if large else (16 if wide else (16 if mid else 13))),
    )

    toast_h = (22 if large else (16 if (wide or mid) else 14)) if toast else 0
    y0 = hdr_h + (8 if large else (4 if (wide or mid) else 2))
    y1 = height - ftr_h - toast_h - (6 if large else (3 if (wide or mid) else 2))

    if not networks:
        empty_sub = format_footer_hint(
            width, height,
            hat_hint="K2: Rescan",
            hat_240_hint="Press K2 to rescan",
            cardputer_hint="Press R to rescan",
            hdmi_hint="Press R to rescan",
        )
        empty_foot = format_footer_hint(
            width, height,
            hat_hint="K3 to exit",
            hat_240_hint="K3 to exit",
            cardputer_hint="Esc to exit",
            hdmi_hint="Esc to exit",
        )
        rj_theme.draw_status_card(
            d, width, height,
            "No WiFi Networks Found",
            sub=empty_sub,
            kind="warn",
            foot=empty_foot,
            fonts=f,
        )
    else:
        pad = 10 if large else (6 if wide else (5 if mid else 3))
        sb_w = 5 if large else (4 if (wide or mid) else 2)
        list_x0 = pad
        list_x1 = int(width * 0.59) - 4 if split else (width - pad - sb_w - 3)

        row_h = 46 if large else (28 if wide else (26 if mid else 21))
        vis = max(1, (y1 - y0) // row_h)
        st = max(0, min(scroll, max(0, len(networks) - vis)))

        for slot in range(vis):
            idx = st + slot
            if idx >= len(networks):
                break
            net = networks[idx]
            is_sel = idx == sel
            is_saved = net["ssid"] in saved_map
            is_active = bool(net.get("active") or (active_ssid and net["ssid"] == active_ssid))

            ry0 = y0 + slot * row_h
            ry1 = ry0 + row_h - (5 if large else (3 if (wide or mid) else 2))
            rj_theme.draw_card(
                d, (list_x0, ry0, list_x1, ry1),
                fill=ET["select_bg"] if is_sel else ET["bg_surface"],
                outline=ET["border_accent"] if is_sel else ET["border_subtle"],
                radius=6 if wide else (4 if mid else 3),
                accent_left=ET["ok"] if is_active else (ET["accent"] if is_sel else None),
                accent_width=3 if (wide or mid) else 2,
            )

            # Right side: signal bars + % + security / saved pills
            rx = list_x1 - (8 if wide else (5 if mid else 4))
            bar_h = 14 if large else (10 if (wide or mid) else 8)
            bar_w = max(2, bar_h // 5) * 4 + max(1, max(2, bar_h // 5) // 2) * 3
            bar_x = rx - bar_w
            bar_y = ry0 + (6 if large else max(2, ((ry1 - ry0) - bar_h) // 2))
            sig = int(net.get("signal", 0))
            sig_col = ET["ok"] if sig >= 65 else (ET["accent_bright"] if sig >= 35 else ET["danger"])
            rj_theme.draw_signal_bars(d, bar_x, bar_y, bar_h, sig, active_color=sig_col, inactive_color=ET["border_subtle"])
            rx = bar_x - (6 if (wide or mid) else 3)

            if wide or mid:
                sig_txt = f"{sig}%"
                sb = rj_theme.measure_text(f["badge"], sig_txt, d)
                sw = sb[2] - sb[0]
                d.text((rx - sw, bar_y + (bar_h - (sb[3] - sb[1])) // 2 - sb[1]), sig_txt, font=f["badge"], fill=sig_col)
                rx -= sw + (6 if wide else 4)

            # Security pill (top row on wide, compact pill on small)
            sec_tag = net.get("sec_tag", "OPEN")
            sec_bg = ET["ok_bg"] if sec_tag == "OPEN" else ET["bg_well"]
            sec_fg = ET["ok"] if sec_tag == "OPEN" else ET["text_muted"]
            if wide:
                sbb = rj_theme.measure_text(f["badge"], sec_tag, d)
                spw = (sbb[2] - sbb[0]) + 8
                if rx - spw > list_x0 + 95:
                    rj_theme.draw_pill(
                        d, rx - spw, ry0 + (5 if large else 2),
                        sec_tag, f["badge"], bg=sec_bg, fg=sec_fg,
                        outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3,
                    )
                    rx -= spw + 4

            # Full SSID text (never hardcoded [:16]!)
            nx = list_x0 + (10 if wide else (7 if mid else 5))
            ssid_avail = max(36, rx - nx - 4)
            ssid_txt = rj_theme.truncate_text(d, net["ssid"], ssid_avail, f["body_bold"] if (wide or mid) else f["body"])
            d.text(
                (nx, ry0 + (5 if large else 2)),
                ssid_txt,
                font=f["body_bold"] if (wide or mid) else f["body"],
                fill=ET["text_bright"] if is_sel else ET["text_primary"],
            )

            # Second row on large (or compact badge on 320px/240px/128px)
            if large:
                sub_x = nx
                sub_y = ry0 + 24
                if is_active:
                    pw, _ = rj_theme.draw_pill(
                        d, sub_x, sub_y, "CONNECTED", f["badge"],
                        bg=ET["ok_bg"], fg=ET["ok"], outline=None, pad_x=5, pad_y=1, radius=3,
                    )
                    sub_x += pw + 5
                if is_saved:
                    pw, _ = rj_theme.draw_pill(
                        d, sub_x, sub_y, "SAVED", f["badge"],
                        bg=ET["select_bg"], fg=ET["accent_bright"], outline=ET["border_accent"],
                        pad_x=5, pad_y=1, radius=3,
                    )
                    sub_x += pw + 6
                sec_full = net.get("security") or "Open network"
                d.text(
                    (sub_x, sub_y + 1),
                    rj_theme.truncate_text(d, sec_full, max(30, list_x1 - sub_x - 8), f["sub"]),
                    font=f["sub"], fill=ET["text_muted"],
                )
            elif wide or mid:
                tag_x = nx
                tag_y = ry0 + (14 if wide else 13)
                if is_active:
                    d.text((tag_x, tag_y), "CONNECTED", font=f["badge"], fill=ET["ok"])
                elif is_saved:
                    d.text((tag_x, tag_y), f"SAVED • {sec_tag}" if mid else "SAVED", font=f["badge"], fill=ET["accent_bright"])
                else:
                    d.text((tag_x, tag_y), sec_tag, font=f["badge"], fill=ET["text_dim"])
            else:
                if is_active or is_saved:
                    tag_lbl = "ACT" if is_active else "SAV"
                    tag_col = ET["ok"] if is_active else ET["accent_bright"]
                    d.text((nx, ry0 + 11), f"{tag_lbl} {sec_tag}", font=f["badge"], fill=tag_col)

        if len(networks) > vis:
            rj_theme.draw_scrollbar(d, list_x1 + 3, y0, y1, st, vis, len(networks), width=sb_w)

        # Right split-pane inspector on widescreen
        if split and 0 <= sel < len(networks):
            cur = networks[sel]
            px0 = list_x1 + 12
            px1 = width - pad
            pw = px1 - px0 - 20

            # Top inspector card (Selected Network)
            top_h = int((y1 - y0) * 0.56)
            rj_theme.draw_card(d, (px0, y0, px1, y0 + top_h), fill=ET["bg_surface"], outline=ET["border_subtle"], radius=7)
            dy = y0 + 10
            for ln in rj_theme.wrap_text(d, cur["ssid"], pw, f["title"])[:2]:
                d.text((px0 + 10, dy), ln, font=f["title"], fill=ET["accent_bright"])
                dy += 18
            dy += 4
            d.line([(px0 + 10, dy), (px1 - 10, dy)], fill=ET["border_subtle"], width=1)
            dy += 8

            sig = int(cur.get("signal", 0))
            sig_col = ET["ok"] if sig >= 65 else (ET["accent_bright"] if sig >= 35 else ET["danger"])
            d.text((px0 + 10, dy), f"Signal: {sig}%", font=f["sub"], fill=ET["text_muted"])
            rj_theme.draw_progress_bar(
                d, (px0 + 82, dy + 2, px1 - 10, dy + 11),
                fraction=sig / 100.0, fill=sig_col, track=ET["bg_well"], radius=3,
            )
            dy += 20

            sec_desc = cur.get("security") or "Open (No password)"
            is_saved = cur["ssid"] in saved_map
            rows = [
                ("Security", sec_desc),
                ("Profile", "Saved (Auto-auth)" if is_saved else "Password via Keyboard"),
                ("Adapter", dev or "auto"),
            ]
            for k_lbl, v_lbl in rows:
                if dy + 16 > y0 + top_h - 6:
                    break
                d.text((px0 + 10, dy), f"{k_lbl}:", font=f["sub"], fill=ET["text_muted"])
                d.text((px0 + 68, dy), rj_theme.truncate_text(d, v_lbl, max(30, pw - 58), f["body"]), font=f["body"], fill=ET["text_primary"])
                dy += 19

            # Bottom card (Active Link & Discovered Subnet Hosts)
            bot_y0 = y0 + top_h + 8
            rj_theme.draw_card(d, (px0, bot_y0, px1, y1), fill=ET["bg_surface"], outline=ET["border_subtle"], radius=7)
            by = bot_y0 + 8
            hdr_lbl = f"SUBNET HOSTS ({len(hosts)})" if hosts else "ACTIVE LINK"
            d.text((px0 + 10, by), hdr_lbl, font=f["badge"], fill=ET["accent_bright"])
            by += 16
            if active_ssid or active_ip:
                link_s = f"{active_ssid or dev}  •  {active_ip or 'no IP'}"
                d.text((px0 + 10, by), rj_theme.truncate_text(d, link_s, pw, f["sub"]), font=f["sub"], fill=ET["ok"])
                by += 16
            else:
                d.text((px0 + 10, by), "Not connected yet", font=f["sub"], fill=ET["text_dim"])
                by += 16

            if hosts:
                for hip in hosts:
                    if by + 13 > y1 - 6:
                        break
                    d.text((px0 + 12, by), f"• {hip}", font=f["mono"], fill=ET["text_primary"])
                    by += 14
            else:
                d.text((px0 + 10, by), "Press Enter to connect or H to scan hosts", font=f["sub"], fill=ET["text_muted"])

    # Toast banner
    if toast:
        ty0 = height - ftr_h - toast_h
        ty1 = height - ftr_h - 1
        t_bg = ET["ok_bg"] if toast_kind == "ok" else (ET["danger_bg"] if toast_kind == "err" else ET["select_bg"])
        t_fg = ET["ok"] if toast_kind == "ok" else (ET["danger"] if toast_kind == "err" else ET["accent_bright"])
        d.rectangle((0, ty0, width - 1, ty1), fill=t_bg)
        d.line([(0, ty0), (width - 1, ty0)], fill=ET["border_subtle"], width=1)
        d.text(
            (8 if wide else 3, ty0 + max(1, (toast_h - 12) // 2)),
            rj_theme.truncate_text(d, toast, width - 16, f["sub"]),
            font=f["sub"], fill=t_fg,
        )

    return img


def _connect_and_discover(dev, net, saved_map, force_prompt=False):
    """Connect to *net*, prompting with lcd_keyboard when password is required."""
    ssid = net["ssid"]
    sec_tag = net.get("sec_tag", _format_security_tag(net.get("security", "")))
    is_open = sec_tag == "OPEN"
    password = saved_map.get(ssid)

    if force_prompt or (not is_open and ssid not in saved_map):
        if LCD_OK and GPIO_MOD is not None:
            entered = lcd_keyboard(
                LCD, FONTS["body"], PINS, GPIO_MOD,
                title=f"PASS: {ssid}",
                default=password or "",
                charset="full",
                max_len=64,
            )
            if entered is None:
                return False, "Cancelled", "", []
            password = entered
        else:
            return False, "Password required", "", []

    _show_progress(
        f"Connecting to {ssid}",
        sub=f"Adapter: {dev}  •  Signal: {net['signal']}%  •  {sec_tag}",
        progress=0.45,
        dev=dev,
    )

    cmd = ["nmcli", "dev", "wifi", "connect", ssid, "ifname", dev]
    if password:
        cmd += ["password", password]
    res = _run(cmd, timeout=25)

    # If a saved profile failed, offer one-shot password prompt via lcd_keyboard
    if res.returncode != 0 and not force_prompt and not is_open and LCD_OK and GPIO_MOD is not None:
        entered = lcd_keyboard(
            LCD, FONTS["body"], PINS, GPIO_MOD,
            title=f"RETRY PASS: {ssid}",
            default=password or "",
            charset="full",
            max_len=64,
        )
        if entered:
            password = entered
            _show_progress(f"Retrying {ssid}", sub=f"Adapter: {dev}", progress=0.55, dev=dev)
            res = _run(["nmcli", "dev", "wifi", "connect", ssid, "ifname", dev, "password", password], timeout=25)

    if res.returncode != 0:
        err = (res.stderr or res.stdout or "Connect failed").strip().splitlines()[0] if (res.stderr or res.stdout) else "Connect failed"
        _show_progress("Connection Failed", sub=ssid, detail_lines=[err], progress=1.0, kind="err", dev=dev)
        time.sleep(1.4)
        return False, err, "", []

    if password:
        _save_wifi_profile(ssid, password, net.get("security", ""))
        saved_map[ssid] = password

    _show_progress(f"Connected: {ssid}", sub=f"Interface: {dev} • Obtaining IPv4...", progress=0.75, kind="ok", dev=dev)
    time.sleep(0.6)
    ip = _get_iface_ip(dev) or "pending"

    _show_progress(f"Connected: {ssid}", sub=f"IP: {ip} • Scanning subnet hosts...", progress=0.90, kind="ok", dev=dev)
    hosts = _discover_hosts(dev, ip) if ip != "pending" else []

    host_lines = [f"IPv4: {ip}", f"Subnet hosts found: {len(hosts)}"]
    for h_ip in hosts[:6]:
        host_lines.append(f"  • {h_ip}")
    if len(hosts) > 6:
        host_lines.append(f"  +{len(hosts) - 6} more")
    _show_progress(f"Connected: {ssid}", sub=f"Adapter: {dev}", detail_lines=host_lines, progress=1.0, kind="ok", dev=dev)
    time.sleep(1.5)
    return True, f"Connected ({ip}) • {len(hosts)} hosts", ip, hosts


def _sort_networks(networks, saved_map, active_ssid=""):
    return sorted(
        networks,
        key=lambda n: (
            1 if (n.get("active") or (active_ssid and n["ssid"] == active_ssid)) else 0,
            1 if n["ssid"] in saved_map else 0,
            n.get("signal", 0),
        ),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _running
    _init_lcd()

    try:
        dev = _select_wifi_iface()
        if not dev:
            _show_progress("No WiFi Interface", sub="No adapter selected or found", progress=1.0, kind="err")
            time.sleep(1.0)
            return 1

        _show_progress("Quick WiFi Scan", sub=f"Using {dev} • Scanning nearby networks...", progress=0.20, dev=dev)
        saved = _get_saved_wifi()
        _run(["nmcli", "device", "wifi", "rescan", "ifname", dev], timeout=12)
        time.sleep(1.2)
        active_ssid = _get_active_ssid(dev)
        active_ip = _get_iface_ip(dev)
        networks = _sort_networks(_scan_wifi(dev=dev, rescan=False), saved, active_ssid)

        # Headless CLI fallback: auto-connect strongest saved network
        if not LCD_OK or GPIO_MOD is None:
            candidates = [n for n in networks if n["ssid"] in saved]
            if not candidates:
                _show_progress("No Saved WiFi in Range", sub="Use WiFi Manager to add profiles", progress=1.0, kind="warn", dev=dev)
                return 1
            best = max(candidates, key=lambda x: x["signal"])
            ok, _msg, _ip, _hosts = _connect_and_discover(dev, best, saved)
            return 0 if ok else 1

        sel = 0
        scroll = 0
        hosts = []
        toast = f"Found {len(networks)} networks ({sum(1 for n in networks if n['ssid'] in saved)} saved)"
        toast_kind = "info"
        toast_until = time.monotonic() + 4.0
        dirty = True

        with text_input_mode():
            flush_input()
            while _running:
                now_m = time.monotonic()
                mid_ui = (not FONTS["wide"]) and WIDTH >= 200
                row_h = 46 if FONTS["large"] else (28 if FONTS["wide"] else (26 if mid_ui else 21))
                hdr_h = 30 if FONTS["large"] else (22 if FONTS["wide"] else (22 if mid_ui else 18))
                ftr_h = 22 if FONTS["large"] else (16 if FONTS["wide"] else (16 if mid_ui else 13))
                vis = max(1, (HEIGHT - hdr_h - ftr_h - 14) // row_h)

                if networks:
                    sel = max(0, min(sel, len(networks) - 1))
                    if sel < scroll:
                        scroll = sel
                    elif sel >= scroll + vis:
                        scroll = sel - vis + 1
                else:
                    sel = 0
                    scroll = 0

                active_toast = toast if (toast and now_m < toast_until) else ""
                if toast and not active_toast:
                    toast = ""
                    dirty = True

                if dirty:
                    frame = render_wifi_frame(
                        WIDTH, HEIGHT, dev, networks, saved,
                        sel=sel, scroll=scroll,
                        active_ssid=active_ssid, active_ip=active_ip,
                        hosts=hosts, toast=active_toast, toast_kind=toast_kind,
                        fonts=FONTS,
                    )
                    LCD.LCD_ShowImage(frame, 0, 0)
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO_MOD)
                if act is None and ch is None:
                    time.sleep(0.03)
                    continue

                kl = ch.lower() if isinstance(ch, str) else ""
                if is_back(act):
                    break
                elif act == "UP" and networks:
                    sel = (sel - 1) % len(networks)
                    dirty = True
                elif act == "DOWN" and networks:
                    sel = (sel + 1) % len(networks)
                    dirty = True
                elif act == "PAGEUP" and networks:
                    sel = max(0, sel - vis)
                    dirty = True
                elif act == "PAGEDOWN" and networks:
                    sel = min(len(networks) - 1, sel + vis)
                    dirty = True
                elif act == "HOME" and networks:
                    sel = 0
                    dirty = True
                elif act == "END" and networks:
                    sel = len(networks) - 1
                    dirty = True
                elif is_confirm(act) and networks:
                    ok, msg, new_ip, new_hosts = _connect_and_discover(dev, networks[sel], saved, force_prompt=False)
                    if ok:
                        active_ssid = networks[sel]["ssid"]
                        active_ip = new_ip
                        hosts = new_hosts
                        for n in networks:
                            n["active"] = (n["ssid"] == active_ssid)
                        networks = _sort_networks(networks, saved, active_ssid)
                        sel = 0
                        scroll = 0
                    toast, toast_kind, toast_until = msg, ("ok" if ok else "err"), time.monotonic() + 5.0
                    flush_input()
                    dirty = True
                elif kl == "p" and networks:
                    ok, msg, new_ip, new_hosts = _connect_and_discover(dev, networks[sel], saved, force_prompt=True)
                    if ok:
                        active_ssid = networks[sel]["ssid"]
                        active_ip = new_ip
                        hosts = new_hosts
                        for n in networks:
                            n["active"] = (n["ssid"] == active_ssid)
                        networks = _sort_networks(networks, saved, active_ssid)
                        sel = 0
                        scroll = 0
                    toast, toast_kind, toast_until = msg, ("ok" if ok else "err"), time.monotonic() + 5.0
                    flush_input()
                    dirty = True
                elif is_action(act, ch, "KEY1", "a"):
                    candidates = [n for n in networks if n["ssid"] in saved]
                    if not candidates:
                        toast, toast_kind, toast_until = "No saved WiFi in range — press OK/Enter on any SSID", "warn", time.monotonic() + 4.0
                    else:
                        best = max(candidates, key=lambda x: x["signal"])
                        ok, msg, new_ip, new_hosts = _connect_and_discover(dev, best, saved, force_prompt=False)
                        if ok:
                            active_ssid = best["ssid"]
                            active_ip = new_ip
                            hosts = new_hosts
                            for n in networks:
                                n["active"] = (n["ssid"] == active_ssid)
                            networks = _sort_networks(networks, saved, active_ssid)
                            sel = 0
                            scroll = 0
                        toast, toast_kind, toast_until = msg, ("ok" if ok else "err"), time.monotonic() + 5.0
                    flush_input()
                    dirty = True
                elif is_action(act, ch, "KEY2", "r"):
                    _show_progress("Rescanning WiFi...", sub=f"Adapter: {dev}", progress=0.35, dev=dev)
                    saved = _get_saved_wifi()
                    active_ssid = _get_active_ssid(dev)
                    active_ip = _get_iface_ip(dev)
                    networks = _sort_networks(_scan_wifi(dev=dev, rescan=True), saved, active_ssid)
                    sel = 0
                    scroll = 0
                    toast, toast_kind, toast_until = f"Scanned {len(networks)} networks", "info", time.monotonic() + 3.5
                    flush_input()
                    dirty = True
                elif kl == "h":
                    active_ip = _get_iface_ip(dev)
                    if active_ip:
                        _show_progress("Scanning Subnet Hosts...", sub=f"{dev} ({active_ip})", progress=0.6, dev=dev)
                        hosts = _discover_hosts(dev, active_ip)
                        toast, toast_kind, toast_until = f"Found {len(hosts)} host(s) on subnet", "ok", time.monotonic() + 4.0
                    else:
                        toast, toast_kind, toast_until = "Not connected — connect first", "warn", time.monotonic() + 3.0
                    flush_input()
                    dirty = True
    finally:
        _running = False
        if LCD_OK and LCD is not None:
            try:
                LCD.LCD_Clear()
            except Exception:
                pass
        if GPIO_MOD is not None:
            try:
                GPIO_MOD.cleanup()
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
