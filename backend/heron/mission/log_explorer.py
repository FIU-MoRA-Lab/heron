"""Prepare decoded MCAP readings for the map without touching vehicle state."""
from __future__ import annotations

import bisect
import json
import math
from collections import defaultdict
from pathlib import Path

from mcap.exceptions import McapError
from mcap.reader import make_reader

MAX_MESSAGES = 200_000
MAX_FILE_BYTES = 256 * 1024 * 1024
GPS_GAP_SECONDS = 2.0


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def numeric_fields(value, prefix=""):
    result = {}
    if isinstance(value, dict):
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else key
            if isinstance(item, dict):
                result.update(numeric_fields(item, name))
            elif finite(item) and key not in {"time_boot_ms", "time_usec"}:
                result[name] = item
    return result


def load_recording(filename):
    path = Path(filename)
    if path.suffix.lower() != ".mcap":
        raise ValueError("Choose an MCAP recording (.mcap)")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("This Explorer supports recordings up to 256 MB")
    records, gps = [], defaultdict(list)
    units = {}
    total = 0
    first, last = None, None
    synthetic = False
    try:
        with path.open("rb") as stream:
            reader = make_reader(stream, validate_crcs=True)
            for _, channel, message in reader.iter_messages():
                total += 1
                if total > MAX_MESSAGES:
                    raise ValueError("This Explorer supports up to 200,000 messages per recording")
                first = message.log_time if first is None else min(first, message.log_time)
                last = message.log_time if last is None else max(last, message.log_time)
                if channel.message_encoding != "json":
                    continue
                payload = json.loads(message.data)
                if not isinstance(payload, dict):
                    continue
                data = payload.get("data", payload)
                if not isinstance(data, dict):
                    continue
                synthetic |= payload.get("synthetic") is True or channel.metadata.get("synthetic") == "true"
                msg_type = payload.get("msg_type", channel.topic.rsplit("/", 1)[-1])
                if msg_type in {"GLOBAL_POSITION_INT", "GPS_RAW_INT"}:
                    lat, lon = data.get("lat"), data.get("lon")
                    if not finite(lat) or not finite(lon):
                        continue
                    lat, lon = lat / 1e7, lon / 1e7
                    if not -85.05112878 <= lat <= 85.05112878 or not -180 <= lon <= 180:
                        continue
                    if msg_type == "GPS_RAW_INT" and (not finite(data.get("fix_type")) or data["fix_type"] < 2):
                        continue
                    values = {}
                    if msg_type == "GLOBAL_POSITION_INT" and finite(data.get("vx")) and finite(data.get("vy")):
                        values["speed_m_s"] = math.hypot(data["vx"], data["vy"]) / 100
                    elif finite(data.get("vel")) and data["vel"] != 65535:
                        values["speed_m_s"] = data["vel"] / 100
                    if finite(data.get("hdg")) and data["hdg"] != 65535:
                        values["heading_deg"] = data["hdg"] / 100
                    if finite(data.get("alt")):
                        values["altitude_m"] = data["alt"] / 1000
                    gps[msg_type].append((message.log_time, lon, lat, values, payload.get("sys_id", 1)))
                    continue
                values = numeric_fields(data)
                if values:
                    records.append((message.log_time, channel.topic, values))
                    try:
                        unit_map = json.loads(channel.metadata.get("units", "{}"))
                        if isinstance(unit_map, dict):
                            units[channel.topic] = {k: v for k, v in unit_map.items() if isinstance(v, str)}
                    except (TypeError, ValueError):
                        pass
                    if msg_type == "ATTITUDE":
                        units[channel.topic] = {name: "rad/s" if name.endswith("speed") else "rad" for name in values}
    except (McapError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Could not read this MCAP recording. Check that it is complete and contains valid JSON messages.") from exc
    positions = gps["GLOBAL_POSITION_INT"] or gps["GPS_RAW_INT"]
    if not positions or first is None or last is None:
        raise ValueError("No usable MAVLink GPS positions found in this recording")
    if len({point[4] for point in positions}) > 1:
        raise ValueError("This Explorer currently supports one vehicle per recording")
    # Prefer GLOBAL_POSITION_INT over raw GPS, deduplicating coincident timestamps.
    positions = list({point[0]: point for point in sorted(positions)}.values())
    times = [point[0] for point in positions]
    duration = (last - first) / 1e9

    def aligned(timestamp):
        index = bisect.bisect_left(times, timestamp)
        if index < len(times) and times[index] == timestamp:
            return positions[index][1:3]
        if index == 0 or index == len(times):
            nearest = positions[0 if index == 0 else -1]
            if abs(timestamp - nearest[0]) <= 350_000_000:
                return nearest[1:3]
            return None
        a, b = positions[index - 1], positions[index]
        if b[0] - a[0] > GPS_GAP_SECONDS * 1e9:
            return None
        fraction = (timestamp - a[0]) / (b[0] - a[0])
        return (a[1] + (b[1] - a[1]) * fraction, a[2] + (b[2] - a[2]) * fraction)

    datasets = defaultdict(list)
    skipped = defaultdict(int)
    gps_topic = "/vehicle/position"
    units[gps_topic] = {"speed_m_s": "m/s", "heading_deg": "deg", "altitude_m": "m"}
    for timestamp, lon, lat, values, _ in positions:
        datasets[gps_topic].append({"time": (timestamp - first) / 1e9,
                                    "position": [lon, lat], "values": values})
    for timestamp, topic, values in records:
        point = aligned(timestamp)
        if point is None:
            skipped[topic] += 1
            continue
        datasets[topic].append({"time": (timestamp - first) / 1e9,
                                "position": list(point), "values": values})
    layers = []
    for topic, points in datasets.items():
        ranges = {}
        for point in points:
            for field, value in point["values"].items():
                low, high = ranges.get(field, (value, value))
                ranges[field] = (min(low, value), max(high, value))
        if not ranges:
            continue
        layers.append({"topic": topic, "count": len(points), "points": points,
                       "unaligned": skipped[topic], "fields": [
                           {"name": name, "min": low, "max": high, "unit": units.get(topic, {}).get(name, "")}
                           for name, (low, high) in ranges.items()]})
    layers.sort(key=lambda layer: (not layer["topic"].startswith("/sensors/"), layer["topic"]))
    return {"filename": path.name, "messageCount": total, "duration": duration,
            "startTime": str(first), "synthetic": synthetic,
            "track": [{"time": (p[0] - first) / 1e9, "position": [p[1], p[2]]} for p in positions],
            "datasets": layers, "unaligned": sum(skipped.values()),
            "alignment": "Receive-time GPS interpolation · maximum GPS gap 2 s"}
