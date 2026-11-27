#!/usr/bin/env python3
"""Unit and integration tests for Bluetooth manager, headless PipeWire/PulseAudio audio routing, and Audio Manager."""

import os
import pathlib
import sys
import time
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

if __name__ == "__main__":
    unittest.main()
