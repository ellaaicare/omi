# Ella Auth Cutover Runbook

This runbook implements the backend portions of ella-ai issue 1185 plan v4 plus
the v5 emergency-route correction. Source changes do not authorize a deploy or
an edge change. Deployment requires a fresh accepting review of the exact head.

## Authority And Edge Matrix

The executable route inventory is
`backend/tests/unit/test_ella_all_router_authority_manifest.py`. It declares
every route in all current Ella router modules, including conditional Guardian
and voice routes, observer, plato_mcp, and escalations.

- Firebase exact owner: chat, onboarding, settings, resolve, invitations,
  canonical owner events/timeline, emergency-contact and caregiver CRUD,
  Guardian owner controls, and `POST /v1/ella/emergency`.
- Exact bound service subject: callback notification/summary/conversation data,
  dashboard-token minting, ledger service events/timeline, Guardian service
  routes, escalation evaluation, observer, enrichment, Photon, and internal
  TTS. A service credential without `X-Ella-Subject-Uid` is denied.
- Dual exact authority: canonical events/timeline, escalation-policy reads,
  Guardian queue/trace reads, and TTS only where the manifest says so.
- `/hermes/*` remains permanently denied at the public edge.
- `/v1/ella/debug/*` remains permanently edge-denied and internal-only.

No step in this runbook changes Caddy. A separate authorized operation owns
each deny or lift.

## Containment Precondition

Before any source deploy, obtain explicit owner authorization to edge-deny all
currently reachable exposed families:

- `/v1/ella/emergency`
- `/v1/ella/daily-summary`
- `/v1/ella/conversation/*/data`
- `/v1/ella/observer/*`
- singular `POST/PUT/DELETE /v1/ella/emergency-contact*`

If authorization is absent, stop before deploy. Record the owner-accepted
temporary exposure and an explicit expiration. Never claim to lift a route that
was not first denied and read back as denied.

## Step 0

Stage these values and update their exact consumers atomically without printing
values:

- `ELLA_CALLBACK_SERVICE_KEY`
- `ELLA_CAREGIVER_SERVICE_KEY`
- `ELLA_INTERNAL_VOICE_TTS_TOKEN`
- `HERMES_MODEL`

Every n8n or Guardian service call must send its distinct credential and one
`X-Ella-Subject-Uid` that matches the request body/query/path subject. Prove the
credential is present and the header shape is correct before deploy. Preserve
`HERMES_API_SERVER_KEY`; do not add a shadowing `HERMES_GATEWAY_TOKEN`
environment value.

Run the bounded ADMIN branch observation before allowlisting/removal. Its only
permitted fields are invocation count and caller class. Then set an explicit
`ELLA_ADMIN_SUBJECT_ALLOWLIST` or keep it empty and prove controlled ADMIN_KEY
impersonation is denied. Any production marker must disable
`LOCAL_DEVELOPMENT` subject fallback.

`ELLA_PLATO_UID` is not Step 0. Set it only post-deploy and pre-binding, verify
process readback without exposing the value, then create and read back the owner
binding.

## Backend-First Hard Cutover

There is no unauthenticated dual-accept window.

1. Deploy reviewed backend authority checks while affected public paths remain
   denied.
2. Atomically update n8n/Guardian consumers to the authenticated credential plus
   exact-subject contract and run safe synthetic probes.
3. Record the exact first released iOS build containing the accepted successor
   client, then set `ELLA_MIN_SUPPORTED_CLIENT_BUILD` to that exact value.
4. At the recorded cutover instant, deny the public caller-UID webhook. Older or
   unattributed Firebase clients receive HTTP 426 `update_required`.
5. Rollback may restore an authenticated service or keep clients unavailable or
   update-required. It must never restore caller-selected unauthenticated UID
   authority.

## Staged Edge Procedure

Before every separately authorized stage:

1. Record the current Caddy SHA-256 and create a hash-verified backup.
2. Validate configuration.
3. Record exact matchers being removed and test the command that re-denies only
   that stage.
4. Capture a 30-minute pre-stage request and attributable-5xx baseline for the
   affected routes and controls.
5. Gracefully reload, read back exact paths, and run safe synthetic probes.

Observe each stage for 30 minutes. Immediately re-deny and stop on any
unauthorized success, cross-owner success, downstream work after a rejected
request, Guardian functional failure, or voice regression. Also roll back on
three attributable 5xx responses within five minutes, or greater than one
percent attributable 5xx over at least 100 affected-route requests. With fewer
than 100 requests, the complete 30-minute synthetic/functional matrix is
mandatory. Backend rollback never removes an edge deny.

Lift only in this order:

1. conversation-data and dashboard-token reads/mints
2. timeline and emergency-contact owner reads
3. events, notification, observer, and emergency bounded writes
4. TTS and daily-summary spend/scheduled work
5. authenticated backend chat
6. onboarding after the accepted successor client is released

Direct Hermes and debug routes never lift under this issue.

## Functional Receipts

Guardian/TTS acceptance requires valid audio from a redacted, non-family
synthetic request using the internal token plus bound subject. Missing, wrong,
rotated/expired, and cross-owner authority must fail before provider work. Any
failure immediately re-denies TTS. Health 200 is only an additional control.

The same-window memory receipt must record all six outcomes:

1. authenticated listen admission
2. conversation processing
3. same-user memory readback
4. second-user memory and conversation denial
5. containment proof for the same observation window
6. controlled ADMIN_KEY impersonation denial

Receipts contain statuses, booleans, counts, timestamps, random correlation
identifiers, and exact reviewed/deployed heads only. Do not record credentials,
headers, UIDs, user content, transcripts, medical data, or provider payloads.
