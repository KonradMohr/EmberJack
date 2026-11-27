#!/usr/bin/env bash
# EmberJack / RaspyJack — System-Level WiFi Stack Repair & Broadcom Hardening
# ---------------------------------------------------------------------------
# Fixes slow boot ("Completed socket interaction for boot stage" / cloud-init)
# AND intermittent WiFi / nmtui connection failures ("must keep retrying until
# it works") on Raspberry Pi OS (Bookworm / Trixie) and Linux systems:
#   1. Stops competing daemons (hostapd, dnsmasq, dhcpcd, wpa_supplicant@wlan*,
#      systemd-networkd on Pi, rogue non-D-Bus wpa_supplicant -i wlan*, stale
#      wlan*mon monitor ifaces).
#   2. Disables Raspberry Pi OS Trixie cloud-init boot stalls & network resets
#      (/etc/cloud/cloud-init.disabled + 99-disable-network-config.cfg),
#      disables blocking wait-online services, removes 0-byte corrupted Netplan
#      YAMLs, and backs up legacy /etc/wpa_supplicant/wpa_supplicant.conf.
#   3. Configures Broadcom brcmfmac driver (roamoff=1 feature_disable=0x82000)
#      to stop firmware roaming drops and WPA2/WPA3 SAE/FWSUP handshake timeouts.
#   4. Hardens NetworkManager (/etc/NetworkManager/conf.d/99-wifi-attacks.conf):
#      wifi.powersave=2, wifi.scan-rand-mac-address=no, permanent MAC, infinite
#      autoconnect retries, and clears wifi.bgscan on all saved WiFi profiles.
#   5. Unblocks rfkill, disables live iw power_save, sets regulatory domain if
#      provided (--country XX) or recovered from legacy config, stops stuck BT
#      discovery scans, and cleanly restarts wpa_supplicant & NetworkManager.
#
# Usage:
#   sudo bash scripts/fix_wifi.sh
#   sudo bash scripts/fix_wifi.sh --country US
#   sudo bash scripts/fix_wifi.sh --reload-driver   # reload brcmfmac live (drops SSH!)
set -euo pipefail

step() { printf "\e[1;34m[STEP]\e[0m %s\n" "$*"; }
info() { printf "\e[1;32m[INFO]\e[0m %s\n" "$*"; }
warn() { printf "\e[1;33m[WARN]\e[0m %s\n" "$*"; }
cmd()  { command -v "$1" >/dev/null 2>&1; }

as_root() {
  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

COUNTRY=""
RELOAD_DRIVER=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --country)
      COUNTRY="${2:-}"
      shift 2
      ;;
    --country=*)
      COUNTRY="${1#*=}"
      shift
      ;;
    --reload-driver)
      RELOAD_DRIVER=1
      shift
      ;;
    -h|--help)
      sed -n '2,23p' "$0"
      exit 0
      ;;
    *)
      shift
      ;;
  esac
done

REBOOT_NEEDED=0
NM_ACTIVE=0
if cmd systemctl && (systemctl is-active --quiet NetworkManager 2>/dev/null || systemctl is-enabled --quiet NetworkManager 2>/dev/null); then
  NM_ACTIVE=1
fi

step "1/7 Checking rfkill, stuck Bluetooth scans & stale monitor interfaces …"
if cmd rfkill; then
  as_root rfkill unblock wifi 2>/dev/null || true
  info "rfkill: WiFi unblocked"
fi

# Stop any stuck Bluetooth discovery scan (shares 2.4GHz antenna on Raspberry Pi)
if cmd bluetoothctl; then
  printf 'scan off\nquit\n' | timeout 3 bluetoothctl >/dev/null 2>&1 || true
fi

# Remove stale monitor interfaces (wlan0mon / wlan1mon / etc.)
for mon in /sys/class/net/wlan*mon; do
  [[ -e "$mon" ]] || continue
  mon_name="$(basename "$mon")"
  warn "Removing stale monitor interface: $mon_name"
  if cmd airmon-ng; then
    as_root airmon-ng stop "$mon_name" >/dev/null 2>&1 || true
  fi
  if [[ -e "/sys/class/net/$mon_name" ]] && cmd iw; then
    as_root iw dev "$mon_name" del 2>/dev/null || true
  fi
done

# Ensure every physical wlan* interface is back in managed mode (not monitor)
if cmd iw; then
  for wdev in /sys/class/net/wlan*; do
    [[ -e "$wdev/wireless" ]] || continue
    wname="$(basename "$wdev")"
    wtype="$(iw dev "$wname" info 2>/dev/null | awk '/type /{print $2; exit}' || true)"
    if [[ "$wtype" == "monitor" ]]; then
      warn "Switching $wname back from monitor mode to managed mode"
      as_root ip link set "$wname" down 2>/dev/null || true
      as_root iw dev "$wname" set type managed 2>/dev/null || true
      as_root ip link set "$wname" up 2>/dev/null || true
    fi
  done
fi

step "2/7 Stopping competing network daemons, cloud-init stalls & rogue wpa_supplicant …"
IS_RPI=0
if [[ -d /sys/module/brcmfmac ]] || grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null; then
  IS_RPI=1
fi

if cmd systemctl; then
  for svc in hostapd dnsmasq; do
    if systemctl is-active --quiet "$svc" 2>/dev/null || systemctl is-enabled --quiet "$svc" 2>/dev/null; then
      as_root systemctl disable --now "$svc" 2>/dev/null || true
      info "Disabled competing service: $svc"
    fi
  done
  # Stop boot-blocking wait-online services (prevents 30-120s UI boot stalls when WiFi is slow/offline)
  for wsvc in NetworkManager-wait-online.service systemd-networkd-wait-online.service; do
    if systemctl is-active --quiet "$wsvc" 2>/dev/null || systemctl is-enabled --quiet "$wsvc" 2>/dev/null; then
      as_root systemctl disable --now "$wsvc" 2>/dev/null || true
      info "Disabled boot-blocking service: $wsvc"
    fi
  done
  if [[ $NM_ACTIVE -eq 1 ]]; then
    for svc in dhcpcd wpa_supplicant@wlan0 wpa_supplicant@wlan1; do
      if systemctl is-active --quiet "$svc" 2>/dev/null || systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        as_root systemctl disable --now "$svc" 2>/dev/null || true
        info "Disabled service competing with NetworkManager: $svc"
      fi
    done
    # On Raspberry Pi OS (Bookworm/Trixie), systemd-networkd must not fight NetworkManager over wlan0
    if [[ $IS_RPI -eq 1 ]]; then
      for nsvc in systemd-networkd.service systemd-networkd.socket; do
        if systemctl is-active --quiet "$nsvc" 2>/dev/null || systemctl is-enabled --quiet "$nsvc" 2>/dev/null; then
          as_root systemctl disable --now "$nsvc" 2>/dev/null || true
          info "Disabled competing systemd-networkd unit on Pi: $nsvc"
        fi
      done
    fi
  fi
fi

# Disable Raspberry Pi OS Trixie cloud-init post-setup boot stalls ("Completed socket interaction for boot stage")
# and stop cloud-init from overwriting NetworkManager / nmtui configs across reboots.
if [[ -d /etc/cloud ]] || cmd cloud-init; then
  as_root mkdir -p /etc/cloud /etc/cloud/cloud.cfg.d
  if [[ ! -f /etc/cloud/cloud-init.disabled ]]; then
    as_root touch /etc/cloud/cloud-init.disabled
    info "Created /etc/cloud/cloud-init.disabled (eliminates cloud-init 'socket interaction' boot stalls)"
    REBOOT_NEEDED=1
  else
    info "/etc/cloud/cloud-init.disabled already present"
  fi
  if [[ ! -f /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg ]]; then
    printf 'network: {config: disabled}\n' | as_root tee /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg >/dev/null
    info "Disabled cloud-init network config resets (99-disable-network-config.cfg)"
  fi
  if cmd systemctl; then
    for csvc in cloud-init-local.service cloud-init-main.service cloud-init-network.service cloud-init.service cloud-config.service cloud-final.service; do
      if systemctl is-enabled --quiet "$csvc" 2>/dev/null || systemctl is-active --quiet "$csvc" 2>/dev/null; then
        as_root systemctl disable "$csvc" 2>/dev/null || true
      fi
    done
  fi
fi

# Clean up 0-byte corrupted Netplan YAMLs (known Raspberry Pi OS Trixie 90-NM-*.yaml truncation bug)
# without touching any non-empty Netplan file holding existing WiFi credentials.
if [[ -d /etc/netplan ]]; then
  for npf in /etc/netplan/*.yaml /etc/netplan/*.yml; do
    [[ -e "$npf" ]] || continue
    if [[ ! -s "$npf" ]]; then
      as_root rm -f "$npf" 2>/dev/null || true
      warn "Removed corrupted 0-byte Netplan file: $npf"
    elif [[ $NM_ACTIVE -eq 1 ]] && grep -qE '^[[:space:]]*renderer:[[:space:]]*networkd\b' "$npf" 2>/dev/null; then
      warn "$npf uses 'renderer: networkd' while NetworkManager is active — this can fight nmtui!"
    fi
  done
fi

# Kill ONLY rogue per-interface wpa_supplicant processes (NEVER NetworkManager's D-Bus `wpa_supplicant -u`!)
if [[ $NM_ACTIVE -eq 1 ]] && cmd pgrep; then
  while read -r pid args; do
    [[ -n "${pid:-}" ]] || continue
    if [[ "$args" == *"-i wlan"* || "$args" == *"-iwlan"* ]] && [[ "$args" != *" -u"* && "$args" != *"-u "* ]]; then
      warn "Killing rogue per-interface wpa_supplicant (PID $pid): $args"
      as_root kill "$pid" 2>/dev/null || true
    fi
  done < <(pgrep -af '^wpa_supplicant' 2>/dev/null || true)
fi

# Back up conflicting legacy wpa_supplicant.conf when NetworkManager owns WiFi
if [[ $NM_ACTIVE -eq 1 && -f /etc/wpa_supplicant/wpa_supplicant.conf ]]; then
  if [[ -z "$COUNTRY" ]]; then
    COUNTRY="$(awk -F= '/^[[:space:]]*country[[:space:]]*=/ {gsub(/[[:space:]\r]/,"",$2); print toupper($2); exit}' /etc/wpa_supplicant/wpa_supplicant.conf 2>/dev/null || true)"
  fi
  as_root mv -f /etc/wpa_supplicant/wpa_supplicant.conf /etc/wpa_supplicant/wpa_supplicant.conf.bak
  info "Backed up legacy /etc/wpa_supplicant/wpa_supplicant.conf -> .bak (prevents NM conflicts)"
fi

# Warn if /etc/network/interfaces still configures wlan0 directly
if [[ $NM_ACTIVE -eq 1 && -f /etc/network/interfaces ]] && grep -qE '^[[:space:]]*(auto|allow-hotplug|iface)[[:space:]]+wlan[0-9]' /etc/network/interfaces 2>/dev/null; then
  warn "/etc/network/interfaces contains a wlan* entry — this can fight NetworkManager!"
fi

# Remove redundant udev rename rule if systemd .link file already pins wlan0
if [[ -d /run/systemd/system && -f /etc/systemd/network/10-onboard-wifi.link && -f /etc/udev/rules.d/70-raspyjack-wifi.rules ]]; then
  as_root rm -f /etc/udev/rules.d/70-raspyjack-wifi.rules 2>/dev/null || true
  as_root udevadm control --reload-rules 2>/dev/null || true
  info "Removed redundant 70-raspyjack-wifi.rules"
fi

step "3/7 Hardening Broadcom brcmfmac driver (Raspberry Pi WiFi stability) …"
HAS_BRCM=0
if [[ -d /sys/module/brcmfmac ]] || grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null; then
  HAS_BRCM=1
fi
if [[ $HAS_BRCM -eq 1 && -d /etc/modprobe.d ]]; then
  BRCM_CONF="/etc/modprobe.d/99-emberjack-brcmfmac.conf"
  WANT_BRCM="options brcmfmac roamoff=1 feature_disable=0x82000"
  if [[ ! -f "$BRCM_CONF" ]] || ! grep -qF "$WANT_BRCM" "$BRCM_CONF" 2>/dev/null; then
    as_root tee "$BRCM_CONF" >/dev/null <<'BRCM'
# EmberJack: fix Broadcom Raspberry Pi WiFi handshake timeouts, association retries & drops
# - roamoff=1: disables aggressive firmware roaming that breaks 4-way handshakes
# - feature_disable=0x82000: disables buggy firmware FWSUP+SAE offload so host wpa_supplicant handles WPA2/WPA3 cleanly
options brcmfmac roamoff=1 feature_disable=0x82000
BRCM
    info "Wrote $BRCM_CONF ($WANT_BRCM)"
    REBOOT_NEEDED=1
  else
    info "$BRCM_CONF already up to date"
  fi
  # Also append brcmfmac options to Raspberry Pi boot cmdline.txt (guarantees the kernel
  # applies them even when brcmfmac loads inside initramfs before /etc/modprobe.d is read!)
  for _cmdf in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    if [[ -f "$_cmdf" ]]; then
      _cmd_changed=0
      for _kopt in "brcmfmac.roamoff=1" "brcmfmac.feature_disable=0x82000"; do
        if ! grep -qw "$_kopt" "$_cmdf" 2>/dev/null; then
          as_root sed -i "1s|\$| $_kopt|" "$_cmdf"
          _cmd_changed=1
        fi
      done
      if [[ $_cmd_changed -eq 1 ]]; then
        info "Added brcmfmac stability flags to $_cmdf (applies cleanly inside initramfs)"
        REBOOT_NEEDED=1
      fi
      break
    fi
  done
  # Check if currently loaded brcmfmac module already has roamoff=1 & feature_disable=0x82000 (532480)
  if [[ -r /sys/module/brcmfmac/parameters/roamoff ]]; then
    CUR_ROAM="$(cat /sys/module/brcmfmac/parameters/roamoff 2>/dev/null || echo 0)"
    if [[ "$CUR_ROAM" != "1" ]]; then
      REBOOT_NEEDED=1
    fi
  fi
  if [[ -r /sys/module/brcmfmac/parameters/feature_disable ]]; then
    CUR_FDIS="$(cat /sys/module/brcmfmac/parameters/feature_disable 2>/dev/null || echo 0)"
    if [[ "$CUR_FDIS" != "532480" && "$CUR_FDIS" != "0x82000" && "$CUR_FDIS" != "0x00082000" ]]; then
      REBOOT_NEEDED=1
    fi
  fi
  if [[ $RELOAD_DRIVER -eq 1 && -d /sys/module/brcmfmac ]]; then
    warn "Reloading brcmfmac kernel module live …"
    as_root modprobe -r brcmfmac_wcc brcmfmac 2>/dev/null || as_root modprobe -r brcmfmac 2>/dev/null || true
    sleep 1
    as_root modprobe brcmfmac 2>/dev/null || true
    sleep 2
    REBOOT_NEEDED=0
    info "brcmfmac reloaded live with new stability options"
  fi
else
  info "Non-Broadcom WiFi host — skipping brcmfmac modprobe config"
fi

step "4/7 Configuring NetworkManager for rock-solid WiFi …"
if [[ $NM_ACTIVE -eq 1 ]]; then
  as_root mkdir -p /etc/NetworkManager/conf.d
  # Neutralize Ubuntu/Debian default-wifi-powersave-on.conf if it forces powersave=3
  if [[ -f /etc/NetworkManager/conf.d/default-wifi-powersave-on.conf ]] && grep -qE 'wifi\.powersave[[:space:]]*=[[:space:]]*3' /etc/NetworkManager/conf.d/default-wifi-powersave-on.conf 2>/dev/null; then
    as_root sed -Ei 's/wifi\.powersave[[:space:]]*=[[:space:]]*3/wifi.powersave = 2/' /etc/NetworkManager/conf.d/default-wifi-powersave-on.conf
    info "Updated default-wifi-powersave-on.conf -> wifi.powersave = 2"
  fi

  as_root tee /etc/NetworkManager/conf.d/99-wifi-attacks.conf >/dev/null <<'NM_CONF'
[keyfile]
unmanaged-devices=interface-name:wlan0mon;interface-name:wlan1mon;interface-name:wlan2mon

[connection]
wifi.powersave = 2
wifi.cloned-mac-address = permanent
connection.autoconnect-retries = 0

[device]
wifi.scan-rand-mac-address = no
NM_CONF
  info "Updated /etc/NetworkManager/conf.d/99-wifi-attacks.conf"
fi

step "5/7 Checking WiFi regulatory domain & disabling live power-save …"
# Auto-recover country code from boot cmdline if not specified
if [[ -z "$COUNTRY" ]]; then
  for _cmdf in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    if [[ -r "$_cmdf" ]]; then
      COUNTRY="$(grep -oE 'cfg80211\.ieee80211_regdom=[A-Za-z]{2}' "$_cmdf" 2>/dev/null | head -n1 | cut -d= -f2 | tr '[:lower:]' '[:upper:]' || true)"
      [[ -n "$COUNTRY" ]] && break
    fi
  done
fi

if [[ -n "$COUNTRY" && "$COUNTRY" =~ ^[A-Z]{2}$ && "$COUNTRY" != "00" ]]; then
  if cmd iw; then
    as_root iw reg set "$COUNTRY" 2>/dev/null || true
  fi
  if cmd raspi-config; then
    as_root raspi-config nonint do_wifi_country "$COUNTRY" 2>/dev/null || true
  elif [[ -d /etc/modprobe.d ]]; then
    echo "options cfg80211 ieee80211_regdom=$COUNTRY" | as_root tee /etc/modprobe.d/99-emberjack-regdom.conf >/dev/null
  fi
  info "WiFi regulatory country set to: $COUNTRY"
elif cmd iw; then
  CUR_REG="$(iw reg get 2>/dev/null | awk '/^country /{print $2; exit}' | tr -d ':' || true)"
  if [[ "$CUR_REG" == "00" || -z "$CUR_REG" ]]; then
    warn "WiFi regulatory country is UNSET (country 00)! 5GHz & high channels may fail to connect."
    warn "Fix with: sudo bash scripts/fix_wifi.sh --country US   (replace US with your 2-letter country code)"
  else
    info "WiFi regulatory country OK: $CUR_REG"
  fi
fi

if cmd iw; then
  for wdev in /sys/class/net/wlan*; do
    [[ -e "$wdev/wireless" ]] || continue
    wname="$(basename "$wdev")"
    as_root ip link set "$wname" up 2>/dev/null || true
    as_root iw dev "$wname" set power_save off 2>/dev/null || true
    info "Disabled live power_save on $wname"
  done
fi

step "6/7 Hardening saved NetworkManager WiFi profiles & restarting NM …"
if [[ $NM_ACTIVE -eq 1 ]]; then
  if cmd nmcli; then
    as_root nmcli radio wifi on 2>/dev/null || true
    for wdev in /sys/class/net/wlan*; do
      [[ -e "$wdev/wireless" ]] || continue
      wname="$(basename "$wdev")"
      as_root nmcli device set "$wname" managed yes 2>/dev/null || true
    done
    # Harden every saved WiFi profile: disable aggressive 30s bgscan, disable powersave,
    # permanent MAC, infinite autoconnect retries, optional PMF (fixes WPA2/WPA3 transition routers)
    while IFS=: read -r cname ctype; do
      [[ -n "${cname:-}" ]] || continue
      if [[ "$ctype" == "802-11-wireless" || "$ctype" == "wifi" ]]; then
        as_root nmcli connection modify "$cname" \
          802-11-wireless.powersave 2 \
          802-11-wireless.bgscan "" \
          802-11-wireless.cloned-mac-address permanent \
          connection.autoconnect-retries 0 2>/dev/null || true
        # On WPA/SAE secured profiles: set PMF to 0 (default = optional for WPA2, required for WPA3-SAE;
        # NEVER 1 which means DISABLE and breaks WPA3/PMF handshakes!) and psk-flags=0 (stored in file,
        # never waiting on a nonexistent headless desktop keyring agent -> fixes "Secrets were required").
        kmgmt="$(nmcli -g 802-11-wireless-security.key-mgmt connection show "$cname" 2>/dev/null || true)"
        if [[ -n "$kmgmt" && "$kmgmt" != "--" ]]; then
          as_root nmcli connection modify "$cname" \
            802-11-wireless-security.pmf 0 \
            802-11-wireless-security.psk-flags 0 2>/dev/null || true
        fi
        info "Hardened saved WiFi profile: $cname"
      fi
    done < <(nmcli -t -f NAME,TYPE connection show 2>/dev/null || true)
  fi

  as_root systemctl restart wpa_supplicant.service 2>/dev/null || true
  sleep 1
  as_root systemctl restart NetworkManager.service 2>/dev/null || true
  sleep 2
  info "Restarted wpa_supplicant & NetworkManager cleanly"
fi

step "7/7 WiFi Diagnostics Summary"
echo "------------------------------------------------------------"
for wdev in /sys/class/net/wlan*; do
  [[ -e "$wdev/wireless" ]] || continue
  wname="$(basename "$wdev")"
  drv="$(basename "$(readlink -f "$wdev/device/driver" 2>/dev/null)" 2>/dev/null || echo unknown)"
  ps_st="$(iw dev "$wname" get power_save 2>/dev/null || echo unknown)"
  ip4="$(ip -4 -o addr show "$wname" 2>/dev/null | awk '{print $4}' | head -n1 || true)"
  echo "  Adapter : $wname  (driver=$drv, ip=${ip4:-none}, $ps_st)"
done
if cmd iw; then
  echo "  RegDom  : $(iw reg get 2>/dev/null | awk '/^country /{print $2; exit}' || echo unknown)"
fi
if cmd rfkill; then
  echo "  rfkill  : $(rfkill list wifi 2>/dev/null | tr '\n' ' ' | sed 's/  */ /g')"
fi
if [[ -r /sys/module/brcmfmac/parameters/roamoff ]]; then
  echo "  brcmfmac: roamoff=$(cat /sys/module/brcmfmac/parameters/roamoff 2>/dev/null) feature_disable=$(cat /sys/module/brcmfmac/parameters/feature_disable 2>/dev/null || echo n/a)"
fi
if [[ -d /etc/cloud ]] || cmd cloud-init; then
  if [[ -f /etc/cloud/cloud-init.disabled ]]; then
    echo "  cloud-init: DISABLED (/etc/cloud/cloud-init.disabled)"
  else
    echo "  cloud-init: ACTIVE (may cause 'socket interaction' boot stalls!)"
  fi
fi
if cmd systemctl; then
  _nm_wo="$(systemctl is-enabled NetworkManager-wait-online.service 2>/dev/null || echo not-found)"
  echo "  wait-online: NetworkManager-wait-online=$_nm_wo"
fi
if [[ -d /etc/netplan ]]; then
  _np_list="$(ls -lh /etc/netplan/*.yaml /etc/netplan/*.yml 2>/dev/null | awk '{print $NF "(" $5 ")"}' | tr '\n' ' ' || true)"
  [[ -n "${_np_list:-}" ]] && echo "  netplan : $_np_list"
fi
echo "------------------------------------------------------------"
if [[ $REBOOT_NEEDED -eq 1 ]]; then
  warn "IMPORTANT: Reboot once ('sudo reboot') so cloud-init stays disabled and brcmfmac loads with roamoff=1 feature_disable=0x82000!"
else
  info "All WiFi & fast-boot fixes are active now!"
fi
