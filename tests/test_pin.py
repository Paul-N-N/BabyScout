from lib.pin import Debounced


def test_debounce_requires_three_stable_samples():
    level = {"value": 0}
    debounced = Debounced(lambda: level["value"])

    level["value"] = 1
    assert debounced.update() == 0
    assert debounced.update() == 0
    assert debounced.update() == 1

    level["value"] = 0
    assert debounced.update() == 1
    assert debounced.update() == 1
    assert debounced.update() == 0


def test_debounce_ignores_a_single_sample_glitch():
    level = {"value": 0}
    debounced = Debounced(lambda: level["value"])

    level["value"] = 1
    debounced.update()
    level["value"] = 0
    assert debounced.update() == 0
    assert debounced.update() == 0
    assert debounced.update() == 0
