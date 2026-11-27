"""
Audio helper - auto-detect playback and capture ALSA devices.
Usage:
    from payloads._audio_helper import get_audio_card, get_alsa_dev
    from payloads._audio_helper import get_capture_card, get_capture_dev
    from payloads._audio_helper import enable_capture, disable_capture
    from payloads._audio_helper import open_capture_stream, get_capture_label
    from payloads._audio_helper import set_playback_volume

USB microphones are preferred over the built-in ES838x codec when present.
Set RASPYJACK_CAPTURE_DEV to force a device (e.g. "plughw:3,0").
"""

import os
import re
import shutil
import subprocess

try:
    import rj_bluetooth as _rj_bt
except Exception:
    _rj_bt = None

_card = None
_dev = None
_capture_card = None
_capture_dev = None
_capture_desc = ""
_ffmpeg_has_pulse_cache = None


def _card_num_from_line(line):
    try:
        return line.split(":")[0].replace("card", "").strip()
    except Exception:
        return None


def get_audio_card():
    """Return playback card number as string. Cached."""
    global _card
    if _card is not None:
        return _card
    try:
        r = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=3)
        for line in r.stdout.split("\n"):
            if "card" in line.lower() and ":" in line:
                num = line.split(":")[0].replace("card", "").strip()
                if any(k in line.upper() for k in ["ES8388", "ES8389", "ES8390"]):
                    _card = num
                    return _card
                elif "HDMI" not in line.upper():
                    _card = num
    except Exception:
        pass
    if _card is None:
        _card = "0"
    return _card


def get_alsa_dev():
    """Return playback ALSA device string like 'plughw:0,0'. Cached."""
    global _dev
    if _dev is not None:
        return _dev
    _dev = f"plughw:{get_audio_card()},0"
    return _dev


def get_capture_card():
    """Return capture card number as string, preferring USB microphones."""
    global _capture_card, _capture_desc
    if _capture_card is not None:
        return _capture_card
    try:
        r = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=3)
        usb_candidate = None
        mic_candidate = None
        fallback = None
        for line in r.stdout.split("\n"):
            if "card" not in line.lower() or ":" not in line:
                continue
            num = _card_num_from_line(line)
            if not num:
                continue
            upper = line.upper()
            if "USB" in upper:
                usb_candidate = usb_candidate or (num, line)
            elif any(k in upper for k in ["MIC", "MICROPHONE"]):
                mic_candidate = mic_candidate or (num, line)
            elif any(k in upper for k in ["ES8388", "ES8389", "ES8390"]):
                fallback = fallback or (num, line)
            elif "HDMI" not in upper:
                fallback = fallback or (num, line)
        for candidate in (usb_candidate, mic_candidate, fallback):
            if candidate is not None:
                _capture_card, _capture_desc = candidate
                return _capture_card
    except Exception:
        pass
    _capture_card = get_audio_card()
    return _capture_card


def get_capture_dev():
    """Return capture ALSA device string like 'plughw:2,0'. Cached.

    RASPYJACK_CAPTURE_DEV overrides auto-detection.
    """
    global _capture_dev
    if _capture_dev is not None:
        return _capture_dev
    override = os.environ.get("RASPYJACK_CAPTURE_DEV", "").strip()
    if override:
        _capture_dev = override
        return _capture_dev
    _capture_dev = f"plughw:{get_capture_card()},0"
    return _capture_dev


def _selected_capture_desc():
    """Return the `arecord -l` line describing the selected capture card."""
    override = os.environ.get("RASPYJACK_CAPTURE_DEV", "").strip()
    if not override:
        get_capture_card()
        return _capture_desc
    card = override.split(":")[-1].split(",")[0]
    try:
        r = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=3)
        for line in r.stdout.split("\n"):
            if ":" in line and _card_num_from_line(line) == card:
                return line
    except Exception:
        pass
    return ""


def is_usb_capture():
    """True when the selected capture device is a USB microphone."""
    return "USB" in _selected_capture_desc().upper()


def is_builtin_codec_capture():
    """True when the selected capture device is the on-board ES838x codec.

    Only this codec needs the AU_EN i2c poke and the ADC MUX/PGA controls;
    everything else is driven through plain ALSA mixer controls.
    """
    upper = _selected_capture_desc().upper()
    return any(k in upper for k in ("ES8388", "ES8389", "ES8390"))


def get_capture_label():
    """Short human-readable name of the capture device, for on-screen display."""
    desc = _selected_capture_desc()
    name = desc.split("[", 1)[1].split("]", 1)[0].strip() if "[" in desc else ""
    if is_builtin_codec_capture():
        return "Built-in Mic"
    if is_usb_capture():
        return name or "USB Mic"
    return name or f"Capture {get_capture_card()}"


def _amixer(card, *args):
    try:
        r = subprocess.run(["amixer", "-c", card] + list(args),
                           capture_output=True, timeout=2)
        return r.returncode == 0
    except Exception:
        return False


def enable_capture(gain_percent=80):
    """Power up and unmute the selected microphone.

    USB mics only need their capture control unmuted and turned up. The built-in
    ES838x codec additionally needs AU_EN asserted over i2c and its ADC path set.
    """
    card = get_capture_card()
    if not is_builtin_codec_capture():
        for control in ("Mic", "Capture", "Microphone", "Digital"):
            _amixer(card, "sset", control, f"{gain_percent}%", "cap")
        return
    subprocess.run(["i2cset", "-f", "-y", "1", "0x4f", "0x06", "0x01"],
                   capture_output=True, timeout=2)
    pga = str(int(12 * gain_percent / 80))
    adc = str(int(220 * gain_percent / 80))
    for control, value in (
        ("ADC MUX", "0"),
        ("ADCL PGA Volume", pga),
        ("ADCR PGA Volume", pga),
        ("ADCL Capture Volume", adc),
        ("ADCR Capture Volume", adc),
    ):
        _amixer(card, "cset", f"name={control}", value)


def disable_capture():
    """Release the microphone. No-op for USB mics, which have nothing to power down."""
    if not is_builtin_codec_capture():
        return
    subprocess.run(["i2cset", "-f", "-y", "1", "0x4f", "0x06", "0x03"],
                   capture_output=True, timeout=2)


def open_capture_stream(rate=16000, channels=1, fmt="S16_LE"):
    """Enable the mic and return a Popen streaming raw PCM on stdout.

    Caller owns the process and must terminate it, then call disable_capture().
    plughw handles rate/channel conversion for USB mics that cannot do 16k mono.
    """
    enable_capture()
    return subprocess.Popen(
        ["arecord", "-D", get_capture_dev(), "-f", fmt, "-r", str(rate),
         "-c", str(channels), "-t", "raw"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def _has_control(card, name):
    try:
        r = subprocess.run(["amixer", "-c", card, "sget", name],
                           capture_output=True, timeout=2)
        return r.returncode == 0
    except Exception:
        return False


_vol_controls = None


def _detect_vol_controls():
    global _vol_controls
    if _vol_controls is not None:
        return _vol_controls
    card = get_audio_card()
    controls = []
    if _has_control(card, "Headphone"):
        controls.append(("Headphone", 19, 63))
    if _has_control(card, "Speaker"):
        controls.append(("Speaker", 0, 1))
    if _has_control(card, "DACL"):
        controls.append(("DACL", 75, 255))
    if _has_control(card, "DACR"):
        controls.append(("DACR", 75, 255))
    _vol_controls = controls
    return _vol_controls


def get_pulse_env():
    """Return environment configured for PipeWire/PulseAudio even under root/sudo."""
    if _rj_bt is not None:
        return _rj_bt.get_audio_env()
    return os.environ.copy()


def _audio_server_popen_kwargs():
    """Return subprocess kwargs authenticating as the PipeWire/PulseAudio socket owner."""
    if _rj_bt is not None and hasattr(_rj_bt, "get_audio_popen_kwargs"):
        return _rj_bt.get_audio_popen_kwargs(drop_to_audio_user=True)
    return {"env": get_pulse_env()}


def _is_pulse_running() -> bool:
    """True when PipeWire-Pulse, PulseAudio, OR pure PipeWire/WirePlumber is reachable."""
    kw = _audio_server_popen_kwargs()
    if shutil.which("pactl"):
        try:
            r = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=2, **kw)
            if r.returncode == 0:
                return True
        except (PermissionError, OSError):
            try:
                r = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=2, env=kw.get("env"))
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        except Exception:
            pass
    if shutil.which("wpctl"):
        try:
            r = subprocess.run(["wpctl", "status"], capture_output=True, text=True, timeout=2, **kw)
            if r.returncode == 0:
                return True
        except (PermissionError, OSError):
            try:
                r = subprocess.run(["wpctl", "status"], capture_output=True, text=True, timeout=2, env=kw.get("env"))
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        except Exception:
            pass
    return False


def _has_pulse_socket() -> bool:
    if _rj_bt is not None and hasattr(_rj_bt, "get_audio_session_info"):
        return bool(_rj_bt.get_audio_session_info().get("pulse_sock"))
    rdir = os.environ.get("XDG_RUNTIME_DIR", "")
    return bool(rdir and os.path.exists(os.path.join(rdir, "pulse", "native")))


def _ffmpeg_supports_pulse() -> bool:
    global _ffmpeg_has_pulse_cache
    if _ffmpeg_has_pulse_cache is not None:
        return _ffmpeg_has_pulse_cache
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-muxers"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        _ffmpeg_has_pulse_cache = " pulse " in r.stdout or "\tpulse" in r.stdout or " pulse\n" in r.stdout
    except Exception:
        _ffmpeg_has_pulse_cache = False
    return _ffmpeg_has_pulse_cache


def _alsa_default_is_sound_server(aplay_cap_l: str = "") -> bool:
    """True when ALSA `default` PCM is redirected to PipeWire or PulseAudio.

    On Raspberry Pi OS (Bookworm/Trixie), `99-pipewire-default.conf` routes ALSA
    `default` through PipeWire. When PipeWire is NOT running (headless boot
    without user lingering), `aplay -D default` fails with Connection refused —
    so we must bypass `default` and target hardware `plughw:<card>,<dev>`.
    """
    text = aplay_cap_l
    if not text and shutil.which("aplay"):
        try:
            r = subprocess.run(["aplay", "-L"], capture_output=True, text=True, timeout=2)
            text = r.stdout or ""
        except Exception:
            return False
    in_default = False
    for line in text.splitlines():
        if not line:
            continue
        if not line[0].isspace():
            in_default = (line.strip() == "default")
            continue
        if in_default:
            low = line.lower()
            if "pipewire" in low or "pulseaudio" in low or "pulse sound" in low:
                return True
    return False


def _get_connected_hdmi_indices() -> set:
    """Return set of connected HDMI connector indices (0 for HDMI-A-1, 1 for HDMI-A-2)."""
    connected = set()
    drm_root = "/sys/class/drm"
    try:
        if os.path.isdir(drm_root):
            for entry in os.listdir(drm_root):
                m = re.search(r"HDMI-A-(\d+)$", entry)
                if not m:
                    continue
                st_path = os.path.join(drm_root, entry, "status")
                try:
                    with open(st_path, "r", encoding="utf-8") as handle:
                        if handle.read().strip().lower() == "connected":
                            # DRM HDMI-A-1 maps to vc4hdmi0 (index 0), HDMI-A-2 -> vc4hdmi1 (index 1)
                            connected.add(max(0, int(m.group(1)) - 1))
                except OSError:
                    continue
    except OSError:
        pass
    return connected


def select_best_alsa_device(
    devices,
    saved_alsa_dev: str = "",
    connected_hdmi: set = None,
    default_is_server: bool = False,
) -> str:
    """Pure priority selector for direct ALSA playback when PipeWire/PulseAudio isn't active."""
    if not devices:
        return saved_alsa_dev or ("plughw:0,0" if default_is_server else "default")

    known_devs = {d["alsa_dev"] for d in devices if d.get("alsa_dev")}
    if saved_alsa_dev and (saved_alsa_dev in known_devs or saved_alsa_dev.startswith("bluealsa:")):
        return saved_alsa_dev

    conn_hdmi = connected_hdmi if connected_hdmi is not None else set()

    # 1. Built-in Cardputer ES838x codec
    for d in devices:
        blob = f"{d.get('card_id', '')} {d.get('card_name', '')}".upper()
        if any(k in blob for k in ("ES8388", "ES8389", "ES8390")):
            return d["alsa_dev"]

    # 2. USB Audio DAC / Speaker / Headset (explicitly plugged-in audio hardware)
    for d in devices:
        blob = f"{d.get('card_id', '')} {d.get('card_name', '')}".upper()
        if "USB" in blob:
            return d["alsa_dev"]

    # 3. Connected Raspberry Pi HDMI display output (matches active HDMI-A-1 / HDMI-A-2 cable!)
    if conn_hdmi:
        for idx in sorted(conn_hdmi):
            needle_a = f"VC4HDMI{idx}"
            needle_b = f"VC4-HDMI-{idx}"
            for d in devices:
                blob = f"{d.get('card_id', '')} {d.get('card_name', '')}".upper()
                if needle_a in blob or needle_b in blob:
                    return d["alsa_dev"]
        for d in devices:
            blob = f"{d.get('card_id', '')} {d.get('card_name', '')}".upper()
            if "HDMI" in blob:
                return d["alsa_dev"]

    # 4. Built-in 3.5mm Headphones / analog codec
    for d in devices:
        blob = f"{d.get('card_id', '')} {d.get('card_name', '')}".upper()
        if "HDMI" not in blob:
            return d["alsa_dev"]

    # 5. Any available hardware playback device (e.g. Pi 5 vc4hdmi0)
    return devices[0]["alsa_dev"]


def get_best_alsa_dev() -> str:
    """Return the best working hardware ALSA playback device (bypassing dead PipeWire `default`)."""
    override = os.environ.get("RJ_AUDIO_DEV", "").strip()
    if override and not override.startswith("pulse:"):
        return override

    saved = ""
    if _rj_bt is not None:
        st = _rj_bt.load_audio_state()
        saved = str(st.get("alsa_dev") or "").strip()

    devices = []
    if _rj_bt is not None and hasattr(_rj_bt, "parse_alsa_playback_devices") and shutil.which("aplay"):
        try:
            r = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=2)
            if r.returncode == 0:
                devices = _rj_bt.parse_alsa_playback_devices(r.stdout or "", selected_alsa_dev=saved)
        except Exception:
            devices = []

    default_is_srv = _alsa_default_is_sound_server()
    conn_hdmi = _get_connected_hdmi_indices()
    return select_best_alsa_device(
        devices,
        saved_alsa_dev=saved,
        connected_hdmi=conn_hdmi,
        default_is_server=default_is_srv,
    )


def get_audio_popen_kwargs(cmd=None, allow_user_drop: bool = True) -> dict:
    """Return subprocess kwargs (`env` + optional `user`/`group`) tailored for *cmd*.

    Drops to the PipeWire/PulseAudio socket owner UID when running as root ONLY
    for commands that talk to PipeWire/PulseAudio (`paplay`, `pw-play`, `pactl`,
    `wpctl`, `ffmpeg -f pulse`, or `ffmpeg -f alsa default` while PipeWire is
    active). Direct hardware ALSA (`plughw:*`) and BlueALSA stay as root so
    `/dev/snd/*` and system D-Bus permissions always work.
    """
    uses_sound_server = False
    if cmd:
        prog = os.path.basename(str(cmd[0]))
        if prog in ("paplay", "pw-play", "pactl", "wpctl"):
            uses_sound_server = True
        elif prog == "mpv" and _is_pulse_running():
            uses_sound_server = True
        elif prog == "ffmpeg":
            for i, arg in enumerate(cmd[:-1]):
                if arg == "-f":
                    fmt = str(cmd[i + 1])
                    if fmt == "pulse":
                        uses_sound_server = True
                        break
                    if fmt == "alsa" and i + 2 < len(cmd) and str(cmd[i + 2]) == "default" and _is_pulse_running():
                        uses_sound_server = True
                        break
    if uses_sound_server and allow_user_drop:
        return _audio_server_popen_kwargs()
    return {"env": get_pulse_env()}


def get_ffmpeg_audio_args(buffer_ms: int = 100):
    """Return optimal ffmpeg output args for active audio (Bluetooth/PipeWire/Pulse/ALSA).

    Automatically routes audio through connected Bluetooth headphones via
    PipeWire/PulseAudio or BlueALSA when available, falling back cleanly to
    a working hardware ALSA `plughw:<card>,<dev>` device (never getting stuck
    on a broken PipeWire ALSA `default` when headless!).
    """
    buf_ms = str(max(40, min(500, int(buffer_ms or 100))))
    override = os.environ.get("RJ_AUDIO_DEV", "").strip()
    if override:
        if override.startswith("pulse:"):
            sink = override.split(":", 1)[1] or "default"
            return ["-f", "pulse", "-buffer_duration", buf_ms, "-stream_name", "EmberJack Audio", sink]
        return ["-f", "alsa", override]

    # 1. PipeWire / PulseAudio (supports Bluetooth A2DP headphones + hot-switching!)
    #    100ms default keeps pause snappy; video callers pass ~220ms to absorb heavy
    #    video keyframe decode spikes & Bluetooth A2DP bursts without audio underruns.
    if _is_pulse_running():
        if _has_pulse_socket() and _ffmpeg_supports_pulse():
            return ["-f", "pulse", "-buffer_duration", buf_ms, "-stream_name", "EmberJack Audio", "default"]
        # Debian/Raspberry Pi OS routes ALSA "default" through pipewire-alsa/pulse
        return ["-f", "alsa", "default"]

    # 2. BlueALSA direct Bluetooth audio (headless Pi without PulseAudio/PipeWire)
    if _rj_bt is not None:
        st = _rj_bt.load_audio_state()
        bt_mac = st.get("bt_mac", "")
        if bt_mac and shutil.which("aplay"):
            try:
                r = subprocess.run(["aplay", "-L"], capture_output=True, text=True, timeout=2)
                if "bluealsa" in (r.stdout or "").lower():
                    return ["-f", "alsa", f"bluealsa:DEV={bt_mac},PROFILE=a2dp"]
            except Exception:
                pass

    # 3. Direct hardware ALSA (honors user selection, USB DAC, connected HDMI, or 3.5mm jack)
    return ["-f", "alsa", get_best_alsa_dev()]


def get_pcm_sink_cmd(rate: int = 44100, channels: int = 2):
    """Return a CLI command accepting raw S16LE PCM on stdin routed to the active output.

    Prefers PipeWire/PulseAudio (`paplay` / `pw-play`) so Bluetooth headphones
    work reliably even when RaspyJack runs as root, falling back cleanly to
    BlueALSA or working hardware ALSA `aplay`.
    """
    override = os.environ.get("RJ_AUDIO_DEV", "").strip()
    if override and shutil.which("aplay") and not override.startswith("pulse:"):
        return ["aplay", "-q", "-D", override, "--buffer-time=80000", "-f", "S16_LE", "-r", str(rate), "-c", str(channels)]

    if _is_pulse_running():
        if _has_pulse_socket() and shutil.which("paplay"):
            return [
                "paplay", "--raw", f"--rate={rate}", f"--channels={channels}",
                "--format=s16le", "--latency-msec=80", "--process-time-msec=20",
                "--stream-name=EmberJack Audio",
            ]
        if shutil.which("pw-play"):
            return [
                "pw-play", "--raw", "--rate", str(rate),
                "--channels", str(channels), "--format", "s16", "--latency=80ms", "-",
            ]
        if shutil.which("paplay"):
            return [
                "paplay", "--raw", f"--rate={rate}", f"--channels={channels}",
                "--format=s16le", "--latency-msec=80", "--process-time-msec=20",
                "--stream-name=EmberJack Audio",
            ]

    if _rj_bt is not None:
        st = _rj_bt.load_audio_state()
        bt_mac = st.get("bt_mac", "")
        if bt_mac and shutil.which("aplay"):
            try:
                r = subprocess.run(["aplay", "-L"], capture_output=True, text=True, timeout=2)
                if "bluealsa" in (r.stdout or "").lower():
                    return [
                        "aplay", "-q", "-D", f"bluealsa:DEV={bt_mac},PROFILE=a2dp",
                        "--buffer-time=80000", "-f", "S16_LE", "-r", str(rate), "-c", str(channels),
                    ]
            except Exception:
                pass

    if shutil.which("aplay"):
        dev = get_best_alsa_dev()
        return ["aplay", "-q", "-D", dev, "--buffer-time=80000", "-f", "S16_LE", "-r", str(rate), "-c", str(channels)]
    return None


def get_active_audio_label() -> str:
    """Short human-readable label of the current audio output (e.g. 'BT: AirPods' or 'HDMI 0')."""
    if _rj_bt is not None and _is_pulse_running():
        try:
            for sink in _rj_bt.list_audio_sinks():
                if sink.get("is_default"):
                    lbl = sink.get("label") or sink.get("name") or "Audio"
                    return lbl[:22]
        except Exception:
            pass
    if _rj_bt is not None:
        st = _rj_bt.load_audio_state()
        if st.get("label") and st.get("alsa_dev"):
            return str(st["label"])[:22]
        if st.get("bt_name"):
            return f"BT: {st['bt_name']}"[:22]
        try:
            best = get_best_alsa_dev()
            for sink in _rj_bt.list_alsa_audio_sinks():
                if sink.get("alsa_dev") == best:
                    return str(sink.get("label") or best)[:22]
        except Exception:
            pass
    return "System Audio"


def set_playback_volume_pct(pct: int) -> int:
    """Set playback volume on 0-100% scale across PipeWire/PulseAudio, Bluetooth, and ALSA."""
    clamped = max(0, min(100, int(pct)))
    if _rj_bt is not None and (_is_pulse_running() or shutil.which("wpctl")):
        _rj_bt.set_system_volume_pct(clamped)
    # Also update Cardputer/ALSA hardware codec controls if present
    vol_63 = int(round(clamped * 63.0 / 100.0))
    card = get_audio_card()
    controls = _detect_vol_controls()
    if controls:
        ratio = max(0, min(63, vol_63)) / 63.0
        for name, lo, hi in controls:
            if name == "Speaker":
                val = "on" if vol_63 > 0 else "off"
            else:
                val = str(int(lo + ratio * (hi - lo)))
            subprocess.Popen(["amixer", "-c", card, "sset", name, val],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif not _is_pulse_running() and shutil.which("amixer"):
        for ctl in ("Master", "PCM"):
            if _has_control(card, ctl):
                subprocess.Popen(["amixer", "-c", card, "sset", ctl, f"{clamped}%", "unmute"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                break
    return clamped


def get_playback_volume_pct() -> int:
    """Return current playback volume percentage (0-100)."""
    if _rj_bt is not None:
        return _rj_bt.get_system_volume_pct()
    return 70


def set_playback_mute(muted: bool) -> None:
    """Mute or unmute default playback sink across PipeWire/PulseAudio and ALSA."""
    kw = _audio_server_popen_kwargs()
    if shutil.which("pactl") and _is_pulse_running():
        try:
            subprocess.Popen(
                ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw,
            )
            return
        except Exception:
            pass
    if shutil.which("wpctl") and _is_pulse_running():
        try:
            subprocess.Popen(
                ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if muted else "0"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw,
            )
            return
        except Exception:
            pass
    if shutil.which("amixer"):
        card = get_audio_card()
        for ctl in ("Master", "PCM", "Headphone"):
            if _has_control(card, ctl):
                subprocess.Popen(
                    ["amixer", "-c", card, "sset", ctl, "mute" if muted else "unmute"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return


def set_playback_volume(vol_0_63):
    """Set playback volume (0-63 scale). Auto-detects PipeWire/Pulse/BT and ALSA controls."""
    pct = int(round(max(0, min(63, vol_0_63)) * 100.0 / 63.0))
    set_playback_volume_pct(pct)


def list_capture_devices():
    """Return [(card_number, description)] for every capture card ALSA reports."""
    devices = []
    try:
        r = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=3)
        for line in r.stdout.split("\n"):
            if "card" not in line.lower() or ":" not in line:
                continue
            num = _card_num_from_line(line)
            if not num:
                continue
            name = line.split("[", 1)[1].split("]", 1)[0].strip() if "[" in line else line.strip()
            devices.append((num, name))
    except Exception:
        pass
    return devices
