"""Text probe for one authorized gateway agent.

This does not open the microphone, play audio, move the robot, or change a
pairing. It sends a chat only when the gateway advertises binding and
text-only turns and returns an epoch for the selected agent.

Run it from the upgrade worktree:

    PYTHONPATH=src python -m reachy_mini_conversation_app.gateway.probe \\
        --identity /path/to/identity.json --text "Reply with the single word pong."
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from reachy_mini_conversation_app.gateway.client import GatewayClient, GatewayError
from reachy_mini_conversation_app.gateway.identity import IdentityError, load_identity
from reachy_mini_conversation_app.gateway.profiles import discovery_from_catalog, discovery_from_paths, unsupported
from reachy_mini_conversation_app.gateway.redact import redact
from reachy_mini_conversation_app.gateway.turn import BoundTurn

DEFAULT_GATEWAY = "https://aiagent.jolinps.com"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send one text turn through the device gateway.")
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY)
    parser.add_argument("--identity", required=True, help="Existing device identity file. This command does not create one.")
    parser.add_argument("--agent", default="", help="Authorized agent id. Required when more than one is authorized.")
    parser.add_argument("--text", default="Reply with the single word pong.")
    parser.add_argument("--list-only", action="store_true", help="Authenticate and list agents, then stop before select.")
    args = parser.parse_args(argv)
    try:
        identity = load_identity(Path(args.identity))
        report = run_probe(
            GatewayClient(args.gateway, identity),
            agent_id=args.agent,
            text=args.text,
            list_only=args.list_only,
        )
    except (GatewayError, IdentityError) as exc:
        print(f"gateway probe failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["chat"]["sent"] else 2


def run_probe(client: GatewayClient, agent_id: str, text: str, list_only: bool = False) -> dict:
    """Authenticate and, when the deployed contract allows it, send one bound text turn."""
    try:
        return _run(client, agent_id, text, list_only)
    except GatewayError as exc:
        raise GatewayError(redact(str(exc), client.secrets), exc.kind) from None


def _run(client: GatewayClient, agent_id: str, text: str, list_only: bool) -> dict:
    health = client.health()
    paths = client.openapi_paths()
    token = client.authenticate()
    listed = client.agents(token)
    agents = _authorized(listed)
    profiles = _profiles(client, token, paths, [agent["id"] for agent in agents])
    contract = _contract(health)
    chat = {"sent": False, "answer": "", "progress": [], "blocker": ""}
    selected = None
    evidence = None
    chosen = ""
    if list_only:
        chat["blocker"] = "List only: no destination was selected and no chat was sent."
    elif not any(agent["enabled"] for agent in agents):
        chat["blocker"] = "This device has no enabled authorized agent. No destination was selected and no chat was sent."
        if not contract:
            chat["blocker"] = f"{chat['blocker']} {_contract_blocker()}"
    elif not contract:
        chat["blocker"] = _contract_blocker()
    else:
        chosen = _choose(agents, agent_id)
    if chosen and contract and not list_only:
        selected = _select(client, token, chosen)
        turn = BoundTurn(client, token, selected["agent_id"], selected["epoch"])
        result = turn.send(text)
        evidence = result["evidence"]
        chat = {"sent": True, "answer": result["answer"], "progress": result["progress"], "blocker": ""}
    report = {
        "device_id": client.identity.device_id,
        "health_is_not_routing_proof": True,
        "hermes_ok": health.get("hermes_ok") is True,
        "chat_binding": health.get("chat_binding") == 1,
        "text_only": health.get("text_only") is True,
        "profile_discovery": profiles,
        "authorized_agents": agents,
        "selected_agent_id": selected["agent_id"] if selected else "",
        "assignment_epoch": selected["epoch"] if selected else None,
        "routing_evidence": evidence if evidence is not None else _no_evidence(selected["agent_id"] if selected else chosen),
        "chat": chat,
        "hermes_instance_proven": False,
    }
    _assert_public(report, client.secrets)
    return report


def _profiles(client: GatewayClient, token: str, paths: set[str], agent_ids: list[str]) -> dict:
    """Read a catalog only when the deployed API publishes that route."""
    missing = discovery_from_paths(paths)
    if missing is not None:
        return missing
    if not agent_ids:
        return unsupported("The catalog route is published, but this device has no authorized agent to inspect.")
    found = []
    seen: set[str] = set()
    source = ""
    fingerprint = ""
    for agent_id in agent_ids:
        try:
            payload = client.catalog(token, agent_id)
        except GatewayError as exc:
            return unsupported(str(exc))
        parsed = discovery_from_catalog(payload)
        if parsed["status"] != "fresh":
            return parsed
        source = parsed["source"] or source
        fingerprint = parsed["fingerprint"] or fingerprint
        for profile in parsed["profiles"]:
            if profile["id"] not in seen:
                seen.add(profile["id"])
                found.append(profile)
    return {
        "status": "fresh",
        "error": "",
        "profiles": found,
        "source": source or "gateway",
        "fingerprint": fingerprint,
    }


def _authorized(payload: dict) -> list[dict]:
    rows = payload.get("agents") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise GatewayError("The gateway agent list was not a list.", "rejected")
    agents = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            continue
        agents.append(
            {
                "id": row["id"].strip(),
                "name": row.get("name") if isinstance(row.get("name"), str) else "",
                "enabled": row.get("enabled") is True,
                "active": row.get("active") is True,
                "instance_id": row.get("instance_id") if isinstance(row.get("instance_id"), str) else "",
                "server_name": row.get("server_name") if isinstance(row.get("server_name"), str) else "",
                "framework": row.get("framework") if isinstance(row.get("framework"), str) else "",
            }
        )
    return agents


def _choose(agents: list[dict], requested: str) -> str:
    enabled = [agent["id"] for agent in agents if agent["enabled"]]
    if requested:
        if requested not in enabled:
            raise GatewayError(
                "That agent is not an enabled agent authorized for this device. No other agent was used.",
                "mismatch",
            )
        return requested
    if len(enabled) == 1:
        return enabled[0]
    if not enabled:
        raise GatewayError("This device has no enabled authorized agent.", "unsupported")
    raise GatewayError(
        "More than one agent is authorized. Pass --agent with the id to use. None was selected.",
        "configuration",
    )


def _contract(health: dict) -> bool:
    return health.get("chat_binding") == 1 and health.get("text_only") is True


def _contract_blocker() -> str:
    return (
        "The gateway /health response does not advertise chat_binding 1 and text_only true. "
        "A chat was not sent, because this deployment would not be required to honor the "
        "destination binding or to keep the turn text-only."
    )


def _select(client: GatewayClient, token: str, agent_id: str) -> dict:
    payload = client.select(token, agent_id)
    selected = payload.get("agent_id")
    epoch = payload.get("epoch")
    if selected != agent_id:
        raise GatewayError(
            "The gateway selected a different agent. The request was not sent.",
            "mismatch",
        )
    if isinstance(epoch, bool) or not isinstance(epoch, int):
        raise GatewayError(
            "The gateway did not return an assignment epoch for the selected agent. The request was not sent.",
            "unsupported",
        )
    return {"agent_id": agent_id, "epoch": epoch}


def _no_evidence(agent_id: str) -> dict:
    """No registry label or health flag is routing evidence."""
    return {
        "agent_id": agent_id,
        "assignment_epoch": None,
        "gateway_request_id": "",
        "backend_instance": "",
        "source": "none",
        "backend_instance_meaning": "gateway_registration",
        "host_inspection": False,
        "proves_answering_instance": False,
    }


def _assert_public(report: dict, secrets: list[str]) -> None:
    encoded = json.dumps(report)
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= 8 and secret in encoded:
            raise GatewayError("The probe report contained a secret and was discarded.", "rejected")


if __name__ == "__main__":
    raise SystemExit(main())
