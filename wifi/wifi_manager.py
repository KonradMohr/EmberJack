#!/usr/bin/env python3
"""
EmberJack / RaspyJack WiFi Management System
============================================
Dual-interface support for EmberJack - use eth0 and multiple WiFi adapters.

Features:
- Reliable nmcli + iw/iwgetid/wpa_cli fallback scanning, status, and connect
- Automatic rfkill unblock, managed-mode enforcement, and stale NM profile recovery
- Unified Saved Profiles (EmberJack JSON profiles + NetworkManager saved connections)
- Dual-interface selection (eth0, wlan0, wlan1+) with live IPv4/SSID inspection
- Detailed `last_error` reporting for clear on-screen feedback
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    import rj_env
except Exception:
    rj_env = None

try:
    import LCD_1in44, LCD_Config  # noqa: F401
    from PIL import Image, ImageDraw, ImageFont  # noqa: F401
    import RPi.GPIO as GPIO  # noqa: F401
    LCD_AVAILABLE = True
except Exception:
    LCD_AVAILABLE = False


def _split_nmcli_line(line: str):
    """Split a `nmcli -t` colon-delimited line honoring backslash-escaped colons."""
    parts = []
    cur = []
    escaped = False
    for ch in line:
        if escaped:
            cur.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _run_cmd(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "Command timed out")
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]} not found")
    except Exception as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


class WiFiManager:
    def __init__(self):
        root_dir = rj_env.get_base_dir() if rj_env is not None else _REPO_ROOT
        self.base_dir = os.path.join(root_dir, "wifi")
        self.profiles_dir = os.path.join(self.base_dir, "profiles")
        self.current_profile_file = os.path.join(self.base_dir, "current_profile.json")
        self.log_file = os.path.join(self.base_dir, "wifi.log")

        try:
            os.makedirs(self.profiles_dir, exist_ok=True)
        except OSError:
            pass

        self.wifi_interfaces = self.detect_wifi_interfaces()
        self.selected_interface = None
        self.current_interface = None
        self.current_profile = None
        self.connection_status = "disconnected"
        self.last_error = ""

    def log(self, message):
        """Log messages with timestamp."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_msg = f"[{timestamp}] {message}"
        print(log_msg)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(log_msg + "\n")
        except Exception:
            pass

    def disable_wifi_powersave(self, interface=None):
        """Disable WiFi power-save sleep (fixes Broadcom Raspberry Pi connection drops)."""
        if not shutil.which("iw"):
            return
        targets = [interface] if (interface and interface != "eth0") else list(self.wifi_interfaces)
        for iface in targets:
            if iface and iface != "eth0":
                _run_cmd(["iw", "dev", iface, "set", "power_save", "off"], timeout=3)

    def _harden_nm_connection(self, con_name, secured=True):
        """Force autoconnect=yes, all-users permissions, disable NM bgscan/powersave/MAC-rand & keyring blocks."""
        if not con_name or not shutil.which("nmcli"):
            return False
        cmd = [
            "nmcli", "connection", "modify", "id", str(con_name),
            "connection.autoconnect", "yes",
            "connection.autoconnect-retries", "0",
            "connection.permissions", "",
            "802-11-wireless.powersave", "2",
            "802-11-wireless.bgscan", "",
            "802-11-wireless.cloned-mac-address", "permanent",
        ]
        res = _run_cmd(cmd, timeout=6)
        if secured:
            # pmf=0 (default: optional on WPA2, required on WPA3-SAE; NEVER 1 which disables PMF!)
            # psk-flags=0 (store PSK in system connection file; avoids "Secrets were required" on headless Pi)
            _run_cmd(
                [
                    "nmcli", "connection", "modify", "id", str(con_name),
                    "802-11-wireless-security.pmf", "0",
                    "802-11-wireless-security.psk-flags", "0",
                ],
                timeout=5,
            )
        return res.returncode == 0

    def _stop_competing_wifi_processes(self, interface=None):
        """Clean up stale monitor interfaces & rogue per-interface wpa_supplicant (never NM's -u daemon)."""
        # 1. Remove any stale wlan*mon interfaces
        if os.path.isdir("/sys/class/net"):
            for entry in os.listdir("/sys/class/net"):
                if entry.startswith("wlan") and entry.endswith("mon"):
                    if shutil.which("airmon-ng"):
                        _run_cmd(["airmon-ng", "stop", entry], timeout=6)
                    if os.path.exists(f"/sys/class/net/{entry}") and shutil.which("iw"):
                        _run_cmd(["iw", "dev", entry, "del"], timeout=4)

        # 2. Switch target interface out of monitor mode if stuck
        if interface and interface != "eth0" and shutil.which("iw"):
            info_r = _run_cmd(["iw", "dev", interface, "info"], timeout=4)
            if info_r.returncode == 0 and "type monitor" in info_r.stdout.lower():
                if shutil.which("ip"):
                    _run_cmd(["ip", "link", "set", interface, "down"], timeout=4)
                _run_cmd(["iw", "dev", interface, "set", "type", "managed"], timeout=4)
                if shutil.which("ip"):
                    _run_cmd(["ip", "link", "set", interface, "up"], timeout=4)

        # 3. Kill ONLY rogue per-interface wpa_supplicant processes when NetworkManager owns WiFi
        if shutil.which("nmcli") and shutil.which("pgrep"):
            pg = _run_cmd(["pgrep", "-af", "^wpa_supplicant"], timeout=4)
            if pg.returncode == 0:
                for line in pg.stdout.splitlines():
                    parts = line.strip().split(None, 1)
                    if len(parts) < 2 or not parts[0].isdigit():
                        continue
                    pid_s, args = parts[0], parts[1]
                    if ("-i wlan" in args or "-iwlan" in args) and (" -u" not in args and "-u " not in args):
                        self.log(f"Stopping rogue per-interface wpa_supplicant PID {pid_s}")
                        _run_cmd(["kill", pid_s], timeout=3)

    def repair_wifi_stack(self, interface=None):
        """Repair system-level WiFi stack (Broadcom brcmfmac, NM bgscan/powersave, competing daemons)."""
        details = []
        reboot_needed = False
        iface = interface or self.get_active_wifi_interface() or "wlan0"

        # Prefer running our bundled scripts/fix_wifi.sh if present and running as root
        script_path = os.path.join(_REPO_ROOT, "scripts", "fix_wifi.sh")
        is_root = hasattr(os, "geteuid") and os.geteuid() == 0
        if is_root and os.path.isfile(script_path) and shutil.which("bash"):
            res = _run_cmd(["bash", script_path], timeout=45)
            out_all = (res.stdout or "") + "\n" + (res.stderr or "")
            reboot_needed = "sudo reboot" in out_all.lower() or "reboot once" in out_all.lower()
            for ln in out_all.splitlines():
                clean = re.sub(r"\x1b\[[0-9;]*m", "", ln).strip()
                if clean:
                    details.append(clean)
            self.wifi_interfaces = self.detect_wifi_interfaces()
            if res.returncode == 0:
                summary = "Repaired! Reboot for brcmfmac" if reboot_needed else "WiFi Stack Repaired!"
                return {"ok": True, "summary": summary, "details": details[-12:], "reboot_needed": reboot_needed}

        # Pure-Python fallback (works in tests or when fix_wifi.sh isn't available)
        self._stop_competing_wifi_processes(iface)
        self._ensure_wifi_ready(iface, enable_autoconnect=True)
        details.append(f"Radio unblocked, managed & autoconnect ON: {iface}")

        if shutil.which("bluetoothctl"):
            _run_cmd(["bluetoothctl", "scan", "off"], timeout=3)

        if shutil.which("systemctl"):
            nm_active = _run_cmd(["systemctl", "is-active", "--quiet", "NetworkManager"], timeout=4).returncode == 0
            for svc in ("hostapd", "dnsmasq", "NetworkManager-wait-online.service", "systemd-networkd-wait-online.service"):
                _run_cmd(["systemctl", "disable", "--now", svc], timeout=5)
            if nm_active:
                for svc in ("dhcpcd", "wpa_supplicant@wlan0", "wpa_supplicant@wlan1"):
                    _run_cmd(["systemctl", "disable", "--now", svc], timeout=5)
                details.append("Disabled competing & wait-online daemons")

        # Disable Raspberry Pi OS Trixie cloud-init boot stalls ("Completed socket interaction") & 0-byte Netplan YAMLs
        if is_root and os.path.isdir("/etc/cloud"):
            try:
                if not os.path.exists("/etc/cloud/cloud-init.disabled"):
                    with open("/etc/cloud/cloud-init.disabled", "w", encoding="utf-8"):
                        pass
                    reboot_needed = True
                    details.append("Disabled cloud-init boot stalls")
                os.makedirs("/etc/cloud/cloud.cfg.d", exist_ok=True)
                ncfg = "/etc/cloud/cloud.cfg.d/99-disable-network-config.cfg"
                if not os.path.exists(ncfg):
                    with open(ncfg, "w", encoding="utf-8") as fh:
                        fh.write("network: {config: disabled}\n")
            except OSError:
                pass
        if is_root and os.path.isdir("/etc/netplan"):
            try:
                for fn in os.listdir("/etc/netplan"):
                    if fn.endswith((".yaml", ".yml")):
                        fp = os.path.join("/etc/netplan", fn)
                        if os.path.isfile(fp) and os.path.getsize(fp) == 0:
                            os.unlink(fp)
                            details.append(f"Removed empty Netplan file: {fn}")
            except OSError:
                pass

        # Harden saved NM WiFi profiles
        if shutil.which("nmcli"):
            res_c = _run_cmd(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], timeout=6)
            hardened_n = 0
            if res_c.returncode == 0:
                for ln in res_c.stdout.splitlines():
                    parts = _split_nmcli_line(ln)
                    if len(parts) >= 2 and parts[1].strip() in ("wifi", "802-11-wireless") and parts[0].strip():
                        if self._harden_nm_connection(parts[0].strip(), secured=False):
                            hardened_n += 1
            if hardened_n:
                details.append(f"Hardened {hardened_n} NM WiFi profile(s)")

        # Check Broadcom driver parameters if running on a Pi
        if os.path.isdir("/sys/module/brcmfmac"):
            try:
                with open("/sys/module/brcmfmac/parameters/roamoff", "r", encoding="utf-8") as fh:
                    if fh.read().strip() != "1":
                        reboot_needed = True
            except OSError:
                pass
            if is_root and os.path.isdir("/etc/modprobe.d"):
                try:
                    with open("/etc/modprobe.d/99-emberjack-brcmfmac.conf", "w", encoding="utf-8") as fh:
                        fh.write(
                            "# EmberJack: fix Broadcom Raspberry Pi WiFi handshake timeouts & drops\n"
                            "options brcmfmac roamoff=1 feature_disable=0x82000\n"
                        )
                    details.append("Configured brcmfmac roamoff=1 feature_disable=0x82000")
                except OSError:
                    pass

        if is_root and shutil.which("systemctl"):
            _run_cmd(["systemctl", "restart", "wpa_supplicant.service"], timeout=10)
            _run_cmd(["systemctl", "restart", "NetworkManager.service"], timeout=12)
            details.append("Restarted wpa_supplicant & NetworkManager")

        self.wifi_interfaces = self.detect_wifi_interfaces()
        summary = "Repaired! Reboot for brcmfmac" if reboot_needed else "WiFi Stack Repaired!"
        return {"ok": True, "summary": summary, "details": details, "reboot_needed": reboot_needed}

    def _ensure_wifi_ready(self, interface=None, enable_autoconnect=False):
        """Ensure WiFi radio is unblocked, power-save is off, and adapter is up, managed & autoconnecting."""
        if shutil.which("rfkill"):
            _run_cmd(["rfkill", "unblock", "wifi"], timeout=4)
        if shutil.which("nmcli"):
            radio = _run_cmd(["nmcli", "radio", "wifi"], timeout=4)
            if radio.returncode == 0 and "disabled" in radio.stdout.lower():
                _run_cmd(["nmcli", "radio", "wifi", "on"], timeout=5)
            if interface and interface != "eth0":
                dev_st = _run_cmd(["nmcli", "-t", "-f", "DEVICE,STATE", "device"], timeout=4)
                if dev_st.returncode == 0:
                    for ln in dev_st.stdout.splitlines():
                        parts = _split_nmcli_line(ln)
                        if len(parts) >= 2 and parts[0].strip() == interface and parts[1].strip().lower() == "unmanaged":
                            _run_cmd(["nmcli", "device", "set", interface, "managed", "yes"], timeout=5)
                            break
                if enable_autoconnect:
                    # Re-enable device-level autoconnect (cleared whenever `nmcli device disconnect` was run)
                    _run_cmd(["nmcli", "device", "set", interface, "autoconnect", "yes"], timeout=5)
        if interface and interface != "eth0":
            oper_path = f"/sys/class/net/{interface}/operstate"
            is_up = False
            try:
                with open(oper_path, "r", encoding="utf-8") as fh:
                    is_up = fh.read().strip().lower() == "up"
            except OSError:
                pass
            if not is_up and shutil.which("ip"):
                _run_cmd(["ip", "link", "set", interface, "up"], timeout=4)
            self.disable_wifi_powersave(interface)

    def detect_wifi_interfaces(self):
        """Detect all available WiFi interfaces."""
        interfaces = []
        try:
            if os.path.isdir("/sys/class/net"):
                for iface in os.listdir("/sys/class/net"):
                    if os.path.exists(f"/sys/class/net/{iface}/wireless"):
                        interfaces.append(iface)
                        self.log(f"Found wireless interface via /sys: {iface}")
            if not interfaces and shutil.which("iwconfig"):
                result = _run_cmd(["iwconfig"], timeout=5)
                for line in result.stdout.split("\n"):
                    if "IEEE 802.11" in line:
                        iface = line.split()[0]
                        if iface not in interfaces:
                            interfaces.append(iface)
                            self.log(f"Found wireless interface via iwconfig: {iface}")
        except Exception as e:
            self.log(f"Error detecting WiFi interfaces: {e}")

        interfaces.sort(key=lambda x: (x != "wlan0", x != "wlan1", x))
        self.log(f"Final detected WiFi interfaces: {interfaces}")
        return interfaces

    def _scan_with_iw(self, interface):
        """Fallback WiFi scan via `iw dev <iface> scan` when nmcli is unavailable/empty."""
        if not shutil.which("iw"):
            return []
        res = _run_cmd(["iw", "dev", interface, "scan"], timeout=18)
        if res.returncode != 0 or not res.stdout:
            return []
        by_ssid = {}
        cur_bssid = ""
        cur_ssid = ""
        cur_sig = 0
        cur_sec = ""
        cur_priv = False

        def _commit():
            if not cur_ssid:
                return
            sec_str = cur_sec or ("WEP" if cur_priv else "")
            prev = by_ssid.get(cur_ssid)
            if prev is None or cur_sig > prev["signal"]:
                by_ssid[cur_ssid] = {
                    "bssid": cur_bssid,
                    "ssid": cur_ssid,
                    "quality": f"{cur_sig}/100",
                    "signal": cur_sig,
                    "encrypted": bool(sec_str),
                    "security": sec_str,
                }

        for raw in res.stdout.splitlines():
            line = raw.strip()
            if line.startswith("BSS "):
                _commit()
                m = re.match(r"BSS\s+([0-9a-fA-F:]{17})", line)
                cur_bssid = m.group(1).upper() if m else ""
                cur_ssid = ""
                cur_sig = 0
                cur_sec = ""
                cur_priv = False
            elif line.startswith("SSID:"):
                cur_ssid = line[5:].strip()
            elif line.startswith("signal:"):
                m = re.search(r"(-?\d+(?:\.\d+)?)\s*dBm", line)
                if m:
                    dbm = float(m.group(1))
                    cur_sig = max(0, min(100, int(round((dbm + 95.0) * 100.0 / 60.0))))
            elif "capability:" in line and "Privacy" in line:
                cur_priv = True
            elif line.startswith("RSN:"):
                cur_sec = "WPA2"
            elif line.startswith("WPA:") and not cur_sec:
                cur_sec = "WPA"
            elif "SAE" in line and cur_sec == "WPA2":
                cur_sec = "WPA3"
        _commit()
        nets = list(by_ssid.values())
        nets.sort(key=lambda n: n.get("signal", 0), reverse=True)
        return nets

    def scan_networks(self, interface=None, rescan=True):
        """Scan for available WiFi networks via nmcli with iw fallback."""
        self.last_error = ""
        if not interface or interface == "eth0":
            interface = self.get_active_wifi_interface()
        if not interface or interface == "eth0":
            self.wifi_interfaces = self.detect_wifi_interfaces()
            interface = self.wifi_interfaces[0] if self.wifi_interfaces else None

        if not interface:
            self.last_error = "No WiFi adapter detected"
            self.log(self.last_error)
            return []

        self._ensure_wifi_ready(interface)
        self.log(f"Scanning networks on {interface}...")

        networks = []
        if shutil.which("nmcli"):
            base_cmd = [
                "nmcli", "-t", "-f", "BSSID,SSID,SIGNAL,SECURITY",
                "device", "wifi", "list", "ifname", interface,
            ]
            res = _run_cmd(base_cmd + ["--rescan", "yes" if rescan else "no"], timeout=20)
            if res.returncode != 0 or not res.stdout.strip():
                # Retry cached list (handles "Scanning not allowed while already scanning")
                res = _run_cmd(base_cmd + ["--rescan", "no"], timeout=10)
            if res.returncode != 0 and res.stderr.strip():
                self.last_error = res.stderr.strip().splitlines()[-1][:64]

            by_ssid = {}
            for line in res.stdout.strip().splitlines():
                if not line.strip():
                    continue
                parts = _split_nmcli_line(line)
                if len(parts) < 4:
                    continue
                bssid = parts[0].strip()
                ssid = parts[1].strip()
                if not ssid:
                    continue
                try:
                    signal = max(0, min(100, int(parts[2].strip())))
                except ValueError:
                    signal = 0
                security = parts[3].strip()
                encrypted = bool(security and security != "--")
                prev = by_ssid.get(ssid)
                if prev is None or signal > prev["signal"]:
                    by_ssid[ssid] = {
                        "bssid": bssid,
                        "ssid": ssid,
                        "quality": f"{signal}/100",
                        "signal": signal,
                        "encrypted": encrypted,
                        "security": security,
                    }
            networks = sorted(by_ssid.values(), key=lambda n: n.get("signal", 0), reverse=True)

        if not networks:
            networks = self._scan_with_iw(interface)

        self.log(f"Found {len(networks)} unique networks")
        return networks

    def _profile_path_for_ssid(self, ssid):
        safe_name = "".join(c for c in str(ssid) if c.isalnum() or c in (" ", "-", "_")).rstrip()
        if not safe_name:
            safe_name = "wifi_profile"
        return os.path.join(self.profiles_dir, f"{safe_name}.json")

    def save_profile(self, ssid, password, interface="auto", priority=1, auto_connect=True):
        """Save a WiFi profile JSON."""
        profile = {
            "ssid": ssid,
            "password": password,
            "interface": interface,
            "priority": priority,
            "auto_connect": auto_connect,
            "created": datetime.now().isoformat(),
            "last_used": None,
        }
        profile_file = self._profile_path_for_ssid(ssid)
        try:
            os.makedirs(self.profiles_dir, exist_ok=True)
            with open(profile_file, "w", encoding="utf-8") as f:
                json.dump(profile, f, indent=2)
            self.log(f"Saved WiFi profile: {ssid}")
            return True
        except Exception as e:
            self.last_error = str(e)[:60]
            self.log(f"Error saving profile: {e}")
            return False

    def load_profiles(self):
        """Load saved WiFi profiles from BOTH EmberJack JSON files and NetworkManager."""
        profiles = []
        seen_ssids = set()

        # 1. EmberJack JSON profiles (skip placeholder sample.json template)
        try:
            if os.path.isdir(self.profiles_dir):
                for filename in sorted(os.listdir(self.profiles_dir)):
                    if not filename.endswith(".json"):
                        continue
                    fpath = os.path.join(self.profiles_dir, filename)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            profile = json.load(f)
                    except Exception:
                        continue
                    ssid = str(profile.get("ssid", "") or "").strip()
                    if not ssid:
                        continue
                    if filename == "sample.json" and ssid == "YourWiFiNetwork":
                        continue
                    profile["filename"] = filename
                    profile.setdefault("source", "json")
                    profiles.append(profile)
                    seen_ssids.add(ssid)
        except Exception as e:
            self.log(f"Error loading JSON profiles: {e}")

        # 2. Existing NetworkManager saved WiFi connections (e.g. Pi Imager "preconfigured", raspi-config, nmcli)
        if shutil.which("nmcli"):
            res = _run_cmd(["nmcli", "-t", "-f", "NAME,TYPE,AUTOCONNECT", "connection", "show"], timeout=6)
            if res.returncode == 0:
                for line in res.stdout.strip().splitlines():
                    if not line.strip():
                        continue
                    parts = _split_nmcli_line(line)
                    if len(parts) < 2:
                        continue
                    name = parts[0].strip()
                    ctype = parts[1].strip()
                    auto_c = (parts[2].strip().lower() != "no") if len(parts) > 2 else True
                    if ctype not in ("wifi", "802-11-wireless") or not name:
                        continue
                    real_ssid = self._get_nm_connection_ssid(name) or name
                    if real_ssid not in seen_ssids:
                        profiles.append({
                            "ssid": real_ssid,
                            "password": "",
                            "interface": "auto",
                            "priority": 1,
                            "auto_connect": auto_c,
                            "last_used": None,
                            "source": "nmcli",
                            "nm_name": name,
                        })
                        seen_ssids.add(real_ssid)

        profiles.sort(key=lambda x: (int(x.get("priority", 1) or 1), 1 if x.get("source") == "json" else 0), reverse=True)
        return profiles

    def _get_nm_connection_ssid(self, nm_name: str) -> str:
        """Read the actual 802-11-wireless.ssid for an NM profile (handles Pi Imager 'preconfigured')."""
        if not shutil.which("nmcli") or not nm_name:
            return ""
        res = _run_cmd(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", "id", nm_name], timeout=4)
        if res.returncode == 0:
            val = res.stdout.strip()
            if val and val != "--":
                return val
        return ""

    def _find_nm_connections_for_ssid(self, ssid: str):
        """Return all NetworkManager WiFi connection names matching an SSID (by name or wireless.ssid)."""
        if not shutil.which("nmcli") or not ssid:
            return []
        res = _run_cmd(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], timeout=5)
        if res.returncode != 0:
            return []
        matches = []
        for line in res.stdout.splitlines():
            parts = _split_nmcli_line(line)
            if len(parts) < 2:
                continue
            name, ctype = parts[0].strip(), parts[1].strip()
            if ctype not in ("wifi", "802-11-wireless") or not name:
                continue
            if name == ssid or self._get_nm_connection_ssid(name) == ssid:
                if name not in matches:
                    matches.append(name)
        return matches

    def delete_profile(self, ssid, nm_name=None):
        """Delete a WiFi profile from EmberJack JSON storage and/or NetworkManager."""
        self.last_error = ""
        removed = False
        profile_file = self._profile_path_for_ssid(ssid)
        try:
            if os.path.exists(profile_file):
                os.remove(profile_file)
                removed = True
        except Exception as e:
            self.last_error = str(e)[:60]
            self.log(f"Error deleting JSON profile: {e}")

        if shutil.which("nmcli"):
            targets = []
            if nm_name:
                targets.append(str(nm_name))
            for cand in self._find_nm_connections_for_ssid(str(ssid)):
                if cand not in targets:
                    targets.append(cand)
            if not targets and ssid:
                targets.append(str(ssid))
            for cname in targets:
                res = _run_cmd(["nmcli", "connection", "delete", "id", cname], timeout=8)
                if res.returncode == 0:
                    removed = True

        if removed:
            self.log(f"Deleted WiFi profile: {ssid}")
        return removed

    def _has_nm_connection(self, name: str) -> bool:
        return bool(self._find_nm_connections_for_ssid(name))

    def _connect_with_wpa_cli(self, ssid, password=None, interface=None):
        """Fallback connect via wpa_cli when NetworkManager/nmcli is unavailable or inactive."""
        if not shutil.which("wpa_cli") or not interface:
            return False
        add_r = _run_cmd(["wpa_cli", "-i", interface, "add_network"], timeout=5)
        if add_r.returncode != 0:
            return False
        net_id = add_r.stdout.strip().splitlines()[-1].strip()
        if not net_id.isdigit():
            return False
        _run_cmd(["wpa_cli", "-i", interface, "set_network", net_id, "ssid", f'"{ssid}"'], timeout=5)
        if password:
            _run_cmd(["wpa_cli", "-i", interface, "set_network", net_id, "psk", f'"{password}"'], timeout=5)
        else:
            _run_cmd(["wpa_cli", "-i", interface, "set_network", net_id, "key_mgmt", "NONE"], timeout=5)
        _run_cmd(["wpa_cli", "-i", interface, "enable_network", net_id], timeout=5)
        sel_r = _run_cmd(["wpa_cli", "-i", interface, "select_network", net_id], timeout=5)
        if sel_r.returncode != 0 or "OK" not in sel_r.stdout:
            return False
        _run_cmd(["wpa_cli", "-i", interface, "save_config"], timeout=5)
        for _ in range(10):
            time.sleep(0.8)
            st = self.get_connection_status(interface)
            if st.get("status") == "connected" and st.get("ssid") == ssid:
                self._mark_connected(ssid, interface)
                return True
        return False

    def connect_to_network(self, ssid, password=None, interface=None, nm_name=None, security=""):
        """Connect to a WiFi network with automatic stale-profile, PMF & WPA2/WPA3-SAE recovery."""
        self.last_error = ""
        if not interface or interface == "eth0":
            interface = self.get_active_wifi_interface()
        if not interface or interface == "eth0":
            self.wifi_interfaces = self.detect_wifi_interfaces()
            interface = self.wifi_interfaces[0] if self.wifi_interfaces else None

        if not interface:
            self.last_error = "No WiFi adapter available"
            self.log(self.last_error)
            return False

        self._stop_competing_wifi_processes(interface)
        self._ensure_wifi_ready(interface, enable_autoconnect=True)
        self.log(f"Connecting to {ssid} on {interface}...")

        try:
            status = self.get_connection_status(interface)
            if status.get("status") == "connected" and status.get("ssid") == ssid:
                self.log(f"Already connected to {ssid}")
                return True

            if not shutil.which("nmcli"):
                if self._connect_with_wpa_cli(ssid, password, interface):
                    return True
                self.last_error = "nmcli (NetworkManager) not installed"
                self.log(self.last_error)
                return False

            existing_nm = ([nm_name] if nm_name else []) + [
                n for n in self._find_nm_connections_for_ssid(ssid) if n != nm_name
            ]
            sec_u = str(security or "").upper()
            prefer_sae = ("SAE" in sec_u or "WPA3" in sec_u) and ("WPA2" not in sec_u and "WPA1" not in sec_u)
            primary_kmgmt = "sae" if prefer_sae else "wpa-psk"
            primary_pmf = "3" if prefer_sae else "0"  # 0=default (optional on WPA2, required on SAE), 3=required

            # Case 1: Saved NM profile with no new password supplied -> harden (incl. psk-flags=0) & bring up!
            if not password and existing_nm:
                last_up_err = ""
                for cname in existing_nm:
                    self._harden_nm_connection(cname, secured=True)
                    for attempt in range(2):
                        up_res = _run_cmd(["nmcli", "connection", "up", "id", cname, "ifname", interface], timeout=30)
                        if up_res.returncode == 0:
                            self._mark_connected(ssid, interface)
                            return True
                        raw_u = (up_res.stderr.strip() or up_res.stdout.strip())
                        if raw_u:
                            last_up_err = raw_u.splitlines()[-1]
                        if attempt == 0:
                            time.sleep(1.0)
                if last_up_err:
                    self.last_error = last_up_err.replace("Error:", "").strip()[:60]

            # Check if SSID is already in nmcli's cache; only rescan if missing to avoid busy-scan errors
            cached = _run_cmd(["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list", "ifname", interface, "--rescan", "no"], timeout=6)
            if ssid not in [ln.strip() for ln in cached.stdout.splitlines()]:
                _run_cmd(["nmcli", "device", "wifi", "rescan", "ifname", interface], timeout=12)
                time.sleep(1.5)

            # Case 2: Standard `nmcli device wifi connect`
            cmd = ["nmcli", "device", "wifi", "connect", ssid, "ifname", interface]
            if password:
                cmd += ["password", password]
            self.log(f"Running: nmcli device wifi connect {ssid} ifname {interface}")
            result = _run_cmd(cmd, timeout=40)

            if result.returncode == 0:
                self._harden_nm_connection(ssid, secured=bool(password))
                self._mark_connected(ssid, interface)
                return True

            err = (result.stderr.strip() or result.stdout.strip()).splitlines()[-1] if (result.stderr.strip() or result.stdout.strip()) else "Connect failed"
            self.log(f"First connect attempt failed for {ssid}: {err}")

            # Case 3: Stale/broken NM profile recovery (including Pi Imager "preconfigured" & Netplan profiles!)
            # Explicitly sets psk-flags=0 (file-stored secret, never waits for desktop keyring agent)
            # and pmf=0/3 (never pmf=1/disable on WPA2/WPA3!).
            if password:
                if existing_nm:
                    primary_nm = existing_nm[0]
                    self.log(f"Updating existing NetworkManager profile '{primary_nm}' for {ssid} in-place...")
                    mod_r = _run_cmd([
                        "nmcli", "connection", "modify", "id", primary_nm,
                        "connection.autoconnect", "yes",
                        "connection.autoconnect-retries", "0",
                        "connection.permissions", "",
                        "wifi-sec.key-mgmt", primary_kmgmt,
                        "wifi-sec.psk", password,
                        "wifi-sec.psk-flags", "0",
                        "802-11-wireless.powersave", "2",
                        "802-11-wireless.bgscan", "",
                        "802-11-wireless.cloned-mac-address", "permanent",
                        "802-11-wireless-security.pmf", primary_pmf,
                    ], timeout=8)
                    if mod_r.returncode == 0:
                        up_mod = _run_cmd(["nmcli", "connection", "up", "id", primary_nm, "ifname", interface], timeout=35)
                        if up_mod.returncode == 0:
                            self._mark_connected(ssid, interface)
                            return True
                        err = (up_mod.stderr.strip() or up_mod.stdout.strip()).splitlines()[-1] if (up_mod.stderr.strip() or up_mod.stdout.strip()) else err

                    self.log(f"Clearing stale NetworkManager profile(s) {existing_nm} for {ssid} and retrying...")
                    for cname in existing_nm:
                        _run_cmd(["nmcli", "connection", "delete", "id", cname], timeout=8)
                    retry = _run_cmd(["nmcli", "device", "wifi", "connect", ssid, "password", password, "ifname", interface], timeout=40)
                    if retry.returncode == 0:
                        self._harden_nm_connection(ssid, secured=True)
                        self._mark_connected(ssid, interface)
                        return True
                    err = (retry.stderr.strip() or retry.stdout.strip()).splitlines()[-1] if (retry.stderr.strip() or retry.stdout.strip()) else err

                # Case 4: Explicit profile creation cycling WPA2-PSK (pmf=default), WPA3-SAE (pmf=required),
                # and legacy WPA2-PSK (pmf=disable) so ANY router (WPA2, WPA2/WPA3 mixed, pure WPA3) works!
                attempts_sec = (
                    [("sae", "3"), ("wpa-psk", "0"), ("wpa-psk", "1")]
                    if prefer_sae
                    else [("wpa-psk", "0"), ("sae", "3"), ("wpa-psk", "1")]
                )
                for km_try, pmf_try in attempts_sec:
                    _run_cmd(["nmcli", "connection", "delete", "id", ssid], timeout=6)
                    add_res = _run_cmd([
                        "nmcli", "connection", "add",
                        "type", "wifi",
                        "ifname", interface,
                        "con-name", ssid,
                        "ssid", ssid,
                        "connection.autoconnect", "yes",
                        "connection.autoconnect-retries", "0",
                        "connection.permissions", "",
                        "wifi-sec.key-mgmt", km_try,
                        "wifi-sec.psk", password,
                        "wifi-sec.psk-flags", "0",
                        "802-11-wireless.powersave", "2",
                        "802-11-wireless.bgscan", "",
                        "802-11-wireless.cloned-mac-address", "permanent",
                        "802-11-wireless-security.pmf", pmf_try,
                    ], timeout=10)
                    if add_res.returncode != 0:
                        continue
                    up2 = _run_cmd(["nmcli", "connection", "up", "id", ssid, "ifname", interface], timeout=35)
                    if up2.returncode == 0:
                        self._mark_connected(ssid, interface)
                        return True
                    err = (up2.stderr.strip() or up2.stdout.strip()).splitlines()[-1] if (up2.stderr.strip() or up2.stdout.strip()) else err
                    _run_cmd(["nmcli", "connection", "delete", "id", ssid], timeout=6)

            # Case 5: If NetworkManager daemon isn't running, try wpa_cli fallback
            if "not running" in err.lower() or "not managed" in err.lower():
                if self._connect_with_wpa_cli(ssid, password, interface):
                    return True

            self.last_error = err.replace("Error:", "").strip()[:60]
            self.log(f"Failed to connect to {ssid}: {self.last_error}")
            return False

        except Exception as e:
            self.last_error = str(e)[:60]
            self.log(f"Error connecting to {ssid}: {e}")
            return False

    def _mark_connected(self, ssid, interface):
        self.log(f"Successfully connected to {ssid}")
        self.current_interface = interface
        self.current_profile = ssid
        self.connection_status = "connected"
        self.last_error = ""
        self.update_profile_last_used(ssid)
        self.save_current_connection(ssid, interface)

    def connect_to_profile(self, profile):
        """Connect using a saved profile (JSON or NetworkManager)."""
        interface = profile.get("interface", "auto")
        if interface == "auto" or not interface or interface == "eth0":
            interface = self.get_active_wifi_interface()
        pw = profile.get("password") or None
        return self.connect_to_network(
            profile["ssid"], pw, interface, nm_name=profile.get("nm_name")
        )

    def disconnect(self, interface=None):
        """Disconnect from WiFi."""
        self.last_error = ""
        if not interface or interface == "eth0":
            interface = self.current_interface or self.get_active_wifi_interface()
        if not interface:
            self.last_error = "No active WiFi interface"
            return False

        ok = False
        if shutil.which("nmcli"):
            res = _run_cmd(["nmcli", "device", "disconnect", interface], timeout=15)
            ok = (res.returncode == 0)
            if not ok:
                self.last_error = (res.stderr.strip() or res.stdout.strip())[:60]
        if not ok and shutil.which("wpa_cli"):
            res2 = _run_cmd(["wpa_cli", "-i", interface, "disconnect"], timeout=8)
            ok = (res2.returncode == 0)

        if ok:
            self.log(f"Disconnected from WiFi on {interface}")
            self.current_interface = None
            self.current_profile = None
            self.connection_status = "disconnected"
            self.last_error = ""
            return True
        self.log(f"Error disconnecting {interface}: {self.last_error}")
        return False

    def _get_iface_ip(self, interface):
        res = _run_cmd(["ip", "-4", "addr", "show", interface], timeout=4)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                if "inet " in line:
                    return line.split("inet ", 1)[1].split("/", 1)[0].strip()
        return None

    def get_connection_status(self, interface=None):
        """Get current connection status for a specific interface (eth0 or wlan*)."""
        if not interface:
            interface = self.current_interface or self.get_active_interface()
        if not interface:
            return {"status": "no_interface", "ssid": None, "ip": None, "interface": None}

        ip_addr = self._get_iface_ip(interface)

        # Wired Ethernet check
        if interface == "eth0" or not os.path.exists(f"/sys/class/net/{interface}/wireless"):
            link = _run_cmd(["ip", "link", "show", interface], timeout=4)
            is_up = (link.returncode == 0 and "state UP" in link.stdout)
            if is_up and ip_addr:
                return {"status": "connected", "ssid": "Ethernet", "ip": ip_addr, "interface": interface}
            return {"status": "disconnected", "ssid": None, "ip": ip_addr, "interface": interface}

        connected_ssid = None
        if shutil.which("nmcli"):
            # Always pass --rescan no so status polling NEVER triggers active radio scans!
            res = _run_cmd(["nmcli", "-t", "-f", "ACTIVE,SSID,DEVICE", "dev", "wifi", "list", "--rescan", "no"], timeout=6)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = _split_nmcli_line(line)
                    if len(parts) >= 3 and parts[0].strip() == "yes" and parts[2].strip() == interface:
                        connected_ssid = parts[1].strip()
                        break

        if not connected_ssid and shutil.which("iwgetid"):
            r_iw = _run_cmd(["iwgetid", "-r", interface], timeout=4)
            if r_iw.returncode == 0 and r_iw.stdout.strip():
                connected_ssid = r_iw.stdout.strip()

        if not connected_ssid and shutil.which("wpa_cli"):
            r_wpa = _run_cmd(["wpa_cli", "-i", interface, "status"], timeout=4)
            if r_wpa.returncode == 0:
                for ln in r_wpa.stdout.splitlines():
                    if ln.startswith("ssid="):
                        connected_ssid = ln.split("=", 1)[1].strip()
                        break

        if connected_ssid:
            return {
                "status": "connected",
                "ssid": connected_ssid,
                "ip": ip_addr,
                "interface": interface,
            }
        return {"status": "disconnected", "ssid": None, "ip": ip_addr, "interface": interface}

    def auto_connect(self):
        """Auto-connect to the best available saved network."""
        self.last_error = ""
        profiles = self.load_profiles()
        auto_profiles = [p for p in profiles if p.get("auto_connect", True)]
        if not auto_profiles:
            self.last_error = "No saved WiFi profiles"
            self.log(self.last_error)
            return False

        available_networks = self.scan_networks(rescan=True)
        available_ssids = {n.get("ssid") for n in available_networks if n.get("ssid")}
        if not available_ssids:
            self.last_error = self.last_error or "No WiFi networks in range"
            return False

        for profile in auto_profiles:
            if profile["ssid"] in available_ssids:
                self.log(f"Auto-connecting to {profile['ssid']}")
                if self.connect_to_profile(profile):
                    return True

        self.last_error = "No saved network in range"
        self.log(self.last_error)
        return False

    def update_profile_last_used(self, ssid):
        """Update the last_used timestamp for a JSON profile."""
        profile_file = self._profile_path_for_ssid(ssid)
        try:
            if os.path.exists(profile_file):
                with open(profile_file, "r", encoding="utf-8") as f:
                    profile = json.load(f)
                profile["last_used"] = datetime.now().isoformat()
                with open(profile_file, "w", encoding="utf-8") as f:
                    json.dump(profile, f, indent=2)
        except Exception as e:
            self.log(f"Error updating profile: {e}")

    def save_current_connection(self, ssid, interface):
        """Save current connection info."""
        current = {
            "ssid": ssid,
            "interface": interface,
            "connected_at": datetime.now().isoformat(),
        }
        try:
            with open(self.current_profile_file, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2)
        except Exception as e:
            self.log(f"Error saving current connection: {e}")

    def set_selected_interface(self, iface):
        """Set the user-selected interface (eth0 or wlan*)."""
        self.selected_interface = iface
        self.log(f"Active interface set to: {iface}")

    def get_active_wifi_interface(self):
        """Return the user-selected WiFi interface, or first available wireless adapter."""
        if self.selected_interface and self.selected_interface in self.wifi_interfaces:
            return self.selected_interface
        return self.wifi_interfaces[0] if self.wifi_interfaces else None

    def get_active_interface(self):
        """Return the user-selected interface (including eth0), or first available WiFi adapter."""
        if self.selected_interface == "eth0":
            return "eth0"
        if self.selected_interface and self.selected_interface in self.wifi_interfaces:
            return self.selected_interface
        return self.wifi_interfaces[0] if self.wifi_interfaces else None

    def get_interface_for_tool(self, preferred="auto"):
        """Get the best interface for network tools."""
        if preferred == "auto":
            if self.selected_interface:
                return self.selected_interface
            status = self.get_connection_status()
            if status.get("status") == "connected":
                return status["interface"]
            return "eth0"
        return preferred


# Global WiFi manager instance
wifi_manager = WiFiManager()


def get_available_interfaces():
    """Get list of available network interfaces for RaspyJack tools."""
    interfaces = ["eth0"]
    interfaces.extend(wifi_manager.wifi_interfaces)
    return interfaces


def get_current_interface():
    """Get the currently active interface for tools."""
    return wifi_manager.get_interface_for_tool()
