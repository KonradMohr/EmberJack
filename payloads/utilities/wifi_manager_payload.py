#!/usr/bin/env python3
"""
RaspyJack WiFi Manager Payload
=============================
Launch the WiFi management interface in RaspyJack

BUTTON LAYOUT:
- Automatic launch of WiFi LCD interface
- Full WiFi network management
- Profile creation and connection
- Interface status and configuration

FEATURES:
- Scan and connect to WiFi networks
- Save network profiles with passwords
- Manage multiple WiFi dongles
- Interface selection for tools
- Connection status monitoring

This payload provides complete WiFi management for RaspyJack
while maintaining full ethernet compatibility.
"""

import os
import sys
import subprocess

_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

try:
    import rj_env
    _BASE_DIR = rj_env.get_base_dir()
except Exception:
    pass

_WIFI_DIR = os.path.join(_BASE_DIR, "wifi")
if _WIFI_DIR not in sys.path:
    sys.path.append(_WIFI_DIR)


def main():
    """Launch the WiFi management interface."""
    try:
        print("Launching RaspyJack WiFi Manager...")
        wifi_interface_path = os.path.join(_WIFI_DIR, "wifi_lcd_interface.py")

        if not os.path.exists(wifi_interface_path):
            print(f"WiFi management system not found at: {wifi_interface_path}")
            return False

        result = subprocess.run([
            sys.executable, wifi_interface_path
        ], cwd=_BASE_DIR, capture_output=False)

        print(f"WiFi manager exited with code: {result.returncode}")
        return result.returncode == 0
        
    except KeyboardInterrupt:
        print("\n⏹️  WiFi manager interrupted by user")
        return True
    except Exception as e:
        print(f"❌ Error launching WiFi manager: {e}")
        return False

if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)