#!/usr/bin/env python3
"""
RaspyJack Virtual Keyboard-Mouse & Universal Kiosk Exit Controller
------------------------------------------------------------------
Runs alongside standalone GUI apps launched from the App Launcher (especially
under headless Wayland `cage` / `xinit`) to provide:

  1. Universal Emergency Exit Hotkey (works inside ANY app, even fullscreen
     apps that ignore Alt+F4 or Ctrl+Q):
       Ctrl + Alt + Q          -> Close GUI app & return to RaspyJack
       Ctrl + Alt + Backspace  -> Close GUI app & return to RaspyJack

  2. Toggleable Virtual Mouse controlled from the Keyboard (via Linux /dev/uinput,
     zero external tools like xdotool/ydotool needed — recognized natively as a
     hardware mouse by Wayland/cage, XWayland, and X11):
       Ctrl + Alt + M  or  ScrollLock  -> Toggle Keyboard Mouse Mode ON / OFF
       Ctrl + Alt + K  or  Esc         -> Switch back to normal Keyboard Typing Mode

     While Mouse Mode is ON (keyboard is temporarily grabbed so keys don't type
     into the app, and the ScrollLock LED lights up):
       Arrows / W A S D / I J K L      -> Move cursor (smooth acceleration)
       Hold Shift                      -> Slow precision movement
       Hold Ctrl                       -> Fast turbo movement
       Space / Enter / Z               -> Left Click (hold to drag!)
       X / Backspace / Menu            -> Right Click
       C                               -> Middle Click
       PageUp / R / Q                  -> Scroll Up
       PageDown / F / E                -> Scroll Down

Uses zero external packages beyond python3-evdev (already required by RaspyJack)
and Linux's built-in /dev/uinput kernel module.
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

# Standard Linux evdev keycodes (match <linux/input-event-codes.h>)
KEY_ESC = 1
KEY_MINUS = 12
KEY_EQUAL = 13
KEY_BACKSPACE = 14
KEY_Q = 16
KEY_W = 17
KEY_E = 18
KEY_R = 19
KEY_I = 23
KEY_LEFTBRACE = 26
KEY_RIGHTBRACE = 27
KEY_ENTER = 28
KEY_LCTRL = 29
KEY_A = 30
KEY_S = 31
KEY_D = 32
KEY_F = 33
KEY_J = 36
KEY_K = 37
KEY_L = 38
KEY_LSHIFT = 42
KEY_Z = 44
KEY_X = 45
KEY_C = 46
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
KEY_COMPOSE = 127
KEY_MENU = 139

# Virtual mouse event codes
EV_KEY = 1
EV_REL = 2
EV_LED = 17
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
_TOGGLE_WAIT_CODES = _CTRL_CODES | _ALT_CODES | {KEY_M, KEY_K, KEY_SCROLLLOCK, KEY_ESC}

_UP_CODES = {KEY_UP, KEY_W, KEY_I}
_DOWN_CODES = {KEY_DOWN, KEY_S, KEY_K}
_LEFT_CODES = {KEY_LEFT, KEY_A, KEY_J}
_RIGHT_CODES = {KEY_RIGHT, KEY_D, KEY_L}
_MOVE_CODES = _UP_CODES | _DOWN_CODES | _LEFT_CODES | _RIGHT_CODES

_LCLICK_CODES = {KEY_SPACE, KEY_ENTER, KEY_KPENTER, KEY_Z}
_RCLICK_CODES = {KEY_X, KEY_BACKSPACE, KEY_MENU, KEY_COMPOSE}
_MCLICK_CODES = {KEY_C}
_SCROLL_UP_CODES = {KEY_PAGEUP, KEY_R, KEY_Q}
_SCROLL_DN_CODES = {KEY_PAGEDOWN, KEY_F, KEY_E}
_SPEED_DN_CODES = {KEY_MINUS, KEY_LEFTBRACE}
_SPEED_UP_CODES = {KEY_EQUAL, KEY_RIGHTBRACE}

# Calibrated keyboard-mouse speed presets (1..10):
# Accounts for libinput/Wayland pointer acceleration so default (level 4) is calm and precise.
# Each entry: (label, base_px_s, max_px_s, tap_step, scroll_rate)
MOUSE_SPEED_LEVELS: Dict[int, Tuple[str, float, float, int, float]] = {
    1:  ("Very Slow",  35.0,  95.0, 1, 3.0),
    2:  ("Slow",       55.0, 150.0, 1, 4.0),
    3:  ("Gentle",     75.0, 210.0, 1, 4.5),
    4:  ("Balanced",   95.0, 270.0, 2, 5.0),
    5:  ("Medium",    125.0, 350.0, 2, 6.0),
    6:  ("Brisk",     160.0, 440.0, 3, 7.0),
    7:  ("Fast",      200.0, 560.0, 3, 8.0),
    8:  ("Very Fast", 250.0, 700.0, 4, 9.0),
    9:  ("Turbo",     310.0, 860.0, 5, 10.0),
    10: ("Max",       380.0, 1040.0, 6, 12.0),
}
DEFAULT_MOUSE_SPEED = 4


def get_speed_label(level: int) -> str:
    lv = max(1, min(10, int(level)))
    return MOUSE_SPEED_LEVELS[lv][0]


class VirtualMouseState:
    """Pure, deterministic state machine for keyboard-driven mouse & exit chords."""

    def __init__(self, mouse_speed: int = DEFAULT_MOUSE_SPEED):
        self.mouse_mode: bool = False
        self.mouse_speed: int = max(1, min(10, int(mouse_speed)))
        self.held_keys: Set[int] = set()
        self.pending_toggle: Optional[bool] = None  # Deferred grab/ungrab until chord keys release
        self.force_exit_requested: bool = False

        # Smooth pointer acceleration & sub-pixel accumulators
        self._move_hold_sec: float = 0.0
        self._acc_x: float = 0.0
        self._acc_y: float = 0.0
        self._scroll_acc: float = 0.0

        # Active virtual mouse buttons
        self.btn_left: bool = False
        self.btn_right: bool = False
        self.btn_middle: bool = False

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
        return bool(self.held_keys & (_MOVE_CODES | _SCROLL_UP_CODES | _SCROLL_DN_CODES))

    def process_key_event(self, code: int, value: int) -> dict:
        """Process a Linux evdev key event (value: 0=up, 1=down, 2=repeat).

        Returns a dict describing immediate actions:
          - force_exit: bool
          - mode_changed: Optional[bool]  (True=entered mouse mode, False=exited)
          - buttons: List[Tuple[int, int]]  [(BTN_*, 0|1), ...]
          - rel_nudge: Tuple[int, int]  immediate 1-step tap nudge for crisp precision
          - wheel_step: int  immediate scroll tick on initial press
        """
        actions: dict = {
            "force_exit": False,
            "mode_changed": None,
            "speed_changed": None,
            "buttons": [],
            "rel_nudge": (0, 0),
            "wheel_step": 0,
        }

        if value == 1:
            self.held_keys.add(code)
        elif value == 0:
            self.held_keys.discard(code)
        elif value == 2:
            self.held_keys.add(code)

        # 1. Universal Emergency Exit Chord: Ctrl + Alt + Q  OR  Ctrl + Alt + Backspace
        if value == 1 and self.ctrl_held and self.alt_held and code in (KEY_Q, KEY_BACKSPACE):
            self.force_exit_requested = True
            actions["force_exit"] = True
            return actions

        # 2. Switch between Mouse Mode & Keyboard Typing Mode:
        #    - Ctrl+Alt+M / ScrollLock: toggle Mouse Mode ON/OFF
        #    - Ctrl+Alt+K: explicitly switch to Keyboard Typing Mode
        #    - Esc (while Mouse Mode is ON): return to Keyboard Typing Mode
        if value == 1:
            if (self.ctrl_held and self.alt_held and code == KEY_M) or code == KEY_SCROLLLOCK:
                self.pending_toggle = not self.mouse_mode
                return actions
            if self.ctrl_held and self.alt_held and code == KEY_K:
                if self.mouse_mode:
                    self.pending_toggle = False
                return actions
            if self.mouse_mode and code == KEY_ESC:
                self.pending_toggle = False
                return actions

        # Commit deferred toggle once chord/toggle keys are all released (prevents stuck modifiers!)
        if self.pending_toggle is not None and not (self.held_keys & _TOGGLE_WAIT_CODES):
            new_mode = self.pending_toggle
            self.pending_toggle = None
            if new_mode != self.mouse_mode:
                self.mouse_mode = new_mode
                self._move_hold_sec = 0.0
                self._acc_x = 0.0
                self._acc_y = 0.0
                self._scroll_acc = 0.0
                if not new_mode:
                    actions["buttons"].extend(self.release_all_buttons())
                actions["mode_changed"] = new_mode
            return actions

        if not self.mouse_mode or self.pending_toggle is not None:
            return actions

        # 3. Mouse button presses/releases (supports click-and-hold dragging!)
        if value in (0, 1):
            new_l = bool(self.held_keys & _LCLICK_CODES)
            new_r = bool(self.held_keys & _RCLICK_CODES)
            new_m = bool(self.held_keys & _MCLICK_CODES)
            if new_l != self.btn_left:
                self.btn_left = new_l
                actions["buttons"].append((BTN_LEFT, 1 if new_l else 0))
            if new_r != self.btn_right:
                self.btn_right = new_r
                actions["buttons"].append((BTN_RIGHT, 1 if new_r else 0))
            if new_m != self.btn_middle:
                self.btn_middle = new_m
                actions["buttons"].append((BTN_MIDDLE, 1 if new_m else 0))

        # 4. Live speed adjustment inside Mouse Mode (- / [ slower, + / = / ] faster)
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

        # 5. Crisp immediate single-tap nudge (scaled by configured mouse_speed!)
        if value == 1:
            _lbl, _base, _mx, tap_step, _sr = MOUSE_SPEED_LEVELS[self.mouse_speed]
            step = 1 if self.shift_held else (max(2, tap_step * 2) if self.ctrl_held else tap_step)
            nx = (-step if code in _LEFT_CODES else (step if code in _RIGHT_CODES else 0))
            ny = (-step if code in _UP_CODES else (step if code in _DOWN_CODES else 0))
            if nx or ny:
                actions["rel_nudge"] = (nx, ny)
            elif code in _SCROLL_UP_CODES and not (self.ctrl_held and self.alt_held):
                actions["wheel_step"] = 1
            elif code in _SCROLL_DN_CODES:
                actions["wheel_step"] = -1

        return actions

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

    def compute_motion_step(self, dt: float) -> Tuple[int, int, int]:
        """Advance smooth continuous pointer & scroll motion by *dt* seconds.

        Returns (dx, dy, wheel_delta).
        """
        if not self.mouse_mode or self.pending_toggle is not None or dt <= 0.0:
            self._move_hold_sec = 0.0
            return 0, 0, 0

        vx = (-1.0 if (self.held_keys & _LEFT_CODES) else 0.0) + (1.0 if (self.held_keys & _RIGHT_CODES) else 0.0)
        vy = (-1.0 if (self.held_keys & _UP_CODES) else 0.0) + (1.0 if (self.held_keys & _DOWN_CODES) else 0.0)

        _lbl, base_spd, max_spd, _tap, base_scroll = MOUSE_SPEED_LEVELS[self.mouse_speed]
        dx = 0
        dy = 0
        if vx != 0.0 or vy != 0.0:
            # Normalize diagonal movement so diagonals aren't 1.41x faster
            if vx != 0.0 and vy != 0.0:
                vx *= 0.70710678
                vy *= 0.70710678
            self._move_hold_sec = min(1.0, self._move_hold_sec + dt)
            # Gentle smooth acceleration over 0.55s (accounts for libinput acceleration)
            ramp = min(1.0, self._move_hold_sec / 0.55)
            speed = base_spd + (max_spd - base_spd) * (ramp * ramp)
            if self.shift_held:
                speed *= 0.35  # Fine pixel-level precision
            elif self.ctrl_held:
                speed *= 2.00  # Fast across-screen glide
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
            (1.0 if (self.held_keys & _SCROLL_UP_CODES and not (self.ctrl_held and self.alt_held)) else 0.0)
            + (-1.0 if (self.held_keys & _SCROLL_DN_CODES) else 0.0)
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


class KeyboardMouseController:
    """Background uinput virtual mouse + universal kiosk exit controller."""

    def __init__(
        self,
        on_force_exit: Optional[Callable[[], None]] = None,
        mouse_speed: int = DEFAULT_MOUSE_SPEED,
        on_speed_change: Optional[Callable[[int], None]] = None,
    ):
        self._on_force_exit = on_force_exit
        self._on_speed_change = on_speed_change
        self._init_speed = max(1, min(10, int(mouse_speed)))
        self.state = VirtualMouseState(mouse_speed=self._init_speed)
        self._uinput = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._active_uinput = False

    @property
    def mouse_available(self) -> bool:
        return self._active_uinput

    def _create_virtual_mouse(self) -> bool:
        if not _EVDEV_OK or evdev is None or _ecodes is None:
            return False
        if not ensure_uinput_available():
            return False
        try:
            caps = {
                _ecodes.EV_REL: [_ecodes.REL_X, _ecodes.REL_Y, _ecodes.REL_WHEEL, _ecodes.REL_HWHEEL],
                _ecodes.EV_KEY: [_ecodes.BTN_LEFT, _ecodes.BTN_RIGHT, _ecodes.BTN_MIDDLE],
            }
            self._uinput = evdev.UInput(
                caps,
                name="RaspyJack Virtual Mouse",
                vendor=0x1209,
                product=0x524A,
                bustype=_ecodes.BUS_USB,
            )
            self._active_uinput = True
            return True
        except Exception:
            self._uinput = None
            self._active_uinput = False
            return False

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

    @staticmethod
    def _set_grab(devices: Dict[str, object], grab: bool) -> None:
        for dev in list(devices.values()):
            try:
                if grab:
                    dev.grab()
                else:
                    dev.ungrab()
            except Exception:
                pass

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
                                    if self.state.mouse_mode:
                                        try:
                                            dev.grab()
                                        except OSError:
                                            pass
                                    open_devs[path] = dev
                                except Exception:
                                    continue

                if not open_devs:
                    self._stop.wait(0.25)
                    last_tick = time.monotonic()
                    continue

                wait_s = 0.016 if self.state.has_active_motion() else 0.12
                fd_map = {dev.fd: (path, dev) for path, dev in open_devs.items()}
                try:
                    readable, _, _ = select.select(list(fd_map.keys()), [], [], wait_s)
                except (OSError, ValueError):
                    self._close_devices(open_devs)
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
                                self._emit_mouse(buttons=self.state.release_all_buttons())
                                self._set_grab(open_devs, False)
                                self._set_kbd_led(open_devs, False)
                                if self._on_force_exit is not None:
                                    try:
                                        self._on_force_exit()
                                    except Exception:
                                        pass
                                return
                            if act["mode_changed"] is True:
                                self._set_grab(open_devs, True)
                                self._set_kbd_led(open_devs, True)
                                # Gentle cursor wiggle wakes up hidden Wayland/cage pointer immediately
                                self._emit_mouse(dx=8, dy=0)
                                time.sleep(0.015)
                                self._emit_mouse(dx=-8, dy=0)
                            elif act["mode_changed"] is False:
                                if act["buttons"]:
                                    self._emit_mouse(buttons=act["buttons"])
                                self._set_kbd_led(open_devs, False)
                                self._set_grab(open_devs, False)
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
                    except (OSError, BlockingIOError):
                        try:
                            dev.close()
                        except Exception:
                            pass
                        open_devs.pop(path, None)

                tick_now = time.monotonic()
                dt = min(0.08, max(0.0, tick_now - last_tick))
                last_tick = tick_now
                if self.state.mouse_mode:
                    dx, dy, wheel = self.state.compute_motion_step(dt)
                    if dx or dy or wheel:
                        self._emit_mouse(dx=dx, dy=dy, wheel=wheel)
        finally:
            self._emit_mouse(buttons=self.state.release_all_buttons())
            self._close_devices(open_devs)

    def start(self) -> bool:
        """Create the virtual mouse device and start the background input thread."""
        if self._thread is not None and self._thread.is_alive():
            return self._active_uinput
        self._stop.clear()
        self.state = VirtualMouseState(mouse_speed=self._init_speed)
        created = self._create_virtual_mouse()
        if created:
            # Give udev/libinput a brief moment to register the new pointer before cage launches
            time.sleep(0.15)
        if _EVDEV_OK:
            self._thread = threading.Thread(target=self._run_loop, name="rj-kbd-mouse", daemon=True)
            self._thread.start()
        return created

    def stop(self) -> None:
        """Release grabs, close keyboard handles, and destroy the virtual mouse device."""
        self._stop.set()
        t = self._thread
        self._thread = None
        if t is not None and t.is_alive():
            t.join(timeout=0.8)
        if self._uinput is not None:
            try:
                self._uinput.close()
            except Exception:
                pass
            self._uinput = None
        self._active_uinput = False
