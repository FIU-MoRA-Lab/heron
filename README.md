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

## macOS dependencies for a fresh clone

Run the table's commands **in order** in Terminal. Skip tools you already have
at the required version. Installer commands come from the linked official
instructions; Python and Node versions below are selected for this repo.

| Dependency | What it provides / required version | Copy and paste into Terminal | Official instructions |
| --- | --- | --- | --- |
| Xcode Command Line Tools | Compiler, linker, and macOS SDK for the desktop app. Finish the installation dialog before continuing. | `xcode-select --install` | [Tauri macOS prerequisites](https://v2.tauri.app/start/prerequisites/#macos) |
| Homebrew | Package manager for the native controller and network tools. After installing, run the **Next steps** commands printed by the installer to put `brew` on your `PATH`. | `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"` | [Homebrew installation](https://docs.brew.sh/Installation) |
| nvm | Installs and selects Node.js versions. | `touch ~/.zshrc`<br>`curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.8/install.sh \| bash`<br>`source ~/.zshrc` | [nvm installation](https://github.com/nvm-sh/nvm#installing-and-updating) |
| Node.js and npm | Use **Node 24**. Node 22.12+ also works. npm is included; no separate npm install is needed. | `nvm install 24`<br>`nvm alias default 24`<br>`nvm use 24` | [Node.js download](https://nodejs.org/en/download), [nvm usage](https://github.com/nvm-sh/nvm#usage) |
| Rust, Cargo, and rustup | **Stable Rust** compiles the native shell. Cargo is Rust's build/package tool and is included with rustup. Choose the default installation when prompted. | `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \| sh`<br>`source "$HOME/.cargo/env"` | [Rust installation](https://rust-lang.org/tools/install/) |
| uv | Installs Python dependencies and launches the backend. | `curl -LsSf https://astral.sh/uv/install.sh \| sh`<br>`source "$HOME/.local/bin/env"` | [uv installation](https://docs.astral.sh/uv/getting-started/installation/) |
| Python | **Python 3.14**, matching `.python-version`. uv installs and manages it; no separate Python installer is needed. | `uv python install 3.14` | [uv Python versions](https://docs.astral.sh/uv/concepts/python-versions/) |
| hidapi | Native library for Logitech controller input. | `brew install hidapi` | [hidapi formula](https://formulae.brew.sh/formula/hidapi) |
| arping | Automatic vehicle Ethernet / antenna discovery. | `brew install arping` | [arping formula](https://formulae.brew.sh/formula/arping) |

Each line in a table cell is a separate command. These shell setup commands
assume macOS's default **zsh** shell. Open a new Terminal after installation
and verify the tools are available:

```bash
node --version
npm --version
rustc --version
cargo --version
uv --version
uv python find 3.14
xcode-select -p
```

Expect Node `v24.x` (or `v22.12` or newer in the 22 series), version output for
npm, Rust, Cargo, and uv, a Python 3.14 executable path, and an Xcode tools path.
The repo installs its own Tauri CLI with `npm ci`; no global Tauri installation
is needed.

## First run from a fresh clone

From the repository root, install the Python and desktop dependencies, then
start development mode:

```bash
uv sync
npm --prefix desktop ci
uv run heron-mission --dev --no-ip-setup
```

`uv sync` creates the repo's `.venv`; `npm ci` installs the locked desktop
dependencies. Development mode starts Vite, compiles the Rust app, and launches
the GUI and Python backend together. **No separate release build is needed to
run it.** The first launch needs internet access to download dependencies and
takes longer because there is no Rust build cache yet.

`--no-ip-setup` skips the terminal network setup. For a vehicle connection, use
**Configure network** in the app or run `uv run heron-network-setup` from the
repo root. For disconnected planning, you can leave network setup skipped.

For subsequent development launches, run the same `uv run heron-mission --dev
--no-ip-setup` command. Rerun `uv sync` after Python dependency changes and
`npm --prefix desktop ci` after desktop dependency changes.

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
manual control remain in Python. `uv run heron-mission` uses an existing release
app when available, otherwise it falls back to development mode (which requires
the dependencies above and `npm ci` first). Pass `--dev` to always use the source
checkout, even if a release app is already installed.

See [desktop/README.md](desktop/README.md) for packaging, offline FIU imagery,
and architecture.

## Build or rebuild the standalone desktop app

`uv run heron-mission` launches the existing desktop build when one is
available; it does not rebuild it after source changes. Rebuild after changing
the interface, native shell, or Python backend so the packaged app includes
the latest code.

To build a standalone app from a fresh clone on **macOS**, install the tools
above, then run these commands from the repository root:

```bash
uv sync
npm --prefix desktop ci

# Build a local unsigned Heron Ground Station.app
npm --prefix desktop run bundle -- --bundles app --no-sign

# Launch the rebuilt app (skip network setup if already configured)
uv run heron-mission --no-ip-setup
```

The bundle command builds the interface, compiles the native shell, and packages
the Python source and FIU imagery. `npm run build` alone only builds the web
interface; it does not produce the native desktop app.

The macOS app is created at
`desktop/src-tauri/target/release/bundle/macos/Heron Ground Station.app`.
You can also open this `.app` directly in Finder. It still requires Python and
uv: the bundle does not include a Python interpreter. On first launch, uv
provisions a separate backend environment and needs internet access; after that,
the bundled FIU imagery works offline.

To rebuild after interface, native-shell, or Python-backend changes, quit the
running app and rerun the bundle command, then launch it again. There is no need
to delete build caches. Repeat `uv sync` and `npm ci` when their dependencies
change. Building updates the app in the path above; if you previously copied it
to `/Applications`, replace that copy to use the new build from Finder.

For development, `uv run heron-mission --dev --no-ip-setup` starts the
Tauri/Vite development app and rebuilds it as needed, even if a packaged build
exists. Use this while editing; rebuild the bundle before returning to the
normal launcher or opening the `.app` directly.

The emergency Textual controller remains available as `heron-controller`.
Terminal launchers configure the GCS network by default; use `--no-ip-setup`
if it is already configured. The desktop app offers **Configure network**
using the OS privilege dialog. See [HERON_SETUP.md](HERON_SETUP.md) for vehicle
network configuration and controls.

<details>
<summary>Linux and Windows setup and builds</summary>

Install Python 3.14, uv, Node.js 22.12+ or 24+, and Rust stable with Cargo.
See the official installation links in the macOS table for platform-specific
instructions; the macOS shell commands are not intended for Windows PowerShell.

On **Debian/Ubuntu**, install the native Tauri dependencies and antenna-discovery
tool:

```bash
sudo apt update
sudo apt install libwebkit2gtk-4.1-dev build-essential curl wget file \
  libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev iputils-arping
```

On **Windows**, install the MSVC C++ build tools and WebView2 described in the
[Tauri prerequisites](https://v2.tauri.app/start/prerequisites/#windows).
Windows builds are secondary; configure the vehicle Ethernet adapter's
`192.168.2.1/24` address manually.

The first-run development commands above also apply on these platforms. For a
standalone build, run from the repo root on the target OS:

```bash
uv sync
npm --prefix desktop ci
npm --prefix desktop run bundle
```

Outputs are under `desktop/src-tauri/target/release/bundle/` (available
AppImage/deb/rpm bundles on Linux; platform bundles on Windows).

</details>
