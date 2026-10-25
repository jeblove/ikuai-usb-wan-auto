#!/usr/bin/env python3
"""iKuai USB DHCP WAN rebind using an authenticated Web UI session.

This version avoids the separately managed /api/v4.0 API token. It reuses
the original project's conservative candidate() and build_save(), and
never writes without an explicit --apply AND config apply=true.

It is safe to run in --once / --dry-run mode while another controller is
operating, but only ONE automatic writer should ever be enabled.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.cookiejar
import json
import os
import stat
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from ikuai_usb_wan_auto import candidate, build_save, load_config, log, StatusLogger, normalize_mac_prefixes


class WebUIError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward authenticated requests to another origin.
        return None


def _valid_url(url):
    if not isinstance(url, str):
        raise ValueError("expected router URL")
    p = urllib.parse.urlsplit(url)
    if (p.scheme != "https" or not p.hostname or p.username or p.password
            or p.path not in ("", "/") or p.query or p.fragment):
        raise ValueError("HTTPS router URL without credentials or path required")
    return url.rstrip("/")


class WebUI:
    def __init__(self, cfg, *, opener=None):
        self.url = _valid_url(cfg.get("url"))
        self.user = cfg.get("username")
        self.password = cfg.get("password")
        if not isinstance(self.user,str) or not self.user or not isinstance(self.password,str) or not self.password:
            raise WebUIError("username/password missing")
        self.cookies = http.cookiejar.CookieJar()
        tls = ssl._create_unverified_context() if cfg.get("insecure") is True else ssl.create_default_context()
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies),
            urllib.request.HTTPSHandler(context=tls),
            _NoRedirect(),
        )
        self.authenticated = False

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.url + path,
            json.dumps(payload,ensure_ascii=False,separators=(",",":")).encode(),
            {"Accept":"application/json","Content-Type":"application/json"},
            method="POST",
        )
        try:
            with self.opener.open(req,timeout=10) as response:
                result = json.load(response)
        except (urllib.error.URLError,TimeoutError,ValueError,UnicodeError,OSError) as exc:
            raise WebUIError("router Web UI request failed") from None
        if not isinstance(result,dict):
            raise WebUIError("router response is not an object")
        return result

    def login(self):
        password_hash = hashlib.md5(self.password.encode("utf-8")).hexdigest()
        # Firmware variants accept one or the other (both are read-only login).
        for passcode in (password_hash,
                         base64.b64encode(("salt_11"+password_hash).encode()).decode()):
            result = self._post("/Action/login",{
                "username":self.user,"passwd":password_hash,
                "pass":passcode,"remember_password":"",
            })
            if result.get("Result")==10000 or result.get("code")==0:
                self.authenticated = True
                return
        raise WebUIError("router authentication refused")

    def _call(self, func, action, param):
        if not self.authenticated:
            self.login()
        payload={"func_name":func,"action":action,"param":param}
        response=self._post("/Action/call",payload)
        if response.get("code")==1008:  # session expired
            self.authenticated=False
            if action!="show":
                # A failed write response can be ambiguous: do not replay.
                raise WebUIError("WAN save authentication state uncertain; no retry")
            self.login()
            response=self._post("/Action/call",payload)
        if not (response.get("code")==0 or response.get("Result")==30000):
            raise WebUIError("router API action not acknowledged")
        return response

    def show(self, func, params):
        result=self._call(func,"show",params)
        data=result.get("results",result.get("Data"))
        if not isinstance(data,dict):
            raise WebUIError("router show result invalid")
        return data

    def read_state(self, *, wan_name):
        if not isinstance(wan_name,str) or not wan_name.startswith("wan") or not wan_name[3:].isdigit():
            raise ValueError("invalid WAN name")
        wans=self.show("wan",{"TYPE":"total,data","limit":"0,50"})
        physical=self.show("lan",{"TYPE":"ether_info"})
        rows=wans.get("data")
        phys=physical.get("ether_info")
        if not isinstance(rows,list) or not isinstance(phys,dict):
            raise WebUIError("router WAN/physical listing invalid")
        total=wans.get("total")
        if type(total) is not int or total!=len(rows) or total>50:
            raise WebUIError("router WAN listing incomplete")
        matching=[w for w in rows if isinstance(w,dict)
                  and w.get("name")==wan_name
                  and str(w.get("id"))==wan_name[3:]]
        if len(matching)!=1:
            raise WebUIError("expected WAN not uniquely found")
        return phys,matching[0]

    def save_wan(self,payload):
        if (not isinstance(payload,dict) or payload.get("func_name")!="wan"
                or payload.get("action")!="save" or not isinstance(payload.get("param"),dict)):
            raise ValueError("invalid WAN save payload")
        return self._call("wan","save",payload["param"])


def arm_write_marker(marker: Path):
    """Durably record a possibly attempted WAN save BEFORE calling the router.

    If interrupted or response/readback is ambiguous, subsequent services
    refuse new writes until an operator explicitly reviews/removes the marker.
    """
    parent = marker.parent
    info = parent.stat()
    if (parent.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode)&0o077):
        raise WebUIError("unsafe write-state directory")
    if marker.is_symlink():
        raise WebUIError("write-state marker must not be a symlink")
    try:
        fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:
        raise WebUIError("unverified previous WAN write; manual review required") from None
    try:
        os.write(fd,b"router WAN save may have been attempted; inspect before resetting\n")
        os.fsync(fd)
    finally:
        os.close(fd)


def disarm_write_marker(marker: Path):
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_mode&0o077:
        raise WebUIError("unsafe write-state marker")
    marker.unlink()


def classify(physical,wan,*,wan_name,usb_name,allowed_mac_prefixes=()):
    mac,reason=candidate(physical,wan,usb_name,wan_name,allowed_mac_prefixes)
    if not mac:
        return None,reason
    # Require exact target and a complete full-config save, even during dry-run.
    build_save(wan,mac,usb_name,wan_name)
    return mac,"ready_to_rebind"


def poll(client, *, wan_name, usb_name, apply, stable=3, interval=3, cooldown=60,
         once=False, guard_file:Path|None=None, allowed_mac_prefixes=(),
         sleeper=time.sleep, clock=time.monotonic):
    if type(stable) is not int or stable<2 or not 1<=interval<=3600 or cooldown<0:
        raise ValueError("poll interval/stability invalid")
    status=StatusLogger(3600)
    previous=None
    streak=0
    last_attempt=-float("inf")
    client.login()
    while True:
        try:
            physical,wan=client.read_state(wan_name=wan_name)
            mac,reason=classify(physical,wan,wan_name=wan_name,usb_name=usb_name,allowed_mac_prefixes=allowed_mac_prefixes)
            if mac is None:
                previous,streak=None,0
                status.state(("idle",reason),"observed: "+reason)
            else:
                streak=streak+1 if mac==previous else 1
                previous=mac
                if not apply:
                    status.state(("dry_run",),"DRY_RUN: safe rebind candidate; no configuration changes")
                elif streak<stable:
                    status.state(("stabilizing",),"waiting for stable USB observations")
                elif clock()-last_attempt<cooldown:
                    status.state(("cooldown",),"cooldown: no WAN writes")
                else:
                    last_attempt=clock()
                    # Re-read immediately before a single targeted save.
                    fresh_physical,fresh_wan=client.read_state(wan_name=wan_name)
                    fresh_mac,_=classify(fresh_physical,fresh_wan,
                                         wan_name=wan_name,usb_name=usb_name,
                                         allowed_mac_prefixes=allowed_mac_prefixes)
                    if fresh_mac!=mac:
                        raise WebUIError("USB state changed during preflight")
                    payload=build_save(fresh_wan,mac,usb_name,wan_name)
                    if guard_file is not None:
                        arm_write_marker(guard_file)
                    # If save/readback fails, guard_file intentionally remains.
                    # Do not retry an uncertain WAN modification after restart.
                    client.save_wan(payload)
                    _,verified=client.read_state(wan_name=wan_name)
                    if (str(verified.get("bandif","")).lower()!=mac
                            or verified.get("bandeth")!=usb_name):
                        raise WebUIError("post-save readback failed; manual review required")
                    if guard_file is not None:
                        disarm_write_marker(guard_file)
                    status.state(("bound",),"SUCCESS: WAN binding readback verified; Internet untested")
                    previous,streak=None,0
        except (ValueError,WebUIError,KeyError,TypeError,urllib.error.URLError) as exc:
            status.error(type(exc).__name__+": "+str(exc))
            previous,streak=None,0
        if once:
            return
        status.health()
        sleeper(interval)


def main(argv=None):
    p=argparse.ArgumentParser(description="Token-free iKuai USB WAN auto-rebind, dry-run by default")
    p.add_argument("--config",required=True,type=Path)
    p.add_argument("--dry-run",action="store_true")
    p.add_argument("--once",action="store_true")
    p.add_argument("--apply",action="store_true",help="also requires config apply=true")
    args=p.parse_args(argv)
    try:
        cfg=load_config(args.config)
        if cfg.get("apply") is not True and args.apply:
            raise ValueError("config apply=true required for WAN changes")
        apply=args.apply and not args.dry_run
        wan_name=cfg.get("wan_interface","wan4")
        usb_name=cfg.get("usb_interface","usb0")
        if not isinstance(usb_name,str) or not usb_name.startswith("usb") or not usb_name[3:].isdigit():
            raise ValueError("USB interface must be usbN")
        allowed_mac_prefixes=normalize_mac_prefixes(cfg.get("mac_prefixes"))
        client=WebUI(cfg)
        log("mode="+("APPLY" if apply else "DRY_RUN")+" WAN="+str(wan_name)+" USB="+usb_name)
        poll(client,wan_name=wan_name,usb_name=usb_name,apply=apply,
             stable=int(cfg.get("stable",3)),
             interval=float(cfg.get("interval",3)),
             cooldown=float(cfg.get("cooldown",60)),
             once=args.once,
             guard_file=Path("/var/lib/ikuai-usb-wan/unverified") if apply else None,
             allowed_mac_prefixes=allowed_mac_prefixes)
    except (OSError,ValueError,WebUIError,TypeError) as exc:
        p.exit(2,"ERROR: "+type(exc).__name__+"; no verified WAN changes\n")


if __name__=="__main__":
    main()