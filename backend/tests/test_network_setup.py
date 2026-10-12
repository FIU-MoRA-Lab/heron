"""Ethernet discovery when macOS hardware enumeration is unavailable."""
import subprocess
import unittest
from unittest.mock import patch

from heron.utils import network_setup


class MacEthernetDiscoveryTests(unittest.TestCase):
    def test_fallback_accepts_only_active_wired_ethernet(self):
        links = """en0: flags=8863<UP,RUNNING> mtu 1500
    media: autoselect
    status: active
en3: flags=8863<UP,RUNNING> mtu 1500
    media: autoselect (1000baseT <full-duplex>)
    status: inactive
en5: flags=8863<UP,RUNNING> mtu 1500
    inet 192.168.2.1 netmask 0xffffff00
    media: autoselect (100baseTX <full-duplex>)
    status: active
bridge0: flags=8863<UP,RUNNING> mtu 1500
    media: autoselect (1000baseT <full-duplex>)
    status: active
"""
        responses = [
            subprocess.CompletedProcess([], 1, "", "AuthorizationCreate() failed: -60008"),
            subprocess.CompletedProcess([], 0, links, ""),
        ]
        with patch.object(network_setup, "_run", side_effect=responses), patch("builtins.print") as output:
            self.assertEqual(network_setup._macos_ethernet_candidates(), [("en5", None)])
            self.assertIn("AuthorizationCreate", output.call_args_list[0].args[0])

    def test_hardware_list_preserves_service_name(self):
        ports = "Hardware Port: USB 10/100/1000 LAN\nDevice: en5\n"
        with patch.object(network_setup, "_run", return_value=subprocess.CompletedProcess([], 0, ports, "")), patch.object(network_setup, "_iface_is_active", return_value=True):
            self.assertEqual(network_setup._macos_ethernet_candidates(), [("en5", "USB 10/100/1000 LAN")])


if __name__ == "__main__":
    unittest.main()
