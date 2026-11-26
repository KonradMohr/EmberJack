#!/usr/bin/env bash
# RaspyJack installation / bootstrap script
# ------------------------------------------------------------
# * Idempotent   – safe to run multiple times
# * Bookworm‑ready – handles /boot/firmware/config.txt move
# * Enables I²C/SPI, installs all deps, sets up systemd units
# * Ends with a health‑check (SPI nodes + Python imports)
# * NEW: WiFi attack support with aircrack-ng and USB dongle tools
# ------------------------------------------------------------
set -euo pipefail

# ───── helpers ───────────────────────────────────────────────
step()  { printf "\e[1;34m[STEP]\e[0m %s\n"  "$*"; }
info()  { printf "\e[1;32m[INFO]\e[0m %s\n"  "$*"; }
warn()  { printf "\e[1;33m[WARN]\e[0m %s\n"  "$*"; }
fail()  { printf "\e[1;31m[FAIL]\e[0m %s\n"  "$*"; exit 1; }
cmd()   { command -v "$1" >/dev/null 2>&1; }

# ───── mode flag ─────────────────────────────────────────────
UPDATE_MODE=0
AUTO_YES=0
FORCE_HDMI=0
NO_SERVICE=0
for _arg in "$@"; do
  case "$_arg" in
    --update)     UPDATE_MODE=1 ;;
    -y|--yes)     AUTO_YES=1 ;;
    --hdmi)       FORCE_HDMI=1 ;;
    --no-service) NO_SERVICE=1 ;;
  esac
done

# Locate actual RaspyJack install directory (supports /root/Raspyjack OR any clone path)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${RJ_INSTALL_DIR:-$SCRIPT_DIR}"
info "Install directory: $INSTALL_DIR"

# Detect Linux package manager
PKG_MGR="unknown"
if cmd apt-get; then
  PKG_MGR="apt"
elif cmd pacman; then
  PKG_MGR="pacman"
elif cmd dnf; then
  PKG_MGR="dnf"
fi
info "Detected package manager: $PKG_MGR"

# Detect desktop GUI session (Wayland / X11)
IS_DESKTOP=0
if [[ -n "${WAYLAND_DISPLAY:-}" || -n "${DISPLAY:-}" ]]; then
  IS_DESKTOP=1
fi

# Helper: run command with sudo only when not already root
as_root() {
  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

# ───── auto-detect CardputerZero hardware ────────────────────
IS_CARDPUTER=0
CFG_TMP=/boot/firmware/config.txt; [[ -f $CFG_TMP ]] || CFG_TMP=/boot/config.txt
if [[ -f "$CFG_TMP" ]] && grep -q "cardputerzero-overlay" "$CFG_TMP" 2>/dev/null; then
  IS_CARDPUTER=1
elif [[ -f /sys/class/graphics/fb0/name ]] && grep -qE "st7789v_m5st|panel-mipi-dbi" /sys/class/graphics/fb0/name 2>/dev/null; then
  IS_CARDPUTER=1
fi

# ───── 0 ▸ convert CRLF if file came from Windows ────────────
if grep -q $'\r' "$0" 2>/dev/null; then
  step "Converting CRLF → LF in $0"
  if cmd dos2unix; then
    dos2unix "$0" || true
  else
    sed -i 's/\r$//' "$0" || true
  fi
fi

# ───── 1 ▸ locate active config.txt ──────
CFG=/boot/firmware/config.txt; [[ -f $CFG ]] || CFG=/boot/config.txt
if [[ -f "$CFG" ]]; then
  info "Using config file: $CFG"
else
  info "No Raspberry Pi boot config.txt found (non-Pi / PC environment)"
fi
add_dtparam() {
  local param="$1"
  [[ -f "$CFG" ]] || { warn "Skipping $param ($CFG not found)"; return 0; }
  if grep -qE "^#?\s*${param%=*}=on" "$CFG"; then
    as_root sed -Ei "s|^#?\s*${param%=*}=.*|${param%=*}=on|" "$CFG"
  else
    echo "$param" | as_root tee -a "$CFG" >/dev/null
  fi
}

# ───── 1‑b ▸ select display type ─────────────────────────────
GUI_CONF="$INSTALL_DIR/gui_conf.json"
if [[ $FORCE_HDMI -eq 1 ]]; then
  DISPLAY_TYPE="HDMI"
  info "CLI flag --hdmi: selected display HDMI"
elif [[ $UPDATE_MODE -eq 1 ]]; then
  # Auto-detect from existing gui_conf.json
  DISPLAY_TYPE=$(python3 - "$GUI_CONF" <<'PY' 2>/dev/null || echo "ST7735_128"
import json, sys
try:
    with open(sys.argv[1]) as f:
        print(json.load(f).get("DISPLAY", {}).get("type", "ST7735_128"))
except Exception:
    print("ST7735_128")
PY
)
  info "Update mode: detected display $DISPLAY_TYPE from $GUI_CONF"
else
  step "Display configuration"
  echo ""
  echo "  Which display are you using?"
  echo ""
  echo "    1) ST7735_128    — 1.44\" 128×128  (original Waveshare HAT)"
  echo "    2) ST7789_240    — 1.3\"  240×240"
  echo "    3) CARDPUTER_320 — M5Stack CardputerZero 320×170"
  echo "    4) HDMI          — Standard HDMI Monitor + Normal Keyboard (PC / Pi)"
  echo ""
  DEFAULT_CHOICE=1
  if [[ $IS_CARDPUTER -eq 1 ]]; then
    DEFAULT_CHOICE=3
    info "CardputerZero hardware auto-detected!"
  elif [[ ! -e /dev/spidev0.0 ]]; then
    DEFAULT_CHOICE=4
    info "No SPI LCD detected — defaulting to HDMI + normal keyboard mode!"
  fi
  if [[ $AUTO_YES -eq 1 || ! -t 0 ]]; then
    DISPLAY_CHOICE="$DEFAULT_CHOICE"
  else
    read -rp "  Enter choice [1/2/3/4] (default: $DEFAULT_CHOICE): " DISPLAY_CHOICE
    DISPLAY_CHOICE="${DISPLAY_CHOICE:-$DEFAULT_CHOICE}"
  fi
  case "$DISPLAY_CHOICE" in
    2) DISPLAY_TYPE="ST7789_240" ;;
    3) DISPLAY_TYPE="CARDPUTER_320" ;;
    4) DISPLAY_TYPE="HDMI" ;;
    *) DISPLAY_TYPE="ST7735_128" ;;
  esac
fi
info "Selected display: $DISPLAY_TYPE"

# Write DISPLAY type into gui_conf.json (preserve ALL existing settings: flip, colors, pins, etc.)
_write_conf_runner=()
if [[ ! -w "$INSTALL_DIR" ]]; then
  _write_conf_runner=(as_root)
fi
mkdir -p "$INSTALL_DIR" 2>/dev/null || as_root mkdir -p "$INSTALL_DIR"
if [[ -f "$GUI_CONF" ]]; then
  "${_write_conf_runner[@]}" python3 - "$GUI_CONF" "$DISPLAY_TYPE" <<'PY'
import json, sys
conf_path, dtype = sys.argv[1], sys.argv[2]
with open(conf_path, "r", encoding="utf-8") as f:
    data = json.load(f)
if "DISPLAY" not in data:
    data["DISPLAY"] = {}
data["DISPLAY"]["type"] = dtype
data["DISPLAY"]["supported_types"] = ["ST7735_128", "ST7789_240", "CARDPUTER_320", "HDMI"]
if dtype == "HDMI":
    hdmi = data["DISPLAY"].setdefault("hdmi", {
        "width": 640, "height": 360, "backend": "auto",
        "fullscreen": False, "scale_mode": "fit", "fbdev_filter": "fast", "fps": 30, "show_hud": True,
    })
    hdmi.setdefault("fbdev_filter", "fast")
with open(conf_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=4)
flip_status = data["DISPLAY"].get("flip", False)
print(f"[OK] {conf_path}: type={dtype}, flip={flip_status}")
PY
else
  info "gui_conf.json not found — creating with defaults."
  "${_write_conf_runner[@]}" python3 - "$GUI_CONF" "$DISPLAY_TYPE" "$INSTALL_DIR" <<'NEWCONF'
import json, os, sys
conf_path, dtype, base_dir = sys.argv[1], sys.argv[2], sys.argv[3]
data = {
    "COLORS": {
        "BACKGROUND": "#0E0E10",
        "BORDER": "#B85E24",
        "GAMEPAD": "#E07A2F",
        "GAMEPAD_FILL": "#F2994A",
        "SELECTED_TEXT": "#F2994A",
        "SELECTED_TEXT_BACKGROUND": "#2D1D12",
        "TEXT": "#E6DFD7"
    },
    "DISPLAY": {
        "type": dtype,
        "supported_types": ["ST7735_128", "ST7789_240", "CARDPUTER_320", "HDMI"],
        "flip": False,
        "hdmi": {
            "width": 640, "height": 360, "backend": "auto",
            "fullscreen": False, "scale_mode": "fit", "fbdev_filter": "fast", "fps": 30, "show_hud": True
        }
    },
    "LOCK": {
        "auto_lock_seconds": 0,
        "enabled": False,
        "pin_hash": ""
    },
    "PATHS": {
        "IMAGEBROWSER_START": os.path.join(base_dir, "img") + "/",
        "SCREENSAVER_GIF": os.path.join(base_dir, "img", "screensaver", "default.gif")
    },
    "PINS": {
        "KEY1_PIN": 21, "KEY2_PIN": 20, "KEY3_PIN": 16,
        "KEY_DOWN_PIN": 19, "KEY_LEFT_PIN": 5,
        "KEY_PRESS_PIN": 13, "KEY_RIGHT_PIN": 26, "KEY_UP_PIN": 6
    }
}
with open(conf_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=4)
print(f"[OK] {conf_path} created: type={dtype}")
NEWCONF
fi

# ───── 1‑c ▸ create payload config & loot directories ────────
step "Creating payload config & data directories …"
for d in ad_recon auto_loot_exfil bt_audio cctv_scanner cctv_viewer dns_tunnel \
         exfil_ftp exfil_smb http_exfil reverse_ssh rtsp_viewer scheduler \
         ssid_pool timer tripwire usb_gadget wifi_alert; do
  "${_write_conf_runner[@]}" mkdir -p "$INSTALL_DIR/config/$d"
done
for ld in wordlists screenshots Apps YouTube/Downloads YouTube/thumbs; do
  "${_write_conf_runner[@]}" mkdir -p "$INSTALL_DIR/loot/$ld"
done

# ───── 1‑d ▸ CardputerZero: replace APPLaunch with dummy for fb_load ─
# fb_load (the DRM→framebuffer bridge) polls /proc/*/exe for the APPLaunch
# binary path. Without a matching process it never refreshes the LCD.
# We keep APPLaunch enabled but replace the binary with sleep so fb_load
# is satisfied while only RaspyJack draws on the framebuffer.
# The original binary is saved as .real for easy revert.
if [[ "$DISPLAY_TYPE" == "CARDPUTER_320" ]]; then
  APPLAUNCH_BIN="/usr/share/APPLaunch/bin/M5CardputerZero-APPLaunch"
  step "Replacing APPLaunch with dummy for fb_load …"
  sudo -u pi XDG_RUNTIME_DIR=/run/user/1000 systemctl --user stop APPLaunch.service 2>/dev/null || true
  if [ -f "$APPLAUNCH_BIN" ] && [ ! -L "$APPLAUNCH_BIN" ]; then
    sudo mv "$APPLAUNCH_BIN" "${APPLAUNCH_BIN}.real"
  fi
  sudo ln -sf /usr/bin/sleep "$APPLAUNCH_BIN"
  # Override the unit to pass "infinity" arg so sleep never exits
  OVERRIDE_DIR="/home/pi/.config/systemd/user/APPLaunch.service.d"
  mkdir -p "$OVERRIDE_DIR"
  cat > "$OVERRIDE_DIR/override.conf" <<'OVRD'
[Service]
ExecStart=
ExecStart=/usr/share/APPLaunch/bin/M5CardputerZero-APPLaunch infinity
OVRD
  sudo -u pi XDG_RUNTIME_DIR=/run/user/1000 systemctl --user daemon-reload 2>/dev/null || true
  sudo -u pi XDG_RUNTIME_DIR=/run/user/1000 systemctl --user enable APPLaunch.service 2>/dev/null || true
  sudo -u pi XDG_RUNTIME_DIR=/run/user/1000 systemctl --user start APPLaunch.service 2>/dev/null || true
  info "APPLaunch replaced with sleep dummy (fb_load satisfied, original saved as .real)"

  step "Disabling desktop environment (saves RAM on CM0) …"
  sudo systemctl stop lightdm.service 2>/dev/null || true
  sudo systemctl disable lightdm.service 2>/dev/null || true
  info "LightDM disabled — HDMI available for RaspyJack mirroring"

  step "Disabling PipeWire/WirePlumber (frees audio device for direct ALSA) …"
  sudo systemctl --user --global disable pipewire.service pipewire.socket pipewire-pulse.service pipewire-pulse.socket wireplumber.service 2>/dev/null || true
  sudo pkill -9 pipewire wireplumber pipewire-pulse 2>/dev/null || true
  info "PipeWire disabled — ALSA direct access for audio"

  step "Releasing serial port for GPS HAT …"
  sudo systemctl stop serial-getty@ttyS0.service 2>/dev/null || true
  sudo systemctl disable serial-getty@ttyS0.service 2>/dev/null || true
  sudo systemctl mask serial-getty@ttyS0.service 2>/dev/null || true
  sudo systemctl stop serial-getty@ttyAMA0.service 2>/dev/null || true
  sudo systemctl disable serial-getty@ttyAMA0.service 2>/dev/null || true
  sudo systemctl mask serial-getty@ttyAMA0.service 2>/dev/null || true
  info "serial-getty disabled on ttyS0/ttyAMA0 — GPS HAT can use UART"

  step "Disabling console on LCD framebuffer …"
  sudo systemctl stop getty@tty1.service 2>/dev/null || true
  sudo systemctl disable getty@tty1.service 2>/dev/null || true
  # Remove console=tty1 from cmdline and add fbcon=map:99 to prevent fbcon on LCD
  CMDLINE="/boot/firmware/cmdline.txt"
  if [ -f "$CMDLINE" ]; then
    sudo sed -i 's/ console=tty1//g' "$CMDLINE"
    grep -q "fbcon=map:99" "$CMDLINE" || sudo sed -i 's/$/ fbcon=map:99/' "$CMDLINE"
  fi
  # Unbind fbcon from LCD immediately
  echo 0 | sudo tee /sys/class/vtconsole/vtcon1/bind 2>/dev/null || true
  info "Console/getty disabled on LCD — no more cursor bleed-through"
fi

# ───── 2 ▸ install / upgrade required system packages ────────
if [[ "$PKG_MGR" == "apt" ]]; then
  PACKAGES=(
    python3 python3-pip python3-dev \
    python3-scapy python3-netifaces python3-pyudev python3-serial \
    python3-smbus python3-pil python3-qrcode python3-numpy \
    python3-setuptools python3-cryptography python3-requests python3-websockets \
    python3-evdev \
    libglib2.0-dev python3-bluez bluez \
    fonts-dejavu-core nmap ncat tcpdump tshark arp-scan dsniff ettercap-text-only php procps \
    aircrack-ng wireless-tools wpasupplicant iw \
    hostapd dnsmasq-base sshpass bridge-utils john autossh reaver ebtables \
    firmware-linux-nonfree firmware-realtek firmware-atheros \
    git curl rfkill kbd i2c-tools rtl-sdr alsa-utils unzip \
    ffmpeg yt-dlp mpv gpsd gpsd-clients
  )
  # Only require Pi SPI/GPIO packages on physical HAT display modes
  if [[ "$DISPLAY_TYPE" == "ST7735_128" || "$DISPLAY_TYPE" == "ST7789_240" ]]; then
    PACKAGES+=( python3-spidev pulseaudio-utils )
    if apt-cache show python3-rpi.gpio >/dev/null 2>&1; then
      PACKAGES+=( python3-rpi.gpio )
    elif apt-cache show python3-rpi-lgpio >/dev/null 2>&1; then
      PACKAGES+=( python3-rpi-lgpio )
    fi
  elif [[ "$DISPLAY_TYPE" == "CARDPUTER_320" ]]; then
    PACKAGES+=( rtl-433 bluez-alsa-utils pulseaudio-utils chocolate-doom freedoom xvfb xdotool )
  elif [[ "$DISPLAY_TYPE" == "HDMI" ]]; then
    PACKAGES+=( libsdl2-2.0-0 pipewire-audio wireplumber libspa-0.2-bluetooth pulseaudio-utils cage xwayland seatd nodejs )
  fi

  # SDR/radio tools
  PACKAGES+=( direwolf multimon-ng transmission-daemon transmission-cli )

  # Fix missing GPG keys before apt update
  step "Checking APT repository keys …"
  if ! as_root apt-get update -qq 2>&1 | grep -q "^E:"; then
    info "APT repositories OK"
  else
    warn "APT key issue detected, attempting fix..."
    if [[ -f /etc/apt/sources.list.d/kali-rolling.list ]]; then
      as_root wget -q -O /usr/share/keyrings/kali-archive-keyring.gpg https://archive.kali.org/archive-key.asc 2>/dev/null \
        && info "Kali GPG key installed" \
        || warn "Could not fetch Kali GPG key"
      if ! grep -q "signed-by" /etc/apt/sources.list.d/kali-rolling.list 2>/dev/null; then
        echo "deb [signed-by=/usr/share/keyrings/kali-archive-keyring.gpg] http://http.kali.org/kali kali-rolling main contrib non-free non-free-firmware" \
          | as_root tee /etc/apt/sources.list.d/kali-rolling.list >/dev/null
        info "Kali repo updated with signed-by"
      fi
    fi
    as_root apt-get update -qq || warn "APT update had errors, continuing..."
  fi

  # Filter out packages unavailable on this distro/arch (e.g., python3-bluez on Trixie/Ubuntu 24.04)
  step "Resolving available APT packages …"
  AVAIL_PACKAGES=()
  MISSING_PACKAGES=()
  for pkg in "${PACKAGES[@]}"; do
    if apt-cache show "$pkg" >/dev/null 2>&1; then
      AVAIL_PACKAGES+=("$pkg")
    else
      MISSING_PACKAGES+=("$pkg")
    fi
  done
  if ((${#MISSING_PACKAGES[@]})); then
    warn "Skipping packages not in this distro's APT repos: ${MISSING_PACKAGES[*]}"
  fi

  step "Installing dependencies …"
  if ((${#AVAIL_PACKAGES[@]})); then
    to_install=()
    mapfile -t to_install < <(as_root apt-get -qq --just-print install "${AVAIL_PACKAGES[@]}" 2>/dev/null | awk '/^Inst/ {print $2}' || true)
    if ((${#to_install[@]})); then
      info "Will install/upgrade ${#to_install[@]} packages..."
      if ! as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${AVAIL_PACKAGES[@]}"; then
        warn "Bulk APT install hit an error — installing core packages individually..."
        for core_pkg in python3 python3-pip python3-pil python3-numpy python3-requests python3-evdev fonts-dejavu-core libsdl2-2.0-0; do
          if apt-cache show "$core_pkg" >/dev/null 2>&1; then
            as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$core_pkg" || true
          fi
        done
      fi
    else
      info "All available APT packages already installed & up‑to‑date."
    fi
  fi

elif [[ "$PKG_MGR" == "pacman" ]]; then
  step "Installing dependencies via pacman (Arch Linux) …"
  ARCH_CORE=(
    python python-pip python-pillow python-numpy python-requests
    python-evdev python-pyserial python-qrcode python-cryptography
    python-websockets sdl2 ttf-dejavu
  )
  ARCH_OPT=(
    python-scapy nmap tcpdump wireshark-cli aircrack-ng iw wpa_supplicant
    hostapd dnsmasq sshpass john ffmpeg yt-dlp mpv nodejs bluez bluez-utils
    pipewire pipewire-pulse pipewire-alsa wireplumber libpulse cage xorg-xwayland seatd
    alsa-utils curl unzip git rtl-sdr gpsd
  )
  ARCH_AVAIL=()
  for pkg in "${ARCH_CORE[@]}" "${ARCH_OPT[@]}"; do
    if pacman -Si "$pkg" >/dev/null 2>&1; then
      ARCH_AVAIL+=("$pkg")
    fi
  done
  if ((${#ARCH_AVAIL[@]})); then
    as_root pacman -S --needed --noconfirm "${ARCH_AVAIL[@]}" \
      || warn "Some pacman packages failed — installing core HDMI dependencies..."
  fi

elif [[ "$PKG_MGR" == "dnf" ]]; then
  step "Installing dependencies via dnf (Fedora/RHEL) …"
  DNF_PKGS=(
    python3 python3-pip python3-pillow python3-numpy python3-requests
    python3-evdev python3-pyserial python3-qrcode python3-cryptography
    SDL2 dejavu-sans-fonts dejavu-sans-mono-fonts
    nmap tcpdump aircrack-ng iw wpa_supplicant hostapd dnsmasq
    ffmpeg yt-dlp mpv nodejs bluez pipewire pipewire-pulseaudio wireplumber pulseaudio-utils
    cage xorg-x11-server-Xwayland seatd alsa-utils curl unzip git
  )
  as_root dnf install -y --skip-broken "${DNF_PKGS[@]}" \
    || warn "Some dnf packages failed (non-critical, continuing...)"

else
  warn "Unsupported package manager — skipping system package install. Ensure python3, Pillow, numpy, requests, evdev, and SDL2 are installed."
fi

# ───── 2‑a1b ▸ Kismet (from official repo, optional) ─────────────
if ! cmd kismet; then
  KISMET_ARCH=$(dpkg --print-architecture 2>/dev/null || true)
  case "$KISMET_ARCH" in
    amd64|arm64)
      # Detect Debian codename for correct repo
      DEBIAN_CODENAME=$(lsb_release -cs 2>/dev/null || grep '^VERSION_CODENAME=' /etc/os-release 2>/dev/null | head -n1 | cut -d= -f2 | tr -d '"\r ' || true)
      DEBIAN_CODENAME=${DEBIAN_CODENAME:-trixie}
      # Kismet repos exist for: bookworm, trixie
      case "$DEBIAN_CODENAME" in
        bookworm|trixie) ;;
        *) DEBIAN_CODENAME="trixie" ;; # fallback
      esac
      step "Installing Kismet from official repo ($DEBIAN_CODENAME, $KISMET_ARCH) …"
      # De-armor off to the side. Tee'ing the live keyring before gpg succeeds
      # truncates it; deleting that file afterwards leaves kismet.list signed-by
      # a path apt can no longer read, and the next unguarded apt aborts.
      kismet_keyring=/usr/share/keyrings/kismet-archive-keyring.gpg
      kismet_list=/etc/apt/sources.list.d/kismet.list
      kismet_stage="${kismet_keyring}.tmp.$$"
      kismet_list_stage="${kismet_list}.tmp.$$"
      kismet_key_ok=0
      kismet_key_tmp=""
      if kismet_key_tmp=$(mktemp "${TMPDIR:-/tmp}/kismet-archive-keyring.XXXXXX"); then
        if wget -O - https://www.kismetwireless.net/repos/kismet-release.gpg.key --quiet \
          | gpg --dearmor | tee "$kismet_key_tmp" >/dev/null 2>&1 \
          && [ -s "$kismet_key_tmp" ]; then
          if sudo tee "$kismet_stage" < "$kismet_key_tmp" >/dev/null \
            && sudo mv -f "$kismet_stage" "$kismet_keyring" \
            && sudo chmod 644 "$kismet_keyring"; then
            kismet_key_ok=1
          else
            if ! sudo rm -f "$kismet_stage"; then
              warn "Could not remove staged Kismet keyring $kismet_stage"
            fi
            warn "Could not install Kismet GPG key $kismet_keyring (non-critical, can install later from WebUI)"
          fi
        else
          warn "Could not fetch Kismet GPG key (non-critical, can install later from WebUI)"
        fi
        if [ -f "$kismet_key_tmp" ] && ! rm -f "$kismet_key_tmp"; then
          warn "Could not remove temporary Kismet keyring $kismet_key_tmp"
        fi
      else
        kismet_key_tmp=""
        warn "Could not create temporary file for Kismet GPG key (non-critical, can install later from WebUI)"
      fi
      if [ "$kismet_key_ok" -eq 1 ]; then
        if echo "deb [arch=$KISMET_ARCH signed-by=/usr/share/keyrings/kismet-archive-keyring.gpg] https://www.kismetwireless.net/repos/apt/release/$DEBIAN_CODENAME $DEBIAN_CODENAME main" \
          | sudo tee "$kismet_list_stage" >/dev/null \
          && sudo mv -f "$kismet_list_stage" "$kismet_list" \
          && sudo apt-get update -qq 2>/dev/null; then
          sudo apt-get install -y --no-install-recommends kismet 2>/dev/null \
            || warn "Kismet install failed (non-critical, can install later from WebUI)"
        else
          if ! sudo rm -f "$kismet_list_stage"; then
            warn "Could not remove staged Kismet source list $kismet_list_stage"
          fi
          warn "Kismet APT update failed (non-critical, can install later from WebUI)"
        fi
      elif ! sudo test -s "$kismet_keyring" && sudo test -e "$kismet_list"; then
        # Keyring is missing or empty, so the list's signed-by path would make
        # the next unguarded apt abort. Drop the list, and say so if we cannot.
        if ! sudo rm -f "$kismet_list"; then
          warn "Could not remove $kismet_list (still references missing Kismet keyring $kismet_keyring)"
        fi
      fi
      if cmd kismet; then
        sudo systemctl disable kismet 2>/dev/null || true
        sudo systemctl stop kismet 2>/dev/null || true
      fi
      ;;
    *)
      if ! sudo rm -f /etc/apt/sources.list.d/kismet.list; then
        warn "Could not remove /etc/apt/sources.list.d/kismet.list"
      fi
      warn "Skipping Kismet install on ${KISMET_ARCH:-unknown}: official Kismet packages require a 64-bit OS (amd64/arm64)"
      ;;
  esac
else
  info "Kismet already installed."
fi

# ───── 2‑a1c ▸ Kismet config ────────────────────────────────────
if cmd kismet && [[ ! -f /etc/kismet/kismet_raspyjack.conf ]]; then
  as_root mkdir -p /etc/kismet
  cat <<KCONF | as_root tee /etc/kismet/kismet_raspyjack.conf >/dev/null
httpd_bind_address=127.0.0.1
httpd_port=2501
log_prefix=$INSTALL_DIR/loot/kismet/logs
KCONF
  info "Kismet RaspyJack config created."
fi

# ───── 2‑a1d ▸ dump1090 (compile if missing, non-critical) ──────
if ! cmd dump1090 && [[ "$PKG_MGR" == "apt" ]]; then
  step "Building dump1090 from source (optional) …"
  (
    as_root apt-get install -y --no-install-recommends build-essential librtlsdr-dev libncurses-dev pkg-config >/dev/null 2>&1
    dump_tmp=$(mktemp -d "${TMPDIR:-/tmp}/dump1090-build.XXXXXX")
    trap 'rm -rf "$dump_tmp"' EXIT
    git clone --depth 1 https://github.com/flightaware/dump1090 "$dump_tmp" >/dev/null 2>&1
    make -C "$dump_tmp" BLADERF=no HACKRF=no LIMESDR=no -j"$(nproc)" >/dev/null 2>&1
    as_root cp "$dump_tmp/dump1090" /usr/local/bin/
  ) && info "dump1090 built and installed." \
    || warn "dump1090 build skipped/failed (non-critical)"
elif cmd dump1090; then
  info "dump1090 already installed."
fi

# ───── 2‑a1e ▸ pip packages (optional) ──────────────────────────
if cmd pip3; then
  as_root pip3 install --break-system-packages sgp4 >/dev/null 2>&1 || true

  # ───── 2‑a2 ▸ pip packages not available via APT ──────────────
  step "Installing optional Python packages via pip …"
  as_root pip3 install --break-system-packages smbus2 >/dev/null 2>&1 \
    || as_root pip3 install smbus2 >/dev/null 2>&1 \
    || warn "smbus2 pip install failed – i2c_scanner payload may not work"

  if [[ "$PKG_MGR" == "apt" ]]; then
    step "Upgrading yt-dlp to latest version (with YouTube JS challenge support) …"
    (as_root pip3 install --upgrade --break-system-packages --ignore-installed "yt-dlp[default]" >/dev/null 2>&1 \
      || as_root pip3 install --upgrade --break-system-packages --ignore-installed yt-dlp >/dev/null 2>&1) \
      && info "yt-dlp upgraded ($(yt-dlp --version 2>/dev/null || echo ready))" \
      || warn "yt-dlp upgrade failed – YouTube / YT Music payloads may be throttled"
  fi

  step "Installing gpsd-py3 …"
  as_root pip3 install --break-system-packages gpsd-py3 >/dev/null 2>&1 \
    || warn "gpsd-py3 install failed – wardriving GPS may not work"
else
  warn "pip3 not found — skipping optional pip installs"
fi

# Disable hostapd/dnsmasq auto-start (only used on-demand by payloads)
if cmd systemctl; then
  as_root systemctl disable --now hostapd 2>/dev/null || true
  as_root systemctl disable --now dnsmasq 2>/dev/null || true
fi

# ───── 2‑b ▸ Wall-of-Flippers: bluepy + bleak ──────────────────
step "Checking BLE libraries (bluepy + bleak) …"

BLUEPY_OK=0
if python3 -c "import bluepy" 2>/dev/null; then
  BLUEPY_OK=1
fi

if [[ "$BLUEPY_OK" -eq 0 && "$PKG_MGR" == "apt" ]] && cmd git; then
  info "bluepy not found, attempting source build (optional)..."
  BLUEPY_BUILD=$(mktemp -d "${TMPDIR:-/tmp}/bluepy-build.XXXXXX")
  if git clone --depth 1 https://github.com/IanHarvey/bluepy.git "$BLUEPY_BUILD" 2>/dev/null; then
    (cd "$BLUEPY_BUILD" && python3 setup.py build && as_root python3 setup.py install) >/dev/null 2>&1 \
      && info "Installed bluepy from source" \
      || warn "bluepy build failed (bleak will be used instead)"
  else
    warn "Could not clone bluepy repo (bleak will be used instead)"
  fi
  rm -rf "$BLUEPY_BUILD"
elif [[ "$BLUEPY_OK" -eq 1 ]]; then
  info "bluepy already installed, skipping build"
fi

# Set capabilities on bluepy-helper (needed for non-root BLE)
HELPER=$(find /usr -name "bluepy-helper" 2>/dev/null | head -1 || true)
if [[ -n "$HELPER" ]] && cmd setcap; then
  as_root setcap 'cap_net_raw,cap_net_admin+eip' "$HELPER" 2>/dev/null \
    && info "bluepy-helper capabilities set" \
    || warn "Could not set bluepy-helper capabilities"
fi

# Install bleak as modern fallback (preferred by wall_of_flippers)
if ! python3 -c "import bleak" 2>/dev/null; then
  if cmd pip3; then
    (as_root pip3 install --break-system-packages bleak >/dev/null 2>&1 \
      || as_root pip3 install bleak >/dev/null 2>&1) \
      && info "bleak installed" \
      || warn "bleak install failed (optional)"
  fi
else
  info "bleak already installed"
fi

# Verify at least one BLE library works
python3 - <<'PY' || warn "No BLE library available; WoF/BLE payloads may not work."
try:
    import bluepy
    print("[OK] bluepy available")
except ImportError:
    try:
        import bleak
        print("[OK] bleak available (bluepy missing)")
    except ImportError:
        print("[WARN] No BLE library")
        import sys; sys.exit(1)
PY

# ───── 2‑c ▸ Navarro (vendored in repo) ─────────────────────────
NAVARRO_PATH="$INSTALL_DIR/Navarro/navarro.py"
if [[ -f "$NAVARRO_PATH" ]]; then
  chmod +x "$NAVARRO_PATH" 2>/dev/null || as_root chmod +x "$NAVARRO_PATH"
  info "Navarro found: $NAVARRO_PATH"
else
  warn "Navarro not found at $NAVARRO_PATH – add Navarro/ to your Raspyjack repo for OSINT payload"
fi

# FontAwesome font (skip if already present and non-empty)
FA_DEST="/usr/share/fonts/truetype/fontawesome/fa-solid-900.ttf"
if [[ ! -s "$FA_DEST" ]]; then
  as_root mkdir -p /usr/share/fonts/truetype/fontawesome 2>/dev/null || true
  FA_OK=0
  FA_TMP=$(mktemp "${TMPDIR:-/tmp}/fa-solid-900.XXXXXX.ttf")
  for FA_URL in \
    "https://use.fontawesome.com/releases/v6.5.1/webfonts/fa-solid-900.ttf" \
    "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/webfonts/fa-solid-900.ttf" \
    "https://raw.githubusercontent.com/FortAwesome/Font-Awesome/6.x/webfonts/fa-solid-900.ttf"; do
    if cmd curl && curl -fsSL --connect-timeout 10 -o "$FA_TMP" "$FA_URL" 2>/dev/null && [[ -s "$FA_TMP" ]]; then
      FA_OK=1
      break
    elif cmd wget && wget -q --timeout=10 -O "$FA_TMP" "$FA_URL" 2>/dev/null && [[ -s "$FA_TMP" ]]; then
      FA_OK=1
      break
    fi
  done
  if [[ "$FA_OK" == "1" ]] && as_root mv -f "$FA_TMP" "$FA_DEST" && as_root chmod 644 "$FA_DEST"; then
    info "FontAwesome font installed"
  else
    rm -f "$FA_TMP"
    warn "Could not download FontAwesome font (Nerd Font / DejaVu fallback will be used)"
  fi
else
  info "FontAwesome font already present"
fi

# ───── 2‑e ▸ Blacklist DVB driver for RTL-SDR direct access ──
step "Blacklisting DVB driver for RTL-SDR …"
if [[ -d /etc/modprobe.d ]]; then
  if ! grep -q "blacklist dvb_usb_rtl28xxu" /etc/modprobe.d/rtlsdr-blacklist.conf 2>/dev/null; then
    echo "blacklist dvb_usb_rtl28xxu" | as_root tee /etc/modprobe.d/rtlsdr-blacklist.conf >/dev/null
    as_root modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true
    info "dvb_usb_rtl28xxu blacklisted"
  else
    info "DVB blacklist already configured"
  fi
fi

# ───── 3 ▸ RPi.GPIO smart router + I²C / SPI kernel modules ──
# Guarantee the smart RPi.GPIO router exists in $INSTALL_DIR/RPi/ for ALL modes
# (prevents ModuleNotFoundError / hardware GPIO crashes if cloned without RPi/).
if [[ ! -f "$INSTALL_DIR/RPi/GPIO.py" ]]; then
  step "Creating smart RPi.GPIO router ($INSTALL_DIR/RPi/GPIO.py) …"
  "${_write_conf_runner[@]}" mkdir -p "$INSTALL_DIR/RPi"
  "${_write_conf_runner[@]}" tee "$INSTALL_DIR/RPi/__init__.py" >/dev/null <<'RPI_INIT'
"""Smart RPi package router for EmberJack / RaspyJack."""
RPI_INIT
  "${_write_conf_runner[@]}" tee "$INSTALL_DIR/RPi/GPIO.py" >/dev/null <<'RPI_GPIO'
"""Smart RPi.GPIO module router for EmberJack / RaspyJack."""

import importlib.machinery
import importlib.util
import os
import sys

_USE_REAL_GPIO = False
try:
    import rj_env

    _dtype = rj_env.get_display_type()
    _USE_REAL_GPIO = _dtype in ("ST7735_128", "ST7789_240")
except Exception:
    _USE_REAL_GPIO = False


def _load_system_rpi_gpio():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    ext_paths = [
        p for p in sys.path
        if p and os.path.abspath(p) != repo_root and not os.path.abspath(p).startswith(repo_root + os.sep)
    ]
    spec = importlib.machinery.PathFinder.find_spec("RPi", ext_paths)
    if spec is None or not spec.submodule_search_locations:
        return None
    gpio_spec = importlib.machinery.PathFinder.find_spec("GPIO", list(spec.submodule_search_locations))
    if gpio_spec is None or gpio_spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(gpio_spec)
    gpio_spec.loader.exec_module(mod)
    return mod


_real_gpio = None
if _USE_REAL_GPIO:
    try:
        _real_gpio = _load_system_rpi_gpio()
    except Exception:
        _real_gpio = None

if _real_gpio is not None:
    for _attr in dir(_real_gpio):
        if not _attr.startswith("__"):
            globals()[_attr] = getattr(_real_gpio, _attr)
else:
    from gpio_shim import *  # noqa: F401,F403
RPI_GPIO
fi

# Preserve ownership for non-root clone directories when installer was invoked via sudo
if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  _DIR_OWNER=$(stat -c '%U' "$INSTALL_DIR" 2>/dev/null || true)
  if [[ "$_DIR_OWNER" == "$SUDO_USER" ]]; then
    _DIR_GROUP=$(stat -c '%G' "$INSTALL_DIR" 2>/dev/null || echo "$SUDO_USER")
    for _own_p in "$INSTALL_DIR/RPi" "$INSTALL_DIR/gui_conf.json" "$INSTALL_DIR/loot" "$INSTALL_DIR/config"; do
      if [[ -e "$_own_p" ]]; then
        as_root chown -R "${_DIR_OWNER}:${_DIR_GROUP}" "$_own_p" 2>/dev/null || true
      fi
    done
  fi
fi

if [[ "$DISPLAY_TYPE" == "HDMI" ]]; then
  info "HDMI monitor + normal keyboard mode — skipping SPI/I²C HAT setup (RPi/GPIO.py router active)"
elif [[ "$DISPLAY_TYPE" == "CARDPUTER_320" ]]; then
  info "CardputerZero: framebuffer display — skipping SPI/I²C HAT setup"

  # ALSA config for ES8388 codec
  step "Configuring ALSA for ES8388 audio codec …"
  ES_CARD=$(aplay -l 2>/dev/null | grep -i 'ES8388\|ES8389' | head -1 | sed 's/card //' | cut -d: -f1 || true)
  ES_CARD=${ES_CARD:-0}
  as_root tee /etc/asound.conf >/dev/null <<ALSA
defaults.pcm.card $ES_CARD
defaults.ctl.card $ES_CARD
ALSA
  info "ALSA configured for ES8388 (card $ES_CARD)"
  info "RPi/GPIO.py shim active (evdev-based keyboard input)"

  # Install opencv-python-headless via pip (not in apt)
  if cmd pip3; then
    step "Installing OpenCV …"
    as_root pip3 install --break-system-packages opencv-python-headless >/dev/null 2>&1 \
      || warn "opencv install failed — video_player may not work"
  fi

else
  step "Checking I²C & SPI …"
  SPI_OK=0
  if ls /dev/spidev0.0 >/dev/null 2>&1 && grep -q "i2c-dev" /etc/modules 2>/dev/null; then
    SPI_OK=1
  fi
  if [[ $SPI_OK -eq 1 ]]; then
    info "I²C & SPI already configured"
  else
    add_dtparam dtparam=i2c_arm=on
    add_dtparam dtparam=i2c1=on
    add_dtparam dtparam=spi=on
    MODULES=(i2c-bcm2835 i2c-dev spi_bcm2835 spidev)
    for m in "${MODULES[@]}"; do
      if [[ -f /etc/modules ]]; then
        grep -qxF "$m" /etc/modules 2>/dev/null || echo "$m" | as_root tee -a /etc/modules >/dev/null
      fi
      as_root modprobe "$m" 2>/dev/null || true
    done
    if [[ -f "$CFG" ]]; then
      grep -qE '^dtoverlay=spi0-[12]cs' "$CFG" || echo 'dtoverlay=spi0-2cs' | as_root tee -a "$CFG" >/dev/null
    fi
    info "I²C & SPI configured"
  fi
fi

# ───── 4 ▸ WiFi attack setup ──────────────────────────────────
step "Setting up WiFi attack environment …"

# Pin onboard WiFi to wlan0 so it never swaps with USB dongles across reboots.
if [[ -f /etc/systemd/network/10-onboard-wifi.link ]]; then
  info "WiFi interface pinning already configured"
else
  step "Pinning onboard WiFi to wlan0 (persistent naming) …"

# Detect WiFi MAC addresses by bus:
# - onboard chip: SDIO/MMC -> forced to wlan0
# - first USB dongle: USB bus -> forced to wlan1
ONBOARD_MAC=""
USB_MAC=""
for dev in /sys/class/net/wlan*; do
  [ -e "$dev" ] || continue
  DEVPATH=$(readlink -f "$dev/device" 2>/dev/null || true)
  if echo "$DEVPATH" | grep -q "mmc"; then
    ONBOARD_MAC=$(cat "$dev/address" 2>/dev/null || true)
    ONBOARD_NAME=$(basename "$dev")
    info "Found onboard WiFi: $ONBOARD_NAME ($ONBOARD_MAC) on SDIO/MMC bus"
  elif [ -z "$USB_MAC" ] && echo "$DEVPATH" | grep -q "usb"; then
    USB_MAC=$(cat "$dev/address" 2>/dev/null || true)
    USB_NAME=$(basename "$dev")
    info "Found USB WiFi dongle: $USB_NAME ($USB_MAC) on USB bus"
  fi
done

if [ -n "$ONBOARD_MAC" ]; then
  # Method 1: systemd .link file (takes priority on Bookworm / modern systemd)
  # This is the RELIABLE way — systemd overrides udev NAME= rules
  sudo tee /etc/systemd/network/10-onboard-wifi.link >/dev/null <<LINK
[Match]
MACAddress=$ONBOARD_MAC

[Link]
Name=wlan0
LINK

  if [ -n "$USB_MAC" ]; then
    sudo tee /etc/systemd/network/11-usb-wifi.link >/dev/null <<LINK
[Match]
MACAddress=$USB_MAC

[Link]
Name=wlan1
LINK
  else
    warn "No USB WiFi dongle detected during install - wlan1 pin skipped"
  fi

  # Method 2: udev rule ONLY on non-systemd systems (on systemd, 10-onboard-wifi.link already handles renaming;
  # having both a .link file and a udev NAME= rule can race and delay/break WiFi initialization at boot)
  if [[ ! -d /run/systemd/system ]]; then
    sudo tee /etc/udev/rules.d/70-raspyjack-wifi.rules >/dev/null <<UDEV
# RaspyJack: pin WiFi interfaces by MAC
# Onboard WiFi (SDIO) -> wlan0
SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="$ONBOARD_MAC", NAME="wlan0"
UDEV
    if [ -n "$USB_MAC" ]; then
      echo "SUBSYSTEM==\"net\", ACTION==\"add\", ATTR{address}==\"$USB_MAC\", NAME=\"wlan1\"" | sudo tee -a /etc/udev/rules.d/70-raspyjack-wifi.rules >/dev/null
    fi
  fi

  sudo udevadm control --reload-rules
  info "Pinned onboard WiFi ($ONBOARD_MAC) to wlan0 via systemd .link"
  if [ -n "$USB_MAC" ]; then
    info "Pinned USB WiFi dongle ($USB_MAC) to wlan1 via systemd .link"
  fi
  info "This will take effect after reboot"
else
  warn "Could not detect onboard WiFi MAC — skipping interface pinning"
  warn "Run 'ip link' and manually create /etc/systemd/network/10-onboard-wifi.link"
fi
fi  # end WiFi pinning check

# Remove redundant udev rename rule on systemd hosts where 10-onboard-wifi.link is already active
if [[ -d /run/systemd/system && -f /etc/systemd/network/10-onboard-wifi.link && -f /etc/udev/rules.d/70-raspyjack-wifi.rules ]]; then
  as_root rm -f /etc/udev/rules.d/70-raspyjack-wifi.rules 2>/dev/null || true
  as_root udevadm control --reload-rules 2>/dev/null || true
  info "Removed redundant 70-raspyjack-wifi.rules (10-onboard-wifi.link handles naming cleanly)"
fi

"${_write_conf_runner[@]}" mkdir -p "$INSTALL_DIR/wifi/profiles"
"${_write_conf_runner[@]}" chmod 755 "$INSTALL_DIR/wifi/profiles"

if [[ ! -f "$INSTALL_DIR/wifi/profiles/sample.json" ]]; then
  "${_write_conf_runner[@]}" tee "$INSTALL_DIR/wifi/profiles/sample.json" >/dev/null <<'PROFILE'
{
  "ssid": "YourWiFiNetwork",
  "password": "your_password_here",
  "interface": "auto",
  "priority": 1,
  "auto_connect": true,
  "created": "2024-01-01T12:00:00",
  "last_used": null,
  "notes": "Sample WiFi profile - edit with your network details"
}
PROFILE
fi

if cmd systemctl && systemctl is-active --quiet NetworkManager 2>/dev/null; then
  # Only mark monitor-mode interfaces unmanaged; NEVER override [main] plugins=ifupdown,keyfile
  # which breaks/slows NetworkManager WiFi on Raspberry Pi OS Bookworm/Trixie!
  if [[ -f /etc/NetworkManager/conf.d/99-wifi-attacks.conf ]] && ! grep -q "plugins=ifupdown" /etc/NetworkManager/conf.d/99-wifi-attacks.conf 2>/dev/null; then
    info "NetworkManager WiFi attack config already present"
  else
    info "Configuring NetworkManager for WiFi monitor interfaces (preserving native keyfile WiFi)"
    as_root mkdir -p /etc/NetworkManager/conf.d
    as_root tee /etc/NetworkManager/conf.d/99-wifi-attacks.conf >/dev/null <<'NM_CONF'
[keyfile]
unmanaged-devices=interface-name:wlan0mon;interface-name:wlan1mon;interface-name:wlan2mon
NM_CONF
    as_root systemctl reload NetworkManager 2>/dev/null || as_root systemctl restart NetworkManager 2>/dev/null || warn "Could not reload NetworkManager"
  fi
else
  warn "NetworkManager not active - WiFi attacks may need manual setup"
fi

# Fast non-blocking fallback: ensure onboard Pi SDIO WiFi is wlan0 before NetworkManager
# (NEVER waits on systemd-udev-settle.service and exits in <10ms if names are already right!)
if cmd systemctl && [[ -d /run/systemd/system ]] && [[ -f "$INSTALL_DIR/scripts/pin_wifi_names.sh" ]]; then
  step "Updating fast boot-time WiFi name pinning service …"
  as_root install -m 0755 "$INSTALL_DIR/scripts/pin_wifi_names.sh" /usr/local/sbin/raspyjack-pin-wifi.sh
  as_root tee /etc/systemd/system/raspyjack-pin-wifi.service >/dev/null <<UNIT
[Unit]
Description=RaspyJack Pin WiFi Interface Names (Fast)
After=local-fs.target
Before=NetworkManager.service network.target

[Service]
Type=oneshot
TimeoutStartSec=5
Environment=RJ_BASE_DIR=$INSTALL_DIR
ExecStart=/usr/local/sbin/raspyjack-pin-wifi.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
  as_root systemctl daemon-reload
  as_root systemctl enable raspyjack-pin-wifi.service || true
fi

# ───── 4‑c ▸ Bluetooth, Virtual Mouse (uinput) & Headless PipeWire ─
if [[ "$DISPLAY_TYPE" == "HDMI" ]] && cmd systemctl && [[ -d /run/systemd/system ]]; then
  # Ensure uinput kernel module is loaded at boot for Keyboard-Mouse Mode (Ctrl+Alt+M)
  if [[ -d /etc/modules-load.d ]]; then
    echo "uinput" | as_root tee /etc/modules-load.d/raspyjack-uinput.conf >/dev/null 2>&1 || true
  fi
  as_root modprobe uinput 2>/dev/null || true

  # Always disable conflicting bluealsa / bluez-alsa daemon on PipeWire HDMI installs:
  # if bluealsa starts at boot it steals BlueZ's A2DP endpoints, leaving PipeWire
  # stuck in muffled 16kHz mono Headset (HFP/HSP) mode with no A2DP profile!
  _BA_STOPPED=0
  for _ba_unit in bluealsa.service bluez-alsa.service bluealsa-aplay.service; do
    if systemctl list-unit-files "$_ba_unit" >/dev/null 2>&1; then
      as_root systemctl stop "$_ba_unit" >/dev/null 2>&1 || true
      as_root systemctl disable "$_ba_unit" >/dev/null 2>&1 || true
      as_root systemctl mask "$_ba_unit" >/dev/null 2>&1 || true
      _BA_STOPPED=1
      info "Disabled conflicting $_ba_unit so PipeWire owns Bluetooth A2DP Stereo"
    fi
  done

  # Disable WirePlumber HFP/HSP autoswitch and enable A2DP Stereo + SBC-XQ on ALL HDMI installs
  as_root mkdir -p /etc/wireplumber/wireplumber.conf.d /etc/wireplumber/bluetooth.lua.d
  as_root tee /etc/wireplumber/wireplumber.conf.d/51-emberjack-bt-a2dp.conf >/dev/null <<'WP_A2DP05'
wireplumber.settings = {
  bluetooth.autoswitch-to-headset-profile = false
}

monitor.bluez.properties = {
  bluez5.enable-sbc-xq = true
  bluez5.enable-msbc = true
  bluez5.roles = [ a2dp_sink a2dp_source hsp_hs hsp_ag hfp_hf hfp_ag ]
}
WP_A2DP05
  as_root tee /etc/wireplumber/bluetooth.lua.d/51-emberjack-bt-a2dp.lua >/dev/null <<'WP_A2DP04'
bluez_monitor.properties["bluez5.autoswitch-to-headset-profile"] = false
bluez_monitor.properties["bluez5.enable-sbc-xq"] = true
bluez_monitor.properties["bluez5.roles"] = "[ a2dp_sink a2dp_source hsp_hs hsp_ag hfp_hf hfp_ag ]"
WP_A2DP04

  if [[ $_BA_STOPPED -eq 1 ]]; then
    as_root systemctl restart bluetooth.service >/dev/null 2>&1 || true
  else
    as_root systemctl enable --now bluetooth.service >/dev/null 2>&1 || true
  fi
fi

if [[ "$DISPLAY_TYPE" == "HDMI" && $IS_DESKTOP -eq 0 ]] && cmd systemctl && [[ -d /run/systemd/system ]]; then
  step "Configuring headless PipeWire, Bluetooth A2DP & App Kiosk …"

  # Disable WirePlumber logind seat-monitoring so Bluetooth A2DP registers on headless boot (WP 0.5 & 0.4)
  as_root mkdir -p /etc/wireplumber/wireplumber.conf.d /etc/wireplumber/bluetooth.lua.d
  as_root tee /etc/wireplumber/wireplumber.conf.d/51-headless-bt.conf >/dev/null <<'WP05'
wireplumber.profiles = {
  main = {
    monitor.bluez.seat-monitoring = disabled
  }
}
WP05
  as_root tee /etc/wireplumber/bluetooth.lua.d/51-headless-bt.lua >/dev/null <<'WP04'
bluez_monitor.properties["with-logind"] = false
WP04
  info "Configured WirePlumber headless Bluetooth A2DP Stereo (WP 0.4 & 0.5)"

  as_root systemctl enable --now bluetooth.service >/dev/null 2>&1 || true
  if systemctl list-unit-files seatd.service >/dev/null 2>&1; then
    as_root systemctl enable --now seatd.service >/dev/null 2>&1 || true
    info "Enabled seatd.service for headless single-app GUI kiosk (cage)"
  fi

  # Locate normal non-root user (SUDO_USER -> UID 1000 -> first regular user >= 1000)
  AUDIO_USER=""
  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]] && id "$SUDO_USER" >/dev/null 2>&1; then
    AUDIO_USER="$SUDO_USER"
  elif id -nu 1000 >/dev/null 2>&1; then
    AUDIO_USER="$(id -nu 1000)"
  else
    AUDIO_USER="$(awk -F: '$3 >= 1000 && $3 < 65000 && $7 !~ /(nologin|false)$/ {print $1; exit}' /etc/passwd 2>/dev/null || true)"
  fi

  if [[ -n "$AUDIO_USER" ]]; then
    AUDIO_UID="$(id -u "$AUDIO_USER")"
    for _grp in audio bluetooth video render input seat; do
      if getent group "$_grp" >/dev/null 2>&1; then
        as_root usermod -aG "$_grp" "$AUDIO_USER" 2>/dev/null || true
      fi
    done
    if cmd loginctl; then
      as_root loginctl enable-linger "$AUDIO_USER" 2>/dev/null || true
    fi
    # Restart bluetoothd AFTER masking bluealsa, then restart user PipeWire/WirePlumber
    # so PipeWire registers the BlueZ A2DP endpoints cleanly.
    as_root systemctl restart bluetooth.service >/dev/null 2>&1 || true
    sleep 1
    as_root systemctl start "user@${AUDIO_UID}.service" >/dev/null 2>&1 || true
    sleep 1
    as_root systemctl --machine="${AUDIO_USER}@.host" --user enable \
      pipewire pipewire-pulse wireplumber >/dev/null 2>&1 || true
    as_root systemctl --machine="${AUDIO_USER}@.host" --user restart \
      pipewire pipewire-pulse wireplumber >/dev/null 2>&1 \
      || warn "Could not start user PipeWire services right now (will start on reboot)"
    info "Enabled headless PipeWire/WirePlumber A2DP lingering for '$AUDIO_USER' (UID $AUDIO_UID)"
  else
    warn "No non-root user found for PipeWire audio; run 'loginctl enable-linger <user>' manually."
  fi
fi

# ───── 5 ▸ RaspyJack core & WebUI systemd services ───────────
INSTALL_SERVICES=1
if [[ $NO_SERVICE -eq 1 ]] || ! cmd systemctl || [[ ! -d /run/systemd/system ]]; then
  INSTALL_SERVICES=0
elif [[ "$DISPLAY_TYPE" == "HDMI" && $IS_DESKTOP -eq 1 && $UPDATE_MODE -eq 0 ]]; then
  echo ""
  info "Desktop session detected (Wayland/X11) with HDMI mode."
  info "On a desktop PC, it is recommended to launch EmberJack on demand via: python3 run_hdmi.py"
  if [[ $AUTO_YES -eq 1 || ! -t 0 ]]; then
    INSTALL_SERVICES=0
  else
    read -rp "  Enable boot-time root systemd services anyway? [y/N]: " _svc_ans
    case "${_svc_ans:-N}" in
      y|Y|yes|YES) INSTALL_SERVICES=1 ;;
      *)           INSTALL_SERVICES=0 ;;
    esac
  fi
fi

if [[ $INSTALL_SERVICES -eq 1 ]]; then
  SERVICE=/etc/systemd/system/raspyjack.service
  step "Configuring core systemd service ($SERVICE) …"

  as_root tee "$SERVICE" >/dev/null <<UNIT
[Unit]
Description=RaspyJack UI Service
After=local-fs.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/raspyjack.py
Restart=on-failure
User=root
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=$INSTALL_DIR
Environment=RJ_BASE_DIR=$INSTALL_DIR

[Install]
WantedBy=multi-user.target
UNIT

  as_root systemctl daemon-reload
  as_root systemctl enable --now raspyjack.service || warn "Could not start raspyjack.service right now"

  # ───── 5‑b ▸ device server & WebUI split services ───────────
  # Shared WebUI token (used by both HTTP + WS servers)
  WEBUI_TOKEN_FILE="$INSTALL_DIR/.webui_token"
  WEBUI_AUTH_SECRET_FILE="$INSTALL_DIR/.webui_session_secret"
  step "Configuring shared WebUI token at $WEBUI_TOKEN_FILE …"
  if ! as_root test -s "$WEBUI_TOKEN_FILE"; then
    as_root python3 - "$WEBUI_TOKEN_FILE" <<'PY'
from pathlib import Path
import secrets, sys

path = Path(sys.argv[1])
path.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
print(f"[OK] Created {path}")
PY
  else
    info "Existing WebUI token file found, keeping it."
  fi
  as_root chown root:root "$WEBUI_TOKEN_FILE"
  as_root chmod 600 "$WEBUI_TOKEN_FILE"

  step "Configuring WebUI auth secret at $WEBUI_AUTH_SECRET_FILE …"
  if ! as_root test -s "$WEBUI_AUTH_SECRET_FILE"; then
    as_root python3 - "$WEBUI_AUTH_SECRET_FILE" <<'PY'
from pathlib import Path
import secrets, sys

path = Path(sys.argv[1])
path.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
print(f"[OK] Created {path}")
PY
  else
    info "Existing WebUI auth secret found, keeping it."
  fi
  as_root chown root:root "$WEBUI_AUTH_SECRET_FILE"
  as_root chmod 600 "$WEBUI_AUTH_SECRET_FILE"

  # Device server
  DEVICE_SERVICE=/etc/systemd/system/raspyjack-device.service
  step "Installing device server systemd service $DEVICE_SERVICE …"

  as_root tee "$DEVICE_SERVICE" >/dev/null <<UNIT
[Unit]
Description=RaspyJack Device Server
After=local-fs.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/device_server.py
Restart=on-failure
User=root
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=$INSTALL_DIR
Environment=RJ_BASE_DIR=$INSTALL_DIR
Environment=RJ_WS_TOKEN_FILE=$INSTALL_DIR/.webui_token
Environment=RJ_WEB_AUTH_SECRET_FILE=$INSTALL_DIR/.webui_session_secret
Environment=RJ_WEB_AUTH_FILE=$INSTALL_DIR/.webui_auth.json

[Install]
WantedBy=multi-user.target
UNIT

  as_root systemctl daemon-reload
  as_root systemctl enable --now raspyjack-device.service || warn "Could not start raspyjack-device.service"

  # WebUI HTTP server
  WEBUI_SERVICE=/etc/systemd/system/raspyjack-webui.service
  step "Installing WebUI systemd service $WEBUI_SERVICE …"

  as_root tee "$WEBUI_SERVICE" >/dev/null <<UNIT
[Unit]
Description=RaspyJack WebUI HTTP Server
After=raspyjack-device.service
Requires=raspyjack-device.service

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/web_server.py
Restart=on-failure
User=root
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=$INSTALL_DIR
Environment=RJ_BASE_DIR=$INSTALL_DIR
Environment=RJ_WS_TOKEN_FILE=$INSTALL_DIR/.webui_token
Environment=RJ_WEB_AUTH_SECRET_FILE=$INSTALL_DIR/.webui_session_secret
Environment=RJ_WEB_AUTH_FILE=$INSTALL_DIR/.webui_auth.json

[Install]
WantedBy=multi-user.target
UNIT

  as_root systemctl daemon-reload
  as_root systemctl enable --now raspyjack-webui.service || warn "Could not start raspyjack-webui.service"

  # ───── 5-c ▸ optional TLS reverse proxy (Caddy) ─────────────
  step "Setting up optional HTTPS reverse proxy with Caddy ..."
  set +e
  TLS_SETUP_OK=1

  # Install Caddy best-effort. If this fails, keep plain HTTP stack available.
  if cmd caddy; then
    info "Caddy already installed"
  elif [[ "$PKG_MGR" == "apt" ]]; then
    step "Installing Caddy package ..."
    if ! as_root apt-get install -y --no-install-recommends caddy; then
      warn "Caddy install failed; keeping WebUI on HTTP only."
      TLS_SETUP_OK=0
    fi
  else
    warn "Caddy not installed (optional); keeping WebUI on HTTP only."
    TLS_SETUP_OK=0
  fi

# Install Caddy auto-config service (refresh if missing or still blocking on network-online.target)
if [ "$TLS_SETUP_OK" -eq 1 ]; then
  if [[ -f /usr/local/sbin/raspyjack-caddy-autoconfig.sh ]] && systemctl is-enabled raspyjack-caddy-autoconfig.service >/dev/null 2>&1 && ! grep -q "network-online.target" /etc/systemd/system/raspyjack-caddy-autoconfig.service 2>/dev/null; then
    info "Caddy auto-config service already installed"
  else
    step "Installing Caddy auto-config service …"

  sudo tee /usr/local/sbin/raspyjack-caddy-autoconfig.sh >/dev/null <<'SCRIPT'
#!/usr/bin/env bash
# RaspyJack: generate Caddyfile + long-lived self-signed cert
# Binds on 0.0.0.0 — works on any network without reconfiguration
set -euo pipefail

CERT_DIR=/etc/caddy/certs
CERT=$CERT_DIR/raspyjack.crt
KEY=$CERT_DIR/raspyjack.key

# Generate 10-year self-signed cert covering all common private IPs
# Only regenerate if cert doesn't exist or is older than 1 year
if [ ! -f "$CERT" ] || [ ! -f "$KEY" ] || \
   [ "$(find "$CERT" -mtime +365 2>/dev/null)" ]; then
  mkdir -p "$CERT_DIR"

  # Collect all current IPs for SAN
  SAN="IP:127.0.0.1,IP:0.0.0.0,DNS:raspyjack,DNS:raspyjack.local,DNS:localhost"
  for iface in $(ls /sys/class/net/); do
    case "$iface" in lo|docker*|veth*|br-*) continue ;; esac
    IP=$(ip -4 -o addr show "$iface" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)
    [ -n "$IP" ] && SAN="$SAN,IP:$IP"
  done

  openssl req -x509 -nodes -days 3650 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
    -keyout "$KEY" -out "$CERT" \
    -subj "/CN=RaspyJack" \
    -addext "subjectAltName=$SAN" 2>/dev/null

  chown caddy:caddy "$KEY" 2>/dev/null || true
  echo "[raspyjack-caddy] Generated new TLS cert with SAN: $SAN"
fi

cat > /etc/caddy/Caddyfile <<EOF
:443 {
    tls $CERT $KEY

    @ws path /ws*
    reverse_proxy @ws 127.0.0.1:8765
    reverse_proxy 127.0.0.1:8080
}
EOF

systemctl reload caddy 2>/dev/null || systemctl restart caddy
echo "[raspyjack-caddy] Bound to 0.0.0.0 (all interfaces)"
SCRIPT
  sudo chmod +x /usr/local/sbin/raspyjack-caddy-autoconfig.sh

  sudo tee /etc/systemd/system/raspyjack-caddy-autoconfig.service >/dev/null <<'UNIT'
[Unit]
Description=RaspyJack Caddy auto-config (detect all IPs)
After=network.target caddy.service
Requires=caddy.service

[Service]
Type=oneshot
ExecStartPre=/bin/sleep 5
ExecStart=/usr/local/sbin/raspyjack-caddy-autoconfig.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT

    sudo systemctl daemon-reload
    sudo systemctl enable raspyjack-caddy-autoconfig.service
    # Run it now to generate the initial Caddyfile
    sudo /usr/local/sbin/raspyjack-caddy-autoconfig.sh
  fi
fi

if [ "$TLS_SETUP_OK" -eq 1 ]; then
  if ! sudo systemctl enable --now caddy.service; then
    warn "Failed to enable/start caddy.service; keeping HTTP services active."
    TLS_SETUP_OK=0
  fi
fi

if [ "$TLS_SETUP_OK" -eq 1 ]; then
  info "HTTPS proxy is enabled. Access WebUI at: https://<device-ip>/"
  info "Caddy auto-config will detect all IPs on every boot."
else
  warn "TLS setup incomplete. WebUI remains available on: http://<device-ip>:8080"
fi
set -e
else
  info "Skipped boot-time systemd services (launch interactively with: python3 $INSTALL_DIR/run_hdmi.py)"
fi

# ───── 6 ▸ final health‑check ────────────────────────────────
step "Running post install checks …"

# 6‑a SPI / framebuffer device nodes
if [[ "$DISPLAY_TYPE" == "HDMI" ]]; then
  info "HDMI display mode configured (launch with: python3 $INSTALL_DIR/run_hdmi.py)"
elif [[ "$DISPLAY_TYPE" == "CARDPUTER_320" ]]; then
  if [[ -e /dev/fb0 ]]; then
    info "Framebuffer found: /dev/fb0 (CardputerZero)"
  else
    warn "Framebuffer /dev/fb0 NOT found – display may not work"
  fi
else
  if ls /dev/spidev* 2>/dev/null | grep -q spidev0.0; then
    info "SPI device found: $(ls /dev/spidev* | xargs)"
  else
    warn "SPI device NOT found – a reboot may still be required."
  fi
fi

# 6‑b WiFi attack tools check
if cmd aireplay-ng && cmd airodump-ng && cmd airmon-ng; then
  info "WiFi attack tools found: aircrack-ng suite installed"
else
  warn "WiFi attack tools missing - check aircrack-ng installation"
fi

# 6‑c USB WiFi dongle detection
if cmd lsusb && lsusb 2>/dev/null | grep -q -i "realtek\|ralink\|atheros\|broadcom"; then
  info "USB WiFi dongles detected: $(lsusb 2>/dev/null | grep -i 'realtek\|ralink\|atheros\|broadcom' | wc -l) devices"
else
  warn "No USB WiFi dongles detected - WiFi attacks require external dongle"
fi

# 6‑c2 Media, Audio, Bluetooth & Headless Kiosk tools check
for _mbin in ffmpeg ffprobe yt-dlp mpv bluetoothctl; do
  if cmd "$_mbin"; then
    if [[ "$_mbin" == "yt-dlp" ]]; then
      info "Tool ready: yt-dlp ($(yt-dlp --version 2>/dev/null || echo installed))"
    else
      info "Tool ready: $_mbin"
    fi
  else
    warn "Optional media/BT tool missing: $_mbin"
  fi
done
for _abin in pactl wpctl amixer aplay; do
  if cmd "$_abin"; then
    info "Audio tool ready: $_abin"
  else
    warn "Optional audio tool missing: $_abin"
  fi
done
if [[ "$DISPLAY_TYPE" == "HDMI" ]]; then
  if cmd systemctl && (systemctl is-active --quiet bluealsa.service 2>/dev/null || systemctl is-active --quiet bluez-alsa.service 2>/dev/null); then
    warn "bluealsa is still active and may block PipeWire Bluetooth A2DP Stereo! Run Audio -> Tools -> Fix BT A2DP Stereo."
  else
    info "Bluetooth A2DP Stereo check OK (no conflicting bluealsa daemon active)"
  fi
  if cmd cage || cmd xinit; then
    info "Standalone GUI app kiosk ready ($(cmd cage && echo cage || echo xinit))"
  else
    warn "No standalone app kiosk found (install: cage xwayland seatd)"
  fi
  if [[ -e /dev/uinput ]]; then
    info "Virtual keyboard-mouse device ready: /dev/uinput"
  else
    warn "/dev/uinput not present yet (run 'sudo modprobe uinput' for Ctrl+Alt+M mouse mode)"
  fi
fi

# 6‑d python imports
RJ_BASE_DIR="$INSTALL_DIR" RJ_DISPLAY_TYPE="$DISPLAY_TYPE" PYTHONPATH="$INSTALL_DIR" python3 - <<'PY' || fail "Python dependency test failed"
import importlib, os, sys
dtype = os.environ.get("RJ_DISPLAY_TYPE", "ST7735_128")
if dtype == "HDMI":
    required = [
        "PIL", "numpy", "requests", "RPi.GPIO", "rj_env", "rj_theme", "rj_keyboard",
        "rj_bluetooth", "rj_audio", "rj_app_launcher", "rj_kbd_mouse",
        "rj_terminal", "rj_ytmusic", "rj_guide", "rj_tool_catalog",
    ]
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        required.append("evdev")
    optional = ["evdev", "scapy", "netifaces", "pyudev", "serial", "qrcode", "bluepy", "bleak"]
elif dtype == "CARDPUTER_320":
    required = [
        "PIL", "numpy", "requests", "evdev", "RPi.GPIO", "rj_env", "rj_theme",
        "rj_keyboard", "rj_audio", "rj_terminal", "rj_ytmusic", "rj_guide", "rj_tool_catalog",
    ]
    optional = ["scapy", "netifaces", "pyudev", "serial", "qrcode", "bluepy", "bleak"]
else:
    required = ["PIL", "requests", "RPi.GPIO", "spidev", "rj_env", "rj_theme", "rj_audio", "rj_guide", "rj_tool_catalog"]
    optional = ["scapy", "netifaces", "pyudev", "serial", "smbus2", "qrcode", "bluepy", "bleak"]

ok = True
seen = set()
for mod in required:
    seen.add(mod)
    try:
        loaded = importlib.import_module(mod)
        if mod == "RPi.GPIO" and dtype in ("HDMI", "CARDPUTER_320"):
            loaded.setmode(loaded.BCM)
            loaded.setup(16, loaded.IN, pull_up_down=loaded.PUD_UP)
            _ = loaded.input(16)
        print("[OK]", mod)
    except Exception as e:
        print("[FAIL]", mod, e)
        ok = False
for mod in optional:
    if mod in seen:
        continue
    try:
        importlib.import_module(mod)
        print("[OK]", mod)
    except Exception:
        print("[SKIP]", mod, "(optional payload dependency)")
if not ok:
    sys.exit(1)
print("[OK] All core Python modules available")
PY

# 6‑e WiFi integration test
RJ_BASE_DIR="$INSTALL_DIR" PYTHONPATH="$INSTALL_DIR" python3 - "$INSTALL_DIR" <<'WIFI_TEST' || warn "WiFi integration test failed - check wifi/ folder"
import os, sys
base = sys.argv[1]
sys.path.insert(0, base)
sys.path.append(os.path.join(base, "wifi"))
try:
    from wifi.raspyjack_integration import get_available_interfaces
    interfaces = get_available_interfaces()
    print(f"[OK] WiFi integration working - found {len(interfaces)} interfaces")
except Exception as e:
    print(f"[WARN] WiFi integration test failed: {e}")
    sys.exit(1)
WIFI_TEST

# 7 ▸ set permissions for binaries
step "Setting executable permissions for binaries in bin/ …"
if [[ -d "$INSTALL_DIR/bin" ]]; then
  shopt -s nullglob
  _bins=("$INSTALL_DIR/bin"/*)
  shopt -u nullglob
  if ((${#_bins[@]})); then
    "${_write_conf_runner[@]}" chmod +x "${_bins[@]}"
    info "Permissions set for files in $INSTALL_DIR/bin/"
  fi
fi

step "Installation finished successfully!"
if [[ "$DISPLAY_TYPE" == "HDMI" && $INSTALL_SERVICES -eq 0 ]]; then
  info "🖥️  Launch EmberJack HDMI UI:  python3 $INSTALL_DIR/run_hdmi.py --no-root"
  info "🔐 Launch with pentest perms:  sudo -E python3 $INSTALL_DIR/run_hdmi.py"
else
  info "⚠️  Reboot is recommended to ensure overlays & services start cleanly."
fi
info "📡 For WiFi attacks: Plug in USB WiFi dongle and run payloads/interception/deauth.py"
