#!/usr/bin/env python3
"""
EmberJack YouTube Personalized Feed & Account Engine (rj_youtube_feed.py)
------------------------------------------------------------------------
Hybrid 2-tier personalized recommendation engine for the YouTube payload:

  Tier 1 — Zero-Login Local Smart Feed (works out of the box):
    - Learns locally from watch history (`loot/YouTube/taste.json`), liked
      videos, channel subscriptions, saved playlists / Watch Later, and
      search history (with curated cold-start fallback).
    - Fetches related videos directly via YouTube InnerTube `/youtubei/v1/next`
      (supporting both modern `lockupViewModel` and legacy `compactVideoRenderer`,
      with automatic `yt-dlp` `list=RD<id>` fallback).
    - Scores, deduplicates, interleaves channels (max 2 consecutive per
      creator), respects dismissed videos & muted channels, and supports
      infinite `+ Load More Videos` pagination.

  Tier 2 — Real YouTube Account Feed (optional):
    - Uses `yt-dlp` native `:ytrecommended`, `:ytsubs`, `:ytwatchlater`,
      and `:ythistory` authenticated via `loot/YouTube/cookies.txt` (uploaded
      via WebUI or SCP) or `--cookies-from-browser <firefox|chromium|chrome|brave>`.
    - Automatically falls back to Tier 1 if cookies are absent or expired.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import rj_env

# ---------------------------------------------------------------------------
# Constants & Configuration
# ---------------------------------------------------------------------------

INNERTUBE_NEXT_URL = "https://www.youtube.com/youtubei/v1/next?prettyPrint=false"
INNERTUBE_CLIENT_CONTEXT = {
    "client": {
        "clientName": "WEB",
        "clientVersion": "2.20260401.01.00",
        "hl": "en",
        "gl": "US",
    }
}
INNERTUBE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.youtube.com",
    "Referer": "https://www.youtube.com/",
}

COOKIE_SOURCE_OPTIONS: Tuple[Tuple[str, str], ...] = (
    ("Auto (cookies.txt -> Browser)", "auto"),
    ("Uploaded cookies.txt File", "file"),
    ("Firefox Browser", "firefox"),
    ("Chromium Browser", "chromium"),
    ("Google Chrome Browser", "chrome"),
    ("Brave Browser", "brave"),
    ("Off (Local Feed Only)", "off"),
)

FEED_MODE_OPTIONS: Tuple[Tuple[str, str], ...] = (
    ("Auto (Account if linked, else Local)", "auto"),
    ("Local Smart Feed Only", "local"),
    ("YouTube Account Home Only", "account"),
)

ACCOUNT_FEED_TARGETS: Dict[str, Tuple[str, str]] = {
    "recommended": (":ytrecommended", "YouTube Account Home"),
    "subscriptions": (":ytsubs", "Account Subscriptions"),
    "watchlater": (":ytwatchlater", "Account Watch Later"),
    "history": (":ythistory", "Account History"),
}

_YOUTUBE_AUTH_COOKIE_NAMES: Set[str] = {
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "LOGIN_INFO",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "__Secure-1PSIDTS",
    "__Secure-3PSIDTS",
}

_STOPWORDS: Set[str] = {
    "the", "and", "for", "with", "that", "this", "from", "your", "you", "are",
    "was", "were", "have", "has", "had", "not", "but", "what", "when", "where",
    "why", "how", "who", "will", "can", "all", "out", "about", "into", "over",
    "more", "most", "very", "just", "like", "best", "top", "new", "video",
    "official", "full", "live", "part", "episode", "watch", "youtube", "shorts",
    "clip", "clips", "music", "audio", "hd", "4k", "1080p", "2024", "2025", "2026",
}

_COLD_START_QUERIES: Tuple[str, ...] = (
    "raspberry pi projects",
    "linux terminal tips",
    "cybersecurity news",
    "synthwave coding mix",
    "electronics diy tutorial",
)

_MAX_WATCH_HISTORY = 100
_MAX_DISMISSED_IDS = 500
_MAX_MUTED_CHANNELS = 200
_MAX_COOKIES_BYTES = 2 * 1024 * 1024  # 2 MB cap
_FEED_CACHE_TTL = 900.0  # 15 minutes

_DATA_DIR_OVERRIDE: Optional[str] = None
_CACHE_LOCK = threading.Lock()
_FEED_CACHE: Dict[str, Tuple[float, List[dict], dict]] = {}


# ---------------------------------------------------------------------------
# Storage & JSON Helpers
# ---------------------------------------------------------------------------

def get_youtube_data_dir() -> str:
    if _DATA_DIR_OVERRIDE:
        d = _DATA_DIR_OVERRIDE
    else:
        d = os.path.join(rj_env.get_base_dir(), "loot", "YouTube")
    os.makedirs(d, exist_ok=True)
    return d


def get_taste_file_path() -> str:
    return os.path.join(get_youtube_data_dir(), "taste.json")


def get_cookies_file_path() -> str:
    return os.path.join(get_youtube_data_dir(), "cookies.txt")


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def _write_json(path: str, data: Any) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def invalidate_feed_cache() -> None:
    with _CACHE_LOCK:
        _FEED_CACHE.clear()


# ---------------------------------------------------------------------------
# Watch History & Taste Profile (`loot/YouTube/taste.json`)
# ---------------------------------------------------------------------------

def _empty_taste_profile() -> dict:
    return {
        "version": 1,
        "watched": [],
        "channels": {},
        "keywords": {},
        "dismissed_ids": [],
        "muted_channels": [],
    }


def _normalize_taste_profile(raw: Any) -> dict:
    base = _empty_taste_profile()
    if not isinstance(raw, dict):
        return base
    watched = raw.get("watched")
    if isinstance(watched, list):
        base["watched"] = [x for x in watched if isinstance(x, dict) and x.get("id")][:_MAX_WATCH_HISTORY]
    channels = raw.get("channels")
    if isinstance(channels, dict):
        base["channels"] = {
            str(k)[:48]: max(1, int(v))
            for k, v in channels.items()
            if str(k).strip() and isinstance(v, (int, float))
        }
    keywords = raw.get("keywords")
    if isinstance(keywords, dict):
        base["keywords"] = {
            str(k).lower()[:32]: max(1, int(v))
            for k, v in keywords.items()
            if str(k).strip() and isinstance(v, (int, float))
        }
    dismissed = raw.get("dismissed_ids")
    if isinstance(dismissed, list):
        seen: Set[str] = set()
        out_d: List[str] = []
        for vid in dismissed:
            s = str(vid or "").strip()
            if s and s not in seen:
                seen.add(s)
                out_d.append(s)
        base["dismissed_ids"] = out_d[:_MAX_DISMISSED_IDS]
    muted = raw.get("muted_channels")
    if isinstance(muted, list):
        seen_m: Set[str] = set()
        out_m: List[str] = []
        for ch in muted:
            s = str(ch or "").strip()
            if s and s.lower() not in seen_m:
                seen_m.add(s.lower())
                out_m.append(s[:48])
        base["muted_channels"] = out_m[:_MAX_MUTED_CHANNELS]
    return base


def load_taste_profile() -> dict:
    return _normalize_taste_profile(_read_json(get_taste_file_path(), {}))


def save_taste_profile(profile: dict) -> None:
    _write_json(get_taste_file_path(), _normalize_taste_profile(profile))


def extract_keywords(text: str, max_terms: int = 8) -> List[str]:
    tokens = re.findall(r"[A-Za-z0-9]{3,}", (text or "").lower())
    out: List[str] = []
    seen: Set[str] = set()
    for tok in tokens:
        if tok in _STOPWORDS or tok.isdigit() or tok in seen:
            continue
        seen.add(tok)
        out.append(tok[:24])
        if len(out) >= max_terms:
            break
    return out


def _format_relative_time(ts: int, now_ts: Optional[int] = None) -> str:
    if not ts or ts <= 0:
        return "Watched recently"
    now = int(now_ts if now_ts is not None else time.time())
    diff = max(0, now - int(ts))
    if diff < 120:
        return "Watched just now"
    if diff < 3600:
        return f"Watched {diff // 60}m ago"
    if diff < 86400:
        return f"Watched {diff // 3600}h ago"
    days = diff // 86400
    if days == 1:
        return "Watched yesterday"
    if days < 30:
        return f"Watched {days}d ago"
    return "Watched earlier"


def record_watch_event(video: dict, watched_sec: float = 0.0, completed: bool = False) -> dict:
    """Record a watched YouTube video in local watch history & update taste weights."""
    if not isinstance(video, dict) or video.get("local"):
        return load_taste_profile()
    vid = str(video.get("id") or "").strip()
    if not vid:
        return load_taste_profile()

    prof = load_taste_profile()
    now_ts = int(time.time())
    title = str(video.get("title") or "Untitled").strip()[:100]
    channel = str(video.get("channel") or "").strip()[:48]
    duration = int(video.get("duration") or 0)
    views = int(video.get("views") or 0)
    thumb = str(video.get("thumb") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")
    ch_url = str(video.get("channel_url") or "")
    ch_id = str(video.get("channel_id") or "")

    prev_count = 0
    prev_sec = 0
    new_watched: List[dict] = []
    for entry in prof["watched"]:
        if entry.get("id") == vid:
            prev_count = int(entry.get("watch_count") or 1)
            prev_sec = int(entry.get("watched_sec") or 0)
        else:
            new_watched.append(entry)

    rec = {
        "id": vid,
        "title": title,
        "channel": channel,
        "channel_id": ch_id,
        "channel_url": ch_url,
        "duration": duration,
        "views": views,
        "thumb": thumb,
        "watched_at": now_ts,
        "watched_sec": max(prev_sec, int(max(0.0, float(watched_sec or 0.0)))),
        "watch_count": prev_count + 1,
    }
    new_watched.insert(0, rec)
    prof["watched"] = new_watched[:_MAX_WATCH_HISTORY]

    # Weight bump for channel & keywords (stronger if completed or watched >= 30s)
    weight = 2 if (completed or float(watched_sec or 0.0) >= 30.0) else 1
    if channel:
        prof["channels"][channel] = int(prof["channels"].get(channel, 0)) + weight
        if len(prof["channels"]) > 120:
            top_ch = sorted(prof["channels"].items(), key=lambda kv: kv[1], reverse=True)[:120]
            prof["channels"] = dict(top_ch)

    for kw in extract_keywords(title):
        prof["keywords"][kw] = int(prof["keywords"].get(kw, 0)) + weight
    if len(prof["keywords"]) > 120:
        top_kw = sorted(prof["keywords"].items(), key=lambda kv: kv[1], reverse=True)[:120]
        prof["keywords"] = dict(top_kw)

    save_taste_profile(prof)
    invalidate_feed_cache()
    return prof


def get_watch_history(limit: int = _MAX_WATCH_HISTORY) -> List[dict]:
    """Return local watch history items ready for the YouTube browser list."""
    prof = load_taste_profile()
    now_ts = int(time.time())
    out: List[dict] = []
    for entry in prof["watched"][:max(0, int(limit))]:
        item = dict(entry)
        item["reason"] = _format_relative_time(int(item.get("watched_at") or 0), now_ts=now_ts)
        if not item.get("thumb") and item.get("id"):
            item["thumb"] = f"https://i.ytimg.com/vi/{item['id']}/hqdefault.jpg"
        out.append(item)
    return out


def remove_watch_history_item(video_id: str) -> bool:
    """Remove a single video from local Watch History."""
    vid = str(video_id or "").strip()
    if not vid:
        return False
    prof = load_taste_profile()
    before = len(prof["watched"])
    prof["watched"] = [e for e in prof["watched"] if str(e.get("id") or "") != vid]
    if len(prof["watched"]) == before:
        return False
    save_taste_profile(prof)
    invalidate_feed_cache()
    return True


def clear_watch_history(clear_resume: bool = True) -> int:
    """Clear all local Watch History entries (and optionally auto-resume markers).

    Preserves dismissed videos and muted channels so explicit feed blocks stay intact.
    Returns the number of watch history entries cleared.
    """
    prof = load_taste_profile()
    count = len(prof["watched"])
    prof["watched"] = []
    prof["channels"] = {}
    prof["keywords"] = {}
    save_taste_profile(prof)
    if clear_resume:
        resume_path = os.path.join(get_youtube_data_dir(), "resume.json")
        if os.path.isfile(resume_path):
            _write_json(resume_path, {})
    invalidate_feed_cache()
    return count


def dismiss_video(video_id: str) -> bool:
    """Mark a video as 'Not Interested' so it stays hidden from For You Feed."""
    vid = str(video_id or "").strip()
    if not vid:
        return False
    prof = load_taste_profile()
    if vid not in prof["dismissed_ids"]:
        prof["dismissed_ids"].insert(0, vid)
        prof["dismissed_ids"] = prof["dismissed_ids"][:_MAX_DISMISSED_IDS]
        save_taste_profile(prof)
        invalidate_feed_cache()
        return True
    return False


def mute_channel(channel_name: str) -> bool:
    """Hide a channel from For You Feed recommendations."""
    ch = str(channel_name or "").strip()[:48]
    if not ch:
        return False
    prof = load_taste_profile()
    if not any(x.lower() == ch.lower() for x in prof["muted_channels"]):
        prof["muted_channels"].insert(0, ch)
        prof["muted_channels"] = prof["muted_channels"][:_MAX_MUTED_CHANNELS]
        save_taste_profile(prof)
        invalidate_feed_cache()
        return True
    return False


def unmute_channel(channel_name: str) -> bool:
    ch = str(channel_name or "").strip().lower()
    if not ch:
        return False
    prof = load_taste_profile()
    before = len(prof["muted_channels"])
    prof["muted_channels"] = [x for x in prof["muted_channels"] if x.lower() != ch]
    if len(prof["muted_channels"]) == before:
        return False
    save_taste_profile(prof)
    invalidate_feed_cache()
    return True


def get_muted_channels() -> List[str]:
    return list(load_taste_profile().get("muted_channels", []))


def reset_taste_profile(clear_resume: bool = True) -> None:
    """Wipe Watch History, learned taste weights, dismissed videos, and muted channels."""
    save_taste_profile(_empty_taste_profile())
    if clear_resume:
        resume_path = os.path.join(get_youtube_data_dir(), "resume.json")
        if os.path.isfile(resume_path):
            _write_json(resume_path, {})
    invalidate_feed_cache()


# ---------------------------------------------------------------------------
# Cookies & Browser Auth Detection
# ---------------------------------------------------------------------------

def validate_cookies_text(text: str) -> Tuple[bool, str, dict]:
    """Validate Netscape-format cookies.txt text for YouTube authentication."""
    raw = text if isinstance(text, str) else ""
    if not raw.strip():
        return False, "Cookie file is empty", {
            "total_cookies": 0,
            "youtube_cookies": 0,
            "has_auth": False,
        }
    if len(raw.encode("utf-8", errors="ignore")) > _MAX_COOKIES_BYTES:
        return False, "Cookie file too large (max 2 MB)", {
            "total_cookies": 0,
            "youtube_cookies": 0,
            "has_auth": False,
        }

    total = 0
    yt_count = 0
    auth_matched: Set[str] = set()
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#HttpOnly_"):
            s = s[len("#HttpOnly_"):]
        elif s.startswith("#"):
            continue
        parts = s.split("\t")
        if len(parts) < 7:
            continue
        domain = parts[0].strip().lower()
        name = parts[5].strip()
        value = parts[6].strip()
        if not domain or not name:
            continue
        total += 1
        if "youtube.com" in domain or domain.endswith(".google.com") or domain == "google.com":
            yt_count += 1
            if value and name in _YOUTUBE_AUTH_COOKIE_NAMES:
                auth_matched.add(name)

    info = {
        "total_cookies": total,
        "youtube_cookies": yt_count,
        "has_auth": bool(auth_matched),
        "auth_cookies": sorted(auth_matched),
    }
    if total == 0:
        return False, "Not a valid Netscape cookies.txt (expected tab-separated rows)", info
    if yt_count == 0:
        return False, "No YouTube or Google cookies found in file", info
    if not auth_matched:
        return True, f"Valid ({yt_count} YouTube cookies, sign-in token not detected — feed may be generic)", info
    return True, f"Connected ({yt_count} YouTube cookies, account session detected)", info


def validate_cookies_file(path: Optional[str] = None) -> Tuple[bool, str, dict]:
    target = path or get_cookies_file_path()
    if not os.path.isfile(target):
        return False, "No cookies.txt installed", {
            "total_cookies": 0,
            "youtube_cookies": 0,
            "has_auth": False,
        }
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            txt = handle.read(_MAX_COOKIES_BYTES + 1024)
    except OSError as exc:
        return False, f"Cannot read cookies.txt: {exc}", {
            "total_cookies": 0,
            "youtube_cookies": 0,
            "has_auth": False,
        }
    return validate_cookies_text(txt)


def save_cookies_text(text: str) -> Tuple[bool, str, dict]:
    """Validate and save Netscape cookies.txt to `loot/YouTube/cookies.txt` (0600)."""
    ok, msg, info = validate_cookies_text(text)
    if not ok:
        return False, msg, info
    dest = get_cookies_file_path()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # Normalize CRLF -> LF and ensure Netscape header so yt-dlp always accepts it
    clean = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if not clean.startswith("# Netscape HTTP Cookie File") and not clean.startswith("# HTTP Cookie File"):
        clean = "# Netscape HTTP Cookie File\n" + clean
    if not clean.endswith("\n"):
        clean += "\n"
    tmp = f"{dest}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(clean)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False, f"Failed to write cookies.txt: {exc}", info
    invalidate_feed_cache()
    return True, msg, info


def delete_cookies_file() -> Tuple[bool, str]:
    dest = get_cookies_file_path()
    if not os.path.exists(dest):
        invalidate_feed_cache()
        return True, "No cookies.txt to remove"
    try:
        os.remove(dest)
        invalidate_feed_cache()
        return True, "Removed YouTube cookies.txt"
    except OSError as exc:
        return False, f"Failed to remove cookies.txt: {exc}"


def _candidate_home_dirs() -> List[str]:
    homes: List[str] = []
    seen: Set[str] = set()

    def _add(p: str) -> None:
        if p and os.path.isdir(p) and p not in seen:
            seen.add(p)
            homes.append(p)

    for env_k in ("SUDO_USER", "LOGNAME", "USER"):
        u = (os.environ.get(env_k) or "").strip()
        if u and u != "root":
            _add(os.path.join("/home", u))
    _add(os.path.expanduser("~"))
    try:
        if os.path.isdir("/home"):
            for entry in sorted(os.listdir("/home")):
                _add(os.path.join("/home", entry))
    except OSError:
        pass
    return homes


def detect_available_browsers() -> List[dict]:
    """Detect local browsers that have cookie profiles usable by yt-dlp."""
    checks: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
        ("firefox", "Firefox", (".mozilla/firefox", "snap/firefox/common/.mozilla/firefox")),
        ("chromium", "Chromium", (".config/chromium",)),
        ("chrome", "Google Chrome", (".config/google-chrome",)),
        ("brave", "Brave", (".config/BraveSoftware/Brave-Browser",)),
    )
    found: List[dict] = []
    seen_browsers: Set[str] = set()
    for home in _candidate_home_dirs():
        for b_key, b_label, rel_paths in checks:
            if b_key in seen_browsers:
                continue
            for rel in rel_paths:
                full = os.path.join(home, rel)
                if not os.path.isdir(full):
                    continue
                if b_key == "firefox":
                    has_prof = os.path.isfile(os.path.join(full, "profiles.ini"))
                    if not has_prof:
                        try:
                            has_prof = any(
                                os.path.isfile(os.path.join(full, d, "cookies.sqlite"))
                                for d in os.listdir(full)
                            )
                        except OSError:
                            has_prof = False
                    if not has_prof:
                        continue
                seen_browsers.add(b_key)
                found.append({"browser": b_key, "label": b_label, "profile_dir": full})
                break
    return found


def _format_browser_cookie_spec(browser_key: str, profile_dir: str = "") -> str:
    """Return yt-dlp `--cookies-from-browser` spec, passing explicit profile path when running as root."""
    b_key = str(browser_key or "").strip().lower()
    p_dir = str(profile_dir or "").strip()
    if p_dir and os.path.isdir(p_dir):
        my_home = os.path.abspath(os.path.expanduser("~"))
        abs_p = os.path.abspath(p_dir)
        # When raspyjack.service runs as root (~=/root), a user browser profile under /home/<user>
        # must be passed explicitly as `browser:/path/to/profile` or yt-dlp won't find it!
        if not abs_p.startswith(my_home + os.sep) and abs_p != my_home:
            return f"{b_key}:{abs_p}"
    return b_key


def resolve_ytdlp_cookie_args(cfg: Optional[dict] = None) -> Tuple[List[str], str, str]:
    """Return `(ytdlp_cli_args, human_status_label, active_source_key)`.

    `active_source_key` is one of `"file"`, `"firefox"`, `"chromium"`, `"chrome"`,
    `"brave"`, `"off"`, or `"none"`.
    """
    src = str((cfg or {}).get("cookie_source", "auto") or "auto").strip().lower()
    if src == "off":
        return [], "Off (Local Only)", "off"

    cfile = get_cookies_file_path()
    file_ok, _file_msg, file_info = validate_cookies_file(cfile)

    if src == "file":
        if file_ok:
            lbl = "cookies.txt (Auth)" if file_info.get("has_auth") else "cookies.txt"
            return ["--cookies", cfile], lbl, "file"
        return [], "No cookies.txt", "none"

    if src in ("firefox", "chromium", "chrome", "brave"):
        browsers = detect_available_browsers()
        matched = next((b for b in browsers if b["browser"] == src), None)
        spec = _format_browser_cookie_spec(src, matched["profile_dir"] if matched else "")
        return ["--cookies-from-browser", spec], f"Browser: {src.title()}", src

    # Auto mode: prefer cookies.txt first (works headless, never locks browser DB)
    if file_ok:
        lbl = "cookies.txt (Auth)" if file_info.get("has_auth") else "cookies.txt"
        return ["--cookies", cfile], lbl, "file"

    browsers = detect_available_browsers()
    if browsers:
        b = browsers[0]
        spec = _format_browser_cookie_spec(b["browser"], b.get("profile_dir", ""))
        return ["--cookies-from-browser", spec], f"Browser: {b['label']}", b["browser"]

    return [], "Not Linked (Local)", "none"


def get_cookies_status(cfg: Optional[dict] = None) -> dict:
    """Return structured status for Pi Settings UI & WebUI Settings endpoint."""
    cfile = get_cookies_file_path()
    exists = os.path.isfile(cfile)
    file_ok, file_msg, file_info = validate_cookies_file(cfile) if exists else (
        False,
        "No cookies.txt uploaded",
        {"total_cookies": 0, "youtube_cookies": 0, "has_auth": False},
    )
    mtime = 0
    size_bytes = 0
    if exists:
        try:
            st = os.stat(cfile)
            mtime = int(st.st_mtime)
            size_bytes = int(st.st_size)
        except OSError:
            pass
    browsers = detect_available_browsers()
    args, active_label, active_source = resolve_ytdlp_cookie_args(cfg)
    return {
        "configured": bool(exists and file_ok),
        "file_exists": exists,
        "file_valid": file_ok,
        "has_auth": bool(file_info.get("has_auth")),
        "youtube_cookies": int(file_info.get("youtube_cookies", 0)),
        "total_cookies": int(file_info.get("total_cookies", 0)),
        "message": file_msg,
        "path": cfile,
        "mtime": mtime,
        "size_bytes": size_bytes,
        "browsers": [b["browser"] for b in browsers],
        "active_source": active_source,
        "active_label": active_label,
        "has_any_account_source": bool(args),
    }


# ---------------------------------------------------------------------------
# InnerTube `/youtubei/v1/next` Related Video Client (Zero-Login)
# ---------------------------------------------------------------------------

def _text_from_yt_obj(obj: Any) -> str:
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, dict):
        if isinstance(obj.get("simpleText"), str):
            return obj["simpleText"].strip()
        if isinstance(obj.get("content"), str):
            return obj["content"].strip()
        runs = obj.get("runs")
        if isinstance(runs, list):
            return "".join(str(r.get("text", "")) for r in runs if isinstance(r, dict)).strip()
    return ""


def _parse_duration_text(text: str) -> int:
    s = (text or "").strip().upper()
    if not s:
        return 0
    if "LIVE" in s:
        return -1
    m = re.search(r"(\d+):(\d{2})(?::(\d{2}))?", s)
    if not m:
        return 0
    if m.group(3) is not None:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return int(m.group(1)) * 60 + int(m.group(2))


def _parse_view_count_text(text: str) -> int:
    s = (text or "").strip().lower().replace(",", "")
    if not s:
        return 0
    m = re.search(r"(\d+(?:\.\d+)?)\s*([kmb])?\s*(?:view|watching)", s)
    if not m:
        return 0
    val = float(m.group(1))
    mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(m.group(2) or "", 1)
    return int(val * mult)


def _extract_lockup_duration(lockup: dict) -> int:
    # Traverse contentImage -> thumbnailViewModel -> overlays -> thumbnailOverlayBadgeViewModel
    stack: List[Any] = [lockup.get("contentImage")]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "thumbnailBadgeViewModel" in node and isinstance(node["thumbnailBadgeViewModel"], dict):
                txt = str(node["thumbnailBadgeViewModel"].get("text") or "").strip()
                dur = _parse_duration_text(txt)
                if dur != 0:
                    return dur
            for v in node.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(node, list):
            stack.extend(node)
    return 0


def _extract_lockup_item(lockup: dict, reason: str = "") -> Optional[dict]:
    if not isinstance(lockup, dict):
        return None
    ctype = str(lockup.get("contentType") or "")
    if ctype and "VIDEO" not in ctype.upper():
        return None
    vid = str(lockup.get("contentId") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        return None

    meta_vm = (lockup.get("metadata") or {}).get("lockupMetadataViewModel") or {}
    if not isinstance(meta_vm, dict):
        return None
    title = _text_from_yt_obj(meta_vm.get("title"))
    if not title:
        return None

    channel = ""
    views = 0
    _meta_inner = meta_vm.get("metadata") or {}
    _cm_vm = (_meta_inner.get("contentMetadataViewModel") or {}) if isinstance(_meta_inner, dict) else {}
    rows = (_cm_vm.get("metadataRows") or []) if isinstance(_cm_vm, dict) else []
    if isinstance(rows, list):
        for r_idx, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            parts = row.get("metadataParts") or []
            for p_idx, part in enumerate(parts):
                txt = _text_from_yt_obj(part.get("text") if isinstance(part, dict) else part)
                if not txt:
                    continue
                if r_idx == 0 and p_idx == 0 and not channel and "view" not in txt.lower():
                    channel = txt
                elif not views and ("view" in txt.lower() or "watching" in txt.lower()):
                    views = _parse_view_count_text(txt)

    # Channel browse endpoint (optional)
    channel_id = ""
    channel_url = ""
    try:
        browse: Any = meta_vm.get("image") or {}
        for _k in (
            "decoratedAvatarViewModel",
            "rendererContext",
            "commandContext",
            "onTap",
            "innertubeCommand",
            "browseEndpoint",
        ):
            browse = (browse.get(_k) or {}) if isinstance(browse, dict) else {}
        if isinstance(browse, dict):
            channel_id = str(browse.get("browseId") or "")
            base_u = str(browse.get("canonicalBaseUrl") or "")
            if base_u.startswith("/"):
                channel_url = f"https://www.youtube.com{base_u}"
    except Exception:
        pass

    dur = _extract_lockup_duration(lockup)
    return {
        "id": vid,
        "title": title[:100],
        "channel": channel[:48],
        "channel_id": channel_id,
        "channel_url": channel_url,
        "duration": dur,
        "views": views,
        "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        "reason": reason,
    }


def _extract_compact_video_item(cvr: dict, reason: str = "") -> Optional[dict]:
    if not isinstance(cvr, dict):
        return None
    vid = str(cvr.get("videoId") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        return None
    title = _text_from_yt_obj(cvr.get("title"))
    if not title:
        return None
    channel = _text_from_yt_obj(cvr.get("longBylineText")) or _text_from_yt_obj(cvr.get("shortBylineText"))
    channel_id = ""
    channel_url = ""
    for by_key in ("longBylineText", "shortBylineText"):
        runs = (cvr.get(by_key) or {}).get("runs") if isinstance(cvr.get(by_key), dict) else None
        if isinstance(runs, list) and runs:
            ep = (runs[0].get("navigationEndpoint") or {}).get("browseEndpoint") or {}
            if isinstance(ep, dict):
                channel_id = str(ep.get("browseId") or "")
                base_u = str(ep.get("canonicalBaseUrl") or "")
                if base_u.startswith("/"):
                    channel_url = f"https://www.youtube.com{base_u}"
                break
    dur = _parse_duration_text(_text_from_yt_obj(cvr.get("lengthText")))
    views = _parse_view_count_text(
        _text_from_yt_obj(cvr.get("viewCountText")) or _text_from_yt_obj(cvr.get("shortViewCountText"))
    )
    return {
        "id": vid,
        "title": title[:100],
        "channel": channel[:48],
        "channel_id": channel_id,
        "channel_url": channel_url,
        "duration": dur,
        "views": views,
        "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        "reason": reason,
    }


def parse_innertube_next_payload(data: Any, reason: str = "", limit: int = 16) -> List[dict]:
    """Parse related videos from a YouTube InnerTube `/youtubei/v1/next` JSON payload.

    Supports both modern `lockupViewModel` cards and legacy `compactVideoRenderer`
    cards, including nested `itemSectionRenderer` wrappers.
    """
    if not isinstance(data, dict):
        return []

    # Primary fast path: secondaryResults.results
    _c_root = data.get("contents") or {}
    _two_col = (_c_root.get("twoColumnWatchNextResults") or {}) if isinstance(_c_root, dict) else {}
    _sec1 = (_two_col.get("secondaryResults") or {}) if isinstance(_two_col, dict) else {}
    _sec2 = (_sec1.get("secondaryResults") or {}) if isinstance(_sec1, dict) else {}
    results_root: Any = _sec2.get("results") if isinstance(_sec2, dict) else None
    search_roots = [results_root] if results_root is not None else [data]

    out: List[dict] = []
    seen: Set[str] = set()
    stack: List[Any] = list(reversed(search_roots))

    while stack and len(out) < limit:
        node = stack.pop()
        if isinstance(node, list):
            for child in reversed(node):
                stack.append(child)
        elif isinstance(node, dict):
            item: Optional[dict] = None
            if "lockupViewModel" in node:
                item = _extract_lockup_item(node["lockupViewModel"], reason=reason)
            elif "compactVideoRenderer" in node:
                item = _extract_compact_video_item(node["compactVideoRenderer"], reason=reason)
            elif "videoRenderer" in node:
                item = _extract_compact_video_item(node["videoRenderer"], reason=reason)

            if item is not None:
                if item["id"] not in seen:
                    seen.add(item["id"])
                    out.append(item)
                continue

            # Descend into wrapper containers (itemSectionRenderer, contents, etc.)
            for k in ("itemSectionRenderer", "contents", "items", "results"):
                child = node.get(k)
                if isinstance(child, (dict, list)):
                    stack.append(child)

    return out


def _normalize_ytdlp_entry(ent: dict, reason: str = "") -> Optional[dict]:
    if not isinstance(ent, dict):
        return None
    vid = str(ent.get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        return None
    title = str(ent.get("title") or "").strip()
    if not title or title in ("[Deleted video]", "[Private video]"):
        return None
    channel = str(ent.get("channel") or ent.get("uploader") or "").strip()
    channel_id = str(ent.get("channel_id") or ent.get("uploader_id") or "").strip()
    channel_url = str(ent.get("channel_url") or ent.get("uploader_url") or "").strip()
    try:
        dur = int(ent.get("duration") or 0)
    except Exception:
        dur = 0
    try:
        views = int(ent.get("view_count") or 0)
    except Exception:
        views = 0
    return {
        "id": vid,
        "title": title[:100],
        "channel": channel[:48],
        "channel_id": channel_id,
        "channel_url": channel_url,
        "duration": dur,
        "views": views,
        "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        "reason": reason,
    }


def fetch_related_via_rd_mix(
    video_id: str,
    reason: str = "",
    limit: int = 14,
    timeout: float = 18.0,
) -> List[dict]:
    """Fallback related-video fetcher using YouTube's auto-generated Mix (`list=RD<id>`)."""
    vid = str(video_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid) or not shutil.which("yt-dlp"):
        return []
    url = f"https://www.youtube.com/watch?v={vid}&list=RD{vid}"
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--no-warnings",
        "--playlist-end",
        str(max(4, int(limit) + 2)),
        url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not r.stdout.strip():
            return []
        data = json.loads(r.stdout)
        entries = data.get("entries") or []
    except Exception:
        return []
    out: List[dict] = []
    seen: Set[str] = {vid}
    for ent in entries:
        item = _normalize_ytdlp_entry(ent, reason=reason)
        if item and item["id"] not in seen:
            seen.add(item["id"])
            out.append(item)
            if len(out) >= limit:
                break
    return out


def fetch_innertube_related(
    video_id: str,
    reason: str = "",
    limit: int = 16,
    timeout: float = 8.0,
    use_rd_fallback: bool = True,
) -> List[dict]:
    """Fetch related videos for a seed `video_id` via InnerTube `/youtubei/v1/next`.

    Falls back automatically to `yt-dlp` Mix (`list=RD<id>`) if InnerTube
    returns empty or fails.
    """
    vid = str(video_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        return []

    payload = json.dumps({
        "context": INNERTUBE_CLIENT_CONTEXT,
        "videoId": vid,
    }).encode("utf-8")
    req = urllib.request.Request(
        INNERTUBE_NEXT_URL,
        data=payload,
        headers=INNERTUBE_HEADERS,
        method="POST",
    )
    items: List[dict] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(2 * 1024 * 1024)
        data = json.loads(raw.decode("utf-8", errors="replace"))
        items = [x for x in parse_innertube_next_payload(data, reason=reason, limit=limit + 2) if x.get("id") != vid][:limit]
    except Exception:
        items = []

    if not items and use_rd_fallback:
        items = fetch_related_via_rd_mix(vid, reason=reason, limit=limit)
    return items


# ---------------------------------------------------------------------------
# Tier 2 — Real YouTube Account Feed (`yt-dlp` Native Extractors)
# ---------------------------------------------------------------------------

def fetch_account_feed(
    feed_kind: str = "recommended",
    start: int = 1,
    count: int = 20,
    cookie_args: Optional[Sequence[str]] = None,
    timeout: float = 35.0,
) -> Tuple[List[dict], str]:
    """Fetch a personalized YouTube account feed using `yt-dlp` cookies."""
    cargs = list(cookie_args or [])
    if not cargs:
        return [], "No YouTube cookies configured"
    if not shutil.which("yt-dlp"):
        return [], "yt-dlp not installed"

    target_spec, target_label = ACCOUNT_FEED_TARGETS.get(
        feed_kind, ACCOUNT_FEED_TARGETS["recommended"]
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
        target_spec,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return [], "Account feed lookup timed out"
    except Exception as exc:
        return [], str(exc)[:64]

    stderr_l = (r.stderr or "").lower()
    if r.returncode != 0 or not (r.stdout or "").strip():
        if any(tok in stderr_l for tok in ("sign in", "login", "cookies", "401", "403", "authenticated")):
            return [], "YouTube cookies expired or not signed in"
        if "could not copy" in stderr_l or "secretstorage" in stderr_l or "pycryptodomex" in stderr_l or "keyring" in stderr_l:
            return [], "Browser cookie DB locked — upload cookies.txt via WebUI"
        err_line = next((ln.strip() for ln in (r.stderr or "").splitlines() if ln.strip()), "")
        return [], (err_line[:72] if err_line else "Could not fetch account feed")

    try:
        data = json.loads(r.stdout)
    except Exception:
        return [], "Invalid response from yt-dlp"

    entries = data.get("entries") or []
    out: List[dict] = []
    seen: Set[str] = set()
    for ent in entries:
        item = _normalize_ytdlp_entry(ent, reason=target_label)
        if item and item["id"] not in seen:
            seen.add(item["id"])
            out.append(item)

    if not out:
        if any(tok in stderr_l for tok in ("sign in", "login", "cookies")):
            return [], "YouTube cookies expired or not signed in"
        return [], f"No videos in {target_label}"
    return out, ""


# ---------------------------------------------------------------------------
# Tier 1 — Zero-Login Local Smart Feed Ranking & Pagination
# ---------------------------------------------------------------------------

def _short_label(text: str, max_len: int = 18) -> str:
    s = (text or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max(1, max_len - 1)].rstrip() + "…"


def _interleave_by_channel(items: List[dict], max_consecutive: int = 2) -> List[dict]:
    """Reorder ranked candidates so at most `max_consecutive` items share a channel."""
    pool = list(items)
    result: List[dict] = []
    while pool:
        last_ch = ""
        streak = 0
        if result:
            last_ch = str(result[-1].get("channel") or "").strip().lower()
            if last_ch:
                for prev in reversed(result):
                    if str(prev.get("channel") or "").strip().lower() == last_ch:
                        streak += 1
                    else:
                        break
        picked_idx = 0
        if last_ch and streak >= max_consecutive:
            for idx, cand in enumerate(pool):
                c_ch = str(cand.get("channel") or "").strip().lower()
                if c_ch != last_ch:
                    picked_idx = idx
                    break
        result.append(pool.pop(picked_idx))
    return result


def build_local_personalized_feed(
    page: int = 1,
    exclude_ids: Optional[Set[str]] = None,
    taste: Optional[dict] = None,
    liked: Optional[List[dict]] = None,
    subs: Optional[List[dict]] = None,
    playlists: Optional[List[dict]] = None,
    search_history: Optional[List[str]] = None,
    presets: Optional[List[str]] = None,
    page_size: int = 18,
    fetch_related_fn: Optional[Callable[[str, str, int], List[dict]]] = None,
    fetch_channel_fn: Optional[Callable[[str, int], Tuple[List[dict], str]]] = None,
    search_fn: Optional[Callable[[str, int], Tuple[List[dict], str]]] = None,
) -> Tuple[List[dict], str, dict]:
    """Build a personalized zero-login feed from local user signals.

    `page` starts at 1; higher pages automatically rotate through deeper
    watch/like/playlist seeds, subscriptions, and search topics so `+ Load More`
    always returns fresh recommendations.
    """
    prof = _normalize_taste_profile(taste) if taste is not None else load_taste_profile()
    liked_list = list(liked or [])
    subs_list = list(subs or [])
    pl_list = list(playlists or [])
    hist_list = [str(q).strip() for q in (search_history or []) if str(q).strip()]
    preset_list = [str(q).strip() for q in (presets or []) if str(q).strip()]

    p_idx = max(0, int(page) - 1)
    rel_fn = fetch_related_fn or (lambda vid, reason, lim: fetch_innertube_related(vid, reason=reason, limit=lim))

    dismissed: Set[str] = set(prof.get("dismissed_ids", []))
    muted_ch: Set[str] = {c.lower() for c in prof.get("muted_channels", [])}
    recent_watched_ids: Set[str] = {str(w.get("id") or "") for w in prof.get("watched", [])[:40]}
    blocked_ids: Set[str] = set(exclude_ids or set()) | dismissed | recent_watched_ids

    # Build ordered seed lists (video seeds, channel seeds, topic queries)
    video_seeds: List[Tuple[str, str, float]] = []
    seen_seed_vids: Set[str] = set()

    def _add_video_seed(vid: str, reason: str, base_score: float) -> None:
        v = str(vid or "").strip()
        if v and v not in seen_seed_vids:
            seen_seed_vids.add(v)
            video_seeds.append((v, reason, base_score))

    for w in prof.get("watched", []):
        ch_lbl = w.get("channel") or w.get("title") or "Recent Watch"
        _add_video_seed(w.get("id", ""), f"Because you watched {_short_label(ch_lbl)}", 3.0)

    for lk in liked_list:
        ch_lbl = lk.get("channel") or lk.get("title") or "Liked Video"
        _add_video_seed(lk.get("id", ""), f"Similar to liked: {_short_label(ch_lbl)}", 2.6)

    for pl in pl_list:
        if not isinstance(pl, dict):
            continue
        pl_nm = str(pl.get("name") or "Playlist")
        for it in (pl.get("items") or [])[:6]:
            if isinstance(it, dict):
                _add_video_seed(it.get("id", ""), f"From {_short_label(pl_nm)}", 2.2)

    # Channel seeds (followed subscriptions)
    channel_seeds: List[Tuple[str, str, float]] = []
    for sub in subs_list:
        if not isinstance(sub, dict):
            continue
        nm = str(sub.get("name") or "").strip()
        ref = str(sub.get("channel_url") or sub.get("channel_id") or nm).strip()
        if nm and nm.lower() not in muted_ch and ref:
            channel_seeds.append((ref, f"Followed: {_short_label(nm)}", 2.8))

    # Topic seeds (search history + top learned keywords)
    topic_seeds: List[Tuple[str, str, float]] = []
    seen_topics: Set[str] = set()
    for q in hist_list:
        ql = q.lower()
        if ql not in seen_topics and not ql.startswith("http"):
            seen_topics.add(ql)
            topic_seeds.append((q, f"Topic: {_short_label(q)}", 1.7))

    top_kws = sorted(prof.get("keywords", {}).items(), key=lambda kv: kv[1], reverse=True)
    for i in range(0, min(len(top_kws), 8), 2):
        pair = " ".join(k for k, _ in top_kws[i:i + 2]).strip()
        if pair and pair not in seen_topics:
            seen_topics.add(pair)
            topic_seeds.append((pair, f"For your interest: {_short_label(pair)}", 1.6))

    is_cold_start = not (video_seeds or channel_seeds or topic_seeds)
    if is_cold_start:
        starters = preset_list or list(_COLD_START_QUERIES)
        for q in starters:
            ql = q.lower()
            if ql not in seen_topics:
                seen_topics.add(ql)
                topic_seeds.append((q, f"Starter Pick: {_short_label(q)}", 1.5))

    # Select this page's slice of tasks so pagination always explores new seeds
    tasks: List[Tuple[str, str, str, float]] = []
    # 1) Up to 3 video seeds for this page
    if video_seeds:
        v_start = p_idx * 3
        v_slice = video_seeds[v_start:v_start + 3]
        if not v_slice and p_idx > 0:
            # Wrap with deeper fetch limit if user paginated far
            v_slice = video_seeds[:2]
        for vid, reason, score in v_slice:
            tasks.append(("related", vid, reason, score))

    # 2) Up to 2 followed channel seeds for this page
    if channel_seeds and fetch_channel_fn is not None:
        c_start = p_idx * 2
        c_slice = channel_seeds[c_start:c_start + 2]
        for ref, reason, score in c_slice:
            tasks.append(("channel", ref, reason, score))

    # 3) Up to 2 topic discovery seeds (3 on cold start)
    if topic_seeds and search_fn is not None:
        t_per_page = 3 if is_cold_start else (2 if len(tasks) < 3 else 1)
        t_start = p_idx * t_per_page
        t_slice = topic_seeds[t_start:t_start + t_per_page]
        for q, reason, score in t_slice:
            tasks.append(("search", q, reason, score))

    if not tasks:
        return [], "No more feed recommendations available", {
            "source": "local",
            "source_label": "Local Smart Feed",
            "cold_start": is_cold_start,
            "seed_count": 0,
        }

    # Fetch candidate batches concurrently (bounded to 4 workers for Pi friendliness)
    def _run_task(t: Tuple[str, str, str, float]) -> Tuple[List[dict], str, float]:
        kind, arg, reason, base_score = t
        try:
            if kind == "related":
                res = rel_fn(arg, reason, 14 if p_idx == 0 else 20)
                return list(res or []), reason, base_score
            if kind == "channel" and fetch_channel_fn is not None:
                res, _err = fetch_channel_fn(arg, 8 if p_idx == 0 else 14)
                return list(res or []), reason, base_score
            if kind == "search" and search_fn is not None:
                res, _err = search_fn(arg, 8 if p_idx == 0 else 12)
                return list(res or []), reason, base_score
        except Exception:
            pass
        return [], reason, base_score

    batches: List[Tuple[List[dict], str, float]] = []
    max_w = max(1, min(3, len(tasks)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as pool:
        futs = [pool.submit(_run_task, t) for t in tasks]
        for fut in futs:
            try:
                batches.append(fut.result(timeout=28.0))
            except Exception:
                pass

    # Merge, filter, and rank candidates
    sub_names_l: Set[str] = {str(s.get("name") or "").strip().lower() for s in subs_list if isinstance(s, dict)}
    ch_weights: Dict[str, int] = {k.lower(): int(v) for k, v in prof.get("channels", {}).items()}
    kw_weights: Dict[str, int] = {k.lower(): int(v) for k, v in prof.get("keywords", {}).items()}

    candidates: Dict[str, Tuple[float, dict, int]] = {}
    for batch_items, reason, base_score in batches:
        for pos, raw_it in enumerate(batch_items):
            if not isinstance(raw_it, dict):
                continue
            vid = str(raw_it.get("id") or "").strip()
            if not vid or vid in blocked_ids:
                continue
            ch_name = str(raw_it.get("channel") or "").strip()
            if ch_name and ch_name.lower() in muted_ch:
                continue

            # Position decay within batch so top related picks stay near top
            item_score = base_score - (pos * 0.06)
            if ch_name:
                chl = ch_name.lower()
                if chl in sub_names_l:
                    item_score += 0.9
                if chl in ch_weights:
                    item_score += min(1.8, ch_weights[chl] * 0.35)
            kw_bonus = 0.0
            for kw in extract_keywords(str(raw_it.get("title") or ""), max_terms=6):
                if kw in kw_weights:
                    kw_bonus += 0.25
            item_score += min(1.25, kw_bonus)

            if vid in candidates:
                prev_score, prev_item, hit_cnt = candidates[vid]
                # Multi-seed consensus boost!
                merged_score = max(prev_score, item_score) + 1.4
                if not prev_item.get("reason") and reason:
                    prev_item["reason"] = reason
                candidates[vid] = (merged_score, prev_item, hit_cnt + 1)
            else:
                it_copy = dict(raw_it)
                it_copy["reason"] = str(it_copy.get("reason") or reason)
                if not it_copy.get("thumb"):
                    it_copy["thumb"] = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                candidates[vid] = (item_score, it_copy, 1)

    ranked = [entry[1] for entry in sorted(candidates.values(), key=lambda x: x[0], reverse=True)]
    diverse = _interleave_by_channel(ranked, max_consecutive=2)[:max(1, int(page_size))]

    if not diverse:
        return [], "Could not load feed (check internet connection)", {
            "source": "local",
            "source_label": "Local Smart Feed",
            "cold_start": is_cold_start,
            "seed_count": len(tasks),
        }

    return diverse, "", {
        "source": "local",
        "source_label": "Starter Feed" if is_cold_start else "Local Smart Feed",
        "cold_start": is_cold_start,
        "seed_count": len(tasks),
    }


# ---------------------------------------------------------------------------
# Hybrid Feed Orchestrator (Auto / Local / Account + 15-Min Cache)
# ---------------------------------------------------------------------------

def _filter_blocked_items(items: List[dict], exclude_ids: Optional[Set[str]] = None) -> List[dict]:
    prof = load_taste_profile()
    dismissed = set(prof.get("dismissed_ids", [])) | set(exclude_ids or set())
    muted = {c.lower() for c in prof.get("muted_channels", [])}
    out: List[dict] = []
    seen: Set[str] = set()
    for it in items:
        vid = str(it.get("id") or "").strip()
        ch = str(it.get("channel") or "").strip().lower()
        if not vid or vid in dismissed or vid in seen:
            continue
        if ch and ch in muted:
            continue
        seen.add(vid)
        out.append(it)
    return out


def fetch_personalized_feed(
    cfg: Optional[dict] = None,
    source: str = "auto",
    page: int = 1,
    exclude_ids: Optional[Set[str]] = None,
    force_refresh: bool = False,
    liked: Optional[List[dict]] = None,
    subs: Optional[List[dict]] = None,
    playlists: Optional[List[dict]] = None,
    search_history: Optional[List[str]] = None,
    presets: Optional[List[str]] = None,
    fetch_related_fn: Optional[Callable[[str, str, int], List[dict]]] = None,
    fetch_channel_fn: Optional[Callable[[str, int], Tuple[List[dict], str]]] = None,
    search_fn: Optional[Callable[[str, int], Tuple[List[dict], str]]] = None,
    fetch_account_fn: Optional[Callable[..., Tuple[List[dict], str]]] = None,
) -> Tuple[List[dict], str, dict]:
    """Unified feed loader combining Tier 2 (YouTube Account) and Tier 1 (Local Smart Feed).

    `source` values:
      - `"auto"` (follows `cfg["feed_mode"]`: account if cookies exist, else local)
      - `"local"` (always Local Smart Feed)
      - `"account"` or `"account_recommended"` (`:ytrecommended`)
      - `"account_subs"` (`:ytsubs`)
      - `"account_watchlater"` (`:ytwatchlater`)
      - `"account_history"` (`:ythistory`)
    """
    eff_source = str(source or "auto").strip().lower()
    if eff_source == "auto":
        mode_cfg = str((cfg or {}).get("feed_mode", "auto") or "auto").strip().lower()
        if mode_cfg in ("local", "account"):
            eff_source = mode_cfg

    cookie_args, cookie_lbl, cookie_key = resolve_ytdlp_cookie_args(cfg)
    acct_fn = fetch_account_fn or fetch_account_feed

    # Cache check for page 1
    cache_key = f"{eff_source}:{cookie_key}"
    if page == 1 and not force_refresh and not exclude_ids:
        with _CACHE_LOCK:
            hit = _FEED_CACHE.get(cache_key)
            if hit is not None and time.monotonic() < hit[0]:
                filtered = _filter_blocked_items(hit[1])
                if filtered:
                    return filtered, "", dict(hit[2], cached=True)

    account_kind_map = {
        "account": "recommended",
        "account_recommended": "recommended",
        "account_subs": "subscriptions",
        "account_watchlater": "watchlater",
        "account_history": "history",
    }

    # Explicit account feed requested
    if eff_source in account_kind_map:
        kind = account_kind_map[eff_source]
        if not cookie_args:
            return [], "Account not linked — upload cookies.txt in WebUI or Settings", {
                "source": eff_source,
                "source_label": ACCOUNT_FEED_TARGETS[kind][1],
                "cookie_label": cookie_lbl,
            }
        start_idx = (max(1, int(page)) - 1) * 20 + 1
        items, err = acct_fn(feed_kind=kind, start=start_idx, count=20, cookie_args=cookie_args)
        if err:
            return [], err, {
                "source": eff_source,
                "source_label": ACCOUNT_FEED_TARGETS[kind][1],
                "cookie_label": cookie_lbl,
            }
        filtered = _filter_blocked_items(items, exclude_ids=exclude_ids)
        meta = {
            "source": eff_source,
            "source_label": ACCOUNT_FEED_TARGETS[kind][1],
            "cookie_label": cookie_lbl,
            "fallback_notice": "",
        }
        if page == 1 and filtered:
            with _CACHE_LOCK:
                _FEED_CACHE[cache_key] = (time.monotonic() + _FEED_CACHE_TTL, list(filtered), dict(meta))
        return filtered, "", meta

    # Auto mode: try Account Home first when cookies are present, fall back smoothly to Local
    fallback_notice = ""
    if eff_source == "auto" and cookie_args:
        start_idx = (max(1, int(page)) - 1) * 20 + 1
        items, err = acct_fn(feed_kind="recommended", start=start_idx, count=20, cookie_args=cookie_args)
        if not err and items:
            filtered = _filter_blocked_items(items, exclude_ids=exclude_ids)
            if filtered:
                meta = {
                    "source": "account_recommended",
                    "source_label": f"YouTube Account ({cookie_lbl})",
                    "cookie_label": cookie_lbl,
                    "fallback_notice": "",
                }
                if page == 1:
                    with _CACHE_LOCK:
                        _FEED_CACHE[cache_key] = (time.monotonic() + _FEED_CACHE_TTL, list(filtered), dict(meta))
                return filtered, "", meta
        if err:
            fallback_notice = f"Account feed fell back to Local ({err})"

    # Tier 1: Local Smart Feed
    items, err, meta = build_local_personalized_feed(
        page=page,
        exclude_ids=exclude_ids,
        liked=liked,
        subs=subs,
        playlists=playlists,
        search_history=search_history,
        presets=presets,
        fetch_related_fn=fetch_related_fn,
        fetch_channel_fn=fetch_channel_fn,
        search_fn=search_fn,
    )
    meta["cookie_label"] = cookie_lbl
    meta["fallback_notice"] = fallback_notice
    if page == 1 and not err and items:
        with _CACHE_LOCK:
            _FEED_CACHE[cache_key] = (time.monotonic() + _FEED_CACHE_TTL, list(items), dict(meta))
    return items, err, meta
