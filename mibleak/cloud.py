"""Read a curtain's GATT LTMK using Xiaomi's QR login and cloud API.

Protocol reference: https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor
No password or cloud session is persisted.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import time

from Crypto.Cipher import ARC4
import qrcode
import requests


def b64(value: bytes) -> str:
    return base64.b64encode(value).decode()


def signature(path: str, key: bytes, fields: dict) -> str:
    parts = ["POST", path, *(f"{k}={v}" for k, v in fields.items()), b64(key)]
    return b64(hashlib.sha1("&".join(parts).encode()).digest())


def crypt(key: bytes, data: bytes) -> bytes:
    return ARC4.new(key, drop=1024).encrypt(data)


def extract_ltmk(result: dict) -> str:
    value = result.get("gatt_ltmk")
    if not isinstance(value, str) or len(value) not in (32, 64):
        raise ValueError("云端未返回有效的 gatt_ltmk；不能用 token 或 beaconkey 代替")
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        raise ValueError("云端 gatt_ltmk 不是十六进制") from None
    if len(raw) not in (16, 32):
        raise ValueError("云端 gatt_ltmk 长度异常")
    return raw.hex()


def save_private(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data.encode() if isinstance(data, str) else data)


class XiaomiCloud:
    def __init__(self, region: str):
        self.session = requests.Session()
        self.base = f"https://{'' if region == 'cn' else region + '.'}api.io.mi.com/app"

    def login(self, qr_path: Path) -> None:
        response = self.session.get("https://account.xiaomi.com/longPolling/loginUrl", params={
            "_qrsize": "480", "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
            "callback": "https://sts.api.io.mi.com/sts", "_hasLogo": "false",
            "sid": "xiaomiio", "serviceParam": "", "_locale": "zh_CN",
            "_dc": str(int(time.time() * 1000)),
        }, timeout=30)
        response.raise_for_status()
        info = json.loads(response.text.removeprefix("&&&START&&&"))
        if not all(k in info for k in ("loginUrl", "lp", "timeout")):
            raise ValueError("小米未返回扫码登录信息")
        import io
        image = io.BytesIO()
        qrcode.make(info["loginUrl"]).save(image, format="PNG")
        save_private(qr_path, image.getvalue())
        print(f"请用米家 App 扫码并确认登录：{qr_path}", flush=True)
        deadline = time.monotonic() + min(float(info["timeout"]), 300)
        try:
            while time.monotonic() < deadline:
                try:
                    response = self.session.get(info["lp"], timeout=10)
                except requests.Timeout:
                    continue
                except requests.ConnectionError:
                    time.sleep(1)
                    continue
                response.raise_for_status()
                login = json.loads(response.text.removeprefix("&&&START&&&"))
                if not all(k in login for k in ("userId", "ssecurity", "location")):
                    raise ValueError("扫码未完成或二维码已失效，请重新运行")
                self.security = login["ssecurity"]
                response = self.session.get(login["location"], timeout=30)
                response.raise_for_status()
                token = next((c.value for c in self.session.cookies if c.name == "serviceToken"), None)
                if not token:
                    raise ValueError("登录未获得 serviceToken")
                self.cookies = {"userId": str(login["userId"]), "serviceToken": token,
                                "yetAnotherServiceToken": token, "locale": "zh_CN"}
                print("小米账号登录成功，正在查询设备。", flush=True)
                return
            raise ValueError("二维码已过期，请重新运行")
        finally:
            qr_path.unlink(missing_ok=True)

    def call(self, path: str, payload: dict) -> dict:
        nonce = os.urandom(8) + int(time.time() / 60).to_bytes(4, "big")
        key = hashlib.sha256(base64.b64decode(self.security) + nonce).digest()
        fields = {"data": json.dumps(payload, separators=(",", ":"))}
        fields["rc4_hash__"] = signature(path, key, fields)
        fields = {k: b64(crypt(key, v.encode())) for k, v in fields.items()}
        fields.update(signature=signature(path, key, fields), ssecurity=self.security, _nonce=b64(nonce))
        response = self.session.post(self.base + path, data=fields, cookies=self.cookies,
            headers={"User-Agent": "Android APP/com.xiaomi.mihome APPV/10.5.201",
                     "Accept-Encoding": "identity", "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
                     "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2"}, timeout=30)
        response.raise_for_status()
        try:
            result = response.json()
        except ValueError:
            result = json.loads(crypt(key, base64.b64decode(response.text)))
        if result.get("code") != 0:
            raise ValueError(f"小米接口 {path} 失败，code={result.get('code')}；请检查地区和设备所属账号")
        return result["result"]

    def devices(self) -> list[dict]:
        homes = self.call("/v2/homeroom/gethome", {
            "fg": True, "fetch_share": True, "fetch_share_dev": True, "limit": 300, "app_ver": 7})
        devices = {}
        for home in homes.get("homelist", []):
            result = self.call("/v2/home/home_device_list", {
                "home_owner": home.get("uid", self.cookies["userId"]), "home_id": home["id"], "limit": 200,
                "get_split_device": True, "support_smart_home": True})
            for device in result.get("device_info", []):
                devices[str(device["did"])] = device
        return list(devices.values())


def main() -> None:
    parser = argparse.ArgumentParser(description="扫码读取米家窗帘 gatt_ltmk")
    parser.add_argument("--region", choices=["cn", "de", "us", "ru", "tw", "sg", "in", "i2"], default="cn")
    parser.add_argument("--did", help="指定设备 DID；省略时查询 lumi.curtain.hmcn02")
    parser.add_argument("--output", type=Path, default=Path.home() / ".config/mijia-curtain-ble/ltmk.json")
    parser.add_argument("--qr", type=Path, default=Path(".xiaomi-login.png"))
    args = parser.parse_args()
    cloud = XiaomiCloud(args.region)
    try:
        cloud.login(args.qr.resolve())
        devices = ([{"did": args.did}] if args.did else [
            d for d in cloud.devices() if d.get("model") == "lumi.curtain.hmcn02"])
        if not devices:
            raise ValueError("此地区未找到 lumi.curtain.hmcn02；确认设备绑定账号、地区，或用 --did 指定设备")
        records = []
        for device in devices:
            result = cloud.call("/v2/blemesh/query_dev", {"did": str(device["did"])})
            records.append({k: device[k] for k in ("did", "name", "model", "mac") if k in device})
            records[-1]["gatt_ltmk"] = extract_ltmk(result)
        save_private(args.output, json.dumps(records, ensure_ascii=False, indent=2) + "\n")
        print(f"成功获取 {len(records)} 台设备的 gatt_ltmk，已保存至 {args.output}（权限 600）。")
    except (ValueError, KeyError, requests.RequestException) as exc:
        # Request exception text can contain credential-bearing URLs.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        parser.exit(1, f"获取失败：{message}\n")
    finally:
        cloud.session.close()


if __name__ == "__main__":
    main()
