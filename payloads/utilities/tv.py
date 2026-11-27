#!/usr/bin/env python3
"""
EmberJack Payload -- TV (YouTube channels)
==========================================
A lean-back "TV" that endlessly cycles a programmed lineup of shows,
pulling episodes from YouTube.

Inspired by a classic bash "fake TV" loop: it interleaves your shows with
bumpers (commercials / trailers / music videos), picks a *random* episode
from each show without repeating the last one, and never stops.

Everything is driven by an easy-to-edit JSON config that is created on first
run:  <base_dir>/loot/TV/tv_config.json

  shows   : name -> { "source": <see below>, "max": <how many to pull> }
  lineup  : ordered list of show names to rotate through, forever

A show "source" can be:
  - a YouTube channel URL   "https://www.youtube.com/@SomeChannel/videos"
  - a YouTube playlist URL  "https://www.youtube.com/playlist?list=PL..."
  - a search query          "search:retro tv commercials"  (or just plain text)

Resolved video lists are cached (default 12h) so it stays fresh without
hammering YouTube on every loop.

Controls (channel-select screen):
  UP/DOWN         Pick a channel (or "All channels")
  OK / Enter      Start watching
  +/-             Volume
  KEY3 / Esc      Exit

Controls (while watching):
  RIGHT / N       Next episode (skip)
  LEFT / P        Skip current
  UP/DOWN (+/-)   Volume
  OK / Space      Pause / Resume
  KEY2 / I        Toggle channel OSD banner
  KEY3 / Esc      Stop / back to channel list
"""

import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import numpy as np
from PIL import Image, ImageDraw

import rj_env
import rj_theme
from payloads._audio_helper import (
    get_audio_popen_kwargs,
    get_ffmpeg_audio_args,
    set_playback_volume,
)
from payloads._display_helper import open_framebuffer
from payloads._input_helper import (
    flush_input,
    format_footer_hint,
    is_action,
    is_back,
    is_confirm,
    is_step_down,
    is_step_up,
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

LCD = LCD_1in44.LCD()
LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
W, H = LCD.width, LCD.height
FONTS = rj_theme.load_app_fonts(W, H)
IS_WIDE = bool(FONTS["wide"])
IS_LARGE = bool(FONTS["large"])
IS_MID = (W >= 200 and H >= 200 and W < 300)
IS_SHORT = (H <= 200)
ET = rj_theme.EMBER_THEME

FB_DEVICE = "/dev/fb1" if os.path.exists("/dev/fb1") else "/dev/fb0"
FB_SIZE = W * H * 2

TV_DIR = os.path.join(rj_env.get_base_dir(), "loot", "TV")
CONFIG_FILE = os.path.join(TV_DIR, "tv_config.json")
CACHE_FILE = os.path.join(TV_DIR, "tv_cache.json")
STATE_FILE = os.path.join(TV_DIR, "tv_state.json")

# Stream quality presets (video[+audio] format selectors for yt-dlp)
STREAM_QUALITIES = {
    "144p": "160+139/160/worst",
    "240p": "133+139/133/worst",
    "360p": "134+139/134/worst",
    "480p": "135+139/135/worst",
    "720p": "136+139/136/worst",
}

DEFAULT_CONFIG = {
    "_comment": (
        "EmberJack TV. 'source' = channel URL, playlist URL, or 'search:<query>'. "
        "'lineup' is the rotation order (repeat names to weight them, like commercials "
        "between every show). Delete tv_cache.json to force a refresh."
    ),
    "shows": {
        "Sn0ren": {"source": "https://www.youtube.com/@sn0ren/videos", "max": 40},
        "ValleyTech": {"source": "https://www.youtube.com/@Valleytechsolutions/videos", "max": 40},
        "Sasquach": {"source": "search:talking sasquach", "max": 40},
        "Hacking": {"source": "search:cybersecurity news 2026", "max": 40},
        "Commercials": {"source": "search:retro tv commercials", "max": 40},
        "Trailers": {"source": "search:movie trailers 2026", "max": 40},
        "MusicVideos": {"source": "search:synthwave music video", "max": 40},
    },
    "lineup": [
        "Sn0ren", "Commercials", "Trailers", "MusicVideos",
        "ValleyTech", "Commercials", "Trailers", "Sasquach",
        "Commercials", "Hacking", "Trailers",
    ],
    "quality": "360p",
    "cache_hours": 12,
    "volume": 40,
    "shuffle_lineup": False,
}

_running = True
_vol = DEFAULT_CONFIG["volume"]
_alsa_dev = "default"
_alsa_card = "0"


def _sig(s, f):
    global _running
    _running = False


signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)


# ---------------------------------------------------------------------------
# Display helpers (Native Obsidian Ember)
# ---------------------------------------------------------------------------
def _chrome_heights():
    if IS_MID:
        return 22, 16
    if IS_WIDE and IS_SHORT:
        return 20, 15
    return None, None


def _show_msg(text, sub="", kind="info", foot=""):
    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)
    hh, _ = _chrome_heights()
    rj_theme.draw_app_header(d, W, "EMBERJACK TV" if IS_WIDE else "TV", FONTS, badge="LIVE", hdr_h=hh)
    rj_theme.draw_status_card(d, W, H, text, sub=sub, kind=kind, foot=foot, fonts=FONTS)
    LCD.LCD_ShowImage(img, 0, 0)


def _format_dur(sec):
    if sec <= 0:
        return "?"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _source_kind(source):
    s = str(source or "").strip()
    if s.startswith("search:"):
        return "SEARCH", s[len("search:"):].strip()
    if "playlist" in s or "list=" in s:
        return "PLAYLIST", s
    if s.startswith(("http://", "https://", "www.")):
        return "CHANNEL", s
    return "SEARCH", s


# ---------------------------------------------------------------------------
# Config / cache / state
# ---------------------------------------------------------------------------
def _load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception:
        os.makedirs(TV_DIR, exist_ok=True)
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
        except Exception:
            pass
        return dict(DEFAULT_CONFIG)


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    try:
        os.makedirs(TV_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------
def _detect_alsa():
    global _alsa_dev, _alsa_card
    try:
        r = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=3)
        for line in r.stdout.split("\n"):
            if "card" in line.lower() and ":" in line:
                card_num = line.split(":")[0].replace("card", "").strip()
                if any(k in line.upper() for k in ["ES8388", "ES8389", "ES8390"]):
                    _alsa_dev = f"plughw:{card_num},0"
                    _alsa_card = card_num
                    return
                elif "HDMI" not in line.upper():
                    _alsa_dev = f"plughw:{card_num},0"
                    _alsa_card = card_num
    except Exception:
        pass


def _set_vol(v):
    global _vol
    _vol = max(0, min(63, int(v)))
    set_playback_volume(_vol)


def _vol_pct():
    return int(round(_vol * 100.0 / 63.0))


# ---------------------------------------------------------------------------
# Source resolution (channel / playlist / search -> list of videos), cached
# ---------------------------------------------------------------------------
def _resolve_source(source, max_results, cache, cache_hours):
    """Return a list of {id,title,channel,duration}. Cached by source key."""
    key = source
    entry = cache.get(key)
    now = time.time()
    if entry and (now - entry.get("ts", 0)) < cache_hours * 3600 and entry.get("items"):
        return entry["items"]

    if source.startswith("search:"):
        target = f"ytsearch{max_results}:{source[len('search:'):].strip()}"
    elif source.startswith(("http://", "https://", "www.")):
        target = source
    else:
        target = f"ytsearch{max_results}:{source.strip()}"

    try:
        r = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--no-download",
             "-I", f":{max_results}", "-j", target],
            capture_output=True, text=True, timeout=90)
    except Exception:
        return entry["items"] if entry else []

    items = []
    for line in r.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        vid = data.get("id", "")
        if not vid:
            continue
        items.append({
            "id": vid,
            "title": (data.get("title") or "?")[:80],
            "channel": (data.get("channel") or data.get("uploader") or "")[:40],
            "duration": int(data.get("duration") or 0),
        })

    if items:
        cache[key] = {"ts": now, "items": items}
        _save_json(CACHE_FILE, cache)
        return items
    return entry["items"] if entry else []


def _pick_episode(show_name, items, state):
    """Pick a random video, avoiding the immediately-previous one."""
    if not items:
        return None
    last_id = state.get(show_name)
    pool = [v for v in items if v["id"] != last_id] or items
    choice = random.choice(pool)
    state[show_name] = choice["id"]
    _save_json(STATE_FILE, state)
    return choice


# ---------------------------------------------------------------------------
# Streaming engine (yt-dlp get-url -> ffmpeg -> open_framebuffer / SPI + audio)
# ---------------------------------------------------------------------------
def _read_frame(proc):
    raw = b""
    while len(raw) < FB_SIZE:
        chunk = proc.stdout.read(FB_SIZE - len(raw))
        if not chunk:
            return None
        raw += chunk
    return raw


def _banner_img(show_name, channel, title, idx, total, quality="360p"):
    """Obsidian Ember channel-change broadcast card ('NOW TUNING: <show>')."""
    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)
    hh, fh = _chrome_heights()
    hdr_h = rj_theme.draw_app_header(
        d, W, "EMBERJACK TV" if IS_WIDE else "TV", FONTS,
        badge="ON AIR", right_pill=f"{idx}/{total} • {quality}",
        hdr_h=hh,
    )
    ftr_h = rj_theme.draw_app_footer(
        d, W, H,
        format_footer_hint(
            W, H,
            hat_hint="OK:P R:Skp K3:X",
            hat_240_hint="OK:Pause L/R:Skip U/D:Vol K2:OSD K3:B",
            cardputer_hint="Spc:Pause L/R:Skip +/-:V I:OSD Esc:Menu",
            hdmi_hint="Space:Pause  Left/Right:Skip  Up/Dn:Vol  I:OSD  Esc:Menu",
        ),
        FONTS,
        ftr_h=fh,
    )

    pad = 16 if IS_LARGE else (10 if IS_MID else (5 if IS_WIDE else 4))
    cy0 = hdr_h + pad
    cy1 = H - ftr_h - pad
    avail_h = max(56, cy1 - cy0)
    cw = min(W - pad * 2, 540 if IS_LARGE else W - pad * 2)
    ch = min(avail_h, 190 if IS_LARGE else (160 if IS_MID else avail_h))
    cx0 = (W - cw) // 2
    by0 = cy0 + max(0, (avail_h - ch) // 2)
    bx1 = cx0 + cw
    by1 = by0 + ch

    rj_theme.draw_card(
        d, (cx0, by0, bx1, by1),
        fill=ET["bg_surface"], outline=ET["border_accent"],
        radius=8 if (IS_WIDE or IS_MID) else 4, accent_left=ET["accent"],
        accent_width=4 if IS_LARGE else (3 if IS_MID else 2),
    )

    ix = cx0 + (18 if IS_LARGE else (12 if IS_MID else (10 if IS_WIDE else 6)))
    iw = cw - (32 if IS_LARGE else (22 if IS_MID else (18 if IS_WIDE else 10)))
    iy = by0 + (16 if IS_LARGE else (12 if IS_MID else (7 if IS_WIDE else 6)))

    rj_theme.draw_pill(
        d, ix, iy, f"CH {idx:02d} • NOW PLAYING", FONTS["badge"],
        bg=ET["select_bg"], fg=ET["accent_bright"], outline=ET["border_accent"],
        pad_x=6 if (IS_WIDE or IS_MID) else 3, pad_y=2 if IS_LARGE else 1, radius=3,
    )
    iy += 28 if IS_LARGE else (22 if IS_MID else (17 if IS_WIDE else 14))

    big_font = rj_env.load_font(22 if IS_LARGE else (17 if IS_MID else (14 if IS_WIDE else 11)), bold=True)
    d.text((ix, iy), rj_theme.truncate_text(d, show_name, iw, big_font), font=big_font, fill=ET["text_bright"])
    iy += 32 if IS_LARGE else (24 if IS_MID else (18 if IS_WIDE else 15))

    lines = rj_theme.wrap_text(d, title or "Loading stream...", iw, FONTS["body"])
    max_t_lines = 3 if (IS_LARGE or IS_MID) else 2
    lh = 18 if IS_LARGE else (15 if IS_MID else (12 if IS_WIDE else 10))
    ch_reserve = 26 if IS_LARGE else (20 if IS_MID else (16 if IS_WIDE else 12))
    for ln in lines[:max_t_lines]:
        if iy + lh > by1 - ch_reserve:
            break
        d.text((ix, iy), ln, font=FONTS["body"], fill=ET["text_primary"])
        iy += lh

    if channel and iy < by1 - (18 if IS_LARGE else (16 if IS_MID else 12)):
        d.text(
            (ix, by1 - (24 if IS_LARGE else (18 if IS_MID else (14 if IS_WIDE else 11)))),
            rj_theme.truncate_text(d, f"Channel: {channel}", iw, FONTS["sub"]),
            font=FONTS["sub"], fill=ET["text_muted"],
        )
    return img


def _build_osd_strip(show_name, title, paused, quality):
    """Build translucent Obsidian Ember playback OSD strip."""
    oh = 34 if IS_LARGE else (24 if IS_MID else (22 if IS_WIDE else 16))
    osd = Image.new("RGBA", (W, oh), (14, 14, 16, 210))
    d = ImageDraw.Draw(osd)
    d.line([(0, 0), (W - 1, 0)], fill=(184, 94, 36, 235), width=1)
    d.rectangle((0, 0, 3 if (IS_WIDE or IS_MID) else 2, oh - 1), fill=(224, 122, 47, 255))

    pad = 10 if IS_LARGE else (6 if (IS_WIDE or IS_MID) else 3)
    state_lbl = "PAUSED" if paused else "LIVE"
    st_bg = (43, 33, 18, 240) if paused else (21, 38, 28, 240)
    st_fg = (230, 168, 69, 255) if paused else (94, 184, 122, 255)
    sb = rj_theme.measure_text(FONTS["badge"], state_lbl, d)
    sw = (sb[2] - sb[0]) + (10 if (IS_WIDE or IS_MID) else 6)
    sh = (sb[3] - sb[1]) + 4
    sy = max(2, (oh - sh) // 2)
    d.rounded_rectangle((pad, sy, pad + sw, sy + sh), radius=3, fill=st_bg, outline=st_fg)
    d.text((pad + sw // 2, sy + sh // 2), state_lbl, font=FONTS["badge"], fill=st_fg, anchor="mm")

    rx = W - pad
    vol_lbl = f"VOL {_vol_pct()}%"
    vb = rj_theme.measure_text(FONTS["badge"], vol_lbl, d)
    vw = (vb[2] - vb[0]) + (10 if (IS_WIDE or IS_MID) else 6)
    d.rounded_rectangle((rx - vw, sy, rx, sy + sh), radius=3, fill=(45, 29, 18, 235), outline=(184, 94, 36, 235))
    d.text((rx - vw // 2, sy + sh // 2), vol_lbl, font=FONTS["badge"], fill=(242, 153, 74, 255), anchor="mm")
    rx -= vw + (6 if (IS_WIDE or IS_MID) else 3)

    if IS_LARGE:
        qb = rj_theme.measure_text(FONTS["badge"], quality, d)
        qw = (qb[2] - qb[0]) + 10
        d.rounded_rectangle((rx - qw, sy, rx, sy + sh), radius=3, fill=(31, 30, 34, 235), outline=(43, 40, 38, 235))
        d.text((rx - qw // 2, sy + sh // 2), quality, font=FONTS["badge"], fill=(230, 223, 215, 255), anchor="mm")
        rx -= qw + 8

    tx = pad + sw + (8 if (IS_WIDE or IS_MID) else 4)
    avail = max(20, rx - tx - 4)
    label = f"{show_name}  •  {title}" if ((IS_WIDE or IS_MID) and title) else show_name
    txt = rj_theme.truncate_text(d, label, avail, FONTS["body_bold"] if IS_LARGE else FONTS["sub"])
    d.text((tx, oh // 2), txt, font=FONTS["body_bold"] if IS_LARGE else FONTS["sub"], fill=(245, 239, 232, 255), anchor="lm")
    return osd


def _composite_osd_rgb565(raw, osd_img):
    """Alpha-composite an RGBA OSD strip onto the bottom of an RGB565 frame."""
    arr = np.frombuffer(raw, dtype="<u2").reshape(H, W).copy()
    oh = osd_img.height
    oy = H - oh
    osd_rgba = np.asarray(osd_img)
    a = osd_rgba[:, :, 3].astype(np.float32) / 255.0
    inv = 1.0 - a
    vid_r = ((arr[oy:, :] >> 11) & 0x1F).astype(np.float32) * 8.0
    vid_g = ((arr[oy:, :] >> 5) & 0x3F).astype(np.float32) * 4.0
    vid_b = (arr[oy:, :] & 0x1F).astype(np.float32) * 8.0
    r = (osd_rgba[:, :, 0].astype(np.float32) * a + vid_r * inv).astype(np.uint16) >> 3
    g = (osd_rgba[:, :, 1].astype(np.float32) * a + vid_g * inv).astype(np.uint16) >> 2
    b = (osd_rgba[:, :, 2].astype(np.float32) * a + vid_b * inv).astype(np.uint16) >> 3
    arr[oy:, :] = (r << 11) | (g << 5) | b
    return arr.tobytes()


def _rgb565_to_pil(raw):
    arr = np.frombuffer(raw, dtype="<u2").reshape(H, W)
    rr = ((arr >> 11) & 0x1F) << 3
    gg = ((arr >> 5) & 0x3F) << 2
    bb = (arr & 0x1F) << 3
    rgb = np.stack([rr.astype(np.uint8), gg.astype(np.uint8), bb.astype(np.uint8)], axis=-1)
    return Image.fromarray(rgb, "RGB")


def _play_video(video_id, title, show_name, channel, idx, total, quality, show_banner):
    """Stream one video full-screen. Returns 'next' / 'skip' / 'stop'."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    quality_fmt = STREAM_QUALITIES.get(quality, STREAM_QUALITIES["360p"])

    LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    if show_banner:
        LCD.LCD_ShowImage(_banner_img(show_name, channel, title, idx, total, quality), 0, 0)
        # Allow skipping during banner wait
        t_end = time.monotonic() + 1.4
        while _running and time.monotonic() < t_end:
            act, ch = poll_ui_input(PINS, GPIO)
            kl = ch.lower() if isinstance(ch, str) else ""
            if is_back(act):
                return "stop"
            if act == "RIGHT" or kl == "n":
                return "next"
            time.sleep(0.03)
    else:
        _show_msg("Tuning in...", show_name, kind="info")

    try:
        r = subprocess.run(["yt-dlp", "-f", quality_fmt, "--get-url", url],
                           capture_output=True, text=True, timeout=40)
        urls = r.stdout.strip().split("\n")
        video_url = urls[0] if urls and urls[0] else ""
        audio_url = urls[1] if len(urls) > 1 else ""
    except Exception:
        _show_msg("Stream error", "Skipping to next show...", kind="warn")
        time.sleep(1.0)
        return "next"

    if not video_url:
        _show_msg("No stream available", "Skipping to next show...", kind="warn")
        time.sleep(1.0)
        return "next"

    target_fps = 24 if IS_WIDE else 8
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "quiet", "-re", "-i", video_url]
    if audio_url:
        cmd += ["-i", audio_url]
    cmd += [
        "-map", "0:v:0",
        "-vf", (
            f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={target_fps}"
        ),
        "-pix_fmt", "rgb565le", "-f", "rawvideo", "pipe:1",
    ]
    if audio_url:
        try:
            aud_args = get_ffmpeg_audio_args()
        except Exception:
            aud_args = ["-f", "alsa", _alsa_dev]
        cmd += ["-map", "1:a:0", "-af", "aresample=async=1", "-ac", "2", "-ar", "44100"] + aud_args

    try:
        popen_kw = get_audio_popen_kwargs(cmd) if audio_url else {}
    except Exception:
        popen_kw = {}

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        bufsize=FB_SIZE * 16, **popen_kw,
    )
    try:
        import fcntl
        fcntl.fcntl(proc.stdout, 1031, FB_SIZE * 32)  # F_SETPIPE_SZ
    except Exception:
        pass

    time.sleep(0.5)
    if proc.poll() is not None:
        _show_msg("ffmpeg stream error", "Skipping to next show...", kind="warn")
        time.sleep(1.0)
        return "next"

    # Use open_framebuffer on HDMI (safe _HDMIFBProxy, never collides with /dev/fb0!)
    # and CardputerZero; SPI HATs render directly via LCD_ShowImage.
    disp_type = getattr(LCD, "display_type", "")
    use_fb = disp_type in ("HDMI", "CARDPUTER_320")
    fb_fd = fb_map = None
    if use_fb:
        fb_fd, fb_map = open_framebuffer(LCD, FB_DEVICE, W, H)

    _set_vol(_vol)
    paused = False
    banner_until = time.time() + 3.0
    osd_img = _build_osd_strip(show_name, title, paused, quality)
    last_frame = None
    result = "next"

    try:
        while _running:
            act, ch = poll_ui_input(PINS, GPIO)
            now = time.time()
            kl = ch.lower() if isinstance(ch, str) else ""

            if is_back(act):
                result = "stop"
                break
            elif act == "RIGHT" or kl == "n":
                result = "next"
                break
            elif act == "LEFT" or kl == "p":
                result = "skip"
                break
            elif act == "UP" or is_step_up(ch):
                _set_vol(_vol + 5)
                banner_until = now + 2.2
                osd_img = _build_osd_strip(show_name, title, paused, quality)
            elif act == "DOWN" or is_step_down(ch):
                _set_vol(_vol - 5)
                banner_until = now + 2.2
                osd_img = _build_osd_strip(show_name, title, paused, quality)
            elif is_action(act, ch, "KEY2", "i"):
                if now < banner_until:
                    banner_until = 0.0
                else:
                    banner_until = now + 3.5
                    osd_img = _build_osd_strip(show_name, title, paused, quality)
            elif act == "OK" or ch == " ":
                paused = not paused
                banner_until = now + 3.0
                osd_img = _build_osd_strip(show_name, title, paused, quality)
                if paused:
                    try:
                        proc.send_signal(signal.SIGSTOP)
                    except Exception:
                        pass
                    if last_frame is not None:
                        out = _composite_osd_rgb565(last_frame, osd_img)
                        if use_fb and fb_map is not None:
                            fb_map.seek(0)
                            fb_map.write(out)
                        else:
                            LCD.LCD_ShowImage(_rgb565_to_pil(out), 0, 0)
                    else:
                        _show_msg(
                            "PAUSED", show_name, kind="warn",
                            foot=format_footer_hint(
                                W, H,
                                hat_hint="OK:Resume",
                                hat_240_hint="OK: Resume",
                                cardputer_hint="Space: Resume",
                                hdmi_hint="Space: Resume",
                            ),
                        )
                else:
                    try:
                        proc.send_signal(signal.SIGCONT)
                    except Exception:
                        pass
                time.sleep(0.15)
                continue

            if paused:
                time.sleep(0.04)
                continue

            raw = _read_frame(proc)
            if raw is None:
                result = "next"
                break

            last_frame = raw
            out_raw = _composite_osd_rgb565(raw, osd_img) if now < banner_until else raw

            if use_fb and fb_map is not None:
                fb_map.seek(0)
                fb_map.write(out_raw)
            else:
                LCD.LCD_ShowImage(_rgb565_to_pil(out_raw), 0, 0)
    finally:
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
        subprocess.run(["pkill", "-9", "ffmpeg"], capture_output=True)
        time.sleep(0.15)
        LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)

    return result


# ---------------------------------------------------------------------------
# Dependency / connectivity checks
# ---------------------------------------------------------------------------
def _check_internet():
    try:
        return subprocess.run(["ping", "-c", "1", "-W", "2", "8.8.8.8"],
                              capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def _check_deps():
    return shutil.which("ffmpeg") is not None and shutil.which("yt-dlp") is not None


# ---------------------------------------------------------------------------
# Channel-select screen (Native Obsidian Ember Split-Pane / Adaptive)
# ---------------------------------------------------------------------------
def _draw_channel_menu(cfg, items, sel, cache):
    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)
    hh, fh = _chrome_heights()

    quality = str(cfg.get("quality", "360p"))
    shows = cfg.get("shows", {})
    lineup = cfg.get("lineup", [])

    hdr_h = rj_theme.draw_app_header(
        d, W, "EMBERJACK TV" if IS_WIDE else "TV", FONTS,
        badge=f"{len(shows)} CH",
        right_text=f"VOL {_vol_pct()}%" if IS_LARGE else "",
        right_pill=quality,
        hdr_h=hh,
    )
    ftr_h = rj_theme.draw_app_footer(
        d, W, H,
        format_footer_hint(
            W, H,
            hat_hint="OK:Watch K3:X",
            hat_240_hint="U/D:Ch OK:Watch K3:Exit",
            cardputer_hint="U/D:Ch Ent:Watch +/-:V Esc:Exit",
            hdmi_hint="Arrows:Channel  Enter:Watch  +/-:Volume  Esc:Exit",
        ),
        FONTS,
        right_text=f"{sel + 1}/{len(items)}" if IS_WIDE else "",
        ftr_h=fh,
    )

    pad = 8 if IS_LARGE else (6 if IS_MID else (4 if IS_WIDE else 3))
    body_y0 = hdr_h + pad
    body_y1 = H - ftr_h - pad
    split = W >= 480

    list_x0 = pad
    list_x1 = int(W * 0.56) - 4 if split else (W - pad - (6 if (IS_WIDE or IS_MID) else 4))
    row_h = 34 if IS_LARGE else (28 if IS_MID else (22 if IS_WIDE else 18))
    vis = max(1, (body_y1 - body_y0) // row_h)
    st = max(0, min(sel - vis + 1, len(items) - vis)) if len(items) > vis else 0

    for slot in range(vis):
        idx = st + slot
        if idx >= len(items):
            break
        ry = body_y0 + slot * row_h
        ry1 = min(body_y1, ry + row_h - (4 if (IS_WIDE or IS_MID) else 2))
        on = (idx == sel)
        label = items[idx]

        rj_theme.draw_card(
            d, (list_x0, ry, list_x1, ry1),
            fill=ET["select_bg"] if on else ET["bg_surface"],
            outline=ET["border_accent"] if on else ET["border_subtle"],
            radius=6 if (IS_WIDE or IS_MID) else 3,
            accent_left=ET["accent"] if on else None,
            accent_width=3 if (IS_WIDE or IS_MID) else 2,
        )

        # Channel number badge + title
        tx = list_x0 + (10 if (IS_WIDE or IS_MID) else 5)
        right_x = list_x1 - (8 if (IS_WIDE or IS_MID) else 4)
        if idx == 0:
            tag = "AUTO"
        else:
            meta = shows.get(label, {})
            tag, _ = _source_kind(meta.get("source", ""))
        if IS_WIDE or IS_MID:
            tb = rj_theme.measure_text(FONTS["badge"], tag, d)
            tw = (tb[2] - tb[0]) + 8
            rj_theme.draw_pill(
                d, right_x - tw, ry + max(2, ((ry1 - ry) - (tb[3] - tb[1]) - 4) // 2),
                tag, FONTS["badge"],
                bg=ET["bg_well"], fg=ET["accent_bright"] if on else ET["text_muted"],
                outline=ET["border_accent"] if on else ET["border_subtle"],
                pad_x=4, pad_y=1, radius=3,
            )
            right_x -= tw + 6

        avail_w = max(24, right_x - tx)
        title_txt = rj_theme.truncate_text(d, label, avail_w, FONTS["body_bold"] if on else FONTS["body"])
        d.text(
            (tx, (ry + ry1) // 2),
            title_txt,
            font=FONTS["body_bold"] if on else FONTS["body"],
            fill=ET["accent_bright"] if on else ET["text_primary"],
            anchor="lm",
        )

    # Scrollbar
    sb_x = list_x1 + 3
    rj_theme.draw_scrollbar(d, sb_x, body_y0, body_y1, st, vis, len(items), width=3 if (IS_WIDE or IS_MID) else 2)

    # Right Details Pane on Widescreen (>= 480px)
    if split:
        px0 = int(W * 0.56) + 6
        px1 = W - pad
        rj_theme.draw_card(
            d, (px0, body_y0, px1, body_y1),
            fill=ET["bg_surface"], outline=ET["border_subtle"],
            radius=8, accent_left=ET["accent_dim"], accent_width=3,
        )
        ix = px0 + 14
        iw = (px1 - px0) - 24
        iy = body_y0 + 14

        if sel == 0:
            rj_theme.draw_pill(
                d, ix, iy, "PROGRAMMED LINEUP", FONTS["badge"],
                bg=ET["select_bg"], fg=ET["accent_bright"], outline=ET["border_accent"],
                pad_x=6, pad_y=2, radius=3,
            )
            iy += 28
            d.text((ix, iy), "All Channels (TV)", font=FONTS["title"], fill=ET["text_bright"])
            iy += 24
            desc = (
                f"Rotates through {len(lineup)} lineup slots across {len(shows)} shows "
                f"with non-repeating random episodes."
            )
            for ln in rj_theme.wrap_text(d, desc, iw, FONTS["sub"])[:3]:
                d.text((ix, iy), ln, font=FONTS["sub"], fill=ET["text_muted"])
                iy += 15
            iy += 8
            d.text((ix, iy), "UP NEXT ROTATION:", font=FONTS["badge"], fill=ET["accent"])
            iy += 16
            rot_str = " -> ".join(lineup[:6]) if lineup else "None"
            for ln in rj_theme.wrap_text(d, rot_str, iw, FONTS["sub"])[:3]:
                d.text((ix, iy), ln, font=FONTS["sub"], fill=ET["text_primary"])
                iy += 15
        else:
            sname = items[sel]
            meta = shows.get(sname, {})
            src = str(meta.get("source", ""))
            skind, squery = _source_kind(src)
            cached_items = cache.get(src, {}).get("items", []) if isinstance(cache, dict) else []

            rj_theme.draw_pill(
                d, ix, iy, f"SINGLE SHOW • {skind}", FONTS["badge"],
                bg=ET["select_bg"], fg=ET["accent_bright"], outline=ET["border_accent"],
                pad_x=6, pad_y=2, radius=3,
            )
            iy += 28
            d.text((ix, iy), rj_theme.truncate_text(d, sname, iw, FONTS["title"]), font=FONTS["title"], fill=ET["text_bright"])
            iy += 24
            for ln in rj_theme.wrap_text(d, squery or "No source configured", iw, FONTS["sub"])[:3]:
                d.text((ix, iy), ln, font=FONTS["sub"], fill=ET["text_muted"])
                iy += 15
            iy += 10
            stats = [
                ("Max Pool", f"{meta.get('max', 40)} videos"),
                ("Cached", f"{len(cached_items)} ready" if cached_items else "Not cached yet"),
                ("Quality", quality),
            ]
            for k_lbl, v_lbl in stats:
                if iy + 18 > body_y1 - 46:
                    break
                d.text((ix, iy), k_lbl, font=FONTS["sub"], fill=ET["text_muted"])
                vb = rj_theme.measure_text(FONTS["body_bold"], v_lbl, d)
                d.text((px1 - 12 - (vb[2] - vb[0]), iy - 1), v_lbl, font=FONTS["body_bold"], fill=ET["text_primary"])
                iy += 20

        # Volume meter anchored at bottom of right pane
        vy = body_y1 - 36
        d.line([(ix, vy - 8), (px1 - 12, vy - 8)], fill=ET["border_subtle"], width=1)
        d.text((ix, vy), f"AUDIO VOLUME  {_vol_pct()}%", font=FONTS["badge"], fill=ET["text_muted"])
        rj_theme.draw_progress_bar(
            d, (ix, vy + 15, px1 - 12, vy + 23),
            _vol / 63.0, fill=ET["accent"], track=ET["bg_well"], outline=ET["border_subtle"], radius=3,
        )

    LCD.LCD_ShowImage(img, 0, 0)


def _channel_menu(cfg):
    """Return 'ALL', a show name, or None (exit)."""
    shows = list(cfg.get("shows", {}).keys())
    items = ["All channels (TV)"] + shows
    cache = _load_json(CACHE_FILE, {})
    sel = 0
    dirty = True

    while _running:
        if dirty:
            _draw_channel_menu(cfg, items, sel, cache)
            dirty = False

        act, ch = poll_ui_input(PINS, GPIO)
        if act is None and ch is None:
            time.sleep(0.03)
            continue

        if is_back(act):
            return None
        elif act == "UP":
            sel = (sel - 1) % len(items)
            dirty = True
        elif act == "DOWN":
            sel = (sel + 1) % len(items)
            dirty = True
        elif is_step_up(ch):
            _set_vol(_vol + 5)
            dirty = True
        elif is_step_down(ch):
            _set_vol(_vol - 5)
            dirty = True
        elif is_confirm(act):
            return "ALL" if sel == 0 else items[sel]
    return None


# ---------------------------------------------------------------------------
# Main TV loop
# ---------------------------------------------------------------------------
def _watch(cfg, selection):
    quality = cfg.get("quality", "360p")
    cache_hours = float(cfg.get("cache_hours", 12))
    shows = cfg.get("shows", {})

    if selection == "ALL":
        lineup = [s for s in cfg.get("lineup", []) if s in shows]
        if not lineup:
            lineup = list(shows.keys())
        if cfg.get("shuffle_lineup"):
            random.shuffle(lineup)
    else:
        lineup = [selection]

    if not lineup:
        _show_msg("No shows configured", "Edit tv_config.json", kind="warn")
        time.sleep(2)
        return

    cache = _load_json(CACHE_FILE, {})
    state = _load_json(STATE_FILE, {})
    show_banner = True
    pos = 0
    total = len(lineup)

    while _running:
        show_name = lineup[pos % total]
        meta = shows.get(show_name, {})
        _show_msg("Loading lineup...", show_name, kind="info")
        items = _resolve_source(meta.get("source", ""), int(meta.get("max", 40)),
                                cache, cache_hours)
        ep = _pick_episode(show_name, items, state)
        if ep is None:
            _show_msg(show_name, "No videos found, skipping", kind="warn")
            time.sleep(1.2)
            pos += 1
            continue

        result = _play_video(
            ep["id"], ep["title"], show_name, ep.get("channel", ""),
            pos % total + 1, total, quality, show_banner,
        )
        if result == "stop":
            return
        pos += 1


def main():
    _detect_alsa()
    cfg = _load_config()
    global _vol
    _vol = int(cfg.get("volume", 40))

    _show_msg("EmberJack TV", "Checking network & codecs...", kind="info")
    if not _check_internet():
        _show_msg("No Internet", "Connect WiFi or Ethernet", kind="err")
        time.sleep(2.5)
        GPIO.cleanup()
        return 1
    if not _check_deps():
        _show_msg("Missing Dependencies", "Install ffmpeg & yt-dlp", kind="err")
        time.sleep(2.5)
        GPIO.cleanup()
        return 1

    try:
        with text_input_mode():
            flush_input()
            while _running:
                selection = _channel_menu(cfg)
                if selection is None:
                    break
                _watch(cfg, selection)
                cfg = _load_config()
    finally:
        subprocess.run(["pkill", "-9", "ffmpeg"], capture_output=True)
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
