"""Platform input adapters for manual waypoint teleoperation."""

from __future__ import annotations

import threading
import queue
import time
import sys

try:
    import hid
    _HID_AVAILABLE = True
except ImportError as exc:
    _HID_AVAILABLE = False
    _HID_ERROR = str(exc)


class GamepadInputReader(threading.Thread):
    """Read left-stick throttle and right-stick steering from a gamepad."""

    def __init__(self, output_queue):
        super().__init__(name="heron-gamepad", daemon=True)
        self.output_queue = output_queue
        self.running = True
        self.status = {"connected": False, "name": None, "error": None}

    def _publish(self, axes):
        """Keep the latest axis state instead of queuing stale movements."""
        try:
            self.output_queue.put_nowait(axes)
        except queue.Full:
            try:
                self.output_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.output_queue.put_nowait(axes)
            except queue.Full:
                pass

    @staticmethod
    def latest_report(gamepad):
        """Discard queued intermediate positions before applying stick input."""
        latest = None
        # Bound each drain so a device continuously producing reports cannot
        # starve input publication or shutdown. All reads are nonblocking.
        for _ in range(256):
            data = gamepad.read(64, timeout=0)
            if not data:
                break
            if len(data) >= 4:
                latest = data
        return latest

    @staticmethod
    def normalize_axis(raw, invert=False):
        """Normalize common 8-bit and signed 16-bit joystick event ranges."""
        if raw < 0 or raw > 255:
            value = raw / (32768 if raw < 0 else 32767)
        else:
            delta = raw - 128
            value = delta / 128.0

        # Scale up slightly to ensure worn thumbsticks can easily reach 100%
        value = value * 1.1
        value = max(-1.0, min(1.0, value))

        if abs(value) < 0.08:
            value = 0.0
        if invert:
            value = -value
        return int(value * 1000)

    def run(self):
        if not _HID_AVAILABLE:
            self.status = {"connected": False, "name": None,
                           "error": f"HID support unavailable: {_HID_ERROR}"}
            self._publish((0, 0, "KEYBOARD"))
            return
        while self.running:
            gamepad = None
            try:
                # Logitech mice and keyboards must never be opened as pads.
                device_info = next((d for d in hid.enumerate()
                                    if d.get("vendor_id") == 0x046d
                                    and (d.get("product_id") == 0xc216 or
                                         (d.get("usage_page") == 1 and
                                          d.get("usage") in (4, 5)))), None)
                if not device_info:
                    self.status = {"connected": False, "name": None, "error": None}
                    self._publish((0, 0, "KEYBOARD"))
                else:
                    gamepad = hid.Device(path=device_info["path"])
                    gamepad.nonblocking = True
                    name = device_info.get("product_string") or "Logitech gamepad"
                    self.status = {"connected": True, "name": name, "error": None}
                    source = f"GAMEPAD · {name}"
                    throttle = yaw = 0
                    while self.running:
                        data = self.latest_report(gamepad)
                        if data is not None:
                            # Dual Action: left Y = throttle, right X = steering.
                            throttle = self.normalize_axis(data[1], invert=True)
                            yaw = self.normalize_axis(data[2])
                        self._publish((throttle, yaw, source))
                        time.sleep(0.01)
            except Exception as exc:
                error = str(exc)
                if self.status.get("error") != error:
                    print(f"[gamepad] {error}", file=sys.stderr, flush=True)
                self.status = {"connected": False, "name": None, "error": error}
                self._publish((0, 0, "KEYBOARD"))
            finally:
                if gamepad is not None:
                    try:
                        gamepad.close()
                    except Exception:
                        pass
            # Retry startup failures and unplugged pads without restarting Heron.
            if self.running:
                time.sleep(1.0)


class ManualControlStreamer(threading.Thread):
    """Keep manual MAVLink output at 20 Hz without depending on UI frame rate."""

    def __init__(self, session, interval=0.05):
        super().__init__(name="heron-manual-stream", daemon=True)
        self.session = session
        self.interval = interval
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            self.session.manual.send_tick()
            self._stop_event.wait(self.interval)
