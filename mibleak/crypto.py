"""Session crypto for Xiaomi BLE mesh login + spec traffic.

Reconstructed from MiHome iOS 11.8.204:
  -[MHXiaoMiBleMeshLoginFiniteStateMachine generateSessionKeyWithShareSecret:callback:]
  -[MHXiaoMiBleMeshLoginFiniteStateMachine encryptData:callback:]
  -[MHBtDeviceXiaoMi setSession:] / encryptDataToMsc: / decryptDataFromMsc:
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MESH_LOGIN_SALT = b"miot-mesh-login-salt"
MESH_LOGIN_INFO = b"miot-mesh-login-info"
LOGIN_CONFIRM_NONCE = bytes.fromhex("101112131415161718191a1b")
TAG_LEN = 4


def gen_keypair() -> tuple[ec.EllipticCurvePrivateKey, bytes]:
    """P-256 keypair; public key in the 64-byte X||Y form sent over the air (0x04 prefix stripped)."""
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return priv, pub[1:]


def ecdh(priv: ec.EllipticCurvePrivateKey, peer_pub64: bytes) -> bytes:
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b"\x04" + peer_pub64)
    return priv.exchange(ec.ECDH(), peer)


def derive_session_key(shared_secret: bytes, ltmk: bytes) -> bytes:
    """HKDF-SHA256(ikm = shared_secret || LTMK, salt, info) -> 64 bytes."""
    return HKDF(algorithm=hashes.SHA256(), length=64, salt=MESH_LOGIN_SALT, info=MESH_LOGIN_INFO).derive(
        shared_secret + ltmk)


def login_confirm(session_key: bytes, device_pub64: bytes) -> bytes:
    """Last login code: AES-CCM(app_key, fixed nonce) over crc32(device public key) as LE u32."""
    crc = struct.pack("<I", zlib.crc32(device_pub64) & 0xFFFFFFFF)
    return AESCCM(session_key[16:32], tag_length=TAG_LEN).encrypt(LOGIN_CONFIRM_NONCE, crc, None)


@dataclass
class Session:
    dev_key: bytes
    app_key: bytes
    dev_iv: bytes
    app_iv: bytes
    counter: int = 0

    @classmethod
    def from_key(cls, sk: bytes) -> "Session":
        return cls(dev_key=sk[0:16], app_key=sk[16:32], dev_iv=sk[32:36], app_iv=sk[36:40])

    def encrypt(self, plaintext: bytes) -> bytes:
        """App -> device: counter_lo16 || ciphertext || tag4."""
        cnt = self.counter
        self.counter += 1
        nonce = self.app_iv + b"\x00" * 4 + struct.pack("<I", cnt & 0xFFFFFFFF)
        return struct.pack("<H", cnt & 0xFFFF) + AESCCM(self.app_key, tag_length=TAG_LEN).encrypt(nonce, plaintext, None)

    def decrypt(self, data: bytes) -> bytes:
        """Device -> app: nonce = dev_iv || 0000 || first two bytes || 0000."""
        if len(data) <= 2 + TAG_LEN:
            raise ValueError("encrypted message too short")
        nonce = self.dev_iv + b"\x00" * 4 + data[:2] + b"\x00\x00"
        return AESCCM(self.dev_key, tag_length=TAG_LEN).decrypt(nonce, data[2:], None)

    # used by the loopback test's fake device (mirror image of the methods above)
    def _device_encrypt(self, plaintext: bytes, cnt: int) -> bytes:
        nonce = self.dev_iv + b"\x00" * 4 + struct.pack("<H", cnt & 0xFFFF) + b"\x00\x00"
        return struct.pack("<H", cnt & 0xFFFF) + AESCCM(self.dev_key, tag_length=TAG_LEN).encrypt(nonce, plaintext, None)

    def _device_decrypt(self, data: bytes) -> bytes:
        nonce = self.app_iv + b"\x00" * 4 + data[:2] + b"\x00\x00"
        return AESCCM(self.app_key, tag_length=TAG_LEN).decrypt(nonce, data[2:], None)
