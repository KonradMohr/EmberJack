#!/usr/bin/env python3
"""
EmberJack Payload -- Package Installer
========================================
Author: 7h30th3r0n3

Native Obsidian Ember split-pane APT & PIP package browser, search, and live
installer with animated progress status card and scrollable console log.

Controls (Browser):
  UP / DOWN           Select package or custom input row
  OK / Enter          Install selected package (or open custom input)
  KEY1 / /            Search / type custom package via lcd_keyboard
  KEY2 / Tab          Toggle APT / PIP mode
  C                   Show installed package count stats
  KEY3 / Esc          Exit

Controls (Install Output):
  UP / DOWN / PgUp/Dn Scroll output log
  KEY1 / C            Show installed stats (when idle)
  OK / Enter / Esc / KEY3 Return to package browser (when idle)
"""

import os
import signal
import subprocess
import sys
import threading
import time

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
from PIL import Image, ImageDraw

import rj_theme
from payloads._input_helper import (
    flush_input,
    format_footer_hint,
    is_action,
    is_back,
    is_confirm,
    is_dismiss,
    poll_ui_input,
    text_input_mode,
)
from payloads._keyboard_helper import lcd_keyboard

try:
    import rj_keyboard as _rj_keyboard
except Exception:
    _rj_keyboard = None

PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}

# Curated starter catalogs (users can always search or type any custom package!)
CURATED_APT = [
    ("nmap", "Network discovery & port scanner"),
    ("tcpdump", "Command-line packet analyzer"),
    ("tshark", "Terminal Wireshark protocol analyzer"),
    ("aircrack-ng", "WiFi security auditing suite"),
    ("hcxdumptool", "Small PMKID/EAPOL capture tool"),
    ("tmux", "Terminal multiplexer"),
    ("htop", "Interactive process & system viewer"),
    ("jq", "Lightweight command-line JSON processor"),
    ("socat", "Multipurpose relay & socket Swiss Army knife"),
    ("ffmpeg", "Audio/video decoding & streaming engine"),
    ("yt-dlp", "Media downloader for TV & YouTube"),
    ("python3-serial", "Python serial port library (pyserial)"),
    ("gpsd-clients", "GPS daemon tools & diagnostics"),
    ("curl", "HTTP/HTTPS command-line client"),
]

CURATED_PIP = [
    ("scapy", "Interactive packet crafting & sniffing"),
    ("pyserial", "Serial port I/O for UART & USB-TTL"),
    ("requests", "Simple ergonomic HTTP client library"),
    ("impacket", "Network protocols toolkit for Python"),
    ("rich", "Rich terminal formatting & tables"),
    ("bleak", "Cross-platform Bluetooth Low Energy client"),
    ("pycryptodome", "Self-contained cryptographic primitives"),
    ("flask", "Lightweight Python web framework"),
    ("gpsd-py3", "Python 3 client for gpsd"),
]

_running = True
_install_lock = threading.Lock()
_install_output = []
_installing = False
_install_rc = None
_install_pkg = ""
_install_started = 0.0


def _cleanup(*_args):
    global _running
    _running = False


signal.signal(signal.SIGINT, _cleanup)
signal.signal(signal.SIGTERM, _cleanup)


# ---------------------------------------------------------------------------
# Package operations (preserved 100% + safe apt-cache search helper)
# ---------------------------------------------------------------------------
def _count_installed(pkg_mode):
    """Count installed packages for apt or pip."""
    try:
        if pkg_mode == "apt":
            result = subprocess.run(
                ["dpkg", "--get-selections"],
                capture_output=True, text=True, timeout=15,
            )
            lines = [l for l in result.stdout.splitlines() if "install" in l]
            return len(lines)
        else:
            result = subprocess.run(
                ["pip3", "list", "--format=columns"],
                capture_output=True, text=True, timeout=15,
            )
            return max(0, len(result.stdout.splitlines()) - 2)
    except (subprocess.TimeoutExpired, OSError):
        return -1


def _search_apt_packages(query, limit=25):
    """Search apt-cache safely; returns [(pkg, desc), ...]."""
    q = str(query or "").strip()
    if not q:
        return list(CURATED_APT)
    try:
        res = subprocess.run(
            ["apt-cache", "search", q],
            capture_output=True, text=True, timeout=8,
        )
        found = []
        for ln in (res.stdout or "").splitlines():
            if " - " in ln:
                name, desc = ln.split(" - ", 1)
                found.append((name.strip(), desc.strip()))
                if len(found) >= limit:
                    break
        if found:
            return found
    except Exception:
        pass
    low = q.lower()
    return [(n, d) for (n, d) in CURATED_APT if low in n.lower() or low in d.lower()]


def _install_package(pkg_name, pkg_mode):
    """Install a package in background, capturing live output."""
    global _installing, _install_output, _install_rc, _install_pkg, _install_started

    with _install_lock:
        _installing = True
        _install_rc = None
        _install_pkg = pkg_name
        _install_started = time.monotonic()
        _install_output = [f"Starting {pkg_mode.upper()} install: {pkg_name}"]

    if pkg_mode == "apt":
        cmd = ["apt-get", "install", "-y", pkg_name]
    else:
        cmd = ["pip3", "install", pkg_name]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:
            stripped = line.rstrip()
            if stripped:
                with _install_lock:
                    _install_output.append(stripped[:96])
                    if len(_install_output) > 400:
                        _install_output = _install_output[-400:]
        proc.wait(timeout=180)
        rc = proc.returncode
        with _install_lock:
            _install_rc = rc
            if rc == 0:
                _install_output.append("")
                _install_output.append("OK: Install complete!")
            else:
                _install_output.append("")
                _install_output.append(f"FAIL: exit {rc}")
    except (subprocess.TimeoutExpired, OSError) as exc:
        with _install_lock:
            _install_rc = -1
            _install_output.append("Err: " + str(exc)[:60])

    with _install_lock:
        _installing = False


# ---------------------------------------------------------------------------
# Native Obsidian Ember Renderers
# ---------------------------------------------------------------------------
def _draw_browser(lcd, fonts, pkg_mode, catalog, cursor, scroll, search_query, stats_cache):
    W, H = lcd.width, lcd.height
    ET = rj_theme.EMBER_THEME
    wide = bool(fonts["wide"])
    large = bool(fonts["large"])
    mid = (not wide) and W >= 200

    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)

    mode_label = "APT" if pkg_mode == "apt" else "PIP"
    cnt = stats_cache.get(pkg_mode)
    right_pill = f"{cnt} INSTALLED" if (isinstance(cnt, int) and cnt >= 0) else f"MODE:{mode_label}"

    hdr_h = rj_theme.draw_app_header(
        d, W, "PACKAGE INSTALLER" if wide else "PACKAGES", fonts,
        badge=mode_label,
        right_text=f"FILTER: {search_query}" if (large and search_query) else "",
        right_pill=right_pill,
        hdr_h=(30 if large else (22 if wide else (22 if mid else 18))),
    )
    hint = format_footer_hint(
        W, H,
        hat_hint="OK:Go K1:Src K2:Md K3:X",
        hat_240_hint="OK:Inst K1:Search K2:Mode K3:Exit",
        cardputer_hint="Ent:Inst /:Search Tab:Mode C:Stat Esc:Exit",
        hdmi_hint="Enter:Install  /:Search/Custom  Tab:APT/PIP  C:Stats  Esc:Exit",
    )
    ftr_h = rj_theme.draw_app_footer(
        d, W, H, hint, fonts,
        ftr_h=(22 if large else (16 if wide else (16 if mid else 13))),
    )

    pad = 8 if large else (5 if (wide or mid) else 3)
    y0 = hdr_h + pad
    y1 = H - ftr_h - pad
    split = W >= 480

    # Row 0 is always "[+] Type / Search Package..." so custom names are 1 press away
    rows = [("__CUSTOM__", "Type any package name or search apt-cache")] + list(catalog)
    total = len(rows)
    cursor = max(0, min(cursor, total - 1))

    list_x0 = pad
    list_x1 = int(W * 0.56) - 4 if split else (W - pad - (6 if (wide or mid) else 4))
    row_h = 34 if large else (22 if wide else (24 if mid else 17))
    vis = max(1, (y1 - y0) // row_h)
    st = max(0, min(scroll, max(0, total - vis)))
    if cursor < st:
        st = cursor
    elif cursor >= st + vis:
        st = cursor - vis + 1

    for slot in range(vis):
        idx = st + slot
        if idx >= total:
            break
        pkg_name, pkg_desc = rows[idx]
        ry = y0 + slot * row_h
        ry1 = ry + row_h - (4 if (wide or mid) else 2)
        on = (idx == cursor)
        is_custom = (pkg_name == "__CUSTOM__")

        rj_theme.draw_card(
            d, (list_x0, ry, list_x1, ry1),
            fill=ET["select_bg"] if on else (ET["bg_elevated"] if is_custom else ET["bg_surface"]),
            outline=ET["border_accent"] if (on or is_custom) else ET["border_subtle"],
            radius=6 if wide else (4 if mid else 3),
            accent_left=ET["accent"] if on else (ET["accent_dim"] if is_custom else None),
            accent_width=3 if (wide or mid) else 2,
        )

        lx = list_x0 + (10 if wide else (8 if mid else 5))
        rx = list_x1 - (8 if (wide or mid) else 4)
        if is_custom:
            label = "+ Custom Package / Search..." if (wide or mid) else "+ Custom Pkg..."
            d.text((lx, (ry + ry1) // 2), rj_theme.truncate_text(d, label, rx - lx, fonts["body_bold"]), font=fonts["body_bold"], fill=ET["accent_bright"], anchor="lm")
        else:
            if large:
                d.text((lx, ry + 5), rj_theme.truncate_text(d, pkg_name, rx - lx, fonts["body_bold"]), font=fonts["body_bold"], fill=ET["accent_bright"] if on else ET["text_bright"])
                d.text((lx, ry + 19), rj_theme.truncate_text(d, pkg_desc, rx - lx, fonts["sub"]), font=fonts["sub"], fill=ET["text_muted"])
            else:
                d.text((lx, (ry + ry1) // 2), rj_theme.truncate_text(d, pkg_name, rx - lx, fonts["body_bold"] if on else fonts["body"]), font=fonts["body_bold"] if on else fonts["body"], fill=ET["accent_bright"] if on else ET["text_primary"], anchor="lm")

    rj_theme.draw_scrollbar(d, list_x1 + 3, y0, y1, st, vis, total, width=3 if (wide or mid) else 2)

    # Right Details & Command Preview Pane on Widescreen (>= 480px)
    if split:
        px0 = int(W * 0.56) + 6
        px1 = W - pad
        rj_theme.draw_card(
            d, (px0, y0, px1, y1),
            fill=ET["bg_surface"], outline=ET["border_subtle"],
            radius=8, accent_left=ET["accent_dim"], accent_width=3,
        )
        ix = px0 + 14
        iw = (px1 - px0) - 24
        iy = y0 + 14
        sel_pkg, sel_desc = rows[cursor]

        rj_theme.draw_pill(
            d, ix, iy, f"{mode_label} PACKAGE" if sel_pkg != "__CUSTOM__" else f"{mode_label} INSTALLER",
            fonts["badge"], bg=ET["select_bg"], fg=ET["accent_bright"], outline=ET["border_accent"],
            pad_x=6, pad_y=2, radius=3,
        )
        iy += 28

        if sel_pkg == "__CUSTOM__":
            d.text((ix, iy), "Custom / Search", font=fonts["title"], fill=ET["text_bright"])
            iy += 24
            help_txt = (
                "Press Enter or / to type any package name to install directly, "
                "or switch between APT and PIP with Tab."
            )
            for ln in rj_theme.wrap_text(d, help_txt, iw, fonts["sub"])[:4]:
                d.text((ix, iy), ln, font=fonts["sub"], fill=ET["text_muted"])
                iy += 15
        else:
            d.text((ix, iy), rj_theme.truncate_text(d, sel_pkg, iw, fonts["title"]), font=fonts["title"], fill=ET["text_bright"])
            iy += 24
            for ln in rj_theme.wrap_text(d, sel_desc or "Linux software package", iw, fonts["body"])[:3]:
                d.text((ix, iy), ln, font=fonts["body"], fill=ET["text_primary"])
                iy += 17
            iy += 10
            d.text((ix, iy), "COMMAND PREVIEW", font=fonts["badge"], fill=ET["text_muted"])
            iy += 16
            cmd_str = f"apt-get install -y {sel_pkg}" if pkg_mode == "apt" else f"pip3 install {sel_pkg}"
            rj_theme.draw_card(d, (ix, iy, px1 - 12, iy + 28), fill=ET["bg_well"], outline=ET["border_subtle"], radius=5)
            d.text((ix + 8, iy + 14), rj_theme.truncate_text(d, cmd_str, iw - 14, fonts["mono"]), font=fonts["mono"], fill=ET["accent_bright"], anchor="lm")

        # Bottom summary box inside right pane
        sy = y1 - 44
        d.line([(ix, sy - 8), (px1 - 12, sy - 8)], fill=ET["border_subtle"], width=1)
        d.text((ix, sy), f"Active Manager: {mode_label}", font=fonts["sub"], fill=ET["text_muted"])
        cnt_lbl = f"{cnt} installed" if (isinstance(cnt, int) and cnt >= 0) else "Press C for stats"
        d.text((ix, sy + 16), f"Catalog: {len(catalog)} shown  •  {cnt_lbl}", font=fonts["sub"], fill=ET["text_primary"])

    lcd.LCD_ShowImage(img, 0, 0)
    return st


def _draw_output(lcd, fonts, lines, scroll, installing, pkg_mode, pkg_name, rc, started_at):
    """Draw the live Obsidian Ember install status card + scrollable console."""
    W, H = lcd.width, lcd.height
    ET = rj_theme.EMBER_THEME
    wide = bool(fonts["wide"])
    large = bool(fonts["large"])
    mid = (not wide) and W >= 200

    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)

    mode_label = "APT" if pkg_mode == "apt" else "PIP"
    hdr_h = rj_theme.draw_app_header(
        d, W, "INSTALLING" if installing else "INSTALL OUTPUT", fonts,
        badge=mode_label, right_pill=pkg_name[:16],
        hdr_h=(30 if large else (22 if wide else (22 if mid else 18))),
    )
    if installing:
        out_hint = "Installing package — please wait..." if (wide or mid) else "Installing..."
    else:
        out_hint = format_footer_hint(
            W, H,
            hat_hint="U/D:Scr K1:St K3:Bk",
            hat_240_hint="U/D:Scroll K1:Stats OK/K3:Back",
            cardputer_hint="U/D:Scroll C:Stats Ent/Esc:Back",
            hdmi_hint="Up/Dn:Scroll  C:Stats  Enter/Esc:Back",
        )
    ftr_h = rj_theme.draw_app_footer(
        d, W, H, out_hint, fonts,
        ftr_h=(22 if large else (16 if wide else (16 if mid else 13))),
    )

    pad = 8 if large else (4 if wide else (5 if mid else 3))
    y0 = hdr_h + pad
    y1 = H - ftr_h - pad

    # 1. Top Progress Status Card
    card_h = 54 if large else (34 if (wide or mid) else 28)
    card_y1 = y0 + card_h
    if installing:
        st_accent = ET["warn"]
        st_badge = "RUNNING"
        elapsed = max(0.0, time.monotonic() - started_at)
        sub_txt = f"Installing {pkg_name} ({int(elapsed)}s)..."
        frac = 0.15 + 0.75 * (0.5 + 0.5 * ((time.monotonic() * 0.7) % 1.0))
    elif rc == 0:
        st_accent = ET["ok"]
        st_badge = "DONE"
        sub_txt = f"{pkg_name} installed successfully"
        frac = 1.0
    else:
        st_accent = ET["danger"]
        st_badge = f"ERR {rc}"
        sub_txt = f"Install failed for {pkg_name}"
        frac = 1.0

    rj_theme.draw_card(
        d, (pad, y0, W - pad, card_y1),
        fill=ET["bg_surface"], outline=st_accent,
        radius=7 if wide else (5 if mid else 4), accent_left=st_accent, accent_width=4 if wide else 2,
    )
    lx = pad + (12 if wide else (8 if mid else 6))
    pb = rj_theme.measure_text(fonts["badge"], st_badge, d)
    pw = (pb[2] - pb[0]) + (10 if (wide or mid) else 6)
    rj_theme.draw_pill(
        d, W - pad - pw - (8 if (wide or mid) else 4), y0 + (8 if large else 4),
        st_badge, fonts["badge"], bg=ET["bg_well"], fg=st_accent, outline=st_accent,
        pad_x=5 if (wide or mid) else 3, pad_y=1, radius=3,
    )
    d.text((lx, y0 + (10 if large else 4)), rj_theme.truncate_text(d, sub_txt, W - pad * 2 - pw - 24, fonts["body_bold"] if (wide or mid) else fonts["sub"]), font=fonts["body_bold"] if (wide or mid) else fonts["sub"], fill=ET["text_bright"])
    rj_theme.draw_progress_bar(
        d, (lx, card_y1 - (14 if large else 9), W - pad - (10 if (wide or mid) else 5), card_y1 - (6 if large else 4)),
        frac, fill=st_accent, track=ET["bg_well"], outline=ET["border_subtle"], radius=3,
    )

    # 2. Recessed Scrollable Monospace Log Console
    log_y0 = card_y1 + (6 if (wide or mid) else 3)
    rj_theme.draw_card(d, (pad, log_y0, W - pad, y1), fill=ET["bg_well"], outline=ET["border_subtle"], radius=6 if wide else (4 if mid else 3))

    log_font = fonts["mono"] if large else fonts["sub"]
    lh = 15 if large else (12 if (wide or mid) else 10)
    vis = max(1, (y1 - log_y0 - 8) // lh)
    st = max(0, min(scroll, max(0, len(lines) - vis)))
    max_w = (W - pad * 2) - (18 if (wide or mid) else 10)
    ty = log_y0 + 4
    for idx in range(st, min(len(lines), st + vis)):
        line = lines[idx]
        if "OK:" in line:
            col = ET["ok"]
        elif "FAIL:" in line or "Err:" in line or "E:" in line[:3]:
            col = ET["danger"]
        else:
            col = ET["text_primary"]
        d.text((pad + (8 if (wide or mid) else 4), ty), rj_theme.truncate_text(d, line, max_w, log_font), font=log_font, fill=col)
        ty += lh

    rj_theme.draw_scrollbar(d, W - pad - (6 if (wide or mid) else 4), log_y0 + 4, y1 - 4, st, vis, len(lines), width=3 if (wide or mid) else 2)
    lcd.LCD_ShowImage(img, 0, 0)
    return vis


def _draw_count(lcd, fonts, apt_cnt, pip_cnt, active_mode):
    W, H = lcd.width, lcd.height
    ET = rj_theme.EMBER_THEME
    wide = bool(fonts["wide"])
    large = bool(fonts["large"])
    mid = (not wide) and W >= 200
    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)
    rj_theme.draw_app_header(
        d, W, "PACKAGE STATS", fonts, badge=active_mode.upper(),
        hdr_h=(30 if large else (22 if wide else (22 if mid else 18))),
    )
    apt_s = f"{apt_cnt} pkgs" if apt_cnt >= 0 else "N/A"
    pip_s = f"{pip_cnt} pkgs" if pip_cnt >= 0 else "N/A"
    cnt_foot = format_footer_hint(
        W, H,
        hat_hint="OK/K3: Back",
        hat_240_hint="OK / K3: Return",
        cardputer_hint="Enter / Esc: Return",
        hdmi_hint="Press Enter or Esc to return",
    )
    rj_theme.draw_status_card(
        d, W, H, "Installed Packages",
        sub=f"APT: {apt_s}   •   PIP: {pip_s}",
        kind="ok" if (apt_cnt >= 0 or pip_cnt >= 0) else "err",
        foot=cnt_foot,
        fonts=fonts,
    )
    lcd.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    fonts = rj_theme.load_app_fonts(lcd.width, lcd.height)

    pkg_mode = "apt"  # "apt" or "pip"
    catalog = list(CURATED_APT)
    search_query = ""
    cursor = 0
    scroll = 0
    out_scroll = 0
    out_vis = 8
    view = "browser"  # browser | output | count
    stats_cache = {}
    dirty = True

    def _start_install(pkg_name):
        nonlocal view, out_scroll, dirty
        out_scroll = 0
        view = "output"
        dirty = True
        threading.Thread(
            target=_install_package,
            args=(pkg_name, pkg_mode),
            daemon=True,
        ).start()

    def _prompt_custom_or_search():
        nonlocal catalog, search_query, cursor, scroll, dirty
        mode_label = "APT" if pkg_mode == "apt" else "PIP"
        entered = lcd_keyboard(
            lcd, fonts["body"], PINS, GPIO,
            title=f"PKG NAME OR ?SEARCH [{mode_label}]",
            default=search_query,
            charset="full",
        )
        if _rj_keyboard is not None:
            _rj_keyboard.set_text_mode(True)
        flush_input()
        if entered is None:
            dirty = True
            return
        val = entered.strip()
        if not val:
            search_query = ""
            catalog = list(CURATED_APT if pkg_mode == "apt" else CURATED_PIP)
            cursor = scroll = 0
            dirty = True
            return
        if val.startswith("?") and pkg_mode == "apt":
            search_query = val[1:].strip()
            catalog = _search_apt_packages(search_query)
            cursor = scroll = 0
            dirty = True
        else:
            _start_install(val)

    try:
        with text_input_mode():
            flush_input()
            while _running:
                if view == "browser":
                    if dirty:
                        scroll = _draw_browser(lcd, fonts, pkg_mode, catalog, cursor, scroll, search_query, stats_cache)
                        dirty = False

                    act, ch = poll_ui_input(PINS, GPIO)
                    if act is None and ch is None:
                        time.sleep(0.03)
                        continue

                    kl = ch.lower() if isinstance(ch, str) else ""
                    total_rows = 1 + len(catalog)
                    if is_back(act):
                        break
                    elif act == "UP":
                        cursor = (cursor - 1) % total_rows
                        dirty = True
                    elif act == "DOWN":
                        cursor = (cursor + 1) % total_rows
                        dirty = True
                    elif act == "PAGEUP":
                        cursor = max(0, cursor - 5)
                        dirty = True
                    elif act == "PAGEDOWN":
                        cursor = min(total_rows - 1, cursor + 5)
                        dirty = True
                    elif act in ("KEY2", "TAB"):
                        pkg_mode = "pip" if pkg_mode == "apt" else "apt"
                        search_query = ""
                        catalog = list(CURATED_APT if pkg_mode == "apt" else CURATED_PIP)
                        cursor = scroll = 0
                        dirty = True
                    elif is_action(act, ch, "KEY1", "/"):
                        _prompt_custom_or_search()
                    elif kl == "c":
                        apt_c = _count_installed("apt")
                        pip_c = _count_installed("pip")
                        stats_cache["apt"] = apt_c
                        stats_cache["pip"] = pip_c
                        _draw_count(lcd, fonts, apt_c, pip_c, pkg_mode)
                        view = "count"
                        dirty = False
                    elif is_confirm(act):
                        if cursor == 0:
                            _prompt_custom_or_search()
                        elif cursor - 1 < len(catalog):
                            _start_install(catalog[cursor - 1][0])

                elif view == "count":
                    act, _ch = poll_ui_input(PINS, GPIO)
                    if is_dismiss(act):
                        view = "browser"
                        dirty = True
                    time.sleep(0.04)

                elif view == "output":
                    with _install_lock:
                        lines = list(_install_output)
                        busy = _installing
                        rc = _install_rc
                        pkg_name = _install_pkg
                        started_at = _install_started

                    if busy:
                        out_scroll = max(0, len(lines) - out_vis)

                    out_vis = _draw_output(lcd, fonts, lines, out_scroll, busy, pkg_mode, pkg_name, rc, started_at)

                    act, ch = poll_ui_input(PINS, GPIO)
                    if is_dismiss(act) and not busy:
                        stats_cache.pop(pkg_mode, None)
                        view = "browser"
                        dirty = True
                        continue
                    elif act == "UP":
                        out_scroll = max(0, out_scroll - 1)
                    elif act == "DOWN":
                        out_scroll = min(max(0, len(lines) - out_vis), out_scroll + 1)
                    elif act == "PAGEUP":
                        out_scroll = max(0, out_scroll - max(3, out_vis // 2))
                    elif act == "PAGEDOWN":
                        out_scroll = min(max(0, len(lines) - out_vis), out_scroll + max(3, out_vis // 2))
                    elif is_action(act, ch, "KEY1", "c") and not busy:
                        apt_c = _count_installed("apt")
                        pip_c = _count_installed("pip")
                        stats_cache["apt"] = apt_c
                        stats_cache["pip"] = pip_c
                        _draw_count(lcd, fonts, apt_c, pip_c, pkg_mode)
                        view = "count"
                        continue

                    time.sleep(0.10 if busy else 0.04)
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
