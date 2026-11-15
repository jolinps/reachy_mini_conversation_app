"""Backend profile discovery. Application personalities are not profiles.

A registry label, a health payload, and an empty catalog do not become the
name ``default``. ``/p/default`` is not guessed.
"""

from __future__ import annotations

CATALOG_ROUTE = "/v1/agents/{agent_id}/catalog"


def discovery_from_paths(paths: set[str]) -> dict | None:
    """Return an unsupported result when the deployed API has no catalog route."""
    if CATALOG_ROUTE in paths or any(path.endswith("/catalog") for path in paths):
        return None
    return unsupported(
        "The deployed gateway has no profile catalog. No profile, including default, was added."
    )


def discovery_from_catalog(payload: dict) -> dict:
    """Accept only profiles the gateway actually listed."""
    if not isinstance(payload, dict):
        return failed("The profile catalog was not JSON.")
    status = payload.get("status")
    if status == "unsupported":
        error = payload.get("error")
        return unsupported(error if isinstance(error, str) and error else "Profile discovery is unsupported.")
    rows = payload.get("profiles")
    if status != "fresh" or not isinstance(rows, list):
        error = payload.get("error")
        return failed(error if isinstance(error, str) and error else "The profile catalog was not a profile list.")
    profiles = []
    seen: set[str] = set()
    for row in rows:
        parsed = _one(row)
        if parsed is None or parsed["id"] in seen:
            continue
        seen.add(parsed["id"])
        profiles.append(parsed)
    source = payload.get("source") if isinstance(payload.get("source"), str) else ""
    return {
        "status": "fresh",
        "error": "",
        "profiles": profiles,
        "source": source,
        "fingerprint": payload.get("fingerprint") if isinstance(payload.get("fingerprint"), str) else "",
    }


def unsupported(error: str) -> dict:
    return {"status": "unsupported", "error": error, "profiles": [], "source": "none", "fingerprint": ""}


def failed(error: str) -> dict:
    return {"status": "failed", "error": error, "profiles": [], "source": "gateway", "fingerprint": ""}


def _one(row: object) -> dict | None:
    if not isinstance(row, dict):
        return None
    identifier = row.get("backend_profile_id") or row.get("name") or row.get("id") or ""
    if not isinstance(identifier, str):
        return None
    identifier = identifier.strip().strip("/")
    if not identifier or "/" in identifier or identifier in {".", ".."}:
        return None
    display = row.get("display_name") if isinstance(row.get("display_name"), str) else identifier
    route = row.get("configured_route") if isinstance(row.get("configured_route"), str) else ""
    if route and route != f"/p/{identifier}":
        route = ""
    return {
        "id": identifier,
        "display_name": display.strip()[:120] or identifier,
        "source": "gateway",
        "configured_route": route,
    }
