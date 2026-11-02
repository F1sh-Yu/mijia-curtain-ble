"""End-to-end test against a simulated device (device side mirrors the reconstructed protocol)."""
import asyncio
import os
import struct
import zlib

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from mibleak import crypto, frames, transport
from mibleak.client import CH_AUTH, CH_SECURE_AUTH, CH_SPEC_NOTIFY, CH_SPEC_WRITE, MiMeshDevice, SpecError
from mibleak.curtain import Curtain


class Pipe:
    """One side of a bidirectional fake GATT link."""

    def __init__(self):
        self.peer: "Pipe" = None
        self._bufs = {}

    def buffer(self, char):
        return self._bufs.setdefault(char, transport.NotifyBuffer())

    async def write(self, char, data):
        self.peer.buffer(char).push(data)


def make_link():
    app, dev = Pipe(), Pipe()
    app.peer, dev.peer = dev, app
    return app, dev


class FakeCurtain:
    def __init__(self, link, ltmk):
        self.link, self.ltmk = link, ltmk
        self.props = {(2, 6): 30, (2, 2): 30, (2, 3): 0, (5, 1): 88}
        self.tx_cnt = 0

    async def run(self):
        msg = await self.link.buffer(CH_AUTH).get(5)
        assert msg == bytes.fromhex("50000000")
        app_pub = await transport.recv(self.link, CH_SECURE_AUTH, transport.TYPE_PUBKEY)
        priv, dev_pub = crypto.gen_keypair()
        await transport.send(self.link, CH_SECURE_AUTH, transport.TYPE_PUBKEY, dev_pub)
        sk = crypto.derive_session_key(crypto.ecdh(priv, app_pub), self.ltmk)
        confirm = await transport.recv(self.link, CH_SECURE_AUTH, transport.TYPE_LOGIN_CONFIRM)
        try:
            plain = AESCCM(sk[16:32], tag_length=4).decrypt(crypto.LOGIN_CONFIRM_NONCE, confirm, None)
            ok = plain == struct.pack("<I", zlib.crc32(dev_pub))
        except Exception:
            ok = False
        await self.link.write(CH_AUTH, bytes.fromhex("51000000" if ok else "52000000"))
        if not ok:
            return
        self.sess = crypto.Session.from_key(sk)
        while True:
            enc = await transport.recv(self.link, CH_SPEC_WRITE, transport.TYPE_SPEC, timeout=30)
            req = frames.parse(self.sess._device_decrypt(enc))
            await self.reply(req)

    async def reply(self, req):
        tid = req.tid
        if req.opcode == frames.OP_GET:
            payload = bytes([len(req.items)])
            for it in req.items:
                key = (it["siid"], it["piid"])
                if key in self.props:
                    payload += struct.pack("<BHh", *key, 0) + frames.encode_value(frames.T_UINT8, self.props[key])
                else:
                    payload += struct.pack("<BHh", *key, -4004)
            await self.send(frames.OP_GET_RSP, payload, tid)
        elif req.opcode == frames.OP_SET:
            payload = bytes([len(req.items)])
            for it in req.items:
                self.props[(it["siid"], it["piid"])] = it["value"]
                payload += struct.pack("<BHh", it["siid"], it["piid"], 0)
            await self.send(frames.OP_SET_RSP, payload, tid)
            if (2, 2) in [(i["siid"], i["piid"]) for i in req.items]:
                # simulate the motor reaching the target, reported as properties_changed
                self.props[(2, 6)] = self.props[(2, 2)]
                await self.send(frames.OP_PROPS_CHANGED, bytes([1]) + struct.pack("<BH", 2, 6)
                                + frames.encode_value(frames.T_UINT8, self.props[(2, 6)]), 0x7777)
        elif req.opcode == frames.OP_ACTION:
            await self.send(frames.OP_ACTION_RSP, struct.pack("<h", 0) + bytes([0]), tid)

    async def send(self, op, payload, tid):
        body = struct.pack("<HB", tid, op) + payload
        frame = struct.pack("<H", (len(body) + 2) | 0x2000) + body
        enc = self.sess._device_encrypt(frame, self.tx_cnt)
        self.tx_cnt += 1
        await transport.send(self.link, CH_SPEC_NOTIFY, transport.TYPE_SPEC, enc)


async def test_login_get_set_action_and_notify():
    ltmk = os.urandom(16)
    app_link, dev_link = make_link()
    fake = FakeCurtain(dev_link, ltmk)
    dev_task = asyncio.create_task(fake.run())
    dev = MiMeshDevice("fake", ltmk, link=app_link)
    changes = []
    dev.on_props_changed = changes.extend
    async with dev:
        c = Curtain(dev)
        items = await dev.get([(2, 6), (5, 1), (7, 7)])
        assert items[0]["value"] == 30 and items[1]["value"] == 88 and items[2]["code"] == -4004
        await c.set_position(75)
        await c.calibrate()
        for _ in range(50):
            if changes:
                break
            await asyncio.sleep(0.02)
        assert changes == [{"siid": 2, "piid": 6, "type": frames.T_UINT8, "value": 75}]
        # long payload forces multi-packet transfer (> 18 bytes per packet)
        many = await dev.get([(2, 6)] * 6)
        assert len(many) == 6
    dev_task.cancel()


async def test_wrong_ltmk_is_denied():
    app_link, dev_link = make_link()
    dev_task = asyncio.create_task(FakeCurtain(dev_link, os.urandom(16)).run())
    with pytest.raises(SpecError, match="denied"):
        await MiMeshDevice("fake", os.urandom(16), link=app_link).login()
    dev_task.cancel()
