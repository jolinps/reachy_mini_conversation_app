"""Calls that exist on the deployed device gateway.

This client can authenticate, list authorized agents, select one, stream one
chat, and cancel it. It has no pairing or registration method. A test can
replace the transport; the default transport uses HTTPS and the system
certificate store.
"""

from __future__ import annotations

import codecs
import json
import secrets
import ssl
import urllib.error
import urllib.request
from collections.abc import Iterator
from urllib.parse import quote, urlparse

from reachy_mini_conversation_app.gateway.identity import DeviceIdentity
from reachy_mini_conversation_app.gateway.redact import redact
from reachy_mini_conversation_app.gateway.signing import sign_fields


class GatewayError(RuntimeError):
    def __init__(self, message: str, kind: str = "rejected"):
        super().__init__(message)
        self.kind = kind


class UrllibTransport:
    def __init__(self) -> None:
        self.context = ssl.create_default_context()

    def __call__(
        self,
        method: str,
        url: str,
        body: dict | None,
        headers: dict,
        stream: bool = False,
    ):
        parsed = urlparse(url)
        loopback = parsed.scheme == "http" and (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not loopback:
            raise GatewayError("The gateway address must be an https URL.", "configuration")
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Accept": "application/json", **headers},
            method=method,
        )
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            response = urllib.request.urlopen(
                request,
                timeout=30,
                context=None if loopback else self.context,
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise GatewayError(_http_message(exc.code, detail), _http_kind(exc.code)) from None
        except urllib.error.URLError as exc:
            raise GatewayError(f"The gateway did not answer. {exc.reason}", "unreachable") from None
        if stream:
            return response.status, response
        with response:
            raw = response.read().decode("utf-8")
        return response.status, json.loads(raw) if raw else {}


class GatewayClient:
    def __init__(self, gateway_url: str, identity: DeviceIdentity, transport=None):
        self.gateway_url = gateway_url.rstrip("/")
        self.identity = identity
        self.transport = transport or UrllibTransport()
        self.secrets = [identity.private_raw.hex()]
        parsed = urlparse(self.gateway_url)
        loopback = parsed.scheme == "http" and (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
        if not ((parsed.scheme == "https" and parsed.hostname) or loopback):
            raise GatewayError("Enter one https gateway address.", "configuration")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise GatewayError("The gateway address cannot include a login, query, or fragment.", "configuration")

    def health(self) -> dict:
        return self._json("GET", "/health", None, {})

    def openapi_paths(self) -> set[str]:
        payload = self._json("GET", "/openapi.json", None, {})
        paths = payload.get("paths") if isinstance(payload, dict) else None
        if not isinstance(paths, dict):
            raise GatewayError("The gateway did not publish its API paths.", "rejected")
        return set(paths)

    def authenticate(self) -> str:
        challenge = self._signed("challenge", "/v1/device/challenge")
        challenge_id = challenge.get("challenge_id")
        nonce = challenge.get("nonce")
        if not isinstance(challenge_id, str) or not isinstance(nonce, str):
            raise GatewayError("The gateway challenge was incomplete.", "rejected")
        fields = {
            "v": 1,
            "purpose": "authenticate",
            "device_id": self.identity.device_id,
            "gateway": self.gateway_url,
            "challenge_id": challenge_id,
            "nonce": nonce,
        }
        body = {**fields, "signature": sign_fields(self.identity.private_key(), fields)}
        payload = self._json("POST", "/v1/device/authenticate", body, {})
        token = payload.get("session_token")
        if not isinstance(token, str) or not token:
            raise GatewayError("The gateway did not start a session.", "rejected")
        self.secrets.append(token)
        return token

    def agents(self, token: str) -> dict:
        return self._json("GET", "/v1/agents", None, {"Authorization": f"Bearer {token}"})

    def catalog(self, token: str, agent_id: str) -> dict:
        encoded = quote(agent_id, safe="")
        return self._json("GET", f"/v1/agents/{encoded}/catalog", None, {"Authorization": f"Bearer {token}"})

    def select(self, token: str, agent_id: str) -> dict:
        return self._json(
            "POST",
            "/v1/agents/select",
            {"agent_id": agent_id},
            {"Authorization": f"Bearer {token}"},
        )

    def chat(self, token: str, body: dict) -> Iterator[str]:
        status, response = self.transport(
            "POST",
            f"{self.gateway_url}/v1/chat",
            body,
            {"Authorization": f"Bearer {token}"},
            True,
        )
        if status != 200:
            detail = response.read().decode("utf-8", "replace") if hasattr(response, "read") else ""
            raise self._error(_http_message(status, detail))
        return _text_chunks(response)

    def cancel(self, token: str, request_id: str) -> dict:
        return self._json(
            "POST",
            "/v1/cancel",
            {"request_id": request_id},
            {"Authorization": f"Bearer {token}"},
        )

    def _signed(self, purpose: str, path: str) -> dict:
        fields = {
            "v": 1,
            "purpose": purpose,
            "device_id": self.identity.device_id,
            "gateway": self.gateway_url,
            "nonce": secrets.token_hex(16),
        }
        body = {**fields, "signature": sign_fields(self.identity.private_key(), fields)}
        return self._json("POST", path, body, {})

    def _json(self, method: str, path: str, body: dict | None, headers: dict) -> dict:
        status, payload = self.transport(method, f"{self.gateway_url}{path}", body, headers, False)
        if status != 200 or not isinstance(payload, dict):
            raise self._error(_http_message(status, json.dumps(payload) if isinstance(payload, dict) else ""))
        return payload

    def _error(self, message: str) -> GatewayError:
        return GatewayError(redact(message, self.secrets), "rejected")


def _text_chunks(response) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        while True:
            raw = response.read(512)
            if not raw:
                tail = decoder.decode(b"", final=True)
                if tail:
                    yield tail
                break
            text = decoder.decode(raw)
            if text:
                yield text
    finally:
        close = getattr(response, "close", None)
        if close is not None:
            close()


def _http_message(status: int, detail: str) -> str:
    try:
        payload = json.loads(detail) if detail else {}
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        value = payload.get("detail") or payload.get("error")
        if isinstance(value, str) and value.strip():
            return value.strip()
    if status:
        return f"The gateway rejected the request ({status})."
    return "The gateway rejected the request."


def _http_kind(status: int) -> str:
    if status in {401, 403}:
        return "rejected"
    if status >= 500:
        return "unreachable"
    return "rejected"
