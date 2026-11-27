#!/usr/bin/env python3
"""
RaspyJack HDMI Launcher
-----------------------
Starts the persistent HDMI display & keyboard host (hdmi_host.py) alongside
raspyjack.py so both the main UI and all 351 child payload subprocesses render
seamlessly to your HDMI monitor with full normal keyboard support.

Examples:
    python3 run_hdmi.py
    python3 run_hdmi.py --fullscreen --resolution 640x360
    python3 run_hdmi.py --resolution 320x180 --scale integer
    sudo -E python3 run_hdmi.py --fullscreen
    sudo python3 run_hdmi.py --backend fbdev
"""

import argparse
import os
import signal
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import hdmi_shm


def parse_args():
    p = argparse.ArgumentParser(description="Launch EmberJack with full HDMI monitor & keyboard support")
    p.add_argument("--resolution", default="", help="Logical canvas WxH (e.g. 640x360, 320x180, 480x480)")
    p.add_argument("--fullscreen", action="store_true", help="Start HDMI window in fullscreen")
    p.add_argument("--windowed", action="store_true", help="Start HDMI window in resizable window mode")
    p.add_argument("--backend", choices=["auto", "sdl2", "gtk", "fbdev"], default="", help="Display backend")
    p.add_argument("--scale", choices=["fit", "integer", "stretch"], default="", help="Aspect scaling mode")
    p.add_argument("--fps", type=int, default=0, help="Target presenter refresh rate (5-60)")
    p.add_argument("--hud", dest="hud", action="store_true", default=None, help="Show bottom key-hint HUD bar")
    p.add_argument("--no-hud", dest="hud", action="store_false", help="Hide bottom key-hint HUD bar")
    p.add_argument("--no-root", action="store_true", help="Allow running without root privileges")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    env = os.environ.copy()
    env["RJ_BASE_DIR"] = BASE_DIR
    env["RJ_DISPLAY_TYPE"] = "HDMI"
    env["RJ_HOST_MANAGED_EXTERNALLY"] = "1"
    env["PYTHONPATH"] = BASE_DIR + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    if args.resolution:
        env["RJ_HDMI_RES"] = args.resolution
    if args.fullscreen:
        env["RJ_HDMI_FULLSCREEN"] = "1"
    elif args.windowed:
        env["RJ_HDMI_FULLSCREEN"] = "0"
    if args.backend:
        env["RJ_HDMI_BACKEND"] = args.backend
    if args.scale:
        env["RJ_HDMI_SCALE"] = args.scale
    if args.fps > 0:
        env["RJ_HDMI_FPS"] = str(args.fps)
    if args.hud is not None:
        env["RJ_HDMI_HUD"] = "1" if args.hud else "0"
    if args.no_root or os.getuid() != 0:
        env["RJ_ALLOW_NONROOT"] = "1"

    os.environ.update(env)

    # Start persistent HDMI host presenter
    host_proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, "hdmi_host.py")],
        cwd=BASE_DIR,
        env=env,
    )

    # Wait briefly for host PID registration
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline:
        running, _ = hdmi_shm.is_host_running()
        if running or host_proc.poll() is not None:
            break
        time.sleep(0.05)

    if host_proc.poll() is not None:
        print(f"[run_hdmi] HDMI host exited prematurely (code {host_proc.returncode}).", file=sys.stderr)
        return host_proc.returncode or 1

    # Start main RaspyJack UI
    rj_proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, "raspyjack.py"), "--hdmi"],
        cwd=BASE_DIR,
        env=env,
    )

    def _forward_stop(*_a):
        for proc in (rj_proc, host_proc):
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

    signal.signal(signal.SIGINT, _forward_stop)
    signal.signal(signal.SIGTERM, _forward_stop)

    try:
        while True:
            if host_proc.poll() is not None:
                # User closed the HDMI window -> shut down RaspyJack cleanly
                if rj_proc.poll() is None:
                    rj_proc.terminate()
                    try:
                        rj_proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        rj_proc.kill()
                break
            if rj_proc.poll() is not None:
                break
            time.sleep(0.1)
    finally:
        for proc in (rj_proc, host_proc):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        hdmi_shm.stop_host()

    return rj_proc.returncode if rj_proc.returncode is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
