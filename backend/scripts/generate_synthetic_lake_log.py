"""Generate a reproducible FIU mission fixture, not a deployable mission plan.

Run from the repository root:
  PYTHONPATH=backend .venv/bin/python backend/scripts/generate_synthetic_lake_log.py
"""
import argparse
import bisect
import hashlib
import heapq
import json
import math
import random
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from mcap.reader import make_reader
from mcap.writer import CompressionType, Writer
from pymavlink import mavutil

from heron.mission.safety_geometry import LakeSafetyGeometry
from heron.utils.logger import mavlink_to_dict

ROOT = Path(__file__).resolve().parents[2]
BASE_NS = 1_791_734_400_000_000_000  # 2026-10-11 16:00:00 UTC (12:00 EDT)
SENSOR_OFFSET_S = 7.5
SENSOR_DRIFT_PPM = 40.0
SWEEP_SPACING_M = 6.0


def json_bytes(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":")).encode()


def segment_distance(point, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    fraction = max(0, min(1, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy)
                          / (dx * dx + dy * dy))) if dx or dy else 0
    return math.dist(point, (a[0] + fraction * dx, a[1] + fraction * dy))


def inside(point, ring):
    x, y = point
    result = False
    for a, b in zip(ring, ring[1:] + ring[:1]):
        if (a[1] > y) != (b[1] > y):
            if x < a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1]):
                result = not result
    return result


def build_route(geometry):
    origin = geometry._lake_outline[0]
    xy = lambda latlon: geometry._local_xy(*latlon, *origin)
    shore = [xy(p) for p in geometry._lake_outline[:-1]]
    safe = [xy(p) for p in geometry._inset_shoreline()[:-1]]
    fountains = [xy(p) for p in geometry._fountain_centers]
    perimeter = [xy(p) for p in geometry._inset_shoreline(7.0)[:-1]]
    circles = [[(c[0] + 7 * math.cos(i * math.tau / 48),
                 c[1] + 7 * math.sin(i * math.tau / 48)) for i in range(48)]
               for c in fountains]

    def valid_point(p):
        return inside(p, safe) and all(math.dist(p, c) >= 5 for c in fountains)

    def valid_edge(a, b):
        # Check at <= 0.25 m spacing and maintain 5 m from fountain centers.
        steps = max(1, math.ceil(math.dist(a, b) / 0.25))
        return (all(segment_distance(c, a, b) >= 5 for c in fountains)
                and all(valid_point((a[0] + (b[0] - a[0]) * i / steps,
                                     a[1] + (b[1] - a[1]) * i / steps))
                        for i in range(steps + 1)))

    # Clip horizontal survey passes to the inset shoreline, then split them
    # around the fountains. Alternate direction to form a lawnmower survey.
    sweeps = []
    ymin, ymax = min(p[1] for p in perimeter), max(p[1] for p in perimeter)
    y = ymin + SWEEP_SPACING_M / 2
    row = 0
    while y < ymax:
        crossings = []
        for a, b in zip(perimeter, perimeter[1:] + perimeter[:1]):
            if (a[1] > y) != (b[1] > y):
                crossings.append(a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1]))
        crossings.sort()
        intervals = list(zip(crossings[::2], crossings[1::2]))
        for cx, cy in fountains:
            radius = 5.5  # Additional clearance beyond the 2 m exclusion zone.
            if abs(y - cy) >= radius:
                continue
            half_width = math.sqrt(radius ** 2 - (y - cy) ** 2)
            left, right = cx - half_width, cx + half_width
            split = []
            for start, end in intervals:
                if right <= start or left >= end:
                    split.append((start, end))
                else:
                    if start < left:
                        split.append((start, left))
                    if right < end:
                        split.append((right, end))
            intervals = split
        segments = [[(start, y), (end, y)] for start, end in intervals if end - start >= 1]
        if row % 2:
            segments = [segment[::-1] for segment in segments[::-1]]
        sweeps.extend(segments)
        row += 1
        y += SWEEP_SPACING_M

    nodes = perimeter + [p for circle in circles for p in circle] + [p for segment in sweeps for p in segment]
    graph = [[] for _ in nodes]
    for i, a in enumerate(nodes):
        for j in range(i):
            b = nodes[j]
            if valid_edge(a, b):
                distance = math.dist(a, b)
                graph[i].append((j, distance))
                graph[j].append((i, distance))

    def connect(a, b):
        start, end = nodes.index(a), nodes.index(b)
        queue = [(0, start, [start])]
        seen = set()
        while queue:
            cost, index, path = heapq.heappop(queue)
            if index in seen:
                continue
            seen.add(index)
            if index == end:
                return [nodes[i] for i in path[1:]]
            for nxt, distance in graph[index]:
                if nxt not in seen:
                    heapq.heappush(queue, (cost + distance, nxt, path + [nxt]))
        raise ValueError("No safe connection between route sections")

    route = [sweeps[0][0]]
    for start, end in sweeps:
        if route[-1] != start:
            route.extend(connect(route[-1], start))
        route.append(end)
    assert all(valid_edge(a, b) for a, b in zip(route, route[1:])), "Unsafe route"
    return origin, shore, safe, fountains, route


def generate(output):
    output.mkdir(parents=True, exist_ok=True)
    geometry = LakeSafetyGeometry()
    origin, shore, safe, fountains, route = build_route(geometry)
    distances = [0.0]
    for a, b in zip(route, route[1:]):
        distances.append(distances[-1] + math.dist(a, b))
    speed = 0.8
    duration = distances[-1] / speed

    def position(t):
        distance = min(distances[-1], max(0, t * speed))
        index = min(len(route) - 2, max(0, bisect.bisect_right(distances, distance) - 1))
        a, b = route[index:index + 2]
        fraction = (distance - distances[index]) / (distances[index + 1] - distances[index])
        x, y = a[0] + fraction * (b[0] - a[0]), a[1] + fraction * (b[1] - a[1])
        lat, lon = geometry._from_local_xy(x, y, *origin)
        heading = math.atan2(b[0] - a[0], b[1] - a[1]) % math.tau
        return x, y, lat, lon, heading

    events = []
    encoder = mavutil.mavlink.MAVLink(None, srcSystem=1, srcComponent=1)

    def mav(t, message):
        message.pack(encoder)
        timestamp = BASE_NS + round(t * 1e9)
        events.append((timestamp, f"/mavlink/{message.get_type()}",
                       mavlink_to_dict(message, timestamp_ns=timestamp)))

    for i in range(math.floor(duration * 5) + 1):
        t = i / 5
        _, _, lat, lon, heading = position(t)
        boot_ms = 120_000 + round(t * 1000)
        mav(t, encoder.global_position_int_encode(
            boot_ms, round(lat * 1e7), round(lon * 1e7), 2000, 0,
            round(speed * math.cos(heading) * 100), round(speed * math.sin(heading) * 100),
            0, round(math.degrees(heading) * 100) % 36000))
        mav(t, encoder.gps_raw_int_encode(
            BASE_NS // 1000 + round(t * 1e6), 3, round(lat * 1e7), round(lon * 1e7),
            2000, 80, 110, round(speed * 100), round(math.degrees(heading) * 100) % 36000, 18))
    for i in range(math.floor(duration * 10) + 1):
        t = i / 10
        heading = position(t)[4]
        yaw = (heading + math.pi) % math.tau - math.pi
        mav(t, encoder.attitude_encode(120_000 + round(t * 1000),
            0.015 * math.sin(t / 3), 0.01 * math.sin(t / 4), yaw,
            0.005 * math.cos(t / 3), 0.0025 * math.cos(t / 4), 0))
    for i in range(math.floor(duration) + 1):
        mav(i, encoder.heartbeat_encode(mavutil.mavlink.MAV_TYPE_SURFACE_BOAT,
            mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA, 128, 10,
            mavutil.mavlink.MAV_STATE_ACTIVE))

    rng = random.Random(20261011)
    readings = []
    for i in range(math.floor(duration) + 1):
        t = max(0, i + rng.uniform(-0.08, 0.08))
        if t > duration:
            continue
        x, y, lat, lon, _ = position(t)
        # Two synthetic spatial patches; these are not observations of FIU water.
        patch = sum(amplitude * math.exp(-math.dist((x, y), center) ** 2 / (2 * width ** 2))
                    for center, amplitude, width in
                    [(fountains[0], 5.4, 12), (fountains[2], 3.0, 10)])
        oxygen = round(max(0.2, 8.4 - patch + rng.gauss(0, 0.08)), 3)
        delay = rng.uniform(0.15, 0.35)
        receive_ns = BASE_NS + round((t + delay) * 1e9)
        sensor_ns = round((SENSOR_OFFSET_S + t * (1 + SENSOR_DRIFT_PPM / 1e6)) * 1e9)
        payload = {"timestamp": receive_ns / 1e9,
                   "timestamp_iso": datetime.fromtimestamp(receive_ns / 1e9, timezone.utc).isoformat(),
                   "sensor_id": "synthetic_exo2_like", "sequence": i,
                   "sensor_time_ns": sensor_ns, "sensor_clock": "independent_device_boot",
                   "data": {"dissolved_oxygen_mg_l": oxygen,
                            "temperature_c": round(27 + 0.4 * math.sin(t / 120), 3)},
                   "quality": "valid", "synthetic": True}
        events.append((receive_ns, "/sensors/water_quality", payload))
        readings.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                         "properties": {**payload, "measurement_elapsed_s": t,
                                        "true_measurement_time_ns": BASE_NS + round(t * 1e9),
                                        "delivery_delay_s": delay}})
    events.sort(key=lambda event: event[0])
    coordinates = [[geometry._from_local_xy(*point, *origin)[1],
                    geometry._from_local_xy(*point, *origin)[0]] for point in route]
    route_geojson = {"type": "FeatureCollection", "features": [{"type": "Feature",
                     "properties": {"synthetic": True, "speed_m_s": speed,
                                    "pattern": "zigzag_survey", "sweep_spacing_m": SWEEP_SPACING_M},
                     "geometry": {"type": "LineString", "coordinates": coordinates}}]}
    lake = geometry.lake_geojson()
    truth = {"type": "FeatureCollection", "name": "Synthetic ground truth, not aligned observations",
             "features": readings}
    manifest = {"synthetic": True, "seed": 20261011, "start_time_utc":
                datetime.fromtimestamp(BASE_NS / 1e9, timezone.utc).isoformat(),
                "duration_s": duration, "recording_duration_s": (events[-1][0] - BASE_NS) / 1e9,
                "route_length_m": distances[-1], "speed_m_s": speed,
                "rates_hz": {"GLOBAL_POSITION_INT": 5, "GPS_RAW_INT": 5, "ATTITUDE": 10,
                             "HEARTBEAT": 1, "water_quality": 1},
                "topic_counts": dict(Counter(topic for _, topic, _ in events)),
                "clock_ground_truth": {"sensor_offset_at_start_s": SENSOR_OFFSET_S,
                    "sensor_drift_ppm": SENSOR_DRIFT_PPM, "delivery_delay_range_s": [0.15, 0.35]},
                "oxygen_range_mg_l": [min(p["properties"]["data"]["dissolved_oxygen_mg_l"] for p in readings),
                                      max(p["properties"]["data"]["dissolved_oxygen_mg_l"] for p in readings)],
                "shore_padding_m": geometry.safety_padding_m, "route_inset_m": 7,
                "route_pattern": "zigzag_survey", "sweep_spacing_m": SWEEP_SPACING_M,
                "sensor_position": "Not recorded; derive by clock alignment and GPS interpolation",
                "mcap_timestamps": "log_time and publish_time are receive time; source clocks remain in payload"}
    path = output / "fiu_synthetic_mission.mcap"
    with path.open("wb") as stream:
        writer = Writer(stream, compression=CompressionType.ZSTD, chunk_size=256 * 1024)
        writer.start(profile="", library="heron-synthetic/1")
        writer.add_metadata("heron.synthetic", {key: json.dumps(value) for key, value in manifest.items()})
        channels = {}
        for topic in manifest["topic_counts"]:
            sample = next(value for _, candidate, value in events if candidate == topic)
            schema = {"$schema": "http://json-schema.org/draft-07/schema#", "title": topic,
                      "type": "object", "required": list(sample), "properties": {}}
            def properties(value):
                if isinstance(value, dict):
                    return {"type": "object", "properties": {k: properties(v) for k, v in value.items()},
                            "required": list(value)}
                return {"type": "boolean" if isinstance(value, bool) else
                        "integer" if isinstance(value, int) else "number" if isinstance(value, float) else "string"}
            schema["properties"] = {key: properties(value) for key, value in sample.items()}
            schema_id = writer.register_schema(name=topic.strip("/").replace("/", "."),
                                              encoding="jsonschema", data=json_bytes(schema))
            units = {"dissolved_oxygen_mg_l": "mg/L", "temperature_c": "degC"} if topic.startswith("/sensors") else {}
            channels[topic] = writer.register_channel(topic, "json", schema_id,
                                  metadata={"synthetic": "true", "units": json.dumps(units)})
        sequences = Counter()
        for timestamp, topic, payload in events:
            writer.add_message(channels[topic], timestamp, json_bytes(payload), timestamp, sequences[topic])
            sequences[topic] += 1
        for name, value in [("lake.geojson", lake), ("route.geojson", route_geojson)]:
            writer.add_attachment(BASE_NS, BASE_NS, name, "application/geo+json", json_bytes(value))
        writer.finish()

    # Validate the actual artifact: CRCs, indexes, JSON, ordering, GPS geometry, and seeking.
    minimum_shore, minimum_fountain = math.inf, math.inf
    counts = Counter()
    previous = -1
    with path.open("rb") as stream:
        reader = make_reader(stream, validate_crcs=True)
        summary = reader.get_summary()
        assert summary and summary.chunk_indexes
        assert summary.statistics.message_count == len(events)
        for schema, channel, message in reader.iter_messages():
            assert schema and schema.encoding == "jsonschema"
            assert message.log_time >= previous
            previous = message.log_time
            value = json.loads(message.data)
            counts[channel.topic] += 1
            if channel.topic == "/mavlink/GLOBAL_POSITION_INT":
                point = geometry._local_xy(value["data"]["lat"] / 1e7, value["data"]["lon"] / 1e7, *origin)
                assert inside(point, safe)
                minimum_shore = min(minimum_shore, *(segment_distance(point, a, b)
                    for a, b in zip(shore, shore[1:] + shore[:1])))
                minimum_fountain = min(minimum_fountain, *(math.dist(point, c) for c in fountains))
            elif channel.topic.startswith("/sensors"):
                assert "lat" not in value and "lon" not in value
        assert dict(counts) == manifest["topic_counts"]
        assert len(list(reader.iter_attachments())) == 2
        middle = BASE_NS + round(duration / 2 * 1e9)
        selected = list(reader.iter_messages(topics=["/sensors/water_quality"],
                                             start_time=middle, end_time=middle + 5_000_000_000))
        assert selected and all(middle <= msg.log_time < middle + 5_000_000_000 for _, _, msg in selected)
    assert minimum_shore >= geometry.safety_padding_m and minimum_fountain >= 5
    manifest.update(minimum_gps_shore_distance_m=minimum_shore,
                    minimum_gps_fountain_distance_m=minimum_fountain,
                    mcap_bytes=path.stat().st_size,
                    mcap_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    validation="CRC, indexed seek, counts, JSON, attachments, safety geometry passed")
    for name, value in [("manifest.json", manifest), ("lake.geojson", lake),
                        ("route.geojson", route_geojson), ("readings_ground_truth.geojson", truth)]:
        (output / name).write_text(json.dumps(value, indent=2) + "\n")
    preview(output, shore, safe, fountains, route, readings, geometry, origin)
    print(json.dumps(manifest, indent=2))


def preview(output, shore, safe, fountains, route, readings, geometry, origin):
    xmin, xmax = min(p[0] for p in shore), max(p[0] for p in shore)
    ymin, ymax = min(p[1] for p in shore), max(p[1] for p in shore)
    scale = min(680 / (xmax - xmin), 680 / (ymax - ymin))
    def screen(p):
        return 110 + (p[0] - xmin) * scale, 110 + (ymax - p[1]) * scale
    def points(ring):
        return " ".join(f"{x:.2f},{y:.2f}" for x, y in map(screen, ring))
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="960" height="900" viewBox="0 0 960 900">',
           '<rect width="960" height="900" fill="#101820"/>',
           '<g font-family="Arial,sans-serif" fill="#e5edf5">',
           '<text x="40" y="44" font-size="25">FIU lake · synthetic zigzag survey</text>',
           '<text x="40" y="73" font-size="15">Ground-truth reading positions · not real lake measurements</text>',
           f'<polygon points="{points(shore)}" fill="#223b49" stroke="#75bdd0" stroke-width="2"/>',
           f'<polygon points="{points(safe)}" fill="none" stroke="#5bccaa" stroke-dasharray="6 5"/>',
           f'<polyline points="{points(route)}" fill="none" stroke="#8fa4b6" stroke-width="1.5"/>']
    for center in fountains:
        x, y = screen(center)
        svg.append(f'<circle cx="{x}" cy="{y}" r="{2 * scale}" fill="#d77a45" fill-opacity="0.5" stroke="#ef9c56"/>')
    for reading in readings:
        lon, lat = reading["geometry"]["coordinates"]
        x, y = screen(geometry._local_xy(lat, lon, *origin))
        oxygen = reading["properties"]["data"]["dissolved_oxygen_mg_l"]
        fraction = max(0, min(1, (oxygen - 2) / 7))
        color = f"rgb({round(240 - 180 * fraction)},{round(85 + 110 * fraction)},{round(60 + 170 * fraction)})"
        svg.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.5" fill="{color}"><title>Oxygen {oxygen} mg/L</title></circle>')
    for i in range(8):
        fraction = i / 7
        color = f"rgb({round(240 - 180 * fraction)},{round(85 + 110 * fraction)},{round(60 + 170 * fraction)})"
        svg.append(f'<rect x="{40 + i * 42}" y="814" width="42" height="14" fill="{color}"/>')
    svg.extend(['<text x="40" y="805" font-size="15">Dissolved oxygen (mg/L)</text>',
                '<text x="40" y="850" font-size="14">2</text><text x="365" y="850" font-size="14">9</text>',
                '<text x="435" y="817" font-size="14">Dashed green: safe-area boundary</text>',
                '<text x="435" y="842" font-size="14">Orange: fountain exclusion zones (2 m)</text>',
                '<text x="40" y="880" font-size="13">Local metric projection · north up · no imagery required</text>',
                '</g></svg>'])
    (output / "preview.svg").write_text("\n".join(svg) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "synthetic-fiu")
    generate(parser.parse_args().output)
