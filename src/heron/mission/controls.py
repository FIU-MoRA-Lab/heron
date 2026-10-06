"""Platform input adapters for manual waypoint teleoperation."""

from __future__ import annotations

import threading
import queue

try:
    from inputs import devices as input_devices
except ImportError:
    input_devices = None


class GamepadInputReader(threading.Thread):
    """Read a gamepad's left stick and publish normalized throttle/yaw axes."""

    def __init__(self, output_queue):
        super().__init__(name="heron-gamepad", daemon=True)
        self.output_queue = output_queue
        self.running = True

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
    def normalize_axis(raw, invert=False):
        """Normalize common 8-bit and signed 16-bit joystick event ranges."""
        if raw < 0 or raw > 255:
            value = raw / (32768 if raw < 0 else 32767)
        else:
            delta = raw - 128
            value = delta / (128 if delta < 0 else 127)
        value = max(-1.0, min(1.0, value))
        if abs(value) < 0.08:
            value = 0.0
        if invert:
            value = -value
        return int(value * 1000)

    def run(self):
        if input_devices is None:
            return
        try:
            pads = input_devices.gamepads
            if not pads:
                return
            gamepad = pads[0]
            self._publish((0, 0, f"GAMEPAD · {gamepad.name}"))
            axes = {"throttle": 0, "yaw": 0}
            while self.running:
                try:
                    events = gamepad.read()
                except Exception:
                    self._publish((0, 0, "KEYBOARD"))
                    return
                for event in events:
                    if event.ev_type != "Absolute":
                        continue
                    if event.code == "ABS_Y":
                        axes["throttle"] = self.normalize_axis(event.state, invert=True)
                    elif event.code == "ABS_X":
                        axes["yaw"] = self.normalize_axis(event.state)
                    else:
                        continue
                    self._publish((axes["throttle"], axes["yaw"], "GAMEPAD"))
        except Exception:
            return


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
