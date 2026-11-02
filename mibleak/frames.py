"""MIoT BLE spec frame codec.

Reconstructed from MiHome iOS 11.8.204, class MHXiaoMiBleSpecProtocolUtils.

Frame layout (all integers little-endian):
    header  u16   (total_frame_length & 0xfff) | (version << 12)
    tid     u16   transaction id (device echoes it in the response)
    opcode  u8
    payload ...

Typed value = typelen u16 ((type << 12) | byte_length) followed by the value bytes.
"""
from __future__ import annotations

import itertools
import struct
from dataclasses import dataclass, field
from typing import Any

# opcodes (+[MHXiaoMiBleSpecProtocolUtils bleSpecOpcode:])
OP_SET = 0x00
OP_SET_RSP = 0x01
OP_GET = 0x02
OP_GET_RSP = 0x03
OP_PROPS_CHANGED = 0x04
OP_ACTION = 0x05
OP_ACTION_RSP = 0x06
OP_EVENT = 0x07
OP_PROTOCOL_EXCHANGE = 0xF0

DEFAULT_VERSION = 2  # cmdToDevice:version: clamps to >= 2

# value types, same numbering the RN plugin sends in `type`
T_BOOL, T_UINT8, T_INT8, T_UINT16, T_INT16, T_UINT32, T_INT32, T_UINT64, T_INT64, T_FLOAT, T_STRING = range(11)

_FMT = {
    T_BOOL: "<?", T_UINT8: "<B", T_INT8: "<b", T_UINT16: "<H", T_INT16: "<h",
    T_UINT32: "<I", T_INT32: "<i", T_UINT64: "<Q", T_INT64: "<q", T_FLOAT: "<f",
}

TYPE_BY_NAME = {
    "bool": T_BOOL, "uint8": T_UINT8, "int8": T_INT8, "uint16": T_UINT16, "int16": T_INT16,
    "uint32": T_UINT32, "int32": T_INT32, "uint64": T_UINT64, "int64": T_INT64,
    "float": T_FLOAT, "string": T_STRING,
}

_tid_counter = itertools.count()


def next_tid() -> int:
    return next(_tid_counter) & 0xFFFF


def encode_value(vtype: int, value: Any) -> bytes:
    if vtype == T_STRING:
        raw = str(value).encode()
    else:
        if vtype == T_FLOAT:
            value = float(value)
        elif vtype == T_BOOL:
            value = bool(int(value)) if isinstance(value, str) else bool(value)
        else:
            value = int(value)
        raw = struct.pack(_FMT[vtype], value)
    if len(raw) > 0xFFF:
        raise ValueError("value too long")
    return struct.pack("<H", (vtype << 12) | len(raw)) + raw


def decode_value(buf: bytes, off: int) -> tuple[int, Any, int]:
    """Returns (type, value, new_offset)."""
    (tl,) = struct.unpack_from("<H", buf, off)
    vtype, ln = tl >> 12, tl & 0xFFF
    raw = buf[off + 2: off + 2 + ln]
    if len(raw) != ln:
        raise ValueError("truncated value")
    if vtype == T_STRING:
        val: Any = raw.decode(errors="replace")
    elif vtype in _FMT:
        val = struct.unpack(_FMT[vtype], raw)[0]
    else:
        val = raw
    return vtype, val, off + 2 + ln


def _frame(opcode: int, payload: bytes, tid: int, version: int) -> bytes:
    body = struct.pack("<HB", tid, opcode) + payload
    total = 2 + len(body)
    return struct.pack("<H", (total & 0xFFF) | (version << 12)) + body


@dataclass
class Prop:
    siid: int
    piid: int
    vtype: int | None = None
    value: Any = None


def build_set(props: list[Prop], tid: int | None = None, version: int = DEFAULT_VERSION) -> bytes:
    p = bytes([len(props)])
    for x in props:
        p += struct.pack("<BH", x.siid, x.piid) + encode_value(x.vtype, x.value)
    return _frame(OP_SET, p, next_tid() if tid is None else tid, version)


def build_get(props: list[Prop], tid: int | None = None, version: int = DEFAULT_VERSION) -> bytes:
    p = bytes([len(props)])
    for x in props:
        p += struct.pack("<BH", x.siid, x.piid)
    return _frame(OP_GET, p, next_tid() if tid is None else tid, version)


def build_action(siid: int, aiid: int, args: list[Prop] | None = None, tid: int | None = None,
                 version: int = DEFAULT_VERSION) -> bytes:
    args = args or []
    p = struct.pack("<BBB", siid, aiid, len(args))
    for a in args:
        p += struct.pack("<H", a.piid) + encode_value(a.vtype, a.value)
    return _frame(OP_ACTION, p, next_tid() if tid is None else tid, version)


def build_protocol_exchange(version: int = DEFAULT_VERSION, tid: int | None = None) -> bytes:
    return _frame(OP_PROTOCOL_EXCHANGE, b"", next_tid() if tid is None else tid, version)


@dataclass
class Frame:
    version: int
    length: int
    tid: int
    opcode: int
    code: int = 0                 # action-rsp only
    siid: int | None = None       # event only
    eiid: int | None = None       # event only
    aiid: int | None = None
    items: list[dict] = field(default_factory=list)


def parse(buf: bytes) -> Frame:
    if len(buf) < 5:
        raise ValueError("frame too short")
    hdr, tid, op = struct.unpack_from("<HHB", buf, 0)
    f = Frame(version=hdr >> 12, length=hdr & 0xFFF, tid=tid, opcode=op)
    if op in (OP_SET_RSP, OP_GET_RSP, OP_PROPS_CHANGED, OP_SET, OP_GET):
        n, off = buf[5], 6
        for _ in range(n):
            siid, piid = struct.unpack_from("<BH", buf, off)
            off += 3
            item: dict = {"siid": siid, "piid": piid}
            if op in (OP_SET_RSP, OP_GET_RSP):
                (item["code"],) = struct.unpack_from("<h", buf, off)
                off += 2
            if op in (OP_PROPS_CHANGED, OP_SET) or (op == OP_GET_RSP and item["code"] == 0):
                item["type"], item["value"], off = decode_value(buf, off)
            f.items.append(item)
    elif op == OP_ACTION_RSP:
        (f.code,) = struct.unpack_from("<h", buf, 5)
        if f.code == 0 and len(buf) > 7:
            n, off = buf[7], 8
            for _ in range(n):
                (piid,) = struct.unpack_from("<H", buf, off)
                vtype, val, off = decode_value(buf, off + 2)
                f.items.append({"piid": piid, "type": vtype, "value": val})
    elif op == OP_ACTION:
        f.siid, f.aiid, n = struct.unpack_from("<BBB", buf, 5)
        off = 8
        for _ in range(n):
            (piid,) = struct.unpack_from("<H", buf, off)
            vtype, val, off = decode_value(buf, off + 2)
            f.items.append({"piid": piid, "type": vtype, "value": val})
    elif op == OP_EVENT:
        f.siid = buf[5]
        (f.eiid,) = struct.unpack_from("<H", buf, 6)
        n, off = buf[8], 9
        for _ in range(n):
            (piid,) = struct.unpack_from("<H", buf, off)
            vtype, val, off = decode_value(buf, off + 2)
            f.items.append({"piid": piid, "type": vtype, "value": val})
    return f
