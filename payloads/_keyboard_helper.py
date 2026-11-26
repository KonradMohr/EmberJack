"""
Shared on-screen + full physical keyboard input helper for RaspyJack payloads.

Usage:
    from payloads._keyboard_helper import lcd_keyboard

    text = lcd_keyboard(lcd, font, pins, gpio, title="Enter IP")
    text = lcd_keyboard(lcd, font, pins, gpio, title="SSID", default="FreeWiFi")
    text = lcd_keyboard(lcd, font, pins, gpio, title="PIN", charset="digits")

    Returns the entered string, or None if cancelled (KEY3 / F3 / Esc).

Controls:
    Physical Keyboard   Full typing (Shift, CapsLock, symbols, Numpad, Space),
                        Backspace/Delete, Enter=Submit, Esc=Cancel, Ctrl+U=Clear
    Tab / F4 / Ctrl+K   Toggle on-screen digital keyboard (hidden by default on
                        HDMI & CardputerZero; auto-shown on SPI button HATs)
    UP/DOWN/LEFT/RIGHT  Navigate on-screen keyboard grid & bottom action bar
    F1 / KEY1 / OK      Press selected key (types char, SPACE, DEL, PAGE, or DONE)
    F2 / KEY2           Switch keyboard page (abc / ABC / 123); Delete on 1-page layouts
    Enter               Submit input immediately
    F3 / KEY3 / Esc     Cancel (return None)
"""

import contextlib
import time
from typing import Dict, List, Optional, Tuple

try:
    from PIL import Image, ImageDraw
except Exception:
    Image = None
    ImageDraw = None

try:
    import rj_env
except Exception:
    rj_env = None

try:
    import rj_keyboard as _rj_keyboard
except Exception:
    _rj_keyboard = None

try:
    from payloads._display_helper import scaled_font
except Exception:
    scaled_font = None

try:
    import rj_theme
    _THEME = rj_theme.EMBER_THEME
except Exception:
    rj_theme = None
    _THEME = {
        "bg_base": "#0E0E10",
        "bg_surface": "#16161A",
        "bg_elevated": "#1F1E22",
        "bg_well": "#111114",
        "border_subtle": "#2B2826",
        "border_accent": "#B85E24",
        "accent": "#E07A2F",
        "accent_bright": "#F2994A",
        "select_bg": "#2D1D12",
        "text_primary": "#E6DFD7",
        "text_bright": "#F5EFE8",
        "text_muted": "#9B928A",
        "text_on_accent": "#120C08",
        "danger": "#D9534F",
    }

from payloads import _input_helper as _input_helper

get_button = _input_helper.get_button
get_virtual_button = getattr(_input_helper, "get_virtual_button", lambda: None)
open_remote_text_session = getattr(_input_helper, "open_remote_text_session", lambda **_kwargs: None)
get_remote_text_event = getattr(_input_helper, "get_remote_text_event", lambda *_args, **_kwargs: None)
close_remote_text_session = getattr(_input_helper, "close_remote_text_session", lambda *_args, **_kwargs: None)
text_input_mode = getattr(_input_helper, "text_input_mode", None)
flush_input = getattr(_input_helper, "flush_input", lambda: None)
_flip = getattr(_input_helper, "_flip", lambda b: b)
_drain_matching_vbtn = getattr(_input_helper, "_drain_matching_vbtn", lambda _b: False)
_consume_suppressed_vbtn = getattr(_input_helper, "_consume_suppressed_vbtn", lambda _b: False)
_BTN_TO_VIRTUAL = getattr(_input_helper, "_BTN_TO_VIRTUAL", {})

# ---------------------------------------------------------------------------
# Clean QWERTY / Specialized Keyboard Pages (no hidden fake-space cells!)
# ---------------------------------------------------------------------------

_PAGES_FULL: List[dict] = [
    {
        "label": "abc",
        "rows": [
            list("qwertyuiop"),
            list("asdfghjkl-"),
            list("zxcvbnm._/"),
        ],
    },
    {
        "label": "ABC",
        "rows": [
            list("QWERTYUIOP"),
            list("ASDFGHJKL-"),
            list("ZXCVBNM._/"),
        ],
    },
    {
        "label": "123",
        "rows": [
            list("1234567890"),
            list("!@#$%^&*()"),
            list("-_=+:;'?,."),
        ],
    },
]

_PAGES_DIGITS: List[dict] = [
    {
        "label": "123",
        "rows": [
            list("1234567890"),
            list(".-:/+"),
        ],
    },
]

_PAGES_IP: List[dict] = [
    {
        "label": "IP",
        "rows": [
            list("123456"),
            list("7890."),
        ],
    },
]

_PAGES_HEX: List[dict] = [
    {
        "label": "HEX",
        "rows": [
            list("0123456789"),
            list("ABCDEF:."),
        ],
    },
]

_PAGES_MAC: List[dict] = [
    {
        "label": "MAC",
        "rows": [
            list("0123456789"),
            list("ABCDEF:"),
        ],
    },
]

_PAGES_URL: List[dict] = [
    {
        "label": "abc",
        "rows": [
            list("qwertyuiop"),
            list("asdfghjkl-"),
            list("zxcvbnm./:"),
        ],
    },
    {
        "label": "123",
        "rows": [
            list("1234567890"),
            list(":/.-_?=&@%"),
        ],
    },
]

_CHARSET_MAP: Dict[str, List[dict]] = {
    "full": _PAGES_FULL,
    "alpha": _PAGES_FULL[:2],
    "digits": _PAGES_DIGITS,
    "ip": _PAGES_IP,
    "hex": _PAGES_HEX,
    "mac": _PAGES_MAC,
    "url": _PAGES_URL,
}

_CHARSET_ALLOWED = {
    "digits": set("0123456789.-:/+"),
    "ip": set("0123456789."),
    "hex": set("0123456789abcdefABCDEF:."),
    "mac": set("0123456789abcdefABCDEF:"),
}


_OSK_TOGGLE_SPECIALS = ("TAB", "F4", "CTRL_K")
_PHYSICAL_KB_DISPLAYS = ("HDMI", "CARDPUTER_320")


def should_show_onscreen_keyboard(lcd=None, display_type: str = "") -> bool:
    """Return True if the on-screen digital keyboard should show automatically.

    - SPI button-HAT screens (`ST7735_128`, `ST7789_240`): True (auto-show!)
    - Devices with built-in/attached physical keyboards (`HDMI`, `CARDPUTER_320`):
      False by default; press Tab, F4, or Ctrl+K to toggle it open anytime.
    """
    dt = str(display_type or "").strip().upper()
    if not dt and lcd is not None:
        dt = str(getattr(lcd, "display_type", "") or "").strip().upper()
    if not dt and rj_env is not None:
        try:
            dt = str(rj_env.get_display_type() or "").strip().upper()
        except Exception:
            dt = ""
    return dt not in _PHYSICAL_KB_DISPLAYS


def get_keyboard_pages(charset: str = "full") -> List[dict]:
    """Return the keyboard page definitions for *charset*."""
    return _CHARSET_MAP.get(charset, _PAGES_FULL)


def get_action_row(pages: List[dict], page_idx: int, charset: str = "full") -> List[dict]:
    """Return the bottom action-bar buttons aligned cleanly below the character grid."""
    if len(pages) > 1:
        next_lbl = pages[(page_idx + 1) % len(pages)]["label"]
        return [
            {"id": "page", "label": f"F2:{next_lbl}", "short": next_lbl, "weight": 1.9},
            {"id": "space", "label": "SPACE", "short": "SPC", "weight": 4.3},
            {"id": "del", "label": "DEL", "short": "DEL", "weight": 1.8},
            {"id": "done", "label": "DONE", "short": "OK", "weight": 2.0},
        ]
    return [
        {"id": "clear", "label": "CLEAR", "short": "CLR", "weight": 2.4},
        {"id": "del", "label": "DEL", "short": "DEL", "weight": 3.0},
        {"id": "done", "label": "DONE", "short": "OK", "weight": 4.6},
    ]


def _filter_char(ch: str, charset: str) -> str:
    allowed = _CHARSET_ALLOWED.get(charset)
    if allowed is None:
        return ch
    if ch in allowed:
        return ch.upper() if charset in ("hex", "mac") else ch
    return ""


def _load_kb_fonts(width: int, height: int, fallback_font=None) -> dict:
    """Load resolution-adaptive fonts for 640x360 HDMI, 320x170 Cardputer, 240x240, and 128x128."""
    large = width >= 540 and height >= 300
    wide = width >= 260
    mid = (not wide) and width >= 200

    if large:
        t_px, i_px, k_px, a_px, b_px, h_px = 14, 15, 16, 12, 10, 11
    elif wide:
        t_px, i_px, k_px, a_px, b_px, h_px = 11, 11, 11, 9, 8, 8
    elif mid:
        t_px, i_px, k_px, a_px, b_px, h_px = 11, 11, 12, 10, 8, 8
    else:
        t_px, i_px, k_px, a_px, b_px, h_px = 8, 8, 8, 7, 7, 7

    def _f(px: int, bold: bool = False, mono: bool = False):
        if rj_env is not None:
            return rj_env.load_font(px, bold=bold, mono=mono)
        if scaled_font is not None:
            try:
                return scaled_font(px)
            except Exception:
                pass
        return fallback_font

    return {
        "large": large,
        "wide": wide,
        "mid": mid,
        "title": _f(t_px, bold=True),
        "input": _f(i_px, bold=large, mono=True),
        "key": _f(k_px, bold=True),
        "action": _f(a_px, bold=True),
        "badge": _f(b_px, bold=True, mono=True),
        "hint": _f(h_px, bold=False),
    }


def _measure_w(d, text: str, font) -> int:
    if not text:
        return 0
    if font is not None and hasattr(font, "getlength"):
        try:
            return max(0, int(round(font.getlength(text))))
        except Exception:
            pass
    if rj_theme is not None and hasattr(rj_theme, "measure_text"):
        bb = rj_theme.measure_text(font, text, draw=d)
        return max(0, int(bb[2] - bb[0]))
    try:
        b = d.textbbox((0, 0), text, font=font)
        return max(0, int(b[2] - b[0]))
    except Exception:
        return len(text) * 7


def _truncate_px(d, text: str, max_w: int, font) -> str:
    if rj_theme is not None and hasattr(rj_theme, "truncate_text"):
        return rj_theme.truncate_text(d, text, max_w, font)
    if _measure_w(d, text, font) <= max_w:
        return text
    out = text
    while len(out) > 1 and _measure_w(d, out + "...", font) > max_w:
        out = out[:-1]
    return out + "..."


def _tail_fit_px(d, text: str, max_w: int, font) -> str:
    """Keep the newest typed tail of *text* visible inside *max_w* pixels."""
    if not text or _measure_w(d, text, font) <= max_w:
        return text
    ell = "…"
    # Binary search longest fitting suffix
    lo, hi = 1, len(text)
    best = text[-1:]
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = ell + text[-mid:]
        if _measure_w(d, cand, font) <= max_w:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _action_col_from_grid_col(cursor_c: int, row_len: int, action_row: List[dict]) -> int:
    """Map a horizontal character-row column to the action-bar button directly below it."""
    if not action_row:
        return 0
    frac = (cursor_c + 0.5) / max(1, row_len)
    tot_w = sum(a["weight"] for a in action_row) or 1.0
    acc = 0.0
    for idx, act in enumerate(action_row):
        acc += act["weight"] / tot_w
        if frac <= acc + 1e-6:
            return idx
    return len(action_row) - 1


def _grid_col_from_action_col(act_c: int, action_row: List[dict], target_row_len: int) -> int:
    """Map an action-bar button center back to the character column above it."""
    if target_row_len <= 1 or not action_row:
        return 0
    tot_w = sum(a["weight"] for a in action_row) or 1.0
    idx = max(0, min(act_c, len(action_row) - 1))
    before = sum(a["weight"] for a in action_row[:idx])
    center = (before + action_row[idx]["weight"] * 0.5) / tot_w
    return max(0, min(target_row_len - 1, int(center * target_row_len)))


def render_text_input_frame(
    width: int,
    height: int,
    title: str,
    text_str: str,
    charset: str = "full",
    max_len: int = 64,
    blink_on: bool = True,
    fonts: Optional[dict] = None,
):
    """Render a clean Obsidian Ember direct-typing dialog (used when OSK is hidden)."""
    T = _THEME
    f = fonts or _load_kb_fonts(width, height)
    large = f["large"]
    wide = f["wide"]
    mid = f["mid"]

    img = Image.new("RGB", (width, height), T["bg_base"])
    d = ImageDraw.Draw(img)

    hdr_h = 30 if large else (22 if wide else (20 if mid else 14))
    ftr_h = 24 if large else (17 if wide else (16 if mid else 12))
    pad_x = 12 if large else (6 if wide else (5 if mid else 2))

    # 1. Header bar
    d.rectangle((0, 0, width - 1, hdr_h - 1), fill=T["bg_elevated"])
    d.rectangle((0, 0, 3 if large else 2, hdr_h - 1), fill=T["accent"])
    d.line([(0, hdr_h - 1), (width - 1, hdr_h - 1)], fill=T["border_subtle"], width=1)

    badge_lbl = charset.upper() if charset != "full" else "INPUT"
    pill_h = 18 if large else (14 if wide else 11)
    pill_y0 = max(1, (hdr_h - pill_h) // 2)
    pill_y1 = pill_y0 + pill_h
    pw = _measure_w(d, badge_lbl, f["badge"]) + (12 if large else 8)
    px0 = width - pad_x - pw
    d.rounded_rectangle(
        (px0, pill_y0, width - pad_x, pill_y1),
        radius=4 if large else 2,
        fill=T["select_bg"],
        outline=T["border_accent"],
        width=1,
    )
    d.text(((px0 + width - pad_x) // 2, (pill_y0 + pill_y1) // 2), badge_lbl, font=f["badge"], fill=T["accent_bright"], anchor="mm")

    tx0 = 12 if large else (7 if wide else 5)
    t_txt = _truncate_px(d, str(title or "Input"), max(32, px0 - tx0 - 8), f["title"])
    d.text((tx0, hdr_h // 2), t_txt, font=f["title"], fill=T["text_bright"], anchor="lm")

    # 2. Centered Obsidian Ember Input Card
    body_y0 = hdr_h + (10 if large else 5)
    body_y1 = height - ftr_h - (10 if large else 5)
    card_w = min(width - pad_x * 2, 560 if large else (width - 14))
    card_h = min(body_y1 - body_y0, 176 if large else (106 if wide else 84))
    cx0 = (width - card_w) // 2
    cy0 = body_y0 + max(0, (body_y1 - body_y0 - card_h) // 2)
    cx1 = cx0 + card_w
    cy1 = cy0 + card_h

    d.rounded_rectangle(
        (cx0, cy0, cx1, cy1),
        radius=9 if large else 5,
        fill=T["bg_surface"],
        outline=T["border_subtle"],
        width=1,
    )
    # Warm ember left accent bar on card
    ac_w = 4 if large else 2
    d.rounded_rectangle((cx0 + 1, cy0 + 8, cx0 + 1 + ac_w, cy1 - 8), radius=2, fill=T["accent"])

    inner_x0 = cx0 + (18 if large else 10)
    inner_x1 = cx1 - (16 if large else 10)

    # Prompt subtitle + char counter
    sub_y = cy0 + (22 if large else 14)
    charset_hints = {
        "ip": "Type IP address and press Enter",
        "digits": "Type numbers and press Enter",
        "hex": "Type hexadecimal value and press Enter",
        "mac": "Type MAC address and press Enter",
        "url": "Type URL or host and press Enter",
    }
    sub_txt = charset_hints.get(charset, "Type your input and press Enter")
    cnt_txt = f"{len(text_str)}/{max_len}"
    cnt_w = _measure_w(d, cnt_txt, f["badge"]) + 10
    d.text((inner_x1, sub_y), cnt_txt, font=f["badge"], fill=T["text_muted"], anchor="rm")
    d.text(
        (inner_x0, sub_y),
        _truncate_px(d, sub_txt, max(30, (inner_x1 - inner_x0) - cnt_w), f["hint"]),
        font=f["hint"],
        fill=T["text_muted"],
        anchor="lm",
    )

    # Large input well
    in_y0 = cy0 + (40 if large else 26)
    in_h = 48 if large else (30 if wide else 24)
    in_y1 = in_y0 + in_h
    d.rounded_rectangle(
        (inner_x0, in_y0, inner_x1, in_y1),
        radius=7 if large else 4,
        fill=T["bg_well"],
        outline=T["border_accent"],
        width=2 if large else 1,
    )

    txt_pad = 12 if large else 7
    avail_in_w = max(24, (inner_x1 - inner_x0) - txt_pad * 2 - 6)
    in_cy = (in_y0 + in_y1) // 2
    in_font = f["key"] if large else f["input"]

    if text_str:
        shown = _tail_fit_px(d, text_str, avail_in_w, in_font)
        d.text((inner_x0 + txt_pad, in_cy), shown, font=in_font, fill=T["text_bright"], anchor="lm")
        caret_x = inner_x0 + txt_pad + _measure_w(d, shown, in_font) + 2
        if blink_on:
            ch_h = max(10, in_h - (18 if large else 10))
            d.rectangle((caret_x, in_cy - ch_h // 2, caret_x + (2 if large else 1), in_cy + ch_h // 2), fill=T["accent_bright"])
    else:
        caret_x = inner_x0 + txt_pad
        if blink_on:
            ch_h = max(10, in_h - (18 if large else 10))
            d.rectangle((caret_x, in_cy - ch_h // 2, caret_x + (2 if large else 1), in_cy + ch_h // 2), fill=T["accent_bright"])
        ph = "Start typing..." if (large or wide) else "Type..."
        d.text((caret_x + 7, in_cy), ph, font=f["hint"], fill=T["text_muted"], anchor="lm")

    # Keybind action chips inside bottom of card
    chip_y0 = in_y1 + (18 if large else 10)
    chip_h = 30 if large else (22 if wide else 18)
    chip_y1 = min(cy1 - (10 if large else 6), chip_y0 + chip_h)
    if (chip_y1 - chip_y0) >= 14:
        chips = (
            [
                ("Enter: Submit", T["select_bg"], T["border_accent"], T["accent_bright"]),
                ("Tab / F4: Digital Keyboard", T["bg_elevated"], T["border_subtle"], T["text_primary"]),
                ("Esc: Cancel", T["bg_elevated"], T["border_subtle"], T["text_muted"]),
            ]
            if large else [
                ("Ent: Submit", T["select_bg"], T["border_accent"], T["accent_bright"]),
                ("Tab: Digital KB", T["bg_elevated"], T["border_subtle"], T["text_primary"]),
                ("Esc: Back", T["bg_elevated"], T["border_subtle"], T["text_muted"]),
            ]
        )
        chip_gap = 10 if large else 5
        chip_widths = [_measure_w(d, c[0], f["action"]) + (20 if large else 10) for c in chips]
        total_cw = sum(chip_widths) + chip_gap * (len(chips) - 1)
        if total_cw > (inner_x1 - inner_x0):
            # Compact fit if narrow
            scale = (inner_x1 - inner_x0 - chip_gap * (len(chips) - 1)) / max(1, sum(chip_widths))
            chip_widths = [max(24, int(w * scale)) for w in chip_widths]
            total_cw = sum(chip_widths) + chip_gap * (len(chips) - 1)
        bx = inner_x0 + max(0, ((inner_x1 - inner_x0) - total_cw) // 2)
        for (clbl, cbg, cout, cfg), cw in zip(chips, chip_widths):
            d.rounded_rectangle((bx, chip_y0, bx + cw, chip_y1), radius=5 if large else 3, fill=cbg, outline=cout, width=1)
            d.text((bx + cw // 2, (chip_y0 + chip_y1) // 2), _truncate_px(d, clbl, cw - 6, f["action"]), font=f["action"], fill=cfg, anchor="mm")
            bx += cw + chip_gap

    # 3. Footer bar
    ftr_y0 = height - ftr_h
    d.rectangle((0, ftr_y0, width - 1, height - 1), fill=T["bg_elevated"])
    d.line([(0, ftr_y0), (width - 1, ftr_y0)], fill=T["border_subtle"], width=1)
    if large:
        hint = "Enter: Submit   •   Bksp: Delete   •   Ctrl+U: Clear   •   Tab / F4: Digital Keyboard   •   Esc / F3: Cancel"
    elif wide:
        hint = "Ent:Submit  Bksp:Del  Ctrl+U:Clr  Tab:Digital KB  Esc:Cancel"
    else:
        hint = "Ent:OK Tab:KB Esc:Back"
    d.text((width // 2, ftr_y0 + ftr_h // 2), _truncate_px(d, hint, width - pad_x * 2, f["hint"]), font=f["hint"], fill=T["text_muted"], anchor="mm")
    return img


def render_keyboard_frame(
    width: int,
    height: int,
    title: str,
    text_str: str,
    pages: List[dict],
    page_idx: int,
    cursor_r: int,
    cursor_c: int,
    charset: str = "full",
    max_len: int = 64,
    blink_on: bool = True,
    fonts: Optional[dict] = None,
    show_osk: bool = True,
    can_hide_osk: bool = False,
):
    """Render a crisp, resolution-adaptive Obsidian Ember keyboard or direct-typing dialog."""
    if not show_osk:
        return render_text_input_frame(
            width=width,
            height=height,
            title=title,
            text_str=text_str,
            charset=charset,
            max_len=max_len,
            blink_on=blink_on,
            fonts=fonts,
        )
    T = _THEME
    f = fonts or _load_kb_fonts(width, height)
    large = f["large"]
    wide = f["wide"]
    mid = f["mid"]

    page = pages[page_idx % len(pages)]
    grid = page["rows"]
    n_rows = len(grid)
    action_row = get_action_row(pages, page_idx, charset=charset)

    img = Image.new("RGB", (width, height), T["bg_base"])
    d = ImageDraw.Draw(img)

    # ------------------------------------------------------------------
    # 1. Header Bar (Title + Page Indicator Pills)
    # ------------------------------------------------------------------
    hdr_h = 30 if large else (21 if wide else (20 if mid else 14))
    ftr_h = 24 if large else (16 if wide else (16 if mid else 12))
    pad_x = 12 if large else (6 if wide else (5 if mid else 2))

    d.rectangle((0, 0, width - 1, hdr_h - 1), fill=T["bg_elevated"])
    d.rectangle((0, 0, 3 if large else 2, hdr_h - 1), fill=T["accent"])
    d.line([(0, hdr_h - 1), (width - 1, hdr_h - 1)], fill=T["border_subtle"], width=1)

    # Right-side page indicator pills (show all pages on large/wide so F2 toggle is obvious)
    rx = width - pad_x
    pill_h = 18 if large else (14 if wide else (13 if mid else 10))
    pill_y0 = max(1, (hdr_h - pill_h) // 2)
    pill_y1 = pill_y0 + pill_h
    show_all_pills = (large or wide) and len(pages) > 1
    pills_to_draw = list(enumerate(pages)) if show_all_pills else [(page_idx, page)]
    for idx, pg in reversed(pills_to_draw):
        lbl = pg["label"]
        pw = _measure_w(d, lbl, f["badge"]) + (12 if large else (8 if wide else 6))
        px0 = rx - pw
        active = (idx == page_idx)
        d.rounded_rectangle(
            (px0, pill_y0, rx, pill_y1),
            radius=4 if large else 2,
            fill=T["select_bg"] if active else T["bg_surface"],
            outline=T["border_accent"] if active else T["border_subtle"],
            width=1,
        )
        d.text(
            ((px0 + rx) // 2, (pill_y0 + pill_y1) // 2),
            lbl,
            font=f["badge"],
            fill=T["accent_bright"] if active else T["text_muted"],
            anchor="mm",
        )
        rx = px0 - (5 if large else 3)

    tx0 = (12 if large else (7 if wide else 5))
    title_avail = max(32, rx - tx0 - 6)
    t_txt = _truncate_px(d, str(title or "Input"), title_avail, f["title"])
    d.text((tx0, hdr_h // 2), t_txt, font=f["title"], fill=T["text_bright"], anchor="lm")

    # ------------------------------------------------------------------
    # 2. Input Well (Text + Glowing Ember Caret + Char Counter)
    # ------------------------------------------------------------------
    in_y0 = hdr_h + (8 if large else (4 if wide else (5 if mid else 2)))
    in_h = 38 if large else (23 if wide else (24 if mid else 15))
    in_y1 = in_y0 + in_h
    in_x0 = pad_x
    in_x1 = width - pad_x
    in_rad = 6 if large else (4 if wide else 2)

    d.rounded_rectangle(
        (in_x0, in_y0, in_x1, in_y1),
        radius=in_rad,
        fill=T["bg_well"],
        outline=T["border_accent"],
        width=2 if large else 1,
    )

    counter_w = 0
    if large:
        cnt_txt = f"{len(text_str)}/{max_len}"
        counter_w = _measure_w(d, cnt_txt, f["badge"]) + 14
        d.text(
            (in_x1 - 10, (in_y0 + in_y1) // 2),
            cnt_txt,
            font=f["badge"],
            fill=T["text_muted"],
            anchor="rm",
        )

    txt_pad = 10 if large else (6 if wide else 3)
    avail_in_w = max(24, (in_x1 - in_x0) - txt_pad * 2 - counter_w - 6)
    cy = (in_y0 + in_y1) // 2

    if text_str:
        shown = _tail_fit_px(d, text_str, avail_in_w, f["input"])
        d.text((in_x0 + txt_pad, cy), shown, font=f["input"], fill=T["text_bright"], anchor="lm")
        caret_x = in_x0 + txt_pad + _measure_w(d, shown, f["input"]) + 2
        if blink_on:
            ch_h = max(8, in_h - (14 if large else (8 if wide else 5)))
            d.rectangle(
                (caret_x, cy - ch_h // 2, caret_x + (2 if large else 1), cy + ch_h // 2),
                fill=T["accent_bright"],
            )
    else:
        caret_x = in_x0 + txt_pad
        if blink_on:
            ch_h = max(8, in_h - (14 if large else (8 if wide else 5)))
            d.rectangle(
                (caret_x, cy - ch_h // 2, caret_x + (2 if large else 1), cy + ch_h // 2),
                fill=T["accent_bright"],
            )
        placeholder = (
            "Type on keyboard or navigate with Arrows + F1..."
            if large else ("Type or Arrows + F1" if wide else ("Type / F1" if mid else "F1:Key"))
        )
        ph_txt = _truncate_px(d, placeholder, max(16, avail_in_w - 6), f["hint"])
        d.text((caret_x + 6, cy), ph_txt, font=f["hint"], fill=T["text_muted"], anchor="lm")

    # ------------------------------------------------------------------
    # 3. Recessed Keyboard Tray (Character Keycaps + Bottom Action Bar)
    # ------------------------------------------------------------------
    tray_y0 = in_y1 + (8 if large else (4 if wide else (6 if mid else 2)))
    tray_y1 = height - ftr_h - (7 if large else (3 if wide else (4 if mid else 2)))
    tray_x0 = pad_x
    tray_x1 = width - pad_x
    tray_pad_x = 8 if large else (4 if wide else (4 if mid else 1))
    tray_pad_y = 8 if large else (4 if wide else (5 if mid else 1))

    if large or wide or mid:
        d.rounded_rectangle(
            (tray_x0, tray_y0, tray_x1, tray_y1),
            radius=8 if large else 5,
            fill=T["bg_surface"],
            outline=T["border_subtle"],
            width=1,
        )

    inner_x0 = tray_x0 + tray_pad_x
    inner_x1 = tray_x1 - tray_pad_x
    inner_y0 = tray_y0 + tray_pad_y
    inner_y1 = tray_y1 - tray_pad_y
    inner_w = max(20, inner_x1 - inner_x0)
    inner_h = max(20, inner_y1 - inner_y0)

    total_rows = n_rows + 1  # character rows + action bar row
    gap_x = 6 if large else (3 if wide else (3 if mid else 1))
    gap_y = 7 if large else (3 if wide else (4 if mid else 2))
    action_extra_gap = 4 if large else (2 if wide else 1)

    avail_rows_h = max(total_rows * 10, inner_h - gap_y * (total_rows - 1) - action_extra_gap)
    key_h = max(10, avail_rows_h // total_rows)
    # Vertically center the grid block inside the tray
    used_h = key_h * total_rows + gap_y * (total_rows - 1) + action_extra_gap
    grid_top = inner_y0 + max(0, (inner_h - used_h) // 2)

    max_cols = max(len(r) for r in grid)
    key_w = max(8, (inner_w - gap_x * max(0, max_cols - 1)) // max(1, max_cols))
    full_row_w = key_w * max_cols + gap_x * max(0, max_cols - 1)
    grid_left = inner_x0 + max(0, (inner_w - full_row_w) // 2)
    key_rad = 6 if large else (4 if wide else (3 if mid else 2))
    bevel = 2 if large else (1 if (wide or mid) else 0)

    # Character rows
    for r_idx, row in enumerate(grid):
        ry0 = grid_top + r_idx * (key_h + gap_y)
        ry1 = ry0 + key_h
        row_w = len(row) * key_w + gap_x * max(0, len(row) - 1)
        row_x0 = grid_left + max(0, (full_row_w - row_w) // 2)

        for c_idx, ch in enumerate(row):
            kx0 = row_x0 + c_idx * (key_w + gap_x)
            kx1 = kx0 + key_w
            is_sel = (r_idx == cursor_r and c_idx == cursor_c)

            if bevel:
                d.rounded_rectangle(
                    (kx0, ry0 + bevel, kx1, ry1 + bevel),
                    radius=key_rad,
                    fill=T["bg_well"],
                )
            if is_sel:
                d.rounded_rectangle(
                    (kx0, ry0, kx1, ry1),
                    radius=key_rad,
                    fill=T["accent"],
                    outline=T["accent_bright"],
                    width=2 if large else 1,
                )
                fg = T["text_on_accent"]
            else:
                d.rounded_rectangle(
                    (kx0, ry0, kx1, ry1),
                    radius=key_rad,
                    fill=T["bg_elevated"],
                    outline=T["border_subtle"],
                    width=1,
                )
                fg = T["text_primary"]

            d.text(((kx0 + kx1) // 2, (ry0 + ry1) // 2), ch, font=f["key"], fill=fg, anchor="mm")

    # Action bar row (aligned flush with full_row_w)
    act_y0 = grid_top + n_rows * (key_h + gap_y) + action_extra_gap
    act_y1 = act_y0 + key_h
    tot_weight = sum(a["weight"] for a in action_row) or 1.0
    n_act = len(action_row)
    act_avail_w = full_row_w - gap_x * max(0, n_act - 1)
    ax = grid_left

    for a_idx, act in enumerate(action_row):
        if a_idx == n_act - 1:
            ax1 = grid_left + full_row_w
        else:
            aw = max(14, int(round(act_avail_w * (act["weight"] / tot_weight))))
            ax1 = min(grid_left + full_row_w, ax + aw)

        is_sel = (cursor_r == n_rows and cursor_c == a_idx)
        aid = act["id"]
        lbl = act["label"] if (large or wide or mid) else act["short"]

        if bevel:
            d.rounded_rectangle(
                (ax, act_y0 + bevel, ax1, act_y1 + bevel),
                radius=key_rad,
                fill=T["bg_well"],
            )

        if is_sel:
            if aid in ("del", "clear"):
                fill_c = T.get("danger", "#D9534F")
                out_c = T["text_bright"]
                fg_c = T["text_bright"]
            else:
                fill_c = T["accent"]
                out_c = T["accent_bright"]
                fg_c = T["text_on_accent"]
            d.rounded_rectangle(
                (ax, act_y0, ax1, act_y1),
                radius=key_rad,
                fill=fill_c,
                outline=out_c,
                width=2 if large else 1,
            )
        else:
            if aid == "done":
                fill_c = T["select_bg"]
                out_c = T["border_accent"]
                fg_c = T["accent_bright"]
            elif aid == "page":
                fill_c = T["bg_well"]
                out_c = T["border_subtle"]
                fg_c = T["accent_bright"]
            else:
                fill_c = T["bg_elevated"]
                out_c = T["border_subtle"]
                fg_c = T["text_primary"]
            d.rounded_rectangle(
                (ax, act_y0, ax1, act_y1),
                radius=key_rad,
                fill=fill_c,
                outline=out_c,
                width=1,
            )

        # Draw clean label (with subtle spacebar notch glyph on large screens for SPACE)
        cx_a = (ax + ax1) // 2
        cy_a = (act_y0 + act_y1) // 2
        d.text((cx_a, cy_a), lbl, font=f["action"], fill=fg_c, anchor="mm")
        if aid == "space" and large and (ax1 - ax) >= 120:
            bar_w = min(46, (ax1 - ax) // 4)
            by = act_y1 - 6
            notch_col = T["text_on_accent"] if is_sel else T["text_muted"]
            d.line([(cx_a - bar_w // 2, by - 2), (cx_a - bar_w // 2, by), (cx_a + bar_w // 2, by), (cx_a + bar_w // 2, by - 2)], fill=notch_col, width=1)

        ax = ax1 + gap_x

    # ------------------------------------------------------------------
    # 4. Footer Hint Bar
    # ------------------------------------------------------------------
    ftr_y0 = height - ftr_h
    d.rectangle((0, ftr_y0, width - 1, height - 1), fill=T["bg_elevated"])
    d.line([(0, ftr_y0), (width - 1, ftr_y0)], fill=T["border_subtle"], width=1)
    if large:
        hide_seg = "   •   Tab/F4: Hide KB" if can_hide_osk else ""
        if len(pages) > 1:
            hint = f"Arrows: Move   •   F1/OK: Press   •   F2: Page   •   Enter: Submit{hide_seg}   •   Esc: Cancel"
        else:
            hint = f"Arrows: Move   •   F1/OK: Press   •   F2: Del   •   Enter: Submit{hide_seg}   •   Esc: Cancel"
    elif wide:
        hide_seg = "  Tab:Hide" if can_hide_osk else ""
        hint = f"F1:Press  F2:Page  Ent:OK{hide_seg}  Esc:Back" if len(pages) > 1 else f"F1:Press  F2:Del  Ent:OK{hide_seg}  Esc:Back"
    elif mid:
        hint = "F1:Key  F2:Page  Ent:OK  Esc:Back" if len(pages) > 1 else "F1:Key  F2:Del  Ent:OK  Esc:Back"
    else:
        hint = "F1:Key F2:Pg Ent:OK K3:Esc" if len(pages) > 1 else "F1:Key F2:Del Ent:OK K3:Esc"

    hint_fit = _truncate_px(d, hint, width - pad_x * 2, f["hint"])
    d.text((width // 2, ftr_y0 + ftr_h // 2), hint_fit, font=f["hint"], fill=T["text_muted"], anchor="mm")

    return img


def lcd_keyboard(lcd, font, pins, gpio, title="Input", default="",
                 charset="full", max_len=64, show_keyboard: Optional[bool] = None):
    """
    Show a clean Obsidian Ember text input prompt on LCD / HDMI while accepting
    full lossless physical keyboard typing without GPIO button collisions.

    Visibility:
      - Auto-shows the on-screen digital keyboard on SPI button-HAT screens
        (`ST7735_128`, `ST7789_240`).
      - Hidden by default on `HDMI` and `CARDPUTER_320` (clean direct-typing dialog);
        press **Tab**, **F4**, or **Ctrl+K** anytime to toggle the digital keyboard.

    Controls (when digital keyboard is visible):
      - Arrows (UP/DOWN/LEFT/RIGHT): Move highlight across grid & SPACE/DEL/DONE bar
      - F1 (KEY1) or HAT OK: Press highlighted on-screen key / button
      - F2 (KEY2): Switch page (abc / ABC / 123), or Delete on single-page charsets
      - Enter: Submit input immediately
      - F3 (KEY3) / Esc: Cancel (return None)
      - Physical typing: Direct letters, Shift, CapsLock, Space, Backspace, Ctrl+U
    """
    if Image is None or ImageDraw is None:
        return default or ""

    pages = get_keyboard_pages(charset)
    page_idx = 0
    cursor_r = 0
    cursor_c = 0
    text = list(default)
    WIDTH, HEIGHT = lcd.width, lcd.height
    kb_fonts = _load_kb_fonts(WIDTH, HEIGHT, fallback_font=font)
    auto_osk = should_show_onscreen_keyboard(lcd=lcd)
    show_osk = auto_osk if show_keyboard is None else bool(show_keyboard)
    can_hide_osk = not auto_osk

    flush_input()
    remote_session_id = open_remote_text_session(
        title=title,
        default=default,
        charset=charset,
        max_len=max_len,
    )

    ctx = text_input_mode() if text_input_mode is not None else contextlib.nullcontext()

    dirty = True
    last_blink_on = None
    last_active_at = time.monotonic()
    last_vbtn_name = None
    last_vbtn_ts = 0.0
    last_gpio_name = None
    last_gpio_ts = 0.0

    def _move_cursor(direction: str) -> None:
        nonlocal cursor_r, cursor_c
        grid = pages[page_idx]["rows"]
        n_rows = len(grid)
        action_row = get_action_row(pages, page_idx, charset=charset)

        if direction == "UP":
            if cursor_r == n_rows:
                cursor_r = n_rows - 1
                cursor_c = _grid_col_from_action_col(cursor_c, action_row, len(grid[cursor_r]))
            elif cursor_r == 0:
                cursor_r = n_rows
                cursor_c = _action_col_from_grid_col(cursor_c, len(grid[0]), action_row)
            else:
                cursor_r -= 1
                cursor_c = min(cursor_c, len(grid[cursor_r]) - 1)
        elif direction == "DOWN":
            if cursor_r == n_rows - 1:
                cursor_c = _action_col_from_grid_col(cursor_c, len(grid[cursor_r]), action_row)
                cursor_r = n_rows
            elif cursor_r >= n_rows:
                cursor_r = 0
                cursor_c = _grid_col_from_action_col(cursor_c, action_row, len(grid[0]))
            else:
                cursor_r += 1
                cursor_c = min(cursor_c, len(grid[cursor_r]) - 1)
        elif direction == "LEFT":
            if cursor_r < n_rows:
                cursor_c = (cursor_c - 1) % len(grid[cursor_r])
            else:
                cursor_c = (cursor_c - 1) % len(action_row)
        elif direction == "RIGHT":
            if cursor_r < n_rows:
                cursor_c = (cursor_c + 1) % len(grid[cursor_r])
            else:
                cursor_c = (cursor_c + 1) % len(action_row)

    def _switch_page() -> None:
        nonlocal page_idx, cursor_r, cursor_c
        if len(pages) > 1:
            page_idx = (page_idx + 1) % len(pages)
            new_grid = pages[page_idx]["rows"]
            new_rows = len(new_grid)
            action_row = get_action_row(pages, page_idx, charset=charset)
            if cursor_r >= new_rows:
                cursor_r = new_rows
                cursor_c = min(cursor_c, len(action_row) - 1)
            else:
                cursor_c = min(cursor_c, len(new_grid[cursor_r]) - 1)
        else:
            if text:
                text.pop()

    def _press_selected() -> Tuple[bool, Optional[str]]:
        grid = pages[page_idx]["rows"]
        n_rows = len(grid)
        if cursor_r < n_rows:
            ch = grid[cursor_r][min(cursor_c, len(grid[cursor_r]) - 1)]
            if len(text) < max_len:
                text.append(ch)
            return False, None

        action_row = get_action_row(pages, page_idx, charset=charset)
        aid = action_row[min(cursor_c, len(action_row) - 1)]["id"]
        if aid == "space":
            if len(text) < max_len:
                text.append(" ")
        elif aid == "del":
            if text:
                text.pop()
        elif aid == "clear":
            text.clear()
        elif aid == "page":
            _switch_page()
        elif aid == "done":
            return True, "".join(text)
        return False, None

    def _poll_button() -> Optional[str]:
        """Read edge-triggered virtual buttons (fast 40ms cadence) + physical GPIO HAT fallback."""
        nonlocal last_vbtn_name, last_vbtn_ts, last_gpio_name, last_gpio_ts
        now_t = time.monotonic()

        # 1. Virtual buttons (F1/F2/F3 from keyboard, or WebUI buttons)
        for _ in range(8):
            mapped = get_virtual_button()
            if not mapped:
                break
            if _consume_suppressed_vbtn(mapped):
                continue
            if mapped == last_vbtn_name and (now_t - last_vbtn_ts) < 0.04:
                return None
            last_vbtn_name = mapped
            last_vbtn_ts = now_t
            return mapped

        # 2. Physical GPIO HAT fallback (skip keys currently held on physical keyboard so
        #    edge-triggered F1/F2/Arrows never double-fire via gpio_shim!)
        for btn_name, pin in pins.items():
            vpin = _BTN_TO_VIRTUAL.get(btn_name)
            if vpin and _rj_keyboard is not None and _rj_keyboard.is_pressed(vpin):
                continue
            if gpio.input(pin) == 0:
                name = _flip(btn_name)
                if name == last_gpio_name and (now_t - last_gpio_ts) < 0.15:
                    return None
                last_gpio_name = name
                last_gpio_ts = now_t
                return name
        last_gpio_name = None
        return None

    try:
        with ctx:
            while True:
                had_input = False

                # 1. Drain ALL queued local physical keyboard & WebUI remote text events
                while True:
                    text_event = get_remote_text_event(remote_session_id)
                    if not text_event:
                        break
                    had_input = True
                    special = str(text_event.get("special") or "")
                    if special == "ESCAPE":
                        return None
                    if special == "ENTER":
                        return "".join(text)
                    if special in ("BACKSPACE", "DELETE"):
                        if text:
                            text.pop()
                    elif special == "CTRL_U":
                        text.clear()
                    elif special in _OSK_TOGGLE_SPECIALS:
                        show_osk = not show_osk
                    elif special in ("UP", "DOWN", "LEFT", "RIGHT"):
                        act_dir = _flip(special)
                        # Suppress companion virtual-button datagram so 1 tap moves 1 cell
                        if text_event.get("source") == "keyboard" or text_event.get("session_id") == "local":
                            if not _drain_matching_vbtn(act_dir):
                                sup = getattr(_input_helper, "_suppressed_vbtns", None)
                                if isinstance(sup, dict):
                                    old_cnt, old_exp = sup.get(act_dir, (0, 0.0))
                                    now_s = time.monotonic()
                                    if now_s > old_exp:
                                        old_cnt = 0
                                    sup[act_dir] = (min(4, old_cnt + 1), now_s + 0.5)
                        if show_osk:
                            _move_cursor(act_dir)
                    elif not special:
                        key_value = str(text_event.get("key") or "")
                        for ch in key_value:
                            if len(text) >= max_len:
                                break
                            accepted = _filter_char(ch, charset)
                            if accepted:
                                text.append(accepted)

                # 2. Poll F1 (KEY1), F2 (KEY2), F3 (KEY3), HAT OK, and WebUI/HAT navigation
                btn = _poll_button()
                if btn:
                    had_input = True
                    if btn == "KEY3":
                        return None
                    elif not show_osk:
                        if btn == "OK":
                            return "".join(text)
                    elif btn in ("KEY1", "OK"):
                        done, val = _press_selected()
                        if done:
                            return val
                    elif btn == "KEY2":
                        _switch_page()
                    elif btn in ("UP", "DOWN", "LEFT", "RIGHT"):
                        _move_cursor(btn)

                now_m = time.monotonic()
                if had_input:
                    dirty = True
                    last_active_at = now_m
                blink_on = (int(now_m * 2.0) % 2 == 0) if (now_m - last_active_at) < 20.0 else True
                if blink_on != last_blink_on:
                    last_blink_on = blink_on
                    dirty = True

                if not dirty:
                    time.sleep(0.02 if (now_m - last_active_at) < 1.5 else 0.04)
                    continue
                dirty = False

                img = render_keyboard_frame(
                    WIDTH,
                    HEIGHT,
                    title=title,
                    text_str="".join(text),
                    pages=pages,
                    page_idx=page_idx,
                    cursor_r=cursor_r,
                    cursor_c=cursor_c,
                    charset=charset,
                    max_len=max_len,
                    blink_on=blink_on,
                    fonts=kb_fonts,
                    show_osk=show_osk,
                    can_hide_osk=can_hide_osk,
                )
                lcd.LCD_ShowImage(img, 0, 0)
                time.sleep(0.015)
    finally:
        close_remote_text_session(remote_session_id)
        flush_input()
