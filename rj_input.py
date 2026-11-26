#!/usr/bin/env python3
"""
RaspyJack input bridge
----------------------
Listens on a Unix datagram socket for JSON input events coming from the
WebSocket server and exposes a tiny queue API so the main UI can treat them
like real button presses.

Environment:
  RJ_INPUT_SOCK  Path to AF_UNIX datagram socket (default: /dev/shm/rj_input.sock)

Protocol (JSON, one datagram per message):
    {"type":"input","button":"UP|DOWN|LEFT|RIGHT|OK|KEY1|KEY2|KEY3","state":"press|release"}
    {"type":"text_key","session_id":"...","key":"a"}
    {"type":"text_key","session_id":"...","special":"BACKSPACE|ENTER|ESCAPE"}

Only "press" events are queued; "release" is ignored for simple navigation.
"""

import os, json, threading, socket, queue, atexit
from collections import deque
from typing import Optional

try:
    import rj_keyboard as _rj_keyboard
except Exception:
    _rj_keyboard = None

_SOCK_PATH = os.environ.get("RJ_INPUT_SOCK", "/dev/shm/rj_input.sock")

# Map frontend button names to RaspyJack getButton() return values
_BTN_MAP = {
    "UP": "KEY_UP_PIN",
    "DOWN": "KEY_DOWN_PIN",
    "LEFT": "KEY_LEFT_PIN",
    "RIGHT": "KEY_RIGHT_PIN",
    "OK": "KEY_PRESS_PIN",
    "KEY1": "KEY1_PIN",
    "KEY2": "KEY2_PIN",
    "KEY3": "KEY3_PIN",
}

_q: "queue.Queue[str]" = queue.Queue()
_text_q: "queue.Queue[dict]" = queue.Queue()
_held: set = set()  # currently held buttons (for continuous input like games)
_held_lock = threading.Lock()
_sock_lock = threading.Lock()
_sock: Optional[socket.socket] = None
_sock_path_bound: Optional[str] = None
_sock_inode: Optional[tuple] = None
_listener_thread: Optional[threading.Thread] = None

# Shared-memory local keyboard ring tracking & deduplication against Unix socket
_shm_text_lock = threading.Lock()
_shm_last_seq: int = 0
_seen_local_seqs: deque = deque(maxlen=256)
_seen_local_seq_set: set = set()


def _record_local_seq(seq: int) -> bool:
    """Record a local keyboard seq; return False if already seen (duplicate)."""
    if seq <= 0:
        return True
    if seq in _seen_local_seq_set:
        return False
    if len(_seen_local_seqs) == _seen_local_seqs.maxlen:
        oldest = _seen_local_seqs.popleft()
        _seen_local_seq_set.discard(oldest)
    _seen_local_seqs.append(seq)
    _seen_local_seq_set.add(seq)
    return True


def _sync_shm_cursor() -> None:
    global _shm_last_seq
    if _rj_keyboard is None:
        return
    with _shm_text_lock:
        try:
            _shm_last_seq = int(_rj_keyboard.get_shm_text_seq())
        except Exception:
            pass
        _seen_local_seqs.clear()
        _seen_local_seq_set.clear()


def _drain_shm_text_events() -> None:
    """Pull any new local keyboard events from rj_keyboard SHM ring into _text_q."""
    global _shm_last_seq
    if _rj_keyboard is None:
        return
    with _shm_text_lock:
        try:
            events, new_seq = _rj_keyboard.read_shm_text_events(_shm_last_seq)
        except Exception:
            return
        _shm_last_seq = new_seq
        for ev in events:
            seq = int(ev.get("seq", 0) or 0)
            if not _record_local_seq(seq):
                continue
            try:
                _text_q.put_nowait(ev)
            except Exception:
                pass


def _cleanup():
    global _sock, _sock_path_bound, _sock_inode
    with _sock_lock:
        sock = _sock
        bound_path = _sock_path_bound
        bound_inode = _sock_inode
        _sock = None
        _sock_path_bound = None
        _sock_inode = None
    try:
        if sock is not None:
            sock.close()
    except Exception:
        pass
    # Only unlink the socket path if we still own that exact inode
    # (prevents exiting child payloads from deleting the parent's active socket!)
    if bound_path and bound_inode is not None:
        try:
            st = os.stat(bound_path)
            if (st.st_dev, st.st_ino) == bound_inode:
                os.unlink(bound_path)
        except Exception:
            pass


def _bind_socket() -> Optional[socket.socket]:
    global _sock, _sock_path_bound, _sock_inode
    with _sock_lock:
        if _sock is not None:
            try:
                _sock.close()
            except Exception:
                pass
            _sock = None
            _sock_inode = None
        if os.path.exists(_SOCK_PATH):
            try:
                os.unlink(_SOCK_PATH)
            except OSError:
                pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.bind(_SOCK_PATH)
        except OSError as exc:
            try:
                sock.close()
            except Exception:
                pass
            print(f"[rj_input] Warning: could not bind {_SOCK_PATH}: {exc} (using SHM fallback)")
            return None
        try:
            os.chmod(_SOCK_PATH, 0o666)
        except Exception:
            pass
        try:
            st = os.stat(_SOCK_PATH)
            _sock_inode = (st.st_dev, st.st_ino)
        except OSError:
            _sock_inode = None
        _sock = sock
        _sock_path_bound = _SOCK_PATH
        return sock


def _listen(sock: socket.socket):
    while True:
        try:
            data, _addr = sock.recvfrom(4096)
        except Exception:
            # Socket closed or transient error → exit thread
            break
        try:
            msg = json.loads(data.decode("utf-8", "ignore"))
        except Exception:
            continue
        msg_type = str(msg.get("type", ""))
        if msg_type == "input":
            button = str(msg.get("button", ""))
            state = str(msg.get("state", ""))
            mapped = _BTN_MAP.get(button)
            if not mapped:
                continue
            is_kbd = msg.get("source") == "keyboard"
            if not is_kbd:
                print(f"[rj_input] {button} {state} -> {mapped}")
            if state == "press":
                try:
                    _q.put_nowait(mapped)
                except Exception:
                    pass
                # Local keyboard held state is tracked atomically in rj_keyboard SHM;
                # only track remote/WebUI held buttons in _held to avoid socket lag/desync.
                if not is_kbd:
                    with _held_lock:
                        _held.add(mapped)
            elif state == "release":
                if not is_kbd:
                    with _held_lock:
                        _held.discard(mapped)
            continue
        if msg_type == "text_key":
            seq = int(msg.get("seq", 0) or 0)
            source = str(msg.get("source", "remote"))
            if source == "keyboard" and seq > 0:
                with _shm_text_lock:
                    if not _record_local_seq(seq):
                        continue
            event = {
                "type": "text_key",
                "session_id": str(msg.get("session_id", "")),
                "source": source,
                "mods": int(msg.get("mods", 0) or 0),
            }
            if seq > 0:
                event["seq"] = seq
            if msg.get("special"):
                event["special"] = str(msg.get("special", ""))
            else:
                event["key"] = str(msg.get("key", ""))
            if event["source"] != "keyboard":
                print(f"[rj_input:text] session={event.get('session_id','')} key={event.get('key','')} special={event.get('special','')}")
            try:
                _text_q.put_nowait(event)
            except Exception:
                pass
            continue


def get_virtual_button() -> Optional[str]:
    """Return next virtual button name (e.g. 'KEY_LEFT_PIN') or None."""
    try:
        return _q.get_nowait()
    except queue.Empty:
        return None


def get_held_buttons() -> set:
    """Return set of currently held button names (for continuous input)."""
    with _held_lock:
        return set(_held)


def get_text_event() -> Optional[dict]:
    """Return next queued text event (from Unix socket or rj_keyboard SHM ring) or None."""
    _drain_shm_text_events()
    try:
        return _text_q.get_nowait()
    except queue.Empty:
        return None


def flush_text_events():
    """Clear queued text events and advance SHM cursor past any prior keypresses."""
    _sync_shm_cursor()
    try:
        while not _text_q.empty():
            _text_q.get_nowait()
    except Exception:
        pass


def flush():
    """Clear all queued and held button state."""
    with _held_lock:
        _held.clear()
    try:
        while not _q.empty():
            _q.get_nowait()
    except Exception:
        pass
    flush_text_events()


def _ensure_started():
    global _listener_thread
    if _listener_thread is None or not _listener_thread.is_alive():
        sock = _bind_socket()
        if sock is not None:
            _listener_thread = threading.Thread(target=_listen, args=(sock,), daemon=True)
            _listener_thread.start()


def ensure_socket_healthy():
    """Verify our listener thread and socket inode are still live; rebind if unlinked."""
    need_rebind = _listener_thread is None or not _listener_thread.is_alive()
    if not need_rebind and _sock_inode is not None:
        try:
            st = os.stat(_SOCK_PATH)
            if (st.st_dev, st.st_ino) != _sock_inode:
                need_rebind = True
        except OSError:
            need_rebind = True
    if need_rebind:
        restart_listener()


def restart_listener():
    """
    Recreate the Unix socket listener synchronously so callers can immediately
    send/receive without a bind race.
    """
    global _listener_thread
    old_thread = _listener_thread
    _cleanup()
    if old_thread is not None and old_thread.is_alive():
        old_thread.join(timeout=0.5)
    sock = _bind_socket()
    if sock is not None:
        _listener_thread = threading.Thread(target=_listen, args=(sock,), daemon=True)
        _listener_thread.start()
    _sync_shm_cursor()


# Start on import and register cleanup
_sync_shm_cursor()
_ensure_started()
atexit.register(_cleanup)
