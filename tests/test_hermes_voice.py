"""Hermes Voice keeps the gateway text exact and drops audio from an old turn."""

from __future__ import annotations

import json
import unittest
from http.client import HTTPConnection

import gstreamer_libs

gstreamer_libs.setup_python_environment()

from reachy_mini_conversation_app.gateway.client import GatewayError
from reachy_mini_conversation_app.voice.hermes_handler import HermesVoiceHandler
from reachy_mini_conversation_app.voice.llm_adapter import HermesLlmAdapter
from reachy_mini_conversation_app.voice.session import GatewayVoiceSession


def _evidence(text: str = "pong") -> str:
    return json.dumps(
        {
            "type": "response_text",
            "text": text,
            "final": True,
            "agent_id": "hermes-root",
            "assignment_epoch": 4,
            "gateway_request_id": "gwr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "backend_instance": "bki_" + "ab" * 16,
        }
    )


class _Gateway:
    def __init__(self) -> None:
        self.auth_count = 0
        self.select_count = 0
        self.texts: list[str] = []
        self.cancelled = 0
        self.secrets: list[str] = []
        self.fail_auth = False

    def authenticate(self) -> str:
        self.auth_count += 1
        if self.fail_auth and self.auth_count == 1:
            raise GatewayError("The gateway rejected the request (401).", "rejected")
        return "token"

    def select(self, token: str, agent_id: str) -> dict:
        self.select_count += 1
        return {"agent_id": agent_id, "epoch": 4}

    def chat(self, token: str, body: dict):
        self.texts.append(body["text"])
        if body["text"] == "expire":
            raise GatewayError("The gateway rejected the request (401).", "rejected")
        yield _evidence("four") + "\n"

    def cancel(self, token: str, request_id: str) -> dict:
        self.cancelled += 1
        return {}


class HermesVoiceTests(unittest.TestCase):
    def test_warmup_and_compaction_do_not_call_the_gateway(self) -> None:
        gateway = _Gateway()
        adapter = HermesLlmAdapter(GatewayVoiceSession(lambda: gateway, "hermes-root"), port=0)
        adapter.start()
        self.addCleanup(adapter.stop)
        port = adapter._server.server_address[1]
        warmup = self._post(port, [{"role": "system", "content": "You are a helpful assistant"}, {"role": "user", "content": "Hello"}])
        compact = self._post(
            port,
            [
                {"role": "system", "content": "You are a conversation memory compressor for a real-time voice AI assistant."},
                {"role": "user", "content": "Summarize the following conversation."},
            ],
        )
        self.assertEqual(warmup["choices"][0]["message"]["content"], "ready")
        self.assertIn("user_summary", compact["choices"][0]["message"]["content"])
        self.assertEqual(gateway.auth_count, 0)
        self.assertEqual(gateway.texts, [])

    def test_only_the_latest_committed_text_reaches_hermes_once(self) -> None:
        gateway = _Gateway()
        adapter = HermesLlmAdapter(GatewayVoiceSession(lambda: gateway, "hermes-root"), port=0)
        adapter.start()
        self.addCleanup(adapter.stop)
        port = adapter._server.server_address[1]
        first = self._post(
            port,
            [
                {"role": "system", "content": "Rewrite the answer."},
                {"role": "user", "content": "partial"},
                {"role": "assistant", "content": "ignored"},
                {"role": "user", "content": "What is two plus two?"},
            ],
        )
        second = self._post(port, [{"role": "user", "content": "Say the word pong."}])
        self.assertEqual(first["choices"][0]["message"]["content"], "four")
        self.assertEqual(second["choices"][0]["message"]["content"], "four")
        self.assertEqual(gateway.texts, ["What is two plus two?", "Say the word pong."])
        self.assertEqual(gateway.auth_count, 1)
        self.assertEqual(gateway.select_count, 1)
        self.assertEqual(adapter.last_turn["agent_id"], "hermes-root")
        self.assertEqual(adapter.last_turn["assignment_epoch"], 4)

    def test_an_expired_session_is_refreshed_once(self) -> None:
        gateway = _Gateway()
        session = GatewayVoiceSession(lambda: gateway, "hermes-root")
        session.complete("hello")
        gateway.fail_auth = False
        with self.assertRaises(GatewayError):
            session.complete("expire")
        self.assertGreaterEqual(gateway.auth_count, 2)

    def test_a_cancelled_turn_discards_a_late_answer(self) -> None:
        gateway = _Gateway()
        session = GatewayVoiceSession(lambda: gateway, "hermes-root")

        def chat(token: str, body: dict):
            gateway.texts.append(body["text"])
            session.cancel_active()
            yield _evidence() + "\n"

        gateway.chat = chat  # type: ignore[method-assign]
        with self.assertRaises(GatewayError) as caught:
            session.complete("wait")
        self.assertEqual(caught.exception.kind, "cancelled")
        self.assertEqual(gateway.cancelled, 1)

    def test_old_playback_is_dropped_when_the_turn_changes(self) -> None:
        handler = HermesVoiceHandler(deps=None)  # type: ignore[arg-type]
        handler._accept_audio = True
        import numpy as np

        self.assertTrue(handler.submit_playback(np.array([1, 2], dtype=np.int16), handler.playback_epoch))
        handler.playback_epoch += 1
        handler._accept_audio = False
        self.assertFalse(handler.submit_playback(np.array([3, 4], dtype=np.int16), handler.playback_epoch - 1))
        self.assertEqual(handler.output_queue.qsize(), 1)

    def _post(self, port: int, messages: list[dict]) -> dict:
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"model": "hermes-root", "messages": messages}).encode()
        connection.request("POST", "/v1/chat/completions", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = json.loads(response.read().decode())
        connection.close()
        self.assertEqual(response.status, 200, payload)
        return payload


if __name__ == "__main__":
    unittest.main()
