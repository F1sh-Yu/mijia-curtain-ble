import struct

from mibleak import frames
from mibleak.frames import Prop


def test_set_target_position_bytes():
    raw = frames.build_set([Prop(2, 2, frames.T_UINT8, 50)], tid=7)
    # header: len 12 | v2<<12, tid 7, op 0, n=1, siid 2, piid 0x0002, typelen (1<<12)|1, value 50
    assert raw.hex() == "0c20" "0700" "00" "01" "02" "0200" "0110" "32"
    assert len(raw) == 12


def test_get_bytes_and_header_length():
    raw = frames.build_get([Prop(2, 6), Prop(5, 1)], tid=0x0102)
    assert raw[4] == frames.OP_GET
    hdr = struct.unpack_from("<H", raw)[0]
    assert hdr & 0xFFF == len(raw) and hdr >> 12 == 2
    assert raw[5:] == bytes([2, 2, 6, 0, 5, 1, 0])


def test_action_aiid_is_one_byte():
    raw = frames.build_action(2, 1, tid=1)
    assert raw[4:] == bytes([frames.OP_ACTION, 2, 1, 0])


def test_value_encodings():
    assert frames.encode_value(frames.T_UINT32, 0x01020304) == bytes.fromhex("0450") + bytes.fromhex("04030201")
    assert frames.encode_value(frames.T_INT16, -2) == bytes.fromhex("0240") + bytes.fromhex("feff")
    assert frames.encode_value(frames.T_BOOL, True) == bytes.fromhex("0100") + b"\x01"
    assert frames.encode_value(frames.T_STRING, "ab") == bytes.fromhex("02a0") + b"ab"


def _rsp(op, payload, tid=7):
    body = struct.pack("<HB", tid, op) + payload
    return struct.pack("<H", (len(body) + 2) | 0x2000) + body


def test_parse_get_rsp_with_error_item():
    payload = bytes([2]) + struct.pack("<BHh", 2, 6, 0) + frames.encode_value(frames.T_UINT8, 42) \
        + struct.pack("<BHh", 9, 9, -4004)
    f = frames.parse(_rsp(frames.OP_GET_RSP, payload))
    assert f.tid == 7 and f.version == 2
    assert f.items == [{"siid": 2, "piid": 6, "code": 0, "type": frames.T_UINT8, "value": 42},
                       {"siid": 9, "piid": 9, "code": -4004}]


def test_parse_props_changed_and_event():
    f = frames.parse(_rsp(frames.OP_PROPS_CHANGED, bytes([1]) + struct.pack("<BH", 2, 6)
                          + frames.encode_value(frames.T_UINT8, 80)))
    assert f.items == [{"siid": 2, "piid": 6, "type": frames.T_UINT8, "value": 80}]
    e = frames.parse(_rsp(frames.OP_EVENT, bytes([2]) + struct.pack("<H", 1) + bytes([0])))
    assert (e.siid, e.eiid, e.items) == (2, 1, [])


def test_parse_action_rsp():
    ok = frames.parse(_rsp(frames.OP_ACTION_RSP, struct.pack("<h", 0) + bytes([0])))
    assert ok.code == 0
    bad = frames.parse(_rsp(frames.OP_ACTION_RSP, struct.pack("<h", -704)))
    assert bad.code == -704
