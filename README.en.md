# iKuai USB WAN Auto

[简体中文](README.md) | English

Automatically detect USB tethering reconnections and rebind a configured iKuai WAN interface after its network adapter MAC changes.

## Features

- Configurable WAN (such as `wan2`) and USB interface (such as `usb0`), with no manufacturer-specific matching.
- Reconnection detection, stabilization checks, and binding verification.
- Automatic login and session refresh.
- Read-only by default; write only when `apply = true`.
- State-change logging and configurable periodic health summaries.

Supports **DHCP WAN interfaces on iKuai 4.x**. Create the target WAN and bind the USB device once in the router UI before enabling automation. WAN changes use a full-configuration save; test in read-only mode first.

## Requirements

Python 3.11+ (standard library only), network access to iKuai management HTTPS, an API token, and management login credentials.

## Install

The example installation path is `/opt/ikuai-usb-wan-auto`:

```bash
sudo git clone https://github.com/jeblove/ikuai-usb-wan-auto.git /opt/ikuai-usb-wan-auto
cd /opt/ikuai-usb-wan-auto
sudo cp config.example.toml config.toml
sudo chmod 600 config.toml
sudo nano config.toml
```

Set `url`, `token`, `username`, `password`, and optionally `wan_interface` and `usb_interface`. If using a self-signed TLS certificate, evaluate whether `insecure = true` is acceptable on your trusted network.

## Test and run

```bash
sudo python3 ikuai_usb_wan_auto.py --config ./config.toml --auth-test-only
sudo python3 ikuai_usb_wan_auto.py --config ./config.toml --dry-run
```

After verifying the selected interface, set `apply = true` locally and run:

```bash
sudo python3 ikuai_usb_wan_auto.py --config ./config.toml
```

Reconnect the tethering device without manually rebinding. `SUCCESS: wan2 rebind verified` (actual WAN name varies) indicates the binding was updated; check DHCP and internet access separately.

## systemd (optional)

```bash
sudo install -m 644 ikuai-usb-wan.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ikuai-usb-wan.service
sudo journalctl -u ikuai-usb-wan.service -f
```

Restart the service after editing config: `sudo systemctl restart ikuai-usb-wan.service`. For another installation path, update both `WorkingDirectory` and `ExecStart` in the service file.

## Offline tests

```bash
python3 -m unittest discover -s tests -v
```

Covers USB interface selection, WAN write safety checks and state-change logging.

## Notes

- The real `config.toml` includes credentials and is Git-ignored; restrict file permissions to `600`.
- `insecure = true` disables TLS certificate verification.
- Only transitions are logged; default health summary is hourly. Set `heartbeat = 0` to disable summaries.
