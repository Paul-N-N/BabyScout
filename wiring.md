# BabyScout Pico 2 W — Complete Wiring Guide

Everything in this file matches the deployed code exactly
(`main.py`, `lib/pin.py`, `lib/display.py`).

## Control scheme

- **9 keyboard switches** = Sleep, Wet, Solid, Wet+Solid, Bottle,
  Left Breast, Breast Feed, Right Breast, Tummy Time
- **EC11 knob turn** = set breast-pump volume (10–500 ml, auto-saves)
- **EC11 center press** = the Breast Pump action (start/stop + record)
- **SSD1306 OLED** = live status of all of the above

## Parts

| Part | Pins | Notes |
|---|---|---|
| 9 keyboard switches (momentary) | 2 each | electrically identical to tactile buttons |
| EC11 rotary encoder with center push button | 4 (A, B, C, GND) | 4 wires; the +V pin is left unconnected |
| SSD1306 OLED 0.96" I2C module | 4 (GND, VCC, SCL, SDA) | **I2C version, not SPI** |
| Pico 2 W | — | powered via micro-USB (5 V) |
| Wire | — | ~25, soldered direct to the Pico and the parts |

## Master diagram

Pin names and numbers are from the official [Pico 2 W
pinout](https://pip-assets.raspberrypi.com/categories/1088-raspberry-pi-pico-2-w/documents/RP-008305-DS-1-pico-2-w-pinout.pdf).
The original Pico W has the identical header, so this wiring works on either
board. (Rows are physically aligned: with the USB port at the top, the left
column reads pins 1→20 top to bottom and the right column pins 40→21.)

```
                        micro-USB 5V (power in)
        ┌─────────────────────────────────────────────────────────────────────┐
  LEFT  │                                    PICO 2 W                         │  RIGHT
  EDGE  │  pin  name    wire to                  pin  name    wire to         │  EDGE
        │─────────────────────────────────────────────────────────────────────│
        │  1   GP0       (free)                   40   VBUS      (do not use) │
        │  2   GP1       (free)                   39   VSYS      (do not use) │
        │  3   GND       (common)                 38   GND       (common)     │
        │  4   GP2       ─── SLEEP btn            37   3V3_EN    (do not use) │
        │  5   GP3       ─── SOLID DIAPER btn     36   3V3(OUT)  ─── OLED VCC │
        │  6   GP4       ─── WET+SOLID btn        35   ADC_VREF  (do not use) │
        │  7   GP5       ─── WET DIAPER btn       34   GP28      (free, ADC2) │
        │  8   GND       (common)                 33   GND       (common)     │
        │  9   GP6       ─── BOTTLE btn           32   GP27      (free, ADC1) │
        │ 10   GP7       ─── LEFT BREAST btn      31   GP26      (free, ADC0) │
        │ 11   GP8       ─── BREAST FEED btn      30   RUN       (do not use) │
        │ 12   GP9       ─── RIGHT BREAST btn     29   GP22      (free)       │
        │ 13   GND       (common)                 28   GND       (common)     │
        │ 14   GP10      ─── TUMMY TIME btn       27   GP21      (free)       │
        │ 15   GP11      (free)                   26   GP20      (free)       │
        │ 16   GP12      (free)                   25   GP19      ─── EC11 C   │
        │ 17   GP13      (free)                   24   GP18      ─── EC11 B   │
        │ 18   GND       (common)                 23   GND       (common)     │
        │ 19   GP14      ─── OLED SDA             22   GP17      ─── EC11 A   │
        │ 20   GP15      ─── OLED SCL             21   GP16      (free)       │
        └─────────────────────────────────────────────────────────────────────┘

  Every button's second wire, the EC11 GND and the OLED GND share one GND
  rail - solder them to any GND pin (3 / 8 / 13 / 18 / 23 / 28 / 33 / 38).
```

## 1) The 9 keyboard switches

One wire from each switch to its GPIO, the other wire to GND.
Internal pull-ups are enabled in code (`BUTTON_TO_GROUND = True`), so
**no external resistors are needed**. Press = pin pulled LOW.

| Switch (label on screen) | GPIO | Pico pin # |
|---|---|---|
| Sleep | GP2 | 4 |
| Solid diaper | GP3 | 5 |
| Wet + Solid diaper | GP4 | 6 |
| Wet diaper | GP5 | 7 |
| Bottle feed | GP6 | 9 |
| Left breast | GP7 | 10 |
| Breast feed | GP8 | 11 |
| Right breast | GP9 | 12 |
| Tummy time | GP10 | 14 |
| **Common (all switches)** | **GND** | **any GND pin (e.g. 13 or 38)** |

## 2) EC11 rotary encoder — the breast pump

Open-drain channels + internal pull-ups: **no external resistors needed**,
and the +V pin is not used (leave it unconnected).

| EC11 pin | Function | GPIO | Pico pin # |
|---|---|---|---|
| A | channel A (turn = volume) | GP17 | 22 |
| B | channel B (turn = volume) | GP18 | 24 |
| C | **center push = BREAST PUMP action** | GP19 | 25 |
| GND | common | GND | any GND pin (e.g. 23 or 38) |

- Turn = set pump volume, ±5 ml per detent, clamped 10–500 ml
- Volume **auto-saves** ~0.8 s after you stop turning (one flash write per
  adjustment) and shows `Volume saved / N ml` on the OLED
- Center press (hold ≥ 150 ms) = fire the Breast Pump action (start/stop +
  record at that volume), same OK/FAILED feedback as the keys
- With the encoder unplugged the pull-ups hold all three lines HIGH and the
  app is completely safe (no phantom steps, no phantom pumps) — verified

## 3) SSD1306 OLED (I2C)

| OLED pin | Function | GPIO | Pico pin # |
|---|---|---|---|
| GND | ground | GND | any GND pin (e.g. 38) |
| VCC | 3.3 V power | 3V3(OUT) | 36 |
| SCL | I2C clock | **GP15** | 20 |
| SDA | I2C data | **GP14** | 19 |

**Pin choice:** GP14 (SDA) / GP15 (SCL) are the Pico's standard I2C1 pair —
valid on stock MicroPython, and also on the feature-limited build this app
was developed against, where the I2C1 pin *pairs* are a fixed set
(`(2,3) (10,11) (14,15) (18,19) (22,23)` in which the common "GP15/GP16"
default is NOT one — so the display must stay on GP14/GP15 either way).
Address 0x3C is auto-detected (0x3D also tried). The module has internal
pull-ups + regulator: no external parts needed.

## 4) Power

- Pico powered from micro-USB (5 V wall charger or hub) — VBUS, pin 40
- 3V3(OUT) pin (36) supplies the OLED (~20–30 mA) — well within budget
- Switches & encoder are pure signal (no supply needed)
- Do NOT connect VCC to VBUS (40), and do not back-power the 3V3(OUT) pin;
  leave 3V3_EN (37) unconnected

## 5) Reserved / do-not-touch pins

- **GP23–GP25, GP29** — CYW43 WiFi radio (internal; not on the header).
  Never wire anything here.
- **VBUS (40), VSYS (39), 3V3_EN (37), ADC_VREF (35), RUN (30)** — system
  pins, leave alone.
- **Free for future use:** GP0, GP1, GP11, GP12, GP13, GP16, GP20, GP21,
  GP22, and GP26/GP27/GP28 (these are also ADC0/1/2).
- The status LED is the **onboard** LED of the CYW43 radio itself (its
  WL_GPIO0 pin — MicroPython exposes it as `Pin("LED")` / `Pin("WL_GPIO0")`).
  It is not a header pin, so it can't clash with any of the wiring above —
  no external LED.

## 6) Quick sanity checks after wiring

1. **Power on.** OLED shows a 1 s splash box, then
   `BabyScout / starting up...` → `Waiting...` → `BabyScout ready / press a button`.
   Onboard LED blinks while waiting, solid when ready.
2. **Turn the knob** → `Pump Volume / N ml` live-updates; stop turning →
   `Volume saved / N ml`.
3. **Center press (hold ~0.2 s)** → Breast Pump fires: the screen flashes
   `Breast Pump / recording...`, then `Breast Pump / started` (or
   `Breast Pump / ended <duration>` on the second press). A failure shows
   `FAILED / Breast Pump`.
4. **Press any key** → the screen shows `<label> / recording...`, then
   `Recorded / <label>` (diapers) or `<label> / started` (timed actions).
   A failure shows `FAILED / <label>`.
5. **Reboot** → the pump volume is still there.
6. **Leave it idle for 5 minutes** (no timer running) → the screen powers
   off; press any button or turn the knob to wake it instantly.

Host-side tests and deployment (from the repo root):

```bash
make test                 # pytest: encoder, fmt_duration, scout toggle logic
make deploy PORT=COM6     # copy every file, byte-verify, reset the Pico
```
