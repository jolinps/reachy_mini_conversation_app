"""Validated settings for the existing console.

Saved values live in the instance directory. Accepted values are a second file.
Neither file is a live connection, and opening this model does not touch a robot.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from reachy_mini_conversation_app.gateway.client import GatewayClient, GatewayError
from reachy_mini_conversation_app.gateway.identity import IdentityError, load_identity
from reachy_mini_conversation_app.gateway.ndjson import TurnParser
from reachy_mini_conversation_app.gateway.profiles import discovery_from_paths
from reachy_mini_conversation_app.gateway.turn import BoundTurn
from reachy_mini_conversation_app.settings_store import (
    AppSettings,
    read_settings,
    update_settings,
)

SECTIONS = (
    {"id": "robot", "title": "Robot & Simulator"},
    {"id": "agent", "title": "AI Agent"},
    {"id": "voice", "title": "Voice"},
    {"id": "conversation", "title": "Conversation"},
    {"id": "tools", "title": "Tools & Modules"},
    {"id": "diagnostics", "title": "Diagnostics"},
)

# Existing controls stay on their current RPC methods. These ids only say where
# the console shows them.
EXISTING_CONTROLS = {
    "voice": ("backend.config",),
    "conversation": ("voices.apply", "speech.config", "language.set", "personalities.*"),
    "tools": ("profile_tools.*", "tool_spaces.*"),
    "diagnostics": ("conversation.status",),
}

ACTIVE_TURNS = {"thinking", "speaking"}
RUNTIME_FIELDS = (
    "hf_connection_mode",
    "hf_ws_url",
    "local_vad_enabled",
    "local_barge_in_enabled",
)
SAVED_ONLY_FIELDS = (
    "robot_mode",
    "robot_host",
    "robot_port",
    "robot_name",
    "robot_advanced_host",
    "gateway_url",
    "gateway_agent_id",
    "device_identity_path",
    "voice_route",
    "stt_provider",
    "tts_provider",
    "stt_language",
    "tts_voice",
    "voice_runtime_url",
)
_ROBOT_MODES = {"physical", "simulator"}
_VOICE_ROUTES = {"huggingface", "hermes"}
_SPEECH_PROVIDERS = {"hosted_hf", "external", "parakeet-tdt", "qwen3-asr", "kitten", "qwen3", "pocket"}
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_SECRET_KEYS = {
    "api_key",
    "token",
    "private_key",
    "private_key_hex",
    "authorization",
    "session_token",
    "signature",
    "answer",
    "transcript",
    "text",
}
_SECRET_TEXT = re.compile(r"(api[_-]?key|token|private[_-]?key|bearer)\s*[:=]\s*\S+", re.I)


class SettingsError(ValueError):
    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def redact(value: Any) -> Any:
    """Remove secrets and transcripts from a value that may be exported."""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if str(key).lower() in _SECRET_KEYS:
                cleaned[key] = "<redacted>"
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_TEXT.sub("<redacted>", value)
    return value


def validate_changes(raw: dict[str, Any]) -> AppSettings:
    """Accept only known, non-secret configuration fields."""
    if not isinstance(raw, dict):
        raise SettingsError("Settings were not an object.", "invalid_settings")
    for key, value in raw.items():
        if str(key).lower() in _SECRET_KEYS:
            raise SettingsError("Secrets are not stored in the console.", "secret_rejected")
        if isinstance(value, str) and _SECRET_TEXT.search(value):
            raise SettingsError("That value looks like a credential.", "secret_rejected")
    unknown = sorted(set(raw) - set(AppSettings.__dataclass_fields__))
    if unknown:
        raise SettingsError("That setting is not part of this console.", "unknown_setting")

    def text(name: str) -> str | None:
        if name not in raw:
            return None
        value = raw[name]
        if not isinstance(value, str) or not value.strip():
            raise SettingsError(f"Enter a value for {name}.", "invalid_settings")
        return value.strip()

    robot_mode = text("robot_mode") if "robot_mode" in raw else None
    if robot_mode is not None and robot_mode not in _ROBOT_MODES:
        raise SettingsError("Choose the physical robot or the local simulator.", "invalid_robot_mode")
    robot_host = text("robot_host") if "robot_host" in raw else None
    advanced = text("robot_advanced_host") if "robot_advanced_host" in raw else None
    for host in (robot_host, advanced):
        if host is not None and (_HOST.match(host) is None or "://" in host):
            raise SettingsError("Enter a host name or address without a login.", "invalid_robot_host")
    robot_port = None
    if "robot_port" in raw:
        value = raw["robot_port"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 65535:
            raise SettingsError("Enter a port from 1 to 65535.", "invalid_robot_port")
        robot_port = value
    robot_name = text("robot_name") if "robot_name" in raw else None
    if robot_name is not None and _AGENT_ID.match(robot_name) is None:
        raise SettingsError("That robot name is not valid.", "invalid_robot_name")

    gateway_url = text("gateway_url") if "gateway_url" in raw else None
    if gateway_url is not None:
        parsed = urlparse(gateway_url)
        loopback = parsed.scheme == "http" and (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
        if not ((parsed.scheme == "https" and parsed.hostname) or loopback):
            raise SettingsError("Enter an https gateway address.", "invalid_gateway_url")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise SettingsError("The gateway address cannot include a login or query.", "invalid_gateway_url")
    agent_id = text("gateway_agent_id") if "gateway_agent_id" in raw else None
    if agent_id is not None and _AGENT_ID.match(agent_id) is None:
        raise SettingsError("That agent id is not valid.", "invalid_agent")
    identity = text("device_identity_path") if "device_identity_path" in raw else None
    if identity is not None and not Path(identity).is_absolute():
        raise SettingsError("The device identity path must be absolute.", "invalid_identity_path")

    voice_route = text("voice_route") if "voice_route" in raw else None
    if voice_route is not None and voice_route not in _VOICE_ROUTES:
        raise SettingsError("Choose the Hugging Face baseline or the Hermes voice route.", "invalid_voice_route")
    for name in ("stt_provider", "tts_provider"):
        if name in raw and text(name) not in _SPEECH_PROVIDERS:
            raise SettingsError("That speech provider is not one of the known choices.", "invalid_provider")
    runtime = text("voice_runtime_url") if "voice_runtime_url" in raw else None
    if runtime is not None:
        parsed = urlparse(runtime)
        if parsed.scheme not in {"ws", "wss", "http", "https"} or not parsed.hostname:
            raise SettingsError("Enter a voice-runtime address.", "invalid_voice_runtime")

    return AppSettings(
        robot_mode=robot_mode,
        robot_host=robot_host,
        robot_port=robot_port,
        robot_name=robot_name,
        robot_advanced_host=advanced,
        gateway_url=gateway_url.rstrip("/") if gateway_url else None,
        gateway_agent_id=agent_id,
        device_identity_path=identity,
        voice_route=voice_route,
        stt_provider=text("stt_provider") if "stt_provider" in raw else None,
        tts_provider=text("tts_provider") if "tts_provider" in raw else None,
        stt_language=text("stt_language") if "stt_language" in raw else None,
        tts_voice=text("tts_voice") if "tts_voice" in raw else None,
        voice_runtime_url=runtime,
    )


def probe_daemon_status(host: str, port: int, timeout_s: float = 2.0) -> dict[str, Any]:
    """Read daemon status. This never starts, stops, or moves the robot."""
    url = f"http://{host}:{port}/api/daemon/status"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return {"tested": True, "ok": False, "message": "The robot did not answer a status check.", "detail": type(exc).__name__}
    if not isinstance(payload, dict):
        return {"tested": True, "ok": False, "message": "The robot status was not readable."}
    simulation = payload.get("simulation_enabled") is True
    return {
        "tested": True,
        "ok": payload.get("state") == "running",
        "state": payload.get("state") if isinstance(payload.get("state"), str) else "unknown",
        "robot_name": payload.get("robot_name") if isinstance(payload.get("robot_name"), str) else "",
        "simulation_enabled": simulation,
        "verified_simulation": simulation and payload.get("state") == "running",
        "message": "Status check completed. The robot was not started or moved.",
    }


class SettingsDesk:
    """Saved configuration, accepted configuration, and explicit tests."""

    def __init__(
        self,
        instance_path: str | Path | None,
        turn_state: Callable[[], str | None] | None = None,
        robot_probe: Callable[[str, int], dict[str, Any]] | None = None,
        client_factory: Callable[[str, Path], GatewayClient] | None = None,
        activity: Callable[[], dict[str, Any]] | None = None,
        runtime_apply: Callable[[AppSettings], dict[str, Any]] | None = None,
        runtime_status: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.instance_path = instance_path
        self.turn_state = turn_state or (lambda: None)
        self.robot_probe = robot_probe or probe_daemon_status
        self.client_factory = client_factory or (lambda url, path: GatewayClient(url, load_identity(path)))
        self.activity = activity
        self.runtime_apply = runtime_apply
        self.runtime_status = runtime_status
        self._robot_result: dict[str, Any] | None = None
        self._agent_result: dict[str, Any] | None = None
        self._catalog: dict[str, Any] = {
            "status": "unsupported",
            "error": "The profile catalog has not been requested.",
            "profiles": [],
            "source": "none",
        }
        self._agents: list[dict[str, Any]] = []

    def snapshot(self) -> dict[str, Any]:
        saved = read_settings(self.instance_path)
        activity = self._activity()
        runtime = self._runtime_state()
        application = _application(saved, runtime)
        state = activity["turn"]
        return redact(
            {
                "sections": list(SECTIONS),
                "existing_controls": EXISTING_CONTROLS,
                "saved": _public_settings(saved),
                "applied": {"fields": list(runtime.get("applied_fields") or [])},
                "pending_apply": _saved_only(saved, runtime),
                "pending_restart": list(runtime.get("pending_restart") or []),
                "application": application,
                "turn_state": state,
                "apply_blocked": _busy_reason(activity) is not None,
                "live_audio_route": (runtime.get("voice") or {}).get("live_route") or "huggingface",
                "statuses": self._statuses(saved, runtime),
                "robot_test": _public_test(self._robot_result),
                "agent_test": _public_test(self._agent_result),
            }
        )

    def save(self, raw: dict[str, Any]) -> dict[str, Any]:
        if self.instance_path is None:
            raise SettingsError("This app has no instance directory for settings.", "settings_unavailable")
        changes = validate_changes(raw)
        update_settings(self.instance_path, changes)
        return self.snapshot()

    def apply(self) -> dict[str, Any]:
        reason = _busy_reason(self._activity())
        if reason == "apply_during_turn":
            raise SettingsError(
                "A turn is in progress. Apply the connection after it finishes.",
                reason,
            )
        if reason == "apply_during_playback":
            raise SettingsError("Audio is still playing. Apply the connection after it finishes.", reason)
        if reason == "apply_during_tool":
            raise SettingsError("A tool is still running. Apply the connection after it finishes.", reason)
        if self.instance_path is None:
            raise SettingsError("This app has no instance directory for settings.", "settings_unavailable")
        saved = read_settings(self.instance_path)
        if self.runtime_apply is not None:
            result = self.runtime_apply(saved)
            if not isinstance(result, dict):
                raise SettingsError("The runtime did not accept the settings.", "settings_unavailable")
        return self.snapshot()

    def _activity(self) -> dict[str, Any]:
        if self.activity is not None:
            reported = self.activity() or {}
            turn = reported.get("turn") or self.turn_state() or "idle"
            return {
                "turn": turn,
                "playback": bool(reported.get("playback")),
                "tool_execution": bool(reported.get("tool_execution")),
            }
        return {"turn": self.turn_state() or "idle", "playback": False, "tool_execution": False}

    def _runtime_state(self) -> dict[str, Any]:
        if self.runtime_status is None:
            return {"state": "unchanged", "applied_fields": [], "pending_restart": []}
        reported = self.runtime_status() or {}
        voice = reported.get("voice") if isinstance(reported.get("voice"), dict) else {}
        return {
            "state": reported.get("state") if reported.get("state") in {"unchanged", "applied", "pending_restart"} else "unchanged",
            "applied_fields": [name for name in reported.get("applied_fields") or [] if name in RUNTIME_FIELDS],
            "pending_restart": [name for name in reported.get("pending_restart") or [] if name in RUNTIME_FIELDS],
            "voice": voice,
        }

    def robot_test(self) -> dict[str, Any]:
        saved = read_settings(self.instance_path)
        host = saved.robot_advanced_host or saved.robot_host
        port = saved.robot_port
        if not host or port is None:
            self._robot_result = {
                "tested": False,
                "ok": False,
                "message": "Save a robot address before testing the connection.",
            }
            return redact(self._robot_result)
        self._robot_result = self.robot_probe(host, port)
        return redact(self._robot_result)

    def refresh_agents(self) -> dict[str, Any]:
        client, failure = self._client()
        if client is None:
            return redact({"ok": False, **failure})
        saved_agent = read_settings(self.instance_path).gateway_agent_id or ""
        started = time.perf_counter()
        try:
            health = client.health()
            auth_started = time.perf_counter()
            token = client.authenticate()
            auth_ms = round((time.perf_counter() - auth_started) * 1000)
            listed = client.agents(token)
            paths = client.openapi_paths()
        except GatewayError as exc:
            return redact({"ok": False, "message": str(exc), "reason": exc.kind})
        catalog = discovery_from_paths(paths)
        if catalog is None and saved_agent:
            try:
                catalog = client.catalog(token, saved_agent)
            except GatewayError as exc:
                catalog = {"status": "failed", "error": str(exc), "profiles": [], "source": "gateway"}
        elif catalog is None:
            catalog = {
                "status": "supported",
                "error": "Select an authorized agent before refreshing its catalog.",
                "profiles": [],
                "source": "gateway",
            }
        self._catalog = catalog
        agents = []
        for row in listed.get("agents") or []:
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                agents.append(
                    {
                        "id": row["id"],
                        "name": row.get("name") if isinstance(row.get("name"), str) else "",
                        "enabled": row.get("enabled") is True,
                    }
                )
        self._agents = agents
        return redact(
            {
                "ok": True,
                "device_id": client.identity.device_id,
                "authenticated": True,
                "agents": agents,
                "catalog": catalog or {"status": "supported"},
                "observed_session_profile": None,
                "timing_ms": {"health": round((auth_started - started) * 1000), "authentication": auth_ms},
                "contract": {
                    "chat_binding": health.get("chat_binding") == 1,
                    "text_only": health.get("text_only") is True,
                },
            }
        )

    def text_test(self, text: str = "Reply with the single word pong.") -> dict[str, Any]:
        client, failure = self._client()
        if client is None:
            self._agent_result = {"tested": False, "ok": False, **failure}
            return redact(self._agent_result)
        saved = read_settings(self.instance_path)
        agent_id = saved.gateway_agent_id or ""
        if not text.strip():
            raise SettingsError("Enter a harmless text prompt.", "invalid_settings")
        started = time.perf_counter()
        try:
            client.health()
            auth_started = time.perf_counter()
            token = client.authenticate()
            auth_ms = round((time.perf_counter() - auth_started) * 1000)
            select_started = time.perf_counter()
            selected = client.select(token, agent_id)
            select_ms = round((time.perf_counter() - select_started) * 1000)
            epoch = selected.get("epoch")
            if selected.get("agent_id") != agent_id or isinstance(epoch, bool) or not isinstance(epoch, int):
                raise GatewayError("The gateway did not select that agent.", "mismatch")
            timing, answer, evidence = _time_chat(client, token, agent_id, epoch, text)
        except (GatewayError, SettingsError) as exc:
            reason = getattr(exc, "kind", None) or getattr(exc, "reason", "rejected")
            self._agent_result = {"tested": True, "ok": False, "message": str(exc), "reason": reason}
            return redact(self._agent_result)
        self._agent_result = {
            "tested": True,
            "ok": True,
            "agent_id": agent_id,
            "answer": answer,
            "evidence": evidence,
            "timing_ms": {"authentication": auth_ms, "selection": select_ms, **timing},
            "setup_ms": round((time.perf_counter() - started) * 1000),
            "observed_session_profile": None,
            "progress_is_not_answer": True,
        }
        public = redact({key: value for key, value in self._agent_result.items() if key != "answer"})
        public["answer"] = answer
        return public

    def diagnostics(self) -> dict[str, Any]:
        payload = self.snapshot()
        payload["robot_test"] = _public_test(self._robot_result)
        payload["agent_test"] = _public_test(self._agent_result)
        return redact(payload)

    def _client(self) -> tuple[GatewayClient | None, dict[str, str]]:
        saved = read_settings(self.instance_path)
        if not saved.gateway_url:
            return None, {"message": "Save a gateway address first.", "reason": "gateway_missing"}
        if not saved.device_identity_path:
            return None, {"message": "Choose the existing device identity file first.", "reason": "identity_missing"}
        path = Path(saved.device_identity_path)
        try:
            return self.client_factory(saved.gateway_url, path), {}
        except (GatewayError, IdentityError) as exc:
            return None, {"message": str(exc), "reason": "identity_unavailable"}

    def _statuses(self, saved: AppSettings, runtime: dict[str, Any]) -> dict[str, Any]:
        identity = _identity_status(saved.device_identity_path)
        return {
            "robot": {
                "connection": "not_tested" if self._robot_result is None else ("ok" if self._robot_result.get("ok") else "failed"),
                "simulation": "not_tested" if self._robot_result is None else (
                    "verified" if self._robot_result.get("verified_simulation") else "not_simulation"
                ),
            },
            "gateway": {
                "authentication": "not_tested" if not self._agents and self._agent_result is None else (
                    "ok" if self._agents or (self._agent_result or {}).get("ok") else "failed"
                ),
                "authorization_status": _authorization(self._agents, saved.gateway_agent_id),
                "latency_ms": _latency(self._agent_result),
                "catalog": self._catalog.get("status") or "unsupported",
                "observed_session_profile": None,
                "agents": self._agents,
            },
            "voice": _voice_status(saved, runtime),
            "credentials": {
                "device_identity": identity,
                "gateway_admin": "server_side",
                "stt": "missing" if saved.stt_provider == "external" else "not_required_for_hosted_hf",
                "tts": "missing" if saved.tts_provider == "external" else "not_required_for_hosted_hf",
            },
        }


def _latency(result: dict[str, Any] | None) -> dict[str, int] | None:
    if not result:
        return None
    timing = result.get("timing_ms")
    if not isinstance(timing, dict):
        return None
    allowed = ("authentication", "selection", "gateway_bound", "first_answer", "final_answer", "progress_events")
    measured = {name: timing[name] for name in allowed if isinstance(timing.get(name), int)}
    return measured or None


def _authorization(agents: list[dict[str, Any]], agent_id: str | None) -> str:
    if not agents:
        return "not_tested"
    if not agent_id:
        return "choose_agent"
    enabled = {row["id"] for row in agents if row.get("enabled")}
    return "granted" if agent_id in enabled else "not_granted"


def _provider_status(provider: str | None) -> str:
    if provider in {None, "", "hosted_hf"}:
        return "inside_huggingface_conversation"
    if provider == "external":
        return "credential_missing"
    return provider


def _identity_status(path: str | None) -> dict[str, Any]:
    if not path:
        return {"configured": False, "file_name": ""}
    file_path = Path(path)
    status: dict[str, Any] = {"configured": file_path.is_file(), "file_name": file_path.name}
    if file_path.is_file():
        try:
            status["device_id"] = load_identity(file_path).device_id
        except IdentityError:
            status["configured"] = False
    return status


def _public_settings(settings: AppSettings) -> dict[str, Any]:
    payload = {
        "robot_mode": settings.robot_mode,
        "robot_host": settings.robot_host,
        "robot_port": settings.robot_port,
        "robot_name": settings.robot_name,
        "robot_advanced_host": settings.robot_advanced_host,
        "gateway_url": settings.gateway_url,
        "gateway_agent_id": settings.gateway_agent_id,
        "voice_route": settings.voice_route or "huggingface",
        "stt_provider": settings.stt_provider or "hosted_hf",
        "tts_provider": settings.tts_provider or "hosted_hf",
        "stt_language": settings.stt_language or "",
        "tts_voice": settings.tts_voice or "",
        "voice_runtime_url": settings.voice_runtime_url or "",
        "identity": _identity_status(settings.device_identity_path),
    }
    return payload


def _busy_reason(activity: dict[str, Any]) -> str | None:
    if activity.get("turn") in ACTIVE_TURNS:
        return "apply_during_turn"
    if activity.get("playback"):
        return "apply_during_playback"
    if activity.get("tool_execution"):
        return "apply_during_tool"
    return None


def _saved_only(saved: AppSettings, runtime: dict[str, Any] | None = None) -> list[str]:
    names = [name for name in SAVED_ONLY_FIELDS if getattr(saved, name) is not None]
    applied = set(((runtime or {}).get("voice") or {}).get("applied_fields") or [])
    return [name for name in names if name not in applied]


def _voice_route_state(saved: AppSettings, voice: dict[str, Any] | None = None) -> str:
    reported = voice or {}
    if reported.get("applied") is True and reported.get("active_mode") == "hermes":
        return "active"
    if reported.get("state") == "pending_restart":
        return "pending_restart"
    if (
        saved.voice_route == "hermes"
        or saved.stt_provider == "external"
        or saved.tts_provider == "external"
        or saved.voice_runtime_url
    ):
        return "unavailable"
    return "live_huggingface"


def _voice_status(saved: AppSettings, runtime: dict[str, Any]) -> dict[str, Any]:
    voice = runtime.get("voice") if isinstance(runtime.get("voice"), dict) else {}
    connected = voice.get("webrtc") == "connected"
    return {
        "saved_route": saved.voice_route or "huggingface",
        "active_route": voice.get("active_mode") or "huggingface",
        "live_route": voice.get("live_route") or "huggingface",
        "route_state": _voice_route_state(saved, voice),
        "hf_speech": runtime.get("state") or "unchanged",
        "transport": "webrtc" if connected else "unavailable",
        "webrtc": voice.get("webrtc") or "not_connected",
        "hosted_hf_supports_custom_providers": False,
        "saved_stt": saved.stt_provider or "",
        "saved_tts": saved.tts_provider or "",
        "stt": voice.get("stt_provider") or _provider_status(saved.stt_provider),
        "tts": voice.get("tts_provider") or _provider_status(saved.tts_provider),
        "stt_language": voice.get("stt_language") or saved.stt_language or "",
        "tts_voice": voice.get("tts_voice") or saved.tts_voice or "",
        "runtime_url": voice.get("runtime_url") or saved.voice_runtime_url or "",
        "agent_id": voice.get("agent_id") or saved.gateway_agent_id or "",
        "credential_blocker": voice.get("credential_blocker") or "",
        "microphone": "push_to_talk" if connected else "gated",
        "speaker": "official_playback" if connected else "official_playback",
    }


def _application(saved: AppSettings, runtime: dict[str, Any]) -> dict[str, Any]:
    voice = runtime.get("voice") if isinstance(runtime.get("voice"), dict) else {}
    return {
        "robot": {
            "state": "saved" if any(getattr(saved, name) is not None for name in ("robot_mode", "robot_host", "robot_port", "robot_name", "robot_advanced_host")) else "unset",
            "applied": False,
        },
        "gateway": {
            "state": "saved" if any(getattr(saved, name) is not None for name in ("gateway_url", "gateway_agent_id", "device_identity_path")) else "unset",
            "applied": False,
        },
        "voice_route": {
            "state": _voice_route_state(saved, voice),
            "applied": voice.get("applied") is True,
            "saved_mode": saved.voice_route or "huggingface",
            "active_mode": voice.get("active_mode") or "huggingface",
            "webrtc": voice.get("webrtc") or "not_connected",
            "live_route": voice.get("live_route") or "huggingface",
            "runtime_url": voice.get("runtime_url") or saved.voice_runtime_url or "",
            "stt_provider": voice.get("stt_provider") or "",
            "tts_provider": voice.get("tts_provider") or "",
            "stt_language": voice.get("stt_language") or "",
            "tts_voice": voice.get("tts_voice") or "",
            "agent_id": voice.get("agent_id") or saved.gateway_agent_id or "",
        },
        "hf_speech": {
            "state": runtime.get("state") or "unchanged",
            "applied_fields": list(runtime.get("applied_fields") or []),
            "pending_restart": list(runtime.get("pending_restart") or []),
        },
    }


def _public_test(result: dict[str, Any] | None) -> dict[str, Any]:
    if result is None:
        return {"tested": False}
    hidden = {"answer", "text", "transcript"}
    return {key: value for key, value in result.items() if key not in hidden}


def _time_chat(client: GatewayClient, token: str, agent_id: str, epoch: int, text: str) -> tuple[dict[str, int], str, dict]:
    turn = BoundTurn(client, token, agent_id, epoch)
    parser = TurnParser()
    request_id = "req-settings"
    body = {
        "request_id": request_id,
        "conversation_id": "reachy-not-used",
        "text": text,
        "expected_agent_id": agent_id,
        "expected_epoch": epoch,
        "text_only": True,
    }
    sent = time.perf_counter()
    first_event = None
    first_answer = None
    final_at = None
    progress = 0
    try:
        for chunk in client.chat(token, body):
            now = time.perf_counter()
            for event in parser.feed(chunk):
                if first_event is None:
                    first_event = now
                if event.get("type") == "progress":
                    progress += 1
                if event.get("type") == "response_text" and isinstance(event.get("text"), str):
                    if first_answer is None:
                        first_answer = now
                    if event.get("final") is True:
                        final_at = now
        for event in parser.finish():
            now = time.perf_counter()
            if event.get("type") == "response_text" and event.get("final") is True:
                final_at = now
                if first_answer is None:
                    first_answer = now
    except GatewayError:
        turn._cancel(request_id)
        raise
    if parser.saw_audio:
        raise GatewayError("The gateway sent audio on a text-only test.", "rejected")
    if parser.error:
        raise GatewayError(parser.error, "rejected")
    if parser.final_text is None:
        raise GatewayError("The gateway closed the test without a final answer.", "rejected")
    evidence = {
        "agent_id": parser.evidence.get("agent_id"),
        "assignment_epoch": parser.evidence.get("assignment_epoch"),
        "gateway_request_id": parser.evidence.get("gateway_request_id"),
        "backend_instance": parser.evidence.get("backend_instance"),
    }
    if evidence["agent_id"] != agent_id or evidence["assignment_epoch"] != epoch:
        raise GatewayError("The test answer did not match the selected agent.", "mismatch")

    def millis(mark: float | None) -> int | None:
        if mark is None:
            return None
        return round((mark - sent) * 1000)

    return (
        {
            "gateway_bound": millis(first_event) or 0,
            "first_answer": millis(first_answer) or 0,
            "final_answer": millis(final_at) or 0,
            "progress_events": progress,
        },
        parser.final_text,
        evidence,
    )
