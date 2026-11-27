#!/usr/bin/env python3
"""
EmberJack Virtual Keyboard-Mouse, Dual-Hand Gaming & Kiosk Exit Controller
--------------------------------------------------------------------------
Runs alongside standalone GUI apps and games launched from the App Launcher
(Wayland labwc/cage/gamescope/weston and X11 xinit) to provide:

  1. Universal Emergency Exit Hotkey (works inside ANY fullscreen app/game):
       Ctrl + Alt + Q           -> Force close app & return to EmberJack

  2. Clean 3-Mode Input Engine (zero external tools; pure Linux /dev/uinput):
       ScrollLock / Ctrl+Alt+M  -> Toggle Typing <-> Preferred Pointer Mode
       Ctrl + Alt + G           -> Toggle Typing <-> Dual-Hand Game Mode

     Mode 1 — TYPING (Default on launch for ALL apps):
       Keyboard is ungrabbed — 100% native typing with zero interference.

     Mode 2 — DESKTOP (Clean cursor control, max 1-2 keys/action):
       Arrows                   -> Move cursor (LShift=slow, LCtrl=fast)
       Space / Enter            -> Left Click (hold to drag)
       Backspace                -> Right Click
       Tab                      -> Middle Click
       PgUp / PgDn  (or [ / ])  -> Scroll Up / Down
       - / =                    -> Cursor Speed Down / Up (1-10)
       Esc                      -> Return to Typing Mode

     Mode 3 — GAME (Dual-Hand Simultaneous Walk + 125 Hz 3D Camera Look!):
       Left hand (WASD, Space, LShift, LCtrl, E, Q, R, F, 1-9, Esc, Tab...)
       passes straight through a virtual passthrough keyboard at <1 ms latency,
       while ONLY the right-hand cluster drives smooth linear 3D mouse look:

       Preset A — Arrows (Default):
         Arrows                 -> 125 Hz Linear 3D Camera Look / Menu Cursor
         Enter                  -> Left Click (Mine / Attack, holdable)
         RShift                 -> Right Click (Place / Use, holdable)
         [ / ]                  -> Scroll Wheel (Hotbar cycle)
         - / =                  -> Look Sensitivity Down / Up (1-10)

       Preset B — IJKL (Compact / 60% / CardputerZero):
         I / J / K / L          -> 125 Hz Linear 3D Camera Look
         U                      -> Left Click (Mine / Attack, holdable)
         O                      -> Right Click (Place / Use, holdable)
         [ / ]                  -> Scroll Wheel (Hotbar cycle)
         - / =                  -> Look Sensitivity Down / Up (1-10)

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
MODE_GAME = "game"
VALID_MODES = (MODE_TYPING, MODE_DESKTOP, MODE_GAME)

# Game-mode right-hand presets
LAYOUT_ARROWS = "arrows"
LAYOUT_IJKL = "ijkl"
VALID_GAME_LAYOUTS = (LAYOUT_ARROWS, LAYOUT_IJKL)

GAME_LAYOUT_LABELS: Dict[str, str] = {
    LAYOUT_ARROWS: "Arrows + Enter / RShift",
    LAYOUT_IJKL: "IJKL + U / O (Compact)",
}

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
KEY_U = 22
KEY_I = 23
KEY_O = 24
KEY_LEFTBRACE = 26
KEY_RIGHTBRACE = 27
KEY_ENTER = 28
KEY_LCTRL = 29
KEY_A = 30
KEY_S = 31
KEY_D = 32
KEY_F = 33
KEY_G = 34
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

# Virtual input event codes
EV_KEY = 1
EV_REL = 2
EV_LED = 17
EV_REP = 20
LED_NUML = 0
LED_CAPSL = 1
LED_SCROLLL = 2
REL_X = 0
REL_Y = 1
REL_WHEEL = 8
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112

# Clean standard Linux PC keyboard keycodes for Virtual Passthrough Keyboard:
# Deliberately excludes KEY_POWER (116), KEY_SLEEP (142), KEY_SUSPEND (205),
# KEY_RFKILL (247), and undefined holes (84, 195..199) so systemd-udevd/libinput
# always classifies this device cleanly as ID_INPUT_KEYBOARD=1.
STD_KEYBOARD_KEYCODES: Tuple[int, ...] = tuple(
    list(range(1, 84))       # ESC .. KPDOT (all letters, digits, symbols, arrows/numpad base, F1-F10, Shift/Ctrl/Alt)
    + [85, 86, 87, 88]       # ZENKAKU, 102ND (<>), F11, F12
    + list(range(96, 112))   # KPENTER, RCTRL, KPSLASH, SYSRQ, RALT, LINEFEED, HOME, UP, PGUP, LEFT, RIGHT, END, DOWN, PGDN, INS, DEL
    + [119]                  # PAUSE
    + [125, 126, 127]        # LEFTMETA, RIGHTMETA, COMPOSE
    + list(range(183, 195))  # F13 .. F24
)
_STD_KBD_CODE_SET: Set[int] = set(STD_KEYBOARD_KEYCODES)

_CTRL_CODES = {KEY_LCTRL, KEY_RCTRL}
_ALT_CODES = {KEY_LALT, KEY_RALT}
_SHIFT_CODES = {KEY_LSHIFT, KEY_RSHIFT}
_TOGGLE_WAIT_CODES = _CTRL_CODES | _ALT_CODES | {KEY_M, KEY_G, KEY_SCROLLLOCK, KEY_ESC}

# Desktop Mode bindings (clean, max 1-2 keys per action)
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

# Game Mode right-hand clusters (all other keys pass straight through to the game!)
_GAME_CLUSTERS: Dict[str, Dict[str, Set[int]]] = {
    LAYOUT_ARROWS: {
        "up": {KEY_UP},
        "down": {KEY_DOWN},
        "left": {KEY_LEFT},
        "right": {KEY_RIGHT},
        "lclick": {KEY_ENTER, KEY_KPENTER},
        "rclick": {KEY_RSHIFT},
        "mclick": set(),
        "scroll_up": {KEY_LEFTBRACE},
        "scroll_dn": {KEY_RIGHTBRACE},
        "speed_dn": {KEY_MINUS},
        "speed_up": {KEY_EQUAL},
    },
    LAYOUT_IJKL: {
        "up": {KEY_I},
        "down": {KEY_K},
        "left": {KEY_J},
        "right": {KEY_L},
        "lclick": {KEY_U},
        "rclick": {KEY_O},
        "mclick": set(),
        "scroll_up": {KEY_LEFTBRACE},
        "scroll_dn": {KEY_RIGHTBRACE},
        "speed_dn": {KEY_MINUS},
        "speed_up": {KEY_EQUAL},
    },
}

# Desktop pointer speed presets (1..10): (label, base_px_s, max_px_s, tap_step, scroll_rate)
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

# Linear 3D Game camera sensitivity presets (1..10): (label, px_per_sec, tap_nudge, scroll_rate)
GAME_SENSITIVITY_LEVELS: Dict[int, Tuple[str, float, int, float]] = {
    1:  ("Ultra Fine",  160.0, 1,  5.0),
    2:  ("Very Slow",   240.0, 2,  6.0),
    3:  ("Slow",        330.0, 2,  6.5),
    4:  ("Smooth",      430.0, 3,  7.0),
    5:  ("Balanced",    540.0, 4,  8.0),
    6:  ("Snappy",      680.0, 5,  8.5),
    7:  ("Fast",        840.0, 6,  9.0),
    8:  ("Very Fast",  1020.0, 7, 10.0),
    9:  ("High",       1240.0, 8, 11.0),
    10: ("Twitch",     1500.0, 10, 12.0),
}
DEFAULT_GAME_SENSITIVITY = 5


def normalize_mode(mode: str, default: str = MODE_TYPING) -> str:
    m = str(mode or "").strip().lower()
    return m if m in VALID_MODES else default


def normalize_game_layout(layout: str, default: str = LAYOUT_ARROWS) -> str:
    l = str(layout or "").strip().lower()
    return l if l in VALID_GAME_LAYOUTS else default


def get_speed_label(level: int) -> str:
    lv = max(1, min(10, int(level)))
    return MOUSE_SPEED_LEVELS[lv][0]


def get_game_sensitivity_label(level: int) -> str:
    lv = max(1, min(10, int(level)))
    return GAME_SENSITIVITY_LEVELS[lv][0]


class VirtualMouseState:
    """Pure, deterministic 3-mode state machine (Typing, Desktop Mouse, Dual-Hand Game)."""

    def __init__(
        self,
        mouse_speed: int = DEFAULT_MOUSE_SPEED,
        game_sensitivity: int = DEFAULT_GAME_SENSITIVITY,
        preferred_mode: str = MODE_DESKTOP,
        initial_mode: str = MODE_TYPING,
        game_layout: str = LAYOUT_ARROWS,
        allow_game_mode: bool = True,
    ):
        self.allow_game_mode: bool = bool(allow_game_mode)
        self.preferred_mode: str = (
            preferred_mode if preferred_mode in (MODE_DESKTOP, MODE_GAME) else MODE_DESKTOP
        )
        if not self.allow_game_mode and self.preferred_mode == MODE_GAME:
            self.preferred_mode = MODE_DESKTOP
        self.mode: str = normalize_mode(initial_mode, default=MODE_TYPING)
        if not self.allow_game_mode and self.mode == MODE_GAME:
            self.mode = MODE_TYPING
        self.game_layout: str = normalize_game_layout(game_layout, default=LAYOUT_ARROWS)
        self.mouse_speed: int = max(1, min(10, int(mouse_speed)))
        self.game_sensitivity: int = max(1, min(10, int(game_sensitivity)))

        self.held_keys: Set[int] = set()
        self.forwarded_keys: Set[int] = set()  # Keys currently held on virtual passthrough keyboard
        self.pending_mode: Optional[str] = None  # Deferred grab/ungrab until chord keys release
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
        """True whenever virtual pointer interception (DESKTOP or GAME) is active."""
        return self.mode != MODE_TYPING

    @property
    def is_game_mode(self) -> bool:
        return self.mode == MODE_GAME

    @property
    def is_desktop_mode(self) -> bool:
        return self.mode == MODE_DESKTOP

    @property
    def needs_grab(self) -> bool:
        return self.mode in (MODE_DESKTOP, MODE_GAME)

    def set_speed(self, level: int) -> int:
        self.mouse_speed = max(1, min(10, int(level)))
        return self.mouse_speed

    def set_game_sensitivity(self, level: int) -> int:
        self.game_sensitivity = max(1, min(10, int(level)))
        return self.game_sensitivity

    def _active_cluster(self) -> Dict[str, Set[int]]:
        if self.mode == MODE_GAME:
            return _GAME_CLUSTERS[self.game_layout]
        return {
            "up": _DESK_UP,
            "down": _DESK_DOWN,
            "left": _DESK_LEFT,
            "right": _DESK_RIGHT,
            "lclick": _DESK_LCLICK,
            "rclick": _DESK_RCLICK,
            "mclick": _DESK_MCLICK,
            "scroll_up": _DESK_SCROLL_UP,
            "scroll_dn": _DESK_SCROLL_DN,
            "speed_dn": _SPEED_DN_CODES,
            "speed_up": _SPEED_UP_CODES,
        }

    def _intercepted_game_keys(self) -> Set[int]:
        cl = _GAME_CLUSTERS[self.game_layout]
        return (
            cl["up"] | cl["down"] | cl["left"] | cl["right"]
            | cl["lclick"] | cl["rclick"] | cl["mclick"]
            | cl["scroll_up"] | cl["scroll_dn"]
            | cl["speed_dn"] | cl["speed_up"]
        )

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
        cl = self._active_cluster()
        move_scroll = cl["up"] | cl["down"] | cl["left"] | cl["right"] | cl["scroll_up"] | cl["scroll_dn"]
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

    def release_all_passthrough_keys(self) -> List[Tuple[int, int]]:
        if not self.forwarded_keys:
            return []
        released = [(code, 0) for code in sorted(self.forwarded_keys)]
        self.forwarded_keys.clear()
        return released

    def _reset_accumulators(self) -> None:
        self._move_hold_sec = 0.0
        self._acc_x = 0.0
        self._acc_y = 0.0
        self._scroll_acc = 0.0

    def process_key_event(self, code: int, value: int) -> dict:
        """Process one Linux evdev key event (value: 0=up, 1=down, 2=repeat).

        Returns an action dict:
          - force_exit: bool
          - mode_changed: Optional[str]        (new MODE_* string if mode switched, else None)
          - grab_changed: Optional[bool]       (True=grab, False=ungrab, None=unchanged)
          - speed_changed: Optional[int]       (new desktop speed 1..10)
          - sensitivity_changed: Optional[int] (new game sensitivity 1..10)
          - buttons: List[Tuple[int, int]]     [(BTN_*, 0|1), ...]
          - passthrough_keys: List[Tuple[int, int]] [(evdev_code, 0|1|2), ...] for virtual kbd
          - rel_nudge: Tuple[int, int]         immediate 1-step tap nudge
          - wheel_step: int                    immediate scroll tick on initial press
        """
        actions: dict = {
            "force_exit": False,
            "mode_changed": None,
            "grab_changed": None,
            "speed_changed": None,
            "sensitivity_changed": None,
            "buttons": [],
            "passthrough_keys": [],
            "rel_nudge": (0, 0),
            "wheel_step": 0,
        }

        if value in (1, 2):
            self.held_keys.add(code)
        elif value == 0:
            self.held_keys.discard(code)

        # 1. Universal Emergency Exit Chord: Ctrl + Alt + Q
        if value == 1 and self.ctrl_held and self.alt_held and code == KEY_Q:
            self.force_exit_requested = True
            actions["force_exit"] = True
            actions["buttons"].extend(self.release_all_buttons())
            actions["passthrough_keys"].extend(self.release_all_passthrough_keys())
            return actions

        # 2. Mode Switching (deferred until chord keys release to guarantee zero stuck modifiers!):
        #    - ScrollLock or Ctrl+Alt+M : Toggle TYPING <-> preferred_mode (DESKTOP or GAME)
        #    - Ctrl+Alt+G               : Toggle TYPING <-> GAME (or switch DESKTOP -> GAME)
        #    - Esc (DESKTOP mode only)  : Return to TYPING (never intercepts Esc in GAME mode!)
        if value == 1:
            if (self.ctrl_held and self.alt_held and code == KEY_M) or code == KEY_SCROLLLOCK:
                self.pending_mode = MODE_TYPING if self.mode != MODE_TYPING else self.preferred_mode
                actions["passthrough_keys"].extend(self.release_all_passthrough_keys())
                return actions
            if self.ctrl_held and self.alt_held and code == KEY_G:
                if self.mode == MODE_GAME:
                    self.pending_mode = MODE_TYPING
                elif self.allow_game_mode:
                    self.pending_mode = MODE_GAME
                actions["passthrough_keys"].extend(self.release_all_passthrough_keys())
                return actions
            if self.mode == MODE_DESKTOP and code == KEY_ESC:
                self.pending_mode = MODE_TYPING
                return actions

        # Commit deferred mode change once chord/toggle keys are all released
        if self.pending_mode is not None and not (self.held_keys & _TOGGLE_WAIT_CODES):
            old_mode = self.mode
            old_grab = self.needs_grab
            new_mode = self.pending_mode
            self.pending_mode = None
            if new_mode != old_mode:
                self.mode = new_mode
                self._reset_accumulators()
                actions["buttons"].extend(self.release_all_buttons())
                actions["passthrough_keys"].extend(self.release_all_passthrough_keys())
                actions["mode_changed"] = new_mode
                new_grab = self.needs_grab
                if new_grab != old_grab:
                    actions["grab_changed"] = new_grab
            return actions

        if self.mode == MODE_TYPING or self.pending_mode is not None:
            return actions

        # 3. In GAME mode, forward ALL non-mouse-cluster keys immediately via Virtual Keyboard!
        #    (Only press=1 / release=0 are emitted; EV_REP on the virtual keyboard lets the
        #     Linux kernel synthesize clean native key-repeat without double-repeating!)
        if self.mode == MODE_GAME:
            intercepted = self._intercepted_game_keys()
            if code not in intercepted:
                if value == 1:
                    self.forwarded_keys.add(code)
                    actions["passthrough_keys"].append((code, 1))
                elif value == 0:
                    if code in self.forwarded_keys:
                        self.forwarded_keys.discard(code)
                        actions["passthrough_keys"].append((code, 0))
                return actions

        cl = self._active_cluster()

        # 4. Mouse button presses / releases (supports click-and-hold dragging / mining!)
        if value in (0, 1):
            new_l = bool(self.held_keys & cl["lclick"])
            new_r = bool(self.held_keys & cl["rclick"])
            new_m = bool(self.held_keys & cl["mclick"])
            if new_l != self.btn_left:
                self.btn_left = new_l
                actions["buttons"].append((BTN_LEFT, 1 if new_l else 0))
            if new_r != self.btn_right:
                self.btn_right = new_r
                actions["buttons"].append((BTN_RIGHT, 1 if new_r else 0))
            if new_m != self.btn_middle:
                self.btn_middle = new_m
                actions["buttons"].append((BTN_MIDDLE, 1 if new_m else 0))

        # 5. Live Speed / Sensitivity Adjustment (- slower, = faster)
        if value == 1 and code in cl["speed_dn"]:
            if self.mode == MODE_GAME:
                old_s = self.game_sensitivity
                new_s = self.set_game_sensitivity(old_s - 1)
                if new_s != old_s:
                    actions["sensitivity_changed"] = new_s
            else:
                old_s = self.mouse_speed
                new_s = self.set_speed(old_s - 1)
                if new_s != old_s:
                    actions["speed_changed"] = new_s
            return actions
        if value == 1 and code in cl["speed_up"]:
            if self.mode == MODE_GAME:
                old_s = self.game_sensitivity
                new_s = self.set_game_sensitivity(old_s + 1)
                if new_s != old_s:
                    actions["sensitivity_changed"] = new_s
            else:
                old_s = self.mouse_speed
                new_s = self.set_speed(old_s + 1)
                if new_s != old_s:
                    actions["speed_changed"] = new_s
            return actions

        # 6. Immediate single-tap nudge & scroll tick
        if value == 1:
            if self.mode == MODE_GAME:
                _lbl, _pxs, tap_step, _sr = GAME_SENSITIVITY_LEVELS[self.game_sensitivity]
                step = tap_step
            else:
                _lbl, _base, _mx, tap_step, _sr = MOUSE_SPEED_LEVELS[self.mouse_speed]
                step = 1 if self.shift_held else (max(2, tap_step * 2) if self.ctrl_held else tap_step)
            nx = (-step if code in cl["left"] else (step if code in cl["right"] else 0))
            ny = (-step if code in cl["up"] else (step if code in cl["down"] else 0))
            if nx or ny:
                actions["rel_nudge"] = (nx, ny)
            elif code in cl["scroll_up"]:
                actions["wheel_step"] = 1
            elif code in cl["scroll_dn"]:
                actions["wheel_step"] = -1

        return actions

    def compute_motion_step(self, dt: float) -> Tuple[int, int, int]:
        """Advance smooth continuous pointer/camera & scroll motion by *dt* seconds.

        Returns (dx, dy, wheel_delta).
        """
        if not self.mouse_mode or self.pending_mode is not None or dt <= 0.0:
            self._move_hold_sec = 0.0
            return 0, 0, 0

        cl = self._active_cluster()
        vx = (-1.0 if (self.held_keys & cl["left"]) else 0.0) + (1.0 if (self.held_keys & cl["right"]) else 0.0)
        vy = (-1.0 if (self.held_keys & cl["up"]) else 0.0) + (1.0 if (self.held_keys & cl["down"]) else 0.0)

        dx = 0
        dy = 0
        if vx != 0.0 or vy != 0.0:
            # Normalize diagonal vector so diagonals are never 1.41x faster
            if vx != 0.0 and vy != 0.0:
                vx *= 0.70710678
                vy *= 0.70710678

            if self.mode == MODE_GAME:
                # Zero-lag linear constant angular velocity for crisp 3D aiming!
                _lbl, speed, _tap, base_scroll = GAME_SENSITIVITY_LEVELS[self.game_sensitivity]
            else:
                _lbl, base_spd, max_spd, _tap, base_scroll = MOUSE_SPEED_LEVELS[self.mouse_speed]
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
            base_scroll = (
                GAME_SENSITIVITY_LEVELS[self.game_sensitivity][3]
                if self.mode == MODE_GAME
                else MOUSE_SPEED_LEVELS[self.mouse_speed][4]
            )

        wheel = 0
        s_dir = (
            (1.0 if (self.held_keys & cl["scroll_up"]) else 0.0)
            + (-1.0 if (self.held_keys & cl["scroll_dn"]) else 0.0)
        )
        if s_dir != 0.0:
            if self.mode == MODE_DESKTOP:
                rate = base_scroll * (1.8 if self.ctrl_held else (0.5 if self.shift_held else 1.0))
            else:
                rate = base_scroll
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
    """Background dual-UInput virtual mouse + passthrough keyboard + kiosk exit controller."""

    def __init__(
        self,
        on_force_exit: Optional[Callable[[], None]] = None,
        mouse_speed: int = DEFAULT_MOUSE_SPEED,
        game_sensitivity: int = DEFAULT_GAME_SENSITIVITY,
        preferred_mode: str = MODE_DESKTOP,
        initial_mode: str = MODE_TYPING,
        game_layout: str = LAYOUT_ARROWS,
        on_speed_change: Optional[Callable[[int], None]] = None,
        on_sensitivity_change: Optional[Callable[[int], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ):
        self._on_force_exit = on_force_exit
        self._on_speed_change = on_speed_change
        self._on_sensitivity_change = on_sensitivity_change
        self._on_log = on_log
        self._init_speed = max(1, min(10, int(mouse_speed)))
        self._init_sens = max(1, min(10, int(game_sensitivity)))
        self._preferred_mode = preferred_mode if preferred_mode in (MODE_DESKTOP, MODE_GAME) else MODE_DESKTOP
        self._initial_mode = normalize_mode(initial_mode, default=MODE_TYPING)
        self._game_layout = normalize_game_layout(game_layout, default=LAYOUT_ARROWS)
        self.state = VirtualMouseState(
            mouse_speed=self._init_speed,
            game_sensitivity=self._init_sens,
            preferred_mode=self._preferred_mode,
            initial_mode=self._initial_mode,
            game_layout=self._game_layout,
        )
        self._uinput = None       # Virtual Mouse UInput
        self._kbd_uinput = None   # Virtual Passthrough Keyboard UInput (for GAME mode)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._active_uinput = False
        self._active_kbd_uinput = False

    def _log(self, msg: str) -> None:
        if self._on_log is not None:
            try:
                self._on_log(f"[kbd-mouse] {msg}")
            except Exception:
                pass

    @property
    def mouse_available(self) -> bool:
        return self._active_uinput

    @property
    def passthrough_keyboard_available(self) -> bool:
        return self._active_kbd_uinput

    def _create_virtual_devices(self) -> bool:
        if not _EVDEV_OK or evdev is None or _ecodes is None:
            self._log("python3-evdev unavailable")
            return False
        if not ensure_uinput_available():
            self._log("/dev/uinput unavailable")
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
            )
            self._active_uinput = True
        except Exception as exc:
            self._log(f"virtual mouse UInput failed: {exc}")
            self._uinput = None
            self._active_uinput = False
            return False

        # Create separate Virtual Passthrough Keyboard for simultaneous WASD + Camera Game Mode
        # Uses clean standard PC keyboard codes + EV_REP + EV_LED + unique phys so udev/libinput
        # reliably tags ID_INPUT=1 + ID_INPUT_KEYBOARD=1 for Wayland (labwc/cage/gamescope) & X11.
        try:
            kbd_caps = {
                _ecodes.EV_KEY: list(STD_KEYBOARD_KEYCODES),
                _ecodes.EV_LED: [LED_NUML, LED_CAPSL, LED_SCROLLL],
                _ecodes.EV_REP: [],
            }
            self._kbd_uinput = evdev.UInput(
                kbd_caps,
                name="EmberJack Virtual Keyboard",
                vendor=0x1209,
                product=0x524B,
                bustype=_ecodes.BUS_USB,
                phys="emberjack/virtual-keyboard",
            )
            self._active_kbd_uinput = True
        except Exception as exc:
            self._log(f"virtual keyboard UInput failed (falling back without EV_LED/EV_REP): {exc}")
            try:
                self._kbd_uinput = evdev.UInput(
                    {_ecodes.EV_KEY: list(STD_KEYBOARD_KEYCODES)},
                    name="EmberJack Virtual Keyboard",
                    vendor=0x1209,
                    product=0x524B,
                    bustype=_ecodes.BUS_USB,
                    phys="emberjack/virtual-keyboard",
                )
                self._active_kbd_uinput = True
            except Exception as exc2:
                self._log(f"virtual keyboard UInput fallback failed: {exc2}")
                self._kbd_uinput = None
                self._active_kbd_uinput = False

        # Wait for systemd-udevd to finish tagging ID_INPUT_MOUSE / ID_INPUT_KEYBOARD & group perms
        # BEFORE launching labwc / cage / gamescope / xinit so libinput discovers both virtual devices!
        if shutil.which("udevadm"):
            try:
                subprocess.run(["udevadm", "settle", "--timeout=2"], capture_output=True, timeout=3)
            except Exception:
                pass
        time.sleep(0.20)
        self._log(
            f"uinput ready: mouse={self._active_uinput} kbd={self._active_kbd_uinput} "
            f"pref={self._preferred_mode} layout={self._game_layout}"
        )
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

    def _emit_passthrough_keys(self, keys: List[Tuple[int, int]]) -> None:
        kui = self._kbd_uinput
        if kui is None or not keys:
            return
        try:
            wrote = False
            for code, val in keys:
                if code in _STD_KBD_CODE_SET:
                    kui.write(EV_KEY, int(code), int(val))
                    wrote = True
            if wrote:
                kui.syn()
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
                                    if self.state.needs_grab:
                                        try:
                                            dev.grab()
                                        except OSError:
                                            pass
                                        try:
                                            dev.set_led(LED_SCROLLL, 1)
                                        except Exception:
                                            pass
                                    open_devs[path] = dev
                                except Exception:
                                    continue

                if not open_devs:
                    self._stop.wait(0.25)
                    last_tick = time.monotonic()
                    continue

                # 125 Hz (8 ms) during active cursor/camera motion; snappy 20 ms idle wake-up
                wait_s = 0.008 if self.state.has_active_motion() else 0.020
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
                            if act["passthrough_keys"]:
                                self._emit_passthrough_keys(act["passthrough_keys"])
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
                                self._log(f"mode -> {act['mode_changed']} (grab={act['grab_changed']} devs={len(open_devs)})")
                                if act["buttons"]:
                                    self._emit_mouse(buttons=act["buttons"])
                                if act["grab_changed"] is True:
                                    self._set_grab(open_devs, True)
                                    self._set_kbd_led(open_devs, True)
                                    if act["mode_changed"] == MODE_DESKTOP:
                                        # Wake hidden Wayland cursor when entering Desktop Mouse Mode
                                        self._emit_mouse(dx=6, dy=0)
                                        time.sleep(0.012)
                                        self._emit_mouse(dx=-6, dy=0)
                                elif act["grab_changed"] is False:
                                    self._set_kbd_led(open_devs, False)
                                    self._set_grab(open_devs, False)
                            else:
                                if act["speed_changed"] is not None and self._on_speed_change is not None:
                                    try:
                                        self._on_speed_change(act["speed_changed"])
                                    except Exception:
                                        pass
                                if act["sensitivity_changed"] is not None and self._on_sensitivity_change is not None:
                                    try:
                                        self._on_sensitivity_change(act["sensitivity_changed"])
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
                dt = min(0.05, max(0.0, tick_now - last_tick))
                last_tick = tick_now
                if self.state.mouse_mode:
                    dx, dy, wheel = self.state.compute_motion_step(dt)
                    if dx or dy or wheel:
                        self._emit_mouse(dx=dx, dy=dy, wheel=wheel)
        finally:
            self._emit_passthrough_keys(self.state.release_all_passthrough_keys())
            self._emit_mouse(buttons=self.state.release_all_buttons())
            self._close_devices(open_devs)

    def start(self) -> bool:
        """Create virtual mouse + passthrough keyboard and start the 125 Hz input thread."""
        if self._thread is not None and self._thread.is_alive():
            return self._active_uinput
        self._stop.clear()
        created = self._create_virtual_devices()
        # Only allow Dual-Hand GAME mode when virtual passthrough keyboard is actually active
        # (prevents grabbing the physical keyboard if uinput keyboard creation failed!).
        self.state = VirtualMouseState(
            mouse_speed=self._init_speed,
            game_sensitivity=self._init_sens,
            preferred_mode=self._preferred_mode,
            initial_mode=self._initial_mode,
            game_layout=self._game_layout,
            allow_game_mode=self._active_kbd_uinput,
        )
        if _EVDEV_OK:
            self._thread = threading.Thread(target=self._run_loop, name="rj-kbd-mouse", daemon=True)
            self._thread.start()
        return created

    def stop(self) -> None:
        """Release grabs, release any held virtual keys/buttons, and destroy virtual devices."""
        self._stop.set()
        t = self._thread
        self._thread = None
        if t is not None and t.is_alive():
            t.join(timeout=0.8)
        for attr in ("_kbd_uinput", "_uinput"):
            ui = getattr(self, attr, None)
            if ui is not None:
                try:
                    ui.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        self._active_uinput = False
        self._active_kbd_uinput = False
