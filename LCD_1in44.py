 # -*- coding:UTF-8 -*-
 ##
 # | file      	:	LCD_1IN44.py
 # |	version		:	V3.0
 # | date		:	2018-07-16
 # | function	:	On the ST7735S / ST7789 chip driver and clear screen, drawing lines, drawing,
 #					writing and other functions to achieve
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
 # LIABILITY WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 # OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 # THE SOFTWARE.
 #

import LCD_Config
import rj_env
try:
    import RPi.GPIO as GPIO
except Exception:
    import gpio_shim as GPIO
import time
import numpy as np
import os
import json
from PIL import Image as PILImage, ImageOps

# ---------------------------------------------------------------------------
# Display type detection from rj_env / gui_conf.json
# Supported: "ST7735_128" (128x128), "ST7789_240" (240x240),
#            "CARDPUTER_320" (320x170), "HDMI" (configurable WxH, default 640x360)
# ---------------------------------------------------------------------------
_DISPLAY_TYPE = rj_env.get_display_type()
_FLIP_180 = rj_env.get_display_flip()

# ---------------------------------------------------------------------------
# Resolution constants based on display type
# ---------------------------------------------------------------------------
if _DISPLAY_TYPE == "HDMI":
    _hdmi_cfg = rj_env.get_hdmi_config()
    LCD_WIDTH  = _hdmi_cfg["width"]
    LCD_HEIGHT = _hdmi_cfg["height"]
    LCD_X = 0
    LCD_Y = 0
    LCD_X_MAXPIXEL = LCD_WIDTH
    LCD_Y_MAXPIXEL = LCD_HEIGHT
elif _DISPLAY_TYPE == "CARDPUTER_320":
    LCD_WIDTH  = 320
    LCD_HEIGHT = 170
    LCD_X = 0
    LCD_Y = 0
    LCD_X_MAXPIXEL = 320
    LCD_Y_MAXPIXEL = 170
elif _DISPLAY_TYPE == "ST7789_240":
    LCD_WIDTH  = 240
    LCD_HEIGHT = 240
    LCD_X = 0
    LCD_Y = 0
    LCD_X_MAXPIXEL = 240
    LCD_Y_MAXPIXEL = 240
else:
    # ST7735_128 — original 1.44" display (default)
    LCD_WIDTH  = 128
    LCD_HEIGHT = 128
    LCD_X = 2
    LCD_Y = 1
    LCD_X_MAXPIXEL = 132
    LCD_Y_MAXPIXEL = 162

# ---------------------------------------------------------------------------
# Scale helper – payloads import this: from LCD_1in44 import S
# All coordinates are authored for 128; S() adapts them to the actual panel.
# ---------------------------------------------------------------------------
LCD_SCALE = min(LCD_WIDTH, LCD_HEIGHT) / 128  # 1.0 on 128x128, 1.875 on 240x240, 2.8125 on 640x360

def S(v):
    """Scale a 128-base pixel value to the current display resolution."""
    return int(v * LCD_SCALE)

# WebUI frame mirror (used by device_server.py)
_FRAME_MIRROR_PATH = os.environ.get("RJ_FRAME_PATH", "/dev/shm/raspyjack_last.jpg")
_FRAME_MIRROR_ENABLED = os.environ.get("RJ_FRAME_MIRROR", "1") != "0"
_FORCE_FRAME_MIRROR = os.environ.get("RJ_FORCE_FRAME_MIRROR", "0") == "1"
_WRITE_RAW_MIRROR = os.environ.get("RJ_WRITE_RAW_MIRROR", "0") == "1"
_WS_CLIENTS_PATH = os.environ.get("RJ_WS_CLIENTS_PATH", "/dev/shm/rj_ws_clients.json")
_CARDPUTER_FRAME_PATH = os.environ.get("RJ_CARDPUTER_FRAME_PATH", "/dev/shm/raspyjack_cardputer.jpg")
_CARDPUTER_FRAME_ENABLED = os.environ.get("RJ_CARDPUTER_FRAME_ENABLED", "1") != "0"
_CARDPUTER_FRAME_MODE = str(os.environ.get("RJ_CARDPUTER_FRAME_MODE", "stretch") or "stretch").strip().lower()
_CARDPUTER_FRAME_WIDTH = max(1, int(os.environ.get("RJ_CARDPUTER_FRAME_WIDTH", "240")))
_CARDPUTER_FRAME_HEIGHT = max(1, int(os.environ.get("RJ_CARDPUTER_FRAME_HEIGHT", "135")))
_CARDPUTER_FRAME_QUALITY = min(100, max(1, int(os.environ.get("RJ_CARDPUTER_FRAME_QUALITY", "76"))))
try:
    _frame_fps = float(os.environ.get("RJ_FRAME_FPS", "10"))
    _FRAME_MIRROR_INTERVAL = 1.0 / max(1.0, _frame_fps)
except Exception:
    _FRAME_MIRROR_INTERVAL = 0.1
_last_frame_save = 0.0
_mirror_seed_written = False
_ws_clients_cache = (0.0, 0, 0)  # (checked_at, legacy_count, cardputer_count)

try:
    _resampling_fast = PILImage.Resampling.BILINEAR
except AttributeError:
    _resampling_fast = PILImage.BILINEAR


def _get_remote_client_counts(now: float):
    """Return (legacy_clients, cardputer_clients) with 1s caching to avoid JPEG work at 0 clients."""
    global _ws_clients_cache
    if _FORCE_FRAME_MIRROR:
        return 1, 1
    checked_at, leg, cp = _ws_clients_cache
    if (now - checked_at) < 1.0:
        return leg, cp
    leg, cp = 0, 0
    try:
        if os.path.isfile(_WS_CLIENTS_PATH):
            with open(_WS_CLIENTS_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle) or {}
            pid = int(data.get("pid", 0) or 0)
            alive = False
            if pid > 0:
                try:
                    os.kill(pid, 0)
                    alive = True
                except PermissionError:
                    alive = True
                except OSError:
                    alive = False
            if alive:
                leg = max(0, int(data.get("legacy", 0) or 0))
                cp = max(0, int(data.get("cardputer", 0) or 0))
    except Exception:
        leg, cp = 0, 0
    _ws_clients_cache = (now, leg, cp)
    return leg, cp


def _build_cardputer_frame(src_image):
    if _CARDPUTER_FRAME_MODE == "stretch":
        return src_image.resize((_CARDPUTER_FRAME_WIDTH, _CARDPUTER_FRAME_HEIGHT), _resampling_fast)
    if _CARDPUTER_FRAME_MODE == "contain":
        return ImageOps.contain(src_image, (_CARDPUTER_FRAME_WIDTH, _CARDPUTER_FRAME_HEIGHT), _resampling_fast)
    return ImageOps.fit(src_image, (_CARDPUTER_FRAME_WIDTH, _CARDPUTER_FRAME_HEIGHT), _resampling_fast)


def _save_cardputer_frame(src_image):
    if not _CARDPUTER_FRAME_ENABLED:
        return
    try:
        cardputer_frame = _build_cardputer_frame(src_image)
        if cardputer_frame.size != (_CARDPUTER_FRAME_WIDTH, _CARDPUTER_FRAME_HEIGHT):
            canvas = PILImage.new("RGB", (_CARDPUTER_FRAME_WIDTH, _CARDPUTER_FRAME_HEIGHT), "black")
            offset_x = max(0, (_CARDPUTER_FRAME_WIDTH - cardputer_frame.width) // 2)
            offset_y = max(0, (_CARDPUTER_FRAME_HEIGHT - cardputer_frame.height) // 2)
            canvas.paste(cardputer_frame, (offset_x, offset_y))
            cardputer_frame = canvas
        cardputer_frame.save(_CARDPUTER_FRAME_PATH, "JPEG", quality=_CARDPUTER_FRAME_QUALITY, subsampling=0)
    except Exception:
        pass

#scanning method
L2R_U2D = 1
L2R_D2U = 2
R2L_U2D = 3
R2L_D2U = 4
U2D_L2R = 5
U2D_R2L = 6
D2U_L2R = 7
D2U_R2L = 8
SCAN_DIR_DFT = U2D_R2L


class LCD:
    def __init__(self):
        self.width = LCD_WIDTH
        self.height = LCD_HEIGHT
        self.LCD_Scan_Dir = SCAN_DIR_DFT
        self.LCD_X_Adjust = LCD_X
        self.LCD_Y_Adjust = LCD_Y
        self.display_type = _DISPLAY_TYPE
        self._last_hdmi_bytes = None
        self._last_hdmi_dims = (0, 0)
        self._last_hdmi_seq = 0

    """    Hardware reset     """
    def  LCD_Reset(self):
        GPIO.output(LCD_Config.LCD_RST_PIN, GPIO.HIGH)
        LCD_Config.Driver_Delay_ms(100)
        GPIO.output(LCD_Config.LCD_RST_PIN, GPIO.LOW)
        LCD_Config.Driver_Delay_ms(100)
        GPIO.output(LCD_Config.LCD_RST_PIN, GPIO.HIGH)
        LCD_Config.Driver_Delay_ms(100)

    """    Write register address and data     """
    def  LCD_WriteReg(self, Reg):
        GPIO.output(LCD_Config.LCD_DC_PIN, GPIO.LOW)
        LCD_Config.SPI_Write_Byte([Reg])

    def LCD_WriteData_8bit(self, Data):
        GPIO.output(LCD_Config.LCD_DC_PIN, GPIO.HIGH)
        LCD_Config.SPI_Write_Byte([Data])

    def LCD_WriteData_NLen16Bit(self, Data, DataLen):
        GPIO.output(LCD_Config.LCD_DC_PIN, GPIO.HIGH)
        for i in range(0, DataLen):
            LCD_Config.SPI_Write_Byte([Data >> 8])
            LCD_Config.SPI_Write_Byte([Data & 0xff])

    # ------------------------------------------------------------------
    # ST7735S register initialization (1.44" 128x128)
    # ------------------------------------------------------------------
    def _LCD_InitReg_ST7735(self):
        #ST7735R Frame Rate
        self.LCD_WriteReg(0xB1)
        self.LCD_WriteData_8bit(0x01)
        self.LCD_WriteData_8bit(0x2C)
        self.LCD_WriteData_8bit(0x2D)

        self.LCD_WriteReg(0xB2)
        self.LCD_WriteData_8bit(0x01)
        self.LCD_WriteData_8bit(0x2C)
        self.LCD_WriteData_8bit(0x2D)

        self.LCD_WriteReg(0xB3)
        self.LCD_WriteData_8bit(0x01)
        self.LCD_WriteData_8bit(0x2C)
        self.LCD_WriteData_8bit(0x2D)
        self.LCD_WriteData_8bit(0x01)
        self.LCD_WriteData_8bit(0x2C)
        self.LCD_WriteData_8bit(0x2D)

        #Column inversion
        self.LCD_WriteReg(0xB4)
        self.LCD_WriteData_8bit(0x07)

        #ST7735R Power Sequence
        self.LCD_WriteReg(0xC0)
        self.LCD_WriteData_8bit(0xA2)
        self.LCD_WriteData_8bit(0x02)
        self.LCD_WriteData_8bit(0x84)
        self.LCD_WriteReg(0xC1)
        self.LCD_WriteData_8bit(0xC5)

        self.LCD_WriteReg(0xC2)
        self.LCD_WriteData_8bit(0x0A)
        self.LCD_WriteData_8bit(0x00)

        self.LCD_WriteReg(0xC3)
        self.LCD_WriteData_8bit(0x8A)
        self.LCD_WriteData_8bit(0x2A)
        self.LCD_WriteReg(0xC4)
        self.LCD_WriteData_8bit(0x8A)
        self.LCD_WriteData_8bit(0xEE)

        self.LCD_WriteReg(0xC5)#VCOM
        self.LCD_WriteData_8bit(0x0E)

        #ST7735R Gamma Sequence
        self.LCD_WriteReg(0xe0)
        self.LCD_WriteData_8bit(0x0f)
        self.LCD_WriteData_8bit(0x1a)
        self.LCD_WriteData_8bit(0x0f)
        self.LCD_WriteData_8bit(0x18)
        self.LCD_WriteData_8bit(0x2f)
        self.LCD_WriteData_8bit(0x28)
        self.LCD_WriteData_8bit(0x20)
        self.LCD_WriteData_8bit(0x22)
        self.LCD_WriteData_8bit(0x1f)
        self.LCD_WriteData_8bit(0x1b)
        self.LCD_WriteData_8bit(0x23)
        self.LCD_WriteData_8bit(0x37)
        self.LCD_WriteData_8bit(0x00)
        self.LCD_WriteData_8bit(0x07)
        self.LCD_WriteData_8bit(0x02)
        self.LCD_WriteData_8bit(0x10)

        self.LCD_WriteReg(0xe1)
        self.LCD_WriteData_8bit(0x0f)
        self.LCD_WriteData_8bit(0x1b)
        self.LCD_WriteData_8bit(0x0f)
        self.LCD_WriteData_8bit(0x17)
        self.LCD_WriteData_8bit(0x33)
        self.LCD_WriteData_8bit(0x2c)
        self.LCD_WriteData_8bit(0x29)
        self.LCD_WriteData_8bit(0x2e)
        self.LCD_WriteData_8bit(0x30)
        self.LCD_WriteData_8bit(0x30)
        self.LCD_WriteData_8bit(0x39)
        self.LCD_WriteData_8bit(0x3f)
        self.LCD_WriteData_8bit(0x00)
        self.LCD_WriteData_8bit(0x07)
        self.LCD_WriteData_8bit(0x03)
        self.LCD_WriteData_8bit(0x10)

        #Enable test command
        self.LCD_WriteReg(0xF0)
        self.LCD_WriteData_8bit(0x01)

        #Disable ram power save mode
        self.LCD_WriteReg(0xF6)
        self.LCD_WriteData_8bit(0x00)

        #65k mode
        self.LCD_WriteReg(0x3A)
        self.LCD_WriteData_8bit(0x05)

    # ------------------------------------------------------------------
    # ST7789 register initialization (1.3" 240x240)
    # ------------------------------------------------------------------
    def _LCD_InitReg_ST7789(self):
        # Memory Data Access Control
        self.LCD_WriteReg(0x36)
        self.LCD_WriteData_8bit(0x00)

        # RGB 5-6-5-bit color format
        self.LCD_WriteReg(0x3A)
        self.LCD_WriteData_8bit(0x05)

        # Porch Setting
        self.LCD_WriteReg(0xB2)
        self.LCD_WriteData_8bit(0x0C)
        self.LCD_WriteData_8bit(0x0C)
        self.LCD_WriteData_8bit(0x00)
        self.LCD_WriteData_8bit(0x33)
        self.LCD_WriteData_8bit(0x33)

        # Gate Control
        self.LCD_WriteReg(0xB7)
        self.LCD_WriteData_8bit(0x35)

        # VCOM Setting (higher value reduces ghosting/image retention)
        self.LCD_WriteReg(0xBB)
        self.LCD_WriteData_8bit(0x2B)

        # LCM Control
        self.LCD_WriteReg(0xC0)
        self.LCD_WriteData_8bit(0x2C)

        # VDV and VRH Command Enable
        self.LCD_WriteReg(0xC2)
        self.LCD_WriteData_8bit(0x01)

        # VRH Set (slightly higher for cleaner transitions)
        self.LCD_WriteReg(0xC3)
        self.LCD_WriteData_8bit(0x15)

        # VDV Set
        self.LCD_WriteReg(0xC4)
        self.LCD_WriteData_8bit(0x20)

        # Frame Rate Control in Normal Mode (0x01 = ~111Hz, reduces ghosting)
        self.LCD_WriteReg(0xC6)
        self.LCD_WriteData_8bit(0x01)

        # Power Control 1
        self.LCD_WriteReg(0xD0)
        self.LCD_WriteData_8bit(0xA4)
        self.LCD_WriteData_8bit(0xA1)

        # Positive Voltage Gamma Control
        self.LCD_WriteReg(0xE0)
        self.LCD_WriteData_8bit(0xD0)
        self.LCD_WriteData_8bit(0x04)
        self.LCD_WriteData_8bit(0x0D)
        self.LCD_WriteData_8bit(0x11)
        self.LCD_WriteData_8bit(0x13)
        self.LCD_WriteData_8bit(0x2B)
        self.LCD_WriteData_8bit(0x3F)
        self.LCD_WriteData_8bit(0x54)
        self.LCD_WriteData_8bit(0x4C)
        self.LCD_WriteData_8bit(0x18)
        self.LCD_WriteData_8bit(0x0D)
        self.LCD_WriteData_8bit(0x0B)
        self.LCD_WriteData_8bit(0x1F)
        self.LCD_WriteData_8bit(0x23)

        # Negative Voltage Gamma Control
        self.LCD_WriteReg(0xE1)
        self.LCD_WriteData_8bit(0xD0)
        self.LCD_WriteData_8bit(0x04)
        self.LCD_WriteData_8bit(0x0C)
        self.LCD_WriteData_8bit(0x11)
        self.LCD_WriteData_8bit(0x13)
        self.LCD_WriteData_8bit(0x2C)
        self.LCD_WriteData_8bit(0x3F)
        self.LCD_WriteData_8bit(0x44)
        self.LCD_WriteData_8bit(0x51)
        self.LCD_WriteData_8bit(0x2F)
        self.LCD_WriteData_8bit(0x1F)
        self.LCD_WriteData_8bit(0x1F)
        self.LCD_WriteData_8bit(0x20)
        self.LCD_WriteData_8bit(0x23)

        # Display Inversion On (ST7789 needs this for correct colors)
        self.LCD_WriteReg(0x21)

    # ------------------------------------------------------------------
    # Dispatch to the correct init register sequence
    # ------------------------------------------------------------------
    def LCD_InitReg(self):
        if self.display_type == "ST7789_240":
            self._LCD_InitReg_ST7789()
        else:
            self._LCD_InitReg_ST7735()

    #********************************************************************************
    #function:	Set the display scan and color transfer modes
    #parameter:
    #		Scan_dir   :   Scan direction
    #		Colorchose :   RGB or GBR color format
    #********************************************************************************
    def LCD_SetGramScanWay(self, Scan_dir):
        #Get the screen scan direction
        self.LCD_Scan_Dir = Scan_dir

        if self.display_type == "ST7789_240":
            # ST7789: simpler MADCTL handling
            if (Scan_dir == L2R_U2D) or (Scan_dir == L2R_D2U) or (Scan_dir == R2L_U2D) or (Scan_dir == R2L_D2U):
                self.width  = LCD_HEIGHT
                self.height = LCD_WIDTH
            else:
                self.width  = LCD_WIDTH
                self.height = LCD_HEIGHT

            madctl = 0x00
            if Scan_dir == L2R_U2D:
                madctl = 0x00
            elif Scan_dir == L2R_D2U:
                madctl = 0x80
            elif Scan_dir == R2L_U2D:
                madctl = 0x40
            elif Scan_dir == R2L_D2U:
                madctl = 0xC0
            elif Scan_dir == U2D_L2R:
                madctl = 0x20
            elif Scan_dir == U2D_R2L:
                madctl = 0x60
            elif Scan_dir == D2U_L2R:
                madctl = 0xA0
            else:  # D2U_R2L
                madctl = 0xE0

            self.LCD_X_Adjust = LCD_X
            self.LCD_Y_Adjust = LCD_Y

            self.LCD_WriteReg(0x36)
            self.LCD_WriteData_8bit(madctl)
            return

        # ST7735S: original logic
        #Get GRAM and LCD width and height
        if (Scan_dir == L2R_U2D) or (Scan_dir == L2R_D2U) or (Scan_dir == R2L_U2D) or (Scan_dir == R2L_D2U) :
            self.width	= LCD_HEIGHT
            self.height 	= LCD_WIDTH
            if Scan_dir == L2R_U2D:
                MemoryAccessReg_Data = 0X00 | 0x00
            elif Scan_dir == L2R_D2U:
                MemoryAccessReg_Data = 0X00 | 0x80
            elif Scan_dir == R2L_U2D:
                MemoryAccessReg_Data = 0x40 | 0x00
            else:		#R2L_D2U:
                MemoryAccessReg_Data = 0x40 | 0x80
        else:
            self.width	= LCD_WIDTH
            self.height 	= LCD_HEIGHT
            if Scan_dir == U2D_L2R:
                MemoryAccessReg_Data = 0X00 | 0x00 | 0x20
            elif Scan_dir == U2D_R2L:
                MemoryAccessReg_Data = 0X00 | 0x40 | 0x20
            elif Scan_dir == D2U_L2R:
                MemoryAccessReg_Data = 0x80 | 0x00 | 0x20
            else:		#R2L_D2U
                MemoryAccessReg_Data = 0x40 | 0x80 | 0x20

        #please set (MemoryAccessReg_Data & 0x10) != 1
        if (MemoryAccessReg_Data & 0x10) != 1:
            self.LCD_X_Adjust = LCD_Y
            self.LCD_Y_Adjust = LCD_X
        else:
            self.LCD_X_Adjust = LCD_X
            self.LCD_Y_Adjust = LCD_Y

        # Set the read / write scan direction of the frame memory
        self.LCD_WriteReg(0x36)		#MX, MY, RGB mode
        self.LCD_WriteData_8bit( MemoryAccessReg_Data | 0x08)	#0x08 set RGB

    #/********************************************************************************
    #function:
    #			initialization
    #********************************************************************************/
    def LCD_Init(self, Lcd_ScanDir=None):
        if (LCD_Config.GPIO_Init() != 0):
            return -1

        if self.display_type in ("CARDPUTER_320", "HDMI"):
            return 0

        # Set SPI speed based on display type
        if self.display_type == "ST7789_240":
            LCD_Config.SPI.max_speed_hz = 62000000  # ST7789 max: 62.5MHz
        else:
            LCD_Config.SPI.max_speed_hz = 9000000

        #Turn on the backlight
        GPIO.output(LCD_Config.LCD_BL_PIN,GPIO.HIGH)

        #Hardware reset
        self.LCD_Reset()

        #Set the initialization register
        self.LCD_InitReg()

        #Set the display scan and color transfer modes
        self.LCD_SetGramScanWay(Lcd_ScanDir or SCAN_DIR_DFT)
        LCD_Config.Driver_Delay_ms(200)

        #sleep out
        self.LCD_WriteReg(0x11)
        LCD_Config.Driver_Delay_ms(120)

        #Turn on the LCD display
        self.LCD_WriteReg(0x29)

    #/********************************************************************************
    #function:	Sets the start position and size of the display area
    #parameter:
    #	Xstart 	:   X direction Start coordinates
    #	Ystart  :   Y direction Start coordinates
    #	Xend    :   X direction end coordinates
    #	Yend    :   Y direction end coordinates
    #********************************************************************************/
    def LCD_SetWindows(self, Xstart, Ystart, Xend, Yend):
        #set the X coordinates
        self.LCD_WriteReg(0x2A)
        self.LCD_WriteData_8bit((Xstart + self.LCD_X_Adjust) >> 8)
        self.LCD_WriteData_8bit((Xstart + self.LCD_X_Adjust) & 0xff)
        self.LCD_WriteData_8bit((Xend - 1 + self.LCD_X_Adjust) >> 8)
        self.LCD_WriteData_8bit((Xend - 1 + self.LCD_X_Adjust) & 0xff)

        #set the Y coordinates
        self.LCD_WriteReg(0x2B)
        self.LCD_WriteData_8bit((Ystart + self.LCD_Y_Adjust) >> 8)
        self.LCD_WriteData_8bit((Ystart + self.LCD_Y_Adjust) & 0xff)
        self.LCD_WriteData_8bit((Yend - 1 + self.LCD_Y_Adjust) >> 8)
        self.LCD_WriteData_8bit((Yend - 1 + self.LCD_Y_Adjust) & 0xff)

        self.LCD_WriteReg(0x2C)

    def LCD_Clear(self):
        self._last_hdmi_bytes = None
        self._last_hdmi_dims = (0, 0)
        self._last_hdmi_seq = 0
        if self.display_type == "HDMI":
            import hdmi_shm
            hdmi_shm.clear_frame(self.width, self.height)
            return
        if self.display_type == "CARDPUTER_320":
            LCD_Config.fb_write(b'\x00' * LCD_Config.FB_SIZE)
            return
        _buffer = [0x00]*(self.width * self.height * 2)
        self.LCD_SetWindows(0, 0, self.width, self.height)
        GPIO.output(LCD_Config.LCD_DC_PIN, GPIO.HIGH)
        for i in range(0,len(_buffer),4096):
            LCD_Config.SPI_Write_Byte(_buffer[i:i+4096])

    def show_raw_frame(self, raw_bytes, src_w=None, src_h=None, fmt="rgb565", flags=None):
        """Fast raw-frame helper for media/camera/game payloads (supports RGB565LE & RGB24)."""
        w = src_w or self.width
        h = src_h or self.height
        self._last_hdmi_bytes = None
        self._last_hdmi_dims = (0, 0)
        self._last_hdmi_seq = 0
        if self.display_type == "HDMI":
            import hdmi_shm
            out_flags = hdmi_shm.FLAG_NO_HUD if flags is None else int(flags)
            if fmt == "rgb24" or len(raw_bytes) == w * h * 3:
                hdmi_shm.publish_frame(raw_bytes, w, h, flags=out_flags)
            elif len(raw_bytes) == w * h * 2:
                hdmi_shm.publish_rgb565(raw_bytes, w, h, flags=out_flags)
            return
        if self.display_type == "CARDPUTER_320" and fmt == "rgb565" and w == self.width and h == self.height:
            LCD_Config.fb_write(raw_bytes)
            return
        if fmt == "rgb565":
            arr = np.frombuffer(raw_bytes, dtype="<u2").reshape((h, w))
            r = ((arr >> 11) & 0x1F) << 3
            g = ((arr >> 5) & 0x3F) << 2
            b = (arr & 0x1F) << 3
            rgb = np.stack([r.astype(np.uint8), g.astype(np.uint8), b.astype(np.uint8)], axis=-1)
            img = PILImage.fromarray(rgb, "RGB")
        else:
            img = PILImage.frombytes("RGB", (w, h), raw_bytes)
        self.LCD_ShowImage(img, 0, 0)

    def LCD_ShowImage(self,Image,Xstart,Ystart):
        global _last_frame_save, _mirror_seed_written
        if (Image == None):
            return
        if _FLIP_180:
            _transpose_rot180 = getattr(getattr(PILImage, "Transpose", PILImage), "ROTATE_180", PILImage.ROTATE_180)
            Image = Image.transpose(_transpose_rot180)
        imwidth, imheight = Image.size
        if imwidth != self.width or imheight != self.height:
            # Preserve square aspect ratio when a 128x128 / 240x240 legacy frame is shown on
            # widescreen HDMI instead of stretching it 1.78x horizontally into blurry ovals.
            if self.display_type == "HDMI" and imwidth == imheight and self.width > self.height:
                target_side = self.height
                resample = PILImage.NEAREST if imwidth <= 160 else PILImage.BILINEAR
                fitted = Image.resize((target_side, target_side), resample)
                canvas = PILImage.new("RGB", (self.width, self.height), (14, 14, 16))
                ox = (self.width - target_side) // 2
                canvas.paste(fitted, (ox, 0))
                Image = canvas
            else:
                resample = PILImage.NEAREST if (self.width % max(1, imwidth) == 0 and self.height % max(1, imheight) == 0) else PILImage.BILINEAR
                Image = Image.resize((self.width, self.height), resample)

        frame_unchanged = False
        if self.display_type == "HDMI":
            import hdmi_shm
            img_rgb = Image if Image.mode == "RGB" else Image.convert("RGB")
            raw = img_rgb.tobytes()
            dims = img_rgb.size
            # Universal identical-frame dedup: skips SHM publish + host wakeup for 161+
            # polling payloads when screen pixels & SHM sequence have not changed.
            if (
                raw == self._last_hdmi_bytes
                and dims == self._last_hdmi_dims
                and self._last_hdmi_seq > 0
                and hdmi_shm.peek_seq() == self._last_hdmi_seq
            ):
                frame_unchanged = True
            else:
                seq = hdmi_shm.publish_frame(raw, dims[0], dims[1])
                self._last_hdmi_bytes = raw
                self._last_hdmi_dims = dims
                self._last_hdmi_seq = int(seq) if isinstance(seq, int) else 0
        elif self.display_type == "CARDPUTER_320":
            img = Image if Image.mode == "RGB" else Image.convert("RGB")
            arr = np.asarray(img)
            r = (arr[..., 0].astype(np.uint16) >> 3) << 11
            g = (arr[..., 1].astype(np.uint16) >> 2) << 5
            b = arr[..., 2].astype(np.uint16) >> 3
            rgb565 = (r | g | b).astype(np.uint16).tobytes()
            LCD_Config.fb_write(rgb565)
        else:
            img = np.asarray(Image)
            pix = np.zeros((self.height, self.width, 2), dtype=np.uint8)
            pix[..., 0] = (img[..., 0] & 0xF8) | (img[..., 1] >> 5)
            pix[..., 1] = ((img[..., 1] << 3) & 0xE0) | (img[..., 2] >> 3)
            self.LCD_SetWindows(0, 0, self.width, self.height)
            GPIO.output(LCD_Config.LCD_DC_PIN, GPIO.HIGH)
            spi_obj = getattr(LCD_Config, "SPI", None)
            write2 = getattr(spi_obj, "writebytes2", None)
            if write2 is not None:
                raw_spi = pix.tobytes()
                for i in range(0, len(raw_spi), 4096):
                    write2(raw_spi[i:i+4096])
            else:
                flat = pix.ravel()
                for i in range(0, flat.size, 4096):
                    LCD_Config.SPI_Write_Byte(flat[i:i+4096].tolist())

        # Mirror the LCD frame for remote clients only when needed (throttled & client-aware)
        if _FRAME_MIRROR_ENABLED or _CARDPUTER_FRAME_ENABLED:
            try:
                now = time.monotonic()
                if (now - _last_frame_save) >= _FRAME_MIRROR_INTERVAL or not _mirror_seed_written:
                    leg_cnt, cp_cnt = _get_remote_client_counts(now)
                    if frame_unchanged and _mirror_seed_written and leg_cnt == 0 and cp_cnt == 0:
                        return
                    wrote_any = False
                    if _FRAME_MIRROR_ENABLED and (leg_cnt > 0 or not _mirror_seed_written):
                        Image.save(_FRAME_MIRROR_PATH, "JPEG", quality=80)
                        _mirror_seed_written = True
                        wrote_any = True
                    # Legacy raw mirror only needed on non-HDMI displays (HDMI already uses hdmi_shm)
                    if self.display_type != "HDMI" or _WRITE_RAW_MIRROR:
                        try:
                            raw = (Image if Image.mode == "RGB" else Image.convert("RGB")).tobytes()
                            with open("/dev/shm/raspyjack_raw.rgb", "wb") as _rf:
                                _rf.write(raw)
                            wrote_any = True
                        except Exception:
                            pass
                    if _CARDPUTER_FRAME_ENABLED and cp_cnt > 0:
                        _save_cardputer_frame(Image)
                        wrote_any = True
                    if wrote_any:
                        _last_frame_save = now
            except Exception:
                pass
