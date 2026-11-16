"""Start the pinned speech-to-speech runtime for Hermes Voice.

This process does not open the robot microphone, the robot speaker, or the
simulator daemon. The conversation app owns those and connects here over WebRTC.
The answer model is the local gateway adapter, not the runtime's default LLM.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from reachy_mini_conversation_app.voice.pin import (  # noqa: E402
    LANGUAGE,
    LLM_BACKEND,
    MODEL_NAME,
    PROJECT,
    RUNTIME_HOST,
    RUNTIME_PORT,
    STT,
    TTS,
    TTS_VOICE,
    VERSION,
    adapter_base_url,
    runtime_origin,
    speech_executable,
)

STATUS_PATH = Path.home() / "Library/Application Support/Reachy/conversation-app/voice-runtime.json"
PYTHON = speech_executable()


def main() -> None:
    if not PYTHON.is_file():
        raise SystemExit(f"The speech environment is missing: {PYTHON}")
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "version": VERSION,
                "stt": STT,
                "tts": TTS,
                "tts_voice": TTS_VOICE,
                "language": LANGUAGE,
                "llm_backend": LLM_BACKEND,
                "model_name": MODEL_NAME,
                "webrtc": runtime_origin() + "/v1/realtime/calls",
                "adapter": adapter_base_url(),
            },
            indent=2,
        )
    )
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.execv(
        str(PYTHON),
        [
            str(PYTHON),
            "serve",
            "--host",
            RUNTIME_HOST,
            "--port",
            str(RUNTIME_PORT),
            "--stt",
            STT,
            "--llm_backend",
            LLM_BACKEND,
            "--model_name",
            MODEL_NAME,
            "--responses_api_base_url",
            adapter_base_url(),
            "--responses_api_api_key",
            "local",
            "--tts",
            TTS,
            "--kitten_voice",
            TTS_VOICE,
            "--parakeet_tdt_language",
            LANGUAGE,
        ],
    )


if __name__ == "__main__":
    main()
