#!/usr/bin/env python3
"""
EmberJack Payload -- Alarm Clock & Countdown Timer
==================================================
Author: 7h30th3r0n3

Crisp Obsidian Ember digital clock with configurable Alarm and
Countdown Timer cards. Flashes the screen and triggers an optional
audio chime when the alarm or timer fires until dismissed.

Controls:
  UP / DOWN    -- Alarm: Hour +/-1     | Timer: Minutes +/-1
  LEFT / RIGHT -- Alarm: Minute +/-1   | Timer: Seconds +/-15
  OK / ENTER   -- Alarm: Arm / Disarm  | Timer: Start / Pause
  KEY1 / TAB   -- Switch active card (Alarm <-> Timer)
  KEY2 / F     -- Toggle 12H / 24H format (Alarm card / keyboard F)
  KEY2 / R     -- Reset Timer (Timer card)
  KEY3 / ESC   -- Exit (or dismiss alert)
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

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
    poll_ui_input,
    text_input_mode,
)

ET = rj_theme.EMBER_THEME

PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}

CONFIG_DIR = os.path.join(rj_env.get_base_dir(), "loot", "Alarm")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
running = True
alarm_hour = 7
alarm_minute = 0
alarm_armed = False
use_24h = True
alarm_triggered = False
trigger_source = "ALARM"  # "ALARM" or "TIMER"

# Countdown timer state
timer_preset_secs = 300   # default 5:00
timer_remaining = 300.0
timer_running = False
timer_last_tick = 0.0
active_card = "alarm"     # "alarm" or "timer"


def cleanup(*_args):
    global running
    running = False


signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config():
    global alarm_hour, alarm_minute, alarm_armed, use_24h, timer_preset_secs, timer_remaining
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        alarm_hour = int(cfg.get("hour", 7)) % 24
        alarm_minute = int(cfg.get("minute", 0)) % 60
        alarm_armed = bool(cfg.get("armed", False))
        use_24h = bool(cfg.get("use_24h", True))
        if "timer_secs" in cfg:
            timer_preset_secs = max(5, min(35999, int(cfg["timer_secs"])))
            timer_remaining = float(timer_preset_secs)
    except Exception:
        pass


def save_config():
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        cfg = {
            "hour": alarm_hour,
            "minute": alarm_minute,
            "armed": alarm_armed,
            "use_24h": use_24h,
            "timer_secs": timer_preset_secs,
        }
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Time formatting & audio chime
# ---------------------------------------------------------------------------

def format_time_str(now, is_24h):
    if is_24h:
        return now.strftime("%H:%M:%S")
    return now.strftime("%I:%M:%S")


def format_ampm(now, is_24h):
    return "" if is_24h else now.strftime("%p")


def format_alarm_str(hour, minute, is_24h):
    if is_24h:
        return f"{hour:02d}:{minute:02d}"
    period = "AM" if hour < 12 else "PM"
    display_h = hour % 12 or 12
    return f"{display_h:02d}:{minute:02d} {period}"


def format_date_str(now):
    return now.strftime("%a, %b %d %Y")


def format_timer_str(secs: float) -> str:
    s = max(0, int(round(secs)))
    h = s // 3600
    m = (s % 3600) // 60
    rem = s % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{rem:02d}"
    return f"{m:02d}:{rem:02d}"


def time_until_alarm(now, hour, minute) -> str:
    now_m = now.hour * 60 + now.minute
    alm_m = hour * 60 + minute
    diff = (alm_m - now_m) % 1440
    if diff == 0:
        diff = 1440
    return f"in {diff // 60}h {diff % 60:02d}m"


def check_alarm():
    if not alarm_armed:
        return False
    now = datetime.now()
    return now.hour == alarm_hour and now.minute == alarm_minute


def _play_alert_beep():
    """Best-effort non-blocking alert beep (no external Python deps)."""
    def _worker():
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass
        try:
            if shutil.which("paplay"):
                for snd in (
                    "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
                    "/usr/share/sounds/freedesktop/stereo/bell.oga",
                ):
                    if os.path.isfile(snd):
                        subprocess.run(["paplay", snd], capture_output=True, timeout=2)
                        return
            if shutil.which("speaker-test"):
                subprocess.run(
                    ["speaker-test", "-t", "sine", "-f", "880", "-l", "1"],
                    capture_output=True, timeout=1,
                )
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Native Resolution-Adaptive Obsidian Ember Renderers
# ---------------------------------------------------------------------------

def _load_clock_fonts(width, height):
    base = rj_theme.load_app_fonts(width, height)
    large = base["large"]
    wide = base["wide"]
    if large:
        clock_px, card_px = 62, 26
    elif wide:
        clock_px, card_px = 30, 16
    elif width >= 200:
        clock_px, card_px = 34, 20
    else:
        clock_px, card_px = 20, 13
    base["clock"] = rj_env.load_font(clock_px, bold=True, mono=True)
    base["card_val"] = rj_env.load_font(card_px, bold=True, mono=True)
    return base


def render_clock_frame(width, height, fonts):
    """Render Obsidian Ember digital clock + Alarm & Timer cards."""
    now = datetime.now()
    wide = fonts["wide"]
    large = fonts["large"]
    is_240 = (not wide) and width >= 200
    split = width >= 460

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    fmt_lbl = "24H" if use_24h else "12H"
    hdr_h = rj_theme.draw_app_header(
        d, width, "EMBERJACK CLOCK" if wide else "CLOCK", fonts,
        badge=fmt_lbl,
        right_text=format_date_str(now),
        right_pill=("ALARM ARMED" if alarm_armed else ("TIMER ON" if timer_running else "")),
    )

    if active_card == "alarm":
        hint = format_footer_hint(
            width, height,
            hat_hint="OK:Arm K1:Tmr K2:Fmt K3:X",
            hat_240_hint="U/D:H L/R:M OK:Arm K1:Tmr K2:Fmt K3:X",
            cardputer_hint="U/D:Hr L/R:Min Enter:Arm Tab:Tmr F:Fmt Esc:X",
            hdmi_hint="Up/Dn:Hour  •  L/R:Min  •  Enter:Arm  •  Tab:Timer  •  F:12/24H  •  Esc:Exit",
        )
    else:
        hint = format_footer_hint(
            width, height,
            hat_hint="OK:Run K1:Alm K2:Rst K3:X",
            hat_240_hint="U/D:M L/R:S OK:Run K1:Alm K2:Rst K3:X",
            cardputer_hint="U/D:Min L/R:15s Enter:Run R:Rst Tab:Alm Esc:X",
            hdmi_hint="Up/Dn:Min  •  L/R:±15s  •  Enter:Start/Pause  •  R:Reset  •  Tab:Alarm  •  Esc:Exit",
        )
    ftr_h = rj_theme.draw_app_footer(d, width, height, hint, fonts, right_text=active_card.upper())

    pad = 12 if large else (5 if wide else (6 if is_240 else 3))
    y0 = hdr_h + (10 if large else (3 if wide else (5 if is_240 else 2)))
    y1 = height - ftr_h - (8 if large else (3 if wide else (5 if is_240 else 2)))

    # 1. Hero Digital Clock Card (clamped vertically for 320x170)
    hero_h = max(34, int((y1 - y0) * (0.48 if split else (0.45 if wide else 0.44))))
    hero_y1 = min(y1 - 40, y0 + hero_h)
    hero_h = max(24, hero_y1 - y0)
    rj_theme.draw_card(
        d, (pad, y0, width - pad, hero_y1),
        fill=ET["bg_surface"], outline=ET["border_subtle"],
        radius=8 if (wide or is_240) else 4, accent_left=ET["accent"], accent_width=4 if large else 2,
    )

    time_str = format_time_str(now, use_24h)
    ampm = format_ampm(now, use_24h)
    cx = width // 2
    cy = y0 + (hero_h - (18 if large else (9 if wide else (12 if is_240 else 6)))) // 2
    d.text((cx, cy), time_str, font=fonts["clock"], fill=ET["text_bright"], anchor="mm")
    if ampm:
        tbb = rj_theme.measure_text(fonts["clock"], time_str, d)
        tw = tbb[2] - tbb[0]
        ax = min(width - pad - 34, cx + tw // 2 + (10 if wide else 4))
        rj_theme.draw_pill(
            d, ax, cy - (10 if large else 6), ampm, fonts["badge"],
            bg=ET["select_bg"], fg=ET["accent_bright"], outline=ET["border_accent"],
            pad_x=5 if wide else 2, pad_y=1, radius=3,
        )

    # Seconds progress bar at bottom of hero card
    sec_frac = (now.second + now.microsecond / 1e6) / 60.0
    bar_x0 = pad + (16 if large else (10 if (wide or is_240) else 6))
    bar_x1 = width - pad - (16 if large else (10 if (wide or is_240) else 6))
    bar_h = 5 if large else 3
    rj_theme.draw_progress_bar(
        d, (bar_x0, hero_y1 - (12 if large else 7), bar_x1, hero_y1 - (12 if large else 7) + bar_h),
        fraction=sec_frac, fill=ET["accent"], track=ET["bg_well"], outline=None, radius=2,
    )

    # 2. Alarm & Timer Cards
    cards_y0 = hero_y1 + (10 if large else (4 if wide else (6 if is_240 else 3)))
    alarm_str = format_alarm_str(alarm_hour, alarm_minute, use_24h)
    timer_str = format_timer_str(timer_remaining)

    if split:
        gap = 10
        mid_x = width // 2
        # Left: Alarm Card
        a_focus = active_card == "alarm"
        rj_theme.draw_card(
            d, (pad, cards_y0, mid_x - gap // 2, y1),
            fill=ET["select_bg"] if a_focus else ET["bg_surface"],
            outline=ET["border_accent"] if a_focus else ET["border_subtle"],
            radius=8,
            accent_left=ET["ok"] if alarm_armed else (ET["accent"] if a_focus else ET["border_subtle"]),
            accent_width=4,
        )
        ax0 = pad + 14
        ax1 = mid_x - gap // 2 - 12
        d.text((ax0, cards_y0 + 10), "ALARM", font=fonts["badge"], fill=ET["accent_bright"] if a_focus else ET["text_muted"])
        st_lbl = "ARMED" if alarm_armed else "OFF"
        sbb = rj_theme.measure_text(fonts["badge"], st_lbl, d)
        rj_theme.draw_pill(
            d, ax1 - (sbb[2] - sbb[0] + 10), cards_y0 + 8, st_lbl, fonts["badge"],
            bg=ET["ok_bg"] if alarm_armed else ET["bg_well"],
            fg=ET["ok"] if alarm_armed else ET["text_dim"],
            outline=None, pad_x=5, pad_y=1, radius=3,
        )
        d.text((ax0, cards_y0 + 34), alarm_str, font=fonts["card_val"], fill=ET["text_bright"])
        sub_a = time_until_alarm(now, alarm_hour, alarm_minute) if alarm_armed else "Press Enter to arm"
        d.text((ax0, cards_y0 + 70), rj_theme.truncate_text(d, sub_a, ax1 - ax0, fonts["sub"]), font=fonts["sub"], fill=ET["ok"] if alarm_armed else ET["text_muted"])
        d.text((ax0, y1 - 20), "U/D: Hour  •  L/R: Minute", font=fonts["badge"], fill=ET["text_dim"])

        # Right: Timer Card
        t_focus = active_card == "timer"
        tx0_box = mid_x + gap // 2
        rj_theme.draw_card(
            d, (tx0_box, cards_y0, width - pad, y1),
            fill=ET["select_bg"] if t_focus else ET["bg_surface"],
            outline=ET["border_accent"] if t_focus else ET["border_subtle"],
            radius=8,
            accent_left=ET["accent_bright"] if timer_running else (ET["accent"] if t_focus else ET["border_subtle"]),
            accent_width=4,
        )
        tx0 = tx0_box + 14
        tx1 = width - pad - 12
        d.text((tx0, cards_y0 + 10), "COUNTDOWN TIMER", font=fonts["badge"], fill=ET["accent_bright"] if t_focus else ET["text_muted"])
        t_lbl = "RUNNING" if timer_running else ("PAUSED" if timer_remaining < timer_preset_secs else "READY")
        tbb = rj_theme.measure_text(fonts["badge"], t_lbl, d)
        rj_theme.draw_pill(
            d, tx1 - (tbb[2] - tbb[0] + 10), cards_y0 + 8, t_lbl, fonts["badge"],
            bg=ET["warn_bg"] if timer_running else ET["bg_well"],
            fg=ET["warn"] if timer_running else ET["text_muted"],
            outline=None, pad_x=5, pad_y=1, radius=3,
        )
        d.text((tx0, cards_y0 + 34), timer_str, font=fonts["card_val"], fill=ET["text_bright"])
        t_frac = timer_remaining / max(1.0, float(timer_preset_secs))
        rj_theme.draw_progress_bar(
            d, (tx0, cards_y0 + 72, tx1, cards_y0 + 80),
            fraction=t_frac, fill=ET["accent_bright"] if timer_running else ET["accent_dim"],
            track=ET["bg_well"], outline=ET["border_subtle"], radius=3,
        )
        d.text((tx0, y1 - 20), "U/D: Min  •  L/R: ±15s  •  R: Reset", font=fonts["badge"], fill=ET["text_dim"])
    else:
        # Compact single active card (Alarm or Timer, switch with K1 / Tab)
        is_alm = active_card == "alarm"
        cards_h = max(24, y1 - cards_y0)
        rj_theme.draw_card(
            d, (pad, cards_y0, width - pad, y1),
            fill=ET["select_bg"], outline=ET["border_accent"],
            radius=5 if (wide or is_240) else 3,
            accent_left=(ET["ok"] if alarm_armed else ET["accent"]) if is_alm else (ET["warn"] if timer_running else ET["accent"]),
            accent_width=3 if (wide or is_240) else 2,
        )
        cx0 = pad + (10 if (wide or is_240) else 5)
        cx1 = width - pad - (8 if (wide or is_240) else 4)
        title_s = "ALARM" if is_alm else "TIMER"
        st_lbl = ("ARMED" if alarm_armed else "OFF") if is_alm else ("RUN" if timer_running else "RDY")
        d.text((cx0, cards_y0 + (5 if (wide or is_240) else 3)), title_s, font=fonts["badge"], fill=ET["accent_bright"])
        sbb = rj_theme.measure_text(fonts["badge"], st_lbl, d)
        rj_theme.draw_pill(
            d, cx1 - (sbb[2] - sbb[0] + (8 if (wide or is_240) else 4)), cards_y0 + (4 if (wide or is_240) else 2), st_lbl, fonts["badge"],
            bg=ET["ok_bg"] if (is_alm and alarm_armed) else (ET["warn_bg"] if (not is_alm and timer_running) else ET["bg_well"]),
            fg=ET["ok"] if (is_alm and alarm_armed) else (ET["warn"] if (not is_alm and timer_running) else ET["text_muted"]),
            outline=None, pad_x=4 if (wide or is_240) else 2, pad_y=1, radius=3,
        )
        val_s = alarm_str if is_alm else timer_str
        val_y = cards_y0 + (18 if wide else (24 if is_240 else 14))
        d.text((cx0, val_y), val_s, font=fonts["card_val"], fill=ET["text_bright"])
        if is_240 and not is_alm and cards_h >= 78:
            t_frac = timer_remaining / max(1.0, float(timer_preset_secs))
            rj_theme.draw_progress_bar(
                d, (cx0, val_y + 28, cx1, val_y + 34),
                fraction=t_frac, fill=ET["accent_bright"] if timer_running else ET["accent_dim"],
                track=ET["bg_well"], outline=ET["border_subtle"], radius=3,
            )
        if (wide or is_240) and cards_h >= 52:
            if is_alm:
                sub_s = time_until_alarm(now, alarm_hour, alarm_minute) if alarm_armed else ("OK: Arm • K1: Timer" if is_240 else "Enter: Arm • Tab: Timer")
            else:
                sub_s = "OK: Run • K2: Reset • K1: Alarm" if is_240 else "Enter: Run • R: Reset • Tab: Alarm"
            sub_y = y1 - (14 if is_240 else 13)
            if sub_y > val_y + 14:
                d.text((cx0, sub_y), rj_theme.truncate_text(d, sub_s, cx1 - cx0, fonts["sub"]), font=fonts["sub"], fill=ET["text_muted"])

    return img


def render_alarm_flash(width, height, fonts, flash_state):
    """Render high-contrast Obsidian Ember alarm/timer alert flash screen."""
    now = datetime.now()
    bg_col = ET["accent"] if flash_state else ET["bg_base"]
    card_bg = ET["bg_base"] if flash_state else ET["danger_bg"]
    fg_col = ET["text_bright"] if flash_state else ET["accent_bright"]

    img = Image.new("RGB", (width, height), bg_col)
    d = ImageDraw.Draw(img)

    pad = 18 if fonts["large"] else (10 if fonts["wide"] else 5)
    rj_theme.draw_card(
        d, (pad, pad, width - pad, height - pad),
        fill=card_bg, outline=ET["accent_bright"] if not flash_state else ET["text_bright"],
        radius=10 if fonts["wide"] else 5, width=2,
    )

    label = "ALARM TRIGGERED!" if trigger_source == "ALARM" else "TIMER COMPLETE!"
    d.text((width // 2, int(height * 0.28)), rj_theme.truncate_text(d, label, width - pad * 2 - 12, fonts["title"]), font=fonts["title"], fill=fg_col, anchor="mm")
    d.text((width // 2, int(height * 0.54)), format_time_str(now, use_24h), font=fonts["clock"], fill=ET["text_bright"], anchor="mm")
    dismiss_txt = format_footer_hint(
        width, height,
        hat_hint="Any btn to dismiss",
        hat_240_hint="Press any button to dismiss",
        cardputer_hint="Press any key to dismiss",
        hdmi_hint="Press any key or button to dismiss",
    )
    d.text((width // 2, int(height * 0.80)), rj_theme.truncate_text(d, dismiss_txt, width - pad * 2 - 12, fonts["sub"]), font=fonts["sub"], fill=ET["text_muted"], anchor="mm")
    return img


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global running, alarm_hour, alarm_minute, alarm_armed, use_24h
    global alarm_triggered, trigger_source, active_card
    global timer_preset_secs, timer_remaining, timer_running, timer_last_tick

    GPIO.setmode(GPIO.BCM)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    LCD_Config.GPIO_Init()
    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    lcd.LCD_Clear()
    W, H = lcd.width, lcd.height
    fonts = _load_clock_fonts(W, H)

    load_config()

    last_trigger_minute = -1
    flash_state = False
    last_flash = 0.0
    last_beep = 0.0
    last_draw = 0.0
    dirty = True

    try:
        with text_input_mode():
            flush_input()
            while running:
                now_m = time.monotonic()

                # Advance countdown timer if running
                if timer_running:
                    dt = now_m - timer_last_tick
                    timer_last_tick = now_m
                    timer_remaining = max(0.0, timer_remaining - dt)
                    if timer_remaining <= 0.0:
                        timer_running = False
                        alarm_triggered = True
                        trigger_source = "TIMER"
                        last_beep = 0.0
                        dirty = True

                # Alarm / Timer ringing mode
                if alarm_triggered:
                    act, ch = poll_ui_input(PINS, GPIO)
                    if act is not None or ch is not None:
                        alarm_triggered = False
                        if trigger_source == "TIMER":
                            timer_remaining = float(timer_preset_secs)
                        flush_input()
                        dirty = True
                        continue
                    if (now_m - last_flash) >= 0.35:
                        flash_state = not flash_state
                        last_flash = now_m
                        lcd.LCD_ShowImage(render_alarm_flash(W, H, fonts, flash_state), 0, 0)
                    if (now_m - last_beep) >= 2.0:
                        last_beep = now_m
                        _play_alert_beep()
                    time.sleep(0.04)
                    continue

                # Check alarm trigger (once per minute)
                now_dt = datetime.now()
                cur_min = now_dt.hour * 60 + now_dt.minute
                if cur_min != last_trigger_minute and check_alarm():
                    alarm_triggered = True
                    trigger_source = "ALARM"
                    last_trigger_minute = cur_min
                    last_beep = 0.0
                    continue

                if dirty or (now_m - last_draw) >= 0.25:
                    lcd.LCD_ShowImage(render_clock_frame(W, H, fonts), 0, 0)
                    last_draw = now_m
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO)
                if act is None and ch is None:
                    time.sleep(0.04)
                    continue

                kl = ch.lower() if isinstance(ch, str) else ""
                if is_back(act):
                    break
                elif act in ("KEY1", "TAB"):
                    active_card = "timer" if active_card == "alarm" else "alarm"
                    dirty = True
                elif active_card == "alarm":
                    if act == "UP":
                        alarm_hour = (alarm_hour + 1) % 24
                        save_config()
                        dirty = True
                    elif act == "DOWN":
                        alarm_hour = (alarm_hour - 1) % 24
                        save_config()
                        dirty = True
                    elif act == "LEFT":
                        alarm_minute = (alarm_minute - 1) % 60
                        save_config()
                        dirty = True
                    elif act == "RIGHT":
                        alarm_minute = (alarm_minute + 1) % 60
                        save_config()
                        dirty = True
                    elif is_confirm(act):
                        alarm_armed = not alarm_armed
                        save_config()
                        dirty = True
                    elif is_action(act, ch, "KEY2", "f"):
                        use_24h = not use_24h
                        save_config()
                        dirty = True
                else:
                    # active_card == "timer"
                    if act == "UP":
                        timer_preset_secs = min(35999, timer_preset_secs + 60)
                        if not timer_running:
                            timer_remaining = float(timer_preset_secs)
                        save_config()
                        dirty = True
                    elif act == "DOWN":
                        timer_preset_secs = max(15, timer_preset_secs - 60)
                        if not timer_running:
                            timer_remaining = float(timer_preset_secs)
                        save_config()
                        dirty = True
                    elif act == "RIGHT":
                        timer_preset_secs = min(35999, timer_preset_secs + 15)
                        if not timer_running:
                            timer_remaining = float(timer_preset_secs)
                        save_config()
                        dirty = True
                    elif act == "LEFT":
                        timer_preset_secs = max(5, timer_preset_secs - 15)
                        if not timer_running:
                            timer_remaining = float(timer_preset_secs)
                        save_config()
                        dirty = True
                    elif is_confirm(act):
                        if timer_remaining <= 0:
                            timer_remaining = float(timer_preset_secs)
                        timer_running = not timer_running
                        timer_last_tick = time.monotonic()
                        dirty = True
                    elif is_action(act, ch, "KEY2", "r"):
                        timer_running = False
                        timer_remaining = float(timer_preset_secs)
                        dirty = True
                    elif kl == "f":
                        use_24h = not use_24h
                        save_config()
                        dirty = True
    finally:
        running = False
        try:
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
