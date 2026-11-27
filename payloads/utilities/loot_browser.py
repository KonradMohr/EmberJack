#!/usr/bin/env python3
"""
RaspyJack Payload -- Loot Browser
----------------------------------
Author: 7h30th3r0n3
Fixed: Hosseios

Browse /root/Raspyjack/loot/ on the LCD.

Controls:
  UP/DOWN  = navigate files/dirs or scroll preview
  OK       = enter directory or preview file
  LEFT     = go up one directory
  KEY1     = show stats (file count, total size)
  KEY2     = delete selected file (with confirmation)
  KEY3     = exit
"""

import os
import sys
import time

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import rj_env
import rj_theme
from PIL import Image
from payloads._display_helper import ScaledDraw, scaled_font
from payloads._input_helper import get_button

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

LOOT_ROOT = os.path.join(rj_env.get_base_dir(), "loot")
DEBOUNCE = 0.25

# Chars per line depends on screen resolution
_SCALE = getattr(LCD_1in44, "LCD_SCALE", 1.0)
CHARS_PER_LINE = int(20 * _SCALE)  # 20 on 128, ~37 on 240


def _fmt_size(nbytes):
    for unit in ("B", "K", "M", "G"):
        if nbytes < 1024:
            return f"{nbytes}{unit}"
        nbytes //= 1024
    return f"{nbytes}T"


def _list_dir(path):
    entries = []
    try:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            is_dir = os.path.isdir(full)
            try:
                size = os.path.getsize(full) if not is_dir else 0
            except OSError:
                size = 0
            entries.append({"name": name, "is_dir": is_dir, "size": size, "path": full})
    except OSError:
        pass
    return entries


def _dir_stats(path):
    total_files = 0
    total_size = 0
    try:
        for root, dirs, files in os.walk(path):
            total_files += len(files)
            for f in files:
                try:
                    total_size += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total_files, total_size


def _is_text_file(path):
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(512)
            if b"\x00" in chunk:
                return False
            return True
    except OSError:
        return False


def _preview_text(path, max_lines=1000):
    lines = []
    try:
        with open(path, "r", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                lines.append(line.rstrip("\n"))
    except OSError:
        lines.append("(read error)")
    return lines


def _file_type_label(path):
    ext = os.path.splitext(path)[1].lower()
    type_map = {
        ".txt": "TEXT", ".log": "LOG", ".csv": "CSV",
        ".json": "JSON", ".xml": "XML", ".pcap": "PCAP",
        ".cap": "CAP", ".png": "IMG", ".jpg": "IMG",
        ".py": "PY", ".sh": "SHELL", ".conf": "CONF",
    }
    return type_map.get(ext, "BIN")


def _draw_browser(lcd, cwd, entries, cursor, scroll_offset, status=""):
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ScaledDraw(img)

    rel_path = cwd.replace(LOOT_ROOT, "loot") if cwd.startswith(LOOT_ROOT) else cwd
    max_path = max(12, CHARS_PER_LINE - 5)
    if len(rel_path) > max_path:
        rel_path = "..." + rel_path[-(max_path - 3):]

    d.rectangle((0, 0, 127, 13), fill=_T["bg_surface"])
    d.rectangle((0, 0, 2, 13), fill=_T["accent"])
    d.line((0, 13, 127, 13), fill=_T["border_accent"], width=1)
    d.text((5, 1), rel_path, font=font, fill=_T["text_bright"])
    d.text((111, 1), "K3", font=font, fill=_T["text_muted"])

    y = 16
    visible = 7
    start = scroll_offset
    end = min(len(entries), start + visible)

    if not entries:
        d.rounded_rectangle((6, 36, 121, 70), radius=4, fill=_T["bg_surface"], outline=_T["border_subtle"])
        d.text((64, 53), "(empty directory)", font=font, fill=_T["text_muted"], anchor="mm")
    else:
        for idx in range(start, end):
            entry = entries[idx]
            is_cursor = idx == cursor
            if is_cursor:
                d.rounded_rectangle((2, y - 1, 122, y + 11), radius=3, fill=_T["select_bg"], outline=_T["border_accent"])
                d.rectangle((2, y, 4, y + 10), fill=_T["accent"])
            if entry["is_dir"]:
                label = f"[{entry['name'][:CHARS_PER_LINE - 3]}]"
                color = _T["accent_bright"] if is_cursor else _T["accent"]
            else:
                size_str = _fmt_size(entry["size"])
                name_max = max(6, CHARS_PER_LINE - 3 - len(size_str))
                name_short = entry["name"][:name_max]
                label = f"{name_short} {size_str}"
                color = _T["text_bright"] if is_cursor else _T["text_primary"]
            d.text((6, y), label[:CHARS_PER_LINE], font=font, fill=color)
            y += 13

        if len(entries) > visible:
            rj_theme.draw_scrollbar(d, 124, 16, 105, start, visible, len(entries), width=3)

    d.rectangle((0, 106, 127, 127), fill=_T["bg_well"])
    d.line((0, 106, 127, 106), fill=_T["border_subtle"], width=1)
    d.text((3, 108), "OK:Open  Left:Up", font=font, fill=_T["text_muted"])
    d.text((3, 117), "K1:Stats K2:Del K3:Exit", font=font, fill=_T["text_muted"])

    if status:
        d.rounded_rectangle((8, 46, 119, 76), radius=5, fill=_T["bg_surface"], outline=_T["border_accent"])
        d.rectangle((8, 50, 11, 72), fill=_T["accent"])
        d.text((64, 61), status[:CHARS_PER_LINE], font=font, fill=_T["accent_bright"], anchor="mm")

    lcd.LCD_ShowImage(img, 0, 0)


def _draw_preview(lcd, path, lines, h_offset=0):
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ScaledDraw(img)

    name = os.path.basename(path)
    max_name = max(10, CHARS_PER_LINE - 2)
    if len(name) > max_name:
        name = name[:max_name - 3] + "..."
    d.rectangle((0, 0, 127, 13), fill=_T["bg_surface"])
    d.rectangle((0, 0, 2, 13), fill=_T["accent"])
    d.line((0, 13, 127, 13), fill=_T["border_accent"], width=1)
    d.text((5, 1), name, font=font, fill=_T["accent_bright"])

    y = 16
    for line in lines:
        display = line[h_offset:h_offset + CHARS_PER_LINE]
        d.text((3, y), display, font=font, fill=_T["text_primary"])
        y += 12

    d.rectangle((0, 114, 127, 127), fill=_T["bg_well"])
    d.line((0, 114, 127, 114), fill=_T["border_subtle"], width=1)
    hint = "U/D:Scrl L/R:Pan K3:Bk"
    if h_offset > 0:
        hint = f"<{h_offset} " + hint
    d.text((3, 116), hint[:CHARS_PER_LINE], font=font, fill=_T["text_muted"])
    lcd.LCD_ShowImage(img, 0, 0)


def _draw_confirm(lcd, filename):
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ScaledDraw(img)
    d.rounded_rectangle((6, 22, 121, 102), radius=5, fill=_T["bg_surface"], outline=_T["danger"])
    d.rectangle((6, 26, 9, 98), fill=_T["danger"])
    d.text((14, 30), "Delete file?", font=font, fill=_T["danger"])
    d.text((14, 48), filename[:CHARS_PER_LINE - 2], font=font, fill=_T["text_bright"])
    d.text((14, 70), "OK = Delete", font=font, fill=_T["accent_bright"])
    d.text((14, 84), "Any = Cancel", font=font, fill=_T["text_muted"])
    lcd.LCD_ShowImage(img, 0, 0)


def _draw_stats(lcd, path, file_count, total_size):
    img = Image.new("RGB", (WIDTH, HEIGHT), _T["bg_base"])
    d = ScaledDraw(img)
    d.rectangle((0, 0, 127, 13), fill=_T["bg_surface"])
    d.rectangle((0, 0, 2, 13), fill=_T["accent"])
    d.line((0, 13, 127, 13), fill=_T["border_accent"], width=1)
    d.text((6, 1), "Loot Stats", font=font, fill=_T["text_bright"])

    d.rounded_rectangle((6, 20, 121, 96), radius=5, fill=_T["bg_surface"], outline=_T["border_subtle"])
    d.text((12, 28), f"Files: {file_count}", font=font, fill=_T["accent_bright"])
    d.text((12, 46), f"Size:  {_fmt_size(total_size)}", font=font, fill=_T["text_bright"])
    rel = path.replace(LOOT_ROOT, "loot")
    if len(rel) > CHARS_PER_LINE - 2:
        rel = "..." + rel[-(CHARS_PER_LINE - 5):]
    d.text((12, 68), rel, font=font, fill=_T["text_muted"])
    d.text((12, 106), "Press any key...", font=font, fill=_T["text_muted"])
    lcd.LCD_ShowImage(img, 0, 0)


def main():
    if not os.path.isdir(LOOT_ROOT):
        try:
            os.makedirs(LOOT_ROOT, exist_ok=True)
        except OSError:
            pass

    cwd = LOOT_ROOT
    entries = _list_dir(cwd)
    cursor = 0
    scroll_offset = 0
    status = ""
    last_press = 0.0
    visible = 7

    # Wait for button release from menu (prevents ghost OK press)
    time.sleep(0.3)
    while get_button(PINS, GPIO) is not None:
        time.sleep(0.05)

    dirty = True
    try:
        while True:
            if dirty:
                _draw_browser(LCD, cwd, entries, cursor, scroll_offset, status)
                dirty = False

            btn = get_button(PINS, GPIO)
            now = time.time()
            if btn and (now - last_press) < DEBOUNCE:
                btn = None
            if btn:
                last_press = now

            # Browse mode
            if btn == "KEY3":
                break
            elif btn == "UP":
                cursor = max(0, cursor - 1)
                if cursor < scroll_offset:
                    scroll_offset = cursor
                status = ""
                dirty = True
            elif btn == "DOWN":
                cursor = min(max(0, len(entries) - 1), cursor + 1)
                if cursor >= scroll_offset + visible:
                    scroll_offset = cursor - visible + 1
                status = ""
                dirty = True
            elif btn == "LEFT":
                if cwd != LOOT_ROOT:
                    cwd = os.path.dirname(cwd)
                    entries = _list_dir(cwd)
                    cursor = 0
                    scroll_offset = 0
                    status = ""
                    dirty = True
            elif btn == "OK" and entries:
                entry = entries[cursor]
                if entry["is_dir"]:
                    cwd = entry["path"]
                    entries = _list_dir(cwd)
                    cursor = 0
                    scroll_offset = 0
                    status = ""
                    dirty = True
                else:
                    # Scrollable preview
                    if _is_text_file(entry["path"]):
                        lines = _preview_text(entry["path"], max_lines=1000)
                    else:
                        ftype = _file_type_label(entry["path"])
                        lines = [
                            f"Type: {ftype}",
                            f"Size: {_fmt_size(entry['size'])}",
                            "",
                            "(binary file)",
                        ]
                    scroll_offset_preview = 0
                    h_offset = 0
                    max_display = 8
                    # Debounce: wait for OK release before entering preview
                    time.sleep(0.2)
                    while get_button(PINS, GPIO) is not None:
                        time.sleep(0.05)
                    preview_dirty = True
                    while True:
                        if preview_dirty:
                            visible_lines = lines[scroll_offset_preview:scroll_offset_preview+max_display]
                            _draw_preview(LCD, entry["path"], visible_lines, h_offset)
                            preview_dirty = False
                        btn_preview = get_button(PINS, GPIO)
                        if btn_preview == "UP":
                            scroll_offset_preview = max(0, scroll_offset_preview - 1)
                            preview_dirty = True
                        elif btn_preview == "DOWN":
                            scroll_offset_preview = min(max(0, len(lines)-max_display), scroll_offset_preview + 1)
                            preview_dirty = True
                        elif btn_preview == "RIGHT":
                            h_offset += 10
                            preview_dirty = True
                        elif btn_preview == "LEFT":
                            h_offset = max(0, h_offset - 10)
                            preview_dirty = True
                        elif btn_preview == "KEY3" or btn_preview == "OK":
                            break
                        time.sleep(0.05)
                    dirty = True

            elif btn == "KEY1":
                fc, ts = _dir_stats(cwd)
                _draw_stats(LCD, cwd, fc, ts)
                while True:
                    btn_stats = get_button(PINS, GPIO)
                    if btn_stats:
                        break
                    time.sleep(0.05)
                dirty = True

            elif btn == "KEY2" and entries:
                entry = entries[cursor]
                if not entry["is_dir"]:
                    _draw_confirm(LCD, entry["name"])
                    while True:
                        btn_confirm = get_button(PINS, GPIO)
                        if btn_confirm == "OK":
                            try:
                                os.remove(entry["path"])
                                status = "Deleted!"
                            except OSError as exc:
                                status = f"Err: {str(exc)[:14]}"
                            entries = _list_dir(cwd)
                            cursor = min(cursor, max(0, len(entries) - 1))
                            break
                        elif btn_confirm:
                            status = "Cancelled"
                            break
                        time.sleep(0.05)
                else:
                    status = "Can't del dirs"
                dirty = True

            time.sleep(0.05)

    finally:
        LCD.LCD_Clear()
        GPIO.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
