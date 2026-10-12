#!/usr/bin/env python3
"""Auto-rebind a USB tethering interface to a configured iKuai WAN. Dry-run by default.

Read:  GET /api/v4.0/interfaces/{physical,wan-config} using API token.
Write: POST /Action/call with func_name=wan, action=save (captured from iKuai UI).
The older Action/call endpoint may require an authenticated browser session cookie.
Do not put tokens/cookies into the script or command line: use environment variables.
"""
import argparse
import base64
import hashlib
import http.cookiejar
import datetime as datetime
import json
import os
import re
import ssl
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

MAC_RE = re.compile(r'^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$')
# Keys present in the observed browser save. Avoid inadvertently sending API-only keys.
BROWSER_SAVE_KEYS = '''tagname ass_check_errip_list ass_check_errip_switch ass_disc_rst_switch ass_multi_total ass_rst_check_interval ass_rst_check_time ass_rst_check_week ass_rst_disc_norestart ass_rst_disc_num bandeth bandif bandlist_5g bandmode check_link_host check_link_mode clientid comment cycle_rst_time default_route dhcp_dns1 dhcp_dns2 dhcp_gateway dhcp_ip_addr dhcp_lease dhcp_netmask dhcp_status dhcp_updatetime disc_auto_switch download duplex enable_ipv6 gateway hostname id internet ip_mask link_time linkmode lte_antenna_switch lte_apn lte_dialnum lte_mode lte_pincode lte_service mac modified_time mru mtu name opt_type12 opt_type60 opt_type61 passwd policy pppoe_ac pppoe_ass_switch pppoe_check_errip_list pppoe_check_errip_switch pppoe_dns1 pppoe_dns2 pppoe_gateway pppoe_ip_addr pppoe_macremote pppoe_netmask pppoe_service pppoe_status pppoe_updatetime qos_download qos_switch qos_upload sim_switch speed timing_rst_switch timing_rst_time timing_rst_week upload username vendorclass wifi_bssid wifi_psk wifi_ssid wifi_wisp'''.split()
DHCP_EMPTY = ('dhcp_dns1','dhcp_dns2','dhcp_gateway','dhcp_ip_addr','dhcp_netmask')
DHCP_ZERO = ('dhcp_lease','dhcp_updatetime')


def log(msg):
    print(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg, flush=True)


class StatusLogger:
    def __init__(self, heartbeat=3600):
        self.last_state = None
        self.last_error = None
        self.last_error_time = 0
        self.heartbeat = heartbeat
        self.last_heartbeat = time.monotonic()

    def state(self, state, message):
        if state != self.last_state:
            log(message)
            self.last_state = state
        self.last_error = None

    def error(self, message):
        now = time.monotonic()
        if message != self.last_error or now - self.last_error_time > 300:
            log('ERROR: ' + message)
            self.last_error_time = now
        self.last_error = message
        self.last_state = ('error', message)

    def health(self):
        now = time.monotonic()
        if self.heartbeat > 0 and now - self.last_heartbeat >= self.heartbeat:
            log('HEALTH: monitoring; state=%s' % (self.last_state,))
            self.last_heartbeat = now


def request(base, path, method='GET', data=None, token=None, cookie=None, insecure=False, opener=None):
    body = None if data is None else json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()
    headers = {'Accept': 'application/json'}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if cookie:
        headers['Cookie'] = cookie
    req = urllib.request.Request(base.rstrip('/') + path, data=body, method=method, headers=headers)
    ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    with (opener.open(req, timeout=10) if opener else urllib.request.urlopen(req, timeout=10, context=ctx)) as response:
        result = json.load(response)
    if not isinstance(result, dict) or result.get('code') != 0:
        raise RuntimeError('API returned failure code=%r message=%r' %
                           (result.get('code') if isinstance(result, dict) else None,
                            result.get('message') if isinstance(result, dict) else None))
    return result


def read_state(args):
    physical = request(args.url, '/api/v4.0/interfaces/physical', token=args.token,
                       insecure=args.insecure)['results']['ether_info']
    wans = request(args.url, '/api/v4.0/interfaces/wan-config', token=args.token,
                   insecure=args.insecure)['results']['data']
    matches = [w for w in wans if w.get('name') == args.wan_interface and str(w.get('id')) == args.wan_interface[3:]]
    if len(matches) != 1:
        raise RuntimeError(f'{args.wan_interface} not uniquely found')
    return physical, matches[0]


def candidate(physical, wan, usb_interface, wan_interface):
    if wan.get('linkmode') != 0 or wan.get('bandeth') != usb_interface:
        return None, f'{wan_interface} is not configured for {usb_interface} in DHCP mode; refusing'
    usb = physical.get(usb_interface)
    if not isinstance(usb, dict):
        return None, f'{usb_interface} absent'
    if not (usb.get('driver') == 'usb' and usb.get('type') == 'USB' and
            usb.get('link') == 1 and MAC_RE.fullmatch(str(usb.get('mac', '')).lower())):
        return None, f'{usb_interface} did not pass hardware validation'
    if usb.get('interface') not in ('Null', wan_interface):
        return None, f'{usb_interface} is assigned to another interface'
    mac = usb['mac'].lower()
    if any(k != usb_interface and isinstance(v,dict) and str(v.get('mac','')).lower() == mac
           for k,v in physical.items()):
        return None, 'duplicate MAC detected'
    if usb.get('interface') == wan_interface and str(wan.get('bandif','')).lower() == mac:
        return None, 'already bound correctly'
    if usb.get('interface') != 'Null':
        return None, 'binding inconsistent; refusing'
    return mac, 'rebind needed'


def build_save(wan, new_mac, usb_interface, wan_interface):
    missing = [key for key in BROWSER_SAVE_KEYS if key not in wan]
    if missing:
        raise ValueError(f'{wan_interface} GET lacks browser-save keys: ' + ','.join(missing))
    if str(wan.get('id')) != wan_interface[3:] or wan.get('name') != wan_interface or wan.get('tagname') != wan_interface:
        raise ValueError('wrong WAN')
    if wan.get('bandeth') != usb_interface or wan.get('linkmode') != 0:
        raise ValueError('unsafe WAN configuration')
    # Do not pass an unrelated WAN password into the full-configuration save.
    # Require DHCP mode with no WAN password.
    if wan.get('passwd') not in ('', '[REDACTED]'):
        raise ValueError('nonempty WAN password: refusal to write')
    payload = {key: wan[key] for key in BROWSER_SAVE_KEYS}
    payload['passwd'] = ''
    payload['bandeth'] = usb_interface
    payload['bandif'] = new_mac
    for key in DHCP_EMPTY:
        payload[key] = ''
    for key in DHCP_ZERO:
        payload[key] = 0
    payload['dhcp_status'] = 1
    return {'func_name':'wan','action':'save','param':payload}


def session_login(args):
    # Authenticate against the router web UI to obtain a session.
    if not args.username or not args.password:
        raise RuntimeError('Set IKUAI_USERNAME and IKUAI_PASSWORD, or IKUAI_COOKIE')
    md5hex = hashlib.md5(args.password.encode('utf-8')).hexdigest()
    login_body = {
        'username': args.username,
        'passwd': md5hex,
        'pass': base64.b64encode(('salt_11' + md5hex).encode()).decode(),
        'remember_password': ''
    }
    # /Action/login can return either code=0 or Result=10000 on firmware families.
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
    req = urllib.request.Request(args.url.rstrip('/') + '/Action/login',
                                 data=json.dumps(login_body).encode(), headers=headers, method='POST')
    with args.opener.open(req, timeout=10) as response:
        obj = json.load(response)
    if not isinstance(obj, dict) or not (obj.get('code') == 0 or obj.get('Result') == 10000):
        raise RuntimeError('Action/login failed: code=%r Result=%r message=%r' %
                           (obj.get('code'),obj.get('Result'),obj.get('message',obj.get('ErrMsg'))))
    # Some deployments provide sess_key in JSON rather than Set-Cookie.
    key = obj.get('sess_key')
    if not key and isinstance(obj.get('results'), dict):
        key = obj['results'].get('sess_key')
    if isinstance(key, str) and key:
        args.cookie = 'username=' + args.username + '; login=1; sess_key=' + key
    elif not any(c.name == 'sess_key' for c in args.jar):
        raise RuntimeError('Action/login reported success but no sess_key was provided')
    log('Action/login successful; session created')


def action_call(args, data):
    try:
        return request(args.url, '/Action/call', method='POST', data=data,
                       cookie=args.cookie, insecure=args.insecure,
                       opener=None if args.cookie else args.opener)
    except RuntimeError as exc:
        if 'code=1008' not in str(exc) or args.cookie_supplied:
            raise
        log('Action/call session expired; attempting one re-login')
        session_login(args)
        return request(args.url, '/Action/call', method='POST', data=data,
                       cookie=args.cookie, insecure=args.insecure,
                       opener=None if args.cookie else args.opener)


def action_auth(args):
    # Authorization for /Action/call is not necessarily the same as v4 REST.
    # Check via read-only Action/call POST first, without changing WAN configuration.
    return action_call(args, {
        'func_name':'lan','action':'show',
        'param':{'TYPE':'ether_info,snapshoot,wan_vlan_fail'}})


def load_config(path):
    """Read Python 3.11 TOML config; refuse insecure permissions on Unix."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise RuntimeError(f'Config file not found: {path}')
    if os.name == 'posix':
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise RuntimeError(f'Config {path} permissions are {mode:03o}; run chmod 600 {path}')
    with path.open('rb') as f:
        cfg = tomllib.load(f)
    if not isinstance(cfg, dict):
        raise RuntimeError('Invalid config')
    allowed = {'url', 'token', 'username', 'password', 'cookie', 'insecure',
               'interval', 'stable', 'cooldown', 'apply', 'usb_interface', 'wan_interface', 'heartbeat'}
    extra = set(cfg) - allowed
    if extra:
        raise RuntimeError('Unknown config keys: ' + ', '.join(sorted(extra)))
    return cfg


def config_or_env(cli, envname, cfg, key, default=None):
    if cli is not None:
        return cli
    val = os.getenv(envname)
    return val if val is not None else cfg.get(key, default)


def main():
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument('--config', default=os.getenv('IKUAI_CONFIG', 'config.toml'))
    known, _ = pre.parse_known_args()
    parser = argparse.ArgumentParser(description=__doc__, parents=[pre])
    parser.add_argument('--url', default=None)
    parser.add_argument('--token', default=None)
    parser.add_argument('--insecure', action='store_true', default=None)
    parser.add_argument('--interval', type=float, default=None)
    parser.add_argument('--usb-interface', default=None, help='USB network interface, e.g. usb0')
    parser.add_argument('--wan-interface', default=None, help='Target WAN, e.g. wan4')
    parser.add_argument('--stable', type=int, default=None)
    parser.add_argument('--cooldown', type=float, default=None)
    parser.add_argument('--heartbeat', type=float, default=None)
    parser.add_argument('--apply', action='store_true', default=None, help='Enable actual writes to configured WAN')
    parser.add_argument('--dry-run', action='store_true', help='Force no writes, even if config apply=true')
    parser.add_argument('--auth-test-only', action='store_true', help='Test session login and read-only Action/call; exit without writes')
    parser.add_argument('--cookie', default=None)
    parser.add_argument('--username', default=None)
    parser.add_argument('--password', default=None)
    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError, RuntimeError, tomllib.TOMLDecodeError) as exc:
        parser.error(str(exc))
    for key, envname, default in [
        ('url', 'IKUAI_URL', None),
        ('token', 'IKUAI_TOKEN', None),
        ('username', 'IKUAI_USERNAME', None),
        ('password', 'IKUAI_PASSWORD', None),
        ('cookie', 'IKUAI_COOKIE', None),
        ('interval', 'IKUAI_INTERVAL', 3),
        ('stable', 'IKUAI_STABLE', 3),
        ('cooldown', 'IKUAI_COOLDOWN', 60),
        ('heartbeat', 'IKUAI_HEARTBEAT', 3600),
        ('usb_interface', 'IKUAI_USB_INTERFACE', 'usb0'),
        ('wan_interface', 'IKUAI_WAN_INTERFACE', 'wan4'),
    ]:
        setattr(args, key, config_or_env(getattr(args, key), envname, cfg, key, default))
    # Convert optional environment strings to the types required by polling.
    try:
        args.interval = float(args.interval)
        args.stable = int(args.stable)
        args.cooldown = float(args.cooldown)
        args.heartbeat = float(args.heartbeat)
    except (ValueError, TypeError):
        parser.error('interval/stable/cooldown/heartbeat must be numbers')
    args.insecure = args.insecure if args.insecure is not None else cfg.get('insecure', False)
    args.apply = args.apply if args.apply is not None else cfg.get('apply', False)
    if not isinstance(args.insecure, bool) or not isinstance(args.apply, bool):
        parser.error('insecure/apply must be TOML booleans true or false')
    if args.dry_run or args.auth_test_only:
        args.apply = False
    args.cookie_supplied = bool(args.cookie)
    args.jar = http.cookiejar.CookieJar()
    ctx = ssl._create_unverified_context() if args.insecure else ssl.create_default_context()
    args.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(args.jar),
                                             urllib.request.HTTPSHandler(context=ctx))
    if not args.token:
        parser.error('IKUAI_TOKEN required')
    if args.stable < 2 or args.interval <= 0 or args.cooldown < 0 or args.heartbeat < 0:
        parser.error('invalid polling configuration')
    if not isinstance(args.usb_interface, str) or not re.fullmatch(r'usb[0-9]+', args.usb_interface):
        parser.error('usb_interface must be a name like usb0 or usb1')
    if not isinstance(args.wan_interface, str) or not re.fullmatch(r'wan[1-9][0-9]*', args.wan_interface):
        parser.error('wan_interface must be a name like wan1 or wan4')
    if not isinstance(args.url, str) or not args.url.startswith('https://'):
        parser.error('url must be explicitly configured as an HTTPS URL')
    if args.insecure:
        log('WARNING: TLS verification disabled (trusted LAN only)')
    if args.apply or args.auth_test_only:
        log('Checking Action/call authentication (read-only preflight)')
        if not args.cookie_supplied:
            session_login(args)
        action_auth(args)  # preflight safe read request
        log('Action/call read-only preflight succeeded')
        if args.auth_test_only:
            return
        log(f'APPLY MODE: {args.wan_interface} changes enabled')
    else:
        log('DRY RUN: no configuration writes')
    status = StatusLogger(args.heartbeat)
    last_attempt = -float('inf')
    previous_mac, streak = None, 0
    while True:
        try:
            physical, wan = read_state(args)
            mac, reason = candidate(physical, wan, args.usb_interface, args.wan_interface)
            if not mac:
                previous_mac, streak = None, 0
                status.state(('idle', reason), reason)
            else:
                streak = streak + 1 if previous_mac == mac else 1
                previous_mac = mac
                if streak < args.stable:
                    status.state(('candidate', mac), 'candidate %s MAC=%s (confirming)' % (args.usb_interface, mac))
                elif time.monotonic()-last_attempt < args.cooldown:
                    status.state(('cooldown', mac), 'candidate stable; cooldown active')
                else:
                    last_attempt = time.monotonic()
                    status.state(('rebind', mac), 'candidate confirmed')
                    log('%s currently %s -> %s %s' % (args.wan_interface,wan.get('bandif'),args.usb_interface,mac))
                    if not args.apply:
                        log('DRY RUN: would call wan/save; no changes made')
                    else:
                        fresh_physical, fresh_wan = read_state(args)
                        fresh_mac, fresh_reason = candidate(fresh_physical, fresh_wan, args.usb_interface, args.wan_interface)
                        if fresh_mac != mac:
                            raise RuntimeError('state changed before write: ' + fresh_reason)
                        payload = build_save(fresh_wan, mac, args.usb_interface, args.wan_interface)
                        # Never backup sensitive WAN config by default; request body kept in memory.
                        action_call(args, payload)
                        _, verified = read_state(args)
                        if str(verified.get('bandif','')).lower() != mac or verified.get('bandeth') != args.usb_interface:
                            raise RuntimeError('save returned code=0, but target WAN readback mismatch')
                        log('SUCCESS: %s rebind verified (DHCP may take a few seconds)' % args.wan_interface)
                        previous_mac, streak = None, 0
                        status.state(('bound', mac), 'binding healthy')
        except KeyboardInterrupt:
            log('Stopped')
            return
        except Exception as exc:
            status.error(str(exc))
            previous_mac, streak = None, 0
        status.health()
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log('Stopped')
            return

if __name__ == '__main__':
    main()