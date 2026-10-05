#!/usr/bin/env python3
"""
Heron Surface Vehicle - Controller Teleoperation Utility with JSON Logging
-------------------------------------------------------------------------
Reads analog stick & button inputs from a Logitech Gamepad / Joystick via evdev
and streams MAVLink MANUAL_CONTROL / RC_CHANNELS_OVERRIDE commands to the vehicle.
Optionally records all incoming telemetry data and controller commands to a JSON/JSONL file gracefully.

Default connection string: udpin:0.0.0.0:14550
"""

import sys
import time
import argparse
import threading
from pymavlink import mavutil

# Import JSON logging tools from mavlink_connect
try:
    from mavlink_connect import mavlink_to_dict, MAVLinkLogger
except ImportError:
    from src.utils.mavlink_connect import mavlink_to_dict, MAVLinkLogger

try:
    import evdev
    from evdev import ecodes
except ImportError:
    print("Error: 'evdev' module is required. Install it using: uv add evdev")
    sys.exit(1)


def find_joystick(name_filter="Logitech"):
    """Find a connected gamepad or joystick device matching the name_filter."""
    devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
    for dev in devices:
        if name_filter.lower() in dev.name.lower() or "gamepad" in dev.name.lower() or "joystick" in dev.name.lower():
            print(f"Found Gamepad: {dev.name} on {dev.path}")
            return dev
    # Fallback to first available input device if any exists
    if devices:
        print(f"Using default input device: {devices[0].name} on {devices[0].path}")
        return devices[0]
    return None


class HeronControllerTeleop:
    def __init__(self, port, baud, device_path=None, log_file=None, log_fmt="jsonl"):
        self.port = port
        self.baud = baud
        self.device_path = device_path
        self.log_file = log_file
        self.log_fmt = log_fmt
        
        # State variables (normalized -1000 to 1000 for MAVLink)
        self.throttle = 0  # Pitch / Forward-Backward thrust (-1000 to 1000)
        self.steering = 0  # Yaw / Left-Right turn (-1000 to 1000)
        self.is_armed = False
        self.running = True
        
        # Joystick deadzone configuration (0-128 range relative to center 128)
        self.DEADZONE = 15
        self.CENTER = 128
        self.SCALE = 127.0

        # JSON Logger
        self.logger = MAVLinkLogger(output_path=self.log_file, fmt=self.log_fmt, print_stdout=False)

    def connect_mavlink(self):
        print(f"Connecting to MAVLink vehicle on {self.port}...")
        self.master = mavutil.mavlink_connection(self.port, baud=self.baud)
        # Ensure Ground Control Station System ID is 255 (standard GCS ID for ArduPilot RC overrides)
        self.master.source_system = 255
        print("Waiting for heartbeat from vehicle...")
        self.master.wait_heartbeat()
        
        self.target_system = self.master.target_system
        self.target_component = self.master.target_component
        
        print(f"Connected! Target System ID: {self.target_system}, Component ID: {self.target_component}")
        if self.log_file:
            self.logger.open()

    def set_arm_state(self, arm=True):
        state_str = "ARM" if arm else "DISARM"
        print(f"\n[COMMAND] Sending {state_str} command to vehicle (sysid={self.target_system}, compid={self.target_component})...")
        self.master.mav.command_long_send(
            self.target_system,
            self.target_component,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            0,
            1 if arm else 0,
            21196 if arm else 0,
            0, 0, 0, 0, 0
        )
        self.is_armed = arm

    def set_manual_mode(self):
        print("\n[COMMAND] Setting vehicle mode to MANUAL...")
        try:
            mode_id = self.master.mode_mapping().get('MANUAL')
            if mode_id is not None:
                self.master.mav.set_mode_send(
                    self.target_system,
                    mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                    mode_id
                )
        except Exception:
            pass

        self.master.mav.command_long_send(
            self.target_system,
            self.target_component,
            mavutil.mavlink.MAV_CMD_DO_SET_MODE,
            0,
            1,  # MAV_MODE_MANUAL_ARMED / MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
            0, 0, 0, 0, 0, 0
        )

    def normalize_axis(self, value, invert=False):
        """Convert raw 0-255 axis value to -1000 to 1000 range with deadzone."""
        diff = value - self.CENTER
        if abs(diff) < self.DEADZONE:
            return 0
        
        normalized = (diff - (self.DEADZONE if diff > 0 else -self.DEADZONE)) / (self.SCALE - self.DEADZONE)
        normalized = max(-1.0, min(1.0, normalized))
        
        if invert:
            normalized = -normalized
            
        return int(normalized * 1000)

    def read_controller_loop(self, dev):
        """Continuously poll evdev events from the Logitech controller."""
        print(f"Listening for joystick inputs from {dev.name}...")
        try:
            for event in dev.read_loop():
                if not self.running:
                    break
                
                # EV_ABS: Analog Stick Movement
                if event.type == ecodes.EV_ABS:
                    if event.code == ecodes.ABS_Y:
                        self.throttle = self.normalize_axis(event.value, invert=True)
                    elif event.code in (ecodes.ABS_RZ, ecodes.ABS_Z, ecodes.ABS_X):
                        self.steering = self.normalize_axis(event.value, invert=False)

                # EV_KEY: Button Presses
                elif event.type == ecodes.EV_KEY:
                    if event.value == 1:  # Button press down
                        print(f"\n[KEY EVENT] Button code {event.code} pressed.")
                        if event.code in (ecodes.BTN_TRIGGER, ecodes.BTN_A, 288):
                            self.set_arm_state(not self.is_armed)
                        elif event.code in (ecodes.BTN_THUMB, ecodes.BTN_B, 289):
                            self.set_manual_mode()

        except Exception as e:
            print(f"\nController read loop exception: {e}")

    def stream_mavlink_commands(self):
        """Stream MANUAL_CONTROL and RC_CHANNELS_OVERRIDE messages at 20 Hz while logging data."""
        print("Starting MAVLink command stream (20 Hz)... Press Ctrl+C to stop.\n")
        
        last_print = 0
        servo_s1 = 0
        servo_s3 = 0
        
        while self.running:
            try:
                # Process all incoming MAVLink telemetry messages
                while True:
                    msg = self.master.recv_match(blocking=False)
                    if not msg:
                        break

                    # Save all incoming telemetry messages to JSON logger
                    if self.log_file:
                        self.logger.log_message(msg)
                    
                    msg_type = msg.get_type()
                    if msg_type == 'STATUSTEXT':
                        text = getattr(msg, 'text', '')
                        severity = getattr(msg, 'severity', 0)
                        print(f"\n[VEHICLE STATUSTEXT ({severity})] {text}")
                    elif msg_type == 'COMMAND_ACK':
                        cmd = getattr(msg, 'command', 0)
                        res = getattr(msg, 'result', 0)
                        print(f"\n[VEHICLE ACK] Command {cmd} -> Result: {res}")
                    elif msg_type == 'SERVO_OUTPUT_RAW':
                        servo_s1 = getattr(msg, 'servo1_raw', 0)
                        servo_s3 = getattr(msg, 'servo3_raw', 0)
                    elif msg_type == 'HEARTBEAT':
                        base_mode = getattr(msg, 'base_mode', 0)
                        if base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED:
                            self.is_armed = True
                        else:
                            self.is_armed = False

                # Calculate PWM values (1000 to 2000, 1500 neutral)
                pwm_steering = int(1500 + (self.steering * 0.5))
                pwm_throttle = int(1500 + (self.throttle * 0.5))

                # 1. Send MANUAL_CONTROL message
                self.master.mav.manual_control_send(
                    self.target_system,
                    self.throttle,   # x (Forward / Reverse)
                    0,               # y (Lateral)
                    0,               # z (Neutral throttle offset)
                    self.steering,   # r (Yaw / Steering)
                    0                # buttons bitmask
                )

                # 2. Send RC_CHANNELS_OVERRIDE
                self.master.mav.rc_channels_override_send(
                    self.target_system,
                    self.target_component,
                    pwm_steering,  # Chan 1: Steering
                    0,             # Chan 2: Unused
                    pwm_throttle,  # Chan 3: Throttle
                    0, 0, 0, 0, 0  # Chan 4-8: Unused
                )

                # Print live status on screen (updates 10 times per sec)
                now = time.time()
                if now - last_print > 0.1:
                    arm_str = "ARMED" if self.is_armed else "DISARMED"
                    status_line = (
                        f"\r[{arm_str:8s}] "
                        f"Thr: {self.throttle:5d} | Str: {self.steering:5d} | "
                        f"PWM Str: {pwm_steering:4d} Thr: {pwm_throttle:4d} | "
                        f"FCU Servo1: {servo_s1:4d} Servo3: {servo_s3:4d}  "
                    )
                    sys.stdout.write(status_line)
                    sys.stdout.flush()
                    last_print = now

                time.sleep(0.05)  # 20 Hz loop
            except Exception as e:
                print(f"\nMAVLink stream error: {e}")
                time.sleep(0.5)

    def start(self):
        if self.device_path:
            dev = evdev.InputDevice(self.device_path)
        else:
            dev = find_joystick("Logitech")

        if not dev:
            print("Error: No joystick/gamepad detected!")
            sys.exit(1)

        self.connect_mavlink()

        # Start controller listener thread
        t = threading.Thread(target=self.read_controller_loop, args=(dev,), daemon=True)
        t.start()

        try:
            self.stream_mavlink_commands()
        except KeyboardInterrupt:
            print("\nShutting down controller teleoperation...")
            self.running = False
            self.set_arm_state(arm=False)
            if self.log_file:
                self.logger.close()
            self.master.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Teleoperate Heron Surface Vehicle using Logitech Controller with optional JSON telemetry logging.")
    parser.add_argument("--port", type=str, default="udpin:0.0.0.0:14550", help="MAVLink connection string (default: udpin:0.0.0.0:14550)")
    parser.add_argument("--baud", type=int, default=57600, help="Baud rate (for serial links)")
    parser.add_argument("--device", type=str, default=None, help="Input device path (e.g. /dev/input/event21). Auto-detects if omitted.")
    parser.add_argument("--log-file", "-o", type=str, default=None, help="Path to output JSON/JSONL log file (e.g. teleop_log.jsonl)")
    parser.add_argument("--log-format", "-f", type=str, choices=["jsonl", "json"], default="jsonl", help="Log format: 'jsonl' (default, streaming lines) or 'json' (array)")

    args = parser.parse_args()

    teleop = HeronControllerTeleop(
        port=args.port,
        baud=args.baud,
        device_path=args.device,
        log_file=args.log_file,
        log_fmt=args.log_format
    )
    teleop.start()
