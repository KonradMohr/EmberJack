"""
Smart RPi package router for RaspyJack.

In HDMI or CARDPUTER_320 mode (or on non-Raspberry Pi systems where hardware
RPi.GPIO is not installed), `import RPi.GPIO as GPIO` transparently resolves to
`gpio_shim` (backed by normal keyboard + WebUI virtual input).

When configured for a real Waveshare SPI HAT (`ST7735_128` or `ST7789_240`) on
a Raspberry Pi that has the system `RPi.GPIO` package installed, this router
dynamically loads the real hardware `RPi.GPIO` module instead.
"""
