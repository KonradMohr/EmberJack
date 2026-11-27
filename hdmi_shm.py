#!/usr/bin/env python3
"""
RaspyJack HDMI Shared-Memory Frame Transport
--------------------------------------------
Provides lock-free, tear-free frame handoff between raspyjack.py / any of the
351 child payload subprocesses and the persistent hdmi_host.py presenter.

Memory layout (/dev/shm/raspyjack_hdmi.shm):
  Offset 0..63   : Header (struct "<4sIQQIIII" = 40 bytes + 24 bytes padding)
                   magic="RJFB", version=1, seq (odd=writing, even=stable),
                   timestamp_ns, width, height, fmt (0=RGB24), flags (1=cleared)
  Offset 64..    : Raw RGB24 pixel bytes (width * height * 3)
"""

import mmap
import os
import signal
import socket
import struct
import subprocess
import sys
import time
from typing import Optional, Tuple

import numpy as np

SHM_PATH = os.environ.get("RJ_HDMI_SHM", "/dev/shm/raspyjack_hdmi.shm")
WAKE_SOCK_PATH = os.environ.get("RJ_HDMI_WAKE_SOCK", "/dev/shm/raspyjack_hdmi_wake.sock")
HOST_PID_PATH = os.environ.get("RJ_HDMI_HOST_PID", "/dev/shm/raspyjack_hdmi_host.pid")

MAGIC = b"RJFB"
VERSION = 1
HEADER_FMT = "<4sIQQIIII"
HEADER_SIZE = 64
FMT_RGB24 = 0
FLAG_CLEARED = 1
FLAG_NO_HUD = 2  # Hide bottom HDMI key-hint bar & use fast integer/nearest video scaling

# Sparse tmpfs mapping supporting up to 1920x1080 RGB24 (only touched pages allocate RAM)
MAX_WIDTH = 1920
MAX_HEIGHT = 1080
MAX_PAYLOAD_BYTES = MAX_WIDTH * MAX_HEIGHT * 3
SHM_TOTAL_SIZE = HEADER_SIZE + MAX_PAYLOAD_BYTES

_writer_fd: Optional[int] = None
_writer_mm: Optional[mmap.mmap] = None
_reader_fd: Optional[int] = None
_reader_mm: Optional[mmap.mmap] = None
_wake_sock: Optional[socket.socket] = None


def _ensure_shm_file(path: str = SHM_PATH) -> int:
    os.makedirs(os.path.dirname(path) or "/dev/shm", exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o666)
    try:
        os.chmod(path, 0o666)
    except OSError:
        pass
    st = os.fstat(fd)
    if st.st_size < SHM_TOTAL_SIZE:
        os.ftruncate(fd, SHM_TOTAL_SIZE)
    return fd


def _get_writer_mm() -> mmap.mmap:
    global _writer_fd, _writer_mm
    if _writer_mm is not None:
        return _writer_mm
    _writer_fd = _ensure_shm_file(SHM_PATH)
    _writer_mm = mmap.mmap(_writer_fd, SHM_TOTAL_SIZE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
    return _writer_mm


def _get_reader_mm() -> Optional[mmap.mmap]:
    global _reader_fd, _reader_mm
    if _reader_mm is not None:
        return _reader_mm
    if not os.path.exists(SHM_PATH):
        return None
    try:
        _reader_fd = os.open(SHM_PATH, os.O_RDONLY)
        st = os.fstat(_reader_fd)
        if st.st_size < HEADER_SIZE:
            os.close(_reader_fd)
            _reader_fd = None
            return None
        _reader_mm = mmap.mmap(_reader_fd, min(st.st_size, SHM_TOTAL_SIZE), mmap.MAP_SHARED, mmap.PROT_READ)
        return _reader_mm
    except OSError:
        return None


def _ping_host() -> None:
    global _wake_sock
    if not os.path.exists(WAKE_SOCK_PATH):
        return
    try:
        if _wake_sock is None:
            _wake_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            _wake_sock.setblocking(False)
        _wake_sock.sendto(b"\x01", WAKE_SOCK_PATH)
    except Exception:
        pass


def publish_frame(rgb_bytes, width: int, height: int, cleared: bool = False, flags: int = 0) -> int:
    """Write an RGB24 frame into shared memory using a seqlock sequence counter."""
    expected = width * height * 3
    if width <= 0 or height <= 0 or expected > MAX_PAYLOAD_BYTES:
        raise ValueError(f"Unsupported HDMI frame size {width}x{height}")
    if len(rgb_bytes) != expected:
        raise ValueError(f"Expected {expected} RGB24 bytes for {width}x{height}, got {len(rgb_bytes)}")

    mm = _get_writer_mm()
    # Read current sequence counter
    cur_magic, _ver, cur_seq = struct.unpack_from("<4sIQ", mm, 0)
    if cur_magic != MAGIC:
        cur_seq = 0
    write_seq = (cur_seq + 1) | 1  # odd = write in progress
    done_seq = write_seq + 1       # even = committed frame
    out_flags = int(flags) | (FLAG_CLEARED if cleared else 0)
    ts = time.monotonic_ns()

    # Mark odd seq before writing payload
    struct.pack_into(HEADER_FMT, mm, 0, MAGIC, VERSION, write_seq, ts, width, height, FMT_RGB24, out_flags)
    mm[HEADER_SIZE : HEADER_SIZE + expected] = rgb_bytes
    # Commit even seq after payload is fully written
    struct.pack_into(HEADER_FMT, mm, 0, MAGIC, VERSION, done_seq, ts, width, height, FMT_RGB24, out_flags)
    _ping_host()
    return done_seq


def publish_pil_image(img, cleared: bool = False, flags: int = 0) -> int:
    """Publish a PIL Image to HDMI shared memory."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    return publish_frame(img.tobytes(), w, h, cleared=cleared, flags=flags)


def publish_rgb565(raw_rgb565: bytes, width: int, height: int, flags: int = 0) -> int:
    """Convert raw RGB565 little-endian buffer to RGB24 and publish."""
    expected = width * height * 2
    if len(raw_rgb565) != expected:
        raise ValueError(f"Expected {expected} RGB565 bytes for {width}x{height}, got {len(raw_rgb565)}")
    arr = np.frombuffer(raw_rgb565, dtype="<u2").reshape((height, width))
    rgb = np.empty((height, width, 3), dtype=np.uint8)
    r = (arr >> 11) & 0x1F
    g = (arr >> 5) & 0x3F
    b = arr & 0x1F
    rgb[..., 0] = (r << 3) | (r >> 2)
    rgb[..., 1] = (g << 2) | (g >> 4)
    rgb[..., 2] = (b << 3) | (b >> 2)
    return publish_frame(rgb.tobytes(), width, height, cleared=False, flags=flags)


def peek_seq() -> int:
    """Ultra-fast (~1us) header-only check returning the current committed frame seq (or 0)."""
    mm = _get_reader_mm()
    if mm is None:
        return 0
    try:
        magic, ver, seq = struct.unpack_from("<4sIQ", mm, 0)
        if magic != MAGIC or ver != VERSION:
            return 0
        return int(seq)
    except Exception:
        return 0


def clear_frame(width: int, height: int) -> int:
    """Publish a solid black frame."""
    return publish_frame(b"\x00" * (width * height * 3), width, height, cleared=True)


def read_frame(last_seq: int = 0) -> Optional[Tuple[int, int, int, bytes, int]]:
    """Read the latest consistent frame if newer than last_seq.

    Returns (seq, width, height, rgb_bytes, flags) or None.
    """
    mm = _get_reader_mm()
    if mm is None:
        return None

    for _ in range(5):
        magic, ver, seq1, _ts, width, height, fmt, flags = struct.unpack_from(HEADER_FMT, mm, 0)
        if magic != MAGIC or ver != VERSION or seq1 == 0 or seq1 == last_seq:
            return None
        if seq1 & 1:
            time.sleep(0.0005)
            continue
        payload_len = width * height * 3
        if width <= 0 or height <= 0 or fmt != FMT_RGB24 or payload_len > MAX_PAYLOAD_BYTES:
            return None
        data = mm[HEADER_SIZE : HEADER_SIZE + payload_len]
        _magic2, _ver2, seq2 = struct.unpack_from("<4sIQ", mm, 0)
        if seq1 == seq2:
            return seq1, width, height, data, flags
        time.sleep(0.0005)
    return None


def is_host_running() -> Tuple[bool, int]:
    """Check whether hdmi_host.py is alive."""
    try:
        with open(HOST_PID_PATH, "r", encoding="utf-8") as handle:
            pid = int(handle.read().strip())
        os.kill(pid, 0)
        return True, pid
    except Exception:
        return False, 0


def ensure_host_running(hdmi_cfg: Optional[dict] = None, wait_timeout: float = 1.5) -> bool:
    """Ensure persistent hdmi_host.py is running unless disabled by env."""
    if os.environ.get("RJ_HDMI_NO_AUTOHOST", "0") == "1":
        return False
    running, _pid = is_host_running()
    if running:
        return True

    import rj_env

    host_script = os.path.join(rj_env.get_base_dir(), "hdmi_host.py")
    if not os.path.isfile(host_script):
        return False

    env = os.environ.copy()
    env.setdefault("PYTHONPATH", rj_env.get_base_dir())
    if hdmi_cfg:
        if "backend" in hdmi_cfg:
            env.setdefault("RJ_HDMI_BACKEND", str(hdmi_cfg["backend"]))
        if "fullscreen" in hdmi_cfg:
            env.setdefault("RJ_HDMI_FULLSCREEN", "1" if hdmi_cfg["fullscreen"] else "0")
        if "scale_mode" in hdmi_cfg:
            env.setdefault("RJ_HDMI_SCALE", str(hdmi_cfg["scale_mode"]))
        if "fps" in hdmi_cfg:
            env.setdefault("RJ_HDMI_FPS", str(hdmi_cfg["fps"]))

    try:
        subprocess.Popen(
            [sys.executable, host_script],
            cwd=rj_env.get_base_dir(),
            env=env,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return False

    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        running, _ = is_host_running()
        if running:
            return True
        time.sleep(0.05)
    return False


def stop_host() -> None:
    """Gracefully terminate hdmi_host.py if running."""
    running, pid = is_host_running()
    if not running:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        alive, _ = is_host_running()
        if not alive:
            return
        time.sleep(0.05)
    # Escalate to SIGKILL if hdmi_host lingered so EVIOCGRAB is guaranteed released
    try:
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.15)
    except OSError:
        pass


def close_mappings() -> None:
    """Close cached mmap objects (helpful for tests)."""
    global _writer_fd, _writer_mm, _reader_fd, _reader_mm, _wake_sock
    for mm in (_writer_mm, _reader_mm):
        if mm is not None:
            try:
                mm.close()
            except Exception:
                pass
    for fd in (_writer_fd, _reader_fd):
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
    if _wake_sock is not None:
        try:
            _wake_sock.close()
        except Exception:
            pass
    _writer_fd = _writer_mm = _reader_fd = _reader_mm = _wake_sock = None
