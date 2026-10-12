#!/usr/bin/env python3
"""Launch the Tauri ground station, keeping terminal network setup available."""
import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid

from heron.utils.network_setup import ensure_gcs_ip


def run_desktop(command, *, cwd=None):
    """Keep terminal interrupts from killing Tauri before its quit dialog."""
    with tempfile.TemporaryDirectory(prefix="heron-desktop-") as control_dir:
        request_path = Path(control_dir) / "quit.request"
        environment = {**os.environ, "HERON_QUIT_REQUEST": str(request_path)}
        options = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }
        child = subprocess.Popen(command, cwd=cwd, env=environment, **options)
        previous_handlers = {}

        def request_quit(*_):
            # Rust consumes each unique request once. Cancel leaves both the
            # backend and this launcher alive; a later Ctrl+C asks again.
            request_path.write_text(uuid.uuid4().hex, encoding="utf-8")

        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[sig] = signal.signal(sig, request_quit)
            return child.wait()
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description="Heron Tauri mission control")
    parser.add_argument("--mission-file", help="Initial GeoJSON, JSON, or CSV mission")
    parser.add_argument("--connection", help="Override the MAVLink connection string")
    parser.add_argument("--no-ip-setup", action="store_true", help="Skip terminal network configuration")
    parser.add_argument("--dev", action="store_true", help="Run Tauri/Vite development mode")
    args = parser.parse_args()
    if not args.no_ip_setup:
        try:
            ensure_gcs_ip()
        except Exception as exc:
            print(f"[Network] IP setup warning: {exc}")
    desktop_args = []
    if args.mission_file:
        desktop_args += ["--mission-file", str(Path(args.mission_file).resolve())]
    if args.connection:
        desktop_args += ["--connection", args.connection]
    root = Path(__file__).resolve().parents[3]
    override = os.environ.get("HERON_DESKTOP")
    candidates = [Path(override)] if override else []
    if sys.platform == "darwin":
        candidates += [root / "desktop/src-tauri/target/release/bundle/macos/Heron Ground Station.app/Contents/MacOS/heron-desktop",
                       Path("/Applications/Heron Ground Station.app/Contents/MacOS/heron-desktop")]
    else:
        candidates += [root / "desktop/src-tauri/target/release" / ("heron-desktop.exe" if os.name == "nt" else "heron-desktop")]
        installed = shutil.which("heron-desktop")
        if installed:
            candidates.append(Path(installed))
    executable = next((path for path in candidates if path.is_file()), None)
    if executable and not args.dev:
        raise SystemExit(run_desktop([str(executable), *desktop_args]))
    desktop = root / "desktop"
    npm = shutil.which("npm")
    if not npm or not (desktop / "node_modules").is_dir():
        parser.exit(1, "Install the desktop app, set HERON_DESKTOP, or run `cd desktop && npm install && npm run desktop`.\n")
    raise SystemExit(run_desktop([npm, "run", "desktop", "--", "--", "--", *desktop_args], cwd=desktop))


if __name__ == "__main__":
    main()
