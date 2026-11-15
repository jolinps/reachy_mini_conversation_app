# Gateway binding rollout

This patch is not deployed. It is the review package for the gateway that the conversation-app text probe will call after a separate, explicit deployment.

The running voice baseline is not part of this change. Do not restart it to deploy the gateway.

## What the patch changes

Compared with commit `5ddc96e`, the gateway process:

- Checks `expected_agent_id` and `expected_epoch` in one transaction before Hermes starts. A stale pair returns 409 and does not open another agent.
- Freezes that agent, epoch, a server request id (`gwr_…`), and an opaque backend instance id (`bki_…`) for the turn. Selection changes and cancellation discard a late reply. The cancelled event still names the destination that was frozen, not the newly selected agent.
- Skips TTS when `text_only` is true. `/health` advertises `chat_binding: 1` and `text_only: true` because those paths are implemented and covered by `tests/test_gateway_adapter.py`.
- Mints `backend_instance` when an agent is registered. A `bki_…` value identifies that gateway registration row: the configured listener, routing mode, and server-side secret. It is not a container name, a host name, `instance_id`, or `server_name`. It does not by itself prove which Hermes process answered. That proof is a host inspection of the process and profile, compared with the listener and routing path that the registration resolved.
- Reports profiles only from the Hermes API list, `hermes_cli.profiles`, or an explicit `HERMES_PROFILE` together with `HERMES_BASE_URL`. An empty catalog stays `unsupported` and does not block a chat to an agent that was registered and granted separately. A profile whose inspected name is `default` can be recorded. The path `/p/default` is never invented from that name.
- Stores a host inspection separately from the registry label. `configured_profile_is_not_proof` stays true. A model reply is not inspection.

`/health` and `hermes_ok` remain a process check. They are not routing evidence.

## Files

- `src/device_gateway/app.py`
- `src/device_gateway/engine.py`
- `src/device_gateway/store.py`
- `src/reachy_hermes/protocol.py`
- `src/reachy_hermes/adapter.py`
- `src/reachy_hermes/hermes_api.py`
- `src/reachy_hermes/profile_catalog.py`
- `tests/test_gateway_adapter.py`
- `deploy/authorize_device.py`

`deploy/gateway-binding.patch` is a diff against local commit `5ddc96e`. The deployed gateway revision was not read, so this patch must not be applied until the files on the server are compared with that commit. If the server tree differs, regenerate the diff against the deployed tree. Do not apply the rest of the dirty working tree.

## Configuration

No new public port. The existing gateway origin stays `https://aiagent.jolinps.com`.

These values stay on the server and are not written into the patch:

- gateway admin token
- Hermes API key
- Hermes private listener URL
- device private key

The SQLite file gains schema version 4 and a nullable-default `backend_instance` column. Existing device rows, public keys, grants, and agent secrets stay in that file. On startup the new process copies a populated older database to `gateway.db.bak-before-4` once. Take a second copy before the restart. That copy is a migration backup, not the normal rollback.

## Deployment, when it is approved

1. On the gateway host, copy the live database to a timestamped file before replacing the process.
2. Apply only this patch and restart the gateway process.
3. Confirm `GET /health` includes `"chat_binding": 1` and `"text_only": true`.
4. Do not register a new device and do not call pairing.

## Rollback

The normal rollback keeps the live database, so device identities, public keys, registry rows, and grants survive.

1. Stop the new gateway process.
2. Restore the previous program from the backup of the deployed tree. Do not replace `gateway.db` with the pre-deploy copy.
3. Start the previous process against the same `GATEWAY_DB`. The added column has a default, so the previous program can keep reading the existing device and grant rows.
4. Confirm the public health response no longer advertises `chat_binding` and `text_only`. The conversation-app probe will refuse to send a chat until those fields return.

Restore the pre-deploy database copy only if the migration itself damages the file. That restore discards registry changes made after the copy. It is not the way to undo a bad program while keeping identities.

## Device authorization

Device `rch_A2VSW1KY88` is already registered. Authentication succeeded and returned no enabled agent. Do not create another identity and do not call `/v1/pairing/register` or `/v1/admin/pair`.

The grant cannot be filled in yet. The deployed gateway revision, Hermes listener, routing path, profile, and instance were not readable from this machine. There is no active SSH session to the VPS. The earlier login was denied and was not retried. A health response, a container name, and a model self-description are not substitutes. Local notes that mention a listener port are not a new inspection.

`OBSERVED_PROFILE=default` is accepted when that name was read from the host. `HERMES_ROUTE_EVIDENCE` must be the URL that was observed. For `private_endpoint`, that URL is the listener with no extra path. For `profile_prefix`, it must already be the observed profile path. The command does not create `/p/default` from the name `default`.

After those values are known, run the dry run, then apply:

```bash
GATEWAY_ADMIN_TOKEN=… \
HERMES_AGENT_ID=… \
HERMES_ROUTING=private_endpoint \
HERMES_PRIVATE_ENDPOINT=… \
HERMES_ROUTE_EVIDENCE=… \
HERMES_API_KEY=… \
OBSERVED_PROFILE=… \
OBSERVED_INSTANCE=… \
INSPECTION_REF=… \
python deploy/authorize_device.py

GATEWAY_ADMIN_TOKEN=… \
HERMES_AGENT_ID=… \
HERMES_ROUTING=private_endpoint \
HERMES_PRIVATE_ENDPOINT=… \
HERMES_ROUTE_EVIDENCE=… \
HERMES_API_KEY=… \
OBSERVED_PROFILE=… \
OBSERVED_INSTANCE=… \
INSPECTION_REF=… \
python deploy/authorize_device.py --apply
```

The dry run makes no network call. `--apply` checks that the device is already paired, registers one agent, grants it to `rch_A2VSW1KY88`, and records the inspection. It prints the agent id, the observed route path, and states that `bki_…` is the gateway registration id minted by the server. It does not print the API key, the listener address, or the admin token.

## Acceptance after that authorization

From the upgrade worktree, one text probe:

```bash
PYTHONPATH=src python -m reachy_mini_conversation_app.gateway.probe \
  --identity /path/to/the-existing-identity.json \
  --text "Reply with the single word pong."
```

A passing turn includes `gateway_request_id`, `backend_instance`, the selected agent id, and the assignment epoch, with `hermes_instance_proven` still false until the recorded inspection is compared with the host. The answer text is not that proof. Profile synchronization and voice integration stay out of this step.
