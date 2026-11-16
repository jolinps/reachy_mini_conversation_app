"""Send one audio fixture to the Hermes voice runtime without opening robot media.

The conversation app must not be in Hermes Voice while this runs: both would
bind the gateway adapter. This process owns the adapter only for the fixture.
"""

from __future__ import annotations

import asyncio
import audioop
import json
import socket
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from reachy_mini_conversation_app.voice.pin import webrtc_site_packages

site = webrtc_site_packages()
if site is not None:
    sys.path.append(str(site))

from reachy_mini_conversation_app.gateway.client import GatewayClient
from reachy_mini_conversation_app.gateway.identity import load_identity
from reachy_mini_conversation_app.voice.llm_adapter import HermesLlmAdapter
from reachy_mini_conversation_app.voice.session import GatewayVoiceSession
from reachy_mini_conversation_app.voice.webrtc import SAMPLE_RATE, SpeechRuntimeClient

IDENTITY = Path("/Users/jl/Documents/Reachy/reachy-identity.json")
GATEWAY = "https://aiagent.jolinps.com"
AGENT = "hermes-root"


def _wait_for_runtime(runtime: subprocess.Popen, log_path: Path) -> None:
    """Wait until the pinned runtime accepts connections. It does not open robot media."""
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if runtime.poll() is not None:
            tail = log_path.read_text(errors="replace")[-2000:]
            raise SystemExit(f"The voice runtime exited {runtime.returncode}.\n{tail}")
        try:
            with socket.create_connection(("127.0.0.1", 8765), timeout=1):
                return
        except OSError:
            time.sleep(1)
    raise SystemExit("The voice runtime did not open port 8765.")


async def run(wav_path: Path) -> int:
    with wave.open(str(wav_path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise SystemExit("The fixture must be mono 16-bit PCM.")
        pcm = wav.readframes(wav.getnframes())
        rate = wav.getframerate()
    if rate != SAMPLE_RATE:
        pcm, _state = audioop.ratecv(pcm, 2, 1, rate, SAMPLE_RATE, None)
    import numpy as np

    samples = np.frombuffer(pcm, dtype=np.int16)
    identity = load_identity(IDENTITY)
    adapter = HermesLlmAdapter(GatewayVoiceSession(lambda: GatewayClient(GATEWAY, identity), AGENT))
    adapter.start()
    log_path = Path("/tmp/hermes-voice-runtime.log")
    log_file = log_path.open("w")
    runtime = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts/run_hermes_voice_runtime.py")],
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_runtime(runtime, log_path)
    except Exception:
        adapter.stop()
        raise
    events: list[dict] = []
    audio_at: list[float] = []
    started = time.perf_counter()

    def on_event(event: dict) -> None:
        kind = event.get("type")
        if kind in {
            "conversation.item.input_audio_transcription.completed",
            "response.created",
            "response.done",
            "response.output_audio_transcript.done",
            "error",
        }:
            events.append(event)

    def on_audio(frame) -> None:
        if not audio_at:
            audio_at.append(time.perf_counter())

    client = SpeechRuntimeClient("http://127.0.0.1:8765", on_event, on_audio)
    try:
        await client.open()
        client.push_microphone(samples)
        # Let the paced track finish the utterance and the server VAD hear silence.
        await asyncio.sleep(samples.size / SAMPLE_RATE + 3)
        deadline = time.perf_counter() + 45
        while time.perf_counter() < deadline:
            done = any(event.get("type") == "response.output_audio_transcript.done" for event in events)
            if done and audio_at:
                break
            await asyncio.sleep(0.2)
    finally:
        try:
            await client.close()
        except Exception as exc:
            print(f"voice client close: {exc}", file=sys.stderr)
        turn = dict(adapter.last_turn)
        adapter.stop()

    transcript = ""
    answer = ""
    for event in events:
        if event.get("type") == "conversation.item.input_audio_transcription.completed":
            transcript = str(event.get("transcript") or "")
        if event.get("type") == "response.output_audio_transcript.done":
            answer = str(event.get("transcript") or "")
    playback_ms = round((audio_at[0] - started) * 1000) if audio_at else None
    report = {
        "transcript": transcript,
        "answer": answer,
        "audio_frames_started": bool(audio_at),
        "playback_submit_ms_from_connect": playback_ms,
        "gateway_request_id": turn.get("gateway_request_id"),
        "agent_id": turn.get("agent_id"),
        "assignment_epoch": turn.get("assignment_epoch"),
        "backend_instance": turn.get("backend_instance"),
        "hermes_ms": turn.get("hermes_ms"),
        "event_types": [event.get("type") for event in events],
    }
    print(json.dumps(report, indent=2))
    return 0 if transcript and answer and audio_at and turn.get("gateway_request_id") else 1


if __name__ == "__main__":
    wav = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/hermes-question.wav")
    raise SystemExit(asyncio.run(run(wav)))
