#!/usr/bin/env python3
"""
RaspyJack Payload -- Standalone GUI App Launcher (No Full Desktop!)
===================================================================
Browse, search, favorite, and launch individual installed Linux GUI apps
(.desktop, Flatpak, Snap, AppImage, or custom binaries) directly from
RaspyJack's Obsidian Ember interface — without ever loading a desktop
environment.

Controls (Clean 1-2 Keys/Action — HAT, CardputerZero & HDMI Keyboard):
  UP / DOWN / PgUp / PgDn   Scroll apps
  LEFT / RIGHT / Tab        Switch category (ALL / FAV / WEB / MEDIA / GAMES / TOOLS)
  ENTER / OK                Launch selected GUI app standalone
  /  or  KEY1               Search / filter apps by name
  O  or  KEY2               Options (Scale, Backend, Game/Mouse Controls, Star, View, Rescan)
  F                         Quick Star / Unstar Favorite
  -  /  =                   Quick Window Scale Down / Up
  Esc / KEY3                Clear search / Back / Exit
"""

import os
import shlex
import signal
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

from PIL import Image, ImageDraw

import rj_app_launcher as launcher
import rj_env
import rj_theme
from payloads._input_helper import format_footer_hint


def _load_fonts(width: int, height: int) -> dict:
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
    }


def _make_monogram_tile(name: str, size: int, active: bool = False) -> Image.Image:
    ET = rj_theme.EMBER_THEME
    tile = Image.new("RGB", (size, size), ET["select_bg"] if active else ET["bg_well"])
    d = ImageDraw.Draw(tile)
    d.rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=max(3, size // 5),
        fill=ET["select_bg"] if active else ET["bg_well"],
        outline=ET["accent"] if active else ET["border_subtle"],
        width=1,
    )
    words = [w for w in (name or "?").split() if w]
    letters = (words[0][0] + words[1][0]).upper() if len(words) >= 2 else (name[:2] or "?").upper()
    f = rj_env.load_font(max(9, int(size * 0.42)), bold=True)
    d.text((size // 2, size // 2), letters, font=f, fill=ET["accent_bright"] if active else ET["accent"], anchor="mm")
    return tile


class _IconCache:
    def __init__(self, user_home: str = ""):
        self._user_home = user_home
        self._cache: Dict[Tuple[str, int], Optional[Image.Image]] = {}

    def get(self, app: dict, size: int, active: bool = False) -> Image.Image:
        key = (app.get("id", ""), size)
        if key not in self._cache:
            path = launcher.resolve_app_icon_path(app.get("icon", ""), user_home=self._user_home)
            img = None
            if path:
                try:
                    with Image.open(path) as raw:
                        rgba = raw.convert("RGBA").resize((size - 4, size - 4), Image.BILINEAR)
                        bg = Image.new("RGBA", (size, size), rj_theme.EMBER_RGB["bg_well"] + (255,))
                        bg.alpha_composite(rgba, (2, 2))
                        img = bg.convert("RGB")
                except Exception:
                    img = None
            self._cache[key] = img
        cached = self._cache[key]
        if cached is not None:
            return cached
        return _make_monogram_tile(app.get("name", "?"), size, active=active)


def render_app_launcher_frame(
    width: int,
    height: int,
    apps: List[dict],
    category: str = "all",
    query: str = "",
    cursor: int = 0,
    scroll: int = 0,
    view_mode: str = "list",
    session_label: str = "KIOSK",
    toast: str = "",
    toast_kind: str = "info",
    overlay: Optional[dict] = None,
    icon_cache: Optional[_IconCache] = None,
    fonts: Optional[dict] = None,
    app_scale: float = 1.0,
    app_scales: Optional[Dict[str, float]] = None,
    app_backend: str = "auto",
    app_backends: Optional[Dict[str, str]] = None,
) -> Image.Image:
    """Pure PIL renderer for the Obsidian Ember Standalone GUI App Launcher."""
    ET = rj_theme.EMBER_THEME
    f = fonts or _load_fonts(width, height)
    wide = f["wide"]
    large = f["large"]
    icons = icon_cache or _IconCache()

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    hdr_h = 30 if large else (24 if wide else 18)
    tab_h = 24 if large else (18 if wide else 14)
    ftr_h = 22 if large else (18 if wide else 13)

    # 1. Header bar
    d.rectangle((0, 0, width - 1, hdr_h - 1), fill=ET["bg_elevated"])
    d.line([(0, hdr_h - 1), (width - 1, hdr_h - 1)], fill=ET["border_subtle"], width=1)
    hx = 8 if wide else 3
    title_txt = "GUI APPS" if wide else "APPS"
    tb = d.textbbox((0, 0), title_txt, font=f["title"])
    d.text((hx, (hdr_h - (tb[3] - tb[1])) // 2 - tb[1]), title_txt, font=f["title"], fill=ET["accent_bright"])
    hx += (tb[2] - tb[0]) + (8 if wide else 4)

    filtered = launcher.filter_apps(apps, category=category, query=query)
    cnt_lbl = str(len(filtered))
    cw, _ = rj_theme.draw_pill(
        d, hx, max(2, (hdr_h - (16 if wide else 11)) // 2),
        cnt_lbl, f["badge"], bg=ET["select_bg"], fg=ET["accent_bright"],
        outline=ET["border_accent"], pad_x=5 if wide else 3, pad_y=1, radius=3,
    )
    hx += cw + 8

    if query and wide:
        q_lbl = rj_theme.truncate_text(d, f'/{query}', max(60, width // 3), f["sub"])
        qw, _ = rj_theme.draw_pill(
            d, hx, max(2, (hdr_h - 16) // 2), q_lbl, f["badge"],
            bg=ET["bg_well"], fg=ET["text_bright"], outline=ET["accent"], pad_x=5, pad_y=1, radius=3,
        )
        hx += qw + 6

    if wide:
        rx_hdr = width - 6
        mode_w = d.textbbox((0, 0), session_label, font=f["badge"])[2] + 10
        rj_theme.draw_pill(
            d, rx_hdr - mode_w, max(2, (hdr_h - 16) // 2),
            session_label, f["badge"],
            bg=ET["bg_surface"],
            fg=ET["warn"] if "NEEDS" in session_label else ET["ok"],
            outline=ET["border_subtle"], pad_x=5, pad_y=1, radius=3,
        )
        rx_hdr -= mode_w + 6
        sc_norm = launcher.normalize_app_scale(app_scale)
        sc_lbl = f"SCALE {launcher.format_app_scale(sc_norm)}" if large else launcher.format_app_scale(sc_norm)
        sc_w = d.textbbox((0, 0), sc_lbl, font=f["badge"])[2] + 10
        if rx_hdr - sc_w > hx + 4:
            sc_custom = abs(sc_norm - 1.0) >= 0.01
            rj_theme.draw_pill(
                d, rx_hdr - sc_w, max(2, (hdr_h - 16) // 2),
                sc_lbl, f["badge"],
                bg=ET["select_bg"] if sc_custom else ET["bg_surface"],
                fg=ET["accent_bright"] if sc_custom else ET["text_muted"],
                outline=ET["border_accent"] if sc_custom else ET["border_subtle"],
                pad_x=5, pad_y=1, radius=3,
            )

    # 2. Category tabs bar
    d.rectangle((0, hdr_h, width - 1, hdr_h + tab_h - 1), fill=ET["bg_surface"])
    d.line([(0, hdr_h + tab_h - 1), (width - 1, hdr_h + tab_h - 1)], fill=ET["border_subtle"], width=1)
    tx = 6 if wide else 2
    for cat in launcher.CATEGORY_TABS:
        lbl = launcher.CATEGORY_LABELS[cat]
        active = cat == category
        tw, _ = rj_theme.draw_pill(
            d, tx, hdr_h + max(2, (tab_h - (16 if wide else 11)) // 2),
            lbl, f["badge"],
            bg=ET["select_bg"] if active else ET["bg_well"],
            fg=ET["accent_bright"] if active else ET["text_muted"],
            outline=ET["accent"] if active else ET["border_subtle"],
            pad_x=6 if large else (4 if wide else 2), pad_y=1, radius=4,
        )
        tx += tw + (6 if large else (4 if wide else 2))
        if tx > width - 24:
            break

    # 3. Viewport
    y0 = hdr_h + tab_h + (5 if wide else 2)
    y1 = height - ftr_h - (22 if (toast and wide) else (4 if wide else 2))

    if not filtered:
        cy = (y0 + y1) // 2
        m1 = f'No apps match "{query}"' if query else "No GUI apps in this category"
        m2 = "Press O -> Add Custom App / AppImage, or R to Rescan" if wide else "O:Add  R:Scan"
        d.text((width // 2, cy - (10 if wide else 6)), rj_theme.truncate_text(d, m1, width - 16, f["body_bold"]), font=f["body_bold"], fill=ET["text_primary"], anchor="mm")
        d.text((width // 2, cy + (10 if wide else 6)), rj_theme.truncate_text(d, m2, width - 16, f["sub"]), font=f["sub"], fill=ET["text_muted"], anchor="mm")
    elif view_mode == "grid" and wide:
        cols = 3 if large else 2
        gap_x, gap_y = (8, 8) if large else (5, 5)
        pad_x = 8 if large else 4
        sb_w = 8
        grid_w = width - pad_x * 2 - sb_w
        card_w = (grid_w - gap_x * (cols - 1)) // cols
        card_h = 74 if large else 46
        vis_rows = max(1, (y1 - y0 + gap_y) // (card_h + gap_y))
        cur_row = cursor // cols
        total_rows = (len(filtered) + cols - 1) // cols
        start_row = max(0, min(scroll, max(0, total_rows - vis_rows)))
        if cur_row < start_row:
            start_row = cur_row
        elif cur_row >= start_row + vis_rows:
            start_row = cur_row - vis_rows + 1

        for r_off in range(vis_rows):
            row_i = start_row + r_off
            for c_i in range(cols):
                idx = row_i * cols + c_i
                if idx >= len(filtered):
                    break
                app = filtered[idx]
                sel = idx == cursor
                x0 = pad_x + c_i * (card_w + gap_x)
                ry0 = y0 + r_off * (card_h + gap_y)
                rj_theme.draw_card(
                    d, (x0, ry0, x0 + card_w, ry0 + card_h),
                    fill=ET["select_bg"] if sel else ET["bg_surface"],
                    outline=ET["border_accent"] if sel else ET["border_subtle"],
                    radius=7 if large else 4,
                    accent_left=ET["accent"] if sel else None,
                    accent_width=3 if large else 2,
                )
                ico_s = 30 if large else 20
                tile = icons.get(app, ico_s, active=sel)
                img.paste(tile, (x0 + 8, ry0 + (card_h - ico_s) // 2))
                tx_c = x0 + 12 + ico_s
                tw_c = max(30, card_w - ico_s - 18)
                nm = ("* " if app.get("favorite") else "") + app.get("name", "?")
                d.text((tx_c, ry0 + (12 if large else 6)), rj_theme.truncate_text(d, nm, tw_c, f["body_bold"]), font=f["body_bold"], fill=ET["text_bright"] if sel else ET["text_primary"])
                if large:
                    sub = app.get("comment") or app.get("source", "APP")
                    d.text((tx_c, ry0 + 32), rj_theme.truncate_text(d, sub, tw_c, f["sub"]), font=f["sub"], fill=ET["text_muted"])
                    rj_theme.draw_pill(
                        d, tx_c, ry0 + card_h - 20, app.get("source", "SYS"), f["badge"],
                        bg=ET["bg_well"], fg=ET["accent_bright"] if sel else ET["text_muted"],
                        outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3,
                    )
        if total_rows > vis_rows:
            rj_theme.draw_scrollbar(d, width - sb_w, y0, y1, start_row, vis_rows, total_rows, width=4)
    else:
        # Split list + detail preview pane on large screens (>= 480px)
        split = width >= 480
        list_x0 = 8 if wide else 2
        list_x1 = int(width * 0.60) - 4 if split else (width - (10 if wide else 5))
        row_h = 38 if large else (26 if wide else 18)
        vis = max(1, (y1 - y0) // row_h)
        st = max(0, min(scroll, max(0, len(filtered) - vis)))

        for slot in range(vis):
            idx = st + slot
            if idx >= len(filtered):
                break
            app = filtered[idx]
            sel = idx == cursor
            ry = y0 + slot * row_h
            ry1 = ry + row_h - (4 if wide else 2)
            rj_theme.draw_card(
                d, (list_x0, ry, list_x1, ry1),
                fill=ET["select_bg"] if sel else ET["bg_surface"],
                outline=ET["border_accent"] if sel else ET["border_subtle"],
                radius=6 if wide else 3,
                accent_left=ET["accent"] if sel else None,
                accent_width=3 if wide else 2,
            )
            nx = list_x0 + (8 if wide else 4)
            if wide:
                ico_s = 24 if large else 18
                tile = icons.get(app, ico_s, active=sel)
                img.paste(tile, (nx, ry + (ry1 - ry - ico_s) // 2))
                nx += ico_s + 8

            src = app.get("source", "SYS")
            rx = list_x1 - (6 if wide else 3)
            if wide:
                sw = d.textbbox((0, 0), src, font=f["badge"])[2] + 8
                rj_theme.draw_pill(
                    d, rx - sw, ry + max(3, (ry1 - ry - 15) // 2), src, f["badge"],
                    bg=ET["bg_well"], fg=ET["accent_bright"] if sel else ET["text_muted"],
                    outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3,
                )
                rx -= sw + 6

            nm = ("* " if app.get("favorite") else "") + app.get("name", "?")
            d.text((nx, ry + (4 if large else 2)), rj_theme.truncate_text(d, nm, max(36, rx - nx), f["body_bold"] if wide else f["body"]), font=f["body_bold"] if wide else f["body"], fill=ET["text_bright"] if sel else ET["text_primary"])
            if large:
                cm = app.get("comment") or app.get("category", "").upper()
                d.text((nx, ry + 20), rj_theme.truncate_text(d, cm, max(36, rx - nx), f["sub"]), font=f["sub"], fill=ET["text_muted"])

        if len(filtered) > vis:
            rj_theme.draw_scrollbar(d, list_x1 + 2, y0, y1, st, vis, len(filtered), width=4 if wide else 2)

        if split and 0 <= cursor < len(filtered):
            app = filtered[cursor]
            px0 = list_x1 + 10
            px1 = width - 8
            rj_theme.draw_card(d, (px0, y0, px1, y1), fill=ET["bg_surface"], outline=ET["border_subtle"], radius=7)
            pw = px1 - px0 - 18
            ico_big = icons.get(app, 48, active=True)
            img.paste(ico_big, (px0 + 10, y0 + 10))
            d.text((px0 + 66, y0 + 14), rj_theme.truncate_text(d, app.get("name", "?"), pw - 58, f["title"]), font=f["title"], fill=ET["accent_bright"])
            overrides_map = app_scales if isinstance(app_scales, dict) else {}
            b_overrides = app_backends if isinstance(app_backends, dict) else {}
            aid_cur = str(app.get("id", ""))
            has_ovr = aid_cur in overrides_map
            eff_sc = launcher.normalize_app_scale(overrides_map[aid_cur] if has_ovr else app_scale)
            sc_tag = f"{launcher.format_app_scale(eff_sc)}{'*' if has_ovr else ''}"
            has_b_ovr = aid_cur in b_overrides
            eff_b = b_overrides[aid_cur] if has_b_ovr else launcher.normalize_app_backend(app_backend)
            b_tag = ""
            if has_b_ovr:
                b_tag = f"  •  {launcher.format_app_backend(eff_b)}*"
            elif eff_b != "auto":
                b_tag = f"  •  {launcher.format_app_backend(eff_b)}"
            elif launcher.is_pointer_lock_game(app):
                b_tag = "  •  3D-GAME"
            cat_badge = f"{app.get('category', 'tools').upper()}  •  {app.get('source', 'SYS')}  •  {sc_tag}{b_tag}"
            d.text((px0 + 66, y0 + 36), rj_theme.truncate_text(d, cat_badge, pw - 58, f["sub"]), font=f["sub"], fill=ET["accent_bright"] if (has_ovr or has_b_ovr) else ET["text_muted"])
            d.line([(px0 + 10, y0 + 66), (px1 - 10, y0 + 66)], fill=ET["border_subtle"], width=1)

            desc = app.get("comment") or "Standalone Linux GUI application"
            words = desc.split()
            lines: List[str] = []
            cur_l = ""
            for w in words:
                cand = (cur_l + " " + w).strip()
                if d.textbbox((0, 0), cand, font=f["body"])[2] <= pw:
                    cur_l = cand
                else:
                    if cur_l:
                        lines.append(cur_l)
                    cur_l = w
                    if len(lines) >= 3:
                        break
            if cur_l and len(lines) < 3:
                lines.append(cur_l)
            dy = y0 + 74
            for ln in lines:
                d.text((px0 + 10, dy), rj_theme.truncate_text(d, ln, pw, f["body"]), font=f["body"], fill=ET["text_primary"])
                dy += 17

            # Command preview box
            cmd_y = y1 - 64
            if cmd_y > dy + 4:
                d.rounded_rectangle((px0 + 10, cmd_y, px1 - 10, cmd_y + 22), radius=4, fill=ET["bg_well"], outline=ET["border_subtle"])
                cmd_str = " ".join(app.get("argv", [])) or app.get("exec", "")
                d.text((px0 + 15, cmd_y + 5), rj_theme.truncate_text(d, "$ " + cmd_str, pw - 10, f["badge"]), font=f["badge"], fill=ET["text_muted"])

            d.text((px0 + 10, y1 - 38), rj_theme.truncate_text(d, "Enter: Launch   F: Star   O: Options", pw, f["sub"]), font=f["sub"], fill=ET["accent"])
            d.text((px0 + 10, y1 - 21), rj_theme.truncate_text(d, "ScrLk/^Alt+M:Mouse  +G:Game  +Q:Exit", pw, f["sub"]), font=f["sub"], fill=ET["text_muted"])

    # 4. Toast banner
    if toast and wide:
        ty0 = height - ftr_h - 20
        t_col = ET["ok"] if toast_kind == "ok" else (ET["danger"] if toast_kind == "err" else ET["accent_bright"])
        d.rectangle((0, ty0, width - 1, height - ftr_h - 1), fill=ET["bg_overlay"])
        d.line([(0, ty0), (width - 1, ty0)], fill=ET["border_subtle"], width=1)
        d.text((8, ty0 + 3), rj_theme.truncate_text(d, toast, width - 16, f["sub"]), font=f["sub"], fill=t_col)

    # 5. Footer (device-aware across 128, 240, 320x170 CardputerZero, and 640x360 HDMI)
    d.rectangle((0, height - ftr_h, width - 1, height - 1), fill=ET["bg_elevated"])
    d.line([(0, height - ftr_h), (width - 1, height - ftr_h)], fill=ET["border_subtle"], width=1)
    hint = format_footer_hint(
        width,
        height,
        hat_hint="OK:Run K1:Find K2:Opt K3:X",
        hat_240_hint="OK:Launch  K1:Find  K2:Opts  K3:Exit",
        cardputer_hint="Ent:Run  /:Find  O:Opts  -/=:Scale  Esc:Exit",
        hdmi_hint="Arrows:Nav  Enter:Launch  /:Search  O:Options  F:Star  -/=:Scale  Esc:Exit",
    )
    hb = d.textbbox((0, 0), hint, font=f["sub"])
    d.text((6 if wide else 3, height - ftr_h + (ftr_h - (hb[3] - hb[1])) // 2 - hb[1]), rj_theme.truncate_text(d, hint, width - 10, f["sub"]), font=f["sub"], fill=ET["text_muted"])

    # 6. Modal overlay (Running App / Options Menu / Message — height-clamped for 320x170!)
    if overlay:
        kind = overlay.get("kind", "")
        if kind == "running":
            bw = min(width - 16, max(116, int(width * 0.78)))
            bh = min(height - 14, 128 if large else (104 if wide else 84))
            bx0 = (width - bw) // 2
            by0 = (height - bh) // 2
            rj_theme.draw_card(
                d, (bx0, by0, bx0 + bw, by0 + bh),
                fill=ET["bg_elevated"], outline=ET["accent"], radius=8 if wide else 4,
                accent_left=ET["ok"], accent_width=4 if wide else 2,
            )
            d.text((bx0 + 12, by0 + (12 if large else 8)), rj_theme.truncate_text(d, overlay.get("title", "App Running"), bw - 22, f["title"]), font=f["title"], fill=ET["accent_bright"])
            d.text((bx0 + 12, by0 + (36 if large else (28 if wide else 24))), rj_theme.truncate_text(d, overlay.get("sub", ""), bw - 22, f["body"]), font=f["body"], fill=ET["text_primary"])
            d.text((bx0 + 12, by0 + (60 if large else (48 if wide else 42))), rj_theme.truncate_text(d, overlay.get("meta", ""), bw - 22, f["sub"]), font=f["sub"], fill=ET["text_muted"])
            d.text((bx0 + 12, by0 + bh - (24 if large else (18 if wide else 16))), rj_theme.truncate_text(d, overlay.get("foot", "K/K1: Close  X/K2: Kill  Esc/K3: Back"), bw - 22, f["sub"]), font=f["sub"], fill=ET["accent"])
        elif kind in ("error", "info"):
            is_err = (kind == "error")
            accent_c = ET["danger"] if is_err else ET["accent"]
            title_c = ET["danger"] if is_err else ET["accent_bright"]
            bw = min(width - 16, max(120, int(width * 0.84)))
            bh = min(height - 12, 152 if large else (132 if wide else 96))
            bx0 = (width - bw) // 2
            by0 = (height - bh) // 2
            rj_theme.draw_card(
                d, (bx0, by0, bx0 + bw, by0 + bh),
                fill=ET["bg_elevated"], outline=accent_c, radius=8 if wide else 4,
                accent_left=accent_c, accent_width=4 if wide else 2,
            )
            d.text((bx0 + 12, by0 + (12 if large else 8)), rj_theme.truncate_text(d, overlay.get("title", "Launch Failed"), bw - 22, f["title"]), font=f["title"], fill=title_c)
            body_lines = overlay.get("lines") or [overlay.get("sub", "")]
            ly = by0 + (36 if large else (26 if wide else 24))
            line_step = 18 if large else (14 if wide else 12)
            for bl in body_lines[:4]:
                if ly + line_step > by0 + bh - (36 if large else 28):
                    break
                d.text((bx0 + 12, ly), rj_theme.truncate_text(d, str(bl), bw - 22, f["body"] if large else f["sub"]), font=f["body"] if large else f["sub"], fill=ET["text_primary"])
                ly += line_step
            if overlay.get("meta"):
                d.text((bx0 + 12, by0 + bh - (40 if large else (30 if wide else 26))), rj_theme.truncate_text(d, overlay["meta"], bw - 22, f["sub"]), font=f["sub"], fill=ET["text_muted"])
            d.text((bx0 + 12, by0 + bh - (22 if large else (16 if wide else 14))), rj_theme.truncate_text(d, overlay.get("foot", "Enter or Esc to dismiss"), bw - 22, f["sub"]), font=f["sub"], fill=ET["accent_bright"])
        elif kind == "menu":
            items = overlay.get("items", [])
            msel = int(overlay.get("sel", 0))
            bw = min(width - 16, max(118, int(width * 0.62)))
            row_h = 26 if large else (20 if wide else 15)
            hdr_part = 36 if wide else 20
            vis = min(len(items), max(3, (height - hdr_part - 36) // row_h))
            bh = hdr_part + vis * row_h + 10
            bx0 = (width - bw) // 2
            by0 = (height - bh) // 2
            rj_theme.draw_card(
                d, (bx0, by0, bx0 + bw, by0 + bh),
                fill=ET["bg_elevated"], outline=ET["border_accent"], radius=8 if wide else 4,
                accent_left=ET["accent"], accent_width=3,
            )
            d.text((bx0 + 10, by0 + 8), rj_theme.truncate_text(d, overlay.get("title", "Options"), bw - 20, f["title"]), font=f["title"], fill=ET["accent_bright"])
            d.line([(bx0 + 6, by0 + hdr_part - 4), (bx0 + bw - 6, by0 + hdr_part - 4)], fill=ET["border_subtle"], width=1)
            mst = max(0, min(msel - vis + 1, max(0, len(items) - vis)))
            for i in range(vis):
                idx = mst + i
                if idx >= len(items):
                    break
                label = items[idx][0] if isinstance(items[idx], tuple) else str(items[idx])
                ry = by0 + hdr_part + i * row_h
                is_s = idx == msel
                if is_s:
                    rj_theme.draw_card(d, (bx0 + 6, ry, bx0 + bw - 6, ry + row_h - 3), fill=ET["select_bg"], radius=4, accent_left=ET["accent"], accent_width=2)
                d.text((bx0 + 14, ry + (4 if wide else 2)), rj_theme.truncate_text(d, label, bw - 24, f["body"]), font=f["body"], fill=ET["accent_bright"] if is_s else ET["text_primary"])

    return img


def main() -> int:
    import RPi.GPIO as GPIO
    import LCD_1in44
    import hdmi_shm
    from payloads._input_helper import (
        poll_ui_input, flush_input, text_input_mode,
        is_confirm, is_back, is_dismiss, is_action, is_step_down, is_step_up,
    )
    from payloads._keyboard_helper import lcd_keyboard

    PINS = {
        "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
        "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
    }
    GPIO.setmode(GPIO.BCM)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    W, H = lcd.width, lcd.height
    fonts = _load_fonts(W, H)

    running = True

    def _on_sig(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _on_sig)
    signal.signal(signal.SIGTERM, _on_sig)

    st = launcher.load_launcher_state()
    view_mode = st.get("view_mode", "list")
    app_scale = float(st.get("app_scale", 1.0))
    app_scales = dict(st.get("app_scales") or {})
    app_backend = str(st.get("app_backend", "auto"))
    app_backends = dict(st.get("app_backends") or {})
    session_info = launcher.detect_display_session()
    icon_cache = _IconCache(user_home=session_info.get("home", ""))
    apps = launcher.discover_gui_apps()
    cat_idx = 0
    query = ""
    cursor = 0
    scroll = 0
    toast = ""
    toast_kind = "info"
    toast_until = 0.0

    def _set_toast(msg: str, kind: str = "info", secs: float = 3.5) -> None:
        nonlocal toast, toast_kind, toast_until
        toast = msg
        toast_kind = kind
        toast_until = time.monotonic() + secs

    def _render(overlay: Optional[dict] = None) -> None:
        cur_toast = toast if time.monotonic() < toast_until else ""
        frame = render_app_launcher_frame(
            W, H, apps=apps, category=launcher.CATEGORY_TABS[cat_idx],
            query=query, cursor=cursor, scroll=scroll, view_mode=view_mode,
            session_label=session_info["mode_label"], toast=cur_toast, toast_kind=toast_kind,
            overlay=overlay, icon_cache=icon_cache, fonts=fonts,
            app_scale=app_scale, app_scales=app_scales,
            app_backend=app_backend, app_backends=app_backends,
        )
        lcd.LCD_ShowImage(frame, 0, 0)

    def _run_modal_menu(title: str, items: List[Tuple[str, object]]):
        sel = 0
        dirty = True
        flush_input()
        while running:
            if dirty:
                _render({"kind": "menu", "title": title, "items": items, "sel": sel})
                dirty = False
            act, ch = poll_ui_input(PINS, GPIO)
            if act == "UP":
                sel = (sel - 1) % len(items)
                dirty = True
            elif act == "DOWN":
                sel = (sel + 1) % len(items)
                dirty = True
            elif is_confirm(act):
                flush_input()
                return items[sel][1]
            elif is_back(act):
                flush_input()
                return None
            time.sleep(0.04)
        return None

    def _show_error_dialog(title: str, detail: str, meta: str = "", kind: str = "error") -> None:
        raw_lines = [ln.strip() for ln in (detail or "Unknown error").splitlines() if ln.strip()]
        wrapped: List[str] = []
        max_ch = 58 if fonts["large"] else (42 if fonts["wide"] else 20)
        for rl in raw_lines[-4:]:
            while len(rl) > max_ch and len(wrapped) < 4:
                wrapped.append(rl[:max_ch])
                rl = rl[max_ch:]
            if rl and len(wrapped) < 4:
                wrapped.append(rl)
        _render({
            "kind": kind,
            "title": title,
            "lines": wrapped or ["Launch failed"],
            "meta": meta or f"Log: {launcher.APP_LAUNCHER_LOG}",
            "foot": "Enter or Esc to dismiss",
        })
        flush_input()
        while running:
            act, _ch = poll_ui_input(PINS, GPIO)
            if is_dismiss(act):
                break
            time.sleep(0.05)
        flush_input()

    def _launch_selected(app: dict, extra_args: Optional[List[str]] = None) -> None:
        nonlocal apps, session_info
        session_info = launcher.detect_display_session()
        cmd, _env, needs_handoff, desc = launcher.build_launch_plan(app, extra_args=extra_args, session_info=session_info)
        if not cmd:
            _show_error_dialog(
                "Kiosk Compositor Needed",
                desc,
                meta="Run that once over SSH, then retry.",
            )
            return

        active_kiosk = os.path.basename(cmd[0]) if cmd else str(session_info.get("kiosk_tool") or "")
        launcher.reset_launcher_debug_log(
            f"=== Launch {app['name']} ({app['id']}) mode={session_info['mode']} backend={active_kiosk} "
            f"user={session_info['user']}({session_info['uid']}) "
            f"seatd={session_info.get('has_seatd')} xwayland={session_info.get('has_xwayland')} ==="
        )

        cur_st_launch = launcher.load_launcher_state()
        pref_mode = launcher.resolve_preferred_mouse_mode(app, state=cur_st_launch)
        g_layout = launcher.normalize_game_layout(cur_st_launch.get("game_layout", "arrows"))
        g_hint = "Arrows+Ent/RShf" if g_layout == "arrows" else "IJKL+U/O"
        _render({
            "kind": "running",
            "title": f"Launching {app['name']}...",
            "sub": desc,
            "meta": f"Starts in Typing  •  ScrLk / Ctrl+Alt+M: {pref_mode.upper()}  •  Ctrl+Alt+G: Game",
            "foot": f"Game: WASD + {g_hint}  •  Ctrl+Alt+Q: Force Exit",
        })

        # Headless TTY single-app kiosk handoff:
        # 1) Pause evdev_keys FIRST so raspyjack.py cannot grab /dev/input the instant hdmi_host stops!
        # 2) Switch away from VT1 (getty login prompt) onto dedicated VT7!
        # 3) Stop hdmi_host cleanly so DRM/fb0 and keyboard are 100% free for cage/xinit!
        evdev_paused = False
        host_was_running = False
        kiosk_vt = int(session_info.get("kiosk_vt", launcher.DEFAULT_KIOSK_VT))
        kbd_mouse = None
        active_proc_ref = [None]
        force_exited = [False]

        def _force_exit_kiosk() -> None:
            force_exited[0] = True
            p_cur = active_proc_ref[0]
            if p_cur is not None and p_cur.poll() is None:
                try:
                    os.killpg(os.getpgid(p_cur.pid), signal.SIGTERM)
                except Exception:
                    try:
                        p_cur.terminate()
                    except Exception:
                        pass
            # Also gently terminate cage/labwc/weston/gamescope/xinit if systemd-run spawned it in its own unit scope
            k_tool = active_kiosk or str(session_info.get("kiosk_tool") or "")
            if k_tool in ("cage", "labwc", "gamescope", "weston", "xinit"):
                try:
                    import subprocess as _sp
                    _sp.run(["pkill", "-TERM", "-x", k_tool], capture_output=True, timeout=2)
                    if k_tool == "xinit":
                        _sp.run(["pkill", "-TERM", "-x", "Xorg"], capture_output=True, timeout=2)
                except Exception:
                    pass

        def _read_proc_err(p) -> str:
            parts: List[str] = []
            try:
                if p.stderr:
                    txt = p.stderr.read(1600).decode("utf-8", "ignore").strip()
                    if txt:
                        parts.append(txt)
            except Exception:
                pass
            err_log = getattr(p, "_rj_err_log", "")
            if err_log and os.path.isfile(err_log):
                try:
                    with open(err_log, "r", encoding="utf-8", errors="ignore") as fh:
                        txt2 = fh.read(2048).strip()
                    if txt2:
                        parts.append(txt2)
                except OSError:
                    pass
                try:
                    os.unlink(err_log)
                except OSError:
                    pass
            out = "\n".join(parts).strip()
            if out:
                launcher.write_launcher_debug_log(out)
            return out

        if needs_handoff:
            try:
                with open(launcher.KIOSK_HANDOFF_FLAG, "w", encoding="utf-8") as fh:
                    fh.write(str(kiosk_vt))
            except OSError:
                pass
            try:
                import evdev_keys
                evdev_keys.pause()
                evdev_paused = True
            except Exception:
                pass
            time.sleep(0.05)
            launcher.switch_vt(kiosk_vt, wait=True)
            launcher.clear_vt_screen(kiosk_vt)
            try:
                host_was_running, _ = hdmi_shm.is_host_running()
                if host_was_running:
                    hdmi_shm.stop_host()
                time.sleep(0.25)
            except Exception:
                pass
            launcher.clear_vt_screen(kiosk_vt)

        # Start dual-UInput Keyboard-Mouse & universal Ctrl+Alt+Q exit controller BEFORE
        # spawning the compositor/app (works in both headless kiosk AND direct Wayland/X11
        # sessions whenever /dev/uinput is accessible!).
        # Apps ALWAYS start in Typing Mode (never auto-grabbed on launch).
        kbd_mouse_status = ""
        try:
            import rj_kbd_mouse
            cur_mouse_speed = int(cur_st_launch.get("mouse_speed", 4))
            cur_game_sens = int(cur_st_launch.get("game_sensitivity", 5))

            def _save_live_speed(new_lv: int) -> None:
                s_cur = launcher.load_launcher_state()
                s_cur["mouse_speed"] = max(1, min(10, int(new_lv)))
                launcher.save_launcher_state(s_cur)

            def _save_live_sens(new_lv: int) -> None:
                s_cur = launcher.load_launcher_state()
                s_cur["game_sensitivity"] = max(1, min(10, int(new_lv)))
                launcher.save_launcher_state(s_cur)

            kbd_mouse = rj_kbd_mouse.KeyboardMouseController(
                on_force_exit=_force_exit_kiosk,
                mouse_speed=cur_mouse_speed,
                game_sensitivity=cur_game_sens,
                preferred_mode=pref_mode,
                initial_mode=rj_kbd_mouse.MODE_TYPING,
                game_layout=g_layout,
                on_speed_change=_save_live_speed,
                on_sensitivity_change=_save_live_sens,
                on_log=launcher.write_launcher_debug_log,
            )
            km_ok = kbd_mouse.start()
            if km_ok:
                if kbd_mouse.passthrough_keyboard_available:
                    kbd_mouse_status = "Controls: Ready (Ctrl+Alt+M/G)"
                else:
                    kbd_mouse_status = "Mouse OK (Game Mode unavailable)"
            else:
                kbd_mouse_status = kbd_mouse.last_error or "Controls need /dev/uinput access"
                if not needs_handoff:
                    kbd_mouse.stop()
                    kbd_mouse = None
        except Exception as _km_exc:
            launcher.write_launcher_debug_log(f"[kbd-mouse] init exception: {_km_exc}")
            kbd_mouse_status = f"Controls error: {str(_km_exc)[:36]}"
            kbd_mouse = None

        # Bare X11 (xinit/Xorg) uses systemd-logind on VT7 rather than seatd, so prefer systemd-run for xinit;
        # for Wayland kiosks (cage/gamescope/weston), prefer direct VT7 when seatd is running.
        prefer_systemd = (active_kiosk == "xinit") or (not bool(session_info.get("has_seatd")))
        proc, _ho, msg = launcher.spawn_standalone_gui_app(
            app,
            extra_args=extra_args,
            session_info=session_info,
            kiosk_vt=kiosk_vt,
            use_systemd_session=prefer_systemd,
        )
        active_proc_ref[0] = proc
        if proc is None:
            if kbd_mouse is not None:
                try:
                    kbd_mouse.stop()
                except Exception:
                    pass
                kbd_mouse = None
            if needs_handoff:
                try:
                    os.unlink(launcher.KIOSK_HANDOFF_FLAG)
                except OSError:
                    pass
            if evdev_paused:
                try:
                    import evdev_keys
                    evdev_keys.resume()
                except Exception:
                    pass
            if host_was_running:
                hdmi_shm.ensure_host_running()
            _show_error_dialog("Launch Failed", msg)
            return

        started = time.monotonic()
        err_txt = ""
        try:
            if needs_handoff:
                # Wait until the standalone kiosk app on VT7 exits, then take back HDMI cleanly
                proc.wait()
                launcher.write_launcher_debug_log(f"[attempt:1] rc={proc.returncode} elapsed={time.monotonic() - started:.2f}s")
                # If Attempt 1 failed quickly (and user didn't force-exit), try alternate VT7 paths
                if not force_exited[0] and proc.returncode != 0 and (time.monotonic() - started) < 4.0 and os.geteuid() == 0:
                    err_txt = _read_proc_err(proc)
                    # Attempt 2: Opposite strategy (direct VT7 if systemd-run failed, or systemd-run if seatd/direct failed)
                    t2 = time.monotonic()
                    retry_u, _ho_u, _msg_u = launcher.spawn_standalone_gui_app(
                        app,
                        extra_args=extra_args,
                        session_info=session_info,
                        as_root=False,
                        kiosk_vt=kiosk_vt,
                        use_systemd_session=(not prefer_systemd),
                    )
                    if retry_u is not None:
                        proc = retry_u
                        active_proc_ref[0] = proc
                        err_txt = ""
                        proc.wait()
                        launcher.write_launcher_debug_log(f"[attempt:2] rc={proc.returncode} elapsed={time.monotonic() - t2:.2f}s")
                    # Attempt 3: If libseat/DRM/VT still rejected non-root, retry directly as root on VT7
                    if not force_exited[0] and proc.returncode != 0 and (time.monotonic() - t2) < 3.0:
                        err_txt = _read_proc_err(proc)
                        low_err = err_txt.lower()
                        if not err_txt or any(k in low_err for k in ("seat", "drm", "permission", "backend", "runtime_dir", "libseat", "tty", "vt", "xorg", "display", "denied")):
                            t3 = time.monotonic()
                            retry_r, _ho_r, _msg_r = launcher.spawn_standalone_gui_app(
                                app,
                                extra_args=extra_args,
                                session_info=session_info,
                                as_root=True,
                                kiosk_vt=kiosk_vt,
                                use_systemd_session=False,
                            )
                            if retry_r is not None:
                                proc = retry_r
                                active_proc_ref[0] = proc
                                err_txt = ""
                                proc.wait()
                                launcher.write_launcher_debug_log(f"[attempt:3] rc={proc.returncode} elapsed={time.monotonic() - t3:.2f}s")
            else:
                # Active display server: monitor app and redraw only when elapsed second ticks
                flush_input()
                last_el = -1
                last_km_sig = None
                while running and proc.poll() is None:
                    el = int(time.monotonic() - started)
                    cur_km_mode = kbd_mouse.state.mode.upper() if kbd_mouse is not None else "OFF"
                    cur_km_sig = (cur_km_mode, kbd_mouse.open_keyboard_count if kbd_mouse is not None else 0)
                    if el != last_el or cur_km_sig != last_km_sig:
                        last_el = el
                        last_km_sig = cur_km_sig
                        km_meta = (
                            f"Mode: {cur_km_mode}  •  Kbd:{kbd_mouse.open_keyboard_count}"
                            if kbd_mouse is not None
                            else kbd_mouse_status
                        )
                        _render({
                            "kind": "running",
                            "title": app["name"],
                            "sub": f"{desc}  •  PID {proc.pid}",
                            "meta": f"{el // 60}:{el % 60:02d}  •  {km_meta}",
                            "foot": "K/K1: Close   X/K2: Force Kill   Esc/K3: Back",
                        })
                    act, ch = poll_ui_input(PINS, GPIO)
                    if is_action(act, ch, "KEY1", "k"):
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                        except Exception:
                            proc.terminate()
                        time.sleep(0.3)
                    elif is_action(act, ch, "KEY2", "x"):
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except Exception:
                            proc.kill()
                        break
                    elif is_back(act):
                        _set_toast(f"{app['name']} running in background (PID {proc.pid})", "info")
                        break
                    time.sleep(0.08)
        finally:
            if kbd_mouse is not None:
                try:
                    kbd_mouse.stop()
                except Exception:
                    pass
                kbd_mouse = None
            if needs_handoff:
                launcher.clear_vt_screen(kiosk_vt)
                try:
                    os.unlink(launcher.KIOSK_HANDOFF_FLAG)
                except OSError:
                    pass
                if evdev_paused:
                    try:
                        import evdev_keys
                        evdev_keys.resume()
                    except Exception:
                        pass
                if host_was_running:
                    hdmi_shm.ensure_host_running()
                lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
            apps = launcher.discover_gui_apps()
            flush_input()

        if proc.poll() is not None:
            rc = proc.returncode
            if not err_txt:
                err_txt = _read_proc_err(proc)
            if force_exited[0] or rc in (-15, 143):
                _set_toast(f"{app['name']} closed", "ok")
            elif rc == 0:
                _set_toast(f"{app['name']} closed cleanly", "ok")
            else:
                hint = ""
                low_e = err_txt.lower()
                if "pointer" in low_e or "locking" in low_e:
                    hint = "3D pointer lock needs Labwc/Gamescope: O -> Backend -> Labwc (Wayland)"
                elif "xwayland" in low_e or "cannot open display" in low_e:
                    hint = "Install XWayland: sudo apt install -y xwayland"
                elif "libseat" in low_e or "seat" in low_e or "unable to create backend" in low_e:
                    hint = "Install seatd: sudo apt install -y seatd && sudo systemctl enable --now seatd"
                _show_error_dialog(
                    f"{app['name']} Failed (exit {rc})",
                    err_txt or f"Exited immediately with code {rc}",
                    meta=hint or f"Full log: {launcher.APP_LAUNCHER_LOG}",
                )

    def _open_options_menu(cur_app: Optional[dict]) -> None:
        nonlocal apps, view_mode, app_scale, app_scales, app_backend, app_backends, session_info
        opts: List[Tuple[str, str]] = []
        if cur_app:
            opts.append((f"Launch {cur_app['name'][:20]}", "launch"))
            opts.append(("Launch with Custom Arguments...", "launch_args"))
            opts.append(("Unstar Favorite" if cur_app.get("favorite") else "Star as Favorite", "fav"))
            if cur_app.get("source") == "CUSTOM":
                opts.append(("Remove Custom App", "rm_custom"))
        cur_st_now = launcher.load_launcher_state()
        cur_spd = int(cur_st_now.get("mouse_speed", 4))
        cur_sens = int(cur_st_now.get("game_sensitivity", 5))
        cur_glay = launcher.normalize_game_layout(cur_st_now.get("game_layout", "arrows"))
        cur_pprof = launcher.normalize_pointer_profile(cur_st_now.get("pointer_profile", "auto"))
        cur_pprofs = dict(cur_st_now.get("pointer_profiles") or {})
        app_scale = float(cur_st_now.get("app_scale", 1.0))
        app_scales = dict(cur_st_now.get("app_scales") or {})
        app_backend = str(cur_st_now.get("app_backend", "auto"))
        app_backends = dict(cur_st_now.get("app_backends") or {})
        session_info = launcher.detect_display_session()
        avail_k = set(session_info.get("available_kiosks") or [])
        try:
            import rj_kbd_mouse as _rkm
            spd_lbl = f"{cur_spd}/10 ({_rkm.get_speed_label(cur_spd)})"
            sens_lbl = f"{cur_sens}/10 ({_rkm.get_game_sensitivity_label(cur_sens)})"
        except Exception:
            spd_lbl = f"{cur_spd}/10"
            sens_lbl = f"{cur_sens}/10"
        opts.append((f"Window Scale (Global): {launcher.format_app_scale(app_scale)}", "global_scale"))
        if cur_app:
            aid_c = str(cur_app.get("id", ""))
            if aid_c in app_scales:
                a_sc_lbl = f"{launcher.format_app_scale(app_scales[aid_c])} (Custom)"
            else:
                a_sc_lbl = f"Auto ({launcher.format_app_scale(app_scale)})"
            opts.append((f"Scale for {cur_app['name'][:14]}: {a_sc_lbl}", "app_scale_ovr"))
        opts.append((f"Launch Backend (Global): {launcher.format_app_backend(app_backend)}", "global_backend"))
        if cur_app:
            aid_c = str(cur_app.get("id", ""))
            if aid_c in app_backends:
                a_bk_lbl = f"{launcher.format_app_backend(app_backends[aid_c])} (Custom)"
            else:
                a_bk_lbl = f"Auto ({launcher.format_app_backend(app_backend)})"
            opts.append((f"Backend for {cur_app['name'][:12]}: {a_bk_lbl}", "app_backend_ovr"))
        opts.append((f"Pointer Toggle Mode: {launcher.format_pointer_profile(cur_pprof)}", "pointer_profile"))
        if cur_app:
            aid_c = str(cur_app.get("id", ""))
            if aid_c in cur_pprofs:
                a_pp_lbl = f"{launcher.format_pointer_profile(cur_pprofs[aid_c])} (Custom)"
            else:
                a_pp_lbl = f"Auto ({launcher.resolve_preferred_mouse_mode(cur_app, cur_st_now).upper()})"
            opts.append((f"Pointer for {cur_app['name'][:12]}: {a_pp_lbl}", "app_pointer_ovr"))
        opts.append((f"Game Look Preset: {launcher.format_game_layout(cur_glay)}", "game_layout"))
        opts.append((f"Desktop Mouse Speed: {spd_lbl}", "mouse_speed"))
        opts.append((f"Game Look Sensitivity: {sens_lbl}", "game_sens"))
        opts.append(("Mouse & Gaming Controls Help", "controls"))
        opts.append(("+ Add Custom App / AppImage", "add_custom"))
        if fonts["wide"]:
            opts.append((f"Switch View ({'Grid' if view_mode == 'list' else 'List'})", "toggle_view"))
        opts.append(("Rescan Installed Apps", "rescan"))
        opts.append(("Close", "close"))

        act = _run_modal_menu(cur_app["name"] if cur_app else "LAUNCHER OPTIONS", opts)
        if not act or act == "close":
            return
        if act == "global_backend":
            bk_opts: List[Tuple[str, object]] = []
            for b_key, b_lbl in launcher.APP_BACKEND_OPTIONS:
                inst_tag = "" if (b_key == "auto" or b_key in avail_k) else " [not installed]"
                mark = "* " if b_key == app_backend else "  "
                bk_opts.append((mark + b_lbl + inst_tag, b_key))
            bk_opts.append(("Back", None))
            picked_bk = _run_modal_menu("LAUNCH BACKEND (ALL APPS)", bk_opts)
            if isinstance(picked_bk, str):
                app_backend = launcher.set_app_backend(picked_bk)
                _set_toast(f"Launch Backend set to {launcher.format_app_backend(app_backend)}", "ok")
            return
        if act == "app_backend_ovr" and cur_app:
            aid_c = str(cur_app.get("id", ""))
            cur_b_ovr = app_backends.get(aid_c)
            bk_opts = [
                (("* " if cur_b_ovr is None else "  ") + f"Auto (Use Global {launcher.format_app_backend(app_backend)})", "auto")
            ]
            for b_key, b_lbl in launcher.APP_BACKEND_OPTIONS:
                if b_key == "auto":
                    continue
                inst_tag = "" if b_key in avail_k else " [not installed]"
                mark = "* " if cur_b_ovr == b_key else "  "
                bk_opts.append((mark + b_lbl + inst_tag, b_key))
            bk_opts.append(("Back", None))
            picked_bk = _run_modal_menu(f"BACKEND: {cur_app['name'][:20]}", bk_opts)
            if picked_bk == "auto":
                launcher.set_app_backend(None, app_id=aid_c)
                app_backends.pop(aid_c, None)
                _set_toast(f"{cur_app['name']}: Backend -> Auto ({launcher.format_app_backend(app_backend)})", "ok")
            elif isinstance(picked_bk, str):
                new_bk = launcher.set_app_backend(picked_bk, app_id=aid_c)
                app_backends[aid_c] = new_bk
                _set_toast(f"{cur_app['name']}: Backend set to {launcher.format_app_backend(new_bk)}", "ok")
            return
        if act == "global_scale":
            sc_opts: List[Tuple[str, object]] = [
                (("* " if abs(val - app_scale) < 0.01 else "  ") + lbl, val)
                for val, lbl in launcher.APP_SCALE_OPTIONS
            ]
            sc_opts.append(("Back", None))
            picked_sc = _run_modal_menu("WINDOW SCALE (ALL APPS)", sc_opts)
            if isinstance(picked_sc, (int, float)):
                app_scale = launcher.set_app_scale(float(picked_sc))
                _set_toast(f"Window Scale set to {launcher.format_app_scale(app_scale)} (quick: - / +)", "ok")
            return
        if act == "app_scale_ovr" and cur_app:
            aid_c = str(cur_app.get("id", ""))
            cur_ovr = app_scales.get(aid_c)
            sc_opts = [
                (("* " if cur_ovr is None else "  ") + f"Auto (Use Global {launcher.format_app_scale(app_scale)})", "auto")
            ]
            for val, lbl in launcher.APP_SCALE_OPTIONS:
                mark = "* " if (cur_ovr is not None and abs(val - cur_ovr) < 0.01) else "  "
                sc_opts.append((mark + lbl, val))
            sc_opts.append(("Back", None))
            picked_sc = _run_modal_menu(f"SCALE: {cur_app['name'][:22]}", sc_opts)
            if picked_sc == "auto":
                launcher.set_app_scale(None, app_id=aid_c)
                app_scales.pop(aid_c, None)
                _set_toast(f"{cur_app['name']}: Scale -> Auto ({launcher.format_app_scale(app_scale)})", "ok")
            elif isinstance(picked_sc, (int, float)):
                new_sc = launcher.set_app_scale(float(picked_sc), app_id=aid_c)
                app_scales[aid_c] = new_sc
                _set_toast(f"{cur_app['name']}: Scale set to {launcher.format_app_scale(new_sc)}", "ok")
            return
        if act == "pointer_profile":
            pp_opts: List[Tuple[str, object]] = [
                (("* " if k == cur_pprof else "  ") + lbl, k)
                for k, lbl in launcher.APP_POINTER_PROFILE_OPTIONS
            ]
            pp_opts.append(("Back", None))
            picked_pp = _run_modal_menu("POINTER TOGGLE TARGET (GLOBAL)", pp_opts)
            if isinstance(picked_pp, str):
                new_pp = launcher.set_pointer_profile(picked_pp)
                _set_toast(f"Pointer toggle target -> {launcher.format_pointer_profile(new_pp)}", "ok")
            return
        if act == "app_pointer_ovr" and cur_app:
            aid_c = str(cur_app.get("id", ""))
            cur_pp_ovr = cur_pprofs.get(aid_c)
            pp_opts = [
                (("* " if cur_pp_ovr is None else "  ") + "Auto (Use Global Setting)", "auto"),
                (("* " if cur_pp_ovr == "desktop" else "  ") + "Desktop Mouse (Cursor + Clicks)", "desktop"),
                (("* " if cur_pp_ovr == "game" else "  ") + "Game Mode (WASD + 3D Look)", "game"),
                ("Back", None),
            ]
            picked_pp = _run_modal_menu(f"POINTER: {cur_app['name'][:20]}", pp_opts)
            if picked_pp == "auto":
                launcher.set_pointer_profile(None, app_id=aid_c)
                _set_toast(f"{cur_app['name']}: Pointer -> Auto", "ok")
            elif isinstance(picked_pp, str):
                new_pp = launcher.set_pointer_profile(picked_pp, app_id=aid_c)
                _set_toast(f"{cur_app['name']}: Pointer -> {launcher.format_pointer_profile(new_pp)}", "ok")
            return
        if act == "game_layout":
            gl_opts: List[Tuple[str, object]] = [
                (("* " if k == cur_glay else "  ") + lbl, k)
                for k, lbl in launcher.GAME_LAYOUT_OPTIONS
            ]
            gl_opts.append(("Back", None))
            picked_gl = _run_modal_menu("GAME MODE LOOK PRESET", gl_opts)
            if isinstance(picked_gl, str):
                new_gl = launcher.set_game_layout(picked_gl)
                _set_toast(f"Game Look Preset -> {launcher.format_game_layout(new_gl)}", "ok")
            return
        if act == "mouse_speed":
            try:
                import rj_kbd_mouse as _rkm
                spd_opts = [
                    (("* " if lv == cur_spd else "  ") + f"{lv}/10  {_rkm.get_speed_label(lv)}" + (" (Default)" if lv == 4 else ""), lv)
                    for lv in range(1, 11)
                ]
            except Exception:
                spd_opts = [(("* " if lv == cur_spd else "  ") + f"Speed {lv}/10", lv) for lv in range(1, 11)]
            spd_opts.append(("Back", None))
            picked_spd = _run_modal_menu("DESKTOP MOUSE SPEED", spd_opts)
            if isinstance(picked_spd, int):
                cur_st = launcher.load_launcher_state()
                cur_st["mouse_speed"] = picked_spd
                launcher.save_launcher_state(cur_st)
                _set_toast(f"Desktop Mouse Speed: {picked_spd}/10 (live: - / =)", "ok")
            return
        if act == "game_sens":
            try:
                import rj_kbd_mouse as _rkm
                s_opts = [
                    (("* " if lv == cur_sens else "  ") + f"{lv}/10  {_rkm.get_game_sensitivity_label(lv)}" + (" (Default)" if lv == 5 else ""), lv)
                    for lv in range(1, 11)
                ]
            except Exception:
                s_opts = [(("* " if lv == cur_sens else "  ") + f"Sensitivity {lv}/10", lv) for lv in range(1, 11)]
            s_opts.append(("Back", None))
            picked_s = _run_modal_menu("GAME LOOK SENSITIVITY", s_opts)
            if isinstance(picked_s, int):
                cur_st = launcher.load_launcher_state()
                cur_st["game_sensitivity"] = picked_s
                launcher.save_launcher_state(cur_st)
                _set_toast(f"Game Look Sensitivity: {picked_s}/10 (live: - / =)", "ok")
            return
        if act == "controls":
            g_str = "Arrows=Look, Ent=L, RShf=R" if cur_glay == "arrows" else "IJKL=Look, U=L, O=R"
            diag_meta = f"Desktop: {spd_lbl}  •  Game: {sens_lbl}"
            try:
                import rj_kbd_mouse as _rkm
                diag = _rkm.diagnose_environment()
                if diag["ready"]:
                    diag_meta = f"uinput: OK  •  Keyboards: {len(diag['keyboard_paths'])}  •  Game: READY"
                elif diag["issues"]:
                    diag_meta = diag["issues"][0]
            except Exception:
                pass
            _show_error_dialog(
                "MOUSE, GAMING & EXIT CONTROLS",
                "ScrLk / ^Alt+M: Pointer  |  ^Alt+G: Game\n"
                "Desk: Arrows, Spc/Ent=L, Bksp=R, Tab=M\n"
                f"Game (WASD live): {g_str}, [/]=Scr\n"
                "Close: Alt+F4 / Ctrl+Q  |  Force: ^Alt+Q",
                meta=diag_meta,
                kind="info",
            )
            return
        if act == "launch" and cur_app:
            _launch_selected(cur_app)
        elif act == "launch_args" and cur_app:
            arg_str = lcd_keyboard(lcd, fonts["body"], PINS, GPIO, title="Extra Launch Args", default="", charset="full", max_len=96)
            if arg_str is not None:
                try:
                    parts = shlex.split(arg_str)
                except ValueError:
                    parts = arg_str.split()
                _launch_selected(cur_app, extra_args=parts)
        elif act == "fav" and cur_app:
            starred = launcher.toggle_favorite(cur_app["id"])
            apps = launcher.discover_gui_apps()
            _set_toast(f"{'Starred' if starred else 'Unstarred'}: {cur_app['name']}", "ok")
        elif act == "rm_custom" and cur_app:
            launcher.remove_custom_app(cur_app["id"])
            apps = launcher.discover_gui_apps()
            _set_toast(f"Removed {cur_app['name']}", "info")
        elif act == "add_custom":
            nm = lcd_keyboard(lcd, fonts["body"], PINS, GPIO, title="App Display Name", default="", charset="full", max_len=40)
            if nm and nm.strip():
                ex = lcd_keyboard(lcd, fonts["body"], PINS, GPIO, title="Command or AppImage Path", default="", charset="full", max_len=120)
                if ex and ex.strip():
                    launcher.add_custom_app(nm.strip(), ex.strip())
                    apps = launcher.discover_gui_apps()
                    _set_toast(f"Added custom app: {nm.strip()}", "ok")
        elif act == "toggle_view":
            view_mode = "grid" if view_mode == "list" else "list"
            cur_st = launcher.load_launcher_state()
            cur_st["view_mode"] = view_mode
            launcher.save_launcher_state(cur_st)
        elif act == "rescan":
            apps = launcher.discover_gui_apps()
            _set_toast(f"Found {len(apps)} GUI app(s)", "ok")
        flush_input()

    try:
        with text_input_mode():
            flush_input()
            dirty = True
            toast_visible = False
            last_active_at = time.monotonic()
            while running:
                now_m = time.monotonic()
                filtered = launcher.filter_apps(apps, category=launcher.CATEGORY_TABS[cat_idx], query=query)
                cols = (3 if fonts["large"] else 2) if (view_mode == "grid" and fonts["wide"]) else 1
                if filtered:
                    cursor = max(0, min(cursor, len(filtered) - 1))
                    if cols == 1:
                        row_h = 38 if fonts["large"] else (26 if fonts["wide"] else 18)
                        hdr_h = 30 if fonts["large"] else (24 if fonts["wide"] else 18)
                        tab_h = 24 if fonts["large"] else (18 if fonts["wide"] else 14)
                        ftr_h = 22 if fonts["large"] else (18 if fonts["wide"] else 13)
                        vis = max(1, (H - hdr_h - tab_h - ftr_h - 10) // row_h)
                        if cursor < scroll:
                            scroll = cursor
                        elif cursor >= scroll + vis:
                            scroll = cursor - vis + 1
                else:
                    cursor = 0
                    scroll = 0

                cur_toast_on = bool(toast and now_m < toast_until)
                if cur_toast_on != toast_visible:
                    toast_visible = cur_toast_on
                    dirty = True

                if dirty:
                    _render()
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO)
                if act is None and ch is None:
                    time.sleep(0.025 if (now_m - last_active_at) < 1.5 else 0.05)
                    continue

                last_active_at = time.monotonic()
                kl = ch.lower() if isinstance(ch, str) else ""

                if is_back(act):
                    if query:
                        query = ""
                        cursor = 0
                        scroll = 0
                        dirty = True
                    else:
                        break
                elif act == "UP":
                    if filtered:
                        cursor = (cursor - cols) % len(filtered)
                        dirty = True
                elif act == "DOWN":
                    if filtered:
                        cursor = (cursor + cols) % len(filtered)
                        dirty = True
                elif act == "PAGEUP" and filtered:
                    cursor = max(0, cursor - (6 if cols == 1 else cols * 2))
                    dirty = True
                elif act == "PAGEDOWN" and filtered:
                    cursor = min(len(filtered) - 1, cursor + (6 if cols == 1 else cols * 2))
                    dirty = True
                elif act == "LEFT":
                    if cols > 1 and filtered and (cursor % cols) > 0:
                        cursor -= 1
                    else:
                        cat_idx = (cat_idx - 1) % len(launcher.CATEGORY_TABS)
                        cursor = 0
                        scroll = 0
                    dirty = True
                elif act == "RIGHT":
                    if cols > 1 and filtered and (cursor % cols) < (cols - 1) and (cursor + 1) < len(filtered):
                        cursor += 1
                    else:
                        cat_idx = (cat_idx + 1) % len(launcher.CATEGORY_TABS)
                        cursor = 0
                        scroll = 0
                    dirty = True
                elif act == "TAB":
                    cat_idx = (cat_idx + 1) % len(launcher.CATEGORY_TABS)
                    cursor = 0
                    scroll = 0
                    dirty = True
                elif is_confirm(act):
                    if filtered:
                        _launch_selected(filtered[cursor])
                        dirty = True
                elif is_action(act, ch, "KEY1", "/"):
                    res = lcd_keyboard(lcd, fonts["body"], PINS, GPIO, title="Search GUI Apps", default=query, charset="full", max_len=40)
                    if res is not None:
                        query = res.strip()
                        cursor = 0
                        scroll = 0
                    flush_input()
                    dirty = True
                elif is_action(act, ch, "KEY2", "o"):
                    _open_options_menu(filtered[cursor] if filtered else None)
                    dirty = True
                elif kl == "f" and filtered:
                    starred = launcher.toggle_favorite(filtered[cursor]["id"])
                    apps = launcher.discover_gui_apps()
                    _set_toast(f"{'Starred' if starred else 'Unstarred'}: {filtered[cursor]['name']}", "ok", secs=2.0)
                    dirty = True
                elif is_step_down(ch):
                    new_sc = launcher.step_app_scale(app_scale, -1)
                    app_scale = launcher.set_app_scale(new_sc)
                    _set_toast(f"Window Scale: {launcher.format_app_scale(app_scale)}", "ok", secs=2.0)
                    dirty = True
                elif is_step_up(ch):
                    new_sc = launcher.step_app_scale(app_scale, +1)
                    app_scale = launcher.set_app_scale(new_sc)
                    _set_toast(f"Window Scale: {launcher.format_app_scale(app_scale)}", "ok", secs=2.0)
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
