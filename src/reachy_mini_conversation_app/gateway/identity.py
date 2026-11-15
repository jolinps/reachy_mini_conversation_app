"""Load an existing device identity. This module never creates or registers one."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from reachy_mini_conversation_app.gateway.signing import b64, private_key_from_raw, raw_public


class IdentityError(RuntimeError):
    pass


@dataclass
class DeviceIdentity:
    device_id: str
    private_raw: bytes
    public_b64: str
    path: Path

    def private_key(self):
        return private_key_from_raw(self.private_raw)


def load_identity(path: Path) -> DeviceIdentity:
    """Read a device id and private key from a file outside the repository."""
    if not path.is_file():
        raise IdentityError(f"Device identity file was not found at {path}.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IdentityError("Device identity file is not JSON.") from exc
    if not isinstance(payload, dict):
        raise IdentityError("Device identity file is not JSON.")
    device_id = payload.get("device_id")
    private_hex = payload.get("private_key_hex")
    if not isinstance(device_id, str) or not device_id.strip():
        raise IdentityError("Device identity file has no device id.")
    if not isinstance(private_hex, str) or not private_hex.strip():
        raise IdentityError("Device identity file has no private key.")
    try:
        private_raw = bytes.fromhex(private_hex.strip())
        key = private_key_from_raw(private_raw)
    except ValueError as exc:
        raise IdentityError("Device identity private key is not valid.") from exc
    return DeviceIdentity(device_id.strip(), private_raw, b64(raw_public(key)), path)
