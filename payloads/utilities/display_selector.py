#!/usr/bin/env python3
"""
RaspyJack Payload -- Display Type Selector
-------------------------------------------
Switch between supported LCD screens from the device menu.
Changes take effect after service restart.

Controls:
  UP/DOWN  : Navigate
  OK       : Select and apply
  KEY3     : Cancel / Exit
"""

import os
import sys
import time
import json

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44, LCD_Config
from PIL import Image, ImageDraw, ImageFont
from payloads._display_helper import ScaledDraw, scaled_font
from payloads._input_helper import get_button

PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}
GPIO.setmode(GPIO.BCM)
for pin in PINS.values():
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

LCD = LCD_1in44.LCD()
LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
WIDTH, HEIGHT = LCD.width, LCD.height
font = scaled_font()
font_big = scaled_font(12)

import rj_env
import rj_theme

_T = rj_theme.EMBER_THEME
CONF_PATH = rj_env.get_config_path()

DISPLAY_OPTIONS = [
    ("HDMI", "HDMI 640x360 (16:9 HD)", (640, 360)),
    ("HDMI", "HDMI 320x180 (16:9 Classic)", (320, 180)),
    ("HDMI", "HDMI 480x480 (1:1 Square)", (480, 480)),
    ("ST7735_128", "1.44\" 128x128 HAT", None),
    ("ST7789_240", "1.3\"  240x240 HAT", None),
    ("CARDPUTER_320", "CardputerZero 320x170", None),
]


def _get_current_type():
    try:
        with open(CONF_PATH) as f:
            data = json.load(f)
        return data.get("DISPLAY", {}).get("type", "ST7735_128")
    except Exception:
        return rj_env.get_display_type()


def _set_display_type(dtype, res=None):
    try:
        with open(CONF_PATH) as f:
            data = json.load(f)
    except Exception:
        data = {}
    if "DISPLAY" not in data:
        data["DISPLAY"] = {}
    data["DISPLAY"]["type"] = dtype
    data["DISPLAY"]["supported_types"] = list(rj_env.SUPPORTED_DISPLAY_TYPES)
    if dtype == "HDMI" and res:
        hdmi = data["DISPLAY"].get("hdmi") or rj_env.get_hdmi_config()
        hdmi["width"], hdmi["height"] = res
        data["DISPLAY"]["hdmi"] = hdmi
    with open(CONF_PATH, "w") as f:
        json.dump(data, f, indent=4)


def _get_current_res():
    try:
        cfg = rj_env.get_hdmi_config()
        return (int(cfg["width"]), int(cfg["height"]))
    except Exception:
        return (640, 360)


def _draw(cursor, current_type, current_res=None):
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ScaledDraw(img)

    # Header
    d.rectangle((0, 0, 127, 15), fill=_T["bg_surface"])
    d.rectangle((0, 0, 2, 15), fill=_T["accent"])
    d.line((0, 15, 127, 15), fill=_T["border_accent"], width=1)
    d.text((6, 1), "Display Selector", font=font_big, fill=_T["text_bright"])

    # Scrollable option cards (4 visible at once)
    vis = 4
    start = max(0, min(cursor - 1, len(DISPLAY_OPTIONS) - vis))
    for row_i, opt_idx in enumerate(range(start, min(len(DISPLAY_OPTIONS), start + vis))):
        dtype, label, opt_res = DISPLAY_OPTIONS[opt_idx]
        y = 18 + row_i * 24
        if dtype == "HDMI":
            is_current = (current_type == "HDMI" and (current_res is None or opt_res == current_res))
        else:
            is_current = dtype == current_type
        is_selected = opt_idx == cursor

        if is_selected:
            d.rounded_rectangle((3, y - 1, 121, y + 20), radius=4, fill=_T["select_bg"], outline=_T["border_accent"])
            d.rectangle((3, y + 1, 5, y + 18), fill=_T["accent"])
        else:
            d.rounded_rectangle((3, y - 1, 121, y + 20), radius=4, fill=_T["bg_surface"], outline=_T["border_subtle"])

        title_col = _T["accent_bright"] if is_selected else _T["text_primary"]
        sub_col = _T["text_primary"] if is_selected else _T["text_muted"]
        d.text((9, y), dtype, font=font, fill=title_col)
        if is_current:
            d.text((95, y), "ACT", font=font, fill=_T["ok"])
        d.text((9, y + 10), label[:21], font=font, fill=sub_col)

    if len(DISPLAY_OPTIONS) > vis:
        rj_theme.draw_scrollbar(d, 124, 18, 112, start, vis, len(DISPLAY_OPTIONS), width=3)

    # Footer
    d.rectangle((0, 115, 127, 127), fill=_T["bg_well"])
    d.line((0, 115, 127, 115), fill=_T["border_subtle"], width=1)
    d.text((3, 117), "OK:Apply  K3:Cancel", font=font, fill=_T["text_muted"])

    LCD.LCD_ShowImage(img, 0, 0)


def main():
    current_type = _get_current_type()
    current_res = _get_current_res() if current_type == "HDMI" else None
    cursor = 0
    for i, (dtype, _, opt_res) in enumerate(DISPLAY_OPTIONS):
        if dtype == current_type and (dtype != "HDMI" or opt_res == current_res):
            cursor = i
            break

    dirty = True
    try:
        while True:
            if dirty:
                _draw(cursor, current_type, current_res)
                dirty = False
            btn = get_button(PINS, GPIO)

            if btn == "KEY3":
                break

            elif btn == "UP":
                cursor = (cursor - 1) % len(DISPLAY_OPTIONS)
                dirty = True

            elif btn == "DOWN":
                cursor = (cursor + 1) % len(DISPLAY_OPTIONS)
                dirty = True

            elif btn == "OK":
                selected_type, selected_label, selected_res = DISPLAY_OPTIONS[cursor]
                _set_display_type(selected_type, selected_res)
                img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
                d = ScaledDraw(img)
                d.rounded_rectangle((6, 22, 121, 106), radius=6, fill=_T["bg_surface"], outline=_T["border_accent"])
                d.rectangle((6, 26, 9, 102), fill=_T["accent"])
                d.text((64, 36), "Display Saved!", font=font_big, fill=_T["ok"], anchor="mm")
                d.text((64, 54), f"{selected_label[:20]}", font=font, fill=_T["text_bright"], anchor="mm")
                d.text((64, 74), "Restart required", font=font, fill=_T["accent_bright"], anchor="mm")
                d.text((64, 94), "OK:Restart  K3:Later", font=font, fill=_T["text_muted"], anchor="mm")
                LCD.LCD_ShowImage(img, 0, 0)

                while True:
                    btn2 = get_button(PINS, GPIO)
                    if btn2 == "OK":
                        img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
                        d = ScaledDraw(img)
                        d.rounded_rectangle((10, 44, 117, 84), radius=6, fill=_T["bg_surface"], outline=_T["border_accent"])
                        d.text((64, 64), "Restarting...", font=font_big, fill=_T["accent_bright"], anchor="mm")
                        LCD.LCD_ShowImage(img, 0, 0)
                        time.sleep(1)
                        os.system("sudo systemctl restart raspyjack")
                        return 0
                    elif btn2 == "KEY3":
                        break
                    time.sleep(0.035)
                break

            time.sleep(0.05)
    finally:
        LCD.LCD_Clear()
        GPIO.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
