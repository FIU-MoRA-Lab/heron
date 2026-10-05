#!/usr/bin/env python3
"""
Heron USV — Satellite Feed Teleoperation & Waypoint Navigation
===============================================================
Interactive Ground Control App for the Heron Autonomous Surface Vehicle (USV).

Features:
- Non-blocking async tile downloading engine (60 FPS smooth rendering).
- Asynchronous MAVLink mission upload worker (Zero UI freezing/lag).
- Thread-safe MAVLink mission request queue (Fixes upload timeouts).
- Interactive mouse & touchpad controls (Google Maps cursor zoom, drag, touchpad shortcuts).
- Vector GUI icons for Start Mission, Emergency Stop, Arm, Load, Save, Clear, Re-Center.
- Clean terminal logging with standard bracketed tags (No emojis).

Usage:
    uv run src/mission/waypoint_teleop.py [--waypoints file.json|csv] [--no-ip-setup] [--tui]
"""

import os
import sys
import time
import math
import json
import csv
import queue
import argparse
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont
import pygame

from pymavlink import mavutil

# Ensure src/ is on sys.path so internal modules resolve when running via
# `uv run src/mission/waypoint_teleop.py` from the project root.
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Textual TUI imports
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Footer, Static, RichLog, Label, DataTable
from rich.text import Text

# Import Heron utilities
from utils.logger import HeronLogger, mavlink_to_dict
from utils.network_setup import ensure_gcs_ip, mavlink_connection_string


# ---------------------------------------------------------------------------
# Constants & Defaults
# ---------------------------------------------------------------------------
DEFAULT_LAT = 25.7617       # Default lat (FIU / Miami area if no GPS fix yet)
DEFAULT_LON = -80.1918      # Default lon
DEFAULT_ZOOM = 17           # Detailed satellite zoom level (1-19)
TILE_SIZE = 256             # Standard Web Mercator tile size (pixels)

CACHE_DIR = Path.home() / ".cache" / "heron_tiles"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

ESRI_TILE_URL = "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
USER_AGENT = "HeronUSV-GCS/1.0"


# ---------------------------------------------------------------------------
# High-Resolution Anti-Aliased Font Renderer
# ---------------------------------------------------------------------------

class FontRenderer:
    """Universal high-resolution anti-aliased text renderer using TrueType fonts."""

    def __init__(self):
        self.pil_fonts = {}
        self.font_paths = [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
            "/System/Library/Fonts/SFNS.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ]
        self._best_font_path = None
        for p in self.font_paths:
            if os.path.exists(p):
                self._best_font_path = p
                break

    def get_font(self, size: int):
        if size not in self.pil_fonts:
            if self._best_font_path:
                try:
                    self.pil_fonts[size] = ImageFont.truetype(self._best_font_path, size)
                except Exception:
                    self.pil_fonts[size] = ImageFont.load_default()
            else:
                self.pil_fonts[size] = ImageFont.load_default()
        return self.pil_fonts[size]

    def render(self, text: str, size: int = 14, color: tuple = (255, 255, 255), bold: bool = False) -> pygame.Surface:
        try:
            font = self.get_font(size)
            bbox = font.getbbox(text)
            w = max(bbox[2] - bbox[0] + 8, 12)
            h = max(bbox[3] - bbox[1] + 8, 14)
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.text((2, 2), text, font=font, fill=color)
            return pygame.image.fromstring(img.tobytes(), img.size, img.mode)
        except Exception:
            surf = pygame.Surface((len(text) * 8, size), pygame.SRCALPHA)
            surf.fill((30, 40, 60))
            return surf


# ---------------------------------------------------------------------------
# Mercator Projection Helpers
# ---------------------------------------------------------------------------

def latlon_to_tile(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Convert (lat, lon) in degrees to fractional Mercator tile coordinates (x, y)."""
    lat_rad = math.radians(math.copysign(min(abs(lat), 85.05112878), lat))
    n = 2.0 ** zoom
    xtile = (lon + 180.0) / 360.0 * n
    ytile = (1.0 - math.log(math.tan(lat_rad) + (1.0 / math.cos(lat_rad))) / math.pi) / 2.0 * n
    return xtile, ytile


def tile_to_latlon(xtile: float, ytile: float, zoom: int) -> tuple[float, float]:
    """Convert fractional Mercator tile coordinates (x, y) to (lat, lon) in degrees."""
    n = 2.0 ** zoom
    lon = xtile / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * ytile / n)))
    lat = math.degrees(lat_rad)
    return lat, lon


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in meters between two lat/lon coordinates."""
    R = 6371000.0  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0)**2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


# ---------------------------------------------------------------------------
# Non-Blocking Async Satellite Tile Engine
# ---------------------------------------------------------------------------

class SatelliteTileEngine:
    """Fetches and caches satellite imagery tiles asynchronously in background threads."""

    def __init__(self, cache_dir: Path = CACHE_DIR):
        self.cache_dir = cache_dir
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

        self._surface_cache: dict[tuple[int, int, int], tuple[pygame.Surface, float, bool]] = {}
        self._pending_requests: set[tuple[int, int, int]] = set()
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self.last_fetch_live = False
        self.last_pull_timestamp: str = "Unknown"

        # Background worker thread for tile fetching
        self._worker_thread = threading.Thread(target=self._download_worker, daemon=True)
        self._worker_thread.start()

    def _download_worker(self):
        while True:
            try:
                z, x, y = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            tile_file = self.cache_dir / f"z{z}_x{x}_y{y}.jpg"
            surface = None
            mtime = time.time()
            is_live = False

            url = ESRI_TILE_URL.format(z=z, x=x, y=y)
            try:
                resp = self.session.get(url, timeout=3.0)
                if resp.status_code == 200 and len(resp.content) > 500:
                    with open(tile_file, "wb") as f:
                        f.write(resp.content)
                    img = Image.open(tile_file).convert("RGB")
                    surface = pygame.image.fromstring(img.tobytes(), img.size, img.mode)
                    mtime = time.time()
                    is_live = True
                    self.last_fetch_live = True
                    self.last_pull_timestamp = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                pass

            if surface is None and tile_file.exists():
                try:
                    img = Image.open(tile_file).convert("RGB")
                    surface = pygame.image.fromstring(img.tobytes(), img.size, img.mode)
                    mtime = tile_file.stat().st_mtime
                    is_live = False
                    self.last_pull_timestamp = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                except Exception:
                    surface = None

            if surface is None:
                surface = pygame.Surface((TILE_SIZE, TILE_SIZE))
                surface.fill((22, 28, 38))
                pygame.draw.rect(surface, (35, 45, 60), (0, 0, TILE_SIZE, TILE_SIZE), 1)
                is_live = False
                mtime = time.time()

            with self._lock:
                self._surface_cache[(z, x, y)] = (surface, mtime, is_live)
                self._pending_requests.discard((z, x, y))

            self._queue.task_done()

    def get_tile(self, z: int, x: int, y: int) -> tuple[pygame.Surface, float, bool]:
        """Non-blocking tile getter. Returns cached tile or placeholder while fetching in background."""
        key = (z, x, y)
        with self._lock:
            if key in self._surface_cache:
                return self._surface_cache[key]

        tile_file = self.cache_dir / f"z{z}_x{x}_y{y}.jpg"
        if tile_file.exists():
            try:
                img = Image.open(tile_file).convert("RGB")
                surface = pygame.image.fromstring(img.tobytes(), img.size, img.mode)
                mtime = tile_file.stat().st_mtime
                is_live = False
                with self._lock:
                    self._surface_cache[key] = (surface, mtime, is_live)
                return surface, mtime, is_live
            except Exception:
                pass

        with self._lock:
            if key not in self._pending_requests:
                self._pending_requests.add(key)
                self._queue.put(key)

        placeholder = pygame.Surface((TILE_SIZE, TILE_SIZE))
        placeholder.fill((22, 28, 38))
        pygame.draw.rect(placeholder, (35, 45, 60), (0, 0, TILE_SIZE, TILE_SIZE), 1)
        return placeholder, time.time(), False


# ---------------------------------------------------------------------------
# MAVLink Controller & Async Waypoint Manager
# ---------------------------------------------------------------------------

class HeronWaypointController:
    """Manages MAVLink connection, vehicle telemetry, arming, and async mission upload."""

    def __init__(self, connection_str: str = "udpin:0.0.0.0:14550"):
        self.connection_str = connection_str
        self.master = None
        self.connected = False
        self.running = True

        # Telemetry State
        self.is_armed = False
        self.mode_str = "DISCONNECTED"
        self.lat = 0.0
        self.lon = 0.0
        self.alt = 0.0
        self.heading = 0.0        # deg (0-360)
        self.yaw = 0.0            # rad
        self.groundspeed = 0.0    # m/s
        self.satellites = 0
        self.fix_type = 0         # 0=No GPS, 3=3D Fix, 4=DGPS, etc.
        self.battery_v = 0.0      # Volts
        self.battery_pct = -1     # %
        self.current_wp_seq = 0   # Current active waypoint index executing

        # Active Waypoints List
        self.waypoints: list[dict] = []
        self._wp_lock = threading.Lock()
        self._log_queue: list[str] = []

        # Thread-safe MAVLink mission queue & upload state
        self._mission_req_queue = queue.Queue()
        self._mav_send_lock = threading.Lock()
        self.is_uploading_mission = False
        self.simulating_mission = False
        self._sim_thread = None

        # Status notification feedback for UI
        self.last_status_msg = ""
        self.status_is_error = False
        self.status_timestamp = 0.0

        # Telemetry Thread
        self.thread = threading.Thread(target=self._telemetry_loop, daemon=True)

        # Logger is always-on: auto-generates a timestamped log in ~/heron_logs/
        self.logger = HeronLogger(
            auto_path     = True,
            session_label = "waypoints",
        )

    def start(self):
        self.thread.start()

    def log(self, text: str):
        print(text)
        self._log_queue.append(text)

    def set_status_msg(self, text: str, error: bool = False):
        self.log(text)
        self.last_status_msg = text
        self.status_is_error = error
        self.status_timestamp = time.time()

    def _telemetry_loop(self):
        self.log(f"[MAVLink] Connecting to {self.connection_str}...")
        while self.running:
            try:
                self.master = mavutil.mavlink_connection(self.connection_str)
                msg = self.master.wait_heartbeat(timeout=3.0)
                if msg:
                    self.connected = True
                    self.log(f"[MAVLink] Connected to System {self.master.target_system} Comp {self.master.target_component}")
                    break
            except Exception:
                time.sleep(1.0)

        # Open logger once connected
        self.logger.open()

        try:
            while self.running and self.master:
                try:
                    msg = self.master.recv_match(blocking=True, timeout=0.5)
                    if not msg:
                        continue

                    # Log every message to the session file
                    self.logger.log_message(msg)

                    msg_type = msg.get_type()

                    if msg_type == 'HEARTBEAT':
                        self.is_armed = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
                        self.mode_str = mavutil.mode_string_v10(msg)

                    elif msg_type in ('GLOBAL_POSITION_INT', 'GPS_RAW_INT', 'GPS2_RAW'):
                        lat_raw = getattr(msg, 'lat', 0)
                        lon_raw = getattr(msg, 'lon', 0)
                        if lat_raw != 0 and lon_raw != 0:
                            self.lat = lat_raw / 1e7
                            self.lon = lon_raw / 1e7
                        if hasattr(msg, 'alt'):
                            self.alt = msg.alt / 1000.0
                        if hasattr(msg, 'hdg') and msg.hdg != 65535:
                            self.heading = msg.hdg / 100.0
                        self.satellites = getattr(msg, 'satellites_visible', getattr(msg, 'satellites', self.satellites))
                        self.fix_type = getattr(msg, 'fix_type', self.fix_type)

                    elif msg_type in ('AHRS2', 'AHRS3'):
                        lat_val = getattr(msg, 'lat', 0)
                        lng_val = getattr(msg, 'lng', 0)
                        if lat_val != 0 and lng_val != 0:
                            self.lat = lat_val / 1e7 if abs(lat_val) > 180 else float(lat_val)
                            self.lon = lng_val / 1e7 if abs(lng_val) > 180 else float(lng_val)

                    elif msg_type == 'HOME_POSITION':
                        if self.lat == 0.0:
                            self.lat = msg.latitude / 1e7
                            self.lon = msg.longitude / 1e7

                    elif msg_type == 'ATTITUDE':
                        self.yaw = msg.yaw
                        if self.heading == 0.0:
                            self.heading = math.degrees(msg.yaw) % 360.0

                    elif msg_type == 'VFR_HUD':
                        self.groundspeed = getattr(msg, 'groundspeed', 0.0)
                        self.heading = getattr(msg, 'heading', self.heading)

                    elif msg_type == 'BATTERY_STATUS':
                        if hasattr(msg, 'voltages') and len(msg.voltages) > 0 and msg.voltages[0] != 65535:
                            self.battery_v = msg.voltages[0] / 1000.0
                        self.battery_pct = getattr(msg, 'battery_remaining', -1)

                    elif msg_type == 'SYS_STATUS':
                        if self.battery_v == 0.0 and hasattr(msg, 'voltage_battery'):
                            self.battery_v = msg.voltage_battery / 1000.0
                        if self.battery_pct == -1 and hasattr(msg, 'battery_remaining'):
                            self.battery_pct = msg.battery_remaining

                    elif msg_type == 'MISSION_CURRENT':
                        self.current_wp_seq = getattr(msg, 'seq', 0)

                    elif msg_type in ('MISSION_REQUEST', 'MISSION_REQUEST_INT', 'MISSION_ACK'):
                        self._mission_req_queue.put(msg)

                except Exception:
                    pass
        finally:
            # Always close logger gracefully when the telemetry loop exits
            self.logger.close()

    # -----------------------------------------------------------------------
    # MAVLink Actions & Async Controls
    # -----------------------------------------------------------------------

    def emergency_stop(self):
        """Immediately aborts autonomous mission, sets MANUAL mode, and zeroes thrusters."""
        self.simulating_mission = False
        self.set_status_msg("[SAFETY] EMERGENCY STOP! Switched to MANUAL mode.", error=True)
        if self.master:
            self.set_mode("MANUAL")
            with self._mav_send_lock:
                for _ in range(5):
                    try:
                        sys_id = self.master.target_system if getattr(self.master, 'target_system', 0) > 0 else 1
                        self.master.mav.manual_control_send(sys_id, 0, 0, 0, 0, 0)
                    except Exception:
                        pass
                    time.sleep(0.02)
        else:
            self.mode_str = "MANUAL"
            self.is_armed = False

    def send_manual_control(self, throttle: int, yaw: int):
        """Send manual control command (throttle: -1000..1000, yaw: -1000..1000)."""
        if self.master and self.connected:
            sys_id = self.master.target_system if getattr(self.master, 'target_system', 0) > 0 else 1
            with self._mav_send_lock:
                self.master.mav.manual_control_send(sys_id, throttle, yaw, 0, 0, 0)

    def set_mode(self, mode_name: str) -> bool:
        """Switch vehicle flight mode (e.g. MANUAL, AUTO, GUIDED, HOLD)."""
        mode_name = mode_name.upper()
        if not self.master or not self.connected:
            self.mode_str = mode_name
            self.log(f"[GCS] Set simulated mode to {mode_name}")
            return True

        if mode_name not in self.master.mode_mapping():
            self.set_status_msg(f"[MAVLink] Unknown mode: {mode_name}", error=True)
            return False

        mode_id = self.master.mode_mapping()[mode_name]
        sys_id = self.master.target_system if getattr(self.master, 'target_system', 0) > 0 else 1
        with self._mav_send_lock:
            self.master.mav.set_mode_send(
                sys_id,
                mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                mode_id
            )
        self.log(f"[MAVLink] Mode set command sent: {mode_name}")
        self.mode_str = mode_name
        return True

    def set_arm(self, arm: bool) -> bool:
        """Arm or Disarm vehicle motors."""
        if not self.master or not self.connected:
            self.is_armed = arm
            self.set_status_msg(f"[GCS] Vehicle {'ARMED' if arm else 'DISARMED'} (Simulated)", error=False)
            return True

        sys_id = self.master.target_system if getattr(self.master, 'target_system', 0) > 0 else 1
        comp_id = self.master.target_component if getattr(self.master, 'target_component', 0) > 0 else 1
        cmd = mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM
        param1 = 1.0 if arm else 0.0
        with self._mav_send_lock:
            self.master.mav.command_long_send(
                sys_id, comp_id, cmd, 0, param1, 0, 0, 0, 0, 0, 0
            )
        self.is_armed = arm
        self.log(f"[MAVLink] {'ARM' if arm else 'DISARM'} command sent.")
        return True

    def upload_mission_async(self):
        """Spawns non-blocking thread to upload mission so UI never freezes."""
        if self.is_uploading_mission:
            self.set_status_msg("[MAVLink] Mission upload already in progress...", error=True)
            return

        t = threading.Thread(target=self._upload_mission_worker, daemon=True)
        t.start()

    def _upload_mission_worker(self):
        self.is_uploading_mission = True
        try:
            self.upload_mission()
        finally:
            self.is_uploading_mission = False

    def upload_mission(self) -> bool:
        with self._wp_lock:
            wps = list(self.waypoints)

        if not wps:
            self.set_status_msg("[MAVLink] No waypoints to upload! Add waypoints on map first.", error=True)
            return False

        # Fallback to simulated autonomous mission if not connected to live vehicle
        if not self.master or not self.connected:
            self.set_status_msg(f"[SIM] Vehicle disconnected. Launching SIMULATED navigation ({len(wps)} WPs)...", error=False)
            self.start_simulated_mission(wps)
            return True

        self.set_status_msg(f"[MAVLink] Uploading {len(wps)} waypoints to ArduRover FCU...", error=False)

        try:
            # Clear old queued mission requests
            while not self._mission_req_queue.empty():
                try:
                    self._mission_req_queue.get_nowait()
                except queue.Empty:
                    break

            sys_id = self.master.target_system if getattr(self.master, 'target_system', 0) > 0 else 1
            comp_id = self.master.target_component if getattr(self.master, 'target_component', 0) > 0 else 1

            # Step 1: Clear existing mission items on vehicle
            with self._mav_send_lock:
                self.master.mav.mission_clear_all_send(sys_id, comp_id)
            time.sleep(0.15)

            total_count = len(wps) + 1  # Item 0 is Home/Takeoff, Items 1..N are waypoints

            # Step 2: Send MISSION_COUNT with retry loop (up to 4 attempts)
            req_msg = None
            for attempt in range(1, 5):
                self.log(f"[MAVLink] Sending MISSION_COUNT={total_count} (Attempt {attempt}/4)...")
                with self._mav_send_lock:
                    self.master.mav.mission_count_send(sys_id, comp_id, total_count)

                try:
                    req_msg = self._mission_req_queue.get(timeout=2.0)
                    if req_msg:
                        break
                except queue.Empty:
                    continue

            if not req_msg:
                self.set_status_msg("[MAVLink] Upload failed: Vehicle ignored MISSION_COUNT (Timeout)", error=True)
                return False

            # Step 3: Handle sequence item requests & MISSION_ACK
            items_sent = set()
            start_time = time.time()

            while time.time() - start_time < 15.0:
                if req_msg is None:
                    try:
                        req_msg = self._mission_req_queue.get(timeout=3.0)
                    except queue.Empty:
                        self.set_status_msg("[MAVLink] Upload failed: Timed out waiting for item request", error=True)
                        return False

                msg = req_msg
                req_msg = None
                msg_type = msg.get_type()

                if msg_type == 'MISSION_ACK':
                    ack_type = getattr(msg, 'type', 0)
                    if ack_type == 0:  # MAV_MISSION_ACCEPTED
                        self.set_status_msg(f"[MAVLink] Mission ACCEPTED! ({len(wps)} WPs). Setting AUTO & ARMED.", error=False)
                        self.set_mode("AUTO")
                        self.set_arm(True)
                        self.current_wp_seq = 1
                        return True
                    else:
                        self.set_status_msg(f"[MAVLink] Mission REJECTED by vehicle (ACK type: {ack_type})", error=True)
                        return False

                elif msg_type in ('MISSION_REQUEST', 'MISSION_REQUEST_INT'):
                    seq = getattr(msg, 'seq', 0)
                    if seq == 0:
                        lat0 = int((self.lat if self.lat != 0 else wps[0]['lat']) * 1e7)
                        lon0 = int((self.lon if self.lon != 0 else wps[0]['lon']) * 1e7)
                        with self._mav_send_lock:
                            self.master.mav.mission_item_int_send(
                                sys_id, comp_id,
                                0,
                                mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                                mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                                0, 1, 0, 0, 0, 0,
                                lat0, lon0, 0.0
                            )
                        items_sent.add(0)
                        self.log(f"[MAVLink] Sent Home Item (seq 0): ({lat0/1e7:.6f}, {lon0/1e7:.6f})")
                    elif 1 <= seq <= len(wps):
                        wp = wps[seq - 1]
                        with self._mav_send_lock:
                            self.master.mav.mission_item_int_send(
                                sys_id, comp_id,
                                seq,
                                mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                                mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                                0, 1, 0, 0, 0, 0,
                                int(wp['lat'] * 1e7),
                                int(wp['lon'] * 1e7),
                                float(wp.get('alt', 2.0))
                            )
                        items_sent.add(seq)
                        self.log(f"[MAVLink] Sent WP {seq}/{len(wps)}: ({wp['lat']:.6f}, {wp['lon']:.6f})")

            self.set_status_msg(f"[MAVLink] Mission upload completed. Setting AUTO mode & ARMED.", error=False)
            self.set_mode("AUTO")
            self.set_arm(True)
            self.current_wp_seq = 1
            return True

        except Exception as e:
            self.set_status_msg(f"[MAVLink] Mission upload error: {e}", error=True)

        return False

    def start_simulated_mission(self, wps: list[dict]):
        """Spawns background simulation loop to step vehicle through waypoints when disconnected."""
        self.simulating_mission = True
        self.is_armed = True
        self.mode_str = "AUTO"
        self.current_wp_seq = 1

        if self.lat == 0.0 or self.lon == 0.0:
            self.lat = wps[0]['lat'] - 0.0001
            self.lon = wps[0]['lon'] - 0.0001

        self._sim_thread = threading.Thread(target=self._simulated_mission_loop, args=(list(wps),), daemon=True)
        self._sim_thread.start()

    def _simulated_mission_loop(self, wps: list[dict]):
        for idx, wp in enumerate(wps):
            if not self.simulating_mission or not self.running:
                break

            self.current_wp_seq = idx + 1
            target_lat, target_lon = wp['lat'], wp['lon']

            while self.simulating_mission and self.running:
                d_lat = target_lat - self.lat
                d_lon = target_lon - self.lon
                dist_deg = math.hypot(d_lat, d_lon)

                # Arrived at waypoint (within ~3 meters)
                if dist_deg < 0.00003:
                    self.log(f"[SIM] Reached Waypoint {idx + 1}/{len(wps)}")
                    time.sleep(0.5)
                    break

                bearing = math.degrees(math.atan2(d_lon, d_lat)) % 360.0
                self.heading = bearing
                self.groundspeed = 2.5  # m/s (~5 knots)

                step_size = 0.00002  # ~2 meters per tick
                self.lat += step_size * math.cos(math.radians(bearing))
                self.lon += step_size * math.sin(math.radians(bearing))

                time.sleep(0.08)

        if self.simulating_mission:
            self.groundspeed = 0.0
            self.mode_str = "HOLD"
            self.simulating_mission = False
            self.set_status_msg("[SIM] Simulated Mission COMPLETED! Mode set to HOLD.", error=False)

    # -----------------------------------------------------------------------
    # Waypoint List Management & File I/O
    # -----------------------------------------------------------------------

    def add_waypoint(self, lat: float, lon: float, alt: float = 2.0):
        with self._wp_lock:
            wp_id = len(self.waypoints) + 1
            self.waypoints.append({"id": wp_id, "lat": round(lat, 7), "lon": round(lon, 7), "alt": round(alt, 1)})
        self.log(f"[WAYPOINT] Added WP {wp_id}: ({lat:.6f}, {lon:.6f})")

    def remove_waypoint(self, index: int):
        with self._wp_lock:
            if 0 <= index < len(self.waypoints):
                self.waypoints.pop(index)
                for idx, wp in enumerate(self.waypoints):
                    wp["id"] = idx + 1
        self.log(f"[WAYPOINT] Removed WP {index + 1}")

    def clear_waypoints(self):
        with self._wp_lock:
            self.waypoints.clear()
        self.log("[WAYPOINT] Cleared all waypoints.")

    def update_waypoint_pos(self, index: int, lat: float, lon: float):
        with self._wp_lock:
            if 0 <= index < len(self.waypoints):
                self.waypoints[index]["lat"] = round(lat, 7)
                self.waypoints[index]["lon"] = round(lon, 7)

    def load_waypoints_file(self, filepath: str) -> bool:
        path = Path(filepath)
        if not path.exists():
            self.log(f"[Waypoints] File not found: {filepath}")
            return False

        try:
            new_wps = []
            if path.suffix.lower() == '.json':
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    items = data.get("waypoints", data) if isinstance(data, dict) else data
                    for idx, item in enumerate(items):
                        new_wps.append({
                            "id": idx + 1,
                            "lat": float(item["lat"]),
                            "lon": float(item["lon"]),
                            "alt": float(item.get("alt", 2.0))
                        })
            elif path.suffix.lower() == '.csv':
                with open(path, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for idx, row in enumerate(reader):
                        new_wps.append({
                            "id": idx + 1,
                            "lat": float(row["lat"] if "lat" in row else row["latitude"]),
                            "lon": float(row["lon"] if "lon" in row else row["longitude"]),
                            "alt": float(row.get("alt", row.get("altitude", 2.0)))
                        })

            if new_wps:
                with self._wp_lock:
                    self.waypoints = new_wps
                self.log(f"[Waypoints] Loaded {len(new_wps)} waypoints from {filepath}")
                return True

        except Exception as e:
            self.log(f"[Waypoints] Error loading file: {e}")

        return False

    def save_waypoints_file(self, filepath: str) -> bool:
        path = Path(filepath)
        try:
            with self._wp_lock:
                wps = list(self.waypoints)

            if path.suffix.lower() == '.json':
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(wps, f, indent=2)
            else:
                with open(path, 'w', encoding='utf-8', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=["id", "lat", "lon", "alt"])
                    writer.writeheader()
                    writer.writerows(wps)

            self.log(f"[Waypoints] Saved {len(wps)} waypoints to {filepath}")
            return True
        except Exception as e:
            self.log(f"[Waypoints] Save error: {e}")
            return False


# ---------------------------------------------------------------------------
# Pygame Satellite Map UI Renderer
# ---------------------------------------------------------------------------

class SatelliteMapUI:
    """Renders interactive satellite tile map, waypoints, vehicle marker, and action toolbar."""

    def __init__(self, controller: HeronWaypointController, tile_engine: SatelliteTileEngine, width: int = 1100, height: int = 750):
        self.controller = controller
        self.tile_engine = tile_engine
        self.width = width
        self.height = height

        # Map View State
        self.center_lat = DEFAULT_LAT
        self.center_lon = DEFAULT_LON
        self.zoom = DEFAULT_ZOOM
        self.auto_center = True     # Continuously lock map center to vehicle live GPS position

        # Mouse & Drag Interaction
        self.is_dragging_map = False
        self.drag_start_mouse = (0, 0)
        self.drag_start_center_tile = (0.0, 0.0)

        self.selected_wp_index = -1
        self.is_dragging_wp = False

        # Fonts & Pygame Surface
        self.fonts = FontRenderer()
        self.screen = None

    def init_display(self):
        pygame.init()
        self.screen = pygame.display.set_mode((self.width, self.height), pygame.RESIZABLE)
        pygame.display.set_caption("Heron USV Ground Control Station")

    def screen_to_latlon(self, px: float, py: float) -> tuple[float, float]:
        """Convert screen pixel coordinates to (lat, lon)."""
        center_xtile, center_ytile = latlon_to_tile(self.center_lat, self.center_lon, self.zoom)
        dx_pixels = px - (self.width / 2.0)
        dy_pixels = py - (self.height / 2.0)

        target_xtile = center_xtile + (dx_pixels / TILE_SIZE)
        target_ytile = center_ytile + (dy_pixels / TILE_SIZE)

        return tile_to_latlon(target_xtile, target_ytile, self.zoom)

    def latlon_to_screen(self, lat: float, lon: float) -> tuple[float, float]:
        """Convert (lat, lon) to screen pixel coordinates."""
        center_xtile, center_ytile = latlon_to_tile(self.center_lat, self.center_lon, self.zoom)
        target_xtile, target_ytile = latlon_to_tile(lat, lon, self.zoom)

        px = (self.width / 2.0) + (target_xtile - center_xtile) * TILE_SIZE
        py = (self.height / 2.0) + (target_ytile - center_ytile) * TILE_SIZE
        return px, py

    def zoom_at_pixel(self, zoom_delta: int, px: float, py: float):
        """Google Maps style zoom: keeps the lat/lon under pixel (px, py) fixed during zoom."""
        new_zoom = max(1, min(19, self.zoom + zoom_delta))
        if new_zoom == self.zoom:
            return

        cursor_lat, cursor_lon = self.screen_to_latlon(px, py)
        self.zoom = new_zoom

        target_xtile, target_ytile = latlon_to_tile(cursor_lat, cursor_lon, self.zoom)
        dx_tiles = (px - (self.width / 2.0)) / TILE_SIZE
        dy_tiles = (py - (self.height / 2.0)) / TILE_SIZE

        new_center_xtile = target_xtile - dx_tiles
        new_center_ytile = target_ytile - dy_tiles
        self.center_lat, self.center_lon = tile_to_latlon(new_center_xtile, new_center_ytile, self.zoom)

    def render(self):
        if not self.screen:
            return

        self.width, self.height = self.screen.get_size()
        self.screen.fill((15, 20, 28))

        # Re-center on vehicle's live GPS feed coordinates if auto_center is enabled
        if self.auto_center and self.controller.lat != 0.0 and self.controller.lon != 0.0:
            self.center_lat = self.controller.lat
            self.center_lon = self.controller.lon

        # -------------------------------------------------------------------
        # 1. Render Satellite Imagery Tiles (Non-blocking)
        # -------------------------------------------------------------------
        center_xtile, center_ytile = latlon_to_tile(self.center_lat, self.center_lon, self.zoom)

        min_tx = int(center_xtile - (self.width / 2.0 / TILE_SIZE) - 1)
        max_tx = int(center_xtile + (self.width / 2.0 / TILE_SIZE) + 1)
        min_ty = int(center_ytile - (self.height / 2.0 / TILE_SIZE) - 1)
        max_ty = int(center_ytile + (self.height / 2.0 / TILE_SIZE) + 1)

        max_tile_idx = (2 ** self.zoom) - 1

        for ty in range(min_ty, max_ty + 1):
            if ty < 0 or ty > max_tile_idx:
                continue
            for tx in range(min_tx, max_tx + 1):
                tx_wrapped = tx % (max_tile_idx + 1)
                tile_surf, _, _ = self.tile_engine.get_tile(self.zoom, tx_wrapped, ty)

                px = (self.width / 2.0) + (tx - center_xtile) * TILE_SIZE
                py = (self.height / 2.0) + (ty - center_ytile) * TILE_SIZE
                self.screen.blit(tile_surf, (px, py))

        # -------------------------------------------------------------------
        # 2. Render Flight Route Lines & Vector Waypoints
        # -------------------------------------------------------------------
        with self.controller._wp_lock:
            wps = list(self.controller.waypoints)

        wp_screen_coords = [self.latlon_to_screen(wp["lat"], wp["lon"]) for wp in wps]

        if len(wp_screen_coords) > 1:
            pygame.draw.lines(self.screen, (0, 220, 255), False, wp_screen_coords, 3)

            for i in range(len(wps) - 1):
                p1 = wp_screen_coords[i]
                p2 = wp_screen_coords[i + 1]
                dist = haversine_distance(wps[i]["lat"], wps[i]["lon"], wps[i+1]["lat"], wps[i+1]["lon"])

                mid_x = (p1[0] + p2[0]) / 2.0
                mid_y = (p1[1] + p2[1]) / 2.0

                lbl = self.fonts.render(f"{dist:.1f} m", size=12, color=(255, 255, 200))
                lbl_bg = pygame.Surface((lbl.get_width() + 6, lbl.get_height() + 4))
                lbl_bg.fill((10, 20, 35))
                lbl_bg.set_alpha(200)
                self.screen.blit(lbl_bg, (mid_x - lbl.get_width() / 2 - 3, mid_y - lbl.get_height() / 2 - 2))
                self.screen.blit(lbl, (mid_x - lbl.get_width() / 2, mid_y - lbl.get_height() / 2))

        for idx, (px, py) in enumerate(wp_screen_coords):
            is_active = (idx + 1 == self.controller.current_wp_seq)
            is_selected = (idx == self.selected_wp_index)

            ring_color = (255, 215, 0) if is_selected else ((0, 255, 120) if is_active else (0, 180, 255))
            pygame.draw.circle(self.screen, ring_color, (int(px), int(py)), 14)
            pygame.draw.circle(self.screen, (15, 20, 30), (int(px), int(py)), 11)

            num_txt = self.fonts.render(str(idx + 1), size=12, color=(255, 255, 255), bold=True)
            self.screen.blit(num_txt, (px - num_txt.get_width() / 2, py - num_txt.get_height() / 2))

        # -------------------------------------------------------------------
        # 3. Render Live Vehicle Boat Marker
        # -------------------------------------------------------------------
        if self.controller.lat != 0.0 and self.controller.lon != 0.0:
            vpx, vpy = self.latlon_to_screen(self.controller.lat, self.controller.lon)

            pygame.draw.circle(self.screen, (255, 60, 60), (int(vpx), int(vpy)), 18, 2)

            hdg_rad = math.radians(self.controller.heading)
            nose_x = vpx + 20 * math.sin(hdg_rad)
            nose_y = vpy - 20 * math.cos(hdg_rad)

            lstern_x = vpx + 10 * math.sin(hdg_rad - math.radians(140))
            lstern_y = vpy - 10 * math.cos(hdg_rad - math.radians(140))

            rstern_x = vpx + 10 * math.sin(hdg_rad + math.radians(140))
            rstern_y = vpy - 10 * math.cos(hdg_rad + math.radians(140))

            pygame.draw.polygon(self.screen, (255, 50, 50), [(nose_x, nose_y), (lstern_x, lstern_y), (rstern_x, rstern_y)])
            pygame.draw.polygon(self.screen, (255, 255, 255), [(nose_x, nose_y), (lstern_x, lstern_y), (rstern_x, rstern_y)], 2)

            boat_lbl = self.fonts.render(f"HERON [{self.controller.groundspeed:.1f} m/s]", size=12, color=(255, 100, 100), bold=True)
            self.screen.blit(boat_lbl, (vpx + 15, vpy - 15))

        # -------------------------------------------------------------------
        # 4. Top Telemetry & Header Bar
        # -------------------------------------------------------------------
        top_bar = pygame.Surface((self.width, 42))
        top_bar.fill((16, 22, 32))
        top_bar.set_alpha(230)
        self.screen.blit(top_bar, (0, 0))

        t1 = self.fonts.render("HERON USV SATELLITE GROUND CONTROL", size=15, color=(88, 166, 255), bold=True)
        self.screen.blit(t1, (12, 11))

        arm_color = (45, 106, 79) if self.controller.is_armed else (109, 26, 26)
        arm_text = "ARMED" if self.controller.is_armed else "DISARMED"
        badge_surf = pygame.Surface((90, 24))
        badge_surf.fill(arm_color)
        b_txt = self.fonts.render(arm_text, size=13, color=(255, 255, 255), bold=True)
        badge_surf.blit(b_txt, (45 - b_txt.get_width() / 2, 12 - b_txt.get_height() / 2))
        self.screen.blit(badge_surf, (370, 9))

        gps_str = f"GPS: ({self.controller.lat:.6f}, {self.controller.lon:.6f})" if self.controller.lat != 0.0 else "GPS: No Fix (0.0, 0.0)"
        lock_str = "TRACKING" if self.auto_center else "MANUAL PAN"
        info_str = f"MODE: {self.controller.mode_str:<8}  Sats: {self.controller.satellites}  Bat: {self.controller.battery_v:.1f}V  {gps_str}  [{lock_str}]"
        info_lbl = self.fonts.render(info_str, size=13, color=(200, 215, 230))
        self.screen.blit(info_lbl, (475, 11))

        # -------------------------------------------------------------------
        # 4b. Interactive Action Control Toolbar with Vector Icons
        # -------------------------------------------------------------------
        toolbar = pygame.Surface((self.width, 46))
        toolbar.fill((22, 28, 38))
        pygame.draw.line(toolbar, (45, 55, 75), (0, 45), (self.width, 45), 1)
        self.screen.blit(toolbar, (0, 42))

        def draw_btn(x, y, w, h, bg_color, border_color, text, icon_type=None, size=13):
            surf = pygame.Surface((w, h))
            surf.fill(bg_color)
            pygame.draw.rect(surf, border_color, (0, 0, w, h), 2)

            # Draw crisp vector icon if specified
            if icon_type == "start":
                pygame.draw.polygon(surf, (255, 255, 255), [(12, 10), (12, 24), (22, 17)])
            elif icon_type == "stop":
                pygame.draw.rect(surf, (255, 255, 255), (12, 11, 12, 12))
            elif icon_type == "arm":
                pygame.draw.circle(surf, (255, 255, 255), (17, 17), 6, 2)
            elif icon_type == "clear":
                pygame.draw.line(surf, (255, 255, 255), (12, 11), (22, 23), 2)
                pygame.draw.line(surf, (255, 255, 255), (22, 11), (12, 23), 2)
            elif icon_type == "center":
                pygame.draw.circle(surf, (255, 255, 255), (17, 17), 6, 2)
                pygame.draw.line(surf, (255, 255, 255), (17, 7), (17, 27), 1)
                pygame.draw.line(surf, (255, 255, 255), (7, 17), (27, 17), 1)

            offset = 16 if icon_type else 0
            t_surf = self.fonts.render(text, size=size, color=(255, 255, 255), bold=True)
            surf.blit(t_surf, (w / 2.0 - t_surf.get_width() / 2.0 + offset / 2, h / 2.0 - t_surf.get_height() / 2.0))
            self.screen.blit(surf, (x, y))

        wp_count = len(wps)
        if self.controller.is_uploading_mission:
            draw_btn(12, 48, 175, 34, (120, 90, 20), (220, 180, 40), "UPLOADING...", "start", 13)
        else:
            btn_start_color = (45, 106, 79) if wp_count > 0 else (35, 50, 45)
            btn_start_border = (64, 180, 100) if wp_count > 0 else (60, 80, 70)
            start_txt = f"START ({wp_count} WPs)" if wp_count > 0 else "START MISSION"
            draw_btn(12, 48, 175, 34, btn_start_color, btn_start_border, start_txt, "start", 13)

        draw_btn(195, 48, 105, 34, (139, 0, 0), (255, 77, 77), "STOP", "stop", 13)

        arm_btn_bg = (217, 119, 6) if self.controller.is_armed else (31, 111, 235)
        arm_btn_bdr = (251, 191, 36) if self.controller.is_armed else (96, 165, 250)
        arm_btn_txt = "DISARM" if self.controller.is_armed else "ARM"
        draw_btn(308, 48, 95, 34, arm_btn_bg, arm_btn_bdr, arm_btn_txt, "arm", 13)

        draw_btn(411, 48, 85, 34, (33, 38, 45), (75, 85, 100), "LOAD", None, 13)
        draw_btn(504, 48, 85, 34, (33, 38, 45), (75, 85, 100), "SAVE", None, 13)
        draw_btn(597, 48, 90, 34, (74, 21, 21), (180, 60, 60), "CLEAR", "clear", 13)
        draw_btn(695, 48, 115, 34, (22, 59, 102), (56, 139, 235), "RE-CENTER", "center", 13)

        if wp_count == 0:
            status_msg = "Mission: Click satellite map to add waypoints"
        elif self.controller.is_uploading_mission:
            status_msg = "Mission: Uploading waypoints to ArduRover FCU..."
        elif self.controller.mode_str == "AUTO":
            status_msg = f"Mission: EXECUTING (WP {self.controller.current_wp_seq} / {wp_count})"
        else:
            status_msg = f"Mission: READY ({wp_count} WPs) — Click [START MISSION] to launch"

        status_lbl = self.fonts.render(status_msg, size=13, color=(255, 220, 100) if wp_count > 0 else (160, 175, 195))
        self.screen.blit(status_lbl, (820, 56))

        # -------------------------------------------------------------------
        # 5. Bottom Instructions Bar
        # -------------------------------------------------------------------
        bot_bar = pygame.Surface((self.width, 30))
        bot_bar.fill((16, 22, 32))
        bot_bar.set_alpha(230)
        self.screen.blit(bot_bar, (0, self.height - 30))

        help_str = "[Click] Add WP  |  [Shift+Click] Delete WP  |  [Pinch / +/-] Google Maps Zoom  |  [V] Re-Center Vehicle  |  [SPACE] EMERGENCY STOP"
        help_lbl = self.fonts.render(help_str, size=12, color=(160, 175, 195))
        self.screen.blit(help_lbl, (12, self.height - 23))

        # -------------------------------------------------------------------
        # 6. Bottom-Right LIVE Satellite Banner Tag
        # -------------------------------------------------------------------
        is_live = self.tile_engine.last_fetch_live
        timestamp = self.tile_engine.last_pull_timestamp

        banner_width = 240
        banner_height = 30
        banner_x = self.width - banner_width - 10
        banner_y = self.height - banner_height - 38

        banner_surf = pygame.Surface((banner_width, banner_height))

        if is_live:
            banner_surf.fill((20, 80, 45))
            pygame.draw.rect(banner_surf, (40, 200, 100), (0, 0, banner_width, banner_height), 2)
            pygame.draw.circle(banner_surf, (0, 255, 120), (16, 15), 5)
            txt1 = self.fonts.render("LIVE SATELLITE FEED", size=13, color=(255, 255, 255), bold=True)
            banner_surf.blit(txt1, (30, 6))
        else:
            banner_surf.fill((80, 65, 15))
            pygame.draw.rect(banner_surf, (220, 180, 30), (0, 0, banner_width, banner_height), 2)
            pygame.draw.circle(banner_surf, (255, 215, 0), (16, 15), 5)
            txt1 = self.fonts.render(f"PULLED: {timestamp}", size=11, color=(255, 240, 190))
            banner_surf.blit(txt1, (28, 7))

        self.screen.blit(banner_surf, (banner_x, banner_y))

        # -------------------------------------------------------------------
        # 7. On-Screen Google Maps Vector Controls (Zoom In/Out + Re-Center)
        # -------------------------------------------------------------------
        btn_x = self.width - 50

        # Center Button
        btn_center = pygame.Surface((36, 36))
        btn_center.fill((30, 40, 55))
        pygame.draw.rect(btn_center, (60, 80, 110), (0, 0, 36, 36), 2)
        pygame.draw.circle(btn_center, (255, 255, 255), (18, 18), 7, 2)
        pygame.draw.line(btn_center, (255, 255, 255), (18, 6), (18, 30), 2)
        pygame.draw.line(btn_center, (255, 255, 255), (6, 18), (30, 18), 2)
        self.screen.blit(btn_center, (btn_x, self.height - 195))

        # Zoom In (+)
        btn_zin = pygame.Surface((36, 36))
        btn_zin.fill((30, 40, 55))
        pygame.draw.rect(btn_zin, (60, 80, 110), (0, 0, 36, 36), 2)
        pygame.draw.line(btn_zin, (255, 255, 255), (18, 10), (18, 26), 3)
        pygame.draw.line(btn_zin, (255, 255, 255), (10, 18), (26, 18), 3)
        self.screen.blit(btn_zin, (btn_x, self.height - 150))

        # Zoom Out (-)
        btn_zout = pygame.Surface((36, 36))
        btn_zout.fill((30, 40, 55))
        pygame.draw.rect(btn_zout, (60, 80, 110), (0, 0, 36, 36), 2)
        pygame.draw.line(btn_zout, (255, 255, 255), (10, 18), (26, 18), 3)
        self.screen.blit(btn_zout, (btn_x, self.height - 105))

        pygame.display.flip()

    # -----------------------------------------------------------------------
    # Pygame Event Handler
    # -----------------------------------------------------------------------

    def handle_event(self, event: pygame.event.Event) -> bool:
        if event.type == pygame.QUIT:
            return False

        elif event.type == pygame.VIDEORESIZE:
            self.width, self.height = event.w, event.h

        elif event.type == pygame.MOUSEWHEEL:
            mx, my = pygame.mouse.get_pos()
            if event.y != 0:
                self.zoom_at_pixel(1 if event.y > 0 else -1, mx, my)

        elif event.type == pygame.MOUSEBUTTONDOWN:
            mx, my = event.pos
            btn_x = self.width - 50

            # Action Control Toolbar Click Handlers (y in 46..82)
            if 46 <= my <= 82:
                if 12 <= mx <= 187:
                    self.controller.upload_mission_async()
                    return True
                elif 195 <= mx <= 300:
                    self.controller.emergency_stop()
                    return True
                elif 308 <= mx <= 403:
                    self.controller.set_arm(not self.controller.is_armed)
                    return True
                elif 411 <= mx <= 496:
                    self.controller.load_waypoints_file("src/utils/waypoints_sample.json")
                    return True
                elif 504 <= mx <= 589:
                    self.controller.save_waypoints_file("src/utils/waypoints_saved.json")
                    return True
                elif 597 <= mx <= 687:
                    self.controller.clear_waypoints()
                    return True
                elif 695 <= mx <= 810:
                    self.auto_center = True
                    if self.controller.lat != 0.0 and self.controller.lon != 0.0:
                        self.center_lat, self.center_lon = self.controller.lat, self.controller.lon
                    elif self.controller.waypoints:
                        self.center_lat, self.center_lon = self.controller.waypoints[0]['lat'], self.controller.waypoints[0]['lon']
                    return True

            # Right Overlay Zoom & Center Buttons
            if mx >= btn_x and mx <= btn_x + 36:
                if self.height - 195 <= my <= self.height - 159:
                    self.auto_center = True
                    if self.controller.lat != 0.0 and self.controller.lon != 0.0:
                        self.center_lat, self.center_lon = self.controller.lat, self.controller.lon
                    elif self.controller.waypoints:
                        self.center_lat, self.center_lon = self.controller.waypoints[0]['lat'], self.controller.waypoints[0]['lon']
                    return True

                elif self.height - 150 <= my <= self.height - 114:
                    self.zoom_at_pixel(+1, self.width / 2, self.height / 2)
                    return True

                elif self.height - 105 <= my <= self.height - 69:
                    self.zoom_at_pixel(-1, self.width / 2, self.height / 2)
                    return True

            if my < 88 or my > self.height - 30:
                return True

            with self.controller._wp_lock:
                wps = list(self.controller.waypoints)

            wp_screen_coords = [self.latlon_to_screen(wp["lat"], wp["lon"]) for wp in wps]

            clicked_wp_idx = -1
            for idx, (wpx, wpy) in enumerate(wp_screen_coords):
                if math.hypot(mx - wpx, my - wpy) <= 18:
                    clicked_wp_idx = idx
                    break

            mods = pygame.key.get_mods()
            is_delete_click = (event.button == 3) or bool(mods & (pygame.KMOD_SHIFT | pygame.KMOD_ALT | pygame.KMOD_META))

            if is_delete_click:
                if clicked_wp_idx >= 0:
                    self.controller.remove_waypoint(clicked_wp_idx)
                    self.selected_wp_index = -1
                else:
                    self.is_dragging_map = True
                    self.drag_start_mouse = (mx, my)
                    self.drag_start_center_tile = latlon_to_tile(self.center_lat, self.center_lon, self.zoom)

            elif event.button == 1:
                if clicked_wp_idx >= 0:
                    self.selected_wp_index = clicked_wp_idx
                    self.is_dragging_wp = True
                else:
                    lat, lon = self.screen_to_latlon(mx, my)
                    self.controller.add_waypoint(lat, lon)
                    if self.controller.lat == 0.0 and len(self.controller.waypoints) == 1:
                        self.center_lat, self.center_lon = lat, lon

            elif event.button in (4, 5):
                self.zoom_at_pixel(1 if event.button == 4 else -1, mx, my)

        elif event.type == pygame.MOUSEBUTTONUP:
            if event.button in (1, 3):
                self.is_dragging_wp = False
                self.is_dragging_map = False

        elif event.type == pygame.MOUSEMOTION:
            mx, my = event.pos
            if self.is_dragging_wp and self.selected_wp_index >= 0:
                lat, lon = self.screen_to_latlon(mx, my)
                self.controller.update_waypoint_pos(self.selected_wp_index, lat, lon)

            elif self.is_dragging_map:
                self.auto_center = False
                dx = mx - self.drag_start_mouse[0]
                dy = my - self.drag_start_mouse[1]

                start_xtile, start_ytile = self.drag_start_center_tile
                new_xtile = start_xtile - (dx / TILE_SIZE)
                new_ytile = start_ytile - (dy / TILE_SIZE)

                self.center_lat, self.center_lon = tile_to_latlon(new_xtile, new_ytile, self.zoom)

        elif event.type == pygame.KEYDOWN:
            if event.key == pygame.K_SPACE:
                self.controller.emergency_stop()

            elif event.key in (pygame.K_PLUS, pygame.K_EQUALS):
                mx, my = pygame.mouse.get_pos()
                self.zoom_at_pixel(+1, mx, my)

            elif event.key in (pygame.K_MINUS, pygame.K_UNDERSCORE):
                mx, my = pygame.mouse.get_pos()
                self.zoom_at_pixel(-1, mx, my)

            elif event.key in (pygame.K_v, pygame.K_r):
                self.auto_center = True
                if self.controller.lat != 0.0 and self.controller.lon != 0.0:
                    self.center_lat = self.controller.lat
                    self.center_lon = self.controller.lon
                    self.controller.log(f"[GCS] Re-centered map on vehicle live GPS feed: ({self.center_lat:.6f}, {self.center_lon:.6f})")
                else:
                    with self.controller._wp_lock:
                        wps = list(self.controller.waypoints)
                    if wps:
                        self.center_lat = wps[0]['lat']
                        self.center_lon = wps[0]['lon']
                        self.controller.log(f"[GCS] Vehicle GPS reports (0, 0). Re-centered map on Waypoint 1: ({self.center_lat:.6f}, {self.center_lon:.6f})")
                    else:
                        self.center_lat = DEFAULT_LAT
                        self.center_lon = DEFAULT_LON
                        self.controller.log(f"[GCS] Vehicle GPS reports (0, 0). Re-centered map on default location: ({DEFAULT_LAT:.6f}, {DEFAULT_LON:.6f})")

            elif event.key == pygame.K_g:
                with self.controller._wp_lock:
                    wps = list(self.controller.waypoints)
                if wps:
                    self.controller.lat = wps[0]['lat']
                    self.controller.lon = wps[0]['lon']
                else:
                    self.controller.lat = DEFAULT_LAT
                    self.controller.lon = DEFAULT_LON
                self.auto_center = True
                self.center_lat = self.controller.lat
                self.center_lon = self.controller.lon
                self.controller.log(f"[GCS] Test GPS location set: ({self.controller.lat:.6f}, {self.controller.lon:.6f})")

            elif event.key == pygame.K_u:
                self.controller.upload_mission_async()

            elif event.key == pygame.K_l:
                sample_file = Path("src/utils/waypoints_sample.json")
                if sample_file.exists():
                    self.controller.load_waypoints_file(str(sample_file))

            elif event.key == pygame.K_s:
                save_file = Path("src/utils/waypoints_saved.json")
                self.controller.save_waypoints_file(str(save_file))

            elif event.key == pygame.K_c:
                self.controller.clear_waypoints()

            elif event.key == pygame.K_m:
                self.controller.set_mode("MANUAL")

            elif event.key == pygame.K_a:
                self.controller.set_arm(True)

            elif event.key == pygame.K_d:
                self.controller.set_arm(False)

            elif event.key in (pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT):
                thr = 300 if event.key == pygame.K_UP else (-300 if event.key == pygame.K_DOWN else 0)
                yaw = 300 if event.key == pygame.K_RIGHT else (-300 if event.key == pygame.K_LEFT else 0)
                self.controller.send_manual_control(thr, yaw)

        return True


# ---------------------------------------------------------------------------
# Textual Terminal UI App (--tui mode)
# ---------------------------------------------------------------------------

class WaypointTUIApp(App):
    """Textual Terminal UI for Heron Waypoint Planner & Teleop."""

    TITLE = "Heron USV Waypoint Ground Station"
    CSS = """
    Screen { background: #0d1117; padding: 0; }
    #title { background: #161b22; color: #58a6ff; text-align: center; height: 1; text-style: bold; }
    #telemetry-panel { background: #161b22; border: tall #30363d; height: 4; padding: 0 1; }
    #table-panel { background: #161b22; border: tall #30363d; height: 1fr; padding: 0 1; }
    #log-panel { background: #161b22; border: tall #30363d; height: 8; padding: 0 1; }
    RichLog { background: #0d1117; height: 1fr; }
    """

    BINDINGS = [
        Binding("space", "stop", "STOP", priority=True),
        Binding("u", "upload", "Upload Mission"),
        Binding("l", "load_file", "Load File"),
        Binding("s", "save_file", "Save File"),
        Binding("c", "clear_wps", "Clear WPs"),
        Binding("a", "arm", "ARM"),
        Binding("d", "disarm", "DISARM"),
        Binding("m", "manual", "MANUAL"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, controller: HeronWaypointController, **kwargs):
        super().__init__(**kwargs)
        self.controller = controller

    def compose(self) -> ComposeResult:
        yield Static("HERON USV  ·  WAYPOINT PLANNER & TELEOPERATION TUI", id="title")

        with Container(id="telemetry-panel"):
            yield Label("Connecting to vehicle telemetry...", id="telem-status")

        with Container(id="table-panel"):
            yield Label("▸ Active Waypoints List", id="table-label")
            table = DataTable(id="wp-table")
            table.add_columns("#", "Latitude", "Longitude", "Alt (m)", "Status")
            yield table

        with Container(id="log-panel"):
            yield Label("▸ System Log Messages", id="log-label")
            yield RichLog(id="msg-log", markup=True, highlight=True, max_lines=30)

        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(1 / 5, self._update_ui)

    def _update_ui(self) -> None:
        c = self.controller

        arm_str = "[bold green]ARMED[/]" if c.is_armed else "[bold red]DISARMED[/]"
        stat_text = (
            f"State: {arm_str} | Mode: [yellow]{c.mode_str}[/] | "
            f"Lat: {c.lat:.6f} Lon: {c.lon:.6f} | "
            f"Speed: {c.groundspeed:.1f} m/s | Bat: {c.battery_v:.1f}V ({c.battery_pct}%) | Sats: {c.satellites}"
        )
        self.query_one("#telem-status", Label).update(Text.from_markup(stat_text))

        table = self.query_one("#wp-table", DataTable)
        table.clear()

        with c._wp_lock:
            wps = list(c.waypoints)

        for wp in wps:
            status = "EXEC" if wp["id"] == c.current_wp_seq else "PENDING"
            table.add_row(str(wp["id"]), f"{wp['lat']:.7f}", f"{wp['lon']:.7f}", f"{wp['alt']:.1f}", status)

        log_widget = self.query_one("#msg-log", RichLog)
        while c._log_queue:
            log_widget.write(c._log_queue.pop(0))

    def action_stop(self) -> None: self.controller.emergency_stop()
    def action_upload(self) -> None: self.controller.upload_mission_async()
    def action_load_file(self) -> None: self.controller.load_waypoints_file("src/utils/waypoints_sample.json")
    def action_save_file(self) -> None: self.controller.save_waypoints_file("src/utils/waypoints_saved.json")
    def action_clear_wps(self) -> None: self.controller.clear_waypoints()
    def action_arm(self) -> None: self.controller.set_arm(True)
    def action_disarm(self) -> None: self.controller.set_arm(False)
    def action_manual(self) -> None: self.controller.set_mode("MANUAL")


# ---------------------------------------------------------------------------
# Main Application Launcher
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Heron USV Satellite Map Teleoperation & Waypoint Planner")
    parser.add_argument("--waypoints", type=str, help="Path to initial CSV or JSON waypoints file")
    parser.add_argument("--no-ip-setup", action="store_true", help="Skip automatic GCS IP network setup")
    parser.add_argument("--tui", action="store_true", help="Run Textual TUI interface alongside Map window")
    args = parser.parse_args()

    if not args.no_ip_setup:
        try:
            print("[Network] Verifying Ethernet connection to Heron companion computer...")
            ensure_gcs_ip()
        except Exception as e:
            print(f"[Network] IP setup warning: {e}")

    conn_str = mavlink_connection_string()
    controller = HeronWaypointController(connection_str=conn_str)
    controller.start()

    if args.waypoints:
        controller.load_waypoints_file(args.waypoints)

    tile_engine = SatelliteTileEngine()
    map_ui = SatelliteMapUI(controller, tile_engine)

    headless = os.environ.get("SDL_VIDEODRIVER") == "dummy" or not os.environ.get("DISPLAY") and sys.platform.startswith("linux")

    if args.tui:
        if not headless:
            def map_thread():
                try:
                    map_ui.init_display()
                    clock = pygame.time.Clock()
                    while controller.running:
                        for event in pygame.event.get():
                            if not map_ui.handle_event(event):
                                controller.running = False
                                break
                        map_ui.render()
                        clock.tick(30)
                except Exception as e:
                    print(f"[UI] Pygame map thread error: {e}")

            t = threading.Thread(target=map_thread, daemon=True)
            t.start()

        app = WaypointTUIApp(controller)
        app.run()
        controller.running = False
        return

    if not headless:
        try:
            map_ui.init_display()
        except Exception as e:
            print(f"[UI] Could not open GUI display window ({e}). Running in headless mode.")
            headless = True

    print("\n" + "=" * 65)
    print(" HERON USV SATELLITE GROUND CONTROL STATION READY")
    print("=" * 65)
    print(" Controls:")
    print("   • Left-Click Map       : Add Waypoint (lat, lon)")
    print("   • Drag Waypoint        : Move Waypoint")
    print("   • Shift+Click Waypoint : Delete Waypoint")
    print("   • Scroll / Pinch       : Google Maps Zoom")
    print("   • Key 'V' / 'R'        : Lock & Re-Center Map on Vehicle Live GPS Feed")
    print("   • Key 'G'              : Set Test GPS Position (Indoor testing)")
    print("   • Key 'U' / Button     : Upload Mission & Start Auto Navigation")
    print("   • Key 'L' / 'S'        : Load / Save Waypoints (JSON/CSV)")
    print("   • Key 'C'              : Clear All Waypoints")
    print("   • Key 'M'              : Set MANUAL Mode")
    print("   • Key 'A' / 'D'        : ARM / DISARM Vehicle")
    print("   • SPACEBAR             : EMERGENCY STOP (Instant Override)")
    print("=" * 65 + "\n")

    running = True
    clock = pygame.time.Clock()

    try:
        while running and controller.running:
            if not headless:
                for event in pygame.event.get():
                    if not map_ui.handle_event(event):
                        running = False
                map_ui.render()
                clock.tick(30)
            else:
                time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n[App] Exiting gracefully...")
    finally:
        controller.running = False
        if not headless:
            pygame.quit()
        print("[App] Shutdown complete.")


if __name__ == "__main__":
    main()
