#!/usr/bin/env python3
"""
RaspyJack Bluetooth & Audio Output Engine
-----------------------------------------
Pure-stdlib backend for scanning, pairing, trusting, connecting, disconnecting,
and forgetting Bluetooth devices (headphones, speakers, headsets, keyboards,
mice, controllers) plus automatic PipeWire / PulseAudio / BlueALSA audio sink
routing and volume control.

Zero external Python dependencies (uses system CLI tools: bluetoothctl, rfkill,
pactl, wpctl, amixer, paplay/pw-play/aplay).
"""

import json
import math
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import termios
import time
import wave
from typing import Callable, Dict, List, Optional, Tuple

try:
    import grp
    import pwd
except ImportError:  # pragma: no cover
    grp = None
    pwd = None

AUDIO_STATE_FILE = os.environ.get("RJ_AUDIO_STATE_FILE", "/dev/shm/rj_audio_output.json")

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_MAC_RE = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")

# Standard Bluetooth Service UUID prefixes for reliable device classification
_AUDIO_SINK_UUIDS = ("0000110b", "0000110a", "0000110d")
_HEADSET_UUIDS = ("00001108", "0000111e", "0000111f", "00001112")
_HID_UUIDS = ("00001124", "00001812")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences and carriage returns from CLI output."""
    if not text:
        return ""
    return _ANSI_RE.sub("", text).replace("\r", "")


def _lookup_uid_info(uid: int) -> dict:
    """Resolve username, primary gid, supplementary groups, and home dir for *uid*."""
    info = {
        "uid": uid,
        "gid": uid,
        "user": "",
        "home": "",
        "extra_groups": [],
    }
    if pwd is None:
        return info
    try:
        pw = pwd.getpwuid(uid)
        info["uid"] = pw.pw_uid
        info["gid"] = pw.pw_gid
        info["user"] = pw.pw_name
        info["home"] = pw.pw_dir
        groups = [pw.pw_gid]
        if grp is not None:
            for g in grp.getgrall():
                if pw.pw_name in g.gr_mem and g.gr_gid not in groups:
                    groups.append(g.gr_gid)
        info["extra_groups"] = groups
    except Exception:
        pass
    return info


def get_audio_session_info() -> dict:
    """Discover the active PipeWire / PulseAudio user runtime session (even when running as root).

    Returns a dict with runtime_dir, pulse_sock, pw_sock, dbus_sock, pulse_cookie,
    and socket owner uid/gid/user/home/extra_groups so root services (like
    raspyjack.service) can authenticate cleanly to a desktop/lingering user's
    PipeWire or PulseAudio daemon.
    """
    env = os.environ
    candidates: List[Tuple[str, Optional[int]]] = []

    cur_rdir = env.get("XDG_RUNTIME_DIR", "").strip()
    if cur_rdir and os.path.isdir(cur_rdir):
        try:
            st = os.stat(cur_rdir)
            candidates.append((cur_rdir, st.st_uid))
        except OSError:
            candidates.append((cur_rdir, None))

    sudo_uid = env.get("SUDO_UID", "").strip()
    if sudo_uid.isdigit():
        rdir = f"/run/user/{sudo_uid}"
        if not any(c[0] == rdir for c in candidates):
            candidates.append((rdir, int(sudo_uid)))

    try:
        if os.path.isdir("/run/user"):
            # Prefer non-zero desktop/Pi user runtime dirs (e.g. /run/user/1000) before uid 0
            raw_uids = [name for name in os.listdir("/run/user") if name.isdigit()]
            raw_uids.sort(key=lambda u: (int(u) == 0, int(u)))
            for name in raw_uids:
                rdir = f"/run/user/{name}"
                if not any(c[0] == rdir for c in candidates):
                    candidates.append((rdir, int(name)))
    except OSError:
        pass

    for rdir, hint_uid in candidates:
        pulse_sock = os.path.join(rdir, "pulse", "native")
        pw_sock = os.path.join(rdir, "pipewire-0")
        has_pulse = os.path.exists(pulse_sock)
        has_pw = os.path.exists(pw_sock)
        if not (has_pulse or has_pw):
            continue
        owner_uid = hint_uid
        for p in (pulse_sock, pw_sock, rdir):
            if os.path.exists(p):
                try:
                    owner_uid = os.stat(p).st_uid
                    break
                except OSError:
                    pass
        if owner_uid is None:
            owner_uid = os.geteuid() if hasattr(os, "geteuid") else 0
        uinfo = _lookup_uid_info(owner_uid)
        dbus_sock = os.path.join(rdir, "bus")
        cookie_path = os.path.join(uinfo["home"], ".config", "pulse", "cookie") if uinfo["home"] else ""
        return {
            "found": True,
            "runtime_dir": rdir,
            "pulse_sock": pulse_sock if has_pulse else "",
            "pw_sock": pw_sock if has_pw else "",
            "dbus_sock": dbus_sock if os.path.exists(dbus_sock) else "",
            "pulse_cookie": cookie_path if (cookie_path and os.path.isfile(cookie_path)) else "",
            "uid": uinfo["uid"],
            "gid": uinfo["gid"],
            "user": uinfo["user"],
            "home": uinfo["home"],
            "extra_groups": uinfo["extra_groups"],
        }

    return {
        "found": False,
        "runtime_dir": cur_rdir,
        "pulse_sock": "",
        "pw_sock": "",
        "dbus_sock": "",
        "pulse_cookie": "",
        "uid": os.geteuid() if hasattr(os, "geteuid") else 0,
        "gid": os.getegid() if hasattr(os, "getegid") else 0,
        "user": "",
        "home": env.get("HOME", ""),
        "extra_groups": [],
    }


def get_audio_env() -> dict:
    """Return an environment capable of talking to PipeWire/PulseAudio even under sudo/root."""
    env = os.environ.copy()
    sess = get_audio_session_info()
    if sess["found"]:
        env["XDG_RUNTIME_DIR"] = sess["runtime_dir"]
        if sess["pulse_sock"]:
            env["PULSE_SERVER"] = f"unix:{sess['pulse_sock']}"
        if sess["pulse_cookie"]:
            env["PULSE_COOKIE"] = sess["pulse_cookie"]
        if sess["dbus_sock"]:
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={sess['dbus_sock']}"
        if hasattr(os, "geteuid") and os.geteuid() == 0 and sess["uid"] != 0:
            if sess["home"]:
                env["HOME"] = sess["home"]
            if sess["user"]:
                env["USER"] = sess["user"]
                env["LOGNAME"] = sess["user"]
    return env


def get_audio_popen_kwargs(drop_to_audio_user: bool = True) -> dict:
    """Return subprocess keyword args (`env`, plus `user`/`group` when root talks to a user PipeWire socket).

    PipeWire/WirePlumber checks peer credentials (SO_PEERCRED) on `/run/user/<uid>/pipewire-0`
    and `/run/user/<uid>/pulse/native`, often rejecting UID 0 (`root`). Dropping the spawned
    audio client (`pactl`, `wpctl`, `paplay`, `pw-play`, `ffmpeg -f pulse`) to the socket
    owner's UID/GID authenticates cleanly with zero config needed.
    """
    sess = get_audio_session_info()
    kwargs: dict = {"env": get_audio_env()}
    if (
        drop_to_audio_user
        and sess["found"]
        and hasattr(os, "geteuid")
        and os.geteuid() == 0
        and sess["uid"] != 0
    ):
        kwargs["user"] = sess["uid"]
        kwargs["group"] = sess["gid"]
        if sess["extra_groups"]:
            kwargs["extra_groups"] = sess["extra_groups"]
    return kwargs


def _run_cmd(cmd: List[str], timeout: float = 5.0, use_audio_env: bool = False) -> Tuple[int, str, str]:
    try:
        extra_kw = get_audio_popen_kwargs(drop_to_audio_user=True) if use_audio_env else {}
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                **extra_kw,
            )
        except (PermissionError, OSError):
            if "user" not in extra_kw:
                raise
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=extra_kw.get("env"),
            )
        return r.returncode, strip_ansi(r.stdout or ""), strip_ansi(r.stderr or "")
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "", "command timed out"
    except Exception as exc:
        return 1, "", str(exc)


def is_bluetoothctl_available() -> bool:
    return shutil.which("bluetoothctl") is not None


def classify_device_type(icon: str = "", name: str = "", uuids: Optional[List[str]] = None) -> str:
    """Classify a Bluetooth device into headphones/speaker/keyboard/mouse/gamepad/phone/device."""
    ic = (icon or "").lower().strip()
    nm = (name or "").lower().strip()
    uuid_blob = " ".join(u.lower() for u in (uuids or []))

    if any(k in ic for k in ("headphone", "headset")):
        return "headphones"
    if any(k in ic for k in ("audio-card", "audio-speaker", "multimedia-player", "audio")):
        if any(k in nm for k in ("pod", "bud", "headphone", "headset", "wh-", "wf-", "qc", "beats", "airpod", "ear")):
            return "headphones"
        return "speaker"
    if "keyboard" in ic:
        return "keyboard"
    if "mouse" in ic or "trackpad" in ic:
        return "mouse"
    if "gaming" in ic or "joystick" in ic:
        return "gamepad"
    if "phone" in ic:
        return "phone"

    # Fall back to advertised Bluetooth service UUIDs + device name hints
    has_audio = any(u in uuid_blob for u in _AUDIO_SINK_UUIDS) or any(u in uuid_blob for u in _HEADSET_UUIDS)
    if has_audio:
        if any(k in nm for k in ("speaker", "soundbar", "boom", "jbl", "flip", "charge", "bose soundlink", "marshall", "sonos", "echo")):
            return "speaker"
        return "headphones"
    if any(u in uuid_blob for u in _HID_UUIDS):
        if any(k in nm for k in ("mouse", "trackball", "mx master", "mx anywhere", "deathadder")):
            return "mouse"
        if any(k in nm for k in ("controller", "gamepad", "xbox", "dualsense", "dualshock", "8bitdo", "joy-con")):
            return "gamepad"
        return "keyboard"

    if any(k in nm for k in ("airpod", "earbud", "buds", "headphone", "headset", "wh-1000", "wf-1000", "beats", "jabra", "sennheiser", "anker soundcore")):
        return "headphones"
    if any(k in nm for k in ("speaker", "jbl", "soundbar", "boombox", "soundcore")):
        return "speaker"
    if any(k in nm for k in ("keyboard", "kbd", "keychron", "hhkb")):
        return "keyboard"
    if any(k in nm for k in ("mouse", "trackpad")):
        return "mouse"
    return "device"


def is_audio_device(dev: dict) -> bool:
    return dev.get("device_type") in ("headphones", "speaker")


def device_type_badge(device_type: str) -> str:
    return {
        "headphones": "HP",
        "speaker": "SPK",
        "keyboard": "KB",
        "mouse": "MSE",
        "gamepad": "PAD",
        "phone": "PHN",
    }.get(device_type, "BT")


def parse_adapter_show(output: str) -> dict:
    """Parse `bluetoothctl show` output into an adapter status dict."""
    clean = strip_ansi(output)
    info = {
        "available": False,
        "powered": False,
        "discoverable": False,
        "pairable": False,
        "discovering": False,
        "name": "Bluetooth",
        "address": "",
        "rfkill_blocked": False,
    }
    if not clean or "No default controller available" in clean:
        return info
    m_ctrl = re.search(r"Controller\s+([0-9A-Fa-f:]{17})", clean)
    if m_ctrl:
        info["available"] = True
        info["address"] = m_ctrl.group(1).upper()
    for raw_line in clean.splitlines():
        line = raw_line.strip()
        if ":" not in line:
            continue
        k, v = [p.strip() for p in line.split(":", 1)]
        kl = k.lower()
        vl = v.lower()
        if kl in ("alias", "name") and v:
            info["name"] = v
        elif kl == "powered":
            info["powered"] = vl == "yes"
        elif kl == "discoverable":
            info["discoverable"] = vl == "yes"
        elif kl == "pairable":
            info["pairable"] = vl == "yes"
        elif kl == "discovering":
            info["discovering"] = vl == "yes"
    return info


def get_adapter_status() -> dict:
    """Query local Bluetooth controller status and rfkill state."""
    if not is_bluetoothctl_available():
        return {
            "available": False,
            "powered": False,
            "discoverable": False,
            "pairable": False,
            "discovering": False,
            "name": "No bluetoothctl",
            "address": "",
            "rfkill_blocked": False,
        }
    _rc, out, _err = _run_cmd(["bluetoothctl", "show"], timeout=4.0)
    info = parse_adapter_show(out)
    # Check rfkill soft/hard block
    if shutil.which("rfkill"):
        rc_rf, out_rf, _ = _run_cmd(["rfkill", "list", "bluetooth"], timeout=2.0)
        if rc_rf == 0 and ("Soft blocked: yes" in out_rf or "Hard blocked: yes" in out_rf):
            info["rfkill_blocked"] = True
    return info


def set_power(on: bool) -> Tuple[bool, str]:
    """Turn Bluetooth adapter on or off (unblocking rfkill if needed)."""
    if not is_bluetoothctl_available():
        return False, "bluetoothctl not installed"
    if on and shutil.which("rfkill"):
        _run_cmd(["rfkill", "unblock", "bluetooth"], timeout=3.0)
        time.sleep(0.25)
    rc, out, err = _run_cmd(["bluetoothctl", "power", "on" if on else "off"], timeout=5.0)
    combined = (out + " " + err).strip()
    if rc == 0 and "Failed" not in combined and "No default controller" not in combined:
        return True, "Bluetooth ON" if on else "Bluetooth OFF"
    return False, (combined or "Power change failed")[:48]


def parse_device_info(mac: str, info_output: str, fallback_name: str = "") -> dict:
    """Parse `bluetoothctl info <MAC>` output into structured device metadata."""
    clean = strip_ansi(info_output)
    mac_u = mac.upper()
    name = fallback_name.strip() or mac_u
    alias = ""
    icon = ""
    paired = False
    bonded = False
    trusted = False
    connected = False
    blocked = False
    rssi: Optional[int] = None
    battery: Optional[int] = None
    uuids: List[str] = []

    for raw_line in clean.splitlines():
        line = raw_line.strip()
        if ":" not in line:
            continue
        k, v = [p.strip() for p in line.split(":", 1)]
        kl = k.lower()
        vl = v.lower()
        if kl == "name" and v:
            name = v
        elif kl == "alias" and v:
            alias = v
        elif kl == "icon" and v:
            icon = v
        elif kl == "paired":
            paired = vl == "yes"
        elif kl == "bonded":
            bonded = vl == "yes"
        elif kl == "trusted":
            trusted = vl == "yes"
        elif kl == "connected":
            connected = vl == "yes"
        elif kl == "blocked":
            blocked = vl == "yes"
        elif kl == "rssi":
            # Handles "0xffffffc4 (-60)" or "-60"
            m_paren = re.search(r"\((-?\d+)\)", v)
            if m_paren:
                rssi = int(m_paren.group(1))
            else:
                m_num = re.search(r"(-?\d+)", v)
                if m_num:
                    val = int(m_num.group(1))
                    rssi = val if val <= 0 else None
        elif kl == "battery percentage":
            m_bat = re.search(r"\((\d+)\)", v)
            if m_bat:
                battery = max(0, min(100, int(m_bat.group(1))))
            else:
                m_hex = re.search(r"0x([0-9a-fA-F]+)", v)
                if m_hex:
                    battery = max(0, min(100, int(m_hex.group(1), 16)))
        elif kl == "uuid":
            uuids.append(v)

    display_name = alias or name or mac_u
    if display_name.replace("-", ":").upper() == mac_u and name and name.replace("-", ":").upper() != mac_u:
        display_name = name

    dev_type = classify_device_type(icon=icon, name=display_name, uuids=uuids)
    return {
        "mac": mac_u,
        "name": display_name,
        "icon": icon,
        "device_type": dev_type,
        "paired": paired or bonded,
        "trusted": trusted,
        "connected": connected,
        "blocked": blocked,
        "rssi": rssi,
        "battery": battery,
        "uuids": uuids,
    }


def _parse_devices_lines(output: str) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for raw_line in strip_ansi(output).splitlines():
        m = re.search(r"Device\s+([0-9A-Fa-f:]{17})\s*(.*)", raw_line)
        if m:
            mac = m.group(1).upper()
            nm = m.group(2).strip()
            if mac not in found or (nm and nm.replace("-", ":").upper() != mac):
                found[mac] = nm or mac
    return found


def _sort_devices(devices: List[dict]) -> List[dict]:
    def _key(d: dict):
        is_unnamed = d["name"].replace("-", ":").upper() == d["mac"].upper()
        return (
            0 if d.get("connected") else 1,
            0 if is_audio_device(d) else 1,
            0 if d.get("paired") else 1,
            0 if not is_unnamed else 1,
            -(d["rssi"] if d.get("rssi") is not None else -120),
            d["name"].lower(),
        )
    return sorted(devices, key=_key)


def get_device_info(mac: str, fallback_name: str = "") -> dict:
    _rc, out, _ = _run_cmd(["bluetoothctl", "info", mac], timeout=3.0)
    return parse_device_info(mac, out, fallback_name=fallback_name)


def list_known_devices() -> List[dict]:
    """List paired, connected, and cached Bluetooth devices with full info."""
    if not is_bluetoothctl_available():
        return []
    seen: Dict[str, str] = {}
    for args in (["bluetoothctl", "devices"], ["bluetoothctl", "devices", "Paired"], ["bluetoothctl", "devices", "Connected"]):
        rc, out, _ = _run_cmd(args, timeout=3.0)
        if rc == 0:
            for mac, nm in _parse_devices_lines(out).items():
                if mac not in seen or (nm and nm.replace("-", ":").upper() != mac):
                    seen[mac] = nm

    devices = [get_device_info(mac, fallback_name=nm) for mac, nm in seen.items()]
    return _sort_devices(devices)


class _BluetoothCtlSession:
    """PTY-backed interactive bluetoothctl controller (handles prompts without newlines!)."""

    def __init__(self):
        self.master_fd: Optional[int] = None
        self.proc: Optional[subprocess.Popen] = None
        self._buf: str = ""

    def start(self, cmd: Optional[List[str]] = None) -> bool:
        try:
            master_fd, slave_fd = pty.openpty()
        except OSError:
            return False
        # Disable TTY echo on slave so sent commands don't mix into parsed output
        try:
            attrs = termios.tcgetattr(slave_fd)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
        except Exception:
            pass

        def _child_setup() -> None:
            os.setsid()
            try:
                import fcntl
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            except Exception:
                pass

        try:
            env = os.environ.copy()
            env["TERM"] = "dumb"
            env["LC_ALL"] = "C"
            self.proc = subprocess.Popen(
                cmd or ["bluetoothctl"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                preexec_fn=_child_setup,
                close_fds=True,
            )
            os.close(slave_fd)
            os.set_blocking(master_fd, False)
            self.master_fd = master_fd
            return True
        except Exception:
            try:
                os.close(master_fd)
            except OSError:
                pass
            try:
                os.close(slave_fd)
            except OSError:
                pass
            return False

    def send(self, cmd: str) -> None:
        if self.master_fd is None:
            return
        try:
            os.write(self.master_fd, (cmd + "\n").encode("utf-8"))
        except OSError:
            pass

    def poll_output(self, timeout: float = 0.2) -> Tuple[List[str], str]:
        """Read available PTY output; return (complete_lines, current_partial_prompt)."""
        if self.master_fd is None:
            return [], ""
        try:
            ready, _, _ = select.select([self.master_fd], [], [], timeout)
        except (OSError, ValueError):
            return [], self._buf
        if ready:
            try:
                chunk = os.read(self.master_fd, 4096)
                if chunk:
                    self._buf += strip_ansi(chunk.decode("utf-8", "ignore"))
            except (BlockingIOError, OSError):
                pass
        lines: List[str] = []
        while "\n" in self._buf:
            ln, self._buf = self._buf.split("\n", 1)
            s = ln.strip()
            if s:
                lines.append(s)
        return lines, self._buf.strip()

    def clear_partial(self) -> None:
        self._buf = ""

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self) -> None:
        try:
            self.send("quit")
        except Exception:
            pass
        if self.proc is not None:
            try:
                self.proc.wait(timeout=1.5)
            except Exception:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=1.0)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
            self.proc = None
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None


def _is_yes_no_prompt(text: str) -> bool:
    low = (text or "").lower()
    return any(
        k in low
        for k in (
            "(yes/no)",
            "confirm passkey",
            "request confirmation",
            "accept pairing",
            "authorize service",
            "allow service",
        )
    )


def scan_devices(
    duration: float = 8.0,
    on_progress: Optional[Callable[[float, int, str], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
) -> List[dict]:
    """Run an active PTY-backed Bluetooth scan for *duration* seconds."""
    if not is_bluetoothctl_available():
        return []

    if shutil.which("rfkill"):
        _run_cmd(["rfkill", "unblock", "bluetooth"], timeout=2.0)

    sess = _BluetoothCtlSession()
    if not sess.start():
        return list_known_devices()

    seen: Dict[str, str] = {}
    latest_name = ""
    start = time.monotonic()

    try:
        sess.send("power on")
        time.sleep(0.25)
        sess.send("pairable on")
        sess.send("scan on")

        while True:
            elapsed = time.monotonic() - start
            if elapsed >= duration:
                break
            if stop_check is not None and stop_check():
                break

            lines, _partial = sess.poll_output(timeout=0.15)
            for line in lines:
                m = re.search(r"Device\s+([0-9A-Fa-f:]{17})\s+(.+)", line)
                if m:
                    mac = m.group(1).upper()
                    tail = m.group(2).strip()
                    # Ignore property update lines like "RSSI: -65" or "UUIDs: ..."
                    if ":" not in tail or tail.replace("-", ":").upper() == mac:
                        if mac not in seen or tail.replace("-", ":").upper() != mac:
                            seen[mac] = tail
                            if tail.replace("-", ":").upper() != mac:
                                latest_name = tail
                    elif mac not in seen:
                        seen[mac] = mac
            if not sess.is_alive():
                break
            if on_progress is not None:
                on_progress(min(1.0, elapsed / max(0.5, duration)), len(seen), latest_name)
    finally:
        sess.send("scan off")
        time.sleep(0.2)
        sess.close()

    # Merge scanned devices with known paired/connected devices and fetch full info
    known = {d["mac"]: d for d in list_known_devices()}
    for mac, nm in seen.items():
        if mac not in known:
            known[mac] = get_device_info(mac, fallback_name=nm)
    return _sort_devices(list(known.values()))


def _save_audio_state(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(AUDIO_STATE_FILE) or "/dev/shm", exist_ok=True)
        tmp = f"{AUDIO_STATE_FILE}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
        os.replace(tmp, AUDIO_STATE_FILE)
        try:
            os.chmod(AUDIO_STATE_FILE, 0o666)
        except OSError:
            pass
    except Exception:
        pass


def load_audio_state() -> dict:
    try:
        if os.path.isfile(AUDIO_STATE_FILE):
            with open(AUDIO_STATE_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def parse_pactl_sinks(short_output: str, default_sink: str = "") -> List[dict]:
    """Parse `pactl list short sinks` output into structured sink entries."""
    sinks: List[dict] = []
    for raw_line in strip_ansi(short_output).splitlines():
        parts = raw_line.strip().split("\t")
        if len(parts) < 2:
            parts = raw_line.strip().split()
        if len(parts) < 2:
            continue
        sink_id = parts[0]
        name = parts[1]
        state = parts[4] if len(parts) >= 5 else ""
        is_bt = "bluez" in name.lower()
        # Extract friendly label from sink name
        label = name
        if is_bt:
            m_mac = re.search(r"([0-9A-Fa-f]{2}(?:_[0-9A-Fa-f]{2}){5})", name)
            mac_str = m_mac.group(1).replace("_", ":").upper() if m_mac else ""
            label = f"BT {mac_str}" if mac_str else "Bluetooth Audio"
        elif "hdmi" in name.lower():
            label = "HDMI Audio"
        elif "usb" in name.lower():
            label = "USB Audio"
        elif "analog" in name.lower() or "platform" in name.lower():
            label = "Headphone / Speaker"
        sinks.append({
            "id": sink_id,
            "name": name,
            "label": label,
            "state": state,
            "is_bluetooth": is_bt,
            "is_default": (name == default_sink),
        })
    return sinks


def parse_wpctl_sinks(status_output: str) -> List[dict]:
    """Parse PipeWire `wpctl status` Audio -> Sinks section into structured sink entries."""
    sinks: List[dict] = []
    in_audio = False
    in_sinks = False
    for raw_line in strip_ansi(status_output).splitlines():
        clean = raw_line.rstrip()
        stripped = clean.strip()
        if not stripped:
            continue
        if stripped == "Audio":
            in_audio = True
            in_sinks = False
            continue
        if in_audio and stripped in ("Video", "Settings"):
            break
        if not in_audio:
            continue
        if "Sinks:" in stripped:
            in_sinks = True
            continue
        if in_sinks:
            if any(sec in stripped for sec in ("Sources:", "Filters:", "Streams:", "Endpoints:", "Devices:")):
                in_sinks = False
                continue
            m = re.search(r"(\*)?\s*(\d+)\.\s+(.+?)(?:\s+\[vol:[^\]]*\])?$", stripped)
            if not m:
                continue
            is_def = bool(m.group(1))
            sink_id = m.group(2)
            raw_lbl = m.group(3).strip()
            low = raw_lbl.lower()
            is_bt = any(k in low for k in ("bluez", "bluetooth", "a2dp", "headset", "airpod", "buds", "wh-"))
            label = f"BT: {raw_lbl}" if (is_bt and not low.startswith("bt")) else raw_lbl
            sinks.append({
                "id": sink_id,
                "name": f"wpctl:{sink_id}",
                "label": label,
                "state": "RUNNING" if is_def else "IDLE",
                "is_bluetooth": is_bt,
                "is_default": is_def,
                "backend": "wpctl",
            })
    return sinks


def parse_alsa_playback_devices(aplay_l_output: str, selected_alsa_dev: str = "") -> List[dict]:
    """Parse `aplay -l` output into structured ALSA playback device entries."""
    devices: List[dict] = []
    seen = set()
    pattern = re.compile(
        r"card\s+(\d+):\s*([^\[]+?)\s*\[([^\]]+)\],\s*device\s+(\d+):\s*([^\[]+?)(?:\s*\[([^\]]+)\])?\s*$",
        re.IGNORECASE,
    )
    for raw_line in strip_ansi(aplay_l_output).splitlines():
        line = raw_line.strip()
        m = pattern.match(line)
        if not m:
            continue
        card_num = m.group(1).strip()
        card_id = m.group(2).strip()
        card_name = m.group(3).strip()
        dev_num = m.group(4).strip()
        dev_name = (m.group(6) or m.group(5) or "").strip()
        alsa_dev = f"plughw:{card_num},{dev_num}"
        if alsa_dev in seen:
            continue
        seen.add(alsa_dev)

        blob = f"{card_id} {card_name} {dev_name}".upper()
        if "VC4HDMI0" in blob or "VC4-HDMI-0" in blob:
            label = "HDMI 0 (vc4-hdmi-0)"
        elif "VC4HDMI1" in blob or "VC4-HDMI-1" in blob:
            label = "HDMI 1 (vc4-hdmi-1)"
        elif "HDMI" in blob:
            label = f"HDMI ({card_name})"
        elif "HEADPHONE" in blob:
            label = "3.5mm Headphones"
        elif "USB" in blob:
            label = f"USB: {card_name}"
        elif any(k in blob for k in ("ES8388", "ES8389", "ES8390")):
            label = "Built-in Speaker"
        else:
            label = card_name or f"ALSA Card {card_num}"

        devices.append({
            "id": f"{card_num},{dev_num}",
            "name": f"alsa:{alsa_dev}",
            "alsa_dev": alsa_dev,
            "card": card_num,
            "device": dev_num,
            "card_id": card_id,
            "card_name": card_name,
            "dev_name": dev_name,
            "label": label,
            "state": "IDLE",
            "is_bluetooth": False,
            "is_default": (alsa_dev == selected_alsa_dev),
            "backend": "alsa",
        })
    return devices


def list_alsa_audio_sinks() -> List[dict]:
    """Return ALSA hardware (and BlueALSA if available) output sinks as fallback when PipeWire isn't up."""
    if not shutil.which("aplay"):
        return []
    st = load_audio_state()
    selected_alsa = st.get("alsa_dev", "")
    rc, out, _ = _run_cmd(["aplay", "-l"], timeout=2.5, use_audio_env=False)
    sinks = parse_alsa_playback_devices(out, selected_alsa_dev=selected_alsa) if rc == 0 else []

    # Expose BlueALSA Bluetooth sink if bluez-alsa-utils is installed and a BT device is saved
    bt_mac = st.get("bt_mac", "")
    if bt_mac:
        rc_l, out_l, _ = _run_cmd(["aplay", "-L"], timeout=2.0, use_audio_env=False)
        if rc_l == 0 and "bluealsa" in out_l.lower():
            bt_alsa = f"bluealsa:DEV={bt_mac},PROFILE=a2dp"
            sinks.insert(0, {
                "id": "bt",
                "name": f"alsa:{bt_alsa}",
                "alsa_dev": bt_alsa,
                "card": "",
                "device": "",
                "card_id": "bluealsa",
                "card_name": "BlueALSA",
                "dev_name": bt_mac,
                "label": f"BT: {st.get('bt_name') or bt_mac}",
                "state": "IDLE",
                "is_bluetooth": True,
                "is_default": (selected_alsa == bt_alsa) or (not selected_alsa),
                "backend": "alsa",
            })

    if sinks and not any(s.get("is_default") for s in sinks):
        sinks[0]["is_default"] = True
    return sinks


def list_audio_sinks() -> List[dict]:
    """Return available audio output sinks (PipeWire/PulseAudio, WirePlumber wpctl, or ALSA hardware)."""
    # 1. PipeWire-Pulse / PulseAudio via pactl (authenticated as socket owner)
    if shutil.which("pactl"):
        rc_def, out_def, _ = _run_cmd(["pactl", "get-default-sink"], timeout=2.5, use_audio_env=True)
        default_sink = out_def.strip() if rc_def == 0 else ""

        rc, out, _ = _run_cmd(["pactl", "list", "short", "sinks"], timeout=2.5, use_audio_env=True)
        if rc == 0:
            sinks = parse_pactl_sinks(out, default_sink=default_sink)
            rc_full, out_full, _ = _run_cmd(["pactl", "list", "sinks"], timeout=3.0, use_audio_env=True)
            if rc_full == 0:
                cur_name = ""
                desc_map: Dict[str, str] = {}
                for line in out_full.splitlines():
                    ls = line.strip()
                    if ls.startswith("Name:"):
                        cur_name = ls.split(":", 1)[1].strip()
                    elif ls.startswith("Description:") and cur_name:
                        desc_map[cur_name] = ls.split(":", 1)[1].strip()
                for s in sinks:
                    s["backend"] = "pactl"
                    if s["name"] in desc_map:
                        desc = desc_map[s["name"]]
                        s["label"] = f"BT: {desc}" if (s["is_bluetooth"] and not desc.lower().startswith("bt")) else desc
            if sinks:
                return sinks

    # 2. Pure PipeWire / WirePlumber via wpctl (no pulseaudio-utils needed)
    if shutil.which("wpctl"):
        rc_w, out_w, _ = _run_cmd(["wpctl", "status"], timeout=2.5, use_audio_env=True)
        if rc_w == 0:
            sinks = parse_wpctl_sinks(out_w)
            if sinks:
                return sinks

    # 3. Direct ALSA hardware playback devices (HDMI 0/1, 3.5mm Headphones, USB, BlueALSA)
    return list_alsa_audio_sinks()


_A2DP_PROFILE_PREFERENCE = (
    "a2dp-sink",
    "a2dp_sink",
    "a2dp-sink-aac",
    "a2dp-sink-sbc",
    "a2dp-sink-sbc_xq",
    "a2dp-sink-aptx_hd",
    "a2dp-sink-aptx",
    "a2dp-sink-ldac",
)


def select_best_a2dp_profile(available_profiles: List[str]) -> Optional[str]:
    """Choose the highest-quality stereo A2DP playback profile from available card profiles.

    Never selects low-bandwidth mono HFP/HSP headset profiles (`headset-head-unit*`).
    """
    if not available_profiles:
        return None
    norm_map = {p.strip().lower(): p.strip() for p in available_profiles if p and p.strip()}
    for pref in _A2DP_PROFILE_PREFERENCE:
        if pref in norm_map:
            return norm_map[pref]
    # Fallback: any profile containing a2dp and sink (excluding input/headset/off)
    for low, orig in norm_map.items():
        if "a2dp" in low and "sink" in low:
            return orig
    return None


def _parse_bluez_card_profiles(cards_output: str) -> List[dict]:
    """Extract bluez card names, active profile, and available profiles from `pactl list cards`."""
    cards: List[dict] = []
    cur: Optional[dict] = None
    in_profiles = False
    for raw in strip_ansi(cards_output).splitlines():
        s = raw.strip()
        if re.match(r"^Card\s+#\d+", s):
            if cur and cur.get("name"):
                cards.append(cur)
            cur = {"name": "", "active_profile": "", "profiles": []}
            in_profiles = False
            continue
        if cur is None:
            continue
        if s.startswith("Name:"):
            cur["name"] = s.split(":", 1)[1].strip()
            in_profiles = False
        elif s.startswith("Profiles:"):
            in_profiles = True
        elif s.startswith("Active Profile:"):
            cur["active_profile"] = s.split(":", 1)[1].strip()
            in_profiles = False
        elif s.startswith("Ports:"):
            in_profiles = False
        elif in_profiles and ":" in s:
            if "available: no" in s.lower():
                continue
            pname = s.split(":", 1)[0].strip()
            if pname:
                cur["profiles"].append(pname)
    if cur and cur.get("name"):
        cards.append(cur)
    return [c for c in cards if "bluez" in c["name"].lower()]


def ensure_bluetooth_a2dp_profile(sink_or_card: str = "", mac: str = "") -> Optional[str]:
    """Ensure a Bluetooth audio card is using high-fidelity A2DP Stereo (not 16kHz HFP/HSP!).

    Returns the resolved A2DP `bluez_output.*` sink name if a profile switch occurred
    and a fresh A2DP sink appeared, otherwise None.
    """
    if not shutil.which("pactl"):
        return None
    target_token = ""
    if mac:
        target_token = mac.upper().replace(":", "_")
    elif sink_or_card:
        m_mac = re.search(r"([0-9A-Fa-f]{2}(?:_[0-9A-Fa-f]{2}){5})", sink_or_card)
        if m_mac:
            target_token = m_mac.group(1).upper()

    rc, out, _ = _run_cmd(["pactl", "list", "cards"], timeout=2.5, use_audio_env=True)
    if rc != 0:
        return None
    bt_cards = _parse_bluez_card_profiles(out)
    if not bt_cards:
        return None

    matched_card = None
    for c in bt_cards:
        if target_token and target_token in c["name"].upper():
            matched_card = c
            break
    if matched_card is None and len(bt_cards) == 1 and ("bluez" in (sink_or_card or "").lower() or mac):
        matched_card = bt_cards[0]
    if matched_card is None:
        return None

    active_p = (matched_card.get("active_profile") or "").lower()
    if "a2dp" in active_p and "sink" in active_p:
        return None  # Already in crisp stereo A2DP mode

    best_a2dp = select_best_a2dp_profile(matched_card.get("profiles", []))
    if not best_a2dp:
        return None

    rc_set, _, _ = _run_cmd(
        ["pactl", "set-card-profile", matched_card["name"], best_a2dp],
        timeout=3.0, use_audio_env=True,
    )
    if rc_set != 0:
        return None

    # Wait briefly (up to 1.8s) for PipeWire to recreate the stereo A2DP sink node
    deadline = time.monotonic() + 1.8
    while time.monotonic() < deadline:
        rc_s, out_s, _ = _run_cmd(["pactl", "list", "short", "sinks"], timeout=2.0, use_audio_env=True)
        if rc_s == 0:
            for s in parse_pactl_sinks(out_s):
                if s.get("is_bluetooth") and (not target_token or target_token in s["name"].upper()):
                    if "headset" not in s["name"].lower() and "handsfree" not in s["name"].lower():
                        return s["name"]
        time.sleep(0.2)
    return None


def ensure_default_sink_a2dp() -> Optional[str]:
    """If the active default sink is a Bluetooth HFP/HSP headset sink, upgrade it to A2DP Stereo."""
    if not shutil.which("pactl"):
        return None
    rc, out, _ = _run_cmd(["pactl", "get-default-sink"], timeout=2.0, use_audio_env=True)
    if rc != 0:
        return None
    def_sink = out.strip()
    if "bluez" not in def_sink.lower():
        return None
    upgraded = ensure_bluetooth_a2dp_profile(sink_or_card=def_sink)
    if upgraded:
        _run_cmd(["pactl", "set-default-sink", upgraded], timeout=2.0, use_audio_env=True)
        return upgraded
    return None


def fix_bluetooth_a2dp_conflicts() -> Tuple[bool, str]:
    """Disable bluealsa conflicts, enforce WirePlumber A2DP config, and restart BT + PipeWire.

    Fixes Raspberry Pi OS systems where `bluealsa.service` steals BlueZ A2DP endpoints
    from PipeWire, leaving headphones stuck in muffled 16kHz mono `headset-head-unit`.
    """
    if not shutil.which("systemctl"):
        return False, "systemctl not available"

    stopped_ba = False
    if os.geteuid() == 0:
        for unit in ("bluealsa.service", "bluez-alsa.service", "bluealsa-aplay.service"):
            rc_c, out_c, _ = _run_cmd(["systemctl", "list-unit-files", unit], timeout=2.0)
            if rc_c == 0 and unit in out_c:
                _run_cmd(["systemctl", "stop", unit], timeout=3.0)
                _run_cmd(["systemctl", "disable", unit], timeout=3.0)
                _run_cmd(["systemctl", "mask", unit], timeout=3.0)
                stopped_ba = True

        # Ensure WirePlumber allows headless A2DP & disables headset auto-switch
        try:
            os.makedirs("/etc/wireplumber/wireplumber.conf.d", exist_ok=True)
            os.makedirs("/etc/wireplumber/bluetooth.lua.d", exist_ok=True)
            with open("/etc/wireplumber/wireplumber.conf.d/51-headless-bt.conf", "w", encoding="utf-8") as fh:
                fh.write(
                    "wireplumber.profiles = {\n"
                    "  main = {\n"
                    "    monitor.bluez.seat-monitoring = disabled\n"
                    "  }\n"
                    "}\n\n"
                    "wireplumber.settings = {\n"
                    "  bluetooth.autoswitch-to-headset-profile = false\n"
                    "}\n\n"
                    "monitor.bluez.properties = {\n"
                    "  bluez5.enable-sbc-xq = true\n"
                    "  bluez5.enable-msbc = true\n"
                    "  bluez5.roles = [ a2dp_sink a2dp_source hsp_hs hsp_ag hfp_hf hfp_ag ]\n"
                    "}\n"
                )
            with open("/etc/wireplumber/bluetooth.lua.d/51-headless-bt.lua", "w", encoding="utf-8") as fh:
                fh.write(
                    'bluez_monitor.properties["with-logind"] = false\n'
                    'bluez_monitor.properties["bluez5.autoswitch-to-headset-profile"] = false\n'
                    'bluez_monitor.properties["bluez5.enable-sbc-xq"] = true\n'
                    'bluez_monitor.properties["bluez5.roles"] = "[ a2dp_sink a2dp_source hsp_hs hsp_ag hfp_hf hfp_ag ]"\n'
                )
        except OSError:
            pass

        _run_cmd(["systemctl", "restart", "bluetooth.service"], timeout=6.0)
        time.sleep(0.8)

    sess = get_audio_session_info()
    uname = sess.get("user", "")
    if os.geteuid() == 0 and uname and uname != "root":
        rc, _, err = _run_cmd(
            ["systemctl", f"--machine={uname}@.host", "--user", "restart", "pipewire", "pipewire-pulse", "wireplumber"],
            timeout=8.0,
        )
    else:
        rc, _, err = _run_cmd(
            ["systemctl", "--user", "restart", "pipewire", "pipewire-pulse", "wireplumber"],
            timeout=8.0,
            use_audio_env=True,
        )
    if rc == 0:
        time.sleep(0.8)
        note = "Disabled BlueALSA & " if stopped_ba else ""
        return True, f"{note}Reset BT A2DP — reconnect headphones!"
    return False, (err or "PipeWire restart failed")[:44]


def activate_bluetooth_audio_sink(mac: str, device_name: str = "") -> Tuple[bool, str]:
    """Switch PipeWire/PulseAudio default sink & A2DP profile to the connected Bluetooth device."""
    mac_u = mac.upper()
    mac_us = mac_u.replace(":", "_")
    _save_audio_state({
        "bt_mac": mac_u,
        "bt_name": device_name or mac_u,
        "updated_at": time.time(),
    })

    has_pactl = shutil.which("pactl") is not None
    has_wpctl = shutil.which("wpctl") is not None
    if not has_pactl and not has_wpctl:
        # BlueALSA or raw ALSA fallback — audio_helper reads AUDIO_STATE_FILE
        return True, "Saved BT audio target"

    # Prefer highest-quality available A2DP Stereo sink profile (never HFP/HSP mono!)
    if has_pactl:
        ensure_bluetooth_a2dp_profile(mac=mac_u)

    # Poll briefly (up to 3.5s) for PipeWire/PulseAudio to expose the bluez sink
    deadline = time.monotonic() + 3.5
    chosen_sink = None
    while time.monotonic() < deadline:
        sinks = list_audio_sinks()
        bt_sinks = [x for x in sinks if x.get("is_bluetooth") and x.get("backend") in ("pactl", "wpctl")]
        for s in bt_sinks:
            if (
                mac_us.lower() in s["name"].lower()
                or (device_name and device_name.lower() in s["label"].lower())
                or len(bt_sinks) == 1
            ):
                chosen_sink = s
                break
        if chosen_sink is not None:
            break
        time.sleep(0.35)

    if chosen_sink is None:
        return False, "Connected (no PipeWire BT sink yet)"

    return set_default_audio_sink(chosen_sink["name"], label=chosen_sink["label"], bt_mac=mac_u)


def _unmute_alsa_card(card_num: str) -> None:
    """Best-effort unmute of common ALSA playback controls on *card_num*."""
    if not card_num or not shutil.which("amixer"):
        return
    for ctl in ("Master", "PCM", "Headphone", "Speaker", "Digital", "HDMI"):
        rc, out, _ = _run_cmd(["amixer", "-c", str(card_num), "sget", ctl], timeout=1.5)
        if rc == 0:
            _run_cmd(["amixer", "-c", str(card_num), "sset", ctl, "unmute"], timeout=1.5)
            # If muted at 0%, bump to a sensible 80% so audio is audible
            if "[0%]" in out:
                _run_cmd(["amixer", "-c", str(card_num), "sset", ctl, "80%"], timeout=1.5)


def set_default_audio_sink(sink_name: str, label: str = "", bt_mac: str = "") -> Tuple[bool, str]:
    """Switch the default PipeWire/PulseAudio/ALSA output sink and move active streams."""
    # Direct ALSA hardware or BlueALSA device selection (works without PipeWire!)
    if sink_name.startswith("alsa:"):
        alsa_dev = sink_name[5:].strip()
        if not alsa_dev:
            return False, "Invalid ALSA device"
        m_card = re.search(r"(?:plug)?hw:(\d+)", alsa_dev)
        if m_card:
            _unmute_alsa_card(m_card.group(1))
        st = load_audio_state()
        st.update({
            "sink": sink_name,
            "alsa_dev": alsa_dev,
            "label": label or alsa_dev,
            "backend": "alsa",
            "updated_at": time.time(),
        })
        if bt_mac:
            st["bt_mac"] = bt_mac
        _save_audio_state(st)
        return True, f"Audio -> {(label or alsa_dev)[:28]}"

    # Pure PipeWire / WirePlumber (wpctl)
    if sink_name.startswith("wpctl:"):
        wp_id = sink_name.split(":", 1)[1].strip()
        if not shutil.which("wpctl"):
            return False, "wpctl not available"
        rc, _, err = _run_cmd(["wpctl", "set-default", wp_id], timeout=3.0, use_audio_env=True)
        if rc != 0:
            return False, (err or "Failed to set wpctl sink")[:44]
        _run_cmd(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"], timeout=2.0, use_audio_env=True)
        _save_audio_state({
            "sink": sink_name,
            "label": label or sink_name,
            "bt_mac": bt_mac,
            "backend": "wpctl",
            "updated_at": time.time(),
        })
        return True, f"Audio -> {(label or sink_name)[:28]}"

    # PipeWire-Pulse / PulseAudio (pactl)
    if not shutil.which("pactl"):
        return False, "pactl not available"

    # If switching to a Bluetooth sink, guarantee it is in A2DP Stereo (not 16kHz HFP/HSP!)
    target_sink = sink_name
    if "bluez" in sink_name.lower() or bt_mac:
        upgraded = ensure_bluetooth_a2dp_profile(sink_or_card=sink_name, mac=bt_mac)
        if upgraded:
            target_sink = upgraded

    rc, _, err = _run_cmd(["pactl", "set-default-sink", target_sink], timeout=3.0, use_audio_env=True)
    if rc != 0:
        return False, (err or "Failed to set audio sink")[:44]
    _run_cmd(["pactl", "set-sink-mute", target_sink, "0"], timeout=2.0, use_audio_env=True)

    # Prevent harsh digital overdrive clipping on Bluetooth sinks if volume was >100%
    if "bluez" in target_sink.lower():
        rc_v, out_v, _ = _run_cmd(["pactl", "get-sink-volume", target_sink], timeout=2.0, use_audio_env=True)
        if rc_v == 0:
            m_v = re.search(r"(\d+)%", out_v)
            if m_v and int(m_v.group(1)) > 100:
                _run_cmd(["pactl", "set-sink-volume", target_sink, "100%"], timeout=2.0, use_audio_env=True)

    # Move all active playback streams to the new sink immediately
    rc_in, out_in, _ = _run_cmd(["pactl", "list", "short", "sink-inputs"], timeout=2.0, use_audio_env=True)
    if rc_in == 0:
        for line in out_in.splitlines():
            parts = line.strip().split()
            if parts and parts[0].isdigit():
                _run_cmd(["pactl", "move-sink-input", parts[0], target_sink], timeout=2.0, use_audio_env=True)

    _save_audio_state({
        "sink": target_sink,
        "label": label or target_sink,
        "bt_mac": bt_mac,
        "backend": "pactl",
        "updated_at": time.time(),
    })
    return True, f"Audio -> {(label or target_sink)[:28]}"


def _attempt_pair_in_session(
    sess: _BluetoothCtlSession,
    mac_u: str,
    device_name: str,
    on_status: Optional[Callable[[str, str], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    """Run one discovery-assisted pair attempt inside a live PTY bluetoothctl session."""
    # Ensure BlueZ actively knows about the device (prevents 'Device ... not available' after scan off!)
    rc_i, out_i, _ = _run_cmd(["bluetoothctl", "info", mac_u], timeout=2.5)
    need_rediscover = (rc_i != 0) or ("not available" in out_i.lower()) or ("device " not in out_i.lower())
    if need_rediscover:
        if on_status:
            on_status("Locating headphones...", device_name)
        sess.send("scan on")
        found_t0 = time.monotonic()
        while (time.monotonic() - found_t0) < 7.0:
            if stop_check is not None and stop_check():
                sess.send("scan off")
                return False, "Cancelled"
            lines, _partial = sess.poll_output(timeout=0.2)
            if any(mac_u in ln.upper() for ln in lines):
                break
            rc_chk, out_chk, _ = _run_cmd(["bluetoothctl", "info", mac_u], timeout=1.5)
            if rc_chk == 0 and "not available" not in out_chk.lower() and "device " in out_chk.lower():
                break

    if on_status:
        on_status("Pairing...", device_name)
    sess.clear_partial()
    sess.send(f"pair {mac_u}")

    paired_ok = False
    pair_err = ""
    last_yes_at = 0.0
    p_start = time.monotonic()
    while (time.monotonic() - p_start) < 25.0:
        if stop_check is not None and stop_check():
            return False, "Cancelled"
        lines, partial = sess.poll_output(timeout=0.2)
        chunks = list(lines) + ([partial] if partial else [])
        for text in chunks:
            low = text.lower()
            if _is_yes_no_prompt(text):
                now = time.monotonic()
                if (now - last_yes_at) > 0.4:
                    sess.send("yes")
                    sess.clear_partial()
                    last_yes_at = now
                continue
            if "passkey:" in low or "pin code:" in low:
                code = "".join(re.findall(r"\d", text))
                if code and on_status:
                    on_status(f"Enter PIN: {code}", device_name)
            elif any(k in low for k in ("pairing successful", "alreadyexists", "already paired", "paired: yes", "bonded: yes")):
                paired_ok = True
                break
            elif any(k in low for k in ("failed to pair", "not available", "authenticationfailed", "authenticationrejected", "authenticationcanceled", "connectionattemptfailed")):
                pair_err = text
                break
        if paired_ok or pair_err:
            break
        if not sess.is_alive():
            break

    # Stop scanning immediately after pairing (active scan causes Broadcom/Pi A2DP connect timeouts!)
    sess.send("scan off")
    time.sleep(0.35)

    if not paired_ok:
        recheck = get_device_info(mac_u)
        if recheck.get("paired"):
            return True, ""
        return False, pair_err or "Pairing failed (put headphones in pairing mode)"
    return True, ""


def connect_device(
    mac: str,
    device_type: str = "device",
    device_name: str = "",
    on_status: Optional[Callable[[str, str], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    """Smart PTY-backed pair + trust + connect workflow for headphones, speakers, and HID devices."""
    if not is_bluetoothctl_available():
        return False, "bluetoothctl not found"

    mac_u = mac.upper()
    if on_status:
        on_status("Checking device...", device_name or mac_u)

    if shutil.which("rfkill"):
        _run_cmd(["rfkill", "unblock", "bluetooth"], timeout=2.0)

    info = get_device_info(mac_u, fallback_name=device_name)
    already_paired = info.get("paired", False)
    if not device_name:
        device_name = info.get("name", mac_u)
    if device_type == "device":
        device_type = info.get("device_type", "device")

    sess = _BluetoothCtlSession()
    if not sess.start():
        return False, "Could not start bluetoothctl"

    conn_ok = False
    conn_err = ""
    try:
        # Use standard `agent on` (DisplayYesNo/KeyboardDisplay) so BOTH JustWorks headphones
        # AND SSP Numeric Comparison headphones pair cleanly without AuthenticationFailed.
        sess.send("power on")
        time.sleep(0.25)
        sess.send("pairable on")
        sess.send("agent on")
        time.sleep(0.2)
        sess.send("default-agent")
        time.sleep(0.25)
        sess.poll_output(timeout=0.1)

        # Step 1: Pair (with automatic stale-cache clean retry if first attempt fails)
        if not already_paired:
            paired_ok, pair_err = _attempt_pair_in_session(
                sess, mac_u, device_name, on_status=on_status, stop_check=stop_check
            )
            if not paired_ok:
                if pair_err == "Cancelled":
                    return False, "Cancelled"
                # Stale half-paired BlueZ entry is a very common cause of headphone pairing failure:
                # remove stale entry, re-scan briefly, and retry once cleanly.
                if on_status:
                    on_status("Retrying clean pair...", device_name)
                sess.send(f"remove {mac_u}")
                time.sleep(0.5)
                sess.poll_output(timeout=0.1)
                paired_ok, pair_err = _attempt_pair_in_session(
                    sess, mac_u, device_name, on_status=on_status, stop_check=stop_check
                )
                if not paired_ok:
                    return False, pair_err[:52]

        # Step 2: Trust for automatic reconnection
        if on_status:
            on_status("Trusting...", device_name)
        sess.send(f"trust {mac_u}")
        time.sleep(0.4)
        sess.poll_output(timeout=0.1)

        # Step 3: Connect (with 1 automatic retry for Pi Broadcom page-timeout)
        for attempt in range(2):
            if on_status:
                on_status("Connecting..." if attempt == 0 else "Reconnecting...", device_name)
            sess.send("scan off")
            sess.clear_partial()
            sess.send(f"connect {mac_u}")
            conn_err = ""
            last_yes_at = 0.0
            c_start = time.monotonic()
            while (time.monotonic() - c_start) < 16.0:
                if stop_check is not None and stop_check():
                    return False, "Cancelled"
                lines, partial = sess.poll_output(timeout=0.2)
                chunks = list(lines) + ([partial] if partial else [])
                for text in chunks:
                    low = text.lower()
                    if _is_yes_no_prompt(text):
                        now = time.monotonic()
                        if (now - last_yes_at) > 0.4:
                            sess.send("yes")
                            sess.clear_partial()
                            last_yes_at = now
                        continue
                    if any(k in low for k in ("connection successful", "already connected", "connected: yes")):
                        conn_ok = True
                        break
                    if "failed to connect" in low or "not available" in low:
                        conn_err = text
                        break
                if conn_ok or conn_err:
                    break
                if not sess.is_alive():
                    break
            if conn_ok:
                break
            if "profile-unavailable" in conn_err.lower():
                break
            time.sleep(0.8)
    finally:
        sess.close()

    final_info = get_device_info(mac_u, fallback_name=device_name)
    if not conn_ok and not final_info.get("connected"):
        if "profile-unavailable" in conn_err.lower():
            return False, "Paired! Install BT audio: libspa-0.2-bluetooth"
        return False, (conn_err or "Connection failed")[:52]

    # Step 4: For headphones / speakers, automatically route audio output to this device!
    if is_audio_device(final_info) or device_type in ("headphones", "speaker"):
        if on_status:
            on_status("Routing audio...", device_name)
        ok_sink, sink_msg = activate_bluetooth_audio_sink(mac_u, device_name=final_info.get("name", device_name))
        if ok_sink:
            return True, f"Connected! {sink_msg}"
        return True, f"Connected: {final_info.get('name', device_name)[:24]}"

    return True, f"Connected: {final_info.get('name', device_name)[:26]}"


def disconnect_device(mac: str) -> Tuple[bool, str]:
    if not is_bluetoothctl_available():
        return False, "bluetoothctl not found"
    rc, out, err = _run_cmd(["bluetoothctl", "disconnect", mac.upper()], timeout=8.0)
    combined = (out + " " + err).strip()
    if rc == 0 and "Failed" not in combined:
        return True, "Disconnected"
    return False, (combined or "Disconnect failed")[:44]


def trust_device(mac: str, trust: bool = True) -> Tuple[bool, str]:
    if not is_bluetoothctl_available():
        return False, "bluetoothctl not found"
    cmd = "trust" if trust else "untrust"
    rc, out, err = _run_cmd(["bluetoothctl", cmd, mac.upper()], timeout=5.0)
    combined = (out + " " + err).strip()
    if rc == 0 and "Failed" not in combined:
        return True, "Trusted" if trust else "Untrusted"
    return False, (combined or "Trust change failed")[:44]


def remove_device(mac: str) -> Tuple[bool, str]:
    if not is_bluetoothctl_available():
        return False, "bluetoothctl not found"
    _run_cmd(["bluetoothctl", "disconnect", mac.upper()], timeout=4.0)
    rc, out, err = _run_cmd(["bluetoothctl", "remove", mac.upper()], timeout=6.0)
    combined = (out + " " + err).strip()
    if rc == 0 and "Failed" not in combined:
        return True, "Device forgotten"
    return False, (combined or "Remove failed")[:44]


def _active_alsa_card_candidates() -> List[str]:
    """Return candidate ALSA card numbers (saved/selected card first, then detected playback cards)."""
    cards: List[str] = []
    st = load_audio_state()
    alsa_dev = st.get("alsa_dev", "")
    m = re.search(r"(?:plug)?hw:(\d+)", alsa_dev)
    if m:
        cards.append(m.group(1))
    if shutil.which("aplay"):
        rc, out, _ = _run_cmd(["aplay", "-l"], timeout=2.0)
        if rc == 0:
            for d in parse_alsa_playback_devices(out):
                if d["card"] not in cards:
                    cards.append(d["card"])
    return cards


def get_system_volume_pct() -> int:
    """Return current default output volume percentage (0-100)."""
    if shutil.which("pactl"):
        rc, out, _ = _run_cmd(["pactl", "get-sink-volume", "@DEFAULT_SINK@"], timeout=2.0, use_audio_env=True)
        if rc == 0:
            m = re.search(r"(\d+)%", out)
            if m:
                return max(0, min(150, int(m.group(1))))
    if shutil.which("wpctl"):
        rc, out, _ = _run_cmd(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], timeout=2.0, use_audio_env=True)
        if rc == 0:
            m = re.search(r"([0-9]+(?:\.[0-9]+)?)", out)
            if m:
                return max(0, min(150, int(round(float(m.group(1)) * 100))))
    if shutil.which("amixer"):
        for card in _active_alsa_card_candidates():
            for ctl in ("Master", "PCM", "Headphone", "Speaker", "Digital"):
                rc, out, _ = _run_cmd(["amixer", "-c", card, "sget", ctl], timeout=1.5)
                if rc == 0:
                    m = re.search(r"\[(\d+)%\]", out)
                    if m:
                        return max(0, min(100, int(m.group(1))))
        for ctl in ("Master", "PCM", "Headphone"):
            rc, out, _ = _run_cmd(["amixer", "sget", ctl], timeout=1.5)
            if rc == 0:
                m = re.search(r"\[(\d+)%\]", out)
                if m:
                    return max(0, min(100, int(m.group(1))))
    return 70


def set_system_volume_pct(pct: int) -> int:
    """Set default output volume (0-100%) on PipeWire/PulseAudio and ALSA."""
    clamped = max(0, min(100, int(pct)))
    applied = False
    if shutil.which("pactl"):
        _run_cmd(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"], timeout=2.0, use_audio_env=True)
        rc2, _, _ = _run_cmd(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{clamped}%"], timeout=2.0, use_audio_env=True)
        applied = applied or (rc2 == 0)
    if not applied and shutil.which("wpctl"):
        _run_cmd(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"], timeout=2.0, use_audio_env=True)
        rc2, _, _ = _run_cmd(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{clamped / 100.0:.2f}"], timeout=2.0, use_audio_env=True)
        applied = applied or (rc2 == 0)
    if not applied and shutil.which("amixer"):
        for card in _active_alsa_card_candidates():
            for ctl in ("Master", "PCM", "Headphone", "Speaker", "Digital"):
                rc, _, _ = _run_cmd(["amixer", "-c", card, "sset", ctl, f"{clamped}%", "unmute"], timeout=1.5)
                if rc == 0:
                    applied = True
                    break
            if applied:
                break
        if not applied:
            for ctl in ("Master", "PCM", "Headphone"):
                rc, _, _ = _run_cmd(["amixer", "sset", ctl, f"{clamped}%", "unmute"], timeout=1.5)
                if rc == 0:
                    break
    return clamped


def play_test_chime() -> Tuple[bool, str]:
    """Generate and play a gentle stereo two-tone test chime to verify speaker/headphone audio."""
    tmp_wav = "/dev/shm/rj_bt_test_chime.wav"
    sr = 22050
    try:
        frames = bytearray()
        # Two pleasant warm notes (C5 -> G5, 0.22s each, gentle envelope)
        for freq, dur in ((523.25, 0.22), (783.99, 0.28)):
            n_samples = int(sr * dur)
            for i in range(n_samples):
                t = i / sr
                env_a = min(1.0, i / 400.0) * min(1.0, (n_samples - i) / 900.0)
                val = int(11000 * env_a * math.sin(2.0 * math.pi * freq * t))
                frames.extend(struct.pack("<hh", val, val))
        with wave.open(tmp_wav, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(bytes(frames))
        try:
            os.chmod(tmp_wav, 0o644)
        except OSError:
            pass
    except Exception as exc:
        return False, f"Chime gen error: {exc}"[:40]

    last_err = ""
    # 1. Try PipeWire / PulseAudio as the audio socket owner (fixes root -> UID 1000 auth!)
    for cmd in (["paplay", tmp_wav], ["pw-play", tmp_wav]):
        if shutil.which(cmd[0]):
            rc, _out, err = _run_cmd(cmd, timeout=5.0, use_audio_env=True)
            if rc == 0:
                return True, f"Played via {cmd[0]}"
            if err:
                last_err = err.splitlines()[-1]

    # 2. Fall back to direct ALSA (selected device, BlueALSA, or real hardware plughw)
    if shutil.which("aplay"):
        alsa_targets: List[str] = []
        try:
            from payloads import _audio_helper as _ah
            best = _ah.get_best_alsa_dev()
            if best:
                alsa_targets.append(best)
        except Exception:
            pass
        st = load_audio_state()
        if st.get("alsa_dev") and st["alsa_dev"] not in alsa_targets:
            alsa_targets.insert(0, st["alsa_dev"])
        if st.get("bt_mac"):
            rc_l, out_l, _ = _run_cmd(["aplay", "-L"], timeout=2.0)
            if rc_l == 0 and "bluealsa" in out_l.lower():
                bt_dev = f"bluealsa:DEV={st['bt_mac']},PROFILE=a2dp"
                if bt_dev not in alsa_targets:
                    alsa_targets.insert(0, bt_dev)
        for s in list_alsa_audio_sinks():
            ad = s.get("alsa_dev", "")
            if ad and ad not in alsa_targets:
                alsa_targets.append(ad)
        if "default" not in alsa_targets:
            alsa_targets.append("default")

        for dev in alsa_targets:
            m_card = re.search(r"(?:plug)?hw:(\d+)", dev)
            if m_card:
                _unmute_alsa_card(m_card.group(1))
            rc, _out, err = _run_cmd(["aplay", "-q", "-D", dev, tmp_wav], timeout=5.0, use_audio_env=False)
            if rc == 0:
                return True, f"Played via ALSA ({dev})"
            if err:
                last_err = err.splitlines()[-1]

    return False, (last_err or "No working audio output")[:46]
