#!/usr/bin/env python3
"""
RaspyJack Payload -- PCAP Analyzer
====================================
Author: 7h30th3r0n3

Browse and analyze pcap/cap/pcapng files directly on the device.
Supports both WiFi captures and network (ethernet) captures.

Dashboards:
  OVERVIEW    Packet count, duration, file size, capture type
  PROTOCOLS   Protocol distribution bar chart (TCP/UDP/DNS/HTTP/ICMP...)
  TOP TALKERS Top source/destination IPs by packet count
  WIFI        SSIDs, BSSIDs, clients, handshakes, PMKIDs detected
  PORTS       Top destination ports
  DNS         Top DNS queries
  TIMELINE    Packets-per-second sparkline
  CREDENTIALS Detected cleartext credentials / interesting strings

Controls:
  UP / DOWN   Navigate file list or scroll dashboard
  OK          Select file / Analyze
  LEFT/RIGHT  Switch dashboard view
  KEY1        Export analysis to JSON
  KEY2        Re-scan files
  KEY3        Back / Exit
"""

import os
import sys
import time
import json
import subprocess
import threading
from collections import defaultdict, deque, Counter
from datetime import datetime

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import LCD_Config
import rj_theme
from PIL import Image, ImageDraw
from payloads._display_helper import ScaledDraw, scaled_font
from payloads._input_helper import get_button

_T = rj_theme.EMBER_THEME

try:
    from scapy.all import (
        PcapReader, Dot11, Dot11Beacon, Dot11Elt, Dot11ProbeReq,
        Dot11ProbeResp, Dot11Auth, Dot11AssoReq, Dot11Deauth,
        EAPOL, IP, IPv6, TCP, UDP, DNS, DNSQR, ICMP, ARP, Ether,
        Raw, conf,
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

LOOT_DIR = "/root/Raspyjack/loot"
EXPORT_DIR = os.path.join(LOOT_DIR, "PCAPAnalyzer")
PCAP_EXTENSIONS = (".pcap", ".cap", ".pcapng")

# Interesting ports for credential sniffing
CLEARTEXT_PORTS = {21, 23, 25, 80, 110, 143, 389, 445, 587, 993, 995, 8080}
CRED_KEYWORDS = [b"user", b"pass", b"login", b"auth", b"token", b"cookie",
                 b"session", b"api_key", b"apikey", b"secret"]

# Dashboard views
VIEWS = ["overview", "protocols", "talkers", "wifi", "ports", "dns",
         "timeline", "creds", "files"]
VIEW_NAMES = {
    "overview": "OVERVIEW",
    "protocols": "PROTOCOLS",
    "talkers": "TOP TALKERS",
    "wifi": "WIFI",
    "ports": "PORTS",
    "dns": "DNS QUERIES",
    "timeline": "TIMELINE",
    "creds": "CREDENTIALS",
    "files": "FILES",
}

EXTRACT_DIR = os.path.join(LOOT_DIR, "PCAPAnalyzer", "extracted")


# ---------------------------------------------------------------------------
# PCAP file discovery
# ---------------------------------------------------------------------------


def _find_pcap_files():
    found = []
    for root, _dirs, files in os.walk(LOOT_DIR):
        for fname in sorted(files):
            if fname.lower().endswith(PCAP_EXTENSIONS):
                filepath = os.path.join(root, fname)
                try:
                    size = os.path.getsize(filepath)
                    rel = os.path.relpath(filepath, LOOT_DIR)
                    found.append({"path": filepath, "name": fname,
                                  "rel": rel, "size": size})
                except OSError:
                    pass
    found.sort(key=lambda x: x["name"].lower())
    return found


def _fmt_size(nbytes):
    for unit in ("B", "K", "M", "G"):
        if nbytes < 1024:
            return f"{nbytes:.0f}{unit}" if nbytes == int(nbytes) else f"{nbytes:.1f}{unit}"
        nbytes /= 1024
    return f"{nbytes:.1f}T"


# ---------------------------------------------------------------------------
# Analysis engine
# ---------------------------------------------------------------------------


class PcapAnalysis:
    """Analyze a pcap file using tshark (fast C engine) with scapy fallback."""

    def __init__(self, filepath):
        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        self.filesize = os.path.getsize(filepath)

        # General stats
        self.total_packets = 0
        self.total_bytes = 0
        self.first_ts = None
        self.last_ts = None
        self.is_wifi = False
        self.is_network = False

        # Protocol distribution
        self.proto_packets = Counter()
        self.proto_bytes = Counter()

        # IP talkers
        self.src_ips = Counter()
        self.dst_ips = Counter()

        # Ports
        self.dst_ports = Counter()

        # DNS
        self.dns_queries = Counter()

        # WiFi
        self.wifi_ssids = {}
        self.wifi_clients = set()
        self.wifi_probes = Counter()
        self.wifi_deauths = 0
        self.wifi_eapol = 0
        self.wifi_handshakes = set()
        self.wifi_pmkid = 0
        self.wifi_channels = Counter()
        self._eapol_pairs = defaultdict(int)

        # Timeline
        self._timeline_buckets = defaultdict(int)

        # Credentials
        self.credentials = []
        self._max_creds = 50

        # Extracted files
        self.extracted_files = []   # [{"name", "size", "protocol", "src"}]
        self.extract_dir = None

        # Progress
        self.progress = 0
        self.analyzing = False
        self.done = False
        self.error = None

    # Threshold: files under 2MB use scapy (instant), above use tshark
    TSHARK_THRESHOLD = 2 * 1024 * 1024

    def analyze(self):
        self.analyzing = True
        # Small files → scapy (no startup overhead, instant for handshakes)
        # Large files → tshark (C engine, 50-100x faster on big pcaps)
        if self.filesize > self.TSHARK_THRESHOLD and self._has_tshark():
            self._analyze_tshark()
        else:
            self._analyze_scapy()
        self.progress = 100
        self.analyzing = False
        self.done = True

    def _has_tshark(self):
        try:
            r = subprocess.run(["tshark", "--version"], capture_output=True,
                               timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # TSHARK ENGINE (fast)
    # ------------------------------------------------------------------

    def _analyze_tshark(self):
        """Single-pass tshark field extraction + targeted queries."""
        self.progress = 5

        # --- Pass 1: General stats via capinfos (instant) ---
        self._tshark_capinfos()
        self.progress = 15

        # --- Pass 2: Protocol hierarchy ---
        self._tshark_protocols()
        self.progress = 30

        # --- Pass 3: IP endpoints ---
        self._tshark_endpoints()
        self.progress = 45

        # --- Pass 4: DNS queries ---
        self._tshark_dns()
        self.progress = 55

        # --- Pass 5: Port stats ---
        self._tshark_ports()
        self.progress = 65

        # --- Pass 6: Timeline ---
        self._tshark_timeline()
        self.progress = 75

        # --- Pass 7: WiFi specific ---
        if self.is_wifi:
            self._tshark_wifi()
        self.progress = 85

        # --- Pass 8: Credentials ---
        self._tshark_creds()
        self.progress = 90

        # --- Pass 9: File extraction ---
        self._tshark_extract_files()
        self.progress = 95

    def _run_tshark(self, args, timeout=60):
        """Run tshark with args, return stdout lines.

        Accept non-zero exit codes — tshark returns errors for truncated
        pcaps or warnings but still outputs valid data.
        """
        cmd = ["tshark", "-r", self.filepath, "-n"] + args
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout)
            output = r.stdout.strip()
            if output:
                return output.splitlines()
        except Exception:
            pass
        return []

    def _tshark_capinfos(self):
        """Get basic capture info via capinfos."""
        try:
            r = subprocess.run(
                ["capinfos", "-M", self.filepath],
                capture_output=True, text=True, timeout=30)
            for line in r.stdout.splitlines():
                if "Number of packets:" in line:
                    try:
                        self.total_packets = int(
                            line.split(":")[-1].strip().replace(",", ""))
                    except Exception:
                        pass
                elif "Data size:" in line or "Data byte count:" in line:
                    try:
                        val = line.split(":")[-1].strip().split()[0].replace(",", "")
                        self.total_bytes = int(val)
                    except Exception:
                        pass
                elif "Earliest packet time:" in line:
                    pass  # parsed from tshark below
                elif "File encapsulation:" in line:
                    encap = line.split(":")[-1].strip().lower()
                    if "802.11" in encap or "ieee" in encap or "radiotap" in encap:
                        self.is_wifi = True
                    elif "ether" in encap or "linux" in encap:
                        self.is_network = True
                elif "Capture duration:" in line:
                    try:
                        dur = float(line.split(":")[-1].strip().split()[0])
                        self._capinfos_duration = dur
                    except Exception:
                        pass
        except Exception:
            pass

        # Get first + last timestamps
        lines = self._run_tshark(
            ["-T", "fields", "-e", "frame.time_epoch", "-c", "1"],
            timeout=10)
        if lines:
            try:
                self.first_ts = float(lines[0].strip())
            except Exception:
                pass

        # Last packet: tail approach
        lines = self._run_tshark(
            ["-T", "fields", "-e", "frame.time_epoch", "-e", "frame.len"],
            timeout=60)
        if lines:
            if self.total_packets == 0:
                self.total_packets = len(lines)
            total_b = 0
            for l in lines:
                p = l.split("\t")
                if p[0]:
                    try:
                        self.last_ts = float(p[0])
                    except Exception:
                        pass
                    if self.first_ts is None:
                        try:
                            self.first_ts = float(p[0])
                        except Exception:
                            pass
                if len(p) >= 2 and p[1]:
                    try:
                        total_b += int(p[1])
                    except Exception:
                        pass
            if self.total_bytes == 0:
                self.total_bytes = total_b

        if self.total_bytes == 0:
            self.total_bytes = self.filesize

    def _tshark_protocols(self):
        """Protocol hierarchy via -z io,phs."""
        lines = self._run_tshark(["-q", "-z", "io,phs"], timeout=30)

        PROTO_MAP = {
            "http": "HTTP", "tls": "HTTPS", "dns": "DNS", "ssh": "SSH",
            "ftp": "FTP", "telnet": "TELNET", "smtp": "SMTP", "smb": "SMB",
            "smb2": "SMB", "dhcp": "DHCP", "mdns": "mDNS", "ssdp": "SSDP",
            "arp": "ARP", "icmp": "ICMP", "tcp": "TCP", "udp": "UDP",
            "eapol": "EAPOL", "wlan": "WLAN", "quic": "QUIC",
            "ntp": "NTP", "snmp": "SNMP", "data": "DATA",
        }

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("=") \
               or stripped.startswith("Filter") \
               or stripped.startswith("Protocol"):
                continue
            # Format: "  proto  frames:NNN bytes:NNN"
            if "frames:" not in stripped:
                continue
            # Extract proto name (first word) and frames count
            proto_name = stripped.split()[0].lower()
            try:
                frames_part = stripped.split("frames:")[1]
                frames = int(frames_part.split()[0])
            except Exception:
                continue
            mapped = PROTO_MAP.get(proto_name)
            if mapped and frames > 0:
                self.proto_packets[mapped] = max(
                    self.proto_packets.get(mapped, 0), frames)

    def _tshark_endpoints(self):
        """IP endpoints for top talkers."""
        for proto in ("ip", "ipv6"):
            lines = self._run_tshark(
                ["-q", "-z", f"endpoints,{proto}"], timeout=30)
            in_data = False
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("=") or stripped.startswith("Filter"):
                    continue
                if "Packets" in stripped or "IPv" in stripped:
                    in_data = True
                    continue
                if not in_data:
                    continue
                parts = stripped.split()
                if len(parts) >= 2:
                    ip = parts[0]
                    if proto == "ipv6":
                        ip = ip[-15:]
                    try:
                        pkts = int(parts[1].replace(",", ""))
                    except Exception:
                        continue
                    self.src_ips[ip] += pkts

    def _tshark_dns(self):
        """Extract DNS query names."""
        lines = self._run_tshark(
            ["-T", "fields", "-e", "dns.qry.name",
             "-Y", "dns", "-c", "5000"],
            timeout=30)
        for line in lines:
            qname = line.strip().rstrip(".")
            if qname:
                self.dns_queries[qname] += 1

    def _tshark_ports(self):
        """Top destination ports."""
        # TCP
        lines = self._run_tshark(
            ["-T", "fields", "-e", "tcp.dstport",
             "-Y", "tcp", "-c", "10000"],
            timeout=30)
        for line in lines:
            try:
                port = int(line.strip())
                self.dst_ports[port] += 1
            except Exception:
                pass

        # UDP
        lines = self._run_tshark(
            ["-T", "fields", "-e", "udp.dstport",
             "-Y", "udp", "-c", "10000"],
            timeout=30)
        for line in lines:
            try:
                port = int(line.strip())
                self.dst_ports[port] += 1
            except Exception:
                pass

    def _tshark_timeline(self):
        """Packets per second via io,stat."""
        # Use 10s buckets for manageable output
        lines = self._run_tshark(
            ["-q", "-z", "io,stat,10"], timeout=30)
        for line in lines:
            stripped = line.strip()
            if not stripped or not stripped.startswith("|"):
                continue
            if "Interval" in stripped or "Col" in stripped or "Frames" in stripped:
                continue
            if "<>" not in stripped:
                continue
            # Format: | start <> end | frames | bytes |
            inner = stripped.replace("|", " ").strip()
            parts = inner.split()
            # parts: [start, <>, end, frames, bytes]
            if len(parts) >= 4:
                try:
                    start = int(float(parts[0]))
                    frames = int(parts[2])
                    self._timeline_buckets[start] = frames
                except Exception:
                    pass

    def _tshark_wifi(self):
        """WiFi-specific analysis."""
        # SSIDs from beacons
        lines = self._run_tshark(
            ["-T", "fields", "-e", "wlan.bssid", "-e", "wlan.ssid",
             "-e", "wlan_radio.channel",
             "-Y", "wlan.fc.type_subtype==0x08", "-c", "2000"],
            timeout=30)
        for line in lines:
            parts = line.split("\t")
            if len(parts) >= 2:
                bssid = (parts[0] or "").upper().replace(":", ":")
                ssid = parts[1] if len(parts) > 1 else ""
                if bssid and ssid:
                    self.wifi_ssids[bssid] = ssid
                if len(parts) >= 3 and parts[2]:
                    try:
                        self.wifi_channels[int(parts[2])] += 1
                    except Exception:
                        pass

        # Clients (data frames: src != bssid)
        lines = self._run_tshark(
            ["-T", "fields", "-e", "wlan.sa", "-e", "wlan.bssid",
             "-Y", "wlan.fc.type==2", "-c", "5000"],
            timeout=30)
        for line in lines:
            parts = line.split("\t")
            if len(parts) >= 2:
                sa = (parts[0] or "").upper()
                bss = (parts[1] or "").upper()
                if sa and bss and sa != bss:
                    self.wifi_clients.add(sa)

        # Deauths
        lines = self._run_tshark(
            ["-T", "fields", "-e", "frame.number",
             "-Y", "wlan.fc.type_subtype==0x0c"],
            timeout=15)
        self.wifi_deauths = len(lines)

        # EAPOL
        lines = self._run_tshark(
            ["-T", "fields", "-e", "wlan.sa", "-e", "wlan.da",
             "-Y", "eapol"],
            timeout=15)
        self.wifi_eapol = len(lines)
        for line in lines:
            parts = line.split("\t")
            if len(parts) >= 2:
                pair = tuple(sorted([
                    (parts[0] or "").upper(),
                    (parts[1] or "").upper()
                ]))
                self._eapol_pairs[pair] += 1

        # Detect handshakes (4+ EAPOL between same pair)
        for pair, count in self._eapol_pairs.items():
            if count >= 4:
                for mac in pair:
                    if mac in self.wifi_ssids:
                        self.wifi_handshakes.add(mac)

        # Half-handshakes (2-3 EAPOL)
        for pair, count in self._eapol_pairs.items():
            if 2 <= count < 4:
                for mac in pair:
                    if mac in self.wifi_ssids:
                        self.wifi_handshakes.add(mac)

        # Probe requests
        lines = self._run_tshark(
            ["-T", "fields", "-e", "wlan.ssid",
             "-Y", "wlan.fc.type_subtype==0x04", "-c", "2000"],
            timeout=15)
        for line in lines:
            ssid = line.strip()
            if ssid:
                self.wifi_probes[ssid] += 1

        # PMKID count via eapol key data
        lines = self._run_tshark(
            ["-T", "fields", "-e", "eapol.keydes.data",
             "-Y", "eapol.keydes.key_info==0x008a"],
            timeout=15)
        for line in lines:
            data = line.strip().replace(":", "")
            if len(data) >= 44:  # KDE header + 16 byte PMKID
                # Search for PMKID KDE: dd 14 00 0f ac 04
                if "dd" in data.lower() and "000fac04" in data.lower():
                    self.wifi_pmkid += 1

    def _tshark_creds(self):
        """Search for cleartext credentials on known ports."""
        ports_filter = " or ".join(
            f"tcp.port=={p}" for p in CLEARTEXT_PORTS)
        # Try http.request.uri and http.authorization first (fast)
        lines = self._run_tshark(
            ["-T", "fields", "-e", "ip.src", "-e", "tcp.dstport",
             "-e", "http.request.uri", "-e", "http.authorization",
             "-e", "http.cookie",
             "-Y", f"http.request",
             "-c", "200"],
            timeout=20)
        for line in lines:
            if len(self.credentials) >= self._max_creds:
                break
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            src = parts[0] or "?"
            try:
                port = int(parts[1]) if parts[1] else 80
            except Exception:
                port = 80
            for field in parts[2:]:
                if not field:
                    continue
                field_lower = field.lower()
                for kw in CRED_KEYWORDS:
                    kw_str = kw.decode() if isinstance(kw, bytes) else kw
                    if kw_str in field_lower:
                        self.credentials.append({
                            "port": port, "src": src,
                            "snippet": field[:60],
                        })
                        break

        # Also try raw TCP payload on cleartext ports
        if len(self.credentials) < self._max_creds:
            lines = self._run_tshark(
                ["-T", "fields", "-e", "ip.src", "-e", "tcp.dstport",
                 "-e", "data.data",
                 "-Y", f"data.data and ({ports_filter})",
                 "-c", "200"],
                timeout=20)
            for line in lines:
                if len(self.credentials) >= self._max_creds:
                    break
                parts = line.split("\t")
                if len(parts) < 3 or not parts[2]:
                    continue
                src = parts[0] or "?"
                try:
                    port = int(parts[1]) if parts[1] else 0
                except Exception:
                    continue
                hex_payload = parts[2].replace(":", "")
                try:
                    raw = bytes.fromhex(hex_payload[:1000])
                except Exception:
                    continue
                raw_lower = raw.lower()
                for kw in CRED_KEYWORDS:
                    if kw in raw_lower:
                        idx = raw_lower.index(kw)
                        start = max(0, idx - 10)
                        end = min(len(raw), idx + 40)
                        try:
                            text = raw[start:end].decode("utf-8", errors="replace")
                        except Exception:
                            text = raw[start:end].hex()
                        text = text.replace("\r", "").replace("\n", " ").strip()
                        if text:
                            self.credentials.append({
                                "port": port, "src": src,
                                "snippet": text[:60],
                            })
                        break

    # ------------------------------------------------------------------
    # FILE EXTRACTION
    # ------------------------------------------------------------------

    def _tshark_extract_files(self):
        """Extract files from pcap using tshark --export-objects."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = os.path.splitext(self.filename)[0][:20]
        self.extract_dir = os.path.join(EXTRACT_DIR, f"{base_name}_{ts}")

        protocols = ["http", "smb", "tftp", "imf"]
        for proto in protocols:
            out_dir = os.path.join(self.extract_dir, proto)
            os.makedirs(out_dir, exist_ok=True)
            try:
                subprocess.run(
                    ["tshark", "-r", self.filepath, "-n",
                     "--export-objects", f"{proto},{out_dir}"],
                    capture_output=True, timeout=60)
            except Exception:
                pass

            # Collect extracted files
            try:
                for fname in os.listdir(out_dir):
                    fpath = os.path.join(out_dir, fname)
                    if os.path.isfile(fpath):
                        size = os.path.getsize(fpath)
                        if size > 0:
                            self.extracted_files.append({
                                "name": fname[:40],
                                "size": size,
                                "protocol": proto.upper(),
                                "path": fpath,
                            })
            except Exception:
                pass

            # Remove empty dirs
            try:
                if not os.listdir(out_dir):
                    os.rmdir(out_dir)
            except Exception:
                pass

        # Sort by size descending
        self.extracted_files.sort(key=lambda x: x["size"], reverse=True)

        # Cleanup empty base dir
        try:
            if os.path.isdir(self.extract_dir) and not os.listdir(self.extract_dir):
                os.rmdir(self.extract_dir)
                self.extract_dir = None
        except Exception:
            pass

    # ------------------------------------------------------------------
    # SCAPY FALLBACK (slow but works without tshark)
    # ------------------------------------------------------------------

    def _analyze_scapy(self):
        """Fallback: stream-parse with scapy if tshark unavailable."""
        try:
            reader = PcapReader(self.filepath)
        except Exception as e:
            self.error = str(e)[:40]
            return

        try:
            for pkt in reader:
                self._scapy_process(pkt)
                self.total_packets += 1
                if self.total_packets % 500 == 0:
                    self.progress = min(95, self.progress + 1)
        except Exception:
            pass
        finally:
            try:
                reader.close()
            except Exception:
                pass

        for pair, count in self._eapol_pairs.items():
            if count >= 2:
                for mac in pair:
                    if mac in self.wifi_ssids:
                        self.wifi_handshakes.add(mac)

    def _scapy_process(self, pkt):
        pkt_len = len(pkt)
        self.total_bytes += pkt_len

        ts = float(pkt.time) if hasattr(pkt, 'time') else None
        if ts:
            if self.first_ts is None:
                self.first_ts = ts
            self.last_ts = ts
            self._timeline_buckets[int(ts)] += 1

        if pkt.haslayer(Dot11):
            self.is_wifi = True
            dot11 = pkt[Dot11]
            if pkt.haslayer(Dot11Beacon):
                bssid = (dot11.addr2 or "").upper()
                try:
                    essid = pkt[Dot11Elt].info.decode("utf-8", errors="replace")
                    if essid:
                        self.wifi_ssids[bssid] = essid
                except Exception:
                    pass
            if pkt.haslayer(Dot11Deauth):
                self.wifi_deauths += 1
            if pkt.haslayer(EAPOL):
                self.wifi_eapol += 1
                src = (dot11.addr2 or "").upper()
                dst = (dot11.addr1 or "").upper()
                pair = tuple(sorted([src, dst]))
                self._eapol_pairs[pair] += 1
            if dot11.type == 0:
                self.proto_packets["MGMT"] += 1
            elif dot11.type == 2:
                self.proto_packets["DATA"] += 1
            return

        if pkt.haslayer(IP):
            self.is_network = True
            ip = pkt[IP]
            self.src_ips[ip.src] += 1
            self.dst_ips[ip.dst] += 1
            if pkt.haslayer(TCP):
                self.dst_ports[pkt[TCP].dport] += 1
                self.proto_packets["TCP"] += 1
            elif pkt.haslayer(UDP):
                self.dst_ports[pkt[UDP].dport] += 1
                self.proto_packets["UDP"] += 1
            if pkt.haslayer(DNS) and pkt.haslayer(DNSQR):
                dns = pkt[DNS]
                if dns.qr == 0:
                    try:
                        qname = pkt[DNSQR].qname
                        if isinstance(qname, bytes):
                            qname = qname.decode("utf-8", errors="ignore").rstrip(".")
                        if qname:
                            self.dns_queries[qname] += 1
                    except Exception:
                        pass
                self.proto_packets["DNS"] += 1

    # ------------------------------------------------------------------
    # Common properties
    # ------------------------------------------------------------------

    @property
    def duration(self):
        if self.first_ts and self.last_ts:
            return self.last_ts - self.first_ts
        return 0

    @property
    def capture_type(self):
        parts = []
        if self.is_wifi:
            parts.append("WiFi")
        if self.is_network:
            parts.append("Network")
        return " + ".join(parts) or "Unknown"

    def timeline_data(self, buckets=20):
        if not self._timeline_buckets:
            return [0] * buckets
        times = sorted(self._timeline_buckets.keys())
        start, end = times[0], times[-1]
        span = max(end - start, 1)
        bucket_size = span / buckets
        result = [0] * buckets
        for t, count in self._timeline_buckets.items():
            idx = min(int((t - start) / bucket_size), buckets - 1)
            result[idx] += count
        return result

    def to_dict(self):
        return {
            "filename": self.filename,
            "filesize": self.filesize,
            "total_packets": self.total_packets,
            "total_bytes": self.total_bytes,
            "duration_seconds": round(self.duration, 2),
            "capture_type": self.capture_type,
            "protocols": dict(self.proto_packets.most_common(20)),
            "top_src_ips": dict(self.src_ips.most_common(10)),
            "top_dst_ips": dict(self.dst_ips.most_common(10)),
            "top_ports": dict(self.dst_ports.most_common(15)),
            "top_dns": dict(self.dns_queries.most_common(15)),
            "wifi_ssids": dict(self.wifi_ssids),
            "wifi_clients": len(self.wifi_clients),
            "wifi_deauths": self.wifi_deauths,
            "wifi_eapol": self.wifi_eapol,
            "wifi_handshakes": len(self.wifi_handshakes),
            "wifi_pmkid": self.wifi_pmkid,
            "wifi_channels": dict(self.wifi_channels.most_common(14)),
            "wifi_probes": dict(self.wifi_probes.most_common(10)),
            "credentials_found": len(self.credentials),
            "extracted_files": [
                {"name": f["name"], "size": f["size"], "protocol": f["protocol"]}
                for f in self.extracted_files
            ],
            "extract_dir": self.extract_dir,
            "analyzed_at": datetime.now().isoformat(),
        }


# ---------------------------------------------------------------------------
# LCD Drawing helpers (native resolution-adaptive Obsidian Ember)
# ---------------------------------------------------------------------------


def _pa_dims(lcd):
    w = int(getattr(lcd, "width", WIDTH) or WIDTH)
    h = int(getattr(lcd, "height", HEIGHT) or HEIGHT)
    return w, h, rj_theme.load_app_fonts(w, h)


def _draw_header(d, font_sm, title, color=None, w=None, fonts=None, badge="", right_text="", right_pill=""):
    w = w or WIDTH
    fonts = fonts or rj_theme.load_app_fonts(w, HEIGHT)
    return rj_theme.draw_app_header(d, w, title, fonts, badge=badge, right_text=right_text, right_pill=right_pill)


def _draw_footer(d, font_sm, text, w=None, h=None, fonts=None, right_text=""):
    w = w or WIDTH
    h = h or HEIGHT
    fonts = fonts or rj_theme.load_app_fonts(w, h)
    return rj_theme.draw_app_footer(d, w, h, text, fonts, right_text=right_text)


def _draw_bar(d, x, y, w, h, pct, color=None):
    rj_theme.draw_progress_bar(
        d, (x, y, x + w, y + h), pct,
        fill=color or _T["accent"], track=_T["bg_well"], outline=_T["border_subtle"], radius=2,
    )


def _draw_sparkline(d, x, y, w, h, data):
    rj_theme.draw_card(d, (x, y, x + w, y + h), fill=_T["bg_well"], outline=_T["border_subtle"], radius=4)
    if not data or max(data) == 0:
        return
    mx = max(data)
    ix = x + 4
    iw = max(6, w - 8)
    ih = max(6, h - 8)
    step = iw / max(1, len(data))
    bw = max(1, int(step) - 2)
    for i, v in enumerate(data):
        bh = max(0, int(v / mx * ih))
        bx = ix + int(i * step)
        color = _T["ok"] if v > mx * 0.6 else (_T["accent_bright"] if v > mx * 0.2 else _T["accent_dim"])
        if bh > 0:
            d.rectangle((bx, y + h - 4 - bh, min(x + w - 4, bx + bw), y + h - 4), fill=color)


def _fmt_duration(seconds):
    if seconds < 1:
        return f"{seconds*1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m = int(seconds) // 60
    s = int(seconds) % 60
    if m < 60:
        return f"{m}m{s:02d}s"
    h = m // 60
    m = m % 60
    return f"{h}h{m:02d}m"


# ---------------------------------------------------------------------------
# LCD Drawing -- File browser
# ---------------------------------------------------------------------------


def _draw_file_list(lcd, font, font_sm, files, cursor, scroll):
    w, h, fonts = _pa_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)

    hdr_h = _draw_header(d, font_sm, "PCAP ANALYZER", w=w, fonts=fonts, badge=f"{len(files)} FILES", right_text="loot/" if large else "")
    ftr_h = _draw_footer(
        d, font_sm,
        "Up/Dn:Nav   OK:Analyze   K2:Rescan   K3:Exit" if wide else "OK:Go K2:Scn K3:X",
        w=w, h=h, fonts=fonts,
        right_text=f"{cursor + 1}/{len(files)}" if (large and files) else "",
    )

    pad = 8 if large else (4 if wide else 2)
    y0, y1 = hdr_h + pad, h - ftr_h - pad

    if not files:
        rj_theme.draw_status_card(d, w, h, "No PCAP Files Found", sub=f"Place .pcap/.cap files in {LOOT_DIR}", kind="info", foot="K2: Rescan   K3: Exit", fonts=fonts)
    else:
        row_h = 28 if large else (21 if wide else (18 if h >= 200 else 13))
        vis = max(1, (y1 - y0) // row_h)
        start = max(0, min(cursor - vis // 2, max(0, len(files) - vis)))
        for i, f in enumerate(files[start:start + vis]):
            idx = start + i
            y = y0 + i * row_h
            sel = idx == cursor
            if sel:
                rj_theme.draw_card(d, (pad, y, w - pad - 5, y + row_h - 2), fill=_T["select_bg"], outline=_T["border_accent"], radius=4 if wide else 2, accent_left=_T["accent"], accent_width=3 if wide else 2)
            elif i % 2 == 1 and wide:
                d.rectangle((pad, y, w - pad - 5, y + row_h - 2), fill=_T["bg_surface"])
            ty = y + max(1, (row_h - (14 if large else 9)) // 2)
            size = _fmt_size(f["size"])
            sw = rj_theme.measure_text(fonts["badge"] if wide else fonts["sub"], size, d)[2] + (10 if wide else 2)
            rx = w - pad - 10
            if wide:
                rj_theme.draw_pill(d, rx - sw, y + max(1, (row_h - 15) // 2), size, fonts["badge"], bg=_T["bg_elevated"], fg=_T["accent_bright"], outline=None, pad_x=5, pad_y=1)
            else:
                d.text((rx - sw + 4, ty), size, font=fonts["sub"], fill=_T["text_muted"])
            rx -= sw + 8
            tx = pad + (10 if wide else 3)
            lbl = f["rel"] if large else f["name"]
            d.text((tx, ty), rj_theme.truncate_text(d, lbl, max(20, rx - tx), fonts["body_bold"] if (sel and wide) else fonts["sub"]), font=fonts["body_bold"] if (sel and wide) else (fonts["body"] if wide else fonts["sub"]), fill=_T["text_bright"] if sel else _T["text_primary"])
        if len(files) > vis:
            rj_theme.draw_scrollbar(d, w - 4, y0, y1, start, vis, len(files), width=3)

    lcd.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# LCD Drawing -- Analysis progress
# ---------------------------------------------------------------------------


def _draw_progress(lcd, font, font_sm, analysis):
    w, h, fonts = _pa_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)

    hdr_h = _draw_header(d, font_sm, "ANALYZING PCAP", w=w, fonts=fonts, badge=f"{analysis.progress}%", right_pill=_fmt_size(analysis.filesize))
    ftr_h = _draw_footer(d, font_sm, "KEY3 / Esc: Cancel", w=w, h=h, fonts=fonts)

    cw = min(w - 16, 440 if large else (280 if wide else w - 8))
    ch = 136 if large else (88 if wide else 72)
    cx0 = (w - cw) // 2
    cy0 = hdr_h + max(4, (h - hdr_h - ftr_h - ch) // 2)
    rj_theme.draw_card(d, (cx0, cy0, cx0 + cw, cy0 + ch), fill=_T["bg_surface"], outline=_T["border_accent"], radius=8 if wide else 4, accent_left=_T["warn"], accent_width=4 if wide else 2)
    tx = cx0 + (16 if wide else 8)
    mw = cw - (32 if wide else 14)
    d.text((tx, cy0 + (14 if large else 8)), rj_theme.truncate_text(d, analysis.filename, mw, fonts["title"] if wide else fonts["body_bold"]), font=fonts["title"] if wide else fonts["body_bold"], fill=_T["text_bright"])
    d.text((tx, cy0 + (42 if large else (28 if wide else 22))), f"{analysis.total_packets:,} packets  |  {_fmt_size(analysis.filesize)}", font=fonts["body"] if wide else fonts["sub"], fill=_T["ok"])
    bar_y = cy0 + (78 if large else (52 if wide else 42))
    _draw_bar(d, tx, bar_y, mw, 14 if large else (10 if wide else 8), analysis.progress / 100.0, _T["accent"])
    if analysis.error:
        d.text((tx, bar_y + (20 if large else 14)), rj_theme.truncate_text(d, f"ERR: {analysis.error}", mw, fonts["sub"]), font=fonts["sub"], fill=_T["danger"])

    lcd.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# LCD Drawing -- Dashboard views
# ---------------------------------------------------------------------------


def _pa_tab_chrome(d, w, h, fonts, view_key, a, badge=""):
    idx = VIEWS.index(view_key) + 1 if view_key in VIEWS else 1
    hdr_h = _draw_header(
        d, None, VIEW_NAMES.get(view_key, view_key.upper()), w=w, fonts=fonts,
        badge=badge or f"{idx}/{len(VIEWS)}",
        right_text=a.filename if fonts["large"] else "",
        right_pill=a.capture_type if fonts["wide"] else "",
    )
    ftr_h = _draw_footer(
        d, None,
        "L/R:Views   Up/Dn:Scroll   K1:Export JSON   K3:Files" if fonts["wide"] else "L/R:Vw U/D:Sc K1:Exp K3:Bk",
        w=w, h=h, fonts=fonts,
        right_text=f"{idx}/{len(VIEWS)}" if fonts["large"] else "",
    )
    return hdr_h, ftr_h


def _draw_overview(lcd, font, font_sm, a, scroll):
    w, h, fonts = _pa_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)
    hdr_h, ftr_h = _pa_tab_chrome(d, w, h, fonts, "overview", a)
    pad = 10 if large else (4 if wide else 2)
    y0, y1 = hdr_h + pad, h - ftr_h - pad

    if w >= 480:
        kpi_h = 64
        gap = 8
        tw = (w - pad * 2 - gap * 3) // 4
        kpis = [
            ("PACKETS", f"{a.total_packets:,}", _T["ok"]),
            ("DATA VOLUME", _fmt_size(a.total_bytes), _T["accent_bright"]),
            ("DURATION", _fmt_duration(a.duration), _T["warn"]),
            ("CREDS / FILES", f"{len(a.credentials)}c / {len(a.extracted_files)}f", _T["danger"] if a.credentials else _T["text_primary"]),
        ]
        for idx_k, (lbl, val, acc) in enumerate(kpis):
            kx = pad + idx_k * (tw + gap)
            rj_theme.draw_card(d, (kx, y0, kx + tw, y0 + kpi_h), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=6, accent_left=acc, accent_width=4)
            d.text((kx + 12, y0 + 10), lbl, font=fonts["badge"], fill=_T["text_muted"])
            d.text((kx + 12, y0 + 30), rj_theme.truncate_text(d, val, tw - 18, fonts["title"]), font=fonts["title"], fill=acc)

        by0 = y0 + kpi_h + 10
        rj_theme.draw_card(d, (pad, by0, w - pad, y1), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=8, accent_left=_T["accent"], accent_width=4)
        lx, ly = pad + 14, by0 + 12
        lmw = w - pad * 2 - 28
        info_rows = [
            ("File", f"{a.filename}  ({_fmt_size(a.filesize)})", _T["text_bright"]),
            ("Capture Type", a.capture_type, _T["accent_bright"]),
        ]
        if a.duration > 0:
            pps = a.total_packets / a.duration
            bps = a.total_bytes * 8 / a.duration
            info_rows.append(("Throughput", f"{pps:.1f} pkt/s   |   {_fmt_size(bps / 8)}/s", _T["ok"]))
        if a.is_wifi:
            info_rows.append(("WiFi Summary", f"SSIDs:{len(a.wifi_ssids)}  CLI:{len(a.wifi_clients)}  HS:{len(a.wifi_handshakes)}  PMKID:{a.wifi_pmkid}  DTH:{a.wifi_deauths}", _T["warn"]))
        if a.credentials:
            info_rows.append(("Cleartext Creds", f"{len(a.credentials)} potential credential(s) found — view CREDS tab", _T["danger"]))
        for k, v, col in info_rows:
            if ly + 22 > y1:
                break
            d.text((lx, ly), f"{k}:", font=fonts["sub"], fill=_T["text_muted"])
            d.text((lx + 118, ly), rj_theme.truncate_text(d, v, lmw - 118, fonts["body_bold"]), font=fonts["body_bold"], fill=col)
            ly += 26
    else:
        lines = [
            (f"File: {a.filename}", _T["text_bright"]),
            (f"Pkts: {a.total_packets:,}  Vol: {_fmt_size(a.total_bytes)}", _T["ok"]),
            (f"Dur: {_fmt_duration(a.duration)}  [{a.capture_type}]", _T["accent_bright"]),
        ]
        if a.duration > 0:
            pps = a.total_packets / a.duration
            lines.append((f"Rate: {pps:.0f} pkt/s  ({_fmt_size(a.total_bytes / a.duration)}/s)", _T["text_muted"]))
        if a.is_wifi:
            lines.append((f"SSID:{len(a.wifi_ssids)} CLI:{len(a.wifi_clients)} HS:{len(a.wifi_handshakes)} PMK:{a.wifi_pmkid}", _T["warn"]))
        if a.credentials:
            lines.append((f"Creds found: {len(a.credentials)}", _T["danger"]))
        row_h = 16 if wide else 12
        vis = max(1, (y1 - y0) // row_h)
        start = max(0, min(scroll, max(0, len(lines) - vis)))
        y = y0 + 2
        for text, col in lines[start:start + vis]:
            d.text((pad + 3, y), rj_theme.truncate_text(d, text, w - pad * 2 - 6, fonts["body"] if wide else fonts["sub"]), font=fonts["body"] if wide else fonts["sub"], fill=col)
            y += row_h

    lcd.LCD_ShowImage(img, 0, 0)


def _draw_ranked_bars(lcd, a, view_key, items, scroll, bar_color, empty_msg, label_fmt=None):
    w, h, fonts = _pa_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)
    hdr_h, ftr_h = _pa_tab_chrome(d, w, h, fonts, view_key, a)
    pad = 8 if large else (4 if wide else 2)
    y0, y1 = hdr_h + pad, h - ftr_h - pad

    if not items:
        rj_theme.draw_status_card(d, w, h, empty_msg, sub="No records for this category in capture", kind="info", fonts=fonts)
    else:
        mx = max(c for _, c in items) or 1
        row_h = 26 if large else (19 if wide else 12)
        vis = max(1, (y1 - y0) // row_h)
        start = max(0, min(scroll, max(0, len(items) - vis)))
        y = y0
        for idx_r, (key, count) in enumerate(items[start:start + vis]):
            if idx_r % 2 == 0 and wide:
                d.rectangle((pad, y, w - pad - 5, y + row_h - 1), fill=_T["bg_surface"])
            lbl = label_fmt(key) if label_fmt else str(key)
            cnt_s = f"{count:,}" if wide else str(count)
            cw = rj_theme.measure_text(fonts["sub"], cnt_s, d)[2]
            lbl_w = int(w * (0.34 if large else (0.40 if wide else 0.48)))
            d.text((pad + (6 if wide else 2), y + (4 if large else 1)), rj_theme.truncate_text(d, lbl, lbl_w - 8, fonts["mono"] if wide else fonts["sub"]), font=fonts["mono"] if wide else fonts["sub"], fill=_T["text_primary"])
            bx0 = pad + lbl_w
            bx1 = w - pad - cw - (16 if wide else 8)
            if bx1 > bx0 + 16:
                _draw_bar(d, bx0, y + (6 if large else 3), bx1 - bx0, 12 if large else (9 if wide else 6), count / mx, bar_color)
            d.text((w - pad - cw - 8, y + (4 if large else 1)), cnt_s, font=fonts["sub"], fill=_T["accent_bright"] if idx_r == 0 and start == 0 else _T["text_muted"])
            y += row_h
        if len(items) > vis:
            rj_theme.draw_scrollbar(d, w - 4, y0, y1, start, vis, len(items), width=3)

    lcd.LCD_ShowImage(img, 0, 0)


def _draw_protocols(lcd, font, font_sm, a, scroll):
    _draw_ranked_bars(lcd, a, "protocols", a.proto_packets.most_common(20), scroll, _T["accent"], "No Protocol Data")


def _draw_talkers(lcd, font, font_sm, a, scroll):
    all_ips = Counter()
    for ip, c in a.src_ips.items():
        all_ips[ip] += c
    for ip, c in a.dst_ips.items():
        all_ips[ip] += c
    _draw_ranked_bars(lcd, a, "talkers", all_ips.most_common(20), scroll, _T["ok"], "No IP Traffic")


def _draw_wifi(lcd, font, font_sm, a, scroll):
    w, h, fonts = _pa_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)
    hdr_h, ftr_h = _pa_tab_chrome(d, w, h, fonts, "wifi", a)
    pad = 8 if large else (4 if wide else 2)
    y0, y1 = hdr_h + pad, h - ftr_h - pad

    if not a.is_wifi:
        rj_theme.draw_status_card(d, w, h, "No WiFi Frames", sub="This capture does not contain 802.11 traffic", kind="info", fonts=fonts)
        lcd.LCD_ShowImage(img, 0, 0)
        return

    y = y0
    if w >= 480:
        kpi_h = 56
        gap = 8
        tw = (w - pad * 2 - gap * 3) // 4
        for idx_k, (lbl, val, acc) in enumerate([
            ("SSIDs / CLIENTS", f"{len(a.wifi_ssids)} / {len(a.wifi_clients)}", _T["ok"]),
            ("HANDSHAKES", str(len(a.wifi_handshakes)), _T["ok"] if a.wifi_handshakes else _T["text_muted"]),
            ("PMKID / EAPOL", f"{a.wifi_pmkid} / {a.wifi_eapol}", _T["accent_bright"]),
            ("DEAUTHS / PROBES", f"{a.wifi_deauths} / {sum(a.wifi_probes.values())}", _T["danger"] if a.wifi_deauths else _T["warn"]),
        ]):
            kx = pad + idx_k * (tw + gap)
            rj_theme.draw_card(d, (kx, y, kx + tw, y + kpi_h), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=6, accent_left=acc, accent_width=4)
            d.text((kx + 10, y + 8), lbl, font=fonts["badge"], fill=_T["text_muted"])
            d.text((kx + 10, y + 26), val, font=fonts["title"], fill=acc)
        y += kpi_h + 10
        ssids = list(a.wifi_ssids.items())
        row_h = 24
        vis = max(1, (y1 - y) // row_h)
        start = max(0, min(scroll, max(0, len(ssids) - vis)))
        for i, (bssid, essid) in enumerate(ssids[start:start + vis]):
            if i % 2 == 0:
                d.rectangle((pad, y, w - pad - 5, y + row_h - 1), fill=_T["bg_surface"])
            has_hs = bssid in a.wifi_handshakes
            tx = pad + 8
            if has_hs:
                pw, _ = rj_theme.draw_pill(d, tx, y + 2, "HS", fonts["badge"], bg=_T["ok_bg"], fg=_T["ok"], outline=None, pad_x=4, pad_y=1)
                tx += pw + 8
            bw = rj_theme.measure_text(fonts["mono"], bssid, d)[2]
            d.text((w - pad - bw - 10, y + 4), bssid, font=fonts["mono"], fill=_T["text_muted"])
            d.text((tx, y + 4), rj_theme.truncate_text(d, essid, w - pad - bw - tx - 20, fonts["body_bold"]), font=fonts["body_bold"], fill=_T["ok"] if has_hs else _T["text_bright"])
            y += row_h
        if len(ssids) > vis:
            rj_theme.draw_scrollbar(d, w - 4, y0 + kpi_h + 10, y1, start, vis, len(ssids), width=3)
    else:
        lines = [
            (f"SSIDs:{len(a.wifi_ssids)} CLI:{len(a.wifi_clients)} EAPOL:{a.wifi_eapol}", _T["ok"]),
            (f"HS:{len(a.wifi_handshakes)} PMKID:{a.wifi_pmkid} DTH:{a.wifi_deauths}", _T["warn"]),
        ]
        for bssid, essid in list(a.wifi_ssids.items())[:12]:
            mark = "! " if bssid in a.wifi_handshakes else "  "
            lines.append((f"{mark}{essid} ({bssid[-8:]})", _T["ok"] if bssid in a.wifi_handshakes else _T["text_primary"]))
        row_h = 16 if wide else 12
        vis = max(1, (y1 - y0) // row_h)
        start = max(0, min(scroll, max(0, len(lines) - vis)))
        for txt, col in lines[start:start + vis]:
            d.text((pad + 3, y), rj_theme.truncate_text(d, txt, w - pad * 2 - 6, fonts["sub"]), font=fonts["sub"], fill=col)
            y += row_h

    lcd.LCD_ShowImage(img, 0, 0)


def _draw_ports(lcd, font, font_sm, a, scroll):
    PORT_NAMES = {
        21: "FTP", 22: "SSH", 23: "TELNET", 25: "SMTP", 53: "DNS",
        67: "DHCP", 80: "HTTP", 110: "POP3", 143: "IMAP", 443: "HTTPS",
        445: "SMB", 993: "IMAPS", 995: "POP3S", 3389: "RDP",
        5353: "mDNS", 8080: "HTTP-P", 8443: "HTTPS-A",
    }
    _draw_ranked_bars(
        lcd, a, "ports", a.dst_ports.most_common(20), scroll, _T["warn"], "No Port Data",
        label_fmt=lambda p: f"{p}/{PORT_NAMES[p]}" if p in PORT_NAMES else str(p),
    )


def _draw_dns(lcd, font, font_sm, a, scroll):
    w, h, fonts = _pa_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)
    hdr_h, ftr_h = _pa_tab_chrome(d, w, h, fonts, "dns", a)
    pad = 8 if large else (4 if wide else 2)
    y0, y1 = hdr_h + pad, h - ftr_h - pad

    top = a.dns_queries.most_common(30)
    if not top:
        rj_theme.draw_status_card(d, w, h, "No DNS Queries", sub="No DNS requests found in capture", kind="info", fonts=fonts)
    else:
        row_h = 24 if large else (18 if wide else 12)
        vis = max(1, (y1 - y0) // row_h)
        start = max(0, min(scroll, max(0, len(top) - vis)))
        y = y0
        for idx_r, (domain, count) in enumerate(top[start:start + vis]):
            if idx_r % 2 == 0 and wide:
                d.rectangle((pad, y, w - pad - 5, y + row_h - 1), fill=_T["bg_surface"])
            cnt_s = str(count)
            cw = rj_theme.measure_text(fonts["badge"] if wide else fonts["sub"], cnt_s, d)[2] + (10 if wide else 4)
            if wide:
                rj_theme.draw_pill(d, w - pad - cw - 8, y + max(1, (row_h - 15) // 2), cnt_s, fonts["badge"], bg=_T["bg_elevated"], fg=_T["accent_bright"], outline=None, pad_x=5, pad_y=1)
            else:
                d.text((w - pad - cw - 3, y + 1), cnt_s, font=fonts["sub"], fill=_T["accent_bright"])
            d.text((pad + (8 if wide else 2), y + (4 if large else 1)), rj_theme.truncate_text(d, domain, w - pad * 2 - cw - 20, fonts["mono"] if wide else fonts["sub"]), font=fonts["mono"] if wide else fonts["sub"], fill=_T["text_primary"])
            y += row_h
        if len(top) > vis:
            rj_theme.draw_scrollbar(d, w - 4, y0, y1, start, vis, len(top), width=3)

    lcd.LCD_ShowImage(img, 0, 0)


def _draw_timeline(lcd, font, font_sm, a, scroll):
    w, h, fonts = _pa_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)
    hdr_h, ftr_h = _pa_tab_chrome(d, w, h, fonts, "timeline", a)
    pad = 10 if large else (4 if wide else 2)
    y0, y1 = hdr_h + pad, h - ftr_h - pad

    buckets = 48 if large else (32 if wide else 24)
    data = a.timeline_data(buckets=buckets)
    has_data = bool(data and max(data) > 0)
    peak = max(data) if has_data else 0
    avg = (sum(data) / len(data)) if has_data else 0.0

    stat_h = 56 if large else (22 if wide else 18)
    chart_h = max(34, (y1 - y0) - stat_h - (10 if large else 6))
    _draw_sparkline(d, pad, y0, w - pad * 2, chart_h, data)

    sy = y0 + chart_h + (8 if large else 4)
    if w >= 480:
        gap = 8
        tw = (w - pad * 2 - gap * 2) // 3
        for idx_k, (lbl, val, acc) in enumerate([
            ("DURATION", _fmt_duration(a.duration), _T["accent_bright"]),
            ("PEAK BUCKET", f"{peak:,} pkts", _T["ok"]),
            ("AVG BUCKET", f"{avg:.1f} pkts", _T["warn"]),
        ]):
            kx = pad + idx_k * (tw + gap)
            rj_theme.draw_card(d, (kx, sy, kx + tw, sy + stat_h), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=6, accent_left=acc, accent_width=4)
            d.text((kx + 12, sy + 8), lbl, font=fonts["badge"], fill=_T["text_muted"])
            d.text((kx + 12, sy + 26), val, font=fonts["title"], fill=acc)
    else:
        txt = f"Dur:{_fmt_duration(a.duration)}  Peak:{peak}  Avg:{avg:.1f}" if has_data else "No timeline data"
        d.text((pad + 3, sy + 2), rj_theme.truncate_text(d, txt, w - pad * 2 - 6, fonts["sub"]), font=fonts["sub"], fill=_T["accent_bright"] if has_data else _T["text_dim"])

    lcd.LCD_ShowImage(img, 0, 0)


def _draw_creds(lcd, font, font_sm, a, scroll):
    w, h, fonts = _pa_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)
    count = len(a.credentials)
    hdr_h, ftr_h = _pa_tab_chrome(d, w, h, fonts, "creds", a, badge=f"{count} FOUND")
    pad = 8 if large else (4 if wide else 2)
    y0, y1 = hdr_h + pad, h - ftr_h - pad

    if not a.credentials:
        rj_theme.draw_status_card(d, w, h, "No Cleartext Creds", sub="No credential keywords matched on cleartext ports", kind="ok", fonts=fonts)
    else:
        card_h = 46 if large else (32 if wide else 22)
        vis = max(1, (y1 - y0) // card_h)
        start = max(0, min(scroll, max(0, count - vis)))
        y = y0
        for cred in a.credentials[start:start + vis]:
            rj_theme.draw_card(d, (pad, y, w - pad - 5, y + card_h - 3), fill=_T["bg_surface"], outline=_T["danger"], radius=5 if wide else 2, accent_left=_T["danger"], accent_width=3 if wide else 2)
            tx = pad + (10 if wide else 4)
            mw = w - pad * 2 - 18
            if wide:
                pw, _ = rj_theme.draw_pill(d, tx, y + (5 if large else 2), f":{cred['port']}", fonts["badge"], bg=_T["danger_bg"], fg=_T["danger"], outline=None, pad_x=5, pad_y=1)
                d.text((tx + pw + 8, y + (5 if large else 2)), cred["src"], font=fonts["body_bold"], fill=_T["text_bright"])
                d.text((tx, y + (24 if large else 16)), rj_theme.truncate_text(d, cred["snippet"], mw, fonts["mono"]), font=fonts["mono"], fill=_T["warn"])
            else:
                d.text((tx, y + 1), rj_theme.truncate_text(d, f":{cred['port']} {cred['src']}", mw, fonts["sub"]), font=fonts["sub"], fill=_T["danger"])
                d.text((tx, y + 10), rj_theme.truncate_text(d, cred["snippet"], mw, fonts["sub"]), font=fonts["sub"], fill=_T["warn"])
            y += card_h
        if count > vis:
            rj_theme.draw_scrollbar(d, w - 4, y0, y1, start, vis, count, width=3)

    lcd.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# LCD Drawing -- Files view
# ---------------------------------------------------------------------------


def _draw_files(lcd, font, font_sm, a, scroll):
    w, h, fonts = _pa_dims(lcd)
    wide, large = fonts["wide"], fonts["large"]
    img = Image.new("RGB", (w, h), _T["bg_base"])
    d = ImageDraw.Draw(img)
    count = len(a.extracted_files)
    hdr_h, ftr_h = _pa_tab_chrome(d, w, h, fonts, "files", a, badge=f"{count} FILES")
    pad = 8 if large else (4 if wide else 2)
    y0, y1 = hdr_h + pad, h - ftr_h - pad

    if not a.extracted_files:
        rj_theme.draw_status_card(d, w, h, "No Extracted Files", sub="No HTTP/SMB/TFTP/IMF objects exported from capture", kind="info", fonts=fonts)
    else:
        total_size = sum(f["size"] for f in a.extracted_files)
        protos = ",".join(sorted(set(f["protocol"] for f in a.extracted_files)))
        d.text((pad + 4, y0), rj_theme.truncate_text(d, f"{count} objects  |  {_fmt_size(total_size)}  |  {protos}", w - pad * 2 - 8, fonts["sub"]), font=fonts["sub"], fill=_T["accent_bright"])
        y = y0 + (18 if large else 13)
        row_h = 24 if large else (18 if wide else 12)
        vis = max(1, (y1 - y) // row_h)
        start = max(0, min(scroll, max(0, count - vis)))
        for idx_r, f in enumerate(a.extracted_files[start:start + vis]):
            if idx_r % 2 == 0 and wide:
                d.rectangle((pad, y, w - pad - 5, y + row_h - 1), fill=_T["bg_surface"])
            ext = os.path.splitext(f["name"])[1].lower()
            if ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico", ".svg"):
                color = _T["ok"]
            elif ext in (".html", ".htm", ".css", ".js", ".json", ".xml"):
                color = _T["accent_bright"]
            elif ext in (".exe", ".dll", ".bin", ".msi", ".elf"):
                color = _T["danger"]
            elif ext in (".zip", ".tar", ".gz", ".7z", ".rar"):
                color = _T["warn"]
            else:
                color = _T["text_primary"]
            rx = w - pad - 10
            sz_s = _fmt_size(f["size"])
            sw = rj_theme.measure_text(fonts["sub"], sz_s, d)[2]
            d.text((rx - sw, y + (4 if large else 1)), sz_s, font=fonts["sub"], fill=_T["text_muted"])
            rx -= sw + 8
            tx = pad + (6 if wide else 2)
            if wide:
                pw, _ = rj_theme.draw_pill(d, tx, y + max(1, (row_h - 15) // 2), f["protocol"][:4], fonts["badge"], bg=_T["bg_elevated"], fg=_T["accent_bright"], outline=None, pad_x=4, pad_y=1)
                tx += pw + 8
            d.text((tx, y + (4 if large else 1)), rj_theme.truncate_text(d, f["name"], max(20, rx - tx - 2), fonts["body"] if wide else fonts["sub"]), font=fonts["body"] if wide else fonts["sub"], fill=color)
            y += row_h
        if count > vis:
            rj_theme.draw_scrollbar(d, w - 4, y0 + (18 if large else 13), y1, start, vis, count, width=3)

    lcd.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _export_analysis(analysis):
    os.makedirs(EXPORT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = os.path.splitext(analysis.filename)[0][:20]
    path = os.path.join(EXPORT_DIR, f"analysis_{name}_{ts}.json")
    with open(path, "w") as f:
        json.dump(analysis.to_dict(), f, indent=2)
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    GPIO.setmode(GPIO.BCM)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    LCD_Config.GPIO_Init()
    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    lcd.LCD_Clear()
    font = scaled_font(10)
    font_sm = scaled_font(8)

    if not SCAPY_OK:
        w, h, fonts = _pa_dims(lcd)
        img = Image.new("RGB", (w, h), _T["bg_base"])
        d = ImageDraw.Draw(img)
        rj_theme.draw_app_header(d, w, "PCAP ANALYZER", fonts, badge="ERROR")
        rj_theme.draw_status_card(d, w, h, "Scapy Not Found", sub="Install python3-scapy", kind="err", fonts=fonts)
        lcd.LCD_ShowImage(img, 0, 0)
        time.sleep(3)
        GPIO.cleanup()
        return 1

    # --- File browser ---
    screen = "browser"  # browser | analyzing | dashboard
    files = _find_pcap_files()
    cursor = 0
    scroll = 0
    analysis = None
    view_idx = 0
    dash_scroll = 0

    try:
        while True:
            btn = get_button(PINS, GPIO)

            # === FILE BROWSER ===
            if screen == "browser":
                if btn == "KEY3":
                    break
                elif btn == "KEY2":
                    files = _find_pcap_files()
                    cursor = 0
                    scroll = 0
                    time.sleep(0.3)
                elif btn == "UP" and files:
                    cursor = max(0, cursor - 1)
                    if cursor < scroll:
                        scroll = cursor
                    time.sleep(0.15)
                elif btn == "DOWN" and files:
                    cursor = min(len(files) - 1, cursor + 1)
                    if cursor >= scroll + 7:
                        scroll = cursor - 6
                    time.sleep(0.15)
                elif btn == "OK" and files:
                    selected = files[cursor]
                    analysis = PcapAnalysis(selected["path"])
                    screen = "analyzing"
                    threading.Thread(target=analysis.analyze, daemon=True).start()
                    time.sleep(0.3)

                _draw_file_list(lcd, font, font_sm, files, cursor, scroll)

            # === ANALYZING ===
            elif screen == "analyzing":
                if btn == "KEY3":
                    screen = "browser"
                    time.sleep(0.3)
                elif analysis and analysis.done:
                    screen = "dashboard"
                    view_idx = 0
                    dash_scroll = 0
                    # Auto-select WiFi view if WiFi capture
                    if analysis.is_wifi and not analysis.is_network:
                        view_idx = VIEWS.index("wifi")
                    time.sleep(0.3)

                if analysis:
                    _draw_progress(lcd, font, font_sm, analysis)

            # === DASHBOARD ===
            elif screen == "dashboard":
                if btn == "KEY3":
                    screen = "browser"
                    time.sleep(0.3)
                elif btn == "RIGHT":
                    view_idx = (view_idx + 1) % len(VIEWS)
                    dash_scroll = 0
                    time.sleep(0.2)
                elif btn == "LEFT":
                    view_idx = (view_idx - 1) % len(VIEWS)
                    dash_scroll = 0
                    time.sleep(0.2)
                elif btn == "UP":
                    dash_scroll = max(0, dash_scroll - 1)
                    time.sleep(0.15)
                elif btn == "DOWN":
                    dash_scroll += 1
                    time.sleep(0.15)
                elif btn == "KEY1":
                    # Export
                    w, h, fonts = _pa_dims(lcd)
                    try:
                        path = _export_analysis(analysis)
                        img = Image.new("RGB", (w, h), _T["bg_base"])
                        d = ImageDraw.Draw(img)
                        rj_theme.draw_app_header(d, w, "PCAP EXPORT", fonts, badge="SAVED")
                        rj_theme.draw_status_card(d, w, h, "Analysis Exported!", sub=os.path.basename(path), kind="ok", fonts=fonts)
                        lcd.LCD_ShowImage(img, 0, 0)
                        time.sleep(2)
                    except Exception as e:
                        img = Image.new("RGB", (w, h), _T["bg_base"])
                        d = ImageDraw.Draw(img)
                        rj_theme.draw_app_header(d, w, "PCAP EXPORT", fonts, badge="ERROR")
                        rj_theme.draw_status_card(d, w, h, "Export Failed", sub=str(e)[:40], kind="err", fonts=fonts)
                        lcd.LCD_ShowImage(img, 0, 0)
                        time.sleep(2)

                current_view = VIEWS[view_idx]
                draw_funcs = {
                    "overview": _draw_overview,
                    "protocols": _draw_protocols,
                    "talkers": _draw_talkers,
                    "wifi": _draw_wifi,
                    "ports": _draw_ports,
                    "dns": _draw_dns,
                    "timeline": _draw_timeline,
                    "creds": _draw_creds,
                    "files": _draw_files,
                }
                draw_funcs[current_view](lcd, font, font_sm, analysis, dash_scroll)

            time.sleep(0.05)

    finally:
        try:
            lcd.LCD_Clear()
        except Exception:
            pass
        GPIO.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
