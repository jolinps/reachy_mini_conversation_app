"""Authenticated gateway session reused across Hermes utterances."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from reachy_mini_conversation_app.gateway.client import GatewayClient, GatewayError
from reachy_mini_conversation_app.gateway.turn import BoundTurn


class GatewayVoiceSession:
    """Select hermes-root once, then bind each committed utterance to that epoch."""

    def __init__(self, client_factory: Callable[[], GatewayClient], agent_id: str):
        self._client_factory = client_factory
        self.agent_id = agent_id
        self._lock = threading.Lock()
        self._client: GatewayClient | None = None
        self._token: str | None = None
        self._epoch: int | None = None
        self._stop: threading.Event | None = None

    def cancel_active(self) -> None:
        """Ask the utterance currently talking to the gateway to stop."""
        with self._lock:
            stop = self._stop
        if stop is not None:
            stop.set()

    def complete(self, text: str, on_delta: Callable[[str], None] | None = None) -> dict:
        """Send one committed utterance. Authentication is refreshed only when it fails."""
        spoken = ""

        def emit(piece: str) -> None:
            nonlocal spoken
            if not piece:
                return
            spoken += piece
            if on_delta is not None:
                on_delta(piece)

        error: GatewayError | None = None
        for attempt in (1, 2):
            stop = self._begin()
            try:
                client, token, epoch = self._ensure()
                started = time.perf_counter()
                result = BoundTurn(client, token, self.agent_id, epoch).send(
                    text,
                    should_stop=stop.is_set,
                    on_delta=emit,
                )
            except GatewayError as exc:
                error = exc
                if attempt == 1 and _needs_refresh(exc):
                    self._invalidate()
                    continue
                raise
            if stop.is_set():
                raise GatewayError("The turn was cancelled. A late answer was discarded.", "cancelled")
            answer = str(result.get("answer") or "")
            if answer.startswith(spoken):
                rest = answer[len(spoken) :]
                if rest:
                    emit(rest)
            elif answer and answer != spoken:
                emit(answer)
            result["hermes_ms"] = round((time.perf_counter() - started) * 1000)
            return result
        if error is not None:
            raise error
        raise GatewayError("The gateway session could not be reused.", "rejected")

    def _begin(self) -> threading.Event:
        with self._lock:
            if self._stop is not None:
                self._stop.set()
            self._stop = threading.Event()
            return self._stop

    def _ensure(self) -> tuple[GatewayClient, str, int]:
        with self._lock:
            if self._client is not None and self._token and isinstance(self._epoch, int):
                return self._client, self._token, self._epoch
            client = self._client or self._client_factory()
            token = client.authenticate()
            selected = client.select(token, self.agent_id)
            epoch = selected.get("epoch")
            if selected.get("agent_id") != self.agent_id or isinstance(epoch, bool) or not isinstance(epoch, int):
                raise GatewayError("The gateway did not select that agent.", "mismatch")
            self._client = client
            self._token = token
            self._epoch = epoch
            return client, token, epoch

    def _invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._epoch = None


def _needs_refresh(exc: GatewayError) -> bool:
    message = str(exc)
    return "(401)" in message or "(403)" in message
