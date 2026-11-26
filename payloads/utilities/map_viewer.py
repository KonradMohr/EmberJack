#!/usr/bin/env python3
"""
EmberJack Payload -- Offline Map Viewer
=========================================
Browse downloaded map tiles on the full display canvas with sleek translucent
Obsidian Ember HUD pills for coordinates, zoom level, and GPS fix status.

Controls:
  Arrows / WASD       Pan North / South / East / West
  KEY1 / + / PgUp     Zoom in
  KEY2 / - / PgDn     Zoom out
  OK / Enter / G      Center on live GPS position
  H / I / Space       Toggle / Wake HUD pills
  KEY3 / Esc / Q      Exit
"""

import math
import os
import signal
import sys
import time

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import LCD_Config
from PIL import Image, ImageDraw, ImageEnhance

import rj_env
import rj_theme
from payloads._input_helper import flush_input, poll_ui_input, text_input_mode

try:
    import gpsd as gpsd_mod
    GPSD_OK = True
except ImportError:
    gpsd_mod = None
    GPSD_OK = False

PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}
TILE_CACHE = os.path.join(rj_env.get_base_dir(), "loot", "wardriving", ".tilecache")
TILE_SIZE = 256

_running = True
_tile_lru = {}


def _sig(*_args):
    global _running
    _running = False


signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)


def _lat_lon_to_tile(lat, lon, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(max(-85, min(85, lat)))
    y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return x, y


def _tile_to_lat_lon(x, y, z):
    n = 2 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def _load_tile(z, x, y):
    key = (z, x, y)
    if key in _tile_lru:
        return _tile_lru[key]
    path = os.path.join(TILE_CACHE, f"{z}_{x}_{y}.png")
    tile = None
    if os.path.isfile(path):
        try:
            tile = Image.open(path).convert("RGB")
        except Exception:
            tile = None
    if len(_tile_lru) >= 64:
        _tile_lru.clear()
    _tile_lru[key] = tile
    return tile


def _get_cache_stats():
    """Get stats about cached tiles."""
    if not os.path.isdir(TILE_CACHE):
        return 0, 0, {}
    files = [f for f in os.listdir(TILE_CACHE) if f.endswith(".png")]
    total_size = sum(os.path.getsize(os.path.join(TILE_CACHE, f)) for f in files)
    zoom_counts = {}
    for f in files:
        parts = f.replace(".png", "").split("_")
        if parts:
            z = parts[0]
            zoom_counts[z] = zoom_counts.get(z, 0) + 1
    return len(files), total_size, zoom_counts


def _get_gps():
    if not GPSD_OK:
        return None, None
    try:
        gpsd_mod.connect()
        pkt = gpsd_mod.get_current()
        if hasattr(pkt, "mode") and pkt.mode >= 2:
            return pkt.lat, pkt.lon
    except Exception:
        pass
    return None, None


def _render_map(center_x, center_y, zoom, lcd_w, lcd_h):
    """Render map tiles into a full-canvas RGB image centered on tile coords."""
    base_bg = rj_theme.EMBER_RGB["bg_base"]

    tiles_x = lcd_w // TILE_SIZE + 3
    tiles_y = lcd_h // TILE_SIZE + 3

    cx_tile = int(math.floor(center_x))
    cy_tile = int(math.floor(center_y))
    px_off = int((center_x - cx_tile) * TILE_SIZE)
    py_off = int((center_y - cy_tile) * TILE_SIZE)

    start_tx = cx_tile - tiles_x // 2
    start_ty = cy_tile - tiles_y // 2

    big_w = tiles_x * TILE_SIZE
    big_h = tiles_y * TILE_SIZE
    big = Image.new("RGB", (big_w, big_h), base_bg)

    tiles_loaded = 0
    tiles_missing = 0
    for dx in range(tiles_x):
        for dy in range(tiles_y):
            tx = start_tx + dx
            ty = start_ty + dy
            tile = _load_tile(zoom, tx, ty)
            if tile is not None:
                big.paste(tile, (dx * TILE_SIZE, dy * TILE_SIZE))
                tiles_loaded += 1
            else:
                tiles_missing += 1

    crop_x = (tiles_x // 2) * TILE_SIZE + px_off - lcd_w // 2
    crop_y = (tiles_y // 2) * TILE_SIZE + py_off - lcd_h // 2
    crop_x = max(0, min(big_w - lcd_w, crop_x))
    crop_y = max(0, min(big_h - lcd_h, crop_y))

    cropped = big.crop((crop_x, crop_y, crop_x + lcd_w, crop_y + lcd_h))

    if tiles_loaded > 0:
        cropped = ImageEnhance.Brightness(cropped).enhance(0.78)
    else:
        # Subtle tactical Obsidian Ember grid on uncached ocean/empty viewport
        gd = ImageDraw.Draw(cropped)
        step = 32 if lcd_w >= 300 else 16
        gx_off = -(px_off % step)
        gy_off = -(py_off % step)
        grid_col = (24, 23, 26)
        for gx in range(gx_off, lcd_w, step):
            gd.line([(gx, 0), (gx, lcd_h - 1)], fill=grid_col, width=1)
        for gy in range(gy_off, lcd_h, step):
            gd.line([(0, gy), (lcd_w - 1, gy)], fill=grid_col, width=1)

    return cropped, tiles_loaded, tiles_missing


def _draw_translucent_pill(overlay_draw, x0, y0, x1, y1, radius, fill_rgba, outline_rgba):
    r = max(0, min(radius, (x1 - x0) // 2, (y1 - y0) // 2))
    overlay_draw.rounded_rectangle((x0, y0, x1, y1), radius=r, fill=fill_rgba, outline=outline_rgba, width=1)


def _compose_frame(cx, cy, zoom, rw, rh, fonts, gps_lat, gps_lon, show_hud, n_cached):
    wide = bool(fonts["wide"])
    large = bool(fonts["large"])
    ET = rj_theme.EMBER_THEME

    map_img, loaded, missing = _render_map(cx, cy, zoom, rw, rh)
    base = map_img.convert("RGBA")
    overlay = Image.new("RGBA", (rw, rh), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    # 1. Center Ember Crosshair
    mid_x, mid_y = rw // 2, rh // 2
    arm = 10 if large else (7 if wide else 5)
    gap = 3 if wide else 2
    for dx0, dy0, dx1, dy1 in (
        (mid_x - arm, mid_y, mid_x - gap, mid_y),
        (mid_x + gap, mid_y, mid_x + arm, mid_y),
        (mid_x, mid_y - arm, mid_x, mid_y - gap),
        (mid_x, mid_y + gap, mid_x, mid_y + arm),
    ):
        od.line([(dx0, dy0), (dx1, dy1)], fill=(14, 14, 16, 220), width=3 if large else 2)
        od.line([(dx0, dy0), (dx1, dy1)], fill=(242, 153, 74, 240), width=1)
    od.ellipse((mid_x - 1, mid_y - 1, mid_x + 1, mid_y + 1), fill=(245, 239, 232, 230))

    # 2. Live GPS Beacon Marker
    if gps_lat is not None and gps_lon is not None:
        gx, gy = _lat_lon_to_tile(gps_lat, gps_lon, zoom)
        px = mid_x + int((gx - cx) * TILE_SIZE)
        py = mid_y + int((gy - cy) * TILE_SIZE)
        if 0 <= px < rw and 0 <= py < rh:
            rr = 7 if large else 5
            od.ellipse((px - rr - 3, py - rr - 3, px + rr + 3, py + rr + 3), fill=(94, 184, 122, 65), outline=(94, 184, 122, 180))
            od.ellipse((px - rr, py - rr, px + rr, py + rr), fill=(94, 184, 122, 245), outline=(245, 239, 232, 255))

    # 3. Missing-tiles centered translucent status card
    if missing > 0 and loaded == 0:
        cw = min(rw - 24, 340 if large else (230 if wide else rw - 10))
        ch = 74 if large else (54 if wide else 46)
        cx0 = (rw - cw) // 2
        cy0 = (rh - ch) // 2 + (22 if large else 14)
        _draw_translucent_pill(od, cx0, cy0, cx0 + cw, cy0 + ch, 8 if wide else 4, (22, 22, 26, 215), (230, 168, 69, 220))
        od.rectangle((cx0 + 1, cy0 + 4, cx0 + (4 if wide else 2), cy0 + ch - 4), fill=(230, 168, 69, 245))
        od.text((cx0 + cw // 2, cy0 + (22 if large else 15)), "No Cached Tiles Here", font=fonts["body_bold"], fill=(245, 239, 232, 255), anchor="mm")
        sub_txt = f"{n_cached} tiles in cache • Use Map Downloader" if (wide and n_cached > 0) else "Run Map Downloader or pan/zoom"
        od.text((cx0 + cw // 2, cy0 + (48 if large else 34)), rj_theme.truncate_text(od, sub_txt, cw - 18, fonts["sub"]), font=fonts["sub"], fill=(155, 146, 138, 255), anchor="mm")

    # 4. Translucent Obsidian Ember Top & Bottom HUD Pills
    pad = 10 if large else (6 if wide else 3)
    pill_h = 26 if large else (20 if wide else 15)

    lat, lon = _tile_to_lat_lon(cx, cy, zoom)

    # Top-Left Pill: Zoom badge + Coordinates (always shown for spatial orientation)
    coord_txt = f"{lat:.4f}, {lon:.4f}" if wide else f"{lat:.2f},{lon:.2f}"
    z_txt = f"Z{zoom}"
    zb = rj_theme.measure_text(fonts["badge"], z_txt, od)
    cb = rj_theme.measure_text(fonts["mono"] if large else fonts["sub"], coord_txt, od)
    zw = (zb[2] - zb[0]) + (10 if wide else 6)
    cw_p = (cb[2] - cb[0]) + zw + (18 if wide else 10)
    _draw_translucent_pill(od, pad, pad, pad + cw_p, pad + pill_h, 6 if wide else 3, (14, 14, 16, 205), (184, 94, 36, 210))
    _draw_translucent_pill(od, pad + 3, pad + 3, pad + 3 + zw, pad + pill_h - 3, 4 if wide else 2, (45, 29, 18, 235), (224, 122, 47, 230))
    od.text((pad + 3 + zw // 2, pad + pill_h // 2), z_txt, font=fonts["badge"], fill=(242, 153, 74, 255), anchor="mm")
    od.text((pad + 3 + zw + (7 if wide else 4), pad + pill_h // 2), coord_txt, font=fonts["mono"] if large else fonts["sub"], fill=(245, 239, 232, 255), anchor="lm")

    # Top-Right Pill: GPS Status + Tile count
    gps_on = gps_lat is not None
    gps_txt = "GPS FIX" if (gps_on and wide) else ("GPS" if gps_on else ("NO GPS" if wide else "NOFIX"))
    tile_txt = f"• {loaded}T" if wide else f" {loaded}t"
    right_lbl = f"{gps_txt} {tile_txt}"
    rb = rj_theme.measure_text(fonts["badge"], right_lbl, od)
    rw_p = (rb[2] - rb[0]) + (16 if wide else 8)
    rx0 = rw - pad - rw_p
    if rx0 > pad + cw_p + 4:
        out_rgba = (94, 184, 122, 215) if gps_on else (43, 40, 38, 210)
        fg_rgba = (94, 184, 122, 255) if gps_on else (155, 146, 138, 255)
        _draw_translucent_pill(od, rx0, pad, rw - pad, pad + pill_h, 6 if wide else 3, (14, 14, 16, 205), out_rgba)
        od.text(((rx0 + rw - pad) // 2, pad + pill_h // 2), right_lbl, font=fonts["badge"], fill=fg_rgba, anchor="mm")

    # Bottom Control HUD Pill (auto-fades after inactivity; wake/toggle with H/I/Space)
    if show_hud:
        hint = (
            "Arrows/WASD:Pan  •  K1/+:Zoom+  •  K2/-:Zoom-  •  OK/G:GPS  •  H:HUD  •  Esc/K3:Exit"
            if large else (
                "Arrows:Pan  K1/K2:Zoom  OK:GPS  K3:Exit" if wide else "K1/2:Z OK:GPS K3:X"
            )
        )
        hb = rj_theme.measure_text(fonts["sub"], hint, od)
        hw_p = min(rw - pad * 2, (hb[2] - hb[0]) + (20 if wide else 10))
        bh = 22 if large else (17 if wide else 13)
        bx0 = (rw - hw_p) // 2
        by0 = rh - pad - bh
        _draw_translucent_pill(od, bx0, by0, bx0 + hw_p, by0 + bh, 6 if wide else 3, (14, 14, 16, 205), (43, 40, 38, 215))
        od.text((rw // 2, by0 + bh // 2), rj_theme.truncate_text(od, hint, hw_p - 10, fonts["sub"]), font=fonts["sub"], fill=(230, 223, 215, 240), anchor="mm")

    out = Image.alpha_composite(base, overlay).convert("RGB")
    return out


def main():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    LCD_Config.GPIO_Init()
    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    lcd.LCD_Clear()

    rw, rh = lcd.width, lcd.height
    fonts = rj_theme.load_app_fonts(rw, rh)

    zoom = 15
    cx, cy = _lat_lon_to_tile(48.8566, 2.3522, zoom)

    gps_lat, gps_lon = _get_gps()
    if gps_lat is not None:
        cx, cy = _lat_lon_to_tile(gps_lat, gps_lon, zoom)

    n_tiles, _cache_size, _zoom_counts = _get_cache_stats()
    hud_pinned = False
    hud_until = time.monotonic() + 4.0
    last_hud_vis = True
    dirty = True

    try:
        with text_input_mode():
            flush_input()
            while _running:
                now = time.monotonic()
                hud_vis = hud_pinned or (now < hud_until)
                if hud_vis != last_hud_vis:
                    last_hud_vis = hud_vis
                    dirty = True

                if dirty:
                    frame = _compose_frame(cx, cy, zoom, rw, rh, fonts, gps_lat, gps_lon, hud_vis, n_tiles)
                    lcd.LCD_ShowImage(frame, 0, 0)
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO)
                if act is None and ch is None:
                    time.sleep(0.03)
                    continue

                kl = ch.lower() if isinstance(ch, str) else ""
                if act in ("KEY3", "ESCAPE", "BACKSPACE") or kl == "q":
                    break

                pan_speed = 0.3 / (2 ** (zoom - 13))
                moved = False
                if act == "UP" or kl == "w":
                    cy -= pan_speed
                    moved = True
                elif act == "DOWN" or kl == "s":
                    cy += pan_speed
                    moved = True
                elif act == "LEFT" or kl == "a":
                    cx -= pan_speed
                    moved = True
                elif act == "RIGHT" or kl == "d":
                    cx += pan_speed
                    moved = True
                elif act in ("KEY1", "PAGEUP") or ch in ("+", "="):
                    if zoom < 17:
                        lat, lon = _tile_to_lat_lon(cx, cy, zoom)
                        zoom += 1
                        cx, cy = _lat_lon_to_tile(lat, lon, zoom)
                        moved = True
                elif act in ("KEY2", "PAGEDOWN") or ch in ("-", "_"):
                    if zoom > 10:
                        lat, lon = _tile_to_lat_lon(cx, cy, zoom)
                        zoom -= 1
                        cx, cy = _lat_lon_to_tile(lat, lon, zoom)
                        moved = True
                elif act in ("OK", "ENTER") or kl == "g":
                    gps_lat, gps_lon = _get_gps()
                    if gps_lat is not None:
                        cx, cy = _lat_lon_to_tile(gps_lat, gps_lon, zoom)
                    moved = True
                elif kl in ("h", "i") or ch == " ":
                    hud_pinned = not hud_pinned
                    hud_until = now + (4.0 if hud_pinned else 0.0)
                    dirty = True

                if moved:
                    hud_until = now + 4.0
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
