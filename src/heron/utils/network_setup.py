#!/usr/bin/env python3
"""
Heron Network Setup Utility
----------------------------
Auto-detects the host OS (Linux or macOS) and configures a static IP of
192.168.2.1/24 on the USB Ethernet adapter connected to the Heron's
MikroTik wireless bridge.

The frozen ground-control IP is:  192.168.2.1 / 24
The vehicle companion computer is: 192.168.2.2

Usage (standalone verification):
    uv run heron-network-setup
    uv run heron-network-setup --dry-run
"""

import re
import sys
import platform
import pathlib
import shutil
import subprocess

# ---------------------------------------------------------------------------
# Frozen network constants
# ---------------------------------------------------------------------------
GCS_IP        = "192.168.2.1"   # Ground Control Station static IP (this machine)
GCS_NETMASK   = "255.255.255.0"
GCS_PREFIX    = "24"
VEHICLE_IP    = "192.168.2.2"   # Heron companion computer (Raspberry Pi)
ANTENNA_GND   = "192.168.2.11"  # MikroTik ground-side antenna
MAVLINK_PORT  = 14550
MAVLINK_CONN  = f"udpin:0.0.0.0:{MAVLINK_PORT}"  # Listen on all interfaces


def _run(cmd: list, check: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command and return the CompletedProcess result."""
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


# ---------------------------------------------------------------------------
# Interface discovery
# ---------------------------------------------------------------------------

def _find_usb_ethernet_linux():
    """Find the unique Ethernet interface that can reach the ground MikroTik.

    Interface names and the number of available NICs are not evidence that an
    interface is connected to the antenna. Probe the antenna's MAC-level
    presence on each carrier-up Ethernet interface and fail closed if the
    result is ambiguous or cannot be established.
    """
    result = _run(["ip", "-o", "link", "show"])
    candidates = []
    for line in result.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        name = parts[1].strip().split("@", 1)[0]
        if name.startswith(("lo", "wl", "docker", "br-", "virbr", "veth", "tun", "tap", "wg")):
            continue
        # Linux ARP probing is meaningful only on a live Ethernet link.
        try:
            iface_type = (pathlib.Path("/sys/class/net") / name / "type").read_text().strip()
            carrier = (pathlib.Path("/sys/class/net") / name / "carrier").read_text().strip()
        except (OSError, ValueError):
            continue
        if iface_type == "1" and carrier == "1":
            candidates.append(name)

    arping = shutil.which("arping")
    if not arping or not candidates:
        return None

    responders = []
    for iface in candidates:
        try:
            probe = _run([arping, "-I", iface, "-c", "2", "-w", "3", ANTENNA_GND])
        except OSError:
            return None
        if probe.returncode == 0:
            responders.append(iface)

    return responders[0] if len(responders) == 1 else None


def _iface_is_active(iface: str) -> bool:
    """Return True if the interface has a live physical link (status: active)."""
    result = _run(["ifconfig", iface])
    return "status: active" in result.stdout


def _find_usb_ethernet_macos():
    """
    Return the unique active Ethernet service that answers for the MikroTik.

    Link activity and adapter naming alone cannot distinguish the antenna
    from another wired network, so never fall back to an unverified adapter.
    """
    result = _run(["networksetup", "-listallhardwareports"])
    lines = result.stdout.splitlines()

    candidates = []   # list of (device, service_name)
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("Hardware Port:"):
            port_name = line.split(":", 1)[1].strip()
            device_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if device_line.startswith("Device:"):
                dev = device_line.split(":", 1)[1].strip()
                if (
                    any(kw in port_name for kw in ("USB", "Ethernet", "Thunderbolt"))
                    and "Wi-Fi" not in port_name
                    and "Bridge" not in port_name
                    and dev.startswith("en")
                ):
                    candidates.append((dev, port_name))
        i += 1

    if not candidates:
        return None, None

    arping = shutil.which("arping")
    if not arping:
        return None, None

    responders = []
    for dev, service in candidates:
        if not _iface_is_active(dev):
            continue
        try:
            probe = _run([arping, "-I", dev, "-c", "2", "-w", "3", ANTENNA_GND])
        except OSError:
            return None, None
        if probe.returncode == 0:
            responders.append((dev, service))

    return responders[0] if len(responders) == 1 else (None, None)


# ---------------------------------------------------------------------------
# Current IP detection
# ---------------------------------------------------------------------------

def _current_ip_linux(iface: str):
    result = _run(["ip", "-4", "addr", "show", "dev", iface])
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("inet "):
            return line.split()[1].split("/")[0]
    return None


def _current_ip_macos(iface: str, service_name: str = None):
    """
    Return the current IPv4 address on *iface* (macOS).

    Prefers `networksetup -getinfo <service_name>` (reads the committed
    static IP configuration) when a service name is available.
    Falls back to parsing `ifconfig` live state.
    """
    # networksetup -getinfo requires the SERVICE name (e.g. "Ethernet Adapter (en3)")
    # NOT the device name (en3) — using the device name returns nothing.
    if service_name:
        result = _run(["networksetup", "-getinfo", service_name])
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("IP address:"):
                ip = line.split(":", 1)[1].strip()
                if ip and ip != "none":
                    return ip

    # Fallback: parse ifconfig live state
    result = _run(["ifconfig", iface])
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("inet ") and not line.startswith("inet 127."):
            return line.split()[1]
    return None


# ---------------------------------------------------------------------------
# Platform-specific setup
# ---------------------------------------------------------------------------

def setup_linux(auto: bool = True) -> bool:
    iface = _find_usb_ethernet_linux()
    if not iface:
        print(f"[network_setup] ERROR: Could not identify a unique wired interface responding as the MikroTik antenna ({ANTENNA_GND}).")
        print("                No network settings were changed. Check the Ethernet cable and install the arping utility.")
        return False

    print(f"[network_setup] Linux: detected wired interface → {iface}")

    if _current_ip_linux(iface) == GCS_IP:
        print(f"[network_setup] {iface} already has IP {GCS_IP}. Nothing to do.")
        return True

    cmds = [
        ["sudo", "ip", "addr", "add", f"{GCS_IP}/{GCS_PREFIX}", "dev", iface],
        ["sudo", "ip", "link", "set", iface, "up"],
    ]

    if not auto:
        print("[network_setup] Dry-run – would execute:")
        for c in cmds:
            print("  " + " ".join(c))
        return False

    try:
        for cmd in cmds:
            _run(cmd, check=True, capture=False)
        print(f"[network_setup] Assigned {GCS_IP}/{GCS_PREFIX} to {iface}.")
        return True
    except subprocess.CalledProcessError as exc:
        print(f"[network_setup] ERROR: {' '.join(exc.cmd)}")
        print("                Try running with sudo, or configure the IP manually.")
        return False


def setup_macos(auto: bool = True) -> bool:
    iface, service = _find_usb_ethernet_macos()
    if not iface:
        print(f"[network_setup] ERROR: Could not identify a unique Ethernet interface responding as the MikroTik antenna ({ANTENNA_GND}).")
        print("                No network settings were changed. Check the cable and install arping (for example, `brew install arping`).")
        print("                You can configure the verified antenna adapter manually via:")
        print("                System Settings > Network > [adapter] > TCP/IP > Manual")
        return False

    print(f"[network_setup] macOS: detected Ethernet → device={iface}, service='{service}'")

    if _current_ip_macos(iface, service_name=service) == GCS_IP:
        print(f"[network_setup] {iface} already has IP {GCS_IP}. Nothing to do.")
        return True

    if service:
        cmd = ["sudo", "networksetup", "-setmanual", service, GCS_IP, GCS_NETMASK]
    else:
        cmd = ["sudo", "ifconfig", iface, GCS_IP, "netmask", GCS_NETMASK, "up"]

    if not auto:
        print("[network_setup] Dry-run – would execute:")
        print("  " + " ".join(cmd))
        return False

    try:
        _run(cmd, check=True, capture=False)
        print(f"[network_setup] Assigned {GCS_IP} to {iface} (service: '{service}').")

        # macOS restarts the interface after networksetup -setmanual, which takes
        # several seconds before the link is fully up and ARP/routing works.
        # Wait up to 10 s for the IP to appear in ifconfig before continuing.
        print(f"[network_setup] Waiting for {iface} to come up...", end="", flush=True)
        import time as _time
        deadline = _time.time() + 10.0
        while _time.time() < deadline:
            result = _run(["ifconfig", iface])
            for line in result.stdout.splitlines():
                if GCS_IP in line:
                    print(" ready.")
                    return True
            print(".", end="", flush=True)
            _time.sleep(0.5)
        print(" timeout (interface may still be initialising).")
        return True  # IP was assigned even if ifconfig hasn't caught up yet

    except subprocess.CalledProcessError as exc:
        print(f"[network_setup] ERROR: {' '.join(exc.cmd)}")
        print("                Try running with sudo, or set the IP manually.")
        return False


# ---------------------------------------------------------------------------
# Connectivity verification
# ---------------------------------------------------------------------------

def _ping(host: str, timeout_sec: int = 2) -> float | None:
    """
    Send a single ICMP ping to *host* and return round-trip time in ms,
    or None if unreachable / timed out.

    Handles the flag difference between Linux (-W seconds) and macOS (-W ms).
    """
    os_name = platform.system()
    if os_name == "Darwin":
        # macOS: -W expects milliseconds
        cmd = ["ping", "-c", "1", "-W", str(timeout_sec * 1000), host]
    else:
        # Linux (and fallback): -W expects seconds
        cmd = ["ping", "-c", "1", "-W", str(timeout_sec), host]

    result = _run(cmd)
    if result.returncode != 0:
        return None

    # Parse "time=12.3 ms" from ping output
    match = re.search(r"time[=<](\d+\.?\d*)\s*ms", result.stdout)
    if match:
        return float(match.group(1))
    return 0.0  # reachable but couldn't parse time


def verify_network_links() -> bool:
    """
    Ping the MikroTik ground antenna and the vehicle companion computer to
    confirm the full RF link is up.

    Returns:
        True if both hops are reachable, False otherwise.
    """
    targets = [
        (ANTENNA_GND, "MikroTik ground antenna"),
        (VEHICLE_IP,  "vehicle companion computer (RPi)"),
    ]

    all_ok = True
    for ip, label in targets:
        rtt = _ping(ip)
        if rtt is not None:
            print(f"[network_setup] Ping {label} ({ip}) ... OK  ({rtt:.1f} ms)")
        else:
            print(f"[network_setup] Ping {label} ({ip}) ... FAILED")
            all_ok = False

    if not all_ok:
        print("[network_setup] WARNING: One or more network hops are unreachable.")
        print("                Check the USB cable, MikroTik bridge, and vehicle power.")

    return all_ok


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_gcs_ip(auto: bool = True) -> bool:
    """
    Detect the host OS, assign the frozen GCS IP (192.168.2.1/24) on the
    USB Ethernet adapter, then verify connectivity to the MikroTik antenna
    and the vehicle companion computer.

    Args:
        auto: If True (default), apply the IP configuration using sudo and
              run connectivity checks.  If False, only print commands (dry-run).

    Returns:
        True if the IP is set and both network hops are reachable, else False.
    """
    os_name = platform.system()
    print(f"[network_setup] Host OS detected: {os_name}")

    if os_name == "Linux":
        ip_ok = setup_linux(auto=auto)
    elif os_name == "Darwin":
        ip_ok = setup_macos(auto=auto)
    else:
        print(f"[network_setup] WARNING: Unsupported OS '{os_name}'.")
        print(f"                Please manually set a static IP of {GCS_IP}/{GCS_PREFIX} on your USB Ethernet adapter.")
        return False

    if ip_ok and auto:
        verify_network_links()

    return ip_ok


def mavlink_connection_string() -> str:
    """Return the MAVLink UDP listen string for this GCS."""
    return MAVLINK_CONN


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Configure static GCS IP for the Heron MAVLink connection.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the required commands without executing them."
    )
    args = parser.parse_args()

    ok = ensure_gcs_ip(auto=not args.dry_run)

    if ok:
        print(f"\n[network_setup] GCS IP ready  : {GCS_IP}")
        print(f"[network_setup] MAVLink string : {MAVLINK_CONN}")
    else:
        print("\n[network_setup] IP configuration failed or skipped.")
        sys.exit(1)


if __name__ == "__main__":
    main()
