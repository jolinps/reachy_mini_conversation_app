"""Pinned speech-to-speech runtime used by Hermes Voice.

The hosted Hugging Face conversation session is a different product. It is not
an STT or TTS service and it is not started while Hermes Voice is selected.
"""

from __future__ import annotations

from pathlib import Path

PROJECT = "huggingface/speech-to-speech"
VERSION = "1.1.0"
COMMIT_INTERFACE = "v1.1.0"

RUNTIME_HOST = "127.0.0.1"
RUNTIME_PORT = 8765
ADAPTER_HOST = "127.0.0.1"
ADAPTER_PORT = 8766

STT = "parakeet-tdt"
TTS = "kitten"
TTS_VOICE = "Bruno"
LANGUAGE = "en"
LLM_BACKEND = "chat-completions"
MODEL_NAME = "hermes-root"

# Languages checked against the v1.1.0 STT tables. Parakeet TDT's declared list
# is European. Qwen3-ASR includes Thai and does not include Burmese. No pinned
# local TTS in this mode speaks Thai or Burmese.
LANGUAGE_SUPPORT = {
    "parakeet-tdt": {"en": True, "th": False, "my": False},
    "qwen3-asr": {"en": True, "th": True, "my": False},
    "kitten": {"en": True, "th": False, "my": False},
}

WEBRTC_PATH = "/v1/realtime/calls"
EVENTS_CHANNEL = "oai-events"


def runtime_origin() -> str:
    return f"http://{RUNTIME_HOST}:{RUNTIME_PORT}"


def webrtc_calls_url(runtime_url: str | None = None) -> str:
    """Return the SDP endpoint. This is not the robot daemon's WebRTC port."""
    base = (runtime_url or runtime_origin()).rstrip("/")
    if base.endswith(WEBRTC_PATH):
        return base
    return base + WEBRTC_PATH


def adapter_base_url() -> str:
    return f"http://{ADAPTER_HOST}:{ADAPTER_PORT}/v1"


def conversation_mode(value: str | None) -> str:
    """Return the only mode that may generate answers."""
    if value == "hermes":
        return "hermes"
    return "huggingface"


def _support_dir() -> Path:
    """Application Support is outside iCloud Documents, so imports are not dataless."""
    return Path.home() / "Library/Application Support/Reachy"


def speech_executable() -> Path:
    """Pinned speech-to-speech binary. Prefer the local environment over a repo venv."""
    local = _support_dir() / "venv-speech" / "bin" / "speech-to-speech"
    if local.is_file():
        return local
    return Path(__file__).resolve().parents[3] / ".venv-speech" / "bin" / "speech-to-speech"


def webrtc_site_packages() -> Path | None:
    """Site-packages that contain aiortc and not the speech runtime's torch/numpy."""
    roots = (
        _support_dir() / "venv-webrtc",
        Path(__file__).resolve().parents[3] / ".venv-webrtc",
    )
    for root in roots:
        matches = sorted((root / "lib").glob("python*/site-packages"))
        if matches and (matches[0] / "aiortc").exists():
            return matches[0]
    return None
