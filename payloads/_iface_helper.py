"""
Shared network interface detection and LCD selection helper.

Usage in a payload:
    from payloads._iface_helper import select_interface

    # WiFi only (for deauth, evil_twin, etc.)
    iface = select_interface(lcd, font, pins, gpio, iface_type="wifi")

    # Ethernet only (for arp_mitm, vlan_hopper, etc.)
    iface = select_interface(lcd, font, pins, gpio, iface_type="eth")

    # Any network interface (for mixed payloads)
    iface = select_interface(lcd, font, pins, gpio, iface_type="any")

    # Returns iface name (str) or None if user cancelled / none found.
    # If only 1 interface matches, auto-selects it (no menu shown).
"""

import os
import subprocess
import time

from payloads._input_helper import (
    flush_input,
    format_footer_hint,
    get_button,
    is_back,
    is_confirm,
    poll_ui_input,
    text_input_mode,
)

try:
    from PIL import Image, ImageDraw
except Exception:
    Image = None
    ImageDraw = None

try:
    import rj_theme
except Exception:
    rj_theme = None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _get_driver(iface):
    """Return kernel driver name for an interface."""
    try:
        return os.path.basename(
            os.path.realpath(f"/sys/class/net/{iface}/device/driver")
        )
    except Exception:
        return ""


def _is_onboard_wifi(iface):
    """True for the onboard RPi WiFi (SDIO / brcmfmac)."""
    try:
        devpath = os.path.realpath(f"/sys/class/net/{iface}/device")
        if "mmc" in devpath:
            return True
    except Exception:
        pass
    return _get_driver(iface) == "brcmfmac"


# Drivers known to support monitor+injection but not reporting it via nl80211
_KNOWN_MONITOR_DRIVERS = {
    "rtl88XXau", "rtl8812au", "rtl8821au", "rtl88x2bu",
    "rtl8188eus", "rtl8187", "rt2800usb", "ath9k_htc",
    "mt76x2u", "mt76x0u", "mt7921u", "rtl8814au",
    "rtl8192cu", "mt7601u",
}


def _supports_mode(iface, mode="AP"):
    """Check if a WiFi interface supports a given mode (AP, monitor, etc.).

    For monitor mode: first checks iw phy info, then falls back to
    driver name matching for known-good drivers that don't report
    capabilities correctly via nl80211 (common with out-of-tree Realtek).
    Works with Nexmon-patched brcmfmac (onboard Pi WiFi with injection).
    """
    try:
        phy_link = os.path.realpath(f"/sys/class/net/{iface}/phy80211")
        phy_name = os.path.basename(phy_link)
        r = subprocess.run(
            ["iw", "phy", phy_name, "info"],
            capture_output=True, text=True, timeout=5,
        )
        if f"* {mode}" in r.stdout:
            return True
    except Exception:
        pass

    # Fallback for monitor mode: check driver name against known-good list
    if mode == "monitor":
        driver = _get_driver(iface)
        if driver in _KNOWN_MONITOR_DRIVERS:
            return True

    return False


def supports_monitor(iface):
    """Public helper: True if *iface* supports monitor mode.

    Works for USB dongles AND onboard WiFi with Nexmon.
    """
    return _supports_mode(iface, "monitor")


def _get_ip(iface):
    """Return first IPv4 address of an interface, or ''."""
    try:
        r = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", iface],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.strip().split("\n"):
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "inet" and i + 1 < len(parts):
                    return parts[i + 1].split("/")[0]
    except Exception:
        pass
    return ""


def _is_up(iface):
    """Return True if the interface has operstate 'up'."""
    try:
        with open(f"/sys/class/net/{iface}/operstate", "r") as f:
            return f.read().strip().lower() == "up"
    except Exception:
        return False


def list_interfaces(iface_type="any"):
    """
    Return list of interface info dicts matching the requested type.

    iface_type:
        "wifi"  -- only wlan* interfaces
        "eth"   -- only eth*, enp*, ens*, usb* (non-wireless)
        "any"   -- all non-loopback, non-virtual interfaces

    Each dict: {name, driver, is_onboard, is_wifi, is_up, ip, supports_ap, supports_monitor}
    Sorted: USB WiFi first, then onboard WiFi, then Ethernet.
    """
    result = []
    try:
        all_ifaces = sorted(os.listdir("/sys/class/net"))
    except Exception:
        return result

    for name in all_ifaces:
        if name == "lo":
            continue

        is_wifi = os.path.isdir(f"/sys/class/net/{name}/wireless")
        is_virtual = os.path.islink(f"/sys/class/net/{name}/device") is False
        # Skip docker/veth/br/tailscale for "any" mode
        if name.startswith(("veth", "br-", "docker", "virbr")):
            continue

        if iface_type == "wifi" and not is_wifi:
            continue
        if iface_type == "eth" and is_wifi:
            continue

        driver = _get_driver(name)
        onboard = _is_onboard_wifi(name) if is_wifi else False
        ip = _get_ip(name)
        up = _is_up(name)

        info = {
            "name": name,
            "driver": driver,
            "is_onboard": onboard,
            "is_wifi": is_wifi,
            "is_up": up,
            "ip": ip,
            "supports_ap": _supports_mode(name, "AP") if is_wifi else False,
            "supports_monitor": _supports_mode(name, "monitor") if is_wifi else False,
        }
        result.append(info)

    # Sort: USB WiFi first, onboard WiFi next, then eth by name
    def _sort_key(i):
        if i["is_wifi"]:
            return (0 if not i["is_onboard"] else 1, i["name"])
        return (2, i["name"])

    return sorted(result, key=_sort_key)


# ---------------------------------------------------------------------------
# Native Resolution-Adaptive Obsidian Ember Interface Picker Renderers
# ---------------------------------------------------------------------------

def render_interface_picker(
    width: int,
    height: int,
    ifaces: list,
    sel: int = 0,
    title: str = "SELECT INTERFACE",
    is_bt: bool = False,
    fonts: dict = None,
):
    """Pure PIL Obsidian Ember renderer for WiFi/Ethernet/Bluetooth interface selection."""
    if Image is None or ImageDraw is None or rj_theme is None:
        return None
    ET = rj_theme.EMBER_THEME
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = f["wide"]
    large = f["large"]
    mid = (not wide) and width >= 200

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)
    hdr_h = rj_theme.draw_app_header(
        d, width, title, f,
        badge=f"{len(ifaces)}",
        right_text=("BLUETOOTH" if is_bt else "NETWORK"),
        hdr_h=(30 if large else (22 if wide else (22 if mid else 18))),
    )
    hint = format_footer_hint(
        width, height,
        hat_hint="OK:Sel K3:Bk",
        hat_240_hint="OK:Select K3:Back",
        cardputer_hint="Enter:Select Esc:Cancel",
        hdmi_hint="Up/Down:Pick  Enter:Select  Esc:Cancel",
    )
    ftr_h = rj_theme.draw_app_footer(
        d, width, height, hint, f,
        right_text=f"{min(sel + 1, max(1, len(ifaces)))}/{max(1, len(ifaces))}",
        ftr_h=(22 if large else (16 if wide else (16 if mid else 13))),
    )

    y0 = hdr_h + (8 if large else (4 if (wide or mid) else 2))
    y1 = height - ftr_h - (6 if large else (3 if (wide or mid) else 2))
    if not ifaces:
        rj_theme.draw_status_card(d, width, height, "No Interfaces Found", "Plug in adapter & retry", kind="err", fonts=f)
        return img

    split = width >= 480
    list_x0 = 10 if large else (6 if wide else (5 if mid else 3))
    list_x1 = int(width * 0.62) - 4 if split else (width - (10 if (wide or mid) else 5))
    row_h = 44 if large else (26 if (wide or mid) else 18)
    vis = max(1, (y1 - y0) // row_h)
    scroll = max(0, min(sel - vis // 2, max(0, len(ifaces) - vis)))

    for slot in range(vis):
        idx = scroll + slot
        if idx >= len(ifaces):
            break
        ifc = ifaces[idx]
        is_sel = idx == sel
        ry0 = y0 + slot * row_h
        ry1 = ry0 + row_h - (5 if large else (3 if (wide or mid) else 2))
        rj_theme.draw_card(
            d, (list_x0, ry0, list_x1, ry1),
            fill=ET["select_bg"] if is_sel else ET["bg_surface"],
            outline=ET["border_accent"] if is_sel else ET["border_subtle"],
            radius=6 if wide else (4 if mid else 3),
            accent_left=ET["accent"] if is_sel else None,
            accent_width=3 if (wide or mid) else 2,
        )

        is_up = bool(ifc.get("is_up"))
        rx = list_x1 - (8 if wide else (5 if mid else 3))
        st_lbl = "UP" if is_up else "DOWN"
        st_fg = ET["ok"] if is_up else ET["danger"]
        st_bg = ET["ok_bg"] if is_up else ET["danger_bg"]
        sw = rj_theme.measure_text(f["badge"], st_lbl, d)[2] + (10 if wide else (8 if mid else 6))
        rj_theme.draw_pill(
            d, rx - sw, ry0 + (5 if large else 2), st_lbl, f["badge"],
            bg=st_bg, fg=st_fg, outline=None, pad_x=5 if wide else (4 if mid else 3), pad_y=1, radius=3,
        )
        rx -= sw + 5

        if is_bt:
            bus_lbl = str(ifc.get("bus") or "BT").upper()[:6]
            sub_line = f"{ifc.get('mac') or 'No MAC'}  •  {ifc.get('bt_version') or bus_lbl}"
            pills = [bus_lbl]
        else:
            if ifc.get("is_wifi"):
                pills = ["USB" if not ifc.get("is_onboard") else "RPi"]
                if ifc.get("supports_monitor"):
                    pills.append("MON")
                if ifc.get("supports_ap"):
                    pills.append("AP")
            else:
                pills = ["ETH"]
            drv = ifc.get("driver") or "kernel"
            ip_s = ifc.get("ip") or "no IP"
            sub_line = f"{drv}  •  {ip_s}"

        if wide or mid:
            for tag in reversed(pills):
                tw = rj_theme.measure_text(f["badge"], tag, d)[2] + 8
                if rx - tw < list_x0 + (85 if wide else 68):
                    break
                rj_theme.draw_pill(
                    d, rx - tw, ry0 + (5 if large else 2), tag, f["badge"],
                    bg=ET["bg_well"], fg=ET["accent_bright"] if is_sel else ET["text_muted"],
                    outline=ET["border_subtle"], pad_x=4, pad_y=1, radius=3,
                )
                rx -= tw + 4

        nx = list_x0 + (10 if wide else (7 if mid else 5))
        name_txt = rj_theme.truncate_text(d, str(ifc.get("name", "?")), max(32, rx - nx - 4), f["body_bold"] if (wide or mid) else f["body"])
        d.text((nx, ry0 + (5 if large else 2)), name_txt, font=f["body_bold"] if (wide or mid) else f["body"], fill=ET["text_bright"] if is_sel else ET["text_primary"])
        if large:
            d.text((nx, ry0 + 23), rj_theme.truncate_text(d, sub_line, list_x1 - nx - 8, f["sub"]), font=f["sub"], fill=ET["text_muted"])
        elif wide or mid:
            d.text((nx, ry0 + 13), rj_theme.truncate_text(d, sub_line, list_x1 - nx - 8, f["badge"]), font=f["badge"], fill=ET["text_muted"])

    if len(ifaces) > vis:
        rj_theme.draw_scrollbar(d, list_x1 + 2, y0, y1, scroll, vis, len(ifaces), width=4 if (wide or mid) else 2)

    if split and 0 <= sel < len(ifaces):
        cur = ifaces[sel]
        px0 = list_x1 + 10
        px1 = width - 10
        rj_theme.draw_card(d, (px0, y0, px1, y1), fill=ET["bg_surface"], outline=ET["border_subtle"], radius=7)
        pw = px1 - px0 - 20
        dy = y0 + 12
        d.text((px0 + 10, dy), rj_theme.truncate_text(d, str(cur.get("name", "?")), pw, f["title"]), font=f["title"], fill=ET["accent_bright"])
        dy += 24
        d.line([(px0 + 10, dy), (px1 - 10, dy)], fill=ET["border_subtle"], width=1)
        dy += 10
        if is_bt:
            rows = [
                ("State", "UP RUNNING" if cur.get("is_up") else "DOWN"),
                ("Bus", str(cur.get("bus") or "unknown")),
                ("MAC", str(cur.get("mac") or "N/A")),
                ("Version", str(cur.get("bt_version") or "N/A")),
            ]
        else:
            kind_s = ("USB WiFi" if not cur.get("is_onboard") else "Onboard RPi WiFi") if cur.get("is_wifi") else "Ethernet / Wired"
            rows = [
                ("Type", kind_s),
                ("State", "LINK UP" if cur.get("is_up") else "DOWN"),
                ("IPv4", str(cur.get("ip") or "Unassigned")),
                ("Driver", str(cur.get("driver") or "N/A")),
                ("Monitor", "Supported" if cur.get("supports_monitor") else "No"),
                ("AP Mode", "Supported" if cur.get("supports_ap") else "No"),
            ]
        for k_lbl, v_lbl in rows:
            if dy + 18 > y1 - 12:
                break
            d.text((px0 + 10, dy), f"{k_lbl}:", font=f["sub"], fill=ET["text_muted"])
            d.text((px0 + 72, dy), rj_theme.truncate_text(d, v_lbl, max(30, pw - 64), f["body"]), font=f["body"], fill=ET["text_primary"])
            dy += 22

    return img


def _poll_picker_action(pins, gpio):
    try:
        act, _ch = poll_ui_input(pins, gpio)
        if is_back(act):
            return "CANCEL"
        if is_confirm(act):
            return "SELECT"
        if act in ("UP", "PAGEUP"):
            return "UP"
        if act in ("DOWN", "PAGEDOWN"):
            return "DOWN"
        return None
    except Exception:
        btn = get_button(pins, gpio)
        if btn == "KEY3":
            return "CANCEL"
        if btn == "OK":
            return "SELECT"
        return btn


def select_interface(lcd, font, pins, gpio, iface_type="any", title=None,
                     require_monitor=False):
    """
    Detect interfaces and let the user pick one on LCD (Obsidian Ember adaptive UI).

    Returns the selected interface name (str) or None if cancelled/not found.
    If only one interface matches, auto-selects it without showing the menu.
    """
    ifaces = list_interfaces(iface_type)

    if require_monitor:
        ifaces = [i for i in ifaces if i.get("supports_monitor")]

    if not ifaces:
        if require_monitor:
            _show_message(lcd, font, "No monitor interface!", kind="err", sub="Plug in a monitor-mode USB WiFi dongle")
        else:
            _show_message(lcd, font, "No interface found!", kind="err", sub="Check WiFi/Ethernet adapter")
        return None

    # Auto-select if only one
    if len(ifaces) == 1:
        return ifaces[0]["name"]

    if title is None:
        titles = {"wifi": "SELECT WIFI", "eth": "SELECT ETHERNET", "any": "SELECT INTERFACE"}
        title = titles.get(iface_type, "SELECT INTERFACE")

    sel = 0
    WIDTH, HEIGHT = lcd.width, lcd.height
    fonts = rj_theme.load_app_fonts(WIDTH, HEIGHT) if rj_theme is not None else None
    dirty = True

    with text_input_mode():
        flush_input()
        while True:
            if dirty and Image is not None and rj_theme is not None:
                frame = render_interface_picker(WIDTH, HEIGHT, ifaces, sel=sel, title=title, is_bt=False, fonts=fonts)
                if frame is not None:
                    lcd.LCD_ShowImage(frame, 0, 0)
                dirty = False

            act = _poll_picker_action(pins, gpio)
            if act == "CANCEL":
                flush_input()
                return None
            elif act == "SELECT":
                flush_input()
                return ifaces[sel]["name"]
            elif act == "UP":
                sel = (sel - 1) % len(ifaces)
                dirty = True
            elif act == "DOWN":
                sel = (sel + 1) % len(ifaces)
                dirty = True
            time.sleep(0.04)


# ---------------------------------------------------------------------------
# Bluetooth interface detection + selection
# ---------------------------------------------------------------------------

def list_bt_interfaces():
    """Return list of Bluetooth HCI interfaces with info."""
    result = []
    bt_path = "/sys/class/bluetooth"
    if not os.path.isdir(bt_path):
        return result
    for name in sorted(os.listdir(bt_path)):
        if not name.startswith("hci"):
            continue
        info = {"name": name, "bus": "", "mac": "", "is_up": False, "bt_version": ""}
        try:
            devpath = os.path.realpath(os.path.join(bt_path, name, "device"))
            if "usb" in devpath:
                info["bus"] = "USB"
            elif "uart" in devpath or "serial" in devpath:
                info["bus"] = "onboard"
            else:
                info["bus"] = "other"
        except Exception:
            pass
        try:
            r = subprocess.run(["hciconfig", name], capture_output=True, text=True, timeout=5)
            out = r.stdout
            if "UP RUNNING" in out:
                info["is_up"] = True
            for line in out.split("\n"):
                if "BD Address:" in line:
                    info["mac"] = line.split("BD Address:")[1].strip().split()[0]
                if "HCI Version:" in line:
                    info["bt_version"] = line.split("HCI Version:")[1].strip().split("(")[0].strip()
        except Exception:
            pass
        result.append(info)
    return sorted(result, key=lambda x: (0 if x["bus"] == "USB" else 1, x["name"]))


def select_bt_interface(lcd, font, pins, gpio, title="SELECT BLUETOOTH"):
    """
    Detect BT interfaces and let the user pick one on LCD (Obsidian Ember adaptive UI).
    Returns hci name (e.g. 'hci0') or None. Auto-selects if only one found.
    """
    ifaces = list_bt_interfaces()

    if not ifaces:
        _show_message(lcd, font, "No BT adapter found!", kind="err", sub="Check Bluetooth hardware or USB dongle")
        return None

    if len(ifaces) == 1:
        if not ifaces[0]["is_up"]:
            subprocess.run(["sudo", "hciconfig", ifaces[0]["name"], "up"],
                           capture_output=True, timeout=5)
        return ifaces[0]["name"]

    sel = 0
    WIDTH, HEIGHT = lcd.width, lcd.height
    fonts = rj_theme.load_app_fonts(WIDTH, HEIGHT) if rj_theme is not None else None
    dirty = True

    with text_input_mode():
        flush_input()
        while True:
            if dirty and Image is not None and rj_theme is not None:
                frame = render_interface_picker(WIDTH, HEIGHT, ifaces, sel=sel, title=title, is_bt=True, fonts=fonts)
                if frame is not None:
                    lcd.LCD_ShowImage(frame, 0, 0)
                dirty = False

            act = _poll_picker_action(pins, gpio)
            if act == "CANCEL":
                flush_input()
                return None
            elif act == "SELECT":
                chosen = ifaces[sel]
                if not chosen["is_up"]:
                    subprocess.run(["sudo", "hciconfig", chosen["name"], "up"],
                                   capture_output=True, timeout=5)
                flush_input()
                return chosen["name"]
            elif act == "UP":
                sel = (sel - 1) % len(ifaces)
                dirty = True
            elif act == "DOWN":
                sel = (sel + 1) % len(ifaces)
                dirty = True
            time.sleep(0.04)


def _show_message(lcd, font, text, color="#FF4444", kind=None, sub=""):
    """Show a brief Obsidian Ember error/info card on LCD."""
    if Image is None or ImageDraw is None or rj_theme is None:
        return
    WIDTH, HEIGHT = lcd.width, lcd.height
    fonts = rj_theme.load_app_fonts(WIDTH, HEIGHT)
    img = Image.new("RGB", (WIDTH, HEIGHT), rj_theme.EMBER_THEME["bg_base"])
    d = ImageDraw.Draw(img)
    if kind is None:
        col_l = str(color or "").lower()
        kind = "err" if ("ff4" in col_l or "red" in col_l or "d95" in col_l) else ("warn" if "aa0" in col_l else "info")
    rj_theme.draw_status_card(d, WIDTH, HEIGHT, str(text), sub=str(sub), kind=kind, fonts=fonts)
    lcd.LCD_ShowImage(img, 0, 0)
    time.sleep(1.8)
