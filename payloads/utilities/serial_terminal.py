#!/usr/bin/env python3
"""
EmberJack Payload -- Serial Terminal (Multi-port)
===================================================
Author: 7h30th3r0n3

Multi-port serial console styled to match EmberJack's Obsidian Ember terminal
(`rj_terminal.py`) with auto-baud detection, dynamic monospace viewport,
port/baud selector, and full physical keyboard typing.

Controls:
  LEFT / RIGHT / Tab  Switch between detected serial ports
  UP / DOWN           Scroll RX history (connected) / Select baud (setup)
  PageUp / PageDown   Fast scroll history
  OK / Enter          Connect (setup) / Send typed line (connected)
  KEY1 / A / Ctrl+B   Auto-detect baud rate (KEY1 / R to rescan when no ports)
  KEY2 / Ctrl+C       Send Ctrl+C (connected) / KEY2 / R Rescan (setup)
  Ctrl+L              Clear console buffer
  Ctrl+D              Disconnect / Reconnect toggle
  KEY3 / Esc          Exit
  Keyboard            Direct command typing & send
"""

import glob
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

import rj_env
import rj_theme
from payloads._input_helper import (
    flush_input,
    format_footer_hint,
    is_action,
    is_back,
    is_confirm,
    poll_ui_input,
    text_input_mode,
)

try:
    import evdev
    from evdev import InputDevice, ecodes
    EVDEV_OK = True
except ImportError:
    EVDEV_OK = False

try:
    import serial
except ImportError:
    subprocess.run(["pip3", "install", "--break-system-packages", "pyserial"],
                   capture_output=True, timeout=60)
    import serial

PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
for p in PINS.values():
    GPIO.setup(p, GPIO.IN, pull_up_down=GPIO.PUD_UP)

time.sleep(0.1)
_STUCK_PINS = {name for name, pin in PINS.items() if GPIO.input(pin) == 0}

LCD = LCD_1in44.LCD()
LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
W, H = LCD.width, LCD.height
FONTS = rj_theme.load_app_fonts(W, H)
IS_WIDE = bool(FONTS["wide"])
IS_LARGE = bool(FONTS["large"])
IS_MID = (not IS_WIDE) and W >= 200
ET = rj_theme.EMBER_THEME

BAUDS = [300, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
_running = True


# Compute dynamic monospace console metrics (matching rj_terminal.py style)
def _compute_console_metrics():
    if IS_LARGE:
        mono_px, hdr_h, tab_h, prompt_h, ftr_h, pad_x = 12, 26, 22, 24, 20, 8
    elif IS_WIDE:
        mono_px, hdr_h, tab_h, prompt_h, ftr_h, pad_x = 10, 20, 16, 19, 16, 5
    elif W >= 200:
        mono_px, hdr_h, tab_h, prompt_h, ftr_h, pad_x = 9, 18, 15, 18, 14, 4
    else:
        mono_px, hdr_h, tab_h, prompt_h, ftr_h, pad_x = 8, 14, 0, 15, 12, 3

    mono_font = rj_env.load_font(mono_px, mono=True)
    mono_bold = rj_env.load_font(mono_px, bold=True, mono=True)
    bb = rj_theme.measure_text(mono_font, "M")
    char_w = max(5, bb[2] - bb[0])
    line_h = max(mono_px + 2, (bb[3] - bb[1]) + 3)
    vp_y0 = hdr_h + tab_h
    vp_y1 = H - ftr_h - prompt_h - 4
    max_lines = max(4, (vp_y1 - vp_y0 - 4) // line_h)
    max_cols = max(16, (W - pad_x * 2 - 8) // char_w)
    return {
        "mono_font": mono_font,
        "mono_bold": mono_bold,
        "hdr_h": hdr_h,
        "tab_h": tab_h,
        "prompt_h": prompt_h,
        "ftr_h": ftr_h,
        "pad_x": pad_x,
        "char_w": char_w,
        "line_h": line_h,
        "vp_y0": vp_y0,
        "vp_y1": vp_y1,
        "max_lines": max_lines,
        "max_cols": max_cols,
    }


M = _compute_console_metrics()

_EVDEV_CHARS = {
    2: '1', 3: '2', 4: '3', 5: '4', 6: '5', 7: '6', 8: '7', 9: '8', 10: '9', 11: '0',
    16: 'q', 17: 'w', 18: 'e', 19: 'r', 20: 't', 21: 'y', 22: 'u', 23: 'i', 24: 'o', 25: 'p',
    30: 'a', 31: 's', 32: 'd', 33: 'f', 34: 'g', 35: 'h', 36: 'j', 37: 'k', 38: 'l',
    44: 'z', 45: 'x', 46: 'c', 47: 'v', 48: 'b', 49: 'n', 50: 'm',
    57: ' ', 12: '-', 13: '=', 52: '.', 53: '/', 39: ';', 40: "'",
    26: '[', 27: ']', 43: '\\', 51: ',',
}

_EVDEV_SHIFT = {
    2: '!', 3: '@', 4: '#', 5: '$', 6: '%', 7: '^', 8: '&', 9: '*', 10: '(', 11: ')',
    12: '_', 13: '+', 52: '>', 53: '?', 39: ':', 40: '"',
    26: '{', 27: '}', 43: '|', 51: '<',
}


def _sig(s, f):
    global _running
    _running = False


signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)


def _poll_input():
    act, ch = poll_ui_input(PINS, GPIO)
    if act and act in _STUCK_PINS and ch is None:
        return None, None
    return act, ch


class SerialPort:
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.baud = 115200
        self.ser = None
        self.connected = False
        self.lines = []
        self.version = 0
        self.lock = threading.Lock()
        self.thread = None

    def connect(self):
        try:
            self.ser = serial.Serial(self.path, self.baud, timeout=0.2)
            self.connected = True
            self.append_sys(f"[Connected {self.path} @ {self.baud} 8N1]")
            self.thread = threading.Thread(target=self._rx_loop, daemon=True)
            self.thread.start()
            return True
        except Exception as exc:
            self.connected = False
            self.append_sys(f"[ERR] {str(exc)[:60]}")
            return False

    def disconnect(self):
        was = self.connected
        self.connected = False
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        if was:
            self.append_sys(f"[Disconnected {self.path}]")

    def append_sys(self, msg):
        with self.lock:
            self.lines.append(msg)
            if len(self.lines) > 500:
                self.lines = self.lines[-500:]
            self.version += 1

    def send(self, data):
        if self.ser and self.connected:
            try:
                self.ser.write(data.encode())
            except Exception:
                pass

    def send_line(self, line):
        self.send(line + "\r\n")
        with self.lock:
            self.lines.append(f"> {line}")
            if len(self.lines) > 500:
                self.lines = self.lines[-500:]
            self.version += 1

    def clear(self):
        with self.lock:
            self.lines = []
            self.version += 1

    def _rx_loop(self):
        while self.connected and _running:
            try:
                raw = self.ser.readline()
                if raw:
                    line = raw.decode(errors="replace").rstrip("\r\n")
                    with self.lock:
                        self.lines.append(line)
                        if len(self.lines) > 500:
                            self.lines = self.lines[-500:]
                        self.version += 1
            except Exception:
                time.sleep(0.05)

    def auto_baud(self):
        """Try baud rates and find the one producing readable ASCII."""
        best_baud = 115200
        best_score = 0.0
        was_connected = self.connected
        if was_connected:
            self.disconnect()
            time.sleep(0.2)

        for baud in [115200, 9600, 57600, 38400, 19200, 230400, 460800, 4800, 1200]:
            try:
                s = serial.Serial(self.path, baud, timeout=0.5)
                s.reset_input_buffer()
                time.sleep(0.3)
                data = s.read(256)
                s.close()
                if not data:
                    continue
                printable = sum(1 for b in data if 32 <= b <= 126 or b in (10, 13, 9))
                score = printable / len(data)
                if score > best_score:
                    best_score = score
                    best_baud = baud
                if score > 0.8:
                    break
            except Exception:
                continue

        self.baud = best_baud
        if was_connected:
            self.connect()
        return best_baud, best_score


def _scan_ports():
    ports = []
    for p in sorted(glob.glob("/dev/ttyUSB*")):
        ports.append(p)
    for p in sorted(glob.glob("/dev/ttyACM*")):
        ports.append(p)
    if os.path.exists("/dev/ttyS0"):
        ports.append("/dev/ttyS0")
    if os.path.exists("/dev/ttyAMA0") and "/dev/ttyAMA0" not in ports:
        ports.append("/dev/ttyAMA0")
    return ports


def _find_evdev_keyboard():
    if not EVDEV_OK:
        return None
    for path in evdev.list_devices():
        try:
            dev = InputDevice(path)
            if "tca8418" in dev.name.lower() or "keyboard" in dev.name.lower():
                return dev
        except Exception:
            pass
    return None


def _draw_status(title, sub="", kind="info"):
    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)
    rj_theme.draw_app_header(d, W, ">_ SERIAL", FONTS, badge="UART")
    rj_theme.draw_status_card(d, W, H, title, sub=sub, kind=kind, fonts=FONTS)
    LCD.LCD_ShowImage(img, 0, 0)


def _draw_screen(serial_ports, active_idx, tx_buf, scroll, blink_on):
    img = Image.new("RGB", (W, H), ET["bg_base"])
    d = ImageDraw.Draw(img)

    if not serial_ports:
        rj_theme.draw_app_header(
            d, W, ">_ SERIAL" if IS_WIDE else "SERIAL", FONTS, badge="0 PORTS",
            hdr_h=(30 if IS_LARGE else (22 if IS_WIDE else (22 if IS_MID else 18))),
        )
        no_port_foot = format_footer_hint(
            W, H,
            hat_hint="K1:Rescan K3:Exit",
            hat_240_hint="K1: Rescan  •  K3: Exit",
            cardputer_hint="R: Rescan  •  Esc: Exit",
            hdmi_hint="R: Rescan   •   Esc: Exit",
        )
        rj_theme.draw_status_card(
            d, W, H, "No Serial Ports Found",
            sub="Plug in USB-TTL / UART adapter",
            kind="warn",
            foot=no_port_foot,
            fonts=FONTS,
        )
        rj_theme.draw_app_footer(
            d, W, H, no_port_foot, FONTS,
            ftr_h=(22 if IS_LARGE else (16 if IS_WIDE else (16 if IS_MID else 13))),
        )
        LCD.LCD_ShowImage(img, 0, 0)
        return

    sp = serial_ports[active_idx]
    hdr_h = M["hdr_h"]
    tab_h = M["tab_h"]
    prompt_h = M["prompt_h"]
    ftr_h = M["ftr_h"]
    vp_y0 = M["vp_y0"]
    vp_y1 = M["vp_y1"]
    pad_x = M["pad_x"]

    # 1. rj_terminal-style Obsidian Ember Header
    d.rectangle((0, 0, W - 1, hdr_h - 1), fill=ET["bg_elevated"])
    d.rectangle((0, 0, 3 if IS_WIDE else 2, hdr_h - 1), fill=ET["accent"])
    d.line([(0, hdr_h - 1), (W - 1, hdr_h - 1)], fill=ET["border_subtle"], width=1)

    bx = 8 if IS_WIDE else 4
    badge_txt = ">_ SERIAL" if IS_WIDE else ">_"
    pw, _ = rj_theme.draw_pill(
        d, bx, max(1, (hdr_h - (16 if IS_LARGE else 12)) // 2), badge_txt, FONTS["badge"],
        bg=ET["select_bg"], fg=ET["accent_bright"], outline=ET["border_accent"],
        pad_x=5 if IS_WIDE else 3, pad_y=1, radius=3,
    )

    # Right header pills: Scroll indicator + Status + Baud
    rx = W - (8 if IS_WIDE else 3)
    st_lbl = "LIVE" if sp.connected else "IDLE"
    st_bg = ET["ok_bg"] if sp.connected else ET["warn_bg"]
    st_fg = ET["ok"] if sp.connected else ET["warn"]
    sb = rj_theme.measure_text(FONTS["badge"], st_lbl, d)
    sw = (sb[2] - sb[0]) + (8 if IS_WIDE else 4)
    rj_theme.draw_pill(
        d, rx - sw, max(1, (hdr_h - (16 if IS_LARGE else 12)) // 2), st_lbl, FONTS["badge"],
        bg=st_bg, fg=st_fg, outline=st_fg, pad_x=4 if IS_WIDE else 2, pad_y=1, radius=3,
    )
    rx -= sw + (5 if IS_WIDE else 3)

    baud_lbl = f"{sp.baud} 8N1" if IS_LARGE else str(sp.baud)
    bb = rj_theme.measure_text(FONTS["badge"], baud_lbl, d)
    bw = (bb[2] - bb[0]) + (8 if IS_WIDE else 4)
    rj_theme.draw_pill(
        d, rx - bw, max(1, (hdr_h - (16 if IS_LARGE else 12)) // 2), baud_lbl, FONTS["badge"],
        bg=ET["bg_surface"], fg=ET["text_primary"], outline=ET["border_subtle"],
        pad_x=4 if IS_WIDE else 2, pad_y=1, radius=3,
    )
    rx -= bw + (5 if IS_WIDE else 3)

    if scroll > 0 and IS_WIDE:
        sc_lbl = f"SCROLL -{scroll}"
        scb = rj_theme.measure_text(FONTS["badge"], sc_lbl, d)
        scw = (scb[2] - scb[0]) + 8
        rj_theme.draw_pill(
            d, rx - scw, max(1, (hdr_h - (16 if IS_LARGE else 12)) // 2), sc_lbl, FONTS["badge"],
            bg=ET["warn_bg"], fg=ET["warn"], outline=ET["border_accent"],
            pad_x=4, pad_y=1, radius=3,
        )
        rx -= scw + 5

    tx0 = bx + pw + (8 if IS_WIDE else 4)
    if rx - tx0 > 24:
        ptxt = rj_theme.truncate_text(d, sp.path if IS_LARGE else sp.name, rx - tx0 - 4, FONTS["sub"])
        d.text((tx0, hdr_h // 2), ptxt, font=FONTS["sub"], fill=ET["text_muted"], anchor="lm")

    # 2. Multi-port Tab Strip (for screens > 128px)
    if tab_h > 0:
        d.rectangle((0, hdr_h, W - 1, hdr_h + tab_h - 1), fill=ET["bg_surface"])
        d.line([(0, hdr_h + tab_h - 1), (W - 1, hdr_h + tab_h - 1)], fill=ET["border_subtle"], width=1)
        n_tabs = len(serial_ports)
        tab_w = min(W // max(1, n_tabs), 140 if IS_LARGE else 100)
        for i, p in enumerate(serial_ports):
            tx = i * tab_w
            is_act = (i == active_idx)
            d.rectangle((tx, hdr_h, tx + tab_w - 2, hdr_h + tab_h - 2), fill=ET["select_bg"] if is_act else ET["bg_surface"])
            if is_act:
                d.rectangle((tx, hdr_h + tab_h - 2, tx + tab_w - 2, hdr_h + tab_h - 1), fill=ET["accent"])
            dot_col = ET["ok"] if p.connected else ET["text_dim"]
            dy = hdr_h + tab_h // 2
            d.ellipse((tx + 6, dy - 2, tx + 10, dy + 2), fill=dot_col)
            lbl = rj_theme.truncate_text(d, p.name, tab_w - 18, FONTS["badge"])
            d.text((tx + 14, dy), lbl, font=FONTS["badge"], fill=ET["accent_bright"] if is_act else ET["text_muted"], anchor="lm")

    # 3. Recessed Console Viewport (bg_well matching rj_terminal.py)
    d.rectangle((0, vp_y0, W - 1, vp_y1), fill=ET["bg_well"])
    with sp.lock:
        lines = list(sp.lines)

    max_lines = M["max_lines"]
    max_cols = M["max_cols"]
    line_h = M["line_h"]

    if not sp.connected:
        # Port & Baud Setup Card inside viewport
        vp_h = max(48, vp_y1 - vp_y0)
        cw = min(W - (16 if IS_LARGE else 10), 420 if IS_LARGE else W - 10)
        ch = min(vp_h - 6, 150 if IS_LARGE else (86 if IS_WIDE else (106 if IS_MID else vp_h - 4)))
        cx0 = (W - cw) // 2
        cy0 = vp_y0 + max(2, (vp_h - ch) // 2)
        rj_theme.draw_card(
            d, (cx0, cy0, cx0 + cw, cy0 + ch),
            fill=ET["bg_surface"], outline=ET["border_accent"],
            radius=8 if IS_WIDE else (5 if IS_MID else 4), accent_left=ET["accent"], accent_width=3 if (IS_WIDE or IS_MID) else 2,
        )
        ix = cx0 + (16 if IS_LARGE else (10 if IS_MID else 8))
        d.text((ix, cy0 + (14 if IS_LARGE else (6 if IS_WIDE else (8 if IS_MID else 5)))), rj_theme.truncate_text(d, f"PORT SETUP  •  {sp.name}", cw - 20, FONTS["title"]), font=FONTS["title"], fill=ET["accent_bright"])
        d.text((ix, cy0 + (38 if IS_LARGE else (22 if IS_WIDE else (25 if IS_MID else 18)))), rj_theme.truncate_text(d, f"Device: {sp.path}", cw - 24, FONTS["mono"]), font=FONTS["mono"], fill=ET["text_muted"])

        # Baud rate selector pill
        by = cy0 + (66 if IS_LARGE else (40 if IS_WIDE else (46 if IS_MID else 34)))
        d.text((ix, by + 3), "Baud:", font=FONTS["body"], fill=ET["text_primary"])
        rj_theme.draw_pill(
            d, ix + (56 if IS_LARGE else (42 if (IS_WIDE or IS_MID) else 34)), by, f"< {sp.baud} >", FONTS["mono"],
            bg=ET["select_bg"], fg=ET["accent_bright"], outline=ET["border_accent"],
            pad_x=8 if IS_LARGE else 4, pad_y=2, radius=4,
        )
        if IS_LARGE:
            d.text((ix, cy0 + ch - 26), "Enter: Connect  •  Up/Dn: Baud  •  A: Auto-Baud  •  R: Rescan", font=FONTS["sub"], fill=ET["text_muted"])
    else:
        # Render monospace console lines
        total = len(lines)
        start = max(0, total - max_lines - scroll)
        visible = lines[start:start + max_lines]
        ty = vp_y0 + 3
        for ln in visible:
            if ty + line_h > vp_y1 + 1:
                break
            if ln.startswith("> "):
                col = ET["accent_bright"]
            elif ln.startswith("[ERR]"):
                col = ET["danger"]
            elif ln.startswith("["):
                col = ET["info"]
            else:
                col = ET["text_primary"]
            d.text((pad_x, ty), ln[:max_cols], font=M["mono_font"], fill=col)
            ty += line_h

        # Right-edge scrollbar
        if total > max_lines:
            rj_theme.draw_scrollbar(
                d, W - (5 if IS_WIDE else 3), vp_y0 + 3, vp_y1 - 3,
                start, max_lines, total, width=3 if IS_WIDE else 2,
            )

    # 4. Command Input Prompt Bar
    py0 = vp_y1 + 2
    py1 = H - ftr_h - 2
    rj_theme.draw_card(
        d, (2, py0, W - 3, py1),
        fill=ET["bg_surface"],
        outline=ET["border_accent"] if sp.connected else ET["border_subtle"],
        radius=4 if IS_WIDE else 2,
    )
    prompt_prefix = "> "
    avail_chars = max(8, max_cols - 3)
    shown_tx = tx_buf[-avail_chars:] if len(tx_buf) > avail_chars else tx_buf
    text_y = (py0 + py1) // 2
    d.text((pad_x + 2, text_y), prompt_prefix + shown_tx, font=M["mono_font"], fill=ET["accent_bright"], anchor="lm")
    if sp.connected and blink_on:
        tb = rj_theme.measure_text(M["mono_font"], prompt_prefix + shown_tx, d)
        cx = pad_x + 2 + (tb[2] - tb[0]) + 1
        ch_h = max(8, (py1 - py0) - 6)
        if cx + M["char_w"] < W - 4:
            d.rectangle((cx, text_y - ch_h // 2, cx + max(5, M["char_w"] - 1), text_y + ch_h // 2), fill=ET["accent"])

    # 5. Footer Hint Bar
    if sp.connected:
        hint = format_footer_hint(
            W, H,
            hat_hint="OK:Tx K1:Bd K2:^C K3:X",
            hat_240_hint="OK:Send K1:AutoBd K2:^C K3:Exit",
            cardputer_hint="Ent:Send ^B:Baud ^C:Sig ^D:Disc Esc:Exit",
            hdmi_hint="Type+Enter:Send  Up/Dn:Scroll  L/R/Tab:Port  ^B:AutoBaud  ^C:Sig  ^D:Disc  ^L:Clr  Esc:Exit",
        )
    else:
        hint = format_footer_hint(
            W, H,
            hat_hint="OK:Con K1:Bd K2:Scn K3:X",
            hat_240_hint="OK:Conn U/D:Baud K1:Auto K2:Scan K3:Exit",
            cardputer_hint="Ent:Conn U/D:Baud A:Auto R:Scan Esc:Exit",
            hdmi_hint="Enter:Connect  Up/Dn:Baud  L/R/Tab:Port  A:AutoBaud  R:Rescan  Esc:Exit",
        )
    rj_theme.draw_app_footer(d, W, H, hint, FONTS, ftr_h=ftr_h)

    LCD.LCD_ShowImage(img, 0, 0)


def main():
    global _running

    port_paths = _scan_ports()
    serial_ports = [SerialPort(p) for p in port_paths]
    active_idx = 0
    tx_buf = ""
    scroll = 0
    kbd = None if getattr(LCD, "display_type", "") == "HDMI" else _find_evdev_keyboard()
    shift_held = False

    if serial_ports:
        _draw_status("Scanning Serial Ports...", f"Found {len(serial_ports)} port(s)", kind="info")
        for sp in serial_ports:
            sp.auto_baud()
            sp.connect()

    dirty = True
    last_rx_ver = -1
    last_blink = -1

    try:
        with text_input_mode():
            flush_input()
            while _running:
                now = time.time()
                act, ch = _poll_input()

                in_console = bool(serial_ports and serial_ports[active_idx].connected)
                if is_back(act, allow_backspace=not in_console):
                    break

                if serial_ports:
                    sp = serial_ports[active_idx]

                    # Port tab switching
                    if act == "LEFT" and len(serial_ports) > 1:
                        active_idx = (active_idx - 1) % len(serial_ports)
                        scroll = 0
                        dirty = True
                    elif act in ("RIGHT", "TAB") and len(serial_ports) > 1:
                        active_idx = (active_idx + 1) % len(serial_ports)
                        scroll = 0
                        dirty = True
                    elif act == "CTRL_D":
                        if sp.connected:
                            sp.disconnect()
                        else:
                            sp.connect()
                        dirty = True
                    elif not sp.connected:
                        if is_confirm(act):
                            sp.connect()
                            dirty = True
                        elif act == "UP":
                            idx = BAUDS.index(sp.baud) if sp.baud in BAUDS else 0
                            sp.baud = BAUDS[(idx + 1) % len(BAUDS)]
                            dirty = True
                        elif act == "DOWN":
                            idx = BAUDS.index(sp.baud) if sp.baud in BAUDS else 0
                            sp.baud = BAUDS[(idx - 1) % len(BAUDS)]
                            dirty = True
                        elif is_action(act, ch, "KEY1", "a"):
                            _draw_status("Auto-Detecting Baud...", sp.path, kind="info")
                            baud, score = sp.auto_baud()
                            _draw_status(f"Detected {baud} baud", f"ASCII match: {int(score * 100)}%", kind="ok")
                            time.sleep(0.9)
                            if not sp.connected:
                                sp.connect()
                            dirty = True
                        elif is_action(act, ch, "KEY2", "r"):
                            for p_old in serial_ports:
                                p_old.disconnect()
                            serial_ports = [SerialPort(p) for p in _scan_ports()]
                            active_idx = 0
                            dirty = True
                    else:
                        # Connected console input
                        if act == "UP":
                            scroll = min(scroll + 1, max(0, len(sp.lines) - M["max_lines"]))
                            dirty = True
                        elif act == "DOWN":
                            scroll = max(0, scroll - 1)
                            dirty = True
                        elif act == "PAGEUP":
                            scroll = min(scroll + M["max_lines"] // 2, max(0, len(sp.lines) - M["max_lines"]))
                            dirty = True
                        elif act == "PAGEDOWN":
                            scroll = max(0, scroll - M["max_lines"] // 2)
                            dirty = True
                        elif act in ("KEY1", "CTRL_B"):
                            _draw_status("Auto-Detecting Baud...", sp.path, kind="info")
                            baud, score = sp.auto_baud()
                            _draw_status(f"Detected {baud} baud", f"ASCII match: {int(score * 100)}%", kind="ok")
                            time.sleep(0.9)
                            if not sp.connected:
                                sp.connect()
                            dirty = True
                        elif act in ("KEY2", "CTRL_C"):
                            sp.send("\x03")
                            sp.append_sys("^C")
                            dirty = True
                        elif act == "CTRL_L":
                            sp.clear()
                            scroll = 0
                            dirty = True
                        elif act == "CTRL_U":
                            tx_buf = ""
                            dirty = True
                        elif is_confirm(act):
                            sp.send_line(tx_buf)
                            tx_buf = ""
                            scroll = 0
                            dirty = True
                        elif act in ("BACKSPACE", "DELETE"):
                            if tx_buf:
                                tx_buf = tx_buf[:-1]
                                dirty = True
                        elif isinstance(ch, str) and len(ch) == 1 and ord(ch) >= 32:
                            tx_buf += ch
                            dirty = True
                else:
                    # No ports detected yet
                    if is_action(act, ch, "KEY1", "r"):
                        serial_ports = [SerialPort(p) for p in _scan_ports()]
                        for sp in serial_ports:
                            sp.auto_baud()
                            sp.connect()
                        active_idx = 0
                        dirty = True

                # Direct TCA8418 / Cardputer evdev fallback (non-HDMI)
                if kbd and serial_ports and serial_ports[active_idx].connected:
                    try:
                        while True:
                            ev = kbd.read_one()
                            if ev is None:
                                break
                            if ev.type != ecodes.EV_KEY:
                                continue
                            if ev.value == 1:
                                code = ev.code
                                if code in (42, 54):
                                    shift_held = True
                                    continue
                                dirty = True
                                if code == 28:
                                    serial_ports[active_idx].send_line(tx_buf)
                                    tx_buf = ""
                                    scroll = 0
                                elif code == 14:
                                    tx_buf = tx_buf[:-1]
                                elif code == 1:
                                    serial_ports[active_idx].send("\x03")
                                else:
                                    if shift_held:
                                        c_ev = _EVDEV_SHIFT.get(code, "") or _EVDEV_CHARS.get(code, "").upper()
                                    else:
                                        c_ev = _EVDEV_CHARS.get(code, "")
                                    if c_ev:
                                        tx_buf += c_ev
                            elif ev.value == 0 and ev.code in (42, 54):
                                shift_held = False
                    except Exception:
                        pass

                cur_rx_ver = serial_ports[active_idx].version if serial_ports else 0
                cur_blink = (int(now * 2) % 2 == 0) if (serial_ports and serial_ports[active_idx].connected) else False
                if dirty or cur_rx_ver != last_rx_ver or cur_blink != last_blink:
                    _draw_screen(serial_ports, active_idx, tx_buf, scroll, cur_blink)
                    last_rx_ver = cur_rx_ver
                    last_blink = cur_blink
                    dirty = False
                time.sleep(0.03)
    finally:
        for sp in serial_ports:
            sp.disconnect()
        try:
            LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
            LCD.LCD_Clear()
        except Exception:
            pass
        try:
            GPIO.cleanup()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
