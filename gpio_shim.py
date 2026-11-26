"""
gpio_shim – Drop-in replacement for RPi.GPIO in HDMI and CardputerZero modes.
Translates GPIO.input() calls to unified keyboard state (rj_keyboard / evdev_keys)
and WebUI virtual held-button state (rj_input).
GPIO.output/setup/setmode/cleanup are safe no-ops on non-HAT devices.
"""

import evdev_keys

try:
    import rj_input
except Exception:
    rj_input = None

# RPi.GPIO constants
BCM = 11
BOARD = 10
IN = 0
OUT = 1
PUD_UP = 22
PUD_DOWN = 21
PUD_OFF = 20
HIGH = 1
LOW = 0
RISING = 31
FALLING = 32
BOTH = 33
VERSION = "0.7.1-shim"
RPI_INFO = {"P1_REVISION": 3, "TYPE": "HDMI/PC Shim"}

# Map GPIO pin numbers -> Raspyjack button names
_DEFAULT_PIN_TO_BUTTON = {
    6: "KEY_UP_PIN",
    19: "KEY_DOWN_PIN",
    5: "KEY_LEFT_PIN",
    26: "KEY_RIGHT_PIN",
    13: "KEY_PRESS_PIN",
    21: "KEY1_PIN",
    20: "KEY2_PIN",
    16: "KEY3_PIN",
}

_PIN_TO_BUTTON = dict(_DEFAULT_PIN_TO_BUTTON)
try:
    from gpio_config import gpio_config

    if gpio_config.pins:
        _PIN_TO_BUTTON = {int(pin): str(name) for name, pin in gpio_config.pins.items() if isinstance(pin, int)}
        for _k, _v in _DEFAULT_PIN_TO_BUTTON.items():
            _PIN_TO_BUTTON.setdefault(_k, _v)
except Exception:
    pass


def setmode(mode):
    pass


def setwarnings(flag):
    pass


def setup(pin, direction, pull_up_down=None, initial=None):
    pass


def output(pin, value):
    pass


def input(pin):
    button = _PIN_TO_BUTTON.get(pin)
    if button is None:
        return 1
    if evdev_keys.is_pressed(button):
        return 0
    if rj_input is not None:
        try:
            if button in rj_input.get_held_buttons():
                return 0
        except Exception:
            pass
    return 1


def cleanup(channel=None):
    pass
