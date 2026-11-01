import ujson as json
import time

# Base for log timestamps. Relative to when this module first loaded (~ boot).
# We use elapsed-since-boot (not wall-clock) so it works without NTP, and the
# gap between two consecutive log lines is the true elapsed time between them.
_boot_ticks = time.ticks_ms()


def log(msg):
    """
    Print a log line prefixed with elapsed seconds since boot, e.g.
    "[12.3s] Waiting for BabyBuddy...". Relative (no NTP needed) and lets you
    read the real interval between events directly.
    """
    ms = time.ticks_diff(time.ticks_ms(), _boot_ticks)
    line = "[" + str(ms // 1000) + "." + str((ms // 100) % 10) + "s] " + str(msg)
    print(line)


def fmt_duration(ms):
    """
    Human-readable duration from milliseconds, e.g.
      45000     -> "45s"
      432000    -> "7m 12s"
      3723000   -> "1h 02m 03s"
    Compact on purpose: every variant must stay within the OLED's 16-char
    line limit (this build's framebuf font is 8 px wide at scale 1).
    Used for the OLED timer readouts (running / ended).
    """
    total = max(0, ms) // 1000
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h:
        return str(h) + "h " + ("%02d" % m) + "m " + ("%02d" % s) + "s"
    if m:
        return str(m) + "m " + ("%02d" % s) + "s"
    return str(s) + "s"


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Local time zone for the OLED clock:
#   "US/Pacific" -> DST-aware US Pacific (the default)
#   int          -> fixed offset in minutes east of UTC, e.g. -300 for UTC-5
TIMEZONE = "US/Pacific"


def _pacific_offset_minutes(utc):
    """
    UTC offset in minutes for US Pacific at a UTC time tuple, including the
    US DST rule (second Sunday in March 10:00 UTC to first Sunday in
    November 09:00 UTC). The Pico has no timezone database, so this is the
    one place the local-time rule lives.
    """
    month, day, hour = utc[1], utc[2], utc[3]
    if month < 3 or month > 11:
        return -8 * 60
    if 3 < month < 11:
        return -7 * 60
    # tm_wday is Monday=0 .. Sunday=6. Work back to the first of the month.
    first_wday = (utc[6] - (day - 1)) % 7
    first_sunday = 1 + (6 - first_wday) % 7
    if month == 3:
        start_day = first_sunday + 7  # second Sunday
        if day > start_day or (day == start_day and hour >= 10):
            return -7 * 60
        return -8 * 60
    # November: DST ends on the first Sunday at 02:00 local daylight.
    if day < first_sunday or (day == first_sunday and hour < 9):
        return -7 * 60
    return -8 * 60


def _local_offset_minutes(utc):
    """Offset from UTC for TIMEZONE at a UTC time tuple, in minutes."""
    if TIMEZONE == "US/Pacific":
        return _pacific_offset_minutes(utc)
    if isinstance(TIMEZONE, int):
        return TIMEZONE
    return 0


def fmt_clock(epoch_seconds):
    """
    Format a UTC epoch as local 12-hour time for the OLED top line:
      "Sat 10/31 9:05P"   (15-16 chars; A/P = AM/PM)
    The weekday is kept, so a full "AM"/"PM" cannot fit the 16-char line.
    """
    utc = time.gmtime(epoch_seconds)
    offset = _local_offset_minutes(utc) * 60
    local = time.gmtime(epoch_seconds + offset)
    # fmt_time re-applies the offset from the UTC epoch itself, so pass the
    # original epoch (NOT the already-shifted one) - it yields the same local.
    return "%s %02d/%02d %s" % (
        _WEEKDAYS[local[6]], local[1], local[2],
        fmt_time(epoch_seconds),
    )


def fmt_time(epoch_seconds):
    """
    Local wall-clock time only, 12-hour: "9:05P" / "12:30A" (5-6 chars).
    Same TIMEZONE/DST handling as fmt_clock; used for the "since HH:MM"
    (running timer) and "at HH:MM" (home screen) context lines.
    """
    utc = time.gmtime(epoch_seconds)
    offset = _local_offset_minutes(utc) * 60
    local = time.gmtime(epoch_seconds + offset)
    hour = local[3]
    suffix = "A" if hour < 12 else "P"
    hour12 = hour % 12
    if hour12 == 0:
        hour12 = 12
    return "%d:%02d%s" % (hour12, local[4], suffix)


def _days_from_civil(year, month, day):
    """Days since 1970-01-01 for a civil date (proleptic Gregorian)."""
    year -= month <= 2
    era = (year if year >= 0 else year - 399) // 400
    year_of_era = year - era * 400
    day_of_year = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    day_of_era = (year_of_era * 365 + year_of_era // 4
                  - year_of_era // 100 + day_of_year)
    return era * 146097 + day_of_era - 719468


def iso_to_epoch(value):
    """
    BabyBuddy's ISO-8601 UTC timestamps ("2026-10-31T22:10:00.123456Z")
    to Unix epoch seconds, or None when not parseable. Only the Z (UTC)
    form is supported - that is what the API sends.
    """
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        date_part, time_part = value.split("T", 1)
        year, month, day = [int(part) for part in date_part.split("-")]
        if time_part.endswith("Z"):
            time_part = time_part[:-1]
        elif "+" in time_part:
            return None  # non-UTC offsets are not supported
        if "." in time_part:
            time_part = time_part.split(".", 1)[0]
        hour, minute, second = [int(part) for part in time_part.split(":")]
        return (_days_from_civil(year, month, day) * 86400
                + hour * 3600 + minute * 60 + second)
    except Exception:
        return None


# Parsed secrets.json, cached after the first successful load. The file is
# read on EVERY API request otherwise (flash read + JSON parse per button press
# and per 20 s timer poll). A failed load is not cached, so it is retried.
_secrets_cache = None


def retrieve_auth_variables(auth_path):
    """
    Open and JSON load authorization variables, all stuff contained in secrets.json

    The parsed dict is cached in memory after the first successful load (see
    _secrets_cache) so API calls don't touch flash every time. Callers must
    copy the AUTHORIZATION dict before adding per-request headers.

    params:

        auth_path(str): string of path that will be opened and json loaded.
    """
    global _secrets_cache
    if _secrets_cache is not None:
        return _secrets_cache
    try:
        with open(auth_path) as open_auth:
            read_auth = json.load(open_auth)
        if not isinstance(read_auth, dict):
            return None
        auth = read_auth.get("AUTHORIZATION")
        if isinstance(auth, dict):
            token = auth.get("Authorization")
            # Only add the scheme when one is missing: a token that already
            # starts with "Token " (or any other scheme) is left untouched.
            if isinstance(token, str) and not token.startswith("Token "):
                auth["Authorization"] = "Token " + token
        _secrets_cache = read_auth
        return read_auth
    except Exception:
        return None


def auth_variables_valid(loaded_auth):
    """
    Determines whether the loaded secrets.json values contain all necessary values

    params:
        loaded_auth(loaded json): json object imported into python that contains the secrets values
    """
    if not isinstance(loaded_auth, dict):
        return False
    ssids = loaded_auth.get("SSIDS_PASSWORD")
    base_url = loaded_auth.get("BASE_URL")
    auth = loaded_auth.get("AUTHORIZATION")
    if not isinstance(ssids, dict) or not ssids:
        return False
    if not isinstance(base_url, str) or not base_url:
        return False
    if not isinstance(auth, dict) or not auth.get("Authorization"):
        return False
    return True


def join_path(*args):
    """
    Takes a bunch of args, and joins them together to form a path

    params:
        args: all parts of the path e.g. join_path(test,best,rest) returns "test/best/rest"
    """
    path_list = list(args)
    return "/".join(path_list)

class partial:

    """
    Method used to pass a function/method with parameters but delay execution till called. Useful for storing functions in a list.

    params:

        func(function): Function you wish to delay calling
        args:(args): Any arguments you wish to pass to the function
        kwargs:(kwargs): Any keyword arguments you wish to pass to the function
    """
    def __init__(self, func, *args, **kwargs):
        self.func = func
        self.args = args
        self.kwargs = kwargs

    def __call__(self, *args, **kwds):
        return self.func(*self.args, *args, **kwds, **self.kwargs)