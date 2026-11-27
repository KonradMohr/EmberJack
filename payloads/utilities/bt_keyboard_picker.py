#!/usr/bin/env python3
"""
EmberJack Payload -- Bluetooth Keyboard Picker
==============================================
Interactive Obsidian Ember helper to scan, pair, trust, and connect
a Bluetooth HID keyboard (or any Bluetooth HID device) without touching
the shell.

Controls:
  UP / DOWN / PgUp / PgDn  -- Navigate discovered devices
  ENTER / OK               -- Pair, trust & connect selected device
  KEY1 / S                 -- Rescan nearby Bluetooth devices
  KEY3 / ESC               -- Stop scan / Back / Exit
"""

import os
import re
import signal
import subprocess
import sys
import time
from select import select
from typing import Dict, List, Tuple

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import LCD_Config
from PIL import Image, ImageDraw

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

ET = rj_theme.EMBER_THEME

PINS: Dict[str, int] = {
    "UP": 6,
    "DOWN": 19,
    "LEFT": 5,
    "RIGHT": 26,
    "OK": 13,
    "KEY1": 21,
    "KEY2": 20,
    "KEY3": 16,
}

SCAN_SECONDS = 10
running = True


def cleanup(*_):
    global running
    running = False


signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)


# ---------------------------------------------------------------------------
# Bluetooth helpers
# ---------------------------------------------------------------------------

def _query_device_info(mac: str) -> dict:
    """Query paired/connected/trusted/HID flags from bluetoothctl info."""
    meta = {"paired": False, "connected": False, "trusted": False, "is_hid": False, "icon": ""}
    try:
        res = subprocess.run(
            ["bluetoothctl", "info", mac],
            capture_output=True, text=True, timeout=3,
        )
        out = res.stdout or ""
        meta["paired"] = "Paired: yes" in out
        meta["connected"] = "Connected: yes" in out
        meta["trusted"] = "Trusted: yes" in out
        low = out.lower()
        if "input-keyboard" in low or "human interface" in low or "00001124-" in low or "00001812-" in low:
            meta["is_hid"] = True
        for ln in out.splitlines():
            if "Icon:" in ln:
                meta["icon"] = ln.split("Icon:", 1)[1].strip()
                break
    except Exception:
        pass
    return meta


def _is_keyboard_name(name: str) -> bool:
    low = (name or "").lower()
    return any(tok in low for tok in ("keyb", "kbd", "hid", "k380", "k480", "mx keys", "magic", "hhkb", "anne", "keychron"))


def _check_cancel_input() -> bool:
    """Poll input non-blockingly so Esc / KEY3 cancels long BT operations."""
    global running
    if not running:
        return True
    act, _ch = poll_ui_input(PINS, GPIO)
    if is_back(act):
        return True
    return False


# ---------------------------------------------------------------------------
# Obsidian Ember Adaptive Renderers
# ---------------------------------------------------------------------------

def render_scan_frame(width, height, elapsed, total, found_count, last_seen="", fonts=None):
    """Render Obsidian Ember Bluetooth scanning progress screen."""
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = f["wide"]
    large = f["large"]
    mid = (not wide) and width >= 200
    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    hdr_h = rj_theme.draw_app_header(
        d, width, "BT KEYBOARD", f, badge="SCAN", right_pill=f"{found_count} FOUND",
        hdr_h=(30 if large else (22 if wide else (22 if mid else 18))),
    )
    scan_hint = format_footer_hint(
        width, height,
        hat_hint="K3: Stop",
        hat_240_hint="K3: Stop scan",
        cardputer_hint="Esc: Stop scan early",
        hdmi_hint="Esc: Stop scan early",
    )
    ftr_h = rj_theme.draw_app_footer(
        d, width, height, scan_hint, f,
        ftr_h=(22 if large else (16 if wide else (16 if mid else 13))),
    )

    pad = 14 if large else (6 if wide else (8 if mid else 4))
    cw = min(width - pad * 2, 460 if large else width - pad * 2)
    ch = min(height - hdr_h - ftr_h - pad * 2, 168 if large else (100 if wide else (112 if mid else 78)))
    cx0 = (width - cw) // 2
    cy0 = hdr_h + max(pad, (height - hdr_h - ftr_h - ch) // 2)
    cx1, cy1 = cx0 + cw, cy0 + ch

    rj_theme.draw_card(
        d, (cx0, cy0, cx1, cy1),
        fill=ET["bg_surface"], outline=ET["border_accent"],
        radius=8 if wide else (6 if mid else 4), accent_left=ET["accent"], accent_width=4 if wide else 2,
    )

    tx = cx0 + (16 if wide else (10 if mid else 8))
    avail = max(40, cw - (28 if wide else (18 if mid else 14)))
    y = cy0 + (14 if large else (7 if wide else (10 if mid else 5)))

    rem = max(0, int(round(total - elapsed)))
    d.text((tx, y), rj_theme.truncate_text(d, "Scanning Bluetooth Devices...", avail, f["title"]), font=f["title"], fill=ET["accent_bright"])
    y += 24 if large else (15 if wide else (16 if mid else 12))
    d.text((tx, y), rj_theme.truncate_text(d, f"Found {found_count} device(s) • {rem}s left", avail, f["body"]), font=f["body"], fill=ET["text_primary"])
    y += 20 if large else (13 if wide else (14 if mid else 10))
    if last_seen and y + 12 < cy1 - 18:
        d.text((tx, y), rj_theme.truncate_text(d, f"Latest: {last_seen}", avail, f["sub"]), font=f["sub"], fill=ET["text_muted"])

    bh = 12 if large else (8 if (wide or mid) else 6)
    by1 = cy1 - (16 if large else (9 if wide else (12 if mid else 6)))
    rj_theme.draw_progress_bar(
        d, (tx, by1 - bh, cx1 - (14 if wide else (10 if mid else 6)), by1),
        fraction=min(1.0, elapsed / max(1.0, total)),
        fill=ET["accent"], track=ET["bg_well"], outline=ET["border_subtle"], radius=4 if wide else 2,
    )
    return img


def render_pairing_frame(width, height, name, mac, stage, passkey="", progress=0.3, fonts=None):
    """Render Obsidian Ember pairing/trusting/connecting & passkey screen."""
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = f["wide"]
    large = f["large"]
    mid = (not wide) and width >= 200
    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    hdr_h = rj_theme.draw_app_header(
        d, width, "BT KEYBOARD", f, badge="PAIRING", right_text=mac,
        hdr_h=(30 if large else (22 if wide else (22 if mid else 18))),
    )
    pair_hint = format_footer_hint(
        width, height,
        hat_hint="K3: Cancel",
        hat_240_hint="K3: Cancel pairing",
        cardputer_hint="Esc: Cancel pairing",
        hdmi_hint="Esc: Cancel pairing",
    )
    ftr_h = rj_theme.draw_app_footer(
        d, width, height, pair_hint, f,
        ftr_h=(22 if large else (16 if wide else (16 if mid else 13))),
    )

    pad = 14 if large else (6 if wide else (8 if mid else 4))
    cw = min(width - pad * 2, 480 if large else width - pad * 2)
    ch = min(height - hdr_h - ftr_h - pad * 2, 196 if large else (106 if wide else (124 if mid else 84)))
    cx0 = (width - cw) // 2
    cy0 = hdr_h + max(pad, (height - hdr_h - ftr_h - ch) // 2)
    cx1, cy1 = cx0 + cw, cy0 + ch

    outline = ET["warn"] if passkey else ET["border_accent"]
    rj_theme.draw_card(
        d, (cx0, cy0, cx1, cy1),
        fill=ET["bg_surface"], outline=outline,
        radius=8 if wide else (6 if mid else 4), accent_left=outline, accent_width=4 if wide else 2,
    )

    tx = cx0 + (16 if wide else (10 if mid else 8))
    avail = max(40, cw - (28 if wide else (18 if mid else 14)))
    y = cy0 + (14 if large else (6 if wide else (10 if mid else 5)))

    d.text((tx, y), rj_theme.truncate_text(d, stage, avail, f["title"]), font=f["title"], fill=ET["warn"] if passkey else ET["accent_bright"])
    y += 22 if large else (14 if wide else (15 if mid else 11))
    d.text((tx, y), rj_theme.truncate_text(d, f"{name} ({mac})", avail, f["sub"]), font=f["sub"], fill=ET["text_muted"])
    y += 18 if large else (12 if wide else (14 if mid else 10))

    if passkey:
        box_h = 54 if large else (30 if wide else (32 if mid else 22))
        box_h = min(box_h, max(18, cy1 - y - 16))
        rj_theme.draw_card(
            d, (tx, y, cx1 - (14 if wide else (10 if mid else 6)), y + box_h),
            fill=ET["bg_well"], outline=ET["accent"], radius=6 if wide else 3,
        )
        pin_font = f["title"]
        try:
            import rj_env
            pin_font = rj_env.load_font(26 if large else (18 if wide else (16 if mid else 12)), bold=True, mono=True)
        except Exception:
            pass
        spaced = " ".join(list(passkey)) if len(passkey) <= 8 else passkey
        d.text(((tx + cx1 - (14 if wide else (10 if mid else 6))) // 2, y + box_h // 2), spaced, font=pin_font, fill=ET["accent_bright"], anchor="mm")
        y += box_h + (8 if large else 4)
        if y + 8 <= cy1 - 2:
            d.text((tx, y), rj_theme.truncate_text(d, "Type PIN on Bluetooth keyboard & press Enter", avail, f["sub"]), font=f["sub"], fill=ET["text_primary"])
    else:
        bh = 10 if large else (7 if (wide or mid) else 5)
        by1 = cy1 - (16 if large else (9 if wide else (12 if mid else 6)))
        rj_theme.draw_progress_bar(
            d, (tx, by1 - bh, cx1 - (14 if wide else (10 if mid else 6)), by1),
            fraction=progress, fill=ET["accent"], track=ET["bg_well"],
            outline=ET["border_subtle"], radius=4 if wide else 2,
        )
    return img


def render_devices_frame(width, height, devices, sel=0, scroll=0, toast="", toast_kind="info", fonts=None):
    """Render Obsidian Ember Bluetooth keyboard device list (split-pane on widescreen)."""
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = f["wide"]
    large = f["large"]
    mid = (not wide) and width >= 200
    split = width >= 480

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    hdr_h = rj_theme.draw_app_header(
        d, width, "BT KEYBOARD", f,
        badge=f"{len(devices)}",
        right_text="HID / KEYBOARD PICKER",
        hdr_h=(30 if large else (22 if wide else (22 if mid else 18))),
    )
    hint = format_footer_hint(
        width, height,
        hat_hint="OK:Pair K1:Scn K3:X",
        hat_240_hint="OK:Pair K1:Scan K3:Exit",
        cardputer_hint="Ent:Pair S:Scan Esc:Exit",
        hdmi_hint="Up/Down:Pick  Enter:Pair & Connect  S:Rescan  Esc:Exit",
    )
    pos_txt = f"{min(sel + 1, max(1, len(devices)))}/{max(1, len(devices))}"
    ftr_h = rj_theme.draw_app_footer(
        d, width, height, hint, f, right_text=pos_txt,
        ftr_h=(22 if large else (16 if wide else (16 if mid else 13))),
    )

    toast_h = (22 if large else (16 if (wide or mid) else 14)) if toast else 0
    y0 = hdr_h + (8 if large else (4 if (wide or mid) else 2))
    y1 = height - ftr_h - toast_h - (6 if large else (3 if (wide or mid) else 2))

    if not devices:
        empty_sub = format_footer_hint(
            width, height,
            hat_hint="K1: Rescan",
            hat_240_hint="Pairing mode -> press K1",
            cardputer_hint="Pairing mode -> press S",
            hdmi_hint="Put keyboard in pairing mode & press S",
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
            "No Bluetooth Devices Found",
            sub=empty_sub,
            kind="warn", foot=empty_foot, fonts=f,
        )
    else:
        pad = 10 if large else (6 if wide else (5 if mid else 3))
        sb_w = 5 if large else (4 if (wide or mid) else 2)
        list_x0 = pad
        list_x1 = int(width * 0.60) - 4 if split else (width - pad - sb_w - 3)
        row_h = 46 if large else (28 if wide else (26 if mid else 21))
        vis = max(1, (y1 - y0) // row_h)
        st = max(0, min(scroll, max(0, len(devices) - vis)))

        for slot in range(vis):
            idx = st + slot
            if idx >= len(devices):
                break
            dev = devices[idx]
            is_sel = idx == sel
            ry0 = y0 + slot * row_h
            ry1 = ry0 + row_h - (5 if large else (3 if (wide or mid) else 2))

            accent = ET["ok"] if dev.get("connected") else (ET["accent"] if is_sel else None)
            rj_theme.draw_card(
                d, (list_x0, ry0, list_x1, ry1),
                fill=ET["select_bg"] if is_sel else ET["bg_surface"],
                outline=ET["border_accent"] if is_sel else ET["border_subtle"],
                radius=6 if wide else (4 if mid else 3),
                accent_left=accent, accent_width=3 if (wide or mid) else 2,
            )

            # Status pills (right aligned)
            pills = []
            if dev.get("connected"):
                pills.append(("CONN", ET["ok_bg"], ET["ok"], None))
            elif dev.get("paired"):
                pills.append(("PAIRED", ET["select_bg"], ET["accent_bright"], ET["border_accent"]))
            else:
                pills.append(("NEW", ET["bg_well"], ET["text_muted"], ET["border_subtle"]))

            if dev.get("is_hid"):
                pills.append(("HID", ET["warn_bg"], ET["warn"], None))

            rx = list_x1 - (8 if wide else (5 if mid else 3))
            for plbl, pbg, pfg, pout in pills:
                pbb = rj_theme.measure_text(f["badge"], plbl, d)
                ppw = (pbb[2] - pbb[0]) + (10 if wide else (8 if mid else 6))
                if rx - ppw < list_x0 + 60:
                    break
                rj_theme.draw_pill(
                    d, rx - ppw, ry0 + (6 if large else (3 if (wide or mid) else 2)),
                    plbl, f["badge"], bg=pbg, fg=pfg, outline=pout,
                    pad_x=5 if wide else (4 if mid else 3), pad_y=1, radius=3,
                )
                rx -= ppw + 4

            nx = list_x0 + (10 if wide else (7 if mid else 5))
            name_txt = rj_theme.truncate_text(d, dev["name"], max(36, rx - nx - 4), f["body_bold"] if (wide or mid) else f["body"])
            d.text(
                (nx, ry0 + (5 if large else (2 if (wide or mid) else 2))),
                name_txt,
                font=f["body_bold"] if (wide or mid) else f["body"],
                fill=ET["text_bright"] if is_sel else ET["text_primary"],
            )
            if large:
                d.text((nx, ry0 + 25), dev["mac"], font=f["mono"], fill=ET["text_muted"])
            elif wide:
                d.text((nx, ry0 + 14), dev["mac"], font=f["sub"], fill=ET["text_muted"])
            elif mid:
                d.text((nx, ry0 + 13), dev["mac"], font=f["sub"], fill=ET["text_muted"])
            else:
                d.text((nx, ry0 + 11), dev["mac"][-8:], font=f["badge"], fill=ET["text_dim"])

        if len(devices) > vis:
            rj_theme.draw_scrollbar(d, list_x1 + 3, y0, y1, st, vis, len(devices), width=sb_w)

        if split and 0 <= sel < len(devices):
            cur = devices[sel]
            px0 = list_x1 + 12
            px1 = width - pad
            pw = px1 - px0 - 20
            rj_theme.draw_card(d, (px0, y0, px1, y1), fill=ET["bg_surface"], outline=ET["border_subtle"], radius=7)
            dy = y0 + 12
            for ln in rj_theme.wrap_text(d, cur["name"], pw, f["title"])[:2]:
                d.text((px0 + 10, dy), ln, font=f["title"], fill=ET["accent_bright"])
                dy += 19
            dy += 4
            d.line([(px0 + 10, dy), (px1 - 10, dy)], fill=ET["border_subtle"], width=1)
            dy += 10
            rows = [
                ("MAC", cur["mac"], f["mono"]),
                ("Type", "HID / Keyboard" if cur.get("is_hid") else (cur.get("icon") or "Bluetooth"), f["body"]),
                ("Status", "Connected" if cur.get("connected") else ("Paired" if cur.get("paired") else "Unpaired"), f["body"]),
                ("Trusted", "Yes" if cur.get("trusted") else "No", f["body"]),
            ]
            for k_lbl, v_lbl, v_font in rows:
                if dy + 18 > y1 - 56:
                    break
                d.text((px0 + 10, dy), f"{k_lbl}:", font=f["sub"], fill=ET["text_muted"])
                d.text((px0 + 66, dy), rj_theme.truncate_text(d, v_lbl, max(36, pw - 56), v_font), font=v_font, fill=ET["text_primary"])
                dy += 22

            guide_y = max(dy + 6, y1 - 54)
            d.line([(px0 + 10, guide_y), (px1 - 10, guide_y)], fill=ET["border_subtle"], width=1)
            for idx_g, gln in enumerate(rj_theme.wrap_text(d, "Press Enter to pair, trust & auto-connect. If prompted, type PIN on keyboard.", pw, f["sub"])[:2]):
                d.text((px0 + 10, guide_y + 8 + idx_g * 15), gln, font=f["sub"], fill=ET["text_muted"])

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
# Bluetooth scan & pair workflows
# ---------------------------------------------------------------------------

def discover_devices(lcd, fonts) -> List[dict]:
    """Scan for nearby Bluetooth devices with live Obsidian Ember progress UI."""
    W, H = lcd.width, lcd.height
    seen: Dict[str, str] = {}

    # Include already-known paired devices first
    try:
        known = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True, timeout=4)
        for ln in (known.stdout or "").splitlines():
            m = re.search(r"Device ([0-9A-F:]{17}) (.+)", ln)
            if m:
                seen[m.group(1)] = m.group(2).strip()
    except Exception:
        pass

    proc = subprocess.Popen(
        ["bluetoothctl"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdin and proc.stdout
    try:
        proc.stdin.write("power on\nscan on\n")
        proc.stdin.flush()
    except Exception:
        pass

    start = time.time()
    last_seen = ""
    last_draw = 0.0
    try:
        while running and (time.time() - start) < SCAN_SECONDS:
            now = time.time()
            if (now - last_draw) >= 0.25:
                frame = render_scan_frame(W, H, now - start, SCAN_SECONDS, len(seen), last_seen=last_seen, fonts=fonts)
                lcd.LCD_ShowImage(frame, 0, 0)
                last_draw = now

            if _check_cancel_input():
                break

            ready, _, _ = select([proc.stdout], [], [], 0.15)
            if ready:
                line = proc.stdout.readline()
                m = re.search(r"Device ([0-9A-F:]{17}) (.+)", line)
                if m:
                    mac, name = m.group(1), m.group(2).strip()
                    seen[mac] = name
                    last_seen = name
    finally:
        try:
            proc.stdin.write("scan off\n")
            proc.stdin.flush()
        except Exception:
            pass
        end = time.time() + 1.2
        while time.time() < end:
            ready, _, _ = select([proc.stdout], [], [], 0.15)
            if ready:
                line = proc.stdout.readline()
                m = re.search(r"Device ([0-9A-F:]{17}) (.+)", line)
                if m:
                    seen[m.group(1)] = m.group(2).strip()
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            pass

    results = []
    for mac, name in seen.items():
        meta = _query_device_info(mac)
        is_hid = meta["is_hid"] or _is_keyboard_name(name)
        results.append({
            "mac": mac,
            "name": name,
            "paired": meta["paired"],
            "connected": meta["connected"],
            "trusted": meta["trusted"],
            "is_hid": is_hid,
            "icon": meta["icon"],
        })

    # Prioritize HID/keyboards, then connected/paired, then alphabetical
    return sorted(
        results,
        key=lambda d_item: (
            0 if d_item["is_hid"] else 1,
            0 if d_item["connected"] else (1 if d_item["paired"] else 2),
            d_item["name"].lower(),
            d_item["mac"],
        ),
    )


def pair_trust_connect(lcd, fonts, mac: str, name: str) -> Tuple[bool, str]:
    """Pair, trust, and connect a Bluetooth device with live UI & passkey display."""
    W, H = lcd.width, lcd.height
    lcd.LCD_ShowImage(render_pairing_frame(W, H, name, mac, "Initializing Bluetooth...", progress=0.15, fonts=fonts), 0, 0)

    proc = subprocess.Popen(
        ["bluetoothctl"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdin and proc.stdout

    def send(cmd: str):
        try:
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()
        except Exception:
            pass

    for cmd in ("power on", "agent on", "default-agent"):
        send(cmd)
        time.sleep(0.25)

    # ---------------- Pair ----------------
    lcd.LCD_ShowImage(render_pairing_frame(W, H, name, mac, "Pairing Device...", progress=0.35, fonts=fonts), 0, 0)
    send(f"pair {mac}")
    paired = False
    cancelled = False
    start = time.time()
    while running and (time.time() - start) < 60:
        if _check_cancel_input():
            cancelled = True
            break
        ready, _, _ = select([proc.stdout], [], [], 0.25)
        if not ready:
            continue
        line = proc.stdout.readline()
        if not line:
            break
        if "Passkey" in line or "PIN code" in line:
            code = "".join(re.findall(r"\d", line))
            if code:
                lcd.LCD_ShowImage(
                    render_pairing_frame(W, H, name, mac, "Type Passkey on Keyboard", passkey=code, progress=0.55, fonts=fonts),
                    0, 0,
                )
        if "Confirm passkey" in line:
            send("yes")
        if "Paired: yes" in line or "Bonded: yes" in line or "AlreadyExists" in line:
            paired = True
            break
        if "Failed" in line or "Authentication" in line:
            break

    if cancelled or not running:
        try:
            proc.terminate()
        except Exception:
            pass
        return False, "Cancelled"

    if not paired:
        try:
            proc.terminate()
        except Exception:
            pass
        return False, "Pairing failed"

    # ---------------- Trust ----------------
    lcd.LCD_ShowImage(render_pairing_frame(W, H, name, mac, "Trusting Device...", progress=0.72, fonts=fonts), 0, 0)
    send(f"trust {mac}")
    time.sleep(0.5)

    # ---------------- Connect ----------------
    lcd.LCD_ShowImage(render_pairing_frame(W, H, name, mac, "Connecting HID...", progress=0.88, fonts=fonts), 0, 0)
    send(f"connect {mac}")
    connected = False
    start = time.time()
    while running and (time.time() - start) < 15:
        if _check_cancel_input():
            cancelled = True
            break
        ready, _, _ = select([proc.stdout], [], [], 0.25)
        if not ready:
            continue
        line = proc.stdout.readline()
        if not line:
            break
        if "Connection successful" in line or "already" in line.lower():
            connected = True
            break
        if "Failed" in line:
            break

    send("quit")
    try:
        proc.wait(timeout=3)
    except Exception:
        proc.terminate()

    if connected and running and not cancelled:
        return True, f"Connected to {name}"
    return False, ("Cancelled" if cancelled else "Connection failed")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global running

    GPIO.setmode(GPIO.BCM)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    LCD_Config.GPIO_Init()
    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    lcd.LCD_Clear()
    W, H = lcd.width, lcd.height
    fonts = rj_theme.load_app_fonts(W, H)

    try:
        with text_input_mode():
            flush_input()
            devices = discover_devices(lcd, fonts)
            flush_input()

            sel = 0
            scroll = 0
            toast = f"Found {len(devices)} Bluetooth device(s)"
            toast_kind = "info"
            toast_until = time.monotonic() + 3.5
            dirty = True

            while running:
                now_m = time.monotonic()
                mid_ui = (not fonts["wide"]) and W >= 200
                row_h = 46 if fonts["large"] else (28 if fonts["wide"] else (26 if mid_ui else 21))
                hdr_h = 30 if fonts["large"] else (22 if fonts["wide"] else (22 if mid_ui else 18))
                ftr_h = 22 if fonts["large"] else (16 if fonts["wide"] else (16 if mid_ui else 13))
                vis = max(1, (H - hdr_h - ftr_h - 14) // row_h)

                if devices:
                    sel = max(0, min(sel, len(devices) - 1))
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
                    frame = render_devices_frame(
                        W, H, devices, sel=sel, scroll=scroll,
                        toast=active_toast, toast_kind=toast_kind, fonts=fonts,
                    )
                    lcd.LCD_ShowImage(frame, 0, 0)
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO)
                if act is None and ch is None:
                    time.sleep(0.03)
                    continue

                if is_back(act):
                    cleanup()
                    break
                elif act == "UP" and devices:
                    sel = (sel - 1) % len(devices)
                    dirty = True
                elif act == "DOWN" and devices:
                    sel = (sel + 1) % len(devices)
                    dirty = True
                elif act == "PAGEUP" and devices:
                    sel = max(0, sel - vis)
                    dirty = True
                elif act == "PAGEDOWN" and devices:
                    sel = min(len(devices) - 1, sel + vis)
                    dirty = True
                elif is_action(act, ch, "KEY1", "s"):
                    devices = discover_devices(lcd, fonts)
                    sel = 0
                    scroll = 0
                    toast = f"Found {len(devices)} device(s)"
                    toast_kind = "info"
                    toast_until = time.monotonic() + 3.5
                    flush_input()
                    dirty = True
                elif is_confirm(act) and devices:
                    chosen = devices[sel]
                    ok, msg = pair_trust_connect(lcd, fonts, chosen["mac"], chosen["name"])
                    if ok:
                        chosen["paired"] = True
                        chosen["trusted"] = True
                        chosen["connected"] = True
                    # Show result card until user confirms or exits
                    res_img = Image.new("RGB", (W, H), ET["bg_base"])
                    rd = ImageDraw.Draw(res_img)
                    res_foot = format_footer_hint(
                        W, H,
                        hat_hint="OK:List K3:Exit",
                        hat_240_hint="OK:List  K3:Exit",
                        cardputer_hint="Enter:List  Esc:Exit",
                        hdmi_hint="Enter: List  •  Esc: Exit",
                    )
                    rj_theme.draw_status_card(
                        rd, W, H,
                        "Keyboard Connected!" if ok else "Connection Failed",
                        sub=f"{chosen['name']} ({chosen['mac']})",
                        kind="ok" if ok else "err",
                        foot=res_foot,
                        fonts=fonts,
                    )
                    lcd.LCD_ShowImage(res_img, 0, 0)
                    flush_input()
                    while running:
                        a2, _c2 = poll_ui_input(PINS, GPIO)
                        if a2 is None:
                            time.sleep(0.04)
                            continue
                        if is_back(a2):
                            cleanup()
                            break
                        if is_confirm(a2):
                            break
                    toast = msg
                    toast_kind = "ok" if ok else "err"
                    toast_until = time.monotonic() + 4.0
                    flush_input()
                    dirty = True
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
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
