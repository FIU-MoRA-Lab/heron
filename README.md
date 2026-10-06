# Heron Ground Control Station

Python tools for MAVLink telemetry, controller teleoperation, and mission planning for the Heron surface vehicle.

## Setup

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

This installs the project and its dependencies. Run commands from the project directory with `uv run`.

## Use

```bash
# Controller teleoperation (keyboard controls are always available)
uv run heron-controller

# Satellite map and mission planning
uv run heron-mission

# Start with the sample route
uv run heron-mission --mission-file src/heron/examples/missions/sample.json

# MAVLink telemetry logger
uv run heron-logger

# Configure/check the GCS network setup without applying changes
uv run heron-network-setup --dry-run
```

Mission control uses a PySide6/Qt Quick interface. The legacy Textual manual controller remains available as `heron-controller`. The tools configure the GCS network automatically by default; use `--no-ip-setup` if the static IP is already configured. See [HERON_SETUP.md](HERON_SETUP.md) for network configuration, options, and controls.
