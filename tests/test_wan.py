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



class TestMacPrefixAllowlist(unittest.TestCase):
    def test_default_accepts_different_randomized_mac_addresses(self):
        for mac in ('02:aa:bb:01:02:03', 'de:ad:be:04:05:06', 'a2:11:22:33:44:55'):
            with self.subTest(mac=mac):
                observed, reason = app.candidate(
                    {'usb0': nic(mac=mac)}, wan(), 'usb0', 'wan2')
                self.assertEqual(observed, mac)
                self.assertEqual(reason, 'rebind needed')

    def test_prefix_matches_full_octets_and_suffix_can_randomize(self):
        allowed = app.normalize_mac_prefixes(['02:AA:BB'])
        for mac in ('02:aa:bb:00:00:01', '02:aa:bb:ff:ee:dd'):
            self.assertTrue(app.mac_allowed(mac, allowed))
            actual, _ = app.candidate(
                {'usb0': nic(mac=mac)}, wan(), 'usb0', 'wan2', allowed)
            self.assertEqual(actual, mac)
        self.assertFalse(app.mac_allowed('02:aa:bc:00:00:01', allowed))
        self.assertFalse(app.mac_allowed('02:aa:bb:01:02:03', ['02:aa:bb:01:04']))
        self.assertFalse(app.mac_allowed('02:aa:bc:01:02:03', ['02:aa:bb']))

    def test_other_phone_or_other_prefix_rejected(self):
        mac, reason = app.candidate(
            {'usb0': nic(mac='a2:bb:cc:01:02:03')},
            wan(), 'usb0', 'wan2', ['02:bb:cc'])
        self.assertIsNone(mac)
        self.assertIn('mac_prefixes', reason)

    def test_multiple_prefixes_are_supported(self):
        prefixes = app.normalize_mac_prefixes(['02:ab:cd', 'a2:bc:de:ef'])
        self.assertEqual(len(prefixes), 2)
        self.assertTrue(app.mac_allowed('a2:bc:de:ef:00:01', prefixes))
        self.assertTrue(app.mac_allowed('02:ab:cd:00:01:02', prefixes))

    def test_reject_malformed_and_overbroad_prefix_lists(self):
        for invalid in (
            '02:aa:bb', ['02'], ['02:'], ['02:a'], ['ZZ:aa'],
            ['02:aa:bb:cc:dd:ee'], ['02:aa:bb','02:AA:BB'],
            [3], [None], [True], ['02:ab']*17,
        ):
            with self.subTest(value=str(invalid)), self.assertRaises(ValueError):
                app.normalize_mac_prefixes(invalid)

    def test_empty_allowlist_unrestricted(self):
        self.assertEqual(app.normalize_mac_prefixes(None), ())
        self.assertEqual(app.normalize_mac_prefixes([]), ())
        self.assertTrue(app.mac_allowed('aa:bb:cc:dd:ee:ff', ()))
        self.assertFalse(app.mac_allowed('not-a-mac', ()))

    def test_other_hardware_guards_still_apply_with_prefixes(self):
        p = ['02:aa:bb']
        w = wan()
        phy = {'usb0': nic(mac='02:aa:bb:00:01:02', interface='wan1')}
        actual, _ = app.candidate(phy, w, 'usb0', 'wan2', p)
        self.assertIsNone(actual)
        phy = {'usb0': nic(mac='02:aa:bb:00:01:02')}
        phy['eth1']={'mac':'02:aa:bb:00:01:02'}
        actual, _ = app.candidate(phy, w, 'usb0', 'wan2', p)
        self.assertIsNone(actual)


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