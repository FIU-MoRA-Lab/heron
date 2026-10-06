"""MAVLink waypoint controller and satellite tile service shared by front ends."""
import csv
import json
import math
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import requests
from PIL import Image
from pymavlink import mavutil
from heron.utils.logger import HeronLogger, mavlink_to_dict
from heron.mission.mission_logic import (
    DEFAULT_ZOOM, TILE_SIZE,
    latlon_to_tile, tile_to_latlon, haversine_distance,
)

SAMPLE_MISSION_FILE = Path(__file__).resolve().parent.parent / "examples" / "missions" / "sample.json"
SAVED_MISSION_FILE = Path.cwd() / "mission_saved.json"

# Constants & Defaults
# ---------------------------------------------------------------------------
CACHE_DIR = Path.home() / ".cache" / "heron_tiles"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

ESRI_TILE_URL = "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
USER_AGENT = "HeronUSV-GCS/1.0"
CDSE_TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
CDSE_PROCESS_URL = "https://sh.dataspace.copernicus.eu/process/v1"
SENTINEL_CLEAR_EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B02", "B03", "B04", "SCL", "dataMask"] }],
    output: { bands: 4, sampleType: "AUTO" },
    mosaicking: "ORBIT"
  };
}
function evaluatePixel(samples) {
  // Observations arrive newest first; use the newest clear pixel and fall back
  // to an older clear observation where clouds cover it.
  for (let i = 0; i < samples.length; i++) {
    const s = samples[i];
    if (!s.dataMask || s.SCL === 0 || s.SCL === 3 ||
        s.SCL === 8 || s.SCL === 9 || s.SCL === 10) continue;
    return [Math.min(1, 2.5 * s.B04),
            Math.min(1, 2.5 * s.B03),
            Math.min(1, 2.5 * s.B02), 1];
  }
  return [0, 0, 0, 0];
}
"""


def _load_local_credentials():
    """Load the ignored project-local env file without printing secret values."""
    candidates = (Path.cwd() / ".env.local", Path(__file__).resolve().parents[3] / ".env.local")
    for env_path in candidates:
        try:
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key.startswith("export "):
                    key = key.removeprefix("export ").strip()
                if key not in {"COPERNICUS_CLIENT_ID", "COPERNICUS_CLIENT_SECRET"}:
                    continue
                value = value.strip().strip("\"'")
                if value:
                    os.environ.setdefault(key, value)
        except OSError:
            continue


# ---------------------------------------------------------------------------
# Non-Blocking Async Satellite Tile Engine
# ---------------------------------------------------------------------------

class SatelliteMapTiles:
    """Fetches and caches satellite imagery tiles asynchronously in background threads."""

    def __init__(self, cache_dir: Path = CACHE_DIR):
        _load_local_credentials()
        self.cache_dir = cache_dir
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

        self._tile_cache: dict[tuple[int, int, int], tuple[Image.Image, float, bool]] = {}
        self._pending_requests: set[tuple[int, int, int]] = set()
        self._sentinel_queue = queue.Queue()
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self.last_fetch_live = False
        self.last_pull_timestamp: str = "Unknown"
        self.last_error = ""
        self.last_sentinel_error = ""
        self.cdse_client_id = os.environ.get("COPERNICUS_CLIENT_ID", "").strip()
        self.cdse_client_secret = os.environ.get("COPERNICUS_CLIENT_SECRET", "").strip()
        self.imagery_source_label = (
            "S2 RECENT CLEAR · ESRI FILL"
            if self.cdse_client_id and self.cdse_client_secret
            else "ESRI IMAGERY · COPERNICUS LOGIN NEEDED"
        )
        self._token_lock = threading.Lock()
        self._access_token = None
        self._token_expires_at = 0.0

        # Background worker thread for tile fetching
        self._worker_thread = threading.Thread(target=self._download_worker, daemon=True)
        self._worker_thread.start()
        self._sentinel_thread = threading.Thread(target=self._sentinel_worker, daemon=True)
        self._sentinel_thread.start()

    def _download_worker(self):
        while True:
            try:
                z, x, y = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            tile_file = self.cache_dir / f"z{z}_x{x}_y{y}.jpg"
            tile_image = None
            mtime = time.time()
            is_live = False

            tile_image = self._fetch_esri_tile(z, x, y)
            is_live = tile_image is not None

            if tile_image is not None:
                self.last_error = ""
                try:
                    tile_image.save(tile_file, format="JPEG", quality=92)
                    mtime = time.time()
                    self.last_fetch_live = is_live
                    self.last_pull_timestamp = datetime.fromtimestamp(
                        mtime, tz=timezone.utc
                    ).strftime("%Y-%m-%d %H:%M UTC")
                except Exception:
                    pass

            if tile_image is None and tile_file.exists():
                try:
                    img = Image.open(tile_file).convert("RGB")
                    tile_image = img
                    mtime = tile_file.stat().st_mtime
                    is_live = False
                    self.last_pull_timestamp = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    self.last_error = ""
                except Exception:
                    tile_image = None

            if tile_image is None:
                esri_error = self.last_error or "Esri tiles are unavailable."
                errors = [error for error in (self.last_sentinel_error, esri_error) if error]
                self.last_error = " ".join(errors) or "No online or cached imagery; check internet access."
                tile_image = Image.new("RGB", (TILE_SIZE, TILE_SIZE), (22, 28, 38))
                is_live = False
                mtime = time.time()

            with self._lock:
                self._tile_cache[(z, x, y)] = (tile_image, mtime, is_live)
                self._pending_requests.discard((z, x, y))

            if self.cdse_client_id and self.cdse_client_secret:
                # Fetch Sentinel separately so its processing latency never
                # blocks the fast basemap tiles from appearing.
                self._sentinel_queue.put((z, x, y))

            self._queue.task_done()

    def _sentinel_worker(self):
        while True:
            try:
                z, x, y = self._sentinel_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                sentinel = self._fetch_sentinel_tile(z, x, y)
                if sentinel is not None:
                    self.last_sentinel_error = ""
                    with self._lock:
                        current = self._tile_cache.get((z, x, y))
                    base = current[0] if current else None
                    if base is not None:
                        composite = Image.alpha_composite(base.convert("RGBA"), sentinel).convert("RGB")
                        tile_file = self.cache_dir / f"z{z}_x{x}_y{y}.jpg"
                        composite.save(tile_file, format="JPEG", quality=92)
                        with self._lock:
                            self._tile_cache[(z, x, y)] = (composite, time.time(), True)
                else:
                    self.last_sentinel_error = "Copernicus returned no image."
            except Exception as exc:
                self.last_sentinel_error = self._brief_error("Copernicus", exc)
            finally:
                self._sentinel_queue.task_done()

    def _fetch_esri_tile(self, z, x, y):
        try:
            url = ESRI_TILE_URL.format(z=z, x=x, y=y)
            response = self.session.get(url, timeout=5.0)
            if response.status_code == 200 and len(response.content) > 500:
                return Image.open(BytesIO(response.content)).convert("RGB")
            self.last_error = f"Esri tile request returned HTTP {response.status_code}."
        except Exception as exc:
            self.last_error = self._brief_error("Esri tiles", exc)
        return None

    @staticmethod
    def _brief_error(source, exc):
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            return f"{source} returned HTTP {exc.response.status_code}."
        return f"{source} request failed ({type(exc).__name__})."

    def _get_cdse_token(self, force_refresh=False):
        with self._token_lock:
            if (not force_refresh and self._access_token
                    and time.time() < self._token_expires_at - 60):
                return self._access_token
            response = self.session.post(
                CDSE_TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.cdse_client_id,
                    "client_secret": self.cdse_client_secret,
                },
                timeout=10.0,
            )
            response.raise_for_status()
            token_data = response.json()
            self._access_token = token_data["access_token"]
            self._token_expires_at = time.time() + int(token_data.get("expires_in", 300))
            return self._access_token

    def _fetch_sentinel_tile(self, z, x, y):
        north, west = tile_to_latlon(x, y, z)
        south, east = tile_to_latlon(x + 1, y + 1, z)
        now = datetime.now(timezone.utc)
        payload = {
            "input": {
                "bounds": {
                    "bbox": [west, south, east, north],
                    "properties": {"crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
                },
                "data": [{
                    "type": "sentinel-2-l2a",
                    "dataFilter": {
                        "timeRange": {
                            "from": (now - timedelta(days=30)).isoformat(),
                            "to": now.isoformat(),
                        },
                        "mosaickingOrder": "mostRecent",
                    },
                }],
            },
            "output": {
                "width": TILE_SIZE,
                "height": TILE_SIZE,
                "responses": [{"identifier": "default", "format": {"type": "image/png"}}],
            },
            "evalscript": SENTINEL_CLEAR_EVALSCRIPT,
        }
        for attempt in range(2):
            token = self._get_cdse_token(force_refresh=bool(attempt))
            response = self.session.post(
                CDSE_PROCESS_URL,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30.0,
            )
            if response.status_code == 401 and attempt == 0:
                continue
            response.raise_for_status()
            return Image.open(BytesIO(response.content)).convert("RGBA")
        return None

    def get_tile(self, z: int, x: int, y: int) -> tuple[Image.Image, float, bool]:
        """Non-blocking tile getter. Returns cached tile or placeholder while fetching in background."""
        key = (z, x, y)
        with self._lock:
            if key in self._tile_cache:
                return self._tile_cache[key]
            if key not in self._pending_requests:
                self._pending_requests.add(key)
                self._queue.put(key)
                placeholder = Image.new("RGB", (TILE_SIZE, TILE_SIZE), (22, 28, 38))
                self._tile_cache[key] = (placeholder, time.time(), False)
            return self._tile_cache[key]


# ---------------------------------------------------------------------------
# MAVLink Controller & Async Waypoint Manager
# ---------------------------------------------------------------------------

class MissionController:
    """Manages MAVLink connection, vehicle telemetry, arming, and async mission upload."""

    def __init__(self, connection_str: str = "udpin:0.0.0.0:14550"):
        self.connection_str = connection_str
        self.master = None
        self.connected = False
        self.running = True
        self.autopilot_type = None
        self.vehicle_type = None
        self.autopilot_component = None

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
        self.mission_uploaded = False
        self.is_starting_mission = False
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
        while self.running and not self.connected:
            try:
                self.master = mavutil.mavlink_connection(self.connection_str)
                while self.running:
                    msg = self.master.recv_match(type="HEARTBEAT", blocking=True, timeout=3.0)
                    if not msg:
                        break
                    # UDP may first deliver a companion-computer/GCS heartbeat.
                    # Only identify the autopilot as connected after a vehicle heartbeat.
                    if not self.master.probably_vehicle_heartbeat(msg):
                        continue
                    self.autopilot_type = getattr(msg, "autopilot", None)
                    self.vehicle_type = getattr(msg, "type", None)
                    self.autopilot_component = msg.get_srcComponent()
                    self.connected = True
                    self.log(
                        f"[MAVLink] Connected to vehicle System {self.master.target_system} "
                        f"(heartbeat component {msg.get_srcComponent()}) "
                        f"(autopilot {self.autopilot_type}, type {self.vehicle_type})"
                    )
                    break
                if not self.connected:
                    self.master.close()
                    self.master = None
                    time.sleep(1.0)
            except Exception:
                self.connected = False
                if self.master:
                    try:
                        self.master.close()
                    except Exception:
                        pass
                self.master = None
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
                        if not self.master.probably_vehicle_heartbeat(msg):
                            continue
                        component = msg.get_srcComponent()
                        if component == mavutil.mavlink.MAV_COMP_ID_AUTOPILOT1:
                            if self.autopilot_component not in (None, component):
                                self.log(
                                    f"[MAVLink] Using autopilot heartbeat component {component} "
                                    f"instead of {self.autopilot_component}."
                                )
                            self.autopilot_component = component
                            self.autopilot_type = getattr(msg, "autopilot", self.autopilot_type)
                            self.vehicle_type = getattr(msg, "type", self.vehicle_type)
                        elif component != self.autopilot_component:
                            continue
                        self.is_armed = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
                        self.mode_str = mavutil.mode_string_v10(msg)
                        if self.autopilot_type == mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA:
                            rover_modes = {
                                mode_id: mode_name
                                for mode_id, mode_name in mavutil.mode_mapping_rover.items()
                            }
                            if self.vehicle_type == 0 or self.mode_str.startswith("Mode("):
                                self.mode_str = rover_modes.get(msg.custom_mode, self.mode_str)

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
        self.set_status_msg("[SAFETY] EMERGENCY STOP requested; sending neutral controls.", error=True)
        if not self.master or not self.connected:
            self.mode_str = "MANUAL"
            self.set_status_msg("[SAFETY] No active vehicle link; neutral controls could not be sent.", error=True)
            return

        # Keep MAVLink writes and the short neutral burst off the GUI thread.
        def stop_worker():
            mode_set = self.set_mode("MANUAL")
            for _ in range(5):
                self.send_manual_control(0, 0)
                time.sleep(0.02)
            if not mode_set:
                self.set_status_msg(
                    "[SAFETY] Neutral controls sent, but MANUAL mode could not be selected.",
                    error=True,
                )
        threading.Thread(target=stop_worker, name="heron-emergency-stop", daemon=True).start()

    def send_manual_control(self, throttle: int, yaw: int):
        """Send manual control command (throttle: -1000..1000, yaw: -1000..1000)."""
        if self.master and self.connected:
            master = self.master
            sys_id = master.target_system if getattr(master, 'target_system', 0) > 0 else 1
            try:
                with self._mav_send_lock:
                    master.mav.manual_control_send(sys_id, throttle, 0, 0, yaw, 0)
                    # Use the same target component selected by the legacy TUI
                    # after its heartbeat. Some vehicle bridges do not route
                    # component-0 RC overrides to the autopilot.
                    comp_id = master.target_component
                    pwm_steer = int(1500 + yaw * 0.5)
                    pwm_throttle = int(1500 + throttle * 0.5)
                    master.mav.rc_channels_override_send(
                        sys_id, comp_id, pwm_steer, 0, pwm_throttle, 0, 0, 0, 0, 0
                    )
                return True
            except Exception as exc:
                self.last_status_msg = f"[MAVLink] Manual control send failed: {exc}"
                self.status_is_error = True
        return False

    def set_mode(self, mode_name: str) -> bool:
        """Switch vehicle flight mode (e.g. MANUAL, AUTO, GUIDED, HOLD)."""
        mode_name = mode_name.upper()
        if not self.master or not self.connected:
            self.mode_str = mode_name
            self.log(f"[GCS] Set simulated mode to {mode_name}")
            return True

        try:
            mode_mapping = self.master.mode_mapping()
        except Exception as exc:
            mode_mapping = None
            self.set_status_msg(f"[MAVLink] Could not read vehicle mode mapping: {exc}", error=True)
        if (not mode_mapping
                and self.autopilot_type == mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA):
            # Heron is an ArduRover surface vehicle. Pymavlink has no table
            # for some generic/legacy HEARTBEAT vehicle types, so use Rover's
            # standard mode table when the autopilot is known to be ArduPilot.
            mode_mapping = {
                mode_name: mode_id
                for mode_id, mode_name in mavutil.mode_mapping_rover.items()
            }
            self.log(
                f"[MAVLink] Using ArduRover mode table for unsupported vehicle type "
                f"{self.vehicle_type}."
            )
        if not mode_mapping:
            self.set_status_msg(
                f"[MAVLink] Cannot select {mode_name}: vehicle mode mapping is unavailable.",
                error=True,
            )
            return False
        if mode_name not in mode_mapping:
            self.set_status_msg(f"[MAVLink] Unknown mode: {mode_name}", error=True)
            return False

        mode_id = mode_mapping[mode_name]
        sys_id = self.master.target_system if getattr(self.master, 'target_system', 0) > 0 else 1
        try:
            with self._mav_send_lock:
                self.master.mav.set_mode_send(
                    sys_id,
                    mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                    mode_id
                )
        except Exception as exc:
            self.set_status_msg(f"[MAVLink] Could not send {mode_name} mode command: {exc}", error=True)
            return False
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
        # The existing terminal controller broadcasts commands with component 0.
        comp_id = 0
        cmd = mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM
        param1 = 1.0 if arm else 0.0
        param2 = 21196.0 if arm else 0.0
        try:
            with self._mav_send_lock:
                self.master.mav.command_long_send(
                    sys_id, comp_id, cmd, 0, param1, param2, 0, 0, 0, 0, 0
                )
        except Exception as exc:
            self.set_status_msg(f"[MAVLink] Could not send {'ARM' if arm else 'DISARM'} command: {exc}", error=True)
            return False
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

    def start_mission_async(self):
        """Start an uploaded mission, or explicitly start offline simulation."""
        if self.is_starting_mission:
            self.set_status_msg("[MAVLink] Mission start already in progress...", error=True)
            return
        threading.Thread(target=self._start_mission_worker, daemon=True).start()

    def _start_mission_worker(self):
        self.is_starting_mission = True
        try:
            with self._wp_lock:
                wps = list(self.waypoints)
            if not wps:
                self.set_status_msg("[MAVLink] No waypoints to start.", error=True)
                return
            if not self.mission_uploaded:
                self.set_status_msg("[MAVLink] Upload the current waypoint plan before starting.", error=True)
                return
            if not self.master or not self.connected:
                self.set_status_msg("[SIM] Starting simulated waypoint mission.", error=False)
                self.start_simulated_mission(wps)
                return
            if not self.set_mode("AUTO"):
                self.set_status_msg("[MAVLink] Mission is uploaded, but AUTO mode could not be selected.", error=True)
                return
            if not self.set_arm(True):
                self.set_status_msg("[MAVLink] AUTO selected, but vehicle could not be armed.", error=True)
                return
            self.current_wp_seq = 1
            self.set_status_msg("[MAVLink] Mission started: AUTO mode selected and vehicle armed.", error=False)
        finally:
            self.is_starting_mission = False

    def _upload_mission_worker(self):
        self.is_uploading_mission = True
        try:
            self.upload_mission()
        finally:
            self.is_uploading_mission = False

    def upload_mission(self) -> bool:
        self.mission_uploaded = False
        with self._wp_lock:
            wps = list(self.waypoints)

        if not wps:
            self.set_status_msg("[MAVLink] No waypoints to upload! Add waypoints on map first.", error=True)
            return False

        # Fallback to simulated autonomous mission if not connected to live vehicle
        if not self.master or not self.connected:
            self.mission_uploaded = True
            self.set_status_msg(
                f"[SIM] {len(wps)} waypoints staged. Press START MISSION to simulate navigation.",
                error=False,
            )
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
                        self.mission_uploaded = True
                        self.set_status_msg(
                            f"[MAVLink] Mission uploaded ({len(wps)} waypoints). Press START MISSION to begin.",
                            error=False,
                        )
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

            self.mission_uploaded = True
            self.set_status_msg(
                f"[MAVLink] Mission uploaded ({len(wps)} waypoints). Press START MISSION to begin.",
                error=False,
            )
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
            self.mission_uploaded = False
            wp_id = len(self.waypoints) + 1
            self.waypoints.append({"id": wp_id, "lat": round(lat, 7), "lon": round(lon, 7), "alt": round(alt, 1)})
        self.log(f"[WAYPOINT] Added WP {wp_id}: ({lat:.6f}, {lon:.6f})")

    def remove_waypoint(self, index: int):
        with self._wp_lock:
            if 0 <= index < len(self.waypoints):
                self.mission_uploaded = False
                self.waypoints.pop(index)
                for idx, wp in enumerate(self.waypoints):
                    wp["id"] = idx + 1
        self.log(f"[WAYPOINT] Removed WP {index + 1}")

    def clear_waypoints(self):
        with self._wp_lock:
            self.mission_uploaded = False
            self.waypoints.clear()
        self.log("[WAYPOINT] Cleared all waypoints.")

    def update_waypoint_pos(self, index: int, lat: float, lon: float):
        with self._wp_lock:
            if 0 <= index < len(self.waypoints):
                self.mission_uploaded = False
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
                    self.mission_uploaded = False
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
