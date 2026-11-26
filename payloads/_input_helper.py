"""
Shared input helper for RaspyJack payloads.
Checks WebUI / HDMI keyboard virtual input first, then falls back to GPIO / gpio_shim.
Reads flip setting from gui_conf.json to swap controls when flipped.
"""

import contextlib
import json
import os
import time
import uuid

try:
    import rj_input
except Exception:
    rj_input = None

try:
    import rj_keyboard
except Exception:
    rj_keyboard = None

try:
    import rj_env
except Exception:
    rj_env = None

_VIRTUAL_TO_BTN = {
    "KEY_UP_PIN": "UP",
    "KEY_DOWN_PIN": "DOWN",
    "KEY_LEFT_PIN": "LEFT",
    "KEY_RIGHT_PIN": "RIGHT",
    "KEY_PRESS_PIN": "OK",
    "KEY1_PIN": "KEY1",
    "KEY2_PIN": "KEY2",
    "KEY3_PIN": "KEY3",
}
_BTN_TO_VIRTUAL = {v: k for k, v in _VIRTUAL_TO_BTN.items()}
_ARROW_BTNS = ("UP", "DOWN", "LEFT", "RIGHT")
_suppressed_vbtns: dict = {}  # btn -> (count, expires_at_monotonic)

# ---------------------------------------------------------------------------
# Flip detection: swap button meanings when device is flipped 180
# ---------------------------------------------------------------------------
_FLIP_MAP = {
    "UP": "DOWN", "DOWN": "UP",
    "LEFT": "RIGHT", "RIGHT": "LEFT",
    "KEY1": "KEY3", "KEY3": "KEY1",
    "OK": "OK", "KEY2": "KEY2",
}

_flip_enabled = None  # None = not yet loaded, lazy init on first use

_CONF_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gui_conf.json"),
    "/root/Raspyjack/gui_conf.json",
]
_TEXT_SESSION_FILE = os.environ.get("RJ_TEXT_SESSION_FILE", "/dev/shm/rj_text_session.json")
_TEXT_SESSION_TIMEOUT = float(os.environ.get("RJ_TEXT_SESSION_TIMEOUT", "30"))


def _is_flip_enabled():
    """Lazy-load flip setting on first call, cache result."""
    global _flip_enabled
    if _flip_enabled is not None:
        return _flip_enabled
    if rj_env is not None:
        _flip_enabled = rj_env.get_display_flip()
        return _flip_enabled
    _flip_enabled = False
    for p in _CONF_PATHS:
        if os.path.isfile(p):
            try:
                with open(p, "r") as f:
                    _flip_enabled = json.load(f).get("DISPLAY", {}).get("flip", False)
            except Exception:
                pass
            break
    return _flip_enabled


def _flip(btn):
    """Apply flip mapping if device is flipped 180."""
    if _is_flip_enabled() and btn:
        return _FLIP_MAP.get(btn, btn)
    return btn


def get_virtual_button():
    """Return a WebUI / local keyboard virtual button name or None."""
    if rj_input is None:
        return None
    try:
        name = rj_input.get_virtual_button()
    except Exception:
        return None
    if not name:
        return None
    return _flip(_VIRTUAL_TO_BTN.get(name))


_last_btn_time = 0
_last_btn_name = None
_GLOBAL_DEBOUNCE = 0.15


def get_button(pins, gpio):
    """
    Return a button name using WebUI / HDMI keyboard virtual input if available,
    otherwise fall back to GPIO. Includes global debounce.
    """
    global _last_btn_time, _last_btn_name
    mapped = get_virtual_button()
    if mapped:
        now = time.time()
        if mapped == _last_btn_name and (now - _last_btn_time) < _GLOBAL_DEBOUNCE:
            return None
        _last_btn_time = now
        _last_btn_name = mapped
        return mapped
    for btn, pin in pins.items():
        if gpio.input(pin) == 0:
            name = _flip(btn)
            now = time.time()
            if name == _last_btn_name and (now - _last_btn_time) < _GLOBAL_DEBOUNCE:
                return None
            _last_btn_time = now
            _last_btn_name = name
            return name
    _last_btn_name = None
    return None


def get_held_buttons():
    """Return set of currently held WebUI + physical keyboard button names (for games)."""
    held = set()
    if rj_input is not None:
        try:
            held.update(rj_input.get_held_buttons())
        except Exception:
            pass
    if rj_keyboard is not None:
        try:
            held.update(rj_keyboard.get_held_buttons())
        except Exception:
            pass
    mapped = {_VIRTUAL_TO_BTN.get(b, b) for b in held if b in _VIRTUAL_TO_BTN}
    if _is_flip_enabled():
        return {_FLIP_MAP.get(b, b) for b in mapped}
    return mapped


@contextlib.contextmanager
def text_input_mode():
    """Suppress character-to-GPIO collisions while reading full keyboard text input."""
    if rj_keyboard is not None:
        with rj_keyboard.text_input_mode():
            yield
    else:
        yield


def _write_text_session(payload):
    directory = os.path.dirname(_TEXT_SESSION_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temp_path = f"{_TEXT_SESSION_FILE}.tmp.{os.getpid()}"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
    os.replace(temp_path, _TEXT_SESSION_FILE)


def open_remote_text_session(title="Input", default="", charset="full", max_len=64):
    session_id = uuid.uuid4().hex
    payload = {
        "active": True,
        "session_id": session_id,
        "title": str(title or "Input")[:32],
        "default": str(default or "")[:128],
        "charset": str(charset or "full"),
        "max_len": int(max_len),
        "started_at": time.time(),
        "timeout": _TEXT_SESSION_TIMEOUT,
    }
    _write_text_session(payload)
    if rj_input is not None:
        try:
            rj_input.flush_text_events()
        except Exception:
            pass
    return session_id


def close_remote_text_session(session_id=None):
    current = {}
    try:
        if os.path.isfile(_TEXT_SESSION_FILE):
            with open(_TEXT_SESSION_FILE, "r", encoding="utf-8") as handle:
                current = json.load(handle) or {}
    except Exception:
        current = {}
    if session_id and current.get("session_id") not in (None, session_id):
        return
    payload = {
        "active": False,
        "session_id": session_id or current.get("session_id", ""),
        "closed_at": time.time(),
    }
    try:
        _write_text_session(payload)
    except Exception:
        pass


def get_remote_text_event(session_id=None):
    """Return next queued text event from either local physical keyboard or WebUI."""
    if rj_input is None:
        return None
    for _ in range(8):
        try:
            event = rj_input.get_text_event()
        except Exception:
            return None
        if not event:
            return None
        if not isinstance(event, dict):
            continue
        event_session = event.get("session_id")
        # Always accept local physical keyboard events ("local")
        if session_id and event_session not in (None, "", "local", session_id):
            continue
        return event
    return None


def get_text_event(session_id=None):
    """Alias for get_remote_text_event() covering local keyboard + WebUI text input."""
    return get_remote_text_event(session_id=session_id)


def poll_text_char(session_id=None):
    """Return the next typed character (or '\\b', '\\n', '\\x1b', '\\x03', '\\x04', '\\t') or None."""
    ev = get_remote_text_event(session_id=session_id)
    if not ev:
        return None
    spec = str(ev.get("special") or "")
    if spec == "BACKSPACE" or spec == "DELETE":
        return "\b"
    if spec == "ENTER":
        return "\n"
    if spec == "ESCAPE":
        return "\x1b"
    if spec == "TAB":
        return "\t"
    if spec.startswith("CTRL_") and len(spec) == 6:
        ch = spec[5]
        if "A" <= ch <= "Z":
            return chr(ord(ch) - ord("A") + 1)
    if not spec:
        key = str(ev.get("key") or "")
        return key if key else None
    return None


def _drain_matching_vbtn(target_btn: str) -> bool:
    """Remove one queued virtual button matching *target_btn* from rj_input._q if present."""
    if rj_input is None:
        return False
    q = getattr(rj_input, "_q", None)
    if q is None:
        return False
    kept = []
    removed = False
    try:
        while True:
            raw = q.get_nowait()
            mapped = _flip(_VIRTUAL_TO_BTN.get(raw))
            if not removed and mapped == target_btn:
                removed = True
                continue
            kept.append(raw)
    except Exception:
        pass
    for raw in kept:
        try:
            q.put_nowait(raw)
        except Exception:
            pass
    return removed


def _consume_suppressed_vbtn(btn: str) -> bool:
    """Return True if *btn* is a duplicate keyboard datagram that should be dropped."""
    entry = _suppressed_vbtns.get(btn)
    if not entry:
        return False
    cnt, exp = entry
    now = time.monotonic()
    if now > exp or cnt <= 0:
        _suppressed_vbtns.pop(btn, None)
        return False
    if cnt <= 1:
        _suppressed_vbtns.pop(btn, None)
    else:
        _suppressed_vbtns[btn] = (cnt - 1, exp)
    return True


def poll_ui_input(pins, gpio):
    """Unified single-event UI input poller for rich keyboard + button payloads.

    Best used inside ``with text_input_mode():``. Eliminates double-firing
    between ``get_text_event()`` and ``get_button()`` (Space/Tab/Enter/Esc/Arrows)
    while preserving F1/F2/F3, WebUI virtual buttons, and physical HAT GPIO.

    Returns ``(action, char)`` where at most one is non-None:
      - ``action``: ``UP|DOWN|LEFT|RIGHT|OK|KEY1|KEY2|KEY3|ENTER|ESCAPE|TAB|
        BACKSPACE|DELETE|PAGEUP|PAGEDOWN|HOME|END|CTRL_*|F4..F9``
      - ``char``: single printable character (including ``" "`` for Space)
    """
    global _last_btn_time, _last_btn_name

    # 1. Lossless keyboard / WebUI text & special events take priority
    ev = get_remote_text_event()
    if ev:
        spec = str(ev.get("special") or "")
        if spec:
            if spec in _ARROW_BTNS:
                act = _flip(spec)
                # Suppress the companion virtual-button datagram emitted by
                # TEXT_MODE_NAV_CODES so one arrow tap never moves 2 rows.
                if ev.get("source") == "keyboard" or ev.get("session_id") == "local":
                    if not _drain_matching_vbtn(act):
                        old_cnt, old_exp = _suppressed_vbtns.get(act, (0, 0.0))
                        now_m = time.monotonic()
                        if now_m > old_exp:
                            old_cnt = 0
                        _suppressed_vbtns[act] = (min(4, old_cnt + 1), now_m + 0.5)
                _last_btn_name = act
                _last_btn_time = time.time()
                return act, None
            return spec, None
        ch = str(ev.get("key") or "")
        if ch:
            return None, ch

    # 2. Virtual buttons (F1/F2/F3 from keyboard, or WebUI buttons)
    for _ in range(8):
        mapped = get_virtual_button()
        if not mapped:
            break
        if _consume_suppressed_vbtn(mapped):
            continue
        now = time.time()
        if mapped == _last_btn_name and (now - _last_btn_time) < _GLOBAL_DEBOUNCE:
            return None, None
        _last_btn_time = now
        _last_btn_name = mapped
        return mapped, None

    # 3. Physical GPIO HAT fallback (skip keyboard-held arrow pins in text mode
    #    because physical keyboard arrows already deliver press + repeat via step 1)
    in_text = bool(rj_keyboard is not None and rj_keyboard.is_text_mode())
    for btn, pin in pins.items():
        if in_text and btn in _ARROW_BTNS:
            vpin = _BTN_TO_VIRTUAL.get(btn)
            if vpin and rj_keyboard.is_pressed(vpin):
                continue
        if gpio.input(pin) == 0:
            name = _flip(btn)
            now = time.time()
            if name == _last_btn_name and (now - _last_btn_time) < _GLOBAL_DEBOUNCE:
                return None, None
            _last_btn_time = now
            _last_btn_name = name
            return name, None

    _last_btn_name = None
    return None, None


def flush_input():
    """Clear all queued and held button state."""
    global _last_btn_name
    _suppressed_vbtns.clear()
    _last_btn_name = None
    if rj_input is not None:
        try:
            rj_input.flush()
        except Exception:
            pass
