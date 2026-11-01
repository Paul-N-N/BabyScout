"""Test bootstrap: stub the MicroPython-only modules so the app modules can be
imported on a normal CPython/pytest install.

The stubs are intentionally tiny - only what the modules touch at import time.
Real hardware behaviour is not simulated here; the pure logic (encoder decode,
duration formatting, timer toggle decisions, display truncation) is what the
tests exercise.
"""

import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# --- MicroPython stdlib aliases ---------------------------------------------
sys.modules.setdefault("ujson", json)
sys.modules.setdefault("uos", os)

# MicroPython's time module adds ticks_* helpers; CPython's does not. Add them
# (and time_ns) so modules that use them can be imported and exercised.
import time as _time

if not hasattr(_time, "ticks_ms"):
    def _ticks_ms():
        return int(_time.monotonic() * 1000) & 0x3FFFFFFF

    _time.ticks_ms = _ticks_ms
    _time.ticks_us = lambda: int(_time.monotonic() * 1000000) & 0x3FFFFFFF
    _time.ticks_cpu = _time.ticks_us
    _time.ticks_diff = lambda a, b: a - b
    _time.ticks_add = lambda ticks, delta: ticks + delta
    _time.sleep_ms = lambda ms: _time.sleep(ms / 1000)

if "urequests" not in sys.modules:
    sys.modules["urequests"] = types.ModuleType("urequests")

if "micropython" not in sys.modules:
    micropython = types.ModuleType("micropython")
    micropython.const = lambda value: value
    sys.modules["micropython"] = micropython

if "framebuf" not in sys.modules:
    framebuf = types.ModuleType("framebuf")

    class FrameBuffer:
        def __init__(self, buffer, width, height, mode):
            self.buffer = buffer
            self.width = width
            self.height = height

        def fill(self, value):
            pass

        def fill_rect(self, x, y, w, h, value):
            pass

        def text(self, string, x, y, color):
            pass

        def show(self):
            pass

    framebuf.FrameBuffer = FrameBuffer
    framebuf.MONO_VLSB = 0
    sys.modules["framebuf"] = framebuf

if "machine" not in sys.modules:
    machine = types.ModuleType("machine")

    class Pin:
        IN = 0
        OUT = 1
        PULL_UP = 2
        PULL_DOWN = 3

        def __init__(self, pin, mode=None, pull=None):
            self.pin = pin
            self._value = 1

        def value(self, value=None):
            if value is None:
                return self._value
            self._value = value

        def on(self):
            self._value = 1

        def off(self):
            self._value = 0

        def toggle(self):
            self._value = 0 if self._value else 1

    class I2C:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("no I2C in tests")

    machine.Pin = Pin
    machine.I2C = I2C
    sys.modules["machine"] = machine
