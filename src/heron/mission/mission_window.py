"""Qt Quick presentation adapter for the waypoint mission-control session."""

from __future__ import annotations

import math
import queue
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Property, QPointF, QRectF, QSize, QUrl, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter, QPen, QPolygonF
from PySide6.QtQuick import QQuickPaintedItem, QQuickView
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtQml import qmlRegisterType
from PySide6.QtWidgets import QApplication, QFileDialog
try:
    from PySide6.QtPositioning import QGeoPositionInfoSource
except ImportError:
    QGeoPositionInfoSource = None

from heron.mission.mission_logic import (
    TILE_SIZE, MapViewport, KeyboardAxisState, latlon_to_tile,
)
from heron.mission.controls import GamepadInputReader, ManualControlStreamer
from heron.mission.mission_core import SAVED_MISSION_FILE, SAMPLE_MISSION_FILE

class ManualKeyboardFilter(QObject):
    """Map arrow/WASD key holds to manual axes while the Manual tab is active."""

    KEYS = {
        Qt.Key.Key_Up: "UP", Qt.Key.Key_W: "W",
        Qt.Key.Key_Down: "DOWN", Qt.Key.Key_S: "S",
        Qt.Key.Key_Left: "LEFT", Qt.Key.Key_A: "A",
        Qt.Key.Key_Right: "RIGHT", Qt.Key.Key_D: "D",
    }

    def __init__(self, backend, view):
        super().__init__(view)
        self.backend, self.view = backend, view
        self.axis_state = KeyboardAxisState()

    def clear(self):
        throttle, yaw = self.axis_state.clear()
        self.backend.setKeyboardAxes(throttle, yaw)

    def eventFilter(self, watched, event):
        if (not self.backend.manual_tab_active or not self.view.isActive()
                or event.type() not in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease)):
            return False
        key = event.key()
        if key not in self.KEYS:
            return False
        if event.isAutoRepeat():
            event.accept()
            return True
        axes = self.axis_state.update(
            self.KEYS[key], event.type() == QEvent.Type.KeyPress
        )
        if axes is None:
            return False
        throttle, yaw = axes
        self.backend.setKeyboardAxes(throttle, yaw)
        event.accept()
        return True


class MapCanvas(QQuickPaintedItem):
    """High-DPI Qt Quick map canvas with antialiased route and marker overlays."""

    zoomChanged = Signal()
    sourceChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # Let Qt render this frequently updated map layer through the scene
        # graph's framebuffer path when the platform's graphics backend allows it.
        self.setRenderTarget(QQuickPaintedItem.RenderTarget.FramebufferObject)
        self.session = self.tiles = None
        self.viewport = MapViewport()
        self._tile_images = {}
        self._press = None
        self._drag_wp = -1
        self._pan = False
        self._pan_candidate = False
        self._last_pos = None
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton | Qt.MouseButton.MiddleButton)
        self.setAcceptHoverEvents(True)

    def set_sources(self, session, tiles):
        self.session, self.tiles = session, tiles
        self.viewport = session.viewport
        self.sourceChanged.emit()
        self.update()

    @Property(int, notify=zoomChanged)
    def zoomLevel(self):
        return self.viewport.zoom

    @Property(str, notify=sourceChanged)
    def imagerySourceLabel(self):
        if not self.tiles:
            return "SATELLITE IMAGERY"
        return self.tiles.imagery_source_label

    def set_zoom(self, zoom):
        self.viewport.set_zoom(zoom)
        self.zoomChanged.emit()
        self.update()

    def _screen_to_geo(self, px, py):
        return self.viewport.screen_to_geo(px, py, self.width(), self.height())

    def _geo_to_screen(self, lat, lon):
        return self.viewport.geo_to_screen(lat, lon, self.width(), self.height())

    def paint(self, painter: QPainter):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.boundingRect(), QColor("#171717"))
        if not self.session or not self.tiles:
            return
        controller = self.session.controller
        old_zoom = self.viewport.zoom
        self.viewport.follow(controller.lat, controller.lon)
        if self.viewport.zoom != old_zoom:
            self.zoomChanged.emit()

        cx, cy = latlon_to_tile(self.viewport.center_lat, self.viewport.center_lon, self.viewport.zoom)
        min_x = math.floor(cx - self.width() / (2 * TILE_SIZE)) - 1
        max_x = math.floor(cx + self.width() / (2 * TILE_SIZE)) + 1
        min_y = math.floor(cy - self.height() / (2 * TILE_SIZE)) - 1
        max_y = math.floor(cy + self.height() / (2 * TILE_SIZE)) + 1
        for ty in range(min_y, max_y + 1):
            if ty < 0 or ty >= 2 ** self.viewport.zoom:
                continue
            for tx in range(min_x, max_x + 1):
                wrapped = tx % (2 ** self.viewport.zoom)
                tile_image, _, _ = self.tiles.get_tile(self.viewport.zoom, wrapped, ty)
                key = (self.viewport.zoom, wrapped, ty, id(tile_image))
                image = self._tile_images.get(key)
                if image is None:
                    raw = tile_image.tobytes("raw", "RGB")
                    image = QImage(raw, tile_image.width, tile_image.height, tile_image.width * 3,
                                   QImage.Format.Format_RGB888).copy()
                    self._tile_images[key] = image
                x = self.width() / 2 + (tx - cx) * TILE_SIZE
                y = self.height() / 2 + (ty - cy) * TILE_SIZE
                painter.drawImage(QRectF(x, y, TILE_SIZE, TILE_SIZE), image)
        if len(self._tile_images) > 320:
            self._tile_images.clear()
        # A subtle dim layer keeps yellow overlays legible over bright imagery.
        painter.fillRect(self.boundingRect(), QColor(0, 0, 0, 38))

        wps = self.session.waypoints_snapshot()
        points = [self._geo_to_screen(wp["lat"], wp["lon"]) for wp in wps]
        if len(points) > 1:
            painter.setPen(QPen(QColor("#ffd84a"), 3, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in points]))
        for index, ((x, y), wp) in enumerate(zip(points, wps)):
            active = wp["id"] == controller.current_wp_seq
            marker = QRectF(x - 14, y - 14, 28, 28)
            painter.setPen(QPen(QColor(0, 0, 0, 105), 5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(marker)
            painter.setPen(QPen(QColor("#fff0a0" if active else "#ffd84a"), 1.5))
            painter.setBrush(QColor("#66510a" if active else "#28230f"))
            painter.drawEllipse(marker)
            painter.setPen(QPen(Qt.PenStyle.NoPen))
            painter.setBrush(QColor("#ffe778" if active else "#ffd84a"))
            painter.drawEllipse(QRectF(x - 10, y - 10, 20, 20))
            painter.setPen(QColor("#241d08"))
            painter.setFont(QFont("Sans Serif", 9, QFont.Weight.Bold))
            painter.drawText(QRectF(x - 10, y - 10, 20, 20),
                             Qt.AlignmentFlag.AlignCenter, str(index + 1))
        if controller.lat and controller.lon:
            x, y = self._geo_to_screen(controller.lat, controller.lon)
            heading = math.radians(controller.heading)
            forward = QPointF(math.sin(heading), -math.cos(heading))
            right = QPointF(math.cos(heading), math.sin(heading))
            center = QPointF(x, y)
            arrow = QPolygonF([
                center + forward * 19,
                center + forward * 1 + right * 9,
                center - forward * 10 + right * 6,
                center - forward * 6,
                center - forward * 10 - right * 6,
                center + forward * 1 - right * 9,
            ])
            painter.setPen(QPen(QColor(0, 0, 0, 190), 4))
            painter.setBrush(QColor("#ffd84a"))
            painter.drawPolygon(arrow)
            painter.setPen(QPen(QColor("#241d08"), 1.3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolygon(arrow)
        if self.tiles.last_error:
            message = "MAP DATA UNAVAILABLE · " + self.tiles.last_error
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#e02a1717"))
            status_rect = QRectF(16, 58, max(320, self.width() - 32), 34)
            painter.drawRoundedRect(status_rect, 7, 7)
            painter.setPen(QColor("#ffd1d1"))
            painter.setFont(QFont("Sans Serif", 9, QFont.Weight.Bold))
            painter.drawText(status_rect.adjusted(12, 0, -12, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             message)

    def _nearest_waypoint(self, x, y):
        return self.session.nearest_waypoint(x, y, self.width(), self.height())

    def mousePressEvent(self, event):
        p = event.position()
        self._press = (p.x(), p.y())
        self._last_pos = self._press
        self._drag_wp = self._nearest_waypoint(*self._press)
        self._pan = event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton)
        self._pan_candidate = (
            event.button() == Qt.MouseButton.LeftButton and self._drag_wp < 0
        )
        if event.button() == Qt.MouseButton.RightButton and self._drag_wp >= 0:
            self.session.remove_waypoint(self._drag_wp)
            self._press = None
        event.accept()

    def mouseMoveEvent(self, event):
        p = event.position()
        x, y = p.x(), p.y()
        if (self._pan_candidate and self._press
                and math.hypot(x - self._press[0], y - self._press[1]) > 5):
            self._pan = True
            self._pan_candidate = False
        if self._last_pos and self._pan:
            dx, dy = x - self._last_pos[0], y - self._last_pos[1]
            self.viewport.pan_pixels(dx, dy)
        elif self._press and self._drag_wp >= 0 and math.hypot(x - self._press[0], y - self._press[1]) > 2:
            self.session.move_waypoint_to(self._drag_wp, x, y, self.width(), self.height())
        self._last_pos = (x, y)
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._press and self._drag_wp < 0 and not self._pan:
            x, y = self._press
            if math.hypot(event.position().x() - x, event.position().y() - y) < 5:
                self.session.add_waypoint_at(x, y, self.width(), self.height())
        self._press = self._last_pos = None
        self._drag_wp, self._pan, self._pan_candidate = -1, False, False
        self.update()
        event.accept()

    def wheelEvent(self, event):
        pos = event.position()
        old_zoom = self.viewport.zoom
        new_zoom = self.viewport.zoom_at(
            1 if event.angleDelta().y() > 0 else -1,
            pos.x(), pos.y(), self.width(), self.height()
        )
        if new_zoom != old_zoom:
            self.zoomChanged.emit()
            self.update()
        event.accept()

    @Slot()
    def recenter(self):
        controller = self.session.controller
        if controller.lat != 0 or controller.lon != 0:
            self.viewport.recenter(controller.lat, controller.lon)
        else:
            # Keep the desktop-location fallback centered and resume following
            # the vehicle automatically once its GPS fix becomes available.
            self.viewport.auto_center = True
        self.update()


class ComputerLocation(QObject):
    """Use the desktop OS location provider as a fallback map center."""

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.source = None
        if QGeoPositionInfoSource is None:
            return
        try:
            self.source = QGeoPositionInfoSource.createDefaultSource(self)
            if self.source is None:
                return
            self.source.positionUpdated.connect(self._position_updated)
            self.source.startUpdates()
            last_position = self.source.lastKnownPosition()
            if last_position and last_position.isValid():
                self._position_updated(last_position)
        except Exception:
            self.source = None

    @Slot(object)
    def _position_updated(self, position):
        if not position or not position.isValid():
            return
        coordinate = position.coordinate()
        if not coordinate.isValid():
            return
        controller = self.session.controller
        # Vehicle GPS always wins; the system location is only a fallback.
        if controller.lat != 0 or controller.lon != 0:
            return
        viewport = self.session.viewport
        if viewport.auto_center:
            old_zoom = viewport.zoom
            viewport.follow_fallback(coordinate.latitude(), coordinate.longitude())
            if viewport.zoom != old_zoom and self.parent() is not None:
                canvas = self.parent().findChild(MapCanvas, "mapCanvas")
                if canvas is not None:
                    canvas.zoomChanged.emit()
                    canvas.update()


class Backend(QObject):
    telemetryChanged = Signal()
    activityChanged = Signal()
    manualChanged = Signal()

    def __init__(self, session, canvas):
        super().__init__()
        self.session, self.controller, self.canvas = session, session.controller, canvas
        self._log_text = ""
        self._last_telemetry_state = None
        self.manual_tab_active = False
        self.keyboard_filter = None
        self._input_queue = queue.Queue(maxsize=1)
        self._gamepad_worker = GamepadInputReader(self._input_queue)
        self._gamepad_worker.start()
        self._input_timer = QTimer(self)
        self._input_timer.timeout.connect(self._refresh_input)
        self._input_timer.start(20)
        self._map_timer = QTimer(self)
        self._map_timer.timeout.connect(self._refresh_map)
        self._map_timer.start(200)
        self._manual_streamer = ManualControlStreamer(session)
        self._manual_streamer.start()

    @Property(str, notify=telemetryChanged)
    def connectionLabel(self):
        return "ONLINE" if self.controller.connected else "CONNECTING"

    @Property(str, notify=telemetryChanged)
    def modeLabel(self): return self.controller.mode_str

    @Property(bool, notify=telemetryChanged)
    def armed(self): return self.controller.is_armed

    @Property(str, notify=telemetryChanged)
    def batteryLabel(self):
        battery_pct = self.controller.battery_pct
        return f"BATTERY {battery_pct}%" if 0 <= battery_pct <= 100 else "BATTERY --"

    @Property(str, notify=telemetryChanged)
    def telemetryLabel(self):
        c = self.controller
        gps = f"{c.lat:.6f}, {c.lon:.6f}" if c.lat else "Waiting for GPS"
        return f"{c.groundspeed:.1f} m/s   ·   HDG {c.heading:.0f}°   ·   {gps}"

    @Property(str, notify=telemetryChanged)
    def missionLabel(self):
        with self.controller._wp_lock:
            count = len(self.controller.waypoints)
        return f"{count} waypoints  ·  {self.controller.satellites} satellites  ·  {self.controller.battery_v:.1f} V"

    @Property(bool, notify=telemetryChanged)
    def missionReady(self):
        with self.controller._wp_lock:
            return bool(self.controller.waypoints) and not self.controller.is_starting_mission

    @Property(bool, notify=telemetryChanged)
    def clearAvailable(self):
        return not self.controller.is_armed and not self.controller.mission_started

    @Property(str, notify=telemetryChanged)
    def statusLabel(self):
        return self.controller.last_status_msg or "Click map to add a waypoint · Drag markers to edit · Right-click to remove"

    @Property(str, notify=activityChanged)
    def logText(self): return self._log_text

    @Property(str, notify=manualChanged)
    def manualLabel(self):
        return f"THROTTLE {self.manualThrottle:+d}     YAW {self.manualYaw:+d}"

    @Property(str, notify=manualChanged)
    def inputSource(self): return self.session.manual.input_source

    @Property(int, notify=manualChanged)
    def manualThrottle(self): return self.session.manual.throttle

    @Property(int, notify=manualChanged)
    def manualYaw(self): return self.session.manual.yaw

    @Property(str, constant=True)
    def monospaceFamily(self):
        database = QFontDatabase()
        installed = set(database.families())
        for family in ("SF Mono", "Menlo", "Monaco", "DejaVu Sans Mono",
                       "Consolas", "Liberation Mono", "Courier New"):
            if family in installed:
                return family
        return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()

    @Slot()
    def refresh(self):
        controller = self.controller
        with controller._wp_lock:
            mission_count = len(controller.waypoints)
        telemetry_state = (
            controller.connected, controller.is_armed, controller.mission_uploaded,
            controller.mission_started,
            controller.mode_str, controller.lat, controller.lon,
            controller.groundspeed, controller.heading, mission_count,
            controller.satellites, controller.battery_v, controller.battery_pct,
            controller.last_status_msg,
        )
        if telemetry_state != self._last_telemetry_state:
            self._last_telemetry_state = telemetry_state
            self.telemetryChanged.emit()

        if self.manual_tab_active and controller.mode_str != "MANUAL":
            self.manual_tab_active = False
            if self.keyboard_filter:
                self.keyboard_filter.clear()

        messages = list(controller._log_queue)
        if messages:
            del controller._log_queue[:len(messages)]
            self._log_text = "\n".join((self._log_text.splitlines() + messages)[-100:])
            self.activityChanged.emit()

    @Slot()
    def _refresh_input(self):
        while True:
            try:
                axes = self._input_queue.get_nowait()
            except queue.Empty:
                break
            self.session.manual.set_axes(*axes)
            self.manualChanged.emit()

    @Slot()
    def _refresh_map(self):
        if self.canvas:
            self.canvas.update()

    @Slot()
    def upload(self): self.session.upload_mission()
    @Slot()
    def startMission(self): self.session.start_mission()
    @Slot()
    def stop(self):
        self.session.emergency_stop()
        self.manualChanged.emit()
        self.telemetryChanged.emit()

    @Slot()
    def shutdown(self):
        if getattr(self, "_shutdown", False):
            return
        self._shutdown = True
        self._input_timer.stop()
        self._map_timer.stop()
        self._manual_streamer.stop()
        self.session.manual.zero(send_neutral=False)
        self.controller.running = False
        self._gamepad_worker.running = False

    @Slot(int)
    def setManualThrottle(self, value):
        self.session.manual.set_throttle(value)
        self.manualChanged.emit()

    @Slot(int)
    def setManualYaw(self, value):
        self.session.manual.set_yaw(value)
        self.manualChanged.emit()

    @Slot(int, int)
    def setKeyboardAxes(self, throttle, yaw):
        self.session.manual.set_axes(throttle, yaw, "KEYBOARD")
        self.manualChanged.emit()

    @Slot(bool)
    def setManualTab(self, active):
        self.manual_tab_active = active
        if not active and self.keyboard_filter:
            self.keyboard_filter.clear()
        if not active and self.session.manual.input_source == "KEYBOARD":
            self.session.manual.set_axes(0, 0, "KEYBOARD")
        self.manualChanged.emit()
    @Slot()
    def arm(self): self.session.arm()
    @Slot()
    def disarm(self): self.session.disarm()
    @Slot()
    def manual(self):
        self.session.set_manual_mode()
        self.manual_tab_active = True
    @Slot()
    def clear(self): self.session.clear_waypoints()
    @Slot()
    def load(self):
        path, _ = QFileDialog.getOpenFileName(None, "Load waypoints", str(SAMPLE_MISSION_FILE), "Waypoints (*.json *.csv)")
        if path: self.session.load_waypoints(path)
    @Slot()
    def save(self):
        path, _ = QFileDialog.getSaveFileName(None, "Save waypoints", str(SAVED_MISSION_FILE), "JSON (*.json);;CSV (*.csv)")
        if path: self.session.save_waypoints(path)
    @Slot()
    def recenter(self): self.canvas.recenter()
    @Slot()
    def zoomIn(self): self.canvas.set_zoom(self.canvas.zoomLevel + 1)
    @Slot()
    def zoomOut(self): self.canvas.set_zoom(self.canvas.zoomLevel - 1)


def run_qt(session, tile_engine):
    # The native macOS style ignores custom Button background/content items.
    # Basic honors the application-defined grayscale controls on every OS.
    QQuickStyle.setStyle("Basic")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Heron Ground Station")
    app.setDesktopFileName("heron-ground-station")
    qmlRegisterType(MapCanvas, "Heron", 1, 0, "MapCanvas")
    view = QQuickView()
    view.setTitle("Heron · Ground Station")
    view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
    view.setMinimumSize(QSize(1120, 720))
    view.setColor(QColor("#0b111b"))
    backend = Backend(session, None)
    computer_location = None
    keyboard_filter = None
    refresh = None
    try:
        # Pass the QObject as an initial root property. This keeps the Python
        # backend attached to this QML instance instead of relying on an
        # unqualified context lookup during binding evaluation.
        view.setInitialProperties({"heron": backend})
        view.setSource(QUrl.fromLocalFile(str(Path(__file__).with_name("mission.qml"))))
        if view.status() == QQuickView.Status.Error:
            details = "\n".join(error.toString() for error in view.errors())
            raise RuntimeError(f"Could not load the Qt Quick mission interface:\n{details}")
        canvas = view.rootObject().findChild(MapCanvas, "mapCanvas")
        if canvas is None:
            raise RuntimeError("Qt Quick interface did not create its map canvas")
        backend.canvas = canvas
        canvas.set_sources(session, tile_engine)
        computer_location = ComputerLocation(session, view)
        keyboard_filter = ManualKeyboardFilter(backend, view)
        backend.keyboard_filter = keyboard_filter
        app.installEventFilter(keyboard_filter)
        view.show()
        refresh = QTimer(view)
        refresh.timeout.connect(backend.refresh)
        refresh.start(100)
        app.exec()
    finally:
        if refresh:
            refresh.stop()
        if keyboard_filter:
            app.removeEventFilter(keyboard_filter)
        backend.shutdown()
        if computer_location and computer_location.source:
            computer_location.source.stopUpdates()
        view.close()
