# Heron Ground Control Station

Python tools for MAVLink telemetry, controller teleoperation, and mission planning for the Heron surface vehicle.

## Repository layout

```text
backend/
  heron/             Python: MAVLink, controls, maps, MCAP logging, emergency TUI
  tests/             Python integration tests
  requirements.txt   Runtime dependencies bundled with the desktop app
desktop/
  ui/                SolidJS interface and MapLibre visualization
  src-tauri/         Native desktop shell and Python process management
  scripts/           Build and resource packaging helpers
FIU-basemap/         Local satellite imagery and metadata
pyproject.toml       Python project configuration; run uv from this directory
uv.lock              Python dependency lockfile
```

See [backend/README.md](backend/README.md) for Python code and
[desktop/README.md](desktop/README.md) for the desktop app.

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
uv run heron-mission --mission-file backend/heron/examples/missions/sample.json

# MAVLink telemetry logger (MCAP)
uv run heron-logger

# Configure/check the GCS network setup without applying changes
uv run heron-network-setup --dry-run
```

Mission control uses a Tauri desktop app with SolidJS and MapLibre. MAVLink and
manual control remain in Python. Set up the GUI with:

```bash
cd desktop
npm ci
npm run desktop
```

Development also requires Node.js, Rust, and the platform's Tauri dependencies.
See [desktop/README.md](desktop/README.md) for setup, packaging, offline FIU
imagery, and architecture. The app ships the FIU rasters and Python source;
users install Python and uv separately.

## Rebuild the desktop app

`uv run heron-mission` launches the existing desktop build when one is
available; it does not rebuild it after source changes. Rebuild after changing
the interface, native shell, or Python backend so the packaged app includes
the latest code.

On macOS, run from the repository root:

```bash
# First setup, or after changing desktop dependencies
npm --prefix desktop ci

# Rebuild Heron Ground Station.app
npm --prefix desktop run bundle -- --bundles app

# Launch the rebuilt app (skip network setup if already configured)
uv run heron-mission --no-ip-setup
```

The app is created at
`desktop/src-tauri/target/release/bundle/macos/Heron Ground Station.app`.
Quit the running app before reopening the rebuilt version. On other platforms,
use `npm --prefix desktop run bundle` for the platform's default bundles.

For development, `uv run heron-mission --dev --no-ip-setup` starts the
Tauri/Vite development app and rebuilds it as needed, even if a packaged build
exists. Use this while editing; rebuild the bundle before returning to the
normal launcher or opening the `.app` directly.

The emergency Textual controller remains available as `heron-controller`.
Terminal launchers configure the GCS network by default; use `--no-ip-setup`
if it is already configured. The desktop app offers **Configure network**
using the OS privilege dialog. See [HERON_SETUP.md](HERON_SETUP.md) for vehicle
network configuration and controls.
