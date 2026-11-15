"""Settings sections: persistence, redaction, and inactive-turn apply."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from reachy_mini_conversation_app.settings_sections import (
    SECTIONS,
    SettingsDesk,
    SettingsError,
    redact,
)
from reachy_mini_conversation_app.settings_store import read_settings


class _Boom:
    def __call__(self, host, port):
        raise AssertionError(f"robot probe ran for {host}:{port}")


class SettingsSectionTests(unittest.TestCase):
    def test_sections_match_the_console_navigation(self) -> None:
        self.assertEqual(
            [item["id"] for item in SECTIONS],
            ["robot", "agent", "voice", "conversation", "tools", "diagnostics"],
        )

    def test_opening_settings_does_not_contact_the_robot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desk = SettingsDesk(tmp, robot_probe=_Boom())
            snapshot = desk.snapshot()
            self.assertEqual(snapshot["statuses"]["robot"]["connection"], "not_tested")
            self.assertFalse((Path(tmp) / "settings.json").exists())

    def test_secrets_are_rejected_and_existing_values_remain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desk = SettingsDesk(tmp, robot_probe=_Boom())
            desk.save({"robot_mode": "simulator", "robot_host": "127.0.0.1", "robot_port": 8010, "robot_name": "reachy-mini-sim"})
            with self.assertRaises(SettingsError) as caught:
                desk.save({"gateway_url": "https://example.test", "device_identity_path": "/tmp/key", "api_key": "secret"})
            self.assertEqual(caught.exception.reason, "secret_rejected")
            self.assertEqual(read_settings(tmp).robot_host, "127.0.0.1")

    def test_apply_waits_for_the_turn_to_finish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = {"value": "speaking"}
            desk = SettingsDesk(tmp, turn_state=lambda: state["value"], robot_probe=_Boom())
            desk.save({"voice_route": "hermes", "stt_provider": "external", "tts_provider": "external"})
            with self.assertRaises(SettingsError) as caught:
                desk.apply()
            self.assertEqual(caught.exception.reason, "apply_during_turn")
            self.assertFalse((Path(tmp) / "settings-applied.json").exists())
            state["value"] = "listening"
            applied = desk.apply()
            self.assertEqual(applied["application"]["voice_route"]["state"], "unavailable")
            self.assertFalse(applied["application"]["voice_route"]["applied"])
            self.assertFalse(applied["application"]["robot"]["applied"])
            self.assertEqual(applied["live_audio_route"], "huggingface")
            self.assertIn("voice_route", applied["pending_apply"])
            self.assertNotIn("voice_route", applied["applied"]["fields"])
            self.assertFalse((Path(tmp) / "settings-applied.json").exists())

    def test_playback_and_tools_block_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            activity = {"turn": "listening", "playback": True, "tool_execution": False}
            desk = SettingsDesk(tmp, activity=lambda: activity, robot_probe=_Boom())
            desk.save({"robot_host": "127.0.0.1", "robot_port": 8010, "robot_name": "reachy-mini-sim", "robot_mode": "simulator"})
            with self.assertRaises(SettingsError) as playback:
                desk.apply()
            self.assertEqual(playback.exception.reason, "apply_during_playback")
            activity["playback"] = False
            activity["tool_execution"] = True
            with self.assertRaises(SettingsError) as tool:
                desk.apply()
            self.assertEqual(tool.exception.reason, "apply_during_tool")

    def test_runtime_apply_reports_restart_without_marking_the_robot_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seen = []

            def runtime(saved):
                seen.append(saved.robot_host)
                return {"state": "pending_restart", "applied_fields": [], "pending_restart": ["local_vad_enabled"]}

            desk = SettingsDesk(tmp, robot_probe=_Boom(), runtime_apply=runtime, runtime_status=lambda: {"state": "pending_restart", "applied_fields": [], "pending_restart": ["local_vad_enabled"]})
            desk.save({"robot_mode": "simulator", "robot_host": "127.0.0.1", "robot_port": 8010, "robot_name": "reachy-mini-sim"})
            result = desk.apply()
            self.assertEqual(seen, ["127.0.0.1"])
            self.assertEqual(result["application"]["hf_speech"]["state"], "pending_restart")
            self.assertFalse(result["application"]["robot"]["applied"])
            self.assertEqual(result["application"]["robot"]["state"], "saved")

    def test_robot_test_is_a_status_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def probe(host, port):
                calls.append((host, port))
                return {"tested": True, "ok": True, "verified_simulation": True, "simulation_enabled": True, "robot_name": "reachy-mini-sim", "state": "running", "message": "ok"}

            desk = SettingsDesk(tmp, robot_probe=probe)
            desk.save({"robot_mode": "simulator", "robot_host": "127.0.0.1", "robot_port": 8010, "robot_name": "reachy-mini-sim"})
            result = desk.robot_test()
            self.assertEqual(calls, [("127.0.0.1", 8010)])
            self.assertTrue(result["verified_simulation"])

    def test_text_timing_separates_progress_from_the_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desk = SettingsDesk(tmp, client_factory=lambda url, path: _FakeClient())
            desk.save(
                {
                    "gateway_url": "https://example.test",
                    "gateway_agent_id": "hermes-root",
                    "device_identity_path": "/Users/jl/identity.json",
                }
            )
            result = desk.text_test("Reply with the single word pong.")
            self.assertTrue(result["ok"])
            self.assertEqual(result["answer"], "pong")
            self.assertGreaterEqual(result["timing_ms"]["progress_events"], 1)
            self.assertIn("gateway_bound", result["timing_ms"])
            self.assertIn("first_answer", result["timing_ms"])
            exported = json.dumps(desk.diagnostics())
            self.assertNotIn("pong", exported)
            self.assertNotIn("private_key", exported)

    def test_settings_methods_register_beside_the_conversation_server(self) -> None:
        import gstreamer_libs

        gstreamer_libs.setup_python_environment()
        from reachy_mini.apps.jsonrpc_server import JsonRpcServer
        from reachy_mini_conversation_app.settings_routes import register_settings_methods

        rpc = JsonRpcServer()
        register_settings_methods(rpc, None)
        for name in (
            "settings.get",
            "settings.save",
            "settings.apply",
            "settings.robot_test",
            "settings.agents",
            "settings.text_test",
            "settings.diagnostics",
        ):
            self.assertIn(name, rpc._methods)

    def test_export_redacts_secret_text(self) -> None:
        self.assertEqual(redact({"api_key": "abc", "note": "token=abc"})["api_key"], "<redacted>")
        self.assertIn("<redacted>", redact({"note": "token=abc"})["note"])


class _FakeClient:
    class identity:
        device_id = "rch_test"

    def health(self):
        return {"chat_binding": 1, "text_only": True}

    def authenticate(self):
        return "session-token"

    def select(self, token, agent):
        return {"agent_id": agent, "epoch": 1}

    def chat(self, token, body):
        instance = "bki_" + ("a" * 32)
        binding = {
            "type": "binding",
            "agent_id": body["expected_agent_id"],
            "assignment_epoch": body["expected_epoch"],
            "gateway_request_id": "gwr_test",
            "backend_instance": instance,
        }
        progress = {"type": "progress", "tool": "reasoning", "preview": "not the answer"}
        final = {
            "type": "response_text",
            "text": "pong",
            "final": True,
            **{key: binding[key] for key in ("agent_id", "assignment_epoch", "gateway_request_id", "backend_instance")},
        }
        yield json.dumps(binding) + "\n" + json.dumps(progress) + "\n" + json.dumps(final) + "\n"


if __name__ == "__main__":
    unittest.main()
