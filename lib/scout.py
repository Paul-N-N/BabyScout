from lib import utils
import urequests as requests
import ujson as json
import uos as os
import time


# Request timeouts (seconds).
# CONNECT_TIMEOUT: the short "is BabyBuddy up?" probe in the boot/wait loop.
#   Kept short so the LED can re-blink + retry quickly instead of hanging on a
#   slow Cloudflare 502.
# ACTION_TIMEOUT: a real recording (button press). A live BabyBuddy call is
#   well under a second, so 15 s only matters when the network is stalled -
#   and capping it makes a stuck request fail fast + clearly instead of
#   hanging the whole device forever (the action retry + FAILED screen in
#   main.py is the last-resort net).
CONNECT_TIMEOUT = 5
ACTION_TIMEOUT = 15


class Scout:
    """
    Object for handling interactions with BabyBuddy Instance

    params:

        base_url(str): url that points to the babybuddy api e.g. 192.168.1.20:32000/api/
    """
    def __init__(self, base_url, timeout=CONNECT_TIMEOUT):
        # Normalise the trailing slash once: send_api_request() builds URLs as
        # base_url + path + "/", so a BASE_URL without it would 404.
        if base_url and not base_url.endswith("/"):
            base_url += "/"
        self.base_url = base_url
        self.children = []
        self.init_children(timeout=timeout)
        self.child_index = None

    def init_children(self, timeout=CONNECT_TIMEOUT):
        """
        Since children can be assigned numbers that may not be sequential, find and store them for later.
        Creates a list of children ids

        timeout(float): seconds before this "is BabyBuddy up?" probe gives up.
            Kept short so the wait loop re-blinks + retries quickly instead of
            hanging on a slow 502 from the Cloudflare tunnel.
        """
        path = "children"
        children = send_api_request(self.base_url, path, timeout=timeout)["results"]
        for child in children:
            self.children.append(child["id"])

    def send_data(self, child_id, activity, data=None):
        """
        Method for sending api request for specific activities for a specific child

        params:
            child_id(int): ID corresponding to child you wish to perform a specific action for
            activity(str): String identifier of the activity being performed
            data(dict): Dictionary containing extra information e.g. timers etc.

        """
        # Copy so the caller's dict is never mutated (the old default {} was
        # shared between calls and got a "child" key added on every request).
        payload = dict(data) if data else {}
        payload["child"] = child_id
        return send_api_request(self.base_url, activity, data=payload)

    def resolve_timers(self, child_id, activity, data=None, label=None):
        """
        Toggle a timed activity (feedings, sleep, ...):
          - no timer running  -> START one (first press).
          - timer(s) running  -> END them, which records the activity with its
                                elapsed time. ALL matching timers are stopped:
                                duplicates left behind by the phone app would
                                otherwise need one press each.

        Robustness:
          * A 4xx rejection when stopping (e.g. 'Duration too long') means the
            stale timer can never be recorded, so it is cleared and the error
            re-raised for a clear, non-retrying failure.
          * A transient error (network / 5xx) is AMBIGUOUS: the request may
            have been applied even though the response was lost. Before
            re-raising, the timer list is re-read; if the state already
            changed, the action is reported as done. This makes the caller's
            one retry safe - blindly retrying a toggle could otherwise start a
            new timer right after a successful stop (or stop one right after a
            successful start).

        params:
            child_id(int): child to act for
            activity(str): endpoint, e.g. "feedings" / "sleep"
            data(dict): extra fields sent when recording (e.g. feeding type/method)
            label(str): human name used in the status log (defaults to activity)

        returns dict: {"label": <name>, "started": True|False, "timer": <activity>}
            so the caller can show "started" vs "ended <duration>" and keep
            tracking the running timer by its API name (see main.py).
        """
        name = label or activity
        matching = [
            timer for timer in self.list_timers(child_id)
            if timer.get("name") == activity
        ]
        if not matching:
            try:
                self.set_timer(child_id, activity)
            except Exception:
                # Ambiguous start: if a timer appeared anyway, the request was
                # applied and only the response was lost.
                try:
                    if self.get_timer(child_id, activity) is not None:
                        utils.log("Started " + name +
                                  " (response lost, timer is running).")
                        return {"label": name, "started": True, "timer": activity}
                except Exception:
                    pass
                raise
            utils.log("Started " + name + " - press again to stop & record it.")
            return {"label": name, "started": True, "timer": activity}

        end_data = dict(data) if data else {}
        try:
            for timer in matching:
                end_data["timer"] = timer["id"]
                send_api_request(self.base_url, activity, data=end_data)
        except Exception as e:
            code = getattr(e, "code", None)
            if code is not None and 400 <= code < 500:
                # Rejected (typically 400 'Duration too long'): this timer was
                # left running far too long and can never be recorded. Clear it
                # so the next press starts fresh, then re-raise so the caller
                # shows a clear (non-retrying) failure.
                cleared = 0
                for timer in matching:
                    try:
                        self.clear_timer(timer["id"])
                        cleared += 1
                    except Exception:
                        pass
                if cleared:
                    utils.log("Cleared stale " + name +
                              " (it had been running too long).")
                else:
                    utils.log("Could not auto-clear the stale " + name +
                              " - clear it in BabyBuddy, then start a fresh one.")
                raise
            # Transient failure: did the stop actually apply before the
            # response was lost? If the timer is gone, treat it as recorded.
            try:
                if self.get_timer(child_id, activity) is None:
                    utils.log("Recorded " + name + " (response lost, timer is gone).")
                    return {"label": name, "started": False, "timer": activity}
            except Exception:
                pass
            raise
        utils.log("Recorded " + name + ".")
        return {"label": name, "started": False, "timer": activity}

    def set_timer(self, child_id, activity):
        """
        Call that instantiates a new timer

        params:
            child_id(int): ID corresponding to child you wish to perform a specific action for
            activity(str): String identifier of the activity being performed
        """
        path = "timers"
        timer = send_api_request(
            self.base_url, path=path, data={"child": child_id, "name": activity}
        )
        return timer

    def get_timer(self, child_id, activity):
        """
        Returns the active timer that matches a given activity type (or None).

        params:
            child_id(int): ID corresponding to child you wish to perform a specific action for
            activity(str): String identifier of the activity being performed
        """
        for timer in self.list_timers(child_id):
            if timer["name"] == activity:
                return timer
        return None

    def list_timers(self, child_id):
        """
        Return ALL active timers for this child, any activity type.
        Used to notice timers started or ended elsewhere (e.g. the BabyBuddy
        app on a phone) so the Pico's display can still reflect them.

        params:
            child_id(int): child whose timers to list
        """
        timer_response = send_api_request(self.base_url, path="timers")
        return [
            t for t in timer_response.get("results", [])
            if t.get("child") == child_id
        ]

    def clear_timer(self, timer_id):
        """
        Cancel (delete) a running timer WITHOUT recording it. Used to discard a
        stale timer (one left running so long that BabyBuddy rejects it as
        'Duration too long') so it can't keep failing on every press.
        """
        send_api_request(
            self.base_url, "timers/" + str(timer_id), method="delete", timeout=CONNECT_TIMEOUT
        )

    def sleep(self, child_id):
        """
        Toggle a sleep timer (first press starts, second stops + records).
        params:
            child_id(int): ID corresponding to child you wish to perform a specific action for
        """
        return self.resolve_timers(child_id, "sleep", label="Sleep")

    def tummy_time(self, child_id):
        """
        Toggle a tummy-time timer (first press starts, second stops + records).
        params:
            child_id(int): ID corresponding to child you wish to perform a specific action for
        """
        return self.resolve_timers(child_id, "tummy-times", label="Tummy Time")

    def breast_pump(self, child_id, amount=0):
        """
        Log a breast-pump session. BabyBuddy's 'pumping' event is TIMED (needs
        start + end) plus a required 'amount' (pumped volume, ml), so this
        reuses the same sleep / tummy-time start/stop toggle: first press
        STARTS it, second press ENDS + records it. BabyBuddy stamps the real
        times (the Pico has no clock). resolve_timers logs the status itself
        ("Started Breast Pump..." / "Recorded Breast Pump.").

        amount(int): pumped volume in ml - e.g. the value selected on the
            rotary encoder (0 = "pumped, volume not entered").
        """
        return self.resolve_timers(child_id, "pumping", data={"amount": amount}, label="Breast Pump")

    def wet_diaper(self, child_id):
        """
        Log wet diaper for a given child
        params:
            child_id(int): ID corresponding to child you wish to perform a specific action for
        """
        activity = "changes"
        data = {"wet": True, "solid": False}
        self.send_data(child_id, activity, data)
        utils.log("Recorded Wet Diaper Change")

    def solid_diaper(self, child_id):
        """
        Log solid diaper for a given child
        params:
            child_id(int): ID corresponding to child you wish to perform a specific action for
        """
        activity = "changes"
        data = {"wet": False, "solid": True}
        self.send_data(child_id, activity, data)
        utils.log("Recorded Solid Diaper Change")

    def wet_solid_diaper(self, child_id):
        """
        Log wet and solif diaper for a given child
        params:
            child_id(int): ID corresponding to child you wish to perform a specific action for
        """
        activity = "changes"
        data = {"wet": True, "solid": True}
        self.send_data(child_id, activity, data)
        utils.log("Recorded Wet & Solid Diaper Change")

    def breast_feed(self, child_id):
        """
        Toggle a feeding timer (first press starts, second stops + records).
        params:
            child_id(int): ID corresponding to child you wish to perform a specific action for
        """
        return self.resolve_timers(
            child_id, "feedings",
            {"type": "breast milk", "method": "both breasts"},
            label="Breast Feed",
        )

    def left_breast(self, child_id):
        """
        Toggle a feeding timer (first press starts, second stops + records).
        params:
            child_id(int): ID corresponding to child you wish to perform a specific action for
        """
        return self.resolve_timers(
            child_id, "feedings",
            {"type": "breast milk", "method": "left breast"},
            label="Left Breast",
        )

    def right_breast(self, child_id):
        """
        Toggle a feeding timer (first press starts, second stops + records).
        params:
            child_id(int): ID corresponding to child you wish to perform a specific action for
        """
        return self.resolve_timers(
            child_id, "feedings",
            {"type": "breast milk", "method": "right breast"},
            label="Right Breast",
        )

    def bottle_feed(self, child_id):
        """
        Toggle a feeding timer (first press starts, second stops + records).
        params:
            child_id(int): ID corresponding to child you wish to perform a specific action for
        """
        return self.resolve_timers(
            child_id, "feedings",
            {"type": "breast milk", "method": "bottle"},
            label="Bottle Feed",
        )

    def next_child(self):
        """
        Method that allows stepping to the next child in list, or returning back to the start
        """
        if len(self.children) == 0:
            raise IndexError("No children setup in BabyBuddy")
        if self.child_index == None:
            self.child_index = 0
            return self.children[0]
        max_index = len(self.children) - 1
        new_index = self.child_index + 1
        if new_index > max_index:
            new_index = 0
        self.child_index = new_index
        return self.children[new_index]

def connect_to_baby_buddy(base_url, retries=5, delay=2):
    """
    Ensure that connectivity can be established to a BabyBuddy instance.
    Retries a bounded number of times to ride out transient failures or a slow
    startup, then raises OSError so the caller can retry (rather than looping
    forever here or crashing on a non-JSON response).
    """
    for attempt in range(1, retries + 1):
        try:
            # Scout's default timeout is 5s (short), so this probe gives up
            # quickly -> the LED can re-blink + retry instead of hanging on a
            # slow Cloudflare 502. (Button actions use ACTION_TIMEOUT.)
            baby_buddy = Scout(base_url)
        except (OSError, ValueError) as e:
            # OSError: host unreachable / connection refused / timeout.
            # ValueError: a response came back but it wasn't valid JSON
            # (e.g. a captive-portal HTML page while BabyBuddy is down).
            utils.log(f"Failed to connect to BabyBuddy (attempt {attempt}/{retries}): {e}")
            time.sleep(delay)
            continue
        utils.log("Connected to BabyBuddy")
        return baby_buddy
    raise OSError("Failed to connect to BabyBuddy")


def _parse_json_response(response, url):
    """
    Parse a BabyBuddy response as JSON. A non-JSON body (an HTML error page, a
    Cloudflare/origin error, a captive-portal or WAF block ...) is reported with
    the HTTP status and a body snippet so the cause is obvious, then ValueError
    is raised (the wait-loop treats it as "not ready yet" and keeps retrying).
    """
    body = response.content
    status = getattr(response, "status_code", None)

    # HTTP error (Cloudflare or the origin). Classify it so the operator knows
    # whether to keep waiting (5xx) or fix the config (4xx).
    if status is not None and not (200 <= int(status) < 300):
        s = int(status)
        if 500 <= s < 600:
            detail = ("HTTP " + str(s) + " = ORIGIN/SERVER error (BabyBuddy down or the "
                      "Cloudflare tunnel down). TRANSIENT - keep retrying; it will "
                      "connect the moment the origin comes back.")
        elif 400 <= s < 500:
            detail = ("HTTP " + str(s) + " = CLIENT error (400 = the request itself is "
                      "rejected, e.g. a feeding left running too long; 401/403 = bad "
                      "AUTHORIZATION; 404 = wrong BASE_URL path). Retrying is unlikely "
                      "to help.")
        else:
            detail = "HTTP " + str(s) + " (unexpected status) from " + url
        snippet = (body or b"")[:200]
        error = ValueError(detail + " from " + url + " Body[:200]=" + repr(snippet))
        error.code = s  # callers use this to tell 4xx (don't retry) from 5xx (retry)
        raise error

    # 2xx (or unknown status) but the body is still not valid JSON.
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        snippet = (body or b"")[:200]
        raise ValueError("HTTP " + str(status) + " from " + url +
                         " but body is not JSON: " + repr(snippet))


def send_api_request(base_url, path, headers=None, data=None, timeout=ACTION_TIMEOUT, method=None):
    """
    Function used to send api requests to BabyBuddy Instance

    params:

        base_url(str): URL that points to BabyBuddy API
        path(str): Or activity to hit the correct api endpoint
        headers(dict): Contains necessary authorization headers
        data(dict): All data necessary to complete a api post request
        timeout(float): seconds before the request gives up. Defaults to
            ACTION_TIMEOUT (button recordings) so a stalled network fails
            fast + clearly; the boot probe passes CONNECT_TIMEOUT.
        method(str): optional "get" | "post" | "delete". When omitted it is
            inferred (POST if there is data, else GET), preserving the original
            behaviour. DELETE is used to clear a stale timer.
    """
    secrets = utils.retrieve_auth_variables(
        utils.join_path(os.getcwd(), "secrets.json")
    )
    if secrets is None:
        raise ValueError(
            "Could not load secrets.json from the filesystem root - "
            "check the file exists and has valid AUTHORIZATION data"
        )
    # Copy the cached dict: per-request headers (User-Agent, Content-Type) must
    # not leak back into the cached secrets object.
    auth_variables = dict(secrets["AUTHORIZATION"])
    # Send a real User-Agent (urequests sends none by default; Cloudflare WAF can
    # block UA-less automated clients with a 403 HTML page).
    auth_variables["User-Agent"] = "BabyBuddy-Scout/1.0 (Raspberry Pi Pico W)"
    if headers:
        auth_variables.update(headers)
    url = base_url + path + "/"
    # timeout: bounds how long the Pico waits on the response (forwarded to the
    # socket) so a stalled request fails fast + clearly instead of hanging.
    m = method.lower() if method else ("post" if data else "get")

    body = None
    if m == "post":
        body = json.dumps(data)
        auth_variables["Content-Type"] = "application/json"
    if m == "delete":
        request = requests.delete
    elif m == "post":
        request = requests.post
    else:
        request = requests.get

    if timeout is not None:
        response = request(url=url, headers=auth_variables, data=body, timeout=timeout)
    else:
        response = request(url=url, headers=auth_variables, data=body)

    # Always release the socket: urequests does not close it for us, and a
    # long-running Pico (timer poll every 20 s) would otherwise leak sockets.
    try:
        return _parse_json_response(response, url)
    finally:
        try:
            response.close()
        except Exception:
            pass
