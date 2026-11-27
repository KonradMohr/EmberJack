#!/usr/bin/env python3
"""
Launch and control the vendored Ragnar port from Raspyjack.
"""

import json
import os
import signal
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import rj_theme
from PIL import Image, ImageDraw

from payloads._display_helper import scaled_font
from payloads._input_helper import get_button

_T = rj_theme.EMBER_THEME

ROOT = Path(__file__).resolve().parents[2]
RAGNAR_ROOT = ROOT / "vendor" / "ragnar"
RAGNAR_SHIM = RAGNAR_ROOT / "raspyjack_headless.py"
RAGNAR_LOG_DIR = ROOT / "loot" / "Ragnar"
RAGNAR_LOG_PATH = RAGNAR_LOG_DIR / "ragnar.log"
RAGNAR_UI_LOG_PATH = RAGNAR_LOG_DIR / "ragnar_ui.log"
RAGNAR_PID_PATH = Path("/dev/shm/raspyjack_ragnar.pid")
RAGNAR_PORT = int(os.environ.get("RAGNAR_PORT", "8091"))

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

PAGES = (
    "overview",
    "stats",
    "targets",
    "credentials",
    "attacks",
    "wifi",
    "address",
    "controls",
    "logs",
)
CONTROL_ITEMS = (
    "start_app",
    "stop_app",
    "automation_on",
    "automation_off",
    "manual_on",
    "manual_off",
    "scan_network",
    "scan_vulns",
    "deep_scan",
    "scan_arp",
)
CONTROL_LABELS = {
    "start_app": "Start Ragnar",
    "stop_app": "Stop Ragnar",
    "automation_on": "Automation ON",
    "automation_off": "Automation OFF",
    "manual_on": "Manual ON",
    "manual_off": "Manual OFF",
    "scan_network": "Network Scan",
    "scan_vulns": "Vuln Scan",
    "deep_scan": "Deep Scan",
    "scan_arp": "ARP Sweep",
}

ATTACK_TYPES = ("ssh", "ftp", "smb", "telnet", "rdp", "sql")
ATTACK_LABELS = {
    "ssh": "SSH Brute",
    "ftp": "FTP Brute",
    "smb": "SMB Brute",
    "telnet": "Telnet Brute",
    "rdp": "RDP Brute",
    "sql": "SQL Brute",
}

LCD = None
WIDTH = 128
HEIGHT = 128
FONT = None
SMALL_FONT = None
SPINNER = ("|", "/", "-", "\\")


def _load_logo() -> Image.Image | None:
    candidates = (
        RAGNAR_ROOT / "web" / "images" / "ragnar.png",
        RAGNAR_ROOT / "web" / "images" / "icon-96x96.png",
    )
    for path in candidates:
        try:
            img = Image.open(path).convert("RGBA")
            return img.resize((28, 28))
        except Exception:
            continue
    return None


RAGNAR_LOGO = _load_logo()


def _log_ui_error(exc: Exception) -> None:
    try:
        RAGNAR_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(RAGNAR_UI_LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {exc.__class__.__name__}: {exc}\n")
            handle.write(traceback.format_exc())
            handle.write("\n")
    except Exception:
        pass


def _init_display() -> None:
    global LCD, WIDTH, HEIGHT, FONT, SMALL_FONT
    GPIO.setmode(GPIO.BCM)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    LCD = LCD_1in44.LCD()
    LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    LCD.LCD_Clear()
    WIDTH, HEIGHT = LCD.width, LCD.height
    FONT = scaled_font(10)
    SMALL_FONT = scaled_font(8)


def _read_pid() -> int | None:
    try:
        return int(RAGNAR_PID_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _clear_pid() -> None:
    try:
        RAGNAR_PID_PATH.unlink()
    except FileNotFoundError:
        pass


def _pid_matches_ragnar(pid: int) -> bool:
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        raw = proc_cmdline.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    return "raspyjack_headless.py" in raw


def _running_pid() -> int | None:
    pid = _read_pid()
    if not pid:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        _clear_pid()
        return None
    if not _pid_matches_ragnar(pid):
        _clear_pid()
        return None
    return pid


def _tail_log(lines: int = 5) -> list[str]:
    try:
        data = RAGNAR_LOG_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    return [line.strip()[:26] for line in data[-lines:] if line.strip()]


def _best_ip() -> str:
    try:
        proc = subprocess.run(
            ["ip", "route", "get", "1.1.1.1"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        parts = proc.stdout.split()
        if "src" in parts:
            return parts[parts.index("src") + 1]
    except Exception:
        pass
    return "device-ip"


def _base_url() -> str:
    return f"http://127.0.0.1:{RAGNAR_PORT}"


def _display_url() -> str:
    return f"http://{_best_ip()}:{RAGNAR_PORT}"


def _preflight_ragnar() -> tuple[bool, str]:
    env = os.environ.copy()
    env["RAGNAR_PAGER_MODE"] = "1"  # skip EPD init — conflicts with RaspyJack LCD SPI
    env["PYTHONPATH"] = str(RAGNAR_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import headlessRagnar; print('OK')"],
            cwd=str(RAGNAR_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception as exc:
        return False, f"Preflight failed: {exc}"

    if proc.returncode == 0:
        return True, "OK"

    blob = "\n".join(part for part in (proc.stderr, proc.stdout) if part).strip()
    if "ModuleNotFoundError" in blob and "No module named" in blob:
        marker = "No module named "
        idx = blob.rfind(marker)
        if idx >= 0:
            missing = blob[idx + len(marker):].strip().strip("'\"")
            return False, f"Missing dep: {missing}"
    if blob:
        return False, _shorten(blob.splitlines()[-1], 24)
    return False, "Import preflight failed"


def _api_json(path: str, method: str = "GET", payload: dict | None = None) -> tuple[bool, dict]:
    url = _base_url() + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            parsed = json.loads(raw) if raw else {}
            return True, parsed
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "ignore")
            parsed = json.loads(raw) if raw else {}
        except Exception:
            parsed = {"error": f"http {exc.code}"}
        return False, parsed
    except Exception as exc:
        return False, {"error": str(exc)}


def _stop_ragnar() -> tuple[bool, str]:
    pid = _running_pid()
    if not pid:
        return False, "Already stopped"

    try:
        os.killpg(pid, signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception as exc:
            return False, f"Stop failed: {exc}"

    deadline = time.time() + 8
    while time.time() < deadline:
        if _running_pid() is None:
            _clear_pid()
            return True, "Ragnar stopped"
        time.sleep(0.2)

    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    _clear_pid()
    return True, "Forced stop"


def _start_ragnar() -> tuple[bool, str]:
    if not RAGNAR_SHIM.exists():
        return False, "Vendored Ragnar missing"
    if _running_pid():
        return True, "Already running"

    ready, message = _preflight_ragnar()
    if not ready:
        if message.startswith("Missing dep:"):
            return False, f"{message} install_ragnar_port"
        return False, message

    RAGNAR_LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["RAGNAR_PORT"] = str(RAGNAR_PORT)
    env["RAGNAR_PAGER_MODE"] = "1"  # skip EPD init — conflicts with RaspyJack LCD SPI
    env["PYTHONPATH"] = str(RAGNAR_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    with open(RAGNAR_LOG_PATH, "ab", buffering=0) as log_handle:
        proc = subprocess.Popen(
            [sys.executable, str(RAGNAR_SHIM)],
            cwd=str(RAGNAR_ROOT),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    RAGNAR_PID_PATH.write_text(f"{proc.pid}\n", encoding="utf-8")

    deadline = time.time() + 8
    while time.time() < deadline:
        if _running_pid():
            ok, _status = _api_json("/api/status")
            if ok:
                return True, "Ragnar started"
        time.sleep(0.4)

    details = _tail_log(2)
    if details:
        return False, details[-1][:24]
    return False, "Start failed"


def _short_bool(value: bool) -> str:
    return "ON" if value else "OFF"


def _shorten(text: str, limit: int = 20) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


def _simple_wrap(text: str, width: int = 18, limit: int = 6) -> list[str]:
    words = str(text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if len(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
            if len(lines) >= limit - 1:
                break
    if len(lines) < limit:
        lines.append(current)
    return [line[:width] for line in lines[:limit]]


def _split_url(url: str) -> list[str]:
    if "://" in url:
        scheme, rest = url.split("://", 1)
        lines = [scheme + "://"]
    else:
        rest = url
        lines = []
    while rest:
        chunk = rest[:18]
        lines.append(chunk)
        rest = rest[18:]
    return lines[:3]


# ---------------------------------------------------------------------------
# State fetching — pulls data from Ragnar API
# ---------------------------------------------------------------------------

def _fetch_state() -> dict:
    running = _running_pid() is not None
    state = {
        "running": running,
        "host_url": _display_url(),
        "api_ok": False,
        "manual_mode": False,
        "automation_enabled": False,
        "target_count": 0,
        "port_count": 0,
        "vulnerability_count": 0,
        "credential_count": 0,
        "level": 0,
        "points": 0,
        "data_stolen": 0,
        "current_ssid": "",
        "orchestrator_status": "",
        "ragnar_status": "",
        "ragnar_status2": "",
        "error": "",
        "logs": _tail_log(4),
        "targets": [],
        "credentials": {},
        "wifi_networks": [],
        "wifi_status": {},
    }
    if not running:
        return state

    ok, data = _api_json("/api/status")
    if not ok:
        state["error"] = _shorten(data.get("error", "API unavailable"), 24)
        return state

    state["api_ok"] = True
    state["manual_mode"] = bool(data.get("manual_mode"))
    state["automation_enabled"] = bool(data.get("automation_enabled"))
    state["target_count"] = int(data.get("target_count", 0) or 0)
    state["port_count"] = int(data.get("port_count", 0) or 0)
    state["vulnerability_count"] = int(data.get("vulnerability_count", 0) or 0)
    state["credential_count"] = int(data.get("credential_count", 0) or 0)
    state["current_ssid"] = str(data.get("current_ssid", "") or "")
    state["orchestrator_status"] = str(data.get("orchestrator_status", "") or "")
    state["ragnar_status"] = str(data.get("ragnar_status", "") or "")
    state["ragnar_status2"] = str(data.get("ragnar_status2", "") or "")
    return state


def _fetch_stats() -> dict:
    """Fetch extended stats from /api/stats."""
    ok, data = _api_json("/api/stats")
    if not ok:
        return {}
    return {
        "targets": int(data.get("target_count", 0) or 0),
        "inactive": int(data.get("inactive_target_count", 0) or 0),
        "total_targets": int(data.get("total_target_count", 0) or 0),
        "new_targets": int(data.get("new_target_count", 0) or 0),
        "lost_targets": int(data.get("lost_target_count", 0) or 0),
        "ports": int(data.get("port_count", 0) or 0),
        "vulns": int(data.get("vulnerability_count", 0) or 0),
        "vuln_hosts": int(data.get("vulnerable_hosts_count", 0) or 0),
        "creds": int(data.get("credential_count", 0) or 0),
        "level": int(data.get("level", 0) or 0),
        "points": int(data.get("points", 0) or 0),
        "data_stolen": int(data.get("total_data_stolen", 0) or 0),
    }


def _fetch_targets() -> list[dict]:
    """Fetch target list from /api/manual/targets."""
    ok, data = _api_json("/api/manual/targets")
    if not ok:
        return []
    raw = data.get("targets", [])
    targets = []
    for t in raw:
        targets.append({
            "ip": str(t.get("ip", "") or ""),
            "hostname": str(t.get("hostname", "") or ""),
            "ports": str(t.get("ports", "") or ""),
            "status": str(t.get("status", "") or "alive"),
            "mac": str(t.get("mac", "") or ""),
        })
    return targets


def _fetch_credentials() -> list[dict]:
    """Fetch credentials from /api/credentials, flattened."""
    ok, data = _api_json("/api/credentials")
    if not ok:
        return []
    flat = []
    if isinstance(data, dict):
        for svc, entries in data.items():
            if not isinstance(entries, list):
                continue
            for e in entries:
                flat.append({
                    "service": str(svc),
                    "ip": str(e.get("ip", "") or ""),
                    "username": str(e.get("username", "") or ""),
                    "password": str(e.get("password", "") or ""),
                })
    return flat


def _fetch_wifi_networks() -> list[dict]:
    """Fetch scanned WiFi networks."""
    ok, data = _api_json("/api/wifi/networks")
    if not ok:
        return []
    nets = data if isinstance(data, list) else data.get("networks", [])
    result = []
    for n in nets[:20]:
        result.append({
            "ssid": str(n.get("ssid", "") or ""),
            "signal": int(n.get("signal", 0) or 0),
            "security": str(n.get("security", "") or ""),
            "connected": bool(n.get("connected")),
        })
    return result


def _fetch_wifi_status() -> dict:
    """Fetch current WiFi connection status."""
    ok, data = _api_json("/api/wifi/status")
    if not ok:
        return {}
    return {
        "connected": bool(data.get("connected")),
        "ssid": str(data.get("ssid", "") or ""),
        "signal": str(data.get("signal", "") or ""),
        "ip": str(data.get("ip", "") or ""),
        "interface": str(data.get("interface", "") or ""),
    }


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _run_control(action: str) -> tuple[bool, str]:
    if action == "start_app":
        return _start_ragnar()
    if action == "stop_app":
        return _stop_ragnar()

    if _running_pid() is None:
        return False, "Start Ragnar first"

    mapping = {
        "automation_on": ("/api/automation/orchestrator/start", {}),
        "automation_off": ("/api/automation/orchestrator/stop", {}),
        "manual_on": ("/api/manual/orchestrator/start", {}),
        "manual_off": ("/api/manual/orchestrator/stop", {}),
        "scan_network": ("/api/manual/scan/network", {}),
        "scan_vulns": ("/api/manual/scan/vulnerability", {"ip": "all"}),
        "deep_scan": ("/api/scan/deep", {}),
        "scan_arp": ("/api/scan/arp-localnet", {}),
    }
    entry = mapping.get(action)
    if not entry:
        return False, "Unknown action"
    path, payload = entry
    method = "POST" if payload is not None else "GET"
    if action in ("scan_arp",):
        method = "GET"
    ok, data = _api_json(path, method=method, payload=payload if method == "POST" else None)
    if ok and (data.get("success") is True or data.get("ok") is True or "message" in data):
        return True, _shorten(data.get("message", CONTROL_LABELS[action]), 24)
    return False, _shorten(data.get("error", "Action failed"), 24)


def _run_attack(target_ip: str, target_port: str, attack_type: str) -> tuple[bool, str]:
    """Execute a manual attack on a specific target."""
    if _running_pid() is None:
        return False, "Start Ragnar first"
    ok, data = _api_json(
        "/api/manual/execute-attack",
        method="POST",
        payload={"ip": target_ip, "port": target_port, "action": attack_type},
    )
    if ok and data.get("success"):
        return True, _shorten(data.get("message", "Attack started"), 24)
    return False, _shorten(data.get("error", "Attack failed"), 24)


def _wifi_scan() -> tuple[bool, str]:
    ok, data = _api_json("/api/wifi/scan", method="POST")
    if ok:
        return True, "WiFi scan started"
    return False, _shorten(data.get("error", "Scan failed"), 24)


def _wifi_connect(ssid: str) -> tuple[bool, str]:
    ok, data = _api_json("/api/wifi/connect", method="POST", payload={"ssid": ssid})
    if ok and data.get("success"):
        return True, f"Connecting to {_shorten(ssid, 14)}"
    return False, _shorten(data.get("error", "Connect failed"), 24)


def _wifi_disconnect() -> tuple[bool, str]:
    ok, data = _api_json("/api/wifi/disconnect", method="POST")
    if ok:
        return True, "Disconnected"
    return False, _shorten(data.get("error", "Failed"), 24)


# ---------------------------------------------------------------------------
# Drawing helpers (native resolution-adaptive Obsidian Ember)
# ---------------------------------------------------------------------------

def _ragnar_fonts() -> dict:
    return rj_theme.load_app_fonts(WIDTH, HEIGHT)


def _draw_chrome(draw, title: str, page_idx: int, status_pill: str = "", footer_hint: str = "") -> tuple[int, int]:
    fonts = _ragnar_fonts()
    hdr_h = rj_theme.draw_app_header(
        draw, WIDTH, f"RAGNAR · {title.upper()}" if fonts["wide"] else title.upper(),
        fonts, badge=f"{page_idx + 1}/{len(PAGES)}", right_pill=status_pill,
    )
    hint = footer_hint or ("L/R:Pages  U/D:Nav  OK:Act  K2:Refresh  K3:Exit" if fonts["wide"] else "L/R:Pg K2:Ref K3:X")
    ftr_h = rj_theme.draw_app_footer(draw, WIDTH, HEIGHT, hint, fonts)
    return hdr_h, ftr_h


def _draw_logo(image: Image.Image, x: int, y: int) -> None:
    if RAGNAR_LOGO is None:
        return
    try:
        image.paste(RAGNAR_LOGO, (x, y), RAGNAR_LOGO)
    except Exception:
        pass


def _draw_signal_anim(draw, tick: int, x: int, y: int) -> None:
    heights = (4, 7, 10, 13)
    active = tick % len(heights)
    for idx, base in enumerate(heights):
        h = base if idx <= active else 3
        left = x + idx * 5
        draw.rectangle((left, y + 13 - h, left + 3, y + 13), fill=_T["ok"] if idx <= active else _T["border_subtle"])


def _draw_spinner(draw, tick: int, x: int, y: int) -> None:
    fonts = _ragnar_fonts()
    draw.text((x, y), SPINNER[tick % len(SPINNER)], font=fonts["badge"], fill=_T["accent_bright"])


def _draw_scrollbar(draw, current: int, total: int, y_start: int = 20, y_end: int = 122) -> None:
    """Draw a proportional scrollbar on the right edge."""
    if total <= 1:
        return
    rj_theme.draw_scrollbar(draw, WIDTH - 5, y_start, y_end, current, 1, total, width=3)


# ---------------------------------------------------------------------------
# Page: Overview
# ---------------------------------------------------------------------------

def _draw_overview(image: Image.Image, draw, state: dict, anim_tick: int) -> None:
    fonts = _ragnar_fonts()
    wide, large = fonts["wide"], fonts["large"]
    hdr_h, ftr_h = _draw_chrome(draw, "Overview", 0, status_pill="RUNNING" if state["running"] else "STOPPED")
    pad = 10 if large else (5 if wide else 3)
    y0, y1 = hdr_h + pad, HEIGHT - ftr_h - pad

    if WIDTH >= 480:
        kpi_h = 64
        gap = 8
        tw = (WIDTH - pad * 2 - gap * 3) // 4
        kpis = [
            ("SERVICE", "RUN" if state["running"] else "STOP", _T["ok"] if state["running"] else _T["danger"]),
            ("TARGETS", str(state["target_count"]), _T["accent_bright"]),
            ("OPEN PORTS", str(state["port_count"]), _T["warn"]),
            ("VULNS / CREDS", f"{state['vulnerability_count']} / {state['credential_count']}", _T["danger"] if state["vulnerability_count"] else _T["ok"]),
        ]
        for idx_k, (lbl, val, acc) in enumerate(kpis):
            kx = pad + idx_k * (tw + gap)
            rj_theme.draw_card(draw, (kx, y0, kx + tw, y0 + kpi_h), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=6, accent_left=acc, accent_width=4)
            draw.text((kx + 12, y0 + 10), lbl, font=fonts["badge"], fill=_T["text_muted"])
            draw.text((kx + 12, y0 + 30), val, font=fonts["title"], fill=acc)

        by0 = y0 + kpi_h + 10
        rj_theme.draw_card(draw, (pad, by0, WIDTH - pad, y1), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=8, accent_left=_T["accent"], accent_width=4)
        _draw_logo(image, WIDTH - pad - 42, by0 + 12)
        _draw_signal_anim(draw, anim_tick, WIDTH - pad - 38, by0 + 48)
        lx = pad + 14
        ly = by0 + 12
        lmw = WIDTH - pad * 2 - 76
        rows = [
            ("API Health", "Connected" if state["api_ok"] else "Offline", _T["ok"] if state["api_ok"] else _T["text_muted"]),
            ("Automation / Manual", f"Auto: {_short_bool(state['automation_enabled'])}   |   Manual: {_short_bool(state['manual_mode'])}", _T["accent_bright"]),
            ("WiFi SSID", state["current_ssid"] or "-", _T["text_primary"]),
            ("Orchestrator", state["orchestrator_status"] or "IDLE", _T["warn"]),
            ("Status", state["ragnar_status2"] or state["error"] or "Use LEFT/RIGHT to switch views", _T["text_muted"]),
        ]
        for k, v, col in rows:
            draw.text((lx, ly), f"{k}:", font=fonts["sub"], fill=_T["text_muted"])
            draw.text((lx + 128, ly), rj_theme.truncate_text(draw, v, max(60, lmw - 128), fonts["body_bold"]), font=fonts["body_bold"], fill=col)
            ly += 24
    else:
        rj_theme.draw_card(draw, (pad, y0, WIDTH - pad, y1), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=5 if wide else 3, accent_left=_T["ok"] if state["running"] else _T["accent"], accent_width=3 if wide else 2)
        if wide:
            _draw_logo(image, WIDTH - pad - 36, y0 + 6)
            _draw_signal_anim(draw, anim_tick, WIDTH - pad - 32, y0 + 40)
        tx = pad + (10 if wide else 5)
        mw = WIDTH - pad - tx - (42 if wide else 4)
        y = y0 + (6 if wide else 3)
        step = 16 if wide else (15 if HEIGHT >= 200 else 10)
        lines = [
            (f"State: {'RUN' if state['running'] else 'STOP'}  API: {'OK' if state['api_ok'] else '--'}", _T["ok"] if state["running"] else _T["danger"]),
            (f"Auto:{_short_bool(state['automation_enabled'])} Man:{_short_bool(state['manual_mode'])}", _T["accent_bright"]),
            (f"T:{state['target_count']} P:{state['port_count']} V:{state['vulnerability_count']} C:{state['credential_count']}", _T["text_bright"]),
            (f"SSID: {state['current_ssid'] or '-'}", _T["text_primary"]),
            (f"Orch: {state['orchestrator_status'] or 'IDLE'}", _T["warn"]),
            (state["ragnar_status2"] or state["error"] or "L/R pages", _T["text_muted"]),
        ]
        for line, col in lines:
            if y + 9 > y1:
                break
            draw.text((tx, y), rj_theme.truncate_text(draw, line, mw, fonts["sub"]), font=fonts["sub"], fill=col)
            y += step


# ---------------------------------------------------------------------------
# Page: Stats (extended metrics)
# ---------------------------------------------------------------------------

def _draw_stats(image: Image.Image, draw, stats: dict, anim_tick: int) -> None:
    fonts = _ragnar_fonts()
    wide, large = fonts["wide"], fonts["large"]
    hdr_h, ftr_h = _draw_chrome(draw, "Stats", 1, status_pill=f"LVL {stats.get('level', 0)}" if stats else "")
    pad = 10 if large else (5 if wide else 3)
    y0, y1 = hdr_h + pad, HEIGHT - ftr_h - pad
    if not stats:
        rj_theme.draw_status_card(draw, WIDTH, HEIGHT, "No Stats Yet", sub="Start Ragnar in Controls page", kind="info", fonts=fonts)
        return

    if WIDTH >= 480:
        gap = 8
        cw = (WIDTH - pad * 2 - gap * 2) // 3
        ch = (y1 - y0 - gap) // 2
        tiles = [
            ("ACTIVE TARGETS", str(stats.get("targets", 0)), f"New:{stats.get('new_targets', 0)} Lost:{stats.get('lost_targets', 0)}", _T["ok"]),
            ("OPEN PORTS", str(stats.get("ports", 0)), f"Total seen: {stats.get('total_targets', 0)}", _T["accent_bright"]),
            ("VULNERABILITIES", str(stats.get("vulns", 0)), f"Across {stats.get('vuln_hosts', 0)} hosts", _T["danger"]),
            ("CREDENTIALS", str(stats.get("creds", 0)), "Harvested accounts", _T["warn"]),
            ("DATA STOLEN", f"{stats.get('data_stolen', 0)} files", "Exfiltrated loot", _T["accent"]),
            ("SCORE / LEVEL", f"Lvl {stats.get('level', 0)}", f"{stats.get('points', 0):,} points", _T["ok"]),
        ]
        for idx_t, (lbl, val, sub, acc) in enumerate(tiles):
            tx = pad + (idx_t % 3) * (cw + gap)
            ty = y0 + (idx_t // 3) * (ch + gap)
            rj_theme.draw_card(draw, (tx, ty, tx + cw, ty + ch), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=6, accent_left=acc, accent_width=4)
            draw.text((tx + 12, ty + 10), lbl, font=fonts["badge"], fill=_T["text_muted"])
            draw.text((tx + 12, ty + 32), val, font=fonts["title"], fill=acc)
            draw.text((tx + 12, ty + 58), rj_theme.truncate_text(draw, sub, cw - 18, fonts["sub"]), font=fonts["sub"], fill=_T["text_muted"])
    else:
        rj_theme.draw_card(draw, (pad, y0, WIDTH - pad, y1), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=5 if wide else 3)
        tx = pad + (8 if wide else 4)
        mw = WIDTH - pad - tx - 4
        y = y0 + (5 if wide else 3)
        step = 15 if wide else 11
        for line, col in [
            (f"Targets: {stats.get('targets', 0)} (+{stats.get('new_targets', 0)}/-{stats.get('lost_targets', 0)})", _T["ok"]),
            (f"Ports: {stats.get('ports', 0)}   Vulns: {stats.get('vulns', 0)}", _T["accent_bright"]),
            (f"Creds: {stats.get('creds', 0)}   Files: {stats.get('data_stolen', 0)}", _T["warn"]),
            (f"Level: {stats.get('level', 0)}   Pts: {stats.get('points', 0)}", _T["text_primary"]),
        ]:
            if y + 9 > y1:
                break
            draw.text((tx, y), rj_theme.truncate_text(draw, line, mw, fonts["sub"]), font=fonts["sub"], fill=col)
            y += step


# ---------------------------------------------------------------------------
# Page: Targets (scrollable host list)
# ---------------------------------------------------------------------------

def _draw_targets(image: Image.Image, draw, targets: list, scroll_idx: int, anim_tick: int) -> None:
    fonts = _ragnar_fonts()
    wide, large = fonts["wide"], fonts["large"]
    hdr_h, ftr_h = _draw_chrome(draw, "Targets", 2, status_pill=f"{len(targets)} HOSTS", footer_hint="Up/Dn:Select  L/R:Pages  K2:Refresh  K3:Exit" if wide else "U/D:Sel L/R:Pg K3:X")
    pad = 8 if large else (4 if wide else 2)
    y0, y1 = hdr_h + pad, HEIGHT - ftr_h - pad

    if not targets:
        rj_theme.draw_status_card(draw, WIDTH, HEIGHT, "No Targets Discovered", sub="Run a Network or ARP scan in Controls", kind="info", fonts=fonts)
        return

    split = WIDTH >= 480
    lx1 = int(WIDTH * 0.58) - 4 if split else WIDTH - pad - 5
    det_h = 0 if split else (22 if wide else 14)
    list_bot = y1 - det_h - (4 if det_h else 0)
    row_h = 26 if large else (19 if wide else 12)
    vis = max(1, (list_bot - y0) // row_h)
    start = max(0, min(scroll_idx - vis // 2, max(0, len(targets) - vis)))

    for i, idx in enumerate(range(start, min(len(targets), start + vis))):
        t = targets[idx]
        sel = idx == scroll_idx
        y = y0 + i * row_h
        if sel:
            rj_theme.draw_card(draw, (pad, y, lx1, y + row_h - 2), fill=_T["select_bg"], outline=_T["border_accent"], radius=4 if wide else 2, accent_left=_T["accent"], accent_width=3 if wide else 2)
        elif i % 2 == 1 and wide:
            draw.rectangle((pad, y, lx1, y + row_h - 2), fill=_T["bg_surface"])
        ports = t.get("ports", "")
        port_cnt = len([p for p in ports.split(",") if p.strip()]) if ports else 0
        ty = y + max(1, (row_h - (13 if large else 9)) // 2)
        rx = lx1 - (8 if wide else 3)
        if port_cnt:
            pt = f"{port_cnt}p"
            pw = rj_theme.measure_text(fonts["badge"], pt, draw)[2] + (8 if wide else 2)
            if wide:
                rj_theme.draw_pill(draw, rx - pw, y + max(1, (row_h - 15) // 2), pt, fonts["badge"], bg=_T["ok_bg"], fg=_T["ok"], outline=None, pad_x=4, pad_y=1)
            else:
                draw.text((rx - pw, ty), pt, font=fonts["sub"], fill=_T["ok"])
            rx -= pw + 6
        tx = pad + (8 if wide else 3)
        lbl = f"{t['ip']}  {t.get('hostname', '')}".strip() if wide else t["ip"]
        draw.text((tx, ty), rj_theme.truncate_text(draw, lbl, max(20, rx - tx - 2), fonts["body_bold"] if (sel and wide) else fonts["sub"]), font=fonts["body_bold"] if (sel and wide) else fonts["sub"], fill=_T["text_bright"] if sel else _T["text_primary"])

    if len(targets) > vis:
        rj_theme.draw_scrollbar(draw, lx1 + 2 if split else WIDTH - 4, y0, list_bot, start, vis, len(targets), width=3)

    if 0 <= scroll_idx < len(targets):
        t = targets[scroll_idx]
        if split:
            px0 = lx1 + 10
            rj_theme.draw_card(draw, (px0, y0, WIDTH - pad, y1), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=8, accent_left=_T["ok"], accent_width=4)
            dx, dy = px0 + 12, y0 + 12
            dmw = WIDTH - pad - dx - 8
            draw.text((dx, dy), "HOST INSPECTOR", font=fonts["badge"], fill=_T["accent_bright"])
            dy += 20
            draw.text((dx, dy), t["ip"], font=fonts["title"], fill=_T["text_bright"])
            dy += 22
            draw.text((dx, dy), rj_theme.truncate_text(draw, t.get("hostname") or "(no hostname)", dmw, fonts["body"]), font=fonts["body"], fill=_T["warn"])
            dy += 22
            draw.text((dx, dy), f"MAC: {t.get('mac') or '-'}", font=fonts["mono"], fill=_T["text_muted"])
            dy += 22
            draw.text((dx, dy), f"Status: {t.get('status') or 'alive'}", font=fonts["sub"], fill=_T["ok"])
            dy += 22
            draw.text((dx, dy), "OPEN PORTS:", font=fonts["badge"], fill=_T["text_muted"])
            dy += 16
            for pln in rj_theme.wrap_text(draw, t.get("ports") or "None discovered", dmw, fonts["mono"])[:4]:
                draw.text((dx, dy), pln, font=fonts["mono"], fill=_T["accent_bright"])
                dy += 16
        else:
            info = t.get("hostname") or (f"P:{t.get('ports')}" if t.get("ports") else "No ports")
            draw.line([(pad, list_bot + 2), (WIDTH - pad, list_bot + 2)], fill=_T["border_subtle"])
            draw.text((pad + 3, list_bot + 4), rj_theme.truncate_text(draw, info, WIDTH - pad * 2 - 6, fonts["sub"]), font=fonts["sub"], fill=_T["warn"])


# ---------------------------------------------------------------------------
# Page: Credentials
# ---------------------------------------------------------------------------

def _draw_credentials(image: Image.Image, draw, creds: list, scroll_idx: int, anim_tick: int) -> None:
    fonts = _ragnar_fonts()
    wide, large = fonts["wide"], fonts["large"]
    hdr_h, ftr_h = _draw_chrome(draw, "Credentials", 3, status_pill=f"{len(creds)} FOUND", footer_hint="Up/Dn:Scroll  L/R:Pages  K2:Refresh  K3:Exit" if wide else "U/D:Scrl L/R:Pg")
    pad = 8 if large else (4 if wide else 2)
    y0, y1 = hdr_h + pad, HEIGHT - ftr_h - pad
    if not creds:
        rj_theme.draw_status_card(draw, WIDTH, HEIGHT, "No Credentials Yet", sub="Run brute-force modules on Attacks page", kind="info", fonts=fonts)
        return
    card_h = 46 if large else (32 if wide else 22)
    vis = max(1, (y1 - y0) // card_h)
    start = max(0, min(scroll_idx - vis // 2, max(0, len(creds) - vis)))
    for i, idx in enumerate(range(start, min(len(creds), start + vis))):
        c = creds[idx]
        sel = idx == scroll_idx
        y = y0 + i * card_h
        rj_theme.draw_card(draw, (pad, y, WIDTH - pad - 5, y + card_h - 3), fill=_T["select_bg"] if sel else _T["bg_surface"], outline=_T["border_accent"] if sel else _T["border_subtle"], radius=5 if wide else 2, accent_left=_T["warn"] if sel else _T["accent_dim"], accent_width=3 if wide else 2)
        tx = pad + (10 if wide else 4)
        mw = WIDTH - pad * 2 - 18
        svc = c.get("service", "?").upper()
        ip = c.get("ip", "")
        u = c.get("username", "?")
        p = c.get("password", "?")
        if wide:
            pw, _ = rj_theme.draw_pill(draw, tx, y + (5 if large else 2), svc[:6], fonts["badge"], bg=_T["warn_bg"], fg=_T["warn"], outline=None, pad_x=5, pad_y=1)
            draw.text((tx + pw + 8, y + (5 if large else 2)), ip, font=fonts["body_bold"], fill=_T["text_bright"])
            draw.text((tx, y + (25 if large else 17)), rj_theme.truncate_text(draw, f"{u} : {p}", mw, fonts["mono"]), font=fonts["mono"], fill=_T["ok"])
        else:
            draw.text((tx, y + 1), rj_theme.truncate_text(draw, f"[{svc[:4]}] {ip}", mw, fonts["sub"]), font=fonts["sub"], fill=_T["accent_bright"])
            draw.text((tx, y + 10), rj_theme.truncate_text(draw, f"{u}:{p}", mw, fonts["sub"]), font=fonts["sub"], fill=_T["warn"])
    if len(creds) > vis:
        rj_theme.draw_scrollbar(draw, WIDTH - 4, y0, y1, start, vis, len(creds), width=3)


# ---------------------------------------------------------------------------
# Page: Attacks (select target + attack type, execute)
# ---------------------------------------------------------------------------

def _draw_attacks(image: Image.Image, draw, targets: list, target_idx: int,
                  attack_idx: int, notice: str | None, anim_tick: int) -> None:
    fonts = _ragnar_fonts()
    wide, large = fonts["wide"], fonts["large"]
    hdr_h, ftr_h = _draw_chrome(draw, "Attacks", 4, status_pill=ATTACK_TYPES[attack_idx].upper(), footer_hint="U/D:Target  K1:Attack Type  OK:Launch  L/R:Pages" if wide else "U/D:Tgt K1:Atk OK:Go")
    pad = 8 if large else (4 if wide else 2)
    y0, y1 = hdr_h + pad, HEIGHT - ftr_h - pad
    if not targets:
        rj_theme.draw_status_card(draw, WIDTH, HEIGHT, "No Targets Available", sub="Run a network scan first", kind="info", fonts=fonts)
        return
    t = targets[target_idx] if 0 <= target_idx < len(targets) else {}
    ip = t.get("ip", "?")
    ports = t.get("ports", "") or "none"

    if WIDTH >= 480:
        mid = int(WIDTH * 0.48)
        rj_theme.draw_card(draw, (pad, y0, mid - 5, y1), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=8, accent_left=_T["danger"], accent_width=4)
        lx, ly = pad + 14, y0 + 12
        lmw = (mid - 5) - lx - 10
        draw.text((lx, ly), f"SELECTED TARGET ({target_idx + 1}/{len(targets)})", font=fonts["badge"], fill=_T["danger"])
        ly += 24
        draw.text((lx, ly), ip, font=fonts["title"], fill=_T["text_bright"])
        ly += 24
        draw.text((lx, ly), rj_theme.truncate_text(draw, t.get("hostname") or "(no hostname)", lmw, fonts["body"]), font=fonts["body"], fill=_T["text_muted"])
        ly += 24
        draw.text((lx, ly), "PORTS:", font=fonts["badge"], fill=_T["text_muted"])
        ly += 16
        draw.text((lx, ly), rj_theme.truncate_text(draw, ports, lmw, fonts["mono"]), font=fonts["mono"], fill=_T["accent_bright"])
        if notice:
            rj_theme.draw_pill(draw, lx, y1 - 30, rj_theme.truncate_text(draw, notice, lmw - 10, fonts["badge"]), fonts["badge"], bg=_T["warn_bg"], fg=_T["warn"], outline=_T["warn"], pad_x=6, pad_y=2)

        rx0 = mid + 5
        rj_theme.draw_card(draw, (rx0, y0, WIDTH - pad, y1), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=8, accent_left=_T["accent"], accent_width=4)
        rx, ry = rx0 + 12, y0 + 12
        draw.text((rx, ry), "MODULE (KEY1 TO CYCLE)", font=fonts["badge"], fill=_T["accent_bright"])
        ry += 22
        for idx, atype in enumerate(ATTACK_TYPES):
            sel = idx == attack_idx
            if sel:
                rj_theme.draw_card(draw, (rx, ry, WIDTH - pad - 10, ry + 26), fill=_T["select_bg"], outline=_T["border_accent"], radius=4, accent_left=_T["danger"], accent_width=3)
            draw.text((rx + 10, ry + 5), ATTACK_LABELS.get(atype, atype), font=fonts["body_bold"] if sel else fonts["body"], fill=_T["text_bright"] if sel else _T["text_muted"])
            ry += 30
    else:
        y = y0
        draw.text((pad + 3, y), rj_theme.truncate_text(draw, f"Tgt: {ip}  ({ports})", WIDTH - pad * 2 - 6, fonts["body_bold"] if wide else fonts["sub"]), font=fonts["body_bold"] if wide else fonts["sub"], fill=_T["ok"])
        y += 16 if wide else 11
        row_h = 16 if wide else 11
        for idx, atype in enumerate(ATTACK_TYPES):
            if y + row_h > y1 - (14 if notice else 0):
                break
            sel = idx == attack_idx
            if sel:
                draw.rectangle((pad, y, WIDTH - pad, y + row_h - 1), fill=_T["select_bg"])
            draw.text((pad + 4, y + 1), f"{'>' if sel else ' '} {ATTACK_LABELS.get(atype, atype)}", font=fonts["sub"], fill=_T["accent_bright"] if sel else _T["text_primary"])
            y += row_h
        if notice:
            draw.text((pad + 3, y1 - 12), rj_theme.truncate_text(draw, notice, WIDTH - pad * 2 - 6, fonts["sub"]), font=fonts["sub"], fill=_T["warn"])


# ---------------------------------------------------------------------------
# Page: WiFi
# ---------------------------------------------------------------------------

def _draw_wifi(image: Image.Image, draw, wifi_status: dict, wifi_nets: list,
               wifi_idx: int, notice: str | None, anim_tick: int) -> None:
    fonts = _ragnar_fonts()
    wide, large = fonts["wide"], fonts["large"]
    conn_lbl = wifi_status.get("ssid", "") if wifi_status.get("connected") else "OFFLINE"
    hdr_h, ftr_h = _draw_chrome(draw, "WiFi", 5, status_pill=conn_lbl[:12], footer_hint="K1:Scan  OK:Join  K2:Disconnect  U/D:Nav  L/R:Pages" if wide else "K1:Scn OK:Join K2:Off")
    pad = 8 if large else (4 if wide else 2)
    y0, y1 = hdr_h + pad, HEIGHT - ftr_h - pad

    st_h = 26 if large else (18 if wide else 12)
    rj_theme.draw_card(draw, (pad, y0, WIDTH - pad, y0 + st_h), fill=_T["bg_surface"], outline=_T["border_subtle"], radius=4 if wide else 2, accent_left=_T["ok"] if wifi_status.get("connected") else _T["warn"], accent_width=3 if wide else 2)
    st_txt = f"Connected: {wifi_status.get('ssid', '?')}  ({wifi_status.get('ip', '?')})" if wifi_status.get("connected") else (notice or "Not connected — press K1 to scan")
    draw.text((pad + (10 if wide else 4), y0 + (5 if large else 2)), rj_theme.truncate_text(draw, st_txt, WIDTH - pad * 2 - 14, fonts["body"] if wide else fonts["sub"]), font=fonts["body"] if wide else fonts["sub"], fill=_T["ok"] if wifi_status.get("connected") else _T["warn"])
    y = y0 + st_h + (6 if wide else 3)

    if not wifi_nets:
        draw.text((WIDTH // 2, (y + y1) // 2), "No networks scanned (press K1)", font=fonts["body"] if wide else fonts["sub"], fill=_T["text_dim"], anchor="mm")
        return

    row_h = 26 if large else (19 if wide else 11)
    vis = max(1, (y1 - y) // row_h)
    start = max(0, min(wifi_idx - vis // 2, max(0, len(wifi_nets) - vis)))
    for i, idx in enumerate(range(start, min(len(wifi_nets), start + vis))):
        n = wifi_nets[idx]
        sel = idx == wifi_idx
        ry = y + i * row_h
        if sel:
            rj_theme.draw_card(draw, (pad, ry, WIDTH - pad - 5, ry + row_h - 2), fill=_T["select_bg"], outline=_T["border_accent"], radius=4 if wide else 2, accent_left=_T["accent"], accent_width=3 if wide else 2)
        ty = ry + max(1, (row_h - (13 if large else 9)) // 2)
        sig = n.get("signal", 0)
        sig_s = f"{sig}dB"
        sw = rj_theme.measure_text(fonts["sub"], sig_s, draw)[2]
        rx = WIDTH - pad - 10
        draw.text((rx - sw, ty), sig_s, font=fonts["sub"], fill=_T["text_muted"])
        rx -= sw + 8
        if wide and n.get("security"):
            pw, _ = rj_theme.draw_pill(draw, rx - 52, ry + max(1, (row_h - 15) // 2), n["security"][:7], fonts["badge"], bg=_T["bg_elevated"], fg=_T["accent_bright"], outline=None, pad_x=4, pad_y=1)
            rx -= pw + 8
        prefix = "* " if n.get("connected") else ""
        draw.text((pad + (8 if wide else 3), ty), rj_theme.truncate_text(draw, f"{prefix}{n.get('ssid', '?')}", max(20, rx - pad - 12), fonts["body_bold"] if (sel and wide) else fonts["sub"]), font=fonts["body_bold"] if (sel and wide) else fonts["sub"], fill=_T["ok"] if n.get("connected") else (_T["text_bright"] if sel else _T["text_primary"]))
    if len(wifi_nets) > vis:
        rj_theme.draw_scrollbar(draw, WIDTH - 4, y, y1, start, vis, len(wifi_nets), width=3)


# ---------------------------------------------------------------------------
# Page: Address
# ---------------------------------------------------------------------------

def _draw_address(image: Image.Image, draw, state: dict, anim_tick: int) -> None:
    fonts = _ragnar_fonts()
    wide, large = fonts["wide"], fonts["large"]
    hdr_h, ftr_h = _draw_chrome(draw, "WebUI", 6, status_pill=f":{RAGNAR_PORT}", footer_hint="OK / K2:Refresh  L/R:Pages  K3:Exit" if wide else "OK:Ref L/R:Pg")
    pad = 10 if large else (5 if wide else 3)
    y0, y1 = hdr_h + pad, HEIGHT - ftr_h - pad
    rj_theme.draw_card(draw, (pad, y0, WIDTH - pad, y1), fill=_T["bg_surface"], outline=_T["border_accent"], radius=8 if wide else 4, accent_left=_T["accent"], accent_width=4 if wide else 2)
    if wide:
        _draw_logo(image, WIDTH - pad - 42, y0 + 12)
    tx = pad + (16 if large else (10 if wide else 5))
    mw = WIDTH - pad - tx - (46 if wide else 6)
    y = y0 + (18 if large else (10 if wide else 6))
    draw.text((tx, y), "RAGNAR WEB INTERFACE", font=fonts["badge"], fill=_T["accent_bright"])
    y += 24 if large else 14
    if wide:
        draw.text((tx, y), rj_theme.truncate_text(draw, state["host_url"], mw, fonts["title"]), font=fonts["title"], fill=_T["ok"])
        y += 32 if large else 20
    else:
        for line in _split_url(state["host_url"]):
            draw.text((tx, y), line, font=fonts["sub"], fill=_T["ok"])
            y += 11
    y += 4
    draw.text((tx, y), rj_theme.truncate_text(draw, "Port kept separate from EmberJack UI.", mw, fonts["body"] if wide else fonts["sub"]), font=fonts["body"] if wide else fonts["sub"], fill=_T["text_primary"])
    y += 22 if large else 14
    if y + 12 <= y1:
        draw.text((tx, y), "Open in browser on same LAN.", font=fonts["sub"], fill=_T["text_muted"])


# ---------------------------------------------------------------------------
# Page: Controls
# ---------------------------------------------------------------------------

def _draw_controls(image: Image.Image, draw, control_idx: int, notice: str | None, anim_tick: int) -> None:
    fonts = _ragnar_fonts()
    wide, large = fonts["wide"], fonts["large"]
    hdr_h, ftr_h = _draw_chrome(draw, "Controls", 7, status_pill=f"{control_idx + 1}/{len(CONTROL_ITEMS)}", footer_hint="Up/Dn:Pick  OK/K1:Execute  L/R:Pages  K3:Exit" if wide else "U/D:Pick OK:Run")
    pad = 8 if large else (4 if wide else 2)
    y0, y1 = hdr_h + pad, HEIGHT - ftr_h - pad
    bot = y1 - (26 if (notice and wide) else (14 if notice else 0))

    row_h = 28 if large else (20 if wide else 13)
    vis = max(1, (bot - y0) // row_h)
    start = max(0, min(control_idx - vis // 2, max(0, len(CONTROL_ITEMS) - vis)))
    for i, idx in enumerate(range(start, min(len(CONTROL_ITEMS), start + vis))):
        key = CONTROL_ITEMS[idx]
        sel = idx == control_idx
        y = y0 + i * row_h
        if sel:
            rj_theme.draw_card(draw, (pad, y, WIDTH - pad - 5, y + row_h - 2), fill=_T["select_bg"], outline=_T["border_accent"], radius=4 if wide else 2, accent_left=_T["accent"], accent_width=3 if wide else 2)
        elif i % 2 == 1 and wide:
            draw.rectangle((pad, y, WIDTH - pad - 5, y + row_h - 2), fill=_T["bg_surface"])
        ty = y + max(1, (row_h - (14 if large else 9)) // 2)
        draw.text((pad + (10 if wide else 4), ty), CONTROL_LABELS[key], font=fonts["body_bold"] if (sel and wide) else (fonts["body"] if wide else fonts["sub"]), fill=_T["accent_bright"] if sel else _T["text_primary"])
        if sel and wide:
            pw = rj_theme.measure_text(fonts["badge"], "OK TO RUN", draw)[2] + 10
            rj_theme.draw_pill(draw, WIDTH - pad - pw - 10, y + max(1, (row_h - 15) // 2), "OK TO RUN", fonts["badge"], bg=_T["ok_bg"], fg=_T["ok"], outline=None, pad_x=5, pad_y=1)
    if len(CONTROL_ITEMS) > vis:
        rj_theme.draw_scrollbar(draw, WIDTH - 4, y0, bot, start, vis, len(CONTROL_ITEMS), width=3)
    if notice:
        draw.text((pad + 4, bot + 4), rj_theme.truncate_text(draw, notice, WIDTH - pad * 2 - 8, fonts["body_bold"] if wide else fonts["sub"]), font=fonts["body_bold"] if wide else fonts["sub"], fill=_T["ok"])


# ---------------------------------------------------------------------------
# Page: Logs
# ---------------------------------------------------------------------------

def _draw_logs(image: Image.Image, draw, state: dict, anim_tick: int) -> None:
    fonts = _ragnar_fonts()
    wide, large = fonts["wide"], fonts["large"]
    hdr_h, ftr_h = _draw_chrome(draw, "Logs", 8, status_pill="TAIL", footer_hint="OK/K2:Refresh  L/R:Pages  K3:Exit" if wide else "K2:Ref L/R:Pg")
    pad = 8 if large else (4 if wide else 2)
    y0, y1 = hdr_h + pad, HEIGHT - ftr_h - pad
    rj_theme.draw_card(draw, (pad, y0, WIDTH - pad, y1), fill=_T["bg_well"], outline=_T["border_subtle"], radius=5 if wide else 2)
    logs = state.get("logs") or [state.get("error") or "No Ragnar logs yet"]
    row_h = 18 if large else (13 if wide else 10)
    max_lines = max(1, (y1 - y0 - 8) // row_h)
    y = y0 + 5
    for line in logs[-max_lines:]:
        draw.text((pad + (8 if wide else 3), y), rj_theme.truncate_text(draw, line, WIDTH - pad * 2 - 12, fonts["mono"] if wide else fonts["sub"]), font=fonts["mono"] if wide else fonts["sub"], fill=_T["text_primary"])
        y += row_h


# ---------------------------------------------------------------------------
# Screen dispatch
# ---------------------------------------------------------------------------

def _draw_screen(page: str, state: dict, control_idx: int, notice: str | None = None,
                 anim_tick: int = 0, extra: dict | None = None) -> None:
    extra = extra or {}
    image = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    draw = ImageDraw.Draw(image)
    if page == "overview":
        _draw_overview(image, draw, state, anim_tick)
    elif page == "stats":
        _draw_stats(image, draw, extra.get("stats", {}), anim_tick)
    elif page == "targets":
        _draw_targets(image, draw, extra.get("targets", []), extra.get("target_scroll", 0), anim_tick)
    elif page == "credentials":
        _draw_credentials(image, draw, extra.get("creds", []), extra.get("cred_scroll", 0), anim_tick)
    elif page == "attacks":
        _draw_attacks(image, draw, extra.get("targets", []), extra.get("attack_target_idx", 0),
                      extra.get("attack_type_idx", 0), notice, anim_tick)
    elif page == "wifi":
        _draw_wifi(image, draw, extra.get("wifi_status", {}), extra.get("wifi_nets", []),
                   extra.get("wifi_idx", 0), notice, anim_tick)
    elif page == "address":
        _draw_address(image, draw, state, anim_tick)
    elif page == "controls":
        _draw_controls(image, draw, control_idx, notice, anim_tick)
    else:
        _draw_logs(image, draw, state, anim_tick)
    LCD.LCD_ShowImage(image, 0, 0)


def _draw_message_screen(title: str, lines: list[str], footer: str | None = None) -> None:
    fonts = _ragnar_fonts()
    image = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    draw = ImageDraw.Draw(image)
    rj_theme.draw_app_header(draw, WIDTH, title.upper(), fonts, badge="RAGNAR")
    sub = " ".join(lines) if lines else ""
    kind = "err" if "error" in title.lower() else "info"
    rj_theme.draw_status_card(draw, WIDTH, HEIGHT, title, sub=sub, kind=kind, foot=footer or "", fonts=fonts)
    if footer:
        rj_theme.draw_app_footer(draw, WIDTH, HEIGHT, footer, fonts)
    LCD.LCD_ShowImage(image, 0, 0)


def _show_fatal_screen(exc: Exception) -> None:
    _log_ui_error(exc)
    # Truncate error details to avoid leaking sensitive info on-screen
    sanitized = f"{exc.__class__.__name__}: {str(exc)[:50]}"
    lines = _simple_wrap(sanitized, width=18, limit=6)
    if not lines:
        lines = ["Unknown Ragnar UI", "error"]
    _draw_message_screen("Ragnar Error", lines, "KEY3 exit")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    _init_display()
    _draw_message_screen("Ragnar", ["Loading UI...", "Please wait"], "KEY3 exit")
    page_idx = 0
    control_idx = 0
    notice = "LT/RT pages"
    notice_until = time.monotonic() + 3
    last_render_key = None
    last_refresh = 0.0
    state = _fetch_state()

    # Per-page scroll/selection indices
    target_scroll = 0
    cred_scroll = 0
    attack_target_idx = 0
    attack_type_idx = 0
    wifi_idx = 0

    # Lazily fetched data (refreshed on page enter or action)
    cached_stats: dict = {}
    cached_targets: list = []
    cached_creds: list = []
    cached_wifi_nets: list = []
    cached_wifi_status: dict = {}
    last_detail_fetch = 0.0  # tracks when detail data was last pulled

    def _refresh_detail_data():
        nonlocal cached_stats, cached_targets, cached_creds
        nonlocal cached_wifi_nets, cached_wifi_status, last_detail_fetch
        page = PAGES[page_idx]
        now = time.monotonic()
        if now - last_detail_fetch < 3.0:
            return
        last_detail_fetch = now
        if page == "stats":
            cached_stats = _fetch_stats()
        elif page in ("targets", "attacks"):
            cached_targets = _fetch_targets()
        elif page == "credentials":
            cached_creds = _fetch_credentials()
        elif page == "wifi":
            cached_wifi_nets = _fetch_wifi_networks()
            cached_wifi_status = _fetch_wifi_status()

    try:
        while True:
            now = time.monotonic()
            if now - last_refresh > 2.0:
                state = _fetch_state()
                last_refresh = now
                _refresh_detail_data()

            shown_notice = notice if now < notice_until else None
            anim_step = int(now * 4)

            extra = {
                "stats": cached_stats,
                "targets": cached_targets,
                "creds": cached_creds,
                "target_scroll": target_scroll,
                "cred_scroll": cred_scroll,
                "attack_target_idx": attack_target_idx,
                "attack_type_idx": attack_type_idx,
                "wifi_status": cached_wifi_status,
                "wifi_nets": cached_wifi_nets,
                "wifi_idx": wifi_idx,
            }

            render_key = (
                PAGES[page_idx],
                control_idx,
                shown_notice,
                anim_step,
                state["running"],
                state["api_ok"],
                state["manual_mode"],
                state["automation_enabled"],
                state["target_count"],
                state["port_count"],
                state["vulnerability_count"],
                state["current_ssid"],
                state["orchestrator_status"],
                state["ragnar_status2"],
                tuple(state.get("logs") or []),
                state["error"],
                target_scroll,
                cred_scroll,
                attack_target_idx,
                attack_type_idx,
                wifi_idx,
                len(cached_targets),
                len(cached_creds),
                len(cached_wifi_nets),
            )
            if render_key != last_render_key:
                _draw_screen(PAGES[page_idx], state, control_idx, shown_notice, anim_step, extra)
                last_render_key = render_key

            btn = get_button(PINS, GPIO)
            if btn == "KEY3":
                break

            cur_page = PAGES[page_idx]

            # ---- LEFT/RIGHT: always change page ----
            if btn == "LEFT":
                old_page = page_idx
                page_idx = (page_idx - 1) % len(PAGES)
                if page_idx != old_page:
                    last_detail_fetch = 0.0
                    _refresh_detail_data()
            elif btn == "RIGHT":
                old_page = page_idx
                page_idx = (page_idx + 1) % len(PAGES)
                if page_idx != old_page:
                    last_detail_fetch = 0.0
                    _refresh_detail_data()

            # ---- UP/DOWN: scroll / select within page ----
            elif btn == "UP":
                if cur_page == "targets":
                    target_scroll = max(0, target_scroll - 1)
                elif cur_page == "credentials":
                    cred_scroll = max(0, cred_scroll - 1)
                elif cur_page == "attacks":
                    attack_target_idx = max(0, attack_target_idx - 1)
                elif cur_page == "wifi":
                    wifi_idx = max(0, wifi_idx - 1)
                elif cur_page == "controls":
                    control_idx = (control_idx - 1) % len(CONTROL_ITEMS)
            elif btn == "DOWN":
                if cur_page == "targets":
                    target_scroll = min(max(0, len(cached_targets) - 1), target_scroll + 1)
                elif cur_page == "credentials":
                    cred_scroll = min(max(0, len(cached_creds) - 1), cred_scroll + 1)
                elif cur_page == "attacks":
                    attack_target_idx = min(max(0, len(cached_targets) - 1), attack_target_idx + 1)
                elif cur_page == "wifi":
                    wifi_idx = min(max(0, len(cached_wifi_nets) - 1), wifi_idx + 1)
                elif cur_page == "controls":
                    control_idx = (control_idx + 1) % len(CONTROL_ITEMS)

            # ---- OK: confirm / select / refresh ----
            elif btn == "OK":
                if cur_page == "controls":
                    ok, notice = _run_control(CONTROL_ITEMS[control_idx])
                    notice_until = time.monotonic() + (4 if ok else 6)
                    state = _fetch_state()
                    last_refresh = time.monotonic()
                elif cur_page == "attacks":
                    if cached_targets and 0 <= attack_target_idx < len(cached_targets):
                        t = cached_targets[attack_target_idx]
                        ports = t.get("ports", "")
                        first_port = ports.split(",")[0].strip() if ports else ""
                        ok, notice = _run_attack(
                            t["ip"], first_port, ATTACK_TYPES[attack_type_idx]
                        )
                        notice_until = time.monotonic() + (4 if ok else 6)
                    else:
                        notice = "No target selected"
                        notice_until = time.monotonic() + 3
                elif cur_page == "wifi":
                    if cached_wifi_nets and 0 <= wifi_idx < len(cached_wifi_nets):
                        ssid = cached_wifi_nets[wifi_idx].get("ssid", "")
                        if ssid:
                            ok, notice = _wifi_connect(ssid)
                            notice_until = time.monotonic() + (4 if ok else 6)
                    else:
                        notice = "No network selected"
                        notice_until = time.monotonic() + 3
                else:
                    state = _fetch_state()
                    last_detail_fetch = 0.0
                    _refresh_detail_data()
                    notice = "Refreshed"
                    notice_until = time.monotonic() + 2

            # ---- KEY1: primary action per page ----
            elif btn == "KEY1":
                if cur_page == "wifi":
                    ok, notice = _wifi_scan()
                    notice_until = time.monotonic() + 3
                    if ok:
                        time.sleep(2)
                        cached_wifi_nets = _fetch_wifi_networks()
                elif cur_page == "attacks":
                    # Cycle attack type
                    attack_type_idx = (attack_type_idx + 1) % len(ATTACK_TYPES)
                elif cur_page == "controls":
                    ok, notice = _run_control(CONTROL_ITEMS[control_idx])
                    notice_until = time.monotonic() + (4 if ok else 6)
                    state = _fetch_state()
                    last_refresh = time.monotonic()
                else:
                    state = _fetch_state()
                    last_detail_fetch = 0.0
                    _refresh_detail_data()
                    notice = "Refreshed"
                    notice_until = time.monotonic() + 2

            # ---- KEY2: secondary action / refresh ----
            elif btn == "KEY2":
                if cur_page == "wifi":
                    ok, notice = _wifi_disconnect()
                    notice_until = time.monotonic() + 3
                    cached_wifi_status = _fetch_wifi_status()
                else:
                    state = _fetch_state()
                    last_detail_fetch = 0.0
                    _refresh_detail_data()
                    notice = "Refreshed"
                    notice_until = time.monotonic() + 2

            time.sleep(0.08)
    except Exception as exc:
        _show_fatal_screen(exc)
        while True:
            btn = get_button(PINS, GPIO)
            if btn == "KEY3":
                break
            time.sleep(0.08)
    finally:
        try:
            if LCD is not None:
                LCD.LCD_Clear()
        except Exception:
            pass
        GPIO.cleanup()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _log_ui_error(exc)
        try:
            if LCD is None:
                _init_display()
            _show_fatal_screen(exc)
            while True:
                btn = get_button(PINS, GPIO)
                if btn == "KEY3":
                    break
                time.sleep(0.08)
        finally:
            try:
                if LCD is not None:
                    LCD.LCD_Clear()
            except Exception:
                pass
            GPIO.cleanup()
