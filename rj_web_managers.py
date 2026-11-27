#!/usr/bin/env python3
"""
EmberJack WebUI Background Managers & System Telemetry
======================================================
Background subprocess managers (ISM rtl_433, RTL-SDR frequency scanner + FM
station directory, generic payload runner), Tailscale/Caddy provisioning, and
Linux system telemetry helpers (CPU, RAM, temperature, uptime, IPv4).

Split out from web_server.py for maintainability while keeping 100%
backwards-compatible re-exports in web_server.py.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
WEB_DIR = ROOT_DIR / "web"
LOOT_DIR = ROOT_DIR / "loot"
PAYLOADS_DIR = ROOT_DIR / "payloads"
TAILSCALE_KEY_PATH = ROOT_DIR / ".tailscale_auth_key"
TAILSCALE_STATUS_PATH = Path("/dev/shm/rj_tailscale_status.json")

# ---------------------------------------------------------------------------
# ISM Manager — runs rtl_433 as a background subprocess
# ---------------------------------------------------------------------------

_ISM_BANDS = [
    {"name": "433 MHz", "freq": 433920000, "desc": "EU ISM / Remotes"},
    {"name": "315 MHz", "freq": 315000000, "desc": "US Remotes / TPMS"},
    {"name": "868 MHz", "freq": 868000000, "desc": "EU ISM / LoRa"},
    {"name": "345 MHz", "freq": 345000000, "desc": "Honeywell Security"},
    {"name": "915 MHz", "freq": 915000000, "desc": "US ISM"},
]
_ISM_LIVE = Path("/dev/shm/rj_ism_live.json")
_ISM_HISTORY_MAX = 60
_ISM_HISTORY_METRICS = (
    "temperature_C", "humidity", "pressure_kPa", "wind_avg_km_h",
    "rain_mm", "pressure_PSI", "temperature_F", "uv", "light_lux",
    "power_W", "energy_kWh", "moisture",
)

_ISM_CAT_KEYWORDS = {
    "remote": ["remote", "came", "nice", "gate", "garage", "button", "keyfob"],
    "weather": ["weather", "temp", "humid", "rain", "wind", "baro", "thermo"],
    "sensor": ["sensor", "motion", "door", "window", "alarm", "smoke", "pir"],
    "tpms": ["tpms", "tire", "pressure"],
    "car": ["car", "auto", "key", "fob", "vehicle"],
}


def _ism_empty_state():
    return {"ts": 0, "running": False, "total_signals": 0,
            "unique_devices": 0, "devices": [], "recent": [],
            "band": "", "command": "", "bands": _ISM_BANDS,
            "proto_counts": {}}


def _ism_categorize(model):
    text = model.lower()
    for cat, kws in _ISM_CAT_KEYWORDS.items():
        for kw in kws:
            if kw in text:
                return cat
    return "other"


class _ISMManager:
    def __init__(self):
        self._proc = None
        self._thread = None
        self._writer_thread = None
        self._lock = threading.Lock()
        self._signals = []
        self._devices = {}
        self._proto_counts = {}
        self._band_idx = 0
        self._start_time = 0
        self._running = False
        self._cmd = ""

    def start(self, band_idx=0):
        self.stop()
        if band_idx < 0 or band_idx >= len(_ISM_BANDS):
            band_idx = 0
        self._band_idx = band_idx
        freq = _ISM_BANDS[band_idx]["freq"]
        self._signals = []
        self._devices = {}
        self._proto_counts = {}
        self._start_time = time.time()
        self._running = True

        cmd = [
            "rtl_433", "-f", str(freq), "-g", "20",
            "-s", "1024000",
            "-F", "json",
            "-M", "time:unix", "-M", "protocol", "-M", "level",
            "-X", "n=CAME-12,m=OOK_PWM,s=320,l=640,r=15000,g=800,t=0,y=1650,bits>=12",
            "-X", "n=Princeton,m=OOK_PWM,s=320,l=640,r=15000,g=800,t=0,y=1650,bits>=24",
            "-X", "n=NiceFLO,m=OOK_PWM,s=700,l=1400,r=15000,g=1600,t=0,y=0,bits>=12",
        ]
        self._cmd = " ".join(cmd)

        try:
            subprocess.run(["pkill", "-9", "rtl_433"], capture_output=True)
            time.sleep(0.3)
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )
            self._thread = threading.Thread(target=self._reader, daemon=True)
            self._thread.start()
            self._writer_thread = threading.Thread(target=self._writer, daemon=True)
            self._writer_thread.start()
        except Exception:
            self._running = False

    def stop(self):
        self._running = False
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        subprocess.run(["pkill", "-9", "rtl_433"], capture_output=True)
        try:
            _ISM_LIVE.unlink(missing_ok=True)
        except Exception:
            pass

    def _reader(self):
        last_sig = {}
        try:
            for line in self._proc.stdout:
                if not self._running:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                model = data.get("model", "Unknown")
                sig_key = f"{model}_{data.get('id', '')}_{data.get('channel', '')}"
                sig_vals = {k: v for k, v in data.items()
                            if k not in ("time", "rssi", "snr", "noise", "mic")}
                sig_hash = str(sig_vals)
                now = time.time()
                if sig_key in last_sig:
                    lh, lt = last_sig[sig_key]
                    if sig_hash == lh and (now - lt) < 2.0:
                        continue
                last_sig[sig_key] = (sig_hash, now)

                from datetime import datetime as _dt
                data["_time_local"] = _dt.now().strftime("%H:%M:%S")
                cat = _ism_categorize(model)
                data["_category"] = cat

                with self._lock:
                    self._signals.append(data)
                    if len(self._signals) > 500:
                        self._signals.pop(0)
                    self._proto_counts[model] = self._proto_counts.get(model, 0) + 1
                    self._update_device(data, now)
        except Exception:
            pass

    def _update_device(self, data, now):
        model = data.get("model", "Unknown")
        dev_id = data.get("id", "")
        channel = data.get("channel", "")
        key = f"{model}_{dev_id}_{channel}"
        entry = self._devices.get(key, {"first_seen": now, "count": 0, "history": {}})
        rssi = data.get("rssi", data.get("snr", None))
        entry.update({
            "model": model, "id": dev_id, "channel": channel,
            "category": data.get("_category", "other"),
            "last_seen": now, "count": entry["count"] + 1,
            "rssi": rssi,
        })
        for k in ("temperature_C", "humidity", "battery_ok", "pressure_kPa",
                   "wind_avg_km_h", "rain_mm", "code", "button", "status",
                   "pressure_PSI", "temperature_F", "uv", "light_lux",
                   "moisture", "depth_cm", "power_W", "energy_kWh"):
            if k in data:
                entry[k] = data[k]
        hist = entry.get("history", {})
        for k in _ISM_HISTORY_METRICS:
            val = data.get(k) if k != "rssi" else rssi
            if val is not None and isinstance(val, (int, float)):
                pts = hist.get(k, [])
                pts.append([round(now, 1), round(val, 2)])
                if len(pts) > _ISM_HISTORY_MAX:
                    pts = pts[-_ISM_HISTORY_MAX:]
                hist[k] = pts
        entry["history"] = hist
        self._devices[key] = entry

    def _writer(self):
        while self._running:
            try:
                with self._lock:
                    recent = list(self._signals[-50:])
                    dev_list = sorted(self._devices.values(),
                                      key=lambda d: d.get("last_seen", 0), reverse=True)
                payload = {
                    "ts": time.time(),
                    "running": self._running,
                    "total_signals": len(self._signals),
                    "unique_devices": len(self._devices),
                    "uptime": int(time.time() - self._start_time),
                    "proto_counts": dict(self._proto_counts),
                    "devices": dev_list[:100],
                    "recent": recent,
                    "band": _ISM_BANDS[self._band_idx]["name"],
                    "freq": _ISM_BANDS[self._band_idx]["freq"],
                    "command": self._cmd,
                    "bands": _ISM_BANDS,
                }
                tmp = str(_ISM_LIVE) + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(payload, f)
                os.replace(tmp, str(_ISM_LIVE))
            except Exception:
                pass
            time.sleep(1.5)


_ism_manager = _ISMManager()


# ---------------------------------------------------------------------------
# Scanner Manager — radio frequency scanner using rtl_fm (watchlist mode)
# ---------------------------------------------------------------------------

_SCANNER_BANDS = [
    {"name": "Airband", "start": 118000000, "end": 137000000, "mod": "am",
     "rate": 16000, "step": 25000, "desc": "Aviation 118-137 MHz",
     "sdr_rate": 48000},
    {"name": "Marine VHF", "start": 156000000, "end": 163000000, "mod": "fm",
     "rate": 16000, "step": 25000, "desc": "Marine 156-163 MHz",
     "sdr_rate": 24000},
    {"name": "PMR446", "start": 446006250, "end": 446193750, "mod": "fm",
     "rate": 16000, "step": 12500, "desc": "PMR446 446.0-446.2 MHz",
     "sdr_rate": 24000},
    {"name": "FM Broadcast", "start": 87500000, "end": 108000000, "mod": "wbfm",
     "rate": 32000, "step": 100000, "desc": "FM Radio 87.5-108 MHz",
     "sdr_rate": 170000},
    {"name": "SAMU/Emergency", "start": 150000000, "end": 174000000, "mod": "fm",
     "rate": 16000, "step": 12500, "desc": "Emergency 150-174 MHz",
     "sdr_rate": 24000},
]
_SCANNER_LIVE = Path("/dev/shm/rj_scanner_live.json")
_SCANNER_AUDIO = Path("/dev/shm/rj_scanner_audio.pcm")
_SCANNER_ACTIVITY_MAX = 100


# ---------------------------------------------------------------------------
# FM Station Database — location-based lookup for FM Broadcast band
# ---------------------------------------------------------------------------

_FM_STATIONS_CACHE = ROOT_DIR / "config" / "fm_stations.json"
_FM_STATIONS: list[dict] = []
_FM_COUNTRY: str = ""
_FM_LAT: float = 0.0
_FM_LON: float = 0.0
_FM_LOADING: bool = False
_FM_LAST_ERROR: str = ""
_FM_LOCK = threading.Lock()


def _fm_get_location() -> tuple[str, float, float]:
    """Detect country and coordinates from GNSS, observer config, or IP."""
    country = ""
    lat, lon = 0.0, 0.0

    # 1. Try GNSS live data
    try:
        gnss_path = Path("/dev/shm/rj_gnss_live.json")
        if gnss_path.exists():
            gnss = json.loads(gnss_path.read_text(encoding="utf-8"))
            fix = gnss.get("fix", {})
            glat = fix.get("lat", 0)
            glon = fix.get("lon", 0)
            if glat and glon:
                lat, lon = float(glat), float(glon)
    except Exception:
        pass

    # 2. Try observer config
    if not lat:
        try:
            obs_path = ROOT_DIR / "config" / "observer.json"
            if obs_path.exists():
                obs = json.loads(obs_path.read_text(encoding="utf-8"))
                olat = obs.get("lat", 0)
                olon = obs.get("lon", 0)
                if olat and olon:
                    lat, lon = float(olat), float(olon)
        except Exception:
            pass

    # 3. IP geolocation fallback
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://ip-api.com/json/",
            headers={"User-Agent": "RaspyJack/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
            country = data.get("countryCode", "")
            if not lat:
                lat = float(data.get("lat", 0))
                lon = float(data.get("lon", 0))
    except Exception:
        pass

    if not country:
        country = "FR"  # default fallback

    return country, lat, lon


def _fm_download_anfr(lat: float, lon: float) -> list[dict]:
    """Download FM stations from ANFR open data (France only).

    Uses the ANFR open-data portal to fetch FM transmitter locations and
    frequencies near the given coordinates, sorted by distance.
    """
    import urllib.request
    import urllib.parse
    import math

    stations: list[dict] = []

    # ANFR open data: query FM stations
    # The dataset "anfr-support" contains all broadcast transmitters
    base_url = "https://data.anfr.fr/api/explore/v2.1/catalog/datasets/observatoire_2g_3g_4g/exports/json"

    # Alternative: use the "donnees-sur-les-installations-radioelectriques" dataset
    # which has FM broadcast stations
    try:
        # Try the radioelectric installations dataset with FM filter
        api_url = (
            "https://data.anfr.fr/api/explore/v2.1/catalog/datasets/"
            "donnees-sur-les-installations-radioelectriques-de-plus-de-5-watts-702a5/exports/json"
            "?where=nature_emetteur%3D%22FM%22"
            "&limit=5000"
            "&select=nom_station,frequence_mhz,coordonnees,commune"
        )
        req = urllib.request.Request(
            api_url, headers={"User-Agent": "RaspyJack/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))

        for entry in data:
            freq_mhz = entry.get("frequence_mhz")
            name = entry.get("nom_station", "").strip()
            city = entry.get("commune", "").strip()
            coords = entry.get("coordonnees", {})

            if not freq_mhz or not name:
                continue

            try:
                freq_mhz = float(freq_mhz)
            except (ValueError, TypeError):
                continue

            # Only keep FM broadcast range (87.5 - 108 MHz)
            if freq_mhz < 87.5 or freq_mhz > 108.0:
                continue

            slat = float(coords.get("lat", 0)) if coords else 0
            slon = float(coords.get("lon", 0)) if coords else 0

            # Calculate distance if we have coordinates
            dist = 9999.0
            if lat and lon and slat and slon:
                dlat = math.radians(slat - lat)
                dlon = math.radians(slon - lon)
                a = (math.sin(dlat / 2) ** 2
                     + math.cos(math.radians(lat))
                     * math.cos(math.radians(slat))
                     * math.sin(dlon / 2) ** 2)
                dist = 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

            stations.append({
                "freq": int(round(freq_mhz * 1e6)),
                "freq_mhz": round(freq_mhz, 1),
                "name": name,
                "city": city,
                "dist_km": round(dist, 1),
            })

        # Sort by distance, keep only stations within 150 km
        stations = sorted(stations, key=lambda s: s["dist_km"])
        stations = [s for s in stations if s["dist_km"] <= 150]

        # Deduplicate by frequency (keep closest)
        seen_freqs: set[int] = set()
        unique: list[dict] = []
        for s in stations:
            freq_key = round(s["freq"] / 100000)  # group within 100kHz
            if freq_key not in seen_freqs:
                seen_freqs.add(freq_key)
                unique.append(s)
        return unique

    except Exception:
        return []


def _fm_download_radio_browser(country: str) -> list[dict]:
    """Fallback: download station names from Radio Browser API.

    This gives internet radio station names (not FM frequencies), but many
    share names with their FM counterparts, useful for display purposes.
    """
    import urllib.request

    stations: list[dict] = []
    try:
        url = (
            f"https://de1.api.radio-browser.info/json/stations/search"
            f"?limit=200&countrycode={country}"
            f"&order=clickcount&reverse=true&hidebroken=true"
        )
        req = urllib.request.Request(
            url, headers={"User-Agent": "RaspyJack/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))

        for s in data:
            name = s.get("name", "").strip()
            if not name:
                continue
            stations.append({
                "name": name,
                "country": s.get("countrycode", ""),
                "tags": s.get("tags", ""),
                "votes": s.get("votes", 0),
            })
    except Exception:
        pass
    return stations


# Built-in French FM station database (major national stations and typical
# frequencies).  These are the most common frequencies for the main French
# national radio networks.  Because FM frequencies vary by transmitter
# site, only the most widespread allocations are listed.
_FM_BUILTIN_FR: list[dict] = [
    {"freq": 87600000, "freq_mhz": 87.6, "name": "FRANCE INTER", "city": ""},
    {"freq": 87800000, "freq_mhz": 87.8, "name": "FRANCE CULTURE", "city": ""},
    {"freq": 88200000, "freq_mhz": 88.2, "name": "FRANCE MUSIQUE", "city": ""},
    {"freq": 89000000, "freq_mhz": 89.0, "name": "RFI", "city": ""},
    {"freq": 89900000, "freq_mhz": 89.9, "name": "TSF JAZZ", "city": ""},
    {"freq": 90400000, "freq_mhz": 90.4, "name": "NOSTALGIE", "city": ""},
    {"freq": 90900000, "freq_mhz": 90.9, "name": "CHERIE FM", "city": ""},
    {"freq": 91300000, "freq_mhz": 91.3, "name": "FRANCE INFO", "city": ""},
    {"freq": 91700000, "freq_mhz": 91.7, "name": "FRANCE BLEU", "city": ""},
    {"freq": 92100000, "freq_mhz": 92.1, "name": "LE MOUV'", "city": ""},
    {"freq": 93100000, "freq_mhz": 93.1, "name": "FIP", "city": ""},
    {"freq": 93500000, "freq_mhz": 93.5, "name": "EUROPE 1", "city": ""},
    {"freq": 93900000, "freq_mhz": 93.9, "name": "RFM", "city": ""},
    {"freq": 94800000, "freq_mhz": 94.8, "name": "RIRE ET CHANSONS", "city": ""},
    {"freq": 95600000, "freq_mhz": 95.6, "name": "CONTACT FM", "city": ""},
    {"freq": 96000000, "freq_mhz": 96.0, "name": "SKYROCK", "city": ""},
    {"freq": 96400000, "freq_mhz": 96.4, "name": "BFM BUSINESS", "city": ""},
    {"freq": 97100000, "freq_mhz": 97.1, "name": "FRANCE INTER", "city": ""},
    {"freq": 97400000, "freq_mhz": 97.4, "name": "FRANCE CULTURE", "city": ""},
    {"freq": 98200000, "freq_mhz": 98.2, "name": "RADIO CLASSIQUE", "city": ""},
    {"freq": 98800000, "freq_mhz": 98.8, "name": "RMC", "city": ""},
    {"freq": 99000000, "freq_mhz": 99.0, "name": "FUN RADIO", "city": ""},
    {"freq": 100300000, "freq_mhz": 100.3, "name": "NRJ", "city": ""},
    {"freq": 100700000, "freq_mhz": 100.7, "name": "OUIFM", "city": ""},
    {"freq": 101100000, "freq_mhz": 101.1, "name": "VIRGIN RADIO", "city": ""},
    {"freq": 101500000, "freq_mhz": 101.5, "name": "RADIO NOVA", "city": ""},
    {"freq": 101900000, "freq_mhz": 101.9, "name": "RTL2", "city": ""},
    {"freq": 103500000, "freq_mhz": 103.5, "name": "RTL", "city": ""},
    {"freq": 104300000, "freq_mhz": 104.3, "name": "RFM", "city": ""},
    {"freq": 104700000, "freq_mhz": 104.7, "name": "EUROPE 2", "city": ""},
    {"freq": 105100000, "freq_mhz": 105.1, "name": "FIP", "city": ""},
    {"freq": 105500000, "freq_mhz": 105.5, "name": "FRANCE INFO", "city": ""},
    {"freq": 106200000, "freq_mhz": 106.2, "name": "RMC INFO", "city": ""},
    {"freq": 106700000, "freq_mhz": 106.7, "name": "BFM", "city": ""},
    {"freq": 107100000, "freq_mhz": 107.1, "name": "FRANCE BLEU", "city": ""},
    {"freq": 107700000, "freq_mhz": 107.7, "name": "MOUV'", "city": ""},
]


def _fm_load_cache() -> bool:
    """Load FM stations from disk cache. Return True if loaded."""
    global _FM_STATIONS, _FM_COUNTRY, _FM_LAT, _FM_LON
    try:
        if _FM_STATIONS_CACHE.exists():
            data = json.loads(_FM_STATIONS_CACHE.read_text(encoding="utf-8"))
            age = time.time() - data.get("ts", 0)
            if age < 86400:  # cache valid for 24 hours
                _FM_STATIONS = data.get("stations", [])
                _FM_COUNTRY = data.get("country", "")
                _FM_LAT = data.get("lat", 0.0)
                _FM_LON = data.get("lon", 0.0)
                return True
    except Exception:
        pass
    return False


def _fm_save_cache() -> None:
    """Persist FM stations to disk cache."""
    try:
        _FM_STATIONS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _FM_STATIONS_CACHE.write_text(json.dumps({
            "country": _FM_COUNTRY,
            "lat": _FM_LAT,
            "lon": _FM_LON,
            "stations": _FM_STATIONS,
            "ts": time.time(),
        }), encoding="utf-8")
    except Exception:
        pass


def _fm_snap_freq(freq_hz: int) -> int:
    """Snap a detected frequency to the nearest known FM station (±50kHz)."""
    with _FM_LOCK:
        stations = _FM_STATIONS
    if not stations:
        return freq_hz
    best_dist = 50001
    best_freq = freq_hz
    for s in stations:
        sf = s.get("freq", 0)
        if not sf:
            continue
        dist = abs(freq_hz - sf)
        if dist < best_dist:
            best_dist = dist
            best_freq = sf
    return best_freq if best_dist <= 50000 else freq_hz


def _fm_download_stations() -> None:
    """Background task: download FM stations based on detected location."""
    global _FM_STATIONS, _FM_COUNTRY, _FM_LAT, _FM_LON
    global _FM_LOADING, _FM_LAST_ERROR

    with _FM_LOCK:
        if _FM_LOADING:
            return
        _FM_LOADING = True

    try:
        country, lat, lon = _fm_get_location()

        with _FM_LOCK:
            _FM_COUNTRY = country
            _FM_LAT = lat
            _FM_LON = lon

        stations: list[dict] = []

        # For France, try ANFR open data first
        if country == "FR":
            stations = _fm_download_anfr(lat, lon)

        # If ANFR failed or not France, use built-in database
        if not stations and country == "FR":
            stations = list(_FM_BUILTIN_FR)
            for s in stations:
                s["dist_km"] = 0

        with _FM_LOCK:
            _FM_STATIONS = stations
            _FM_LAST_ERROR = ""

        _fm_save_cache()

    except Exception as exc:
        with _FM_LOCK:
            _FM_LAST_ERROR = str(exc)
    finally:
        with _FM_LOCK:
            _FM_LOADING = False


def _fm_ensure_loaded() -> None:
    """Ensure FM stations are loaded, triggering download if needed."""
    with _FM_LOCK:
        if _FM_STATIONS or _FM_LOADING:
            return

    if _fm_load_cache():
        return

    # Start background download
    threading.Thread(target=_fm_download_stations, daemon=True).start()


def _fm_get_stations_response() -> dict:
    """Build the API response for FM stations."""
    _fm_ensure_loaded()
    with _FM_LOCK:
        return {
            "country": _FM_COUNTRY,
            "lat": _FM_LAT,
            "lon": _FM_LON,
            "loading": _FM_LOADING,
            "error": _FM_LAST_ERROR,
            "count": len(_FM_STATIONS),
            "stations": list(_FM_STATIONS),
        }


class _ScannerManager:
    """Manages an rtl_fm subprocess for radio frequency scanning."""

    _DEFAULT_WATCHLIST = [
        {"freq": 121500000, "label": "Emergency", "mod": "usb"},
        {"freq": 123450000, "label": "Air-Air", "mod": "usb"},
        {"freq": 156800000, "label": "Marine Ch16", "mod": "fm"},
        {"freq": 446006250, "label": "PMR446 Ch1", "mod": "fm"},
    ]
    _DWELL_TIME = 0.8
    _HOLD_DELAY = 3.0
    _HOLD_MAX = 15

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()
        self._running = False
        self._mode = "scan"
        self._band_idx = 0
        self._freq = 0
        self._squelch = 50
        self._signal_level = 0.0
        self._scanning = False
        self._paused_on_signal = False
        self._signal_start = 0.0
        self._skip_signal = False
        self._activity_log: list[dict] = []
        self._start_time = 0.0
        self._watchlist: list[dict] = list(self._DEFAULT_WATCHLIST)
        self._watch_idx = 0
        self._priority_idx = 0
        self._rds: dict = {"ps": "", "rt": "", "pty": "", "pi": ""}
        self._rds_proc = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self, band_idx: int = 0, mode: str = "scan",
              freq: int = 0, squelch: int = 50) -> None:
        self.stop()
        if band_idx < 0 or band_idx >= len(_SCANNER_BANDS):
            band_idx = 0
        self._band_idx = band_idx
        self._mode = mode
        self._squelch = max(0, min(100, squelch))
        self._activity_log = []
        self._paused_on_signal = False
        self._skip_signal = False
        self._signal_level = 0.0
        self._start_time = time.time()
        self._watch_idx = 0

        band = _SCANNER_BANDS[band_idx]
        if mode == "manual" and freq > 0:
            self._freq = int(freq)
        else:
            self._freq = band["start"]

        self._scanning = mode == "scan"

        for prog in ("rtl_fm", "rtl_433", "rtl_adsb", "rtl_sdr", "rtl_power"):
            subprocess.run(["pkill", "-9", prog], capture_output=True)
        time.sleep(0.3)

        try:
            _SCANNER_AUDIO.unlink(missing_ok=True)
        except Exception:
            pass

        self._running = True

        threading.Thread(target=self._writer, daemon=True).start()
        if mode == "scan":
            threading.Thread(target=self._scan_loop, daemon=True).start()
        else:
            self._start_rtl_fm()

    def stop(self) -> None:
        self._running = False
        self._scanning = False
        self._stop_rds()
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        subprocess.run(["pkill", "-9", "rtl_fm"], capture_output=True)
        subprocess.run(["pkill", "-9", "redsea"], capture_output=True)
        try:
            _SCANNER_LIVE.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            _SCANNER_AUDIO.unlink(missing_ok=True)
        except Exception:
            pass

    def _stop_rds(self) -> None:
        if self._rds_proc:
            try:
                self._rds_proc.terminate()
                self._rds_proc.wait(timeout=2)
            except Exception:
                try:
                    self._rds_proc.kill()
                except Exception:
                    pass
            self._rds_proc = None
        self._rds = {"ps": "", "rt": "", "pty": "", "pi": ""}

    def _start_rtl_fm(self) -> None:
        """Launch rtl_fm for the current frequency and modulation."""
        self._stop_rds()
        band = _SCANNER_BANDS[self._band_idx]
        mod_flag = band["mod"]
        sdr_rate = band.get("sdr_rate", band["rate"])
        out_rate = band["rate"]
        is_fm_broadcast = band["name"] == "FM Broadcast"

        cmd = [
            "rtl_fm",
            "-M", mod_flag,
            "-f", str(self._freq),
            "-s", str(sdr_rate),
            "-r", str(out_rate),
            "-l", "0",
            "-g", "49.6",
            "-E", "deemp",
            "-A", "fast",
        ]

        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            threading.Thread(target=self._reader, daemon=True).start()

        except Exception:
            self._proc = None

    def _rds_quick_decode(self) -> None:
        """Quick RDS decode: brief MPX capture before audio starts."""
        if not self._running:
            return
        freq = self._freq
        try:
            cmd = (f"rtl_fm -f {freq} -M fm -s 171000 -l 0 -g 49.6"
                   f" - 2>/dev/null | timeout 4 redsea --input mpx"
                   f" -r 171000 -p 2>/dev/null")
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=8,
            )
            for line in (result.stdout or "").strip().splitlines():
                try:
                    data = json.loads(line)
                    with self._lock:
                        if "ps" in data:
                            self._rds["ps"] = data["ps"].strip()
                        elif "partial_ps" in data:
                            ps = data["partial_ps"].strip()
                            if len(ps) > len(self._rds.get("ps", "")):
                                self._rds["ps"] = ps
                        if "radiotext" in data:
                            self._rds["rt"] = data["radiotext"].strip()
                        elif "partial_radiotext" in data:
                            rt = data["partial_radiotext"].strip()
                            if len(rt) > len(self._rds.get("rt", "")):
                                self._rds["rt"] = rt
                        if "prog_type" in data and data["prog_type"] != "No PTY":
                            self._rds["pty"] = data["prog_type"]
                        if "pi" in data:
                            self._rds["pi"] = data["pi"]
                except (json.JSONDecodeError, KeyError):
                    pass
        except Exception:
            pass

    def _rds_parser(self) -> None:
        """Parse JSON RDS data from redsea stderr."""
        proc = self._rds_proc
        if not proc:
            return
        try:
            for line in proc.stderr:
                if not self._running:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    with self._lock:
                        if "ps" in data:
                            self._rds["ps"] = data["ps"].strip()
                        elif "partial_ps" in data:
                            ps = data["partial_ps"].strip()
                            if len(ps) > len(self._rds.get("ps", "")):
                                self._rds["ps"] = ps
                        if "radiotext" in data:
                            self._rds["rt"] = data["radiotext"].strip()
                        elif "partial_radiotext" in data:
                            rt = data["partial_radiotext"].strip()
                            if len(rt) > len(self._rds.get("rt", "")):
                                self._rds["rt"] = rt
                        if "prog_type" in data and data["prog_type"] != "No PTY":
                            self._rds["pty"] = data["prog_type"]
                        if "pi" in data:
                            self._rds["pi"] = data["pi"]
                except (json.JSONDecodeError, KeyError):
                    pass
        except Exception:
            pass

    def _reader(self) -> None:
        """Read PCM data from rtl_fm stdout, write to buffer, compute signal."""
        import math as _math
        import struct as _struct

        proc = self._proc
        if not proc:
            return
        try:
            while self._running and proc.poll() is None:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                try:
                    with open(str(_SCANNER_AUDIO), "ab") as f:
                        f.write(chunk)
                except Exception:
                    pass
                try:
                    n = len(chunk) // 2
                    if n > 0:
                        samples = _struct.unpack(f"<{n}h", chunk[:n * 2])
                        rms = _math.sqrt(sum(s * s for s in samples) / n)
                        db = 20 * _math.log10(max(rms, 1) / 32768)
                        level = max(0.0, min(100.0, (db + 40) * 2.5))
                        with self._lock:
                            self._signal_level = round(level, 1)
                except Exception:
                    pass
        except Exception:
            pass

    def _scan_loop(self) -> None:
        """Hybrid scanner: rtl_power sweep → listen to active freqs → repeat."""
        while self._running and self._mode == "scan":
            band = _SCANNER_BANDS[self._band_idx]

            # Phase 1: fast sweep
            with self._lock:
                self._paused_on_signal = False
                self._scanning = True
            active_freqs = self._quick_sweep(band)

            if not self._running or self._mode != "scan":
                break

            if not active_freqs:
                time.sleep(0.5)
                continue

            # Phase 2: listen to each active freq
            is_fm = band["name"] == "FM Broadcast"
            for freq in active_freqs:
                if not self._running or self._mode != "scan":
                    break

                if is_fm:
                    freq = _fm_snap_freq(freq)

                with self._lock:
                    self._freq = freq
                    self._scanning = True
                    self._paused_on_signal = False
                self._retune()
                time.sleep(0.5)

                # HOLD — rtl_power already confirmed signal, listen directly
                with self._lock:
                    self._paused_on_signal = True
                    self._scanning = False
                    self._signal_start = time.time()
                    self._skip_signal = False

                hold_start = time.time()
                while self._running and self._mode == "scan":
                    time.sleep(0.2)
                    elapsed = time.time() - hold_start
                    with self._lock:
                        skip = self._skip_signal
                    if skip:
                        break
                    if elapsed >= self._HOLD_MAX:
                        break

                if self._running and self._mode == "scan":
                    duration = time.time() - self._signal_start
                    self._log_activity(duration)
                    with self._lock:
                        self._paused_on_signal = False
                        self._skip_signal = False

    def _quick_sweep(self, band: dict) -> list[int]:
        """Fast band sweep with rtl_power, return active frequencies."""
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        subprocess.run(["pkill", "-9", "rtl_fm"], capture_output=True)
        time.sleep(0.2)

        cmd = (f"rtl_power -f {band['start']}:{band['end']}:{band['step']}"
               f" -g 40 -i 1 -e 2 -1")
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, Exception):
            return []

        if not result.stdout:
            return []

        all_powers = []
        for line in result.stdout.strip().splitlines():
            parts = line.split(",")
            if len(parts) < 7:
                continue
            try:
                f_start = float(parts[2])
                f_step = float(parts[4])
                powers = [float(x) for x in parts[6:]]
                for i, p in enumerate(powers):
                    all_powers.append((int(f_start + i * f_step), p))
            except (ValueError, IndexError):
                continue

        if not all_powers:
            return []

        sorted_p = sorted(p for _, p in all_powers)
        median = sorted_p[len(sorted_p) // 2]
        threshold = median + 8.0

        active = sorted(
            [(f, p) for f, p in all_powers if p > threshold],
            key=lambda x: -x[1],
        )
        return [f for f, _ in active[:10]]

    def _step_freq(self, direction: int) -> None:
        """Step to next/prev frequency in band and retune rtl_fm."""
        band = _SCANNER_BANDS[self._band_idx]
        new_freq = self._freq + (band["step"] * direction)
        if new_freq > band["end"]:
            new_freq = band["start"]
        elif new_freq < band["start"]:
            new_freq = band["end"]
        with self._lock:
            self._freq = new_freq
        self._retune()

    def _change_band(self, idx: int) -> None:
        """Switch band without resetting mode/squelch."""
        if idx < 0 or idx >= len(_SCANNER_BANDS):
            return
        saved_mode = self._mode
        saved_sq = self._squelch
        self.stop()
        self._band_idx = idx
        self._freq = _SCANNER_BANDS[idx]["start"]
        self._squelch = saved_sq
        self._mode = saved_mode
        self._activity_log = []
        self._paused_on_signal = False
        self._signal_level = 0.0
        self._start_time = time.time()

        for prog in ("rtl_fm", "rtl_433", "rtl_adsb", "rtl_sdr", "rtl_power"):
            subprocess.run(["pkill", "-9", prog], capture_output=True)
        time.sleep(0.3)
        try:
            _SCANNER_AUDIO.unlink(missing_ok=True)
        except Exception:
            pass

        self._running = True
        self._start_rtl_fm()
        threading.Thread(target=self._writer, daemon=True).start()
        if self._mode == "scan":
            threading.Thread(target=self._scan_loop, daemon=True).start()

    def _retune(self) -> None:
        """Kill current rtl_fm and restart on current frequency."""
        self._rds = {"ps": "", "rt": "", "pty": "", "pi": ""}
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        time.sleep(0.1)
        self._start_rtl_fm()

    def _log_activity(self, duration: float) -> None:
        entry = {
            "ts": time.time(),
            "freq": self._freq,
            "freq_display": f"{self._freq / 1e6:.3f}",
            "signal": self._signal_level,
            "band": _SCANNER_BANDS[self._band_idx]["name"],
            "duration": round(duration, 1),
        }
        with self._lock:
            self._activity_log.append(entry)
            if len(self._activity_log) > _SCANNER_ACTIVITY_MAX:
                self._activity_log.pop(0)

    def _writer(self) -> None:
        """Persist live state to JSON every second."""
        while self._running:
            try:
                with self._lock:
                    band = _SCANNER_BANDS[self._band_idx]
                    payload = {
                        "ts": time.time(),
                        "running": self._running,
                        "mode": self._mode,
                        "band": band["name"],
                        "band_idx": self._band_idx,
                        "freq": self._freq,
                        "freq_display": f"{self._freq / 1e6:.3f}",
                        "modulation": band["mod"],
                        "squelch": self._squelch,
                        "signal_level": self._signal_level,
                        "scanning": self._mode == "scan"
                        and not self._paused_on_signal,
                        "paused_on_signal": self._paused_on_signal,
                        "sample_rate": band["rate"],
                        "watchlist": list(self._watchlist),
                        "watch_idx": self._watch_idx % max(1, len(self._watchlist)),
                        "priority_idx": self._priority_idx,
                        "activity_log": list(
                            self._activity_log[-_SCANNER_ACTIVITY_MAX:]
                        ),
                        "rds": dict(self._rds) if band["name"] == "FM Broadcast" else None,
                        "bands": [
                            {
                                "name": b["name"],
                                "desc": b["desc"],
                                "start": b["start"],
                                "end": b["end"],
                                "mod": b["mod"],
                            }
                            for b in _SCANNER_BANDS
                        ],
                    }
                tmp = str(_SCANNER_LIVE) + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(payload, f)
                os.replace(tmp, str(_SCANNER_LIVE))
            except Exception:
                pass
            time.sleep(1.0)

    def control(self, action: str, **kwargs) -> dict:
        """Handle control commands."""
        if action == "tune":
            freq = int(kwargs.get("freq", self._freq))
            if _SCANNER_BANDS[self._band_idx]["name"] == "FM Broadcast":
                freq = _fm_snap_freq(freq)
            with self._lock:
                self._freq = freq
                self._mode = "manual"
                self._scanning = False
                self._paused_on_signal = False
            self._retune()
            return {"ok": True}

        if action == "squelch":
            with self._lock:
                self._squelch = max(0, min(100, int(kwargs.get("level", 50))))
            return {"ok": True}

        if action == "scan":
            with self._lock:
                self._mode = "scan"
                self._scanning = True
                self._paused_on_signal = False
            threading.Thread(target=self._scan_loop, daemon=True).start()
            return {"ok": True}

        if action == "hold":
            with self._lock:
                self._mode = "manual"
                self._scanning = False
                self._paused_on_signal = False
            return {"ok": True}

        if action == "band":
            idx = int(kwargs.get("idx", 0))
            if 0 <= idx < len(_SCANNER_BANDS):
                if self._running:
                    self._change_band(idx)
                else:
                    self._band_idx = idx
                    self._freq = _SCANNER_BANDS[idx]["start"]
            return {"ok": True}

        if action == "step":
            direction = int(kwargs.get("dir", 1))
            self._step_freq(direction)
            return {"ok": True}

        if action == "next":
            if self._mode == "scan":
                with self._lock:
                    self._skip_signal = True
            else:
                self._step_freq(1)
            return {"ok": True}

        if action == "hold_time":
            seconds = int(kwargs.get("seconds", 15))
            self._HOLD_MAX = max(3, min(120, seconds))
            return {"ok": True}

        if action == "watchlist":
            freqs = kwargs.get("freqs", [])
            if isinstance(freqs, list) and freqs:
                wl = []
                for item in freqs:
                    if isinstance(item, dict) and "freq" in item:
                        wl.append({
                            "freq": int(item["freq"]),
                            "label": str(item.get("label", "")),
                            "mod": str(item.get("mod", "usb")),
                        })
                    elif isinstance(item, (int, float)):
                        wl.append({"freq": int(item), "label": "", "mod": "usb"})
                if wl:
                    with self._lock:
                        self._watchlist = wl
                        self._watch_idx = 0
                        self._priority_idx = 0
            return {"ok": True, "count": len(self._watchlist)}

        if action == "priority":
            idx = int(kwargs.get("idx", 0))
            with self._lock:
                if 0 <= idx < len(self._watchlist):
                    self._priority_idx = idx
            return {"ok": True}

        return {"error": f"unknown action: {action}"}


_scanner_manager = _ScannerManager()


# ---------------------------------------------------------------------------
# Generic payload process manager — direct subprocess, no LCD payload dance
# ---------------------------------------------------------------------------

class _PayloadRunner:
    """Manages a payload Python script as a direct subprocess."""

    def __init__(self, name: str, script_path: str, live_path: str, kill_cmd: str | None = None):
        self._name = name
        self._script = script_path
        self._live = Path(live_path)
        self._kill_cmd = kill_cmd
        self._proc = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, extra_args: list[str] | None = None):
        self.stop()
        cmd = [sys.executable, self._script, "--headless"]
        if extra_args:
            cmd.extend(extra_args)
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            self._proc = None

    def stop(self):
        with self._lock:
            if self._proc:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=5)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                self._proc = None
            if self._kill_cmd:
                subprocess.run(["pkill", "-9", "-f", self._kill_cmd],
                               capture_output=True)
            if str(self._live):
                try:
                    self._live.unlink(missing_ok=True)
                except Exception:
                    pass


def _kill_payload(script_name: str) -> None:
    subprocess.run(["pkill", "-f", script_name], capture_output=True)
    time.sleep(0.3)
    subprocess.run(["pkill", "-9", "-f", script_name], capture_output=True)


_honeypot_runner = _PayloadRunner(
    "Honeypot", str(PAYLOADS_DIR / "reconnaissance" / "honeypot_siem.py"),
    "/dev/shm/rj_honeypot_live.json", "honeypot_siem.py",
)


# WebUI only listens on these interfaces — wlan1+ are for attacks/monitor mode
WEBUI_INTERFACES = ["eth0", "eth1", "wlan0", "tailscale0"]


def _get_interface_ip(interface: str) -> str | None:
    """Get the IPv4 address of a network interface."""
    try:
        result = subprocess.run(
            ["ip", "-4", "addr", "show", interface],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if "inet " in line:
                    return line.split("inet ")[1].split("/")[0]
    except Exception:
        pass
    return None


def _get_webui_bind_addrs() -> list[tuple[str, str]]:
    """Return (ip, iface_label) pairs the WebUI should bind to."""
    addrs: list[tuple[str, str]] = []
    for iface in WEBUI_INTERFACES:
        ip = _get_interface_ip(iface)
        if ip:
            addrs.append((ip, iface))
    # Always include localhost for local access
    addrs.append(("127.0.0.1", "lo"))
    return addrs


_CPU_SNAPSHOT = None
_CPU_CORE_SNAPSHOTS: dict[int, tuple[int, int]] = {}


def _tailscale_write_status(payload: dict) -> None:
    """Persist last Tailscale install/bootstrap status for the WebUI."""
    try:
        TAILSCALE_STATUS_PATH.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass


def _tailscale_read_status() -> dict:
    try:
        if not TAILSCALE_STATUS_PATH.exists():
            return {}
        raw = TAILSCALE_STATUS_PATH.read_text(encoding="utf-8")
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _tailscale_installed() -> bool:
    """Return True if the tailscale CLI appears to be installed."""
    try:
        return shutil.which("tailscale") is not None
    except Exception:
        return False


def _tailscale_status() -> dict:
    """
    Best-effort snapshot of the Tailscale daemon.
    Returns {"backend_state": str|None, "ip": str|None}.
    """
    summary: dict[str, str | None] = {"backend_state": None, "ip": None}
    if not _tailscale_installed():
        return summary
    try:
        res = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode != 0 or not res.stdout:
            return summary
        data = json.loads(res.stdout)
        if not isinstance(data, dict):
            return summary
        summary["backend_state"] = str(data.get("BackendState") or "") or None
        self_info = data.get("Self") or {}
        if isinstance(self_info, dict):
            ips = self_info.get("TailscaleIPs") or []
            if isinstance(ips, list) and ips:
                summary["ip"] = str(ips[0])
    except Exception:
        pass
    return summary


def _tailscale_write_key(key: str) -> tuple[bool, str]:
    """Store the auth key in a root-only file so tailscale can read it."""
    value = str(key or "").strip()
    if not value:
        return False, "missing auth key"
    try:
        TAILSCALE_KEY_PATH.write_text(value + "\n", encoding="utf-8")
        try:
            os.chmod(TAILSCALE_KEY_PATH, 0o600)
        except Exception:
            # On some platforms chmod may fail; do not treat as fatal.
            pass
        return True, "ok"
    except Exception as exc:
        return False, f"write error: {exc}"


def _regenerate_caddyfile_and_reload() -> None:
    """
    Regenerate /etc/caddy/Caddyfile with current IPs (eth0, wlan0, tailscale0)
    and reload Caddy. Same logic as install_raspyjack.sh so that installing
    Tailscale from the WebUI updates HTTPS to listen on the Tailscale IP
    without re-running the install script.
    """
    hosts: list[str] = []
    for iface in ("eth0", "wlan0", "tailscale0"):
        try:
            res = subprocess.run(
                ["ip", "-4", "-o", "addr", "show", iface],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode != 0 or not res.stdout:
                continue
            # First line: "2: eth0    inet 192.168.1.100/24 ..." -> take 4th field, strip /suffix
            line = res.stdout.strip().split("\n")[0]
            parts = line.split()
            if len(parts) >= 4:
                addr = parts[3].split("/")[0].strip()
                if addr and addr not in hosts:
                    hosts.append(addr)
        except Exception:
            continue
    hosts.append("localhost")

    if not hosts:
        return

    caddy_site_addrs = ", ".join(hosts)
    caddyfile_content = f"""{{
    # RaspyJack self-signed internal CA (local trust only)
    auto_https disable_redirects
}}

{caddy_site_addrs} {{
    tls internal

    @ws path /ws*
    reverse_proxy @ws 127.0.0.1:8765 {{
        header_up X-Forwarded-Proto {{scheme}}
        header_up X-Forwarded-Host {{host}}
    }}

    reverse_proxy 127.0.0.1:8080 {{
        header_up X-Forwarded-Proto {{scheme}}
        header_up X-Forwarded-Host {{host}}
    }}
}}
"""

    tmp = Path("/dev/shm/rj_caddyfile_tmp")
    try:
        tmp.write_text(caddyfile_content, encoding="utf-8")
        subprocess.run(
            ["sudo", "cp", str(tmp), "/etc/caddy/Caddyfile"],
            check=True,
            timeout=10,
        )
        subprocess.run(
            ["sudo", "systemctl", "reload", "caddy"],
            check=True,
            timeout=15,
        )
    except Exception:
        pass
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def _tailscale_run_install_and_up() -> None:
    """
    Run the official install script and bring Tailscale up using the stored auth key.
    This is executed in a background thread so HTTP handlers can return quickly.
    """
    _tailscale_write_status({"installing": True, "ok": False, "error": None})

    try:
        if not TAILSCALE_KEY_PATH.exists():
            _tailscale_write_status({
                "installing": False,
                "ok": False,
                "error": "auth key not found",
            })
            return
    except Exception:
        _tailscale_write_status({
            "installing": False,
            "ok": False,
            "error": "auth key not found",
        })
        return

    # 1) Install Tailscale using the official script.
    try:
        install_res = subprocess.run(
            ["sh", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        _tailscale_write_status({
            "installing": False,
            "ok": False,
            "error": "tailscale install timeout",
        })
        return
    except Exception as exc:
        _tailscale_write_status({
            "installing": False,
            "ok": False,
            "error": str(exc),
        })
        return

    if install_res.returncode != 0:
        msg = (install_res.stderr or install_res.stdout or "").strip()
        if not msg:
            msg = f"tailscale install failed (code {install_res.returncode})"
        _tailscale_write_status({
            "installing": False,
            "ok": False,
            "error": msg[:200],
        })
        return

    # 2) Bring the daemon up using the stored auth key (non-interactive).
    try:
        auth_arg = f"--auth-key=file:{TAILSCALE_KEY_PATH}"
        up_res = subprocess.run(
            ["tailscale", "up", auth_arg, "--ssh"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        _tailscale_write_status({
            "installing": False,
            "ok": False,
            "error": "tailscale up timeout",
        })
        return
    except Exception as exc:
        _tailscale_write_status({
            "installing": False,
            "ok": False,
            "error": str(exc),
        })
        return

    if up_res.returncode != 0:
        msg = (up_res.stderr or up_res.stdout or "").strip()
        if not msg:
            msg = f"tailscale up failed (code {up_res.returncode})"
        _tailscale_write_status({
            "installing": False,
            "ok": False,
            "error": msg[:200],
        })
        return

    # Regenerate Caddyfile with tailscale0 IP and reload Caddy so HTTPS works over Tailscale.
    _regenerate_caddyfile_and_reload()

    _tailscale_write_status({
        "installing": False,
        "ok": True,
        "error": None,
    })


def _tailscale_run_reauth() -> None:
    """
    Re-authenticate an existing Tailscale install using the stored auth key.
    Does not re-run the install script, only `tailscale up --reset --auth-key=... --ssh`.
    """
    _tailscale_write_status({"installing": True, "ok": False, "error": None})

    try:
        if not TAILSCALE_KEY_PATH.exists():
            _tailscale_write_status({
                "installing": False,
                "ok": False,
                "error": "auth key not found",
            })
            return
    except Exception:
        _tailscale_write_status({
            "installing": False,
            "ok": False,
            "error": "auth key not found",
        })
        return

    try:
        auth_arg = f"--auth-key=file:{TAILSCALE_KEY_PATH}"
        up_res = subprocess.run(
            ["tailscale", "up", "--reset", auth_arg, "--ssh"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        _tailscale_write_status({
            "installing": False,
            "ok": False,
            "error": "tailscale up timeout",
        })
        return
    except Exception as exc:
        _tailscale_write_status({
            "installing": False,
            "ok": False,
            "error": str(exc),
        })
        return

    if up_res.returncode != 0:
        msg = (up_res.stderr or up_res.stdout or "").strip()
        if not msg:
            msg = f"tailscale up failed (code {up_res.returncode})"
        _tailscale_write_status({
            "installing": False,
            "ok": False,
            "error": msg[:200],
        })
        return

    _regenerate_caddyfile_and_reload()

    _tailscale_write_status({
        "installing": False,
        "ok": True,
        "error": None,
    })


def _read_cpu_percent() -> float:
    """Best-effort CPU usage based on /proc/stat delta."""
    global _CPU_SNAPSHOT
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            line = f.readline().strip()
        if not line.startswith("cpu "):
            return 0.0
        parts = [int(x) for x in line.split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
        if _CPU_SNAPSHOT is None:
            _CPU_SNAPSHOT = (idle, total)
            return 0.0
        prev_idle, prev_total = _CPU_SNAPSHOT
        _CPU_SNAPSHOT = (idle, total)
        idle_delta = idle - prev_idle
        total_delta = total - prev_total
        if total_delta <= 0:
            return 0.0
        pct = 100.0 * (1.0 - (idle_delta / total_delta))
        return max(0.0, min(100.0, pct))
    except Exception:
        return 0.0


def _read_cpu_per_core() -> list[float]:
    """Per-core CPU usage from /proc/stat delta."""
    global _CPU_CORE_SNAPSHOTS
    result = []
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines:
            if not line.startswith("cpu") or line.startswith("cpu "):
                continue
            label = line.split()[0]
            core_id = int(label[3:])
            parts = [int(x) for x in line.split()[1:]]
            idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
            total = sum(parts)
            prev = _CPU_CORE_SNAPSHOTS.get(core_id)
            if prev is None:
                _CPU_CORE_SNAPSHOTS[core_id] = (idle, total)
                result.append(0.0)
                continue
            prev_idle, prev_total = prev
            _CPU_CORE_SNAPSHOTS[core_id] = (idle, total)
            d_total = total - prev_total
            if d_total <= 0:
                result.append(0.0)
                continue
            pct = 100.0 * (1.0 - ((idle - prev_idle) / d_total))
            result.append(round(max(0.0, min(100.0, pct)), 1))
    except Exception:
        pass
    return result


def _read_meminfo() -> tuple[int, int, int, int]:
    """Return (mem_used, mem_total, swap_used, swap_total) in bytes."""
    try:
        vals = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                key, rest = line.split(":", 1)
                vals[key.strip()] = int(rest.strip().split()[0]) * 1024
        total = int(vals.get("MemTotal", 0))
        available = int(vals.get("MemAvailable", vals.get("MemFree", 0)))
        used = max(0, total - available)
        swap_total = int(vals.get("SwapTotal", 0))
        swap_free = int(vals.get("SwapFree", 0))
        swap_used = max(0, swap_total - swap_free)
        return used, total, swap_used, swap_total
    except Exception:
        return 0, 0, 0, 0


def _read_temp_c() -> float | None:
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text(encoding="utf-8").strip()
        val = float(raw)
        return val / 1000.0 if val > 1000 else val
    except Exception:
        return None


def _read_uptime_seconds() -> int:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return 0


def _read_ipv4_interfaces() -> list[dict]:
    out = []
    try:
        res = subprocess.run(
            ["ip", "-o", "-4", "addr", "show", "up"],
            capture_output=True, text=True, timeout=3,
        )
        if res.returncode != 0:
            return out
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            iface = parts[1]
            if iface == "lo":
                continue
            try:
                inet_idx = parts.index("inet")
                addr = parts[inet_idx + 1].split("/")[0]
            except Exception:
                addr = "-"
            out.append({"name": iface, "ipv4": addr, "up": True})
    except Exception:
        pass
    return out
