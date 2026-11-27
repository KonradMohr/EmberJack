#!/usr/bin/env python3
"""
RaspyJack Persistent HDMI Display Host & Focused Keyboard Presenter
-------------------------------------------------------------------
Reads frames published to /dev/shm/raspyjack_hdmi.shm by raspyjack.py OR any
of its 351 child payload subprocesses and renders them continuously to an HDMI
monitor via the best available backend:

  1. SDL2  (ctypes -> libSDL2-2.0.so.0): Wayland, X11, and headless KMSDRM
  2. GTK   (gi.repository.Gtk 4/3):      Wayland / X11 fallback
  3. fbdev (Linux /dev/fb0 ioctl+mmap):  Headless HDMI console / Raspberry Pi TTY

Hotkeys (Desktop Window):
  F11 / Alt+Enter : Toggle fullscreen
  F10             : Toggle bottom key-hint HUD bar
  F12             : Save PNG screenshot to loot/screenshots/
"""

import ctypes
import datetime
import fcntl
import math
import mmap
import os
import select
import signal
import socket
import struct
import sys
import time
from typing import Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw

import hdmi_shm
import rj_env
import rj_keyboard
import rj_theme

# ---------------------------------------------------------------------------
# Pure geometry & pixel-format helpers (unit-testable without display HW)
# ---------------------------------------------------------------------------

def compute_viewport(
    src_w: int,
    src_h: int,
    dst_w: int,
    dst_h: int,
    scale_mode: str = "fit",
) -> Tuple[int, int, int, int]:
    """Compute (x, y, w, h) destination rectangle preserving aspect ratio."""
    if src_w <= 0 or src_h <= 0 or dst_w <= 0 or dst_h <= 0:
        return 0, 0, max(1, dst_w), max(1, dst_h)
    mode = (scale_mode or "fit").lower()
    if mode == "stretch":
        return 0, 0, dst_w, dst_h
    if mode == "integer":
        factor = min(dst_w // src_w, dst_h // src_h)
        if factor >= 1:
            rw = src_w * factor
            rh = src_h * factor
            return (dst_w - rw) // 2, (dst_h - rh) // 2, rw, rh
    scale = min(dst_w / src_w, dst_h / src_h)
    rw = max(1, int(round(src_w * scale)))
    rh = max(1, int(round(src_h * scale)))
    rw = min(rw, dst_w)
    rh = min(rh, dst_h)
    return (dst_w - rw) // 2, (dst_h - rh) // 2, rw, rh


def _init_fb_buffer(dst_w: int, dst_h: int, bpp: int, line_length: int, red_off: int, green_off: int, blue_off: int) -> np.ndarray:
    """Allocate a clean black framebuffer byte matrix (dst_h x stride) with 32bpp alpha=255."""
    bytes_pp = max(2, bpp // 8)
    stride = max(line_length, dst_w * bytes_pp)
    buf = np.zeros((dst_h, stride), dtype=np.uint8)
    if bpp not in (16, 24):
        alpha_idx = 6 - (((red_off // 8) % 4) + ((green_off // 8) % 4) + ((blue_off // 8) % 4))
        if 0 <= alpha_idx < 4:
            buf[:, alpha_idx : dst_w * 4 : 4] = 255
    return buf


_SCALE_LUT_CACHE: dict = {}
_BGRX_SCRATCH: dict = {}


def _get_bgrx_scratch(band_h: int, src_w: int) -> np.ndarray:
    """Return a reusable `(band_h, src_w)` uint32 buffer to avoid per-frame BGRX OR allocs."""
    key = (int(band_h), int(src_w))
    arr = _BGRX_SCRATCH.get(key)
    if arr is None:
        if len(_BGRX_SCRATCH) >= 8:
            _BGRX_SCRATCH.clear()
        arr = np.empty(key, dtype="<u4")
        _BGRX_SCRATCH[key] = arr
    return arr


def _get_scale_luts(src_w: int, src_h: int, vw: int, vh: int) -> Tuple[np.ndarray, np.ndarray]:
    """Return cached 1D nearest-neighbor row/column index maps for (src_w,src_h)->(vw,vh)."""
    key = (int(src_w), int(src_h), int(vw), int(vh))
    cached = _SCALE_LUT_CACHE.get(key)
    if cached is not None:
        return cached
    y_lut = (np.arange(vh, dtype=np.int32) * src_h) // vh
    x_lut = (np.arange(vw, dtype=np.int32) * src_w) // vw
    if len(_SCALE_LUT_CACHE) >= 32:
        _SCALE_LUT_CACHE.clear()
    _SCALE_LUT_CACHE[key] = (y_lut, x_lut)
    return y_lut, x_lut


def _blit_rgb24_into_fb_buf_dirty(
    buf: np.ndarray,
    rgb_bytes: bytes,
    src_w: int,
    src_h: int,
    dst_w: int,
    dst_h: int,
    bpp: int,
    red_off: int = 16,
    green_off: int = 8,
    blue_off: int = 0,
    scale_mode: str = "fit",
    fast_scale: bool = False,
    dirty_rows: Optional[Tuple[int, int]] = None,
) -> Tuple[int, int, int, int, int, int]:
    """Scale RGB24 and blit directly into *buf*, optionally updating only *dirty_rows*.

    Returns (vx, vy, vw, vh, dy0, dy1) where framebuffer rows [vy+dy0 : vy+dy1) were
    updated. When *dirty_rows* is given as (sy0, sy1) in source space, only the minimal
    affected source band is color-converted, scaled, and copied into *buf* — cutting
    menu/terminal partial-update CPU & memory bandwidth by 15x-115x on /dev/fb0.
    """
    vx, vy, vw, vh = compute_viewport(src_w, src_h, dst_w, dst_h, scale_mode=scale_mode)
    same_size = (vw == src_w and vh == src_h)
    exact_int = (not same_size) and (vw % src_w == 0) and (vh % src_h == 0)

    if dirty_rows is not None:
        sy0 = max(0, min(src_h, int(dirty_rows[0])))
        sy1 = max(sy0, min(src_h, int(dirty_rows[1])))
        if sy1 <= sy0:
            return vx, vy, vw, vh, 0, 0
        is_partial = (sy0 > 0 or sy1 < src_h)
    else:
        sy0, sy1 = 0, src_h
        is_partial = False

    row_stride = src_w * 3

    if bpp == 16:
        if (same_size or exact_int or fast_scale) and (buf.strides[0] % 2 == 0):
            dst_u16 = buf[vy : vy + vh, vx * 2 : (vx + vw) * 2].view("<u2")
            if same_size:
                dy0, dy1 = sy0, sy1
                local_y = None
                x_lut = None
            elif exact_int:
                fy = vh // src_h
                fx = vw // src_w
                dy0, dy1 = sy0 * fy, sy1 * fy
                local_y = None
                x_lut = None
            else:
                y_lut, x_lut = _get_scale_luts(src_w, src_h, vw, vh)
                if is_partial:
                    dy0 = int(np.searchsorted(y_lut, sy0, side="left"))
                    dy1 = int(np.searchsorted(y_lut, sy1, side="left"))
                    if dy1 <= dy0:
                        return vx, vy, vw, vh, 0, 0
                else:
                    dy0, dy1 = 0, vh
                local_y = y_lut[dy0:dy1] - sy0

            band_h = sy1 - sy0
            band_bytes = rgb_bytes if not is_partial else rgb_bytes[sy0 * row_stride : sy1 * row_stride]
            arr = np.frombuffer(band_bytes, dtype=np.uint8).reshape((band_h, src_w, 3))
            src_u16 = (
                ((arr[..., 0].astype(np.uint16) >> 3) << 11)
                | ((arr[..., 1].astype(np.uint16) >> 2) << 5)
                | (arr[..., 2].astype(np.uint16) >> 3)
            ).astype("<u2")
            if same_size:
                np.copyto(dst_u16[dy0:dy1], src_u16)
            elif exact_int:
                dst_u16[dy0:dy1].reshape((band_h, fy, src_w, fx))[:] = src_u16[:, None, :, None]
            else:
                np.copyto(dst_u16[dy0:dy1], src_u16[local_y][:, x_lut])
            return vx, vy, vw, vh, dy0, dy1

        img = Image.frombytes("RGB", (src_w, src_h), rgb_bytes)
        if not same_size:
            if is_partial and (sy1 - sy0) < int(src_h * 0.75):
                dy0 = max(0, int(math.floor((sy0 - 1.5) * vh / src_h)))
                dy1 = min(vh, int(math.ceil((sy1 + 1.5) * vh / src_h)))
                if dy1 <= dy0:
                    return vx, vy, vw, vh, 0, 0
                box = (0.0, dy0 * src_h / vh, float(src_w), dy1 * src_h / vh)
                img = img.resize((vw, dy1 - dy0), Image.BILINEAR, box=box)
            else:
                dy0, dy1 = 0, vh
                img = img.resize((vw, vh), Image.BILINEAR)
        else:
            dy0, dy1 = 0, vh
        arr = np.asarray(img, dtype=np.uint8)
        r = (arr[..., 0].astype(np.uint16) >> 3) << 11
        g = (arr[..., 1].astype(np.uint16) >> 2) << 5
        b = (arr[..., 2].astype(np.uint16) >> 3)
        pix16 = (r | g | b).astype("<u2")
        buf[vy + dy0 : vy + dy1, vx * 2 : (vx + vw) * 2] = pix16.view(np.uint8).reshape((dy1 - dy0, vw * 2))
        return vx, vy, vw, vh, dy0, dy1

    if bpp == 24:
        img = Image.frombytes("RGB", (src_w, src_h), rgb_bytes)
        if not same_size:
            resample = Image.NEAREST if (exact_int or fast_scale) else Image.BILINEAR
            img = img.resize((vw, vh), resample)
        raw_mode = "RGB" if red_off == 0 else "BGR"
        vp = np.frombuffer(img.tobytes("raw", raw_mode), dtype=np.uint8).reshape((vh, vw * 3))
        buf[vy : vy + vh, vx * 3 : (vx + vw) * 3] = vp
        return vx, vy, vw, vh, 0, vh

    # 32bpp (standard Linux / Raspberry Pi /dev/fb0 XRGB8888 / BGRA)
    r_i = (red_off // 8) % 4
    g_i = (green_off // 8) % 4
    b_i = (blue_off // 8) % 4
    is_bgra = (r_i, g_i, b_i) == (2, 1, 0)
    is_rgba = (r_i, g_i, b_i) == (0, 1, 2)

    if (is_bgra or is_rgba) and sys.byteorder == "little" and (buf.strides[0] % 4 == 0):
        dst_u32 = buf[vy : vy + vh, vx * 4 : (vx + vw) * 4].view("<u4")
        if same_size or exact_int or fast_scale:
            if same_size:
                dy0, dy1 = sy0, sy1
                local_y = None
                x_lut = None
            elif exact_int:
                fy = vh // src_h
                fx = vw // src_w
                dy0, dy1 = sy0 * fy, sy1 * fy
                local_y = None
                x_lut = None
            else:
                y_lut, x_lut = _get_scale_luts(src_w, src_h, vw, vh)
                if is_partial:
                    dy0 = int(np.searchsorted(y_lut, sy0, side="left"))
                    dy1 = int(np.searchsorted(y_lut, sy1, side="left"))
                    if dy1 <= dy0:
                        return vx, vy, vw, vh, 0, 0
                else:
                    dy0, dy1 = 0, vh
                local_y = y_lut[dy0:dy1] - sy0

            # Convert ONLY the dirty SMALL source rows to 32-bit packed pixels first!
            band_h = sy1 - sy0
            band_bytes = rgb_bytes if not is_partial else rgb_bytes[sy0 * row_stride : sy1 * row_stride]
            s_img = Image.frombytes("RGB", (src_w, band_h), band_bytes)
            if is_bgra:
                raw_u32 = np.frombuffer(s_img.tobytes("raw", "BGRX"), dtype="<u4").reshape((band_h, src_w))
                src_u32 = _get_bgrx_scratch(band_h, src_w)
                np.bitwise_or(raw_u32, np.uint32(0xFF000000), out=src_u32)
            else:
                src_u32 = np.frombuffer(s_img.tobytes("raw", "RGBX"), dtype="<u4").reshape((band_h, src_w))

            if same_size:
                np.copyto(dst_u32[dy0:dy1], src_u32)
            elif exact_int:
                # Zero-allocation 4D stride broadcast (avoids 11MB/frame np.repeat temp allocs!)
                dst_u32[dy0:dy1].reshape((band_h, fy, src_w, fx))[:] = src_u32[:, None, :, None]
            else:
                # Non-integer display (e.g. 640x379->1824x1080 or 1366x768): precomputed LUT
                np.copyto(dst_u32[dy0:dy1], src_u32[local_y][:, x_lut])
            return vx, vy, vw, vh, dy0, dy1

        # Smooth non-integer UI frame: dirty-band bilinear scale + direct BGRX/RGBX
        if is_partial and (sy1 - sy0) < int(src_h * 0.75):
            dy0 = max(0, int(math.floor((sy0 - 1.5) * vh / src_h)))
            dy1 = min(vh, int(math.ceil((sy1 + 1.5) * vh / src_h)))
            if dy1 <= dy0:
                return vx, vy, vw, vh, 0, 0
            box = (0.0, dy0 * src_h / vh, float(src_w), dy1 * src_h / vh)
            img = Image.frombytes("RGB", (src_w, src_h), rgb_bytes).resize((vw, dy1 - dy0), Image.BILINEAR, box=box)
        else:
            dy0, dy1 = 0, vh
            img = Image.frombytes("RGB", (src_w, src_h), rgb_bytes).resize((vw, vh), Image.BILINEAR)
        band_dh = dy1 - dy0
        if is_bgra:
            vp_u32 = np.frombuffer(img.tobytes("raw", "BGRX"), dtype="<u4").reshape((band_dh, vw)) | np.uint32(0xFF000000)
        else:
            vp_u32 = np.frombuffer(img.tobytes("raw", "RGBX"), dtype="<u4").reshape((band_dh, vw))
        np.copyto(dst_u32[dy0:dy1], vp_u32)
        return vx, vy, vw, vh, dy0, dy1

    # Unusual 32bpp channel layout fallback
    img = Image.frombytes("RGB", (src_w, src_h), rgb_bytes)
    if not same_size:
        resample = Image.NEAREST if (exact_int or fast_scale) else Image.BILINEAR
        img = img.resize((vw, vh), resample)
    arr = np.asarray(img, dtype=np.uint8)
    vp_view = buf[vy : vy + vh, vx * 4 : (vx + vw) * 4].reshape((vh, vw, 4))
    vp_view[..., r_i] = arr[..., 0]
    vp_view[..., g_i] = arr[..., 1]
    vp_view[..., b_i] = arr[..., 2]
    return vx, vy, vw, vh, 0, vh


def _blit_rgb24_into_fb_buf(
    buf: np.ndarray,
    rgb_bytes: bytes,
    src_w: int,
    src_h: int,
    dst_w: int,
    dst_h: int,
    bpp: int,
    red_off: int = 16,
    green_off: int = 8,
    blue_off: int = 0,
    scale_mode: str = "fit",
    fast_scale: bool = False,
) -> Tuple[int, int, int, int]:
    """Scale RGB24 and blit directly into the active viewport slice of *buf*."""
    vx, vy, vw, vh, _dy0, _dy1 = _blit_rgb24_into_fb_buf_dirty(
        buf,
        rgb_bytes,
        src_w,
        src_h,
        dst_w,
        dst_h,
        bpp,
        red_off=red_off,
        green_off=green_off,
        blue_off=blue_off,
        scale_mode=scale_mode,
        fast_scale=fast_scale,
        dirty_rows=None,
    )
    return vx, vy, vw, vh


def convert_rgb24_to_fb(
    rgb_bytes: bytes,
    src_w: int,
    src_h: int,
    dst_w: int,
    dst_h: int,
    bpp: int,
    line_length: int,
    red_off: int = 16,
    green_off: int = 8,
    blue_off: int = 0,
    scale_mode: str = "fit",
) -> bytes:
    """Scale RGB24 frame into a full framebuffer image matching bpp, offsets, and stride."""
    buf = _init_fb_buffer(dst_w, dst_h, bpp, line_length, red_off, green_off, blue_off)
    _blit_rgb24_into_fb_buf(
        buf, rgb_bytes, src_w, src_h, dst_w, dst_h, bpp,
        red_off=red_off, green_off=green_off, blue_off=blue_off, scale_mode=scale_mode,
    )
    return buf.tobytes()


_HUD_STRIP_CACHE: dict = {}


def _get_hud_strip(w: int, h: int, text_mode: bool) -> Tuple[bytes, int]:
    """Return cached raw RGB24 bytes & height for the Obsidian Ember HUD strip."""
    key = (int(w), int(h), bool(text_mode))
    cached = _HUD_STRIP_CACHE.get(key)
    if cached is not None:
        return cached

    hud_h = max(16, int(h * 0.055))
    strip = Image.new("RGB", (w, hud_h), rj_theme.EMBER_RGB["bg_overlay"])
    d = ImageDraw.Draw(strip)
    d.line([(0, 0), (w, 0)], fill=rj_theme.EMBER_RGB["border_subtle"], width=1)
    if text_mode:
        badge = "TEXT"
        badge_bg = rj_theme.EMBER_THEME["accent"]
        badge_fg = rj_theme.EMBER_THEME["text_on_accent"]
        hints = [
            "Type normally  Enter:OK  Bksp:Del  PgUp/Dn:Scroll  F3/Ctrl+Q:Menu  F11:Full  F10:HUD",
            "Type normally  Enter:OK  Bksp:Del  F3:Menu  F11:Full  F10:HUD",
            "Enter:OK  Esc/F3:Back  F10:HUD",
        ]
        col = rj_theme.EMBER_RGB["text_primary"]
    else:
        badge = "NAV"
        badge_bg = rj_theme.EMBER_THEME["select_bg"]
        badge_fg = rj_theme.EMBER_THEME["accent_bright"]
        hints = [
            "Arrows:Nav  Enter:OK  Space/F1:View  F2:Term  Esc/F3:Back  F11:Full  F12:Shot  F10:HUD",
            "Arrows:Nav Enter:OK F1:K1 F2:K2 Esc/F3:K3 F11:Full F12:Shot F10:HUD",
            "Arrows:Nav  Enter:OK  F1/F2/F3  Esc:Back  F10:HUD",
            "Arrows Enter F1-F3 Esc",
        ]
        col = rj_theme.EMBER_RGB["text_muted"]

    base_px = max(9, hud_h - 6)
    badge_font = rj_env.load_font(size=max(8, base_px - 1), bold=True, mono=True)
    bpw, _bph = rj_theme.draw_pill(
        d,
        5,
        max(2, (hud_h - (base_px + 2)) // 2),
        badge,
        badge_font,
        bg=badge_bg,
        fg=badge_fg,
        outline=rj_theme.EMBER_THEME["border_accent"],
        pad_x=4,
        pad_y=1,
        radius=3,
    )

    text_x = 5 + bpw + 7
    max_w = max(40, w - text_x - 6)
    chosen_hint = hints[-1]
    chosen_font = rj_env.load_font(size=base_px, mono=True)
    found_fit = False
    for px in range(base_px, 7, -1):
        f = rj_env.load_font(size=px, mono=True)
        for cand in hints:
            bbox = d.textbbox((0, 0), cand, font=f)
            if (bbox[2] - bbox[0]) <= max_w:
                chosen_hint = cand
                chosen_font = f
                found_fit = True
                break
        if found_fit:
            break

    bbox = d.textbbox((0, 0), chosen_hint, font=chosen_font)
    text_h = bbox[3] - bbox[1]
    ty = max(1, (hud_h - text_h) // 2 - bbox[1])
    d.text((text_x, ty), chosen_hint, font=chosen_font, fill=col)

    res = (strip.tobytes(), hud_h)
    if len(_HUD_STRIP_CACHE) > 16:
        _HUD_STRIP_CACHE.clear()
    _HUD_STRIP_CACHE[key] = res
    return res


def compose_hud_overlay(rgb_bytes: bytes, w: int, h: int, text_mode: bool = False) -> Tuple[bytes, int, int]:
    """Append a slim cached Obsidian Ember key-hint bar below the logical frame."""
    strip_bytes, hud_h = _get_hud_strip(w, h, text_mode=text_mode)
    return rgb_bytes + strip_bytes, w, h + hud_h


def save_screenshot(rgb_bytes: bytes, w: int, h: int) -> Optional[str]:
    """Save the current HDMI frame as a PNG under loot/screenshots/."""
    try:
        out_dir = os.path.join(rj_env.get_base_dir(), "loot", "screenshots")
        os.makedirs(out_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(out_dir, f"raspyjack_{ts}.png")
        Image.frombytes("RGB", (w, h), rgb_bytes).save(path, "PNG")
        print(f"[hdmi_host] Screenshot saved: {path}", flush=True)
        return path
    except Exception as exc:
        print(f"[hdmi_host] Screenshot failed: {exc}", flush=True)
        return None


# ---------------------------------------------------------------------------
# SDL2 scancode -> Linux evdev keycode table
# ---------------------------------------------------------------------------

_SDL_SCANCODE_TO_EVDEV = {
    # Letters A..Z (SDL 4..29)
    4: 30, 5: 48, 6: 46, 7: 32, 8: 18, 9: 33, 10: 34, 11: 35, 12: 23,
    13: 36, 14: 37, 15: 38, 16: 50, 17: 49, 18: 24, 19: 25, 20: 16,
    21: 19, 22: 31, 23: 20, 24: 22, 25: 47, 26: 17, 27: 45, 28: 21, 29: 44,
    # Digits 1..9, 0 (SDL 30..39)
    30: 2, 31: 3, 32: 4, 33: 5, 34: 6, 35: 7, 36: 8, 37: 9, 38: 10, 39: 11,
    # Control / punctuation
    40: 28,   # Return
    41: 1,    # Escape
    42: 14,   # Backspace
    43: 15,   # Tab
    44: 57,   # Space
    45: 12,   # Minus
    46: 13,   # Equals
    47: 26,   # LeftBracket
    48: 27,   # RightBracket
    49: 43,   # Backslash
    50: 43,   # NonUSHash
    51: 39,   # Semicolon
    52: 40,   # Apostrophe
    53: 41,   # Grave
    54: 51,   # Comma
    55: 52,   # Period
    56: 53,   # Slash
    57: 58,   # CapsLock
    # Function keys F1..F12
    58: 59, 59: 60, 60: 61, 61: 62, 62: 63, 63: 64,
    64: 65, 65: 66, 66: 67, 67: 68, 68: 87, 69: 88,
    # Navigation
    73: 110,  # Insert
    74: 102,  # Home
    75: 104,  # PageUp
    76: 111,  # Delete
    77: 107,  # End
    78: 109,  # PageDown
    79: 106,  # Right
    80: 105,  # Left
    81: 108,  # Down
    82: 103,  # Up
    # Numpad
    84: 98,   # KP /
    85: 55,   # KP *
    86: 74,   # KP -
    87: 78,   # KP +
    88: 96,   # KP Enter
    89: 79, 90: 80, 91: 81,  # KP 1,2,3
    92: 75, 93: 76, 94: 77,  # KP 4,5,6
    95: 71, 96: 72, 97: 73,  # KP 7,8,9
    98: 82,   # KP 0
    99: 83,   # KP .
    100: 86,  # ISO backslash / <>
    # Modifiers
    224: 29,  # LCtrl
    225: 42,  # LShift
    226: 56,  # LAlt
    228: 97,  # RCtrl
    229: 54,  # RShift
    230: 100, # RAlt
}


def _sdl_mod_to_rj_mod(sdl_mod: int) -> int:
    m = 0
    if sdl_mod & 0x0001:
        m |= rj_keyboard.MOD_LSHIFT
    if sdl_mod & 0x0002:
        m |= rj_keyboard.MOD_RSHIFT
    if sdl_mod & 0x2000:
        m |= rj_keyboard.MOD_CAPSLOCK
    if sdl_mod & 0x0040:
        m |= rj_keyboard.MOD_LCTRL
    if sdl_mod & 0x0080:
        m |= rj_keyboard.MOD_RCTRL
    if sdl_mod & 0x0100:
        m |= rj_keyboard.MOD_LALT
    if sdl_mod & 0x0200:
        m |= rj_keyboard.MOD_RALT
    return m


# ---------------------------------------------------------------------------
# Backend 1: SDL2 via ctypes (Wayland / X11 / KMSDRM)
# ---------------------------------------------------------------------------

class _SDLRect(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("w", ctypes.c_int),
        ("h", ctypes.c_int),
    ]


class _SDL2Backend:
    SDL_INIT_VIDEO = 0x00000020
    SDL_WINDOWPOS_CENTERED = 0x2FFF0000
    SDL_WINDOW_SHOWN = 0x00000004
    SDL_WINDOW_RESIZABLE = 0x00000020
    SDL_WINDOW_FULLSCREEN_DESKTOP = 0x00001001
    SDL_RENDERER_ACCELERATED = 0x00000002
    SDL_RENDERER_PRESENTVSYNC = 0x00000004
    SDL_RENDERER_SOFTWARE = 0x00000001
    SDL_PIXELFORMAT_RGB24 = 0x17101803
    SDL_TEXTUREACCESS_STREAMING = 1
    SDL_QUIT = 0x100
    SDL_WINDOWEVENT = 0x200
    SDL_KEYDOWN = 0x300
    SDL_KEYUP = 0x301
    SDL_TEXTINPUT = 0x303
    SDL_WINDOWEVENT_FOCUS_GAINED = 12
    SDL_WINDOWEVENT_FOCUS_LOST = 13

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.kb_cfg = rj_env.get_keyboard_config()
        self.fullscreen = bool(cfg.get("fullscreen", False))
        self.scale_mode = str(cfg.get("scale_mode", "fit"))
        self.show_hud = bool(cfg.get("show_hud", True))
        self.sdl = None
        self.window = None
        self.renderer = None
        self.texture = None
        self.tex_w = 0
        self.tex_h = 0
        self.last_raw: Optional[Tuple[bytes, int, int]] = None
        self.last_text_mode = False
        self._last_disp_sig = None
        self._last_fallback_char: Tuple[str, float] = ("", 0.0)
        self.running = True
        self._load_sdl()

    def _load_sdl(self):
        for lib_name in ("libSDL2-2.0.so.0", "libSDL2-2.0.so", "libSDL2.so"):
            try:
                self.sdl = ctypes.CDLL(lib_name)
                break
            except OSError:
                continue
        if self.sdl is None:
            raise RuntimeError("libSDL2 shared library not found")

        s = self.sdl
        s.SDL_Init.argtypes = [ctypes.c_uint32]
        s.SDL_Init.restype = ctypes.c_int
        s.SDL_Quit.argtypes = []
        s.SDL_GetError.argtypes = []
        s.SDL_GetError.restype = ctypes.c_char_p
        s.SDL_SetHint.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        s.SDL_SetHint.restype = ctypes.c_int
        s.SDL_CreateWindow.argtypes = [
            ctypes.c_char_p, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint32,
        ]
        s.SDL_CreateWindow.restype = ctypes.c_void_p
        s.SDL_DestroyWindow.argtypes = [ctypes.c_void_p]
        s.SDL_SetWindowFullscreen.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        s.SDL_SetWindowFullscreen.restype = ctypes.c_int
        s.SDL_CreateRenderer.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
        s.SDL_CreateRenderer.restype = ctypes.c_void_p
        s.SDL_DestroyRenderer.argtypes = [ctypes.c_void_p]
        s.SDL_GetRendererOutputSize.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
        ]
        s.SDL_GetRendererOutputSize.restype = ctypes.c_int
        s.SDL_CreateTexture.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]
        s.SDL_CreateTexture.restype = ctypes.c_void_p
        s.SDL_DestroyTexture.argtypes = [ctypes.c_void_p]
        s.SDL_UpdateTexture.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        s.SDL_UpdateTexture.restype = ctypes.c_int
        s.SDL_SetRenderDrawColor.argtypes = [
            ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint8,
        ]
        s.SDL_RenderClear.argtypes = [ctypes.c_void_p]
        s.SDL_RenderCopy.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_SDLRect),
        ]
        s.SDL_RenderPresent.argtypes = [ctypes.c_void_p]
        s.SDL_PollEvent.argtypes = [ctypes.c_void_p]
        s.SDL_PollEvent.restype = ctypes.c_int
        s.SDL_StartTextInput.argtypes = []
        s.SDL_StopTextInput.argtypes = []
        s.SDL_GetModState.argtypes = []
        s.SDL_GetModState.restype = ctypes.c_int

        # Prefer crisp scaling when integer/fit
        s.SDL_SetHint(b"SDL_RENDER_SCALE_QUALITY", b"1" if self.scale_mode == "fit" else b"0")
        s.SDL_SetHint(b"SDL_VIDEO_WAYLAND_ALLOW_LIBDECOR", b"1")

        if s.SDL_Init(self.SDL_INIT_VIDEO) != 0:
            err = s.SDL_GetError().decode("utf-8", "ignore")
            raise RuntimeError(f"SDL_Init failed: {err}")

        win_w = int(self.cfg.get("window_width", 1280))
        win_h = int(self.cfg.get("window_height", 720))
        flags = self.SDL_WINDOW_SHOWN | self.SDL_WINDOW_RESIZABLE
        if self.fullscreen:
            flags |= self.SDL_WINDOW_FULLSCREEN_DESKTOP

        self.window = s.SDL_CreateWindow(
            b"EmberJack HDMI",
            self.SDL_WINDOWPOS_CENTERED,
            self.SDL_WINDOWPOS_CENTERED,
            win_w,
            win_h,
            flags,
        )
        if not self.window:
            err = s.SDL_GetError().decode("utf-8", "ignore")
            s.SDL_Quit()
            raise RuntimeError(f"SDL_CreateWindow failed: {err}")

        self.renderer = s.SDL_CreateRenderer(
            self.window, -1, self.SDL_RENDERER_ACCELERATED | self.SDL_RENDERER_PRESENTVSYNC
        )
        if not self.renderer:
            self.renderer = s.SDL_CreateRenderer(self.window, -1, self.SDL_RENDERER_SOFTWARE)
        if not self.renderer:
            err = s.SDL_GetError().decode("utf-8", "ignore")
            s.SDL_DestroyWindow(self.window)
            s.SDL_Quit()
            raise RuntimeError(f"SDL_CreateRenderer failed: {err}")

        s.SDL_StartTextInput()

    def _toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        flag = self.SDL_WINDOW_FULLSCREEN_DESKTOP if self.fullscreen else 0
        self.sdl.SDL_SetWindowFullscreen(self.window, flag)

    def _ensure_texture(self, w: int, h: int):
        if self.texture and self.tex_w == w and self.tex_h == h:
            return
        if self.texture:
            self.sdl.SDL_DestroyTexture(self.texture)
        self.texture = self.sdl.SDL_CreateTexture(
            self.renderer,
            self.SDL_PIXELFORMAT_RGB24,
            self.SDL_TEXTUREACCESS_STREAMING,
            w,
            h,
        )
        self.tex_w = w
        self.tex_h = h

    def present(self, rgb_bytes: bytes, w: int, h: int, flags: int = 0):
        self.last_raw = (rgb_bytes, w, h, int(flags))
        text_mode = rj_keyboard.is_text_mode()
        self.last_text_mode = text_mode
        want_hud = self.show_hud and not (int(flags) & hdmi_shm.FLAG_NO_HUD)
        if want_hud:
            disp_bytes, dw, dh = compose_hud_overlay(rgb_bytes, w, h, text_mode=text_mode)
        else:
            disp_bytes, dw, dh = rgb_bytes, w, h

        sig = (dw, dh, disp_bytes)
        if self._last_disp_sig == sig and self.texture:
            return
        self._last_disp_sig = sig

        self._ensure_texture(dw, dh)
        if not self.texture:
            return
        self.sdl.SDL_UpdateTexture(self.texture, None, disp_bytes, dw * 3)
        self._redraw()

    def _redraw(self):
        if not self.texture or not self.renderer:
            return
        out_w = ctypes.c_int(0)
        out_h = ctypes.c_int(0)
        self.sdl.SDL_GetRendererOutputSize(self.renderer, ctypes.byref(out_w), ctypes.byref(out_h))
        vx, vy, vw, vh = compute_viewport(
            self.tex_w, self.tex_h, max(1, out_w.value), max(1, out_h.value), self.scale_mode
        )
        rect = _SDLRect(vx, vy, vw, vh)
        self.sdl.SDL_SetRenderDrawColor(self.renderer, 14, 14, 16, 255)
        self.sdl.SDL_RenderClear(self.renderer)
        self.sdl.SDL_RenderCopy(self.renderer, self.texture, None, ctypes.byref(rect))
        self.sdl.SDL_RenderPresent(self.renderer)

    def _process_sdl_event_batch(self, raw_events: list) -> None:
        """Process a polled batch of raw 64-byte SDL_Event buffers.

        If SDL2 delivers SDL_TEXTINPUT alongside printable SDL_KEYDOWN events
        (standard X11/Wayland with text-input protocol), we prefer SDL_TEXTINPUT
        to preserve OS keyboard layout / dead keys.  If SDL_TEXTINPUT does NOT
        arrive in the same event batch (common on KMSDRM, labwc/Wayfire Wayland,
        and headless SDL2), we automatically fall back to translating the evdev
        scancode + modifiers so typing NEVER silently drops characters.
        """
        has_textinput_in_batch = any(
            struct.unpack_from("<I", raw, 0)[0] == self.SDL_TEXTINPUT
            for raw in raw_events
        )

        for ev_buf in raw_events:
            ev_type = struct.unpack_from("<I", ev_buf, 0)[0]

            if ev_type == self.SDL_QUIT:
                rj_keyboard.clear_all_keys()
                self.running = False
                return

            if ev_type == self.SDL_WINDOWEVENT:
                win_ev = ev_buf[12]
                if win_ev == self.SDL_WINDOWEVENT_FOCUS_LOST:
                    rj_keyboard.clear_all_keys()
                else:
                    if win_ev == self.SDL_WINDOWEVENT_FOCUS_GAINED and self.sdl is not None:
                        try:
                            self.sdl.SDL_StartTextInput()
                        except Exception:
                            pass
                    self._redraw()
                continue

            if ev_type in (self.SDL_KEYDOWN, self.SDL_KEYUP):
                pressed = ev_type == self.SDL_KEYDOWN
                repeat = bool(ev_buf[13])
                scancode, _sym, sdl_mod = struct.unpack_from("<iiH", ev_buf, 16)
                rj_mod = _sdl_mod_to_rj_mod(sdl_mod)

                # Host window hotkeys
                if pressed and not repeat:
                    if scancode == 68 or (scancode in (40, 88) and (rj_mod & rj_keyboard.MOD_ALT_ANY)):
                        self._toggle_fullscreen()
                        continue
                    if scancode == 67:  # F10 toggle HUD
                        self.show_hud = not self.show_hud
                        if self.last_raw:
                            self.present(*self.last_raw)
                        continue
                    if scancode == 69 and self.last_raw:  # F12 screenshot
                        save_screenshot(self.last_raw[0], self.last_raw[1], self.last_raw[2])
                        continue

                evdev_code = _SDL_SCANCODE_TO_EVDEV.get(scancode)
                if evdev_code is None:
                    continue

                is_printable = (
                    evdev_code in rj_keyboard.STANDARD_CHAR_MAP
                    and not (rj_mod & (rj_keyboard.MOD_CTRL_ANY | rj_keyboard.MOD_ALT_ANY))
                )
                # Suppress scancode char ONLY when a paired SDL_TEXTINPUT arrived in this tick
                use_scancode_text = (not is_printable) or (not has_textinput_in_batch)
                if pressed and is_printable and use_scancode_text:
                    fb_ch, _ = rj_keyboard.translate_evdev_code(evdev_code, mod_mask=rj_mod, profile="standard")
                    if fb_ch:
                        self._last_fallback_char = (fb_ch, time.monotonic())
                elif pressed:
                    self._last_fallback_char = ("", 0.0)

                rj_keyboard.handle_key_event(
                    evdev_code,
                    pressed=pressed,
                    is_repeat=repeat,
                    profile="standard",
                    kb_cfg=self.kb_cfg,
                    mod_mask_override=rj_mod,
                    emit_text_from_code=use_scancode_text,
                )
                continue

            if ev_type == self.SDL_TEXTINPUT:
                raw_text = bytes(ev_buf[12:44]).split(b"\x00", 1)[0]
                text = raw_text.decode("utf-8", "ignore")
                if text:
                    # Deduplicate rare delayed async-IME SDL_TEXTINPUT right after fallback
                    now = time.monotonic()
                    fb_ch, fb_ts = self._last_fallback_char
                    if len(text) == 1 and text == fb_ch and (now - fb_ts) < 0.05:
                        self._last_fallback_char = ("", 0.0)
                        continue
                    self._last_fallback_char = ("", 0.0)
                    sdl_mod = self.sdl.SDL_GetModState() if self.sdl is not None else 0
                    rj_mod = _sdl_mod_to_rj_mod(sdl_mod)
                    for ch in text:
                        rj_keyboard.send_text_event(char=ch, mod_mask=rj_mod)

    def poll_events(self):
        ev_buf = (ctypes.c_uint8 * 64)()
        batch = []
        while self.sdl.SDL_PollEvent(ctypes.byref(ev_buf)) != 0:
            batch.append(bytes(ev_buf))
        if batch:
            self._process_sdl_event_batch(batch)

        # Refresh HUD bar if text_mode flipped
        if self.show_hud and self.last_raw and not (self.last_raw[3] & hdmi_shm.FLAG_NO_HUD):
            cur_tm = rj_keyboard.is_text_mode()
            if cur_tm != self.last_text_mode:
                self.present(*self.last_raw)

    def close(self):
        rj_keyboard.clear_all_keys()
        if self.sdl:
            try:
                self.sdl.SDL_StopTextInput()
            except Exception:
                pass
            if self.texture:
                self.sdl.SDL_DestroyTexture(self.texture)
            if self.renderer:
                self.sdl.SDL_DestroyRenderer(self.renderer)
            if self.window:
                self.sdl.SDL_DestroyWindow(self.window)
            self.sdl.SDL_Quit()


# ---------------------------------------------------------------------------
# Backend 2: GTK4 / GTK3 via PyGObject (Wayland / X11 fallback)
# ---------------------------------------------------------------------------

class _GTKBackend:
    def __init__(self, cfg: dict):
        import gi

        try:
            gi.require_version("Gtk", "4.0")
            self.gtk_major = 4
        except ValueError:
            gi.require_version("Gtk", "3.0")
            self.gtk_major = 3

        from gi.repository import Gdk, GLib, Gtk

        self.Gtk = Gtk
        self.Gdk = Gdk
        self.GLib = GLib
        self.cfg = cfg
        self.kb_cfg = rj_env.get_keyboard_config()
        self.fullscreen = bool(cfg.get("fullscreen", False))
        self.show_hud = bool(cfg.get("show_hud", True))
        self.last_raw: Optional[Tuple[bytes, int, int]] = None
        self.last_text_mode = False
        self._last_disp_sig = None
        self.running = True

        if self.gtk_major == 4:
            self._init_gtk4()
        else:
            self._init_gtk3()

    def _init_gtk4(self):
        Gtk, Gdk = self.Gtk, self.Gdk
        Gtk.init()
        self.win = Gtk.Window(title="EmberJack HDMI")
        self.win.set_default_size(
            int(self.cfg.get("window_width", 1280)),
            int(self.cfg.get("window_height", 720)),
        )
        css = Gtk.CssProvider()
        css.load_from_data(b"window { background-color: #0e0e10; }")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.picture = Gtk.Picture()
        self.picture.set_can_shrink(True)
        self.picture.set_hexpand(True)
        self.picture.set_vexpand(True)
        self.win.set_child(self.picture)

        key_ctrl = Gtk.EventControllerKey()
        if hasattr(Gtk, "PropagationPhase"):
            key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect("key-pressed", self._on_key_pressed_gtk4)
        key_ctrl.connect("key-released", self._on_key_released_gtk4)
        self.win.add_controller(key_ctrl)

        focus_ctrl = Gtk.EventControllerFocus()
        focus_ctrl.connect("leave", lambda *_: rj_keyboard.clear_all_keys())
        self.win.add_controller(focus_ctrl)

        self.win.connect("close-request", self._on_close)
        if self.fullscreen:
            self.win.fullscreen()
        self.win.present()

    def _init_gtk3(self):
        Gtk = self.Gtk
        self.win = Gtk.Window(title="EmberJack HDMI")
        self.win.set_default_size(
            int(self.cfg.get("window_width", 1280)),
            int(self.cfg.get("window_height", 720)),
        )
        self.image_widget = Gtk.Image()
        self.win.add(self.image_widget)
        self.win.connect("destroy", self._on_close)
        self.win.connect("key-press-event", self._on_key_gtk3, True)
        self.win.connect("key-release-event", self._on_key_gtk3, False)
        self.win.connect("focus-out-event", lambda *_: rj_keyboard.clear_all_keys())
        if self.fullscreen:
            self.win.fullscreen()
        self.win.show_all()

    def _on_close(self, *_args):
        rj_keyboard.clear_all_keys()
        self.running = False
        return False

    def _gtk_state_to_mod(self, state: int) -> int:
        Gdk = self.Gdk
        m = 0
        if state & int(Gdk.ModifierType.SHIFT_MASK):
            m |= rj_keyboard.MOD_LSHIFT
        if state & int(Gdk.ModifierType.LOCK_MASK):
            m |= rj_keyboard.MOD_CAPSLOCK
        if state & int(Gdk.ModifierType.CONTROL_MASK):
            m |= rj_keyboard.MOD_LCTRL
        if state & int(Gdk.ModifierType.ALT_MASK if hasattr(Gdk.ModifierType, "ALT_MASK") else Gdk.ModifierType.MOD1_MASK):
            m |= rj_keyboard.MOD_LALT
        return m

    def _handle_gtk_key(self, keyval: int, keycode: int, state: int, pressed: bool):
        evdev_code = max(0, int(keycode) - 8)
        rj_mod = self._gtk_state_to_mod(state)

        if pressed:
            if evdev_code == 87 or (evdev_code in (28, 96) and (rj_mod & rj_keyboard.MOD_ALT_ANY)):
                self.fullscreen = not self.fullscreen
                if self.fullscreen:
                    self.win.fullscreen()
                else:
                    self.win.unfullscreen()
                return True
            if evdev_code == 68:  # F10
                self.show_hud = not self.show_hud
                if self.last_raw:
                    self.present(*self.last_raw)
                return True
            if evdev_code == 88 and self.last_raw:  # F12
                save_screenshot(self.last_raw[0], self.last_raw[1], self.last_raw[2])
                return True

        text_override = None
        if pressed and not (rj_mod & (rj_keyboard.MOD_CTRL_ANY | rj_keyboard.MOD_ALT_ANY)):
            uni = self.Gdk.keyval_to_unicode(keyval)
            if uni >= 32 and evdev_code not in rj_keyboard.SPECIAL_KEY_CODES:
                text_override = chr(uni)

        rj_keyboard.handle_key_event(
            evdev_code,
            pressed=pressed,
            is_repeat=False,
            text_override=text_override,
            profile="standard",
            kb_cfg=self.kb_cfg,
            mod_mask_override=rj_mod,
            emit_text_from_code=(text_override is None),
        )
        return True

    def _on_key_pressed_gtk4(self, _ctrl, keyval, keycode, state):
        return self._handle_gtk_key(keyval, keycode, int(state), True)

    def _on_key_released_gtk4(self, _ctrl, keyval, keycode, state):
        return self._handle_gtk_key(keyval, keycode, int(state), False)

    def _on_key_gtk3(self, _widget, event, pressed):
        return self._handle_gtk_key(event.keyval, event.hardware_keycode, int(event.state), pressed)

    def present(self, rgb_bytes: bytes, w: int, h: int, flags: int = 0):
        self.last_raw = (rgb_bytes, w, h, int(flags))
        text_mode = rj_keyboard.is_text_mode()
        self.last_text_mode = text_mode
        want_hud = self.show_hud and not (int(flags) & hdmi_shm.FLAG_NO_HUD)
        if want_hud:
            disp_bytes, dw, dh = compose_hud_overlay(rgb_bytes, w, h, text_mode=text_mode)
        else:
            disp_bytes, dw, dh = rgb_bytes, w, h

        sig = (dw, dh, disp_bytes)
        if self._last_disp_sig == sig:
            return
        self._last_disp_sig = sig

        if self.gtk_major == 4:
            gbytes = self.GLib.Bytes.new(disp_bytes)
            tex = self.Gdk.MemoryTexture.new(
                dw, dh, self.Gdk.MemoryFormat.R8G8B8, gbytes, dw * 3
            )
            self.picture.set_paintable(tex)
        else:
            from gi.repository import GdkPixbuf

            pb = GdkPixbuf.Pixbuf.new_from_data(
                disp_bytes, GdkPixbuf.Colorspace.RGB, False, 8, dw, dh, dw * 3
            )
            self.image_widget.set_from_pixbuf(pb)

    def poll_events(self):
        ctx = self.GLib.MainContext.default()
        while ctx.pending():
            ctx.iteration(False)
        if self.show_hud and self.last_raw and not (self.last_raw[3] & hdmi_shm.FLAG_NO_HUD):
            cur_tm = rj_keyboard.is_text_mode()
            if cur_tm != self.last_text_mode:
                self.present(*self.last_raw)

    def close(self):
        rj_keyboard.clear_all_keys()
        try:
            self.win.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Backend 3: Linux HDMI Framebuffer (/dev/fb0) for headless TTY / Raspberry Pi
# ---------------------------------------------------------------------------

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
_VT_GETSTATE = 0x5603
_VT_ACTIVATE = 0x5606
_VT_WAITACTIVE = 0x5607
_KIOSK_HANDOFF_FLAG = "/dev/shm/rj_kiosk_handoff"


def _fb_get_active_vt() -> Optional[int]:
    for dev in ("/dev/tty0", "/dev/console"):
        try:
            fd = os.open(dev, os.O_RDONLY)
            try:
                buf = bytearray(6)
                fcntl.ioctl(fd, _VT_GETSTATE, buf)
                vt = struct.unpack("<H", buf[:2])[0]
                return int(vt) if vt > 0 else None
            finally:
                os.close(fd)
        except OSError:
            continue
    return None


def _fb_switch_vt(vt_num: int) -> bool:
    for dev in ("/dev/tty0", "/dev/console"):
        try:
            fd = os.open(dev, os.O_RDWR)
            try:
                fcntl.ioctl(fd, _VT_ACTIVATE, int(vt_num))
                fcntl.ioctl(fd, _VT_WAITACTIVE, int(vt_num))
                return True
            finally:
                os.close(fd)
        except OSError:
            continue
    return False


def _fb_clear_vt(vt_num: int) -> None:
    try:
        fd = os.open(f"/dev/tty{int(vt_num)}", os.O_WRONLY | os.O_NOCTTY)
        try:
            os.write(fd, b"\x1b[2J\x1b[H\x1b[?25l")
        finally:
            os.close(fd)
    except OSError:
        pass


def _find_hdmi_fb_device() -> str:
    env_fb = os.environ.get("RJ_HDMI_FB_DEVICE", "").strip()
    if env_fb and os.path.exists(env_fb):
        return env_fb
    preferred = []
    fallback = []
    for idx in range(8):
        dev = f"/dev/fb{idx}"
        if not os.path.exists(dev):
            continue
        name = ""
        try:
            with open(f"/sys/class/graphics/fb{idx}/name", "r", encoding="utf-8") as handle:
                name = handle.read().strip().lower()
        except Exception:
            pass
        if any(skip in name for skip in ("st7789v_m5st", "panel-mipi-dbi")):
            continue
        if any(good in name for good in ("vc4", "drm", "simple", "efi", "i915", "amdgpu", "nouveau", "vesa")):
            preferred.append(dev)
        else:
            fallback.append(dev)
    if preferred:
        return preferred[0]
    if fallback:
        return fallback[0]
    return "/dev/fb0"


def _query_fb_info(fd: int) -> dict:
    """Query Linux fb_var_screeninfo and fb_fix_screeninfo via ioctl."""
    vbuf = bytearray(160)
    fbuf = bytearray(80)
    fcntl.ioctl(fd, FBIOGET_VSCREENINFO, vbuf)
    fcntl.ioctl(fd, FBIOGET_FSCREENINFO, fbuf)

    xres, yres = struct.unpack_from("<II", vbuf, 0)
    bpp = struct.unpack_from("<I", vbuf, 24)[0]
    red_off, red_len = struct.unpack_from("<II", vbuf, 32)
    green_off, green_len = struct.unpack_from("<II", vbuf, 44)
    blue_off, blue_len = struct.unpack_from("<II", vbuf, 56)

    # struct fb_fix_screeninfo: char id[16]; unsigned long smem_start; u32 smem_len;
    # u32 type; u32 type_aux; u32 visual; u16 xpanstep; u16 ypanstep; u16 ywrapstep; u32 line_length;
    fix_fmt = "16sLIIIIHHHI"
    unpacked = struct.unpack_from(fix_fmt, fbuf, 0)
    smem_len = unpacked[2]
    line_length = unpacked[9]

    return {
        "width": xres,
        "height": yres,
        "bpp": bpp,
        "red_off": red_off,
        "red_len": red_len,
        "green_off": green_off,
        "green_len": green_len,
        "blue_off": blue_off,
        "blue_len": blue_len,
        "smem_len": smem_len,
        "line_length": line_length or (xres * max(2, bpp // 8)),
    }


class _FBDevBackend:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.scale_mode = str(cfg.get("scale_mode", "fit"))
        self.fbdev_filter = str(cfg.get("fbdev_filter", "fast") or "fast").strip().lower()
        self.show_hud = bool(cfg.get("show_hud", True))
        self.fb_dev = _find_hdmi_fb_device()
        self.fd = os.open(self.fb_dev, os.O_RDWR)
        self.info = _query_fb_info(self.fd)
        map_size = max(self.info["smem_len"], self.info["line_length"] * self.info["height"])
        self.mm = mmap.mmap(self.fd, map_size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        self.running = True
        self._last_frame = None
        self._last_text_mode = False
        self._last_geom = None
        self._last_disp_bytes: Optional[bytes] = None
        self._prev_disp_arr: Optional[np.ndarray] = None
        self._fb_buf = _init_fb_buffer(
            self.info["width"],
            self.info["height"],
            self.info["bpp"],
            self.info["line_length"],
            self.info["red_off"],
            self.info["green_off"],
            self.info["blue_off"],
        )
        self._prev_f10 = False
        self._prev_f12 = False
        self._prev_vt: Optional[int] = None
        self._host_vt: Optional[int] = None
        # Switch headless fbdev display onto a clean unused VT (default VT8) so getty@tty1
        # never bleeds 'login:' text over RaspyJack or steals keystrokes during app handoffs.
        if os.environ.get("RJ_HDMI_NO_VT_SWITCH", "0") != "1":
            target_vt = max(2, min(12, int(os.environ.get("RJ_HDMI_VT", "8") or "8")))
            cur_vt = _fb_get_active_vt()
            if cur_vt is not None and cur_vt != target_vt:
                if _fb_switch_vt(target_vt):
                    self._prev_vt = cur_vt
                    self._host_vt = target_vt
                    _fb_clear_vt(target_vt)
            elif cur_vt == target_vt:
                self._host_vt = target_vt
                _fb_clear_vt(target_vt)
        self._set_cursor_blink(False)
        self._unblank()

    def _unblank(self):
        blank_path = self.fb_dev.replace("/dev/", "/sys/class/graphics/") + "/blank"
        try:
            with open(blank_path, "w", encoding="utf-8") as handle:
                handle.write("0")
        except Exception:
            pass

    @staticmethod
    def _set_cursor_blink(enable: bool):
        try:
            with open("/sys/class/graphics/fbcon/cursor_blink", "w", encoding="utf-8") as handle:
                handle.write("1" if enable else "0")
        except Exception:
            pass

    def present(self, rgb_bytes: bytes, w: int, h: int, flags: int = 0):
        flags_i = int(flags)
        self._last_frame = (rgb_bytes, w, h, flags_i)
        text_mode = rj_keyboard.is_text_mode()
        self._last_text_mode = text_mode
        no_hud = bool(flags_i & hdmi_shm.FLAG_NO_HUD)
        want_hud = self.show_hud and not no_hud
        if want_hud:
            disp_bytes, dw, dh = compose_hud_overlay(rgb_bytes, w, h, text_mode=text_mode)
        else:
            disp_bytes, dw, dh = rgb_bytes, w, h

        if not isinstance(disp_bytes, bytes):
            disp_bytes = bytes(disp_bytes)

        use_fast = no_hud or (self.fbdev_filter != "smooth")
        geom = (dw, dh, want_hud, use_fast)
        geom_changed = (geom != self._last_geom)
        if not geom_changed and self._last_disp_bytes is not None and disp_bytes == self._last_disp_bytes:
            return

        dirty_rows: Optional[Tuple[int, int]] = None
        cur_arr = None
        if geom_changed:
            # Clear letterbox areas only when source geometry/HUD height changes
            self._fb_buf = _init_fb_buffer(
                self.info["width"],
                self.info["height"],
                self.info["bpp"],
                self.info["line_length"],
                self.info["red_off"],
                self.info["green_off"],
                self.info["blue_off"],
            )
            self._last_geom = geom
            self._prev_disp_arr = None
        elif not no_hud:
            # UI mode: detect dirty row band so typing/scrolling updates only changed rows.
            # Video mode (FLAG_NO_HUD) skips this scan since full motion changes every row.
            cur_arr = np.frombuffer(disp_bytes, dtype=np.uint8).reshape((dh, dw * 3))
            if self._prev_disp_arr is not None and self._prev_disp_arr.shape == cur_arr.shape:
                diff_rows = np.flatnonzero(np.any(cur_arr != self._prev_disp_arr, axis=1))
                if diff_rows.size == 0:
                    self._last_disp_bytes = disp_bytes
                    self._prev_disp_arr = cur_arr
                    return
                dirty_rows = (int(diff_rows[0]), int(diff_rows[-1]) + 1)

        _vx, vy, _vw, vh, dy0, dy1 = _blit_rgb24_into_fb_buf_dirty(
            self._fb_buf,
            disp_bytes,
            dw,
            dh,
            self.info["width"],
            self.info["height"],
            self.info["bpp"],
            red_off=self.info["red_off"],
            green_off=self.info["green_off"],
            blue_off=self.info["blue_off"],
            scale_mode=self.scale_mode,
            fast_scale=use_fast,
            dirty_rows=dirty_rows,
        )
        self._last_disp_bytes = disp_bytes
        if not no_hud:
            if cur_arr is None:
                cur_arr = np.frombuffer(disp_bytes, dtype=np.uint8).reshape((dh, dw * 3))
            self._prev_disp_arr = cur_arr
        else:
            self._prev_disp_arr = None

        if geom_changed:
            self.mm.seek(0)
            self.mm.write(memoryview(self._fb_buf))
        elif dy1 > dy0:
            row_start = vy + dy0
            row_end = vy + dy1
            if row_start == 0 and row_end == self.info["height"]:
                self.mm.seek(0)
                self.mm.write(memoryview(self._fb_buf))
            else:
                stride = self._fb_buf.shape[1]
                self.mm.seek(row_start * stride)
                self.mm.write(memoryview(self._fb_buf[row_start:row_end]))

    def poll_events(self):
        # Support F10 (HUD toggle), F12 (screenshot), and Ctrl+Alt+Backspace (exit) in headless fbdev mode
        f10 = rj_keyboard.is_key_pressed(68)
        if f10 and not self._prev_f10:
            self.show_hud = not self.show_hud
            if self._last_frame is not None:
                self.present(*self._last_frame)
        self._prev_f10 = f10

        f12 = rj_keyboard.is_key_pressed(88)
        if f12 and not self._prev_f12 and self._last_frame is not None:
            save_screenshot(self._last_frame[0], self._last_frame[1], self._last_frame[2])
        self._prev_f12 = f12

        if self.show_hud and self._last_frame is not None and not (self._last_frame[3] & hdmi_shm.FLAG_NO_HUD):
            cur_tm = rj_keyboard.is_text_mode()
            if cur_tm != self._last_text_mode:
                self.present(*self._last_frame)

        mods = rj_keyboard.get_modifiers()
        if (mods & rj_keyboard.MOD_CTRL_ANY) and (mods & rj_keyboard.MOD_ALT_ANY) and rj_keyboard.is_key_pressed(14):
            self.running = False

    def close(self):
        handoff_active = os.path.exists(_KIOSK_HANDOFF_FLAG)
        try:
            self.mm.seek(0)
            self.mm.write(b"\x00" * (self.info["line_length"] * self.info["height"]))
        except Exception:
            pass
        if not handoff_active:
            self._set_cursor_blink(True)
            if self._prev_vt is not None:
                _fb_switch_vt(self._prev_vt)
        try:
            self.mm.close()
        except Exception:
            pass
        try:
            os.close(self.fd)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Backend Selection & Host Main Loop
# ---------------------------------------------------------------------------

def _create_backend(cfg: dict):
    req = str(cfg.get("backend", "auto") or "auto").lower()
    has_gui = rj_env.has_desktop_session()

    if req == "sdl2":
        return _SDL2Backend(cfg), True
    if req == "gtk":
        return _GTKBackend(cfg), True
    if req == "fbdev":
        return _FBDevBackend(cfg), False

    # Auto selection
    errors = []
    if has_gui:
        for cls in (_SDL2Backend, _GTKBackend):
            try:
                return cls(cfg), True
            except Exception as exc:
                errors.append(f"{cls.__name__}: {exc}")
    # Headless HDMI console / Raspberry Pi TTY
    for cls, owns_kbd in ((_FBDevBackend, False), (_SDL2Backend, True)):
        try:
            return cls(cfg), owns_kbd
        except Exception as exc:
            errors.append(f"{cls.__name__}: {exc}")

    raise RuntimeError("No usable HDMI display backend found: " + "; ".join(errors))


def _bind_wake_socket() -> Optional[socket.socket]:
    path = hdmi_shm.WAKE_SOCK_PATH
    try:
        if os.path.exists(path):
            os.unlink(path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(path)
        try:
            os.chmod(path, 0o666)
        except OSError:
            pass
        sock.setblocking(False)
        return sock
    except Exception:
        return None


def main() -> int:
    # Ensure XDG_RUNTIME_DIR exists (prevents Wayland/SDL2/GTK warnings under sudo/systemd)
    if not os.environ.get("XDG_RUNTIME_DIR") or not os.path.isdir(os.environ.get("XDG_RUNTIME_DIR", "")):
        _euid = os.geteuid()
        _rdir = f"/run/user/{_euid}"
        if not os.path.isdir(_rdir):
            _rdir = f"/tmp/rj-runtime-{_euid}"
            try:
                os.makedirs(_rdir, mode=0o700, exist_ok=True)
            except OSError:
                pass
        if os.path.isdir(_rdir):
            os.environ["XDG_RUNTIME_DIR"] = _rdir

    cfg = rj_env.get_hdmi_config()
    backend, owns_desktop_kbd = _create_backend(cfg)

    with open(hdmi_shm.HOST_PID_PATH, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))

    # Always register hdmi_host as the single keyboard owner (desktop OR headless fbdev)
    # so raspyjack.py and child payloads read SHM instead of opening duplicate evdev handles.
    rj_keyboard.register_host(os.getpid())
    evdev_started = False
    if not owns_desktop_kbd:
        import evdev_keys

        grab_kbd = os.environ.get("RJ_HDMI_GRAB_KBD", "1").strip().lower() not in ("0", "false", "no", "off")
        evdev_keys.start(as_host=True, grab=grab_kbd)
        evdev_started = True

    wake_sock = _bind_wake_socket()
    stop_flag = False

    def _on_signal(*_args):
        nonlocal stop_flag
        stop_flag = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # Render initial Obsidian Ember boot placeholder until raspyjack.py publishes first frame
    init_w, init_h = cfg["width"], cfg["height"]
    boot_img = Image.new("RGB", (init_w, init_h), rj_theme.EMBER_RGB["bg_base"])
    d = ImageDraw.Draw(boot_img)
    card_w = min(init_w - 32, max(220, int(init_w * 0.62)))
    card_h = min(init_h - 32, max(84, int(init_h * 0.42)))
    cx0 = (init_w - card_w) // 2
    cy0 = (init_h - card_h) // 2
    rj_theme.draw_card(
        d,
        (cx0, cy0, cx0 + card_w, cy0 + card_h),
        fill=rj_theme.EMBER_THEME["bg_surface"],
        outline=rj_theme.EMBER_THEME["border_accent"],
        radius=10,
        accent_left=rj_theme.EMBER_THEME["accent"],
        accent_width=4,
    )
    title_px = max(14, min(24, init_h // 16))
    sub_px = max(10, min(13, init_h // 28))
    d.text((init_w // 2, init_h // 2 - sub_px), "EMBERJACK", font=rj_env.load_font(title_px, bold=True), fill=rj_theme.EMBER_THEME["accent_bright"], anchor="mm")
    d.text((init_w // 2, init_h // 2 + sub_px + 2), "Initializing HDMI display & keyboard...", font=rj_env.load_font(sub_px), fill=rj_theme.EMBER_THEME["text_muted"], anchor="mm")
    backend.present(boot_img.tobytes(), init_w, init_h)

    last_seq = 0
    frame_interval = 1.0 / max(5, int(cfg.get("fps", 30)))
    # Allow slight early-arrival jitter (up to 50% of frame_interval) so 20-30fps video
    # frames never suffer an extra sleep cycle from phase mismatch with ffmpeg.
    min_frame_dt = min(0.015, frame_interval * 0.50)
    last_present_at = 0.0
    last_active_at = time.monotonic()

    try:
        while backend.running and not stop_flag:
            backend.poll_events()

            now = time.monotonic()
            pending_seq, pending_flags = hdmi_shm.peek_seq_and_flags()
            frame_ready = (pending_seq != 0 and pending_seq != last_seq)
            is_video_frame = bool(pending_flags & hdmi_shm.FLAG_NO_HUD)
            # Video frames (FLAG_NO_HUD) are already smoothly paced by the player itself;
            # never stack a secondary post-blit throttle on top of them, and stamp
            # last_present_at BEFORE present() so blit time doesn't inflate frame spacing.
            if frame_ready and (is_video_frame or (now - last_present_at) >= min_frame_dt):
                frame = hdmi_shm.read_frame(last_seq=last_seq)
                if frame is not None:
                    seq, w, h, rgb_bytes, flags = frame
                    last_seq = seq
                    last_present_at = time.monotonic()
                    backend.present(rgb_bytes, w, h, flags=flags)
                    last_active_at = time.monotonic()
                    pending_seq, pending_flags = hdmi_shm.peek_seq_and_flags()
                    frame_ready = (pending_seq != 0 and pending_seq != last_seq)
                    is_video_frame = bool(pending_flags & hdmi_shm.FLAG_NO_HUD)

            # Headless fbdev wakes instantly on wake_sock, so a 100ms idle timeout
            # cuts idle wakeups 6x with zero added display latency. Desktop SDL2/GTK
            # relaxes gently to 40ms when idle for >1s to keep input snappy and cool.
            if not owns_desktop_kbd:
                wait_s = min(0.025, frame_interval) if (now - last_active_at) < 0.5 else 0.10
            else:
                wait_s = min(0.016, frame_interval) if (now - last_active_at) < 1.0 else 0.04

            # Never sleep 25-100ms while a newer frame is already waiting in SHM!
            if frame_ready:
                if is_video_frame:
                    wait_s = 0.001
                else:
                    rem = min_frame_dt - (time.monotonic() - last_present_at)
                    wait_s = max(0.001, min(wait_s, rem))

            if wake_sock is not None:
                ready, _, _ = select.select([wake_sock], [], [], wait_s)
                if ready:
                    last_active_at = time.monotonic()
                    try:
                        while True:
                            wake_sock.recv(64)
                    except BlockingIOError:
                        pass
            else:
                time.sleep(wait_s)
    finally:
        if evdev_started:
            try:
                import evdev_keys

                evdev_keys.stop()
            except Exception:
                pass
        rj_keyboard.unregister_host(os.getpid())
        backend.close()
        if wake_sock is not None:
            try:
                wake_sock.close()
            except Exception:
                pass
        for path in (hdmi_shm.HOST_PID_PATH, hdmi_shm.WAKE_SOCK_PATH):
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
