"""Cached Esri raster tiles with bundled FIU imagery, served to MapLibre."""
import json
import math
import threading
import time
from io import BytesIO
from pathlib import Path
import requests
from PIL import Image
from heron.mission.mission_logic import TILE_SIZE

ESRI_TILE_URL = "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"


class RasterTiles:
    def __init__(self, fiu_root, cache_dir):
        self.fiu_root = Path(fiu_root)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._fiu_lock = threading.Lock()
        self._fiu_images = {}
        self._fiu_bounds = {}
        self.last_error = ""
        self._downloads = threading.BoundedSemaphore(6)

    def png(self, source, z, x, y):
        if source not in {"esri", "fiu"} or not 0 <= z <= 22 or not (0 <= x < 2**z and 0 <= y < 2**z):
            raise ValueError("Invalid tile")
        if source == "fiu":
            image = self._fetch_fiu_tile(z, x, y)
            if image is None:
                raise OSError("FIU imagery is unavailable")
        else:
            path = self.cache_dir / f"esri_imagery_z{z}_x{x}_y{y}.jpg"
            image = None
            try:
                with Image.open(path) as cached:
                    image = cached.convert("RGB")
            except (OSError, ValueError):
                pass
            if image is None or time.time() - path.stat().st_mtime > 86400:
                try:
                    with self._downloads:
                        response = requests.get(ESRI_TILE_URL.format(z=z, x=x, y=y), timeout=5,
                                                headers={"User-Agent": "HeronUSV-GCS/2.0"})
                        response.raise_for_status()
                        with Image.open(BytesIO(response.content)) as downloaded:
                            image = downloaded.convert("RGB")
                        # Atomic replacement prevents readers seeing partially written tiles.
                        import tempfile
                        with tempfile.NamedTemporaryFile(dir=self.cache_dir, suffix=".jpg", delete=False) as out:
                            temp = Path(out.name)
                            image.save(out, format="JPEG", quality=92)
                        temp.replace(path)
                    self.last_error = ""
                except (requests.RequestException, OSError, ValueError) as exc:
                    self.last_error = self._brief_error("Esri tiles", exc)
                    if image is None:
                        raise OSError(self.last_error) from exc
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def close(self):
        with self._fiu_lock:
            for image in self._fiu_images.values():
                image.close()
            self._fiu_images.clear()

    def _fetch_fiu_tile(self, z: int, x: int, y: int):
        """Warp the packaged WGS84 GeoTIFF imagery into a Web Mercator tile."""
        with self._fiu_lock:
            return self._render_fiu_tile(z, x, y)

    def _overlay_fiu_tile(self, base_image, z: int, x: int, y: int):
        """Keep Esri visible outside the packaged FIU imagery coverage."""
        overlay = self._fetch_fiu_tile(z, x, y)
        if overlay is None:
            return base_image
        return Image.alpha_composite(base_image.convert("RGBA"), overlay).convert("RGB")

    def _render_fiu_tile(self, z: int, x: int, y: int):
        from PIL import Image

        output = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
        fiu_root = self.fiu_root
        paths = sorted(fiu_root.glob("**/raster_tiles/*.tif"))
        if not paths:
            self.last_error = "FIU basemap GeoTIFFs were not found."
            return None

        tile_count = 1 << z
        lon_west = x / tile_count * 360.0 - 180.0
        lon_east = (x + 1) / tile_count * 360.0 - 180.0
        def tile_lat(ty):
            return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / tile_count))))
        lat_north, lat_south = tile_lat(y), tile_lat(y + 1)
        for path in paths:
            try:
                image = self._fiu_images.get(path)
                if image is None:
                    Image.MAX_IMAGE_PIXELS = None
                    image = Image.open(path)
                    self._fiu_images[path] = image
                valid_bounds = self._fiu_bounds.get(path)
                if valid_bounds is None:
                    metadata_path = path.parent.parent / "metadata_tiles" / f"{path.stem}.geojson"
                    metadata = json.loads(metadata_path.read_text())
                    points = metadata["features"][0]["geometry"]["coordinates"][0]
                    valid_bounds = (
                        min(point[0] for point in points), max(point[0] for point in points),
                        min(point[1] for point in points), max(point[1] for point in points),
                    )
                    self._fiu_bounds[path] = valid_bounds
                tags = image.tag_v2
                tiepoint = tags[33922]
                pixel_scale = tags[33550]
                width, height = image.size
                origin_lon, origin_lat = tiepoint[3], tiepoint[4]
                scale_lon, scale_lat = pixel_scale[0], pixel_scale[1]
                west, east = origin_lon, origin_lon + width * scale_lon
                north, south = origin_lat, origin_lat - height * scale_lat
                valid_west, valid_east, valid_south, valid_north = valid_bounds
                clip_west = max(west, valid_west, lon_west)
                clip_east = min(east, valid_east, lon_east)
                clip_south = max(south, valid_south, lat_south)
                clip_north = min(north, valid_north, lat_north)
                if clip_west >= clip_east or clip_south >= clip_north:
                    continue

                src_box = (
                    max(0, int((clip_west - west) / scale_lon)),
                    max(0, int((north - clip_north) / scale_lat)),
                    min(width, math.ceil((clip_east - west) / scale_lon)),
                    min(height, math.ceil((north - clip_south) / scale_lat)),
                )
                if src_box[2] <= src_box[0] or src_box[3] <= src_box[1]:
                    continue
                crop = image.crop(src_box).convert("RGBA")
                left = round((clip_west - lon_west) / (lon_east - lon_west) * TILE_SIZE)
                right = round((clip_east - lon_west) / (lon_east - lon_west) * TILE_SIZE)
                top = round((lat_north - clip_north) / (lat_north - lat_south) * TILE_SIZE)
                bottom = round((lat_north - clip_south) / (lat_north - lat_south) * TILE_SIZE)
                if right > left and bottom > top:
                    output.alpha_composite(
                        crop.resize((right - left, bottom - top), Image.Resampling.BILINEAR),
                        (left, top),
                    )
            except Exception as exc:
                self.last_error = self._brief_error("FIU basemap", exc)
        return output

    @staticmethod
    def _brief_error(source, exc):
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            return f"{source} returned HTTP {exc.response.status_code}."
        return f"{source} request failed ({type(exc).__name__})."

