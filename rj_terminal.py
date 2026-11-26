#!/usr/bin/env python3
"""
RaspyJack Integrated Pure-Python VT100/ANSI PTY Terminal
--------------------------------------------------------
Zero external library dependencies (stdlib pty/termios/fcntl/select + PIL).

Features:
  - Full interactive PTY bash/sh session with dynamic TIOCSWINSZ sizing
  - VT100 / xterm-256color ANSI screen buffer (cursor movement, clear,
    scroll regions, alternate screen for vim/nano/htop/less, OSC titles,
    16/256/24-bit color mapped to eye-friendly Obsidian Ember tones)
  - 1,000-line scrollback buffer (PageUp/PageDown or Shift+Up/Down)
  - Persistent background session: F3 / Ctrl+Q detaches back to RaspyJack
    Main Menu while keeping the shell alive; `exit` or Ctrl+D closes it
  - Quick-Command overlay (F1 / KEY1) for instant shortcuts & button-only use
"""

from collections import deque
import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw

import rj_env
import rj_input
import rj_keyboard
import rj_theme

DEFAULT_FG = rj_theme.EMBER_RGB["text_primary"]
DEFAULT_BG = rj_theme.EMBER_RGB["bg_well"]
CURSOR_BG = rj_theme.EMBER_RGB["accent"]
CURSOR_FG = rj_theme.EMBER_RGB["bg_base"]

# Cell tuple: (char, fg_rgb, bg_rgb, bold, dim, inverse, underline)
BLANK_CELL = (" ", DEFAULT_FG, DEFAULT_BG, False, False, False, False)

QUICK_COMMANDS: List[Tuple[str, str]] = [
    ("Interfaces (ip -br a)", "ip -br a\n"),
    ("WiFi Devices (iw dev)", "iw dev\n"),
    ("Listening Ports (ss)", "ss -tulpn\n"),
    ("Disk Usage (df -h)", "df -h\n"),
    ("Memory (free -h)", "free -h\n"),
    ("Clear Screen", "\x0c"),
    ("Send Ctrl+C", "\x03"),
    ("Restart Shell", "__RESTART__"),
    ("Close Shell", "__CLOSE__"),
]


class AnsiScreenBuffer:
    """Thread-safe pure-Python VT100/ANSI cell grid with scrollback & alt screen."""

    def __init__(self, rows: int = 20, cols: int = 76, max_scrollback: int = 1000):
        self.rows = max(4, int(rows))
        self.cols = max(16, int(cols))
        self.max_scrollback = max(50, int(max_scrollback))
        self.lock = threading.RLock()

        self.grid: List[List[tuple]] = self._make_grid(self.rows, self.cols)
        self._row_ver_seq: int = 1
        self.row_versions: List[int] = [0] * self.rows
        self.scrollback: deque = deque(maxlen=self.max_scrollback)
        self._scrollback_vers: deque = deque(maxlen=self.max_scrollback)
        self.alt_grid: Optional[List[List[tuple]]] = None
        self.alt_row_versions: Optional[List[int]] = None
        self.in_alt_screen: bool = False

        self.cx: int = 0
        self.cy: int = 0
        self.saved_cursor: Tuple[int, int] = (0, 0)
        self.saved_main_cursor: Tuple[int, int] = (0, 0)
        self.cursor_visible: bool = True
        self.app_cursor_keys: bool = False
        self.pending_wrap: bool = False

        self.scroll_top: int = 0
        self.scroll_bottom: int = self.rows - 1
        self.scroll_offset: int = 0

        # Active SGR attributes
        self.fg: Tuple[int, int, int] = DEFAULT_FG
        self.bg: Tuple[int, int, int] = DEFAULT_BG
        self.bold: bool = False
        self.dim: bool = False
        self.inverse: bool = False
        self.underline: bool = False

        self.title: str = "bash"
        self.dirty: bool = True
        self._utf8_buf = bytearray()

        # Parser state machine
        self._state: str = "NORMAL"
        self._csi_buf: str = ""
        self._osc_buf: str = ""
        self._osc_esc: bool = False

    @staticmethod
    def _blank_row(cols: int) -> List[tuple]:
        return [BLANK_CELL] * cols

    def _make_grid(self, rows: int, cols: int) -> List[List[tuple]]:
        return [self._blank_row(cols) for _ in range(rows)]

    def _next_row_ver(self) -> int:
        self._row_ver_seq += 1
        return self._row_ver_seq

    def _touch_row(self, r: int) -> None:
        if 0 <= r < self.rows:
            self._row_ver_seq += 1
            self.row_versions[r] = self._row_ver_seq

    def _cur_blank(self) -> tuple:
        return (" ", self.fg, self.bg, False, False, False, False)

    def resize(self, new_rows: int, new_cols: int) -> None:
        with self.lock:
            nr = max(4, int(new_rows))
            nc = max(16, int(new_cols))
            if nr == self.rows and nc == self.cols:
                return
            old = self.grid
            new_grid = self._make_grid(nr, nc)
            # Preserve bottom rows if shrinking vertically
            start_r = max(0, len(old) - nr)
            for dst_r, src_r in enumerate(range(start_r, len(old))):
                if dst_r >= nr:
                    break
                row = old[src_r]
                copy_c = min(len(row), nc)
                new_grid[dst_r][:copy_c] = row[:copy_c]
            self.grid = new_grid
            self.rows = nr
            self.cols = nc
            self.row_versions = [self._next_row_ver() for _ in range(nr)]
            self.cy = max(0, min(nr - 1, self.cy - start_r))
            self.cx = max(0, min(nc - 1, self.cx))
            self.scroll_top = 0
            self.scroll_bottom = nr - 1
            self.pending_wrap = False
            self.scroll_offset = 0
            self.dirty = True

    def clear_all(self) -> None:
        with self.lock:
            self.grid = self._make_grid(self.rows, self.cols)
            self.row_versions = [0] * self.rows
            self.cx = 0
            self.cy = 0
            self.pending_wrap = False
            self.scroll_offset = 0
            self.dirty = True

    def scroll_view(self, delta: int) -> None:
        """Scroll viewport up (+delta) or down (-delta) through scrollback."""
        with self.lock:
            if self.in_alt_screen:
                self.scroll_offset = 0
                return
            max_off = len(self.scrollback)
            new_off = max(0, min(max_off, self.scroll_offset + int(delta)))
            if new_off != self.scroll_offset:
                self.scroll_offset = new_off
                self.dirty = True

    def reset_scroll(self) -> None:
        with self.lock:
            if self.scroll_offset != 0:
                self.scroll_offset = 0
                self.dirty = True

    def _scroll_up_region(self, count: int = 1) -> None:
        top, bot = self.scroll_top, self.scroll_bottom
        n = max(1, min(count, bot - top + 1))
        for _ in range(n):
            removed = self.grid.pop(top)
            removed_v = self.row_versions.pop(top)
            if not self.in_alt_screen and top == 0 and bot == self.rows - 1:
                self.scrollback.append(removed)
                self._scrollback_vers.append(removed_v)
            self.grid.insert(bot, self._blank_row(self.cols))
            self.row_versions.insert(bot, 0)

    def _scroll_down_region(self, count: int = 1) -> None:
        top, bot = self.scroll_top, self.scroll_bottom
        n = max(1, min(count, bot - top + 1))
        for _ in range(n):
            self.grid.pop(bot)
            self.row_versions.pop(bot)
            self.grid.insert(top, self._blank_row(self.cols))
            self.row_versions.insert(top, 0)

    def _line_feed(self) -> None:
        self.pending_wrap = False
        if self.cy == self.scroll_bottom:
            self._scroll_up_region(1)
        elif self.cy < self.rows - 1:
            self.cy += 1

    def _reverse_index(self) -> None:
        self.pending_wrap = False
        if self.cy == self.scroll_top:
            self._scroll_down_region(1)
        elif self.cy > 0:
            self.cy -= 1

    def _put_char(self, ch: str) -> None:
        if self.pending_wrap:
            self.cx = 0
            self._line_feed()
            self.pending_wrap = False
        if 0 <= self.cy < self.rows and 0 <= self.cx < self.cols:
            self.grid[self.cy][self.cx] = (
                ch, self.fg, self.bg, self.bold, self.dim, self.inverse, self.underline
            )
            self._touch_row(self.cy)
        if self.cx >= self.cols - 1:
            self.pending_wrap = True
        else:
            self.cx += 1

    def _reset_sgr(self) -> None:
        self.fg = DEFAULT_FG
        self.bg = DEFAULT_BG
        self.bold = False
        self.dim = False
        self.inverse = False
        self.underline = False

    def _handle_sgr(self, params: List[int]) -> None:
        if not params:
            self._reset_sgr()
            return
        i = 0
        n = len(params)
        while i < n:
            p = params[i]
            if p == 0:
                self._reset_sgr()
            elif p == 1:
                self.bold = True
            elif p == 2:
                self.dim = True
            elif p == 4:
                self.underline = True
            elif p == 7:
                self.inverse = True
            elif p == 22:
                self.bold = False
                self.dim = False
            elif p == 24:
                self.underline = False
            elif p == 27:
                self.inverse = False
            elif 30 <= p <= 37:
                self.fg = rj_theme.ANSI_16_EMBER[p - 30]
            elif p == 39:
                self.fg = DEFAULT_FG
            elif 40 <= p <= 47:
                self.bg = rj_theme.ANSI_16_EMBER[p - 40]
            elif p == 49:
                self.bg = DEFAULT_BG
            elif 90 <= p <= 97:
                self.fg = rj_theme.ANSI_16_EMBER[8 + (p - 90)]
            elif 100 <= p <= 107:
                self.bg = rj_theme.ANSI_16_EMBER[8 + (p - 100)]
            elif p in (38, 48):
                is_fg = p == 38
                if i + 1 < n and params[i + 1] == 5 and i + 2 < n:
                    col = rj_theme.ansi_256_to_rgb(params[i + 2])
                    if is_fg:
                        self.fg = col
                    else:
                        self.bg = col
                    i += 2
                elif i + 1 < n and params[i + 1] == 2 and i + 4 < n:
                    col = (
                        max(0, min(255, params[i + 2])),
                        max(0, min(255, params[i + 3])),
                        max(0, min(255, params[i + 4])),
                    )
                    if is_fg:
                        self.fg = col
                    else:
                        self.bg = col
                    i += 4
            i += 1

    def _enter_alt_screen(self) -> None:
        if self.in_alt_screen:
            return
        self.alt_grid = self.grid
        self.alt_row_versions = self.row_versions
        self.saved_main_cursor = (self.cx, self.cy)
        self.grid = self._make_grid(self.rows, self.cols)
        self.row_versions = [0] * self.rows
        self.cx = 0
        self.cy = 0
        self.scroll_top = 0
        self.scroll_bottom = self.rows - 1
        self.scroll_offset = 0
        self.pending_wrap = False
        self.in_alt_screen = True

    def _leave_alt_screen(self) -> None:
        if not self.in_alt_screen:
            return
        if self.alt_grid is not None:
            self.grid = self.alt_grid
            self.alt_grid = None
        if self.alt_row_versions is not None and len(self.alt_row_versions) == self.rows:
            self.row_versions = self.alt_row_versions
        else:
            self.row_versions = [self._next_row_ver() for _ in range(self.rows)]
        self.alt_row_versions = None
        self.cx, self.cy = self.saved_main_cursor
        self.cx = max(0, min(self.cols - 1, self.cx))
        self.cy = max(0, min(self.rows - 1, self.cy))
        self.scroll_top = 0
        self.scroll_bottom = self.rows - 1
        self.pending_wrap = False
        self.in_alt_screen = False

    def _dispatch_csi(self, raw: str, cmd: str) -> None:
        private = raw.startswith("?")
        clean = raw[1:] if private else raw
        # Strip intermediate chars like '>' or '!'
        if clean and clean[0] in ">!<=":
            return
        parts = clean.split(";") if clean else []
        params: List[int] = []
        for part in parts:
            try:
                params.append(int(part) if part else 0)
            except ValueError:
                params.append(0)

        def p1(idx: int = 0, default: int = 1) -> int:
            if idx < len(params) and params[idx] > 0:
                return params[idx]
            return default

        def p0(idx: int = 0, default: int = 0) -> int:
            if idx < len(params):
                return params[idx]
            return default

        if private:
            if cmd in ("h", "l"):
                enable = cmd == "h"
                for code in params:
                    if code == 25:
                        self.cursor_visible = enable
                    elif code == 1:
                        self.app_cursor_keys = enable
                    elif code in (1049, 1047, 47):
                        if enable:
                            self._enter_alt_screen()
                        else:
                            self._leave_alt_screen()
            return

        if cmd == "m":
            self._handle_sgr(params)
        elif cmd == "A":  # CUU
            self.pending_wrap = False
            self.cy = max(self.scroll_top, self.cy - p1())
        elif cmd == "B":  # CUD
            self.pending_wrap = False
            self.cy = min(self.scroll_bottom, self.cy + p1())
        elif cmd == "C":  # CUF
            self.pending_wrap = False
            self.cx = min(self.cols - 1, self.cx + p1())
        elif cmd == "D":  # CUB
            self.pending_wrap = False
            self.cx = max(0, self.cx - p1())
        elif cmd == "E":  # CNL
            self.pending_wrap = False
            self.cy = min(self.scroll_bottom, self.cy + p1())
            self.cx = 0
        elif cmd == "F":  # CPL
            self.pending_wrap = False
            self.cy = max(self.scroll_top, self.cy - p1())
            self.cx = 0
        elif cmd == "G":  # CHA
            self.pending_wrap = False
            self.cx = max(0, min(self.cols - 1, p1() - 1))
        elif cmd == "d":  # VPA
            self.pending_wrap = False
            self.cy = max(0, min(self.rows - 1, p1() - 1))
        elif cmd in ("H", "f"):  # CUP / HVP
            self.pending_wrap = False
            r = p1(0, 1) - 1
            c = p1(1, 1) - 1
            self.cy = max(0, min(self.rows - 1, r))
            self.cx = max(0, min(self.cols - 1, c))
        elif cmd == "J":  # ED
            mode = p0(0, 0)
            blank = self._cur_blank()
            blank_ver = 0 if blank == BLANK_CELL else self._next_row_ver()
            if mode == 0:
                for c in range(self.cx, self.cols):
                    self.grid[self.cy][c] = blank
                self._touch_row(self.cy)
                for r in range(self.cy + 1, self.rows):
                    self.grid[r] = [blank] * self.cols
                    self.row_versions[r] = blank_ver
            elif mode == 1:
                for r in range(0, self.cy):
                    self.grid[r] = [blank] * self.cols
                    self.row_versions[r] = blank_ver
                for c in range(0, self.cx + 1):
                    self.grid[self.cy][c] = blank
                self._touch_row(self.cy)
            elif mode in (2, 3):
                for r in range(self.rows):
                    self.grid[r] = [blank] * self.cols
                    self.row_versions[r] = blank_ver
                if mode == 3:
                    self.scrollback.clear()
                    self._scrollback_vers.clear()
                    self.scroll_offset = 0
        elif cmd == "K":  # EL
            mode = p0(0, 0)
            blank = self._cur_blank()
            if mode == 0:
                for c in range(self.cx, self.cols):
                    self.grid[self.cy][c] = blank
                self._touch_row(self.cy)
            elif mode == 1:
                for c in range(0, self.cx + 1):
                    self.grid[self.cy][c] = blank
                self._touch_row(self.cy)
            elif mode == 2:
                self.grid[self.cy] = [blank] * self.cols
                self.row_versions[self.cy] = 0 if blank == BLANK_CELL else self._next_row_ver()
        elif cmd == "L":  # IL
            n = p1()
            if self.scroll_top <= self.cy <= self.scroll_bottom:
                for _ in range(min(n, self.scroll_bottom - self.cy + 1)):
                    self.grid.pop(self.scroll_bottom)
                    self.row_versions.pop(self.scroll_bottom)
                    self.grid.insert(self.cy, self._blank_row(self.cols))
                    self.row_versions.insert(self.cy, 0)
        elif cmd == "M":  # DL
            n = p1()
            if self.scroll_top <= self.cy <= self.scroll_bottom:
                for _ in range(min(n, self.scroll_bottom - self.cy + 1)):
                    self.grid.pop(self.cy)
                    self.row_versions.pop(self.cy)
                    self.grid.insert(self.scroll_bottom, self._blank_row(self.cols))
                    self.row_versions.insert(self.scroll_bottom, 0)
        elif cmd == "P":  # DCH
            n = min(p1(), self.cols - self.cx)
            row = self.grid[self.cy]
            del row[self.cx : self.cx + n]
            row.extend([self._cur_blank()] * n)
            self._touch_row(self.cy)
        elif cmd == "@":  # ICH
            n = min(p1(), self.cols - self.cx)
            row = self.grid[self.cy]
            row[self.cx : self.cx] = [self._cur_blank()] * n
            del row[self.cols :]
            self._touch_row(self.cy)
        elif cmd == "X":  # ECH
            n = min(p1(), self.cols - self.cx)
            blank = self._cur_blank()
            for c in range(self.cx, self.cx + n):
                self.grid[self.cy][c] = blank
            self._touch_row(self.cy)
        elif cmd == "S":  # SU
            self._scroll_up_region(p1())
        elif cmd == "T":  # SD
            self._scroll_down_region(p1())
        elif cmd == "r":  # DECSTBM
            top = p1(0, 1) - 1
            bot = p1(1, self.rows) - 1
            if 0 <= top < bot < self.rows:
                self.scroll_top = top
                self.scroll_bottom = bot
            else:
                self.scroll_top = 0
                self.scroll_bottom = self.rows - 1
            self.cx = 0
            self.cy = 0
            self.pending_wrap = False
        elif cmd == "s":
            self.saved_cursor = (self.cx, self.cy)
        elif cmd == "u":
            self.cx, self.cy = self.saved_cursor
            self.cx = max(0, min(self.cols - 1, self.cx))
            self.cy = max(0, min(self.rows - 1, self.cy))

    def feed(self, data) -> None:
        """Feed raw UTF-8 / ANSI bytes (or str) from PTY into the screen buffer."""
        if not data:
            return
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        self._utf8_buf.extend(data)
        try:
            text = self._utf8_buf.decode("utf-8")
            self._utf8_buf.clear()
        except UnicodeDecodeError as exc:
            # Decode complete prefix; keep trailing incomplete multibyte sequence
            text = self._utf8_buf[: exc.start].decode("utf-8", "replace")
            tail = self._utf8_buf[exc.start :]
            if len(tail) > 4:
                text += tail.decode("utf-8", "replace")
                self._utf8_buf.clear()
            else:
                self._utf8_buf = bytearray(tail)

        if not text:
            return

        with self.lock:
            i = 0
            n_text = len(text)
            while i < n_text:
                ch = text[i]
                if self._state == "NORMAL":
                    code = ord(ch)
                    if 32 <= code < 127:
                        # Fast-path contiguous printable ASCII runs without per-char state dispatch
                        if self.pending_wrap:
                            self.cx = 0
                            self._line_feed()
                            self.pending_wrap = False
                        row = self.grid[self.cy]
                        cols = self.cols
                        cx = self.cx
                        fg = self.fg
                        bg = self.bg
                        bold = self.bold
                        dim = self.dim
                        inv = self.inverse
                        ul = self.underline
                        touched = False
                        while i < n_text:
                            c = text[i]
                            oc = ord(c)
                            if not (32 <= oc < 127):
                                break
                            if self.pending_wrap:
                                self.cx = 0
                                if touched:
                                    self._touch_row(self.cy)
                                    touched = False
                                self._line_feed()
                                self.pending_wrap = False
                                row = self.grid[self.cy]
                                cx = 0
                            row[cx] = (c, fg, bg, bold, dim, inv, ul)
                            touched = True
                            if cx >= cols - 1:
                                self.pending_wrap = True
                            else:
                                cx += 1
                            i += 1
                        self.cx = cx
                        if touched:
                            self._touch_row(self.cy)
                        continue
                    if ch == "\x1b":
                        self._state = "ESC"
                    elif ch == "\r":
                        self.cx = 0
                        self.pending_wrap = False
                    elif ch == "\n" or ch == "\x0b" or ch == "\x0c":
                        self._line_feed()
                    elif ch == "\b":
                        self.pending_wrap = False
                        if self.cx > 0:
                            self.cx -= 1
                    elif ch == "\t":
                        self.pending_wrap = False
                        nxt = ((self.cx // 8) + 1) * 8
                        self.cx = min(self.cols - 1, nxt)
                    elif ch == "\x07":
                        pass  # Bell
                    elif code >= 32 and code != 127:
                        self._put_char(ch)

                elif self._state == "ESC":
                    if ch == "[":
                        self._state = "CSI"
                        self._csi_buf = ""
                    elif ch == "]":
                        self._state = "OSC"
                        self._osc_buf = ""
                        self._osc_esc = False
                    elif ch in ("(", ")", "*", "+"):
                        self._state = "CHARSET"
                    elif ch == "7":
                        self.saved_cursor = (self.cx, self.cy)
                        self._state = "NORMAL"
                    elif ch == "8":
                        self.cx, self.cy = self.saved_cursor
                        self._state = "NORMAL"
                    elif ch == "D":
                        self._line_feed()
                        self._state = "NORMAL"
                    elif ch == "M":
                        self._reverse_index()
                        self._state = "NORMAL"
                    elif ch == "E":
                        self.cx = 0
                        self._line_feed()
                        self._state = "NORMAL"
                    elif ch == "c":
                        self._reset_sgr()
                        self.clear_all()
                        self._state = "NORMAL"
                    else:
                        self._state = "NORMAL"

                elif self._state == "CHARSET":
                    self._state = "NORMAL"

                elif self._state == "CSI":
                    o = ord(ch)
                    if 0x40 <= o <= 0x7E:
                        self._dispatch_csi(self._csi_buf, ch)
                        self._state = "NORMAL"
                        self._csi_buf = ""
                    elif len(self._csi_buf) < 64:
                        self._csi_buf += ch
                    else:
                        self._state = "NORMAL"
                        self._csi_buf = ""

                elif self._state == "OSC":
                    if ch == "\x07":
                        self._finish_osc()
                        self._state = "NORMAL"
                    elif self._osc_esc and ch == "\\":
                        self._finish_osc()
                        self._state = "NORMAL"
                    elif ch == "\x1b":
                        self._osc_esc = True
                    else:
                        self._osc_esc = False
                        if len(self._osc_buf) < 256:
                            self._osc_buf += ch
                i += 1

            self.dirty = True

    def _finish_osc(self) -> None:
        buf = self._osc_buf
        self._osc_buf = ""
        self._osc_esc = False
        if ";" in buf:
            code, val = buf.split(";", 1)
            if code in ("0", "2") and val.strip():
                self.title = val.strip()[:60]

    def get_visible_rows_with_versions(self) -> Tuple[List[List[tuple]], List[int], int, int, bool, int]:
        """Return (visible_rows, row_versions, cursor_x, cursor_y, show_cursor, scroll_offset)."""
        with self.lock:
            if self.in_alt_screen or self.scroll_offset == 0:
                rows_copy = [list(r) for r in self.grid]
                vers_copy = list(self.row_versions)
                return rows_copy, vers_copy, self.cx, self.cy, self.cursor_visible, 0

            sb = list(self.scrollback)
            sb_vers = list(self._scrollback_vers)
            if len(sb_vers) < len(sb):
                sb_vers = [self._next_row_ver() for _ in range(len(sb) - len(sb_vers))] + sb_vers
            combined = sb + self.grid
            combined_vers = sb_vers + self.row_versions
            total = len(combined)
            end = max(self.rows, total - self.scroll_offset)
            start = max(0, end - self.rows)
            view = [list(r) for r in combined[start:end]]
            view_vers = list(combined_vers[start:end])
            while len(view) < self.rows:
                view.append(self._blank_row(self.cols))
                view_vers.append(0)
            return view, view_vers, -1, -1, False, self.scroll_offset

    def get_visible_rows(self) -> Tuple[List[List[tuple]], int, int, bool, int]:
        """Return (visible_rows, cursor_x, cursor_y, show_cursor, scroll_offset)."""
        view, _vers, cx, cy, show_cursor, scroll_off = self.get_visible_rows_with_versions()
        return view, cx, cy, show_cursor, scroll_off

    def get_plain_lines(self) -> List[str]:
        """Return plain stripped text lines of the visible screen (handy for tests)."""
        rows_copy, *_ = self.get_visible_rows()
        return ["".join(cell[0] for cell in r).rstrip() for r in rows_copy]


# ---------------------------------------------------------------------------
# Persistent PTY Session Manager
# ---------------------------------------------------------------------------

class PersistentTerminalSession:
    """Manages a live Linux PTY shell that can detach & resume seamlessly."""

    def __init__(self):
        self.master_fd: Optional[int] = None
        self.proc: Optional[subprocess.Popen] = None
        self.screen: Optional[AnsiScreenBuffer] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self.exit_code: Optional[int] = None

    def is_alive(self) -> bool:
        with self._lock:
            return (
                self.proc is not None
                and self.proc.poll() is None
                and self.master_fd is not None
            )

    @property
    def pid(self) -> int:
        return int(self.proc.pid) if self.proc is not None else 0

    def ensure_started(self, rows: int, cols: int, cwd: Optional[str] = None) -> AnsiScreenBuffer:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None and self.screen is not None:
                self._resize_unlocked(rows, cols)
                return self.screen

            self._close_unlocked()
            self._stop_evt.clear()
            self.exit_code = None
            self.screen = AnsiScreenBuffer(rows=rows, cols=cols)

            master_fd, slave_fd = pty.openpty()
            try:
                fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            except Exception:
                pass
            # Ensure standard cooked/echo/signal TTY flags on slave_fd
            try:
                attrs = termios.tcgetattr(slave_fd)
                attrs[0] |= termios.ICRNL
                attrs[1] |= termios.OPOST | termios.ONLCR
                attrs[3] |= termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN
                termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
            except Exception:
                pass

            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            env["COLORTERM"] = "truecolor"
            env["COLUMNS"] = str(cols)
            env["LINES"] = str(rows)
            env.setdefault("LANG", "C.UTF-8")
            if not env.get("XDG_RUNTIME_DIR") or not os.path.isdir(env["XDG_RUNTIME_DIR"]):
                _euid = os.geteuid()
                _rdir = f"/run/user/{_euid}"
                if not os.path.isdir(_rdir):
                    _rdir = f"/tmp/rj-runtime-{_euid}"
                    try:
                        os.makedirs(_rdir, mode=0o700, exist_ok=True)
                    except OSError:
                        pass
                if os.path.isdir(_rdir):
                    env["XDG_RUNTIME_DIR"] = _rdir

            shell_bin = os.environ.get("SHELL", "").strip()
            if (
                not shell_bin
                or not os.path.isfile(shell_bin)
                or not os.access(shell_bin, os.X_OK)
                or os.path.basename(shell_bin) in ("nologin", "false")
            ):
                shell_bin = "/bin/bash" if os.path.isfile("/bin/bash") else "/bin/sh"
            args = [shell_bin, "-i"] if os.path.basename(shell_bin) in ("bash", "zsh", "sh") else [shell_bin]

            work_dir = cwd or rj_env.get_base_dir()
            if not os.path.isdir(work_dir):
                work_dir = os.path.expanduser("~")

            self.screen.title = f"{os.path.basename(shell_bin)} — {os.path.basename(work_dir) or work_dir}"

            def _pty_child_setup() -> None:
                os.setsid()
                try:
                    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                except Exception:
                    pass

            self.proc = subprocess.Popen(
                args,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=work_dir,
                env=env,
                preexec_fn=_pty_child_setup,
                close_fds=True,
            )
            os.close(slave_fd)
            os.set_blocking(master_fd, False)
            self.master_fd = master_fd

            self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
            self._reader_thread.start()
            return self.screen

    def _read_loop(self) -> None:
        while not self._stop_evt.is_set():
            fd = self.master_fd
            if fd is None:
                break
            try:
                ready, _, _ = select.select([fd], [], [], 0.25)
            except (OSError, ValueError):
                break
            if not ready:
                if self.proc is not None and self.proc.poll() is not None:
                    self.exit_code = self.proc.returncode
                    break
                continue
            try:
                chunk = os.read(fd, 8192)
                if not chunk:
                    break
                if self.screen is not None:
                    self.screen.feed(chunk)
            except BlockingIOError:
                continue
            except OSError:
                break
        if self.proc is not None and self.proc.poll() is not None:
            self.exit_code = self.proc.returncode

    def _resize_unlocked(self, rows: int, cols: int) -> None:
        if self.screen is not None:
            self.screen.resize(rows, cols)
        if self.master_fd is not None:
            try:
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            except Exception:
                pass
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGWINCH)
            except Exception:
                pass

    def resize(self, rows: int, cols: int) -> None:
        with self._lock:
            self._resize_unlocked(rows, cols)

    def write(self, data: bytes) -> bool:
        if not data:
            return True
        fd = self.master_fd
        if fd is None:
            return False
        try:
            os.write(fd, data)
            if self.screen is not None:
                self.screen.reset_scroll()
            return True
        except OSError:
            return False

    def _close_unlocked(self) -> None:
        self._stop_evt.set()
        fd = self.master_fd
        self.master_fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        proc = self.proc
        self.proc = None
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            try:
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()

    def restart(self, rows: int, cols: int, cwd: Optional[str] = None) -> AnsiScreenBuffer:
        self.close()
        return self.ensure_started(rows, cols, cwd=cwd)


_SHARED_SESSION = PersistentTerminalSession()


def get_shared_session() -> PersistentTerminalSession:
    return _SHARED_SESSION


def is_shared_session_alive() -> bool:
    return _SHARED_SESSION.is_alive()


def close_shared_session() -> None:
    _SHARED_SESSION.close()


# ---------------------------------------------------------------------------
# Terminal Layout Metrics & Key Encoding
# ---------------------------------------------------------------------------

def compute_terminal_metrics(width: int, height: int) -> dict:
    """Compute crisp font sizes, header/footer heights, and (rows, cols)."""
    compact = width < 260 or height < 160
    if width >= 560 and height >= 300:
        font_px = 13
    elif width >= 300 and height >= 160:
        font_px = 10
    elif width >= 200:
        font_px = 9
    else:
        font_px = 7

    mono_font = rj_env.load_font(font_px, mono=True)
    mono_bold = rj_env.load_font(font_px, bold=True, mono=True)
    ui_font = rj_env.load_font(max(8, font_px - 2), bold=False)
    ui_bold = rj_env.load_font(max(8, font_px - 1), bold=True)

    probe = ImageDraw.Draw(Image.new("RGB", (64, 64)))
    mb = probe.textbbox((0, 0), "M", font=mono_font)
    gb = probe.textbbox((0, 0), "Mg", font=mono_font)
    char_w_f = (probe.textlength("MMMMMMMMMM", font=mono_font) / 10.0) if hasattr(probe, "textlength") else float(mb[2] - mb[0])
    char_w_f = max(4.5, char_w_f)
    char_w = max(5, int(round(char_w_f)))
    line_h = max(font_px + 2, (gb[3] - gb[1]) + 3)

    hdr_h = max(14, line_h + 6) if not compact else max(12, line_h + 2)
    ftr_h = max(14, line_h + 3) if not compact else max(11, line_h + 1)
    pad_x = 6 if not compact else 2
    pad_y = 4 if not compact else 2
    sb_reserve = 6 if not compact else 3

    cols = max(18, int((width - pad_x * 2 - sb_reserve) // char_w_f))
    rows = max(6, (height - hdr_h - ftr_h - pad_y * 2) // line_h)

    return {
        "compact": compact,
        "font_px": font_px,
        "mono_font": mono_font,
        "mono_bold": mono_bold,
        "ui_font": ui_font,
        "ui_bold": ui_bold,
        "char_w": char_w,
        "char_w_f": char_w_f,
        "line_h": line_h,
        "hdr_h": hdr_h,
        "ftr_h": ftr_h,
        "pad_x": pad_x,
        "pad_y": pad_y,
        "cols": cols,
        "rows": rows,
    }


def encode_text_event_to_pty(ev: dict, app_cursor: bool = False) -> bytes:
    """Translate an rj_input text_key event dict into raw PTY input bytes."""
    spec = str(ev.get("special") or "")
    key = str(ev.get("key") or "")
    mods = int(ev.get("mods", 0) or 0)
    ctrl_on = bool(mods & rj_keyboard.MOD_CTRL_ANY)
    alt_on = bool(mods & rj_keyboard.MOD_ALT_ANY)

    if spec:
        prefix = b"\x1bO" if app_cursor else b"\x1b["
        if spec == "ENTER":
            return b"\r"
        if spec == "BACKSPACE":
            return b"\x7f"
        if spec == "TAB":
            return b"\t"
        if spec == "ESCAPE":
            return b"\x1b"
        if spec == "DELETE":
            return b"\x1b[3~"
        if spec == "INSERT":
            return b"\x1b[2~"
        if spec == "HOME":
            return b"\x1b[H"
        if spec == "END":
            return b"\x1b[F"
        if spec == "PAGEUP":
            return b"\x1b[5~"
        if spec == "PAGEDOWN":
            return b"\x1b[6~"
        if spec == "UP":
            return prefix + b"A"
        if spec == "DOWN":
            return prefix + b"B"
        if spec == "RIGHT":
            return prefix + b"C"
        if spec == "LEFT":
            return prefix + b"D"
        if spec == "CTRL_SPACE":
            return b"\x00"
        if spec == "CTRL_LBRACKET":
            return b"\x1b"
        if spec == "CTRL_BACKSLASH":
            return b"\x1c"
        if spec == "CTRL_RBRACKET":
            return b"\x1d"
        _fn_seq = {
            "F4": b"\x1bOS",
            "F5": b"\x1b[15~",
            "F6": b"\x1b[17~",
            "F7": b"\x1b[18~",
            "F8": b"\x1b[19~",
            "F9": b"\x1b[20~",
        }
        if spec in _fn_seq:
            return _fn_seq[spec]
        if spec.startswith("CTRL_") and len(spec) == 6:
            c = spec[5]
            if "A" <= c <= "Z":
                return bytes([ord(c) - ord("A") + 1])
        return b""

    if key:
        if ctrl_on and len(key) == 1:
            low = key.lower()
            if "a" <= low <= "z":
                return bytes([ord(low) - ord("a") + 1])
            if key == " ":
                return b"\x00"
        raw = key.encode("utf-8", "ignore")
        return (b"\x1b" + raw) if alt_on else raw
    return b""


# ---------------------------------------------------------------------------
# PIL Renderer (Pure function, easily unit-testable)
# ---------------------------------------------------------------------------

def _render_terminal_row_strip(
    row: List[tuple],
    width: int,
    line_h: int,
    pad_x: int,
    char_w_f: float,
    mono_font,
    mono_bold,
) -> Image.Image:
    """Render a single terminal row onto an isolated (width x line_h) PIL strip."""
    strip = Image.new("RGB", (width, line_h), DEFAULT_BG)
    d = ImageDraw.Draw(strip)
    c_idx = 0
    row_len = len(row)
    while c_idx < row_len:
        ch, fg, bg, bold, dim, inv, ul = row[c_idx]
        if inv:
            fg, bg = bg, fg
        if dim:
            fg = (fg[0] * 2 // 3, fg[1] * 2 // 3, fg[2] * 2 // 3)

        run_chars = [ch]
        end_c = c_idx + 1
        while end_c < row_len:
            n_ch, n_fg, n_bg, n_bold, n_dim, n_inv, n_ul = row[end_c]
            if n_inv:
                n_fg, n_bg = n_bg, n_fg
            if n_dim:
                n_fg = (n_fg[0] * 2 // 3, n_fg[1] * 2 // 3, n_fg[2] * 2 // 3)
            if (n_fg, n_bg, n_bold, n_ul) == (fg, bg, bold, ul):
                run_chars.append(n_ch)
                end_c += 1
            else:
                break

        rx = pad_x + int(round(c_idx * char_w_f))
        rx_end = pad_x + int(round(end_c * char_w_f))
        if bg != DEFAULT_BG:
            d.rectangle((rx, 0, max(rx, rx_end - 1), line_h - 1), fill=bg)
        run_text = "".join(run_chars)
        if run_text.strip():
            d.text((rx, 0), run_text, font=(mono_bold if bold else mono_font), fill=fg)
        if ul:
            d.line([(rx, line_h - 2), (max(rx, rx_end - 1), line_h - 2)], fill=fg, width=1)
        c_idx = end_c
    return strip


def render_terminal_frame(
    screen: AnsiScreenBuffer,
    width: int,
    height: int,
    metrics: Optional[dict] = None,
    cursor_blink_on: bool = True,
    quick_menu_open: bool = False,
    quick_index: int = 0,
    pid: int = 0,
) -> Image.Image:
    """Render the current AnsiScreenBuffer into an Obsidian Ember PIL Image.

    Uses a per-row versioned strip & base-canvas cache attached to *metrics* so:
      - Typing 1 character re-renders only 1 row instead of all 20 (~10x-18x faster).
      - Scrolling 1 line reuses 19 already-rendered row strips (~10x-15x faster).
      - Cursor blinking re-renders 0 rows (~50x-100x faster).
    """
    m = metrics or compute_terminal_metrics(width, height)
    hdr_h = m["hdr_h"]
    ftr_h = m["ftr_h"]
    pad_x = m["pad_x"]
    pad_y = m["pad_y"]
    char_w = m["char_w"]
    char_w_f = float(m.get("char_w_f", char_w))
    line_h = m["line_h"]
    mono_font = m["mono_font"]
    mono_bold = m["mono_bold"]
    ui_font = m["ui_font"]
    ui_bold = m["ui_bold"]
    compact = m["compact"]

    visible_rows, visible_vers, cx, cy, show_cursor, scroll_off = screen.get_visible_rows_with_versions()
    vp_y0 = hdr_h
    vp_y1 = height - ftr_h - 1
    top_y = vp_y0 + pad_y

    # Persistent incremental cache when caller supplies a reusable metrics dict
    cache = m.get("_render_cache") if metrics is not None else None
    if (
        cache is None
        or cache.get("screen_id") != id(screen)
        or cache.get("size") != (width, height)
    ):
        base_img = Image.new("RGB", (width, height), rj_theme.EMBER_RGB["bg_base"])
        bd = ImageDraw.Draw(base_img)
        bd.rectangle((0, vp_y0, width - 1, vp_y1), fill=DEFAULT_BG)
        cache = {
            "screen_id": id(screen),
            "size": (width, height),
            "base_img": base_img,
            "header_key": None,
            "footer_key": None,
            "sb_key": None,
            "displayed_vers": [],
            "row_strips": {},
            "blank_strip": Image.new("RGB", (width, line_h), DEFAULT_BG),
        }
        if metrics is not None:
            m["_render_cache"] = cache

    base_img = cache["base_img"]
    bd = ImageDraw.Draw(base_img)

    # 1. Header chrome (redrawn only when title / scroll / alt / pid changes)
    header_key = (screen.title or "shell", scroll_off, screen.in_alt_screen, pid)
    if cache["header_key"] != header_key:
        bd.rectangle((0, 0, width - 1, hdr_h - 1), fill=rj_theme.EMBER_THEME["bg_elevated"])
        bd.line([(0, hdr_h - 1), (width - 1, hdr_h - 1)], fill=rj_theme.EMBER_THEME["border_subtle"], width=1)

        badge_x = 4 if not compact else 2
        badge_text = ">_ TERM" if not compact else ">_"
        pw, _ph = rj_theme.draw_pill(
            bd,
            badge_x,
            max(1, (hdr_h - (m["font_px"] + 2)) // 2),
            badge_text,
            ui_bold,
            bg=rj_theme.EMBER_THEME["select_bg"],
            fg=rj_theme.EMBER_THEME["accent_bright"],
            outline=rj_theme.EMBER_THEME["border_accent"],
            pad_x=5 if not compact else 3,
            pad_y=1,
            radius=3,
        )

        right_x = width - 4
        if scroll_off > 0:
            sp_txt = f"SCROLL -{scroll_off}"
            sb = rj_theme.measure_text(ui_bold, sp_txt, bd)
            sp_w = (sb[2] - sb[0]) + 10
            right_x -= sp_w
            rj_theme.draw_pill(
                bd, right_x, max(1, (hdr_h - (m["font_px"] + 2)) // 2),
                sp_txt, ui_bold,
                bg=rj_theme.EMBER_THEME["warn_bg"],
                fg=rj_theme.EMBER_THEME["warn"],
                outline=rj_theme.EMBER_THEME["border_accent"],
                pad_x=5, pad_y=1, radius=3,
            )
            right_x -= 4
        elif screen.in_alt_screen:
            ap_txt = "ALT"
            ab = rj_theme.measure_text(ui_bold, ap_txt, bd)
            ap_w = (ab[2] - ab[0]) + 8
            right_x -= ap_w
            rj_theme.draw_pill(
                bd, right_x, max(1, (hdr_h - (m["font_px"] + 2)) // 2),
                ap_txt, ui_bold,
                bg=rj_theme.EMBER_THEME["bg_surface"],
                fg=rj_theme.EMBER_THEME["accent"],
                outline=rj_theme.EMBER_THEME["border_subtle"],
                pad_x=4, pad_y=1, radius=3,
            )
            right_x -= 4
        elif pid > 0 and not compact:
            pid_txt = f"PID {pid}"
            pb = rj_theme.measure_text(ui_font, pid_txt, bd)
            pw2 = pb[2] - pb[0]
            right_x -= pw2
            bd.text((right_x, (hdr_h - (pb[3] - pb[1])) // 2 - pb[1]), pid_txt, font=ui_font, fill=rj_theme.EMBER_THEME["text_dim"])
            right_x -= 6

        title_x = badge_x + pw + (6 if not compact else 3)
        title_max_w = max(20, right_x - title_x)
        title_str = rj_theme.truncate_text(bd, screen.title or "shell", title_max_w, ui_font)
        tb = rj_theme.measure_text(ui_font, title_str or "A", bd)
        bd.text((title_x, (hdr_h - (tb[3] - tb[1])) // 2 - tb[1]), title_str, font=ui_font, fill=rj_theme.EMBER_THEME["text_muted"])
        cache["header_key"] = header_key

    # 2. Incremental row rendering (only re-render rows whose version changed)
    displayed_vers = cache["displayed_vers"]
    if len(displayed_vers) != len(visible_rows):
        bd.rectangle((0, vp_y0, width - 1, vp_y1), fill=DEFAULT_BG)
        displayed_vers = [None] * len(visible_rows)
        cache["displayed_vers"] = displayed_vers

    row_strips = cache["row_strips"]
    blank_strip = cache["blank_strip"]
    any_row_updated = False
    for r_idx, row in enumerate(visible_rows):
        ry = top_y + r_idx * line_h
        if ry + line_h > vp_y1 + 2:
            break
        r_ver = visible_vers[r_idx]
        if displayed_vers[r_idx] == r_ver:
            continue
        any_row_updated = True
        if r_ver == 0:
            strip = blank_strip
        else:
            strip = row_strips.get(r_ver)
            if strip is None:
                strip = _render_terminal_row_strip(
                    row, width, line_h, pad_x, char_w_f, mono_font, mono_bold
                )
                if len(row_strips) >= 96:
                    # Retain strips currently referenced on screen; drop stale ones
                    live_set = set(visible_vers)
                    row_strips = {k: v for k, v in row_strips.items() if k in live_set}
                    cache["row_strips"] = row_strips
                row_strips[r_ver] = strip
        max_h = vp_y1 + 1 - ry
        if max_h < line_h:
            if max_h > 0:
                base_img.paste(strip.crop((0, 0, width, max_h)), (0, ry))
        else:
            base_img.paste(strip, (0, ry))
        displayed_vers[r_idx] = r_ver

    # Scrollbar on right edge of viewport (redrawn if rows updated or scrollbar metrics changed)
    sb_total = len(screen.scrollback) + screen.rows
    want_sb = (not screen.in_alt_screen) and (sb_total > screen.rows)
    sb_key = (want_sb, sb_total, screen.rows, scroll_off)
    if any_row_updated or cache["sb_key"] != sb_key:
        if want_sb:
            sb_offset = max(0, len(screen.scrollback) - scroll_off)
            rj_theme.draw_scrollbar(
                bd,
                width - (5 if not compact else 3),
                vp_y0 + 3,
                vp_y1 - 3,
                sb_offset,
                screen.rows,
                sb_total,
                width=3 if not compact else 2,
            )
        elif cache["sb_key"] is not None and cache["sb_key"][0]:
            # Clear old scrollbar strip (including top/bottom pad_y margins) when hidden
            sb_w = 5 if not compact else 3
            bd.rectangle((width - sb_w, vp_y0 + 3, width - 1, vp_y1 - 3), fill=DEFAULT_BG)
        cache["sb_key"] = sb_key

    # 3. Footer hint bar (redrawn only when quick_menu_open toggles)
    footer_key = (bool(quick_menu_open), compact)
    if cache["footer_key"] != footer_key:
        ftr_y = height - ftr_h
        bd.rectangle((0, ftr_y, width - 1, height - 1), fill=rj_theme.EMBER_THEME["bg_surface"])
        bd.line([(0, ftr_y), (width - 1, ftr_y)], fill=rj_theme.EMBER_THEME["border_subtle"], width=1)
        if quick_menu_open:
            hint = "Arrows:Pick  Enter:Run  Esc/F1:Close" if not compact else "U/D OK F1:Cls"
        elif compact:
            hint = "F1:Cmd F2:Clr F3:Menu"
        elif width < 440:
            hint = "PgUp/Dn:Scrl F1:Quick F2:Clr F3:Menu"
        else:
            hint = "Type:Shell  PgUp/Dn:Scroll  F1:Quick Cmds  F2:Clear  F3/Ctrl+Q:Menu  exit:Close"
        hint = rj_theme.truncate_text(bd, hint, width - 8, ui_font)
        hb = rj_theme.measure_text(ui_font, hint or "A", bd)
        bd.text((4, ftr_y + (ftr_h - (hb[3] - hb[1])) // 2 - hb[1]), hint, font=ui_font, fill=rj_theme.EMBER_THEME["text_muted"])
        cache["footer_key"] = footer_key

    # 4. Copy clean base frame and paint transient cursor + optional Quick-Command overlay
    img = base_img.copy()
    has_cursor = bool(show_cursor and cursor_blink_on and 0 <= cy < len(visible_rows) and 0 <= cx < screen.cols)
    if has_cursor or quick_menu_open:
        d = ImageDraw.Draw(img)
        if has_cursor:
            cur_x = pad_x + int(round(cx * char_w_f))
            cur_x1 = pad_x + int(round((cx + 1) * char_w_f)) - 1
            cur_y = top_y + cy * line_h
            if cur_y + line_h <= vp_y1 + 2:
                d.rectangle((cur_x, cur_y, max(cur_x, cur_x1), cur_y + line_h - 1), fill=CURSOR_BG)
                cur_ch = visible_rows[cy][cx][0]
                if cur_ch and cur_ch != " ":
                    d.text((cur_x, cur_y), cur_ch, font=mono_bold, fill=CURSOR_FG)

        if quick_menu_open:
            box_w = min(width - 16, max(116, int(width * 0.56)))
            item_h = max(13, line_h + 3)
            vis_n = min(len(QUICK_COMMANDS), max(4, (vp_y1 - vp_y0 - 28) // item_h))
            box_h = 22 + vis_n * item_h + 6
            bx0 = (width - box_w) // 2
            by0 = vp_y0 + max(4, ((vp_y1 - vp_y0) - box_h) // 2)
            bx1 = bx0 + box_w
            by1 = min(vp_y1 - 2, by0 + box_h)

            rj_theme.draw_card(
                d,
                (bx0, by0, bx1, by1),
                fill=rj_theme.EMBER_THEME["bg_surface"],
                outline=rj_theme.EMBER_THEME["border_accent"],
                radius=6,
                width=1,
            )
            d.text((bx0 + 8, by0 + 4), "QUICK COMMANDS", font=ui_bold, fill=rj_theme.EMBER_THEME["accent_bright"])
            d.line([(bx0 + 4, by0 + 18), (bx1 - 4, by0 + 18)], fill=rj_theme.EMBER_THEME["border_subtle"], width=1)

            q_start = max(0, min(quick_index - vis_n + 1, len(QUICK_COMMANDS) - vis_n))
            for i in range(vis_n):
                idx = q_start + i
                if idx >= len(QUICK_COMMANDS):
                    break
                label, _cmd = QUICK_COMMANDS[idx]
                ry = by0 + 21 + i * item_h
                sel = idx == quick_index
                if sel:
                    rj_theme.draw_card(
                        d,
                        (bx0 + 5, ry, bx1 - 6, ry + item_h - 2),
                        fill=rj_theme.EMBER_THEME["select_bg"],
                        outline=None,
                        radius=4,
                        accent_left=rj_theme.EMBER_THEME["accent"],
                        accent_width=2,
                    )
                txt = rj_theme.truncate_text(d, label, box_w - 20, ui_font)
                fg = rj_theme.EMBER_THEME["accent_bright"] if sel else rj_theme.EMBER_THEME["text_primary"]
                d.text((bx0 + 12, ry + 2), txt, font=ui_font, fill=fg)

    return img


# ---------------------------------------------------------------------------
# Remote Text Session Helper (WebUI + Cardputer keyboard forwarding)
# ---------------------------------------------------------------------------

_TEXT_SESSION_FILE = os.environ.get("RJ_TEXT_SESSION_FILE", "/dev/shm/rj_text_session.json")


def _write_remote_text_session(payload: dict) -> None:
    try:
        directory = os.path.dirname(_TEXT_SESSION_FILE)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{_TEXT_SESSION_FILE}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        os.replace(tmp, _TEXT_SESSION_FILE)
        try:
            os.chmod(_TEXT_SESSION_FILE, 0o666)
        except OSError:
            pass
    except Exception:
        pass


def _open_terminal_remote_session() -> str:
    session_id = uuid.uuid4().hex
    _write_remote_text_session({
        "active": True,
        "session_id": session_id,
        "title": "Terminal",
        "default": "",
        "charset": "terminal",
        "max_len": 0,
        "started_at": time.time(),
        "timeout": 0,
    })
    return session_id


def _close_terminal_remote_session(session_id: Optional[str]) -> None:
    if not session_id:
        return
    try:
        if os.path.isfile(_TEXT_SESSION_FILE):
            with open(_TEXT_SESSION_FILE, "r", encoding="utf-8") as handle:
                cur = json.load(handle) or {}
            if cur.get("session_id") not in (None, session_id):
                return
    except Exception:
        pass
    _write_remote_text_session({
        "active": False,
        "session_id": session_id,
        "closed_at": time.time(),
    })


# ---------------------------------------------------------------------------
# Interactive Terminal UI Loop
# ---------------------------------------------------------------------------

def run_terminal_ui(
    lcd,
    pins: Optional[Dict[str, int]] = None,
    gpio=None,
    session: Optional[PersistentTerminalSession] = None,
    persistent: bool = True,
    cwd: Optional[str] = None,
) -> int:
    """Run the interactive Obsidian Ember PTY terminal on *lcd*.

    Returns 0 on normal detach (F3 / Ctrl+Q) or clean shell exit.
    """
    W, H = lcd.width, lcd.height
    metrics = compute_terminal_metrics(W, H)
    sess = session if session is not None else (_SHARED_SESSION if persistent else PersistentTerminalSession())
    screen = sess.ensure_started(metrics["rows"], metrics["cols"], cwd=cwd)

    # Ensure input transports are healthy before entering loop
    try:
        if hasattr(rj_input, "ensure_socket_healthy"):
            rj_input.ensure_socket_healthy()
    except Exception:
        pass
    if not rj_keyboard.is_host_active():
        try:
            import evdev_keys
            evdev_keys.start()
        except Exception:
            pass

    quick_open = False
    quick_idx = 0
    running = True
    last_blink = time.monotonic()
    last_active_at = last_blink
    blink_on = True
    last_render = 0.0

    rj_input.flush()
    remote_sid = _open_terminal_remote_session()

    # Support both raspyjack.py pin names ("KEY1_PIN") and payload names ("KEY1")
    _PIN_ALIASES = (
        ("KEY3_PIN", ("KEY3_PIN", "KEY3")),
        ("KEY1_PIN", ("KEY1_PIN", "KEY1")),
        ("KEY2_PIN", ("KEY2_PIN", "KEY2")),
    )
    _QUICK_PIN_ALIASES = (
        ("KEY_UP_PIN", ("KEY_UP_PIN", "UP")),
        ("KEY_DOWN_PIN", ("KEY_DOWN_PIN", "DOWN")),
        ("KEY_PRESS_PIN", ("KEY_PRESS_PIN", "OK")),
    )

    def _poll_fn_buttons(include_nav: bool = False) -> Optional[str]:
        v = rj_input.get_virtual_button()
        if v:
            return v
        if pins and gpio is not None:
            groups = _PIN_ALIASES + (_QUICK_PIN_ALIASES if include_nav else ())
            for canonical, aliases in groups:
                for alias in aliases:
                    pin = pins.get(alias)
                    if pin is not None:
                        try:
                            if gpio.input(pin) == 0:
                                return canonical
                        except Exception:
                            pass
                        break
        return None

    last_btn_at = 0.0
    last_btn_name = ""

    try:
        with rj_keyboard.text_input_mode():
            while running:
                if not sess.is_alive():
                    # Give reader thread a moment to flush final output
                    time.sleep(0.05)
                    frame = render_terminal_frame(
                        screen, W, H, metrics=metrics,
                        cursor_blink_on=False, quick_menu_open=False, pid=0,
                    )
                    lcd.LCD_ShowImage(frame, 0, 0)
                    sess.close()
                    break

                now = time.monotonic()
                force_redraw = last_render == 0.0

                # 1. Process lossless keyboard text & special events (local SHM/sock + WebUI remote)
                while True:
                    ev = rj_input.get_text_event()
                    if not ev:
                        break
                    ev_sid = str(ev.get("session_id") or "")
                    if ev_sid not in ("", "local", remote_sid):
                        continue
                    spec = str(ev.get("special") or "")
                    mods = int(ev.get("mods", 0) or 0)
                    shift_on = bool(mods & rj_keyboard.MOD_SHIFT_ANY)

                    if quick_open:
                        if spec in ("ESCAPE", "LEFT"):
                            quick_open = False
                            force_redraw = True
                        elif spec == "UP":
                            quick_idx = (quick_idx - 1) % len(QUICK_COMMANDS)
                            force_redraw = True
                        elif spec == "DOWN":
                            quick_idx = (quick_idx + 1) % len(QUICK_COMMANDS)
                            force_redraw = True
                        elif spec == "ENTER":
                            _lbl, cmd = QUICK_COMMANDS[quick_idx]
                            quick_open = False
                            if cmd == "__RESTART__":
                                screen = sess.restart(metrics["rows"], metrics["cols"], cwd=cwd)
                            elif cmd == "__CLOSE__":
                                sess.close()
                                running = False
                                break
                            else:
                                sess.write(cmd.encode("utf-8"))
                            force_redraw = True
                        continue

                    # Detach back to Main Menu on Ctrl+Q
                    if spec == "CTRL_Q":
                        running = False
                        break

                    # Scrollback controls (PageUp/PageDown or Shift+Up/Down)
                    if spec == "PAGEUP":
                        screen.scroll_view(max(3, metrics["rows"] // 2))
                        force_redraw = True
                        continue
                    if spec == "PAGEDOWN":
                        screen.scroll_view(-max(3, metrics["rows"] // 2))
                        force_redraw = True
                        continue
                    if shift_on and spec == "UP":
                        screen.scroll_view(2)
                        force_redraw = True
                        continue
                    if shift_on and spec == "DOWN":
                        screen.scroll_view(-2)
                        force_redraw = True
                        continue

                    raw_bytes = encode_text_event_to_pty(ev, app_cursor=screen.app_cursor_keys)
                    if raw_bytes:
                        if not sess.write(raw_bytes):
                            running = False
                            break
                        blink_on = True
                        last_blink = now
                        force_redraw = True

                if not running:
                    break

                # 2. Function buttons (F1/KEY1, F2/KEY2, F3/KEY3)
                btn = _poll_fn_buttons(include_nav=quick_open)
                if btn:
                    if btn != last_btn_name or (now - last_btn_at) >= 0.22:
                        last_btn_name = btn
                        last_btn_at = now
                        if btn == "KEY3_PIN":
                            if quick_open:
                                quick_open = False
                                force_redraw = True
                            else:
                                running = False
                                break
                        elif btn == "KEY1_PIN":
                            quick_open = not quick_open
                            force_redraw = True
                        elif btn == "KEY2_PIN":
                            if quick_open:
                                quick_open = False
                            elif screen.scroll_offset > 0:
                                screen.reset_scroll()
                            else:
                                sess.write(b"\x0c")
                            force_redraw = True
                        elif quick_open:
                            if btn == "KEY_UP_PIN":
                                quick_idx = (quick_idx - 1) % len(QUICK_COMMANDS)
                                force_redraw = True
                            elif btn == "KEY_DOWN_PIN":
                                quick_idx = (quick_idx + 1) % len(QUICK_COMMANDS)
                                force_redraw = True
                            elif btn == "KEY_PRESS_PIN":
                                _lbl, cmd = QUICK_COMMANDS[quick_idx]
                                quick_open = False
                                if cmd == "__RESTART__":
                                    screen = sess.restart(metrics["rows"], metrics["cols"], cwd=cwd)
                                elif cmd == "__CLOSE__":
                                    sess.close()
                                    running = False
                                    break
                                else:
                                    sess.write(cmd.encode("utf-8"))
                                force_redraw = True
                else:
                    last_btn_name = ""

                # 3. Cursor blink (paused after 20s idle -> 0 FPS) & capped 20 FPS render
                if screen.dirty or force_redraw:
                    last_active_at = now
                if (now - last_active_at) < 20.0:
                    if (now - last_blink) >= 0.75:
                        blink_on = not blink_on
                        last_blink = now
                        force_redraw = True
                elif not blink_on:
                    blink_on = True
                    force_redraw = True

                if (screen.dirty or force_redraw) and (now - last_render) >= 0.05:
                    screen.dirty = False
                    frame = render_terminal_frame(
                        screen,
                        W,
                        H,
                        metrics=metrics,
                        cursor_blink_on=blink_on,
                        quick_menu_open=quick_open,
                        quick_index=quick_idx,
                        pid=sess.pid,
                    )
                    lcd.LCD_ShowImage(frame, 0, 0)
                    last_render = now

                time.sleep(0.015 if (now - last_active_at) < 1.5 else 0.045)
    finally:
        _close_terminal_remote_session(remote_sid)
        if not persistent:
            sess.close()
        rj_input.flush()

    return 0
