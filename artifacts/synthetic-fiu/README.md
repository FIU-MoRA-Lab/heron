# Synthetic FIU lake recording

This fixture contains invented measurements, not observations of FIU water.
It is a development input for a map-first, Kepler-style Explorer with Python
preprocessing and deck.gl playback. The route is not an operational mission plan.

## Contents

- `fiu_synthetic_mission.mcap`: indexed, Zstandard-compressed recording with JSON
  payloads and JSON schemas on every channel.
- `lake.geojson`: shoreline, 2 m inward shoreline buffer, and three 2 m fountain
  exclusion zones, derived from the app's FIU lake geometry.
- `route.geojson`: zigzag survey with parallel passes clipped to the usable lake
  area, alternating directions, and connecting detours around fountain zones.
- `readings_ground_truth.geojson`: exact simulated measurement locations and
  times. For checking alignment only; these locations are not in sensor messages.
- `manifest.json`: rates, counts, units-related field names, clock ground truth,
  route statistics, validation results, and MCAP checksum.
- `preview.svg`: north-up map of the route and individual readings colored by
  dissolved oxygen, with the lake and safety geometry. No heatmap or clustering.
- `preview.png`: raster copy of the SVG for convenient viewing.

The lake and route GeoJSONs are also embedded as MCAP attachments. FIU imagery
stays in the existing app assets rather than being duplicated in this recording.

## Streams

| Topic | Rate | Notes |
| --- | --- | --- |
| `/mavlink/GLOBAL_POSITION_INT` | 5 Hz | Latitude/longitude in degrees × 1e7; velocity in cm/s; heading in centidegrees |
| `/mavlink/GPS_RAW_INT` | 5 Hz | GPS epoch timestamp, 3D fix, 18 satellites |
| `/mavlink/ATTITUDE` | 10 Hz | Synthetic roll/pitch oscillations and path heading in radians |
| `/mavlink/HEARTBEAT` | 1 Hz | Surface boat, armed, active; mode value is illustrative |
| `/sensors/water_quality` | Approximately 1 Hz | Dissolved oxygen in mg/L and temperature in °C |

MAVLink records use the same decoded JSON envelope as `HeronLogger`: `timestamp`,
`timestamp_iso`, `msg_type`, `sys_id`, `comp_id`, and `data`. Messages are produced
with pymavlink. They are JSON-decoded MAVLink data, not raw wire packets.
The water-quality schema is a proposed synthetic schema, not the EXO2 protocol.
Route corners are piecewise linear and attitude turn dynamics are simplified;
this fixture is for visualization and alignment, not vehicle dynamics testing.

The mission lasts about 13 minutes, travels about 624 m at 0.8 m/s,
and contains 17,166 messages. Survey passes are spaced 6 m apart and clipped
to a polygon inset 7 m from the shoreline; connecting detours can extend slightly
outside that inset. All route segments stay inside the app's safe area and at least 5 m from
fountain centers. Water quality has two synthetic spatial patches and small
random noise, including readings below 4 mg/L for testing a simple filter.

## Timing and alignment

MCAP `log_time` and `publish_time` both hold recorder receive time in Unix epoch
nanoseconds. The recorder does not claim to know synchronized sensor publication
time. The water-quality payload preserves a separate `sensor_time_ns`, labeled
`independent_device_boot`. GPS timestamps are epoch based, while flight-controller
`time_boot_ms` begins at 120 seconds at mission start.

The simulated sensor clock follows:

```python
sensor_time_s = 7.5 + mission_elapsed_s * (1 + 40 / 1_000_000)
```

Sensor sampling has ±80 ms jitter and arrives 150–350 ms after measurement. Sensor
records contain no GPS coordinates. The intended first Explorer implementation
can interpolate vehicle position at receive time, with a maximum allowed GPS
gap and an explicit approximate-alignment label. This introduces up to about
0.28 m of delay-related displacement in this fixture. Later Python analysis can
correct the independent clock using known calibration or synchronization evidence.
The simulator's known offset/drift are not evidence that these corrections can
be recovered exactly from real sensor streams.

`readings_ground_truth.geojson` supplies exact positions, timestamps, and delivery
delays for checking either method. The static preview uses those exact positions.
No spatial aggregation, heatmaps, or cluster calculations have been applied.

## Regenerate and inspect

From the repository root:

```sh
PYTHONPATH=backend .venv/bin/python backend/scripts/generate_synthetic_lake_log.py
```

The generator has a fixed random seed and start time. It writes MCAP and GeoJSON
artifacts, the manifest, and SVG preview; the PNG is a convenience render generated
separately. It reads the finished MCAP back with CRC validation, checks channel
counts and indexes, seeks to a five-second sensor interval, verifies attachments,
and checks the recorded GPS samples against the safe area and fountain clearance.

```python
import json
from mcap.reader import make_reader

with open("artifacts/synthetic-fiu/fiu_synthetic_mission.mcap", "rb") as stream:
    reader = make_reader(stream)
    for schema, channel, message in reader.iter_messages(
        topics=["/sensors/water_quality"]
    ):
        reading = json.loads(message.data)
        if reading["data"]["dissolved_oxygen_mg_l"] < 4:
            print(message.log_time, reading["data"])
```
