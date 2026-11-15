"""NDJSON events from the device gateway.

``response_text`` with ``final`` true is the completed answer. In this gateway
that text is the full reply, not a delta. A non-final ``response_text`` is a
delta. Status and progress never become answer text.
"""

from __future__ import annotations

import json


class TurnParser:
    def __init__(self) -> None:
        self._raw = ""
        self.final_text: str | None = None
        self.deltas: list[str] = []
        self.progress: list[dict[str, str]] = []
        self.error = ""
        self.cancelled = False
        self.saw_audio = False
        self.evidence: dict = {}

    def feed(self, text: str) -> list[dict]:
        self._raw += text
        events = []
        while "\n" in self._raw:
            line, self._raw = self._raw.split("\n", 1)
            event = self._line(line)
            if event is not None:
                events.append(event)
        return events

    def finish(self) -> list[dict]:
        event = self._line(self._raw)
        self._raw = ""
        if event is None:
            return []
        return [event]

    def _line(self, line: str) -> dict | None:
        line = line.strip()
        if not line:
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("The gateway stream was not NDJSON.") from exc
        if not isinstance(event, dict):
            raise ValueError("The gateway stream was not NDJSON.")
        self._observe(event)
        return event

    def _observe(self, event: dict) -> None:
        for key in ("agent_id", "assignment_epoch", "gateway_request_id", "backend_instance"):
            value = event.get(key)
            if value not in (None, ""):
                self.evidence[key] = value
        kind = event.get("type")
        if kind == "response_text":
            text = event.get("text")
            if not isinstance(text, str):
                text = ""
            if event.get("final") is True:
                self.final_text = text
            else:
                self.deltas.append(text)
            return
        if kind == "progress":
            tool = event.get("tool")
            preview = event.get("preview")
            self.progress.append(
                {
                    "tool": tool if isinstance(tool, str) else "",
                    "preview": preview if isinstance(preview, str) else "",
                }
            )
            return
        if kind == "error":
            message = event.get("message")
            self.error = message if isinstance(message, str) and message else "The selected agent failed."
            return
        if kind == "cancelled":
            self.cancelled = True
            return
        if isinstance(kind, str) and kind.startswith("audio"):
            self.saw_audio = True
