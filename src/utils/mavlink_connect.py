#!/usr/bin/env python3
"""
MAVLink Connection & JSON Parser Utility
-----------------------------------------
Connects to a MAVLink device/vehicle, listens for incoming MAVLink telemetry messages,
parses them into structured JSON format, and gracefully logs them to console or file (JSON / JSONL).

Examples:
    # Print parsed JSON messages to console in real-time
    python src/utils/mavlink_connect.py --format json

    # Save all messages to JSON Lines file (streaming-safe)
    python src/utils/mavlink_connect.py --output telemetry.jsonl

    # Save all messages to a single JSON array file
    python src/utils/mavlink_connect.py --output telemetry.json --format json
"""

import sys
import time
import json
import argparse
from datetime import datetime, timezone
from pymavlink import mavutil

# Auto-configure the frozen GCS static IP (192.168.2.1/24) on the USB Ethernet adapter
try:
    from network_setup import ensure_gcs_ip, mavlink_connection_string, MAVLINK_CONN
except ImportError:
    from src.utils.network_setup import ensure_gcs_ip, mavlink_connection_string, MAVLINK_CONN


def mavlink_to_dict(msg):
    """
    Convert a pymavlink message into a JSON-serializable dictionary.
    
    Args:
        msg: pymavlink message object.
        
    Returns:
        dict: Structured representation of the MAVLink message.
    """
    if msg is None:
        return None

    # Get raw dictionary from pymavlink message
    raw_dict = msg.to_dict()
    
    # Remove redundant header item if present
    raw_dict.pop('mavpackettype', None)

    # Process non-serializable objects (e.g. bytearrays, bytes)
    clean_data = {}
    for key, val in raw_dict.items():
        if isinstance(val, (bytes, bytearray)):
            try:
                clean_data[key] = val.decode('utf-8', errors='replace').rstrip('\x00')
            except Exception:
                clean_data[key] = list(val)
        else:
            clean_data[key] = val

    # Build standardized message dictionary
    timestamp = time.time()
    return {
        "timestamp": timestamp,
        "timestamp_iso": datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(),
        "msg_type": msg.get_type(),
        "sys_id": getattr(msg, '_header', None).srcSystem if hasattr(msg, '_header') else msg.get_srcSystem(),
        "comp_id": getattr(msg, '_header', None).srcComponent if hasattr(msg, '_header') else msg.get_srcComponent(),
        "data": clean_data
    }


class MAVLinkLogger:
    """Handles graceful logging of MAVLink messages to JSON or JSONL format."""
    
    def __init__(self, output_path=None, fmt="jsonl", print_stdout=True, msg_types=None):
        self.output_path = output_path
        self.fmt = fmt.lower()
        self.print_stdout = print_stdout
        self.msg_types = set(m.upper() for m in msg_types) if msg_types else None
        self.file_handle = None
        self.msg_count = 0
        self.first_json_entry = True

        # Infer format from file extension if not explicitly forced
        if output_path and output_path.endswith('.json') and fmt not in ('json', 'jsonl'):
            self.fmt = 'json'

    def open(self):
        if self.output_path:
            self.file_handle = open(self.output_path, 'w', encoding='utf-8')
            if self.fmt == 'json':
                self.file_handle.write("[\n")
            print(f"Logging telemetry data to '{self.output_path}' (format: {self.fmt.upper()})...")

    def log_message(self, msg):
        msg_type = msg.get_type()
        
        # Filter message types if filter specified
        if self.msg_types and msg_type.upper() not in self.msg_types:
            return

        parsed = mavlink_to_dict(msg)
        if not parsed:
            return

        self.msg_count += 1
        json_str = json.dumps(parsed, default=str)

        # Output to console if requested
        if self.print_stdout:
            if self.fmt in ('json', 'jsonl'):
                print(json_str)
            else:
                print(f"[{parsed['msg_type']}] sys={parsed['sys_id']} comp={parsed['comp_id']}: {parsed['data']}")

        # Output to file if open
        if self.file_handle:
            if self.fmt == 'jsonl':
                self.file_handle.write(json_str + "\n")
                self.file_handle.flush()
            elif self.fmt == 'json':
                if not self.first_json_entry:
                    self.file_handle.write(",\n")
                self.file_handle.write("  " + json_str)
                self.first_json_entry = False
                self.file_handle.flush()

    def close(self):
        if self.file_handle:
            if self.fmt == 'json' and not self.file_handle.closed:
                self.file_handle.write("\n]\n")
            self.file_handle.flush()
            self.file_handle.close()
            print(f"\nSaved {self.msg_count} messages gracefully to '{self.output_path}'.")


def connect_and_listen(port, baud_rate, output_path=None, fmt="jsonl", print_stdout=True, msg_types=None):
    print(f"Connecting to {port} at {baud_rate} baud...")

    master = mavutil.mavlink_connection(port, baud=baud_rate)

    print("Waiting for heartbeat...")
    master.wait_heartbeat()
    print(f"Heartbeat from system (system {master.target_system} component {master.target_component})")

    logger = MAVLinkLogger(output_path=output_path, fmt=fmt, print_stdout=print_stdout, msg_types=msg_types)
    logger.open()

    print("Listening for MAVLink messages... (Press Ctrl+C to stop)")
    try:
        while True:
            msg = master.recv_match(blocking=True)
            if not msg:
                continue
            logger.log_message(msg)

    except KeyboardInterrupt:
        print("\nStopping connection...")
    finally:
        logger.close()
        master.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Connect to a MAVLink device, parse messages to JSON, and save logs.")
    parser.add_argument("--port", type=str, default=None,
                        help="MAVLink connection string (default: udpin:0.0.0.0:14550). "
                             "Overrides the auto-detected value.")
    parser.add_argument("--no-ip-setup", action="store_true",
                        help="Skip automatic static IP configuration on the USB Ethernet adapter.")
    parser.add_argument("--baud", type=int, default=57600, help="Baud rate for serial connections")
    parser.add_argument("--output", "-o", type=str, default=None, help="Output file path to save JSON logs (e.g. log.json or log.jsonl)")
    parser.add_argument("--format", "-f", type=str, choices=["jsonl", "json", "raw"], default="jsonl", help="Output format: 'jsonl' (line-by-line streaming) or 'json' (JSON array)")
    parser.add_argument("--quiet", "-q", action="store_true", help="Do not print messages to stdout (useful when saving to file)")
    parser.add_argument("--types", type=str, default=None, help="Comma-separated list of message types to log (default: save all messages)")

    args = parser.parse_args()

    # --- Network setup: assign frozen GCS IP 192.168.2.1/24 ---
    if not args.no_ip_setup:
        ok = ensure_gcs_ip(auto=True)
        if not ok:
            print("[WARNING] Could not configure the static IP automatically. "
                  "MAVLink telemetry may not be received.")
    else:
        print("[network_setup] Skipping IP setup (--no-ip-setup flag set).")

    # Resolve connection string: CLI flag > auto default
    port = args.port if args.port else mavlink_connection_string()

    msg_types_list = args.types.split(",") if args.types else None
    print_stdout = not args.quiet

    connect_and_listen(
        port=port,
        baud_rate=args.baud,
        output_path=args.output,
        fmt=args.format,
        print_stdout=print_stdout,
        msg_types=msg_types_list
    )
