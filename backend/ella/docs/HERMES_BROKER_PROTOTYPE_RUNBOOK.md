# Hermes broker ordinary prototype (OMI)

Fail-closed, default-off adapter so **one exact synthetic account/profile** can
use the merged Ella first-party Hermes webhook broker instead of calling
Hermes Cloud `/v1/responses` directly.

Controlling product cut line: Greg PROTOTYPE CUT LINE (allowlisted synthetic
only; no global switch; no Plato/Mini fallback).

## Pins

| Surface | SHA |
|---|---|
| OMI base main | `a9af15c4373cd5456acfac89d3e9f07e8506b580` |
| Ella broker merge (main) | `114c30dec5312360c860e95ff46b18ad37e5f4ed` |

## Behaviour

| User class | Transport |
|---|---|
| Flag off (default) | Existing direct Hermes Cloud `/v1/responses` |
| Flag on, not exact allowlist | Existing direct path unchanged |
| Flag on + exact synthetic account/profile (+ optional binding) | Broker `POST .../admit` then bounded poll for terminal result |

On broker failure/timeout/mismatch: **explicit `ProvisioningError`**, content-free
where required. **No** fallback to direct Hermes, Plato, Mini, or OpenClaw.

## Env (names only)

```text
# Master switch — must be the exact lowercase string "true"
ELLA_HERMES_BROKER_PROTOTYPE_ENABLED=false

# Exact allowlist — canonical owner UUIDs (ella_runtime_bindings.account_user_id /
# profile_user_id = users.id), NOT the auth omi_uid string.
ELLA_HERMES_BROKER_PROTOTYPE_ACCOUNT_ID=
ELLA_HERMES_BROKER_PROTOTYPE_PROFILE_ID=
ELLA_HERMES_BROKER_PROTOTYPE_BINDING_ID=   # optional; when set must match runtime.binding_id

# Private broker HTTPS endpoint (host must match allowlist)
ELLA_HERMES_BROKER_BASE_URL=https://broker.example.internal
ELLA_HERMES_BROKER_ALLOWED_HOST=broker.example.internal

# Service auth — reference only
ELLA_HERMES_BROKER_SERVICE_TOKEN_REF=env:ELLA_HERMES_BROKER_SERVICE_TOKEN
# ELLA_HERMES_BROKER_SERVICE_TOKEN is set in the secret store, never committed

# Bounded wait
ELLA_HERMES_BROKER_POLL_INTERVAL_SECONDS=0.5
ELLA_HERMES_BROKER_POLL_TIMEOUT_SECONDS=45
ELLA_HERMES_BROKER_CALLBACK_DEADLINE_SECONDS=90
```

## Rollback

Set `ELLA_HERMES_BROKER_PROTOTYPE_ENABLED` to any value other than exact `true`
(or unset). All traffic returns to the pre-prototype direct path.

## Companion change required on ella-ai (blocker for live smoke)

Merged broker routes include admit, callback, and workers only — **no owner-pinned
result read/wait API**. Canonical writeback stores a **content-free**
`result_sha256`; the answer lives in `broker_writeback_outbox.result_json`.

Minimal companion (ella-ai):

```http
GET /v1/ella/internal/hermes-webhook-broker/requests/{request_id}
  ?account_id=<uuid-or-opaque>&profile_id=<uuid-or-opaque>
Authorization: Bearer <ELLA_HERMES_BROKER_SERVICE_TOKEN>
```

Requirements:

1. Service auth identical to admit.
2. Re-prove request owner (`account_id`/`profile_id`) under the shared authority
   key before returning anything.
3. Return bounded JSON, for example:

```json
{
  "status": "pending|awaiting_callback|completed|failed|quarantined|...",
  "request_id": "hwb_...",
  "correlation_id": "hwb:...",
  "account_id": "...",
  "profile_id": "...",
  "lane": "chat_turn",
  "outcome": "success|error|null",
  "duplicate": false,
  "result": { "answer": "...", "session_key": "...", "session_id": "...", "canonical_user_event_id": "..." }
}
```

4. Until completed/failed, `result` may be null. Never return another owner's row.
5. Cap body size (≤256KiB) and apply read timeouts.

Until that endpoint exists, the OMI prototype returns
`hermes_broker_prototype_result_endpoint_missing` after a successful admit when
GET yields HTTP 404 — fail closed, no direct-path bypass.

## Out of scope (see ella-ai#1157)

Production concurrency, warm pool, global rollout, multi-tenant scaling, invite
issuance (dashboard + omi#333).

## Code map

| File | Role |
|---|---|
| `ella/services/hermes_broker_prototype.py` | Flag + exact allowlist |
| `ella/services/hermes_broker_client.py` | Admit + poll client |
| `ella/services/hermes_cloud_runtime.py` | Transport selection at provider send |
| `tests/unit/test_hermes_broker_prototype.py` | Focused fake-broker coverage |
