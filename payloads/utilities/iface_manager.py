#!/usr/bin/env python3
"""
EmberJack Payload -- Interface Manager
======================================
Author: 7h30th3r0n3

Centralized network interface management: rfkill, monitor mode,
up/down, IP config, MAC & driver info. One place to control all
WiFi, Ethernet, and Bluetooth interfaces.

Controls:
  UP / DOWN / PgUp / PgDn  -- Navigate interface list or actions
  ENTER / OK / RIGHT / TAB -- Open/focus actions for selected interface
  LEFT                     -- Back to list (or open detail view on compact)
  KEY1 / R                 -- Refresh interface list
  KEY2 / B                 -- Quick toggle RFKill block/unblock
  U                        -- Quick toggle UP / DOWN
  M                        -- Quick toggle Monitor / Managed (WiFi)
  E                        -- Export interface snapshot JSON to loot
  KEY3 / ESC / Q           -- Back / Exit
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import LCD_Config
from PIL import Image, ImageDraw

import rj_env
import rj_theme
from payloads._input_helper import flush_input, poll_ui_input, text_input_mode

ET = rj_theme.EMBER_THEME

PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}
LOOT_DIR = os.path.join(rj_env.get_base_dir(), "loot", "IfaceManager")

_running = True


def _cleanup(*_):
    global _running
    _running = False


signal.signal(signal.SIGINT, _cleanup)
signal.signal(signal.SIGTERM, _cleanup)


# ---------------------------------------------------------------------------
# Interface detection
# ---------------------------------------------------------------------------

def _run(cmd, timeout=5):
    """Run a command and return stdout."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def _get_driver(iface):
    try:
        return os.path.basename(os.path.realpath(f"/sys/class/net/{iface}/device/driver"))
    except Exception:
        return ""


def _get_ip(iface):
    out = _run(["ip", "-4", "-o", "addr", "show", iface])
    for line in out.split("\n"):
        parts = line.split()
        for i, p in enumerate(parts):
            if p == "inet" and i + 1 < len(parts):
                return parts[i + 1]
    return ""


def _get_mac(iface):
    try:
        with open(f"/sys/class/net/{iface}/address", "r", encoding="utf-8") as f:
            return f.read().strip().upper()
    except Exception:
        return ""


def _get_operstate(iface):
    try:
        with open(f"/sys/class/net/{iface}/operstate", "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "unknown"


def _get_mode(iface):
    """Return WiFi mode: Managed, Monitor, Master/AP, or ''."""
    out = _run(["iw", "dev", iface, "info"])
    if "type monitor" in out:
        return "Monitor"
    if "type AP" in out:
        return "Master"
    if "type managed" in out:
        return "Managed"
    m = re.search(r"type\s+(\S+)", out)
    if m:
        return m.group(1).capitalize()
    return ""


def _is_wifi(iface):
    return os.path.isdir(f"/sys/class/net/{iface}/wireless")


def _is_bluetooth(iface):
    return iface.startswith("hci")


def _get_rfkill_state():
    """Return dict {index: {type, soft, hard, device}}."""
    out = _run(["rfkill", "--json"])
    if not out:
        out = _run(["rfkill", "list"])
        result = {}
        current = None
        for line in out.split("\n"):
            if line and line[0].isdigit() and ":" in line:
                idx = line.split(":")[0].strip()
                rest = line.split(":", 1)[1].strip()
                dev_type = "bluetooth" if "Bluetooth" in rest else "wlan" if "Wireless" in rest else rest
                current = idx
                result[idx] = {"type": dev_type, "soft": False, "hard": False, "device": rest}
            elif current and "Soft blocked" in line:
                result[current]["soft"] = "yes" in line.lower()
            elif current and "Hard blocked" in line:
                result[current]["hard"] = "yes" in line.lower()
        return result

    try:
        data = json.loads(out)
        result = {}
        for item in data.get("rfkilldevices", data.get("", [])):
            idx = str(item.get("id", ""))
            result[idx] = {
                "type": item.get("type", ""),
                "soft": item.get("soft", "") == "blocked",
                "hard": item.get("hard", "") == "blocked",
                "device": item.get("device", ""),
            }
        return result
    except Exception:
        return {}


def _supports_mode(iface, mode):
    try:
        phy_link = os.path.realpath(f"/sys/class/net/{iface}/phy80211")
        phy_name = os.path.basename(phy_link)
        out = _run(["iw", "phy", phy_name, "info"])
        return f"* {mode}" in out
    except Exception:
        return False


def _scan_interfaces():
    """Return list of all network interface info dicts."""
    ifaces = []

    try:
        for name in sorted(os.listdir("/sys/class/net")):
            if name == "lo" or name.startswith(("veth", "br-", "docker", "virbr")):
                continue
            is_wifi = _is_wifi(name)
            info = {
                "name": name,
                "type": "wifi" if is_wifi else "eth",
                "driver": _get_driver(name),
                "mac": _get_mac(name),
                "ip": _get_ip(name),
                "state": _get_operstate(name),
                "mode": _get_mode(name) if is_wifi else "",
                "supports_mon": _supports_mode(name, "monitor") if is_wifi else False,
                "supports_ap": _supports_mode(name, "AP") if is_wifi else False,
                "rfkill_soft": False,
                "rfkill_hard": False,
            }
            ifaces.append(info)
    except Exception:
        pass

    hci_out = _run(["hciconfig"])
    blocks = []
    cur_block = []
    for line in hci_out.split("\n"):
        if line and not line[0].isspace() and ":" in line:
            if cur_block:
                blocks.append("\n".join(cur_block))
            cur_block = [line]
        elif cur_block:
            cur_block.append(line)
    if cur_block:
        blocks.append("\n".join(cur_block))

    for blk in blocks:
        first_line = blk.split("\n", 1)[0]
        name = first_line.split(":")[0].strip()
        state = "up" if "UP RUNNING" in blk else "down"
        mac = ""
        for l2 in blk.split("\n"):
            if "BD Address:" in l2:
                parts = l2.split("BD Address:")
                if len(parts) > 1:
                    mac = parts[1].strip().split()[0]
                break
        ifaces.append({
            "name": name,
            "type": "bluetooth",
            "driver": "hci",
            "mac": mac,
            "ip": "",
            "state": state,
            "mode": "",
            "supports_mon": False,
            "supports_ap": False,
            "rfkill_soft": False,
            "rfkill_hard": False,
        })

    rfk = _get_rfkill_state()
    for _idx, rinfo in rfk.items():
        rtype = rinfo["type"].lower()
        for ifc in ifaces:
            if rtype == "bluetooth" and ifc["type"] == "bluetooth":
                ifc["rfkill_soft"] = rinfo["soft"]
                ifc["rfkill_hard"] = rinfo["hard"]
            elif rtype in ("wlan", "wireless") and ifc["type"] == "wifi":
                ifc["rfkill_soft"] = rinfo["soft"]
                ifc["rfkill_hard"] = rinfo["hard"]

    return ifaces


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _set_up(iface):
    _run(["sudo", "ip", "link", "set", iface, "up"])


def _set_down(iface):
    _run(["sudo", "ip", "link", "set", iface, "down"])


def _set_monitor(iface):
    """Switch WiFi interface to monitor mode."""
    _run(["sudo", "ip", "link", "set", iface, "down"])
    out = _run(["sudo", "airmon-ng", "start", iface], timeout=15)
    if "monitor mode" in out.lower() or "enabled" in out.lower():
        return f"{iface} -> monitor (airmon)"
    _run(["sudo", "iw", iface, "set", "monitor", "none"])
    _run(["sudo", "ip", "link", "set", iface, "up"])
    mode = _get_mode(iface)
    if mode == "Monitor":
        return f"{iface} -> Monitor"
    mon_name = f"{iface}mon"
    if os.path.exists(f"/sys/class/net/{mon_name}"):
        return f"{mon_name} created"
    return "Monitor mode failed"


def _set_managed(iface):
    """Switch WiFi interface back to managed mode."""
    base = iface[:-3] if iface.endswith("mon") else iface
    _run(["sudo", "airmon-ng", "stop", iface], timeout=10)
    _run(["sudo", "ip", "link", "set", base, "down"])
    _run(["sudo", "iw", base, "set", "type", "managed"])
    _run(["sudo", "ip", "link", "set", base, "up"])
    _run(["sudo", "nmcli", "device", "set", base, "managed", "yes"], timeout=5)
    return f"{base} -> Managed"


def _restore_network():
    """Re-manage all WiFi interfaces + restart NetworkManager."""
    try:
        for name in sorted(os.listdir("/sys/class/net")):
            if os.path.isdir(f"/sys/class/net/{name}/wireless"):
                _run(["sudo", "nmcli", "device", "set", name, "managed", "yes"], timeout=5)
    except Exception:
        pass
    _run(["sudo", "rfkill", "unblock", "wifi"], timeout=3)
    _run(["sudo", "systemctl", "restart", "NetworkManager"], timeout=10)
    _run(["sudo", "systemctl", "restart", "wpa_supplicant"], timeout=10)
    return "Network restored"


def _rfkill_toggle(iface_info):
    """Toggle rfkill soft block for an interface."""
    itype = iface_info["type"]
    rf_type = "bluetooth" if itype == "bluetooth" else "wifi"
    if iface_info["rfkill_soft"]:
        _run(["sudo", "rfkill", "unblock", rf_type])
        return f"{rf_type} unblocked"
    _run(["sudo", "rfkill", "block", rf_type])
    return f"{rf_type} blocked"


def _rfkill_unblock_all():
    _run(["sudo", "rfkill", "unblock", "all"])
    return "All RF unblocked"


def _hci_up(iface):
    _run(["sudo", "hciconfig", iface, "up"])
    return f"{iface} up"


def _hci_down(iface):
    _run(["sudo", "hciconfig", iface, "down"])
    return f"{iface} down"


def _export_snapshot(ifaces):
    try:
        os.makedirs(LOOT_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(LOOT_DIR, f"ifaces_{ts}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"timestamp": ts, "interfaces": ifaces}, fh, indent=2)
        return f"Saved ifaces_{ts}.json"
    except OSError as exc:
        return f"Export failed: {exc}"


def _get_actions(iface_info):
    """Return list of available actions for an interface."""
    actions = []
    name = iface_info["name"]
    itype = iface_info["type"]
    state = iface_info["state"]

    if itype == "bluetooth":
        actions.append(("HCI Up", lambda: _hci_up(name)))
        actions.append(("HCI Down", lambda: _hci_down(name)))
        if iface_info["rfkill_soft"]:
            actions.append(("RF Unblock", lambda: _rfkill_toggle(iface_info)))
        else:
            actions.append(("RF Block", lambda: _rfkill_toggle(iface_info)))
        actions.append(("Unblock ALL RF", lambda: _rfkill_unblock_all()))
        return actions

    if state != "up":
        actions.append(("Bring UP", lambda: (_set_up(name), f"{name} up")[1]))
    else:
        actions.append(("Bring DOWN", lambda: (_set_down(name), f"{name} down")[1]))

    if itype == "wifi":
        mode = iface_info["mode"]
        is_onboard = iface_info.get("driver") == "brcmfmac"
        if mode != "Monitor" and iface_info["supports_mon"] and not is_onboard:
            actions.append(("Enable Monitor", lambda: _set_monitor(name)))
        if mode == "Monitor":
            actions.append(("Disable Monitor", lambda: _set_managed(name)))
        if iface_info["supports_ap"] and mode != "Master":
            actions.append(("(AP via payload)", lambda: "Use evil_twin/captive_portal"))
        actions.append(("NM Re-manage", lambda: (
            _run(["sudo", "nmcli", "device", "set", name, "managed", "yes"], timeout=5),
            f"{name} re-managed")[1]))
        if iface_info["rfkill_soft"]:
            actions.append(("RF Unblock", lambda: _rfkill_toggle(iface_info)))
        else:
            actions.append(("RF Block", lambda: _rfkill_toggle(iface_info)))

    actions.append(("Unblock ALL RF", lambda: _rfkill_unblock_all()))
    actions.append(("Restore Network", lambda: _restore_network()))
    return actions


# ---------------------------------------------------------------------------
# Native Resolution-Adaptive Obsidian Ember Renderer
# ---------------------------------------------------------------------------

def _iface_pills(ifc):
    """Build list of (label, bg, fg, outline) pills for an interface card."""
    pills = []
    if ifc["rfkill_hard"] or ifc["rfkill_soft"]:
        pills.append(("BLK", ET["danger_bg"], ET["danger"], None))
    elif ifc["state"] == "up":
        pills.append(("UP", ET["ok_bg"], ET["ok"], None))
    else:
        pills.append(("DOWN", ET["bg_well"], ET["text_dim"], ET["border_subtle"]))

    mode = ifc.get("mode", "")
    if mode == "Monitor":
        pills.append(("MON", ET["select_bg"], ET["accent_bright"], ET["border_accent"]))
    elif mode == "Master":
        pills.append(("AP", ET["warn_bg"], ET["warn"], None))
    elif ifc["type"] == "bluetooth":
        pills.append(("BT", ET["bg_well"], ET["info"], ET["border_subtle"]))
    elif ifc["type"] == "wifi":
        pills.append(("WIFI", ET["bg_well"], ET["text_muted"], ET["border_subtle"]))
    else:
        pills.append(("ETH", ET["bg_well"], ET["text_muted"], ET["border_subtle"]))
    return pills


def render_iface_frame(
    width,
    height,
    ifaces,
    sel=0,
    scroll=0,
    view="list",
    actions=None,
    action_sel=0,
    toast="",
    toast_kind="info",
    busy_msg="",
    fonts=None,
):
    """Pure PIL native resolution-adaptive Obsidian Ember Interface Manager renderer."""
    f = fonts or rj_theme.load_app_fonts(width, height)
    wide = f["wide"]
    large = f["large"]
    split = width >= 460
    actions = actions or []

    img = Image.new("RGB", (width, height), ET["bg_base"])
    d = ImageDraw.Draw(img)

    up_cnt = sum(1 for i in ifaces if i.get("state") == "up" and not i.get("rfkill_soft") and not i.get("rfkill_hard"))
    hdr_title = "INTERFACE MANAGER" if wide else "IFACE MGR"
    hdr_h = rj_theme.draw_app_header(
        d, width, hdr_title, f,
        badge=f"{len(ifaces)}",
        right_text=f"{up_cnt} UP",
        right_pill=("ACTIONS" if view == "actions" else ""),
    )

    if view == "actions":
        hint = "Up/Down:Pick  Enter:Run  Left/Esc:Back" if wide else "OK:Run K3:Back"
    elif view == "detail":
        hint = "Press any key to return"
    elif large:
        hint = "Enter/Tab:Actions  •  U:Up/Dn  •  M:Mon  •  K2/B:RFKill  •  K1/R:Ref  •  Esc/Q:Exit"
    elif wide:
        hint = "OK:Act  U:Up/Dn  K1/R:Ref  K2:RF  K3:Exit"
    else:
        hint = "OK:Act K1:Ref K2:RF K3:X"
    pos_txt = f"{min(sel + 1, max(1, len(ifaces)))}/{max(1, len(ifaces))}"
    ftr_h = rj_theme.draw_app_footer(d, width, height, hint, f, right_text=pos_txt)

    if busy_msg:
        rj_theme.draw_status_card(d, width, height, "Applying Change...", sub=busy_msg, kind="warn", fonts=f)
        return img

    toast_h = (22 if large else 16) if toast else 0
    y0 = hdr_h + (8 if large else (4 if wide else 2))
    y1 = height - ftr_h - toast_h - (6 if large else (3 if wide else 2))
    pad = 10 if large else (6 if wide else 3)

    if not ifaces:
        rj_theme.draw_status_card(
            d, width, height,
            "No Interfaces Found",
            sub="Press K1 / R to scan again",
            kind="warn", foot="Esc / K3 to exit", fonts=f,
        )
        return img

    cur = ifaces[max(0, min(sel, len(ifaces) - 1))]

    # Compact-only full-screen detail view
    if not split and view == "detail":
        rj_theme.draw_card(d, (pad, y0, width - pad, y1), fill=ET["bg_surface"], outline=ET["border_accent"], radius=5 if wide else 3)
        dy = y0 + (8 if wide else 4)
        d.text((pad + 8, dy), cur["name"], font=f["title"], fill=ET["accent_bright"])
        dy += 18 if wide else 13
        fields = [
            ("Type", cur["type"].upper()),
            ("State", cur["state"].upper()),
            ("IP", cur["ip"] or "Unassigned"),
            ("MAC", cur["mac"] or "N/A"),
            ("Driver", cur["driver"] or "kernel"),
        ]
        if cur["type"] == "wifi":
            fields.append(("Mode", cur["mode"] or "Managed"))
            fields.append(("Caps", f"Mon:{'Y' if cur['supports_mon'] else 'N'} AP:{'Y' if cur['supports_ap'] else 'N'}"))
        if cur["rfkill_hard"] or cur["rfkill_soft"]:
            fields.append(("RFKill", "HARD BLOCKED" if cur["rfkill_hard"] else "SOFT BLOCKED"))
        step = 15 if wide else 11
        for k_lbl, v_lbl in fields:
            if dy + step > y1 - 4:
                break
            d.text((pad + 8, dy), f"{k_lbl}:", font=f["sub"], fill=ET["text_muted"])
            d.text((pad + (54 if wide else 40), dy), rj_theme.truncate_text(d, v_lbl, width - pad * 2 - 58, f["body"]), font=f["body"], fill=ET["text_primary"])
            dy += step
        return img

    # Compact-only full-screen actions view
    if not split and view == "actions":
        rj_theme.draw_card(d, (pad, y0, width - pad, y1), fill=ET["bg_surface"], outline=ET["border_subtle"], radius=5 if wide else 3)
        ay0 = y0 + (6 if wide else 3)
        d.text((pad + 6, ay0), rj_theme.truncate_text(d, f"{cur['name']} Actions", width - pad * 2 - 12, f["body_bold"]), font=f["body_bold"], fill=ET["accent_bright"])
        ay0 += 17 if wide else 13
        arow_h = 22 if wide else 14
        avis = max(1, (y1 - ay0 - 4) // arow_h)
        ast = max(0, min(action_sel - avis // 2, max(0, len(actions) - avis)))
        for slot in range(avis):
            idx = ast + slot
            if idx >= len(actions):
                break
            lbl, _fn = actions[idx]
            asel = idx == action_sel
            ry0 = ay0 + slot * arow_h
            ry1 = ry0 + arow_h - 2
            rj_theme.draw_card(
                d, (pad + 4, ry0, width - pad - 4, ry1),
                fill=ET["select_bg"] if asel else ET["bg_well"],
                outline=ET["border_accent"] if asel else ET["border_subtle"],
                radius=4 if wide else 2,
                accent_left=ET["accent"] if asel else None, accent_width=2,
            )
            d.text((pad + 10, ry0 + (3 if wide else 1)), rj_theme.truncate_text(d, lbl, width - pad * 2 - 22, f["body"]), font=f["body"], fill=ET["text_bright"] if asel else ET["text_primary"])
        return img

    # Main List (left pane on split-pane, full width on compact)
    sb_w = 5 if large else (4 if wide else 2)
    list_x0 = pad
    list_x1 = int(width * 0.52) - 4 if split else (width - pad - sb_w - 3)
    list_focused = (view == "list")

    row_h = 48 if large else (32 if wide else 21)
    vis = max(1, (y1 - y0) // row_h)
    st = max(0, min(scroll, max(0, len(ifaces) - vis)))

    for slot in range(vis):
        idx = st + slot
        if idx >= len(ifaces):
            break
        ifc = ifaces[idx]
        is_sel = idx == sel
        ry0 = y0 + slot * row_h
        ry1 = ry0 + row_h - (5 if large else (3 if wide else 2))

        accent_col = (
            ET["danger"] if (ifc["rfkill_hard"] or ifc["rfkill_soft"])
            else ET["accent"] if is_sel
            else (ET["ok"] if ifc["state"] == "up" else None)
        )
        rj_theme.draw_card(
            d, (list_x0, ry0, list_x1, ry1),
            fill=ET["select_bg"] if is_sel else ET["bg_surface"],
            outline=(ET["border_accent"] if (is_sel and list_focused) else ET["border_subtle"]),
            radius=6 if wide else 3,
            accent_left=accent_col,
            accent_width=3 if wide else 2,
        )

        # Right-side status & mode pills
        rx = list_x1 - (8 if wide else 3)
        for plbl, pbg, pfg, pout in _iface_pills(ifc):
            pbb = rj_theme.measure_text(f["badge"], plbl, d)
            ppw = (pbb[2] - pbb[0]) + (10 if wide else 6)
            if rx - ppw < list_x0 + (62 if wide else 46):
                break
            rj_theme.draw_pill(
                d, rx - ppw, ry0 + (6 if large else (3 if wide else 2)),
                plbl, f["badge"], bg=pbg, fg=pfg, outline=pout,
                pad_x=5 if wide else 3, pad_y=1, radius=3,
            )
            rx -= ppw + (4 if wide else 2)

        nx = list_x0 + (10 if wide else 5)
        name_txt = rj_theme.truncate_text(d, ifc["name"], max(36, rx - nx - 4), f["body_bold"] if wide else f["body"])
        d.text(
            (nx, ry0 + (5 if large else (3 if wide else 2))),
            name_txt,
            font=f["body_bold"] if wide else f["body"],
            fill=ET["text_bright"] if is_sel else ET["text_primary"],
        )

        if large:
            sub = f"{ifc['ip'] or 'no IP'}  •  {ifc['driver'] or 'kernel'}"
            d.text((nx, ry0 + 25), rj_theme.truncate_text(d, sub, list_x1 - nx - 8, f["sub"]), font=f["sub"], fill=ET["text_muted"])
        elif wide:
            sub = ifc["ip"] or ifc["mac"] or ifc["driver"] or ""
            d.text((nx, ry0 + 16), rj_theme.truncate_text(d, sub, list_x1 - nx - 8, f["sub"]), font=f["sub"], fill=ET["text_muted"])
        else:
            sub = ifc["ip"].split("/")[0] if ifc["ip"] else (ifc["driver"] or "")
            if sub:
                d.text((nx, ry0 + 11), rj_theme.truncate_text(d, sub, list_x1 - nx - 4, f["badge"]), font=f["badge"], fill=ET["text_dim"])

    if len(ifaces) > vis:
        rj_theme.draw_scrollbar(d, list_x1 + 3, y0, y1, st, vis, len(ifaces), width=sb_w)

    # Right Split-Pane Inspector + Quick Actions (W >= 460)
    if split:
        px0 = list_x1 + 12
        px1 = width - pad
        pw = px1 - px0 - 20

        # Top Detail Inspector Card
        det_h = int((y1 - y0) * 0.50)
        rj_theme.draw_card(
            d, (px0, y0, px1, y0 + det_h),
            fill=ET["bg_surface"], outline=ET["border_subtle"], radius=7,
        )
        dy = y0 + 10
        d.text((px0 + 10, dy), rj_theme.truncate_text(d, cur["name"], pw - 70, f["title"]), font=f["title"], fill=ET["accent_bright"])
        st_lbl = "BLOCKED" if (cur["rfkill_hard"] or cur["rfkill_soft"]) else cur["state"].upper()
        st_bg = ET["danger_bg"] if "BLOCK" in st_lbl else (ET["ok_bg"] if st_lbl == "UP" else ET["bg_well"])
        st_fg = ET["danger"] if "BLOCK" in st_lbl else (ET["ok"] if st_lbl == "UP" else ET["text_muted"])
        sbb = rj_theme.measure_text(f["badge"], st_lbl, d)
        rj_theme.draw_pill(d, px1 - 10 - (sbb[2] - sbb[0] + 10), dy, st_lbl, f["badge"], bg=st_bg, fg=st_fg, outline=None, pad_x=5, pad_y=1, radius=3)
        dy += 22
        d.line([(px0 + 10, dy), (px1 - 10, dy)], fill=ET["border_subtle"], width=1)
        dy += 7

        det_rows = [
            ("IPv4", cur["ip"] or "Unassigned", f["mono"]),
            ("MAC", cur["mac"] or "N/A", f["mono"]),
            ("Driver", cur["driver"] or "kernel", f["body"]),
        ]
        if cur["type"] == "wifi":
            caps = []
            if cur["supports_mon"]:
                caps.append("Monitor")
            if cur["supports_ap"]:
                caps.append("AP")
            det_rows.append(("Mode", f"{cur['mode'] or 'Managed'} ({'/'.join(caps) or 'STA'})", f["sub"]))
        else:
            det_rows.append(("Type", cur["type"].upper(), f["sub"]))

        for k_lbl, v_lbl, v_font in det_rows:
            if dy + 16 > y0 + det_h - 4:
                break
            d.text((px0 + 10, dy), f"{k_lbl}:", font=f["sub"], fill=ET["text_muted"])
            d.text((px0 + 62, dy), rj_theme.truncate_text(d, v_lbl, max(40, pw - 52), v_font), font=v_font, fill=ET["text_primary"])
            dy += 18

        # Bottom Actions Panel
        act_y0 = y0 + det_h + 8
        act_focused = (view == "actions")
        rj_theme.draw_card(
            d, (px0, act_y0, px1, y1),
            fill=ET["bg_surface"],
            outline=ET["border_accent"] if act_focused else ET["border_subtle"],
            radius=7,
            accent_left=ET["accent"] if act_focused else None,
            accent_width=3,
        )
        ay = act_y0 + 8
        d.text(
            (px0 + 10, ay),
            "ACTIONS (Enter to run)" if act_focused else "QUICK ACTIONS (Enter/Tab)",
            font=f["badge"],
            fill=ET["accent_bright"] if act_focused else ET["text_muted"],
        )
        ay += 17
        avail_acts = actions if actions else _get_actions(cur)
        arow_h = 22
        avis = max(1, (y1 - ay - 6) // arow_h)
        ast = max(0, min((action_sel if act_focused else 0) - avis // 2, max(0, len(avail_acts) - avis)))
        for slot in range(avis):
            idx = ast + slot
            if idx >= len(avail_acts):
                break
            lbl, _fn = avail_acts[idx]
            asel = act_focused and (idx == action_sel)
            ry0 = ay + slot * arow_h
            ry1 = ry0 + arow_h - 3
            rj_theme.draw_card(
                d, (px0 + 8, ry0, px1 - 12, ry1),
                fill=ET["select_bg"] if asel else ET["bg_well"],
                outline=ET["border_accent"] if asel else ET["border_subtle"],
                radius=4,
            )
            d.text(
                (px0 + 14, ry0 + 2),
                rj_theme.truncate_text(d, lbl, pw - 14, f["sub"]),
                font=f["body_bold"] if asel else f["sub"],
                fill=ET["accent_bright"] if asel else ET["text_primary"],
            )
        if len(avail_acts) > avis:
            rj_theme.draw_scrollbar(d, px1 - 8, ay, y1 - 6, ast, avis, len(avail_acts), width=3)

    if toast:
        ty0 = height - ftr_h - toast_h
        ty1 = height - ftr_h - 1
        t_bg = ET["ok_bg"] if toast_kind == "ok" else (ET["danger_bg"] if toast_kind == "err" else ET["select_bg"])
        t_fg = ET["ok"] if toast_kind == "ok" else (ET["danger"] if toast_kind == "err" else ET["accent_bright"])
        d.rectangle((0, ty0, width - 1, ty1), fill=t_bg)
        d.line([(0, ty0), (width - 1, ty0)], fill=ET["border_subtle"], width=1)
        d.text((8 if wide else 3, ty0 + max(1, (toast_h - 12) // 2)), rj_theme.truncate_text(d, toast, width - 16, f["sub"]), font=f["sub"], fill=t_fg)

    return img


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _running

    GPIO.setmode(GPIO.BCM)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    LCD_Config.GPIO_Init()
    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    lcd.LCD_Clear()
    W, H = lcd.width, lcd.height
    fonts = rj_theme.load_app_fonts(W, H)
    split = W >= 460

    ifaces = _scan_interfaces()
    sel = 0
    scroll = 0
    view = "list"  # list | actions | detail
    action_sel = 0
    actions = _get_actions(ifaces[0]) if ifaces else []
    toast = f"Loaded {len(ifaces)} interface(s)"
    toast_kind = "info"
    toast_until = time.monotonic() + 3.0
    dirty = True

    def _run_action_with_busy(label, fn):
        nonlocal ifaces, sel, actions, action_sel, toast, toast_kind, toast_until, dirty
        busy_frame = render_iface_frame(W, H, ifaces, sel=sel, scroll=scroll, view=view, actions=actions, action_sel=action_sel, busy_msg=label, fonts=fonts)
        lcd.LCD_ShowImage(busy_frame, 0, 0)
        res = str(fn())
        ifaces = _scan_interfaces()
        sel = min(sel, max(0, len(ifaces) - 1))
        actions = _get_actions(ifaces[sel]) if ifaces else []
        action_sel = min(action_sel, max(0, len(actions) - 1))
        toast = res
        toast_kind = "err" if "fail" in res.lower() else "ok"
        toast_until = time.monotonic() + 4.0
        flush_input()
        dirty = True

    try:
        with text_input_mode():
            flush_input()
            while _running:
                now_m = time.monotonic()
                row_h = 48 if fonts["large"] else (32 if fonts["wide"] else 21)
                hdr_h = 30 if fonts["large"] else (24 if fonts["wide"] else 18)
                ftr_h = 22 if fonts["large"] else (18 if fonts["wide"] else 13)
                vis = max(1, (H - hdr_h - ftr_h - 14) // row_h)

                if ifaces:
                    sel = max(0, min(sel, len(ifaces) - 1))
                    if sel < scroll:
                        scroll = sel
                    elif sel >= scroll + vis:
                        scroll = sel - vis + 1
                else:
                    sel = 0
                    scroll = 0

                active_toast = toast if (toast and now_m < toast_until) else ""
                if toast and not active_toast:
                    toast = ""
                    dirty = True

                if dirty:
                    frame = render_iface_frame(
                        W, H, ifaces, sel=sel, scroll=scroll, view=view,
                        actions=actions, action_sel=action_sel,
                        toast=active_toast, toast_kind=toast_kind, fonts=fonts,
                    )
                    lcd.LCD_ShowImage(frame, 0, 0)
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO)
                if act is None and ch is None:
                    time.sleep(0.03)
                    continue

                kl = ch.lower() if isinstance(ch, str) else ""

                if view == "detail":
                    view = "list"
                    dirty = True
                    continue

                if view == "actions":
                    if act in ("KEY3", "ESCAPE", "BACKSPACE", "LEFT", "TAB") or kl == "q":
                        view = "list"
                        dirty = True
                    elif act == "UP" and actions:
                        action_sel = (action_sel - 1) % len(actions)
                        dirty = True
                    elif act == "DOWN" and actions:
                        action_sel = (action_sel + 1) % len(actions)
                        dirty = True
                    elif act in ("OK", "ENTER") and actions:
                        lbl, fn = actions[action_sel]
                        _run_action_with_busy(lbl, fn)
                        if not split:
                            view = "list"
                    continue

                # view == "list"
                if act in ("KEY3", "ESCAPE", "BACKSPACE") or kl == "q":
                    break
                elif act == "UP" and ifaces:
                    sel = (sel - 1) % len(ifaces)
                    actions = _get_actions(ifaces[sel])
                    action_sel = 0
                    dirty = True
                elif act == "DOWN" and ifaces:
                    sel = (sel + 1) % len(ifaces)
                    actions = _get_actions(ifaces[sel])
                    action_sel = 0
                    dirty = True
                elif act == "PAGEUP" and ifaces:
                    sel = max(0, sel - vis)
                    actions = _get_actions(ifaces[sel])
                    action_sel = 0
                    dirty = True
                elif act == "PAGEDOWN" and ifaces:
                    sel = min(len(ifaces) - 1, sel + vis)
                    actions = _get_actions(ifaces[sel])
                    action_sel = 0
                    dirty = True
                elif act in ("OK", "ENTER", "RIGHT", "TAB") and ifaces:
                    actions = _get_actions(ifaces[sel])
                    action_sel = 0
                    view = "actions"
                    dirty = True
                elif act == "LEFT" and ifaces and not split:
                    view = "detail"
                    dirty = True
                elif act == "KEY1" or kl == "r":
                    _run_action_with_busy("Refreshing interfaces...", lambda: "Refreshed")
                elif (act == "KEY2" or kl == "b") and ifaces:
                    target = ifaces[sel]
                    _run_action_with_busy(f"RFKill toggle {target['name']}", lambda t=target: _rfkill_toggle(t))
                elif kl == "u" and ifaces:
                    target = ifaces[sel]
                    if target["type"] == "bluetooth":
                        fn = (lambda t=target: _hci_down(t["name"])) if target["state"] == "up" else (lambda t=target: _hci_up(t["name"]))
                    elif target["state"] == "up":
                        fn = lambda t=target: (_set_down(t["name"]), f"{t['name']} down")[1]
                    else:
                        fn = lambda t=target: (_set_up(t["name"]), f"{t['name']} up")[1]
                    _run_action_with_busy(f"Toggling {target['name']}...", fn)
                elif kl == "m" and ifaces:
                    target = ifaces[sel]
                    if target["type"] == "wifi":
                        if target["mode"] == "Monitor":
                            _run_action_with_busy(f"Disabling monitor on {target['name']}", lambda t=target: _set_managed(t["name"]))
                        elif target["supports_mon"] and target.get("driver") != "brcmfmac":
                            _run_action_with_busy(f"Enabling monitor on {target['name']}", lambda t=target: _set_monitor(t["name"]))
                        else:
                            toast, toast_kind, toast_until = "Monitor mode not supported on this adapter", "warn", time.monotonic() + 3.0
                            dirty = True
                elif kl == "e":
                    msg = _export_snapshot(ifaces)
                    toast, toast_kind, toast_until = msg, ("err" if "failed" in msg.lower() else "ok"), time.monotonic() + 3.5
                    dirty = True
    finally:
        _running = False
        try:
            lcd.LCD_Clear()
        except Exception:
            pass
        try:
            GPIO.cleanup()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
