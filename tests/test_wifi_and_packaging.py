#!/usr/bin/env python3
"""Unit and integration tests for WiFi reliability, fresh-install packaging, payload error guards, and feed/anti-stutter regressions."""

import os
import pathlib
import sys
import unittest

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

                # 3. Kiosk Launch Backend selection (global + per-app + auto 3D pointer-lock game routing!)
                self.assertEqual(al.get_effective_app_backend(), "auto")
                mcpi_app = {
                    "id": "mcpi-reborn", "name": "Minecraft Pi Reborn",
                    "argv": ["minecraft-pi-reborn-client"], "exec": "minecraft-pi-reborn-client",
                    "category": "games", "source": "SYS",
                }
                self.assertTrue(al.is_pointer_lock_game(mcpi_app))
                self.assertFalse(al.is_pointer_lock_game(ff_app))

                # Auto mode automatically prefers labwc (full Wayland pointer lock + multi-window!)
                # over cage for 3D pointer-lock games, falling back to gamescope -> xinit
                sess_labwc = dict(sess_cage, available_kiosks=["cage", "labwc", "xinit"])
                cmd_lw, env_lw, ho_lw, desc_lw = al.build_launch_plan(mcpi_app, session_info=sess_labwc)
                self.assertTrue(ho_lw)
                self.assertEqual(cmd_lw[0], "labwc")
                self.assertEqual(cmd_lw[1], "-S")
                self.assertIn("minecraft-pi-reborn-client", cmd_lw[2])
                self.assertEqual(env_lw.get("XDG_SESSION_TYPE"), "wayland")
                self.assertIn("labwc", desc_lw.lower())

                # When only cage + xinit exist, Auto routes 3D games to xinit (and wraps with matchbox WM if available)
                sess_both = dict(sess_cage, available_kiosks=["cage", "xinit"], x11_wm="/usr/bin/matchbox-window-manager")
                cmd_mc, env_mc, ho_mc, desc_mc = al.build_launch_plan(mcpi_app, session_info=sess_both)
                self.assertTrue(ho_mc)
                self.assertEqual(os.path.basename(cmd_mc[0]), "xinit")
                self.assertEqual(env_mc.get("XDG_SESSION_TYPE"), "x11")
                self.assertEqual(env_mc.get("SDL_VIDEODRIVER"), "x11")
                self.assertIn("matchbox-window-manager", " ".join(cmd_mc))
                self.assertIn("xinit", desc_mc.lower())

                # Regular apps stay on cage under Auto mode
                cmd_ff_auto, _eff_a, _hoff, _dff = al.build_launch_plan(ff_app, session_info=sess_labwc)
                self.assertEqual(cmd_ff_auto[0], "cage")

                # Explicit per-app backend override + persistence + missing backend install hint
                al.set_app_backend("labwc", app_id="firefox")
                self.assertEqual(al.get_effective_app_backend("firefox"), "labwc")
                cmd_ff_lw, _eff_lw, _holw2, _dfflw = al.build_launch_plan(ff_app, session_info=sess_labwc)
                self.assertEqual(cmd_ff_lw[0], "labwc")

                al.set_app_backend("gamescope", app_id="firefox")
                cmd_miss, _emiss, ho_miss, err_miss = al.build_launch_plan(ff_app, session_info=sess_both)
                self.assertIsNone(cmd_miss)
                self.assertFalse(ho_miss)
                self.assertIn("gamescope", err_miss.lower())

                al.set_app_backend(None, app_id="firefox")
                self.assertEqual(al.get_effective_app_backend("firefox"), "auto")

                # Installer includes labwc + matchbox-window-manager for HDMI 3D games
                self.assertIn("labwc", inst_txt)
                self.assertIn("matchbox-window-manager", inst_txt)

                # App launcher UI renders scale & backend badges cleanly across all 4 screen sizes
                for w, h in ((640, 360), (320, 170), (240, 240), (128, 128)):
                    im = al_ui.render_app_launcher_frame(
                        w, h, [mcpi_app, chr_app, ff_app], session_label="CAGE KIOSK",
                        app_scale=1.5, app_scales={"chromium": 0.75},
                        app_backend="auto", app_backends={"mcpi-reborn": "xinit"},
                    )
                    self.assertEqual(im.size, (w, h))
            finally:
                if old_base is None:
                    os.environ.pop("RJ_BASE_DIR", None)
                else:
                    os.environ["RJ_BASE_DIR"] = old_base

    def test_canonical_input_helpers_cardputer_chords_and_zero_keybind_bloat(self):
        import glob
        import re
        import rj_keyboard
        from payloads import _input_helper as ih

        # 1. Canonical action matchers enforce clean 1-2 bindings per action
        self.assertTrue(ih.is_confirm("OK"))
        self.assertTrue(ih.is_confirm("ENTER"))
        self.assertFalse(ih.is_confirm("SPACE"))
        self.assertTrue(ih.is_back("KEY3"))
        self.assertTrue(ih.is_back("ESCAPE"))
        self.assertTrue(ih.is_back("BACKSPACE"))
        self.assertTrue(ih.is_back("DELETE"))
        self.assertFalse(ih.is_back("BACKSPACE", allow_backspace=False))
        self.assertFalse(ih.is_back("DELETE", allow_delete=False))
        self.assertTrue(ih.is_dismiss("OK"))
        self.assertTrue(ih.is_dismiss("ESCAPE"))
        self.assertTrue(ih.is_dismiss("BACKSPACE"))
        self.assertTrue(ih.is_dismiss("DELETE"))
        std_nav = rj_keyboard.get_nav_map("standard")
        self.assertEqual(std_nav.get(14), "KEY3_PIN")   # Backspace -> Back in nav mode
        self.assertEqual(std_nav.get(111), "KEY3_PIN")  # Delete -> Back in nav mode
        self.assertTrue(ih.is_action("KEY1", None, "KEY1", "/"))
        self.assertTrue(ih.is_action(None, "/", "KEY1", "/"))
        self.assertFalse(ih.is_action(None, "s", "KEY1", "/"))
        self.assertTrue(ih.is_step_down("-"))
        self.assertFalse(ih.is_step_down("["))
        self.assertTrue(ih.is_step_up("="))
        self.assertTrue(ih.is_step_up("+"))

        # 2. Device-aware footer hints: SPI HAT vs CardputerZero vs HDMI
        self.assertEqual(
            ih.format_footer_hint(128, 128, hat_hint="K1:A", hat_240_hint="K1:Act", cardputer_hint="A:Act", hdmi_hint="A: Action", display_type="ST7735_128"),
            "K1:A",
        )
        self.assertEqual(
            ih.format_footer_hint(240, 240, hat_hint="K1:A", hat_240_hint="K1:Act", cardputer_hint="A:Act", hdmi_hint="A: Action", display_type="ST7789_240"),
            "K1:Act",
        )
        self.assertEqual(
            ih.format_footer_hint(320, 170, hat_hint="K1:A", hat_240_hint="K1:Act", cardputer_hint="A:Act", hdmi_hint="A: Action", display_type="CARDPUTER_320"),
            "A:Act",
        )
        self.assertEqual(
            ih.format_footer_hint(640, 360, hat_hint="K1:A", hat_240_hint="K1:Act", cardputer_hint="A:Act", hdmi_hint="A: Action", display_type="HDMI"),
            "A: Action",
        )

        # 3. CardputerZero / 60% chords work in text_input_mode without physical F1-F3 / PgUp-PgDn
        self.assertEqual(rj_keyboard.translate_evdev_code(2, rj_keyboard.MOD_LCTRL), (None, "KEY1"))
        self.assertEqual(rj_keyboard.translate_evdev_code(3, rj_keyboard.MOD_LCTRL), (None, "KEY2"))
        self.assertEqual(rj_keyboard.translate_evdev_code(4, rj_keyboard.MOD_LCTRL), (None, "KEY3"))
        self.assertEqual(rj_keyboard.translate_evdev_code(103, rj_keyboard.MOD_LALT), (None, "PAGEUP"))
        self.assertEqual(rj_keyboard.translate_evdev_code(108, rj_keyboard.MOD_LALT), (None, "PAGEDOWN"))

        # 4. Static regression audit: zero 3+-key exit/confirm/action pileups in poll_ui_input apps
        root = str(ROOT)
        nav_tokens = {
            "UP", "DOWN", "LEFT", "RIGHT", "OK", "KEY1", "KEY2", "KEY3",
            "ENTER", "ESCAPE", "BACKSPACE", "TAB", "SPACE", "PGUP", "PGDN",
            "HOME", "END", "DELETE",
        }
        digit_range = set("0123456789.+-()^")
        nav_dirs = {"UP", "DOWN", "LEFT", "RIGHT", "PAGEUP", "PAGEDOWN", "HOME", "END"}
        files = sorted(glob.glob(os.path.join(root, "payloads/utilities/*.py"))) + [
            os.path.join(root, "wifi/wifi_lcd_interface.py"),
            os.path.join(root, "payloads/_iface_helper.py"),
        ]
        offenders = []
        for path in files:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                src = fh.read()
            if path.startswith(os.path.join(root, "payloads/utilities")) and "poll_ui_input" not in src:
                continue
            for ln_no, line in enumerate(src.splitlines(), 1):
                s = line.strip()
                if not s.startswith(("if ", "elif ")) or not re.search(r"\b(act|btn|button|ch|kl|key)\b", s):
                    continue
                toks = re.findall(r'["\']([^"\']{1,12})["\']', s)
                ktoks = [
                    t for t in toks
                    if t in nav_tokens or len(t) == 1 or t.lower() in ("esc", "enter", "space", "tab", "backspace", "delete")
                ]
                if len(ktoks) >= 3 and not set(ktoks).issubset(nav_dirs) and not set(ktoks).issubset(digit_range):
                    offenders.append(f"{os.path.relpath(path, root)}:{ln_no}: {s[:90]}")
        self.assertEqual(offenders, [], f"Bloated >=3-key branches found: {offenders}")

    def test_wifi_manager_scan_connect_profiles_and_ui_screens(self):
        import json
        import subprocess
        import tempfile
        from unittest import mock
        from wifi import wifi_manager as wm_mod
        from wifi import wifi_lcd_interface as wui

        # 1. Backslash-aware nmcli splitter preserves `#` and escaped `:` in SSIDs & BSSIDs
        parts = wm_mod._split_nmcli_line(r"AA\:BB\:CC\:DD\:EE\:FF:Home#5G\:Fast:84:WPA2")
        self.assertEqual(parts, ["AA:BB:CC:DD:EE:FF", "Home#5G:Fast", "84", "WPA2"])

        old_base = os.environ.get("RJ_BASE_DIR")
        with tempfile.TemporaryDirectory() as td:
            os.environ["RJ_BASE_DIR"] = td
            try:
                wm = wm_mod.WiFiManager()
                wm.wifi_interfaces = ["wlan0", "wlan1"]

                # 2. Active interface vs active WiFi interface when eth0 is selected
                wm.set_selected_interface("eth0")
                self.assertEqual(wm.get_active_interface(), "eth0")
                self.assertEqual(wm.get_active_wifi_interface(), "wlan0")
                wm.set_selected_interface("wlan1")
                self.assertEqual(wm.get_active_interface(), "wlan1")
                self.assertEqual(wm.get_active_wifi_interface(), "wlan1")

                # 3. `iw` fallback scan parsing (WPA3/SAE, WPA2, Open, deduplication by strongest signal)
                iw_sample = (
                    "BSS aa:bb:cc:dd:ee:01(on wlan0)\n"
                    "\tsignal: -72.00 dBm\n"
                    "\tcapability: ESS Privacy\n"
                    "\tSSID: EmberNet\n"
                    "\tRSN:\t * Version: 1\n"
                    "\t\t * Authentication suites: SAE\n"
                    "BSS aa:bb:cc:dd:ee:02(on wlan0)\n"
                    "\tsignal: -48.00 dBm\n"
                    "\tcapability: ESS Privacy\n"
                    "\tSSID: EmberNet\n"
                    "\tRSN:\t * Version: 1\n"
                    "\t\t * Authentication suites: PSK\n"
                    "BSS 11:22:33:44:55:66(on wlan0)\n"
                    "\tsignal: -65.00 dBm\n"
                    "\tcapability: ESS\n"
                    "\tSSID: CafeOpen\n"
                )
                with mock.patch.object(wm_mod.shutil, "which", side_effect=lambda c: "/usr/sbin/iw" if c == "iw" else None), \
                     mock.patch.object(wm_mod, "_run_cmd", return_value=subprocess.CompletedProcess(["iw"], 0, iw_sample, "")):
                    nets_iw = wm.scan_networks("wlan0")
                self.assertEqual(len(nets_iw), 2)
                self.assertEqual(nets_iw[0]["ssid"], "EmberNet")
                self.assertTrue(nets_iw[0]["encrypted"])
                self.assertEqual(nets_iw[1]["ssid"], "CafeOpen")
                self.assertFalse(nets_iw[1]["encrypted"])

                # 4. nmcli scan (busy-scan retry + `#` in SSID) + per-interface status matching
                def _fake_nmcli(cmd, timeout=15):
                    if cmd[:5] == ["nmcli", "-t", "-f", "BSSID,SSID,SIGNAL,SECURITY", "device"]:
                        if cmd[-1] == "yes":
                            return subprocess.CompletedProcess(cmd, 1, "", "Error: Scanning not allowed while already scanning")
                        out = (
                            r"AA\:BB\:CC\:DD\:EE\:FF:Lab#Wifi:91:WPA2" + "\n"
                            r"11\:22\:33\:44\:55\:66:Guest:45:--" + "\n"
                        )
                        return subprocess.CompletedProcess(cmd, 0, out, "")
                    if cmd[:4] == ["nmcli", "-t", "-f", "ACTIVE,SSID,DEVICE"]:
                        return subprocess.CompletedProcess(cmd, 0, "yes:Lab#Wifi:wlan0\nno:Guest:wlan0\n", "")
                    if cmd[:3] == ["ip", "-4", "addr"]:
                        if cmd[-1] == "wlan0":
                            return subprocess.CompletedProcess(cmd, 0, "    inet 192.168.1.42/24 brd 192.168.1.255 scope global wlan0\n", "")
                        if cmd[-1] == "eth0":
                            return subprocess.CompletedProcess(cmd, 0, "    inet 10.0.0.5/24 scope global eth0\n", "")
                        return subprocess.CompletedProcess(cmd, 0, "", "")
                    if cmd[:3] == ["ip", "link", "show"]:
                        return subprocess.CompletedProcess(cmd, 0, "2: eth0: <BROADCAST,MULTICAST,UP> state UP\n", "")
                    if cmd[:4] == ["nmcli", "-t", "-f", "NAME,TYPE,AUTOCONNECT"]:
                        return subprocess.CompletedProcess(cmd, 0, "preconfigured:802-11-wireless:yes\neth0-wired:802-3-ethernet:yes\n", "")
                    if cmd[:4] == ["nmcli", "-g", "802-11-wireless.ssid", "connection"]:
                        return subprocess.CompletedProcess(cmd, 0, "PiHomeNet\n", "")
                    return subprocess.CompletedProcess(cmd, 0, "", "")

                with mock.patch.object(wm_mod.shutil, "which", side_effect=lambda c: f"/usr/bin/{c}"), \
                     mock.patch.object(wm_mod, "_run_cmd", side_effect=_fake_nmcli), \
                     mock.patch("os.path.exists", side_effect=lambda p: p.endswith("/wlan0/wireless") or p.endswith("/wlan1/wireless") or os.path.isfile(p) or os.path.isdir(p)):
                    nets_nm = wm.scan_networks("wlan0")
                    self.assertEqual([n["ssid"] for n in nets_nm], ["Lab#Wifi", "Guest"])
                    self.assertTrue(nets_nm[0]["encrypted"])
                    self.assertFalse(nets_nm[1]["encrypted"])

                    st_w0 = wm.get_connection_status("wlan0")
                    st_w1 = wm.get_connection_status("wlan1")
                    st_eth = wm.get_connection_status("eth0")
                    self.assertEqual(st_w0["status"], "connected")
                    self.assertEqual(st_w0["ssid"], "Lab#Wifi")
                    self.assertEqual(st_w0["ip"], "192.168.1.42")
                    self.assertEqual(st_w1["status"], "disconnected")
                    self.assertEqual(st_eth["status"], "connected")
                    self.assertEqual(st_eth["ssid"], "Ethernet")

                    # 5. Saved profiles: skips sample.json placeholder & merges Pi Imager 'preconfigured' using real SSID
                    os.makedirs(wm.profiles_dir, exist_ok=True)
                    with open(os.path.join(wm.profiles_dir, "sample.json"), "w", encoding="utf-8") as fh:
                        json.dump({"ssid": "YourWiFiNetwork", "password": "YourWiFiPassword"}, fh)
                    wm.save_profile("OfficeNet", "secret123", priority=2)
                    profs = wm.load_profiles()
                    ssids = [p["ssid"] for p in profs]
                    self.assertNotIn("YourWiFiNetwork", ssids)
                    self.assertEqual(ssids, ["OfficeNet", "PiHomeNet"])
                    pi_prof = [p for p in profs if p["ssid"] == "PiHomeNet"][0]
                    self.assertEqual(pi_prof["nm_name"], "preconfigured")

                # 6. Stale NM profile recovery on connect_to_network:
                #    First tries in-place `nmcli connection modify` + `up` (preserves Pi Imager profile),
                #    and if that still fails with a corrupted profile, clears 'preconfigured' & retries.
                connect_calls = []
                def _fake_connect(cmd, timeout=15):
                    connect_calls.append(cmd)
                    if cmd[:4] == ["nmcli", "-t", "-f", "ACTIVE,SSID,DEVICE"]:
                        return subprocess.CompletedProcess(cmd, 0, "", "")
                    if cmd[:4] == ["nmcli", "-t", "-f", "NAME,TYPE"]:
                        return subprocess.CompletedProcess(cmd, 0, "preconfigured:802-11-wireless\n", "")
                    if cmd[:4] == ["nmcli", "-g", "802-11-wireless.ssid", "connection"]:
                        return subprocess.CompletedProcess(cmd, 0, "PiHomeNet\n", "")
                    if cmd[:5] == ["nmcli", "-t", "-f", "SSID", "dev"]:
                        return subprocess.CompletedProcess(cmd, 0, "PiHomeNet\n", "")
                    if cmd[:3] == ["nmcli", "connection", "modify"]:
                        return subprocess.CompletedProcess(cmd, 1, "", "Error: corrupted key-mgmt")
                    if cmd[:4] == ["nmcli", "device", "wifi", "connect"]:
                        deleted = any(c[:4] == ["nmcli", "connection", "delete", "id"] and c[4] == "preconfigured" for c in connect_calls)
                        if not deleted:
                            return subprocess.CompletedProcess(cmd, 4, "", "Error: Connection activation failed: Secrets were required, but not provided.")
                        return subprocess.CompletedProcess(cmd, 0, "Device 'wlan0' successfully activated.", "")
                    return subprocess.CompletedProcess(cmd, 0, "", "")

                with mock.patch.object(wm_mod.shutil, "which", side_effect=lambda c: f"/usr/bin/{c}"), \
                     mock.patch.object(wm_mod, "_run_cmd", side_effect=_fake_connect):
                    ok = wm.connect_to_network("PiHomeNet", "newpass123", "wlan0")
                self.assertTrue(ok)
                self.assertEqual(wm.last_error, "")
                self.assertTrue(any(c[:4] == ["nmcli", "connection", "modify", "id"] and c[4] == "preconfigured" for c in connect_calls))
                self.assertTrue(any(c[:5] == ["nmcli", "connection", "delete", "id", "preconfigured"] for c in connect_calls))

                # Installer & fix_wifi.sh harden Broadcom brcmfmac, NM bgscan/powersave/MAC/retries & competing daemons
                with open(os.path.join(str(ROOT), "install_raspyjack.sh"), "r", encoding="utf-8") as fh:
                    inst_s = fh.read()
                self.assertIn("wifi.powersave = 2", inst_s)
                self.assertIn("autoconnect-retries-default = 0", inst_s)
                self.assertIn("connection.autoconnect yes", inst_s)
                self.assertIn("managed yes autoconnect yes", inst_s)
                self.assertIn("wifi.cloned-mac-address = permanent", inst_s)
                self.assertIn("options brcmfmac roamoff=1 feature_disable=0x82000", inst_s)
                self.assertIn("brcmfmac.feature_disable=0x82000", inst_s)
                self.assertIn("802-11-wireless-security.psk-flags 0", inst_s)
                self.assertIn("802-11-wireless-security.pmf 0", inst_s)
                self.assertIn("802-11-wireless.bgscan", inst_s)
                self.assertIn("/etc/cloud/cloud-init.disabled", inst_s)
                self.assertIn("99-disable-network-config.cfg", inst_s)
                self.assertIn("NetworkManager-wait-online.service", inst_s)
                fix_sh_path = os.path.join(str(ROOT), "scripts", "fix_wifi.sh")
                self.assertTrue(os.path.isfile(fix_sh_path))
                with open(fix_sh_path, "r", encoding="utf-8") as fh:
                    fix_s = fh.read()
                self.assertIn("autoconnect-retries-default = 0", fix_s)
                self.assertIn("connection.autoconnect yes", fix_s)
                self.assertIn("connection.permissions \"\"", fix_s)
                self.assertIn("managed yes autoconnect yes", fix_s)
                self.assertIn("options brcmfmac roamoff=1 feature_disable=0x82000", fix_s)
                self.assertIn("brcmfmac.feature_disable=0x82000", fix_s)
                self.assertIn("802-11-wireless-security.pmf 0", fix_s)
                self.assertIn("802-11-wireless-security.psk-flags 0", fix_s)
                self.assertNotIn("802-11-wireless-security.pmf 1", fix_s)
                self.assertIn("wpa_supplicant@wlan0", fix_s)
                self.assertIn("/etc/cloud/cloud-init.disabled", fix_s)
                self.assertIn("99-disable-network-config.cfg", fix_s)
                self.assertIn("NetworkManager-wait-online.service", fix_s)
                self.assertIn("systemd-networkd-wait-online.service", fix_s)
                self.assertIn("/etc/netplan", fix_s)
                self.assertIn("--country", fix_s)

                # Verify WPA3/SAE fallback + psk-flags=0 + pmf=0/3 recovery on "Secrets were required"
                sae_calls = []
                def _fake_sae_connect(cmd, timeout=15):
                    sae_calls.append(cmd)
                    if cmd[:4] == ["nmcli", "-t", "-f", "ACTIVE,SSID,DEVICE"]:
                        return subprocess.CompletedProcess(cmd, 0, "", "")
                    if cmd[:4] == ["nmcli", "-t", "-f", "NAME,TYPE"]:
                        return subprocess.CompletedProcess(cmd, 0, "", "")
                    if cmd[:5] == ["nmcli", "-t", "-f", "SSID", "dev"]:
                        return subprocess.CompletedProcess(cmd, 0, "WPA3Home\n", "")
                    if cmd[:4] == ["nmcli", "device", "wifi", "connect"]:
                        return subprocess.CompletedProcess(cmd, 4, "", "Error: Connection activation failed: Secrets were required, but not provided.")
                    if cmd[:3] == ["nmcli", "connection", "add"]:
                        return subprocess.CompletedProcess(cmd, 0, "Connection added", "")
                    if cmd[:3] == ["nmcli", "connection", "up"]:
                        # Only succeed once WPA3 SAE (key-mgmt=sae, pmf=3, psk-flags=0) was added!
                        added_sae = any(
                            c[:3] == ["nmcli", "connection", "add"]
                            and "sae" in c
                            and "wifi-sec.psk-flags" in c
                            for c in sae_calls
                        )
                        if added_sae:
                            return subprocess.CompletedProcess(cmd, 0, "Connection successfully activated", "")
                        return subprocess.CompletedProcess(cmd, 4, "", "Error: Connection activation failed: Secrets were required, but not provided.")
                    return subprocess.CompletedProcess(cmd, 0, "", "")

                with mock.patch.object(wm_mod.shutil, "which", side_effect=lambda c: f"/usr/bin/{c}"), \
                     mock.patch.object(wm_mod, "_run_cmd", side_effect=_fake_sae_connect):
                    ok_sae = wm.connect_to_network("WPA3Home", "supersecret99", "wlan0", security="WPA2 WPA3")
                self.assertTrue(ok_sae)
                self.assertTrue(any(c[:3] == ["nmcli", "connection", "add"] and "sae" in c and "3" in c for c in sae_calls))
                self.assertTrue(any(c[:3] == ["nmcli", "connection", "add"] and "wifi-sec.psk-flags" in c and "0" in c for c in sae_calls))
                self.assertTrue(any(c[:3] == ["nmcli", "connection", "add"] and "connection.autoconnect" in c and "yes" in c for c in sae_calls))
                self.assertTrue(any(c[:6] == ["nmcli", "device", "set", "wlan0", "autoconnect", "yes"] for c in sae_calls))
                with open(os.path.join(str(ROOT), "wifi", "raspyjack_integration.py"), "r", encoding="utf-8") as fh:
                    integ_s = fh.read()
                self.assertNotIn("pkill', '-f', f'wpa_supplicant", integ_s)
                self.assertIn("'ip', 'route', 'replace', 'default'", integ_s)

                # Status polling NEVER triggers active scans (--rescan no), and rogue wpa_supplicant cleanup preserves NM's -u daemon
                poll_cmds = []
                def _fake_poll_and_rogue(cmd, timeout=15):
                    poll_cmds.append(cmd)
                    if cmd[:2] == ["pgrep", "-af"]:
                        return subprocess.CompletedProcess(
                            cmd, 0,
                            "401 /sbin/wpa_supplicant -u -s -O DIR=/run/wpa_supplicant GROUP=netdev\n"
                            "999 wpa_supplicant -B -i wlan0 -c /etc/wpa_supplicant/wpa_supplicant.conf\n",
                            "",
                        )
                    return subprocess.CompletedProcess(cmd, 0, "", "")

                with mock.patch.object(wm_mod.shutil, "which", side_effect=lambda c: f"/usr/bin/{c}"), \
                     mock.patch.object(wm_mod, "_run_cmd", side_effect=_fake_poll_and_rogue), \
                     mock.patch("os.path.exists", side_effect=lambda p: p.endswith("/wlan0/wireless") or os.path.isfile(p) or os.path.isdir(p)):
                    wm.get_connection_status("wlan0")
                    wm._stop_competing_wifi_processes("wlan0")
                status_wifi_cmds = [c for c in poll_cmds if c[:4] == ["nmcli", "-t", "-f", "ACTIVE,SSID,DEVICE"]]
                self.assertTrue(status_wifi_cmds and "--rescan" in status_wifi_cmds[0] and "no" in status_wifi_cmds[0])
                kill_cmds = [c for c in poll_cmds if c and c[0] == "kill"]
                self.assertEqual(kill_cmds, [["kill", "999"]])

                # Repair WiFi Stack menu item & Python fallback run cleanly
                self.assertTrue(any("Repair WiFi Stack" in lbl for lbl, _, _ in wui.MAIN_MENU_ITEMS))
                with mock.patch.object(wm_mod.shutil, "which", side_effect=lambda c: f"/usr/bin/{c}"), \
                     mock.patch.object(wm_mod, "_run_cmd", return_value=subprocess.CompletedProcess(["ok"], 0, "", "")), \
                     mock.patch("os.geteuid", return_value=1000):
                    rep_res = wm.repair_wifi_stack("wlan0")
                self.assertTrue(rep_res["ok"])
                self.assertTrue(rep_res["summary"])

                # 7. WiFiLCDInterface startup does NOT block on scan_networks, and all views render cleanly on all 4 screens
                with mock.patch.object(wm_mod.WiFiManager, "scan_networks", side_effect=AssertionError("Must not block on scan in __init__")), \
                     mock.patch.object(wm_mod.WiFiManager, "load_profiles", return_value=[]):
                    ui = wui.WiFiLCDInterface()
                    self.assertEqual(ui.current_menu, "main")

                for w, h in ((640, 360), (320, 170), (240, 240), (128, 128)):
                    for scr in ("main", "scan", "profiles", "interface", "status", "message"):
                        im = wui.render_wifi_screen(
                            w, h, scr,
                            active_iface="wlan0",
                            status={"status": "connected", "ssid": "Lab#Wifi", "ip": "192.168.1.42", "interface": "wlan0"},
                            scanned_networks=nets_nm,
                            saved_profiles=profs,
                            interfaces=["eth0", "wlan0", "wlan1"],
                            wifi_interfaces=["wlan0", "wlan1"],
                            message="Connected!",
                        )
                        self.assertEqual(im.size, (w, h))
                    im_err = wui.render_wifi_screen(w, h, "scan", scanned_networks=[], last_error="No WiFi adapter detected")
                    self.assertEqual(im_err.size, (w, h))

                # 8. raspyjack.py WiFi submenu targets real payloads/utilities/iface_manager.py
                self.assertTrue(os.path.isfile(os.path.join(str(ROOT), "payloads", "utilities", "iface_manager.py")))
                self.assertTrue(os.path.isfile(os.path.join(str(ROOT), "payloads", "utilities", "wifi_manager_payload.py")))
            finally:
                if old_base is None:
                    os.environ.pop("RJ_BASE_DIR", None)
                else:
                    os.environ["RJ_BASE_DIR"] = old_base

    def test_youtube_personalized_feed_watch_history_and_cookies(self):
        import json
        import stat
        import subprocess
        import tempfile
        from unittest import mock
        import rj_guide
        import rj_youtube_feed as ytf
        from payloads.utilities import youtube as yt

        # 1. Balanced 10-card Home Menu has For You Feed (#1) and Watch History (#8)
        home_keys = [k for _, k, _ in yt.HOME_MENU_ITEMS]
        self.assertEqual(len(home_keys), 10)
        self.assertEqual(home_keys[0], "feed")
        self.assertIn("watch_history", home_keys)

        # 2. InnerTube parser handles BOTH modern lockupViewModel and legacy compactVideoRenderer
        innertube_sample = {
            "contents": {
                "twoColumnWatchNextResults": {
                    "secondaryResults": {
                        "secondaryResults": {
                            "results": [
                                {
                                    "lockupViewModel": {
                                        "contentId": "AbCdEfGh123",
                                        "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
                                        "contentImage": {
                                            "thumbnailViewModel": {
                                                "overlays": [
                                                    {
                                                        "thumbnailOverlayBadgeViewModel": {
                                                            "thumbnailBadges": [
                                                                {"thumbnailBadgeViewModel": {"text": "14:25"}}
                                                            ]
                                                        }
                                                    }
                                                ]
                                            }
                                        },
                                        "metadata": {
                                            "lockupMetadataViewModel": {
                                                "title": {"content": "Modern Lockup Linux Deep Dive"},
                                                "metadata": {
                                                    "contentMetadataViewModel": {
                                                        "metadataRows": [
                                                            {"metadataParts": [{"text": {"content": "LinuxLab"}}]},
                                                            {"metadataParts": [{"text": {"content": "1.2M views"}}, {"text": {"content": "2 days ago"}}]},
                                                        ]
                                                    }
                                                },
                                            }
                                        },
                                    }
                                },
                                {
                                    "compactVideoRenderer": {
                                        "videoId": "XyZ98765432",
                                        "title": {"simpleText": "Legacy Compact Pi Build"},
                                        "longBylineText": {
                                            "runs": [
                                                {
                                                    "text": "PiMakers",
                                                    "navigationEndpoint": {
                                                        "browseEndpoint": {
                                                            "browseId": "UC1234567890",
                                                            "canonicalBaseUrl": "/@PiMakers",
                                                        }
                                                    },
                                                }
                                            ]
                                        },
                                        "lengthText": {"simpleText": "1:02:03"},
                                        "viewCountText": {"simpleText": "450K views"},
                                    }
                                },
                                {
                                    "lockupViewModel": {
                                        "contentId": "PLplaylist1",
                                        "contentType": "LOCKUP_CONTENT_TYPE_PLAYLIST",
                                        "metadata": {"lockupMetadataViewModel": {"title": {"content": "Should Skip Playlist"}}},
                                    }
                                },
                            ]
                        }
                    }
                }
            }
        }
        parsed = ytf.parse_innertube_next_payload(innertube_sample, reason="Because you watched LinuxLab")
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["id"], "AbCdEfGh123")
        self.assertEqual(parsed[0]["title"], "Modern Lockup Linux Deep Dive")
        self.assertEqual(parsed[0]["channel"], "LinuxLab")
        self.assertEqual(parsed[0]["duration"], 14 * 60 + 25)
        self.assertEqual(parsed[0]["views"], 1_200_000)
        self.assertEqual(parsed[0]["reason"], "Because you watched LinuxLab")
        self.assertEqual(parsed[1]["id"], "XyZ98765432")
        self.assertEqual(parsed[1]["channel"], "PiMakers")
        self.assertEqual(parsed[1]["channel_id"], "UC1234567890")
        self.assertEqual(parsed[1]["channel_url"], "https://www.youtube.com/@PiMakers")
        self.assertEqual(parsed[1]["duration"], 3723)
        self.assertEqual(parsed[1]["views"], 450_000)

        # 3. InnerTube RD mix fallback when HTTP fails
        rd_json = json.dumps({
            "entries": [
                {"id": "SeedVid0001", "title": "Seed Video", "channel": "SeedCh", "duration": 100},
                {"id": "MixVid00002", "title": "RD Mix Track", "channel": "MixCh", "duration": 240, "view_count": 55000},
            ]
        })
        with mock.patch.object(ytf.urllib.request, "urlopen", side_effect=OSError("offline")), \
             mock.patch.object(ytf.shutil, "which", return_value="/usr/bin/yt-dlp"), \
             mock.patch.object(ytf.subprocess, "run", return_value=subprocess.CompletedProcess(["yt-dlp"], 0, rd_json, "")):
            rd_items = ytf.fetch_innertube_related("SeedVid0001", reason="Mix Fallback", limit=6)
        self.assertEqual([x["id"] for x in rd_items], ["MixVid00002"])
        self.assertEqual(rd_items[0]["reason"], "Mix Fallback")

        old_override = ytf._DATA_DIR_OVERRIDE
        old_base = os.environ.get("RJ_BASE_DIR")
        with tempfile.TemporaryDirectory() as td:
            ytf._DATA_DIR_OVERRIDE = os.path.join(td, "loot", "YouTube")
            os.environ["RJ_BASE_DIR"] = td
            try:
                ytf.invalidate_feed_cache()

                # 4. Watch History recording, single remove, and full Clear (including resume.json!)
                ytf.record_watch_event({"id": "vid_watch_01", "title": "Linux Kernel Networking", "channel": "NetGuru", "duration": 600}, watched_sec=120)
                ytf.record_watch_event({"id": "vid_watch_02", "title": "Raspberry Pi Audio Tuning", "channel": "PiLab", "duration": 420}, watched_sec=400, completed=True)
                yt.save_resume_position("vid_watch_01", 120.0, 600)
                self.assertGreater(yt.get_resume_position("vid_watch_01"), 100.0)

                hist = ytf.get_watch_history()
                self.assertEqual([h["id"] for h in hist], ["vid_watch_02", "vid_watch_01"])
                self.assertIn("Watched", hist[0]["reason"])

                # Remove single item
                self.assertTrue(ytf.remove_watch_history_item("vid_watch_02"))
                self.assertEqual([h["id"] for h in ytf.get_watch_history()], ["vid_watch_01"])

                # Dismiss & mute should survive Clear Watch History, while watch history & resume.json clear cleanly
                ytf.dismiss_video("vid_hidden01")
                ytf.mute_channel("SpamChannel")
                cleared_n = ytf.clear_watch_history(clear_resume=True)
                self.assertEqual(cleared_n, 1)
                self.assertEqual(ytf.get_watch_history(), [])
                self.assertEqual(yt.get_resume_position("vid_watch_01"), 0.0)
                prof_after_clear = ytf.load_taste_profile()
                self.assertIn("vid_hidden01", prof_after_clear["dismissed_ids"])
                self.assertIn("SpamChannel", prof_after_clear["muted_channels"])

                # Reset taste profile wipes dismissed & muted too
                ytf.reset_taste_profile(clear_resume=True)
                prof_reset = ytf.load_taste_profile()
                self.assertEqual(prof_reset["dismissed_ids"], [])
                self.assertEqual(prof_reset["muted_channels"], [])

                # 5. Cold-start Local Smart Feed uses starter presets cleanly
                def _fake_search(q, lim):
                    slug = q.replace(" ", "")[:6]
                    return ([
                        {"id": f"{slug}00001"[:11].ljust(11, "x"), "title": f"Starter {q}", "channel": f"Ch_{slug}", "duration": 300, "views": 9000}
                    ], "")

                cold_items, cold_err, cold_meta = ytf.build_local_personalized_feed(
                    page=1, presets=["raspberry pi", "linux tips"], search_fn=_fake_search
                )
                self.assertEqual(cold_err, "")
                self.assertTrue(cold_meta["cold_start"])
                self.assertGreaterEqual(len(cold_items), 2)

                # 6. Warm Local Smart Feed: ranking consensus, dismissed/muted filtering, channel interleaving & pagination
                ytf.record_watch_event({"id": "SeedWatch01", "title": "Linux Security Labs", "channel": "SecCh", "duration": 500}, watched_sec=250)
                ytf.dismiss_video("Dismissed01")
                ytf.mute_channel("BlockedCreator")

                def _fake_related(vid, reason, lim):
                    if vid == "SeedWatch01":
                        return [
                            {"id": "Dismissed01", "title": "Should Hide", "channel": "OkCh", "duration": 100},
                            {"id": "MutedVid001", "title": "Muted Creator", "channel": "BlockedCreator", "duration": 100},
                            {"id": "FloodVid001", "title": "Flood 1", "channel": "SameChan", "duration": 200},
                            {"id": "FloodVid002", "title": "Flood 2", "channel": "SameChan", "duration": 200},
                            {"id": "FloodVid003", "title": "Flood 3", "channel": "SameChan", "duration": 200},
                            {"id": "SharedWin01", "title": "Consensus Linux Winner", "channel": "OtherChan", "duration": 310},
                        ]
                    return [
                        {"id": "SharedWin01", "title": "Consensus Linux Winner", "channel": "OtherChan", "duration": 310},
                        {"id": "LikeRel0001", "title": "Liked Seed Related", "channel": "DiffChan", "duration": 280},
                    ]

                warm_items, warm_err, warm_meta = ytf.build_local_personalized_feed(
                    page=1,
                    liked=[{"id": "SeedLiked01", "title": "Liked Linux Guide", "channel": "GuideCh"}],
                    fetch_related_fn=_fake_related,
                )
                self.assertEqual(warm_err, "")
                self.assertFalse(warm_meta["cold_start"])
                warm_ids = [x["id"] for x in warm_items]
                self.assertNotIn("Dismissed01", warm_ids)
                self.assertNotIn("MutedVid001", warm_ids)
                # Multi-seed consensus video ranks #1
                self.assertEqual(warm_ids[0], "SharedWin01")
                # Channel interleaving ensures no >2 consecutive from SameChan
                ch_seq = [x["channel"] for x in warm_items]
                for i in range(len(ch_seq) - 2):
                    self.assertFalse(ch_seq[i] == ch_seq[i + 1] == ch_seq[i + 2] == "SameChan")

                # 7. Cookies validation, 0600 saving, and deletion
                bad_ok, _bad_msg, _ = ytf.save_cookies_text("not a cookie file at all")
                self.assertFalse(bad_ok)

                valid_cookies = (
                    "# Netscape HTTP Cookie File\n"
                    ".youtube.com\tTRUE\t/\tTRUE\t1999999999\tSAPISID\tabc123token\n"
                    ".youtube.com\tTRUE\t/\tTRUE\t1999999999\t__Secure-3PSID\txyz987token\n"
                )
                ok_c, msg_c, info_c = ytf.save_cookies_text(valid_cookies)
                self.assertTrue(ok_c, msg_c)
                self.assertTrue(info_c["has_auth"])
                self.assertEqual(info_c["youtube_cookies"], 2)
                cpath = ytf.get_cookies_file_path()
                self.assertEqual(stat.S_IMODE(os.stat(cpath).st_mode), 0o600)

                args_c, lbl_c, src_c = ytf.resolve_ytdlp_cookie_args({"cookie_source": "auto"})
                self.assertEqual(src_c, "file")
                self.assertEqual(args_c, ["--cookies", cpath])
                self.assertIn("cookies.txt", lbl_c)

                # 8. Hybrid Auto mode: uses Account feed when valid, falls back smoothly to Local if expired
                def _fake_acct_ok(feed_kind="recommended", start=1, count=20, cookie_args=None):
                    return ([{"id": "AcctHome001", "title": "My Account Pick", "channel": "FavCreator", "duration": 400, "reason": "YouTube Account Home"}], "")

                acct_items, acct_err, acct_meta = ytf.fetch_personalized_feed(
                    cfg={"feed_mode": "auto", "cookie_source": "file"},
                    force_refresh=True,
                    fetch_account_fn=_fake_acct_ok,
                )
                self.assertEqual(acct_err, "")
                self.assertEqual(acct_items[0]["id"], "AcctHome001")
                self.assertIn("YouTube Account", acct_meta["source_label"])

                def _fake_acct_expired(feed_kind="recommended", start=1, count=20, cookie_args=None):
                    return ([], "YouTube cookies expired or not signed in")

                fb_items, fb_err, fb_meta = ytf.fetch_personalized_feed(
                    cfg={"feed_mode": "auto", "cookie_source": "file"},
                    force_refresh=True,
                    liked=[{"id": "SeedLiked01", "title": "Liked Linux Guide", "channel": "GuideCh"}],
                    fetch_related_fn=_fake_related,
                    fetch_account_fn=_fake_acct_expired,
                )
                self.assertEqual(fb_err, "")
                self.assertTrue(fb_items)
                self.assertEqual(fb_meta["source"], "local")
                self.assertIn("fell back", fb_meta["fallback_notice"])

                ok_del, _ = ytf.delete_cookies_file()
                self.assertTrue(ok_del)
                self.assertFalse(os.path.exists(cpath))

                # 9. Render 10-card Home Menu, For You Feed (with reason + Load More), and Watch History across all 4 screens
                sample_feed = [
                    {"id": "AbCdEfGh123", "title": "Linux Deep Dive", "channel": "LinuxLab", "duration": 865, "views": 1200000, "reason": "Because you watched SecCh"},
                    {"id": "XyZ98765432", "title": "Pi Zero 2W Audio Build", "channel": "PiMakers", "duration": 540, "views": 45000, "reason": "YouTube Account Home"},
                ]
                for w, h in ((640, 360), (320, 170), (240, 240), (128, 128)):
                    im_h = yt.render_home_menu(w, h, 0, history_count=12, feed_badge="ACCT", liked_count=5, downloads_count=3)
                    self.assertEqual(im_h.size, (w, h))
                    im_f = yt.render_results_view(w, h, "FOR YOU FEED", "Local Smart Feed", sample_feed, 0, 0, has_load_more=True, feed_mode=True)
                    self.assertEqual(im_f.size, (w, h))
                    im_w = yt.render_results_view(w, h, "WATCH HISTORY", "2 watched", sample_feed, 0, 0, watch_history_mode=True)
                    self.assertEqual(im_w.size, (w, h))
                    im_we = yt.render_results_view(w, h, "WATCH HISTORY", "0 watched", [], 0, 0, watch_history_mode=True)
                    self.assertEqual(im_we.size, (w, h))

                # 10. Installer, WebUI & User Guide wiring checks
                with open(os.path.join(str(ROOT), "install_raspyjack.sh"), "r", encoding="utf-8") as fh:
                    inst_txt = fh.read()
                self.assertIn("python3-pycryptodomex", inst_txt)
                self.assertIn("python3-secretstorage", inst_txt)
                self.assertIn("ffmpeg yt-dlp mpv nodejs python3-pycryptodomex", inst_txt)

                with open(os.path.join(str(ROOT), "web_server.py"), "r", encoding="utf-8") as fh:
                    ws_txt = fh.read()
                self.assertIn("/api/settings/youtube_cookies", ws_txt)
                with open(os.path.join(str(ROOT), "web", "index.html"), "r", encoding="utf-8") as fh:
                    html_txt = fh.read()
                self.assertIn("ytCookiesUploadBtn", html_txt)
                with open(os.path.join(str(ROOT), "web", "app.js"), "r", encoding="utf-8") as fh:
                    js_txt = fh.read()
                self.assertIn("/api/settings/youtube_cookies", js_txt)

                yt_guide = next(ch for ch in rj_guide.GUIDE_TOPICS if ch["id"] == "youtube_video")
                guide_blob = " ".join(str(b) for b in yt_guide["blocks"])
                self.assertIn("For You Feed", guide_blob)
                self.assertIn("Clear All Watch History", guide_blob)
                self.assertIn("cookies.txt", guide_blob)
            finally:
                ytf._DATA_DIR_OVERRIDE = old_override
                ytf.invalidate_feed_cache()
                if old_base is None:
                    os.environ.pop("RJ_BASE_DIR", None)
                else:
                    os.environ["RJ_BASE_DIR"] = old_base

    def test_ytmusic_personalized_feed_shared_cookies_and_history_clear(self):
        import tempfile
        import rj_guide
        import rj_youtube_feed as ytf
        import rj_ytmusic as ytm
        from payloads.utilities import youtube_music as ytm_ui

        # 1. 7-tab layout includes FOR YOU right beside NOW
        self.assertEqual(len(ytm_ui.TABS), 7)
        self.assertEqual(ytm_ui.TABS[:2], ("now", "for_you"))
        self.assertIn("FOR YOU", ytm_ui.TAB_LABELS.values())

        old_override = ytf._DATA_DIR_OVERRIDE
        old_base = os.environ.get("RJ_BASE_DIR")
        with tempfile.TemporaryDirectory() as td:
            ytf._DATA_DIR_OVERRIDE = os.path.join(td, "loot", "YouTube")
            os.environ["RJ_BASE_DIR"] = td
            try:
                ytm.invalidate_music_feed_cache()

                # 2. Listening History recording, single removal, and full Clear (keeping hidden artists/tracks)
                t1 = {"id": "TrkSeed0001", "title": "Nightcall", "artist": "Kavinsky", "duration": 258, "thumb": ""}
                t2 = {"id": "TrkSeed0002", "title": "Sunset", "artist": "The Midnight", "duration": 276, "thumb": ""}
                ytm.record_recent(t1)
                ytm.record_recent(t2)
                ytm.record_track_play(t1, played_sec=120)
                self.assertEqual([x["id"] for x in ytm.load_recent()], ["TrkSeed0002", "TrkSeed0001"])

                self.assertTrue(ytm.remove_recent_item("TrkSeed0002"))
                self.assertEqual([x["id"] for x in ytm.load_recent()], ["TrkSeed0001"])

                ytm.dismiss_music_track("HideMe00001")
                ytm.mute_music_artist("SkipArtist")
                cleared = ytm.clear_listen_history(clear_stats=True)
                self.assertEqual(cleared, 1)
                self.assertEqual(ytm.load_recent(), [])
                self.assertEqual(ytm.get_most_played_tracks(), [])
                self.assertIn("HideMe00001", ytm.load_music_taste()["dismissed_ids"])
                self.assertIn("SkipArtist", ytm.get_muted_artists())

                # Reset music taste wipes hidden tracks & muted artists too
                ytm.reset_music_taste(clear_history=True, clear_stats=True)
                self.assertEqual(ytm.load_music_taste()["dismissed_ids"], [])
                self.assertEqual(ytm.get_muted_artists(), [])

                # 3. Cold-start Local Smart Mix uses RADIO_STATIONS starter mix
                def _fake_search(q, max_results=10):
                    slug = "".join(c for c in q if c.isalnum())[:6]
                    return ([
                        {"id": f"{slug}11111"[:11].ljust(11, "a"), "title": f"Track {slug}", "artist": f"Art_{slug}", "duration": 210, "thumb": ""}
                    ], "")

                cold_tracks, cold_err, cold_meta = ytm.build_local_music_feed(page=1, search_fn=_fake_search)
                self.assertEqual(cold_err, "")
                self.assertTrue(cold_meta["cold_start"])
                self.assertGreaterEqual(len(cold_tracks), 3)
                self.assertIn("Starter Mix", cold_tracks[0]["reason"])

                # 4. Warm Local Smart Mix: consensus ranking, hidden filtering & max-2 artist interleaving
                ytm.record_recent(t1)
                ytm.dismiss_music_track("HideTrack01")
                ytm.mute_music_artist("MutedBand")

                def _fake_mix(vid, lim):
                    if vid == "TrkSeed0001":
                        return ([
                            {"id": "HideTrack01", "title": "Hidden Song", "artist": "GoodArtist", "duration": 200},
                            {"id": "MutedSong01", "title": "Muted Band Track", "artist": "MutedBand", "duration": 200},
                            {"id": "FloodTrk001", "title": "Flood 1", "artist": "FloodArtist", "duration": 210},
                            {"id": "FloodTrk002", "title": "Flood 2", "artist": "FloodArtist", "duration": 210},
                            {"id": "FloodTrk003", "title": "Flood 3", "artist": "FloodArtist", "duration": 210},
                            {"id": "Consensus01", "title": "Shared Synth Hit", "artist": "Carpenter Brut", "duration": 245},
                        ], "")
                    return ([
                        {"id": "Consensus01", "title": "Shared Synth Hit", "artist": "Carpenter Brut", "duration": 245},
                        {"id": "LikeMix0001", "title": "Midnight Drive", "artist": "Timecop1983", "duration": 230},
                    ], "")

                warm_tracks, warm_err, warm_meta = ytm.build_local_music_feed(
                    page=1, liked=[t2], fetch_mix_fn=_fake_mix, search_fn=_fake_search
                )
                self.assertEqual(warm_err, "")
                self.assertFalse(warm_meta["cold_start"])
                w_ids = [x["id"] for x in warm_tracks]
                self.assertNotIn("HideTrack01", w_ids)
                self.assertNotIn("MutedSong01", w_ids)
                self.assertEqual(w_ids[0], "Consensus01")
                arts = [x["artist"] for x in warm_tracks]
                for i in range(len(arts) - 2):
                    self.assertFalse(arts[i] == arts[i + 1] == arts[i + 2] == "FloodArtist")

                # 5. Shared cookies.txt powers YT Music Account Supermix + automatic Auto fallback on expired cookies
                valid_cookies = (
                    "# Netscape HTTP Cookie File\n"
                    ".youtube.com\tTRUE\t/\tTRUE\t1999999999\tSAPISID\tabc123token\n"
                    ".youtube.com\tTRUE\t/\tTRUE\t1999999999\t__Secure-3PSID\txyz987token\n"
                )
                ok_c, _, _ = ytf.save_cookies_text(valid_cookies)
                self.assertTrue(ok_c)

                def _fake_ytm_acct_ok(feed_kind="supermix", start=1, count=20, cookie_args=None):
                    return ([
                        {"id": "SuperMix001", "title": "My Supermix Song", "artist": "Daft Punk", "duration": 260, "reason": "YT Music Supermix"}
                    ], "")

                acct_tr, acct_err, acct_meta = ytm.fetch_ytm_personalized_feed(
                    cfg={"feed_mode": "auto", "cookie_source": "file"},
                    force_refresh=True,
                    fetch_account_fn=_fake_ytm_acct_ok,
                )
                self.assertEqual(acct_err, "")
                self.assertEqual(acct_tr[0]["id"], "SuperMix001")
                self.assertIn("YT Music Supermix", acct_meta["source_label"])

                def _fake_ytm_acct_exp(feed_kind="supermix", start=1, count=20, cookie_args=None):
                    return ([], "YouTube cookies expired or not signed in")

                fb_tr, fb_err, fb_meta = ytm.fetch_ytm_personalized_feed(
                    cfg={"feed_mode": "auto", "cookie_source": "file"},
                    force_refresh=True,
                    liked=[t2],
                    fetch_mix_fn=_fake_mix,
                    search_fn=_fake_search,
                    fetch_account_fn=_fake_ytm_acct_exp,
                )
                self.assertEqual(fb_err, "")
                self.assertTrue(fb_tr)
                self.assertEqual(fb_meta["source"], "local")
                self.assertIn("fell back", fb_meta["fallback_notice"])

                # 6. Render FOR YOU tab (with reason + Load More) & Listening History across all 4 display sizes
                p_st = {
                    "track": t1, "state": "playing", "elapsed": 42.0, "spectrum": [0.4] * 16,
                    "peaks": [0.7] * 16, "visualizer": "peaks", "queue": [t1, t2], "current_idx": 0,
                    "volume_pct": 80, "muted": False, "eq_name": "Warm Ember", "speed": 1.0,
                    "loudnorm": False, "sleep_label": "", "shuffle": False, "repeat_mode": "all",
                    "auto_radio": True, "liked": True, "offline_now": False, "audio_label": "BT: Sony XM5",
                }
                sample_fy = [
                    {"id": "Consensus01", "title": "Shared Synth Hit", "artist": "Carpenter Brut", "duration": 245, "reason": "Because you played Kavinsky"},
                    {"id": "LikeMix0001", "title": "Midnight Drive", "artist": "Timecop1983", "duration": 230, "reason": "Similar to Liked: The Midnight"},
                ]
                for w, h in ((640, 360), (320, 170), (240, 240), (128, 128)):
                    im_fy = ytm_ui.render_ytmusic_frame(
                        w, h, "for_you", p_st,
                        {"items": sample_fy, "cursor": 0, "scroll": 0, "has_load_more": True, "banner": "FOR YOU [SMART] • Local Smart Mix • F:Source U:Refresh"},
                    )
                    self.assertEqual(im_fy.size, (w, h))
                    im_lm = ytm_ui.render_ytmusic_frame(
                        w, h, "for_you", p_st,
                        {"items": sample_fy, "cursor": 2, "scroll": 0, "has_load_more": True, "banner": "FOR YOU [SMART] • Local Smart Mix"},
                    )
                    self.assertEqual(im_lm.size, (w, h))
                    im_hist = ytm_ui.render_ytmusic_frame(
                        w, h, "lists", p_st,
                        {"items": [t1, t2], "cursor": 0, "scroll": 0, "in_playlist": True, "in_history": True, "banner": "LISTENING HISTORY • 2 tracks • X:Remove S:Clear All"},
                    )
                    self.assertEqual(im_hist.size, (w, h))

                # 7. User Guide mentions YT Music For You Mix, Listening History clear & shared cookies
                ytm_guide = next(ch for ch in rj_guide.GUIDE_TOPICS if ch["id"] == "youtube_music")
                guide_blob = " ".join(str(b) for b in ytm_guide["blocks"])
                self.assertIn("FOR YOU", guide_blob)
                self.assertIn("Clear All Listening History", guide_blob)
                self.assertIn("cookies.txt", guide_blob)
            finally:
                ytf._DATA_DIR_OVERRIDE = old_override
                ytm.invalidate_music_feed_cache()
                ytf.invalidate_feed_cache()
                if old_base is None:
                    os.environ.pop("RJ_BASE_DIR", None)
                else:
                    os.environ["RJ_BASE_DIR"] = old_base

    def test_youtube_anti_stutter_pacer_keepalive_mpv_and_for_you_crash_fix(self):
        import ast
        import builtins
        import collections
        import http.server
        import tempfile
        import threading
        from unittest import mock
        import hdmi_shm
        import rj_youtube_feed as ytf
        from payloads import _audio_helper as ah
        from payloads.utilities import youtube as yt

        # 1. VideoFramePacer spreads a 3-frame PipeWire/BT audio burst evenly instead of dropping 2/3 frames
        pacer = yt.VideoFramePacer(target_fps=24.0, max_queued=8)
        fq = collections.deque()
        free = collections.deque()
        t0 = 100.0
        for i in range(3):
            due = pacer.schedule_due(t0 + i * 0.002, delay_s=0.0)
            fq.append((due, i + 1, bytearray(16)))
        # At t0, only frame 1 is popped; frames 2 & 3 stay scheduled ~39ms apart
        s1, b1, w1 = pacer.pop_due(fq, t0, free, 8)
        self.assertEqual(s1, 1)
        self.assertIsNotNone(b1)
        self.assertEqual(len(fq), 2)
        s2_early, b2_early, _ = pacer.pop_due(fq, t0 + 0.010, free, 8)
        self.assertIsNone(s2_early)
        self.assertIsNone(b2_early)
        s2, b2, _ = pacer.pop_due(fq, t0 + 0.042, free, 8)
        self.assertEqual(s2, 2)
        self.assertIsNotNone(b2)
        s3, b3, _ = pacer.pop_due(fq, t0 + 0.085, free, 8)
        self.assertEqual(s3, 3)
        self.assertIsNotNone(b3)
        # Stale backlog (>2.5 frames overdue) is dropped cleanly to preserve A/V sync
        for i in range(4):
            fq.append((t0 + 1.0 + i * 0.04, 10 + i, bytearray(16)))
        s_late, _, _ = pacer.pop_due(fq, t0 + 1.5, free, 8)
        self.assertEqual(s_late, 13)
        self.assertGreaterEqual(len(free), 3)

        # 2. _KeepAliveHttpClient reuses one persistent HTTP/1.1 connection across sequential Range requests
        blob = bytes((i * 29) & 0xFF for i in range(64 * 1024))

        class _KeepAliveHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                rng = self.headers.get("Range", "")
                lo_s, hi_s = rng[6:].split("-", 1)
                lo, hi = int(lo_s), min(len(blob) - 1, int(hi_s))
                chunk = blob[lo : hi + 1]
                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Range", f"bytes {lo}-{hi}/{len(blob)}")
                self.send_header("Content-Length", str(len(chunk)))
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.write(chunk)

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _KeepAliveHandler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        cli = yt._KeepAliveHttpClient(timeout=5.0)
        try:
            url = f"http://127.0.0.1:{port}/v"
            got_all = bytearray()
            for idx in range(4):
                lo = idx * 16384
                hi = lo + 16383
                resp = cli.open_range(url, f"bytes={lo}-{hi}")
                self.assertEqual(resp.status, 206)
                got_all.extend(resp.read())
                cli.finish(resp, fully_read=True)
            self.assertEqual(bytes(got_all), blob)
            self.assertEqual(cli.connections_created, 1)
            self.assertEqual(cli.connections_reused, 3)
        finally:
            cli.close_all()
            srv.shutdown()
            srv.server_close()

        # 3. Codec / ffmpeg / render-size / audio-helper / HDMI SHM flags guarantees
        q720 = dict(yt.STREAM_QUALITIES)["720p"]
        self.assertIn("vcodec!^=av01", q720)
        self.assertIn("vcodec^=vp9", q720)
        # Auto mode preserves crisp full 640x360 (exact 2x/3x divisor of 720p/1080p) for 360p-1080p
        self.assertEqual(yt.compute_video_render_size("720p", 640, 360, "auto", is_hdmi=True), (640, 360))
        self.assertEqual(yt.compute_video_render_size("1080p", 640, 360, "auto", is_hdmi=True), (640, 360))
        self.assertEqual(yt.compute_video_render_size("720p", 640, 360, "balanced", is_hdmi=True), (480, 270))
        self.assertEqual(yt.compute_video_render_size("720p", 640, 360, "sharp", is_hdmi=True), (640, 360))
        cmd_v = yt.build_video_ffmpeg_cmd(
            "http://127.0.0.1:9999/v", "http://127.0.0.1:9999/a",
            640, 360, "rgb24", 30, ["-f", "pulse", "-buffer_duration", "180", "-minreq", "3840", "default"],
            use_realtime=False,
        )
        self.assertEqual(cmd_v[cmd_v.index("-threads") + 1], "2")
        self.assertEqual(cmd_v[cmd_v.index("-ar") + 1], "48000")
        self.assertIn("-probesize", cmd_v)
        self.assertIn("flags=bilinear", cmd_v[cmd_v.index("-vf") + 1])
        with mock.patch.object(ah, "_is_pulse_running", return_value=True), \
             mock.patch.object(ah, "_has_pulse_socket", return_value=True), \
             mock.patch.object(ah, "_ffmpeg_supports_pulse", return_value=True):
            a_args = ah.get_ffmpeg_audio_args(buffer_ms=180)
            self.assertIn("180", a_args)
            self.assertIn("-minreq", a_args)
            self.assertEqual(a_args[a_args.index("-minreq") + 1], "3840")
            with mock.patch.object(ah, "_audio_server_popen_kwargs", return_value={"env": {}, "user": "pi"}) as m_drop:
                kw = ah.get_audio_popen_kwargs(["mpv", "--vo=drm", "video.mkv"], allow_user_drop=True)
                self.assertEqual(kw.get("user"), "pi")
                m_drop.assert_called_once()

        # hdmi_shm.peek_seq_and_flags exposes FLAG_NO_HUD cleanly without copying payload
        with tempfile.TemporaryDirectory() as td:
            old_shm = hdmi_shm.SHM_PATH
            hdmi_shm.close_mappings()
            hdmi_shm.SHM_PATH = os.path.join(td, "test_hdmi.shm")
            try:
                seq = hdmi_shm.publish_frame(b"\x10" * (4 * 4 * 3), 4, 4, flags=hdmi_shm.FLAG_NO_HUD)
                p_seq, p_flags = hdmi_shm.peek_seq_and_flags()
                self.assertEqual(p_seq, seq)
                self.assertTrue(p_flags & hdmi_shm.FLAG_NO_HUD)
            finally:
                hdmi_shm.close_mappings()
                hdmi_shm.SHM_PATH = old_shm

        # 4. For You Feed crash regression guard: zero undefined names across youtube.py + None-safe InnerTube parser
        yt_path = os.path.join(str(ROOT), "payloads", "utilities", "youtube.py")
        with open(yt_path, "r", encoding="utf-8") as fh:
            yt_src = fh.read()
        self.assertNotIn("load_search_history", yt_src)
        self.assertIn("search_history=load_history()", yt_src)
        self.assertIn("_evdev_keys.start(as_host=True, grab=True)", yt_src)

        # Static scope audit ensures no NameError can sneak back into YouTube or YT Music
        builtin_names = set(dir(builtins)) | {"__file__"}
        for rel in ("payloads/utilities/youtube.py", "rj_youtube_data.py", "rj_youtube_stream.py", "rj_youtube_mpv.py", "rj_youtube_feed.py", "payloads/utilities/youtube_music.py", "rj_ytmusic.py"):
            with open(os.path.join(str(ROOT), rel), "r", encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=rel)
            undef = []

            def _audit_scope(node, parent_known):
                loc = set()

                def _bind(n):
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        loc.add(n.name)
                        return
                    if isinstance(n, ast.Import):
                        for a in n.names:
                            loc.add(a.asname or a.name.split(".")[0])
                        return
                    if isinstance(n, ast.ImportFrom):
                        for a in n.names:
                            if a.name != "*":
                                loc.add(a.asname or a.name)
                        return
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                        loc.add(n.id)
                    if isinstance(n, ast.ExceptHandler) and n.name:
                        loc.add(n.name)
                    if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.Lambda)):
                        return
                    for ch in ast.iter_child_nodes(n):
                        _bind(ch)

                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
                        loc.add(arg.arg)
                    if node.args.vararg:
                        loc.add(node.args.vararg.arg)
                    if node.args.kwarg:
                        loc.add(node.args.kwarg.arg)
                    for st in node.body:
                        _bind(st)
                elif isinstance(node, (ast.Module, ast.ClassDef)):
                    for st in node.body:
                        _bind(st)
                elif isinstance(node, ast.Lambda):
                    for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
                        loc.add(arg.arg)
                elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                    for g in node.generators:
                        _bind(g.target)

                known = parent_known | loc | builtin_names

                def _chk(n):
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for d in n.args.defaults + n.args.kw_defaults:
                            if d is not None:
                                _chk(d)
                        for dec in n.decorator_list:
                            _chk(dec)
                        _audit_scope(n, known)
                        return
                    if isinstance(n, ast.ClassDef):
                        for b in n.bases:
                            _chk(b)
                        _audit_scope(n, known)
                        return
                    if isinstance(n, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                        _audit_scope(n, known)
                        return
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in known:
                        undef.append(f"{rel}:{n.lineno}:{n.id}")
                        return
                    for ch in ast.iter_child_nodes(n):
                        _chk(ch)

                if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    for st in node.body:
                        _chk(st)
                elif isinstance(node, ast.Lambda):
                    _chk(node.body)
                elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
                    for g in node.generators:
                        _chk(g.iter)
                        for iff in g.ifs:
                            _chk(iff)
                    _chk(node.elt)
                elif isinstance(node, ast.DictComp):
                    for g in node.generators:
                        _chk(g.iter)
                        for iff in g.ifs:
                            _chk(iff)
                    _chk(node.key)
                    _chk(node.value)

            _audit_scope(tree, set())
            self.assertEqual(undef, [], f"Undefined symbol(s) found: {undef}")

        # None-valued nested InnerTube fields must never raise AttributeError
        weird_lockup = {
            "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
            "contentId": "AbCdEf12345",
            "metadata": {
                "lockupMetadataViewModel": {
                    "title": {"content": "Resilient Video"},
                    "metadata": {"contentMetadataViewModel": None},
                    "image": {"decoratedAvatarViewModel": None},
                }
            },
        }
        parsed_w = ytf._extract_lockup_item(weird_lockup, reason="Test")
        self.assertIsNotNone(parsed_w)
        self.assertEqual(parsed_w["id"], "AbCdEf12345")
        self.assertEqual(ytf.parse_innertube_next_payload({"contents": {"twoColumnWatchNextResults": None}}), [])

if __name__ == "__main__":
    unittest.main()
