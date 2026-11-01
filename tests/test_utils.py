from lib import utils
import calendar


def test_fmt_duration_compact_and_bounded():
    assert utils.fmt_duration(0) == "0s"
    assert utils.fmt_duration(45_000) == "45s"
    assert utils.fmt_duration(432_000) == "7m 12s"
    assert utils.fmt_duration(3_723_000) == "1h 02m 03s"
    # Longest realistic value still fits the OLED's 16-char line.
    assert utils.fmt_duration(86_399_000) == "23h 59m 59s"
    assert len(utils.fmt_duration(86_399_000)) <= 16
    # Negative / clock skew is clamped, never a negative duration.
    assert utils.fmt_duration(-5) == "0s"


def test_fmt_clock_pacific_dst_12_hour_and_line_length():
    def epoch(*utc):
        return calendar.timegm(utc + (0, 0, 0, 0))

    # DST still active (before 09:00 UTC on the first Sunday in November).
    assert utils.fmt_clock(epoch(2026, 11, 1, 4, 5)) == "Sat 10/31 9:05P"
    # DST ended at 09:00 UTC: local time is now PST.
    assert utils.fmt_clock(epoch(2026, 11, 1, 10, 5)) == "Sun 11/01 2:05A"
    # DST starts at 10:00 UTC on the second Sunday in March.
    assert utils.fmt_clock(epoch(2026, 3, 8, 9, 59)) == "Sun 03/08 1:59A"
    assert utils.fmt_clock(epoch(2026, 3, 8, 10, 0)) == "Sun 03/08 3:00A"
    # Standard time in January; noon and midnight use 12, never 0.
    assert utils.fmt_clock(epoch(2026, 1, 15, 20, 0)) == "Thu 01/15 12:00P"
    assert utils.fmt_clock(epoch(2026, 1, 15, 8, 0)) == "Thu 01/15 12:00A"
    # Two-digit hours still fit the OLED's 16-char line.
    assert utils.fmt_clock(epoch(2026, 1, 16, 6, 5)) == "Thu 01/15 10:05P"
    # Every clock string must fit the OLED's 16-char line.
    for utc in ((2026, 11, 1, 4, 5), (2026, 1, 16, 6, 5),
                (2026, 3, 8, 10, 0)):
        assert len(utils.fmt_clock(epoch(*utc))) <= 16


def test_fmt_clock_accepts_a_fixed_offset(monkeypatch):
    monkeypatch.setattr(utils, "TIMEZONE", -5 * 60)
    epoch = calendar.timegm((2026, 1, 15, 20, 0, 0, 0, 0))
    assert utils.fmt_clock(epoch) == "Thu 01/15 3:00P"


def test_fmt_time_12_hour():
    def epoch(*utc):
        return calendar.timegm(utc + (0, 0, 0))

    # Local (US Pacific) times: noon/midnight edge and a normal hour.
    assert utils.fmt_time(epoch(2026, 1, 15, 20, 0)) == "12:00P"
    assert utils.fmt_time(epoch(2026, 1, 15, 8, 0)) == "12:00A"
    # 21:05 PDT (DST active) -> 9:05P
    assert utils.fmt_time(epoch(2026, 11, 1, 4, 5)) == "9:05P"
    # Fits the "since " / "at " context lines: "since 10:05P" is 13 chars.
    assert len("since " + utils.fmt_time(0)) <= 16


def test_iso_to_epoch_parses_babybuddy_timestamps():
    # Fractional seconds + Z (the form the API sends).
    assert (utils.iso_to_epoch("2026-10-31T22:10:00.123456Z")
            == calendar.timegm((2026, 10, 31, 22, 10, 0, 0, 0, 0)))
    # No fraction.
    assert (utils.iso_to_epoch("2026-03-08T10:00:00Z")
            == calendar.timegm((2026, 3, 8, 10, 0, 0, 0, 0, 0)))
    # Leap-year day count.
    assert (utils.iso_to_epoch("2024-02-29T23:59:59Z")
            == calendar.timegm((2024, 2, 29, 23, 59, 59, 0, 0, 0)))
    # Unparseable / unsupported forms -> None (never raises).
    assert utils.iso_to_epoch("garbage") is None
    assert utils.iso_to_epoch("2026-10-31 22:10:00Z") is None
    assert utils.iso_to_epoch("2026-03-08T10:00:00+05:00") is None
    assert utils.iso_to_epoch(None) is None
    assert utils.iso_to_epoch(12345) is None


def test_auth_variables_valid():
    assert not utils.auth_variables_valid(None)
    assert not utils.auth_variables_valid({})
    good = {
        "SSIDS_PASSWORD": {"wifi": "password"},
        "BASE_URL": "https://babybuddy.example/api/",
        "AUTHORIZATION": {"Authorization": "Token abc"},
    }
    assert utils.auth_variables_valid(good)
    assert not utils.auth_variables_valid(
        {"SSIDS_PASSWORD": {}, "BASE_URL": "x", "AUTHORIZATION": {"Authorization": "t"}}
    )
    assert not utils.auth_variables_valid(
        {"SSIDS_PASSWORD": {"w": "p"}, "BASE_URL": "x", "AUTHORIZATION": {}}
    )


def test_retrieve_auth_variables_adds_scheme_and_caches(tmp_path):
    utils._secrets_cache = None
    path = tmp_path / "secrets.json"
    path.write_text('{"AUTHORIZATION": {"Authorization": "abc"}}')

    loaded = utils.retrieve_auth_variables(str(path))
    assert loaded["AUTHORIZATION"]["Authorization"] == "Token abc"

    # A later call is served from memory (the file on disk is ignored).
    path.write_text("this is not json")
    assert utils.retrieve_auth_variables(str(path)) is loaded

    utils._secrets_cache = None


def test_retrieve_auth_variables_does_not_double_prefix(tmp_path):
    utils._secrets_cache = None
    path = tmp_path / "secrets.json"
    path.write_text('{"AUTHORIZATION": {"Authorization": "Token abc"}}')

    loaded = utils.retrieve_auth_variables(str(path))
    assert loaded["AUTHORIZATION"]["Authorization"] == "Token abc"

    utils._secrets_cache = None
