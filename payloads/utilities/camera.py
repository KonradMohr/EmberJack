#!/usr/bin/env python3
"""
EmberJack Payload -- Camera
==============================
Author: 7h30th3r0n3

Photo, video, and timelapse capture with IMX219 camera in native Obsidian Ember UI.

Flow: Menu -> Photo / Video / Timelapse / Gallery / Settings
  Photo:     live viewfinder with Ember HUD + OK/Enter to snap (instant)
             KEY2/H for hi-res capture (brief preview pause)
  Video:     OK/Enter to start rec (preview stops), OK/Enter to stop
  Timelapse: configurable interval capture + automatic mp4 compilation
  Gallery:   widescreen split-pane browse + thumbnail preview + view/play/delete
  Settings:  resolution selector & media counts

Controls per state:
  Menu:     UP/DN navigate, OK/Enter select, KEY3/Esc exit
  Photo:    OK/Enter snap, KEY2/H hi-res, KEY3/Esc back
  Video:    OK/Enter start/stop rec, KEY3/Esc back
  Gallery:  UP/DN navigate, OK/Enter view/play, KEY2/D delete, KEY3/Esc back
  Settings: UP/DN navigate, OK/Enter change, KEY3/Esc back
"""

from datetime import datetime
import glob
import os
import signal
import subprocess
import sys
import threading
import time

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import numpy as np
from PIL import Image, ImageDraw

import rj_env
import rj_theme
from payloads._display_helper import open_framebuffer
from payloads._input_helper import (
    flush_input,
    format_footer_hint,
    is_action,
    is_back,
    is_confirm,
    is_dismiss,
    poll_ui_input,
    text_input_mode,
)

PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
for p in PINS.values():
    GPIO.setup(p, GPIO.IN, pull_up_down=GPIO.PUD_UP)

time.sleep(0.1)
_STUCK_PINS = {name for name, pin in PINS.items() if GPIO.input(pin) == 0}

LCD = LCD_1in44.LCD()
LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
W, H = LCD.width, LCD.height
FONTS = rj_theme.load_app_fonts(W, H)
IS_WIDE = bool(FONTS["wide"])
IS_LARGE = bool(FONTS["large"])
IS_MID = (W >= 200 and H >= 200 and W < 300)
IS_SHORT = (H <= 200)
ET = rj_theme.EMBER_THEME

LOOT_DIR = os.path.join(rj_env.get_base_dir(), "loot", "Camera")
PHOTO_DIR = os.path.join(LOOT_DIR, "Photos")
VIDEO_DIR = os.path.join(LOOT_DIR, "Videos")
FB_DEVICE = "/dev/fb1" if os.path.exists("/dev/fb1") else "/dev/fb0"
FB_SIZE = W * H * 2
DEBOUNCE = 0.16

RESOLUTIONS = [
    ("VGA", 640, 480),
    ("HD", 1280, 720),
    ("FHD", 1920, 1080),
    ("8MP", 3280, 2464),
]

MENU_ITEMS = [
    ("Photo", "Live viewfinder & instant / hi-res snapshot"),
    ("Video", "30fps H.264 recording with live timer"),
    ("Timelapse", "Scheduled interval frames -> MP4 video"),
    ("Gallery", "Browse photos, thumbnails & recorded clips"),
    ("Settings", "Capture resolution & loot storage info"),
]

_running = True
_recording = False
_previewing = False
_preview_proc = None
_last_frame = None
_frame_lock = threading.Lock()

# Viewfinder HUD state shared with _preview_thread
_hud_mode = "PHOTO"
_hud_res = "8MP"
_hud_overlay_rgba = None
_hud_key = None
_thumb_cache = {}


def _sig(s, f):
    global _running
    _running = False


signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)


def _poll_input():
    act, ch = poll_ui_input(PINS, GPIO)
    if act and act in _STUCK_PINS and ch is None:
        return None, None
    return act, ch


def _check_camera():
    try:
        r = subprocess.run(
            ["rpicam-hello", "--list-cameras"],
            capture_output=True, text=True, timeout=5)
        return "imx219" in r.stdout.lower()
    except Exception:
        return False


def _chrome_heights():
    if IS_MID:
        return 22, 16
    if IS_WIDE and IS_SHORT:
        return 20, 15
    return None, None


def _show_msg(line1, line2="", kind="info", foot=""):
    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)
    hh, _ = _chrome_heights()
    rj_theme.draw_app_header(d, W, "EMBERJACK CAMERA" if IS_WIDE else "CAMERA", FONTS, badge="IMX219", hdr_h=hh)
    rj_theme.draw_status_card(d, W, H, line1, sub=line2, kind=kind, foot=foot, fonts=FONTS)
    LCD.LCD_ShowImage(img, 0, 0)


# ─── Viewfinder HUD Overlay ───

def _get_viewfinder_hud(mode_lbl, res_lbl):
    """Build and cache a lightweight RGBA Obsidian Ember viewfinder HUD."""
    global _hud_overlay_rgba, _hud_key
    key = (mode_lbl, res_lbl)
    if _hud_overlay_rgba is not None and _hud_key == key:
        return _hud_overlay_rgba

    hud = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(hud)

    top_h = 26 if IS_LARGE else (20 if IS_MID else (18 if IS_WIDE else 14))
    bot_h = 22 if IS_LARGE else (18 if IS_MID else (16 if IS_WIDE else 13))
    d.rectangle((0, 0, W - 1, top_h - 1), fill=(14, 14, 16, 175))
    d.line([(0, top_h - 1), (W - 1, top_h - 1)], fill=(184, 94, 36, 190), width=1)
    d.rectangle((0, H - bot_h, W - 1, H - 1), fill=(14, 14, 16, 185))
    d.line([(0, H - bot_h), (W - 1, H - bot_h)], fill=(43, 40, 38, 200), width=1)

    pad = 8 if IS_LARGE else (6 if IS_MID else (5 if IS_WIDE else 3))
    # Top-left mode pill
    pb = rj_theme.measure_text(FONTS["badge"], mode_lbl, d)
    pw = (pb[2] - pb[0]) + (10 if (IS_WIDE or IS_MID) else 6)
    ph = (pb[3] - pb[1]) + 4
    py = max(1, (top_h - ph) // 2)
    pill_fg = (217, 91, 85, 255) if mode_lbl == "VIDEO" else (242, 153, 74, 255)
    d.rounded_rectangle((pad, py, pad + pw, py + ph), radius=3, fill=(45, 29, 18, 225), outline=pill_fg)
    d.text((pad + pw // 2, py + ph // 2), mode_lbl, font=FONTS["badge"], fill=pill_fg, anchor="mm")

    # Top-right resolution pill
    rb = rj_theme.measure_text(FONTS["badge"], res_lbl, d)
    rw = (rb[2] - rb[0]) + (10 if (IS_WIDE or IS_MID) else 6)
    d.rounded_rectangle((W - pad - rw, py, W - pad, py + ph), radius=3, fill=(22, 22, 26, 220), outline=(184, 94, 36, 200))
    d.text((W - pad - rw // 2, py + ph // 2), res_lbl, font=FONTS["badge"], fill=(230, 223, 215, 255), anchor="mm")

    # Subtle viewfinder framing brackets
    mx = 18 if IS_LARGE else (12 if IS_MID else (10 if IS_WIDE else 6))
    my_top = top_h + (10 if IS_LARGE else (7 if IS_MID else 5))
    my_bot = H - bot_h - (10 if IS_LARGE else (7 if IS_MID else 5))
    blen = 14 if IS_LARGE else (10 if IS_MID else (8 if IS_WIDE else 5))
    bcol = (242, 153, 74, 175)
    # 4 corner L-brackets
    for (cx, cy, dx, dy) in (
        (mx, my_top, 1, 1),
        (W - mx, my_top, -1, 1),
        (mx, my_bot, 1, -1),
        (W - mx, my_bot, -1, -1),
    ):
        d.line([(cx, cy), (cx + dx * blen, cy)], fill=bcol, width=2 if (IS_LARGE or IS_MID) else 1)
        d.line([(cx, cy), (cx, cy + dy * blen)], fill=bcol, width=2 if (IS_LARGE or IS_MID) else 1)

    # Bottom controls hint
    if mode_lbl == "PHOTO":
        hint = format_footer_hint(
            W, H,
            hat_hint="OK:Snap K2:HR K3:B",
            hat_240_hint="OK:Snap K2:HiRes K3:Back",
            cardputer_hint="Ent:Snap H:HiRes Esc:Back",
            hdmi_hint="Enter:Snap  •  H:Hi-Res  •  Esc:Menu",
        )
    else:
        hint = format_footer_hint(
            W, H,
            hat_hint="OK:Rec K3:Back",
            hat_240_hint="OK:Record K3:Back",
            cardputer_hint="Ent:Record Esc:Back",
            hdmi_hint="Enter:Start Recording  •  Esc:Menu",
        )
    d.text((W // 2, H - bot_h // 2), rj_theme.truncate_text(d, hint, W - pad * 2, FONTS["sub"]), font=FONTS["sub"], fill=(230, 223, 215, 235), anchor="mm")

    arr = np.asarray(hud)
    _hud_overlay_rgba = (arr[:, :, :3].astype(np.float32), (arr[:, :, 3].astype(np.float32) / 255.0), arr[:, :, 3] > 0)
    _hud_key = key
    return _hud_overlay_rgba


# ─── Preview ───

def _start_preview(mode_lbl="PHOTO", res_lbl="8MP"):
    global _preview_proc, _previewing, _hud_mode, _hud_res
    _hud_mode = mode_lbl
    _hud_res = res_lbl
    if _previewing:
        return
    _previewing = True
    _preview_proc = subprocess.Popen(
        ["rpicam-vid", "--width", str(W), "--height", str(H),
         "--framerate", "15", "--codec", "yuv420",
         "--rotation", "180",
         "-t", "0", "--nopreview", "-o", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    threading.Thread(target=_preview_thread, daemon=True).start()


def _preview_thread():
    global _previewing, _last_frame

    frame_size = W * H * 3 // 2
    disp_type = getattr(LCD, "display_type", "")
    use_fb = disp_type in ("HDMI", "CARDPUTER_320")
    fb_fd = None
    fb_map = None
    if use_fb:
        try:
            fb_fd, fb_map = open_framebuffer(LCD, FB_DEVICE, W, H)
        except Exception:
            use_fb = False

    try:
        while _previewing and _running and _preview_proc and _preview_proc.poll() is None:
            raw = b""
            while len(raw) < frame_size and _previewing:
                chunk = _preview_proc.stdout.read(frame_size - len(raw))
                if not chunk:
                    break
                raw += chunk
            if len(raw) < frame_size:
                break

            yuv = np.frombuffer(raw, dtype=np.uint8)
            y = yuv[:W * H].reshape(H, W).astype(np.int16)
            u_raw = yuv[W * H:W * H + W * H // 4].reshape(H // 2, W // 2)
            v_raw = yuv[W * H + W * H // 4:].reshape(H // 2, W // 2)
            u = np.repeat(np.repeat(u_raw, 2, axis=0), 2, axis=1).astype(np.int16) - 128
            v = np.repeat(np.repeat(v_raw, 2, axis=0), 2, axis=1).astype(np.int16) - 128

            r = np.clip(y + ((359 * v) >> 8), 0, 255).astype(np.uint8)
            g = np.clip(y - ((88 * u + 183 * v) >> 8), 0, 255).astype(np.uint8)
            b = np.clip(y + ((454 * u) >> 8), 0, 255).astype(np.uint8)

            # Save clean un-overlaid frame for instant photo snap!
            with _frame_lock:
                _last_frame = np.stack([r, g, b], axis=-1)

            # Blend Obsidian Ember viewfinder HUD onto live preview
            hud_rgb, hud_a, hud_mask = _get_viewfinder_hud(_hud_mode, _hud_res)
            dr = r.copy()
            dg = g.copy()
            db = b.copy()
            a = hud_a[hud_mask]
            inv = 1.0 - a
            dr[hud_mask] = (hud_rgb[hud_mask, 0] * a + dr[hud_mask].astype(np.float32) * inv).astype(np.uint8)
            dg[hud_mask] = (hud_rgb[hud_mask, 1] * a + dg[hud_mask].astype(np.float32) * inv).astype(np.uint8)
            db[hud_mask] = (hud_rgb[hud_mask, 2] * a + db[hud_mask].astype(np.float32) * inv).astype(np.uint8)

            if use_fb and fb_map is not None:
                rgb565 = (
                    ((dr.astype(np.uint16) >> 3) << 11)
                    | ((dg.astype(np.uint16) >> 2) << 5)
                    | (db.astype(np.uint16) >> 3)
                )
                fb_map.seek(0)
                fb_map.write(rgb565.tobytes())
            else:
                LCD.LCD_ShowImage(Image.fromarray(np.stack([dr, dg, db], axis=-1), "RGB"), 0, 0)
    except Exception:
        pass
    finally:
        try:
            if fb_map is not None:
                fb_map.close()
            if fb_fd is not None:
                os.close(fb_fd)
        except Exception:
            pass
        _previewing = False


def _stop_preview():
    global _preview_proc, _previewing
    _previewing = False
    if _preview_proc and _preview_proc.poll() is None:
        _preview_proc.kill()
        try:
            _preview_proc.wait(timeout=2)
        except Exception:
            pass
    _preview_proc = None
    time.sleep(0.25)


# ─── Capture ───

def _snap_photo():
    """Instant capture from preview frame (no interruption)."""
    os.makedirs(PHOTO_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(PHOTO_DIR, f"photo_{ts}.jpg")
    with _frame_lock:
        frame = _last_frame
    if frame is None:
        return None, 0
    try:
        Image.fromarray(frame, "RGB").save(path, "JPEG", quality=90)
        return path, os.path.getsize(path) // 1024
    except Exception:
        return None, 0


def _snap_hires(res_idx):
    """Hi-res capture via rpicam-still (pauses preview)."""
    os.makedirs(PHOTO_DIR, exist_ok=True)
    res_name, w, h = RESOLUTIONS[res_idx]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(PHOTO_DIR, f"hires_{ts}.jpg")
    _stop_preview()
    subprocess.run(
        ["rpicam-still", "-o", path, "--width", str(w), "--height", str(h),
         "-t", "500", "--nopreview", "-q", "90", "--rotation", "180"],
        capture_output=True, timeout=10)
    _start_preview("PHOTO", res_name)
    if os.path.isfile(path):
        return path, os.path.getsize(path) // 1024
    return None, 0


def _start_recording(res_idx):
    global _recording
    os.makedirs(VIDEO_DIR, exist_ok=True)
    _, w, h = RESOLUTIONS[min(res_idx, 2)]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(VIDEO_DIR, f"video_{ts}.h264")
    _recording = True
    subprocess.Popen(
        ["rpicam-vid", "-o", path, "--width", str(w), "--height", str(h),
         "--framerate", "30", "-t", "0", "--nopreview", "--rotation", "180",
         "--codec", "h264"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return path


def _stop_recording():
    global _recording
    subprocess.run(["pkill", "-INT", "rpicam-vid"], capture_output=True)
    time.sleep(0.5)
    _recording = False


# ─── File lists & playback ───

def _list_photos():
    if not os.path.isdir(PHOTO_DIR):
        return []
    return sorted(glob.glob(os.path.join(PHOTO_DIR, "*.jpg")), reverse=True)


def _list_videos():
    if not os.path.isdir(VIDEO_DIR):
        return []
    return sorted(glob.glob(os.path.join(VIDEO_DIR, "*.h264")), reverse=True)


def _show_photo(path, idx=0, total=1):
    try:
        src = Image.open(path).convert("RGB")
        img = Image.new("RGB", (W, H), ET["bg_base"])
        src.thumbnail((W, H), Image.BILINEAR)
        ox = (W - src.width) // 2
        oy = (H - src.height) // 2
        img.paste(src, (ox, oy))
        # Subtle Obsidian Ember bottom photo caption overlay
        bar_h = 24 if IS_LARGE else (18 if (IS_WIDE or IS_MID) else 14)
        overlay = Image.new("RGBA", (W, bar_h), (14, 14, 16, 200))
        od = ImageDraw.Draw(overlay)
        od.line([(0, 0), (W - 1, 0)], fill=(184, 94, 36, 220), width=1)
        lbl = f"{idx + 1}/{total}  •  {os.path.basename(path)}"
        hint = format_footer_hint(
            W, H,
            hat_hint="K2:Del K3:B",
            hat_240_hint="L/R K2:Del K3:Back",
            cardputer_hint="L/R D:Del Esc:Back",
            hdmi_hint="L/R:Nav  D:Del  Esc:Back",
        )
        hb = rj_theme.measure_text(FONTS["sub"], hint, od)
        hw = hb[2] - hb[0]
        od.text((W - 6 - hw, bar_h // 2), hint, font=FONTS["sub"], fill=(155, 146, 138, 255), anchor="lm")
        od.text((8, bar_h // 2), rj_theme.truncate_text(od, lbl, max(24, W - hw - 22), FONTS["sub"]), font=FONTS["sub"], fill=(245, 239, 232, 255), anchor="lm")
        base_rgba = img.convert("RGBA")
        base_rgba.paste(overlay, (0, H - bar_h), overlay)
        LCD.LCD_ShowImage(base_rgba.convert("RGB"), 0, 0)
        return True
    except Exception:
        return False


def _play_video(path):
    """Play h264 video via ffmpeg to open_framebuffer / SPI LCD."""
    proc = None
    fb_fd = fb_map = None
    try:
        proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "quiet",
             "-i", path,
             "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2",
             "-pix_fmt", "rgb565le", "-f", "rawvideo", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        disp_type = getattr(LCD, "display_type", "")
        use_fb = disp_type in ("HDMI", "CARDPUTER_320")
        if use_fb:
            fb_fd, fb_map = open_framebuffer(LCD, FB_DEVICE, W, H)

        while _running:
            raw = proc.stdout.read(FB_SIZE)
            if not raw or len(raw) < FB_SIZE:
                break
            if use_fb and fb_map is not None:
                fb_map.seek(0)
                fb_map.write(raw)
            else:
                arr = np.frombuffer(raw, dtype="<u2").reshape(H, W)
                rr = ((arr >> 11) & 0x1F) << 3
                gg = ((arr >> 5) & 0x3F) << 2
                bb = (arr & 0x1F) << 3
                LCD.LCD_ShowImage(Image.fromarray(np.stack([rr.astype(np.uint8), gg.astype(np.uint8), bb.astype(np.uint8)], axis=-1), "RGB"), 0, 0)
            act, ch = _poll_input()
            if is_dismiss(act):
                break
    except Exception:
        pass
    finally:
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
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


# ─── Native Obsidian Ember Screens ───

def _draw_menu(sel, res_idx):
    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)

    n_photos = len(_list_photos())
    n_videos = len(_list_videos())
    res_name, rw, rh = RESOLUTIONS[res_idx]
    hh, fh = _chrome_heights()

    hdr_h = rj_theme.draw_app_header(
        d, W, "EMBERJACK CAMERA" if IS_WIDE else "CAMERA", FONTS,
        badge="IMX219" if (IS_WIDE or not IS_MID) else res_name,
        right_text=f"{n_photos} IMG • {n_videos} VID" if IS_LARGE else "",
        right_pill=res_name,
        hdr_h=hh,
    )
    ftr_h = rj_theme.draw_app_footer(
        d, W, H,
        format_footer_hint(
            W, H,
            hat_hint="OK:Open K3:Exit",
            hat_240_hint="U/D:Nav OK:Open K3:Exit",
            cardputer_hint="U/D:Nav Ent:Open Esc:Exit",
            hdmi_hint="Arrows:Select  Enter:Open  Esc:Exit",
        ),
        FONTS,
        ftr_h=fh,
    )

    pad = 8 if IS_LARGE else (6 if IS_MID else (4 if IS_WIDE else 3))
    y0 = hdr_h + pad
    y1 = H - ftr_h - pad
    split = W >= 480

    list_x0 = pad
    list_x1 = int(W * 0.56) - 4 if split else (W - pad)
    row_h = max(16, (y1 - y0) // len(MENU_ITEMS))

    for i, (title, desc) in enumerate(MENU_ITEMS):
        ry = y0 + i * row_h
        ry1 = min(y1, ry + row_h - (5 if IS_LARGE else (4 if IS_MID else (3 if IS_WIDE else 2))))
        on = (i == sel)
        rj_theme.draw_card(
            d, (list_x0, ry, list_x1, ry1),
            fill=ET["select_bg"] if on else ET["bg_surface"],
            outline=ET["border_accent"] if on else ET["border_subtle"],
            radius=6 if (IS_WIDE or IS_MID) else 3,
            accent_left=ET["accent"] if on else None,
            accent_width=3 if (IS_WIDE or IS_MID) else 2,
        )
        lx = list_x0 + (12 if (IS_WIDE or IS_MID) else 5)
        if IS_LARGE:
            d.text((lx, ry + 7), title, font=FONTS["body_bold"], fill=ET["accent_bright"] if on else ET["text_bright"])
            d.text((lx, ry + 25), rj_theme.truncate_text(d, desc, list_x1 - lx - 10, FONTS["sub"]), font=FONTS["sub"], fill=ET["text_muted"])
        elif IS_MID and (ry1 - ry) >= 28:
            d.text((lx, ry + 4), title, font=FONTS["body_bold"], fill=ET["accent_bright"] if on else ET["text_bright"])
            d.text((lx, ry + 18), rj_theme.truncate_text(d, desc, list_x1 - lx - 8, FONTS["sub"]), font=FONTS["sub"], fill=ET["text_muted"])
        else:
            d.text((lx, (ry + ry1) // 2), title, font=FONTS["body_bold"] if on else FONTS["body"], fill=ET["accent_bright"] if on else ET["text_primary"], anchor="lm")

    if split:
        px0 = int(W * 0.56) + 6
        px1 = W - pad
        rj_theme.draw_card(
            d, (px0, y0, px1, y1),
            fill=ET["bg_surface"], outline=ET["border_subtle"],
            radius=8, accent_left=ET["accent_dim"], accent_width=3,
        )
        ix = px0 + 14
        iw = (px1 - px0) - 24
        iy = y0 + 14
        rj_theme.draw_pill(
            d, ix, iy, "CAMERA STUDIO", FONTS["badge"],
            bg=ET["select_bg"], fg=ET["accent_bright"], outline=ET["border_accent"],
            pad_x=6, pad_y=2, radius=3,
        )
        iy += 28
        d.text((ix, iy), MENU_ITEMS[sel][0], font=FONTS["title"], fill=ET["text_bright"])
        iy += 22
        for ln in rj_theme.wrap_text(d, MENU_ITEMS[sel][1], iw, FONTS["sub"])[:3]:
            d.text((ix, iy), ln, font=FONTS["sub"], fill=ET["text_muted"])
            iy += 15
        iy += 12
        d.line([(ix, iy), (px1 - 12, iy)], fill=ET["border_subtle"], width=1)
        iy += 12
        for k_lbl, v_lbl in (
            ("Resolution", f"{res_name} ({rw}x{rh})"),
            ("Saved Photos", str(n_photos)),
            ("Saved Videos", str(n_videos)),
            ("Loot Path", "loot/Camera"),
        ):
            if iy + 18 > y1 - 10:
                break
            d.text((ix, iy), k_lbl, font=FONTS["sub"], fill=ET["text_muted"])
            vb = rj_theme.measure_text(FONTS["body_bold"], v_lbl, d)
            d.text((px1 - 12 - (vb[2] - vb[0]), iy - 1), v_lbl, font=FONTS["body_bold"], fill=ET["text_primary"])
            iy += 22

    LCD.LCD_ShowImage(img, 0, 0)


def _get_thumb(path, tw, th):
    key = (path, tw, th)
    cached = _thumb_cache.get(key)
    if cached is not None:
        return cached
    try:
        im = Image.open(path).convert("RGB")
        im.thumbnail((tw, th), Image.BILINEAR)
        if len(_thumb_cache) >= 24:
            _thumb_cache.clear()
        _thumb_cache[key] = im
        return im
    except Exception:
        return None


def _draw_gallery(files, sel):
    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)
    hh, fh = _chrome_heights()

    hdr_h = rj_theme.draw_app_header(
        d, W, "GALLERY", FONTS,
        badge=f"{len(files)} FILES",
        right_pill=f"{sel + 1}/{len(files)}" if files else "",
        hdr_h=hh,
    )
    ftr_h = rj_theme.draw_app_footer(
        d, W, H,
        format_footer_hint(
            W, H,
            hat_hint="OK:View K2:Del K3:B",
            hat_240_hint="OK:View K2:Del K3:Back",
            cardputer_hint="Ent:View D:Del Esc:Back",
            hdmi_hint="Arrows:Nav  Enter:View/Play  D:Delete  Esc:Back",
        ),
        FONTS,
        ftr_h=fh,
    )

    if not files:
        rj_theme.draw_status_card(
            d, W, H, "Gallery Empty",
            sub="Capture a photo or video first", kind="info",
            foot=format_footer_hint(
                W, H,
                hat_hint="K3:Back",
                hat_240_hint="KEY3: Return to menu",
                cardputer_hint="Esc: Return to menu",
                hdmi_hint="Esc to return to menu",
            ),
            fonts=FONTS,
        )
        LCD.LCD_ShowImage(img, 0, 0)
        return

    pad = 8 if IS_LARGE else (6 if IS_MID else (4 if IS_WIDE else 3))
    y0 = hdr_h + pad
    y1 = H - ftr_h - pad
    split = W >= 480

    list_x0 = pad
    list_x1 = int(W * 0.54) - 4 if split else (W - pad - (6 if (IS_WIDE or IS_MID) else 4))
    row_h = 32 if IS_LARGE else (26 if IS_MID else (22 if IS_WIDE else 18))
    vis = max(1, (y1 - y0) // row_h)
    st = max(0, min(sel - vis + 1, len(files) - vis)) if len(files) > vis else 0

    for slot in range(vis):
        idx = st + slot
        if idx >= len(files):
            break
        ry = y0 + slot * row_h
        ry1 = min(y1, ry + row_h - (4 if (IS_WIDE or IS_MID) else 2))
        on = (idx == sel)
        fpath = files[idx]
        is_img = fpath.endswith(".jpg")
        name = os.path.basename(fpath)
        try:
            sz_kb = os.path.getsize(fpath) // 1024
        except OSError:
            sz_kb = 0

        rj_theme.draw_card(
            d, (list_x0, ry, list_x1, ry1),
            fill=ET["select_bg"] if on else ET["bg_surface"],
            outline=ET["border_accent"] if on else ET["border_subtle"],
            radius=6 if (IS_WIDE or IS_MID) else 3,
            accent_left=ET["accent"] if on else None,
            accent_width=3 if (IS_WIDE or IS_MID) else 2,
        )

        lx = list_x0 + (8 if (IS_WIDE or IS_MID) else 4)
        rx = list_x1 - (8 if (IS_WIDE or IS_MID) else 3)
        if IS_WIDE or IS_MID:
            ext_lbl = "IMG" if is_img else "VID"
            pw, _ = rj_theme.draw_pill(
                d, lx, (ry + ry1) // 2 - 7, ext_lbl, FONTS["badge"],
                bg=ET["bg_well"], fg=ET["accent_bright"] if is_img else ET["warn"],
                outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3,
            )
            lx += pw + 6
            sz_txt = f"{sz_kb}KB"
            sb = rj_theme.measure_text(FONTS["badge"], sz_txt, d)
            d.text((rx - (sb[2] - sb[0]), (ry + ry1) // 2), sz_txt, font=FONTS["badge"], fill=ET["text_muted"], anchor="lm")
            rx -= (sb[2] - sb[0]) + 6

        d.text(
            (lx, (ry + ry1) // 2),
            rj_theme.truncate_text(d, name, max(20, rx - lx), FONTS["body_bold"] if on else FONTS["sub"]),
            font=FONTS["body_bold"] if on else FONTS["sub"],
            fill=ET["accent_bright"] if on else ET["text_primary"],
            anchor="lm",
        )

    rj_theme.draw_scrollbar(d, list_x1 + 3, y0, y1, st, vis, len(files), width=3 if (IS_WIDE or IS_MID) else 2)

    if split:
        px0 = int(W * 0.54) + 6
        px1 = W - pad
        rj_theme.draw_card(
            d, (px0, y0, px1, y1),
            fill=ET["bg_surface"], outline=ET["border_subtle"], radius=8,
        )
        fpath = files[sel]
        is_img = fpath.endswith(".jpg")
        tw = (px1 - px0) - 20
        th = (y1 - y0) - 70
        vx0, vy0 = px0 + 10, y0 + 10
        rj_theme.draw_card(d, (vx0, vy0, vx0 + tw, vy0 + th), fill=ET["bg_well"], outline=ET["border_subtle"], radius=6)

        if is_img:
            thumb = _get_thumb(fpath, tw - 4, th - 4)
            if thumb is not None:
                img.paste(thumb, (vx0 + (tw - thumb.width) // 2, vy0 + (th - thumb.height) // 2))
            else:
                d.text((vx0 + tw // 2, vy0 + th // 2), "Preview N/A", font=FONTS["sub"], fill=ET["text_muted"], anchor="mm")
        else:
            d.text((vx0 + tw // 2, vy0 + th // 2 - 8), "H.264 VIDEO CLIP", font=FONTS["body_bold"], fill=ET["accent_bright"], anchor="mm")
            d.text((vx0 + tw // 2, vy0 + th // 2 + 12), "Press Enter / OK to play", font=FONTS["sub"], fill=ET["text_muted"], anchor="mm")

        my = vy0 + th + 10
        d.text((vx0, my), rj_theme.truncate_text(d, os.path.basename(fpath), tw, FONTS["body_bold"]), font=FONTS["body_bold"], fill=ET["text_bright"])
        try:
            sz = os.path.getsize(fpath) // 1024
            mt = datetime.fromtimestamp(os.path.getmtime(fpath)).strftime("%Y-%m-%d %H:%M")
        except OSError:
            sz, mt = 0, ""
        d.text((vx0, my + 18), f"{sz} KB  •  {mt}", font=FONTS["sub"], fill=ET["text_muted"])

    LCD.LCD_ShowImage(img, 0, 0)


def _draw_settings(res_idx, sel):
    res_name, rw, rh = RESOLUTIONS[res_idx]
    items = [
        ("Resolution", f"{res_name} ({rw}x{rh})"),
        ("Saved Photos", str(len(_list_photos()))),
        ("Saved Videos", str(len(_list_videos()))),
        ("Back to Menu", ""),
    ]
    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)
    hh, fh = _chrome_heights()

    hdr_h = rj_theme.draw_app_header(d, W, "CAMERA SETTINGS" if IS_WIDE else "SETTINGS", FONTS, badge=res_name, hdr_h=hh)
    ftr_h = rj_theme.draw_app_footer(
        d, W, H,
        format_footer_hint(
            W, H,
            hat_hint="OK:Change K3:Back",
            hat_240_hint="U/D:Nav OK:Change K3:Back",
            cardputer_hint="U/D:Nav Ent:Change Esc:Back",
            hdmi_hint="Arrows:Nav  Enter:Cycle Resolution  Esc:Back",
        ),
        FONTS,
        ftr_h=fh,
    )

    pad = 10 if IS_LARGE else (7 if IS_MID else (4 if IS_WIDE else 3))
    y0 = hdr_h + pad
    y1 = H - ftr_h - pad
    row_h = min(46 if IS_LARGE else (36 if IS_MID else 28), max(16, (y1 - y0) // len(items)))

    for i, (lbl, val) in enumerate(items):
        ry = y0 + i * row_h
        ry1 = min(y1, ry + row_h - (6 if IS_LARGE else (4 if IS_MID else 3)))
        on = (i == sel)
        rj_theme.draw_card(
            d, (pad, ry, W - pad, ry1),
            fill=ET["select_bg"] if on else ET["bg_surface"],
            outline=ET["border_accent"] if on else ET["border_subtle"],
            radius=6 if (IS_WIDE or IS_MID) else 3,
            accent_left=ET["accent"] if on else None,
            accent_width=3 if (IS_WIDE or IS_MID) else 2,
        )
        lx = pad + (12 if (IS_WIDE or IS_MID) else 5)
        d.text((lx, (ry + ry1) // 2), lbl, font=FONTS["body_bold"] if on else FONTS["body"], fill=ET["accent_bright"] if on else ET["text_primary"], anchor="lm")
        if val:
            vb = rj_theme.measure_text(FONTS["badge"], val, d)
            vw = (vb[2] - vb[0]) + (10 if (IS_WIDE or IS_MID) else 6)
            rj_theme.draw_pill(
                d, W - pad - vw - (8 if (IS_WIDE or IS_MID) else 4), (ry + ry1) // 2 - ((vb[3] - vb[1]) // 2 + 2),
                val, FONTS["badge"],
                bg=ET["bg_well"], fg=ET["accent_bright"] if i == 0 else ET["text_primary"],
                outline=ET["border_accent"] if (i == 0 and on) else ET["border_subtle"],
                pad_x=5 if (IS_WIDE or IS_MID) else 3, pad_y=1, radius=3,
            )

    LCD.LCD_ShowImage(img, 0, 0)


def _draw_rec_screen(rec_time, res_idx):
    """Obsidian Ember studio recording screen (when preview paused for H.264 encode)."""
    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)
    hh, fh = _chrome_heights()

    res_name, rw, rh = RESOLUTIONS[min(res_idx, 2)]
    hdr_h = rj_theme.draw_app_header(d, W, "VIDEO RECORDING" if IS_WIDE else "REC", FONTS, badge="REC", right_pill=f"{res_name} 30FPS", hdr_h=hh)
    ftr_h = rj_theme.draw_app_footer(
        d, W, H,
        format_footer_hint(
            W, H,
            hat_hint="OK:Stop Rec",
            hat_240_hint="OK:Stop Recording",
            cardputer_hint="Enter:Stop Recording",
            hdmi_hint="Enter:Stop Recording",
        ),
        FONTS,
        ftr_h=fh,
    )

    avail_h = max(56, H - hdr_h - ftr_h - (16 if IS_LARGE else 10))
    cw = min(W - (24 if IS_LARGE else 12), 380 if IS_LARGE else (210 if IS_MID else W - 12))
    ch = min(avail_h, 170 if IS_LARGE else (140 if IS_MID else avail_h))
    cx0 = (W - cw) // 2
    cy0 = hdr_h + max(4, (H - hdr_h - ftr_h - ch) // 2)
    rj_theme.draw_card(
        d, (cx0, cy0, cx0 + cw, cy0 + ch),
        fill=ET["bg_surface"], outline=ET["danger"],
        radius=8 if (IS_WIDE or IS_MID) else 4, accent_left=ET["danger"], accent_width=4 if (IS_WIDE or IS_MID) else 2,
    )

    blink = int(time.time() * 2) % 2 == 0
    dot_r = 7 if IS_LARGE else (6 if IS_MID else (5 if IS_WIDE else 4))
    dot_cx = cx0 + cw // 2 - (44 if IS_LARGE else (34 if IS_MID else 26))
    dot_cy = cy0 + (28 if IS_LARGE else (22 if IS_MID else (16 if IS_WIDE else 14)))
    d.ellipse((dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r), fill=ET["danger"] if blink else ET["danger_bg"], outline=ET["danger"])
    d.text((dot_cx + dot_r + 8, dot_cy), "RECORDING", font=FONTS["title"], fill=ET["danger"], anchor="lm")

    m, s = divmod(int(rec_time), 60)
    big_t = rj_env.load_font(34 if IS_LARGE else (26 if IS_MID else (20 if IS_WIDE else 16)), bold=True, mono=True)
    d.text((cx0 + cw // 2, cy0 + ch // 2 + (4 if IS_LARGE else 2)), f"{m:02d}:{s:02d}", font=big_t, fill=ET["text_bright"], anchor="mm")
    d.text((cx0 + cw // 2, cy0 + ch - (22 if IS_LARGE else (18 if IS_MID else 12))), f"{res_name} ({rw}x{rh}) • 30fps H.264", font=FONTS["sub"], fill=ET["text_muted"], anchor="mm")

    LCD.LCD_ShowImage(img, 0, 0)


def _draw_timelapse(tl_active, interval_label, interval_sec, frame_count, elapsed, next_in):
    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)
    hh, fh = _chrome_heights()

    hdr_h = rj_theme.draw_app_header(
        d, W, "TIMELAPSE", FONTS,
        badge="ACTIVE" if tl_active else "READY",
        right_pill=interval_label,
        hdr_h=hh,
    )
    act_lbl = "Stop & Compile" if tl_active else "Start"
    act_short = "Stop" if tl_active else "Go"
    ftr_h = rj_theme.draw_app_footer(
        d, W, H,
        format_footer_hint(
            W, H,
            hat_hint=f"OK:{act_short} U/D:Int K3:B",
            hat_240_hint=f"OK:{act_short} U/D:Int K3:Back",
            cardputer_hint=f"Ent:{act_short} U/D:Int Esc:Back",
            hdmi_hint=f"Enter:{act_lbl}  Up/Dn:Interval  Esc:Back",
        ),
        FONTS,
        ftr_h=fh,
    )

    pad = 10 if IS_LARGE else (7 if IS_MID else (4 if IS_WIDE else 4))
    avail_h = max(60, H - hdr_h - ftr_h - pad * 2)
    cw = min(W - pad * 2, 460 if IS_LARGE else W - pad * 2)
    ch = min(avail_h, 200 if IS_LARGE else (168 if IS_MID else avail_h))
    cx0 = (W - cw) // 2
    cy0 = hdr_h + pad + max(0, (avail_h - ch) // 2)

    accent = ET["ok"] if tl_active else ET["accent"]
    rj_theme.draw_card(
        d, (cx0, cy0, cx0 + cw, cy0 + ch),
        fill=ET["bg_surface"], outline=accent,
        radius=8 if (IS_WIDE or IS_MID) else 4, accent_left=accent, accent_width=4 if (IS_WIDE or IS_MID) else 2,
    )

    ix = cx0 + (16 if IS_LARGE else (12 if IS_MID else 8))
    ix1 = cx0 + cw - (16 if IS_LARGE else (12 if IS_MID else 8))
    iy = cy0 + (16 if IS_LARGE else (12 if IS_MID else 7))

    m, s = divmod(int(elapsed), 60)
    rows = [
        ("Status", "CAPTURING" if tl_active else "STANDBY", ET["ok"] if tl_active else ET["warn"]),
        ("Interval", interval_label, ET["accent_bright"]),
        ("Captured Frames", str(frame_count), ET["text_bright"]),
        ("Elapsed", f"{m:02d}m {s:02d}s" if tl_active else "00m 00s", ET["text_muted"]),
    ]
    step = 28 if IS_LARGE else (24 if IS_MID else (17 if IS_WIDE else 14))
    bar_reserve = (26 if IS_LARGE else (18 if IS_MID else 14)) if tl_active else 6
    for klbl, vlbl, vcol in rows:
        if iy + step > cy0 + ch - bar_reserve:
            break
        d.text((ix, iy), klbl, font=FONTS["body"] if (IS_LARGE or IS_MID) else FONTS["sub"], fill=ET["text_muted"])
        vb = rj_theme.measure_text(FONTS["body_bold"] if (IS_LARGE or IS_MID) else FONTS["sub"], vlbl, d)
        d.text((ix1 - (vb[2] - vb[0]), iy), vlbl, font=FONTS["body_bold"] if (IS_LARGE or IS_MID) else FONTS["sub"], fill=vcol)
        iy += step

    if tl_active and interval_sec > 0:
        frac = max(0.0, min(1.0, 1.0 - (next_in / float(interval_sec))))
        rj_theme.draw_progress_bar(
            d, (ix, cy0 + ch - (20 if IS_LARGE else (15 if IS_MID else 11)), ix1, cy0 + ch - (12 if IS_LARGE else (9 if IS_MID else 6))),
            frac, fill=ET["ok"], track=ET["bg_well"], outline=ET["border_subtle"], radius=3,
        )

    LCD.LCD_ShowImage(img, 0, 0)


# ─── Main ───

def main():
    global _running, _recording

    if not _check_camera():
        _show_msg("No Camera Detected", "Connect IMX219 CSI camera & reboot", kind="err")
        time.sleep(2.5)
        GPIO.cleanup()
        return 1

    os.makedirs(PHOTO_DIR, exist_ok=True)
    os.makedirs(VIDEO_DIR, exist_ok=True)

    INTERVALS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600]
    INTERVAL_LABELS = ["1s", "2s", "5s", "10s", "15s", "30s", "1min", "2min", "5min", "10min"]

    state = "menu"
    menu_sel = 0
    gallery_sel = 0
    settings_sel = 0
    res_idx = 3
    last_btn = 0.0
    rec_start = 0.0
    tl_active = False
    tl_interval_idx = 3
    tl_frame_count = 0
    tl_last_capture = 0.0
    tl_session_dir = ""
    dirty = True

    try:
        with text_input_mode():
            flush_input()
            while _running:
                act, ch = _poll_input()
                now = time.time()

                # ── Menu ──
                if state == "menu":
                    if is_back(act):
                        break
                    if act == "UP" and now - last_btn > DEBOUNCE:
                        last_btn = now
                        menu_sel = (menu_sel - 1) % len(MENU_ITEMS)
                        dirty = True
                    elif act == "DOWN" and now - last_btn > DEBOUNCE:
                        last_btn = now
                        menu_sel = (menu_sel + 1) % len(MENU_ITEMS)
                        dirty = True
                    elif is_confirm(act) and now - last_btn > DEBOUNCE:
                        last_btn = now
                        dirty = True
                        if menu_sel == 0:
                            state = "photo"
                            _start_preview("PHOTO", RESOLUTIONS[res_idx][0])
                        elif menu_sel == 1:
                            state = "video"
                            _start_preview("VIDEO", RESOLUTIONS[min(res_idx, 2)][0])
                        elif menu_sel == 2:
                            state = "timelapse"
                        elif menu_sel == 3:
                            state = "gallery"
                            gallery_sel = 0
                        elif menu_sel == 4:
                            state = "settings"
                            settings_sel = 0
                        continue
                    if dirty:
                        _draw_menu(menu_sel, res_idx)
                        dirty = False

                # ── Photo (preview running with Ember HUD) ──
                elif state == "photo":
                    if is_back(act):
                        _stop_preview()
                        state = "menu"
                        dirty = True
                        continue
                    if is_confirm(act) and now - last_btn > 0.45:
                        last_btn = now
                        path, size = _snap_photo()
                        _stop_preview()
                        if path:
                            _show_msg(f"Photo Saved ({size}KB)", os.path.basename(path), kind="ok")
                        else:
                            _show_msg("Capture Failed", "No frame available", kind="err")
                        time.sleep(1.5)
                        _start_preview("PHOTO", RESOLUTIONS[res_idx][0])
                    elif is_action(act, ch, "KEY2", "h") and now - last_btn > 0.45:
                        last_btn = now
                        _show_msg("Hi-Res Capture...", RESOLUTIONS[res_idx][0], kind="info")
                        path, size = _snap_hires(res_idx)
                        _stop_preview()
                        if path:
                            _show_msg(f"Hi-Res Saved ({size}KB)", os.path.basename(path), kind="ok")
                        else:
                            _show_msg("Hi-Res Capture Failed", "", kind="err")
                        time.sleep(1.5)
                        _start_preview("PHOTO", RESOLUTIONS[res_idx][0])

                # ── Video ──
                elif state == "video":
                    if is_back(act) and not _recording:
                        _stop_preview()
                        state = "menu"
                        dirty = True
                        continue
                    if is_confirm(act) and now - last_btn > DEBOUNCE:
                        last_btn = now
                        if not _recording:
                            _stop_preview()
                            _start_recording(res_idx)
                            rec_start = now
                        else:
                            _stop_recording()
                            _show_msg("Video Saved!", f"{int(now - rec_start)}s clip saved", kind="ok")
                            time.sleep(1.2)
                            _start_preview("VIDEO", RESOLUTIONS[min(res_idx, 2)][0])
                    if _recording:
                        _draw_rec_screen(now - rec_start, res_idx)

                # ── Timelapse ──
                elif state == "timelapse":
                    if is_back(act) and not tl_active:
                        state = "menu"
                        dirty = True
                        continue
                    if is_confirm(act) and now - last_btn > 0.3:
                        last_btn = now
                        if not tl_active:
                            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            tl_session_dir = os.path.join(LOOT_DIR, "Timelapse", f"session_{ts}")
                            os.makedirs(tl_session_dir, exist_ok=True)
                            tl_active = True
                            tl_frame_count = 0
                            tl_last_capture = 0.0
                            rec_start = now
                        else:
                            tl_active = False
                            _show_msg("Compiling Timelapse...", f"{tl_frame_count} frames -> MP4", kind="info")
                            fps = min(30, max(5, tl_frame_count // 30))
                            output = os.path.join(tl_session_dir, "timelapse.mp4")
                            subprocess.run(
                                ["ffmpeg", "-hide_banner", "-loglevel", "quiet",
                                 "-framerate", str(fps),
                                 "-i", os.path.join(tl_session_dir, "frame_%05d.jpg"),
                                 "-vf", "scale=1280:720",
                                 "-c:v", "libx264", "-preset", "ultrafast",
                                 "-pix_fmt", "yuv420p", "-crf", "28", output],
                                capture_output=True, timeout=600)
                            if os.path.isfile(output) and os.path.getsize(output) > 100:
                                sz = os.path.getsize(output) // 1024
                                _show_msg(f"Timelapse Ready ({sz}KB)", "timelapse.mp4", kind="ok")
                            else:
                                _show_msg("Compile Failed", "Check ffmpeg / frame count", kind="err")
                            time.sleep(2)
                        dirty = True

                    if not tl_active:
                        if act == "UP" and now - last_btn > DEBOUNCE:
                            last_btn = now
                            tl_interval_idx = (tl_interval_idx + 1) % len(INTERVALS)
                            dirty = True
                        elif act == "DOWN" and now - last_btn > DEBOUNCE:
                            last_btn = now
                            tl_interval_idx = (tl_interval_idx - 1) % len(INTERVALS)
                            dirty = True

                    if tl_active and now - tl_last_capture >= INTERVALS[tl_interval_idx]:
                        tl_last_capture = now
                        _, cw, ch_px = RESOLUTIONS[min(res_idx, 2)]
                        path = os.path.join(tl_session_dir, f"frame_{tl_frame_count:05d}.jpg")
                        subprocess.run(
                            ["rpicam-still", "-o", path, "--width", str(cw), "--height", str(ch_px),
                             "-t", "300", "--nopreview", "-q", "85", "--rotation", "180"],
                            capture_output=True, timeout=10)
                        if os.path.isfile(path):
                            tl_frame_count += 1
                        dirty = True

                    next_in = max(0.0, INTERVALS[tl_interval_idx] - (now - tl_last_capture)) if tl_active else 0.0
                    if dirty or tl_active:
                        _draw_timelapse(
                            tl_active, INTERVAL_LABELS[tl_interval_idx],
                            INTERVALS[tl_interval_idx], tl_frame_count,
                            (now - rec_start) if tl_active else 0.0, next_in,
                        )
                        dirty = False

                # ── Gallery ──
                elif state == "gallery":
                    files = _list_photos() + _list_videos()
                    if gallery_sel >= len(files):
                        gallery_sel = max(0, len(files) - 1)

                    if is_back(act):
                        state = "menu"
                        dirty = True
                        continue
                    if act == "UP" and now - last_btn > DEBOUNCE and files:
                        last_btn = now
                        gallery_sel = (gallery_sel - 1) % len(files)
                        dirty = True
                    elif act == "DOWN" and now - last_btn > DEBOUNCE and files:
                        last_btn = now
                        gallery_sel = (gallery_sel + 1) % len(files)
                        dirty = True
                    elif is_confirm(act) and now - last_btn > DEBOUNCE and files:
                        last_btn = now
                        f = files[gallery_sel]
                        if f.endswith(".jpg"):
                            photos = [p for p in files if p.endswith(".jpg")]
                            photo_idx = photos.index(f) if f in photos else 0
                            _show_photo(photos[photo_idx], photo_idx, len(photos))
                            confirm_foot = format_footer_hint(
                                W, H,
                                hat_hint="OK:Yes K3:No",
                                hat_240_hint="OK:Yes  K3:Cancel",
                                cardputer_hint="Enter:Yes  Esc:Cancel",
                                hdmi_hint="Enter: Yes  •  Esc: Cancel",
                            )
                            while _running:
                                ba, bc = _poll_input()
                                if is_back(ba):
                                    break
                                if ba == "LEFT" and photo_idx > 0:
                                    photo_idx -= 1
                                    _show_photo(photos[photo_idx], photo_idx, len(photos))
                                    time.sleep(0.15)
                                elif ba == "RIGHT" and photo_idx < len(photos) - 1:
                                    photo_idx += 1
                                    _show_photo(photos[photo_idx], photo_idx, len(photos))
                                    time.sleep(0.15)
                                elif is_action(ba, bc, "KEY2", "d"):
                                    _show_msg("Delete Photo?", os.path.basename(photos[photo_idx]), kind="err", foot=confirm_foot)
                                    while _running:
                                        ca, _cc = _poll_input()
                                        if is_confirm(ca):
                                            try:
                                                os.remove(photos[photo_idx])
                                            except OSError:
                                                pass
                                            photos.pop(photo_idx)
                                            if not photos:
                                                break
                                            photo_idx = min(photo_idx, len(photos) - 1)
                                            _show_photo(photos[photo_idx], photo_idx, len(photos))
                                            break
                                        if is_back(ca):
                                            _show_photo(photos[photo_idx], photo_idx, len(photos))
                                            break
                                        time.sleep(0.05)
                                    if not photos:
                                        break
                                time.sleep(0.04)
                        elif f.endswith(".h264"):
                            _play_video(f)
                        dirty = True
                    elif is_action(act, ch, "KEY2", "d") and now - last_btn > 0.35 and files:
                        last_btn = now
                        confirm_foot = format_footer_hint(
                            W, H,
                            hat_hint="OK:Yes K3:No",
                            hat_240_hint="OK:Yes  K3:Cancel",
                            cardputer_hint="Enter:Yes  Esc:Cancel",
                            hdmi_hint="Enter: Yes  •  Esc: Cancel",
                        )
                        _show_msg("Delete File?", os.path.basename(files[gallery_sel]), kind="err", foot=confirm_foot)
                        while _running:
                            ca, _cc = _poll_input()
                            if is_confirm(ca):
                                try:
                                    os.remove(files[gallery_sel])
                                except OSError:
                                    pass
                                files = _list_photos() + _list_videos()
                                if gallery_sel >= len(files):
                                    gallery_sel = max(0, len(files) - 1)
                                break
                            if is_back(ca):
                                break
                            time.sleep(0.05)
                        dirty = True
                    if dirty:
                        _draw_gallery(files, gallery_sel)
                        dirty = False

                # ── Settings ──
                elif state == "settings":
                    if is_back(act):
                        state = "menu"
                        dirty = True
                        continue
                    if act == "UP" and now - last_btn > DEBOUNCE:
                        last_btn = now
                        settings_sel = (settings_sel - 1) % 4
                        dirty = True
                    elif act == "DOWN" and now - last_btn > DEBOUNCE:
                        last_btn = now
                        settings_sel = (settings_sel + 1) % 4
                        dirty = True
                    elif is_confirm(act) and now - last_btn > DEBOUNCE:
                        last_btn = now
                        if settings_sel == 0:
                            res_idx = (res_idx + 1) % len(RESOLUTIONS)
                            dirty = True
                        elif settings_sel == 3:
                            state = "menu"
                            dirty = True
                            continue
                    if dirty:
                        _draw_settings(res_idx, settings_sel)
                        dirty = False

                time.sleep(0.04)
    finally:
        _stop_preview()
        if _recording:
            _stop_recording()
        try:
            LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
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
