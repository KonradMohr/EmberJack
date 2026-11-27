#!/usr/bin/env python3
"""Unit and integration tests for GUI App Launcher, User Guide, efficiency optimizations, UI modernization, and repository hygiene."""

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
        self.assertEqual(len(documented_ids), 328)

        # Full-text search matches deep tools and ranks direct tool matches first + auto-scrolls!
        self.assertTrue(any(t["id"] == "kbd_mouse" for t in rj_guide.filter_topics(topics, query="ctrl+alt+m")))
        self.assertTrue(any(t["id"] == "kbd_mouse" for t in rj_guide.filter_topics(topics, query="scrolllock")))
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


class TestRepositoryHygiene(unittest.TestCase):
    """Repository-wide AST hygiene, portability helpers, junk removal, and WebUI split checks."""

    SKIP_DIRS = {".git", "__pycache__", "Responder", "DNSSpoof", "Navarro", "vendor"}

    def _first_party_py_files(self):
        out = []
        for dirpath, dirnames, filenames in os.walk(str(ROOT)):
            dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS]
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    out.append(os.path.join(dirpath, fn))
        return out

    def test_zero_syntax_errors_bare_excepts_and_duplicate_top_defs(self):
        import ast

        syntax_errs = []
        bare_excepts = []
        dup_defs = []

        for fp in self._first_party_py_files():
            rel = os.path.relpath(fp, str(ROOT))
            try:
                with open(fp, "r", encoding="utf-8") as fh:
                    tree = ast.parse(fh.read(), filename=rel)
            except SyntaxError as exc:
                syntax_errs.append(f"{rel}: {exc}")
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and node.type is None:
                    bare_excepts.append(f"{rel}:{node.lineno}")

            seen = {}
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name in seen:
                        dup_defs.append(f"{rel}:{seen[node.name]}&{node.lineno}:{node.name}")
                    else:
                        seen[node.name] = node.lineno

        self.assertEqual(syntax_errs, [], f"Syntax errors found: {syntax_errs}")
        self.assertEqual(bare_excepts, [], f"Bare except clauses found: {bare_excepts}")
        self.assertEqual(dup_defs, [], f"Duplicate top-level defs found: {dup_defs}")

    def test_rj_env_path_helpers_and_cleaned_orphans(self):
        import tempfile
        import rj_env

        old = os.environ.get("RJ_BASE_DIR")
        with tempfile.TemporaryDirectory() as td:
            os.environ["RJ_BASE_DIR"] = td
            try:
                self.assertEqual(rj_env.get_path("payloads", "utilities"), os.path.join(td, "payloads", "utilities"))
                self.assertEqual(rj_env.get_loot_dir("YouTube"), os.path.join(td, "loot", "YouTube"))
                self.assertEqual(rj_env.get_config_dir("gui_conf.json"), os.path.join(td, "config", "gui_conf.json"))
            finally:
                if old is None:
                    os.environ.pop("RJ_BASE_DIR", None)
                else:
                    os.environ["RJ_BASE_DIR"] = old

        # Confirmed junk / orphaned files must stay removed (runtime logs like wifi/wifi.log are gitignored)
        for gone in (
            ".DS_Store",
            "payloads/wifi/deauth_debug.log",
            "index.html",
            "input_events.py",
            "payloads/utilities/birdnet.py",
        ):
            self.assertFalse(os.path.exists(os.path.join(str(ROOT), gone)), f"Orphan still present: {gone}")
        with open(os.path.join(str(ROOT), ".gitignore"), "r", encoding="utf-8") as fh:
            gi_txt = fh.read()
        self.assertIn("*.log", gi_txt)
        self.assertIn(".DS_Store", gi_txt)

    def test_web_managers_split_reexports(self):
        import rj_web_managers as wm
        import web_server as ws

        self.assertIs(ws._ism_manager, wm._ism_manager)
        self.assertIs(ws._scanner_manager, wm._scanner_manager)
        self.assertIs(ws._honeypot_runner, wm._honeypot_runner)
        self.assertIs(ws._read_cpu_percent, wm._read_cpu_percent)
        self.assertIs(ws._tailscale_status, wm._tailscale_status)

if __name__ == "__main__":
    unittest.main()
