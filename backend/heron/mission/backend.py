"""Local authenticated bridge for the Tauri desktop. No GUI dependencies.

Stdout contains exactly one readiness JSON record; diagnostics use stderr.
The server binds an ephemeral loopback port and exits when its parent closes
stdin. The GUI lease expires manual inputs if polling stops.
"""
from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import queue
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from heron.mission.controls import GamepadInputReader, ManualControlStreamer
from heron.mission.log_explorer import load_recording
from heron.mission.map_tiles import RasterTiles
from heron.mission.mission_core import MissionController
from heron.mission.mission_logic import MissionSession
from heron.mission.safety_geometry import LakeSafetyGeometry
from heron.utils.network_setup import ensure_gcs_ip, mavlink_connection_string


class DesktopBackend:
    def __init__(self, controller, tiles, *, inputs=True):
        self.controller = controller
        self.session = MissionSession(controller)
        self.tiles = tiles
        self.geometry = LakeSafetyGeometry()
        self.logs = []
        self._commands = threading.Lock()
        self._close_lock = threading.Lock()
        self._closing = False
        self._closed = False
        self._last_ui = time.monotonic()
        self._last_axes = self._last_ui
        self._last_gamepad = 0.0
        self._manual_active = False
        self._stop = threading.Event()
        self._input_queue = queue.Queue(maxsize=1)
        self.gamepad = GamepadInputReader(self._input_queue) if inputs else None
        self.streamer = ManualControlStreamer(self.session)
        self.worker = threading.Thread(target=self._input_loop, daemon=True)
        self.desktop_location = None

    def start(self):
        self.controller.start()
        if self.gamepad:
            self.gamepad.start()
        self.streamer.start()
        self.worker.start()
        threading.Thread(target=self._locate_desktop, daemon=True).start()

    def _locate_desktop(self):
        try:
            response = requests.get("https://ipapi.co/json/", timeout=5)
            response.raise_for_status()
            data = response.json()
            lat, lon = coordinates(data)
            self.desktop_location = {"lat": lat, "lon": lon}
        except (requests.RequestException, ValueError, TypeError, KeyError):
            pass

    def _input_loop(self):
        while not self._stop.wait(0.02):
            with self._commands:
                try:
                    throttle, yaw, source = self._input_queue.get_nowait()
                    self._last_gamepad = time.monotonic()
                    if self._manual_active and self.controller.mode_str == "MANUAL":
                        # Keyboard/pad has priority while held; a centered gamepad
                        # must not erase a held key on every HID report.
                        manual = self.session.manual
                        if source == "KEYBOARD" and manual.input_source.startswith("GAMEPAD"):
                            manual.zero(source="KEYBOARD")
                        if source.startswith("GAMEPAD") and (
                            manual.input_source.startswith("GAMEPAD")
                            or not (manual.throttle or manual.yaw)
                        ):
                            manual.set_axes(throttle, yaw, source)
                except queue.Empty:
                    pass
                self.expire_inputs()

    def expire_inputs(self, now=None):
        now = time.monotonic() if now is None else now
        m = self.session.manual
        ui_expired = now - self._last_ui > 0.75
        axes_expired = (now - self._last_gamepad > 0.4 if m.input_source.startswith("GAMEPAD")
                        else now - self._last_axes > 0.4)
        if ui_expired or axes_expired or self.controller.mode_str != "MANUAL":
            if m.throttle or m.yaw:
                m.zero()
            if ui_expired:
                self._manual_active = False

    def state(self):
        with self._commands:
            self._last_ui = time.monotonic()
            c = self.controller
            waypoints = [dict(wp) for wp in self.session.waypoints_snapshot()]
            messages = c._log_queue[:]
            del c._log_queue[:len(messages)]
            self.logs = (self.logs + messages)[-100:]
            locked = c.is_armed or c.mission_started
            editing_locked = locked or c.is_uploading_mission or c.is_starting_mission
            first, last = (waypoints[0], waypoints[-1]) if waypoints else ({}, {})
            return {
                "connected": c.connected, "armed": c.is_armed, "mode": c.mode_str,
                "lat": c.lat, "lon": c.lon, "alt": c.alt, "heading": c.heading,
                "speed": c.groundspeed, "satellites": c.satellites, "fix": c.fix_type,
                "battery_v": c.battery_v, "battery_pct": c.battery_pct,
                "current_wp": c.current_wp_seq, "waypoints": waypoints,
                "uploaded": c.mission_uploaded, "uploading": c.is_uploading_mission,
                "started": c.mission_started, "starting": c.is_starting_mission,
                "simulating": c.simulating_mission,
                "clear_available": not editing_locked,
                "close_available": not editing_locked and len(waypoints) >= 3 and
                    (first["lat"], first["lon"]) != (last["lat"], last["lon"]),
                "padding_enabled": not (locked or c.mission_uploaded or waypoints),
                "padding": self.geometry.safety_padding_m,
                "lake": self.geometry.lake_geojson(),
                "status": c.last_status_msg, "status_error": c.status_is_error,
                "logs": self.logs, "tile_error": self.tiles.last_error,
                "gamepad": self.gamepad.status if self.gamepad else
                    {"connected": False, "name": None, "error": None},
                "manual": {"throttle": self.session.manual.throttle,
                           "yaw": self.session.manual.yaw,
                           "source": self.session.manual.input_source,
                           "active": self._manual_active},
                "desktop_location": self.desktop_location,
            }

    def command(self, action, data):
        # File decoding must not block state polling or the manual-input lease.
        if action == "load_mcap":
            return load_recording(file_path(data))
        with self._commands:
            if self._closing:
                raise ValueError("Ground station is shutting down")
            c, s = self.controller, self.session
            if action == "manual_axes":
                throttle = number(data.get("throttle"), -1000, 1000)
                yaw = number(data.get("yaw"), -1000, 1000)
                source = data.get("source", "KEYBOARD")
                if source not in {"KEYBOARD", "BUTTON PAD"}:
                    raise ValueError("Unknown manual input source")
                if self._manual_active and c.mode_str == "MANUAL":
                    s.manual.set_axes(int(throttle), int(yaw), source)
                    self._last_axes = time.monotonic()
            elif action == "manual_active":
                if not isinstance(data.get("active"), bool):
                    raise ValueError("active must be a boolean")
                self._manual_active = data["active"] and c.mode_str == "MANUAL"
                if not self._manual_active:
                    s.manual.zero()
            elif action == "stop":
                self._manual_active = False
                s.emergency_stop()
            elif action == "arm":
                s.arm()
            elif action == "disarm":
                self._manual_active = False
                s.manual.zero()
                s.disarm()
            elif action == "manual":
                s.set_manual_mode()
                self._manual_active = c.mode_str == "MANUAL"
            elif action in {"upload", "start"}:
                self._manual_active = False
                s.manual.zero()
                if action == "upload":
                    s.upload_mission()
                else:
                    s.start_mission()
            elif action == "clear":
                if c.is_uploading_mission or c.is_starting_mission:
                    raise ValueError("Wait for mission upload/start to finish before editing")
                s.clear_waypoints()
            elif action == "close_loop":
                if c.is_uploading_mission or c.is_starting_mission:
                    raise ValueError("Wait for mission upload/start to finish before editing")
                c.close_waypoint_loop()
            elif action in {"add_waypoint", "move_waypoint", "remove_waypoint", "load"}:
                if c.is_uploading_mission or c.is_starting_mission:
                    raise ValueError("Wait for mission upload/start to finish before editing")
                if action == "add_waypoint":
                    lat, lon = coordinates(data)
                    c.add_waypoint(lat, lon, number(data.get("alt", 2), -1000, 10000))
                elif action == "load":
                    if not s.load_waypoints(file_path(data)):
                        raise ValueError("Could not load mission file")
                else:
                    index = data.get("index")
                    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(c.waypoints):
                        raise ValueError("Invalid waypoint index")
                    if action == "move_waypoint":
                        c.update_waypoint_pos(index, *coordinates(data))
                    else:
                        c.remove_waypoint(index)
            elif action == "save":
                if not s.save_waypoints(file_path(data)):
                    raise ValueError("Could not save mission file")
            elif action == "padding":
                if c.is_armed or c.mission_started or c.mission_uploaded or c.waypoints:
                    raise ValueError("Lake padding is locked during missions")
                self.geometry.safety_padding_m = number(data.get("meters"), 0, 10)
            elif action == "export_lake":
                path = Path(file_path(data)).with_suffix(".geojson")
                path.write_text(json.dumps(self.geometry.lake_geojson(), indent=2) + "\n", encoding="utf-8")
                c.set_status_msg(f"Exported lake safety GeoJSON: {path}")
            else:
                raise ValueError("Unknown command")
        return {"ok": True}

    def prepare_quit(self):
        """Pause manual input while the native quit confirmation is visible."""
        with self._commands:
            self._manual_active = False
            self.session.manual.zero()
            return {"armed": self.controller.quit_is_armed}

    def request_shutdown(self, confirmed_armed=False):
        """Recheck arming at the shutdown boundary, after the native dialog."""
        with self._commands:
            with self.controller._mav_send_lock:
                if self.controller.quit_is_armed and not confirmed_armed:
                    return False
                self.controller.running = False
            self._closing = True
            self._manual_active = False
            self.session.manual.zero()
            self.controller.simulating_mission = False
            return True

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self.request_shutdown(confirmed_armed=True)
            self._close_resources()

    def _close_resources(self):
        self._stop.set()
        self._manual_active = False
        self.session.manual.zero()
        # Neutral is sent before tearing down the MAVLink transport.
        for _ in range(5):
            self.controller.send_manual_control(0, 0)
            time.sleep(0.02)
        self.streamer.stop()
        if self.gamepad:
            self.gamepad.running = False
        self.controller.running = False
        self.controller.simulating_mission = False
        if self.controller.master:
            try:
                self.controller.master.close()
            except Exception as exc:
                self.controller.log(f"[MAVLink] Transport close failed: {exc}")
        if self.controller.thread.is_alive():
            self.controller.thread.join(timeout=4.5)
        self.controller.logger.close()
        self.tiles.close()


def number(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Expected a finite number")
    if not minimum <= value <= maximum:
        raise ValueError(f"Value must be between {minimum} and {maximum}")
    return float(value)


def coordinates(data):
    return (number(data.get("lat", data.get("latitude")), -85.05112878, 85.05112878),
            number(data.get("lon", data.get("longitude")), -180, 180))


def file_path(data):
    path = data.get("path")
    if not isinstance(path, str) or not path or "\0" in path:
        raise ValueError("Expected a file path")
    return path


def confirm_terminal_quit(armed):
    if not armed:
        return True
    print("Vehicle is armed. Are you sure you want to quit?", file=sys.stderr)
    if not sys.stdin.isatty():
        print("Quit cancelled: an interactive confirmation is required.", file=sys.stderr)
        return False
    try:
        return input("Quit? [y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def make_server(backend, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Tile query strings carry the session token: never log them.

        def send(self, status, body, content_type="application/json"):
            if not isinstance(body, bytes):
                body = json.dumps(body, allow_nan=False).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store" if content_type == "application/json" else "private, max-age=86400")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # MapLibre cancels obsolete tile requests during pan/zoom.

        def authorized(self, query, *, tiles=False):
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if tiles:
                supplied = parse_qs(query).get("token", [""])[0]
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                self.send(401, {"error": "Unauthorized"})
                return False
            return True

        def do_GET(self):
            url = urlsplit(self.path)
            tile = url.path.startswith("/tiles/")
            if not self.authorized(url.query, tiles=tile):
                return
            try:
                if url.path == "/state":
                    self.send(200, backend.state())
                elif tile:
                    parts = url.path.split("/")
                    if len(parts) != 6 or not parts[5].endswith(".png"):
                        raise ValueError("Invalid tile URL")
                    self.send(200, backend.tiles.png(parts[2], int(parts[3]), int(parts[4]),
                                                    int(parts[5][:-4])), "image/png")
                else:
                    self.send(404, {"error": "Not found"})
            except (ValueError, OSError) as exc:
                self.send(400 if isinstance(exc, ValueError) else 503, {"error": str(exc)})

        def do_POST(self):
            if not self.authorized(""):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("Invalid request size")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Expected a JSON object")
                if self.path == "/command":
                    self.send(200, backend.command(data.get("action"), data))
                elif self.path == "/prepare-quit":
                    self.send(200, backend.prepare_quit())
                elif self.path == "/shutdown":
                    if backend.request_shutdown(confirmed_armed=data.get("confirmed_armed") is True):
                        self.send(200, {"ok": True})
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
                    else:
                        self.send(409, {"error": "Vehicle is armed. Quit confirmation required.", "armed": True})
                else:
                    self.send(404, {"error": "Not found"})
            except (ValueError, TypeError, OSError) as exc:
                self.send(400, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="Heron desktop Python backend")
    parser.add_argument("--mission-file")
    parser.add_argument("--connection", default=mavlink_connection_string())
    parser.add_argument("--fiu-root", type=Path, default=Path(__file__).resolve().parents[3] / "FIU-basemap")
    parser.add_argument("--no-ip-setup", action="store_true")
    parser.add_argument("--managed", action="store_true", help="Exit when parent closes stdin")
    args = parser.parse_args()
    protocol_stdout = sys.stdout
    sys.stdout = sys.stderr
    if not args.no_ip_setup:
        ensure_gcs_ip()
    controller = MissionController(args.connection)
    if args.mission_file:
        if not controller.load_waypoints_file(args.mission_file):
            raise ValueError("Could not load initial mission")
    token = secrets.token_urlsafe(32)
    cache = Path(os.environ.get("HERON_CACHE_DIR", Path.home() / ".cache" / "heron_tiles"))
    backend = DesktopBackend(controller, RasterTiles(args.fiu_root, cache))
    server = make_server(backend, token)
    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    terminal_quit_pending = False
    def terminal_shutdown(*_):
        nonlocal terminal_quit_pending
        if terminal_quit_pending:
            return
        terminal_quit_pending = True
        try:
            while True:
                status = backend.prepare_quit()
                if not confirm_terminal_quit(status["armed"]):
                    return
                if backend.request_shutdown(confirmed_armed=status["armed"]):
                    shutdown()
                    return
        finally:
            terminal_quit_pending = False
    # Tauri handles the user's Ctrl+C and keeps the backend alive until the
    # user confirms. Backend stdin EOF remains the unconditional parent-death
    # cleanup path; it cannot depend on a still-running GUI.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_IGN if args.managed else terminal_shutdown)
    if args.managed:
        def parent_watch():
            sys.stdin.buffer.read()
            shutdown()
        threading.Thread(target=parent_watch, daemon=True).start()
    backend.start()
    print(json.dumps({"port": server.server_port, "token": token,
                      "python": sys.executable,
                      "source": str(Path(__file__).resolve().parents[2])}),
          file=protocol_stdout, flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        backend.close()


if __name__ == "__main__":
    main()
