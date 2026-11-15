"""Ed25519 signatures for the device gateway.

The signed bytes are canonical JSON. The gateway origin is inside the payload,
so a signature for one gateway cannot be replayed against another. The private
key is not placed in the JSON body; only the signature is.
"""

from __future__ import annotations

import base64
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat


def canonical(fields: dict) -> bytes:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def private_key_from_raw(raw: bytes) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(raw)


def raw_public(key: Ed25519PrivateKey | Ed25519PublicKey) -> bytes:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return public.public_bytes(Encoding.Raw, PublicFormat.Raw)


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def sign_fields(key: Ed25519PrivateKey, fields: dict) -> str:
    return b64(key.sign(canonical(fields)))


def verify_fields(public_b64: str, fields: dict, signature: str) -> bool:
    try:
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64.encode("ascii"), validate=True))
        public.verify(base64.b64decode(signature.encode("ascii"), validate=True), canonical(fields))
    except (InvalidSignature, ValueError):
        return False
    return True
