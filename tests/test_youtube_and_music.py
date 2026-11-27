#!/usr/bin/env python3
"""Unit and integration tests for YouTube player, YouTube Music, stream preloader, and mpv Obsidian Ember OSD."""

import os
import pathlib
import sys
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
        import json
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

        # Settings defaults, v1->v2 migration & persistence of preload_sec, balanced render_scale, player_engine
        old_base = os.environ.get("RJ_BASE_DIR")
        with tempfile.TemporaryDirectory() as td:
            os.environ["RJ_BASE_DIR"] = td
            try:
                cfg0 = yt.load_settings()
                self.assertEqual(cfg0["preload_sec"], 5)
                self.assertEqual(cfg0["player_engine"], "auto")
                self.assertEqual(cfg0["fps_wide"], 30)
                cfg0["preload_sec"] = -1
                cfg0["render_scale"] = "balanced"
                cfg0["player_engine"] = "builtin"
                cfg0["fps_wide"] = 24
                yt.save_settings(cfg0)
                cfg1 = yt.load_settings()
                self.assertEqual(cfg1["preload_sec"], -1)
                self.assertEqual(cfg1["render_scale"], "balanced")
                self.assertEqual(cfg1["player_engine"], "builtin")
                self.assertEqual(cfg1["fps_wide"], 24)
                # Legacy v1 settings file (without settings_version) migrates 24fps+builtin -> 30fps+auto once
                legacy_p = os.path.join(yt.get_youtube_data_dir(), "settings.json")
                with open(legacy_p, "w", encoding="utf-8") as fh:
                    json.dump({"fps_wide": 24, "player_engine": "builtin", "quality_idx": 4}, fh)
                cfg_mig = yt.load_settings()
                self.assertEqual(cfg_mig["fps_wide"], 30)
                self.assertEqual(cfg_mig["player_engine"], "auto")
                self.assertEqual(cfg_mig["quality_idx"], 4)
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

        # 1. Home menu has balanced 10-card grid including For You Feed, Subscriptions, Playlists & Watch History
        home_keys = [k for _, k, _ in yt.HOME_MENU_ITEMS]
        self.assertEqual(len(home_keys), 10)
        self.assertIn("feed", home_keys)
        self.assertIn("subs", home_keys)
        self.assertIn("playlists", home_keys)
        self.assertIn("watch_history", home_keys)

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

        # Render Load More Videos button row across all 4 screen sizes (highlighted + preview card)
        lm_items = [
            {"id": "v1", "title": "First Clip", "channel": "Demo", "duration": 180, "views": 5000},
            {"id": "v2", "title": "Second Clip", "channel": "Demo", "duration": 420, "views": 12000},
        ]
        for w, h in ((640, 360), (320, 170), (240, 240), (128, 128)):
            im_lm = yt.render_results_view(
                w, h, "RESULTS", "lofi", lm_items, cursor=len(lm_items), scroll=0,
                has_load_more=True,
            )
            self.assertEqual(im_lm.size, (w, h))

        # Verify paginated fetch_channel_videos & fetch_playlist_details build --playlist-start/--playlist-end cleanly
        import inspect
        self.assertIn("start", inspect.signature(yt.fetch_channel_videos).parameters)
        self.assertIn("start", inspect.signature(yt.fetch_playlist_details).parameters)

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


class TestMpvOsdAndIpc(unittest.TestCase):
    """Tests for rj_youtube_mpv ASS OSD rendering, IPC socket protocol, and YouTube module split."""

    def test_escape_ass_and_osd_builders(self):
        import rj_youtube_mpv as ympv

        # Escaping prevents ASS override tag injection and flattens newlines safely
        escaped = ympv.escape_ass("A {bold} \\ test\nLine 2")
        self.assertNotIn("{bold}", escaped)
        self.assertIn(r"\{bold\}", escaped)
        self.assertIn(r"\\", escaped)
        self.assertNotIn("\n", escaped)
        self.assertIn("Line 2", escaped)

        # Full Obsidian Ember OSD overlay contains vector drawings, title, channel, badges, timestamps, toast & paused badge
        ass = ympv.build_mpv_osd_ass(
            title="Smooth 720p Demo {Raw}",
            elapsed=125.0,
            duration=300.0,
            volume_pct=85,
            muted=False,
            paused=True,
            quality_label="720p",
            audio_label="BT Headphones",
            channel="EmberJack Lab",
            liked=True,
            watch_later=True,
            toast="Speed: 1.25x",
            playlist_pos="2/5",
            buffered_sec=28.0,
            speed=1.25,
            loop_one=True,
            captions_on=True,
            show_bars=True,
        )
        self.assertIn("Smooth 720p Demo", ass)
        self.assertIn("EmberJack Lab", ass)
        self.assertIn("BT Headphones", ass)
        self.assertIn("2:05 / 5:00", ass)
        self.assertIn("720P", ass)
        self.assertIn("1.25x", ass)
        self.assertIn("LOOP", ass)
        self.assertIn("CC", ass)
        self.assertIn("LIKED", ass)
        self.assertIn("WL", ass)
        self.assertIn("PAUSED", ass)
        self.assertIn("VOL 85%", ass)
        self.assertIn("Speed: 1.25x", ass)
        self.assertIn("2/5", ass)
        self.assertIn(r"\p1", ass)  # ASS vector shapes for bars/pills

        # Playback Actions modal renders title, subtitle, highlighted selection, and items
        menu_ass = ympv.build_mpv_menu_ass(
            "PLAYBACK ACTIONS",
            "Smooth 720p Demo",
            [
                ("Resume Playback", "resume"),
                ("Quality: 720p", "quality"),
                ("Speed: 1.25x", "speed"),
                ("Toggle Captions", "cc"),
            ],
            cursor=2,
        )
        self.assertIn("PLAYBACK ACTIONS", menu_ass)
        self.assertIn("Smooth 720p Demo", menu_ass)
        self.assertIn("Speed: 1.25x", menu_ass)
        self.assertIn("Resume Playback", menu_ass)
        self.assertIn("Esc: Resume", menu_ass)

    def test_mpv_ipc_client_unix_socket_roundtrip(self):
        import json
        import socket
        import tempfile
        import threading
        import rj_youtube_mpv as ympv

        with tempfile.TemporaryDirectory() as td:
            sock_path = os.path.join(td, "mpv_test.sock")
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(sock_path)
            srv.listen(1)
            received = []

            def _serve():
                try:
                    conn, _ = srv.accept()
                    with conn:
                        buf = b""
                        # Send an unsolicited async event first to ensure request_id matching skips it
                        conn.sendall(b'{"event": "playback-restart"}\n')
                        while True:
                            chunk = conn.recv(4096)
                            if not chunk:
                                break
                            buf += chunk
                            while b"\n" in buf:
                                line, buf = buf.split(b"\n", 1)
                                if not line.strip():
                                    continue
                                msg = json.loads(line.decode("utf-8"))
                                received.append(msg)
                                rid = msg.get("request_id")
                                cmd = msg.get("command")
                                if isinstance(cmd, list) and cmd[:2] == ["get_property", "time-pos"]:
                                    resp = {"data": 42.5, "request_id": rid, "error": "success"}
                                else:
                                    resp = {"data": {}, "request_id": rid, "error": "success"}
                                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                except OSError:
                    pass

            th = threading.Thread(target=_serve, daemon=True)
            th.start()
            cli = ympv.MpvIpcClient(sock_path, timeout=2.0)
            try:
                self.assertTrue(cli.connect())
                self.assertTrue(cli.connected)
                pos = cli.get_property("time-pos", default=0.0)
                self.assertAlmostEqual(pos, 42.5)
                self.assertTrue(cli.set_osd_overlay(ympv.OSD_OVERLAY_ID, "{\\an5}Hello"))
                self.assertTrue(cli.clear_osd_overlay(ympv.OSD_OVERLAY_ID))
                self.assertTrue(cli.set_property("pause", True))
                # Sync round-trip ensures server has processed prior async fire-and-forget commands
                self.assertAlmostEqual(cli.get_property("time-pos", default=0.0), 42.5)
            finally:
                cli.close()
                srv.close()
                th.join(timeout=2.0)

            self.assertFalse(cli.connected)
            # Verify named osd-overlay command wire format matches mpv 0.35-0.41+
            osd_cmds = [m["command"] for m in received if isinstance(m.get("command"), dict)]
            self.assertEqual(len(osd_cmds), 2)
            self.assertEqual(osd_cmds[0]["name"], "osd-overlay")
            self.assertEqual(osd_cmds[0]["format"], "ass-events")
            self.assertEqual(osd_cmds[0]["res_x"], 1280)
            self.assertEqual(osd_cmds[0]["res_y"], 720)
            self.assertEqual(osd_cmds[1]["name"], "osd-overlay")
            self.assertEqual(osd_cmds[1]["format"], "none")

    def test_youtube_split_reexports_backwards_compatible(self):
        import rj_youtube_data as ydata
        import rj_youtube_stream as ystream
        from payloads.utilities import youtube as yt

        self.assertIs(yt._URL_CACHE, ydata._URL_CACHE)
        self.assertIs(yt.STREAM_QUALITIES, ydata.STREAM_QUALITIES)
        self.assertIs(yt.ThumbnailCache, ydata.ThumbnailCache)
        self.assertIs(yt.StreamPreloader, ystream.StreamPreloader)
        self.assertIs(yt.VideoFramePacer, ystream.VideoFramePacer)
        self.assertIs(yt._ChunkedRangeProxy, ystream._ChunkedRangeProxy)
        self.assertIs(yt.build_video_ffmpeg_cmd, ystream.build_video_ffmpeg_cmd)

if __name__ == "__main__":
    unittest.main()
