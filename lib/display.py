# SSD1306 OLED status display (I2C), wrapped so a MISSING or BROKEN display
# can never affect the app: if nothing is found on the bus, every call below
# is a no-op and BabyScout runs exactly as it did before the display existed.
#
# Wiring (4-pin I2C SSD1306 module):
#   GND -> GND
#   VCC -> 3V3
#   SDA -> GP14
#   SCL -> GP15
#
# NOTE: this MicroPython build only accepts fixed (sda, scl) pin PAIRS for
# I2C1: (2,3), (10,11), (14,15), (18,19), (22,23). GP15/GP16 (the common
# default) is NOT one of them - the valid free pair is GP14/GP15.
#
# Driver: lib/ssd1306.py (TimHanewich/MicroPython-SSD1306, Pico-targeted).

from machine import I2C, Pin
from lib.utils import log

DISPLAY_SDA_PIN = 14
DISPLAY_SCL_PIN = 15
DISPLAY_WIDTH = 128
DISPLAY_HEIGHT = 64  # 128x64 (8-page) module; use 32 for a 128x32 module
DISPLAY_ADDRS = (0x3C, 0x3D)  # the two standard SSD1306 I2C addresses
# The datasheet warns that continuous operation dims the panel after ~1000 h
# and recommends turning it off when not needed. Max contrast (0xFF, the
# driver default) is the worst case, so run at ~50% while the screen is on.
DISPLAY_CONTRAST = 0x7F


class Display:
    """
    Thin wrapper: text(title, detail, foot) updates the main lines (plus the
    optional 4th "context" line on 128x64 panels). ok is False (and every
    method a no-op) when no display is present or one failed.
    """

    def __init__(self):
        self.buf = None
        self.ok = False
        self.header = None  # optional top line (the clock)
        self.last = None  # last (header, title, detail, foot) drawn - skips redraws
        self.awake = False  # panel powered on?
        self.dirty = False  # framebuffer changed while the panel was off
        try:
            from lib.ssd1306 import SSD1306_I2C
            i2c = None
            for constructor in (
                lambda: I2C(1, sda=Pin(DISPLAY_SDA_PIN), scl=Pin(DISPLAY_SCL_PIN), baudrate=400000),
                lambda: I2C(1, 400000, sda=Pin(DISPLAY_SDA_PIN), scl=Pin(DISPLAY_SCL_PIN)),  # this build's signature
            ):
                try:
                    i2c = constructor()
                    break
                except (TypeError, ValueError):
                    continue
            if i2c is None:
                raise RuntimeError("I2C constructor not supported")
            found = i2c.scan()
            addr = None
            for candidate in DISPLAY_ADDRS:
                if candidate in found:
                    addr = candidate
                    break
            if addr is None:
                log("Display: no SSD1306 found on I2C - continuing without one.")
                return
            self.buf = SSD1306_I2C(DISPLAY_WIDTH, DISPLAY_HEIGHT, i2c, addr)
            self.height = DISPLAY_HEIGHT
            self.ok = True
            self.awake = True
            try:
                self.buf.contrast(DISPLAY_CONTRAST)
            except Exception:
                # Contrast is a nicety; never let it disable the display.
                pass
            log("Display: SSD1306 found at " + hex(addr))
        except Exception as e:
            self.buf = None
            self.ok = False
            log("Display: init failed (" + str(e) + ") - continuing without one.")

    def text(self, title, detail=None, foot=None):
        """
        Redraw the display: a title line, an optional detail line and an
        optional foot line (a small context line such as "since 9:41P").
        Never raises - a display fault must not touch the button loop.
        """
        if not self.ok or self.buf is None:
            return
        try:
            # This build's framebuf font is 8 px wide at scale 1, so a 128 px
            # line holds exactly 16 characters. Cap every line to the width so
            # a long string can never run off the panel.
            cap = DISPLAY_WIDTH // 8
            h = (self.header or "")[:cap]
            t = (title or "")[:cap]
            d = (detail or "")[:cap]
            f = (foot or "")[:cap]
            if (h, t, d, f) == self.last:
                # Nothing changed: skip the ~1 KB I2C transfer (the encoder
                # path redraws on every poll, so this matters).
                return
            self.buf.fill(0)
            if self.height >= 64:
                # 8-row panel (128x64): scale 1 (16 chars/line), centred.
                # (Scale 2 would only fit 8 chars per line.) The foot line is
                # the spare 4th line under detail (128x32 panels have no room).
                if h:
                    self.buf.text(h, 0, 0, 1)
                self.buf.text(t, 0, 20, 1)
                if d:
                    self.buf.text(d, 0, 36, 1)
                if f:
                    self.buf.text(f, 0, 48, 1)
            else:
                # 4-row panel: header at the top, then title/detail. No foot
                # line - all four rows are already used.
                y = 0
                if h:
                    self.buf.text(h, 0, 0, 1)
                    y = 8
                self.buf.text(t, 0, y, 1)
                if d:
                    self.buf.text(d, 0, y + 8, 1)
            if self.awake:
                self.buf.show()
                self.dirty = False
            else:
                # Panel is powered off: keep the framebuffer current but skip
                # the I2C transfer. wake() pushes it when the screen comes back.
                self.dirty = True
            self.last = (h, t, d, f)
        except Exception:
            # Display stopped answering (I2C timeout, loose wire, ...): stop
            # trying so the app keeps running, same as if none was fitted.
            self.ok = False
            self.buf = None
            self.last = None
            self.awake = False
            self.dirty = False

    def set_header(self, header):
        """
        Set the optional top line (the clock) and repaint the current screen.
        Safe while the panel is asleep: the buffer is updated and shown on wake.
        """
        header = (header or "")[:DISPLAY_WIDTH // 8]
        if header == self.header:
            return
        self.header = header
        if self.last:
            # Keep the title/detail/foot currently on screen; text() re-renders
            # them together with the new header (and honours the sleep state).
            self.text(self.last[1], self.last[2], self.last[3])

    def asleep(self):
        """
        True only when a FITTED panel is currently powered off (the idle
        timeout state). False when no display is present or one has failed:
        there is nothing to wake, so input must act normally instead of being
        consumed as a wake.
        """
        return self.ok and not self.awake

    def sleep(self):
        """
        Power the panel off (RAM is retained), per the datasheet's advice to
        turn it off when not needed. Returns True only if this call did it.
        """
        if not self.ok or self.buf is None or not self.awake:
            return False
        try:
            self.buf.poweroff()
            self.awake = False
            self.dirty = False
            return True
        except Exception:
            self.ok = False
            self.buf = None
            self.last = None
            self.awake = False
            self.dirty = False
            return False

    def wake(self):
        """
        Power the panel back on and push any framebuffer changes made while it
        was off. Returns True only if this call did it.
        """
        if not self.ok or self.buf is None or self.awake:
            return False
        try:
            self.buf.poweron()
            try:
                self.buf.contrast(DISPLAY_CONTRAST)
            except Exception:
                pass
            if self.dirty:
                self.buf.show()
                self.dirty = False
            self.awake = True
            return True
        except Exception:
            self.ok = False
            self.buf = None
            self.last = None
            self.awake = False
            self.dirty = False
            return False


display = Display()