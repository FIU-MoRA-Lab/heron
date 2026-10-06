# Heron Surface Vehicle MAVLink Connection Guide

This guide details the network topology, system configuration (for Linux and macOS), and Python script setup required to connect to and teleoperate the Heron Surface Vehicle using `pymavlink` and a Logitech Gamepad/Joystick.

---

## 1. System & Network Architecture

- **Ground Control Computer IP**: `192.168.2.1 / 24` (Static IP required on USB Ethernet Adapter)
- **MikroTik Wireless Antenna Bridge**: `192.168.2.11` (Ground) / `192.168.2.12` (Vehicle)
- **Vehicle Companion Computer (Raspberry Pi)**: `192.168.2.2`
- **MAVLink Stream**: `mavlink-routerd` on the companion computer routes telemetry output to UDP target `192.168.2.1:14550`.

---

## 2. Host Network Setup

### Linux Setup

Automatic Linux setup scans carrier-up Ethernet interfaces for the MikroTik ground antenna at `192.168.2.11`. It configures an interface only when exactly one interface responds. Install `arping` if needed (for example, `sudo apt install iputils-arping` on Debian or Ubuntu).

If you configure the connection manually, identify the interface connected to the MikroTik and run:

```bash
# Assign static IP 192.168.2.1 with netmask 255.255.255.0 (/24)
sudo ip addr add 192.168.2.1/24 dev enx207bd2bd7e8f

# Bring interface up
sudo ip link set enx207bd2bd7e8f up
```

---

### macOS Setup

#### Option A: Terminal (`ifconfig`)
Only use this on the Ethernet adapter physically connected to the MikroTik antenna. Do not choose an adapter based only on its name or because it is the only active Ethernet link. Find and verify the adapter first, then run:

```bash
# 1. Set static IP 192.168.2.1 and subnet mask 255.255.255.0
sudo ifconfig enX 192.168.2.1 netmask 255.255.255.0 up
```

#### Option B: Terminal (`networksetup`)
```bash
# 1. List network services to find the USB adapter name (e.g., "USB 10/100/1000 LAN")
networksetup -listallnetworkservices

# 2. Assign static IP using the service name
sudo networksetup -setmanual "USB 10/100/1000 LAN" 192.168.2.1 255.255.255.0
```

#### Option C: macOS GUI (System Settings)
1. Open **System Settings** > **Network**.
2. Select your **USB Ethernet Adapter**.
3. Click **Details...** > **TCP/IP**.
4. Change **Configure IPv4** to **Manually**.
5. Set:
   - **IP Address**: `192.168.2.1`
   - **Subnet Mask**: `255.255.255.0`
   - **Router**: *(Leave blank)*
6. Click **OK** and **Apply**.

---

### Automatic Setup via Script (`heron-network-setup`)

Instead of running the OS-specific commands above manually, the Python scripts
(`logger.py`, `legacy/controller.py`, and `mission_control.py`) will **automatically detect
your OS and assign the static IP at startup**.

On both Linux and macOS, automatic setup now probes active Ethernet links for
the MikroTik ground antenna (`192.168.2.11`) and changes settings only when
exactly one interface responds. Install `arping` if needed (for example,
`brew install arping` on macOS). If detection is missing or ambiguous, setup
stops without changing network settings.

You can also run the setup standalone to verify or pre-configure the interface:

```bash
# Auto-detect OS & assign 192.168.2.1/24 (requires sudo password prompt)
uv run heron-network-setup

# Dry-run: print commands without executing
uv run heron-network-setup --dry-run
```

> **Note**: The frozen GCS IP is `192.168.2.1`. This is hardcoded in
> `heron.utils.network_setup` and used by all scripts automatically.

---

## 3. Python Setup & Dependencies

The project uses `uv` for dependency management:

```bash
# Create/sync the project environment and install Heron as a package
uv sync
```

This installs the `heron` package from `src/` in editable mode and provides the
`heron-controller`, `heron-mission`, `heron-logger`, and
`heron-network-setup` commands. Run commands from the project root with `uv run`.

---

## 4. Telemetry & Teleoperation Scripts

> **Auto IP setup**: Both scripts below automatically detect whether you are on
> **Linux** or **macOS** and assign the frozen static IP **`192.168.2.1/24`**
> to your USB Ethernet adapter before connecting. A `sudo` password prompt may
> appear. Use `--no-ip-setup` to skip this if the IP is already configured.

### 4.1 Telemetry Logger (`heron-logger`)

Listens for incoming MAVLink telemetry and logs every session to a timestamped
JSONL file in `~/heron_logs/` automatically. Also prints live output when
`--stdout` is passed.

```bash
# Run standalone telemetry logger (auto-configures 192.168.2.1 on USB Ethernet)
uv run heron-logger

# Also print messages to the terminal
uv run heron-logger --stdout

# Save to a specific file instead of the auto-generated timestamped name
uv run heron-logger --output my_log.jsonl

# Skip automatic IP setup (IP already configured)
uv run heron-logger --no-ip-setup
```

> **Note**: `heron-controller` and `heron-mission` both
> start the logger automatically on every run — no flags needed. Logs land in `~/heron_logs/`.

### 4.2 Logitech Controller Teleoperation (`heron-controller`)

Teleoperates the Heron surface vehicle thrusters using a Logitech gamepad / joystick:

```bash
# Run controller teleoperation (auto-configures 192.168.2.1 on USB Ethernet at startup)
uv run heron-controller

# Optional: Specify input device explicitly if multiple joysticks are connected
uv run heron-controller --keyboard

# Skip automatic IP setup (IP already configured)
uv run heron-controller --no-ip-setup
```

#### Controller Controls Mapping:
- **Left Stick (Vertical)**: Throttle (Forward / Reverse)
- **Right Stick / Left Stick (Horizontal)**: Steering / Yaw turning
- **Button 1 (A / Trigger)**: Toggle ARM / DISARM
- **Button 2 (B / Thumb)**: Set Vehicle Mode to MANUAL

### 4.3 Satellite Map & Mission Control (`heron-mission`)

Interactive Ground Control Station with live satellite imagery map, mouse click waypoint planning, and emergency manual override:

```bash
# Launch Satellite Map Ground Control Station
uv run heron-mission

# Launch with pre-loaded waypoints file (JSON or CSV)
uv run heron-mission --mission-file src/heron/examples/missions/sample.json
```

#### Recent Sentinel-2 imagery

The map can overlay the newest cloud-free Sentinel-2 L2A pixels from the last 30 days over Esri imagery. Esri remains visible where Sentinel-2 has clouds or no valid observation. To enable it, create a Copernicus Data Space OAuth client and set these variables in the shell before launching:

```bash
export COPERNICUS_CLIENT_ID='your-client-id'
export COPERNICUS_CLIENT_SECRET='your-client-secret'
uv run heron-mission
```

Without both variables, the map uses Esri imagery. Keep the client secret in your environment or a local secret manager; do not commit it to the repository. Sentinel-2 requests use the Copernicus Data Space Processing API, which requires OAuth authentication.

#### Map & Teleop Controls:
- The default desktop interface uses PySide6 / Qt Quick for a crisp, scalable cross-platform UI.
- **Click Map**: Drop a new waypoint `(lat, lon)` on the satellite view.
- **Click & Drag Waypoint**: Move an existing waypoint.
- **Right-Click Waypoint**: Delete it. Middle-drag or right-drag empty map space to pan.
- **Scroll / Trackpad Scroll**: Cursor-centered zoom. The map controls also provide zoom and re-center buttons.
- **Load / Save / Clear / Upload / Start Mission / Arm / Disarm / Manual / Stop**: Use the Mission Control panel.
- **Manual control**: Use the arrow keys / WASD or a connected gamepad. The manual-input overlay shows the active source and axis values.
- Run `heron-controller` for the legacy Textual manual-control console and its keyboard controls.

---

## 5. Verification & Troubleshooting

1. **Ping Ground Antenna**:
   ```bash
   ping 192.168.2.11
   ```

2. **Ping Vehicle Companion Computer**:
   ```bash
   ping 192.168.2.2
   ```

3. **Check Gamepad Device Detection**:
   ```bash
   ls -l /dev/input/js* /dev/input/event*
   ```
