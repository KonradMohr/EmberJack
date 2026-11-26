#!/usr/bin/env python3
"""
EmberJack Payload -- Brightness Control
=======================================
Author: 7h30th3r0n3

Sleek Obsidian Ember backlight brightness slider with live adjustment,
percentage pill, and quick presets.

Controls:
  LEFT / -           -- Decrease brightness (-5%)
  RIGHT / +          -- Increase brightness (+5%)
  PgUp / PgDn        -- Coarse adjust (+/-15%)
  1 / 2 / 3 / 4      -- Quick presets (25% / 50% / 75% / 100%)
  OK / ENTER         -- Set to 100%
  KEY2 / P           -- Cycle presets (25 / 50 / 75 / 100%)
  KEY3 / ESC         -- Exit (keeps current brightness)
"""

import glob
import math
import os
import signal
import sys
import time

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import LCD_Config
from PIL import Image, ImageDraw

import rj_env
import rj_theme
from payloads._input_helper import (
    flush_input,
    format_footer_hint,
    is_action,
    is_back,
    is_confirm,
    is_step_down,
    is_step_up,
    poll_ui_input,
    text_input_mode,
)

ET = rj_theme.EMBER_THEME

PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}

BL_PATH = "/sys/class/backlight/backlight/brightness"
BL_MAX_PATH = "/sys/class/backlight/backlight/max_brightness"
PRESETS = (25, 50, 75, 100)

_running = True
_STUCK_PINS = set()


def _sig(_s, _f):
    global _running
    _running = False


signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)


# ---------------------------------------------------------------------------
# Backlight discovery & read/write
# ---------------------------------------------------------------------------

def _discover_backlight():
    """Return (device_name, brightness_path, max_brightness) or (None, None, 100)."""
    if os.path.exists(BL_PATH):
        max_b = 100
        try:
            with open(BL_MAX_PATH, "r", encoding="utf-8") as fh:
                max_b = max(1, int(fh.read().strip()))
        except Exception:
            max_b = 100
        return "backlight", BL_PATH, max_b

    for bpath in sorted(glob.glob("/sys/class/backlight/*/brightness")):
        dev_dir = os.path.dirname(bpath)
        dev_name = os.path.basename(dev_dir)
        max_b = 100
        try:
            with open(os.path.join(dev_dir, "max_brightness"), "r", encoding="utf-8") as fh:
                max_b = max(1, int(fh.read().strip()))
        except Exception:
            max_b = 100
        return dev_name, bpath, max_b

    return None, None, 100


def _get_brightness(bpath, max_b):
    try:
        with open(bpath, "r", encoding="utf-8") as fh:
            raw = int(fh.read().strip())
        if max_b == 100:
            return max(1, min(100, raw))
        return max(1, min(100, int(round((raw / max(1, max_b)) * 100))))
    except Exception:
        return 100


def _set_brightness(bpath, max_b, pct):
    pct = max(1, min(100, int(pct)))
    raw = pct if max_b == 100 else max(1, min(max_b, int(round((pct / 100.0) * max_b))))
    try:
        with open(bpath, "w", encoding="utf-8") as fh:
            fh.write(str(raw))
    except Exception:
        pass
    return pct


def _level_label(pct: int):
    if pct >= 90:
        return ("MAX", ET["accent_bright"])
    if pct >= 65:
        return ("HIGH", ET["accent"])
    if pct >= 35:
        return ("MED", ET["warn"])
    if pct >= 15:
        return ("LOW", ET["text_muted"])
    return ("DIM", ET["text_dim"])


# ---------------------------------------------------------------------------
# Obsidian Ember Adaptive Renderer
# ---------------------------------------------------------------------------

def _draw_sun_icon(d, cx, cy, radius, pct):
    """Draw a crisp geometric Ember sun icon whose glow scales with brightness."""
    col = ET["accent_bright"] if pct >= 50 else (ET["accent"] if pct >= 20 else ET["text_muted"])
    core_r = max(3, int(radius * 0.48))
    d.ellipse((cx - core_r, cy - core_r, cx + core_r, cy + core_r), fill=col)
    ray_inner = core_r + max(2, int(radius * 0.18))
    ray_outer = ray_inner + max(2, int(radius * (0.18 + 0.22 * (pct / 100.0))))
    w = 2 if radius >= 14 else 1
    for i in range(8):
        ang = i * (math.pi / 4.0)
        x0 = int(round(cx + math.cos(ang) * ray_inner))
        y0 = int(round(cy + math.sin(ang) * ray_inner))
        x1 = int(round(cx + math.cos(ang) * ray_outer))
        y1 = int(round(cy + math.sin(ang) * ray_outer))
        d.line([(x0, y0), (x1, y1)], fill=col, width=w)


def render_brightness_frame(width, height, brightness, dev_name="backlight", fonts=None):
    """Pure PIL native resolution-adaptive Obsidian Ember Brightness slider card."""
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = f["wide"]
    large = f["large"]
    is_240 = (not wide) and width >= 200

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    lvl_lbl, lvl_col = _level_label(brightness)
    hdr_h = rj_theme.draw_app_header(
        d, width, "BRIGHTNESS", f,
        badge=lvl_lbl,
        right_text=dev_name,
        right_pill=f"{brightness}%",
    )

    hint = format_footer_hint(
        width, height,
        hat_hint="L/R:Adj OK:100 K2:Pre K3:X",
        hat_240_hint="L/R:±5%  OK:100%  K2:Preset  K3:Exit",
        cardputer_hint="L/R or -/+:±5%  P/1-4:Preset  Enter:100%  Esc:Exit",
        hdmi_hint="Left/Right or -/+: ±5%  •  P / 1-4: Presets  •  Enter: 100%  •  Esc: Exit",
    )
    ftr_h = rj_theme.draw_app_footer(d, width, height, hint, f)

    pad = 14 if large else (6 if wide else (8 if is_240 else 4))
    body_h = height - hdr_h - ftr_h
    cw = min(width - pad * 2, 500 if large else width - pad * 2)
    ch = min(body_h - pad * 2, 224 if large else (114 if wide else (152 if is_240 else body_h - pad * 2)))
    cx0 = (width - cw) // 2
    cy0 = hdr_h + max(pad, (body_h - ch) // 2)
    cx1, cy1 = cx0 + cw, cy0 + ch

    rj_theme.draw_card(
        d, (cx0, cy0, cx1, cy1),
        fill=ET["bg_surface"], outline=ET["border_accent"],
        radius=10 if large else (6 if (wide or is_240) else 4),
        accent_left=ET["accent"], accent_width=4 if large else 2,
    )

    inner_x0 = cx0 + (22 if large else (12 if (wide or is_240) else 7))
    inner_x1 = cx1 - (22 if large else (12 if (wide or is_240) else 7))

    # Top row inside card: Sun icon + Big Percentage + Level Pill
    big_font = rj_env.load_font(44 if large else (24 if wide else (24 if is_240 else 16)), bold=True, mono=True)
    top_cy = cy0 + (54 if large else (26 if wide else (34 if is_240 else 24)))
    sun_r = 18 if large else (11 if wide else (12 if is_240 else 8))
    _draw_sun_icon(d, inner_x0 + sun_r + 2, top_cy, sun_r, brightness)

    pct_txt = f"{brightness}%"
    d.text((inner_x0 + sun_r * 2 + (18 if large else 10), top_cy), pct_txt, font=big_font, fill=ET["text_bright"], anchor="lm")

    # Right-side percentage & level pill
    pill_txt = f"{lvl_lbl} • {brightness}%" if wide else lvl_lbl
    pbb = rj_theme.measure_text(f["badge"], pill_txt, d)
    ppw = (pbb[2] - pbb[0]) + (12 if (wide or is_240) else 6)
    pph = (pbb[3] - pbb[1]) + (6 if large else 4)
    rj_theme.draw_pill(
        d, inner_x1 - ppw, top_cy - pph // 2, pill_txt, f["badge"],
        bg=ET["select_bg"], fg=lvl_col, outline=ET["border_accent"],
        pad_x=6 if (wide or is_240) else 3, pad_y=3 if large else 2, radius=4 if (wide or is_240) else 3,
    )

    # Slider track + progress fill + glowing Ember thumb knob
    bar_h = 18 if large else (12 if (wide or is_240) else 9)
    bar_y0 = cy0 + (112 if large else (56 if wide else (74 if is_240 else 48)))
    bar_y1 = bar_y0 + bar_h
    frac = brightness / 100.0
    rj_theme.draw_progress_bar(
        d, (inner_x0, bar_y0, inner_x1, bar_y1),
        fraction=frac, fill=ET["accent"], track=ET["bg_well"],
        outline=ET["border_subtle"], radius=bar_h // 2,
    )

    # Slider thumb knob
    knob_x = inner_x0 + int(round((inner_x1 - inner_x0) * frac))
    knob_x = max(inner_x0 + bar_h // 2, min(inner_x1 - bar_h // 2, knob_x))
    knob_y = (bar_y0 + bar_y1) // 2
    knob_r = (bar_h // 2) + (4 if large else 2)
    d.ellipse((knob_x - knob_r, knob_y - knob_r, knob_x + knob_r, knob_y + knob_r), fill=ET["accent_bright"], outline=ET["text_bright"], width=2 if large else 1)

    # Preset chips below slider (wide / large / 240x240)
    if wide or is_240:
        chip_y0 = bar_y1 + (24 if large else (12 if wide else 18))
        chip_h = 26 if large else (17 if wide else 20)
        if chip_y0 + chip_h <= cy1 - (10 if large else 4):
            gap = 10 if large else (6 if wide else 5)
            n_p = len(PRESETS)
            chip_w = (inner_x1 - inner_x0 - gap * (n_p - 1)) // n_p
            for idx, pval in enumerate(PRESETS):
                px0 = inner_x0 + idx * (chip_w + gap)
                px1 = inner_x1 if idx == n_p - 1 else (px0 + chip_w)
                active = abs(brightness - pval) <= 2
                rj_theme.draw_card(
                    d, (px0, chip_y0, px1, chip_y0 + chip_h),
                    fill=ET["select_bg"] if active else ET["bg_well"],
                    outline=ET["border_accent"] if active else ET["border_subtle"],
                    radius=5 if large else 3,
                )
                lbl = f"{idx + 1}: {pval}%" if large else f"{pval}%"
                d.text(
                    ((px0 + px1) // 2, chip_y0 + chip_h // 2),
                    lbl, font=f["badge"],
                    fill=ET["accent_bright"] if active else ET["text_muted"],
                    anchor="mm",
                )
    else:
        tick_y = bar_y1 + 6
        if tick_y + 10 < cy1:
            d.text((inner_x0, tick_y), "0%", font=f["badge"], fill=ET["text_dim"])
            d.text(((inner_x0 + inner_x1) // 2, tick_y), "50%", font=f["badge"], fill=ET["text_dim"], anchor="ma")
            d.text((inner_x1, tick_y), "100%", font=f["badge"], fill=ET["text_dim"], anchor="ra")

    return img


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _running, _STUCK_PINS

    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for p in PINS.values():
        GPIO.setup(p, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    time.sleep(0.08)
    _STUCK_PINS = {name for name, pin in PINS.items() if GPIO.input(pin) == 0}

    LCD_Config.GPIO_Init()
    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    lcd.LCD_Clear()
    W, H = lcd.width, lcd.height
    fonts = rj_theme.load_app_fonts(W, H)

    dev_name, bpath, max_b = _discover_backlight()

    try:
        with text_input_mode():
            flush_input()
            if not bpath:
                img = Image.new("RGB", (W, H), ET["bg_base"])
                d = ImageDraw.Draw(img)
                rj_theme.draw_app_header(d, W, "BRIGHTNESS", fonts, badge="N/A")
                rj_theme.draw_status_card(
                    d, W, H,
                    "No Backlight Control",
                    sub="No /sys/class/backlight device found on this display",
                    kind="warn",
                    foot="Press any key to exit",
                    fonts=fonts,
                )
                lcd.LCD_ShowImage(img, 0, 0)
                deadline = time.monotonic() + 3.5
                while _running and time.monotonic() < deadline:
                    a, c = poll_ui_input(PINS, GPIO)
                    if (a and a not in _STUCK_PINS) or c:
                        break
                    time.sleep(0.04)
                return 1

            brightness = _get_brightness(bpath, max_b)
            dirty = True

            while _running:
                if dirty:
                    lcd.LCD_ShowImage(render_brightness_frame(W, H, brightness, dev_name=dev_name, fonts=fonts), 0, 0)
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO)
                if act in _STUCK_PINS:
                    act = None
                if act is None and ch is None:
                    time.sleep(0.03)
                    continue

                if is_back(act):
                    break
                elif act == "RIGHT" or is_step_up(ch):
                    brightness = _set_brightness(bpath, max_b, brightness + 5)
                    dirty = True
                elif act == "LEFT" or is_step_down(ch):
                    brightness = _set_brightness(bpath, max_b, brightness - 5)
                    dirty = True
                elif act == "PAGEUP":
                    brightness = _set_brightness(bpath, max_b, brightness + 15)
                    dirty = True
                elif act == "PAGEDOWN":
                    brightness = _set_brightness(bpath, max_b, brightness - 15)
                    dirty = True
                elif is_confirm(act):
                    brightness = _set_brightness(bpath, max_b, 100)
                    dirty = True
                elif is_action(act, ch, "KEY2", "p"):
                    nxt = next((p for p in PRESETS if p > brightness + 2), PRESETS[0])
                    brightness = _set_brightness(bpath, max_b, nxt)
                    dirty = True
                elif ch in ("1", "2", "3", "4"):
                    brightness = _set_brightness(bpath, max_b, PRESETS[int(ch) - 1])
                    dirty = True
    finally:
        _running = False
        try:
            lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
            lcd.LCD_Clear()
        except Exception:
            pass
        try:
            GPIO.cleanup()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
