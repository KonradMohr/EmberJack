#!/usr/bin/env python3
"""
RaspyJack Payload -- Payload Search
=====================================
Author: 7h30th3r0n3

Search installed payloads by keyword and view their docstrings.
Scans the payloads directory for .py files (excluding _ prefixed helpers).

Controls
--------
  UP / DOWN    -- Navigate character picker / results
  LEFT / RIGHT -- Move cursor in search input
  OK           -- Confirm character / select result to view docstring
  KEY1         -- Start new search
  KEY3         -- Exit (or back from docstring view)
"""

import os
import sys
import time
import signal
import ast

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import rj_theme
from PIL import Image
from payloads._display_helper import ScaledDraw, scaled_font
from payloads._input_helper import get_button
from payloads._keyboard_helper import lcd_keyboard

_T = rj_theme.EMBER_THEME

PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}
GPIO.setmode(GPIO.BCM)
for pin in PINS.values():
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

LCD = LCD_1in44.LCD()
LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
WIDTH, HEIGHT = LCD.width, LCD.height
font = scaled_font()

PAYLOAD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DEBOUNCE = 0.18
ROW_H = 12
VISIBLE_ROWS = 7
_running = True


def _cleanup(*_args):
    global _running
    _running = False


signal.signal(signal.SIGINT, _cleanup)
signal.signal(signal.SIGTERM, _cleanup)


# ---------------------------------------------------------------------------
# Payload scanning
# ---------------------------------------------------------------------------

def _scan_payloads():
    """Recursively find all .py payload files (excluding _ prefixed)."""
    results = []
    for root, _dirs, files in os.walk(PAYLOAD_PATH):
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            if fname.startswith("_"):
                continue
            rel = os.path.relpath(os.path.join(root, fname), PAYLOAD_PATH)
            results.append({"name": fname[:-3], "rel": rel,
                            "full": os.path.join(root, fname)})
    return results


_DOC_CACHE = {}
_CATALOG_DESC_INDEX = None


def _get_catalog_desc_index():
    global _CATALOG_DESC_INDEX
    if _CATALOG_DESC_INDEX is not None:
        return _CATALOG_DESC_INDEX
    idx = {}
    try:
        import rj_tool_catalog
        for cat, items in rj_tool_catalog.TOOL_CATALOG.items():
            for item in items:
                tid = str(item.get("id") or "").strip()
                title = str(item.get("title") or tid).strip()
                desc = str(item.get("desc") or "").strip()
                if tid and desc:
                    entry_text = f"{title}\n\n{desc}"
                    idx[f"{cat}/{tid}"] = entry_text
                    idx.setdefault(tid, entry_text)
    except Exception:
        pass
    _CATALOG_DESC_INDEX = idx
    return idx


def _extract_docstring(filepath, rel=""):
    """Return cached catalog description or module docstring from *filepath*."""
    cache_key = rel or filepath
    if cache_key in _DOC_CACHE:
        return _DOC_CACHE[cache_key]
    norm = str(rel or "").replace("\\", "/")
    if norm.endswith(".py"):
        norm = norm[:-3]
    cat_idx = _get_catalog_desc_index()
    cat_doc = cat_idx.get(norm) or cat_idx.get(os.path.splitext(os.path.basename(filepath))[0])
    if cat_doc:
        _DOC_CACHE[cache_key] = cat_doc
        return cat_doc
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read(), filename=filepath)
        docstring = ast.get_docstring(tree)
        res = docstring if docstring else "(no docstring)"
    except Exception:
        res = "(parse error)"
    _DOC_CACHE[cache_key] = res
    return res


def _search(payloads, keyword):
    """Filter payloads whose name or path contains the keyword."""
    kw = keyword.lower().strip()
    if not kw:
        return list(payloads)
    return [p for p in payloads if kw in p["name"].lower()
            or kw in p["rel"].lower()]


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def _draw_header(d, title):
    d.rectangle((0, 0, 127, 14), fill=_T["bg_surface"])
    d.rectangle((0, 0, 2, 14), fill=_T["accent"])
    d.line((0, 14, 127, 14), fill=_T["border_accent"], width=1)
    d.text((6, 1), title[:19], font=font, fill=_T["text_bright"])
    d.text((111, 1), "K3", font=font, fill=_T["text_muted"])


def _draw_footer(d, text):
    d.rectangle((0, 115, 127, 127), fill=_T["bg_well"])
    d.line((0, 115, 127, 115), fill=_T["border_subtle"], width=1)
    d.text((3, 117), text[:26], font=font, fill=_T["text_muted"])


def _draw_results(matches, cursor, scroll):
    """Display Obsidian Ember search results list."""
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ScaledDraw(img)
    _draw_header(d, f"RESULTS ({len(matches)})")

    y = 17
    end = min(len(matches), scroll + VISIBLE_ROWS)
    for i in range(scroll, end):
        is_selected = i == cursor
        if is_selected:
            d.rounded_rectangle((2, y - 1, 122, y + ROW_H - 1), radius=3, fill=_T["select_bg"], outline=_T["border_accent"])
            d.rectangle((2, y, 4, y + ROW_H - 2), fill=_T["accent"])
        fg = _T["accent_bright"] if is_selected else _T["text_primary"]
        d.text((7, y), matches[i]["name"][:19], font=font, fill=fg)
        y += ROW_H + 1

    if len(matches) > VISIBLE_ROWS:
        rj_theme.draw_scrollbar(d, 124, 17, 112, scroll, VISIBLE_ROWS, len(matches), width=3)

    if not matches:
        d.rounded_rectangle((8, 42, 119, 76), radius=4, fill=_T["bg_surface"], outline=_T["border_subtle"])
        d.text((64, 59), "No matches found", font=font, fill=_T["warn"], anchor="mm")

    _draw_footer(d, "OK:View K1:Search K3:Bk")
    LCD.LCD_ShowImage(img, 0, 0)


def _draw_docstring(name, docstring, scroll):
    """Display a payload's docstring in an Obsidian Ember card."""
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ScaledDraw(img)
    _draw_header(d, name[:18])

    d.rounded_rectangle((2, 16, 122, 113), radius=4, fill=_T["bg_surface"], outline=_T["border_subtle"])
    lines = docstring.split("\n")
    y = 19
    end = min(len(lines), scroll + VISIBLE_ROWS + 1)
    for i in range(scroll, end):
        d.text((6, y), lines[i][:22], font=font, fill=_T["text_primary"])
        y += ROW_H
        if y > 108:
            break

    if len(lines) > VISIBLE_ROWS + 1:
        rj_theme.draw_scrollbar(d, 124, 17, 112, scroll, VISIBLE_ROWS + 1, len(lines), width=3)

    _draw_footer(d, "UP/DN:Scroll  K3:Back")
    LCD.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _running

    all_payloads = _scan_payloads()
    state = "input"  # input | results | docstring
    matches = []
    cursor = 0
    scroll = 0
    doc_text = ""
    doc_name = ""
    doc_scroll = 0
    last_press = 0.0

    dirty = True
    try:
        while _running:
            if state == "input":
                keyword = lcd_keyboard(LCD, font, PINS, GPIO,
                                       title="PAYLOAD SEARCH",
                                       charset="full")
                if keyword is None:
                    break
                matches = _search(all_payloads, keyword)
                cursor = 0
                scroll = 0
                state = "results"
                dirty = True
                continue

            if dirty:
                if state == "results":
                    _draw_results(matches, cursor, scroll)
                elif state == "docstring":
                    _draw_docstring(doc_name, doc_text, doc_scroll)
                dirty = False

            btn = get_button(PINS, GPIO)
            now = time.time()
            if btn and (now - last_press) < DEBOUNCE:
                btn = None
            if btn:
                last_press = now

            if btn == "KEY3":
                if state == "docstring":
                    state = "results"
                    dirty = True
                    continue
                elif state == "results":
                    state = "input"
                    dirty = True
                    continue
                else:
                    break

            if state == "results":
                if btn == "DOWN" and matches:
                    cursor = min(cursor + 1, len(matches) - 1)
                    if cursor >= scroll + VISIBLE_ROWS:
                        scroll = cursor - VISIBLE_ROWS + 1
                    dirty = True
                elif btn == "UP":
                    cursor = max(cursor - 1, 0)
                    if cursor < scroll:
                        scroll = cursor
                    dirty = True
                elif btn == "OK" and matches:
                    entry = matches[cursor]
                    doc_text = _extract_docstring(entry["full"], entry["rel"])
                    doc_name = entry["name"]
                    doc_scroll = 0
                    state = "docstring"
                    dirty = True
                elif btn == "KEY1":
                    state = "input"
                    dirty = True

            elif state == "docstring":
                lines = doc_text.split("\n")
                max_scroll = max(0, len(lines) - VISIBLE_ROWS - 1)
                if btn == "DOWN":
                    doc_scroll = min(doc_scroll + 1, max_scroll)
                    dirty = True
                elif btn == "UP":
                    doc_scroll = max(doc_scroll - 1, 0)
                    dirty = True

            time.sleep(0.05)

    finally:
        try:
            LCD.LCD_Clear()
        except Exception:
            pass
        GPIO.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
