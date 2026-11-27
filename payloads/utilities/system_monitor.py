#!/usr/bin/env python3
"""
RaspyJack Payload -- System Monitor
=====================================
Author: 7h30th3r0n3

Real-time system resource dashboard.  Reads CPU, RAM, temperature, disk,
uptime, network throughput, and service status from /proc and /sys.

Setup / Prerequisites
---------------------
- RaspyJack base system with LCD hat.
- Thermal zone at /sys/class/thermal/thermal_zone0/temp.

Controls
--------
  LEFT / RIGHT  -- Switch view (Dashboard, CPU Graph, Network, Net Info)
  UP / DOWN     -- Scroll within a view
  OK            -- Force refresh
  KEY3          -- Exit
"""

import os
import sys
import time
import subprocess
import threading
from collections import deque

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import rj_env
import rj_theme
from PIL import Image
from payloads._display_helper import ScaledDraw, scaled_font
from payloads._input_helper import get_button

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

VIEWS = ["Dashboard", "CPU Graph", "Network", "Net Info"]
REFRESH_INTERVAL = 2.0
DEBOUNCE = 0.20
SERVICES = ["raspyjack", "raspyjack-device", "raspyjack-webui", "caddy"]

lock = threading.Lock()
_running = True

# Rolling CPU history for graph (60 values)
cpu_history = deque(maxlen=60)


# ---------------------------------------------------------------------------
# System readers
# ---------------------------------------------------------------------------

_prev_cpu = None


def _read_cpu_percent():
    """Compute CPU usage % from /proc/stat delta."""
    global _prev_cpu
    try:
        with open("/proc/stat", "r") as fh:
            parts = fh.readline().split()
        values = [int(v) for v in parts[1:8]]
        idle = values[3] + values[4]
        total = sum(values)
    except (OSError, ValueError, IndexError):
        return 0.0

    if _prev_cpu is None:
        _prev_cpu = (idle, total)
        return 0.0

    prev_idle, prev_total = _prev_cpu
    _prev_cpu = (idle, total)
    d_idle = idle - prev_idle
    d_total = total - prev_total
    if d_total == 0:
        return 0.0
    return round((1.0 - d_idle / d_total) * 100, 1)


def _read_load_avg():
    """Return 1-min load average string."""
    try:
        with open("/proc/loadavg", "r") as fh:
            return fh.read().split()[0]
    except (OSError, IndexError):
        return "?"


def _read_memory():
    """Return (used_mb, total_mb) from /proc/meminfo."""
    info = {}
    try:
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    info[key] = int(parts[1])
    except (OSError, ValueError):
        return (0, 0)
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used = total - avail
    return (used // 1024, total // 1024)


def _read_temperature():
    """Return CPU temperature in Celsius via cross-hardware rj_env reader."""
    try:
        t = rj_env.read_cpu_temp()
        if t is not None:
            return float(t)
    except Exception:
        pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as fh:
            return int(fh.read().strip()) / 1000.0
    except (OSError, ValueError):
        return 0.0


def _read_disk():
    """Return (used_gb, free_gb, total_gb) for /."""
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bfree * st.f_frsize
        used = total - free
        return (round(used / 1e9, 1), round(free / 1e9, 1), round(total / 1e9, 1))
    except OSError:
        return (0.0, 0.0, 0.0)


def _read_uptime():
    """Return uptime string."""
    try:
        with open("/proc/uptime", "r") as fh:
            secs = int(float(fh.read().split()[0]))
        days = secs // 86400
        h = (secs % 86400) // 3600
        m = (secs % 3600) // 60
        return f"{days}d:{h:02d}h:{m:02d}m"
    except (OSError, ValueError):
        return "?"


_prev_net = {}


def _read_net_bytes():
    """Return dict of iface -> (rx_bps, tx_bps)."""
    global _prev_net
    current = {}
    try:
        with open("/proc/net/dev", "r") as fh:
            for line in fh:
                if ":" not in line:
                    continue
                iface, rest = line.split(":", 1)
                iface = iface.strip()
                if iface == "lo":
                    continue
                parts = rest.split()
                if len(parts) >= 9:
                    rx = int(parts[0])
                    tx = int(parts[8])
                    current[iface] = (rx, tx, time.time())
    except (OSError, ValueError):
        pass

    rates = {}
    for iface, (rx, tx, ts) in current.items():
        if iface in _prev_net:
            prx, ptx, pts = _prev_net[iface]
            dt = max(0.1, ts - pts)
            rates[iface] = (
                int((rx - prx) / dt),
                int((tx - ptx) / dt),
            )
        else:
            rates[iface] = (0, 0)
    _prev_net = current
    return rates


def _read_iface_ips():
    """Return list of (iface, ip) for active interfaces."""
    results = []
    try:
        out = subprocess.run(
            ["ip", "-4", "addr", "show"],
            capture_output=True, text=True, timeout=3,
        )
        iface = None
        for line in out.stdout.splitlines():
            stripped = line.strip()
            if not line.startswith(" ") and ":" in stripped:
                parts = stripped.split(":")
                if len(parts) >= 2:
                    iface = parts[1].strip()
            elif stripped.startswith("inet ") and iface and iface != "lo":
                addr = stripped.split()[1].split("/")[0]
                results.append((iface, addr))
    except Exception:
        pass
    return results


def _read_wifi_signal():
    """Return WiFi signal level string from iw dev wlan0 link, or None."""
    try:
        out = subprocess.run(
            ["iw", "dev", "wlan0", "link"],
            capture_output=True, text=True, timeout=3,
        )
        for line in out.stdout.splitlines():
            if "signal:" in line:
                # e.g. "	signal: -52 dBm"
                val = line.strip().split("signal:")[1].strip()
                return val
        return None
    except Exception:
        return None


def _read_services_batch(names):
    """Query all systemd service states in a single subprocess call."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", *names],
            capture_output=True, text=True, timeout=3,
        )
        lines = [ln.strip() for ln in result.stdout.splitlines()]
        return {
            svc: (lines[i] if i < len(lines) and lines[i] else "inactive")
            for i, svc in enumerate(names)
        }
    except Exception:
        return {svc: "error" for svc in names}


class SysState:
    __slots__ = (
        "cpu_pct", "load", "mem_used", "mem_total", "temp",
        "disk_used", "disk_free", "disk_total", "uptime",
        "net_rates", "services", "iface_ips", "wifi_signal",
    )

    def __init__(self):
        self.cpu_pct = 0.0
        self.load = "?"
        self.mem_used = 0
        self.mem_total = 0
        self.temp = 0.0
        self.disk_used = 0.0
        self.disk_free = 0.0
        self.disk_total = 0.0
        self.uptime = "?"
        self.net_rates = {}
        self.services = {}
        self.iface_ips = []
        self.wifi_signal = None


state = SysState()
_state_version = 0


def _refresh():
    """Collect all metrics into a new state snapshot."""
    global state, _state_version
    s = SysState()
    s.cpu_pct = _read_cpu_percent()
    s.load = _read_load_avg()
    s.mem_used, s.mem_total = _read_memory()
    s.temp = _read_temperature()
    s.disk_used, s.disk_free, s.disk_total = _read_disk()
    s.uptime = _read_uptime()
    s.net_rates = _read_net_bytes()
    s.iface_ips = _read_iface_ips()
    s.wifi_signal = _read_wifi_signal()
    s.services = _read_services_batch(SERVICES)
    with lock:
        state = s
        cpu_history.append(s.cpu_pct)
        _state_version += 1


def _auto_refresh():
    while _running:
        _refresh()
        deadline = time.time() + REFRESH_INTERVAL
        while _running and time.time() < deadline:
            time.sleep(0.2)


def _fmt_bytes(b):
    """Format bytes/sec to human-readable."""
    if b < 1024:
        return f"{b}B/s"
    if b < 1024 * 1024:
        return f"{b // 1024}K/s"
    return f"{b // (1024 * 1024)}M/s"


def _temp_color(t):
    if t >= 75:
        return _T["danger"]
    if t >= 60:
        return _T["warn"]
    return _T["ok"]


def _draw_header(d, view_name):
    d.rectangle((0, 0, 127, 14), fill=_T["bg_surface"])
    d.rectangle((0, 0, 2, 14), fill=_T["accent"])
    d.line((0, 14, 127, 14), fill=_T["border_accent"], width=1)
    d.text((6, 1), f"MON: {view_name}", font=font, fill=_T["text_bright"])
    d.text((111, 1), "K3", font=font, fill=_T["text_muted"])


def _draw_footer(d, hint):
    d.rectangle((0, 115, 127, 127), fill=_T["bg_well"])
    d.line((0, 115, 127, 115), fill=_T["border_subtle"], width=1)
    d.text((3, 117), hint, font=font, fill=_T["text_muted"])


def _draw_dashboard(lcd, snap):
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ScaledDraw(img)
    _draw_header(d, "Dashboard")

    # Metrics card
    d.rounded_rectangle((2, 16, 125, 72), radius=4, fill=_T["bg_surface"], outline=_T["border_subtle"])
    y = 19
    d.text((5, y), f"CPU {snap.cpu_pct:.0f}%", font=font, fill=_T["accent_bright"])
    cpu_col = _T["danger"] if snap.cpu_pct > 80 else _T["warn"] if snap.cpu_pct > 55 else _T["accent"]
    rj_theme.draw_progress_bar(d, (58, y + 1, 121, y + 8), snap.cpu_pct / 100.0, bg=_T["bg_well"], fill=cpu_col, outline=_T["border_subtle"], radius=2)
    y += 12

    mem_ratio = (snap.mem_used / snap.mem_total) if snap.mem_total else 0.0
    d.text((5, y), f"RAM {snap.mem_used}/{snap.mem_total}M", font=font, fill=_T["text_primary"])
    y += 11

    tc = _temp_color(snap.temp)
    d.text((5, y), f"Temp {snap.temp:.1f}C", font=font, fill=tc)
    d.text((70, y), f"Ld {snap.load}", font=font, fill=_T["text_muted"])
    y += 11

    d.text((5, y), f"Dsk {snap.disk_used}/{snap.disk_total}G", font=font, fill=_T["text_primary"])
    y += 11
    d.text((5, y), f"Up {snap.uptime}", font=font, fill=_T["text_muted"])

    # Services card
    d.rounded_rectangle((2, 75, 125, 113), radius=4, fill=_T["bg_surface"], outline=_T["border_subtle"])
    sy = 77
    for svc, st in snap.services.items():
        col = _T["ok"] if st == "active" else _T["danger"]
        short = svc.replace("raspyjack-", "rj-")[:12]
        d.ellipse((5, sy + 2, 8, sy + 5), fill=col)
        d.text((11, sy), f"{short}: {st}", font=font, fill=_T["text_primary"] if st == "active" else _T["text_muted"])
        sy += 9
        if sy > 106:
            break

    _draw_footer(d, "L/R:View OK:Ref K3:X")
    lcd.LCD_ShowImage(img, 0, 0)


def _draw_cpu_graph(lcd, history):
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ScaledDraw(img)
    _draw_header(d, "CPU Graph")

    graph_top = 20
    graph_bottom = 100
    graph_h = graph_bottom - graph_top
    graph_left = 22
    graph_right = 123

    d.rounded_rectangle((graph_left - 2, graph_top - 2, graph_right + 2, graph_bottom + 2), radius=4, fill=_T["bg_surface"], outline=_T["border_subtle"])

    for pct in (0, 50, 100):
        y_pos = graph_bottom - int(pct / 100 * graph_h)
        d.text((1, y_pos - 4), f"{pct:>3}", font=font, fill=_T["text_muted"])
        d.line((graph_left, y_pos, graph_right, y_pos), fill=_T["border_subtle"])

    vals = list(history)
    if len(vals) > 1:
        w = graph_right - graph_left
        step = w / max(1, len(vals) - 1)
        points = []
        for i, v in enumerate(vals):
            x = graph_left + int(i * step)
            y = graph_bottom - int(min(v, 100) / 100 * graph_h)
            points.append((x, y))
        for i in range(len(points) - 1):
            d.line([points[i], points[i + 1]], fill=_T["accent_bright"], width=1)

    if vals:
        d.text((4, 104), f"Now: {vals[-1]:.0f}%   Peak: {max(vals):.0f}%", font=font, fill=_T["accent_bright"])

    _draw_footer(d, "L/R:View OK:Ref K3:X")
    lcd.LCD_ShowImage(img, 0, 0)


def _draw_network(lcd, snap, scroll):
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ScaledDraw(img)
    _draw_header(d, "Network")

    ifaces = list(snap.net_rates.items())
    if not ifaces:
        d.text((8, 50), "No interfaces", font=font, fill=_T["text_muted"])
    else:
        y = 17
        visible = 3
        end = min(len(ifaces), scroll + visible)
        for i in range(scroll, end):
            iface, (rx, tx) = ifaces[i]
            d.rounded_rectangle((3, y, 122, y + 29), radius=4, fill=_T["bg_surface"], outline=_T["border_subtle"])
            d.rectangle((3, y + 3, 5, y + 26), fill=_T["accent"])
            d.text((8, y + 2), iface[:14], font=font, fill=_T["accent_bright"])
            d.text((8, y + 12), f"RX {_fmt_bytes(rx)}", font=font, fill=_T["ok"])
            d.text((8, y + 20), f"TX {_fmt_bytes(tx)}", font=font, fill=_T["warn"])
            y += 32
        if len(ifaces) > visible:
            rj_theme.draw_scrollbar(d, 124, 17, 112, scroll, visible, len(ifaces), width=3)

    _draw_footer(d, "L/R:View U/D:Scrl K3:X")
    lcd.LCD_ShowImage(img, 0, 0)


def _draw_net_info(lcd, snap, scroll):
    """Draw Obsidian Ember network interfaces with IPs and WiFi signal."""
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ScaledDraw(img)
    _draw_header(d, "Net Info")

    y = 17
    items = []
    for iface, ip in snap.iface_ips:
        items.append((iface, ip))

    if snap.wifi_signal is not None:
        items.append(("WiFi sig", snap.wifi_signal))

    if not items:
        d.text((8, 50), "No interfaces", font=font, fill=_T["text_muted"])
    else:
        visible = 4
        end = min(len(items), scroll + visible)
        for i in range(scroll, end):
            label, value = items[i]
            d.rounded_rectangle((3, y, 122, y + 21), radius=4, fill=_T["bg_surface"], outline=_T["border_subtle"])
            d.text((7, y + 2), f"{label}", font=font, fill=_T["accent_bright"])
            d.text((7, y + 11), str(value)[:20], font=font, fill=_T["text_bright"])
            y += 24
            if y > 106:
                break
        if len(items) > visible:
            rj_theme.draw_scrollbar(d, 124, 17, 112, scroll, visible, len(items), width=3)

    _draw_footer(d, "L/R:View U/D:Scrl K3:X")
    lcd.LCD_ShowImage(img, 0, 0)


def main():
    global _running

    _refresh()
    t = threading.Thread(target=_auto_refresh, daemon=True)
    t.start()

    view_idx = 0
    scroll = 0
    last_press = 0.0
    dirty = True
    last_ver = -1

    try:
        while True:
            btn = get_button(PINS, GPIO)
            now = time.time()
            if btn and (now - last_press) < DEBOUNCE:
                btn = None
            if btn:
                last_press = now

            if btn == "KEY3":
                break
            elif btn == "RIGHT":
                view_idx = (view_idx + 1) % len(VIEWS)
                scroll = 0
                dirty = True
            elif btn == "LEFT":
                view_idx = (view_idx - 1) % len(VIEWS)
                scroll = 0
                dirty = True
            elif btn == "UP":
                scroll = max(0, scroll - 1)
                dirty = True
            elif btn == "DOWN":
                scroll += 1
                dirty = True
            elif btn == "OK":
                _refresh()
                dirty = True

            with lock:
                cur_ver = _state_version
                if dirty or cur_ver != last_ver:
                    snap = state
                    hist = list(cpu_history)
                    need_draw = True
                    last_ver = cur_ver
                    dirty = False
                else:
                    need_draw = False

            if need_draw:
                view = VIEWS[view_idx]
                if view == "Dashboard":
                    _draw_dashboard(LCD, snap)
                elif view == "CPU Graph":
                    _draw_cpu_graph(LCD, hist)
                elif view == "Network":
                    _draw_network(LCD, snap, scroll)
                elif view == "Net Info":
                    _draw_net_info(LCD, snap, scroll)

            time.sleep(0.05)

    finally:
        _running = False
        try:
            LCD.LCD_Clear()
        except Exception:
            pass
        GPIO.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
