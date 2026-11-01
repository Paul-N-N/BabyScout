from lib.encoder import Encoder


class FakePin:
    def __init__(self, value=0):
        self._value = value

    def value(self):
        return self._value

    def set(self, value):
        self._value = value


def turn(a, b, states):
    """Feed A/B states; each state needs 2 polls (2-sample stability)."""
    enc = Encoder(a, b)
    steps = 0
    for state in states:
        a.set(state[0])
        b.set(state[1])
        enc.poll()          # first sample: state seen once
        steps += enc.poll()  # second sample: accepted
    return steps


FORWARD = [(1, 0), (1, 1), (0, 1), (0, 0)]
REVERSE = [(0, 1), (1, 1), (1, 0), (0, 0)]


def test_one_detent_forward():
    assert turn(FakePin(0), FakePin(0), FORWARD) == 1


def test_one_detent_reverse():
    assert turn(FakePin(0), FakePin(0), REVERSE) == -1


def test_multiple_detents_accumulate():
    assert turn(FakePin(0), FakePin(0), FORWARD * 3) == 3
    assert turn(FakePin(0), FakePin(0), REVERSE * 2) == -2


def test_two_bit_jump_is_ignored():
    # (0,0) -> (1,1) skips a state: a missed transition, not a valid step.
    assert turn(FakePin(0), FakePin(0), [(1, 1)]) == 0


def test_bounce_does_not_register():
    a, b = FakePin(0), FakePin(0)
    enc = Encoder(a, b)
    a.set(1)
    b.set(0)
    assert enc.poll() == 0  # seen once - not stable yet
    a.set(0)
    b.set(0)
    assert enc.poll() == 0  # back to the previous state before acceptance
    assert enc.poll() == 0
