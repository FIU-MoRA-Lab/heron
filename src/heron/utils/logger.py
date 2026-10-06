#!/usr/bin/env python3
"""
Heron MAVLink Logger
--------------------
Persistent telemetry logger for Heron USV MAVLink sessions.
Automatically used by mission_control and legacy/controller.py on every run —
no flags required.  Each session produces a timestamped JSONL log file in
``~/heron_logs/`` unless you specify a custom path.

Standalone usage (listen-only mode):
    uv run heron-logger
    uv run heron-logger --output custom.jsonl
    uv run heron-logger --format json --types HEARTBEAT,STATUSTEXT
"""

import time
import json
import signal
import argparse
from pathlib import Path
from datetime import datetime, timezone
from pymavlink import mavutil

from heron.utils.network_setup import ensure_gcs_ip, mavlink_connection_string

# Default directory for auto-generated session logs
DEFAULT_LOG_DIR = Path.home() / "heron_logs"


# ---------------------------------------------------------------------------
# Message conversion helper
# ---------------------------------------------------------------------------

def mavlink_to_dict(msg):
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

    clean_data = {}
    for key, val in raw_dict.items():
        if isinstance(val, (bytes, bytearray)):
            try:
                clean_data[key] = val.decode("utf-8", errors="replace").rstrip("\x00")
            except Exception:
                clean_data[key] = list(val)
        else:
            clean_data[key] = val

    ts = time.time()
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
    """
    Persistent MAVLink telemetry logger.

    Supports both explicit and auto-generated (timestamped) output paths.
    Works as a context manager for guaranteed graceful shutdown::

        with HeronLogger(session_label="controller") as log:
            log.log_message(msg)

    Or open/close manually::

        log = HeronLogger(session_label="waypoints")
        log.open()
        ...
        log.close()
    """

    def __init__(
        self,
        output_path=None,
        fmt="jsonl",
        print_stdout=False,
        msg_types=None,
        auto_path=True,
        session_label="session",
    ):
        """
        Args:
            output_path:   Explicit file path for log output.  When None and
                           auto_path is True a timestamped file is created
                           inside ~/heron_logs/.
            fmt:           'jsonl' (default, streaming-safe) or 'json'
                           (single JSON array).  Inferred from file extension
                           when possible.
            print_stdout:  Echo every message to stdout as well (default False).
            msg_types:     If given, only log messages whose type is in this list.
            auto_path:     Generate a timestamped path automatically when
                           output_path is None.
            session_label: Short label embedded in the auto-generated filename
                           (e.g. 'controller' or 'waypoints').
        """
        self.print_stdout = print_stdout
        self.msg_types    = set(m.upper() for m in msg_types) if msg_types else None
        self.msg_count    = 0
        self._file        = None
        self._first_entry = True

        # Resolve output path
        if output_path:
            self.output_path = Path(output_path)
            # Infer format from extension if not forced
            if self.output_path.suffix == ".json":
                fmt = "json"
            elif self.output_path.suffix == ".jsonl":
                fmt = "jsonl"
        elif auto_path:
            DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            ext   = "json" if fmt == "json" else "jsonl"
            self.output_path = DEFAULT_LOG_DIR / f"heron_{session_label}_{stamp}.{ext}"
        else:
            self.output_path = None

        self.fmt = fmt.lower()

    # ------------------------------------------------------------------
    # Context-manager interface
    # ------------------------------------------------------------------

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self):
        """Open the log file (creates parent directories if needed)."""
        if self.output_path is None:
            return
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.output_path, "w", encoding="utf-8")
        if self.fmt == "json":
            self._file.write("[\n")
        print(f"[Logger] Session log -> {self.output_path}  (format: {self.fmt.upper()})")

    def close(self):
        """Flush and close the log file gracefully."""
        if self._file and not self._file.closed:
            if self.fmt == "json":
                self._file.write("\n]\n")
            self._file.flush()
            self._file.close()
            print(f"[Logger] Saved {self.msg_count} messages -> {self.output_path}")

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def log_message(self, msg):
        """Parse and record a single pymavlink message."""
        msg_type = msg.get_type()

        if self.msg_types and msg_type.upper() not in self.msg_types:
            return

        parsed = mavlink_to_dict(msg)
        if not parsed:
            return

        self.msg_count += 1
        json_str = json.dumps(parsed, default=str)

        if self.print_stdout:
            print(json_str)

        if self._file:
            if self.fmt == "jsonl":
                self._file.write(json_str + "\n")
                self._file.flush()
            elif self.fmt == "json":
                if not self._first_entry:
                    self._file.write(",\n")
                self._file.write("  " + json_str)
                self._first_entry = False
                self._file.flush()

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self):
        state = "open" if (self._file and not self._file.closed) else "closed"
        return (
            f"HeronLogger(path={self.output_path}, fmt={self.fmt}, "
            f"msgs={self.msg_count}, state={state})"
        )


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
        description="Heron MAVLink logger -- streams telemetry to JSONL/JSON."
    )
    ap.add_argument("--port",         type=str, default=None,
                    help="MAVLink connection string (auto-detected by default).")
    ap.add_argument("--baud",         type=int, default=57600)
    ap.add_argument("--output", "-o", type=str, default=None,
                    help="Output file path (auto-generated timestamp name by default).")
    ap.add_argument("--format", "-f", type=str,
                    choices=["jsonl", "json"], default="jsonl")
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
