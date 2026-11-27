#!/usr/bin/env python3
"""
EmberJack YouTube mpv IPC Client & Obsidian Ember ASS Video OSD
===============================================================
Provides a persistent JSON-IPC connection to `mpv` so GPU/DRM hardware video
playback keeps EmberJack's full Obsidian Ember on-screen HUD, progress bar,
toast notifications, and interactive in-video Playback Actions menu — all
composited by mpv's libass/GPU pipeline with ~0% CPU overhead and zero stutter.
"""

import json
import os
import socket
import time
from typing import Any, List, Optional, Sequence, Tuple

# ASS uses BGR byte order (&HBBGGRR&) and inverted alpha (&H00& opaque -> &HFF& transparent)
_ASS_BG = "&H100E0E&"        # #0E0E10 bg_base
_ASS_SURFACE = "&H1E1A1B&"   # #1B1A1E bg_surface
_ASS_SELECT = "&H121D2D&"    # #2D1D12 select_bg
_ASS_WELL = "&H2A2426&"      # #26242A bg_well
_ASS_BORDER = "&H245EB8&"    # #B85E24 border_accent
_ASS_SUBTLE = "&H363032&"    # #323036 border_subtle
_ASS_ACCENT = "&H2F7AE0&"    # #E07A2F accent
_ASS_BRIGHT = "&H4A99F2&"    # #F2994A accent_bright
_ASS_BUF = "&H203E70&"       # #703E20 buffered progress bar
_ASS_TEXT = "&HD7DFE6&"      # #E6DFD7 text_primary
_ASS_WHITE = "&HF0F8FF&"     # #FFF8F0 text_bright
_ASS_MUTED = "&H8A929B&"     # #9B928A text_muted
_ASS_OK = "&H80DE4A&"        # #4ADE80 ok
_ASS_WARN = "&H24BFFB&"      # #FBBF24 warn

OSD_RES_X = 1280
OSD_RES_Y = 720
OSD_OVERLAY_ID = 1
MENU_OVERLAY_ID = 2


def escape_ass(text: str) -> str:
    """Escape arbitrary user/YouTube strings for safe ASS dialogue rendering."""
    s = str(text or "")
    s = s.replace("\r", " ").replace("\n", " ")
    s = s.replace("\\", r"\\")
    s = s.replace("{", r"\{").replace("}", r"\}")
    return s


def _fmt_time(sec: float) -> str:
    s = max(0, int(sec or 0))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _ass_rect(x0: int, y0: int, x1: int, y1: int, color: str, alpha: str = "&H00&") -> str:
    """Return an ASS vector drawing event for a crisp filled rectangle."""
    return (
        rf"{{\an7\pos(0,0)\bord0\shad0\1c{color}\1a{alpha}\p1}}"
        f"m {int(x0)} {int(y0)} l {int(x1)} {int(y0)} l {int(x1)} {int(y1)} l {int(x0)} {int(y1)}"
        r"{\p0}"
    )


def _ass_text(
    x: int,
    y: int,
    text: str,
    size: int = 24,
    color: str = _ASS_TEXT,
    align: int = 7,
    bold: bool = False,
    border: float = 0.0,
) -> str:
    """Return an ASS positioned text line with Obsidian Ember styling."""
    b_tag = r"\b1" if bold else r"\b0"
    bord_tag = rf"\bord{border:g}\3c&H100E0E&" if border > 0 else r"\bord0"
    return (
        rf"{{\an{align}\pos({int(x)},{int(y)})\fnDejaVu Sans\fs{int(size)}"
        rf"{b_tag}{bord_tag}\shad0\1c{color}\1a&H00&}}{escape_ass(text)}"
    )


def _trunc(text: str, max_chars: int) -> str:
    s = str(text or "").strip()
    if len(s) <= max_chars:
        return s
    return s[: max(1, max_chars - 3)].rstrip() + "..."


def build_mpv_osd_ass(
    title: str,
    elapsed: float,
    duration: float,
    volume_pct: int,
    muted: bool = False,
    paused: bool = False,
    quality_label: str = "720p",
    audio_label: str = "",
    channel: str = "",
    liked: bool = False,
    watch_later: bool = False,
    toast: str = "",
    playlist_pos: str = "",
    buffered_sec: float = 0.0,
    speed: float = 1.0,
    loop_one: bool = False,
    captions_on: bool = False,
    show_bars: bool = True,
) -> str:
    """Build a crisp 1280x720 Obsidian Ember ASS overlay for mpv's `osd-overlay`."""
    lines: List[str] = []
    W, H = OSD_RES_X, OSD_RES_Y

    if show_bars:
        # ---- Top Bar (title, channel & right-aligned status pills) ----
        top_h = 76
        lines.append(_ass_rect(0, 0, W, top_h, _ASS_BG, alpha="&H28&"))
        lines.append(_ass_rect(0, top_h - 2, W, top_h, _ASS_BORDER, alpha="&H40&"))
        # Left ember accent bar
        lines.append(_ass_rect(0, 0, 6, top_h - 2, _ASS_ACCENT))

        # Status pills on the right
        pills: List[Tuple[str, str, str]] = []
        if quality_label:
            pills.append((str(quality_label).upper(), _ASS_BRIGHT, _ASS_SELECT))
        if abs(float(speed or 1.0) - 1.0) > 0.01:
            pills.append((f"{float(speed):g}x", _ASS_OK, _ASS_WELL))
        if loop_one:
            pills.append(("LOOP", _ASS_OK, _ASS_WELL))
        if captions_on:
            pills.append(("CC", _ASS_BRIGHT, _ASS_WELL))
        if liked:
            pills.append(("LIKED", _ASS_BRIGHT, _ASS_SELECT))
        if watch_later:
            pills.append(("WL", _ASS_TEXT, _ASS_WELL))
        if playlist_pos:
            pills.append((str(playlist_pos), _ASS_TEXT, _ASS_WELL))

        px = W - 26
        for lbl, fg, bg in reversed(pills):
            pw = max(56, len(lbl) * 13 + 22)
            x0 = px - pw
            lines.append(_ass_rect(x0, 20, px, 54, bg, alpha="&H18&"))
            lines.append(_ass_rect(x0, 20, px, 22, _ASS_BORDER, alpha="&H60&"))
            lines.append(_ass_rect(x0, 52, px, 54, _ASS_BORDER, alpha="&H60&"))
            lines.append(_ass_text((x0 + px) // 2, 37, lbl, size=18, color=fg, align=5, bold=True))
            px = x0 - 10

        max_title_chars = max(24, (px - 36) // 13)
        lines.append(_ass_text(26, 14, _trunc(title or "YouTube", max_title_chars), size=26, color=_ASS_WHITE, align=7, bold=True))
        sub_parts = []
        if channel:
            sub_parts.append(_trunc(channel, 34))
        if audio_label:
            sub_parts.append(_trunc(audio_label, 24))
        if sub_parts:
            lines.append(_ass_text(26, 46, "  •  ".join(sub_parts), size=17, color=_ASS_MUTED, align=7))

        # ---- Bottom Transport Bar (progress bar, time, hints, volume) ----
        bot_y = H - 104
        lines.append(_ass_rect(0, bot_y, W, H, _ASS_BG, alpha="&H28&"))
        lines.append(_ass_rect(0, bot_y, W, bot_y + 2, _ASS_BORDER, alpha="&H40&"))

        bar_x0, bar_x1 = 30, W - 30
        bar_y0, bar_y1 = bot_y + 22, bot_y + 34
        bar_w = bar_x1 - bar_x0
        lines.append(_ass_rect(bar_x0, bar_y0, bar_x1, bar_y1, _ASS_WELL, alpha="&H10&"))

        el = max(0.0, float(elapsed or 0.0))
        dur = max(0.0, float(duration or 0.0))
        buf_s = max(0.0, float(buffered_sec or 0.0))
        if dur > 0.0:
            prog = min(1.0, max(0.0, el / dur))
            buf_frac = min(1.0, max(prog, (el + buf_s) / dur)) if buf_s > 0.0 else prog
            if buf_frac > prog:
                bx = bar_x0 + int(bar_w * buf_frac)
                lines.append(_ass_rect(bar_x0, bar_y0, bx, bar_y1, _ASS_BUF, alpha="&H10&"))
            px_fill = bar_x0 + max(8, int(bar_w * prog)) if prog > 0.0 else bar_x0
            if px_fill > bar_x0:
                lines.append(_ass_rect(bar_x0, bar_y0, px_fill, bar_y1, _ASS_ACCENT))
                knob_x = min(bar_x1 - 4, max(bar_x0 + 4, px_fill))
                lines.append(_ass_rect(knob_x - 5, bar_y0 - 4, knob_x + 5, bar_y1 + 4, _ASS_BRIGHT))
        else:
            # Live stream indicator bar
            lines.append(_ass_rect(bar_x0, bar_y0, bar_x1, bar_y1, _ASS_ACCENT, alpha="&H60&"))

        state_lbl = "PAUSED" if paused else "PLAY"
        state_col = _ASS_WARN if paused else _ASS_OK
        time_lbl = f"{_fmt_time(el)} / {_fmt_time(dur)}" if dur > 0.0 else f"{_fmt_time(el)}  LIVE"
        lines.append(_ass_text(30, bot_y + 50, state_lbl, size=20, color=state_col, align=7, bold=True))
        lines.append(_ass_text(122, bot_y + 50, time_lbl, size=20, color=_ASS_WHITE, align=7, bold=True))

        vol_lbl = "MUTED" if muted else f"VOL {int(volume_pct)}%"
        vol_col = _ASS_WARN if muted else _ASS_BRIGHT
        lines.append(_ass_text(W - 30, bot_y + 50, vol_lbl, size=20, color=vol_col, align=9, bold=True))

        hint = "Space:Pause   Left/Right:±15s   Up/Dn:Vol   I:HUD   O:Menu   S:Quality   L:Like   Esc:Exit"
        lines.append(_ass_text(W // 2, bot_y + 78, hint, size=16, color=_ASS_MUTED, align=8))

    # ---- Center Pause Badge (when paused and no modal menu open) ----
    if paused and show_bars:
        pw, ph = 240, 68
        cx0, cy0 = (W - pw) // 2, (H - ph) // 2
        lines.append(_ass_rect(cx0, cy0, cx0 + pw, cy0 + ph, _ASS_SURFACE, alpha="&H20&"))
        lines.append(_ass_rect(cx0, cy0, cx0 + 6, cy0 + ph, _ASS_ACCENT))
        lines.append(_ass_rect(cx0, cy0, cx0 + pw, cy0 + 2, _ASS_BORDER))
        lines.append(_ass_rect(cx0, cy0 + ph - 2, cx0 + pw, cy0 + ph, _ASS_BORDER))
        lines.append(_ass_text(W // 2 + 3, H // 2, "PAUSED", size=30, color=_ASS_BRIGHT, align=5, bold=True))

    # ---- Floating Toast Pill (works even when HUD bars are hidden!) ----
    if toast:
        t_clean = _trunc(toast, 56)
        tw = min(W - 120, max(220, len(t_clean) * 14 + 54))
        th = 48
        tx0 = (W - tw) // 2
        ty0 = 92 if show_bars else 28
        lines.append(_ass_rect(tx0, ty0, tx0 + tw, ty0 + th, _ASS_SURFACE, alpha="&H18&"))
        lines.append(_ass_rect(tx0, ty0, tx0 + 5, ty0 + th, _ASS_ACCENT))
        lines.append(_ass_rect(tx0, ty0, tx0 + tw, ty0 + 2, _ASS_BORDER))
        lines.append(_ass_rect(tx0, ty0 + th - 2, tx0 + tw, ty0 + th, _ASS_BORDER))
        lines.append(_ass_text(W // 2, ty0 + th // 2, t_clean, size=22, color=_ASS_BRIGHT, align=5, bold=True))

    return "\n".join(lines)


def build_mpv_menu_ass(
    title: str,
    subtitle: str,
    options: Sequence[Tuple[str, Any]],
    cursor: int,
) -> str:
    """Build a floating Obsidian Ember interactive modal rendered natively inside mpv."""
    W, H = OSD_RES_X, OSD_RES_Y
    lines: List[str] = []
    # Subtle full-screen dim scrim
    lines.append(_ass_rect(0, 0, W, H, _ASS_BG, alpha="&H78&"))

    card_w = 640
    row_h = 42
    max_vis = 9
    total = len(options)
    vis = min(max_vis, max(1, total))
    hdr_h = 74
    ftr_h = 40
    card_h = hdr_h + vis * row_h + ftr_h + 16
    x0 = (W - card_w) // 2
    y0 = max(24, (H - card_h) // 2)
    x1 = x0 + card_w
    y1 = y0 + card_h

    # Outer Obsidian Ember panel
    lines.append(_ass_rect(x0, y0, x1, y1, _ASS_SURFACE, alpha="&H0C&"))
    lines.append(_ass_rect(x0, y0, x0 + 6, y1, _ASS_ACCENT))
    lines.append(_ass_rect(x0, y0, x1, y0 + 2, _ASS_BORDER))
    lines.append(_ass_rect(x0, y1 - 2, x1, y1, _ASS_BORDER))
    lines.append(_ass_rect(x1 - 2, y0, x1, y1, _ASS_BORDER))

    # Header
    lines.append(_ass_rect(x0 + 6, y0 + 2, x1 - 2, y0 + hdr_h, _ASS_BG, alpha="&H20&"))
    lines.append(_ass_rect(x0 + 6, y0 + hdr_h - 2, x1 - 2, y0 + hdr_h, _ASS_SUBTLE))
    lines.append(_ass_text(x0 + 24, y0 + 14, _trunc(title, 44), size=24, color=_ASS_BRIGHT, align=7, bold=True))
    if subtitle:
        lines.append(_ass_text(x0 + 24, y0 + 45, _trunc(subtitle, 56), size=16, color=_ASS_MUTED, align=7))

    cur_idx = max(0, min(total - 1, int(cursor))) if total else 0
    start = max(0, min(cur_idx - vis // 2, max(0, total - vis)))
    ry = y0 + hdr_h + 8
    for slot in range(vis):
        idx = start + slot
        if idx >= total:
            break
        lbl = str(options[idx][0])
        sel = (idx == cur_idx)
        row_y0 = ry + slot * row_h
        row_y1 = row_y0 + row_h - 6
        if sel:
            lines.append(_ass_rect(x0 + 18, row_y0, x1 - 18, row_y1, _ASS_SELECT, alpha="&H08&"))
            lines.append(_ass_rect(x0 + 18, row_y0, x0 + 23, row_y1, _ASS_ACCENT))
            lines.append(_ass_rect(x0 + 18, row_y0, x1 - 18, row_y0 + 1, _ASS_BORDER))
            lines.append(_ass_rect(x0 + 18, row_y1 - 1, x1 - 18, row_y1, _ASS_BORDER))
            lines.append(_ass_text(x0 + 36, (row_y0 + row_y1) // 2, _trunc(lbl, 48), size=20, color=_ASS_BRIGHT, align=4, bold=True))
        else:
            lines.append(_ass_rect(x0 + 18, row_y0, x1 - 18, row_y1, _ASS_BG, alpha="&H50&"))
            lines.append(_ass_text(x0 + 36, (row_y0 + row_y1) // 2, _trunc(lbl, 48), size=19, color=_ASS_TEXT, align=4))

    # Footer hint
    fy = y1 - ftr_h
    lines.append(_ass_rect(x0 + 6, fy, x1 - 2, fy + 1, _ASS_SUBTLE))
    lines.append(_ass_text((x0 + x1) // 2, fy + ftr_h // 2, "Up/Down: Navigate   •   Enter: Select   •   Esc: Resume", size=16, color=_ASS_MUTED, align=5))
    return "\n".join(lines)


class MpvIpcClient:
    """Persistent UNIX-socket JSON IPC client for mpv with request_id matching.

    Keeping a single socket connected for the lifetime of playback is required by
    mpv so `osd-overlay` graphics stay visible instead of vanishing on socket close.
    """

    def __init__(self, sock_path: str, timeout: float = 0.25):
        self.sock_path = str(sock_path or "")
        self.timeout = float(timeout)
        self._sock: Optional[socket.socket] = None
        self._buf: bytearray = bytearray()
        self._next_rid: int = 1

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> bool:
        if self._sock is not None:
            return True
        if not self.sock_path or not os.path.exists(self.sock_path):
            return False
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(self.timeout)
            s.connect(self.sock_path)
            self._sock = s
            self._buf.clear()
            return True
        except OSError:
            try:
                s.close()
            except OSError:
                pass
            return False

    def close(self) -> None:
        s = self._sock
        self._sock = None
        self._buf.clear()
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def _drain_pending_events(self) -> None:
        """Non-blocking drain of unsolicited mpv event lines so buffer stays tiny."""
        if self._sock is None:
            return
        try:
            self._sock.setblocking(False)
            while True:
                chunk = self._sock.recv(8192)
                if not chunk:
                    self.close()
                    return
                self._buf.extend(chunk)
                if len(self._buf) > 65536:
                    del self._buf[:-16384]
        except BlockingIOError:
            pass
        except OSError:
            self.close()
        finally:
            if self._sock is not None:
                try:
                    self._sock.settimeout(self.timeout)
                except OSError:
                    self.close()
        # Discard complete lines that are just async events
        nl = self._buf.rfind(b"\n")
        if nl >= 0:
            del self._buf[: nl + 1]

    def request(self, command: Any, wait_reply: bool = True) -> Optional[dict]:
        """Send an mpv IPC command (list or named-arg dict) and optionally await reply."""
        if not self.connect() or self._sock is None:
            return None
        self._drain_pending_events()
        if self._sock is None:
            return None

        rid = self._next_rid
        self._next_rid += 1
        payload = (json.dumps({"command": command, "request_id": rid}) + "\n").encode("utf-8")
        try:
            self._sock.sendall(payload)
        except OSError:
            self.close()
            return None

        if not wait_reply:
            return {"error": "sent", "request_id": rid}

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                raw_ln = bytes(self._buf[:nl]).decode("utf-8", "ignore").strip()
                del self._buf[: nl + 1]
                if not raw_ln:
                    continue
                try:
                    obj = json.loads(raw_ln)
                except Exception:
                    continue
                if isinstance(obj, dict) and obj.get("request_id") == rid:
                    return obj
                continue
            rem = max(0.01, deadline - time.monotonic())
            try:
                self._sock.settimeout(rem)
                chunk = self._sock.recv(4096)
                if not chunk:
                    self.close()
                    return None
                self._buf.extend(chunk)
            except socket.timeout:
                break
            except OSError:
                self.close()
                return None
        return None

    def command(self, *args: Any) -> bool:
        resp = self.request(list(args), wait_reply=False)
        return resp is not None

    def get_property(self, prop_name: str, default: Any = None) -> Any:
        resp = self.request(["get_property", str(prop_name)], wait_reply=True)
        if isinstance(resp, dict) and resp.get("error") == "success":
            return resp.get("data", default)
        return default

    def set_property(self, prop_name: str, value: Any) -> bool:
        resp = self.request(["set_property", str(prop_name), value], wait_reply=False)
        return resp is not None

    def set_osd_overlay(
        self,
        overlay_id: int,
        ass_data: str,
        res_x: int = OSD_RES_X,
        res_y: int = OSD_RES_Y,
    ) -> bool:
        cmd = {
            "name": "osd-overlay",
            "id": int(overlay_id),
            "format": "ass-events",
            "res_x": int(res_x),
            "res_y": int(res_y),
            "data": str(ass_data or ""),
        }
        resp = self.request(cmd, wait_reply=False)
        return resp is not None

    def clear_osd_overlay(self, overlay_id: int) -> bool:
        cmd = {
            "name": "osd-overlay",
            "id": int(overlay_id),
            "format": "none",
            "data": "",
        }
        resp = self.request(cmd, wait_reply=False)
        return resp is not None
