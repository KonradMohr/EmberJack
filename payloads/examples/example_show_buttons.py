#!/usr/bin/env python3
"""
RaspyJack *payload* example – **Show Buttons**
=============================================
This script lives in the ``payloads/`` folder of the RaspyJack project.
It demonstrates how to:

1. **Read** the on‑board joystick (UP / DOWN / LEFT / RIGHT / OK) **and** the
   three extra push‑buttons (KEY1 / KEY2 / KEY3) present on most Waveshare
   1.44‑inch LCD HATs.
2. **Display** the name of the button currently being pressed, centred on the
   LCD in bright green text.
3. **Exit cleanly** when:
   * the user presses **KEY3** (bottom‑right button) – *new feature*;
   * the user hits *Ctrl‑C* in the terminal;
   * RaspyJack UI sends a *SIGTERM* signal when the payload is stopped from the
     menu.

The code is **heavily commented** so that an absolute Python beginner can read
and understand every step.
"""

# ---------------------------------------------------------------------------
# 0) Make sure we can import local helper modules when launched directly
# ---------------------------------------------------------------------------
import os, sys
# «…/Raspyjack/» is two directories up from this script. Add it to sys.path so
# that `import LCD_1in44` works even when we run the script manually from
# inside the “payloads” folder.
sys.path.append(os.path.abspath(os.path.join(__file__, '..', '..', '..')))

# ---------------------------- Standard library ----------------------------
import time           # sleep() for timing / debouncing
import signal         # capture Ctrl‑C (SIGINT) & termination (SIGTERM)
import sys            # print exceptions to stderr

# ----------------------------- Third‑party libs ---------------------------
# These come pre‑installed on RaspyJack; on a vanilla Pi OS you’d need:
#   sudo apt install python3-pil python3-rpi.gpio
import RPi.GPIO as GPIO               # Raspberry Pi GPIO access
import LCD_1in44, LCD_Config          # Waveshare driver helpers for the LCD
from PIL import Image, ImageDraw
from payloads._display_helper import scaled_font
from payloads._input_helper import get_button, format_footer_hint

# ---------------------------------------------------------------------------
# 1) GPIO pin mapping (BCM numbering) – tweak here if your wiring differs
# ---------------------------------------------------------------------------
# Keys are logical names, values are BCM GPIO numbers on the Pi header.
# All buttons are **active‑LOW**: they read 0 V (logic 0) when pressed.
PINS: dict[str, int] = {
    "UP"   : 6,
    "DOWN" : 19,
    "LEFT" : 5,
    "RIGHT": 26,
    "OK"   : 13,   # joystick centre push
    "KEY1" : 21,
    "KEY2" : 20,
    "KEY3" : 16,   # ← acts as «Back to menu»
}

# ---------------------------------------------------------------------------
# 2) GPIO initialisation
# ---------------------------------------------------------------------------
GPIO.setmode(GPIO.BCM)  # use BCM numbers rather than physical pin numbers
for pin in PINS.values():
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

# ---------------------------------------------------------------------------
# 3) LCD initialisation
# ---------------------------------------------------------------------------
LCD = LCD_1in44.LCD()                     # create driver instance
LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)      # default scan direction (portrait)
WIDTH, HEIGHT = LCD.width, LCD.height     # native resolution (128/240/320x170/640x360)
font = scaled_font(12 if WIDTH >= 200 else 10)
font_sm = scaled_font(9 if WIDTH >= 200 else 7)

# ---------------------------------------------------------------------------
# 4) Helper: draw centred text natively on any screen geometry
# ---------------------------------------------------------------------------

def draw(text: str) -> None:
    """Clear the screen and draw *text* centred natively at any resolution."""
    img = Image.new("RGB", (WIDTH, HEIGHT), "#0E0E10")
    d = ImageDraw.Draw(img)

    hdr_h = max(14, int(HEIGHT * 0.11))
    ftr_h = max(13, int(HEIGHT * 0.10))
    d.rectangle((0, 0, WIDTH - 1, hdr_h - 1), fill="#1F1E22")
    d.text((WIDTH // 2, hdr_h // 2), "BUTTON TEST", font=font_sm, fill="#F2994A", anchor="mm")

    d.text((WIDTH // 2, (hdr_h + HEIGHT - ftr_h) // 2), text, font=font, fill="#E6DFD7", anchor="mm")

    d.rectangle((0, HEIGHT - ftr_h, WIDTH - 1, HEIGHT - 1), fill="#1F1E22")
    hint = format_footer_hint(
        WIDTH, HEIGHT,
        hat_hint="K3:Exit",
        hat_240_hint="Press any key  K3:Exit",
        cardputer_hint="Press any key  Esc:Exit",
        hdmi_hint="Press any button or key   •   Esc: Exit",
    )
    d.text((WIDTH // 2, HEIGHT - ftr_h // 2), hint, font=font_sm, fill="#9B928A", anchor="mm")
    LCD.LCD_ShowImage(img, 0, 0)

# ---------------------------------------------------------------------------
# 5) Graceful shutdown – SIGINT/SIGTERM & KEY3
# ---------------------------------------------------------------------------
running = True  # global flag for the main loop


def cleanup(*_):
    """Signal handler: stop the main loop so `finally` can clean up."""
    global running
    running = False

# Register handlers for Ctrl‑C and external termination
signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

# ---------------------------------------------------------------------------
# 6) Main loop – poll buttons & update display
# ---------------------------------------------------------------------------
try:
    draw("Ready!")

    while running:
        pressed: str | None = get_button(PINS, GPIO)  # name of the button pressed

        # 6.2 – act on the result
        if pressed:
            if pressed == "KEY3":  # ← user wants to go back to RaspyJack
                running = False    # leave the main loop → exit script
                break

            # display which button was pressed
            draw(f"{pressed} pressed")

            # wait until the same button is released (basic debouncing)
            while GPIO.input(PINS[pressed]) == 0 and running:
                time.sleep(0.05)
        else:
            # no button activity → small sleep to reduce CPU usage
            time.sleep(0.05)

except Exception as exc:
    # Log unexpected errors
    print(f"[ERROR] {exc}", file=sys.stderr)

finally:
    # -----------------------------------------------------------------------
    # 7) Always executed: clear the screen and release GPIO resources
    # -----------------------------------------------------------------------
    LCD.LCD_Clear()   # avoid leaving ghost images on the display
    GPIO.cleanup()    # reset all GPIO pins to a safe state

