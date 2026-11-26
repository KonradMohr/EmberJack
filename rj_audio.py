#!/usr/bin/env python3
"""
EmberJack Unified Audio Management Engine
-----------------------------------------
Pure-stdlib controller for PipeWire / PulseAudio / WirePlumber / BlueALSA / ALSA
running cleanly under both non-root desktop sessions and root headless Raspberry Pi
systemd services (authenticating to the desktop user's PipeWire/Pulse socket via
`rj_bluetooth.get_audio_popen_kwargs`).

Capabilities:
  - List & switch default output devices (HDMI, 3.5mm, USB DAC, Bluetooth A2DP)
  - List & switch default input devices (USB / BT / built-in microphones)
  - Live per-device volume (0-150% on PipeWire/Pulse, 0-100% on ALSA) & mute
  - Live per-application playback stream mixer (volume, mute, move to another sink)
  - Hardware & Bluetooth card profile switching (e.g. A2DP Stereo vs HFP Headset)
  - Stereo / Left / Right channel test chimes (zero external dependencies)
  - One-click PipeWire / WirePlumber service restart & full diagnostics
"""

import math
import os
import re
import shutil
import struct
import subprocess
import time
import wave
from typing import Dict, List, Optional, Tuple

import rj_bluetooth as _bt

# ---------------------------------------------------------------------------
# Helpers & Classification
# ---------------------------------------------------------------------------

def _classify_audio_kind(name: str = "", label: str = "", is_bt: bool = False) -> str:
    blob = f"{name} {label}".lower()
    if is_bt or any(k in blob for k in ("bluez", "bluetooth", "a2dp", "airpod", "wh-1000", "wf-1000", "buds")):
        return "bluetooth"
    if "hdmi" in blob or "vc4hdmi" in blob or "displayport" in blob:
        return "hdmi"
    if "usb" in blob:
        return "usb"
    if any(k in blob for k in ("headphone", "analog", "bcm2835", "3.5mm", "jack", "lineout")):
        return "analog"
    if any(k in blob for k in ("es8388", "es8389", "es8390", "speaker", "built-in", "builtin", "internal")):
        return "builtin"
    return "audio"


def kind_badge(kind: str) -> str:
    return {
        "bluetooth": "BT",
        "hdmi": "HDMI",
        "usb": "USB",
        "analog": "JACK",
        "builtin": "SPK",
        "mic": "MIC",
    }.get(kind, "AUD")


def _extract_pct(volume_line: str) -> int:
    """Extract first percentage from a `pactl` Volume: line."""
    m = re.search(r"(\d+)%", volume_line or "")
    return max(0, min(150, int(m.group(1)))) if m else 70


# ---------------------------------------------------------------------------
# Pure Testable Parsers (pactl / wpctl / arecord)
# ---------------------------------------------------------------------------

def parse_pactl_sinks_detailed(full_output: str, default_sink: str = "") -> List[dict]:
    """Parse `pactl list sinks` into rich output device dicts with volume, mute & port."""
    sinks: List[dict] = []
    cur: Optional[dict] = None

    def _flush() -> None:
        if cur is None or not cur.get("name"):
            return
        name = cur["name"]
        is_bt = "bluez" in name.lower() or "bluetooth" in cur.get("desc", "").lower()
        raw_desc = cur.get("desc") or name
        if is_bt and not raw_desc.lower().startswith("bt"):
            label = f"BT: {raw_desc}"
        else:
            label = raw_desc
        kind = _classify_audio_kind(name=name, label=label, is_bt=is_bt)
        sinks.append({
            "id": cur.get("id", ""),
            "name": name,
            "label": label,
            "desc": raw_desc,
            "state": cur.get("state", "IDLE"),
            "volume_pct": cur.get("volume_pct", 70),
            "muted": bool(cur.get("muted", False)),
            "active_port": cur.get("active_port", ""),
            "kind": kind,
            "is_bluetooth": is_bt,
            "is_default": (name == default_sink),
            "backend": "pactl",
        })

    for raw in _bt.strip_ansi(full_output).splitlines():
        m_hdr = re.match(r"^Sink\s+#(\d+)", raw.strip())
        if m_hdr:
            _flush()
            cur = {"id": m_hdr.group(1), "volume_pct": 70, "muted": False, "state": "IDLE"}
            continue
        if cur is None:
            continue
        s = raw.strip()
        if s.startswith("State:"):
            cur["state"] = s.split(":", 1)[1].strip()
        elif s.startswith("Name:"):
            cur["name"] = s.split(":", 1)[1].strip()
        elif s.startswith("Description:"):
            cur["desc"] = s.split(":", 1)[1].strip()
        elif s.startswith("Mute:"):
            cur["muted"] = s.split(":", 1)[1].strip().lower() == "yes"
        elif s.startswith("Volume:") and "Base Volume:" not in s:
            cur["volume_pct"] = _extract_pct(s)
        elif s.startswith("Active Port:"):
            cur["active_port"] = s.split(":", 1)[1].strip()
    _flush()

    if sinks and not any(x["is_default"] for x in sinks):
        sinks[0]["is_default"] = True
    return sinks


def parse_pactl_sources_detailed(
    full_output: str,
    default_source: str = "",
    include_monitors: bool = False,
) -> List[dict]:
    """Parse `pactl list sources` into structured microphone/input dicts."""
    sources: List[dict] = []
    monitors: List[dict] = []
    cur: Optional[dict] = None

    def _flush() -> None:
        if cur is None or not cur.get("name"):
            return
        name = cur["name"]
        is_mon = ".monitor" in name.lower() or cur.get("monitor_of", "") != ""
        is_bt = "bluez" in name.lower() or "bluetooth" in cur.get("desc", "").lower()
        raw_desc = cur.get("desc") or name
        label = f"BT: {raw_desc}" if (is_bt and not raw_desc.lower().startswith("bt")) else raw_desc
        kind = "bluetooth" if is_bt else ("usb" if "usb" in f"{name} {label}".lower() else "mic")
        entry = {
            "id": cur.get("id", ""),
            "name": name,
            "label": label,
            "desc": raw_desc,
            "state": cur.get("state", "IDLE"),
            "volume_pct": cur.get("volume_pct", 80),
            "muted": bool(cur.get("muted", False)),
            "kind": kind,
            "is_bluetooth": is_bt,
            "is_monitor": is_mon,
            "is_default": (name == default_source),
            "backend": "pactl",
        }
        if is_mon:
            monitors.append(entry)
        else:
            sources.append(entry)

    for raw in _bt.strip_ansi(full_output).splitlines():
        m_hdr = re.match(r"^Source\s+#(\d+)", raw.strip())
        if m_hdr:
            _flush()
            cur = {"id": m_hdr.group(1), "volume_pct": 80, "muted": False, "state": "IDLE", "monitor_of": ""}
            continue
        if cur is None:
            continue
        s = raw.strip()
        if s.startswith("State:"):
            cur["state"] = s.split(":", 1)[1].strip()
        elif s.startswith("Name:"):
            cur["name"] = s.split(":", 1)[1].strip()
        elif s.startswith("Description:"):
            cur["desc"] = s.split(":", 1)[1].strip()
        elif s.startswith("Monitor of Sink:"):
            val = s.split(":", 1)[1].strip()
            if val.lower() != "n/a":
                cur["monitor_of"] = val
        elif s.startswith("Mute:"):
            cur["muted"] = s.split(":", 1)[1].strip().lower() == "yes"
        elif s.startswith("Volume:") and "Base Volume:" not in s:
            cur["volume_pct"] = _extract_pct(s)
    _flush()

    out = sources + monitors if include_monitors else sources
    if out and not any(x["is_default"] for x in out):
        out[0]["is_default"] = True
    return out


def parse_pactl_sink_inputs(full_output: str) -> List[dict]:
    """Parse `pactl list sink-inputs` into live playing application stream entries."""
    streams: List[dict] = []
    cur: Optional[dict] = None

    def _flush() -> None:
        if cur is None or not cur.get("id"):
            return
        app = cur.get("app_name") or cur.get("media_name") or f"Stream #{cur['id']}"
        media = cur.get("media_name") or ""
        label = app if (not media or media.lower() == app.lower()) else f"{app}: {media}"
        streams.append({
            "id": cur["id"],
            "sink_id": cur.get("sink_id", ""),
            "app_name": app,
            "media_name": media,
            "label": label[:48],
            "volume_pct": cur.get("volume_pct", 100),
            "muted": bool(cur.get("muted", False)),
            "corked": bool(cur.get("corked", False)),
            "backend": "pactl",
        })

    for raw in _bt.strip_ansi(full_output).splitlines():
        m_hdr = re.match(r"^Sink Input\s+#(\d+)", raw.strip())
        if m_hdr:
            _flush()
            cur = {"id": m_hdr.group(1), "volume_pct": 100, "muted": False, "corked": False}
            continue
        if cur is None:
            continue
        s = raw.strip()
        if s.startswith("Sink:"):
            cur["sink_id"] = s.split(":", 1)[1].strip()
        elif s.startswith("Corked:"):
            cur["corked"] = s.split(":", 1)[1].strip().lower() == "yes"
        elif s.startswith("Mute:"):
            cur["muted"] = s.split(":", 1)[1].strip().lower() == "yes"
        elif s.startswith("Volume:") and "Base Volume:" not in s:
            cur["volume_pct"] = _extract_pct(s)
        elif s.startswith("application.name ="):
            cur["app_name"] = s.split("=", 1)[1].strip().strip('"')
        elif s.startswith("media.name ="):
            cur["media_name"] = s.split("=", 1)[1].strip().strip('"')
    _flush()
    return streams


def parse_pactl_cards(full_output: str) -> List[dict]:
    """Parse `pactl list cards` into sound cards with available switchable profiles."""
    cards: List[dict] = []
    cur: Optional[dict] = None
    in_profiles = False

    def _flush() -> None:
        if cur is None or not cur.get("name"):
            return
        profiles = cur.get("profiles", [])
        if not profiles:
            return
        name = cur["name"]
        desc = cur.get("desc") or name
        is_bt = "bluez" in name.lower()
        cards.append({
            "id": cur.get("id", ""),
            "name": name,
            "label": f"BT: {desc}" if (is_bt and not desc.lower().startswith("bt")) else desc,
            "is_bluetooth": is_bt,
            "active_profile": cur.get("active_profile", ""),
            "profiles": profiles,
        })

    for raw in _bt.strip_ansi(full_output).splitlines():
        m_hdr = re.match(r"^Card\s+#(\d+)", raw.strip())
        if m_hdr:
            _flush()
            cur = {"id": m_hdr.group(1), "profiles": []}
            in_profiles = False
            continue
        if cur is None:
            continue
        s = raw.strip()
        if s.startswith("Name:"):
            cur["name"] = s.split(":", 1)[1].strip()
            in_profiles = False
        elif s.startswith("device.description ="):
            cur["desc"] = s.split("=", 1)[1].strip().strip('"')
        elif s.startswith("Profiles:"):
            in_profiles = True
        elif s.startswith("Active Profile:"):
            cur["active_profile"] = s.split(":", 1)[1].strip()
            in_profiles = False
        elif s.startswith("Ports:"):
            in_profiles = False
        elif in_profiles and ":" in s:
            # e.g. "a2dp-sink: High Fidelity Playback (A2DP Sink) (sinks: 1, ..., available: yes)"
            if "available: no" in s.lower():
                continue
            pname, rest = s.split(":", 1)
            pname = pname.strip()
            pdesc = re.sub(r"\s*\((?:sinks:|priority:)[^)]*\)\s*", "", rest).strip()
            if pname:
                cur["profiles"].append({"name": pname, "label": pdesc or pname})
    _flush()
    return cards


def parse_wpctl_sources(status_output: str) -> List[dict]:
    """Parse PipeWire `wpctl status` Audio -> Sources section into input dicts."""
    sources: List[dict] = []
    in_audio = False
    in_sources = False
    for raw_line in _bt.strip_ansi(status_output).splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped == "Audio":
            in_audio = True
            in_sources = False
            continue
        if in_audio and stripped in ("Video", "Settings"):
            break
        if not in_audio:
            continue
        if "Sources:" in stripped:
            in_sources = True
            continue
        if in_sources:
            if any(sec in stripped for sec in ("Sinks:", "Filters:", "Streams:", "Endpoints:", "Devices:")):
                in_sources = False
                continue
            m = re.search(r"(\*)?\s*(\d+)\.\s+(.+?)(?:\s+\[vol:\s*([0-9.]+)(\s+MUTED)?\])?$", stripped)
            if not m:
                continue
            is_def = bool(m.group(1))
            src_id = m.group(2)
            raw_lbl = m.group(3).strip()
            vol_pct = int(round(float(m.group(4)) * 100)) if m.group(4) else 80
            muted = bool(m.group(5))
            is_bt = any(k in raw_lbl.lower() for k in ("bluez", "bluetooth", "headset"))
            sources.append({
                "id": src_id,
                "name": f"wpctl:{src_id}",
                "label": f"BT: {raw_lbl}" if (is_bt and not raw_lbl.lower().startswith("bt")) else raw_lbl,
                "state": "RUNNING" if is_def else "IDLE",
                "volume_pct": max(0, min(150, vol_pct)),
                "muted": muted,
                "kind": "bluetooth" if is_bt else ("usb" if "usb" in raw_lbl.lower() else "mic"),
                "is_bluetooth": is_bt,
                "is_default": is_def,
                "backend": "wpctl",
            })
    return sources


def parse_alsa_capture_devices(arecord_l_output: str, selected_cap_dev: str = "") -> List[dict]:
    """Parse `arecord -l` output into ALSA capture device entries."""
    devices: List[dict] = []
    seen = set()
    pattern = re.compile(
        r"card\s+(\d+):\s*([^\[]+?)\s*\[([^\]]+)\],\s*device\s+(\d+):\s*([^\[]+?)(?:\s*\[([^\]]+)\])?\s*$",
        re.IGNORECASE,
    )
    for raw_line in _bt.strip_ansi(arecord_l_output).splitlines():
        m = pattern.match(raw_line.strip())
        if not m:
            continue
        card_num, card_id, card_name, dev_num = m.group(1), m.group(2).strip(), m.group(3).strip(), m.group(4)
        alsa_dev = f"plughw:{card_num},{dev_num}"
        if alsa_dev in seen:
            continue
        seen.add(alsa_dev)
        blob = f"{card_id} {card_name}".upper()
        if "USB" in blob:
            label = f"USB Mic: {card_name}"
            kind = "usb"
        elif any(k in blob for k in ("ES8388", "ES8389", "ES8390")):
            label = "Built-in Mic"
            kind = "builtin"
        else:
            label = f"Mic: {card_name}"
            kind = "mic"
        devices.append({
            "id": f"{card_num},{dev_num}",
            "name": f"alsa:{alsa_dev}",
            "alsa_dev": alsa_dev,
            "card": card_num,
            "label": label,
            "state": "IDLE",
            "volume_pct": 80,
            "muted": False,
            "kind": kind,
            "is_bluetooth": False,
            "is_default": (alsa_dev == selected_cap_dev),
            "backend": "alsa",
        })
    if devices and not any(d["is_default"] for d in devices):
        devices[0]["is_default"] = True
    return devices


# ---------------------------------------------------------------------------
# Live System Query & Control Functions
# ---------------------------------------------------------------------------

def _pactl_available() -> bool:
    if not shutil.which("pactl"):
        return False
    rc, _, _ = _bt._run_cmd(["pactl", "info"], timeout=2.0, use_audio_env=True)
    return rc == 0


def _wpctl_available() -> bool:
    if not shutil.which("wpctl"):
        return False
    rc, _, _ = _bt._run_cmd(["wpctl", "status"], timeout=2.0, use_audio_env=True)
    return rc == 0


def get_audio_backend_status() -> dict:
    """Inspect active audio server, user session, Pulse socket & default sinks."""
    sess = _bt.get_audio_session_info() if hasattr(_bt, "get_audio_session_info") else {}
    backend = "Direct ALSA"
    server_name = "ALSA"
    if shutil.which("pactl"):
        rc, out, _ = _bt._run_cmd(["pactl", "info"], timeout=2.0, use_audio_env=True)
        if rc == 0:
            m = re.search(r"Server Name:\s*(.+)", out)
            sname = m.group(1).strip() if m else "PulseAudio"
            server_name = sname
            backend = "PipeWire" if "pipewire" in sname.lower() else "PulseAudio"
    if backend == "Direct ALSA" and _wpctl_available():
        backend = "PipeWire (wpctl)"
        server_name = "PipeWire / WirePlumber"

    return {
        "backend": backend,
        "server_name": server_name,
        "user": sess.get("user", ""),
        "uid": sess.get("uid", 0),
        "has_pulse_sock": bool(sess.get("pulse_sock")),
        "has_pw_sock": bool(sess.get("pw_sock")),
        "has_pactl": shutil.which("pactl") is not None,
        "has_wpctl": shutil.which("wpctl") is not None,
        "has_amixer": shutil.which("amixer") is not None,
    }


def list_output_devices() -> List[dict]:
    """Return enriched playback output devices with volume %, mute state, and kind."""
    if shutil.which("pactl"):
        rc_def, out_def, _ = _bt._run_cmd(["pactl", "get-default-sink"], timeout=2.0, use_audio_env=True)
        def_sink = out_def.strip() if rc_def == 0 else ""
        rc, out, _ = _bt._run_cmd(["pactl", "list", "sinks"], timeout=3.0, use_audio_env=True)
        if rc == 0:
            sinks = parse_pactl_sinks_detailed(out, default_sink=def_sink)
            if sinks:
                return sinks

    if shutil.which("wpctl"):
        rc_w, out_w, _ = _bt._run_cmd(["wpctl", "status"], timeout=2.5, use_audio_env=True)
        if rc_w == 0:
            sinks = _bt.parse_wpctl_sinks(out_w)
            if sinks:
                for s in sinks:
                    s["kind"] = _classify_audio_kind(s["name"], s["label"], s.get("is_bluetooth", False))
                    rc_v, out_v, _ = _bt._run_cmd(["wpctl", "get-volume", str(s["id"])], timeout=1.5, use_audio_env=True)
                    if rc_v == 0:
                        m_v = re.search(r"([0-9]+(?:\.[0-9]+)?)", out_v)
                        s["volume_pct"] = max(0, min(150, int(round(float(m_v.group(1)) * 100)))) if m_v else 70
                        s["muted"] = "MUTED" in out_v.upper()
                    else:
                        s["volume_pct"] = 70
                        s["muted"] = False
                return sinks

    # Direct ALSA hardware / BlueALSA fallback
    alsa_sinks = _bt.list_alsa_audio_sinks()
    for s in alsa_sinks:
        s["kind"] = _classify_audio_kind(s.get("name", ""), s.get("label", ""), s.get("is_bluetooth", False))
        vol, muted = _get_alsa_card_vol_mute(s.get("card", ""))
        s["volume_pct"] = vol
        s["muted"] = muted
    return alsa_sinks


def _get_alsa_card_vol_mute(card_num: str) -> Tuple[int, bool]:
    if not card_num or not shutil.which("amixer"):
        return 70, False
    for ctl in ("Master", "PCM", "Headphone", "Speaker", "Digital"):
        rc, out, _ = _bt._run_cmd(["amixer", "-c", str(card_num), "sget", ctl], timeout=1.5)
        if rc == 0:
            m = re.search(r"\[(\d+)%\]", out)
            vol = int(m.group(1)) if m else 70
            muted = "[off]" in out.lower()
            return max(0, min(100, vol)), muted
    return 70, False


def list_input_devices() -> List[dict]:
    """Return available microphones / capture inputs across PipeWire, Pulse, or ALSA."""
    if shutil.which("pactl"):
        rc_def, out_def, _ = _bt._run_cmd(["pactl", "get-default-source"], timeout=2.0, use_audio_env=True)
        def_src = out_def.strip() if rc_def == 0 else ""
        rc, out, _ = _bt._run_cmd(["pactl", "list", "sources"], timeout=3.0, use_audio_env=True)
        if rc == 0:
            srcs = parse_pactl_sources_detailed(out, default_source=def_src, include_monitors=False)
            if srcs:
                return srcs

    if shutil.which("wpctl"):
        rc_w, out_w, _ = _bt._run_cmd(["wpctl", "status"], timeout=2.5, use_audio_env=True)
        if rc_w == 0:
            srcs = parse_wpctl_sources(out_w)
            if srcs:
                return srcs

    if shutil.which("arecord"):
        st = _bt.load_audio_state()
        rc, out, _ = _bt._run_cmd(["arecord", "-l"], timeout=2.5)
        if rc == 0:
            return parse_alsa_capture_devices(out, selected_cap_dev=st.get("capture_dev", ""))
    return []


def list_active_streams() -> List[dict]:
    """Return currently playing application audio streams (PipeWire-Pulse / PulseAudio)."""
    if not shutil.which("pactl"):
        return []
    rc, out, _ = _bt._run_cmd(["pactl", "list", "sink-inputs"], timeout=2.5, use_audio_env=True)
    if rc != 0:
        return []
    return parse_pactl_sink_inputs(out)


def list_cards_with_profiles() -> List[dict]:
    """Return audio cards with switchable profiles (e.g. Bluetooth A2DP vs Headset)."""
    if not shutil.which("pactl"):
        return []
    rc, out, _ = _bt._run_cmd(["pactl", "list", "cards"], timeout=3.0, use_audio_env=True)
    if rc != 0:
        return []
    return parse_pactl_cards(out)


def set_default_output(dev: dict) -> Tuple[bool, str]:
    """Switch active default output device and move running streams."""
    return _bt.set_default_audio_sink(dev["name"], label=dev.get("label", ""))


def set_output_volume(dev: dict, pct: int) -> int:
    """Set volume for a specific output device (0-150% on PipeWire/Pulse, 0-100% on ALSA)."""
    backend = dev.get("backend", "pactl")
    max_v = 100 if backend == "alsa" else 150
    clamped = max(0, min(max_v, int(pct)))
    if backend == "pactl" and shutil.which("pactl"):
        _bt._run_cmd(["pactl", "set-sink-mute", dev["name"], "0"], timeout=2.0, use_audio_env=True)
        _bt._run_cmd(["pactl", "set-sink-volume", dev["name"], f"{clamped}%"], timeout=2.0, use_audio_env=True)
    elif backend == "wpctl" and shutil.which("wpctl"):
        _bt._run_cmd(["wpctl", "set-mute", str(dev["id"]), "0"], timeout=2.0, use_audio_env=True)
        _bt._run_cmd(["wpctl", "set-volume", str(dev["id"]), f"{clamped / 100.0:.2f}"], timeout=2.0, use_audio_env=True)
    elif shutil.which("amixer"):
        card = str(dev.get("card") or "")
        for ctl in ("Master", "PCM", "Headphone", "Speaker", "Digital"):
            args = (["amixer", "-c", card, "sset", ctl, f"{clamped}%", "unmute"] if card
                    else ["amixer", "sset", ctl, f"{clamped}%", "unmute"])
            rc, _, _ = _bt._run_cmd(args, timeout=1.5)
            if rc == 0:
                break
    return clamped


def set_output_mute(dev: dict, muted: bool) -> bool:
    backend = dev.get("backend", "pactl")
    if backend == "pactl" and shutil.which("pactl"):
        _bt._run_cmd(["pactl", "set-sink-mute", dev["name"], "1" if muted else "0"], timeout=2.0, use_audio_env=True)
    elif backend == "wpctl" and shutil.which("wpctl"):
        _bt._run_cmd(["wpctl", "set-mute", str(dev["id"]), "1" if muted else "0"], timeout=2.0, use_audio_env=True)
    elif shutil.which("amixer"):
        card = str(dev.get("card") or "")
        for ctl in ("Master", "PCM", "Headphone", "Speaker"):
            args = (["amixer", "-c", card, "sset", ctl, "mute" if muted else "unmute"] if card
                    else ["amixer", "sset", ctl, "mute" if muted else "unmute"])
            rc, _, _ = _bt._run_cmd(args, timeout=1.5)
            if rc == 0:
                break
    return muted


def set_default_input(dev: dict) -> Tuple[bool, str]:
    """Switch active default microphone / capture source."""
    backend = dev.get("backend", "pactl")
    label = dev.get("label") or dev.get("name", "Mic")
    if backend == "pactl" and shutil.which("pactl"):
        rc, _, err = _bt._run_cmd(["pactl", "set-default-source", dev["name"]], timeout=2.5, use_audio_env=True)
        if rc != 0:
            return False, (err or "Failed to set mic")[:44]
        _bt._run_cmd(["pactl", "set-source-mute", dev["name"], "0"], timeout=2.0, use_audio_env=True)
        return True, f"Mic -> {label[:28]}"
    if backend == "wpctl" and shutil.which("wpctl"):
        rc, _, err = _bt._run_cmd(["wpctl", "set-default", str(dev["id"])], timeout=2.5, use_audio_env=True)
        if rc != 0:
            return False, (err or "Failed to set mic")[:44]
        return True, f"Mic -> {label[:28]}"
    if backend == "alsa" and dev.get("alsa_dev"):
        st = _bt.load_audio_state()
        st["capture_dev"] = dev["alsa_dev"]
        st["capture_label"] = label
        _bt._save_audio_state(st)
        return True, f"Mic -> {label[:28]}"
    return False, "No audio input backend"


def set_input_volume(dev: dict, pct: int) -> int:
    backend = dev.get("backend", "pactl")
    max_v = 100 if backend == "alsa" else 150
    clamped = max(0, min(max_v, int(pct)))
    if backend == "pactl" and shutil.which("pactl"):
        _bt._run_cmd(["pactl", "set-source-mute", dev["name"], "0"], timeout=2.0, use_audio_env=True)
        _bt._run_cmd(["pactl", "set-source-volume", dev["name"], f"{clamped}%"], timeout=2.0, use_audio_env=True)
    elif backend == "wpctl" and shutil.which("wpctl"):
        _bt._run_cmd(["wpctl", "set-mute", str(dev["id"]), "0"], timeout=2.0, use_audio_env=True)
        _bt._run_cmd(["wpctl", "set-volume", str(dev["id"]), f"{clamped / 100.0:.2f}"], timeout=2.0, use_audio_env=True)
    elif shutil.which("amixer"):
        card = str(dev.get("card") or "")
        for ctl in ("Mic", "Capture", "Microphone", "Digital"):
            args = (["amixer", "-c", card, "sset", ctl, f"{clamped}%", "cap"] if card
                    else ["amixer", "sset", ctl, f"{clamped}%", "cap"])
            rc, _, _ = _bt._run_cmd(args, timeout=1.5)
            if rc == 0:
                break
    return clamped


def set_input_mute(dev: dict, muted: bool) -> bool:
    backend = dev.get("backend", "pactl")
    if backend == "pactl" and shutil.which("pactl"):
        _bt._run_cmd(["pactl", "set-source-mute", dev["name"], "1" if muted else "0"], timeout=2.0, use_audio_env=True)
    elif backend == "wpctl" and shutil.which("wpctl"):
        _bt._run_cmd(["wpctl", "set-mute", str(dev["id"]), "1" if muted else "0"], timeout=2.0, use_audio_env=True)
    elif shutil.which("amixer"):
        card = str(dev.get("card") or "")
        for ctl in ("Mic", "Capture", "Microphone"):
            args = (["amixer", "-c", card, "sset", ctl, "nocap" if muted else "cap"] if card
                    else ["amixer", "sset", ctl, "nocap" if muted else "cap"])
            rc, _, _ = _bt._run_cmd(args, timeout=1.5)
            if rc == 0:
                break
    return muted


def set_stream_volume(stream_id: str, pct: int) -> int:
    clamped = max(0, min(150, int(pct)))
    if shutil.which("pactl"):
        _bt._run_cmd(["pactl", "set-sink-input-mute", str(stream_id), "0"], timeout=2.0, use_audio_env=True)
        _bt._run_cmd(["pactl", "set-sink-input-volume", str(stream_id), f"{clamped}%"], timeout=2.0, use_audio_env=True)
    return clamped


def set_stream_mute(stream_id: str, muted: bool) -> bool:
    if shutil.which("pactl"):
        _bt._run_cmd(["pactl", "set-sink-input-mute", str(stream_id), "1" if muted else "0"], timeout=2.0, use_audio_env=True)
    return muted


def move_stream_to_sink(stream_id: str, sink_name: str) -> Tuple[bool, str]:
    if not shutil.which("pactl"):
        return False, "pactl required"
    rc, _, err = _bt._run_cmd(["pactl", "move-sink-input", str(stream_id), sink_name], timeout=2.5, use_audio_env=True)
    if rc == 0:
        return True, "Stream moved"
    return False, (err or "Move failed")[:44]


def set_card_profile(card_name: str, profile_name: str) -> Tuple[bool, str]:
    if not shutil.which("pactl"):
        return False, "pactl required"
    rc, _, err = _bt._run_cmd(["pactl", "set-card-profile", card_name, profile_name], timeout=3.0, use_audio_env=True)
    if rc == 0:
        return True, f"Profile: {profile_name[:28]}"
    return False, (err or "Profile switch failed")[:44]


def play_test_tone(channel: str = "both") -> Tuple[bool, str]:
    """Play a gentle two-tone chime on both channels, left-only, or right-only."""
    ch = (channel or "both").strip().lower()
    if ch == "both":
        return _bt.play_test_chime()

    tmp_wav = f"/dev/shm/rj_audio_test_{ch}.wav"
    sr = 22050
    try:
        frames = bytearray()
        notes = ((440.0, 0.25), (659.25, 0.28)) if ch == "left" else ((523.25, 0.25), (783.99, 0.28))
        for freq, dur in notes:
            n_samples = int(sr * dur)
            for i in range(n_samples):
                t = i / sr
                env_a = min(1.0, i / 400.0) * min(1.0, (n_samples - i) / 900.0)
                sample = int(12000 * env_a * math.sin(2.0 * math.pi * freq * t))
                left_s = sample if ch == "left" else 0
                right_s = sample if ch == "right" else 0
                frames.extend(struct.pack("<hh", left_s, right_s))
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
        return False, f"Tone error: {exc}"[:40]

    for cmd in (["paplay", tmp_wav], ["pw-play", tmp_wav]):
        if shutil.which(cmd[0]):
            rc, _, _ = _bt._run_cmd(cmd, timeout=5.0, use_audio_env=True)
            if rc == 0:
                return True, f"Tested {ch.upper()} ({cmd[0]})"

    if shutil.which("aplay"):
        try:
            from payloads import _audio_helper as _ah
            dev = _ah.get_best_alsa_dev()
        except Exception:
            dev = "default"
        rc, _, err = _bt._run_cmd(["aplay", "-q", "-D", dev, tmp_wav], timeout=5.0)
        if rc == 0:
            return True, f"Tested {ch.upper()} (ALSA)"
        return False, (err or "ALSA playback failed")[:44]

    return False, "No audio player available"


def restart_audio_stack() -> Tuple[bool, str]:
    """Stop conflicting BlueALSA daemons and restart PipeWire / WirePlumber / PulseAudio."""
    if not shutil.which("systemctl"):
        return False, "systemctl not available"
    if os.geteuid() == 0:
        for unit in ("bluealsa.service", "bluez-alsa.service", "bluealsa-aplay.service"):
            _bt._run_cmd(["systemctl", "stop", unit], timeout=2.0)
            _bt._run_cmd(["systemctl", "disable", unit], timeout=2.0)
    sess = _bt.get_audio_session_info() if hasattr(_bt, "get_audio_session_info") else {}
    uname = sess.get("user", "")
    if os.geteuid() == 0 and uname and uname != "root":
        rc, _, err = _bt._run_cmd(
            ["systemctl", f"--machine={uname}@.host", "--user", "restart", "pipewire", "pipewire-pulse", "wireplumber"],
            timeout=8.0,
        )
        if rc == 0:
            time.sleep(0.5)
            return True, f"Restarted PipeWire ({uname})"
        rc2, _, err2 = _bt._run_cmd(
            ["systemctl", f"--machine={uname}@.host", "--user", "restart", "pulseaudio"],
            timeout=8.0,
        )
        if rc2 == 0:
            time.sleep(0.5)
            return True, f"Restarted PulseAudio ({uname})"
        return False, (err or err2 or "Restart failed")[:44]

    rc, _, err = _bt._run_cmd(
        ["systemctl", "--user", "restart", "pipewire", "pipewire-pulse", "wireplumber"],
        timeout=8.0,
        use_audio_env=True,
    )
    if rc == 0:
        time.sleep(0.5)
        return True, "Restarted PipeWire"
    return False, (err or "Restart failed")[:44]
