#!/usr/bin/env python3
"""
RaspyJack payload – Linux Shell
================================
Author: 7h30th3r0n3 (HDMI PTY mode added for PC/HDMI port)

- In HDMI mode: runs an interactive PTY-backed bash shell rendered directly on
  the HDMI display with full normal keyboard support (Shift, symbols, Tab,
  Arrows, Ctrl+C, Ctrl+D, Backspace; F3 or `exit` returns to RaspyJack).
- On CardputerZero: launches fbterm on /dev/fb1 as before.
"""

import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
from PIL import Image, ImageDraw
import LCD_1in44
import LCD_Config
import rj_env
import rj_theme
import rj_terminal
from payloads._display_helper import ScaledDraw, scaled_font
from payloads._input_helper import get_text_event, get_button, text_input_mode, flush_input


def _run_hdmi_pty_shell(lcd) -> int:
    """Interactive VT100/ANSI PTY terminal via shared rj_terminal engine."""
    pins = {
        "KEY_UP_PIN": 6,
        "KEY_DOWN_PIN": 19,
        "KEY_LEFT_PIN": 5,
        "KEY_RIGHT_PIN": 26,
        "KEY_PRESS_PIN": 13,
        "KEY1_PIN": 21,
        "KEY2_PIN": 20,
        "KEY3_PIN": 16,
    }
    for p in pins.values():
        try:
            GPIO.setup(p, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        except Exception:
            pass
    try:
        rj_terminal.run_terminal_ui(
            lcd,
            pins=pins,
            gpio=GPIO,
            persistent=False,
            cwd=rj_env.get_base_dir(),
        )
    finally:
        lcd.LCD_Clear()
        GPIO.cleanup()
    return 0


def main():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(16, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    LCD_Config.GPIO_Init()
    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)

    if getattr(lcd, "display_type", "") == "HDMI":
        return _run_hdmi_pty_shell(lcd)

    T = rj_theme.EMBER_THEME
    W, H = lcd.width, lcd.height
    font = scaled_font(9)

    img = Image.new("RGB", (W, H), T["bg_base"])
    d = ScaledDraw(img)
    # Check/install fbterm
    if subprocess.run(["which", "fbterm"], capture_output=True).returncode != 0:
        d = ScaledDraw(img)
        d.text((W // 2, 40), "Installing fbterm...", font=font, fill=T["accent_bright"], anchor="mm")
        lcd.LCD_ShowImage(img, 0, 0)
        r = subprocess.run(["sudo", "apt", "install", "-y", "fbterm"],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            img = Image.new("RGB", (W, H), T["bg_base"])
            d = ScaledDraw(img)
            d.text((W // 2, 40), "Install failed!", font=font, fill=T["danger"], anchor="mm")
            lcd.LCD_ShowImage(img, 0, 0)
            time.sleep(3)
            GPIO.cleanup()
            return 1
        img = Image.new("RGB", (W, H), T["bg_base"])

    # Load CardputerZero sym keymap
    kmap = "/usr/share/keymaps/tca8418_keypad_m5stack_keymap.map"
    if os.path.isfile(kmap):
        subprocess.run(["sudo", "loadkeys", kmap], capture_output=True)

    d = ScaledDraw(img)
    d.text((W // 2, 40), "Starting Shell...", font=font, fill=T["accent_bright"], anchor="mm")
    d.text((W // 2, 60), "KEY3 to exit", font=scaled_font(8), fill=T["text_muted"], anchor="mm")
    lcd.LCD_ShowImage(img, 0, 0)

    import threading
    import evdev_keys

    try:
        proc = subprocess.Popen(
            ["sudo", "openvt", "--switch", "--wait", "--",
             "bash", "-c",
             "FRAMEBUFFER=/dev/fb1 TERM=fbterm fbterm -s 10 -- bash --login"],
        )

        def _watch_key3():
            while proc.poll() is None:
                if evdev_keys.is_pressed("KEY3_PIN"):
                    proc.terminate()
                    subprocess.run(["sudo", "chvt", "1"], capture_output=True)
                    break
                time.sleep(0.1)

        threading.Thread(target=_watch_key3, daemon=True).start()
        proc.wait()
        subprocess.run(["sudo", "chvt", "1"], capture_output=True)
    except FileNotFoundError:
        img = Image.new("RGB", (W, H), T["bg_base"])
        d = ScaledDraw(img)
        d.text((W // 2, 40), "fbterm not found", font=font, fill=T["danger"], anchor="mm")
        d.text((W // 2, 60), "apt install fbterm", font=scaled_font(8), fill=T["text_muted"], anchor="mm")
        lcd.LCD_ShowImage(img, 0, 0)
        time.sleep(3)
        GPIO.cleanup()
        return 1
    except Exception as e:
        print(f"[shell] Error: {e}", file=sys.stderr)

    lcd.LCD_Clear()
    GPIO.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
