#!/usr/bin/env python3
"""
FAST WiFi Switcher - Responsive wlan0/wlan1 Control
===================================================
Fast, responsive interface for switching between wlan0 and wlan1.
No bullshit, no delays, just quick switching that actually works.

CONTROLS:
- KEY1: Switch to wlan0
- KEY2: Switch to wlan1  
- KEY3: Exit
- UP/DOWN: Quick toggle between interfaces
- CENTER: Show current status

FEATURES:
- Instant response to button presses
- Actual routing changes (not just display)
- Real-time verification
- Minimal delay interface
"""

import os
import sys
import time
import subprocess
import signal

_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _p in (_BASE_DIR, os.path.join(_BASE_DIR, "wifi")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rj_theme
_T = rj_theme.EMBER_THEME

try:
    # Import RaspyJack LCD functions (PROPER WAY)
    import LCD_1in44, LCD_Config
    import RPi.GPIO as GPIO
    from PIL import Image, ImageDraw, ImageFont
    from payloads._display_helper import ScaledDraw, scaled_font
    from payloads._input_helper import get_virtual_button
    from payloads._input_helper import _flip as _flip_btn
    
    # Import WiFi integration functions
    from wifi.raspyjack_integration import (
        set_raspyjack_interface,
        get_current_raspyjack_interface,
        get_interface_status,
        ensure_interface_default
    )
    
    IMPORTS_OK = True
except Exception as e:
    print(f"Import error: {e}")
    IMPORTS_OK = False

class FastWiFiSwitcher:
    def __init__(self):
        if not IMPORTS_OK:
            raise Exception("Required modules not available")
        
        # LCD setup - proper initialization
        LCD_Config.GPIO_Init()
        self.lcd = LCD_1in44.LCD()
        self.lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
        self.lcd.LCD_Clear()
        self.WIDTH, self.HEIGHT = self.lcd.width, self.lcd.height
        self.font = scaled_font()
        
        # GPIO setup with FAST response
        GPIO.setmode(GPIO.BCM)
        self.buttons = {
            'UP': 6,
            'DOWN': 19,
            'LEFT': 5,
            'RIGHT': 26,
            'CENTER': 13,
            'KEY1': 21,  # wlan0
            'KEY2': 20,  # wlan1
            'KEY3': 16   # exit
        }
        
        for pin in self.buttons.values():
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        
        self.running = True
        self.last_update = 0
        self.current_interface = self.get_current()
        
        # Button state tracking for responsiveness
        self.button_states = {pin: 1 for pin in self.buttons.values()}
        self.last_press_time = {pin: 0 for pin in self.buttons.values()}
        
        print("🚀 Fast WiFi Switcher initialized")
    
    def get_current(self):
        """Get current interface quickly."""
        try:
            return get_current_raspyjack_interface()
        except Exception:
            return "unknown"
    
    def show_fast(self, line1, line2="", line3="", line4="", color="white"):
        """Obsidian Ember card display for fast interface switching."""
        try:
            accent_map = {
                "green": _T["ok"],
                "yellow": _T["warn"],
                "red": _T["danger"],
                "white": _T["accent_bright"],
            }
            accent = accent_map.get(color, _T["accent_bright"])
            img = Image.new("RGB", (self.WIDTH, self.HEIGHT), _T["bg_base"])
            d = ScaledDraw(img)

            # Header bar
            d.rectangle((0, 0, 127, 15), fill=_T["bg_surface"])
            d.rectangle((0, 0, 2, 15), fill=_T["accent"])
            d.line((0, 15, 127, 15), fill=_T["border_accent"], width=1)
            d.text((5, 2), "WiFi Switcher", font=self.font, fill=_T["text_bright"])

            # Main status card
            d.rounded_rectangle((4, 20, 123, 96), radius=5, fill=_T["bg_surface"], outline=_T["border_subtle"])
            d.rectangle((4, 24, 6, 92), fill=accent)
            d.text((11, 25), (line1 or "")[:18], font=self.font, fill=accent)
            if line2:
                d.text((11, 42), line2[:18], font=self.font, fill=_T["text_bright"])
            if line3:
                d.text((11, 59), line3[:18], font=self.font, fill=_T["text_primary"])
            if line4:
                d.text((11, 76), line4[:18], font=self.font, fill=_T["text_muted"])

            # Footer keycaps
            d.rectangle((0, 112, 127, 127), fill=_T["bg_well"])
            d.line((0, 112, 127, 112), fill=_T["border_subtle"], width=1)
            d.text((3, 115), "K1:wl0 K2:wl1 K3:Exit", font=self.font, fill=_T["text_muted"])

            self.lcd.LCD_ShowImage(img, 0, 0)
        except Exception as e:
            print(f"LCD error: {e}")
            print(f"Display: {line1} {line2} {line3} {line4}")
    
    def check_interface_fast(self, interface):
        """Fast interface status check."""
        try:
            result = subprocess.run(['ip', 'addr', 'show', interface], 
                                  capture_output=True, text=True, timeout=1)
            if result.returncode == 0:
                if 'state UP' in result.stdout and 'inet ' in result.stdout:
                    # Extract IP quickly
                    for line in result.stdout.split('\n'):
                        if 'inet ' in line:
                            ip = line.split('inet ')[1].split('/')[0]
                            return {'up': True, 'ip': ip}
            return {'up': False, 'ip': None}
        except Exception:
            return {'up': False, 'ip': None}
    
    def switch_interface_fast(self, target_interface):
        """Ultra-fast interface switching using the fixed integration function."""
        self.show_fast("SWITCHING...", f"To: {target_interface}", "Please wait", "", "yellow")
        
        def lcd_callback(msg):
            """Callback to show status on LCD."""
            self.show_fast("SWITCHING", f"To: {target_interface}", msg[:15], "", "yellow")
            time.sleep(0.5)  # Brief pause to read status
        
        try:
            print(f"🔄 Fast switching to {target_interface} using integration function")
            
            # Use the FIXED set_raspyjack_interface function with LCD callback
            # This includes all our bug fixes:
            # - SSID parsing fix
            # - Auto-connect using WiFi profiles  
            # - Disconnected interface handling
            # - LCD-friendly status messages
            success = set_raspyjack_interface(target_interface, lcd_callback)
            
            if success:
                # Quick status check for display
                status = self.check_interface_fast(target_interface)
                if status['up']:
                    self.show_fast("SUCCESS!", f"Now using:", target_interface, f"IP: {status['ip'][:12]}", "green")
                else:
                    self.show_fast("SUCCESS!", f"Switched to:", target_interface, "Getting IP...", "green")
                
                self.current_interface = target_interface
                print(f"✅ Successfully switched to {target_interface}")
                time.sleep(1.5)
                return True
            else:
                self.show_fast("FAILED!", f"{target_interface}", "switch failed", "Check logs", "red")
                print(f"❌ Failed to switch to {target_interface}")
                time.sleep(2)
                return False
                
        except Exception as e:
            self.show_fast("ERROR!", str(e)[:15], "", "", "red")
            print(f"❌ Switch error: {e}")
            time.sleep(2)
            return False
    
    def read_buttons_fast(self):
        """Ultra-fast button reading with debouncing."""
        current_time = time.time()
        pressed_buttons = []

        virtual = get_virtual_button()
        if virtual:
            pressed_buttons.append(virtual)
            return pressed_buttons
        
        for button_name, pin in self.buttons.items():
            current_state = GPIO.input(pin)
            
            # Detect button press (transition from 1 to 0)
            if self.button_states[pin] == 1 and current_state == 0:
                # Debouncing - ignore if pressed too recently
                if current_time - self.last_press_time[pin] > 0.1:  # 100ms debounce
                    pressed_buttons.append(_flip_btn(button_name))
                    self.last_press_time[pin] = current_time
            
            self.button_states[pin] = current_state
        
        return pressed_buttons
    
    def show_status(self):
        """Show current interface status."""
        current = self.get_current()
        
        # Quick status check for current interface
        if current.startswith('wlan'):
            status = self.check_interface_fast(current)
            if status['up']:
                self.show_fast("Current:", current, f"IP: {status['ip'][:12]}", "KEY1/2: Switch", "white")
            else:
                self.show_fast("Current:", current, "NOT CONNECTED", "KEY1/2: Switch", "red")
        else:
            self.show_fast("Current:", current, "Non-WiFi iface", "KEY1/2: Switch", "yellow")
    
    def run(self):
        """Main fast response loop."""
        self.show_fast("Fast WiFi", "Switcher", "KEY1: wlan0", "KEY2: wlan1")
        time.sleep(0.6)
        shown_default = False

        while self.running:
            try:
                # Fast button check - no delays
                pressed = self.read_buttons_fast()
                
                if pressed:
                    shown_default = False
                    for button in pressed:
                        if button == 'KEY1':
                            # Switch to wlan0
                            print("KEY1 pressed - switching to wlan0")
                            self.switch_interface_fast('wlan0')
                            
                        elif button == 'KEY2':
                            # Switch to wlan1
                            print("KEY2 pressed - switching to wlan1")
                            self.switch_interface_fast('wlan1')
                            
                        elif button == 'KEY3':
                            # Exit
                            self.show_fast("Exiting...", "", "", "")
                            time.sleep(0.5)
                            self.running = False
                            break
                            
                        elif button == 'UP' or button == 'DOWN':
                            # Quick toggle
                            current = self.get_current()
                            if current == 'wlan0':
                                self.switch_interface_fast('wlan1')
                            elif current == 'wlan1':
                                self.switch_interface_fast('wlan0')
                            else:
                                self.switch_interface_fast('wlan1')  # Default to wlan1
                                
                        elif button == 'CENTER':
                            # Show status
                            self.show_status()
                            time.sleep(1.5)
                
                # Update display periodically only when interface actually changes or after an action
                current_time = time.monotonic()
                if not shown_default or (current_time - self.last_update) > 2.5:
                    if not pressed and self.running:
                        current = self.get_current()
                        if not shown_default or current != self.current_interface:
                            self.current_interface = current
                            self.show_fast("Fast Switcher", f"Current: {current}", "KEY1: wlan0", "KEY2: wlan1")
                            shown_default = True
                    self.last_update = current_time
                
                time.sleep(0.035)
                
            except KeyboardInterrupt:
                self.running = False
                break
            except Exception as e:
                print(f"Error in main loop: {e}")
                time.sleep(0.1)
    
    def cleanup(self):
        """Clean up resources."""
        try:
            self.show_fast("Goodbye!", "", "", "")
            time.sleep(0.5)
            self.lcd.LCD_Clear()
            GPIO.cleanup()
        except Exception:
            pass

def main():
    """Main function."""
    print("🚀 Starting Fast WiFi Switcher")
    
    if not IMPORTS_OK:
        print("❌ Required modules not available")
        return
    
    switcher = None
    try:
        switcher = FastWiFiSwitcher()
        switcher.run()
    except KeyboardInterrupt:
        print("\n⏹️  Interrupted by user")
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        if switcher:
            switcher.cleanup()

if __name__ == "__main__":
    main() 