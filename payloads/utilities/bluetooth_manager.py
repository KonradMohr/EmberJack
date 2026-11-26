#!/usr/bin/env python3
"""
RaspyJack Payload -- Bluetooth & Audio Connection Manager
=========================================================
Obsidian Ember themed manager for scanning, pairing, trusting, connecting,
disconnecting, and forgetting Bluetooth headphones, speakers, keyboards, mice,
and gamepads — with automatic PipeWire / PulseAudio / BlueALSA A2DP audio
routing and live volume + test chime controls.

Controls (100% Keyboard + Button + WebUI):
  UP / DOWN       Navigate devices / menus
  LEFT / RIGHT    Switch filter tab (All / Audio / Paired)
  ENTER / OK      Open device actions (Pair & Connect, Set Audio, Test, Forget)
  F1 / KEY1 / S   Scan for nearby Bluetooth devices (8s, Esc/K3 stops early)
  F2 / KEY2 / O   Tools & Audio Output switcher
  T               Play stereo test chime to verify headphones
  + / -           Adjust output volume (+/- 5%)
  P               Toggle Bluetooth adapter power ON/OFF
  R               Refresh device list
  F3 / KEY3 / Esc Back / Exit
"""

import os
import signal
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

from PIL import Image, ImageDraw

import rj_bluetooth
import rj_env
import rj_theme

TABS = ("all", "audio", "paired")
TAB_LABELS = {"all": "ALL", "audio": "AUDIO", "paired": "PAIRED"}


def filter_devices(devices: List[dict], tab: str) -> List[dict]:
    """Filter device list by active tab ('all', 'audio', 'paired')."""
    if tab == "audio":
        return [d for d in devices if rj_bluetooth.is_audio_device(d)]
    if tab == "paired":
        return [d for d in devices if d.get("paired") or d.get("connected")]
    return list(devices)


def build_device_actions(dev: dict) -> List[Tuple[str, str]]:
    """Build context-sensitive action list for a Bluetooth device."""
    actions: List[Tuple[str, str]] = []
    connected = bool(dev.get("connected"))
    paired = bool(dev.get("paired"))
    trusted = bool(dev.get("trusted"))
    is_audio = rj_bluetooth.is_audio_device(dev)

    if connected:
        if is_audio:
            actions.append(("Set as Audio Output", "set_audio"))
            actions.append(("Play Test Chime", "test_chime"))
        actions.append(("Disconnect", "disconnect"))
    else:
        lbl = "Connect" if paired else "Pair & Connect"
        if is_audio:
            lbl += " (Audio)"
        actions.append((lbl, "connect"))

    actions.append(("Untrust Device" if trusted else "Trust (Auto-Connect)", "toggle_trust"))
    if paired or connected:
        actions.append(("Forget / Unpair", "remove"))
    actions.append(("Back", "back"))
    return actions


def _load_ui_fonts(width: int, height: int) -> dict:
    compact = width < 260 or height < 160
    if width >= 560 and height >= 300:
        title_px, body_px, sub_px, badge_px = 15, 13, 11, 10
    elif width >= 300 and height >= 160:
        title_px, body_px, sub_px, badge_px = 12, 10, 9, 8
    elif width >= 200:
        title_px, body_px, sub_px, badge_px = 11, 10, 8, 8
    else:
        title_px, body_px, sub_px, badge_px = 9, 8, 7, 7
    return {
        "compact": compact,
        "wide": width >= 300,
        "title": rj_env.load_font(title_px, bold=True),
        "body": rj_env.load_font(body_px, bold=False),
        "body_bold": rj_env.load_font(body_px, bold=True),
        "sub": rj_env.load_font(sub_px, bold=False),
        "badge": rj_env.load_font(badge_px, bold=True, mono=True),
    }


def _rssi_to_pct(rssi: Optional[int]) -> int:
    if rssi is None:
        return 0
    return max(5, min(100, int((rssi + 100) * 2.0)))


def render_bt_manager_frame(
    width: int,
    height: int,
    adapter: dict,
    devices: List[dict],
    tab: str = "all",
    cursor: int = 0,
    scroll: int = 0,
    audio_label: str = "System Audio",
    volume_pct: int = 70,
    toast: str = "",
    toast_kind: str = "info",
    overlay: Optional[dict] = None,
    fonts: Optional[dict] = None,
) -> Image.Image:
    """Pure PIL renderer for the Obsidian Ember Bluetooth Connection Manager."""
    ET = rj_theme.EMBER_THEME
    f = fonts or _load_ui_fonts(width, height)
    compact = f["compact"]
    wide = f["wide"]

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    hdr_h = 30 if width >= 560 else (24 if wide else 18)
    tab_h = 24 if width >= 560 else (18 if wide else 14)
    ftr_h = 22 if width >= 560 else (18 if wide else 13)

    # 1. Header bar
    d.rectangle((0, 0, width - 1, hdr_h - 1), fill=ET["bg_elevated"])
    d.line([(0, hdr_h - 1), (width - 1, hdr_h - 1)], fill=ET["border_subtle"], width=1)
    hx = 8 if wide else 3
    title_txt = "BLUETOOTH" if wide else "BT"
    tb = d.textbbox((0, 0), title_txt, font=f["title"])
    d.text((hx, (hdr_h - (tb[3] - tb[1])) // 2 - tb[1]), title_txt, font=f["title"], fill=ET["accent_bright"])
    hx += (tb[2] - tb[0]) + (8 if wide else 4)

    powered = bool(adapter.get("powered"))
    avail = bool(adapter.get("available"))
    pwr_lbl = "ON" if powered else ("NO ADAPTER" if not avail else "OFF")
    pwr_bg = "#1b3324" if powered else ("#3a1d1d" if not avail else ET["bg_well"])
    pwr_fg = ET["ok"] if powered else (ET["danger"] if not avail else ET["text_muted"])
    pw, _ = rj_theme.draw_pill(
        d, hx, max(2, (hdr_h - (16 if wide else 12)) // 2),
        pwr_lbl, f["badge"], bg=pwr_bg, fg=pwr_fg,
        outline=ET["border_subtle"], pad_x=5 if wide else 3, pad_y=1, radius=3,
    )
    hx += pw + 8

    # Right side of header: Active Audio Output + Volume
    if wide:
        vol_txt = f"VOL {volume_pct}%"
        vw = d.textbbox((0, 0), vol_txt, font=f["badge"])[2] + 10
        rj_theme.draw_pill(
            d, width - vw - 6, max(2, (hdr_h - 16) // 2),
            vol_txt, f["badge"], bg=ET["bg_surface"], fg=ET["accent_bright"],
            outline=ET["border_subtle"], pad_x=5, pad_y=1, radius=3,
        )
        avail_w = width - vw - hx - 16
        if avail_w > 70 and audio_label:
            aud_txt = rj_theme.truncate_text(d, audio_label, avail_w, f["sub"])
            ab = d.textbbox((0, 0), aud_txt, font=f["sub"])
            d.text(
                (width - vw - 14 - (ab[2] - ab[0]), (hdr_h - (ab[3] - ab[1])) // 2 - ab[1]),
                aud_txt, font=f["sub"], fill=ET["text_muted"],
            )

    # 2. Filter tabs row (ALL / AUDIO / PAIRED)
    d.rectangle((0, hdr_h, width - 1, hdr_h + tab_h - 1), fill=ET["bg_surface"])
    d.line([(0, hdr_h + tab_h - 1), (width - 1, hdr_h + tab_h - 1)], fill=ET["border_subtle"], width=1)
    tx = 6 if wide else 2
    for t_key in TABS:
        cnt = len(filter_devices(devices, t_key))
        lbl = f"{TAB_LABELS[t_key]} ({cnt})" if wide else f"{TAB_LABELS[t_key]}:{cnt}"
        active = t_key == tab
        bg = ET["select_bg"] if active else ET["bg_well"]
        fg = ET["accent_bright"] if active else ET["text_muted"]
        out_col = ET["accent"] if active else ET["border_subtle"]
        tw, _ = rj_theme.draw_pill(
            d, tx, hdr_h + max(2, (tab_h - (16 if wide else 11)) // 2),
            lbl, f["badge"], bg=bg, fg=fg, outline=out_col,
            pad_x=6 if wide else 3, pad_y=1, radius=4,
        )
        tx += tw + (6 if wide else 3)

    # 3. Device list viewport
    list_y0 = hdr_h + tab_h + (4 if wide else 2)
    list_y1 = height - ftr_h - (22 if toast and wide else (4 if wide else 2))
    filtered = filter_devices(devices, tab)

    if not filtered:
        cy = (list_y0 + list_y1) // 2
        if not avail:
            msg1 = "No Bluetooth adapter detected"
            msg2 = "Check hci0 / bluetoothd service"
        elif not powered:
            msg1 = "Bluetooth is turned OFF"
            msg2 = "Press P or F2 -> Power ON"
        else:
            msg1 = "No audio devices yet" if tab == "audio" else "No devices found"
            msg2 = "Put headphones in pairing mode & press F1 / S to scan" if wide else "F1/S: Scan"
        msg1 = rj_theme.truncate_text(d, msg1, width - 16, f["body_bold"])
        msg2 = rj_theme.truncate_text(d, msg2, width - 16, f["sub"])
        d.text((width // 2, cy - (10 if wide else 6)), msg1, font=f["body_bold"], fill=ET["text_primary"], anchor="mm")
        d.text((width // 2, cy + (10 if wide else 6)), msg2, font=f["sub"], fill=ET["text_muted"], anchor="mm")
    else:
        row_h = 40 if width >= 560 else (30 if wide else 20)
        vis_n = max(1, (list_y1 - list_y0) // row_h)
        st = max(0, min(scroll, max(0, len(filtered) - vis_n)))
        sb_w = 8 if wide else 4
        card_x0 = 8 if wide else 2
        card_x1 = width - sb_w - (8 if wide else 3)

        for slot in range(vis_n):
            idx = st + slot
            if idx >= len(filtered):
                break
            dev = filtered[idx]
            sel = idx == cursor
            ry0 = list_y0 + slot * row_h
            ry1 = ry0 + row_h - (4 if wide else 2)

            rj_theme.draw_card(
                d, (card_x0, ry0, card_x1, ry1),
                fill=ET["select_bg"] if sel else ET["bg_surface"],
                outline=ET["border_accent"] if sel else ET["border_subtle"],
                radius=6 if wide else 3,
                accent_left=ET["accent"] if sel else (ET["ok"] if dev.get("connected") else None),
                accent_width=3 if wide else 2,
            )

            # Left type badge
            dtype = dev.get("device_type", "device")
            b_txt = rj_bluetooth.device_type_badge(dtype)
            is_aud = rj_bluetooth.is_audio_device(dev)
            b_bg = "#352112" if is_aud else ET["bg_well"]
            b_fg = ET["accent_bright"] if is_aud else ET["text_muted"]
            bx = card_x0 + (8 if wide else 4)
            by = ry0 + max(3, ((ry1 - ry0) - (16 if wide else 11)) // 2)
            bw, _ = rj_theme.draw_pill(
                d, bx, by, b_txt, f["badge"], bg=b_bg, fg=b_fg,
                outline=ET["border_accent"] if is_aud else ET["border_subtle"],
                pad_x=4 if wide else 2, pad_y=1, radius=3,
            )
            name_x = bx + bw + (8 if wide else 4)

            # Right-side status badges (CONNECTED / PAIRED / Battery / RSSI)
            rx = card_x1 - (8 if wide else 3)
            if wide and dev.get("rssi") is not None:
                bar_w = 18
                bar_h = 12
                rj_theme.draw_signal_bars(d, rx - bar_w, ry0 + (ry1 - ry0 - bar_h) // 2, bar_h, _rssi_to_pct(dev.get("rssi")))
                rx -= bar_w + 6

            if wide and dev.get("battery") is not None:
                bat_lbl = f"{dev['battery']}%"
                bat_w = d.textbbox((0, 0), bat_lbl, font=f["badge"])[2] + 8
                rj_theme.draw_pill(
                    d, rx - bat_w, ry0 + max(3, (ry1 - ry0 - 15) // 2),
                    bat_lbl, f["badge"], bg=ET["bg_well"], fg=ET["ok"] if dev["battery"] >= 25 else ET["warn"],
                    outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3,
                )
                rx -= bat_w + 5

            if dev.get("connected"):
                st_lbl, st_bg, st_fg = ("CONNECTED" if wide else "ON"), "#1b3324", ET["ok"]
            elif dev.get("paired"):
                st_lbl, st_bg, st_fg = ("PAIRED" if wide else "PRD"), "#332414", ET["accent_bright"]
            else:
                st_lbl, st_bg, st_fg = ("NEW" if wide else "NEW"), ET["bg_well"], ET["text_muted"]
            st_w = d.textbbox((0, 0), st_lbl, font=f["badge"])[2] + (10 if wide else 6)
            rj_theme.draw_pill(
                d, rx - st_w, ry0 + max(2, (ry1 - ry0 - (15 if wide else 11)) // 2),
                st_lbl, f["badge"], bg=st_bg, fg=st_fg,
                outline=ET["border_subtle"], pad_x=5 if wide else 3, pad_y=1, radius=3,
            )
            rx -= st_w + (8 if wide else 3)

            avail_name_w = max(32, rx - name_x)
            dev_name = rj_theme.truncate_text(d, dev.get("name") or dev.get("mac", "?"), avail_name_w, f["body_bold"])
            if wide:
                d.text((name_x, ry0 + 4), dev_name, font=f["body_bold"], fill=ET["text_bright"] if sel else ET["text_primary"])
                sub_parts = [dev.get("mac", "")]
                if dev.get("trusted"):
                    sub_parts.append("Trusted")
                sub_txt = rj_theme.truncate_text(d, "  •  ".join(sub_parts), avail_name_w, f["sub"])
                d.text((name_x, ry0 + 21), sub_txt, font=f["sub"], fill=ET["text_muted"])
            else:
                d.text((name_x, ry0 + 4), dev_name, font=f["body"], fill=ET["text_bright"] if sel else ET["text_primary"])

        if len(filtered) > vis_n:
            rj_theme.draw_scrollbar(d, width - sb_w - 2, list_y0, list_y1, st, vis_n, len(filtered), width=4 if wide else 2)

    # 4. Optional status toast banner
    if toast and wide:
        ty0 = height - ftr_h - 20
        t_col = ET["ok"] if toast_kind == "ok" else (ET["danger"] if toast_kind == "err" else ET["accent_bright"])
        d.rectangle((0, ty0, width - 1, height - ftr_h - 1), fill=ET["bg_overlay"])
        d.line([(0, ty0), (width - 1, ty0)], fill=ET["border_subtle"], width=1)
        tt = rj_theme.truncate_text(d, toast, width - 16, f["sub"])
        d.text((8, ty0 + 3), tt, font=f["sub"], fill=t_col)

    # 5. Footer key-hints
    d.rectangle((0, height - ftr_h, width - 1, height - 1), fill=ET["bg_elevated"])
    d.line([(0, height - ftr_h), (width - 1, height - ftr_h)], fill=ET["border_subtle"], width=1)
    if width >= 520:
        hint = "Arrows:Nav/Tab  Enter:Actions  F1/S:Scan  F2/O:Audio/Tools  T:Chime  +/-:Vol  F3/Esc:Exit"
    elif wide:
        hint = "OK:Act  F1/S:Scan  F2:Tools  T:Test  K3:Exit"
    else:
        hint = "OK F1:Scan F2:Tool K3:X"
    hint = rj_theme.truncate_text(d, hint, width - 8, f["sub"])
    hb = d.textbbox((0, 0), hint, font=f["sub"])
    d.text((4, height - ftr_h + (ftr_h - (hb[3] - hb[1])) // 2 - hb[1]), hint, font=f["sub"], fill=ET["text_muted"])

    # 6. Modal overlays (Scanning / Busy / Action Menu / Audio Sink Picker)
    if overlay:
        kind = overlay.get("kind", "")
        if kind in ("scan", "busy"):
            bw = min(width - 20, max(116, int(width * 0.68)))
            bh = 98 if wide else 74
            bx0 = (width - bw) // 2
            by0 = (height - bh) // 2
            rj_theme.draw_card(
                d, (bx0, by0, bx0 + bw, by0 + bh),
                fill=ET["bg_elevated"], outline=ET["border_accent"], radius=8 if wide else 4,
                accent_left=ET["accent"], accent_width=3,
            )
            title = rj_theme.truncate_text(d, overlay.get("title", "Working..."), bw - 20, f["title"])
            sub = rj_theme.truncate_text(d, overlay.get("sub", ""), bw - 20, f["sub"])
            d.text((bx0 + 12, by0 + 10), title, font=f["title"], fill=ET["accent_bright"])
            if sub:
                d.text((bx0 + 12, by0 + (32 if wide else 24)), sub, font=f["sub"], fill=ET["text_primary"])
            if kind == "scan":
                frac = float(overlay.get("progress", 0.0))
                bar_y = by0 + (54 if wide else 42)
                rj_theme.draw_progress_bar(d, (bx0 + 12, bar_y, bx0 + bw - 12, bar_y + (10 if wide else 7)), frac)
                foot = overlay.get("foot", "Press Esc / F3 to stop early")
                d.text((bx0 + 12, bar_y + (18 if wide else 12)), rj_theme.truncate_text(d, foot, bw - 20, f["sub"]), font=f["sub"], fill=ET["text_muted"])
        elif kind == "menu":
            items = overlay.get("items", [])
            msel = int(overlay.get("sel", 0))
            mtitle = overlay.get("title", "Actions")
            msub = overlay.get("sub", "")
            bw = min(width - 16, max(118, int(width * 0.62)))
            row_h = 26 if width >= 560 else (20 if wide else 15)
            hdr_part = (42 if msub else 28) if wide else 20
            vis = min(len(items), max(3, (height - hdr_part - 36) // row_h))
            bh = hdr_part + vis * row_h + 12
            bx0 = (width - bw) // 2
            by0 = (height - bh) // 2
            rj_theme.draw_card(
                d, (bx0, by0, bx0 + bw, by0 + bh),
                fill=ET["bg_elevated"], outline=ET["border_accent"], radius=8 if wide else 4,
                accent_left=ET["accent"], accent_width=3,
            )
            d.text((bx0 + 10, by0 + 6), rj_theme.truncate_text(d, mtitle, bw - 20, f["title"]), font=f["title"], fill=ET["accent_bright"])
            if msub and wide:
                d.text((bx0 + 10, by0 + 23), rj_theme.truncate_text(d, msub, bw - 20, f["sub"]), font=f["sub"], fill=ET["text_muted"])
            d.line([(bx0 + 6, by0 + hdr_part - 3), (bx0 + bw - 6, by0 + hdr_part - 3)], fill=ET["border_subtle"], width=1)
            mst = max(0, min(msel - vis + 1, max(0, len(items) - vis)))
            for i in range(vis):
                idx = mst + i
                if idx >= len(items):
                    break
                label = items[idx][0] if isinstance(items[idx], tuple) else str(items[idx])
                ry = by0 + hdr_part + i * row_h
                is_s = idx == msel
                if is_s:
                    rj_theme.draw_card(
                        d, (bx0 + 6, ry, bx0 + bw - 6, ry + row_h - 3),
                        fill=ET["select_bg"], outline=None, radius=4,
                        accent_left=ET["accent"], accent_width=2,
                    )
                txt = rj_theme.truncate_text(d, label, bw - 24, f["body"])
                d.text((bx0 + 14, ry + (4 if wide else 2)), txt, font=f["body"], fill=ET["accent_bright"] if is_s else ET["text_primary"])

    return img


def main() -> int:
    import RPi.GPIO as GPIO
    import LCD_1in44
    from payloads._input_helper import poll_ui_input, flush_input, text_input_mode

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
    fonts = _load_ui_fonts(W, H)

    running = True

    def _on_sig(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _on_sig)
    signal.signal(signal.SIGTERM, _on_sig)

    try:
        import payloads._audio_helper as audio_helper
    except Exception:
        audio_helper = None

    adapter = rj_bluetooth.get_adapter_status()
    devices: List[dict] = []
    tab_idx = 0
    cursor = 0
    scroll = 0
    toast = ""
    toast_kind = "info"
    toast_until = 0.0
    volume_pct = rj_bluetooth.get_system_volume_pct()
    audio_label = audio_helper.get_active_audio_label() if audio_helper else "System Audio"

    def _set_toast(msg: str, kind: str = "info", secs: float = 3.5) -> None:
        nonlocal toast, toast_kind, toast_until
        toast = msg
        toast_kind = kind
        toast_until = time.monotonic() + secs

    def _render(overlay: Optional[dict] = None) -> None:
        cur_toast = toast if time.monotonic() < toast_until else ""
        frame = render_bt_manager_frame(
            W, H,
            adapter=adapter,
            devices=devices,
            tab=TABS[tab_idx],
            cursor=cursor,
            scroll=scroll,
            audio_label=audio_label,
            volume_pct=volume_pct,
            toast=cur_toast,
            toast_kind=toast_kind,
            overlay=overlay,
            fonts=fonts,
        )
        lcd.LCD_ShowImage(frame, 0, 0)

    def _refresh_state() -> None:
        nonlocal adapter, devices, audio_label, volume_pct, cursor, scroll
        adapter = rj_bluetooth.get_adapter_status()
        devices = rj_bluetooth.list_known_devices()
        audio_label = audio_helper.get_active_audio_label() if audio_helper else "System Audio"
        volume_pct = rj_bluetooth.get_system_volume_pct()
        filtered = filter_devices(devices, TABS[tab_idx])
        if filtered:
            cursor = max(0, min(cursor, len(filtered) - 1))
        else:
            cursor = 0
            scroll = 0

    def _run_scan() -> None:
        nonlocal devices, adapter, cursor, scroll
        if not adapter.get("powered"):
            _render({"kind": "busy", "title": "Enabling Bluetooth...", "sub": "Unblocking & powering adapter"})
            ok_p, msg_p = rj_bluetooth.set_power(True)
            adapter = rj_bluetooth.get_adapter_status()
            if not ok_p:
                _set_toast(msg_p, "err")
                return

        stop_scan = False
        last_draw = 0.0

        def _check_stop() -> bool:
            nonlocal stop_scan
            if not running:
                return True
            act, _ch = poll_ui_input(PINS, GPIO)
            if act in ("KEY3", "ESCAPE", "BACKSPACE"):
                stop_scan = True
            return stop_scan

        def _on_prog(frac: float, count: int, latest: str) -> None:
            nonlocal last_draw
            now_m = time.monotonic()
            if (now_m - last_draw) < 0.12 and frac < 0.99:
                return
            last_draw = now_m
            sub = f"Found {count} device(s)" + (f"  •  {latest[:20]}" if latest else " — put headphones in pairing mode")
            _render({
                "kind": "scan",
                "title": "Scanning Bluetooth...",
                "sub": sub,
                "progress": frac,
                "foot": "Press Esc / F3 to finish early",
            })

        _render({"kind": "scan", "title": "Scanning Bluetooth...", "sub": "Put headphones in pairing mode...", "progress": 0.05})
        found = rj_bluetooth.scan_devices(duration=8.0, on_progress=_on_prog, stop_check=_check_stop)
        devices = found
        adapter = rj_bluetooth.get_adapter_status()
        cursor = 0
        scroll = 0
        audio_cnt = len(filter_devices(devices, "audio"))
        _set_toast(f"Scan complete: {len(devices)} device(s), {audio_cnt} audio", "ok")
        flush_input()

    def _run_modal_menu(title: str, sub: str, items: List[Tuple[str, object]]):
        sel = 0
        dirty = True
        flush_input()
        while running:
            if dirty:
                _render({"kind": "menu", "title": title, "sub": sub, "items": items, "sel": sel})
                dirty = False
            act, ch = poll_ui_input(PINS, GPIO)
            if act == "UP":
                sel = (sel - 1) % len(items)
                dirty = True
            elif act == "DOWN":
                sel = (sel + 1) % len(items)
                dirty = True
            elif act in ("OK", "ENTER"):
                flush_input()
                return items[sel][1]
            elif act in ("KEY3", "LEFT", "ESCAPE", "BACKSPACE") or (ch and ch.lower() == "q"):
                flush_input()
                return None
            time.sleep(0.04)
        return None

    def _open_audio_sink_picker() -> None:
        nonlocal audio_label
        sinks = rj_bluetooth.list_audio_sinks()
        if not sinks:
            _set_toast("No audio outputs detected (check aplay -l)", "err")
            return
        items = [
            (("* " if s["is_default"] else "  ") + s["label"], s)
            for s in sinks
        ]
        items.append(("Back", None))
        picked = _run_modal_menu("AUDIO OUTPUT", "Select default speaker / headphones", items)
        if isinstance(picked, dict):
            ok, msg = rj_bluetooth.set_default_audio_sink(picked["name"], label=picked["label"])
            audio_label = audio_helper.get_active_audio_label() if audio_helper else picked["label"]
            _set_toast(msg, "ok" if ok else "err")

    def _open_tools_menu() -> None:
        nonlocal adapter
        pwr_action_lbl = "Turn Bluetooth OFF" if adapter.get("powered") else "Turn Bluetooth ON"
        items = [
            ("Scan for Devices (8s)", "scan"),
            ("Switch Audio Output", "sinks"),
            ("Play Test Audio Chime", "chime"),
            (pwr_action_lbl, "power"),
            ("Refresh Device List", "refresh"),
            ("Close", "close"),
        ]
        act = _run_modal_menu("BLUETOOTH TOOLS", f"Adapter: {adapter.get('name', 'hci0')}", items)
        if act == "scan":
            _run_scan()
        elif act == "sinks":
            _open_audio_sink_picker()
        elif act == "chime":
            _render({"kind": "busy", "title": "Playing Test Chime...", "sub": audio_label})
            ok, msg = rj_bluetooth.play_test_chime()
            _set_toast(msg, "ok" if ok else "err")
        elif act == "power":
            new_state = not adapter.get("powered")
            _render({"kind": "busy", "title": "Bluetooth Power...", "sub": "Turning ON" if new_state else "Turning OFF"})
            ok, msg = rj_bluetooth.set_power(new_state)
            _refresh_state()
            _set_toast(msg, "ok" if ok else "err")
        elif act == "refresh":
            _render({"kind": "busy", "title": "Refreshing...", "sub": "Reading Bluetooth devices"})
            _refresh_state()
            _set_toast(f"Loaded {len(devices)} device(s)", "info")

    def _handle_device_action(dev: dict) -> None:
        actions = build_device_actions(dev)
        act = _run_modal_menu(dev.get("name", dev["mac"]), dev["mac"], actions)
        if not act or act == "back":
            return
        if act == "connect":
            cancelled = False

            def _cancel_chk() -> bool:
                nonlocal cancelled
                if not running:
                    return True
                a, _c = poll_ui_input(PINS, GPIO)
                if a in ("KEY3", "ESCAPE", "BACKSPACE"):
                    cancelled = True
                return cancelled

            def _status_cb(title_s: str, sub_s: str) -> None:
                _render({"kind": "busy", "title": title_s, "sub": sub_s})

            ok, msg = rj_bluetooth.connect_device(
                dev["mac"],
                device_type=dev.get("device_type", "device"),
                device_name=dev.get("name", ""),
                on_status=_status_cb,
                stop_check=_cancel_chk,
            )
            _refresh_state()
            _set_toast(msg, "ok" if ok else "err", secs=5.0)
        elif act == "disconnect":
            _render({"kind": "busy", "title": "Disconnecting...", "sub": dev.get("name", dev["mac"])})
            ok, msg = rj_bluetooth.disconnect_device(dev["mac"])
            _refresh_state()
            _set_toast(msg, "ok" if ok else "err")
        elif act == "set_audio":
            _render({"kind": "busy", "title": "Routing Audio...", "sub": dev.get("name", dev["mac"])})
            ok, msg = rj_bluetooth.activate_bluetooth_audio_sink(dev["mac"], device_name=dev.get("name", ""))
            _refresh_state()
            _set_toast(msg, "ok" if ok else "err")
        elif act == "test_chime":
            _render({"kind": "busy", "title": "Playing Test Chime...", "sub": dev.get("name", dev["mac"])})
            ok, msg = rj_bluetooth.play_test_chime()
            _set_toast(msg, "ok" if ok else "err")
        elif act == "toggle_trust":
            ok, msg = rj_bluetooth.trust_device(dev["mac"], trust=not dev.get("trusted"))
            _refresh_state()
            _set_toast(msg, "ok" if ok else "err")
        elif act == "remove":
            _render({"kind": "busy", "title": "Forgetting Device...", "sub": dev.get("name", dev["mac"])})
            ok, msg = rj_bluetooth.remove_device(dev["mac"])
            _refresh_state()
            _set_toast(msg, "ok" if ok else "err")
        flush_input()

    try:
        with text_input_mode():
            _render({"kind": "busy", "title": "Bluetooth Manager", "sub": "Loading paired & connected devices..."})
            _refresh_state()
            flush_input()

            dirty = True
            toast_visible = False
            last_active_at = time.monotonic()

            while running:
                now_m = time.monotonic()
                filtered = filter_devices(devices, TABS[tab_idx])
                row_h = 40 if W >= 560 else (30 if W >= 300 else 20)
                hdr_h = 30 if W >= 560 else (24 if W >= 300 else 18)
                tab_h = 24 if W >= 560 else (18 if W >= 300 else 14)
                ftr_h = 22 if W >= 560 else (18 if W >= 300 else 13)
                vis_n = max(1, (H - hdr_h - tab_h - ftr_h - 8) // row_h)

                if filtered:
                    cursor = max(0, min(cursor, len(filtered) - 1))
                    if cursor < scroll:
                        scroll = cursor
                    elif cursor >= scroll + vis_n:
                        scroll = cursor - vis_n + 1
                else:
                    cursor = 0
                    scroll = 0

                cur_toast_on = bool(toast and now_m < toast_until)
                if cur_toast_on != toast_visible:
                    toast_visible = cur_toast_on
                    dirty = True

                if dirty:
                    _render()
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO)
                if act is None and ch is None:
                    time.sleep(0.025 if (now_m - last_active_at) < 1.5 else 0.05)
                    continue

                last_active_at = time.monotonic()
                k_low = ch.lower() if isinstance(ch, str) else ""

                if act in ("KEY3", "ESCAPE", "BACKSPACE") or k_low == "q":
                    break
                elif act == "UP":
                    if filtered:
                        cursor = (cursor - 1) % len(filtered)
                        dirty = True
                elif act == "DOWN":
                    if filtered:
                        cursor = (cursor + 1) % len(filtered)
                        dirty = True
                elif act == "PAGEUP":
                    if filtered:
                        cursor = max(0, cursor - vis_n)
                        dirty = True
                elif act == "PAGEDOWN":
                    if filtered:
                        cursor = min(len(filtered) - 1, cursor + vis_n)
                        dirty = True
                elif act == "LEFT":
                    tab_idx = (tab_idx - 1) % len(TABS)
                    cursor = 0
                    scroll = 0
                    dirty = True
                elif act in ("RIGHT", "TAB"):
                    tab_idx = (tab_idx + 1) % len(TABS)
                    cursor = 0
                    scroll = 0
                    dirty = True
                elif k_low == "1":
                    tab_idx, cursor, scroll, dirty = 0, 0, 0, True
                elif k_low == "2":
                    tab_idx, cursor, scroll, dirty = 1, 0, 0, True
                elif k_low == "3":
                    tab_idx, cursor, scroll, dirty = 2, 0, 0, True
                elif act in ("OK", "ENTER"):
                    if filtered:
                        _handle_device_action(filtered[cursor])
                    else:
                        _run_scan()
                    dirty = True
                elif act == "KEY1" or k_low == "s":
                    _run_scan()
                    dirty = True
                elif act == "KEY2" or k_low == "o":
                    _open_tools_menu()
                    dirty = True
                elif k_low == "p":
                    new_state = not adapter.get("powered")
                    _render({"kind": "busy", "title": "Bluetooth Power...", "sub": "Turning ON" if new_state else "Turning OFF"})
                    ok, msg = rj_bluetooth.set_power(new_state)
                    _refresh_state()
                    _set_toast(msg, "ok" if ok else "err")
                    dirty = True
                elif k_low == "r":
                    _render({"kind": "busy", "title": "Refreshing...", "sub": "Reading Bluetooth devices"})
                    _refresh_state()
                    _set_toast(f"Loaded {len(devices)} device(s)", "info")
                    dirty = True
                elif k_low == "t":
                    _render({"kind": "busy", "title": "Playing Test Chime...", "sub": audio_label})
                    ok, msg = rj_bluetooth.play_test_chime()
                    _set_toast(msg, "ok" if ok else "err")
                    dirty = True
                elif ch in ("+", "=", "]"):
                    volume_pct = rj_bluetooth.set_system_volume_pct(volume_pct + 5)
                    _set_toast(f"Volume: {volume_pct}%", "info", secs=1.8)
                    dirty = True
                elif ch in ("-", "_", "["):
                    volume_pct = rj_bluetooth.set_system_volume_pct(volume_pct - 5)
                    _set_toast(f"Volume: {volume_pct}%", "info", secs=1.8)
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
