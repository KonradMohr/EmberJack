#!/usr/bin/env python3
"""
RaspyJack Unified Keyboard Engine & Shared-Memory State
-------------------------------------------------------
Shared across raspyjack.py, hdmi_host.py, evdev_keys.py, gpio_shim.py, and all
351 payload subprocesses.

Features:
  1. Shared-memory live key bitmap & RaspyJack button mask (/dev/shm/rj_kbd_state.shm)
  2. Clean Navigation Mode vs Text-Input Mode separation (PID-tracked, crash-safe)
  3. Complete standard Linux PC scancode translation (Shift, CapsLock, symbols, Numpad)
  4. Legacy CardputerZero TCA8418 profile preserved when CARDPUTER_320 is active
  5. Lossless character & special-key event delivery over rj_input.sock
"""

import contextlib
import json
import mmap
import os
import socket
import struct
from typing import Dict, Optional, Tuple

SHM_PATH = os.environ.get("RJ_KBD_SHM", "/dev/shm/rj_kbd_state.shm")
INPUT_SOCK_PATH = os.environ.get("RJ_INPUT_SOCK", "/dev/shm/rj_input.sock")

MAGIC = b"RJKD"
VERSION = 1
SHM_SIZE = 4096
# Header: magic(4s), version(I), host_pid(I), text_mode_pid(I), button_mask(I), mod_mask(I), seq(Q)
HDR_FMT = "<4sIIIIIQ"
BITMAP_OFFSET = 32
BITMAP_BYTES = 64   # 512 evdev keycodes

# Lock-free shared-memory text-event ring buffer (backup & co-transport with rj_input.sock)
TEXT_RING_MAGIC = 0x524A5451  # "RJTQ"
TEXT_RING_HDR_OFFSET = 128
TEXT_RING_HDR_FMT = "<IQII"   # magic(I), write_seq(Q), cap(I), reserved(I)
TEXT_RING_DATA_OFFSET = 160
TEXT_RING_CAP = 64
TEXT_RING_SLOT_FMT = "<QII44s"  # seq(Q), mods(I), flags(I: bit0=special), utf8_payload(44s)
TEXT_RING_SLOT_SIZE = struct.calcsize(TEXT_RING_SLOT_FMT)  # 60 bytes -> 160 + 64*60 = 4000 <= 4096
TEXT_FLAG_SPECIAL = 1 << 0

BUTTON_BITS: Dict[str, int] = {
    "KEY_UP_PIN": 1 << 0,
    "KEY_DOWN_PIN": 1 << 1,
    "KEY_LEFT_PIN": 1 << 2,
    "KEY_RIGHT_PIN": 1 << 3,
    "KEY_PRESS_PIN": 1 << 4,
    "KEY1_PIN": 1 << 5,
    "KEY2_PIN": 1 << 6,
    "KEY3_PIN": 1 << 7,
}

BUTTON_TO_INPUT_NAME: Dict[str, str] = {
    "KEY_UP_PIN": "UP",
    "KEY_DOWN_PIN": "DOWN",
    "KEY_LEFT_PIN": "LEFT",
    "KEY_RIGHT_PIN": "RIGHT",
    "KEY_PRESS_PIN": "OK",
    "KEY1_PIN": "KEY1",
    "KEY2_PIN": "KEY2",
    "KEY3_PIN": "KEY3",
}

MOD_LSHIFT = 1 << 0
MOD_RSHIFT = 1 << 1
MOD_CAPSLOCK = 1 << 2
MOD_LCTRL = 1 << 3
MOD_RCTRL = 1 << 4
MOD_LALT = 1 << 5
MOD_RALT = 1 << 6
MOD_SHIFT_ANY = MOD_LSHIFT | MOD_RSHIFT
MOD_CTRL_ANY = MOD_LCTRL | MOD_RCTRL
MOD_ALT_ANY = MOD_LALT | MOD_RALT

# Linux evdev modifier keycodes
CODE_LSHIFT = 42
CODE_RSHIFT = 54
CODE_CAPSLOCK = 58
CODE_LCTRL = 29
CODE_RCTRL = 97
CODE_LALT = 56
CODE_RALT = 100

MOD_CODE_TO_BIT = {
    CODE_LSHIFT: MOD_LSHIFT,
    CODE_RSHIFT: MOD_RSHIFT,
    CODE_LCTRL: MOD_LCTRL,
    CODE_RCTRL: MOD_RCTRL,
    CODE_LALT: MOD_LALT,
    CODE_RALT: MOD_RALT,
}

# Standard PC/USB Linux evdev keycodes -> (unshifted, shifted)
STANDARD_CHAR_MAP: Dict[int, Tuple[str, str]] = {
    # Number row
    2: ("1", "!"), 3: ("2", "@"), 4: ("3", "#"), 5: ("4", "$"), 6: ("5", "%"),
    7: ("6", "^"), 8: ("7", "&"), 9: ("8", "*"), 10: ("9", "("), 11: ("0", ")"),
    12: ("-", "_"), 13: ("=", "+"),
    # Top row
    16: ("q", "Q"), 17: ("w", "W"), 18: ("e", "E"), 19: ("r", "R"), 20: ("t", "T"),
    21: ("y", "Y"), 22: ("u", "U"), 23: ("i", "I"), 24: ("o", "O"), 25: ("p", "P"),
    26: ("[", "{"), 27: ("]", "}"), 43: ("\\", "|"),
    # Home row
    30: ("a", "A"), 31: ("s", "S"), 32: ("d", "D"), 33: ("f", "F"), 34: ("g", "G"),
    35: ("h", "H"), 36: ("j", "J"), 37: ("k", "K"), 38: ("l", "L"),
    39: (";", ":"), 40: ("'", '"'), 41: ("`", "~"),
    # Bottom row
    44: ("z", "Z"), 45: ("x", "X"), 46: ("c", "C"), 47: ("v", "V"), 48: ("b", "B"),
    49: ("n", "N"), 50: ("m", "M"),
    51: (",", "<"), 52: (".", ">"), 53: ("/", "?"),
    86: ("<", ">"),  # ISO 102nd key
    # Space
    57: (" ", " "),
    # Numpad
    71: ("7", "7"), 72: ("8", "8"), 73: ("9", "9"), 74: ("-", "-"),
    75: ("4", "4"), 76: ("5", "5"), 77: ("6", "6"), 78: ("+", "+"),
    79: ("1", "1"), 80: ("2", "2"), 81: ("3", "3"),
    82: ("0", "0"), 83: (".", "."),
    55: ("*", "*"), 98: ("/", "/"),
}

# Legacy CardputerZero TCA8418 Fn-layer keycodes (ONLY used in tca8418 profile)
TCA8418_CHAR_MAP: Dict[int, str] = {
    16: "q", 17: "w", 18: "e", 19: "r", 20: "t", 21: "y", 22: "u", 23: "i", 24: "o", 25: "p",
    30: "a", 31: "s", 32: "d", 33: "f", 34: "g", 35: "h", 36: "j", 37: "k", 38: "l",
    44: "z", 45: "x", 46: "c", 47: "v", 48: "b", 49: "n", 50: "m",
    2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7", 9: "8", 10: "9", 11: "0",
    57: " ",
    26: "!", 27: "@", 39: "#", 40: "$", 41: "%", 43: "^",
    51: "&", 52: "*", 53: "(", 94: ")",
    55: "~", 69: "`", 70: "_", 71: "-", 72: "+", 73: "=",
    74: "[", 75: "]", 76: "{", 77: "}",
    79: ";", 80: ":", 81: "'", 82: '"', 83: "<", 85: ">",
    86: "\\", 89: "|", 90: ",", 91: ".", 92: "/", 93: "?",
}

SPECIAL_KEY_CODES: Dict[int, str] = {
    1: "ESCAPE",
    14: "BACKSPACE",
    15: "TAB",
    28: "ENTER",
    96: "ENTER",      # KEY_KPENTER
    110: "INSERT",
    111: "DELETE",
    102: "HOME",
    107: "END",
    103: "UP",
    108: "DOWN",
    105: "LEFT",
    106: "RIGHT",
    104: "PAGEUP",
    109: "PAGEDOWN",
    62: "F4",
    63: "F5",
    64: "F6",
    65: "F7",
    66: "F8",
    67: "F9",
}

# Navigation button mapping for STANDARD PC keyboards (no F/Z/X/C collision!)
STANDARD_NAV_MAP: Dict[int, str] = {
    103: "KEY_UP_PIN",
    108: "KEY_DOWN_PIN",
    105: "KEY_LEFT_PIN",
    106: "KEY_RIGHT_PIN",
    28: "KEY_PRESS_PIN",   # Enter
    96: "KEY_PRESS_PIN",   # Numpad Enter
    1: "KEY3_PIN",         # Escape
    14: "KEY3_PIN",        # Backspace (suppressed automatically in text mode!)
    59: "KEY1_PIN",        # F1
    60: "KEY2_PIN",        # F2
    61: "KEY3_PIN",        # F3
    57: "KEY1_PIN",        # Space (suppressed automatically in text mode!)
    15: "KEY2_PIN",        # Tab (suppressed automatically in text mode!)
}

# Keys that remain active as nav buttons even in text-input mode
TEXT_MODE_NAV_CODES = {
    103: "KEY_UP_PIN",
    108: "KEY_DOWN_PIN",
    105: "KEY_LEFT_PIN",
    106: "KEY_RIGHT_PIN",
    59: "KEY1_PIN",        # F1
    60: "KEY2_PIN",        # F2
    61: "KEY3_PIN",        # F3
}

# CardputerZero TCA8418 navigation map (unified with STANDARD_NAV_MAP: Space=KEY1, Tab=KEY2, Esc/Bksp=KEY3)
TCA8418_NAV_MAP: Dict[int, str] = {
    103: "KEY_UP_PIN",
    108: "KEY_DOWN_PIN",
    105: "KEY_LEFT_PIN",
    106: "KEY_RIGHT_PIN",
    33: "KEY_UP_PIN",
    44: "KEY_LEFT_PIN",
    45: "KEY_DOWN_PIN",
    46: "KEY_RIGHT_PIN",
    28: "KEY_PRESS_PIN",
    1: "KEY3_PIN",
    57: "KEY1_PIN",
    15: "KEY2_PIN",
    14: "KEY3_PIN",
}

_shm_fd: Optional[int] = None
_shm_mm: Optional[mmap.mmap] = None
_dgram_sock: Optional[socket.socket] = None
_text_mode_depth: int = 0


def _get_shm(create: bool = True) -> Optional[mmap.mmap]:
    global _shm_fd, _shm_mm
    if _shm_mm is not None:
        return _shm_mm
    if not create and not os.path.exists(SHM_PATH):
        return None
    try:
        os.makedirs(os.path.dirname(SHM_PATH) or "/dev/shm", exist_ok=True)
        flags = (os.O_CREAT | os.O_RDWR) if create else os.O_RDWR
        _shm_fd = os.open(SHM_PATH, flags, 0o666)
        try:
            os.chmod(SHM_PATH, 0o666)
        except OSError:
            pass
        st = os.fstat(_shm_fd)
        if st.st_size < SHM_SIZE:
            os.ftruncate(_shm_fd, SHM_SIZE)
        _shm_mm = mmap.mmap(_shm_fd, SHM_SIZE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        magic = _shm_mm[0:4]
        if magic != MAGIC:
            _shm_mm[0:SHM_SIZE] = b"\x00" * SHM_SIZE
            struct.pack_into(HDR_FMT, _shm_mm, 0, MAGIC, VERSION, 0, 0, 0, 0, 0)
            struct.pack_into(TEXT_RING_HDR_FMT, _shm_mm, TEXT_RING_HDR_OFFSET, TEXT_RING_MAGIC, 0, TEXT_RING_CAP, 0)
        else:
            rmagic = struct.unpack_from("<I", _shm_mm, TEXT_RING_HDR_OFFSET)[0]
            if rmagic != TEXT_RING_MAGIC:
                _shm_mm[TEXT_RING_HDR_OFFSET:SHM_SIZE] = b"\x00" * (SHM_SIZE - TEXT_RING_HDR_OFFSET)
                struct.pack_into(TEXT_RING_HDR_FMT, _shm_mm, TEXT_RING_HDR_OFFSET, TEXT_RING_MAGIC, 0, TEXT_RING_CAP, 0)
        return _shm_mm
    except OSError:
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def get_nav_map(profile: str = "standard", kb_cfg: Optional[dict] = None) -> Dict[int, str]:
    """Return evdev_code -> button_name mapping for the requested keyboard profile."""
    if profile == "tca8418":
        return dict(TCA8418_NAV_MAP)
    nav = {
        103: "KEY_UP_PIN",
        108: "KEY_DOWN_PIN",
        105: "KEY_LEFT_PIN",
        106: "KEY_RIGHT_PIN",
        28: "KEY_PRESS_PIN",
        96: "KEY_PRESS_PIN",
        1: "KEY3_PIN",
        59: "KEY1_PIN",
        60: "KEY2_PIN",
        61: "KEY3_PIN",
    }
    cfg = kb_cfg or {}
    if cfg.get("space_is_key1", True):
        nav[57] = "KEY1_PIN"
    if cfg.get("tab_is_key2", True):
        nav[15] = "KEY2_PIN"
    if cfg.get("backspace_is_key2", False):
        nav[14] = "KEY2_PIN"
    if cfg.get("number_keys_as_fn", False):
        nav[2] = "KEY1_PIN"
        nav[3] = "KEY2_PIN"
        nav[4] = "KEY3_PIN"
    if cfg.get("wasd_nav", False):
        nav[17] = "KEY_UP_PIN"
        nav[31] = "KEY_DOWN_PIN"
        nav[30] = "KEY_LEFT_PIN"
        nav[32] = "KEY_RIGHT_PIN"
    return nav


def translate_evdev_code(code: int, mod_mask: int = 0, profile: str = "standard") -> Tuple[Optional[str], Optional[str]]:
    """Translate a Linux evdev keycode + modifier mask to (char, special_name)."""
    # CardputerZero / 60% compact keyboard paging chords (no physical PgUp / PgDn keys)
    if (mod_mask & (MOD_ALT_ANY | MOD_CTRL_ANY)) and code in (103, 108):
        return None, ("PAGEUP" if code == 103 else "PAGEDOWN")

    if code in SPECIAL_KEY_CODES:
        return None, SPECIAL_KEY_CODES[code]

    # CardputerZero / 60% function-key fallback in text_input_mode: Ctrl+1/2/3 -> KEY1/KEY2/KEY3
    if (mod_mask & MOD_CTRL_ANY) and code in (2, 3, 4):
        return None, {2: "KEY1", 3: "KEY2", 4: "KEY3"}[code]

    # Ctrl+letter & Ctrl+symbol control sequences (useful for terminal / shell)
    if (mod_mask & MOD_CTRL_ANY) and code in STANDARD_CHAR_MAP:
        base_ch = STANDARD_CHAR_MAP[code][0]
        if "a" <= base_ch <= "z":
            return None, f"CTRL_{base_ch.upper()}"
        if base_ch == " ":
            return None, "CTRL_SPACE"
        if base_ch == "[":
            return None, "CTRL_LBRACKET"
        if base_ch == "\\":
            return None, "CTRL_BACKSLASH"
        if base_ch == "]":
            return None, "CTRL_RBRACKET"

    if profile == "tca8418":
        ch = TCA8418_CHAR_MAP.get(code)
        if ch is not None and ch.isalpha() and (mod_mask & (MOD_SHIFT_ANY | MOD_CAPSLOCK)):
            shift = bool(mod_mask & MOD_SHIFT_ANY)
            caps = bool(mod_mask & MOD_CAPSLOCK)
            if shift ^ caps:
                ch = ch.upper()
        return ch, None

    pair = STANDARD_CHAR_MAP.get(code)
    if pair is None:
        return None, None
    unshifted, shifted = pair
    shift_on = bool(mod_mask & MOD_SHIFT_ANY)
    caps_on = bool(mod_mask & MOD_CAPSLOCK)
    if unshifted.isalpha():
        return (shifted if (shift_on ^ caps_on) else unshifted), None
    return (shifted if shift_on else unshifted), None


def register_host(pid: Optional[int] = None) -> None:
    """Register hdmi_host.py as the active desktop keyboard owner."""
    mm = _get_shm(create=True)
    if mm is None:
        return
    struct.pack_into("<I", mm, 8, int(pid if pid is not None else os.getpid()))


def unregister_host(pid: Optional[int] = None) -> None:
    mm = _get_shm(create=False)
    if mm is None:
        return
    cur = struct.unpack_from("<I", mm, 8)[0]
    target = int(pid if pid is not None else os.getpid())
    if cur == target or not _pid_alive(cur):
        struct.pack_into("<I", mm, 8, 0)
        clear_all_keys()


def is_host_active() -> bool:
    """Return True if a live desktop hdmi_host.py is driving keyboard state."""
    mm = _get_shm(create=False)
    if mm is None:
        return False
    host_pid = struct.unpack_from("<I", mm, 8)[0]
    if host_pid and _pid_alive(host_pid):
        return True
    if host_pid:
        struct.pack_into("<I", mm, 8, 0)
    return False


def set_text_mode(active: bool) -> None:
    """Enable or disable Text-Input Mode across processes (PID-tracked)."""
    mm = _get_shm(create=True)
    if mm is None:
        return
    pid = os.getpid() if active else 0
    if not active:
        cur = struct.unpack_from("<I", mm, 12)[0]
        if cur != 0 and cur != os.getpid() and _pid_alive(cur):
            return
    struct.pack_into("<I", mm, 12, pid)
    if active:
        # Clear any non-arrow buttons that were held when entering text mode
        cur_mask = struct.unpack_from("<I", mm, 16)[0]
        nav_only = (
            BUTTON_BITS["KEY_UP_PIN"]
            | BUTTON_BITS["KEY_DOWN_PIN"]
            | BUTTON_BITS["KEY_LEFT_PIN"]
            | BUTTON_BITS["KEY_RIGHT_PIN"]
        )
        struct.pack_into("<I", mm, 16, cur_mask & nav_only)


def is_text_mode() -> bool:
    """Return True if any live process currently holds Text-Input Mode."""
    mm = _get_shm(create=False)
    if mm is None:
        return False
    pid = struct.unpack_from("<I", mm, 12)[0]
    if pid == 0:
        return False
    if _pid_alive(pid):
        return True
    # Auto-recover from crashed holder
    struct.pack_into("<I", mm, 12, 0)
    return False


@contextlib.contextmanager
def text_input_mode():
    """Re-entrant context manager that isolates keyboard typing from GPIO button collisions."""
    global _text_mode_depth
    _text_mode_depth += 1
    set_text_mode(True)
    try:
        yield
    finally:
        _text_mode_depth = max(0, _text_mode_depth - 1)
        if _text_mode_depth == 0:
            set_text_mode(False)


def _send_input_datagram(payload: dict) -> None:
    global _dgram_sock
    sock_path = os.environ.get("RJ_INPUT_SOCK", INPUT_SOCK_PATH)
    if not os.path.exists(sock_path):
        return
    try:
        if _dgram_sock is None:
            _dgram_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            _dgram_sock.setblocking(False)
        _dgram_sock.sendto(json.dumps(payload, separators=(",", ":")).encode("utf-8"), sock_path)
    except Exception:
        pass


def send_button_event(button_pin_name: str, pressed: bool) -> None:
    """Forward a press/release event to rj_input.sock for the active foreground process."""
    btn = BUTTON_TO_INPUT_NAME.get(button_pin_name)
    if not btn:
        return
    _send_input_datagram({
        "type": "input",
        "button": btn,
        "state": "press" if pressed else "release",
        "source": "keyboard",
    })


def push_shm_text_event(
    char: Optional[str] = None,
    special: Optional[str] = None,
    mod_mask: int = 0,
) -> int:
    """Append a text/special event to the shared-memory ring buffer and return its seq."""
    mm = _get_shm(create=True)
    if mm is None:
        return 0
    if special:
        raw = str(special).encode("utf-8", "ignore")[:43]
        flags = TEXT_FLAG_SPECIAL
    elif char:
        raw = str(char).encode("utf-8", "ignore")[:43]
        flags = 0
    else:
        return 0
    if not raw:
        return 0
    try:
        rmagic, write_seq, cap, _res = struct.unpack_from(TEXT_RING_HDR_FMT, mm, TEXT_RING_HDR_OFFSET)
        if rmagic != TEXT_RING_MAGIC or cap <= 0 or cap > TEXT_RING_CAP:
            write_seq = 0
            cap = TEXT_RING_CAP
        next_seq = write_seq + 1
        slot_off = TEXT_RING_DATA_OFFSET + ((next_seq - 1) % cap) * TEXT_RING_SLOT_SIZE
        # Seqlock write: invalidate slot seq=0, write payload, then publish slot seq & ring head
        struct.pack_into("<Q", mm, slot_off, 0)
        struct.pack_into(TEXT_RING_SLOT_FMT, mm, slot_off, next_seq, int(mod_mask) & 0xFFFF, flags, raw)
        struct.pack_into(TEXT_RING_HDR_FMT, mm, TEXT_RING_HDR_OFFSET, TEXT_RING_MAGIC, next_seq, cap, 0)
        return next_seq
    except Exception:
        return 0


def get_shm_text_seq() -> int:
    """Return the latest shared-memory text ring sequence number (0 if unavailable)."""
    mm = _get_shm(create=False)
    if mm is None:
        return 0
    try:
        rmagic, write_seq, _cap, _res = struct.unpack_from(TEXT_RING_HDR_FMT, mm, TEXT_RING_HDR_OFFSET)
        if rmagic != TEXT_RING_MAGIC:
            return 0
        return int(write_seq)
    except Exception:
        return 0


def read_shm_text_events(last_seq: int = 0):
    """Read all new text/special events from SHM ring with seq > last_seq.

    Returns (events_list, new_last_seq).
    """
    mm = _get_shm(create=False)
    if mm is None:
        return [], last_seq
    try:
        rmagic, write_seq, cap, _res = struct.unpack_from(TEXT_RING_HDR_FMT, mm, TEXT_RING_HDR_OFFSET)
        if rmagic != TEXT_RING_MAGIC or cap <= 0 or cap > TEXT_RING_CAP:
            return [], last_seq
        if write_seq < last_seq:
            # SHM was reinitialized; resync cleanly
            last_seq = 0
        if write_seq <= last_seq:
            return [], last_seq
        start_seq = max(last_seq + 1, write_seq - cap + 1)
        out = []
        for target_seq in range(start_seq, write_seq + 1):
            slot_off = TEXT_RING_DATA_OFFSET + ((target_seq - 1) % cap) * TEXT_RING_SLOT_SIZE
            s_seq, mods, flags, raw_buf = struct.unpack_from(TEXT_RING_SLOT_FMT, mm, slot_off)
            if s_seq != target_seq:
                continue
            text = raw_buf.split(b"\x00", 1)[0].decode("utf-8", "ignore")
            if not text:
                continue
            ev = {
                "type": "text_key",
                "session_id": "local",
                "source": "keyboard",
                "mods": int(mods),
                "seq": int(s_seq),
            }
            if flags & TEXT_FLAG_SPECIAL:
                ev["special"] = text
            else:
                ev["key"] = text
            out.append(ev)
        return out, int(write_seq)
    except Exception:
        return [], last_seq


def send_text_event(char: Optional[str] = None, special: Optional[str] = None, mod_mask: int = 0) -> None:
    """Queue a lossless local keyboard text/special event to BOTH SHM ring and rj_input.sock."""
    if not special and not char:
        return
    seq = push_shm_text_event(char=char, special=special, mod_mask=mod_mask)
    payload: Dict[str, object] = {
        "type": "text_key",
        "session_id": "local",
        "source": "keyboard",
        "mods": int(mod_mask),
    }
    if seq:
        payload["seq"] = seq
    if special:
        payload["special"] = special
    elif char:
        payload["key"] = char
    _send_input_datagram(payload)


def set_key_state(evdev_code: int, pressed: bool) -> None:
    """Update raw evdev keycode bit in shared memory."""
    mm = _get_shm(create=True)
    if mm is None or not (0 <= evdev_code < BITMAP_BYTES * 8):
        return
    byte_idx = BITMAP_OFFSET + (evdev_code >> 3)
    bit_mask = 1 << (evdev_code & 7)
    val = mm[byte_idx]
    if pressed:
        val |= bit_mask
    else:
        val &= (~bit_mask & 0xFF)
    mm[byte_idx] = val


def set_button_state(button_name: str, pressed: bool) -> None:
    """Update logical RaspyJack button bit in shared memory."""
    mm = _get_shm(create=True)
    bit = BUTTON_BITS.get(button_name)
    if mm is None or not bit:
        return
    mask = struct.unpack_from("<I", mm, 16)[0]
    if pressed:
        mask |= bit
    else:
        mask &= ~bit
    struct.pack_into("<I", mm, 16, mask)


def set_modifiers(mod_mask: int) -> None:
    mm = _get_shm(create=True)
    if mm is None:
        return
    struct.pack_into("<I", mm, 20, int(mod_mask) & 0xFFFF)


def get_modifiers() -> int:
    mm = _get_shm(create=False)
    if mm is None:
        return 0
    return struct.unpack_from("<I", mm, 20)[0]


def update_modifier_from_code(evdev_code: int, pressed: bool, is_repeat: bool = False) -> int:
    """Update modifier mask in SHM from a Linux evdev key event and return new mask."""
    cur = get_modifiers()
    if evdev_code == CODE_CAPSLOCK:
        if pressed and not is_repeat:
            cur ^= MOD_CAPSLOCK
            set_modifiers(cur)
        return cur
    bit = MOD_CODE_TO_BIT.get(evdev_code)
    if bit:
        if pressed:
            cur |= bit
        else:
            cur &= ~bit
        set_modifiers(cur)
    return cur


def recompute_button_mask(nav_map: Dict[int, str]) -> int:
    """Recompute button_mask from currently pressed raw keys, respecting text_mode."""
    mm = _get_shm(create=True)
    if mm is None:
        return 0
    in_text = is_text_mode()
    active_map = TEXT_MODE_NAV_CODES if in_text else nav_map
    mask = 0
    for code, btn in active_map.items():
        if is_key_pressed(code):
            mask |= BUTTON_BITS.get(btn, 0)
    struct.pack_into("<I", mm, 16, mask)
    return mask


def handle_key_event(
    evdev_code: int,
    pressed: bool,
    is_repeat: bool = False,
    text_override: Optional[str] = None,
    profile: str = "standard",
    kb_cfg: Optional[dict] = None,
    mod_mask_override: Optional[int] = None,
    emit_text_from_code: bool = True,
) -> None:
    """Process one key press/release/repeat from either desktop window or evdev.

    Updates shared memory bitmaps AND dispatches lossless events to rj_input.sock
    with clean Navigation Mode vs Text-Input Mode isolation.
    """
    set_key_state(evdev_code, pressed)

    if mod_mask_override is not None:
        set_modifiers(mod_mask_override)
        mod_mask = mod_mask_override
    else:
        mod_mask = update_modifier_from_code(evdev_code, pressed, is_repeat=is_repeat)

    nav_map = get_nav_map(profile=profile, kb_cfg=kb_cfg)
    in_text = is_text_mode()
    active_nav = TEXT_MODE_NAV_CODES if in_text else nav_map

    # Recompute logical button mask in SHM
    recompute_button_mask(nav_map)

    # Dispatch navigation button edge to rj_input.sock (initial press + release only)
    mapped_btn = active_nav.get(evdev_code)
    if mapped_btn and not is_repeat:
        send_button_event(mapped_btn, pressed)

    # Dispatch lossless text / special events on press & repeat
    if pressed:
        if text_override is not None:
            if text_override:
                send_text_event(char=text_override, mod_mask=mod_mask)
            return
        if emit_text_from_code:
            char, special = translate_evdev_code(evdev_code, mod_mask=mod_mask, profile=profile)
            if special:
                send_text_event(special=special, mod_mask=mod_mask)
            elif char is not None:
                send_text_event(char=char, mod_mask=mod_mask)


def is_pressed(button_name: str) -> bool:
    """Return True if the given RaspyJack button (e.g. 'KEY_UP_PIN') is held."""
    mm = _get_shm(create=False)
    bit = BUTTON_BITS.get(button_name)
    if mm is None or not bit:
        return False
    return bool(struct.unpack_from("<I", mm, 16)[0] & bit)


def is_key_pressed(evdev_code: int) -> bool:
    """Return True if the given raw Linux evdev keycode is currently held."""
    mm = _get_shm(create=False)
    if mm is None or not (0 <= evdev_code < BITMAP_BYTES * 8):
        return False
    byte_idx = BITMAP_OFFSET + (evdev_code >> 3)
    return bool(mm[byte_idx] & (1 << (evdev_code & 7)))


def any_pressed() -> bool:
    mm = _get_shm(create=False)
    if mm is None:
        return False
    if struct.unpack_from("<I", mm, 16)[0]:
        return True
    return any(b != 0 for b in mm[BITMAP_OFFSET : BITMAP_OFFSET + BITMAP_BYTES])


def get_held_buttons() -> set:
    """Return set of currently held button pin names (e.g. {'KEY_UP_PIN'})."""
    mm = _get_shm(create=False)
    if mm is None:
        return set()
    mask = struct.unpack_from("<I", mm, 16)[0]
    if not mask:
        return set()
    return {name for name, bit in BUTTON_BITS.items() if mask & bit}


def get_pressed_button() -> Optional[str]:
    mm = _get_shm(create=False)
    if mm is None:
        return None
    mask = struct.unpack_from("<I", mm, 16)[0]
    if not mask:
        return None
    for name, bit in BUTTON_BITS.items():
        if mask & bit:
            return name
    return None


def clear_all_keys() -> None:
    """Release all keys & buttons (e.g. on window focus loss or shutdown)."""
    mm = _get_shm(create=False)
    if mm is None:
        return
    held = get_held_buttons()
    struct.pack_into("<I", mm, 16, 0)
    struct.pack_into("<I", mm, 20, 0)
    mm[BITMAP_OFFSET : BITMAP_OFFSET + BITMAP_BYTES] = b"\x00" * BITMAP_BYTES
    for btn in held:
        send_button_event(btn, False)


def close_mappings() -> None:
    global _shm_fd, _shm_mm, _dgram_sock
    if _shm_mm is not None:
        try:
            _shm_mm.close()
        except Exception:
            pass
    if _shm_fd is not None:
        try:
            os.close(_shm_fd)
        except Exception:
            pass
    if _dgram_sock is not None:
        try:
            _dgram_sock.close()
        except Exception:
            pass
    _shm_fd = _shm_mm = _dgram_sock = None
