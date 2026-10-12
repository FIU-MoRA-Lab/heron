"""Quit confirmation and terminal process isolation without a real vehicle."""
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from heron.mission.backend import confirm_terminal_quit
from heron.mission.legacy.controller import HeronTeleop, HeronTUIApp, QuitConfirmation


class TerminalQuitTests(unittest.TestCase):
    def test_confirmation_defaults_to_cancel_and_interrupt_cancels(self):
        with patch('sys.stdin.isatty', return_value=True), patch('sys.stderr'):
            for answer, expected in [('', False), ('n', False), ('yes', True), ('Y', True)]:
                with patch('builtins.input', return_value=answer):
                    self.assertEqual(confirm_terminal_quit(True), expected)
            with patch('builtins.input', side_effect=KeyboardInterrupt):
                self.assertFalse(confirm_terminal_quit(True))
        with patch('sys.stdin.isatty', return_value=False), patch('sys.stderr'):
            self.assertFalse(confirm_terminal_quit(True))
        self.assertTrue(confirm_terminal_quit(False))

    @unittest.skipIf(os.name == 'nt', 'Unix process-group isolation')
    def test_launcher_ctrl_c_can_cancel_then_request_again(self):
        self.check_launcher([sys.executable])

    @unittest.skipIf(os.name == 'nt' or not shutil.which('uv'), 'Requires Unix and uv')
    def test_uv_launcher_ctrl_c_can_cancel_then_request_again(self):
        self.check_launcher([shutil.which('uv'), 'run', '--no-sync', 'python'])

    def check_launcher(self, launcher):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = root / 'desktop.py'
            fixture.write_text('''import os, pathlib, signal, time
root = pathlib.Path(os.environ['QUIT_TEST_ROOT'])
request = pathlib.Path(os.environ['HERON_QUIT_REQUEST'])
signal.signal(signal.SIGINT, lambda *_: exit(99))
(root / 'ready').touch()
last = ''
count = 0
while True:
    if request.exists():
        value = request.read_text()
        if value and value != last:
            last = value
            count += 1
            (root / f'request-{count}').touch()
            if count == 2:
                time.sleep(.25)  # Model the app's graceful resource cleanup.
                break
    time.sleep(.02)
''')
            process = subprocess.Popen(
                [*launcher, '-c', 'from heron.mission.mission_control import run_desktop; '
                 'import sys; sys.exit(run_desktop([sys.executable, sys.argv[1]]))', str(fixture)],
                env={**os.environ, 'QUIT_TEST_ROOT': temp}, start_new_session=True)

            def wait_for(name):
                deadline = time.monotonic() + 5
                while not (root / name).exists() and time.monotonic() < deadline:
                    self.assertIsNone(process.poll())
                    time.sleep(.02)
                self.assertTrue((root / name).exists(), name)

            try:
                wait_for('ready')
                os.killpg(process.pid, signal.SIGINT)
                wait_for('request-1')
                self.assertIsNone(process.poll(), 'Cancel must leave the launcher running')
                os.killpg(process.pid, signal.SIGINT)
                wait_for('request-2')
                self.assertEqual(process.wait(timeout=5), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


class TUIQuitTests(unittest.IsolatedAsyncioTestCase):
    async def test_armed_ctrl_c_cancel_repeat_and_confirm(self):
        with patch('heron.mission.legacy.controller.HeronLogger'):
            teleop = HeronTeleop('test', 57600)
        teleop.is_armed = True
        teleop.throttle = 800
        app = HeronTUIApp(teleop)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press('ctrl+c')
            self.assertIsInstance(app.screen, QuitConfirmation)
            self.assertTrue(teleop.running)
            self.assertEqual(teleop.throttle, 0)
            await pilot.press('ctrl+c')
            self.assertIsInstance(app.screen, QuitConfirmation)
            await pilot.click('#cancel-quit')
            self.assertTrue(teleop.running)
            self.assertFalse(teleop.confirming_quit)
            await pilot.press('q')
            await pilot.click('#confirm-quit')
            self.assertFalse(teleop.running)

    async def test_disarmed_ctrl_c_exits(self):
        with patch('heron.mission.legacy.controller.HeronLogger'):
            teleop = HeronTeleop('test', 57600)
        app = HeronTUIApp(teleop)
        async with app.run_test() as pilot:
            await pilot.press('ctrl+c')
            self.assertFalse(teleop.running)
