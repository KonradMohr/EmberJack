#!/usr/bin/env python3
"""
EmberJack Payload -- Audio & Volume Manager
===========================================
Obsidian Ember mixer and device manager for HDMI monitors, CardputerZero, and
SPI LCDs. Manage speakers, HDMI outputs, Bluetooth headphones, microphones,
live per-app audio streams, hardware/BT profiles, and L/R stereo tests without
a desktop environment.

Controls:
  TAB / 1-4 / [ ]     Switch Tabs (Outputs / Inputs / Streams / Tools)
  UP / DOWN           Select device / stream / tool
  LEFT / RIGHT (-/+)  Adjust volume (-5% / +5%) live
  ENTER / OK          Set default output/mic, move stream, or run tool
  M / KEY1            Mute / Unmute selected device or stream
  T / KEY2            Play stereo test chime (or refresh)
  R                   Refresh audio devices & streams
  B                   Open Bluetooth Manager
  F3 / KEY3 / Esc / Q Back / Exit
"""

import os
import signal
import subprocess
import sys
import time
from typing import List, Optional, Tuple

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

from PIL import Image, ImageDraw

import rj_audio as aud
import rj_env
import rj_theme

TABS: Tuple[Tuple[str, str], ...] = (
    ("outputs", "OUTPUTS"),
    ("inputs", "INPUTS"),
    ("streams", "STREAMS"),
    ("tools", "TOOLS"),
)

TOOLS_ITEMS: List[Tuple[str, str, str]] = [
    ("Play Stereo Test Chime", "test_both", "Play gentle C5-G5 tone on both channels"),
    ("Test Left Channel Only", "test_left", "Verify left speaker / left ear cup"),
    ("Test Right Channel Only", "test_right", "Verify right speaker / right ear cup"),
    ("Switch Card / BT Profile", "profiles", "Toggle Bluetooth A2DP vs Headset or HDMI profile"),
    ("Fix BT A2DP Stereo (Unblock)", "fix_bt_a2dp", "Disable BlueALSA conflict & restore A2DP Stereo"),
    ("Open Bluetooth Manager", "bt_mgr", "Pair & connect wireless headphones/speakers"),
    ("Restart Audio Services", "restart", "Restart PipeWire / WirePlumber / PulseAudio"),
    ("Refresh Audio Devices", "refresh", "Re-scan sinks, mics & active playback streams"),
]


def _load_fonts(width: int, height: int) -> dict:
    wide = width >= 300
    large = width >= 540 and height >= 300
    if large:
        t_px, b_px, s_px, tag_px = 14, 13, 11, 10
    elif wide:
        t_px, b_px, s_px, tag_px = 12, 10, 9, 8
    elif width >= 200:
        t_px, b_px, s_px, tag_px = 11, 10, 8, 8
    else:
        t_px, b_px, s_px, tag_px = 9, 8, 7, 7
    return {
        "wide": wide,
        "large": large,
        "title": rj_env.load_font(t_px, bold=True),
        "body": rj_env.load_font(b_px, bold=False),
        "body_bold": rj_env.load_font(b_px, bold=True),
        "sub": rj_env.load_font(s_px, bold=False),
        "badge": rj_env.load_font(tag_px, bold=True, mono=True),
    }


def _draw_vol_bar(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], vol_pct: int, muted: bool, radius: int = 3) -> None:
    ET = rj_theme.EMBER_THEME
    x0, y0, x1, y1 = box
    frac = max(0.0, min(1.0, float(vol_pct) / 100.0))
    fill_col = ET["text_muted"] if muted else (ET["warn"] if vol_pct > 100 else ET["accent"])
    rj_theme.draw_progress_bar(d, (x0, y0, x1, y1), frac, bg=ET["bg_well"], fill=fill_col, radius=radius)


def render_audio_manager_frame(
    width: int,
    height: int,
    tab_idx: int,
    outputs: List[dict],
    inputs: List[dict],
    streams: List[dict],
    cursor: int,
    scroll: int,
    backend_info: dict,
    toast: str = "",
    toast_kind: str = "info",
    overlay: Optional[dict] = None,
    fonts: Optional[dict] = None,
) -> Image.Image:
    """Pure Obsidian Ember renderer for the Audio Manager (640x360, 320x170, 240x240, 128x128)."""
    ET = rj_theme.EMBER_THEME
    f = fonts or _load_fonts(width, height)
    wide = f["wide"]
    large = f["large"]

    hdr_h = 30 if large else (22 if wide else 16)
    tab_h = 24 if large else (18 if wide else 14)
    ftr_h = 22 if large else (16 if wide else 13)

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    # 1. Header bar
    d.rectangle((0, 0, width - 1, hdr_h - 1), fill=ET["bg_elevated"])
    d.rectangle((0, 0, 3 if large else 2, hdr_h - 1), fill=ET["accent"])
    d.line([(0, hdr_h - 1), (width - 1, hdr_h - 1)], fill=ET["border_subtle"], width=1)

    title_txt = "AUDIO MANAGER" if wide else "AUDIO"
    d.text((10 if large else 6, hdr_h // 2), title_txt, font=f["title"], fill=ET["accent_bright"], anchor="lm")

    # Right header pills: Backend + Master Volume
    def_out = next((o for o in outputs if o.get("is_default")), outputs[0] if outputs else None)
    m_vol = int(def_out.get("volume_pct", 70)) if def_out else 0
    m_muted = bool(def_out.get("muted", False)) if def_out else False
    vol_lbl = "MUTED" if m_muted else f"VOL {m_vol}%"
    rx = width - (8 if large else 4)
    vb = rj_theme.measure_text(f["badge"], vol_lbl, d)
    vw = (vb[2] - vb[0]) + (12 if large else 6)
    rj_theme.draw_pill(
        d, rx - vw, max(2, (hdr_h - (18 if large else 12)) // 2), vol_lbl, f["badge"],
        bg=ET["bg_well"], fg=ET["warn"] if m_muted else ET["accent_bright"],
        outline=ET["border_accent"] if not m_muted else ET["warn"],
        pad_x=5 if large else 3, pad_y=1, radius=3,
    )
    rx -= vw + 6

    if wide:
        bk_lbl = str(backend_info.get("backend", "ALSA")).upper()[:14]
        bb = rj_theme.measure_text(f["badge"], bk_lbl, d)
        bw = (bb[2] - bb[0]) + 10
        rj_theme.draw_pill(
            d, rx - bw, max(2, (hdr_h - (18 if large else 12)) // 2), bk_lbl, f["badge"],
            bg=ET["bg_surface"], fg=ET["text_primary"], outline=ET["border_subtle"],
            pad_x=5, pad_y=1, radius=3,
        )

    # 2. Tab bar
    ty0 = hdr_h
    d.rectangle((0, ty0, width - 1, ty0 + tab_h - 1), fill=ET["bg_surface"])
    d.line([(0, ty0 + tab_h - 1), (width - 1, ty0 + tab_h - 1)], fill=ET["border_subtle"], width=1)

    tab_counts = (len(outputs), len(inputs), len(streams), len(TOOLS_ITEMS))
    compact_tabs = ("OUT", "MIC", "APP", "TLS")
    tw = width // len(TABS)
    for i, (_tkey, tlbl) in enumerate(TABS):
        tx0 = i * tw
        tx1 = (width - 1) if i == len(TABS) - 1 else (tx0 + tw - 1)
        active = (i == tab_idx)
        if active:
            d.rectangle((tx0, ty0, tx1, ty0 + tab_h - 1), fill=ET["select_bg"])
            d.rectangle((tx0, ty0 + tab_h - 2, tx1, ty0 + tab_h - 1), fill=ET["accent"])
        lbl_str = f"{tlbl} ({tab_counts[i]})" if (large and i < 3) else (tlbl if wide else compact_tabs[i])
        d.text(
            ((tx0 + tx1) // 2, ty0 + tab_h // 2),
            lbl_str,
            font=f["badge"],
            fill=ET["accent_bright"] if active else ET["text_muted"],
            anchor="mm",
        )

    # 3. Main Body (Split-pane on widescreen >= 480px)
    y0 = ty0 + tab_h + (6 if large else 3)
    y1 = height - ftr_h - (5 if large else 2)
    tab_key = TABS[tab_idx][0]
    active_list = outputs if tab_key == "outputs" else (inputs if tab_key == "inputs" else (streams if tab_key == "streams" else TOOLS_ITEMS))

    split = width >= 480
    list_x0 = 8 if wide else 2
    list_x1 = int(width * 0.62) - 4 if split else (width - (8 if wide else 4))
    row_h = 46 if (large and tab_key != "tools") else (36 if large else (28 if wide else 20))
    vis = max(1, (y1 - y0) // row_h)
    total = len(active_list)
    cur_clamped = max(0, min(cursor, max(0, total - 1)))
    st = max(0, min(scroll, max(0, total - vis)))

    if total == 0:
        empty_msg = {
            "outputs": "No audio output devices found",
            "inputs": "No microphones / capture inputs found",
            "streams": "No apps playing audio right now",
            "tools": "No tools available",
        }.get(tab_key, "Empty")
        empty_sub = "Press R to rescan or open Tools -> Restart Audio" if tab_key != "streams" else "Start YouTube, YT Music, or an app to mix it here"
        cx = (list_x0 + list_x1) // 2
        cy = (y0 + y1) // 2
        d.text((cx, cy - 8), rj_theme.truncate_text(d, empty_msg, list_x1 - list_x0 - 10, f["body_bold"]), font=f["body_bold"], fill=ET["text_muted"], anchor="mm")
        if wide:
            d.text((cx, cy + 12), rj_theme.truncate_text(d, empty_sub, list_x1 - list_x0 - 12, f["sub"]), font=f["sub"], fill=ET["text_muted"], anchor="mm")
    else:
        for slot in range(vis):
            idx = st + slot
            if idx >= total:
                break
            ry = y0 + slot * row_h
            ry1 = ry + row_h - (4 if wide else 2)
            sel = (idx == cur_clamped)

            rj_theme.draw_card(
                d, (list_x0, ry, list_x1, ry1),
                fill=ET["select_bg"] if sel else ET["bg_surface"],
                outline=ET["border_accent"] if sel else ET["border_subtle"],
                radius=6 if wide else 3,
                accent_left=ET["accent"] if sel else None,
                accent_width=3 if wide else 2,
            )

            item = active_list[idx]
            if tab_key == "tools":
                t_lbl, _t_act, t_desc = item
                d.text((list_x0 + (10 if wide else 5), ry + (5 if large else 3)), rj_theme.truncate_text(d, t_lbl, list_x1 - list_x0 - 18, f["body_bold"]), font=f["body_bold"], fill=ET["accent_bright"] if sel else ET["text_bright"])
                if large:
                    d.text((list_x0 + 10, ry + 20), rj_theme.truncate_text(d, t_desc, list_x1 - list_x0 - 18, f["sub"]), font=f["sub"], fill=ET["text_muted"])
            else:
                # Device or stream card with badge + inline volume bar!
                is_def = bool(item.get("is_default"))
                muted = bool(item.get("muted"))
                vol = int(item.get("volume_pct", 70))
                lx = list_x0 + (10 if wide else 4)
                rx_card = list_x1 - (8 if wide else 3)

                # Right-side status badge ([ACTIVE] or [MUTE] or % readout)
                right_tag = "MUTE" if muted else f"{vol}%"
                rb = rj_theme.measure_text(f["badge"], right_tag, d)
                rw_t = (rb[2] - rb[0]) + (8 if wide else 4)
                rj_theme.draw_pill(
                    d, rx_card - rw_t, ry + (5 if large else 2), right_tag, f["badge"],
                    bg=ET["bg_well"], fg=ET["warn"] if muted else (ET["accent_bright"] if sel else ET["text_primary"]),
                    outline=ET["border_subtle"], pad_x=4 if wide else 2, pad_y=1, radius=3,
                )
                rx_card -= rw_t + 5

                if is_def and wide:
                    ab = rj_theme.measure_text(f["badge"], "ACTIVE", d)
                    aw_t = (ab[2] - ab[0]) + 8
                    rj_theme.draw_pill(
                        d, rx_card - aw_t, ry + (5 if large else 2), "ACTIVE", f["badge"],
                        bg="#1b2d22", fg=ET["ok"], outline=ET["ok"], pad_x=4, pad_y=1, radius=3,
                    )
                    rx_card -= aw_t + 5

                if wide:
                    k_lbl = "APP" if tab_key == "streams" else aud.kind_badge(item.get("kind", "audio"))
                    kw_t, _ = rj_theme.draw_pill(
                        d, lx, ry + (5 if large else 2), k_lbl, f["badge"],
                        bg=ET["bg_well"], fg=ET["accent_bright"] if is_def else ET["text_muted"],
                        outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3,
                    )
                    lx += kw_t + 6

                nm = str(item.get("label") or item.get("name") or "Audio")
                nm_avail = max(24, rx_card - lx - 4)
                d.text((lx, ry + (5 if large else 2)), rj_theme.truncate_text(d, nm, nm_avail, f["body_bold"] if wide else f["body"]), font=f["body_bold"] if wide else f["body"], fill=ET["text_bright"] if sel else ET["text_primary"])

                # Inline volume meter bar
                bar_y = ry + (27 if large else (17 if wide else 13))
                bar_h = 7 if large else (5 if wide else 3)
                if bar_y + bar_h < ry1:
                    _draw_vol_bar(d, (list_x0 + (10 if wide else 4), bar_y, list_x1 - (8 if wide else 4), bar_y + bar_h), vol, muted, radius=3 if large else 1)

        if total > vis:
            rj_theme.draw_scrollbar(d, list_x1 + 2, y0, y1, st, vis, total, width=4 if wide else 2)

    # 4. Right Inspector & Live Mixer Pane (HDMI widescreen)
    if split:
        px0 = list_x1 + 10
        px1 = width - 8
        rj_theme.draw_card(d, (px0, y0, px1, y1), fill=ET["bg_surface"], outline=ET["border_subtle"], radius=7)
        ip = 10
        iy = y0 + ip
        iw = px1 - px0 - ip * 2

        if tab_key == "tools" or total == 0:
            # Audio Stack Diagnostics Summary
            d.text((px0 + ip, iy), "AUDIO STATUS", font=f["badge"], fill=ET["accent"])
            iy += 18
            rows_diag = [
                ("Server", str(backend_info.get("backend", "ALSA"))),
                ("Output", (def_out.get("label", "None") if def_out else "None")),
                ("Volume", "MUTED" if m_muted else f"{m_vol}%"),
                ("Inputs", f"{len(inputs)} detected"),
                ("Streams", f"{len(streams)} active"),
            ]
            if backend_info.get("user"):
                rows_diag.append(("Session", f"{backend_info['user']} (UID {backend_info.get('uid', 0)})"))
            for rk, rv in rows_diag:
                if iy + 16 > y1 - 28:
                    break
                d.text((px0 + ip, iy), f"{rk}:", font=f["sub"], fill=ET["text_muted"])
                d.text((px0 + ip + 56, iy), rj_theme.truncate_text(d, rv, iw - 56, f["sub"]), font=f["sub"], fill=ET["text_bright"])
                iy += 18
            d.text((px0 + ip, y1 - 24), "T: Test Chime   B: Bluetooth", font=f["sub"], fill=ET["accent_bright"])
        else:
            sel_item = active_list[cur_clamped]
            s_lbl = str(sel_item.get("label") or sel_item.get("name") or "Audio")
            s_vol = int(sel_item.get("volume_pct", 70))
            s_muted = bool(sel_item.get("muted", False))
            s_def = bool(sel_item.get("is_default", False))

            hdr_tag = "ACTIVE STREAM" if tab_key == "streams" else ("DEFAULT OUTPUT" if (tab_key == "outputs" and s_def) else ("DEFAULT MIC" if (tab_key == "inputs" and s_def) else ("OUTPUT DEVICE" if tab_key == "outputs" else "INPUT DEVICE")))
            d.text((px0 + ip, iy), hdr_tag, font=f["badge"], fill=ET["ok"] if s_def else ET["accent"])
            iy += 18

            # Wrapped title (up to 2 lines)
            wrapped = rj_theme.wrap_text(d, s_lbl, iw, f["body_bold"])
            for ln in wrapped[:2]:
                d.text((px0 + ip, iy), ln, font=f["body_bold"], fill=ET["text_bright"])
                iy += 16
            iy += 4

            # Big volume gauge card
            gauge_y0 = iy
            gauge_y1 = gauge_y0 + 52
            rj_theme.draw_card(d, (px0 + ip, gauge_y0, px1 - ip, gauge_y1), fill=ET["bg_well"], outline=ET["border_accent"] if not s_muted else ET["warn"], radius=6)
            d.text((px0 + ip + 8, gauge_y0 + 8), "LEVEL", font=f["badge"], fill=ET["text_muted"])
            v_big = "MUTED" if s_muted else f"{s_vol}%"
            d.text((px1 - ip - 8, gauge_y0 + 6), v_big, font=f["title"], fill=ET["warn"] if s_muted else ET["accent_bright"], anchor="ra")
            _draw_vol_bar(d, (px0 + ip + 8, gauge_y0 + 30, px1 - ip - 8, gauge_y0 + 42), s_vol, s_muted, radius=4)
            iy = gauge_y1 + 10

            # Metadata details
            meta_rows = [
                ("Backend", str(sel_item.get("backend", "alsa")).upper()),
                ("State", "PAUSED" if sel_item.get("corked") else str(sel_item.get("state", "READY"))),
            ]
            if sel_item.get("active_port"):
                meta_rows.append(("Port", str(sel_item["active_port"])))
            for mk, mv in meta_rows:
                if iy + 14 > y1 - 36:
                    break
                d.text((px0 + ip, iy), f"{mk}:", font=f["sub"], fill=ET["text_muted"])
                d.text((px0 + ip + 54, iy), rj_theme.truncate_text(d, mv, iw - 54, f["sub"]), font=f["sub"], fill=ET["text_primary"])
                iy += 16

            # Action hints at bottom of inspector
            act_line1 = "<- / -> : Volume   M: Mute"
            act_line2 = "Enter: Move to Output" if tab_key == "streams" else ("Enter: Set Default   T: Test" if tab_key == "outputs" else "Enter: Set Default Mic")
            d.text((px0 + ip, y1 - 34), rj_theme.truncate_text(d, act_line1, iw, f["sub"]), font=f["sub"], fill=ET["text_muted"])
            d.text((px0 + ip, y1 - 18), rj_theme.truncate_text(d, act_line2, iw, f["sub"]), font=f["sub"], fill=ET["accent_bright"])

    # 5. Toast notification banner (non-blocking; anchored over list pane on split layouts)
    if toast and not overlay:
        tb = rj_theme.measure_text(f["body_bold"], toast, d)
        max_toast_w = (list_x1 - list_x0 - 8) if split else (width - 20)
        tw_b = min(max_toast_w, (tb[2] - tb[0]) + 24)
        th_b = 26 if wide else 18
        toast_cx = (list_x0 + list_x1) // 2 if split else (width // 2)
        tx0_b = toast_cx - tw_b // 2
        ty0_b = height - ftr_h - th_b - 6
        out_c = ET["danger"] if toast_kind == "err" else (ET["ok"] if toast_kind == "ok" else ET["border_accent"])
        rj_theme.draw_card(d, (tx0_b, ty0_b, tx0_b + tw_b, ty0_b + th_b), fill=ET["bg_elevated"], outline=out_c, radius=5)
        d.text((toast_cx, ty0_b + th_b // 2), rj_theme.truncate_text(d, toast, tw_b - 12, f["body_bold"]), font=f["body_bold"], fill=ET["text_bright"], anchor="mm")

    # 6. Modal overlay (card/profile picker or stream sink picker)
    if overlay:
        ov_title = str(overlay.get("title", "SELECT"))
        ov_items = overlay.get("items", [])
        ov_sel = int(overlay.get("sel", 0))
        bw = min(width - 24, 380 if large else (width - 12))
        bh = min(height - 32, 220 if large else (height - 18))
        bx0 = (width - bw) // 2
        by0 = (height - bh) // 2
        rj_theme.draw_card(d, (bx0, by0, bx0 + bw, by0 + bh), fill=ET["bg_elevated"], outline=ET["accent"], radius=8 if wide else 4, width=2)
        d.text((bx0 + 12, by0 + 10), rj_theme.truncate_text(d, ov_title, bw - 24, f["title"]), font=f["title"], fill=ET["accent_bright"])
        d.line([(bx0 + 8, by0 + (30 if large else 22)), (bx0 + bw - 8, by0 + (30 if large else 22))], fill=ET["border_subtle"], width=1)
        oy0 = by0 + (36 if large else 26)
        oy1 = by0 + bh - 8
        orow = 26 if large else 18
        ovis = max(1, (oy1 - oy0) // orow)
        ost = max(0, min(ov_sel - ovis // 2, max(0, len(ov_items) - ovis)))
        for s_i in range(ovis):
            idx_o = ost + s_i
            if idx_o >= len(ov_items):
                break
            olbl = str(ov_items[idx_o][0])
            ory = oy0 + s_i * orow
            is_s = (idx_o == ov_sel)
            if is_s:
                rj_theme.draw_card(d, (bx0 + 8, ory, bx0 + bw - 8, ory + orow - 3), fill=ET["select_bg"], outline=ET["border_accent"], radius=4, accent_left=ET["accent"], accent_width=2)
            d.text((bx0 + 16, ory + (5 if large else 2)), rj_theme.truncate_text(d, olbl, bw - 30, f["body_bold"] if is_s else f["body"]), font=f["body_bold"] if is_s else f["body"], fill=ET["accent_bright"] if is_s else ET["text_primary"])

    # 7. Footer hint bar
    d.rectangle((0, height - ftr_h, width - 1, height - 1), fill=ET["bg_elevated"])
    d.line([(0, height - ftr_h), (width - 1, height - ftr_h)], fill=ET["border_subtle"], width=1)
    if large:
        hint = "Tab:Section   ^v:Select   <>:Vol   Enter:Apply   M:Mute   T:Test   B:Bluetooth   R:Refresh   Esc:Exit"
    elif wide:
        hint = "Tab:Tab  ^v:Pick  <>:Vol  OK:Set  M:Mute  T:Test  Esc:Back"
    else:
        hint = "<>:Vol OK:Set K1:Mute K3:Back"
    d.text((width // 2, height - ftr_h + ftr_h // 2), rj_theme.truncate_text(d, hint, width - 8, f["sub"]), font=f["sub"], fill=ET["text_muted"], anchor="mm")
    return img


def main() -> int:
    import RPi.GPIO as GPIO
    import LCD_1in44
    try:
        import rj_keyboard
    except Exception:
        rj_keyboard = None
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
    fonts = _load_fonts(W, H)

    running = True

    def _on_sig(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _on_sig)
    signal.signal(signal.SIGTERM, _on_sig)

    tab_idx = 0
    cursors = [0, 0, 0, 0]
    scrolls = [0, 0, 0, 0]
    outputs: List[dict] = []
    inputs: List[dict] = []
    streams: List[dict] = []
    backend_info: dict = {}
    toast = ""
    toast_kind = "info"
    toast_until = 0.0

    def _set_toast(msg: str, kind: str = "info", secs: float = 2.6) -> None:
        nonlocal toast, toast_kind, toast_until
        toast = msg
        toast_kind = kind
        toast_until = time.monotonic() + secs

    def _refresh_all() -> None:
        nonlocal outputs, inputs, streams, backend_info
        backend_info = aud.get_audio_backend_status()
        outputs = aud.list_output_devices()
        inputs = aud.list_input_devices()
        streams = aud.list_active_streams()

    def _render(overlay: Optional[dict] = None) -> None:
        now = time.monotonic()
        frame = render_audio_manager_frame(
            W, H, tab_idx=tab_idx, outputs=outputs, inputs=inputs, streams=streams,
            cursor=cursors[tab_idx], scroll=scrolls[tab_idx], backend_info=backend_info,
            toast=(toast if now < toast_until else ""), toast_kind=toast_kind,
            overlay=overlay, fonts=fonts,
        )
        lcd.LCD_ShowImage(frame, 0, 0)

    def _run_modal(title: str, items: List[Tuple[str, object]]):
        if not items:
            return None
        sel = 0
        dirty_m = True
        flush_input()
        while running:
            if dirty_m:
                _render({"title": title, "items": items, "sel": sel})
                dirty_m = False
            act, ch = poll_ui_input(PINS, GPIO)
            if act == "UP":
                sel = (sel - 1) % len(items)
                dirty_m = True
            elif act == "DOWN":
                sel = (sel + 1) % len(items)
                dirty_m = True
            elif act in ("OK", "ENTER"):
                flush_input()
                return items[sel][1]
            elif act in ("KEY3", "LEFT", "ESCAPE", "BACKSPACE") or (ch and ch.lower() == "q"):
                flush_input()
                return None
            time.sleep(0.03)
        return None

    def _open_bt_manager() -> None:
        bt_script = os.path.join(rj_env.get_base_dir(), "payloads", "utilities", "bluetooth_manager.py")
        if os.path.isfile(bt_script):
            subprocess.run([sys.executable, bt_script], cwd=rj_env.get_base_dir())
            if rj_keyboard is not None:
                rj_keyboard.set_text_mode(True)
            lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
            flush_input()
            _refresh_all()

    def _run_profile_picker() -> None:
        cards = aud.list_cards_with_profiles()
        if not cards:
            _set_toast("No switchable profiles (needs PipeWire/Pulse)", kind="err")
            return
        card = cards[0] if len(cards) == 1 else _run_modal("SELECT SOUND CARD", [(c["label"], c) for c in cards])
        if not isinstance(card, dict):
            return
        p_opts = [
            (("* " if p["name"] == card.get("active_profile") else "  ") + p["label"], p["name"])
            for p in card.get("profiles", [])
        ]
        chosen = _run_modal(f"PROFILE: {card['label'][:18]}", p_opts)
        if chosen:
            ok, msg = aud.set_card_profile(card["name"], str(chosen))
            _refresh_all()
            _set_toast(msg, kind="ok" if ok else "err")

    try:
        with text_input_mode():
            _refresh_all()
            dirty = True
            last_active_at = time.monotonic()
            last_toast_visible = False
            flush_input()

            while running:
                now = time.monotonic()
                toast_vis = bool(toast and now < toast_until)
                if toast_vis != last_toast_visible:
                    last_toast_visible = toast_vis
                    dirty = True

                tab_key = TABS[tab_idx][0]
                cur_list = outputs if tab_key == "outputs" else (inputs if tab_key == "inputs" else (streams if tab_key == "streams" else TOOLS_ITEMS))
                total = len(cur_list)
                if total > 0:
                    cursors[tab_idx] = max(0, min(cursors[tab_idx], total - 1))
                else:
                    cursors[tab_idx] = 0
                    scrolls[tab_idx] = 0

                if dirty:
                    _render()
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO)
                if act is None and ch is None:
                    time.sleep(0.025 if (now - last_active_at) < 1.5 else 0.05)
                    continue

                last_active_at = time.monotonic()
                dirty = True
                kl = ch.lower() if isinstance(ch, str) else ""

                if act in ("KEY3", "ESCAPE", "BACKSPACE") or kl == "q":
                    break
                elif act == "TAB" or kl == "]":
                    tab_idx = (tab_idx + 1) % len(TABS)
                elif kl == "[":
                    tab_idx = (tab_idx - 1) % len(TABS)
                elif ch in ("1", "2", "3", "4"):
                    tab_idx = int(ch) - 1
                elif kl == "r":
                    _refresh_all()
                    _set_toast("Refreshed audio devices", kind="ok", secs=1.8)
                elif kl == "b":
                    _open_bt_manager()
                elif kl == "t" or act == "KEY2":
                    _set_toast("Playing test chime...", kind="info", secs=1.5)
                    _render()
                    ok, msg = aud.play_test_tone("both")
                    _set_toast(msg, kind="ok" if ok else "err")
                elif act == "UP" and total > 0:
                    cursors[tab_idx] = (cursors[tab_idx] - 1) % total
                    if cursors[tab_idx] < scrolls[tab_idx]:
                        scrolls[tab_idx] = cursors[tab_idx]
                elif act == "DOWN" and total > 0:
                    cursors[tab_idx] = (cursors[tab_idx] + 1) % total
                    vis_est = 4 if fonts["large"] else 3
                    if cursors[tab_idx] >= scrolls[tab_idx] + vis_est:
                        scrolls[tab_idx] = cursors[tab_idx] - vis_est + 1
                elif (act == "LEFT" or ch in ("-", "_")) and total > 0 and tab_key != "tools":
                    it = cur_list[cursors[tab_idx]]
                    new_v = max(0, int(it.get("volume_pct", 70)) - 5)
                    if tab_key == "outputs":
                        it["volume_pct"] = aud.set_output_volume(it, new_v)
                        it["muted"] = False
                    elif tab_key == "inputs":
                        it["volume_pct"] = aud.set_input_volume(it, new_v)
                        it["muted"] = False
                    elif tab_key == "streams":
                        it["volume_pct"] = aud.set_stream_volume(it["id"], new_v)
                        it["muted"] = False
                elif (act == "RIGHT" or ch in ("+", "=")) and total > 0 and tab_key != "tools":
                    it = cur_list[cursors[tab_idx]]
                    new_v = int(it.get("volume_pct", 70)) + 5
                    if tab_key == "outputs":
                        it["volume_pct"] = aud.set_output_volume(it, new_v)
                        it["muted"] = False
                    elif tab_key == "inputs":
                        it["volume_pct"] = aud.set_input_volume(it, new_v)
                        it["muted"] = False
                    elif tab_key == "streams":
                        it["volume_pct"] = aud.set_stream_volume(it["id"], new_v)
                        it["muted"] = False
                elif (kl == "m" or act == "KEY1") and total > 0 and tab_key != "tools":
                    it = cur_list[cursors[tab_idx]]
                    new_m = not bool(it.get("muted", False))
                    if tab_key == "outputs":
                        it["muted"] = aud.set_output_mute(it, new_m)
                    elif tab_key == "inputs":
                        it["muted"] = aud.set_input_mute(it, new_m)
                    elif tab_key == "streams":
                        it["muted"] = aud.set_stream_mute(it["id"], new_m)
                    _set_toast("Muted" if new_m else "Unmuted", kind="info", secs=1.5)
                elif act in ("OK", "ENTER") and total > 0:
                    it = cur_list[cursors[tab_idx]]
                    if tab_key == "outputs":
                        ok, msg = aud.set_default_output(it)
                        _refresh_all()
                        _set_toast(msg, kind="ok" if ok else "err")
                    elif tab_key == "inputs":
                        ok, msg = aud.set_default_input(it)
                        _refresh_all()
                        _set_toast(msg, kind="ok" if ok else "err")
                    elif tab_key == "streams":
                        if outputs:
                            s_opts = [(("* " if o.get("is_default") else "  ") + o["label"], o) for o in outputs]
                            picked = _run_modal("MOVE STREAM TO OUTPUT", s_opts)
                            if isinstance(picked, dict):
                                ok, msg = aud.move_stream_to_sink(it["id"], picked["name"])
                                _refresh_all()
                                _set_toast(msg, kind="ok" if ok else "err")
                    elif tab_key == "tools":
                        t_act = it[1]
                        if t_act == "test_both":
                            ok, msg = aud.play_test_tone("both")
                            _set_toast(msg, kind="ok" if ok else "err")
                        elif t_act == "test_left":
                            ok, msg = aud.play_test_tone("left")
                            _set_toast(msg, kind="ok" if ok else "err")
                        elif t_act == "test_right":
                            ok, msg = aud.play_test_tone("right")
                            _set_toast(msg, kind="ok" if ok else "err")
                        elif t_act == "profiles":
                            _run_profile_picker()
                        elif t_act == "fix_bt_a2dp":
                            import rj_bluetooth
                            _set_toast("Fixing BT A2DP & disabling BlueALSA...", kind="info", secs=3.0)
                            _render()
                            ok, msg = rj_bluetooth.fix_bluetooth_a2dp_conflicts()
                            _refresh_all()
                            _set_toast(msg, kind="ok" if ok else "err", secs=4.0)
                        elif t_act == "bt_mgr":
                            _open_bt_manager()
                        elif t_act == "restart":
                            _set_toast("Restarting audio stack...", kind="info", secs=2.5)
                            _render()
                            ok, msg = aud.restart_audio_stack()
                            _refresh_all()
                            _set_toast(msg, kind="ok" if ok else "err")
                        elif t_act == "refresh":
                            _refresh_all()
                            _set_toast("Audio devices refreshed", kind="ok", secs=1.8)
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
