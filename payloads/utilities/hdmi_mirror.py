#!/usr/bin/env python3
"""
RaspyJack Payload -- HDMI Mirror
==================================
Author: 7h30th3r0n3

Mirrors the LCD framebuffer to HDMI output in real-time.
Runs as a background process — does NOT take over the LCD.
Launch to start, launch again to stop.

Requires: HDMI cable connected
"""

import os
import sys
import time
import signal
import subprocess
import mmap

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

MIRROR_PID_FILE = "/tmp/raspyjack_hdmi_mirror.pid"

import RPi.GPIO as GPIO
import LCD_1in44
import LCD_Config
from PIL import Image
from payloads._display_helper import ScaledDraw, scaled_font

PINS = {"UP":6,"DOWN":19,"LEFT":5,"RIGHT":26,"OK":13,"KEY1":21,"KEY2":20,"KEY3":16}
GPIO.setmode(GPIO.BCM)
for p in PINS.values():
    GPIO.setup(p, GPIO.IN, pull_up_down=GPIO.PUD_UP)
LCD = LCD_1in44.LCD()
LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
W, H = LCD.width, LCD.height
font = scaled_font(12)
font_sm = scaled_font(7)

def _show_msg(title, msg, col=(0, 255, 100)):
    img = Image.new("RGB", (W, H), "black")
    d = ScaledDraw(img)
    d.text((64, 45), title, font=font, fill=col, anchor="mm")
    d.text((64, 65), msg, font=font_sm, fill=(100, 100, 100), anchor="mm")
    LCD.LCD_ShowImage(img, 0, 0)
    time.sleep(1.5)


def _find_lcd_fb():
    for i in range(4):
        try:
            with open(f"/sys/class/graphics/fb{i}/name") as f:
                if any(n in f.read() for n in ("st7789v_m5st", "panel-mipi-dbi")):
                    return f"/dev/fb{i}", 320, 170
        except Exception:
            pass
    return None, 0, 0


def _check_hdmi():
    try:
        with open("/sys/class/drm/card0-HDMI-A-1/status") as f:
            return "connected" in f.read().lower()
    except Exception:
        return False


def _is_running():
    try:
        with open(MIRROR_PID_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True, pid
    except Exception:
        return False, 0


def _mirror_daemon(fb_path, fb_w, fb_h):
    """Daemon: read LCD fb and pipe to mpv on HDMI. No LCD interaction."""
    fb_size = fb_w * fb_h * 2

    # Free HDMI: stop display manager and console
    subprocess.run(["systemctl", "stop", "lightdm"], capture_output=True)
    subprocess.run(["systemctl", "stop", "getty@tty1"], capture_output=True)
    time.sleep(0.5)

    cmd = [
        "mpv", "--vo=drm", "--really-quiet",
        "--no-terminal", "--no-osc",
        f"--demuxer-rawvideo-w={fb_w}",
        f"--demuxer-rawvideo-h={fb_h}",
        "--demuxer-rawvideo-mp-format=rgb565le",
        "--demuxer-rawvideo-fps=15",
        "--demuxer=rawvideo",
        "-",
    ]

    mpv = subprocess.Popen(
        cmd, stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    fd = os.open(fb_path, os.O_RDONLY)
    fb = mmap.mmap(fd, fb_size, mmap.MAP_SHARED, mmap.PROT_READ)

    # Write our PID
    with open(MIRROR_PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    frame_interval = 1.0 / 15

    try:
        while mpv.poll() is None:
            t0 = time.monotonic()
            fb.seek(0)
            frame = fb.read(fb_size)
            try:
                mpv.stdin.write(frame)
                mpv.stdin.flush()
            except BrokenPipeError:
                break
            elapsed = time.monotonic() - t0
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        fb.close()
        os.close(fd)
        mpv.kill()
        mpv.wait(timeout=3)
        try:
            os.unlink(MIRROR_PID_FILE)
        except Exception:
            pass



def _mirror_daemon_jpeg():
    """Daemon: read raw LCD RGB frame, scale preserving aspect ratio, write to HDMI fb."""
    RAW_PATH = "/dev/shm/raspyjack_raw.rgb"
    import hdmi_host

    subprocess.run(["systemctl", "stop", "lightdm"], capture_output=True)
    subprocess.run(["systemctl", "stop", "getty@tty1"], capture_output=True)
    time.sleep(0.5)

    with open(MIRROR_PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    hdmi_fb = hdmi_host._find_hdmi_fb_device()
    if not hdmi_fb or not os.path.exists(hdmi_fb):
        return

    fd = os.open(hdmi_fb, os.O_RDWR)
    info = hdmi_host._query_fb_info(fd)
    fb_size = max(info["smem_len"], info["line_length"] * info["height"])
    fb = mmap.mmap(fd, fb_size, mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)

    lcd_w, lcd_h = 128, 128
    try:
        import LCD_1in44
        lcd_w, lcd_h = LCD_1in44.LCD_WIDTH, LCD_1in44.LCD_HEIGHT
    except Exception:
        pass
    raw_size = lcd_w * lcd_h * 3
    frame_interval = 1.0 / 15

    try:
        while True:
            t0 = time.monotonic()
            try:
                with open(RAW_PATH, "rb") as rf:
                    raw = rf.read()
                if len(raw) == raw_size:
                    pixels = hdmi_host.convert_rgb24_to_fb(
                        raw, lcd_w, lcd_h,
                        info["width"], info["height"],
                        info["bpp"], info["line_length"],
                        red_off=info["red_off"], green_off=info["green_off"], blue_off=info["blue_off"],
                        scale_mode="fit",
                    )
                    fb.seek(0)
                    fb.write(pixels)
            except Exception:
                pass
            elapsed = time.monotonic() - t0
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        fb.close()
        os.close(fd)
        try:
            os.unlink(MIRROR_PID_FILE)
        except Exception:
            pass


def main():
    if getattr(LCD, "display_type", "") == "HDMI":
        _show_msg("Native HDMI", "Already active!", (0, 255, 100))
        return 0
    running, pid = _is_running()

    if running:
        # Toggle OFF: kill the mirror daemon
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        subprocess.run(["pkill", "-9", "mpv"], capture_output=True)
        try:
            os.unlink(MIRROR_PID_FILE)
        except Exception:
            pass
        _show_msg("HDMI Mirror", "OFF", (255, 60, 60))
        return 0

    # Toggle ON
    if not _check_hdmi():
        _show_msg("HDMI Mirror", "No HDMI cable!", (255, 200, 0))
        return 1

    fb_path, fb_w, fb_h = _find_lcd_fb()
    use_jpeg = fb_path is None

    if use_jpeg and not os.path.exists("/dev/shm/raspyjack_raw.rgb") and not os.path.exists("/dev/shm/raspyjack_last.jpg"):
        _show_msg("HDMI Mirror", "No LCD data!", (255, 60, 60))
        return 1

    pid = os.fork()
    if pid > 0:
        mode = "JPEG" if use_jpeg else "FB"
        _show_msg("HDMI Mirror", f"ON ({mode})", (0, 255, 100))
        return 0

    # Child: become daemon
    os.setsid()
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    if use_jpeg:
        _mirror_daemon_jpeg()
    else:
        _mirror_daemon(fb_path, fb_w, fb_h)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
