#!/usr/bin/env python3
"""
EmberJack / RaspyJack payload - Auto-Update (Obsidian Ember UI)
===============================================================
Author: 7h30th3r0n3

Checks for updates from GitHub, shows changelog, backs up configs,
pulls latest code, restores user configs, and reboots.

Controls
--------
  UP / DOWN    Select action / scroll changelog or backups
  ENTER / OK   Run selected action / restore backup
  KEY1 / U     Check & Update / continue after changelog
  KEY2 / R     Rollback to previous backup
  KEY3 / Esc   Back / Exit
"""

import os
import sys
import time
import re
import json
import signal
import subprocess
import shutil
import urllib.request
from datetime import datetime
from typing import List, Optional, Tuple

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_ansi(text):
    """Remove ANSI escape codes from text."""
    return _ANSI_RE.sub("", text)


sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

from PIL import Image, ImageDraw
import rj_theme
from payloads._input_helper import (
    format_footer_hint,
    is_action,
    is_back,
    is_confirm,
)

# ---------------------------------------------------------------------------
# Constants (update engine behavior preserved)
# ---------------------------------------------------------------------------
RASPYJACK_DIR = "/root/Raspyjack"
BACKUP_ROOT = "/root/raspyjack_backups"
SERVICE_NAME = "raspyjack"
PACKAGE_NAME = "raspyjack"
GIT_REMOTE = "origin"
GIT_BRANCH = "main"
STORE_REGISTRY_URL = "https://cardputer.cc/generated/registry.json"
STORE_REGISTRY_MAX_BYTES = 4 * 1024 * 1024
INSTALL_SCRIPT = "/root/Raspyjack/install_raspyjack.sh"

CONFIG_FILES = [
    "gui_conf.json",
    "discord_webhook.txt",
    "menu_icons.json",
]
CONFIG_DIRS = [
    "config",
]

PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}

UPDATE_STAGES = ("Fetch", "Backup", "Pull", "Restore", "Install")

_running = True
_LCD = None
_GPIO = None
_WIDTH = 128
_HEIGHT = 128
_FONTS = None


def _cleanup_signal(*_):
    global _running
    _running = False


signal.signal(signal.SIGINT, _cleanup_signal)
signal.signal(signal.SIGTERM, _cleanup_signal)


# ---------------------------------------------------------------------------
# Pure Obsidian Ember PIL Renderers (640x360, 320x170, 240x240, 128x128)
# ---------------------------------------------------------------------------

def _measure_h(draw, font, text="Ag") -> int:
    b = rj_theme.measure_text(font, text, draw)
    return max(8, b[3] - b[1])


def _wrap_px(draw, text: str, max_w: int, font) -> List[str]:
    """Pixel-aware word wrapper supporting long tokens/URLs."""
    out: List[str] = []
    max_w = max(24, int(max_w))
    for raw_line in str(text or "").splitlines():
        if not raw_line.strip():
            out.append("")
            continue
        words = raw_line.split()
        cur = ""
        for w in words:
            cand = f"{cur} {w}" if cur else w
            bw = rj_theme.measure_text(font, cand, draw)[2]
            if bw <= max_w:
                cur = cand
                continue
            if cur:
                out.append(cur)
                cur = ""
            if rj_theme.measure_text(font, w, draw)[2] <= max_w:
                cur = w
            else:
                chunk = ""
                for ch in w:
                    if chunk and rj_theme.measure_text(font, chunk + ch, draw)[2] > max_w:
                        out.append(chunk)
                        chunk = ch
                    else:
                        chunk += ch
                cur = chunk
        if cur:
            out.append(cur)
    return out or [""]


def _format_backup_name(name: str) -> str:
    if len(name) >= 15 and name[8] == "_":
        return f"{name[:4]}-{name[4:6]}-{name[6:8]}  {name[9:11]}:{name[11:13]}"
    return name


def render_home_frame(
    width: int,
    height: int,
    version: str,
    date: str,
    disk_free: str,
    backup_count: int,
    is_git: bool = True,
    selected_action: int = 0,
    remote_label: str = "origin/main",
    fonts: Optional[dict] = None,
) -> Image.Image:
    """Render the Obsidian Ember Auto-Update home dashboard."""
    ET = rj_theme.EMBER_THEME
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = bool(f["wide"])
    large = bool(f["large"])

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    badge = "GIT" if is_git else "STORE"
    hdr_h = rj_theme.draw_app_header(
        d, width, "AUTO-UPDATE", f,
        badge=badge,
        right_pill=str(version)[:12] if wide else "",
    )
    if is_git:
        hint = format_footer_hint(
            width, height,
            hat_hint="K1:Upd K2:Roll K3:X",
            hat_240_hint="OK:Run K1:Upd K2:Roll K3:Exit",
            cardputer_hint="Enter/U:Update  R:Rollback  Esc:Exit",
            hdmi_hint="Up/Down:Select  Enter/U:Update  R:Rollback  Esc:Exit",
        )
    else:
        hint = format_footer_hint(
            width, height,
            hat_hint="K1:Check K3:Exit",
            hat_240_hint="OK/K1:Check Store  K3:Exit",
            cardputer_hint="Enter/U:Check Store  Esc:Exit",
            hdmi_hint="Enter/U:Check App Store  Esc:Exit",
        )
    ftr_h = rj_theme.draw_app_footer(d, width, height, hint, f, right_text=remote_label if large and is_git else "")

    pad = 12 if large else (6 if wide else 4)
    y0 = hdr_h + pad
    y1 = height - ftr_h - pad
    avail_h = max(40, y1 - y0)

    actions: List[Tuple[str, str, str, str]] = [
        (
            "Check & Update" if is_git else "Check App Store",
            "Fetch, backup, pull & install" if is_git else "Query Cardputer registry",
            "U" if wide else "K1",
            ET["accent"],
        ),
    ]
    if is_git:
        actions.append((
            "Rollback Configs",
            f"Restore from {backup_count} saved backup(s)",
            "R" if wide else "K2",
            ET["warn"],
        ))
    actions.append((
        "Exit Updater",
        "Return to EmberJack menu",
        "ESC" if wide else "K3",
        ET["text_muted"],
    ))
    sel = max(0, min(selected_action, len(actions) - 1))

    if wide:
        # Two-column adaptive layout (640x360 HDMI and 320x170 Cardputer)
        gap = 12 if large else 6
        left_w = int((width - pad * 2 - gap) * (0.46 if large else 0.48))
        lx0, lx1 = pad, pad + left_w
        rx0, rx1 = lx1 + gap, width - pad

        # Left card: System & Repository info
        rj_theme.draw_card(
            d, (lx0, y0, lx1, y1),
            fill=ET["bg_surface"], outline=ET["border_subtle"],
            radius=8 if large else 5, accent_left=ET["accent"], accent_width=3 if large else 2,
        )
        cx = lx0 + (14 if large else 8)
        cw = (lx1 - lx0) - (22 if large else 14)
        cy = y0 + (12 if large else 6)

        sec_lbl = "SYSTEM STATUS" if large else "STATUS"
        d.text((cx, cy), sec_lbl, font=f["badge"], fill=ET["accent_bright"])
        cy += _measure_h(d, f["badge"]) + (10 if large else 5)
        d.line([(cx, cy - 3), (lx1 - (10 if large else 6), cy - 3)], fill=ET["border_subtle"], width=1)

        rows = [
            ("Version", str(version)),
            ("Date", str(date)),
            ("Disk Free", str(disk_free)),
            ("Backups", f"{backup_count} saved" if large else str(backup_count)),
        ]
        if large:
            rows.append(("Source", remote_label if is_git else "App Store (.deb)"))

        row_step = max(18, (y1 - cy - (10 if large else 4)) // max(1, len(rows)))
        for idx, (lbl, val) in enumerate(rows):
            ry = cy + idx * row_step
            if ry + 12 > y1 - 4:
                break
            d.text((cx, ry), lbl, font=f["sub"], fill=ET["text_muted"])
            val_font = f["mono"] if idx == 0 else f["body_bold"]
            val_col = ET["accent_bright"] if idx == 0 else (ET["ok"] if lbl == "Disk Free" else ET["text_primary"])
            val_s = rj_theme.truncate_text(d, val, max(40, cw - (72 if large else 52)), val_font)
            vb = rj_theme.measure_text(val_font, val_s, d)
            d.text((lx1 - (12 if large else 7) - (vb[2] - vb[0]), ry - 1), val_s, font=val_font, fill=val_col)

        # Right column: Action cards
        a_gap = 10 if large else 5
        a_h = max(28, (avail_h - a_gap * (len(actions) - 1)) // len(actions))
        for i, (title, sub, key_tag, accent) in enumerate(actions):
            ay0 = y0 + i * (a_h + a_gap)
            ay1 = min(y1, ay0 + a_h)
            active = i == sel
            rj_theme.draw_card(
                d, (rx0, ay0, rx1, ay1),
                fill=ET["select_bg"] if active else ET["bg_surface"],
                outline=ET["border_accent"] if active else ET["border_subtle"],
                radius=8 if large else 5,
                accent_left=accent if active else (ET["border_subtle"] if i == len(actions) - 1 else accent),
                accent_width=4 if (active and large) else (3 if active else 2),
            )
            ax = rx0 + (14 if large else 9)
            tag_w = rj_theme.measure_text(f["badge"], key_tag, d)[2] + (14 if large else 10)
            tag_x = rx1 - tag_w - (10 if large else 5)
            tag_y = ay0 + max(4, (ay1 - ay0 - (18 if large else 13)) // 2)
            rj_theme.draw_pill(
                d, tag_x, tag_y, key_tag, f["badge"],
                bg=ET["accent"] if active else ET["bg_well"],
                fg=ET["text_on_accent"] if active else ET["text_muted"],
                outline=ET["accent_bright"] if active else ET["border_subtle"],
                pad_x=6 if large else 4, pad_y=2 if large else 1, radius=4,
            )
            max_tw = max(30, tag_x - ax - 8)
            th = _measure_h(d, f["body_bold"])
            if large and (ay1 - ay0) >= 48:
                d.text((ax, ay0 + (ay1 - ay0) // 2 - th - 1), rj_theme.truncate_text(d, title, max_tw, f["title"]), font=f["title"], fill=ET["text_bright"] if active else ET["text_primary"])
                d.text((ax, ay0 + (ay1 - ay0) // 2 + 4), rj_theme.truncate_text(d, sub, max_tw, f["sub"]), font=f["sub"], fill=ET["accent_bright"] if active else ET["text_muted"])
            else:
                d.text((ax, ay0 + max(2, (ay1 - ay0 - th) // 2)), rj_theme.truncate_text(d, title, max_tw, f["body_bold"]), font=f["body_bold"], fill=ET["text_bright"] if active else ET["text_primary"])
    else:
        # Compact stacked layout (240x240 and 128x128 HAT)
        is_240 = width >= 200
        info_h = 84 if is_240 else 48
        rj_theme.draw_card(
            d, (pad, y0, width - pad, y0 + info_h),
            fill=ET["bg_surface"], outline=ET["border_subtle"],
            radius=5 if is_240 else 3, accent_left=ET["accent"], accent_width=2,
        )
        ix = pad + (8 if is_240 else 6)
        iw = width - pad * 2 - (14 if is_240 else 10)
        iy = y0 + (6 if is_240 else 3)
        step = 18 if is_240 else 11
        compact_rows = [
            ("Ver", str(version), ET["accent_bright"]),
            ("Date", str(date), ET["text_primary"]),
            ("Disk", f"{disk_free}  Bkp:{backup_count}", ET["text_muted"]),
        ]
        if is_240:
            compact_rows = [
                ("Version", str(version), ET["accent_bright"]),
                ("Date", str(date), ET["text_primary"]),
                ("Disk Free", str(disk_free), ET["ok"]),
                ("Backups", f"{backup_count} saved", ET["text_muted"]),
            ]
        for lbl, val, col in compact_rows:
            d.text((ix, iy), f"{lbl}:", font=f["sub"], fill=ET["text_muted"])
            lbl_w = rj_theme.measure_text(f["sub"], f"{lbl}: ", d)[2]
            d.text((ix + lbl_w, iy), rj_theme.truncate_text(d, val, max(20, iw - lbl_w), f["body_bold"] if is_240 else f["sub"]), font=f["body_bold"] if is_240 else f["sub"], fill=col)
            iy += step

        ay = y0 + info_h + (8 if is_240 else 4)
        a_gap = 6 if is_240 else 3
        if is_240 or len(actions) <= 2:
            vis_actions = list(enumerate(actions))
        else:
            st_a = max(0, min(sel - 1, len(actions) - 2))
            vis_actions = list(enumerate(actions))[st_a:st_a + 2]
        a_h = max(16, (y1 - ay - a_gap * (len(vis_actions) - 1)) // max(1, len(vis_actions)))
        for slot, (i, (title, _sub, key_tag, accent)) in enumerate(vis_actions):
            ry0 = ay + slot * (a_h + a_gap)
            ry1 = min(y1, ry0 + a_h)
            active = i == sel
            rj_theme.draw_card(
                d, (pad, ry0, width - pad, ry1),
                fill=ET["select_bg"] if active else ET["bg_surface"],
                outline=ET["border_accent"] if active else ET["border_subtle"],
                radius=4 if is_240 else 3, accent_left=accent if active else None, accent_width=2,
            )
            th = _measure_h(d, f["sub"])
            d.text((pad + 6, ry0 + max(1, (ry1 - ry0 - th) // 2)), rj_theme.truncate_text(d, f"[{key_tag}] {title}", width - pad * 2 - 12, f["body_bold"] if is_240 else f["sub"]), font=f["body_bold"] if is_240 else f["sub"], fill=ET["text_bright"] if active else ET["text_primary"])

    return img


def render_progress_frame(
    width: int,
    height: int,
    title: str,
    detail: str = "",
    progress_pct: Optional[int] = None,
    stage_idx: int = -1,
    log_lines: Optional[List[str]] = None,
    fonts: Optional[dict] = None,
) -> Image.Image:
    """Render an Obsidian Ember progress screen with optional pipeline steps & log well."""
    ET = rj_theme.EMBER_THEME
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = bool(f["wide"])
    large = bool(f["large"])

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    pct_txt = f"{int(max(0, min(100, progress_pct)))}%" if progress_pct is not None else "LIVE"
    hdr_h = rj_theme.draw_app_header(d, width, "AUTO-UPDATE", f, badge="WORKING", right_pill=pct_txt)
    ftr_h = rj_theme.draw_app_footer(
        d, width, height,
        "Please wait — do not power off device" if wide else "Please wait...",
        f,
    )

    pad = 12 if large else (6 if wide else 4)
    y = hdr_h + (10 if large else (5 if wide else 4))
    y_bot = height - ftr_h - (10 if large else (5 if wide else 4))

    # Stage stepper pills on wide displays when a stage is active
    if wide and 0 <= stage_idx < len(UPDATE_STAGES):
        sx = pad
        for idx, st_lbl in enumerate(UPDATE_STAGES):
            done = idx < stage_idx
            cur = idx == stage_idx
            tag = f"{idx + 1}.{st_lbl}" if large else st_lbl[:4].upper()
            pw, _ = rj_theme.draw_pill(
                d, sx, y, tag, f["badge"],
                bg=ET["select_bg"] if cur else (ET["bg_elevated"] if done else ET["bg_well"]),
                fg=ET["accent_bright"] if cur else (ET["ok"] if done else ET["text_muted"]),
                outline=ET["accent"] if cur else (ET["ok"] if done else ET["border_subtle"]),
                pad_x=6 if large else 4, pad_y=2 if large else 1, radius=4,
            )
            sx += pw + (8 if large else 4)
            if sx > width - pad - 30:
                break
        y += (26 if large else 18)

    # Main progress card
    rj_theme.draw_card(
        d, (pad, y, width - pad, y_bot),
        fill=ET["bg_surface"], outline=ET["border_accent"],
        radius=8 if large else 4, accent_left=ET["accent"], accent_width=4 if large else 2,
    )
    cx = pad + (16 if large else (10 if wide else 7))
    cw = width - pad * 2 - (28 if large else (18 if wide else 12))
    cy = y + (14 if large else (8 if wide else 6))

    d.text((cx, cy), rj_theme.truncate_text(d, title, cw, f["title"]), font=f["title"], fill=ET["text_bright"])
    cy += _measure_h(d, f["title"]) + (10 if large else 5)

    # Reserve bottom space for progress bar
    bar_h = 12 if large else (9 if wide else 7)
    bar_reserve = (bar_h + (22 if large else 14)) if progress_pct is not None else (8 if large else 4)
    well_bot = y_bot - bar_reserve

    # Log / detail well
    if well_bot - cy >= 24:
        rj_theme.draw_card(
            d, (cx, cy, cx + cw, well_bot),
            fill=ET["bg_well"], outline=ET["border_subtle"],
            radius=5 if large else 3,
        )
        inner_x = cx + (10 if large else 5)
        inner_w = max(24, cw - (20 if large else 10))
        inner_y = cy + (8 if large else 4)
        line_h = _measure_h(d, f["sub"]) + (5 if large else 3)
        max_rows = max(1, (well_bot - inner_y - 4) // line_h)

        display_lines: List[str] = []
        if log_lines:
            for ln in log_lines[-max_rows:]:
                display_lines.append(rj_theme.truncate_text(d, ln, inner_w, f["sub"]))
        elif detail:
            display_lines = _wrap_px(d, detail, inner_w, f["sub"])[:max_rows]

        for i, ln in enumerate(display_lines):
            is_last = (i == len(display_lines) - 1) and bool(log_lines)
            d.text(
                (inner_x, inner_y + i * line_h),
                ln,
                font=f["sub"],
                fill=ET["text_primary"] if (is_last or not log_lines) else ET["text_muted"],
            )

    if progress_pct is not None:
        pct_f = max(0.0, min(1.0, float(progress_pct) / 100.0))
        by0 = y_bot - bar_h - (10 if large else 6)
        bar_w = cw - (42 if wide else 28)
        rj_theme.draw_progress_bar(
            d, (cx, by0, cx + max(24, bar_w), by0 + bar_h),
            pct_f, fill=ET["accent"], track=ET["bg_well"], outline=ET["border_subtle"], radius=4 if wide else 2,
        )
        pct_s = f"{int(pct_f * 100)}%"
        pb = rj_theme.measure_text(f["badge"], pct_s, d)
        d.text((cx + cw - (pb[2] - pb[0]), by0 + (bar_h - (pb[3] - pb[1])) // 2 - pb[1]), pct_s, font=f["badge"], fill=ET["accent_bright"])

    return img


def render_changelog_frame(
    width: int,
    height: int,
    commits: List[str],
    scroll_pos: int = 0,
    fonts: Optional[dict] = None,
) -> Image.Image:
    """Render scrollable Obsidian Ember changelog screen."""
    ET = rj_theme.EMBER_THEME
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = bool(f["wide"])
    large = bool(f["large"])

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    hdr_h = rj_theme.draw_app_header(
        d, width, "CHANGELOG", f,
        badge=f"{len(commits)} NEW",
        right_text="Ready to install" if large else "",
    )
    hint = format_footer_hint(
        width, height,
        hat_hint="K1:Install K3:Skip",
        hat_240_hint="OK/K1:Install  U/D:Scr  K3:Skip",
        cardputer_hint="Enter/U:Install  U/D:Scroll  Esc:Skip",
        hdmi_hint="Enter/U:Install & Reboot  Up/Down:Scroll  Esc:Skip",
    )
    ftr_h = rj_theme.draw_app_footer(d, width, height, hint, f)

    pad = 10 if large else (6 if wide else 3)
    y0 = hdr_h + (8 if large else 4)
    y1 = height - ftr_h - (8 if large else 4)
    sb_w = 8 if wide else 4
    row_h = 34 if large else (24 if wide else 15)
    row_gap = 5 if large else (3 if wide else 2)
    vis = max(1, (y1 - y0 + row_gap) // (row_h + row_gap))
    max_scroll = max(0, len(commits) - vis)
    sp = max(0, min(scroll_pos, max_scroll))

    list_x1 = width - pad - (sb_w + 4 if len(commits) > vis else 0)

    for i, line in enumerate(commits[sp:sp + vis]):
        ry0 = y0 + i * (row_h + row_gap)
        ry1 = min(y1, ry0 + row_h)
        rj_theme.draw_card(
            d, (pad, ry0, list_x1, ry1),
            fill=ET["bg_surface"], outline=ET["border_subtle"],
            radius=6 if large else 3, accent_left=ET["ok"] if i == 0 and sp == 0 else ET["accent"], accent_width=3 if large else 2,
        )
        parts = line.split(" ", 1)
        tx = pad + (10 if large else 6)
        if len(parts) == 2:
            shash, msg = parts[0][:7], parts[1].strip()
            pw, ph = rj_theme.draw_pill(
                d, tx, ry0 + max(2, (ry1 - ry0 - (18 if large else 11)) // 2),
                shash, f["badge"],
                bg=ET["bg_well"], fg=ET["accent_bright"], outline=ET["border_accent"],
                pad_x=5 if large else 3, pad_y=2 if large else 1, radius=3,
            )
            mx = tx + pw + (8 if large else 4)
            mh = _measure_h(d, f["body"] if wide else f["sub"])
            d.text(
                (mx, ry0 + max(2, (ry1 - ry0 - mh) // 2)),
                rj_theme.truncate_text(d, msg, max(24, list_x1 - mx - 6), f["body"] if wide else f["sub"]),
                font=f["body"] if wide else f["sub"],
                fill=ET["text_primary"],
            )
        else:
            mh = _measure_h(d, f["body"] if wide else f["sub"])
            d.text(
                (tx, ry0 + max(2, (ry1 - ry0 - mh) // 2)),
                rj_theme.truncate_text(d, line, max(24, list_x1 - tx - 6), f["body"] if wide else f["sub"]),
                font=f["body"] if wide else f["sub"],
                fill=ET["text_primary"],
            )

    if len(commits) > vis:
        rj_theme.draw_scrollbar(d, width - pad - sb_w + 2, y0, y1, sp, vis, len(commits), width=4 if wide else 2)

    return img


def render_rollback_frame(
    width: int,
    height: int,
    backups: List[str],
    sel: int = 0,
    fonts: Optional[dict] = None,
) -> Image.Image:
    """Render Obsidian Ember config backup rollback selector."""
    ET = rj_theme.EMBER_THEME
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = bool(f["wide"])
    large = bool(f["large"])

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    hdr_h = rj_theme.draw_app_header(
        d, width, "ROLLBACK", f,
        badge=f"{len(backups)} SAVED",
        right_text="Configs & custom payloads" if large else "",
    )
    hint = format_footer_hint(
        width, height,
        hat_hint="OK:Rest K3:Bk",
        hat_240_hint="OK:Restore  U/D:Sel  K3:Back",
        cardputer_hint="Enter:Restore  U/D:Sel  Esc:Back",
        hdmi_hint="Enter:Restore & Restart  Up/Down:Select  Esc:Back",
    )
    ftr_h = rj_theme.draw_app_footer(d, width, height, hint, f)

    pad = 10 if large else (6 if wide else 4)
    y0 = hdr_h + (8 if large else 4)
    y1 = height - ftr_h - (8 if large else 4)

    if not backups:
        empty_foot = format_footer_hint(
            width, height,
            hat_hint="K3:Back",
            hat_240_hint="K3: Back",
            cardputer_hint="Esc: Back",
            hdmi_hint="Press Esc to go back",
        )
        rj_theme.draw_status_card(
            d, width, height,
            "No Backups Found",
            sub="Backups are saved before each update" if wide else "Run an update first",
            kind="warn",
            foot=empty_foot,
            fonts=f,
        )
        return img

    sb_w = 8 if wide else 4
    row_h = 42 if large else (26 if wide else 18)
    row_gap = 6 if large else (3 if wide else 2)
    vis = max(1, (y1 - y0 + row_gap) // (row_h + row_gap))
    sel = max(0, min(sel, len(backups) - 1))
    start = max(0, min(sel - vis + 1, max(0, len(backups) - vis))) if sel >= vis else 0
    list_x1 = width - pad - (sb_w + 4 if len(backups) > vis else 0)

    for r_i, name in enumerate(backups[start:start + vis]):
        idx = start + r_i
        active = idx == sel
        ry0 = y0 + r_i * (row_h + row_gap)
        ry1 = min(y1, ry0 + row_h)
        rj_theme.draw_card(
            d, (pad, ry0, list_x1, ry1),
            fill=ET["select_bg"] if active else ET["bg_surface"],
            outline=ET["warn"] if active else ET["border_subtle"],
            radius=6 if large else 3,
            accent_left=ET["warn"] if active else ET["border_subtle"],
            accent_width=4 if (active and large) else 2,
        )
        tx = pad + (12 if large else 7)
        label = _format_backup_name(name)
        if large:
            d.text((tx, ry0 + 6), rj_theme.truncate_text(d, label, list_x1 - tx - 90, f["body_bold"]), font=f["body_bold"], fill=ET["text_bright"] if active else ET["text_primary"])
            d.text((tx, ry0 + 23), "Restores gui_conf, config/ & custom payloads", font=f["sub"], fill=ET["accent_bright"] if active else ET["text_muted"])
            if idx == 0:
                pw = rj_theme.measure_text(f["badge"], "LATEST", d)[2] + 10
                rj_theme.draw_pill(
                    d, list_x1 - pw - 8, ry0 + (row_h - 16) // 2, "LATEST", f["badge"],
                    bg=ET["bg_well"], fg=ET["ok"], outline=ET["ok"], pad_x=5, pad_y=1, radius=3,
                )
        else:
            mh = _measure_h(d, f["body_bold"] if wide else f["sub"])
            d.text(
                (tx, ry0 + max(1, (ry1 - ry0 - mh) // 2)),
                rj_theme.truncate_text(d, ("> " if active else "  ") + label, list_x1 - tx - 6, f["body_bold"] if wide else f["sub"]),
                font=f["body_bold"] if wide else f["sub"],
                fill=ET["text_bright"] if active else ET["text_primary"],
            )

    if len(backups) > vis:
        rj_theme.draw_scrollbar(d, width - pad - sb_w + 2, y0, y1, start, vis, len(backups), width=4 if wide else 2)

    return img


def render_message_frame(
    width: int,
    height: int,
    lines,
    kind: str = "info",
    foot: str = "",
    fonts: Optional[dict] = None,
) -> Image.Image:
    """Render a centered Obsidian Ember status / notification card."""
    ET = rj_theme.EMBER_THEME
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = bool(f["wide"])
    large = bool(f["large"])

    if isinstance(lines, str):
        raw_lines = [l.strip() for l in lines.splitlines() if l.strip()]
    else:
        raw_lines = [str(l).strip() for l in lines if str(l).strip()]
    if not raw_lines:
        raw_lines = ["Status"]

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    badge = "OK" if kind == "ok" else ("WARN" if kind == "warn" else ("ERROR" if kind == "err" else "INFO"))
    hdr_h = rj_theme.draw_app_header(d, width, "AUTO-UPDATE", f, badge=badge)
    default_foot = format_footer_hint(
        width, height,
        hat_hint="OK/K3:Cont",
        hat_240_hint="OK / K3: Continue",
        cardputer_hint="Enter / Esc: Continue",
        hdmi_hint="Press Enter or Esc to continue",
    )
    ftr_h = rj_theme.draw_app_footer(d, width, height, foot or default_foot, f)

    accent = (
        ET["ok"] if kind == "ok"
        else ET["warn"] if kind == "warn"
        else ET["danger"] if kind == "err"
        else ET["accent"]
    )
    cw = min(width - (24 if large else 10), max(112, int(width * (0.74 if wide else 0.90))))
    ch = min(height - hdr_h - ftr_h - (20 if large else 10), max(64, int((height - hdr_h - ftr_h) * 0.78)))
    cx0 = (width - cw) // 2
    cy0 = hdr_h + ((height - hdr_h - ftr_h) - ch) // 2

    rj_theme.draw_card(
        d, (cx0, cy0, cx0 + cw, cy0 + ch),
        fill=ET["bg_surface"], outline=accent,
        radius=8 if large else 4, accent_left=accent, accent_width=4 if large else 2,
    )

    tx = cx0 + (18 if large else 10)
    tw = cw - (30 if large else 16)
    ty = cy0 + (16 if large else 8)

    d.text((tx, ty), rj_theme.truncate_text(d, raw_lines[0], tw, f["title"]), font=f["title"], fill=ET["accent_bright"] if kind == "info" else accent)
    ty += _measure_h(d, f["title"]) + (10 if large else 6)

    line_h = _measure_h(d, f["body"]) + (6 if large else 3)
    for ln in raw_lines[1:]:
        for wrapped in _wrap_px(d, ln, tw, f["body"]):
            if ty + line_h > cy0 + ch - (10 if large else 4):
                break
            d.text((tx, ty), wrapped, font=f["body"], fill=ET["text_primary"])
            ty += line_h

    return img


def render_error_frame(
    width: int,
    height: int,
    title: str,
    detail: str,
    scroll: int = 0,
    fonts: Optional[dict] = None,
) -> Tuple[Image.Image, int]:
    """Render scrollable Obsidian Ember error card and return (img, max_scroll)."""
    ET = rj_theme.EMBER_THEME
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = bool(f["wide"])
    large = bool(f["large"])

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    hdr_h = rj_theme.draw_app_header(d, width, title[:22], f, badge="ERROR")

    pad = 12 if large else (6 if wide else 4)
    y0 = hdr_h + (8 if large else 4)
    ftr_h = 22 if large else (18 if wide else 13)
    y1 = height - ftr_h - (8 if large else 4)

    rj_theme.draw_card(
        d, (pad, y0, width - pad, y1),
        fill=ET["bg_surface"], outline=ET["danger"],
        radius=8 if large else 4, accent_left=ET["danger"], accent_width=4 if large else 2,
    )

    tx = pad + (14 if large else 8)
    sb_w = 8 if wide else 4
    tw = width - pad * 2 - (28 if large else 16) - sb_w
    lines = _wrap_px(d, detail, tw, f["body"] if wide else f["sub"])
    body_f = f["body"] if wide else f["sub"]
    line_h = _measure_h(d, body_f) + (6 if large else 3)
    inner_y0 = y0 + (12 if large else 6)
    vis = max(1, (y1 - inner_y0 - (8 if large else 4)) // line_h)
    max_scroll = max(0, len(lines) - vis)
    sp = max(0, min(scroll, max_scroll))

    for i, ln in enumerate(lines[sp:sp + vis]):
        d.text((tx, inner_y0 + i * line_h), ln, font=body_f, fill=ET["text_primary"])

    if len(lines) > vis:
        rj_theme.draw_scrollbar(d, width - pad - sb_w - 2, inner_y0, y1 - 6, sp, vis, len(lines), width=4 if wide else 2)

    if len(lines) > vis:
        hint = format_footer_hint(
            width, height,
            hat_hint="U/D:Scr OK/K3:Cont",
            hat_240_hint="U/D:Scroll  OK/K3:Continue",
            cardputer_hint="U/D:Scroll  Enter/Esc:Continue",
            hdmi_hint="Up/Down:Scroll  Enter/Esc:Continue",
        )
    else:
        hint = format_footer_hint(
            width, height,
            hat_hint="OK/K3:Cont",
            hat_240_hint="OK / K3: Continue",
            cardputer_hint="Enter / Esc: Continue",
            hdmi_hint="Enter / Esc: Continue",
        )
    rj_theme.draw_app_footer(d, width, height, hint, f)
    return img, max_scroll


# ---------------------------------------------------------------------------
# Display helper wrappers (using active LCD when running on device)
# ---------------------------------------------------------------------------

def _push_frame(img: Image.Image) -> None:
    if _LCD is not None:
        _LCD.LCD_ShowImage(img, 0, 0)


def _poll_nav() -> Optional[str]:
    """Read unified keyboard + button input and normalize to updater actions."""
    from payloads._input_helper import poll_ui_input
    act, ch = poll_ui_input(PINS, _GPIO)
    if act is None and ch is None:
        return None
    if is_confirm(act):
        return "OK"
    if is_back(act):
        return "KEY3"
    if is_action(act, ch, "KEY1", "u"):
        return "KEY1"
    if is_action(act, ch, "KEY2", "r"):
        return "KEY2"
    if act in ("UP", "DOWN", "LEFT", "RIGHT", "PAGEUP", "PAGEDOWN"):
        return act
    return None


def show(lines, invert=False, spacing=2, kind=None):
    """Show centered status card on LCD."""
    if kind is None:
        kind = "ok" if invert else "info"
    img = render_message_frame(_WIDTH, _HEIGHT, lines, kind=kind, fonts=_FONTS)
    _push_frame(img)


def show_error(title, detail):
    """Show scrollable error screen with word-wrapped detail text."""
    scroll = 0
    dirty = True
    max_scroll = 0
    while _running:
        if dirty:
            img, max_scroll = render_error_frame(_WIDTH, _HEIGHT, title, detail, scroll=scroll, fonts=_FONTS)
            _push_frame(img)
            dirty = False
        btn = _poll_nav()
        if btn == "UP":
            if scroll > 0:
                scroll -= 1
                dirty = True
        elif btn == "DOWN":
            if scroll < max_scroll:
                scroll += 1
                dirty = True
        elif btn == "PAGEUP":
            ns = max(0, scroll - 5)
            if ns != scroll:
                scroll = ns
                dirty = True
        elif btn == "PAGEDOWN":
            ns = min(max_scroll, scroll + 5)
            if ns != scroll:
                scroll = ns
                dirty = True
        elif btn in ("OK", "KEY3"):
            break
        time.sleep(0.03)


def show_progress(title, detail="", progress_pct=None, stage_idx=-1, log_lines=None):
    """Show Obsidian Ember progress screen with word-wrapped detail and bar at bottom."""
    img = render_progress_frame(
        _WIDTH, _HEIGHT, title, detail=detail,
        progress_pct=progress_pct, stage_idx=stage_idx,
        log_lines=log_lines, fonts=_FONTS,
    )
    _push_frame(img)


def draw_home(version, date, disk_free, backup_count, selected_action=0):
    img = render_home_frame(
        _WIDTH, _HEIGHT, version, date, disk_free, backup_count,
        is_git=is_git_checkout(), selected_action=selected_action,
        remote_label=f"{GIT_REMOTE}/{GIT_BRANCH}", fonts=_FONTS,
    )
    _push_frame(img)


def draw_changelog(commits, scroll_pos):
    img = render_changelog_frame(_WIDTH, _HEIGHT, commits, scroll_pos=scroll_pos, fonts=_FONTS)
    _push_frame(img)


def draw_rollback(backups, sel):
    img = render_rollback_frame(_WIDTH, _HEIGHT, backups, sel=sel, fonts=_FONTS)
    _push_frame(img)


# ---------------------------------------------------------------------------
# Git / Store helpers (update mechanics unchanged)
# ---------------------------------------------------------------------------

def _git(args, timeout=30):
    """Run a git command in RASPYJACK_DIR."""
    try:
        r = subprocess.run(
            ["git", "-C", RASPYJACK_DIR] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return False, "", str(e)


def is_git_checkout():
    # Checked on disk, not via git (a failing git call must not turn a clone into a
    # package install); a leftover .git under a dpkg-owned install does not count.
    return (
        os.path.exists(os.path.join(RASPYJACK_DIR, ".git"))
        and get_installed_package_version() is None
    )


def get_installed_package_version():
    try:
        r = subprocess.run(
            ["dpkg-query", "-W", "-f=${db:Status-Abbrev} ${Version}", PACKAGE_NAME],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    status, _, version = r.stdout.strip().partition(" ")
    return version.strip() if r.returncode == 0 and status.startswith("ii") and version.strip() else None


def fetch_store_registry():
    try:
        req = urllib.request.Request(
            STORE_REGISTRY_URL, headers={"User-Agent": "raspyjack-auto-update"}
        )
        with urllib.request.urlopen(req, timeout=20) as response:
            registry = json.loads(response.read(STORE_REGISTRY_MAX_BYTES))
    except Exception:
        return None
    return registry if isinstance(registry, dict) else None


def find_store_version(registry):
    apps = registry.get("apps")
    if not isinstance(apps, list):
        return None
    for app in apps:
        download = app.get("download") if isinstance(app, dict) else None
        if isinstance(download, dict) and download.get("package") == PACKAGE_NAME:
            return app.get("version")
    return None


def is_store_version_newer(store_version, installed_version):
    """Return True/False, or None when dpkg can't compare the versions."""
    try:
        r = subprocess.run(
            ["dpkg", "--compare-versions", store_version, "gt", installed_version],
            timeout=10,
        )
    except Exception:
        return None
    if r.returncode in (0, 1):
        return r.returncode == 0
    return None


def check_store_update():
    installed = get_installed_package_version()
    if not installed:
        return "Package not\ninstalled", False
    registry = fetch_store_registry()
    if registry is None:
        return "Can't reach\nApp Store registry", False
    store_version = find_store_version(registry)
    if not store_version:
        return "Not listed in\nthe App Store", False
    newer = is_store_version_newer(store_version, installed)
    if newer is None:
        return "Can't compare\nversions", False
    if newer:
        return f"New: {store_version}\nUpdate in App Store\nSearch: RaspyJack\nCode: RJAK", True
    return f"Already up to date!\n{installed}", False


def get_current_version():
    """Return (short_hash, date, full_hash)."""
    if not is_git_checkout():
        package_version = get_installed_package_version()
        return package_version or "?", "package (.deb)" if package_version else "no git, no package", ""
    ok, h, _ = _git(["rev-parse", "--short", "HEAD"])
    short = h if ok else "?"
    ok, fh, _ = _git(["rev-parse", "HEAD"])
    full = fh if ok else ""
    ok, d, _ = _git(["log", "-1", "--format=%cd", "--date=short"])
    date = d if ok else "?"
    return short, date, full


def check_update_available():
    """Fetch remote and check if updates exist. Returns (available, info, count)."""
    if not is_git_checkout():
        show_progress("Checking...", "Querying App Store registry", progress_pct=20)
        info, newer_available = check_store_update()
        return False, info, 1 if newer_available else 0
    show_progress("Checking for updates...", f"Fetching {GIT_REMOTE}/{GIT_BRANCH}", progress_pct=15, stage_idx=0)
    ok, _, err = _git(["fetch", GIT_REMOTE], timeout=60)
    if not ok:
        return False, f"Fetch failed:\n{' '.join(err.split())[:60]}", 0

    ok, local, _ = _git(["rev-parse", "HEAD"])
    ok2, remote, _ = _git(["rev-parse", f"{GIT_REMOTE}/{GIT_BRANCH}"])
    if not ok or not ok2:
        return False, "Can't read hashes", 0

    if local == remote:
        return False, "Already up to date!", 0

    ok, log, _ = _git(["log", "--oneline", f"HEAD..{GIT_REMOTE}/{GIT_BRANCH}"])
    lines = [l for l in log.split("\n") if l.strip()] if log else []
    return True, f"{len(lines)} new commit(s)", len(lines)


def get_changelog(old_hash):
    """Return list of commit messages since old_hash."""
    ok, log, _ = _git(["log", "--oneline", f"{old_hash}..HEAD"])
    if not ok or not log:
        return []
    return [l.strip() for l in log.split("\n") if l.strip()][:10]


# ---------------------------------------------------------------------------
# Backup (lightweight, targeted — unchanged)
# ---------------------------------------------------------------------------

def _count_backups():
    if not os.path.isdir(BACKUP_ROOT):
        return 0
    return len([d for d in os.listdir(BACKUP_ROOT) if os.path.isdir(os.path.join(BACKUP_ROOT, d))])


def smart_backup():
    """Backup only config files and custom payloads. Returns (ok, backup_path)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(BACKUP_ROOT, ts)

    try:
        os.makedirs(backup_dir, exist_ok=True)

        # Backup config files
        for fname in CONFIG_FILES:
            src = os.path.join(RASPYJACK_DIR, fname)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(backup_dir, fname))

        # Backup config directories
        for dname in CONFIG_DIRS:
            src = os.path.join(RASPYJACK_DIR, dname)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(backup_dir, dname))

        # Backup custom payloads (untracked by git)
        ok, untracked, _ = _git(["ls-files", "--others", "--exclude-standard", "payloads/"])
        if ok and untracked:
            custom_dir = os.path.join(backup_dir, "custom_payloads")
            os.makedirs(custom_dir, exist_ok=True)
            for relpath in untracked.split("\n"):
                relpath = relpath.strip()
                if not relpath:
                    continue
                src = os.path.join(RASPYJACK_DIR, relpath)
                dst = os.path.join(custom_dir, os.path.basename(relpath))
                if os.path.isfile(src):
                    shutil.copy2(src, dst)

        return True, backup_dir
    except Exception as e:
        return False, str(e)


def restore_configs(backup_dir):
    """Restore config files from a backup directory."""
    restored = 0
    try:
        # Restore config files
        for fname in CONFIG_FILES:
            src = os.path.join(backup_dir, fname)
            dst = os.path.join(RASPYJACK_DIR, fname)
            if os.path.isfile(src):
                shutil.copy2(src, dst)
                restored += 1

        # Restore config directories
        for dname in CONFIG_DIRS:
            src = os.path.join(backup_dir, dname)
            dst = os.path.join(RASPYJACK_DIR, dname)
            if os.path.isdir(src):
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
                restored += 1

        # Restore custom payloads
        custom_dir = os.path.join(backup_dir, "custom_payloads")
        if os.path.isdir(custom_dir):
            payloads_dir = os.path.join(RASPYJACK_DIR, "payloads")
            for fname in os.listdir(custom_dir):
                src = os.path.join(custom_dir, fname)
                dst = os.path.join(payloads_dir, fname)
                if os.path.isfile(src) and not os.path.isfile(dst):
                    shutil.copy2(src, dst)
                    restored += 1

        return True, f"{restored} items restored"
    except Exception as e:
        return False, str(e)


def cleanup_old_backups(keep=3):
    """Keep only the N most recent backups."""
    if not os.path.isdir(BACKUP_ROOT):
        return
    dirs = sorted([
        d for d in os.listdir(BACKUP_ROOT)
        if os.path.isdir(os.path.join(BACKUP_ROOT, d))
    ])
    while len(dirs) > keep:
        old = dirs.pop(0)
        try:
            shutil.rmtree(os.path.join(BACKUP_ROOT, old))
        except Exception:
            pass


def list_backups():
    """Return sorted list of backup directory names."""
    if not os.path.isdir(BACKUP_ROOT):
        return []
    return sorted([
        d for d in os.listdir(BACKUP_ROOT)
        if os.path.isdir(os.path.join(BACKUP_ROOT, d))
    ], reverse=True)


# ---------------------------------------------------------------------------
# Update + install (unchanged)
# ---------------------------------------------------------------------------

def git_update():
    """Hard reset to latest remote."""
    ok, _, err = _git(["reset", "--hard", f"{GIT_REMOTE}/{GIT_BRANCH}"])
    if not ok:
        return False, err[:80]
    return True, "OK"


def run_install_script():
    """Run install script with live progress on LCD."""
    if not os.path.isfile(INSTALL_SCRIPT):
        return True, "no script"

    # Detect current display type to auto-answer the interactive prompt
    display_choice = "1"  # default ST7735_128
    try:
        with open(os.path.join(RASPYJACK_DIR, "gui_conf.json"), "r") as f:
            cfg = json.load(f)
        dtype = cfg.get("DISPLAY", {}).get("type", "")
        if dtype == "ST7789_240":
            display_choice = "2"
        elif dtype == "CARDPUTER_320":
            display_choice = "3"
        elif dtype == "HDMI":
            display_choice = "4"
    except Exception:
        pass

    try:
        import select
        INSTALL_TIMEOUT = 180  # 3 minutes max per readline
        proc = subprocess.Popen(
            ["bash", INSTALL_SCRIPT, "--update"],
            cwd=RASPYJACK_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            proc.stdin.write(display_choice + "\n")
            proc.stdin.flush()
        except Exception:
            pass
        line_count = 0
        last_line = ""
        recent_lines: List[str] = []
        start = time.monotonic()
        while True:
            if time.monotonic() - start > 900:
                proc.kill()
                return False, "Timeout (15min)"
            ready, _, _ = select.select([proc.stdout], [], [], INSTALL_TIMEOUT)
            if not ready:
                show_progress("Installing update...", "Still working...", min(90, line_count * 2), stage_idx=4, log_lines=recent_lines)
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            clean = _strip_ansi(line.strip())
            if clean:
                line_count += 1
                last_line = clean
                recent_lines.append(clean)
                if len(recent_lines) > 12:
                    recent_lines.pop(0)
                show_progress("Installing update...", clean, min(95, line_count * 2), stage_idx=4, log_lines=recent_lines)

        rc = proc.wait(timeout=30)
        if rc != 0:
            return False, f"Exit code {rc}: {last_line}"
        return True, "OK"
    except Exception as e:
        return False, str(e)[:80]


def restart_service():
    try:
        subprocess.run(["systemctl", "restart", SERVICE_NAME], check=True, timeout=10)
        return True, "OK"
    except Exception as e:
        return False, str(e)


def do_reboot():
    subprocess.run(["sync"], check=False)
    subprocess.run(["systemctl", "reboot"], check=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _running, _LCD, _GPIO, _WIDTH, _HEIGHT, _FONTS

    import RPi.GPIO as GPIO
    import LCD_1in44
    import LCD_Config  # noqa: F401
    from payloads._input_helper import flush_input, text_input_mode

    _GPIO = GPIO
    GPIO.setmode(GPIO.BCM)
    for p in PINS.values():
        GPIO.setup(p, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    _LCD = LCD_1in44.LCD()
    _LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    _LCD.LCD_Clear()
    _WIDTH, _HEIGHT = _LCD.width, _LCD.height
    _FONTS = rj_theme.load_app_fonts(_WIDTH, _HEIGHT)

    def _refresh_meta():
        ver, dt, fhash = get_current_version()
        try:
            usage = shutil.disk_usage(RASPYJACK_DIR)
            dfree = f"{usage.free // (1024 * 1024)} MB"
        except Exception:
            dfree = "?"
        return ver, dt, fhash, dfree, _count_backups()

    version, date, full_hash, disk_free, backup_count = _refresh_meta()
    view = "home"
    sel_action = 0
    changelog: List[str] = []
    changelog_scroll = 0
    rollback_sel = 0
    backups: List[str] = []
    should_reboot = False
    dirty = True

    def _trigger_update():
        nonlocal version, date, full_hash, disk_free, backup_count
        nonlocal view, changelog, changelog_scroll, should_reboot, dirty

        available, info, count = check_update_available()
        if not available:
            kind = "ok" if "up to date" in info.lower() else ("warn" if count > 0 else "info")
            show(info.split("\n"), kind=kind)
            if is_git_checkout():
                t_end = time.monotonic() + 2.5
                while _running and time.monotonic() < t_end:
                    if _poll_nav() is not None:
                        break
                    time.sleep(0.05)
            else:
                time.sleep(0.3)
                while _running and _poll_nav() is None:
                    time.sleep(0.05)
            version, date, full_hash, disk_free, backup_count = _refresh_meta()
            dirty = True
            return False

        show([f"{count} Update(s) Available!", "Backing up user configs..."], kind="info")
        time.sleep(0.9)

        # 2. Smart backup
        show_progress("Backing up...", "Saving gui_conf, config/ & custom payloads", progress_pct=30, stage_idx=1)
        ok, backup_path = smart_backup()
        if not ok:
            show_error("BACKUP FAILED", backup_path)
            dirty = True
            return False

        # 3. Check disk space
        try:
            usage = shutil.disk_usage(RASPYJACK_DIR)
            if usage.free < 100 * 1024 * 1024:
                show_error("LOW DISK SPACE", f"Only {usage.free // (1024 * 1024)}MB free, need 100MB minimum")
                dirty = True
                return False
        except Exception:
            pass

        # 4. Git pull
        old_hash = full_hash
        show_progress("Updating repository...", f"git reset --hard {GIT_REMOTE}/{GIT_BRANCH}", progress_pct=55, stage_idx=2)
        ok, info = git_update()
        if not ok:
            show_error("UPDATE FAILED", info)
            dirty = True
            return False

        # 5. Restore configs
        show_progress("Restoring configs...", "Restoring user configs & custom payloads", progress_pct=75, stage_idx=3)
        restore_configs(backup_path)

        # 6. Cleanup old backups
        cleanup_old_backups(keep=3)

        # 7. Show changelog
        changelog = get_changelog(old_hash)
        if changelog:
            changelog_scroll = 0
            view = "changelog"
            dirty = True
            return False

        # No changelog, go straight to install
        show_progress("Installing update...", "Running install_raspyjack.sh --update", progress_pct=80, stage_idx=4)
        ok, info = run_install_script()
        if not ok:
            show_error("INSTALL FAILED", info)
            version, date, full_hash, disk_free, backup_count = _refresh_meta()
            dirty = True
            return False

        show(["Update Complete!", "Rebooting system..."], kind="ok")
        time.sleep(2)
        should_reboot = True
        return True

    try:
        with text_input_mode():
            flush_input()
            while _running:
                if dirty:
                    if view == "home":
                        draw_home(version, date, disk_free, backup_count, selected_action=sel_action)
                    elif view == "changelog":
                        draw_changelog(changelog, changelog_scroll)
                    elif view == "rollback":
                        draw_rollback(backups, rollback_sel)
                    dirty = False

                btn = _poll_nav()
                if btn is None:
                    time.sleep(0.03)
                    continue

                if view == "home":
                    is_git = is_git_checkout()
                    n_actions = 3 if is_git else 2
                    if btn == "KEY3":
                        break
                    elif btn == "UP":
                        sel_action = (sel_action - 1) % n_actions
                        dirty = True
                    elif btn == "DOWN":
                        sel_action = (sel_action + 1) % n_actions
                        dirty = True
                    elif btn == "KEY1" or (btn == "OK" and sel_action == 0):
                        if _trigger_update():
                            break
                        flush_input()
                    elif (btn == "KEY2" and is_git) or (btn == "OK" and is_git and sel_action == 1):
                        backups = list_backups()
                        rollback_sel = 0
                        view = "rollback"
                        dirty = True
                    elif btn == "OK" and sel_action == n_actions - 1:
                        break

                elif view == "changelog":
                    if btn == "KEY3":
                        view = "home"
                        version, date, full_hash, disk_free, backup_count = _refresh_meta()
                        dirty = True
                    elif btn in ("KEY1", "OK"):
                        show_progress("Installing update...", "Running install_raspyjack.sh --update", progress_pct=80, stage_idx=4)
                        ok, info = run_install_script()
                        if not ok:
                            show_error("INSTALL FAILED", info)
                            view = "home"
                            version, date, full_hash, disk_free, backup_count = _refresh_meta()
                            dirty = True
                            continue

                        show(["Update Complete!", "Rebooting system..."], kind="ok")
                        time.sleep(2)
                        should_reboot = True
                        break
                    elif btn == "UP":
                        if changelog_scroll > 0:
                            changelog_scroll -= 1
                            dirty = True
                    elif btn == "DOWN":
                        if changelog_scroll < max(0, len(changelog) - 1):
                            changelog_scroll += 1
                            dirty = True
                    elif btn == "PAGEUP":
                        changelog_scroll = max(0, changelog_scroll - 5)
                        dirty = True
                    elif btn == "PAGEDOWN":
                        changelog_scroll = min(max(0, len(changelog) - 1), changelog_scroll + 5)
                        dirty = True

                elif view == "rollback":
                    if btn == "KEY3":
                        view = "home"
                        dirty = True
                    elif btn == "UP" and backups:
                        rollback_sel = (rollback_sel - 1) % len(backups)
                        dirty = True
                    elif btn == "DOWN" and backups:
                        rollback_sel = (rollback_sel + 1) % len(backups)
                        dirty = True
                    elif btn == "OK" and backups:
                        selected = backups[rollback_sel]
                        bpath = os.path.join(BACKUP_ROOT, selected)
                        show_progress("Restoring backup...", _format_backup_name(selected), progress_pct=50, stage_idx=3)
                        ok, info = restore_configs(bpath)
                        if ok:
                            show(["Backup Restored!", info, "Restarting service..."], kind="ok")
                            time.sleep(1.5)
                            restart_service()
                            time.sleep(0.8)
                            break
                        else:
                            show_error("RESTORE FAILED", info)
                            dirty = True
    finally:
        try:
            _LCD.LCD_Clear()
        except Exception:
            pass
        try:
            GPIO.cleanup()
        except Exception:
            pass

    if should_reboot:
        do_reboot()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
