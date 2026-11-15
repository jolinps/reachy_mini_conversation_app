"""JSON-RPC methods for the settings sections.

These methods read and save configuration. They do not open a robot connection
when the settings view asks for its current values.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from reachy_mini.io.jsonrpc import JsonRpcError
from reachy_mini.apps.jsonrpc_server import JsonRpcServer

from reachy_mini_conversation_app.settings_sections import SettingsDesk, SettingsError


def register_settings_methods(
    rpc: JsonRpcServer,
    instance_path: str | Path | None,
    turn_state: Callable[[], str | None] | None = None,
    desk: SettingsDesk | None = None,
    activity: Callable[[], dict] | None = None,
    runtime_apply: Callable | None = None,
    runtime_status: Callable[[], dict] | None = None,
) -> SettingsDesk:
    """Register settings.* on the existing console RPC server."""
    service = desk or SettingsDesk(
        instance_path,
        turn_state,
        activity=activity,
        runtime_apply=runtime_apply,
        runtime_status=runtime_status,
    )

    def guard(method):
        def wrapped(params: dict | None) -> dict:
            try:
                return method(params or {})
            except SettingsError as exc:
                raise JsonRpcError(str(exc), reason=exc.reason, code=-32602) from exc

        return wrapped

    rpc.register("settings.get", guard(lambda _params: service.snapshot()))
    rpc.register("settings.save", guard(lambda params: service.save(params)))
    rpc.register("settings.apply", guard(lambda _params: service.apply()))
    rpc.register("settings.robot_test", guard(lambda _params: service.robot_test()))
    rpc.register("settings.agents", guard(lambda _params: service.refresh_agents()))
    rpc.register(
        "settings.text_test",
        guard(lambda params: service.text_test(str(params.get("text") or "Reply with the single word pong."))),
    )
    rpc.register("settings.diagnostics", guard(lambda _params: service.diagnostics()))
    return service
