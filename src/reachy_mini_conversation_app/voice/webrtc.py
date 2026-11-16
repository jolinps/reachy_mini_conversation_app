"""WebRTC client for the pinned speech-to-speech runtime.

Audio uses the runtime's SDP endpoint and the oai-events data channel.
The robot daemon keeps its own connection; this peer connection never
uses that daemon's signalling port.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import urllib.error
import urllib.request
from collections import deque
from fractions import Fraction

import numpy as np
from numpy.typing import NDArray

from reachy_mini_conversation_app.voice.pin import EVENTS_CHANNEL, webrtc_calls_url

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
WIRE_RATE = 48000
FRAME_SAMPLES = 960  # 20 ms at 48 kHz


class SpeechRuntimeClient:
    """Send microphone PCM while push-to-talk is held and receive TTS PCM."""

    def __init__(self, runtime_url: str | None, on_event, on_audio) -> None:
        self.runtime_url = runtime_url
        self._on_event = on_event
        self._on_audio = on_audio
        self.pc = None
        self.channel = None
        self.connected = False
        self._pcm: deque[np.int16] = deque(maxlen=SAMPLE_RATE * 2)
        self._pts = 0
        self._clock: float | None = None
        self._closed = False

    async def open(self) -> None:
        from aiortc import RTCPeerConnection, RTCSessionDescription

        self.pc = RTCPeerConnection()
        self.channel = self.pc.createDataChannel(EVENTS_CHANNEL)
        self.channel.on("open", self._session_update)
        self.channel.on("message", self._message)
        self.pc.addTrack(_outbound_track(self))

        @self.pc.on("track")
        def _on_track(track) -> None:
            if track.kind == "audio":
                asyncio.create_task(self._read_audio(track))

        @self.pc.on("connectionstatechange")
        def _on_state() -> None:
            pc = self.pc
            if pc is None:
                self.connected = False
                return
            state = pc.connectionState
            self.connected = state == "connected"
            logger.info("Hermes voice WebRTC %s", state)

        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        await self._wait_for_ice()
        answer = await asyncio.to_thread(self._post_offer, self.pc.localDescription.sdp)
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        await self._wait_until_connected()

    async def close(self) -> None:
        self._closed = True
        self.connected = False
        pc = self.pc
        self.pc = None
        self.channel = None
        if pc is not None:
            await pc.close()

    def send_event(self, payload: dict) -> None:
        channel = self.channel
        if channel is None or channel.readyState != "open":
            return
        channel.send(json.dumps(payload))

    def push_microphone(self, samples: NDArray[np.int16]) -> None:
        """Queue 16 kHz PCM. Samples recorded while push-to-talk is up never arrive here."""
        self._pcm.extend(np.asarray(samples, dtype=np.int16).reshape(-1).tolist())

    def cancel_response(self) -> None:
        self.send_event({"type": "response.cancel"})

    def _session_update(self) -> None:
        self.send_event(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "audio": {
                        "input": {
                            "turn_detection": {
                                "type": "server_vad",
                                "create_response": True,
                                "interrupt_response": True,
                                "silence_duration_ms": 500,
                            }
                        }
                    },
                },
            }
        )

    def _message(self, message: str | bytes) -> None:
        raw = message.decode("utf-8") if isinstance(message, bytes) else message
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return
        if isinstance(event, dict):
            self._on_event(event)

    async def _read_audio(self, track) -> None:
        while not self._closed:
            try:
                frame = await track.recv()
            except Exception:
                return
            array = frame.to_ndarray()
            samples = np.asarray(array, dtype=np.int16).reshape(-1)
            rate = int(getattr(frame, "sample_rate", WIRE_RATE) or WIRE_RATE)
            if rate != SAMPLE_RATE and rate > 0:
                step = max(1, round(rate / SAMPLE_RATE))
                samples = samples[::step]
            if samples.size:
                self._on_audio(samples.astype(np.int16, copy=False))

    async def _wait_for_ice(self) -> None:
        if self.pc is None or self.pc.iceGatheringState == "complete":
            return
        done = asyncio.Event()

        @self.pc.on("icegatheringstatechange")
        def _changed() -> None:
            if self.pc is not None and self.pc.iceGatheringState == "complete":
                done.set()

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(done.wait(), timeout=3)

    async def _wait_until_connected(self) -> None:
        if self.connected:
            return
        for _ in range(50):
            if self.pc is None:
                raise RuntimeError("The voice runtime connection closed.")
            if self.pc.connectionState == "connected":
                self.connected = True
                return
            if self.pc.connectionState in {"failed", "closed"}:
                raise RuntimeError("The voice runtime WebRTC connection failed.")
            await asyncio.sleep(0.1)
        raise RuntimeError("The voice runtime WebRTC connection did not open.")

    def _post_offer(self, sdp: str) -> str:
        request = urllib.request.Request(
            webrtc_calls_url(self.runtime_url),
            data=sdp.encode("utf-8"),
            headers={"Content-Type": "application/sdp"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8")
                if response.status not in {200, 201}:
                    raise RuntimeError(f"The voice runtime rejected the WebRTC offer ({response.status}).")
                return body
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"The voice runtime rejected the WebRTC offer ({exc.code}).") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"The voice runtime is not accepting WebRTC calls. {exc.reason}") from None


def _outbound_track(owner: SpeechRuntimeClient):
    """20 ms frames. Silence leaves the runtime's VAD idle between button presses."""
    from aiortc.mediastreams import AudioStreamTrack
    from av import AudioFrame

    class OutboundTrack(AudioStreamTrack):
        kind = "audio"

        async def recv(self):  # type: ignore[override]
            samples = np.repeat(_take_16k(owner._pcm, FRAME_SAMPLES // 3), 3).astype(np.int16, copy=False)
            frame = AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
            frame.planes[0].update(np.ascontiguousarray(samples).tobytes())
            frame.sample_rate = WIRE_RATE
            frame.pts = owner._pts
            frame.time_base = Fraction(1, WIRE_RATE)
            owner._pts += FRAME_SAMPLES
            if owner._clock is None:
                owner._clock = time.monotonic()
            delay = owner._clock + (owner._pts / WIRE_RATE) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            return frame

    return OutboundTrack()


def _take_16k(buffer: deque[np.int16], count: int) -> NDArray[np.int16]:
    samples = np.zeros(count, dtype=np.int16)
    filled = 0
    while buffer and filled < count:
        samples[filled] = buffer.popleft()
        filled += 1
    return samples
