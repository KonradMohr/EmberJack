#!/usr/bin/env python3
"""
EmberJack Virtual Keyboard-Mouse & Kiosk Exit Controller
--------------------------------------------------------
Runs alongside standalone GUI apps and games launched from the App Launcher
(Wayland labwc/cage/gamescope/weston and X11 xinit) to provide:

  1. Universal Emergency Exit Hotkey (works inside ANY fullscreen app/game):
       Ctrl + Alt + Q           -> Force close app & return to EmberJack

  2. Clean 2-Mode Virtual Mouse (zero external tools; pure Linux /dev/uinput):
       ScrollLock / Ctrl+Alt+M  -> Toggle Typing Mode <-> Mouse Mode

     Mode 1 — TYPING (Default on launch for ALL apps):
       Keyboard is ungrabbed — 100% native typing and gaming with zero interference.

     Mode 2 — MOUSE (Clean cursor control, max 1-2 keys/action):
       Arrows                   -> Move cursor (LShift=slow, LCtrl=fast)
       Space / Enter            -> Left Click (hold to drag)
       Backspace                -> Right Click
       Tab                      -> Middle Click
       PgUp / PgDn  (or [ / ])  -> Scroll Up / Down
       - / =                    -> Cursor Speed Down / Up (1-10)
       Esc                      -> Return to Typing Mode

Zero external packages beyond python3-evdev and Linux /dev/uinput.
"""

import os
import select
import shutil
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

try:
    import evdev
    from evdev import ecodes as _ecodes
    _EVDEV_OK = True
except ImportError:
    evdev = None
    _ecodes = None
    _EVDEV_OK = False

import evdev_keys

# Operating modes
MODE_TYPING = "typing"
MODE_DESKTOP = "desktop"
VALID_MODES = (MODE_TYPING, MODE_DESKTOP)

# Standard Linux evdev keycodes (<linux/input-event-codes.h>)
KEY_ESC = 1
KEY_MINUS = 12
KEY_EQUAL = 13
KEY_BACKSPACE = 14
KEY_TAB = 15
KEY_Q = 16
KEY_W = 17
KEY_E = 18
KEY_R = 19
KEY_LEFTBRACE = 26
KEY_RIGHTBRACE = 27
KEY_ENTER = 28
KEY_LCTRL = 29
KEY_A = 30
KEY_S = 31
KEY_D = 32
KEY_LSHIFT = 42
KEY_M = 50
KEY_RSHIFT = 54
KEY_LALT = 56
KEY_SPACE = 57
KEY_SCROLLLOCK = 70
KEY_KPENTER = 96
KEY_RCTRL = 97
KEY_RALT = 100
KEY_UP = 103
KEY_PAGEUP = 104
KEY_LEFT = 105
KEY_RIGHT = 106
KEY_DOWN = 108
KEY_PAGEDOWN = 109

# Virtual input event codes
EV_KEY = 1
EV_REL = 2
LED_SCROLLL = 2
REL_X = 0
REL_Y = 1
REL_WHEEL = 8
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112

_CTRL_CODES = {KEY_LCTRL, KEY_RCTRL}
_ALT_CODES = {KEY_LALT, KEY_RALT}
_SHIFT_CODES = {KEY_LSHIFT, KEY_RSHIFT}
_TOGGLE_WAIT_CODES = _CTRL_CODES | _ALT_CODES | {KEY_M, KEY_SCROLLLOCK, KEY_ESC}

# Mouse Mode bindings (clean, max 1-2 keys per action)
_DESK_UP = {KEY_UP}
_DESK_DOWN = {KEY_DOWN}
_DESK_LEFT = {KEY_LEFT}
_DESK_RIGHT = {KEY_RIGHT}
_DESK_MOVE = _DESK_UP | _DESK_DOWN | _DESK_LEFT | _DESK_RIGHT
_DESK_LCLICK = {KEY_SPACE, KEY_ENTER, KEY_KPENTER}
_DESK_RCLICK = {KEY_BACKSPACE}
_DESK_MCLICK = {KEY_TAB}
_DESK_SCROLL_UP = {KEY_PAGEUP, KEY_LEFTBRACE}
_DESK_SCROLL_DN = {KEY_PAGEDOWN, KEY_RIGHTBRACE}
_SPEED_DN_CODES = {KEY_MINUS}
_SPEED_UP_CODES = {KEY_EQUAL}

# Pointer speed presets (1..10): (label, base_px_s, max_px_s, tap_step, scroll_rate)
MOUSE_SPEED_LEVELS: Dict[int, Tuple[str, float, float, int, float]] = {
    1:  ("Very Slow",   35.0,   95.0, 1,  3.0),
    2:  ("Slow",        55.0,  150.0, 1,  4.0),
    3:  ("Gentle",      75.0,  210.0, 1,  4.5),
    4:  ("Balanced",    95.0,  270.0, 2,  5.0),
    5:  ("Medium",     125.0,  350.0, 2,  6.0),
    6:  ("Brisk",      160.0,  440.0, 3,  7.0),
    7:  ("Fast",       200.0,  560.0, 3,  8.0),
    8:  ("Very Fast",  250.0,  700.0, 4,  9.0),
    9:  ("Turbo",      310.0,  860.0, 5, 10.0),
    10: ("Max",        380.0, 1040.0, 6, 12.0),
}
DEFAULT_MOUSE_SPEED = 4


def normalize_mode(mode: str, default: str = MODE_TYPING) -> str:
    m = str(mode or "").strip().lower()
    return m if m in VALID_MODES else default


def get_speed_label(level: int) -> str:
    lv = max(1, min(10, int(level)))
    return MOUSE_SPEED_LEVELS[lv][0]


class VirtualMouseState:
    """Pure, deterministic 2-mode state machine (Typing Mode <-> Mouse Mode)."""

    def __init__(
        self,
        mouse_speed: int = DEFAULT_MOUSE_SPEED,
        initial_mode: str = MODE_TYPING,
    ):
        self.mode: str = normalize_mode(initial_mode, default=MODE_TYPING)
        self.mouse_speed: int = max(1, min(10, int(mouse_speed)))

        self.held_keys: Set[int] = set()
        self.pending_mode: Optional[str] = None  # Deferred grab/ungrab until chord keys release
        self._pending_since: float = 0.0
        self.force_exit_requested: bool = False

        # Smooth motion & sub-pixel accumulators
        self._move_hold_sec: float = 0.0
        self._acc_x: float = 0.0
        self._acc_y: float = 0.0
        self._scroll_acc: float = 0.0

        # Active virtual mouse buttons
        self.btn_left: bool = False
        self.btn_right: bool = False
        self.btn_middle: bool = False

    @property
    def mouse_mode(self) -> bool:
        """True whenever Mouse Mode is active."""
        return self.mode == MODE_DESKTOP

    @property
    def is_desktop_mode(self) -> bool:
        return self.mode == MODE_DESKTOP

    @property
    def needs_grab(self) -> bool:
        return self.mode == MODE_DESKTOP

    def set_speed(self, level: int) -> int:
        self.mouse_speed = max(1, min(10, int(level)))
        return self.mouse_speed

    @property
    def ctrl_held(self) -> bool:
        return bool(self.held_keys & _CTRL_CODES)

    @property
    def alt_held(self) -> bool:
        return bool(self.held_keys & _ALT_CODES)

    @property
    def shift_held(self) -> bool:
        return bool(self.held_keys & _SHIFT_CODES)

    def has_active_motion(self) -> bool:
        if not self.mouse_mode:
            return False
        move_scroll = _DESK_MOVE | _DESK_SCROLL_UP | _DESK_SCROLL_DN
        return bool(self.held_keys & move_scroll)

    def release_all_buttons(self) -> List[Tuple[int, int]]:
        released: List[Tuple[int, int]] = []
        if self.btn_left:
            self.btn_left = False
            released.append((BTN_LEFT, 0))
        if self.btn_right:
            self.btn_right = False
            released.append((BTN_RIGHT, 0))
        if self.btn_middle:
            self.btn_middle = False
            released.append((BTN_MIDDLE, 0))
        return released

    def _reset_accumulators(self) -> None:
        self._move_hold_sec = 0.0
        self._acc_x = 0.0
        self._acc_y = 0.0
        self._scroll_acc = 0.0

    @staticmethod
    def _empty_actions() -> dict:
        return {
            "force_exit": False,
            "mode_changed": None,
            "grab_changed": None,
            "speed_changed": None,
            "buttons": [],
            "rel_nudge": (0, 0),
            "wheel_step": 0,
        }

    def _commit_pending_mode(self, actions: dict) -> bool:
        if self.pending_mode is None:
            return False
        old_mode = self.mode
        old_grab = self.needs_grab
        new_mode = self.pending_mode
        self.pending_mode = None
        self._pending_since = 0.0
        if new_mode != old_mode:
            self.mode = new_mode
            self._reset_accumulators()
            actions["buttons"].extend(self.release_all_buttons())
            actions["mode_changed"] = new_mode
            new_grab = self.needs_grab
            if new_grab != old_grab:
                actions["grab_changed"] = new_grab
            return True
        return False

    def sync_active_keys(self, active_codes: Set[int]) -> dict:
        """Prune stale held keys against kernel active_keys() and commit any unblocked toggle."""
        actions = self._empty_actions()
        stale = self.held_keys - active_codes
        if stale:
            self.held_keys.difference_update(stale)
        if self.pending_mode is not None and not (self.held_keys & _TOGGLE_WAIT_CODES):
            self._commit_pending_mode(actions)
        return actions

    def flush_pending_if_timed_out(self, now: float, timeout: float = 0.65) -> dict:
        """Force-commit a deferred mode toggle if a chord key-up was missed or held too long."""
        actions = self._empty_actions()
        if self.pending_mode is not None and self._pending_since > 0.0 and (now - self._pending_since) >= timeout:
            self.held_keys.difference_update(_TOGGLE_WAIT_CODES)
            self._commit_pending_mode(actions)
        return actions

    def process_key_event(self, code: int, value: int, now: Optional[float] = None) -> dict:
        """Process one Linux evdev key event (value: 0=up, 1=down, 2=repeat).

        Returns an action dict:
          - force_exit: bool
          - mode_changed: Optional[str]    (new MODE_* string if mode switched, else None)
          - grab_changed: Optional[bool]   (True=grab, False=ungrab, None=unchanged)
          - speed_changed: Optional[int]   (new mouse speed 1..10)
          - buttons: List[Tuple[int, int]] [(BTN_*, 0|1), ...]
          - rel_nudge: Tuple[int, int]     immediate 1-step tap nudge
          - wheel_step: int                immediate scroll tick on initial press
        """
        actions = self._empty_actions()

        if value in (1, 2):
            self.held_keys.add(code)
        elif value == 0:
            self.held_keys.discard(code)

        # 1. Universal Emergency Exit Chord: Ctrl + Alt + Q (any press order!)
        if (
            value == 1
            and self.ctrl_held
            and self.alt_held
            and (KEY_Q in self.held_keys)
            and code in (_CTRL_CODES | _ALT_CODES | {KEY_Q})
        ):
            self.force_exit_requested = True
            actions["force_exit"] = True
            actions["buttons"].extend(self.release_all_buttons())
            return actions

        # 2. Mode Switching (deferred until chord keys release to guarantee zero stuck modifiers;
        #    works regardless of key-press order, e.g. Ctrl+Alt+M, Alt+Ctrl+M, or Ctrl+M+Alt!):
        #    - ScrollLock or Ctrl+Alt+M : Toggle TYPING <-> MOUSE
        #    - Esc (Mouse Mode only)    : Return to TYPING
        if value == 1 and self.pending_mode is None:
            ts_now = time.monotonic() if now is None else float(now)
            m_chord = (
                self.ctrl_held
                and self.alt_held
                and (KEY_M in self.held_keys)
                and code in (_CTRL_CODES | _ALT_CODES | {KEY_M})
            )
            if m_chord or code == KEY_SCROLLLOCK:
                self.pending_mode = MODE_TYPING if self.mode != MODE_TYPING else MODE_DESKTOP
                self._pending_since = ts_now
                return actions
            if self.mode == MODE_DESKTOP and code == KEY_ESC:
                self.pending_mode = MODE_TYPING
                self._pending_since = ts_now
                return actions

        # Commit deferred mode change once chord/toggle keys are all released
        if self.pending_mode is not None and not (self.held_keys & _TOGGLE_WAIT_CODES):
            self._commit_pending_mode(actions)
            return actions

        if self.mode == MODE_TYPING or self.pending_mode is not None:
            return actions

        # 3. Mouse button presses / releases (supports click-and-hold dragging)
        if value in (0, 1):
            new_l = bool(self.held_keys & _DESK_LCLICK)
            new_r = bool(self.held_keys & _DESK_RCLICK)
            new_m = bool(self.held_keys & _DESK_MCLICK)
            if new_l != self.btn_left:
                self.btn_left = new_l
                actions["buttons"].append((BTN_LEFT, 1 if new_l else 0))
            if new_r != self.btn_right:
                self.btn_right = new_r
                actions["buttons"].append((BTN_RIGHT, 1 if new_r else 0))
            if new_m != self.btn_middle:
                self.btn_middle = new_m
                actions["buttons"].append((BTN_MIDDLE, 1 if new_m else 0))

        # 4. Live Speed Adjustment (- slower, = faster)
        if value == 1 and code in _SPEED_DN_CODES:
            old_s = self.mouse_speed
            new_s = self.set_speed(old_s - 1)
            if new_s != old_s:
                actions["speed_changed"] = new_s
            return actions
        if value == 1 and code in _SPEED_UP_CODES:
            old_s = self.mouse_speed
            new_s = self.set_speed(old_s + 1)
            if new_s != old_s:
                actions["speed_changed"] = new_s
            return actions

        # 5. Immediate single-tap nudge & scroll tick
        if value == 1:
            _lbl, _base, _mx, tap_step, _sr = MOUSE_SPEED_LEVELS[self.mouse_speed]
            step = 1 if self.shift_held else (max(2, tap_step * 2) if self.ctrl_held else tap_step)
            nx = (-step if code in _DESK_LEFT else (step if code in _DESK_RIGHT else 0))
            ny = (-step if code in _DESK_UP else (step if code in _DESK_DOWN else 0))
            if nx or ny:
                actions["rel_nudge"] = (nx, ny)
            elif code in _DESK_SCROLL_UP:
                actions["wheel_step"] = 1
            elif code in _DESK_SCROLL_DN:
                actions["wheel_step"] = -1

        return actions

    def compute_motion_step(self, dt: float) -> Tuple[int, int, int]:
        """Advance smooth continuous cursor & scroll motion by *dt* seconds.

        Returns (dx, dy, wheel_delta).
        """
        if not self.mouse_mode or self.pending_mode is not None or dt <= 0.0:
            self._move_hold_sec = 0.0
            return 0, 0, 0

        vx = (-1.0 if (self.held_keys & _DESK_LEFT) else 0.0) + (1.0 if (self.held_keys & _DESK_RIGHT) else 0.0)
        vy = (-1.0 if (self.held_keys & _DESK_UP) else 0.0) + (1.0 if (self.held_keys & _DESK_DOWN) else 0.0)
        _lbl, base_spd, max_spd, _tap, base_scroll = MOUSE_SPEED_LEVELS[self.mouse_speed]

        dx = 0
        dy = 0
        if vx != 0.0 or vy != 0.0:
            if vx != 0.0 and vy != 0.0:
                vx *= 0.70710678
                vy *= 0.70710678
            self._move_hold_sec = min(1.0, self._move_hold_sec + dt)
            ramp = min(1.0, self._move_hold_sec / 0.50)
            speed = base_spd + (max_spd - base_spd) * (ramp * ramp)
            if self.shift_held:
                speed *= 0.35
            elif self.ctrl_held:
                speed *= 2.00

            self._acc_x += vx * speed * dt
            self._acc_y += vy * speed * dt
            dx = int(self._acc_x)
            dy = int(self._acc_y)
            self._acc_x -= dx
            self._acc_y -= dy
        else:
            self._move_hold_sec = 0.0
            self._acc_x = 0.0
            self._acc_y = 0.0

        wheel = 0
        s_dir = (
            (1.0 if (self.held_keys & _DESK_SCROLL_UP) else 0.0)
            + (-1.0 if (self.held_keys & _DESK_SCROLL_DN) else 0.0)
        )
        if s_dir != 0.0:
            rate = base_scroll * (1.8 if self.ctrl_held else (0.5 if self.shift_held else 1.0))
            self._scroll_acc += s_dir * rate * dt
            wheel = int(self._scroll_acc)
            self._scroll_acc -= wheel
        else:
            self._scroll_acc = 0.0

        return dx, dy, wheel


def ensure_uinput_available() -> bool:
    """Ensure Linux /dev/uinput is loaded and accessible."""
    if os.path.exists("/dev/uinput"):
        return True
    if hasattr(os, "geteuid") and os.geteuid() == 0 and shutil.which("modprobe"):
        try:
            subprocess.run(["modprobe", "uinput"], capture_output=True, timeout=3)
        except Exception:
            pass
    return os.path.exists("/dev/uinput")


def diagnose_environment() -> dict:
    """Inspect /dev/uinput and /dev/input readiness for Keyboard-Mouse Mode."""
    uinput_exists = os.path.exists("/dev/uinput")
    uinput_writable = bool(uinput_exists and os.access("/dev/uinput", os.W_OK))
    kbd_paths = evdev_keys.discover_keyboard_devices() if _EVDEV_OK else []
    issues: List[str] = []
    if not _EVDEV_OK:
        issues.append("python3-evdev not installed")
    elif not uinput_exists:
        issues.append("/dev/uinput missing (run: sudo modprobe uinput)")
    elif not uinput_writable:
        issues.append("No write access to /dev/uinput (run sudo or add udev rule)")
    if _EVDEV_OK and not kbd_paths:
        issues.append("No readable keyboard in /dev/input (need sudo or 'input' group)")
    return {
        "evdev_ok": _EVDEV_OK,
        "uinput_exists": uinput_exists,
        "uinput_writable": uinput_writable,
        "keyboard_paths": kbd_paths,
        "ready": bool(_EVDEV_OK and uinput_writable and kbd_paths),
        "issues": issues,
    }


class KeyboardMouseController:
    """Background UInput virtual mouse + universal Ctrl+Alt+Q exit controller."""

    def __init__(
        self,
        on_force_exit: Optional[Callable[[], None]] = None,
        mouse_speed: int = DEFAULT_MOUSE_SPEED,
        initial_mode: str = MODE_TYPING,
        on_speed_change: Optional[Callable[[int], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ):
        self._on_force_exit = on_force_exit
        self._on_speed_change = on_speed_change
        self._on_log = on_log
        self._init_speed = max(1, min(10, int(mouse_speed)))
        self._initial_mode = normalize_mode(initial_mode, default=MODE_TYPING)
        self.state = VirtualMouseState(
            mouse_speed=self._init_speed,
            initial_mode=self._initial_mode,
        )
        self._uinput = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._active_uinput = False
        self.last_error: str = ""
        self.open_keyboard_count: int = 0

    def _log(self, msg: str) -> None:
        if self._on_log is not None:
            try:
                self._on_log(f"[kbd-mouse] {msg}")
            except Exception:
                pass

    @property
    def mouse_available(self) -> bool:
        return self._active_uinput

    def _create_virtual_devices(self) -> bool:
        self.last_error = ""
        if not _EVDEV_OK or evdev is None or _ecodes is None:
            self.last_error = "python3-evdev unavailable"
            self._log(self.last_error)
            return False
        if not ensure_uinput_available():
            self.last_error = "/dev/uinput unavailable (modprobe uinput)"
            self._log(self.last_error)
            return False
        if not os.access("/dev/uinput", os.W_OK):
            self.last_error = "No write permission on /dev/uinput (run sudo or add udev rule)"
            self._log(self.last_error)
            return False
        try:
            mouse_caps = {
                _ecodes.EV_REL: [_ecodes.REL_X, _ecodes.REL_Y, _ecodes.REL_WHEEL, _ecodes.REL_HWHEEL],
                _ecodes.EV_KEY: [_ecodes.BTN_LEFT, _ecodes.BTN_RIGHT, _ecodes.BTN_MIDDLE],
            }
            self._uinput = evdev.UInput(
                mouse_caps,
                name="EmberJack Virtual Mouse",
                vendor=0x1209,
                product=0x524A,
                bustype=_ecodes.BUS_USB,
                phys="emberjack/virtual-mouse",
                max_effects=0,
            )
            self._active_uinput = True
        except Exception as exc:
            self.last_error = f"Virtual mouse failed: {exc}"
            self._log(f"virtual mouse UInput failed: {exc}")
            self._uinput = None
            self._active_uinput = False
            return False

        # Ensure systemd-udevd tags ID_INPUT_MOUSE / ID_SEAT before compositor starts
        if shutil.which("udevadm"):
            dev_obj = getattr(self._uinput, "device", None)
            dev_path = getattr(dev_obj, "path", "") if dev_obj is not None else ""
            if dev_path and hasattr(os, "geteuid") and os.geteuid() == 0:
                try:
                    subprocess.run(["udevadm", "trigger", "--action=add", dev_path], capture_output=True, timeout=2)
                except Exception:
                    pass
            try:
                subprocess.run(["udevadm", "settle", "--timeout=2"], capture_output=True, timeout=3)
            except Exception:
                pass
        time.sleep(0.15)
        m_path = getattr(getattr(self._uinput, "device", None), "path", "?")
        self._log(f"uinput ready: mouse={self._active_uinput}({m_path})")
        return self._active_uinput

    def _emit_mouse(self, dx: int = 0, dy: int = 0, wheel: int = 0, buttons: Optional[List[Tuple[int, int]]] = None) -> None:
        ui = self._uinput
        if ui is None:
            return
        wrote = False
        try:
            if dx:
                ui.write(EV_REL, REL_X, int(dx))
                wrote = True
            if dy:
                ui.write(EV_REL, REL_Y, int(dy))
                wrote = True
            if wheel:
                ui.write(EV_REL, REL_WHEEL, int(wheel))
                wrote = True
            if buttons:
                for btn_code, val in buttons:
                    ui.write(EV_KEY, btn_code, int(val))
                    wrote = True
            if wrote:
                ui.syn()
        except OSError:
            pass

    @staticmethod
    def _set_kbd_led(devices: Dict[str, object], on: bool) -> None:
        for dev in list(devices.values()):
            try:
                dev.set_led(LED_SCROLLL, 1 if on else 0)
            except Exception:
                pass

    def _set_grab(self, devices: Dict[str, object], grab: bool) -> None:
        for path, dev in list(devices.items()):
            for attempt in range(2):
                try:
                    if grab:
                        dev.grab()
                    else:
                        dev.ungrab()
                    break
                except OSError as exc:
                    if grab and attempt == 0:
                        time.sleep(0.05)
                        continue
                    self._log(f"{'grab' if grab else 'ungrab'} failed on {path}: {exc}")
                except Exception as exc:
                    self._log(f"{'grab' if grab else 'ungrab'} error on {path}: {exc}")
                    break

    @staticmethod
    def _collect_active_keys(devices: Dict[str, object]) -> Set[int]:
        active: Set[int] = set()
        for dev in list(devices.values()):
            try:
                active.update(int(c) for c in dev.active_keys())
            except Exception:
                pass
        return active

    def _apply_mode_actions(self, act: dict, open_devs: Dict[str, object]) -> None:
        if act["mode_changed"] is not None:
            self._log(f"mode -> {act['mode_changed']} (grab={act['grab_changed']} devs={len(open_devs)})")
            if act["buttons"]:
                self._emit_mouse(buttons=act["buttons"])
            if act["grab_changed"] is True:
                self._set_grab(open_devs, True)
                self._set_kbd_led(open_devs, True)
                # Wake hidden Wayland pointer cleanly (zero net offset)
                self._emit_mouse(dx=6, dy=0)
                time.sleep(0.012)
                self._emit_mouse(dx=-6, dy=0)
            elif act["grab_changed"] is False:
                self._set_kbd_led(open_devs, False)
                self._set_grab(open_devs, False)

    @staticmethod
    def _close_devices(devices: Dict[str, object]) -> None:
        for dev in list(devices.values()):
            try:
                dev.set_led(LED_SCROLLL, 0)
            except Exception:
                pass
            try:
                dev.ungrab()
            except Exception:
                pass
            try:
                dev.close()
            except Exception:
                pass
        devices.clear()

    def _run_loop(self) -> None:
        open_devs: Dict[str, object] = {}
        last_scan = 0.0
        last_nodes: Optional[tuple] = None
        last_tick = time.monotonic()
        exit_fired = False
        logged_no_devs = False

        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if not open_devs or (now - last_scan) >= 4.0:
                    last_scan = now
                    try:
                        cur_nodes = tuple(sorted(n for n in os.listdir("/dev/input") if n.startswith("event")))
                    except OSError:
                        cur_nodes = ()
                    if not open_devs or cur_nodes != last_nodes:
                        last_nodes = cur_nodes
                        desired = set(evdev_keys.discover_keyboard_devices())
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
                                    if self.state.needs_grab:
                                        try:
                                            dev.grab()
                                        except OSError as g_exc:
                                            self._log(f"initial grab failed on {path}: {g_exc}")
                                        try:
                                            dev.set_led(LED_SCROLLL, 1)
                                        except Exception:
                                            pass
                                    open_devs[path] = dev
                                    self._log(f"opened keyboard: {path} ({dev.name})")
                                except Exception as open_exc:
                                    self._log(f"cannot open {path}: {open_exc}")
                                    continue
                        self.open_keyboard_count = len(open_devs)
                        if not open_devs and not logged_no_devs:
                            logged_no_devs = True
                            self._log("no readable physical keyboards found in /dev/input (need sudo or 'input' group)")
                        elif open_devs:
                            logged_no_devs = False

                if not open_devs:
                    self._stop.wait(0.25)
                    last_tick = time.monotonic()
                    continue

                wait_s = 0.010 if (self.state.has_active_motion() or self.state.pending_mode is not None) else 0.020
                fd_map = {dev.fd: (path, dev) for path, dev in open_devs.items()}
                try:
                    readable, _, _ = select.select(list(fd_map.keys()), [], [], wait_s)
                except (OSError, ValueError):
                    self._close_devices(open_devs)
                    self.open_keyboard_count = 0
                    self._stop.wait(0.15)
                    continue

                for fd in readable:
                    entry = fd_map.get(fd)
                    if not entry:
                        continue
                    path, dev = entry
                    try:
                        for event in dev.read():
                            if event.type != EV_KEY:
                                continue
                            act = self.state.process_key_event(event.code, event.value)
                            if act["force_exit"] and not exit_fired:
                                exit_fired = True
                                self._emit_mouse(buttons=act["buttons"] or self.state.release_all_buttons())
                                self._set_grab(open_devs, False)
                                self._set_kbd_led(open_devs, False)
                                if self._on_force_exit is not None:
                                    try:
                                        self._on_force_exit()
                                    except Exception:
                                        pass
                                return
                            if act["mode_changed"] is not None:
                                self._apply_mode_actions(act, open_devs)
                            else:
                                if act["speed_changed"] is not None and self._on_speed_change is not None:
                                    try:
                                        self._on_speed_change(act["speed_changed"])
                                    except Exception:
                                        pass
                                nx, ny = act["rel_nudge"]
                                if nx or ny or act["wheel_step"] or act["buttons"]:
                                    self._emit_mouse(
                                        dx=nx,
                                        dy=ny,
                                        wheel=act["wheel_step"],
                                        buttons=act["buttons"],
                                    )
                    except BlockingIOError:
                        continue
                    except OSError as read_exc:
                        self._log(f"keyboard read error on {path}: {read_exc}")
                        try:
                            dev.close()
                        except Exception:
                            pass
                        open_devs.pop(path, None)
                        self.open_keyboard_count = len(open_devs)
                        sync_act = self.state.sync_active_keys(self._collect_active_keys(open_devs))
                        self._apply_mode_actions(sync_act, open_devs)

                tick_now = time.monotonic()
                if self.state.pending_mode is not None:
                    sync_act = self.state.sync_active_keys(self._collect_active_keys(open_devs))
                    if sync_act["mode_changed"] is not None:
                        self._apply_mode_actions(sync_act, open_devs)
                    else:
                        to_act = self.state.flush_pending_if_timed_out(tick_now, timeout=0.65)
                        if to_act["mode_changed"] is not None:
                            self._apply_mode_actions(to_act, open_devs)

                dt = min(0.05, max(0.0, tick_now - last_tick))
                last_tick = tick_now
                if self.state.mouse_mode:
                    dx, dy, wheel = self.state.compute_motion_step(dt)
                    if dx or dy or wheel:
                        self._emit_mouse(dx=dx, dy=dy, wheel=wheel)
        finally:
            self._emit_mouse(buttons=self.state.release_all_buttons())
            self._close_devices(open_devs)
            self.open_keyboard_count = 0

    def start(self) -> bool:
        """Create virtual mouse and start the background input thread."""
        if self._thread is not None and self._thread.is_alive():
            return self._active_uinput
        self._stop.clear()
        created = self._create_virtual_devices()
        self.state = VirtualMouseState(
            mouse_speed=self._init_speed,
            initial_mode=self._initial_mode,
        )
        if _EVDEV_OK:
            self._thread = threading.Thread(target=self._run_loop, name="rj-kbd-mouse", daemon=True)
            self._thread.start()
        return created

    def stop(self) -> None:
        """Release grabs, release any held virtual buttons, and destroy virtual mouse."""
        self._stop.set()
        t = self._thread
        self._thread = None
        if t is not None and t.is_alive():
            t.join(timeout=0.8)
        ui = self._uinput
        if ui is not None:
            try:
                ui.close()
            except Exception:
                pass
            self._uinput = None
        self._active_uinput = False
