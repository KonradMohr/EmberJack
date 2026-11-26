"""
evdev_keys – Unified Keyboard Reader for RaspyJack
--------------------------------------------------
Supports:
  1. Desktop HDMI window mode (reads focused key state from rj_keyboard SHM,
     avoiding global key-stealing and root /dev/input permission issues)
  2. Headless HDMI / Raspberry Pi / Linux TTY mode (auto-discovers ALL real
     USB/Bluetooth/PS2/built-in keyboards via Linux evdev with hotplug support
     and full Shift/CapsLock/Ctrl/Alt/Numpad translation)
  3. M5Stack CardputerZero TCA8418 keypad (legacy profile automatically active
     when CARDPUTER_320 or TCA8418 device is detected)
"""

import os
import select
import threading
import time
from typing import Dict, List, Optional

import rj_env
import rj_keyboard

try:
    import evdev
    _EVDEV_IMPORTED = True
except ImportError:
    evdev = None
    _EVDEV_IMPORTED = False

EVDEV_DEVICE = os.environ.get("RJ_KEYBOARD_DEVICE", "").strip()
_PAUSE_FLAG = "/dev/shm/rj_evdev_pause"
_RAW_PIPE = "/dev/shm/rj_key_events"

_kb_cfg = rj_env.get_keyboard_config()
_profile = _kb_cfg.get("resolved_profile", "standard")
_KEYMAP = rj_keyboard.get_nav_map(profile=_profile, kb_cfg=_kb_cfg)

_REVERSE_MAP: Dict[str, List[int]] = {}
for _code, _name in _KEYMAP.items():
    _REVERSE_MAP.setdefault(_name, []).append(_code)

_key_state: Dict[int, bool] = {}
_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_is_host_owner: bool = False
_grab_devices: bool = False

# Required letter keycodes to distinguish real keyboards from power buttons/mice
_REQUIRED_KBD_KEYS = {16, 30, 44, 28}  # Q, A, Z, ENTER


# Virtual uinput devices created by EmberJack / RaspyJack (must never be grabbed back!)
_VIRTUAL_SKIP_NAMES = ("emberjack virtual", "raspyjack virtual")
_VIRTUAL_SKIP_IDS = {(0x1209, 0x524A), (0x1209, 0x524B)}


def is_real_keyboard_device(dev) -> bool:
    """Return True if an evdev InputDevice is a real keyboard (or TCA8418 keypad)."""
    try:
        name = (dev.name or "").lower()
        if any(tok in name for tok in _VIRTUAL_SKIP_NAMES):
            return False
        info = getattr(dev, "info", None)
        if info is not None and (getattr(info, "vendor", 0), getattr(info, "product", 0)) in _VIRTUAL_SKIP_IDS:
            return False
        if "tca8418" in name:
            return True
        caps = dev.capabilities()
        if not caps:
            return False
        ev_key = evdev.ecodes.EV_KEY if _EVDEV_IMPORTED else 1
        keys = set(caps.get(ev_key, []))
        if not keys:
            return False
        # Must expose standard alphabet + Enter (filters out mice, power buttons, HDMI CEC)
        return _REQUIRED_KBD_KEYS.issubset(keys)
    except Exception:
        return False


def discover_keyboard_devices() -> List[str]:
    """Discover all attached real Linux keyboard /dev/input/event* paths."""
    if not _EVDEV_IMPORTED:
        return []
    if EVDEV_DEVICE:
        return [EVDEV_DEVICE] if os.path.exists(EVDEV_DEVICE) else []
    found: List[str] = []
    try:
        paths = evdev.list_devices()
    except Exception:
        paths = []
    for path in sorted(paths):
        try:
            dev = evdev.InputDevice(path)
            ok = is_real_keyboard_device(dev)
            dev.close()
            if ok:
                found.append(path)
        except Exception:
            continue
    return found


def _close_devices(devices: Dict[str, object]) -> None:
    for dev in list(devices.values()):
        try:
            dev.ungrab()
        except Exception:
            pass
        try:
            dev.close()
        except Exception:
            pass
    devices.clear()


def _reader_loop() -> None:
    """Multi-device hotplug evdev reader (idles when another host owns input)."""
    global _profile, _KEYMAP
    open_devs: Dict[str, object] = {}
    last_scan = 0.0
    last_dev_nodes: Optional[tuple] = None
    need_rescan = True

    while not _stop_event.is_set():
        # Idle when hdmi_host.py owns keyboard input in another process (prevents duplicate keypresses)
        if (rj_keyboard.is_host_active() and not _is_host_owner) or os.path.exists(_PAUSE_FLAG):
            if open_devs:
                _close_devices(open_devs)
                need_rescan = True
            _stop_event.wait(0.35)
            continue

        if not _EVDEV_IMPORTED:
            _stop_event.wait(0.5)
            continue

        now = time.monotonic()
        # Only enumerate /dev/input/event* when /dev/input directory entries change,
        # on device error, or periodically when no keyboard is currently open.
        if need_rescan or (now - last_scan) >= 5.0:
            last_scan = now
            try:
                cur_nodes = tuple(sorted(n for n in os.listdir("/dev/input") if n.startswith("event")))
            except OSError:
                cur_nodes = ()
            if need_rescan or cur_nodes != last_dev_nodes or not open_devs:
                last_dev_nodes = cur_nodes
                need_rescan = False
                desired = set(discover_keyboard_devices())
                for stale in list(open_devs.keys()):
                    if stale not in desired:
                        try:
                            open_devs[stale].ungrab()
                        except Exception:
                            pass
                        try:
                            open_devs[stale].close()
                        except Exception:
                            pass
                        del open_devs[stale]
                for path in sorted(desired):
                    if path not in open_devs:
                        try:
                            dev = evdev.InputDevice(path)
                            if _grab_devices:
                                try:
                                    dev.grab()
                                except OSError:
                                    pass
                            open_devs[path] = dev
                            if "tca8418" in (dev.name or "").lower() and _kb_cfg.get("profile") == "auto":
                                _profile = "tca8418"
                                _KEYMAP = rj_keyboard.get_nav_map(profile=_profile, kb_cfg=_kb_cfg)
                        except Exception:
                            continue

        if not open_devs:
            _stop_event.wait(0.5)
            continue

        fd_map = {dev.fd: (path, dev) for path, dev in open_devs.items()}
        try:
            readable, _, _ = select.select(list(fd_map.keys()), [], [], 0.5)
        except (OSError, ValueError):
            _close_devices(open_devs)
            need_rescan = True
            _stop_event.wait(0.25)
            continue

        for fd in readable:
            entry = fd_map.get(fd)
            if not entry:
                continue
            path, dev = entry
            try:
                for event in dev.read():
                    if event.type != evdev.ecodes.EV_KEY:
                        continue
                    pressed = event.value > 0
                    is_repeat = event.value == 2
                    with _lock:
                        _key_state[event.code] = pressed
                    dev_profile = "tca8418" if "tca8418" in (dev.name or "").lower() else _profile
                    rj_keyboard.handle_key_event(
                        event.code,
                        pressed=pressed,
                        is_repeat=is_repeat,
                        profile=dev_profile,
                        kb_cfg=_kb_cfg,
                    )
            except (OSError, BlockingIOError):
                try:
                    dev.close()
                except Exception:
                    pass
                open_devs.pop(path, None)
                need_rescan = True

    _close_devices(open_devs)


def start(as_host: bool = False, grab: bool = False) -> None:
    global _thread, _is_host_owner, _grab_devices
    if as_host:
        _is_host_owner = True
        _grab_devices = bool(grab)
    if _thread is not None and _thread.is_alive():
        return
    if os.environ.get("RJ_NO_EVDEV_AUTOSTART", "0") == "1" and not as_host:
        return
    # Skip raw /dev/input reading when another host owns input or inside a desktop GUI session
    if not as_host and (rj_keyboard.is_host_active() or (rj_env.is_hdmi_mode() and rj_env.has_desktop_session())):
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_reader_loop, daemon=True)
    _thread.start()


def stop() -> None:
    global _thread, _is_host_owner, _grab_devices
    _stop_event.set()
    t = _thread
    _thread = None
    if t is not None and t.is_alive():
        t.join(timeout=0.5)
    _is_host_owner = False
    _grab_devices = False


def is_pressed(button_name: str) -> bool:
    if rj_keyboard.is_pressed(button_name):
        return True
    if rj_keyboard.is_host_active():
        return False
    in_text = rj_keyboard.is_text_mode()
    active_map = rj_keyboard.TEXT_MODE_NAV_CODES if in_text else _KEYMAP
    with _lock:
        for code, mapped in active_map.items():
            if mapped == button_name and _key_state.get(code, False):
                return True
    return False


def is_key_pressed(evdev_code: int) -> bool:
    if rj_keyboard.is_key_pressed(evdev_code):
        return True
    with _lock:
        return _key_state.get(evdev_code, False)


def any_pressed() -> bool:
    if rj_keyboard.any_pressed():
        return True
    with _lock:
        return any(_key_state.values())


def get_pressed_button() -> Optional[str]:
    btn = rj_keyboard.get_pressed_button()
    if btn:
        return btn
    in_text = rj_keyboard.is_text_mode()
    active_map = rj_keyboard.TEXT_MODE_NAV_CODES if in_text else _KEYMAP
    with _lock:
        for code, pressed in _key_state.items():
            if pressed and code in active_map:
                return active_map[code]
    return None


def get_modifiers() -> int:
    return rj_keyboard.get_modifiers()


def set_text_mode(active: bool) -> None:
    rj_keyboard.set_text_mode(active)


def pause() -> None:
    try:
        with open(_PAUSE_FLAG, "w", encoding="utf-8"):
            pass
    except Exception:
        pass
    time.sleep(0.2)


def resume() -> None:
    try:
        os.unlink(_PAUSE_FLAG)
    except OSError:
        pass


if not os.path.exists(_PAUSE_FLAG):
    start()
