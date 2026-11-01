from lib.pin import (create_button, button_is_pressed, led_on, led_acknowledge,
                     led_signal_success, led_signal_failure, led_flash_not_ready,
                     led_flash_forever, create_encoder, led_nudge, Debounced)
from lib.encoder import Encoder
import time
import ntptime
import ujson
import uos as os
from lib.utils import (retrieve_auth_variables, join_path, auth_variables_valid,
                       partial, log, fmt_duration, fmt_clock, fmt_time, iso_to_epoch)
from picoconnection import PicoConnection
from lib.scout import connect_to_baby_buddy

APP_VERSION = "2.1"

# Retrieve WLAN Variables from secrets.json
WLAN_VARIABLES = retrieve_auth_variables(join_path(os.getcwd(), "secrets.json"))


# --- Optional SSD1306 OLED status display -----------------------------------
# Initialised BEFORE the radio: PicoConnection() spends ~2 s letting the CYW43
# settle plus a scan, so doing it first would leave the screen blank at boot.
# Graceful by design: a missing or broken display is a no-op (see
# lib/display.py), so this can never prevent the app from working.
from lib.display import display, DISPLAY_WIDTH

# Boot splash (1 s): also doubles as a display self-test - it exercises the
# full-panel fill, fill_rect and text paths on the real panel before anything
# else runs. Any failure here is swallowed; the display just stops being used.
try:
    if display.ok and display.buf is not None:
        _b = display.buf
        _b.fill(1)
        _y_box = 20 if display.height >= 64 else 4
        _b.fill_rect(8, _y_box, 112, 24, 0)
        _b.text("BabyScout v" + APP_VERSION, 14, _y_box + 8, 1)
        _b.show()
        time.sleep(1)
except Exception:
    display.ok = False
    display.buf = None
    display.last = None
display.text("BabyScout", "starting up...")
log("BabyScout v" + APP_VERSION + " starting")


def hard_fault(reason):
    """
    Unrecoverable startup problem (there is nothing to retry against). Signal
    on the OLED (if fitted) and with a continuously flashing LED, then stop.
    """
    log("HARD FAULT: " + reason)
    display.text("HARD FAULT", reason)
    led_flash_forever()


# A valid configuration is a hard requirement we cannot fix by retrying.
if not auth_variables_valid(WLAN_VARIABLES):
    hard_fault("secrets.json missing or invalid (need SSIDS_PASSWORD, BASE_URL and AUTHORIZATION)")


# Object for managing WiFi connectivity (2 s radio settle + initial scan).
pico_connection = PicoConnection()


# Remember whether we've already announced the WiFi state, so a fresh join
# (logged by picoconnection as "WLAN Connected" + IP) and an already-joined
# radio both show a clear confirmation without spamming every retry.
_wifi_ok_reported = False

# How many consecutive rounds have failed to join WiFi. Each failed round
# lengthens the pre-join radio warm-up (1 s, 2 s, 3 s ... capped at 6 s): a
# cold-boot radio sometimes simply needs more powered-on time before it will
# associate. Resets to 0 the moment WiFi comes up.
_wifi_rounds_failed = 0

# Clock: NTP gives UTC; lib.utils.fmt_clock converts it to the configured local
# zone (US Pacific with DST by default). The Pico has no battery-backed RTC, so
# it resyncs after boot and every 6 h (or every 5 min while it has never had a
# successful sync). If no sync succeeds for 24 h the line is cleared rather than
# showing a stale time as if it were current.
CLOCK_SYNC_MS = 6 * 60 * 60 * 1000
CLOCK_RETRY_MS = 5 * 60 * 1000
CLOCK_STALE_MS = 24 * 60 * 60 * 1000
CLOCK_ERROR_LOG_MS = 30 * 60 * 1000
NTP_TIMEOUT_S = 3  # bounds ntptime.settime() so a dead network cannot hang the loop
last_clock_attempt = None
last_clock_ok = None
clock_synced = False
_last_clock_error_log = None
last_event = None  # last recorded/ended activity, shown on the home screen
last_event_at = None  # wall-clock epoch of last_event (for "at HH:MM")


def ensure_wifi(rescan=False):
    """
    Make sure we're joined to WiFi. Re-scans and tries EVERY configured SSID that
    is currently visible (in config order) until one connects, so a dead or
    blocked network (e.g. a 5 GHz-only / NONET SSID) falls back to the next one.
    Reports each stage on the display (scan -> no-AP wait -> per-SSID attempt
    -> IP). Returns True when joined, else None.

    rescan: force a fresh AP scan. At boot the scan PicoConnection already did
        is reused; a dropped link or a failed round re-scans.
    """
    global _wifi_ok_reported
    global _wifi_rounds_failed
    if pico_connection.wlan.isconnected():
        _wifi_rounds_failed = 0
        if not _wifi_ok_reported:
            log("WiFi OK " + str(pico_connection.wlan.ifconfig()))
            _wifi_ok_reported = True
        display.text("WiFi", pico_connection.wlan.ifconfig()[0])
        return True
    _wifi_ok_reported = False
    display.text("WiFi", "scanning...")
    # Reuse the boot scan; re-scan when asked (link dropped), when the cache is
    # empty, or after a failed round (the AP may have appeared since).
    if rescan or not pico_connection.nearby_access_points or _wifi_rounds_failed:
        pico_connection.nearby_access_points = pico_connection.scan_access_points(verbose=False)
    # If we can't see ANY access points the radio is usually just still
    # warming up after a cold boot. Wait a beat and re-scan. NOTE: we
    # deliberately do NOT toggle wlan.active(False) here - deactivating the
    # radio is a known way to wedge the CYW43 so badly only a power cycle
    # recovers it.
    if not pico_connection.nearby_access_points:
        display.text("WiFi", "no APs - waiting")
        time.sleep(2)
        pico_connection.nearby_access_points = pico_connection.scan_access_points(verbose=False)
    # Escalating warm-up: each failed round adds a second of settle time
    # before the join attempt (capped), so a cold radio gets progressively
    # more time instead of the same 1 s every round.
    warmup = min(1 + _wifi_rounds_failed, 6)
    for ssid, password in WLAN_VARIABLES["SSIDS_PASSWORD"].items():
        if ssid not in pico_connection.nearby_access_points:
            continue
        display.text("WiFi", "trying " + ssid[:9])
        if pico_connection.connect_to_access_point(ssid, password, timeout=10, warmup=warmup):
            # connect_to_access_point already printed "WLAN Connected" + the IP.
            _wifi_ok_reported = True
            _wifi_rounds_failed = 0
            display.text("WiFi", pico_connection.wlan.ifconfig()[0])
            return True
    display.text("WiFi", "retry shortly")
    return None


def clock_due():
    """True when the RTC should be synced (6 h normally, 5 min until it works)."""
    if last_clock_attempt is None:
        return True
    interval = CLOCK_SYNC_MS if clock_synced else CLOCK_RETRY_MS
    return time.ticks_diff(time.ticks_ms(), last_clock_attempt) >= interval


def sync_clock():
    """
    Set the RTC from NTP and refresh the OLED clock line. Never raises: a
    failed sync just leaves the previous time (or no clock) in place. The
    query is bounded by ntptime.timeout so a dead network cannot freeze the
    button loop.
    """
    global clock_synced, last_clock_attempt, last_clock_ok
    global _last_clock_error_log
    last_clock_attempt = time.ticks_ms()
    try:
        ntptime.timeout = NTP_TIMEOUT_S
        ntptime.settime()
    except Exception as e:
        now = time.ticks_ms()
        # Throttle: a device with no NTP route would otherwise log every retry.
        if (_last_clock_error_log is None
                or time.ticks_diff(now, _last_clock_error_log) >= CLOCK_ERROR_LOG_MS):
            _last_clock_error_log = now
            log("Clock: NTP sync failed (" + str(e) + ")")
        if not clock_synced:
            display.set_header("")  # stay blank until the first success
        return False
    clock_synced = True
    last_clock_ok = time.ticks_ms()
    _last_clock_error_log = None
    log("Clock: NTP synced")
    display.set_header(fmt_clock(int(time.time())))
    return True


PUMP_MIN_ML = 10
PUMP_MAX_ML = 500
PUMP_STEP_ML = 5
PUMP_VOL_FILE = "pump_volume.json"
# Save once the knob has been still this long (ms). Longer than the old
# 800 ms so a slow multi-detent turn produces ONE write, not one per step.
PUMP_SAVE_DELAY_MS = 2000


def clamp_pump_volume(v):
    return max(PUMP_MIN_ML, min(PUMP_MAX_ML, int(v)))


def load_pump_volume(default=100):
    try:
        with open(PUMP_VOL_FILE) as f:
            return clamp_pump_volume(ujson.load(f).get("pump_volume", default))
    except Exception:
        return clamp_pump_volume(default)


last_saved_volume = None  # value already on flash; the last one is never rewritten


def save_pump_volume(v):
    """
    Write the volume to flash atomically (temp file + rename) so a power
    loss mid-write cannot corrupt pump_volume.json. Returns True when
    saved. (The unchanged-value check lives in the caller.)
    """
    global last_saved_volume
    v = clamp_pump_volume(v)
    tmp = PUMP_VOL_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(ujson.dumps({"pump_volume": v}))
        try:
            os.rename(tmp, PUMP_VOL_FILE)
        except Exception:
            # Some filesystems refuse to rename over an existing file.
            try:
                os.remove(PUMP_VOL_FILE)
            except Exception:
                pass
            os.rename(tmp, PUMP_VOL_FILE)
        last_saved_volume = v
        return True
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


def pump_volume_step(delta):
    global pump_volume
    pump_volume = clamp_pump_volume(pump_volume + delta * PUMP_STEP_ML)


pump_volume = load_pump_volume()
last_saved_volume = pump_volume
log("Pump volume: " + str(pump_volume) + " ml (knob = adjust, auto-saves; center press = breast pump)")


def home_screen():
    """
    The idle screen: the last recorded/ended event (or a ready message) plus
    the current pump volume, so the knob's setting is visible without
    touching it. The 4th line shows WHEN the last event happened (if the
    clock is synced). Called at boot, and again whenever a status note
    expires with no running timer.
    """
    detail = "Pump " + str(pump_volume) + " ml"
    foot = None
    if last_event and last_event_at is not None:
        foot = "at " + fmt_time(last_event_at)
    display.text(last_event or "BabyScout ready", detail, foot)


def wait_for_babybuddy():
    """
    Keep trying to reach BabyBuddy until it answers, blinking the LED (main
    thread) every round so "not ready yet" is unmistakable. The moment it comes
    back the LED goes solid and the buttons go live - no power cycle needed.
    Returns a ready Scout instance.

    Note: the LED can only blink when the main thread runs; while we are blocked
    waiting on a WiFi join or an HTTP response the Pico can't touch it (single
    threaded). So each round does ONE quick attempt and a visible blink, keeping
    the dark gaps short instead of one long pause.
    """
    global _wifi_rounds_failed
    while True:
        # Visible main-thread blink, left OFF (never rests solid-on = "ready").
        led_flash_not_ready(count=4)
        if not ensure_wifi():
            log("Waiting for WiFi...")
            _wifi_rounds_failed += 1
            display.text("Waiting...", "no WiFi yet")
            time.sleep(0.5)
            continue
        _wifi_rounds_failed = 0
        display.text("Waiting...", "BabyBuddy...")
        if clock_due():
            sync_clock()
        try:
            # One attempt per round (the loop below is the retry) keeps this
            # iteration short, so the blink above repeats frequently.
            baby_buddy = connect_to_baby_buddy(base_url=WLAN_VARIABLES["BASE_URL"], retries=1, delay=0)
            baby_buddy.next_child()
        except Exception as e:
            # Broad on purpose: a network failure (OSError) OR an HTTP 5xx/4xx
            # (ValueError from the API) should both just retry + blink, never
            # crash the boot. KeyboardInterrupt is NOT an Exception, so Ctrl+C
            # still works.
            log("Waiting for BabyBuddy... (" + str(e) + ")")
            display.text("Waiting...", str(e)[:16])
            time.sleep(0.5)
        else:
            log("Connected to BabyBuddy - ready")
            home_screen()
            return baby_buddy


BABY_SCOUT = wait_for_babybuddy()

# --- Connected and healthy: solid LED, start the buttons --------------------
led_on()

# --- Rotary encoder (EC11): the breast pump ---------------------------------
# Turning the knob sets the pump volume (ml) - auto-saved once you stop
# turning; the center push button triggers the Breast Pump action itself
# (start/stop + record at that volume).
# Wired: A->GP17, B->GP18, C (switch)->GP19, GND->GND
# (see create_encoder in lib/pin.py).
ENCODER_A_PIN = 17
ENCODER_B_PIN = 18
ENCODER_SW_PIN = 19
enc_a, enc_b, enc_sw = create_encoder(ENCODER_A_PIN, ENCODER_B_PIN, ENCODER_SW_PIN)

# Polling quadrature decoder (lib/encoder.py: 2-sample bounce stability,
# one detent per full A/B cycle). poll() returns signed detents per call.
enc = Encoder(enc_a, enc_b)


def breast_pump_action():
    # Read the encoder-selected volume at press time (start/stop toggle).
    return BABY_SCOUT.breast_pump(
        BABY_SCOUT.children[BABY_SCOUT.child_index], amount=pump_volume
    )


# Create button matrix, matching gpio pin to the specific action you want to capture.
# Buttons are wired to GROUND (see BUTTON_TO_GROUND in lib/pin.py); the 9 keyboard
# switches sit on the tidy left-edge block GP2-GP10, and GP17-19 hold the EC11
# encoder above (whose center press fires the breast pump, so no pump key).
button_gpio_pins = [2, 3, 4, 5, 6, 7, 8, 9, 10]
button_actions = [
    partial(BABY_SCOUT.sleep, BABY_SCOUT.children[BABY_SCOUT.child_index]),
    partial(BABY_SCOUT.solid_diaper, BABY_SCOUT.children[BABY_SCOUT.child_index]),
    partial(BABY_SCOUT.wet_solid_diaper, BABY_SCOUT.children[BABY_SCOUT.child_index]),
    partial(BABY_SCOUT.wet_diaper, BABY_SCOUT.children[BABY_SCOUT.child_index]),
    partial(BABY_SCOUT.bottle_feed, BABY_SCOUT.children[BABY_SCOUT.child_index]),
    partial(BABY_SCOUT.left_breast, BABY_SCOUT.children[BABY_SCOUT.child_index]),
    partial(BABY_SCOUT.breast_feed, BABY_SCOUT.children[BABY_SCOUT.child_index]),
    partial(BABY_SCOUT.right_breast, BABY_SCOUT.children[BABY_SCOUT.child_index]),
    partial(BABY_SCOUT.tummy_time, BABY_SCOUT.children[BABY_SCOUT.child_index]),
]
# The breast pump is NOT a key any more: the EC11 center button fires it.

# Human names in the SAME order as button_actions (used for the OLED feedback).
button_labels = [
    "Sleep", "Solid Diaper", "Wet + Solid", "Wet Diaper", "Bottle Feed",
    "Left Breast", "Breast Feed", "Right Breast", "Tummy Time"
]

# True = timed start/stop (resolve_timers), False = immediate record. Used so a
# 4xx only drops the running-timer display tracking for a timed action.
button_timed = [True, False, False, False, True, True, True, True, True]

# Initialize buttons
buttons = []
for button in button_gpio_pins:
    buttons.append(create_button(button))


# --- Button handling: honest feedback + can't crash -------------------------
# A deliberate press is held at least this long (debounce + reject an
# accidental quick tap or a toddler brushing the buttons).
MIN_HOLD_MS = 150
# Heartbeat (ms): with this much time passing and no button activity, the loop
# logs that it is alive and waiting. If the loop ever froze, this would stop -
# which is exactly how you'd spot a real hang from the serial console.
HEARTBEAT_MS = 30000
# Power the OLED off after this long with no button/encoder input - running
# timers included (the phone and the solid LED already show one is going).
# The panel datasheet warns that continuous operation dims it after ~1000 h
# and recommends turning it off when not needed; any input wakes it again
# instantly. A press while the panel is off only wakes it - it is consumed,
# not acted on - so a "is it still on?" tap can never start a timer.
DISPLAY_OFF_MS = 5 * 60 * 1000


# --- Timer awareness (display) ------------------------------------------------
# Actions are either IMMEDIATE (diapers - recorded at once) or TIMED (sleep,
# tummy time, feedings, pumping - start/stop). For timed ones we track what is
# running so the OLED can show it live:
#   * started on THIS Pico  -> we know the start tick -> true elapsed readout
#   * started on a phone    -> we know it is running, but the Pico has no
#                              clock, so no elapsed time (we say so honestly)
# resolve_timers() returns {"label", "started", "timer"} for timed actions
# (None for immediate ones), and we poll BabyBuddy's active timers every
# TIMER_POLL_MS to catch timers started or ended elsewhere.
TIMER_POLL_MS = 20000
# While no timer is running, poll less often: the device is mostly idle then,
# and a timer started on a phone only needs a minute-scale latency to appear.
TIMER_POLL_IDLE_MS = 60000
TIMER_NAMES = {
    "sleep": "Sleep",
    "tummy-times": "Tummy Time",
    "pumping": "Breast Pump",
    # "feedings" covers ALL feedings (bottle / left / right / both): while a
    # feeding timer is running the API cannot say which method was used (that
    # is only saved when it is stopped + recorded), so a polled feeding is
    # shown generically as "Feeding". A feeding started on THIS Pico keeps its
    # specific button label (matched by timer name, not label, below).
    "feedings": "Feeding",
}
active_timer = None  # None, or {"label": str, "start_ms": int|None}
note_hold_until = 0  # status messages hold the screen until this tick
_last_refresh_error_log = None  # throttles "timer poll failed" log lines


def wake_display():
    """
    Power the OLED back on after the idle timeout. Called on any button or
    encoder input, and before any status message, so a blank screen never
    hides a result.
    """
    if display.wake():
        log("Display on")


def note(title, detail=None, remember=False):
    """
    Show a status message and hold it for a few seconds, even while a running
    timer would otherwise repaint the screen every second.

    remember: also store the line as last_event so the home screen can show
        what just happened after the note expires (results only - transient
        "recording..." / "reconnecting..." lines do not).
    """
    global note_hold_until
    global last_event
    global last_event_at
    wake_display()
    display.text(title, detail)
    note_hold_until = time.ticks_ms() + 4000
    if remember:
        text = title if not detail else title + " " + detail
        last_event = text[:DISPLAY_WIDTH // 8]
        # Wall-clock time of the event, so the home screen can show "at HH:MM".
        # None while the clock has never synced (no past time to show).
        last_event_at = int(time.time()) if clock_synced else None


def _ended_detail(elapsed):
    """
    "ended <duration>" for the OLED, falling back to the bare duration when the
    full string would not fit the 16-char line (e.g. very long timers).
    """
    detail = "ended " + fmt_duration(elapsed)
    if len(detail) > DISPLAY_WIDTH // 8:
        detail = fmt_duration(elapsed)
    return detail


def note_timer_result(result, start_ms=None):
    """
    Update the running-timer tracker from a resolve_timers() result:
      started=True  -> remember label + start tick so the 1 Hz loop can show
                       the live elapsed time, and say the timer STARTED
                       (nothing is recorded yet - it is recorded when the
                       button is pressed again).
      started=False -> the timer was just stopped + recorded: show the elapsed
                       time if THIS Pico started it, otherwise just "ended".
    Immediate actions return None -> nothing to track.

    start_ms: when the press fired (not when the API answered), so the live
        readout does not lose the network round-trip.
    """
    global active_timer
    if not isinstance(result, dict) or "started" not in result:
        return
    label = result.get("label") or "Timer"
    if result.get("started"):
        active_timer = {
            "label": label,
            "start_ms": start_ms if start_ms is not None else time.ticks_ms(),
            "timer": result.get("timer"),
            # Wall-clock start time, so the 4th line can show "since HH:MM".
            # None while the clock has never synced.
            "since": int(time.time()) if clock_synced else None,
        }
        note(label, "started", remember=True)
    else:
        ended = active_timer
        active_timer = None
        if ended and ended["label"] == label and ended["start_ms"] is not None:
            elapsed = time.ticks_diff(time.ticks_ms(), ended["start_ms"])
            note(label, _ended_detail(elapsed), remember=True)
        elif ended and ended["label"] == label and ended.get("since") is not None:
            note(label, _ended_detail(
                (int(time.time()) - ended["since"]) * 1000), remember=True)
        else:
            note(label, "ended", remember=True)


def refresh_timer_state():
    """
    Poll BabyBuddy's active timers so timers started/ended on a phone still
    show on the display. Never raises - offline or transient failures just
    keep the current state (the next poll retries).
    """
    global active_timer
    global _last_refresh_error_log
    try:
        timers = BABY_SCOUT.list_timers(BABY_SCOUT.children[BABY_SCOUT.child_index])
        if not isinstance(timers, list):
            return
        if not timers:
            if active_timer is not None:
                ended = active_timer
                active_timer = None
                if ended["start_ms"] is not None:
                    note(ended["label"], _ended_detail(
                        time.ticks_diff(time.ticks_ms(), ended["start_ms"])),
                        remember=True)
                elif ended.get("since") is not None:
                    # Phone-started but we know its start timestamp: real duration.
                    note(ended["label"], _ended_detail(
                        (int(time.time()) - ended["since"]) * 1000),
                        remember=True)
                else:
                    note(ended["label"], "ended", remember=True)
            return
        t = timers[0]
        if not isinstance(t, dict):
            return
        name = t.get("name")
        # Map the API's timer name ("sleep", "tummy-times", ...) to a display
        # label. NOTE: never call str.title()/capitalize() here - this
        # MicroPython build lacks them (the old eager .title() default
        # crashed the app on the first live timer).
        label = TIMER_NAMES.get(name) if isinstance(name, str) else None
        if not label:
            label = name if (isinstance(name, str) and name) else "Timer"
        # Only replace what we show when the ACTIVITY changed. Match on the
        # API timer name (not the label) so a feeding started on THIS Pico
        # keeps its specific label ("Bottle Feed", ...) and its start tick,
        # instead of being reset to "Feeding / from the app" - which would
        # lose the live elapsed readout.
        if active_timer is None or active_timer.get("timer") != name:
            # The API's "start" timestamp lets us show real elapsed time +
            # "since HH:MM" for a phone-started timer (None if absent/unsynced).
            since = iso_to_epoch(t.get("start"))
            active_timer = {
                "label": label,
                "start_ms": None,
                "timer": name,
                "since": since,
            }
            note(label, "from the app")
    except Exception as e:
        # Display sync must NEVER take the app down: on any glitch, keep the
        # current state and let the next poll (20 s) retry. Log at most once a
        # minute so a persistent failure (e.g. an expired token) is visible on
        # serial without flooding it.
        now = time.ticks_ms()
        if (_last_refresh_error_log is None
                or time.ticks_diff(now, _last_refresh_error_log) >= 60000):
            _last_refresh_error_log = now
            log("Timer poll failed (" + str(e) + ") - keeping display state")
        return


def drop_timer_tracking():
    """
    BabyBuddy REJECTED a timed action (e.g. 'Duration too long'): scout has
    already auto-cleared the stale timer, so stop tracking it on the display
    too (it is no longer running anywhere).
    """
    global active_timer
    active_timer = None


def _handle_action(action, label, timed=False, press_ts=None):
    """
    Perform one action with honest, distinct feedback and no crashes:
      success -> screen shows the RESULT, then LED ends LIT  = "it recorded"
      failure -> one WiFi-reconnect + retry; if that also fails,
                 LED ends DARK = "NOT recorded, do it manually"

    Screen order matters for responsiveness: the caller has ALREADY shown an
    optimistic "<label> / recording..." the instant the press fired, so this
    function must update the screen BEFORE doing the slow LED sequence.
    Timed actions show "<label> / started" or "<label> / ended <duration>"
    (a START is NOT "Recorded" - nothing is recorded until the button is
    pressed again); immediate actions show "Recorded / <label>".

    Called for every baby button AND for the EC11 center press (breast pump),
    so both get exactly the same feedback.

    timed: the action is a start/stop toggle, so a 4xx means the running-timer
        display tracking should be dropped (scout already cleared the timer).
    press_ts: ticks_ms() when the press fired, so the live elapsed readout does
        not lose the network round-trip.

    A 4xx (client) error - e.g. a feeding left running so long BabyBuddy rejects
    it as 'Duration too long' - means OUR request is wrong, so we fail fast +
    clearly WITHOUT a doomed retry (retrying a 4xx won't fix it, and it wastes
    the time the next press needs). Only transient errors (5xx / network) retry.
    Every path ends in a known LED state and nothing here can raise out of the
    button loop and freeze the device.
    """
    try:
        result = action()
        # Screen first (instant), LED sequence second (0.8 s of blinks): the
        # caregiver checks the screen, and it must change the moment the API
        # answers - not after the LED animation.
        if isinstance(result, dict) and "started" in result:
            note_timer_result(result, start_ms=press_ts)
        else:
            note("Recorded", label, remember=True)
        led_signal_success()
    except Exception as e:
        code = getattr(e, "code", None)
        if code is not None and 400 <= code < 500:
            log("Rejected by BabyBuddy (HTTP " + str(code) + "): " + str(e))
            log("NOT recorded - fix it in BabyBuddy, then press again.")
            if timed:
                drop_timer_tracking()
            note("FAILED", label, remember=True)
            led_signal_failure()
            return
        log("Action failed (" + str(e) + ") - checking connection...")
        log("WiFi connected: " + str(pico_connection.wlan.isconnected()))
        note("Retrying...", label)
        ok = False
        retry_result = None
        try:
            # rescan=True: this is a recovery attempt, so don't trust the AP
            # list cached before the link dropped.
            if ensure_wifi(rescan=True):
                retry_result = action()
                ok = True
        except Exception as e2:
            log("Retry also failed (" + str(e2) + ")")
        if ok:
            if isinstance(retry_result, dict) and "started" in retry_result:
                note_timer_result(retry_result, start_ms=press_ts)
            else:
                note("Recorded", label, remember=True)
            led_signal_success()
        else:
            log("NOT recorded - please log this in BabyBuddy manually.")
            note("FAILED", label, remember=True)
            led_signal_failure()


# Loop: watch the buttons; a confirmed press is acted on exactly once per press.
# Edge-detection (tracking each button's previous level) replaces the old
# blocking "wait for release" loop, so a held or stuck-high button can never
# wedge the single-threaded loop - the Pico stays responsive and simply won't
# re-fire until the button is released. This is what fixes "it hangs after
# pressing". A quick tap is still rejected via MIN_HOLD_MS.
def button_pressed():
    global _wifi_rounds_failed
    global clock_synced
    global note_hold_until
    # Baseline each button on its ACTUAL level at startup, not an assumed low.
    # A button that is already held / reading high the instant we start - e.g. a
    # finger still on it from just before a Ctrl+C + re-run, or a loose contact -
    # must NOT count as a press; only a fresh release-then-press after startup
    # should fire an action. This is what stops "the last action repeats on
    # re-run with no button press".
    # Debounced inputs (3 stable samples): contact bounce can no longer fake or
    # swallow an edge. The initial stable state IS the startup baseline.
    debounced = [Debounced(lambda b=b: button_is_pressed(b.value())) for b in buttons]
    last = [d.stable for d in debounced]   # last stable "pressed?" state
    log("Button baseline (1=pressed): " + str([1 if p else 0 for p in last]))
    down_since = [None] * len(buttons)    # ticks_ms when the current press began
    acted = [False] * len(buttons)        # already fired on the current press?

    def resync_buttons():
        # A release that happened while _handle_action() was blocking must be
        # seen now, otherwise a re-press could be swallowed (the §3.2 symptom:
        # press, release and press again while the first call was in flight).
        resync_now = time.ticks_ms()
        for i in range(len(buttons)):
            current = debounced[i].update()
            if current != last[i]:
                last[i] = current
                down_since[i] = resync_now if current else None
                acted[i] = False
                log("btn " + button_labels[i] +
                    (" down" if current else " up") + " (resync)")

    # Heartbeat timestamp. Any action resets it, so the log below only appears
    # after HEARTBEAT_MS with no button activity - a clean "still alive" marker.
    last_beat = time.ticks_ms()

    # --- Rotary encoder (pump volume) state --------------------------------
    # Decode itself lives in lib/encoder.py (shared with the bench test).
    # vol_log_pending / last_step_ts implement the "log once when the knob
    # stops moving" behaviour.
    vol_log_pending = False
    last_step_ts = time.ticks_ms()
    sw_db = Debounced(lambda: button_is_pressed(enc_sw.value()))
    sw_last = sw_db.stable
    sw_down_since = None
    sw_acted = False
    last_tick_second = -1    # 1 Hz repaint guard for the running-timer readout
    last_clock_text = None   # last header string drawn
    last_clock_minute = None  # 1-minute recomputation guard for the clock
    last_timer_poll = time.ticks_ms()
    last_action_ts = 0       # skip the timer poll right after a press
    last_activity_ts = time.ticks_ms()  # last button/encoder input (display timeout)

    while True:
        now = time.ticks_ms()
        # Clock header: recomputed once per minute (not every loop pass), and
        # cleared entirely once the last successful sync is 24 h old - the Pico
        # has no battery-backed RTC, so an old time must not look current.
        if clock_synced:
            if (last_clock_ok is not None
                    and time.ticks_diff(now, last_clock_ok) >= CLOCK_STALE_MS):
                clock_synced = False
                last_clock_text = None
                last_clock_minute = None
                display.set_header("")
                log("Clock: stale (no NTP for 24h) - top line cleared")
            else:
                minute = int(time.time()) // 60
                if minute != last_clock_minute:
                    last_clock_minute = minute
                    stamp = fmt_clock(minute * 60)
                    if stamp != last_clock_text:
                        last_clock_text = stamp
                        display.set_header(stamp)
        for index in range(len(buttons)):
            down = debounced[index].update()
            if down != last[index]:
                # An edge happened on this button (pressed or released).
                last_activity_ts = now
                asleep = display.asleep()
                wake_display()
                if down and asleep:
                    # The panel was off: this press only wakes it and is
                    # CONSUMED - no action. down_since stays None, so the fire
                    # guard below can never fire from it; the release resets
                    # the state anyway. The screen lighting up is the
                    # feedback; a second deliberate press acts.
                    last[index] = down
                    log("btn " + button_labels[index] + " down (wake only, no action)")
                else:
                    if down:
                        # A fresh press begins (debounced below).
                        down_since[index] = now
                        acted[index] = False
                    else:
                        # Released - ready for the next press.
                        down_since[index] = None
                        acted[index] = False
                    last[index] = down
                    # Edge log: the diagnostic trail for a "press did nothing"
                    # report (§3.2) - a fired press always logs here first.
                    log("btn " + button_labels[index] + (" down" if down else " up"))

            # Fire on a deliberate press, exactly once per press. Only while the
            # button is still held and MIN_HOLD_MS has elapsed - so a quick tap
            # never registers, and a held/stuck button neither hangs the loop
            # nor double-fires.
            if (down and down_since[index] is not None and not acted[index]
                    and time.ticks_diff(now, down_since[index]) >= MIN_HOLD_MS):
                acted[index] = True
                # Instant optimistic feedback: the press IS registered now. The
                # real result (Recorded / started / ended / FAILED / Retrying)
                # replaces this as soon as the API answers - before this line
                # the screen only changed ~1-3 s later (after the network call
                # + LED sequence), which made presses feel unresponsive.
                note(button_labels[index], "recording...")
                led_acknowledge()  # "I saw your press" (result comes after the call)
                _handle_action(button_actions[index], button_labels[index],
                               timed=button_timed[index],
                               press_ts=down_since[index])
                last_action_ts = time.ticks_ms()
                last_beat = time.ticks_ms()  # activity - reset the idle heartbeat
                resync_buttons()

        # --- Rotary encoder: completed detents since the last poll ----------
        enc_steps = enc.poll()
        if enc_steps != 0:
            last_activity_ts = time.ticks_ms()
            wake_display()
            # Reversal: on this wiring a positive decode is a counter-clockwise
            # turn, so invert the sign to make CLOCKWISE = increase volume.
            # Apply ALL steps first, then give ONE nudge: led_nudge() sleeps
            # 50 ms, so a per-detent blip would block polling mid-flick and
            # lose steps on a fast turn.
            delta = -1 if enc_steps > 0 else 1
            for _ in range(abs(enc_steps)):
                pump_volume_step(delta)
            last_step_ts = time.ticks_ms()
            vol_log_pending = True
            led_nudge()  # quick blip: "knob registered"
            note("Pump Volume", str(pump_volume) + " ml")

        # Once the knob has been still long enough, save the selected volume.
        # Only when the value actually changed (so a turn-and-reverse back to
        # the saved value writes nothing), and only after PUMP_SAVE_DELAY_MS -
        # a slow multi-detent turn therefore costs ONE flash write total.
        if (vol_log_pending
                and time.ticks_diff(time.ticks_ms(), last_step_ts) >= PUMP_SAVE_DELAY_MS):
            vol_log_pending = False
            if pump_volume == last_saved_volume:
                pass  # back to the saved value: nothing to write
            elif save_pump_volume(pump_volume):
                log("Pump volume: " + str(pump_volume) + " ml (saved)")
            else:
                log("Pump volume: " + str(pump_volume) + " ml (could not save to flash)")
                note("Volume", "not saved")  # on-screen warning (log has the detail)

        # Center push button: fire the Breast Pump action (same edge-detect +
        # min-hold logic as the baby buttons).
        sw_down = sw_db.update()
        if sw_down != sw_last:
            last_activity_ts = time.ticks_ms()
            asleep = display.asleep()
            wake_display()
            if sw_down and asleep:
                # Panel was off: the center push only wakes it and is
                # CONSUMED - no action (same rule as the baby buttons).
                # sw_down_since stays None, so the fire guard below can never
                # fire from it; the release resets the state anyway.
                sw_last = sw_down
                log("enc sw down (wake only, no action)")
            else:
                if sw_down:
                    sw_down_since = time.ticks_ms()
                    sw_acted = False
                else:
                    sw_down_since = None
                    sw_acted = False
                sw_last = sw_down
                log("enc sw " + ("down" if sw_down else "up"))
        if (sw_down and sw_down_since is not None and not sw_acted
                and time.ticks_diff(time.ticks_ms(), sw_down_since) >= MIN_HOLD_MS):
            sw_acted = True
            note("Breast Pump", "recording...")  # instant feedback, as with the buttons
            led_acknowledge()  # "I saw your press" (result comes after the call)
            _handle_action(breast_pump_action, "Breast Pump",
                           timed=True, press_ts=sw_down_since)
            last_action_ts = time.ticks_ms()
            last_beat = time.ticks_ms()
            # Resync after the blocking action (see resync_buttons): a release
            # that happened while the call was in flight must be seen.
            resync_buttons()
            sw_current = sw_db.update()
            if sw_current != sw_last:
                sw_last = sw_current
                sw_down_since = time.ticks_ms() if sw_current else None
                sw_acted = False
                log("enc sw " + ("down" if sw_current else "up") + " (resync)")

        # --- Display: live readout for a running timer (1 Hz) ----------------
        # While a timer is running, keep its elapsed time on the screen. The
        # 4th line shows "since HH:MM" whenever the start time is known - Pico
        # timers store their start tick, phone timers carry the API's "start".
        # Only when neither is known (clock never synced + phone timer) do we
        # fall back to the bare "from the app". Status notes hold the screen
        # first (note_hold_until). While the panel is off after the idle
        # timeout, text() still refreshes the framebuffer, so a wake shows the
        # live elapsed time immediately.
        if (active_timer is not None
                and time.ticks_diff(time.ticks_ms(), note_hold_until) >= 0):
            since = active_timer.get("since")
            if active_timer["start_ms"] is not None:
                tick = time.ticks_diff(time.ticks_ms(), active_timer["start_ms"])
                if (tick // 1000) != last_tick_second:
                    last_tick_second = tick // 1000
                    foot = "since " + fmt_time(since) if since is not None else None
                    display.text(active_timer["label"], fmt_duration(tick), foot)
            elif since is not None:
                # Phone-started: elapsed from the API's start timestamp.
                tick = (int(time.time()) - since) * 1000
                if (tick // 1000) != last_tick_second:
                    last_tick_second = tick // 1000
                    display.text(active_timer["label"], fmt_duration(tick),
                                 "since " + fmt_time(since))
            elif last_tick_second != -2:
                last_tick_second = -2
                display.text(active_timer["label"], "from the app")

        # --- Home screen: a status note has expired and nothing is running ----
        # Instead of freezing on the last note for up to the 5-minute idle
        # timeout, fall back to the idle screen (last event + pump volume).
        # (While a timer runs, the 1 Hz readout above owns the screen.)
        if (active_timer is None
                and note_hold_until != 0
                and time.ticks_diff(time.ticks_ms(), note_hold_until) >= 0):
            note_hold_until = 0
            home_screen()

        # --- Housekeeping every 20 s (60 s while no timer runs; skipped for
        # 5 s after a press, so it can't clobber an in-flight action or its
        # result message) ------------------------------------------------------
        # First: notice a DROPPED WiFi. After boot nothing else watches the
        # radio - without this, a router reboot leaves the "ready" LED + a
        # stale screen, and the next press blocks ~15 s on the API timeout
        # before failing. ensure_wifi() is just an isconnected() read when the
        # link is up, so this costs nothing normally; when it's down it
        # re-scans + rejoins with the same escalating warm-up as at boot and
        # shows each stage on the screen (and the warm-up counter keeps
        # escalating across rounds while it stays down).
        # Then, only when the link is up: one small GET /timers so timers
        # started or ended on a phone still show on the display.
        # While a timer runs, poll every TIMER_POLL_MS; with none running,
        # every TIMER_POLL_IDLE_MS (a phone-started timer only needs
        # minute-scale latency to show up, and it saves battery).
        poll_ms = TIMER_POLL_MS if active_timer is not None else TIMER_POLL_IDLE_MS
        if (time.ticks_diff(time.ticks_ms(), last_timer_poll) >= poll_ms
                and time.ticks_diff(time.ticks_ms(), last_action_ts) >= 5000):
            last_timer_poll = time.ticks_ms()
            if not pico_connection.wlan.isconnected():
                log("WiFi dropped - reconnecting...")
                display.text("WiFi", "reconnecting...")
                if not ensure_wifi(rescan=True):
                    _wifi_rounds_failed += 1  # next round gets a longer warm-up
            else:
                refresh_timer_state()
                if clock_due():
                    sync_clock()

        # --- OLED idle timeout -------------------------------------------------
        # Datasheet: continuous operation dims the panel after ~1000 h, so turn
        # it off when it is not needed: after DISPLAY_OFF_MS with no button/
        # encoder input, running timers included. The only carve-out is a held
        # status message (note_hold_until); any input edge wakes it, and a
        # press while it is off only wakes it - it does not act.
        if (display.awake
                and time.ticks_diff(time.ticks_ms(), note_hold_until) >= 0
                and time.ticks_diff(time.ticks_ms(), last_activity_ts) >= DISPLAY_OFF_MS):
            if display.sleep():
                log("Display off (idle)")

        # Heartbeat: after HEARTBEAT_MS with no button activity, prove we're alive
        # and waiting. If the loop ever froze, this line would stop appearing.
        if time.ticks_diff(time.ticks_ms(), last_beat) >= HEARTBEAT_MS:
            log("Alive: idle, waiting for buttons")
            last_beat = time.ticks_ms()

        # Pace the idle poll (no button pressed) so we don't spin at 100% CPU /
        # drain battery; ~100 Hz is far more than enough for button debounce.
        # While the knob is actively moving (last 300 ms), poll faster (~500 Hz)
        # so a fast flick does not outrun the quadrature decoder; after it
        # stops, relax back to 100 Hz for the (up to 2 s) save window.
        knob_moving = (vol_log_pending
                       and time.ticks_diff(time.ticks_ms(), last_step_ts) < 300)
        time.sleep(0.002 if knob_moving else 0.01)


# Begin button checking loop
button_pressed()


