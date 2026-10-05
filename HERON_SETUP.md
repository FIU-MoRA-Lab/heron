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

Identify your USB Ethernet interface name using `ip a` (e.g., `enx207bd2bd7e8f` or `eth1`), then run:

```bash
# 1. Flush existing IP configurations on the adapter
sudo ip addr flush dev enx207bd2bd7e8f

# 2. Assign static IP 192.168.2.1 with netmask 255.255.255.0 (/24)
sudo ip addr add 192.168.2.1/24 dev enx207bd2bd7e8f

# 3. Bring interface up
sudo ip link set enx207bd2bd7e8f up
```

---

### macOS Setup

#### Option A: Terminal (`ifconfig`)
Find your network interface name using `ifconfig` (e.g., `en5` or `en7`), then run:

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

### Automatic Setup via Script (`src/utils/network_setup.py`)

Instead of running the OS-specific commands above manually, the Python scripts
(`logger.py`, `controller_teleop.py`, and `waypoint_teleop.py`) will **automatically detect
your OS and assign the static IP at startup**.

You can also run the setup standalone to verify or pre-configure the interface:

```bash
# Auto-detect OS & assign 192.168.2.1/24 (requires sudo password prompt)
uv run src/utils/network_setup.py

# Dry-run: print commands without executing
uv run src/utils/network_setup.py --dry-run
```

> **Note**: The frozen GCS IP is `192.168.2.1`. This is hardcoded in
> `src/utils/network_setup.py` and used by all scripts automatically.

---

## 3. Python Setup & Dependencies

The project uses `uv` for dependency management:

```bash
# Install dependencies
uv add pymavlink pyserial inputs
```

---

## 4. Telemetry & Teleoperation Scripts

> **Auto IP setup**: Both scripts below automatically detect whether you are on
> **Linux** or **macOS** and assign the frozen static IP **`192.168.2.1/24`**
> to your USB Ethernet adapter before connecting. A `sudo` password prompt may
> appear. Use `--no-ip-setup` to skip this if the IP is already configured.

### 4.1 Telemetry Logger (`src/utils/logger.py`)

Listens for incoming MAVLink telemetry and logs every session to a timestamped
JSONL file in `~/heron_logs/` automatically. Also prints live output when
`--stdout` is passed.

```bash
# Run standalone telemetry logger (auto-configures 192.168.2.1 on USB Ethernet)
uv run src/utils/logger.py

# Also print messages to the terminal
uv run src/utils/logger.py --stdout

# Save to a specific file instead of the auto-generated timestamped name
uv run src/utils/logger.py --output my_log.jsonl

# Skip automatic IP setup (IP already configured)
uv run src/utils/logger.py --no-ip-setup
```

> **Note**: `src/mission/controller_teleop.py` and `src/mission/waypoint_teleop.py` both
> start the logger automatically on every run — no flags needed. Logs land in `~/heron_logs/`.

### 4.2 Logitech Controller Teleoperation (`src/mission/controller_teleop.py`)

Teleoperates the Heron surface vehicle thrusters using a Logitech gamepad / joystick:

```bash
# Run controller teleoperation (auto-configures 192.168.2.1 on USB Ethernet at startup)
uv run src/mission/controller_teleop.py

# Optional: Specify input device explicitly if multiple joysticks are connected
uv run src/mission/controller_teleop.py --device /dev/input/event21

# Skip automatic IP setup (IP already configured)
uv run src/mission/controller_teleop.py --no-ip-setup
```

#### Controller Controls Mapping:
- **Left Stick (Vertical)**: Throttle (Forward / Reverse)
- **Right Stick / Left Stick (Horizontal)**: Steering / Yaw turning
- **Button 1 (A / Trigger)**: Toggle ARM / DISARM
- **Button 2 (B / Thumb)**: Set Vehicle Mode to MANUAL

### 4.3 Satellite Feed & Waypoint Navigation (`src/mission/waypoint_teleop.py`)

Interactive Ground Control Station with live satellite imagery map, mouse click waypoint planning, and emergency manual override:

```bash
# Launch Satellite Map Ground Control Station
uv run src/mission/waypoint_teleop.py

# Launch with pre-loaded waypoints file (JSON or CSV)
uv run src/mission/waypoint_teleop.py --waypoints src/examples/waypoints/waypoints_sample.json

# Launch with dual Textual TUI dashboard + Satellite Map window
uv run src/mission/waypoint_teleop.py --tui --waypoints src/examples/waypoints/waypoints_sample.json
```

#### Map & Teleop Controls:
- **Click / Tap Map**: Drop new waypoint `(lat, lon)` on satellite view.
- **Click & Drag Waypoint**: Move existing waypoint position.
- **Shift + Click / Alt + Click / Right-Click**: Delete waypoint (easy touchpad shortcut!).
- **Trackpad Pinch / Two-finger Scroll / `+`/`-`**: **Google Maps style cursor-centered zoom** (keeps cursor position anchored while zooming).
- **On-screen `[+]` / `[-]` & `[🎯 Center]` Buttons**: Quick zoom and re-center controls on map overlay.
- **Key `V` / `R`**: 📡 **Lock & Re-Center Map on Vehicle Live GPS Feed** (follows vehicle coordinates in real-time).
- **Key `G`**: 🧪 **Set Test GPS Position** (useful when testing indoors before 3D GPS lock is acquired).
- **Key `U`**: Upload Waypoint Mission to ArduRover FCU via MAVLink and trigger `AUTO` mode.
- **Key `L` / `S`**: Load / Save waypoints from/to JSON or CSV files (`lat,lon,alt`).
- **Key `C`**: Clear all waypoints.
- **Key `M`**: Set MANUAL mode.
- **Key `A` / `D`**: ARM / DISARM vehicle.
- **SPACEBAR**: 🚨 **EMERGENCY STOP** (Instantly aborts auto navigation, sets MANUAL mode, and zeroes thrusters).
- **Arrow Keys / WASD**: Immediate manual control override.

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
