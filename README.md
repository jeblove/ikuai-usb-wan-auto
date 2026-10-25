# iKuai USB WAN Auto

简体中文 | [English](README.en.md)

自动检测 USB 网络共享设备重连，并在网卡 MAC 变化后重新绑定 iKuai 指定 WAN 接口。

## 功能

- 配置目标 WAN（如 `wan2`）和 USB 网卡（如 `usb0`），不限制设备品牌。
- 重连检测、防抖与绑定结果校验。
- 自动登录，Session 失效后重新认证。
- 默认只读；仅在 `apply = true` 时修改 WAN 配置。
- 状态变化日志及可配置的定期健康摘要。

当前适用于 iKuai 4.x 的 **DHCP 类型 WAN**。使用前，需要先在路由器管理页面创建目标 WAN 并绑定一次 USB 网卡。WAN 写入使用完整配置保存，请先进行只读验证。

## 环境要求

Python 3.11+（仅标准库）、可访问 iKuai 管理 HTTPS 的设备、iKuai API Token 和管理登录账号密码。

## 安装

以下示例统一安装到 `/opt/ikuai-usb-wan-auto`，需要 Git 和 Python 3.11+：

```bash
sudo git clone https://github.com/jeblove/ikuai-usb-wan-auto.git /opt/ikuai-usb-wan-auto
cd /opt/ikuai-usb-wan-auto
sudo cp config.example.toml config.toml
sudo chmod 600 config.toml
sudo nano config.toml
```

编辑 `config.toml`：填写 `url`、`token`、`username`、`password`，并按需要修改 `wan_interface`、`usb_interface`。如果使用自签名证书，请在可信网络中自行决定是否设置 `insecure = true`。

## 测试与运行

```bash
sudo python3 ikuai_usb_wan_auto.py --config ./config.toml --auth-test-only
sudo python3 ikuai_usb_wan_auto.py --config ./config.toml --dry-run
```

确认目标接口正确后，将配置中的 `apply` 改为 `true`，然后运行：

```bash
sudo python3 ikuai_usb_wan_auto.py --config ./config.toml
```

重新连接 USB 网络共享设备，无需手动绑定；出现 `SUCCESS: wan2 rebind verified`（接口名以实际配置为准）说明绑定已更新。DHCP 地址及实际联网情况需另外确认。

## systemd 常驻（可选）

```bash
sudo install -m 644 ikuai-usb-wan.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ikuai-usb-wan.service
sudo journalctl -u ikuai-usb-wan.service -f
```

修改配置后执行 `sudo systemctl restart ikuai-usb-wan.service`。如使用其他安装目录，请同时修改服务文件中的 `WorkingDirectory` 与 `ExecStart`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

不连接路由器即可测试 USB 网卡选择、WAN 配置保护和日志状态变化。

## 注意

- `config.toml` 含凭据且已被 Git 忽略，建议权限设为 `600`。
- `insecure = true` 会跳过 HTTPS 证书验证。
- 日志只在状态变化时输出；默认每小时输出一次健康摘要，可设置 `heartbeat = 0` 关闭。


## 使用 Web 管理会话自动绑定（不依赖 API Token）

新增 `ikuai_usb_wan_auto_webui.py`。适用于 REST API Token 已撤销但管理员仍允许通过 Web 会话登录的 iKuai 4.x DHCP WAN；复用原项目的 USB 设备校验与完整保存字段保护。

配置文件仅需 `url`、`username`、`password`，以及 `wan_interface = "wan4"`、`usb_interface = "usb0"` 等常规选项；**无需 `token`**。建议将配置文件存放在受控私有目录并设置 `chmod 600`。

首次只读检查：

```bash
python3 ikuai_usb_wan_auto_webui.py --config /path/to/config.toml --dry-run --once
```

确认目标是独立的 DHCP WAN、USB 物理网卡确实出现且旧控制服务已经停止后，先准备仅供服务账户读写的私有状态目录（权限 700）：

```bash
install -d -m 700 /var/lib/ikuai-usb-wan
# config.toml 中还必须显式配置 apply = true
python3 ikuai_usb_wan_auto_webui.py --config /path/to/config.toml --apply
```

**重要限制：**

- 本脚本不会主动控制 PVE USB 透传，也不会自动开启手机上的 USB 网络共享；如果爱快没有发现 `usb0`，它只记录缺失。
- 默认只读；执行写入同时要求 CLI `--apply` 和配置 `apply = true`。不可与旧的自动绑定实例同时启用。
- 仅在网卡已被路由器识别为 USB、链路有效、未被其他 WAN 使用、MAC 与指定 WAN 绑定不符等条件同时成立时，才会尝试完整配置保存。
- 保存前创建私有 `/var/lib/ikuai-usb-wan/unverified` 状态标记；写后回读完全成功才删除。若写入或回读不确定，后续自动写入被阻止，需要人工确认并移除标记，防止重启后盲目重试。
- 绑定成功不等于 WAN 已获取 DHCP、公网可以访问，更不证明所有游戏 UDP 已按预期分流。
- 未核实多线负载、默认路由与移动流量策略之前，切勿将自动绑定视为移动流量安全保障。

## 可选 MAC 前缀白名单（动态 MAC 自动重绑）

**不需要、也不应该在配置文件中写死整台手机的 MAC。** 每次检测到 `usb0` 的真实 USB 网卡后，程序都会读取其当前完整 MAC；如与目标 WAN 绑定值不同且安全检查通过，才将 **iKuai WAN 所必需的完整 `bandif`** 更新为当前 MAC。此前绑定的 MAC 不用手动维护。

现在两种后端均支持 `config.toml` 中的可选 `mac_prefixes`：

```toml
wan_interface = "wan4"
usb_interface = "usb0"

# 默认推荐：不限制 MAC 前缀，允许随机 MAC 和更换手机
mac_prefixes = []

# 如果确知设备前 3 字节稳定，可改为：
# mac_prefixes = ["02:ab:cd"]
# 可添加多个已认可的前缀；字母大小写不敏感
```

`mac_prefixes` 必须是 TOML 字符串数组，每项 2～5 个完整十六进制字节（例如 `02:ab` 或 `02:ab:cd`）；最多 16 项。**按字节边界匹配**，不能使用通配符、半个字节或单字节宽泛前缀。格式不合法时启动直接报错，拒绝读写。留空数组表示不启用该白名单，以兼容手机随机 MAC。

前缀白名单只是**附加限制**，不是设备身份认证；共享同一前缀的其他设备也可能匹配。无论是否使用前缀，仍需满足：指定物理 `usbN`、USB 驱动和类型、链路已启动、未被其他 WAN 占用、无重复 MAC、完整 WAN 保存字段等检查。只更改当前目标 WAN 的绑定 MAC，其余配置保持原状。

**注意**：有的系统会连前缀也随机化，配置了固定前缀后可能因不匹配而拒绝绑定；这种情况下请保持 `mac_prefixes = []`。前缀过滤无法防止有意伪造 MAC。PVE USB 透传和手机的 USB 共享开关是独立前置条件，脚本无法自行保证其一直在线。

无 Token Web 会话后端与原 REST Token 后端均支持此前缀设置，生效前先运行各自的 `--dry-run` 并检查日志。
