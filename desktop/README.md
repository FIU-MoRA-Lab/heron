# Heron desktop

The GUI uses Tauri 2, SolidJS, TypeScript, and MapLibre GL JS. Python owns MAVLink,
mission files and upload, simulation, gamepad reading, lake geometry, and the
20 Hz manual-control loop. The legacy Textual controller remains independent.

## Folder guide

- `ui/`: SolidJS components, MapLibre map, shared types, and styles.
- `src-tauri/`: Rust native shell, Tauri configuration, and app icons. Its
  `src/` is Rust source only; the standard folder name keeps Tauri tooling compatible.
- `scripts/`: packaging helpers.
- `dist/`, `node_modules/`, `.bundle-resources/`, and `src-tauri/target/`:
  generated output, excluded from Git.

Python code, tests, and runtime requirements are in `../backend/`.

## Develop

Install Python 3.14+, uv, Node.js 22.12+ (or 24+), Rust, and the
[Tauri platform prerequisites](https://v2.tauri.app/start/prerequisites/).
On macOS, install Xcode command-line tools and `brew install hidapi` for
Logitech input. The desktop launcher adds Homebrew's library directories to
the backend's dylib search path, including when launched from Finder.
On Debian/Ubuntu, install:

```sh
sudo apt install libwebkit2gtk-4.1-dev build-essential curl wget file \
  libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev
```

From the repository root:

```sh
uv sync
cd desktop
npm ci
npm run desktop
```

Tauri starts Vite and the Python backend. Development uses the repo's `.venv`
when available. GUI startup doesn't run a terminal sudo prompt. Use **Configure
network** in the app (macOS administrator dialog / Linux polkit), or run
`uv run heron-network-setup` from the repository first. Antenna discovery still
requires Homebrew `arping` on macOS or `iputils-arping` on Linux.

The existing launcher also works:

```sh
uv run heron-mission --dev --no-ip-setup
uv run heron-mission --dev --mission-file backend/heron/examples/missions/sample.json
```

Without `--no-ip-setup`, the launcher runs the existing terminal network setup
before opening Tauri. Relative mission paths are resolved before launch.
To isolate UI testing from the vehicle, use a separate UDP port:

```sh
cd desktop
npm run desktop -- -- -- --connection udpin:127.0.0.1:14650
```

The two separators after npm's separator are required by Tauri: runner
arguments precede the second separator; app arguments follow it.

## Build and install

```sh
cd desktop
npm run bundle
```

Build on the target OS. macOS produces an app/DMG; Linux produces the available
AppImage/deb/rpm bundles; Windows builds are secondary and require MSVC and
WebView2. Build artifacts are in `src-tauri/target/release/bundle/`.
For a local unsigned macOS app:

```sh
npm run bundle -- --bundles app --no-sign
```

Bundles contain the Python source, backend requirements, FIU GeoTIFFs and their
metadata, and the lake GeoJSON. They do not contain Python itself. Install
Python 3.14+ and uv separately. On first launch, uv provisions an isolated
backend environment; internet access is required for that initial dependency
installation. Once provisioned, FIU imagery works offline. Esri imagery is
cached as it is viewed; uncached Esri coverage needs internet access.

`HERON_UV` can point to an explicit uv executable. `HERON_PYTHON` can point to
an existing interpreter with `backend/requirements.txt` installed, avoiding uv
provisioning. `HERON_DESKTOP` selects the desktop executable used by the Python
launcher. The launcher otherwise finds the repo's release app or an installed
app; if no release exists, it runs development mode.

Python telemetry logs are compressed `.mcap` files with per-message MAVLink
channels and nanosecond receive timestamps. Desktop and standalone logs are saved
in your Documents folder under `Heron/logs` on Windows, macOS, and Linux,
including redirected or localized Documents folders. `HERON_LOG_DIR` overrides
the log location. The raster cache lives in Tauri's writable application-data
directory; `HERON_CACHE_DIR` overrides the standalone backend tile cache.

Windows users must configure the Ethernet adapter's `192.168.2.1/24` address
manually. The emergency TUI is still available from a checkout with `uv run
heron-controller`; don't run it simultaneously with the desktop on the same
UDP receive port.

## Controls

- Click empty map space to add a waypoint; drag a marker to move it; right-click
  a marker to remove it. Drag empty space with left/middle/right mouse buttons
  to pan; scroll or pinch to zoom. Recenter resumes vehicle following; **FIU**
  centers on the lake for offline planning.
- Load JSON, CSV, or GeoJSON; save the route as GeoJSON; close loops, clear,
  and slide to start a mission. Start Mission automatically uploads the current
  route before starting it; there is no separate upload step in the GUI.
  Disconnected mission start uses the
  existing simulator and is labeled **SIMULATION**.
- Slide to arm/disarm. **Stop vehicle** or Space invokes the existing emergency
  stop behavior. The **Enable** toggle in Manual input selects MANUAL mode and
  enables keyboard/gamepad input; switching it off clears input to neutral.
  Hold WASD or
  arrows for manual axes. Throttle and steering sliders are read-only
  indicators of gamepad/keyboard input and cannot send commands by dragging.
- Switching out of manual mode, disabling input, losing window focus, or
  opening a native file dialog clears input. The backend also expires stale
  axes after 400 ms and GUI input after 750 ms without state polling.
- Closing the window, quitting from the application menu, or pressing Ctrl+C
  in the launcher checks the vehicle's armed state. An armed vehicle shows
  **Vehicle is armed. Are you sure you want to quit?** with Quit and Cancel.
  Cancel keeps the connection open; manual input is paused and must be enabled
  again. Confirmed shutdown sends neutral manual input, closes MAVLink, and
  finishes the MCAP log. It does not send a disarm command. The emergency TUI
  also asks for confirmation when armed.
- Lake padding retains the shoreline inset and fountain exclusion circles.
  It locks once a route exists, is uploaded/started, or the vehicle is armed.
  **Export lake** writes the source and derived safety geometry as GeoJSON.
  These overlays visualize safety areas; they do not implement an autopilot
  geofence.

## Process boundary

Rust launches `python -m heron.mission.backend --managed --no-ip-setup`, captures
a single readiness record containing an ephemeral loopback port and random
session token, and proxies commands through `backend_request`. The webview
has native dialog permissions and no general shell or filesystem permission.
MapLibre fetches raster tiles from the same loopback server with the token.
No vehicle commands are sent by Rust or JavaScript directly.

Telemetry is polled at 10 Hz. HID samples and manual MAVLink transmission stay
in Python threads. UI manual updates keep one request in flight and replace
queued values to avoid replaying stale throttle. Backend stdin stays open
while Tauri owns it; EOF triggers neutral output, MAVLink teardown, and log
flush. Rust allows graceful shutdown before forcibly stopping a stuck child.
The Python launcher and `npm run desktop` isolate the native app from terminal
signals and forward quit requests to its confirmation handler.

## Verify

```sh
uv run python -m unittest discover -s backend/tests -v
cd desktop
npm run build
cargo check --manifest-path src-tauri/Cargo.toml --locked
```

The integration suite uses loopback sockets and a separate UDP port, with no
real vehicle or Qt dependency. Native gamepad hardware, administrator network
setup, and real MAVLink vehicle operation still need validation on the target
machines.

The GitHub Actions desktop workflow builds macOS and Linux and runs the Python
integration suite. Windows runs as a best-effort job and doesn't block the
primary platforms. These jobs run after the changes are pushed; creating the
workflow locally does not verify those platforms.
