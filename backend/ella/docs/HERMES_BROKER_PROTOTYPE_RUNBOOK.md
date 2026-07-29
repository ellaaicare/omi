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
| Flag on + exact synthetic account/profile (+ optional binding) | Broker stock-canary admission then bounded stock-result poll |

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

# HTTPS:443 normally. The only HTTP exception is the exact host-local mapping:
ELLA_HERMES_BROKER_BASE_URL=http://127.0.0.1:18097
ELLA_HERMES_BROKER_ALLOWED_HOST=127.0.0.1

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

## Stock contract

```http
POST /v1/ella/internal/hermes-webhook-broker/stock-canary/admit
GET /v1/ella/internal/hermes-webhook-broker/stock-canary/requests/{request_id}
  ?account_id=<uuid-or-opaque>&profile_id=<uuid-or-opaque>
Authorization: Bearer <ELLA_HERMES_BROKER_SERVICE_TOKEN>
```

Admission always supplies server-owned `delivery_platform=ella_callback_stock`,
`callback_source=hermes_stock_0_19_quiet_window`, and
`webhook_route=ella-stock-synthetic`. Results must be the owner-, request-,
correlation-, and lane-pinned `stock_best_effort_v1` projection with
`terminal_proof=false`. OMI accepts success only at `writeback_completed`.
HTTP 404, generic broker projections, terminal-proof claims, or unknown states
fail closed with no direct/Plato fallback.

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
