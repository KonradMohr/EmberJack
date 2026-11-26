"""
Display helper for payloads – automatic 128-base coordinate scaling.

Usage in a payload:
    from payloads._display_helper import ScaledDraw, scaled_font

    img = Image.new("RGB", (WIDTH, HEIGHT), "black")
    d = ScaledDraw(img)           # drop-in replacement for ImageDraw.Draw
    font = scaled_font()          # readable font for current resolution

All pixel coordinates passed to d.text(), d.rectangle(), d.line(), etc.
are automatically scaled from 128-base to the actual LCD / HDMI resolution.
"""

import os
from PIL import ImageDraw, ImageFont

try:
    import rj_env
    _DISPLAY_TYPE = rj_env.get_display_type()
except Exception:
    rj_env = None
    _DISPLAY_TYPE = "ST7735_128"

try:
    import rj_theme as _rj_theme
except Exception:
    _rj_theme = None

EMBER_REMAP_ENABLED = os.environ.get("RJ_LEGACY_EMBER_REMAP", "1") != "0"

try:
    import LCD_1in44
    _LCD_W = LCD_1in44.LCD_WIDTH
    _LCD_H = LCD_1in44.LCD_HEIGHT
    _DISPLAY_TYPE = getattr(LCD_1in44, "_DISPLAY_TYPE", _DISPLAY_TYPE)
except Exception:
    if _DISPLAY_TYPE == "HDMI" and rj_env is not None:
        _hcfg = rj_env.get_hdmi_config()
        _LCD_W, _LCD_H = _hcfg["width"], _hcfg["height"]
    elif _DISPLAY_TYPE == "ST7789_240":
        _LCD_W, _LCD_H = 240, 240
    elif _DISPLAY_TYPE == "CARDPUTER_320":
        _LCD_W, _LCD_H = 320, 170
    else:
        _LCD_W, _LCD_H = 128, 128

DISPLAY_TYPE = _DISPLAY_TYPE
LCD_SCALE_X = _LCD_W / 128
LCD_SCALE_Y = _LCD_H / 128
LCD_SCALE = min(LCD_SCALE_X, LCD_SCALE_Y)

_is_square = abs(LCD_SCALE_X - LCD_SCALE_Y) < 0.01


def is_hdmi() -> bool:
    return DISPLAY_TYPE == "HDMI"


class _HDMIFBProxy:
    """Drop-in mmap-compatible proxy routing raw RGB565 writes to LCD.show_raw_frame()."""

    def __init__(self, lcd, width: int, height: int):
        self._lcd = lcd
        self._w = int(width)
        self._h = int(height)
        self._buf = bytearray(self._w * self._h * 2)
        self._pos = 0

    def seek(self, offset: int, whence: int = 0):
        if whence == 0:
            self._pos = max(0, offset)
        elif whence == 1:
            self._pos = max(0, self._pos + offset)
        elif whence == 2:
            self._pos = max(0, len(self._buf) + offset)

    def write(self, data):
        mv = memoryview(data)
        n = len(mv)
        if self._pos == 0 and n == len(self._buf):
            self._lcd.show_raw_frame(bytes(mv), self._w, self._h, fmt="rgb565")
            self._pos = n
            return n
        end = min(len(self._buf), self._pos + n)
        self._buf[self._pos:end] = mv[: end - self._pos]
        self._pos = end
        if self._pos >= len(self._buf):
            self._lcd.show_raw_frame(bytes(self._buf), self._w, self._h, fmt="rgb565")
        return n

    def flush(self):
        self._lcd.show_raw_frame(bytes(self._buf), self._w, self._h, fmt="rgb565")

    def close(self):
        pass


def open_framebuffer(lcd, fb_device: str, width: int, height: int):
    """Return (fb_fd, fb_map) for raw RGB565 streaming.

    In HDMI mode (or if /dev/fb* is unavailable), returns (None, _HDMIFBProxy)
    so callers' existing fb_map.seek(0)/fb_map.write(raw)/fb_map.close() loops
    stream directly to the HDMI display without corrupting /dev/fb0.
    """
    import os
    import mmap as _mmap

    if getattr(lcd, "display_type", DISPLAY_TYPE) == "HDMI":
        return None, _HDMIFBProxy(lcd, width, height)
    try:
        fb_size = int(width) * int(height) * 2
        fd = os.open(fb_device, os.O_RDWR)
        mm = _mmap.mmap(fd, fb_size, _mmap.MAP_SHARED, _mmap.PROT_WRITE | _mmap.PROT_READ)
        return fd, mm
    except Exception:
        return None, _HDMIFBProxy(lcd, width, height)


def SX(v):
    """Scale a 128-base X coordinate to current display width."""
    return int(v * LCD_SCALE_X)


def SY(v):
    """Scale a 128-base Y coordinate to current display height."""
    return int(v * LCD_SCALE_Y)


def S(v):
    """Scale a 128-base pixel value uniformly (fonts, line widths)."""
    return int(v * LCD_SCALE)


def scaled_font(size=10, bold=False, mono=False, icon=False):
    """Return a TrueType font scaled for the current display across Linux distros.

    *size* is the desired point size on a 128px screen; the returned font
    is proportionally larger on bigger panels and HDMI monitors.
    """
    scaled_size = max(6, S(size))
    if rj_env is not None:
        return rj_env.load_font(scaled_size, bold=bold, mono=mono, icon=icon)
    try:
        return ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", scaled_size
        )
    except Exception:
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# ScaledDraw – wraps ImageDraw.Draw, scaling all 128-base coordinates
# ---------------------------------------------------------------------------
def _scale_point(pt):
    """Scale a 2-tuple or 2-list."""
    if _is_square:
        return (S(pt[0]), S(pt[1]))
    return (SX(pt[0]), SY(pt[1]))


def _scale_coords(coords):
    """Scale a flat sequence of coordinates or a list of point tuples."""
    if not coords:
        return coords
    if isinstance(coords[0], (list, tuple)):
        return [_scale_point(p) for p in coords]
    if len(coords) == 4:
        if _is_square:
            return [S(coords[0]), S(coords[1]), S(coords[2]), S(coords[3])]
        return [SX(coords[0]), SY(coords[1]), SX(coords[2]), SY(coords[3])]
    if len(coords) == 2:
        return _scale_point(coords)
    return coords


class ScaledDraw:
    """Drop-in replacement for ``ImageDraw.Draw`` that auto-scales 128-base coordinates
    and optionally normalizes harsh legacy neon/pure-black colors to Obsidian Ember.

    If ``LCD_SCALE_X == 1.0 and LCD_SCALE_Y == 1.0`` (128x128), coordinate passthrough
    adds zero scaling overhead.
    """

    def __init__(self, image, remap_colors: bool = True):
        self._image = image
        self._draw = ImageDraw.Draw(image)
        self._passthrough = (LCD_SCALE_X == 1.0 and LCD_SCALE_Y == 1.0)
        self._remap = bool(remap_colors and EMBER_REMAP_ENABLED and _rj_theme is not None)
        # Warm pure #000000 legacy canvases to Obsidian Ember #0E0E10 automatically
        if self._remap and getattr(image, "mode", "") == "RGB":
            try:
                w, h = image.size
                if w > 0 and h > 0 and image.getpixel((0, 0)) == (0, 0, 0) and image.getpixel((w - 1, h - 1)) == (0, 0, 0):
                    self._draw.rectangle((0, 0, w - 1, h - 1), fill=_rj_theme.EMBER_RGB["bg_base"])
            except Exception:
                pass

    def _c(self, color):
        if self._remap and color is not None:
            return _rj_theme.remap_legacy_color(color)
        return color

    # -- Scaled drawing primitives ------------------------------------------

    def text(self, xy, text, fill=None, font=None, anchor=None, **kw):
        if not self._passthrough:
            xy = _scale_point(xy)
        self._draw.text(xy, text, fill=self._c(fill), font=font, anchor=anchor, **kw)

    def rectangle(self, xy, fill=None, outline=None, width=1, **kw):
        if not self._passthrough:
            xy = _scale_coords(xy)
            width = max(1, S(width)) if width > 1 else width
        self._draw.rectangle(xy, fill=self._c(fill), outline=self._c(outline), width=width, **kw)

    def rounded_rectangle(self, xy, radius=0, fill=None, outline=None, width=1, **kw):
        if not self._passthrough:
            xy = _scale_coords(xy)
            radius = max(1, S(radius)) if radius > 0 else 0
            width = max(1, S(width)) if width > 1 else width
        self._draw.rounded_rectangle(xy, radius=radius, fill=self._c(fill), outline=self._c(outline), width=width, **kw)

    def line(self, xy, fill=None, width=1, **kw):
        if not self._passthrough:
            xy = _scale_coords(xy)
            width = max(1, S(width)) if width > 1 else width
        self._draw.line(xy, fill=self._c(fill), width=width, **kw)

    def ellipse(self, xy, fill=None, outline=None, width=1, **kw):
        if not self._passthrough:
            if (not _is_square) and isinstance(xy, (list, tuple)) and len(xy) == 4 and not isinstance(xy[0], (list, tuple)):
                bw = abs(xy[2] - xy[0])
                bh = abs(xy[3] - xy[1])
                if abs(bw - bh) <= 2 and bw <= 24:
                    cx = SX((xy[0] + xy[2]) * 0.5)
                    cy = SY((xy[1] + xy[3]) * 0.5)
                    r = max(1, int(round(S(bw) * 0.5)))
                    xy = [cx - r, cy - r, cx + r, cy + r]
                else:
                    xy = _scale_coords(xy)
            else:
                xy = _scale_coords(xy)
            width = max(1, S(width)) if width > 1 else width
        self._draw.ellipse(xy, fill=self._c(fill), outline=self._c(outline), width=width, **kw)

    def polygon(self, xy, fill=None, outline=None, **kw):
        if not self._passthrough:
            xy = _scale_coords(xy)
        self._draw.polygon(xy, fill=self._c(fill), outline=self._c(outline), **kw)

    def arc(self, xy, start, end, fill=None, width=1, **kw):
        if not self._passthrough:
            xy = _scale_coords(xy)
            width = max(1, S(width)) if width > 1 else width
        self._draw.arc(xy, start, end, fill=self._c(fill), width=width, **kw)

    def pieslice(self, xy, start, end, fill=None, outline=None, width=1, **kw):
        if not self._passthrough:
            xy = _scale_coords(xy)
            width = max(1, S(width)) if width > 1 else width
        self._draw.pieslice(xy, start, end, fill=self._c(fill), outline=self._c(outline), width=width, **kw)

    def textbbox(self, xy, text, font=None, **kw):
        if not self._passthrough:
            xy = _scale_point(xy)
        return self._draw.textbbox(xy, text, font=font, **kw)

    def textsize(self, text, font=None, **kw):
        """Pillow 10+ compatibility shim for legacy payloads calling draw.textsize()."""
        bb = self._draw.textbbox((0, 0), str(text or ""), font=font)
        return (bb[2] - bb[0], bb[3] - bb[1])

    def textlength(self, text, font=None, **kw):
        return self._draw.textlength(text, font=font, **kw)

    # -- Passthrough for anything else --------------------------------------
    def __getattr__(self, name):
        return getattr(self._draw, name)
