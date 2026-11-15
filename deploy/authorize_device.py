"""Grant one already-paired device an inspected Hermes destination.

The device id is fixed. This command never creates an identity and never calls
pairing or /v1/admin/pair. Without --apply it prints the operation and does
not open a network connection. Private values are read from the environment
and are not printed.

A profile whose inspected name is ``default`` is a real name. This command
does not turn that name into ``/p/default``. The listener and the routing
path have to be supplied from the host inspection.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEVICE_ID = "rch_A2VSW1KY88"
GATEWAY = "https://aiagent.jolinps.com"
PLACEHOLDERS = {"", "changeme", "change-me", "todo"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Register and grant one inspected agent. Does not pair a device.")
    parser.add_argument("--apply", action="store_true", help="Send the admin calls. The default is a dry run.")
    args = parser.parse_args()
    try:
        values = _values()
    except ValueError as exc:
        print(f"authorization not prepared: {exc}", file=sys.stderr)
        return 2
    print("device_id:", DEVICE_ID)
    print("gateway:", GATEWAY)
    print("agent_id:", values["agent_id"])
    print("observed_profile:", values["observed_profile"])
    print("routing:", values["routing"])
    print("observed_route_path:", values["route_path"])
    print("observed_instance:", values["observed_instance"])
    print("inspection_ref:", values["inspection_ref"])
    print("listener and API key: present, not printed")
    print("profile path inferred: no")
    print("calls: GET /v1/admin/devices/{device_id}")
    print("calls: POST /v1/admin/agents")
    print("calls: POST /v1/admin/devices/{device_id}/agents")
    print("calls: POST /v1/admin/agents/{agent_id}/inspection")
    print("not called: /v1/pairing/register, /v1/admin/pair, /v1/device/forget")
    if not args.apply:
        print("dry run: no request was sent")
        return 0
    token = values.pop("admin_token")
    try:
        _apply(token, values)
    except RuntimeError as exc:
        print(f"authorization failed: {exc}", file=sys.stderr)
        return 2
    return 0


def _values() -> dict:
    required = {
        "admin_token": "GATEWAY_ADMIN_TOKEN",
        "agent_id": "HERMES_AGENT_ID",
        "private_endpoint": "HERMES_PRIVATE_ENDPOINT",
        "api_key": "HERMES_API_KEY",
        "observed_profile": "OBSERVED_PROFILE",
        "observed_instance": "OBSERVED_INSTANCE",
        "inspection_ref": "INSPECTION_REF",
        "routing": "HERMES_ROUTING",
        "route_evidence": "HERMES_ROUTE_EVIDENCE",
    }
    values = {}
    missing = []
    for key, name in required.items():
        value = os.environ.get(name, "").strip()
        if value.lower() in PLACEHOLDERS or value == "/p/default":
            missing.append(name)
        else:
            values[key] = value
    if missing:
        raise ValueError(
            "Host inspection is still required. The listener and routing path were not inferred. Missing or placeholder: "
            + ", ".join(missing)
        )
    if "/" in values["observed_profile"] or values["observed_profile"] in {".", ".."}:
        raise ValueError("OBSERVED_PROFILE is not a single profile name. A path such as /p/default is not a profile name.")
    if values["routing"] not in {"private_endpoint", "profile_prefix"}:
        raise ValueError("HERMES_ROUTING must be private_endpoint or profile_prefix, chosen from the host inspection.")
    if not values["private_endpoint"].startswith(("http://", "https://")):
        raise ValueError("HERMES_PRIVATE_ENDPOINT must be the listener URL read on the host.")
    if "aiagent.jolinps.com" in values["private_endpoint"]:
        raise ValueError("HERMES_PRIVATE_ENDPOINT must be the Hermes listener, not the public gateway.")
    endpoint, route_path = _require_observed_route(values["private_endpoint"], values["routing"], values["observed_profile"], values["route_evidence"])
    values["private_endpoint"] = endpoint
    values["route_path"] = route_path
    return values


def _apply(token: str, values: dict) -> None:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    status = _request("GET", f"/v1/admin/devices/{DEVICE_ID}", None, headers)
    if status.get("device_id") != DEVICE_ID or status.get("status") != "paired":
        raise RuntimeError("This device is not already paired. Pairing was not started.")
    created = _request(
        "POST",
        "/v1/admin/agents",
        {
            "agent_id": values["agent_id"],
            "framework": "hermes",
            "instance_id": values["observed_instance"],
            "profile": values["observed_profile"],
            "private_endpoint": values["private_endpoint"],
            "api_key": values["api_key"],
            "display_name": values["agent_id"],
            "routing": values["routing"],
        },
        headers,
    )
    _request("POST", f"/v1/admin/devices/{DEVICE_ID}/agents", {"agent_id": values["agent_id"]}, headers)
    _request(
        "POST",
        f"/v1/admin/agents/{values['agent_id']}/inspection",
        {
            "inspection_ref": values["inspection_ref"],
            "observed_profile": values["observed_profile"],
            "observed_instance": values["observed_instance"],
        },
        headers,
    )
    print("granted:", DEVICE_ID, created.get("agent_id", values["agent_id"]))
    print("bki_ identifies the gateway registration row created for this grant.")
    print("It was minted by the gateway and was not sent in this request.")
    print("It does not by itself identify the Hermes process. That comes from the recorded inspection and the resolved listener.")


def _require_observed_route(endpoint: str, routing: str, profile: str, evidence: str) -> tuple[str, str]:
    """Check the supplied listener and path. Do not invent a profile path."""
    listener = endpoint.rstrip("/")
    observed = evidence.rstrip("/")
    if not observed.startswith(("http://", "https://")):
        raise ValueError("HERMES_ROUTE_EVIDENCE must be the URL observed for this route. No path was inferred.")
    listener_parts = urlparse(listener)
    observed_parts = urlparse(observed)
    if listener_parts.scheme != observed_parts.scheme or listener_parts.netloc != observed_parts.netloc:
        raise ValueError("The observed route is not on the configured listener. No other path was substituted.")
    observed_path = observed_parts.path or "/"
    if routing == "private_endpoint":
        if observed_path not in {"", "/"}:
            raise ValueError(
                "private_endpoint routing was observed with an extra path. "
                "That path was not rewritten, and /p/default was not inferred."
            )
        return listener, "/"
    suffix = "/p/" + profile
    if observed_path != suffix:
        raise ValueError(
            "profile_prefix routing does not match the observed path. "
            "The path was not inferred from the profile name."
        )
    return listener, suffix


def _request(method: str, path: str, body: dict | None, headers: dict) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(GATEWAY + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        exc.read()
        raise RuntimeError(f"{method} {path} was rejected ({exc.code}).") from None
    if not isinstance(payload, dict):
        raise RuntimeError(f"{method} {path} did not return JSON.")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
