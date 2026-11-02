# mijia-curtain-ble

不需要米家网关，用电脑蓝牙直接控制米家 / Aqara 窗帘伴侣（型号 `lumi.curtain.hmcn02`）。

可以做到：

- 命令行打开、关闭、停止、调到指定位置，读取状态（位置、电量等）
- 每天定时自动打开窗帘（cron）
- iPhone 快捷指令 / Siri 一句话开关窗帘（通过 SSH 调用 Mac）

## 准备

- 一台带蓝牙的 Mac，放在窗帘的蓝牙范围内（Linux 理论上可用，未测试）
- Python ≥ 3.10 和 [uv](https://docs.astral.sh/uv/)
- 窗帘已在米家 App 中绑定，能正常使用

```bash
git clone https://github.com/F1sh-Yu/mijia-curtain-ble.git
cd mijia-curtain-ble
uv sync
```

首次运行时 macOS 会请求蓝牙权限，请在「系统设置 → 隐私与安全性 → 蓝牙」中允许你使用的终端。

## 第 1 步：获取窗帘密钥

连接窗帘需要设备密钥（`gatt_ltmk`），它保存在小米云端。用绑定窗帘的米家账号扫码登录获取：

```bash
uv run python -m mibleak.cloud --region cn
```

- `--region` 与米家 App 中的地区一致：中国大陆填 `cn`，其他可选 `de` `us` `ru` `tw` `sg` `in` `i2`
- 命令会生成二维码图片 `.xiaomi-login.png`，打开后用米家 App 扫码并确认；二维码过期就重新运行
- 自动查找失败时，可加 `--did <设备DID>` 指定设备

结果保存在 `~/.config/mijia-curtain-ble/ltmk.json`（仅本人可读），包括设备 DID、名称、MAC 和密钥。
工具不会保存账号密码或登录会话。**这个文件相当于窗帘的钥匙，请勿分享或提交到仓库。**

## 第 2 步：找到窗帘的蓝牙地址

```bash
uv run curtain scan
```

输出示例：

```
XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX  rssi= -60  mac=AA:BB:CC:DD:EE:FF  pid=...
```

找到 `mac` 与 `ltmk.json` 中 MAC 相同的那一行，记下第一列的地址。
macOS 上这个地址是 CoreBluetooth UUID，每台 Mac 都不一样，换电脑需要重新扫描。

## 第 3 步：命令行控制

```bash
export CURTAIN_ADDRESS=<第 2 步的地址>
export CURTAIN_LTMK="$(uv run python -c 'import json,pathlib; d=json.loads((pathlib.Path.home()/".config/mijia-curtain-ble/ltmk.json").read_text()); print(d[0]["gatt_ltmk"])')"
# 有多台窗帘时，把 d[0] 改成对应设备的序号

uv run curtain status        # 读取状态
uv run curtain open          # 打开
uv run curtain close         # 关闭
uv run curtain stop          # 停止
uv run curtain pos 50        # 移动到 50%
```

其他命令：

| 命令 | 作用 |
|---|---|
| `curtain reverse on/off` | 反转电机方向 |
| `curtain handpull on/off` | 开关「轻拉启动」 |
| `curtain limit open/close/clear` | 把当前位置设为打开或关闭的行程终点，或清除终点 |
| `curtain calibrate` | 行程校准 |
| `curtain identify` | 让设备响铃或闪灯，确认是哪一台 |
| `curtain synctime` | 同步设备时间 |
| `curtain watch` | 持续打印状态变化，Ctrl-C 退出 |
| `curtain -v ...` / `-vv ...` | 输出调试日志 |

也可以用 `-a <地址> -k <密钥>` 代替环境变量。

## 第 4 步（可选）：后台服务、Siri、定时打开

`siri-curtain` 是给 Siri 和定时任务用的入口，支持 `open`、`close`、`status`、`ensure-open`。
macOS 不允许 SSH 会话直接使用蓝牙，所以它分成两部分：

- 后台服务（`siri-curtain serve`）：常驻在你的登录会话里，负责操作蓝牙
- 其他调用（SSH、cron）：通过只有当前用户能访问的本地 socket，把命令发给后台服务

### 4.1 配置设备

创建 `~/.config/mijia-curtain-ble/device.json`：

```json
{"address": "<第 2 步的地址>", "mac": "<窗帘 MAC>"}
```

也可以改用环境变量 `CURTAIN_ADDRESS` 和 `CURTAIN_MAC`。

### 4.2 登录后自动启动后台服务

创建 `~/Library/LaunchAgents/com.example.mijia-curtain.plist`。把 `<项目路径>` 换成仓库的绝对路径，
`<用户目录>` 换成你的 home 目录（如 `/Users/xxx`）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>com.example.mijia-curtain</string>
	<key>ProgramArguments</key>
	<array>
		<string><项目路径>/siri-curtain</string>
		<string>serve</string>
	</array>
	<key>WorkingDirectory</key>
	<string><项目路径></string>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>ThrottleInterval</key>
	<integer>10</integer>
	<key>StandardOutPath</key>
	<string><用户目录>/.config/mijia-curtain-ble/service.log</string>
	<key>StandardErrorPath</key>
	<string><用户目录>/.config/mijia-curtain-ble/service-error.log</string>
</dict>
</plist>
```

加载并测试：

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.mijia-curtain.plist
./siri-curtain status
```

修改代码或配置后，重启服务：

```bash
launchctl kickstart -k gui/$(id -u)/com.example.mijia-curtain
```

注意：

- 服务在用户登录后才会启动；Mac 需要保持唤醒
- 不用自启服务的话，也可以在本机终端手动运行 `./siri-curtain serve`。不要重复启动，也不要在 SSH 里启动

### 4.3 iPhone 快捷指令 / Siri

iPhone 和 Mac 要连在同一局域网，并在 Mac 上打开「系统设置 → 通用 → 共享 → 远程登录」。

在「快捷指令」App 中新建「打开窗帘」，添加操作「通过 SSH 运行脚本」：

- 主机：`<Mac 主机名>.local` 或 Mac 的局域网 IP
- 端口：`22`
- 用户：你的 macOS 用户名
- 认证：Mac 登录密码，或配置 SSH 密钥
- 脚本：`<项目路径>/siri-curtain open`

用同样的方法创建「关闭窗帘」，把脚本参数改为 `close`。之后对 Siri 说快捷指令的名称即可。

建议先把参数设为 `status` 运行一次，确认 SSH 和蓝牙都正常。
可以在 SSH 操作后加一个「朗读文本」，内容选 SSH 的输出，这样能听到执行结果。
提示「已发送」表示指令已经送达，不代表窗帘已经开到头或关到底。

### 4.4 每天定时打开

运行 `crontab -e`，添加一行（时间按 Mac 的系统时区）：

```cron
0 8 * * * <项目路径>/siri-curtain ensure-open >> ~/.config/mijia-curtain-ble/morning-open.log 2>&1
```

`ensure-open` 会先读取位置：已经全开就跳过，否则只发送一次打开指令。
连接失败时最多尝试 3 次；读不到有效位置就不动作。

cron 不会唤醒电脑。Mac 睡眠或关机时错过的任务，事后不会补执行。

## 常见问题

- **`login denied by device (wrong LTMK?)`**：密钥不对。确认选中的是 `ltmk.json` 里 MAC 与扫描结果一致的那台设备，或重新执行第 1 步。
- **扫描不到设备**：确认终端有蓝牙权限，并离窗帘足够近。如果手机上的米家 App 正连着窗帘，先关掉 App 再试。
- **`本地窗帘服务未启动`**：后台服务没在运行。检查 4.2 的配置，或查看 `~/.config/mijia-curtain-ble/service-error.log`。
- **偶尔超时**：蓝牙连接不稳定，重试即可。可以用 `curtain -vv status` 查看详细日志。
- **米家的 `token`、`beaconkey` 能代替密钥吗**：不能，必须用第 1 步获取的 `gatt_ltmk`。

## 致谢

小米云扫码登录和请求签名参考了 [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor)。

## 免责声明

本项目仅供个人学习使用，与小米、Aqara 无关，使用风险自负。
