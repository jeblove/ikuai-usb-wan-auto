import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ikuai_usb_wan_auto as legacy
import ikuai_usb_wan_auto_webui as auto


def nic(mac="22:33:44:55:66:77", interface="Null"):
    return {"driver":"usb","type":"USB","link":1,
            "mac":mac,"interface":interface}


def wan(mac="aa:bb:cc:dd:ee:ff", bound="usb0"):
    row={k:"" for k in legacy.BROWSER_SAVE_KEYS}
    row.update({"name":"wan4","tagname":"wan4","id":4,
                "bandeth":bound,"bandif":mac,"linkmode":0,
                "passwd":"","dhcp_status":1,"default_route":1})
    return row


class FakeBackend:
    def __init__(self, physically_present=True, *, bad_readback=False):
        self.physically_present=physically_present
        self.bad_readback=bad_readback
        self.state=wan()
        self.save_calls=0
        self.login_calls=0
        self.read_calls=0

    def login(self):
        self.login_calls+=1

    def read_state(self, *, wan_name):
        self.read_calls+=1
        if wan_name!="wan4":
            raise ValueError("wrong interface")
        physical={"usb0":nic()} if self.physically_present else {}
        return physical,dict(self.state)

    def save_wan(self, payload):
        self.save_calls+=1
        if not self.bad_readback:
            self.state["bandif"]=payload["param"]["bandif"]


class TestWebUIValidation(unittest.TestCase):
    def test_only_router_https_origin_without_credentials(self):
        for u in ("http://192.168.1.1","https://u:p@example.com",
                  "https://example.com/action","https://example.com/?x=1"):
            with self.subTest(u=u),self.assertRaises(ValueError):
                auto._valid_url(u)

    def test_missing_usb_never_writes_even_apply(self):
        fake=FakeBackend(False)
        auto.poll(fake,wan_name="wan4",usb_name="usb0",apply=True,once=True)
        self.assertEqual(fake.save_calls,0)

    def test_rebind_stable_after_three_consecutive_reads(self):
        fake=FakeBackend()
        sleeps=[]
        def sleep(_):
            sleeps.append(1)
        with patch.object(auto.time,"sleep",side_effect=sleep):
            # single iteration is not enough to write
            auto.poll(fake,wan_name="wan4",usb_name="usb0",apply=True,once=True)
        self.assertEqual(fake.save_calls,0)
        # Simulate a real polling session ending after verification.
        class StopOnRead(FakeBackend):
            def read_state(self,*,wan_name):
                if self.save_calls:
                    raise StopIteration()
                return super().read_state(wan_name=wan_name)
        trial=StopOnRead()
        def short_sleep(_): pass
        with self.assertRaises(StopIteration):
            auto.poll(trial,wan_name="wan4",usb_name="usb0",apply=True,
                      stable=3,sleeper=short_sleep)
        self.assertEqual(trial.save_calls,1)

    def test_dry_run_never_writes(self):
        fake=FakeBackend()
        auto.poll(fake,wan_name="wan4",usb_name="usb0",apply=False,once=True)
        self.assertEqual(fake.save_calls,0)
        self.assertEqual(fake.read_calls,1)

    def test_bad_rollback_readback_stops_unverified(self):
        fake=FakeBackend(bad_readback=True)
        def stop_after_error(_):
            raise StopIteration()
        with self.assertRaises(StopIteration):
            auto.poll(fake,wan_name="wan4",usb_name="usb0",apply=True,
                      stable=2,sleeper=stop_after_error)
        self.assertEqual(fake.save_calls,0)

    def test_original_candidate_guards_mismatched_binding(self):
        physical={"usb0":nic(interface="wan4")}
        got,_=auto.classify(physical,wan(),wan_name="wan4",usb_name="usb0")
        self.assertIsNone(got)

    def test_uses_full_browser_save_schema(self):
        mac,code=auto.classify({"usb0":nic()},wan(),wan_name="wan4",usb_name="usb0")
        self.assertEqual(mac,"22:33:44:55:66:77")
        self.assertEqual(code,"ready_to_rebind")
        broken=wan()
        del broken["mtu"]
        with self.assertRaises(ValueError):
            auto.classify({"usb0":nic()},broken,wan_name="wan4",usb_name="usb0")

    def test_incomplete_wan_listing_refused(self):
        cfg={"url":"https://example.test","username":"u","password":"p"}
        instance=auto.WebUI(cfg,opener=object())
        instance.show=lambda func,params: (
            {"total":2,"data":[wan()]} if func=="wan"
            else {"ether_info":{"usb0":nic()}}
        )
        with self.assertRaises(auto.WebUIError):
            instance.read_state(wan_name="wan4")

    def test_expired_save_is_not_retried(self):
        cfg={"url":"https://example.test","username":"u","password":"p"}
        instance=auto.WebUI(cfg,opener=object())
        instance.authenticated=True
        count=[0]
        def fake_post(path,payload):
            count[0]+=1
            return {"code":1008}
        instance._post=fake_post
        with self.assertRaises(auto.WebUIError):
            instance.save_wan({"func_name":"wan","action":"save","param":{"id":4}})
        self.assertEqual(count,[1])

    def test_only_once_preflight_does_not_expose_sensitive_data(self):
        fake=FakeBackend()
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            auto.poll(fake,wan_name="wan4",usb_name="usb0",apply=False,once=True)
        self.assertNotIn("22:33:44:55:66:77",output.getvalue())
        self.assertNotIn("aa:bb:cc:dd:ee:ff",output.getvalue())

    def test_guard_marker_removed_only_after_verified_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            private=Path(tmp)/"private"
            private.mkdir(mode=0o700)
            guard=private/"unverified"
            fake=FakeBackend()
            ticks=[0]
            def stop_after_third(_):
                ticks[0]+=1
                if ticks[0]>=3:
                    raise StopIteration()
            with self.assertRaises(StopIteration):
                auto.poll(fake,wan_name="wan4",usb_name="usb0",
                          apply=True,stable=2,guard_file=guard,
                          sleeper=stop_after_third)
            self.assertEqual(fake.save_calls,1)
            self.assertFalse(guard.exists())

    def test_guard_marker_remains_after_ambiguous_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            private=Path(tmp)/"private"
            private.mkdir(mode=0o700)
            guard=private/"unverified"
            fake=FakeBackend(bad_readback=True)
            ticks=[0]
            def stop_after_third(_):
                ticks[0]+=1
                if ticks[0]>=3:
                    raise StopIteration()
            with self.assertRaises(StopIteration):
                auto.poll(fake,wan_name="wan4",usb_name="usb0",
                          apply=True,stable=2,guard_file=guard,
                          sleeper=stop_after_third)
            self.assertEqual(fake.save_calls,1)
            self.assertTrue(guard.exists())
            self.assertEqual(guard.stat().st_mode & 0o777,0o600)
            # Restart still refuses writes while manual review is pending.
            another=FakeBackend()
            ticks[0]=0
            with self.assertRaises(StopIteration):
                auto.poll(another,wan_name="wan4",usb_name="usb0",
                          apply=True,stable=2,guard_file=guard,
                          sleeper=stop_after_third)
            self.assertEqual(another.save_calls,0)

    def test_guard_refuses_insecure_state_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)/"insecure"
            directory.mkdir(mode=0o755)
            with self.assertRaises(auto.WebUIError):
                auto.arm_write_marker(directory/"marker")


    def test_webui_dry_run_accepts_randomized_suffix_with_allowlist(self):
        fake=FakeBackend()
        fake.state['bandif']='dd:ee:ff:11:22:33'
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            auto.poll(fake,wan_name='wan4',usb_name='usb0',
                      apply=False,once=True,allowed_mac_prefixes=('22:33:44',))
        self.assertIn('DRY_RUN',out.getvalue())
        self.assertEqual(fake.save_calls,0)

    def test_webui_rejects_nonmatching_mac_even_if_apply(self):
        fake=FakeBackend()
        auto.poll(fake,wan_name='wan4',usb_name='usb0',
                  apply=True,once=True,allowed_mac_prefixes=('02:ab:cd',))
        self.assertEqual(fake.save_calls,0)
        self.assertEqual(fake.read_calls,1)

    def test_webui_prefix_eligible_writes_only_after_stable_observations(self):
        fake=FakeBackend()
        ticks=[0]
        def stop(_):
            ticks[0]+=1
            if ticks[0]>=3:
                raise StopIteration()
        with self.assertRaises(StopIteration):
            auto.poll(fake,wan_name='wan4',usb_name='usb0',
                      apply=True,stable=2,sleeper=stop,
                      allowed_mac_prefixes=('22:33:44',))
        self.assertEqual(fake.save_calls,1)
        self.assertEqual(fake.state['bandif'],'22:33:44:55:66:77')

    def test_main_rejects_invalid_prefix_before_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'config.toml'
            p.write_text('url="https://localhost"\nusername="u"\npassword="p"\n'
                         'apply=true\nmac_prefixes=["02:qq"]\n')
            p.chmod(0o600)
            with patch.object(auto,'WebUI') as client:
                with self.assertRaises(SystemExit) as ex:
                    auto.main(['--config',str(p),'--once'])
                self.assertEqual(ex.exception.code,2)
                client.assert_not_called()

    def test_cli_apply_refused_when_config_is_dry(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"config.toml"
            p.write_text('url="https://localhost"\nusername="u"\npassword="p"\napply=false\n')
            p.chmod(0o600)
            with self.assertRaises(SystemExit) as ex:
                auto.main(["--config",str(p),"--apply","--once"])
            self.assertEqual(ex.exception.code,2)


if __name__=="__main__":
    unittest.main()