#!/usr/bin/env python3
"""
Heron MAVLink Logger
--------------------
Persistent telemetry logger for Heron USV MAVLink sessions.
Automatically used by mission_control and legacy/controller.py on every run —
no flags required.  Each session produces a timestamped MCAP log file in
the user's Documents/Heron/logs directory unless you specify a custom path or
``HERON_LOG_DIR``. Documents is resolved using the operating system's settings.

Standalone usage (listen-only mode):
    uv run heron-logger
    uv run heron-logger --output custom.mcap
    uv run heron-logger --format mcap --types HEARTBEAT,STATUSTEXT
"""

import time
import os
import json
import signal
import argparse
import math
import threading
from array import array
from mcap.writer import CompressionType, Writer
from pathlib import Path
from platformdirs import user_documents_path
from datetime import datetime, timezone
from pymavlink import mavutil

from heron.utils.network_setup import ensure_gcs_ip, mavlink_connection_string

def default_log_directory():
    """Respect redirected Windows folders and Linux XDG user directories."""
    override = os.environ.get("HERON_LOG_DIR")
    if override:
        return Path(override).expanduser()
    return user_documents_path() / "Heron" / "logs"


DEFAULT_LOG_DIR = default_log_directory()


# ---------------------------------------------------------------------------
# Message conversion helper
# ---------------------------------------------------------------------------

def mavlink_to_dict(msg, timestamp_ns=None):
    """
    Convert a pymavlink message into a JSON-serialisable dictionary.

    Args:
        msg: pymavlink message object.

    Returns:
        dict with keys: timestamp, timestamp_iso, msg_type, sys_id, comp_id, data.
    """
    if msg is None:
        return None

    raw_dict = msg.to_dict()
    raw_dict.pop("mavpackettype", None)

    def normalize(value):
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("utf-8", errors="replace").rstrip("\x00")
        if isinstance(value, (list, tuple, array)):
            return [normalize(item) for item in value]
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value

    clean_data = {key: normalize(value) for key, value in raw_dict.items()}
    ts = (timestamp_ns if timestamp_ns is not None else time.time_ns()) / 1_000_000_000
    return {
        "timestamp":     ts,
        "timestamp_iso": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        "msg_type":      msg.get_type(),
        "sys_id":        (
            getattr(msg, "_header", None).srcSystem
            if hasattr(msg, "_header")
            else msg.get_srcSystem()
        ),
        "comp_id":       (
            getattr(msg, "_header", None).srcComponent
            if hasattr(msg, "_header")
            else msg.get_srcComponent()
        ),
        "data":          clean_data,
    }


# ---------------------------------------------------------------------------
# HeronLogger
# ---------------------------------------------------------------------------

class HeronLogger:
    """Write indexed, compressed MCAP telemetry with one channel per MAVLink type.

    JSON-schema channels keep decoded telemetry readable by standard MCAP
    tools. The file itself is a binary MCAP container, not a JSON log.
    """

    def __init__(self, output_path=None, fmt="mcap", print_stdout=False,
                 msg_types=None, auto_path=True, session_label="session"):
        if fmt.lower() != "mcap":
            raise ValueError("Telemetry logs now use MCAP; choose fmt='mcap'")
        self.fmt = "mcap"
        self.print_stdout = print_stdout
        self.msg_types = set(m.upper() for m in msg_types) if msg_types else None
        self.session_label = session_label
        self.msg_count = 0
        self._file = None
        self._writer = None
        self._channels = {}
        self._lock = threading.RLock()
        self._last_flush = 0.0
        if output_path:
            self.output_path = Path(output_path).with_suffix(".mcap")
        elif auto_path:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            self.output_path = DEFAULT_LOG_DIR / f"heron_{session_label}_{stamp}.mcap"
        else:
            self.output_path = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    def open(self):
        with self._lock:
            if self.output_path is None or (self._file and not self._file.closed):
                return
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.output_path.open("wb")
            try:
                self._writer = Writer(self._file, chunk_size=256 * 1024,
                                      compression=CompressionType.ZSTD)
                self._writer.start(library="heron/0.1.0")
                self._writer.add_metadata("heron.session", {
                    "label": self.session_label,
                    "timestamp_clock": "Unix epoch nanoseconds; vehicle message receive time",
                })
                self._channels = {}
                self.msg_count = 0
                self._last_flush = time.monotonic()
                self._file.flush()
            except Exception:
                self._file.close()
                self._writer = None
                raise
            print(f"[Logger] Session log -> {self.output_path}  (format: MCAP)")

    def close(self):
        with self._lock:
            if self._file and not self._file.closed:
                try:
                    self._writer.finish()
                    self._file.flush()
                finally:
                    self._file.close()
                    self._writer = None
                print(f"[Logger] Saved {self.msg_count} messages -> {self.output_path}")

    def log_message(self, msg):
        if msg is None:
            return
        msg_type = msg.get_type()
        if self.msg_types and msg_type.upper() not in self.msg_types:
            return
        timestamp_ns = time.time_ns()
        parsed = mavlink_to_dict(msg, timestamp_ns=timestamp_ns)
        json_str = json.dumps(parsed, allow_nan=False, separators=(",", ":"))
        with self._lock:
            if self._writer:
                channel = self._channels.get(msg_type)
                if channel is None:
                    schema = {
                        "$schema": "http://json-schema.org/draft-07/schema#",
                        "title": f"MAVLink {msg_type}",
                        "type": "object",
                        "properties": {
                            "timestamp": {"type": "number"},
                            "timestamp_iso": {"type": "string"},
                            "msg_type": {"type": "string", "const": msg_type},
                            "sys_id": {"type": "integer"},
                            "comp_id": {"type": "integer"},
                            "data": {"type": "object", "properties": {
                                key: field_schema(value) for key, value in parsed["data"].items()
                            }},
                        },
                        "required": ["timestamp", "timestamp_iso", "msg_type", "sys_id", "comp_id", "data"],
                    }
                    schema_id = self._writer.register_schema(
                        name=f"mavlink.{msg_type}", encoding="jsonschema",
                        data=json.dumps(schema).encode("utf-8"))
                    channel = self._writer.register_channel(
                        topic=f"/mavlink/{msg_type}", message_encoding="json", schema_id=schema_id)
                    self._channels[msg_type] = channel
                self._writer.add_message(channel_id=channel, log_time=timestamp_ns,
                                         publish_time=timestamp_ns, data=json_str.encode("utf-8"),
                                         sequence=self.msg_count & 0xffffffff)
                # Bound crash recovery loss without flushing a compressed chunk
                # for every telemetry packet. Close writes the remaining chunk
                # and the indexes/summary/footer for seeking and playback.
                if time.monotonic() - self._last_flush >= 1.0:
                    self._writer.flush()
                    self._last_flush = time.monotonic()
            self.msg_count += 1
            if self.print_stdout:
                print(json_str)

    def __repr__(self):
        state = "open" if self._file and not self._file.closed else "closed"
        return f"HeronLogger(path={self.output_path}, fmt=mcap, msgs={self.msg_count}, state={state})"


def field_schema(value):
    """Describe message fields without relying on a specific MAVLink dialect."""
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": ["number", "null"]}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        return {"type": "array", "items": field_schema(value[0]) if value else {}}
    # A NaN/unknown sensor field is normalized to null. Its next sample may
    # become valid, so don't constrain the channel to null-only values.
    return {}


# ---------------------------------------------------------------------------
# MAVLinkLogger alias kept for any code that imported the old name
# ---------------------------------------------------------------------------
MAVLinkLogger = HeronLogger


# ---------------------------------------------------------------------------
# Standalone listen-and-log entry point
# ---------------------------------------------------------------------------

def _listen(port, baud, logger):
    print(f"[Logger] Connecting to {port} at {baud} baud...")
    master = mavutil.mavlink_connection(port, baud=baud)
    print("[Logger] Waiting for heartbeat...")
    master.wait_heartbeat()
    print(
        f"[Logger] Heartbeat from system "
        f"(sys={master.target_system} comp={master.target_component})"
    )
    print("[Logger] Listening for MAVLink messages... (Ctrl-C to stop)")

    try:
        while True:
            msg = master.recv_match(blocking=True)
            if msg:
                logger.log_message(msg)
    except KeyboardInterrupt:
        print("\n[Logger] Stopping...")
    finally:
        master.close()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Heron MAVLink logger -- records telemetry to MCAP."
    )
    ap.add_argument("--port",         type=str, default=None,
                    help="MAVLink connection string (auto-detected by default).")
    ap.add_argument("--baud",         type=int, default=57600)
    ap.add_argument("--output", "-o", type=str, default=None,
                    help="Output file path (auto-generated timestamp name by default).")
    ap.add_argument("--format", "-f", type=str,
                    choices=["mcap"], default="mcap")
    ap.add_argument("--types",        type=str, default=None,
                    help="Comma-separated MAVLink message types to log (default: all).")
    ap.add_argument("--stdout", "-v", action="store_true",
                    help="Also print messages to stdout.")
    ap.add_argument("--no-ip-setup",  action="store_true",
                    help="Skip automatic static IP configuration.")
    args = ap.parse_args()

    if not args.no_ip_setup:
        ok = ensure_gcs_ip(auto=True)
        if not ok:
            print("[WARNING] Could not configure the static IP automatically.")
    else:
        print("[network_setup] Skipping IP setup (--no-ip-setup).")

    port      = args.port or mavlink_connection_string()
    msg_types = args.types.split(",") if args.types else None

    log = HeronLogger(
        output_path   = args.output,
        fmt           = args.format,
        print_stdout  = args.stdout,
        msg_types     = msg_types,
        auto_path     = args.output is None,
        session_label = "standalone",
    )

    # Register SIGTERM so `kill <pid>` also closes gracefully
    def _on_signal(sig, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_signal)

    with log:
        _listen(port=port, baud=args.baud, logger=log)


if __name__ == "__main__":
    main()
