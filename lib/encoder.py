# Quadrature decode for an EC11 rotary encoder. Kept in its own module (no
# hardware imports) so main.py and the bench test exercise the SAME code, and
# the logic can be unit-tested on a PC with fake pins.
#
# A and B are 90 deg apart, so a valid electrical step is a 1-bit change and
# WHICH bit changed tells the direction. A full mechanical detent is one full
# A/B cycle = 4 such transitions. 2-bit jumps (a missed transition) are
# deliberately absent and ignored.
ENCODER_DIR = {
    (0, 0, 1, 0): 1,
    (1, 0, 1, 1): 1,
    (1, 1, 0, 1): 1,
    (0, 1, 0, 0): 1,
    (0, 0, 0, 1): -1,
    (0, 1, 1, 1): -1,
    (1, 1, 1, 0): -1,
    (1, 0, 0, 0): -1,
}


class Encoder:
    """
    Polling quadrature decoder for an EC11 (channels a, b).

    A state must be seen on 2 consecutive polls before it is accepted
    (2-sample stability), so mechanical bounce can't register a step. poll()
    returns the signed number of detents completed since the previous poll
    (0 if none). Turning back mid-cycle cancels; an incomplete cycle stays in
    the accumulator for the next poll.

    a, b: anything with .value() -> 0/1 (machine.Pin on the Pico, or a fake
        object in tests).
    """

    def __init__(self, a, b):
        self.a = a
        self.b = b
        start = (a.value(), b.value())
        self.prev = start
        self.last = start
        self.stable = 1
        self.acc = 0

    def poll(self):
        cur = (self.a.value(), self.b.value())
        if cur != self.last:
            self.last = cur
            self.stable = 1
        else:
            self.stable += 1
        if self.stable == 2 and cur != self.prev:
            delta = ENCODER_DIR.get((self.prev[0], self.prev[1], cur[0], cur[1]), 0)
            self.prev = cur
            if delta:
                self.acc += delta
        steps = 0
        while abs(self.acc) >= 4:  # full A/B cycle (4 transitions) = one detent
            step = 1 if self.acc > 0 else -1
            self.acc -= 4 * step
            steps += step
        return steps