from machine import Pin
import time


# --- Persistent status LED -------------------------------------------------
# A single, shared LED pin for the whole program (one Pin object, not a new one
# per call, so the "solid on" state stays stable). The LED is the CYW43 radio's
# own status LED (its WL_GPIO0 / EXT_GPIO0 line, not a header pin), so flashes
# are kept slow (>= ~0.25 s per half-cycle) to avoid disturbing the radio.
#
#   Solid ON   -> connected to WiFi AND BabyBuddy, fully operational (buttons live).
#   Flashing   -> working but not ready: no WiFi, can't reach BabyBuddy, no children.
#   Off        -> starting up / not yet connected.
#
# Flashes are driven from the MAIN thread (not a background thread/timer): the
# event loop - and therefore any thread or machine.Timer - is starved while the
# blocking wlan.connect()/HTTP calls run, so only the main thread can reliably
# blink the LED. The "not ready" flashes always end OFF so they can never be
# mistaken for the solid-ON "ready" state.
_status_led = Pin("LED", Pin.OUT)
_status_led.off()


def led_on():
    """Turn the status LED solid on (system healthy / connected)."""
    _status_led.on()


def led_acknowledge():
    """
    A single quick blip - 'I saw you press it' (an acknowledgement, NOT a
    result). The real result signal (success/failure) comes after the API call
    finishes, so a caregiver is never left wondering whether it recorded.
    """
    _status_led.off()
    time.sleep(0.15)
    _status_led.on()


def led_signal_success():
    """
    'It was recorded.' Two pulses, then rests SOLID ON (the ready state).
    Ends LIT, which is what separates it from a failure (which ends dark).
    Caregiver model: lit at the end = it worked.
    Half-cycles are kept >= ~0.2 s to avoid disturbing the CYW43 radio, which
    shares its WL_GPIO0 LED line.
    """
    for _ in range(2):
        _status_led.off()
        time.sleep(0.2)
        _status_led.on()
        time.sleep(0.2)
    _status_led.on()


def led_signal_failure():
    """
    'It was NOT recorded - please do it manually in BabyBuddy.' Three slower
    pulses, then rests OFF. Ends DARK, which is what separates it from a
    success (which ends lit). Caregiver model: dark at the end = it didn't.
    """
    for _ in range(3):
        _status_led.on()
        time.sleep(0.3)
        _status_led.off()
        time.sleep(0.3)
    _status_led.off()


def led_flash_not_ready(count=3, period=0.25):
    """
    Blink the status LED (main thread) and leave it OFF. Signals "working but
    not ready yet" - deliberately never rests solid-on (that is the "ready"
    state), so it is unmistakable. Runs on the main thread so it always happens
    even while a background thread/timer would be starved by the network I/O.
    """
    for _ in range(count):
        _status_led.on()
        time.sleep(period)
        _status_led.off()
        time.sleep(period)


def led_flash_forever(period=0.25):
    """
    Run an endless LED flash loop to indicate a persistent problem state.
    This never returns - it is intended for a terminal fault (nothing to
    retry against).
    """
    while True:
        _status_led.toggle()
        time.sleep(period)


# --- Button wiring polarity -------------------------------------------------
# The physical buttons are wired to GROUND: the internal pull-UP holds the pin
# high when the button is released, and a press pulls it LOW (0). The pull and
# the pressed-test must always agree, so both are driven by this one constant.
#
#   BUTTON_TO_GROUND = True  -> GND wiring: pull-UP,   press reads LOW  (0)
#   BUTTON_TO_GROUND = False -> 3V3 wiring: pull-DOWN, press reads HIGH (1)
BUTTON_TO_GROUND = True


def button_is_pressed(pin_level):
    """
    Translate a raw pin level (0 or 1) into "is the button held down", honouring
    BUTTON_TO_GROUND so the pull config and the pressed-test can't drift apart.
    """
    return (pin_level == 0) if BUTTON_TO_GROUND else (pin_level == 1)


def create_button(pin):
    """
    Reuseable function for creating buttons (polarity per BUTTON_TO_GROUND).

    params:
        pin(int): GPIO Number to detect the button on.
    """
    pull = Pin.PULL_UP if BUTTON_TO_GROUND else Pin.PULL_DOWN
    return Pin(pin, Pin.IN, pull)


# --- Rotary encoder (EC11) ---------------------------------------------------
# A 5-pin EC11 rotary encoder with center push button. The A/B quadrature
# channels are open-drain (their low side is wired to GND) and the center
# switch common is GND too, so all three GPIOs use the internal pull-UP:
# released = HIGH (1), pulled low = LOW (0).
#
#   A  (channel A)    -> a_pin
#   B  (channel B)    -> b_pin
#   C  (center switch)-> sw_pin
#   GND (common)      -> GND
#   VCC               -> 3V3 (bare EC11 has no supply pin; harmless if fitted)

def create_encoder(a_pin, b_pin, sw_pin):
    """
    Create the pins for an EC11 rotary encoder + center push button.

    params:
        a_pin(int):  GPIO for quadrature channel A (open-drain to GND).
        b_pin(int):  GPIO for quadrature channel B (open-drain to GND).
        sw_pin(int): GPIO for the center push button (switch to GND).

    returns:
        (a, b, sw) machine.Pin objects, all inputs with the internal pull-UP.
    """
    return (
        Pin(a_pin, Pin.IN, Pin.PULL_UP),
        Pin(b_pin, Pin.IN, Pin.PULL_UP),
        Pin(sw_pin, Pin.IN, Pin.PULL_UP),
    )


def led_nudge():
    """
    A very short blip - quick 'input registered' feedback (e.g. knob steps).
    Deliberately briefer than led_acknowledge() so a fast turn stays responsive.
    """
    _status_led.off()
    time.sleep(0.05)
    _status_led.on()


class Debounced:
    """
    Debounce one digital input. A new level must be seen on `samples`
    consecutive update() calls before it is accepted, so contact bounce and
    electrical noise can't register as an edge.

    read: callable returning the raw level (0/1).
    samples: consecutive identical reads required to accept a change.
        The loop polls at ~100 Hz, so the default 3 = ~20-30 ms of debounce.

    The initial stable state is read in __init__, so startup baseline and the
    debouncer always agree (a button held at boot is not a press).
    """

    def __init__(self, read, samples=3):
        self.read = read
        self.samples = samples
        self.stable = self.read()
        self.count = 0

    def update(self):
        raw = self.read()
        if raw == self.stable:
            self.count = 0
            return self.stable
        self.count += 1
        if self.count >= self.samples:
            self.stable = raw
            self.count = 0
        return self.stable
