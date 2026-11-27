#!/usr/bin/env python3
"""
EmberJack YouTube Streaming, Preload Cache & HTTP Range Proxy Engine
====================================================================
Handles RAM/disk preload caching, ffmpeg video pipeline construction,
keep-alive HTTP client pooling, real-time video frame pacing, chunked
O(1)-RAM HTTP Range proxy, piped chunked HTTP pump, and StreamPreloader.

Split out from payloads/utilities/youtube.py for maintainability while keeping
100% backwards-compatible re-exports in payloads/utilities/youtube.py.
"""

import collections
import errno
import fcntl
import http.client
import http.server
import os
import queue
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

from rj_youtube_data import (
    STREAM_QUALITIES,  # noqa: F401
    _kill_proc,
    get_youtube_data_dir,
)

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
    ("Auto (mpv GPU -> Built-in)", "auto"),
    ("mpv Hardware (GPU/DRM)", "mpv"),
    ("Built-in (Overlay OSD)", "builtin"),
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


def compute_video_render_size(
    quality_label: str,
    max_w: int,
    max_h: int,
    render_scale: str = "auto",
    is_hdmi: bool = True,
) -> Tuple[int, int]:
    """Choose optimal video decode/render dimensions for sharp, smooth built-in playback.

    On HDMI (640x360 canvas):
      - 'fast' (or 'auto' at 144p/240p): 320x180 (exact 4x/6x divisor of 720p/1080p)
      - 'balanced' (explicit opt-in for low-power Pi): 480x270
      - 'sharp' (or 'auto' at 360p/480p/720p/1080p/LOCAL): 640x360 (exact 2x divisor of 720p
        and exact 3x divisor of 1080p — never downscales HD video into blocky 480x270 pixels!)
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

    v_is_http = (not input_is_pipe) and str(video_url).startswith(("http://", "https://"))
    v_is_local_http = str(video_url).startswith(("http://127.0.0.1:", "http://localhost:"))
    if v_is_http:
        if not v_is_local_http:
            cmd += ["-user_agent", _YT_USER_AGENT]
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "2"]
    if input_is_pipe or v_is_local_http or not v_is_http:
        cmd += ["-probesize", "1M", "-analyzeduration", "1M"]
    # Cap decode threads to 2 so Raspberry Pi leaves CPU cores free for hdmi_host & PipeWire/BT
    cmd += ["-thread_queue_size", "512", "-threads", "2"]
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
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=bilinear",
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
    # Native 48 kHz avoids double 48k->44.1k->48k resampling on YouTube Opus/AAC + PipeWire/HDMI/BT
    cmd += [
        "-map", a_map,
        "-af", af_chain,
        "-ac", "2",
        "-ar", "48000",
        "-avoid_negative_ts", "make_zero",
        "-max_muxing_queue_size", "1024",
    ] + list(audio_args)
    return cmd


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


class _KeepAliveHttpClient:
    """Thread-local HTTP/1.1 Keep-Alive connection pool for zero-handshake Range chunks.

    Reuses the open TLS socket to `googlevideo.com` across consecutive 2 MB Range
    requests instead of paying a 150-450 ms TCP+TLS handshake on every chunk.
    Handles HTTP redirects and transparently reconnects if an idle socket closed.
    """

    def __init__(self, timeout: float = 15.0):
        self.timeout = float(timeout)
        self._tls_ctx = ssl.create_default_context()
        self._local = threading.local()
        self._all_lock = threading.Lock()
        self._all_conns: List[http.client.HTTPConnection] = []
        self.connections_created: int = 0
        self.connections_reused: int = 0

    def _get_conn(self, scheme: str, host: str, port: int) -> Tuple[http.client.HTTPConnection, bool]:
        slot = getattr(self._local, "conn_slot", None)
        if slot is not None:
            s_scheme, s_host, s_port, conn = slot
            if s_scheme == scheme and s_host == host and s_port == port and conn.sock is not None:
                self.connections_reused += 1
                return conn, True
            self._close_local()
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=self.timeout, context=self._tls_ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
        self._local.conn_slot = (scheme, host, port, conn)
        self.connections_created += 1
        with self._all_lock:
            self._all_conns.append(conn)
        return conn, False

    def _close_local(self) -> None:
        slot = getattr(self._local, "conn_slot", None)
        self._local.conn_slot = None
        if slot is not None:
            conn = slot[3]
            try:
                conn.close()
            except Exception:
                pass
            with self._all_lock:
                try:
                    self._all_conns.remove(conn)
                except ValueError:
                    pass

    def open_range(self, url: str, range_header: str) -> http.client.HTTPResponse:
        cur_url = str(url)
        for _redir in range(5):
            parsed = urllib.parse.urlsplit(cur_url)
            scheme = (parsed.scheme or "http").lower()
            host = parsed.hostname or ""
            if not host:
                raise ValueError(f"Invalid URL: {cur_url}")
            port = parsed.port or (443 if scheme == "https" else 80)
            req_target = parsed.path or "/"
            if parsed.query:
                req_target = f"{req_target}?{parsed.query}"
            hdrs = {
                "User-Agent": _YT_USER_AGENT,
                "Accept": "*/*",
                "Connection": "keep-alive",
            }
            if range_header:
                hdrs["Range"] = range_header

            resp = None
            for _retry in range(2):
                conn, was_reused = self._get_conn(scheme, host, port)
                try:
                    conn.request("GET", req_target, headers=hdrs)
                    resp = conn.getresponse()
                    break
                except (http.client.HTTPException, OSError):
                    self._close_local()
                    if not was_reused:
                        raise
            if resp is None:
                raise OSError("No HTTP response")

            status = int(resp.status)
            if status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location") or ""
                try:
                    resp.read(4096)
                except Exception:
                    pass
                self.finish(resp, fully_read=(not resp.will_close))
                if not loc:
                    return resp
                cur_url = urllib.parse.urljoin(cur_url, loc)
                continue
            return resp
        raise OSError("Too many redirects")

    def finish(self, resp: Optional[http.client.HTTPResponse], fully_read: bool = True) -> None:
        if resp is None:
            return
        will_close = bool(getattr(resp, "will_close", True))
        try:
            resp.close()
        except Exception:
            pass
        if (not fully_read) or will_close:
            self._close_local()

    def close_all(self) -> None:
        self._close_local()
        with self._all_lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
        for c in conns:
            try:
                c.close()
            except Exception:
                pass


class VideoFramePacer:
    """Jitter-buffer frame presentation pacer for audio-clock-paced ffmpeg playback.

    When `ffmpeg` outputs simultaneously to rawvideo (`pipe:1`) and a blocking audio
    sink (`-f pulse` / ALSA / Bluetooth), frames arrive in bursts whenever the audio
    sink unblocks, followed by pauses. Dropping everything past queue depth 1 destroys
    smooth cadence, while racing ahead of `1.0 * frame_step` creates periodic queue
    starvation stutters!

    `VideoFramePacer` assigns each arriving frame an exact `1.00 * frame_step` deadline
    (trimming gently to `0.96 * frame_step` only when excess backlog builds up), absorbing
    even 250ms Bluetooth/PipeWire audio bursts with zero periodic sawtooth pauses.
    """

    def __init__(self, target_fps: float = 30.0, max_queued: int = 12):
        self.frame_step = 1.0 / max(8.0, min(60.0, float(target_fps or 30.0)))
        self.max_queued = max(4, int(max_queued))
        self.max_burst_lead = max(0.28, self.frame_step * 7.5)
        self.last_due: float = 0.0

    def schedule_due(self, arrival_ts: float, delay_s: float = 0.0, queued_count: int = 0) -> float:
        base_due = float(arrival_ts) + max(0.0, float(delay_s or 0.0))
        if self.last_due <= 0.0 or base_due >= self.last_due + self.frame_step * 3.0:
            due = base_due
        else:
            # Exact 1.00x cadence at normal depth prevents periodic sawtooth starvation;
            # gently catch up (0.96x) only if >=4 frames accumulate in the jitter buffer.
            pace_mult = 0.96 if int(queued_count or 0) >= 4 else 1.0
            due = max(base_due, self.last_due + self.frame_step * pace_mult)
            cap = base_due + self.max_burst_lead
            if due > cap:
                due = cap
        self.last_due = due
        return due

    def pop_due(self, frame_queue: collections.deque, now: float, free_bufs: collections.deque, pool_size: int):
        """Pop the next due frame `(seq, buf)` or `(None, None)` plus recommended sleep time."""
        if not frame_queue:
            return None, None, min(0.012, self.frame_step * 0.40)

        # Drop ONLY genuinely stale frames when CPU/decode fell far behind real time
        max_overdue = self.frame_step * 3.5
        while len(frame_queue) > 1 and (now - frame_queue[0][0]) > max_overdue:
            _old_due, _old_seq, old_b = frame_queue.popleft()
            if len(free_bufs) < pool_size:
                free_bufs.append(old_b)

        head_due = frame_queue[0][0]
        if head_due <= now:
            _due, seq, buf = frame_queue.popleft()
            if frame_queue:
                nxt_wait = max(0.001, min(0.015, frame_queue[0][0] - now))
            else:
                nxt_wait = min(0.012, self.frame_step * 0.40)
            return seq, buf, nxt_wait

        nxt_wait = max(0.001, min(0.015, head_due - now))
        return None, None, nxt_wait

    def shift(self, delta_s: float) -> None:
        if self.last_due > 0.0:
            self.last_due += float(delta_s)


class _RangeProxyHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 16


class _ChunkedRangeProxy:
    """Local seekable HTTP Range proxy that defeats YouTube single-GET throttling in O(1) RAM.

    Translates unbounded/open-ended `ffmpeg` or `mpv` reads into bounded 2 MB upstream
    `Range: bytes=start-end` requests over persistent HTTP/1.1 Keep-Alive TLS sockets
    with a bounded (~8 MB) background prefetch queue and natural TCP backpressure.
    Supports arbitrary HTTP `Range` seeks (including end-of-file MP4 `moov` atoms and
    WebM `Cues`) so 1-to-10-hour videos play smoothly on Raspberry Pi without writing
    to `/dev/shm`!
    """

    def __init__(
        self,
        video_url: str,
        audio_url: str = "",
        chunk_size: int = 2 * 1024 * 1024,
        prefetch_chunks: int = 4,
        timeout: float = 15.0,
    ):
        self.video_url = str(video_url or "")
        self.audio_url = str(audio_url or "")
        self.chunk_size = max(16 * 1024, int(chunk_size))
        self.prefetch_chunks = max(1, min(8, int(prefetch_chunks)))
        self.timeout = float(timeout)

        self.stop_event = threading.Event()
        self.port: int = 0
        self.video_proxy_url: str = ""
        self.audio_proxy_url: str = ""
        self.bytes_served: int = 0
        self.upstream_range_requests: int = 0
        self._started_at: float = 0.0
        self._lock = threading.Lock()
        self._http = _KeepAliveHttpClient(timeout=self.timeout)
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
            resp = None
            fully_read = False
            try:
                resp = self._http.open_range(upstream_url, "bytes=0-0")
                status = int(resp.status)
                if status in (400, 401, 403, 404, 410):
                    self._http.finish(resp, fully_read=False)
                    break
                if status >= 400:
                    self._http.finish(resp, fully_read=False)
                    if self.stop_event.wait(0.2 * (attempt + 1)):
                        break
                    continue
                ct = resp.getheader("Content-Type") or ""
                if ct:
                    info["ctype"] = ct.split(";", 1)[0].strip()
                if status == 206:
                    tot = _parse_content_range_total(resp.getheader("Content-Range") or "")
                    if tot > 0:
                        info["total"] = tot
                    info["accept_ranges"] = True
                    try:
                        resp.read()
                        fully_read = True
                    except Exception:
                        pass
                else:
                    cl = resp.getheader("Content-Length") or ""
                    if cl and str(cl).isdigit():
                        info["total"] = int(cl)
                    info["accept_ranges"] = ((resp.getheader("Accept-Ranges") or "").lower() == "bytes")
                self._http.finish(resp, fully_read=fully_read)
                break
            except Exception:
                self._http.finish(resp, fully_read=False)
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
            resp = None
            fully_read = False
            try:
                resp = self._http.open_range(upstream_url, f"bytes={lo}-{hi}")
                status = int(resp.status)
                if status in (400, 401, 403, 404, 410, 416):
                    self._http.finish(resp, fully_read=False)
                    return None, 0, ""
                if status >= 400:
                    self._http.finish(resp, fully_read=False)
                    if self.stop_event.wait(0.25 * (attempt + 1)):
                        return None, 0, ""
                    continue
                ctype = (resp.getheader("Content-Type") or "application/octet-stream").split(";", 1)[0].strip()
                tot = _parse_content_range_total(resp.getheader("Content-Range") or "") if status == 206 else 0
                if status == 200:
                    cl = resp.getheader("Content-Length") or ""
                    if cl and str(cl).isdigit():
                        tot = int(cl)
                chunks: List[bytes] = []
                got = 0
                eof = False
                while not self.stop_event.is_set() and not req_stop.is_set():
                    part = resp.read(65536)
                    if not part:
                        eof = True
                        break
                    chunks.append(part)
                    got += len(part)
                    if status == 200 and want_len > 0 and got >= (hi + 1):
                        break
                fully_read = eof
                blob = b"".join(chunks)
                if status == 200:
                    blob = blob[lo : hi + 1]
                self._http.finish(resp, fully_read=fully_read)
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
            except Exception:
                self._http.finish(resp, fully_read=False)
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

        # Bounded RAM prefetch queue (~8 MB max per stream) with automatic TCP backpressure
        q_slots = max(8, min(160, (self.prefetch_chunks * self.chunk_size) // 65536))
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
                        try:
                            cand = self._http.open_range(upstream_url, f"bytes={pos}-{req_hi}")
                            code = int(cand.status)
                            if code == 416 and pos > lo:
                                self._http.finish(cand, fully_read=False)
                                return
                            if code in (400, 401, 403, 404, 410):
                                self._http.finish(cand, fully_read=False)
                                return
                            if code >= 400:
                                self._http.finish(cand, fully_read=False)
                            else:
                                resp = cand
                                break
                        except Exception:
                            pass
                        if self.stop_event.wait(0.25 * (attempt + 1)):
                            return

                    if resp is None:
                        return

                    chunk_got = 0
                    eof = False
                    try:
                        status = int(resp.status)
                        if status == 206:
                            with self._lock:
                                self.upstream_range_requests += 1
                            cr_tot = _parse_content_range_total(resp.getheader("Content-Range") or "")
                            if cr_tot > 0 and target_end < 0:
                                with self._lock:
                                    if path in self._meta and self._meta[path]["total"] <= 0:
                                        self._meta[path]["total"] = cr_tot
                            while not self.stop_event.is_set() and not req_stop.is_set():
                                part = resp.read(65536)
                                if not part:
                                    eof = True
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
                                    eof = True
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
                        self._http.finish(resp, fully_read=eof)
                    chunk_idx += 1
            finally:
                self._http._close_local()
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
                resp = None
                for attempt in range(2):
                    if self.stop_event.is_set():
                        return False
                    try:
                        cand = self._http.open_range(upstream_url, f"bytes={pos}-{req_hi}")
                        code = int(cand.status)
                        if code == 416 and pos > 0:
                            self._http.finish(cand, fully_read=False)
                            break
                        if code in (400, 401, 403, 404, 410):
                            self._http.finish(cand, fully_read=False)
                            return False
                        if code >= 400:
                            self._http.finish(cand, fully_read=False)
                        else:
                            resp = cand
                            break
                    except Exception:
                        pass
                    if self.stop_event.wait(0.2 * (attempt + 1)):
                        return False
                if resp is None:
                    break
                got_chunk = 0
                eof = False
                try:
                    status = int(resp.status)
                    while pos < goal and not self.stop_event.is_set():
                        part = resp.read(65536)
                        if not part:
                            eof = True
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
                    self._http.finish(resp, fully_read=eof)
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
                self._http._close_local()
                self.warm_done.set()

        threading.Thread(target=_worker, daemon=True).start()

    def speed_kbps(self) -> float:
        dt = max(0.1, time.monotonic() - self._started_at)
        return (max(self.bytes_served, self.warmed_bytes) / 1024.0) / dt

    def stop(self) -> None:
        self.stop_event.set()
        self._http.close_all()
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
        http_cli = _KeepAliveHttpClient(timeout=self.timeout)
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
                resp = None
                last_err = ""
                for attempt in range(3):
                    if self.stop_event.is_set():
                        return
                    try:
                        cand = http_cli.open_range(self.url, f"bytes={pos}-{end}")
                        code = int(cand.status)
                        if code == 416 and pos > 0:
                            http_cli.finish(cand, fully_read=False)
                            self.done = True
                            return
                        if code in (400, 401, 403, 404, 410):
                            http_cli.finish(cand, fully_read=False)
                            self.failed = True
                            self.error = f"HTTP {code}"
                            return
                        if code >= 400:
                            http_cli.finish(cand, fully_read=False)
                            last_err = f"HTTP {code}"
                        else:
                            resp = cand
                            break
                    except Exception as exc:
                        last_err = str(exc)[:64]
                    time.sleep(0.25 * (attempt + 1))

                if resp is None:
                    self.failed = True
                    self.error = last_err or "HTTP fetch failed"
                    return

                eof = False
                try:
                    status = int(resp.status)
                    if status == 206:
                        self.range_requests += 1
                        cr_tot = _parse_content_range_total(resp.getheader("Content-Range") or "")
                        if cr_tot > 0:
                            self.total_bytes = cr_tot
                        chunk_got = 0
                        while not self.stop_event.is_set():
                            piece = resp.read(65536)
                            if not piece:
                                eof = True
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
                        cl = resp.getheader("Content-Length") or ""
                        if cl and str(cl).isdigit():
                            self.total_bytes = int(cl)
                        while not self.stop_event.is_set():
                            piece = resp.read(65536)
                            if not piece:
                                eof = True
                                break
                            if not self._write_all(fd, piece):
                                return
                            n_len = len(piece)
                            pos += n_len
                            self.downloaded_bytes = pos
                        self.done = True
                        return
                finally:
                    http_cli.finish(resp, fully_read=eof)
        finally:
            http_cli.close_all()
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
