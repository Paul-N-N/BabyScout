import pytest

from lib import scout


class HttpError(Exception):
    def __init__(self, code):
        super().__init__("HTTP %d" % code)
        self.code = code


class FakeScout(scout.Scout):
    """Scout with the network layer replaced by in-memory timer state."""

    def __init__(self, timers=None):
        self.base_url = "http://babybuddy.test/api/"
        self.children = [1]
        self.child_index = 0
        self.timers = list(timers or [])
        self.calls = []

    def list_timers(self, child_id):
        return list(self.timers)

    def get_timer(self, child_id, activity):
        for timer in self.timers:
            if timer["name"] == activity:
                return timer
        return None

    def set_timer(self, child_id, activity):
        self.calls.append(("set", activity))
        timer = {"id": 99, "name": activity, "child": child_id}
        self.timers.append(timer)
        return timer

    def clear_timer(self, timer_id):
        self.calls.append(("clear", timer_id))
        self.timers = [timer for timer in self.timers if timer["id"] != timer_id]


def timer(timer_id, name="sleep", child=1):
    return {"id": timer_id, "name": name, "child": child}


def test_start_when_no_timer(monkeypatch):
    s = FakeScout()
    monkeypatch.setattr(scout, "send_api_request", lambda *args, **kwargs: {})

    result = s.resolve_timers(1, "sleep", label="Sleep")

    assert result == {"label": "Sleep", "started": True, "timer": "sleep"}
    assert s.calls == [("set", "sleep")]


def test_stop_stops_every_duplicate(monkeypatch):
    s = FakeScout([timer(5), timer(6)])
    sent = []

    def fake_request(base_url, path, data=None, **kwargs):
        sent.append(dict(data))
        return {}

    monkeypatch.setattr(scout, "send_api_request", fake_request)

    result = s.resolve_timers(1, "sleep", label="Sleep")

    assert result == {"label": "Sleep", "started": False, "timer": "sleep"}
    assert [payload["timer"] for payload in sent] == [5, 6]


def test_4xx_clears_the_stale_timer(monkeypatch):
    s = FakeScout([timer(5)])

    def fake_request(*args, **kwargs):
        raise HttpError(400)

    monkeypatch.setattr(scout, "send_api_request", fake_request)

    with pytest.raises(HttpError):
        s.resolve_timers(1, "sleep", label="Sleep")

    assert ("clear", 5) in s.calls
    assert s.timers == []


def test_network_error_does_not_clear_a_running_timer(monkeypatch):
    s = FakeScout([timer(5)])

    def fake_request(*args, **kwargs):
        raise OSError("connection reset")

    monkeypatch.setattr(scout, "send_api_request", fake_request)

    with pytest.raises(OSError):
        s.resolve_timers(1, "sleep", label="Sleep")

    # The stop may have failed cleanly: keep the timer so the retry can stop it.
    assert ("clear", 5) not in s.calls
    assert s.timers == [timer(5)]


def test_network_error_but_stop_applied_is_success(monkeypatch):
    s = FakeScout([timer(5)])

    def fake_request(*args, **kwargs):
        # The server applied the stop; the response was lost.
        s.timers = []
        raise OSError("response lost")

    monkeypatch.setattr(scout, "send_api_request", fake_request)

    result = s.resolve_timers(1, "sleep", label="Sleep")

    assert result == {"label": "Sleep", "started": False, "timer": "sleep"}


def test_network_error_but_start_applied_is_success(monkeypatch):
    s = FakeScout()

    def lost_set_timer(child_id, activity):
        s.timers.append(timer(7, activity, child_id))
        raise OSError("response lost")

    s.set_timer = lost_set_timer

    result = s.resolve_timers(1, "sleep", label="Sleep")

    assert result == {"label": "Sleep", "started": True, "timer": "sleep"}


def test_send_data_does_not_mutate_the_callers_dict(monkeypatch):
    captured = {}

    def fake_request(base_url, path, data=None, **kwargs):
        captured.update(data)
        return {}

    monkeypatch.setattr(scout, "send_api_request", fake_request)
    s = FakeScout()
    payload = {"wet": True}

    s.send_data(1, "changes", payload)

    assert payload == {"wet": True}
    assert captured == {"wet": True, "child": 1}


def test_base_url_gets_a_trailing_slash(monkeypatch):
    monkeypatch.setattr(scout.Scout, "init_children", lambda self, timeout=5: None)

    assert scout.Scout("http://babybuddy.test/api").base_url == "http://babybuddy.test/api/"
    assert scout.Scout("http://babybuddy.test/api/").base_url == "http://babybuddy.test/api/"
