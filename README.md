# mijia-curtain-ble

不经过米家 App 或网关，用 Python（bleak）直接通过蓝牙控制米家/Aqara 窗帘伴侣（`lumi.curtain.hmcn02`）。

协议是从米家 iOS 11.8.204 的主程序和窗帘插件 `Plugin_1025799`（com.lumi.curtain 1.0.65）逆向还原的。已在真机上验证状态读取与开关控制；回环测试使用模拟设备。

## 用法

```bash
uv sync
uv run curtain scan                        # 找设备：macOS 上的地址是 CoreBluetooth UUID，MAC 从 MiBeacon 广播里解析
export CURTAIN_ADDRESS=<scan 列出的地址>
export CURTAIN_LTMK=<gatt_ltmk 十六进制>
uv run curtain -v status
uv run curtain open | close | stop | pos 50
uv run curtain reverse on | handpull off | limit open | calibrate | identify
uv run curtain watch                       # 打印 properties_changed 和事件
uv run curtain get 2.6 5.1 / set 2.2 uint8 30 / action 2.1
```

### 获取 LTMK

使用绑定设备的米家账号扫码登录，地区与 App 保持一致（中国大陆为 `cn`）：

```bash
uv run python -m mibleak.cloud --region cn
```

打开命令输出的 `.xiaomi-login.png`，用米家 App 扫码并确认。工具自动查找
`lumi.curtain.hmcn02`，调用 `POST /v2/blemesh/query_dev` 获取 `result.gatt_ltmk`。
也可以加 `--did <设备DID>` 指定设备。二维码过期后重新运行命令。

结果保存至 `~/.config/mijia-curtain-ble/ltmk.json`（权限 `600`），包含 DID、名称、MAC
和密钥；不会保存账号密码或云端登录会话，也不会将密钥打印到日志。多个窗帘按记录区分。
单台窗帘可这样载入密钥：

```bash
export CURTAIN_LTMK="$(uv run python -c 'import json,pathlib; d=json.loads((pathlib.Path.home()/".config/mijia-curtain-ble/ltmk.json").read_text()); assert len(d)==1, "多个设备，请按 DID 选择"; print(d[0]["gatt_ltmk"])')"
```

`token`、`beaconkey` 不能代替 `gatt_ltmk`。登录和签名协议参考
[Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor)。

## iPhone Siri

`siri-curtain` 支持 `open`、`close`、`status`、`ensure-open`。设备地址从环境变量
`CURTAIN_ADDRESS` / `CURTAIN_MAC` 或 `~/.config/mijia-curtain-ble/device.json` 读取：

```json
{"address": "<curtain scan 列出的 CoreBluetooth UUID>", "mac": "<窗帘 MAC>"}
```

SSH 会话直接访问蓝牙曾返回 `DENIED_BY_UNKNOWN`，因此调用端通过仅当前用户
可访问的 Unix socket 请求本地服务，由本地服务读取密钥和操作蓝牙。
CoreBluetooth 地址因 Mac 而异，换机器需重新 `curtain scan`。

可安装登录自启服务（LaunchAgent），例如
`~/Library/LaunchAgents/com.example.mijia-curtain.plist`，`ProgramArguments` 为
`<项目路径>/siri-curtain serve`。登录后自动启动，进程异常退出会自动重启，已验证该启动方式可读取蓝牙状态。
无需保持终端窗口打开。Mac 必须保持唤醒；开机但尚未登录时服务不会启动。

手动重启服务：

```bash
launchctl kickstart -k gui/$(id -u)/com.example.mijia-curtain
```

日志位于 `~/.config/mijia-curtain-ble/service.log` 和 `service-error.log`。
不使用自启服务时可在有蓝牙权限的本地环境运行 `siri-curtain serve`，不要重复启动，
也不要从 SSH 会话直接启动蓝牙服务。

iPhone 与 Mac 连接同一局域网。在 iPhone「快捷指令」创建「打开窗帘」和「关闭窗帘」，
分别添加「通过 SSH 运行脚本」：

- 主机：`<Mac 主机名>.local` 或局域网 IP
- 端口：`22`；用户：`<macOS 用户名>`
- 认证：Mac 登录密码，直接在 iPhone 输入；也可以配置 SSH 密钥认证
- 打开脚本：`<项目路径>/siri-curtain open`
- 关闭脚本：`<项目路径>/siri-curtain close`

先把脚本参数设为 `status`，手动运行验证 SSH 和蓝牙权限，再改成 `open` / `close`。
可以在 SSH 动作后添加「朗读文本」，
内容选择 SSH 的输出。操作反馈表示指令已发送，不表示窗帘已到达终点。

Mac 需要保持唤醒并位于窗帘蓝牙范围内。已验证本地服务的状态读取和定时打开；
iPhone 经 SSH 访问和 Siri 调用需要在手机端完成验证。

## 每天早上自动打开

在当前用户的 crontab 中每天 08:00 执行 `siri-curtain ensure-open`（使用 Mac 系统时区）：

```cron
0 8 * * * <项目路径>/siri-curtain ensure-open >> ~/.config/mijia-curtain-ble/morning-open.log 2>&1
```

命令通过本地服务读取位置：100% 时跳过；有效位置小于 100% 时只发送一次打开指令。
连接/认证或读取状态发生超时、蓝牙错误时，最多尝试 3 次（首次加 2 次重试），
每次断开连接后等待 5 秒，用新会话重新连接和读取。单次连接/认证限时 40 秒，
状态读取限时 15 秒；断开清理限时 5 秒，清理失败则停止，不继续重试。
位置无效或 3 次均失败时不操作；打开指令最多发送一次，发送超时不重试。
日志记录在 `~/.config/mijia-curtain-ble/morning-open.log`。
各次尝试的阶段、耗时和错误记录在 `~/.config/mijia-curtain-ble/service.log`。

Mac 需已登录、保持唤醒并在窗帘蓝牙范围内；
cron 不负责唤醒电脑，睡眠或关机错过的 08:00 任务不会补执行。

## 协议摘要（逆向结论）

| 层 | 内容 | 来源（MiHome 二进制中的类） |
|---|---|---|
| GATT | 服务 FE95；`0010` 认证控制，`0016` 认证数据，`001A` 写 spec，`001B` 收 spec；全部开通知，写入不带响应 | MHXiaoMiBleMeshLoginFiniteStateMachine、MHXiaoMiBleSpecProcessCommand、MHXiaoMiBleSpecNotificationReceiver |
| 分包传输 | `000000<type><cnt16>` → `00000101` → `<idx16>+≤18B` × cnt → `00000100`；`00000105<idx>` 表示请求重传 | MHXiaomiBleProcessMutilWriteCommand |
| Mesh 登录 | 往 0010 写 `50000000`；0016 上用类型 03 互换 64B P-256 公钥；`sk = HKDF-SHA256(ECDH ‖ LTMK, "miot-mesh-login-salt", "miot-mesh-login-info", 64)`；类型 05 发送 `AES-CCM(sk[16:32], nonce=101112…1b, tag4, crc32(dev_pub) LE)`；0010 回 `51000000` 表示成功，`52000000` 表示拒绝 | generateSessionKeyWithShareSecret:、encryptData: |
| 会话加密 | `devKey=sk[0:16] appKey=sk[16:32] devIv=sk[32:36] appIv=sk[36:40]`；发送：`cnt16 ‖ CCM(appKey, appIv‖0000‖cnt32)`；接收：`CCM(devKey, devIv‖0000‖cnt16‖0000)`，tag 都是 4 字节 | MHBtDeviceXiaoMi setSession: / encryptDataToMsc: / decryptDataFromMsc: |
| Spec 帧 | `hdr16=(len)\|(ver<<12)`，`tid16`，`op8`；set/get：`n8 + [siid8 piid16 (typelen16 value)]`；action：`siid8 aiid8 n8 + [piid16 typelen value]`；typelen=`(type<<12)\|len` | MHXiaoMiBleSpecProtocolUtils |
| opcode | 0 set，1 set-rsp，2 get，3 get-rsp，4 properties_changed，5 action，6 action-rsp，7 event，0xF0 protocol_exchange | bleSpecOpcode: |

## 已知不确定点

- 设备主动发起的单包（`000002…`）的 ACK 格式是推测的：`00000300`。
- get-rsp 里 code 不为 0 时，假定该项后面不带值。
- 写入用不用带响应的 write，是根据特征属性自动选的。
- 云端 LTMK 可能有加密版本（`p_isVersionSupportEncryptLtmk`），但代码路径显示这只用于带安全码的门锁类设备，窗帘不涉及。
