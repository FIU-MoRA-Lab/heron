"""Controller connection recovery without sending vehicle commands."""
import queue
import unittest
from unittest.mock import Mock, patch

from heron.mission import controls


PAD = {"vendor_id": 0x046d, "product_id": 0xc216, "path": b"pad",
       "product_string": "Logitech Dual Action", "usage_page": 1, "usage": 4}
MOUSE = {"vendor_id": 0x046d, "product_id": 1, "path": b"mouse",
         "usage_page": 1, "usage": 2}


class GamepadRecoveryTests(unittest.TestCase):
    def exercise_recovery(self, fail_open=False):
        reader = controls.GamepadInputReader(queue.Queue(maxsize=1))
        device = Mock()
        statuses = []
        def read(*args, **kwargs):
            self.assertEqual(kwargs, {"timeout": 0})
            if not reader.running:
                return b""
            reader.running = False
            return bytes([128, 0, 255, 128])
        device.read.side_effect = read
        api = Mock()
        api.enumerate.side_effect = [[MOUSE, PAD], [MOUSE, PAD]] if fail_open else [[MOUSE], [MOUSE, PAD]]
        api.Device.side_effect = [OSError("temporarily unavailable"), device] if fail_open else [device]
        with patch.object(controls, "_HID_AVAILABLE", True), patch.object(controls, "hid", api, create=True), patch.object(controls.time, "sleep", side_effect=lambda _: statuses.append(reader.status.copy())), patch("sys.stderr"):
            reader.run()
        self.assertEqual(api.enumerate.call_count, 2)
        api.Device.assert_called_with(path=b"pad")
        device.close.assert_called_once()
        self.assertTrue(reader.status["connected"])
        self.assertEqual(reader.status["name"], "Logitech Dual Action")
        self.assertIsNone(reader.status["error"])
        self.assertEqual(reader.output_queue.get_nowait(), (1000, 1000, "GAMEPAD · Logitech Dual Action"))
        self.assertFalse(statuses[0]["connected"])
        if fail_open:
            self.assertEqual(statuses[0]["error"], "temporarily unavailable")

    def test_controller_connected_after_startup(self):
        self.exercise_recovery()

    def test_backlog_applies_release_without_replaying_old_movements(self):
        device = Mock()
        move = bytes([128, 0, 255, 128])
        release = bytes([128, 128, 128, 128])
        device.read.side_effect = [move] * 80 + [release, b""]
        data = controls.GamepadInputReader.latest_report(device)
        self.assertEqual(data, release)
        self.assertEqual(controls.GamepadInputReader.normalize_axis(data[1], invert=True), 0)
        self.assertEqual(controls.GamepadInputReader.normalize_axis(data[2]), 0)
        for call in device.read.call_args_list:
            self.assertEqual(call.kwargs, {"timeout": 0})

    def test_continuous_reports_cannot_starve_publication(self):
        device = Mock()
        device.read.return_value = bytes([128, 128, 128, 128])
        self.assertIsNotNone(controls.GamepadInputReader.latest_report(device))
        self.assertEqual(device.read.call_count, 256)

    def test_recovers_from_open_error(self):
        self.exercise_recovery(fail_open=True)

    def test_unplug_publishes_neutral_keyboard_and_retries(self):
        reader = controls.GamepadInputReader(queue.Queue(maxsize=1))
        api = Mock()
        device = Mock()
        device.read.side_effect = OSError("disconnected")
        api.enumerate.return_value = [PAD]
        api.Device.return_value = device
        def stop(_):
            reader.running = False
        with patch.object(controls, "_HID_AVAILABLE", True), patch.object(controls, "hid", api, create=True), patch.object(controls.time, "sleep", side_effect=stop), patch("sys.stderr"):
            reader.run()
        self.assertEqual(reader.output_queue.get_nowait(), (0, 0, "KEYBOARD"))
        self.assertFalse(reader.status["connected"])
        self.assertEqual(reader.status["error"], "disconnected")
        device.close.assert_called_once()
