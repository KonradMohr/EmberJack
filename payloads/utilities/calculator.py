#!/usr/bin/env python3
"""
EmberJack Payload -- Scientific Calculator
===========================================
Author: 7h30th3r0n3

Obsidian Ember scientific calculator with widescreen display well, tactile
keypad, live history tape, programmer base converter, and direct physical
keyboard entry.

Controls:
  0-9, ., +, -, *, /, ^, (, )  Direct keyboard math entry
  Enter / =                    Evaluate expression
  Backspace                    Delete last character
  Delete / C                   Clear expression
  Arrows + OK / Space          Navigate & press keypad grid
  KEY1 / Tab                   Toggle Standard / Scientific mode
  KEY2 / B                     Base conversion overlay (DEC/HEX/BIN/OCT)
  H                            History overlay (or H key in grid)
  KEY3 / Esc                   Close overlay / Exit
"""

import math
import os
import signal
import sys
import time

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
from PIL import Image, ImageDraw

import rj_env
import rj_theme
from payloads._input_helper import (
    flush_input,
    format_footer_hint,
    is_action,
    is_back,
    is_confirm,
    is_dismiss,
    poll_ui_input,
    text_input_mode,
)

PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
for pin in PINS.values():
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

LCD = LCD_1in44.LCD()
LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
WIDTH, HEIGHT = LCD.width, LCD.height
FONTS = rj_theme.load_app_fonts(WIDTH, HEIGHT)
IS_WIDE = bool(FONTS["wide"])
IS_LARGE = bool(FONTS["large"])
IS_240 = (not IS_WIDE) and WIDTH >= 200
ET = rj_theme.EMBER_THEME

FONT_EXPR = rj_env.load_font(15 if IS_LARGE else (10 if IS_WIDE else (11 if IS_240 else 9)), bold=False, mono=True)
FONT_RES = rj_env.load_font(22 if IS_LARGE else (13 if IS_WIDE else (15 if IS_240 else 10)), bold=True, mono=True)
FONT_KEY = rj_env.load_font(14 if IS_LARGE else (10 if IS_WIDE else (11 if IS_240 else 8)), bold=True)

# ---------------------------------------------------------------------------
# Safe math evaluation namespace (preserved 100%)
# ---------------------------------------------------------------------------
_SAFE_MATH = {
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "sqrt": math.sqrt,
    "pow": math.pow,
    "log": math.log10,
    "ln": math.log,
    "pi": math.pi,
    "e": math.e,
    "abs": abs,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
}


def _safe_eval(expression):
    """Evaluate a math expression safely, returning (result_str, error)."""
    if not expression.strip():
        return ("", None)
    expr = expression.replace("×", "*").replace("÷", "/")
    try:
        result = eval(expr, {"__builtins__": {}}, _SAFE_MATH)  # noqa: S307
    except ZeroDivisionError:
        return ("", "Div by zero")
    except (SyntaxError, TypeError):
        return ("", "Syntax error")
    except (ValueError, OverflowError) as exc:
        return ("", str(exc)[:22])
    except Exception as exc:
        return ("", str(exc)[:22])
    if isinstance(result, float):
        if result == int(result) and abs(result) < 1e15:
            return (str(int(result)), None)
        return (f"{result:.10g}", None)
    return (str(result), None)


# ---------------------------------------------------------------------------
# Base conversion
# ---------------------------------------------------------------------------
_BASES = ["DEC", "HEX", "BIN", "OCT"]


def _convert_base(value_str, target_base):
    """Convert a decimal integer string to the target base representation."""
    try:
        val = int(float(value_str))
    except (ValueError, OverflowError):
        return "N/A"
    if target_base == "DEC":
        return str(val)
    if target_base == "HEX":
        return hex(val)
    if target_base == "BIN":
        return bin(val)
    if target_base == "OCT":
        return oct(val)
    return str(val)


# ---------------------------------------------------------------------------
# Button grid definitions
# ---------------------------------------------------------------------------
GRID_STD = [
    ["C", "CE", "(", ")", "÷"],
    ["7", "8", "9", "×", "^"],
    ["4", "5", "6", "-", "√"],
    ["1", "2", "3", "+", "H"],
    ["0", ".", "±", "=", "←"],
]

GRID_SCI = [
    ["C", "CE", "(", ")", "÷"],
    ["sin", "cos", "tan", "×", "^"],
    ["ln", "log", "√", "-", "π"],
    ["7", "8", "9", "+", "e"],
    ["4", "5", "6", ".", "H"],
    ["1", "2", "3", "=", "←"],
    ["0", "A", "B", "D", "F"],
]


def _get_grid(scientific, hex_mode):
    if scientific:
        return GRID_SCI
    if hex_mode:
        grid = [row[:] for row in GRID_STD]
        grid.append(["A", "B", "C₂", "D", "F"])
        return grid
    return GRID_STD


def _btn_type(label):
    if label == "=":
        return "equal"
    if label in ("+", "-", "×", "÷", "^"):
        return "operator"
    if label in ("C", "CE", "←"):
        return "clear"
    if label in ("sin", "cos", "tan", "ln", "log", "√",
                 "π", "e", "(", ")", "H", "±", "C₂"):
        return "function"
    return "number"


def _find_key_in_grid(grid, label):
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            if cell == label:
                return r, c
    return None


# ---------------------------------------------------------------------------
# Native Obsidian Ember Renderer
# ---------------------------------------------------------------------------
def _tail_fit(d, text, max_w, font):
    if not text:
        return ""
    bb = rj_theme.measure_text(font, text, d)
    if (bb[2] - bb[0]) <= max_w:
        return text
    lo, hi = 1, len(text)
    best = text[-1:]
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = "…" + text[-mid:]
        cb = rj_theme.measure_text(font, cand, d)
        if (cb[2] - cb[0]) <= max_w:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _render_calculator(expression, result, error, history, scientific, sel_row, sel_col,
                       show_history, show_conversion, conv_base_idx):
    img = Image.new("RGB", (WIDTH, HEIGHT), ET["bg_base"])
    d = ImageDraw.Draw(img)

    mode_label = "SCI" if scientific else "STD"
    hdr_h = rj_theme.draw_app_header(
        d, WIDTH, "CALCULATOR" if IS_WIDE else "CALC", FONTS,
        badge=mode_label,
        right_text="DIRECT KB READY" if IS_LARGE else "",
        right_pill="B:BASE" if IS_WIDE else "",
    )
    if show_history:
        foot_hint = format_footer_hint(
            WIDTH, HEIGHT,
            hat_hint="OK/K3:Close",
            hat_240_hint="OK / K3: Close History",
            cardputer_hint="Enter / Esc: Close History",
            hdmi_hint="Enter / Esc: Close History",
        )
    elif show_conversion:
        foot_hint = format_footer_hint(
            WIDTH, HEIGHT,
            hat_hint="U/D:Sel OK:Use K3:Bk",
            hat_240_hint="U/D:Base  OK:Use  K3:Back",
            cardputer_hint="U/D:Base  Enter:Use  Esc:Back",
            hdmi_hint="Up/Down:Select Base  •  Enter:Use Value  •  Esc:Close",
        )
    else:
        foot_hint = format_footer_hint(
            WIDTH, HEIGHT,
            hat_hint="OK:Key K1:Sci K2:B K3:X",
            hat_240_hint="OK:Key K1:Sci K2:Base K3:Exit",
            cardputer_hint="0-9/+-*/  Enter:=  Tab:Sci  B:Base  C:Clr  Esc:Exit",
            hdmi_hint="0-9 +-*/^  Enter:=  Bksp:Del  C:Clr  Tab:Sci  B:Base  H:Hist  Esc:Exit",
        )
    ftr_h = rj_theme.draw_app_footer(d, WIDTH, HEIGHT, foot_hint, FONTS)

    pad = 8 if IS_LARGE else (4 if IS_WIDE else (5 if IS_240 else 3))
    y0 = hdr_h + pad
    y1 = HEIGHT - ftr_h - pad
    split = WIDTH >= 480

    # --- Full-screen overlays when triggered ---
    if show_history:
        rj_theme.draw_card(
            d, (pad, y0, WIDTH - pad, y1),
            fill=ET["bg_surface"], outline=ET["border_accent"],
            radius=8 if IS_WIDE else 4, accent_left=ET["accent"], accent_width=3 if IS_WIDE else 2,
        )
        title_y = y0 + (8 if IS_LARGE else (5 if IS_WIDE else 4))
        div_y = title_y + (20 if IS_LARGE else (15 if IS_WIDE else (14 if IS_240 else 11)))
        d.text((pad + (12 if IS_WIDE else 6), title_y), "CALCULATION HISTORY" if (IS_WIDE or IS_240) else "HISTORY", font=FONTS["title"], fill=ET["accent_bright"])
        d.line([(pad + 6, div_y), (WIDTH - pad - 6, div_y)], fill=ET["border_subtle"], width=1)
        if not history:
            d.text((WIDTH // 2, (div_y + y1) // 2), "No calculations yet", font=FONTS["body"], fill=ET["text_muted"], anchor="mm")
        else:
            ry = div_y + (6 if IS_LARGE else 4)
            step = 24 if IS_LARGE else (15 if IS_WIDE else (16 if IS_240 else 11))
            for expr_s, res_s in reversed(history[-10:]):
                if ry + step > y1 - 2:
                    break
                line = f"{expr_s}  =  {res_s}"
                d.text((pad + (12 if IS_WIDE else 6), ry), rj_theme.truncate_text(d, line, WIDTH - pad * 2 - (24 if IS_WIDE else 12), FONTS["mono"]), font=FONTS["mono"], fill=ET["text_primary"])
                ry += step
        LCD.LCD_ShowImage(img, 0, 0)
        return

    if show_conversion:
        rj_theme.draw_card(
            d, (pad, y0, WIDTH - pad, y1),
            fill=ET["bg_surface"], outline=ET["border_accent"],
            radius=8 if IS_WIDE else 4, accent_left=ET["accent"], accent_width=3 if IS_WIDE else 2,
        )
        title_y = y0 + (8 if IS_LARGE else (5 if IS_WIDE else 4))
        div_y = title_y + (20 if IS_LARGE else (15 if IS_WIDE else (14 if IS_240 else 11)))
        d.text((pad + (12 if IS_WIDE else 6), title_y), "BASE CONVERTER" if (IS_WIDE or IS_240) else "BASES", font=FONTS["title"], fill=ET["accent_bright"])
        d.line([(pad + 6, div_y), (WIDTH - pad - 6, div_y)], fill=ET["border_subtle"], width=1)
        if not result:
            d.text((WIDTH // 2, (div_y + y1) // 2), "Evaluate an expression first" if (IS_WIDE or IS_240) else "Eval first", font=FONTS["body"], fill=ET["text_muted"], anchor="mm")
        else:
            ry = div_y + (6 if IS_LARGE else 4)
            avail_conv = max(16, y1 - ry - 3)
            max_rh = 38 if IS_LARGE else (24 if IS_WIDE else (26 if IS_240 else 18))
            row_h = min(max_rh, max(12, avail_conv // len(_BASES)))
            row_gap = 4 if IS_LARGE else 2
            bx_pad = 10 if IS_WIDE else 5
            for i, base in enumerate(_BASES):
                ry_bot = min(y1 - 3, ry + row_h - row_gap)
                if ry_bot - ry < 10:
                    break
                conv = _convert_base(result, base)
                sel = (i == conv_base_idx)
                rj_theme.draw_card(
                    d, (pad + bx_pad, ry, WIDTH - pad - bx_pad, ry_bot),
                    fill=ET["select_bg"] if sel else ET["bg_well"],
                    outline=ET["border_accent"] if sel else ET["border_subtle"],
                    radius=5 if IS_WIDE else 3,
                    accent_left=ET["accent"] if sel else None, accent_width=3 if IS_WIDE else 2,
                )
                mid_y = (ry + ry_bot) // 2
                d.text((pad + bx_pad + (10 if IS_WIDE else 5), mid_y), base, font=FONTS["badge"], fill=ET["accent_bright"] if sel else ET["text_muted"], anchor="lm")
                val_x = pad + (66 if IS_WIDE else (52 if IS_240 else 38))
                val_txt = rj_theme.truncate_text(d, conv, max(24, WIDTH - pad - bx_pad - val_x - 4), FONTS["mono"])
                d.text((val_x, mid_y), val_txt, font=FONTS["mono"], fill=ET["text_bright"] if sel else ET["text_primary"], anchor="lm")
                ry += row_h
        LCD.LCD_ShowImage(img, 0, 0)
        return

    # --- Main Calculator View ---
    calc_x0 = pad
    calc_x1 = int(WIDTH * 0.62) - 4 if split else (WIDTH - pad)

    # 1. Recessed Display Well (clamped for 320x170 vertical safety)
    disp_h = 66 if IS_LARGE else (30 if IS_WIDE else (42 if IS_240 else 26))
    disp_y1 = y0 + disp_h
    rj_theme.draw_card(
        d, (calc_x0, y0, calc_x1, disp_y1),
        fill=ET["bg_well"], outline=ET["border_accent"],
        radius=7 if IS_WIDE else 4,
    )

    inner_x0 = calc_x0 + (12 if IS_LARGE else (6 if IS_WIDE else 4))
    inner_x1 = calc_x1 - (12 if IS_LARGE else (6 if IS_WIDE else 4))
    inner_w = max(20, inner_x1 - inner_x0)

    expr_shown = _tail_fit(d, expression or "0", inner_w, FONT_EXPR)
    d.text((inner_x1, y0 + (14 if IS_LARGE else (7 if IS_WIDE else (10 if IS_240 else 6)))), expr_shown, font=FONT_EXPR, fill=ET["text_muted"] if not expression else ET["text_primary"], anchor="rm")

    res_y = disp_y1 - (20 if IS_LARGE else (9 if IS_WIDE else (14 if IS_240 else 8)))
    if error:
        err_shown = rj_theme.truncate_text(d, error, inner_w, FONTS["body_bold"])
        d.text((inner_x1, res_y), err_shown, font=FONTS["body_bold"], fill=ET["danger"], anchor="rm")
    elif result:
        res_shown = _tail_fit(d, f"= {result}", inner_w, FONT_RES)
        d.text((inner_x1, res_y), res_shown, font=FONT_RES, fill=ET["accent_bright"], anchor="rm")
    else:
        d.text((inner_x1, res_y), "0", font=FONT_RES, fill=ET["text_dim"], anchor="rm")

    # 2. Tactile Obsidian Ember Keypad Grid
    grid = _get_grid(scientific, False)
    rows = len(grid)
    cols = len(grid[0]) if grid else 1
    grid_y0 = disp_y1 + (8 if IS_LARGE else (3 if IS_WIDE else (5 if IS_240 else 3)))
    gap_x = 5 if IS_LARGE else (3 if (IS_WIDE or IS_240) else 2)
    gap_y = 5 if IS_LARGE else (2 if IS_WIDE else (3 if IS_240 else 1))
    avail_w = (calc_x1 - calc_x0) - gap_x * (cols - 1)
    avail_h = max(rows * 8, (y1 - grid_y0) - gap_y * (rows - 1))
    key_w = max(8, avail_w // cols)
    key_h = max(8, avail_h // rows)
    key_rad = 6 if IS_LARGE else (3 if (IS_WIDE or IS_240) else 2)

    for r, row in enumerate(grid):
        for c, label in enumerate(row):
            kx0 = calc_x0 + c * (key_w + gap_x)
            ky0 = grid_y0 + r * (key_h + gap_y)
            kx1 = calc_x1 if c == cols - 1 else (kx0 + key_w)
            ky1 = min(y1, y1 if r == rows - 1 else (ky0 + key_h))
            if ky1 <= ky0 + 2:
                continue
            is_sel = (r == sel_row and c == sel_col)
            btype = _btn_type(label)

            if btype == "equal":
                bg = ET["accent_bright"] if is_sel else ET["accent"]
                fg = ET["text_on_accent"]
                out = ET["text_bright"] if is_sel else ET["accent_bright"]
            elif btype == "operator":
                bg = ET["accent"] if is_sel else ET["select_bg"]
                fg = ET["text_on_accent"] if is_sel else ET["accent_bright"]
                out = ET["accent_bright"] if is_sel else ET["border_accent"]
            elif btype == "clear":
                bg = ET["danger"] if is_sel else ET["danger_bg"]
                fg = ET["text_bright"] if is_sel else ET["danger"]
                out = ET["text_bright"] if is_sel else ET["border_subtle"]
            elif btype == "function":
                bg = ET["select_bg"] if is_sel else ET["bg_surface"]
                fg = ET["accent_bright"] if is_sel else ET["text_muted"]
                out = ET["accent_bright"] if is_sel else ET["border_subtle"]
            else:
                bg = ET["select_bg"] if is_sel else ET["bg_elevated"]
                fg = ET["accent_bright"] if is_sel else ET["text_bright"]
                out = ET["accent_bright"] if is_sel else ET["border_subtle"]

            rj_theme.draw_card(
                d, (kx0, ky0, kx1, ky1),
                fill=bg, outline=out, radius=key_rad, width=2 if (is_sel and IS_LARGE) else 1,
            )
            k_font = FONTS["badge"] if (len(label) > 2 or (ky1 - ky0) <= 12) else FONT_KEY
            d.text(((kx0 + kx1) // 2, (ky0 + ky1) // 2), label, font=k_font, fill=fg, anchor="mm")

    # 3. Widescreen Right Pane: Live History Tape + Base Preview
    if split:
        px0 = int(WIDTH * 0.62) + 6
        px1 = WIDTH - pad
        base_h = 92
        tape_y1 = y1 - base_h - 6

        # History Tape Card
        rj_theme.draw_card(
            d, (px0, y0, px1, tape_y1),
            fill=ET["bg_surface"], outline=ET["border_subtle"],
            radius=8, accent_left=ET["accent_dim"], accent_width=3,
        )
        d.text((px0 + 12, y0 + 10), "HISTORY TAPE", font=FONTS["badge"], fill=ET["accent_bright"])
        d.line([(px0 + 8, y0 + 26), (px1 - 8, y0 + 26)], fill=ET["border_subtle"], width=1)

        if not history:
            d.text(((px0 + px1) // 2, (y0 + 26 + tape_y1) // 2), "Tape empty", font=FONTS["sub"], fill=ET["text_dim"], anchor="mm")
        else:
            iw = (px1 - px0) - 22
            ty = y0 + 32
            for expr_s, res_s in reversed(history[-6:]):
                if ty + 28 > tape_y1 - 4:
                    break
                d.text((px0 + 12, ty), rj_theme.truncate_text(d, expr_s, iw, FONTS["sub"]), font=FONTS["sub"], fill=ET["text_muted"])
                rb = rj_theme.measure_text(FONTS["mono"], f"= {res_s}", d)
                d.text((px1 - 10 - (rb[2] - rb[0]), ty + 12), rj_theme.truncate_text(d, f"= {res_s}", iw, FONTS["mono"]), font=FONTS["mono"], fill=ET["accent_bright"])
                ty += 31

        # Programmer Base Preview Card
        by0 = tape_y1 + 6
        rj_theme.draw_card(d, (px0, by0, px1, y1), fill=ET["bg_surface"], outline=ET["border_subtle"], radius=8)
        d.text((px0 + 12, by0 + 8), "PROGRAMMER BASES", font=FONTS["badge"], fill=ET["text_muted"])
        src_val = result or (expression if expression.lstrip("-").isdigit() else "")
        iy = by0 + 24
        for base in _BASES:
            conv = _convert_base(src_val, base) if src_val else "-"
            d.text((px0 + 12, iy), base, font=FONTS["badge"], fill=ET["accent"])
            d.text((px0 + 48, iy - 1), rj_theme.truncate_text(d, conv, (px1 - px0) - 58, FONTS["mono"]), font=FONTS["mono"], fill=ET["text_primary"])
            iy += 16

    LCD.LCD_ShowImage(img, 0, 0)


# ---------------------------------------------------------------------------
# Input handling (preserved 100%)
# ---------------------------------------------------------------------------
def _handle_btn_press(label, expression, result, error, history):
    new_expr = expression
    new_result = result
    new_error = error
    new_history = list(history)

    if label == "C":
        return ("", "", None, new_history)

    if label == "CE":
        stripped = new_expr.rstrip()
        if stripped:
            i = len(stripped) - 1
            while i > 0 and stripped[i - 1] not in " +-×÷^()":
                i -= 1
            new_expr = stripped[:i]
        return (new_expr, new_result, None, new_history)

    if label == "←":
        if new_expr:
            new_expr = new_expr[:-1]
        return (new_expr, new_result, None, new_history)

    if label == "=":
        res, err = _safe_eval(new_expr)
        if err:
            return (new_expr, "", err, new_history)
        if res:
            new_history = list(new_history)
            new_history.append((new_expr, res))
            if len(new_history) > 10:
                new_history = new_history[-10:]
        return (new_expr, res, None, new_history)

    if label == "±":
        if new_expr and new_expr[0] == "-":
            new_expr = new_expr[1:]
        elif new_expr:
            new_expr = "-" + new_expr
        else:
            new_expr = "-"
        return (new_expr, new_result, new_error, new_history)

    if label == "√":
        return (new_expr + "sqrt(", new_result, None, new_history)

    if label == "^":
        return (new_expr + "**", new_result, None, new_history)

    if label == "π":
        return (new_expr + "pi", new_result, None, new_history)

    if label == "H":
        return (new_expr, new_result, new_error, new_history)

    if label in ("sin", "cos", "tan", "ln", "log"):
        return (new_expr + label + "(", new_result, None, new_history)

    if label == "C₂":
        return (new_expr + "C", new_result, None, new_history)

    return (new_expr + label, new_result, None, new_history)


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------
_running = True


def _cleanup(*_args):
    global _running
    _running = False


signal.signal(signal.SIGINT, _cleanup)
signal.signal(signal.SIGTERM, _cleanup)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    expression = ""
    result = ""
    error = None
    history = []
    sel_row = 0
    sel_col = 0
    scientific = False
    show_history = False
    show_conversion = False
    conv_base_idx = 0
    dirty = True

    try:
        with text_input_mode():
            flush_input()
            while _running:
                grid = _get_grid(scientific, False)
                num_rows = len(grid)
                num_cols = len(grid[0]) if grid else 1
                sel_row = min(sel_row, num_rows - 1)
                sel_col = min(sel_col, num_cols - 1)

                if dirty:
                    _render_calculator(
                        expression, result, error, history, scientific,
                        sel_row, sel_col, show_history, show_conversion, conv_base_idx,
                    )
                    dirty = False

                act, ch = poll_ui_input(PINS, GPIO)
                if act is None and ch is None:
                    time.sleep(0.03)
                    continue

                dirty = True
                kl = ch.lower() if isinstance(ch, str) else ""

                # ----- Overlays -----
                if show_history:
                    if is_dismiss(act):
                        show_history = False
                    continue

                if show_conversion:
                    if is_back(act):
                        show_conversion = False
                    elif act == "UP":
                        conv_base_idx = (conv_base_idx - 1) % len(_BASES)
                    elif act == "DOWN":
                        conv_base_idx = (conv_base_idx + 1) % len(_BASES)
                    elif is_confirm(act):
                        if result:
                            converted = _convert_base(result, _BASES[conv_base_idx])
                            if converted != "N/A":
                                expression = converted
                                result = ""
                                error = None
                        show_conversion = False
                    continue

                # ----- Exit (keep Backspace for deleting calculator digits!) -----
                if is_back(act, allow_backspace=False):
                    break

                # ----- Mode toggles -----
                if act in ("KEY1", "TAB"):
                    scientific = not scientific
                    sel_row = sel_col = 0
                    continue
                if is_action(act, ch, "KEY2", "b"):
                    show_conversion = True
                    continue
                if kl == "h":
                    show_history = True
                    continue

                # ----- Direct physical keyboard entry -----
                if act == "BACKSPACE":
                    expression, result, error, history = _handle_btn_press("←", expression, result, error, history)
                    pos = _find_key_in_grid(grid, "←")
                    if pos:
                        sel_row, sel_col = pos
                    continue
                if act == "DELETE" or kl == "c":
                    expression, result, error, history = _handle_btn_press("C", expression, result, error, history)
                    pos = _find_key_in_grid(grid, "C")
                    if pos:
                        sel_row, sel_col = pos
                    continue
                if act == "ENTER" or ch == "=":
                    expression, result, error, history = _handle_btn_press("=", expression, result, error, history)
                    pos = _find_key_in_grid(grid, "=")
                    if pos:
                        sel_row, sel_col = pos
                    continue
                if ch in ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", ".", "+", "-", "(", ")", "^"):
                    expression, result, error, history = _handle_btn_press(ch, expression, result, error, history)
                    pos = _find_key_in_grid(grid, ch)
                    if pos:
                        sel_row, sel_col = pos
                    continue
                if ch == "*":
                    expression, result, error, history = _handle_btn_press("×", expression, result, error, history)
                    pos = _find_key_in_grid(grid, "×")
                    if pos:
                        sel_row, sel_col = pos
                    continue
                if ch == "/":
                    expression, result, error, history = _handle_btn_press("÷", expression, result, error, history)
                    pos = _find_key_in_grid(grid, "÷")
                    if pos:
                        sel_row, sel_col = pos
                    continue

                # ----- Grid navigation -----
                if act == "UP":
                    sel_row = (sel_row - 1) % num_rows
                elif act == "DOWN":
                    sel_row = (sel_row + 1) % num_rows
                elif act == "LEFT":
                    sel_col = (sel_col - 1) % num_cols
                elif act == "RIGHT":
                    sel_col = (sel_col + 1) % num_cols
                elif act == "OK" or ch == " ":
                    label = grid[sel_row][sel_col]
                    if label == "H":
                        show_history = True
                    else:
                        expression, result, error, history = _handle_btn_press(
                            label, expression, result, error, history,
                        )
    finally:
        try:
            LCD.LCD_Clear()
        except Exception:
            pass
        try:
            GPIO.cleanup()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
