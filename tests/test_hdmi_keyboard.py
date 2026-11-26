#!/usr/bin/env python3
"""Comprehensive unit and integration tests for RaspyJack HDMI & normal keyboard port."""

import importlib
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Ensure tests run cleanly without spawning a real GUI window or opening /dev/input
os.environ["RJ_HDMI_NO_AUTOHOST"] = "1"
os.environ["RJ_NO_EVDEV_AUTOSTART"] = "1"
os.environ["RJ_DISPLAY_TYPE"] = "HDMI"
os.environ["RJ_BASE_DIR"] = str(ROOT)


class TestRjEnv(unittest.TestCase):
    def test_base_dir_and_display_type(self):
        import rj_env

        self.assertEqual(os.path.abspath(rj_env.get_base_dir()), str(ROOT))
        self.assertEqual(rj_env.get_display_type(), "HDMI")
        self.assertTrue(rj_env.is_hdmi_mode())

    def test_hdmi_config_parsing(self):
        import rj_env

        old = os.environ.get("RJ_HDMI_RES")
        try:
            os.environ["RJ_HDMI_RES"] = "320x180"
            cfg = rj_env.get_hdmi_config()
            self.assertEqual((cfg["width"], cfg["height"]), (320, 180))
            os.environ["RJ_HDMI_RES"] = "640x360"
            cfg = rj_env.get_hdmi_config()
            self.assertEqual((cfg["width"], cfg["height"]), (640, 360))
        finally:
            if old is None:
                os.environ.pop("RJ_HDMI_RES", None)
            else:
                os.environ["RJ_HDMI_RES"] = old

    def test_cross_distro_font_loading(self):
        import rj_env

        f_sans = rj_env.load_font(14)
        f_bold = rj_env.load_font(14, bold=True)
        f_mono = rj_env.load_font(14, mono=True)
        f_icon = rj_env.load_font(14, icon=True)
        self.assertIsNotNone(f_sans)
        self.assertIsNotNone(f_bold)
        self.assertIsNotNone(f_mono)
        self.assertIsNotNone(f_icon)

    def test_cpu_temp_never_raises(self):
        import rj_env

        t = rj_env.read_cpu_temp()
        self.assertIsInstance(t, float)
        self.assertGreaterEqual(t, 0.0)


class TestHDMISharedMemory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.shm_path = os.path.join(self.tmp.name, "test_hdmi.shm")
        import hdmi_shm

        hdmi_shm.close_mappings()
        self._old_shm = hdmi_shm.SHM_PATH
        hdmi_shm.SHM_PATH = self.shm_path

    def tearDown(self):
        import hdmi_shm

        hdmi_shm.close_mappings()
        hdmi_shm.SHM_PATH = self._old_shm
        self.tmp.cleanup()

    def test_publish_and_read_roundtrip(self):
        import hdmi_shm

        img = Image.new("RGB", (160, 90), (12, 34, 56))
        seq1 = hdmi_shm.publish_pil_image(img)
        self.assertTrue(seq1 > 0 and seq1 % 2 == 0)

        frame = hdmi_shm.read_frame(last_seq=0)
        self.assertIsNotNone(frame)
        rseq, rw, rh, rgb, flags = frame
        self.assertEqual(rseq, seq1)
        self.assertEqual((rw, rh), (160, 90))
        self.assertEqual(flags, 0)
        self.assertEqual(rgb[:3], bytes([12, 34, 56]))
        # Same seq returns None
        self.assertIsNone(hdmi_shm.read_frame(last_seq=rseq))
        self.assertEqual(hdmi_shm.peek_seq(), seq1)

        # Video fast-mode flag (FLAG_NO_HUD) & bytearray zero-copy publish
        ba = bytearray([9, 8, 7] * (32 * 18))
        seq2 = hdmi_shm.publish_frame(ba, 32, 18, flags=hdmi_shm.FLAG_NO_HUD)
        self.assertEqual(hdmi_shm.peek_seq(), seq2)
        f2 = hdmi_shm.read_frame(last_seq=seq1)
        self.assertIsNotNone(f2)
        self.assertEqual(f2[4], hdmi_shm.FLAG_NO_HUD)
        self.assertEqual(f2[3][:3], bytes([9, 8, 7]))

    def test_rgb565_conversion(self):
        import hdmi_shm
        import numpy as np

        # Pure red in RGB565LE = 0xF800
        raw565 = np.full((8, 16), 0xF800, dtype="<u2").tobytes()
        seq = hdmi_shm.publish_rgb565(raw565, 16, 8)
        frame = hdmi_shm.read_frame(last_seq=0)
        self.assertIsNotNone(frame)
        self.assertEqual(frame[0], seq)
        self.assertEqual(frame[3][:3], bytes([255, 0, 0]))

    def test_cross_subprocess_frame_handoff(self):
        import hdmi_shm

        # Parent publishes frame 1
        seq1 = hdmi_shm.publish_pil_image(Image.new("RGB", (64, 36), (1, 2, 3)))
        # Child subprocess publishes frame 2 into same SHM
        env = os.environ.copy()
        env["RJ_HDMI_SHM"] = self.shm_path
        env["PYTHONPATH"] = str(ROOT)
        code = (
            "import hdmi_shm; from PIL import Image; "
            "hdmi_shm.publish_pil_image(Image.new('RGB', (64, 36), (200, 100, 50)))"
        )
        res = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, msg=res.stderr)

        frame = hdmi_shm.read_frame(last_seq=seq1)
        self.assertIsNotNone(frame)
        self.assertGreater(frame[0], seq1)
        self.assertEqual(frame[3][:3], bytes([200, 100, 50]))


class TestHDMIHostGeometryAndFBDev(unittest.TestCase):
    def test_compute_viewport_fit_and_integer(self):
        import hdmi_host

        # 640x360 into 1920x1080 -> exact 3x full screen
        self.assertEqual(hdmi_host.compute_viewport(640, 360, 1920, 1080, "fit"), (0, 0, 1920, 1080))
        # 480x480 into 1920x1080 -> centered pillarbox 1080x1080 at x=420
        self.assertEqual(hdmi_host.compute_viewport(480, 480, 1920, 1080, "fit"), (420, 0, 1080, 1080))
        # Integer scale 320x180 into 1000x600 -> 3x = 960x540 centered at (20, 30)
        self.assertEqual(hdmi_host.compute_viewport(320, 180, 1000, 600, "integer"), (20, 30, 960, 540))
        # Stretch fills entire target
        self.assertEqual(hdmi_host.compute_viewport(480, 480, 1920, 1080, "stretch"), (0, 0, 1920, 1080))

    def test_convert_rgb24_to_fb_16bpp_and_32bpp_with_stride(self):
        import hdmi_host

        # Pure blue 4x4 into 8x4 32bpp BGRA with 40-byte stride (8 bytes padding per row)
        rgb = bytes([0, 0, 255]) * (4 * 4)
        fb32 = hdmi_host.convert_rgb24_to_fb(
            rgb, 4, 4, 8, 4, bpp=32, line_length=40,
            red_off=16, green_off=8, blue_off=0, scale_mode="fit",
        )
        self.assertEqual(len(fb32), 40 * 4)
        # Centered 4x4 inside 8x4 starts at x=2 -> byte offset 8 in row 0 -> BGRA = (255, 0, 0, 255)
        self.assertEqual(fb32[0:4], bytes([0, 0, 0, 255]))
        self.assertEqual(fb32[8:12], bytes([255, 0, 0, 255]))

        # Pure green 4x4 into 4x4 16bpp RGB565
        rgb_g = bytes([0, 255, 0]) * (4 * 4)
        fb16 = hdmi_host.convert_rgb24_to_fb(
            rgb_g, 4, 4, 4, 4, bpp=16, line_length=8, scale_mode="fit",
        )
        self.assertEqual(len(fb16), 32)
        # RGB565 green = 0x07E0 -> LE bytes 0xE0, 0x07
        self.assertEqual(fb16[0:2], bytes([0xE0, 0x07]))

        # Fast-video integer & non-integer 32bpp/16bpp scaling preserves colors & alpha=255
        import numpy as np
        buf32 = hdmi_host._init_fb_buffer(12, 8, 32, 48, 16, 8, 0)
        vx, vy, vw, vh = hdmi_host._blit_rgb24_into_fb_buf(
            buf32, bytes([10, 20, 200]) * (4 * 2), 4, 2, 12, 8, 32, 16, 8, 0, "fit", fast_scale=True
        )
        self.assertEqual((vx, vy, vw, vh), (0, 1, 12, 6))
        self.assertEqual(bytes(buf32[1, 0:4]), bytes([200, 20, 10, 255]))
        buf32_ni = hdmi_host._init_fb_buffer(10, 6, 32, 40, 16, 8, 0)
        hdmi_host._blit_rgb24_into_fb_buf(
            buf32_ni, bytes([200, 30, 40]) * (4 * 3), 4, 3, 10, 6, 32, 16, 8, 0, "fit", fast_scale=True
        )
        self.assertEqual(bytes(buf32_ni[0, 4:8]), bytes([40, 30, 200, 255]))
        buf16_i = hdmi_host._init_fb_buffer(8, 8, 16, 16, 11, 5, 0)
        hdmi_host._blit_rgb24_into_fb_buf(
            buf16_i, rgb_g, 4, 4, 8, 8, 16, 11, 5, 0, "fit", fast_scale=True
        )
        self.assertEqual(bytes(buf16_i[0, 0:2]), bytes([0xE0, 0x07]))

    def test_compose_hud_overlay_dimensions_and_modes(self):
        import hdmi_host

        rgb = bytes([10, 20, 30]) * (640 * 360)
        out_nav, w1, h1 = hdmi_host.compose_hud_overlay(rgb, 640, 360, text_mode=False)
        out_txt, w2, h2 = hdmi_host.compose_hud_overlay(rgb, 640, 360, text_mode=True)
        self.assertEqual(w1, 640)
        self.assertGreater(h1, 360)
        self.assertEqual(len(out_nav), w1 * h1 * 3)
        self.assertEqual((w2, h2), (w1, h1))
        self.assertEqual(len(out_txt), w2 * h2 * 3)
        self.assertNotEqual(out_nav[640 * 360 * 3 :], out_txt[640 * 360 * 3 :])


class TestKeyboardEngineAndShim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import rj_keyboard

        rj_keyboard.close_mappings()
        self._old_kbd_shm = rj_keyboard.SHM_PATH
        rj_keyboard.SHM_PATH = os.path.join(self.tmp.name, "test_kbd.shm")
        self._old_sock = os.environ.get("RJ_INPUT_SOCK")
        os.environ["RJ_INPUT_SOCK"] = os.path.join(self.tmp.name, "test_input.sock")
        rj_keyboard.INPUT_SOCK_PATH = os.environ["RJ_INPUT_SOCK"]

        import rj_input

        rj_input._SOCK_PATH = os.environ["RJ_INPUT_SOCK"]
        rj_input.restart_listener()
        rj_input.flush()

    def tearDown(self):
        import rj_keyboard
        import rj_input

        rj_keyboard.set_text_mode(False)
        rj_keyboard.clear_all_keys()
        rj_keyboard.close_mappings()
        rj_keyboard.SHM_PATH = self._old_kbd_shm
        if self._old_sock is None:
            os.environ.pop("RJ_INPUT_SOCK", None)
        else:
            os.environ["RJ_INPUT_SOCK"] = self._old_sock
        rj_input._cleanup()
        self.tmp.cleanup()

    def test_standard_scancode_translation_shift_caps_symbols_numpad(self):
        import rj_keyboard

        # Lowercase, Shift uppercase, CapsLock uppercase, Shift+CapsLock lowercase
        self.assertEqual(rj_keyboard.translate_evdev_code(30, 0), ("a", None))
        self.assertEqual(rj_keyboard.translate_evdev_code(30, rj_keyboard.MOD_LSHIFT), ("A", None))
        self.assertEqual(rj_keyboard.translate_evdev_code(30, rj_keyboard.MOD_CAPSLOCK), ("A", None))
        self.assertEqual(
            rj_keyboard.translate_evdev_code(30, rj_keyboard.MOD_LSHIFT | rj_keyboard.MOD_CAPSLOCK),
            ("a", None),
        )
        # Shifted digits & punctuation (NOT CardputerZero Fn codes!)
        self.assertEqual(rj_keyboard.translate_evdev_code(2, 0), ("1", None))
        self.assertEqual(rj_keyboard.translate_evdev_code(2, rj_keyboard.MOD_RSHIFT), ("!", None))
        self.assertEqual(rj_keyboard.translate_evdev_code(26, 0), ("[", None))
        self.assertEqual(rj_keyboard.translate_evdev_code(26, rj_keyboard.MOD_LSHIFT), ("{", None))
        self.assertEqual(rj_keyboard.translate_evdev_code(52, 0), (".", None))
        self.assertEqual(rj_keyboard.translate_evdev_code(53, rj_keyboard.MOD_LSHIFT), ("?", None))
        # Numpad & Ctrl+C
        self.assertEqual(rj_keyboard.translate_evdev_code(79, 0), ("1", None))
        self.assertEqual(rj_keyboard.translate_evdev_code(96, 0), (None, "ENTER"))
        self.assertEqual(rj_keyboard.translate_evdev_code(46, rj_keyboard.MOD_LCTRL), (None, "CTRL_C"))

    def test_no_f_z_x_c_arrow_collision_in_standard_profile(self):
        import rj_keyboard
        import gpio_shim

        for code in (33, 44, 45, 46):  # F, Z, X, C
            rj_keyboard.handle_key_event(code, pressed=True, profile="standard")
            self.assertEqual(gpio_shim.input(6), 1)   # UP must NOT trigger
            self.assertEqual(gpio_shim.input(19), 1)  # DOWN must NOT trigger
            self.assertEqual(gpio_shim.input(5), 1)   # LEFT must NOT trigger
            self.assertEqual(gpio_shim.input(26), 1)  # RIGHT must NOT trigger
            rj_keyboard.handle_key_event(code, pressed=False, profile="standard")

    def test_headless_host_registration_prevents_duplicate_evdev_readers(self):
        import rj_keyboard
        import evdev_keys

        rj_keyboard.register_host(os.getpid())
        try:
            self.assertTrue(rj_keyboard.is_host_active())
            # Non-host process must not spawn a duplicate evdev reader thread when host is active
            old_thread = evdev_keys._thread
            evdev_keys._thread = None
            evdev_keys._is_host_owner = False
            evdev_keys.start(as_host=False)
            self.assertIsNone(evdev_keys._thread)
            evdev_keys._thread = old_thread
        finally:
            rj_keyboard.unregister_host(os.getpid())

    def test_text_mode_suppresses_space_tab_enter_escape_gpio_collisions(self):
        import rj_keyboard
        import gpio_shim
        import rj_input

        # In navigation mode, Space triggers KEY1 (pin 21 -> LOW = 0)
        rj_keyboard.handle_key_event(57, pressed=True, profile="standard")
        self.assertEqual(gpio_shim.input(21), 0)
        rj_keyboard.handle_key_event(57, pressed=False, profile="standard")
        self.assertEqual(gpio_shim.input(21), 1)
        # Allow async Unix datagram listener thread to dequeue navigation-mode events before flushing
        time.sleep(0.03)
        rj_input.flush()

        # In text-input mode, Space types ' ' and does NOT trigger KEY1!
        with rj_keyboard.text_input_mode():
            rj_keyboard.handle_key_event(57, pressed=True, profile="standard")
            self.assertEqual(gpio_shim.input(21), 1)
            rj_keyboard.handle_key_event(57, pressed=False, profile="standard")
            rj_keyboard.handle_key_event(28, pressed=True, profile="standard")
            self.assertEqual(gpio_shim.input(13), 1)  # OK suppressed
            rj_keyboard.handle_key_event(28, pressed=False, profile="standard")

        # Wait briefly for datagrams on Unix socket
        time.sleep(0.05)
        ev1 = rj_input.get_text_event()
        ev2 = rj_input.get_text_event()
        self.assertIsNotNone(ev1)
        self.assertEqual(ev1.get("key"), " ")
        self.assertIsNotNone(ev2)
        self.assertEqual(ev2.get("special"), "ENTER")

    def test_rpi_gpio_shim_router_loads_in_hdmi_mode(self):
        import RPi.GPIO as GPIO

        self.assertEqual(GPIO.BCM, 11)
        self.assertEqual(GPIO.PUD_UP, 22)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(16, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        self.assertEqual(GPIO.input(16), 1)

    def test_lcd_keyboard_accepts_full_shifted_typing_without_collisions(self):
        import rj_keyboard
        import rj_input
        import RPi.GPIO as GPIO
        import LCD_1in44
        from payloads._keyboard_helper import lcd_keyboard
        from payloads._display_helper import scaled_font

        lcd = LCD_1in44.LCD()
        lcd.LCD_Init()
        pins = {"UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26, "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16}
        font = scaled_font()

        # Feed typing sequence from a background thread once lcd_keyboard opens
        import threading

        def _type_sequence():
            time.sleep(0.08)
            for ch in "Hi World! 123x":
                rj_keyboard.send_text_event(char=ch)
                time.sleep(0.005)
            rj_keyboard.send_text_event(special="BACKSPACE")
            time.sleep(0.01)
            rj_keyboard.send_text_event(special="ENTER")

        t = threading.Thread(target=_type_sequence, daemon=True)
        t.start()
        result = lcd_keyboard(lcd, font, pins, GPIO, title="Test", default="", charset="full", max_len=32)
        t.join(timeout=2)
        self.assertEqual(result, "Hi World! 123")

    def test_lcd_keyboard_f1_press_f2_page_space_del_done_and_renderers(self):
        import threading
        import rj_keyboard
        import RPi.GPIO as GPIO
        import LCD_1in44
        from payloads._keyboard_helper import (
            lcd_keyboard,
            render_keyboard_frame,
            get_keyboard_pages,
            should_show_onscreen_keyboard,
        )
        from payloads._display_helper import scaled_font

        # 0. Display-type auto-visibility rules:
        #    Hidden by default on HDMI & CARDPUTER_320; auto-shown on SPI button-HAT screens!
        self.assertFalse(should_show_onscreen_keyboard(display_type="HDMI"))
        self.assertFalse(should_show_onscreen_keyboard(display_type="CARDPUTER_320"))
        self.assertTrue(should_show_onscreen_keyboard(display_type="ST7735_128"))
        self.assertTrue(should_show_onscreen_keyboard(display_type="ST7789_240"))

        lcd = LCD_1in44.LCD()
        lcd.LCD_Init()
        self.assertFalse(should_show_onscreen_keyboard(lcd=lcd))
        pins = {"UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26, "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16}
        font = scaled_font()

        def _tap(evdev_code: int, hold: float = 0.03, pause: float = 0.06):
            rj_keyboard.handle_key_event(evdev_code, pressed=True, profile="standard")
            time.sleep(hold)
            rj_keyboard.handle_key_event(evdev_code, pressed=False, profile="standard")
            time.sleep(pause)

        # 1. On HDMI (OSK hidden by default): F1 does NOT type while hidden; pressing Tab (15)
        #    reveals the digital keyboard; then F1 -> 'q', RIGHT -> 'w', F2 -> ABC, F1 -> 'W',
        #    DOWN x3 -> Action Bar, RIGHT -> SPACE -> F1 -> ' ', RIGHT -> DEL -> F1 deletes ' ',
        #    LEFT -> SPACE -> F1 -> ' ', ENTER submits!
        def _drive_hdmi_toggle_and_f1_f2_space():
            time.sleep(0.10)
            _tap(59)   # F1 while OSK hidden on HDMI -> ignored (must not type 'q')
            _tap(15)   # TAB -> opens digital keyboard!
            _tap(59)   # F1 -> 'q' at (0, 0)
            _tap(106)  # RIGHT -> (0, 1) 'w'
            _tap(60)   # F2 -> switch page abc -> ABC
            _tap(59)   # F1 -> 'W' at (0, 1)
            _tap(108)  # DOWN -> row 1
            _tap(108)  # DOWN -> row 2
            _tap(108)  # DOWN -> Action Bar (from col 1 lands on 'page' col 0)
            _tap(106)  # RIGHT -> 'space' (col 1)
            _tap(59)   # F1 -> SPACE (' ')
            _tap(106)  # RIGHT -> 'del' (col 2)
            _tap(59)   # F1 -> DEL (removes ' ')
            _tap(105)  # LEFT -> 'space' (col 1)
            _tap(59)   # F1 -> SPACE (' ')
            _tap(28)   # ENTER -> submit!

        t1 = threading.Thread(target=_drive_hdmi_toggle_and_f1_f2_space, daemon=True)
        t1.start()
        res1 = lcd_keyboard(lcd, font, pins, GPIO, title="Digital KB", default="", charset="full", max_len=32)
        t1.join(timeout=3)
        self.assertEqual(res1, "qW ")

        # 2. On SPI button-HAT screen (ST7735_128), digital keyboard auto-shows WITHOUT pressing Tab!
        class _FakeHATLCD:
            width = 128
            height = 128
            display_type = "ST7735_128"
            def LCD_ShowImage(self, _img, _x, _y):
                pass

        hat_lcd = _FakeHATLCD()
        self.assertTrue(should_show_onscreen_keyboard(lcd=hat_lcd))

        def _drive_hat_auto_osk_done():
            time.sleep(0.10)
            _tap(59)   # F1 immediately types 'q' because OSK auto-showed on HAT!
            _tap(103)  # UP wraps from row 0 directly to Action Bar (col 0 = 'page')
            _tap(105)  # LEFT wraps from col 0 to col 3 = 'done'
            _tap(59)   # F1 on DONE -> submits!

        t2 = threading.Thread(target=_drive_hat_auto_osk_done, daemon=True)
        t2.start()
        res2 = lcd_keyboard(hat_lcd, font, pins, GPIO, title="HAT Auto", default="", charset="full", max_len=32)
        t2.join(timeout=3)
        self.assertEqual(res2, "q")

        # 3. Verify F4 (evdev 62) also toggles OSK on & off cleanly on HDMI
        def _drive_f4_toggle():
            time.sleep(0.10)
            _tap(62)   # F4 -> show OSK
            _tap(59)   # F1 -> 'q'
            _tap(62)   # F4 -> hide OSK again
            _tap(59)   # F1 while hidden -> ignored
            _tap(28)   # ENTER -> submit 'q'

        t3 = threading.Thread(target=_drive_f4_toggle, daemon=True)
        t3.start()
        res3 = lcd_keyboard(lcd, font, pins, GPIO, title="F4 Toggle", default="", charset="full", max_len=32)
        t3.join(timeout=3)
        self.assertEqual(res3, "q")

        # 4. Render both direct-typing dialog (show_osk=False) & full keyboard across geometries
        for cs in ("full", "alpha", "digits", "ip", "hex", "mac", "url"):
            pgs = get_keyboard_pages(cs)
            for w, h in ((640, 360), (320, 170), (240, 240), (128, 128)):
                im_a = render_keyboard_frame(w, h, "Title", "", pgs, 0, 0, 0, charset=cs, blink_on=True, show_osk=True, can_hide_osk=(w >= 320))
                im_b = render_keyboard_frame(w, h, "Long Title Example", "hello world 123 ", pgs, 0, len(pgs[0]["rows"]), 1, charset=cs, blink_on=True, show_osk=True)
                im_c = render_keyboard_frame(w, h, "Search YouTube", "lofi beats ", pgs, 0, 0, 0, charset=cs, blink_on=True, show_osk=False)
                self.assertEqual(im_a.size, (w, h))
                self.assertEqual(im_b.size, (w, h))
                self.assertEqual(im_c.size, (w, h))


class TestLCDAndScaledDrawInHDMI(unittest.TestCase):
    def test_lcd_hdmi_init_draw_and_open_framebuffer_proxy(self):
        import hdmi_shm
        import LCD_1in44
        from payloads._display_helper import ScaledDraw, scaled_font, open_framebuffer, _HDMIFBProxy

        tmp = tempfile.TemporaryDirectory()
        hdmi_shm.close_mappings()
        old_shm = hdmi_shm.SHM_PATH
        hdmi_shm.SHM_PATH = os.path.join(tmp.name, "lcd_test.shm")
        try:
            lcd = LCD_1in44.LCD()
            self.assertEqual(lcd.display_type, "HDMI")
            self.assertEqual(lcd.LCD_Init(), 0)

            img = Image.new("RGB", (lcd.width, lcd.height), "black")
            d = ScaledDraw(img)
            d.rectangle((0, 0, 127, 127), fill=(10, 20, 30))
            d.text((6, 6), "HDMI OK", font=scaled_font(10), fill=(0, 255, 0))
            lcd.LCD_ShowImage(img, 0, 0)

            frame = hdmi_shm.read_frame(last_seq=0)
            self.assertIsNotNone(frame)
            self.assertEqual((frame[1], frame[2]), (lcd.width, lcd.height))

            # Verify open_framebuffer returns _HDMIFBProxy in HDMI mode
            fd, fb = open_framebuffer(lcd, "/dev/fb0", 320, 170)
            self.assertIsNone(fd)
            self.assertIsInstance(fb, _HDMIFBProxy)
            fb.seek(0)
            fb.write(b"\x00\xf8" * (320 * 170))
            fb.close()
            frame2 = hdmi_shm.read_frame(last_seq=frame[0])
            self.assertIsNotNone(frame2)
            self.assertEqual((frame2[1], frame2[2]), (320, 170))
        finally:
            hdmi_shm.close_mappings()
            hdmi_shm.SHM_PATH = old_shm
            tmp.cleanup()

    def test_lcd_hdmi_deduplicates_identical_frames_and_resumes_on_change_or_external_write(self):
        import hdmi_shm
        import LCD_1in44

        tmp = tempfile.TemporaryDirectory()
        hdmi_shm.close_mappings()
        old_shm = hdmi_shm.SHM_PATH
        hdmi_shm.SHM_PATH = os.path.join(tmp.name, "lcd_dedup.shm")
        try:
            lcd = LCD_1in44.LCD()
            lcd.LCD_Init()
            img = Image.new("RGB", (lcd.width, lcd.height), (14, 14, 16))
            lcd.LCD_ShowImage(img, 0, 0)
            seq1 = hdmi_shm.peek_seq()
            self.assertGreater(seq1, 0)

            # Identical frame must NOT re-publish to SHM
            lcd.LCD_ShowImage(img, 0, 0)
            self.assertEqual(hdmi_shm.peek_seq(), seq1)

            # Changing 1 pixel must immediately publish
            img.putpixel((10, 10), (242, 153, 74))
            lcd.LCD_ShowImage(img, 0, 0)
            seq2 = hdmi_shm.peek_seq()
            self.assertGreater(seq2, seq1)

            # External process publishing to SHM invalidates dedup even for identical image
            hdmi_shm.publish_frame(b"\x11" * (lcd.width * lcd.height * 3), lcd.width, lcd.height)
            seq_ext = hdmi_shm.peek_seq()
            self.assertGreater(seq_ext, seq2)
            lcd.LCD_ShowImage(img, 0, 0)
            seq3 = hdmi_shm.peek_seq()
            self.assertGreater(seq3, seq_ext)

            # LCD_Clear also invalidates dedup
            lcd.LCD_Clear()
            seq_clr = hdmi_shm.peek_seq()
            lcd.LCD_ShowImage(img, 0, 0)
            self.assertGreater(hdmi_shm.peek_seq(), seq_clr)
        finally:
            hdmi_shm.close_mappings()
            hdmi_shm.SHM_PATH = old_shm
            tmp.cleanup()

    def test_fbdev_dirty_row_blit_matches_full_blit_across_geometries_and_filters(self):
        import numpy as np
        import hdmi_host

        rng = np.random.default_rng(42)
        cases = [
            # (src_w, src_h, dst_w, dst_h, bpp, fast_scale)
            (640, 360, 1920, 1080, 32, True),   # Exact 3x3 integer
            (640, 379, 1920, 1080, 32, True),   # Non-integer HUD-on fast LUT
            (640, 360, 1366, 768, 32, True),    # Non-integer laptop display fast LUT
            (640, 379, 1920, 1080, 16, True),   # 16bpp RGB565 fast LUT
            (640, 379, 1920, 1080, 32, False),  # Non-integer HUD-on smooth bilinear
        ]
        for sw, sh, dw, dh, bpp, fast in cases:
            bpp_bytes = bpp // 8
            stride = dw * bpp_bytes
            frame_a = rng.integers(0, 256, size=(sh, sw, 3), dtype=np.uint8)
            frame_b = frame_a.copy()
            # Modify a 14-row band in the middle (like typing 1 terminal row)
            sy0, sy1 = 120, 134
            frame_b[sy0:sy1] = rng.integers(0, 256, size=(sy1 - sy0, sw, 3), dtype=np.uint8)

            buf_full = hdmi_host._init_fb_buffer(dw, dh, bpp, stride, 16, 8, 0)
            hdmi_host._blit_rgb24_into_fb_buf_dirty(
                buf_full, frame_b.tobytes(), sw, sh, dw, dh, bpp,
                red_off=16, green_off=8, blue_off=0, scale_mode="fit", fast_scale=fast,
            )

            buf_part = hdmi_host._init_fb_buffer(dw, dh, bpp, stride, 16, 8, 0)
            hdmi_host._blit_rgb24_into_fb_buf_dirty(
                buf_part, frame_a.tobytes(), sw, sh, dw, dh, bpp,
                red_off=16, green_off=8, blue_off=0, scale_mode="fit", fast_scale=fast,
            )
            vx, vy, vw, vh, dy0, dy1 = hdmi_host._blit_rgb24_into_fb_buf_dirty(
                buf_part, frame_b.tobytes(), sw, sh, dw, dh, bpp,
                red_off=16, green_off=8, blue_off=0, scale_mode="fit", fast_scale=fast,
                dirty_rows=(sy0, sy1),
            )
            self.assertGreater(dy1, dy0)
            self.assertLess(dy1 - dy0, vh)
            if fast:
                self.assertTrue(
                    np.array_equal(buf_full, buf_part),
                    f"Fast partial blit mismatch for {(sw, sh, dw, dh, bpp)}",
                )
            else:
                max_diff = int(np.max(np.abs(buf_full.astype(np.int16) - buf_part.astype(np.int16))))
                self.assertLessEqual(
                    max_diff, 1,
                    f"Smooth partial blit drift {max_diff} for {(sw, sh, dw, dh, bpp)}",
                )


if __name__ == "__main__":
    unittest.main()
