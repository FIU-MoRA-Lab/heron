#!/usr/bin/env python3
"""
Heron USV — Textual TUI Teleoperation
======================================
Full-screen terminal UI built with Textual.  Streams MAVLink MANUAL_CONTROL
to the Heron's differential-thrust (skid-steer) drive:

    x  = throttle  (forward / reverse, −1000…+1000)
    r  = yaw       (left / right,      −1000…+1000)

ArduPilot rover firmware converts x/r into left/right thruster PWM automatically.

Keyboard controls (always active inside the TUI):
    ↑ / ↓       Throttle forward / reverse   (each press ±150)
    ← / →       Yaw left / right             (each press ±150)
    SPACE       Emergency stop
    a           ARM vehicle
    d           DISARM vehicle
    m           Set MANUAL mode
    q / Ctrl-C  Quit

Run:
    uv run heron-controller [--keyboard] [--no-ip-setup]
"""

import time
import threading
import argparse
from pymavlink import mavutil

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Footer, Static, RichLog, Label
from rich.text import Text

from heron.utils.logger import HeronLogger
from heron.utils.network_setup import ensure_gcs_ip, mavlink_connection_string

try:
    import inputs as inputs_lib
    _INPUTS_AVAILABLE = True
except ImportError:
    _INPUTS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
KB_STEP      = 150      # MAVLink units added per key press  (out of ±1000)
ARM_COOLDOWN = 0.6      # minimum seconds between arm/disarm commands
STREAM_HZ    = 20       # MAVLink command rate


# ---------------------------------------------------------------------------
# Rich bar helper
# ---------------------------------------------------------------------------

def _rich_bar(value: int, max_val: int = 1000, width: int = 30) -> Text:
    """Centered bidirectional bar — left=negative (red), right=positive (green)."""
    ratio  = max(-1.0, min(1.0, value / max_val))
    half   = width // 2
    filled = int(abs(ratio) * half)

    bar = Text(no_wrap=True)
    if ratio < 0:
        bar.append("▓" * (half - filled), style="#3d3d3d")
        bar.append("█" * filled,          style="bold red")
        bar.append("┃",                   style="bold white")
        bar.append("░" * half,            style="#3d3d3d")
    else:
        bar.append("░" * half,            style="#3d3d3d")
        bar.append("┃",                   style="bold white")
        bar.append("█" * filled,          style="bold green")
        bar.append("▓" * (half - filled), style="#3d3d3d")
    return bar


# ---------------------------------------------------------------------------
# Custom Widgets
# ---------------------------------------------------------------------------

class ArmStatusWidget(Static):
    """Large armed / disarmed indicator."""
    is_armed = reactive(False)
    mode_str = reactive("---")
    conn_str = reactive("Connecting…")

    def render(self) -> Text:
        t = Text(no_wrap=True)
        if self.is_armed:
            t.append("  ●  ARMED    ", style="bold white on #2d6a4f")
        else:
            t.append("  ○  DISARMED ", style="bold white on #6d1a1a")
        t.append("   ", style="")
        t.append("MODE ", style="dim")
        t.append(f"{self.mode_str:<10}", style="bold yellow")
        t.append("   ", style="")
        t.append(self.conn_str, style="dim cyan")
        return t


class AxisBarWidget(Static):
    """Single labeled bidirectional bar."""

    def __init__(self, label: str, **kwargs):
        super().__init__(**kwargs)
        self._label = label
        self._value = 0

    def set_value(self, v: int) -> None:
        self._value = v
        self.refresh()

    def render(self) -> Text:
        t = Text(no_wrap=True)
        t.append(f" {self._label:<11}", style="bold cyan")
        t.append_text(_rich_bar(self._value))
        color = "green" if self._value > 0 else ("red" if self._value < 0 else "dim white")
        t.append(f"  {self._value:+5d}", style=f"bold {color}")
        return t


class FCUWidget(Static):
    """FCU servo readback + PWM outputs."""
    servo1   = reactive(0)
    servo3   = reactive(0)
    throttle = reactive(0)
    yaw      = reactive(0)

    def render(self) -> Text:
        pwm_steer = int(1500 + self.yaw      * 0.5)
        pwm_thr   = int(1500 + self.throttle * 0.5)
        t = Text(no_wrap=True)
        t.append(" FCU  ", style="bold")
        t.append(f"Left thruster (S1): {self.servo1:4d} µs   "
                 f"Right thruster (S3): {self.servo3:4d} µs\n", style="dim")
        t.append(" PWM  ", style="bold")
        t.append(f"Steer override: {pwm_steer:4d} µs   "
                 f"Thr override:   {pwm_thr:4d} µs", style="dim")
        return t


# ---------------------------------------------------------------------------
# Textual App
# ---------------------------------------------------------------------------

class HeronTUIApp(App):

    TITLE   = "Heron USV Teleoperation"
    CSS_PATH = None   # inline CSS below

    CSS = """
    Screen {
        background: #0d1117;
        padding: 0;
    }

    /* ── Title banner ── */
    #title {
        background: #161b22;
        color: #58a6ff;
        text-align: center;
        content-align: center middle;
        height: 1;
        text-style: bold;
        margin: 0;
    }

    /* ── Status row ── */
    #status-panel {
        background: #161b22;
        border: tall #30363d;
        height: 3;
        padding: 0 1;
        margin: 0;
        content-align: left middle;
    }

    ArmStatusWidget {
        height: 1;
        margin: 0;
    }

    /* ── Axis bars ── */
    #bars-panel {
        background: #161b22;
        border: tall #30363d;
        height: 4;
        padding: 0 1;
        margin: 0;
    }

    AxisBarWidget {
        height: 1;
        margin: 0;
    }

    /* ── FCU panel ── */
    #fcu-panel {
        background: #161b22;
        border: tall #30363d;
        height: 4;
        padding: 0 1;
        margin: 0;
    }

    FCUWidget {
        height: 2;
        margin: 0;
    }

    /* ── Message log ── */
    #log-panel {
        background: #161b22;
        border: tall #30363d;
        height: 1fr;
        margin: 0;
        padding: 0 1;
    }

    #log-label {
        color: #8b949e;
        text-style: bold;
        height: 1;
    }

    RichLog {
        background: #0d1117;
        height: 1fr;
    }

    Footer {
        background: #161b22;
        color: #8b949e;
    }

    Footer > .footer--key {
        background: #1f6feb;
        color: white;
    }
    """

    BINDINGS = [
        Binding("up",    "throttle_up",   "Thr ↑",    priority=True),
        Binding("down",  "throttle_down", "Thr ↓",    priority=True),
        Binding("left",  "yaw_left",      "Yaw ←",    priority=True),
        Binding("right", "yaw_right",     "Yaw →",    priority=True),
        Binding("space", "stop",          "STOP",     priority=True),
        Binding("a",     "arm",           "ARM",      priority=True),
        Binding("d",     "disarm",        "DISARM",   priority=True),
        Binding("m",     "manual_mode",   "MANUAL",   priority=True),
        Binding("q",     "quit",          "Quit"),
    ]

    def __init__(self, teleop: "HeronTeleop", **kwargs):
        super().__init__(**kwargs)
        self.teleop = teleop

    def compose(self) -> ComposeResult:
        yield Static("HERON USV  ·  MAVLink Teleoperation", id="title")

        with Container(id="status-panel"):
            yield ArmStatusWidget(id="arm-status")

        with Container(id="bars-panel"):
            yield AxisBarWidget("THROTTLE", id="bar-thr")
            yield AxisBarWidget("YAW",      id="bar-yaw")

        with Container(id="fcu-panel"):
            yield FCUWidget(id="fcu")

        with Container(id="log-panel"):
            yield Label("▸ Vehicle Messages", id="log-label")
            yield RichLog(id="veh-log", markup=True, highlight=True, max_lines=50)

        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(1 / 10, self._poll_state)   # UI refresh at 10 Hz

    def _poll_state(self) -> None:
        t = self.teleop

        # Arm status + mode
        arm = self.query_one("#arm-status", ArmStatusWidget)
        arm.is_armed = t.is_armed
        arm.mode_str = t.mode_str
        arm.conn_str = t.conn_str

        # Bars
        self.query_one("#bar-thr", AxisBarWidget).set_value(t.throttle)
        self.query_one("#bar-yaw", AxisBarWidget).set_value(t.yaw)

        # FCU readback
        fcu = self.query_one("#fcu", FCUWidget)
        fcu.servo1   = t.servo1_raw
        fcu.servo3   = t.servo3_raw
        fcu.throttle = t.throttle
        fcu.yaw      = t.yaw

        # Flush pending log messages
        log = self.query_one("#veh-log", RichLog)
        while t._log_queue:
            log.write(t._log_queue.pop(0))

    # Actions ---------------------------------------------------------------

    @staticmethod
    def _step_axis(value: int, delta: int) -> int:
        """Step an axis, snapping to zero instead of skipping past it."""
        next_value = max(-1000, min(1000, value + delta))
        if value and next_value and (value < 0) != (next_value < 0):
            return 0
        return next_value

    def action_throttle_up(self)   -> None: self.teleop.throttle = self._step_axis(self.teleop.throttle, KB_STEP)
    def action_throttle_down(self) -> None: self.teleop.throttle = self._step_axis(self.teleop.throttle, -KB_STEP)
    def action_yaw_left(self)      -> None: self.teleop.yaw      = self._step_axis(self.teleop.yaw, -KB_STEP)
    def action_yaw_right(self)     -> None: self.teleop.yaw      = self._step_axis(self.teleop.yaw, KB_STEP)

    def action_stop(self) -> None:
        self.teleop.emergency_stop()
        self.teleop._enqueue_log("[bold yellow][STOP] EMERGENCY STOP — throttle & yaw zeroed[/]")

    def action_arm(self)        -> None: self.teleop.arm()
    def action_disarm(self)     -> None: self.teleop.disarm()
    def action_manual_mode(self)-> None: self.teleop.set_manual_mode()

    def action_quit(self) -> None:
        self.teleop.running = False
        self.exit()


# ---------------------------------------------------------------------------
# MAVLink state + background thread
# ---------------------------------------------------------------------------

class HeronTeleop:
    """
    Manages MAVLink connection and vehicle state.
    Runs the 20 Hz command loop in a background thread.
    The Textual app reads state and calls command methods directly.
    """

    def __init__(self, port: str, baud: int, log_file=None, log_fmt="jsonl"):
        self.port     = port
        self.baud     = baud

        # Shared state (read by UI, written by background thread + UI actions)
        self.throttle   = 0
        self.yaw        = 0
        self.is_armed   = False
        self.mode_str   = "---"
        self.conn_str   = "Not connected"
        self.servo1_raw = 0
        self.servo3_raw = 0
        self.running    = True

        self._log_queue: list[str] = []
        self._last_arm_cmd = 0.0

        # Logger is always-on: auto-generates a timestamped log in ~/heron_logs/
        # unless the caller provides an explicit output path.
        self.logger = HeronLogger(
            output_path   = log_file,
            fmt           = log_fmt,
            print_stdout  = False,
            auto_path     = log_file is None,
            session_label = "controller",
        )

    def _enqueue_log(self, msg: str) -> None:
        self._log_queue.append(msg)

    # Vehicle commands ------------------------------------------------------

    def arm(self) -> None:
        if time.time() - self._last_arm_cmd < ARM_COOLDOWN:
            return
        self._last_arm_cmd = time.time()
        self._enqueue_log("[bold green]→ ARM command sent[/]")
        self.master.mav.command_long_send(
            self.target_system, self.target_component,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            0, 1, 21196, 0, 0, 0, 0, 0
        )

    def disarm(self) -> None:
        if time.time() - self._last_arm_cmd < ARM_COOLDOWN:
            return
        self._last_arm_cmd = time.time()
        self.throttle = 0
        self.yaw      = 0
        self._enqueue_log("[bold red]→ DISARM command sent[/]")
        self.master.mav.command_long_send(
            self.target_system, self.target_component,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            0, 0, 0, 0, 0, 0, 0, 0
        )

    def set_manual_mode(self) -> None:
        try:
            mode_id = self.master.mode_mapping().get("MANUAL")
            if mode_id is not None:
                self.master.mav.set_mode_send(
                    self.target_system,
                    mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                    mode_id
                )
        except Exception:
            pass
        self.master.mav.command_long_send(
            self.target_system, self.target_component,
            mavutil.mavlink.MAV_CMD_DO_SET_MODE,
            0, 1, 0, 0, 0, 0, 0, 0
        )

    def emergency_stop(self) -> None:
        self.throttle = 0
        self.yaw      = 0

    # Background thread -----------------------------------------------------

    def run_in_thread(self) -> None:
        """Connect and stream commands. Runs in a daemon thread."""
        # Open logger at thread start (always-on)
        self.logger.open()
        try:
            self.conn_str = f"Connecting to {self.port}…"
            self.master   = mavutil.mavlink_connection(self.port, baud=self.baud)
            self.master.source_system = 255
            self.conn_str = "Waiting for heartbeat…"
            self.master.wait_heartbeat()
            self.target_system    = self.master.target_system
            self.target_component = self.master.target_component
            self.conn_str = f"Connected · SYS {self.target_system}"
            self._enqueue_log(f"[bold cyan][OK] Heartbeat from system {self.target_system}[/]")

            interval = 1.0 / STREAM_HZ
            while self.running:
                t_start = time.time()

                # Drain incoming MAVLink messages
                while True:
                    msg = self.master.recv_match(blocking=False)
                    if not msg:
                        break
                    self.logger.log_message(msg)
                    mt = msg.get_type()
                    if mt == "SERVO_OUTPUT_RAW":
                        self.servo1_raw = getattr(msg, "servo1_raw", 0)
                        self.servo3_raw = getattr(msg, "servo3_raw", 0)
                    elif mt == "HEARTBEAT":
                        self.is_armed = bool(
                            getattr(msg, "base_mode", 0)
                            & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
                        )
                        try:
                            mapping = {v: k for k, v in self.master.mode_mapping().items()}
                            self.mode_str = mapping.get(getattr(msg, "custom_mode", 0), "?")
                        except Exception:
                            pass
                    elif mt == "STATUSTEXT":
                        text = getattr(msg, "text", "").strip()
                        sev  = getattr(msg, "severity", 6)
                        color = "red" if sev <= 3 else ("yellow" if sev <= 4 else "white")
                        self._enqueue_log(f"[{color}][VEH] {text}[/]")
                    elif mt == "COMMAND_ACK":
                        cmd = getattr(msg, "command", 0)
                        res = getattr(msg, "result", -1)
                        style = "green" if res == 0 else "red"
                        self._enqueue_log(f"[{style}][ACK] cmd={cmd} result={res}[/]")

                # Send MANUAL_CONTROL (Heron differential thrust)
                self.master.mav.manual_control_send(
                    self.target_system,
                    self.throttle,   # x: forward / reverse
                    0,               # y: lateral (unused)
                    0,               # z: vertical (unused)
                    self.yaw,        # r: yaw / differential
                    0
                )

                # Also send RC_CHANNELS_OVERRIDE
                pwm_steer = int(1500 + self.yaw      * 0.5)
                pwm_thr   = int(1500 + self.throttle * 0.5)
                self.master.mav.rc_channels_override_send(
                    self.target_system, self.target_component,
                    pwm_steer, 0, pwm_thr, 0, 0, 0, 0, 0
                )

                elapsed = time.time() - t_start
                time.sleep(max(0, interval - elapsed))

        except Exception as exc:
            self._enqueue_log(f"[bold red][ERROR] MAVLink error: {exc}[/]")
            self.conn_str = f"Error: {exc}"
        finally:
            # Always close logger gracefully on thread exit
            self.logger.close()
            try:
                self.master.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Gamepad support (optional, background thread)
# ---------------------------------------------------------------------------

AXIS_MID  = 128
AXIS_MAX  = 255
DEADZONE  = 15


def _normalize_axis(value: int, invert: bool = False) -> int:
    scale = AXIS_MAX - AXIS_MID
    diff  = value - AXIS_MID
    if abs(diff) < DEADZONE:
        return 0
    n = (diff - (DEADZONE if diff > 0 else -DEADZONE)) / (scale - DEADZONE)
    n = max(-1.0, min(1.0, n))
    return int((-n if invert else n) * 1000)


def find_gamepad(name_filter: str = "Logitech"):
    if not _INPUTS_AVAILABLE:
        return None
    pads = inputs_lib.devices.gamepads
    if not pads:
        return None
    for gp in pads:
        n = gp.name.lower()
        if name_filter.lower() in n or "gamepad" in n or "joystick" in n:
            return gp
    return pads[0]


def gamepad_thread(teleop: HeronTeleop, gp) -> None:
    AXIS_MAP   = {"ABS_Y": ("throttle", True), "ABS_X": ("yaw", False), "ABS_RZ": ("yaw", False)}
    ARM_CODES  = {"BTN_SOUTH", "BTN_A", "BTN_TRIGGER"}
    DARM_CODES = {"BTN_NORTH", "BTN_X"}
    MODE_CODES = {"BTN_EAST",  "BTN_B"}
    try:
        while teleop.running:
            for event in gp.read():
                if event.ev_type == "Absolute" and event.code in AXIS_MAP:
                    attr, inv = AXIS_MAP[event.code]
                    setattr(teleop, attr, _normalize_axis(event.state, inv))
                elif event.ev_type == "Key" and event.state == 1:
                    if event.code in ARM_CODES:   teleop.arm()
                    elif event.code in DARM_CODES: teleop.disarm()
                    elif event.code in MODE_CODES: teleop.set_manual_mode()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Heron USV Textual TUI teleoperation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--port",        type=str, default=None)
    parser.add_argument("--baud",        type=int, default=57600)
    parser.add_argument("--keyboard",    action="store_true",
                        help="Ignore gamepad even if connected.")
    parser.add_argument("--log-file",    "-o", type=str, default=None)
    parser.add_argument("--log-format",  "-f", type=str,
                        choices=["jsonl", "json"], default="jsonl")
    parser.add_argument("--no-ip-setup", action="store_true")

    args = parser.parse_args()

    # --- Network setup ---
    if not args.no_ip_setup:
        ok = ensure_gcs_ip(auto=True)
        if not ok:
            print("[WARNING] Could not configure static IP automatically.")
    else:
        print("[network_setup] Skipping IP setup.")

    port = args.port or mavlink_connection_string()

    # --- Build teleop state machine ---
    teleop = HeronTeleop(port=port, baud=args.baud,
                         log_file=args.log_file, log_fmt=args.log_format)

    # --- Start MAVLink background thread ---
    mav_thread = threading.Thread(target=teleop.run_in_thread, daemon=True)
    mav_thread.start()

    # --- Optional gamepad thread ---
    if not args.keyboard:
        gp = find_gamepad()
        if gp:
            print(f"[input] Gamepad detected: {gp.name}")
            gp_thread = threading.Thread(target=gamepad_thread, args=(teleop, gp), daemon=True)
            gp_thread.start()

    # --- Run Textual TUI (blocks until quit) ---
    app = HeronTUIApp(teleop=teleop)
    app.run()

    # --- Cleanup after TUI exits ---
    teleop.running = False
    teleop.emergency_stop()
    print("Shutdown complete.")


if __name__ == "__main__":
    main()
