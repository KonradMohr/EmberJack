#!/usr/bin/env python3
"""
RaspyJack Payload -- SDR Capture & Replay
==========================================
Author: 7h30th3r0n3

Flipper Zero-style capture and replay for ISM bands (315/433/868/915 MHz).
Record raw IQ signals, browse a capture library, and replay via rpitx.

Controls:
  OK          : Record (capture) / Select (library) / Transmit (replay)
  KEY1 (SPACE): Cycle views (Capture / Library / Replay)
  UP/DOWN     : Adjust gain (capture) / Scroll (library)
  LEFT/RIGHT  : Change frequency band
  KEY2 (BKSP) : Delete selected capture
  KEY3 (ESC)  : Exit

Requires: apt install rtl-sdr
Optional: rpitx (for TX replay)
"""

import os
import sys
import time
import signal
import subprocess
import threading
import json
from datetime import datetime
from collections import deque

import numpy as np
_RJ_BASE_DIR = os.environ.get("RJ_BASE_DIR") or os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import rj_theme
from PIL import Image, ImageDraw
from payloads._display_helper import scaled_font, SY
from payloads._input_helper import get_button
from payloads.sdr._sdr_core import SDRDevice, detect_sdr, compute_fft

_T = rj_theme.EMBER_THEME
_RGB = rj_theme.EMBER_RGB

# ---------------------------------------------------------------------------
# Pin & LCD setup
# ---------------------------------------------------------------------------
PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}
WIDTH, HEIGHT = LCD_1in44.LCD_WIDTH, LCD_1in44.LCD_HEIGHT

# ---------------------------------------------------------------------------
# Theme colors (Obsidian Ember)
# ---------------------------------------------------------------------------
COL_BG = _RGB["bg_base"]
COL_HEADER = _RGB["bg_elevated"]
COL_CAPTURE = _RGB["ok"]
COL_REPLAY = _RGB["danger"]
COL_SIGNAL = _RGB["accent_bright"]
COL_PURPLE = _RGB["accent"]
COL_MUTED = _RGB["text_muted"]
COL_DIM = _RGB["text_dim"]
COL_SELECTED = _RGB["select_bg"]

# Waterfall gradient stops: Obsidian Ember thermal palette
_WF_STOPS = [
    (0.00, (14, 14, 16)),
    (0.25, (45, 29, 18)),
    (0.50, (140, 74, 28)),
    (0.70, (224, 122, 47)),
    (0.86, (242, 153, 74)),
    (1.00, (245, 239, 232)),
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
LOOT_DIR = os.path.join(_RJ_BASE_DIR, 'loot', 'SDR', 'replay')
DEBOUNCE = 0.18
SAMPLE_RATE = 2_048_000
SIGNAL_THRESHOLD_DB = -30.0
VIEWS = ["capture", "library", "replay"]

BANDS = [
    {"name": "300 MHz", "freq": 300_000_000, "desc": "Gate/Alarm"},
    {"name": "315 MHz", "freq": 315_000_000, "desc": "US Remotes"},
    {"name": "390 MHz", "freq": 390_000_000, "desc": "Car Keys"},
    {"name": "418 MHz", "freq": 418_000_000, "desc": "EU Remote"},
    {"name": "433 MHz", "freq": 433_920_000, "desc": "EU ISM"},
    {"name": "434 MHz", "freq": 434_000_000, "desc": "EU Sensors"},
    {"name": "868 MHz", "freq": 868_000_000, "desc": "EU LoRa"},
    {"name": "915 MHz", "freq": 915_000_000, "desc": "US ISM"},
    {"name": "Custom", "freq": 433_920_000, "desc": "Manual"},
]

FREQ_STEP = 100_000  # 100 kHz step for custom frequency tuning

GAIN_STEPS = [0, 10, 20, 30, 40, 49]

# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
font = scaled_font(10)
font_sm = scaled_font(9)
font_lg = scaled_font(13)
font_xs = scaled_font(8)

# ---------------------------------------------------------------------------
# Module state (immutable replacement pattern: new dicts when updating)
# ---------------------------------------------------------------------------
_running = True
_last_btn = 0


def _sig_handler(_s, _f):
    global _running
    _running = False


signal.signal(signal.SIGINT, _sig_handler)
signal.signal(signal.SIGTERM, _sig_handler)


def _btn():
    global _last_btn
    b = get_button(PINS, GPIO)
    if b is None:
        return None
    now = time.time()
    if now - _last_btn < DEBOUNCE:
        return None
    _last_btn = now
    return b


# ---------------------------------------------------------------------------
# Waterfall LUT builder
# ---------------------------------------------------------------------------
def _interp_color(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def _build_waterfall_lut():
    lut = []
    for i in range(256):
        t = i / 255.0
        for j in range(len(_WF_STOPS) - 1):
            t0 = _WF_STOPS[j][0]
            t1 = _WF_STOPS[j + 1][0]
            if t0 <= t <= t1:
                seg_t = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                lut.append(_interp_color(_WF_STOPS[j][1], _WF_STOPS[j + 1][1], seg_t))
                break
        else:
            lut.append(_WF_STOPS[-1][1])
    return lut


_WF_LUT = _build_waterfall_lut()

# ---------------------------------------------------------------------------
# Waterfall buffer
# ---------------------------------------------------------------------------
DB_MIN = -70.0
DB_MAX = -5.0


class _WaterfallBuf:
    """Ring buffer of FFT rows for the mini waterfall display."""

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self._rows = deque(maxlen=height)

    def push(self, fft_db):
        resampled = np.interp(
            np.linspace(0, len(fft_db) - 1, self.width),
            np.arange(len(fft_db)),
            fft_db,
        )
        indices = np.clip(
            ((resampled - DB_MIN) / max(0.1, DB_MAX - DB_MIN) * 255),
            0, 255,
        ).astype(np.uint8)
        self._rows.append(indices)

    def render(self, image, x0, y0, w, h):
        rows = list(self._rows)
        if not rows:
            return
        n = min(len(rows), h)
        last_rows = rows[-n:]
        stacked = np.array(last_rows, dtype=np.uint8)
        if stacked.shape[1] != w:
            x_old = np.linspace(0, stacked.shape[1] - 1, stacked.shape[1])
            x_new = np.linspace(0, stacked.shape[1] - 1, w)
            resampled = np.zeros((n, w), dtype=np.uint8)
            for i in range(n):
                resampled[i] = np.interp(x_new, x_old, stacked[i]).astype(np.uint8)
            stacked = resampled
        lut_arr = np.array(_WF_LUT, dtype=np.uint8)
        pixels = lut_arr[stacked[::-1]]
        wf_img = Image.fromarray(pixels, "RGB")
        image.paste(wf_img, (x0, y0 + h - n))


# ---------------------------------------------------------------------------
# rpitx availability
# ---------------------------------------------------------------------------
def _rpitx_available():
    try:
        r = subprocess.run(["which", "rpitx"], capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Capture metadata helpers
# ---------------------------------------------------------------------------
def _make_capture_path(freq_hz):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    freq_mhz = freq_hz / 1e6
    filename = f"replay_{freq_mhz:.3f}MHz_{ts}.iq"
    return os.path.join(LOOT_DIR, filename)


def _write_metadata(iq_path, freq_hz, sample_rate, gain, duration):
    meta = {
        "freq_hz": freq_hz,
        "freq_mhz": freq_hz / 1e6,
        "sample_rate": sample_rate,
        "gain": gain,
        "duration_s": round(duration, 2),
        "timestamp": datetime.now().isoformat(),
        "filename": os.path.basename(iq_path),
    }
    meta_path = iq_path.replace(".iq", ".json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    return meta_path


def _read_metadata(iq_path):
    meta_path = iq_path.replace(".iq", ".json")
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r") as f:
                return json.load(f)
        except Exception:
            pass
    # Fallback: parse filename
    base = os.path.basename(iq_path)
    parts = base.replace(".iq", "").split("_")
    freq_str = ""
    for p in parts:
        if "MHz" in p:
            freq_str = p.replace("MHz", "")
            break
    freq_mhz = float(freq_str) if freq_str else 433.92
    size = os.path.getsize(iq_path)
    duration = size / (SAMPLE_RATE * 2)  # uint8 IQ = 2 bytes per sample
    return {
        "freq_hz": int(freq_mhz * 1e6),
        "freq_mhz": freq_mhz,
        "sample_rate": SAMPLE_RATE,
        "gain": 30,
        "duration_s": round(duration, 2),
        "timestamp": "",
        "filename": base,
    }


def _list_captures():
    if not os.path.isdir(LOOT_DIR):
        return []
    entries = []
    for f in sorted(os.listdir(LOOT_DIR), reverse=True):
        if f.endswith(".iq"):
            path = os.path.join(LOOT_DIR, f)
            meta = _read_metadata(path)
            entries.append({"path": path, "meta": meta})
    return entries


def _delete_capture(path):
    try:
        os.remove(path)
    except OSError:
        pass
    meta_path = path.replace(".iq", ".json")
    try:
        os.remove(meta_path)
    except OSError:
        pass


def _format_duration(seconds):
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}m{s:02d}s"


def _format_size(path):
    try:
        sz = os.path.getsize(path)
    except OSError:
        return "?"
    if sz < 1024:
        return f"{sz}B"
    if sz < 1024 * 1024:
        return f"{sz / 1024:.1f}K"
    return f"{sz / (1024 * 1024):.1f}M"


# ---------------------------------------------------------------------------
# Drawing: signal strength bar (native resolution-adaptive Obsidian Ember)
# ---------------------------------------------------------------------------
def _sdr_dims(lcd):
    w = int(getattr(lcd, "width", WIDTH) or WIDTH)
    h = int(getattr(lcd, "height", HEIGHT) or HEIGHT)
    return w, h, rj_theme.load_app_fonts(w, h)


def _draw_signal_bar(draw, db, x0, y0, w, h, fnt=None):
    """Draw a horizontal signal strength meter bar with threshold marker & dB label."""
    fill_pct = min(1.0, max(0.0, (db - DB_MIN) / (DB_MAX - DB_MIN)))
    if fill_pct < 0.35:
        bar_color = _T["accent_dim"]
    elif fill_pct < 0.60:
        bar_color = _T["accent"]
    elif fill_pct < 0.80:
        bar_color = _T["ok"]
    else:
        bar_color = _T["danger"]

    rj_theme.draw_progress_bar(
        draw, (x0, y0, x0 + w, y0 + h), fill_pct,
        fill=bar_color, track=_T["bg_well"], outline=_T["border_subtle"], radius=3,
    )
    thresh_x = x0 + 2 + int((w - 4) * max(0.0, (SIGNAL_THRESHOLD_DB - DB_MIN) / (DB_MAX - DB_MIN)))
    draw.line([(thresh_x, y0 + 1), (thresh_x, y0 + h - 1)], fill=_T["danger"], width=1)

    draw.text(
        (x0 + w + 6, y0 - 1),
        f"{db:.0f} dB",
        font=fnt or font_xs,
        fill=_T["accent_bright"] if db > SIGNAL_THRESHOLD_DB else _T["text_muted"],
    )


# ---------------------------------------------------------------------------
# Drawing: CAPTURE view
# ---------------------------------------------------------------------------
def _draw_capture(lcd, sdr, band_idx, gain_idx, recording, rec_start, wf_buf):
    w, h, fonts = _sdr_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    draw = ImageDraw.Draw(img)
    band = BANDS[band_idx]
    freq_mhz = band["freq"] / 1e6
    gain_val = GAIN_STEPS[gain_idx]

    rec_badge = f"REC {time.time() - rec_start:.1f}s" if recording else band["name"]
    hdr_h = rj_theme.draw_app_header(
        draw, w, "SDR CAPTURE", fonts,
        badge=rec_badge,
        right_text=band["desc"] if wide else "",
        right_pill=f"{freq_mhz:.3f} MHz | G:{gain_val}" if wide else "",
    )
    is_custom = band["name"] == "Custom"
    if recording:
        hint = "OK:Stop Recording   L/R:Band   K1:Views   K3:Exit" if wide else "OK:Stop LR:Bnd K3:X"
    elif is_custom:
        hint = "OK:Record   Up/Dn:Freq +/-100kHz   L/R:Band   K1:Views" if wide else "OK:Rec UD:Frq LR:Bnd"
    else:
        hint = "OK:Record   Up/Dn:Gain   L/R:Band   K1:Library   K3:Exit" if wide else "OK:Rec UD:Gn LR:Bnd"
    ftr_h = rj_theme.draw_app_footer(draw, w, h, hint, fonts, right_text=f"{SAMPLE_RATE / 1e6:.2f} MS/s" if large else "")

    pad = 8 if large else (4 if wide else 2)
    y = hdr_h + pad

    db = sdr.get_signal_db() if sdr.is_running else -100.0
    above_thresh = db > SIGNAL_THRESHOLD_DB

    # Telemetry control strip
    strip_h = 48 if large else (34 if wide else 28)
    rj_theme.draw_card(draw, (pad, y, w - pad, y + strip_h), fill=_T["bg_surface"], outline=_T["danger"] if recording else _T["border_subtle"], radius=5 if wide else 3, accent_left=_T["danger"] if recording else (_T["ok"] if above_thresh else _T["accent"]), accent_width=3 if wide else 2)
    tx = pad + (10 if wide else 4)
    if wide:
        draw.text((tx, y + (6 if large else 3)), f"{freq_mhz:.3f} MHz", font=fonts["title"] if large else fonts["body_bold"], fill=_T["accent_bright"])
        st_lbl = "RECORDING" if recording else ("SIGNAL DETECTED" if above_thresh else "LISTENING")
        st_fg = _T["danger"] if recording else (_T["ok"] if above_thresh else _T["text_muted"])
        st_bg = _T["danger_bg"] if recording else (_T["ok_bg"] if above_thresh else _T["bg_elevated"])
        sw = rj_theme.measure_text(fonts["badge"], st_lbl, draw)[2] + 12
        rj_theme.draw_pill(draw, w - pad - sw - 8, y + (5 if large else 2), st_lbl, fonts["badge"], bg=st_bg, fg=st_fg, outline=None, pad_x=6, pad_y=2)
        bar_y = y + (28 if large else 19)
        bar_w = max(80, w - pad * 2 - (92 if large else 74))
        _draw_signal_bar(draw, db, tx, bar_y, bar_w, 12 if large else 9, fnt=fonts["sub"])
    else:
        draw.text((tx, y + 2), f"{freq_mhz:.2f}M  G:{gain_val}", font=fonts["sub"], fill=_T["accent_bright"])
        _draw_signal_bar(draw, db, tx, y + 14, max(48, w - pad * 2 - 48), 8, fnt=fonts["sub"])

    # Waterfall + live FFT overlay
    wf_y = y + strip_h + (6 if wide else 3)
    wf_bot = h - ftr_h - (4 if wide else 2)
    wf_h = max(20, wf_bot - wf_y)
    wf_x = pad if wide else 0
    wf_w = (w - pad * 2) if wide else w

    if sdr.is_running:
        iq = sdr.get_iq_block(256)
        fft_db = compute_fft(iq, 256)
        wf_buf.push(fft_db)
        wf_buf.render(img, wf_x, wf_y, wf_w, wf_h)
        resampled = np.interp(
            np.linspace(0, len(fft_db) - 1, wf_w),
            np.arange(len(fft_db)),
            fft_db,
        )
        points = []
        for i in range(wf_w):
            norm = max(0.0, min(1.0, (resampled[i] - DB_MIN) / (DB_MAX - DB_MIN)))
            py = int(wf_y + wf_h - norm * wf_h)
            points.append((wf_x + i, py))
        if len(points) > 1:
            draw.line(points, fill=_T["accent_bright"], width=2 if large else 1)
    else:
        wf_buf.render(img, wf_x, wf_y, wf_w, wf_h)

    if wide:
        draw.rectangle((wf_x, wf_y, wf_x + wf_w - 1, wf_y + wf_h - 1), outline=_T["border_subtle"])

    lcd.LCD_ShowImage(img, 0, 0)
    return db


# ---------------------------------------------------------------------------
# Drawing: LIBRARY view
# ---------------------------------------------------------------------------
def _draw_library(lcd, captures, scroll, selected):
    w, h, fonts = _sdr_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)

    hdr_h = rj_theme.draw_app_header(d, w, "SDR LIBRARY", fonts, badge=f"{len(captures)} FILES", right_text="IQ Captures" if wide else "")
    ftr_h = rj_theme.draw_app_footer(d, w, h, "OK:Load Replay   K2:Delete   K1:Views   K3:Exit" if wide else "OK:Sel K2:Del K1:Vw", fonts)

    pad = 8 if large else (4 if wide else 2)
    y0, y1 = hdr_h + pad, h - ftr_h - pad

    if not captures:
        rj_theme.draw_status_card(d, w, h, "No IQ Captures Yet", sub="Record RF signals in CAPTURE view with OK", kind="info", fonts=fonts)
        lcd.LCD_ShowImage(img, 0, 0)
        return

    row_h = 44 if large else (32 if wide else 22)
    vis = max(1, (y1 - y0) // row_h)
    sel = max(0, min(selected, len(captures) - 1))
    start = max(0, min(sel - vis // 2, max(0, len(captures) - vis)))
    y = y0

    for i in range(start, min(len(captures), start + vis)):
        cap = captures[i]
        meta = cap["meta"]
        is_sel = i == sel
        rj_theme.draw_card(
            d, (pad, y, w - pad - 5, y + row_h - 3),
            fill=_T["select_bg"] if is_sel else _T["bg_surface"],
            outline=_T["border_accent"] if is_sel else _T["border_subtle"],
            radius=5 if wide else 2, accent_left=_T["accent"] if is_sel else None, accent_width=3 if wide else 2,
        )
        freq_str = f"{meta.get('freq_mhz', 0):.3f} MHz" if wide else f"{meta.get('freq_mhz', 0):.1f}M"
        dur_str = _format_duration(meta.get("duration_s", 0))
        size_str = _format_size(cap["path"])
        ts = meta.get("timestamp", "")
        if ts:
            try:
                ts_short = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M:%S" if large else "%m/%d %H:%M")
            except (ValueError, TypeError):
                ts_short = ts[:16]
        else:
            ts_short = os.path.basename(cap["path"])

        tx = pad + (10 if wide else 4)
        if wide:
            d.text((tx, y + (5 if large else 2)), freq_str, font=fonts["body_bold"], fill=_T["accent_bright"] if is_sel else _T["text_bright"])
            meta_r = f"{dur_str}  |  {size_str}"
            mw_r = rj_theme.measure_text(fonts["badge"], meta_r, d)[2] + 10
            rj_theme.draw_pill(d, w - pad - mw_r - 10, y + (4 if large else 2), meta_r, fonts["badge"], bg=_T["ok_bg"] if is_sel else _T["bg_elevated"], fg=_T["ok"] if is_sel else _T["text_muted"], outline=None, pad_x=5, pad_y=1)
            sub_s = f"{ts_short}   {os.path.basename(cap['path'])}" if large else ts_short
            d.text((tx, y + (23 if large else 16)), rj_theme.truncate_text(d, sub_s, w - pad * 2 - 20, fonts["sub"]), font=fonts["sub"], fill=_T["text_muted"])
        else:
            d.text((tx, y + 1), f"{freq_str} {dur_str} {size_str}", font=fonts["sub"], fill=_T["accent_bright"] if is_sel else _T["text_primary"])
            d.text((tx, y + 10), rj_theme.truncate_text(d, ts_short, w - pad * 2 - 12, fonts["sub"]), font=fonts["sub"], fill=_T["text_dim"])
        y += row_h

    if len(captures) > vis:
        rj_theme.draw_scrollbar(d, w - 4, y0, y1, start, vis, len(captures), width=3)

    lcd.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# Drawing: REPLAY view
# ---------------------------------------------------------------------------
def _draw_replay(lcd, selected_cap, transmitting, tx_progress):
    w, h, fonts = _sdr_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)

    hdr_h = rj_theme.draw_app_header(d, w, "SDR REPLAY", fonts, badge="TX LIVE" if transmitting else "READY")
    ftr_h = rj_theme.draw_app_footer(
        d, w, h,
        ("OK:Stop TX   K3:Exit" if transmitting else "OK:Transmit via rpitx   K1:Views   K3:Exit") if wide else ("OK:Stop K3:Exit" if transmitting else "OK:TX K1:Vw K3:X"),
        fonts,
    )

    if selected_cap is None:
        rj_theme.draw_status_card(d, w, h, "No Signal Selected", sub="Choose an IQ recording in LIBRARY view", kind="info", fonts=fonts)
        lcd.LCD_ShowImage(img, 0, 0)
        return

    meta = selected_cap["meta"]
    freq_mhz = meta.get("freq_mhz", 0)
    duration = meta.get("duration_s", 0)
    sample_rate = meta.get("sample_rate", SAMPLE_RATE)

    pad = 10 if large else (4 if wide else 2)
    y = hdr_h + pad
    y_bot = h - ftr_h - pad

    # Signal info card
    info_h = 68 if large else (44 if wide else 38)
    rj_theme.draw_card(d, (pad, y, w - pad, y + info_h), fill=_T["bg_surface"], outline=_T["danger"] if transmitting else _T["border_subtle"], radius=6 if wide else 3, accent_left=_T["danger"] if transmitting else _T["accent"], accent_width=4 if wide else 2)
    tx = pad + (14 if wide else 5)
    d.text((tx, y + (8 if large else 3)), f"{freq_mhz:.3f} MHz", font=fonts["title"], fill=_T["accent_bright"])
    detail_line = f"Duration: {_format_duration(duration)}   |   Rate: {sample_rate / 1e6:.2f} MS/s   |   Size: {_format_size(selected_cap['path'])}"
    if wide:
        d.text((tx, y + (36 if large else 22)), rj_theme.truncate_text(d, detail_line, w - pad * 2 - 24, fonts["body"] if large else fonts["sub"]), font=fonts["body"] if large else fonts["sub"], fill=_T["text_primary"])
    else:
        d.text((tx, y + 15), f"{_format_duration(duration)}  {sample_rate / 1e6:.1f}MS/s", font=fonts["sub"], fill=_T["text_muted"])
        d.text((tx, y + 25), f"Size: {_format_size(selected_cap['path'])}", font=fonts["sub"], fill=_T["text_dim"])

    # IQ waveform card
    tx_box_h = 54 if large else (30 if wide else 22)
    wv_y = y + info_h + (8 if large else 4)
    wv_h = max(24, y_bot - tx_box_h - wv_y - (8 if large else 4))
    rj_theme.draw_card(d, (pad, wv_y, w - pad, wv_y + wv_h), fill=_T["bg_well"], outline=_T["border_subtle"], radius=5 if wide else 2)
    wv_x0 = pad + 4
    wv_w = max(20, w - pad * 2 - 8)
    center_y = wv_y + wv_h // 2
    d.line([(wv_x0, center_y), (wv_x0 + wv_w, center_y)], fill=_T["border_subtle"], width=1)
    try:
        with open(selected_cap["path"], "rb") as fh:
            raw = fh.read(min(4096, os.path.getsize(selected_cap["path"])))
        if raw:
            samples = np.frombuffer(raw, dtype=np.uint8)
            floats = (samples.astype(np.float32) - 127.5) / 127.5
            resampled = np.interp(np.linspace(0, len(floats) - 1, wv_w), np.arange(len(floats)), floats)
            amp = max(4, (wv_h // 2) - 4)
            points = [(wv_x0 + i, int(center_y - resampled[i] * amp)) for i in range(wv_w)]
            if len(points) > 1:
                d.line(points, fill=_T["danger"] if transmitting else _T["accent_bright"], width=2 if large else 1)
    except Exception:
        pass

    # TX status area
    ty = y_bot - tx_box_h
    if transmitting:
        blink_col = _T["danger"] if int(time.time() * 4) % 2 == 0 else _T["warn"]
        d.text((w // 2, ty + (10 if large else 4)), "TRANSMITTING RF SIGNAL...", font=fonts["title"] if large else fonts["body_bold"], fill=blink_col, anchor="mt")
        bar_y = ty + (34 if large else 18)
        rj_theme.draw_progress_bar(d, (pad + 12, bar_y, w - pad - 12, bar_y + (12 if large else 7)), min(1.0, tx_progress), fill=_T["danger"], track=_T["bg_well"], outline=_T["border_subtle"], radius=3)
    else:
        has_rpitx = _rpitx_available()
        if has_rpitx:
            d.text((w // 2, ty + tx_box_h // 2), "Press OK to transmit via rpitx", font=fonts["body_bold"] if wide else fonts["sub"], fill=_T["ok"], anchor="mm")
        else:
            d.text((w // 2, ty + tx_box_h // 2), "rpitx not installed (apt install rpitx)", font=fonts["body"] if wide else fonts["sub"], fill=_T["danger"], anchor="mm")

    lcd.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# Splash / status screens
# ---------------------------------------------------------------------------
def _draw_splash(lcd, msg, sub="", color=COL_CAPTURE):
    w, h, fonts = _sdr_dims(lcd)
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)
    rj_theme.draw_app_header(d, w, "SDR REPLAY", fonts, badge="RF")
    kind = "err" if color == COL_REPLAY else ("ok" if color == COL_CAPTURE else "info")
    rj_theme.draw_status_card(d, w, h, msg, sub=sub, kind=kind, fonts=fonts)
    lcd.LCD_ShowImage(img, 0, 0)


def _draw_confirm(lcd, msg, sub=""):
    w, h, fonts = _sdr_dims(lcd)
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)
    rj_theme.draw_app_header(d, w, "CONFIRM", fonts, badge="DELETE")
    rj_theme.draw_status_card(d, w, h, msg, sub=sub, kind="err", foot="OK: Yes, Delete   KEY2 / K3: Cancel", fonts=fonts)
    lcd.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# TX via rpitx
# ---------------------------------------------------------------------------
_tx_proc = None
_tx_lock = threading.Lock()


def _start_tx(iq_path, freq_hz, sample_rate):
    global _tx_proc
    _stop_tx()
    freq_khz = freq_hz / 1000.0
    cmd = [
        "rpitx", "-m", "IQ",
        "-i", iq_path,
        "-f", str(freq_khz),
        "-s", str(sample_rate),
    ]
    with _tx_lock:
        try:
            _tx_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )
        except FileNotFoundError:
            _tx_proc = None
            return False
    return True


def _stop_tx():
    global _tx_proc
    with _tx_lock:
        if _tx_proc is not None:
            try:
                os.killpg(os.getpgid(_tx_proc.pid), signal.SIGKILL)
            except Exception:
                pass
            _tx_proc = None


def _is_tx_running():
    with _tx_lock:
        if _tx_proc is None:
            return False
        poll = _tx_proc.poll()
        if poll is not None:
            return False
        return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    GPIO.setmode(GPIO.BCM)
    for p in PINS.values():
        GPIO.setup(p, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)

    # Detect SDR hardware
    _draw_splash(lcd, "Detecting SDR...", "Please wait", COL_SIGNAL)
    found, label, backend = detect_sdr()

    if not found:
        _draw_splash(lcd, "No SDR found!", label, COL_REPLAY)
        time.sleep(3)
        GPIO.cleanup()
        return 0

    _draw_splash(lcd, f"Found: {label}", "Initializing...", COL_CAPTURE)
    time.sleep(0.8)

    sdr = SDRDevice()
    wf_buf = _WaterfallBuf(WIDTH, SY(60))

    # State
    view_idx = 0
    band_idx = 0
    gain_idx = 3  # default gain=30
    recording = False
    rec_start = 0.0
    rec_path = ""
    lib_scroll = 0
    lib_selected = 0
    captures = []
    selected_cap = None
    transmitting = False
    tx_start = 0.0

    # Start SDR listening on default band
    sdr.start(BANDS[band_idx]["freq"], SAMPLE_RATE, GAIN_STEPS[gain_idx], backend)

    try:
        while _running:
            view = VIEWS[view_idx]

            # -- Draw current view --
            if view == "capture":
                _draw_capture(
                    lcd, sdr, band_idx, gain_idx,
                    recording, rec_start, wf_buf,
                )
            elif view == "library":
                _draw_library(lcd, captures, lib_scroll, lib_selected)
            elif view == "replay":
                tx_progress = 0.0
                if transmitting and selected_cap:
                    elapsed = time.time() - tx_start
                    dur = selected_cap["meta"].get("duration_s", 1)
                    tx_progress = min(1.0, elapsed / max(0.01, dur))
                    if not _is_tx_running():
                        transmitting = False
                _draw_replay(lcd, selected_cap, transmitting, tx_progress)

            # -- Handle input --
            btn = _btn()

            if btn == "KEY3":
                # Exit
                break

            elif btn == "KEY1":
                # Cycle views
                old_view = view
                view_idx = (view_idx + 1) % len(VIEWS)
                new_view = VIEWS[view_idx]

                # Refresh library when entering library view
                if new_view == "library":
                    captures = _list_captures()
                    lib_scroll = 0
                    lib_selected = min(lib_selected, max(0, len(captures) - 1))

                # Ensure SDR runs in capture mode
                if new_view == "capture" and not sdr.is_running:
                    sdr.start(BANDS[band_idx]["freq"], SAMPLE_RATE, GAIN_STEPS[gain_idx], backend)

            elif btn == "OK":
                if view == "capture":
                    if recording:
                        # Stop recording
                        sdr.stop_recording()
                        duration = time.time() - rec_start
                        _write_metadata(
                            rec_path,
                            BANDS[band_idx]["freq"],
                            SAMPLE_RATE,
                            GAIN_STEPS[gain_idx],
                            duration,
                        )
                        recording = False
                        _draw_splash(
                            lcd,
                            f"Saved {_format_duration(duration)}",
                            os.path.basename(rec_path),
                            COL_CAPTURE,
                        )
                        time.sleep(1.2)
                    else:
                        # Start recording
                        os.makedirs(LOOT_DIR, exist_ok=True)
                        rec_path = _make_capture_path(BANDS[band_idx]["freq"])
                        sdr.start_recording(rec_path)
                        rec_start = time.time()
                        recording = True

                elif view == "library":
                    if captures and 0 <= lib_selected < len(captures):
                        selected_cap = captures[lib_selected]
                        view_idx = VIEWS.index("replay")

                elif view == "replay":
                    if transmitting:
                        _stop_tx()
                        transmitting = False
                        _draw_splash(lcd, "TX Stopped", "", COL_REPLAY)
                        time.sleep(0.8)
                    elif selected_cap is not None:
                        if not _rpitx_available():
                            _draw_splash(
                                lcd,
                                "rpitx not found!",
                                "apt install rpitx",
                                COL_REPLAY,
                            )
                            time.sleep(2)
                        else:
                            # Stop SDR RX before TX
                            sdr.stop()
                            meta = selected_cap["meta"]
                            ok = _start_tx(
                                selected_cap["path"],
                                meta.get("freq_hz", BANDS[band_idx]["freq"]),
                                meta.get("sample_rate", SAMPLE_RATE),
                            )
                            if ok:
                                transmitting = True
                                tx_start = time.time()
                            else:
                                _draw_splash(lcd, "TX Failed!", "", COL_REPLAY)
                                time.sleep(1.5)
                                sdr.start(
                                    BANDS[band_idx]["freq"],
                                    SAMPLE_RATE,
                                    GAIN_STEPS[gain_idx],
                                    backend,
                                )

            elif btn == "UP":
                if view == "capture":
                    if BANDS[band_idx]["name"] == "Custom" and not recording:
                        BANDS[band_idx]["freq"] += FREQ_STEP
                        BANDS[band_idx]["desc"] = f"{BANDS[band_idx]['freq']/1e6:.1f}M"
                        sdr.stop()
                        sdr.start(BANDS[band_idx]["freq"], SAMPLE_RATE, GAIN_STEPS[gain_idx], backend)
                        wf_buf = _WaterfallBuf(WIDTH, SY(60))
                    else:
                        gain_idx = min(len(GAIN_STEPS) - 1, gain_idx + 1)
                        sdr.set_gain(GAIN_STEPS[gain_idx])
                elif view == "library":
                    lib_selected = max(0, lib_selected - 1)
                    visible = max(1, (HEIGHT - SY(30)) // SY(20))
                    if lib_selected < lib_scroll:
                        lib_scroll = lib_selected

            elif btn == "DOWN":
                if view == "capture":
                    if BANDS[band_idx]["name"] == "Custom" and not recording:
                        BANDS[band_idx]["freq"] = max(24_000_000, BANDS[band_idx]["freq"] - FREQ_STEP)
                        BANDS[band_idx]["desc"] = f"{BANDS[band_idx]['freq']/1e6:.1f}M"
                        sdr.stop()
                        sdr.start(BANDS[band_idx]["freq"], SAMPLE_RATE, GAIN_STEPS[gain_idx], backend)
                        wf_buf = _WaterfallBuf(WIDTH, SY(60))
                    else:
                        gain_idx = max(0, gain_idx - 1)
                        sdr.set_gain(GAIN_STEPS[gain_idx])
                elif view == "library":
                    if captures:
                        lib_selected = min(len(captures) - 1, lib_selected + 1)
                        visible = max(1, (HEIGHT - SY(30)) // SY(20))
                        if lib_selected >= lib_scroll + visible:
                            lib_scroll = lib_selected - visible + 1

            elif btn == "LEFT":
                if view == "capture" and not recording:
                    band_idx = (band_idx - 1) % len(BANDS)
                    sdr.stop()
                    sdr.start(
                        BANDS[band_idx]["freq"],
                        SAMPLE_RATE,
                        GAIN_STEPS[gain_idx],
                        backend,
                    )
                    wf_buf = _WaterfallBuf(WIDTH, SY(60))

            elif btn == "RIGHT":
                if view == "capture" and not recording:
                    band_idx = (band_idx + 1) % len(BANDS)
                    sdr.stop()
                    sdr.start(
                        BANDS[band_idx]["freq"],
                        SAMPLE_RATE,
                        GAIN_STEPS[gain_idx],
                        backend,
                    )
                    wf_buf = _WaterfallBuf(WIDTH, SY(60))

            elif btn == "KEY2":
                if view == "library" and captures and 0 <= lib_selected < len(captures):
                    cap = captures[lib_selected]
                    fname = os.path.basename(cap["path"])[:20]
                    _draw_confirm(lcd, "Delete capture?", fname)
                    # Wait for confirmation
                    confirmed = None
                    t0 = time.time()
                    while _running and confirmed is None and (time.time() - t0) < 10:
                        cb = _btn()
                        if cb == "OK":
                            confirmed = True
                        elif cb in ("KEY2", "KEY3"):
                            confirmed = False
                        time.sleep(0.05)
                    if confirmed:
                        _delete_capture(cap["path"])
                        captures = _list_captures()
                        lib_selected = min(lib_selected, max(0, len(captures) - 1))
                        _draw_splash(lcd, "Deleted", "", COL_REPLAY)
                        time.sleep(0.8)

            time.sleep(0.05)

    finally:
        # Cleanup
        if recording:
            sdr.stop_recording()
            duration = time.time() - rec_start
            if rec_path:
                _write_metadata(
                    rec_path,
                    BANDS[band_idx]["freq"],
                    SAMPLE_RATE,
                    GAIN_STEPS[gain_idx],
                    duration,
                )
        _stop_tx()
        sdr.stop()
        lcd.LCD_Clear()
        GPIO.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
