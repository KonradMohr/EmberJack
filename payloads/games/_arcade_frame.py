#!/usr/bin/env python3
"""
EmberJack Arcade Bezel Compositor — Obsidian Ember
--------------------------------------------------
Pure-PIL, high-FPS retro game bezel compositor for classic 128x128 arcade games.

- On small/square displays (128x128, 240x240): crisp Image.NEAREST scaling directly
  to the full display with zero bezel overhead.
- On widescreen HDMI (640x360) and Cardputer (320x170): renders a cached Obsidian
  Ember arcade bezel with centered 1:1 square pixel-art viewport, subtle #B85E24
  ember border, left title/score/best/status panel, and right controls panel.

Zero external dependencies beyond stdlib, Pillow (PIL), rj_theme, and rj_env.
"""

import os
import sys
from collections import OrderedDict
from typing import Any, Dict, Iterable, Optional, Tuple

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PIL import Image, ImageDraw

import rj_env
import rj_theme

DEFAULT_CONTROLS: Tuple[str, ...] = (
    "Arrows: Move",
    "OK / Enter: Action",
    "K1 / K2: Extra",
    "Esc / K3: Exit",
)

try:
    _NEAREST = Image.Resampling.NEAREST
except AttributeError:
    _NEAREST = Image.NEAREST

# Bounded two-level LRU caches for sub-millisecond per-frame compositing:
#   1) _STATIC_BEZEL_CACHE: static bezel + layout geometry per (w, h, title, controls)
#   2) _HUD_BEZEL_CACHE: static bezel + rendered dynamic score/best/status panel
_STATIC_BEZEL_CACHE: "OrderedDict[tuple, Tuple[Image.Image, Dict[str, Any]]]" = OrderedDict()
_HUD_BEZEL_CACHE: "OrderedDict[tuple, Image.Image]" = OrderedDict()
_MAX_STATIC_CACHE = 12
_MAX_HUD_CACHE = 16


def _lru_get(cache: OrderedDict, key: tuple):
    val = cache.get(key)
    if val is not None:
        cache.move_to_end(key)
    return val


def _lru_put(cache: OrderedDict, key: tuple, val: Any, limit: int) -> None:
    cache[key] = val
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def _normalize_controls(controls: Optional[Iterable[Any]]) -> Tuple[str, ...]:
    if controls is None:
        return DEFAULT_CONTROLS
    out = []
    for item in controls:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            k_s = str(item[0]).strip()
            v_s = str(item[1]).strip()
            txt = f"{k_s}: {v_s}" if (k_s and v_s) else (k_s or v_s)
        else:
            txt = str(item).strip()
        if txt:
            out.append(txt)
    return tuple(out) if out else DEFAULT_CONTROLS


def _status_colors(status_text: str) -> Tuple[str, str, str]:
    """Return (bg, fg, outline) hex colors semantically matched to *status_text*."""
    T = rj_theme.EMBER_THEME
    up = status_text.upper()
    if any(k in up for k in ("OVER", "BREACH", "FAIL", "LOSE", "AI WIN", "DEAD")):
        return T["danger_bg"], T["danger"], T["danger"]
    if any(k in up for k in ("WIN", "VICTORY", "SOLVED", "COMPLETE", "CLEAR", "DONE", "GO!")):
        return T["ok_bg"], T["ok"], T["ok"]
    if any(k in up for k in ("PAUSE", "WAIT", "WATCH", "THINK", "READY", "START", "PREP")):
        return T["warn_bg"], T["warn"], T["warn"]
    return T["select_bg"], T["accent_bright"], T["border_accent"]


def _load_bezel_fonts(wide_hd: bool) -> Dict[str, Any]:
    if wide_hd:
        return {
            "badge": rj_env.load_font(9, bold=True, mono=True),
            "micro": rj_env.load_font(8, bold=True, mono=True),
            "title": rj_env.load_font(14, bold=True),
            "section": rj_env.load_font(10, bold=True),
            "label": rj_env.load_font(9, bold=True),
            "score": rj_env.load_font(16, bold=True, mono=True),
            "score_sm": rj_env.load_font(12, bold=True, mono=True),
            "best": rj_env.load_font(13, bold=True, mono=True),
            "status": rj_env.load_font(10, bold=True, mono=True),
            "key": rj_env.load_font(9, bold=True, mono=True),
            "action": rj_env.load_font(11, bold=False),
            "foot": rj_env.load_font(8, bold=False, mono=True),
        }
    return {
        "badge": rj_env.load_font(7, bold=True, mono=True),
        "micro": rj_env.load_font(7, bold=True, mono=True),
        "title": rj_env.load_font(9, bold=True),
        "section": rj_env.load_font(8, bold=True),
        "label": rj_env.load_font(7, bold=True),
        "score": rj_env.load_font(10, bold=True, mono=True),
        "score_sm": rj_env.load_font(8, bold=True, mono=True),
        "best": rj_env.load_font(9, bold=True, mono=True),
        "status": rj_env.load_font(8, bold=True, mono=True),
        "key": rj_env.load_font(8, bold=True, mono=True),
        "action": rj_env.load_font(8, bold=False),
        "foot": rj_env.load_font(7, bold=False, mono=True),
    }


def _compute_layout(width: int, height: int) -> Dict[str, Any]:
    wide_hd = width >= 480 and height >= 240
    if wide_hd:
        v_margin = 20 if height >= 340 else 12
        min_side = 136
        raw_vp = min(height - v_margin * 2, width - min_side * 2)
        vp_size = max(128, (raw_vp // 8) * 8)
        vp_x = (width - vp_size) // 2
        vp_y = (height - vp_size) // 2
        outer_pad = 10
        inner_gap = 12
        panel_top = max(8, vp_y - 6)
        panel_bot = min(height - 9, vp_y + vp_size + 5)
        left_box = (outer_pad, panel_top, vp_x - inner_gap, panel_bot)
        right_box = (vp_x + vp_size + inner_gap, panel_top, width - 1 - outer_pad, panel_bot)
    else:
        v_margin = 5 if height <= 180 else 8
        min_side = 68
        raw_vp = min(height - v_margin * 2, width - min_side * 2)
        vp_size = max(96, (raw_vp // 8) * 8)
        vp_x = (width - vp_size) // 2
        vp_y = (height - vp_size) // 2
        outer_pad = 3
        inner_gap = 6
        panel_top = max(2, vp_y - 2)
        panel_bot = min(height - 3, vp_y + vp_size + 1)
        left_box = (outer_pad, panel_top, vp_x - inner_gap, panel_bot)
        right_box = (vp_x + vp_size + inner_gap, panel_top, width - 1 - outer_pad, panel_bot)

    return {
        "width": width,
        "height": height,
        "wide_hd": wide_hd,
        "vp_x": vp_x,
        "vp_y": vp_y,
        "vp_size": vp_size,
        "left_box": left_box,
        "right_box": right_box,
    }


def _build_static_bezel(
    width: int,
    height: int,
    title: str,
    controls: Tuple[str, ...],
) -> Tuple[Image.Image, Dict[str, Any]]:
    T = rj_theme.EMBER_THEME
    layout = _compute_layout(width, height)
    wide_hd = layout["wide_hd"]
    fonts = _load_bezel_fonts(wide_hd)

    img = Image.new("RGB", (width, height), T["bg_base"])
    d = ImageDraw.Draw(img)

    vp_x = layout["vp_x"]
    vp_y = layout["vp_y"]
    vp_size = layout["vp_size"]
    lx0, ly0, lx1, ly1 = layout["left_box"]
    rx0, ry0, rx1, ry1 = layout["right_box"]

    # Subtle cabinet top/bottom accent rails on wide displays
    if wide_hd:
        d.line([(10, 6), (width - 11, 6)], fill=T["border_subtle"], width=1)
        d.line([(10, height - 7), (width - 11, height - 7)], fill=T["border_subtle"], width=1)
        mid_x = width // 2
        d.line([(mid_x - 36, 6), (mid_x + 36, 6)], fill=T["border_accent"], width=1)
        d.line([(mid_x - 36, height - 7), (mid_x + 36, height - 7)], fill=T["border_accent"], width=1)

        # Outer recessed bezel housing around square viewport
        hx0, hy0 = vp_x - 6, vp_y - 6
        hx1, hy1 = vp_x + vp_size + 5, vp_y + vp_size + 5
        rj_theme.draw_card(
            d,
            (hx0, hy0, hx1, hy1),
            fill=T["bg_overlay"],
            outline=T["border_subtle"],
            radius=6,
            width=1,
        )
        # Corner precision ember ticks
        tick = 8
        d.line([(hx0, hy0 + tick), (hx0, hy0), (hx0 + tick, hy0)], fill=T["accent"], width=1)
        d.line([(hx1 - tick, hy0), (hx1, hy0), (hx1, hy0 + tick)], fill=T["accent"], width=1)
        d.line([(hx0, hy1 - tick), (hx0, hy1), (hx0 + tick, hy1)], fill=T["accent"], width=1)
        d.line([(hx1 - tick, hy1), (hx1, hy1), (hx1, hy1 - tick)], fill=T["accent"], width=1)

    # Crisp #B85E24 ember border framing the square game viewport
    bx0, by0 = vp_x - 2, vp_y - 2
    bx1, by1 = vp_x + vp_size + 1, vp_y + vp_size + 1
    d.rectangle((bx0, by0, bx1, by1), fill=T["bg_well"], outline=T["border_accent"], width=2)

    # ------------------------------------------------------------------
    # Left Panel (Static Shell + Branding + Game Title)
    # ------------------------------------------------------------------
    if wide_hd:
        rj_theme.draw_card(
            d,
            (lx0, ly0, lx1, ly1),
            fill=T["bg_surface"],
            outline=T["border_subtle"],
            radius=8,
            accent_left=T["accent"],
            accent_width=3,
        )
        ix0 = lx0 + 11
        ix1 = lx1 - 9
        inner_w = max(20, ix1 - ix0)
        y = ly0 + 10

        # EmberJack Arcade Badge
        _, ph = rj_theme.draw_pill(
            d,
            ix0,
            y,
            "EMBERJACK",
            fonts["badge"],
            bg=T["select_bg"],
            fg=T["accent_bright"],
            outline=T["border_accent"],
            pad_x=5,
            pad_y=2,
            radius=4,
        )
        y += ph + 5
        d.text((ix0 + 1, y), "ARCADE CABINET", font=fonts["micro"], fill=T["text_dim"])
        y += 14

        # Game Title (up to 2 wrapped lines)
        title_str = str(title or "ARCADE").strip().upper()
        title_lines = rj_theme.wrap_text(d, title_str, inner_w, fonts["title"])[:2]
        for line in title_lines:
            clipped = rj_theme.truncate_text(d, line, inner_w, fonts["title"])
            d.text((ix0, y), clipped, font=fonts["title"], fill=T["text_bright"])
            y += 17

        # Accent divider below title
        y += 3
        d.line([(ix0, y), (ix1, y)], fill=T["border_subtle"], width=1)
        d.line([(ix0, y), (min(ix1, ix0 + 30), y)], fill=T["accent"], width=2)
        dyn_y = y + 10

        # Footer mark inside left panel
        foot_y = ly1 - 21
        d.line([(ix0, foot_y), (ix1, foot_y)], fill=T["border_subtle"], width=1)
        d.text((ix0, foot_y + 6), "1:1 PIXEL RETRO", font=fonts["foot"], fill=T["text_dim"])
        dyn_bottom = foot_y - 6
    else:
        # Compact Cardputer left panel
        rj_theme.draw_card(
            d,
            (lx0, ly0, lx1, ly1),
            fill=T["bg_surface"],
            outline=T["border_subtle"],
            radius=4,
        )
        d.rectangle((lx0 + 2, ly0 + 1, lx1 - 2, ly0 + 2), fill=T["accent"])
        ix0 = lx0 + 5
        ix1 = lx1 - 5
        inner_w = max(16, ix1 - ix0)
        y = ly0 + 5

        d.text(
            (ix0, y),
            rj_theme.truncate_text(d, "EMBERJACK", inner_w, fonts["badge"]),
            font=fonts["badge"],
            fill=T["accent"],
        )
        y += 11

        title_str = str(title or "ARCADE").strip().upper()
        title_lines = rj_theme.wrap_text(d, title_str, inner_w, fonts["title"])[:2]
        for line in title_lines:
            clipped = rj_theme.truncate_text(d, line, inner_w, fonts["title"])
            d.text((ix0, y), clipped, font=fonts["title"], fill=T["text_bright"])
            y += 11

        y += 2
        d.line([(ix0, y), (ix1, y)], fill=T["border_subtle"], width=1)
        d.line([(ix0, y), (min(ix1, ix0 + 18), y)], fill=T["border_accent"], width=1)
        dyn_y = y + 5
        dyn_bottom = ly1 - 5

    # ------------------------------------------------------------------
    # Right Panel (Controls Card — 100% Static, Baked Into Cache)
    # ------------------------------------------------------------------
    if wide_hd:
        rj_theme.draw_card(
            d,
            (rx0, ry0, rx1, ry1),
            fill=T["bg_surface"],
            outline=T["border_subtle"],
            radius=8,
        )
        rix0 = rx0 + 10
        rix1 = rx1 - 10
        r_inner_w = max(20, rix1 - rix0)
        ry = ry0 + 12

        # Header with ember pip
        d.rectangle((rix0, ry + 2, rix0 + 3, ry + 11), fill=T["accent"])
        d.text((rix0 + 8, ry), "CONTROLS", font=fonts["section"], fill=T["accent_bright"])
        ry += 18
        d.line([(rix0, ry), (rix1, ry)], fill=T["border_subtle"], width=1)
        ry += 10

        r_foot_y = ry1 - 21
        avail_h = max(40, r_foot_y - 6 - ry)
        items = list(controls)[:6]
        row_step = min(46, max(32, avail_h // max(1, len(items))))

        for item in items:
            if ry + 24 > r_foot_y - 2:
                break
            if ":" in item:
                key_part, act_part = item.split(":", 1)
                key_txt = rj_theme.truncate_text(d, key_part.strip(), r_inner_w - 10, fonts["key"])
                act_txt = rj_theme.truncate_text(d, act_part.strip(), r_inner_w - 2, fonts["action"])
                _, kph = rj_theme.draw_pill(
                    d,
                    rix0,
                    ry,
                    key_txt,
                    fonts["key"],
                    bg=T["bg_elevated"],
                    fg=T["accent_bright"],
                    outline=T["border_subtle"],
                    pad_x=5,
                    pad_y=2,
                    radius=4,
                )
                d.text((rix0 + 2, ry + kph + 3), act_txt, font=fonts["action"], fill=T["text_primary"])
            else:
                txt = rj_theme.truncate_text(d, item, r_inner_w, fonts["action"])
                d.text((rix0, ry + 4), txt, font=fonts["action"], fill=T["text_primary"])
            ry += row_step

        d.line([(rix0, r_foot_y), (rix1, r_foot_y)], fill=T["border_subtle"], width=1)
        d.text((rix0, r_foot_y + 6), "EMBERJACK ARCADE", font=fonts["foot"], fill=T["text_dim"])
    else:
        # Compact Cardputer right panel
        rj_theme.draw_card(
            d,
            (rx0, ry0, rx1, ry1),
            fill=T["bg_surface"],
            outline=T["border_subtle"],
            radius=4,
        )
        d.rectangle((rx0 + 2, ry0 + 1, rx1 - 2, ry0 + 2), fill=T["border_accent"])
        rix0 = rx0 + 5
        rix1 = rx1 - 5
        r_inner_w = max(16, rix1 - rix0)
        ry = ry0 + 5

        d.text((rix0, ry), "KEYS", font=fonts["section"], fill=T["accent_bright"])
        ry += 12
        d.line([(rix0, ry), (rix1, ry)], fill=T["border_subtle"], width=1)
        ry += 5

        avail_h = max(20, (ry1 - 4) - ry)
        items = list(controls)[:5]
        row_step = min(28, max(20, avail_h // max(1, len(items))))

        for item in items:
            if ry + 16 > ry1 - 3:
                break
            if ":" in item:
                key_part, act_part = item.split(":", 1)
                key_txt = rj_theme.truncate_text(d, key_part.strip(), r_inner_w, fonts["key"])
                act_txt = rj_theme.truncate_text(d, act_part.strip(), r_inner_w, fonts["action"])
                d.text((rix0, ry), key_txt, font=fonts["key"], fill=T["accent_bright"])
                d.text((rix0, ry + 9), act_txt, font=fonts["action"], fill=T["text_primary"])
            else:
                txt = rj_theme.truncate_text(d, item, r_inner_w, fonts["action"])
                d.text((rix0, ry + 2), txt, font=fonts["action"], fill=T["text_primary"])
            ry += row_step

    layout["fonts"] = fonts
    layout["dyn_x0"] = ix0
    layout["dyn_x1"] = ix1
    layout["dyn_y"] = dyn_y
    layout["dyn_bottom"] = dyn_bottom
    return img, layout


def _render_hud_bezel(
    static_img: Image.Image,
    layout: Dict[str, Any],
    score_str: Optional[str],
    best_str: Optional[str],
    status_str: str,
) -> Image.Image:
    T = rj_theme.EMBER_THEME
    bezel = static_img.copy()
    if score_str is None and best_str is None and not status_str:
        return bezel

    d = ImageDraw.Draw(bezel)
    wide_hd = layout["wide_hd"]
    fonts = layout["fonts"]
    ix0 = layout["dyn_x0"]
    ix1 = layout["dyn_x1"]
    cy = layout["dyn_y"]
    bottom = layout["dyn_bottom"]
    inner_w = max(16, ix1 - ix0)

    if wide_hd:
        # SCORE card
        if score_str is not None and cy + 44 <= bottom:
            card_h = 46
            rj_theme.draw_card(
                d,
                (ix0, cy, ix1, cy + card_h),
                fill=T["bg_elevated"],
                outline=T["border_accent"],
                radius=6,
                accent_left=T["accent"],
                accent_width=3,
            )
            d.text((ix0 + 9, cy + 6), "SCORE", font=fonts["label"], fill=T["text_muted"])
            sb = rj_theme.measure_text(fonts["score"], score_str, d)
            s_font = fonts["score"] if (sb[2] - sb[0]) <= (inner_w - 14) else fonts["score_sm"]
            val_txt = rj_theme.truncate_text(d, score_str, inner_w - 14, s_font)
            d.text((ix0 + 9, cy + 20), val_txt, font=s_font, fill=T["accent_bright"])
            cy += card_h + 8

        # BEST card
        if best_str is not None and cy + 40 <= bottom:
            card_h = 42
            rj_theme.draw_card(
                d,
                (ix0, cy, ix1, cy + card_h),
                fill=T["bg_well"],
                outline=T["border_subtle"],
                radius=6,
            )
            d.text((ix0 + 8, cy + 5), "BEST", font=fonts["label"], fill=T["text_muted"])
            best_txt = rj_theme.truncate_text(d, best_str, inner_w - 14, fonts["best"])
            d.text((ix0 + 8, cy + 19), best_txt, font=fonts["best"], fill=T["warn"])
            cy += card_h + 8

        # STATUS pill
        if status_str and cy + 24 <= bottom:
            s_bg, s_fg, s_out = _status_colors(status_str)
            pill_h = 26
            rj_theme.draw_card(
                d,
                (ix0, cy, ix1, cy + pill_h),
                fill=s_bg,
                outline=s_out,
                radius=5,
            )
            st_txt = rj_theme.truncate_text(d, status_str.upper(), inner_w - 10, fonts["status"])
            stb = rj_theme.measure_text(fonts["status"], st_txt, d)
            tx = ix0 + max(4, (inner_w - (stb[2] - stb[0])) // 2) - stb[0]
            ty = cy + max(2, (pill_h - (stb[3] - stb[1])) // 2) - stb[1]
            d.text((tx, ty), st_txt, font=fonts["status"], fill=s_fg)
    else:
        # Compact Cardputer dynamic HUD
        if score_str is not None and cy + 26 <= bottom:
            card_h = 28
            rj_theme.draw_card(
                d,
                (ix0, cy, ix1, cy + card_h),
                fill=T["bg_elevated"],
                outline=T["border_accent"],
                radius=4,
            )
            d.text((ix0 + 4, cy + 3), "SCORE", font=fonts["label"], fill=T["text_muted"])
            sb = rj_theme.measure_text(fonts["score"], score_str, d)
            s_font = fonts["score"] if (sb[2] - sb[0]) <= (inner_w - 8) else fonts["score_sm"]
            val_txt = rj_theme.truncate_text(d, score_str, inner_w - 8, s_font)
            d.text((ix0 + 4, cy + 13), val_txt, font=s_font, fill=T["accent_bright"])
            cy += card_h + 5

        if best_str is not None and cy + 24 <= bottom:
            card_h = 25
            rj_theme.draw_card(
                d,
                (ix0, cy, ix1, cy + card_h),
                fill=T["bg_well"],
                outline=T["border_subtle"],
                radius=4,
            )
            d.text((ix0 + 4, cy + 2), "BEST", font=fonts["label"], fill=T["text_muted"])
            best_txt = rj_theme.truncate_text(d, best_str, inner_w - 8, fonts["best"])
            d.text((ix0 + 4, cy + 11), best_txt, font=fonts["best"], fill=T["warn"])
            cy += card_h + 5

        if status_str and cy + 16 <= bottom:
            s_bg, s_fg, s_out = _status_colors(status_str)
            pill_h = 18
            rj_theme.draw_card(
                d,
                (ix0, cy, ix1, cy + pill_h),
                fill=s_bg,
                outline=s_out,
                radius=3,
            )
            st_txt = rj_theme.truncate_text(d, status_str.upper(), inner_w - 6, fonts["status"])
            stb = rj_theme.measure_text(fonts["status"], st_txt, d)
            tx = ix0 + max(2, (inner_w - (stb[2] - stb[0])) // 2) - stb[0]
            ty = cy + max(1, (pill_h - (stb[3] - stb[1])) // 2) - stb[1]
            d.text((tx, ty), st_txt, font=fonts["status"], fill=s_fg)

    return bezel


def compose_arcade_frame(
    width: int,
    height: int,
    game_img: Image.Image,
    title: str = "ARCADE",
    score: Any = None,
    best: Any = None,
    controls: Optional[Iterable[str]] = None,
    status: str = "",
) -> Image.Image:
    """Compose a retro game frame inside an Obsidian Ember arcade bezel.

    On small or square screens (``width <= 240`` or ``abs(width - height) <= 32``),
    returns *game_img* cleanly scaled with ``Image.NEAREST`` to ``(width, height)``.
    On widescreen displays (HDMI 640x360, Cardputer 320x170), composites the crisp
    1:1 game viewport centered inside a cached Obsidian Ember arcade bezel.
    """
    w = max(1, int(width))
    h = max(1, int(height))

    src = game_img if game_img.mode == "RGB" else game_img.convert("RGB")

    # Small or square display path — full-bleed crisp nearest scaling
    if w <= 240 or abs(w - h) <= 32:
        if src.size != (w, h):
            return src.resize((w, h), _NEAREST)
        return src

    title_key = str(title or "ARCADE").strip()
    controls_key = _normalize_controls(controls)
    static_key = (w, h, title_key, controls_key)

    cached_static = _lru_get(_STATIC_BEZEL_CACHE, static_key)
    if cached_static is None:
        cached_static = _build_static_bezel(w, h, title_key, controls_key)
        _lru_put(_STATIC_BEZEL_CACHE, static_key, cached_static, _MAX_STATIC_CACHE)
    static_img, layout = cached_static

    score_str = None if score is None else str(score)
    best_str = None if best is None else str(best)
    status_str = str(status or "").strip()

    hud_key = (static_key, score_str, best_str, status_str)
    hud_bezel = _lru_get(_HUD_BEZEL_CACHE, hud_key)
    if hud_bezel is None:
        hud_bezel = _render_hud_bezel(static_img, layout, score_str, best_str, status_str)
        _lru_put(_HUD_BEZEL_CACHE, hud_key, hud_bezel, _MAX_HUD_CACHE)

    frame = hud_bezel.copy()
    vp_size = layout["vp_size"]
    vp_x = layout["vp_x"]
    vp_y = layout["vp_y"]

    gw, gh = src.size
    if gw == gh:
        tw, th = vp_size, vp_size
    elif gw > gh:
        tw = vp_size
        th = max(1, int(round(vp_size * (gh / gw))))
    else:
        th = vp_size
        tw = max(1, int(round(vp_size * (gw / gh))))

    scaled = src if (gw, gh) == (tw, th) else src.resize((tw, th), _NEAREST)
    px = vp_x + (vp_size - tw) // 2
    py = vp_y + (vp_size - th) // 2
    frame.paste(scaled, (px, py))
    return frame


def present_game_frame(
    lcd: Any,
    game_img: Image.Image,
    title: str = "ARCADE",
    score: Any = None,
    best: Any = None,
    controls: Optional[Iterable[str]] = None,
    status: str = "",
) -> None:
    """Compose *game_img* with the Obsidian Ember arcade bezel and push to *lcd*."""
    w = getattr(lcd, "width", game_img.width)
    h = getattr(lcd, "height", game_img.height)
    frame = compose_arcade_frame(
        w,
        h,
        game_img,
        title=title,
        score=score,
        best=best,
        controls=controls,
        status=status,
    )
    lcd.LCD_ShowImage(frame, 0, 0)
