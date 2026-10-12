"""Offline tests for interface safety and logging. Run: python3 -m unittest discover -s tests -v"""
import contextlib
import io
import types
import unittest
from unittest.mock import patch

import ikuai_usb_wan_auto as app


def nic(mac="aa:b3:01:8b:be:ae", interface="Null"):
    return {"driver": "usb", "type": "USB", "link": 1,
            "mac": mac, "interface": interface, "model": "USB tethering"}


def wan(name="wan2", usb="usb0", mac="11:22:33:44:55:66"):
    return {"name": name, "tagname": name, "id": int(name[3:]),
            "bandeth": usb, "bandif": mac, "linkmode": 0, "passwd": ""}


class TestCandidate(unittest.TestCase):
    def test_brand_independent_usb(self):
        mac, reason = app.candidate({"usb0": nic()}, wan(), "usb0", "wan2")
        self.assertEqual(mac, "aa:b3:01:8b:be:ae")
        self.assertEqual(reason, "rebind needed")

    def test_correct_existing_binding(self):
        w = wan(mac="aa:b3:01:8b:be:ae")
        mac, reason = app.candidate({"usb0": nic(interface="wan2")}, w, "usb0", "wan2")
        self.assertIsNone(mac)
        self.assertEqual(reason, "already bound correctly")

    def test_refuses_other_wan_assignment(self):
        mac, _ = app.candidate({"usb0": nic(interface="wan1")}, wan(), "usb0", "wan2")
        self.assertIsNone(mac)

    def test_refuses_non_usb_device(self):
        device = nic()
        device["driver"] = "igc"
        mac, _ = app.candidate({"usb0": device}, wan(), "usb0", "wan2")
        self.assertIsNone(mac)

    def test_refuses_duplicate_mac(self):
        physical = {"usb0": nic(), "eth0": {"mac": "aa:b3:01:8b:be:ae"}}
        mac, reason = app.candidate(physical, wan(), "usb0", "wan2")
        self.assertIsNone(mac)
        self.assertEqual(reason, "duplicate MAC detected")

    def test_refuses_wrong_usb_or_linkmode(self):
        w = wan(usb="usb1")
        mac, _ = app.candidate({"usb0": nic()}, w, "usb0", "wan2")
        self.assertIsNone(mac)
        w = wan()
        w["linkmode"] = 4
        mac, _ = app.candidate({"usb0": nic()}, w, "usb0", "wan2")
        self.assertIsNone(mac)


class TestWanWrite(unittest.TestCase):
    def test_full_save_only_replaces_binding_and_dhcp_state(self):
        w = {k: "" for k in app.BROWSER_SAVE_KEYS}
        w.update(wan())
        w.update({"dhcp_ip_addr": "192.0.2.10", "dhcp_lease": 100,
                  "dhcp_status": 2, "default_route": 1})
        result = app.build_save(w, "aa:b3:01:8b:be:ae", "usb0", "wan2")
        self.assertEqual(result["func_name"], "wan")
        self.assertEqual(result["action"], "save")
        p = result["param"]
        self.assertEqual((p["bandif"], p["bandeth"]), ("aa:b3:01:8b:be:ae", "usb0"))
        self.assertEqual(p["default_route"], 1)
        self.assertEqual(p["dhcp_ip_addr"], "")
        self.assertEqual(p["dhcp_status"], 1)

    def test_rejects_different_target(self):
        w = {k: "" for k in app.BROWSER_SAVE_KEYS}
        w.update(wan("wan1"))
        with self.assertRaises(ValueError):
            app.build_save(w, "aa:b3:01:8b:be:ae", "usb0", "wan2")

    def test_rejects_missing_fields_and_password(self):
        with self.assertRaises(ValueError):
            app.build_save(wan(), "aa:b3:01:8b:be:ae", "usb0", "wan2")
        w = {k: "" for k in app.BROWSER_SAVE_KEYS}
        w.update(wan())
        w["passwd"] = "secret"
        with self.assertRaises(ValueError):
            app.build_save(w, "aa:b3:01:8b:be:ae", "usb0", "wan2")


class TestReadState(unittest.TestCase):
    def test_selects_configured_wan(self):
        def fake_request(base, path, **kwargs):
            if path.endswith("physical"):
                return {"results": {"ether_info": {"usb0": nic()}}}
            return {"results": {"data": [wan("wan1"), wan("wan2")]}}
        args = types.SimpleNamespace(url="https://example.invalid", token="placeholder",
                                     insecure=False, wan_interface="wan2")
        with patch.object(app, "request", side_effect=fake_request):
            physical, result = app.read_state(args)
        self.assertEqual(result["name"], "wan2")
        self.assertIn("usb0", physical)


class TestLogging(unittest.TestCase):
    def test_identical_state_is_logged_once(self):
        out = io.StringIO()
        status = app.StatusLogger(heartbeat=0)
        with contextlib.redirect_stdout(out):
            status.state(("idle", "healthy"), "healthy")
            status.state(("idle", "healthy"), "healthy")
            status.state(("idle", "absent"), "absent")
        self.assertEqual(out.getvalue().count("healthy"), 1)
        self.assertEqual(out.getvalue().count("absent"), 1)


if __name__ == "__main__":
    unittest.main()
