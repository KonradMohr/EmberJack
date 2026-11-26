#!/usr/bin/env python3
"""
EmberJack Payload -- Obsidian Ember IRC Client
==============================================
Author: 7h30th3r0n3

Connects to an IRC server and displays channel messages in a crisp
widescreen Obsidian Ember viewport with channel tabs, user sidebar,
and message composer via the shared keyboard helper.

Controls:
  UP / DOWN / PgUp / PgDn  -- Scroll chat history (END = jump to live)
  LEFT / RIGHT / TAB       -- Switch channel
  ENTER / OK / KEY1 / I    -- Compose & send message (supports /join, /nick)
  KEY2 / U                 -- Show / hide user list
  R                        -- Reconnect if disconnected
  KEY3 / ESC / Q           -- Back / Exit

Config: <base_dir>/loot/IRC/config.json
"""

import json
import os
import random
import signal
import socket
import sys
import threading
import time
from datetime import datetime

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import LCD_Config
from PIL import Image, ImageDraw

import rj_env
import rj_theme
from payloads._input_helper import flush_input, poll_ui_input, text_input_mode
from payloads._keyboard_helper import lcd_keyboard

ET = rj_theme.EMBER_THEME

PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}

CONFIG_DIR = os.path.join(rj_env.get_base_dir(), "loot", "IRC")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
MAX_MESSAGES = 150
DEFAULT_CONFIG = {
    "server": "irc.libera.chat",
    "port": 6667,
    "nick": None,
    "channels": ["#emberjack"],
}

# Warm, readable Obsidian Ember nick palette
_NICK_COLORS = (
    "#F2994A",  # tangerine
    "#5EB87A",  # soft sage
    "#6E9ECE",  # soft slate blue
    "#C284B8",  # muted mauve
    "#66B8B2",  # muted teal
    "#E6A845",  # honey gold
    "#D98C4A",  # copper
)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
running = True
irc_sock = None
sock_lock = threading.Lock()
state_lock = threading.Lock()

server = "irc.libera.chat"
port = 6667
nick = ""
channels = ["#emberjack"]
channel_idx = 0

messages = {}  # {channel: [{"nick": str, "text": str, "ts": str}]}
users = {}     # {channel: [str]}

scroll_from_bottom = 0  # 0 = pinned to live bottom; >0 = lines scrolled up
view_mode = "chat"      # "chat" or "users" (compact)
show_sidebar = True     # widescreen users sidebar toggle
connected = False
status_msg = "Connecting..."
_state_version = 0


def cleanup(*_args):
    global running
    running = False


signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def generate_nick():
    digits = random.randint(1000, 9999)
    return f"EmberJack_{digits}"


def load_config():
    global server, port, nick, channels
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        server = str(cfg.get("server", "irc.libera.chat") or "irc.libera.chat")
        port = int(cfg.get("port", 6667) or 6667)
        nick = str(cfg.get("nick", "") or "") or generate_nick()
        loaded_channels = cfg.get("channels", ["#emberjack"])
        if isinstance(loaded_channels, list) and loaded_channels:
            channels = [str(c) for c in loaded_channels if c]
    except Exception:
        nick = generate_nick()


def save_config():
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        cfg = {
            "server": server,
            "port": port,
            "nick": nick,
            "channels": channels,
        }
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# IRC protocol helpers
# ---------------------------------------------------------------------------

def _bump_version():
    global _state_version
    _state_version += 1


def irc_send(line):
    with sock_lock:
        if irc_sock is not None:
            try:
                irc_sock.sendall((line + "\r\n").encode("utf-8", errors="replace"))
            except Exception:
                pass


def add_message(channel, nick_name, text):
    ts = datetime.now().strftime("%H:%M")
    with state_lock:
        if channel not in messages:
            messages[channel] = []
        messages[channel].append({"nick": nick_name, "text": text, "ts": ts})
        if len(messages[channel]) > MAX_MESSAGES:
            messages[channel] = messages[channel][-MAX_MESSAGES:]
        _bump_version()


def current_channel():
    with state_lock:
        if not channels:
            return "#emberjack"
        return channels[channel_idx % len(channels)]


def current_messages():
    ch = current_channel()
    with state_lock:
        return list(messages.get(ch, []))


def current_users():
    ch = current_channel()
    with state_lock:
        return list(users.get(ch, []))


def irc_receive_thread():
    global connected, status_msg
    buf = ""

    while running:
        with sock_lock:
            sock = irc_sock
        if sock is None:
            time.sleep(0.1)
            continue

        try:
            data = sock.recv(4096)
        except socket.timeout:
            continue
        except Exception:
            with state_lock:
                status_msg = "Disconnected"
                connected = False
                _bump_version()
            break

        if not data:
            with state_lock:
                status_msg = "Disconnected"
                connected = False
                _bump_version()
            break

        buf += data.decode("utf-8", errors="replace")
        while "\r\n" in buf:
            line, buf = buf.split("\r\n", 1)
            handle_irc_line(line)


def handle_irc_line(line):
    global connected, status_msg

    if line.startswith("PING"):
        payload = line[5:] if len(line) > 5 else ""
        irc_send(f"PONG {payload}")
        return

    parts = line.split(" ")
    if len(parts) < 2:
        return

    prefix = parts[0]
    command = parts[1]

    if command == "001":
        with state_lock:
            connected = True
            status_msg = "Connected"
            chs = list(channels)
            _bump_version()
        for ch in chs:
            irc_send(f"JOIN {ch}")
        return

    if command == "353" and len(parts) >= 5:
        chan_part = parts[4]
        user_str = line.split(":", 2)[-1] if line.count(":") >= 2 else ""
        user_list = [u.lstrip("@+%~&") for u in user_str.split() if u]
        with state_lock:
            existing = set(users.get(chan_part, []))
            existing.update(user_list)
            users[chan_part] = sorted(existing, key=str.lower)
            _bump_version()
        return

    if command == "366":
        return

    if command == "PRIVMSG" and len(parts) >= 3:
        sender = prefix.split("!")[0].lstrip(":")
        target = parts[2]
        text = line.split(":", 2)[-1] if line.count(":") >= 2 else ""
        channel_name = target if target.startswith("#") else sender
        add_message(channel_name, sender, text)
        return

    if command == "JOIN":
        sender = prefix.split("!")[0].lstrip(":")
        chan = parts[2].lstrip(":") if len(parts) > 2 else ""
        if chan:
            add_message(chan, "*", f"{sender} joined")
            with state_lock:
                ulist = users.setdefault(chan, [])
                if sender not in ulist:
                    ulist.append(sender)
                    ulist.sort(key=str.lower)
                _bump_version()
        return

    if command in ("PART", "QUIT"):
        sender = prefix.split("!")[0].lstrip(":")
        chan = parts[2].lstrip(":") if len(parts) > 2 and command == "PART" else ""
        if chan:
            add_message(chan, "*", f"{sender} left")
            with state_lock:
                if chan in users:
                    users[chan] = [u for u in users[chan] if u != sender]
                _bump_version()
        return

    if command in ("433", "432"):
        with state_lock:
            status_msg = "Nick in use / invalid"
            _bump_version()
        return


def connect_irc():
    global irc_sock, connected, status_msg

    with state_lock:
        status_msg = f"Connecting to {server}:{port}..."
        connected = False
        _bump_version()

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect((server, port))
        sock.settimeout(2)
    except Exception as exc:
        with state_lock:
            status_msg = f"Connection error: {str(exc)[:48]}"
            connected = False
            _bump_version()
        return False

    with sock_lock:
        irc_sock = sock

    irc_send(f"NICK {nick}")
    irc_send(f"USER {nick} 0 * :EmberJack IRC")

    recv_thread = threading.Thread(target=irc_receive_thread, daemon=True)
    recv_thread.start()
    return True


def disconnect_irc():
    global irc_sock, connected
    irc_send("QUIT :EmberJack signing off")
    time.sleep(0.2)
    with sock_lock:
        if irc_sock is not None:
            try:
                irc_sock.close()
            except Exception:
                pass
            irc_sock = None
    with state_lock:
        connected = False
        _bump_version()


def _nick_color(name: str) -> str:
    if name == "*":
        return ET["text_dim"]
    if name == nick:
        return ET["accent_bright"]
    return _NICK_COLORS[sum(ord(c) for c in name) % len(_NICK_COLORS)]


# ---------------------------------------------------------------------------
# Native Resolution-Adaptive Obsidian Ember Renderers
# ---------------------------------------------------------------------------

def render_connecting_frame(width, height, fonts=None):
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = f["wide"]
    large = f["large"]
    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    hdr_h = rj_theme.draw_app_header(d, width, "EMBERJACK IRC" if wide else "IRC", f, badge="OFFLINE", right_text=server)
    ftr_h = rj_theme.draw_app_footer(d, width, height, "R: Reconnect  •  Esc / K3: Exit" if wide else "R:Reconn K3:X", f)

    pad = 14 if large else (8 if wide else 4)
    cw = min(width - pad * 2, 460 if large else width - pad * 2)
    ch = min(height - hdr_h - ftr_h - pad * 2, 180 if large else 108)
    cx0 = (width - cw) // 2
    cy0 = hdr_h + max(pad, (height - hdr_h - ftr_h - ch) // 2)
    cx1, cy1 = cx0 + cw, cy0 + ch

    is_err = "err" in status_msg.lower() or "disconnect" in status_msg.lower()
    accent = ET["danger"] if is_err else ET["accent"]
    rj_theme.draw_card(
        d, (cx0, cy0, cx1, cy1),
        fill=ET["bg_surface"], outline=accent,
        radius=8 if wide else 4, accent_left=accent, accent_width=4 if wide else 2,
    )

    tx = cx0 + (16 if wide else 8)
    avail = max(40, cw - (28 if wide else 14))
    y = cy0 + (14 if large else (8 if wide else 4))
    d.text((tx, y), rj_theme.truncate_text(d, status_msg, avail, f["title"]), font=f["title"], fill=ET["danger"] if is_err else ET["accent_bright"])
    y += 24 if large else (16 if wide else 12)

    rows = [
        ("Server", f"{server}:{port}"),
        ("Nick", nick),
        ("Channels", ", ".join(channels)),
    ]
    step = 20 if large else (14 if wide else 11)
    for kl, vl in rows:
        if y + step > cy1 - 8:
            break
        d.text((tx, y), f"{kl}:", font=f["sub"], fill=ET["text_muted"])
        d.text((tx + (66 if wide else 42), y), rj_theme.truncate_text(d, vl, max(30, avail - 68), f["body"]), font=f["body"], fill=ET["text_primary"])
        y += step
    return img


def render_irc_frame(width, height, fonts=None):
    """Pure PIL native resolution-adaptive Obsidian Ember IRC client renderer."""
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = f["wide"]
    large = f["large"]
    split = width >= 480

    with state_lock:
        is_conn = connected
        ch_list = list(channels)
        cur_ch = ch_list[channel_idx % len(ch_list)] if ch_list else "#emberjack"
        msgs = list(messages.get(cur_ch, []))
        ulist = list(users.get(cur_ch, []))
        sc_up = scroll_from_bottom
        vmode = view_mode
        sbar = show_sidebar

    if not is_conn and not msgs:
        return render_connecting_frame(width, height, fonts=f), 0, 8

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    hdr_h = rj_theme.draw_app_header(
        d, width, cur_ch, f,
        badge="ONLINE" if is_conn else "OFFLINE",
        right_text=f"{nick} @ {server}",
        right_pill=f"{len(ulist)} USERS",
    )

    if vmode == "users" and not split:
        hint = "KEY2 / U / Esc: Back to chat" if wide else "K2/K3: Back"
    elif large:
        hint = "Enter/K1:Message  •  Up/Dn/PgUp/PgDn:Scroll  •  L/R/Tab:Channel  •  K2/U:Users  •  Esc/Q:Exit"
    elif wide:
        hint = "OK:Msg  L/R:Chan  K2/U:Users  K3:Exit"
    else:
        hint = "OK:Msg K2:Usr K3:X"
    ftr_h = rj_theme.draw_app_footer(d, width, height, hint, f, right_text="SCROLLBACK" if sc_up > 0 else "LIVE")

    # Channel strip on wide screens
    tab_h = (22 if large else 16) if (wide and len(ch_list) > 0) else 0
    if tab_h:
        d.rectangle((0, hdr_h, width - 1, hdr_h + tab_h - 1), fill=ET["bg_surface"])
        d.line([(0, hdr_h + tab_h - 1), (width - 1, hdr_h + tab_h - 1)], fill=ET["border_subtle"], width=1)
        tx = 8
        for c_name in ch_list:
            active = c_name == cur_ch
            pw, _ = rj_theme.draw_pill(
                d, tx, hdr_h + max(2, (tab_h - 14) // 2), c_name, f["badge"],
                bg=ET["select_bg"] if active else ET["bg_well"],
                fg=ET["accent_bright"] if active else ET["text_muted"],
                outline=ET["border_accent"] if active else ET["border_subtle"],
                pad_x=6, pad_y=1, radius=3,
            )
            tx += pw + 6
            if tx > width - 50:
                break

    pad = 8 if large else (5 if wide else 2)
    comp_h = 26 if large else (18 if wide else 0)
    y0 = hdr_h + tab_h + (6 if large else (3 if wide else 2))
    y1 = height - ftr_h - comp_h - (8 if large else (4 if wide else 2))

    # Compact full-screen users view
    if not split and vmode == "users":
        rj_theme.draw_card(d, (pad, y0, width - pad, height - ftr_h - 4), fill=ET["bg_surface"], outline=ET["border_accent"], radius=5 if wide else 3)
        uy = y0 + (6 if wide else 3)
        d.text((pad + 8, uy), f"Users in {cur_ch} ({len(ulist)})", font=f["body_bold"], fill=ET["accent_bright"])
        uy += 18 if wide else 13
        step = 15 if wide else 11
        if not ulist:
            d.text((pad + 8, uy), "No users listed yet...", font=f["sub"], fill=ET["text_muted"])
        else:
            for u in ulist:
                if uy + step > height - ftr_h - 6:
                    break
                d.ellipse((pad + 8, uy + 4, pad + 12, uy + 8), fill=ET["ok"])
                d.text((pad + 16, uy), rj_theme.truncate_text(d, u, width - pad * 2 - 24, f["body"]), font=f["body"], fill=_nick_color(u))
                uy += step
        return img, 0, 8

    # Chat viewport box (recessed Obsidian Ember well)
    side_w = (146 if large else 108) if (split and sbar) else 0
    chat_x0 = pad
    chat_x1 = width - pad - (side_w + 8 if side_w else 0)

    rj_theme.draw_card(
        d, (chat_x0, y0, chat_x1, y1),
        fill=ET["bg_well"], outline=ET["border_subtle"],
        radius=6 if wide else 3,
    )

    inner_x0 = chat_x0 + (10 if large else (6 if wide else 4))
    inner_x1 = chat_x1 - (12 if wide else 6)
    avail_w = max(40, inner_x1 - inner_x0)
    line_font = f["body"] if large else (f["sub"] if wide else f["badge"])
    line_h = 17 if large else (13 if wide else 10)

    # Build wrapped visual rows: (ts, nick_lbl, nick_col, body_text, is_sys)
    vis_rows = []
    for m in msgs:
        mnick = m.get("nick", "")
        mtext = m.get("text", "")
        mts = m.get("ts", "")
        if mnick == "*":
            prefix = f"[{mts}] * " if large else "* "
            wrapped = rj_theme.wrap_text(d, prefix + mtext, avail_w, line_font)
            for wl in wrapped:
                vis_rows.append(("", "", ET["text_dim"], wl, True))
        else:
            n_lbl = f"<{mnick}> " if wide else f"<{mnick[:7]}> "
            ts_lbl = f"{mts} " if large else ""
            ts_w = rj_theme.measure_text(f["badge"], ts_lbl, d)[2] if ts_lbl else 0
            n_w = rj_theme.measure_text(f["body_bold"] if large else line_font, n_lbl, d)[2]
            first_avail = max(30, avail_w - ts_w - n_w)
            wlines = rj_theme.wrap_text(d, mtext, first_avail, line_font)
            col = _nick_color(mnick)
            for idx_l, wl in enumerate(wlines):
                if idx_l == 0:
                    vis_rows.append((ts_lbl, n_lbl, col, wl, False))
                else:
                    vis_rows.append(("", "  ", col, wl, False))

    vis_lines = max(1, (y1 - y0 - (12 if large else 6)) // line_h)
    max_scroll_up = max(0, len(vis_rows) - vis_lines)
    eff_sc_up = max(0, min(sc_up, max_scroll_up))
    start_idx = max(0, len(vis_rows) - vis_lines - eff_sc_up)
    slice_rows = vis_rows[start_idx:start_idx + vis_lines]

    if not slice_rows:
        cy = (y0 + y1) // 2
        d.text(
            ((chat_x0 + chat_x1) // 2, cy),
            "Connected — press Enter to send a message" if wide else "No messages yet",
            font=f["sub"], fill=ET["text_dim"], anchor="mm",
        )
    else:
        ty = y0 + (6 if large else 3)
        for ts_lbl, n_lbl, ncol, body_txt, is_sys in slice_rows:
            x = inner_x0
            if is_sys:
                d.text((x, ty), rj_theme.truncate_text(d, body_txt, avail_w, line_font), font=line_font, fill=ET["text_muted"])
            else:
                if ts_lbl:
                    d.text((x, ty + 1), ts_lbl, font=f["badge"], fill=ET["text_dim"])
                    x += rj_theme.measure_text(f["badge"], ts_lbl, d)[2]
                if n_lbl:
                    nf = f["body_bold"] if large else line_font
                    d.text((x, ty), n_lbl, font=nf, fill=ncol)
                    x += rj_theme.measure_text(nf, n_lbl, d)[2]
                d.text((x, ty), rj_theme.truncate_text(d, body_txt, max(16, inner_x1 - x), line_font), font=line_font, fill=ET["text_primary"])
            ty += line_h

    if len(vis_rows) > vis_lines:
        rj_theme.draw_scrollbar(d, chat_x1 - (7 if wide else 4), y0 + 5, y1 - 5, start_idx, vis_lines, len(vis_rows), width=4 if wide else 2)

    # Widescreen Users Sidebar
    if side_w:
        sx0 = chat_x1 + 8
        sx1 = width - pad
        rj_theme.draw_card(d, (sx0, y0, sx1, y1), fill=ET["bg_surface"], outline=ET["border_subtle"], radius=6)
        sy = y0 + 8
        d.text((sx0 + 8, sy), f"USERS ({len(ulist)})", font=f["badge"], fill=ET["accent_bright"])
        sy += 16
        d.line([(sx0 + 8, sy), (sx1 - 8, sy)], fill=ET["border_subtle"], width=1)
        sy += 6
        ustep = 16 if large else 13
        if not ulist:
            d.text((sx0 + 8, sy), "Loading...", font=f["sub"], fill=ET["text_dim"])
        else:
            for u in ulist:
                if sy + ustep > y1 - 6:
                    break
                d.ellipse((sx0 + 8, sy + 5, sx0 + 12, sy + 9), fill=ET["ok"])
                d.text((sx0 + 16, sy), rj_theme.truncate_text(d, u, side_w - 24, f["sub"]), font=f["sub"], fill=_nick_color(u))
                sy += ustep

    # Message composer prompt bar (wide screens)
    if comp_h:
        cy0 = y1 + (5 if large else 3)
        cy1 = cy0 + comp_h
        rj_theme.draw_card(
            d, (pad, cy0, width - pad, cy1),
            fill=ET["bg_surface"], outline=ET["border_accent"],
            radius=5, accent_left=ET["accent"], accent_width=3,
        )
        prompt_s = f"[{nick}] Press Enter / OK to compose message to {cur_ch}..." if large else f"Enter/OK: Send to {cur_ch}"
        d.text(
            (pad + 12, cy0 + max(2, (comp_h - (14 if large else 10)) // 2)),
            rj_theme.truncate_text(d, prompt_s, width - pad * 2 - 24, f["sub"]),
            font=f["sub"], fill=ET["text_muted"],
        )

    return img, max_scroll_up, vis_lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global running, channel_idx, scroll_from_bottom, view_mode, show_sidebar, nick

    GPIO.setmode(GPIO.BCM)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    LCD_Config.GPIO_Init()
    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    lcd.LCD_Clear()
    W, H = lcd.width, lcd.height
    fonts = rj_theme.load_app_fonts(W, H)
    split = W >= 480

    load_config()
    save_config()

    for ch in channels:
        messages[ch] = []
        users[ch] = []

    try:
        with text_input_mode():
            flush_input()
            lcd.LCD_ShowImage(render_connecting_frame(W, H, fonts=fonts), 0, 0)
            connect_irc()

            # Brief non-blocking wait for initial welcome (Esc/K3 cancels cleanly)
            wait_start = time.time()
            while not connected and running and (time.time() - wait_start) < 10:
                lcd.LCD_ShowImage(render_connecting_frame(W, H, fonts=fonts), 0, 0)
                act, ch = poll_ui_input(PINS, GPIO)
                kl = ch.lower() if isinstance(ch, str) else ""
                if act in ("KEY3", "ESCAPE", "BACKSPACE") or kl == "q":
                    return 0
                time.sleep(0.15)

            last_ver = -1
            max_sc = 0
            vis_lines = 8
            dirty = True

            while running:
                if _state_version != last_ver:
                    last_ver = _state_version
                    dirty = True

                if dirty:
                    frame, max_sc, vis_lines = render_irc_frame(W, H, fonts=fonts)
                    lcd.LCD_ShowImage(frame, 0, 0)
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO)
                if act is None and ch is None:
                    time.sleep(0.04)
                    continue

                kl = ch.lower() if isinstance(ch, str) else ""

                if view_mode == "users" and not split:
                    if act in ("KEY2", "KEY3", "ESCAPE", "BACKSPACE") or kl in ("u", "q"):
                        view_mode = "chat"
                        dirty = True
                    continue

                if act in ("KEY3", "ESCAPE", "BACKSPACE") or kl == "q":
                    break
                elif act == "UP":
                    scroll_from_bottom = min(max_sc, scroll_from_bottom + 1)
                    dirty = True
                elif act == "DOWN":
                    scroll_from_bottom = max(0, scroll_from_bottom - 1)
                    dirty = True
                elif act == "PAGEUP":
                    scroll_from_bottom = min(max_sc, scroll_from_bottom + max(1, vis_lines - 1))
                    dirty = True
                elif act == "PAGEDOWN":
                    scroll_from_bottom = max(0, scroll_from_bottom - max(1, vis_lines - 1))
                    dirty = True
                elif act == "END":
                    scroll_from_bottom = 0
                    dirty = True
                elif act == "LEFT" and channels:
                    channel_idx = (channel_idx - 1) % len(channels)
                    scroll_from_bottom = 0
                    irc_send(f"NAMES {current_channel()}")
                    dirty = True
                elif act in ("RIGHT", "TAB") and channels:
                    channel_idx = (channel_idx + 1) % len(channels)
                    scroll_from_bottom = 0
                    irc_send(f"NAMES {current_channel()}")
                    dirty = True
                elif act in ("OK", "ENTER", "KEY1") or kl == "i":
                    chan = current_channel()
                    msg_text = lcd_keyboard(
                        lcd, fonts["body"], PINS, GPIO,
                        title=f"MSG {chan}",
                        charset="full",
                        max_len=200,
                    )
                    if msg_text:
                        msg_text = msg_text.strip()
                        if msg_text.startswith("/join "):
                            new_ch = msg_text.split(None, 1)[1].strip()
                            if new_ch and not new_ch.startswith("#"):
                                new_ch = "#" + new_ch
                            if new_ch:
                                with state_lock:
                                    if new_ch not in channels:
                                        channels.append(new_ch)
                                        messages.setdefault(new_ch, [])
                                        users.setdefault(new_ch, [])
                                    channel_idx = channels.index(new_ch)
                                save_config()
                                if connected:
                                    irc_send(f"JOIN {new_ch}")
                        elif msg_text.startswith("/nick "):
                            new_nick = msg_text.split(None, 1)[1].strip()
                            if new_nick:
                                nick = new_nick
                                save_config()
                                if connected:
                                    irc_send(f"NICK {nick}")
                                add_message(chan, "*", f"Nick changed to {nick}")
                        elif connected:
                            irc_send(f"PRIVMSG {chan} :{msg_text}")
                            add_message(chan, nick, msg_text)
                    scroll_from_bottom = 0
                    flush_input()
                    dirty = True
                elif act == "KEY2" or kl == "u":
                    irc_send(f"NAMES {current_channel()}")
                    if split:
                        show_sidebar = not show_sidebar
                    else:
                        view_mode = "users"
                    dirty = True
                elif kl == "r" and not connected:
                    disconnect_irc()
                    connect_irc()
                    dirty = True
    finally:
        running = False
        disconnect_irc()
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
