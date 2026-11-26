#!/usr/bin/env python3
"""
RaspyJack *payload* – **Show USB Keyboard Key**
==============================================
Displays the name of the key pressed on a USB keyboard connected to the
Raspberry Pi. It listens to the first detected *evdev* keyboard device and
shows the key name centred in bright green on the Waveshare 1.44‑inch LCD.

The script exits cleanly when:
* **ESC** is pressed on the keyboard;
* **KEY3** (bottom‑right button) is pressed on the HAT;
* the user hits *Ctrl‑C* in the terminal;
* RaspyJack UI sends a *SIGTERM* signal.

Make sure the runtime dependency is installed:

```bash
sudo apt install python3-evdev
```
"""

# ---------------------------------------------------------------------------
# 0) Ensure we can import RaspyJack helpers when launched directly
# ---------------------------------------------------------------------------
import os, sys, time, signal, select, fcntl
sys.path.append(os.path.abspath(os.path.join(__file__, '..', '..', '..')))

# ---------------------------- Third‑party libs -----------------------------
import LCD_1in44, LCD_Config          # Waveshare LCD driver
import rj_theme
from PIL import Image, ImageDraw, ImageFont
from payloads._display_helper import ScaledDraw, scaled_font

_T = rj_theme.EMBER_THEME
_KEY_HISTORY = []
try:
    from evdev import InputDevice, categorize, ecodes, list_devices
except ImportError:
    InputDevice = categorize = ecodes = list_devices = None
import RPi.GPIO as GPIO               # Raspberry Pi GPIO access

# Shared input helper (WebUI virtual + GPIO)
from payloads._input_helper import get_button

# ---------------------------------------------------------------------------
# 1) GPIO initialisation (only KEY3 used for «Back to menu»)
# ---------------------------------------------------------------------------
KEY3_PIN = 16                         # BCM pin number
GPIO.setmode(GPIO.BCM)
GPIO.setup(KEY3_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

# ---------------------------------------------------------------------------
# 2) LCD initialisation
# ---------------------------------------------------------------------------
LCD = LCD_1in44.LCD()
LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
WIDTH, HEIGHT = LCD.width, LCD.height
font = scaled_font(12, bold=True)
font_sm = scaled_font(8)


def draw(text: str, record: bool = True) -> None:
    """Render Obsidian Ember live keyboard visualizer card with key history."""
    if record and text:
        _KEY_HISTORY.append(text)
        if len(_KEY_HISTORY) > 5:
            del _KEY_HISTORY[:-5]
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ScaledDraw(img)

    # Header
    d.rectangle((0, 0, 127, 15), fill=_T["bg_surface"])
    d.rectangle((0, 0, 2, 15), fill=_T["accent"])
    d.line((0, 15, 127, 15), fill=_T["border_accent"], width=1)
    d.text((6, 2), "Keyboard Tester", font=font_sm, fill=_T["text_bright"])
    d.text((100, 2), "HID", font=font_sm, fill=_T["accent_bright"])

    # Active key well card
    d.rounded_rectangle((6, 22, 121, 74), radius=6, fill=_T["bg_surface"], outline=_T["border_accent"])
    d.text((64, 30), "LAST KEY", font=font_sm, fill=_T["text_muted"], anchor="mt")
    d.text((64, 52), text[:18], font=font, fill=_T["accent_bright"], anchor="mm")

    # History pill strip
    d.rounded_rectangle((6, 80, 121, 108), radius=4, fill=_T["bg_well"], outline=_T["border_subtle"])
    hist = " · ".join(_KEY_HISTORY[-5:]) if _KEY_HISTORY else "Waiting for keypress..."
    d.text((64, 94), hist[:26], font=font_sm, fill=_T["text_primary"] if _KEY_HISTORY else _T["text_muted"], anchor="mm")

    # Footer
    d.rectangle((0, 115, 127, 127), fill=_T["bg_well"])
    d.line((0, 115, 127, 115), fill=_T["border_subtle"], width=1)
    d.text((3, 117), "Type any key  Esc/K3:Exit", font=font_sm, fill=_T["text_muted"])
    LCD.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# 3) Helper: find the first keyboard‑like input device
# ---------------------------------------------------------------------------

def find_keyboard() -> InputDevice:
    """Return the first real keyboard /dev/input/event* device."""
    import evdev_keys
    paths = evdev_keys.discover_keyboard_devices()
    if paths:
        return InputDevice(paths[0])
    for path in list_devices():
        dev = InputDevice(path)
        if ecodes.EV_KEY in dev.capabilities():
            return dev
    raise RuntimeError("No keyboard event device found")


# ---------------------------------------------------------------------------
# 4) Graceful shutdown – SIGINT/SIGTERM & KEY3/ESC
# ---------------------------------------------------------------------------

running = True  # global flag for the main loop


def cleanup(*_):
    global running
    running = False


signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

# ---------------------------------------------------------------------------
# 5) Main loop – poll keyboard & update display
# ---------------------------------------------------------------------------

keyboard = None
try:
    if getattr(LCD, "display_type", "") == "HDMI":
        from payloads._input_helper import get_text_event, text_input_mode
        draw("Ready", record=False)
        with text_input_mode():
            while running:
                if get_button({"KEY3": KEY3_PIN}, GPIO) == "KEY3":
                    break
                while running:
                    ev = get_text_event()
                    if not ev:
                        break
                    spec = str(ev.get("special") or "")
                    key = str(ev.get("key") or "")
                    label = spec if spec else repr(key)
                    draw(label)
                    if spec == "ESCAPE":
                        running = False
                time.sleep(0.03)
    else:
        keyboard = find_keyboard()

        # -----------------------------------------------------------------------
        # Make the device *non‑blocking* – works across all evdev versions
        # -----------------------------------------------------------------------
        if hasattr(keyboard, "set_blocking"):
            keyboard.set_blocking(False)        # evdev ≥ 1.5
        elif hasattr(keyboard, "setblocking"):
            keyboard.setblocking(False)         # very old evdev (< 1.3)
        else:
            # Fallback: set O_NONBLOCK flag on the file descriptor manually
            flags = fcntl.fcntl(keyboard.fd, fcntl.F_GETFL)
            fcntl.fcntl(keyboard.fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        draw("Ready", record=False)

        poller = select.poll()
        poller.register(keyboard.fd, select.POLLIN)

        while running:
            # Alternate exit: KEY3 on the HAT acts as «Back to menu»
            if get_button({"KEY3": KEY3_PIN}, GPIO) == "KEY3":
                break

            # Poll keyboard with 50 ms timeout
            if poller.poll(50):
                for event in keyboard.read():
                    if event.type == ecodes.EV_KEY:
                        key_event = categorize(event)
                        if key_event.keystate == key_event.key_down:
                            key_name = (
                                key_event.keycode
                                if isinstance(key_event.keycode, str)
                                else key_event.keycode[0]
                            )
                            draw(str(key_name))
                            if key_name in ("KEY_ESC", "ESC"):
                                running = False

            time.sleep(0.01)  # small sleep to reduce CPU usage

except Exception as exc:
    # Log unexpected errors
    print(f"[ERROR] {exc}", file=sys.stderr)

finally:
    # -----------------------------------------------------------------------
    # 6) Always executed: clear the screen and release resources
    # -----------------------------------------------------------------------
    LCD.LCD_Clear()
    GPIO.cleanup()
    try:
        if keyboard is not None:
            keyboard.close()
    except Exception:
        pass
