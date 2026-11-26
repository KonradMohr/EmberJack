#!/usr/bin/env python3
"""
RaspyJack Payload -- Obsidian Ember YouTube & Media Player
==========================================================
Responsive, eye-friendly YouTube video & music player for HDMI monitors,
CardputerZero, and SPI LCDs with Bluetooth / PipeWire / PulseAudio / ALSA
audio routing, live OSD overlay, seeking, quality switching, thumbnails,
URL/playlist support, and offline Downloads playback.

Controls (100% Keyboard + Button + WebUI):
  Home Menu:
    UP / DOWN / LEFT / RIGHT  Navigate
    OK / Enter                Open selected menu item
    KEY1 / S                  Search YouTube / URL / Playlist
    M                         Search Music (Audio Only)
    KEY2 / O                  Quality & Audio Settings
    KEY3 / Esc                Exit

  Lists / Results:
    UP / DOWN / PgUp / PgDn   Navigate
    OK / Enter                Play Video (or Audio in Music Mode)
    KEY1 / O                  Item Actions menu (Audio/DL/Later/Playlist/Info/Filter)
    M                         Play Audio Only
    D                         Download MP4 or MP3 (or Entire Playlist)
    L / W / A                 Like / Watch Later / Add to Playlist
    F / U / V                 Filter or Follow / Open Channel / Video Info
    KEY2 / S                  Quality & Audio Settings
    KEY3 / Esc                Back

  Video Playback:
    OK / Space                Pause / Resume
    LEFT / RIGHT              Seek -15s / +15s
    UP / DOWN (+/-)           Volume Up / Down (Bluetooth/PipeWire/ALSA)
    KEY1 / O                  Playback Actions menu (Quality/Speed/CC/DL/Like/Next/Settings)
    KEY2 / I                  Toggle OSD HUD overlay
    , / .                     Adjust A/V Sync (-50ms / +50ms live)
    Z / X                     Decrease / Increase Speed (0.5x - 2.0x)
    N / B                     Next / Previous track in playlist
    M / R / T / C / K         Mute / Loop / Captions / Chapters / Screenshot
    S / L / W / A / D / V / F Quality / Like / Later / Playlist / Download / Info / Follow
    KEY3 / Esc                Stop playback

  Music (Audio-Only) Playback:
    OK / Space                Pause / Resume
    LEFT / RIGHT (B / N)      Previous / Next track
    , / .                     Seek -15s / +15s
    UP / DOWN (+/-)           Volume Up / Down
    KEY1 / O                  Music Actions menu (Shuffle/Loop/Mute/Like/DL/Settings)
    KEY2 / S                  Audio & Settings
    M / H / R / L / D         Mute / Shuffle / Loop / Like / Download MP3
    KEY3 / Esc                Stop playback
"""

import collections
import errno
import fcntl
import http.server
import io
import json
import math
import mmap
import os
import queue
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

from PIL import Image, ImageDraw

import rj_env
import rj_theme
from payloads._input_helper import (
    format_footer_hint,
    is_action,
    is_back,
    is_confirm,
    is_step_down,
    is_step_up,
)

# ---------------------------------------------------------------------------
# Paths, Constants & Format Selectors
# ---------------------------------------------------------------------------

def get_youtube_data_dir() -> str:
    return os.path.join(rj_env.get_base_dir(), "loot", "YouTube")


def get_downloads_dir() -> str:
    return os.path.join(get_youtube_data_dir(), "Downloads")


def get_thumb_cache_dir() -> str:
    return os.path.join(get_youtube_data_dir(), "thumbs")


LONG_VIDEO_THRESHOLD_SEC = 900  # >15 min streams via O(1)-RAM Range proxy instead of full /dev/shm remux
MIN_PRELOAD_FREE_BYTES = 300 * 1024 * 1024  # 300 MB free required before starting a RAM/disk-cached .mkv preload
MIN_SHM_SAFETY_FREE_BYTES = 128 * 1024 * 1024  # Stop preload early if free space drops below 128 MB


def _ensure_writable_cache_dir(path: str) -> Optional[str]:
    """Create *path* if needed and return it only if actually writable by this UID."""
    try:
        os.makedirs(path, exist_ok=True)
        try:
            os.chmod(path, 0o777)
        except OSError:
            pass
        if os.path.isdir(path) and os.access(path, os.W_OK | os.X_OK):
            return path
    except OSError:
        pass
    # Fall back to a per-UID sibling if the shared folder is owned by root (e.g. after systemd run)
    uid_path = f"{path}_{os.getuid()}"
    try:
        os.makedirs(uid_path, exist_ok=True)
        if os.path.isdir(uid_path) and os.access(uid_path, os.W_OK | os.X_OK):
            return uid_path
    except OSError:
        pass
    return None


def get_preload_cache_dir() -> str:
    """Return a writable preload cache dir with >=300 MB free (/dev/shm -> /tmp -> disk)."""
    override = os.environ.get("RJ_YT_CACHE_DIR", "").strip()
    if override:
        ok_ov = _ensure_writable_cache_dir(override)
        if ok_ov:
            return ok_ov

    candidates: List[Tuple[str, str]] = [
        ("/dev/shm", os.path.join("/dev/shm", "rj_yt_preload")),
        (tempfile.gettempdir(), os.path.join(tempfile.gettempdir(), "rj_yt_preload")),
        (get_youtube_data_dir(), os.path.join(get_youtube_data_dir(), ".preload_cache")),
    ]
    best_fallback = ""
    best_free = -1
    for root_dir, target_dir in candidates:
        if not os.path.isdir(root_dir):
            try:
                os.makedirs(root_dir, exist_ok=True)
            except OSError:
                continue
        try:
            free_b = int(shutil.disk_usage(root_dir).free)
        except OSError:
            continue
        writable = _ensure_writable_cache_dir(target_dir)
        if not writable:
            continue
        if free_b >= MIN_PRELOAD_FREE_BYTES:
            return writable
        if free_b > best_free:
            best_free = free_b
            best_fallback = writable

    if best_fallback:
        return best_fallback
    d = os.path.join(tempfile.gettempdir(), f"rj_yt_preload_{os.getuid()}")
    os.makedirs(d, exist_ok=True)
    return d


def prune_preload_cache(
    keep_path: str = "",
    max_bytes: int = 180 * 1024 * 1024,
    max_files: int = 2,
    cache_dir: Optional[str] = None,
) -> None:
    """Keep preload cache bounded so /dev/shm never fills up on Raspberry Pi."""
    cdir = cache_dir or get_preload_cache_dir()
    if not os.path.isdir(cdir):
        return
    now = time.time()
    mkvs: List[Tuple[float, int, str]] = []
    try:
        for fn in os.listdir(cdir):
            full = os.path.join(cdir, fn)
            if fn.startswith("fifo_") and os.path.isdir(full):
                try:
                    if (now - os.path.getmtime(full)) > 600:
                        shutil.rmtree(full, ignore_errors=True)
                except OSError:
                    pass
                continue
            if not fn.endswith(".mkv") or not os.path.isfile(full):
                continue
            try:
                st = os.stat(full)
                mkvs.append((st.st_mtime, st.st_size, full))
            except OSError:
                continue
    except OSError:
        return

    # Sort oldest first
    mkvs.sort(key=lambda t: t[0])
    total_bytes = sum(sz for _, sz, _ in mkvs)
    count = len(mkvs)
    keep_norm = os.path.abspath(keep_path) if keep_path else ""
    for _mt, sz, path in mkvs:
        if count <= max_files and total_bytes <= max_bytes:
            break
        if keep_norm and os.path.abspath(path) == keep_norm:
            continue
        try:
            os.unlink(path)
            total_bytes -= sz
            count -= 1
        except OSError:
            pass


def clear_preload_cache(cache_dir: Optional[str] = None) -> None:
    """Remove all temporary preloaded stream files on player exit."""
    if cache_dir:
        dirs = [cache_dir]
    else:
        uid = os.getuid()
        dirs = [
            get_preload_cache_dir(),
            os.path.join("/dev/shm", "rj_yt_preload"),
            os.path.join("/dev/shm", f"rj_yt_preload_{uid}"),
            os.path.join(tempfile.gettempdir(), "rj_yt_preload"),
            os.path.join(tempfile.gettempdir(), f"rj_yt_preload_{uid}"),
            os.path.join(get_youtube_data_dir(), ".preload_cache"),
        ]
    seen = set()
    for cdir in dirs:
        if not cdir or cdir in seen or not os.path.isdir(cdir):
            continue
        seen.add(cdir)
        try:
            for fn in os.listdir(cdir):
                full = os.path.join(cdir, fn)
                try:
                    if os.path.isdir(full):
                        shutil.rmtree(full, ignore_errors=True)
                    elif fn.endswith(".mkv") or fn.endswith(".pipe"):
                        os.unlink(full)
                except OSError:
                    pass
        except OSError:
            pass


PRELOAD_OPTIONS: List[Tuple[str, int]] = [
    ("Off (Direct Stream)", 0),
    ("3s Buffer (Fast Start)", 3),
    ("5s Buffer (Recommended)", 5),
    ("10s Buffer (Anti-Stutter)", 10),
    ("Full Video (Zero Lag)", -1),
]

PLAYER_ENGINES: List[Tuple[str, str]] = [
    ("Built-in (Preloaded)", "builtin"),
    ("mpv Hardware (GPU/DRM)", "mpv"),
]

_EST_BYTES_PER_SEC: Dict[str, int] = {
    "144p": 35_000,
    "240p": 65_000,
    "360p": 125_000,
    "480p": 210_000,
    "720p": 380_000,
    "1080p": 700_000,
}

_YT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


HISTORY_MAX = 25

# Prefer fast-decoding H.264 (avc1 <=30fps) so Raspberry Pi software decode stays ahead of real-time
# (avoids AV1/60fps VP9 CPU spikes and multi-second video lag), falling back cleanly to VP9/AV1/best.
STREAM_QUALITIES: List[Tuple[str, str]] = [
    ("144p", "bestvideo[height<=144][vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[height<=144]+bestaudio/best[height<=144]/worst"),
    ("240p", "bestvideo[height<=240][vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[height<=240]+bestaudio/best[height<=240]/worst"),
    ("360p", "best[height<=360][ext=mp4]/bestvideo[height<=360][vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[height<=360]+bestaudio/best[height<=360]/best"),
    ("480p", "bestvideo[height<=480][vcodec^=avc1][fps<=30]+bestaudio[acodec^=mp4a]/bestvideo[height<=480][vcodec^=avc1]+bestaudio/bestvideo[height<=480]+bestaudio/best[height<=480]/best"),
    ("720p", "bestvideo[height<=720][vcodec^=avc1][fps<=30]+bestaudio[acodec^=mp4a]/bestvideo[height<=720][vcodec^=avc1]+bestaudio/best[height<=720][ext=mp4]/bestvideo[height<=720]+bestaudio/best[height<=720]/best"),
    ("1080p", "bestvideo[height<=1080][vcodec^=avc1][fps<=30]+bestaudio[acodec^=mp4a]/bestvideo[height<=1080][vcodec^=avc1]+bestaudio/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"),
]

MP4_QUALITIES: List[Tuple[str, str]] = [
    ("Best MP4", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"),
    ("720p MP4", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best"),
    ("480p MP4", "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]/best"),
    ("360p MP4", "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]/best"),
]

MP3_QUALITIES: List[Tuple[str, str]] = [
    ("320 kbps", "320"),
    ("192 kbps", "192"),
    ("128 kbps", "128"),
    ("96 kbps", "96"),
]

DEFAULT_PRESETS: List[str] = [
    "lofi hip hop radio",
    "talking sasquach",
    "sn0ren",
    "cybersecurity news",
    "linux terminal tips",
    "CTF writeup",
    "synthwave coding mix",
    "raspberry pi projects",
]

HOME_MENU_ITEMS: List[Tuple[str, str, str]] = [
    ("Search / URL", "search", "Search YouTube, filters, URL or ID"),
    ("Music Mode", "music", "Audio-only streaming & Bluetooth"),
    ("Subscriptions", "subs", "Followed channels & latest uploads"),
    ("Playlists & Later", "playlists", "Watch Later, saved & imported lists"),
    ("Quick Search", "presets", "Saved favorite search presets"),
    ("Liked Videos", "liked", "Your bookmarked videos & tracks"),
    ("Downloads", "downloads", "Offline MP4, MP3 & playlist folders"),
    ("Settings & Audio", "settings", "Quality, speed, captions, BT & audio"),
]

SPEED_OPTIONS: Tuple[float, ...] = (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)
WATCH_LATER_ID = "__watch_later__"
SEARCH_SORT_OPTIONS: Tuple[Tuple[str, str], ...] = (
    ("Relevance (Default)", "relevance"),
    ("Most Viewed", "views"),
    ("Shortest First", "shortest"),
    ("Longest First", "longest"),
)
SEARCH_DUR_OPTIONS: Tuple[Tuple[str, str], ...] = (
    ("Any Duration", "any"),
    ("Short (< 4 min)", "short"),
    ("Medium (4 - 20 min)", "medium"),
    ("Long (> 20 min)", "long"),
)

_VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".avi")
_AUDIO_EXTS = (".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac")


# ---------------------------------------------------------------------------
# Pure Helpers (Formatting, URL Parsing, Persistence, Fonts)
# ---------------------------------------------------------------------------

def format_duration(sec: int) -> str:
    sec = int(sec or 0)
    if sec <= 0:
        return "LIVE" if sec < 0 else "--:--"
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_views(views) -> str:
    try:
        n = int(views or 0)
    except Exception:
        return ""
    if n <= 0:
        return ""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B views"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M views"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K views"
    return f"{n} views"


def parse_youtube_target(text: str) -> Tuple[str, str]:
    """Classify user input into ('playlist', url), ('video', id), or ('search', query)."""
    s = (text or "").strip()
    if not s:
        return "search", ""
    # Playlist URL (explicit /playlist or list= without v=)
    m_list = re.search(r"[?&]list=([A-Za-z0-9_-]{10,})", s)
    if m_list and ("/playlist" in s or "v=" not in s):
        return "playlist", s
    # Standard watch?v=, youtu.be/, /shorts/, /embed/, /live/
    m_vid = re.search(
        r"(?:youtube\.com/(?:watch\?.*?\bv=|shorts/|embed/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})",
        s,
    )
    if m_vid:
        return "video", m_vid.group(1)
    if m_list:
        return "playlist", s
    # Raw 11-char YouTube ID (no spaces, typical ID chars)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s) and ("-" in s or "_" in s or any(c.isdigit() for c in s)):
        return "video", s
    return "search", s


def load_ui_fonts(width: int, height: int) -> dict:
    wide = width >= 300
    large = width >= 540 and height >= 300
    mid = (width >= 200 and height >= 200 and width < 300)
    short = (height <= 200)
    if large:
        t_px, b_px, s_px, tag_px = 15, 13, 11, 10
    elif wide:
        t_px, b_px, s_px, tag_px = 12, 10, 9, 8
    elif width >= 200:
        t_px, b_px, s_px, tag_px = 11, 10, 8, 8
    else:
        t_px, b_px, s_px, tag_px = 9, 8, 7, 7
    return {
        "wide": wide,
        "large": large,
        "mid": mid,
        "short": short,
        "title": rj_env.load_font(t_px, bold=True),
        "body": rj_env.load_font(b_px, bold=False),
        "body_bold": rj_env.load_font(b_px, bold=True),
        "sub": rj_env.load_font(s_px, bold=False),
        "badge": rj_env.load_font(tag_px, bold=True, mono=True),
    }


def _yt_chrome_heights(width: int, height: int, fonts: dict) -> Tuple[int, int]:
    large = bool(fonts.get("large"))
    wide = bool(fonts.get("wide"))
    mid = bool(fonts.get("mid", width >= 200 and height >= 200 and width < 300))
    short = bool(fonts.get("short", height <= 200))
    if large:
        return 30, 22
    if wide and short:
        return 20, 16
    if wide:
        return 24, 18
    if mid:
        return 22, 16
    return 18, 13


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def _write_json(path: str, data) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def load_settings(default_wide: bool = True) -> dict:
    path = os.path.join(get_youtube_data_dir(), "settings.json")
    cfg = _read_json(path, {})
    if not isinstance(cfg, dict):
        cfg = {}
    # 360p (640x360 single muxed H.264+AAC stream) matches the 640x360 HDMI canvas 1:1 with minimal CPU
    default_q = 2
    r_scale = str(cfg.get("render_scale", "auto") or "auto").strip().lower()
    if r_scale not in ("auto", "fast", "balanced", "sharp"):
        r_scale = "auto"
    try:
        preload_sec = int(cfg.get("preload_sec", 5))
    except Exception:
        preload_sec = 5
    if preload_sec not in (0, 3, 5, 10, -1):
        preload_sec = 5
    engine = str(cfg.get("player_engine", "builtin") or "builtin").strip().lower()
    if engine not in ("builtin", "mpv"):
        engine = "builtin"
    try:
        spd = float(cfg.get("playback_speed", 1.0))
    except Exception:
        spd = 1.0
    if spd not in SPEED_OPTIONS:
        spd = 1.0
    return {
        "quality_idx": max(0, min(len(STREAM_QUALITIES) - 1, int(cfg.get("quality_idx", default_q)))),
        "auto_next": bool(cfg.get("auto_next", True)),
        "show_osd": bool(cfg.get("show_osd", True)),
        "fps_wide": max(10, min(30, int(cfg.get("fps_wide", 24)))),
        "fps_compact": max(8, min(24, int(cfg.get("fps_compact", 12)))),
        "render_scale": r_scale,
        "preload_sec": preload_sec,
        "player_engine": engine,
        "playback_speed": spd,
        "auto_captions": bool(cfg.get("auto_captions", False)),
        # Extra video delay (ms) to match Bluetooth A2DP wireless audio latency (0 on wired, ~200ms on BT)
        "av_sync_ms": max(-500, min(800, int(cfg.get("av_sync_ms", 0)))),
        "bt_sync_ms": max(0, min(600, int(cfg.get("bt_sync_ms", 200)))),
    }


def compute_video_render_size(
    quality_label: str,
    max_w: int,
    max_h: int,
    render_scale: str = "auto",
    is_hdmi: bool = True,
) -> Tuple[int, int]:
    """Choose optimal video decode/render dimensions for smooth playback.

    On HDMI (640x360 canvas):
      - 'fast' (or 'auto' at 144p/240p): 320x180 (exact 6x integer divisor of 1080p, 4x less CPU)
      - 'balanced': 480x270 (exact 4x integer divisor of 1080p, ~1.8x less CPU than 640x360)
      - 'sharp' (or 'auto' at 360p+): 640x360 (exact 3x integer divisor of 1080p)
    """
    if not is_hdmi or max_w < 480 or max_h < 270:
        return int(max_w), int(max_h)
    mode = (render_scale or "auto").strip().lower()
    ql = (quality_label or "").strip().lower()
    if mode == "fast" or (mode == "auto" and ql in ("144p", "240p")):
        return 320, 180
    if mode == "balanced":
        return 480, 270
    return int(max_w), int(max_h)


def build_video_ffmpeg_cmd(
    video_url: str,
    audio_url: str,
    width: int,
    height: int,
    pix_fmt: str,
    target_fps: int,
    audio_args: List[str],
    start_offset: float = 0.0,
    audio_delay_ms: int = 0,
    use_realtime: bool = True,
    input_is_pipe: bool = False,
    playback_speed: float = 1.0,
) -> List[str]:
    """Build an A/V-synchronized, low-CPU ffmpeg command for single- or dual-stream video playback."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts"]

    if not input_is_pipe and str(video_url).startswith(("http://", "https://")):
        if not str(video_url).startswith(("http://127.0.0.1:", "http://localhost:")):
            cmd += ["-user_agent", _YT_USER_AGENT]
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "2"]
    cmd += ["-thread_queue_size", "512", "-threads", "0"]
    if use_realtime:
        cmd += ["-re"]
    if input_is_pipe:
        cmd += ["-f", "matroska", "-i", "pipe:0"]
        if start_offset > 0:
            cmd += ["-ss", f"{start_offset:.2f}"]
    else:
        if start_offset > 0:
            cmd += ["-ss", f"{start_offset:.2f}"]
        cmd += ["-i", video_url]

    if audio_url and not input_is_pipe:
        if str(audio_url).startswith(("http://", "https://")):
            if not str(audio_url).startswith(("http://127.0.0.1:", "http://localhost:")):
                cmd += ["-user_agent", _YT_USER_AGENT]
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "2"]
        cmd += ["-thread_queue_size", "512"]
        if use_realtime:
            cmd += ["-re"]
        if start_offset > 0:
            cmd += ["-ss", f"{start_offset:.2f}"]
        cmd += ["-i", audio_url]

    spd = max(0.5, min(2.0, float(playback_speed or 1.0)))
    vf_parts = []
    if abs(spd - 1.0) > 0.01:
        vf_parts.append(f"setpts=PTS/{spd:.3f}")
    vf_parts.extend([
        f"fps={target_fps}",
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=fast_bilinear",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=#0e0e10",
    ])
    vf = ",".join(vf_parts)
    cmd += [
        "-map", "0:v:0",
        "-vf", vf,
        "-pix_fmt", pix_fmt,
        "-avoid_negative_ts", "make_zero",
        "-max_muxing_queue_size", "1024",
        "-f", "rawvideo",
        "pipe:1",
    ]

    af_parts = []
    if abs(spd - 1.0) > 0.01:
        af_parts.append(f"atempo={spd:.3f}")
    if audio_delay_ms > 0:
        af_parts.append(f"adelay={int(audio_delay_ms)}|{int(audio_delay_ms)}")
    af_parts.append("aresample=async=1:first_pts=0")
    af_chain = ",".join(af_parts)

    a_map = "1:a:0" if (audio_url and not input_is_pipe) else "0:a:0?"
    cmd += [
        "-map", a_map,
        "-af", af_chain,
        "-ac", "2",
        "-ar", "44100",
        "-avoid_negative_ts", "make_zero",
        "-max_muxing_queue_size", "1024",
    ] + list(audio_args)
    return cmd


def save_settings(cfg: dict) -> None:
    _write_json(os.path.join(get_youtube_data_dir(), "settings.json"), cfg)


def load_history() -> List[str]:
    data = _read_json(os.path.join(get_youtube_data_dir(), "history.json"), [])
    return [str(x) for x in data][:HISTORY_MAX] if isinstance(data, list) else []


def save_history_entry(query: str) -> List[str]:
    q = (query or "").strip()
    if not q:
        return load_history()
    hist = [x for x in load_history() if x.lower() != q.lower()]
    hist.insert(0, q)
    hist = hist[:HISTORY_MAX]
    _write_json(os.path.join(get_youtube_data_dir(), "history.json"), hist)
    return hist


def clear_history() -> None:
    _write_json(os.path.join(get_youtube_data_dir(), "history.json"), [])


def load_liked() -> List[dict]:
    data = _read_json(os.path.join(get_youtube_data_dir(), "liked.json"), [])
    return data if isinstance(data, list) else []


def save_liked(liked: List[dict]) -> None:
    _write_json(os.path.join(get_youtube_data_dir(), "liked.json"), liked)


def is_liked(video_id: str) -> bool:
    return any(v.get("id") == video_id for v in load_liked())


def toggle_like_video(video: dict) -> bool:
    """Toggle liked state for *video*. Returns True if now liked, False if removed."""
    vid = video.get("id", "")
    if not vid:
        return False
    liked = load_liked()
    for i, item in enumerate(liked):
        if item.get("id") == vid:
            liked.pop(i)
            save_liked(liked)
            return False
    liked.insert(0, {
        "id": vid,
        "title": str(video.get("title", "Untitled"))[:100],
        "channel": str(video.get("channel", ""))[:40],
        "duration": int(video.get("duration") or 0),
        "views": int(video.get("views") or 0),
        "thumb": str(video.get("thumb", "")),
    })
    save_liked(liked)
    return True


def load_presets() -> List[str]:
    data = _read_json(os.path.join(get_youtube_data_dir(), "presets.json"), None)
    if isinstance(data, list):
        return [str(x) for x in data]
    return list(DEFAULT_PRESETS)


def save_presets(presets: List[str]) -> None:
    _write_json(os.path.join(get_youtube_data_dir(), "presets.json"), presets)


def sanitize_yt_name(text: str, max_len: int = 52, fallback: str = "Playlist") -> str:
    cleaned = "".join(c for c in str(text or "") if c.isalnum() or c in " -_()").strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)[:max_len].strip(" .-_")
    return cleaned or fallback


def extract_playlist_title(entries: List[dict], fallback: str = "YouTube Playlist") -> str:
    """Extract the real YouTube playlist title from yt-dlp flat JSON entries."""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for key in ("playlist_title", "playlist", "title"):
            if key == "title" and entry.get("_type") != "playlist":
                continue
            val = str(entry.get(key) or "").strip()
            if val and val.lower() not in ("na", "none", "youtube"):
                return val[:56]
    return fallback


# ---- Video Playlists & Watch Later ----

def load_video_playlists() -> List[dict]:
    data = _read_json(os.path.join(get_youtube_data_dir(), "playlists.json"), [])
    pls = data if isinstance(data, list) else []
    if not any(p.get("id") == WATCH_LATER_ID for p in pls):
        pls.insert(0, {"id": WATCH_LATER_ID, "name": "Watch Later", "items": [], "source_url": "", "pinned": True})
    return pls


def save_video_playlists(playlists: List[dict]) -> None:
    _write_json(os.path.join(get_youtube_data_dir(), "playlists.json"), playlists)


def is_in_watch_later(video_id: str) -> bool:
    for pl in load_video_playlists():
        if pl.get("id") == WATCH_LATER_ID:
            return any(v.get("id") == video_id for v in pl.get("items", []))
    return False


def toggle_watch_later(video: dict) -> bool:
    vid = str(video.get("id") or "")
    if not vid:
        return False
    pls = load_video_playlists()
    for pl in pls:
        if pl.get("id") == WATCH_LATER_ID:
            items = pl.setdefault("items", [])
            for i, it in enumerate(items):
                if it.get("id") == vid:
                    items.pop(i)
                    save_video_playlists(pls)
                    return False
            items.insert(0, {
                "id": vid,
                "title": str(video.get("title", "Untitled"))[:100],
                "channel": str(video.get("channel", ""))[:40],
                "channel_url": str(video.get("channel_url", "")),
                "channel_id": str(video.get("channel_id", "")),
                "duration": int(video.get("duration") or 0),
                "views": int(video.get("views") or 0),
                "thumb": str(video.get("thumb", "")),
            })
            save_video_playlists(pls)
            return True
    return False


def create_video_playlist(name: str, items: Optional[List[dict]] = None, source_url: str = "") -> dict:
    pls = load_video_playlists()
    pid = f"vpl_{int(time.time())}_{random.randint(100, 999)}"
    pl = {
        "id": pid,
        "name": (name or "").strip()[:56] or "My Video Playlist",
        "items": [dict(x) for x in (items or [])],
        "source_url": str(source_url or "").strip(),
    }
    # Insert directly after pinned Watch Later
    ins_at = 1 if (pls and pls[0].get("id") == WATCH_LATER_ID) else 0
    pls.insert(ins_at, pl)
    save_video_playlists(pls)
    return pl


def add_to_video_playlist(playlist_id: str, video: dict) -> bool:
    vid = str(video.get("id") or "")
    if not vid:
        return False
    pls = load_video_playlists()
    for pl in pls:
        if pl.get("id") == playlist_id:
            items = pl.setdefault("items", [])
            if any(x.get("id") == vid for x in items):
                return False
            items.append(dict(video))
            save_video_playlists(pls)
            return True
    return False


def remove_from_video_playlist(playlist_id: str, idx: int) -> bool:
    pls = load_video_playlists()
    for pl in pls:
        if pl.get("id") == playlist_id:
            items = pl.get("items", [])
            if 0 <= idx < len(items):
                items.pop(idx)
                save_video_playlists(pls)
                return True
    return False


def rename_video_playlist(playlist_id: str, new_name: str) -> bool:
    clean = (new_name or "").strip()[:56]
    if not clean or playlist_id == WATCH_LATER_ID:
        return False
    pls = load_video_playlists()
    for pl in pls:
        if pl.get("id") == playlist_id:
            pl["name"] = clean
            save_video_playlists(pls)
            return True
    return False


def delete_video_playlist(playlist_id: str) -> bool:
    if playlist_id == WATCH_LATER_ID:
        return False
    pls = load_video_playlists()
    kept = [p for p in pls if p.get("id") != playlist_id]
    if len(kept) == len(pls):
        return False
    save_video_playlists(kept)
    return True


# ---- Silent Auto-Resume Timestamps (No Watch History Menu Clutter) ----

def load_resume_map() -> Dict[str, dict]:
    data = _read_json(os.path.join(get_youtube_data_dir(), "resume.json"), {})
    return data if isinstance(data, dict) else {}


def save_resume_position(video_id: str, pos_sec: float, duration: int) -> None:
    vid = str(video_id or "").strip()
    if not vid or duration <= 30:
        return
    rmap = load_resume_map()
    pos = float(pos_sec or 0.0)
    if pos < 10.0 or pos >= (duration * 0.92):
        if vid in rmap:
            rmap.pop(vid, None)
            _write_json(os.path.join(get_youtube_data_dir(), "resume.json"), rmap)
        return
    rmap[vid] = {"pos": round(pos, 1), "duration": int(duration), "updated": int(time.time())}
    # Keep bounded to 200 most recent entries
    if len(rmap) > 200:
        oldest = sorted(rmap.items(), key=lambda kv: kv[1].get("updated", 0))[:len(rmap) - 200]
        for k, _ in oldest:
            rmap.pop(k, None)
    _write_json(os.path.join(get_youtube_data_dir(), "resume.json"), rmap)


def get_resume_position(video_id: str) -> float:
    entry = load_resume_map().get(str(video_id or ""))
    if isinstance(entry, dict):
        return float(entry.get("pos") or 0.0)
    return 0.0


# ---- Channel Subscriptions (Zero API Keys) ----

def load_subscriptions() -> List[dict]:
    data = _read_json(os.path.join(get_youtube_data_dir(), "subscriptions.json"), [])
    return data if isinstance(data, list) else []


def save_subscriptions(subs: List[dict]) -> None:
    _write_json(os.path.join(get_youtube_data_dir(), "subscriptions.json"), subs)


def _norm_channel_url(channel_name: str = "", channel_url: str = "", channel_id: str = "") -> str:
    u = (channel_url or "").strip()
    if u.startswith(("http://", "https://")):
        return u.rstrip("/")
    cid = (channel_id or "").strip()
    if cid.startswith("UC"):
        return f"https://www.youtube.com/channel/{cid}"
    nm = (channel_name or "").strip()
    if nm.startswith("@"):
        return f"https://www.youtube.com/{nm}"
    return ""


def is_subscribed(channel_name: str, channel_id: str = "") -> bool:
    nm_l = (channel_name or "").strip().lower()
    cid_l = (channel_id or "").strip().lower()
    for s in load_subscriptions():
        if cid_l and str(s.get("channel_id", "")).lower() == cid_l:
            return True
        if nm_l and str(s.get("name", "")).lower() == nm_l:
            return True
    return False


def toggle_subscription(channel_name: str, channel_url: str = "", channel_id: str = "") -> bool:
    nm = (channel_name or "").strip()
    if not nm:
        return False
    subs = load_subscriptions()
    nm_l = nm.lower()
    cid_l = (channel_id or "").strip().lower()
    for i, s in enumerate(subs):
        if (cid_l and str(s.get("channel_id", "")).lower() == cid_l) or str(s.get("name", "")).lower() == nm_l:
            subs.pop(i)
            save_subscriptions(subs)
            return False
    subs.insert(0, {
        "name": nm[:48],
        "channel_id": (channel_id or "").strip(),
        "url": _norm_channel_url(nm, channel_url, channel_id),
        "added_at": int(time.time()),
    })
    save_subscriptions(subs)
    return True


# ---- Video Chapters & WebVTT Closed Captions ----

def get_active_chapter(chapters: Optional[List[dict]], elapsed: float) -> Optional[dict]:
    if not chapters:
        return None
    el = float(elapsed or 0.0)
    cur = None
    for ch in chapters:
        st = float(ch.get("start", 0.0))
        en = float(ch.get("end", 1e9))
        if st <= el < en:
            return ch
        if st <= el:
            cur = ch
    return cur


def get_adjacent_chapter_time(chapters: Optional[List[dict]], elapsed: float, direction: int) -> Optional[float]:
    if not chapters:
        return None
    el = float(elapsed or 0.0)
    starts = sorted(float(c.get("start", 0.0)) for c in chapters)
    if direction > 0:
        for st in starts:
            if st > el + 1.5:
                return st
        return None
    for st in reversed(starts):
        if st < el - 3.0:
            return st
    return starts[0] if starts else None


_VTT_TIME_RE = re.compile(r"(?:(\d{1,2}):)?(\d{2}):(\d{2})(?:[.,](\d{1,3}))?")
_VTT_TAG_RE = re.compile(r"<[^>]+>")


def _parse_vtt_ts(match: re.Match) -> float:
    h = int(match.group(1) or 0)
    m = int(match.group(2) or 0)
    s = int(match.group(3) or 0)
    ms_s = match.group(4) or "0"
    ms = int(ms_s.ljust(3, "0")[:3])
    return h * 3600.0 + m * 60.0 + s + ms / 1000.0


def parse_vtt_subtitles(vtt_text: str) -> List[Tuple[float, float, str]]:
    """Parse WebVTT (.vtt) text into sorted, cleaned [(start_sec, end_sec, text), ...]."""
    cues: List[Tuple[float, float, str]] = []
    if not vtt_text:
        return cues
    blocks = re.split(r"\r?\n\s*\r?\n", vtt_text)
    for blk in blocks:
        lines = [ln.strip() for ln in blk.splitlines() if ln.strip()]
        if not lines:
            continue
        ts_idx = -1
        for i, ln in enumerate(lines):
            if "-->" in ln:
                ts_idx = i
                break
        if ts_idx < 0:
            continue
        parts = lines[ts_idx].split("-->")
        if len(parts) < 2:
            continue
        m_st = _VTT_TIME_RE.search(parts[0])
        m_en = _VTT_TIME_RE.search(parts[1])
        if not m_st or not m_en:
            continue
        st_s = _parse_vtt_ts(m_st)
        en_s = _parse_vtt_ts(m_en)
        if en_s <= st_s:
            continue
        raw_txt = " ".join(lines[ts_idx + 1:])
        clean = _VTT_TAG_RE.sub("", raw_txt)
        clean = (
            clean.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&nbsp;", " ")
            .replace("&#39;", "'")
            .replace("&quot;", '"')
        )
        clean = re.sub(r"\s+", " ", clean).strip()
        if not clean:
            continue
        # Deduplicate YouTube rolling auto-caption overlapping lines
        if cues and cues[-1][2] == clean and abs(cues[-1][1] - st_s) < 0.4:
            cues[-1] = (cues[-1][0], max(cues[-1][1], en_s), clean)
        else:
            cues.append((st_s, en_s, clean))
    cues.sort(key=lambda c: c[0])
    return cues


def get_active_subtitle(cues: Optional[List[Tuple[float, float, str]]], elapsed: float) -> str:
    if not cues:
        return ""
    el = float(elapsed or 0.0)
    matched = ""
    for st, en, txt in cues:
        if st <= el <= en:
            matched = txt
        elif st > el:
            break
    return matched


def apply_search_filters(items: List[dict], sort_by: str = "relevance", duration_filter: str = "any") -> List[dict]:
    """Filter and sort YouTube search results by duration bucket and sort order."""
    out = list(items)
    if duration_filter == "short":
        out = [x for x in out if 0 < int(x.get("duration") or 0) < 240]
    elif duration_filter == "medium":
        out = [x for x in out if 240 <= int(x.get("duration") or 0) <= 1200]
    elif duration_filter == "long":
        out = [x for x in out if int(x.get("duration") or 0) > 1200]

    if sort_by == "views":
        out.sort(key=lambda x: int(x.get("views") or 0), reverse=True)
    elif sort_by == "shortest":
        out.sort(key=lambda x: int(x.get("duration") or 999999))
    elif sort_by == "longest":
        out.sort(key=lambda x: int(x.get("duration") or 0), reverse=True)
    return out


# ---- Organized Downloads (Root + Playlist Subfolders) ----

def list_download_folders() -> List[dict]:
    d_dir = get_downloads_dir()
    folders: List[dict] = []
    if not os.path.isdir(d_dir):
        return folders
    try:
        for entry in sorted(os.listdir(d_dir), key=str.lower):
            full = os.path.join(d_dir, entry)
            if not os.path.isdir(full):
                continue
            cnt = sum(
                1 for fn in os.listdir(full)
                if os.path.isfile(os.path.join(full, fn)) and fn.lower().endswith(_VIDEO_EXTS + _AUDIO_EXTS)
            )
            folders.append({"name": entry, "path": full, "count": cnt})
    except OSError:
        pass
    return folders


def move_download_to_folder(filepath: str, folder_name: str) -> Tuple[bool, str, str]:
    src = os.path.abspath(str(filepath or ""))
    if not src or not os.path.isfile(src):
        return False, "File not found", ""
    clean = sanitize_yt_name(folder_name, max_len=52, fallback="") if folder_name else ""
    dest_dir = os.path.join(get_downloads_dir(), clean) if clean else get_downloads_dir()
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.abspath(os.path.join(dest_dir, os.path.basename(src)))
    if src == dest:
        return True, "Already in folder", dest
    try:
        shutil.move(src, dest)
        return True, f"Moved to {clean or 'Downloads'}", dest
    except OSError as exc:
        return False, str(exc)[:44], ""


def list_downloads(folder: Optional[str] = None, kind_filter: str = "all") -> List[dict]:
    """List locally downloaded MP4/MP3 media files in Downloads (including playlist subfolders)."""
    d_dir = get_downloads_dir()
    if not os.path.isdir(d_dir):
        return []
    scan_dirs: List[Tuple[str, str]] = []
    if folder:
        scan_dirs.append((os.path.join(d_dir, folder), folder))
    else:
        scan_dirs.append((d_dir, ""))
        try:
            for entry in sorted(os.listdir(d_dir), key=str.lower):
                sub = os.path.join(d_dir, entry)
                if os.path.isdir(sub):
                    scan_dirs.append((sub, entry))
        except OSError:
            pass

    raw_items: List[Tuple[float, dict]] = []
    for cur_dir, fld_lbl in scan_dirs:
        if not os.path.isdir(cur_dir):
            continue
        try:
            entries = os.listdir(cur_dir)
        except OSError:
            continue
        for fn in entries:
            full = os.path.join(cur_dir, fn)
            if not os.path.isfile(full):
                continue
            low = fn.lower()
            if low.endswith(_VIDEO_EXTS):
                kind = "video"
            elif low.endswith(_AUDIO_EXTS):
                kind = "audio"
            else:
                continue
            if kind_filter in ("video", "audio") and kind != kind_filter:
                continue
            try:
                size_mb = os.path.getsize(full) / (1024.0 * 1024.0)
                mtime = os.path.getmtime(full)
            except OSError:
                size_mb = 0.0
                mtime = 0.0
            meta_lbl = f"{kind.upper()}  •  {size_mb:.1f} MB"
            if fld_lbl:
                meta_lbl = f"[{fld_lbl}]  {meta_lbl}"
            raw_items.append((mtime, {
                "id": fn,
                "path": full,
                "title": os.path.splitext(fn)[0],
                "channel": meta_lbl,
                "folder": fld_lbl,
                "duration": 0,
                "kind": kind,
                "size_mb": size_mb,
                "local": True,
            }))
    raw_items.sort(key=lambda pair: pair[0], reverse=True)
    return [it for _, it in raw_items]


# ---------------------------------------------------------------------------
# Async Thumbnail Fetcher & Disk Cache (Zero Extra Dependencies)
# ---------------------------------------------------------------------------

class ThumbnailCache:
    """Thread-safe async thumbnail loader backed by PIL + urllib."""

    def __init__(self):
        self._mem: Dict[str, Optional[Image.Image]] = {}
        self._pending: set = set()
        self._lock = threading.Lock()
        self.updated = False

    def get(self, video_id: str, url: str = "") -> Optional[Image.Image]:
        if not video_id:
            return None
        with self._lock:
            if video_id in self._mem:
                return self._mem[video_id]
            if video_id in self._pending:
                return None
            self._pending.add(video_id)

        thumb_url = url or f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
        threading.Thread(target=self._fetch_worker, args=(video_id, thumb_url), daemon=True).start()
        return None

    def _fetch_worker(self, video_id: str, url: str) -> None:
        cache_dir = get_thumb_cache_dir()
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "", video_id)[:24]
        cache_path = os.path.join(cache_dir, f"{safe_id}.jpg")
        img: Optional[Image.Image] = None
        try:
            if os.path.isfile(cache_path):
                with Image.open(cache_path) as raw:
                    img = raw.convert("RGB")
            else:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    blob = resp.read(512 * 1024)
                with Image.open(io.BytesIO(blob)) as raw:
                    img = raw.convert("RGB")
                try:
                    os.makedirs(cache_dir, exist_ok=True)
                    img.save(cache_path, "JPEG", quality=80)
                except Exception:
                    pass
        except Exception:
            img = None
        with self._lock:
            self._mem[video_id] = img
            self._pending.discard(video_id)
            if img is not None:
                self.updated = True


# ---------------------------------------------------------------------------
# Pure Obsidian Ember PIL Renderers (Testable & Resolution-Adaptive)
# ---------------------------------------------------------------------------

def _draw_header(
    d: ImageDraw.ImageDraw,
    width: int,
    hdr_h: int,
    title: str,
    fonts: dict,
    badge: str = "",
    right_text: str = "",
    right_pill: str = "",
) -> None:
    ET = rj_theme.EMBER_THEME
    wide = fonts["wide"]
    d.rectangle((0, 0, width - 1, hdr_h - 1), fill=ET["bg_elevated"])
    d.line([(0, hdr_h - 1), (width - 1, hdr_h - 1)], fill=ET["border_subtle"], width=1)

    x = 8 if wide else 3
    # Accent play glyph box
    box_s = 16 if fonts["large"] else (12 if wide else 9)
    by = (hdr_h - box_s) // 2
    d.rounded_rectangle((x, by, x + box_s + 4, by + box_s), radius=3, fill=ET["accent"])
    tri_x = x + (box_s + 4) // 2 - 2
    tri_y = by + box_s // 2
    tri_r = max(2, box_s // 4)
    d.polygon(
        [(tri_x - 1, tri_y - tri_r), (tri_x - 1, tri_y + tri_r), (tri_x + tri_r + 2, tri_y)],
        fill=ET["text_on_accent"],
    )
    x += box_s + (10 if wide else 6)

    tb = d.textbbox((0, 0), title, font=fonts["title"])
    d.text((x, (hdr_h - (tb[3] - tb[1])) // 2 - tb[1]), title, font=fonts["title"], fill=ET["accent_bright"])
    x += (tb[2] - tb[0]) + (8 if wide else 4)

    if badge:
        bw, _ = rj_theme.draw_pill(
            d, x, max(2, (hdr_h - (16 if wide else 11)) // 2),
            badge, fonts["badge"], bg=ET["select_bg"], fg=ET["accent_bright"],
            outline=ET["border_accent"], pad_x=5 if wide else 3, pad_y=1, radius=3,
        )
        x += bw + 6

    rx = width - (8 if wide else 3)
    if right_pill and wide:
        pw = d.textbbox((0, 0), right_pill, font=fonts["badge"])[2] + 10
        rj_theme.draw_pill(
            d, rx - pw, max(2, (hdr_h - 16) // 2),
            right_pill, fonts["badge"], bg=ET["bg_surface"], fg=ET["text_primary"],
            outline=ET["border_subtle"], pad_x=5, pad_y=1, radius=3,
        )
        rx -= pw + 8

    if right_text and wide and (rx - x) > 55:
        rt = rj_theme.truncate_text(d, right_text, rx - x - 4, fonts["sub"])
        rb = d.textbbox((0, 0), rt, font=fonts["sub"])
        d.text((rx - (rb[2] - rb[0]), (hdr_h - (rb[3] - rb[1])) // 2 - rb[1]), rt, font=fonts["sub"], fill=ET["text_muted"])


def _draw_footer(d: ImageDraw.ImageDraw, width: int, height: int, ftr_h: int, hint: str, fonts: dict) -> None:
    ET = rj_theme.EMBER_THEME
    d.rectangle((0, height - ftr_h, width - 1, height - 1), fill=ET["bg_elevated"])
    d.line([(0, height - ftr_h), (width - 1, height - ftr_h)], fill=ET["border_subtle"], width=1)
    txt = rj_theme.truncate_text(d, hint, width - 10, fonts["sub"])
    hb = d.textbbox((0, 0), txt, font=fonts["sub"])
    d.text((6 if fonts["wide"] else 3, height - ftr_h + (ftr_h - (hb[3] - hb[1])) // 2 - hb[1]), txt, font=fonts["sub"], fill=ET["text_muted"])


def render_status_card(
    width: int,
    height: int,
    title: str,
    sub: str = "",
    kind: str = "info",
    progress: Optional[float] = None,
    foot: str = "",
    fonts: Optional[dict] = None,
) -> Image.Image:
    ET = rj_theme.EMBER_THEME
    f = fonts or load_ui_fonts(width, height)
    wide = f["wide"]
    mid = bool(f.get("mid", width >= 200 and height >= 200 and width < 300))
    short = bool(f.get("short", height <= 200))
    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    cw = min(width - 16, max(112, int(width * 0.74)))
    if wide and not short:
        raw_ch = 118 if progress is not None else 90
    elif wide or mid:
        raw_ch = 104 if progress is not None else 82
    else:
        raw_ch = 78 if progress is not None else 58
    ch = min(height - 12, raw_ch)
    cx0 = (width - cw) // 2
    cy0 = max(4, (height - ch) // 2)
    accent = ET["danger"] if kind == "err" else (ET["ok"] if kind == "ok" else ET["accent"])
    rj_theme.draw_card(
        d, (cx0, cy0, cx0 + cw, cy0 + ch),
        fill=ET["bg_surface"], outline=accent, radius=8 if (wide or mid) else 4,
        accent_left=accent, accent_width=4 if (wide or mid) else 2,
    )
    tx = cx0 + (16 if (wide and not short) else (10 if (wide or mid) else 8))
    d.text((tx, cy0 + (14 if (wide and not short) else (10 if (wide or mid) else 7))), rj_theme.truncate_text(d, title, cw - 22, f["title"]), font=f["title"], fill=ET["accent_bright"] if kind == "info" else accent)
    if sub:
        d.text((tx, cy0 + (38 if (wide and not short) else (30 if (wide or mid) else 22))), rj_theme.truncate_text(d, sub, cw - 22, f["body"]), font=f["body"], fill=ET["text_primary"])
    if progress is not None:
        bar_h = 10 if (wide and not short) else (8 if (wide or mid) else 6)
        by = min(cy0 + ch - (32 if (wide or mid) else 24), cy0 + (66 if (wide and not short) else (52 if (wide or mid) else 40)))
        rj_theme.draw_progress_bar(d, (tx, by, cx0 + cw - (14 if (wide or mid) else 8), by + bar_h), progress)
        d.text((cx0 + cw - (14 if (wide or mid) else 8), by + bar_h + 3), f"{int(round(progress * 100))}%", font=f["badge"], fill=ET["accent_bright"], anchor="ra")
    if foot:
        d.text((tx, cy0 + ch - (20 if (wide and not short) else (16 if (wide or mid) else 13))), rj_theme.truncate_text(d, foot, cw - 22, f["sub"]), font=f["sub"], fill=ET["text_muted"])
    return img


def render_home_menu(
    width: int,
    height: int,
    sel_idx: int,
    audio_label: str = "System Audio",
    quality_label: str = "720p",
    online: bool = True,
    liked_count: int = 0,
    downloads_count: int = 0,
    subs_count: int = 0,
    playlists_count: int = 0,
    fonts: Optional[dict] = None,
) -> Image.Image:
    ET = rj_theme.EMBER_THEME
    f = fonts or load_ui_fonts(width, height)
    wide = f["wide"]
    large = f["large"]
    mid = bool(f.get("mid", width >= 200 and height >= 200 and width < 300))
    short = bool(f.get("short", height <= 200))
    hdr_h, ftr_h = _yt_chrome_heights(width, height, f)

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    badge = "OFFLINE" if not online else quality_label
    _draw_header(d, width, hdr_h, "YOUTUBE", f, badge=badge, right_text=audio_label)

    y0 = hdr_h + (8 if large else (4 if mid else 3))
    y1 = height - ftr_h - (6 if large else 3)

    if large:
        cols = 2
        gap_x, gap_y = 10, 8
        pad_x = 12
        card_w = (width - pad_x * 2 - gap_x) // cols
        rows_n = (len(HOME_MENU_ITEMS) + cols - 1) // cols
        card_h = max(44, min(64, (y1 - y0 - gap_y * (rows_n - 1)) // rows_n))
        for i, (label, key, desc) in enumerate(HOME_MENU_ITEMS):
            r, c = divmod(i, cols)
            x0 = pad_x + c * (card_w + gap_x)
            ry0 = y0 + r * (card_h + gap_y)
            sel = i == sel_idx
            rj_theme.draw_card(
                d, (x0, ry0, x0 + card_w, ry0 + card_h),
                fill=ET["select_bg"] if sel else ET["bg_surface"],
                outline=ET["border_accent"] if sel else ET["border_subtle"],
                radius=7,
                accent_left=ET["accent"] if sel else None,
                accent_width=3,
            )
            extra = ""
            if key == "liked" and liked_count:
                extra = str(liked_count)
            elif key == "downloads" and downloads_count:
                extra = str(downloads_count)
            elif key == "subs" and subs_count:
                extra = str(subs_count)
            elif key == "playlists" and playlists_count:
                extra = str(playlists_count)
            d.text((x0 + 12, ry0 + 8), label, font=f["body_bold"], fill=ET["accent_bright"] if sel else ET["text_bright"])
            if extra:
                ew = d.textbbox((0, 0), extra, font=f["badge"])[2] + 10
                rj_theme.draw_pill(
                    d, x0 + card_w - ew - 8, ry0 + 8, extra, f["badge"],
                    bg=ET["bg_well"], fg=ET["accent_bright"], outline=ET["border_subtle"],
                    pad_x=5, pad_y=1, radius=3,
                )
            d.text((x0 + 12, ry0 + 28), rj_theme.truncate_text(d, desc, card_w - 20, f["sub"]), font=f["sub"], fill=ET["text_muted"])
    else:
        row_h = 24 if mid else (21 if (wide and short) else (22 if wide else 14))
        vis = max(1, (y1 - y0) // row_h)
        st = max(0, min(sel_idx - vis // 2, max(0, len(HOME_MENU_ITEMS) - vis)))
        for slot in range(vis):
            idx = st + slot
            if idx >= len(HOME_MENU_ITEMS):
                break
            label, key, _desc = HOME_MENU_ITEMS[idx]
            sel = idx == sel_idx
            ry = y0 + slot * row_h
            ry1 = min(y1, ry + row_h - 2)
            rj_theme.draw_card(
                d, (4, ry, width - 8, ry1),
                fill=ET["select_bg"] if sel else ET["bg_surface"],
                outline=ET["border_accent"] if sel else None,
                radius=4 if (wide or mid) else 2,
                accent_left=ET["accent"] if sel else None,
                accent_width=2,
            )
            d.text((10 if (wide or mid) else 7, ry + (4 if mid else (3 if wide else 1))), label, font=f["body_bold"] if (sel and (wide or mid)) else f["body"], fill=ET["accent_bright"] if sel else ET["text_primary"])
        if len(HOME_MENU_ITEMS) > vis:
            rj_theme.draw_scrollbar(d, width - 5, y0, y1, st, vis, len(HOME_MENU_ITEMS), width=3)

    hint = format_footer_hint(
        width, height,
        hat_hint="OK:Open K1:Find K2:Set K3:X",
        hat_240_hint="OK:Open K1:Search K2:Set K3:Exit",
        cardputer_hint="Ent:Open S:Find M:Mus O:Set Esc:Exit",
        hdmi_hint="Arrows:Nav  Enter:Open  S:Search  M:Music  O:Settings  Esc:Exit",
    )
    _draw_footer(d, width, height, ftr_h, hint, f)
    return img


def render_results_view(
    width: int,
    height: int,
    title: str,
    subtitle: str,
    items: List[dict],
    cursor: int,
    scroll: int,
    has_load_more: bool = False,
    liked_ids: Optional[set] = None,
    thumb_img: Optional[Image.Image] = None,
    music_mode: bool = False,
    audio_label: str = "",
    resume_map: Optional[Dict[str, dict]] = None,
    watch_later_ids: Optional[set] = None,
    filter_badge: str = "",
    is_playlist: bool = False,
    fonts: Optional[dict] = None,
) -> Image.Image:
    ET = rj_theme.EMBER_THEME
    f = fonts or load_ui_fonts(width, height)
    wide = f["wide"]
    large = f["large"]
    mid = bool(f.get("mid", width >= 200 and height >= 200 and width < 300))
    short = bool(f.get("short", height <= 200))
    hdr_h, ftr_h = _yt_chrome_heights(width, height, f)

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    _draw_header(d, width, hdr_h, title, f, badge=str(len(items)), right_text=subtitle or audio_label, right_pill=filter_badge)

    y0 = hdr_h + (5 if large else 3)
    y1 = height - ftr_h - (4 if large else 2)
    liked_set = liked_ids or set()
    wl_set = watch_later_ids or set()
    rmap = resume_map or {}
    total = len(items) + (1 if (has_load_more and items) else 0)

    if total == 0:
        d.text((width // 2, (y0 + y1) // 2), "No items found", font=f["body_bold"], fill=ET["text_muted"], anchor="mm")
        empty_hint = format_footer_hint(
            width, height,
            hat_hint="K1:Act K2:Set K3:Back",
            hat_240_hint="K1:Filter  K2:Settings  K3:Back",
            cardputer_hint="F:Filter  S:Settings  Esc:Back",
            hdmi_hint="F:Filter  •  S:Settings  •  Esc:Back",
        )
        _draw_footer(d, width, height, ftr_h, empty_hint, f)
        return img

    split = width >= 460
    list_x0 = 8 if wide else (4 if mid else 2)
    list_x1 = int(width * 0.62) - 4 if split else (width - (10 if (wide or mid) else 5))
    row_h = 38 if large else (25 if (wide and short) else (26 if wide else (24 if mid else 18)))
    vis = max(1, (y1 - y0) // row_h)
    st = max(0, min(scroll, max(0, total - vis)))

    for slot in range(vis):
        idx = st + slot
        if idx >= total:
            break
        ry = y0 + slot * row_h
        ry1 = min(y1, ry + row_h - (4 if (wide or mid) else 2))
        sel = idx == cursor

        if idx < len(items):
            it = items[idx]
            rj_theme.draw_card(
                d, (list_x0, ry, list_x1, ry1),
                fill=ET["select_bg"] if sel else ET["bg_surface"],
                outline=ET["border_accent"] if sel else ET["border_subtle"],
                radius=6 if (wide or mid) else 3,
                accent_left=ET["accent"] if sel else None,
                accent_width=3 if (wide or mid) else 2,
            )
            dur = format_duration(it.get("duration", 0)) if not it.get("local") else it.get("kind", "FILE").upper()
            dur_w = d.textbbox((0, 0), dur, font=f["badge"])[2] + (8 if (wide or mid) else 4)
            rx_c = list_x1 - (6 if (wide or mid) else 2)
            rj_theme.draw_pill(
                d, rx_c - dur_w, ry + (4 if (wide or mid) else 2),
                dur, f["badge"], bg=ET["bg_well"], fg=ET["accent_bright"] if sel else ET["text_muted"],
                outline=ET["border_subtle"], pad_x=4 if (wide or mid) else 2, pad_y=1, radius=3,
            )
            rx_c -= dur_w + 5
            if (wide or mid) and it.get("id") in wl_set:
                wl_w = d.textbbox((0, 0), "WL", font=f["badge"])[2] + 6
                rj_theme.draw_pill(
                    d, rx_c - wl_w, ry + 4, "WL", f["badge"],
                    bg="#2a2216", fg=ET["accent_bright"], outline=ET["border_accent"], pad_x=3, pad_y=1, radius=3,
                )
                rx_c -= wl_w + 5
            liked_mark = "* " if it.get("id") in liked_set else ""
            title_avail = max(40, rx_c - (list_x0 + 10))
            t_txt = rj_theme.truncate_text(d, liked_mark + str(it.get("title", "?")), title_avail, f["body_bold"])
            d.text((list_x0 + (8 if (wide or mid) else 4), ry + (3 if (wide or mid) else 1)), t_txt, font=f["body_bold"] if (wide or mid) else f["body"], fill=ET["text_bright"] if sel else ET["text_primary"])
            if wide or mid:
                ch_txt = rj_theme.truncate_text(d, str(it.get("channel", "")), list_x1 - list_x0 - 16, f["sub"])
                d.text((list_x0 + 8, ry + (20 if large else 12)), ch_txt, font=f["sub"], fill=ET["text_muted"])

            # Subtle auto-resume progress underline when user has watched part of this video
            r_ent = rmap.get(str(it.get("id") or ""))
            if isinstance(r_ent, dict) and r_ent.get("duration"):
                r_frac = max(0.04, min(1.0, float(r_ent.get("pos", 0)) / float(r_ent["duration"])))
                d.rectangle((list_x0 + 4, ry1 - 2, list_x0 + 4 + int((list_x1 - list_x0 - 8) * r_frac), ry1 - 1), fill=ET["accent"])
        else:
            rj_theme.draw_card(
                d, (list_x0, ry, list_x1, ry1),
                fill=ET["select_bg"] if sel else ET["bg_well"],
                outline=ET["border_accent"] if sel else ET["border_subtle"],
                radius=6 if wide else 3,
                accent_left=ET["accent"] if sel else None,
                accent_width=3 if wide else 2,
            )
            d.text(((list_x0 + list_x1) // 2, (ry + ry1) // 2), "Load more results...", font=f["body_bold"], fill=ET["accent_bright"] if sel else ET["text_muted"], anchor="mm")

    if total > vis:
        rj_theme.draw_scrollbar(d, list_x1 + 2, y0, y1, st, vis, total, width=4 if wide else 2)

    if split:
        px0 = list_x1 + 10
        px1 = width - 8
        rj_theme.draw_card(d, (px0, y0, px1, y1), fill=ET["bg_surface"], outline=ET["border_subtle"], radius=7)
        pw = px1 - px0 - 14
        if cursor < len(items):
            cur_item = items[cursor]
            th_w = pw
            th_h = max(72, min(116, int(th_w * 9 / 16)))
            tx0, ty0 = px0 + 7, y0 + 7
            d.rounded_rectangle((tx0, ty0, tx0 + th_w, ty0 + th_h), radius=5, fill=ET["bg_well"], outline=ET["border_subtle"])
            if thumb_img is not None:
                try:
                    resized = thumb_img.resize((th_w - 2, th_h - 2), Image.BILINEAR)
                    img.paste(resized, (tx0 + 1, ty0 + 1))
                except Exception:
                    pass
            else:
                d.text((tx0 + th_w // 2, ty0 + th_h // 2), "AUDIO" if (music_mode or cur_item.get("kind") == "audio") else "VIDEO", font=f["badge"], fill=ET["text_muted"], anchor="mm")

            dur_lbl = format_duration(cur_item.get("duration", 0)) if not cur_item.get("local") else cur_item.get("kind", "MEDIA").upper()
            dw = d.textbbox((0, 0), dur_lbl, font=f["badge"])[2] + 8
            rj_theme.draw_pill(
                d, tx0 + th_w - dw - 4, ty0 + th_h - 19, dur_lbl, f["badge"],
                bg=ET["bg_base"], fg=ET["accent_bright"], outline=ET["border_accent"],
                pad_x=4, pad_y=1, radius=3,
            )

            ty = ty0 + th_h + 8
            words = str(cur_item.get("title", "")).split()
            lines: List[str] = []
            cur_l = ""
            for w in words:
                cand = (cur_l + " " + w).strip()
                if d.textbbox((0, 0), cand, font=f["body_bold"])[2] <= pw:
                    cur_l = cand
                else:
                    if cur_l:
                        lines.append(cur_l)
                    cur_l = w
                    if len(lines) >= 2:
                        break
            if cur_l and len(lines) < 3:
                lines.append(cur_l)
            for ln in lines[:3]:
                d.text((tx0, ty), rj_theme.truncate_text(d, ln, pw, f["body_bold"]), font=f["body_bold"], fill=ET["text_bright"])
                ty += 16

            ch = str(cur_item.get("channel", ""))
            vw = format_views(cur_item.get("views", 0))
            meta = "  •  ".join([x for x in (ch, vw) if x])
            if meta and ty + 14 < y1 - 36:
                d.text((tx0, ty + 3), rj_theme.truncate_text(d, meta, pw, f["sub"]), font=f["sub"], fill=ET["text_muted"])

            act_y = y1 - 34
            if cur_item.get("local"):
                d.text((tx0, act_y), rj_theme.truncate_text(d, "Enter: Play   A: Move Folder", pw, f["sub"]), font=f["sub"], fill=ET["accent"])
                d.text((tx0, act_y + 15), rj_theme.truncate_text(d, "F: Folders/Filter   D: Delete", pw, f["sub"]), font=f["sub"], fill=ET["text_muted"])
            elif music_mode:
                d.text((tx0, act_y), rj_theme.truncate_text(d, "Enter: Audio  W:Later  A:List", pw, f["sub"]), font=f["sub"], fill=ET["accent"])
                d.text((tx0, act_y + 15), rj_theme.truncate_text(d, "L:Like  D:MP3  U:Chan  V:Info", pw, f["sub"]), font=f["sub"], fill=ET["text_muted"])
            else:
                d.text((tx0, act_y), rj_theme.truncate_text(d, "Enter:Video  M:Audio  W:Later", pw, f["sub"]), font=f["sub"], fill=ET["accent"])
                d.text((tx0, act_y + 15), rj_theme.truncate_text(d, "A:List  U:Chan  V:Info  D:DL", pw, f["sub"]), font=f["sub"], fill=ET["text_muted"])
        else:
            d.text(((px0 + px1) // 2, (y0 + y1) // 2), "Press Enter to fetch\n10 more results", font=f["sub"], fill=ET["text_muted"], anchor="mm")

    if is_playlist:
        hdmi_h = "Enter:Play  M:Audio  D:Download Video/All  W:Later  A:SaveList  U:Chan  V:Info  S:Set  Esc:Back"
        cp_h = "Ent:Play M:Aud D:DL W:WL O:Act S:Set Esc:B"
    elif music_mode:
        hdmi_h = "Enter:Play Audio  W:Later  A:List  L:Like  D:MP3  F:Filter/Sub  U:Channel  S:Set  Esc:Back"
        cp_h = "Ent:Audio L:Lk D:MP3 O:Act S:Set Esc:Back"
    else:
        hdmi_h = "Enter:Play  M:Audio  W:Later  A:List  F:Filter/Sub  U:Chan  V:Info  D:Download  S:Set  Esc:Back"
        cp_h = "Ent:Play M:Aud D:DL W:WL O:Act S:Set Esc:B"
    hint = format_footer_hint(
        width, height,
        hat_hint="OK:Play K1:Act K2:Set K3:B",
        hat_240_hint="OK:Play K1:Actions K2:Set K3:Back",
        cardputer_hint=cp_h,
        hdmi_hint=hdmi_h,
    )
    _draw_footer(d, width, height, ftr_h, hint, f)
    return img


def render_subtitle_overlay(
    frame_img: Image.Image,
    subtitle_text: str,
    osd_visible: bool = False,
    fonts: Optional[dict] = None,
) -> Image.Image:
    """Composite crisp, high-contrast closed captions onto a video frame."""
    txt = str(subtitle_text or "").strip()
    if not txt:
        return frame_img
    w, h = frame_img.size
    f = fonts or load_ui_fonts(w, h)
    large = f["large"]
    wide = f["wide"]
    short = bool(f.get("short", h <= 200))
    sub_font = f["body_bold"] if large else f["body"]
    out = frame_img.copy() if frame_img.mode == "RGB" else frame_img.convert("RGB")
    d = ImageDraw.Draw(out)

    max_w = max(80, int(w * 0.86))
    words = txt.split()
    lines: List[str] = []
    cur = ""
    for wd in words:
        cand = (cur + " " + wd).strip()
        if d.textbbox((0, 0), cand, font=sub_font)[2] <= max_w:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = wd
            if len(lines) >= 1:
                break
    if cur and len(lines) < 2:
        lines.append(cur)
    lines = [rj_theme.truncate_text(d, ln, max_w, sub_font) for ln in lines[:2]]
    if not lines:
        return out

    line_h = 18 if large else (13 if wide else 10)
    box_w = min(w - 16, max(d.textbbox((0, 0), ln, font=sub_font)[2] for ln in lines) + (20 if wide else 10))
    box_h = len(lines) * line_h + (10 if wide else 6)
    bot_margin = (62 if large else (34 if (wide and short) else (40 if wide else 28))) if osd_visible else (14 if large else 6)
    x0 = max(4, (w - box_w) // 2)
    y0 = max(8, h - bot_margin - box_h)
    x1 = min(w - 4, x0 + box_w)
    y1 = min(h - 4, y0 + box_h)

    crop = out.crop((x0, y0, x1, y1))
    dark = Image.new("RGB", (x1 - x0, y1 - y0), (10, 10, 12))
    out.paste(Image.blend(crop, dark, 0.84), (x0, y0))
    d = ImageDraw.Draw(out)
    d.rectangle((x0, y0, x1 - 1, y1 - 1), outline=(90, 56, 32), width=1)
    ty = y0 + (5 if wide else 3)
    for ln in lines:
        d.text((w // 2, ty), ln, font=sub_font, fill="#f7f2eb", anchor="mt")
        ty += line_h
    return out


def render_video_info_card(
    width: int,
    height: int,
    item: dict,
    details: Optional[dict] = None,
    scroll: int = 0,
    is_sub: bool = False,
    in_wl: bool = False,
    liked: bool = False,
    fonts: Optional[dict] = None,
) -> Image.Image:
    """Obsidian Ember full video info & scrollable description card."""
    ET = rj_theme.EMBER_THEME
    f = fonts or load_ui_fonts(width, height)
    wide = f["wide"]
    large = f["large"]
    mid = bool(f.get("mid", width >= 200 and height >= 200 and width < 300))
    short = bool(f.get("short", height <= 200))
    hdr_h, ftr_h = _yt_chrome_heights(width, height, f)
    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    det = details or {}
    ch_name = str(det.get("channel") or item.get("channel") or "YouTube")
    _draw_header(d, width, hdr_h, "VIDEO INFO", f, badge="SUBBED" if is_sub else "", right_text=ch_name)

    y0 = hdr_h + (8 if large else (5 if mid else 3))
    y1 = height - ftr_h - (6 if large else 3)
    px = 12 if large else (8 if mid else 6)
    pw = width - px * 2

    title = str(item.get("title") or "Untitled")
    max_t_lines = 1 if short else 2
    for ln in rj_theme.wrap_text(d, title, pw, f["title"])[:max_t_lines]:
        d.text((px, y0), ln, font=f["title"], fill=ET["text_bright"])
        y0 += 19 if large else 14

    meta_parts = [ch_name]
    vw = format_views(det.get("views") or item.get("views") or 0)
    if vw:
        meta_parts.append(vw)
    ud = str(det.get("upload_date") or item.get("upload_date") or "")
    if len(ud) == 8 and ud.isdigit():
        meta_parts.append(f"{ud[:4]}-{ud[4:6]}-{ud[6:]}")
    chaps = det.get("chapters") or []
    if chaps:
        meta_parts.append(f"{len(chaps)} chapters")
    d.text((px, y0 + 1), rj_theme.truncate_text(d, "  •  ".join(meta_parts), pw, f["sub"]), font=f["sub"], fill=ET["accent_bright"])
    y0 += 20 if large else 13

    # Status pills row
    pill_x = px
    for active, lbl in ((liked, "LIKED"), (in_wl, "WATCH LATER"), (is_sub, "SUBSCRIBED")):
        if active:
            pw_p, _ = rj_theme.draw_pill(d, pill_x, y0, lbl, f["badge"], bg=ET["select_bg"], fg=ET["ok"] if lbl == "SUBSCRIBED" else ET["accent_bright"], outline=ET["border_accent"], pad_x=4, pad_y=1, radius=3)
            pill_x += pw_p + 6
    if pill_x > px:
        y0 += 20 if large else 14

    y0 = min(y0, max(hdr_h + 2, y1 - 24))
    rj_theme.draw_card(d, (px, y0, width - px, y1), fill=ET["bg_surface"], outline=ET["border_subtle"], radius=6 if (wide or mid) else 3)
    desc = str(det.get("description") or "No description available for this video.")
    desc_lines: List[str] = []
    for para in desc.splitlines():
        if not para.strip():
            desc_lines.append("")
        else:
            desc_lines.extend(rj_theme.wrap_text(d, para.strip(), pw - 20, f["body"]))
    lh = 17 if large else (13 if (wide or mid) else 10)
    vis = max(1, (y1 - y0 - 10) // lh)
    st = max(0, min(scroll, max(0, len(desc_lines) - vis)))
    ty = y0 + 5
    for ln in desc_lines[st:st + vis]:
        if ln:
            d.text((px + 10, ty), ln, font=f["body"], fill=ET["text_primary"])
        ty += lh
    if len(desc_lines) > vis:
        rj_theme.draw_scrollbar(d, width - px - 6, y0 + 4, y1 - 4, st, vis, len(desc_lines), width=3)

    hint = format_footer_hint(
        width, height,
        hat_hint="U/D:Scr OK:Act K3:B",
        hat_240_hint="U/D:Scroll OK:Actions K3:Back",
        cardputer_hint="^v:Scr O:Act F:Sub W:WL D:DL Esc:B",
        hdmi_hint="^v:Scroll  O:Actions  F:Follow  U:Channel  W:Later  A:Playlist  L:Like  D:Download  Esc:Back",
    )
    _draw_footer(d, width, height, ftr_h, hint, f)
    return img


def render_video_osd(
    frame_img: Image.Image,
    title: str,
    elapsed: float,
    duration: int,
    volume_pct: int,
    muted: bool = False,
    paused: bool = False,
    quality_label: str = "720p",
    audio_label: str = "",
    liked: bool = False,
    toast: str = "",
    playlist_pos: str = "",
    fonts: Optional[dict] = None,
    buffered_frac: float = 0.0,
    buffer_badge: str = "",
    chapters: Optional[List[dict]] = None,
    speed: float = 1.0,
    loop_one: bool = False,
    captions_on: bool = False,
) -> Image.Image:
    """Composite a sleek translucent Obsidian Ember OSD over a video PIL frame (fast strip-only blend)."""
    ET = rj_theme.EMBER_THEME
    w, h = frame_img.size
    f = fonts or load_ui_fonts(w, h)
    wide = f["wide"]
    large = f["large"]
    mid = bool(f.get("mid", w >= 200 and h >= 200 and w < 300))
    short = bool(f.get("short", h <= 200))

    top_h = 30 if large else (20 if (wide and short) else (22 if (wide or mid) else 16))
    active_ch = get_active_chapter(chapters, elapsed) if chapters else None
    bot_h = (56 if active_ch else 44) if large else (30 if (wide and short) else (34 if (wide or mid) else 24))

    out = frame_img.copy() if frame_img.mode == "RGB" else frame_img.convert("RGB")
    top_dark = Image.new("RGB", (w, top_h), (14, 14, 16))
    out.paste(Image.blend(out.crop((0, 0, w, top_h)), top_dark, 0.80), (0, 0))
    bot_dark = Image.new("RGB", (w, bot_h), (14, 14, 16))
    out.paste(Image.blend(out.crop((0, h - bot_h, w, h)), bot_dark, 0.82), (0, h - bot_h))

    d = ImageDraw.Draw(out)
    d.line([(0, top_h), (w, top_h)], fill=(184, 94, 36), width=1)
    d.line([(0, h - bot_h), (w, h - bot_h)], fill=(184, 94, 36), width=1)

    tx = 8 if wide else 3
    if playlist_pos:
        pw, _ = rj_theme.draw_pill(
            d, tx, max(2, (top_h - (16 if wide else 11)) // 2),
            playlist_pos, f["badge"], bg=ET["select_bg"], fg=ET["accent_bright"],
            outline=ET["border_accent"], pad_x=4, pad_y=1, radius=3,
        )
        tx += pw + 6

    rx = w - (8 if wide else 3)
    if quality_label and wide:
        qw = d.textbbox((0, 0), quality_label, font=f["badge"])[2] + 8
        rj_theme.draw_pill(
            d, rx - qw, max(2, (top_h - 16) // 2), quality_label, f["badge"],
            bg=ET["bg_surface"], fg=ET["accent_bright"], outline=ET["border_subtle"],
            pad_x=4, pad_y=1, radius=3,
        )
        rx -= qw + 5
    if abs(speed - 1.0) > 0.01 and wide:
        sp_lbl = f"{speed:g}x"
        sw = d.textbbox((0, 0), sp_lbl, font=f["badge"])[2] + 8
        rj_theme.draw_pill(
            d, rx - sw, max(2, (top_h - 16) // 2), sp_lbl, f["badge"],
            bg=ET["bg_well"], fg=ET["ok"], outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3,
        )
        rx -= sw + 5
    if captions_on and wide:
        cw_p = d.textbbox((0, 0), "CC", font=f["badge"])[2] + 8
        rj_theme.draw_pill(
            d, rx - cw_p, max(2, (top_h - 16) // 2), "CC", f["badge"],
            bg=ET["select_bg"], fg=ET["accent_bright"], outline=ET["accent"], pad_x=4, pad_y=1, radius=3,
        )
        rx -= cw_p + 5
    if loop_one and wide:
        lp_w = d.textbbox((0, 0), "LOOP", font=f["badge"])[2] + 8
        rj_theme.draw_pill(
            d, rx - lp_w, max(2, (top_h - 16) // 2), "LOOP", f["badge"],
            bg=ET["bg_well"], fg=ET["ok"], outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3,
        )
        rx -= lp_w + 5
    if buffer_badge and wide:
        bw = d.textbbox((0, 0), buffer_badge, font=f["badge"])[2] + 8
        rj_theme.draw_pill(
            d, rx - bw, max(2, (top_h - 16) // 2), buffer_badge, f["badge"],
            bg=ET["bg_well"], fg=ET["ok"] if buffer_badge == "CACHED" else ET["text_primary"],
            outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3,
        )
        rx -= bw + 5
    if liked and wide:
        lw = d.textbbox((0, 0), "LIKED", font=f["badge"])[2] + 8
        rj_theme.draw_pill(
            d, rx - lw, max(2, (top_h - 16) // 2), "LIKED", f["badge"],
            bg="#3b1d12", fg=ET["accent_bright"], outline=ET["accent"],
            pad_x=4, pad_y=1, radius=3,
        )
        rx -= lw + 5
    if audio_label and large and (rx - tx) > 180:
        al = rj_theme.truncate_text(d, audio_label, 110, f["sub"])
        ab = d.textbbox((0, 0), al, font=f["sub"])
        d.text((rx - (ab[2] - ab[0]), (top_h - (ab[3] - ab[1])) // 2 - ab[1]), al, font=f["sub"], fill=ET["text_muted"])
        rx -= (ab[2] - ab[0]) + 10

    t_txt = rj_theme.truncate_text(d, title, max(40, rx - tx - 6), f["body_bold"])
    tb = d.textbbox((0, 0), t_txt, font=f["body_bold"])
    d.text((tx, (top_h - (tb[3] - tb[1])) // 2 - tb[1]), t_txt, font=f["body_bold"], fill=ET["text_bright"])

    if paused:
        pw_b, ph_b = (150, 44) if large else (104, 30)
        px0 = (w - pw_b) // 2
        py0 = (h - ph_b) // 2
        rj_theme.draw_card(d, (px0, py0, px0 + pw_b, py0 + ph_b), fill=ET["bg_elevated"], outline=ET["accent"], radius=8, width=2)
        d.text((w // 2, h // 2), "PAUSED", font=f["title"], fill=ET["accent_bright"], anchor="mm")
    elif toast:
        tw = min(w - 24, d.textbbox((0, 0), toast, font=f["body_bold"])[2] + 24)
        th = 28 if wide else 18
        px0 = (w - tw) // 2
        py0 = top_h + 10
        rj_theme.draw_card(d, (px0, py0, px0 + tw, py0 + th), fill=ET["bg_elevated"], outline=ET["border_accent"], radius=6)
        d.text((w // 2, py0 + th // 2), rj_theme.truncate_text(d, toast, tw - 12, f["body_bold"]), font=f["body_bold"], fill=ET["accent_bright"], anchor="mm")

    bar_x0 = 10 if wide else 4
    bar_x1 = w - (10 if wide else 4)
    if active_ch and large:
        ch_lbl = f"CH: {active_ch.get('title', '')}"
        d.text((bar_x0, h - bot_h + 4), rj_theme.truncate_text(d, ch_lbl, bar_x1 - bar_x0, f["sub"]), font=f["sub"], fill=ET["accent_bright"])
        bar_y = h - bot_h + 21
    else:
        bar_y = h - bot_h + (8 if large else 5)
    bar_h = 6 if large else 4
    prog = (min(1.0, max(0.0, elapsed / duration)) if duration > 0 else 0.0)
    buf_f = min(1.0, max(0.0, float(buffered_frac or 0.0)))
    bar_rad = max(1, bar_h // 2)
    d.rounded_rectangle((bar_x0, bar_y, bar_x1, bar_y + bar_h), radius=bar_rad, fill=ET["bg_well"])
    if buf_f > 0.0:
        bx_end = bar_x0 + max(bar_h, int((bar_x1 - bar_x0) * buf_f))
        d.rounded_rectangle((bar_x0, bar_y, min(bar_x1, bx_end), bar_y + bar_h), radius=bar_rad, fill=(112, 62, 32))
    if prog > 0.0:
        px_end = bar_x0 + max(bar_h, int((bar_x1 - bar_x0) * prog))
        d.rounded_rectangle((bar_x0, bar_y, min(bar_x1, px_end), bar_y + bar_h), radius=bar_rad, fill=ET["accent"])

    # Chapter tick markers along the progress bar
    if chapters and duration > 0:
        for ch in chapters:
            cs = float(ch.get("start", 0.0))
            if 1.0 < cs < duration - 1.0:
                cx = bar_x0 + int((bar_x1 - bar_x0) * (cs / duration))
                d.line([(cx, bar_y), (cx, bar_y + bar_h)], fill=ET["bg_base"], width=2)

    info_y = bar_y + bar_h + (6 if large else 3)
    state_str = "PAUSED" if paused else "PLAY"
    time_str = f"{state_str}  {format_duration(int(elapsed))}" + (f" / {format_duration(duration)}" if duration > 0 else "")
    d.text((bar_x0, info_y), time_str, font=f["sub"], fill=ET["text_primary"])

    vol_str = "MUTE" if muted else f"VOL {volume_pct}%"
    vb = d.textbbox((0, 0), vol_str, font=f["badge"])
    d.text((bar_x1 - (vb[2] - vb[0]), info_y), vol_str, font=f["badge"], fill=ET["warn"] if muted else ET["accent_bright"])

    if large:
        mid_hint = "Spc:Pause <>:Seek 0-9:Jump Z/X:Spd T:CC C:Chap V:Info R:Loop K:Snap"
        mb = d.textbbox((0, 0), mid_hint, font=f["sub"])
        if (mb[2] - mb[0]) < (w - 220):
            d.text((w // 2, info_y + 1), mid_hint, font=f["sub"], fill=ET["text_muted"], anchor="mt")

    return out


def render_music_dashboard(
    width: int,
    height: int,
    track: dict,
    idx: int,
    total: int,
    elapsed: float,
    volume_pct: int,
    muted: bool = False,
    paused: bool = False,
    liked: bool = False,
    shuffle: bool = False,
    loop_one: bool = False,
    audio_label: str = "System Audio",
    thumb_img: Optional[Image.Image] = None,
    toast: str = "",
    tick: float = 0.0,
    fonts: Optional[dict] = None,
) -> Image.Image:
    """Obsidian Ember Music Player dashboard with album art & warm equalizer bars."""
    ET = rj_theme.EMBER_THEME
    f = fonts or load_ui_fonts(width, height)
    wide = f["wide"]
    large = f["large"]
    mid = bool(f.get("mid", width >= 200 and height >= 200 and width < 300))
    short = bool(f.get("short", height <= 200))
    hdr_h, ftr_h = _yt_chrome_heights(width, height, f)

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    _draw_header(d, width, hdr_h, "MUSIC", f, badge=f"{idx + 1}/{max(1, total)}", right_text=audio_label, right_pill="MUTE" if muted else f"VOL {volume_pct}%")

    y0 = hdr_h + (10 if large else (5 if mid else 3))
    y1 = height - ftr_h - (8 if large else 3)

    if large:
        # Left artwork card, right metadata + animated warm equalizer + progress
        art_w = 200
        art_h = min(y1 - y0 - 16, 130)
        ax0, ay0 = 14, y0 + 4
        rj_theme.draw_card(d, (ax0, ay0, ax0 + art_w, ay0 + art_h), fill=ET["bg_surface"], outline=ET["border_accent"], radius=8)
        if thumb_img is not None:
            try:
                img.paste(thumb_img.resize((art_w - 4, art_h - 4), Image.BILINEAR), (ax0 + 2, ay0 + 2))
            except Exception:
                pass
        else:
            d.text((ax0 + art_w // 2, ay0 + art_h // 2), "AUDIO STREAM", font=f["badge"], fill=ET["accent_bright"], anchor="mm")

        # Animated warm-ember equalizer bars under artwork
        eq_y0 = ay0 + art_h + 12
        eq_y1 = min(y1 - 44, eq_y0 + 38)
        if eq_y1 - eq_y0 >= 14:
            n_bars = 16
            bar_gap = 4
            bw = max(4, (art_w - bar_gap * (n_bars - 1)) // n_bars)
            for b in range(n_bars):
                bx = ax0 + b * (bw + bar_gap)
                if paused or muted:
                    level = 0.14
                else:
                    level = 0.25 + 0.72 * abs(math.sin(tick * 3.8 + b * 0.65) * math.cos(tick * 1.9 - b * 0.35))
                bh = max(3, int((eq_y1 - eq_y0) * level))
                col = ET["accent_bright"] if level > 0.65 else ET["accent"]
                d.rounded_rectangle((bx, eq_y1 - bh, bx + bw, eq_y1), radius=2, fill=col)

        # Right panel: track details + status badges
        rx0 = ax0 + art_w + 18
        rw = width - rx0 - 14
        rj_theme.draw_card(d, (rx0, ay0, width - 14, eq_y1), fill=ET["bg_surface"], outline=ET["border_subtle"], radius=8, accent_left=ET["accent"], accent_width=3)
        ty = ay0 + 12
        # Status pills
        px = rx0 + 12
        st_lbl = "PAUSED" if paused else "NOW PLAYING"
        pw, _ = rj_theme.draw_pill(d, px, ty, st_lbl, f["badge"], bg=ET["select_bg"], fg=ET["accent_bright"], outline=ET["border_accent"], pad_x=6, pad_y=2, radius=4)
        px += pw + 6
        if liked:
            pw, _ = rj_theme.draw_pill(d, px, ty, "LIKED", f["badge"], bg="#3b1d12", fg=ET["accent_bright"], outline=ET["accent"], pad_x=5, pad_y=2, radius=4)
            px += pw + 6
        if shuffle:
            pw, _ = rj_theme.draw_pill(d, px, ty, "SHUFFLE", f["badge"], bg=ET["bg_well"], fg=ET["ok"], outline=ET["border_subtle"], pad_x=5, pad_y=2, radius=4)
            px += pw + 6
        if loop_one:
            rj_theme.draw_pill(d, px, ty, "LOOP", f["badge"], bg=ET["bg_well"], fg=ET["ok"], outline=ET["border_subtle"], pad_x=5, pad_y=2, radius=4)

        ty += 28
        # Wrap title across up to 2 lines
        words = str(track.get("title", "Untitled")).split()
        lines: List[str] = []
        cur = ""
        for word in words:
            cand = (cur + " " + word).strip()
            if d.textbbox((0, 0), cand, font=f["title"])[2] <= (rw - 24):
                cur = cand
            else:
                if cur:
                    lines.append(cur)
                cur = word
                if len(lines) >= 2:
                    break
        if cur and len(lines) < 2:
            lines.append(cur)
        for ln in lines:
            d.text((rx0 + 12, ty), rj_theme.truncate_text(d, ln, rw - 24, f["title"]), font=f["title"], fill=ET["text_bright"])
            ty += 20

        ch = str(track.get("channel", ""))
        if ch:
            d.text((rx0 + 12, ty + 4), rj_theme.truncate_text(d, ch, rw - 24, f["body"]), font=f["body"], fill=ET["text_muted"])

        if toast:
            d.text((rx0 + 12, eq_y1 - 24), rj_theme.truncate_text(d, toast, rw - 24, f["sub"]), font=f["sub"], fill=ET["accent_bright"])

        # Bottom progress bar spanning full width
        dur = int(track.get("duration") or 0)
        prog = min(1.0, max(0.0, elapsed / dur)) if dur > 0 else 0.0
        bar_y = y1 - 30
        rj_theme.draw_progress_bar(d, (14, bar_y, width - 14, bar_y + 8), prog)
        d.text((14, bar_y + 13), format_duration(int(elapsed)), font=f["sub"], fill=ET["text_primary"])
        d.text((width - 14, bar_y + 13), format_duration(dur), font=f["sub"], fill=ET["text_muted"], anchor="ra")
    else:
        # Compact / CardputerZero (320x170) / 240x240 mid-tier layout
        d.text((6, y0 + 2), rj_theme.truncate_text(d, str(track.get("title", "")), width - 12, f["body_bold"]), font=f["body_bold"], fill=ET["text_bright"])
        d.text((6, y0 + (16 if short else (18 if (wide or mid) else 14))), rj_theme.truncate_text(d, str(track.get("channel", "")), width - 12, f["sub"]), font=f["sub"], fill=ET["text_muted"])
        st_y = y0 + (30 if short else (34 if (wide or mid) else 26))
        st_lbl = "PAUSED" if paused else ("LIKED" if liked else "PLAYING")
        d.text((6, st_y), st_lbl, font=f["badge"], fill=ET["accent_bright"])
        if toast and (wide or mid):
            d.text((width - 6, st_y), rj_theme.truncate_text(d, toast, max(50, width - 80), f["sub"]), font=f["sub"], fill=ET["ok"], anchor="ra")
        dur = int(track.get("duration") or 0)
        prog = min(1.0, max(0.0, elapsed / dur)) if dur > 0 else 0.0
        bar_y = y1 - (22 if short else (26 if (wide or mid) else 18))
        # Animated warm-ember equalizer bars when vertical room allows (240x240 & 320x170)
        eq_y0 = st_y + 16
        eq_y1 = bar_y - 6
        if eq_y1 - eq_y0 >= 14:
            n_bars = 14 if (wide or mid) else 10
            bar_gap = 3
            bw = max(3, (width - 12 - bar_gap * (n_bars - 1)) // n_bars)
            for b in range(n_bars):
                bx = 6 + b * (bw + bar_gap)
                level = 0.14 if (paused or muted) else (0.25 + 0.70 * abs(math.sin(tick * 3.8 + b * 0.65) * math.cos(tick * 1.9 - b * 0.35)))
                bh = max(3, int((eq_y1 - eq_y0) * level))
                col = ET["accent_bright"] if level > 0.65 else ET["accent"]
                d.rounded_rectangle((bx, eq_y1 - bh, bx + bw, eq_y1), radius=2, fill=col)
        rj_theme.draw_progress_bar(d, (6, bar_y, width - 6, bar_y + 6), prog)
        d.text((6, bar_y + 9), format_duration(int(elapsed)), font=f["sub"], fill=ET["text_primary"])
        d.text((width - 6, bar_y + 9), format_duration(dur), font=f["sub"], fill=ET["text_muted"], anchor="ra")

    hint = format_footer_hint(
        width, height,
        hat_hint="OK:P <>:T ^v:V K1:Act K3:X",
        hat_240_hint="OK:Play <>:Trk ^v:V K1:Act K2:Set K3:X",
        cardputer_hint="Spc:P <>:Trk +/-:V O:Act S:Set Esc:Stop",
        hdmi_hint="Spc:Pause  <>:Prev/Next  ,/.:Seek  ^v:Vol  M:Mute  H:Shuf  R:Loop  L:Like  D:MP3  O:Act  Esc:Stop",
    )
    _draw_footer(d, width, height, ftr_h, hint, f)
    return img


# ---------------------------------------------------------------------------
# yt-dlp Network & Subprocess Helpers
# ---------------------------------------------------------------------------

def _kill_proc(proc: Optional[subprocess.Popen]) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except Exception:
                proc.kill()
                proc.wait(timeout=1.0)
    except Exception:
        pass


def check_internet() -> bool:
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", "2", "1.1.1.1"], capture_output=True, timeout=4)
        if r.returncode == 0:
            return True
    except Exception:
        pass
    try:
        with urllib.request.urlopen("https://www.youtube.com/generate_204", timeout=3):
            return True
    except Exception:
        return False


def check_deps() -> Tuple[bool, List[str]]:
    missing = []
    if not shutil.which("ffmpeg"):
        missing.append("ffmpeg")
    if not shutil.which("yt-dlp"):
        missing.append("yt-dlp")
    return (len(missing) == 0), missing


def _parse_yt_entry(data: dict) -> Optional[dict]:
    vid = str(data.get("id") or "")
    if not vid:
        return None
    thumb = str(data.get("thumbnail") or "")
    if not thumb:
        thumbs = data.get("thumbnails")
        if isinstance(thumbs, list) and thumbs:
            thumb = str(thumbs[-1].get("url") or "")
    ch_url = str(data.get("channel_url") or data.get("uploader_url") or "")
    ch_id = str(data.get("channel_id") or "")
    return {
        "id": vid,
        "title": str(data.get("title") or "Untitled")[:100],
        "channel": str(data.get("channel") or data.get("uploader") or "")[:40],
        "channel_url": ch_url,
        "channel_id": ch_id,
        "duration": int(data.get("duration") or 0),
        "views": int(data.get("view_count") or 0),
        "upload_date": str(data.get("upload_date") or ""),
        "thumb": thumb,
    }


def search_youtube(query: str, max_results: int = 12) -> Tuple[List[dict], str]:
    """Search YouTube via yt-dlp flat playlist. Returns (results, error_msg)."""
    try:
        r = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--no-download", "--no-warnings", "-j", f"ytsearch{max_results}:{query}"],
            capture_output=True, text=True, timeout=35,
        )
        if r.returncode != 0 and not r.stdout.strip():
            return [], (r.stderr or "Search failed").strip().splitlines()[-1][:48]
        results = []
        for line in r.stdout.splitlines():
            if not line.strip():
                continue
            try:
                entry = _parse_yt_entry(json.loads(line))
                if entry:
                    results.append(entry)
            except Exception:
                continue
        return results, ""
    except subprocess.TimeoutExpired:
        return [], "Search timed out"
    except Exception as exc:
        return [], str(exc)[:48]


def fetch_playlist_details(url: str, max_results: int = 100) -> Tuple[str, List[dict], str]:
    """Fetch a YouTube playlist's REAL title and video entries."""
    try:
        r = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--no-download", "--no-warnings", "--playlist-end", str(max_results), "-j", url],
            capture_output=True, text=True, timeout=55,
        )
        if r.returncode != 0 and not r.stdout.strip():
            return "", [], (r.stderr or "Playlist error").strip().splitlines()[-1][:48]
        raw_objs: List[dict] = []
        results: List[dict] = []
        for line in r.stdout.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                raw_objs.append(obj)
                entry = _parse_yt_entry(obj)
                if entry:
                    results.append(entry)
            except Exception:
                continue
        pl_title = extract_playlist_title(raw_objs, fallback="")
        if not pl_title:
            try:
                r2 = subprocess.run(
                    ["yt-dlp", "--flat-playlist", "--no-download", "--no-warnings", "--playlist-items", "1", "-J", url],
                    capture_output=True, text=True, timeout=15,
                )
                if r2.returncode == 0 and r2.stdout.strip():
                    pl_title = extract_playlist_title([json.loads(r2.stdout)], fallback="YouTube Playlist")
            except Exception:
                pl_title = "YouTube Playlist"
        return (pl_title or "YouTube Playlist"), results, ""
    except subprocess.TimeoutExpired:
        return "", [], "Playlist timed out"
    except Exception as exc:
        return "", [], str(exc)[:48]


def fetch_playlist(url: str, max_results: int = 30) -> Tuple[List[dict], str]:
    _title, items, err = fetch_playlist_details(url, max_results=max_results)
    return items, err


def sync_video_playlist(playlist_id: str, max_results: int = 100) -> Tuple[int, str]:
    pls = load_video_playlists()
    target = next((p for p in pls if p.get("id") == playlist_id), None)
    if target is None:
        return 0, "Playlist not found"
    src_url = str(target.get("source_url") or "").strip()
    if not src_url:
        return 0, "Not linked to a YouTube URL"
    _ttl, items, err = fetch_playlist_details(src_url, max_results=max_results)
    if err and not items:
        return 0, err
    existing = {x.get("id") for x in target.get("items", [])}
    added = 0
    for it in items:
        if it.get("id") not in existing:
            target.setdefault("items", []).append(it)
            existing.add(it.get("id"))
            added += 1
    if added:
        save_video_playlists(pls)
    return added, (f"Added {added} new videos" if added else "Playlist already up to date")


_VIDEO_DETAILS_CACHE: Dict[str, dict] = {}
_VIDEO_SUBS_CACHE: Dict[str, Tuple[List[Tuple[float, float, str]], str]] = {}


def fetch_video_details(video_id: str) -> dict:
    """Fetch chapters, full description, upload date & channel URL (cached per session)."""
    vid = str(video_id or "").strip()
    if not vid:
        return {}
    if vid in _VIDEO_DETAILS_CACHE:
        return _VIDEO_DETAILS_CACHE[vid]
    url = f"https://www.youtube.com/watch?v={vid}"
    info: dict = {"chapters": [], "description": "", "upload_date": "", "channel_url": "", "channel_id": "", "like_count": 0}
    try:
        r = subprocess.run(
            ["yt-dlp", "--no-download", "--no-warnings", "--no-playlist", "-j", url],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode == 0 and r.stdout.strip():
            raw = json.loads(r.stdout.splitlines()[0])
            chaps = []
            for ch in (raw.get("chapters") or []):
                if isinstance(ch, dict) and ch.get("title"):
                    chaps.append({
                        "start": float(ch.get("start_time") or 0.0),
                        "end": float(ch.get("end_time") or 0.0),
                        "title": str(ch.get("title") or "")[:64],
                    })
            info = {
                "chapters": chaps,
                "description": str(raw.get("description") or "")[:2400],
                "upload_date": str(raw.get("upload_date") or ""),
                "channel_url": str(raw.get("channel_url") or raw.get("uploader_url") or ""),
                "channel_id": str(raw.get("channel_id") or ""),
                "channel": str(raw.get("channel") or raw.get("uploader") or ""),
                "like_count": int(raw.get("like_count") or 0),
                "views": int(raw.get("view_count") or 0),
                "duration": int(raw.get("duration") or 0),
            }
            _VIDEO_DETAILS_CACHE[vid] = info
    except Exception:
        pass
    return info


def fetch_video_subtitles(video_id: str, lang: str = "en") -> Tuple[List[Tuple[float, float, str]], str, str]:
    """Download & parse English WebVTT subtitles (manual preferred, auto-generated fallback)."""
    vid = str(video_id or "").strip()
    if not vid:
        return [], "", "Invalid video ID"
    if vid in _VIDEO_SUBS_CACHE:
        cues, vp = _VIDEO_SUBS_CACHE[vid]
        return cues, vp, ("" if cues else "No captions available")
    if not shutil.which("yt-dlp"):
        return [], "", "yt-dlp not installed"
    sub_dir = os.path.join("/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else tempfile.gettempdir(), "rj_yt_subs")
    os.makedirs(sub_dir, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", vid)[:24]
    out_tpl = os.path.join(sub_dir, f"{safe_id}.%(ext)s")
    url = f"https://www.youtube.com/watch?v={vid}"
    try:
        subprocess.run(
            [
                "yt-dlp", "--skip-download", "--no-playlist", "--no-warnings",
                "--write-sub", "--write-auto-sub",
                "--sub-lang", f"{lang}.*,{lang}", "--sub-format", "vtt",
                "-o", out_tpl, url,
            ],
            capture_output=True, text=True, timeout=22,
        )
        vtt_files = sorted(
            [os.path.join(sub_dir, fn) for fn in os.listdir(sub_dir) if fn.startswith(safe_id + ".") and fn.endswith(".vtt")]
        )
        if not vtt_files:
            _VIDEO_SUBS_CACHE[vid] = ([], "")
            return [], "", "No English captions found"
        vtt_path = vtt_files[0]
        with open(vtt_path, "r", encoding="utf-8", errors="ignore") as handle:
            cues = parse_vtt_subtitles(handle.read())
        _VIDEO_SUBS_CACHE[vid] = (cues, vtt_path)
        return cues, vtt_path, ("" if cues else "Captions empty")
    except subprocess.TimeoutExpired:
        return [], "", "Captions timed out"
    except Exception as exc:
        return [], "", str(exc)[:44]


def fetch_channel_videos(channel_ref: str, max_results: int = 15) -> Tuple[List[dict], str]:
    """Fetch recent uploads for a YouTube channel URL, @handle, or UC... ID."""
    ref = (channel_ref or "").strip()
    if not ref:
        return [], "Missing channel reference"
    if not shutil.which("yt-dlp"):
        return [], "yt-dlp not installed"
    if ref.startswith(("http://", "https://")):
        url = ref.rstrip("/")
        if not url.endswith("/videos"):
            url += "/videos"
    elif ref.startswith("@"):
        url = f"https://www.youtube.com/{ref}/videos"
    elif ref.startswith("UC") and len(ref) >= 20:
        url = f"https://www.youtube.com/channel/{ref}/videos"
    else:
        return search_youtube(ref, max_results=max_results)
    try:
        r = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--no-download", "--no-warnings", "--playlist-end", str(max_results), "-j", url],
            capture_output=True, text=True, timeout=40,
        )
        if r.returncode != 0 and not r.stdout.strip():
            return [], (r.stderr or "Channel fetch failed").strip().splitlines()[-1][:48]
        items: List[dict] = []
        for line in r.stdout.splitlines():
            if not line.strip():
                continue
            try:
                entry = _parse_yt_entry(json.loads(line))
                if entry:
                    items.append(entry)
            except Exception:
                continue
        return items, ""
    except subprocess.TimeoutExpired:
        return [], "Channel lookup timed out"
    except Exception as exc:
        return [], str(exc)[:48]


def fetch_subscriptions_feed(subs: List[dict], per_channel: int = 5) -> List[dict]:
    """Fetch latest uploads from followed channels in parallel (bounded to 4 workers)."""
    if not subs:
        return []
    out_lock = threading.Lock()
    buckets: List[Tuple[int, List[dict]]] = []
    sem = threading.Semaphore(4)

    def _worker(idx: int, sub: dict) -> None:
        ref = sub.get("url") or sub.get("channel_id") or sub.get("name", "")
        with sem:
            vids, _err = fetch_channel_videos(ref, max_results=per_channel)
        for v in vids:
            if not v.get("channel"):
                v["channel"] = sub.get("name", "")
        with out_lock:
            buckets.append((idx, vids))

    threads = [threading.Thread(target=_worker, args=(i, s), daemon=True) for i, s in enumerate(subs[:12])]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30.0)

    # Interleave newest-per-channel cleanly (slot 0 from each channel, then slot 1, ...)
    buckets.sort(key=lambda b: b[0])
    feed: List[dict] = []
    seen = set()
    for slot in range(per_channel):
        for _i, vids in buckets:
            if slot < len(vids):
                vid = vids[slot]
                if vid.get("id") not in seen:
                    seen.add(vid.get("id"))
                    feed.append(vid)
    return feed


def fetch_video_meta(video_id: str) -> Optional[dict]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        r = subprocess.run(
            ["yt-dlp", "--no-download", "--no-warnings", "--no-playlist", "-j", url],
            capture_output=True, text=True, timeout=25,
        )
        if r.returncode == 0 and r.stdout.strip():
            return _parse_yt_entry(json.loads(r.stdout.splitlines()[0]))
    except Exception:
        pass
    return {"id": video_id, "title": video_id, "channel": "YouTube", "duration": 0, "views": 0, "thumb": ""}


_URL_CACHE_TTL = 1200.0  # YouTube signed URLs remain valid for ~5-6 hours; 20 min is always fresh
_URL_CACHE_LOCK = threading.Lock()
_URL_CACHE: Dict[Tuple[str, str], Tuple[float, str, str]] = {}
_AUDIO_URL_CACHE: Dict[str, Tuple[float, str]] = {}
_URL_PREFETCH_PENDING: set = set()


def resolve_stream_urls(video_id: str, quality_fmt: str, use_cache: bool = True) -> Tuple[str, str, str]:
    """Return (video_url, audio_url, error_msg), backed by an in-memory TTL cache."""
    cache_key = (str(video_id), str(quality_fmt))
    now = time.monotonic()
    if use_cache:
        with _URL_CACHE_LOCK:
            hit = _URL_CACHE.get(cache_key)
            if hit is not None and now < hit[0]:
                return hit[1], hit[2], ""

    url = f"https://www.youtube.com/watch?v={video_id}"
    for fmt in (quality_fmt, "best"):
        try:
            r = subprocess.run(
                ["yt-dlp", "-f", fmt, "--no-playlist", "--no-warnings", "--get-url", url],
                capture_output=True, text=True, timeout=45,
            )
            lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip().startswith("http")]
            if lines:
                v_url, a_url = lines[0], (lines[1] if len(lines) > 1 else "")
                with _URL_CACHE_LOCK:
                    _URL_CACHE[cache_key] = (time.monotonic() + _URL_CACHE_TTL, v_url, a_url)
                return v_url, a_url, ""
        except subprocess.TimeoutExpired:
            return "", "", "Stream lookup timed out"
        except Exception as exc:
            return "", "", str(exc)[:48]
    return "", "", "Could not resolve stream URL"


def prefetch_stream_urls_async(video_id: str, quality_fmt: str) -> None:
    """Resolve the next playlist track's URL in the background so track switching is instant."""
    if not video_id:
        return
    cache_key = (str(video_id), str(quality_fmt))
    now = time.monotonic()
    with _URL_CACHE_LOCK:
        hit = _URL_CACHE.get(cache_key)
        if hit is not None and now < hit[0]:
            return
        if cache_key in _URL_PREFETCH_PENDING:
            return
        _URL_PREFETCH_PENDING.add(cache_key)

    def _worker() -> None:
        try:
            resolve_stream_urls(video_id, quality_fmt, use_cache=True)
        except Exception:
            pass
        finally:
            with _URL_CACHE_LOCK:
                _URL_PREFETCH_PENDING.discard(cache_key)

    threading.Thread(target=_worker, daemon=True).start()


def resolve_audio_url(video_id: str, use_cache: bool = True) -> Tuple[str, str]:
    now = time.monotonic()
    if use_cache:
        with _URL_CACHE_LOCK:
            hit = _AUDIO_URL_CACHE.get(video_id)
            if hit is not None and now < hit[0]:
                return hit[1], ""

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        r = subprocess.run(
            ["yt-dlp", "-f", "bestaudio/best", "--no-playlist", "--no-warnings", "--get-url", url],
            capture_output=True, text=True, timeout=40,
        )
        lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip().startswith("http")]
        if lines:
            with _URL_CACHE_LOCK:
                _AUDIO_URL_CACHE[video_id] = (time.monotonic() + _URL_CACHE_TTL, lines[0])
            return lines[0], ""
        return "", (r.stderr or "No audio stream").strip()[:48]
    except subprocess.TimeoutExpired:
        return "", "Audio lookup timed out"
    except Exception as exc:
        return "", str(exc)[:48]


# ---------------------------------------------------------------------------
# Anti-Throttle Chunked HTTP Range Pump & Progressive Matroska Stream Preloader
# ---------------------------------------------------------------------------

def _is_hls_or_manifest(url: str) -> bool:
    low = (url or "").lower()
    return (".m3u8" in low) or ("manifest.googlevideo.com" in low) or ("/hls_playlist/" in low)


def _parse_content_range_total(header_val: str) -> int:
    if not header_val:
        return 0
    m = re.search(r"/(\d+)\s*$", str(header_val).strip())
    return int(m.group(1)) if m else 0


def _parse_http_range_header(rng_hdr: str, total: int = 0) -> Tuple[int, int, bool]:
    """Parse RFC 7233 single byte range header -> (start, end_inclusive_or_-1, is_range)."""
    raw = (rng_hdr or "").strip()
    if not raw.lower().startswith("bytes="):
        return 0, (total - 1 if total > 0 else -1), False
    spec = raw[6:].split(",")[0].strip()
    if "-" not in spec:
        return 0, (total - 1 if total > 0 else -1), False
    lo_s, hi_s = spec.split("-", 1)
    lo_s, hi_s = lo_s.strip(), hi_s.strip()
    try:
        if not lo_s and hi_s:
            # Suffix range: bytes=-N (last N bytes, e.g. end-of-file MP4 moov atom)
            suf = int(hi_s)
            if total > 0 and suf > 0:
                return max(0, total - suf), total - 1, True
            return 0, (total - 1 if total > 0 else -1), True
        lo = max(0, int(lo_s)) if lo_s else 0
        if hi_s:
            hi = int(hi_s)
            if total > 0:
                hi = min(total - 1, hi)
        else:
            hi = (total - 1) if total > 0 else -1
        return lo, hi, True
    except ValueError:
        return 0, (total - 1 if total > 0 else -1), False


def should_use_range_proxy(
    duration: int,
    start_offset: float = 0.0,
    preload_sec: int = 5,
    free_cache_bytes: Optional[int] = None,
    video_url: str = "",
    audio_url: str = "",
) -> bool:
    """Decide whether to stream through the O(1)-RAM anti-throttle Range proxy vs full-file StreamPreloader.

    Uses the seekable local HTTP Range proxy whenever:
      - Video is long (>15 min) or has unknown duration (0), preventing /dev/shm RAM exhaustion
        and non-seekable FIFO `moov` atom stalls on Raspberry Pi.
      - Playback starts/seeks mid-video (`start_offset > 0.1`), keeping seeks anti-throttled & <1.5s.
      - User chose `preload_sec == 0` (still defeats YouTube single-GET throttling!).
      - Free space in the preload cache directory is below 300 MB.
    Returns False for local files, live streams (`duration < 0`), and HLS manifests.
    """
    v_u = str(video_url or "")
    a_u = str(audio_url or "")
    if not v_u.startswith(("http://", "https://")):
        return False
    if _is_hls_or_manifest(v_u) or (a_u and _is_hls_or_manifest(a_u)):
        return False
    dur = int(duration or 0)
    if dur < 0:
        return False  # Live stream
    if int(preload_sec) == 0:
        return True
    if dur == 0 or dur > LONG_VIDEO_THRESHOLD_SEC:
        return True
    if float(start_offset or 0.0) > 0.1:
        return True
    if free_cache_bytes is None:
        try:
            free_cache_bytes = int(shutil.disk_usage(get_preload_cache_dir()).free)
        except OSError:
            free_cache_bytes = 0
    if int(free_cache_bytes) < MIN_PRELOAD_FREE_BYTES:
        return True
    return False


class _RangeProxyHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 16


class _ChunkedRangeProxy:
    """Local seekable HTTP Range proxy that defeats YouTube single-GET throttling in O(1) RAM.

    Translates unbounded/open-ended `ffmpeg` or `mpv` reads into bounded 2 MB upstream
    `Range: bytes=start-end` requests with a small bounded (~6 MB) background prefetch
    queue and natural TCP backpressure. Supports arbitrary HTTP `Range` seeks (including
    end-of-file MP4 `moov` atoms and WebM `Cues`) so 1-to-10-hour videos play smoothly
    on Raspberry Pi without ever writing to `/dev/shm`!
    """

    def __init__(
        self,
        video_url: str,
        audio_url: str = "",
        chunk_size: int = 2 * 1024 * 1024,
        prefetch_chunks: int = 3,
        timeout: float = 15.0,
    ):
        self.video_url = str(video_url or "")
        self.audio_url = str(audio_url or "")
        self.chunk_size = max(16 * 1024, int(chunk_size))
        self.prefetch_chunks = max(1, min(6, int(prefetch_chunks)))
        self.timeout = float(timeout)

        self.stop_event = threading.Event()
        self.port: int = 0
        self.video_proxy_url: str = ""
        self.audio_proxy_url: str = ""
        self.bytes_served: int = 0
        self.upstream_range_requests: int = 0
        self._started_at: float = 0.0
        self._lock = threading.Lock()
        self._meta: Dict[str, dict] = {}
        self._small_cache: "collections.OrderedDict[Tuple[str, int, int], bytes]" = collections.OrderedDict()
        self._head_cache: Dict[str, bytes] = {}
        self.warmed_bytes: int = 0
        self.warm_target_bytes: int = 0
        self.warm_done = threading.Event()
        self.warm_failed: bool = False
        self._server: Optional[_RangeProxyHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def _stream_url_for_path(self, path: str) -> str:
        clean = (path or "").split("?", 1)[0]
        if clean == "/v":
            return self.video_url
        if clean == "/a":
            return self.audio_url
        return ""

    def _probe_stream(self, path: str, upstream_url: str) -> dict:
        with self._lock:
            cached = self._meta.get(path)
            if cached is not None:
                return cached
        info = {"total": 0, "ctype": "application/octet-stream", "accept_ranges": True}
        for attempt in range(3):
            if self.stop_event.is_set():
                return info
            req = urllib.request.Request(
                upstream_url,
                headers={
                    "User-Agent": _YT_USER_AGENT,
                    "Accept": "*/*",
                    "Range": "bytes=0-0",
                },
            )
            try:
                resp = urllib.request.urlopen(req, timeout=self.timeout)
                try:
                    status = int(getattr(resp, "status", 200) or 200)
                    hdrs = resp.headers
                    ct = hdrs.get("Content-Type", "")
                    if ct:
                        info["ctype"] = ct.split(";", 1)[0].strip()
                    if status == 206:
                        tot = _parse_content_range_total(hdrs.get("Content-Range", ""))
                        if tot > 0:
                            info["total"] = tot
                        info["accept_ranges"] = True
                        try:
                            resp.read(1)
                        except Exception:
                            pass
                    else:
                        cl = hdrs.get("Content-Length", "")
                        if cl and str(cl).isdigit():
                            info["total"] = int(cl)
                        info["accept_ranges"] = (hdrs.get("Accept-Ranges", "").lower() == "bytes")
                    break
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass
            except urllib.error.HTTPError as he:
                code = he.code
                try:
                    he.close()
                except Exception:
                    pass
                if code in (400, 401, 403, 404, 410):
                    break
            except Exception:
                pass
            if self.stop_event.wait(0.2 * (attempt + 1)):
                break
        with self._lock:
            self._meta[path] = info
        return info

    def _fetch_exact_range(
        self, path: str, upstream_url: str, lo: int, hi: int, req_stop: threading.Event
    ) -> Tuple[Optional[bytes], int, str]:
        with self._lock:
            head_b = self._head_cache.get(path)
            if head_b is not None and hi >= lo and hi < len(head_b):
                meta = self._meta.get(path, {})
                return head_b[lo : hi + 1], int(meta.get("total", 0)), str(meta.get("ctype", "application/octet-stream"))
        cache_key = (path, lo, hi)
        want_len = (hi - lo + 1) if hi >= lo else 0
        if 0 < want_len <= 256 * 1024:
            with self._lock:
                hit = self._small_cache.get(cache_key)
                if hit is not None:
                    self._small_cache.move_to_end(cache_key)
                    meta = self._meta.get(path, {})
                    return hit, int(meta.get("total", 0)), str(meta.get("ctype", "application/octet-stream"))

        for attempt in range(3):
            if self.stop_event.is_set() or req_stop.is_set():
                return None, 0, ""
            req = urllib.request.Request(
                upstream_url,
                headers={
                    "User-Agent": _YT_USER_AGENT,
                    "Accept": "*/*",
                    "Range": f"bytes={lo}-{hi}",
                },
            )
            try:
                resp = urllib.request.urlopen(req, timeout=self.timeout)
                try:
                    status = int(getattr(resp, "status", 200) or 200)
                    hdrs = resp.headers
                    ctype = (hdrs.get("Content-Type", "") or "application/octet-stream").split(";", 1)[0].strip()
                    tot = _parse_content_range_total(hdrs.get("Content-Range", "")) if status == 206 else 0
                    if status == 200:
                        cl = hdrs.get("Content-Length", "")
                        if cl and str(cl).isdigit():
                            tot = int(cl)
                    chunks: List[bytes] = []
                    got = 0
                    while not self.stop_event.is_set() and not req_stop.is_set():
                        part = resp.read(65536)
                        if not part:
                            break
                        chunks.append(part)
                        got += len(part)
                        if status == 200 and want_len > 0 and got >= (hi + 1):
                            break
                    blob = b"".join(chunks)
                    if status == 200:
                        blob = blob[lo : hi + 1]
                    with self._lock:
                        self.upstream_range_requests += 1
                        if tot > 0 and path in self._meta and self._meta[path]["total"] <= 0:
                            self._meta[path]["total"] = tot
                        if 0 < len(blob) <= 256 * 1024:
                            self._small_cache[cache_key] = blob
                            self._small_cache.move_to_end(cache_key)
                            while len(self._small_cache) > 12:
                                self._small_cache.popitem(last=False)
                    return blob, tot, ctype
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass
            except urllib.error.HTTPError as he:
                code = he.code
                try:
                    he.close()
                except Exception:
                    pass
                if code in (400, 401, 403, 404, 410, 416):
                    return None, 0, ""
            except Exception:
                pass
            if self.stop_event.wait(0.25 * (attempt + 1)):
                return None, 0, ""
        return None, 0, ""

    def _stream_sequential(
        self,
        handler: http.server.BaseHTTPRequestHandler,
        path: str,
        upstream_url: str,
        lo: int,
        end_byte: int,
        total: int,
        is_range: bool,
        ctype: str,
    ) -> None:
        target_end = end_byte if end_byte >= lo else ((total - 1) if total > 0 else -1)
        if is_range and target_end >= lo:
            handler.send_response(206)
            tot_lbl = str(total) if total > 0 else "*"
            handler.send_header("Content-Range", f"bytes {lo}-{target_end}/{tot_lbl}")
            handler.send_header("Content-Length", str(target_end - lo + 1))
        else:
            handler.send_response(200)
            if total > 0 and lo == 0:
                handler.send_header("Content-Length", str(total))
        handler.send_header("Content-Type", ctype or "application/octet-stream")
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Connection", "close")
        handler.end_headers()

        # Bounded RAM prefetch queue (~6 MB max per stream) with automatic TCP backpressure
        q_slots = max(8, min(128, (self.prefetch_chunks * self.chunk_size) // 65536))
        buf_q: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=q_slots)
        req_stop = threading.Event()
        first_chunk_sz = min(self.chunk_size, 512 * 1024)

        def _put_piece(piece: bytes) -> bool:
            while not self.stop_event.is_set() and not req_stop.is_set():
                try:
                    buf_q.put(piece, timeout=0.1)
                    return True
                except queue.Full:
                    continue
            return False

        def _producer() -> None:
            pos = lo
            chunk_idx = 0
            try:
                # Serve pre-warmed RAM head buffer immediately at 0ms latency before hitting upstream
                with self._lock:
                    head_b = self._head_cache.get(path)
                if head_b is not None and pos < len(head_b):
                    slice_end = len(head_b)
                    if target_end >= 0:
                        slice_end = min(slice_end, target_end + 1)
                    mv_h = memoryview(head_b)
                    while pos < slice_end and not self.stop_event.is_set() and not req_stop.is_set():
                        nxt_p = min(slice_end, pos + 65536)
                        if not _put_piece(bytes(mv_h[pos:nxt_p])):
                            return
                        pos = nxt_p
                    if target_end >= 0 and pos > target_end:
                        return
                    if total > 0 and pos >= total:
                        return
                    chunk_idx = 1

                while not self.stop_event.is_set() and not req_stop.is_set():
                    if target_end >= 0 and pos > target_end:
                        return
                    step = first_chunk_sz if chunk_idx == 0 else self.chunk_size
                    req_hi = pos + step - 1
                    if target_end >= 0:
                        req_hi = min(req_hi, target_end)

                    resp = None
                    for attempt in range(3):
                        if self.stop_event.is_set() or req_stop.is_set():
                            return
                        req = urllib.request.Request(
                            upstream_url,
                            headers={
                                "User-Agent": _YT_USER_AGENT,
                                "Accept": "*/*",
                                "Range": f"bytes={pos}-{req_hi}",
                            },
                        )
                        try:
                            resp = urllib.request.urlopen(req, timeout=self.timeout)
                            break
                        except urllib.error.HTTPError as he:
                            code = he.code
                            try:
                                he.close()
                            except Exception:
                                pass
                            if code == 416 and pos > lo:
                                return
                            if code in (400, 401, 403, 404, 410):
                                return
                        except Exception:
                            pass
                        if self.stop_event.wait(0.25 * (attempt + 1)):
                            return

                    if resp is None:
                        return

                    chunk_got = 0
                    try:
                        status = int(getattr(resp, "status", 200) or 200)
                        if status == 206:
                            with self._lock:
                                self.upstream_range_requests += 1
                            cr_tot = _parse_content_range_total(resp.headers.get("Content-Range", ""))
                            if cr_tot > 0 and target_end < 0:
                                with self._lock:
                                    if path in self._meta and self._meta[path]["total"] <= 0:
                                        self._meta[path]["total"] = cr_tot
                            while not self.stop_event.is_set() and not req_stop.is_set():
                                part = resp.read(65536)
                                if not part:
                                    break
                                if target_end >= 0 and (pos + len(part) - 1) > target_end:
                                    part = part[: (target_end - pos + 1)]
                                if not part:
                                    break
                                if not _put_piece(part):
                                    return
                                n_len = len(part)
                                pos += n_len
                                chunk_got += n_len
                                if target_end >= 0 and pos > target_end:
                                    return
                            if chunk_got == 0:
                                return
                            if cr_tot > 0 and pos >= cr_tot:
                                return
                        else:
                            # Upstream ignored Range and returned 200 full stream
                            if pos > 0:
                                return
                            while not self.stop_event.is_set() and not req_stop.is_set():
                                part = resp.read(65536)
                                if not part:
                                    break
                                if target_end >= 0 and (pos + len(part) - 1) > target_end:
                                    part = part[: (target_end - pos + 1)]
                                if not part:
                                    break
                                if not _put_piece(part):
                                    return
                                pos += len(part)
                                if target_end >= 0 and pos > target_end:
                                    return
                            return
                    except Exception:
                        # Network drop mid-chunk: retry seamlessly from exact queued `pos`
                        if chunk_got == 0:
                            time.sleep(0.2)
                    finally:
                        try:
                            resp.close()
                        except Exception:
                            pass
                    chunk_idx += 1
            finally:
                while not req_stop.is_set() and not self.stop_event.is_set():
                    try:
                        buf_q.put(None, timeout=0.1)
                        break
                    except queue.Full:
                        continue

        prod_t = threading.Thread(target=_producer, daemon=True)
        prod_t.start()
        try:
            while not self.stop_event.is_set():
                try:
                    piece = buf_q.get(timeout=0.2)
                except queue.Empty:
                    if not prod_t.is_alive():
                        break
                    continue
                if piece is None:
                    break
                try:
                    handler.wfile.write(piece)
                    with self._lock:
                        self.bytes_served += len(piece)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        finally:
            req_stop.set()
            try:
                while True:
                    buf_q.get_nowait()
            except queue.Empty:
                pass
            prod_t.join(timeout=1.0)

    def start(self) -> "_ChunkedRangeProxy":
        proxy = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def do_HEAD(self):
                self.close_connection = True
                path = (self.path or "").split("?", 1)[0]
                upstream = proxy._stream_url_for_path(path)
                if not upstream:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return
                meta = proxy._probe_stream(path, upstream)
                self.send_response(200)
                self.send_header("Content-Type", meta["ctype"])
                self.send_header("Accept-Ranges", "bytes")
                if meta["total"] > 0:
                    self.send_header("Content-Length", str(meta["total"]))
                self.send_header("Connection", "close")
                self.end_headers()

            def do_GET(self):
                self.close_connection = True
                path = (self.path or "").split("?", 1)[0]
                upstream = proxy._stream_url_for_path(path)
                if not upstream:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return
                meta = proxy._probe_stream(path, upstream)
                total = int(meta.get("total") or 0)
                ctype = str(meta.get("ctype") or "application/octet-stream")
                lo, hi, is_range = _parse_http_range_header(self.headers.get("Range", ""), total=total)
                if total > 0 and lo >= total:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return

                # Small bounded random-access Range (e.g. MP4 header / end-of-file moov / WebM Cues)
                small_limit = min(proxy.chunk_size, 512 * 1024)
                if is_range and hi >= lo and (hi - lo + 1) <= small_limit:
                    req_stop = threading.Event()
                    blob, tot_r, ct_r = proxy._fetch_exact_range(path, upstream, lo, hi, req_stop)
                    if blob is None:
                        self.send_response(502)
                        self.send_header("Content-Length", "0")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        return
                    actual_tot = tot_r or total
                    actual_hi = lo + len(blob) - 1
                    self.send_response(206)
                    self.send_header("Content-Type", ct_r or ctype)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes {lo}-{actual_hi}/{actual_tot if actual_tot > 0 else '*'}")
                    self.send_header("Content-Length", str(len(blob)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        self.wfile.write(blob)
                        with proxy._lock:
                            proxy.bytes_served += len(blob)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    return

                # Open-ended or large sequential stream -> 2 MB anti-throttle Range chunks + bounded prefetch
                proxy._stream_sequential(self, path, upstream, lo, hi, total, is_range, ctype)

        self._started_at = time.monotonic()
        self._server = _RangeProxyHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._server.server_address[1])
        self.video_proxy_url = f"http://127.0.0.1:{self.port}/v" if self.video_url else ""
        self.audio_proxy_url = f"http://127.0.0.1:{self.port}/a" if self.audio_url else ""
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()
        return self

    def start_warm_buffer(self, target_bytes: int = 2 * 1024 * 1024) -> None:
        """Pre-buffer bounded head chunks of /v (and /a) in RAM so long streams start smoothly."""
        v_cap = max(256 * 1024, min(4 * 1024 * 1024, int(target_bytes)))
        a_cap = max(128 * 1024, min(1024 * 1024, v_cap // 3)) if self.audio_url else 0
        self.warm_target_bytes = v_cap + a_cap
        self.warmed_bytes = 0
        self.warm_failed = False
        self.warm_done.clear()

        def _warm_one(path: str, upstream_url: str, cap: int) -> bool:
            if not upstream_url or cap <= 0 or self.stop_event.is_set():
                return True
            meta = self._probe_stream(path, upstream_url)
            total = int(meta.get("total") or 0)
            goal = min(cap, total) if total > 0 else cap
            buf = bytearray()
            pos = 0
            step = min(self.chunk_size, 1024 * 1024)
            while pos < goal and not self.stop_event.is_set():
                req_hi = min(goal - 1, pos + step - 1)
                req = urllib.request.Request(
                    upstream_url,
                    headers={
                        "User-Agent": _YT_USER_AGENT,
                        "Accept": "*/*",
                        "Range": f"bytes={pos}-{req_hi}",
                    },
                )
                resp = None
                for attempt in range(2):
                    if self.stop_event.is_set():
                        return False
                    try:
                        resp = urllib.request.urlopen(req, timeout=self.timeout)
                        break
                    except urllib.error.HTTPError as he:
                        code = he.code
                        try:
                            he.close()
                        except Exception:
                            pass
                        if code == 416 and pos > 0:
                            break
                        if code in (400, 401, 403, 404, 410):
                            return False
                    except Exception:
                        pass
                    if self.stop_event.wait(0.2 * (attempt + 1)):
                        return False
                if resp is None:
                    break
                got_chunk = 0
                try:
                    status = int(getattr(resp, "status", 200) or 200)
                    while pos < goal and not self.stop_event.is_set():
                        part = resp.read(65536)
                        if not part:
                            break
                        need = goal - pos
                        if len(part) > need:
                            part = part[:need]
                        buf.extend(part)
                        n_len = len(part)
                        pos += n_len
                        got_chunk += n_len
                        with self._lock:
                            self.warmed_bytes += n_len
                    if status == 206:
                        with self._lock:
                            self.upstream_range_requests += 1
                    else:
                        break
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass
                if got_chunk == 0:
                    break
            if buf:
                with self._lock:
                    self._head_cache[path] = bytes(buf)
                return True
            return False

        def _worker() -> None:
            try:
                ok_v = _warm_one("/v", self.video_url, v_cap)
                if self.audio_url and not self.stop_event.is_set():
                    _warm_one("/a", self.audio_url, a_cap)
                if not ok_v:
                    self.warm_failed = True
            except Exception:
                self.warm_failed = True
            finally:
                self.warm_done.set()

        threading.Thread(target=_worker, daemon=True).start()

    def speed_kbps(self) -> float:
        dt = max(0.1, time.monotonic() - self._started_at)
        return (max(self.bytes_served, self.warmed_bytes) / 1024.0) / dt

    def stop(self) -> None:
        self.stop_event.set()
        srv = self._server
        self._server = None
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass
        t = self._thread
        self._thread = None
        if t is not None and t.is_alive():
            t.join(timeout=1.5)


class _ChunkedHttpPump:
    """Downloads an HTTP media URL in bounded Range chunks into a POSIX FIFO.

    YouTube's googlevideo.com servers aggressively throttle single unbounded HTTP GET
    requests (down to ~40-80 KB/s), causing constant stutter even at 144p. Requesting
    bounded 2 MB `Range: bytes=start-end` chunks (matching official clients & yt-dlp)
    bypasses single-GET throttling and saturates Wi-Fi so the local RAM buffer stays
    tens of seconds ahead of playback.
    """

    def __init__(
        self,
        url: str,
        fifo_path: str,
        stop_event: threading.Event,
        remux_proc_ref: list,
        chunk_size: int = 2 * 1024 * 1024,
        timeout: float = 12.0,
        cache_dir: str = "",
        min_free_bytes: int = MIN_SHM_SAFETY_FREE_BYTES,
    ):
        self.url = url
        self.fifo_path = fifo_path
        self.stop_event = stop_event
        self.remux_proc_ref = remux_proc_ref
        self.chunk_size = max(16 * 1024, int(chunk_size))
        self.timeout = float(timeout)
        self.cache_dir = str(cache_dir or "")
        self.min_free_bytes = max(0, int(min_free_bytes))
        self.downloaded_bytes = 0
        self.total_bytes = 0
        self.range_requests = 0
        self.done = False
        self.failed = False
        self.error = ""

    def _open_fifo_writer(self) -> Optional[int]:
        """Open FIFO write-end without ever hanging if ffmpeg exits or user cancels."""
        while not self.stop_event.is_set():
            proc = self.remux_proc_ref[0] if self.remux_proc_ref else None
            if proc is not None and proc.poll() is not None:
                return None
            try:
                fd = os.open(self.fifo_path, os.O_WRONLY | os.O_NONBLOCK)
                flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
                try:
                    fcntl.fcntl(fd, 1031, 1024 * 1024)  # F_SETPIPE_SZ
                except OSError:
                    pass
                return fd
            except OSError as exc:
                if exc.errno in (errno.ENXIO, errno.ENOENT):
                    time.sleep(0.02)
                    continue
                self.failed = True
                self.error = str(exc)[:64]
                return None
        return None

    def _write_all(self, fd: int, data: bytes) -> bool:
        mv = memoryview(data)
        total = len(mv)
        written = 0
        while written < total:
            if self.stop_event.is_set():
                return False
            try:
                n = os.write(fd, mv[written:])
                if n <= 0:
                    return False
                written += n
            except BrokenPipeError:
                return False
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                return False
        return True

    def run(self) -> None:
        fd = self._open_fifo_writer()
        if fd is None:
            return
        pos = 0
        next_space_check = 4 * 1024 * 1024
        try:
            while not self.stop_event.is_set():
                if self.cache_dir and self.min_free_bytes > 0 and pos >= next_space_check:
                    next_space_check = pos + 4 * 1024 * 1024
                    try:
                        if shutil.disk_usage(self.cache_dir).free < self.min_free_bytes:
                            self.failed = True
                            self.error = "Cache space low"
                            return
                    except OSError:
                        pass
                end = pos + self.chunk_size - 1
                req = urllib.request.Request(
                    self.url,
                    headers={
                        "User-Agent": _YT_USER_AGENT,
                        "Accept": "*/*",
                        "Range": f"bytes={pos}-{end}",
                    },
                )
                resp = None
                last_err = ""
                for attempt in range(3):
                    if self.stop_event.is_set():
                        return
                    try:
                        resp = urllib.request.urlopen(req, timeout=self.timeout)
                        break
                    except urllib.error.HTTPError as he:
                        code = he.code
                        try:
                            he.close()
                        except Exception:
                            pass
                        if code == 416 and pos > 0:
                            self.done = True
                            return
                        if code in (400, 401, 403, 404, 410):
                            self.failed = True
                            self.error = f"HTTP {code}"
                            return
                        last_err = f"HTTP {code}"
                    except Exception as exc:
                        last_err = str(exc)[:64]
                    time.sleep(0.25 * (attempt + 1))

                if resp is None:
                    self.failed = True
                    self.error = last_err or "HTTP fetch failed"
                    return

                try:
                    status = int(getattr(resp, "status", 200) or 200)
                    headers = resp.headers
                    if status == 206:
                        self.range_requests += 1
                        cr_tot = _parse_content_range_total(headers.get("Content-Range", ""))
                        if cr_tot > 0:
                            self.total_bytes = cr_tot
                        chunk_got = 0
                        while not self.stop_event.is_set():
                            piece = resp.read(65536)
                            if not piece:
                                break
                            if not self._write_all(fd, piece):
                                return
                            n_len = len(piece)
                            pos += n_len
                            chunk_got += n_len
                            self.downloaded_bytes = pos
                        if (self.total_bytes > 0 and pos >= self.total_bytes) or chunk_got == 0:
                            self.done = True
                            return
                    else:
                        # Server returned 200 (full body, ignored Range): stream until EOF once
                        cl = headers.get("Content-Length", "")
                        if cl and str(cl).isdigit():
                            self.total_bytes = int(cl)
                        while not self.stop_event.is_set():
                            piece = resp.read(65536)
                            if not piece:
                                break
                            if not self._write_all(fd, piece):
                                return
                            n_len = len(piece)
                            pos += n_len
                            self.downloaded_bytes = pos
                        self.done = True
                        return
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


class StreamPreloader:
    """Progressively preloads & stream-copies YouTube video+audio into a local Matroska (.mkv) cache.

    Advantages over direct ffmpeg HTTP streaming:
      1. Defeats YouTube single-GET throttling via 2 MB HTTP Range chunks.
      2. Remuxes split video+audio streams (`-c copy -flush_packets 1 -f matroska`, <1% CPU)
         into a single RAM-cached `.mkv` file in `/dev/shm/rj_yt_preload`.
      3. Playback can start after only 3s / 5s / 10s (or Full Video) via tail-feeding `pipe:0`
         while the rest of the video downloads at full Wi-Fi speed in the background.
      4. Seeking inside a completed `.mkv` takes <150ms locally with zero network roundtrips!
    """

    def __init__(
        self,
        video_id: str,
        video_url: str,
        audio_url: str = "",
        quality_label: str = "360p",
        duration: int = 0,
        cache_dir: Optional[str] = None,
        chunk_size: int = 2 * 1024 * 1024,
        start_offset: float = 0.0,
    ):
        self.video_id = str(video_id or "vid")
        self.video_url = str(video_url or "")
        self.audio_url = str(audio_url or "")
        self.quality_label = str(quality_label or "360p")
        self.duration = int(duration or 0)
        self.base_offset = max(0.0, float(start_offset or 0.0))
        self.remaining_duration = max(0, int(self.duration - self.base_offset)) if self.duration > 0 else 0
        self.key = (self.video_id, self.quality_label)
        self.cache_dir = cache_dir or get_preload_cache_dir()
        self.chunk_size = int(chunk_size)

        safe_id = re.sub(r"[^A-Za-z0-9_-]", "", self.video_id)[:20] or "vid"
        safe_q = re.sub(r"[^A-Za-z0-9_-]", "", self.quality_label)[:10] or "q"
        stamp = int(time.monotonic() * 1000) % 1_000_000
        os.makedirs(self.cache_dir, exist_ok=True)
        self.mkv_path = os.path.join(self.cache_dir, f"yt_{safe_id}_{safe_q}_{os.getpid()}_{stamp}.mkv")

        self.stop_event = threading.Event()
        self.done_event = threading.Event()
        self.complete = False
        self.failed = False
        self.error = ""
        self._pumps: List[_ChunkedHttpPump] = []
        self._remux_proc_ref: List[Optional[subprocess.Popen]] = [None]
        self._fifo_dir: str = ""
        self._thread: Optional[threading.Thread] = None
        self._started_at: float = 0.0

    def start(self) -> "StreamPreloader":
        prune_preload_cache(keep_path=self.mkv_path, cache_dir=self.cache_dir)
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run_remux, daemon=True)
        self._thread.start()
        return self

    def _run_remux(self) -> None:
        proc: Optional[subprocess.Popen] = None
        pump_threads: List[threading.Thread] = []
        stderr_lines: collections.deque = collections.deque(maxlen=12)
        try:
            # Use direct input-side ffmpeg HTTP Range seek for HLS manifests OR mid-video offsets
            # (>0.1s, e.g. mid-video quality switches): snaps cleanly to the nearest preceding
            # keyframe without downloading the skipped prefix or dropping video packets.
            use_direct_seek = (
                _is_hls_or_manifest(self.video_url)
                or _is_hls_or_manifest(self.audio_url)
                or (self.base_offset > 0.1)
            )
            if use_direct_seek:
                cmd = [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-user_agent", _YT_USER_AGENT,
                    "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "2",
                    "-thread_queue_size", "512",
                ]
                if self.base_offset > 0.1:
                    cmd += ["-ss", f"{self.base_offset:.2f}"]
                cmd += ["-i", self.video_url]
                if self.audio_url:
                    cmd += [
                        "-user_agent", _YT_USER_AGENT,
                        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "2",
                        "-thread_queue_size", "512",
                    ]
                    if self.base_offset > 0.1:
                        cmd += ["-ss", f"{self.base_offset:.2f}"]
                    cmd += ["-i", self.audio_url, "-map", "0:v:0", "-map", "1:a:0"]
                else:
                    cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
                cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero", "-flush_packets", "1", "-f", "matroska", self.mkv_path]
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                self._remux_proc_ref[0] = proc
            else:
                self._fifo_dir = tempfile.mkdtemp(prefix="fifo_", dir=self.cache_dir)
                fifo_v = os.path.join(self._fifo_dir, "v.pipe")
                os.mkfifo(fifo_v)
                cmd = [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-thread_queue_size", "512", "-i", fifo_v,
                ]
                fifo_a = ""
                if self.audio_url:
                    fifo_a = os.path.join(self._fifo_dir, "a.pipe")
                    os.mkfifo(fifo_a)
                    cmd += ["-thread_queue_size", "512", "-i", fifo_a]
                # Fast zero-decode packet skip when resuming mid-video (e.g., quality switch in first 45s)
                if self.base_offset > 0.1:
                    cmd += ["-ss", f"{self.base_offset:.2f}"]
                if self.audio_url:
                    cmd += ["-map", "0:v:0", "-map", "1:a:0"]
                else:
                    cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
                cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero", "-flush_packets", "1", "-f", "matroska", self.mkv_path]

                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                self._remux_proc_ref[0] = proc

                pv = _ChunkedHttpPump(
                    self.video_url, fifo_v, self.stop_event, self._remux_proc_ref,
                    chunk_size=self.chunk_size, cache_dir=self.cache_dir,
                )
                self._pumps.append(pv)
                tv = threading.Thread(target=pv.run, daemon=True)
                pump_threads.append(tv)
                tv.start()

                if fifo_a:
                    pa = _ChunkedHttpPump(
                        self.audio_url, fifo_a, self.stop_event, self._remux_proc_ref,
                        chunk_size=self.chunk_size, cache_dir=self.cache_dir,
                    )
                    self._pumps.append(pa)
                    ta = threading.Thread(target=pa.run, daemon=True)
                    pump_threads.append(ta)
                    ta.start()

            if proc.stderr is not None:
                try:
                    for raw_ln in proc.stderr:
                        txt = raw_ln.decode("utf-8", "ignore").strip()
                        if txt:
                            stderr_lines.append(txt)
                finally:
                    try:
                        proc.stderr.close()
                    except Exception:
                        pass
            rc = proc.wait()
            for t in pump_threads:
                t.join(timeout=1.5)

            # Seekable Range-proxy retry inside StreamPreloader when non-seekable FIFO remux fails
            # (e.g., MP4 with end-of-file moov atom larger than ffmpeg's probe buffer).
            fatal_http = any(p.error.startswith("HTTP 4") for p in self._pumps if p.error)
            if (
                not self.stop_event.is_set()
                and not use_direct_seek
                and not fatal_http
                and (rc != 0 or self.mkv_bytes() <= 512)
                and self.video_url.startswith(("http://", "https://"))
            ):
                if self._fifo_dir:
                    shutil.rmtree(self._fifo_dir, ignore_errors=True)
                    self._fifo_dir = ""
                retry_proxy = None
                try:
                    retry_proxy = _ChunkedRangeProxy(
                        video_url=self.video_url,
                        audio_url=self.audio_url,
                        chunk_size=self.chunk_size,
                    ).start()
                    rcmd = [
                        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-thread_queue_size", "512", "-i", retry_proxy.video_proxy_url,
                    ]
                    if retry_proxy.audio_proxy_url:
                        rcmd += [
                            "-thread_queue_size", "512", "-i", retry_proxy.audio_proxy_url,
                            "-map", "0:v:0", "-map", "1:a:0",
                        ]
                    else:
                        rcmd += ["-map", "0:v:0", "-map", "0:a:0?"]
                    rcmd += ["-c", "copy", "-avoid_negative_ts", "make_zero", "-flush_packets", "1", "-f", "matroska", self.mkv_path]
                    proc = subprocess.Popen(rcmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    self._remux_proc_ref[0] = proc
                    if proc.stderr is not None:
                        try:
                            for raw_ln in proc.stderr:
                                txt = raw_ln.decode("utf-8", "ignore").strip()
                                if txt:
                                    stderr_lines.append(txt)
                        finally:
                            try:
                                proc.stderr.close()
                            except Exception:
                                pass
                    rc = proc.wait()
                    if rc == 0 and self.mkv_bytes() > 512:
                        self._pumps = []  # Cleared so prior FIFO failure doesn't override clean proxy remux
                finally:
                    if retry_proxy is not None:
                        retry_proxy.stop()

            if not self.stop_event.is_set():
                any_pump_failed = any(p.failed for p in self._pumps)
                mkv_sz = self.mkv_bytes()
                if rc == 0 and mkv_sz > 512 and not any_pump_failed:
                    try:
                        os.chmod(self.mkv_path, 0o644)
                    except OSError:
                        pass
                    self.complete = True
                else:
                    self.failed = True
                    pump_err = next((p.error for p in self._pumps if p.error), "")
                    self.error = pump_err or (stderr_lines[-1] if stderr_lines else f"remux rc={rc}")
        except Exception as exc:
            if not self.stop_event.is_set():
                self.failed = True
                self.error = str(exc)[:64]
        finally:
            _kill_proc(proc)
            self._remux_proc_ref[0] = None
            if self._fifo_dir:
                shutil.rmtree(self._fifo_dir, ignore_errors=True)
                self._fifo_dir = ""
            self.done_event.set()

    def mkv_bytes(self) -> int:
        try:
            return os.path.getsize(self.mkv_path)
        except OSError:
            return 0

    def downloaded_bytes(self) -> int:
        if self._pumps:
            return sum(p.downloaded_bytes for p in self._pumps)
        return self.mkv_bytes()

    def total_expected_bytes(self) -> int:
        if self._pumps and all(p.total_bytes > 0 for p in self._pumps):
            return sum(p.total_bytes for p in self._pumps)
        return 0

    def estimated_buffered_sec(self) -> float:
        """Estimated seconds buffered in mkv_path AFTER `self.base_offset`."""
        if self.complete:
            if self.remaining_duration > 0:
                return float(self.remaining_duration)
            if self.duration > 0:
                return max(0.0, float(self.duration) - self.base_offset)
        cur = self.mkv_bytes()
        tot = self.total_expected_bytes()
        if tot > 0 and self.duration > 0:
            bps = max(1.0, float(tot) / float(self.duration))
            return cur / bps
        bps = float(_EST_BYTES_PER_SEC.get(self.quality_label, 160_000))
        return cur / max(1.0, bps)

    def buffered_end_sec(self) -> float:
        """Timeline timestamp (in seconds) reached by the preload buffer."""
        if self.complete and self.duration > 0:
            return float(self.duration)
        end_s = self.base_offset + self.estimated_buffered_sec()
        if self.duration > 0:
            return min(float(self.duration), end_s)
        return end_s

    def covers_offset(self, offset: float) -> bool:
        """True if this preloader covers playback starting at *offset* cleanly."""
        off = max(0.0, float(offset or 0.0))
        if off < self.base_offset - 0.1:
            return False
        if self.complete:
            return True
        return off <= (self.buffered_end_sec() + 12.0)

    def buffered_fraction(self) -> float:
        if self.complete:
            return 1.0
        if self.duration > 0:
            return min(0.99, max(0.0, self.buffered_end_sec() / float(self.duration)))
        cur = self.mkv_bytes()
        tot = self.total_expected_bytes()
        if tot > 0:
            return min(0.99, max(0.0, cur / float(tot)))
        return 0.0

    def speed_kbps(self) -> float:
        dt = max(0.1, time.monotonic() - self._started_at)
        return (self.downloaded_bytes() / 1024.0) / dt

    def ready_for_playback(self, target_sec: int) -> bool:
        if self.failed or self.complete:
            return True
        cur = self.mkv_bytes()
        if target_sec < 0:
            return False  # Full Video mode waits for complete
        if target_sec == 0:
            return cur >= 64 * 1024
        min_bytes = 64 * 1024 if self.quality_label in ("144p", "240p") else 128 * 1024
        return cur >= min_bytes and self.estimated_buffered_sec() >= float(target_sec)

    def start_tail_feeder(self, dst_stdin, feed_stop: threading.Event) -> threading.Thread:
        """Stream growing `.mkv` bytes into playback ffmpeg's stdin until download finishes."""

        def _feeder() -> None:
            fh = None
            try:
                # Wait briefly for file creation if needed
                deadline = time.monotonic() + 5.0
                while not os.path.isfile(self.mkv_path) and not feed_stop.is_set() and not self.done_event.is_set():
                    if time.monotonic() > deadline:
                        return
                    time.sleep(0.02)
                if not os.path.isfile(self.mkv_path):
                    return
                fh = open(self.mkv_path, "rb")
                while not feed_stop.is_set():
                    chunk = fh.read(65536)
                    if chunk:
                        try:
                            dst_stdin.write(chunk)
                        except (BrokenPipeError, OSError, ValueError):
                            return
                    else:
                        if self.done_event.is_set():
                            # Final drain after remuxer closed the file
                            while not feed_stop.is_set():
                                tail = fh.read(65536)
                                if not tail:
                                    break
                                try:
                                    dst_stdin.write(tail)
                                except (BrokenPipeError, OSError, ValueError):
                                    return
                            return
                        time.sleep(0.025)
            except Exception:
                pass
            finally:
                if fh is not None:
                    try:
                        fh.close()
                    except Exception:
                        pass
                try:
                    dst_stdin.close()
                except Exception:
                    pass

        t = threading.Thread(target=_feeder, daemon=True)
        t.start()
        return t

    def stop(self, remove_file: bool = False) -> None:
        self.stop_event.set()
        proc = self._remux_proc_ref[0]
        _kill_proc(proc)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        if self._fifo_dir:
            shutil.rmtree(self._fifo_dir, ignore_errors=True)
            self._fifo_dir = ""
        if remove_file and self.mkv_path:
            try:
                if os.path.isfile(self.mkv_path):
                    os.unlink(self.mkv_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Interactive Application
# ---------------------------------------------------------------------------

def main() -> int:
    import RPi.GPIO as GPIO
    import LCD_1in44
    try:
        import rj_keyboard
    except Exception:
        rj_keyboard = None
    from payloads import _audio_helper as audio_helper
    from payloads._input_helper import poll_ui_input, flush_input, text_input_mode
    from payloads._keyboard_helper import lcd_keyboard

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
    fonts = load_ui_fonts(W, H)
    is_hdmi = getattr(lcd, "display_type", "") == "HDMI"
    is_cardputer = getattr(lcd, "display_type", "") == "CARDPUTER_320"

    running = True

    def _on_sig(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _on_sig)
    signal.signal(signal.SIGTERM, _on_sig)

    cfg = load_settings(default_wide=fonts["wide"])
    thumbs = ThumbnailCache()
    vol_pct = audio_helper.get_playback_volume_pct()
    muted = False
    audio_label = audio_helper.get_active_audio_label()

    def _show_card(title: str, sub: str = "", kind: str = "info", progress: Optional[float] = None, foot: str = "") -> None:
        lcd.LCD_ShowImage(render_status_card(W, H, title, sub, kind=kind, progress=progress, foot=foot, fonts=fonts), 0, 0)

    def _ask_text(title: str, default: str = "", max_len: int = 80) -> Optional[str]:
        flush_input()
        res = lcd_keyboard(lcd, fonts["body"], PINS, GPIO, title=title, default=default, charset="full", max_len=max_len)
        flush_input()
        return res

    def _set_vol(new_pct: int) -> None:
        nonlocal vol_pct, muted
        muted = False
        vol_pct = audio_helper.set_playback_volume_pct(new_pct)

    def _toggle_mute() -> None:
        nonlocal muted
        muted = not muted
        audio_helper.set_playback_mute(muted)

    def _choose_from_modal(title: str, sub: str, options: List[Tuple[str, object]], initial: int = 0):
        sel = max(0, min(initial, max(0, len(options) - 1)))
        ET = rj_theme.EMBER_THEME
        mid = bool(fonts.get("mid", W >= 200 and H >= 200 and W < 300))
        short = bool(fonts.get("short", H <= 200))
        dirty = True
        flush_input()
        while running:
            if dirty:
                img = Image.new("RGB", (W, H), ET["bg_base"])
                d = ImageDraw.Draw(img)
                hdr_h, ftr_h = _yt_chrome_heights(W, H, fonts)
                _draw_header(d, W, hdr_h, title, fonts, right_text=sub)
                y0 = hdr_h + (8 if fonts["large"] else (5 if mid else 4))
                y1 = H - ftr_h - 4
                row_h = 30 if fonts["large"] else (24 if mid else (21 if (fonts["wide"] and short) else (22 if fonts["wide"] else 16)))
                vis = max(1, (y1 - y0) // row_h)
                st = max(0, min(sel - vis // 2, max(0, len(options) - vis)))
                for slot in range(vis):
                    idx = st + slot
                    if idx >= len(options):
                        break
                    lbl = options[idx][0]
                    ry = y0 + slot * row_h
                    ry1 = min(y1, ry + row_h - 3)
                    is_s = idx == sel
                    rj_theme.draw_card(
                        d, (10 if (fonts["wide"] or mid) else 3, ry, W - (14 if (fonts["wide"] or mid) else 6), ry1),
                        fill=ET["select_bg"] if is_s else ET["bg_surface"],
                        outline=ET["border_accent"] if is_s else ET["border_subtle"],
                        radius=6 if (fonts["wide"] or mid) else 3,
                        accent_left=ET["accent"] if is_s else None,
                        accent_width=3 if (fonts["wide"] or mid) else 2,
                    )
                    d.text((20 if (fonts["wide"] or mid) else 8, ry + (6 if fonts["large"] else (4 if mid else 3))), rj_theme.truncate_text(d, lbl, W - (36 if (fonts["wide"] or mid) else 18), fonts["body_bold"] if is_s else fonts["body"]), font=fonts["body_bold"] if is_s else fonts["body"], fill=ET["accent_bright"] if is_s else ET["text_primary"])
                if len(options) > vis:
                    rj_theme.draw_scrollbar(d, W - 8, y0, y1, st, vis, len(options), width=4 if (fonts["wide"] or mid) else 2)
                _draw_footer(
                    d, W, H, ftr_h,
                    format_footer_hint(
                        W, H,
                        hat_hint="U/D OK:Go K3:Back",
                        hat_240_hint="U/D:Pick OK:Apply K3:Back",
                        cardputer_hint="U/D:Pick Ent:Apply Esc:Back",
                        hdmi_hint="UP/DN:Pick  Enter:Apply  Esc:Back",
                    ),
                    fonts,
                )
                lcd.LCD_ShowImage(img, 0, 0)
                dirty = False

            act, _ch = poll_ui_input(PINS, GPIO)
            if act == "UP":
                sel = (sel - 1) % len(options)
                dirty = True
            elif act == "DOWN":
                sel = (sel + 1) % len(options)
                dirty = True
            elif is_confirm(act):
                flush_input()
                return options[sel][1]
            elif is_back(act):
                flush_input()
                return None
            time.sleep(0.04)
        return None

    def _pick_stream_quality() -> bool:
        old_q = cfg["quality_idx"]
        opts = [
            (f"{lbl}" + ("  [ACTIVE]" if i == old_q else ""), i)
            for i, (lbl, _) in enumerate(STREAM_QUALITIES)
        ]
        chosen = _choose_from_modal("STREAM QUALITY", "Higher = sharper, needs more CPU/net", opts, initial=old_q)
        if chosen is not None and chosen != old_q:
            cfg["quality_idx"] = int(chosen)
            save_settings(cfg)
            return True
        return False

    def _download_media(
        video_id: str,
        title: str,
        fmt: str,
        q_label: str,
        q_val: str,
        dest_dir: Optional[str] = None,
        silent_done: bool = False,
        header_prefix: str = "",
    ) -> str:
        """Download one video/audio file. Returns 'ok', 'skip', 'cancel', or 'err'."""
        dl_dir = dest_dir or get_downloads_dir()
        os.makedirs(dl_dir, exist_ok=True)
        safe = sanitize_yt_name(title, max_len=52, fallback=video_id)
        out_file = os.path.join(dl_dir, f"{safe}.{fmt}")
        if os.path.isfile(out_file):
            if not silent_done:
                _show_card("Already Downloaded", f"{safe[:28]}.{fmt}", kind="ok")
                time.sleep(0.9)
            return "skip"
        url = f"https://www.youtube.com/watch?v={video_id}"
        if fmt == "mp4":
            cmd = [
                "yt-dlp", "-f", q_val, "--merge-output-format", "mp4", "--newline",
                "-o", os.path.join(dl_dir, f"{safe}.%(ext)s"), "--no-playlist", url,
            ]
        else:
            cmd = [
                "yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", q_val,
                "--embed-metadata", "--newline",
                "-o", os.path.join(dl_dir, f"{safe}.%(ext)s"), "--no-playlist", url,
            ]
        hdr_lbl = header_prefix or f"Downloading {fmt.upper()} ({q_label})"
        cancel_foot = format_footer_hint(
            W, H,
            hat_hint="K3:Cancel",
            hat_240_hint="KEY3: Cancel",
            cardputer_hint="Esc: Cancel",
            hdmi_hint="Press Esc to cancel",
        )
        _show_card(hdr_lbl, safe[:32], progress=0.0, foot=cancel_foot)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        last_pct = -1.0
        cancelled = False
        try:
            while proc.poll() is None:
                if not running:
                    cancelled = True
                    break
                act, _k = poll_ui_input(PINS, GPIO)
                if is_back(act):
                    cancelled = True
                    break
                if proc.stdout is None:
                    break
                line = proc.stdout.readline()
                if not line:
                    continue
                if "Merging" in line or "ExtractAudio" in line:
                    _show_card(hdr_lbl, f"Processing {safe[:24]}...", progress=0.98)
                    continue
                m_pct = re.search(r"(\d+(?:\.\d+)?)%", line)
                if m_pct:
                    pct = float(m_pct.group(1))
                    if abs(pct - last_pct) >= 1.0:
                        last_pct = pct
                        _show_card(hdr_lbl, safe[:32], progress=min(1.0, pct / 100.0), foot=cancel_foot)
        finally:
            if cancelled:
                _kill_proc(proc)
        if cancelled:
            if not silent_done:
                _show_card("Download Cancelled", safe[:32], kind="err")
                time.sleep(0.9)
            return "cancel"
        if proc.returncode == 0:
            if not silent_done:
                _show_card("Download Complete!", f"{safe[:28]}.{fmt}", kind="ok")
                time.sleep(1.2)
            return "ok"
        if not silent_done:
            _show_card("Download Failed", "Check yt-dlp / connection", kind="err")
            time.sleep(1.2)
        return "err"

    def _download_playlist_flow(items: List[dict], playlist_name: str) -> None:
        if not items:
            return
        fmt = _choose_from_modal("DOWNLOAD PLAYLIST FORMAT", f"{playlist_name[:20]} ({len(items)})", [
            ("MP4 Video (All Items)", "mp4"),
            ("MP3 Audio (All Items)", "mp3"),
            ("Cancel", None),
        ])
        if not fmt:
            return
        quals = MP4_QUALITIES if fmt == "mp4" else MP3_QUALITIES
        chosen = _choose_from_modal(f"PLAYLIST {fmt.upper()} QUALITY", playlist_name[:28], [(lbl, (lbl, val)) for lbl, val in quals])
        if not chosen:
            return
        q_lbl, q_val = chosen
        folder = sanitize_yt_name(playlist_name, max_len=48, fallback="Playlist")
        dest_dir = os.path.join(get_downloads_dir(), folder)
        os.makedirs(dest_dir, exist_ok=True)
        saved_n = skipped_n = err_n = 0
        cancelled = False
        for i, it in enumerate(items):
            if not running:
                cancelled = True
                break
            if it.get("local"):
                skipped_n += 1
                continue
            st = _download_media(
                it["id"], it.get("title", it["id"]), fmt, q_lbl, q_val,
                dest_dir=dest_dir, silent_done=True,
                header_prefix=f"Playlist {i + 1}/{len(items)} ({fmt.upper()})",
            )
            if st == "cancel":
                cancelled = True
                break
            elif st == "ok":
                saved_n += 1
            elif st == "skip":
                skipped_n += 1
            else:
                err_n += 1
        title_done = "Playlist Download Stopped" if cancelled else "Playlist Downloaded!"
        _show_card(title_done, f"Saved: {saved_n}  •  Skipped: {skipped_n}  •  Err: {err_n}", kind="info" if cancelled else "ok", foot=f"Folder: Downloads/{folder}")
        time.sleep(2.0)

    def _open_download_menu(video: dict, playlist_items: Optional[List[dict]] = None, playlist_name: str = "") -> None:
        if video.get("local"):
            return
        if playlist_items and len(playlist_items) > 1:
            scope = _choose_from_modal("DOWNLOAD", playlist_name[:28] or "Playlist", [
                (f"Download Highlighted Only ({video.get('title', '')[:20]})", "one"),
                (f"Download ENTIRE Playlist ({len(playlist_items)} items)", "all"),
                ("Cancel", None),
            ])
            if scope == "all":
                _download_playlist_flow(playlist_items, playlist_name or "Playlist")
                return
            if scope != "one":
                return
        fmt = _choose_from_modal("DOWNLOAD FORMAT", video.get("title", "")[:28], [
            ("MP4 Video (Video + Audio)", "mp4"),
            ("MP3 Audio (Music Only)", "mp3"),
            ("Cancel", None),
        ])
        if not fmt:
            return
        quals = MP4_QUALITIES if fmt == "mp4" else MP3_QUALITIES
        chosen = _choose_from_modal(f"{fmt.upper()} QUALITY", video.get("title", "")[:28], [(lbl, (lbl, val)) for lbl, val in quals])
        if chosen:
            _download_media(video["id"], video.get("title", video["id"]), fmt, chosen[0], chosen[1])

    def _open_settings_menu() -> None:
        nonlocal audio_label
        while running:
            audio_label = audio_helper.get_active_audio_label()
            q_lbl = STREAM_QUALITIES[cfg["quality_idx"]][0]
            rs_lbl = {
                "auto": "Auto",
                "fast": "Fast (320x180)",
                "balanced": "Balanced (480x270)",
                "sharp": "Sharp (640x360)",
            }.get(cfg.get("render_scale", "auto"), "Auto")
            pl_sec = int(cfg.get("preload_sec", 5))
            pl_lbl = {0: "Off", 3: "3s", 5: "5s", 10: "10s", -1: "Full Video"}.get(pl_sec, f"{pl_sec}s")
            has_mpv = shutil.which("mpv") is not None
            eng_val = cfg.get("player_engine", "builtin")
            eng_lbl = "mpv Hardware" if (eng_val == "mpv" and has_mpv) else "Built-in (Preloaded)"
            spd_val = float(cfg.get("playback_speed", 1.0))
            opts = [
                (f"Stream Quality: {q_lbl}", "quality"),
                (f"Playback Speed: {spd_val:g}x", "speed"),
                (f"Auto Captions (CC): {'ON' if cfg.get('auto_captions') else 'OFF'}", "captions"),
                (f"Preload Buffer: {pl_lbl}", "preload"),
                (f"Playback Engine: {eng_lbl}", "engine"),
                (f"Render Mode: {rs_lbl}", "render_scale"),
                (f"Video FPS: {cfg['fps_wide']} fps", "fps"),
                (f"Auto-Next Playlist: {'ON' if cfg['auto_next'] else 'OFF'}", "auto_next"),
                (f"Show Video OSD HUD: {'ON' if cfg['show_osd'] else 'OFF'}", "osd"),
                (f"BT A/V Delay Comp: +{cfg['bt_sync_ms']}ms", "bt_sync"),
                (f"Fine A/V Sync Offset: {cfg['av_sync_ms']:+d}ms", "av_sync"),
                (f"Audio Output: {audio_label}", "audio_sink"),
                ("Open Audio Manager (Mixer/Profiles)", "aud_mgr"),
                ("Open Bluetooth Headphones Manager", "bt_mgr"),
                (f"Volume: {vol_pct}% (+10%)", "vol_up"),
                (f"Volume: {vol_pct}% (-10%)", "vol_dn"),
                ("Back", None),
            ]
            act = _choose_from_modal("YOUTUBE SETTINGS", audio_label, opts)
            if not act:
                return
            if act == "quality":
                _pick_stream_quality()
            elif act == "speed":
                sp_opts = [(f"{s:g}x" + ("  [ACTIVE]" if abs(s - spd_val) < 0.01 else ""), s) for s in SPEED_OPTIONS]
                picked_sp = _choose_from_modal("PLAYBACK SPEED", "Works in Built-in & mpv", sp_opts)
                if picked_sp is not None:
                    cfg["playback_speed"] = float(picked_sp)
                    save_settings(cfg)
            elif act == "captions":
                cfg["auto_captions"] = not bool(cfg.get("auto_captions", False))
                save_settings(cfg)
            elif act == "preload":
                pl_opts = [
                    (lbl + ("  [ACTIVE]" if val == pl_sec else ""), val)
                    for lbl, val in PRELOAD_OPTIONS
                ]
                picked_pl = _choose_from_modal("PRELOAD BUFFER", "Pre-buffers video in RAM to eliminate lag", pl_opts)
                if picked_pl is not None:
                    cfg["preload_sec"] = int(picked_pl)
                    save_settings(cfg)
            elif act == "engine":
                mpv_note = "GPU/DRM Zero-Copy" if has_mpv else "Install: sudo apt install mpv"
                eng_opts = [
                    ("Built-in (Preloaded — Integrated HUD/OSD)" + ("  [ACTIVE]" if eng_val == "builtin" else ""), "builtin"),
                    (f"mpv Hardware ({mpv_note})" + ("  [ACTIVE]" if eng_val == "mpv" else ""), "mpv"),
                ]
                picked_eng = _choose_from_modal("PLAYBACK ENGINE", "mpv uses Pi GPU for smooth 720p/1080p", eng_opts)
                if picked_eng == "mpv" and not has_mpv:
                    _show_card("mpv Not Installed", "Run: sudo apt install -y mpv", kind="err", foot="Keeping Built-in (Preloaded) engine")
                    time.sleep(2.0)
                elif picked_eng in ("builtin", "mpv"):
                    cfg["player_engine"] = str(picked_eng)
                    save_settings(cfg)
            elif act == "fps":
                fps_opts = [(f"{v} fps" + (" (Default)" if v == 24 else ""), v) for v in (15, 20, 24, 30)]
                picked_fps = _choose_from_modal("VIDEO FRAME RATE", "Lower FPS uses less Raspberry Pi CPU", fps_opts)
                if picked_fps is not None:
                    cfg["fps_wide"] = int(picked_fps)
                    save_settings(cfg)
            elif act == "render_scale":
                rs_opts = [
                    ("Auto (320x180 for 144/240p, 640x360 for 360p+)", "auto"),
                    ("Fast (320x180 — Max FPS / Lowest CPU)", "fast"),
                    ("Balanced (480x270 — Sharp & Smooth on 1080p)", "balanced"),
                    ("Sharp (640x360 — Crispest on 1080p)", "sharp"),
                ]
                picked_rs = _choose_from_modal("VIDEO RENDER MODE", "Fast/Balanced cut pipe & display CPU", rs_opts)
                if picked_rs is not None:
                    cfg["render_scale"] = str(picked_rs)
                    save_settings(cfg)
            elif act == "auto_next":
                cfg["auto_next"] = not cfg["auto_next"]
                save_settings(cfg)
            elif act == "osd":
                cfg["show_osd"] = not cfg["show_osd"]
                save_settings(cfg)
            elif act == "bt_sync":
                bt_opts = [(f"+{ms} ms" + (" (Default)" if ms == 200 else ""), ms) for ms in (0, 100, 150, 200, 250, 300, 400)]
                picked_ms = _choose_from_modal("BT AUDIO DELAY", "Delay video to match BT headphones", bt_opts)
                if picked_ms is not None:
                    cfg["bt_sync_ms"] = int(picked_ms)
                    save_settings(cfg)
            elif act == "av_sync":
                sync_opts = [(f"{ms:+d} ms", ms) for ms in (-300, -200, -100, -50, 0, 50, 100, 150, 200, 300, 400)]
                picked_ms = _choose_from_modal("FINE A/V SYNC OFFSET", "+ delays video, - delays audio", sync_opts)
                if picked_ms is not None:
                    cfg["av_sync_ms"] = int(picked_ms)
                    save_settings(cfg)
            elif act == "audio_sink":
                try:
                    import rj_bluetooth
                    sinks = rj_bluetooth.list_audio_sinks()
                    if not sinks:
                        _show_card("Audio Output", "No PipeWire/Pulse sinks (using ALSA)")
                        time.sleep(1.3)
                    else:
                        s_opts = [(("* " if s["is_default"] else "  ") + s["label"], s) for s in sinks]
                        picked = _choose_from_modal("AUDIO OUTPUT", "Select speakers / headphones", s_opts)
                        if isinstance(picked, dict):
                            rj_bluetooth.set_default_audio_sink(picked["name"], label=picked["label"])
                            audio_label = audio_helper.get_active_audio_label()
                except Exception:
                    pass
            elif act in ("bt_mgr", "aud_mgr"):
                sub_fn = "bluetooth_manager.py" if act == "bt_mgr" else "audio_manager.py"
                sub_script = os.path.join(rj_env.get_base_dir(), "payloads", "utilities", sub_fn)
                if os.path.isfile(sub_script):
                    subprocess.run([sys.executable, sub_script], cwd=rj_env.get_base_dir())
                    if rj_keyboard is not None:
                        rj_keyboard.set_text_mode(True)
                    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
                    audio_label = audio_helper.get_active_audio_label()
                    flush_input()
            elif act == "vol_up":
                _set_vol(vol_pct + 10)
            elif act == "vol_dn":
                _set_vol(vol_pct - 10)

    def _add_to_video_playlist_flow(video: dict) -> None:
        if video.get("local"):
            return
        pls = load_video_playlists()
        opts: List[Tuple[str, object]] = [
            (f"{p['name']} ({len(p.get('items', []))})", p["id"]) for p in pls
        ]
        opts.append(("+ Create New Playlist...", "__new__"))
        opts.append(("Cancel", None))
        picked = _choose_from_modal("ADD TO PLAYLIST", video.get("title", "")[:26], opts)
        if not picked:
            return
        if picked == "__new__":
            nm = _ask_text("New Video Playlist Name", max_len=48)
            if nm and nm.strip():
                pl = create_video_playlist(nm.strip(), [video])
                _show_card("Saved!", f"Created {pl['name']}", kind="ok")
                time.sleep(0.9)
        else:
            ok_a = add_to_video_playlist(str(picked), video)
            _show_card("Playlist Updated" if ok_a else "Already in Playlist", video.get("title", "")[:28], kind="ok" if ok_a else "info")
            time.sleep(0.8)

    def _browse_channel_flow(item_or_ref, label: str = "") -> None:
        if isinstance(item_or_ref, dict):
            ref = item_or_ref.get("channel_url") or item_or_ref.get("channel_id") or item_or_ref.get("channel", "")
            ch_lbl = label or item_or_ref.get("channel", "Channel")
            if not ref and item_or_ref.get("id"):
                _show_card("Finding Channel...", ch_lbl[:28])
                det = fetch_video_details(item_or_ref["id"])
                ref = det.get("channel_url") or det.get("channel_id") or det.get("channel") or ch_lbl
        else:
            ref = str(item_or_ref or "")
            ch_lbl = label or ref
        if not ref:
            return
        _show_card("Loading Channel...", ch_lbl[:32])
        vids, err = fetch_channel_videos(ref, max_results=16)
        if err or not vids:
            _show_card("Channel Error", err or "No videos found", kind="err")
            time.sleep(1.3)
            return
        _browse_media_list(f"CHANNEL: {ch_lbl[:16]}", vids, subtitle=f"{len(vids)} uploads")

    def _open_video_info_modal(item: dict) -> None:
        if item.get("local"):
            return
        _show_card("Loading Video Info...", item.get("title", "")[:32])
        det = fetch_video_details(item.get("id", ""))
        ch_name = str(det.get("channel") or item.get("channel") or "")
        ch_url = str(det.get("channel_url") or item.get("channel_url") or "")
        ch_id = str(det.get("channel_id") or item.get("channel_id") or "")
        scroll = 0
        dirty_i = True
        flush_input()
        while running:
            if dirty_i:
                im = render_video_info_card(
                    W, H, item=item, details=det, scroll=scroll,
                    is_sub=is_subscribed(ch_name, ch_id),
                    in_wl=is_in_watch_later(item.get("id", "")),
                    liked=is_liked(item.get("id", "")),
                    fonts=fonts,
                )
                lcd.LCD_ShowImage(im, 0, 0)
                dirty_i = False
            act, ch = poll_ui_input(PINS, GPIO)
            if act is None and ch is None:
                time.sleep(0.03)
                continue
            dirty_i = True
            kl = ch.lower() if isinstance(ch, str) else ""
            if is_back(act):
                flush_input()
                return
            elif act == "DOWN":
                scroll += 2
            elif act == "UP":
                scroll = max(0, scroll - 2)
            elif act == "PAGEDOWN":
                scroll += 8
            elif act == "PAGEUP":
                scroll = max(0, scroll - 8)
            elif is_confirm(act) or is_action(act, ch, "KEY1", "o"):
                i_opts: List[Tuple[str, object]] = []
                if ch_name:
                    i_opts.append(("Unfollow Channel" if is_subscribed(ch_name, ch_id) else f"Follow {ch_name[:18]}", "follow"))
                if ch_url or ch_id or ch_name:
                    i_opts.append(("Browse Channel Uploads", "chan"))
                i_opts.extend([
                    ("Remove Watch Later" if is_in_watch_later(item.get("id", "")) else "Add to Watch Later", "wl"),
                    ("Add to Playlist...", "pl"),
                    ("Unlike Video" if is_liked(item.get("id", "")) else "Like Video", "like"),
                    ("Download (MP4 / MP3)...", "dl"),
                    ("Back", None),
                ])
                ia = _choose_from_modal("VIDEO ACTIONS", item.get("title", "")[:24], i_opts)
                if ia == "follow" and ch_name:
                    toggle_subscription(ch_name, ch_url, ch_id)
                elif ia == "chan":
                    _browse_channel_flow({"channel": ch_name, "channel_url": ch_url, "channel_id": ch_id}, label=ch_name)
                elif ia == "wl":
                    toggle_watch_later(item)
                elif ia == "pl":
                    _add_to_video_playlist_flow(item)
                elif ia == "like":
                    toggle_like_video(item)
                elif ia == "dl":
                    _open_download_menu(item)
            elif kl == "f" and ch_name:
                toggle_subscription(ch_name, ch_url, ch_id)
            elif kl == "u" and (ch_url or ch_id or ch_name):
                _browse_channel_flow({"channel": ch_name, "channel_url": ch_url, "channel_id": ch_id}, label=ch_name)
            elif kl == "w":
                toggle_watch_later(item)
            elif kl == "a":
                _add_to_video_playlist_flow(item)
            elif kl == "l":
                toggle_like_video(item)
            elif kl == "d":
                _open_download_menu(item)

    # ------------------------------------------------------------------
    # Video Playback Engine (Preloaded RAM Cache + Zero-GC Pool + Optional mpv GPU)
    # ------------------------------------------------------------------

    def _probe_local_has_audio(filepath: str) -> bool:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type", "-of", "csv=p=0", filepath],
                capture_output=True, text=True, timeout=3,
            )
            return "audio" in (r.stdout or "")
        except Exception:
            return True

    def _mpv_ipc_cmd(sock_path: str, command_list: list) -> None:
        if not sock_path or not os.path.exists(sock_path):
            return
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(0.25)
            s.connect(sock_path)
            payload = (json.dumps({"command": command_list}) + "\n").encode("utf-8")
            s.sendall(payload)
        except OSError:
            pass
        finally:
            try:
                s.close()
            except OSError:
                pass

    def _play_with_mpv(
        item: dict,
        video_url: str,
        audio_url: str,
        start_offset: float,
        eff_sync_ms: int,
        is_local: bool,
        preloader: Optional[StreamPreloader],
        range_proxy: Optional[_ChunkedRangeProxy] = None,
    ) -> Tuple[str, float]:
        """Optional hardware-accelerated mpv engine (GPU/DRM on Pi, fullscreen on Wayland/X11).

        Returns ('fallback', start_offset) if mpv cannot initialize cleanly so the caller
        transparently falls back to the Built-in (Preloaded) renderer.
        """
        nonlocal vol_pct, muted
        mpv_bin = shutil.which("mpv")
        if not mpv_bin:
            return ("fallback", start_offset)

        import hdmi_shm
        try:
            import rj_app_launcher as _app_l
            sess = _app_l.detect_display_session()
            handoff_flag = _app_l.KIOSK_HANDOFF_FLAG
        except Exception:
            sess = {"has_live_session": bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))}
            handoff_flag = "/dev/shm/rj_kiosk_handoff"

        has_live_session = bool(sess.get("has_live_session"))
        ipc_sock = os.path.join(tempfile.gettempdir(), f"rj_mpv_{os.getpid()}.sock")
        try:
            if os.path.exists(ipc_sock):
                os.unlink(ipc_sock)
        except OSError:
            pass

        # Prefer completed preloaded MKV, else anti-throttle local Range proxy for long/seeked streams
        use_mkv = (not is_local) and preloader is not None and preloader.complete and os.path.isfile(preloader.mkv_path)
        use_proxy_mpv = (not is_local) and (not use_mkv) and range_proxy is not None and bool(range_proxy.video_proxy_url)
        media_src = preloader.mkv_path if use_mkv else (range_proxy.video_proxy_url if use_proxy_mpv else video_url)
        mpv_audio = range_proxy.audio_proxy_url if use_proxy_mpv else audio_url
        mpv_start = max(0.0, start_offset - preloader.base_offset) if (use_mkv and preloader is not None) else start_offset

        cmd = [
            mpv_bin,
            "--no-config",
            "--no-terminal",
            "--fs",
            "--hwdec=auto-safe",
            "--cache=yes",
            "--demuxer-max-bytes=64MiB",
            "--demuxer-readahead-secs=30",
            f"--input-ipc-server={ipc_sock}",
            f"--force-media-title={str(item.get('title', 'YouTube'))[:72]}",
            f"--speed={float(cfg.get('playback_speed', 1.0)):.2f}",
            "--osd-level=1",
        ]
        if not has_live_session:
            cmd += ["--vo=drm"]
        if mpv_start > 0.1:
            cmd += [f"--start={mpv_start:.2f}"]
        if eff_sync_ms != 0:
            # In mpv, negative audio-delay plays audio earlier (compensating BT latency)
            cmd += [f"--audio-delay={-eff_sync_ms / 1000.0:.3f}"]
        if (not is_local) and cfg.get("auto_captions") and item.get("id"):
            _cues, vtt_p, _se = fetch_video_subtitles(item["id"])
            if vtt_p and os.path.isfile(vtt_p):
                cmd += [f"--sub-file={vtt_p}"]
        if (not is_local) and (not use_mkv) and mpv_audio:
            cmd += [f"--audio-file={mpv_audio}"]
        cmd.append(media_src)

        env = audio_helper.get_pulse_env()
        if has_live_session:
            if sess.get("wayland_display"):
                env["WAYLAND_DISPLAY"] = sess["wayland_display"]
            if sess.get("x_display"):
                env["DISPLAY"] = sess["x_display"]
            if sess.get("runtime_dir"):
                env["XDG_RUNTIME_DIR"] = sess["runtime_dir"]

        host_was_running = False
        if not has_live_session:
            try:
                with open(handoff_flag, "w", encoding="utf-8") as fh:
                    fh.write("mpv")
            except OSError:
                pass
            try:
                host_was_running, _ = hdmi_shm.is_host_running()
                if host_was_running:
                    hdmi_shm.stop_host()
                time.sleep(0.2)
            except Exception:
                pass

        ff_kw = (
            audio_helper.get_audio_popen_kwargs(cmd, allow_user_drop=(not is_local) and has_live_session)
            if hasattr(audio_helper, "get_audio_popen_kwargs")
            else {"env": env}
        )
        if "env" in ff_kw:
            merged_env = dict(ff_kw["env"])
            merged_env.update(env)
            ff_kw["env"] = merged_env
        else:
            ff_kw["env"] = env

        proc = None
        started = time.monotonic()
        result: Tuple[str, float] = ("next", 0.0)
        flush_input()
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **ff_kw)
            while running:
                rc = proc.poll()
                if rc is not None:
                    if rc != 0 and (time.monotonic() - started) < 2.5:
                        result = ("fallback", start_offset)
                    break

                act, ch = poll_ui_input(PINS, GPIO)
                kl = ch.lower() if isinstance(ch, str) else ""
                if is_back(act):
                    _mpv_ipc_cmd(ipc_sock, ["quit"])
                    result = ("stop", 0.0)
                    break
                elif kl == "n":
                    _mpv_ipc_cmd(ipc_sock, ["quit"])
                    result = ("next", 0.0)
                    break
                elif kl == "b":
                    _mpv_ipc_cmd(ipc_sock, ["quit"])
                    result = ("prev", 0.0)
                    break
                elif act == "OK" or ch == " ":
                    _mpv_ipc_cmd(ipc_sock, ["cycle", "pause"])
                elif act == "RIGHT":
                    _mpv_ipc_cmd(ipc_sock, ["seek", 15, "relative"])
                elif act == "LEFT":
                    _mpv_ipc_cmd(ipc_sock, ["seek", -15, "relative"])
                elif act == "UP" or is_step_up(ch):
                    _set_vol(vol_pct + 5)
                    _mpv_ipc_cmd(ipc_sock, ["show-text", f"Volume: {vol_pct}%"])
                elif act == "DOWN" or is_step_down(ch):
                    _set_vol(vol_pct - 5)
                    _mpv_ipc_cmd(ipc_sock, ["show-text", f"Volume: {vol_pct}%"])
                elif kl == "m":
                    _toggle_mute()
                    _mpv_ipc_cmd(ipc_sock, ["show-text", "Muted" if muted else f"Unmuted ({vol_pct}%)"])
                elif is_action(act, ch, "KEY2", "i"):
                    _mpv_ipc_cmd(ipc_sock, ["show-progress"])
                elif is_action(act, ch, "KEY1", "o"):
                    cur_sp = float(cfg.get("playback_speed", 1.0))
                    idx_sp = SPEED_OPTIONS.index(cur_sp) if cur_sp in SPEED_OPTIONS else 2
                    nxt_sp = SPEED_OPTIONS[(idx_sp + 1) % len(SPEED_OPTIONS)]
                    cfg["playback_speed"] = nxt_sp
                    save_settings(cfg)
                    _mpv_ipc_cmd(ipc_sock, ["set_property", "speed", nxt_sp])
                    _mpv_ipc_cmd(ipc_sock, ["show-text", f"Speed: {nxt_sp:g}x"])
                elif kl in ("z", "x"):
                    cur_sp = float(cfg.get("playback_speed", 1.0))
                    idx_sp = SPEED_OPTIONS.index(cur_sp) if cur_sp in SPEED_OPTIONS else 2
                    nxt_sp = SPEED_OPTIONS[(idx_sp + (1 if kl == "x" else -1)) % len(SPEED_OPTIONS)]
                    cfg["playback_speed"] = nxt_sp
                    save_settings(cfg)
                    _mpv_ipc_cmd(ipc_sock, ["set_property", "speed", nxt_sp])
                    _mpv_ipc_cmd(ipc_sock, ["show-text", f"Speed: {nxt_sp:g}x"])
                elif kl == "r":
                    _mpv_ipc_cmd(ipc_sock, ["cycle-values", "loop-file", "inf", "no"])
                    _mpv_ipc_cmd(ipc_sock, ["show-text", "Toggled Loop"])
                elif kl == "t":
                    _mpv_ipc_cmd(ipc_sock, ["cycle", "sub-visibility"])
                elif ch in ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9"):
                    _mpv_ipc_cmd(ipc_sock, ["seek", int(ch) * 10, "absolute-percent"])
                time.sleep(0.03)
        except Exception:
            result = ("fallback", start_offset)
        finally:
            _kill_proc(proc)
            try:
                if os.path.exists(ipc_sock):
                    os.unlink(ipc_sock)
            except OSError:
                pass
            if not has_live_session:
                try:
                    if os.path.exists(handoff_flag):
                        os.unlink(handoff_flag)
                except OSError:
                    pass
                if host_was_running:
                    try:
                        hdmi_shm.ensure_host_running()
                        time.sleep(0.15)
                    except Exception:
                        pass
            flush_input()
        return result

    def _play_video_item(
        item: dict,
        start_offset: float = 0.0,
        playlist_pos: str = "",
        preloader_box: Optional[list] = None,
        next_item: Optional[dict] = None,
    ) -> Tuple[str, float]:
        """Play one video (remote YouTube with progressive RAM preloading or local file)."""
        nonlocal audio_label
        audio_label = audio_helper.get_active_audio_label()
        title = item.get("title", "Untitled")
        duration = int(item.get("duration") or 0)
        is_local = bool(item.get("local"))
        q_label, q_fmt = STREAM_QUALITIES[cfg["quality_idx"]]
        preload_sec = int(cfg.get("preload_sec", 5))
        # Live streams (duration < 0) cannot preload "Full Video" — cap at 3s rolling buffer
        if duration < 0 and preload_sec < 0:
            preload_sec = 3

        _show_card(
            "Loading Video..." if start_offset <= 0 else f"Seeking to {format_duration(int(start_offset))}...",
            title[:36],
            foot=f"{'LOCAL' if is_local else q_label}  •  {audio_label}",
        )

        if is_local:
            video_url = item["path"]
            audio_url = ""
        else:
            video_url, audio_url, err = resolve_stream_urls(item["id"], q_fmt)
            if not video_url:
                _show_card("Stream Error", err or "Unavailable", kind="err")
                time.sleep(1.8)
                return ("next", 0.0)
            # Prefetch next playlist track's URL in background while watching this one
            if next_item and not next_item.get("local") and next_item.get("id"):
                prefetch_stream_urls_async(next_item["id"], q_fmt)

        # Stream delivery strategy:
        #   - Short videos (<=15 min) from start with >=300 MB free cache: progressive RAM-cached
        #     `.mkv` via StreamPreloader (instant replays/seeks inside short clips).
        #   - Long videos (>15 min / unknown duration), mid-video seeks/resumes, low /dev/shm space,
        #     preload_sec==0, OR StreamPreloader fallback: O(1)-RAM seekable anti-throttle HTTP Range
        #     proxy (_ChunkedRangeProxy) — prevents /dev/shm RAM exhaustion, handles end-of-file
        #     MP4 `moov` atoms & WebM `Cues` instantly, and defeats googlevideo.com throttling!
        preloader: Optional[StreamPreloader] = None
        range_proxy: Optional[_ChunkedRangeProxy] = None
        if not is_local:
            existing = preloader_box[0] if (preloader_box is not None and preloader_box) else None
            can_reuse = (
                preload_sec != 0
                and existing is not None
                and existing.key == (item.get("id", ""), q_label)
                and not existing.failed
                and existing.covers_offset(start_offset)
            )
            if can_reuse:
                preloader = existing
            elif should_use_range_proxy(
                duration=duration,
                start_offset=start_offset,
                preload_sec=preload_sec,
                video_url=video_url,
                audio_url=audio_url,
            ):
                if existing is not None:
                    existing.stop(remove_file=True)
                    if preloader_box is not None:
                        preloader_box[0] = None
                range_proxy = _ChunkedRangeProxy(video_url=video_url, audio_url=audio_url).start()
                if preload_sec != 0 and start_offset <= 0.1:
                    est_bps = int(_EST_BYTES_PER_SEC.get(q_label, 160_000))
                    warm_secs = 10 if preload_sec < 0 else max(3, int(preload_sec))
                    warm_target = max(512 * 1024, min(4 * 1024 * 1024, est_bps * warm_secs))
                    range_proxy.start_warm_buffer(target_bytes=warm_target)
                    gate_started = time.monotonic()
                    last_ui_at = 0.0
                    preload_foot = format_footer_hint(
                        W, H,
                        hat_hint="OK:Play K3:Cancel",
                        hat_240_hint="OK: Play Now  •  K3: Cancel",
                        cardputer_hint="Enter: Play Now  •  Esc: Cancel",
                        hdmi_hint="Enter: Play Now   •   Esc: Cancel",
                    )
                    flush_input()
                    while running and not range_proxy.warm_done.is_set():
                        now_p = time.monotonic()
                        if (now_p - gate_started) > 8.0:
                            break
                        act_p, _ch_p = poll_ui_input(PINS, GPIO)
                        if is_back(act_p):
                            range_proxy.stop()
                            return ("stop", 0.0)
                        if is_confirm(act_p) and range_proxy.warmed_bytes >= 128 * 1024:
                            break
                        if (now_p - last_ui_at) >= 0.12:
                            wb = range_proxy.warmed_bytes
                            wt = max(1, range_proxy.warm_target_bytes)
                            prog_p = min(0.99, wb / float(wt))
                            mb = wb / (1024.0 * 1024.0)
                            spd = range_proxy.speed_kbps()
                            lbl_s = "Full" if preload_sec < 0 else f"{preload_sec}s"
                            _show_card(
                                f"Preloading Stream ({lbl_s} Buffer)...",
                                f"{q_label}  •  {mb:.1f} MB ({spd:.0f} KB/s)",
                                progress=prog_p,
                                foot=preload_foot,
                            )
                            last_ui_at = now_p
                        time.sleep(0.04)
                else:
                    _show_card(
                        "Seeking Stream..." if start_offset > 0.1 else "Streaming Video...",
                        title[:36],
                        foot=f"{q_label}  •  Anti-Throttle Stream  •  {audio_label}",
                    )
            elif preload_sec != 0:
                if existing is not None:
                    existing.stop(remove_file=True)
                    if preloader_box is not None:
                        preloader_box[0] = None
                preloader = StreamPreloader(
                    video_id=item.get("id", ""),
                    video_url=video_url,
                    audio_url=audio_url,
                    quality_label=q_label,
                    duration=duration,
                    start_offset=start_offset,
                ).start()
                if preloader_box is not None:
                    preloader_box[0] = preloader

            if preloader is not None:
                rel_offset = max(0.0, start_offset - preloader.base_offset)

                # Wait for pre-buffer gate (ALWAYS waits when starting at rel_offset == 0,
                # including fresh videos AND mid-video quality switches!)
                if not preloader.complete and not preloader.failed:
                    gate_started = time.monotonic()
                    last_ui_at = 0.0
                    flush_input()
                    while running and not preloader.failed:
                        if rel_offset <= 0.1:
                            if preloader.ready_for_playback(preload_sec):
                                break
                        else:
                            if preloader.complete:
                                break
                            if preload_sec > 0 and preloader.estimated_buffered_sec() >= (rel_offset + max(3.0, float(preload_sec))):
                                break
                            if preload_sec > 0 and (time.monotonic() - gate_started) > 6.0 and preloader.estimated_buffered_sec() < rel_offset:
                                break

                        now_p = time.monotonic()
                        # Bail out cleanly to seekable Range proxy if nothing arrived in 12s
                        if preloader.mkv_bytes() == 0 and (now_p - gate_started) > 12.0:
                            preloader.stop(remove_file=True)
                            preloader = None
                            if preloader_box is not None:
                                preloader_box[0] = None
                            break

                        act_p, _ch_p = poll_ui_input(PINS, GPIO)
                        if is_back(act_p):
                            preloader.stop(remove_file=True)
                            if preloader_box is not None:
                                preloader_box[0] = None
                            return ("stop", 0.0)
                        if is_confirm(act_p) and preloader.mkv_bytes() >= 64 * 1024 and rel_offset <= 0.1:
                            break

                        if (now_p - last_ui_at) >= 0.12:
                            mb = preloader.mkv_bytes() / (1024.0 * 1024.0)
                            spd = preloader.speed_kbps()
                            buf_s = preloader.estimated_buffered_sec()
                            if preload_sec < 0:
                                prog_p = preloader.buffered_fraction()
                                card_t = "Preloading Full Video..."
                            else:
                                tgt = max(1.0, (rel_offset + preload_sec) if rel_offset > 0.1 else float(preload_sec))
                                prog_p = min(0.99, buf_s / tgt)
                                card_t = (
                                    f"Switching to {q_label} ({preload_sec}s Buffer)..."
                                    if start_offset > 0.1 and rel_offset <= 0.1
                                    else f"Preloading ({preload_sec}s Buffer)..."
                                )
                            sub_txt = f"{q_label}  •  {mb:.1f} MB ({spd:.0f} KB/s)  •  {buf_s:.0f}s ready"
                            _show_card(
                                card_t, sub_txt, progress=prog_p,
                                foot=format_footer_hint(
                                    W, H,
                                    hat_hint="OK:Play K3:Cancel",
                                    hat_240_hint="OK: Play Now  •  K3: Cancel",
                                    cardputer_hint="Enter: Play Now  •  Esc: Cancel",
                                    hdmi_hint="Enter: Play Now   •   Esc: Cancel",
                                ),
                            )
                            last_ui_at = now_p
                        time.sleep(0.05)

                if preloader is not None and preloader.failed:
                    preloader.stop(remove_file=True)
                    preloader = None
                    if preloader_box is not None:
                        preloader_box[0] = None

                # Automatic fallback to anti-throttle seekable Range proxy (e.g., end-of-file moov atom!)
                if (
                    preloader is None
                    and range_proxy is None
                    and duration >= 0
                    and video_url.startswith(("http://", "https://"))
                    and not _is_hls_or_manifest(video_url)
                    and not _is_hls_or_manifest(audio_url)
                ):
                    range_proxy = _ChunkedRangeProxy(video_url=video_url, audio_url=audio_url).start()
                    _show_card(
                        "Streaming Video...",
                        title[:36],
                        foot=f"{q_label}  •  Anti-Throttle Stream  •  {audio_label}",
                    )

        # Compute effective A/V sync offset:
        #   - Positive (ms): delay video presentation live in Python (great for BT A2DP ~200ms lag!)
        #   - Negative (ms): delay audio in ffmpeg via adelay filter
        is_bt_out = audio_label.upper().startswith("BT")
        base_bt_ms = int(cfg.get("bt_sync_ms", 200)) if is_bt_out else 0
        eff_sync_ms = base_bt_ms + int(cfg.get("av_sync_ms", 0))
        audio_delay_ms = max(0, -eff_sync_ms)
        video_delay_ms = [max(0, eff_sync_ms)]

        # Optional hardware-accelerated mpv engine (GPU/DRM on Pi)
        if cfg.get("player_engine") == "mpv" and is_hdmi and shutil.which("mpv"):
            try:
                mpv_res = _play_with_mpv(
                    item=item,
                    video_url=video_url,
                    audio_url=audio_url,
                    start_offset=start_offset,
                    eff_sync_ms=eff_sync_ms,
                    is_local=is_local,
                    preloader=preloader,
                    range_proxy=range_proxy,
                )
            except Exception:
                mpv_res = ("fallback", start_offset)
            if mpv_res[0] != "fallback":
                if range_proxy is not None:
                    range_proxy.stop()
                return mpv_res

        pix_fmt = "rgb565le" if is_cardputer else "rgb24"
        vid_w, vid_h = compute_video_render_size(
            "LOCAL" if is_local else q_label,
            W,
            H,
            render_scale=cfg.get("render_scale", "auto"),
            is_hdmi=is_hdmi,
        )
        vid_fonts = fonts if (vid_w, vid_h) == (W, H) else load_ui_fonts(vid_w, vid_h)
        frame_bytes = vid_w * vid_h * (2 if pix_fmt == "rgb565le" else 3)
        target_fps = cfg["fps_wide"] if fonts["wide"] else cfg["fps_compact"]

        audio_args = audio_helper.get_ffmpeg_audio_args()
        has_hw_audio_sink = bool(audio_args) and ("null" not in audio_args)

        # Select fastest playback source:
        #   1. Local file: direct file (audio-clock-paced when audio track + real sink exist)
        #   2. Completed preloaded .mkv in RAM: direct file with instant <150ms seek & no -re fighting!
        #   3. Actively growing preloaded .mkv: tail-feed into pipe:0 with audio-clock pacing!
        #      (On mid-video quality switch, rel_offset == 0.0 -> zero decode-skip & zero -re stutter!)
        #   4. Anti-throttle seekable local HTTP Range proxy: O(1) RAM (~6 MB) for long videos,
        #      mid-video seeks, low /dev/shm, or end-of-file moov fallback — audio-clock-paced!
        #   5. Direct fallback (HLS / live streams): STOP any preloader first so it never competes!
        input_is_pipe = False
        use_tail_feeder = False
        rel_offset = max(0.0, start_offset - preloader.base_offset) if preloader is not None else start_offset
        if is_local:
            play_v, play_a = video_url, ""
            play_offset = start_offset
            use_realtime = not (has_hw_audio_sink and _probe_local_has_audio(video_url))
        elif preloader is not None and preloader.complete and os.path.isfile(preloader.mkv_path):
            play_v, play_a = preloader.mkv_path, ""
            play_offset = rel_offset
            use_realtime = not has_hw_audio_sink
        elif (
            preloader is not None
            and not preloader.failed
            and preloader.mkv_bytes() >= 32 * 1024
            and (rel_offset <= 0.1 or preloader.estimated_buffered_sec() >= rel_offset + 2.0)
        ):
            play_v, play_a = "pipe:0", ""
            play_offset = rel_offset
            input_is_pipe = True
            use_tail_feeder = True
            use_realtime = not has_hw_audio_sink
        elif range_proxy is not None and range_proxy.video_proxy_url:
            if preloader is not None:
                preloader.stop(remove_file=True)
                preloader = None
                if preloader_box is not None:
                    preloader_box[0] = None
            play_v, play_a = range_proxy.video_proxy_url, range_proxy.audio_proxy_url
            play_offset = start_offset
            use_realtime = not has_hw_audio_sink
        else:
            # Never leave a background preloader fighting direct HTTP playback for Wi-Fi/CPU!
            if preloader is not None:
                preloader.stop(remove_file=True)
                preloader = None
                if preloader_box is not None:
                    preloader_box[0] = None
            play_v, play_a = video_url, audio_url
            play_offset = start_offset
            use_realtime = True

        cur_speed = float(cfg.get("playback_speed", 1.0))
        cmd = build_video_ffmpeg_cmd(
            video_url=play_v,
            audio_url=play_a,
            width=vid_w,
            height=vid_h,
            pix_fmt=pix_fmt,
            target_fps=target_fps,
            audio_args=audio_args,
            start_offset=play_offset,
            audio_delay_ms=audio_delay_ms,
            use_realtime=use_realtime,
            input_is_pipe=input_is_pipe,
            playback_speed=cur_speed,
        )

        # Always allow dropping to PipeWire/PulseAudio user for pipe:0, HTTP, and 0o644 RAM-cached .mkv
        allow_drop = not is_local
        ff_kw = (
            audio_helper.get_audio_popen_kwargs(cmd, allow_user_drop=allow_drop)
            if hasattr(audio_helper, "get_audio_popen_kwargs")
            else {"env": audio_helper.get_pulse_env()}
        )
        popen_stdin = subprocess.PIPE if input_is_pipe else subprocess.DEVNULL
        # bufsize=0 (unbuffered raw stdout pipe) eliminates multi-frame Python pipe buffering lag!
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=popen_stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                **ff_kw,
            )
        except (PermissionError, OSError):
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=popen_stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    env=audio_helper.get_pulse_env(),
                )
            except Exception:
                if range_proxy is not None:
                    range_proxy.stop()
                raise

        # Expand Linux pipe buffers (stdout to 2-4 MB, stdin pipe to 1-2 MB) so ffmpeg NEVER stalls!
        if proc.stdout is not None:
            try:
                _pfd = proc.stdout.fileno()
                for _psz in (4 * 1024 * 1024, 2 * 1024 * 1024, 1024 * 1024):
                    try:
                        fcntl.fcntl(_pfd, 1031, _psz)  # F_SETPIPE_SZ
                        break
                    except OSError:
                        continue
            except Exception:
                pass
        if input_is_pipe and proc.stdin is not None:
            try:
                _ifd = proc.stdin.fileno()
                for _isz in (2 * 1024 * 1024, 1024 * 1024):
                    try:
                        fcntl.fcntl(_ifd, 1031, _isz)  # F_SETPIPE_SZ
                        break
                    except OSError:
                        continue
            except Exception:
                pass

        feed_stop = threading.Event()
        if use_tail_feeder and preloader is not None and proc.stdin is not None:
            preloader.start_tail_feeder(proc.stdin, feed_stop)

        # Background stderr drainer so ffmpeg NEVER deadlocks on a full 64 KB stderr pipe
        stderr_tail: collections.deque = collections.deque(maxlen=16)

        def _stderr_drainer() -> None:
            try:
                se = proc.stderr
                if se is None:
                    return
                for raw_ln in se:
                    txt = raw_ln.decode("utf-8", "ignore").strip()
                    if txt:
                        stderr_tail.append(txt)
            except Exception:
                pass

        threading.Thread(target=_stderr_drainer, daemon=True).start()

        # Zero-GC pre-allocated frame buffer pool + instant Event wakeup + live video delay queue
        pool_size = 10
        frame_lock = threading.Lock()
        free_bufs: collections.deque = collections.deque(bytearray(frame_bytes) for _ in range(pool_size))
        frame_queue: collections.deque = collections.deque()
        frame_queue_max = 12
        frame_event = threading.Event()
        reader_done = threading.Event()
        frame_seq = [0]

        def _frame_reader() -> None:
            try:
                stdout = proc.stdout
                if stdout is None:
                    return
                while True:
                    with frame_lock:
                        buf = free_bufs.popleft() if free_bufs else bytearray(frame_bytes)
                    mv = memoryview(buf)
                    got = 0
                    while got < frame_bytes:
                        n = stdout.readinto(mv[got:])
                        if not n:
                            return
                        got += n
                    ts_now = time.monotonic()
                    with frame_lock:
                        frame_seq[0] += 1
                        while len(frame_queue) >= frame_queue_max:
                            _ot, _os, old_b = frame_queue.popleft()
                            if len(free_bufs) < pool_size:
                                free_bufs.append(old_b)
                        frame_queue.append((ts_now, frame_seq[0], buf))
                    frame_event.set()
            except Exception:
                pass
            finally:
                reader_done.set()
                frame_event.set()

        reader_t = threading.Thread(target=_frame_reader, daemon=True)
        reader_t.start()

        # Optional HDMI direct SHM import (avoids per-frame import & keeps FLAG_NO_HUD consistent)
        _hdmi_shm = None
        if is_hdmi:
            try:
                import hdmi_shm as _hdmi_shm
            except Exception:
                _hdmi_shm = None

        # Optional CardputerZero direct fb mmap
        fb_fd = None
        fb_map = None
        if is_cardputer:
            fb_dev = "/dev/fb1" if os.path.exists("/dev/fb1") else "/dev/fb0"
            try:
                fb_fd = os.open(fb_dev, os.O_RDWR)
                fb_map = mmap.mmap(fb_fd, frame_bytes, mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)
            except Exception:
                fb_map = None

        play_started = time.monotonic()
        base_media_offset = start_offset
        paused = False
        pause_at = 0.0
        osd_until = time.monotonic() + (3.2 if cfg["show_osd"] else 0.0)
        osd_pinned = False
        toast = ""
        toast_until = 0.0
        liked_now = (not is_local) and is_liked(item.get("id", ""))
        loop_one = False
        captions_on = bool((not is_local) and cfg.get("auto_captions"))
        sub_cues_box: List[list] = [[]]
        details_box: List[dict] = [{}]
        last_drawn_seq = -1
        last_drawn_raw = None
        paused_drawn = False
        result: Tuple[str, float] = ("next", 0.0)
        flush_input()

        # Fetch chapters & metadata asynchronously AFTER startup settles (avoids CPU/Wi-Fi
        # contention during the first 4.5s while ffmpeg probes & decodes initial frames!).
        play_stop = threading.Event()
        if not is_local and item.get("id"):
            def _bg_meta(vid_id=item["id"], want_caps=captions_on) -> None:
                nonlocal duration
                if play_stop.wait(4.5):
                    return
                det = fetch_video_details(vid_id)
                if play_stop.is_set():
                    return
                if det:
                    details_box[0] = det
                    if duration <= 0 and int(det.get("duration") or 0) > 0:
                        duration = int(det["duration"])
                        item["duration"] = duration
                if want_caps and not play_stop.is_set():
                    cues, _vp, _err = fetch_video_subtitles(vid_id)
                    if cues and not play_stop.is_set():
                        sub_cues_box[0] = cues
            threading.Thread(target=_bg_meta, daemon=True).start()

        def _bump_osd(msg: str = "", secs: float = 3.0) -> None:
            nonlocal osd_until, toast, toast_until, paused_drawn
            osd_until = time.monotonic() + secs
            paused_drawn = False
            if msg:
                toast = msg
                toast_until = time.monotonic() + min(secs, 2.2)

        def _cur_elapsed() -> float:
            wall = (pause_at - play_started) if paused else (time.monotonic() - play_started)
            return max(0.0, base_media_offset + wall * cur_speed)

        def _buf_osd_info() -> Tuple[float, str]:
            if is_local:
                return 1.0, ""
            if range_proxy is not None:
                return 0.0, "STREAM"
            if preloader is None or preloader.failed:
                return 0.0, ""
            bf = preloader.buffered_fraction()
            if preloader.complete:
                return 1.0, "CACHED"
            if bf > 0.0:
                return bf, f"BUF {int(bf * 100)}%"
            return 0.0, ""

        try:
            while running:
                now = time.monotonic()
                if reader_done.is_set() and not paused:
                    if last_drawn_seq < 0 and proc.poll() not in (None, 0):
                        err_line = stderr_tail[-1] if stderr_tail else "ffmpeg failed"
                        _show_card("Playback Error", err_line[:40], kind="err")
                        time.sleep(1.8)
                    elif loop_one:
                        result = ("seek", 0.0)
                    break

                act, ch = poll_ui_input(PINS, GPIO)
                kl = ch.lower() if isinstance(ch, str) else ""

                if is_back(act):
                    result = ("stop", _cur_elapsed())
                    break
                elif kl == "n":
                    result = ("next", 0.0)
                    break
                elif kl == "b":
                    result = ("prev", 0.0)
                    break
                elif act == "RIGHT":
                    target_t = _cur_elapsed() + 15.0
                    if duration > 0:
                        target_t = min(max(0.0, duration - 3.0), target_t)
                    result = ("seek", target_t)
                    break
                elif act == "LEFT":
                    target_t = max(0.0, _cur_elapsed() - 15.0)
                    result = ("seek", target_t)
                    break
                elif act == "PAGEDOWN":
                    target_t = _cur_elapsed() + 60.0
                    if duration > 0:
                        target_t = min(max(0.0, duration - 3.0), target_t)
                    result = ("seek", target_t)
                    break
                elif act == "PAGEUP":
                    result = ("seek", max(0.0, _cur_elapsed() - 60.0))
                    break
                elif ch in ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9") and duration > 0:
                    target_t = duration * (int(ch) / 10.0)
                    result = ("seek", min(max(0.0, duration - 3.0), target_t))
                    break
                elif ch == "<":
                    chaps = details_box[0].get("chapters") or []
                    t_ch = get_adjacent_chapter_time(chaps, _cur_elapsed(), -1)
                    if t_ch is not None:
                        result = ("seek", t_ch)
                        break
                    _bump_osd("No previous chapter")
                elif ch == ">":
                    chaps = details_box[0].get("chapters") or []
                    t_ch = get_adjacent_chapter_time(chaps, _cur_elapsed(), 1)
                    if t_ch is not None:
                        result = ("seek", t_ch)
                        break
                    _bump_osd("No next chapter")
                elif kl in ("z", "x"):
                    step_d = 1 if kl == "x" else -1
                    idx_sp = SPEED_OPTIONS.index(cur_speed) if cur_speed in SPEED_OPTIONS else 2
                    nxt_sp = SPEED_OPTIONS[(idx_sp + step_d) % len(SPEED_OPTIONS)]
                    cfg["playback_speed"] = nxt_sp
                    save_settings(cfg)
                    result = ("seek", _cur_elapsed())
                    break
                elif kl == "r":
                    loop_one = not loop_one
                    _bump_osd(f"Loop Video: {'ON' if loop_one else 'OFF'}")
                elif kl == "t" and not is_local:
                    if not captions_on:
                        if not sub_cues_box[0]:
                            _bump_osd("Loading English captions...")
                            cues, _vp, err_s = fetch_video_subtitles(item.get("id", ""))
                            if cues:
                                sub_cues_box[0] = cues
                                captions_on = True
                                _bump_osd(f"Captions ON ({len(cues)} lines)")
                            else:
                                _bump_osd(err_s or "No captions available")
                        else:
                            captions_on = True
                            _bump_osd("Captions ON")
                    else:
                        captions_on = False
                        _bump_osd("Captions OFF")
                elif kl == "c" and not is_local:
                    chaps = details_box[0].get("chapters") or []
                    if not chaps:
                        _bump_osd("No chapters in this video")
                    else:
                        cur_t = _cur_elapsed()
                        try:
                            proc.send_signal(signal.SIGSTOP)
                        except Exception:
                            pass
                        ch_opts = [(f"{format_duration(int(c['start']))}  {c['title']}", float(c["start"])) for c in chaps]
                        picked_t = _choose_from_modal("CHAPTERS", f"{len(chaps)} chapters", ch_opts)
                        if picked_t is not None:
                            result = ("seek", float(picked_t))
                            break
                        try:
                            proc.send_signal(signal.SIGCONT)
                        except Exception:
                            pass
                        play_started += (time.monotonic() - now)
                elif kl == "k" and last_drawn_raw is not None:
                    try:
                        ss_dir = os.path.join(rj_env.get_base_dir(), "loot", "screenshots")
                        os.makedirs(ss_dir, exist_ok=True)
                        if pix_fmt == "rgb24":
                            snap = Image.frombytes("RGB", (vid_w, vid_h), bytes(last_drawn_raw))
                        else:
                            import numpy as np
                            arr_s = np.frombuffer(last_drawn_raw, dtype="<u2").reshape((vid_h, vid_w))
                            snap = Image.fromarray(np.stack([(((arr_s >> 11) & 0x1F) << 3).astype(np.uint8), (((arr_s >> 5) & 0x3F) << 2).astype(np.uint8), ((arr_s & 0x1F) << 3).astype(np.uint8)], axis=-1), "RGB")
                        fn_s = f"yt_{sanitize_yt_name(item.get('id', 'frame'), 16)}_{int(time.time())}.png"
                        snap.save(os.path.join(ss_dir, fn_s))
                        _bump_osd(f"Saved: {fn_s}")
                    except Exception:
                        _bump_osd("Screenshot failed")
                elif kl == "w" and not is_local:
                    in_w = toggle_watch_later(item)
                    _bump_osd("Added to Watch Later" if in_w else "Removed from Watch Later")
                elif kl == "f" and not is_local:
                    ch_nm = str(details_box[0].get("channel") or item.get("channel") or "")
                    if ch_nm:
                        sub_on = toggle_subscription(
                            ch_nm,
                            details_box[0].get("channel_url") or item.get("channel_url", ""),
                            details_box[0].get("channel_id") or item.get("channel_id", ""),
                        )
                        _bump_osd(f"Subscribed: {ch_nm[:22]}" if sub_on else f"Unsubscribed: {ch_nm[:20]}")
                elif kl == "a" and not is_local:
                    try:
                        proc.send_signal(signal.SIGSTOP)
                    except Exception:
                        pass
                    _add_to_video_playlist_flow(item)
                    try:
                        proc.send_signal(signal.SIGCONT)
                    except Exception:
                        pass
                    play_started += (time.monotonic() - now)
                elif kl == "v" and not is_local:
                    try:
                        proc.send_signal(signal.SIGSTOP)
                    except Exception:
                        pass
                    _open_video_info_modal(item)
                    try:
                        proc.send_signal(signal.SIGCONT)
                    except Exception:
                        pass
                    play_started += (time.monotonic() - now)
                elif act == "UP" or is_step_up(ch):
                    _set_vol(vol_pct + 5)
                    _bump_osd(f"Volume: {vol_pct}%")
                elif act == "DOWN" or is_step_down(ch):
                    _set_vol(vol_pct - 5)
                    _bump_osd(f"Volume: {vol_pct}%")
                elif kl == "m":
                    _toggle_mute()
                    _bump_osd("Muted" if muted else f"Unmuted ({vol_pct}%)")
                elif is_action(act, ch, "KEY2", "i"):
                    osd_pinned = not osd_pinned
                    osd_until = now + (3600.0 if osd_pinned else 0.0)
                elif is_action(act, ch, "KEY1", "o"):
                    cur_t = _cur_elapsed()
                    try:
                        proc.send_signal(signal.SIGSTOP)
                    except Exception:
                        pass
                    p_opts: List[Tuple[str, object]] = []
                    if not is_local:
                        p_opts.append((f"Stream Quality: {q_label}", "quality"))
                    p_opts.append((f"Playback Speed: {cur_speed:g}x", "speed"))
                    if not is_local:
                        p_opts.append((f"Captions (CC): {'ON' if captions_on else 'OFF'}", "cc"))
                    p_opts.extend([
                        (f"Loop Video: {'ON' if loop_one else 'OFF'}", "loop"),
                        (f"Mute: {'ON' if muted else 'OFF'}", "mute"),
                    ])
                    if not is_local:
                        p_opts.extend([
                            ("Unlike Video" if liked_now else "Like Video", "like"),
                            ("Remove Watch Later" if is_in_watch_later(item.get("id", "")) else "Add to Watch Later", "wl"),
                            ("Add to Playlist...", "pl"),
                            ("Download (MP4 / MP3)...", "dl"),
                        ])
                        if details_box[0].get("chapters"):
                            p_opts.append(("Jump to Chapter...", "chap"))
                        p_opts.append(("Video Info & Channel", "info"))
                    if playlist_pos:
                        p_opts.extend([("Next Video", "next"), ("Previous Video", "prev")])
                    p_opts.extend([("Audio & Settings...", "settings"), ("Resume", None)])
                    pa = _choose_from_modal("PLAYBACK ACTIONS", title[:24], p_opts)
                    if pa == "quality":
                        if _pick_stream_quality():
                            result = ("seek", cur_t)
                            break
                    elif pa == "speed":
                        sp_opts = [(f"{s:g}x" + ("  [ACTIVE]" if abs(s - cur_speed) < 0.01 else ""), s) for s in SPEED_OPTIONS]
                        picked_sp = _choose_from_modal("PLAYBACK SPEED", "Applies immediately", sp_opts)
                        if picked_sp is not None and abs(float(picked_sp) - cur_speed) > 0.01:
                            cfg["playback_speed"] = float(picked_sp)
                            save_settings(cfg)
                            result = ("seek", cur_t)
                            break
                    elif pa == "cc" and not is_local:
                        if not captions_on:
                            if not sub_cues_box[0]:
                                cues, _vp, err_s = fetch_video_subtitles(item.get("id", ""))
                                if cues:
                                    sub_cues_box[0] = cues
                                    captions_on = True
                                    _bump_osd(f"Captions ON ({len(cues)})")
                                else:
                                    _bump_osd(err_s or "No captions")
                            else:
                                captions_on = True
                                _bump_osd("Captions ON")
                        else:
                            captions_on = False
                            _bump_osd("Captions OFF")
                    elif pa == "loop":
                        loop_one = not loop_one
                        _bump_osd(f"Loop: {'ON' if loop_one else 'OFF'}")
                    elif pa == "mute":
                        _toggle_mute()
                        _bump_osd("Muted" if muted else f"Unmuted ({vol_pct}%)")
                    elif pa == "like" and not is_local:
                        liked_now = toggle_like_video(item)
                        _bump_osd("Added to Liked" if liked_now else "Removed from Liked")
                    elif pa == "wl" and not is_local:
                        in_w = toggle_watch_later(item)
                        _bump_osd("Added to Watch Later" if in_w else "Removed from Watch Later")
                    elif pa == "pl" and not is_local:
                        _add_to_video_playlist_flow(item)
                    elif pa == "dl" and not is_local:
                        _open_download_menu(item)
                    elif pa == "chap":
                        chaps = details_box[0].get("chapters") or []
                        ch_opts = [(f"{format_duration(int(c['start']))}  {c['title']}", float(c["start"])) for c in chaps]
                        picked_t = _choose_from_modal("CHAPTERS", f"{len(chaps)} chapters", ch_opts)
                        if picked_t is not None:
                            result = ("seek", float(picked_t))
                            break
                    elif pa == "info" and not is_local:
                        _open_video_info_modal(item)
                    elif pa == "next":
                        result = ("next", 0.0)
                        break
                    elif pa == "prev":
                        result = ("prev", 0.0)
                        break
                    elif pa == "settings":
                        old_q_s = cfg["quality_idx"]
                        old_sp_s = float(cfg.get("playback_speed", 1.0))
                        _open_settings_menu()
                        if cfg["quality_idx"] != old_q_s or abs(float(cfg.get("playback_speed", 1.0)) - old_sp_s) > 0.01:
                            result = ("seek", cur_t)
                            break
                    try:
                        proc.send_signal(signal.SIGCONT)
                    except Exception:
                        pass
                    play_started += (time.monotonic() - now)
                elif kl == ",":
                    cfg["av_sync_ms"] = max(-500, int(cfg.get("av_sync_ms", 0)) - 50)
                    save_settings(cfg)
                    new_eff = (int(cfg.get("bt_sync_ms", 200)) if is_bt_out else 0) + cfg["av_sync_ms"]
                    if new_eff < 0:
                        result = ("seek", _cur_elapsed())
                        break
                    video_delay_ms[0] = new_eff
                    _bump_osd(f"A/V Sync: {new_eff:+d}ms (live)")
                elif kl == ".":
                    cfg["av_sync_ms"] = min(800, int(cfg.get("av_sync_ms", 0)) + 50)
                    save_settings(cfg)
                    new_eff = (int(cfg.get("bt_sync_ms", 200)) if is_bt_out else 0) + cfg["av_sync_ms"]
                    if audio_delay_ms > 0 and new_eff >= 0:
                        result = ("seek", _cur_elapsed())
                        break
                    video_delay_ms[0] = max(0, new_eff)
                    _bump_osd(f"A/V Sync: {new_eff:+d}ms (live)")
                elif act == "OK" or ch == " ":
                    paused = not paused
                    if paused:
                        pause_at = now
                        try:
                            proc.send_signal(signal.SIGSTOP)
                        except Exception:
                            pass
                        _bump_osd("Paused", secs=3600.0)
                    else:
                        pause_dt = now - pause_at
                        play_started += pause_dt
                        with frame_lock:
                            if frame_queue:
                                shifted = [(t + pause_dt, sq, raw_b) for (t, sq, raw_b) in frame_queue]
                                frame_queue.clear()
                                frame_queue.extend(shifted)
                        try:
                            proc.send_signal(signal.SIGCONT)
                        except Exception:
                            pass
                        osd_until = now + (3600.0 if osd_pinned else 2.2)
                        toast = ""
                elif kl == "l" and not is_local:
                    liked_now = toggle_like_video(item)
                    _bump_osd("Added to Liked" if liked_now else "Removed from Liked")
                elif kl == "s" and not is_local:
                    cur_t = _cur_elapsed()
                    try:
                        proc.send_signal(signal.SIGSTOP)
                    except Exception:
                        pass
                    changed = _pick_stream_quality()
                    if changed:
                        result = ("seek", cur_t)
                        break
                    try:
                        proc.send_signal(signal.SIGCONT)
                    except Exception:
                        pass
                    play_started += (time.monotonic() - now)
                elif kl == "d" and not is_local:
                    try:
                        proc.send_signal(signal.SIGSTOP)
                    except Exception:
                        pass
                    _open_download_menu(item)
                    try:
                        proc.send_signal(signal.SIGCONT)
                    except Exception:
                        pass
                    play_started += (time.monotonic() - now)

                # Present newest due frame (drops any stale backlog & recycles buffers to pool!)
                show_osd = paused or osd_pinned or (now < osd_until)
                active_sub = get_active_subtitle(sub_cues_box[0], _cur_elapsed()) if (captions_on and sub_cues_box[0]) else ""
                need_pil = show_osd or bool(active_sub)
                delay_s = video_delay_ms[0] / 1000.0
                cur_seq = last_drawn_seq
                raw_frame = None
                next_due_wait = 0.015
                with frame_lock:
                    if frame_queue:
                        if delay_s <= 0.0:
                            while len(frame_queue) > 1:
                                _dt, _ds, drop_b = frame_queue.popleft()
                                if len(free_bufs) < pool_size:
                                    free_bufs.append(drop_b)
                            _ts, q_seq, q_raw = frame_queue.popleft()
                            if q_seq != last_drawn_seq:
                                cur_seq, raw_frame = q_seq, q_raw
                            elif len(free_bufs) < pool_size:
                                free_bufs.append(q_raw)
                        else:
                            while frame_queue and (frame_queue[0][0] + delay_s) <= now:
                                _ts, q_seq, q_raw = frame_queue.popleft()
                                if raw_frame is not None and len(free_bufs) < pool_size:
                                    free_bufs.append(raw_frame)
                                cur_seq, raw_frame = q_seq, q_raw
                            if cur_seq == last_drawn_seq:
                                if raw_frame is not None and len(free_bufs) < pool_size:
                                    free_bufs.append(raw_frame)
                                raw_frame = None
                            if frame_queue:
                                next_due_wait = max(0.002, min(0.015, (frame_queue[0][0] + delay_s) - now))

                if raw_frame is not None:
                    prev_raw = last_drawn_raw
                    last_drawn_seq = cur_seq
                    last_drawn_raw = raw_frame
                    if prev_raw is not None and prev_raw is not raw_frame:
                        with frame_lock:
                            if len(free_bufs) < pool_size:
                                free_bufs.append(prev_raw)
                    paused_drawn = False
                    if pix_fmt == "rgb565le" and fb_map is not None and not need_pil:
                        fb_map.seek(0)
                        fb_map.write(raw_frame)
                    elif pix_fmt == "rgb24" and is_hdmi and not need_pil and _hdmi_shm is not None:
                        _hdmi_shm.publish_frame(raw_frame, vid_w, vid_h, flags=_hdmi_shm.FLAG_NO_HUD)
                    elif pix_fmt == "rgb24" and is_hdmi and not need_pil and hasattr(lcd, "show_raw_frame"):
                        lcd.show_raw_frame(raw_frame, vid_w, vid_h, fmt="rgb24")
                    else:
                        if pix_fmt == "rgb24":
                            pil_f = Image.frombytes("RGB", (vid_w, vid_h), bytes(raw_frame))
                        else:
                            import numpy as np
                            arr = np.frombuffer(raw_frame, dtype="<u2").reshape((vid_h, vid_w))
                            r = ((arr >> 11) & 0x1F) << 3
                            g = ((arr >> 5) & 0x3F) << 2
                            b = (arr & 0x1F) << 3
                            pil_f = Image.fromarray(np.stack([r.astype(np.uint8), g.astype(np.uint8), b.astype(np.uint8)], axis=-1), "RGB")
                        if show_osd:
                            bf, bb = _buf_osd_info()
                            pil_f = render_video_osd(
                                pil_f, title=title, elapsed=_cur_elapsed(), duration=duration,
                                volume_pct=vol_pct, muted=muted, paused=paused,
                                quality_label=("LOCAL" if is_local else q_label),
                                audio_label=audio_label, liked=liked_now,
                                toast=(toast if now < toast_until else ""),
                                playlist_pos=playlist_pos, fonts=vid_fonts,
                                buffered_frac=bf, buffer_badge=bb,
                                chapters=details_box[0].get("chapters"),
                                speed=cur_speed, loop_one=loop_one, captions_on=captions_on,
                            )
                        if active_sub:
                            pil_f = render_subtitle_overlay(pil_f, active_sub, osd_visible=show_osd, fonts=vid_fonts)
                        if is_hdmi and _hdmi_shm is not None:
                            _hdmi_shm.publish_pil_image(pil_f, flags=_hdmi_shm.FLAG_NO_HUD)
                        else:
                            lcd.LCD_ShowImage(pil_f, 0, 0)
                elif paused and last_drawn_raw is not None:
                    if not paused_drawn:
                        if pix_fmt == "rgb24":
                            pil_base = Image.frombytes("RGB", (vid_w, vid_h), bytes(last_drawn_raw))
                        else:
                            import numpy as np
                            arr = np.frombuffer(last_drawn_raw, dtype="<u2").reshape((vid_h, vid_w))
                            r = ((arr >> 11) & 0x1F) << 3
                            g = ((arr >> 5) & 0x3F) << 2
                            b = (arr & 0x1F) << 3
                            pil_base = Image.fromarray(np.stack([r.astype(np.uint8), g.astype(np.uint8), b.astype(np.uint8)], axis=-1), "RGB")
                        bf, bb = _buf_osd_info()
                        pil_p = render_video_osd(
                            pil_base, title=title, elapsed=_cur_elapsed(), duration=duration,
                            volume_pct=vol_pct, muted=muted, paused=True,
                            quality_label=("LOCAL" if is_local else q_label),
                            audio_label=audio_label, liked=liked_now,
                            playlist_pos=playlist_pos, fonts=vid_fonts,
                            buffered_frac=bf, buffer_badge=bb,
                            chapters=details_box[0].get("chapters"),
                            speed=cur_speed, loop_one=loop_one, captions_on=captions_on,
                        )
                        if active_sub:
                            pil_p = render_subtitle_overlay(pil_p, active_sub, osd_visible=True, fonts=vid_fonts)
                        if is_hdmi and _hdmi_shm is not None:
                            _hdmi_shm.publish_pil_image(pil_p, flags=_hdmi_shm.FLAG_NO_HUD)
                        else:
                            lcd.LCD_ShowImage(pil_p, 0, 0)
                        paused_drawn = True
                    time.sleep(0.04)
                    continue

                frame_event.wait(timeout=next_due_wait)
                frame_event.clear()
        finally:
            play_stop.set()
            feed_stop.set()
            try:
                proc.send_signal(signal.SIGCONT)
            except Exception:
                pass
            _kill_proc(proc)
            if range_proxy is not None:
                try:
                    range_proxy.stop()
                except Exception:
                    pass
            try:
                if fb_map is not None:
                    fb_map.close()
            except Exception:
                pass
            try:
                if fb_fd is not None:
                    os.close(fb_fd)
            except Exception:
                pass
            if not is_hdmi:
                lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
        return result

    def _play_video_playlist(items: List[dict], start_idx: int = 0) -> None:
        idx = start_idx
        offset = 0.0
        first_prompt = True
        preloader_box: List[Optional[StreamPreloader]] = [None]
        try:
            while 0 <= idx < len(items) and running:
                cur_item = items[idx]
                if first_prompt and offset <= 0.0 and not cur_item.get("local") and cur_item.get("id"):
                    saved_pos = get_resume_position(cur_item["id"])
                    if saved_pos >= 10.0:
                        ans = _choose_from_modal("RESUME PLAYBACK?", cur_item.get("title", "")[:28], [
                            (f"Resume from {format_duration(int(saved_pos))}", "resume"),
                            ("Start from Beginning (0:00)", "start"),
                        ])
                        if ans == "resume":
                            offset = saved_pos
                        elif ans is None:
                            break
                first_prompt = False
                pos = f"{idx + 1}/{len(items)}" if len(items) > 1 else ""
                nxt = items[idx + 1] if (cfg["auto_next"] and idx + 1 < len(items)) else None
                act, new_off = _play_video_item(
                    cur_item,
                    start_offset=offset,
                    playlist_pos=pos,
                    preloader_box=preloader_box,
                    next_item=nxt,
                )
                offset = 0.0
                if act == "stop":
                    if not cur_item.get("local") and cur_item.get("id"):
                        save_resume_position(cur_item["id"], new_off, int(cur_item.get("duration") or 0))
                    break
                elif act == "seek":
                    offset = new_off
                    continue
                # Leaving current video -> clear/update resume & release RAM preload file
                if not cur_item.get("local") and cur_item.get("id") and act == "next":
                    save_resume_position(cur_item["id"], float(cur_item.get("duration") or 0), int(cur_item.get("duration") or 0))
                if preloader_box[0] is not None:
                    preloader_box[0].stop(remove_file=True)
                    preloader_box[0] = None
                if act == "prev":
                    idx = max(0, idx - 1)
                elif act == "next":
                    if cfg["auto_next"] and idx + 1 < len(items):
                        idx += 1
                    else:
                        break
                else:
                    break
        finally:
            if preloader_box[0] is not None:
                preloader_box[0].stop(remove_file=True)
                preloader_box[0] = None

    # ------------------------------------------------------------------
    # Music (Audio-Only) Playback Engine
    # ------------------------------------------------------------------

    def _play_music_playlist(items: List[dict], start_idx: int = 0) -> None:
        nonlocal audio_label
        idx = start_idx
        shuffle = False
        loop_one = False
        offset = 0.0

        while 0 <= idx < len(items) and running:
            track = items[idx]
            is_local = bool(track.get("local"))
            audio_label = audio_helper.get_active_audio_label()
            _show_card("Loading Audio...", track.get("title", "")[:36], foot=audio_label)

            if is_local:
                src_url = track["path"]
            else:
                src_url, err = resolve_audio_url(track["id"])
                if not src_url:
                    _show_card("Audio Error", err or "Unavailable", kind="err")
                    time.sleep(1.5)
                    idx += 1
                    continue

            audio_args = audio_helper.get_ffmpeg_audio_args()
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re"]
            if offset > 0:
                cmd += ["-ss", f"{offset:.1f}"]
            cmd += ["-i", src_url, "-vn", "-af", "aresample=async=1", "-ac", "2", "-ar", "44100"] + audio_args

            ff_kw = (
                audio_helper.get_audio_popen_kwargs(cmd, allow_user_drop=not is_local)
                if hasattr(audio_helper, "get_audio_popen_kwargs")
                else {"env": audio_helper.get_pulse_env()}
            )
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    **ff_kw,
                )
            except (PermissionError, OSError):
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    env=audio_helper.get_pulse_env(),
                )

            started = time.monotonic() - offset
            offset = 0.0
            paused = False
            pause_at = 0.0
            liked_now = (not is_local) and is_liked(track.get("id", ""))
            toast = ""
            toast_until = 0.0
            action = "next"
            flush_input()

            def _cur_el() -> float:
                return (pause_at - started) if paused else (time.monotonic() - started)

            last_dash_at = 0.0
            dash_dirty = True

            try:
                while running:
                    now = time.monotonic()
                    if proc.poll() is not None and not paused:
                        if proc.returncode not in (None, 0) and (now - started) < 2.5:
                            err_b = (proc.stderr.read(240).decode("utf-8", "ignore") if proc.stderr else "").strip()
                            _show_card("Audio Output Error", (err_b.splitlines()[-1] if err_b else "ffmpeg audio failed")[:42], kind="err")
                            time.sleep(2.0)
                            action = "stop"
                        else:
                            action = "repeat" if loop_one else "next"
                        break

                    act, ch = poll_ui_input(PINS, GPIO)
                    kl = ch.lower() if isinstance(ch, str) else ""
                    if act is not None or ch is not None:
                        dash_dirty = True

                    if act in ("KEY3", "ESCAPE", "BACKSPACE") or kl == "q":
                        action = "stop"
                        break
                    elif act == "RIGHT" or kl == "n":
                        action = "next"
                        break
                    elif act == "LEFT" or kl == "b":
                        action = "prev"
                        break
                    elif kl in (".", "k"):
                        offset = _cur_el() + 15.0
                        action = "seek"
                        break
                    elif kl in (",", "j"):
                        offset = max(0.0, _cur_el() - 15.0)
                        action = "seek"
                        break
                    elif act == "UP" or ch in ("+", "="):
                        _set_vol(vol_pct + 5)
                        toast, toast_until = f"Volume: {vol_pct}%", now + 2.0
                    elif act == "DOWN" or ch in ("-", "_"):
                        _set_vol(vol_pct - 5)
                        toast, toast_until = f"Volume: {vol_pct}%", now + 2.0
                    elif kl == "m":
                        _toggle_mute()
                        toast, toast_until = ("Muted" if muted else f"Unmuted ({vol_pct}%)"), now + 2.0
                    elif act in ("OK", "ENTER", "KEY1") or kl in (" ", "p"):
                        paused = not paused
                        if paused:
                            pause_at = now
                            try:
                                proc.send_signal(signal.SIGSTOP)
                            except Exception:
                                pass
                        else:
                            started += (now - pause_at)
                            try:
                                proc.send_signal(signal.SIGCONT)
                            except Exception:
                                pass
                    elif kl == "h":
                        shuffle = not shuffle
                        toast, toast_until = f"Shuffle: {'ON' if shuffle else 'OFF'}", now + 2.0
                    elif kl == "r":
                        loop_one = not loop_one
                        toast, toast_until = f"Loop Track: {'ON' if loop_one else 'OFF'}", now + 2.0
                    elif kl == "l" and not is_local:
                        liked_now = toggle_like_video(track)
                        toast, toast_until = ("Added to Liked" if liked_now else "Removed from Liked"), now + 2.0
                    elif kl == "d" and not is_local:
                        try:
                            proc.send_signal(signal.SIGSTOP)
                        except Exception:
                            pass
                        _download_media(track["id"], track.get("title", track["id"]), "mp3", "320 kbps", "320")
                        try:
                            proc.send_signal(signal.SIGCONT)
                        except Exception:
                            pass
                        started += (time.monotonic() - now)

                    if thumbs.updated:
                        thumbs.updated = False
                        dash_dirty = True
                    if dash_dirty or (not paused and (now - last_dash_at) >= 0.125):
                        th = None if is_local else thumbs.get(track.get("id", ""), track.get("thumb", ""))
                        dash = render_music_dashboard(
                            W, H, track=track, idx=idx, total=len(items),
                            elapsed=_cur_el(), volume_pct=vol_pct, muted=muted, paused=paused,
                            liked=liked_now, shuffle=shuffle, loop_one=loop_one,
                            audio_label=audio_label, thumb_img=th,
                            toast=(toast if now < toast_until else ""),
                            tick=now, fonts=fonts,
                        )
                        lcd.LCD_ShowImage(dash, 0, 0)
                        last_dash_at = now
                        dash_dirty = False
                    time.sleep(0.04)
            finally:
                try:
                    proc.send_signal(signal.SIGCONT)
                except Exception:
                    pass
                _kill_proc(proc)

            if action == "stop":
                break
            elif action in ("seek", "repeat"):
                continue
            elif action == "prev":
                idx = (idx - 1) % len(items)
            else:
                if shuffle and len(items) > 1:
                    choices = [i for i in range(len(items)) if i != idx]
                    idx = random.choice(choices)
                else:
                    idx += 1

    # ------------------------------------------------------------------
    # Browse Results / Liked / Downloads Helper
    # ------------------------------------------------------------------

    def _browse_media_list(
        title: str,
        items: List[dict],
        subtitle: str = "",
        search_query: str = "",
        music_mode: bool = False,
        allow_delete_liked: bool = False,
        allow_delete_local: bool = False,
        playlist_id: str = "",
        playlist_name: str = "",
        playlist_url: str = "",
    ) -> None:
        base_items = list(items)
        sort_by = "relevance"
        dur_filter = "any"
        dl_folder: Optional[str] = None
        dl_kind = "all"
        cursor = 0
        scroll = 0
        dirty = True
        liked_ids = {v.get("id") for v in load_liked()}
        wl_ids = {v.get("id") for v in next((p.get("items", []) for p in load_video_playlists() if p.get("id") == WATCH_LATER_ID), [])}
        resume_map = load_resume_map()
        last_active_at = time.monotonic()
        flush_input()

        def _filtered_view() -> Tuple[List[dict], str]:
            if allow_delete_local:
                cur = list_downloads(folder=dl_folder, kind_filter=dl_kind)
                parts = []
                if dl_folder:
                    parts.append(dl_folder[:12])
                if dl_kind != "all":
                    parts.append(dl_kind.upper())
                return cur, (" ".join(parts) if parts else "")
            if sort_by == "relevance" and dur_filter == "any":
                return base_items, ""
            filt = apply_search_filters(base_items, sort_by=sort_by, duration_filter=dur_filter)
            fb = []
            if sort_by != "relevance":
                fb.append(sort_by.upper()[:6])
            if dur_filter != "any":
                fb.append(dur_filter.upper()[:5])
            return filt, "/".join(fb)

        while running:
            view_items, f_badge = _filtered_view()
            total = len(view_items) + (1 if (search_query and base_items) else 0)
            hdr_h = 30 if fonts["large"] else (24 if fonts["wide"] else 18)
            ftr_h = 22 if fonts["large"] else (18 if fonts["wide"] else 13)
            row_h = 38 if fonts["large"] else (26 if fonts["wide"] else 18)
            vis = max(1, (H - hdr_h - ftr_h - 9) // row_h)

            if total > 0:
                cursor = max(0, min(cursor, total - 1))
                if cursor < scroll:
                    scroll = cursor
                elif cursor >= scroll + vis:
                    scroll = cursor - vis + 1
            else:
                cursor = 0
                scroll = 0

            if thumbs.updated:
                thumbs.updated = False
                dirty = True

            if dirty:
                cur_thumb = None
                if cursor < len(view_items) and not view_items[cursor].get("local"):
                    cur_thumb = thumbs.get(view_items[cursor]["id"], view_items[cursor].get("thumb", ""))
                frame = render_results_view(
                    W, H, title=title, subtitle=subtitle, items=view_items,
                    cursor=cursor, scroll=scroll, has_load_more=bool(search_query),
                    liked_ids=liked_ids, thumb_img=cur_thumb, music_mode=music_mode,
                    audio_label=audio_label, resume_map=resume_map,
                    watch_later_ids=wl_ids, filter_badge=f_badge,
                    is_playlist=bool(playlist_id or playlist_name),
                    fonts=fonts,
                )
                lcd.LCD_ShowImage(frame, 0, 0)
                dirty = False

            act, ch = poll_ui_input(PINS, GPIO)
            if act is None and ch is None:
                time.sleep(0.025 if (time.monotonic() - last_active_at) < 1.5 else 0.05)
                continue

            last_active_at = time.monotonic()
            dirty = True
            kl = ch.lower() if isinstance(ch, str) else ""

            if act in ("KEY3", "LEFT", "ESCAPE", "BACKSPACE") or kl == "q":
                return
            elif act == "UP":
                if total:
                    cursor = (cursor - 1) % total
            elif act == "DOWN":
                if total:
                    cursor = (cursor + 1) % total
            elif act == "PAGEUP" and total:
                cursor = max(0, cursor - vis)
            elif act == "PAGEDOWN" and total:
                cursor = min(total - 1, cursor + vis)
            elif act in ("OK", "ENTER"):
                if total == 0:
                    continue
                if search_query and cursor == len(view_items):
                    next_n = len(base_items) + 10
                    _show_card("Loading More...", f"Fetching top {next_n} results")
                    more, err = search_youtube(search_query, max_results=next_n)
                    if err:
                        _show_card("Search Error", err, kind="err")
                        time.sleep(1.3)
                    else:
                        existing = {x["id"] for x in base_items}
                        for m in more:
                            if m["id"] not in existing:
                                base_items.append(m)
                else:
                    picked = view_items[cursor]
                    if music_mode or picked.get("kind") == "audio":
                        _play_music_playlist(view_items, start_idx=cursor)
                    else:
                        _play_video_playlist(view_items, start_idx=cursor)
                    liked_ids = {v.get("id") for v in load_liked()}
                    wl_ids = {v.get("id") for v in next((p.get("items", []) for p in load_video_playlists() if p.get("id") == WATCH_LATER_ID), [])}
                    resume_map = load_resume_map()
                    flush_input()
            elif (act == "KEY1" or kl == "m") and cursor < len(view_items):
                picked = view_items[cursor]
                if allow_delete_liked:
                    toggle_like_video(picked)
                    base_items = [x for x in base_items if x.get("id") != picked.get("id")]
                    liked_ids = {v.get("id") for v in load_liked()}
                elif allow_delete_local:
                    try:
                        os.remove(picked["path"])
                    except OSError:
                        pass
                else:
                    _play_music_playlist(view_items, start_idx=cursor)
                    liked_ids = {v.get("id") for v in load_liked()}
                    flush_input()
            elif kl == "w" and cursor < len(view_items) and not view_items[cursor].get("local"):
                picked = view_items[cursor]
                in_w = toggle_watch_later(picked)
                wl_ids = {v.get("id") for v in next((p.get("items", []) for p in load_video_playlists() if p.get("id") == WATCH_LATER_ID), [])}
                if playlist_id == WATCH_LATER_ID and not in_w:
                    base_items = [x for x in base_items if x.get("id") != picked.get("id")]
                _show_card("Watch Later", "Added to Watch Later" if in_w else "Removed from Watch Later", kind="ok" if in_w else "info")
                time.sleep(0.6)
            elif kl == "a" and cursor < len(view_items):
                picked = view_items[cursor]
                if allow_delete_local:
                    folders = list_download_folders()
                    f_opts: List[Tuple[str, object]] = [("Downloads Root", "")] + [(f"Folder: {fd['name']} ({fd['count']})", fd["name"]) for fd in folders] + [("+ New Folder...", "__new__"), ("Cancel", None)]
                    dest = _choose_from_modal("MOVE DOWNLOAD TO FOLDER", picked.get("title", "")[:24], f_opts)
                    if dest == "__new__":
                        nm = _ask_text("New Downloads Folder", max_len=40)
                        dest = nm.strip() if (nm and nm.strip()) else None
                    if dest is not None:
                        ok_m, msg_m, _ = move_download_to_folder(picked["path"], str(dest))
                        _show_card("Moved" if ok_m else "Move Failed", msg_m, kind="ok" if ok_m else "err")
                        time.sleep(0.8)
                elif playlist_url and not playlist_id:
                    # Save entire fetched URL playlist by its real name!
                    nm = _ask_text("Save Playlist Name", default=playlist_name[:48], max_len=48)
                    if nm and nm.strip():
                        pl_s = create_video_playlist(nm.strip(), base_items, source_url=playlist_url)
                        _show_card("Saved Playlist!", f"{pl_s['name']} ({len(base_items)})", kind="ok")
                        time.sleep(1.0)
                else:
                    _add_to_video_playlist_flow(picked)
            elif kl == "f":
                if allow_delete_local:
                    folders = list_download_folders()
                    f_opts = [
                        ("All Downloads", ("all", None)),
                        ("Videos Only (MP4/MKV)", ("video", None)),
                        ("Audio Only (MP3/M4A)", ("audio", None)),
                    ] + [(f"Folder: {fd['name']} ({fd['count']})", ("all", fd["name"])) for fd in folders] + [("Cancel", None)]
                    picked_f = _choose_from_modal("FILTER DOWNLOADS", "Browse by type or playlist folder", f_opts)
                    if isinstance(picked_f, tuple):
                        dl_kind, dl_folder = picked_f
                        cursor = 0
                        scroll = 0
                elif search_query:
                    f_act = _choose_from_modal("SEARCH FILTERS", f"Sort: {sort_by} • Dur: {dur_filter}", [
                        ("Sort: Relevance (Default)", ("sort", "relevance")),
                        ("Sort: Most Viewed", ("sort", "views")),
                        ("Sort: Shortest First", ("sort", "shortest")),
                        ("Sort: Longest First", ("sort", "longest")),
                        ("Duration: Any", ("dur", "any")),
                        ("Duration: Short (< 4 min)", ("dur", "short")),
                        ("Duration: Medium (4 - 20 min)", ("dur", "medium")),
                        ("Duration: Long (> 20 min)", ("dur", "long")),
                        ("Back", None),
                    ])
                    if isinstance(f_act, tuple):
                        if f_act[0] == "sort":
                            sort_by = f_act[1]
                        else:
                            dur_filter = f_act[1]
                        cursor = 0
                        scroll = 0
                elif cursor < len(view_items) and not view_items[cursor].get("local"):
                    it_f = view_items[cursor]
                    ch_nm = it_f.get("channel", "")
                    if ch_nm:
                        on_s = toggle_subscription(ch_nm, it_f.get("channel_url", ""), it_f.get("channel_id", ""))
                        _show_card("Subscriptions", f"Followed {ch_nm[:24]}" if on_s else f"Unfollowed {ch_nm[:22]}", kind="ok" if on_s else "info")
                        time.sleep(0.8)
            elif kl == "u" and cursor < len(view_items) and not view_items[cursor].get("local"):
                _browse_channel_flow(view_items[cursor])
            elif kl == "v" and cursor < len(view_items) and not view_items[cursor].get("local"):
                _open_video_info_modal(view_items[cursor])
                liked_ids = {v.get("id") for v in load_liked()}
                wl_ids = {v.get("id") for v in next((p.get("items", []) for p in load_video_playlists() if p.get("id") == WATCH_LATER_ID), [])}
            elif kl == "l" and cursor < len(view_items) and not view_items[cursor].get("local"):
                toggle_like_video(view_items[cursor])
                liked_ids = {v.get("id") for v in load_liked()}
                if allow_delete_liked:
                    base_items = [x for x in base_items if x.get("id") != view_items[cursor].get("id")]
            elif kl == "x" and playlist_id and cursor < len(view_items):
                rem_it = view_items[cursor]
                if playlist_id == WATCH_LATER_ID:
                    toggle_watch_later(rem_it)
                else:
                    remove_from_video_playlist(playlist_id, cursor)
                base_items = [x for x in base_items if x.get("id") != rem_it.get("id")]
            elif kl == "d" and cursor < len(view_items):
                if allow_delete_local:
                    try:
                        os.remove(view_items[cursor]["path"])
                    except OSError:
                        pass
                else:
                    _open_download_menu(
                        view_items[cursor],
                        playlist_items=(base_items if (playlist_id or playlist_name) else None),
                        playlist_name=playlist_name or title,
                    )
            elif act == "KEY2" or kl == "s":
                _open_settings_menu()

    def _open_playlists_hub() -> None:
        while running:
            pls = load_video_playlists()
            opts: List[Tuple[str, object]] = []
            for p in pls:
                tag = "[PIN] " if p.get("id") == WATCH_LATER_ID else ("(YT) " if p.get("source_url") else "")
                opts.append((f"{tag}{p['name']} ({len(p.get('items', []))})", ("open", p)))
            opts.append(("+ Create New Video Playlist", ("new", None)))
            opts.append(("+ Import YouTube Playlist URL", ("import", None)))
            opts.append(("Back", None))
            picked = _choose_from_modal("PLAYLISTS & WATCH LATER", f"{len(pls)} lists", opts)
            if not picked:
                return
            kind, val = picked
            if kind == "new":
                nm = _ask_text("New Video Playlist Name", max_len=48)
                if nm and nm.strip():
                    create_video_playlist(nm.strip(), [])
            elif kind == "import":
                u = _ask_text("YouTube Playlist URL", max_len=140)
                if u and u.strip():
                    _show_card("Fetching Playlist...", u.strip()[:34])
                    pl_title, items, err = fetch_playlist_details(u.strip(), max_results=100)
                    if err or not items:
                        _show_card("Playlist Error", err or "No videos found", kind="err")
                        time.sleep(1.5)
                        continue
                    final_nm = _ask_text("Confirm Playlist Name", default=pl_title[:48], max_len=48)
                    if final_nm is None:
                        continue
                    final_nm = final_nm.strip() or pl_title
                    mode = _choose_from_modal(f"{final_nm[:22]} ({len(items)})", "Choose action", [
                        ("Save Playlist (Stream Online)", "save"),
                        ("Save & Download Entire Playlist", "download"),
                        ("Browse Only (Don't Save)", "browse"),
                    ])
                    if mode == "save":
                        pl_n = create_video_playlist(final_nm, items, source_url=u.strip())
                        _browse_media_list(pl_n["name"][:18], pl_n["items"], subtitle=f"{len(items)} videos", playlist_id=pl_n["id"], playlist_name=pl_n["name"], playlist_url=u.strip())
                    elif mode == "download":
                        pl_n = create_video_playlist(final_nm, items, source_url=u.strip())
                        _download_playlist_flow(pl_n["items"], pl_n["name"])
                    elif mode == "browse":
                        _browse_media_list(final_nm[:18], items, subtitle=f"{len(items)} videos", playlist_name=final_nm, playlist_url=u.strip())
            elif kind == "open" and isinstance(val, dict):
                pl = val
                act_opts = [
                    (f"Open & Browse ({len(pl.get('items', []))} videos)", "browse"),
                    ("Play All from Start", "play"),
                    ("Download ENTIRE Playlist (MP4/MP3)", "dl_all"),
                ]
                if pl.get("source_url"):
                    act_opts.append(("Sync New Videos from YouTube", "sync"))
                if pl.get("id") != WATCH_LATER_ID:
                    act_opts.append(("Rename Playlist", "rename"))
                    act_opts.append(("Delete Playlist", "delete"))
                act_opts.append(("Back", None))
                pa = _choose_from_modal(pl["name"][:24], f"{len(pl.get('items', []))} videos", act_opts)
                if pa == "browse":
                    _browse_media_list(pl["name"][:18], list(pl.get("items", [])), subtitle=f"{len(pl.get('items', []))} videos", playlist_id=pl["id"], playlist_name=pl["name"], playlist_url=pl.get("source_url", ""))
                elif pa == "play" and pl.get("items"):
                    _play_video_playlist(pl["items"], 0)
                elif pa == "dl_all":
                    _download_playlist_flow(pl.get("items", []), pl["name"])
                elif pa == "sync":
                    _show_card("Syncing Playlist...", pl["name"][:28])
                    _n, msg_s = sync_video_playlist(pl["id"])
                    _show_card("Playlist Sync", msg_s, kind="ok")
                    time.sleep(1.2)
                elif pa == "rename":
                    nn = _ask_text("Rename Playlist", default=pl["name"], max_len=48)
                    if nn and nn.strip():
                        rename_video_playlist(pl["id"], nn.strip())
                elif pa == "delete":
                    delete_video_playlist(pl["id"])

    def _open_subscriptions_hub() -> None:
        while running:
            subs = load_subscriptions()
            opts: List[Tuple[str, object]] = []
            if subs:
                opts.append((f"Latest Uploads Feed ({len(subs)} channels)", ("feed", None)))
                for s in subs:
                    opts.append((f"Channel: {s['name']}", ("chan", s)))
            opts.append(("+ Follow Channel (@handle or URL)", ("add", None)))
            if subs:
                opts.append(("- Unfollow a Channel", ("del", None)))
            opts.append(("Back", None))
            picked = _choose_from_modal("SUBSCRIPTIONS", f"{len(subs)} followed", opts)
            if not picked:
                return
            kind, val = picked
            if kind == "feed":
                _show_card("Loading Subscriptions Feed...", f"Fetching latest from {len(subs)} channels")
                feed = fetch_subscriptions_feed(subs, per_channel=5)
                if not feed:
                    _show_card("Subscriptions Feed", "No videos found", kind="err")
                    time.sleep(1.3)
                else:
                    _browse_media_list("SUBSCRIPTIONS", feed, subtitle=f"{len(feed)} latest")
            elif kind == "chan" and isinstance(val, dict):
                _browse_channel_flow(val.get("url") or val.get("channel_id") or val.get("name", ""), label=val.get("name", ""))
            elif kind == "add":
                ref = _ask_text("Channel @handle or URL", default="@", max_len=90)
                if ref and ref.strip() and ref.strip() != "@":
                    raw_r = ref.strip()
                    nm = raw_r.rstrip("/").split("/")[-1]
                    toggle_subscription(nm, channel_url=raw_r if raw_r.startswith("http") else f"https://www.youtube.com/{raw_r}")
                    _show_card("Subscribed!", nm[:28], kind="ok")
                    time.sleep(0.9)
            elif kind == "del" and subs:
                rm = _choose_from_modal("UNFOLLOW CHANNEL", "Pick channel to remove", [(s["name"], s) for s in subs] + [("Cancel", None)])
                if isinstance(rm, dict):
                    toggle_subscription(rm["name"], channel_id=rm.get("channel_id", ""))

    def _run_search_flow(query_text: str, music_mode: bool = False) -> None:
        q = (query_text or "").strip()
        if not q:
            return
        kind, val = parse_youtube_target(q)
        if kind == "video":
            _show_card("Loading Video Info...", val)
            meta = fetch_video_meta(val)
            if meta:
                if music_mode:
                    _play_music_playlist([meta], 0)
                else:
                    _play_video_playlist([meta], 0)
            return
        if kind == "playlist":
            _show_card("Loading Playlist...", q[:34])
            pl_title, items, err = fetch_playlist_details(val, max_results=100)
            if err or not items:
                _show_card("Playlist Error", err or "No videos found", kind="err")
                time.sleep(1.5)
                return
            _browse_media_list(
                pl_title[:18] or "PLAYLIST",
                items,
                subtitle=f"{len(items)} items (A:Save)",
                music_mode=music_mode,
                playlist_name=pl_title,
                playlist_url=val,
            )
            return

        save_history_entry(q)
        _show_card("Searching YouTube...", q[:36])
        items, err = search_youtube(q, max_results=14)
        if err or not items:
            _show_card("No Results", err or f'Nothing for "{q[:20]}"', kind="err")
            time.sleep(1.5)
            return
        _browse_media_list(
            "MUSIC RESULTS" if music_mode else "RESULTS",
            items,
            subtitle=q[:22],
            search_query=q,
            music_mode=music_mode,
        )

    # ------------------------------------------------------------------
    # Startup Check & Home Menu Loop
    # ------------------------------------------------------------------

    try:
        with text_input_mode():
            _show_card("YouTube Player", "Checking ffmpeg, yt-dlp & network...")
            deps_ok, missing = check_deps()
            online = check_internet()
            if not deps_ok:
                _show_card(
                    "Missing Tools",
                    "Install: " + ", ".join(missing),
                    kind="err",
                    foot="sudo apt install ffmpeg yt-dlp  (offline media still browsable)",
                )
                time.sleep(2.2)

            menu_sel = 0
            liked_cnt = len(load_liked())
            dl_cnt = len(list_downloads())
            subs_cnt = len(load_subscriptions())
            pls_cnt = len(load_video_playlists())
            last_meta_chk = time.monotonic()
            dirty = True
            last_active_at = last_meta_chk
            flush_input()

            while running:
                now_m = time.monotonic()
                if (now_m - last_meta_chk) >= 5.0:
                    new_aud = audio_helper.get_active_audio_label()
                    new_lk = len(load_liked())
                    new_dl = len(list_downloads())
                    new_sb = len(load_subscriptions())
                    new_pl = len(load_video_playlists())
                    if (new_aud, new_lk, new_dl, new_sb, new_pl) != (audio_label, liked_cnt, dl_cnt, subs_cnt, pls_cnt):
                        audio_label, liked_cnt, dl_cnt, subs_cnt, pls_cnt = new_aud, new_lk, new_dl, new_sb, new_pl
                        dirty = True
                    last_meta_chk = now_m

                if dirty:
                    q_lbl = STREAM_QUALITIES[cfg["quality_idx"]][0]
                    frame = render_home_menu(
                        W, H, sel_idx=menu_sel, audio_label=audio_label,
                        quality_label=q_lbl, online=online,
                        liked_count=liked_cnt, downloads_count=dl_cnt,
                        subs_count=subs_cnt, playlists_count=pls_cnt, fonts=fonts,
                    )
                    lcd.LCD_ShowImage(frame, 0, 0)
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO)
                if act is None and ch is None:
                    time.sleep(0.025 if (now_m - last_active_at) < 1.5 else 0.05)
                    continue

                last_active_at = time.monotonic()
                dirty = True
                kl = ch.lower() if isinstance(ch, str) else ""

                if act in ("KEY3", "ESCAPE", "BACKSPACE") or kl == "q":
                    break
                elif act == "UP":
                    step = 2 if fonts["large"] else 1
                    menu_sel = (menu_sel - step) % len(HOME_MENU_ITEMS)
                elif act == "DOWN":
                    step = 2 if fonts["large"] else 1
                    menu_sel = (menu_sel + step) % len(HOME_MENU_ITEMS)
                elif act == "LEFT":
                    menu_sel = (menu_sel - 1) % len(HOME_MENU_ITEMS)
                elif act == "RIGHT":
                    menu_sel = (menu_sel + 1) % len(HOME_MENU_ITEMS)
                elif kl == "s":
                    q = _ask_text("Search YouTube / URL / Playlist", max_len=140)
                    if q:
                        _run_search_flow(q, music_mode=False)
                        last_meta_chk = 0.0
                elif kl == "m":
                    q = _ask_text("Search Music (Audio)", max_len=120)
                    if q:
                        _run_search_flow(q, music_mode=True)
                        last_meta_chk = 0.0
                elif act == "KEY2":
                    _open_settings_menu()
                    last_meta_chk = 0.0
                elif act in ("OK", "ENTER"):
                    action = HOME_MENU_ITEMS[menu_sel][1]
                    if action == "search":
                        q = _ask_text("Search YouTube / URL / Playlist", max_len=140)
                        if q:
                            _run_search_flow(q, music_mode=False)
                    elif action == "music":
                        q = _ask_text("Search Music (Audio)", max_len=120)
                        if q:
                            _run_search_flow(q, music_mode=True)
                    elif action == "subs":
                        _open_subscriptions_hub()
                    elif action == "playlists":
                        _open_playlists_hub()
                    elif action == "presets":
                        presets = load_presets()
                        opts = [(p, ("go", p)) for p in presets]
                        opts.append(("+ Add New Preset", ("add", None)))
                        opts.append(("- Delete Selected Preset", ("del", None)))
                        chosen = _choose_from_modal("QUICK SEARCH", f"{len(presets)} saved", opts)
                        if chosen:
                            if chosen[0] == "go":
                                _run_search_flow(chosen[1], music_mode=False)
                            elif chosen[0] == "add":
                                np = _ask_text("New Search Preset")
                                if np and np.strip() not in presets:
                                    presets.append(np.strip())
                                    save_presets(presets)
                            elif chosen[0] == "del" and presets:
                                rm = _choose_from_modal("DELETE PRESET", "Pick preset to remove", [(p, p) for p in presets])
                                if rm in presets:
                                    presets.remove(rm)
                                    save_presets(presets)
                    elif action == "liked":
                        liked_items = load_liked()
                        _browse_media_list("LIKED VIDEOS", liked_items, subtitle=f"{len(liked_items)} saved", allow_delete_liked=True, playlist_name="Liked Videos")
                    elif action == "downloads":
                        dl_items = list_downloads()
                        tot_mb = sum(float(x.get("size_mb", 0.0)) for x in dl_items)
                        _browse_media_list("DOWNLOADS (OFFLINE)", dl_items, subtitle=f"{len(dl_items)} • {tot_mb:.0f} MB (F:Folders)", allow_delete_local=True)
                    elif action == "settings":
                        _open_settings_menu()
                    last_meta_chk = 0.0
    finally:
        try:
            clear_preload_cache()
        except Exception:
            pass
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
