#!/usr/bin/env python3
"""
EmberJack YouTube Data, Persistence, Subtitles & yt-dlp Resolvers
=================================================================
Pure data helpers, settings/bookmarks/playlists/history/resume/subscriptions
persistence, VTT subtitle parsing, chapter helpers, offline download listing,
thumbnail caching, and yt-dlp search/metadata/stream-URL resolution.

Split out from payloads/utilities/youtube.py for maintainability while keeping
100% backwards-compatible re-exports in payloads/utilities/youtube.py.
"""

import io
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

from PIL import Image

import rj_env
import rj_youtube_feed

# ---------------------------------------------------------------------------
# Paths, Constants & Format Selectors
# ---------------------------------------------------------------------------

def get_youtube_data_dir() -> str:
    return os.path.join(rj_env.get_base_dir(), "loot", "YouTube")


def get_downloads_dir() -> str:
    return os.path.join(get_youtube_data_dir(), "Downloads")


def get_thumb_cache_dir() -> str:
    return os.path.join(get_youtube_data_dir(), "thumbs")


HISTORY_MAX = 25

# Prefer fast-decoding H.264 (avc1 <=30fps), then VP9 (<=30fps), explicitly avoiding
# heavy software AV1 (av01) decode on Raspberry Pi unless no other codec exists.
STREAM_QUALITIES: List[Tuple[str, str]] = [
    ("144p", "bestvideo[height<=144][vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[height<=144][vcodec!^=av01]+bestaudio/best[height<=144]/worst"),
    ("240p", "bestvideo[height<=240][vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[height<=240][vcodec!^=av01]+bestaudio/best[height<=240]/worst"),
    ("360p", "best[height<=360][ext=mp4]/bestvideo[height<=360][vcodec^=avc1][fps<=30]+bestaudio[acodec^=mp4a]/bestvideo[height<=360][vcodec!^=av01][fps<=30]+bestaudio/bestvideo[height<=360][vcodec!^=av01]+bestaudio/best[height<=360]/best"),
    ("480p", "bestvideo[height<=480][vcodec^=avc1][fps<=30]+bestaudio[acodec^=mp4a]/bestvideo[height<=480][vcodec^=avc1]+bestaudio/bestvideo[height<=480][vcodec^=vp9][fps<=30]+bestaudio/bestvideo[height<=480][vcodec!^=av01]+bestaudio/best[height<=480]/best"),
    ("720p", "bestvideo[height<=720][vcodec^=avc1][fps<=30]+bestaudio[acodec^=mp4a]/bestvideo[height<=720][vcodec^=avc1]+bestaudio/best[height<=720][ext=mp4]/bestvideo[height<=720][vcodec^=vp9][fps<=30]+bestaudio/bestvideo[height<=720][vcodec!^=av01]+bestaudio/bestvideo[height<=720]+bestaudio/best[height<=720]/best"),
    ("1080p", "bestvideo[height<=1080][vcodec^=avc1][fps<=30]+bestaudio[acodec^=mp4a]/bestvideo[height<=1080][vcodec^=avc1]+bestaudio/bestvideo[height<=1080][vcodec^=vp9][fps<=30]+bestaudio/bestvideo[height<=1080][vcodec!^=av01]+bestaudio/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"),
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
    ("For You Feed", "feed", "Personalized picks & YouTube Home"),
    ("Search / URL", "search", "Search YouTube, filters, URL or ID"),
    ("Music Mode", "music", "Audio-only streaming & Bluetooth"),
    ("Subscriptions", "subs", "Followed channels & latest uploads"),
    ("Playlists & Later", "playlists", "Watch Later, saved & imported lists"),
    ("Quick Search", "presets", "Saved favorite search presets"),
    ("Liked Videos", "liked", "Your bookmarked videos & tracks"),
    ("Watch History", "watch_history", "Recently watched videos & clear"),
    ("Downloads", "downloads", "Offline MP4, MP3 & playlist folders"),
    ("Settings & Audio", "settings", "Feed, account, quality, speed & BT"),
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


_SETTINGS_SCHEMA_VERSION = 2


def load_settings(default_wide: bool = True) -> dict:
    path = os.path.join(get_youtube_data_dir(), "settings.json")
    cfg = _read_json(path, {})
    if not isinstance(cfg, dict):
        cfg = {}
    # One-time migration from legacy v1 settings (which defaulted to 24fps + builtin):
    # upgrades legacy defaults to 30fps (eliminates 30->24fps periodic judder) & Auto
    # (native GPU/DRM mpv on HDMI -> zero pixelation, smooth fallback to Built-in).
    schema_v = int(cfg.get("settings_version", 0) or 0)
    migrated = False
    if cfg and schema_v < _SETTINGS_SCHEMA_VERSION:
        if int(cfg.get("fps_wide", 24) or 24) == 24:
            cfg["fps_wide"] = 30
            migrated = True
        if str(cfg.get("player_engine", "builtin") or "builtin").strip().lower() == "builtin":
            cfg["player_engine"] = "auto"
            migrated = True
    # 720p H.264 on wide/HDMI (sharp on native mpv GPU/DRM; falls back smoothly on 360p/compact)
    default_q = 4 if default_wide else 2
    r_scale = str(cfg.get("render_scale", "auto") or "auto").strip().lower()
    if r_scale not in ("auto", "fast", "balanced", "sharp"):
        r_scale = "auto"
    try:
        preload_sec = int(cfg.get("preload_sec", 5))
    except Exception:
        preload_sec = 5
    if preload_sec not in (0, 3, 5, 10, -1):
        preload_sec = 5
    engine = str(cfg.get("player_engine", "auto") or "auto").strip().lower()
    if engine not in ("auto", "builtin", "mpv"):
        engine = "auto"
    try:
        spd = float(cfg.get("playback_speed", 1.0))
    except Exception:
        spd = 1.0
    if spd not in SPEED_OPTIONS:
        spd = 1.0
    feed_mode = str(cfg.get("feed_mode", "auto") or "auto").strip().lower()
    if feed_mode not in ("auto", "local", "account"):
        feed_mode = "auto"
    cookie_source = str(cfg.get("cookie_source", "auto") or "auto").strip().lower()
    if cookie_source not in ("auto", "file", "firefox", "chromium", "chrome", "brave", "off"):
        cookie_source = "auto"
    out = {
        "settings_version": _SETTINGS_SCHEMA_VERSION,
        "quality_idx": max(0, min(len(STREAM_QUALITIES) - 1, int(cfg.get("quality_idx", default_q)))),
        "auto_next": bool(cfg.get("auto_next", True)),
        "show_osd": bool(cfg.get("show_osd", True)),
        "fps_wide": max(10, min(30, int(cfg.get("fps_wide", 30)))),
        "fps_compact": max(8, min(24, int(cfg.get("fps_compact", 12)))),
        "render_scale": r_scale,
        "preload_sec": preload_sec,
        "player_engine": engine,
        "playback_speed": spd,
        "auto_captions": bool(cfg.get("auto_captions", False)),
        # Extra video delay (ms) to match Bluetooth A2DP wireless audio latency (0 on wired, ~200ms on BT)
        "av_sync_ms": max(-500, min(800, int(cfg.get("av_sync_ms", 0)))),
        "bt_sync_ms": max(0, min(600, int(cfg.get("bt_sync_ms", 200)))),
        # Personalized Feed & YouTube Account Cookies
        "feed_mode": feed_mode,
        "cookie_source": cookie_source,
        "use_cookies_for_streams": bool(cfg.get("use_cookies_for_streams", True)),
        "auto_open_feed": bool(cfg.get("auto_open_feed", False)),
    }
    if migrated:
        save_settings(out)
    return out


def _active_stream_cookie_args(cfg: Optional[dict] = None) -> List[str]:
    """Return yt-dlp cookie args for stream/details lookup when enabled."""
    try:
        c = cfg if isinstance(cfg, dict) else load_settings()
        if not c.get("use_cookies_for_streams", True):
            return []
        args, _lbl, _src = rj_youtube_feed.resolve_ytdlp_cookie_args(c)
        return list(args)
    except Exception:
        return []


def save_settings(cfg: dict) -> None:
    out = dict(cfg) if isinstance(cfg, dict) else {}
    out["settings_version"] = _SETTINGS_SCHEMA_VERSION
    _write_json(os.path.join(get_youtube_data_dir(), "settings.json"), out)


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


def fetch_playlist_details(url: str, max_results: int = 100, start: int = 1) -> Tuple[str, List[dict], str]:
    """Fetch a YouTube playlist's REAL title and video entries (supports pagination via *start*)."""
    p_start = max(1, int(start))
    p_end = max(p_start, p_start + max(1, int(max_results)) - 1) if p_start > 1 else max(1, int(max_results))
    cmd = ["yt-dlp", "--flat-playlist", "--no-download", "--no-warnings"]
    if p_start > 1:
        cmd += ["--playlist-start", str(p_start)]
    cmd += ["--playlist-end", str(p_end), "-j", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=55)
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
    c_args = _active_stream_cookie_args()
    try:
        r = subprocess.run(
            ["yt-dlp", "--no-download", "--no-warnings", "--no-playlist", *c_args, "-j", url],
            capture_output=True, text=True, timeout=20,
        )
        if (r.returncode != 0 or not r.stdout.strip()) and c_args:
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


def fetch_channel_videos(channel_ref: str, max_results: int = 15, start: int = 1) -> Tuple[List[dict], str]:
    """Fetch recent uploads for a YouTube channel URL, @handle, or UC... ID (paginated via *start*)."""
    ref = (channel_ref or "").strip()
    if not ref:
        return [], "Missing channel reference"
    if not shutil.which("yt-dlp"):
        return [], "yt-dlp not installed"
    p_start = max(1, int(start))
    p_end = max(p_start, p_start + max(1, int(max_results)) - 1) if p_start > 1 else max(1, int(max_results))
    if ref.startswith(("http://", "https://")):
        url = ref.rstrip("/")
        if not url.endswith("/videos"):
            url += "/videos"
    elif ref.startswith("@"):
        url = f"https://www.youtube.com/{ref}/videos"
    elif ref.startswith("UC") and len(ref) >= 20:
        url = f"https://www.youtube.com/channel/{ref}/videos"
    else:
        return search_youtube(ref, max_results=p_end)
    cmd = ["yt-dlp", "--flat-playlist", "--no-download", "--no-warnings"]
    if p_start > 1:
        cmd += ["--playlist-start", str(p_start)]
    cmd += ["--playlist-end", str(p_end), "-j", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
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
    c_args = _active_stream_cookie_args()
    attempts = [c_args, []] if c_args else [[]]
    for fmt in (quality_fmt, "best"):
        for extra_c in attempts:
            try:
                r = subprocess.run(
                    ["yt-dlp", "-f", fmt, "--no-playlist", "--no-warnings", *extra_c, "--get-url", url],
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
    c_args = _active_stream_cookie_args()
    attempts = [c_args, []] if c_args else [[]]
    last_err = "No audio stream"
    for extra_c in attempts:
        try:
            r = subprocess.run(
                ["yt-dlp", "-f", "bestaudio/best", "--no-playlist", "--no-warnings", *extra_c, "--get-url", url],
                capture_output=True, text=True, timeout=40,
            )
            lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip().startswith("http")]
            if lines:
                with _URL_CACHE_LOCK:
                    _AUDIO_URL_CACHE[video_id] = (time.monotonic() + _URL_CACHE_TTL, lines[0])
                return lines[0], ""
            if r.stderr:
                last_err = r.stderr.strip()[:48]
        except subprocess.TimeoutExpired:
            return "", "Audio lookup timed out"
        except Exception as exc:
            return "", str(exc)[:48]
    return "", last_err
