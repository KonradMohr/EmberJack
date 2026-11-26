#!/usr/bin/env python3
"""
EmberJack Payload -- Video Player
===================================
Author: 7h30th3r0n3

Single-process offline video player: ffmpeg handles audio+video decode & sync.
Video frames -> pipe -> open_framebuffer (HDMI proxy / Cardputer fb) or SPI LCD.
Audio -> PipeWire/Pulse/ALSA directly from ffmpeg.

Controls (Browser):
  UP / DOWN         Select folder or video
  OK / Enter        Open folder / Play video
  LEFT / Backspace  Parent directory
  KEY1 / I          Video file info
  KEY2 / L          Toggle Loop
  +/-               Volume +/- 10%
  KEY3 / Esc        Back / Exit

Controls (Playback):
  OK / Space        Pause / Resume
  LEFT / RIGHT      Seek -10s / +10s
  UP / DOWN (+/-)   Volume +/- 10%
  KEY1 / L          Toggle Loop
  KEY2 / I          Show / Hide OSD
  KEY3 / Esc        Stop playback
"""

import os
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
from payloads._audio_helper import (
    get_audio_popen_kwargs,
    get_ffmpeg_audio_args,
    set_playback_volume_pct,
)
from payloads._display_helper import open_framebuffer
from payloads._input_helper import (
    flush_input,
    format_footer_hint,
    is_action,
    is_back,
    is_confirm,
    is_dismiss,
    is_step_down,
    is_step_up,
    poll_ui_input,
    text_input_mode,
)

try:
    import cv2
    CV2_OK = True
except ImportError:
    CV2_OK = False

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
WIDTH, HEIGHT = LCD.width, LCD.height
FONTS = rj_theme.load_app_fonts(WIDTH, HEIGHT)
IS_WIDE = bool(FONTS["wide"])
IS_LARGE = bool(FONTS["large"])
IS_MID = (WIDTH >= 200 and HEIGHT >= 200 and WIDTH < 300)
IS_SHORT = (HEIGHT <= 200)
ET = rj_theme.EMBER_THEME

VIDEO_EXTENSIONS = {".mp4", ".avi", ".mkv", ".mov", ".webm", ".flv", ".m4v"}
START_DIR = os.path.join(rj_env.get_base_dir(), "loot")
DEBOUNCE = 0.16
FB_DEVICE = "/dev/fb1" if os.path.exists("/dev/fb1") else "/dev/fb0"
FB_SIZE = WIDTH * HEIGHT * 2

_running = True
_volume = 40
_loop = False


def _get_card():
    try:
        r = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=3)
        for line in r.stdout.split("\n"):
            if "ES8388" in line or "ES8389" in line:
                return line.split(":")[0].replace("card", "").strip()
    except Exception:
        pass
    return "0"


def _sig(s, f):
    global _running
    _running = False


signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)


def _tpa_enable():
    try:
        import smbus2
        bus = smbus2.SMBus(1)
        bus.write_byte_data(0x60, 0x01, 0xC0, force=True)
        bus.close()
    except Exception:
        pass


def _set_volume(vol):
    global _volume
    _volume = max(0, min(100, int(vol)))
    try:
        set_playback_volume_pct(_volume)
    except Exception:
        dac_val = int(75 + (_volume * 180 / 100))
        hp_val = int(19 + (_volume * 44 / 100))
        card = _get_card()
        subprocess.Popen(["amixer", "-c", card, "sset", "Headphone", str(hp_val)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen(["amixer", "-c", card, "sset", "DACL", str(dac_val)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen(["amixer", "-c", card, "sset", "DACR", str(dac_val)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _tpa_enable()


def _format_time(seconds):
    if not seconds or seconds < 0:
        seconds = 0
    h, m, s = int(seconds) // 3600, (int(seconds) % 3600) // 60, int(seconds) % 60
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def _human_size(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}"
        size /= 1024
    return f"{size:.0f}TB"


def _list_dir(path):
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
        elif os.path.splitext(e)[1].lower() in VIDEO_EXTENSIONS:
            try:
                sz = os.path.getsize(full)
            except OSError:
                sz = 0
            files.append({"name": e, "path": full, "is_dir": False, "size": sz})
    return dirs + files


# ---------------------------------------------------------------------------
# Native Obsidian Ember Browser & Info Screens
# ---------------------------------------------------------------------------
def _chrome_heights():
    if IS_MID:
        return 22, 16
    if IS_WIDE and IS_SHORT:
        return 20, 15
    return None, None


def _draw_browser(items, cursor, scroll, current_dir):
    img = Image.new("RGB", (WIDTH, HEIGHT), ET["bg_base"])
    d = ImageDraw.Draw(img)
    hh, fh = _chrome_heights()

    dirname = os.path.basename(current_dir) or current_dir
    hdr_h = rj_theme.draw_app_header(
        d, WIDTH, "EMBERJACK VIDEO" if IS_WIDE else "VIDEO", FONTS,
        badge=dirname[:12],
        right_text="LOOP ON" if (_loop and IS_LARGE) else "",
        right_pill=f"VOL {_volume}%",
        hdr_h=hh,
    )
    ftr_h = rj_theme.draw_app_footer(
        d, WIDTH, HEIGHT,
        format_footer_hint(
            WIDTH, HEIGHT,
            hat_hint="OK:Play K1:Inf K3:X",
            hat_240_hint="OK:Play L:Up K1:Inf K2:Lp K3:X",
            cardputer_hint="Ent:Play Left:Up I:Inf L:Lp +/-:V Esc:X",
            hdmi_hint="Arrows:Nav  Enter:Play  Left:Up  I:Info  L:Loop  +/-:Vol  Esc:Exit",
        ),
        FONTS,
        right_text=f"{cursor + 1}/{len(items)}" if (IS_WIDE and items) else "",
        ftr_h=fh,
    )

    pad = 8 if IS_LARGE else (6 if IS_MID else (4 if IS_WIDE else 3))
    y0 = hdr_h + pad
    y1 = HEIGHT - ftr_h - pad
    split = WIDTH >= 480

    list_x0 = pad
    list_x1 = int(WIDTH * 0.60) - 4 if split else (WIDTH - pad - (6 if (IS_WIDE or IS_MID) else 4))

    if not items:
        rj_theme.draw_status_card(
            d, WIDTH, HEIGHT, "No Videos Found",
            sub=f"Folder: {current_dir}", kind="info",
            foot=format_footer_hint(
                WIDTH, HEIGHT,
                hat_hint="L:Parent K3:Exit",
                hat_240_hint="Left:Parent  K3:Exit",
                cardputer_hint="Left:Parent  Esc:Exit",
                hdmi_hint="Left/Bksp: Parent folder  •  Copy .mp4/.mkv/.webm here",
            ),
            fonts=FONTS,
        )
    else:
        row_h = 32 if IS_LARGE else (26 if IS_MID else (22 if IS_WIDE else 16))
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
            ry1 = min(y1, ry + row_h - (4 if (IS_WIDE or IS_MID) else 2))
            is_sel = idx == cursor

            rj_theme.draw_card(
                d, (list_x0, ry, list_x1, ry1),
                fill=ET["select_bg"] if is_sel else ET["bg_surface"],
                outline=ET["border_accent"] if is_sel else ET["border_subtle"],
                radius=6 if (IS_WIDE or IS_MID) else 3,
                accent_left=ET["accent"] if is_sel else None,
                accent_width=3 if (IS_WIDE or IS_MID) else 2,
            )

            lx = list_x0 + (10 if (IS_WIDE or IS_MID) else 4)
            rx = list_x1 - (8 if (IS_WIDE or IS_MID) else 3)
            tag = "DIR" if item["is_dir"] else _human_size(item.get("size", 0))
            tag_fg = ET["warn"] if item["is_dir"] else ET["text_muted"]

            if IS_WIDE or IS_MID:
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

        rj_theme.draw_scrollbar(d, list_x1 + 3, y0, y1, st, vis, len(items), width=3 if (IS_WIDE or IS_MID) else 2)

        # Right Details Pane on Widescreen (>= 480px)
        if split:
            px0 = int(WIDTH * 0.60) + 6
            px1 = WIDTH - pad
            rj_theme.draw_card(
                d, (px0, y0, px1, y1),
                fill=ET["bg_surface"], outline=ET["border_subtle"],
                radius=8, accent_left=ET["accent_dim"], accent_width=3,
            )
            ix = px0 + 14
            iw = (px1 - px0) - 24
            iy = y0 + 14
            cur = items[min(cursor, len(items) - 1)]
            ext = "FOLDER" if cur["is_dir"] else (os.path.splitext(cur["name"])[1].lstrip(".").upper() or "VIDEO")
            rj_theme.draw_pill(
                d, ix, iy, ext, FONTS["badge"],
                bg=ET["select_bg"], fg=ET["accent_bright"], outline=ET["border_accent"],
                pad_x=6, pad_y=2, radius=3,
            )
            iy += 28
            for ln in rj_theme.wrap_text(d, cur["name"], iw, FONTS["body_bold"])[:3]:
                d.text((ix, iy), ln, font=FONTS["body_bold"], fill=ET["text_bright"])
                iy += 17
            iy += 10
            n_dirs = sum(1 for x in items if x["is_dir"])
            n_vids = len(items) - n_dirs
            rows = [
                ("Folder", dirname),
                ("Videos", str(n_vids)),
                ("Subdirs", str(n_dirs)),
                ("Loop Mode", "ON" if _loop else "OFF"),
            ]
            if not cur["is_dir"]:
                rows.insert(0, ("File Size", _human_size(cur.get("size", 0))))
            for mk, mv in rows:
                if iy + 18 > y1 - 48:
                    break
                d.text((ix, iy), mk, font=FONTS["sub"], fill=ET["text_muted"])
                vb = rj_theme.measure_text(FONTS["body"], mv, d)
                d.text((px1 - 12 - (vb[2] - vb[0]), iy - 1), mv, font=FONTS["body"], fill=ET["text_primary"])
                iy += 19

            vy = y1 - 36
            d.line([(ix, vy - 8), (px1 - 12, vy - 8)], fill=ET["border_subtle"], width=1)
            d.text((ix, vy), f"PLAYBACK VOLUME  {_volume}%", font=FONTS["badge"], fill=ET["text_muted"])
            rj_theme.draw_progress_bar(
                d, (ix, vy + 15, px1 - 12, vy + 23), _volume / 100.0,
                fill=ET["accent"], track=ET["bg_well"], outline=ET["border_subtle"], radius=3,
            )

    LCD.LCD_ShowImage(img, 0, 0)


def _probe_video_info(filepath):
    w, h, fps, dur = 0, 0, 0.0, 0.0
    if CV2_OK:
        try:
            cap = cv2.VideoCapture(filepath)
            w, h = int(cap.get(3)), int(cap.get(4))
            fps = float(cap.get(5) or 0.0)
            dur = int(cap.get(7)) / fps if fps > 0 else 0.0
            cap.release()
        except Exception:
            pass
    if dur <= 0:
        dur = _get_duration(filepath)
    if h <= 0:
        h = _get_resolution(filepath)
    return w, h, fps, dur


def _show_info_screen(filepath):
    w, h, fps, dur = _probe_video_info(filepath)
    has_aud = _has_audio(filepath)
    try:
        sz_str = _human_size(os.path.getsize(filepath))
    except OSError:
        sz_str = "?"

    img = Image.new("RGB", (WIDTH, HEIGHT), ET["bg_base"])
    d = ImageDraw.Draw(img)
    hh, fh = _chrome_heights()
    hdr_h = rj_theme.draw_app_header(d, WIDTH, "VIDEO INFO", FONTS, badge="META", hdr_h=hh)
    ftr_h = rj_theme.draw_app_footer(
        d, WIDTH, HEIGHT,
        format_footer_hint(
            WIDTH, HEIGHT,
            hat_hint="OK/K3:Back",
            hat_240_hint="OK / K3: Back",
            cardputer_hint="Enter / Esc: Back",
            hdmi_hint="Enter / Esc: Return",
        ),
        FONTS,
        ftr_h=fh,
    )

    pad = 12 if IS_LARGE else (7 if IS_MID else (4 if IS_WIDE else 4))
    avail_h = max(60, HEIGHT - hdr_h - ftr_h - pad * 2)
    cw = min(WIDTH - pad * 2, 480 if IS_LARGE else WIDTH - pad * 2)
    ch = min(avail_h, 220 if IS_LARGE else (175 if IS_MID else avail_h))
    cx0 = (WIDTH - cw) // 2
    cy0 = hdr_h + pad + max(0, (avail_h - ch) // 2)
    rj_theme.draw_card(
        d, (cx0, cy0, cx0 + cw, cy0 + ch),
        fill=ET["bg_surface"], outline=ET["border_accent"],
        radius=8 if (IS_WIDE or IS_MID) else 4, accent_left=ET["accent"], accent_width=3 if (IS_WIDE or IS_MID) else 2,
    )

    ix = cx0 + (16 if IS_LARGE else (10 if IS_MID else 8))
    iw = cw - (28 if IS_LARGE else (18 if IS_MID else 14))
    iy = cy0 + (14 if IS_LARGE else (10 if IS_MID else 6))
    d.text((ix, iy), rj_theme.truncate_text(d, os.path.basename(filepath), iw, FONTS["title"]), font=FONTS["title"], fill=ET["accent_bright"])
    iy += 28 if IS_LARGE else (20 if IS_MID else (16 if IS_WIDE else 14))

    res_str = f"{w}x{h}" if (w and h) else (f"{h}p" if h else "Unknown")
    fps_str = f"{fps:.1f} fps" if fps > 0 else "N/A"
    rows = [
        ("Resolution", res_str),
        ("Frame Rate", fps_str),
        ("Duration", _format_time(dur) if dur > 0 else "Unknown"),
        ("File Size", sz_str),
        ("Audio Track", "Yes" if has_aud else "Silent"),
    ]
    step = 24 if IS_LARGE else (20 if IS_MID else (16 if IS_WIDE else 13))
    row_f = FONTS["body"] if (IS_LARGE or IS_MID) else FONTS["sub"]
    val_f = FONTS["body_bold"] if (IS_LARGE or IS_MID) else FONTS["sub"]
    for k_lbl, v_lbl in rows:
        if iy + step > cy0 + ch - 4:
            break
        d.text((ix, iy), k_lbl, font=row_f, fill=ET["text_muted"])
        vb = rj_theme.measure_text(val_f, v_lbl, d)
        d.text((cx0 + cw - (16 if IS_LARGE else (10 if IS_MID else 8)) - (vb[2] - vb[0]), iy), v_lbl, font=val_f, fill=ET["text_primary"])
        iy += step

    LCD.LCD_ShowImage(img, 0, 0)
    flush_input()
    while _running:
        act, _ch = poll_ui_input(PINS, GPIO)
        if is_dismiss(act):
            break
        time.sleep(0.03)


# ---------------------------------------------------------------------------
# Media Probing & Playback Engine
# ---------------------------------------------------------------------------
def _get_duration(filepath):
    """Get video duration in seconds via ffprobe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=5)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _read_frame(proc):
    """Read exactly one RGB565 frame from pipe. Returns bytes or None."""
    raw = b""
    while len(raw) < FB_SIZE:
        chunk = proc.stdout.read(FB_SIZE - len(raw))
        if not chunk:
            return None
        raw += chunk
    return raw


_osd_img = None


def _build_osd(fname, paused, elapsed, duration):
    """Build sleek translucent Obsidian Ember playback OSD overlay."""
    global _osd_img
    oh = 38 if IS_LARGE else (28 if IS_MID else (24 if IS_WIDE else 18))
    img = Image.new("RGBA", (WIDTH, oh), (14, 14, 16, 212))
    d = ImageDraw.Draw(img)

    # Top hairline amber accent
    d.line([(0, 0), (WIDTH - 1, 0)], fill=(184, 94, 36, 235), width=1)
    d.rectangle((0, 0, 3 if (IS_WIDE or IS_MID) else 2, oh - 4), fill=(224, 122, 47, 255))

    pad = 10 if IS_LARGE else (6 if (IS_WIDE or IS_MID) else 3)
    bar_h = 4 if (IS_LARGE or IS_MID) else 3
    mid_y = (oh - bar_h) // 2

    # Play/Pause pill
    st_lbl = "PAUSED" if paused else "PLAY"
    st_bg = (43, 33, 18, 240) if paused else (45, 29, 18, 240)
    st_fg = (230, 168, 69, 255) if paused else (242, 153, 74, 255)
    sb = rj_theme.measure_text(FONTS["badge"], st_lbl, d)
    sw = (sb[2] - sb[0]) + (10 if (IS_WIDE or IS_MID) else 6)
    sh = (sb[3] - sb[1]) + 4
    sy = max(2, mid_y - sh // 2)
    d.rounded_rectangle((pad, sy, pad + sw, sy + sh), radius=3, fill=st_bg, outline=st_fg)
    d.text((pad + sw // 2, sy + sh // 2), st_lbl, font=FONTS["badge"], fill=st_fg, anchor="mm")

    # Right side: time + vol + loop
    rx = WIDTH - pad
    time_txt = f"{_format_time(elapsed)}/{_format_time(duration)}" if duration > 0 else _format_time(elapsed)
    tb = rj_theme.measure_text(FONTS["badge"], time_txt, d)
    tw = tb[2] - tb[0]
    d.text((rx - tw, mid_y), time_txt, font=FONTS["badge"], fill=(230, 223, 215, 255), anchor="lm")
    rx -= tw + (8 if (IS_WIDE or IS_MID) else 4)

    vol_txt = f"V{_volume}%"
    vb = rj_theme.measure_text(FONTS["badge"], vol_txt, d)
    vw = (vb[2] - vb[0]) + (8 if (IS_WIDE or IS_MID) else 4)
    d.rounded_rectangle((rx - vw, sy, rx, sy + sh), radius=3, fill=(31, 30, 34, 235), outline=(43, 40, 38, 235))
    d.text((rx - vw // 2, sy + sh // 2), vol_txt, font=FONTS["badge"], fill=(242, 153, 74, 255), anchor="mm")
    rx -= vw + (6 if (IS_WIDE or IS_MID) else 3)

    if _loop and (IS_WIDE or IS_MID):
        lb = rj_theme.measure_text(FONTS["badge"], "LOOP", d)
        lw = (lb[2] - lb[0]) + 8
        d.rounded_rectangle((rx - lw, sy, rx, sy + sh), radius=3, fill=(21, 38, 28, 235), outline=(94, 184, 122, 235))
        d.text((rx - lw // 2, sy + sh // 2), "LOOP", font=FONTS["badge"], fill=(94, 184, 122, 255), anchor="mm")
        rx -= lw + 6

    # Filename
    tx = pad + sw + (8 if IS_WIDE else 4)
    avail = max(16, rx - tx - 4)
    title_f = FONTS["body_bold"] if IS_LARGE else FONTS["sub"]
    d.text((tx, mid_y), rj_theme.truncate_text(d, fname, avail, title_f), font=title_f, fill=(245, 239, 232, 255), anchor="lm")

    # Progress bar along bottom of OSD
    d.rectangle((0, oh - bar_h, WIDTH - 1, oh - 1), fill=(22, 22, 26, 220))
    if duration > 0:
        prog = max(0.0, min(1.0, elapsed / duration))
        px = int(WIDTH * prog)
        if px > 0:
            d.rectangle((0, oh - bar_h, px, oh - 1), fill=(224, 122, 47, 255))

    _osd_img = img


def _composite_frame_with_osd(frame_raw, show_osd):
    if not show_osd or _osd_img is None:
        return frame_raw

    arr = np.frombuffer(frame_raw, dtype="<u2").reshape(HEIGHT, WIDTH).copy()
    osd_h = _osd_img.height
    osd_y = HEIGHT - osd_h

    osd_rgba = np.asarray(_osd_img)
    a = osd_rgba[:, :, 3].astype(np.float32) / 255.0
    inv = 1.0 - a

    vid_r = ((arr[osd_y:, :] >> 11) & 0x1F).astype(np.float32) * 8.0
    vid_g = ((arr[osd_y:, :] >> 5) & 0x3F).astype(np.float32) * 4.0
    vid_b = (arr[osd_y:, :] & 0x1F).astype(np.float32) * 8.0

    r = (osd_rgba[:, :, 0].astype(np.float32) * a + vid_r * inv).astype(np.uint16) >> 3
    g = (osd_rgba[:, :, 1].astype(np.float32) * a + vid_g * inv).astype(np.uint16) >> 2
    b = (osd_rgba[:, :, 2].astype(np.float32) * a + vid_b * inv).astype(np.uint16) >> 3

    arr[osd_y:, :] = (r << 11) | (g << 5) | b
    return arr.tobytes()


def _present_raw(fb_map, use_fb, frame_raw, show_osd):
    out = _composite_frame_with_osd(frame_raw, show_osd)
    if use_fb and fb_map is not None:
        fb_map.seek(0)
        fb_map.write(out)
    else:
        arr = np.frombuffer(out, dtype="<u2").reshape(HEIGHT, WIDTH)
        rr = ((arr >> 11) & 0x1F) << 3
        gg = ((arr >> 5) & 0x3F) << 2
        bb = (arr & 0x1F) << 3
        rgb = np.stack([rr.astype(np.uint8), gg.astype(np.uint8), bb.astype(np.uint8)], axis=-1)
        LCD.LCD_ShowImage(Image.fromarray(rgb, "RGB"), 0, 0)


def _has_audio(filepath):
    """Check if file has an audio stream."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=5)
        return "audio" in r.stdout
    except Exception:
        return False


def _get_resolution(filepath):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=5)
        return int(r.stdout.strip())
    except Exception:
        return 0


def _start_playback(filepath, seek=0):
    """Start ffmpeg with video pipe + audio output, synced from seek position."""
    has_aud = _has_audio(filepath)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "quiet",
        "-threads", "0",
    ]
    if not has_aud:
        cmd.append("-re")
    cmd += [
        "-ss", str(seek),
        "-fflags", "+nobuffer+fastseek",
        "-analyzeduration", "0", "-probesize", "32768",
        "-i", filepath,
        "-map", "0:v:0",
        "-vf", f"fps=20,scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:flags=fast_bilinear,pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2",
        "-pix_fmt", "rgb565le",
        "-avoid_negative_ts", "make_zero",
        "-f", "rawvideo", "pipe:1",
    ]
    if has_aud:
        try:
            aud_args = get_ffmpeg_audio_args()
        except Exception:
            aud_args = ["-f", "alsa", "default"]
        cmd += ["-map", "0:a:0", "-af", "aresample=async=1:first_pts=0", "-ac", "2", "-ar", "44100", "-avoid_negative_ts", "make_zero"] + aud_args

    try:
        popen_kw = get_audio_popen_kwargs(cmd) if has_aud else {}
    except Exception:
        popen_kw = {}

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0, **popen_kw)
    if proc.stdout is not None:
        try:
            import fcntl
            for _sz in (4 * 1024 * 1024, 2 * 1024 * 1024, 1024 * 1024):
                try:
                    fcntl.fcntl(proc.stdout.fileno(), 1031, _sz)
                    break
                except OSError:
                    continue
        except Exception:
            pass
    return proc


def _kill_proc(p):
    if p:
        try:
            p.kill()
            p.wait(timeout=2)
        except Exception:
            pass


def _play_video(filepath):
    global _loop

    res = _get_resolution(filepath)
    if res > 1080:
        img = Image.new("RGB", (WIDTH, HEIGHT), ET["bg_base"])
        d = ImageDraw.Draw(img)
        hh, _ = _chrome_heights()
        rj_theme.draw_app_header(d, WIDTH, "EMBERJACK VIDEO" if IS_WIDE else "VIDEO", FONTS, badge="LIMIT", hdr_h=hh)
        rj_theme.draw_status_card(
            d, WIDTH, HEIGHT, "Resolution Too High",
            sub=f"{res}p exceeds 1080p max — re-encode to 720p",
            kind="err",
            foot=format_footer_hint(
                WIDTH, HEIGHT,
                hat_hint="OK/K3:Back",
                hat_240_hint="OK or KEY3 to go back",
                cardputer_hint="Enter or Esc to go back",
                hdmi_hint="Enter or Esc to go back",
            ),
            fonts=FONTS,
        )
        LCD.LCD_ShowImage(img, 0, 0)
        flush_input()
        while _running:
            act, _ch = poll_ui_input(PINS, GPIO)
            if is_dismiss(act):
                break
            time.sleep(0.04)
        return

    fname = os.path.splitext(os.path.basename(filepath))[0]
    duration = _get_duration(filepath)
    _set_volume(_volume)

    proc = _start_playback(filepath)

    disp_type = getattr(LCD, "display_type", "")
    use_fb = disp_type in ("HDMI", "CARDPUTER_320")
    fb_fd = fb_map = None
    if use_fb:
        fb_fd, fb_map = open_framebuffer(LCD, FB_DEVICE, WIDTH, HEIGHT)

    paused = False
    start_time = time.time()
    pause_offset = 0.0
    osd_until = time.time() + 3.0
    last_osd_rebuild = 0.0
    last_frame = None

    try:
        while _running:
            act, ch = poll_ui_input(PINS, GPIO)
            now = time.time()

            if is_back(act):
                break
            elif act == "OK" or ch == " ":
                paused = not paused
                osd_until = now + 3.0
                last_osd_rebuild = 0.0
                if paused:
                    pause_offset = now - start_time
                    _kill_proc(proc)
                    proc = None
                    if last_frame:
                        _build_osd(fname, True, pause_offset, duration)
                        _present_raw(fb_map, use_fb, last_frame, True)
                else:
                    proc = _start_playback(filepath, pause_offset)
                    start_time = now - pause_offset
                time.sleep(DEBOUNCE)
                continue
            elif act == "UP" or is_step_up(ch):
                _set_volume(_volume + 10)
                osd_until = now + 2.2
                last_osd_rebuild = 0.0
            elif act == "DOWN" or is_step_down(ch):
                _set_volume(_volume - 10)
                osd_until = now + 2.2
                last_osd_rebuild = 0.0
            elif act == "LEFT":
                elapsed = pause_offset if paused else (now - start_time)
                seek_to = max(0.0, elapsed - 10.0)
                _kill_proc(proc)
                pause_offset = seek_to
                if not paused:
                    proc = _start_playback(filepath, seek_to)
                    start_time = now - seek_to
                osd_until = now + 2.2
                last_osd_rebuild = 0.0
            elif act == "RIGHT":
                elapsed = pause_offset if paused else (now - start_time)
                seek_to = min(duration, elapsed + 10.0) if duration else elapsed + 10.0
                _kill_proc(proc)
                pause_offset = seek_to
                if not paused:
                    proc = _start_playback(filepath, seek_to)
                    start_time = now - seek_to
                osd_until = now + 2.2
                last_osd_rebuild = 0.0
            elif is_action(act, ch, "KEY1", "l"):
                _loop = not _loop
                osd_until = now + 2.2
                last_osd_rebuild = 0.0
            elif is_action(act, ch, "KEY2", "i"):
                osd_until = 0.0 if now < osd_until else (now + 3.5)
                last_osd_rebuild = 0.0

            if paused:
                time.sleep(0.04)
                continue

            raw = _read_frame(proc)
            if raw is None:
                if _loop:
                    _kill_proc(proc)
                    proc = _start_playback(filepath)
                    start_time = now
                    continue
                break

            last_frame = raw
            show_osd = now < osd_until

            if show_osd and (now - last_osd_rebuild > 0.5):
                _build_osd(fname, paused, now - start_time, duration)
                last_osd_rebuild = now
            _present_raw(fb_map, use_fb, raw, show_osd)

    finally:
        _kill_proc(proc)
        if fb_map is not None:
            try:
                fb_map.close()
            except Exception:
                pass
        if fb_fd is not None:
            try:
                os.close(fb_fd)
            except Exception:
                pass
        subprocess.run(["pkill", "-9", "ffmpeg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.2)
        LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)


def main():
    global _loop
    _set_volume(_volume)
    current_dir = START_DIR if os.path.isdir(START_DIR) else rj_env.get_base_dir()
    cursor, scroll, dir_stack = 0, 0, []
    items = _list_dir(current_dir)
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

                if is_back(act):
                    if dir_stack:
                        current_dir, cursor, scroll = dir_stack.pop()
                        items = _list_dir(current_dir)
                        dirty = True
                    else:
                        break
                elif act in ("LEFT", "BACKSPACE"):
                    if dir_stack:
                        current_dir, cursor, scroll = dir_stack.pop()
                        items = _list_dir(current_dir)
                        dirty = True
                    else:
                        parent = os.path.dirname(current_dir)
                        if parent != current_dir:
                            dir_stack.append((current_dir, cursor, scroll))
                            current_dir, cursor, scroll = parent, 0, 0
                            items = _list_dir(current_dir)
                            dirty = True
                elif act == "UP" and items:
                    cursor = (cursor - 1) % len(items)
                    if cursor < scroll:
                        scroll = cursor
                    dirty = True
                elif act == "DOWN" and items:
                    cursor = (cursor + 1) % len(items)
                    dirty = True
                elif is_step_up(ch):
                    _set_volume(_volume + 10)
                    dirty = True
                elif is_step_down(ch):
                    _set_volume(_volume - 10)
                    dirty = True
                elif is_confirm(act) and items and cursor < len(items):
                    item = items[cursor]
                    if item["is_dir"]:
                        dir_stack.append((current_dir, cursor, scroll))
                        current_dir, cursor, scroll = item["path"], 0, 0
                        items = _list_dir(current_dir)
                    else:
                        _play_video(item["path"])
                        items = _list_dir(current_dir)
                    dirty = True
                elif is_action(act, ch, "KEY1", "i") and items and cursor < len(items):
                    if not items[cursor]["is_dir"]:
                        _show_info_screen(items[cursor]["path"])
                        dirty = True
                elif is_action(act, ch, "KEY2", "l"):
                    _loop = not _loop
                    dirty = True
    finally:
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
