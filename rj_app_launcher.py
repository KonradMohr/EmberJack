#!/usr/bin/env python3
"""
RaspyJack Standalone GUI App Launcher Engine (No Full Desktop!)
---------------------------------------------------------------
Discovers installed Linux GUI applications (.desktop files across system,
Flatpak, Snap, user local dirs, plus AppImages and custom entries) and launches
an individual GUI app by itself:

  - On an active Wayland/X11 session: runs the single app window directly under
    the desktop session user (handling root -> user privilege handoff cleanly).
  - On headless Linux TTY / Raspberry Pi HDMI framebuffer: runs ONLY that single
    app in a lightweight single-app kiosk compositor (cage, gamescope, weston
    kiosk-shell, or bare xinit) — NEVER starting a full desktop environment —
    temporarily handing off HDMI/evdev cleanly and restoring RaspyJack on exit.

Zero external Python dependencies (stdlib + PIL for optional app icons).
"""

import fcntl
import json
import os
import pwd
import re
import shlex
import shutil
import socket
import struct
import subprocess
import termios
from typing import Dict, List, Optional, Tuple

import rj_env

# Linux Virtual Terminal ioctl constants (<linux/vt.h>)
_VT_GETSTATE = 0x5603
_VT_ACTIVATE = 0x5606
_VT_WAITACTIVE = 0x5607
_VT_DISALLOCATE = 0x5608

KIOSK_HANDOFF_FLAG = "/dev/shm/rj_kiosk_handoff"
APP_LAUNCHER_LOG = "/dev/shm/rj_app_launcher.log"
DEFAULT_KIOSK_VT = max(2, min(12, int(os.environ.get("RJ_KIOSK_VT", "7") or "7")))
DEFAULT_HOST_VT = max(2, min(12, int(os.environ.get("RJ_HDMI_VT", "8") or "8")))

# Desktop environments, window managers, and panels explicitly excluded
# so the launcher only runs standalone individual GUI applications.
_EXCLUDED_EXEC_BASENAMES = {
    "lxsession", "startlxde", "startlxqt", "lxqt-session", "startxfce4",
    "xfce4-session", "gnome-session", "plasma_session", "startplasma-x11",
    "startplasma-wayland", "mate-session", "cinnamon-session", "budgie-desktop",
    "openbox", "openbox-session", "fluxbox", "icewm-session", "i3", "sway",
    "hyprland", "wayfire", "labwc", "kwin", "kwin_wayland", "kwin_x11",
    "mutter", "xfwm4", "marco", "lxpanel", "xfce4-panel", "waybar", "polybar",
    "lightdm", "gdm", "sddm", "raspyjack",
}

_EXCLUDED_DESKTOP_IDS = {
    "lxde-logout", "xfce4-session-logout", "gnome-session-properties",
    "raspyjack", "hdmi_host",
}

_FIELD_CODE_RE = re.compile(r"%[fFuUdDnNickvm]")

CATEGORY_TABS = ("all", "fav", "web", "media", "games", "tools")
CATEGORY_LABELS = {
    "all": "ALL",
    "fav": "FAV",
    "web": "WEB",
    "media": "MEDIA",
    "games": "GAMES",
    "tools": "TOOLS",
}

APP_SCALE_OPTIONS: Tuple[Tuple[float, str], ...] = (
    (0.50, "50% (Tiny — Fit More)"),
    (0.75, "75% (Compact)"),
    (1.00, "100% (Native / Default)"),
    (1.25, "125% (Medium)"),
    (1.50, "150% (Large)"),
    (1.75, "175% (Extra Large)"),
    (2.00, "200% (2x Huge)"),
    (2.50, "250% (2.5x Max)"),
)

APP_BACKEND_OPTIONS: Tuple[Tuple[str, str], ...] = (
    ("auto", "Auto (Cage for Apps, Labwc/Gamescope for 3D Games)"),
    ("labwc", "Labwc (Wayland — Full Pointer Lock & Multi-Window)"),
    ("cage", "Cage (Wayland Single-App Kiosk)"),
    ("gamescope", "Gamescope (Game Compositor — Pointer Lock)"),
    ("xinit", "X11 / xinit (Legacy X11 Kiosk)"),
    ("weston", "Weston (Wayland Kiosk)"),
)

_APP_BACKEND_BADGES: Dict[str, str] = {
    "auto": "AUTO",
    "labwc": "LABWC",
    "cage": "CAGE",
    "xinit": "X11",
    "gamescope": "GSCOPE",
    "weston": "WESTON",
}

_BACKEND_INSTALL_HINTS: Dict[str, str] = {
    "labwc": "Labwc not installed. Run: sudo apt install -y labwc xwayland seatd",
    "cage": "Cage not installed. Run: sudo apt install -y cage xwayland seatd",
    "xinit": "X11 (xinit) not installed. Run: sudo apt install -y xinit xserver-xorg-core xserver-xorg-input-libinput matchbox-window-manager",
    "gamescope": "Gamescope not installed. Run: sudo apt install -y gamescope",
    "weston": "Weston not installed. Run: sudo apt install -y weston",
}

# Known 3D / FPS apps that require pointer lock (zwp_pointer_constraints_v1 / XGrabPointer),
# which minimal `cage` does not implement; Auto mode prefers labwc / gamescope / xinit when installed.
_POINTER_LOCK_GAME_HINTS = (
    "minecraft", "mcpi", "minetest", "luanti", "chocolate-doom", "crispy-doom",
    "gzdoom", "prboom", "dsda-doom", "quake", "quakespasm", "ioquake3",
    "openarena", "xonotic", "eduke32", "supertuxkart", "retroarch",
)

_CHROMIUM_SCALE_BINS = {
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
    "brave", "brave-browser", "vivaldi", "vivaldi-stable", "microsoft-edge",
    "code", "codium", "vscodium", "discord", "obsidian", "spotify", "electron",
}


def normalize_app_backend(val, default: str = "auto") -> str:
    s = str(val or "").strip().lower()
    return s if s in _APP_BACKEND_BADGES else default


def format_app_backend(backend: str) -> str:
    return _APP_BACKEND_BADGES.get(normalize_app_backend(backend), "AUTO")


def is_pointer_lock_game(app: dict) -> bool:
    """Return True if *app* is a known 3D/FPS game that needs mouse pointer locking."""
    if not isinstance(app, dict):
        return False
    hay = " ".join([
        str(app.get("id", "")),
        str(app.get("name", "")),
        str(app.get("exec", "")),
        " ".join(str(x) for x in (app.get("argv") or [])),
    ]).lower()
    return any(tok in hay for tok in _POINTER_LOCK_GAME_HINTS)


def normalize_app_scale(val, default: float = 1.0) -> float:
    """Clamp & normalize an app window scale factor into [0.50, 2.50]."""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return float(default)
    if not (0.25 <= f <= 4.0):
        return float(default)
    return round(max(0.50, min(2.50, f)), 2)


def format_app_scale(scale: float) -> str:
    """Return human percentage string like '100%', '75%', '150%'."""
    return f"{int(round(normalize_app_scale(scale) * 100))}%"


def step_app_scale(current: float, direction: int) -> float:
    """Step *current* scale up (+1) or down (-1) across APP_SCALE_OPTIONS."""
    vals = [opt[0] for opt in APP_SCALE_OPTIONS]
    cur = normalize_app_scale(current)
    idx = min(range(len(vals)), key=lambda i: abs(vals[i] - cur))
    nxt = max(0, min(len(vals) - 1, idx + (1 if direction > 0 else -1)))
    return vals[nxt]


def get_apps_data_dir() -> str:
    return os.path.join(rj_env.get_base_dir(), "loot", "Apps")


def _state_file() -> str:
    return os.path.join(get_apps_data_dir(), "launcher_state.json")


def _custom_apps_file() -> str:
    return os.path.join(get_apps_data_dir(), "custom_apps.json")


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def _write_json(path: str, data) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def load_launcher_state() -> dict:
    st = _read_json(_state_file(), {})
    if not isinstance(st, dict):
        st = {}
    raw_scales = st.get("app_scales", {})
    clean_scales: Dict[str, float] = {}
    if isinstance(raw_scales, dict):
        for k, v in raw_scales.items():
            if k:
                clean_scales[str(k)] = normalize_app_scale(v)
    raw_backends = st.get("app_backends", {})
    clean_backends: Dict[str, str] = {}
    if isinstance(raw_backends, dict):
        for k, v in raw_backends.items():
            nb = normalize_app_backend(v, default="")
            if k and nb and nb != "auto":
                clean_backends[str(k)] = nb
    return {
        "favorites": list(st.get("favorites", [])) if isinstance(st.get("favorites"), list) else [],
        "recent": list(st.get("recent", [])) if isinstance(st.get("recent"), list) else [],
        "view_mode": st.get("view_mode", "list") if st.get("view_mode") in ("list", "grid") else "list",
        "mouse_speed": max(1, min(10, int(st.get("mouse_speed", 4) or 4))),
        "app_scale": normalize_app_scale(st.get("app_scale", 1.0), default=1.0),
        "app_scales": clean_scales,
        "app_backend": normalize_app_backend(st.get("app_backend", "auto"), default="auto"),
        "app_backends": clean_backends,
    }


def get_effective_app_backend(app_id: str = "", state: Optional[dict] = None) -> str:
    """Return effective kiosk backend for *app_id* (per-app override if set, else global)."""
    st = state if isinstance(state, dict) else load_launcher_state()
    if app_id:
        overrides = st.get("app_backends")
        if isinstance(overrides, dict) and app_id in overrides:
            return normalize_app_backend(overrides[app_id], default="auto")
    return normalize_app_backend(st.get("app_backend", "auto"), default="auto")


def set_app_backend(backend: Optional[str], app_id: str = "") -> str:
    """Persist global kiosk backend (app_id='') or per-app override (None/'auto' clears override)."""
    st = load_launcher_state()
    if app_id:
        overrides = dict(st.get("app_backends") or {})
        norm = normalize_app_backend(backend, default="auto") if backend else "auto"
        if not backend or norm == "auto":
            overrides.pop(app_id, None)
            st["app_backends"] = overrides
            save_launcher_state(st)
            return st["app_backend"]
        overrides[app_id] = norm
        st["app_backends"] = overrides
        save_launcher_state(st)
        return norm
    norm = normalize_app_backend(backend, default="auto")
    st["app_backend"] = norm
    save_launcher_state(st)
    return norm


def get_effective_app_scale(app_id: str = "", state: Optional[dict] = None) -> float:
    """Return effective scale factor for *app_id* (per-app override if set, else global scale)."""
    st = state if isinstance(state, dict) else load_launcher_state()
    if app_id:
        overrides = st.get("app_scales")
        if isinstance(overrides, dict) and app_id in overrides:
            return normalize_app_scale(overrides[app_id])
    return normalize_app_scale(st.get("app_scale", 1.0))


def set_app_scale(scale: Optional[float], app_id: str = "") -> float:
    """Persist global scale (app_id='') or per-app scale override (scale=None clears override)."""
    st = load_launcher_state()
    if app_id:
        overrides = dict(st.get("app_scales") or {})
        if scale is None:
            overrides.pop(app_id, None)
            st["app_scales"] = overrides
            save_launcher_state(st)
            return st["app_scale"]
        val = normalize_app_scale(scale)
        overrides[app_id] = val
        st["app_scales"] = overrides
        save_launcher_state(st)
        return val
    val = normalize_app_scale(1.0 if scale is None else scale)
    st["app_scale"] = val
    save_launcher_state(st)
    return val


def save_launcher_state(state: dict) -> None:
    _write_json(_state_file(), state)


def toggle_favorite(app_id: str) -> bool:
    st = load_launcher_state()
    favs = st["favorites"]
    if app_id in favs:
        favs.remove(app_id)
        save_launcher_state(st)
        return False
    favs.insert(0, app_id)
    save_launcher_state(st)
    return True


def record_recent_launch(app_id: str) -> None:
    st = load_launcher_state()
    rec = [x for x in st["recent"] if x != app_id]
    rec.insert(0, app_id)
    st["recent"] = rec[:24]
    save_launcher_state(st)


def load_custom_apps() -> List[dict]:
    data = _read_json(_custom_apps_file(), [])
    return data if isinstance(data, list) else []


def add_custom_app(name: str, exec_cmd: str, category: str = "tools", comment: str = "Custom GUI App") -> dict:
    apps = load_custom_apps()
    app_id = "custom:" + re.sub(r"[^a-z0-9_-]+", "_", name.lower()).strip("_")
    entry = {
        "id": app_id,
        "name": name.strip(),
        "exec": exec_cmd.strip(),
        "comment": comment.strip(),
        "category": category if category in CATEGORY_TABS else "tools",
        "icon": "",
        "path": "",
        "source": "CUSTOM",
    }
    apps = [a for a in apps if a.get("id") != app_id]
    apps.insert(0, entry)
    _write_json(_custom_apps_file(), apps)
    return entry


def remove_custom_app(app_id: str) -> bool:
    apps = load_custom_apps()
    new_apps = [a for a in apps if a.get("id") != app_id]
    if len(new_apps) == len(apps):
        return False
    _write_json(_custom_apps_file(), new_apps)
    return True


# ---------------------------------------------------------------------------
# Freedesktop .desktop Parsing & Categorization
# ---------------------------------------------------------------------------

def clean_exec_command(exec_str: str) -> List[str]:
    """Strip Freedesktop %u/%F field codes and parse Exec= into argv list."""
    if not exec_str:
        return []
    cleaned = _FIELD_CODE_RE.sub("", exec_str).replace("%%", "%").strip()
    try:
        tokens = shlex.split(cleaned)
    except ValueError:
        tokens = cleaned.split()
    return [t for t in tokens if t]


def map_desktop_category(categories_str: str) -> str:
    cats = {c.strip().lower() for c in (categories_str or "").split(";") if c.strip()}
    if cats & {"webbrowser", "network", "email", "instantmessaging", "chat", "ircclient", "filetransfer"}:
        return "web"
    if cats & {"audiovideo", "audio", "video", "music", "player", "graphics", "photography", "2dgraphics", "3dgraphics", "rastergraphics"}:
        return "media"
    if cats & {"game", "emulator", "arcade", "actiongame", "adventuregame", "boardgame", "strategygame"}:
        return "games"
    return "tools"


def _exec_is_available(argv: List[str], try_exec: str = "") -> bool:
    if try_exec:
        if os.path.isabs(try_exec):
            return os.path.isfile(try_exec) and os.access(try_exec, os.X_OK)
        return shutil.which(try_exec) is not None
    if not argv:
        return False
    # Handle `env VAR=1 cmd ...` prefix
    idx = 0
    if os.path.basename(argv[0]) == "env":
        idx = 1
        while idx < len(argv) and ("=" in argv[idx] or argv[idx].startswith("-")):
            idx += 1
    if idx >= len(argv):
        return False
    cmd0 = argv[idx]
    if os.path.isabs(cmd0):
        return os.path.isfile(cmd0) and os.access(cmd0, os.X_OK)
    return shutil.which(cmd0) is not None


def _extract_primary_binary(argv: List[str]) -> str:
    idx = 0
    if argv and os.path.basename(argv[0]) == "env":
        idx = 1
        while idx < len(argv) and ("=" in argv[idx] or argv[idx].startswith("-")):
            idx += 1
    return os.path.basename(argv[idx]).lower() if idx < len(argv) else ""


def parse_desktop_file(path: str, source: str = "SYS", verify_exec: bool = True) -> Optional[dict]:
    """Parse a Freedesktop .desktop file into a structured GUI app dict (or None if excluded)."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            lines = handle.readlines()
    except OSError:
        return None

    in_entry = False
    fields: Dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            if in_entry:
                break  # Only read main [Desktop Entry] section
            in_entry = (line == "[Desktop Entry]")
            continue
        if not in_entry or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        # Keep unlocalized keys (ignore Name[fr]= etc.)
        if "[" in k:
            continue
        if k not in fields:
            fields[k] = v.strip()

    if fields.get("Type") != "Application":
        return None
    if fields.get("NoDisplay", "").lower() == "true":
        return None
    if fields.get("Hidden", "").lower() == "true":
        return None
    if fields.get("Terminal", "").lower() == "true":
        return None

    name = fields.get("Name", "").strip()
    exec_raw = fields.get("Exec", "").strip()
    if not name or not exec_raw:
        return None

    argv = clean_exec_command(exec_raw)
    if not argv:
        return None

    desktop_id = os.path.splitext(os.path.basename(path))[0]
    primary_bin = _extract_primary_binary(argv)
    if desktop_id.lower() in _EXCLUDED_DESKTOP_IDS or primary_bin in _EXCLUDED_EXEC_BASENAMES:
        return None
    # Filter desktop-only settings/screensavers/autostart shims
    only_show = fields.get("OnlyShowIn", "").lower()
    if only_show and any(x in only_show for x in ("kde", "gnome", "xfce", "lxde", "lxqt", "mate")) and "screensaver" in desktop_id.lower():
        return None

    try_exec = fields.get("TryExec", "").strip()
    if verify_exec and not _exec_is_available(argv, try_exec=try_exec):
        return None

    return {
        "id": desktop_id,
        "name": name,
        "comment": fields.get("Comment") or fields.get("GenericName") or "",
        "exec": exec_raw,
        "argv": argv,
        "icon": fields.get("Icon", ""),
        "category": map_desktop_category(fields.get("Categories", "")),
        "raw_categories": fields.get("Categories", ""),
        "path": fields.get("Path", ""),
        "desktop_file": path,
        "source": source,
    }


def detect_desktop_user() -> Tuple[str, int, int, str]:
    """Detect the real non-root desktop/Pi user (username, uid, gid, home_dir)."""
    if os.geteuid() != 0:
        pw = pwd.getpwuid(os.geteuid())
        return pw.pw_name, pw.pw_uid, pw.pw_gid, pw.pw_dir

    # When running as root (systemd / sudo), prefer SUDO_USER -> /run/user/<uid> -> konrad/pi
    candidates: List[str] = []
    sudo_u = os.environ.get("SUDO_USER", "").strip()
    if sudo_u and sudo_u != "root":
        candidates.append(sudo_u)

    try:
        if os.path.isdir("/run/user"):
            for entry in sorted(os.listdir("/run/user")):
                if entry.isdigit() and int(entry) >= 1000:
                    try:
                        candidates.append(pwd.getpwuid(int(entry)).pw_name)
                    except KeyError:
                        pass
    except OSError:
        pass

    for fallback_u in ("konrad", "pi", "ubuntu", "dietpi"):
        if fallback_u not in candidates:
            candidates.append(fallback_u)

    for uname in candidates:
        try:
            pw = pwd.getpwnam(uname)
            if pw.pw_uid >= 1000 and os.path.isdir(pw.pw_dir):
                return pw.pw_name, pw.pw_uid, pw.pw_gid, pw.pw_dir
        except KeyError:
            continue

    # Last resort: first UID >= 1000 with a real home
    for pw in pwd.getpwall():
        if 1000 <= pw.pw_uid < 60000 and os.path.isdir(pw.pw_dir):
            return pw.pw_name, pw.pw_uid, pw.pw_gid, pw.pw_dir

    pw = pwd.getpwuid(0)
    return pw.pw_name, pw.pw_uid, pw.pw_gid, pw.pw_dir


def resolve_app_icon_path(icon_name: str, user_home: str = "") -> Optional[str]:
    """Resolve a Freedesktop icon name or path to a raster image file (PNG/JPG/BMP/XPM)."""
    if not icon_name:
        return None
    raster_exts = (".png", ".jpg", ".jpeg", ".bmp", ".xpm")
    if os.path.isabs(icon_name):
        if os.path.isfile(icon_name) and icon_name.lower().endswith(raster_exts):
            return icon_name
        return None

    home = user_home or os.path.expanduser("~")
    search_roots = [
        os.path.join(home, ".local/share/icons/hicolor"),
        "/var/lib/flatpak/exports/share/icons/hicolor",
        "/usr/share/icons/hicolor",
        "/usr/share/icons/Papirus",
        "/usr/share/icons/Adwaita",
    ]
    sizes = ("64x64", "48x48", "128x128", "32x32", "96x96", "256x256")
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        for sz in sizes:
            for sub in ("apps", "categories", "places"):
                for ext in (".png", ".jpg"):
                    cand = os.path.join(root, sz, sub, icon_name + ext)
                    if os.path.isfile(cand):
                        return cand
    for pix in ("/usr/share/pixmaps", os.path.join(home, ".icons")):
        for ext in ("", ".png", ".xpm", ".jpg"):
            cand = os.path.join(pix, icon_name + ext)
            if os.path.isfile(cand) and cand.lower().endswith(raster_exts):
                return cand
    return None


def discover_gui_apps(
    extra_desktop_dirs: Optional[List[Tuple[str, str]]] = None,
    verify_exec: bool = True,
) -> List[dict]:
    """Discover all installed standalone GUI applications (.desktop, Flatpak, AppImage, custom)."""
    _uname, _uid, _gid, user_home = detect_desktop_user()

    if extra_desktop_dirs is not None:
        scan_dirs = list(extra_desktop_dirs)
    else:
        scan_dirs = [
            (os.path.join(user_home, ".local/share/applications"), "USER"),
            ("/var/lib/flatpak/exports/share/applications", "FLATPAK"),
            ("/var/lib/snapd/desktop/applications", "SNAP"),
            ("/usr/local/share/applications", "LOCAL"),
            ("/usr/share/applications", "SYS"),
        ]

    seen_ids: Dict[str, dict] = {}
    seen_names: set = set()

    # 1. Custom user-added apps first (highest priority)
    for custom in load_custom_apps():
        argv = clean_exec_command(custom.get("exec", ""))
        if not argv:
            continue
        cid = str(custom.get("id") or custom.get("name"))
        seen_ids[cid] = {
            "id": cid,
            "name": str(custom.get("name", cid)),
            "comment": str(custom.get("comment", "Custom GUI App")),
            "exec": str(custom.get("exec", "")),
            "argv": argv,
            "icon": str(custom.get("icon", "")),
            "category": custom.get("category", "tools"),
            "path": str(custom.get("path", "")),
            "source": "CUSTOM",
        }
        seen_names.add(seen_ids[cid]["name"].lower())

    # 2. Scan AppImages in ~/Applications and loot/Apps/
    appimage_dirs = [
        os.path.join(user_home, "Applications"),
        os.path.join(get_apps_data_dir(), "AppImages"),
        get_apps_data_dir(),
    ]
    for adir in appimage_dirs:
        if not os.path.isdir(adir):
            continue
        try:
            for fn in sorted(os.listdir(adir)):
                if fn.lower().endswith(".appimage"):
                    full = os.path.join(adir, fn)
                    if os.path.isfile(full):
                        stem = re.sub(r"[-_]?v?\d+(\.\d+)*.*", "", os.path.splitext(fn)[0]).replace("_", " ").replace("-", " ").strip() or os.path.splitext(fn)[0]
                        aid = f"appimage:{fn}"
                        if aid not in seen_ids:
                            seen_ids[aid] = {
                                "id": aid,
                                "name": stem,
                                "comment": f"AppImage ({fn})",
                                "exec": shlex.quote(full),
                                "argv": [full],
                                "icon": "",
                                "category": "tools",
                                "path": adir,
                                "source": "APPIMG",
                            }
        except OSError:
            pass

    # 3. Scan Freedesktop .desktop directories
    for dpath, src_tag in scan_dirs:
        if not os.path.isdir(dpath):
            continue
        try:
            entries = sorted(os.listdir(dpath))
        except OSError:
            continue
        for fn in entries:
            if not fn.endswith(".desktop"):
                continue
            full = os.path.join(dpath, fn)
            if not os.path.isfile(full):
                continue
            app = parse_desktop_file(full, source=src_tag, verify_exec=verify_exec)
            if app is None:
                continue
            if app["id"] in seen_ids or app["name"].lower() in seen_names:
                continue
            seen_ids[app["id"]] = app
            seen_names.add(app["name"].lower())

    # Sort: Favorites -> Recently Launched -> Alphabetical
    st = load_launcher_state()
    fav_set = set(st["favorites"])
    rec_order = {aid: i for i, aid in enumerate(st["recent"])}

    apps = list(seen_ids.values())
    for a in apps:
        a["favorite"] = a["id"] in fav_set
        a["recent_rank"] = rec_order.get(a["id"], 9999)

    apps.sort(key=lambda a: (0 if a["favorite"] else 1, a["recent_rank"], a["name"].lower()))
    return apps


def filter_apps(apps: List[dict], category: str = "all", query: str = "") -> List[dict]:
    q = (query or "").strip().lower()
    out = []
    for a in apps:
        if category == "fav" and not a.get("favorite"):
            continue
        if category not in ("all", "fav") and a.get("category") != category:
            continue
        if q:
            hay = f"{a.get('name', '')} {a.get('comment', '')} {a.get('id', '')} {a.get('source', '')}".lower()
            if q not in hay:
                continue
        out.append(a)
    return out


# ---------------------------------------------------------------------------
# Single-App Display Detection & Kiosk Launch Planner (NO Full Desktop!)
# ---------------------------------------------------------------------------

def ensure_runtime_dir(uid: int, gid: int, preferred: str = "") -> str:
    """Guarantee a valid 0700 XDG_RUNTIME_DIR exists for *uid* and return its path.

    Prevents 'XDG_RUNTIME_DIR not set' failures in Wayland compositors (cage,
    weston, gamescope), GTK/Qt/SDL2 apps, and PipeWire when launched under sudo
    or from a boot-time systemd service before a user PAM session exists.
    """
    candidates: List[str] = []
    if preferred:
        candidates.append(preferred)
    env_rdir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if env_rdir and env_rdir not in candidates:
        candidates.append(env_rdir)
    std_rdir = f"/run/user/{uid}"
    if std_rdir not in candidates:
        candidates.append(std_rdir)
    tmp_rdir = f"/tmp/rj-runtime-{uid}"
    if tmp_rdir not in candidates:
        candidates.append(tmp_rdir)

    euid = os.geteuid()
    for path in candidates:
        try:
            if os.path.isdir(path):
                st = os.stat(path)
                if st.st_uid == uid or (euid != 0 and os.access(path, os.W_OK | os.X_OK)):
                    return path
                if euid == 0 and path == tmp_rdir:
                    os.chown(path, uid, gid)
                    os.chmod(path, 0o700)
                    return path
            else:
                # Only create /run/user/<uid> if we are root (and /run/user exists); always allow /tmp fallback
                parent = os.path.dirname(path) or "/"
                if path == std_rdir and (euid != 0 or not os.path.isdir(parent)):
                    continue
                if path != std_rdir and path != tmp_rdir:
                    continue
                os.makedirs(path, mode=0o700, exist_ok=True)
                if euid == 0:
                    os.chown(path, uid, gid)
                os.chmod(path, 0o700)
                return path
        except OSError:
            continue

    return tmp_rdir


def get_active_vt() -> Optional[int]:
    """Return the currently active Linux virtual terminal number (e.g. 1..12) or None."""
    for dev in ("/dev/tty0", "/dev/console"):
        try:
            fd = os.open(dev, os.O_RDONLY)
            try:
                buf = bytearray(6)  # struct vt_stat { u16 v_active, v_signal, v_state; }
                fcntl.ioctl(fd, _VT_GETSTATE, buf)
                vt = struct.unpack("<H", buf[:2])[0]
                return int(vt) if vt > 0 else None
            finally:
                os.close(fd)
        except OSError:
            continue
    try:
        with open("/sys/class/tty/tty0/active", "r", encoding="utf-8") as fh:
            txt = fh.read().strip()
        if txt.startswith("tty") and txt[3:].isdigit():
            return int(txt[3:])
    except OSError:
        pass
    return None


def switch_vt(vt_num: int, wait: bool = True) -> bool:
    """Switch active Linux console VT away from getty@tty1 cleanly."""
    vt = max(1, min(63, int(vt_num)))
    for dev in ("/dev/tty0", "/dev/console"):
        try:
            fd = os.open(dev, os.O_RDWR)
            try:
                fcntl.ioctl(fd, _VT_ACTIVATE, vt)
                if wait:
                    fcntl.ioctl(fd, _VT_WAITACTIVE, vt)
                return True
            finally:
                os.close(fd)
        except OSError:
            continue
    if shutil.which("chvt"):
        try:
            r = subprocess.run(["chvt", str(vt)], capture_output=True, timeout=3)
            return r.returncode == 0
        except Exception:
            pass
    return False


def clear_vt_screen(vt_num: int) -> None:
    """Clear a VT and hide the blinking console cursor so no login prompt bleeds through."""
    try:
        with open("/sys/class/graphics/fbcon/cursor_blink", "w", encoding="utf-8") as fh:
            fh.write("0")
    except OSError:
        pass
    tty_path = f"/dev/tty{int(vt_num)}"
    try:
        fd = os.open(tty_path, os.O_WRONLY | os.O_NOCTTY)
        try:
            os.write(fd, b"\x1b[2J\x1b[H\x1b[?25l")
        finally:
            os.close(fd)
    except OSError:
        pass


def _find_free_x_display() -> str:
    """Return first unused X11 display string (e.g. ':0', ':1', ...) to avoid login greeter clashes."""
    for n in range(10):
        if not os.path.exists(f"/tmp/.X11-unix/X{n}") and not os.path.exists(f"/tmp/.X{n}-lock"):
            return f":{n}"
    return ":0"


def _has_systemd_session_runner() -> bool:
    return os.path.isdir("/run/systemd/system") and bool(shutil.which("systemd-run"))


def _is_unix_socket_live(sock_path: str) -> bool:
    """Return True only if *sock_path* has a listening display server (ignores stale sockets!)."""
    if not sock_path or not os.path.exists(sock_path):
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(0.15)
        s.connect(sock_path)
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def _has_user_x_session(uid: int, user_home: str, x_sock_name: str) -> bool:
    """Return True if an X11 socket is LIVE and belongs to a real user session (not a stale socket or root greeter)."""
    sock_path = os.path.join("/tmp/.X11-unix", x_sock_name)
    if not _is_unix_socket_live(sock_path):
        return False
    try:
        if os.stat(sock_path).st_uid == uid:
            return True
    except OSError:
        return False
    return os.path.isfile(os.path.join(user_home, ".Xauthority"))


def detect_display_session() -> dict:
    """Detect existing single-window display sockets OR available headless kiosk wrappers."""
    uname, uid, gid, user_home = detect_desktop_user()
    rdir = f"/run/user/{uid}"

    wayland_display = os.environ.get("WAYLAND_DISPLAY", "").strip()
    if wayland_display:
        wl_cand = wayland_display if os.path.isabs(wayland_display) else os.path.join(os.environ.get("XDG_RUNTIME_DIR", rdir), wayland_display)
        if not _is_unix_socket_live(wl_cand):
            wayland_display = ""
    if not wayland_display and os.path.isdir(rdir):
        for sock_name in ("wayland-0", "wayland-1"):
            if _is_unix_socket_live(os.path.join(rdir, sock_name)):
                wayland_display = sock_name
                break

    x_display = os.environ.get("DISPLAY", "").strip()
    if x_display and x_display.startswith(":"):
        xnum = x_display[1:].split(".", 1)[0]
        if xnum.isdigit() and not _is_unix_socket_live(f"/tmp/.X11-unix/X{xnum}"):
            x_display = ""
    if not x_display and os.path.isdir("/tmp/.X11-unix"):
        try:
            xsocks = sorted(f for f in os.listdir("/tmp/.X11-unix") if f.startswith("X"))
            for xsock in xsocks:
                if _has_user_x_session(uid, user_home, xsock):
                    x_display = ":" + xsock[1:]
                    break
        except OSError:
            pass

    # Check single-app headless kiosk wrappers (NEVER full desktop sessions!)
    available_kiosks = [tool for tool in ("cage", "labwc", "gamescope", "weston", "xinit") if shutil.which(tool)]
    kiosk_tool = available_kiosks[0] if available_kiosks else ""

    has_live_session = bool(wayland_display or x_display)
    if has_live_session:
        mode = "direct_wayland" if wayland_display else "direct_x11"
        mode_label = "WAYLAND" if wayland_display else "X11"
    elif kiosk_tool:
        mode = f"kiosk_{kiosk_tool}"
        mode_label = f"{kiosk_tool.upper()} KIOSK"
    else:
        mode = "missing_kiosk"
        mode_label = "NEEDS CAGE/XINIT"

    return {
        "mode": mode,
        "mode_label": mode_label,
        "has_live_session": has_live_session,
        "wayland_display": wayland_display,
        "x_display": x_display,
        "kiosk_tool": kiosk_tool,
        "available_kiosks": available_kiosks,
        "kiosk_vt": DEFAULT_KIOSK_VT,
        "has_seatd": os.path.exists("/run/seatd.sock"),
        "has_xwayland": shutil.which("Xwayland") is not None,
        "user": uname,
        "uid": uid,
        "gid": gid,
        "home": user_home,
        "runtime_dir": rdir if os.path.isdir(rdir) else "",
    }


def _resolve_command_path(argv: List[str]) -> List[str]:
    """Resolve binary path so systemd-run never fails with 203/EXEC on non-standard PATH dirs."""
    if not argv:
        return argv
    resolved = shutil.which(argv[0])
    if resolved:
        return [resolved] + argv[1:]
    return argv


def _prepare_wayland_app_argv(app_argv: List[str]) -> List[str]:
    """Add native Wayland hint flags for Chromium/Electron apps inside Wayland kiosks."""
    if not app_argv:
        return app_argv
    primary = _extract_primary_binary(app_argv)
    if primary in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "brave", "brave-browser"):
        if not any(a.startswith("--ozone-platform") for a in app_argv):
            return [app_argv[0], "--ozone-platform-hint=auto"] + app_argv[1:]
    return app_argv


def apply_scale_to_launch(app_argv: List[str], env: dict, scale: float) -> List[str]:
    """Apply cross-toolkit render scaling (GTK, Qt, Firefox, Chromium, Electron, Xft, Cursor)."""
    sc = normalize_app_scale(scale)
    if abs(sc - 1.0) < 0.01:
        return list(app_argv)

    sc_str = f"{sc:g}"
    gdk_int = max(1, int(sc))
    gdk_dpi = round(sc / float(gdk_int), 3)
    dpi = max(48, min(300, int(round(96.0 * sc))))
    cursor_sz = max(12, min(64, int(round(24.0 * sc))))

    env["RJ_APP_SCALE"] = sc_str
    # GTK3 / GTK4 / Firefox / LibreOffice
    env["GDK_SCALE"] = str(gdk_int)
    env["GDK_DPI_SCALE"] = f"{gdk_dpi:g}"
    # Qt5 / Qt6 (supports exact fractional scaling down to 0.5x and up to 2.5x)
    env["QT_ENABLE_HIGHDPI_SCALING"] = "1"
    env["QT_AUTO_SCREEN_SCALE_FACTOR"] = "0"
    env["QT_SCALE_FACTOR"] = sc_str
    env["QT_SCALE_FACTOR_ROUNDING_POLICY"] = "PassThrough"
    # Electron / EFL / Cursor / Xft DPI
    env["ELECTRON_FORCE_DEVICE_SCALE_FACTOR"] = sc_str
    env["ELM_SCALE"] = sc_str
    env["XCURSOR_SIZE"] = str(cursor_sz)
    env["XFT_DPI"] = str(dpi)

    out_argv = list(app_argv)
    primary = _extract_primary_binary(out_argv)
    if primary in _CHROMIUM_SCALE_BINS and not any(a.startswith("--force-device-scale-factor") for a in out_argv):
        out_argv = [out_argv[0], f"--force-device-scale-factor={sc_str}"] + out_argv[1:]
    return out_argv


def build_launch_plan(
    app: dict,
    extra_args: Optional[List[str]] = None,
    session_info: Optional[dict] = None,
    as_root: bool = False,
    kiosk_vt: Optional[int] = None,
    scale: Optional[float] = None,
    backend: Optional[str] = None,
) -> Tuple[Optional[List[str]], dict, bool, str]:
    """Build the command argv & env to launch ONE GUI app without starting a desktop.

    Returns (cmd_argv_or_None, env_dict, needs_hdmi_handoff, description_or_error).
    """
    info = session_info or detect_display_session()
    app_argv = list(app.get("argv") or clean_exec_command(app.get("exec", "")))
    if extra_args:
        app_argv.extend(extra_args)
    if not app_argv:
        return None, {}, False, "Invalid Exec command"

    # Make sure AppImages are marked executable
    if app.get("source") == "APPIMG" and os.path.isfile(app_argv[0]) and not os.access(app_argv[0], os.X_OK):
        try:
            os.chmod(app_argv[0], 0o755)
        except OSError:
            pass

    target_uid = 0 if as_root else int(info.get("uid", os.geteuid()))
    target_gid = 0 if as_root else int(info.get("gid", os.getegid()))
    vt_num = int(kiosk_vt if kiosk_vt is not None else info.get("kiosk_vt", DEFAULT_KIOSK_VT))
    runtime_dir = ensure_runtime_dir(
        target_uid,
        target_gid,
        preferred="" if as_root else str(info.get("runtime_dir") or ""),
    )

    env = os.environ.copy()
    env["HOME"] = "/root" if as_root else info["home"]
    env["USER"] = "root" if as_root else info["user"]
    env["LOGNAME"] = "root" if as_root else info["user"]
    env["XDG_RUNTIME_DIR"] = runtime_dir
    env["XDG_SEAT"] = "seat0"
    # Always target the dedicated kiosk VT (VT7 by default), NEVER VT1 where getty@tty1 runs!
    env["XDG_VTNR"] = str(vt_num)

    eff_scale = normalize_app_scale(
        scale if scale is not None else get_effective_app_scale(str(app.get("id", "")))
    )
    app_argv = apply_scale_to_launch(app_argv, env, eff_scale)
    scale_suffix = f" @ {format_app_scale(eff_scale)}" if abs(eff_scale - 1.0) >= 0.01 else ""

    # Preserve PulseAudio/PipeWire & DBus session sockets from the desktop user's runtime dir
    user_rdir = str(info.get("runtime_dir") or f"/run/user/{info.get('uid', 1000)}")
    for r_cand in (runtime_dir, user_rdir):
        if not r_cand or not os.path.isdir(r_cand):
            continue
        pulse_sock = os.path.join(r_cand, "pulse", "native")
        if os.path.exists(pulse_sock):
            env.setdefault("PULSE_SERVER", f"unix:{pulse_sock}")
        dbus_sock = os.path.join(r_cand, "bus")
        if os.path.exists(dbus_sock):
            env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={dbus_sock}")

    mode = info["mode"]

    if mode in ("direct_wayland", "direct_x11"):
        if info["wayland_display"]:
            env["WAYLAND_DISPLAY"] = info["wayland_display"]
        if info["x_display"]:
            env["DISPLAY"] = info["x_display"]
            xauth = os.path.join(info["home"], ".Xauthority")
            if os.path.isfile(xauth):
                env.setdefault("XAUTHORITY", xauth)
        return app_argv, env, False, f"Direct single-app launch ({info['mode_label']}{scale_suffix})"

    # Resolve headless kiosk backend (supports explicit global/per-app choice + automatic
    # gamescope/xinit preference for 3D pointer-lock games like MCPI-Reborn!)
    avail_kiosks = list(info.get("available_kiosks") or ([info["kiosk_tool"]] if info.get("kiosk_tool") else []))
    req_backend = normalize_app_backend(
        backend if backend is not None else get_effective_app_backend(str(app.get("id", "")))
    )
    if req_backend != "auto":
        if req_backend in avail_kiosks or (
            "available_kiosks" not in info and info.get("kiosk_tool") == req_backend
        ):
            mode = f"kiosk_{req_backend}"
        else:
            return None, env, False, _BACKEND_INSTALL_HINTS.get(req_backend, f"Kiosk backend '{req_backend}' not installed.")
    elif mode == "kiosk_cage" and is_pointer_lock_game(app):
        # cage does not implement Wayland pointer constraints; prefer labwc (native Pi Wayland WM
        # with full zwp_pointer_constraints_v1 + multi-window support), else gamescope, else xinit!
        if "labwc" in avail_kiosks:
            mode = "kiosk_labwc"
        elif "gamescope" in avail_kiosks:
            mode = "kiosk_gamescope"
        elif "xinit" in avail_kiosks:
            mode = "kiosk_xinit"

    # Headless TTY kiosk modes MUST NOT inherit stale DISPLAY / WAYLAND_DISPLAY
    # (otherwise wlroots/cage/labwc tries to nest instead of claiming the DRM/KMS display!).
    for stale_key in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY"):
        env.pop(stale_key, None)

    env["XDG_SESSION_CLASS"] = "user"

    # Headless TTY single-app kiosk mode on dedicated VT (isolated from getty@tty1 login prompt!)
    if mode in ("kiosk_cage", "kiosk_labwc", "kiosk_gamescope", "kiosk_weston"):
        env["XDG_SESSION_TYPE"] = "wayland"
        env.setdefault("MOZ_ENABLE_WAYLAND", "1")
        env.setdefault("GDK_BACKEND", "wayland,x11")
        env.setdefault("QT_QPA_PLATFORM", "wayland;xcb")
        env.setdefault("SDL_VIDEODRIVER", "wayland,x11")
        env.setdefault("ELECTRON_OZONE_PLATFORM_HINT", "auto")
        env.setdefault("NIXOS_OZONE_WL", "1")
        wl_app = _prepare_wayland_app_argv(app_argv)
        if mode == "kiosk_labwc":
            # labwc -S <cmd> runs ONE session app (supporting multi-window dialogs, Alt+F4,
            # XWayland, and full 3D mouse pointer locking) and exits cleanly when the app exits!
            env.setdefault("WLR_LIBINPUT_NO_DEVICES", "1")
            session_cmd = " ".join(shlex.quote(arg) for arg in wl_app)
            return ["labwc", "-S", session_cmd], env, True, f"Single-app Wayland WM (labwc/vt{vt_num}{scale_suffix})"
        if mode == "kiosk_cage":
            # Cage runs ONE Wayland/XWayland app fullscreen on VT7 and exits when the app closes
            env.setdefault("WLR_LIBINPUT_NO_DEVICES", "1")
            return ["cage", "-s", "--"] + wl_app, env, True, f"Single-app Wayland kiosk (cage/vt{vt_num}{scale_suffix})"
        if mode == "kiosk_gamescope":
            gs_cmd = ["gamescope", "-f"]
            if abs(eff_scale - 1.0) >= 0.01:
                virt_w = max(320, int(round(1920.0 / eff_scale)))
                virt_h = max(240, int(round(1080.0 / eff_scale)))
                gs_cmd += ["-w", str(virt_w), "-h", str(virt_h)]
            return gs_cmd + ["--"] + wl_app, env, True, f"Single-app kiosk (gamescope/vt{vt_num}{scale_suffix})"
        weston_cmd = ["weston", "--shell=kiosk-shell.so"]
        if eff_scale >= 1.99:
            weston_cmd.append(f"--scale={int(round(eff_scale))}")
        return weston_cmd + ["--"] + wl_app, env, True, f"Single-app kiosk (weston/vt{vt_num}{scale_suffix})"

    if mode == "kiosk_xinit":
        # Single-app X11 session on dedicated VT7; wrap with a tiny kiosk WM (matchbox/openbox)
        # when installed so windows maximize, keep focus across dialogs, and support Alt+F4.
        env["XDG_SESSION_TYPE"] = "x11"
        env.setdefault("GDK_BACKEND", "x11")
        env.setdefault("QT_QPA_PLATFORM", "xcb")
        env.setdefault("SDL_VIDEODRIVER", "x11")
        abs_cmd = shutil.which(app_argv[0]) or (os.path.abspath(app_argv[0]) if "/" in app_argv[0] else app_argv[0])
        xdisp = _find_free_x_display()
        x_srv_args = [xdisp, f"vt{vt_num}", "-keeptty"]
        if abs(eff_scale - 1.0) >= 0.01:
            x_srv_args += ["-dpi", str(max(48, min(300, int(round(96.0 * eff_scale)))))]
        wm_cmd = ""
        if info.get("x11_wm") or shutil.which("matchbox-window-manager"):
            wm_bin = str(info.get("x11_wm") or shutil.which("matchbox-window-manager"))
            if os.path.basename(wm_bin) == "matchbox-window-manager":
                wm_cmd = f"{shlex.quote(wm_bin)} -use_titlebar no -use_cursor yes"
            else:
                wm_cmd = shlex.quote(wm_bin)
        elif shutil.which("openbox"):
            wm_cmd = shlex.quote(shutil.which("openbox") or "openbox")
        if wm_cmd:
            quoted_app = " ".join(shlex.quote(a) for a in ([abs_cmd] + app_argv[1:]))
            x_client_sh = f"{wm_cmd} & _wm=$!; sleep 0.2; {quoted_app}; _rc=$?; kill $_wm 2>/dev/null || true; exit $_rc"
            return ["/usr/bin/xinit" if os.path.isfile("/usr/bin/xinit") else "xinit", "/bin/sh", "-c", x_client_sh, "--"] + x_srv_args, env, True, f"Single-app X11 kiosk (xinit+WM/vt{vt_num}{scale_suffix})"
        return ["xinit", abs_cmd] + app_argv[1:] + ["--"] + x_srv_args, env, True, f"Bare single-app X11 (xinit/vt{vt_num}{scale_suffix})"

    return (
        None,
        env,
        False,
        "No kiosk compositor installed. Run: sudo apt install -y cage labwc xwayland seatd",
    )


def build_systemd_kiosk_command(
    cmd: List[str],
    env: dict,
    user: str,
    gid: int,
    cwd: str,
    vt_num: int,
    err_log_path: str = "",
) -> List[str]:
    """Wrap *cmd* in a clean systemd-run PAM/logind session on /dev/tty<vt_num>.

    This gives the non-root user a genuine seat0/VT7 session (DRM master + libinput
    keyboard/mouse + XDG_RUNTIME_DIR + PipeWire) WITHOUT touching getty@tty1 or
    prompting for any login password.
    """
    vt = int(vt_num)
    sr_cmd = [
        "systemd-run",
        "--wait",
        "--collect",
        "--quiet",
        f"--uid={user}",
        f"--gid={int(gid)}",
        f"--working-directory={cwd}",
        "-p", "PAMName=login",
        "-p", f"UtmpIdentifier=tty{vt}",
        "-p", "UtmpMode=user",
        "-p", f"TTYPath=/dev/tty{vt}",
        "-p", "StandardInput=tty-force",
        "-p", "StandardOutput=tty",
        "-p", "TTYReset=yes",
        "-p", "TTYVHangup=yes",
    ]
    if err_log_path:
        sr_cmd += ["-p", f"StandardError=file:{err_log_path}"]
    for key in (
        "XDG_VTNR",
        "XDG_SEAT",
        "XDG_SESSION_TYPE",
        "XDG_SESSION_CLASS",
        "XDG_RUNTIME_DIR",
        "WLR_LIBINPUT_NO_DEVICES",
        "MOZ_ENABLE_WAYLAND",
        "GDK_BACKEND",
        "QT_QPA_PLATFORM",
        "SDL_VIDEODRIVER",
        "ELECTRON_OZONE_PLATFORM_HINT",
        "ELECTRON_FORCE_DEVICE_SCALE_FACTOR",
        "NIXOS_OZONE_WL",
        "RJ_APP_SCALE",
        "GDK_SCALE",
        "GDK_DPI_SCALE",
        "QT_ENABLE_HIGHDPI_SCALING",
        "QT_AUTO_SCREEN_SCALE_FACTOR",
        "QT_SCALE_FACTOR",
        "QT_SCALE_FACTOR_ROUNDING_POLICY",
        "ELM_SCALE",
        "XCURSOR_SIZE",
        "XFT_DPI",
        "PULSE_SERVER",
        "DBUS_SESSION_BUS_ADDRESS",
        "HOME",
        "USER",
        "LOGNAME",
    ):
        val = env.get(key)
        if val:
            sr_cmd += ["-E", f"{key}={val}"]
    sr_cmd.append("--")
    sr_cmd.extend(_resolve_command_path(cmd))
    return sr_cmd


def _prepare_kiosk_err_log(uid: int, gid: int) -> str:
    """Return a safe non-sticky error log path immune to Linux fs.protected_regular=2."""
    # NOTE: NEVER chown a file in sticky /tmp (01777) to non-root when systemd (root)
    # opens StandardError=file: — Linux kernel fs.protected_regular=2 rejects it with EACCES!
    # Using a dedicated non-sticky directory owned by the user lets both root and uid open it.
    base_dir = "/run/raspyjack-kiosk"
    user_dir = os.path.join(base_dir, f"u{int(uid)}")
    try:
        os.makedirs(base_dir, mode=0o755, exist_ok=True)
        os.makedirs(user_dir, mode=0o755, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(user_dir, int(uid), int(gid))
            os.chmod(user_dir, 0o755)
        log_path = os.path.join(user_dir, f"kiosk_{os.getpid()}.err")
        try:
            if os.path.exists(log_path):
                os.unlink(log_path)
        except OSError:
            pass
        return log_path
    except OSError:
        return ""


def write_launcher_debug_log(entry: str) -> None:
    """Persist diagnostic output of the most recent app launch to /dev/shm/rj_app_launcher.log."""
    try:
        with open(APP_LAUNCHER_LOG, "a", encoding="utf-8") as fh:
            fh.write(entry + "\n")
        os.chmod(APP_LAUNCHER_LOG, 0o644)
    except OSError:
        pass


def reset_launcher_debug_log(header: str) -> None:
    try:
        with open(APP_LAUNCHER_LOG, "w", encoding="utf-8") as fh:
            fh.write(header + "\n")
        os.chmod(APP_LAUNCHER_LOG, 0o644)
    except OSError:
        pass


def spawn_standalone_gui_app(
    app: dict,
    extra_args: Optional[List[str]] = None,
    session_info: Optional[dict] = None,
    as_root: bool = False,
    kiosk_vt: Optional[int] = None,
    use_systemd_session: bool = True,
    scale: Optional[float] = None,
    backend: Optional[str] = None,
) -> Tuple[Optional[subprocess.Popen], bool, str]:
    """Spawn the selected standalone GUI app isolated from getty@tty1."""
    info = session_info or detect_display_session()
    vt_num = int(kiosk_vt if kiosk_vt is not None else info.get("kiosk_vt", DEFAULT_KIOSK_VT))
    cmd, env, needs_handoff, desc = build_launch_plan(
        app, extra_args=extra_args, session_info=info, as_root=as_root,
        kiosk_vt=vt_num, scale=scale, backend=backend,
    )
    if not cmd:
        return None, False, desc

    cwd = app.get("path") if (app.get("path") and os.path.isdir(app["path"])) else info["home"]
    if not os.path.isdir(cwd):
        cwd = "/"

    # 1. Preferred headless kiosk launch on systemd + root: genuine non-root logind session on VT7!
    #    (When seatd is NOT running, PAMName=login gives libseat a real seat0/VT7 session.
    #     When seatd IS running, systemd-run still sets up clean VT7 ownership + user env.)
    if (
        needs_handoff
        and use_systemd_session
        and not as_root
        and os.geteuid() == 0
        and int(info.get("uid", 0)) >= 1000
        and _has_systemd_session_runner()
    ):
        err_log = _prepare_kiosk_err_log(int(info["uid"]), int(info["gid"]))
        sr_cmd = build_systemd_kiosk_command(
            cmd,
            env=env,
            user=str(info["user"]),
            gid=int(info["gid"]),
            cwd=cwd,
            vt_num=vt_num,
            err_log_path=err_log,
        )
        write_launcher_debug_log(f"[attempt:systemd-run] cmd={' '.join(shlex.quote(x) for x in sr_cmd)}")
        try:
            proc = subprocess.Popen(
                sr_cmd,
                cwd="/",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
            )
            setattr(proc, "_rj_err_log", err_log)
            record_recent_launch(app["id"])
            return proc, needs_handoff, desc
        except Exception as exc:
            write_launcher_debug_log(f"[attempt:systemd-run] spawn exception: {exc}")
            # Fall through cleanly to direct VT7 spawn below

    # 2. Direct spawn (desktop Wayland/X11 OR direct controlling-TTY VT7 kiosk — works great with seatd!)
    drop_privs = (not as_root) and (os.geteuid() == 0 and info["uid"] >= 1000)
    tty_path = f"/dev/tty{vt_num}" if needs_handoff else ""
    attach_tty = bool(tty_path and os.path.exists(tty_path))
    if attach_tty and os.geteuid() == 0:
        try:
            os.chown(tty_path, int(info["uid"]) if drop_privs else 0, int(info["gid"]) if drop_privs else 0)
            os.chmod(tty_path, 0o620)
        except OSError:
            pass

    tag = "direct-root" if as_root else "direct-user"
    write_launcher_debug_log(f"[attempt:{tag}] tty={tty_path or 'none'} cmd={' '.join(shlex.quote(x) for x in cmd)}")

    def _preexec() -> None:
        os.setsid()
        if attach_tty:
            try:
                tty_fd = os.open(tty_path, os.O_RDWR)
                try:
                    fcntl.ioctl(tty_fd, termios.TIOCSCTTY, 1)
                except OSError:
                    try:
                        fcntl.ioctl(tty_fd, termios.TIOCSCTTY, 0)
                    except OSError:
                        pass
                os.dup2(tty_fd, 0)
                os.dup2(tty_fd, 1)
                if tty_fd > 2:
                    os.close(tty_fd)
            except OSError:
                pass
        if drop_privs:
            try:
                os.initgroups(info["user"], info["gid"])
                os.setgid(info["gid"])
                os.setuid(info["uid"])
            except OSError:
                pass

    try:
        proc = subprocess.Popen(
            _resolve_command_path(cmd),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            preexec_fn=_preexec,
            close_fds=True,
        )
        record_recent_launch(app["id"])
        return proc, needs_handoff, desc
    except Exception as exc:
        write_launcher_debug_log(f"[attempt:{tag}] spawn exception: {exc}")
        return None, False, str(exc)[:64]
