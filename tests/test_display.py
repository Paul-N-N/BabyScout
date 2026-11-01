from lib.display import Display


class FakeBuffer:
    def __init__(self):
        self.calls = []

    def fill(self, value):
        self.calls.append(("fill", value))

    def text(self, string, x, y, color):
        self.calls.append(("text", string, x, y, color))

    def show(self):
        self.calls.append(("show",))

    def poweroff(self):
        self.calls.append(("poweroff",))

    def poweron(self):
        self.calls.append(("poweron",))

    def contrast(self, value):
        self.calls.append(("contrast", value))


def make_display():
    # Bypass __init__ (which would need real I2C hardware).
    display = Display.__new__(Display)
    display.buf = FakeBuffer()
    display.ok = True
    display.height = 64
    display.header = None
    display.last = None
    display.awake = True
    display.dirty = False
    return display


def test_text_is_capped_to_16_chars():
    display = make_display()
    display.text("x" * 20, "y" * 20)
    texts = [call for call in display.buf.calls if call[0] == "text"]
    assert texts[0][1] == "x" * 16
    assert texts[1][1] == "y" * 16


def test_identical_redraw_is_skipped():
    display = make_display()
    display.text("Sleep", "45s")
    drawn = len(display.buf.calls)
    display.text("Sleep", "45s")
    assert len(display.buf.calls) == drawn


def test_changed_text_redraws():
    display = make_display()
    display.text("Sleep", "45s")
    display.text("Sleep", "46s")
    texts = [call[1] for call in display.buf.calls if call[0] == "text"]
    assert texts == ["Sleep", "45s", "Sleep", "46s"]


def test_header_is_drawn_at_the_top():
    display = make_display()
    display.header = "Sat Oct 31 21:05"
    display.text("Sleep", "45s")
    texts = [call for call in display.buf.calls if call[0] == "text"]
    assert texts[0] == ("text", "Sat Oct 31 21:05", 0, 0, 1)
    assert texts[1] == ("text", "Sleep", 0, 20, 1)


def test_foot_line_is_the_spare_4th_line():
    display = make_display()
    display.text("Sleep", "1h 02m 03s", "since 9:41P")
    texts = [call for call in display.buf.calls if call[0] == "text"]
    assert texts[0] == ("text", "Sleep", 0, 20, 1)
    assert texts[1] == ("text", "1h 02m 03s", 0, 36, 1)
    assert texts[2] == ("text", "since 9:41P", 0, 48, 1)


def test_foot_omitted_when_none_and_in_redraw_cache():
    display = make_display()
    display.text("Sleep", "45s")
    drawn = len(display.buf.calls)
    # Same lines, no foot: identical to the cached (h, t, d, f) -> skipped.
    display.text("Sleep", "45s", None)
    assert len(display.buf.calls) == drawn
    # A foot appearing forces a repaint...
    display.text("Sleep", "45s", "since 9:41P")
    with_foot = len(display.buf.calls)
    assert ("text", "since 9:41P", 0, 48, 1) in display.buf.calls
    # ...and it being dropped again forces another.
    display.text("Sleep", "45s")
    assert len(display.buf.calls) > with_foot


def test_set_header_keeps_the_foot_line():
    display = make_display()
    display.text("Sleep", "45s", "since 9:41P")
    display.set_header("Sat Oct 31 21:05")
    texts = [call for call in display.buf.calls
             if call[0] == "text" and call[1] == "since 9:41P"]
    assert texts[-1] == ("text", "since 9:41P", 0, 48, 1)


def test_small_panel_has_no_foot_line():
    display = make_display()
    display.height = 32
    display.text("Sleep", "45s", "since 9:41P")
    ys = sorted({call[3] for call in display.buf.calls if call[0] == "text"})
    assert max(ys) < 32  # nothing drawn below the 4-row panel
    assert ("text", "since 9:41P", 0, 48, 1) not in display.buf.calls


def test_set_header_repaints_the_current_screen():
    display = make_display()
    display.text("Sleep", "45s")
    display.set_header("Sat Oct 31 21:05")
    display.set_header("Sat Oct 31 21:06")
    headers = [call[1] for call in display.buf.calls
               if call[0] == "text" and call[3] == 0]
    assert headers == ["Sat Oct 31 21:05", "Sat Oct 31 21:06"]
    # The title/detail still come from the screen that was already showing.
    texts = [call[1] for call in display.buf.calls if call[0] == "text"]
    assert texts[-3:] == ["Sat Oct 31 21:06", "Sleep", "45s"]


def test_missing_display_is_a_noop():
    display = Display.__new__(Display)
    display.buf = None
    display.ok = False
    display.height = 64
    display.header = None
    display.last = None
    display.awake = False
    display.dirty = False
    display.text("Sleep", "45s")  # must not raise
    assert display.sleep() is False
    assert display.wake() is False


def test_sleep_powers_off_and_wake_pushes_changes():
    display = make_display()
    display.text("Sleep", "45s")
    assert display.sleep() is True
    assert ("poweroff",) in display.buf.calls

    # While asleep, text updates the framebuffer but does not touch the panel.
    before = len(display.buf.calls)
    display.text("Sleep", "46s")
    new_calls = display.buf.calls[before:]
    assert ("show",) not in new_calls
    assert display.dirty is True

    assert display.wake() is True
    assert ("poweron",) in display.buf.calls
    assert ("show",) in display.buf.calls
    assert display.dirty is False


def test_sleep_is_a_noop_when_already_asleep():
    display = make_display()
    assert display.sleep() is True
    assert display.sleep() is False
    assert display.wake() is True
    assert display.wake() is False


def test_asleep_only_when_fitted_and_powered_off():
    # No display fitted: never "asleep" - there is nothing to wake, so input
    # must act normally instead of being consumed as a wake.
    display = Display.__new__(Display)
    display.buf = None
    display.ok = False
    display.height = 64
    display.header = None
    display.last = None
    display.awake = False
    display.dirty = False
    assert display.asleep() is False

    # Fitted and on: not asleep. Idle timeout powers it off: asleep. Wake: on.
    display = make_display()
    assert display.asleep() is False
    assert display.sleep() is True
    assert display.asleep() is True
    assert display.wake() is True
    assert display.asleep() is False
