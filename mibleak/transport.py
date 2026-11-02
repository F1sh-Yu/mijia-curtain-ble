"""Xiaomi BLE multi-packet transfer over GATT notify/write characteristics.

Reconstructed from MiHome iOS 11.8.204 (MHXiaomiBleProcessMutilWriteCommand and the mesh login
FSM). Every characteristic used here speaks the same framing:

  control  00 00 00 <type> <count u16le>     announce <count> data packets of <type>
  ack      00 00 01 01                       ready to receive
           00 00 01 00                       all packets received
           00 00 01 02 / 03 / 04             busy / timeout / cancel
           00 00 01 05 <idx u16le>           packet <idx> lost, resend
  data     <idx u16le, starting at 1> <payload, at most 18 bytes>
  single   00 00 02 <type> <payload>         one-shot transfer, acked with 00 00 03 00
"""
from __future__ import annotations

import asyncio
import collections
import logging
import struct
from typing import Protocol

log = logging.getLogger(__name__)

DMTU = 18  # default payload bytes per packet ("DMTU" startInfo override, else 18)

ACK_READY = bytes.fromhex("00000101")
ACK_OK = bytes.fromhex("00000100")
ACK_BUSY = bytes.fromhex("00000102")
ACK_TIMEOUT = bytes.fromhex("00000103")
ACK_CANCEL = bytes.fromhex("00000104")
ACK_LOST = bytes.fromhex("00000105")
ACK_SINGLE_OK = bytes.fromhex("00000300")

# transfer types
TYPE_SPEC = 0x00
TYPE_PUBKEY = 0x03
TYPE_LOGIN_CONFIRM = 0x05


class TransferError(Exception):
    pass


class NotifyBuffer:
    """Per-characteristic notification FIFO that supports putting messages back."""

    def __init__(self) -> None:
        self._items: collections.deque[bytes] = collections.deque()
        self._cond = asyncio.Condition()

    def push(self, msg: bytes) -> None:
        self._items.append(bytes(msg))
        asyncio.get_running_loop().create_task(self._notify())

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    def unget(self, msgs: list[bytes]) -> None:
        self._items.extendleft(reversed(msgs))

    async def get(self, timeout: float) -> bytes:
        async with self._cond:
            await asyncio.wait_for(self._cond.wait_for(lambda: bool(self._items)), timeout)
            return self._items.popleft()


class Link(Protocol):
    async def write(self, char: str, data: bytes) -> None: ...
    def buffer(self, char: str) -> NotifyBuffer: ...


async def _next(link: Link, char: str, timeout: float) -> bytes:
    try:
        return await link.buffer(char).get(timeout)
    except asyncio.TimeoutError:
        raise TransferError(f"timeout waiting for notification on {char}") from None


def _check_ack(msg: bytes) -> None:
    if msg[:4] == ACK_BUSY:
        raise TransferError("device busy")
    if msg[:4] == ACK_TIMEOUT:
        raise TransferError("device timed out waiting for packets")
    if msg[:4] == ACK_CANCEL:
        raise TransferError("device cancelled transfer")


async def send(link: Link, char: str, ttype: int, data: bytes, timeout: float = 10.0) -> None:
    chunks = [data[i:i + DMTU] for i in range(0, len(data), DMTU)] or [b""]
    stash: list[bytes] = []  # device-initiated messages that arrive mid-transfer
    try:
        await link.write(char, b"\x00\x00\x00" + bytes([ttype]) + struct.pack("<H", len(chunks)))
        while True:
            msg = await _next(link, char, timeout)
            if msg[:4] == ACK_READY:
                break
            _check_ack(msg)
            stash.append(msg)
        for i, c in enumerate(chunks, 1):
            await link.write(char, struct.pack("<H", i) + c)
        while True:
            msg = await _next(link, char, timeout)
            if msg[:4] == ACK_OK:
                return
            if msg[:4] == ACK_LOST and len(msg) >= 6:
                (idx,) = struct.unpack_from("<H", msg, 4)
                log.debug("resending packet %d", idx)
                await link.write(char, struct.pack("<H", idx) + chunks[idx - 1])
                continue
            _check_ack(msg)
            stash.append(msg)
    finally:
        link.buffer(char).unget(stash)


async def recv(link: Link, char: str, expect_type: int | None = None, timeout: float = 10.0) -> bytes:
    """Receive one transfer initiated by the device."""
    while True:
        msg = await _next(link, char, timeout)
        if msg[:3] == b"\x00\x00\x02" and len(msg) >= 4:  # single pack
            await link.write(char, ACK_SINGLE_OK)
            return msg[4:]
        if msg[:3] == b"\x00\x00\x00" and len(msg) >= 6:
            ttype = msg[3]
            (count,) = struct.unpack_from("<H", msg, 4)
            if expect_type is not None and ttype != expect_type:
                log.debug("unexpected transfer type %d (wanted %d)", ttype, expect_type)
            break
        log.debug("ignoring %s on %s while waiting for transfer start", msg.hex(), char)
    await link.write(char, ACK_READY)
    parts: dict[int, bytes] = {}
    while len(parts) < count:
        msg = await _next(link, char, timeout)
        if len(msg) < 2:
            continue
        (idx,) = struct.unpack_from("<H", msg, 0)
        if idx == 0:  # stray control message
            _check_ack(msg)
            continue
        parts[idx] = msg[2:]
    await link.write(char, ACK_OK)
    return b"".join(parts[i] for i in range(1, count + 1))
