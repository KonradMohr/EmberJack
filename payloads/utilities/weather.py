#!/usr/bin/env python3
"""
EmberJack Payload -- Weather Display
=====================================
Author: 7h30th3r0n3

Fetches and displays current weather conditions and 3-day forecast from wttr.in
in a native resolution-adaptive Obsidian Ember layout.

Controls:
  UP / DOWN         -- Scroll details / forecast
  OK / Enter / U    -- Toggle °C / °F units
  KEY1 / R          -- Refresh weather data
  KEY2 / C          -- Change city (lcd_keyboard)
  KEY3 / Esc / Q    -- Exit

Config: <base_dir>/loot/Weather/config.json
"""

from datetime import datetime
import json
import math
import os
import signal
import subprocess
import sys
import time
import urllib.parse

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import LCD_Config
from PIL import Image, ImageDraw

import rj_env
import rj_theme
from payloads._input_helper import flush_input, poll_ui_input, text_input_mode
from payloads._keyboard_helper import lcd_keyboard

try:
    import rj_keyboard as _rj_keyboard
except Exception:
    _rj_keyboard = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}
CONFIG_DIR = os.path.join(rj_env.get_base_dir(), "loot", "Weather")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
running = True
city = "Paris"
unit = "C"  # "C" or "F"
weather_lines = ["Press KEY1 / R to fetch"]
weather_data = None
scroll_offset = 0


def cleanup(*_args):
    global running
    running = False


signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def load_config():
    global city, unit
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        city = str(cfg.get("city", "Paris") or "Paris")
        u = str(cfg.get("unit", "C") or "C").upper()
        unit = "F" if u == "F" else "C"
    except Exception:
        city = "Paris"
        unit = "C"


def save_config():
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"city": city, "unit": unit}, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Weather fetching (rich wttr.in JSON + plain-text fallback)
# ---------------------------------------------------------------------------
def _classify_icon(desc):
    low = str(desc or "").lower()
    if any(k in low for k in ("thunder", "storm", "lightning")):
        return "storm"
    if any(k in low for k in ("snow", "sleet", "blizzard", "ice", "freezing")):
        return "snow"
    if any(k in low for k in ("rain", "drizzle", "shower")):
        return "rain"
    if any(k in low for k in ("fog", "mist", "haze")):
        return "fog"
    if any(k in low for k in ("partly", "scattered", "few clouds")):
        return "partly"
    if any(k in low for k in ("cloud", "overcast")):
        return "cloud"
    return "sun"


def _day_label(date_str, idx):
    if idx == 0:
        return "Today"
    if idx == 1:
        return "Tomorrow"
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%a %d")
    except Exception:
        return date_str[-5:] if date_str else f"Day {idx + 1}"


def fetch_weather(location):
    """Fetch weather from wttr.in using curl. Populates weather_data and returns lines."""
    global weather_data
    enc = urllib.parse.quote(str(location or "Paris").strip() or "Paris")

    # 1. Try structured JSON first for rich hero card + 3-day forecast strip
    try:
        res_j = subprocess.run(
            ["curl", "-s", "--max-time", "12", f"https://wttr.in/{enc}?format=j1"],
            capture_output=True, text=True, timeout=15,
        )
        raw_j = (res_j.stdout or "").strip()
        if raw_j.startswith("{"):
            payload = json.loads(raw_j)
            cur_list = payload.get("current_condition") or []
            if cur_list:
                cc = cur_list[0]
                area_list = payload.get("nearest_area") or [{}]
                area = area_list[0] if area_list else {}
                area_name = (area.get("areaName") or [{}])[0].get("value") or location
                country = (area.get("country") or [{}])[0].get("value") or ""
                desc = (cc.get("weatherDesc") or [{}])[0].get("value") or "Unknown"

                forecast = []
                for idx, day in enumerate((payload.get("weather") or [])[:3]):
                    hourly = day.get("hourly") or []
                    mid = hourly[len(hourly) // 2] if hourly else {}
                    f_desc = (mid.get("weatherDesc") or [{}])[0].get("value") or desc
                    rain_ch = mid.get("chanceofrain") or "0"
                    forecast.append({
                        "label": _day_label(day.get("date", ""), idx),
                        "max_c": str(day.get("maxtempC", "?")),
                        "min_c": str(day.get("mintempC", "?")),
                        "max_f": str(day.get("maxtempF", "?")),
                        "min_f": str(day.get("mintempF", "?")),
                        "desc": f_desc,
                        "icon": _classify_icon(f_desc),
                        "rain": f"{rain_ch}%",
                    })

                weather_data = {
                    "ok": True,
                    "location": area_name,
                    "country": country,
                    "temp_c": str(cc.get("temp_C", "?")),
                    "temp_f": str(cc.get("temp_F", "?")),
                    "feels_c": str(cc.get("FeelsLikeC", "?")),
                    "feels_f": str(cc.get("FeelsLikeF", "?")),
                    "desc": desc,
                    "icon": _classify_icon(desc),
                    "humidity": f"{cc.get('humidity', '?')}%",
                    "wind_kmph": f"{cc.get('windspeedKmph', '?')} km/h",
                    "wind_mph": f"{cc.get('windspeedMiles', '?')} mph",
                    "wind_dir": str(cc.get("winddir16Point", "")),
                    "precip": f"{cc.get('precipMM', '0.0')} mm",
                    "pressure": f"{cc.get('pressure', '?')} hPa",
                    "uv": str(cc.get("uvIndex", "?")),
                    "updated": datetime.now().strftime("%H:%M"),
                    "forecast": forecast,
                    "raw": "",
                }
                lines = [
                    f"{area_name}: {desc}",
                    f"Temp: {weather_data['temp_c']}C (Feels {weather_data['feels_c']}C)",
                    f"Wind: {weather_data['wind_kmph']} {weather_data['wind_dir']}",
                    f"Humidity: {weather_data['humidity']}  UV: {weather_data['uv']}",
                    f"Precip: {weather_data['precip']}  {weather_data['pressure']}",
                ]
                for fday in forecast:
                    lines.append(f"{fday['label']}: {fday['min_c']}..{fday['max_c']}C {fday['desc']}")
                return lines
    except Exception:
        pass

    # 2. Fallback to original wttr.in single-line format
    weather_data = None
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "12", f"wttr.in/{enc}?format=%l:+%c+%t+%w+%h+%p"],
            capture_output=True, text=True, timeout=15,
        )
        raw = (result.stdout or "").strip()
        if not raw or "Unknown location" in raw:
            weather_data = {"ok": False, "error": f"City '{location}' not found or offline"}
            return [f"City: {location}", "Not found or offline"]
        weather_data = {
            "ok": True,
            "location": location,
            "country": "",
            "temp_c": "",
            "temp_f": "",
            "feels_c": "",
            "feels_f": "",
            "desc": raw,
            "icon": _classify_icon(raw),
            "humidity": "-",
            "wind_kmph": "-",
            "wind_mph": "-",
            "wind_dir": "",
            "precip": "-",
            "pressure": "-",
            "uv": "-",
            "updated": datetime.now().strftime("%H:%M"),
            "forecast": [],
            "raw": raw,
        }
        return [ln for ln in raw.splitlines() if ln.strip()] or ["No data"]
    except Exception as exc:
        weather_data = {"ok": False, "error": str(exc)[:48]}
        return [f"Error: {str(exc)[:30]}"]


# ---------------------------------------------------------------------------
# Vector Weather Icon Drawing (PIL primitives in Obsidian Ember palette)
# ---------------------------------------------------------------------------
def _draw_weather_icon(d, cx, cy, size, icon_kind):
    ET = rj_theme.EMBER_THEME
    r = max(6, size // 2)

    def _draw_sun(sx, sy, sr):
        d.ellipse((sx - sr, sy - sr, sx + sr, sy + sr), fill=ET["accent_bright"], outline=ET["warn"])
        ray_in = sr + max(2, sr // 4)
        ray_out = sr + max(5, sr // 2)
        for deg in range(0, 360, 45):
            rad = math.radians(deg)
            d.line(
                [(sx + int(math.cos(rad) * ray_in), sy + int(math.sin(rad) * ray_in)),
                 (sx + int(math.cos(rad) * ray_out), sy + int(math.sin(rad) * ray_out))],
                fill=ET["warn"], width=max(1, size // 18),
            )

    def _draw_cloud(kx, ky, kw, kh, fill=ET["text_primary"]):
        d.ellipse((kx - kw // 2, ky - kh // 3, kx - kw // 10, ky + kh // 2), fill=fill)
        d.ellipse((kx - kw // 3, ky - kh // 2, kx + kw // 4, ky + kh // 3), fill=fill)
        d.ellipse((kx, ky - kh // 3, kx + kw // 2, ky + kh // 2), fill=fill)
        d.rectangle((kx - kw // 3, ky, kx + kw // 3, ky + kh // 2), fill=fill)

    if icon_kind == "sun":
        _draw_sun(cx, cy, int(r * 0.56))
    elif icon_kind == "partly":
        _draw_sun(cx - int(r * 0.25), cy - int(r * 0.22), int(r * 0.36))
        _draw_cloud(cx + int(r * 0.12), cy + int(r * 0.14), int(size * 0.72), int(size * 0.44), fill=ET["text_primary"])
    elif icon_kind == "cloud":
        _draw_cloud(cx, cy, int(size * 0.80), int(size * 0.50), fill=ET["text_muted"])
    elif icon_kind == "rain":
        _draw_cloud(cx, cy - int(r * 0.18), int(size * 0.76), int(size * 0.44), fill=ET["text_muted"])
        dy = cy + int(r * 0.26)
        for ox in (-int(r * 0.36), 0, int(r * 0.36)):
            d.line([(cx + ox, dy), (cx + ox - 3, dy + int(r * 0.42))], fill=ET["accent_bright"], width=max(2, size // 16))
    elif icon_kind == "storm":
        _draw_cloud(cx, cy - int(r * 0.20), int(size * 0.78), int(size * 0.44), fill=ET["text_muted"])
        pts = [
            (cx + 2, cy + 1),
            (cx - 5, cy + int(r * 0.42)),
            (cx + 1, cy + int(r * 0.42)),
            (cx - 3, cy + int(r * 0.80)),
        ]
        d.line(pts, fill=ET["warn"], width=max(2, size // 14))
    elif icon_kind == "snow":
        _draw_cloud(cx, cy - int(r * 0.20), int(size * 0.76), int(size * 0.44), fill=ET["text_muted"])
        dy = cy + int(r * 0.38)
        for ox in (-int(r * 0.35), 0, int(r * 0.35)):
            d.ellipse((cx + ox - 2, dy - 2, cx + ox + 2, dy + 2), fill=ET["text_bright"])
    elif icon_kind == "fog":
        for i, off in enumerate((-0.34, 0.0, 0.34)):
            ly = cy + int(r * off)
            lw = int(r * (0.75 if i != 1 else 0.90))
            d.line([(cx - lw, ly), (cx + lw, ly)], fill=ET["text_muted"], width=max(2, size // 14))


# ---------------------------------------------------------------------------
# Native Obsidian Ember Drawing
# ---------------------------------------------------------------------------
def draw_fetching(lcd, fonts):
    W, H = lcd.width, lcd.height
    ET = rj_theme.EMBER_THEME
    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)
    rj_theme.draw_app_header(d, W, "EMBERJACK WEATHER" if fonts["wide"] else "WEATHER", fonts, badge="SYNC")
    rj_theme.draw_status_card(
        d, W, H, "Fetching Weather...",
        sub=f"Location: {city}", kind="info",
        foot="Connecting to wttr.in...", fonts=fonts,
    )
    lcd.LCD_ShowImage(img, 0, 0)


def draw_main(lcd, fonts):
    """Render native Obsidian Ember weather hero card + forecast strip."""
    W, H = lcd.width, lcd.height
    ET = rj_theme.EMBER_THEME
    wide = bool(fonts["wide"])
    large = bool(fonts["large"])

    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)

    loc_title = (weather_data.get("location") if isinstance(weather_data, dict) and weather_data.get("ok") else city) or city
    updated = (weather_data.get("updated", "") if isinstance(weather_data, dict) else "")
    hdr_h = rj_theme.draw_app_header(
        d, W, "EMBERJACK WEATHER" if wide else "WEATHER", fonts,
        badge=f"°{unit}",
        right_text=f"UPD {updated}" if (large and updated) else "",
        right_pill=loc_title[:16],
    )
    ftr_h = rj_theme.draw_app_footer(
        d, W, H,
        "K1/R:Refresh  K2/C:City  Enter/U:°C/°F  Esc/K3:Exit" if large else (
            "K1:Ref  K2:City  OK:°C/°F  K3:Exit" if wide else "K1:R K2:City K3:X"
        ),
        fonts,
    )

    pad = 8 if large else (5 if wide else 3)
    y0 = hdr_h + pad
    y1 = H - ftr_h - pad

    if not isinstance(weather_data, dict) or not weather_data.get("ok"):
        err_msg = (weather_data or {}).get("error", "No weather data")
        rj_theme.draw_status_card(
            d, W, H, f"Weather Unavailable ({city})",
            sub=err_msg, kind="warn",
            foot="Press K1/R to retry or K2/C to change city", fonts=fonts,
        )
        lcd.LCD_ShowImage(img, 0, 0)
        return

    # Plain-text fallback card if wttr.in returned text instead of j1 JSON
    if weather_data.get("raw"):
        rj_theme.draw_card(
            d, (pad, y0, W - pad, y1),
            fill=ET["bg_surface"], outline=ET["border_accent"],
            radius=8 if wide else 4, accent_left=ET["accent"], accent_width=3 if wide else 2,
        )
        lines = rj_theme.wrap_text(d, weather_data["raw"], W - pad * 2 - 20, fonts["body"])
        lh = 18 if large else (13 if wide else 11)
        vis = max(1, (y1 - y0 - 14) // lh)
        st = max(0, min(scroll_offset, max(0, len(lines) - vis)))
        for i, ln in enumerate(lines[st:st + vis]):
            d.text((pad + 12, y0 + 10 + i * lh), ln, font=fonts["body"], fill=ET["text_primary"])
        rj_theme.draw_scrollbar(d, W - pad - 5, y0 + 6, y1 - 6, st, vis, len(lines), width=3)
        lcd.LCD_ShowImage(img, 0, 0)
        return

    # Structured rich weather data
    temp_val = weather_data["temp_c"] if unit == "C" else weather_data["temp_f"]
    feels_val = weather_data["feels_c"] if unit == "C" else weather_data["feels_f"]
    wind_val = weather_data["wind_kmph"] if unit == "C" else weather_data["wind_mph"]
    wind_full = f"{wind_val} {weather_data['wind_dir']}".strip()
    forecast = weather_data.get("forecast") or []

    if large:
        # ---- HDMI Widescreen (640x360): Hero Card (top) + 3-Day Forecast Strip (bottom) ----
        hero_h = int((y1 - y0) * 0.54)
        hero_y1 = y0 + hero_h
        rj_theme.draw_card(
            d, (pad, y0, W - pad, hero_y1),
            fill=ET["bg_surface"], outline=ET["border_accent"],
            radius=8, accent_left=ET["accent"], accent_width=4,
        )

        # Left side of Hero: Vector Icon + Huge Temp + Description
        icon_cx = pad + 56
        icon_cy = y0 + hero_h // 2
        _draw_weather_icon(d, icon_cx, icon_cy, 58, weather_data["icon"])

        temp_font = rj_env.load_font(42, bold=True)
        tx = pad + 106
        d.text((tx, y0 + 18), f"{temp_val}°{unit}", font=temp_font, fill=ET["accent_bright"])
        d.text(
            (tx, y0 + 74),
            rj_theme.truncate_text(d, weather_data["desc"], int(W * 0.36), fonts["title"]),
            font=fonts["title"], fill=ET["text_bright"],
        )
        loc_sub = f"{weather_data['location']}" + (f", {weather_data['country']}" if weather_data["country"] else "")
        d.text(
            (tx, y0 + 98),
            rj_theme.truncate_text(d, f"Feels like {feels_val}°{unit}  •  {loc_sub}", int(W * 0.38), fonts["sub"]),
            font=fonts["sub"], fill=ET["text_muted"],
        )

        # Right side of Hero: 2x2 Recessed Metric Tiles
        mx0 = int(W * 0.55)
        mx1 = W - pad - 12
        mw = (mx1 - mx0 - 8) // 2
        mh = (hero_h - 32) // 2
        metrics = [
            ("WIND", wind_full, ET["accent_bright"]),
            ("HUMIDITY", weather_data["humidity"], ET["ok"]),
            ("PRECIP", weather_data["precip"], ET["info"]),
            ("UV / PRESS", f"UV {weather_data['uv']} • {weather_data['pressure'].split()[0]}", ET["warn"]),
        ]
        for idx_m, (mk, mv, mcol) in enumerate(metrics):
            col_i = idx_m % 2
            row_i = idx_m // 2
            bx0 = mx0 + col_i * (mw + 8)
            by0 = y0 + 12 + row_i * (mh + 8)
            rj_theme.draw_card(d, (bx0, by0, bx0 + mw, by0 + mh), fill=ET["bg_well"], outline=ET["border_subtle"], radius=6)
            d.text((bx0 + 10, by0 + 8), mk, font=fonts["badge"], fill=ET["text_muted"])
            d.text((bx0 + 10, by0 + 25), rj_theme.truncate_text(d, mv, mw - 18, fonts["body_bold"]), font=fonts["body_bold"], fill=mcol)

        # Bottom: 3-Day Forecast Strip
        fy0 = hero_y1 + 8
        fy1 = y1
        if forecast:
            n_f = len(forecast)
            gap = 8
            fw = (W - pad * 2 - gap * (n_f - 1)) // n_f
            for i, fday in enumerate(forecast):
                fx0 = pad + i * (fw + gap)
                fx1 = (W - pad) if i == n_f - 1 else (fx0 + fw)
                rj_theme.draw_card(
                    d, (fx0, fy0, fx1, fy1),
                    fill=ET["bg_surface"], outline=ET["border_accent"] if i == 0 else ET["border_subtle"],
                    radius=7, accent_left=ET["accent"] if i == 0 else None, accent_width=3,
                )
                d.text((fx0 + 12, fy0 + 10), fday["label"].upper(), font=fonts["badge"], fill=ET["accent_bright"] if i == 0 else ET["text_muted"])
                rb = rj_theme.measure_text(fonts["badge"], f"RAIN {fday['rain']}", d)
                d.text((fx1 - 10 - (rb[2] - rb[0]), fy0 + 10), f"RAIN {fday['rain']}", font=fonts["badge"], fill=ET["info"])

                _draw_weather_icon(d, fx0 + 30, (fy0 + fy1) // 2 + 8, 28, fday["icon"])

                hi = fday["max_c"] if unit == "C" else fday["max_f"]
                lo = fday["min_c"] if unit == "C" else fday["min_f"]
                d.text((fx0 + 54, fy0 + 34), f"{hi}° / {lo}°{unit}", font=fonts["title"], fill=ET["text_bright"])
                d.text(
                    (fx0 + 54, fy0 + 58),
                    rj_theme.truncate_text(d, fday["desc"], fx1 - fx0 - 64, fonts["sub"]),
                    font=fonts["sub"], fill=ET["text_muted"],
                )

    elif wide:
        # ---- CardputerZero (320x170): Compact Hero Card + 3-col Forecast Strip ----
        hero_h = int((y1 - y0) * 0.56)
        hero_y1 = y0 + hero_h
        rj_theme.draw_card(
            d, (pad, y0, W - pad, hero_y1),
            fill=ET["bg_surface"], outline=ET["border_accent"],
            radius=5, accent_left=ET["accent"], accent_width=3,
        )
        _draw_weather_icon(d, pad + 28, y0 + hero_h // 2, 30, weather_data["icon"])
        t_font = rj_env.load_font(22, bold=True)
        d.text((pad + 54, y0 + 6), f"{temp_val}°{unit}", font=t_font, fill=ET["accent_bright"])
        d.text((pad + 54, y0 + 32), rj_theme.truncate_text(d, weather_data["desc"], 130, fonts["body_bold"]), font=fonts["body_bold"], fill=ET["text_bright"])
        d.text((pad + 54, y0 + 47), f"Feels {feels_val}°{unit}", font=fonts["sub"], fill=ET["text_muted"])

        rx = int(W * 0.60)
        d.text((rx, y0 + 8), f"W: {wind_full[:13]}", font=fonts["sub"], fill=ET["text_primary"])
        d.text((rx, y0 + 23), f"H: {weather_data['humidity']}  UV:{weather_data['uv']}", font=fonts["sub"], fill=ET["ok"])
        d.text((rx, y0 + 38), f"P: {weather_data['precip']}", font=fonts["sub"], fill=ET["info"])

        fy0 = hero_y1 + 4
        if forecast:
            n_f = len(forecast)
            gap = 4
            fw = (W - pad * 2 - gap * (n_f - 1)) // n_f
            for i, fday in enumerate(forecast):
                fx0 = pad + i * (fw + gap)
                fx1 = (W - pad) if i == n_f - 1 else (fx0 + fw)
                rj_theme.draw_card(d, (fx0, fy0, fx1, y1), fill=ET["bg_surface"], outline=ET["border_subtle"], radius=4)
                hi = fday["max_c"] if unit == "C" else fday["max_f"]
                lo = fday["min_c"] if unit == "C" else fday["min_f"]
                d.text((fx0 + 6, fy0 + 4), fday["label"][:8], font=fonts["badge"], fill=ET["accent_bright"] if i == 0 else ET["text_muted"])
                d.text((fx0 + 6, fy0 + 18), f"{hi}°/{lo}°", font=fonts["body_bold"], fill=ET["text_bright"])
                d.text((fx0 + 6, fy0 + 32), rj_theme.truncate_text(d, fday["desc"], fw - 10, fonts["sub"]), font=fonts["sub"], fill=ET["text_muted"])

    else:
        # ---- Square SPI HATs (128x128 / 240x240): Clean Scrollable Obsidian Ember Card ----
        cards = [
            f"{temp_val}°{unit}  {weather_data['desc']}",
            f"Feels: {feels_val}°{unit}",
            f"Wind: {wind_full}",
            f"Humidity: {weather_data['humidity']}  UV: {weather_data['uv']}",
            f"Precip: {weather_data['precip']}",
            f"Press: {weather_data['pressure']}",
        ]
        for fday in forecast:
            hi = fday["max_c"] if unit == "C" else fday["max_f"]
            lo = fday["min_c"] if unit == "C" else fday["min_f"]
            cards.append(f"{fday['label']}: {lo}..{hi}°{unit}")

        rj_theme.draw_card(
            d, (pad, y0, W - pad - 4, y1),
            fill=ET["bg_surface"], outline=ET["border_accent"],
            radius=4, accent_left=ET["accent"], accent_width=2,
        )
        lh = 18 if H >= 200 else 13
        vis = max(1, (y1 - y0 - 8) // lh)
        st = max(0, min(scroll_offset, max(0, len(cards) - vis)))
        for i, ln in enumerate(cards[st:st + vis]):
            col = ET["accent_bright"] if (st + i) == 0 else ET["text_primary"]
            d.text(
                (pad + 6, y0 + 5 + i * lh),
                rj_theme.truncate_text(d, ln, W - pad * 2 - 14, fonts["body_bold"] if (st + i) == 0 else fonts["sub"]),
                font=fonts["body_bold"] if (st + i) == 0 else fonts["sub"],
                fill=col,
            )
        rj_theme.draw_scrollbar(d, W - pad - 2, y0 + 2, y1 - 2, st, vis, len(cards), width=2)

    lcd.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global running, city, unit, weather_lines, scroll_offset

    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    fonts = rj_theme.load_app_fonts(lcd.width, lcd.height)

    load_config()
    draw_fetching(lcd, fonts)
    weather_lines = fetch_weather(city)
    scroll_offset = 0
    dirty = True

    try:
        with text_input_mode():
            flush_input()
            while running:
                if dirty:
                    draw_main(lcd, fonts)
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO)
                if act is None and ch is None:
                    time.sleep(0.04)
                    continue

                kl = ch.lower() if isinstance(ch, str) else ""
                if act in ("KEY3", "ESCAPE", "BACKSPACE") or kl == "q":
                    break
                elif act == "UP":
                    if scroll_offset > 0:
                        scroll_offset -= 1
                        dirty = True
                elif act == "DOWN":
                    scroll_offset += 1
                    dirty = True
                elif act in ("OK", "ENTER") or kl == "u":
                    unit = "F" if unit == "C" else "C"
                    save_config()
                    dirty = True
                elif act == "KEY1" or kl == "r":
                    draw_fetching(lcd, fonts)
                    weather_lines = fetch_weather(city)
                    scroll_offset = 0
                    dirty = True
                elif act == "KEY2" or kl == "c":
                    new_city = lcd_keyboard(
                        lcd, fonts["body"], PINS, GPIO,
                        title="SET CITY", default=city, charset="full",
                    )
                    if _rj_keyboard is not None:
                        _rj_keyboard.set_text_mode(True)
                    flush_input()
                    if new_city is not None and new_city.strip():
                        city = new_city.strip()
                        save_config()
                        draw_fetching(lcd, fonts)
                        weather_lines = fetch_weather(city)
                        scroll_offset = 0
                    dirty = True
    finally:
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
