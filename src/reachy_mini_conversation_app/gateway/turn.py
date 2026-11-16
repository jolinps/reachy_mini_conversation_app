"""One text turn bound to the agent and epoch selected for it.

A late line after cancellation or a destination change is discarded. The
client does not choose another agent when the selected one is unavailable.
"""

from __future__ import annotations

import secrets

from reachy_mini_conversation_app.gateway.client import GatewayClient, GatewayError
from reachy_mini_conversation_app.gateway.ndjson import TurnParser
from reachy_mini_conversation_app.gateway.redact import redact


class BoundTurn:
    def __init__(self, client: GatewayClient, token: str, agent_id: str, epoch: int):
        if not agent_id or isinstance(epoch, bool) or not isinstance(epoch, int):
            raise GatewayError("A turn needs an agent id and an assignment epoch.", "unsupported")
        self.client = client
        self.token = token
        self.agent_id = agent_id
        self.epoch = epoch
        self.generation = 0

    def release(self) -> None:
        """Drop this destination so a late result cannot be accepted."""
        self.generation += 1
        self.agent_id = ""
        self.epoch = -1

    def send(self, text: str, should_stop=None, on_delta=None) -> dict:
        if not text.strip():
            raise GatewayError("The text request was empty.", "configuration")
        if not self.agent_id:
            raise GatewayError("No destination is bound. The request was not sent.", "unsupported")
        agent_id = self.agent_id
        epoch = self.epoch
        generation = self.generation
        request_id = "req-" + secrets.token_hex(8)
        body = {
            "request_id": request_id,
            "conversation_id": "reachy-not-used",
            "text": text,
            "expected_agent_id": agent_id,
            "expected_epoch": epoch,
            "text_only": True,
        }
        parser = TurnParser()
        stopped = False
        try:
            for chunk in self.client.chat(self.token, body):
                if self._stop_requested(should_stop) or self._stale(agent_id, epoch, generation):
                    stopped = True
                    break
                for event in parser.feed(chunk):
                    if self._stop_requested(should_stop) or self._stale(agent_id, epoch, generation):
                        parser.final_text = None
                        stopped = True
                        break
                    self._require_same_agent(event, agent_id)
                    self._emit_delta(event, on_delta)
                if stopped:
                    break
            if not stopped:
                for event in parser.finish():
                    self._require_same_agent(event, agent_id)
        except GatewayError:
            self._cancel(request_id)
            raise
        if stopped or self._stale(agent_id, epoch, generation):
            self._cancel(request_id)
            raise GatewayError("The turn was cancelled. A late answer was discarded.", "cancelled")
        if parser.cancelled:
            raise GatewayError("The gateway cancelled the turn.", "cancelled")
        if parser.error:
            raise GatewayError(redact(parser.error, self.client.secrets), "rejected")
        if parser.saw_audio:
            raise GatewayError(
                "The gateway sent audio on a text-only turn. The answer was discarded.",
                "rejected",
            )
        if parser.final_text is None:
            raise GatewayError("The gateway closed the turn without a final answer.", "rejected")
        evidence = _evidence(parser.evidence, agent_id, epoch)
        return {
            "answer": parser.final_text,
            "progress": list(parser.progress),
            "request_id": request_id,
            "evidence": evidence,
        }

    def _require_same_agent(self, event: dict, agent_id: str) -> None:
        other = event.get("agent_id")
        if isinstance(other, str) and other and other != agent_id:
            self.release()
            raise GatewayError(
                "The gateway answered from a different agent. The answer was discarded.",
                "mismatch",
            )

    @staticmethod
    def _emit_delta(event: dict, on_delta) -> None:
        """Forward a non-final text delta. The final event is the whole answer."""
        if on_delta is None or event.get("type") != "response_text" or event.get("final") is True:
            return
        text = event.get("text")
        if isinstance(text, str) and text:
            on_delta(text)

    def _stop_requested(self, should_stop) -> bool:
        return should_stop is not None and should_stop()

    def _stale(self, agent_id: str, epoch: int, generation: int) -> bool:
        return self.generation != generation or self.agent_id != agent_id or self.epoch != epoch

    def _cancel(self, request_id: str) -> None:
        self.release()
        try:
            self.client.cancel(self.token, request_id)
        except GatewayError:
            return


def _evidence(observed: dict, agent_id: str, epoch: int) -> dict:
    """Accept only the destination the gateway froze for this turn."""
    if observed.get("agent_id") != agent_id or observed.get("assignment_epoch") != epoch:
        raise GatewayError(
            "The gateway result did not match the bound destination. The answer was discarded.",
            "mismatch",
        )
    request_id = observed.get("gateway_request_id")
    instance = observed.get("backend_instance")
    if not isinstance(request_id, str) or not request_id.startswith("gwr_"):
        raise GatewayError(
            "The gateway result did not include a server request id. The answer was discarded.",
            "rejected",
        )
    if not isinstance(instance, str) or not instance.startswith("bki_") or len(instance) != 36:
        raise GatewayError(
            "The gateway result did not include a backend instance identifier. The answer was discarded.",
            "rejected",
        )
    return {
        "agent_id": agent_id,
        "assignment_epoch": epoch,
        "gateway_request_id": request_id,
        "backend_instance": instance,
        "source": "gateway_binding",
        "backend_instance_meaning": "gateway_registration",
        "host_inspection": False,
        "proves_answering_instance": False,
    }
