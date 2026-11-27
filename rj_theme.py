#!/usr/bin/env python3
"""
RaspyJack Shared Design System — "Obsidian Ember" (Eye-Friendly Orange & Black)
-------------------------------------------------------------------------------
Zero-dependency theme constants, WCAG contrast utilities, presets, ANSI 16-color
mapping, and PIL drawing primitives shared across raspyjack.py, rj_terminal.py,
hdmi_host.py, wifi/wifi_lcd_interface.py, payloads/_keyboard_helper.py, and core
utility payload GUIs.

Palette Philosophy:
  - Avoids harsh pure #000000 + neon #FF6600/#05FF00 glare and halation.
  - Uses warm charcoal-obsidian surfaces (#0E0E10, #16161A, #1F1E22),
    desaturated burnt-amber accents (#E07A2F, #F2994A), roasted-amber
    selection fills (#2D1D12), and warm parchment body text (#E6DFD7).
"""

import functools
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Core Obsidian Ember Palette (Hex)
# ---------------------------------------------------------------------------

EMBER_THEME: Dict[str, str] = {
    "bg_base": "#0E0E10",        # Primary canvas (warm obsidian)
    "bg_surface": "#16161A",     # Cards, panels, modals
    "bg_elevated": "#1F1E22",    # Top bar, headers, keycaps, pills
    "bg_well": "#111114",        # Terminal viewport, input fields
    "bg_overlay": "#141316",     # HUD bar, dimmed overlays
    "border_subtle": "#2B2826",  # Hairline dividers, card frames
    "border_accent": "#B85E24",  # Active card/modal border, focus ring
    "accent": "#E07A2F",         # Primary burnt-amber brand accent
    "accent_bright": "#F2994A",  # Soft tangerine headings, active icons
    "accent_dim": "#8C4A1C",     # Muted copper for tracks/secondary marks
    "select_bg": "#2D1D12",      # Selected row/card fill (warm roasted amber)
    "select_border": "#E07A2F",  # Left selection bar / active card outline
    "text_primary": "#E6DFD7",   # Readable warm parchment text
    "text_bright": "#F5EFE8",    # High-emphasis titles
    "text_muted": "#9B928A",     # Secondary labels, descriptions
    "text_dim": "#6B645E",       # Subtle hints, disabled items
    "text_on_accent": "#120C08", # Dark ink on solid amber buttons
    "ok": "#5EB87A",             # Soft sage green (connected / active)
    "ok_bg": "#15261C",          # Muted green pill surface
    "warn": "#E6A845",           # Soft honey gold (warning / scanning)
    "warn_bg": "#2B2112",        # Muted honey pill surface
    "danger": "#D95B55",         # Muted coral-red (error / stop)
    "danger_bg": "#2B1616",      # Muted red pill surface
    "info": "#D98C4A",           # Warm copper info tone
}


@functools.lru_cache(maxsize=256)
def hex_to_rgb(color_hex: str) -> Tuple[int, int, int]:
    """Convert '#RRGGBB' or '#RGB' to an (R, G, B) tuple."""
    s = str(color_hex or "#000000").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        return (14, 14, 16)
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return (14, 14, 16)


def rgb_to_hex(rgb: Tuple[int, int, int]) -> str:
    r, g, b = (max(0, min(255, int(c))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


@functools.lru_cache(maxsize=512)
def blend_hex(c1: str, c2: str, alpha: float) -> str:
    """Linearly interpolate between two hex colors (alpha in [0.0, 1.0])."""
    a = max(0.0, min(1.0, float(alpha)))
    r1, g1, b1 = hex_to_rgb(c1)
    r2, g2, b2 = hex_to_rgb(c2)
    return rgb_to_hex((
        int(round(r1 + (r2 - r1) * a)),
        int(round(g1 + (g2 - g1) * a)),
        int(round(b1 + (b2 - b1) * a)),
    ))


def relative_luminance(color_hex: str) -> float:
    """Compute WCAG 2.1 relative luminance in [0.0, 1.0]."""
    def _chan(c: int) -> float:
        v = c / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = hex_to_rgb(color_hex)
    return 0.2126 * _chan(r) + 0.7152 * _chan(g) + 0.0722 * _chan(b)


def contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    """Return WCAG contrast ratio between two hex colors (1.0 .. 21.0)."""
    l1 = relative_luminance(fg_hex)
    l2 = relative_luminance(bg_hex)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


# RGB tuple lookup for callers working with PIL RGB tuples
EMBER_RGB: Dict[str, Tuple[int, int, int]] = {
    k: hex_to_rgb(v) for k, v in EMBER_THEME.items()
}

# ---------------------------------------------------------------------------
# Color Config Presets for gui_conf.json ("COLORS" section)
# ---------------------------------------------------------------------------

THEME_PRESETS: Dict[str, Dict[str, str]] = {
    "Obsidian Ember": {
        "BACKGROUND": "#0E0E10",
        "BORDER": "#B85E24",
        "TEXT": "#E6DFD7",
        "SELECTED_TEXT": "#F2994A",
        "SELECTED_TEXT_BACKGROUND": "#2D1D12",
        "GAMEPAD": "#E07A2F",
        "GAMEPAD_FILL": "#F2994A",
    },
    "Deep Amber": {
        "BACKGROUND": "#0D0B09",
        "BORDER": "#A65A1E",
        "TEXT": "#EBE3D8",
        "SELECTED_TEXT": "#F0A854",
        "SELECTED_TEXT_BACKGROUND": "#2B1E10",
        "GAMEPAD": "#D9822B",
        "GAMEPAD_FILL": "#F0A854",
    },
    "Carbon Copper": {
        "BACKGROUND": "#101114",
        "BORDER": "#9E5329",
        "TEXT": "#E2DDD8",
        "SELECTED_TEXT": "#EC8F56",
        "SELECTED_TEXT_BACKGROUND": "#291D18",
        "GAMEPAD": "#D4723B",
        "GAMEPAD_FILL": "#EC8F56",
    },
    "Classic Green": {
        "BACKGROUND": "#0B0F0C",
        "BORDER": "#257845",
        "TEXT": "#D8EAE0",
        "SELECTED_TEXT": "#5FD18B",
        "SELECTED_TEXT_BACKGROUND": "#142E1D",
        "GAMEPAD": "#2EA85C",
        "GAMEPAD_FILL": "#5FD18B",
    },
}

DEFAULT_GUI_COLORS: Dict[str, str] = dict(THEME_PRESETS["Obsidian Ember"])

# Exact legacy neon-green defaults shipped in older RaspyJack gui_conf.json
LEGACY_NEON_DEFAULTS: Dict[str, str] = {
    "BORDER": "#05ff00",
    "BACKGROUND": "#000000",
    "TEXT": "#05ff00",
    "SELECTED_TEXT": "#00ff55",
    "SELECTED_TEXT_BACKGROUND": "#2d0fff",
}


def should_migrate_legacy_colors(colors: Optional[dict]) -> bool:
    """Return True if *colors* matches the old unmodified neon-green defaults."""
    if not isinstance(colors, dict):
        return False
    for key, legacy_val in LEGACY_NEON_DEFAULTS.items():
        cur = str(colors.get(key, "") or "").strip().lower()
        if cur != legacy_val:
            return False
    return True


# ---------------------------------------------------------------------------
# Eye-Friendly ANSI 16-Color Table (for rj_terminal.py & xterm parity)
# ---------------------------------------------------------------------------

ANSI_16_EMBER: List[Tuple[int, int, int]] = [
    (26, 24, 24),     # 0  Black (warm charcoal)
    (217, 91, 85),    # 1  Red (muted coral)
    (94, 184, 122),   # 2  Green (soft sage)
    (230, 168, 69),   # 3  Yellow (warm honey)
    (110, 158, 206),  # 4  Blue (soft slate blue)
    (194, 132, 184),  # 5  Magenta (muted mauve)
    (102, 184, 178),  # 6  Cyan (muted teal)
    (214, 206, 198),  # 7  White (warm stone)
    (112, 105, 99),   # 8  Bright Black (gray)
    (232, 118, 110),  # 9  Bright Red
    (124, 204, 148),  # 10 Bright Green
    (242, 186, 94),   # 11 Bright Yellow / Amber
    (138, 180, 222),  # 12 Bright Blue
    (212, 156, 204),  # 13 Bright Magenta
    (132, 204, 198),  # 14 Bright Cyan
    (242, 236, 228),  # 15 Bright White (warm cream)
]


def ansi_256_to_rgb(index: int) -> Tuple[int, int, int]:
    """Convert xterm 256-color index (0..255) to an eye-friendly RGB tuple."""
    idx = max(0, min(255, int(index)))
    if idx < 16:
        return ANSI_16_EMBER[idx]
    if idx < 232:
        n = idx - 16
        r = n // 36
        g = (n % 36) // 6
        b = n % 6
        levels = (0, 95, 135, 175, 215, 245)
        return (levels[r], levels[g], levels[b])
    gray = 8 + (idx - 232) * 10
    return (gray, gray, gray)


# ---------------------------------------------------------------------------
# Reusable PIL Drawing Helpers (works with ImageDraw.Draw & ScaledDraw)
# ---------------------------------------------------------------------------

def _safe_rounded_rect(draw, box, radius: int, fill=None, outline=None, width: int = 1):
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return
    r = max(0, min(int(radius), int((x1 - x0) // 2), int((y1 - y0) // 2)))
    if r > 0 and hasattr(draw, "rounded_rectangle"):
        try:
            draw.rounded_rectangle((x0, y0, x1, y1), radius=r, fill=fill, outline=outline, width=width)
            return
        except Exception:
            pass
    draw.rectangle((x0, y0, x1, y1), fill=fill, outline=outline, width=width)


def draw_card(
    draw,
    box: Tuple[int, int, int, int],
    fill: str = EMBER_THEME["bg_surface"],
    outline: Optional[str] = EMBER_THEME["border_subtle"],
    radius: int = 6,
    accent_left: Optional[str] = None,
    accent_width: int = 3,
    width: int = 1,
) -> None:
    """Draw a rounded Obsidian Ember card with optional left accent stripe."""
    _safe_rounded_rect(draw, box, radius=radius, fill=fill, outline=outline, width=width)
    if accent_left:
        x0, y0, _x1, y1 = box
        inset = max(1, min(3, radius // 2))
        aw = max(2, int(accent_width))
        _safe_rounded_rect(
            draw,
            (x0 + 1, y0 + inset, x0 + 1 + aw, y1 - inset),
            radius=max(1, aw // 2),
            fill=accent_left,
        )


# ---------------------------------------------------------------------------
# Cached Text Measurement & Wrapping Helpers
# ---------------------------------------------------------------------------

_TEXT_BBOX_CACHE: Dict[tuple, Tuple[int, int, int, int]] = {}
_TRUNCATE_CACHE: Dict[tuple, str] = {}
_WRAP_CACHE: Dict[tuple, Tuple[str, ...]] = {}


def _font_cache_key(font) -> tuple:
    if font is None:
        return (0, "", 0)
    return (id(font), getattr(font, "path", ""), getattr(font, "size", 0))


def measure_text(font, text: str, draw=None) -> Tuple[int, int, int, int]:
    """Return cached (x0, y0, x1, y1) bounding box for single-line *text*."""
    s = str(text or "")
    if not s:
        return (0, 0, 0, 0)
    if "\n" in s and draw is not None:
        return draw.textbbox((0, 0), s, font=font)
    key = (_font_cache_key(font), s)
    cached = _TEXT_BBOX_CACHE.get(key)
    if cached is not None:
        return cached
    if font is not None and hasattr(font, "getbbox"):
        bbox = font.getbbox(s)
    elif draw is not None:
        bbox = draw.textbbox((0, 0), s, font=font)
    else:
        bbox = (0, 0, len(s) * 6, 10)
    if len(_TEXT_BBOX_CACHE) >= 4096:
        _TEXT_BBOX_CACHE.clear()
    _TEXT_BBOX_CACHE[key] = bbox
    return bbox


def draw_pill(
    draw,
    x: int,
    y: int,
    text: str,
    font,
    bg: str = EMBER_THEME["select_bg"],
    fg: str = EMBER_THEME["accent_bright"],
    outline: Optional[str] = EMBER_THEME["border_accent"],
    pad_x: int = 6,
    pad_y: int = 2,
    radius: int = 4,
) -> Tuple[int, int]:
    """Draw a compact pill badge at (x, y) and return (pill_w, pill_h)."""
    bbox = measure_text(font, text, draw)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pw = tw + pad_x * 2
    ph = th + pad_y * 2
    _safe_rounded_rect(draw, (x, y, x + pw, y + ph), radius=radius, fill=bg, outline=outline)
    draw.text((x + pad_x - bbox[0], y + pad_y - bbox[1]), text, font=font, fill=fg)
    return pw, ph


def draw_scrollbar(
    draw,
    x: int,
    y0: int,
    y1: int,
    offset: int,
    visible: int,
    total: int,
    width: int = 3,
    track_color: str = EMBER_THEME["bg_elevated"],
    thumb_color: str = EMBER_THEME["accent"],
) -> None:
    """Draw a proportional vertical scrollbar when total > visible."""
    if total <= visible or y1 <= y0:
        return
    track_h = y1 - y0
    _safe_rounded_rect(draw, (x, y0, x + width, y1), radius=max(1, width // 2), fill=track_color)
    thumb_h = max(8, int(track_h * (visible / max(1, total))))
    max_off = max(1, total - visible)
    ratio = max(0.0, min(1.0, offset / max_off))
    thumb_y = y0 + int((track_h - thumb_h) * ratio)
    _safe_rounded_rect(
        draw,
        (x, thumb_y, x + width, min(y1, thumb_y + thumb_h)),
        radius=max(1, width // 2),
        fill=thumb_color,
    )


def draw_progress_bar(
    draw,
    box: Tuple[int, int, int, int],
    fraction: float,
    fill: str = EMBER_THEME["accent"],
    track: str = EMBER_THEME["bg_well"],
    outline: Optional[str] = EMBER_THEME["border_subtle"],
    radius: int = 3,
    bg: Optional[str] = None,
) -> None:
    """Draw a sleek horizontal progress bar for fraction in [0.0, 1.0]."""
    x0, y0, x1, y1 = box
    frac = max(0.0, min(1.0, float(fraction)))
    track_col = bg if bg is not None else track
    _safe_rounded_rect(draw, box, radius=radius, fill=track_col, outline=outline)
    inner_w = max(0, (x1 - x0) - 2)
    fill_w = int(round(inner_w * frac))
    if fill_w > 0:
        _safe_rounded_rect(
            draw,
            (x0 + 1, y0 + 1, x0 + 1 + fill_w, y1 - 1),
            radius=max(1, radius - 1),
            fill=fill,
        )


def draw_signal_bars(
    draw,
    x: int,
    y: int,
    height: int,
    pct: int,
    active_color: str = EMBER_THEME["accent_bright"],
    inactive_color: str = EMBER_THEME["border_subtle"],
) -> None:
    """Draw a 4-bar wireless signal strength indicator."""
    bars = 4
    bar_w = max(2, height // 5)
    gap = max(1, bar_w // 2)
    filled = 0
    if pct >= 75:
        filled = 4
    elif pct >= 50:
        filled = 3
    elif pct >= 25:
        filled = 2
    elif pct > 0:
        filled = 1
    for i in range(bars):
        bh = max(2, int(height * (i + 1) / bars))
        bx = x + i * (bar_w + gap)
        by = y + height - bh
        col = active_color if i < filled else inactive_color
        draw.rectangle((bx, by, bx + bar_w, y + height), fill=col)


def truncate_text(draw, text: str, max_width: int, font, ellipsis: str = "...") -> str:
    """Truncate *text* with *ellipsis* so its rendered width fits *max_width*."""
    s = str(text or "")
    mw = int(max_width)
    if not s or mw <= 0:
        return ""
    key = (_font_cache_key(font), s, mw, ellipsis)
    cached = _TRUNCATE_CACHE.get(key)
    if cached is not None:
        return cached

    bbox = measure_text(font, s, draw)
    if (bbox[2] - bbox[0]) <= mw:
        res = s
    else:
        eb = measure_text(font, ellipsis, draw)
        ew = eb[2] - eb[0]
        if ew >= mw:
            res = ellipsis[:1]
        else:
            lo, hi = 0, len(s)
            best = ""
            while lo <= hi:
                mid = (lo + hi) // 2
                cand = s[:mid]
                cb = measure_text(font, cand, draw)
                if (cb[2] - cb[0]) + ew <= mw:
                    best = cand
                    lo = mid + 1
                else:
                    hi = mid - 1
            res = best + ellipsis
    if len(_TRUNCATE_CACHE) >= 2048:
        _TRUNCATE_CACHE.clear()
    _TRUNCATE_CACHE[key] = res
    return res


def wrap_text(draw, text: str, max_width: int, font) -> List[str]:
    """Word-wrap *text* so each line fits within *max_width* pixels."""
    s = str(text or "")
    mw = int(max_width)
    key = (_font_cache_key(font), s, mw)
    cached = _WRAP_CACHE.get(key)
    if cached is not None:
        return list(cached)

    lines: List[str] = []
    for raw_line in (s.splitlines() or [""]):
        if not raw_line:
            lines.append("")
            continue
        words = raw_line.split(" ")
        current = ""
        for word in words:
            trial = word if not current else f"{current} {word}"
            tb = measure_text(font, trial, draw)
            if (tb[2] - tb[0]) <= mw:
                current = trial
                continue
            if current:
                lines.append(current)
                current = word
            else:
                chunk = ""
                for ch in word:
                    cand = chunk + ch
                    cb = measure_text(font, cand, draw)
                    if (cb[2] - cb[0]) <= mw:
                        chunk = cand
                    else:
                        if chunk:
                            lines.append(chunk)
                        chunk = ch
                current = chunk
        lines.append(current)
    res = tuple(lines)
    if len(_WRAP_CACHE) >= 1024:
        _WRAP_CACHE.clear()
    _WRAP_CACHE[key] = res
    return list(res)


# ---------------------------------------------------------------------------
# Legacy Color -> Obsidian Ember Palette Mapper (for ScaledDraw & Legacy UIs)
# ---------------------------------------------------------------------------

_LEGACY_STR_MAP: Dict[str, str] = {
    # Pure black / legacy dark backgrounds -> warm matte obsidian surfaces
    "black": EMBER_THEME["bg_base"],
    "#000": EMBER_THEME["bg_base"],
    "#000000": EMBER_THEME["bg_base"],
    "#050505": EMBER_THEME["bg_base"],
    "#080808": EMBER_THEME["bg_base"],
    "#0a0a0a": EMBER_THEME["bg_base"],
    "#0c0c0c": EMBER_THEME["bg_base"],
    "#0d0d0d": EMBER_THEME["bg_base"],
    "#111": EMBER_THEME["bg_surface"],
    "#111111": EMBER_THEME["bg_surface"],
    "#121212": EMBER_THEME["bg_surface"],
    "#141414": EMBER_THEME["bg_surface"],
    "#181818": EMBER_THEME["bg_surface"],
    "#1a1a1a": EMBER_THEME["bg_surface"],
    "#001a1a": EMBER_THEME["bg_surface"],
    "#001122": EMBER_THEME["bg_surface"],
    "#001428": EMBER_THEME["bg_surface"],
    "#0a0a2e": EMBER_THEME["bg_surface"],
    "#0a1020": EMBER_THEME["bg_surface"],
    "#0f172a": EMBER_THEME["bg_surface"],
    "#101820": EMBER_THEME["bg_surface"],
    "#112244": EMBER_THEME["bg_elevated"],
    "#1a1a2e": EMBER_THEME["bg_surface"],
    "#16213e": EMBER_THEME["bg_elevated"],
    "#222": EMBER_THEME["bg_elevated"],
    "#222222": EMBER_THEME["bg_elevated"],
    # Legacy harsh blue / green selection bars -> warm roasted amber
    "#06303a": EMBER_THEME["select_bg"],
    "#003366": EMBER_THEME["select_bg"],
    "#002244": EMBER_THEME["select_bg"],
    "#003344": EMBER_THEME["select_bg"],
    "#004466": EMBER_THEME["select_bg"],
    "#003300": EMBER_THEME["select_bg"],
    "#004400": EMBER_THEME["select_bg"],
    "#2d0fff": EMBER_THEME["select_bg"],
    "navy": EMBER_THEME["select_bg"],
    "darkblue": EMBER_THEME["select_bg"],
    # Legacy harsh cyan / blue highlights -> warm burnt amber
    "cyan": EMBER_THEME["accent_bright"],
    "aqua": EMBER_THEME["accent_bright"],
    "blue": EMBER_THEME["accent"],
    "deepskyblue": EMBER_THEME["accent_bright"],
    "dodgerblue": EMBER_THEME["accent"],
    "#00ffff": EMBER_THEME["accent_bright"],
    "#00e5ff": EMBER_THEME["accent_bright"],
    "#00d4ff": EMBER_THEME["accent_bright"],
    "#00ccff": EMBER_THEME["accent_bright"],
    "#00aaff": EMBER_THEME["accent_bright"],
    "#0088ff": EMBER_THEME["accent"],
    "#0066ff": EMBER_THEME["accent"],
    "#0000ff": EMBER_THEME["accent"],
    "#38bdf8": EMBER_THEME["accent_bright"],
    "#58a6ff": EMBER_THEME["accent_bright"],
    "#0ff": EMBER_THEME["accent_bright"],
    "#00f": EMBER_THEME["accent"],
    # Legacy neon green -> soft sage ok
    "lime": EMBER_THEME["ok"],
    "green": EMBER_THEME["ok"],
    "#0f0": EMBER_THEME["ok"],
    "#00ff00": EMBER_THEME["ok"],
    "#05ff00": EMBER_THEME["ok"],
    "#00ff55": EMBER_THEME["ok"],
    "#00ff88": EMBER_THEME["ok"],
    "#00cc00": EMBER_THEME["ok"],
    "#22c55e": EMBER_THEME["ok"],
    # Legacy harsh red -> muted coral danger
    "red": EMBER_THEME["danger"],
    "#f00": EMBER_THEME["danger"],
    "#ff0000": EMBER_THEME["danger"],
    "#ff3333": EMBER_THEME["danger"],
    "#ff4444": EMBER_THEME["danger"],
    "#ff5252": EMBER_THEME["danger"],
    # Legacy harsh yellow / orange -> warm honey warn / ember accent
    "yellow": EMBER_THEME["warn"],
    "gold": EMBER_THEME["warn"],
    "orange": EMBER_THEME["accent"],
    "#ff0": EMBER_THEME["warn"],
    "#ffff00": EMBER_THEME["warn"],
    "#ffaa00": EMBER_THEME["warn"],
    "#ffcc00": EMBER_THEME["warn"],
    "#ff8800": EMBER_THEME["accent"],
    "#ff6600": EMBER_THEME["accent"],
    # Legacy magenta -> soft copper-mauve
    "magenta": "#C284B8",
    "#ff00ff": "#C284B8",
    # Pure white / harsh grays -> warm parchment & stone grays
    "white": EMBER_THEME["text_primary"],
    "#fff": EMBER_THEME["text_primary"],
    "#ffffff": EMBER_THEME["text_primary"],
    "#eee": EMBER_THEME["text_primary"],
    "#eeeeee": EMBER_THEME["text_primary"],
    "#ddd": EMBER_THEME["text_primary"],
    "#cccccc": EMBER_THEME["text_primary"],
    "#ccc": EMBER_THEME["text_primary"],
    "gray": EMBER_THEME["text_muted"],
    "grey": EMBER_THEME["text_muted"],
    "#aaa": EMBER_THEME["text_muted"],
    "#aaaaaa": EMBER_THEME["text_muted"],
    "#999": EMBER_THEME["text_muted"],
    "#999999": EMBER_THEME["text_muted"],
    "#888": EMBER_THEME["text_muted"],
    "#888888": EMBER_THEME["text_muted"],
    "#777": EMBER_THEME["text_dim"],
    "#777777": EMBER_THEME["text_dim"],
    "#666": EMBER_THEME["text_dim"],
    "#666666": EMBER_THEME["text_dim"],
    "#555": EMBER_THEME["text_dim"],
    "#555555": EMBER_THEME["text_dim"],
    "#444": EMBER_THEME["border_subtle"],
    "#444444": EMBER_THEME["border_subtle"],
    "#333": EMBER_THEME["border_subtle"],
    "#333333": EMBER_THEME["border_subtle"],
}

_LEGACY_RGB_MAP: Dict[Tuple[int, int, int], Tuple[int, int, int]] = {
    (0, 0, 0): EMBER_RGB["bg_base"],
    (10, 10, 10): EMBER_RGB["bg_base"],
    (17, 17, 17): EMBER_RGB["bg_surface"],
    (20, 20, 20): EMBER_RGB["bg_surface"],
    (26, 26, 26): EMBER_RGB["bg_surface"],
    (0, 0, 40): EMBER_RGB["bg_surface"],
    (0, 26, 26): EMBER_RGB["bg_surface"],
    (0, 0, 255): EMBER_RGB["accent"],
    (0, 255, 255): EMBER_RGB["accent_bright"],
    (0, 229, 255): EMBER_RGB["accent_bright"],
    (0, 204, 255): EMBER_RGB["accent_bright"],
    (0, 255, 0): EMBER_RGB["ok"],
    (5, 255, 0): EMBER_RGB["ok"],
    (255, 0, 0): EMBER_RGB["danger"],
    (255, 255, 0): EMBER_RGB["warn"],
    (255, 165, 0): EMBER_RGB["accent"],
    (255, 255, 255): EMBER_RGB["text_primary"],
    (128, 128, 128): EMBER_RGB["text_muted"],
    (136, 136, 136): EMBER_RGB["text_muted"],
}


def remap_legacy_color(color):
    """Map harsh 1990s neon/pure-black legacy UI colors to soft Obsidian Ember equivalents.

    Preserves native EMBER_THEME values, non-legacy custom colors, None, and input types
    (str -> str, RGB tuple -> RGB tuple).
    """
    if color is None:
        return None
    if isinstance(color, str):
        key = color.strip().lower()
        return _LEGACY_STR_MAP.get(key, color)
    if isinstance(color, tuple) and len(color) == 3:
        return _LEGACY_RGB_MAP.get(color, color)
    return color


# ---------------------------------------------------------------------------
# High-Level Adaptive Obsidian Ember App Chrome Primitives
# ---------------------------------------------------------------------------

def load_app_fonts(width: int, height: int) -> dict:
    """Return a standard resolution-adaptive font bundle for any EmberJack utility screen."""
    import rj_env

    wide = width >= 300
    large = width >= 540 and height >= 300
    if large:
        t_px, b_px, s_px, tag_px = 15, 13, 11, 10
    elif wide:
        t_px, b_px, s_px, tag_px = 12, 10, 9, 8
    elif width >= 200:
        t_px, b_px, s_px, tag_px = 11, 10, 8, 8
    else:
        t_px, b_px, s_px, tag_px = 9, 8, 7, 7
    return {
        "wide": wide,
        "large": large,
        "title": rj_env.load_font(t_px, bold=True),
        "body": rj_env.load_font(b_px, bold=False),
        "body_bold": rj_env.load_font(b_px, bold=True),
        "sub": rj_env.load_font(s_px, bold=False),
        "badge": rj_env.load_font(tag_px, bold=True, mono=True),
        "mono": rj_env.load_font(b_px, bold=False, mono=True),
    }


def draw_app_header(
    draw,
    width: int,
    title: str,
    fonts: dict,
    badge: str = "",
    right_text: str = "",
    right_pill: str = "",
    hdr_h: Optional[int] = None,
) -> int:
    """Draw a standard Obsidian Ember top app header bar and return its pixel height."""
    wide = bool(fonts.get("wide"))
    large = bool(fonts.get("large"))
    h = int(hdr_h) if hdr_h is not None else (30 if large else (24 if wide else 18))
    draw.rectangle((0, 0, width - 1, h - 1), fill=EMBER_THEME["bg_elevated"])
    draw.rectangle((0, 0, 3 if wide else 2, h - 1), fill=EMBER_THEME["accent"])
    draw.line([(0, h - 1), (width - 1, h - 1)], fill=EMBER_THEME["border_subtle"], width=1)

    x = 10 if wide else 5
    tb = measure_text(fonts["title"], title, draw)
    draw.text((x, (h - (tb[3] - tb[1])) // 2 - tb[1]), title, font=fonts["title"], fill=EMBER_THEME["accent_bright"])
    x += (tb[2] - tb[0]) + (8 if wide else 4)

    if badge:
        bw, _ = draw_pill(
            draw, x, max(2, (h - (16 if wide else 11)) // 2),
            badge, fonts["badge"], bg=EMBER_THEME["select_bg"], fg=EMBER_THEME["accent_bright"],
            outline=EMBER_THEME["border_accent"], pad_x=5 if wide else 3, pad_y=1, radius=3,
        )
        x += bw + 6

    rx = width - (8 if wide else 4)
    if right_pill and wide:
        pw = measure_text(fonts["badge"], right_pill, draw)[2] + 10
        draw_pill(
            draw, rx - pw, max(2, (h - 16) // 2),
            right_pill, fonts["badge"], bg=EMBER_THEME["bg_surface"], fg=EMBER_THEME["text_primary"],
            outline=EMBER_THEME["border_subtle"], pad_x=5, pad_y=1, radius=3,
        )
        rx -= pw + 8

    if right_text and wide and (rx - x) > 55:
        rt = truncate_text(draw, right_text, rx - x - 4, fonts["sub"])
        rb = measure_text(fonts["sub"], rt, draw)
        draw.text((rx - (rb[2] - rb[0]), (h - (rb[3] - rb[1])) // 2 - rb[1]), rt, font=fonts["sub"], fill=EMBER_THEME["text_muted"])
    return h


def draw_app_footer(
    draw,
    width: int,
    height: int,
    hint: str,
    fonts: dict,
    right_text: str = "",
    ftr_h: Optional[int] = None,
) -> int:
    """Draw a standard Obsidian Ember bottom hint footer bar and return its pixel height."""
    wide = bool(fonts.get("wide"))
    large = bool(fonts.get("large"))
    h = int(ftr_h) if ftr_h is not None else (22 if large else (18 if wide else 13))
    draw.rectangle((0, height - h, width - 1, height - 1), fill=EMBER_THEME["bg_elevated"])
    draw.line([(0, height - h), (width - 1, height - h)], fill=EMBER_THEME["border_subtle"], width=1)
    pad = 8 if wide else 3
    avail = width - pad * 2
    if right_text and wide:
        rb = measure_text(fonts["badge"], right_text, draw)
        rw = rb[2] - rb[0]
        draw.text((width - pad - rw, height - h + (h - (rb[3] - rb[1])) // 2 - rb[1]), right_text, font=fonts["badge"], fill=EMBER_THEME["accent_bright"])
        avail = max(40, avail - rw - 10)
    txt = truncate_text(draw, hint, avail, fonts["sub"])
    hb = measure_text(fonts["sub"], txt, draw)
    draw.text((pad, height - h + (h - (hb[3] - hb[1])) // 2 - hb[1]), txt, font=fonts["sub"], fill=EMBER_THEME["text_muted"])
    return h


def draw_status_card(
    draw,
    width: int,
    height: int,
    title: str,
    sub: str = "",
    kind: str = "info",
    foot: str = "",
    fonts: Optional[dict] = None,
) -> None:
    """Draw a centered Obsidian Ember modal status card (info / ok / warn / err)."""
    f = fonts or load_app_fonts(width, height)
    wide = bool(f.get("wide"))
    cw = min(width - 16, max(112, int(width * 0.72)))
    ch = 96 if wide else 64
    cx0 = (width - cw) // 2
    cy0 = (height - ch) // 2
    accent = (
        EMBER_THEME["danger"] if kind == "err"
        else EMBER_THEME["ok"] if kind == "ok"
        else EMBER_THEME["warn"] if kind == "warn"
        else EMBER_THEME["accent"]
    )
    draw_card(
        draw, (cx0, cy0, cx0 + cw, cy0 + ch),
        fill=EMBER_THEME["bg_surface"], outline=accent,
        radius=8 if wide else 4, accent_left=accent, accent_width=4 if wide else 2,
    )
    tx = cx0 + (16 if wide else 8)
    draw.text((tx, cy0 + (14 if wide else 8)), truncate_text(draw, title, cw - 24, f["title"]), font=f["title"], fill=EMBER_THEME["accent_bright"] if kind == "info" else accent)
    if sub:
        draw.text((tx, cy0 + (40 if wide else 24)), truncate_text(draw, sub, cw - 24, f["body"]), font=f["body"], fill=EMBER_THEME["text_primary"])
    if foot:
        draw.text((tx, cy0 + ch - (22 if wide else 15)), truncate_text(draw, foot, cw - 24, f["sub"]), font=f["sub"], fill=EMBER_THEME["text_muted"])
