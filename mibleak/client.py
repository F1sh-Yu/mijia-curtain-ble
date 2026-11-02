"""BLE client for Xiaomi mesh-login devices speaking the MIoT BLE spec protocol."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from . import crypto, frames, transport
from .frames import Prop

log = logging.getLogger(__name__)

SERVICE_UUID = "0000fe95-0000-1000-8000-00805f9b34fb"
CH_AUTH = "0010"         # login control: 50000000 -> 51000000 (ok) / 52000000 (denied)
CH_SECURE_AUTH = "0016"  # login data: public keys, confirm code
CH_SPEC_WRITE = "001a"   # encrypted spec requests (app -> device)
CH_SPEC_NOTIFY = "001b"  # encrypted spec responses / property changes (device -> app)

LOGIN_START = bytes.fromhex("50000000")
LOGIN_OK = bytes.fromhex("51000000")
LOGIN_DENIED = bytes.fromhex("52000000")


def char_uuid(short: str) -> str:
    return f"0000{short.lower()}-0000-1000-8000-00805f9b34fb"


class SpecError(Exception):
    pass


@dataclass
class Advert:
    device: BLEDevice
    rssi: int
    product_id: int | None
    mac: str | None


def parse_mibeacon(sd: bytes) -> tuple[int | None, str | None]:
    """MiBeacon service data: frame_ctrl u16, product_id u16, frame_cnt u8, [mac 6 bytes reversed]."""
    if len(sd) < 5:
        return None, None
    fc = int.from_bytes(sd[0:2], "little")
    pid = int.from_bytes(sd[2:4], "little")
    mac = None
    if fc & 0x10 and len(sd) >= 11:
        mac = ":".join(f"{b:02X}" for b in reversed(sd[5:11]))
    return pid, mac


async def scan(timeout: float = 8.0) -> list[Advert]:
    found: dict[str, Advert] = {}

    def cb(dev: BLEDevice, adv: AdvertisementData) -> None:
        sd = adv.service_data.get(SERVICE_UUID)
        if sd is None:
            return
        pid, mac = parse_mibeacon(sd)
        prev = found.get(dev.address)
        found[dev.address] = Advert(dev, adv.rssi, pid or (prev and prev.product_id),
                                    mac or (prev and prev.mac))

    async with BleakScanner(cb):
        await asyncio.sleep(timeout)
    return sorted(found.values(), key=lambda a: -a.rssi)


class BleakLink:
    def __init__(self, client: BleakClient):
        self.client = client
        self._bufs: dict[str, transport.NotifyBuffer] = {}
        self._no_rsp: dict[str, bool] = {}

    def buffer(self, char: str) -> transport.NotifyBuffer:
        return self._bufs.setdefault(char, transport.NotifyBuffer())

    async def enable(self, char: str, on_message: Callable[[bytes], None] | None = None) -> None:
        buf = self.buffer(char)
        c = self.client.services.get_characteristic(char_uuid(char))
        if c is None:
            raise SpecError(f"characteristic {char} not found under FE95")
        self._no_rsp[char] = "write-without-response" in c.properties

        def handler(_: object, data: bytearray) -> None:
            log.debug("<- %s %s", char, bytes(data).hex())
            if on_message is not None:
                on_message(bytes(data))
            else:
                buf.push(bytes(data))

        await self.client.start_notify(c, handler)

    async def write(self, char: str, data: bytes) -> None:
        log.debug("-> %s %s", char, data.hex())
        await self.client.write_gatt_char(char_uuid(char), data, response=not self._no_rsp.get(char, True))


class MiMeshDevice:
    """Mesh-login (LTMK) Xiaomi BLE device, e.g. lumi.curtain.hmcn02."""

    def __init__(self, address: str | BLEDevice, ltmk: bytes, version: int = frames.DEFAULT_VERSION,
                 link: transport.Link | None = None):
        if len(ltmk) not in (16, 32):
            log.warning("unexpected LTMK length %d", len(ltmk))
        self.address = address
        self.ltmk = ltmk
        self.version = version
        self.client = BleakClient(address) if link is None else None
        self.link = link if link is not None else BleakLink(self.client)
        self.session: crypto.Session | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._rx_task: asyncio.Task | None = None
        self.on_props_changed: Callable[[list[dict]], None] | None = None
        self.on_event: Callable[[frames.Frame], None] | None = None

    async def __aenter__(self) -> "MiMeshDevice":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        if self.client is not None:
            await self.client.connect()
            for ch in (CH_AUTH, CH_SECURE_AUTH, CH_SPEC_WRITE, CH_SPEC_NOTIFY):
                await self.link.enable(ch)
        await self.login()
        self._rx_task = asyncio.create_task(self._rx_loop())

    async def disconnect(self) -> None:
        if self._rx_task:
            self._rx_task.cancel()
        if self.client is not None and self.client.is_connected:
            await self.client.disconnect()

    async def login(self) -> None:
        link = self.link
        await link.write(CH_AUTH, LOGIN_START)
        priv, app_pub = crypto.gen_keypair()
        await transport.send(link, CH_SECURE_AUTH, transport.TYPE_PUBKEY, app_pub)
        dev_pub = await transport.recv(link, CH_SECURE_AUTH, transport.TYPE_PUBKEY)
        if len(dev_pub) != 64:
            raise SpecError(f"device public key has unexpected length {len(dev_pub)}")
        sk = crypto.derive_session_key(crypto.ecdh(priv, dev_pub), self.ltmk)
        await transport.send(link, CH_SECURE_AUTH, transport.TYPE_LOGIN_CONFIRM, crypto.login_confirm(sk, dev_pub))
        while True:
            msg = await transport._next(link, CH_AUTH, 10.0)
            if msg[:4] == LOGIN_OK:
                break
            if msg[:4] == LOGIN_DENIED:
                raise SpecError("login denied by device (wrong LTMK?)")
            log.debug("auth channel: %s", msg.hex())
        self.session = crypto.Session.from_key(sk)
        log.info("login ok")

    async def _rx_loop(self) -> None:
        while True:
            try:
                enc = await transport.recv(self.link, CH_SPEC_NOTIFY, transport.TYPE_SPEC, timeout=3600)
                frame = frames.parse(self.session.decrypt(enc))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep the loop alive on a bad frame
                log.warning("rx error: %s", e)
                continue
            fut = self._pending.pop(frame.tid, None)
            if fut is not None and not fut.done():
                fut.set_result(frame)
            elif frame.opcode == frames.OP_PROPS_CHANGED and self.on_props_changed:
                self.on_props_changed(frame.items)
            elif frame.opcode == frames.OP_EVENT and self.on_event:
                self.on_event(frame)
            else:
                log.debug("unsolicited frame op=%#x tid=%d %s", frame.opcode, frame.tid, frame.items)

    async def _request(self, raw: bytes, timeout: float = 10.0) -> frames.Frame:
        if self.session is None:
            raise SpecError("not logged in")
        tid = int.from_bytes(raw[2:4], "little")
        fut = asyncio.get_running_loop().create_future()
        self._pending[tid] = fut
        try:
            await transport.send(self.link, CH_SPEC_WRITE, transport.TYPE_SPEC, self.session.encrypt(raw))
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(tid, None)

    async def get(self, props: list[tuple[int, int]]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(props), 6):  # the plugin batches reads by 6
            batch = [Prop(s, p) for s, p in props[i:i + 6]]
            out += (await self._request(frames.build_get(batch, version=self.version))).items
        return out

    async def set(self, props: list[Prop]) -> list[dict]:
        return (await self._request(frames.build_set(props, version=self.version))).items

    async def action(self, siid: int, aiid: int, args: list[Prop] | None = None) -> frames.Frame:
        f = await self._request(frames.build_action(siid, aiid, args, version=self.version))
        if f.code != 0:
            raise SpecError(f"action {siid}.{aiid} failed: code {f.code}")
        return f

    async def protocol_exchange(self) -> int:
        f = await self._request(frames.build_protocol_exchange(self.version))
        return f.version
