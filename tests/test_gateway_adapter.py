"""Local gateway adapter tests.

These tests use a fake transport. A passing test is not a live Hermes turn.
"""

import json
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from reachy_mini_conversation_app.gateway.client import GatewayClient, GatewayError
from reachy_mini_conversation_app.gateway.identity import load_identity
from reachy_mini_conversation_app.gateway.ndjson import TurnParser
from reachy_mini_conversation_app.gateway.probe import run_probe
from reachy_mini_conversation_app.gateway.profiles import discovery_from_catalog
from reachy_mini_conversation_app.gateway.redact import redact
from reachy_mini_conversation_app.gateway.signing import b64, raw_public, sign_fields, verify_fields
from reachy_mini_conversation_app.gateway.turn import BoundTurn

PRIVATE = "ab" * 32
TOKEN = "session-token-value"


class ScriptTransport:
    def __init__(self, health=None, agents=None, select=None, chunks=None, paths=None, catalog=None):
        self.calls = []
        self.health = health or {"status": "ok", "hermes_ok": True}
        self.agents = agents or {"agents": [_agent()]}
        self.select = select or {"status": "selected", "agent_id": "agent-a", "epoch": 4}
        self.chunks = chunks or [_line({"type": "response_text", "text": "pong", "final": True})]
        self.paths = paths or {"/health", "/v1/chat", "/v1/agents", "/v1/agents/select", "/v1/cancel"}
        self.catalog = catalog or {"status": "fresh", "profiles": [], "source": "api"}

    def __call__(self, method, url, body, headers, stream=False):
        self.calls.append((method, url, body, dict(headers), stream))
        if url.endswith("/health"):
            return 200, self.health
        if url.endswith("/openapi.json"):
            return 200, {"paths": {path: {} for path in self.paths}}
        if url.endswith("/v1/device/challenge"):
            return 200, {"challenge_id": "challenge-1", "nonce": body["nonce"]}
        if url.endswith("/v1/device/authenticate"):
            return 200, {"session_token": TOKEN}
        if url.endswith("/v1/agents"):
            return 200, self.agents
        if url.endswith("/catalog"):
            return 200, self.catalog
        if url.endswith("/v1/agents/select"):
            return 200, self.select
        if url.endswith("/v1/cancel"):
            return 200, {"status": "cancelled"}
        if url.endswith("/v1/chat") and stream:
            return 200, _Chunks(self.chunks)
        raise AssertionError(url)


class _Chunks:
    def __init__(self, parts):
        self.parts = list(parts)

    def read(self, _size):
        if not self.parts:
            return b""
        return self.parts.pop(0)

    def close(self):
        return None


def _agent(agent_id="agent-a", name="Hermes A", instance_id="hermes-a", server_name="hermes-a.internal"):
    return {
        "id": agent_id,
        "name": name,
        "enabled": True,
        "active": True,
        "instance_id": instance_id,
        "server_name": server_name,
        "framework": "hermes",
        "profile": "registry-label",
    }


def _line(event) -> bytes:
    return (json.dumps(event) + "\n").encode("utf-8")


def _identity(directory: Path):
    path = directory / "identity.json"
    path.write_text(json.dumps({"device_id": "device-test", "private_key_hex": PRIVATE}), encoding="utf-8")
    return load_identity(path)


def _client(directory: Path, transport: ScriptTransport) -> GatewayClient:
    return GatewayClient("https://gateway.test", _identity(directory), transport)


def _urls(transport: ScriptTransport) -> list[str]:
    return [url for _method, url, _body, _headers, _stream in transport.calls]


class GatewayAdapterTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)

    def tearDown(self):
        self._temporary.cleanup()

    def test_ndjson_keeps_progress_out_of_the_final_answer(self):
        parser = TurnParser()
        first = parser.feed('{"type":"status","state":"started"}\n{"type":"progress","tool":"lookup","preview":"work')
        second = parser.feed('ing"}\n{"type":"response_text","text":"partial","final":false}\n')
        third = parser.feed('{"type":"response_text","text":"pong","final":true}\n')
        self.assertEqual(
            [event["type"] for event in first + second + third],
            ["status", "progress", "response_text", "response_text"],
        )
        self.assertEqual(parser.progress, [{"tool": "lookup", "preview": "working"}])
        self.assertEqual(parser.deltas, ["partial"])
        self.assertEqual(parser.final_text, "pong")
        self.assertEqual(parser.finish(), [])

    def test_ndjson_rejects_a_broken_stream(self):
        parser = TurnParser()
        with self.assertRaisesRegex(ValueError, "not NDJSON"):
            parser.feed("this is not json\n")

    def test_bound_turn_rejects_a_different_agent_and_discards_its_text(self):
        transport = ScriptTransport(
            chunks=[_line({"type": "response_text", "text": "from the other agent", "final": True, "agent_id": "agent-b"})]
        )
        turn = BoundTurn(_client(self.directory, transport), TOKEN, "agent-a", 4)
        with self.assertRaises(GatewayError) as caught:
            turn.send("ping")
        self.assertEqual(caught.exception.kind, "mismatch")
        self.assertNotIn("from the other agent", str(caught.exception))
        self.assertTrue(any(url.endswith("/v1/cancel") for url in _urls(transport)))

    def test_late_answer_after_cancel_or_switch_is_discarded(self):
        transport = ScriptTransport(
            chunks=[
                _line({"type": "progress", "tool": "lookup", "preview": "working"}),
                _line({"type": "response_text", "text": "too late", "final": True}),
            ]
        )
        turn = BoundTurn(_client(self.directory, transport), TOKEN, "agent-a", 4)

        def stop_after_progress():
            return any(url.endswith("/v1/chat") for url in _urls(transport))

        with self.assertRaisesRegex(GatewayError, "late answer was discarded") as caught:
            turn.send("ping", should_stop=stop_after_progress)
        self.assertEqual(caught.exception.kind, "cancelled")
        self.assertNotIn("too late", str(caught.exception))

        transport.calls.clear()
        transport.chunks = [
            _line({"type": "progress", "tool": "lookup", "preview": "working"}),
            _line({"type": "response_text", "text": "after the switch", "final": True}),
        ]
        turn = BoundTurn(_client(self.directory, transport), TOKEN, "agent-a", 4)

        def switch_away():
            if any(url.endswith("/v1/chat") for url in _urls(transport)):
                turn.release()
            return False

        with self.assertRaisesRegex(GatewayError, "late answer was discarded"):
            turn.send("ping", should_stop=switch_away)

    def test_secret_redaction_removes_the_key_and_session_token(self):
        cleaned = redact(f"failed with {PRIVATE} and {TOKEN}", [PRIVATE, TOKEN])
        self.assertEqual(cleaned, "failed with [redacted] and [redacted]")
        self.assertNotIn(PRIVATE, cleaned)

    def test_authenticate_body_does_not_contain_the_private_key(self):
        transport = ScriptTransport()
        client = _client(self.directory, transport)
        client.authenticate()
        sent = json.dumps([call[2] for call in transport.calls])
        self.assertNotIn(PRIVATE, sent)
        challenge = next(body for _method, url, body, _headers, _stream in transport.calls if url.endswith("/challenge"))
        signature = challenge.pop("signature")
        self.assertTrue(verify_fields(client.identity.public_b64, challenge, signature))

    def test_missing_contract_does_not_select_or_chat(self):
        transport = ScriptTransport(
            health={"status": "ok", "hermes_ok": True, "hermes_detail": "API server capabilities responded."}
        )
        report = run_probe(_client(self.directory, transport), agent_id="agent-a", text="Reply with the single word pong.")
        self.assertFalse(report["hermes_instance_proven"])
        self.assertFalse(report["chat"]["sent"])
        self.assertEqual(report["chat"]["answer"], "")
        self.assertIn("chat_binding", report["chat"]["blocker"])
        self.assertFalse(any(url.endswith("/v1/agents/select") or url.endswith("/v1/chat") for url in _urls(transport)))
        self.assertEqual(report["profile_discovery"]["status"], "unsupported")
        self.assertEqual(report["profile_discovery"]["profiles"], [])
        self.assertNotIn("/p/default", json.dumps(report["profile_discovery"]))
        self.assertFalse(any(url.endswith("/catalog") for url in _urls(transport)))
        self.assertFalse(report["routing_evidence"]["proves_answering_instance"])
        self.assertNotIn(TOKEN, json.dumps(report))
        self.assertNotIn(PRIVATE, json.dumps(report))

    def test_select_mismatch_or_missing_epoch_does_not_send(self):
        mismatch = ScriptTransport(
            health={"chat_binding": 1, "text_only": True},
            select={"status": "selected", "agent_id": "agent-b", "epoch": 4},
        )
        with self.assertRaisesRegex(GatewayError, "different agent"):
            run_probe(_client(self.directory, mismatch), agent_id="agent-a", text="ping")
        self.assertFalse(any(url.endswith("/v1/chat") for url in _urls(mismatch)))

        missing_epoch = ScriptTransport(
            health={"chat_binding": 1, "text_only": True},
            select={"status": "selected", "agent_id": "agent-a"},
        )
        with self.assertRaisesRegex(GatewayError, "assignment epoch"):
            run_probe(_client(self.directory, missing_epoch), agent_id="agent-a", text="ping")
        self.assertFalse(any(url.endswith("/v1/chat") for url in _urls(missing_epoch)))

    def test_no_authorized_agent_is_reported_without_a_fallback(self):
        transport = ScriptTransport(health={"status": "ok", "hermes_ok": True}, agents={"agents": []})
        report = run_probe(_client(self.directory, transport), agent_id="", text="ping")
        self.assertEqual(report["authorized_agents"], [])
        self.assertEqual(report["selected_agent_id"], "")
        self.assertFalse(report["chat"]["sent"])
        self.assertIn("no enabled authorized agent", report["chat"]["blocker"])
        self.assertIn("chat_binding", report["chat"]["blocker"])
        self.assertFalse(any(url.endswith("/v1/agents/select") or url.endswith("/v1/chat") for url in _urls(transport)))

    def test_unauthorized_agent_is_not_replaced(self):
        transport = ScriptTransport(
            health={"chat_binding": 1, "text_only": True},
            agents={"agents": [_agent("agent-a"), _agent("agent-b", name="Hermes B")]},
        )
        with self.assertRaisesRegex(GatewayError, "not an enabled agent"):
            run_probe(_client(self.directory, transport), agent_id="agent-c", text="ping")
        self.assertFalse(any(url.endswith("/v1/agents/select") or url.endswith("/v1/chat") for url in _urls(transport)))

    def test_several_agents_require_an_explicit_choice(self):
        transport = ScriptTransport(
            health={"chat_binding": 1, "text_only": True},
            agents={"agents": [_agent("agent-a"), _agent("agent-b", name="Hermes B")]},
        )
        with self.assertRaisesRegex(GatewayError, "Pass --agent"):
            run_probe(_client(self.directory, transport), agent_id="", text="ping")
        self.assertFalse(any(url.endswith("/v1/agents/select") for url in _urls(transport)))

    def test_bound_text_turn_separates_progress_from_the_answer(self):
        transport = ScriptTransport(
            health={"chat_binding": 1, "text_only": True},
            paths={
                "/health",
                "/v1/chat",
                "/v1/agents",
                "/v1/agents/select",
                "/v1/cancel",
                "/v1/agents/{agent_id}/catalog",
            },
            catalog={
                "status": "fresh",
                "source": "api",
                "fingerprint": "abc",
                "profiles": [
                    {
                        "backend_profile_id": "classroom",
                        "display_name": "Classroom",
                        "configured_route": "/p/classroom",
                    }
                ],
            },
            chunks=[
                _line({"type": "progress", "tool": "lookup", "preview": "working"}),
                _line(
                    {
                        "type": "response_text",
                        "text": "pong",
                        "final": True,
                        "agent_id": "agent-a",
                        "assignment_epoch": 4,
                        "gateway_request_id": "gwr_" + "ab" * 16,
                        "backend_instance": "bki_" + "cd" * 16,
                    }
                ),
            ],
        )
        report = run_probe(_client(self.directory, transport), agent_id="", text="Reply with the single word pong.")
        self.assertFalse(report["hermes_instance_proven"])
        self.assertTrue(report["chat"]["sent"])
        self.assertEqual(report["chat"]["answer"], "pong")
        self.assertEqual(report["chat"]["progress"], [{"tool": "lookup", "preview": "working"}])
        self.assertEqual(report["selected_agent_id"], "agent-a")
        self.assertEqual(report["assignment_epoch"], 4)
        self.assertEqual(
            report["profile_discovery"]["profiles"],
            [
                {
                    "id": "classroom",
                    "display_name": "Classroom",
                    "source": "gateway",
                    "configured_route": "/p/classroom",
                }
            ],
        )
        self.assertFalse(report["routing_evidence"]["proves_answering_instance"])
        self.assertEqual(report["routing_evidence"]["source"], "gateway_binding")
        self.assertEqual(report["routing_evidence"]["gateway_request_id"], "gwr_" + "ab" * 16)
        self.assertEqual(report["routing_evidence"]["backend_instance"], "bki_" + "cd" * 16)
        self.assertNotIn("hermes-a", json.dumps(report["routing_evidence"]))
        chat = next(body for _method, url, body, _headers, stream in transport.calls if url.endswith("/v1/chat"))
        self.assertEqual(chat["expected_agent_id"], "agent-a")
        self.assertEqual(chat["expected_epoch"], 4)
        self.assertTrue(chat["text_only"])
        self.assertNotIn(TOKEN, json.dumps(report))

    def test_catalog_does_not_invent_default_when_discovery_fails(self):
        parsed = discovery_from_catalog(
            {"status": "unsupported", "profiles": [], "error": "Profile discovery is not available."}
        )
        self.assertEqual(parsed["status"], "unsupported")
        self.assertEqual(parsed["profiles"], [])
        self.assertNotIn("/p/default", json.dumps(parsed))

    def test_error_event_is_a_failure_and_not_an_answer(self):
        transport = ScriptTransport(
            health={"chat_binding": 1, "text_only": True},
            chunks=[_line({"type": "error", "message": f"backend failed token {TOKEN}"})],
        )
        client = _client(self.directory, transport)
        client.secrets.append(TOKEN)
        turn = BoundTurn(client, TOKEN, "agent-a", 4)
        with self.assertRaisesRegex(GatewayError, "backend failed") as caught:
            turn.send("ping")
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_answer_without_server_routing_evidence_is_discarded(self):
        transport = ScriptTransport(
            health={"chat_binding": 1, "text_only": True},
            chunks=[
                _line(
                    {
                        "type": "response_text",
                        "text": "I am Hermes",
                        "final": True,
                        "agent_id": "agent-a",
                        "assignment_epoch": 4,
                    }
                )
            ],
        )
        with self.assertRaisesRegex(GatewayError, "server request id"):
            run_probe(_client(self.directory, transport), agent_id="agent-a", text="ping")

    def test_audio_on_a_text_turn_is_rejected(self):
        transport = ScriptTransport(
            chunks=[
                _line({"type": "audio_chunk", "data": "AAAA"}),
                _line({"type": "response_text", "text": "spoken", "final": True}),
            ]
        )
        turn = BoundTurn(_client(self.directory, transport), TOKEN, "agent-a", 4)
        with self.assertRaisesRegex(GatewayError, "sent audio"):
            turn.send("ping")

    def test_sign_and_verify_round_trip(self):
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        loaded = Ed25519PrivateKey.from_private_bytes(raw)
        fields = {"gateway": "https://gateway.test", "purpose": "challenge", "device_id": "device-test"}
        signature = sign_fields(loaded, fields)
        public = b64(raw_public(loaded))
        self.assertTrue(verify_fields(public, fields, signature))
        self.assertFalse(verify_fields(public, {**fields, "purpose": "authenticate"}, signature))


if __name__ == "__main__":
    unittest.main()
