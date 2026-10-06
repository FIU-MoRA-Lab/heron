"""Engine-independent state and behavior for waypoint planning and teleop.

Front ends translate their native input events into this module and render its
state. No UI toolkit or drawing types belong here.
"""

from __future__ import annotations

import math
import threading

DEFAULT_ZOOM = 17
TILE_SIZE = 256
MIN_ZOOM = 1
MAX_ZOOM = 19
MAX_MANUAL_INPUT = 1000


def latlon_to_tile(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Convert geographic coordinates to fractional Web Mercator tile units."""
    lat_rad = math.radians(math.copysign(min(abs(lat), 85.05112878), lat))
    n = 2.0 ** zoom
    xtile = (lon + 180.0) / 360.0 * n
    ytile = (1.0 - math.log(math.tan(lat_rad) + (1.0 / math.cos(lat_rad))) / math.pi) / 2.0 * n
    return xtile, ytile


def tile_to_latlon(xtile: float, ytile: float, zoom: int) -> tuple[float, float]:
    """Convert fractional Web Mercator tile units to geographic coordinates."""
    n = 2.0 ** zoom
    lon = xtile / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * ytile / n)))
    return math.degrees(lat_rad), lon


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance between coordinates in meters."""
    earth_radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return earth_radius * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


class MapViewport:
    """Map center, zoom, and projection operations shared by all front ends."""

    def __init__(self, lat=None, lon=None, zoom=None):
        # Start with a useful world view while waiting for a real location.
        # Zoom into the vehicle/desktop automatically when a fix arrives.
        self.center_lat = float(lat) if lat is not None else 0.0
        self.center_lon = float(lon) if lon is not None else 0.0
        has_location = (lat is not None and lon is not None and
                        (float(lat) != 0.0 or float(lon) != 0.0))
        self._auto_zoom = zoom is None and not has_location
        initial_zoom = zoom if zoom is not None else (DEFAULT_ZOOM if has_location else 4)
        self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, int(initial_zoom)))
        self.auto_center = True

    def screen_to_geo(self, px, py, width, height):
        cx, cy = latlon_to_tile(self.center_lat, self.center_lon, self.zoom)
        tx = cx + (px - width / 2.0) / TILE_SIZE
        ty = cy + (py - height / 2.0) / TILE_SIZE
        return tile_to_latlon(tx, ty, self.zoom)

    def geo_to_screen(self, lat, lon, width, height):
        cx, cy = latlon_to_tile(self.center_lat, self.center_lon, self.zoom)
        tx, ty = latlon_to_tile(lat, lon, self.zoom)
        return width / 2.0 + (tx - cx) * TILE_SIZE, height / 2.0 + (ty - cy) * TILE_SIZE

    def pan_pixels(self, dx, dy):
        cx, cy = latlon_to_tile(self.center_lat, self.center_lon, self.zoom)
        self.center_lat, self.center_lon = tile_to_latlon(
            cx - dx / TILE_SIZE, cy - dy / TILE_SIZE, self.zoom
        )
        self.auto_center = False
        self._auto_zoom = False

    def set_zoom(self, zoom):
        self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, int(zoom)))
        self._auto_zoom = False
        return self.zoom

    def zoom_at(self, zoom_delta, px, py, width, height):
        new_zoom = max(MIN_ZOOM, min(MAX_ZOOM, self.zoom + int(zoom_delta)))
        if new_zoom == self.zoom:
            return self.zoom
        fixed_lat, fixed_lon = self.screen_to_geo(px, py, width, height)
        self.zoom = new_zoom
        self._auto_zoom = False
        tx, ty = latlon_to_tile(fixed_lat, fixed_lon, self.zoom)
        cx = tx - (px - width / 2.0) / TILE_SIZE
        cy = ty - (py - height / 2.0) / TILE_SIZE
        self.center_lat, self.center_lon = tile_to_latlon(cx, cy, self.zoom)
        return self.zoom

    def follow(self, lat, lon):
        if self.auto_center and (lat != 0 or lon != 0):
            self.center_lat, self.center_lon = lat, lon
            if self._auto_zoom:
                self.zoom = DEFAULT_ZOOM
                self._auto_zoom = False

    def follow_fallback(self, lat, lon):
        """Center on desktop location only until vehicle GPS becomes available."""
        if self.auto_center and (lat != 0 or lon != 0):
            self.center_lat, self.center_lon = lat, lon
            if self._auto_zoom:
                self.zoom = DEFAULT_ZOOM
                self._auto_zoom = False

    def recenter(self, lat=None, lon=None):
        if lat is not None and lon is not None and (lat != 0 or lon != 0):
            self.center_lat, self.center_lon = lat, lon
        self.auto_center = True


class ManualControlState:
    """Manual axes and MAVLink streaming policy, independent of any UI toolkit."""

    def __init__(self, controller):
        self.controller = controller
        self.throttle = 0
        self.yaw = 0
        self.input_source = "KEYBOARD"
        self.neutral_ticks = 0
        self._lock = threading.Lock()

    @staticmethod
    def _clamp(value):
        return max(-MAX_MANUAL_INPUT, min(MAX_MANUAL_INPUT, int(value)))

    def set_axes(self, throttle, yaw, source):
        with self._lock:
            self.throttle = self._clamp(throttle)
            self.yaw = self._clamp(yaw)
            self.input_source = source
            if self.throttle or self.yaw or source == "KEYBOARD":
                self.neutral_ticks = 5

    def set_throttle(self, value, source="BUTTON PAD"):
        with self._lock:
            self.throttle = self._clamp(value)
            self.input_source = source
            if self.throttle or self.yaw or source == "KEYBOARD":
                self.neutral_ticks = 5

    def set_yaw(self, value, source="BUTTON PAD"):
        with self._lock:
            self.yaw = self._clamp(value)
            self.input_source = source
            if self.throttle or self.yaw or source == "KEYBOARD":
                self.neutral_ticks = 5

    def send_tick(self):
        if not self.controller.running or self.controller.mode_str != "MANUAL":
            return
        with self._lock:
            throttle, yaw = self.throttle, self.yaw
            if throttle or yaw:
                self.neutral_ticks = 5
            elif self.neutral_ticks > 0:
                self.neutral_ticks -= 1
            else:
                return
        self.controller.send_manual_control(throttle, yaw)

    def zero(self, source=None, send_neutral=True):
        with self._lock:
            self.throttle = self.yaw = 0
            if source is not None:
                self.input_source = source
            self.neutral_ticks = 5 if send_neutral else 0


class KeyboardAxisState:
    """Convert abstract arrow/WASD key names to manual axis values."""

    _AXIS_KEYS = {
        "UP": ("throttle", 1000), "W": ("throttle", 1000),
        "DOWN": ("throttle", -1000), "S": ("throttle", -1000),
        "LEFT": ("yaw", -1000), "A": ("yaw", -1000),
        "RIGHT": ("yaw", 1000), "D": ("yaw", 1000),
    }

    def __init__(self):
        self.pressed = set()

    def update(self, key, is_pressed):
        key = key.upper()
        if key not in self._AXIS_KEYS:
            return None
        if is_pressed:
            self.pressed.add(key)
        else:
            self.pressed.discard(key)
        throttle = self._direction_value(("UP", "W"), ("DOWN", "S"))
        yaw = self._direction_value(("RIGHT", "D"), ("LEFT", "A"))
        return throttle, yaw

    def clear(self):
        self.pressed.clear()
        return 0, 0

    def _direction_value(self, positive_keys, negative_keys):
        if self.pressed.intersection(positive_keys):
            return 1000
        if self.pressed.intersection(negative_keys):
            return -1000
        return 0


class MissionSession:
    """Application-level facade shared by the Qt UI and future front ends."""

    def __init__(self, controller, viewport=None):
        self.controller = controller
        self.viewport = viewport or MapViewport()
        self.manual = ManualControlState(controller)

    def waypoints_snapshot(self):
        with self.controller._wp_lock:
            return list(self.controller.waypoints)

    def nearest_waypoint(self, px, py, width, height, radius=20):
        nearest, distance = -1, radius
        for index, wp in enumerate(self.waypoints_snapshot()):
            x, y = self.viewport.geo_to_screen(wp["lat"], wp["lon"], width, height)
            candidate = math.hypot(x - px, y - py)
            if candidate < distance:
                nearest, distance = index, candidate
        return nearest

    def add_waypoint_at(self, px, py, width, height):
        lat, lon = self.viewport.screen_to_geo(px, py, width, height)
        self.controller.add_waypoint(lat, lon)

    def move_waypoint_to(self, index, px, py, width, height):
        lat, lon = self.viewport.screen_to_geo(px, py, width, height)
        self.controller.update_waypoint_pos(index, lat, lon)

    def remove_waypoint(self, index):
        self.controller.remove_waypoint(index)

    def upload_mission(self): self.controller.upload_mission_async()
    def start_mission(self): self.controller.start_mission_async()
    def emergency_stop(self):
        self.manual.zero(source="BUTTON PAD", send_neutral=False)
        self.controller.emergency_stop()
    def arm(self): self.controller.set_arm(True)
    def disarm(self): self.controller.set_arm(False)
    def set_manual_mode(self): self.controller.set_mode("MANUAL")
    def clear_waypoints(self): self.controller.clear_waypoints()
    def load_waypoints(self, path): return self.controller.load_waypoints_file(path)
    def save_waypoints(self, path): return self.controller.save_waypoints_file(path)
