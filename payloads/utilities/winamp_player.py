#!/usr/bin/env python3
"""
EmberJack Payload -- Local Music Player (Winamp / EmberAmp)
===========================================================
Author: 7h30th3r0n3

Obsidian Ember local music player with real-time FFT spectrum analyzer,
widescreen Now Playing + Playlist split pane, and Bluetooth speaker pairing.

Controls (Browser):
  UP / DOWN         Select folder or audio file
  OK / Enter / R    Open folder / Play track
  LEFT / Backspace  Parent directory
  KEY2 / B          Bluetooth Speaker menu
  KEY3 / Esc / Q    Back / Exit

Controls (Now Playing):
  OK / Enter / Spc  Play / Pause
  RIGHT / N         Next track
  LEFT / P          Previous track (or restart if > 3s)
  UP / DOWN (+/-)   Volume +/- 5%
  KEY1 / S          Toggle Shuffle
  KEY2 / R          Toggle Repeat
  KEY3 / Esc / Q    Back to browser
"""

import os
import random
import signal
import subprocess
import sys
import time

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import LCD_Config
import numpy as np
from PIL import Image, ImageDraw

import rj_env
import rj_theme
from payloads._audio_helper import set_playback_volume
from payloads._input_helper import flush_input, poll_ui_input, text_input_mode

PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
for p in PINS.values():
    GPIO.setup(p, GPIO.IN, pull_up_down=GPIO.PUD_UP)

LCD = LCD_1in44.LCD()
LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
W, H = LCD.width, LCD.height
FONTS = rj_theme.load_app_fonts(W, H)
IS_WIDE = bool(FONTS["wide"])
IS_LARGE = bool(FONTS["large"])
ET = rj_theme.EMBER_THEME

AUDIO_EXT = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".opus"}
START_DIR = os.path.join(rj_env.get_base_dir(), "loot")
DEBOUNCE = 0.14
_running = True
_volume = 40
_shuffle = False
_repeat = False
_audio_proc = None
_pcm_proc = None

# Warm Obsidian Ember spectrum gradient (bottom -> top)
SPEC_GRADIENT = [
    (112, 58, 24),
    (140, 74, 28),
    (176, 92, 36),
    (204, 108, 42),
    (224, 122, 47),
    (234, 138, 60),
    (242, 153, 74),
    (238, 166, 72),
    (230, 168, 69),
    (217, 91, 85),
]

NUM_BARS = 24 if IS_LARGE else (19 if IS_WIDE else 14)
_peak_vals = [0.0] * NUM_BARS
_peak_decay = 0.91
_title_scroll_offset = 0
_last_marquee_t = 0.0

FONT_TIMER = rj_env.load_font(22 if IS_LARGE else (15 if IS_WIDE else 12), bold=True, mono=True)


def _sig(s, f):
    global _running
    _running = False


signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)


def _set_vol(vol):
    global _volume
    _volume = max(0, min(100, int(vol)))
    set_playback_volume(int(_volume * 63 / 100))
    _tpa_enable()


# ---------------------------------------------------------------------------
# Audio + PCM for spectrum
# ---------------------------------------------------------------------------
def _tpa_enable():
    try:
        import smbus2
        bus = smbus2.SMBus(1)
        bus.write_byte_data(0x60, 0x01, 0xC0, force=True)
        bus.close()
    except Exception:
        pass


def _start_audio(path):
    global _audio_proc, _pcm_proc
    _stop_audio()
    _audio_proc = subprocess.Popen(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
         "-fflags", "nobuffer", "-analyzeduration", "0", "-probesize", "32768", path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _pcm_proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "quiet",
         "-fflags", "nobuffer", "-analyzeduration", "0", "-probesize", "32768",
         "-i", path, "-f", "s16le", "-ac", "1", "-ar", "8000", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )


def _stop_audio():
    global _audio_proc, _pcm_proc
    for p in (_audio_proc, _pcm_proc):
        if p:
            try:
                p.kill()
                p.wait(timeout=1)
            except Exception:
                pass
    _audio_proc = _pcm_proc = None
    subprocess.run(["pkill", "-9", "aplay"], capture_output=True)


def _audio_playing():
    return _audio_proc is not None and _audio_proc.poll() is None


def _get_spectrum(n_bars=NUM_BARS):
    if not _pcm_proc or _pcm_proc.poll() is not None:
        return [0.0] * n_bars
    try:
        raw = _pcm_proc.stdout.read(512)
        if not raw or len(raw) < 512:
            return [0.0] * n_bars
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        fft = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
        fft_db = 20 * np.log10(fft + 1e-10)
        bins = np.array_split(fft_db[:len(fft_db) // 2], n_bars)
        return [max(0.0, min(1.0, float(np.max(b) + 50.0) / 50.0)) for b in bins]
    except Exception:
        return [0.0] * n_bars


def _get_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=3)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _fmt_time(s):
    if not s or s < 0:
        s = 0
    return f"{int(s) // 60:02d}:{int(s) % 60:02d}"


def _human_size(sz):
    for u in ("B", "KB", "MB", "GB"):
        if sz < 1024:
            return f"{sz:.0f}{u}"
        sz /= 1024
    return f"{sz:.0f}TB"


# ---------------------------------------------------------------------------
# Native Obsidian Ember Now Playing + Playlist Renderer
# ---------------------------------------------------------------------------
def _draw_spectrum_well(d, box, spectrum_bars):
    global _peak_vals
    x0, y0, x1, y1 = box
    rj_theme.draw_card(d, box, fill=ET["bg_well"], outline=ET["border_subtle"], radius=6 if IS_WIDE else 3)

    n = len(spectrum_bars)
    if len(_peak_vals) != n:
        _peak_vals = [0.0] * n

    pad_x = 8 if IS_LARGE else (4 if IS_WIDE else 3)
    pad_y = 6 if IS_LARGE else (4 if IS_WIDE else 2)
    inner_w = max(8, (x1 - x0) - pad_x * 2)
    inner_h = max(8, (y1 - y0) - pad_y * 2)
    gap = 3 if IS_LARGE else (2 if IS_WIDE else 1)
    bar_w = max(2, (inner_w - gap * (n - 1)) // max(1, n))
    total_bw = bar_w * n + gap * (n - 1)
    start_x = x0 + pad_x + max(0, (inner_w - total_bw) // 2)
    base_y = y1 - pad_y

    # Subtle horizontal grid lines inside spectrum well
    if IS_WIDE:
        for frac in (0.33, 0.66):
            gy = int(base_y - inner_h * frac)
            d.line([(x0 + 4, gy), (x1 - 4, gy)], fill=ET["bg_surface"], width=1)

    seg_h = 3 if IS_LARGE else 2
    seg_gap = 1
    n_grad = len(SPEC_GRADIENT)

    for i, val in enumerate(spectrum_bars):
        bx = start_x + i * (bar_w + gap)
        if val > _peak_vals[i]:
            _peak_vals[i] = val
        else:
            _peak_vals[i] *= _peak_decay

        bh = int(val * inner_h)
        y = base_y
        while base_y - y < bh:
            top = max(base_y - bh, y - seg_h)
            pct = (base_y - top) / max(1, inner_h)
            ci = min(n_grad - 1, int(pct * n_grad))
            d.rectangle((bx, top, bx + bar_w - 1, y - 1), fill=SPEC_GRADIENT[ci])
            y = top - seg_gap

        peak_h = int(_peak_vals[i] * inner_h)
        if peak_h > 2:
            py = max(y0 + pad_y, base_y - peak_h)
            d.rectangle((bx, py, bx + bar_w - 1, py + (2 if IS_LARGE else 1)), fill=ET["text_bright"])


def _draw_player(playlist, order, idx, duration, elapsed, playing, paused, spectrum_bars):
    global _title_scroll_offset, _last_marquee_t

    track_path = playlist[order[idx]]
    track_name = os.path.splitext(os.path.basename(track_path))[0]
    ext_lbl = os.path.splitext(track_path)[1].lstrip(".").upper() or "AUDIO"
    total = len(playlist)

    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)

    state_badge = "PAUSED" if paused else ("PLAY" if playing else "STOP")
    hdr_h = rj_theme.draw_app_header(
        d, W, "EMBERJACK AUDIO" if IS_WIDE else "AUDIO", FONTS,
        badge=state_badge,
        right_text=f"VOL {_volume}%" if IS_LARGE else "",
        right_pill=f"{idx + 1}/{total}",
    )
    ftr_h = rj_theme.draw_app_footer(
        d, W, H,
        "Space/OK:Play  Left/Right:Track  Up/Dn:Vol  K1/S:Shuf  K2/R:Rpt  Esc/K3:List" if IS_LARGE else (
            "OK:Play L/R:Trk U/D:Vol K1:Shf K2:Rpt K3:Back" if IS_WIDE else "OK:P L/R:T K3:B"
        ),
        FONTS,
    )

    pad = 8 if IS_LARGE else (5 if IS_WIDE else 3)
    body_y0 = hdr_h + pad
    body_y1 = H - ftr_h - pad
    split = W >= 480

    # Marquee tick
    now_m = time.monotonic()
    if playing and not paused and (now_m - _last_marquee_t) > 0.22:
        _title_scroll_offset += 1
        _last_marquee_t = now_m

    left_x0 = pad
    left_x1 = int(W * 0.59) - 4 if split else (W - pad)

    # --- Left / Main Now Playing Card ---
    rj_theme.draw_card(
        d, (left_x0, body_y0, left_x1, body_y1),
        fill=ET["bg_surface"], outline=ET["border_subtle"],
        radius=8 if IS_WIDE else 4,
        accent_left=ET["accent"] if (playing and not paused) else ET["warn"],
        accent_width=3 if IS_WIDE else 2,
    )

    ix0 = left_x0 + (14 if IS_LARGE else (8 if IS_WIDE else 5))
    ix1 = left_x1 - (12 if IS_LARGE else (7 if IS_WIDE else 4))
    iw = max(30, ix1 - ix0)
    cy = body_y0 + (10 if IS_LARGE else (5 if IS_WIDE else 4))

    # Top row inside card: Timer + Format/Status pills
    time_str = _fmt_time(elapsed)
    d.text((ix0, cy), time_str, font=FONT_TIMER, fill=ET["accent_bright"])

    rx = ix1
    if IS_WIDE:
        for pill_lbl, active, col_fg in (
            ("RPT", _repeat, ET["ok"]),
            ("SHF", _shuffle, ET["accent_bright"]),
            (ext_lbl, False, ET["text_muted"]),
        ):
            pb = rj_theme.measure_text(FONTS["badge"], pill_lbl, d)
            pw = (pb[2] - pb[0]) + 8
            rj_theme.draw_pill(
                d, rx - pw, cy + (4 if IS_LARGE else 1), pill_lbl, FONTS["badge"],
                bg=ET["select_bg"] if active else ET["bg_well"],
                fg=col_fg if active else ET["text_dim"],
                outline=ET["border_accent"] if active else ET["border_subtle"],
                pad_x=4, pad_y=1, radius=3,
            )
            rx -= pw + 4
    else:
        rem_lbl = f"-{_fmt_time(max(0, duration - elapsed))}" if duration > 0 else f"V{_volume}%"
        rb = rj_theme.measure_text(FONTS["sub"], rem_lbl, d)
        d.text((rx - (rb[2] - rb[0]), cy + 2), rem_lbl, font=FONTS["sub"], fill=ET["text_muted"])

    cy += 30 if IS_LARGE else (20 if IS_WIDE else 16)

    # Track title (marquee if too wide)
    t_font = FONTS["title"] if IS_LARGE else FONTS["body_bold"]
    tb = rj_theme.measure_text(t_font, track_name, d)
    if (tb[2] - tb[0]) > iw:
        loop_s = track_name + "   •   "
        off = _title_scroll_offset % len(loop_s)
        shown = rj_theme.truncate_text(d, loop_s[off:] + loop_s, iw, t_font, ellipsis="")
    else:
        shown = track_name
    d.text((ix0, cy), shown, font=t_font, fill=ET["text_bright"])
    cy += 22 if IS_LARGE else (14 if IS_WIDE else 12)

    # Bottom controls block heights
    vol_block_h = 30 if IS_LARGE else (18 if IS_WIDE else 14)
    seek_block_h = 26 if IS_LARGE else (16 if IS_WIDE else 13)
    spec_y1 = body_y1 - vol_block_h - seek_block_h - (10 if IS_LARGE else 6)
    if spec_y1 > cy + (20 if IS_WIDE else 12):
        _draw_spectrum_well(d, (ix0, cy, ix1, spec_y1), spectrum_bars)
        cy = spec_y1 + (8 if IS_LARGE else 4)

    # Seek / Progress bar + timestamps
    prog = min(1.0, elapsed / duration) if duration > 0 else 0.0
    bar_h = 7 if IS_LARGE else (5 if IS_WIDE else 4)
    rj_theme.draw_progress_bar(
        d, (ix0, cy, ix1, cy + bar_h), prog,
        fill=ET["accent"], track=ET["bg_well"], outline=ET["border_subtle"], radius=3,
    )
    cy += bar_h + (3 if IS_LARGE else 2)
    if IS_WIDE or H >= 160:
        d.text((ix0, cy), _fmt_time(elapsed), font=FONTS["badge"], fill=ET["text_muted"])
        rem = max(0.0, duration - elapsed) if duration > 0 else 0.0
        rt = f"-{_fmt_time(rem)}  /  {_fmt_time(duration)}" if (IS_LARGE and duration > 0) else (f"-{_fmt_time(rem)}" if duration > 0 else "--:--")
        rb = rj_theme.measure_text(FONTS["badge"], rt, d)
        d.text((ix1 - (rb[2] - rb[0]), cy), rt, font=FONTS["badge"], fill=ET["text_muted"])
    cy += 16 if IS_LARGE else (11 if IS_WIDE else 8)

    # Volume bar + transport state row
    vy = min(cy + 2, body_y1 - (22 if IS_LARGE else 14))
    vol_lbl = f"VOL {_volume:3d}%"
    vb = rj_theme.measure_text(FONTS["badge"], vol_lbl, d)
    d.text((ix0, vy), vol_lbl, font=FONTS["badge"], fill=ET["accent_bright"])
    vx0 = ix0 + (vb[2] - vb[0]) + (8 if IS_WIDE else 4)
    vx1 = ix1 - (110 if IS_LARGE else 0)
    if vx1 > vx0 + 20:
        rj_theme.draw_progress_bar(
            d, (vx0, vy + (2 if IS_LARGE else 1), vx1, vy + (10 if IS_LARGE else 7)),
            _volume / 100.0,
            fill=ET["warn"] if _volume >= 85 else ET["accent"],
            track=ET["bg_well"], outline=ET["border_subtle"], radius=3,
        )
    if IS_LARGE:
        bt_txt = "BT:ON" if _bt_connected else "LOCAL"
        bb = rj_theme.measure_text(FONTS["badge"], bt_txt, d)
        bw = (bb[2] - bb[0]) + 10
        rj_theme.draw_pill(
            d, ix1 - bw, vy - 1, bt_txt, FONTS["badge"],
            bg=ET["ok_bg"] if _bt_connected else ET["bg_well"],
            fg=ET["ok"] if _bt_connected else ET["text_muted"],
            outline=ET["ok"] if _bt_connected else ET["border_subtle"],
            pad_x=5, pad_y=1, radius=3,
        )

    # --- Right Playlist Pane on Widescreen (>= 480px) ---
    if split:
        px0 = int(W * 0.59) + 4
        px1 = W - pad
        rj_theme.draw_card(
            d, (px0, body_y0, px1, body_y1),
            fill=ET["bg_surface"], outline=ET["border_subtle"],
            radius=8,
        )
        d.text((px0 + 12, body_y0 + 10), f"PLAYLIST ({total})", font=FONTS["badge"], fill=ET["accent_bright"])
        d.line([(px0 + 8, body_y0 + 26), (px1 - 8, body_y0 + 26)], fill=ET["border_subtle"], width=1)

        py0 = body_y0 + 32
        py1 = body_y1 - 8
        row_h = 26
        vis = max(1, (py1 - py0) // row_h)
        st = max(0, min(idx - vis // 2, total - vis)) if total > vis else 0

        for slot in range(vis):
            pidx = st + slot
            if pidx >= total:
                break
            ry = py0 + slot * row_h
            ry1 = ry + row_h - 3
            is_cur = (pidx == idx)
            tpath = playlist[order[pidx]]
            tname = os.path.splitext(os.path.basename(tpath))[0]

            if is_cur:
                rj_theme.draw_card(
                    d, (px0 + 6, ry, px1 - 10, ry1),
                    fill=ET["select_bg"], outline=ET["border_accent"],
                    radius=4, accent_left=ET["accent"], accent_width=2,
                )
            prefix = "> " if is_cur else f"{pidx + 1:02d} "
            lbl = rj_theme.truncate_text(
                d, prefix + tname, (px1 - px0) - 26,
                FONTS["body_bold"] if is_cur else FONTS["sub"],
            )
            d.text(
                (px0 + 12, (ry + ry1) // 2), lbl,
                font=FONTS["body_bold"] if is_cur else FONTS["sub"],
                fill=ET["accent_bright"] if is_cur else ET["text_primary"],
                anchor="lm",
            )

        rj_theme.draw_scrollbar(d, px1 - 6, py0, py1, st, vis, total, width=3)

    LCD.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# Bluetooth audio (preserved backend + Obsidian Ember UI)
# ---------------------------------------------------------------------------
_bt_connected = False
_bt_device = None


def _bt_scan_and_list():
    """Scan for BT devices using hcitool + bluetoothctl."""
    subprocess.run(["bluetoothctl", "power", "on"], capture_output=True, timeout=3)
    devices = []
    seen = set()
    try:
        r = subprocess.run(["hcitool", "scan", "--flush"], capture_output=True, text=True, timeout=12)
        for line in r.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("Scanning"):
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                mac = parts[0].strip()
                name = parts[1].strip() if parts[1].strip() != "n/a" else mac
                if mac not in seen:
                    devices.append((mac, name))
                    seen.add(mac)
    except Exception:
        pass
    try:
        scan = subprocess.Popen(["bluetoothctl", "scan", "on"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(5)
        scan.kill()
        scan.wait()
        r = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True, timeout=3)
        for line in r.stdout.strip().splitlines():
            parts = line.split(" ", 2)
            if len(parts) >= 3:
                mac, name = parts[1], parts[2]
                if mac not in seen:
                    devices.append((mac, name))
                    seen.add(mac)
    except Exception:
        pass
    return devices


def _bt_connect(mac):
    """Pair and connect to a BT audio device."""
    global _bt_connected, _bt_device
    subprocess.Popen(["pkill", "bluealsa"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    subprocess.Popen(["bluealsa", "--profile=a2dp-source", "--codec=SBC"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    subprocess.run(["bluetoothctl", "pair", mac], capture_output=True, timeout=10)
    subprocess.run(["bluetoothctl", "trust", mac], capture_output=True, timeout=5)
    r = subprocess.run(["bluetoothctl", "connect", mac], capture_output=True, text=True, timeout=10)
    if "successful" in r.stdout.lower() or "connected" in r.stdout.lower():
        _bt_connected = True
        _bt_device = mac
        time.sleep(2)
        return True
    return False


def _bt_disconnect():
    """Disconnect BT audio."""
    global _bt_connected, _bt_device
    if _bt_device:
        subprocess.run(["bluetoothctl", "disconnect", _bt_device], capture_output=True, timeout=5)
    _bt_connected = False
    _bt_device = None


def _show_bt_status(title, sub="", kind="info"):
    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)
    rj_theme.draw_app_header(d, W, "BT SPEAKER", FONTS, badge="AUDIO")
    rj_theme.draw_status_card(d, W, H, title, sub=sub, kind=kind, fonts=FONTS)
    LCD.LCD_ShowImage(img, 0, 0)


def _bt_menu():
    """Bluetooth speaker selection screen (Obsidian Ember)."""
    _show_bt_status("Scanning Bluetooth...", "Power on your speaker", kind="info")
    devices = _bt_scan_and_list()
    cursor = 0
    scroll = 0
    dirty = True

    while _running:
        if dirty:
            img = Image.new("RGB", (W, H), ET["bg_base"])
            d = ImageDraw.Draw(img)
            hdr_h = rj_theme.draw_app_header(
                d, W, "BT SPEAKER", FONTS,
                badge="LINKED" if _bt_connected else f"{len(devices)} FOUND",
            )
            ftr_h = rj_theme.draw_app_footer(
                d, W, H,
                "Enter/OK:Connect  K1:Disc  K2/R:Rescan  Esc/K3:Back" if IS_WIDE else "OK:Link K2:Scan K3:B",
                FONTS,
            )
            pad = 8 if IS_LARGE else (5 if IS_WIDE else 3)
            y0 = hdr_h + pad
            y1 = H - ftr_h - pad

            if not devices:
                rj_theme.draw_status_card(
                    d, W, H, "No Bluetooth Speakers",
                    sub="Turn on speaker & press K2 / R", kind="warn", fonts=FONTS,
                )
            else:
                row_h = 34 if IS_LARGE else (24 if IS_WIDE else 18)
                vis = max(1, (y1 - y0) // row_h)
                scroll = max(0, min(scroll, max(0, len(devices) - vis)))
                if cursor < scroll:
                    scroll = cursor
                elif cursor >= scroll + vis:
                    scroll = cursor - vis + 1

                list_x1 = W - pad - 6
                for i in range(vis):
                    idx = scroll + i
                    if idx >= len(devices):
                        break
                    mac, name = devices[idx]
                    ry = y0 + i * row_h
                    ry1 = ry + row_h - (4 if IS_WIDE else 2)
                    is_sel = idx == cursor
                    linked = _bt_device == mac
                    rj_theme.draw_card(
                        d, (pad, ry, list_x1, ry1),
                        fill=ET["select_bg"] if is_sel else ET["bg_surface"],
                        outline=ET["ok"] if linked else (ET["border_accent"] if is_sel else ET["border_subtle"]),
                        radius=6 if IS_WIDE else 3,
                        accent_left=ET["ok"] if linked else (ET["accent"] if is_sel else None),
                        accent_width=3 if IS_WIDE else 2,
                    )
                    lx = pad + (10 if IS_WIDE else 5)
                    rx = list_x1 - (8 if IS_WIDE else 4)
                    if linked and IS_WIDE:
                        pb = rj_theme.measure_text(FONTS["badge"], "CONNECTED", d)
                        pw = (pb[2] - pb[0]) + 8
                        rj_theme.draw_pill(
                            d, rx - pw, (ry + ry1) // 2 - 7, "CONNECTED", FONTS["badge"],
                            bg=ET["ok_bg"], fg=ET["ok"], outline=ET["ok"], pad_x=4, pad_y=1, radius=3,
                        )
                        rx -= pw + 6
                    lbl = rj_theme.truncate_text(d, f"{name}  ({mac})" if IS_LARGE else name, max(20, rx - lx), FONTS["body_bold"] if is_sel else FONTS["body"])
                    d.text((lx, (ry + ry1) // 2), lbl, font=FONTS["body_bold"] if is_sel else FONTS["body"], fill=ET["ok"] if linked else (ET["accent_bright"] if is_sel else ET["text_primary"]), anchor="lm")

                rj_theme.draw_scrollbar(d, list_x1 + 3, y0, y1, scroll, vis, len(devices), width=3 if IS_WIDE else 2)

            LCD.LCD_ShowImage(img, 0, 0)
            dirty = False

        act, ch = poll_ui_input(PINS, GPIO)
        if act is None and ch is None:
            time.sleep(0.03)
            continue
        kl = ch.lower() if isinstance(ch, str) else ""
        if act in ("KEY3", "ESCAPE", "BACKSPACE", "LEFT") or kl == "q":
            return
        elif act == "UP" and devices:
            cursor = (cursor - 1) % len(devices)
            dirty = True
        elif act == "DOWN" and devices:
            cursor = (cursor + 1) % len(devices)
            dirty = True
        elif act in ("OK", "ENTER") and devices:
            mac, name = devices[cursor]
            if _bt_device == mac:
                _bt_disconnect()
            else:
                _show_bt_status("Connecting...", name, kind="info")
                if not _bt_connect(mac):
                    _show_bt_status("Connection Failed", name, kind="err")
                    time.sleep(1.4)
            dirty = True
        elif act == "KEY2" or kl == "r":
            _show_bt_status("Scanning Bluetooth...", "Please wait...", kind="info")
            devices = _bt_scan_and_list()
            cursor = scroll = 0
            dirty = True
        elif act == "KEY1" and _bt_connected:
            _bt_disconnect()
            dirty = True


# ---------------------------------------------------------------------------
# File browser (Native Obsidian Ember Split-Pane)
# ---------------------------------------------------------------------------
def _list_audio(path):
    items = []
    try:
        entries = sorted(os.listdir(path))
    except OSError:
        return items
    dirs, files = [], []
    for e in entries:
        if e.startswith("."):
            continue
        full = os.path.join(path, e)
        if os.path.isdir(full):
            dirs.append({"name": e + "/", "path": full, "is_dir": True})
        elif os.path.splitext(e)[1].lower() in AUDIO_EXT:
            try:
                sz = os.path.getsize(full)
            except OSError:
                sz = 0
            files.append({"name": e, "path": full, "is_dir": False, "size": sz})
    return dirs + files


def _draw_browser(items, cursor, scroll, current_dir):
    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)

    dirname = os.path.basename(current_dir) or current_dir
    hdr_h = rj_theme.draw_app_header(
        d, W, "EMBERJACK MUSIC" if IS_WIDE else "MUSIC", FONTS,
        badge=dirname[:12],
        right_text=f"VOL {_volume}%" if IS_LARGE else "",
        right_pill="BT:ON" if _bt_connected else "LOCAL",
    )
    ftr_h = rj_theme.draw_app_footer(
        d, W, H,
        "Arrows:Nav  Enter:Play/Open  Left:Up  K2/B:Bluetooth  Esc/K3:Exit" if IS_LARGE else (
            "OK:Play  L:Up  K2:BT  K3:Exit" if IS_WIDE else "OK:Play L:Up K3:X"
        ),
        FONTS,
        right_text=f"{cursor + 1}/{len(items)}" if (IS_WIDE and items) else "",
    )

    pad = 8 if IS_LARGE else (5 if IS_WIDE else 3)
    y0 = hdr_h + pad
    y1 = H - ftr_h - pad
    split = W >= 480

    list_x0 = pad
    list_x1 = int(W * 0.60) - 4 if split else (W - pad - (6 if IS_WIDE else 4))

    if not items:
        rj_theme.draw_status_card(
            d, W, H, "No Audio Files Here",
            sub=f"Folder: {current_dir}", kind="info",
            foot="Left/Bksp: Parent folder  •  Add .mp3/.wav/.flac/.ogg", fonts=FONTS,
        )
    else:
        row_h = 32 if IS_LARGE else (22 if IS_WIDE else 16)
        vis = max(1, (y1 - y0) // row_h)
        st = max(0, min(scroll, max(0, len(items) - vis)))
        if cursor < st:
            st = cursor
        elif cursor >= st + vis:
            st = cursor - vis + 1

        for slot in range(vis):
            idx = st + slot
            if idx >= len(items):
                break
            item = items[idx]
            ry = y0 + slot * row_h
            ry1 = ry + row_h - (4 if IS_WIDE else 2)
            is_sel = idx == cursor

            rj_theme.draw_card(
                d, (list_x0, ry, list_x1, ry1),
                fill=ET["select_bg"] if is_sel else ET["bg_surface"],
                outline=ET["border_accent"] if is_sel else ET["border_subtle"],
                radius=6 if IS_WIDE else 3,
                accent_left=ET["accent"] if is_sel else None,
                accent_width=3 if IS_WIDE else 2,
            )

            lx = list_x0 + (10 if IS_WIDE else 4)
            rx = list_x1 - (8 if IS_WIDE else 3)
            if item["is_dir"]:
                tag = "DIR"
                tag_fg = ET["warn"] if is_sel else ET["accent"]
            else:
                tag = _human_size(item.get("size", 0))
                tag_fg = ET["text_muted"]

            if IS_WIDE:
                tb = rj_theme.measure_text(FONTS["badge"], tag, d)
                tw = (tb[2] - tb[0]) + 8
                rj_theme.draw_pill(
                    d, rx - tw, (ry + ry1) // 2 - ((tb[3] - tb[1]) // 2 + 2),
                    tag, FONTS["badge"],
                    bg=ET["bg_well"], fg=tag_fg, outline=ET["border_subtle"],
                    pad_x=4, pad_y=1, radius=3,
                )
                rx -= tw + 6

            disp_name = item["name"] if item["is_dir"] else os.path.splitext(item["name"])[0]
            txt = rj_theme.truncate_text(d, disp_name, max(20, rx - lx), FONTS["body_bold"] if is_sel else FONTS["body"])
            col = ET["warn"] if (item["is_dir"] and not is_sel) else (ET["accent_bright"] if is_sel else ET["text_primary"])
            d.text((lx, (ry + ry1) // 2), txt, font=FONTS["body_bold"] if is_sel else FONTS["body"], fill=col, anchor="lm")

        rj_theme.draw_scrollbar(d, list_x1 + 3, y0, y1, st, vis, len(items), width=3 if IS_WIDE else 2)

        # Right Details Pane on Widescreen
        if split:
            px0 = int(W * 0.60) + 6
            px1 = W - pad
            rj_theme.draw_card(
                d, (px0, y0, px1, y1),
                fill=ET["bg_surface"], outline=ET["border_subtle"],
                radius=8, accent_left=ET["accent_dim"], accent_width=3,
            )
            ix = px0 + 14
            iw = (px1 - px0) - 24
            iy = y0 + 14
            cur_item = items[min(cursor, len(items) - 1)]
            n_dirs = sum(1 for x in items if x["is_dir"])
            n_tracks = len(items) - n_dirs

            pill_txt = "FOLDER" if cur_item["is_dir"] else (os.path.splitext(cur_item["name"])[1].lstrip(".").upper() or "TRACK")
            rj_theme.draw_pill(
                d, ix, iy, pill_txt, FONTS["badge"],
                bg=ET["select_bg"], fg=ET["accent_bright"], outline=ET["border_accent"],
                pad_x=6, pad_y=2, radius=3,
            )
            iy += 28
            for ln in rj_theme.wrap_text(d, cur_item["name"], iw, FONTS["body_bold"])[:3]:
                d.text((ix, iy), ln, font=FONTS["body_bold"], fill=ET["text_bright"])
                iy += 17
            iy += 10
            meta_rows = [
                ("Folder", dirname),
                ("Tracks", str(n_tracks)),
                ("Subdirs", str(n_dirs)),
                ("Shuffle", "ON" if _shuffle else "OFF"),
                ("Repeat", "ON" if _repeat else "OFF"),
            ]
            if not cur_item["is_dir"]:
                meta_rows.insert(0, ("Size", _human_size(cur_item.get("size", 0))))
            for mk, mv in meta_rows:
                if iy + 18 > y1 - 44:
                    break
                d.text((ix, iy), mk, font=FONTS["sub"], fill=ET["text_muted"])
                vb = rj_theme.measure_text(FONTS["body"], mv, d)
                d.text((px1 - 12 - (vb[2] - vb[0]), iy - 1), mv, font=FONTS["body"], fill=ET["text_primary"])
                iy += 19

            vy = y1 - 36
            d.line([(ix, vy - 8), (px1 - 12, vy - 8)], fill=ET["border_subtle"], width=1)
            d.text((ix, vy), f"VOLUME  {_volume}%", font=FONTS["badge"], fill=ET["text_muted"])
            rj_theme.draw_progress_bar(
                d, (ix, vy + 15, px1 - 12, vy + 23), _volume / 100.0,
                fill=ET["accent"], track=ET["bg_well"], outline=ET["border_subtle"], radius=3,
            )

    LCD.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# Player logic
# ---------------------------------------------------------------------------
def _play_mode(playlist, start_idx=0):
    global _shuffle, _repeat, _title_scroll_offset, _peak_vals

    if not playlist:
        return

    idx = start_idx
    playing = False
    paused = False
    play_start = 0.0
    pause_offset = 0.0
    duration = 0.0
    order = list(range(len(playlist)))
    _peak_vals = [0.0] * NUM_BARS
    _title_scroll_offset = 0

    _set_vol(_volume)

    while _running:
        track = playlist[order[idx]]

        if not playing:
            duration = _get_duration(track)
            _start_audio(track)
            playing = True
            paused = False
            play_start = time.monotonic()
            pause_offset = 0.0
            _title_scroll_offset = 0

        elapsed = pause_offset if paused else pause_offset + (time.monotonic() - play_start)
        spectrum = _get_spectrum(NUM_BARS) if (playing and not paused) else [0.0] * NUM_BARS

        _draw_player(playlist, order, idx, duration, elapsed, playing, paused, spectrum)

        # Auto-advance
        if playing and not paused and not _audio_playing():
            if _repeat:
                _start_audio(track)
                play_start = time.monotonic()
                pause_offset = 0.0
            elif idx < len(playlist) - 1:
                idx += 1
                playing = False
                continue
            else:
                playing = False
                continue

        act, ch = poll_ui_input(PINS, GPIO)
        kl = ch.lower() if isinstance(ch, str) else ""

        if act in ("KEY3", "ESCAPE", "BACKSPACE") or kl == "q":
            _stop_audio()
            break
        elif act in ("OK", "ENTER") or ch == " ":
            if not paused:
                paused = True
                pause_offset += time.monotonic() - play_start
                for p in (_audio_proc, _pcm_proc):
                    if p:
                        try:
                            p.send_signal(signal.SIGSTOP)
                        except Exception:
                            pass
            else:
                paused = False
                play_start = time.monotonic()
                for p in (_audio_proc, _pcm_proc):
                    if p:
                        try:
                            p.send_signal(signal.SIGCONT)
                        except Exception:
                            pass
            time.sleep(DEBOUNCE)
        elif act == "RIGHT" or kl == "n":
            _stop_audio()
            idx = (idx + 1) % len(playlist)
            playing = False
            time.sleep(DEBOUNCE)
        elif act == "LEFT" or kl == "p":
            _stop_audio()
            if elapsed > 3:
                playing = False
            else:
                idx = (idx - 1) % len(playlist)
                playing = False
            time.sleep(DEBOUNCE)
        elif act == "UP" or ch in ("+", "="):
            _set_vol(_volume + 5)
            time.sleep(0.06)
        elif act == "DOWN" or ch in ("-", "_"):
            _set_vol(_volume - 5)
            time.sleep(0.06)
        elif act == "KEY1" or kl == "s":
            _shuffle = not _shuffle
            cur = order[idx]
            if _shuffle:
                random.shuffle(order)
                idx = order.index(cur)
            else:
                order = list(range(len(playlist)))
                idx = cur
            time.sleep(DEBOUNCE)
        elif act == "KEY2" or kl == "r":
            _repeat = not _repeat
            time.sleep(DEBOUNCE)

        time.sleep(0.03)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    _set_vol(_volume)
    current_dir = START_DIR if os.path.isdir(START_DIR) else rj_env.get_base_dir()
    cursor, scroll, dir_stack = 0, 0, []
    items = _list_audio(current_dir)
    dirty = True

    try:
        with text_input_mode():
            flush_input()
            while _running:
                if dirty:
                    _draw_browser(items, cursor, scroll, current_dir)
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO)
                if act is None and ch is None:
                    time.sleep(0.03)
                    continue

                kl = ch.lower() if isinstance(ch, str) else ""
                if act in ("KEY3", "ESCAPE") or kl == "q":
                    if dir_stack:
                        current_dir, cursor, scroll = dir_stack.pop()
                        items = _list_audio(current_dir)
                        dirty = True
                    else:
                        break
                elif act == "KEY2" or kl == "b":
                    _bt_menu()
                    dirty = True
                elif act in ("LEFT", "BACKSPACE"):
                    if dir_stack:
                        current_dir, cursor, scroll = dir_stack.pop()
                        items = _list_audio(current_dir)
                        dirty = True
                    else:
                        parent = os.path.dirname(current_dir)
                        if parent != current_dir:
                            dir_stack.append((current_dir, cursor, scroll))
                            current_dir, cursor, scroll = parent, 0, 0
                            items = _list_audio(current_dir)
                            dirty = True
                elif act == "UP" and items:
                    cursor = (cursor - 1) % len(items)
                    if cursor < scroll:
                        scroll = cursor
                    dirty = True
                elif act == "DOWN" and items:
                    cursor = (cursor + 1) % len(items)
                    dirty = True
                elif ch in ("+", "="):
                    _set_vol(_volume + 5)
                    dirty = True
                elif ch in ("-", "_"):
                    _set_vol(_volume - 5)
                    dirty = True
                elif act in ("OK", "ENTER", "RIGHT") and items and cursor < len(items):
                    item = items[cursor]
                    if item["is_dir"]:
                        dir_stack.append((current_dir, cursor, scroll))
                        current_dir, cursor, scroll = item["path"], 0, 0
                        items = _list_audio(current_dir)
                    else:
                        audio = [i["path"] for i in items if not i["is_dir"]]
                        ai = audio.index(item["path"])
                        _play_mode(audio, ai)
                        items = _list_audio(current_dir)
                    dirty = True
    finally:
        _stop_audio()
        try:
            LCD.LCD_Clear()
        except Exception:
            pass
        try:
            GPIO.cleanup()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
