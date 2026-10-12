"""Lake safety geometry independent of the desktop renderer."""
import json
import math
from pathlib import Path


class LakeSafetyGeometry:
    def __init__(self):
        self._lake_outline, self._fountain_centers, self.safety_padding_m = self._load_lake_overlay()

    @staticmethod
    def _load_lake_overlay():
        overlay_dir = Path(__file__).with_name("overlays")
        try:
            lake_data = json.loads((overlay_dir / "fiu_lake.geojson").read_text())
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            return [], [], 2.0
        shoreline, fountains = [], []
        for feature in lake_data.get("features", []):
            properties = feature.get("properties") or {}
            geometry = feature.get("geometry") or {}
            role = properties.get("role")
            coordinates = geometry.get("coordinates", [])
            try:
                if role == "shoreline" and geometry.get("type") == "Polygon":
                    shoreline = [(float(point[1]), float(point[0])) for point in coordinates[0]]
                elif role in ("fountain", "fountains"):
                    points = coordinates if geometry.get("type") == "MultiPoint" else [coordinates]
                    fountains.extend((float(point[1]), float(point[0])) for point in points)
            except (IndexError, TypeError, ValueError):
                continue
        try:
            default_padding = max(0.0, min(10.0, float(lake_data.get("padding_m", 2.0))))
        except (TypeError, ValueError):
            default_padding = 2.0
        return shoreline, fountains, default_padding

    @staticmethod
    def _local_xy(lat, lon, origin_lat, origin_lon):
        earth_radius = 6_371_008.8
        scale_x = math.cos(math.radians(origin_lat))
        return (
            earth_radius * math.radians(lon - origin_lon) * scale_x,
            earth_radius * math.radians(lat - origin_lat),
        )

    @staticmethod
    def _from_local_xy(x, y, origin_lat, origin_lon):
        earth_radius = 6_371_008.8
        lon = origin_lon + math.degrees(x / (earth_radius * math.cos(math.radians(origin_lat))))
        lat = origin_lat + math.degrees(y / earth_radius)
        return lat, lon

    def _inset_shoreline(self, padding=None):
        points = list(self._lake_outline)
        if len(points) > 1 and points[0] == points[-1]:
            points.pop()
        if len(points) < 3:
            return []
        padding = self.safety_padding_m if padding is None else max(0.0, float(padding))
        if padding <= 0:
            return points + [points[0]]
        origin_lat = sum(point[0] for point in points) / len(points)
        origin_lon = sum(point[1] for point in points) / len(points)
        xy = [self._local_xy(lat, lon, origin_lat, origin_lon) for lat, lon in points]
        signed_area = sum(
            xy[i][0] * xy[(i + 1) % len(xy)][1] - xy[(i + 1) % len(xy)][0] * xy[i][1]
            for i in range(len(xy))
        ) / 2
        if abs(signed_area) < 1e-4:
            return []
        orientation = 1 if signed_area > 0 else -1
        normals = []
        for i, point in enumerate(xy):
            nxt = xy[(i + 1) % len(xy)]
            dx, dy = nxt[0] - point[0], nxt[1] - point[1]
            length = math.hypot(dx, dy)
            if length < 1e-6:
                return []
            normals.append((-orientation * dy / length, orientation * dx / length))

        inset = []
        for i, vertex in enumerate(xy):
            previous = (i - 1) % len(xy)
            n1, n2 = normals[previous], normals[i]
            p1 = (vertex[0] + n1[0] * padding, vertex[1] + n1[1] * padding)
            p2 = (vertex[0] + n2[0] * padding, vertex[1] + n2[1] * padding)
            d1 = (xy[i][0] - xy[previous][0], xy[i][1] - xy[previous][1])
            nxt = xy[(i + 1) % len(xy)]
            d2 = (nxt[0] - vertex[0], nxt[1] - vertex[1])
            cross = d1[0] * d2[1] - d1[1] * d2[0]
            if abs(cross) < 1e-8:
                nx, ny = n1[0] + n2[0], n1[1] + n2[1]
                norm = math.hypot(nx, ny) or 1.0
                candidate = (vertex[0] + nx / norm * padding, vertex[1] + ny / norm * padding)
            else:
                delta = (p2[0] - p1[0], p2[1] - p1[1])
                factor = (delta[0] * d2[1] - delta[1] * d2[0]) / cross
                candidate = (p1[0] + factor * d1[0], p1[1] + factor * d1[1])
                if math.hypot(candidate[0] - vertex[0], candidate[1] - vertex[1]) > padding * 8 + 1:
                    nx, ny = n1[0] + n2[0], n1[1] + n2[1]
                    norm = math.hypot(nx, ny) or 1.0
                    candidate = (vertex[0] + nx / norm * padding, vertex[1] + ny / norm * padding)
            inset.append(candidate)
        inset_area = sum(
            inset[i][0] * inset[(i + 1) % len(inset)][1]
            - inset[(i + 1) % len(inset)][0] * inset[i][1]
            for i in range(len(inset))
        ) / 2
        if inset_area * orientation <= 0 or abs(inset_area) >= abs(signed_area):
            return []
        geographic = [self._from_local_xy(x, y, origin_lat, origin_lon) for x, y in inset]
        return geographic + [geographic[0]]

    def lake_geojson(self, padding=None):
        """Return the source lake features plus the current derived safety geometry."""
        padding = self.safety_padding_m if padding is None else max(0.0, min(10.0, float(padding)))
        features = []

        def add_feature(name, role, geometry, **properties):
            features.append({
                "type": "Feature",
                "properties": {"name": name, "role": role, **properties},
                "geometry": geometry,
            })

        if len(self._lake_outline) >= 3:
            shoreline = [[lon, lat] for lat, lon in self._lake_outline]
            add_feature("FIU Lake shoreline", "shoreline", {
                "type": "Polygon", "coordinates": [shoreline],
            })
            inset = self._inset_shoreline(padding)
            if inset:
                add_feature("FIU Lake safe area", "safe_area", {
                    "type": "Polygon", "coordinates": [[[lon, lat] for lat, lon in inset]],
                }, padding_m=padding, operation="inward shoreline offset")

        for index, (lat, lon) in enumerate(self._fountain_centers, start=1):
            add_feature(f"Fountain {index} center", "fountain_center", {
                "type": "Point", "coordinates": [lon, lat],
            })
            if padding > 0:
                ring = []
                for segment in range(49):
                    angle = 2 * math.pi * segment / 48
                    circle_lat, circle_lon = self._from_local_xy(
                        math.cos(angle) * padding, math.sin(angle) * padding, lat, lon,
                    )
                    ring.append([circle_lon, circle_lat])
                add_feature(f"Fountain {index} safety radius", "fountain_safety_area", {
                    "type": "Polygon", "coordinates": [ring],
                }, padding_m=padding, units="meters")

        return {
            "type": "FeatureCollection",
            "name": "FIU Lake safety areas",
            "padding_m": padding,
            "units": "meters",
            "features": features,
        }

