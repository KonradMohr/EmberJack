#!/usr/bin/env python3
"""
RaspyJack Payload -- Wardriving
================================
Author: 7h30th3r0n3 / dag nazty

Professional wardriving scanner with multi-card support,
GPS tracking, and Wigle-compatible export.

Views (cycle with KEY1):
  LIVE       Real-time AP discovery with signal bars
  GPS        Coordinates, speed, satellites, movement
  STATS      Security distribution, channel heatmap
  NETWORKS   Scrollable list of discovered APs
  EXPORT     Export status, file counts

Controls:
  OK         Start / Stop scan
  KEY1       Cycle views
  KEY2       Export data now
  KEY3       Exit (hold 2s)
  UP/DOWN    Scroll (in NETWORKS view)
  LEFT/RIGHT Change sort (NETWORKS view)

Exports: Wigle CSV, JSON, KML
Loot: /root/Raspyjack/loot/wardriving/
"""

import os
import sys
import time
import json
import csv
import sqlite3
import signal
import threading
import subprocess
import struct
import socket
from datetime import datetime, timezone
from collections import deque
_RJ_BASE_DIR = os.environ.get("RJ_BASE_DIR") or os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import LCD_Config
import rj_theme
import math
import urllib.request
from io import BytesIO
from PIL import Image, ImageDraw, ImageEnhance
from payloads._display_helper import scaled_font
from payloads._input_helper import get_button
from payloads._iface_helper import list_interfaces

_T = rj_theme.EMBER_THEME

try:
    from scapy.all import (
        Dot11, Dot11Beacon, Dot11Elt, Dot11ProbeResp, Dot11ProbeReq,
        RadioTap, sniff as scapy_sniff, conf,
    )
    SCAPY_OK = True
except ImportError:
    SCAPY_OK = False

try:
    import gpsd as gpsd_mod
    GPSD_OK = True
except ImportError:
    GPSD_OK = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}
WIDTH, HEIGHT = LCD_1in44.LCD_WIDTH, LCD_1in44.LCD_HEIGHT

LOOT_DIR = os.path.join(_RJ_BASE_DIR, 'loot', 'wardriving')
DB_PATH = os.path.join(LOOT_DIR, "networks.db")

CHANNELS_24 = list(range(1, 14))
CHANNELS_5 = [36, 40, 44, 48, 52, 56, 60, 64,
              100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140,
              149, 153, 157, 161, 165]
DWELL_24 = 0.3       # 300ms like Kismet (was 800ms)
DWELL_5 = 0.4        # slightly longer for DFS channels

VIEWS = ["live", "map", "gps", "cards", "channels", "stats", "networks", "export"]
AUTOSAVE_INTERVAL = 15   # autosave tick interval (seconds)
DB_SAVE_TICKS     = 4    # save DB every N ticks (60s)
WIGLE_SAVE_TICKS  = 8    # rewrite session Wigle CSV every N ticks (120s)
MAX_NETWORKS = 8000      # keep recent APs in RAM, old ones live in CSV/DB only
AUTO_MODE = "--auto" in sys.argv

# Known monitor drivers (from _iface_helper)
KNOWN_MONITOR_DRIVERS = {
    "rtl88XXau", "rtl8812au", "rtl8821au", "rtl88x2bu",
    "rtl8188eus", "rtl8187", "rt2800usb", "ath9k_htc",
    "mt76x2u", "mt76x0u", "mt7921u", "rtl8814au",
}

# OUI vendor lookup (top entries)
OUI_DB = {
    "00:1A:2B": "Ayecom", "00:50:F2": "Microsoft", "00:0C:29": "VMware",
    "00:1E:58": "D-Link", "00:14:6C": "Netgear", "00:1B:11": "D-Link",
    "00:24:D7": "Intel", "00:26:5A": "D-Link", "3C:37:86": "Netgear",
    "F8:E4:FB": "Apple", "D8:6C:63": "Apple", "AC:BC:32": "Apple",
    "B0:BE:76": "TP-Link", "50:C7:BF": "TP-Link", "C0:25:E9": "TP-Link",
    "E4:F0:42": "Google", "94:B8:6D": "Google", "00:1F:33": "Netgear",
    "20:CF:30": "ASUSTek", "04:D4:C4": "ASUSTek", "2C:FD:A1": "Intel",
    "DC:A6:32": "Raspberry", "B8:27:EB": "Raspberry",
    "00:23:69": "Cisco", "00:1A:A0": "Dell", "F0:9F:C2": "Ubiquiti",
    "18:E8:29": "Samsung", "00:26:B0": "Apple", "C8:69:CD": "Apple",
    "F4:F5:D8": "Google", "7C:2F:80": "Huawei", "88:71:B1": "Huawei",
    "FC:F5:28": "ZyXEL", "34:31:C4": "AVM/Fritz",
    "A4:91:B1": "Actiontec", "CC:2D:E0": "Routerboard",
    "E0:63:DA": "Ubiquiti", "24:A4:3C": "Ubiquiti",
    "44:D9:E7": "Ubiquiti", "78:8A:20": "Ubiquiti",
    "28:80:23": "SFR", "E8:F7:24": "SFR",
    "14:0C:76": "Freebox", "F4:CA:E5": "Freebox",
    "00:07:CB": "Freebox", "00:24:D4": "Freebox",
    "5C:A6:E6": "Bouygues", "34:8A:AE": "Bouygues",
    "68:A3:78": "Freebox", "24:95:04": "SFR",
    "A4:3E:51": "Orange/Livebox", "E4:5D:51": "Orange/Livebox",
    "84:A1:D1": "Orange/Livebox", "30:23:03": "Belkin",
}

# ---------------------------------------------------------------------------
# Thread-safe state
# ---------------------------------------------------------------------------
lock = threading.Lock()
_shutdown = threading.Event()
_scanning = threading.Event()

# Scan data
networks = {}          # bssid -> {ssid, channel, signal, security, ...}
_seen_bssids = set()   # permanent dedup — never purged to avoid CSV duplicates
probes = {}            # client_mac -> {ssids: set, count, last_seen, signal}
gps_data = None        # {lat, lon, alt, speed, sats, mode, ts}
gps_ready = False
current_channel = 0
_per_iface_channel = {}  # {iface: current_ch} — avoids cross-card channel confusion
scan_start_time = 0
total_beacons = 0
total_probes = 0

# Incremental counters (O(1) updates, replaces O(n) scans)
_inc_sec_count = {}    # security_type -> count
_inc_ch_count = {}     # channel -> count
_inc_wigle_count = 0   # APs with GPS

# Ring buffers for O(1) display (replaces O(n) heapq/sort per frame)
_recent_bssids = deque(maxlen=64)   # (timestamp, bssid) for recent view
_top_signals = []                    # top 8 by signal, maintained incrementally
_insertion_order = deque(maxlen=MAX_NETWORKS + 2000)
_gps_bssids = deque(maxlen=200)     # recent GPS-bearing BSSIDs for map

# Interfaces
mon_ifaces = []        # list of active monitor interfaces
dual_mode = False
card_state = {}        # iface -> {channel, channels_24, channels_5, band, driver, packets}

# UI
view_idx = 0
scroll = 0
live_sort = 0  # 0=signal, 1=recent, 2=name, 3=open_first
sort_mode = 0          # 0=signal, 1=name, 2=security


def _cleanup_signal(*_):
    _shutdown.set()
    _scanning.clear()


signal.signal(signal.SIGINT, _cleanup_signal)
signal.signal(signal.SIGTERM, _cleanup_signal)


# ---------------------------------------------------------------------------
# GPS
# ---------------------------------------------------------------------------


def _detect_gps_device():
    """Auto-detect GPS device path (delegates to _gps_helper)."""
    try:
        from payloads._gps_helper import detect_gps
        dev, baud = detect_gps()
        return dev
    except ImportError:
        for dev in ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0",
                    "/dev/ttyUSB1", "/dev/ttyAMA0", "/dev/ttyS0"]:
            if os.path.exists(dev):
                return dev
        return None


def _start_gpsd():
    """Start gpsd with auto-detected GPS (delegates to _gps_helper)."""
    try:
        from payloads._gps_helper import start_gps
        return start_gps()
    except ImportError:
        pass
    try:
        r = subprocess.run(["pgrep", "-x", "gpsd"], capture_output=True)
        if r.returncode == 0:
            return True
        dev = _detect_gps_device()
        if not dev:
            return False
        subprocess.run(["killall", "-9", "gpsd"], capture_output=True)
        time.sleep(0.5)
        subprocess.Popen(
            ["gpsd", "-n", dev],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
        return True
    except Exception:
        return False


_gps_sats_used = 0
_gps_sats_visible = 0


def _gpsd_sat_poller():
    """Poll gpsd JSON socket for satellite counts (SKY messages)."""
    global _gps_sats_used, _gps_sats_visible
    import socket as _sock
    import json as _j
    while not _shutdown.is_set():
        try:
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            s.settimeout(5)
            s.connect(("127.0.0.1", 2947))
            s.sendall(b'?WATCH={"enable":true,"json":true}\n')
            buf = ""
            while not _shutdown.is_set():
                data = s.recv(4096).decode("utf-8", errors="ignore")
                if not data:
                    break
                buf += data
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if '"class":"SKY"' not in line:
                        continue
                    try:
                        sky = _j.loads(line)
                        n = sky.get("nSat", -1)
                        if n < 0:
                            continue
                        _gps_sats_visible = n
                        u = sky.get("uSat", 0)
                        if u == 0 and "satellites" in sky:
                            u = sum(1 for sat in sky["satellites"] if sat.get("used"))
                        _gps_sats_used = u
                    except Exception:
                        pass
            s.close()
        except Exception:
            pass
        if _shutdown.wait(timeout=3):
            break


def _gps_updater():
    """Background thread: poll gpsd for position updates."""
    global gps_data, gps_ready

    if not GPSD_OK:
        return

    _start_gpsd()

    try:
        gpsd_mod.connect()
    except Exception:
        return

    gps_ready = True
    threading.Thread(target=_gpsd_sat_poller, daemon=True).start()

    _no_fix_count = 0
    while not _shutdown.is_set():
        try:
            pkt = gpsd_mod.get_current()
            if hasattr(pkt, 'mode') and pkt.mode >= 2:
                _no_fix_count = 0
                with lock:
                    gps_data = {
                        "lat": pkt.lat,
                        "lon": pkt.lon,
                        "alt": pkt.alt if pkt.mode >= 3 else 0,
                        "speed": getattr(pkt, 'hspeed', 0),
                        "sats": _gps_sats_used,
                        "sats_visible": _gps_sats_visible,
                        "mode": pkt.mode,
                        "ts": time.time(),
                    }
            else:
                _no_fix_count += 1
                if _no_fix_count > 150:
                    with lock:
                        if gps_data:
                            gps_data["mode"] = 0
        except Exception:
            pass

        if _shutdown.wait(timeout=0.2):
            break


# ---------------------------------------------------------------------------
# Monitor mode
# ---------------------------------------------------------------------------


def _get_driver(iface):
    try:
        return os.path.basename(
            os.path.realpath(f"/sys/class/net/{iface}/device/driver"))
    except Exception:
        return ""


def _is_onboard(iface):
    drv = _get_driver(iface)
    if drv == "brcmfmac":
        return True
    try:
        devpath = os.path.realpath(f"/sys/class/net/{iface}/device")
        return "mmc" in devpath
    except Exception:
        return False


def _find_monitor_interfaces():
    """Find all monitor-capable USB WiFi interfaces."""
    result = []
    ifaces = list_interfaces("wifi")
    for i in ifaces:
        if i.get("is_onboard"):
            continue
        if i.get("supports_monitor") or _get_driver(i["name"]) in KNOWN_MONITOR_DRIVERS:
            result.append(i["name"])
    return result


def _monitor_up(iface):
    """Enable monitor mode on interface."""
    for cmd in [
        ["sudo", "ip", "link", "set", iface, "down"],
        ["sudo", "iw", iface, "set", "monitor", "none"],
        ["sudo", "ip", "link", "set", iface, "up"],
    ]:
        subprocess.run(cmd, capture_output=True, timeout=5)
    time.sleep(0.3)

    r = subprocess.run(["iw", "dev", iface, "info"],
                       capture_output=True, text=True, timeout=5)
    if "type monitor" in r.stdout:
        return iface

    # Fallback: airmon-ng
    subprocess.run(["sudo", "airmon-ng", "start", iface],
                   capture_output=True, timeout=15)
    for name in (f"{iface}mon", iface):
        r = subprocess.run(["iw", "dev", name, "info"],
                           capture_output=True, text=True, timeout=5)
        if "type monitor" in r.stdout:
            return name
    return None


def _monitor_down(iface):
    if not iface:
        return
    base = iface.replace("mon", "")
    subprocess.run(["sudo", "airmon-ng", "stop", iface],
                   capture_output=True, timeout=10)
    for cmd in [
        ["sudo", "ip", "link", "set", base, "down"],
        ["sudo", "iw", base, "set", "type", "managed"],
        ["sudo", "ip", "link", "set", base, "up"],
    ]:
        subprocess.run(cmd, capture_output=True, timeout=5)


def _restart_monitor_mode(iface):
    """Re-enable monitor mode on a card that stopped receiving."""
    if not os.path.isdir(f"/sys/class/net/{iface}"):
        return
    for cmd in [
        ["sudo", "ip", "link", "set", iface, "down"],
        ["sudo", "iw", iface, "set", "monitor", "none"],
        ["sudo", "ip", "link", "set", iface, "up"],
    ]:
        subprocess.run(cmd, capture_output=True, timeout=5)


def _read_rx_dropped(iface):
    try:
        with open(f"/sys/class/net/{iface}/statistics/rx_dropped") as f:
            return int(f.read().strip())
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Raw 802.11 frame parsing (replaces scapy hot path)
# ---------------------------------------------------------------------------

_SUBTYPE_PROBE_REQ = 4
_SUBTYPE_PROBE_RESP = 5
_SUBTYPE_BEACON = 8

_RTAP_FIELD_SIZES = {
    0: 8, 1: 1, 2: 1, 3: 4, 4: 2, 5: 1, 6: 1, 7: 1,
    8: 2, 9: 2, 10: 1, 11: 1, 12: 1, 13: 1, 14: 8, 15: 8,
    16: 8, 17: 3, 18: 4, 19: 2,
}
_RTAP_FIELD_ALIGN = {
    0: 8, 3: 2, 4: 2, 8: 2, 9: 2, 14: 8, 15: 8, 16: 8, 18: 4,
}


def _parse_radiotap(raw):
    if len(raw) < 8:
        return 0, -99
    hdr_len = struct.unpack_from('<H', raw, 2)[0]
    present = struct.unpack_from('<I', raw, 4)[0]

    offset = 8
    while present & (1 << 31):
        if offset + 4 > len(raw):
            return hdr_len, -99
        present = struct.unpack_from('<I', raw, offset)[0]
        offset += 4

    present = struct.unpack_from('<I', raw, 4)[0]
    signal = -99
    for bit in range(32):
        if not (present & (1 << bit)):
            continue
        align = _RTAP_FIELD_ALIGN.get(bit, 1)
        if align > 1:
            offset = (offset + align - 1) & ~(align - 1)
        if bit == 5:
            if offset < len(raw):
                signal = struct.unpack_from('b', raw, offset)[0]
            break
        size = _RTAP_FIELD_SIZES.get(bit, 0)
        if size == 0:
            break
        offset += size
    return hdr_len, signal


def _parse_80211_mgmt(raw, rtap_len):
    if len(raw) < rtap_len + 24:
        return None
    fc = struct.unpack_from('<H', raw, rtap_len)[0]
    ftype = (fc >> 2) & 0x03
    subtype = (fc >> 4) & 0x0F
    if ftype != 0:
        return None
    addr2 = raw[rtap_len + 10: rtap_len + 16]
    addr3 = raw[rtap_len + 16: rtap_len + 22]
    return {
        'subtype': subtype,
        'sa': ':'.join(f'{b:02X}' for b in addr2),
        'bssid': ':'.join(f'{b:02X}' for b in addr3),
        'body_offset': rtap_len + 24,
    }


def _parse_ies(raw, offset):
    result = {'ssid': '', 'channel': 0, 'security': 'Open', 'cipher': '', 'wps': False}
    pos = offset
    end = len(raw)
    while pos + 2 <= end:
        ie_id = raw[pos]
        ie_len = raw[pos + 1]
        pos += 2
        if pos + ie_len > end:
            break
        ie_data = raw[pos: pos + ie_len]
        if ie_id == 0:
            try:
                result['ssid'] = ie_data.decode('utf-8', errors='replace')
            except Exception:
                pass
        elif ie_id == 3 and ie_len >= 1:
            result['channel'] = ie_data[0]
        elif ie_id == 48 and ie_len >= 2:
            result['security'] = 'WPA2-PSK'
            if ie_len >= 8 and b'\x00\x0f\xac\x08' in ie_data:
                result['security'] = 'WPA3-SAE'
            if b'\x00\x0f\xac\x04' in ie_data:
                result['cipher'] = 'CCMP'
            elif b'\x00\x0f\xac\x02' in ie_data:
                result['cipher'] = 'TKIP'
        elif ie_id == 221 and ie_len >= 4:
            if ie_data[:4] == b'\x00\x50\xf2\x01':
                if result['security'] == 'Open':
                    result['security'] = 'WPA'
                    result['cipher'] = 'TKIP'
            elif ie_data[:4] == b'\x00\x50\xf2\x04':
                result['wps'] = True
        pos += ie_len
    return result


# ---------------------------------------------------------------------------
# Security detection
# ---------------------------------------------------------------------------


def _parse_security(pkt):
    """Parse security type from beacon frame."""
    cap = pkt.sprintf("{Dot11Beacon:%Dot11Beacon.cap%}").strip()
    privacy = "privacy" in cap.lower() if cap else False

    security = "Open"
    cipher = ""
    auth = ""
    wps = False

    try:
        elt = pkt.getlayer(Dot11Elt)
        while elt:
            # RSN IE (WPA2/WPA3)
            if elt.ID == 48 and elt.info and len(elt.info) >= 8:
                raw = bytes(elt.info)
                # AKM suite
                if len(raw) >= 14:
                    akm_count = int.from_bytes(raw[8:10], "little")
                    for i in range(akm_count):
                        off = 10 + i * 4
                        if off + 4 <= len(raw):
                            akm_type = raw[off + 3]
                            if akm_type == 8:
                                security = "WPA3-SAE"
                                auth = "SAE"
                            elif akm_type == 2:
                                if security != "WPA3-SAE":
                                    security = "WPA2-PSK"
                                    auth = "PSK"
                            elif akm_type == 1:
                                security = "WPA2-EAP"
                                auth = "Enterprise"
                # Cipher
                if len(raw) >= 8:
                    cs = raw[5]
                    cipher = "CCMP" if cs == 4 else "TKIP" if cs == 2 else "AES"

                if security == "Open":
                    security = "WPA2"

            # WPA IE (vendor specific)
            if elt.ID == 221 and elt.info and len(elt.info) >= 8:
                raw = bytes(elt.info)
                if raw[:3] == b'\x00\x50\xf2' and raw[3] == 1:
                    if security == "Open":
                        security = "WPA"
                        cipher = "TKIP"
                        auth = "PSK"

            # WPS
            if elt.ID == 221 and elt.info:
                raw = bytes(elt.info)
                if raw[:4] == b'\x00\x50\xf2\x04':
                    wps = True

            elt = elt.payload.getlayer(Dot11Elt)
    except Exception:
        pass

    if privacy and security == "Open":
        security = "WEP"

    return {"security": security, "cipher": cipher, "auth": auth, "wps": wps}


def _ts_iso(ts):
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts).isoformat()
    return ts


def _wigle_time(ts):
    """Epoch -> WiGLE CSV FirstSeen ('YYYY-MM-DD HH:MM:SS', UTC per the WiGLE spec)."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _gps_fix(now_ts):
    """Position for a sighting, or None without a recent 2D/3D fix.

    Carries the fix time ("ts"), so a CSV row's FirstSeen is the moment the
    device was at that position. WarDrift rebuilds the route from these pairs
    and rejects trails that jump.
    """
    snap = gps_data
    if not snap or snap.get("mode", 0) < 2 or now_ts - snap.get("ts", 0) > 30:
        return None
    if abs(snap.get("lat", 0)) < 1 and abs(snap.get("lon", 0)) < 1:
        return None
    return {"lat": snap["lat"], "lon": snap["lon"],
            "alt": snap.get("alt", 0), "ts": snap["ts"]}


def _get_vendor(mac):
    """OUI vendor lookup."""
    prefix = mac[:8].upper()
    return OUI_DB.get(prefix, "")


def _update_top_signals(bssid, signal):
    """Maintain top 8 BSSIDs by signal. Minimal alloc."""
    if len(_top_signals) < 8:
        _top_signals.append((signal, bssid))
        return
    min_idx = 0
    min_sig = _top_signals[0][0]
    for i in range(1, len(_top_signals)):
        if _top_signals[i][0] < min_sig:
            min_sig = _top_signals[i][0]
            min_idx = i
        if _top_signals[i][1] == bssid:
            _top_signals[i] = (signal, bssid)
            return
    if signal > min_sig:
        _top_signals[min_idx] = (signal, bssid)


# ---------------------------------------------------------------------------
# Merge functions (scapy-independent)
# ---------------------------------------------------------------------------


def _merge_raw_probe(client_mac, ssid, signal):
    global total_probes
    if not client_mac or client_mac == "FF:FF:FF:FF:FF:FF":
        return
    now_ts = time.time()
    with lock:
        total_probes += 1
        if client_mac not in probes:
            probes[client_mac] = {
                "ssids": set(), "count": 0,
                "last_seen": now_ts, "signal": signal, "_ts": now_ts,
            }
        p = probes[client_mac]
        p["count"] += 1
        p["last_seen"] = now_ts
        p["_ts"] = now_ts
        if ssid:
            p["ssids"].add(ssid)
        p["signal"] = (p["signal"] * 0.7) + (signal * 0.3)


def _merge_raw_network(bssid, ssid, channel, signal, security, cipher, wps):
    global total_beacons, _inc_wigle_count
    if not bssid or bssid == "FF:FF:FF:FF:FF:FF":
        return
    if not ssid:
        ssid = "<hidden>"
    vendor = _get_vendor(bssid)
    now_ts = time.time()

    gps_pos = _gps_fix(now_ts)

    csv_snap = None
    with lock:
        total_beacons += 1
        if bssid in _seen_bssids and bssid not in networks:
            return

        is_new = bssid not in networks
        if is_new:
            _seen_bssids.add(bssid)
            net_entry = {
                "ssid": ssid, "bssid": bssid, "channel": channel,
                "signal": signal, "security": security, "cipher": cipher,
                "auth": "", "wps": wps, "vendor": vendor,
                "first_seen": now_ts, "last_seen": now_ts,
                "gps": gps_pos, "beacon_count": 1,
            }
            networks[bssid] = net_entry
            _dirty_bssids.add(bssid)
            _insertion_order.append(bssid)
            _inc_sec_count[security] = _inc_sec_count.get(security, 0) + 1
            _inc_ch_count[channel] = _inc_ch_count.get(channel, 0) + 1
            if gps_pos:
                _inc_wigle_count += 1
                _gps_bssids.append(bssid)
            _recent_bssids.append((now_ts, bssid))
            _update_top_signals(bssid, signal)
            csv_snap = net_entry
        else:
            net = networks[bssid]
            net["last_seen"] = now_ts
            net["beacon_count"] += 1
            net["signal"] = (net["signal"] * 3 + signal) >> 2
            if gps_pos and not net["gps"]:
                net["gps"] = gps_pos
                _inc_wigle_count += 1
                _gps_bssids.append(bssid)
                _dirty_bssids.add(bssid)
                csv_snap = net
            if ssid != "<hidden>" and net["ssid"] == "<hidden>":
                net["ssid"] = ssid

    if csv_snap:
        _append_live_csv(bssid, csv_snap)


# ---------------------------------------------------------------------------
# Channel hoppers
# ---------------------------------------------------------------------------


def _packet_handler(pkt):
    global total_beacons, total_probes, _inc_wigle_count

    if not pkt.haslayer(Dot11):
        return

    # --- Probe Requests: track client devices ---
    if pkt.haslayer(Dot11ProbeReq):
        try:
            client_mac = (pkt[Dot11].addr2 or "").upper()
            if not client_mac or client_mac == "FF:FF:FF:FF:FF:FF":
                return
            try:
                probe_ssid = pkt[Dot11Elt].info.decode("utf-8", errors="replace")
            except Exception:
                probe_ssid = ""

            sig = getattr(pkt, "dBm_AntSignal", -99)
            now = datetime.now().isoformat()

            now_ts_p = time.time()
            with lock:
                total_probes += 1
                if client_mac not in probes:
                    probes[client_mac] = {
                        "ssids": set(),
                        "count": 0,
                        "last_seen": now,
                        "signal": sig,
                        "_ts": now_ts_p,
                    }
                p = probes[client_mac]
                p["count"] += 1
                p["last_seen"] = now
                p["_ts"] = now_ts_p
                if probe_ssid:
                    p["ssids"].add(probe_ssid)
                # Signal averaging (rolling)
                p["signal"] = (p["signal"] * 0.7) + (sig * 0.3)
        except Exception:
            pass
        return

    # --- Beacons / Probe Responses: discover APs ---
    if not pkt.haslayer(Dot11Beacon) and not pkt.haslayer(Dot11ProbeResp):
        return

    try:
        bssid = (pkt[Dot11].addr2 or "").upper()
        if not bssid or bssid == "FF:FF:FF:FF:FF:FF":
            return

        # SSID
        try:
            ssid = pkt[Dot11Elt].info.decode("utf-8", errors="replace")
        except Exception:
            ssid = ""
        if not ssid:
            ssid = "<hidden>"

        # Signal
        sig = getattr(pkt, "dBm_AntSignal", -99)

        # Channel from DS Parameter Set IE
        channel = 0
        try:
            elt = pkt.getlayer(Dot11Elt)
            while elt:
                if elt.ID == 3 and elt.info:
                    channel = elt.info[0]
                    break
                elt = elt.payload.getlayer(Dot11Elt)
        except Exception:
            pass
        if channel == 0:
            channel = current_channel

        # Security
        sec = _parse_security(pkt)

        # Vendor
        vendor = _get_vendor(bssid)

        now = datetime.now().isoformat()
        now_ts = time.time()

        gps_pos = _gps_fix(now_ts)

        csv_snap = None
        with lock:
            total_beacons += 1

            if bssid in _seen_bssids and bssid not in networks:
                return

            is_new = bssid not in networks
            if is_new:
                _seen_bssids.add(bssid)
                net_entry = {
                    "ssid": ssid,
                    "bssid": bssid,
                    "channel": channel,
                    "signal": sig,
                    "security": sec["security"],
                    "cipher": sec["cipher"],
                    "auth": sec["auth"],
                    "wps": sec["wps"],
                    "vendor": vendor,
                    "first_seen": now,
                    "last_seen": now,
                    "gps": gps_pos,
                    "beacon_count": 1,
                }
                networks[bssid] = net_entry
                _dirty_bssids.add(bssid)
                _insertion_order.append(bssid)
                _inc_sec_count[sec["security"]] = _inc_sec_count.get(sec["security"], 0) + 1
                _inc_ch_count[channel] = _inc_ch_count.get(channel, 0) + 1
                if gps_pos:
                    _inc_wigle_count += 1
                    _gps_bssids.append(bssid)
                _recent_bssids.append((now_ts, bssid))
                _update_top_signals(bssid, sig)
                csv_snap = net_entry
            else:
                net = networks[bssid]
                net["last_seen"] = now
                net["beacon_count"] += 1
                net["signal"] = (net["signal"] * 3 + sig) >> 2
                if gps_pos and not net["gps"]:
                    net["gps"] = gps_pos
                    _inc_wigle_count += 1
                    _gps_bssids.append(bssid)
                    _dirty_bssids.add(bssid)
                    csv_snap = net
                if ssid != "<hidden>" and net["ssid"] == "<hidden>":
                    net["ssid"] = ssid

        if csv_snap:
            _append_live_csv(bssid, csv_snap)

    except Exception:
        pass


# ---------------------------------------------------------------------------
# Channel hoppers
# ---------------------------------------------------------------------------


def _channel_hopper_split(iface, channels):
    """Hop a specific set of channels on iface (for N-card split)."""
    global current_channel
    with lock:
        if iface not in card_state:
            card_state[iface] = {"channel": 0, "channels": channels, "packets": 0}
        else:
            card_state[iface]["channels"] = channels
    while not _shutdown.is_set() and _scanning.is_set():
        for ch in channels:
            if _shutdown.is_set() or not _scanning.is_set():
                return
            r = subprocess.run(
                ["sudo", "iw", "dev", iface, "set", "channel", str(ch)],
                capture_output=True, timeout=3,
            )
            if r.returncode != 0:
                continue
            _per_iface_channel[iface] = ch
            with lock:
                current_channel = ch
                if iface in card_state:
                    card_state[iface]["channel"] = ch
            dwell = DWELL_5 if ch > 14 else DWELL_24
            if _shutdown.wait(timeout=dwell):
                return


def _raw_monitor_worker(iface):
    """Raw AF_PACKET capture on monitor interface — no scapy."""
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
        sock.bind((iface, 0))
        sock.settimeout(2.0)
    except OSError:
        with lock:
            if iface in card_state:
                card_state[iface]["status"] = "sock_fail"
        return

    pkt_count = 0
    backoff = 1
    try:
        while not _shutdown.is_set() and _scanning.is_set():
            if not os.path.isdir(f"/sys/class/net/{iface}"):
                with lock:
                    if iface in card_state:
                        card_state[iface]["status"] = "disconnected"
                break
            try:
                raw = sock.recv(65535)
            except socket.timeout:
                continue
            except OSError:
                if _shutdown.wait(timeout=backoff):
                    break
                backoff = min(backoff * 2, 30)
                continue

            backoff = 1
            pkt_count += 1
            if pkt_count % 10 == 0:
                with lock:
                    if iface in card_state:
                        card_state[iface]["packets"] = pkt_count

            rtap_len, signal = _parse_radiotap(raw)
            frame = _parse_80211_mgmt(raw, rtap_len)
            if not frame:
                continue

            if frame['subtype'] in (_SUBTYPE_BEACON, _SUBTYPE_PROBE_RESP):
                body_start = frame['body_offset'] + 12
                ies = _parse_ies(raw, body_start)
                ch = ies['channel'] or _per_iface_channel.get(iface, current_channel)
                _merge_raw_network(
                    frame['bssid'], ies['ssid'], ch, signal,
                    ies['security'], ies['cipher'], ies['wps'])
            elif frame['subtype'] == _SUBTYPE_PROBE_REQ:
                body_start = frame['body_offset']
                ies = _parse_ies(raw, body_start)
                _merge_raw_probe(frame['sa'], ies['ssid'], signal)
    finally:
        sock.close()


def _build_probe_request(src_mac):
    """Build a raw 802.11 probe request broadcast frame with radiotap header."""
    mac_bytes = bytes.fromhex(src_mac.replace(":", ""))
    bcast = b'\xff\xff\xff\xff\xff\xff'
    radiotap = struct.pack('<BBHI', 0, 0, 8, 0)
    fc = struct.pack('<H', 0x0040)
    duration = b'\x00\x00'
    seq = b'\x00\x00'
    header = fc + duration + bcast + mac_bytes + bcast + seq
    ssid_ie = struct.pack('BB', 0, 0)
    rates_ie = struct.pack('BB', 1, 8) + b'\x82\x84\x8b\x96\x0c\x12\x18\x24'
    return radiotap + header + ssid_ie + rates_ie


def _monitor_channel_hopper(iface, active_mode=False):
    """Single-card channel hopper. Injects probe requests in active mode."""
    channels = CHANNELS_24 + CHANNELS_5

    inject_sock = None
    probe_frame = None
    if active_mode:
        try:
            r = subprocess.run(["cat", f"/sys/class/net/{iface}/address"],
                               capture_output=True, text=True, timeout=3)
            mac = r.stdout.strip().upper()
        except Exception:
            mac = "00:11:22:33:44:55"
        probe_frame = _build_probe_request(mac)
        try:
            inject_sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                                        socket.htons(0x0003))
            inject_sock.bind((iface, 0))
        except OSError:
            inject_sock = None

    while not _shutdown.is_set() and _scanning.is_set():
        for ch in channels:
            if _shutdown.is_set() or not _scanning.is_set():
                if inject_sock:
                    inject_sock.close()
                return
            r = subprocess.run(
                ["sudo", "iw", "dev", iface, "set", "channel", str(ch)],
                capture_output=True, timeout=3)
            if r.returncode != 0:
                continue
            _per_iface_channel[iface] = ch
            with lock:
                if iface in card_state:
                    card_state[iface]["channel"] = ch
            if inject_sock:
                try:
                    inject_sock.send(probe_frame)
                except Exception:
                    pass
            dwell = 0.2 if ch <= 14 else 0.3
            if _shutdown.wait(timeout=dwell):
                if inject_sock:
                    inject_sock.close()
                return


def _active_scan_worker(iface, freqs, stagger=0, passive=False):
    """Scan via kernel — active (probe requests) or passive (listen only)."""
    subprocess.run(["sudo", "ip", "link", "set", iface, "up"],
                   capture_output=True, timeout=5)
    time.sleep(0.5)
    if stagger > 0:
        if _shutdown.wait(timeout=stagger):
            return
    scan_count = 0
    while not _shutdown.is_set() and _scanning.is_set():
        if not os.path.isdir(f"/sys/class/net/{iface}"):
            with lock:
                if iface in card_state:
                    card_state[iface]["status"] = "disconnected"
            return
        try:
            if passive:
                cmd = ["sudo", "iw", "dev", iface, "scan", "passive"]
            else:
                cmd = ["sudo", "iw", "dev", iface, "scan", "flush"]
                if freqs:
                    cmd += ["freq"] + [str(f) for f in freqs]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if r.returncode == 0 and "BSS " in r.stdout:
                _parse_iw_scan(r.stdout)
                scan_count += 1
                with lock:
                    if iface in card_state:
                        card_state[iface]["packets"] = scan_count
                        card_state[iface]["status"] = "active"
            elif "busy" in r.stderr.lower() or "busy" in r.stdout.lower():
                if _shutdown.wait(timeout=0.5):
                    return
                continue
            else:
                with lock:
                    if iface in card_state:
                        card_state[iface]["status"] = "busy"
                if _shutdown.wait(timeout=2):
                    return
                continue
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass
        if _shutdown.wait(timeout=3):
            return


def _assign_card_roles(ifaces):
    """Assign cards: N-1 active scan (managed) + 1 monitor (raw socket).
    Returns (active_cards, monitor_card_or_None).
    """
    if not ifaces:
        return [], None
    if len(ifaces) == 1:
        return ifaces, None

    monitor_capable = []
    for iface in ifaces:
        drv = _get_driver(iface)
        if drv in KNOWN_MONITOR_DRIVERS:
            monitor_capable.append(iface)

    if not monitor_capable:
        return ifaces, None

    monitor_card = monitor_capable[0]
    active_cards = [i for i in ifaces if i != monitor_card]
    return active_cards, monitor_card


_IW_FREQS_24 = [2412, 2417, 2422, 2427, 2432, 2437, 2442,
                 2447, 2452, 2457, 2462, 2467, 2472]
_IW_FREQS_5 = [5180, 5200, 5220, 5240, 5260, 5280, 5300, 5320,
               5500, 5520, 5540, 5560, 5580, 5600, 5620, 5640,
               5660, 5680, 5700, 5745, 5765, 5785, 5805, 5825]


def _iw_scanner(iface):
    """Active AP discovery via 'iw dev scan' — forces probe requests."""
    while not _shutdown.is_set() and _scanning.is_set():
        try:
            freqs = _IW_FREQS_24 + _IW_FREQS_5
            freq_args = []
            for f in freqs:
                freq_args += ["freq", str(f)]
            r = subprocess.run(
                ["sudo", "iw", "dev", iface, "scan", "flush"] + freq_args,
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode != 0:
                if _shutdown.wait(timeout=5):
                    break
                continue

            _parse_iw_scan(r.stdout)
        except Exception:
            pass

        interval = 3 if not mon_ifaces else 8
        if _shutdown.wait(timeout=interval):
            break


def _parse_iw_scan(output):
    """Parse 'iw dev scan' output and merge into networks dict."""
    global total_beacons

    bssid = None
    ssid = ""
    channel = 0
    signal = -99
    security = "Open"
    cipher = ""
    wps = False

    for line in output.splitlines():
        line = line.strip()

        if line.startswith("BSS "):
            if bssid:
                _merge_iw_network(bssid, ssid, channel, signal, security,
                                  cipher, wps)
            # New AP
            parts = line.split()
            bssid = parts[1].split("(")[0].upper() if len(parts) > 1 else None
            ssid = ""
            channel = 0
            signal = -99
            security = "Open"
            cipher = ""
            wps = False

        elif line.startswith("SSID:"):
            ssid = line[5:].strip()
        elif line.startswith("signal:"):
            try:
                signal = int(float(line.split(":")[1].strip().split()[0]))
            except Exception:
                pass
        elif line.startswith("DS Parameter set: channel"):
            try:
                channel = int(line.split("channel")[1].strip())
            except Exception:
                pass
        elif line.startswith("* primary channel:"):
            try:
                channel = int(line.split(":")[1].strip())
            except Exception:
                pass
        elif "WPA:" in line:
            if security == "Open":
                security = "WPA"
        elif "RSN:" in line:
            security = "WPA2-PSK"
        elif "SAE" in line:
            security = "WPA3-SAE"
        elif "CCMP" in line:
            cipher = "CCMP"
        elif "TKIP" in line and not cipher:
            cipher = "TKIP"
        elif "WPS" in line:
            wps = True
        elif "Privacy" in line or "privacy" in line:
            if security == "Open":
                security = "WEP"

    # Save last AP
    if bssid:
        _merge_iw_network(bssid, ssid, channel, signal, security,
                          cipher, wps)


def _merge_iw_network(bssid, ssid, channel, signal, security,
                       cipher, wps):
    """Merge iw scan result into networks dict."""
    global total_beacons, _inc_wigle_count
    if not bssid or bssid == "FF:FF:FF:FF:FF:FF":
        return
    if not ssid:
        ssid = "<hidden>"

    vendor = _get_vendor(bssid)
    csv_snap = None
    now_ts = time.time()

    gps_pos = _gps_fix(now_ts)

    with lock:
        total_beacons += 1

        if bssid in _seen_bssids and bssid not in networks:
            return

        is_new = bssid not in networks
        if is_new:
            _seen_bssids.add(bssid)
            net_entry = {
                "ssid": ssid, "bssid": bssid, "channel": channel,
                "signal": signal, "security": security, "cipher": cipher,
                "auth": "", "wps": wps, "vendor": vendor,
                "first_seen": now_ts, "last_seen": now_ts,
                "gps": gps_pos, "beacon_count": 1,
            }
            networks[bssid] = net_entry
            _insertion_order.append(bssid)
            _inc_sec_count[security] = _inc_sec_count.get(security, 0) + 1
            _inc_ch_count[channel] = _inc_ch_count.get(channel, 0) + 1
            if gps_pos:
                _inc_wigle_count += 1
                _gps_bssids.append(bssid)
            _recent_bssids.append((now_ts, bssid))
            _update_top_signals(bssid, signal)
            csv_snap = net_entry
        else:
            net = networks[bssid]
            net["last_seen"] = now_ts
            net["beacon_count"] += 1
            net["signal"] = (net["signal"] * 3 + signal) >> 2
            if gps_pos and not net["gps"]:
                net["gps"] = gps_pos
                _inc_wigle_count += 1
                _gps_bssids.append(bssid)
                _dirty_bssids.add(bssid)
                csv_snap = net
            if ssid != "<hidden>" and net["ssid"] == "<hidden>":
                net["ssid"] = ssid

    if csv_snap:
        _append_live_csv(bssid, csv_snap)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


_db_conn = None


def _init_db():
    global _db_conn
    os.makedirs(LOOT_DIR, exist_ok=True)
    _db_conn = sqlite3.connect(DB_PATH, timeout=10)
    c = _db_conn.cursor()
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA wal_autocheckpoint=500")
    c.execute("""CREATE TABLE IF NOT EXISTS networks (
        bssid TEXT PRIMARY KEY, ssid TEXT, channel INTEGER,
        signal INTEGER, security TEXT, cipher TEXT, auth TEXT,
        wps BOOLEAN, vendor TEXT, first_seen TEXT, last_seen TEXT,
        lat REAL, lon REAL, alt REAL, beacon_count INTEGER)""")
    _db_conn.commit()


def _close_db():
    global _db_conn
    if _db_conn:
        try:
            _db_conn.close()
        except Exception:
            pass
        _db_conn = None


def _load_seen_from_db():
    """Load existing BSSIDs from DB to prevent duplicates after restart."""
    global _inc_wigle_count
    if not _db_conn:
        return
    try:
        rows = _db_conn.execute("SELECT bssid FROM networks").fetchall()
        for r in rows:
            _seen_bssids.add(r[0])
        row = _db_conn.execute("SELECT COUNT(*) FROM networks WHERE lat IS NOT NULL").fetchone()
        if row:
            _inc_wigle_count = row[0]
        for sec, cnt in _db_conn.execute("SELECT security, COUNT(*) FROM networks GROUP BY security"):
            _inc_sec_count[sec] = cnt
        for ch, cnt in _db_conn.execute("SELECT channel, COUNT(*) FROM networks GROUP BY channel"):
            _inc_ch_count[ch] = cnt
    except Exception:
        pass


_db_saved_count = 0
_dirty_bssids = set()


def _save_to_db():
    """Save dirty networks to SQLite. Lock held only for snapshot copy."""
    global _db_saved_count
    try:
        with lock:
            if not _dirty_bssids:
                return
            batch_keys = list(_dirty_bssids)[:500]
            snap = {}
            for b in batch_keys:
                n = networks.get(b)
                if n:
                    snap[b] = (
                        b, n["ssid"], n["channel"], n["signal"],
                        n["security"], n["cipher"], n["auth"], n["wps"],
                        n["vendor"], n["first_seen"], n["last_seen"],
                        n.get("gps"), n["beacon_count"],
                    )
                _dirty_bssids.discard(b)
            _db_saved_count = len(networks)

        batch = []
        for b, tup in snap.items():
            bssid, ssid, ch, sig, sec, cipher, auth, wps, vendor, fs, ls, gps, bc = tup
            batch.append((
                bssid, ssid, ch, sig, sec, cipher, auth, wps, vendor,
                _ts_iso(fs), _ts_iso(ls),
                gps["lat"] if gps else None,
                gps["lon"] if gps else None,
                gps.get("alt") if gps else None,
                bc,
            ))
        if not _db_conn:
            return
        c = _db_conn.cursor()
        c.executemany("""INSERT OR REPLACE INTO networks
            (bssid, ssid, channel, signal, security, cipher, auth,
             wps, vendor, first_seen, last_seen, lat, lon, alt, beacon_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", batch)
        _db_conn.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _security_to_wigle(sec, cipher, auth):
    """Convert security to Wigle AuthMode format."""
    if sec == "Open":
        return "[ESS]"
    if sec == "WEP":
        return "[WEP][ESS]"
    if sec == "WPA3-SAE":
        return f"[WPA3-SAE-{cipher or 'CCMP'}][ESS]"
    if sec == "WPA2-EAP":
        return f"[WPA2-EAP-{cipher or 'CCMP'}][ESS]"
    if "WPA2" in sec:
        return f"[WPA2-PSK-{cipher or 'CCMP'}][ESS]"
    if "WPA" in sec:
        return f"[WPA-PSK-{cipher or 'TKIP'}][ESS]"
    return f"[{sec}][ESS]"


def _ch_to_freq(ch):
    if 1 <= ch <= 13:
        return 2407 + ch * 5
    if ch == 14:
        return 2484
    if 36 <= ch <= 165:
        return 5000 + ch * 5
    return 0


def _export_all():
    """Export to Wigle CSV, JSON, KML. Returns list of created files."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(LOOT_DIR, exist_ok=True)
    files = []

    with lock:
        nets = dict(networks)

    if not nets:
        return files

    # Save DB first
    _save_to_db()

    # --- Wigle CSV ---
    wigle_path = os.path.join(LOOT_DIR, f"wigle_{ts}.csv")
    try:
        with open(wigle_path, "w", newline="") as f:
            f.write("WigleWifi-1.4,appRelease=RaspyJack-v2,model=RaspberryPi,"
                    "release=2.0,device=RaspyJack,display=LCD144,"
                    "board=RaspberryPi,brand=7h30th3r0n3\n")
            writer = csv.writer(f)
            writer.writerow([
                "MAC", "SSID", "AuthMode", "FirstSeen", "Channel", "RSSI",
                "CurrentLatitude", "CurrentLongitude", "AltitudeMeters",
                "AccuracyMeters", "Type",
            ])
            for bssid, n in nets.items():
                gps = n.get("gps")
                if not gps:
                    continue
                auth_mode = _security_to_wigle(
                    n["security"], n["cipher"], n["auth"])
                writer.writerow([
                    bssid, n["ssid"], auth_mode, _wigle_time(gps["ts"]),
                    n["channel"], n["signal"],
                    f"{gps['lat']:.6f}", f"{gps['lon']:.6f}",
                    f"{gps.get('alt', 0):.1f}", "10", "WIFI",
                ])
        files.append(wigle_path)
    except Exception:
        pass

    # --- JSON ---
    json_path = os.path.join(LOOT_DIR, f"scan_{ts}.json")
    try:
        export = {
            "scan_info": {
                "timestamp": ts,
                "total_networks": len(nets),
                "wigle_ready": sum(1 for n in nets.values() if n.get("gps")),
            },
            "networks": list(nets.values()),
        }
        with open(json_path, "w") as f:
            json.dump(export, f, indent=2)
        files.append(json_path)
    except Exception:
        pass

    # --- KML ---
    kml_path = os.path.join(LOOT_DIR, f"scan_{ts}.kml")
    try:
        kml = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<kml xmlns="http://www.opengis.net/kml/2.2">',
               '<Document><name>RaspyJack Wardriving</name>']
        for bssid, n in nets.items():
            gps = n.get("gps")
            if not gps:
                continue
            kml.append(f'<Placemark><name>{n["ssid"]}</name>')
            kml.append(f'<description>BSSID:{bssid} Sec:{n["security"]} '
                       f'Ch:{n["channel"]} Sig:{n["signal"]}dBm</description>')
            kml.append(f'<Point><coordinates>{gps["lon"]:.6f},'
                       f'{gps["lat"]:.6f},{gps.get("alt", 0):.0f}'
                       f'</coordinates></Point></Placemark>')
        kml.append('</Document></kml>')
        with open(kml_path, "w") as f:
            f.write("\n".join(kml))
        files.append(kml_path)
    except Exception:
        pass

    return files


# ---------------------------------------------------------------------------
# LCD Drawing
# ---------------------------------------------------------------------------


def _signal_bar(sig):
    """Convert dBm to 0-4 bar level."""
    if sig >= -50:
        return 4
    if sig >= -60:
        return 3
    if sig >= -70:
        return 2
    if sig >= -80:
        return 1
    return 0


def _draw_signal_bars(d, x, y, level):
    """Draw mini signal bars."""
    pct = max(0, min(100, int(level) * 25))
    col = _T["ok"] if level >= 3 else (_T["warn"] if level == 2 else _T["danger"])
    rj_theme.draw_signal_bars(d, x, y, 10, pct, active_color=col, inactive_color=_T["border_subtle"])


def _sec_color(sec):
    if sec == "Open":
        return _T["danger"]
    if sec == "WEP":
        return _T["accent"]
    if "WPA3" in sec:
        return _T["ok"]
    if "WPA2" in sec:
        return _T["accent_bright"]
    if "WPA" in sec:
        return _T["warn"]
    return _T["text_muted"]


def _wd_dims(lcd):
    w = int(getattr(lcd, "width", WIDTH) or WIDTH)
    h = int(getattr(lcd, "height", HEIGHT) or HEIGHT)
    return w, h, rj_theme.load_app_fonts(w, h)


def _draw_live(lcd, font, font_sm):
    w, h, fonts = _wd_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)

    LIVE_SORTS = ["Signal", "Recent", "Name", "Open"]
    max_fetch = 14 if large else (8 if wide else 6)

    with lock:
        net_count = len(_seen_bssids)
        cli_count = len(probes)
        beacons = total_beacons
        ch = current_channel
        gps_snap = dict(gps_data) if gps_data else None

        if live_sort == 0:
            recent = [networks[b] for _, b in sorted(_top_signals, reverse=True)[:max_fetch]
                      if b in networks]
        else:
            seen = set()
            recent = []
            for _, b in reversed(_recent_bssids):
                if b not in seen and b in networks:
                    seen.add(b)
                    recent.append(networks[b])
                    if len(recent) >= max_fetch:
                        break
            if live_sort == 2:
                recent.sort(key=lambda n: n["ssid"].lower())
            elif live_sort == 3:
                recent.sort(key=lambda n: (0 if n["security"] == "Open" else 1, -n["signal"]))

    scanning = _scanning.is_set()
    dm = dual_mode
    mode_tag = "2x" if dm else "1x"

    hdr_h = rj_theme.draw_app_header(
        d, w, "WARDRIVING", fonts,
        badge=f"{'SCAN' if scanning else 'IDLE'} {mode_tag}",
        right_text=f"Bcn:{beacons}" if wide else "",
        right_pill=f"SORT: {LIVE_SORTS[live_sort]}" if wide else "",
    )
    ftr_h = rj_theme.draw_app_footer(
        d, w, h,
        "OK:Scan  K1:Views  L/R:Sort  K2:Export  K3:Exit" if wide else "OK:Scn K1:Vw <>:Srt",
        fonts,
        right_text=f"CH {ch}" if large else "",
    )

    pad = 8 if large else (4 if wide else 2)
    y = hdr_h + pad
    y_bot = h - ftr_h - pad

    if w >= 480:
        # 4 KPI telemetry tiles
        kpi_h = 56
        gap = 8
        tw = (w - pad * 2 - gap * 3) // 4
        gps_val = f"{gps_snap['lat']:.4f},{gps_snap['lon']:.4f}" if gps_snap else "No GPS Fix"
        kpis = [
            ("APs FOUND", f"{net_count:,}", _T["ok"]),
            ("CLIENTS", f"{cli_count:,}", _T["accent_bright"]),
            ("CHANNEL", f"CH {ch}" if ch else "--", _T["warn"]),
            ("GPS FIX", gps_val, _T["ok"] if gps_snap else _T["danger"]),
        ]
        for idx_k, (lbl, val, acc) in enumerate(kpis):
            kx = pad + idx_k * (tw + gap)
            rj_theme.draw_card(
                d, (kx, y, kx + tw, y + kpi_h),
                fill=_T["bg_surface"], outline=_T["border_subtle"],
                radius=6, accent_left=acc, accent_width=4,
            )
            d.text((kx + 10, y + 7), lbl, font=fonts["badge"], fill=_T["text_muted"])
            d.text((kx + 10, y + 26), rj_theme.truncate_text(d, val, tw - 16, fonts["body_bold"] if idx_k == 3 else fonts["title"]), font=fonts["body_bold"] if idx_k == 3 else fonts["title"], fill=acc)
        y += kpi_h + 8

        # Column header strip
        d.rectangle((pad, y, w - pad, y + 20), fill=_T["bg_elevated"])
        c_ssid = pad + 8
        c_bssid = int(w * 0.40)
        c_ch = int(w * 0.64)
        c_sec = int(w * 0.72)
        c_sig = int(w * 0.87)
        for cx, title in [(c_ssid, "SSID"), (c_bssid, "BSSID / VENDOR"), (c_ch, "CH"), (c_sec, "SECURITY"), (c_sig, "SIGNAL")]:
            d.text((cx, y + 4), title, font=fonts["badge"], fill=_T["text_muted"])
        y += 22

        if not recent:
            d.text((w // 2, (y + y_bot) // 2), "Waiting for beacons... Press OK to scan", font=fonts["body"], fill=_T["text_dim"], anchor="mm")
        else:
            row_h = 24
            for idx_r, n in enumerate(recent):
                if y + row_h > y_bot:
                    break
                if idx_r % 2 == 0:
                    d.rectangle((pad, y, w - pad, y + row_h - 1), fill=_T["bg_surface"])
                sig = n["signal"]
                sec = n["security"]
                vendor = n.get("vendor", "")
                bssid_txt = f"{n['bssid']}" + (f" ({vendor})" if vendor else "")
                d.text((c_ssid, y + 4), rj_theme.truncate_text(d, n["ssid"], c_bssid - c_ssid - 8, fonts["body_bold"]), font=fonts["body_bold"], fill=_T["text_bright"])
                d.text((c_bssid, y + 5), rj_theme.truncate_text(d, bssid_txt, c_ch - c_bssid - 8, fonts["sub"]), font=fonts["sub"], fill=_T["text_muted"])
                d.text((c_ch, y + 5), str(n.get("channel", "-")), font=fonts["sub"], fill=_T["accent_bright"])
                rj_theme.draw_pill(d, c_sec, y + 3, sec[:8], fonts["badge"], bg=_T["bg_elevated"], fg=_sec_color(sec), outline=None, pad_x=4, pad_y=1)
                rj_theme.draw_signal_bars(d, c_sig, y + 5, 12, _signal_bar(sig) * 25, active_color=_T["ok"] if sig >= -60 else (_T["warn"] if sig >= -75 else _T["danger"]))
                d.text((c_sig + 24, y + 5), f"{sig}", font=fonts["sub"], fill=_T["text_muted"])
                y += row_h
    else:
        # Cardputer / 240x240 / 128x128
        d.text((pad + 2, y), f"AP:{net_count} CLI:{cli_count} CH:{ch}", font=fonts["body_bold"] if wide else fonts["sub"], fill=_T["ok"])
        y += 14 if wide else 11
        gps_txt = f"GPS:{gps_snap['lat']:.4f},{gps_snap['lon']:.4f}" if gps_snap else "GPS: No fix"
        d.text((pad + 2, y), rj_theme.truncate_text(d, gps_txt, w - pad * 2 - 44, fonts["sub"]), font=fonts["sub"], fill=_T["ok"] if gps_snap else _T["danger"])
        if not wide:
            sw = rj_theme.measure_text(fonts["sub"], LIVE_SORTS[live_sort], d)[2]
            d.text((w - pad - sw - 2, y), LIVE_SORTS[live_sort], font=fonts["sub"], fill=_T["text_dim"])
        y += 13 if wide else 11
        d.line([(pad, y), (w - pad, y)], fill=_T["border_subtle"])
        y += 3

        if not recent:
            d.text((w // 2, (y + y_bot) // 2), "No networks yet", font=fonts["body"] if wide else fonts["sub"], fill=_T["text_dim"], anchor="mm")
        else:
            row_h = 18 if wide else (18 if h >= 200 else 13)
            for idx_r, n in enumerate(recent):
                if y + row_h > y_bot:
                    break
                if idx_r % 2 == 0 and wide:
                    d.rectangle((pad, y, w - pad, y + row_h - 1), fill=_T["bg_surface"])
                sig = n["signal"]
                sec = n["security"]
                rx = w - pad - 3
                db_s = f"{sig}"
                dw = rj_theme.measure_text(fonts["sub"], db_s, d)[2]
                d.text((rx - dw, y + 2), db_s, font=fonts["sub"], fill=_T["text_muted"])
                rx -= dw + 4
                rj_theme.draw_signal_bars(d, rx - 14, y + 2, 10 if wide else 8, _signal_bar(sig) * 25, active_color=_T["ok"] if sig >= -60 else (_T["warn"] if sig >= -75 else _T["danger"]))
                rx -= 18
                if wide:
                    pw, _ = rj_theme.draw_pill(d, rx - 46, y + 1, sec[:7], fonts["badge"], bg=_T["bg_elevated"], fg=_sec_color(sec), outline=None, pad_x=3, pad_y=1)
                    rx -= pw + 5
                else:
                    d.ellipse((rx - 6, y + 3, rx - 2, y + 7), fill=_sec_color(sec))
                    rx -= 9
                d.text((pad + 3, y + 2), rj_theme.truncate_text(d, n["ssid"], max(16, rx - pad - 5), fonts["body"] if wide else fonts["sub"]), font=fonts["body"] if wide else fonts["sub"], fill=_T["text_primary"])
                y += row_h

    lcd.LCD_ShowImage(img, 0, 0)


def _draw_gps(lcd, font, font_sm):
    w, h, fonts = _wd_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)

    with lock:
        gps_snap = dict(gps_data) if gps_data else None

    hdr_h = rj_theme.draw_app_header(
        d, w, "GPS TELEMETRY", fonts,
        badge=f"{gps_snap.get('mode', 0)}D FIX" if gps_snap else "NO FIX",
        right_pill=f"SATS: {gps_snap.get('sats', 0)}" if (gps_snap and wide) else "",
    )
    ftr_h = rj_theme.draw_app_footer(d, w, h, "K1:Next View   K2:Export   K3:Exit" if wide else "K1:View K3:Exit", fonts)

    pad = 10 if large else (5 if wide else 3)
    y0 = hdr_h + pad
    y1 = h - ftr_h - pad

    if not gps_snap:
        if not GPSD_OK:
            t_msg, s_msg, kind = "gpsd-py3 Missing", "pip3 install gpsd-py3", "err"
        elif not gps_ready:
            t_msg, s_msg, kind = "GPS Not Detected", "Check USB GPS receiver connection", "err"
        else:
            t_msg, s_msg, kind = "Acquiring Satellites...", "Move outdoors with clear sky view", "warn"
        rj_theme.draw_status_card(d, w, h, t_msg, sub=s_msg, kind=kind, fonts=fonts)
    else:
        lat = gps_snap["lat"]
        lon = gps_snap["lon"]
        alt = gps_snap.get("alt", 0)
        speed = gps_snap.get("speed", 0)
        sats = gps_snap.get("sats", 0)
        mode = gps_snap.get("mode", 0)
        age = time.time() - gps_snap.get("ts", time.time())
        fix_type = f"{mode}D Fix" if mode >= 2 else "No fix"
        speed_kmh = speed * 3.6 if speed else 0
        if speed_kmh < 2:
            mvt, mvt_col = "Stationary", _T["text_muted"]
        elif speed_kmh < 8:
            mvt, mvt_col = "Walking", _T["ok"]
        elif speed_kmh < 30:
            mvt, mvt_col = "Cycling", _T["warn"]
        else:
            mvt, mvt_col = "Driving", _T["danger"]
        age_txt = f"{age:.0f}s ago" if age < 60 else "stale"

        if w >= 480:
            gap = 10
            cw = (w - pad * 2 - gap) // 2
            ch = (y1 - y0 - gap) // 2
            tiles = [
                ("COORDINATES", f"{lat:.6f}", f"LON  {lon:.6f}", _T["ok"]),
                ("ALTITUDE & FIX", f"{alt:.1f} m", f"{fix_type}  |  Updated {age_txt}", _T["accent_bright"]),
                ("SPEED & MOTION", f"{speed_kmh:.1f} km/h", mvt, mvt_col),
                ("SATELLITES", f"{sats} locked", "Wigle geotagging active" if mode >= 2 else "Waiting for lock", _T["warn"]),
            ]
            for idx_t, (lbl, big_v, sub_v, acc) in enumerate(tiles):
                tx = pad + (idx_t % 2) * (cw + gap)
                ty = y0 + (idx_t // 2) * (ch + gap)
                rj_theme.draw_card(d, (tx, ty, tx + cw, ty + ch), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=8, accent_left=acc, accent_width=4)
                d.text((tx + 14, ty + 12), lbl, font=fonts["badge"], fill=_T["text_muted"])
                d.text((tx + 14, ty + 38), big_v, font=fonts["title"], fill=acc)
                d.text((tx + 14, ty + 68), sub_v, font=fonts["body"], fill=_T["text_primary"])
        else:
            rj_theme.draw_card(d, (pad, y0, w - pad, y1), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=5 if wide else 3, accent_left=_T["ok"], accent_width=3 if wide else 2)
            tx = pad + (10 if wide else 6)
            y = y0 + (6 if wide else 4)
            step = 18 if wide else (18 if h >= 200 else 13)
            for txt, col in [
                (f"Fix: {fix_type}   Sats: {sats} ({age_txt})", _T["ok"]),
                (f"Lat: {lat:.6f}", _T["text_bright"]),
                (f"Lon: {lon:.6f}", _T["text_bright"]),
                (f"Alt: {alt:.1f}m", _T["text_muted"]),
                (f"Speed: {speed_kmh:.1f} km/h  [{mvt}]", mvt_col),
            ]:
                if y + 10 > y1:
                    break
                d.text((tx, y), rj_theme.truncate_text(d, txt, w - pad - tx - 4, fonts["body"] if wide else fonts["sub"]), font=fonts["body"] if wide else fonts["sub"], fill=col)
                y += step

    lcd.LCD_ShowImage(img, 0, 0)


_stats_cache = {}
_stats_cache_ts = 0


def _refresh_stats_cache():
    global _stats_cache, _stats_cache_ts
    now = time.time()
    if now - _stats_cache_ts < 2.0 and _stats_cache:
        return _stats_cache
    with lock:
        total = len(_seen_bssids)
        cli_count = len(probes)
        probe_count = total_probes
        gps_snap = dict(gps_data) if gps_data else None
        wigle_ready = _inc_wigle_count
        sec_count = dict(_inc_sec_count)
        ch_count = dict(_inc_ch_count)
    _stats_cache = {
        "total": total, "cli": cli_count, "probes": probe_count,
        "gps": gps_snap, "wigle": wigle_ready,
        "sec": sec_count, "ch": ch_count,
    }
    _stats_cache_ts = now
    return _stats_cache


def _draw_stats(lcd, font, font_sm):
    w, h, fonts = _wd_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)

    sc = _refresh_stats_cache()
    total = sc["total"]
    cli_count = sc["cli"]
    wigle_ready = sc["wigle"]
    sec_count = sc["sec"]
    ch_count = sc["ch"]

    hdr_h = rj_theme.draw_app_header(
        d, w, "STATISTICS", fonts,
        badge=f"{total} APs",
        right_text=f"CLI:{cli_count}" if wide else "",
        right_pill=f"WIGLE:{wigle_ready}" if wide else "",
    )
    ftr_h = rj_theme.draw_app_footer(d, w, h, "K1:Views   K2:Export   K3:Exit" if wide else "K1:Vw K2:Exp K3:X", fonts)

    pad = 8 if large else (4 if wide else 2)
    y = hdr_h + pad
    y_bot = h - ftr_h - pad

    if not wide:
        d.text((pad + 2, y), f"AP:{total} CLI:{cli_count} GPS:{wigle_ready}", font=fonts["sub"], fill=_T["text_bright"])
        y += 13

    sec_items = []
    for sec_name in ["Open", "WEP", "WPA", "WPA2-PSK", "WPA2-EAP", "WPA3-SAE"]:
        cnt = sec_count.get(sec_name, 0)
        if cnt == 0 and sec_name not in ("Open", "WPA2-PSK"):
            continue
        sec_items.append((sec_name, cnt))

    if w >= 480:
        # Left card: Security Distribution bars | Right card: 2.4GHz Channel Heatmap
        mid = int(w * 0.54)
        rj_theme.draw_card(d, (pad, y, mid - 4, y_bot), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=8, accent_left=_T["accent"], accent_width=4)
        lx = pad + 14
        ly = y + 12
        d.text((lx, ly), "SECURITY BREAKDOWN", font=fonts["badge"], fill=_T["accent_bright"])
        ly += 24
        for sec_name, cnt in sec_items:
            pct = cnt / max(total, 1)
            col = _sec_color(sec_name)
            d.text((lx, ly), sec_name, font=fonts["body_bold"], fill=col)
            bx0 = lx + 92
            bx1 = mid - 68
            rj_theme.draw_progress_bar(d, (bx0, ly + 2, bx1, ly + 13), pct, fill=col, track=_T["bg_well"], outline=_T["border_subtle"], radius=3)
            d.text((bx1 + 8, ly), f"{cnt} ({int(pct * 100)}%)", font=fonts["sub"], fill=_T["text_muted"])
            ly += 28

        rx0 = mid + 6
        rj_theme.draw_card(d, (rx0, y, w - pad, y_bot), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=8, accent_left=_T["ok"], accent_width=4)
        rx = rx0 + 14
        d.text((rx, y + 12), "2.4 GHz CHANNEL LOAD", font=fonts["badge"], fill=_T["ok"])
        max_ch = max((ch_count.get(c, 0) for c in CHANNELS_24), default=1) or 1
        bar_area_x = rx
        bar_area_w = (w - pad - 12) - bar_area_x
        cw = max(10, bar_area_w // len(CHANNELS_24))
        b_top = y + 44
        b_bot = y_bot - 30
        b_max = max(20, b_bot - b_top)
        for i, ch in enumerate(CHANNELS_24):
            bx = bar_area_x + i * cw
            cnt = ch_count.get(ch, 0)
            bh = max(2, int(cnt / max_ch * b_max)) if cnt > 0 else 0
            col = _T["ok"] if cnt > max_ch * 0.5 else (_T["accent"] if cnt > 0 else _T["bg_elevated"])
            if bh > 0:
                d.rectangle((bx + 2, b_bot - bh, bx + cw - 3, b_bot), fill=col)
            d.text((bx + 2, b_bot + 6), str(ch), font=fonts["badge"], fill=_T["text_primary"] if cnt else _T["text_dim"])
    else:
        row_h = 18 if wide else (16 if h >= 200 else 11)
        for sec_name, cnt in sec_items:
            if y + row_h > y_bot - 18:
                break
            pct = cnt / max(total, 1)
            col = _sec_color(sec_name)
            d.text((pad + 2, y), sec_name[:8], font=fonts["sub"], fill=col)
            bx0 = pad + (66 if wide else 48)
            bx1 = w - pad - (34 if wide else 24)
            rj_theme.draw_progress_bar(d, (bx0, y + 2, bx1, y + (10 if wide else 7)), pct, fill=col, track=_T["bg_well"], outline=None, radius=2)
            d.text((bx1 + 4, y), str(cnt), font=fonts["sub"], fill=_T["text_muted"])
            y += row_h
        if total > 0 and y + 14 < y_bot:
            max_ch = max((ch_count.get(c, 0) for c in CHANNELS_24), default=1) or 1
            d.text((pad + 2, y_bot - 12), "CH:", font=fonts["sub"], fill=_T["text_dim"])
            cw = max(6, (w - pad * 2 - 24) // len(CHANNELS_24))
            for i, ch in enumerate(CHANNELS_24):
                bx = pad + 22 + i * cw
                cnt = ch_count.get(ch, 0)
                bh = max(1, int(cnt / max_ch * 12)) if cnt > 0 else 0
                if bh > 0:
                    d.rectangle((bx, y_bot - bh, bx + cw - 2, y_bot), fill=_T["ok"] if cnt > max_ch * 0.5 else _T["accent"])

    lcd.LCD_ShowImage(img, 0, 0)


def _draw_cards(lcd, font, font_sm, scroll_pos=0):
    w, h, fonts = _wd_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)

    scanning = _scanning.is_set()
    with lock:
        cards = list(card_state.items())
        n_aps = len(networks)

    n_cards = len(cards)
    hdr_h = rj_theme.draw_app_header(
        d, w, "WIFI CARDS", fonts,
        badge="SCAN" if scanning else "IDLE",
        right_pill=f"{n_cards} ACTIVE" if wide else "",
    )
    ftr_h = rj_theme.draw_app_footer(d, w, h, f"AP:{n_aps}  Up/Dn:Scroll  K1:Views  K3:Exit" if wide else f"AP:{n_aps} ^v:Scrl K1:Vw", fonts)

    pad = 8 if large else (4 if wide else 2)
    y0 = hdr_h + pad
    y1 = h - ftr_h - pad

    if not cards:
        rj_theme.draw_status_card(d, w, h, "No Active Cards", sub="Press OK to start wardriving scan", kind="info", fonts=fonts)
    else:
        card_h = 74 if large else (48 if wide else 34)
        vis = max(1, (y1 - y0) // card_h)
        start = min(scroll_pos, max(0, n_cards - vis))
        accents = [_T["accent_bright"], _T["ok"], _T["warn"], "#C284B8"]
        y = y0
        for i in range(start, min(n_cards, start + vis)):
            iface, st = cards[i]
            ch = st.get("channel", 0)
            band = st.get("band", "?")
            driver = st.get("driver", "?")
            pkts = st.get("packets", 0)
            chs = st.get("channels", [])
            status = st.get("status", "active")
            drops = st.get("rx_dropped", 0)
            col = accents[i % len(accents)]
            st_col = _T["ok"] if status == "active" else (_T["warn"] if status == "restarting" else _T["danger"])

            rj_theme.draw_card(d, (pad, y, w - pad - (5 if n_cards > vis else 0), y + card_h - 4), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=6 if wide else 3, accent_left=col, accent_width=4 if wide else 2)
            tx = pad + (12 if wide else 5)
            d.text((tx, y + (6 if large else 2)), iface, font=fonts["body_bold"] if wide else fonts["sub"], fill=col)
            ch_txt = f"CH {ch}" if ch else "IDLE"
            if wide:
                pw, _ = rj_theme.draw_pill(d, w - pad - 88, y + (5 if large else 2), status.upper()[:7], fonts["badge"], bg=_T["bg_elevated"], fg=st_col, outline=None, pad_x=5, pad_y=1)
                d.text((tx + (110 if large else 76), y + (6 if large else 2)), f"{ch_txt}   Band: {band}   Driver: {driver}", font=fonts["sub"], fill=_T["text_muted"])
            else:
                d.text((w - pad - 44, y + 2), ch_txt, font=fonts["sub"], fill=_T["text_bright"])
            pkts_str = f"{pkts // 1000}k" if pkts > 1000 else str(pkts)
            drop_str = f"  drop:{drops}" if drops > 0 else ""
            d.text((tx, y + (28 if large else (19 if wide else 13))), f"Pkts: {pkts_str}{drop_str}", font=fonts["sub"], fill=_T["text_primary"])
            if ch and scanning and chs:
                bar_y = y + (50 if large else (34 if wide else 23))
                pos = chs.index(ch) if ch in chs else 0
                frac = pos / max(1, len(chs) - 1)
                rj_theme.draw_progress_bar(d, (tx, bar_y, w - pad - 12, bar_y + (8 if large else 5)), frac, fill=col, track=_T["bg_well"], outline=_T["border_subtle"], radius=2)
            y += card_h
        if n_cards > vis:
            rj_theme.draw_scrollbar(d, w - 5, y0, y1, start, vis, n_cards, width=3)

    lcd.LCD_ShowImage(img, 0, 0)


def _draw_channels(lcd, font, font_sm):
    """Dashboard: AP count per channel with bar chart."""
    w, h, fonts = _wd_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)

    with lock:
        ch_count = dict(_inc_ch_count)
        total = len(_seen_bssids)

    hdr_h = rj_theme.draw_app_header(d, w, "CHANNELS", fonts, badge=f"{total} APs", right_pill="2.4 + 5 GHz" if wide else "")
    ftr_h = rj_theme.draw_app_footer(d, w, h, "K1:Views   K2:Export   K3:Exit" if wide else "K1:Vw K2:Exp K3:X", fonts)

    pad = 8 if large else (4 if wide else 2)
    y0 = hdr_h + pad
    y1 = h - ftr_h - pad
    mid_y = y0 + (y1 - y0) // 2

    def _draw_band(title, chs, bx0, by0, bx1, by1, hi_col, lo_col):
        rj_theme.draw_card(d, (bx0, by0, bx1, by1), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=6 if wide else 3)
        d.text((bx0 + (10 if wide else 4), by0 + (5 if wide else 2)), title, font=fonts["badge"], fill=hi_col)
        mx = max((ch_count.get(c, 0) for c in chs), default=1) or 1
        ix0 = bx0 + (10 if wide else 3)
        iw = max(20, (bx1 - bx0) - (20 if wide else 6))
        cw = max(6, iw // max(1, len(chs)))
        b_top = by0 + (24 if large else (16 if wide else 13))
        b_bot = by1 - (18 if large else (13 if wide else 10))
        b_h = max(8, b_bot - b_top)
        for i, ch in enumerate(chs):
            x = ix0 + i * cw
            cnt = ch_count.get(ch, 0)
            bh = max(2, int(cnt / mx * b_h)) if cnt > 0 else 0
            col = hi_col if cnt > mx * 0.5 else (lo_col if cnt > 0 else _T["bg_elevated"])
            if bh > 0:
                d.rectangle((x + 1, b_bot - bh, x + cw - 2, b_bot), fill=col)
                if wide and cnt > 0:
                    d.text((x + 1, max(by0 + 2, b_bot - bh - (12 if large else 9))), str(cnt), font=fonts["sub"], fill=col)
            lbl = str(ch) if (wide or ch < 100) else str(ch)[-2:]
            d.text((x + 1, b_bot + 2), lbl, font=fonts["sub"], fill=_T["text_primary"] if cnt else _T["text_dim"])

    _draw_band("2.4 GHz BAND", CHANNELS_24, pad, y0, w - pad, mid_y - 3, _T["ok"], _T["accent"])
    ch5_active = [ch for ch in CHANNELS_5 if ch_count.get(ch, 0) > 0]
    ch5_show = CHANNELS_5[:12 if wide else 8] if not ch5_active else sorted(set(CHANNELS_5[:8]) | set(ch5_active))[:(16 if wide else 12)]
    _draw_band("5 GHz BAND", ch5_show, pad, mid_y + 3, w - pad, y1, _T["accent_bright"], _T["accent_dim"])

    lcd.LCD_ShowImage(img, 0, 0)


_nets_cache = []
_nets_cache_sort = -1
_nets_cache_ts = 0


def _draw_networks(lcd, font, font_sm, scroll_pos, sort):
    global _nets_cache, _nets_cache_sort, _nets_cache_ts
    w, h, fonts = _wd_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)

    sort_names = ["Signal", "Name", "Security"]
    now = time.time()
    if now - _nets_cache_ts > 2.0 or sort != _nets_cache_sort:
        with lock:
            seen = set()
            nets = []
            for _, b in reversed(_recent_bssids):
                if b not in seen and b in networks:
                    seen.add(b)
                    nets.append(networks[b])
        if sort == 0:
            nets.sort(key=lambda n: n["signal"], reverse=True)
        elif sort == 1:
            nets.sort(key=lambda n: n["ssid"].lower())
        elif sort == 2:
            sec_order = {"Open": 0, "WEP": 1, "WPA": 2, "WPA2-PSK": 3,
                         "WPA2-EAP": 4, "WPA3-SAE": 5}
            nets.sort(key=lambda n: sec_order.get(n["security"], 3))
        _nets_cache = nets
        _nets_cache_sort = sort
        _nets_cache_ts = now
    nets = _nets_cache

    hdr_h = rj_theme.draw_app_header(
        d, w, "NETWORKS", fonts,
        badge=f"{len(nets)} APs",
        right_pill=f"SORT: {sort_names[sort]}" if wide else "",
    )
    ftr_h = rj_theme.draw_app_footer(d, w, h, "Up/Dn:Scroll  L/R:Sort  K1:Views  K3:Exit" if wide else "U/D:Scrl L/R:Srt K1:Vw", fonts)

    pad = 8 if large else (4 if wide else 2)
    y0 = hdr_h + pad
    y1 = h - ftr_h - pad

    if not nets:
        rj_theme.draw_status_card(d, w, h, "No Networks", sub="Start scanning with OK", kind="info", fonts=fonts)
    else:
        row_h = 24 if large else (19 if wide else (18 if h >= 200 else 12))
        vis = max(1, (y1 - y0) // row_h)
        start = max(0, min(scroll_pos, max(0, len(nets) - vis)))
        y = y0
        for idx_r, n in enumerate(nets[start:start + vis]):
            if idx_r % 2 == 0 and wide:
                d.rectangle((pad, y, w - pad - 5, y + row_h - 1), fill=_T["bg_surface"])
            sig = n["signal"]
            sec = n["security"]
            ch = n.get("channel", "-")
            has_gps = bool(n.get("gps"))
            rx = w - pad - 8
            db_s = f"{sig}dB" if wide else f"{sig}"
            dw = rj_theme.measure_text(fonts["sub"], db_s, d)[2]
            d.text((rx - dw, y + (4 if large else 1)), db_s, font=fonts["sub"], fill=_T["text_muted"])
            rx -= dw + (6 if wide else 3)
            if wide:
                rj_theme.draw_signal_bars(d, rx - 16, y + (4 if large else 2), 12 if large else 9, _signal_bar(sig) * 25, active_color=_T["ok"] if sig >= -60 else (_T["warn"] if sig >= -75 else _T["danger"]))
                rx -= 22
                pw, _ = rj_theme.draw_pill(d, rx - 58, y + (2 if large else 1), sec[:8], fonts["badge"], bg=_T["bg_elevated"], fg=_sec_color(sec), outline=None, pad_x=4, pad_y=1)
                rx -= pw + 8
                ch_s = f"CH{ch}"
                cw_p = rj_theme.measure_text(fonts["sub"], ch_s, d)[2]
                d.text((rx - cw_p, y + (4 if large else 1)), ch_s, font=fonts["sub"], fill=_T["accent_bright"])
                rx -= cw_p + 10
                if large:
                    b_s = n.get("bssid", "")
                    bw = rj_theme.measure_text(fonts["mono"], b_s, d)[2]
                    d.text((rx - bw, y + 4), b_s, font=fonts["mono"], fill=_T["text_dim"])
                    rx -= bw + 10
            else:
                sec_s = sec[:5]
                sw = rj_theme.measure_text(fonts["sub"], sec_s, d)[2]
                d.text((rx - sw, y + 1), sec_s, font=fonts["sub"], fill=_sec_color(sec))
                rx -= sw + 4
            tx = pad + (6 if wide else 2)
            gps_dot = "* " if has_gps else "  "
            d.text((tx, y + (3 if large else 1)), rj_theme.truncate_text(d, f"{gps_dot}{n['ssid']}", max(18, rx - tx - 2), fonts["body_bold"] if wide else fonts["sub"]), font=fonts["body_bold"] if wide else fonts["sub"], fill=_T["ok"] if has_gps else _T["text_primary"])
            y += row_h
        if len(nets) > vis:
            rj_theme.draw_scrollbar(d, w - 5, y0, y1, start, vis, len(nets), width=3)

    lcd.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# Live map view — tile download + real-time AP plotting
# ---------------------------------------------------------------------------

_MAP_TILE_CACHE = os.path.join(_RJ_BASE_DIR, 'loot', 'wardriving', '.tilecache')
_MAP_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
_map_bg = None
_map_bbox = None
_map_overlay_cache = None
_map_overlay_ts = 0
_map_overlay_count = 0


def _lat_to_merc(lat):
    lat = max(-85.0, min(85.0, lat))
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


_tile_download_lock = threading.Lock()


def _fetch_map_tile(z, x, y):
    """Load tile from cache, download in background if missing."""
    os.makedirs(_MAP_TILE_CACHE, exist_ok=True)
    cache_path = os.path.join(_MAP_TILE_CACHE, f"{z}_{x}_{y}.png")
    if os.path.isfile(cache_path):
        try:
            return Image.open(cache_path).convert("RGB")
        except Exception:
            pass
    url = _MAP_TILE_URL.format(z=z, x=x, y=y)
    try:
        with _tile_download_lock:
            if os.path.isfile(cache_path):
                return Image.open(cache_path).convert("RGB")
            req = urllib.request.Request(url, headers={"User-Agent": "RaspyJack/1.0"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = resp.read()
            with open(cache_path, "wb") as f:
                f.write(data)
            return Image.open(BytesIO(data)).convert("RGB")
    except Exception:
        return None


def _build_map_bg(lat, lon, width, height):
    """Build a background map centered on lat/lon. Returns (image, bbox)."""
    z = 15
    n = 2 ** z
    x_center = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(max(-85, min(85, lat)))
    y_center = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)

    big = Image.new("RGB", (3 * 256, 3 * 256), (14, 14, 16))
    for dx in range(-1, 2):
        for dy in range(-1, 2):
            tile = _fetch_map_tile(z, x_center + dx, y_center + dy)
            if tile:
                big.paste(tile, ((dx + 1) * 256, (dy + 1) * 256))

    nw_lon = (x_center - 1) / n * 360.0 - 180.0
    se_lon = (x_center + 2) / n * 360.0 - 180.0
    nw_lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y_center - 1) / n))))
    se_lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y_center + 2) / n))))

    nw_merc = _lat_to_merc(nw_lat)
    se_merc = _lat_to_merc(se_lat)

    darkened = ImageEnhance.Brightness(big).enhance(0.45)
    resized = darkened.resize((width, height), Image.BILINEAR)

    return resized, (nw_merc, se_merc, nw_lon, se_lon)


def _map_project(lat, lon, bbox, width, height):
    """Project lat/lon to pixel using Mercator."""
    nw_merc, se_merc, nw_lon, se_lon = bbox
    merc_span = nw_merc - se_merc
    lon_span = se_lon - nw_lon
    if merc_span == 0 or lon_span == 0:
        return width // 2, height // 2
    merc = _lat_to_merc(lat)
    x = int((lon - nw_lon) / lon_span * width)
    y = int((nw_merc - merc) / merc_span * height)
    return x, y


def _draw_map(lcd, font, font_sm):
    global _map_bg, _map_bbox, _map_overlay_cache, _map_overlay_ts, _map_overlay_count
    w, h, fonts = _wd_dims(lcd)

    with lock:
        gps_snap = dict(gps_data) if gps_data else None
        net_count = len(_seen_bssids)
        gps_nets_snap = []
        seen_b = set()
        for b in reversed(_gps_bssids):
            if b not in seen_b and b in networks:
                seen_b.add(b)
                n = networks[b]
                if n.get("gps"):
                    gps_nets_snap.append(n)

    scanning = _scanning.is_set()

    if not gps_snap:
        img = Image.new("RGB", (w, h), _T["bg_base"])
        d = ImageDraw.Draw(img)
        rj_theme.draw_app_header(d, w, "LIVE MAP", fonts, badge=f"AP:{net_count}")
        rj_theme.draw_status_card(d, w, h, "Waiting for GPS Fix", sub="Move outdoors to render live AP map", kind="warn", fonts=fonts)
        rj_theme.draw_app_footer(d, w, h, "K1:Views   K3:Exit", fonts)
        lcd.LCD_ShowImage(img, 0, 0)
        return

    cur_lat = gps_snap["lat"]
    cur_lon = gps_snap["lon"]

    need_reload = _map_bg is None or _map_bbox is None or _map_bg.size != (w, h)
    if not need_reload:
        cx, cy = _map_project(cur_lat, cur_lon, _map_bbox, w, h)
        margin = w // 8
        if cx < margin or cx > w - margin or cy < margin or cy > h - margin:
            need_reload = True
    if need_reload:
        if _map_bg is None:
            _loading = Image.new("RGB", (w, h), _T["bg_base"])
            _ld = ImageDraw.Draw(_loading)
            rj_theme.draw_app_header(_ld, w, "LIVE MAP", fonts, badge="LOADING")
            rj_theme.draw_status_card(_ld, w, h, "Loading Map Tiles...", sub=f"{cur_lat:.4f}, {cur_lon:.4f}", kind="info", fonts=fonts)
            lcd.LCD_ShowImage(_loading, 0, 0)
        try:
            _map_bg, _map_bbox = _build_map_bg(cur_lat, cur_lon, w, h)
            _map_overlay_cache = None
        except Exception:
            pass

    if _map_bg is not None and _map_bbox is not None:
        img = _map_bg.copy()
        d = ImageDraw.Draw(img)

        now_map = time.time()
        rebuild_overlay = (now_map - _map_overlay_ts > 3.0 or
                           _map_overlay_count != net_count or
                           _map_overlay_cache is None)
        if rebuild_overlay:
            gps_nets = list(gps_nets_snap)
            gps_nets.sort(key=lambda n: n.get("first_seen", ""))
            overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            if len(gps_nets) >= 2:
                pts = [_map_project(n["gps"]["lat"], n["gps"]["lon"], _map_bbox, w, h) for n in gps_nets]
                for i in range(len(pts) - 1):
                    x1, y1 = pts[i]
                    x2, y2 = pts[i + 1]
                    if (-10 <= x1 <= w + 10 and -10 <= y1 <= h + 10) or \
                       (-10 <= x2 <= w + 10 and -10 <= y2 <= h + 10):
                        od.line([(x1, y1), (x2, y2)], fill=(224, 122, 47, 190), width=2 if w >= 300 else 1)
            dot_r = 3 if w >= 300 else 2
            for n in gps_nets:
                x, y = _map_project(n["gps"]["lat"], n["gps"]["lon"], _map_bbox, w, h)
                if x < -5 or x > w + 5 or y < -5 or y > h + 5:
                    continue
                color = _sec_color(n.get("security", ""))
                od.ellipse([x - dot_r, y - dot_r, x + dot_r, y + dot_r], fill=color)
            _map_overlay_cache = overlay
            _map_overlay_ts = now_map
            _map_overlay_count = net_count

        if _map_overlay_cache:
            img.paste(_map_overlay_cache, (0, 0), _map_overlay_cache)

        cx, cy = _map_project(cur_lat, cur_lon, _map_bbox, w, h)
        d.line([(cx - 6, cy), (cx + 6, cy)], fill=_T["text_bright"], width=1)
        d.line([(cx, cy - 6), (cx, cy + 6)], fill=_T["text_bright"], width=1)
        d.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], outline=_T["accent_bright"], width=2)

        rj_theme.draw_app_header(
            d, w, "LIVE MAP", fonts,
            badge="SCAN" if scanning else "IDLE",
            right_pill=f"AP:{net_count} GPS:{len(gps_nets_snap)}",
        )
        rj_theme.draw_app_footer(d, w, h, "K1:Views   K2:Export   K3:Exit" if fonts["wide"] else "K1:Vw K3:X", fonts)
        lcd.LCD_ShowImage(img, 0, 0)
    else:
        img = Image.new("RGB", (w, h), _T["bg_base"])
        d = ImageDraw.Draw(img)
        rj_theme.draw_app_header(d, w, "LIVE MAP", fonts, badge="TILES")
        rj_theme.draw_status_card(d, w, h, "Map Loading...", sub="Fetching OpenStreetMap tiles", kind="info", fonts=fonts)
        rj_theme.draw_app_footer(d, w, h, "K1:Views   K3:Exit", fonts)
        lcd.LCD_ShowImage(img, 0, 0)


def _draw_export(lcd, font, font_sm, export_files):
    w, h, fonts = _wd_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)

    with lock:
        total = len(_seen_bssids)
        wigle_ready = _inc_wigle_count

    try:
        existing_cnt = len([f for f in os.listdir(LOOT_DIR) if f.endswith((".csv", ".json", ".kml"))])
    except Exception:
        existing_cnt = 0

    hdr_h = rj_theme.draw_app_header(d, w, "EXPORT LOOT", fonts, badge="WIGLE/KML", right_pill=f"{existing_cnt} FILES" if wide else "")
    ftr_h = rj_theme.draw_app_footer(d, w, h, "KEY2:Export Now   K1:Views   K3:Exit" if wide else "K2:Exp K1:Vw K3:X", fonts)

    pad = 8 if large else (4 if wide else 2)
    y0 = hdr_h + pad
    y1 = h - ftr_h - pad

    if w >= 480:
        kpi_h = 64
        gap = 8
        tw = (w - pad * 2 - gap * 2) // 3
        for idx_k, (lbl, val, acc) in enumerate([
            ("TOTAL APs", f"{total:,}", _T["text_bright"]),
            ("GEOTAGGED (WIGLE)", f"{wigle_ready:,}", _T["ok"]),
            ("NO GPS", f"{max(0, total - wigle_ready):,}", _T["danger"]),
        ]):
            kx = pad + idx_k * (tw + gap)
            rj_theme.draw_card(d, (kx, y0, kx + tw, y0 + kpi_h), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=6, accent_left=acc, accent_width=4)
            d.text((kx + 12, y0 + 10), lbl, font=fonts["badge"], fill=_T["text_muted"])
            d.text((kx + 12, y0 + 30), val, font=fonts["title"], fill=acc)
        by0 = y0 + kpi_h + 10
        rj_theme.draw_card(d, (pad, by0, w - pad, y1), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=8, accent_left=_T["accent"], accent_width=4)
        lx = pad + 14
        ly = by0 + 12
        d.text((lx, ly), f"RECENT EXPORTS  ({existing_cnt} files in loot/wardriving)", font=fonts["badge"], fill=_T["accent_bright"])
        ly += 24
        if export_files:
            for f in export_files[-5:]:
                d.text((lx, ly), rj_theme.truncate_text(d, os.path.basename(f), w - pad * 2 - 28, fonts["mono"]), font=fonts["mono"], fill=_T["ok"])
                ly += 22
        else:
            d.text((lx, ly), "No manual exports yet — press KEY2 / F2 to generate Wigle CSV, JSON & KML.", font=fonts["body"], fill=_T["text_muted"])
    else:
        rj_theme.draw_card(d, (pad, y0, w - pad, y1), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=5 if wide else 3)
        tx = pad + (8 if wide else 4)
        mw = w - pad - tx - 4
        y = y0 + (6 if wide else 4)
        step = 16 if wide else 12
        d.text((tx, y), f"APs:{total}  GPS:{wigle_ready}  NoGPS:{max(0, total - wigle_ready)}" if wide else f"AP:{total}  GPS:{wigle_ready}", font=fonts["body_bold"] if wide else fonts["sub"], fill=_T["ok"])
        y += step
        if export_files:
            for f in export_files[-3:]:
                if y + 10 > y1:
                    break
                d.text((tx, y), rj_theme.truncate_text(d, os.path.basename(f), mw, fonts["sub"]), font=fonts["sub"], fill=_T["accent_bright"])
                y += step - 1
        else:
            d.text((tx, y), "Press KEY2 to export", font=fonts["sub"], fill=_T["text_muted"])
            y += step
        if y + 10 <= y1:
            d.text((tx, y1 - (14 if wide else 11)), f"Loot dir: {existing_cnt} files", font=fonts["sub"], fill=_T["text_dim"])

    lcd.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# Session management + Auto-save
# ---------------------------------------------------------------------------

SESSION_DIR = os.path.join(LOOT_DIR, "sessions")
_session_id = ""
_session_wigle_path = ""
_session_json_path = ""


def _init_session():
    """Create a new session with timestamped files."""
    global _session_id, _session_wigle_path, _session_json_path
    os.makedirs(SESSION_DIR, exist_ok=True)
    _session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    _session_wigle_path = os.path.join(SESSION_DIR, f"session_{_session_id}_wigle.csv")
    _session_json_path = os.path.join(SESSION_DIR, f"session_{_session_id}.json")
    # Create session metadata
    meta = {
        "session_id": _session_id,
        "start_time": datetime.now().isoformat(),
        "device": "RaspyJack",
        "networks": 0,
        "wigle_ready": 0,
    }
    try:
        with open(_session_json_path, "w") as f:
            json.dump(meta, f, indent=2)
    except Exception:
        pass


def _prune_networks():
    """Remove oldest-inserted networks via FIFO. O(k) instead of O(n log n)."""
    global _inc_wigle_count
    with lock:
        over = len(networks) - MAX_NETWORKS
        if over <= 0:
            return
        evicted = 0
        while evicted < over and _insertion_order:
            b = _insertion_order.popleft()
            if b not in networks:
                continue
            net = networks[b]
            sec = net["security"]
            ch = net["channel"]
            _inc_sec_count[sec] = max(0, _inc_sec_count.get(sec, 1) - 1)
            _inc_ch_count[ch] = max(0, _inc_ch_count.get(ch, 1) - 1)
            if net.get("gps"):
                _inc_wigle_count = max(0, _inc_wigle_count - 1)
            _dirty_bssids.discard(b)
            del networks[b]
            evicted += 1


_autosave_counter = 0


def _prune_probes():
    """Evict probes not seen in 5 minutes to bound memory."""
    cutoff = time.time() - 300
    with lock:
        stale = [k for k, v in probes.items() if v.get("_ts", 0) < cutoff]
        for k in stale:
            del probes[k]


def _watchdog_thread():
    """Monitor card health: detect stale sniffers, USB disconnects, kernel drops."""
    _prev_packets = {}
    _stale_count = {}
    while not _shutdown.is_set() and _scanning.is_set():
        if _shutdown.wait(timeout=10):
            return
        with lock:
            ifaces = list(card_state.keys())
        for iface in ifaces:
            if not os.path.isdir(f"/sys/class/net/{iface}"):
                with lock:
                    if iface in card_state:
                        card_state[iface]["status"] = "disconnected"
                continue

            with lock:
                cur = card_state.get(iface, {}).get("packets", 0)

            prev = _prev_packets.get(iface, 0)
            _prev_packets[iface] = cur
            if cur == prev:
                _stale_count[iface] = _stale_count.get(iface, 0) + 1
                if _stale_count[iface] >= 3:
                    _restart_monitor_mode(iface)
                    _stale_count[iface] = 0
                    with lock:
                        if iface in card_state:
                            card_state[iface]["status"] = "restarted"
            else:
                _stale_count[iface] = 0
                with lock:
                    if iface in card_state:
                        card_state[iface]["status"] = "active"

            drops = _read_rx_dropped(iface)
            with lock:
                if iface in card_state:
                    card_state[iface]["rx_dropped"] = drops

        try:
            with open("/proc/self/statm") as f:
                rss_pages = int(f.read().split()[1])
            rss_mb = rss_pages * 4096 // (1024 * 1024)
            if rss_mb > 350:
                import gc
                gc.collect()
                _prune_probes()
        except Exception:
            pass


def _autosave_thread():
    """Background thread: periodic DB save and session metadata update."""
    global _autosave_counter
    while not _shutdown.is_set():
        if _shutdown.wait(timeout=AUTOSAVE_INTERVAL):
            break
        if not _scanning.is_set():
            continue
        _autosave_counter += 1
        _flush_csv_buffer()
        _prune_networks()
        _save_session_meta()
        if _autosave_counter % DB_SAVE_TICKS == 0:
            _save_to_db()
            _prune_probes()
    # Final save on shutdown
    _flush_csv_buffer()
    _save_to_db()
    _save_session_meta()


def _save_session_meta():
    """Update session metadata JSON."""
    if not _session_json_path:
        return
    with lock:
        total = len(_seen_bssids)
        wigle = total
    try:
        meta = {
            "session_id": _session_id,
            "start_time": _session_id,
            "end_time": datetime.now().isoformat(),
            "device": "RaspyJack",
            "networks": total,
            "wigle_ready": wigle,
            "duration_seconds": int(time.time() - scan_start_time) if scan_start_time else 0,
        }
        with open(_session_json_path, "w") as f:
            json.dump(meta, f, indent=2)
    except Exception:
        pass


_WIGLE_HEADER = ("WigleWifi-1.4,appRelease=RaspyJack-v2,model=RaspberryPi,"
                  "release=2.0,device=RaspyJack,display=LCD144,"
                  "board=RaspberryPi,brand=7h30th3r0n3\n")
_WIGLE_COLS = ("MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,"
               "CurrentLatitude,CurrentLongitude,AltitudeMeters,"
               "AccuracyMeters,Type\n")


_csv_buffer = deque(maxlen=10000)


def _append_live_csv(bssid, net):
    """Queue a network for CSV write. Flushed periodically by autosave."""
    gps = net.get("gps")
    if not gps:
        return
    auth = _security_to_wigle(net["security"], net["cipher"], net.get("auth", ""))
    _csv_buffer.append([
        bssid, net["ssid"], auth, _wigle_time(gps["ts"]),
        net["channel"], net["signal"],
        f"{gps['lat']:.6f}", f"{gps['lon']:.6f}",
        f"{gps.get('alt', 0):.1f}", "10", "WIFI",
    ])


def _flush_csv_buffer():
    """Write buffered CSV rows to session file. Called from autosave thread."""
    if not _csv_buffer or not _session_wigle_path:
        return
    try:
        path = _session_wigle_path
        is_new = not os.path.isfile(path) or os.path.getsize(path) < 10
        with open(path, "a", newline="") as f:
            if is_new:
                f.write(_WIGLE_HEADER)
                f.write(_WIGLE_COLS)
            writer = csv.writer(f)
            while _csv_buffer:
                writer.writerow(_csv_buffer.popleft())
    except Exception:
        pass






# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _emergency_save(signum=None, frame=None):
    """Emergency save on crash or signal."""
    try:
        _flush_csv_buffer()
        _save_to_db()
        _save_session_meta()
    except Exception:
        pass


def main():
    global view_idx, scroll, sort_mode, dual_mode, live_sort

    os.makedirs(LOOT_DIR, exist_ok=True)
    signal.signal(signal.SIGTERM, _emergency_save)
    signal.signal(signal.SIGINT, _emergency_save)

    GPIO.setmode(GPIO.BCM)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    LCD_Config.GPIO_Init()
    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    lcd.LCD_Clear()
    font = scaled_font(10)
    font_sm = scaled_font(8)

    w, h, fonts = _wd_dims(lcd)

    def _show_wd_card(title, sub="", kind="info", foot=""):
        img_c = Image.new("RGB", (w, h), _T["bg_base"])
        dc = ImageDraw.Draw(img_c)
        rj_theme.draw_app_header(dc, w, "WARDRIVING", fonts, badge="STATUS")
        rj_theme.draw_status_card(dc, w, h, title, sub=sub, kind=kind, foot=foot, fonts=fonts)
        lcd.LCD_ShowImage(img_c, 0, 0)

    def _show_scan_mode_picker(sel):
        img_m = Image.new("RGB", (w, h), _T["bg_base"])
        dm = ImageDraw.Draw(img_m)
        hdr_h = rj_theme.draw_app_header(dm, w, "SCAN MODE", fonts, badge="SELECT")
        ftr_h = rj_theme.draw_app_footer(dm, w, h, "Up/Dn:Choose   OK:Confirm   K3:Cancel" if fonts["wide"] else "U/D:Pick OK:Go", fonts)
        pad = 12 if fonts["large"] else (6 if fonts["wide"] else 4)
        opts = [
            ("PASSIVE (Stealth)", "Zero TX — silent beacon & probe monitor"),
            ("ACTIVE (Probe)", "Broadcast probes + managed iw scan"),
        ]
        ch = 58 if fonts["large"] else (38 if fonts["wide"] else 30)
        total_h = ch * 2 + pad
        y_m = hdr_h + max(4, (h - hdr_h - ftr_h - total_h) // 2)
        for idx_o, (otitle, osub) in enumerate(opts):
            is_s = idx_o == sel
            rj_theme.draw_card(
                dm, (pad, y_m, w - pad, y_m + ch),
                fill=_T["select_bg"] if is_s else _T["bg_surface"],
                outline=_T["accent"] if is_s else _T["border_subtle"],
                radius=6 if fonts["wide"] else 3,
                accent_left=_T["ok"] if is_s else _T["border_subtle"],
                accent_width=4 if fonts["wide"] else 2,
            )
            tx = pad + (14 if fonts["wide"] else 6)
            dm.text((tx, y_m + (10 if fonts["large"] else 4)), otitle, font=fonts["title"] if fonts["large"] else fonts["body_bold"], fill=_T["ok"] if is_s else _T["text_primary"])
            if fonts["wide"] or h >= 200:
                dm.text((tx, y_m + (34 if fonts["large"] else 20)), rj_theme.truncate_text(dm, osub, w - pad * 2 - 20, fonts["sub"]), font=fonts["sub"], fill=_T["text_muted"])
            y_m += ch + pad
        lcd.LCD_ShowImage(img_m, 0, 0)

    if not SCAPY_OK:
        _show_wd_card("Scapy Not Found", sub="Install python3-scapy", kind="err")
        time.sleep(3)
        GPIO.cleanup()
        return 1

    _init_db()
    _load_seen_from_db()

    # Start GPS thread
    gps_thread = threading.Thread(target=_gps_updater, daemon=True)
    gps_thread.start()

    # Show splash
    if not AUTO_MODE:
        _show_wd_card(
            "EmberJack Wardriving",
            sub="Multi-card WiFi + GPS + Wigle CSV/KML",
            kind="ok",
            foot="Press OK to start scanning",
        )
        time.sleep(1.5)

    export_files = []
    threads = []
    _auto_started = False

    # Start background threads
    threading.Thread(target=_autosave_thread, daemon=True).start()
    threading.Thread(target=_watchdog_thread, daemon=True).start()

    try:
        while not _shutdown.is_set():
            # Auto-mode: simulate OK press on first loop iteration
            if AUTO_MODE and not _auto_started:
                btn = "OK"
                _auto_started = True
            else:
                btn = get_button(PINS, GPIO)

            # KEY3 = exit
            if btn == "KEY3":
                break

            # OK = toggle scan
            if btn == "OK":
                if not _scanning.is_set():
                    _scan_active_mode = False
                    _mode_sel = 0
                    mb = "OK"

                    if not AUTO_MODE:
                        _show_scan_mode_picker(_mode_sel)

                        while True:
                            mb = get_button(PINS, GPIO)
                            if mb == "KEY3":
                                break
                            if mb in ("UP", "DOWN"):
                                _mode_sel = 1 - _mode_sel
                                _show_scan_mode_picker(_mode_sel)
                            if mb == "OK":
                                _scan_active_mode = (_mode_sel == 1)
                                break
                            time.sleep(0.05)

                    if mb == "KEY3":
                        continue

                    ifaces = _find_monitor_interfaces()

                    _scanning.set()
                    global scan_start_time
                    scan_start_time = time.time()
                    _init_session()
                    mon_ifaces.clear()

                    mode_txt = "ACTIVE" if _scan_active_mode else "PASSIVE"
                    _show_wd_card(f"Starting {mode_txt} Scan...", sub=f"{len(ifaces)} USB card(s) + onboard wlan0", kind="warn")

                    active_cards, monitor_card = _assign_card_roles(ifaces)

                    # Setup monitor mode on ONE card only (for probes/beacons)
                    if monitor_card:
                        mon = _monitor_up(monitor_card)
                        if mon:
                            mon_ifaces.append(mon)
                            card_state[mon] = {
                                "channel": 0, "channels": CHANNELS_24 + CHANNELS_5,
                                "band": "monitor", "driver": _get_driver(monitor_card),
                                "packets": 0, "status": "active", "role": "monitor",
                            }
                            t = threading.Thread(target=_raw_monitor_worker,
                                                 args=(mon,), daemon=True)
                            t.start()
                            threads.append(t)
                            t = threading.Thread(target=_monitor_channel_hopper,
                                                 args=(mon, _scan_active_mode), daemon=True)
                            t.start()
                            threads.append(t)

                    # Active scan workers (managed mode — no monitor setup)
                    # Detect 5GHz support per card before assigning frequencies
                    cards_5g_capable = set()
                    for iface in active_cards:
                        subprocess.run(["sudo", "ip", "link", "set", iface, "up"],
                                       capture_output=True, timeout=5)
                    time.sleep(0.5)
                    for iface in active_cards:
                        r = subprocess.run(
                            ["sudo", "iw", "dev", iface, "scan", "freq", "5180"],
                            capture_output=True, timeout=10)
                        if r.returncode == 0:
                            cards_5g_capable.add(iface)

                    n_active = max(len(active_cards), 1)
                    stagger_step = 3.0 / n_active
                    for idx, iface in enumerate(active_cards):
                        if iface in cards_5g_capable:
                            avail_freqs = _IW_FREQS_24 + _IW_FREQS_5
                        else:
                            avail_freqs = _IW_FREQS_24
                        card_freqs = [avail_freqs[i] for i in range(idx, len(avail_freqs), n_active)]
                        band_str = "2.4+5" if iface in cards_5g_capable else "2.4"
                        card_state[iface] = {
                            "channel": 0, "channels": card_freqs,
                            "band": band_str, "driver": _get_driver(iface),
                            "packets": 0, "status": "active", "role": "scan",
                        }
                        _passive = not _scan_active_mode
                        t = threading.Thread(target=_active_scan_worker,
                                             args=(iface, card_freqs, idx * stagger_step, _passive),
                                             daemon=True)
                        t.start()
                        threads.append(t)

                    # Always include wlan0 as scanner
                    if os.path.isdir("/sys/class/net/wlan0/wireless"):
                        wlan0_in_use = "wlan0" in active_cards or (monitor_card == "wlan0")
                        if not wlan0_in_use:
                            card_state["wlan0"] = {
                                "channel": 0, "channels": [],
                                "band": "onboard", "driver": "brcmfmac",
                                "packets": 0, "status": "active", "role": "scan",
                            }
                            _passive = not _scan_active_mode
                            t = threading.Thread(target=_active_scan_worker,
                                                 args=("wlan0", _IW_FREQS_24, 1.0, _passive),
                                                 daemon=True)
                            t.start()
                            threads.append(t)

                    dual_mode = len(active_cards) + len(mon_ifaces) >= 2

                    if not active_cards and not mon_ifaces:
                        _show_wd_card("Onboard Scan Mode", sub="No USB cards — using wlan0 active scan", kind="warn")
                        time.sleep(1.5)

                else:
                    # Stop scan
                    _scanning.clear()
                    time.sleep(0.3)
                    try:
                        _save_to_db()
                        _save_session_meta()
                    except Exception:
                        pass

                time.sleep(0.3)

            # KEY1 = cycle views
            elif btn == "KEY1":
                view_idx = (view_idx + 1) % len(VIEWS)
                scroll = 0
                time.sleep(0.25)

            # KEY2 = export
            elif btn == "KEY2":
                _show_wd_card("Exporting Loot...", sub="Writing Wigle CSV, JSON & KML", kind="warn")
                export_files = _export_all()
                if export_files:
                    _show_wd_card("Export Complete!", sub=f"{len(export_files)} file(s) saved to loot/wardriving", kind="ok")
                else:
                    _show_wd_card("Nothing to Export", sub="No networks captured yet", kind="err")
                time.sleep(1.5)

            # UP/DOWN = scroll (networks view)
            elif btn == "UP":
                scroll = max(0, scroll - 1)
                time.sleep(0.12)
            elif btn == "DOWN":
                with lock:
                    max_s = max(0, len(networks) - 8)
                scroll = min(max_s, scroll + 1)
                time.sleep(0.12)

            # LEFT/RIGHT = sort
            elif btn == "LEFT" or btn == "RIGHT":
                current_view = VIEWS[view_idx]
                if current_view == "live":
                    live_sort = (live_sort + (1 if btn == "RIGHT" else -1)) % 4
                elif current_view == "networks":
                    sort_mode = (sort_mode + 1) % 3
                time.sleep(0.2)

            # Draw current view
            try:
                current_view = VIEWS[view_idx]
                if current_view == "live":
                    _draw_live(lcd, font, font_sm)
                elif current_view == "map":
                    _draw_map(lcd, font, font_sm)
                elif current_view == "gps":
                    _draw_gps(lcd, font, font_sm)
                elif current_view == "cards":
                    _draw_cards(lcd, font, font_sm, scroll)
                elif current_view == "channels":
                    _draw_channels(lcd, font, font_sm)
                elif current_view == "stats":
                    _draw_stats(lcd, font, font_sm)
                elif current_view == "networks":
                    _draw_networks(lcd, font, font_sm, scroll, sort_mode)
                elif current_view == "export":
                    _draw_export(lcd, font, font_sm, export_files)
            except Exception:
                pass

            time.sleep(0.1 if _scanning.is_set() else 0.05)

    finally:
        _shutdown.set()
        _scanning.clear()
        _close_db()

        def _exit_msg(text):
            try:
                _show_wd_card(text, sub="Cleaning up wardriving session", kind="warn")
            except Exception:
                pass

        _exit_msg("Saving data...")
        _save_to_db()
        _save_session_meta()

        if mon_ifaces:
            _exit_msg("Restoring WiFi...")
            for iface in mon_ifaces:
                _monitor_down(iface)

        _exit_msg("Restarting network...")
        subprocess.run(["systemctl", "restart", "NetworkManager"],
                       capture_output=True, timeout=10)

        try:
            lcd.LCD_Clear()
        except Exception:
            pass
        GPIO.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
