#!/usr/bin/env python3
"""Unit and integration tests for Obsidian Ember theme, PTY terminal, HUD, and keyboard/mouse input."""

import os
import pathlib
import sys
import time
import unittest

from PIL import Image, ImageDraw

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["RJ_HDMI_NO_AUTOHOST"] = "1"
os.environ["RJ_NO_EVDEV_AUTOSTART"] = "1"
os.environ["RJ_DISPLAY_TYPE"] = "HDMI"
os.environ["RJ_HDMI_RES"] = "640x360"
os.environ["RJ_BASE_DIR"] = str(ROOT)
os.environ["RJ_ALLOW_NONROOT"] = "1"
os.environ["RJ_FAST_BOOT"] = "1"


class TestObsidianEmberTheme(unittest.TestCase):
    def test_wcag_aa_contrast_ratios(self):
        import rj_theme

        t = rj_theme.EMBER_THEME
        for bg in (t["bg_base"], t["bg_surface"], t["bg_well"]):
            self.assertGreaterEqual(rj_theme.contrast_ratio(t["text_primary"], bg), 4.5)
            self.assertGreaterEqual(rj_theme.contrast_ratio(t["text_bright"], bg), 7.0)
            self.assertGreaterEqual(rj_theme.contrast_ratio(t["accent"], bg), 4.5)
            self.assertGreaterEqual(rj_theme.contrast_ratio(t["accent_bright"], bg), 4.5)
            self.assertGreaterEqual(rj_theme.contrast_ratio(t["ok"], bg), 4.5)
            self.assertGreaterEqual(rj_theme.contrast_ratio(t["warn"], bg), 4.5)

        self.assertGreaterEqual(rj_theme.contrast_ratio(t["text_on_accent"], t["accent"]), 4.5)
        self.assertGreaterEqual(rj_theme.contrast_ratio(t["text_on_accent"], t["accent_bright"]), 4.5)
        self.assertGreaterEqual(rj_theme.contrast_ratio(t["accent_bright"], t["select_bg"]), 4.5)
        self.assertGreaterEqual(rj_theme.contrast_ratio(t["text_bright"], t["select_bg"]), 7.0)

    def test_theme_presets_and_legacy_migration(self):
        import rj_theme

        required_keys = {
            "BACKGROUND", "BORDER", "TEXT", "SELECTED_TEXT",
            "SELECTED_TEXT_BACKGROUND", "GAMEPAD", "GAMEPAD_FILL",
        }
        for name in ("Obsidian Ember", "Deep Amber", "Carbon Copper", "Classic Green"):
            self.assertIn(name, rj_theme.THEME_PRESETS)
            self.assertEqual(set(rj_theme.THEME_PRESETS[name].keys()), required_keys)

        self.assertTrue(rj_theme.should_migrate_legacy_colors(dict(rj_theme.LEGACY_NEON_DEFAULTS)))
        custom = dict(rj_theme.DEFAULT_GUI_COLORS)
        custom["BORDER"] = "#123456"
        self.assertFalse(rj_theme.should_migrate_legacy_colors(custom))

    def test_pil_primitives_and_scaled_draw(self):
        import rj_theme
        from payloads._display_helper import ScaledDraw

        img = Image.new("RGB", (640, 360), rj_theme.EMBER_THEME["bg_base"])
        d = ImageDraw.Draw(img)
        rj_theme.draw_card(d, (10, 10, 200, 100), accent_left=rj_theme.EMBER_THEME["accent"])
        rj_theme.draw_progress_bar(d, (20, 40, 180, 50), 0.65, bg=rj_theme.EMBER_THEME["bg_well"])
        rj_theme.draw_scrollbar(d, 190, 14, 96, 5, 10, 40)
        rj_theme.draw_signal_bars(d, 20, 60, 12, 80)

        sd = ScaledDraw(img)
        sd.rounded_rectangle((4, 4, 124, 124), radius=4, outline=rj_theme.EMBER_THEME["border_accent"])
        self.assertEqual(img.size, (640, 360))


class TestAnsiTerminal(unittest.TestCase):
    def test_vt100_parser_sgr_cursor_and_scrollback(self):
        import rj_terminal

        buf = rj_terminal.AnsiScreenBuffer(rows=5, cols=30, max_scrollback=50)
        for i in range(8):
            buf.feed(f"line {i}\r\n".encode())
        self.assertGreaterEqual(len(buf.scrollback), 3)

        # Carriage return overwrite
        buf.feed(b"hello\rWORLD")
        vis, _, _, _, _ = buf.get_visible_rows()
        self.assertTrue(any("".join(c[0] for c in row).startswith("WORLD") for row in vis))

        # SGR 16-color, 256-color, 24-bit color & OSC title
        buf.feed(b"\x1b]0;EmberShell\x07\x1b[2J\x1b[1;1H\x1b[38;2;242;153;74mRGB\x1b[0m \x1b[38;5;208mX256\x1b[0m")
        self.assertEqual(buf.title, "EmberShell")
        vis2, _, _, _, _ = buf.get_visible_rows()
        row0 = vis2[0]
        self.assertEqual(row0[0][0], "R")
        self.assertEqual(row0[0][1], (242, 153, 74))
        self.assertEqual("".join(c[0] for c in row0[:8]), "RGB X256")

        # Alternate screen switch preserves main screen
        buf.feed(b"\x1b[?1049hALT_CONTENT\x1b[?1049l")
        vis3, _, _, _, _ = buf.get_visible_rows()
        self.assertEqual("".join(c[0] for c in vis3[0][:8]), "RGB X256")

    def test_keyboard_to_pty_encoding(self):
        import rj_terminal

        self.assertEqual(rj_terminal.encode_text_event_to_pty({"special": "ENTER"}), b"\r")
        self.assertEqual(rj_terminal.encode_text_event_to_pty({"special": "BACKSPACE"}), b"\x7f")
        self.assertEqual(rj_terminal.encode_text_event_to_pty({"special": "UP"}), b"\x1b[A")
        self.assertEqual(rj_terminal.encode_text_event_to_pty({"special": "CTRL_C"}), b"\x03")
        self.assertEqual(rj_terminal.encode_text_event_to_pty({"special": "CTRL_SPACE"}), b"\x00")
        self.assertEqual(rj_terminal.encode_text_event_to_pty({"special": "INSERT"}), b"\x1b[2~")
        self.assertEqual(rj_terminal.encode_text_event_to_pty({"special": "F5"}), b"\x1b[15~")
        self.assertEqual(rj_terminal.encode_text_event_to_pty({"key": "ls -la"}), b"ls -la")

    def test_real_pty_session_and_rendering(self):
        import rj_terminal

        sess = rj_terminal.PersistentTerminalSession()
        try:
            screen = sess.ensure_started(rows=14, cols=60, cwd=str(ROOT))
            self.assertTrue(sess.is_alive())
            sess.write(b"echo RJ_PTY_ROUNDTRIP_OK\n")
            deadline = time.time() + 3.0
            found = False
            while time.time() < deadline:
                vis, _, _, _, _ = screen.get_visible_rows()
                lines = ["".join(c[0] for c in row) for row in vis]
                if any("RJ_PTY_ROUNDTRIP_OK" in ln for ln in lines):
                    found = True
                    break
                time.sleep(0.05)
            self.assertTrue(found, "Expected echo output from live PTY session")

            for w, h in ((640, 360), (320, 170), (128, 128)):
                frame = rj_terminal.render_terminal_frame(
                    screen, w, h, cursor_blink_on=True,
                    quick_menu_open=(w == 640), quick_index=1,
                    pid=sess.pid,
                )
                self.assertEqual(frame.size, (w, h))
                self.assertEqual(frame.mode, "RGB")
        finally:
            sess.close()
            self.assertFalse(sess.is_alive())


class TestExtendedKeyboardAndHUD(unittest.TestCase):
    def test_extended_evdev_keys(self):
        import rj_keyboard

        self.assertEqual(rj_keyboard.translate_evdev_code(110, mod_mask=0), (None, "INSERT"))
        self.assertEqual(rj_keyboard.translate_evdev_code(62, mod_mask=0), (None, "F4"))
        self.assertEqual(rj_keyboard.translate_evdev_code(67, mod_mask=0), (None, "F9"))
        self.assertEqual(rj_keyboard.translate_evdev_code(57, mod_mask=rj_keyboard.MOD_LCTRL), (None, "CTRL_SPACE"))
        self.assertEqual(rj_keyboard.translate_evdev_code(26, mod_mask=rj_keyboard.MOD_LCTRL), (None, "CTRL_LBRACKET"))

    def test_hdmi_hud_ember_theme_and_dimensions(self):
        import hdmi_host
        import rj_theme

        src = Image.new("RGB", (640, 360), rj_theme.EMBER_RGB["bg_base"])
        nav_bytes, nw, nh = hdmi_host.compose_hud_overlay(src.tobytes(), 640, 360, text_mode=False)
        txt_bytes, tw, th = hdmi_host.compose_hud_overlay(src.tobytes(), 640, 360, text_mode=True)
        self.assertEqual(nw, 640)
        self.assertEqual(tw, 640)
        self.assertGreater(nh, 360)
        self.assertEqual(nh, th)

        nav_img = Image.frombytes("RGB", (nw, nh), nav_bytes)
        bg_px = nav_img.getpixel((nw - 2, nh - 2))
        self.assertEqual(bg_px, rj_theme.EMBER_RGB["bg_overlay"])


class TestRaspyJackCoreUIRender(unittest.TestCase):
    def test_main_menu_terminal_and_views_render_headless(self):
        import raspyjack

        main_labels = [entry[0] for entry in raspyjack.m.menu["a"]]
        self.assertIn(" Terminal", main_labels)
        color_labels = [entry[0] for entry in raspyjack.m.menu["aea"]]
        self.assertIn(" Theme Presets", color_labels)
        preset_labels = [entry[0] for entry in raspyjack.m.menu["aeap"]]
        self.assertIn(" Obsidian Ember", preset_labels)

        self.assertEqual(raspyjack.color.background.upper(), "#0E0E10")

        raspyjack.RenderMenuWindowOnce(raspyjack.m.GetMenuList(), 0)
        self.assertEqual(raspyjack.image.size, (640, 360))
        raspyjack.RenderMenuGridOnce(raspyjack.m.GetMenuList(), 2)
        raspyjack.RenderMenuCarouselOnce(raspyjack.m.GetMenuList(), 1)
        raspyjack._render_color_picker([224, 122, 47], 0)
        raspyjack._draw_lock_screen("Enter PIN", "1234", "Verify")
        raspyjack.Dialog_info("Obsidian Ember\nUI Ready", wait=False, timeout=0)


class TestTerminalInputEndToEnd(unittest.TestCase):
    def setUp(self):
        import tempfile
        import rj_keyboard
        import rj_input
        import rj_terminal

        self.tmp = tempfile.TemporaryDirectory()
        rj_keyboard.close_mappings()
        self._old_kbd_shm = rj_keyboard.SHM_PATH
        self._old_kbd_sock = rj_keyboard.INPUT_SOCK_PATH
        self._old_env_sock = os.environ.get("RJ_INPUT_SOCK")
        self._old_in_sock = rj_input._SOCK_PATH
        self._old_ts_file = rj_terminal._TEXT_SESSION_FILE

        rj_keyboard.SHM_PATH = os.path.join(self.tmp.name, "term_kbd.shm")
        sock_path = os.path.join(self.tmp.name, "term_input.sock")
        os.environ["RJ_INPUT_SOCK"] = sock_path
        rj_keyboard.INPUT_SOCK_PATH = sock_path
        rj_input._SOCK_PATH = sock_path
        rj_terminal._TEXT_SESSION_FILE = os.path.join(self.tmp.name, "term_session.json")
        rj_input.restart_listener()
        rj_input.flush()

    def tearDown(self):
        import rj_keyboard
        import rj_input
        import rj_terminal

        rj_keyboard.set_text_mode(False)
        rj_keyboard.clear_all_keys()
        rj_keyboard.close_mappings()
        rj_keyboard.SHM_PATH = self._old_kbd_shm
        rj_keyboard.INPUT_SOCK_PATH = self._old_kbd_sock
        if self._old_env_sock is None:
            os.environ.pop("RJ_INPUT_SOCK", None)
        else:
            os.environ["RJ_INPUT_SOCK"] = self._old_env_sock
        rj_input._cleanup()
        rj_input._SOCK_PATH = self._old_in_sock
        rj_terminal._TEXT_SESSION_FILE = self._old_ts_file
        self.tmp.cleanup()

    def test_shm_ring_delivers_text_when_socket_unavailable_and_dedups_when_live(self):
        import rj_keyboard
        import rj_input

        # 1. With BOTH socket and SHM ring active, each key arrives EXACTLY ONCE
        with rj_keyboard.text_input_mode():
            rj_keyboard.handle_key_event(30, pressed=True)   # 'a'
            rj_keyboard.handle_key_event(30, pressed=False)
            rj_keyboard.handle_key_event(48, pressed=True)   # 'b'
            rj_keyboard.handle_key_event(48, pressed=False)
        time.sleep(0.05)
        got = []
        while True:
            ev = rj_input.get_text_event()
            if not ev:
                break
            got.append(ev.get("key") or ev.get("special"))
        self.assertEqual(got, ["a", "b"])

        # 2. Tear down the Unix socket completely — SHM ring MUST still deliver every key!
        rj_input._cleanup()
        self.assertFalse(os.path.exists(rj_input._SOCK_PATH))
        with rj_keyboard.text_input_mode():
            for code in (38, 31, 28):  # 'l', 's', ENTER
                rj_keyboard.handle_key_event(code, pressed=True)
                rj_keyboard.handle_key_event(code, pressed=False)
        got2 = []
        while True:
            ev = rj_input.get_text_event()
            if not ev:
                break
            got2.append(ev.get("key") or ev.get("special"))
        self.assertEqual(got2, ["l", "s", "ENTER"])

    def test_sdl2_keydown_without_textinput_falls_back_and_dedups_when_present(self):
        import struct
        import hdmi_host
        import rj_input

        # Construct lightweight _SDL2Backend stub without needing a physical display
        be = hdmi_host._SDL2Backend.__new__(hdmi_host._SDL2Backend)
        be.kb_cfg = {}
        be.sdl = None
        be.show_hud = False
        be.last_raw = None
        be.last_text_mode = False
        be._last_fallback_char = ("", 0.0)
        be.running = True

        def _make_keydown(scancode: int, sdl_mod: int = 0) -> bytes:
            buf = bytearray(64)
            struct.pack_into("<I", buf, 0, be.SDL_KEYDOWN)
            buf[12] = 1  # SDL_PRESSED
            struct.pack_into("<iiH", buf, 16, scancode, 0, sdl_mod)
            return bytes(buf)

        def _make_textinput(text: str) -> bytes:
            buf = bytearray(64)
            struct.pack_into("<I", buf, 0, be.SDL_TEXTINPUT)
            raw = text.encode("utf-8")[:31]
            buf[12 : 12 + len(raw)] = raw
            return bytes(buf)

        # Case A: KMSDRM/Wayland where SDL_TEXTINPUT never fires -> must fall back to scancode ('a', '!')
        be._process_sdl_event_batch([_make_keydown(4, 0)])         # SDL scancode 4 -> 'a'
        be._process_sdl_event_batch([_make_keydown(30, 0x0001)])   # SDL scancode 30 + LShift -> '!'

        # Case B: Desktop where SDL_TEXTINPUT fires in same batch -> must emit OS char once (no duplicate)
        be._process_sdl_event_batch([_make_keydown(20, 0), _make_textinput("q")])

        # Case C: Delayed async-IME SDL_TEXTINPUT right after fallback -> deduplicated
        be._process_sdl_event_batch([_make_keydown(5, 0)])         # 'b' fallback
        be._process_sdl_event_batch([_make_textinput("b")])        # delayed dup ignored

        time.sleep(0.05)
        chars = []
        while True:
            ev = rj_input.get_text_event()
            if not ev:
                break
            chars.append(ev.get("key") or ev.get("special"))
        self.assertEqual(chars, ["a", "!", "q", "b"])

    def test_terminal_ui_accepts_typed_command_remote_session_and_ctrl_c(self):
        import json
        import threading
        import rj_keyboard
        import rj_terminal

        class _DummyLCD:
            width = 640
            height = 360

            def __init__(self):
                self.frames = 0

            def LCD_ShowImage(self, img, x, y):
                self.frames += 1

        lcd = _DummyLCD()
        sess = rj_terminal.PersistentTerminalSession()
        seen_session_active = []

        def _driver():
            # Wait for terminal UI to boot & open remote text session
            deadline = time.time() + 3.0
            while time.time() < deadline:
                if os.path.isfile(rj_terminal._TEXT_SESSION_FILE):
                    try:
                        with open(rj_terminal._TEXT_SESSION_FILE, "r", encoding="utf-8") as h:
                            st = json.load(h)
                        if st.get("active"):
                            seen_session_active.append(st)
                            break
                    except Exception:
                        pass
                time.sleep(0.03)
            time.sleep(0.25)

            # Type `sleep 30` + Enter, interrupt it via Ctrl+C (proves TIOCSCTTY controlling TTY!),
            # then run `echo TERM_KB_FIXED_OK` + Enter and detach via Ctrl+Q.
            for ch in "sleep 30":
                rj_keyboard.send_text_event(char=ch)
                time.sleep(0.008)
            rj_keyboard.send_text_event(special="ENTER")
            time.sleep(0.25)
            rj_keyboard.send_text_event(special="CTRL_C", mod_mask=rj_keyboard.MOD_LCTRL)
            time.sleep(0.25)

            for ch in "echo TERM_KB_FIXED_OK":
                rj_keyboard.send_text_event(char=ch)
                time.sleep(0.008)
            rj_keyboard.send_text_event(special="ENTER")

            # Wait until output appears on AnsiScreenBuffer
            out_deadline = time.time() + 4.0
            while time.time() < out_deadline:
                if sess.screen and any("TERM_KB_FIXED_OK" in ln for ln in sess.screen.get_plain_lines()):
                    break
                time.sleep(0.05)

            rj_keyboard.send_text_event(special="CTRL_Q", mod_mask=rj_keyboard.MOD_LCTRL)

        t = threading.Thread(target=_driver, daemon=True)
        t.start()
        try:
            rc = rj_terminal.run_terminal_ui(lcd, session=sess, persistent=True, cwd=str(ROOT))
            t.join(timeout=6.0)
            self.assertEqual(rc, 0)
            self.assertGreater(lcd.frames, 0)
            self.assertTrue(seen_session_active, "Terminal should open remote text session for WebUI")
            self.assertEqual(seen_session_active[0].get("charset"), "terminal")
            lines = sess.screen.get_plain_lines()
            self.assertTrue(
                any("TERM_KB_FIXED_OK" in ln for ln in lines),
                f"Expected typed command output in terminal screen, got: {lines}",
            )
            # Remote text session must be closed cleanly on detach
            with open(rj_terminal._TEXT_SESSION_FILE, "r", encoding="utf-8") as h:
                closed_st = json.load(h)
            self.assertFalse(closed_st.get("active"))
        finally:
            sess.close()


class TestKeyboardMouseAndKioskExit(unittest.TestCase):
    def test_virtual_mouse_toggle_motion_clicks_and_force_exit(self):
        import types
        import rj_kbd_mouse as km
        import evdev_keys
        import rj_keyboard

        # Virtual mouse must NEVER be rediscovered/grabbed by evdev_keys!
        fake_vmouse = types.SimpleNamespace(
            name="EmberJack Virtual Mouse",
            info=types.SimpleNamespace(vendor=0x1209, product=0x524A),
            capabilities=lambda: {1: [km.BTN_LEFT, km.BTN_RIGHT]},
        )
        self.assertFalse(evdev_keys.is_real_keyboard_device(fake_vmouse))

        # Ctrl+Alt+M works in ANY key-press order (e.g. Ctrl -> M -> Alt) AND recovers from a missed key-up!
        st_ord = km.VirtualMouseState()
        st_ord.process_key_event(km.KEY_LCTRL, 1, now=10.0)
        st_ord.process_key_event(km.KEY_M, 1, now=10.01)
        st_ord.process_key_event(km.KEY_LALT, 1, now=10.02)
        self.assertEqual(st_ord.pending_mode, km.MODE_DESKTOP)
        st_ord.process_key_event(km.KEY_M, 0, now=10.05)
        st_ord.process_key_event(km.KEY_LCTRL, 0, now=10.06)
        self.assertEqual(st_ord.mode, km.MODE_TYPING)  # still waiting on LAlt
        a_sync = st_ord.sync_active_keys(set())
        self.assertEqual(a_sync["mode_changed"], km.MODE_DESKTOP)
        self.assertIs(a_sync["grab_changed"], True)
        self.assertTrue(st_ord.mouse_mode)

        # Timeout fallback prevents infinite pending_mode deadlock even without active_keys()
        st_ord.process_key_event(km.KEY_M, 1, now=20.0)
        st_ord.process_key_event(km.KEY_LALT, 1, now=20.01)
        st_ord.process_key_event(km.KEY_LCTRL, 1, now=20.02)
        self.assertEqual(st_ord.pending_mode, km.MODE_TYPING)
        a_to = st_ord.flush_pending_if_timed_out(20.75, timeout=0.65)
        self.assertEqual(a_to["mode_changed"], km.MODE_TYPING)
        self.assertIs(a_to["grab_changed"], False)
        self.assertEqual(st_ord.mode, km.MODE_TYPING)

        diag = km.diagnose_environment()
        self.assertIn("uinput_writable", diag)
        self.assertIn("keyboard_paths", diag)
        self.assertIn("ready", diag)

        # 1. Always starts in TYPING mode (never auto-grabs on launch!)
        st = km.VirtualMouseState()
        self.assertEqual(st.mode, km.MODE_TYPING)
        self.assertFalse(st.mouse_mode)

        # Normal typing while Typing Mode is ON produces NO mouse actions
        act = st.process_key_event(km.KEY_W, 1)
        self.assertEqual(act["rel_nudge"], (0, 0))
        self.assertFalse(act["force_exit"])
        st.process_key_event(km.KEY_W, 0)

        # Ctrl+Alt+M arms toggle on press, commits ONLY after chord keys release (no stuck modifiers!)
        st.process_key_event(km.KEY_LCTRL, 1)
        st.process_key_event(km.KEY_LALT, 1)
        a_down = st.process_key_event(km.KEY_M, 1)
        self.assertIsNone(a_down["mode_changed"])
        self.assertFalse(st.mouse_mode)
        st.process_key_event(km.KEY_M, 0)
        st.process_key_event(km.KEY_LALT, 0)
        a_up = st.process_key_event(km.KEY_LCTRL, 0)
        self.assertEqual(a_up["mode_changed"], km.MODE_DESKTOP)
        self.assertIs(a_up["grab_changed"], True)
        self.assertTrue(st.mouse_mode)

        # Mouse movement (Arrows + Shift slow + live speed 1..10)
        self.assertEqual(st.mouse_speed, km.DEFAULT_MOUSE_SPEED)
        a_mv = st.process_key_event(km.KEY_RIGHT, 1)
        self.assertGreater(a_mv["rel_nudge"][0], 0)
        dx_default, dy1, _ = st.compute_motion_step(0.05)
        self.assertGreater(dx_default, 0)
        self.assertEqual(dy1, 0)
        a_dn = st.process_key_event(km.KEY_MINUS, 1)
        self.assertEqual(a_dn["speed_changed"], km.DEFAULT_MOUSE_SPEED - 1)
        st.process_key_event(km.KEY_MINUS, 0)
        st.set_speed(1)
        dx_min, _, _ = st.compute_motion_step(0.05)
        st.set_speed(9)
        dx_fast, _, _ = st.compute_motion_step(0.05)
        self.assertLess(dx_min, dx_fast)
        st.set_speed(km.DEFAULT_MOUSE_SPEED)
        st.process_key_event(km.KEY_LSHIFT, 1)
        dx_slow, _, _ = st.compute_motion_step(0.05)
        self.assertLess(dx_slow, dx_fast)
        st.process_key_event(km.KEY_LSHIFT, 0)
        st.process_key_event(km.KEY_RIGHT, 0)

        # Unified nav maps: Backspace -> KEY3_PIN, Tab -> KEY2_PIN in BOTH standard and tca8418
        self.assertEqual(rj_keyboard.STANDARD_NAV_MAP.get(14), "KEY3_PIN")
        self.assertEqual(rj_keyboard.TCA8418_NAV_MAP.get(14), "KEY3_PIN")
        self.assertEqual(rj_keyboard.TCA8418_NAV_MAP.get(15), "KEY2_PIN")
        self.assertNotIn(14, rj_keyboard.TEXT_MODE_NAV_CODES)

        # Mouse clicks & scroll: Space/Enter=Left, Backspace=Right, Tab=Middle, PgUp=Scroll
        a_lc = st.process_key_event(km.KEY_SPACE, 1)
        self.assertIn((km.BTN_LEFT, 1), a_lc["buttons"])
        a_rc = st.process_key_event(km.KEY_BACKSPACE, 1)
        self.assertIn((km.BTN_RIGHT, 1), a_rc["buttons"])
        a_mc = st.process_key_event(km.KEY_TAB, 1)
        self.assertIn((km.BTN_MIDDLE, 1), a_mc["buttons"])
        a_sc = st.process_key_event(km.KEY_PAGEUP, 1)
        self.assertEqual(a_sc["wheel_step"], 1)
        st.process_key_event(km.KEY_PAGEUP, 0)

        # Esc exits Mouse Mode back to Typing and releases all held buttons
        st.process_key_event(km.KEY_ESC, 1)
        a_exit_mm = st.process_key_event(km.KEY_ESC, 0)
        self.assertEqual(a_exit_mm["mode_changed"], km.MODE_TYPING)
        self.assertIs(a_exit_mm["grab_changed"], False)
        self.assertFalse(st.mouse_mode)
        self.assertIn((km.BTN_LEFT, 0), a_exit_mm["buttons"])
        self.assertIn((km.BTN_RIGHT, 0), a_exit_mm["buttons"])
        self.assertIn((km.BTN_MIDDLE, 0), a_exit_mm["buttons"])

        # ScrollLock toggles Mouse Mode on, and Universal Emergency Exit Ctrl+Alt+Q releases buttons
        st.process_key_event(km.KEY_SCROLLLOCK, 1)
        a_sl = st.process_key_event(km.KEY_SCROLLLOCK, 0)
        self.assertEqual(a_sl["mode_changed"], km.MODE_DESKTOP)
        st.process_key_event(km.KEY_SPACE, 1)
        st.process_key_event(km.KEY_LCTRL, 1)
        st.process_key_event(km.KEY_LALT, 1)
        a_quit = st.process_key_event(km.KEY_Q, 1)
        self.assertTrue(a_quit["force_exit"])
        self.assertTrue(st.force_exit_requested)
        self.assertIn((km.BTN_LEFT, 0), a_quit["buttons"])

if __name__ == "__main__":
    unittest.main()
