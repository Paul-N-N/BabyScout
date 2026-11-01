import network
import time


class PicoConnection:
    def __init__(self):
        self.wlan = self.wireless_client_setup()
        self.nearby_access_points = self.scan_access_points()

    @staticmethod
    def wireless_client_setup():
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        wlan.config(pm = 0xa11140)
        # Let the CYW43 firmware settle (~2 s after a cold power-on) before
        # the first scan/association - joining too soon after power-on is a
        # common cause of cold-boot WiFi failures on the Pico W.
        time.sleep(2)
        return wlan

    def connect_to_access_point(self, ssid, password, timeout=30, warmup=1):
        # Attempt to connect to WIFI
        if ssid not in self.nearby_access_points:
            return False
        # Ensure the radio is active and give it warmup seconds to settle
        # before associating. Associating too soon after (re)activation is a
        # common cause of CYW43_LINK_NONET (-2) on the Pico W / Pico 2 W: the
        # radio "forgets" the SSIDs it saw during the earlier scan. main.py
        # escalates this on each failed round (cold radios need longer).
        self.wlan.active(True)
        time.sleep(warmup)
        self.wlan.connect(ssid, password)
        print(f"Attempting to connect to {ssid}")
        if self._wait_connected(timeout, ssid):
            return True
        # First attempt failed: reset the association and try once more.
        # Deliberately NOT toggling wlan.active(False) here - deactivating the
        # radio can wedge the CYW43 in a bad state that only a full power
        # cycle recovers (the classic "cold boot fails, power cycle fixes it").
        print("Connection failed, resetting association and retrying...")
        self.wlan.disconnect()
        time.sleep(2)
        self.wlan.connect(ssid, password)
        print(f"Retrying connect to {ssid}")
        if self._wait_connected(timeout, ssid):
            return True
        print(f"Failed to connect to {ssid} after retry.")
        return False

    def _wait_connected(self, timeout, ssid):
        start = time.ticks_ms()
        last_status = None
        while not self.wlan.isconnected():
            status = self.wlan.status()
            if time.ticks_diff(time.ticks_ms(), start) > timeout * 1000:
                break
            if status != last_status:
                print(f"status: {status}")
                last_status = status
            time.sleep(0.5)  # 0.5 s poll: joins finish sooner and failures are detected sooner
        if self.wlan.isconnected():
            print("WLAN Connected")
            print(self.wlan.ifconfig())
            return True
        return False

    def scan_access_points(self, verbose=True):
        nearby_access_point_list = []
        nearby_access_points = self.wlan.scan()
        for ssid in nearby_access_points:
            nearby_access_point_list.append(ssid[0].decode("utf-8"))
        # NOTE: do NOT toggle wlan.active(False) here - deactivating the radio
        # before connecting can leave the CYW43 in a bad state on the Pico W.
        while "" in nearby_access_point_list:
            nearby_access_point_list.remove("")
        if verbose:
            print(nearby_access_point_list)
        return nearby_access_point_list
