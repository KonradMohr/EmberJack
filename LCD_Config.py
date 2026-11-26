##
 #  @filename   :   DEV_Config.py / LCD_Config.py
 #  @brief      :   LCD hardware interface implements (GPIO, SPI, Framebuffer, HDMI)
 #                   Supports: SPI displays (ST7735, ST7789) + CardputerZero fb + HDMI
 #  @author     :   Yehui from Waveshare (original), 7h30th3r0n3 (CardputerZero)
 #
 # Permission is hereby granted, free of charge, to any person obtaining a copy
 # of this software and associated documnetation files (the "Software"), to deal
 # in the Software without restriction, including without limitation the rights
 # to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 # copies of the Software, and to permit persons to  whom the Software is
 # furished to do so, subject to the following conditions:
 #
 # The above copyright notice and this permission notice shall be included in
 # all copies or substantial portions of the Software.
 #
 # THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 # IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 # FITNESS OR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 # AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 # LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 # OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 # THE SOFTWARE.
 #

import os
import time

import rj_env

_DISPLAY_TYPE = rj_env.get_display_type()


if _DISPLAY_TYPE == "HDMI":
    # ===================================================================
    # HDMI Monitor Mode: shared-memory presenter (no SPI, no physical GPIO)
    # ===================================================================
    import hdmi_shm

    _hdmi_cfg = rj_env.get_hdmi_config()
    LCD_RST_PIN = -1
    LCD_DC_PIN = -1
    LCD_CS_PIN = -1
    LCD_BL_PIN = -1

    FB_DEVICE = hdmi_shm.SHM_PATH
    FB_WIDTH = _hdmi_cfg["width"]
    FB_HEIGHT = _hdmi_cfg["height"]
    FB_BPP = 24
    FB_SIZE = FB_WIDTH * FB_HEIGHT * 3

    class _SpiStub:
        max_speed_hz = 0
        mode = 0

        def writebytes(self, data):
            pass

    SPI = _SpiStub()

    def fb_write(data: bytes, width: int = None, height: int = None, fmt: str = "rgb565"):
        w = width or FB_WIDTH
        h = height or FB_HEIGHT
        if fmt == "rgb24" or len(data) == w * h * 3:
            hdmi_shm.publish_frame(data, w, h)
        elif len(data) == w * h * 2:
            hdmi_shm.publish_rgb565(data, w, h)

    def epd_digital_write(pin, value):
        pass

    def Driver_Delay_ms(xms):
        pass

    def SPI_Write_Byte(data):
        pass

    def GPIO_Init():
        hdmi_shm.ensure_host_running(_hdmi_cfg)
        return 0

elif _DISPLAY_TYPE == "CARDPUTER_320":
    # ===================================================================
    # CardputerZero: framebuffer stub (no SPI, no GPIO for display)
    # ===================================================================
    import mmap

    LCD_RST_PIN = -1
    LCD_DC_PIN = -1
    LCD_CS_PIN = -1
    LCD_BL_PIN = -1

    _FB_NAMES = ("st7789v_m5st", "panel-mipi-dbi")
    FB_DEVICE = os.environ.get("RJ_FB_DEVICE", "")
    if not FB_DEVICE:
        FB_DEVICE = "/dev/fb0"
        for _i in range(4):
            _fb_name_path = f"/sys/class/graphics/fb{_i}/name"
            try:
                with open(_fb_name_path) as _fn:
                    if any(n in _fn.read() for n in _FB_NAMES):
                        FB_DEVICE = f"/dev/fb{_i}"
                        break
            except Exception:
                pass
    FB_WIDTH = 320
    FB_HEIGHT = 170
    FB_BPP = 16
    FB_SIZE = FB_WIDTH * FB_HEIGHT * (FB_BPP // 8)

    _fb_fd = None
    _fb_mmap = None

    class _SpiStub:
        max_speed_hz = 0
        mode = 0

        def writebytes(self, data):
            pass

    SPI = _SpiStub()

    def _open_fb():
        global _fb_fd, _fb_mmap
        if _fb_mmap is not None:
            return _fb_mmap
        _fb_fd = os.open(FB_DEVICE, os.O_RDWR)
        _fb_mmap = mmap.mmap(
            _fb_fd, FB_SIZE, mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ
        )
        return _fb_mmap

    def fb_write(data: bytes):
        fb = _open_fb()
        fb.seek(0)
        fb.write(data)

    def epd_digital_write(pin, value):
        pass

    def Driver_Delay_ms(xms):
        pass

    def SPI_Write_Byte(data):
        pass

    def _unblank_fb():
        blank_path = FB_DEVICE.replace("/dev/", "/sys/class/graphics/") + "/blank"
        try:
            with open(blank_path, "w") as f:
                f.write("0")
        except Exception:
            pass

    def GPIO_Init():
        _open_fb()
        _unblank_fb()
        return 0

else:
    # ===================================================================
    # Standard Raspberry Pi: SPI + GPIO for Waveshare HAT displays
    # ===================================================================
    import spidev
    import RPi.GPIO as GPIO

    LCD_RST_PIN = 27
    LCD_DC_PIN = 25
    LCD_CS_PIN = 8
    LCD_BL_PIN = 24

    SPI = spidev.SpiDev(0, 0)

    def epd_digital_write(pin, value):
        GPIO.output(pin, value)

    def Driver_Delay_ms(xms):
        time.sleep(xms / 1000.0)

    def SPI_Write_Byte(data):
        SPI.writebytes(data)

    def GPIO_Init():
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(LCD_RST_PIN, GPIO.OUT)
        GPIO.setup(LCD_DC_PIN, GPIO.OUT)
        GPIO.setup(LCD_CS_PIN, GPIO.OUT)
        GPIO.setup(LCD_BL_PIN, GPIO.OUT)
        SPI.max_speed_hz = 9000000
        SPI.mode = 0b00
        return 0

### END OF FILE ###
