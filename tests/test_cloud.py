import base64
import hashlib
import json

import pytest

from mibleak.cloud import XiaomiCloud, crypt, extract_ltmk, save_private


def test_query_dev_encrypted_request_and_response(monkeypatch):
    cloud = XiaomiCloud("cn")
    cloud.security = base64.b64encode(b"test-security").decode()
    cloud.cookies = {"userId": "123"}
    expected = "0123456789abcdef" * 2

    def post(url, *, data, cookies, headers, timeout):
        assert url == "https://api.io.mi.com/app/v2/blemesh/query_dev"
        key = hashlib.sha256(b"test-security" + base64.b64decode(data["_nonce"])).digest()
        payload = crypt(key, base64.b64decode(data["data"])).decode()
        assert json.loads(payload) == {"did": "test-did"}
        clear_signature = base64.b64encode(hashlib.sha1(
            f"POST&/v2/blemesh/query_dev&data={payload}&{base64.b64encode(key).decode()}".encode()
        ).digest()).decode()
        assert crypt(key, base64.b64decode(data["rc4_hash__"])).decode() == clear_signature

        class Response:
            text = base64.b64encode(crypt(key, json.dumps({
                "code": 0, "result": {"gatt_ltmk": expected}}).encode())).decode()

            def raise_for_status(self):
                pass

            def json(self):
                raise ValueError

        return Response()

    monkeypatch.setattr(cloud.session, "post", post)
    assert extract_ltmk(cloud.call("/v2/blemesh/query_dev", {"did": "test-did"})) == expected


@pytest.mark.parametrize("result", [{}, {"token": "a" * 32}, {"beaconkey": "a" * 32},
    {"gatt_ltmk": "x" * 32}, {"gatt_ltmk": "a" * 30}])
def test_reject_missing_or_invalid_ltmk(result):
    with pytest.raises(ValueError):
        extract_ltmk(result)


def test_secret_file_permissions(tmp_path):
    path = tmp_path / "ltmk.json"
    path.write_text("old")
    path.chmod(0o644)
    save_private(path, "secret")
    assert path.read_text() == "secret"
    assert path.stat().st_mode & 0o777 == 0o600
