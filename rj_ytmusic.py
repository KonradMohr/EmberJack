#!/usr/bin/env python3
"""
EmberJack Full YouTube Music Engine
-----------------------------------
Zero-external-library backend powering the dedicated Obsidian Ember YouTube
Music client:
  - YouTube Music search, real-name URL/playlist import & 1-click playlist sync
  - Full-playlist offline MP3 downloading (into organized Library/<Playlist>/ folders,
    skip-existing, embedded metadata + cover art, M3U export, cancel-safe)
  - Offline Library & Folder organizer + Smart Offline-First playback resolution
  - Continuous background 48kHz audio playback routed through Bluetooth / PipeWire /
    PulseAudio / BlueALSA / ALSA via payloads._audio_helper
  - Instant <20ms pause/resume (tears down & reopens PCM sink cleanly at exact sample)
    and glitch-free mid-song output switching (`reopen_audio_sink()`)
  - Live 16-band logarithmic numpy FFT spectrum analyzer + Peak-Hold caps
  - Synced LRC & plain lyrics fetcher (lrclib.net, locally cached, speed-aware)
  - 9 ffmpeg EQ presets, Loudness Normalizer, Playback Speed (0.75x-2.0x),
    Sleep Timer with 5s smooth fade-out, Listening Stats & Most Played
"""

import concurrent.futures
import io
import json
import math
import os
import random
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
from PIL import Image

import rj_env
import rj_youtube_feed

try:
    from payloads import _audio_helper as audio_helper
except Exception:
    audio_helper = None


# ---------------------------------------------------------------------------
# Paths, Presets & Curated Mood/Genre Radio Stations
# ---------------------------------------------------------------------------

def get_ytm_dir() -> str:
    return os.path.join(rj_env.get_base_dir(), "loot", "YTMusic")


def get_library_dir() -> str:
    return os.path.join(get_ytm_dir(), "Library")


def get_thumbs_dir() -> str:
    return os.path.join(get_ytm_dir(), "thumbs")


def get_lyrics_dir() -> str:
    return os.path.join(get_ytm_dir(), "lyrics")


EQ_PRESETS: List[Tuple[str, str]] = [
    ("Flat", "aresample=async=1"),
    ("Bass Boost", "bass=g=5:f=95:w=0.6,aresample=async=1"),
    ("Warm Ember", "bass=g=3:f=110:w=0.7,treble=g=-1.5:f=6500,aresample=async=1"),
    ("Vocal Clarity", "equalizer=f=2200:t=q:w=1.2:g=4,bass=g=-1:f=100,aresample=async=1"),
    ("Late Night", "acompressor=threshold=-20dB:ratio=4:attack=10:release=120,aresample=async=1"),
    ("Electronic", "bass=g=4.5:f=85:w=0.6,treble=g=2.5:f=9000,aresample=async=1"),
    ("Acoustic", "equalizer=f=180:t=q:w=1.0:g=2,equalizer=f=3200:t=q:w=1.2:g=2.5,aresample=async=1"),
    ("Podcast", "highpass=f=85,equalizer=f=2600:t=q:w=1.0:g=4.5,acompressor=threshold=-18dB:ratio=3,aresample=async=1"),
    ("Lo-Fi Warm", "lowpass=f=8500,bass=g=2.5:f=120:w=0.8,aresample=async=1"),
]

SPEED_OPTIONS: Tuple[float, ...] = (0.75, 1.0, 1.25, 1.5, 2.0)
VISUALIZER_MODES: Tuple[str, ...] = ("bars", "peaks", "mirror", "cover")
SLEEP_OPTIONS: Tuple[Tuple[str, int], ...] = (
    ("Off", 0),
    ("End of Current Track", -1),
    ("15 Minutes", 15),
    ("30 Minutes", 30),
    ("45 Minutes", 45),
    ("60 Minutes", 60),
)

RADIO_STATIONS: List[Tuple[str, str, str]] = [
    ("Lo-Fi Focus", "lofi hip hop beats chill study music", "Mellow beats for coding & focus"),
    ("Synthwave Drive", "synthwave retrowave outrun music mix", "Warm analog neon night vibes"),
    ("Indie & Acoustic", "indie folk acoustic chill songs", "Relaxed acoustic guitar & vocals"),
    ("Hip-Hop Beats", "boom bap hip hop instrumental beats", "Classic beats & head-nodding grooves"),
    ("Rock Classics", "classic rock greatest hits official audio", "Timeless guitars & stadium anthems"),
    ("Electronic & House", "deep house electronic melodic mix", "Smooth electronic & progressive sets"),
    ("Late Night Jazz", "smooth jazz coffee shop instrumental", "Warm saxophone & piano atmosphere"),
    ("Gaming OST", "best video game soundtrack chill music", "Iconic synth & orchestral game themes"),
    ("Ambient Space", "deep ambient space music focus", "Atmospheric drones & soundscapes"),
    ("Classical Study", "classical piano violin study music", "Timeless compositions for deep work"),
]

_AUDIO_EXTS = (".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac")
_LRC_TAG_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]")
_PCM_RATE = 48000  # PipeWire & Bluetooth A2DP native rate (avoids live resampling artifacts)

YTM_FEED_MODE_OPTIONS: Tuple[Tuple[str, str], ...] = (
    ("Auto (Account Mix -> Smart Mix)", "auto"),
    ("Local Smart Mix Only", "local"),
    ("YT Music Account Only", "account"),
)

YTM_ACCOUNT_FEED_TARGETS: Dict[str, Tuple[str, str]] = {
    "supermix": (
        "https://music.youtube.com/playlist?list=RDTMAK5uy_kset8DisdE7LSD4TNjEVvrKRTmG7a56sY",
        "YT Music Supermix",
    ),
    "liked": ("https://music.youtube.com/playlist?list=LM", "YT Music Liked Songs"),
    "history": ("https://music.youtube.com/history", "YT Music History"),
    "recommended": (":ytrecommended", "YouTube Account Home"),
}

_YTM_FEED_CACHE_LOCK = threading.Lock()
_YTM_FEED_CACHE: Dict[str, Tuple[float, List[dict], dict]] = {}
_YTM_FEED_CACHE_TTL = 900.0  # 15 minutes


# ---------------------------------------------------------------------------
# Pure Metadata, Formatting, URL, DSP & LRC Helpers
# ---------------------------------------------------------------------------

def format_time(sec: float) -> str:
    s = max(0, int(sec or 0))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def sanitize_filename(text: str, max_len: int = 72, fallback: str = "Untitled") -> str:
    """Return a clean filesystem-safe filename/folder stem."""
    cleaned = "".join(c for c in str(text or "") if c.isalnum() or c in " -_()").strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)[:max_len].strip(" .-_")
    return cleaned or fallback


def build_audio_filter_chain(eq_filter: str, speed: float = 1.0, loudnorm: bool = False) -> str:
    """Build ffmpeg `-af` chain combining optional loudness leveling, EQ, and speed."""
    parts: List[str] = []
    if loudnorm:
        parts.append("dynaudnorm=f=150:g=15:p=0.92")
    spd = max(0.5, min(2.0, float(speed or 1.0)))
    if abs(spd - 1.0) > 0.01:
        parts.append(f"atempo={spd:.2f}")
    if eq_filter:
        parts.append(eq_filter)
    else:
        parts.append("aresample=async=1")
    return ",".join(parts)


def normalize_track_metadata(raw: dict) -> Optional[dict]:
    """Normalize yt-dlp JSON entry into a clean music track dict."""
    vid = str(raw.get("id") or "").strip()
    if not vid:
        return None
    title = str(raw.get("track") or raw.get("title") or "Untitled").strip()
    artist = str(raw.get("artist") or raw.get("creator") or raw.get("channel") or raw.get("uploader") or "").strip()
    album = str(raw.get("album") or "").strip()

    # Clean YouTube Music auto-generated "Artist - Topic" channel suffix
    if artist.endswith(" - Topic"):
        artist = artist[:-8].strip()
    if artist.endswith("VEVO"):
        artist = artist[:-4].strip()

    # Split "Artist - Song Title" when channel looks generic or matches
    if " - " in title and (not artist or artist.lower() in ("various artists", "youtube", "")):
        left, right = title.split(" - ", 1)
        if 1 < len(left) <= 36 and len(right) >= 2:
            artist = left.strip()
            title = right.strip()

    # Strip noisy YouTube suffixes like "(Official Audio)" / "[Official Music Video]"
    clean_title = re.sub(
        r"\s*[\(\[](?:official\s+(?:music\s+)?(?:video|audio|lyric\s+video|visualizer)|hd|4k|lyrics?|audio)[\)\]]",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip() or title

    thumb = str(raw.get("thumbnail") or "")
    if not thumb:
        thumbs = raw.get("thumbnails")
        if isinstance(thumbs, list) and thumbs:
            thumb = str(thumbs[-1].get("url") or "")
    if not thumb:
        thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

    return {
        "id": vid,
        "title": clean_title[:96],
        "artist": (artist or "YouTube Music")[:48],
        "album": album[:48],
        "duration": int(raw.get("duration") or 0),
        "thumb": thumb,
        "local": False,
        "path": "",
    }


def parse_ytmusic_input(text: str) -> Tuple[str, str]:
    """Classify input into ('playlist', url), ('track', id), or ('search', query)."""
    s = (text or "").strip()
    if not s:
        return "search", ""
    m_list = re.search(r"[?&]list=([A-Za-z0-9_-]{10,})", s)
    if m_list and ("/playlist" in s or "v=" not in s):
        return "playlist", s
    m_vid = re.search(
        r"(?:(?:music\.)?youtube\.com/(?:watch\?.*?\bv=|shorts/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})",
        s,
    )
    if m_vid:
        return "track", m_vid.group(1)
    if m_list:
        return "playlist", s
    return "search", s


def parse_lrc(lrc_text: str) -> List[Tuple[float, str]]:
    """Parse LRC synced lyrics into sorted [(timestamp_sec, line_text), ...]."""
    out: List[Tuple[float, str]] = []
    if not lrc_text:
        return out
    for raw_line in lrc_text.splitlines():
        line = raw_line.strip()
        matches = list(_LRC_TAG_RE.finditer(line))
        if not matches:
            continue
        lyric_txt = _LRC_TAG_RE.sub("", line).strip()
        for m in matches:
            mins = int(m.group(1))
            secs = int(m.group(2))
            frac_s = m.group(3) or "0"
            frac = int(frac_s) / (1000.0 if len(frac_s) == 3 else (100.0 if len(frac_s) == 2 else 10.0))
            out.append((mins * 60.0 + secs + frac, lyric_txt or "♪"))
    out.sort(key=lambda pair: pair[0])
    return out


def get_active_lyric_index(synced_lines: List[Tuple[float, str]], elapsed: float) -> int:
    """Return the index of the active synced lyric line at *elapsed* seconds."""
    if not synced_lines:
        return -1
    active = 0
    for i, (ts, _txt) in enumerate(synced_lines):
        if ts <= elapsed + 0.25:
            active = i
        else:
            break
    return active


def extract_playlist_title_from_entries(entries: List[dict], fallback: str = "YouTube Playlist") -> str:
    """Extract the real YouTube / YT Music playlist title from yt-dlp JSON entry objects."""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for key in ("playlist_title", "playlist", "album", "title"):
            if key == "title" and entry.get("_type") != "playlist":
                continue
            val = str(entry.get(key) or "").strip()
            if val and val.lower() not in ("na", "none", "youtube", "youtube music"):
                # Strip redundant "Album - " prefix sometimes returned by YT Music
                if val.startswith("Album - ") and len(val) > 8:
                    val = val[8:].strip()
                return val[:56]
    return fallback


# ---------------------------------------------------------------------------
# Persistence (Settings, Liked Songs, Playlists, Stats, Offline Index)
# ---------------------------------------------------------------------------

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


def load_settings() -> dict:
    cfg = _read_json(os.path.join(get_ytm_dir(), "settings.json"), {})
    if not isinstance(cfg, dict):
        cfg = {}
    eq_idx = max(0, min(len(EQ_PRESETS) - 1, int(cfg.get("eq_idx", 0))))
    rep = cfg.get("repeat_mode", "all")
    if rep not in ("off", "all", "one"):
        rep = "all"
    try:
        spd = float(cfg.get("speed", 1.0))
    except Exception:
        spd = 1.0
    if spd not in SPEED_OPTIONS:
        spd = 1.0
    viz = str(cfg.get("visualizer", "peaks") or "peaks").strip().lower()
    if viz not in VISUALIZER_MODES:
        viz = "peaks"
    feed_mode = str(cfg.get("feed_mode", "auto") or "auto").strip().lower()
    if feed_mode not in ("auto", "local", "account"):
        feed_mode = "auto"
    cookie_source = str(cfg.get("cookie_source", "auto") or "auto").strip().lower()
    if cookie_source not in ("auto", "file", "firefox", "chromium", "chrome", "brave", "off"):
        cookie_source = "auto"
    return {
        "eq_idx": eq_idx,
        "shuffle": bool(cfg.get("shuffle", False)),
        "repeat_mode": rep,
        "auto_radio": bool(cfg.get("auto_radio", True)),
        "volume_pct": max(5, min(100, int(cfg.get("volume_pct", 70)))),
        "speed": spd,
        "loudnorm": bool(cfg.get("loudnorm", False)),
        "visualizer": viz,
        "feed_mode": feed_mode,
        "cookie_source": cookie_source,
        "use_cookies_for_streams": bool(cfg.get("use_cookies_for_streams", True)),
    }


def _active_cookie_args(cfg: Optional[dict] = None) -> List[str]:
    """Return shared yt-dlp cookie CLI args when enabled for streams/imports."""
    try:
        c = cfg if isinstance(cfg, dict) else load_settings()
        if not c.get("use_cookies_for_streams", True):
            return []
        args, _lbl, _src = rj_youtube_feed.resolve_ytdlp_cookie_args(c)
        return list(args)
    except Exception:
        return []


def invalidate_music_feed_cache() -> None:
    with _YTM_FEED_CACHE_LOCK:
        _YTM_FEED_CACHE.clear()


def save_settings(cfg: dict) -> None:
    _write_json(os.path.join(get_ytm_dir(), "settings.json"), cfg)


def load_liked() -> List[dict]:
    data = _read_json(os.path.join(get_ytm_dir(), "liked.json"), [])
    return data if isinstance(data, list) else []


def is_liked(track_id: str) -> bool:
    return any(t.get("id") == track_id for t in load_liked())


def toggle_like(track: dict) -> bool:
    tid = track.get("id", "")
    if not tid:
        return False
    liked = load_liked()
    for i, item in enumerate(liked):
        if item.get("id") == tid:
            liked.pop(i)
            _write_json(os.path.join(get_ytm_dir(), "liked.json"), liked)
            return False
    liked.insert(0, {
        "id": tid,
        "title": track.get("title", "Untitled"),
        "artist": track.get("artist", "YouTube Music"),
        "album": track.get("album", ""),
        "duration": int(track.get("duration") or 0),
        "thumb": track.get("thumb", ""),
        "local": bool(track.get("local", False)),
        "path": track.get("path", ""),
    })
    _write_json(os.path.join(get_ytm_dir(), "liked.json"), liked)
    return True


def load_recent() -> List[dict]:
    data = _read_json(os.path.join(get_ytm_dir(), "recent.json"), [])
    return data[:100] if isinstance(data, list) else []


def record_recent(track: dict) -> None:
    tid = track.get("id", "")
    if not tid:
        return
    rec = [t for t in load_recent() if t.get("id") != tid]
    rec.insert(0, {
        "id": tid,
        "title": track.get("title", "Untitled"),
        "artist": track.get("artist", "YouTube Music"),
        "album": track.get("album", ""),
        "duration": int(track.get("duration") or 0),
        "thumb": track.get("thumb", ""),
        "local": bool(track.get("local", False)),
        "path": track.get("path", ""),
        "played_at": int(time.time()),
    })
    _write_json(os.path.join(get_ytm_dir(), "recent.json"), rec[:100])
    invalidate_music_feed_cache()


def remove_recent_item(track_id: str) -> bool:
    """Remove a single track from local YT Music Listening History."""
    tid = str(track_id or "").strip()
    if not tid:
        return False
    rec = load_recent()
    kept = [t for t in rec if str(t.get("id") or "") != tid]
    if len(kept) == len(rec):
        return False
    _write_json(os.path.join(get_ytm_dir(), "recent.json"), kept)
    invalidate_music_feed_cache()
    return True


def clear_listen_history(clear_stats: bool = True) -> int:
    """Clear local YT Music Listening History (and optionally Most Played stats).

    Preserves explicit dismissed tracks & muted artists. Returns number of
    history items removed.
    """
    rec = load_recent()
    count = len(rec)
    _write_json(os.path.join(get_ytm_dir(), "recent.json"), [])
    if clear_stats:
        _write_json(os.path.join(get_ytm_dir(), "stats.json"), {"tracks": {}, "total_sec": 0})
    invalidate_music_feed_cache()
    return count


# ---- Music Taste Filters (Not Interested / Hidden Artists) ----

def _music_taste_path() -> str:
    return os.path.join(get_ytm_dir(), "music_taste.json")


def load_music_taste() -> dict:
    raw = _read_json(_music_taste_path(), {})
    if not isinstance(raw, dict):
        raw = {}
    d_ids = [str(x).strip() for x in (raw.get("dismissed_ids") or []) if str(x).strip()]
    m_art = [str(x).strip()[:48] for x in (raw.get("muted_artists") or []) if str(x).strip()]
    return {
        "dismissed_ids": list(dict.fromkeys(d_ids))[:500],
        "muted_artists": list(dict.fromkeys(m_art))[:200],
    }


def save_music_taste(taste: dict) -> None:
    _write_json(_music_taste_path(), {
        "dismissed_ids": list(taste.get("dismissed_ids") or [])[:500],
        "muted_artists": list(taste.get("muted_artists") or [])[:200],
    })


def dismiss_music_track(track_id: str) -> bool:
    tid = str(track_id or "").strip()
    if not tid:
        return False
    t = load_music_taste()
    if tid not in t["dismissed_ids"]:
        t["dismissed_ids"].insert(0, tid)
        save_music_taste(t)
        invalidate_music_feed_cache()
        return True
    return False


def mute_music_artist(artist: str) -> bool:
    art = str(artist or "").strip()[:48]
    if not art:
        return False
    t = load_music_taste()
    if not any(a.lower() == art.lower() for a in t["muted_artists"]):
        t["muted_artists"].insert(0, art)
        save_music_taste(t)
        invalidate_music_feed_cache()
        return True
    return False


def unmute_music_artist(artist: str) -> bool:
    art = str(artist or "").strip().lower()
    if not art:
        return False
    t = load_music_taste()
    before = len(t["muted_artists"])
    t["muted_artists"] = [a for a in t["muted_artists"] if a.lower() != art]
    if len(t["muted_artists"]) == before:
        return False
    save_music_taste(t)
    invalidate_music_feed_cache()
    return True


def get_muted_artists() -> List[str]:
    return list(load_music_taste().get("muted_artists", []))


def reset_music_taste(clear_history: bool = True, clear_stats: bool = True) -> None:
    """Wipe dismissed tracks, hidden artists, and optionally listening history & stats."""
    save_music_taste({"dismissed_ids": [], "muted_artists": []})
    if clear_history:
        clear_listen_history(clear_stats=clear_stats)
    else:
        invalidate_music_feed_cache()


# ---- Listening Stats & Most Played ----

def load_stats() -> dict:
    data = _read_json(os.path.join(get_ytm_dir(), "stats.json"), {})
    if not isinstance(data, dict):
        return {"tracks": {}, "total_sec": 0}
    data.setdefault("tracks", {})
    data.setdefault("total_sec", 0)
    return data


def record_track_play(track: dict, played_sec: float = 0.0) -> None:
    tid = str(track.get("id") or "").strip()
    if not tid:
        return
    st = load_stats()
    trks = st.setdefault("tracks", {})
    cur = trks.get(tid, {})
    cur["plays"] = int(cur.get("plays", 0)) + 1
    cur["last_played"] = int(time.time())
    cur["track"] = {
        "id": tid,
        "title": track.get("title", "Untitled"),
        "artist": track.get("artist", "YouTube Music"),
        "album": track.get("album", ""),
        "duration": int(track.get("duration") or 0),
        "thumb": track.get("thumb", ""),
        "local": bool(track.get("local", False)),
        "path": track.get("path", ""),
    }
    trks[tid] = cur
    if played_sec > 0:
        st["total_sec"] = int(st.get("total_sec", 0)) + int(played_sec)
    _write_json(os.path.join(get_ytm_dir(), "stats.json"), st)


def get_most_played_tracks(limit: int = 30) -> List[dict]:
    st = load_stats()
    trks = st.get("tracks", {})
    if not isinstance(trks, dict):
        return []
    ranked = sorted(
        trks.values(),
        key=lambda item: (int(item.get("plays", 0)), int(item.get("last_played", 0))),
        reverse=True,
    )
    out: List[dict] = []
    for entry in ranked[:limit]:
        t = entry.get("track")
        if isinstance(t, dict) and t.get("id"):
            copy_t = dict(t)
            copy_t["plays"] = int(entry.get("plays", 1))
            out.append(copy_t)
    return out


# ---- Offline Index & Smart Offline-First Resolution ----

def load_offline_index() -> dict:
    data = _read_json(os.path.join(get_ytm_dir(), "offline_index.json"), {})
    if not isinstance(data, dict):
        return {"by_id": {}, "by_path": {}}
    data.setdefault("by_id", {})
    data.setdefault("by_path", {})
    return data


def save_offline_index(idx: dict) -> None:
    _write_json(os.path.join(get_ytm_dir(), "offline_index.json"), idx)


def probe_audio_duration(filepath: str) -> int:
    """Return rounded integer audio duration in seconds via ffprobe (0 on failure)."""
    if not filepath or not os.path.isfile(filepath) or not shutil.which("ffprobe"):
        return 0
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", filepath,
            ],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            return max(0, int(round(float(r.stdout.strip()))))
    except Exception:
        pass
    return 0


def register_offline_file(track: dict, filepath: str, folder: str = "") -> dict:
    """Register a downloaded file in offline_index.json for instant offline-first lookup."""
    idx = load_offline_index()
    abs_p = os.path.abspath(filepath)
    dur = int(track.get("duration") or 0)
    if dur <= 0:
        dur = probe_audio_duration(abs_p)
    tid = str(track.get("id") or f"local:{os.path.basename(abs_p)}")
    meta = {
        "id": tid,
        "path": abs_p,
        "title": str(track.get("title") or os.path.splitext(os.path.basename(abs_p))[0])[:96],
        "artist": str(track.get("artist") or "Offline Library")[:48],
        "album": str(track.get("album") or folder or "Offline")[:48],
        "duration": dur,
        "thumb": str(track.get("thumb") or ""),
        "folder": folder,
    }
    if tid and not tid.startswith("local:"):
        idx["by_id"][tid] = meta
    idx["by_path"][abs_p] = meta
    save_offline_index(idx)
    return meta


def find_local_path_for_track(track: Optional[dict], _index: Optional[dict] = None) -> str:
    """Resolve an existing downloaded local audio path for *track* if present on disk."""
    if not isinstance(track, dict):
        return ""
    direct = str(track.get("path") or "")
    if direct and os.path.isfile(direct):
        return direct

    idx = _index if _index is not None else load_offline_index()
    tid = str(track.get("id") or "")
    if tid and tid in idx.get("by_id", {}):
        cand = str(idx["by_id"][tid].get("path") or "")
        if cand and os.path.isfile(cand):
            return cand

    # Fallback filename check across Library/ (root & subfolders)
    raw_name = f"{track.get('artist', 'Artist')} - {track.get('title', tid)}"
    safe = sanitize_filename(raw_name, max_len=72, fallback=tid or "track")
    safe_low = safe.lower()
    lib_dir = get_library_dir()
    if os.path.isdir(lib_dir):
        for root, _dirs, files in os.walk(lib_dir):
            for fn in files:
                if not fn.lower().endswith(_AUDIO_EXTS):
                    continue
                if os.path.splitext(fn)[0].lower() == safe_low:
                    full = os.path.join(root, fn)
                    if os.path.isfile(full):
                        return full
    return ""


def is_track_offline(track: Optional[dict], _index: Optional[dict] = None) -> bool:
    return bool(find_local_path_for_track(track, _index=_index))


# ---- Playlists CRUD, Sorting, Reordering, Syncing & M3U Export ----

def load_playlists() -> List[dict]:
    data = _read_json(os.path.join(get_ytm_dir(), "playlists.json"), [])
    return data if isinstance(data, list) else []


def save_playlists(playlists: List[dict]) -> None:
    _write_json(os.path.join(get_ytm_dir(), "playlists.json"), playlists)


def create_playlist(
    name: str,
    tracks: Optional[List[dict]] = None,
    source_url: str = "",
    offline_dir: str = "",
) -> dict:
    pls = load_playlists()
    pid = f"pl_{int(time.time())}_{random.randint(100, 999)}"
    pl = {
        "id": pid,
        "name": (name or "").strip() or "My Playlist",
        "tracks": [dict(t) for t in (tracks or [])],
        "source_url": str(source_url or "").strip(),
        "offline_dir": str(offline_dir or "").strip(),
        "created_at": int(time.time()),
    }
    pls.insert(0, pl)
    save_playlists(pls)
    return pl


def rename_playlist(playlist_id: str, new_name: str) -> Tuple[bool, str]:
    clean = (new_name or "").strip()
    if not clean:
        return False, "Name cannot be empty"
    pls = load_playlists()
    for pl in pls:
        if pl.get("id") == playlist_id:
            old_dir_name = str(pl.get("offline_dir") or "").strip()
            new_dir_name = sanitize_filename(clean, max_len=56, fallback="Playlist")
            if old_dir_name and old_dir_name != new_dir_name:
                old_full = os.path.join(get_library_dir(), old_dir_name)
                new_full = os.path.join(get_library_dir(), new_dir_name)
                if os.path.isdir(old_full) and not os.path.exists(new_full):
                    try:
                        os.rename(old_full, new_full)
                        pl["offline_dir"] = new_dir_name
                        _rewrite_offline_paths_prefix(old_full, new_full, pls)
                    except OSError:
                        pass
            pl["name"] = clean[:56]
            save_playlists(pls)
            return True, f"Renamed to {pl['name']}"
    return False, "Playlist not found"


def _rewrite_offline_paths_prefix(old_prefix: str, new_prefix: str, playlists: List[dict]) -> None:
    old_abs = os.path.abspath(old_prefix)
    new_abs = os.path.abspath(new_prefix)
    idx = load_offline_index()
    new_by_path: Dict[str, dict] = {}
    for p, meta in idx.get("by_path", {}).items():
        if p.startswith(old_abs + os.sep) or p == old_abs:
            np_path = new_abs + p[len(old_abs):]
            meta = dict(meta)
            meta["path"] = np_path
            meta["folder"] = os.path.basename(new_abs)
            new_by_path[np_path] = meta
            tid = meta.get("id", "")
            if tid and tid in idx.get("by_id", {}):
                idx["by_id"][tid] = meta
        else:
            new_by_path[p] = meta
    idx["by_path"] = new_by_path
    save_offline_index(idx)
    for pl in playlists:
        for t in pl.get("tracks", []):
            tp = str(t.get("path") or "")
            if tp and (os.path.abspath(tp).startswith(old_abs + os.sep)):
                t["path"] = new_abs + os.path.abspath(tp)[len(old_abs):]


def add_track_to_playlist(playlist_id: str, track: dict) -> bool:
    return add_tracks_to_playlist(playlist_id, [track]) > 0


def add_tracks_to_playlist(playlist_id: str, tracks: List[dict]) -> int:
    """Add one or more tracks to *playlist_id*, skipping duplicates. Returns count added."""
    pls = load_playlists()
    for pl in pls:
        if pl.get("id") == playlist_id:
            cur = pl.setdefault("tracks", [])
            existing_ids = {t.get("id") for t in cur if t.get("id")}
            existing_paths = {t.get("path") for t in cur if t.get("path")}
            added = 0
            for t in tracks:
                tid = t.get("id")
                tp = t.get("path")
                if (tid and tid in existing_ids) or (tp and tp in existing_paths):
                    continue
                cur.append(dict(t))
                if tid:
                    existing_ids.add(tid)
                if tp:
                    existing_paths.add(tp)
                added += 1
            if added:
                save_playlists(pls)
            return added
    return 0


def remove_track_from_playlist(playlist_id: str, idx: int) -> bool:
    pls = load_playlists()
    for pl in pls:
        if pl.get("id") == playlist_id:
            trks = pl.get("tracks", [])
            if 0 <= idx < len(trks):
                trks.pop(idx)
                save_playlists(pls)
                return True
    return False


def move_playlist_track(playlist_id: str, idx: int, delta: int) -> int:
    pls = load_playlists()
    for pl in pls:
        if pl.get("id") == playlist_id:
            trks = pl.get("tracks", [])
            new_i = idx + delta
            if 0 <= idx < len(trks) and 0 <= new_i < len(trks):
                trks[idx], trks[new_i] = trks[new_i], trks[idx]
                save_playlists(pls)
                return new_i
    return idx


def sort_playlist_tracks(playlist_id: str, mode: str = "title") -> bool:
    pls = load_playlists()
    for pl in pls:
        if pl.get("id") == playlist_id:
            trks = pl.get("tracks", [])
            if mode == "artist":
                trks.sort(key=lambda t: (str(t.get("artist", "")).lower(), str(t.get("title", "")).lower()))
            elif mode == "duration":
                trks.sort(key=lambda t: int(t.get("duration") or 0))
            else:
                trks.sort(key=lambda t: str(t.get("title", "")).lower())
            save_playlists(pls)
            return True
    return False


def delete_playlist(playlist_id: str) -> bool:
    pls = load_playlists()
    kept = [p for p in pls if p.get("id") != playlist_id]
    if len(kept) == len(pls):
        return False
    save_playlists(kept)
    return True


def count_playlist_offline_tracks(playlist: dict, _index: Optional[dict] = None) -> int:
    idx = _index if _index is not None else load_offline_index()
    return sum(1 for t in playlist.get("tracks", []) if find_local_path_for_track(t, _index=idx))


def export_playlist_m3u(playlist: dict) -> Optional[str]:
    """Write an .m3u playlist file into Library/ for all locally available tracks."""
    idx = load_offline_index()
    lines = ["#EXTM3U"]
    found = 0
    for t in playlist.get("tracks", []):
        lp = find_local_path_for_track(t, _index=idx)
        if lp:
            dur = int(t.get("duration") or -1)
            lines.append(f"#EXTINF:{dur},{t.get('artist', '')} - {t.get('title', '')}")
            lines.append(lp)
            found += 1
    if found == 0:
        return None
    lib_dir = get_library_dir()
    os.makedirs(lib_dir, exist_ok=True)
    safe = sanitize_filename(playlist.get("name", "Playlist"), max_len=56, fallback="Playlist")
    m3u_path = os.path.join(lib_dir, f"{safe}.m3u")
    try:
        with open(m3u_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return m3u_path
    except OSError:
        return None


def list_offline_folders() -> List[dict]:
    """Return offline playlist subfolders inside loot/YTMusic/Library/ with track counts."""
    lib_dir = get_library_dir()
    folders: List[dict] = []
    if not os.path.isdir(lib_dir):
        return folders
    try:
        for entry in sorted(os.listdir(lib_dir), key=str.lower):
            full = os.path.join(lib_dir, entry)
            if not os.path.isdir(full):
                continue
            cnt = sum(
                1 for fn in os.listdir(full)
                if os.path.isfile(os.path.join(full, fn)) and fn.lower().endswith(_AUDIO_EXTS)
            )
            folders.append({"name": entry, "path": full, "count": cnt})
    except OSError:
        pass
    return folders


def list_offline_library(folder: Optional[str] = None, probe_durations: bool = False) -> List[dict]:
    """Scan local downloaded tracks in YTMusic Library (including subfolders) + YouTube Downloads."""
    lib_dir = get_library_dir()
    yt_dl_dir = os.path.join(rj_env.get_base_dir(), "loot", "YouTube", "Downloads")
    idx = load_offline_index()
    by_path = idx.get("by_path", {})
    idx_dirty = False

    scan_roots: List[Tuple[str, str]] = []
    if folder:
        scan_roots.append((os.path.join(lib_dir, folder), folder))
    else:
        if os.path.isdir(lib_dir):
            scan_roots.append((lib_dir, ""))
            try:
                for entry in sorted(os.listdir(lib_dir), key=str.lower):
                    sub = os.path.join(lib_dir, entry)
                    if os.path.isdir(sub):
                        scan_roots.append((sub, entry))
            except OSError:
                pass
        if os.path.isdir(yt_dl_dir):
            scan_roots.append((yt_dl_dir, "YouTube"))
            try:
                for entry in sorted(os.listdir(yt_dl_dir), key=str.lower):
                    sub = os.path.join(yt_dl_dir, entry)
                    if os.path.isdir(sub):
                        scan_roots.append((sub, f"YT/{entry}"))
            except OSError:
                pass

    seen = set()
    tracks: List[Tuple[float, dict]] = []
    for d_path, folder_lbl in scan_roots:
        if not os.path.isdir(d_path):
            continue
        try:
            entries = os.listdir(d_path)
        except OSError:
            continue
        for fn in entries:
            if not fn.lower().endswith(_AUDIO_EXTS):
                continue
            full = os.path.abspath(os.path.join(d_path, fn))
            if not os.path.isfile(full) or full in seen:
                continue
            seen.add(full)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                mtime = 0.0

            cached = by_path.get(full)
            if isinstance(cached, dict):
                tid = str(cached.get("id") or f"local:{fn}")
                ttl = str(cached.get("title") or os.path.splitext(fn)[0])[:90]
                art = str(cached.get("artist") or "Offline Library")[:45]
                alb = str(cached.get("album") or folder_lbl or "Offline")[:45]
                dur = int(cached.get("duration") or 0)
                thumb = str(cached.get("thumb") or "")
            else:
                stem = os.path.splitext(fn)[0]
                if " - " in stem:
                    art, ttl = stem.split(" - ", 1)
                else:
                    art, ttl = "Offline Library", stem
                tid = f"local:{fn}"
                alb = folder_lbl or "Offline"
                dur = 0
                thumb = ""

            if dur <= 0 and probe_durations:
                dur = probe_audio_duration(full)
                if dur > 0:
                    by_path[full] = {
                        "id": tid, "path": full, "title": ttl.strip()[:90],
                        "artist": art.strip()[:45], "album": alb, "duration": dur,
                        "thumb": thumb, "folder": folder_lbl,
                    }
                    idx_dirty = True

            tracks.append((mtime, {
                "id": tid,
                "title": ttl.strip()[:90],
                "artist": art.strip()[:45],
                "album": alb,
                "folder": folder_lbl,
                "duration": dur,
                "thumb": thumb,
                "local": True,
                "path": full,
                "mtime": mtime,
            }))

    if idx_dirty:
        save_offline_index(idx)
    tracks.sort(key=lambda pair: pair[0], reverse=True)
    return [t for _, t in tracks]


def move_offline_track_to_folder(track_path: str, folder_name: str) -> Tuple[bool, str, str]:
    """Physically move an offline track into loot/YTMusic/Library/<folder_name>/ (or root if empty)."""
    src = os.path.abspath(str(track_path or ""))
    if not src or not os.path.isfile(src):
        return False, "File not found", ""
    clean_folder = sanitize_filename(folder_name, max_len=56, fallback="") if folder_name else ""
    dest_dir = os.path.join(get_library_dir(), clean_folder) if clean_folder else get_library_dir()
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.abspath(os.path.join(dest_dir, os.path.basename(src)))
    if src == dest:
        return True, "Already in folder", dest
    try:
        shutil.move(src, dest)
    except OSError as exc:
        return False, str(exc)[:44], ""

    # Update offline_index.json and any playlist references
    idx = load_offline_index()
    meta = idx.get("by_path", {}).pop(src, None)
    if isinstance(meta, dict):
        meta["path"] = dest
        meta["folder"] = clean_folder
        idx["by_path"][dest] = meta
        tid = meta.get("id", "")
        if tid and tid in idx.get("by_id", {}):
            idx["by_id"][tid] = meta
        save_offline_index(idx)
    pls = load_playlists()
    changed = False
    for pl in pls:
        for t in pl.get("tracks", []):
            if t.get("path") and os.path.abspath(t["path"]) == src:
                t["path"] = dest
                changed = True
    if changed:
        save_playlists(pls)
    return True, f"Moved to {clean_folder or 'Library'}", dest


# ---------------------------------------------------------------------------
# Async Cover Art & Lyrics Loaders (Pure Stdlib + PIL)
# ---------------------------------------------------------------------------

class CoverArtCache:
    """Async square-cropped album cover fetcher with disk cache & offline art support."""

    def __init__(self):
        self._mem: Dict[str, Optional[Image.Image]] = {}
        self._pending: set = set()
        self._lock = threading.Lock()
        self.dirty = False

    def get(self, track: Optional[dict]) -> Optional[Image.Image]:
        if not track:
            return None
        tid = str(track.get("id") or "")
        if not tid:
            return None
        with self._lock:
            if tid in self._mem:
                return self._mem[tid]
            if tid in self._pending:
                return None
            self._pending.add(tid)
        url = "" if track.get("local") and tid.startswith("local:") else str(
            track.get("thumb") or (f"https://i.ytimg.com/vi/{tid}/hqdefault.jpg" if not tid.startswith("local:") else "")
        )
        local_path = str(track.get("path") or "")
        threading.Thread(target=self._worker, args=(tid, url, local_path), daemon=True).start()
        return None

    def _worker(self, tid: str, url: str, local_path: str = "") -> None:
        safe = re.sub(r"[^A-Za-z0-9_-]", "", tid)[:32] or "cover"
        cpath = os.path.join(get_thumbs_dir(), f"{safe}.jpg")
        img: Optional[Image.Image] = None
        try:
            if os.path.isfile(cpath):
                with Image.open(cpath) as raw:
                    img = raw.convert("RGB")
            elif local_path and os.path.isfile(local_path) and shutil.which("ffmpeg"):
                # Try extracting embedded ID3/MP4 album art from offline file
                r = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", local_path, "-an", "-vframes", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                    capture_output=True, timeout=4,
                )
                if r.returncode == 0 and r.stdout:
                    with Image.open(io.BytesIO(r.stdout)) as raw:
                        rgb = raw.convert("RGB")
                        w, h = rgb.size
                        side = min(w, h)
                        img = rgb.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2)).resize((180, 180), Image.BILINEAR)
            if img is None and url:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    blob = resp.read(512 * 1024)
                with Image.open(io.BytesIO(blob)) as raw:
                    rgb = raw.convert("RGB")
                    w, h = rgb.size
                    side = min(w, h)
                    left = (w - side) // 2
                    top = (h - side) // 2
                    img = rgb.crop((left, top, left + side, top + side)).resize((180, 180), Image.BILINEAR)
                try:
                    os.makedirs(get_thumbs_dir(), exist_ok=True)
                    img.save(cpath, "JPEG", quality=82)
                except Exception:
                    pass
        except Exception:
            img = None
        with self._lock:
            self._mem[tid] = img
            self._pending.discard(tid)
            if img is not None:
                self.dirty = True


class LyricsCache:
    """Async synced/plain lyrics fetcher using lrclib.net + local JSON cache."""

    def __init__(self):
        self._mem: Dict[str, dict] = {}
        self._pending: set = set()
        self._lock = threading.Lock()

    def get(self, track: Optional[dict]) -> dict:
        if not track:
            return {"status": "empty", "synced": [], "plain": []}
        tid = str(track.get("id") or "")
        with self._lock:
            if tid in self._mem:
                return self._mem[tid]
            if tid in self._pending:
                return {"status": "loading", "synced": [], "plain": []}
            self._pending.add(tid)
        threading.Thread(
            target=self._worker,
            args=(tid, track.get("artist", ""), track.get("title", ""), int(track.get("duration") or 0)),
            daemon=True,
        ).start()
        return {"status": "loading", "synced": [], "plain": []}

    def _worker(self, tid: str, artist: str, title: str, duration: int) -> None:
        safe = re.sub(r"[^A-Za-z0-9_-]", "", tid)[:32]
        cpath = os.path.join(get_lyrics_dir(), f"{safe}.json")
        res = {"status": "none", "synced": [], "plain": []}
        try:
            if os.path.isfile(cpath):
                cached = _read_json(cpath, None)
                if isinstance(cached, dict):
                    res = cached
            else:
                q_params = {"track_name": title}
                if artist and artist.lower() not in ("youtube music", "offline library"):
                    q_params["artist_name"] = artist
                if duration > 0:
                    q_params["duration"] = str(duration)
                qs = urllib.parse.urlencode(q_params)
                urls = [
                    f"https://lrclib.net/api/get?{qs}",
                    f"https://lrclib.net/api/search?{urllib.parse.urlencode({'q': f'{artist} {title}'.strip()})}",
                ]
                item = None
                for u in urls:
                    try:
                        req = urllib.request.Request(u, headers={"User-Agent": "EmberJack-YTMusic/2.0"})
                        with urllib.request.urlopen(req, timeout=5) as resp:
                            data = json.loads(resp.read(256 * 1024).decode("utf-8", "ignore"))
                        if isinstance(data, dict) and (data.get("syncedLyrics") or data.get("plainLyrics")):
                            item = data
                            break
                        if isinstance(data, list) and data:
                            for cand in data:
                                if cand.get("syncedLyrics") or cand.get("plainLyrics"):
                                    item = cand
                                    break
                            if item:
                                break
                    except Exception:
                        continue
                if item:
                    synced = parse_lrc(item.get("syncedLyrics") or "")
                    plain = [ln.strip() for ln in (item.get("plainLyrics") or "").splitlines() if ln.strip()]
                    if synced or plain:
                        res = {"status": "ok", "synced": synced, "plain": plain}
                        _write_json(cpath, res)
        except Exception:
            pass
        with self._lock:
            self._mem[tid] = res
            self._pending.discard(tid)


# ---------------------------------------------------------------------------
# YouTube Music Network Fetchers & Batch Downloader (yt-dlp CLI)
# ---------------------------------------------------------------------------

def search_music(query: str, max_results: int = 15) -> Tuple[List[dict], str]:
    """Search YouTube Music tracks via yt-dlp."""
    q = (query or "").strip()
    if not q:
        return [], ""
    if not shutil.which("yt-dlp"):
        return [], "yt-dlp not installed"
    try:
        r = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--no-download", "--no-warnings", "-j", f"ytsearch{max_results}:{q}"],
            capture_output=True, text=True, timeout=35,
        )
        if r.returncode != 0 and not r.stdout.strip():
            return [], (r.stderr or "Search failed").strip().splitlines()[-1][:48]
        tracks = []
        for line in r.stdout.splitlines():
            if not line.strip():
                continue
            try:
                t = normalize_track_metadata(json.loads(line))
                if t:
                    tracks.append(t)
            except Exception:
                continue
        return tracks, ""
    except subprocess.TimeoutExpired:
        return [], "Search timed out"
    except Exception as exc:
        return [], str(exc)[:48]


def fetch_playlist_details(url: str, limit: int = 100) -> Tuple[str, List[dict], str]:
    """Fetch a YouTube / YT Music playlist's REAL title and tracks."""
    if not shutil.which("yt-dlp"):
        return "", [], "yt-dlp not installed"
    c_args = _active_cookie_args()
    try:
        r = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--no-download", "--no-warnings", "--playlist-end", str(limit), *c_args, "-j", url],
            capture_output=True, text=True, timeout=55,
        )
        if (r.returncode != 0 or not r.stdout.strip()) and c_args:
            r = subprocess.run(
                ["yt-dlp", "--flat-playlist", "--no-download", "--no-warnings", "--playlist-end", str(limit), "-j", url],
                capture_output=True, text=True, timeout=55,
            )
        if r.returncode != 0 and not r.stdout.strip():
            return "", [], (r.stderr or "Playlist fetch failed").strip().splitlines()[-1][:48]
        raw_entries: List[dict] = []
        tracks: List[dict] = []
        for line in r.stdout.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                raw_entries.append(obj)
                t = normalize_track_metadata(obj)
                if t:
                    tracks.append(t)
            except Exception:
                continue
        pl_title = extract_playlist_title_from_entries(raw_entries, fallback="")
        if not pl_title:
            # Lightweight single-JSON header fallback to grab container playlist title
            try:
                r2 = subprocess.run(
                    ["yt-dlp", "--flat-playlist", "--no-download", "--no-warnings", "--playlist-items", "1", "-J", url],
                    capture_output=True, text=True, timeout=15,
                )
                if r2.returncode == 0 and r2.stdout.strip():
                    meta = json.loads(r2.stdout)
                    pl_title = extract_playlist_title_from_entries([meta], fallback="YouTube Playlist")
            except Exception:
                pl_title = "YouTube Playlist"
        return (pl_title or "YouTube Playlist"), tracks, ""
    except subprocess.TimeoutExpired:
        return "", [], "Playlist timed out"
    except Exception as exc:
        return "", [], str(exc)[:48]


def fetch_playlist_tracks(url: str, limit: int = 40) -> Tuple[List[dict], str]:
    """Backwards-compatible wrapper returning (tracks, err)."""
    _title, tracks, err = fetch_playlist_details(url, limit=limit)
    return tracks, err


def sync_imported_playlist(playlist_id: str, limit: int = 100) -> Tuple[int, str]:
    """Re-fetch an imported playlist's source_url and append any newly added songs."""
    pls = load_playlists()
    target = next((p for p in pls if p.get("id") == playlist_id), None)
    if target is None:
        return 0, "Playlist not found"
    src_url = str(target.get("source_url") or "").strip()
    if not src_url:
        return 0, "Not linked to a YouTube URL"
    pl_title, tracks, err = fetch_playlist_details(src_url, limit=limit)
    if err and not tracks:
        return 0, err
    added = add_tracks_to_playlist(playlist_id, tracks)
    if pl_title and target.get("name", "").startswith("Imported"):
        rename_playlist(playlist_id, pl_title)
    return added, (f"Added {added} new tracks" if added else "Playlist already up to date")


def fetch_single_track(video_id: str) -> Tuple[Optional[dict], str]:
    """Fetch exact metadata for a single YouTube video ID."""
    if not shutil.which("yt-dlp"):
        return None, "yt-dlp not installed"
    url = f"https://www.youtube.com/watch?v={video_id}"
    c_args = _active_cookie_args()
    try:
        r = subprocess.run(
            ["yt-dlp", "--no-download", "--no-playlist", "--no-warnings", *c_args, "-j", url],
            capture_output=True, text=True, timeout=25,
        )
        if (r.returncode != 0 or not r.stdout.strip()) and c_args:
            r = subprocess.run(
                ["yt-dlp", "--no-download", "--no-playlist", "--no-warnings", "-j", url],
                capture_output=True, text=True, timeout=25,
            )
        if r.returncode == 0 and r.stdout.strip():
            return normalize_track_metadata(json.loads(r.stdout.splitlines()[0])), ""
        return None, (r.stderr or "Track unavailable").strip().splitlines()[-1][:48]
    except subprocess.TimeoutExpired:
        return None, "Track lookup timed out"
    except Exception as exc:
        return None, str(exc)[:48]


def fetch_radio_mix(video_id: str, limit: int = 18) -> Tuple[List[dict], str]:
    """Fetch YouTube Music Auto-Radio Mix (`list=RD<video_id>`) for endless similar tracks."""
    if not video_id or video_id.startswith("local:"):
        return [], "Radio requires a YouTube track"
    radio_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    tracks, err = fetch_playlist_tracks(radio_url, limit=limit)
    filtered = [t for t in tracks if t.get("id") != video_id]
    return filtered, err


def resolve_best_audio_url(video_id: str) -> Tuple[str, str]:
    if not shutil.which("yt-dlp"):
        return "", "yt-dlp not installed"
    url = f"https://www.youtube.com/watch?v={video_id}"
    c_args = _active_cookie_args()
    attempts = [c_args, []] if c_args else [[]]
    last_err = ""
    for fmt in ("bestaudio[ext=m4a]/bestaudio/best", "bestaudio/best", "best"):
        for extra_c in attempts:
            try:
                r = subprocess.run(
                    ["yt-dlp", "-f", fmt, "--no-playlist", "--no-warnings", *extra_c, "--get-url", url],
                    capture_output=True, text=True, timeout=30,
                )
                lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip().startswith("http")]
                if lines:
                    return lines[0], ""
                if r.stderr:
                    last_err = r.stderr.strip().splitlines()[-1][:48]
            except subprocess.TimeoutExpired:
                return "", "Audio stream timed out"
            except Exception as exc:
                return "", str(exc)[:48]
    return "", last_err or "Could not resolve audio URL"


# ---------------------------------------------------------------------------
# Personalized YT Music Feed Engine (Tier 1 Local Smart Mix + Tier 2 Account)
# ---------------------------------------------------------------------------

def _short_reason_lbl(text: str, max_len: int = 18) -> str:
    s = (text or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max(1, max_len - 1)].rstrip() + "…"


def _interleave_by_artist(tracks: List[dict], max_consecutive: int = 2) -> List[dict]:
    """Reorder ranked tracks so at most `max_consecutive` songs share an artist."""
    pool = list(tracks)
    result: List[dict] = []
    while pool:
        last_art = ""
        streak = 0
        if result:
            last_art = str(result[-1].get("artist") or "").strip().lower()
            if last_art and last_art != "youtube music":
                for prev in reversed(result):
                    if str(prev.get("artist") or "").strip().lower() == last_art:
                        streak += 1
                    else:
                        break
        picked_idx = 0
        if last_art and last_art != "youtube music" and streak >= max_consecutive:
            for idx, cand in enumerate(pool):
                if str(cand.get("artist") or "").strip().lower() != last_art:
                    picked_idx = idx
                    break
        result.append(pool.pop(picked_idx))
    return result


def _filter_blocked_music(tracks: List[dict], exclude_ids: Optional[Set[str]] = None) -> List[dict]:
    taste = load_music_taste()
    blocked_ids = set(taste.get("dismissed_ids", [])) | set(exclude_ids or set())
    muted_arts = {a.lower() for a in taste.get("muted_artists", [])}
    out: List[dict] = []
    seen: Set[str] = set()
    for t in tracks:
        tid = str(t.get("id") or "").strip()
        art = str(t.get("artist") or "").strip().lower()
        if not tid or tid in blocked_ids or tid in seen:
            continue
        if art and art in muted_arts:
            continue
        seen.add(tid)
        out.append(t)
    return out


def fetch_ytm_account_feed(
    feed_kind: str = "supermix",
    start: int = 1,
    count: int = 20,
    cookie_args: Optional[Sequence[str]] = None,
    timeout: float = 35.0,
) -> Tuple[List[dict], str]:
    """Fetch a real authenticated YouTube Music feed via shared cookies."""
    cargs = list(cookie_args or [])
    if not cargs:
        return [], "No YouTube cookies configured"
    if not shutil.which("yt-dlp"):
        return [], "yt-dlp not installed"

    target_url, target_label = YTM_ACCOUNT_FEED_TARGETS.get(
        feed_kind, YTM_ACCOUNT_FEED_TARGETS["supermix"]
    )
    p_start = max(1, int(start))
    p_end = p_start + max(4, int(count)) - 1
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--no-warnings",
        "--playlist-start",
        str(p_start),
        "--playlist-end",
        str(p_end),
        *cargs,
        target_url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return [], "Account mix lookup timed out"
    except Exception as exc:
        return [], str(exc)[:64]

    stderr_l = (r.stderr or "").lower()
    if r.returncode != 0 or not (r.stdout or "").strip():
        if any(tok in stderr_l for tok in ("sign in", "login", "cookies", "401", "403", "authenticated")):
            return [], "YouTube cookies expired or not signed in"
        if any(tok in stderr_l for tok in ("could not copy", "secretstorage", "pycryptodomex", "keyring")):
            return [], "Browser cookie DB locked — upload cookies.txt via WebUI"
        err_line = next((ln.strip() for ln in (r.stderr or "").splitlines() if ln.strip()), "")
        return [], (err_line[:68] if err_line else f"Could not fetch {target_label}")

    try:
        data = json.loads(r.stdout)
    except Exception:
        return [], "Invalid response from yt-dlp"

    entries = data.get("entries") or []
    out: List[dict] = []
    seen: Set[str] = set()
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        tr = normalize_track_metadata(ent)
        if not tr or tr["id"] in seen:
            continue
        if tr["title"] in ("[Deleted video]", "[Private video]"):
            continue
        tr["reason"] = target_label
        seen.add(tr["id"])
        out.append(tr)

    if not out:
        if any(tok in stderr_l for tok in ("sign in", "login", "cookies")):
            return [], "YouTube cookies expired or not signed in"
        return [], f"No tracks in {target_label}"
    return out, ""


def build_local_music_feed(
    page: int = 1,
    exclude_ids: Optional[Set[str]] = None,
    recent: Optional[List[dict]] = None,
    most_played: Optional[List[dict]] = None,
    liked: Optional[List[dict]] = None,
    playlists: Optional[List[dict]] = None,
    taste: Optional[dict] = None,
    page_size: int = 18,
    fetch_mix_fn: Optional[Callable[[str, int], Tuple[List[dict], str]]] = None,
    search_fn: Optional[Callable[[str, int], Tuple[List[dict], str]]] = None,
) -> Tuple[List[dict], str, dict]:
    """Build a music-first zero-login personalized mix from local YT Music signals."""
    rec_list = list(recent if recent is not None else load_recent())
    top_list = list(most_played if most_played is not None else get_most_played_tracks(limit=30))
    lk_list = list(liked if liked is not None else load_liked())
    pl_list = list(playlists if playlists is not None else load_playlists())
    t_prof = taste if isinstance(taste, dict) else load_music_taste()

    mix_fn = fetch_mix_fn or fetch_radio_mix
    srch_fn = search_fn or search_music
    p_idx = max(0, int(page) - 1)

    dismissed: Set[str] = set(t_prof.get("dismissed_ids", []))
    muted_arts: Set[str] = {a.lower() for a in t_prof.get("muted_artists", [])}
    recent_ids: Set[str] = {str(t.get("id") or "") for t in rec_list[:15] if t.get("id")}
    blocked_ids: Set[str] = set(exclude_ids or set()) | dismissed | recent_ids

    # Track artist affinity weights for ranking
    artist_weights: Dict[str, float] = {}

    def _bump_artist(art: str, w: float) -> None:
        a = str(art or "").strip().lower()
        if a and a not in ("youtube music", "offline library", "various artists"):
            artist_weights[a] = artist_weights.get(a, 0.0) + w

    track_seeds: List[Tuple[str, str, float]] = []
    seen_seed_ids: Set[str] = set()

    def _add_seed(tr: dict, reason: str, score: float) -> None:
        if not isinstance(tr, dict):
            return
        tid = str(tr.get("id") or "").strip()
        if not tid or tid.startswith("local:") or tid in seen_seed_ids:
            return
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", tid):
            return
        seen_seed_ids.add(tid)
        track_seeds.append((tid, reason, score))

    for tr in top_list:
        art = tr.get("artist") or tr.get("title") or "Favorite"
        _bump_artist(art, min(4.0, float(tr.get("plays", 1)) * 0.8))
        _add_seed(tr, f"Because you played {_short_reason_lbl(art)}", 3.2)

    for tr in rec_list:
        art = tr.get("artist") or tr.get("title") or "Recent"
        _bump_artist(art, 0.7)
        _add_seed(tr, f"Because you played {_short_reason_lbl(art)}", 3.0)

    for tr in lk_list:
        art = tr.get("artist") or tr.get("title") or "Liked"
        _bump_artist(art, 1.5)
        _add_seed(tr, f"Similar to Liked: {_short_reason_lbl(art)}", 2.8)

    for pl in pl_list:
        if not isinstance(pl, dict):
            continue
        pnm = str(pl.get("name") or "Playlist")
        for tr in (pl.get("tracks") or [])[:5]:
            if isinstance(tr, dict):
                _bump_artist(tr.get("artist", ""), 0.5)
                _add_seed(tr, f"From {_short_reason_lbl(pnm)}", 2.3)

    # Artist discovery queries from top favorite artists (even if user only played offline files!)
    top_artists = [
        a for a, _w in sorted(artist_weights.items(), key=lambda kv: kv[1], reverse=True)
        if a not in muted_arts
    ]
    artist_queries: List[Tuple[str, str, float]] = [
        (f"{a} official audio songs", f"More by {_short_reason_lbl(a.title())}", 2.0)
        for a in top_artists[:8]
    ]

    is_cold_start = not (track_seeds or artist_queries)
    tasks: List[Tuple[str, str, str, float]] = []

    if is_cold_start:
        # Rotate through RADIO_STATIONS 3 per page for instant starter mix
        n_st = len(RADIO_STATIONS)
        start_s = (p_idx * 3) % n_st
        for off in range(min(3, n_st)):
            st_name, st_q, _desc = RADIO_STATIONS[(start_s + off) % n_st]
            tasks.append(("search", st_q, f"Starter Mix: {_short_reason_lbl(st_name)}", 1.8))
    else:
        if track_seeds:
            t_start = p_idx * 3
            t_slice = track_seeds[t_start:t_start + 3]
            if not t_slice and p_idx > 0:
                t_slice = track_seeds[:2]
            for tid, reason, score in t_slice:
                tasks.append(("mix", tid, reason, score))
        if artist_queries:
            a_per = 2 if len(tasks) < 2 else 1
            a_start = p_idx * a_per
            for q, reason, score in artist_queries[a_start:a_start + a_per]:
                tasks.append(("search", q, reason, score))

    if not tasks:
        return [], "No more music recommendations available", {
            "source": "local",
            "source_label": "Local Smart Mix",
            "cold_start": is_cold_start,
        }

    def _run_task(t: Tuple[str, str, str, float]) -> Tuple[List[dict], str, float]:
        kind, arg, reason, base_score = t
        try:
            if kind == "mix":
                res, _err = mix_fn(arg, 16 if p_idx == 0 else 22)
                return list(res or []), reason, base_score
            res, _err = srch_fn(arg, 10 if p_idx == 0 else 14)
            return list(res or []), reason, base_score
        except Exception:
            return [], reason, base_score

    batches: List[Tuple[List[dict], str, float]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(4, len(tasks)))) as pool:
        futs = [pool.submit(_run_task, t) for t in tasks]
        for fut in futs:
            try:
                batches.append(fut.result(timeout=28.0))
            except Exception:
                pass

    candidates: Dict[str, Tuple[float, dict, int]] = {}
    for batch, reason, base_score in batches:
        for pos, raw_tr in enumerate(batch):
            if not isinstance(raw_tr, dict):
                continue
            tid = str(raw_tr.get("id") or "").strip()
            if not tid or tid in blocked_ids:
                continue
            art = str(raw_tr.get("artist") or "").strip()
            if art and art.lower() in muted_arts:
                continue
            score = base_score - (pos * 0.06)
            if art and art.lower() in artist_weights:
                score += min(1.8, artist_weights[art.lower()] * 0.35)
            if tid in candidates:
                prev_s, prev_t, hits = candidates[tid]
                candidates[tid] = (max(prev_s, score) + 1.4, prev_t, hits + 1)
            else:
                tr_copy = dict(raw_tr)
                tr_copy["reason"] = str(tr_copy.get("reason") or reason)
                if not tr_copy.get("thumb") and not tid.startswith("local:"):
                    tr_copy["thumb"] = f"https://i.ytimg.com/vi/{tid}/hqdefault.jpg"
                candidates[tid] = (score, tr_copy, 1)

    ranked = [entry[1] for entry in sorted(candidates.values(), key=lambda x: x[0], reverse=True)]
    diverse = _interleave_by_artist(ranked, max_consecutive=2)[:max(1, int(page_size))]

    if not diverse:
        return [], "Could not build mix (check internet connection)", {
            "source": "local",
            "source_label": "Local Smart Mix",
            "cold_start": is_cold_start,
        }
    return diverse, "", {
        "source": "local",
        "source_label": "Starter Mix" if is_cold_start else "Local Smart Mix",
        "cold_start": is_cold_start,
    }


def fetch_ytm_personalized_feed(
    cfg: Optional[dict] = None,
    source: str = "auto",
    page: int = 1,
    exclude_ids: Optional[Set[str]] = None,
    force_refresh: bool = False,
    recent: Optional[List[dict]] = None,
    most_played: Optional[List[dict]] = None,
    liked: Optional[List[dict]] = None,
    playlists: Optional[List[dict]] = None,
    fetch_mix_fn: Optional[Callable[[str, int], Tuple[List[dict], str]]] = None,
    search_fn: Optional[Callable[[str, int], Tuple[List[dict], str]]] = None,
    fetch_account_fn: Optional[Callable[..., Tuple[List[dict], str]]] = None,
) -> Tuple[List[dict], str, dict]:
    """Unified YT Music feed loader combining shared-cookie Account feeds and Local Smart Mix."""
    eff_cfg = cfg if isinstance(cfg, dict) else load_settings()
    eff_source = str(source or "auto").strip().lower()
    if eff_source == "auto":
        mode_cfg = str(eff_cfg.get("feed_mode", "auto") or "auto").strip().lower()
        if mode_cfg in ("local", "account"):
            eff_source = mode_cfg

    cookie_args, cookie_lbl, cookie_key = rj_youtube_feed.resolve_ytdlp_cookie_args(eff_cfg)
    acct_fn = fetch_account_fn or fetch_ytm_account_feed

    cache_key = f"{eff_source}:{cookie_key}"
    if page == 1 and not force_refresh and not exclude_ids:
        with _YTM_FEED_CACHE_LOCK:
            hit = _YTM_FEED_CACHE.get(cache_key)
            if hit is not None and time.monotonic() < hit[0]:
                filtered = _filter_blocked_music(hit[1])
                if filtered:
                    return filtered, "", dict(hit[2], cached=True)

    acct_map = {
        "account": "supermix",
        "account_supermix": "supermix",
        "account_liked": "liked",
        "account_history": "history",
        "account_recommended": "recommended",
    }

    if eff_source in acct_map:
        kind = acct_map[eff_source]
        if not cookie_args:
            return [], "Account not linked — upload cookies.txt in WebUI Settings", {
                "source": eff_source,
                "source_label": YTM_ACCOUNT_FEED_TARGETS[kind][1],
                "cookie_label": cookie_lbl,
            }
        start_idx = (max(1, int(page)) - 1) * 20 + 1
        items, err = acct_fn(feed_kind=kind, start=start_idx, count=20, cookie_args=cookie_args)
        if err and kind == "supermix" and eff_source == "account":
            # Newer YT Music accounts may not have a Supermix yet -> try Liked Music
            items2, err2 = acct_fn(feed_kind="liked", start=start_idx, count=20, cookie_args=cookie_args)
            if not err2 and items2:
                items, err, kind = items2, "", "liked"
        if err:
            return [], err, {
                "source": eff_source,
                "source_label": YTM_ACCOUNT_FEED_TARGETS[kind][1],
                "cookie_label": cookie_lbl,
            }
        filtered = _filter_blocked_music(items, exclude_ids=exclude_ids)
        meta = {
            "source": eff_source,
            "source_label": YTM_ACCOUNT_FEED_TARGETS[kind][1],
            "cookie_label": cookie_lbl,
            "fallback_notice": "",
        }
        if page == 1 and filtered:
            with _YTM_FEED_CACHE_LOCK:
                _YTM_FEED_CACHE[cache_key] = (time.monotonic() + _YTM_FEED_CACHE_TTL, list(filtered), dict(meta))
        return filtered, "", meta

    # Auto mode: try YT Music Supermix (then Account Liked Music) when cookies exist, else fall back to Local Smart Mix
    fallback_notice = ""
    if eff_source == "auto" and cookie_args:
        start_idx = (max(1, int(page)) - 1) * 20 + 1
        items, err = acct_fn(feed_kind="supermix", start=start_idx, count=20, cookie_args=cookie_args)
        chosen_kind = "supermix"
        if (err or not items) and "expired" not in err.lower() and "signed in" not in err.lower():
            items2, err2 = acct_fn(feed_kind="liked", start=start_idx, count=20, cookie_args=cookie_args)
            if not err2 and items2:
                items, err, chosen_kind = items2, "", "liked"
        if not err and items:
            filtered = _filter_blocked_music(items, exclude_ids=exclude_ids)
            if filtered:
                meta = {
                    "source": f"account_{chosen_kind}",
                    "source_label": f"{YTM_ACCOUNT_FEED_TARGETS[chosen_kind][1]} ({cookie_lbl})",
                    "cookie_label": cookie_lbl,
                    "fallback_notice": "",
                }
                if page == 1:
                    with _YTM_FEED_CACHE_LOCK:
                        _YTM_FEED_CACHE[cache_key] = (time.monotonic() + _YTM_FEED_CACHE_TTL, list(filtered), dict(meta))
                return filtered, "", meta
        if err:
            fallback_notice = f"Account mix fell back to Local ({err})"

    items, err, meta = build_local_music_feed(
        page=page,
        exclude_ids=exclude_ids,
        recent=recent,
        most_played=most_played,
        liked=liked,
        playlists=playlists,
        fetch_mix_fn=fetch_mix_fn,
        search_fn=search_fn,
    )
    meta["cookie_label"] = cookie_lbl
    meta["fallback_notice"] = fallback_notice
    if page == 1 and not err and items:
        with _YTM_FEED_CACHE_LOCK:
            _YTM_FEED_CACHE[cache_key] = (time.monotonic() + _YTM_FEED_CACHE_TTL, list(items), dict(meta))
    return items, err, meta


def download_track_offline(
    track: dict,
    on_progress: Optional[Callable[[float], None]] = None,
    target_dir: Optional[str] = None,
    folder_name: str = "",
    cancel_event: Optional[threading.Event] = None,
    audio_format: str = "mp3",
) -> Tuple[bool, str]:
    """Download a YouTube Music track into the Offline Library (or playlist subfolder)."""
    existing = find_local_path_for_track(track)
    if existing and os.path.isfile(existing):
        if on_progress:
            on_progress(1.0)
        return True, "Already offline"
    if track.get("local"):
        return True, "Already offline"
    if not shutil.which("yt-dlp"):
        return False, "yt-dlp not installed"

    clean_folder = sanitize_filename(folder_name, max_len=56, fallback="") if folder_name else ""
    dest_dir = target_dir or (os.path.join(get_library_dir(), clean_folder) if clean_folder else get_library_dir())
    os.makedirs(dest_dir, exist_ok=True)

    raw_name = f"{track.get('artist', 'Artist')} - {track.get('title', track['id'])}"
    safe = sanitize_filename(raw_name, max_len=72, fallback=track["id"])
    fmt = audio_format if audio_format in ("mp3", "m4a", "opus") else "mp3"
    out_path = os.path.join(dest_dir, f"{safe}.{fmt}")
    if os.path.isfile(out_path):
        register_offline_file(track, out_path, folder=clean_folder)
        if on_progress:
            on_progress(1.0)
        return True, "Already offline"

    url = f"https://www.youtube.com/watch?v={track['id']}"
    c_args = _active_cookie_args()
    # Try with embedded cover art first (works on MP3/M4A with ffmpeg), fallback without if needed
    base_cmd = [
        "yt-dlp", "-x", "--audio-format", fmt, "--audio-quality", "0",
        "--embed-metadata", "--newline", "--no-playlist", *c_args,
        "-o", os.path.join(dest_dir, f"{safe}.%(ext)s"),
    ]
    attempts = [base_cmd + ["--embed-thumbnail", url], base_cmd + [url]] if fmt in ("mp3", "m4a") else [base_cmd + [url]]
    if c_args:
        no_c_base = [
            "yt-dlp", "-x", "--audio-format", fmt, "--audio-quality", "0",
            "--embed-metadata", "--newline", "--no-playlist",
            "-o", os.path.join(dest_dir, f"{safe}.%(ext)s"),
        ]
        attempts.append(no_c_base + [url])

    for cmd in attempts:
        if cancel_event is not None and cancel_event.is_set():
            return False, "Cancelled"
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        try:
            while proc.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    try:
                        proc.terminate()
                        proc.wait(timeout=1.0)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    return False, "Cancelled"
                if proc.stdout is None:
                    break
                line = proc.stdout.readline()
                if not line:
                    continue
                m = re.search(r"(\d+(?:\.\d+)?)%", line)
                if m and on_progress:
                    on_progress(min(1.0, float(m.group(1)) / 100.0))
            proc.wait(timeout=10)
        except Exception as exc:
            try:
                proc.kill()
            except Exception:
                pass
            return False, str(exc)[:44]

        if proc.returncode == 0:
            # Find actual created file (extension might match fmt or fallback)
            saved_path = out_path if os.path.isfile(out_path) else ""
            if not saved_path:
                for ext in _AUDIO_EXTS:
                    cand = os.path.join(dest_dir, f"{safe}{ext}")
                    if os.path.isfile(cand):
                        saved_path = cand
                        break
            if saved_path:
                register_offline_file(track, saved_path, folder=clean_folder)
                track["path"] = saved_path
            if on_progress:
                on_progress(1.0)
            return True, f"Saved: {safe[:26]}.{fmt}"
    return False, "Download failed"


def download_playlist_offline(
    tracks: List[dict],
    playlist_name: str,
    playlist_id: str = "",
    on_progress: Optional[Callable[[int, int, str, float, int, int], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    audio_format: str = "mp3",
) -> dict:
    """Download an entire playlist into loot/YTMusic/Library/<playlist_name>/ sequentially.

    Skips already-downloaded tracks, links downloaded paths back into playlists.json,
    writes an .m3u playlist file, and stops cleanly if cancel_event is set.
    """
    folder = sanitize_filename(playlist_name, max_len=56, fallback="Playlist")
    dest_dir = os.path.join(get_library_dir(), folder)
    os.makedirs(dest_dir, exist_ok=True)

    total = len(tracks)
    downloaded = 0
    skipped = 0
    failed = 0
    cancelled = False

    for i, track in enumerate(tracks):
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        title = str(track.get("title") or track.get("id") or f"Track {i + 1}")
        existing = find_local_path_for_track(track)
        if existing and os.path.isfile(existing):
            skipped += 1
            track["path"] = existing
            if on_progress:
                on_progress(i + 1, total, title, 1.0, skipped, failed)
            continue

        def _item_cb(frac: float, _idx=i, _ttl=title) -> None:
            if on_progress:
                on_progress(_idx, total, _ttl, frac, skipped, failed)

        if on_progress:
            on_progress(i, total, title, 0.02, skipped, failed)
        ok, msg = download_track_offline(
            track,
            on_progress=_item_cb,
            target_dir=dest_dir,
            folder_name=folder,
            cancel_event=cancel_event,
            audio_format=audio_format,
        )
        if msg == "Cancelled":
            cancelled = True
            break
        if ok:
            if msg == "Already offline":
                skipped += 1
            else:
                downloaded += 1
        else:
            failed += 1
        if on_progress:
            on_progress(i + 1, total, title, 1.0, skipped, failed)

    # Link offline_dir & local paths back onto playlist + export .m3u
    if playlist_id:
        pls = load_playlists()
        for pl in pls:
            if pl.get("id") == playlist_id:
                pl["offline_dir"] = folder
                idx_map = load_offline_index()
                for t in pl.get("tracks", []):
                    lp = find_local_path_for_track(t, _index=idx_map)
                    if lp:
                        t["path"] = lp
                save_playlists(pls)
                export_playlist_m3u(pl)
                break
    else:
        export_playlist_m3u({"name": playlist_name, "tracks": tracks})

    return {
        "total": total,
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "cancelled": cancelled,
        "folder": folder,
        "dest_dir": dest_dir,
    }


# ---------------------------------------------------------------------------
# Real-Time FFT Spectrum Helper (16 Logarithmic Bands)
# ---------------------------------------------------------------------------

def compute_fft_bands(pcm_s16le: bytes, n_bands: int = 16, channels: int = 1) -> List[float]:
    """Compute *n_bands* normalized (0.0..1.0) logarithmic frequency magnitudes from s16le PCM."""
    min_bytes = 256 * max(1, channels)
    if len(pcm_s16le) < min_bytes:
        return [0.05] * n_bands
    samples = np.frombuffer(pcm_s16le, dtype="<i2").astype(np.float32) / 32768.0
    if channels == 2 and len(samples) >= 2:
        even_n = (len(samples) // 2) * 2
        samples = 0.5 * (samples[:even_n:2] + samples[1:even_n:2])
    n = min(len(samples), 1024)
    win = samples[-n:] * np.hanning(n)
    mag = np.abs(np.fft.rfft(win))
    max_bin = len(mag) - 1
    if max_bin < n_bands + 2:
        return [0.05] * n_bands

    top_bin = min(max_bin, max(n_bands * 2, int(max_bin * 0.60)))
    raw_edges = np.geomspace(1, top_bin, n_bands + 1)
    bars: List[float] = []
    for i in range(n_bands):
        lo = max(1, int(round(raw_edges[i])))
        hi = max(lo + 1, int(round(raw_edges[i + 1])))
        hi = min(len(mag), hi)
        val = float(np.mean(mag[lo:hi]))
        norm = math.log1p(val * 3.8) / math.log1p(26.0)
        bars.append(max(0.05, min(1.0, norm)))
    return bars


def _scale_pcm_s16le(raw: bytes, gain: float) -> bytes:
    """Apply smooth software gain (0.0..1.0) to raw S16LE PCM (for Sleep Timer fade-out)."""
    if gain >= 0.999:
        return raw
    if gain <= 0.001:
        return b"\x00" * len(raw)
    arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) * float(gain)
    return np.clip(arr, -32768.0, 32767.0).astype("<i2").tobytes()


# ---------------------------------------------------------------------------
# Continuous Background YouTube Music Playback Engine
# ---------------------------------------------------------------------------

class YTMusicPlayer:
    """Continuous 48kHz audio engine with instant pause, clean sink reopen, EQ, speed, sleep timer & FFT."""

    def __init__(self):
        self._lock = threading.RLock()
        self.cfg = load_settings()
        self.queue: List[dict] = []
        self.current_idx: int = -1
        self.proc: Optional[subprocess.Popen] = None
        self.sink_proc: Optional[subprocess.Popen] = None
        self._unpaused = threading.Event()
        self._unpaused.set()
        self._sink_reopen_requested: bool = False
        self.state: str = "stopped"  # stopped | loading | playing | paused | error
        self.status_msg: str = "Ready"
        self.started_at: float = 0.0
        self.pause_at: float = 0.0
        self.offset_sec: float = 0.0
        self.muted: bool = False
        self.volume_pct: int = audio_helper.get_playback_volume_pct() if audio_helper else self.cfg["volume_pct"]

        # Sleep timer: 0 = off, >0 = monotonic deadline, -1 = pause at end of current track
        self.sleep_mode: int = 0
        self.sleep_deadline: float = 0.0

        self._spectrum: List[float] = [0.06] * 16
        self._peaks: List[float] = [0.08] * 16
        self._url_cache: Dict[str, Tuple[str, float]] = {}  # vid -> (url, fetched_at)
        self._play_token: int = 0
        self._closing: bool = False
        self._radio_fetching: bool = False

    @property
    def current_track(self) -> Optional[dict]:
        with self._lock:
            if 0 <= self.current_idx < len(self.queue):
                return self.queue[self.current_idx]
            return None

    @property
    def eq_name(self) -> str:
        return EQ_PRESETS[self.cfg["eq_idx"]][0]

    @property
    def eq_filter(self) -> str:
        return EQ_PRESETS[self.cfg["eq_idx"]][1]

    @property
    def speed(self) -> float:
        return float(self.cfg.get("speed", 1.0))

    @property
    def loudnorm(self) -> bool:
        return bool(self.cfg.get("loudnorm", False))

    @property
    def visualizer(self) -> str:
        return str(self.cfg.get("visualizer", "peaks"))

    def get_elapsed(self) -> float:
        with self._lock:
            spd = self.speed
            if self.state == "playing":
                return max(0.0, self.offset_sec + (time.monotonic() - self.started_at) * spd)
            if self.state == "paused":
                return max(0.0, self.offset_sec + (self.pause_at - self.started_at) * spd)
            return self.offset_sec

    def get_spectrum(self) -> List[float]:
        with self._lock:
            if self.state != "playing" or self.muted:
                self._spectrum = [max(0.05, b * 0.82) for b in self._spectrum]
                self._peaks = [max(0.06, p - 0.03) for p in self._peaks]
            else:
                self._peaks = [max(b, p - 0.018) for b, p in zip(self._spectrum, self._peaks)]
            return list(self._spectrum)

    def get_peaks(self) -> List[float]:
        with self._lock:
            return list(self._peaks)

    def set_sleep_timer(self, minutes: int) -> str:
        """Configure Sleep Timer (0=Off, -1=End of current track, >0=minutes)."""
        with self._lock:
            m = int(minutes)
            if m == 0:
                self.sleep_mode = 0
                self.sleep_deadline = 0.0
                return "Sleep Timer: OFF"
            if m < 0:
                self.sleep_mode = -1
                self.sleep_deadline = 0.0
                return "Sleep: End of Track"
            self.sleep_mode = m
            self.sleep_deadline = time.monotonic() + m * 60.0
            return f"Sleep Timer: {m}m"

    def get_sleep_label(self) -> str:
        with self._lock:
            if self.sleep_mode == 0:
                return ""
            if self.sleep_mode < 0:
                return "SLP:END"
            rem = max(0, int(math.ceil((self.sleep_deadline - time.monotonic()) / 60.0)))
            return f"SLP:{rem}m"

    def _compute_sleep_gain(self) -> Tuple[float, bool]:
        """Return (fade_gain 0..1, timer_expired_bool)."""
        with self._lock:
            if self.sleep_mode <= 0 or self.sleep_deadline <= 0.0:
                return 1.0, False
            rem = self.sleep_deadline - time.monotonic()
            if rem <= 0.0:
                self.sleep_mode = 0
                self.sleep_deadline = 0.0
                return 0.0, True
            if rem < 5.0:
                return max(0.0, rem / 5.0), False
            return 1.0, False

    def _kill_one(self, proc: Optional[subprocess.Popen]) -> None:
        if proc is None:
            return
        try:
            proc.send_signal(signal.SIGCONT)
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=0.45)
                except Exception:
                    proc.kill()
                    proc.wait(timeout=0.35)
        except Exception:
            pass

    def _stop_proc_unlocked(self) -> None:
        self._unpaused.set()
        proc = self.proc
        sink = self.sink_proc
        self.proc = None
        self.sink_proc = None
        self._kill_one(sink)
        self._kill_one(proc)

    def _spawn_sink_process(self) -> Optional[subprocess.Popen]:
        """Spawn a fresh low-latency 48kHz stereo PCM sink targeting the CURRENT default output."""
        if not audio_helper:
            return None
        sink_cmd = audio_helper.get_pcm_sink_cmd(rate=_PCM_RATE, channels=2)
        if not sink_cmd:
            return None
        env = audio_helper.get_pulse_env()
        sink_kw = (
            audio_helper.get_audio_popen_kwargs(sink_cmd)
            if hasattr(audio_helper, "get_audio_popen_kwargs")
            else {"env": env}
        )
        try:
            return subprocess.Popen(
                sink_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=4096,
                **sink_kw,
            )
        except (PermissionError, OSError):
            try:
                return subprocess.Popen(
                    sink_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    bufsize=4096,
                    env=env,
                )
            except Exception:
                return None
        except Exception:
            return None

    def reopen_audio_sink(self) -> None:
        """Cleanly reopen the PCM audio sink on the current output (fixes BT/HDMI switch glitches!)."""
        with self._lock:
            self._sink_reopen_requested = True
            old_sink = self.sink_proc
            self.sink_proc = None
        if old_sink is not None:
            self._kill_one(old_sink)

    def play_tracks(self, tracks: List[dict], start_idx: int = 0) -> None:
        """Replace queue with *tracks* and immediately begin playing *start_idx*."""
        if not tracks:
            return
        with self._lock:
            self.queue = [dict(t) for t in tracks]
            self.current_idx = max(0, min(start_idx, len(self.queue) - 1))
        self._start_current(offset=0.0)

    def play_index(self, idx: int) -> None:
        with self._lock:
            if not (0 <= idx < len(self.queue)):
                return
            self.current_idx = idx
        self._start_current(offset=0.0)

    def enqueue(self, track: dict, play_next: bool = False) -> None:
        with self._lock:
            if not self.queue:
                self.queue = [dict(track)]
                self.current_idx = 0
                should_start = True
            else:
                if play_next and self.current_idx >= 0:
                    self.queue.insert(self.current_idx + 1, dict(track))
                else:
                    self.queue.append(dict(track))
                should_start = (self.state == "stopped")
        if should_start:
            self._start_current(offset=0.0)

    def remove_from_queue(self, idx: int) -> None:
        restart = False
        with self._lock:
            if not (0 <= idx < len(self.queue)):
                return
            self.queue.pop(idx)
            if not self.queue:
                self._stop_proc_unlocked()
                self.current_idx = -1
                self.state = "stopped"
                return
            if idx < self.current_idx:
                self.current_idx -= 1
            elif idx == self.current_idx:
                self.current_idx = min(self.current_idx, len(self.queue) - 1)
                restart = True
        if restart:
            self._start_current(offset=0.0)

    def move_queue_item(self, idx: int, delta: int) -> int:
        with self._lock:
            new_i = idx + delta
            if not (0 <= idx < len(self.queue) and 0 <= new_i < len(self.queue)):
                return idx
            self.queue[idx], self.queue[new_i] = self.queue[new_i], self.queue[idx]
            if self.current_idx == idx:
                self.current_idx = new_i
            elif self.current_idx == new_i:
                self.current_idx = idx
            return new_i

    def clear_queue(self) -> None:
        with self._lock:
            self._play_token += 1
            self._stop_proc_unlocked()
            self.queue.clear()
            self.current_idx = -1
            self.state = "stopped"
            self.status_msg = "Queue cleared"

    def _get_cached_or_resolve(self, track: dict) -> Tuple[str, str]:
        # Smart Offline-First: always use local downloaded MP3 if available on disk!
        local_p = find_local_path_for_track(track)
        if local_p:
            return local_p, ""
        if track.get("local"):
            return "", "Local file missing"
        vid = track.get("id", "")
        with self._lock:
            cached = self._url_cache.get(vid)
            if cached and (time.monotonic() - cached[1]) < 1800:
                return cached[0], ""
        url, err = resolve_best_audio_url(vid)
        if url:
            with self._lock:
                self._url_cache[vid] = (url, time.monotonic())
        return url, err

    def _prefetch_next_track(self) -> None:
        with self._lock:
            if not self.queue or self.current_idx < 0:
                return
            nxt_i = (self.current_idx + 1) % len(self.queue)
            if nxt_i == self.current_idx:
                return
            nxt = self.queue[nxt_i]
            if nxt.get("local") or nxt.get("id") in self._url_cache or find_local_path_for_track(nxt):
                return
        threading.Thread(target=self._get_cached_or_resolve, args=(nxt,), daemon=True).start()

    def _start_current(self, offset: float = 0.0) -> None:
        with self._lock:
            self._play_token += 1
            tok = self._play_token
            self._stop_proc_unlocked()
            track = self.current_track
            if track is None:
                self.state = "stopped"
                return
            self.state = "loading"
            self.offset_sec = max(0.0, offset)
            self._sink_reopen_requested = False
            self.status_msg = f"Loading {track.get('title', '')[:26]}..."

        threading.Thread(target=self._playback_worker, args=(tok, dict(track), max(0.0, offset)), daemon=True).start()

    def _playback_worker(self, token: int, track: dict, offset: float) -> None:
        src_url, err = self._get_cached_or_resolve(track)
        with self._lock:
            if self._closing or token != self._play_token:
                return
            if not src_url:
                self.state = "error"
                self.status_msg = err or "Stream error"
                return

        if not shutil.which("ffmpeg"):
            with self._lock:
                self.state = "error"
                self.status_msg = "ffmpeg not installed"
            return

        record_recent(track)
        env = audio_helper.get_pulse_env() if audio_helper else os.environ.copy()
        af_chain = build_audio_filter_chain(self.eq_filter, speed=self.speed, loudnorm=self.loudnorm)

        # Decode + DSP to clean 48kHz stereo S16LE PCM piped to fresh low-latency sink
        # (80ms paplay/pw-play/aplay -> instant pause & zero resampling glitches on BT/HDMI!)
        sink_proc = self._spawn_sink_process()
        use_sink_pipe = sink_proc is not None
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if src_url.startswith("http"):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        if offset > 0:
            cmd += ["-ss", f"{offset:.1f}"]

        if use_sink_pipe:
            cmd += ["-i", src_url, "-vn", "-af", af_chain, "-ac", "2", "-ar", str(_PCM_RATE), "-f", "s16le", "pipe:1"]
            ff_kw = {"env": env}
        else:
            audio_args = audio_helper.get_ffmpeg_audio_args() if audio_helper else ["-f", "alsa", "default"]
            cmd += ["-i", src_url, "-vn", "-af", af_chain, "-ac", "2", "-ar", str(_PCM_RATE)] + audio_args
            ff_kw = (
                audio_helper.get_audio_popen_kwargs(cmd, allow_user_drop=not os.path.isfile(src_url))
                if (audio_helper and hasattr(audio_helper, "get_audio_popen_kwargs"))
                else {"env": env}
            )

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE if use_sink_pipe else subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=4096,
                **ff_kw,
            )
        except Exception as exc:
            self._kill_one(sink_proc)
            with self._lock:
                if token == self._play_token:
                    self.state = "error"
                    self.status_msg = str(exc)[:44]
            return

        with self._lock:
            if self._closing or token != self._play_token:
                self._kill_one(proc)
                self._kill_one(sink_proc)
                return
            self.proc = proc
            self.sink_proc = sink_proc
            self._unpaused.set()
            self.state = "playing"
            self.offset_sec = offset
            self.started_at = time.monotonic()
            self.status_msg = "Playing"

        self._prefetch_next_track()

        # Pump 4096-byte stereo PCM chunks (~21ms @ 48kHz) to audio sink; throttle FFT to ~12 Hz
        chunk_bytes = 4096
        bytes_pumped = 0
        last_fft_at = 0.0
        stats_recorded = False
        play_wall_start = time.monotonic()
        try:
            stdout = proc.stdout
            while stdout is not None and not self._closing:
                # 1. Instant Pause: wait without touching PipeWire, then reopen fresh sink on Resume!
                if not self._unpaused.is_set():
                    self._unpaused.wait(timeout=0.05)
                    with self._lock:
                        if token != self._play_token:
                            break
                    continue

                with self._lock:
                    if token != self._play_token:
                        break
                    need_open = (self.sink_proc is None) or self._sink_reopen_requested
                    if need_open:
                        old_s = self.sink_proc
                        self.sink_proc = None
                        self._sink_reopen_requested = False
                    else:
                        old_s = None
                if old_s is not None:
                    self._kill_one(old_s)
                if need_open:
                    fresh = self._spawn_sink_process()
                    with self._lock:
                        if self._closing or token != self._play_token or not self._unpaused.is_set():
                            self._kill_one(fresh)
                            continue
                        self.sink_proc = fresh

                # 2. Check Sleep Timer (smooth 5s fade-out -> pause cleanly)
                sleep_gain, sleep_expired = self._compute_sleep_gain()
                if sleep_expired:
                    self.toggle_pause()
                    with self._lock:
                        self.status_msg = "Sleep timer paused"
                    continue

                raw = stdout.read(chunk_bytes)
                if not raw:
                    break
                bytes_pumped += len(raw)

                if not stats_recorded and (time.monotonic() - play_wall_start) >= 18.0:
                    stats_recorded = True
                    threading.Thread(target=record_track_play, args=(track, 20.0), daemon=True).start()

                with self._lock:
                    cur_sink = self.sink_proc
                sink_in = cur_sink.stdin if cur_sink is not None else None
                if sink_in is not None:
                    try:
                        if self.muted:
                            out_chunk = b"\x00" * len(raw)
                        elif sleep_gain < 0.999:
                            out_chunk = _scale_pcm_s16le(raw, sleep_gain)
                        else:
                            out_chunk = raw
                        sink_in.write(out_chunk)
                        sink_in.flush()
                    except (BrokenPipeError, OSError):
                        # If sink was intentionally killed by pause or output-switch, loop cleanly!
                        with self._lock:
                            if token != self._play_token:
                                break
                            if not self._unpaused.is_set() or self._sink_reopen_requested or self.sink_proc is None:
                                continue
                        break

                now_fft = time.monotonic()
                if not self.muted and (now_fft - last_fft_at) >= 0.08:
                    last_fft_at = now_fft
                    bands = compute_fft_bands(raw, n_bands=16, channels=2)
                    with self._lock:
                        if token != self._play_token:
                            break
                        self._spectrum = [
                            max(b, prev * 0.65) for prev, b in zip(self._spectrum, bands)
                        ]
                elif token != self._play_token:
                    break
        except Exception:
            pass
        finally:
            with self._lock:
                fin_sink = self.sink_proc
            if fin_sink is not None and fin_sink.stdin is not None:
                try:
                    fin_sink.stdin.close()
                except Exception:
                    pass

        rc = proc.wait()
        sink_rc = 0
        sink_err = ""
        with self._lock:
            cur_s = self.sink_proc
        if cur_s is not None:
            try:
                sink_rc = cur_s.wait(timeout=1.5)
            except Exception:
                self._kill_one(cur_s)
            try:
                sink_err = (cur_s.stderr.read(160).decode("utf-8", "ignore") if cur_s.stderr else "").strip()
            except Exception:
                pass
        ff_err = ""
        try:
            ff_err = (proc.stderr.read(160).decode("utf-8", "ignore") if proc.stderr else "").strip()
        except Exception:
            pass

        with self._lock:
            if self._closing or token != self._play_token:
                return
            self.proc = None
            self.sink_proc = None
            if (rc != 0 or sink_rc != 0) and bytes_pumped < 32768:
                self.state = "error"
                msg = (sink_err or ff_err or "Audio playback error").splitlines()[-1]
                self.status_msg = msg[:46]
                return
            # End-of-track sleep timer stops cleanly after this song finishes!
            if self.sleep_mode < 0:
                self.sleep_mode = 0
                self.state = "stopped"
                self.status_msg = "Sleep timer finished"
                return
        # Natural track end -> auto-advance!
        self._auto_advance()

    def _auto_advance(self) -> None:
        with self._lock:
            if not self.queue or self._closing:
                self.state = "stopped"
                return
            rep = self.cfg["repeat_mode"]
            if rep == "one":
                pass
            elif self.cfg["shuffle"] and len(self.queue) > 1:
                choices = [i for i in range(len(self.queue)) if i != self.current_idx]
                self.current_idx = random.choice(choices)
            elif self.current_idx + 1 < len(self.queue):
                self.current_idx += 1
            elif self.cfg["auto_radio"] and self.current_track and not self.current_track.get("local"):
                seed_id = self.current_track["id"]
                self._append_radio_and_play_next(seed_id)
                return
            elif rep == "all":
                self.current_idx = 0
            else:
                self.state = "stopped"
                self.status_msg = "Queue finished"
                return
        self._start_current(offset=0.0)

    def _append_radio_and_play_next(self, seed_id: str) -> None:
        if self._radio_fetching:
            return
        self._radio_fetching = True
        self.status_msg = "Loading Auto-Radio..."

        def _worker():
            try:
                more, _err = fetch_radio_mix(seed_id, limit=15)
                with self._lock:
                    existing = {t.get("id") for t in self.queue}
                    added = [t for t in more if t.get("id") not in existing]
                    if added:
                        self.queue.extend(added)
                        self.current_idx = min(self.current_idx + 1, len(self.queue) - 1)
                        play_now = True
                    elif self.cfg["repeat_mode"] == "all" and self.queue:
                        self.current_idx = 0
                        play_now = True
                    else:
                        self.state = "stopped"
                        play_now = False
                if play_now:
                    self._start_current(offset=0.0)
            finally:
                self._radio_fetching = False

        threading.Thread(target=_worker, daemon=True).start()

    def next_track(self) -> None:
        with self._lock:
            if not self.queue:
                return
            if self.cfg["shuffle"] and len(self.queue) > 1:
                choices = [i for i in range(len(self.queue)) if i != self.current_idx]
                self.current_idx = random.choice(choices)
            elif self.current_idx + 1 < len(self.queue):
                self.current_idx += 1
            elif self.cfg["auto_radio"] and self.current_track and not self.current_track.get("local"):
                seed_id = self.current_track["id"]
                self._append_radio_and_play_next(seed_id)
                return
            else:
                self.current_idx = 0
        self._start_current(offset=0.0)

    def prev_track(self) -> None:
        with self._lock:
            if not self.queue:
                return
            if self.get_elapsed() > 4.0:
                pass
            else:
                self.current_idx = (self.current_idx - 1) % len(self.queue)
        self._start_current(offset=0.0)

    def toggle_pause(self) -> None:
        """Instant <20ms pause/resume by closing the PCM sink on pause and reopening cleanly on resume."""
        sink_to_close = None
        with self._lock:
            if self.proc is None:
                if self.current_track is not None:
                    self._start_current(offset=self.offset_sec)
                return
            now = time.monotonic()
            if self.state == "playing":
                self.pause_at = now
                self.state = "paused"
                self.status_msg = "Paused"
                self._unpaused.clear()
                if self.sink_proc is not None:
                    sink_to_close = self.sink_proc
                    self.sink_proc = None
                else:
                    try:
                        self.proc.send_signal(signal.SIGSTOP)
                    except Exception:
                        pass
            elif self.state == "paused":
                self.started_at += (now - self.pause_at)
                self.state = "playing"
                self.status_msg = "Playing"
                self._unpaused.set()
                if self.sink_proc is None and getattr(self.proc, "stdout", None) is None:
                    try:
                        self.proc.send_signal(signal.SIGCONT)
                    except Exception:
                        pass
        # Terminate PCM sink outside lock -> cuts PulseAudio/PipeWire buffer immediately (<20ms)!
        if sink_to_close is not None:
            self._kill_one(sink_to_close)

    def seek_relative(self, delta_sec: float) -> None:
        track = self.current_track
        if track is None:
            return
        dur = int(track.get("duration") or 0)
        target = max(0.0, self.get_elapsed() + delta_sec)
        if dur > 0:
            target = min(max(0.0, dur - 2.0), target)
        self._start_current(offset=target)

    def set_volume(self, pct: int) -> int:
        self.muted = False
        clamped = max(0, min(100, int(pct)))
        if audio_helper:
            clamped = audio_helper.set_playback_volume_pct(clamped)
        self.volume_pct = clamped
        self.cfg["volume_pct"] = clamped
        save_settings(self.cfg)
        return clamped

    def toggle_mute(self) -> bool:
        self.muted = not self.muted
        if audio_helper:
            audio_helper.set_playback_mute(self.muted)
        return self.muted

    def cycle_eq(self) -> str:
        with self._lock:
            self.cfg["eq_idx"] = (self.cfg["eq_idx"] + 1) % len(EQ_PRESETS)
            save_settings(self.cfg)
            was_playing = self.state in ("playing", "paused")
            pos = self.get_elapsed()
            name = self.eq_name
        if was_playing:
            self._start_current(offset=pos)
        return name

    def cycle_speed(self, delta: int = 1) -> float:
        with self._lock:
            cur = self.speed
            idx = SPEED_OPTIONS.index(cur) if cur in SPEED_OPTIONS else 1
            nxt = SPEED_OPTIONS[(idx + delta) % len(SPEED_OPTIONS)]
            self.cfg["speed"] = nxt
            save_settings(self.cfg)
            was_playing = self.state in ("playing", "paused")
            pos = self.get_elapsed()
        if was_playing:
            self._start_current(offset=pos)
        return nxt

    def toggle_loudnorm(self) -> bool:
        with self._lock:
            self.cfg["loudnorm"] = not self.loudnorm
            save_settings(self.cfg)
            was_playing = self.state in ("playing", "paused")
            pos = self.get_elapsed()
            on = self.loudnorm
        if was_playing:
            self._start_current(offset=pos)
        return on

    def cycle_visualizer(self) -> str:
        with self._lock:
            cur = self.visualizer
            idx = VISUALIZER_MODES.index(cur) if cur in VISUALIZER_MODES else 0
            nxt = VISUALIZER_MODES[(idx + 1) % len(VISUALIZER_MODES)]
            self.cfg["visualizer"] = nxt
            save_settings(self.cfg)
            return nxt

    def toggle_shuffle(self) -> bool:
        self.cfg["shuffle"] = not self.cfg["shuffle"]
        save_settings(self.cfg)
        return self.cfg["shuffle"]

    def cycle_repeat(self) -> str:
        order = ("off", "all", "one")
        cur = self.cfg["repeat_mode"]
        nxt = order[(order.index(cur) + 1) % len(order)]
        self.cfg["repeat_mode"] = nxt
        save_settings(self.cfg)
        return nxt

    def toggle_auto_radio(self) -> bool:
        self.cfg["auto_radio"] = not self.cfg["auto_radio"]
        save_settings(self.cfg)
        return self.cfg["auto_radio"]

    def close(self) -> None:
        with self._lock:
            self._closing = True
            self._play_token += 1
            self._stop_proc_unlocked()
            self.state = "stopped"
