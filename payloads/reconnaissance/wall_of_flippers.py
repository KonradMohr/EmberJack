#!/usr/bin/env python3
"""
RaspyJack payload - Wall of Flippers
------------------------------------------------
A RaspyJack-native version of Wall of Flippers
- Live BLE scanning
- Threat summaries, nearby WoF detection, history persistence, settings
- Credits: https://github.com/K3YOMI/Wall-of-Flippers
- Device Port Author: @h0ss310s https://github.com/Hosseios
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Allow imports from RaspyJack root
sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

try:
    import RPi.GPIO as GPIO
    import LCD_1in44
    import rj_theme
    from PIL import Image, ImageDraw, ImageFont
    from payloads._display_helper import ScaledDraw, scaled_font

    LCD_AVAILABLE = True
    _T = rj_theme.EMBER_THEME
except Exception:
    GPIO = None
    LCD_1in44 = None
    rj_theme = None
    Image = None
    ImageDraw = None
    ImageFont = None
    LCD_AVAILABLE = False
    _T = {}

from payloads._input_helper import get_button
from payloads._display_helper import scaled_font
from payloads._iface_helper import select_bt_interface

try:
    from bleak import BleakScanner
except Exception:
    BleakScanner = None

try:
    from bluepy.btle import Scanner as BluepyScanner
except Exception:
    BluepyScanner = None


PINS = {
    "UP": 6,
    "DOWN": 19,
    "LEFT": 5,
    "RIGHT": 26,
    "OK": 13,
    "KEY1": 21,
    "KEY2": 20,
    "KEY3": 16,
}

WIDTH = LCD_1in44.LCD_WIDTH if LCD_1in44 else 128
HEIGHT = LCD_1in44.LCD_HEIGHT if LCD_1in44 else 128
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
BASE_DIR = os.path.abspath(os.path.join(__file__, "..", "..", ".."))
LOOT_DIR = os.path.join(BASE_DIR, "loot")
WOF_LOOT_DIR = os.path.join(LOOT_DIR, "WOF")
HISTORY_PATH = os.path.join(WOF_LOOT_DIR, "history.json")

FLIPPER_TYPES = {
    "00003081-0000-1000-8000-00805f9b34fb": "B",
    "00003082-0000-1000-8000-00805f9b34fb": "W",
    "00003083-0000-1000-8000-00805f9b34fb": "T",
}
FLIPPER_OUIS = ("80:e1:26", "80:e1:27", "0c:fa:22")

FORBIDDEN_PACKETS = [
    {"PCK": "00001812-0000-1000-8000-00805f9b34fb", "TYPE": "BLE_HUMAN_INTERFACE_DEVICE"},
    {"PCK": "4c000719010_2055_______________", "TYPE": "BLE_APPLE_DEVICE_POPUP_CLOSE"},
    {"PCK": "4c000f05c00____________________", "TYPE": "BLE_APPLE_ACTION_MODAL_LONG"},
    {"PCK": "4c00071907_____________________", "TYPE": "BLE_APPLE_DEVICE_CONNECT"},
    {"PCK": "4c0004042a0000000f05c1__604c950", "TYPE": "BLE_APPLE_DEVICE_SETUP"},
    {"PCK": "2cfe___________________________", "TYPE": "BLE_ANDROID_DEVICE_CONNECT"},
    {"PCK": "750042098102141503210109____01_", "TYPE": "BLE_SAMSUNG_BUDS_POPUP_LONG"},
    {"PCK": "7500010002000101ff000043_______", "TYPE": "BLE_SAMSUNG_WATCH_PAIR_LONG"},
    {"PCK": "0600030080_____________________", "TYPE": "BLE_WINDOWS_SWIFT_PAIR_SHORT"},
    {"PCK": "ff006db643ce97fe427c___________", "TYPE": "BLE_LOVE_TOYS_SHORT_DISTANCE"},
]

WOF_ADVERTISER_RAW = "2c2222222222"
MIN_BYTE_LENGTH = 3
MAX_BYTE_LENGTH = 450
MAX_FLIPPERS_RATELIMITED = 3
RATELIMIT_SECONDS = 5
FLIPPER_UID_PREFIX = "0000308"
FLIPPER_UID_SUFFIX = "0000-1000-8000-00805f9b34fb"


def _available_hci_adapters() -> List[int]:
    if os.name != "posix":
        return [0]
    adapters = []
    try:
        for item in os.listdir("/sys/class/bluetooth/"):
            if item.startswith("hci"):
                try:
                    adapters.append(int(item.replace("hci", "")))
                except Exception:
                    continue
    except Exception:
        pass
    return sorted(adapters) if adapters else [0]


def _match_packet(packet: str, pattern: str) -> bool:
    """
    Match behavior aligned with original WoF:
    - Compare only zipped chars (pattern may be longer)
    - "_" in pattern acts as wildcard
    - Require packet to include at least as many non-wildcard chars as pattern
    """
    if not all((p1 == p2 or p2 == "_") for p1, p2 in zip(packet, pattern)):
        return False
    needed = len(pattern.replace("_", ""))
    found = sum(ch != "_" for ch in packet)
    return found >= needed


def _short(s: str, n: int) -> str:
    s = str(s or "")
    if len(s) <= n:
        return s
    if n <= 1:
        return s[:n]
    return s[: n - 1] + "."


def _mac_tail(mac: str) -> str:
    mac = str(mac or "").upper()
    return mac[-8:] if len(mac) >= 8 else mac


def _age_text(ts: int) -> str:
    diff = max(0, int(time.time()) - int(ts))
    m, s = divmod(diff, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        if h >= 24:
            d, h = divmod(h, 24)
            return f"{d}d{h}h"
        return f"{h}h{m}m"
    return f"{m}m{s}s"


@dataclass
class Settings:
    scan_interval: float = 1.0
    offline_timeout: int = 25
    sort_mode: str = "last_seen"  # last_seen | rssi
    badge_mode: bool = False
    hci_adapter: int = 0


@dataclass
class DeviceRecord:
    name: str
    mac: str
    rssi: int
    dtype: str
    uid: str
    detection: str
    first_seen: int
    last_seen: int


@dataclass
class WoFState:
    devices: Dict[str, DeviceRecord] = field(default_factory=dict)
    packet_total: int = 0
    forbidden_total: int = 0
    forbidden_by_type: Dict[str, int] = field(default_factory=dict)
    nearby_wof: Dict[str, dict] = field(default_factory=dict)
    all_packets: int = 0
    live_count: int = 0
    offline_count: int = 0
    is_ratelimited: bool = False
    last_ratelimit_until: int = 0
    suspicious_dropped: int = 0
    scan_enabled: bool = True
    scanner_backend: str = "init"
    scanner_health: str = "starting"
    last_error: str = ""
    new_events: deque = field(default_factory=lambda: deque(maxlen=40))
    threat_events: deque = field(default_factory=lambda: deque(maxlen=80))
    history_sessions: List[dict] = field(default_factory=list)
    running_since: int = field(default_factory=lambda: int(time.time()))
    selected_live: int = 0
    selected_threat: int = 0
    selected_history: int = 0
    selected_setting: int = 0
    selected_nearby: int = 0
    detail_mac: str = ""
    current_screen: str = "dashboard"

    def live_devices(self) -> List[DeviceRecord]:
        now = int(time.time())
        return [d for d in self.devices.values() if (now - d.last_seen) <= settings.offline_timeout]

    def offline_devices(self) -> List[DeviceRecord]:
        now = int(time.time())
        return [d for d in self.devices.values() if (now - d.last_seen) > settings.offline_timeout]


settings = Settings()
state = WoFState()
running = True
state_lock = threading.Lock()


class ScannerWorker:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.new_flipper_times: deque = deque(maxlen=20)
        self.btctl_proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._btctl_stop_scan()
        if self.thread:
            self.thread.join(timeout=2.0)

    def _run(self) -> None:
        # Prefer bleak (modern, maintained) over bluepy (abandoned, broken on BlueZ 5.80+)
        if BleakScanner is not None:
            with state_lock:
                state.scanner_backend = "bleak"
                state.scanner_health = "running"
            try:
                asyncio.run(self._run_bleak())
                return
            except Exception as exc:
                with state_lock:
                    state.last_error = f"Bleak failed: {exc}"
                    state.scanner_health = "degraded"
        # Fallback to bluepy if bleak unavailable
        if os.name == "posix" and BluepyScanner is not None:
            with state_lock:
                state.scanner_backend = "bluepy"
                state.scanner_health = "running"
            try:
                self._run_bluepy()
                return
            except Exception as exc:
                with state_lock:
                    state.last_error = f"Bluepy failed: {exc}"
                    state.scanner_health = "degraded"
        # Last resort: bluetoothctl
        with state_lock:
            state.scanner_backend = "bluetoothctl"
            state.scanner_health = "running"
        self._run_bluetoothctl()

    async def _run_bleak(self) -> None:
        async def on_detect(device, adv_data) -> None:
            if self.stop_event.is_set():
                return
            if not state.scan_enabled:
                return
            packets = []
            uuids = list(adv_data.service_uuids or [])
            for uid in uuids:
                packets.append(str(uid).lower())
            for company_id, mbytes in (adv_data.manufacturer_data or {}).items():
                try:

                    packets.append(f"{int(company_id):04x}{bytes(mbytes).hex()}".lower())
                except Exception:
                    pass
            for suid, sbytes in (adv_data.service_data or {}).items():
                try:
                    packets.append(str(suid).lower())
                    packets.append(bytes(sbytes).hex().lower())
                except Exception:
                    pass
            self._consume_observation(
                mac=str(device.address or "").lower(),
                name=str(device.name or getattr(adv_data, "local_name", None) or "Unknown"),
                rssi=int(getattr(adv_data, "rssi", None) or getattr(device, "rssi", -100)),
                packets=packets,
                from_backend="bleak",
            )

        while not self.stop_event.is_set():
            try:
                scanner = BleakScanner(detection_callback=on_detect)
                async with scanner:
                    t_end = time.monotonic() + max(0.2, settings.scan_interval)
                    while time.monotonic() < t_end and not self.stop_event.is_set():
                        await asyncio.sleep(0.05)
            except Exception as exc:
                with state_lock:
                    state.last_error = f"scan loop: {exc}"
                    state.scanner_health = "retrying"
                await asyncio.sleep(1.0)

    def _run_bluepy(self) -> None:
        adapters = _available_hci_adapters()
        if settings.hci_adapter not in adapters:
            settings.hci_adapter = adapters[0]
            with state_lock:
                state.last_error = f"bluepy: switched to hci{settings.hci_adapter}"
        while not self.stop_event.is_set():
            if not state.scan_enabled:
                time.sleep(0.1)
                continue
            try:
                scanner = BluepyScanner(int(settings.hci_adapter))
                scan_seconds = max(1.0, float(settings.scan_interval))
                devices = scanner.scan(scan_seconds)
                with state_lock:
                    state.scanner_health = "running"
                    if state.last_error.startswith("bluepy:"):
                        state.last_error = ""
                for dev in devices:
                    if self.stop_event.is_set():
                        break
                    packets = []
                    name = "Unknown"
                    for ad_type, desc, value in dev.getScanData():
                        sval = str(value or "").strip()
                        if sval:
                            packets.append(sval.lower())
                        if desc in ("Complete Local Name", "Short Local Name") and sval:
                            name = sval
                    self._consume_observation(
                        mac=str(getattr(dev, "addr", "")).lower(),
                        name=name,
                        rssi=int(getattr(dev, "rssi", -100)),
                        packets=packets,
                        from_backend="bluepy",
                    )
            except Exception as exc:
                err = str(exc)
                # If selected adapter is bad/busy, try another available adapter.
                if "MgmtError" in err or "Failed to execute mgmt cmd" in err:
                    adapters = _available_hci_adapters()
                    if len(adapters) > 1:
                        cur = adapters.index(settings.hci_adapter) if settings.hci_adapter in adapters else 0
                        settings.hci_adapter = adapters[(cur + 1) % len(adapters)]
                        err = f"{err}; retrying hci{settings.hci_adapter}"
                with state_lock:
                    state.last_error = _short(f"bluepy: {err}", 120)
                    state.scanner_health = "retrying"
                time.sleep(1.0)

    def _run_bluetoothctl(self) -> None:
        while not self.stop_event.is_set():
            try:
                if state.scan_enabled:
                    if self.btctl_proc is None:
                        self._btctl_start_scan()
                    elif self.btctl_proc.poll() is not None:
                        # Scanner process exited unexpectedly; recreate it.
                        self.btctl_proc = None
                        self._btctl_start_scan()
                    text = self._btctl_read_devices()
                    if text:
                        with state_lock:
                            if state.scanner_health != "running":
                                state.scanner_health = "running"
                            # Clear stale soft warning when reads recover.
                            if state.last_error.startswith("btctl timeout"):
                                state.last_error = ""
                    for line in text.splitlines():
                        m = re.match(r"^Device\s+([0-9A-Fa-f:]{17})\s*(.*)$", line.strip())
                        if not m:
                            continue
                        mac = m.group(1).lower()
                        name = m.group(2).strip() or "Unknown"
                        self._consume_observation(mac=mac, name=name, rssi=-99, packets=[], from_backend="btctl")
                else:
                    # If user paused scanning, stop discovery immediately.
                    if self.btctl_proc is not None:
                        self._btctl_stop_scan()
            except Exception as exc:
                with state_lock:
                    state.last_error = f"btctl: {exc}"
                    state.scanner_health = "retrying"
                self._btctl_stop_scan()
            time.sleep(max(0.5, settings.scan_interval))
        self._btctl_stop_scan()

    def _btctl_start_scan(self) -> None:
        # Best effort: ensure adapter is up.
        try:
            subprocess.run(
                ["hciconfig", f"hci{int(settings.hci_adapter)}", "up"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=4,
            )
        except Exception:
            pass
        self.btctl_proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if self.btctl_proc.stdin:
            self.btctl_proc.stdin.write("scan on\n")
            self.btctl_proc.stdin.flush()
        with state_lock:
            state.scanner_health = "running"

    def _btctl_stop_scan(self) -> None:
        if self.btctl_proc is None:
            return
        try:
            if self.btctl_proc.stdin:
                self.btctl_proc.stdin.write("scan off\n")
                self.btctl_proc.stdin.flush()
        except Exception:
            pass
        try:
            self.btctl_proc.terminate()
            self.btctl_proc.wait(timeout=2)
        except Exception:
            try:
                self.btctl_proc.kill()
            except Exception:
                pass
        self.btctl_proc = None

    def _btctl_read_devices(self) -> str:
        try:
            proc = subprocess.run(
                ["bluetoothctl", "devices"],
                capture_output=True,
                text=True,
                timeout=8,
            )
            return proc.stdout or ""
        except subprocess.TimeoutExpired:
            # Under heavy BLE noise, this call can timeout. Keep scanner alive.
            with state_lock:
                state.last_error = "btctl timeout (high BLE load)"
                if state.scanner_health == "running":
                    state.scanner_health = "degraded"
            return ""

    def _classify_flipper(self, mac: str, name: str, packets: List[str]) -> Tuple[bool, str, str, str]:
        uid = "UNK"
        dtype = "UNK"
        is_flipper = False
        detection = "Unknown"
        key_found = False

        for packet in packets:
            p_low = str(packet).lower()
            if p_low in FLIPPER_TYPES:
                uid = p_low
                dtype = FLIPPER_TYPES[p_low]
                key_found = True
                is_flipper = True
                detection = "Identifier"
                break

        if not key_found:
            for packet in packets:
                p_low = str(packet).lower()
                if p_low.startswith(FLIPPER_UID_PREFIX) and p_low.endswith(FLIPPER_UID_SUFFIX):
                    uid = p_low
                    dtype = "SPF"
                    is_flipper = True
                    detection = "Identifier"
                    break

        if name.lower().startswith("flipper"):
            is_flipper = True
            detection = "Name"
            if uid == "UNK":
                uid = "name-only"
            if dtype == "UNK":
                dtype = "N"
        elif mac.startswith(FLIPPER_OUIS):
            is_flipper = True
            if detection == "Unknown":
                detection = "Address"
            if uid == "UNK":
                uid = "oui-only"
            if dtype == "UNK":
                dtype = "O"
        elif is_flipper and detection == "Unknown":
            detection = "Identifier"

        return is_flipper, uid, dtype, detection

    def _consume_observation(self, mac: str, name: str, rssi: int, packets: List[str], from_backend: str) -> None:
        now = int(time.time())
        mac = (mac or "unknown").lower()
        is_flipper, uid, dtype, detection = self._classify_flipper(mac, name, packets)

        with state_lock:
            state.packet_total += len(packets)
            for packet in packets:
                packet = str(packet).lower()
                if len(packet) > MIN_BYTE_LENGTH:
                    state.all_packets += 1
                if packet.startswith(WOF_ADVERTISER_RAW):
                    try:
                        decoded = bytes.fromhex(packet.replace(WOF_ADVERTISER_RAW, "")).decode("utf-8").replace("\x00", "")
                        decoded = decoded.strip() or "WoF"
                    except Exception:
                        decoded = "WoF"
                    prev = state.nearby_wof.get(decoded, {"count": 0, "last_seen": 0})
                    prev["count"] = int(prev.get("count", 0)) + 1
                    prev["last_seen"] = now
                    state.nearby_wof[decoded] = prev
                for fp in FORBIDDEN_PACKETS:
                    if _match_packet(packet, fp["PCK"]):
                        t = fp["TYPE"]
                        state.forbidden_total += 1
                        state.forbidden_by_type[t] = state.forbidden_by_type.get(t, 0) + 1
                        state.threat_events.appendleft(
                            {"t": now, "type": t, "mac": mac, "pkt": _short(packet, 24)}
                        )
                if len(packet) > MAX_BYTE_LENGTH:
                    t = "SUSPICIOUS_PACKET"
                    state.forbidden_total += 1
                    state.forbidden_by_type[t] = state.forbidden_by_type.get(t, 0) + 1
                    state.threat_events.appendleft(
                        {"t": now, "type": t, "mac": mac, "pkt": _short(packet, 24)}
                    )

            if is_flipper:
                is_new = mac not in state.devices
                if is_new:
                    self.new_flipper_times.append(now)
                    recent_new = [t for t in self.new_flipper_times if now - t <= RATELIMIT_SECONDS]
                    if len(recent_new) >= MAX_FLIPPERS_RATELIMITED:
                        state.is_ratelimited = True
                        state.last_ratelimit_until = now + RATELIMIT_SECONDS
                if state.is_ratelimited and is_new:
                    state.suspicious_dropped += 1
                else:
                    if mac in state.devices:
                        rec = state.devices[mac]
                        rec.last_seen = now
                        rec.rssi = rssi
                        if name and rec.name in ("Unknown", "UNK"):
                            rec.name = name
                    else:
                        state.devices[mac] = DeviceRecord(
                            name=name or "Unknown",
                            mac=mac,
                            rssi=rssi,
                            dtype=dtype,
                            uid=uid,
                            detection=detection,
                            first_seen=now,
                            last_seen=now,
                        )
                    state.new_events.appendleft(
                        {
                            "t": now,
                            "name": name or "Unknown",
                            "mac": mac,
                            "det": detection,
                            "dtype": dtype,
                            "backend": from_backend,
                        }
                    )
            if state.is_ratelimited and now >= state.last_ratelimit_until:
                state.is_ratelimited = False


class Ui:
    def __init__(self) -> None:
        self.lcd = None
        self.font_small = None
        self.font_med = None
        self.font_big = None
        self.fonts = None
        self.w = WIDTH
        self.h = HEIGHT
        self._last_console = ""
        if LCD_AVAILABLE:
            self.lcd = LCD_1in44.LCD()
            self.lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
            self.w = int(getattr(self.lcd, "width", WIDTH) or WIDTH)
            self.h = int(getattr(self.lcd, "height", HEIGHT) or HEIGHT)
            self.fonts = rj_theme.load_app_fonts(self.w, self.h)
            self.font_small = self.fonts["sub"]
            self.font_med = self.fonts["body"]
            self.font_big = self.fonts["title"]

    def _load_font(self, size: int, bold: bool = False):
        try:
            return ImageFont.truetype(FONT_BOLD if bold else FONT_PATH, size)
        except Exception:
            return scaled_font()

    def _rssi_pct(self, rssi: int) -> int:
        return max(0, min(100, int((int(rssi) + 100) * 100 / 60)))

    def _rssi_col(self, rssi: int) -> str:
        if rssi >= -60:
            return _T["ok"]
        if rssi >= -78:
            return _T["warn"]
        return _T["danger"]

    def draw(self) -> None:
        if self.lcd is None:
            self._draw_console()
            return
        self.w = int(getattr(self.lcd, "width", self.w) or self.w)
        self.h = int(getattr(self.lcd, "height", self.h) or self.h)
        self.fonts = rj_theme.load_app_fonts(self.w, self.h)
        img = Image.new("RGB", (self.w, self.h), _T["bg_base"])
        d = ImageDraw.Draw(img)
        with state_lock:
            screen = state.current_screen
            live = state.live_devices()
            offline = state.offline_devices()
            state.live_count = len(live)
            state.offline_count = len(offline)

            if screen == "dashboard":
                self._screen_dashboard(d, live, offline)
            elif screen == "live":
                self._screen_live(d, live, offline)
            elif screen == "detail":
                self._screen_detail(d)
            elif screen == "threats":
                self._screen_threats(d)
            elif screen == "nearby":
                self._screen_nearby(d)
            elif screen == "history":
                self._screen_history(d)
            elif screen == "settings":
                self._screen_settings(d)
            else:
                self._screen_dashboard(d, live, offline)
        self.lcd.LCD_ShowImage(img, 0, 0)

    def _header(self, d, title: str, status: str = "", right_text: str = "") -> int:
        return rj_theme.draw_app_header(
            d, self.w, title, self.fonts,
            badge=status,
            right_text=right_text if self.fonts["wide"] else "",
            right_pill=f"{state.scanner_backend} hci{settings.hci_adapter}" if (self.fonts["wide"] and not right_text) else "",
        )

    def _footer(self, d, txt: str, right_text: str = "") -> int:
        return rj_theme.draw_app_footer(d, self.w, self.h, txt, self.fonts, right_text=right_text)

    def _screen_dashboard(self, d, live: List[DeviceRecord], offline: List[DeviceRecord]) -> None:
        wide, large = self.fonts["wide"], self.fonts["large"]
        hdr_h = self._header(d, "WALL OF FLIPPERS", "RUN" if state.scan_enabled else "PAUSE")
        ftr_h = self._footer(
            d,
            "L/R:Views   K1:Pause/Scan   K2:Clear   K3:Exit" if wide else "L/R:Vw K1:Scn K3:X",
            right_text=f"Up {_age_text(state.running_since)}" if large else "",
        )
        pad = 10 if large else (5 if wide else 3)
        y0 = hdr_h + pad
        y1 = self.h - ftr_h - pad

        if self.w >= 480:
            kpi_h = 68
            gap = 8
            tw = (self.w - pad * 2 - gap * 3) // 4
            kpis = [
                ("LIVE FLIPPERS", str(len(live)), _T["ok"]),
                ("OFFLINE", str(len(offline)), _T["text_muted"]),
                ("BLE THREATS", str(state.forbidden_total), _T["danger"] if state.forbidden_total else _T["warn"]),
                ("NEARBY WoF", str(len(state.nearby_wof)), _T["accent_bright"]),
            ]
            for idx_k, (lbl, val, acc) in enumerate(kpis):
                kx = pad + idx_k * (tw + gap)
                rj_theme.draw_card(d, (kx, y0, kx + tw, y0 + kpi_h), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=6, accent_left=acc, accent_width=4)
                d.text((kx + 12, y0 + 10), lbl, font=self.fonts["badge"], fill=_T["text_muted"])
                d.text((kx + 12, y0 + 32), val, font=self.fonts["title"], fill=acc)

            by0 = y0 + kpi_h + 10
            mid = int(self.w * 0.56)
            rj_theme.draw_card(d, (pad, by0, mid - 5, y1), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=8, accent_left=_T["accent"], accent_width=4)
            lx = pad + 14
            ly = by0 + 12
            lmw = (mid - 5) - lx - 10
            d.text((lx, ly), "LATEST FLIPPER DETECTION", font=self.fonts["badge"], fill=_T["accent_bright"])
            ly += 24
            if state.new_events:
                e = state.new_events[0]
                d.text((lx, ly), rj_theme.truncate_text(d, e["name"], lmw, self.fonts["title"]), font=self.fonts["title"], fill=_T["text_bright"])
                ly += 26
                d.text((lx, ly), e["mac"].upper(), font=self.fonts["mono"], fill=_T["accent_bright"])
                ly += 24
                d.text((lx, ly), f"Method: {e['det']}   |   Type: {e.get('dtype', '?')}   |   {_age_text(int(e['t']))} ago", font=self.fonts["body"], fill=_T["text_muted"])
            else:
                d.text((lx, ly + 10), "Listening for Flipper Zero BLE beacons...", font=self.fonts["body"], fill=_T["text_dim"])

            rx0 = mid + 5
            rj_theme.draw_card(d, (rx0, by0, self.w - pad, y1), fill=_T["bg_surface"], outline=_T["danger"] if state.is_ratelimited else _T["border_subtle"], radius=8, accent_left=_T["ok"] if state.scanner_health == "running" else _T["warn"], accent_width=4)
            rx = rx0 + 14
            ry = by0 + 12
            rmw = self.w - pad - rx - 10
            d.text((rx, ry), "SCANNER TELEMETRY", font=self.fonts["badge"], fill=_T["ok"])
            ry += 24
            for k, v, col in [
                ("Backend", f"{state.scanner_backend} (hci{settings.hci_adapter})", _T["text_bright"]),
                ("Health", state.scanner_health, _T["ok"] if state.scanner_health == "running" else _T["warn"]),
                ("BLE Pkts", f"{state.all_packets:,}", _T["accent_bright"]),
                ("Uptime", _age_text(state.running_since), _T["text_muted"]),
            ]:
                d.text((rx, ry), f"{k}:", font=self.fonts["sub"], fill=_T["text_muted"])
                d.text((rx + 72, ry), rj_theme.truncate_text(d, v, rmw - 72, self.fonts["body_bold"]), font=self.fonts["body_bold"], fill=col)
                ry += 22
            if state.is_ratelimited:
                ttl = max(0, state.last_ratelimit_until - int(time.time()))
                d.text((rx, y1 - 24), f"FLOOD GUARD ACTIVE ({ttl}s)", font=self.fonts["badge"], fill=_T["danger"])
            elif state.last_error:
                d.text((rx, y1 - 24), rj_theme.truncate_text(d, state.last_error, rmw, self.fonts["sub"]), font=self.fonts["sub"], fill=_T["danger"])
        else:
            rj_theme.draw_card(d, (pad, y0, self.w - pad, y1), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=5 if wide else 3, accent_left=_T["accent"], accent_width=3 if wide else 2)
            tx = pad + (10 if wide else 5)
            mw = self.w - pad - tx - 4
            y = y0 + (6 if wide else 3)
            step = 17 if wide else (16 if self.h >= 200 else 12)
            d.text((tx, y), f"Live:{len(live)}  Off:{len(offline)}  Thr:{state.forbidden_total}", font=self.fonts["body_bold"] if wide else self.fonts["sub"], fill=_T["ok"])
            y += step
            d.text((tx, y), f"Nearby:{len(state.nearby_wof)}  Pkts:{state.all_packets}", font=self.fonts["body"] if wide else self.fonts["sub"], fill=_T["accent_bright"])
            y += step
            d.text((tx, y), f"{state.scanner_backend} hci{settings.hci_adapter}  Up:{_age_text(state.running_since)}", font=self.fonts["sub"], fill=_T["text_muted"])
            y += step
            if state.is_ratelimited:
                ttl = max(0, state.last_ratelimit_until - int(time.time()))
                d.text((tx, y), f"Rate-limit guard: {ttl}s", font=self.fonts["sub"], fill=_T["danger"])
                y += step
            elif state.last_error:
                d.text((tx, y), rj_theme.truncate_text(d, state.last_error, mw, self.fonts["sub"]), font=self.fonts["sub"], fill=_T["danger"])
                y += step
            if state.new_events and y + 10 <= y1:
                e = state.new_events[0]
                d.text((tx, y), rj_theme.truncate_text(d, f"Last: {e['name']} ({_mac_tail(e['mac'])})", mw, self.fonts["sub"]), font=self.fonts["sub"], fill=_T["text_bright"])
            elif y + 10 <= y1:
                d.text((tx, y), "No flippers detected yet", font=self.fonts["sub"], fill=_T["text_dim"])

    def _sorted_live(self, live: List[DeviceRecord]) -> List[DeviceRecord]:
        if settings.sort_mode == "rssi":
            return sorted(live, key=lambda x: x.rssi, reverse=True)
        return sorted(live, key=lambda x: x.last_seen, reverse=True)

    def _screen_live(self, d, live: List[DeviceRecord], offline: List[DeviceRecord]) -> None:
        wide, large = self.fonts["wide"], self.fonts["large"]
        merged = self._sorted_live(live) + sorted(offline, key=lambda x: x.last_seen, reverse=True)
        hdr_h = self._header(d, "FLIPPERS", f"{len(live)} LIVE", right_text=f"{len(merged)} total")
        ftr_h = self._footer(d, "Up/Dn:Select  OK:Detail  L/R:Views  K1:Scan" if wide else "OK:Det U/D:Sel L/R:Vw")
        pad = 8 if large else (4 if wide else 2)
        y0 = hdr_h + pad
        y1 = self.h - ftr_h - pad

        if not merged:
            rj_theme.draw_status_card(d, self.w, self.h, "No Flippers Yet", sub="Scanning BLE advertisements...", kind="info", fonts=self.fonts)
            return

        state.selected_live = max(0, min(state.selected_live, len(merged) - 1))
        split = self.w >= 480
        lx1 = int(self.w * 0.62) - 4 if split else self.w - pad - 5
        row_h = 28 if large else (21 if wide else (18 if self.h >= 200 else 13))
        vis = max(1, (y1 - y0) // row_h)
        start = max(0, min(state.selected_live - vis // 2, max(0, len(merged) - vis)))
        now = int(time.time())

        for idx, rec in enumerate(merged[start : start + vis]):
            gi = start + idx
            sel = gi == state.selected_live
            y = y0 + idx * row_h
            is_live = (now - rec.last_seen) <= settings.offline_timeout
            if sel:
                rj_theme.draw_card(d, (pad, y, lx1, y + row_h - 2), fill=_T["select_bg"], outline=_T["border_accent"], radius=4 if wide else 2, accent_left=_T["ok"] if is_live else _T["text_muted"], accent_width=3 if wide else 2)
            elif idx % 2 == 1 and wide:
                d.rectangle((pad, y, lx1, y + row_h - 2), fill=_T["bg_surface"])
            ty = y + max(1, (row_h - (13 if large else (10 if wide else 8))) // 2)
            rx = lx1 - (8 if wide else 3)
            if is_live:
                rj_theme.draw_signal_bars(d, rx - 14, y + max(1, (row_h - (11 if large else 8)) // 2), 11 if large else 8, self._rssi_pct(rec.rssi), active_color=self._rssi_col(rec.rssi))
                rx -= 18
                rtxt = f"{rec.rssi}"
            else:
                rtxt = "OFF"
            rw = rj_theme.measure_text(self.fonts["sub"], rtxt, d)[2]
            d.text((rx - rw, ty), rtxt, font=self.fonts["sub"], fill=_T["text_muted"])
            rx -= rw + (8 if wide else 3)
            if wide:
                mt = rec.mac.upper() if large else _mac_tail(rec.mac)
                mw_m = rj_theme.measure_text(self.fonts["sub"], mt, d)[2]
                d.text((rx - mw_m, ty), mt, font=self.fonts["sub"], fill=_T["text_dim"])
                rx -= mw_m + 8
            tx = pad + (8 if wide else 3)
            if wide:
                pw, _ = rj_theme.draw_pill(d, tx, y + max(1, (row_h - 15) // 2), "L" if is_live else "O", self.fonts["badge"], bg=_T["ok_bg"] if is_live else _T["bg_elevated"], fg=_T["ok"] if is_live else _T["text_dim"], outline=None, pad_x=4, pad_y=1)
                tx += pw + 6
                name_s = rec.name
            else:
                name_s = f"{'L' if is_live else 'O'} {rec.name.replace('Flipper ', '')}"
            d.text((tx, ty), rj_theme.truncate_text(d, name_s, max(16, rx - tx - 2), self.fonts["body_bold"] if sel else self.fonts["sub"]), font=self.fonts["body_bold"] if (sel and wide) else (self.fonts["body"] if wide else self.fonts["sub"]), fill=_T["text_bright"] if sel else _T["text_primary"])

        if len(merged) > vis:
            rj_theme.draw_scrollbar(d, lx1 + 2 if split else self.w - 4, y0, y1, start, vis, len(merged), width=3)

        if split:
            rec = merged[state.selected_live]
            px0 = lx1 + 10
            rj_theme.draw_card(d, (px0, y0, self.w - pad, y1), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=8, accent_left=_T["accent"], accent_width=4)
            dx, dy = px0 + 12, y0 + 12
            dmw = self.w - pad - dx - 8
            d.text((dx, dy), "FLIPPER INSPECTOR", font=self.fonts["badge"], fill=_T["accent_bright"])
            dy += 20
            d.text((dx, dy), rj_theme.truncate_text(d, rec.name, dmw, self.fonts["title"]), font=self.fonts["title"], fill=_T["text_bright"])
            dy += 22
            d.text((dx, dy), rec.mac.upper(), font=self.fonts["mono"], fill=_T["accent_bright"])
            dy += 22
            d.line([(dx, dy), (self.w - pad - 10, dy)], fill=_T["border_subtle"])
            dy += 10
            for k, v in [("RSSI", f"{rec.rssi} dBm"), ("Type", rec.dtype), ("Detect", rec.detection), ("First", _age_text(rec.first_seen)), ("Last", _age_text(rec.last_seen))]:
                d.text((dx, dy), f"{k}:", font=self.fonts["sub"], fill=_T["text_muted"])
                d.text((dx + 58, dy), rj_theme.truncate_text(d, v, dmw - 58, self.fonts["body_bold"]), font=self.fonts["body_bold"], fill=_T["text_primary"])
                dy += 20

    def _screen_detail(self, d) -> None:
        wide, large = self.fonts["wide"], self.fonts["large"]
        hdr_h = self._header(d, "DEVICE DETAIL")
        ftr_h = self._footer(d, "LEFT:Back   K3:Exit")
        rec = state.devices.get(state.detail_mac)
        if rec is None:
            rj_theme.draw_status_card(d, self.w, self.h, "No Device Selected", sub="Press LEFT to return", kind="info", fonts=self.fonts)
            return
        pad = 10 if large else (5 if wide else 3)
        y0, y1 = hdr_h + pad, self.h - ftr_h - pad
        rj_theme.draw_card(d, (pad, y0, self.w - pad, y1), fill=_T["bg_surface"], outline=_T["border_accent"], radius=8 if wide else 4, accent_left=_T["accent"], accent_width=4 if wide else 2)
        tx = pad + (14 if wide else 6)
        mw = self.w - pad - tx - 6
        y = y0 + (12 if large else (6 if wide else 4))
        step = 24 if large else (17 if wide else (16 if self.h >= 200 else 12))
        d.text((tx, y), rj_theme.truncate_text(d, rec.name, mw, self.fonts["title"]), font=self.fonts["title"], fill=_T["text_bright"])
        y += step + (4 if large else 1)
        for lbl, val, col in [
            ("MAC", rec.mac.upper(), _T["accent_bright"]),
            ("RSSI / Type", f"{rec.rssi} dBm   |   Type: {rec.dtype}", self._rssi_col(rec.rssi)),
            ("Detection", rec.detection, _T["text_primary"]),
            ("UID", rec.uid, _T["text_muted"]),
            ("Seen", f"First {_age_text(rec.first_seen)}   |   Last {_age_text(rec.last_seen)}", _T["ok"]),
        ]:
            if y + 10 > y1:
                break
            d.text((tx, y), rj_theme.truncate_text(d, f"{lbl}: {val}", mw, self.fonts["body"] if wide else self.fonts["sub"]), font=self.fonts["body"] if wide else self.fonts["sub"], fill=col)
            y += step

    def _screen_threats(self, d) -> None:
        wide, large = self.fonts["wide"], self.fonts["large"]
        hdr_h = self._header(d, "BLE THREATS", f"{state.forbidden_total}")
        ftr_h = self._footer(d, "Up/Dn:Select   K2:Clear Threats   L/R:Views" if wide else "U/D:Sel K2:Clr L/R:Vw")
        pad = 8 if large else (4 if wide else 2)
        y0, y1 = hdr_h + pad, self.h - ftr_h - pad

        if state.scanner_backend == "bluetoothctl" and not state.forbidden_by_type:
            rj_theme.draw_status_card(d, self.w, self.h, "btctl Backend Limited", sub="Install bleak/bluepy for full BLE spam threat signatures", kind="warn", fonts=self.fonts)
            return
        items = sorted(state.forbidden_by_type.items(), key=lambda kv: kv[1], reverse=True)
        if not items:
            rj_theme.draw_status_card(d, self.w, self.h, "No Threats Detected", sub="Monitoring Apple, Android, Samsung & Windows BLE spam", kind="ok", fonts=self.fonts)
            return

        state.selected_threat = max(0, min(state.selected_threat, len(items) - 1))
        detail_h = 46 if large else (30 if wide else 22)
        list_bot = y1 - detail_h - 4
        row_h = 26 if large else (20 if wide else 12)
        vis = max(1, (list_bot - y0) // row_h)
        start = max(0, min(state.selected_threat - vis // 2, max(0, len(items) - vis)))
        for idx, (tt, cnt) in enumerate(items[start : start + vis]):
            gi = start + idx
            sel = gi == state.selected_threat
            y = y0 + idx * row_h
            if sel:
                rj_theme.draw_card(d, (pad, y, self.w - pad - 5, y + row_h - 2), fill=_T["danger_bg"], outline=_T["danger"], radius=4 if wide else 2, accent_left=_T["danger"], accent_width=3 if wide else 2)
            ty = y + max(1, (row_h - (13 if large else 9)) // 2)
            cnt_s = str(cnt)
            cw_p = rj_theme.measure_text(self.fonts["badge"], cnt_s, d)[2] + (10 if wide else 4)
            if wide:
                rj_theme.draw_pill(d, self.w - pad - cw_p - 8, y + max(1, (row_h - 15) // 2), cnt_s, self.fonts["badge"], bg=_T["danger_bg"], fg=_T["danger"], outline=_T["danger"], pad_x=5, pad_y=1)
            else:
                d.text((self.w - pad - cw_p - 4, ty), cnt_s, font=self.fonts["sub"], fill=_T["danger"])
            d.text((pad + (8 if wide else 3), ty), rj_theme.truncate_text(d, tt.replace("BLE_", ""), self.w - pad * 2 - cw_p - 22, self.fonts["body_bold"] if (sel and wide) else self.fonts["sub"]), font=self.fonts["body_bold"] if (sel and wide) else self.fonts["sub"], fill=_T["text_bright"] if sel else _T["warn"])
        if len(items) > vis:
            rj_theme.draw_scrollbar(d, self.w - 4, y0, list_bot, start, vis, len(items), width=3)

        sel_type = items[state.selected_threat][0]
        ev = next((x for x in state.threat_events if x.get("type") == sel_type), None)
        rj_theme.draw_card(d, (pad, list_bot + 4, self.w - pad, y1), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=4 if wide else 2)
        if ev:
            d.text((pad + (8 if wide else 3), list_bot + (8 if large else 5)), rj_theme.truncate_text(d, f"SRC: {ev.get('mac', '').upper()}   ({_age_text(int(ev.get('t', 0)))} ago)", self.w - pad * 2 - 12, self.fonts["sub"]), font=self.fonts["sub"], fill=_T["accent_bright"])
            if wide or self.h >= 200:
                d.text((pad + (8 if wide else 3), list_bot + (24 if large else 16)), rj_theme.truncate_text(d, f"PKT: {ev.get('pkt', '')}", self.w - pad * 2 - 12, self.fonts["mono"]), font=self.fonts["mono"], fill=_T["text_muted"])

    def _screen_nearby(self, d) -> None:
        wide, large = self.fonts["wide"], self.fonts["large"]
        hdr_h = self._header(d, "NEARBY WoF", f"{len(state.nearby_wof)}")
        ftr_h = self._footer(d, "Up/Dn:Select   L/R:Views   K3:Exit" if wide else "U/D:Sel L/R:Vw")
        pad = 8 if large else (4 if wide else 2)
        y0, y1 = hdr_h + pad, self.h - ftr_h - pad
        items = sorted(state.nearby_wof.items(), key=lambda kv: int(kv[1].get("count", 0)), reverse=True)
        if not items:
            rj_theme.draw_status_card(d, self.w, self.h, "No Nearby WoF", sub="Listening for peer Wall of Flippers beacons", kind="info", fonts=self.fonts)
            return
        state.selected_nearby = max(0, min(state.selected_nearby, len(items) - 1))
        row_h = 26 if large else (20 if wide else 13)
        vis = max(1, (y1 - y0) // row_h)
        start = max(0, min(state.selected_nearby - vis // 2, max(0, len(items) - vis)))
        for idx, (name, meta) in enumerate(items[start : start + vis]):
            gi = start + idx
            sel = gi == state.selected_nearby
            y = y0 + idx * row_h
            if sel:
                rj_theme.draw_card(d, (pad, y, self.w - pad - 5, y + row_h - 2), fill=_T["select_bg"], outline=_T["border_accent"], radius=4 if wide else 2, accent_left=_T["accent"], accent_width=3 if wide else 2)
            ty = y + max(1, (row_h - (13 if large else 9)) // 2)
            age_s = _age_text(int(meta.get("last_seen", 0)))
            cnt_s = f"x{meta.get('count', 0)}"
            right_s = f"{cnt_s}  {age_s}"
            rw = rj_theme.measure_text(self.fonts["sub"], right_s, d)[2]
            d.text((self.w - pad - rw - 8, ty), right_s, font=self.fonts["sub"], fill=_T["accent_bright"])
            d.text((pad + (8 if wide else 3), ty), rj_theme.truncate_text(d, name, self.w - pad * 2 - rw - 20, self.fonts["body_bold"] if (sel and wide) else self.fonts["sub"]), font=self.fonts["body_bold"] if (sel and wide) else self.fonts["sub"], fill=_T["text_bright"] if sel else _T["text_primary"])
        if len(items) > vis:
            rj_theme.draw_scrollbar(d, self.w - 4, y0, y1, start, vis, len(items), width=3)

    def _screen_history(self, d) -> None:
        wide, large = self.fonts["wide"], self.fonts["large"]
        hdr_h = self._header(d, "HISTORY", f"{len(state.history_sessions)}")
        ftr_h = self._footer(d, "Up/Dn:Scroll   K2:Save Snapshot   L/R:Views" if wide else "K2:Snap L/R:Vw")
        pad = 8 if large else (4 if wide else 2)
        y0, y1 = hdr_h + pad, self.h - ftr_h - pad
        if not state.history_sessions:
            rj_theme.draw_status_card(d, self.w, self.h, "No Saved Sessions", sub="Press KEY2 to save snapshot now", kind="info", fonts=self.fonts)
            return
        state.selected_history = max(0, min(state.selected_history, len(state.history_sessions) - 1))
        sessions = list(reversed(state.history_sessions))
        row_h = 44 if large else (32 if wide else 22)
        vis = max(1, (y1 - y0) // row_h)
        start = max(0, min(state.selected_history - vis // 2, max(0, len(sessions) - vis)))
        for idx, sess in enumerate(sessions[start : start + vis]):
            gi = start + idx
            sel = gi == state.selected_history
            y = y0 + idx * row_h
            rj_theme.draw_card(d, (pad, y, self.w - pad - 5, y + row_h - 3), fill=_T["select_bg"] if sel else _T["bg_surface"], outline=_T["border_accent"] if sel else _T["border_subtle"], radius=4 if wide else 2, accent_left=_T["accent"] if sel else None, accent_width=3 if wide else 2)
            tx = pad + (10 if wide else 4)
            mw = self.w - pad * 2 - 16
            l1 = f"{sess.get('ts', '')}   Live:{sess.get('live', 0)}   Threats:{sess.get('threats', 0)}" if wide else f"{_short(sess.get('ts', ''), 11)} L{sess.get('live', 0)} T{sess.get('threats', 0)}"
            l2 = f"Nearby:{sess.get('nearby', 0)}  Pkts:{sess.get('packets', 0)}  Backend:{sess.get('backend', '')}" + (f"  |  {sess.get('latest', '')}" if sess.get("latest") and wide else "")
            d.text((tx, y + (5 if large else 2)), rj_theme.truncate_text(d, l1, mw, self.fonts["body_bold"] if wide else self.fonts["sub"]), font=self.fonts["body_bold"] if wide else self.fonts["sub"], fill=_T["text_bright"] if sel else _T["text_primary"])
            d.text((tx, y + (23 if large else (16 if wide else 11))), rj_theme.truncate_text(d, l2, mw, self.fonts["sub"]), font=self.fonts["sub"], fill=_T["text_muted"])
        if len(sessions) > vis:
            rj_theme.draw_scrollbar(d, self.w - 4, y0, y1, start, vis, len(sessions), width=3)

    def _screen_settings(self, d) -> None:
        wide, large = self.fonts["wide"], self.fonts["large"]
        hdr_h = self._header(d, "SETTINGS", state.scanner_health.upper()[:6])
        ftr_h = self._footer(d, "Up/Dn:Nav   OK:Cycle Value   K2:Save   L/R:Views" if wide else "OK:Edit K2:Save L/R:Vw")
        pad = 8 if large else (4 if wide else 2)
        y0, y1 = hdr_h + pad, self.h - ftr_h - pad
        items = [
            ("Scan Interval", f"{settings.scan_interval:.1f}s"),
            ("Offline Timeout", f"{settings.offline_timeout}s"),
            ("Sort Order", settings.sort_mode),
            ("Badge Mode", "ON" if settings.badge_mode else "OFF"),
            ("HCI Adapter", f"hci{settings.hci_adapter}"),
        ]
        state.selected_setting = max(0, min(state.selected_setting, len(items) - 1))
        row_h = 34 if large else (22 if wide else 14)
        y = y0
        for i, (k, v) in enumerate(items):
            sel = i == state.selected_setting
            if sel:
                rj_theme.draw_card(d, (pad, y, self.w - pad, y + row_h - 3), fill=_T["select_bg"], outline=_T["border_accent"], radius=5 if wide else 2, accent_left=_T["accent"], accent_width=3 if wide else 2)
            elif wide:
                rj_theme.draw_card(d, (pad, y, self.w - pad, y + row_h - 3), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=5)
            ty = y + max(1, (row_h - (15 if large else 10)) // 2)
            d.text((pad + (12 if wide else 4), ty), k, font=self.fonts["body_bold"] if (sel and wide) else (self.fonts["body"] if wide else self.fonts["sub"]), fill=_T["text_bright"] if sel else _T["text_primary"])
            vw = rj_theme.measure_text(self.fonts["badge"] if wide else self.fonts["sub"], v, d)[2]
            if wide:
                rj_theme.draw_pill(d, self.w - pad - vw - 22, y + max(2, (row_h - 17) // 2), v, self.fonts["badge"], bg=_T["bg_elevated"], fg=_T["accent_bright"] if sel else _T["ok"], outline=_T["border_accent"] if sel else None, pad_x=6, pad_y=2)
            else:
                d.text((self.w - pad - vw - 4, ty), v, font=self.fonts["sub"], fill=_T["accent_bright"])
            y += row_h

        if y + 16 <= y1:
            info_s = f"Health: {state.scanner_health}   |   Dropped: {state.suspicious_dropped}"
            d.text((pad + 4, y + 4), rj_theme.truncate_text(d, info_s, self.w - pad * 2 - 8, self.fonts["sub"]), font=self.fonts["sub"], fill=_T["text_muted"])
            if state.last_error and y + 32 <= y1:
                d.text((pad + 4, y + 20), rj_theme.truncate_text(d, state.last_error, self.w - pad * 2 - 8, self.fonts["sub"]), font=self.fonts["sub"], fill=_T["danger"])

    def _draw_console(self) -> None:
        with state_lock:
            s = (
                f"WoF {state.current_screen} "
                f"scan={'on' if state.scan_enabled else 'off'} "
                f"live={state.live_count} off={state.offline_count} "
                f"threat={state.forbidden_total}"
            )
        if s != self._last_console:
            print(s)
            self._last_console = s


def _now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load_history() -> None:
    os.makedirs(WOF_LOOT_DIR, exist_ok=True)
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            state.history_sessions = list(data.get("sessions", []))[-40:]
            saved = data.get("settings", {})
            settings.scan_interval = float(saved.get("scan_interval", settings.scan_interval))
            settings.offline_timeout = int(saved.get("offline_timeout", settings.offline_timeout))
            settings.sort_mode = str(saved.get("sort_mode", settings.sort_mode))
            settings.badge_mode = bool(saved.get("badge_mode", settings.badge_mode))
            settings.hci_adapter = int(saved.get("hci_adapter", settings.hci_adapter))
            # Restore archived devices so users keep historical visibility between runs.
            devices = data.get("devices", [])
            if isinstance(devices, list):
                restored: Dict[str, DeviceRecord] = {}
                for row in devices:
                    try:
                        mac = str(row.get("mac", "")).lower()
                        if not mac:
                            continue
                        restored[mac] = DeviceRecord(
                            name=str(row.get("name", "Unknown")),
                            mac=mac,
                            rssi=int(row.get("rssi", -100)),
                            dtype=str(row.get("dtype", "UNK")),
                            uid=str(row.get("uid", "UNK")),
                            detection=str(row.get("detection", "Unknown")),
                            first_seen=int(row.get("first_seen", int(time.time()))),
                            last_seen=int(row.get("last_seen", int(time.time()))),
                        )
                    except Exception:
                        continue
                if restored:
                    state.devices.update(restored)
            # Backward compatibility: nearby values used to be plain counts.
            legacy_nearby = data.get("nearby_wof", {})
            if isinstance(legacy_nearby, dict):
                converted = {}
                now = int(time.time())
                for k, v in legacy_nearby.items():
                    if isinstance(v, dict):
                        converted[str(k)] = {
                            "count": int(v.get("count", 0)),
                            "last_seen": int(v.get("last_seen", now)),
                        }
                    else:
                        converted[str(k)] = {"count": int(v), "last_seen": now}
                state.nearby_wof = converted
            state.forbidden_total = int(data.get("forbidden_total", state.forbidden_total))
            state.forbidden_by_type = dict(data.get("forbidden_by_type", state.forbidden_by_type))
            state.suspicious_dropped = int(data.get("suspicious_dropped", state.suspicious_dropped))
    except Exception:
        pass


def save_history_snapshot() -> None:
    os.makedirs(WOF_LOOT_DIR, exist_ok=True)
    with state_lock:
        sess = {
            "ts": _now_stamp(),
            "live": len(state.live_devices()),
            "offline": len(state.offline_devices()),
            "threats": state.forbidden_total,
            "nearby": len(state.nearby_wof),
            "packets": state.all_packets,
            "backend": state.scanner_backend,
        }
        if state.new_events:
            latest = state.new_events[0]
            sess["latest"] = f"{latest.get('name', 'Unknown')}@{latest.get('mac', '')[-5:]}"
        top_threat = sorted(state.forbidden_by_type.items(), key=lambda kv: kv[1], reverse=True)
        if top_threat:
            sess["top_threat"] = f"{top_threat[0][0]}:{top_threat[0][1]}"
        state.history_sessions.append(sess)
        state.history_sessions = state.history_sessions[-40:]
        device_archive = []
        for rec in sorted(state.devices.values(), key=lambda x: x.last_seen, reverse=True)[:300]:
            device_archive.append(
                {
                    "name": rec.name,
                    "mac": rec.mac,
                    "rssi": rec.rssi,
                    "dtype": rec.dtype,
                    "uid": rec.uid,
                    "detection": rec.detection,
                    "first_seen": rec.first_seen,
                    "last_seen": rec.last_seen,
                }
            )
        payload = {
            "schema_version": 2,
            "saved_at": _now_stamp(),
            "sessions": state.history_sessions,
            "settings": {
                "scan_interval": settings.scan_interval,
                "offline_timeout": settings.offline_timeout,
                "sort_mode": settings.sort_mode,
                "badge_mode": settings.badge_mode,
                "hci_adapter": settings.hci_adapter,
            },
            "devices": device_archive,
            "nearby_wof": state.nearby_wof,
            "forbidden_total": state.forbidden_total,
            "forbidden_by_type": state.forbidden_by_type,
            "suspicious_dropped": state.suspicious_dropped,
        }
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception:
        pass


def _signal_handler(_signum, _frame) -> None:
    global running
    running = False


def _handle_button(btn: str) -> None:
    if not btn:
        return
    request_snapshot = False
    handled = False
    with state_lock:
        if btn == "KEY3":
            if state.current_screen == "detail":
                state.current_screen = "live"
                return
            global running
            running = False
            return
        if btn == "KEY1":
            state.scan_enabled = not state.scan_enabled
            return
        if btn == "LEFT":
            if state.current_screen == "detail":
                state.current_screen = "live"
                return
            order = ["dashboard", "live", "threats", "nearby", "history", "settings"]
            i = order.index(state.current_screen) if state.current_screen in order else 0
            state.current_screen = order[(i - 1) % len(order)]
            return
        if btn == "RIGHT":
            if state.current_screen == "detail":
                return
            order = ["dashboard", "live", "threats", "nearby", "history", "settings"]
            i = order.index(state.current_screen) if state.current_screen in order else 0
            state.current_screen = order[(i + 1) % len(order)]
            return

        if btn == "KEY2":
            handled = True
            if state.current_screen == "threats":
                state.forbidden_total = 0
                state.forbidden_by_type = {}
                state.suspicious_dropped = 0
                state.threat_events.clear()
            elif state.current_screen == "dashboard":
                state.nearby_wof = {}
                state.new_events.clear()
            else:
                request_snapshot = True
        
        if handled:
            # KEY2 actions should not fall through to navigation handlers.
            pass

        elif state.current_screen == "live":
            total = len(state.live_devices()) + len(state.offline_devices())
            if btn == "UP":
                state.selected_live = max(0, state.selected_live - 1)
            elif btn == "DOWN":
                state.selected_live = min(max(0, total - 1), state.selected_live + 1)
            elif btn == "OK" and total > 0:
                merged = (
                    sorted(state.live_devices(), key=lambda x: x.last_seen, reverse=True)
                    + sorted(state.offline_devices(), key=lambda x: x.last_seen, reverse=True)
                )
                state.detail_mac = merged[state.selected_live].mac
                state.current_screen = "detail"
        elif state.current_screen == "threats":
            total = len(state.forbidden_by_type)
            if btn == "UP":
                state.selected_threat = max(0, state.selected_threat - 1)
            elif btn == "DOWN":
                state.selected_threat = min(max(0, total - 1), state.selected_threat + 1)
        elif state.current_screen == "nearby":
            total = len(state.nearby_wof)
            if btn == "UP":
                state.selected_nearby = max(0, state.selected_nearby - 1)
            elif btn == "DOWN":
                state.selected_nearby = min(max(0, total - 1), state.selected_nearby + 1)
        elif state.current_screen == "history":
            total = len(state.history_sessions)
            if btn == "UP":
                state.selected_history = max(0, state.selected_history - 1)
            elif btn == "DOWN":
                state.selected_history = min(max(0, total - 1), state.selected_history + 1)
        elif state.current_screen == "settings":
            options = 5
            if btn == "UP":
                state.selected_setting = max(0, state.selected_setting - 1)
            elif btn == "DOWN":
                state.selected_setting = min(options - 1, state.selected_setting + 1)
            elif btn == "OK":
                idx = state.selected_setting
                if idx == 0:
                    vals = [0.5, 1.0, 1.5, 2.0]
                    cur = vals.index(settings.scan_interval) if settings.scan_interval in vals else 1
                    settings.scan_interval = vals[(cur + 1) % len(vals)]
                elif idx == 1:
                    vals = [15, 25, 40, 60]
                    cur = vals.index(settings.offline_timeout) if settings.offline_timeout in vals else 1
                    settings.offline_timeout = vals[(cur + 1) % len(vals)]
                elif idx == 2:
                    settings.sort_mode = "rssi" if settings.sort_mode == "last_seen" else "last_seen"
                elif idx == 3:
                    settings.badge_mode = not settings.badge_mode
                elif idx == 4:
                    settings.hci_adapter = (settings.hci_adapter + 1) % 4
                request_snapshot = True
    if request_snapshot:
        save_history_snapshot()


def _setup_gpio() -> None:
    if GPIO is None:
        return
    GPIO.setmode(GPIO.BCM)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)


def _cleanup(ui: Ui, scanner: ScannerWorker) -> None:
    scanner.stop()
    save_history_snapshot()
    if ui.lcd is not None:
        try:
            ui.lcd.LCD_Clear()
        except Exception:
            pass
    if GPIO is not None:
        try:
            GPIO.cleanup()
        except Exception:
            pass


def main() -> None:
    global running
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    load_history()
    _setup_gpio()

    # Let user select BT adapter
    if LCD_AVAILABLE and LCD_1in44:
        import LCD_Config as _LC
        _LC.GPIO_Init()
        _lcd = LCD_1in44.LCD()
        _lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
        _font = scaled_font()
        chosen = select_bt_interface(_lcd, _font, PINS, GPIO)
        if chosen:
            try:
                settings.hci_adapter = int(chosen.replace("hci", ""))
            except ValueError:
                pass

    ui = Ui()
    scanner = ScannerWorker()
    scanner.start()

    frame_sleep = 0.1 if settings.badge_mode else 0.08
    last_btn = None
    last_btn_t = 0.0
    last_snapshot_t = time.monotonic()

    try:
        while running:
            btn = get_button(PINS, GPIO) if GPIO is not None else None
            now = time.time()
            if btn:
                if btn != last_btn or (now - last_btn_t) > 0.15:
                    _handle_button(btn)
                    last_btn = btn
                    last_btn_t = now
            else:
                last_btn = None
            ui.draw()
            frame_sleep = 0.13 if settings.badge_mode else 0.08
            # Periodic snapshot keeps history meaningful across long sessions.
            if time.monotonic() - last_snapshot_t >= 60.0:
                save_history_snapshot()
                last_snapshot_t = time.monotonic()
            time.sleep(frame_sleep)
    finally:
        _cleanup(ui, scanner)


if __name__ == "__main__":
    main()

