#!/usr/bin/env python3
"""Unit and integration tests for Obsidian Ember theme, integrated PTY terminal,
and reworked RaspyJack UIs."""

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


class TestBluetoothConnectionTool(unittest.TestCase):
    def test_bluetooth_parsing_classification_and_audio_sinks(self):
        import rj_bluetooth
        from payloads.utilities import bluetooth_manager as btm

        adapter_out = (
            "Controller AA:BB:CC:DD:EE:01 (public)\n"
            "\tName: raspyjack\n"
            "\tAlias: RaspyJack-BT\n"
            "\tPowered: yes\n"
            "\tDiscoverable: no\n"
            "\tPairable: yes\n"
        )
        ad = rj_bluetooth.parse_adapter_show(adapter_out)
        self.assertTrue(ad["available"])
        self.assertTrue(ad["powered"])
        self.assertEqual(ad["address"], "AA:BB:CC:DD:EE:01")

        hp_info = (
            "Device 11:22:33:44:55:66 (public)\n"
            "\tName: WH-1000XM5\n"
            "\tAlias: Sony WH-1000XM5\n"
            "\tIcon: audio-headphones\n"
            "\tPaired: yes\n"
            "\tTrusted: yes\n"
            "\tConnected: yes\n"
            "\tRSSI: 0xffffffca (-54)\n"
            "\tUUID: Audio Sink (0000110b-0000-1000-8000-00805f9b34fb)\n"
            "\tBattery Percentage: 0x55 (85)\n"
        )
        hp = rj_bluetooth.parse_device_info("11:22:33:44:55:66", hp_info)
        self.assertEqual(hp["name"], "Sony WH-1000XM5")
        self.assertEqual(hp["device_type"], "headphones")
        self.assertTrue(rj_bluetooth.is_audio_device(hp))
        self.assertEqual(hp["battery"], 85)
        self.assertEqual(hp["rssi"], -54)
        self.assertTrue(hp["connected"])

        kb_info = (
            "Device AA:11:22:33:44:55 (public)\n"
            "\tName: Keychron K3\n"
            "\tIcon: input-keyboard\n"
            "\tPaired: yes\n"
            "\tConnected: no\n"
        )
        kb = rj_bluetooth.parse_device_info("AA:11:22:33:44:55", kb_info)
        self.assertEqual(kb["device_type"], "keyboard")
        self.assertFalse(rj_bluetooth.is_audio_device(kb))

        sinks_out = (
            "42\talsa_output.platform-bcm2835.stereo\tPipeWire\ts16le 2ch 48000Hz\tIDLE\n"
            "57\tbluez_output.11_22_33_44_55_66.a2dp-sink\tPipeWire\ts16le 2ch 44100Hz\tRUNNING\n"
        )
        sinks = rj_bluetooth.parse_pactl_sinks(sinks_out, default_sink="bluez_output.11_22_33_44_55_66.a2dp-sink")
        self.assertEqual(len(sinks), 2)
        self.assertTrue(sinks[1]["is_bluetooth"])
        self.assertTrue(sinks[1]["is_default"])

        devs = [hp, kb]
        self.assertEqual(len(btm.filter_devices(devs, "audio")), 1)
        self.assertEqual(len(btm.filter_devices(devs, "paired")), 2)
        hp_actions = [a[1] for a in btm.build_device_actions(hp)]
        self.assertIn("set_audio", hp_actions)
        self.assertIn("test_chime", hp_actions)
        self.assertIn("disconnect", hp_actions)
        kb_actions = [a[1] for a in btm.build_device_actions(kb)]
        self.assertIn("connect", kb_actions)

        # Render across HDMI, Cardputer, and 128px resolutions
        for w, h in ((640, 360), (320, 170), (128, 128)):
            f1 = btm.render_bt_manager_frame(w, h, ad, devs, tab="all", cursor=0, audio_label="BT: Sony WH-1000XM5", volume_pct=75, toast="Connected! Audio -> Sony WH-1000XM5", toast_kind="ok")
            self.assertEqual(f1.size, (w, h))
            f2 = btm.render_bt_manager_frame(w, h, ad, devs, overlay={"kind": "scan", "title": "Scanning...", "sub": "Found 2", "progress": 0.6})
            self.assertEqual(f2.size, (w, h))
            f3 = btm.render_bt_manager_frame(w, h, ad, devs, overlay={"kind": "menu", "title": hp["name"], "sub": hp["mac"], "items": btm.build_device_actions(hp), "sel": 0})
            self.assertEqual(f3.size, (w, h))

        # Main menu exposes Bluetooth entry
        import raspyjack
        main_labels = [entry[0] for entry in raspyjack.m.menu["a"]]
        self.assertIn(" Bluetooth", main_labels)


class TestYouTubePlayerRework(unittest.TestCase):
    def test_youtube_url_parsing_storage_and_renderers(self):
        import tempfile
        from payloads.utilities import youtube as yt

        self.assertEqual(yt.parse_youtube_target("https://www.youtube.com/watch?v=dQw4w9WgXcQ"), ("video", "dQw4w9WgXcQ"))
        self.assertEqual(yt.parse_youtube_target("https://youtu.be/dQw4w9WgXcQ?t=42"), ("video", "dQw4w9WgXcQ"))
        self.assertEqual(yt.parse_youtube_target("https://youtube.com/shorts/abcDEF123_-"), ("video", "abcDEF123_-"))
        self.assertEqual(yt.parse_youtube_target("https://www.youtube.com/playlist?list=PL1234567890ABCDEF")[0], "playlist")
        self.assertEqual(yt.parse_youtube_target("lofi hip hop radio"), ("search", "lofi hip hop radio"))

        self.assertEqual(yt.format_duration(75), "1:15")
        self.assertEqual(yt.format_duration(3665), "1:01:05")
        self.assertEqual(yt.format_views(1_500_000), "1.5M views")
        self.assertEqual(yt.format_views(42_300), "42.3K views")

        # Verify A/V sync & performance guarantees: dual -re, fps BEFORE scale, thread_queue_size, H.264 & adaptive sizing
        q720 = dict(yt.STREAM_QUALITIES)["720p"]
        self.assertIn("avc1", q720)
        self.assertEqual(yt.compute_video_render_size("144p", 640, 360, "auto", is_hdmi=True), (320, 180))
        self.assertEqual(yt.compute_video_render_size("240p", 640, 360, "auto", is_hdmi=True), (320, 180))
        self.assertEqual(yt.compute_video_render_size("360p", 640, 360, "auto", is_hdmi=True), (640, 360))
        self.assertEqual(yt.compute_video_render_size("720p", 640, 360, "fast", is_hdmi=True), (320, 180))
        self.assertEqual(yt.compute_video_render_size("144p", 320, 170, "auto", is_hdmi=False), (320, 170))
        ff_cmd = yt.build_video_ffmpeg_cmd(
            "https://v.example/vid", "https://a.example/aud",
            640, 360, "rgb24", 24,
            ["-f", "pulse", "-buffer_duration", "60", "default"],
            start_offset=15.0,
            audio_delay_ms=100,
        )
        self.assertEqual(ff_cmd.count("-re"), 2, "Both video and audio inputs MUST have -re")
        self.assertEqual(ff_cmd.count("-thread_queue_size"), 2)
        self.assertIn("-reconnect", ff_cmd)
        vf_str = ff_cmd[ff_cmd.index("-vf") + 1]
        self.assertTrue(vf_str.startswith("fps=24,scale="), f"fps must run before scale: {vf_str}")
        self.assertIn("-fflags", ff_cmd)
        self.assertIn("make_zero", ff_cmd)
        self.assertIn("adelay=100|100,aresample=async=1:first_pts=0", ff_cmd)
        self.assertIn("-buffer_duration", ff_cmd)
        local_cmd = yt.build_video_ffmpeg_cmd("/tmp/local.mp4", "", 640, 360, "rgb24", 24, ["-f", "alsa", "plughw:0,0"])
        self.assertNotIn("-reconnect", local_cmd)

        # Isolated persistence & downloads check
        old_base = os.environ.get("RJ_BASE_DIR")
        with tempfile.TemporaryDirectory() as td:
            os.environ["RJ_BASE_DIR"] = td
            try:
                hist = yt.save_history_entry("lofi beats")
                self.assertEqual(hist[0], "lofi beats")
                sample = {"id": "dQw4w9WgXcQ", "title": "Synthwave Mix", "channel": "Ember Radio", "duration": 215, "views": 125000}
                self.assertTrue(yt.toggle_like_video(sample))
                self.assertTrue(yt.is_liked("dQw4w9WgXcQ"))
                self.assertFalse(yt.toggle_like_video(sample))
                dl_dir = yt.get_downloads_dir()
                os.makedirs(dl_dir, exist_ok=True)
                with open(os.path.join(dl_dir, "demo_track.mp3"), "wb") as h:
                    h.write(b"ID3" + b"\x00" * 1024)
                dls = yt.list_downloads()
                self.assertEqual(len(dls), 1)
                self.assertEqual(dls[0]["kind"], "audio")
            finally:
                if old_base is None:
                    os.environ.pop("RJ_BASE_DIR", None)
                else:
                    os.environ["RJ_BASE_DIR"] = old_base

        # Render all YouTube views across 640x360, 320x170, and 128x128
        sample_items = [
            {"id": "vid12345678", "title": "Obsidian Ember Synthwave Coding Mix 2026", "channel": "Ember FM", "duration": 1845, "views": 482000},
            {"id": "vid87654321", "title": "Raspberry Pi Linux Audio & Bluetooth Setup", "channel": "Pi Lab", "duration": 612, "views": 95400},
        ]
        thumb = Image.new("RGB", (320, 180), (45, 28, 18))
        for w, h in ((640, 360), (320, 170), (128, 128)):
            self.assertEqual(yt.render_home_menu(w, h, 0, audio_label="BT: Sony WH-1000XM5", quality_label="720p", liked_count=4, downloads_count=2).size, (w, h))
            self.assertEqual(yt.render_results_view(w, h, "RESULTS", "synthwave", sample_items, 0, 0, has_load_more=True, liked_ids={"vid12345678"}, thumb_img=thumb).size, (w, h))
            vid_frame = Image.new("RGB", (w, h), (22, 26, 32))
            osd = yt.render_video_osd(vid_frame, sample_items[0]["title"], 412.0, 1845, 75, quality_label="720p", audio_label="BT: Sony WH-1000XM5", liked=True, playlist_pos="1/2")
            self.assertEqual(osd.size, (w, h))
            music = yt.render_music_dashboard(w, h, sample_items[0], 0, 2, 412.0, 75, liked=True, shuffle=True, audio_label="BT: Sony WH-1000XM5", thumb_img=thumb, tick=1.5)
            self.assertEqual(music.size, (w, h))
            self.assertEqual(yt.render_status_card(w, h, "Downloading MP3...", "Synthwave Mix", progress=0.68).size, (w, h))


class TestGUIAppLauncher(unittest.TestCase):
    def test_desktop_parsing_filtering_kiosk_plans_and_rendering(self):
        import tempfile
        import rj_app_launcher as al
        from payloads.utilities import app_launcher as al_ui

        self.assertEqual(al.clean_exec_command("firefox --new-window %u"), ["firefox", "--new-window"])
        self.assertEqual(al.map_desktop_category("Network;WebBrowser;"), "web")
        self.assertEqual(al.map_desktop_category("AudioVideo;Player;"), "media")
        self.assertEqual(al.map_desktop_category("Game;Emulator;"), "games")

        old_base = os.environ.get("RJ_BASE_DIR")
        with tempfile.TemporaryDirectory() as td:
            os.environ["RJ_BASE_DIR"] = td
            d_dir = os.path.join(td, "desktops")
            os.makedirs(d_dir, exist_ok=True)
            try:
                # Valid GUI browser entry
                with open(os.path.join(d_dir, "firefox.desktop"), "w", encoding="utf-8") as h:
                    h.write("[Desktop Entry]\nType=Application\nName=Firefox\nComment=Browse the Web\nExec=firefox %u\nCategories=Network;WebBrowser;\n")
                # Valid media player entry
                with open(os.path.join(d_dir, "vlc.desktop"), "w", encoding="utf-8") as h:
                    h.write("[Desktop Entry]\nType=Application\nName=VLC Media Player\nComment=Play videos and music\nExec=vlc %U\nCategories=AudioVideo;Player;\n")
                # Excluded: desktop session manager (MUST NEVER appear!)
                with open(os.path.join(d_dir, "xfce.desktop"), "w", encoding="utf-8") as h:
                    h.write("[Desktop Entry]\nType=Application\nName=XFCE Desktop\nExec=xfce4-session\nCategories=System;\n")
                # Excluded: terminal-only & NoDisplay entries
                with open(os.path.join(d_dir, "htop.desktop"), "w", encoding="utf-8") as h:
                    h.write("[Desktop Entry]\nType=Application\nName=htop\nExec=htop\nTerminal=true\n")
                with open(os.path.join(d_dir, "hidden.desktop"), "w", encoding="utf-8") as h:
                    h.write("[Desktop Entry]\nType=Application\nName=Hidden\nExec=true\nNoDisplay=true\n")

                # Add a custom app & verify discovery + favorites + filtering
                al.add_custom_app("Wireshark", "wireshark", category="tools", comment="Packet analyzer")
                apps = al.discover_gui_apps(extra_desktop_dirs=[(d_dir, "SYS")], verify_exec=False)
                names = [a["name"] for a in apps]
                self.assertIn("Firefox", names)
                self.assertIn("VLC Media Player", names)
                self.assertIn("Wireshark", names)
                self.assertNotIn("XFCE Desktop", names)
                self.assertNotIn("htop", names)
                self.assertNotIn("Hidden", names)

                self.assertEqual(len(al.filter_apps(apps, category="web")), 1)
                self.assertEqual(len(al.filter_apps(apps, query="packet")), 1)
                self.assertTrue(al.toggle_favorite("firefox"))
                apps2 = al.discover_gui_apps(extra_desktop_dirs=[(d_dir, "SYS")], verify_exec=False)
                self.assertEqual(apps2[0]["id"], "firefox")

                # Launch plans: Direct Wayland vs Headless Cage Kiosk vs Missing Kiosk
                ff = apps2[0]
                cmd_w, env_w, ho_w, _ = al.build_launch_plan(ff, session_info={
                    "mode": "direct_wayland", "mode_label": "WAYLAND", "wayland_display": "wayland-0",
                    "x_display": "", "kiosk_tool": "", "user": "konrad", "uid": 1000, "gid": 1000,
                    "home": td, "runtime_dir": td,
                })
                self.assertEqual(cmd_w, ["firefox"])
                self.assertFalse(ho_w)
                self.assertEqual(env_w.get("WAYLAND_DISPLAY"), "wayland-0")

                cmd_c, env_c, ho_c, _ = al.build_launch_plan(ff, session_info={
                    "mode": "kiosk_cage", "mode_label": "CAGE KIOSK", "wayland_display": "",
                    "x_display": "", "kiosk_tool": "cage", "user": "konrad", "uid": os.geteuid(), "gid": os.getegid(),
                    "home": td, "runtime_dir": "",
                })
                self.assertEqual(cmd_c, ["cage", "-s", "--", "firefox"])
                self.assertTrue(ho_c)
                # Even when runtime_dir is empty (headless boot/sudo), XDG_RUNTIME_DIR MUST be set & exist!
                self.assertTrue(env_c.get("XDG_RUNTIME_DIR"))
                self.assertTrue(os.path.isdir(env_c["XDG_RUNTIME_DIR"]))
                # Kiosk launches MUST target a dedicated VT (e.g. VT7), NEVER VT1 where getty@tty1 login runs!
                self.assertEqual(env_c.get("XDG_VTNR"), str(al.DEFAULT_KIOSK_VT))
                self.assertNotEqual(env_c.get("XDG_VTNR"), "1")

                # xinit kiosk plan must explicitly pass vt7 and -keeptty
                cmd_x, env_x, ho_x, _ = al.build_launch_plan(ff, session_info={
                    "mode": "kiosk_xinit", "mode_label": "XINIT KIOSK", "wayland_display": "",
                    "x_display": "", "kiosk_tool": "xinit", "kiosk_vt": 7,
                    "user": "konrad", "uid": os.geteuid(), "gid": os.getegid(),
                    "home": td, "runtime_dir": td,
                })
                self.assertTrue(ho_x)
                self.assertIn("vt7", cmd_x)
                self.assertIn("-keeptty", cmd_x)
                self.assertEqual(env_x.get("XDG_VTNR"), "7")

                # systemd-run kiosk wrapper creates a clean PAM/logind session on /dev/tty7 (isolated from tty1)
                sr_cmd = al.build_systemd_kiosk_command(
                    cmd_c, env_c, user="konrad", gid=1000, cwd=td, vt_num=7, err_log_path="/run/raspyjack-kiosk/u1000/test.err"
                )
                self.assertEqual(sr_cmd[0], "systemd-run")
                self.assertIn("PAMName=login", sr_cmd)
                self.assertIn("UtmpIdentifier=tty7", sr_cmd)
                self.assertIn("UtmpMode=user", sr_cmd)
                self.assertIn("TTYPath=/dev/tty7", sr_cmd)
                self.assertIn("StandardInput=tty-force", sr_cmd)
                self.assertIn("XDG_VTNR=7", " ".join(sr_cmd))
                self.assertIn("XDG_SESSION_TYPE=wayland", " ".join(sr_cmd))

                # Kiosk plans must strip stale DISPLAY/WAYLAND_DISPLAY so cage claims DRM instead of nesting
                self.assertNotIn("WAYLAND_DISPLAY", env_c)
                self.assertNotIn("DISPLAY", env_c)
                self.assertEqual(env_c.get("MOZ_ENABLE_WAYLAND"), "1")
                self.assertIn(env_c.get("ELECTRON_OZONE_PLATFORM_HINT"), ("auto", "wayland"))

                # Stale/non-listening Wayland socket file must NOT fool _is_unix_socket_live
                stale_sock = os.path.join(td, "wayland-0")
                with open(stale_sock, "w", encoding="utf-8") as sh:
                    sh.write("")
                self.assertFalse(al._is_unix_socket_live(stale_sock))

                # Chromium inside Wayland kiosk gets native Wayland hint flag automatically
                self.assertIn("--ozone-platform-hint=auto", al._prepare_wayland_app_argv(["chromium-browser", "https://example.com"]))

                cmd_m, _env_m, ho_m, err_m = al.build_launch_plan(ff, session_info={
                    "mode": "missing_kiosk", "mode_label": "NEEDS CAGE/XINIT", "wayland_display": "",
                    "x_display": "", "kiosk_tool": "", "user": "konrad", "uid": 1000, "gid": 1000,
                    "home": td, "runtime_dir": td,
                })
                self.assertIsNone(cmd_m)
                self.assertIn("cage", err_m.lower())

                # Render both list & grid views + running & error overlays across resolutions
                for w, h in ((640, 360), (320, 170), (128, 128)):
                    self.assertEqual(al_ui.render_app_launcher_frame(w, h, apps2, view_mode="list", session_label="CAGE KIOSK").size, (w, h))
                    self.assertEqual(al_ui.render_app_launcher_frame(w, h, apps2, view_mode="grid", session_label="WAYLAND").size, (w, h))
                    self.assertEqual(al_ui.render_app_launcher_frame(w, h, apps2, overlay={"kind": "running", "title": "Firefox", "sub": "PID 1234"}).size, (w, h))
                    self.assertEqual(al_ui.render_app_launcher_frame(w, h, apps2, overlay={"kind": "error", "title": "Failed", "lines": ["Err line 1", "Err line 2"], "meta": "Log: /dev/shm/rj_app_launcher.log"}).size, (w, h))
            finally:
                if old_base is None:
                    os.environ.pop("RJ_BASE_DIR", None)
                else:
                    os.environ["RJ_BASE_DIR"] = old_base

        import raspyjack
        main_labels = [entry[0] for entry in raspyjack.m.menu["a"]]
        self.assertIn(" Apps", main_labels)


class TestYouTubeMusicPlayer(unittest.TestCase):
    def test_metadata_lrc_fft_queue_playlists_and_all_tabs_render(self):
        import math
        import struct
        import tempfile
        import rj_ytmusic as ytm
        from payloads.utilities import youtube_music as ytm_ui

        # 1. Track normalization & URL classification
        tr = ytm.normalize_track_metadata({
            "id": "abc12345678",
            "title": "Midnight City (Official Audio)",
            "uploader": "M83 - Topic",
            "duration": 243,
        })
        self.assertEqual(tr["title"], "Midnight City")
        self.assertEqual(tr["artist"], "M83")
        self.assertEqual(ytm.parse_ytmusic_input("https://music.youtube.com/watch?v=abc12345678"), ("track", "abc12345678"))
        self.assertEqual(ytm.parse_ytmusic_input("https://music.youtube.com/playlist?list=PL1234567890ABCD")[0], "playlist")

        # 2. Synced LRC lyrics parser & cursor tracking
        lrc = "[00:05.00]First line\n[00:12.50][01:00.00]Chorus line\n[00:20.00]Second verse"
        parsed = ytm.parse_lrc(lrc)
        self.assertEqual(len(parsed), 4)
        self.assertEqual(ytm.get_active_lyric_index(parsed, 13.0), 1)
        self.assertEqual(parsed[1][1], "Chorus line")

        # 3. Live 16-band numpy FFT on synthetic 440Hz sine PCM
        pcm = bytearray()
        for i in range(512):
            val = int(16000 * math.sin(2.0 * math.pi * 440.0 * i / 8000.0))
            pcm.extend(struct.pack("<h", val))
        bands = ytm.compute_fft_bands(bytes(pcm), n_bands=16)
        self.assertEqual(len(bands), 16)
        self.assertTrue(all(0.0 <= b <= 1.0 for b in bands))
        self.assertGreater(max(bands), 0.25)

        # 4. Persistence, playlists, queue reordering, and offline library
        old_base = os.environ.get("RJ_BASE_DIR")
        with tempfile.TemporaryDirectory() as td:
            os.environ["RJ_BASE_DIR"] = td
            try:
                self.assertTrue(ytm.toggle_like(tr))
                self.assertTrue(ytm.is_liked(tr["id"]))
                pl = ytm.create_playlist("Night Drive", [tr])
                self.assertEqual(len(ytm.load_playlists()), 1)
                self.assertTrue(ytm.delete_playlist(pl["id"]))

                lib_dir = ytm.get_library_dir()
                os.makedirs(lib_dir, exist_ok=True)
                with open(os.path.join(lib_dir, "Kavinsky - Nightcall.mp3"), "wb") as h:
                    h.write(b"ID3" + b"\x00" * 512)
                off = ytm.list_offline_library()
                self.assertEqual(len(off), 1)
                self.assertEqual(off[0]["artist"], "Kavinsky")
                self.assertEqual(off[0]["title"], "Nightcall")

                # Player queue manipulation (without spawning audio)
                p = ytm.YTMusicPlayer()
                p.queue = [tr, off[0]]
                p.current_idx = 0
                new_i = p.move_queue_item(0, 1)
                self.assertEqual(new_i, 1)
                self.assertEqual(p.current_idx, 1)
                self.assertEqual(p.cycle_repeat(), "one")
                self.assertTrue(p.toggle_shuffle())
                p.close()
            finally:
                if old_base is None:
                    os.environ.pop("RJ_BASE_DIR", None)
                else:
                    os.environ["RJ_BASE_DIR"] = old_base

        # 5. Render all 6 tabs + synced lyrics view across resolutions
        cov = Image.new("RGB", (180, 180), (60, 32, 18))
        p_state = {
            "track": tr, "state": "playing", "status_msg": "Playing", "elapsed": 13.0,
            "spectrum": bands, "queue": [tr, {"id": "nxt1", "title": "Nightcall", "artist": "Kavinsky", "duration": 258}],
            "current_idx": 0, "volume_pct": 75, "muted": False, "eq_name": "Warm Ember",
            "shuffle": True, "repeat_mode": "all", "auto_radio": True, "liked": True,
            "audio_label": "BT: Sony WH-1000XM5",
        }
        v_data = {"items": p_state["queue"], "cursor": 0, "scroll": 0, "liked_ids": {tr["id"]}}
        lyr = {"status": "ok", "synced": parsed, "plain": []}
        for w, h in ((640, 360), (320, 170), (128, 128)):
            self.assertEqual(ytm_ui.render_ytmusic_frame(w, h, "now", p_state, v_data, cover_img=cov, show_lyrics=False).size, (w, h))
            self.assertEqual(ytm_ui.render_ytmusic_frame(w, h, "now", p_state, v_data, cover_img=cov, lyrics_data=lyr, show_lyrics=True).size, (w, h))
            for tab in ("search", "radio", "queue", "lists", "offline"):
                self.assertEqual(ytm_ui.render_ytmusic_frame(w, h, tab, p_state, v_data, preview_img=cov).size, (w, h))

        import raspyjack
        main_labels = [entry[0] for entry in raspyjack.m.menu["a"]]
        self.assertNotIn(" YT Music", main_labels)
        self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(os.path.dirname(__file__)), "payloads", "utilities", "youtube_music.py")))


class TestBluetoothAndYTMusicBugFixes(unittest.TestCase):
    def test_bluetooth_pty_handles_newline_less_passkey_prompt(self):
        import rj_bluetooth

        self.assertTrue(rj_bluetooth._is_yes_no_prompt("[agent] Confirm passkey 482910 (yes/no): "))
        self.assertTrue(rj_bluetooth._is_yes_no_prompt("Authorize service 0000110b-0000-1000-8000-00805f9b34fb (yes/no):"))
        self.assertTrue(rj_bluetooth._is_yes_no_prompt("Request confirmation"))
        self.assertFalse(rj_bluetooth._is_yes_no_prompt("[NEW] Device AA:BB:CC:DD:EE:FF Headphones"))

        # Spawn a tiny Python child inside _BluetoothCtlSession that prints a prompt
        # WITHOUT a trailing newline and waits for "yes\n" (just like BlueZ bluetoothctl!)
        script = (
            "import sys; "
            "sys.stdout.write('[agent] Confirm passkey 123456 (yes/no): '); "
            "sys.stdout.flush(); "
            "ans = sys.stdin.readline().strip(); "
            "print('Pairing successful' if ans == 'yes' else 'Failed')"
        )
        sess = rj_bluetooth._BluetoothCtlSession()
        self.assertTrue(sess.start(cmd=[sys.executable, "-u", "-c", script]))
        try:
            paired = False
            replied = False
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not paired:
                lines, partial = sess.poll_output(timeout=0.15)
                if partial and rj_bluetooth._is_yes_no_prompt(partial) and not replied:
                    sess.clear_partial()
                    sess.send("yes")
                    replied = True
                for ln in lines:
                    if "Pairing successful" in ln:
                        paired = True
            self.assertTrue(replied, "PTY session should detect newline-less yes/no prompt")
            self.assertTrue(paired, "Child should receive 'yes' and complete pairing")
        finally:
            sess.close()

    def test_ytmusic_stereo_fft_pause_event_and_pcm_sink(self):
        import math
        import struct
        import rj_ytmusic as ytm
        from payloads import _audio_helper as audio_helper

        # Stereo 44.1kHz 440Hz sine wave -> 16 distinct normalized FFT bands
        pcm = bytearray()
        for i in range(1024):
            val = int(18000 * math.sin(2.0 * math.pi * 440.0 * i / 44100.0))
            pcm.extend(struct.pack("<hh", val, val))
        bands = ytm.compute_fft_bands(bytes(pcm), n_bands=16, channels=2)
        self.assertEqual(len(bands), 16)
        self.assertTrue(all(0.0 <= b <= 1.0 for b in bands))
        self.assertGreater(max(bands), 0.30)

        # PCM sink helper returns a real playback command
        sink_cmd = audio_helper.get_pcm_sink_cmd(44100, 2)
        self.assertIsInstance(sink_cmd, list)
        self.assertTrue(len(sink_cmd) >= 1)

        # Pause/unpause uses threading.Event (never SIGSTOP on audio sink!)
        p = ytm.YTMusicPlayer()
        try:
            p.proc = object()
            p.sink_proc = object()
            p.state = "playing"
            p._unpaused.set()
            p.toggle_pause()
            self.assertEqual(p.state, "paused")
            self.assertFalse(p._unpaused.is_set())
            p.toggle_pause()
            self.assertEqual(p.state, "playing")
            self.assertTrue(p._unpaused.is_set())
            self.assertTrue(p.toggle_mute())
            self.assertFalse(p.toggle_mute())
        finally:
            p.proc = None
            p.sink_proc = None
            p.close()

    def test_reentrant_text_mode_and_poll_ui_input_no_double_fire(self):
        import rj_keyboard
        import rj_input
        from payloads import _input_helper as ih

        PINS = {
            "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
            "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
        }
        PIN_TO_VBTN = {
            6: "KEY_UP_PIN", 19: "KEY_DOWN_PIN", 5: "KEY_LEFT_PIN", 26: "KEY_RIGHT_PIN",
            13: "KEY_PRESS_PIN", 21: "KEY1_PIN", 20: "KEY2_PIN", 16: "KEY3_PIN",
        }

        class _ShmGPIO:
            def input(self, pin):
                vbtn = PIN_TO_VBTN.get(pin)
                return 0 if (vbtn and rj_keyboard.is_pressed(vbtn)) else 1

        gpio = _ShmGPIO()

        # 1. Nested text_input_mode() must stay active when inner context (e.g. lcd_keyboard) exits
        with ih.text_input_mode():
            self.assertTrue(rj_keyboard.is_text_mode())
            with ih.text_input_mode():
                self.assertTrue(rj_keyboard.is_text_mode())
            self.assertTrue(rj_keyboard.is_text_mode())

            # 2. Space, Tab, Enter, Escape, ArrowDown, F1 each produce EXACTLY ONE event!
            ih.flush_input()
            cases = [
                (57, (None, " ")),       # Space -> char " " (NOT KEY1!)
                (15, ("TAB", None)),     # Tab -> TAB (NOT KEY2!)
                (28, ("ENTER", None)),   # Enter -> ENTER (NOT ENTER + OK!)
                (1, ("ESCAPE", None)),   # Escape -> ESCAPE (NOT ESCAPE + KEY3!)
                (108, ("DOWN", None)),   # ArrowDown -> ONE DOWN (even while held in SHM!)
                (59, ("KEY1", None)),    # F1 -> KEY1
            ]
            for evdev_code, expected in cases:
                rj_keyboard.handle_key_event(evdev_code, True)
                time.sleep(0.015)  # Allow Unix socket listener thread to receive any datagram
                first = ih.poll_ui_input(PINS, gpio)
                second = ih.poll_ui_input(PINS, gpio)
                rj_keyboard.handle_key_event(evdev_code, False)
                third = ih.poll_ui_input(PINS, gpio)
                self.assertEqual(first, expected, f"First poll mismatch for evdev {evdev_code}")
                self.assertEqual(second, (None, None), f"Double-fire while held for evdev {evdev_code}")
                self.assertEqual(third, (None, None), f"Stray event after release for evdev {evdev_code}")

        self.assertFalse(rj_keyboard.is_text_mode())


class TestEfficiencyOptimizations(unittest.TestCase):
    def test_font_caching_and_cached_hud_strip(self):
        import rj_env
        import hdmi_host

        # Repeated load_font calls must reuse the exact same cached FreeTypeFont instance
        f1 = rj_env.load_font(13, bold=True)
        f2 = rj_env.load_font(13, bold=True)
        self.assertIs(f1, f2)

        # HUD strip caching produces exact output and skips PIL on cache hit
        rgb = bytes([14, 14, 16]) * (640 * 360)
        out1, w1, h1 = hdmi_host.compose_hud_overlay(rgb, 640, 360, text_mode=False)
        t0 = time.monotonic()
        out2, w2, h2 = hdmi_host.compose_hud_overlay(rgb, 640, 360, text_mode=False)
        elapsed = time.monotonic() - t0
        self.assertEqual((w1, h1), (w2, h2))
        self.assertEqual(out1, out2)
        self.assertLess(elapsed, 0.05)

        # Reusable fb buffer blitter matches convert_rgb24_to_fb across 16bpp, 24bpp, and 32bpp
        rgb4 = bytes([12, 34, 250]) * (4 * 4)
        for bpp, stride, r_o, g_o, b_o in (
            (16, 16, 11, 5, 0),
            (24, 28, 16, 8, 0),
            (32, 40, 16, 8, 0),
            (32, 40, 0, 8, 16),
        ):
            expected = hdmi_host.convert_rgb24_to_fb(
                rgb4, 4, 4, 8, 4, bpp=bpp, line_length=stride, red_off=r_o, green_off=g_o, blue_off=b_o
            )
            buf = hdmi_host._init_fb_buffer(8, 4, bpp, stride, r_o, g_o, b_o)
            hdmi_host._blit_rgb24_into_fb_buf(
                buf, rgb4, 4, 4, 8, 4, bpp=bpp, red_off=r_o, green_off=g_o, blue_off=b_o
            )
            self.assertEqual(buf.tobytes(), expected)

    def test_lazy_frame_mirror_skips_jpegs_at_zero_clients(self):
        import json
        import tempfile
        import LCD_1in44

        with tempfile.TemporaryDirectory() as td:
            old_vals = (
                LCD_1in44._FRAME_MIRROR_PATH,
                LCD_1in44._CARDPUTER_FRAME_PATH,
                LCD_1in44._WS_CLIENTS_PATH,
                LCD_1in44._FRAME_MIRROR_ENABLED,
                LCD_1in44._CARDPUTER_FRAME_ENABLED,
                LCD_1in44._FORCE_FRAME_MIRROR,
                LCD_1in44._mirror_seed_written,
                LCD_1in44._last_frame_save,
                LCD_1in44._ws_clients_cache,
            )
            try:
                jpg_path = os.path.join(td, "last.jpg")
                cp_path = os.path.join(td, "cp.jpg")
                cli_path = os.path.join(td, "clients.json")
                LCD_1in44._FRAME_MIRROR_PATH = jpg_path
                LCD_1in44._CARDPUTER_FRAME_PATH = cp_path
                LCD_1in44._WS_CLIENTS_PATH = cli_path
                LCD_1in44._FRAME_MIRROR_ENABLED = True
                LCD_1in44._CARDPUTER_FRAME_ENABLED = True
                LCD_1in44._FORCE_FRAME_MIRROR = False
                LCD_1in44._mirror_seed_written = False
                LCD_1in44._last_frame_save = 0.0
                LCD_1in44._ws_clients_cache = (0.0, 0, 0)

                with open(cli_path, "w", encoding="utf-8") as fh:
                    json.dump({"pid": os.getpid(), "legacy": 0, "cardputer": 0}, fh)

                lcd = LCD_1in44.LCD()
                img = Image.new("RGB", (lcd.width, lcd.height), (14, 14, 16))

                # 1st frame writes 1 initial seed JPEG only; skips Cardputer JPEG
                lcd.LCD_ShowImage(img, 0, 0)
                self.assertTrue(os.path.isfile(jpg_path))
                self.assertFalse(os.path.isfile(cp_path))

                # Subsequent frames with 0 clients skip JPEG encoding completely
                os.unlink(jpg_path)
                LCD_1in44._last_frame_save = 0.0
                LCD_1in44._ws_clients_cache = (0.0, 0, 0)
                lcd.LCD_ShowImage(img, 0, 0)
                self.assertFalse(os.path.isfile(jpg_path))
                self.assertFalse(os.path.isfile(cp_path))

                # When remote clients connect, mirroring resumes automatically!
                with open(cli_path, "w", encoding="utf-8") as fh:
                    json.dump({"pid": os.getpid(), "legacy": 1, "cardputer": 1}, fh)
                LCD_1in44._last_frame_save = 0.0
                LCD_1in44._ws_clients_cache = (0.0, 0, 0)
                lcd.LCD_ShowImage(img, 0, 0)
                self.assertTrue(os.path.isfile(jpg_path))
                self.assertTrue(os.path.isfile(cp_path))
            finally:
                (
                    LCD_1in44._FRAME_MIRROR_PATH,
                    LCD_1in44._CARDPUTER_FRAME_PATH,
                    LCD_1in44._WS_CLIENTS_PATH,
                    LCD_1in44._FRAME_MIRROR_ENABLED,
                    LCD_1in44._CARDPUTER_FRAME_ENABLED,
                    LCD_1in44._FORCE_FRAME_MIRROR,
                    LCD_1in44._mirror_seed_written,
                    LCD_1in44._last_frame_save,
                    LCD_1in44._ws_clients_cache,
                ) = old_vals

    def test_proc_scanner_is_fast_with_zero_sleep(self):
        import raspyjack

        t0 = time.monotonic()
        r1 = raspyjack.is_mitm_running()
        r2 = raspyjack.is_responder_running()
        elapsed = time.monotonic() - t0
        self.assertIsInstance(r1, bool)
        self.assertIsInstance(r2, bool)
        self.assertLess(elapsed, 0.5, "Pure-/proc status scan must not sleep 1s or fork shells")


class TestHeadlessAudioFixes(unittest.TestCase):
    def test_wpctl_and_alsa_parsers_and_headless_selector(self):
        import tempfile
        from unittest import mock
        import rj_bluetooth
        from payloads import _audio_helper as ah

        wp_out = (
            "PipeWire 'pipewire-0' [1.0.3]\n"
            "Audio\n"
            " ├─ Devices:\n"
            " │      40. Built-in Audio [alsa]\n"
            " ├─ Sinks:\n"
            " │      47. Built-in Audio Digital Stereo (HDMI)    [vol: 0.75]\n"
            " │  *   59. Sony WH-1000XM5                         [vol: 0.85]\n"
            " ├─ Sources:\n"
            " │      61. USB Mic                                 [vol: 1.00]\n"
            "Video\n"
        )
        wp_sinks = rj_bluetooth.parse_wpctl_sinks(wp_out)
        self.assertEqual(len(wp_sinks), 2)
        self.assertEqual(wp_sinks[0]["name"], "wpctl:47")
        self.assertFalse(wp_sinks[0]["is_default"])
        self.assertEqual(wp_sinks[1]["name"], "wpctl:59")
        self.assertTrue(wp_sinks[1]["is_default"])
        self.assertTrue(wp_sinks[1]["is_bluetooth"])

        aplay_l = (
            "**** List of PLAYBACK Hardware Devices ****\n"
            "card 0: vc4hdmi0 [vc4-hdmi-0], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]\n"
            "card 1: vc4hdmi1 [vc4-hdmi-1], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]\n"
            "card 2: Headphones [bcm2835 Headphones], device 0: bcm2835 Headphones [bcm2835 Headphones]\n"
        )
        alsa_devs = rj_bluetooth.parse_alsa_playback_devices(aplay_l)
        self.assertEqual(len(alsa_devs), 3)
        self.assertEqual(alsa_devs[0]["alsa_dev"], "plughw:0,0")
        self.assertEqual(alsa_devs[0]["label"], "HDMI 0 (vc4-hdmi-0)")
        self.assertEqual(alsa_devs[1]["label"], "HDMI 1 (vc4-hdmi-1)")
        self.assertEqual(alsa_devs[2]["label"], "3.5mm Headphones")

        # Detects dead PipeWire/PulseAudio ALSA `default` hijacking
        self.assertTrue(ah._alsa_default_is_sound_server(
            "default\n    Default ALSA Output (currently PipeWire Media Server)\nnull\n"
        ))
        self.assertFalse(ah._alsa_default_is_sound_server(
            "default\n    Direct hardware device\n"
        ))

        # Priority selection: saved override -> ES838x -> USB -> connected HDMI -> 3.5mm jack -> first card
        self.assertEqual(ah.select_best_alsa_device(alsa_devs, saved_alsa_dev="plughw:2,0", connected_hdmi={0}), "plughw:2,0")
        self.assertEqual(ah.select_best_alsa_device(alsa_devs, connected_hdmi={1}), "plughw:1,0")
        self.assertEqual(ah.select_best_alsa_device(alsa_devs, connected_hdmi=set()), "plughw:2,0")

        usb_devs = alsa_devs + rj_bluetooth.parse_alsa_playback_devices(
            "card 3: Device [USB Audio Device], device 0: USB Audio [USB Audio]\n"
        )
        self.assertEqual(ah.select_best_alsa_device(usb_devs, connected_hdmi={0}), "plughw:3,0")

        # Saving an ALSA sink works even without pactl/PipeWire
        old_state_file = rj_bluetooth.AUDIO_STATE_FILE
        with tempfile.TemporaryDirectory() as td:
            rj_bluetooth.AUDIO_STATE_FILE = os.path.join(td, "audio.json")
            try:
                ok, msg = rj_bluetooth.set_default_audio_sink("alsa:plughw:2,0", label="3.5mm Headphones")
                self.assertTrue(ok)
                self.assertIn("3.5mm Headphones", msg)
                st = rj_bluetooth.load_audio_state()
                self.assertEqual(st.get("alsa_dev"), "plughw:2,0")
                self.assertEqual(st.get("backend"), "alsa")
            finally:
                rj_bluetooth.AUDIO_STATE_FILE = old_state_file

        # Root-to-PipeWire UID drop: ONLY for sound-server clients, NEVER for direct hardware ALSA
        fake_sess = {
            "found": True,
            "runtime_dir": "/run/user/1000",
            "pulse_sock": "/run/user/1000/pulse/native",
            "pw_sock": "/run/user/1000/pipewire-0",
            "dbus_sock": "/run/user/1000/bus",
            "pulse_cookie": "",
            "uid": 1000,
            "gid": 1000,
            "user": "pi",
            "home": "/home/pi",
            "extra_groups": [1000, 29],
        }
        with mock.patch.object(rj_bluetooth, "get_audio_session_info", return_value=fake_sess), \
             mock.patch("os.geteuid", return_value=0):
            kw_pw = ah.get_audio_popen_kwargs(["paplay", "--raw"])
            self.assertEqual(kw_pw.get("user"), 1000)
            self.assertEqual(kw_pw.get("group"), 1000)
            self.assertEqual(kw_pw["env"].get("XDG_RUNTIME_DIR"), "/run/user/1000")
            self.assertEqual(kw_pw["env"].get("PULSE_SERVER"), "unix:/run/user/1000/pulse/native")

            kw_ff_pulse = ah.get_audio_popen_kwargs(["ffmpeg", "-i", "https://x", "-f", "pulse", "default"])
            self.assertEqual(kw_ff_pulse.get("user"), 1000)

            kw_alsa = ah.get_audio_popen_kwargs(["aplay", "-q", "-D", "plughw:0,0"])
            self.assertNotIn("user", kw_alsa)

            kw_ff_alsa = ah.get_audio_popen_kwargs(["ffmpeg", "-i", "x.mp3", "-f", "alsa", "plughw:2,0"])
            self.assertNotIn("user", kw_ff_alsa)


class TestKeyboardMouseAndKioskExit(unittest.TestCase):
    def test_virtual_mouse_toggle_motion_clicks_and_force_exit(self):
        import rj_kbd_mouse as km

        st = km.VirtualMouseState()
        self.assertFalse(st.mouse_mode)

        # Normal typing while Mouse Mode is OFF produces NO mouse actions
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
        self.assertIs(a_up["mode_changed"], True)
        self.assertTrue(st.mouse_mode)

        # Movement (tap nudge + continuous acceleration + Shift precision + live speed settings 1..10)
        self.assertEqual(st.mouse_speed, km.DEFAULT_MOUSE_SPEED)
        a_mv = st.process_key_event(km.KEY_RIGHT, 1)
        self.assertGreater(a_mv["rel_nudge"][0], 0)
        dx_default, dy1, _ = st.compute_motion_step(0.05)
        self.assertGreater(dx_default, 0)
        self.assertEqual(dy1, 0)
        # Live speed adjust: '-' lowers speed, '+' raises speed
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

        # Backspace maps to KEY3_PIN in standard nav mode, and stays pure BACKSPACE in text mode
        import rj_keyboard
        self.assertEqual(rj_keyboard.STANDARD_NAV_MAP.get(14), "KEY3_PIN")
        self.assertNotIn(14, rj_keyboard.TEXT_MODE_NAV_CODES)

        # Left click hold + Right click + Scroll wheel
        a_lc = st.process_key_event(km.KEY_SPACE, 1)
        self.assertIn((km.BTN_LEFT, 1), a_lc["buttons"])
        a_rc = st.process_key_event(km.KEY_X, 1)
        self.assertIn((km.BTN_RIGHT, 1), a_rc["buttons"])
        a_sc = st.process_key_event(km.KEY_PAGEUP, 1)
        self.assertEqual(a_sc["wheel_step"], 1)
        st.process_key_event(km.KEY_PAGEUP, 0)

        # Exiting Mouse Mode with Esc automatically releases any held mouse buttons!
        st.process_key_event(km.KEY_ESC, 1)
        a_exit_mm = st.process_key_event(km.KEY_ESC, 0)
        self.assertIs(a_exit_mm["mode_changed"], False)
        self.assertFalse(st.mouse_mode)
        self.assertIn((km.BTN_LEFT, 0), a_exit_mm["buttons"])
        self.assertIn((km.BTN_RIGHT, 0), a_exit_mm["buttons"])

        # Dedicated Ctrl+Alt+K explicitly returns from Mouse Mode to Keyboard Typing Mode
        st.process_key_event(km.KEY_SCROLLLOCK, 1)
        a_on2 = st.process_key_event(km.KEY_SCROLLLOCK, 0)
        self.assertIs(a_on2["mode_changed"], True)
        self.assertTrue(st.mouse_mode)
        st.process_key_event(km.KEY_LCTRL, 1)
        st.process_key_event(km.KEY_LALT, 1)
        st.process_key_event(km.KEY_K, 1)
        st.process_key_event(km.KEY_K, 0)
        st.process_key_event(km.KEY_LALT, 0)
        a_kbd = st.process_key_event(km.KEY_LCTRL, 0)
        self.assertIs(a_kbd["mode_changed"], False)
        self.assertFalse(st.mouse_mode)

        # Universal Emergency Exit: Ctrl+Alt+Q works from typing mode immediately
        st.process_key_event(km.KEY_LCTRL, 1)
        st.process_key_event(km.KEY_LALT, 1)
        a_quit = st.process_key_event(km.KEY_Q, 1)
        self.assertTrue(a_quit["force_exit"])
        self.assertTrue(st.force_exit_requested)


class TestBuiltInUserGuide(unittest.TestCase):
    def test_guide_topics_search_rendering_and_main_menu_entry(self):
        import rj_guide
        import rj_tool_catalog
        from payloads.utilities import guide as guide_ui
        import raspyjack

        topics = rj_guide.get_all_topics()
        self.assertGreaterEqual(len(topics), 25)
        for tab in rj_guide.GUIDE_TABS:
            sub = rj_guide.filter_topics(topics, tab=tab)
            self.assertGreaterEqual(len(sub), 2, f"Expected topics in tab {tab}")

        # 100% repo coverage: EVERY user-facing payload file in payloads/ (ignoring _* helpers
        # and the 3 intentionally skipped test/example stubs) MUST be documented in rj_tool_catalog!
        ignored_stubs = {
            "examples/example_show_buttons.py",
            "utilities/test_ble_wait_for_present.py",
            "utilities/trigger_marker.py",
        }
        payloads_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "payloads")
        expected_ids = set()
        for cat in sorted(os.listdir(payloads_root)):
            cpath = os.path.join(payloads_root, cat)
            if not os.path.isdir(cpath) or cat.startswith("_") or cat == "__pycache__":
                continue
            for dirpath, dirnames, filenames in os.walk(cpath):
                dirnames[:] = [d for d in dirnames if not d.startswith("_") and d != "__pycache__"]
                for fn in filenames:
                    if not fn.endswith(".py") or fn.startswith("_"):
                        continue
                    rel = f"{cat}/{os.path.relpath(os.path.join(dirpath, fn), cpath)}"
                    if rel in ignored_stubs:
                        continue
                    expected_ids.add(f"{cat}/{os.path.splitext(fn)[0]}")

        documented_ids = set()
        for cat, entries in rj_tool_catalog.TOOL_CATALOG.items():
            for entry in entries:
                self.assertTrue(entry.get("title"), f"Missing title for {cat}/{entry.get('id')}")
                self.assertGreaterEqual(len(entry.get("desc", "")), 18, f"Description too short for {cat}/{entry.get('id')}")
                documented_ids.add(f"{cat}/{entry['id']}")

        missing = sorted(expected_ids - documented_ids)
        self.assertEqual(missing, [], f"Undocumented user-facing payloads: {missing}")
        self.assertEqual(len(documented_ids), 329)

        # Full-text search matches deep tools and ranks direct tool matches first + auto-scrolls!
        self.assertTrue(any(t["id"] == "kbd_mouse" for t in rj_guide.filter_topics(topics, query="ctrl + alt + k")))
        self.assertTrue(any(t["id"] == "youtube_music" for t in rj_guide.filter_topics(topics, query="fft")))
        self.assertTrue(any(t["id"] == "troubleshooting" for t in rj_guide.filter_topics(topics, query="wireplumber")))
        self.assertTrue(any(t["id"] == "cli_and_setup_scripts" for t in rj_guide.filter_topics(topics, query="install_proxmark3")))
        kerb_hits = rj_guide.filter_topics(topics, query="kerberoast")
        self.assertTrue(kerb_hits and kerb_hits[0]["id"] == "cat_credentials")
        fonts640 = guide_ui._load_fonts(640, 360)
        kerb_rows = guide_ui.build_article_lines(kerb_hits[0], 390, fonts640)
        self.assertGreater(guide_ui.find_first_match_row(kerb_rows, "kerberoast"), 0)
        self.assertEqual(rj_guide.filter_topics(topics, query="nonexistent_xyz_token_999"), [])

        # Render split, payload-encyclopedia, reader-focused, full-width reader & empty-search across all screen sizes
        util_topic_idx = next(i for i, t in enumerate(topics) if t["id"] == "cat_utilities")
        for w, h in ((640, 360), (320, 170), (128, 128)):
            img1, max_sc1 = guide_ui.render_guide_frame(w, h, topics, tab="all", cursor=util_topic_idx, focus="list")
            self.assertEqual(img1.size, (w, h))
            if w >= 480:
                self.assertGreater(max_sc1, 20)
            else:
                self.assertEqual(max_sc1, 0)
            img_r, max_sc_r = guide_ui.render_guide_frame(w, h, topics, tab="all", cursor=util_topic_idx, article_scroll=10, focus="reader")
            self.assertEqual(img_r.size, (w, h))
            self.assertGreater(max_sc_r, 20)
            img2, max_sc2 = guide_ui.render_guide_frame(w, h, topics, tab="payloads", cursor=0, article_scroll=5, focus="reader", full_reader=True, query="winamp")
            self.assertEqual(img2.size, (w, h))
            self.assertGreaterEqual(max_sc2, 0)
            img3, _ = guide_ui.render_guide_frame(w, h, topics, tab="all", query="no_match_zzz")
            self.assertEqual(img3.size, (w, h))

        # Main menu & menu_icons expose " Guide"
        main_labels = [entry[0] for entry in raspyjack.m.menu["a"]]
        self.assertIn(" Guide", main_labels)
        self.assertTrue(bool(raspyjack._menu_icon_for_label(" Guide")))


class TestPerformanceOptimizations(unittest.TestCase):
    def test_rj_theme_caches_match_uncached_results(self):
        import rj_env
        import rj_theme
        from PIL import ImageDraw

        img = Image.new("RGB", (320, 100), (14, 14, 16))
        d = ImageDraw.Draw(img)
        font = rj_env.load_font(12)

        sample = "RaspyJack Obsidian Ember UI"
        self.assertEqual(rj_theme.measure_text(font, sample, d), d.textbbox((0, 0), sample, font=font))
        # Repeat hit from cache
        self.assertEqual(rj_theme.measure_text(font, sample, d), d.textbbox((0, 0), sample, font=font))

        trunc = rj_theme.truncate_text(d, "A very long menu label that must truncate cleanly", 90, font)
        self.assertTrue(trunc.endswith("..."))
        self.assertLessEqual(d.textbbox((0, 0), trunc, font=font)[2], 90)

        w1 = rj_theme.wrap_text(d, "alpha beta gamma delta epsilon zeta", 75, font)
        w1_copy = list(w1)
        w1.append("MUTATED")
        w2 = rj_theme.wrap_text(d, "alpha beta gamma delta epsilon zeta", 75, font)
        self.assertEqual(w2, w1_copy, "Cached wrap_text must return an isolated list copy")

        self.assertEqual(rj_theme.blend_hex("#000000", "#ffffff", 0.5), "#808080")
        self.assertEqual(rj_theme.hex_to_rgb("#E07A2F"), (224, 122, 47))

    def test_terminal_row_cache_is_pixel_identical_to_uncached_render(self):
        import rj_terminal

        metrics = rj_terminal.compute_terminal_metrics(640, 360)
        screen = rj_terminal.AnsiScreenBuffer(rows=metrics["rows"], cols=metrics["cols"])

        def _assert_pixel_equal(cursor_on=True, quick_open=False, quick_idx=0, pid=1234):
            cached = rj_terminal.render_terminal_frame(
                screen, 640, 360, metrics=metrics,
                cursor_blink_on=cursor_on, quick_menu_open=quick_open,
                quick_index=quick_idx, pid=pid,
            )
            fresh = rj_terminal.render_terminal_frame(
                screen, 640, 360, metrics=None,
                cursor_blink_on=cursor_on, quick_menu_open=quick_open,
                quick_index=quick_idx, pid=pid,
            )
            self.assertEqual(cached.tobytes(), fresh.tobytes())

        # 1. Initial colored prompt
        screen.feed("\x1b[1;32mroot@pi\x1b[0m:\x1b[34m~\x1b[0m$ ")
        _assert_pixel_equal(cursor_on=True)
        # 2. Cursor blink OFF -> ON (0 rows re-rendered)
        _assert_pixel_equal(cursor_on=False)
        _assert_pixel_equal(cursor_on=True)
        # 3. Single char typing (1 row re-rendered)
        screen.feed("ls -la")
        _assert_pixel_equal(cursor_on=True)
        # 4. Scroll 30 lines into scrollback
        for i in range(30):
            screen.feed(f"\r\nline {i:02d}: \x1b[33mitem_{i}\x1b[0m ok")
        _assert_pixel_equal(cursor_on=True)
        # 5. Scrollback navigation up and back down
        screen.scroll_view(5)
        _assert_pixel_equal(cursor_on=True)
        screen.reset_scroll()
        _assert_pixel_equal(cursor_on=True)
        # 6. Quick command overlay toggle
        _assert_pixel_equal(cursor_on=True, quick_open=True, quick_idx=2)
        _assert_pixel_equal(cursor_on=True, quick_open=False)
        # 7. Alt-screen enter & leave
        screen.feed("\x1b[?1049h\x1b[2J\x1b[HALT SCREEN VIM MODE")
        _assert_pixel_equal(cursor_on=True)
        screen.feed("\x1b[?1049l")
        _assert_pixel_equal(cursor_on=True)
        # 8. Clear all
        screen.clear_all()
        _assert_pixel_equal(cursor_on=True)

    def test_raspyjack_cached_palette_and_zero_disk_catalog_summary(self):
        import raspyjack

        # Derived palette attributes update synchronously when preset/color changes
        orig = raspyjack.color.Dictonary()
        try:
            raspyjack.color.ApplyPreset("Classic Green")
            self.assertEqual(raspyjack.color.border, "#257845")
            self.assertEqual(
                raspyjack.color.border_subtle,
                raspyjack.rj_theme.blend_hex(raspyjack.color.background, "#257845", 0.24),
            )
            raspyjack.color.Set(0, "#101012")
            self.assertEqual(
                raspyjack.color.surface,
                raspyjack.rj_theme.blend_hex("#101012", "#26252B", 0.35),
            )
        finally:
            raspyjack.color.LoadDictonary(orig, allow_migrate=False)

        # Catalog-backed payload summary returns curated description immediately
        raspyjack._PAYLOAD_DOC_CACHE.pop("utilities/guide.py", None)
        summary = raspyjack._extract_payload_summary("utilities/guide.py")
        self.assertIn("reference manual", summary.lower())
        self.assertNotEqual(summary, "Executable EmberJack payload script.")

        # All 3 menu views render cleanly with cached toolbar/footer strips
        items = raspyjack.m.GetMenuList()
        raspyjack.RenderMenuWindowOnce(items, 0)
        raspyjack.RenderMenuWindowOnce(items, 1)
        raspyjack.RenderMenuCarouselOnce(items, 1)
        raspyjack.RenderMenuGridOnce(items, 1)

    def test_youtube_preload_settings_render_modes_and_osd_buffer(self):
        import tempfile
        from payloads.utilities import youtube as yt

        # Balanced 480x270 (exact 4x divisor of 1920x1080)
        self.assertEqual(yt.compute_video_render_size("720p", 640, 360, "balanced", is_hdmi=True), (480, 270))
        self.assertEqual(yt.compute_video_render_size("360p", 640, 360, "balanced", is_hdmi=True), (480, 270))

        # Audio-clock-paced preloaded local MKV omits -re
        mkv_cmd = yt.build_video_ffmpeg_cmd(
            "/dev/shm/rj_yt_preload/demo.mkv", "", 480, 270, "rgb24", 24,
            ["-f", "pulse", "-buffer_duration", "150", "default"],
            start_offset=12.5, use_realtime=False, input_is_pipe=False,
        )
        self.assertNotIn("-re", mkv_cmd)
        self.assertNotIn("-reconnect", mkv_cmd)
        self.assertEqual(mkv_cmd[mkv_cmd.index("-ss") + 1], "12.50")
        self.assertLess(mkv_cmd.index("-ss"), mkv_cmd.index("-i"))

        # Progressive growing MKV over pipe:0 uses -f matroska -i pipe:0 and puts -ss after -i
        pipe_cmd = yt.build_video_ffmpeg_cmd(
            "pipe:0", "", 320, 180, "rgb24", 24,
            ["-f", "pulse", "-buffer_duration", "150", "default"],
            start_offset=8.0, use_realtime=False, input_is_pipe=True,
        )
        self.assertNotIn("-re", pipe_cmd)
        self.assertNotIn("-reconnect", pipe_cmd)
        self.assertIn("matroska", pipe_cmd)
        self.assertIn("pipe:0", pipe_cmd)
        self.assertGreater(pipe_cmd.index("-ss"), pipe_cmd.index("pipe:0"))

        # Settings defaults & persistence of preload_sec, balanced render_scale, player_engine
        old_base = os.environ.get("RJ_BASE_DIR")
        with tempfile.TemporaryDirectory() as td:
            os.environ["RJ_BASE_DIR"] = td
            try:
                cfg0 = yt.load_settings()
                self.assertEqual(cfg0["preload_sec"], 5)
                self.assertEqual(cfg0["player_engine"], "builtin")
                cfg0["preload_sec"] = -1
                cfg0["render_scale"] = "balanced"
                cfg0["player_engine"] = "mpv"
                yt.save_settings(cfg0)
                cfg1 = yt.load_settings()
                self.assertEqual(cfg1["preload_sec"], -1)
                self.assertEqual(cfg1["render_scale"], "balanced")
                self.assertEqual(cfg1["player_engine"], "mpv")
            finally:
                if old_base is None:
                    os.environ.pop("RJ_BASE_DIR", None)
                else:
                    os.environ["RJ_BASE_DIR"] = old_base

        # OSD renders cleanly with buffered_frac + buffer_badge across resolutions
        for w, h in ((640, 360), (480, 270), (320, 180), (128, 128)):
            frame = Image.new("RGB", (w, h), (18, 22, 28))
            osd1 = yt.render_video_osd(frame, "Preload Test Video", 30.0, 120, 80, buffered_frac=0.72, buffer_badge="BUF 72%")
            osd2 = yt.render_video_osd(frame, "Preload Test Video", 30.0, 120, 80, buffered_frac=1.0, buffer_badge="CACHED")
            self.assertEqual(osd1.size, (w, h))
            self.assertEqual(osd2.size, (w, h))

    def test_youtube_preloader_chunked_range_remux_and_seek(self):
        import http.server
        import shutil
        import subprocess
        import tempfile
        import threading
        from payloads.utilities import youtube as yt

        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg not installed")

        with tempfile.TemporaryDirectory() as td:
            v_src = os.path.join(td, "v_only.mp4")
            a_src = os.path.join(td, "a_only.m4a")
            # Generate tiny 3-second faststart H.264 video + AAC audio test streams
            r1 = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=3",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", v_src,
                ],
                capture_output=True, timeout=20,
            )
            r2 = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", a_src,
                ],
                capture_output=True, timeout=20,
            )
            self.assertEqual(r1.returncode, 0, r1.stderr.decode("utf-8", "ignore"))
            self.assertEqual(r2.returncode, 0, r2.stderr.decode("utf-8", "ignore"))

            with open(v_src, "rb") as fh:
                v_blob = fh.read()
            with open(a_src, "rb") as fh:
                a_blob = fh.read()
            blobs = {"/video.mp4": v_blob, "/audio.m4a": a_blob}

            class _RangeHandler(http.server.BaseHTTPRequestHandler):
                def log_message(self, fmt, *args):
                    pass

                def do_GET(self):
                    data = blobs.get(self.path)
                    if data is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    total = len(data)
                    rng = self.headers.get("Range", "")
                    if rng.startswith("bytes="):
                        spec = rng[6:].split(",")[0].strip()
                        lo_s, hi_s = spec.split("-", 1)
                        lo = int(lo_s) if lo_s else 0
                        hi = min(total - 1, int(hi_s)) if hi_s else (total - 1)
                        if lo >= total:
                            self.send_response(416)
                            self.send_header("Content-Range", f"bytes */{total}")
                            self.end_headers()
                            return
                        chunk = data[lo : hi + 1]
                        self.send_response(206)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Range", f"bytes {lo}-{hi}/{total}")
                        self.send_header("Content-Length", str(len(chunk)))
                        self.end_headers()
                        self.wfile.write(chunk)
                    else:
                        self.send_response(200)
                        self.send_header("Content-Length", str(total))
                        self.end_headers()
                        self.wfile.write(data)

            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
            port = srv.server_address[1]
            st = threading.Thread(target=srv.serve_forever, daemon=True)
            st.start()
            cache_dir = os.path.join(td, "cache")
            try:
                base_url = f"http://127.0.0.1:{port}"
                # Use 16 KB chunks so multiple HTTP 206 Range requests are exercised
                preloader = yt.StreamPreloader(
                    video_id="unitVid001",
                    video_url=f"{base_url}/video.mp4",
                    audio_url=f"{base_url}/audio.m4a",
                    quality_label="360p",
                    duration=3,
                    cache_dir=cache_dir,
                    chunk_size=16 * 1024,
                ).start()
                self.assertTrue(preloader.done_event.wait(timeout=15.0), "Preloader did not finish in time")
                self.assertTrue(preloader.complete, f"Preloader failed: {preloader.error}")
                self.assertFalse(preloader.failed)
                self.assertTrue(os.path.isfile(preloader.mkv_path))
                self.assertEqual(preloader.buffered_fraction(), 1.0)
                self.assertGreaterEqual(sum(p.range_requests for p in preloader._pumps), 2)
                self.assertTrue(preloader.ready_for_playback(5))
                self.assertTrue(preloader.ready_for_playback(-1))

                # 1. Verify progressive tail-feeder -> pipe:0 decode works cleanly
                frame_bytes = 320 * 180 * 3
                pipe_cmd = yt.build_video_ffmpeg_cmd(
                    "pipe:0", "", 320, 180, "rgb24", 12, ["-f", "null", "-"],
                    use_realtime=False, input_is_pipe=True,
                )
                p_play = subprocess.Popen(
                    pipe_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
                )
                feed_stop = threading.Event()
                ft = preloader.start_tail_feeder(p_play.stdin, feed_stop)
                got_bytes = 0
                try:
                    while True:
                        b = p_play.stdout.read(frame_bytes)
                        if not b:
                            break
                        got_bytes += len(b)
                finally:
                    p_play.stdout.close()
                rc = p_play.wait(timeout=10)
                ft.join(timeout=2)
                self.assertEqual(rc, 0)
                self.assertGreaterEqual(got_bytes // frame_bytes, 24)

                # 2. Verify instant cached seek on completed local MKV
                seek_cmd = yt.build_video_ffmpeg_cmd(
                    preloader.mkv_path, "", 320, 180, "rgb24", 12, ["-f", "null", "-"],
                    start_offset=1.5, use_realtime=False, input_is_pipe=False,
                )
                p_seek = subprocess.Popen(
                    seek_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
                )
                seek_bytes = 0
                try:
                    while True:
                        b = p_seek.stdout.read(frame_bytes)
                        if not b:
                            break
                        seek_bytes += len(b)
                finally:
                    p_seek.stdout.close()
                self.assertEqual(p_seek.wait(timeout=10), 0)
                self.assertGreaterEqual(seek_bytes // frame_bytes, 10)

                # 2b. Mid-video quality switch preloader (start_offset=1.5):
                #     remuxes directly starting at 1.5s with zero decode skip & plays from pipe:0 with offset=0!
                mid_pl = yt.StreamPreloader(
                    video_id="unitVid001",
                    video_url=f"{base_url}/video.mp4",
                    audio_url=f"{base_url}/audio.m4a",
                    quality_label="240p",
                    duration=3,
                    cache_dir=cache_dir,
                    chunk_size=16 * 1024,
                    start_offset=1.5,
                ).start()
                self.assertTrue(mid_pl.done_event.wait(timeout=15.0))
                self.assertTrue(mid_pl.complete, f"Mid-offset preloader failed: {mid_pl.error}")
                self.assertFalse(mid_pl.covers_offset(0.0))
                self.assertTrue(mid_pl.covers_offset(1.5))
                self.assertTrue(mid_pl.covers_offset(2.2))
                mid_cmd = yt.build_video_ffmpeg_cmd(
                    "pipe:0", "", 320, 180, "rgb24", 12, ["-f", "null", "-"],
                    start_offset=0.0, use_realtime=False, input_is_pipe=True,
                )
                p_mid = subprocess.Popen(
                    mid_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
                )
                mid_stop = threading.Event()
                ft_mid = mid_pl.start_tail_feeder(p_mid.stdin, mid_stop)
                mid_bytes = 0
                try:
                    while True:
                        b = p_mid.stdout.read(frame_bytes)
                        if not b:
                            break
                        mid_bytes += len(b)
                finally:
                    p_mid.stdout.close()
                self.assertEqual(p_mid.wait(timeout=10), 0)
                ft_mid.join(timeout=2)
                self.assertGreaterEqual(mid_bytes // frame_bytes, 10)
                mid_pl.stop(remove_file=True)

                # 3. Verify 404 stream sets failed=True and releases ready_for_playback immediately
                bad = yt.StreamPreloader(
                    video_id="missing404",
                    video_url=f"{base_url}/nope.mp4",
                    audio_url="",
                    quality_label="360p",
                    duration=10,
                    cache_dir=cache_dir,
                ).start()
                self.assertTrue(bad.done_event.wait(timeout=10.0))
                self.assertTrue(bad.failed)
                self.assertFalse(bad.complete)
                self.assertTrue(bad.ready_for_playback(5))
                bad.stop(remove_file=True)

                # 4. Cache clear cleans up .mkv
                yt.clear_preload_cache(cache_dir=cache_dir)
                self.assertFalse(os.path.exists(preloader.mkv_path))
            finally:
                srv.shutdown()
                srv.server_close()

    def test_hdmi_zero_alloc_stride_blit_exactness(self):
        import numpy as np
        import hdmi_host

        # Verify 32-bit BGRA and 16-bit RGB565 4D stride broadcast scaling for 3x, 4x, 6x factors
        for src_w, src_h, factor in ((320, 180, 6), (480, 270, 4), (640, 360, 3)):
            fb_w, fb_h = src_w * factor, src_h * factor
            rng = np.random.default_rng(42 + factor)
            src = rng.integers(0, 256, size=(src_h, src_w, 3), dtype=np.uint8)

            # 32-bit BGRA check (x8r8g8b8)
            fb32 = np.zeros((fb_h, fb_w * 4), dtype=np.uint8)
            hdmi_host._blit_rgb24_into_fb_buf_dirty(
                fb32, src.tobytes(), src_w, src_h, fb_w, fb_h, 32,
                red_off=16, green_off=8, blue_off=0, scale_mode="fit", fast_scale=True,
            )
            out32 = fb32.reshape((fb_h, fb_w, 4))
            self.assertEqual(out32[0, 0, 2], int(src[0, 0, 0]))  # R
            self.assertEqual(out32[0, 0, 1], int(src[0, 0, 1]))  # G
            self.assertEqual(out32[0, 0, 0], int(src[0, 0, 2]))  # B
            # Check integer block replication at bottom-right pixel
            self.assertTrue(np.array_equal(out32[-1, -1, :3], out32[-factor, -factor, :3]))
            self.assertEqual(out32[-1, -1, 2], int(src[-1, -1, 0]))

            # 16-bit RGB565 check
            fb16 = np.zeros((fb_h, fb_w * 2), dtype=np.uint8)
            hdmi_host._blit_rgb24_into_fb_buf_dirty(
                fb16, src.tobytes(), src_w, src_h, fb_w, fb_h, 16,
                red_off=11, green_off=5, blue_off=0, scale_mode="fit", fast_scale=True,
            )
            out16 = fb16.view("<u2").reshape((fb_h, fb_w))
            exp0 = ((int(src[0, 0, 0]) >> 3) << 11) | ((int(src[0, 0, 1]) >> 2) << 5) | (int(src[0, 0, 2]) >> 3)
            self.assertEqual(int(out16[0, 0]), exp0)
            self.assertEqual(int(out16[factor - 1, factor - 1]), exp0)


class TestAudioManager(unittest.TestCase):
    def test_parsers_and_device_classification(self):
        import rj_audio as aud

        sinks_txt = (
            "Sink #47\n"
            "\tState: SUSPENDED\n"
            "\tName: alsa_output.platform-fef00700.hdmi.hdmi-stereo\n"
            "\tDescription: Built-in Audio Digital Stereo (HDMI)\n"
            "\tMute: no\n"
            "\tVolume: front-left: 49152 /  75% / -7.50 dB,   front-right: 49152 /  75% / -7.50 dB\n"
            "\tActive Port: hdmi-output-0\n"
            "\n"
            "Sink #59\n"
            "\tState: RUNNING\n"
            "\tName: bluez_output.AA_BB_CC_DD_EE_FF.1\n"
            "\tDescription: Sony WH-1000XM5\n"
            "\tMute: yes\n"
            "\tVolume: front-left: 55705 /  85% / -4.23 dB\n"
            "\tActive Port: headset-output\n"
            "\n"
            "Sink #62\n"
            "\tState: IDLE\n"
            "\tName: alsa_output.usb-FiiO_USB_DAC-00.analog-stereo\n"
            "\tDescription: FiiO USB DAC Analog Stereo\n"
            "\tMute: no\n"
            "\tVolume: front-left: 65536 / 100% / 0.00 dB\n"
        )
        sinks = aud.parse_pactl_sinks_detailed(sinks_txt, default_sink="bluez_output.AA_BB_CC_DD_EE_FF.1")
        self.assertEqual(len(sinks), 3)
        self.assertEqual(sinks[0]["kind"], "hdmi")
        self.assertEqual(sinks[0]["volume_pct"], 75)
        self.assertFalse(sinks[0]["is_default"])
        self.assertEqual(sinks[1]["kind"], "bluetooth")
        self.assertTrue(sinks[1]["is_bluetooth"])
        self.assertTrue(sinks[1]["is_default"])
        self.assertTrue(sinks[1]["muted"])
        self.assertEqual(sinks[1]["volume_pct"], 85)
        self.assertEqual(sinks[2]["kind"], "usb")
        self.assertEqual(sinks[2]["volume_pct"], 100)

        sources_txt = (
            "Source #48\n"
            "\tState: IDLE\n"
            "\tName: alsa_output.platform-fef00700.hdmi.hdmi-stereo.monitor\n"
            "\tDescription: Monitor of Built-in Audio Digital Stereo (HDMI)\n"
            "\tMute: no\n"
            "\tVolume: front-left: 65536 / 100% / 0.00 dB\n"
            "\n"
            "Source #63\n"
            "\tState: RUNNING\n"
            "\tName: alsa_input.usb-Blue_Microphones_Yeti-00.mono-fallback\n"
            "\tDescription: Yeti Stereo Microphone\n"
            "\tMute: no\n"
            "\tVolume: mono: 58982 /  90% / -2.75 dB\n"
        )
        sources = aud.parse_pactl_sources_detailed(
            sources_txt, default_source="alsa_input.usb-Blue_Microphones_Yeti-00.mono-fallback"
        )
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["label"], "Yeti Stereo Microphone")
        self.assertEqual(sources[0]["kind"], "usb")
        self.assertEqual(sources[0]["volume_pct"], 90)
        self.assertTrue(sources[0]["is_default"])

        streams_txt = (
            "Sink Input #114\n"
            "\tDriver: PipeWire\n"
            "\tSink: 59\n"
            "\tCorked: no\n"
            "\tMute: no\n"
            "\tVolume: front-left: 52428 /  80% / -5.81 dB\n"
            "\tProperties:\n"
            "\t\tmedia.name = \"YouTube - Lo-Fi Beats\"\n"
            "\t\tapplication.name = \"Firefox\"\n"
            "\n"
            "Sink Input #115\n"
            "\tSink: 47\n"
            "\tCorked: yes\n"
            "\tMute: yes\n"
            "\tVolume: front-left: 32768 /  50% / -18.06 dB\n"
            "\tProperties:\n"
            "\t\tapplication.name = \"EmberJack YT Music\"\n"
        )
        streams = aud.parse_pactl_sink_inputs(streams_txt)
        self.assertEqual(len(streams), 2)
        self.assertEqual(streams[0]["id"], "114")
        self.assertIn("Firefox", streams[0]["label"])
        self.assertEqual(streams[0]["volume_pct"], 80)
        self.assertFalse(streams[0]["corked"])
        self.assertEqual(streams[1]["id"], "115")
        self.assertTrue(streams[1]["corked"])
        self.assertTrue(streams[1]["muted"])

        cards_txt = (
            "Card #44\n"
            "\tName: bluez_card.AA_BB_CC_DD_EE_FF\n"
            "\tProperties:\n"
            "\t\tdevice.description = \"Sony WH-1000XM5\"\n"
            "\tProfiles:\n"
            "\t\ta2dp-sink: High Fidelity Playback (A2DP Sink) (sinks: 1, sources: 0, priority: 18, available: yes)\n"
            "\t\theadset-head-unit: Headset Head Unit (HSP/HFP) (sinks: 1, sources: 1, priority: 2, available: yes)\n"
            "\t\ta2dp-sink-ldac: LDAC (sinks: 1, sources: 0, priority: 20, available: no)\n"
            "\tActive Profile: a2dp-sink\n"
        )
        cards = aud.parse_pactl_cards(cards_txt)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["label"], "BT: Sony WH-1000XM5")
        self.assertEqual(cards[0]["active_profile"], "a2dp-sink")
        prof_names = [p["name"] for p in cards[0]["profiles"]]
        self.assertIn("a2dp-sink", prof_names)
        self.assertIn("headset-head-unit", prof_names)
        self.assertNotIn("a2dp-sink-ldac", prof_names)

        wp_txt = (
            "Audio\n"
            " ├─ Sinks:\n"
            " │  *   47. HDMI Stereo [vol: 0.70]\n"
            " ├─ Sources:\n"
            " │  *   61. USB Mic Pro [vol: 0.95 MUTED]\n"
            "Video\n"
        )
        wp_mics = aud.parse_wpctl_sources(wp_txt)
        self.assertEqual(len(wp_mics), 1)
        self.assertEqual(wp_mics[0]["volume_pct"], 95)
        self.assertTrue(wp_mics[0]["muted"])
        self.assertTrue(wp_mics[0]["is_default"])

        arec_txt = (
            "**** List of CAPTURE Hardware Devices ****\n"
            "card 2: Mic [USB PnP Sound Device], device 0: USB Audio [USB Audio]\n"
        )
        alsa_mics = aud.parse_alsa_capture_devices(arec_txt)
        self.assertEqual(len(alsa_mics), 1)
        self.assertEqual(alsa_mics[0]["alsa_dev"], "plughw:2,0")
        self.assertEqual(alsa_mics[0]["kind"], "usb")

    def test_audio_manager_ui_renders_all_tabs_and_main_menu_wired(self):
        from payloads.utilities import audio_manager as am_ui
        import raspyjack
        import rj_guide
        import rj_tool_catalog

        sample_outputs = [
            {"id": "59", "name": "bluez_output.AA", "label": "Sony WH-1000XM5", "kind": "bluetooth", "volume_pct": 80, "muted": False, "is_default": True, "backend": "pactl", "state": "RUNNING", "active_port": "headset-output"},
            {"id": "47", "name": "alsa_output.hdmi0", "label": "HDMI 0 (vc4-hdmi-0)", "kind": "hdmi", "volume_pct": 65, "muted": False, "is_default": False, "backend": "pactl", "state": "SUSPENDED", "active_port": "hdmi-output-0"},
            {"id": "62", "name": "alsa_output.jack", "label": "3.5mm Headphones", "kind": "jack", "volume_pct": 50, "muted": True, "is_default": False, "backend": "pactl", "state": "IDLE"},
        ]
        sample_inputs = [
            {"id": "63", "name": "alsa_input.usb_mic", "label": "USB PnP Microphone", "kind": "usb", "volume_pct": 90, "muted": False, "is_default": True, "backend": "pactl", "state": "RUNNING"},
        ]
        sample_streams = [
            {"id": "114", "name": "114", "label": "Firefox - Lo-Fi Beats", "kind": "stream", "volume_pct": 75, "muted": False, "corked": False, "backend": "pactl", "state": "PLAYING"},
            {"id": "115", "name": "115", "label": "EmberJack YT Music", "kind": "stream", "volume_pct": 100, "muted": True, "corked": True, "backend": "pactl", "state": "PAUSED"},
        ]
        bk = {"backend": "PipeWire", "has_pactl": True, "has_wpctl": True, "user": "pi", "uid": 1000}

        for w, h in ((640, 360), (320, 170), (240, 240), (128, 128)):
            for t_i in range(4):
                im = am_ui.render_audio_manager_frame(
                    w, h, tab_idx=t_i, outputs=sample_outputs, inputs=sample_inputs, streams=sample_streams,
                    cursor=0, scroll=0, backend_info=bk, toast="Output set: Sony WH-1000XM5", toast_kind="ok",
                )
                self.assertEqual(im.size, (w, h))
            # Empty states + modal overlay
            im_empty = am_ui.render_audio_manager_frame(
                w, h, tab_idx=2, outputs=[], inputs=[], streams=[], cursor=0, scroll=0, backend_info=bk,
            )
            self.assertEqual(im_empty.size, (w, h))
            im_modal = am_ui.render_audio_manager_frame(
                w, h, tab_idx=3, outputs=sample_outputs, inputs=sample_inputs, streams=sample_streams,
                cursor=3, scroll=0, backend_info=bk,
                overlay={"title": "PROFILE: Sony WH-1000XM5", "items": [("* High Fidelity (A2DP)", "a2dp"), ("  Headset (HSP/HFP)", "hfp")], "sel": 0},
            )
            self.assertEqual(im_modal.size, (w, h))

        # Main menu, icons, descriptions, catalog & guide wiring
        main_labels = [entry[0] for entry in raspyjack.m.menu["a"]]
        self.assertIn(" Audio", main_labels)
        self.assertTrue(bool(raspyjack._menu_icon_for_label(" Audio")))
        self.assertIn(" Audio", raspyjack.MENU_DESCRIPTIONS)
        util_ids = {e["id"] for e in rj_tool_catalog.TOOL_CATALOG["utilities"]}
        self.assertIn("audio_manager", util_ids)
        self.assertTrue(any(t["id"] == "audio_manager" for t in rj_guide.filter_topics(rj_guide.get_all_topics(), query="pavucontrol")))


class TestAudioFixesAndYTMusicYouTubeUpgrades(unittest.TestCase):
    def test_audio_low_latency_pause_and_bluetooth_a2dp_selection(self):
        import rj_bluetooth as bt
        import rj_ytmusic as ytm
        from payloads import _audio_helper as ah

        # 1. Bluetooth A2DP selector ALWAYS picks high-fidelity A2DP Stereo, NEVER HFP/HSP mono!
        profs = ["off", "headset-head-unit", "headset-head-unit-cvsd", "a2dp-sink-sbc", "a2dp-sink-sbc_xq", "a2dp-sink-aac"]
        self.assertEqual(bt.select_best_a2dp_profile(profs), "a2dp-sink-aac")
        self.assertEqual(bt.select_best_a2dp_profile(["headset-head-unit", "a2dp-sink", "a2dp-sink-sbc_xq"]), "a2dp-sink")
        self.assertIsNone(bt.select_best_a2dp_profile(["off", "headset-head-unit"]))

        # Verify HDMI installer never installs conflicting bluez-alsa-utils alongside PipeWire
        inst_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "install_raspyjack.sh")
        with open(inst_path, "r", encoding="utf-8") as fh:
            inst_txt = fh.read()
        for ln in inst_txt.splitlines():
            if "pipewire-audio" in ln and "PACKAGES+=" in ln:
                self.assertNotIn("bluez-alsa-utils", ln)
        self.assertIn("bluetooth.autoswitch-to-headset-profile = false", inst_txt)

        cards_raw = (
            "Card #44\n"
            "\tName: bluez_card.AA_BB_CC_DD_EE_FF\n"
            "\tProfiles:\n"
            "\t\theadset-head-unit: Headset (sinks: 1, sources: 1, available: yes)\n"
            "\t\ta2dp-sink: High Fidelity Playback (sinks: 1, sources: 0, available: yes)\n"
            "\t\ta2dp-sink-ldac: LDAC (sinks: 1, sources: 0, available: no)\n"
            "\tActive Profile: headset-head-unit\n"
        )
        bt_cards = bt._parse_bluez_card_profiles(cards_raw)
        self.assertEqual(len(bt_cards), 1)
        self.assertEqual(bt_cards[0]["active_profile"], "headset-head-unit")
        self.assertIn("a2dp-sink", bt_cards[0]["profiles"])
        self.assertNotIn("a2dp-sink-ldac", bt_cards[0]["profiles"])

        # 2. PCM sink commands use <=80ms latency (fixes 2-second pause delay!)
        sink_cmd = ah.get_pcm_sink_cmd(48000, 2)
        self.assertIsInstance(sink_cmd, list)
        joined = " ".join(sink_cmd)
        self.assertTrue(
            ("--latency-msec=80" in joined) or ("--latency=80ms" in joined) or ("--buffer-time=80000" in joined),
            f"Expected low-latency flag in PCM sink command, got: {sink_cmd}",
        )

        # 3. Instant pause tears down sink_proc immediately & reopen_audio_sink flags clean reopen
        class _DummyProc:
            def __init__(self):
                self.terminated = False
                self.stdout = object()
            def poll(self):
                return None if not self.terminated else 0
            def terminate(self):
                self.terminated = True
            def wait(self, timeout=None):
                return 0

        p = ytm.YTMusicPlayer()
        try:
            d_ff = _DummyProc()
            d_sink = _DummyProc()
            p.proc = d_ff
            p.sink_proc = d_sink
            p.state = "playing"
            p._unpaused.set()
            p.toggle_pause()
            self.assertTrue(d_sink.terminated, "Pause must immediately close PCM sink to eliminate 2s drain")
            self.assertIsNone(p.sink_proc)
            self.assertEqual(p.state, "paused")
            p.toggle_pause()
            self.assertEqual(p.state, "playing")
            self.assertTrue(p._unpaused.is_set())

            d_sink2 = _DummyProc()
            p.sink_proc = d_sink2
            p.reopen_audio_sink()
            self.assertTrue(d_sink2.terminated)
            self.assertTrue(p._sink_reopen_requested)
        finally:
            p.proc = None
            p.sink_proc = None
            p.close()

    def test_ytmusic_real_playlist_names_offline_org_batch_dl_dsp_and_ui(self):
        import tempfile
        import threading
        from unittest import mock
        import rj_ytmusic as ytm
        from payloads.utilities import youtube_music as ytm_ui

        # Real playlist title extraction
        self.assertEqual(
            ytm.extract_playlist_title_from_entries([{"id": "a", "playlist_title": "Album - Outrun Nights"}]),
            "Outrun Nights",
        )
        self.assertEqual(
            ytm.extract_playlist_title_from_entries([{"id": "a", "playlist": "Chill Coding Beats"}]),
            "Chill Coding Beats",
        )

        # DSP filter chain (EQ + Speed + Loudnorm) + PCM fade gain
        af = ytm.build_audio_filter_chain("bass=g=5:f=95:w=0.6,aresample=async=1", speed=1.25, loudnorm=True)
        self.assertIn("dynaudnorm=", af)
        self.assertIn("atempo=1.25", af)
        self.assertIn("bass=g=5", af)
        self.assertEqual(ytm._scale_pcm_s16le(b"\x10\x20\x30\x40", 0.0), b"\x00\x00\x00\x00")

        old_base = os.environ.get("RJ_BASE_DIR")
        with tempfile.TemporaryDirectory() as td:
            os.environ["RJ_BASE_DIR"] = td
            try:
                t1 = {"id": "vid11111111", "title": "Nightcall", "artist": "Kavinsky", "duration": 258, "thumb": ""}
                t2 = {"id": "vid22222222", "title": "Sunset", "artist": "The Midnight", "duration": 276, "thumb": ""}
                t3 = {"id": "vid33333333", "title": "Resonance", "artist": "Home", "duration": 212, "thumb": ""}

                pl = ytm.create_playlist("Synthwave Essentials", [t1, t2], source_url="https://music.youtube.com/playlist?list=PLTEST123456")
                self.assertEqual(pl["name"], "Synthwave Essentials")
                self.assertEqual(ytm.add_tracks_to_playlist(pl["id"], [t2, t3]), 1)  # deduplicates t2
                self.assertEqual(ytm.move_playlist_track(pl["id"], 0, 1), 1)
                self.assertTrue(ytm.sort_playlist_tracks(pl["id"], mode="title"))

                # Batch playlist download into Library/<PlaylistName>/ (mocked single-track downloader)
                def _fake_dl(track, on_progress=None, target_dir=None, folder_name="", cancel_event=None, audio_format="mp3"):
                    os.makedirs(target_dir, exist_ok=True)
                    fp = os.path.join(target_dir, f"{track['artist']} - {track['title']}.mp3")
                    with open(fp, "wb") as h:
                        h.write(b"ID3" + b"\x00" * 128)
                    ytm.register_offline_file(track, fp, folder=folder_name)
                    track["path"] = fp
                    if on_progress:
                        on_progress(1.0)
                    return True, "Saved"

                with mock.patch.object(ytm, "download_track_offline", side_effect=_fake_dl):
                    res = ytm.download_playlist_offline(
                        [t1, t2, t3], playlist_name="Synthwave Essentials", playlist_id=pl["id"], cancel_event=threading.Event()
                    )
                self.assertEqual(res["downloaded"], 3)
                self.assertEqual(res["skipped"], 0)
                self.assertTrue(os.path.isdir(os.path.join(ytm.get_library_dir(), "Synthwave Essentials")))
                self.assertTrue(os.path.isfile(os.path.join(ytm.get_library_dir(), "Synthwave Essentials.m3u")))

                # Re-running batch download skips all 3 already-offline files!
                res2 = ytm.download_playlist_offline([t1, t2, t3], playlist_name="Synthwave Essentials", playlist_id=pl["id"])
                self.assertEqual(res2["skipped"], 3)
                self.assertEqual(res2["downloaded"], 0)

                # Smart offline-first lookup + offline folders + physical folder rename & move
                lp1 = ytm.find_local_path_for_track({"id": "vid11111111", "title": "Nightcall", "artist": "Kavinsky"})
                self.assertTrue(lp1 and os.path.isfile(lp1))
                flds = ytm.list_offline_folders()
                self.assertEqual(flds[0]["name"], "Synthwave Essentials")
                self.assertEqual(flds[0]["count"], 3)

                ok_rn, _ = ytm.rename_playlist(pl["id"], "Night Drive Mix")
                self.assertTrue(ok_rn)
                self.assertTrue(os.path.isdir(os.path.join(ytm.get_library_dir(), "Night Drive Mix")))
                off_in_fld = ytm.list_offline_library(folder="Night Drive Mix")
                self.assertEqual(len(off_in_fld), 3)

                ok_mv, _, new_p = ytm.move_offline_track_to_folder(off_in_fld[0]["path"], "Favorites")
                self.assertTrue(ok_mv and os.path.isfile(new_p))
                self.assertEqual(len(ytm.list_offline_library(folder="Favorites")), 1)

                # Listening stats + sleep timer + UI render across all visualizers & batch download modal
                ytm.record_track_play(t1, played_sec=120)
                ytm.record_track_play(t1, played_sec=120)
                ytm.record_track_play(t2, played_sec=60)
                top = ytm.get_most_played_tracks()
                self.assertEqual(top[0]["id"], "vid11111111")
                self.assertEqual(top[0]["plays"], 2)

                p = ytm.YTMusicPlayer()
                try:
                    self.assertIn("15m", p.set_sleep_timer(15))
                    self.assertTrue(p.get_sleep_label().startswith("SLP:"))
                    self.assertIn("End", p.set_sleep_timer(-1))
                    self.assertEqual(p.get_sleep_label(), "SLP:END")
                    p.set_sleep_timer(0)
                    self.assertEqual(p.get_sleep_label(), "")
                    self.assertEqual(p.cycle_speed(1), 1.25)
                    self.assertTrue(p.toggle_loudnorm())
                finally:
                    p.close()

                for viz in ytm.VISUALIZER_MODES:
                    p_st = {
                        "track": t1, "state": "playing", "elapsed": 42.0, "spectrum": [0.4] * 16,
                        "peaks": [0.7] * 16, "visualizer": viz, "queue": [t1, t2], "current_idx": 0,
                        "volume_pct": 80, "muted": False, "eq_name": "Warm Ember", "speed": 1.25,
                        "loudnorm": True, "sleep_label": "SLP:15m", "shuffle": True, "repeat_mode": "all",
                        "auto_radio": True, "liked": True, "offline_now": True, "audio_label": "BT: Sony XM5",
                    }
                    for w, h in ((640, 360), (320, 170), (128, 128)):
                        self.assertEqual(ytm_ui.render_ytmusic_frame(w, h, "now", p_st, {}).size, (w, h))
                self.assertEqual(
                    ytm_ui.render_ytmusic_frame(
                        640, 360, "offline", p_st,
                        {"items": off_in_fld, "cursor": 0, "scroll": 0, "banner": "FOLDER: ALL • SORT: NEWEST (3) • 1 MARKED", "selected_keys": {off_in_fld[0]["path"]}},
                        overlay={"kind": "batch_busy", "title": "DOWNLOADING: Night Drive Mix", "sub": "Track 2/3: Sunset", "item_prog": 0.65, "overall_prog": 0.55, "overall_txt": "Saved/Done: 1/3 • Skipped: 0 • Errors: 0"},
                    ).size,
                    (640, 360),
                )
            finally:
                if old_base is None:
                    os.environ.pop("RJ_BASE_DIR", None)
                else:
                    os.environ["RJ_BASE_DIR"] = old_base

    def test_youtube_video_playlists_subs_chapters_captions_speed_filters_and_ui(self):
        import tempfile
        from payloads.utilities import youtube as yt

        # 1. Home menu has Subscriptions & Playlists & Later, and NO Watch/Listen History!
        home_keys = [k for _, k, _ in yt.HOME_MENU_ITEMS]
        self.assertEqual(len(home_keys), 8)
        self.assertIn("subs", home_keys)
        self.assertIn("playlists", home_keys)
        self.assertNotIn("history", home_keys)
        self.assertNotIn("watch_history", home_keys)

        # 2. Real playlist title extraction, Chapters & WebVTT Captions
        self.assertEqual(yt.extract_playlist_title([{"playlist_title": "Linux Kernel Deep Dives"}]), "Linux Kernel Deep Dives")
        chaps = [
            {"start": 0.0, "end": 65.0, "title": "Intro"},
            {"start": 65.0, "end": 180.0, "title": "Hardware Setup"},
            {"start": 180.0, "end": 300.0, "title": "Live Demo"},
        ]
        self.assertEqual(yt.get_active_chapter(chaps, 90.0)["title"], "Hardware Setup")
        self.assertEqual(yt.get_adjacent_chapter_time(chaps, 90.0, 1), 180.0)
        self.assertEqual(yt.get_adjacent_chapter_time(chaps, 90.0, -1), 65.0)

        vtt = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:04.500\n"
            "<c>Welcome to</c> <i>EmberJack</i>!\n\n"
            "00:00:05.000 --> 00:00:08.000\n"
            "Testing &amp; Captions\n"
        )
        cues = yt.parse_vtt_subtitles(vtt)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0][2], "Welcome to EmberJack!")
        self.assertEqual(yt.get_active_subtitle(cues, 2.0), "Welcome to EmberJack!")
        self.assertEqual(yt.get_active_subtitle(cues, 6.0), "Testing & Captions")

        # 3. Search filters & Speed ffmpeg command
        sample = [
            {"id": "a", "title": "Short", "duration": 120, "views": 5000},
            {"id": "b", "title": "Med", "duration": 600, "views": 90000},
            {"id": "c", "title": "Long", "duration": 2400, "views": 12000},
        ]
        self.assertEqual([x["id"] for x in yt.apply_search_filters(sample, sort_by="views")], ["b", "c", "a"])
        self.assertEqual([x["id"] for x in yt.apply_search_filters(sample, duration_filter="short")], ["a"])
        self.assertEqual([x["id"] for x in yt.apply_search_filters(sample, duration_filter="long")], ["c"])

        cmd_spd = yt.build_video_ffmpeg_cmd(
            "http://example.com/v.mp4", "", 640, 360, "rgb24", 24, ["-f", "null", "-"], playback_speed=1.5
        )
        self.assertIn("setpts=PTS/1.500", " ".join(cmd_spd))
        self.assertIn("atempo=1.500", " ".join(cmd_spd))

        # 4. Persistence: Watch Later, Video Playlists, Silent Auto-Resume, Subscriptions & Download Folders
        old_base = os.environ.get("RJ_BASE_DIR")
        with tempfile.TemporaryDirectory() as td:
            os.environ["RJ_BASE_DIR"] = td
            try:
                vid = {"id": "abc12345678", "title": "Pi 5 Benchmark", "channel": "Jeff Geerling", "channel_url": "https://www.youtube.com/@JeffGeerling", "duration": 600, "views": 500000}
                self.assertTrue(yt.toggle_watch_later(vid))
                self.assertTrue(yt.is_in_watch_later("abc12345678"))
                pl = yt.create_video_playlist("Pi Builds", [vid], source_url="https://www.youtube.com/playlist?list=PLPI12345678")
                self.assertTrue(yt.rename_video_playlist(pl["id"], "Raspberry Pi Lab"))
                self.assertTrue(any(p["name"] == "Raspberry Pi Lab" for p in yt.load_video_playlists()))

                yt.save_resume_position("abc12345678", 145.0, 600)
                self.assertEqual(yt.get_resume_position("abc12345678"), 145.0)
                yt.save_resume_position("abc12345678", 580.0, 600)  # >=92% clears resume
                self.assertEqual(yt.get_resume_position("abc12345678"), 0.0)

                self.assertTrue(yt.toggle_subscription("Jeff Geerling", "https://www.youtube.com/@JeffGeerling"))
                self.assertTrue(yt.is_subscribed("Jeff Geerling"))

                dl_dir = yt.get_downloads_dir()
                os.makedirs(dl_dir, exist_ok=True)
                fp = os.path.join(dl_dir, "demo.mp4")
                with open(fp, "wb") as h:
                    h.write(b"\x00" * 1024)
                ok_m, _, new_fp = yt.move_download_to_folder(fp, "Pi Builds")
                self.assertTrue(ok_m and os.path.isfile(new_fp))
                self.assertEqual(yt.list_download_folders()[0]["name"], "Pi Builds")
                self.assertEqual(len(yt.list_downloads(folder="Pi Builds", kind_filter="video")), 1)

                # 5. Render upgraded YouTube screens across resolutions
                base_frame = Image.new("RGB", (640, 360), (24, 30, 42))
                for w, h in ((640, 360), (320, 170), (128, 128)):
                    self.assertEqual(yt.render_home_menu(w, h, 2, subs_count=4, playlists_count=3, liked_count=12, downloads_count=7).size, (w, h))
                    self.assertEqual(yt.render_results_view(w, h, "RESULTS", "pi", [vid], 0, 0, resume_map={"abc12345678": {"pos": 180, "duration": 600}}, watch_later_ids={"abc12345678"}, filter_badge="VIEWS").size, (w, h))
                    self.assertEqual(yt.render_video_info_card(w, h, vid, {"description": "Full hardware test.\nLine 2.", "chapters": chaps, "upload_date": "20261120"}, is_sub=True, in_wl=True, liked=True).size, (w, h))
                osd = yt.render_video_osd(base_frame, "Pi 5 Benchmark", 90.0, 300, 80, chapters=chaps, speed=1.5, loop_one=True, captions_on=True)
                sub_im = yt.render_subtitle_overlay(osd, "Welcome to EmberJack!", osd_visible=True)
                self.assertEqual(sub_im.size, (640, 360))
            finally:
                if old_base is None:
                    os.environ.pop("RJ_BASE_DIR", None)
                else:
                    os.environ["RJ_BASE_DIR"] = old_base

    def test_youtube_long_video_range_proxy_moov_seek_installer_and_guide(self):
        import http.server
        import shutil
        import subprocess
        import tempfile
        import threading
        import urllib.request
        import rj_guide
        from payloads.utilities import youtube as yt

        # 1. Decision matrix: long videos (>15 min), unknown duration, mid-video seeks,
        #    low /dev/shm, and preload_sec=0 route through the O(1)-RAM Range proxy!
        http_v = "https://rr1---sn-example.googlevideo.com/videoplayback?id=1"
        http_a = "https://rr1---sn-example.googlevideo.com/videoplayback?id=2"
        self.assertFalse(yt.should_use_range_proxy(300, 0.0, 5, 1024 * 1024 * 1024, http_v, http_a))
        self.assertTrue(yt.should_use_range_proxy(3600, 0.0, 5, 1024 * 1024 * 1024, http_v, http_a))
        self.assertTrue(yt.should_use_range_proxy(3600, 0.0, -1, 1024 * 1024 * 1024, http_v, http_a))
        self.assertTrue(yt.should_use_range_proxy(0, 0.0, 5, 1024 * 1024 * 1024, http_v, http_a))
        self.assertTrue(yt.should_use_range_proxy(300, 120.0, 5, 1024 * 1024 * 1024, http_v, http_a))
        self.assertTrue(yt.should_use_range_proxy(300, 0.0, 5, 100 * 1024 * 1024, http_v, http_a))
        self.assertTrue(yt.should_use_range_proxy(300, 0.0, 0, 1024 * 1024 * 1024, http_v, http_a))
        self.assertFalse(yt.should_use_range_proxy(3600, 0.0, 5, 1024 * 1024 * 1024, "/tmp/local.mp4", ""))
        self.assertFalse(yt.should_use_range_proxy(-1, 0.0, 5, 1024 * 1024 * 1024, http_v, ""))
        self.assertFalse(yt.should_use_range_proxy(3600, 0.0, 5, 1024 * 1024 * 1024, "https://manifest.googlevideo.com/x.m3u8", ""))

        # Range header parser covers open-ended, bounded, suffix, and no-range
        self.assertEqual(yt._parse_http_range_header("bytes=100-", 1000), (100, 999, True))
        self.assertEqual(yt._parse_http_range_header("bytes=10-50", 1000), (10, 50, True))
        self.assertEqual(yt._parse_http_range_header("bytes=-256", 1000), (744, 999, True))
        self.assertEqual(yt._parse_http_range_header("", 1000), (0, 999, False))

        # 2. Live _ChunkedRangeProxy end-to-end test: bounded upstream chunks + end-of-file moov + ffmpeg seek
        with tempfile.TemporaryDirectory() as td:
            # Build a non-faststart MP4 (moov atom at the VERY END of the file!) if ffmpeg is present
            has_ff = bool(shutil.which("ffmpeg"))
            if has_ff:
                v_src = os.path.join(td, "v_tail_moov.mp4")
                a_src = os.path.join(td, "a_tail_moov.m4a")
                r1 = subprocess.run(
                    [
                        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=3",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", v_src,
                    ],
                    capture_output=True, timeout=20,
                )
                r2 = subprocess.run(
                    [
                        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                        "-c:a", "aac", "-b:a", "96k", a_src,
                    ],
                    capture_output=True, timeout=20,
                )
                self.assertEqual(r1.returncode, 0, r1.stderr.decode("utf-8", "ignore"))
                self.assertEqual(r2.returncode, 0, r2.stderr.decode("utf-8", "ignore"))
                with open(v_src, "rb") as fh:
                    v_blob = fh.read()
                with open(a_src, "rb") as fh:
                    a_blob = fh.read()
            else:
                v_blob = bytes((i * 31) & 0xFF for i in range(96 * 1024))
                a_blob = bytes((i * 17) & 0xFF for i in range(48 * 1024))

            blobs = {"/video.mp4": v_blob, "/audio.m4a": a_blob}
            upstream_spans = []

            class _UpHandler(http.server.BaseHTTPRequestHandler):
                def log_message(self, fmt, *args):
                    pass

                def do_GET(self):
                    data = blobs.get(self.path)
                    if data is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    total = len(data)
                    rng = self.headers.get("Range", "")
                    if rng.startswith("bytes="):
                        lo_s, hi_s = rng[6:].split(",")[0].strip().split("-", 1)
                        lo = int(lo_s) if lo_s else 0
                        hi = min(total - 1, int(hi_s)) if hi_s else (total - 1)
                        if lo >= total:
                            self.send_response(416)
                            self.send_header("Content-Range", f"bytes */{total}")
                            self.end_headers()
                            return
                        chunk = data[lo : hi + 1]
                        upstream_spans.append((self.path, lo, hi, len(chunk)))
                        self.send_response(206)
                        self.send_header("Content-Type", "video/mp4" if self.path.endswith(".mp4") else "audio/mp4")
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Content-Range", f"bytes {lo}-{hi}/{total}")
                        self.send_header("Content-Length", str(len(chunk)))
                        self.end_headers()
                        self.wfile.write(chunk)
                    else:
                        self.send_response(200)
                        self.send_header("Content-Length", str(total))
                        self.end_headers()
                        self.wfile.write(data)

            up_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _UpHandler)
            up_port = up_srv.server_address[1]
            threading.Thread(target=up_srv.serve_forever, daemon=True).start()
            proxy = yt._ChunkedRangeProxy(
                video_url=f"http://127.0.0.1:{up_port}/video.mp4",
                audio_url=f"http://127.0.0.1:{up_port}/audio.m4a",
                chunk_size=16 * 1024,
                prefetch_chunks=2,
            ).start()
            try:
                # HEAD reports exact Content-Length + Accept-Ranges: bytes
                req_h = urllib.request.Request(proxy.video_proxy_url, method="HEAD")
                with urllib.request.urlopen(req_h, timeout=5) as rh:
                    self.assertEqual(rh.status, 200)
                    self.assertEqual(int(rh.headers.get("Content-Length", "0")), len(v_blob))
                    self.assertEqual(rh.headers.get("Accept-Ranges"), "bytes")

                # Unbounded GET returns exact payload while splitting upstream into <=16 KB Range chunks
                with urllib.request.urlopen(proxy.video_proxy_url, timeout=8) as rv:
                    got_full = rv.read()
                self.assertEqual(got_full, v_blob)
                v_spans = [s for s in upstream_spans if s[0] == "/video.mp4" and s[3] > 1]
                self.assertGreaterEqual(len(v_spans), 2)
                self.assertTrue(all(s[3] <= 16 * 1024 for s in v_spans))

                # End-of-file suffix Range (moov seek) + mid-file Range return exact slices
                req_tail = urllib.request.Request(proxy.video_proxy_url, headers={"Range": "bytes=-1024"})
                with urllib.request.urlopen(req_tail, timeout=5) as rt:
                    self.assertEqual(rt.status, 206)
                    self.assertEqual(rt.read(), v_blob[-1024:])

                req_mid = urllib.request.Request(proxy.video_proxy_url, headers={"Range": "bytes=1000-2023"})
                with urllib.request.urlopen(req_mid, timeout=5) as rm:
                    self.assertEqual(rm.status, 206)
                    self.assertEqual(rm.read(), v_blob[1000:2024])

                # Live ffmpeg decode + mid-video seek on non-faststart MP4/M4A through the proxy!
                if has_ff:
                    frame_bytes = 320 * 180 * 3
                    seek_cmd = yt.build_video_ffmpeg_cmd(
                        proxy.video_proxy_url, proxy.audio_proxy_url, 320, 180, "rgb24", 12,
                        ["-f", "null", "-"], start_offset=1.5, use_realtime=False, input_is_pipe=False,
                    )
                    p_seek = subprocess.Popen(
                        seek_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                    )
                    got_b = 0
                    try:
                        while True:
                            buf = p_seek.stdout.read(frame_bytes)
                            if not buf:
                                break
                            got_b += len(buf)
                    finally:
                        p_seek.stdout.close()
                    self.assertEqual(p_seek.wait(timeout=12), 0)
                    self.assertGreaterEqual(got_b // frame_bytes, 10)
            finally:
                proxy.stop()
                up_srv.shutdown()
                up_srv.server_close()

        # 3. Verify updated installer & User Guide cover long videos, yt-dlp, bluealsa, and core modules
        inst_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "install_raspyjack.sh")
        with open(inst_path, "r", encoding="utf-8") as fh:
            inst_txt = fh.read()
        for needle in ("yt-dlp[default]", "nodejs", "51-emberjack-bt-a2dp.conf", "rj_ytmusic", "rj_guide", "rj_terminal", "bluealsa"):
            self.assertIn(needle, inst_txt)

        all_topics = rj_guide.get_all_topics()
        self.assertTrue(any(t["id"] == "troubleshooting" for t in rj_guide.filter_topics(all_topics, query="bluealsa")))
        self.assertTrue(any(t["id"] == "youtube_video" for t in rj_guide.filter_topics(all_topics, query="range proxy")))
        self.assertTrue(any(t["id"] == "audio_manager" for t in rj_guide.filter_topics(all_topics, query="fix bt a2dp")))


class TestCodebaseUIModernization(unittest.TestCase):
    def test_legacy_color_remap_scaled_draw_and_chrome_primitives(self):
        import rj_theme
        from payloads._display_helper import ScaledDraw

        ET = rj_theme.EMBER_THEME
        # String & RGB tuple remapping preserves type and leaves native EMBER_THEME untouched
        self.assertEqual(rj_theme.remap_legacy_color("black"), ET["bg_base"])
        self.assertEqual(rj_theme.remap_legacy_color("#00ffff"), ET["accent_bright"])
        self.assertEqual(rj_theme.remap_legacy_color("lime"), ET["ok"])
        self.assertEqual(rj_theme.remap_legacy_color((0, 0, 0)), rj_theme.EMBER_RGB["bg_base"])
        self.assertEqual(rj_theme.remap_legacy_color((0, 255, 0)), rj_theme.EMBER_RGB["ok"])
        self.assertEqual(rj_theme.remap_legacy_color(ET["accent"]), ET["accent"])
        self.assertIsNone(rj_theme.remap_legacy_color(None))

        # ScaledDraw warms pure black canvases to Obsidian Ember and provides Pillow 10+ textsize shim
        im = Image.new("RGB", (128, 128), (0, 0, 0))
        sd = ScaledDraw(im)
        self.assertEqual(im.getpixel((0, 0)), rj_theme.EMBER_RGB["bg_base"])
        sd.rectangle((0, 0, 127, 14), fill="#001a1a")
        self.assertEqual(im.getpixel((2, 2)), rj_theme.EMBER_RGB["bg_surface"])
        tw, th = sd.textsize("EmberJack")
        self.assertGreater(tw, 0)
        self.assertGreater(th, 0)

        # Opt-out preserves raw black
        im_raw = Image.new("RGB", (64, 64), (0, 0, 0))
        _sd_raw = ScaledDraw(im_raw, remap_colors=False)
        self.assertEqual(im_raw.getpixel((0, 0)), (0, 0, 0))

        # High-level chrome primitives render cleanly across resolutions
        for w, h in ((640, 360), (320, 170), (240, 240), (128, 128)):
            canvas = Image.new("RGB", (w, h), ET["bg_base"])
            d = ImageDraw.Draw(canvas)
            fonts = rj_theme.load_app_fonts(w, h)
            hh = rj_theme.draw_app_header(d, w, "DEMO APP", fonts, badge="LIVE", right_text="HDMI")
            fh = rj_theme.draw_app_footer(d, w, h, "Arrows:Nav  Esc:Back", fonts, right_text="1/5")
            rj_theme.draw_status_card(d, w, h, "Ready", sub="All systems nominal", kind="ok", fonts=fonts)
            self.assertGreater(hh, 0)
            self.assertGreater(fh, 0)

    def test_interface_picker_wifi_manager_and_arcade_bezel_render_all_sizes(self):
        from payloads import _iface_helper as ifh
        from wifi import wifi_lcd_interface as wui
        from payloads.games import _arcade_frame as af

        sample_ifaces = [
            {"name": "wlan1", "driver": "rtl8812au", "is_onboard": False, "is_wifi": True, "is_up": True, "ip": "192.168.1.44", "supports_ap": True, "supports_monitor": True},
            {"name": "wlan0", "driver": "brcmfmac", "is_onboard": True, "is_wifi": True, "is_up": True, "ip": "192.168.1.20", "supports_ap": False, "supports_monitor": False},
            {"name": "eth0", "driver": "bcmgenet", "is_onboard": True, "is_wifi": False, "is_up": False, "ip": "", "supports_ap": False, "supports_monitor": False},
        ]
        sample_bt = [
            {"name": "hci0", "bus": "USB", "mac": "AA:BB:CC:DD:EE:FF", "is_up": True, "bt_version": "5.3"},
        ]
        sample_nets = [
            {"ssid": "EmberLab_5G", "bssid": "AA:BB:CC:11:22:33", "signal": 88, "encrypted": True, "security": "WPA2"},
            {"ssid": "GuestOpen", "bssid": "AA:BB:CC:44:55:66", "signal": 54, "encrypted": False, "security": ""},
        ]
        sample_profs = [
            {"ssid": "EmberLab_5G", "priority": 1, "interface": "auto", "auto_connect": True, "last_used": "2026-11-26"},
        ]
        game_sq = Image.new("RGB", (128, 128), (18, 22, 26))

        for w, h in ((640, 360), (320, 170), (240, 240), (128, 128)):
            self.assertEqual(ifh.render_interface_picker(w, h, sample_ifaces, sel=0, title="SELECT WIFI").size, (w, h))
            self.assertEqual(ifh.render_interface_picker(w, h, sample_bt, sel=0, title="SELECT BT", is_bt=True).size, (w, h))
            for scr in ("main", "scan", "profiles", "interface", "status", "message"):
                im_w = wui.render_wifi_screen(
                    w, h, scr, menu_index=0, active_iface="wlan0",
                    status={"status": "connected", "ssid": "EmberLab_5G", "ip": "192.168.1.20", "interface": "wlan0"},
                    scanned_networks=sample_nets, saved_profiles=sample_profs,
                    interfaces=["eth0", "wlan0", "wlan1"], wifi_interfaces=["wlan0", "wlan1"],
                    message="Connected!", message_kind="ok",
                )
                self.assertEqual(im_w.size, (w, h))
            im_a = af.compose_arcade_frame(
                w, h, game_sq, title="SNAKE", score=42, best=128,
                controls=[("Arrows", "Move"), ("Enter", "Start"), ("Esc", "Exit")], status="PLAYING",
            )
            self.assertEqual(im_a.size, (w, h))

    def test_zero_removed_pillow10_textsize_calls_and_webui_ember_branding(self):
        repo_root = os.path.dirname(os.path.dirname(__file__))
        hits = []
        for dp, _, fns in os.walk(os.path.join(repo_root, "payloads")):
            if "__pycache__" in dp:
                continue
            for fn in fns:
                if not fn.endswith(".py") or fn.startswith("_"):
                    continue
                fp = os.path.join(dp, fn)
                with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                    if ".textsize(" in fh.read():
                        hits.append(os.path.relpath(fp, repo_root))
        self.assertEqual(hits, [], f"Found deprecated Pillow 10+ .textsize() calls in: {hits}")

        import raspyjack
        self.assertTrue(callable(getattr(raspyjack, "_display_text_file", None)))

        for rel in ("web/index.html", "web/ide.html", "web/ide.js", "web/llm.html"):
            with open(os.path.join(repo_root, rel), "r", encoding="utf-8") as fh:
                txt = fh.read()
            self.assertIn("EmberJack", txt, f"{rel} should include EmberJack branding")


class TestFreshInstallPackagingAndPayloadErrors(unittest.TestCase):
    def test_rpi_router_not_gitignored_and_installer_verifies_hdmi_gpio(self):
        repo_root = os.path.dirname(os.path.dirname(__file__))

        with open(os.path.join(repo_root, ".gitignore"), "r", encoding="utf-8") as fh:
            ignore_lines = [ln.strip() for ln in fh.read().splitlines() if ln.strip() and not ln.strip().startswith("#")]
        self.assertNotIn("RPi/", ignore_lines, ".gitignore must NOT exclude RPi/ router package")
        self.assertNotIn("RPi", ignore_lines, ".gitignore must NOT exclude RPi router package")

        self.assertTrue(os.path.isfile(os.path.join(repo_root, "RPi", "__init__.py")))
        self.assertTrue(os.path.isfile(os.path.join(repo_root, "RPi", "GPIO.py")))

        # RPi.GPIO in HDMI mode must resolve cleanly to gpio_shim without Pi hardware
        import RPi.GPIO as rpi_gpio
        rpi_gpio.setmode(rpi_gpio.BCM)
        rpi_gpio.setup(16, rpi_gpio.IN, pull_up_down=rpi_gpio.PUD_UP)
        self.assertIn(rpi_gpio.input(16), (0, 1))

        # Installer must ensure RPi/GPIO.py exists across modes and test full submodule import
        with open(os.path.join(repo_root, "install_raspyjack.sh"), "r", encoding="utf-8") as fh:
            inst_txt = fh.read()
        self.assertIn('if [[ ! -f "$INSTALL_DIR/RPi/GPIO.py" ]]; then', inst_txt)
        self.assertIn("importlib.import_module(mod)", inst_txt)
        self.assertNotIn("importlib.import_module(mod.split('.')[0])", inst_txt)
        self.assertIn('"RPi.GPIO"', inst_txt)

    def test_extract_payload_error_tail_reads_only_new_traceback(self):
        import tempfile
        import raspyjack

        with tempfile.NamedTemporaryFile("wb", delete=False) as tf:
            old_part = b"Old previous run warning\nEverything succeeded earlier\n"
            tf.write(old_part)
            offset = len(old_part)
            tf.write(
                b"Traceback (most recent call last):\n"
                b'  File "/home/user/EmberJack/payloads/utilities/guide.py", line 25, in <module>\n'
                b"    import RPi.GPIO as GPIO\n"
                b"    ^^^^^^^^^^^^^^^^^^^^^^^\n"
                b"ModuleNotFoundError: No module named 'RPi'\n"
            )
            path = tf.name
        try:
            tail = raspyjack._extract_payload_error_tail(path, start_pos=offset, max_lines=2)
            self.assertIn("ModuleNotFoundError: No module named 'RPi'", tail)
            self.assertNotIn("Old previous run warning", tail)
            self.assertNotIn("Traceback (most recent call last):", tail)
            self.assertNotIn("^^^", tail)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_updater_obsidian_ember_uis_render_across_all_resolutions(self):
        from payloads.utilities import auto_update as au
        from payloads.utilities import payload_updater as pu

        sample_commits = [
            "a1b2c3d Fix missing RPi router shim on fresh installs",
            "d4e5f6a Upgrade updater UIs to Obsidian Ember",
            "9876543 Improve YouTube long-video range proxy",
        ]
        sample_backups = ["20261126_131000", "20261125_184500"]
        sample_changes = [
            ("A", "payloads/utilities/new_tool.py"),
            ("M", "payloads/utilities/auto_update.py"),
            ("D", "payloads/legacy/old_demo.py"),
        ]

        for w, h in ((640, 360), (320, 170), (240, 240), (128, 128)):
            self.assertEqual(au.render_home_frame(w, h, "a1b2c3d", "2026-11-26", "4820 MB", 2, is_git=True, selected_action=0).size, (w, h))
            self.assertEqual(au.render_home_frame(w, h, "1.4.2", "package (.deb)", "4820 MB", 0, is_git=False, selected_action=0).size, (w, h))
            self.assertEqual(au.render_progress_frame(w, h, "Installing update...", "Running install_raspyjack.sh", progress_pct=68, stage_idx=4, log_lines=["[STEP] Checking deps", "[OK] All core modules available"]).size, (w, h))
            self.assertEqual(au.render_changelog_frame(w, h, sample_commits, scroll_pos=0).size, (w, h))
            self.assertEqual(au.render_rollback_frame(w, h, sample_backups, sel=0).size, (w, h))
            self.assertEqual(au.render_rollback_frame(w, h, [], sel=0).size, (w, h))
            self.assertEqual(au.render_message_frame(w, h, ["Update Complete!", "Rebooting system..."], kind="ok").size, (w, h))
            err_im, _ms = au.render_error_frame(w, h, "UPDATE FAILED", "Could not reach origin/main while fetching remote refs", scroll=0)
            self.assertEqual(err_im.size, (w, h))

            self.assertEqual(pu.render_home_frame(w, h, "a1b2c3d", payloads_only=True).size, (w, h))
            self.assertEqual(pu.render_status_frame(w, h, "Fetching Updates...", "Checking origin/main", kind="info").size, (w, h))
            self.assertEqual(pu.render_changes_frame(w, h, sample_changes, sample_commits, 0, True, "a1b2c3d", "f0e1d2c").size, (w, h))
            self.assertEqual(pu.render_changes_frame(w, h, [], [], 0, True, "a1b2c3d", "a1b2c3d").size, (w, h))
            self.assertEqual(pu.render_progress_frame(w, h, 2, 3, "payloads/utilities/auto_update.py").size, (w, h))
            self.assertEqual(pu.render_result_frame(w, h, 3, 0, "f0e1d2c").size, (w, h))

    def test_youtube_preload_cache_dir_threshold_and_range_proxy_warm_buffer(self):
        import http.server
        import shutil
        import threading
        import urllib.request
        from unittest import mock
        from payloads.utilities import youtube as yt

        # 1. get_preload_cache_dir must NOT pick /dev/shm when free space is 220 MB (< 300 MB MIN_PRELOAD_FREE_BYTES),
        #    falling back to /tmp so 512 MB Raspberry Pis keep StreamPreloader enabled!
        fake_usage_small = shutil._ntuple_diskusage(256 * 1024 * 1024, 36 * 1024 * 1024, 220 * 1024 * 1024)
        fake_usage_big = shutil._ntuple_diskusage(16 * 1024**3, 2 * 1024**3, 14 * 1024**3)
        old_ov = os.environ.pop("RJ_YT_CACHE_DIR", None)
        try:
            def _fake_du(path):
                if str(path).startswith("/dev/shm"):
                    return fake_usage_small
                return fake_usage_big

            with mock.patch("payloads.utilities.youtube.shutil.disk_usage", side_effect=_fake_du):
                chosen = yt.get_preload_cache_dir()
                self.assertFalse(chosen.startswith("/dev/shm"), f"Should skip small /dev/shm, got {chosen}")
                self.assertTrue(os.access(chosen, os.W_OK | os.X_OK))
        finally:
            if old_ov is not None:
                os.environ["RJ_YT_CACHE_DIR"] = old_ov

        # 2. _ChunkedRangeProxy.start_warm_buffer pre-fetches head bytes into RAM and serves byte 0..N with 0 extra hits
        payload = bytes((i * 19) & 0xFF for i in range(512 * 1024))
        req_count = [0]

        class _WarmHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                req_count[0] += 1
                total = len(payload)
                rng = self.headers.get("Range", "")
                if rng.startswith("bytes="):
                    lo_s, hi_s = rng[6:].split("-", 1)
                    lo = int(lo_s) if lo_s else 0
                    hi = min(total - 1, int(hi_s)) if hi_s else (total - 1)
                    chunk = payload[lo : hi + 1]
                    self.send_response(206)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Range", f"bytes {lo}-{hi}/{total}")
                    self.send_header("Content-Length", str(len(chunk)))
                    self.end_headers()
                    self.wfile.write(chunk)
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", str(total))
                    self.end_headers()
                    self.wfile.write(payload)

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _WarmHandler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        proxy = yt._ChunkedRangeProxy(
            video_url=f"http://127.0.0.1:{port}/v.mp4",
            chunk_size=128 * 1024,
        ).start()
        try:
            proxy.start_warm_buffer(target_bytes=512 * 1024)
            self.assertTrue(proxy.warm_done.wait(timeout=5.0))
            self.assertFalse(proxy.warm_failed)
            self.assertEqual(proxy.warmed_bytes, len(payload))
            hits_after_warm = req_count[0]
            # Full read from byte 0 must be satisfied straight from RAM head cache with 0 new upstream requests!
            with urllib.request.urlopen(proxy.video_proxy_url, timeout=5) as resp:
                got = resp.read()
            self.assertEqual(got, payload)
            self.assertEqual(req_count[0], hits_after_warm)
        finally:
            proxy.stop()
            srv.shutdown()
            srv.server_close()

    def test_fast_boot_wifi_installer_and_app_launcher_window_scaling(self):
        import tempfile
        import rj_app_launcher as al
        from payloads.utilities import app_launcher as al_ui

        repo_root = os.path.dirname(os.path.dirname(__file__))

        # 1. Installer & WiFi boot pinning must NOT stall UI boot on network-online.target / udev-settle / ifupdown
        with open(os.path.join(repo_root, "install_raspyjack.sh"), "r", encoding="utf-8") as fh:
            inst_txt = fh.read()
        self.assertNotIn("Wants=network-online.target", inst_txt)
        self.assertNotIn("Wants=systemd-udev-settle.service", inst_txt)
        non_comment = "\n".join(ln for ln in inst_txt.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotIn("plugins=ifupdown,keyfile", non_comment)
        self.assertIn("unmanaged-devices=interface-name:wlan0mon", inst_txt)
        self.assertIn("TimeoutStartSec=5", inst_txt)

        with open(os.path.join(repo_root, "scripts", "pin_wifi_names.sh"), "r", encoding="utf-8") as fh:
            pin_txt = fh.read()
        self.assertNotIn("seq 1 40", pin_txt)
        self.assertIn("sleep 0.25", pin_txt)

        # 2. App Launcher window scaling: persistence (global + per-app), toolkit env/flags, systemd-run export & UI
        old_base = os.environ.get("RJ_BASE_DIR")
        with tempfile.TemporaryDirectory() as td:
            os.environ["RJ_BASE_DIR"] = td
            try:
                self.assertEqual(al.get_effective_app_scale(), 1.0)
                self.assertEqual(al.step_app_scale(1.0, +1), 1.25)
                self.assertEqual(al.step_app_scale(1.0, -1), 0.75)

                al.set_app_scale(1.5)
                self.assertEqual(al.get_effective_app_scale(), 1.5)
                al.set_app_scale(0.75, app_id="chromium")
                self.assertEqual(al.get_effective_app_scale("chromium"), 0.75)
                self.assertEqual(al.get_effective_app_scale("firefox"), 1.5)

                sess_cage = {
                    "mode": "kiosk_cage", "mode_label": "CAGE KIOSK", "wayland_display": "",
                    "x_display": "", "kiosk_tool": "cage", "kiosk_vt": 7,
                    "user": "pi", "uid": os.geteuid(), "gid": os.getegid(),
                    "home": td, "runtime_dir": td,
                }
                chr_app = {"id": "chromium", "name": "Chromium", "argv": ["chromium-browser"], "exec": "chromium-browser", "source": "SYS"}
                cmd_c, env_c, _ho, desc_c = al.build_launch_plan(chr_app, session_info=sess_cage)
                self.assertIn("--force-device-scale-factor=0.75", cmd_c)
                self.assertEqual(env_c.get("QT_SCALE_FACTOR"), "0.75")
                self.assertEqual(env_c.get("GDK_SCALE"), "1")
                self.assertEqual(env_c.get("GDK_DPI_SCALE"), "0.75")
                self.assertIn("75%", desc_c)

                # systemd-run kiosk wrapper exports all scale variables so headless Pi apps receive them
                sr = al.build_systemd_kiosk_command(cmd_c, env_c, user="pi", gid=1000, cwd=td, vt_num=7)
                sr_joined = " ".join(sr)
                for var in ("GDK_SCALE=1", "GDK_DPI_SCALE=0.75", "QT_SCALE_FACTOR=0.75", "RJ_APP_SCALE=0.75"):
                    self.assertIn(var, sr_joined)

                # xinit applies scaled -dpi; gamescope applies scaled virtual -w/-h
                ff_app = {"id": "firefox", "name": "Firefox", "argv": ["firefox"], "exec": "firefox", "source": "SYS"}
                sess_x = dict(sess_cage, mode="kiosk_xinit", mode_label="XINIT KIOSK", kiosk_tool="xinit")
                cmd_x, env_x, _hox, desc_x = al.build_launch_plan(ff_app, session_info=sess_x)
                self.assertIn("-dpi", cmd_x)
                self.assertIn("144", cmd_x)
                self.assertEqual(env_x.get("GDK_DPI_SCALE"), "1.5")
                self.assertIn("150%", desc_x)

                sess_gs = dict(sess_cage, mode="kiosk_gamescope", mode_label="GAMESCOPE", kiosk_tool="gamescope")
                cmd_g, _eg, _hog, _dg = al.build_launch_plan(ff_app, session_info=sess_gs, scale=2.0)
                self.assertIn("-w", cmd_g)
                self.assertIn("960", cmd_g)
                self.assertIn("540", cmd_g)

                # Clearing per-app override falls back to global scale
                al.set_app_scale(None, app_id="chromium")
                self.assertEqual(al.get_effective_app_scale("chromium"), 1.5)

                # App launcher UI renders scale pill cleanly across resolutions
                for w, h in ((640, 360), (320, 170), (128, 128)):
                    im = al_ui.render_app_launcher_frame(
                        w, h, [chr_app, ff_app], session_label="CAGE KIOSK",
                        app_scale=1.5, app_scales={"chromium": 0.75},
                    )
                    self.assertEqual(im.size, (w, h))
            finally:
                if old_base is None:
                    os.environ.pop("RJ_BASE_DIR", None)
                else:
                    os.environ["RJ_BASE_DIR"] = old_base


if __name__ == "__main__":
    unittest.main()
