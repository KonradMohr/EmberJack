#!/usr/bin/env python3
"""
RaspyJack Environment, Path, Font & Configuration Helper
--------------------------------------------------------
Centralized cross-platform / cross-distro helpers so RaspyJack runs cleanly on:
  - Raspberry Pi + Waveshare SPI HAT (ST7735_128, ST7789_240)
  - M5Stack CardputerZero (CARDPUTER_320)
  - Standard Linux PC / Raspberry Pi with HDMI monitor + normal keyboard (HDMI)
"""

import glob
import json
import os
import shutil
import subprocess
from typing import Any, Dict, Optional

from PIL import ImageFont

SUPPORTED_DISPLAY_TYPES = ("ST7735_128", "ST7789_240", "CARDPUTER_320", "HDMI")

HDMI_DEFAULTS: Dict[str, Any] = {
    "width": 640,
    "height": 360,
    "backend": "auto",          # auto | sdl2 | gtk | fbdev
    "fullscreen": False,
    "scale_mode": "fit",        # fit | integer | stretch
    "fbdev_filter": "fast",     # fast | smooth
    "fps": 30,
    "show_hud": True,
    "window_width": 1280,
    "window_height": 720,
}

KEYBOARD_DEFAULTS: Dict[str, Any] = {
    "profile": "auto",          # auto | standard | tca8418
    "wasd_nav": False,
    "number_keys_as_fn": False,
    "space_is_key1": True,
    "tab_is_key2": True,
    "backspace_is_key2": False,
}

_FONT_CACHE: Dict[tuple, Optional[str]] = {}
_LOADED_FONT_CACHE: Dict[tuple, Any] = {}
_CPU_TEMP_PATH: Optional[str] = None


def get_base_dir() -> str:
    """Return the RaspyJack root directory (portable across install paths)."""
    env_dir = os.environ.get("RJ_BASE_DIR", "").strip()
    if env_dir and os.path.isdir(env_dir):
        return os.path.abspath(env_dir)
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.isfile(os.path.join(here, "raspyjack.py")):
        return here
    if os.path.isdir("/root/Raspyjack"):
        return "/root/Raspyjack"
    return here


def get_config_path() -> str:
    """Return active gui_conf.json path."""
    env_cfg = os.environ.get("RJ_CONFIG_PATH", "").strip()
    if env_cfg:
        return env_cfg
    local_cfg = os.path.join(get_base_dir(), "gui_conf.json")
    if os.path.isfile(local_cfg):
        return local_cfg
    root_cfg = "/root/Raspyjack/gui_conf.json"
    if os.path.isfile(root_cfg):
        return root_cfg
    return local_cfg


def load_gui_conf() -> Dict[str, Any]:
    """Load gui_conf.json safely, returning {} on error or missing file."""
    path = get_config_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def is_cardputer_hardware() -> bool:
    """Return True if running on M5Stack CardputerZero LCD framebuffer."""
    for idx in range(4):
        try:
            with open(f"/sys/class/graphics/fb{idx}/name", "r", encoding="utf-8") as handle:
                name = handle.read()
            if any(marker in name for marker in ("st7789v_m5st", "panel-mipi-dbi")):
                return True
        except Exception:
            pass
    return False


def has_spi_hardware() -> bool:
    """Return True if Raspberry Pi SPI0 device node exists."""
    return os.path.exists("/dev/spidev0.0")


def has_desktop_session() -> bool:
    """Return True if a Wayland or X11 display server is available."""
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def get_display_type() -> str:
    """Determine active display type from env, gui_conf.json, or hardware."""
    env_type = os.environ.get("RJ_DISPLAY_TYPE", "").strip().upper()
    if env_type in SUPPORTED_DISPLAY_TYPES:
        return env_type

    conf = load_gui_conf()
    cfg_type = str(conf.get("DISPLAY", {}).get("type", "") or "").strip()
    if cfg_type in SUPPORTED_DISPLAY_TYPES:
        if cfg_type == "ST7735_128" and is_cardputer_hardware():
            return "CARDPUTER_320"
        return cfg_type

    if is_cardputer_hardware():
        return "CARDPUTER_320"

    if not has_spi_hardware() and os.environ.get("RJ_AUTO_HDMI", "1") != "0":
        return "HDMI"

    return "ST7735_128"


def is_hdmi_mode() -> bool:
    return get_display_type() == "HDMI"


def get_display_flip() -> bool:
    conf = load_gui_conf()
    return bool(conf.get("DISPLAY", {}).get("flip", False))


def _parse_resolution(spec: str) -> Optional[tuple]:
    if not spec:
        return None
    parts = spec.lower().replace("×", "x").split("x")
    if len(parts) != 2:
        return None
    try:
        w, h = int(parts[0].strip()), int(parts[1].strip())
        if 128 <= w <= 1920 and 128 <= h <= 1080:
            return w, h
    except ValueError:
        pass
    return None


def get_hdmi_config() -> Dict[str, Any]:
    """Return merged HDMI configuration from defaults, gui_conf.json, and env."""
    cfg = dict(HDMI_DEFAULTS)
    conf = load_gui_conf()
    for section in (conf.get("HDMI"), conf.get("DISPLAY", {}).get("hdmi")):
        if isinstance(section, dict):
            for k, v in section.items():
                if k in cfg and v is not None:
                    cfg[k] = v

    res_env = os.environ.get("RJ_HDMI_RES", "").strip()
    parsed = _parse_resolution(res_env)
    if parsed:
        cfg["width"], cfg["height"] = parsed

    if os.environ.get("RJ_HDMI_WIDTH"):
        try:
            cfg["width"] = max(128, min(1920, int(os.environ["RJ_HDMI_WIDTH"])))
        except ValueError:
            pass
    if os.environ.get("RJ_HDMI_HEIGHT"):
        try:
            cfg["height"] = max(128, min(1080, int(os.environ["RJ_HDMI_HEIGHT"])))
        except ValueError:
            pass
    if os.environ.get("RJ_HDMI_BACKEND"):
        cfg["backend"] = os.environ["RJ_HDMI_BACKEND"].strip().lower()
    if os.environ.get("RJ_HDMI_FULLSCREEN") is not None:
        cfg["fullscreen"] = os.environ["RJ_HDMI_FULLSCREEN"].strip().lower() in ("1", "true", "yes", "on")
    if os.environ.get("RJ_HDMI_SCALE"):
        mode = os.environ["RJ_HDMI_SCALE"].strip().lower()
        if mode in ("fit", "integer", "stretch"):
            cfg["scale_mode"] = mode
    if os.environ.get("RJ_HDMI_FPS"):
        try:
            cfg["fps"] = max(5, min(60, int(os.environ["RJ_HDMI_FPS"])))
        except ValueError:
            pass
    if os.environ.get("RJ_HDMI_HUD") is not None:
        cfg["show_hud"] = os.environ["RJ_HDMI_HUD"].strip().lower() in ("1", "true", "yes", "on")
    if os.environ.get("RJ_HDMI_FBDEV_FILTER"):
        flt = os.environ["RJ_HDMI_FBDEV_FILTER"].strip().lower()
        if flt in ("fast", "smooth"):
            cfg["fbdev_filter"] = flt

    cfg["width"] = max(128, min(1920, int(cfg["width"])))
    cfg["height"] = max(128, min(1080, int(cfg["height"])))
    cfg["fps"] = max(5, min(60, int(cfg["fps"])))
    if str(cfg.get("fbdev_filter", "fast")).strip().lower() not in ("fast", "smooth"):
        cfg["fbdev_filter"] = "fast"
    else:
        cfg["fbdev_filter"] = str(cfg["fbdev_filter"]).strip().lower()
    return cfg


def get_keyboard_config() -> Dict[str, Any]:
    """Return merged keyboard configuration."""
    cfg = dict(KEYBOARD_DEFAULTS)
    conf = load_gui_conf()
    kb = conf.get("KEYBOARD")
    if isinstance(kb, dict):
        for k, v in kb.items():
            if k in cfg and v is not None:
                cfg[k] = v

    env_prof = os.environ.get("RJ_KEYBOARD_PROFILE", "").strip().lower()
    if env_prof in ("auto", "standard", "tca8418"):
        cfg["profile"] = env_prof

    if cfg["profile"] == "auto":
        cfg["resolved_profile"] = "tca8418" if get_display_type() == "CARDPUTER_320" else "standard"
    else:
        cfg["resolved_profile"] = cfg["profile"]
    return cfg


def _fc_match_font(pattern: str) -> Optional[str]:
    if not shutil.which("fc-match"):
        return None
    try:
        res = subprocess.run(
            ["fc-match", "-f", "%{file}", pattern],
            capture_output=True,
            text=True,
            timeout=2,
        )
        path = res.stdout.strip()
        if path and os.path.isfile(path) and path.lower().endswith((".ttf", ".otf", ".ttc")):
            return path
    except Exception:
        pass
    return None


def find_font(bold: bool = False, mono: bool = False, icon: bool = False) -> Optional[str]:
    """Locate a TrueType/OpenType font across Debian, Arch, Fedora, and bundled paths."""
    cache_key = (bool(bold), bool(mono), bool(icon))
    if cache_key in _FONT_CACHE:
        return _FONT_CACHE[cache_key]

    base = get_base_dir()
    candidates = []
    if icon:
        candidates = [
            "/usr/share/fonts/truetype/fontawesome/fa-solid-900.ttf",
            "/usr/share/fonts/TTF/fa-solid-900.ttf",
            "/usr/share/fonts/OTF/Font Awesome 6 Free-Solid-900.otf",
            "/usr/share/fonts/OTF/Font Awesome 5 Free-Solid-900.otf",
            "/usr/share/fonts/fontawesome/fa-solid-900.ttf",
            "/usr/share/fonts/fontawesome-free/fa-solid-900.ttf",
            os.path.join(base, "fonts", "fa-solid-900.ttf"),
        ]
        # Nerd Fonts include full FontAwesome PUA glyphs (0xF000-0xF8FF)
        for pattern in (
            "/usr/share/fonts/TTF/*NerdFontPropo-Regular.ttf",
            "/usr/share/fonts/TTF/*NerdFont-Regular.ttf",
            "/usr/share/fonts/TTF/*NerdFont*.ttf",
            "/usr/share/fonts/truetype/*/*NerdFont*.ttf",
            "/usr/share/fonts/OTF/*NerdFont*.otf",
        ):
            candidates.extend(sorted(glob.glob(pattern)))
    elif mono and bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",
            "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
            "/usr/share/fonts/liberation/LiberationMono-Bold.ttf",
            "/usr/share/fonts/TTF/LiberationMono-Bold.ttf",
            "/usr/share/fonts/noto/NotoSansMono-Bold.ttf",
            "/usr/share/fonts/Adwaita/AdwaitaMono-Bold.ttf",
            "/usr/share/fonts/TTF/JetBrainsMonoNLNerdFont-Bold.ttf",
        ]
    elif mono:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
            "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
            "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
            "/usr/share/fonts/liberation/LiberationMono-Regular.ttf",
            "/usr/share/fonts/TTF/LiberationMono-Regular.ttf",
            "/usr/share/fonts/noto/NotoSansMono-Regular.ttf",
            "/usr/share/fonts/Adwaita/AdwaitaMono-Regular.ttf",
            "/usr/share/fonts/TTF/JetBrainsMonoNLNerdFont-Regular.ttf",
        ]
    elif bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/TTF/LiberationSans-Bold.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/TTF/LiberationSans-Regular.ttf",
            "/usr/share/fonts/noto/NotoSans-Regular.ttf",
        ]

    found = None
    for path in candidates:
        if os.path.isfile(path):
            found = path
            break

    if found is None:
        if icon:
            matched = _fc_match_font("Font Awesome 6 Free:style=Solid")
            if matched and ("awesome" in matched.lower() or "fa-solid" in matched.lower()):
                found = matched
        elif mono:
            found = _fc_match_font("monospace:bold" if bold else "monospace")
        else:
            found = _fc_match_font("sans:bold" if bold else "sans")

    _FONT_CACHE[cache_key] = found
    return found


def load_font(size: int = 10, bold: bool = False, mono: bool = False, icon: bool = False):
    """Load and cache a Pillow TrueType font safely across distros; never raises."""
    px = max(6, int(size))
    req_key = (px, bool(bold), bool(mono), bool(icon))
    cached = _LOADED_FONT_CACHE.get(req_key)
    if cached is not None:
        return cached

    path = find_font(bold=bold, mono=mono, icon=icon)
    font_obj = None
    if path:
        path_key = (path, px)
        font_obj = _LOADED_FONT_CACHE.get(path_key)
        if font_obj is None:
            try:
                font_obj = ImageFont.truetype(path, px)
                _LOADED_FONT_CACHE[path_key] = font_obj
            except Exception:
                font_obj = None
    if font_obj is None and (icon or bold or mono):
        fallback = find_font(bold=False, mono=False, icon=False)
        if fallback:
            fb_key = (fallback, px)
            font_obj = _LOADED_FONT_CACHE.get(fb_key)
            if font_obj is None:
                try:
                    font_obj = ImageFont.truetype(fallback, px)
                    _LOADED_FONT_CACHE[fb_key] = font_obj
                except Exception:
                    font_obj = None
    if font_obj is None:
        try:
            font_obj = ImageFont.load_default(size=px)
        except TypeError:
            font_obj = ImageFont.load_default()
    _LOADED_FONT_CACHE[req_key] = font_obj
    return font_obj


def _parse_temp_file(path: str) -> Optional[float]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            val = int(handle.read().strip())
        if val > 1000:
            return val / 1000.0
        if 0 < val < 150:
            return float(val)
    except Exception:
        return None
    return None


def read_cpu_temp() -> float:
    """Safely read CPU temperature in Celsius on Pi or Linux PC; returns 0.0 if absent."""
    global _CPU_TEMP_PATH
    if _CPU_TEMP_PATH:
        val = _parse_temp_file(_CPU_TEMP_PATH)
        if val is not None:
            return val
        _CPU_TEMP_PATH = None

    paths = [
        "/sys/class/thermal/thermal_zone0/temp",
        *sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")),
        *sorted(glob.glob("/sys/class/hwmon/hwmon*/temp1_input")),
    ]
    for path in paths:
        val = _parse_temp_file(path)
        if val is not None:
            _CPU_TEMP_PATH = path
            return val
    return 0.0
