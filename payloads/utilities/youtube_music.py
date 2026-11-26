#!/usr/bin/env python3
"""
EmberJack Payload -- Obsidian Ember YouTube Music Player
========================================================
Dedicated full-featured YouTube Music client with continuous background 48kHz
playback while browsing, real-name playlist importing & syncing, full-playlist
offline downloading, offline folder/playlist organization with multi-select,
smart offline-first playback, instant pause, glitch-free Bluetooth/HDMI output
switching, 4 FFT visualizer modes, synced karaoke lyrics, 9 EQ presets,
Loudness Normalizer, Playback Speed, Sleep Timer, and Listening Stats.

Controls (100% Keyboard + Button + WebUI):
  Global (work on EVERY tab while music plays in background!):
    1..6 / Tab            Switch tab (NOW / SEARCH / RADIO / QUEUE / LISTS / OFFLINE)
    Space                 Instant Pause / Resume playback
    N / B                 Next / Previous track
    + / -                 Volume Up / Down (Bluetooth / PipeWire / ALSA)
    M                     Mute / Unmute
    E                     Cycle 9 EQ Presets
    H                     Toggle Shuffle
    R                     Cycle Repeat (OFF / ALL / ONE)
    KEY2 / O              Audio, Speed, Sleep Timer, Track Controls, Tabs & Settings
    /                     Search YouTube Music (songs, artists, or playlist URL)
    KEY3 / Esc            Back / Clear selection / Exit

  Now Playing Tab:
    OK / Enter            Pause / Resume (or open Radio if stopped)
    LEFT / RIGHT          Seek -15s / +15s (or switch tab if stopped)
    UP / DOWN             Volume +5% / -5%
    , / .                 Decrease / Increase Playback Speed (0.75x - 2.0x)
    KEY1 / Y              Toggle Synced Lyrics / Up Next panel
    V                     Cycle Visualizer (Peak VU / Bars / Mirror / Cover)
    G                     Launch Auto-Radio Mix from current song
    L / A / D             Like / Add to Playlist / Download MP3 Offline

  Browse Tabs (Search / Radio / Queue / Lists / Offline):
    UP / DOWN / PgUp/Dn   Scroll items
    LEFT / RIGHT          Switch tabs (or columns in Radio grid)
    OK / Enter            Play / Open selected item
    KEY1                  Context Actions menu (or Search when empty)
    Q / W                 Add to End of Queue / Play Next
    V                     Multi-select toggle (Offline & Playlists)
    A                     Add highlighted/selected track(s) to Playlist or Folder
    D                     Download track — OR Download Entire Playlist (on LISTS)
    S                     Playlist Actions / Sort Offline / Save Queue as Playlist
    F                     Switch Offline Folder filter (in OFFLINE tab)
    [ / ]                 Reorder tracks in Queue or custom Playlist
    X / Delete            Remove track / Delete playlist / Delete offline file
"""

import os
import random
import signal
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Set, Tuple

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

from PIL import Image, ImageDraw

import rj_env
import rj_theme
import rj_ytmusic as ytm
from payloads._input_helper import (
    format_footer_hint,
    is_action,
    is_back,
    is_confirm,
    is_step_down,
    is_step_up,
)

TABS = ("now", "search", "radio", "queue", "lists", "offline")
TAB_LABELS = {
    "now": "NOW",
    "search": "SEARCH",
    "radio": "RADIO",
    "queue": "QUEUE",
    "lists": "LISTS",
    "offline": "OFFLINE",
}

OFFLINE_SORT_MODES = ("newest", "title", "artist", "duration")
OFFLINE_SORT_LABELS = {
    "newest": "NEWEST",
    "title": "A-Z",
    "artist": "ARTIST",
    "duration": "LENGTH",
}


def _load_fonts(width: int, height: int) -> dict:
    wide = width >= 300
    large = width >= 540 and height >= 300
    mid = (width >= 200 and height >= 200 and width < 300)
    short = (height <= 200)
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
        "mid": mid,
        "short": short,
        "title": rj_env.load_font(t_px, bold=True),
        "body": rj_env.load_font(b_px, bold=False),
        "body_bold": rj_env.load_font(b_px, bold=True),
        "sub": rj_env.load_font(s_px, bold=False),
        "badge": rj_env.load_font(tag_px, bold=True, mono=True),
    }


def _layout_metrics(width: int, height: int, active_tab: str, has_banner: bool, fonts: dict) -> dict:
    wide = bool(fonts.get("wide"))
    large = bool(fonts.get("large"))
    mid = bool(fonts.get("mid", width >= 200 and height >= 200 and width < 300))
    short = bool(fonts.get("short", height <= 200))
    if large:
        hdr_h, tab_h, ftr_h = 30, 24, 22
        mini_h = 26 if active_tab != "now" else 0
        ban_h = 20 if has_banner else 0
        row_h = 36
    elif wide and short:
        hdr_h, tab_h, ftr_h = 19, 16, 15
        mini_h = 16 if active_tab != "now" else 0
        ban_h = 14 if has_banner else 0
        row_h = 22
    elif wide:
        hdr_h, tab_h, ftr_h = 22, 18, 17
        mini_h = 18 if active_tab != "now" else 0
        ban_h = 15 if has_banner else 0
        row_h = 24
    elif mid:
        hdr_h, tab_h, ftr_h = 20, 16, 15
        mini_h = 16 if active_tab != "now" else 0
        ban_h = 14 if has_banner else 0
        row_h = 22
    else:
        hdr_h, tab_h, ftr_h = 17, 14, 12
        mini_h = 0
        ban_h = 0
        row_h = 16
    return {
        "hdr_h": hdr_h,
        "tab_h": tab_h,
        "ftr_h": ftr_h,
        "mini_h": mini_h,
        "ban_h": ban_h,
        "row_h": row_h,
        "mid": mid,
        "short": short,
    }


def _wrap_text(d: ImageDraw.ImageDraw, text: str, max_w: int, font, max_lines: int = 2) -> List[str]:
    words = str(text or "").split()
    lines: List[str] = []
    cur = ""
    for w in words:
        cand = (cur + " " + w).strip()
        if d.textbbox((0, 0), cand, font=font)[2] <= max_w:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w
            if len(lines) >= max_lines - 1:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return [rj_theme.truncate_text(d, ln, max_w, font) for ln in lines[:max_lines]]


def _draw_top_chrome(
    d: ImageDraw.ImageDraw,
    width: int,
    hdr_h: int,
    tab_h: int,
    active_tab: str,
    audio_label: str,
    vol_pct: int,
    muted: bool,
    eq_name: str,
    speed: float,
    loudnorm: bool,
    sleep_label: str,
    fonts: dict,
) -> None:
    ET = rj_theme.EMBER_THEME
    wide = fonts["wide"]
    large = fonts["large"]
    mid = bool(fonts.get("mid", False))

    d.rectangle((0, 0, width - 1, hdr_h - 1), fill=ET["bg_elevated"])
    d.line([(0, hdr_h - 1), (width - 1, hdr_h - 1)], fill=ET["border_subtle"], width=1)
    hx = 8 if wide else (5 if mid else 3)

    # Circular Ember Music emblem
    r = 8 if large else (6 if (wide or mid) else 5)
    cy = hdr_h // 2
    d.ellipse((hx, cy - r, hx + r * 2, cy + r), fill=ET["accent"])
    d.polygon(
        [(hx + r - 2, cy - r // 2), (hx + r - 2, cy + r // 2), (hx + r + r // 2 + 1, cy)],
        fill=ET["text_on_accent"],
    )
    hx += r * 2 + (7 if wide else (5 if mid else 4))

    brand = "YT MUSIC" if (wide or mid) else "YTM"
    tb = d.textbbox((0, 0), brand, font=fonts["title"])
    d.text((hx, (hdr_h - (tb[3] - tb[1])) // 2 - tb[1]), brand, font=fonts["title"], fill=ET["accent_bright"])
    hx += (tb[2] - tb[0]) + (8 if wide else (5 if mid else 4))

    if large:
        if eq_name and eq_name != "Flat":
            eq_short = eq_name.split()[0].upper()[:6]
            ew, _ = rj_theme.draw_pill(
                d, hx, max(2, (hdr_h - 16) // 2), f"EQ:{eq_short}",
                fonts["badge"], bg=ET["select_bg"], fg=ET["accent_bright"],
                outline=ET["border_accent"], pad_x=4, pad_y=1, radius=3,
            )
            hx += ew + 5
        if abs(speed - 1.0) > 0.01:
            sw, _ = rj_theme.draw_pill(
                d, hx, max(2, (hdr_h - 16) // 2), f"{speed:g}x",
                fonts["badge"], bg=ET["bg_well"], fg=ET["ok"],
                outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3,
            )
            hx += sw + 5
        if loudnorm:
            lw, _ = rj_theme.draw_pill(
                d, hx, max(2, (hdr_h - 16) // 2), "NORM",
                fonts["badge"], bg=ET["bg_well"], fg=ET["accent_bright"],
                outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3,
            )
            hx += lw + 5
        if sleep_label:
            slw, _ = rj_theme.draw_pill(
                d, hx, max(2, (hdr_h - 16) // 2), sleep_label,
                fonts["badge"], bg="#281d38", fg="#d7b3ff",
                outline="#6f4c9a", pad_x=4, pad_y=1, radius=3,
            )
            hx += slw + 5

    if wide or mid:
        vol_lbl = "MUTE" if muted else f"VOL {vol_pct}%"
        vw = d.textbbox((0, 0), vol_lbl, font=fonts["badge"])[2] + (10 if wide else 8)
        pill_h = 16 if large else 13
        rj_theme.draw_pill(
            d, width - vw - 6, max(1, (hdr_h - pill_h) // 2), vol_lbl, fonts["badge"],
            bg=ET["bg_surface"], fg=ET["warn"] if muted else ET["accent_bright"],
            outline=ET["border_subtle"], pad_x=5 if wide else 4, pad_y=1, radius=3,
        )
        avail = width - vw - hx - 16
        if wide and avail > 58 and audio_label:
            al = rj_theme.truncate_text(d, audio_label, avail, fonts["sub"])
            ab = d.textbbox((0, 0), al, font=fonts["sub"])
            d.text((width - vw - 14 - (ab[2] - ab[0]), (hdr_h - (ab[3] - ab[1])) // 2 - ab[1]), al, font=fonts["sub"], fill=ET["text_muted"])

    # Navigation tab strip
    d.rectangle((0, hdr_h, width - 1, hdr_h + tab_h - 1), fill=ET["bg_surface"])
    d.line([(0, hdr_h + tab_h - 1), (width - 1, hdr_h + tab_h - 1)], fill=ET["border_subtle"], width=1)
    tx = 6 if wide else (4 if mid else 2)
    pill_th = 16 if large else (13 if (wide or mid) else 11)
    for t_key in TABS:
        lbl = TAB_LABELS[t_key]
        act = t_key == active_tab
        tw, _ = rj_theme.draw_pill(
            d, tx, hdr_h + max(1, (tab_h - pill_th) // 2),
            lbl, fonts["badge"],
            bg=ET["select_bg"] if act else ET["bg_well"],
            fg=ET["accent_bright"] if act else ET["text_muted"],
            outline=ET["accent"] if act else ET["border_subtle"],
            pad_x=6 if large else (4 if wide else (3 if mid else 2)), pad_y=1, radius=4 if large else 3,
        )
        tx += tw + (6 if large else (3 if (wide or mid) else 2))
        if tx > width - 16:
            break


def _draw_mini_player(
    d: ImageDraw.ImageDraw,
    width: int,
    y0: int,
    mini_h: int,
    track: Optional[dict],
    state: str,
    elapsed: float,
    fonts: dict,
) -> None:
    ET = rj_theme.EMBER_THEME
    d.rectangle((0, y0, width - 1, y0 + mini_h - 1), fill=ET["bg_overlay"])
    d.line([(0, y0), (width - 1, y0)], fill=ET["border_accent"] if state == "playing" else ET["border_subtle"], width=1)
    if track is None:
        d.text((8 if fonts["wide"] else 3, y0 + 5), "Nothing playing  •  Pick a song, Playlist, or Radio station", font=fonts["sub"], fill=ET["text_muted"])
        return

    dur = int(track.get("duration") or 0)
    prog = min(1.0, max(0.0, elapsed / dur)) if dur > 0 else 0.0
    if prog > 0:
        d.rectangle((0, y0, int(width * prog), y0 + 2), fill=ET["accent"])

    st_lbl = "PLAY" if state == "playing" else ("PAUSE" if state == "paused" else ("LOAD" if state == "loading" else "STOP"))
    st_fg = ET["ok"] if state == "playing" else ET["accent_bright"]
    px = 8 if fonts["wide"] else 3
    pw, _ = rj_theme.draw_pill(
        d, px, y0 + max(4, (mini_h - 14) // 2), st_lbl, fonts["badge"],
        bg=ET["select_bg"], fg=st_fg, outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3,
    )
    px += pw + 8

    t_right = f"{ytm.format_time(elapsed)}" + (f"/{ytm.format_time(dur)}" if dur > 0 else "")
    rw = d.textbbox((0, 0), t_right, font=fonts["badge"])[2] + 10
    d.text((width - rw, y0 + max(4, (mini_h - 12) // 2)), t_right, font=fonts["badge"], fill=ET["text_muted"])

    label = f"{track.get('title', 'Untitled')}  —  {track.get('artist', '')}"
    d.text(
        (px, y0 + max(4, (mini_h - 13) // 2)),
        rj_theme.truncate_text(d, label, max(40, width - px - rw - 8), fonts["body_bold"] if fonts["large"] else fonts["sub"]),
        font=fonts["body_bold"] if fonts["large"] else fonts["sub"],
        fill=ET["text_bright"],
    )


def _draw_visualizer(
    d: ImageDraw.ImageDraw,
    ax0: int,
    spec_y0: int,
    art_s: int,
    spec_y1: int,
    spectrum: List[float],
    peaks: List[float],
    mode: str,
) -> None:
    ET = rj_theme.EMBER_THEME
    h = spec_y1 - spec_y0
    if h < 14 or mode == "cover":
        return
    n_bars = min(16, len(spectrum))
    gap = 3
    bw = max(4, (art_s - gap * (n_bars - 1)) // n_bars)
    if mode == "mirror":
        mid_y = (spec_y0 + spec_y1) // 2
        half_h = max(4, h // 2 - 1)
        d.line([(ax0, mid_y), (ax0 + art_s, mid_y)], fill=ET["border_subtle"], width=1)
        for i in range(n_bars):
            bx = ax0 + i * (bw + gap)
            lvl = max(0.08, min(1.0, float(spectrum[i])))
            bh = max(2, int(half_h * lvl))
            col = ET["accent_bright"] if lvl > 0.62 else (ET["accent"] if lvl > 0.28 else ET["accent_dim"])
            d.rounded_rectangle((bx, mid_y - bh, bx + bw, mid_y + bh), radius=2, fill=col)
    else:
        for i in range(n_bars):
            bx = ax0 + i * (bw + gap)
            lvl = max(0.06, min(1.0, float(spectrum[i])))
            bh = max(3, int(h * lvl))
            col = ET["accent_bright"] if lvl > 0.65 else (ET["accent"] if lvl > 0.30 else ET["accent_dim"])
            d.rounded_rectangle((bx, spec_y1 - bh, bx + bw, spec_y1), radius=2, fill=col)
            if mode == "peaks" and i < len(peaks):
                pk = max(lvl, min(1.0, float(peaks[i])))
                py = spec_y1 - max(4, int(h * pk))
                d.rectangle((bx, py - 2, bx + bw, py), fill="#ffd29d")


def render_ytmusic_frame(
    width: int,
    height: int,
    active_tab: str,
    player_state: dict,
    view_data: dict,
    cover_img: Optional[Image.Image] = None,
    preview_img: Optional[Image.Image] = None,
    lyrics_data: Optional[dict] = None,
    show_lyrics: bool = False,
    toast: str = "",
    toast_kind: str = "info",
    overlay: Optional[dict] = None,
    fonts: Optional[dict] = None,
) -> Image.Image:
    """Pure PIL renderer for the full Obsidian Ember YouTube Music application."""
    ET = rj_theme.EMBER_THEME
    f = fonts or _load_fonts(width, height)
    wide = f["wide"]
    large = f["large"]
    banner_txt_pre = str(view_data.get("banner") or "")
    lm = _layout_metrics(width, height, active_tab, bool(banner_txt_pre), f)
    mid = lm["mid"]
    short = lm["short"]

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    hdr_h = lm["hdr_h"]
    tab_h = lm["tab_h"]
    ftr_h = lm["ftr_h"]
    mini_h = lm["mini_h"]

    _draw_top_chrome(
        d, width, hdr_h, tab_h, active_tab,
        audio_label=player_state.get("audio_label", "System Audio"),
        vol_pct=int(player_state.get("volume_pct", 70)),
        muted=bool(player_state.get("muted", False)),
        eq_name=str(player_state.get("eq_name", "Flat")),
        speed=float(player_state.get("speed", 1.0)),
        loudnorm=bool(player_state.get("loudnorm", False)),
        sleep_label=str(player_state.get("sleep_label", "")),
        fonts=f,
    )

    y0 = hdr_h + tab_h + (5 if large else (3 if (wide or mid) else 2))
    y1 = height - ftr_h - mini_h - (4 if large else 2)

    cur_track = player_state.get("track")
    p_state = player_state.get("state", "stopped")
    elapsed = float(player_state.get("elapsed", 0.0))
    spectrum = player_state.get("spectrum") or [0.06] * 16
    peaks = player_state.get("peaks") or spectrum
    viz_mode = str(player_state.get("visualizer", "peaks"))
    queue = player_state.get("queue") or []
    cur_q_idx = int(player_state.get("current_idx", -1))

    # ======================================================================
    # TAB 1: NOW PLAYING (Cover Art + 4-Mode Visualizer + Track/Up-Next OR Synced Lyrics)
    # ======================================================================
    if active_tab == "now":
        if cur_track is None:
            cy = (y0 + y1) // 2
            d.text((width // 2, cy - 10), "YouTube Music Ready", font=f["title"], fill=ET["accent_bright"], anchor="mm")
            empty_now = format_footer_hint(
                width, height,
                hat_hint="OK:Radio L/R:Tabs",
                hat_240_hint="OK:Radio  L/R:Tabs  K2:Menu",
                cardputer_hint="/:Search  3:Radio  5:Lists  6:Offline",
                hdmi_hint="Press / to Search, 3 for Radio, 5 for Playlists, or 6 for Offline",
            )
            d.text((width // 2, cy + 10), rj_theme.truncate_text(d, empty_now, width - 12, f["sub"]), font=f["sub"], fill=ET["text_muted"], anchor="mm")
        elif large:
            art_s = min(176 if viz_mode == "cover" else 148, y1 - y0 - (46 if viz_mode == "cover" else 82))
            ax0, ay0 = 14, y0 + 4
            rj_theme.draw_card(d, (ax0, ay0, ax0 + art_s, ay0 + art_s), fill=ET["bg_surface"], outline=ET["border_accent"], radius=8)
            if cover_img is not None:
                try:
                    img.paste(cover_img.resize((art_s - 4, art_s - 4), Image.BILINEAR), (ax0 + 2, ay0 + 2))
                except Exception:
                    pass
            else:
                d.ellipse((ax0 + art_s // 2 - 24, ay0 + art_s // 2 - 24, ax0 + art_s // 2 + 24, ay0 + art_s // 2 + 24), fill=ET["select_bg"], outline=ET["accent"], width=2)
                d.text((ax0 + art_s // 2, ay0 + art_s // 2), "YTM", font=f["title"], fill=ET["accent_bright"], anchor="mm")

            spec_y0 = ay0 + art_s + 8
            spec_y1 = y1 - 38
            _draw_visualizer(d, ax0, spec_y0, art_s, spec_y1, spectrum, peaks, viz_mode)

            rx0 = ax0 + art_s + 16
            rx1 = width - 14
            rw = rx1 - rx0
            rj_theme.draw_card(d, (rx0, ay0, rx1, spec_y1), fill=ET["bg_surface"], outline=ET["border_subtle"], radius=8, accent_left=ET["accent"], accent_width=3)

            if show_lyrics:
                ly_hdr_y = ay0 + 8
                d.text((rx0 + 12, ly_hdr_y), "LYRICS", font=f["badge"], fill=ET["accent_bright"])
                d.text((rx1 - 12, ly_hdr_y), rj_theme.truncate_text(d, cur_track.get("title", ""), rw - 90, f["sub"]), font=f["sub"], fill=ET["text_muted"], anchor="ra")
                d.line([(rx0 + 10, ly_hdr_y + 18), (rx1 - 10, ly_hdr_y + 18)], fill=ET["border_subtle"], width=1)

                ld = lyrics_data or {"status": "none", "synced": [], "plain": []}
                ly_y0 = ly_hdr_y + 24
                ly_y1 = spec_y1 - 8
                if ld.get("status") == "loading":
                    d.text(((rx0 + rx1) // 2, (ly_y0 + ly_y1) // 2), "Fetching synced lyrics...", font=f["body"], fill=ET["text_muted"], anchor="mm")
                elif ld.get("synced"):
                    synced = ld["synced"]
                    act_i = ytm.get_active_lyric_index(synced, elapsed)
                    line_h = 22
                    vis_l = max(3, (ly_y1 - ly_y0) // line_h)
                    st_l = max(0, min(act_i - vis_l // 2, max(0, len(synced) - vis_l)))
                    for s_off in range(vis_l):
                        li = st_l + s_off
                        if li >= len(synced):
                            break
                        _ts, ltxt = synced[li]
                        ly = ly_y0 + s_off * line_h
                        is_cur = li == act_i
                        if is_cur:
                            rj_theme.draw_card(d, (rx0 + 8, ly - 2, rx1 - 8, ly + line_h - 4), fill=ET["select_bg"], radius=4)
                        d.text(
                            (rx0 + 14, ly + 1),
                            rj_theme.truncate_text(d, ltxt, rw - 28, f["body_bold"] if is_cur else f["body"]),
                            font=f["body_bold"] if is_cur else f["body"],
                            fill=ET["accent_bright"] if is_cur else ET["text_muted"],
                        )
                elif ld.get("plain"):
                    plain = ld["plain"]
                    line_h = 19
                    vis_l = max(3, (ly_y1 - ly_y0) // line_h)
                    for s_off in range(min(vis_l, len(plain))):
                        d.text((rx0 + 14, ly_y0 + s_off * line_h), rj_theme.truncate_text(d, plain[s_off], rw - 28, f["body"]), font=f["body"], fill=ET["text_primary"])
                else:
                    d.text(((rx0 + rx1) // 2, (ly_y0 + ly_y1) // 2), "Lyrics unavailable for this track", font=f["body"], fill=ET["text_muted"], anchor="mm")
            else:
                py = ay0 + 10
                px = rx0 + 12
                st_lbl = p_state.upper()
                st_fg = ET["ok"] if p_state == "playing" else (ET["danger"] if p_state == "error" else ET["accent_bright"])
                pw, _ = rj_theme.draw_pill(d, px, py, st_lbl, f["badge"], bg=ET["select_bg"], fg=st_fg, outline=ET["border_accent"], pad_x=5, pad_y=1, radius=3)
                px += pw + 5
                if player_state.get("offline_now"):
                    pw, _ = rj_theme.draw_pill(d, px, py, "OFFLINE", f["badge"], bg="#1b2d22", fg=ET["ok"], outline=ET["ok"], pad_x=4, pad_y=1, radius=3)
                    px += pw + 5
                if player_state.get("liked"):
                    pw, _ = rj_theme.draw_pill(d, px, py, "LIKED", f["badge"], bg="#3b1d12", fg=ET["accent_bright"], outline=ET["accent"], pad_x=5, pad_y=1, radius=3)
                    px += pw + 5
                if player_state.get("shuffle") and px < rx1 - 90:
                    pw, _ = rj_theme.draw_pill(d, px, py, "SHUF", f["badge"], bg=ET["bg_well"], fg=ET["ok"], outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3)
                    px += pw + 5
                rep = player_state.get("repeat_mode", "all")
                if rep != "off" and px < rx1 - 70:
                    pw, _ = rj_theme.draw_pill(d, px, py, f"REP:{rep.upper()}", f["badge"], bg=ET["bg_well"], fg=ET["accent_bright"], outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3)
                    px += pw + 5
                if player_state.get("auto_radio") and px < rx1 - 52:
                    rj_theme.draw_pill(d, px, py, "RADIO", f["badge"], bg=ET["bg_well"], fg=ET["ok"], outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3)

                py += 24
                for ln in _wrap_text(d, cur_track.get("title", "Untitled"), rw - 24, f["title"], max_lines=2):
                    d.text((rx0 + 12, py), ln, font=f["title"], fill=ET["text_bright"])
                    py += 19
                art_str = cur_track.get("artist", "YouTube Music")
                if cur_track.get("album"):
                    art_str += f"  •  {cur_track['album']}"
                d.text((rx0 + 12, py + 2), rj_theme.truncate_text(d, art_str, rw - 24, f["body"]), font=f["body"], fill=ET["accent_bright"])
                py += 22

                d.line([(rx0 + 10, py + 2), (rx1 - 10, py + 2)], fill=ET["border_subtle"], width=1)
                py += 8
                d.text((rx0 + 12, py), f"UP NEXT  ({max(0, len(queue) - cur_q_idx - 1)} queued)", font=f["badge"], fill=ET["text_muted"])
                py += 16
                shown_up = 0
                for qi in range(cur_q_idx + 1, min(len(queue), cur_q_idx + 4)):
                    if py + 15 > spec_y1 - 6:
                        break
                    qt = queue[qi]
                    line_s = f"{qi - cur_q_idx}. {qt.get('title', '?')} — {qt.get('artist', '')}"
                    d.text((rx0 + 12, py), rj_theme.truncate_text(d, line_s, rw - 24, f["sub"]), font=f["sub"], fill=ET["text_primary"])
                    py += 16
                    shown_up += 1
                if shown_up == 0:
                    msg_q = "Auto-Radio will queue similar tracks automatically" if player_state.get("auto_radio") else "Press G to start Auto-Radio Mix"
                    d.text((rx0 + 12, py), rj_theme.truncate_text(d, msg_q, rw - 24, f["sub"]), font=f["sub"], fill=ET["text_muted"])

            dur = int(cur_track.get("duration") or 0)
            prog = min(1.0, max(0.0, elapsed / dur)) if dur > 0 else 0.0
            bar_y = y1 - 28
            rj_theme.draw_progress_bar(d, (14, bar_y, width - 14, bar_y + 8), prog)
            d.text((14, bar_y + 12), ytm.format_time(elapsed), font=f["sub"], fill=ET["text_primary"])
            if toast:
                t_col = ET["ok"] if toast_kind == "ok" else (ET["danger"] if toast_kind == "err" else ET["accent_bright"])
                d.text((width // 2, bar_y + 12), rj_theme.truncate_text(d, toast, width - 160, f["sub"]), font=f["sub"], fill=t_col, anchor="mt")
            elif player_state.get("status_msg") and p_state in ("loading", "error"):
                d.text((width // 2, bar_y + 12), rj_theme.truncate_text(d, player_state["status_msg"], width - 160, f["sub"]), font=f["sub"], fill=ET["warn"], anchor="mt")
            d.text((width - 14, bar_y + 12), ytm.format_time(dur) if dur > 0 else "--:--", font=f["sub"], fill=ET["text_muted"], anchor="ra")
        else:
            d.text((6, y0 + 2), rj_theme.truncate_text(d, cur_track.get("title", ""), width - 12, f["body_bold"]), font=f["body_bold"], fill=ET["text_bright"])
            d.text((6, y0 + (15 if short else (18 if (wide or mid) else 13))), rj_theme.truncate_text(d, cur_track.get("artist", ""), width - 12, f["sub"]), font=f["sub"], fill=ET["accent_bright"])
            sy0 = y0 + (29 if short else (34 if (wide or mid) else 25))
            sy1 = y1 - (23 if short else (26 if (wide or mid) else 18))
            if show_lyrics and sy1 - sy0 >= 14:
                ld = lyrics_data or {"status": "none", "synced": [], "plain": []}
                if ld.get("status") == "loading":
                    d.text((width // 2, (sy0 + sy1) // 2), "Fetching lyrics...", font=f["sub"], fill=ET["text_muted"], anchor="mm")
                elif ld.get("synced"):
                    synced = ld["synced"]
                    act_i = ytm.get_active_lyric_index(synced, elapsed)
                    lh_c = 13 if (wide or mid) else 10
                    vis_l = max(1, (sy1 - sy0) // lh_c)
                    st_l = max(0, min(act_i - vis_l // 2, max(0, len(synced) - vis_l)))
                    for s_off in range(vis_l):
                        li = st_l + s_off
                        if li >= len(synced):
                            break
                        _ts, ltxt = synced[li]
                        is_cur = li == act_i
                        d.text(
                            (6, sy0 + s_off * lh_c),
                            rj_theme.truncate_text(d, ltxt, width - 12, f["body_bold"] if is_cur else f["sub"]),
                            font=f["body_bold"] if is_cur else f["sub"],
                            fill=ET["accent_bright"] if is_cur else ET["text_muted"],
                        )
                elif ld.get("plain"):
                    lh_c = 12 if (wide or mid) else 10
                    vis_l = max(1, (sy1 - sy0) // lh_c)
                    for s_off in range(min(vis_l, len(ld["plain"]))):
                        d.text((6, sy0 + s_off * lh_c), rj_theme.truncate_text(d, ld["plain"][s_off], width - 12, f["sub"]), font=f["sub"], fill=ET["text_primary"])
                else:
                    d.text((width // 2, (sy0 + sy1) // 2), "Lyrics unavailable", font=f["sub"], fill=ET["text_muted"], anchor="mm")
            elif sy1 - sy0 >= 10:
                nb = 14 if (wide or mid) else 12
                bw = max(3, (width - 12 - (nb - 1) * 2) // nb)
                for i in range(nb):
                    lvl = max(0.08, min(1.0, float(spectrum[i % len(spectrum)])))
                    bh = max(2, int((sy1 - sy0) * lvl))
                    d.rectangle((6 + i * (bw + 2), sy1 - bh, 6 + i * (bw + 2) + bw, sy1), fill=ET["accent"])
            dur = int(cur_track.get("duration") or 0)
            prog = min(1.0, max(0.0, elapsed / dur)) if dur > 0 else 0.0
            bar_y = y1 - (18 if short else (20 if (wide or mid) else 14))
            rj_theme.draw_progress_bar(d, (6, bar_y, width - 6, bar_y + 5), prog)
            time_s = f"{ytm.format_time(elapsed)} / {ytm.format_time(dur)}"
            d.text((6, bar_y + 7), time_s, font=f["sub"], fill=ET["text_muted"])
            if toast and (wide or mid):
                tw_avail = max(40, width - 96)
                t_col = ET["ok"] if toast_kind == "ok" else (ET["danger"] if toast_kind == "err" else ET["accent_bright"])
                d.text((width - 6, bar_y + 7), rj_theme.truncate_text(d, toast, tw_avail, f["sub"]), font=f["sub"], fill=t_col, anchor="ra")

    # ======================================================================
    # TAB 3: RADIO STATIONS (Mood & Genre Grid)
    # ======================================================================
    elif active_tab == "radio":
        rcur = int(view_data.get("cursor", 0))
        if large:
            cols = 2
            gap_x, gap_y = 10, 7
            pad_x = 12
            cw = (width - pad_x * 2 - gap_x) // cols
            ch = 44
            vis_rows = max(1, (y1 - y0 + gap_y) // (ch + gap_y))
            total_rows = (len(ytm.RADIO_STATIONS) + cols - 1) // cols
            st_row = max(0, min((rcur // cols) - vis_rows + 1, max(0, total_rows - vis_rows)))
            for r_off in range(vis_rows):
                ri = st_row + r_off
                for ci in range(cols):
                    idx = ri * cols + ci
                    if idx >= len(ytm.RADIO_STATIONS):
                        break
                    name, _q, desc = ytm.RADIO_STATIONS[idx]
                    sel = idx == rcur
                    x0 = pad_x + ci * (cw + gap_x)
                    ry = y0 + r_off * (ch + gap_y)
                    rj_theme.draw_card(
                        d, (x0, ry, x0 + cw, ry + ch),
                        fill=ET["select_bg"] if sel else ET["bg_surface"],
                        outline=ET["border_accent"] if sel else ET["border_subtle"],
                        radius=7, accent_left=ET["accent"] if sel else None, accent_width=3,
                    )
                    d.text((x0 + 12, ry + 6), name, font=f["body_bold"], fill=ET["accent_bright"] if sel else ET["text_bright"])
                    d.text((x0 + 12, ry + 24), rj_theme.truncate_text(d, desc, cw - 20, f["sub"]), font=f["sub"], fill=ET["text_muted"])
        else:
            row_h = 22 if wide else 15
            vis = max(1, (y1 - y0) // row_h)
            st = max(0, min(rcur - vis // 2, max(0, len(ytm.RADIO_STATIONS) - vis)))
            for slot in range(vis):
                idx = st + slot
                if idx >= len(ytm.RADIO_STATIONS):
                    break
                name, _q, _desc = ytm.RADIO_STATIONS[idx]
                sel = idx == rcur
                ry = y0 + slot * row_h
                rj_theme.draw_card(d, (4, ry, width - 8, ry + row_h - 2), fill=ET["select_bg"] if sel else ET["bg_surface"], radius=3, accent_left=ET["accent"] if sel else None, accent_width=2)
                d.text((10, ry + 3), name, font=f["body"], fill=ET["accent_bright"] if sel else ET["text_primary"])

    # ======================================================================
    # TABS 2, 4, 5, 6: LIST VIEWS (Search / Queue / Playlists / Offline)
    # ======================================================================
    else:
        items = view_data.get("items", [])
        cursor = int(view_data.get("cursor", 0))
        scroll = int(view_data.get("scroll", 0))
        empty_msg = view_data.get("empty_msg", "No items yet")
        empty_sub = view_data.get(
            "empty_sub",
            format_footer_hint(
                width, height,
                hat_hint="K1:Search  L/R:Tabs",
                hat_240_hint="Press KEY1 to search YouTube Music",
                cardputer_hint="Press / to search YouTube Music",
                hdmi_hint="Press / to search YouTube Music",
            ),
        )
        liked_ids = view_data.get("liked_ids") or set()
        offline_ids = view_data.get("offline_ids") or set()
        selected_keys = view_data.get("selected_keys") or set()
        banner_txt = str(view_data.get("banner") or "")
        show_preview = large and active_tab in ("search", "queue", "offline")

        if banner_txt and lm["ban_h"] > 0:
            bh_ban = lm["ban_h"]
            d.rectangle((8 if wide else 4, y0, width - (8 if wide else 4), y0 + bh_ban - 1), fill=ET["bg_surface"])
            d.line([(8 if wide else 4, y0 + bh_ban - 1), (width - (8 if wide else 4), y0 + bh_ban - 1)], fill=ET["border_subtle"], width=1)
            d.text((14 if wide else 8, y0 + (3 if large else 2)), rj_theme.truncate_text(d, banner_txt, width - 24, f["badge"]), font=f["badge"], fill=ET["accent_bright"])
            y0 += bh_ban + (3 if large else 2)

        if not items:
            cy = (y0 + y1) // 2
            d.text((width // 2, cy - 10), empty_msg, font=f["body_bold"], fill=ET["text_primary"], anchor="mm")
            d.text((width // 2, cy + 10), rj_theme.truncate_text(d, empty_sub, width - 24, f["sub"]), font=f["sub"], fill=ET["text_muted"], anchor="mm")
        else:
            lx0 = 8 if wide else (4 if mid else 2)
            lx1 = int(width * 0.64) - 4 if show_preview else (width - (10 if wide else (8 if mid else 5)))
            row_h = lm["row_h"]
            vis = max(1, (y1 - y0) // row_h)
            st = max(0, min(scroll, max(0, len(items) - vis)))

            for slot in range(vis):
                idx = st + slot
                if idx >= len(items):
                    break
                it = items[idx]
                sel = idx == cursor
                is_now = (active_tab == "queue" and idx == cur_q_idx)
                sel_key = str(it.get("path") or it.get("id") or idx)
                is_marked = sel_key in selected_keys
                ry = y0 + slot * row_h
                ry1 = min(y1, ry + row_h - (4 if large else (3 if (wide or mid) else 2)))
                rj_theme.draw_card(
                    d, (lx0, ry, lx1, ry1),
                    fill=ET["select_bg"] if sel else ("#251b14" if is_marked else ET["bg_surface"]),
                    outline=ET["border_accent"] if sel else (ET["ok"] if is_now else (ET["accent"] if is_marked else ET["border_subtle"])),
                    radius=6 if (wide or mid) else 3,
                    accent_left=ET["ok"] if is_now else (ET["accent"] if (sel or is_marked) else None),
                    accent_width=3 if (wide or mid) else 2,
                )
                right_lbl = ""
                right_ok = False
                if it.get("badge"):
                    right_lbl = str(it["badge"])
                    right_ok = "OFF" in right_lbl
                elif it.get("duration"):
                    right_lbl = ytm.format_time(it["duration"])
                elif it.get("local") or it.get("id") in offline_ids:
                    right_lbl = "OFFLINE"
                    right_ok = True

                rx = lx1 - (6 if (wide or mid) else 3)
                if right_lbl:
                    rw_p = d.textbbox((0, 0), right_lbl, font=f["badge"])[2] + (8 if (wide or mid) else 4)
                    rj_theme.draw_pill(
                        d, rx - rw_p, ry + max(1, (ry1 - ry - (15 if large else 11)) // 2),
                        right_lbl, f["badge"],
                        bg="#1b2d22" if right_ok else ET["bg_well"],
                        fg=ET["ok"] if right_ok else (ET["accent_bright"] if sel else ET["text_muted"]),
                        outline=ET["ok"] if right_ok else ET["border_subtle"],
                        pad_x=4 if (wide or mid) else 2, pad_y=1, radius=3,
                    )
                    rx -= rw_p + 6

                if (wide or mid) and not it.get("badge") and it.get("duration") and (it.get("local") or it.get("id") in offline_ids):
                    ow_p = d.textbbox((0, 0), "OFF", font=f["badge"])[2] + 6
                    rj_theme.draw_pill(
                        d, rx - ow_p, ry + max(1, (ry1 - ry - (15 if large else 11)) // 2),
                        "OFF", f["badge"], bg="#1b2d22", fg=ET["ok"], outline=ET["ok"], pad_x=3, pad_y=1, radius=3,
                    )
                    rx -= ow_p + 5

                mark_prefix = "[x] " if is_marked else ""
                prefix = mark_prefix + ("> " if is_now else ("* " if it.get("id") in liked_ids else ""))
                ttl = prefix + str(it.get("title") or it.get("name") or "?")
                d.text((lx0 + (8 if (wide or mid) else 4), ry + (3 if large else (1 if (wide or mid) else 1))), rj_theme.truncate_text(d, ttl, max(36, rx - lx0 - 10), f["body_bold"] if (wide or mid) else f["body"]), font=f["body_bold"] if (wide or mid) else f["body"], fill=ET["text_bright"] if sel else ET["text_primary"])
                if wide or mid:
                    sub = str(it.get("artist") or it.get("subtitle") or "")
                    if it.get("folder") and active_tab == "offline":
                        sub = f"[{it['folder']}]  {sub}" if sub else f"[{it['folder']}]"
                    if sub:
                        d.text((lx0 + 8, ry + (19 if large else 11)), rj_theme.truncate_text(d, sub, max(36, rx - lx0 - 10), f["sub"]), font=f["sub"], fill=ET["text_muted"])

            if len(items) > vis:
                rj_theme.draw_scrollbar(d, lx1 + 2, y0, y1, st, vis, len(items), width=4 if (wide or mid) else 2)

            if show_preview and 0 <= cursor < len(items):
                sel_it = items[cursor]
                px0, px1 = lx1 + 10, width - 8
                rj_theme.draw_card(d, (px0, y0, px1, y1), fill=ET["bg_surface"], outline=ET["border_subtle"], radius=7)
                pw = px1 - px0 - 16
                art_s = min(110, pw, max(60, (y1 - y0) // 2))
                ax = px0 + (px1 - px0 - art_s) // 2
                ay = y0 + 8
                d.rounded_rectangle((ax, ay, ax + art_s, ay + art_s), radius=6, fill=ET["bg_well"], outline=ET["border_subtle"])
                if preview_img is not None:
                    try:
                        img.paste(preview_img.resize((art_s - 2, art_s - 2), Image.BILINEAR), (ax + 1, ay + 1))
                    except Exception:
                        pass
                else:
                    d.text((ax + art_s // 2, ay + art_s // 2), "OFFLINE" if sel_it.get("local") else "TRACK", font=f["badge"], fill=ET["ok"] if sel_it.get("local") else ET["text_muted"], anchor="mm")

                ty = ay + art_s + 8
                for ln in _wrap_text(d, sel_it.get("title", ""), pw, f["body_bold"], max_lines=2):
                    d.text((px0 + 8, ty), ln, font=f["body_bold"], fill=ET["text_bright"])
                    ty += 16
                d.text((px0 + 8, ty + 2), rj_theme.truncate_text(d, sel_it.get("artist", ""), pw, f["sub"]), font=f["sub"], fill=ET["accent_bright"])
                if active_tab == "search":
                    act_s = "Enter:Play  W:Next  Q:Queue  S:SaveList"
                elif active_tab == "queue":
                    act_s = "Enter:Jump  S:SaveList  [/]:Move"
                else:
                    act_s = "V:Mark  A:Organize  F:Folder  S:Sort"
                d.text((px0 + 8, y1 - 20), rj_theme.truncate_text(d, act_s, pw, f["sub"]), font=f["sub"], fill=ET["text_muted"])

        # Non-blocking toast banner on browse tabs
        if toast and not overlay:
            tb = rj_theme.measure_text(f["body_bold"], toast, d)
            tw_b = min(width - 20, (tb[2] - tb[0]) + 22)
            th_b = 24 if wide else 16
            tx0_b = (width - tw_b) // 2
            ty0_b = y1 - th_b - 4
            out_c = ET["danger"] if toast_kind == "err" else (ET["ok"] if toast_kind == "ok" else ET["border_accent"])
            rj_theme.draw_card(d, (tx0_b, ty0_b, tx0_b + tw_b, ty0_b + th_b), fill=ET["bg_elevated"], outline=out_c, radius=5)
            d.text((width // 2, ty0_b + th_b // 2), rj_theme.truncate_text(d, toast, tw_b - 10, f["body_bold"]), font=f["body_bold"], fill=ET["text_bright"], anchor="mm")

    # Persistent mini-player bar on non-Now-Playing tabs
    if mini_h > 0:
        _draw_mini_player(d, width, height - ftr_h - mini_h, mini_h, cur_track, p_state, elapsed, f)

    # Footer
    d.rectangle((0, height - ftr_h, width - 1, height - 1), fill=ET["bg_elevated"])
    d.line([(0, height - ftr_h), (width - 1, height - ftr_h)], fill=ET["border_subtle"], width=1)
    hdmi_hints = {
        "now": "Spc:Pause  <>:Seek  ,/.:Speed  V:Viz  Y:Lyrics  G:Radio  E:EQ  L:Like  D:Save  O:Settings",
        "search": "/:Search  Enter:Play  W:Next  Q:Queue  S:SaveList  L:Like  A:Playlist  D:Save  Tab:Tabs",
        "radio": "Arrows:Pick Station  Enter:Start Radio  Spc:Pause  +/-:Vol  O:Settings  Tab:Tabs",
        "queue": "Enter:Play  S:Save as Playlist  X:Remove  [/]:Reorder  G:Radio  C:Clear  Tab:Tabs",
        "lists": (
            "Enter:Play  S:Playlist Actions  V:Mark  [/]:Move  D:Save  X:Remove  Esc:Back"
            if view_data.get("in_playlist")
            else "Enter:Open  D:Download Playlist  S:Actions  C:New  I:Import URL  X:Delete  Tab:Tabs"
        ),
        "offline": "Enter:Play  V:Multi-Select  A:Organize/Playlist  F:Folders  S:Sort  Q:Queue  X:Delete",
    }
    cp_hints = {
        "now": "Spc:P <>:Sk Y:Lyr E:EQ O:Set Tab:Tab",
        "search": "/:Find Ent:Play Q:Que D:DL O:Set Tab",
        "radio": "Ent:Play Spc:P /:Find O:Set Tab:Tab",
        "queue": "Ent:Play S:Save X:Del O:Set Tab:Tab",
        "lists": "Ent:Play S:Act D:DL Esc:Back" if view_data.get("in_playlist") else "Ent:Open D:DL C:New I:Imp O:Set Tab",
        "offline": "Ent:Play F:Fld S:Sort A:Org X:Del Tab",
    }
    hat_hints = {
        "now": "OK:P K1:Lyr K2:Set K3:X",
        "search": "OK:Go K1:Act K2:Set K3:X",
        "radio": "OK:Go K1:Fnd K2:Set K3:X",
        "queue": "OK:Go K1:Act K2:Set K3:X",
        "lists": "OK:Go K1:Act K2:Set K3:B",
        "offline": "OK:Go K1:Act K2:Set K3:X",
    }
    hat240_hints = {
        "now": "OK:Pause K1:Lyrics K2:Menu K3:Exit",
        "search": "OK:Play K1:Actions K2:Menu K3:Exit",
        "radio": "OK:Tune K1:Search K2:Menu K3:Exit",
        "queue": "OK:Play K1:Actions K2:Menu K3:Exit",
        "lists": "OK:Play K1:Actions K2:Menu K3:Back" if view_data.get("in_playlist") else "OK:Open K1:Actions K2:Menu K3:Exit",
        "offline": "OK:Play K1:Actions K2:Menu K3:Exit",
    }
    hint = format_footer_hint(
        width, height,
        hat_hint=hat_hints.get(active_tab, "OK:Go K1:Act K2:Set K3:X"),
        hat_240_hint=hat240_hints.get(active_tab, "OK:Go K1:Actions K2:Menu K3:Exit"),
        cardputer_hint=cp_hints.get(active_tab, "Tab:Tabs Spc:P /:Find O:Set Esc:X"),
        hdmi_hint=hdmi_hints.get(active_tab, "Tab:Tabs  Spc:Pause  O:Audio/Settings  Esc:Exit"),
    )
    hb = d.textbbox((0, 0), hint, font=f["sub"])
    d.text((6 if (wide or mid) else 3, height - ftr_h + (ftr_h - (hb[3] - hb[1])) // 2 - hb[1]), rj_theme.truncate_text(d, hint, width - 10, f["sub"]), font=f["sub"], fill=ET["text_muted"])

    # Modal Overlay (Busy / Batch Download / Menu)
    if overlay:
        kind = overlay.get("kind", "")
        if kind == "busy":
            bw = min(width - 16, max(112, int(width * 0.72)))
            bh = min(height - 12, 94 if large else (82 if (wide or mid) else 64))
            bx0, by0 = (width - bw) // 2, max(4, (height - bh) // 2)
            rj_theme.draw_card(d, (bx0, by0, bx0 + bw, by0 + bh), fill=ET["bg_elevated"], outline=ET["border_accent"], radius=8 if (wide or mid) else 4, accent_left=ET["accent"], accent_width=3)
            d.text((bx0 + (14 if wide else 8), by0 + (12 if large else 8)), rj_theme.truncate_text(d, overlay.get("title", "Loading..."), bw - 20, f["title"]), font=f["title"], fill=ET["accent_bright"])
            d.text((bx0 + (14 if wide else 8), by0 + (38 if large else (30 if (wide or mid) else 24))), rj_theme.truncate_text(d, overlay.get("sub", ""), bw - 20, f["body"]), font=f["body"], fill=ET["text_primary"])
            if overlay.get("progress") is not None:
                rj_theme.draw_progress_bar(d, (bx0 + (14 if wide else 8), by0 + bh - (22 if large else 16), bx0 + bw - (14 if wide else 8), by0 + bh - (12 if large else 9)), float(overlay["progress"]))
        elif kind == "batch_busy":
            bw = min(width - 16, max(116, int(width * 0.80)))
            bh = min(height - 10, 136 if large else (106 if (wide or mid) else 96))
            bx0, by0 = (width - bw) // 2, max(4, (height - bh) // 2)
            rj_theme.draw_card(d, (bx0, by0, bx0 + bw, by0 + bh), fill=ET["bg_elevated"], outline=ET["accent"], radius=8 if (wide or mid) else 4, accent_left=ET["accent"], accent_width=4 if wide else 2, width=2)
            px_m = bx0 + (14 if wide else 8)
            pw_m = bw - (26 if wide else 16)
            d.text((px_m, by0 + (10 if large else 6)), rj_theme.truncate_text(d, overlay.get("title", "Downloading Playlist..."), pw_m, f["title"]), font=f["title"], fill=ET["accent_bright"])
            d.text((px_m, by0 + (34 if large else (23 if (wide or mid) else 19))), rj_theme.truncate_text(d, overlay.get("sub", ""), pw_m, f["body"]), font=f["body"], fill=ET["text_primary"])
            # Track progress bar
            rj_theme.draw_progress_bar(d, (px_m, by0 + (58 if large else (40 if (wide or mid) else 34)), bx0 + bw - (14 if wide else 8), by0 + (66 if large else (46 if (wide or mid) else 39))), float(overlay.get("item_prog", 0.0)))
            # Overall playlist progress bar
            d.text((px_m, by0 + (74 if large else (52 if (wide or mid) else 45))), rj_theme.truncate_text(d, overlay.get("overall_txt", ""), pw_m, f["sub"]), font=f["sub"], fill=ET["text_muted"])
            rj_theme.draw_progress_bar(d, (px_m, by0 + (92 if large else (66 if (wide or mid) else 58)), bx0 + bw - (14 if wide else 8), by0 + (100 if large else (72 if (wide or mid) else 63))), float(overlay.get("overall_prog", 0.0)), fill=ET["ok"])
            cancel_txt = format_footer_hint(
                width, height,
                hat_hint="K3:Stop after track",
                hat_240_hint="KEY3: Stop after current track",
                cardputer_hint="Esc: Stop after current track",
                hdmi_hint="Press Esc to stop after current track",
            )
            d.text((px_m, by0 + bh - (22 if large else (15 if (wide or mid) else 13))), rj_theme.truncate_text(d, cancel_txt, pw_m, f["sub"]), font=f["sub"], fill=ET["text_muted"])
        elif kind == "menu":
            m_items = overlay.get("items", [])
            msel = int(overlay.get("sel", 0))
            bw = min(width - 14, max(114, int(width * (0.66 if large else 0.84))))
            row_h = 26 if large else (20 if (wide or mid) else 15)
            hdr_p = 36 if large else (24 if (wide or mid) else 19)
            max_bh = height - 10
            vis_m = min(len(m_items), max(2, (max_bh - hdr_p - 8) // row_h))
            bh = min(max_bh, hdr_p + vis_m * row_h + 8)
            bx0, by0 = (width - bw) // 2, max(4, (height - bh) // 2)
            rj_theme.draw_card(d, (bx0, by0, bx0 + bw, by0 + bh), fill=ET["bg_elevated"], outline=ET["border_accent"], radius=8 if (wide or mid) else 4, accent_left=ET["accent"], accent_width=3)
            d.text((bx0 + 10, by0 + (8 if large else 5)), rj_theme.truncate_text(d, overlay.get("title", "Options"), bw - 20, f["title"]), font=f["title"], fill=ET["accent_bright"])
            d.line([(bx0 + 6, by0 + hdr_p - 4), (bx0 + bw - 6, by0 + hdr_p - 4)], fill=ET["border_subtle"], width=1)
            mst = max(0, min(msel - vis_m + 1, max(0, len(m_items) - vis_m)))
            for i in range(vis_m):
                idx = mst + i
                if idx >= len(m_items):
                    break
                lbl = m_items[idx][0] if isinstance(m_items[idx], tuple) else str(m_items[idx])
                ry = by0 + hdr_p + i * row_h
                is_s = idx == msel
                if is_s:
                    rj_theme.draw_card(d, (bx0 + 6, ry, bx0 + bw - 6, ry + row_h - 3), fill=ET["select_bg"], radius=4, accent_left=ET["accent"], accent_width=2)
                d.text((bx0 + 14, ry + (4 if large else 2)), rj_theme.truncate_text(d, lbl, bw - 24, f["body"]), font=f["body"], fill=ET["accent_bright"] if is_s else ET["text_primary"])

    return img


def main() -> int:
    import RPi.GPIO as GPIO
    import LCD_1in44
    try:
        import rj_keyboard
    except Exception:
        rj_keyboard = None
    from payloads import _audio_helper as audio_helper
    from payloads._input_helper import poll_ui_input, flush_input, text_input_mode
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

    player = ytm.YTMusicPlayer()
    covers = ytm.CoverArtCache()
    lyrics_cache = ytm.LyricsCache()

    tab_idx = 2  # Start on RADIO (or NOW once playing)
    show_lyrics = False
    search_results: List[dict] = []
    search_query = ""
    search_playlist_title = ""
    cursors: Dict[str, int] = {t: 0 for t in TABS}
    scrolls: Dict[str, int] = {t: 0 for t in TABS}
    open_playlist_id: Optional[str] = None  # "__liked__", "__top__", or custom playlist id
    pl_cursor = 0
    pl_scroll = 0
    offline_folder_filter: Optional[str] = None  # None = All offline tracks
    offline_sort_idx = 0
    selected_keys: Set[str] = set()
    audio_label = audio_helper.get_active_audio_label()
    last_audio_chk = time.monotonic()

    liked_cache: List[dict] = []
    liked_ids_cache: set = set()
    top_cache: List[dict] = []
    playlists_cache: List[dict] = []
    offline_cache: List[dict] = []
    offline_index_cache: dict = {}
    offline_ids_cache: set = set()
    last_error_seen = ""
    last_track_id = ""

    def _sort_offline_items(tracks: List[dict]) -> List[dict]:
        mode = OFFLINE_SORT_MODES[offline_sort_idx]
        if mode == "title":
            return sorted(tracks, key=lambda t: str(t.get("title", "")).lower())
        if mode == "artist":
            return sorted(tracks, key=lambda t: (str(t.get("artist", "")).lower(), str(t.get("title", "")).lower()))
        if mode == "duration":
            return sorted(tracks, key=lambda t: int(t.get("duration") or 0), reverse=True)
        return list(tracks)

    def _refresh_library_cache(include_offline: bool = False, probe_dur: bool = False) -> None:
        nonlocal liked_cache, liked_ids_cache, top_cache, playlists_cache, offline_cache, offline_index_cache, offline_ids_cache
        liked_cache = ytm.load_liked()
        liked_ids_cache = {t.get("id") for t in liked_cache if t.get("id")}
        top_cache = ytm.get_most_played_tracks(limit=30)
        playlists_cache = ytm.load_playlists()
        offline_index_cache = ytm.load_offline_index()
        offline_ids_cache = {
            tid for tid, meta in offline_index_cache.get("by_id", {}).items()
            if meta.get("path") and os.path.isfile(meta["path"])
        }
        if include_offline:
            raw_off = ytm.list_offline_library(folder=offline_folder_filter, probe_durations=probe_dur)
            offline_cache = _sort_offline_items(raw_off)

    _refresh_library_cache(include_offline=True, probe_dur=False)

    toast = ""
    toast_kind = "info"
    toast_until = 0.0

    def _set_toast(msg: str, kind: str = "info", secs: float = 2.8) -> None:
        nonlocal toast, toast_kind, toast_until
        toast = msg
        toast_kind = kind
        toast_until = time.monotonic() + secs

    def _get_playlist_by_id(pid: str) -> Optional[dict]:
        for pl in playlists_cache:
            if pl.get("id") == pid:
                return pl
        return None

    def _get_open_playlist_tracks() -> List[dict]:
        if open_playlist_id == "__liked__":
            return list(liked_cache)
        if open_playlist_id == "__top__":
            return list(top_cache)
        pl = _get_playlist_by_id(open_playlist_id or "")
        return list(pl.get("tracks", [])) if pl else []

    def _build_lists_view() -> List[dict]:
        if open_playlist_id is not None:
            return _get_open_playlist_tracks()
        # No Listen History in menu — clean Liked Songs + Most Played + User/Imported Playlists!
        rows = [
            {"id": "__liked__", "title": "Liked Songs", "subtitle": "Your saved favorites", "badge": str(len(liked_cache))},
        ]
        if top_cache:
            rows.append({"id": "__top__", "title": "Most Played", "subtitle": "Your top songs by play count", "badge": str(len(top_cache))})
        for pl in playlists_cache:
            t_cnt = len(pl.get("tracks", []))
            off_cnt = ytm.count_playlist_offline_tracks(pl, _index=offline_index_cache)
            if t_cnt > 0 and off_cnt == t_cnt:
                badge = f"{t_cnt} OFF"
            elif off_cnt > 0:
                badge = f"{off_cnt}/{t_cnt} OFF"
            else:
                badge = str(t_cnt)
            sub_parts = []
            if pl.get("source_url"):
                sub_parts.append("Synced YT Playlist")
            elif pl.get("offline_dir"):
                sub_parts.append(f"Folder: {pl['offline_dir']}")
            else:
                sub_parts.append("Custom Playlist")
            rows.append({
                "id": pl["id"],
                "title": pl["name"],
                "subtitle": "  •  ".join(sub_parts),
                "badge": badge,
                "custom": True,
                "playlist": pl,
            })
        rows.append({"id": "__new__", "title": "+ Create New Playlist", "subtitle": "Build a custom mix", "badge": "NEW"})
        rows.append({"id": "__import__", "title": "+ Import YouTube Playlist URL", "subtitle": "Auto-names & optional full offline download", "badge": "URL"})
        return rows

    def _get_player_snapshot() -> dict:
        tr = player.current_track
        return {
            "track": tr,
            "state": player.state,
            "status_msg": player.status_msg,
            "elapsed": player.get_elapsed(),
            "spectrum": player.get_spectrum(),
            "peaks": player.get_peaks(),
            "visualizer": player.visualizer,
            "queue": list(player.queue),
            "current_idx": player.current_idx,
            "volume_pct": player.volume_pct,
            "muted": player.muted,
            "eq_name": player.eq_name,
            "speed": player.speed,
            "loudnorm": player.loudnorm,
            "sleep_label": player.get_sleep_label(),
            "shuffle": player.cfg["shuffle"],
            "repeat_mode": player.cfg["repeat_mode"],
            "auto_radio": player.cfg["auto_radio"],
            "liked": bool(tr and tr.get("id") in liked_ids_cache),
            "offline_now": bool(tr and (tr.get("local") or tr.get("id") in offline_ids_cache)),
            "audio_label": audio_label,
        }

    def _render(overlay: Optional[dict] = None) -> None:
        tab = TABS[tab_idx]
        p_snap = _get_player_snapshot()
        v_data: dict = {
            "liked_ids": liked_ids_cache,
            "offline_ids": offline_ids_cache,
            "selected_keys": selected_keys if tab in ("offline", "lists") else set(),
        }
        preview_img = None

        if tab == "search":
            v_data.update({
                "items": search_results,
                "cursor": cursors["search"],
                "scroll": scrolls["search"],
                "banner": f"PLAYLIST: {search_playlist_title} (Press S to save)" if search_playlist_title else "",
                "empty_msg": f'No results for "{search_query}"' if search_query else "Search YouTube Music",
                "empty_sub": format_footer_hint(
                    W, H,
                    hat_hint="OK or K1: Search",
                    hat_240_hint="Press OK or KEY1 to search",
                    cardputer_hint="Press / or Enter to search songs or URL",
                    hdmi_hint="Press / or Enter to search songs, artists, albums, or paste a playlist URL",
                ),
            })
            if 0 <= cursors["search"] < len(search_results):
                preview_img = covers.get(search_results[cursors["search"]])
        elif tab == "radio":
            v_data["cursor"] = cursors["radio"]
        elif tab == "queue":
            q_items = p_snap["queue"]
            v_data.update({
                "items": q_items,
                "cursor": cursors["queue"],
                "scroll": scrolls["queue"],
                "empty_msg": "Up Next Queue is Empty",
                "empty_sub": format_footer_hint(
                    W, H,
                    hat_hint="L/R:Radio  K1:Search",
                    hat_240_hint="Switch to Radio or press KEY1 to search",
                    cardputer_hint="Press 3 for Radio or / to search",
                    hdmi_hint="Pick a Radio station (3) or Search (/) to queue tracks",
                ),
            })
            if 0 <= cursors["queue"] < len(q_items):
                preview_img = covers.get(q_items[cursors["queue"]])
        elif tab == "lists":
            items = _build_lists_view()
            open_pl = _get_playlist_by_id(open_playlist_id or "") if open_playlist_id else None
            ban = ""
            if open_playlist_id == "__liked__":
                ban = f"LIKED SONGS  •  {len(items)} tracks  •  D: Download All"
            elif open_playlist_id == "__top__":
                ban = f"MOST PLAYED  •  {len(items)} tracks"
            elif open_pl is not None:
                sel_note = f"  •  {len(selected_keys)} SEL" if selected_keys else ""
                ban = f"PLAYLIST: {open_pl['name']}  ({len(items)}){sel_note}"
            v_data.update({
                "items": items,
                "in_playlist": open_playlist_id is not None,
                "banner": ban,
                "cursor": pl_cursor if open_playlist_id else cursors["lists"],
                "scroll": pl_scroll if open_playlist_id else scrolls["lists"],
                "empty_msg": "Playlist is Empty",
                "empty_sub": "Press A on any song or offline track to add it here (Left/Esc: Back)",
            })
        elif tab == "offline":
            fld_lbl = offline_folder_filter or "ALL OFFLINE"
            srt_lbl = OFFLINE_SORT_LABELS[OFFLINE_SORT_MODES[offline_sort_idx]]
            sel_note = f"  •  {len(selected_keys)} MARKED" if selected_keys else ""
            v_data.update({
                "items": offline_cache,
                "cursor": cursors["offline"],
                "scroll": scrolls["offline"],
                "banner": f"FOLDER: {fld_lbl}  •  SORT: {srt_lbl}  ({len(offline_cache)}){sel_note}",
                "empty_msg": "No Offline Songs in This View",
                "empty_sub": "Press D on any song/playlist to download, or F to switch folder",
            })
            if 0 <= cursors["offline"] < len(offline_cache):
                preview_img = covers.get(offline_cache[cursors["offline"]])

        cov = covers.get(p_snap["track"])
        lyr = lyrics_cache.get(p_snap["track"]) if (tab == "now" and show_lyrics) else None
        cur_toast = toast if time.monotonic() < toast_until else ""
        frame = render_ytmusic_frame(
            W, H, active_tab=tab, player_state=p_snap, view_data=v_data,
            cover_img=cov, preview_img=preview_img, lyrics_data=lyr, show_lyrics=show_lyrics,
            toast=cur_toast, toast_kind=toast_kind, overlay=overlay, fonts=fonts,
        )
        lcd.LCD_ShowImage(frame, 0, 0)

    def _run_modal_menu(title: str, items: List[Tuple[str, object]]):
        if not items:
            return None
        sel = 0
        dirty = True
        flush_input()
        while running:
            if dirty:
                _render({"kind": "menu", "title": title, "items": items, "sel": sel})
                dirty = False
            act, _ch = poll_ui_input(PINS, GPIO)
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
            time.sleep(0.03)
        return None

    def _download_playlist_flow(tracks: List[dict], playlist_name: str, playlist_id: str = "") -> None:
        if not tracks:
            _set_toast("Playlist has no tracks to download", "err")
            return
        cancel_ev = threading.Event()
        prog_state = {
            "done": 0, "total": len(tracks), "title": tracks[0].get("title", ""),
            "item_frac": 0.0, "skipped": 0, "failed": 0, "finished": False, "summary": None,
        }
        prog_lock = threading.Lock()

        def _on_prog(done: int, total: int, ttl: str, frac: float, skipped: int, failed: int) -> None:
            with prog_lock:
                prog_state["done"] = done
                prog_state["total"] = total
                prog_state["title"] = ttl
                prog_state["item_frac"] = frac
                prog_state["skipped"] = skipped
                prog_state["failed"] = failed

        def _worker() -> None:
            res = ytm.download_playlist_offline(
                tracks, playlist_name=playlist_name, playlist_id=playlist_id,
                on_progress=_on_prog, cancel_event=cancel_ev,
            )
            with prog_lock:
                prog_state["summary"] = res
                prog_state["finished"] = True

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        flush_input()
        last_draw = 0.0
        while running:
            with prog_lock:
                fin = prog_state["finished"]
                done = prog_state["done"]
                total = max(1, prog_state["total"])
                ttl = prog_state["title"]
                ifrac = prog_state["item_frac"]
                sk = prog_state["skipped"]
                fl = prog_state["failed"]
            if fin:
                break
            now = time.monotonic()
            if (now - last_draw) >= 0.12:
                overall = min(1.0, (done + (ifrac if done < total else 0.0)) / total)
                _render({
                    "kind": "batch_busy",
                    "title": f"DOWNLOADING: {playlist_name[:22]}",
                    "sub": f"Track {min(done + 1, total)}/{total}: {ttl[:28]}",
                    "item_prog": ifrac,
                    "overall_prog": overall,
                    "overall_txt": f"Saved/Done: {done}/{total}  •  Skipped: {sk}  •  Errors: {fl}",
                })
                last_draw = now
            act, _ch = poll_ui_input(PINS, GPIO)
            if is_back(act):
                cancel_ev.set()
            time.sleep(0.04)

        t.join(timeout=3.0)
        _refresh_library_cache(include_offline=True, probe_dur=False)
        res = prog_state.get("summary") or {}
        if res.get("cancelled"):
            _set_toast(f"Stopped: {res.get('downloaded', 0)} saved, {res.get('skipped', 0)} already offline", "info", secs=3.5)
        else:
            _set_toast(f"Playlist Offline! {res.get('downloaded', 0)} saved, {res.get('skipped', 0)} skipped", "ok", secs=3.8)
        flush_input()

    def _create_playlist_flow(initial_tracks: Optional[List[dict]] = None, default_name: str = "", source_url: str = "") -> Optional[dict]:
        nm = lcd_keyboard(lcd, fonts["body"], PINS, GPIO, title="Playlist Name", default=default_name, charset="full", max_len=48)
        flush_input()
        if nm and nm.strip():
            pl = ytm.create_playlist(nm.strip(), initial_tracks or [], source_url=source_url)
            _refresh_library_cache()
            _set_toast(f"Created playlist: {pl['name']}", "ok")
            return pl
        return None

    def _import_playlist_flow() -> None:
        nonlocal tab_idx
        u = lcd_keyboard(lcd, fonts["body"], PINS, GPIO, title="YouTube Playlist URL", default="", charset="full", max_len=140)
        flush_input()
        if not (u and u.strip()):
            return
        url = u.strip()
        _render({"kind": "busy", "title": "Fetching Playlist Info...", "sub": url[:36]})
        pl_title, tracks, err = ytm.fetch_playlist_details(url, limit=100)
        if not tracks:
            _set_toast(err or "Import failed", "err")
            return

        # Pre-fill with the REAL YouTube playlist title so user can accept with Enter or edit!
        final_name = lcd_keyboard(lcd, fonts["body"], PINS, GPIO, title="Confirm Playlist Name", default=pl_title[:48], charset="full", max_len=48)
        flush_input()
        if final_name is None:
            return
        final_name = final_name.strip() or pl_title

        mode = _run_modal_menu(
            f"{final_name[:22]} ({len(tracks)} songs)",
            [
                ("Save Playlist (Stream Online)", "save"),
                ("Save & Download Entire Playlist Offline", "download"),
                ("Play Now (Don't Save)", "play"),
                ("Cancel", None),
            ],
        )
        if mode == "save":
            pl = ytm.create_playlist(final_name, tracks, source_url=url)
            _refresh_library_cache()
            _set_toast(f"Saved '{pl['name']}' ({len(tracks)} songs)", "ok")
        elif mode == "download":
            pl = ytm.create_playlist(final_name, tracks, source_url=url)
            _refresh_library_cache()
            _download_playlist_flow(pl["tracks"], pl["name"], playlist_id=pl["id"])
        elif mode == "play":
            player.play_tracks(tracks, 0)
            tab_idx = 0
            _set_toast(f"Playing {final_name} ({len(tracks)})", "ok")

    def _organize_tracks_flow(tracks: List[dict], allow_folder_move: bool = False) -> None:
        if not tracks:
            return
        _refresh_library_cache()
        lbl_hdr = f"ORGANIZE {len(tracks)} TRACKS" if len(tracks) > 1 else "ADD TO PLAYLIST / FOLDER"
        opts: List[Tuple[str, object]] = [
            (f"Playlist: {p['name']} ({len(p.get('tracks', []))})", ("pl", p["id"]))
            for p in playlists_cache
        ]
        opts.append(("+ Create New Playlist...", ("new_pl", None)))
        if allow_folder_move:
            opts.append(("Move File(s) to Offline Folder...", ("move_folder", None)))
        opts.append(("Cancel", None))
        picked = _run_modal_menu(lbl_hdr, opts)
        if not picked:
            return
        kind, val = picked
        if kind == "new_pl":
            pl = _create_playlist_flow(tracks)
            if pl:
                selected_keys.clear()
        elif kind == "pl":
            added = ytm.add_tracks_to_playlist(str(val), tracks)
            _refresh_library_cache()
            selected_keys.clear()
            _set_toast(f"Added {added} track(s) to playlist" if added else "Already in playlist", "ok" if added else "info")
        elif kind == "move_folder":
            folders = ytm.list_offline_folders()
            f_opts: List[Tuple[str, object]] = [("Library Root (No Folder)", "")]
            f_opts.extend([(f"Folder: {fd['name']} ({fd['count']})", fd["name"]) for fd in folders])
            f_opts.append(("+ Create New Folder...", "__new_fld__"))
            f_opts.append(("Cancel", None))
            dest_f = _run_modal_menu("MOVE TO OFFLINE FOLDER", f_opts)
            if dest_f is None:
                return
            if dest_f == "__new_fld__":
                nm = lcd_keyboard(lcd, fonts["body"], PINS, GPIO, title="New Offline Folder Name", default="", charset="full", max_len=40)
                flush_input()
                if not (nm and nm.strip()):
                    return
                dest_f = nm.strip()
            moved = 0
            for tr in tracks:
                p = tr.get("path") or ytm.find_local_path_for_track(tr)
                if p:
                    ok_m, _, _ = ytm.move_offline_track_to_folder(p, str(dest_f))
                    if ok_m:
                        moved += 1
            selected_keys.clear()
            _refresh_library_cache(include_offline=True)
            _set_toast(f"Moved {moved} file(s) -> {dest_f or 'Library Root'}", "ok" if moved else "err")

    def _playlist_actions_flow(pl: Optional[dict], special_id: str = "") -> None:
        nonlocal tab_idx, open_playlist_id
        if special_id == "__liked__":
            trks = list(liked_cache)
            act = _run_modal_menu("LIKED SONGS ACTIONS", [
                ("Play All", "play"),
                ("Shuffle Play", "shuf"),
                ("Download All Liked Songs Offline", "dl_all"),
                ("Export M3U Playlist", "m3u"),
                ("Cancel", None),
            ])
            if act == "play" and trks:
                player.play_tracks(trks, 0)
                tab_idx = 0
            elif act == "shuf" and trks:
                sh = list(trks)
                random.shuffle(sh)
                player.play_tracks(sh, 0)
                tab_idx = 0
            elif act == "dl_all":
                _download_playlist_flow(trks, "Liked Songs")
            elif act == "m3u":
                mp = ytm.export_playlist_m3u({"name": "Liked Songs", "tracks": trks})
                _set_toast("Exported Liked Songs.m3u" if mp else "No offline files to export", "ok" if mp else "err")
            return
        if pl is None:
            return
        trks = list(pl.get("tracks", []))
        opts = [
            ("Play Playlist", "play"),
            ("Shuffle Play", "shuf"),
            ("Download Entire Playlist Offline", "dl_all"),
            ("Rename Playlist", "rename"),
        ]
        if pl.get("source_url"):
            opts.append(("Sync New Tracks from YouTube URL", "sync"))
        opts.extend([
            ("Sort Tracks by Title (A-Z)", "sort_title"),
            ("Sort Tracks by Artist (A-Z)", "sort_artist"),
            ("Sort Tracks by Duration", "sort_dur"),
            ("Export M3U File", "m3u"),
            ("Delete Playlist", "delete"),
            ("Cancel", None),
        ])
        act = _run_modal_menu(f"PLAYLIST: {pl['name'][:22]}", opts)
        if not act:
            return
        if act == "play" and trks:
            player.play_tracks(trks, 0)
            tab_idx = 0
        elif act == "shuf" and trks:
            sh = list(trks)
            random.shuffle(sh)
            player.play_tracks(sh, 0)
            tab_idx = 0
        elif act == "dl_all":
            _download_playlist_flow(trks, pl["name"], playlist_id=pl["id"])
        elif act == "rename":
            nn = lcd_keyboard(lcd, fonts["body"], PINS, GPIO, title="Rename Playlist", default=pl["name"], charset="full", max_len=48)
            flush_input()
            if nn and nn.strip():
                ok_r, msg_r = ytm.rename_playlist(pl["id"], nn.strip())
                _refresh_library_cache(include_offline=True)
                _set_toast(msg_r, "ok" if ok_r else "err")
        elif act == "sync":
            _render({"kind": "busy", "title": "Syncing Playlist...", "sub": pl["name"][:30]})
            added, msg_s = ytm.sync_imported_playlist(pl["id"], limit=100)
            _refresh_library_cache()
            _set_toast(msg_s, "ok" if added >= 0 else "err")
        elif act.startswith("sort_"):
            mode = {"sort_title": "title", "sort_artist": "artist", "sort_dur": "duration"}[act]
            ytm.sort_playlist_tracks(pl["id"], mode=mode)
            _refresh_library_cache()
            _set_toast(f"Sorted by {mode}", "ok")
        elif act == "m3u":
            mp = ytm.export_playlist_m3u(pl)
            _set_toast(f"Saved {os.path.basename(mp)}" if mp else "Download tracks first to export M3U", "ok" if mp else "err")
        elif act == "delete":
            ytm.delete_playlist(pl["id"])
            if open_playlist_id == pl["id"]:
                open_playlist_id = None
            _refresh_library_cache()
            _set_toast("Deleted playlist", "info")

    def _pick_offline_folder_flow() -> None:
        nonlocal offline_folder_filter
        folders = ytm.list_offline_folders()
        opts: List[Tuple[str, object]] = [
            (("* " if offline_folder_filter is None else "  ") + "All Offline Tracks", "__ALL__")
        ]
        for fd in folders:
            sel_m = "* " if offline_folder_filter == fd["name"] else "  "
            opts.append((f"{sel_m}{fd['name']} ({fd['count']} songs)", fd["name"]))
        opts.append(("Probe All Track Durations (ffprobe)", "__PROBE__"))
        opts.append(("Cancel", None))
        picked = _run_modal_menu("OFFLINE FOLDERS", opts)
        if picked is None:
            return
        if picked == "__PROBE__":
            _render({"kind": "busy", "title": "Scanning Audio Metadata...", "sub": "Reading offline track durations"})
            _refresh_library_cache(include_offline=True, probe_dur=True)
            _set_toast("Updated offline track durations", "ok")
            return
        offline_folder_filter = None if picked == "__ALL__" else str(picked)
        selected_keys.clear()
        cursors["offline"] = 0
        scrolls["offline"] = 0
        _refresh_library_cache(include_offline=True)

    def _start_radio_from_track(track: dict) -> None:
        nonlocal tab_idx
        if track.get("local") and str(track.get("id", "")).startswith("local:"):
            _set_toast("Radio needs a YouTube track ID", "err")
            return
        _render({"kind": "busy", "title": "Building Radio Mix...", "sub": f"Similar to {track.get('title', '')[:26]}"})
        more, err = ytm.fetch_radio_mix(track["id"], limit=18)
        if err and not more:
            _set_toast(err, "err")
            return
        full_q = [track] + more
        player.play_tracks(full_q, 0)
        tab_idx = 0
        _set_toast(f"Radio Mix started ({len(full_q)} tracks)", "ok")

    def _download_flow(track: dict) -> None:
        if ytm.is_track_offline(track, _index=offline_index_cache):
            _set_toast("Already saved offline", "info")
            return
        cancel_ev = threading.Event()

        def _prog(frac: float) -> None:
            _render({"kind": "busy", "title": "Saving to Offline Library...", "sub": track.get("title", "")[:30], "progress": frac})

        _render({"kind": "busy", "title": "Saving to Offline Library...", "sub": track.get("title", "")[:30], "progress": 0.05})
        ok, msg = ytm.download_track_offline(track, on_progress=_prog, cancel_event=cancel_ev)
        _refresh_library_cache(include_offline=True)
        _set_toast(msg, "ok" if ok else "err", secs=3.5)

    def _do_search(q_raw: str) -> None:
        nonlocal search_results, search_query, search_playlist_title, tab_idx
        q = (q_raw or "").strip()
        if not q:
            return
        search_query = q
        search_playlist_title = ""
        kind, val = ytm.parse_ytmusic_input(q)
        if kind == "playlist":
            _render({"kind": "busy", "title": "Loading Playlist...", "sub": q[:32]})
            pl_title, tracks, err = ytm.fetch_playlist_details(val, limit=100)
            if err or not tracks:
                _set_toast(err or "Empty playlist", "err")
                return
            search_results = tracks
            search_playlist_title = pl_title
            cursors["search"] = 0
            scrolls["search"] = 0
            tab_idx = 1
            _set_toast(f"Loaded '{pl_title[:24]}' ({len(tracks)})", "ok")
            return
        if kind == "track":
            _render({"kind": "busy", "title": "Loading Track...", "sub": val})
            tr, err = ytm.fetch_single_track(val)
            if tr is not None:
                player.play_tracks([tr], 0)
                tab_idx = 0
                _set_toast(f"Playing: {tr.get('title', val)[:28]}", "ok")
                return
            _set_toast(err or "Track unavailable", "err")
            return
        _render({"kind": "busy", "title": "Searching YouTube Music...", "sub": q[:34]})
        tracks, err = ytm.search_music(q, max_results=18)
        if err or not tracks:
            _set_toast(err or "No songs found", "err")
            return
        search_results = tracks
        cursors["search"] = 0
        scrolls["search"] = 0
        tab_idx = 1
        _set_toast(f"Found {len(tracks)} tracks", "ok")

    def _open_audio_settings() -> None:
        nonlocal audio_label, tab_idx, open_playlist_id, show_lyrics
        while running:
            audio_label = audio_helper.get_active_audio_label()
            slp_lbl = player.get_sleep_label() or "OFF"
            opts: List[Tuple[str, object]] = []
            if player.current_track is not None:
                opts.append(("Track Controls (Next/Prev/Like/DL)...", "track_ctl"))
            opts.extend([
                (f"Switch Tab ({TAB_LABELS[TABS[tab_idx]]})...", "switch_tab"),
                (f"EQ Preset: {player.eq_name}", "eq"),
                (f"Playback Speed: {player.speed:g}x", "speed"),
                (f"Loudness Normalizer: {'ON' if player.loudnorm else 'OFF'}", "loudnorm"),
                (f"Sleep Timer: {slp_lbl}", "sleep"),
                (f"Visualizer Mode: {player.visualizer.upper()}", "viz"),
                (f"Auto-Radio Mix: {'ON' if player.cfg['auto_radio'] else 'OFF'}", "radio"),
                (f"Shuffle: {'ON' if player.cfg['shuffle'] else 'OFF'}", "shuf"),
                (f"Repeat: {player.cfg['repeat_mode'].upper()}", "rep"),
                (f"Mute: {'ON' if player.muted else 'OFF'}", "mute"),
                (f"Audio Output: {audio_label}", "sinks"),
                ("Open Audio Manager (Mixer/Profiles)", "aud_mgr"),
                ("Open Bluetooth Headphones Manager", "bt"),
                ("Back", None),
            ])
            act = _run_modal_menu("YT MUSIC AUDIO & SETTINGS", opts)
            if not act:
                return
            if act == "track_ctl":
                tr = player.current_track
                if tr is None:
                    continue
                is_lk = bool(tr.get("id") in liked_ids_cache)
                t_act = _run_modal_menu(
                    tr.get("title", "Track")[:24],
                    [
                        ("Play / Pause", "pause"),
                        ("Next Track", "next"),
                        ("Previous Track", "prev"),
                        (f"Synced Lyrics: {'ON' if show_lyrics else 'OFF'}", "lyrics"),
                        ("Remove from Liked" if is_lk else "Add to Liked Songs", "like"),
                        ("Add to Playlist / Folder...", "org"),
                        ("Download MP3 Offline", "dl"),
                        ("Start Auto-Radio Mix", "mix"),
                        ("Back", None),
                    ],
                )
                if t_act == "pause":
                    player.toggle_pause()
                elif t_act == "next":
                    player.next_track()
                elif t_act == "prev":
                    player.prev_track()
                elif t_act == "lyrics":
                    show_lyrics = not show_lyrics
                    return
                elif t_act == "like":
                    lk = ytm.toggle_like(tr)
                    _refresh_library_cache()
                    _set_toast("Added to Liked Songs" if lk else "Removed from Liked", "ok")
                elif t_act == "org":
                    _organize_tracks_flow([tr], allow_folder_move=bool(tr.get("local")))
                elif t_act == "dl":
                    _download_flow(tr)
                elif t_act == "mix":
                    _start_radio_from_track(tr)
                    return
            elif act == "switch_tab":
                t_opts = [
                    (("* " if i == tab_idx else "  ") + TAB_LABELS[tk], i)
                    for i, tk in enumerate(TABS)
                ] + [("Cancel", None)]
                picked_t = _run_modal_menu("SWITCH TAB", t_opts)
                if picked_t is not None:
                    open_playlist_id = None
                    selected_keys.clear()
                    tab_idx = int(picked_t)
                    if TABS[tab_idx] in ("lists", "offline"):
                        _refresh_library_cache(include_offline=(TABS[tab_idx] == "offline"))
                    return
            elif act == "mute":
                m_on = player.toggle_mute()
                _set_toast("Muted" if m_on else f"Unmuted ({player.volume_pct}%)", "info")
            elif act == "eq":
                nm = player.cycle_eq()
                _set_toast(f"EQ: {nm}", "ok")
            elif act == "speed":
                spd = player.cycle_speed(1)
                _set_toast(f"Playback Speed: {spd:g}x", "ok")
            elif act == "loudnorm":
                on = player.toggle_loudnorm()
                _set_toast(f"Loudness Normalizer: {'ON' if on else 'OFF'}", "ok")
            elif act == "sleep":
                picked_s = _run_modal_menu("SLEEP TIMER", [(lbl, val) for lbl, val in ytm.SLEEP_OPTIONS])
                if picked_s is not None:
                    _set_toast(player.set_sleep_timer(int(picked_s)), "ok")
            elif act == "viz":
                vz = player.cycle_visualizer()
                _set_toast(f"Visualizer: {vz.upper()}", "info")
            elif act == "radio":
                on = player.toggle_auto_radio()
                _set_toast(f"Auto-Radio: {'ON' if on else 'OFF'}", "info")
            elif act == "shuf":
                on = player.toggle_shuffle()
                _set_toast(f"Shuffle: {'ON' if on else 'OFF'}", "info")
            elif act == "rep":
                rep = player.cycle_repeat()
                _set_toast(f"Repeat: {rep.upper()}", "info")
            elif act == "sinks":
                try:
                    import rj_bluetooth
                    sinks = rj_bluetooth.list_audio_sinks()
                    if sinks:
                        picked = _run_modal_menu("AUDIO OUTPUT", [(("* " if s["is_default"] else "  ") + s["label"], s) for s in sinks] + [("Back", None)])
                        if isinstance(picked, dict):
                            ok_s, msg_s = rj_bluetooth.set_default_audio_sink(picked["name"], label=picked["label"])
                            audio_label = audio_helper.get_active_audio_label()
                            # Cleanly reopen 48kHz PCM sink on EVERY output change -> zero BT/HDMI crackling!
                            if ok_s and player.state in ("playing", "paused"):
                                player.reopen_audio_sink()
                            _set_toast(msg_s if ok_s else f"Audio error: {msg_s}", "ok" if ok_s else "err")
                    else:
                        _set_toast("No audio outputs detected (check aplay -l)", "err")
                except Exception:
                    pass
            elif act in ("bt", "aud_mgr"):
                sub_name = "bluetooth_manager.py" if act == "bt" else "audio_manager.py"
                sub_script = os.path.join(rj_env.get_base_dir(), "payloads", "utilities", sub_name)
                if os.path.isfile(sub_script):
                    subprocess.run([sys.executable, sub_script], cwd=rj_env.get_base_dir())
                    if rj_keyboard is not None:
                        rj_keyboard.set_text_mode(True)
                    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
                    audio_label = audio_helper.get_active_audio_label()
                    if player.state in ("playing", "paused"):
                        player.reopen_audio_sink()
                    flush_input()

    def _open_browse_actions(tab_nm: str, items_now: List[dict], cur_i: int, sel_it: Optional[dict], marked_fn) -> None:
        nonlocal offline_sort_idx, offline_cache, pl_cursor
        opts: List[Tuple[str, object]] = [("Search YouTube Music / URL...", "search")]
        if tab_nm == "search" and sel_it is not None:
            opts.extend([
                ("Play Next in Queue", "play_next"),
                ("Add to End of Queue", "queue"),
                ("Like / Unlike Track", "like"),
                ("Add to Playlist...", "org"),
                ("Download MP3 Offline", "dl"),
                ("Start Auto-Radio Mix", "radio"),
                ("Save Results as Playlist", "save_res"),
            ])
        elif tab_nm == "queue":
            if sel_it is not None:
                opts.extend([
                    ("Like / Unlike Track", "like"),
                    ("Add to Playlist...", "org"),
                    ("Download MP3 Offline", "dl"),
                    ("Move Track Up", "mv_up"),
                    ("Move Track Down", "mv_dn"),
                    ("Remove from Queue", "rm"),
                ])
            if player.queue:
                opts.append(("Save Queue as Playlist", "save_q"))
                opts.append(("Clear Queue", "clear_q"))
        elif tab_nm == "lists":
            if open_playlist_id is None:
                if sel_it and sel_it.get("custom"):
                    opts.append(("Playlist Actions (DL/Rename/Sync/Del)...", "pl_act"))
                    opts.append(("Download Entire Playlist Offline", "dl_pl"))
                elif sel_it and sel_it.get("id") == "__liked__":
                    opts.append(("Liked Songs Actions...", "pl_act"))
                    opts.append(("Download All Liked Offline", "dl_pl"))
                opts.append(("+ Create New Playlist", "new_pl"))
                opts.append(("+ Import YouTube Playlist URL", "imp_pl"))
            else:
                if open_playlist_id in ("__liked__",) or _get_playlist_by_id(open_playlist_id):
                    opts.append(("Playlist Actions...", "pl_act"))
                if sel_it is not None:
                    opts.extend([
                        ("Play Next in Queue", "play_next"),
                        ("Add to End of Queue", "queue"),
                        ("Add to Another Playlist...", "org"),
                        ("Download MP3 Offline", "dl"),
                    ])
                    if open_playlist_id not in ("__liked__", "__top__"):
                        opts.append(("Move Track Up", "mv_up"))
                        opts.append(("Move Track Down", "mv_dn"))
                    if open_playlist_id != "__top__":
                        opts.append(("Remove Track", "rm"))
        elif tab_nm == "offline":
            opts.append((f"Switch Folder ({offline_folder_filter or 'ALL'})...", "folder"))
            opts.append((f"Cycle Sort ({OFFLINE_SORT_LABELS[OFFLINE_SORT_MODES[offline_sort_idx]]})", "sort"))
            if sel_it is not None:
                opts.extend([
                    ("Play Next in Queue", "play_next"),
                    ("Add to End of Queue", "queue"),
                    ("Organize / Move to Folder...", "org"),
                    ("Delete Offline File", "rm"),
                ])
        opts.append(("Cancel", None))
        picked = _run_modal_menu("ACTIONS", opts)
        if not picked:
            return
        if picked == "search":
            q = lcd_keyboard(lcd, fonts["body"], PINS, GPIO, title="Search YouTube Music / URL", default=search_query, charset="full", max_len=120)
            if q:
                _do_search(q)
            flush_input()
        elif picked == "play_next" and sel_it:
            player.enqueue(sel_it, play_next=True)
            _set_toast(f"Play Next: {sel_it.get('title', '')[:24]}", "ok", secs=2.0)
        elif picked == "queue" and sel_it:
            targets = marked_fn()
            for tr_q in targets:
                player.enqueue(tr_q, play_next=False)
            selected_keys.clear()
            _set_toast(f"Queued {len(targets)} track(s)", "ok", secs=2.0)
        elif picked == "like" and sel_it:
            lk = ytm.toggle_like(sel_it)
            _refresh_library_cache()
            _set_toast("Added to Liked Songs" if lk else "Removed from Liked", "ok", secs=2.0)
        elif picked == "org" and sel_it:
            _organize_tracks_flow(marked_fn(), allow_folder_move=(tab_nm == "offline"))
        elif picked == "dl" and sel_it:
            _download_flow(sel_it)
        elif picked == "radio" and sel_it:
            _start_radio_from_track(sel_it)
        elif picked == "save_res" and search_results:
            _create_playlist_flow(search_results, default_name=search_playlist_title or search_query[:40] or "Search Mix")
        elif picked == "save_q" and player.queue:
            _create_playlist_flow(list(player.queue), default_name="Saved Queue")
        elif picked == "clear_q":
            player.clear_queue()
            _set_toast("Queue cleared", "info", secs=1.8)
        elif picked == "mv_up" and items_now:
            if tab_nm == "queue":
                cursors["queue"] = player.move_queue_item(cur_i, -1)
            elif tab_nm == "lists" and open_playlist_id not in (None, "__liked__", "__top__"):
                pl_cursor = ytm.move_playlist_track(open_playlist_id, cur_i, -1)
                _refresh_library_cache()
        elif picked == "mv_dn" and items_now:
            if tab_nm == "queue":
                cursors["queue"] = player.move_queue_item(cur_i, 1)
            elif tab_nm == "lists" and open_playlist_id not in (None, "__liked__", "__top__"):
                pl_cursor = ytm.move_playlist_track(open_playlist_id, cur_i, 1)
                _refresh_library_cache()
        elif picked == "pl_act":
            if open_playlist_id is None and sel_it:
                if sel_it.get("custom"):
                    _playlist_actions_flow(sel_it.get("playlist"))
                elif sel_it.get("id") == "__liked__":
                    _playlist_actions_flow(None, special_id="__liked__")
            elif open_playlist_id == "__liked__":
                _playlist_actions_flow(None, special_id="__liked__")
            elif open_playlist_id:
                _playlist_actions_flow(_get_playlist_by_id(open_playlist_id))
        elif picked == "dl_pl" and sel_it:
            if sel_it.get("custom") and sel_it.get("playlist"):
                pl_obj = sel_it["playlist"]
                _download_playlist_flow(pl_obj.get("tracks", []), pl_obj["name"], playlist_id=pl_obj["id"])
            elif sel_it.get("id") == "__liked__":
                _download_playlist_flow(list(liked_cache), "Liked Songs")
        elif picked == "new_pl":
            _create_playlist_flow()
        elif picked == "imp_pl":
            _import_playlist_flow()
        elif picked == "folder":
            _pick_offline_folder_flow()
        elif picked == "sort":
            offline_sort_idx = (offline_sort_idx + 1) % len(OFFLINE_SORT_MODES)
            offline_cache = _sort_offline_items(offline_cache)
            _set_toast(f"Sort: {OFFLINE_SORT_LABELS[OFFLINE_SORT_MODES[offline_sort_idx]]}", "info", secs=1.8)
        elif picked == "rm" and sel_it:
            if tab_nm == "queue":
                player.remove_from_queue(cur_i)
                _set_toast("Removed from queue", "info", secs=1.6)
            elif tab_nm == "lists" and open_playlist_id == "__liked__":
                ytm.toggle_like(sel_it)
                _refresh_library_cache()
                _set_toast("Removed from Liked Songs", "info", secs=1.8)
            elif tab_nm == "lists" and open_playlist_id not in (None, "__top__"):
                ytm.remove_track_from_playlist(open_playlist_id, cur_i)
                _refresh_library_cache()
                _set_toast("Removed from playlist", "info", secs=1.8)
            elif tab_nm == "offline":
                p = sel_it.get("path")
                if p and os.path.isfile(p):
                    try:
                        os.remove(p)
                        _refresh_library_cache(include_offline=True)
                        _set_toast("Deleted offline file", "info", secs=1.8)
                    except OSError:
                        pass

    try:
        with text_input_mode():
            flush_input()
            dirty = True
            last_render_at = 0.0
            last_mini_sec = -1
            last_p_state = ""
            toast_visible = False
            last_cov_id = 0
            last_prev_id = 0
            last_lyr_status = ""
            last_active_at = time.monotonic()

            while running:
                now = time.monotonic()
                if (now - last_audio_chk) >= 5.0:
                    new_aud = audio_helper.get_active_audio_label()
                    if new_aud != audio_label:
                        audio_label = new_aud
                        dirty = True
                    last_audio_chk = now

                cur_tr = player.current_track
                cur_tid = str(cur_tr.get("id", "")) if cur_tr else ""
                if cur_tid and cur_tid != last_track_id:
                    last_track_id = cur_tid
                    top_cache = ytm.get_most_played_tracks(limit=30)
                    dirty = True
                if player.state != last_p_state:
                    last_p_state = player.state
                    dirty = True
                if player.state == "error" and player.status_msg and player.status_msg != last_error_seen:
                    last_error_seen = player.status_msg
                    _set_toast(player.status_msg, "err", secs=4.5)
                    dirty = True
                elif player.state != "error":
                    last_error_seen = ""

                tab = TABS[tab_idx]
                has_ban = (tab == "offline") or (tab == "lists" and open_playlist_id is not None) or (tab == "search" and bool(search_playlist_title))
                lm = _layout_metrics(W, H, tab, has_ban, fonts)
                ban_extra = (lm["ban_h"] + (3 if fonts["large"] else 2)) if lm["ban_h"] > 0 else 0
                vis = max(1, (H - lm["hdr_h"] - lm["tab_h"] - lm["ftr_h"] - lm["mini_h"] - ban_extra - 9) // lm["row_h"])

                if tab == "search":
                    active_items = search_results
                elif tab == "queue":
                    active_items = list(player.queue)
                elif tab == "lists":
                    active_items = _build_lists_view()
                elif tab == "offline":
                    active_items = offline_cache
                else:
                    active_items = []

                if tab in ("search", "queue", "offline"):
                    if active_items:
                        cursors[tab] = max(0, min(cursors[tab], len(active_items) - 1))
                        if cursors[tab] < scrolls[tab]:
                            scrolls[tab] = cursors[tab]
                        elif cursors[tab] >= scrolls[tab] + vis:
                            scrolls[tab] = cursors[tab] - vis + 1
                    else:
                        cursors[tab] = 0
                        scrolls[tab] = 0
                elif tab == "lists":
                    if open_playlist_id is not None:
                        if active_items:
                            pl_cursor = max(0, min(pl_cursor, len(active_items) - 1))
                            if pl_cursor < pl_scroll:
                                pl_scroll = pl_cursor
                            elif pl_cursor >= pl_scroll + vis:
                                pl_scroll = pl_cursor - vis + 1
                        else:
                            pl_cursor = 0
                            pl_scroll = 0
                    elif active_items:
                        cursors["lists"] = max(0, min(cursors["lists"], len(active_items) - 1))
                        if cursors["lists"] < scrolls["lists"]:
                            scrolls["lists"] = cursors["lists"]
                        elif cursors["lists"] >= scrolls["lists"] + vis:
                            scrolls["lists"] = cursors["lists"] - vis + 1

                cur_toast_on = bool(toast and now < toast_until)
                if cur_toast_on != toast_visible:
                    toast_visible = cur_toast_on
                    dirty = True

                cov_id = id(covers.get(cur_tr)) if cur_tr else 0
                if cov_id != last_cov_id:
                    last_cov_id = cov_id
                    dirty = True
                if tab in ("search", "queue", "offline") and 0 <= cursors[tab] < len(active_items):
                    prev_id = id(covers.get(active_items[cursors[tab]]))
                    if prev_id != last_prev_id:
                        last_prev_id = prev_id
                        dirty = True
                if tab == "now" and show_lyrics and cur_tr:
                    ld_chk = lyrics_cache.get(cur_tr)
                    ls = str(ld_chk.get("status", "")) if ld_chk else ""
                    if ls != last_lyr_status:
                        last_lyr_status = ls
                        dirty = True

                should_render = dirty
                if not should_render:
                    if tab == "now" and player.state == "playing" and not player.muted:
                        if (now - last_render_at) >= 0.10:
                            should_render = True
                    elif player.state == "playing":
                        sec_now = int(player.get_elapsed())
                        if sec_now != last_mini_sec:
                            last_mini_sec = sec_now
                            should_render = True
                    elif player.state == "loading" and (now - last_render_at) >= 0.30:
                        should_render = True

                if should_render:
                    _render()
                    last_render_at = now
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO)
                if act is None and ch is None:
                    time.sleep(0.03 if (tab == "now" and player.state == "playing") or (now - last_active_at) < 1.5 else 0.05)
                    continue

                last_active_at = time.monotonic()
                dirty = True
                kl = ch.lower() if isinstance(ch, str) else ""

                # ---- Global controls ----
                if is_back(act):
                    if selected_keys:
                        selected_keys.clear()
                        _set_toast("Selection cleared", "info", secs=1.4)
                    elif tab == "lists" and open_playlist_id is not None:
                        open_playlist_id = None
                    else:
                        break
                elif act == "TAB":
                    open_playlist_id = None
                    selected_keys.clear()
                    tab_idx = (tab_idx + 1) % len(TABS)
                    if TABS[tab_idx] in ("lists", "offline"):
                        _refresh_library_cache(include_offline=(TABS[tab_idx] == "offline"))
                elif ch in ("1", "2", "3", "4", "5", "6"):
                    open_playlist_id = None
                    selected_keys.clear()
                    tab_idx = int(ch) - 1
                    if TABS[tab_idx] in ("lists", "offline"):
                        _refresh_library_cache(include_offline=(TABS[tab_idx] == "offline"))
                elif ch == " ":
                    if player.current_track is not None:
                        player.toggle_pause()
                    elif player.queue:
                        player.play_index(0)
                    else:
                        _set_toast("Pick a song, Playlist, or Radio station first", "info", secs=2.0)
                elif kl == "n":
                    player.next_track()
                elif kl == "b":
                    player.prev_track()
                elif is_step_up(ch):
                    v = player.set_volume(player.volume_pct + 5)
                    _set_toast(f"Volume: {v}%", "info", secs=1.6)
                elif is_step_down(ch):
                    v = player.set_volume(player.volume_pct - 5)
                    _set_toast(f"Volume: {v}%", "info", secs=1.6)
                elif kl == "m":
                    m_on = player.toggle_mute()
                    _set_toast("Muted" if m_on else f"Unmuted ({player.volume_pct}%)", "info", secs=1.6)
                elif kl == "e":
                    nm = player.cycle_eq()
                    _set_toast(f"EQ Preset: {nm}", "ok", secs=2.2)
                elif kl == "h":
                    on = player.toggle_shuffle()
                    _set_toast(f"Shuffle: {'ON' if on else 'OFF'}", "info", secs=1.8)
                elif kl == "r":
                    rep = player.cycle_repeat()
                    _set_toast(f"Repeat: {rep.upper()}", "info", secs=1.8)
                elif is_action(act, ch, "KEY2", "o"):
                    _open_audio_settings()
                elif ch == "/":
                    q = lcd_keyboard(lcd, fonts["body"], PINS, GPIO, title="Search YouTube Music / URL", default=search_query, charset="full", max_len=120)
                    if q:
                        _do_search(q)
                    flush_input()

                # ---- Tab-specific controls ----
                elif tab == "now":
                    tr = player.current_track
                    if act == "UP":
                        v = player.set_volume(player.volume_pct + 5)
                        _set_toast(f"Volume: {v}%", "info", secs=1.6)
                    elif act == "DOWN":
                        v = player.set_volume(player.volume_pct - 5)
                        _set_toast(f"Volume: {v}%", "info", secs=1.6)
                    elif act == "RIGHT":
                        if tr is not None:
                            player.seek_relative(15.0)
                            _set_toast("Seek +15s", "info", secs=1.4)
                        else:
                            tab_idx = (tab_idx + 1) % len(TABS)
                    elif act == "LEFT":
                        if tr is not None:
                            player.seek_relative(-15.0)
                            _set_toast("Seek -15s", "info", secs=1.4)
                        else:
                            tab_idx = (tab_idx - 1) % len(TABS)
                    elif ch in (",", "<"):
                        spd = player.cycle_speed(-1)
                        _set_toast(f"Speed: {spd:g}x", "ok", secs=1.8)
                    elif ch in (".", ">"):
                        spd = player.cycle_speed(1)
                        _set_toast(f"Speed: {spd:g}x", "ok", secs=1.8)
                    elif kl == "v":
                        vz = player.cycle_visualizer()
                        _set_toast(f"Visualizer: {vz.upper()}", "info", secs=1.8)
                    elif is_confirm(act):
                        if tr is not None:
                            player.toggle_pause()
                        else:
                            tab_idx = 2
                    elif is_action(act, ch, "KEY1", "y"):
                        show_lyrics = not show_lyrics
                    elif kl == "g" and tr:
                        _start_radio_from_track(tr)
                    elif kl == "l" and tr:
                        lk = ytm.toggle_like(tr)
                        _refresh_library_cache()
                        _set_toast("Added to Liked Songs" if lk else "Removed from Liked", "ok")
                    elif kl == "a" and tr:
                        _organize_tracks_flow([tr], allow_folder_move=bool(tr.get("local")))
                    elif kl == "d" and tr:
                        _download_flow(tr)

                elif tab == "radio":
                    cols = 2 if fonts["large"] else 1
                    if act == "UP":
                        cursors["radio"] = (cursors["radio"] - cols) % len(ytm.RADIO_STATIONS)
                    elif act == "DOWN":
                        cursors["radio"] = (cursors["radio"] + cols) % len(ytm.RADIO_STATIONS)
                    elif act == "LEFT":
                        if cols > 1 and (cursors["radio"] % cols) > 0:
                            cursors["radio"] -= 1
                        else:
                            tab_idx = (tab_idx - 1) % len(TABS)
                    elif act == "RIGHT":
                        if cols > 1 and (cursors["radio"] % cols) < cols - 1 and cursors["radio"] + 1 < len(ytm.RADIO_STATIONS):
                            cursors["radio"] += 1
                        else:
                            tab_idx = (tab_idx + 1) % len(TABS)
                    elif act == "KEY1":
                        q = lcd_keyboard(lcd, fonts["body"], PINS, GPIO, title="Search YouTube Music / URL", default=search_query, charset="full", max_len=120)
                        if q:
                            _do_search(q)
                        flush_input()
                    elif is_confirm(act):
                        st_name, st_query, _desc = ytm.RADIO_STATIONS[cursors["radio"]]
                        _render({"kind": "busy", "title": f"Tuning {st_name}...", "sub": "Building YouTube Music station"})
                        tracks, err = ytm.search_music(st_query, max_results=16)
                        if err or not tracks:
                            _set_toast(err or "Station unavailable", "err")
                        else:
                            player.play_tracks(tracks, 0)
                            tab_idx = 0
                            _set_toast(f"{st_name} ({len(tracks)} tracks)", "ok")
                        flush_input()

                else:
                    cur_idx = pl_cursor if (tab == "lists" and open_playlist_id is not None) else cursors[tab]
                    sel_item = active_items[cur_idx] if (0 <= cur_idx < len(active_items)) else None

                    def _marked_or_single() -> List[dict]:
                        if selected_keys:
                            out = [x for x in active_items if str(x.get("path") or x.get("id") or "") in selected_keys]
                            if out:
                                return out
                        return [sel_item] if sel_item is not None else []

                    if act == "UP" and active_items:
                        nxt = (cur_idx - 1) % len(active_items)
                        if tab == "lists" and open_playlist_id is not None:
                            pl_cursor = nxt
                        else:
                            cursors[tab] = nxt
                    elif act == "DOWN" and active_items:
                        nxt = (cur_idx + 1) % len(active_items)
                        if tab == "lists" and open_playlist_id is not None:
                            pl_cursor = nxt
                        else:
                            cursors[tab] = nxt
                    elif act == "PAGEUP" and active_items:
                        nxt = max(0, cur_idx - vis)
                        if tab == "lists" and open_playlist_id is not None:
                            pl_cursor = nxt
                        else:
                            cursors[tab] = nxt
                    elif act == "PAGEDOWN" and active_items:
                        nxt = min(len(active_items) - 1, cur_idx + vis)
                        if tab == "lists" and open_playlist_id is not None:
                            pl_cursor = nxt
                        else:
                            cursors[tab] = nxt
                    elif act == "LEFT":
                        if tab == "lists" and open_playlist_id is not None:
                            open_playlist_id = None
                            selected_keys.clear()
                        else:
                            selected_keys.clear()
                            tab_idx = (tab_idx - 1) % len(TABS)
                            if TABS[tab_idx] in ("lists", "offline"):
                                _refresh_library_cache(include_offline=(TABS[tab_idx] == "offline"))
                    elif act == "RIGHT":
                        open_playlist_id = None
                        selected_keys.clear()
                        tab_idx = (tab_idx + 1) % len(TABS)
                        if TABS[tab_idx] in ("lists", "offline"):
                            _refresh_library_cache(include_offline=(TABS[tab_idx] == "offline"))
                    elif act == "KEY1":
                        if tab == "search" and not active_items:
                            q = lcd_keyboard(lcd, fonts["body"], PINS, GPIO, title="Search YouTube Music / URL", default=search_query, charset="full", max_len=120)
                            if q:
                                _do_search(q)
                            flush_input()
                        else:
                            _open_browse_actions(tab, active_items, cur_idx, sel_item, _marked_or_single)
                    elif is_confirm(act):
                        if tab == "search":
                            if not active_items:
                                q = lcd_keyboard(lcd, fonts["body"], PINS, GPIO, title="Search YouTube Music", default="", charset="full", max_len=120)
                                if q:
                                    _do_search(q)
                                flush_input()
                            else:
                                player.play_tracks(active_items, cur_idx)
                                tab_idx = 0
                        elif tab == "queue" and active_items:
                            player.play_index(cur_idx)
                            tab_idx = 0
                        elif tab == "offline" and active_items:
                            player.play_tracks(active_items, cur_idx)
                            tab_idx = 0
                        elif tab == "lists" and sel_item is not None:
                            if open_playlist_id is None:
                                sid = sel_item.get("id")
                                if sid == "__new__":
                                    _create_playlist_flow()
                                elif sid == "__import__":
                                    _import_playlist_flow()
                                else:
                                    open_playlist_id = str(sid)
                                    pl_cursor = 0
                                    pl_scroll = 0
                                    selected_keys.clear()
                            else:
                                player.play_tracks(active_items, cur_idx)
                                tab_idx = 0
                    elif kl == "v" and sel_item and (tab == "offline" or (tab == "lists" and open_playlist_id is not None)):
                        sk = str(sel_item.get("path") or sel_item.get("id") or cur_idx)
                        if sk in selected_keys:
                            selected_keys.remove(sk)
                        else:
                            selected_keys.add(sk)
                    elif tab == "offline" and kl == "f":
                        _pick_offline_folder_flow()
                    elif tab == "offline" and kl == "s":
                        offline_sort_idx = (offline_sort_idx + 1) % len(OFFLINE_SORT_MODES)
                        offline_cache = _sort_offline_items(offline_cache)
                        _set_toast(f"Sort: {OFFLINE_SORT_LABELS[OFFLINE_SORT_MODES[offline_sort_idx]]}", "info", secs=1.8)
                    elif tab == "search" and kl == "s" and search_results:
                        def_nm = search_playlist_title or search_query[:40] or "Search Mix"
                        _create_playlist_flow(search_results, default_name=def_nm)
                    elif tab == "queue" and kl == "s" and player.queue:
                        _create_playlist_flow(list(player.queue), default_name="Saved Queue")
                    elif tab == "lists" and open_playlist_id is None and kl == "c":
                        _create_playlist_flow()
                    elif tab == "lists" and open_playlist_id is None and kl == "i":
                        _import_playlist_flow()
                    elif tab == "lists" and kl == "s":
                        if open_playlist_id is None and sel_item:
                            if sel_item.get("custom"):
                                _playlist_actions_flow(sel_item.get("playlist"))
                            elif sel_item.get("id") == "__liked__":
                                _playlist_actions_flow(None, special_id="__liked__")
                        elif open_playlist_id is not None:
                            if open_playlist_id == "__liked__":
                                _playlist_actions_flow(None, special_id="__liked__")
                            else:
                                _playlist_actions_flow(_get_playlist_by_id(open_playlist_id))
                    elif tab == "lists" and open_playlist_id is None and kl == "d" and sel_item:
                        if sel_item.get("custom") and sel_item.get("playlist"):
                            pl_obj = sel_item["playlist"]
                            _download_playlist_flow(pl_obj.get("tracks", []), pl_obj["name"], playlist_id=pl_obj["id"])
                        elif sel_item.get("id") == "__liked__":
                            _download_playlist_flow(list(liked_cache), "Liked Songs")
                        elif sel_item.get("id") == "__top__":
                            _download_playlist_flow(list(top_cache), "Most Played")
                    elif kl == "q" and sel_item and (tab in ("search", "offline") or (tab == "lists" and open_playlist_id is not None)):
                        targets = _marked_or_single()
                        for tr_q in targets:
                            player.enqueue(tr_q, play_next=False)
                        selected_keys.clear()
                        _set_toast(f"Queued {len(targets)} track(s)", "ok", secs=2.0)
                    elif kl == "w" and sel_item and (tab in ("search", "offline") or (tab == "lists" and open_playlist_id is not None)):
                        player.enqueue(sel_item, play_next=True)
                        _set_toast(f"Play Next: {sel_item.get('title', '')[:24]}", "ok", secs=2.0)
                    elif kl == "g" and sel_item and (tab in ("search", "queue") or (tab == "lists" and open_playlist_id is not None)):
                        _start_radio_from_track(sel_item)
                    elif kl == "l" and sel_item and (tab in ("search", "queue") or (tab == "lists" and open_playlist_id is not None)):
                        lk = ytm.toggle_like(sel_item)
                        _refresh_library_cache()
                        _set_toast("Added to Liked Songs" if lk else "Removed from Liked", "ok", secs=2.0)
                    elif kl == "a" and sel_item and (tab in ("search", "queue", "offline") or (tab == "lists" and open_playlist_id is not None)):
                        _organize_tracks_flow(_marked_or_single(), allow_folder_move=(tab == "offline"))
                    elif kl == "d" and sel_item and (tab in ("search", "queue") or (tab == "lists" and open_playlist_id is not None)):
                        _download_flow(sel_item)
                    elif kl == "x" or act == "DELETE":
                        if tab == "queue" and active_items:
                            player.remove_from_queue(cur_idx)
                            _set_toast("Removed from queue", "info", secs=1.6)
                        elif tab == "lists" and open_playlist_id is None and sel_item and sel_item.get("custom"):
                            ytm.delete_playlist(sel_item["id"])
                            _refresh_library_cache()
                            _set_toast("Deleted playlist", "info", secs=1.8)
                        elif tab == "lists" and open_playlist_id is not None and sel_item:
                            if open_playlist_id == "__liked__":
                                for tr_rm in _marked_or_single():
                                    ytm.toggle_like(tr_rm)
                                selected_keys.clear()
                                _refresh_library_cache()
                                _set_toast("Removed from Liked Songs", "info", secs=1.8)
                            elif open_playlist_id != "__top__":
                                if selected_keys:
                                    pls = ytm.load_playlists()
                                    for pl in pls:
                                        if pl.get("id") == open_playlist_id:
                                            pl["tracks"] = [t for t in pl.get("tracks", []) if str(t.get("path") or t.get("id") or "") not in selected_keys]
                                            ytm.save_playlists(pls)
                                            break
                                    selected_keys.clear()
                                else:
                                    ytm.remove_track_from_playlist(open_playlist_id, cur_idx)
                                _refresh_library_cache()
                                _set_toast("Removed from playlist", "info", secs=1.8)
                        elif tab == "offline" and sel_item:
                            removed_n = 0
                            for tr_del in _marked_or_single():
                                p = tr_del.get("path")
                                if p and os.path.isfile(p):
                                    try:
                                        os.remove(p)
                                        removed_n += 1
                                    except OSError:
                                        pass
                            selected_keys.clear()
                            _refresh_library_cache(include_offline=True)
                            if removed_n:
                                _set_toast(f"Deleted {removed_n} offline file(s)", "info", secs=1.8)
                    elif tab == "queue" and ch == "[" and active_items:
                        cursors["queue"] = player.move_queue_item(cur_idx, -1)
                    elif tab == "queue" and ch == "]" and active_items:
                        cursors["queue"] = player.move_queue_item(cur_idx, 1)
                    elif tab == "lists" and open_playlist_id not in (None, "__liked__", "__top__") and ch == "[" and active_items:
                        pl_cursor = ytm.move_playlist_track(open_playlist_id, cur_idx, -1)
                        _refresh_library_cache()
                    elif tab == "lists" and open_playlist_id not in (None, "__liked__", "__top__") and ch == "]" and active_items:
                        pl_cursor = ytm.move_playlist_track(open_playlist_id, cur_idx, 1)
                        _refresh_library_cache()
                    elif tab == "queue" and kl == "c":
                        player.clear_queue()
                        _set_toast("Queue cleared", "info", secs=1.8)
    finally:
        player.close()
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
