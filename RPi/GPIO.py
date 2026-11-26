"""
Smart RPi.GPIO module router for RaspyJack.
"""

import importlib.machinery
import importlib.util
import os
import sys

_USE_REAL_GPIO = False
try:
    import rj_env

    _dtype = rj_env.get_display_type()
    _USE_REAL_GPIO = _dtype in ("ST7735_128", "ST7789_240")
except Exception:
    _USE_REAL_GPIO = False


def _load_system_rpi_gpio():
    """Attempt to load system-installed RPi.GPIO from outside this repository."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    ext_paths = [
        p for p in sys.path
        if p and os.path.abspath(p) != repo_root and not os.path.abspath(p).startswith(repo_root + os.sep)
    ]
    spec = importlib.machinery.PathFinder.find_spec("RPi", ext_paths)
    if spec is None or not spec.submodule_search_locations:
        return None
    gpio_spec = importlib.machinery.PathFinder.find_spec("GPIO", list(spec.submodule_search_locations))
    if gpio_spec is None or gpio_spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(gpio_spec)
    gpio_spec.loader.exec_module(mod)
    return mod


_real_gpio = None
if _USE_REAL_GPIO:
    try:
        _real_gpio = _load_system_rpi_gpio()
    except Exception:
        _real_gpio = None

if _real_gpio is not None:
    for _attr in dir(_real_gpio):
        if not _attr.startswith("__"):
            globals()[_attr] = getattr(_real_gpio, _attr)
else:
    from gpio_shim import *  # noqa: F401,F403
