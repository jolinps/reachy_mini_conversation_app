"""OpenAI chat-completions shim that returns Hermes text unchanged.

speech-to-speech 1.1.0 calls this process for its LLM stage. The response body
is the gateway answer, so the runtime's TTS speaks Hermes rather than a local
model. Warmup and history compaction stay local and never reach the gateway.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from reachy_mini_conversation_app.gateway.client import GatewayError
from reachy_mini_conversation_app.voice.pin import ADAPTER_HOST, ADAPTER_PORT, MODEL_NAME
from reachy_mini_conversation_app.voice.session import GatewayVoiceSession

logger = logging.getLogger(__name__)

_WARMUP_SYSTEM = "You are a helpful assistant"
_WARMUP_USER = "Hello"
_COMPACTION_PREFIX = "You are a conversation memory compressor"


class HermesLlmAdapter:
    """Localhost chat-completions server bound to one gateway session."""

    def __init__(self, session: GatewayVoiceSession, host: str = ADAPTER_HOST, port: int = ADAPTER_PORT):
        self.session = session
        self.host = host
        self.port = port
        self.last_turn: dict[str, Any] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        owner = self

        class ReuseServer(ThreadingHTTPServer):
            allow_reuse_address = True

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                owner._handle(self)

            def log_message(self, fmt: str, *args: object) -> None:
                logger.debug("hermes adapter " + fmt, *args)

        self._server = ReuseServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="hermes-llm-adapter", daemon=True)
        self._thread.start()
        logger.info("Hermes voice adapter listening on http://%s:%s/v1", self.host, self.port)

    def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.shutdown()
            server.server_close()

    def _handle(self, request: BaseHTTPRequestHandler) -> None:
        if request.path.rstrip("/") != "/v1/chat/completions":
            _send_json(request, 404, {"error": {"message": "Not found.", "type": "not_found"}})
            return
        length = int(request.headers.get("Content-Length") or "0")
        try:
            payload = json.loads(request.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            _send_json(request, 400, {"error": {"message": "The request was not JSON.", "type": "invalid_request"}})
            return
        messages = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(messages, list):
            _send_json(request, 400, {"error": {"message": "The request had no messages.", "type": "invalid_request"}})
            return
        stream = payload.get("stream") is True
        try:
            text, local = self._answer(messages, stream, request)
        except GatewayError as exc:
            logger.info("Hermes voice turn failed: %s", exc.kind)
            if getattr(request, "_sse_open", False):
                self.session.cancel_active()
                return
            _send_json(request, 502, {"error": {"message": str(exc), "type": exc.kind}})
            return
        if local or not stream:
            _send_json(request, 200, _completion(text))
            return
        self._stream(request, text)

    def _answer(
        self,
        messages: list[dict[str, Any]],
        stream: bool,
        request: BaseHTTPRequestHandler,
    ) -> tuple[str, bool]:
        if _is_warmup(messages):
            return "ready", True
        if _is_compaction(messages):
            return _compaction_json(), True
        text = _latest_user_text(messages)
        if not text.strip():
            return "", True
        pieces: list[str] = []
        started = time.perf_counter()

        def on_delta(piece: str) -> None:
            pieces.append(piece)
            if stream:
                try:
                    _write_sse(request, _chunk(piece))
                except (BrokenPipeError, ConnectionError, OSError):
                    self.session.cancel_active()

        result = self.session.complete(text, on_delta=on_delta if stream else None)
        answer = str(result.get("answer") or "")
        evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
        self.last_turn = {
            "hermes_ms": result.get("hermes_ms"),
            "adapter_ms": round((time.perf_counter() - started) * 1000),
            "agent_id": evidence.get("agent_id"),
            "assignment_epoch": evidence.get("assignment_epoch"),
            "gateway_request_id": evidence.get("gateway_request_id"),
            "backend_instance": evidence.get("backend_instance"),
            "streamed_pieces": len(pieces),
        }
        logger.info(
            "Hermes voice turn agent=%s epoch=%s request=%s hermes_ms=%s",
            evidence.get("agent_id"),
            evidence.get("assignment_epoch"),
            evidence.get("gateway_request_id"),
            result.get("hermes_ms"),
        )
        return answer, False

    def _stream(self, request: BaseHTTPRequestHandler, text: str) -> None:
        # Deltas were already written. Close with the terminal chunk.
        try:
            _write_sse(request, _chunk("", finish="stop"))
            _write_sse(request, "[DONE]")
            request.wfile.write(b"0\r\n\r\n")
            request.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            self.session.cancel_active()
            return
        request.close_connection = True


def _is_warmup(messages: list[dict[str, Any]]) -> bool:
    rendered = [(_role(message), _content_text(message.get("content"))) for message in messages]
    return rendered == [("system", _WARMUP_SYSTEM), ("user", _WARMUP_USER)]


def _is_compaction(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        if _role(message) == "system" and _content_text(message.get("content")).startswith(_COMPACTION_PREFIX):
            return True
    return False


def _compaction_json() -> str:
    return json.dumps(
        {
            "user_summary": "The user is speaking with the selected Hermes destination.",
            "assistant_summary": "Hermes answered the committed utterances.",
        }
    )


def _latest_user_text(messages: list[dict[str, Any]]) -> str:
    text = ""
    for message in messages:
        if _role(message) == "user":
            text = _content_text(message.get("content"))
    return text


def _role(message: dict[str, Any]) -> str:
    role = message.get("role")
    return role if isinstance(role, str) else ""


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"text", "input_text"}:
                value = part.get("text")
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts)
    return ""


def _completion(text: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-hermes",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _chunk(text: str, finish: str | None = None) -> dict[str, Any]:
    delta: dict[str, str] = {"content": text} if text else {}
    return {
        "id": "chatcmpl-hermes",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _send_json(request: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    request.send_response(status)
    request.send_header("Content-Type", "application/json")
    request.send_header("Content-Length", str(len(body)))
    request.end_headers()
    request.wfile.write(body)


def _write_sse(request: BaseHTTPRequestHandler, payload: dict[str, Any] | str) -> None:
    if not getattr(request, "_sse_open", False):
        request.send_response(200)
        request.send_header("Content-Type", "text/event-stream")
        request.send_header("Cache-Control", "no-cache")
        request.send_header("Transfer-Encoding", "chunked")
        request.end_headers()
        request._sse_open = True  # type: ignore[attr-defined]
    data = payload if isinstance(payload, str) else json.dumps(payload)
    block = f"data: {data}\n\n".encode("utf-8")
    size = f"{len(block):x}\r\n".encode("ascii")
    request.wfile.write(size + block + b"\r\n")
    request.wfile.flush()
