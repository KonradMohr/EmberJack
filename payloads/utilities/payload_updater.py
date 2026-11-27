#!/usr/bin/env python3
"""
EmberJack / RaspyJack Payload -- Payload Updater (Obsidian Ember UI)
====================================================================
Author: 7h30th3r0n3

Smart incremental updater: fetches only new and modified payloads
from GitHub. Does NOT touch system files, configs, or loot.

Flow:
  1. git fetch (download changes without applying)
  2. Compare local vs remote payload files
  3. Show list of new/modified/deleted payloads
  4. User confirms -> apply changes (payload files only)
  5. No reboot needed

Controls:
  OK / Enter   Fetch / Apply update
  UP / DOWN    Scroll file list
  KEY1 / M     Toggle scope: payloads only vs all files
  KEY2 / R     Refresh / re-fetch remote updates
  KEY3 / Esc   Back / Exit
"""

import os
import sys
import time
import subprocess
import signal
from typing import List, Optional, Tuple
_RJ_BASE_DIR = os.environ.get("RJ_BASE_DIR") or os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

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
# Constants (update mechanics preserved)
# ---------------------------------------------------------------------------
PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}

RASPYJACK_DIR = _RJ_BASE_DIR
GIT_REMOTE = "origin"
GIT_BRANCH = "main"

# Only update these directories by default (safe, no system breakage)
PAYLOAD_DIRS = ["payloads/", "img/", "menu_icons.json"]

# Never touch these (user data, configs, secrets)
EXCLUDE_PATTERNS = [
    "loot/", "config/", ".env", "discord_webhook.txt",
    "__pycache__/", "*.pyc", ".git/",
]

_shutdown = False


def _signal_handler(*_):
    global _shutdown
    _shutdown = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ---------------------------------------------------------------------------
# Git helpers (update behavior preserved)
# ---------------------------------------------------------------------------

def _git(args, timeout=30):
    """Run git command in RASPYJACK_DIR, return (success, stdout)."""
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=RASPYJACK_DIR,
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode == 0, r.stdout.strip()
    except Exception as e:
        return False, str(e)


def _get_current_hash():
    ok, out = _git(["rev-parse", "--short", "HEAD"])
    return out if ok else "unknown"


def _get_remote_hash():
    ok, out = _git(["rev-parse", "--short", f"{GIT_REMOTE}/{GIT_BRANCH}"])
    return out if ok else "unknown"


def _fetch():
    """Fetch remote changes without applying."""
    ok, _out = _git(["fetch", GIT_REMOTE, GIT_BRANCH], timeout=60)
    return ok


def _get_changed_files():
    """List files changed between local HEAD and remote.

    Returns list of (status, filepath) tuples:
      status: 'A' (added), 'M' (modified), 'D' (deleted)
    """
    ok, out = _git(["diff", "--name-status", f"HEAD..{GIT_REMOTE}/{GIT_BRANCH}"])
    if not ok or not out:
        return []

    changes = []
    for line in out.splitlines():
        parts = line.split("\t")          # renames have 3 fields: Rxxx old new
        if len(parts) < 2:
            continue
        status = parts[0][0]              # A, M, D, R, C
        if status in ("R", "C") and len(parts) >= 3:
            old_path, new_path = parts[1], parts[2]
            if status == "R":             # rename: old file goes away, new arrives
                changes.append(("D", old_path))
            changes.append(("A", new_path))   # copy (C): old stays, only new added
        else:
            changes.append((status, parts[1]))
    return changes


def _filter_payload_changes(changes, payloads_only=True):
    """Filter changes to only payload-related files."""
    filtered = []
    for status, filepath in changes:
        # Skip excluded patterns
        if any(pat in filepath for pat in EXCLUDE_PATTERNS):
            continue

        if payloads_only:
            # Only include files in PAYLOAD_DIRS
            if not any(filepath.startswith(d) or filepath == d.rstrip("/")
                       for d in PAYLOAD_DIRS):
                continue

        filtered.append((status, filepath))
    return filtered


def _get_commit_log(n=10):
    """Get last N commits between local and remote."""
    ok, out = _git(["log", "--oneline",
                     f"HEAD..{GIT_REMOTE}/{GIT_BRANCH}",
                     f"-{n}"])
    if not ok or not out:
        return []
    return out.splitlines()


def _apply_file(filepath):
    """Checkout a single file from remote."""
    ok, _ = _git(["checkout", f"{GIT_REMOTE}/{GIT_BRANCH}", "--", filepath])
    return ok


def _delete_file(filepath):
    """Delete a file that was removed in remote."""
    full = os.path.join(RASPYJACK_DIR, filepath)
    try:
        if os.path.exists(full):
            os.remove(full)
        return True
    except Exception:
        return False


def _apply_changes(changes, progress_cb=None):
    """Apply filtered changes. Returns (applied, failed) counts."""
    applied = 0
    failed = 0
    total = len(changes)

    for i, (status, filepath) in enumerate(changes):
        if _shutdown:
            break

        if progress_cb:
            progress_cb(i + 1, total, filepath)

        if status == "D":
            if _delete_file(filepath):
                applied += 1
            else:
                failed += 1
        else:
            if _apply_file(filepath):
                applied += 1
            else:
                failed += 1

    return applied, failed


# ---------------------------------------------------------------------------
# Pure Obsidian Ember PIL Renderers (640x360, 320x170, 240x240, 128x128)
# ---------------------------------------------------------------------------

def _measure_h(draw, font, text="Ag") -> int:
    b = rj_theme.measure_text(font, text, draw)
    return max(8, b[3] - b[1])


def render_home_frame(
    width: int,
    height: int,
    local_hash: str,
    payloads_only: bool = True,
    fetched: bool = False,
    fonts: Optional[dict] = None,
) -> Image.Image:
    """Render Obsidian Ember Payload Updater landing screen."""
    ET = rj_theme.EMBER_THEME
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = bool(f["wide"])
    large = bool(f["large"])

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    mode = "PAYLOADS" if payloads_only else "ALL FILES"
    is_240 = (not wide) and width >= 200
    hdr_h = rj_theme.draw_app_header(
        d, width, "PAYLOAD UPDATER", f,
        badge=mode if (wide or is_240) else "",
        right_pill=local_hash[:10] if wide else "",
    )
    hint = format_footer_hint(
        width, height,
        hat_hint="OK:Chk K1:Mode K3:X",
        hat_240_hint="OK:Check  K1:Mode  K3:Exit",
        cardputer_hint="Enter:Check  M:Scope  Esc:Exit",
        hdmi_hint="Enter:Check Updates  M:Toggle Scope  Esc:Exit",
    )
    ftr_h = rj_theme.draw_app_footer(d, width, height, hint, f, right_text=f"{GIT_REMOTE}/{GIT_BRANCH}" if large else "")

    pad = 14 if large else (6 if (wide or is_240) else 4)
    cw = width - pad * 2
    ch = height - hdr_h - ftr_h - pad * 2
    cx0, cy0 = pad, hdr_h + pad
    cx1, cy1 = cx0 + cw, cy0 + ch

    rj_theme.draw_card(
        d, (cx0, cy0, cx1, cy1),
        fill=ET["bg_surface"], outline=ET["border_subtle"],
        radius=8 if large else 4, accent_left=ET["accent"], accent_width=4 if large else 2,
    )
    tx = cx0 + (18 if large else (10 if (wide or is_240) else 7))
    tw = cw - (32 if large else (18 if (wide or is_240) else 12))
    ty = cy0 + (16 if large else (8 if (wide or is_240) else 6))

    d.text((tx, ty), "Incremental Git Updater" if (wide or is_240) else "Payload Updater", font=f["title"], fill=ET["text_bright"])
    ty += _measure_h(d, f["title"]) + (10 if large else 5)

    # Info pills / metadata row
    pw, ph = rj_theme.draw_pill(
        d, tx, ty, f"LOCAL {local_hash[:10]}", f["badge"],
        bg=ET["bg_well"], fg=ET["accent_bright"], outline=ET["border_accent"],
        pad_x=6 if large else 4, pad_y=2 if large else 1, radius=4,
    )
    if wide or is_240:
        rj_theme.draw_pill(
            d, tx + pw + 8, ty, "NO REBOOT", f["badge"],
            bg=ET["bg_well"], fg=ET["ok"], outline=ET["ok"],
            pad_x=6 if large else 4, pad_y=2 if large else 1, radius=4,
        )
    ty += ph + (12 if large else 6)

    if wide or is_240:
        desc_lines = [
            "Fetches new & updated payloads without touching loot/ or config/.",
            f"Target branch: {GIT_REMOTE}/{GIT_BRANCH}   •   Mode: {mode}",
        ] if large else [
            "Updates payloads/ without touching loot or config.",
        ]
        for dl in desc_lines:
            d.text((tx, ty), rj_theme.truncate_text(d, dl, tw, f["body"] if large else f["sub"]), font=f["body"] if large else f["sub"], fill=ET["text_muted"])
            ty += _measure_h(d, f["body"] if large else f["sub"]) + (6 if large else 3)

    # Primary CTA button card at bottom of panel
    btn_h = 36 if large else (24 if (wide or is_240) else 18)
    by1 = cy1 - (14 if large else (8 if (wide or is_240) else 6))
    by0 = max(ty + 4, by1 - btn_h)
    rj_theme.draw_card(
        d, (tx, by0, cx1 - (18 if large else 8), by1),
        fill=ET["select_bg"], outline=ET["accent"],
        radius=6 if large else 4, accent_left=ET["accent_bright"], accent_width=3 if large else 2,
    )
    cta = format_footer_hint(
        width, height,
        hat_hint="OK: View Changes" if fetched else "OK: Check Updates",
        hat_240_hint="OK: View Fetched Changes" if fetched else "OK: Check for Updates",
        cardputer_hint="View Changes  [Enter]" if fetched else "Check Updates  [Enter]",
        hdmi_hint="View Fetched Changes  [Enter]" if fetched else "Check for Updates  [Enter]",
    )
    ch_h = _measure_h(d, f["body_bold"] if (wide or is_240) else f["sub"])
    d.text((tx + (12 if large else 7), by0 + max(2, (by1 - by0 - ch_h) // 2)), rj_theme.truncate_text(d, cta, tw - 16, f["body_bold"] if (wide or is_240) else f["sub"]), font=f["body_bold"] if (wide or is_240) else f["sub"], fill=ET["text_bright"])

    return img


def render_status_frame(
    width: int,
    height: int,
    line1: str,
    line2: str = "",
    line3: str = "",
    kind: str = "info",
    fonts: Optional[dict] = None,
) -> Image.Image:
    """Render Obsidian Ember status modal card."""
    ET = rj_theme.EMBER_THEME
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = bool(f["wide"])

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    badge = "OK" if kind == "ok" else ("WARN" if kind == "warn" else ("ERROR" if kind == "err" else "SYNC"))
    rj_theme.draw_app_header(d, width, "PAYLOAD UPDATER", f, badge=badge)
    rj_theme.draw_app_footer(d, width, height, line3 or ("Please wait..." if wide else ""), f)
    rj_theme.draw_status_card(d, width, height, line1, sub=line2, kind=kind, foot=line3 if wide else "", fonts=f)
    return img


def render_changes_frame(
    width: int,
    height: int,
    changes: List[Tuple[str, str]],
    commits: List[str],
    scroll: int,
    payloads_only: bool,
    local_hash: str,
    remote_hash: str,
    fonts: Optional[dict] = None,
) -> Image.Image:
    """Render Obsidian Ember diff preview with change pills, file list, and commits."""
    ET = rj_theme.EMBER_THEME
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = bool(f["wide"])
    large = bool(f["large"])

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    mode = "PAYLOADS" if payloads_only else "ALL FILES"
    is_240 = (not wide) and width >= 200
    hdr_h = rj_theme.draw_app_header(
        d, width, "PAYLOAD UPDATER", f,
        badge=mode,
        right_pill=f"{local_hash[:7]}->{remote_hash[:7]}" if wide else "",
    )
    if changes:
        hint = format_footer_hint(
            width, height,
            hat_hint="OK:Apply K1:Mode K3:X",
            hat_240_hint="OK:Apply K1:Mode K2:Ref K3:Bk",
            cardputer_hint="Enter:Apply  M:Scope  R:Ref  Esc:Back",
            hdmi_hint="Enter:Apply  M:Toggle Scope  R:Refresh  Up/Down:Scroll  Esc:Back",
        )
    else:
        hint = format_footer_hint(
            width, height,
            hat_hint="K1:Mode K2:Ref K3:X",
            hat_240_hint="K1:Mode  K2:Refresh  K3:Back",
            cardputer_hint="M:Scope  R:Refresh  Esc:Back",
            hdmi_hint="M:Toggle Scope  R:Refresh  Esc:Back",
        )
    ftr_h = rj_theme.draw_app_footer(d, width, height, hint, f)

    pad = 10 if large else (6 if (wide or is_240) else 3)
    y = hdr_h + (8 if large else 4)
    y_bot = height - ftr_h - (8 if large else 4)

    n_add = sum(1 for s, _ in changes if s == "A")
    n_mod = sum(1 for s, _ in changes if s == "M")
    n_del = sum(1 for s, _ in changes if s == "D")

    # Summary pill bar
    sx = pad
    for txt, fg, outl in (
        (f"+{n_add} NEW" if (wide or is_240) else f"+{n_add}", ET["ok"], ET["ok"]),
        (f"~{n_mod} MOD" if (wide or is_240) else f"~{n_mod}", ET["warn"], ET["warn"]),
        (f"-{n_del} DEL" if (wide or is_240) else f"-{n_del}", ET["danger"], ET["danger"]),
    ):
        pw, ph = rj_theme.draw_pill(
            d, sx, y, txt, f["badge"],
            bg=ET["bg_surface"], fg=fg, outline=outl,
            pad_x=6 if large else 3, pad_y=2 if large else 1, radius=4,
        )
        sx += pw + (6 if large else 3)
    y += ph + (8 if large else 4)

    if not changes:
        empty_foot = format_footer_hint(
            width, height,
            hat_hint="K1:Mode  K3:Back",
            hat_240_hint="K1:Scope  K2:Ref  K3:Back",
            cardputer_hint="M:ALL FILES  R:Ref  Esc:Back",
            hdmi_hint="Press M for ALL FILES, R to refresh, or Esc to go back",
        )
        rj_theme.draw_status_card(
            d, width, height,
            "Already Up to Date!",
            sub=f"Local {local_hash[:7]} matches {remote_hash[:7]}" if (wide or is_240) else f"{local_hash[:7]} == {remote_hash[:7]}",
            kind="ok",
            foot=empty_foot,
            fonts=f,
        )
        return img

    # On 640x360 widescreen, split left = changed files, right = recent commits
    sb_w = 8 if wide else (5 if is_240 else 4)
    if large and commits:
        gap = 10
        left_w = int((width - pad * 2 - gap) * 0.62)
        list_x1 = pad + left_w - sb_w - 4
        right_x0 = pad + left_w + gap
        right_x1 = width - pad
    else:
        list_x1 = width - pad - sb_w - 3
        right_x0 = right_x1 = 0

    row_h = 26 if large else (18 if wide else (17 if is_240 else 13))
    row_gap = 4 if large else 2
    vis = max(1, (y_bot - y + row_gap) // (row_h + row_gap))
    sp = max(0, min(scroll, max(0, len(changes) - vis)))

    for i, (status, filepath) in enumerate(changes[sp:sp + vis]):
        ry0 = y + i * (row_h + row_gap)
        ry1 = min(y_bot, ry0 + row_h)
        if status == "A":
            sym, col = "+", ET["ok"]
        elif status == "M":
            sym, col = "~", ET["warn"]
        elif status == "D":
            sym, col = "-", ET["danger"]
        else:
            sym, col = "?", ET["text_muted"]

        rj_theme.draw_card(
            d, (pad, ry0, list_x1, ry1),
            fill=ET["bg_surface"], outline=ET["border_subtle"],
            radius=4 if large else 2, accent_left=col, accent_width=3 if large else 2,
        )
        tx = pad + (8 if large else 5)
        pad_y_pill = 2 if large else 1
        sb = rj_theme.measure_text(f["badge"], sym, d)
        pill_h = (sb[3] - sb[1]) + pad_y_pill * 2
        pw, _ = rj_theme.draw_pill(
            d, tx, ry0 + max(1, (ry1 - ry0 - pill_h) // 2),
            sym, f["badge"], bg=ET["bg_well"], fg=col, outline=col,
            pad_x=5 if large else 2, pad_y=pad_y_pill, radius=3,
        )
        fx = tx + pw + (6 if large else 4)
        label = filepath if large else os.path.basename(filepath)
        fh_px = _measure_h(d, f["sub"])
        d.text(
            (fx, ry0 + max(1, (ry1 - ry0 - fh_px) // 2)),
            rj_theme.truncate_text(d, label, max(20, list_x1 - fx - 5), f["sub"]),
            font=f["sub"],
            fill=ET["text_primary"],
        )

    if len(changes) > vis:
        rj_theme.draw_scrollbar(d, list_x1 + 3, y, y_bot, sp, vis, len(changes), width=4 if wide else 2)

    # Widescreen commits sidebar
    if large and commits and right_x1 > right_x0 + 40:
        rj_theme.draw_card(
            d, (right_x0, y, right_x1, y_bot),
            fill=ET["bg_surface"], outline=ET["border_subtle"],
            radius=6, accent_left=ET["accent"], accent_width=2,
        )
        cx = right_x0 + 10
        cw = right_x1 - right_x0 - 16
        cy = y + 8
        d.text((cx, cy), f"COMMITS ({len(commits)})", font=f["badge"], fill=ET["accent_bright"])
        cy += _measure_h(d, f["badge"]) + 8
        d.line([(cx, cy - 3), (right_x1 - 8, cy - 3)], fill=ET["border_subtle"], width=1)
        c_step = _measure_h(d, f["sub"]) + 7
        for cline in commits:
            if cy + c_step > y_bot - 6:
                break
            d.text((cx, cy), rj_theme.truncate_text(d, cline, cw, f["sub"]), font=f["sub"], fill=ET["text_muted"])
            cy += c_step

    return img


def render_progress_frame(
    width: int,
    height: int,
    current: int,
    total: int,
    filename: str,
    fonts: Optional[dict] = None,
) -> Image.Image:
    """Render Obsidian Ember file-apply progress card."""
    ET = rj_theme.EMBER_THEME
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = bool(f["wide"])
    large = bool(f["large"])

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    pct = int(current / max(total, 1) * 100)
    hdr_h = rj_theme.draw_app_header(d, width, "UPDATING", f, badge=f"{current}/{total}", right_pill=f"{pct}%")
    ftr_h = rj_theme.draw_app_footer(d, width, height, "Applying remote file changes..." if wide else "Updating...", f)

    pad = 14 if large else (8 if wide else 4)
    y0 = hdr_h + pad
    y1 = height - ftr_h - pad
    rj_theme.draw_card(
        d, (pad, y0, width - pad, y1),
        fill=ET["bg_surface"], outline=ET["border_accent"],
        radius=8 if large else 4, accent_left=ET["accent"], accent_width=4 if large else 2,
    )

    tx = pad + (18 if large else 10)
    tw = width - pad * 2 - (32 if large else 18)
    ty = y0 + (18 if large else 10)

    d.text((tx, ty), f"Updating {current} of {total} files", font=f["title"], fill=ET["text_bright"])
    ty += _measure_h(d, f["title"]) + (12 if large else 6)

    bar_h = 14 if large else (10 if wide else 8)
    rj_theme.draw_progress_bar(
        d, (tx, ty, tx + tw, ty + bar_h),
        pct / 100.0, fill=ET["accent"], track=ET["bg_well"], outline=ET["border_subtle"], radius=4 if wide else 2,
    )
    ty += bar_h + (14 if large else 8)

    well_h = 34 if large else (22 if wide else 16)
    if ty + well_h <= y1 - 6:
        rj_theme.draw_card(d, (tx, ty, tx + tw, ty + well_h), fill=ET["bg_well"], outline=ET["border_subtle"], radius=4 if large else 2)
        fn = filename if large else os.path.basename(filename)
        fh = _measure_h(d, f["sub"])
        d.text((tx + (10 if large else 5), ty + max(2, (well_h - fh) // 2)), rj_theme.truncate_text(d, fn, tw - 16, f["sub"]), font=f["sub"], fill=ET["accent_bright"])

    return img


def render_result_frame(
    width: int,
    height: int,
    applied: int,
    failed: int,
    local_hash: str,
    fonts: Optional[dict] = None,
) -> Image.Image:
    """Render Obsidian Ember completion summary card."""
    ET = rj_theme.EMBER_THEME
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = bool(f["wide"])
    large = bool(f["large"])

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    kind = "ok" if failed == 0 else "warn"
    accent = ET["ok"] if failed == 0 else ET["warn"]
    hdr_h = rj_theme.draw_app_header(d, width, "UPDATE COMPLETE", f, badge="DONE" if failed == 0 else "PARTIAL", right_pill=local_hash[:10] if wide else "")
    res_hint = format_footer_hint(
        width, height,
        hat_hint="OK/K3:Exit",
        hat_240_hint="OK / K3: Exit",
        cardputer_hint="Enter / Esc: Exit",
        hdmi_hint="Press Enter or Esc to exit",
    )
    ftr_h = rj_theme.draw_app_footer(d, width, height, res_hint, f)

    pad = 14 if large else (6 if wide else 4)
    y0 = hdr_h + pad
    y1 = height - ftr_h - pad
    rj_theme.draw_card(
        d, (pad, y0, width - pad, y1),
        fill=ET["bg_surface"], outline=accent,
        radius=8 if large else 4, accent_left=accent, accent_width=4 if large else 2,
    )

    tx = pad + (18 if large else 10)
    ty = y0 + (16 if large else 8)
    d.text((tx, ty), "Payloads Updated" if failed == 0 else "Completed With Errors", font=f["title"], fill=accent)
    ty += _measure_h(d, f["title"]) + (12 if large else 6)

    pw, ph = rj_theme.draw_pill(
        d, tx, ty, f"APPLIED: {applied}", f["badge"],
        bg=ET["bg_well"], fg=ET["ok"], outline=ET["ok"],
        pad_x=6 if large else 4, pad_y=2 if large else 1, radius=4,
    )
    if failed > 0:
        rj_theme.draw_pill(
            d, tx + pw + 8, ty, f"FAILED: {failed}", f["badge"],
            bg=ET["bg_well"], fg=ET["danger"], outline=ET["danger"],
            pad_x=6 if large else 4, pad_y=2 if large else 1, radius=4,
        )
    ty += ph + (12 if large else 6)

    d.text((tx, ty), f"Version: {local_hash[:10]}", font=f["body"] if wide else f["sub"], fill=ET["text_primary"])
    ty += _measure_h(d, f["body"] if wide else f["sub"]) + (6 if large else 3)
    d.text((tx, ty), "No reboot needed — ready to use", font=f["sub"], fill=ET["ok"])

    return img


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _shutdown

    import RPi.GPIO as GPIO
    import LCD_1in44
    import LCD_Config
    from payloads._input_helper import flush_input, poll_ui_input, text_input_mode

    GPIO.setmode(GPIO.BCM)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    LCD_Config.GPIO_Init()
    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    lcd.LCD_Clear()
    w, h = lcd.width, lcd.height
    fonts = rj_theme.load_app_fonts(w, h)

    def _poll_nav() -> Optional[str]:
        act, ch = poll_ui_input(PINS, GPIO)
        if act is None and ch is None:
            return None
        if is_confirm(act):
            return "OK"
        if is_back(act):
            return "KEY3"
        if is_action(act, ch, "KEY1", "m"):
            return "KEY1"
        if is_action(act, ch, "KEY2", "r"):
            return "KEY2"
        if act in ("UP", "DOWN", "PAGEUP", "PAGEDOWN"):
            return act
        return None

    # Check git repo exists
    if not os.path.isdir(os.path.join(RASPYJACK_DIR, ".git")):
        lcd.LCD_ShowImage(render_status_frame(w, h, "No Git Repo Found", RASPYJACK_DIR, "Clone repo to update", kind="err", fonts=fonts), 0, 0)
        time.sleep(3)
        GPIO.cleanup()
        return 1

    # State
    screen = "home"    # home, changes, updating, result
    changes: List[Tuple[str, str]] = []
    all_changes: List[Tuple[str, str]] = []
    commits: List[str] = []
    scroll = 0
    payloads_only = True
    applied = 0
    failed = 0
    local_hash = _get_current_hash()
    remote_hash = ""
    fetched = False
    dirty = True

    try:
        with text_input_mode():
            flush_input()
            while not _shutdown:
                if dirty:
                    if screen == "home":
                        lcd.LCD_ShowImage(render_home_frame(w, h, local_hash, payloads_only=payloads_only, fetched=fetched, fonts=fonts), 0, 0)
                    elif screen == "changes":
                        lcd.LCD_ShowImage(render_changes_frame(w, h, changes, commits, scroll, payloads_only, local_hash, remote_hash, fonts=fonts), 0, 0)
                    elif screen == "result":
                        lcd.LCD_ShowImage(render_result_frame(w, h, applied, failed, local_hash, fonts=fonts), 0, 0)
                    dirty = False

                btn = _poll_nav()
                if btn is None:
                    time.sleep(0.03)
                    continue

                if btn == "KEY3":
                    if screen == "result":
                        break
                    elif screen == "changes":
                        screen = "home"
                        dirty = True
                    else:
                        break

                elif btn == "OK":
                    if screen == "result":
                        break
                    elif screen == "home" and not fetched:
                        lcd.LCD_ShowImage(render_status_frame(w, h, "Fetching Updates...", f"Checking {GIT_REMOTE}/{GIT_BRANCH}", "Please wait...", kind="info", fonts=fonts), 0, 0)
                        ok = _fetch()
                        if not ok:
                            lcd.LCD_ShowImage(render_status_frame(w, h, "Fetch Failed", "Check network connection", "Returning to home...", kind="err", fonts=fonts), 0, 0)
                            time.sleep(2)
                            dirty = True
                            continue

                        fetched = True
                        remote_hash = _get_remote_hash()
                        all_changes = _get_changed_files()
                        commits = _get_commit_log()

                        if local_hash == remote_hash:
                            changes = []
                        else:
                            changes = _filter_payload_changes(all_changes, payloads_only)

                        screen = "changes"
                        scroll = 0
                        dirty = True

                    elif screen == "home" and fetched:
                        changes = _filter_payload_changes(all_changes, payloads_only)
                        screen = "changes"
                        scroll = 0
                        dirty = True

                    elif screen == "changes" and changes:
                        screen = "updating"

                        def progress_cb(current, total, filename):
                            lcd.LCD_ShowImage(render_progress_frame(w, h, current, total, filename, fonts=fonts), 0, 0)

                        applied, failed = _apply_changes(changes, progress_cb)

                        # Update local HEAD to match what we applied
                        if applied > 0 and failed == 0:
                            _git(["reset", "--hard", f"{GIT_REMOTE}/{GIT_BRANCH}"],
                                 timeout=30)

                        local_hash = _get_current_hash()
                        screen = "result"
                        dirty = True

                elif btn == "KEY1" and screen in ("home", "changes"):
                    payloads_only = not payloads_only
                    if screen == "changes":
                        changes = _filter_payload_changes(all_changes, payloads_only)
                        scroll = 0
                    dirty = True

                elif btn == "KEY2" and screen in ("home", "changes"):
                    lcd.LCD_ShowImage(render_status_frame(w, h, "Fetching Updates...", f"Checking {GIT_REMOTE}/{GIT_BRANCH}", "Please wait...", kind="info", fonts=fonts), 0, 0)
                    if not _fetch():
                        lcd.LCD_ShowImage(render_status_frame(w, h, "Fetch Failed", "Check network connection", "Returning...", kind="err", fonts=fonts), 0, 0)
                        time.sleep(2)
                        dirty = True
                        continue
                    fetched = True
                    remote_hash = _get_remote_hash()
                    all_changes = _get_changed_files()
                    commits = _get_commit_log()
                    changes = [] if local_hash == remote_hash else _filter_payload_changes(all_changes, payloads_only)
                    screen = "changes"
                    scroll = 0
                    dirty = True

                elif btn == "UP" and screen == "changes":
                    if scroll > 0:
                        scroll -= 1
                        dirty = True

                elif btn == "DOWN" and screen == "changes":
                    if scroll < max(0, len(changes) - 1):
                        scroll += 1
                        dirty = True

                elif btn == "PAGEUP" and screen == "changes":
                    scroll = max(0, scroll - 6)
                    dirty = True

                elif btn == "PAGEDOWN" and screen == "changes":
                    scroll = min(max(0, len(changes) - 1), scroll + 6)
                    dirty = True

    finally:
        try:
            lcd.LCD_Clear()
        except Exception:
            pass
        GPIO.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
