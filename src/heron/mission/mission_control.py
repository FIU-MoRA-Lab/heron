#!/usr/bin/env python3
"""Qt Quick mission-control entry point."""
import argparse
import os
import sys
from heron.mission.mission_core import MissionController, SatelliteMapTiles
from heron.mission.mission_logic import MissionSession
from heron.utils.network_setup import ensure_gcs_ip, mavlink_connection_string


def main():
    parser = argparse.ArgumentParser(description="Heron mission control")
    parser.add_argument("--mission-file", type=str, help="Initial JSON or CSV mission file")
    parser.add_argument("--no-ip-setup", action="store_true", help="Skip automatic GCS IP network setup")
    args = parser.parse_args()
    if not args.no_ip_setup:
        try:
            print("[Network] Verifying Ethernet connection to Heron companion computer...")
            ensure_gcs_ip()
        except Exception as exc:
            print(f"[Network] IP setup warning: {exc}")
    controller = MissionController(mavlink_connection_string())
    controller.start()
    if args.mission_file:
        controller.load_waypoints_file(args.mission_file)
    headless = (os.environ.get("SDL_VIDEODRIVER") == "dummy"
                or not os.environ.get("DISPLAY") and sys.platform.startswith("linux"))
    if headless:
        print("[UI] No display available. Run heron-controller for terminal manual control.")
        controller.running = False
        return
    try:
        from heron.mission.mission_window import run_qt
        run_qt(MissionSession(controller), SatelliteMapTiles())
    except Exception as exc:
        controller.running = False
        print(f"[UI] Qt Quick window could not start: {exc}")
        raise


if __name__ == "__main__":
    main()
