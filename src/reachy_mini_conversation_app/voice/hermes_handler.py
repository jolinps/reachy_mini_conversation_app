"""Hermes Voice handler: robot audio, WebRTC runtime, gateway text, robot playback.

Only this handler runs while Hermes Voice is selected. It does not open the
hosted Hugging Face conversation session, and it does not send speculative
transcripts or robot tool calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from reachy_mini_conversation_app.conversation_handler import AudioFrame, ConversationHandler
from reachy_mini_conversation_app.gateway.client import GatewayClient
from reachy_mini_conversation_app.gateway.identity import load_identity
from reachy_mini_conversation_app.huggingface_realtime import _parse_turn_latency
from reachy_mini_conversation_app.settings_store import read_settings
from reachy_mini_conversation_app.tools.core_tools import ToolDependencies
from reachy_mini_conversation_app.voice.llm_adapter import HermesLlmAdapter
from reachy_mini_conversation_app.voice.pin import (
    LANGUAGE,
    LANGUAGE_SUPPORT,
    STT,
    TTS,
    TTS_VOICE,
    conversation_mode,
)
from reachy_mini_conversation_app.voice.session import GatewayVoiceSession
from reachy_mini_conversation_app.voice.webrtc import SpeechRuntimeClient

logger = logging.getLogger(__name__)


class _NoRunningTools:
    def get_running_tools(self) -> list[object]:
        return []


class HermesVoiceHandler(ConversationHandler):
    """Push-to-talk turns through the speech runtime and the existing gateway client."""

    conversation_mode = "hermes"

    def __init__(self, deps: ToolDependencies, instance_path: str | None = None, startup_voice: str | None = None):
        super().__init__()
        self.deps = deps
        self.instance_path = instance_path
        self.connection: SpeechRuntimeClient | None = None
        self.output_queue: asyncio.Queue = asyncio.Queue()
        self.tool_manager = _NoRunningTools()
        self.paced_playback = True
        self._startup_voice = startup_voice
        self._push_to_talk = False
        self._stopped: asyncio.Event | None = None
        self._client: SpeechRuntimeClient | None = None
        self._adapter: HermesLlmAdapter | None = None
        self._accept_audio = False
        self._stt_started: float | None = None
        self._transcript_at: float | None = None
        self._playback_mark: float | None = None
        self.last_timing: dict[str, Any] = {}

    def _is_connected(self) -> bool:
        return self.connection is not None and bool(getattr(self.connection, "connected", False))

    def _idle_behavior_ready(self) -> bool:
        return False

    async def start_up(self) -> None:
        saved = read_settings(self.instance_path)
        if conversation_mode(saved.voice_route) != "hermes":
            raise RuntimeError("Hermes Voice is not the selected mode.")
        if not saved.gateway_url or not saved.device_identity_path or not saved.gateway_agent_id:
            raise RuntimeError("Save the gateway, device identity, and agent before Hermes Voice.")
        identity_path = Path(saved.device_identity_path)
        agent_id = saved.gateway_agent_id

        def client_factory() -> GatewayClient:
            return GatewayClient(saved.gateway_url or "", load_identity(identity_path))

        self._adapter = HermesLlmAdapter(GatewayVoiceSession(client_factory, agent_id))
        self._adapter.start()
        self._client = SpeechRuntimeClient(saved.voice_runtime_url, self._on_event, self._on_audio)
        self._stopped = asyncio.Event()
        try:
            await self._client.open()
            self.connection = self._client
            logger.info("Hermes Voice connected over WebRTC. The Hugging Face conversation model is not running.")
            await self._stopped.wait()
        finally:
            self.connection = None
            self._accept_audio = False
            client = self._client
            self._client = None
            if client is not None:
                await client.close()
            adapter = self._adapter
            self._adapter = None
            if adapter is not None:
                adapter.stop()

    async def shutdown(self) -> None:
        self._accept_audio = False
        client = self._client or self.connection
        if client is not None:
            client.cancel_response()
        if self._adapter is not None:
            self._adapter.session.cancel_active()
        if self._stopped is not None and not self._stopped.is_set():
            self._stopped.set()

    async def receive(self, frame: AudioFrame) -> None:
        if not self._push_to_talk or self._client is None:
            return
        samples = np.asarray(frame[1], dtype=np.int16).reshape(-1)
        self._client.push_microphone(samples)

    def set_push_to_talk(self, active: bool) -> None:
        """Hold speech only while the button is down. Release lets VAD commit one utterance."""
        self._push_to_talk = bool(active)
        if active:
            self._stt_started = time.perf_counter()
            self._transcript_at = None
            self._playback_mark = None

    async def interrupt(self) -> None:
        """Stop queued playback, TTS, and the gateway turn before a new one can start."""
        self.playback_epoch += 1
        self._accept_audio = False
        self._playback_mark = None
        self._clear_output()
        client = self._client or self.connection
        if client is not None:
            client.cancel_response()
        if self._adapter is not None:
            self._adapter.session.cancel_active()
        self._mark_activity("interrupted")

    async def say(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            raise ValueError("say: empty text")
        if not self._is_connected() or self._client is None:
            raise RuntimeError("say: no active session")
        self._client.send_event(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        self._client.send_event({"type": "response.create"})

    async def apply_personality(self, profile: str | None) -> str:
        return profile or "default"

    async def refresh_instructions(self) -> bool:
        return False

    async def get_available_voices(self) -> list[str]:
        return [TTS_VOICE]

    def get_current_voice(self) -> str:
        saved = read_settings(self.instance_path)
        return saved.tts_voice or self._startup_voice or TTS_VOICE

    async def change_voice(self, voice: str) -> str:
        if self._client is not None:
            self._client.send_event(
                {
                    "type": "session.update",
                    "session": {"audio": {"output": {"voice": voice}}},
                }
            )
        return voice

    def submit_playback(self, samples: NDArray[np.int16], generation: int) -> bool:
        """Queue TTS audio for the official playback loop, dropping a superseded turn."""
        if generation != self.playback_epoch or not self._accept_audio:
            return False
        audio = np.ascontiguousarray(samples, dtype=np.int16)
        if audio.size == 0:
            return False
        self.playback_frame_epochs[id(audio)] = generation
        self.playback_pending_frames += 1
        if self._playback_mark is None:
            self._playback_mark = time.perf_counter()
            self._publish_timing()
        self.output_queue.put_nowait((16000, audio))
        return True

    def voice_report(self) -> dict[str, Any]:
        saved = read_settings(self.instance_path)
        connected = self._is_connected()
        saved_stt = saved.stt_provider if saved.stt_provider not in {None, "", "hosted_hf"} else STT
        saved_tts = saved.tts_provider if saved.tts_provider not in {None, "", "hosted_hf"} else TTS
        saved_voice = saved.tts_voice or TTS_VOICE
        saved_language = saved.stt_language or LANGUAGE
        matches = saved_stt == STT and saved_tts == TTS and saved_voice == TTS_VOICE and saved_language == LANGUAGE
        applied = connected and matches and conversation_mode(saved.voice_route) == "hermes"
        return {
            "state": "active" if applied else ("pending_restart" if conversation_mode(saved.voice_route) == "hermes" else "unavailable"),
            "applied": applied,
            "active_mode": "hermes" if connected else "hermes",
            "saved_mode": conversation_mode(saved.voice_route),
            "webrtc": "connected" if connected else "not_connected",
            "live_route": "hermes" if connected else "none",
            "runtime_url": saved.voice_runtime_url or "http://127.0.0.1:8765",
            "stt_provider": STT if connected else "",
            "tts_provider": TTS if connected else "",
            "stt_language": LANGUAGE if connected else "",
            "tts_voice": TTS_VOICE if connected else "",
            "agent_id": saved.gateway_agent_id or "",
            "applied_fields": ["voice_route", "stt_provider", "tts_provider", "stt_language", "tts_voice", "voice_runtime_url"] if applied else [],
            "languages": LANGUAGE_SUPPORT,
            "credential_blocker": _credential_blocker(saved_stt, saved_tts, saved_language),
            "last_timing": dict(self.last_timing),
        }

    def _on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "speech_to_speech.input_audio_transcription.snapshot":
            return
        if kind == "conversation.item.input_audio_transcription.delta":
            return
        if kind == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript") if isinstance(event.get("transcript"), str) else ""
            self._transcript_at = time.perf_counter()
            if transcript:
                self._emit_transcript("user", transcript, True)
            self._mark_activity("user_transcription_completed")
            return
        if kind == "response.created":
            self._accept_audio = True
            self._playback_mark = None
            self._mark_activity("response_created")
            return
        if kind == "response.output_audio_transcript.done":
            transcript = event.get("transcript") if isinstance(event.get("transcript"), str) else ""
            if transcript:
                self._emit_transcript("assistant", transcript, True)
            return
        if kind in {"response.done", "response.cancelled"}:
            self._accept_audio = False
            self._publish_timing(event.get("response"))
            self._mark_activity("assistant_transcript_done")

    def _on_audio(self, samples: NDArray[np.int16]) -> None:
        self.submit_playback(samples, self.playback_epoch)

    def _publish_timing(self, response: object | None = None) -> None:
        metadata = getattr(response, "metadata", None)
        if metadata is None and isinstance(response, dict):
            metadata = response.get("metadata")
        parsed = _parse_turn_latency(metadata if isinstance(metadata, dict) else None) or {}
        adapter = self._adapter.last_turn if self._adapter is not None else {}
        timing: dict[str, Any] = dict(parsed)
        if isinstance(adapter.get("hermes_ms"), int):
            timing["hermes_ms"] = adapter["hermes_ms"]
        if self._stt_started is not None and self._transcript_at is not None:
            timing["stt_ms"] = round((self._transcript_at - self._stt_started) * 1000)
        if self._transcript_at is not None and self._playback_mark is not None:
            timing["playback_submit_ms"] = round((self._playback_mark - self._transcript_at) * 1000)
        for key in ("gateway_request_id", "assignment_epoch", "agent_id", "backend_instance"):
            if adapter.get(key) not in (None, ""):
                timing[key] = adapter[key]
        self.last_timing = timing
        public = {key: value for key, value in timing.items() if key in {"stt_ms", "hermes_ms", "llm_ms", "tts_ttfa_ms", "playback_submit_ms"}}
        if public:
            self._emit_latency(public)

    def _clear_output(self) -> None:
        while not self.output_queue.empty():
            try:
                self.output_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self.playback_pending_frames = 0
        self.playback_until = 0.0


def _credential_blocker(stt: str, tts: str, language: str) -> str:
    if stt == "external" or tts == "external":
        return "An external speech credential is not configured."
    support = LANGUAGE_SUPPORT.get(STT, {})
    if language in {"th", "my"} and not support.get(language, False):
        if language == "th":
            return "Thai needs the qwen3-asr model selected and a Thai TTS. Neither is the running runtime, and no speech API credential is configured."
        return "Burmese is not in the pinned speech-to-speech STT tables, and no speech API credential is configured."
    return ""
