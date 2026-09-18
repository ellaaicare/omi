# Self-hosted iMessage operations contract

This document is the source-side handoff for the isolated Ella Photon bridge.
Deployment authority and the one-owner canary are tracked only in
`ellaaicare/ella-ai#1259`; merging this file alone does not authorize either.
Atlas owns launch and service templates; the backend owns the executable, HTTP,
and authority contracts described here.

## Boundaries

- This lane uses the owner's existing `hermes-chat` runtime. It does not create
  another agent or fall back to a retained or Plato workspace.
- Migrations `020_create_imessage_enrollment_authority.sql` and
  `021_create_imessage_runtime_outbox.sql` are additive and do not alter
  migration 009 or any `ella_photon_*` Cloud-canary table.
- App calls use an exact Firebase bearer. The app cannot select a UID, account,
  profile, project, runtime, endpoint, credential, or provider route.
- The bridge uses a dedicated transport credential. That credential cannot
  assert an owner; inbound identity is derived from the unique line/contact
  binding after handset proof.
- Phase 1 is one-to-one text DM only. Groups, attachments, caregiver delivery,
  and unknown-sender model execution remain denied.

## Source contracts

- OpenAPI: `backend/ella/docs/imessage-enrollment.openapi.yaml`
- Internal transport OpenAPI:
  `backend/ella/docs/imessage-runtime-internal.openapi.yaml`
- Migrations: `backend/migrations/020_create_imessage_enrollment_authority.sql`
  and `backend/migrations/021_create_imessage_runtime_outbox.sql`
- App router: `backend/ella/routers/imessage_enrollment.py`
- Internal runtime router: `backend/ella/routers/imessage_runtime.py`
- Service: `backend/ella/services/imessage_enrollment.py`
- Runtime service: `backend/ella/services/imessage_runtime.py`
- Repository: `backend/database/imessage_enrollment.py`
- Runtime repository: `backend/database/imessage_runtime.py`
- Bridge executable: `backend/sidecars/imessage_photon_bridge/main.py`
- Crash-safe bridge core: `backend/sidecars/imessage_photon_bridge/bridge.py`
- Bridge lifecycle tests: `backend/tests/unit/test_imessage_photon_bridge.py`

The app flow is:

1. Read `GET /v1/ella/imessage/consent/policy` before sharing data.
2. Submit the exact policy and decision to `POST /v1/ella/imessage/consent`.
3. Start enrollment with the returned grant receipt at
   `POST /v1/ella/imessage/enrollment/start`.
4. Show the returned one-time code and assigned destination to the owner.
5. Poll `GET /v1/ella/imessage/enrollment` until it reports `ready`.
6. Revoke through `POST /v1/ella/imessage/enrollment/revoke` using the current
   generation. Revocation is not reported complete unless the authenticated
   registrar proves the exact local registration/inbound/delivery artifacts
   absent; its response explicitly reports that the upstream provider user is
   retained but unbound.

## Bridge interface

The backend calls one fixed registrar authority:

- `POST {ELLA_IMESSAGE_REGISTRAR_URL}/v1/registrations`
- `Authorization: Bearer` from `ELLA_IMESSAGE_REGISTRAR_TOKEN`
- `Idempotency-Key` is the backend-generated provider request UUID.
- Body contains only `channel=imessage`, `mode=text_dm`, and the handset E.164.
- Response must be bounded JSON with `registration_id` and
  `assigned_destination`.

Provider enrollment alone does not create this local mapping. The
app-authenticated enrollment path must invoke the registrar once, including
when the handset is already present in the Ella Photon project. In that case
the registrar performs list-before-create, persists the existing provider user
and assigned destination, and does not create a duplicate. This bootstrap is
reachability only; consent and proof remain backend authority.

The bridge delivers an inbound proof to:

- `POST /v1/ella/internal/imessage/proof`
- `X-Ella-Imessage-Transport-Token` from
  `ELLA_IMESSAGE_TRANSPORT_TOKEN`
- Body contains assigned destination, handset, one-time code, provider message
  ID, line identity, and contact identity. It never contains a UID.

Registration is reachability, not identity. The backend persists intent before
the registrar call, persists provider acceptance before runtime revalidation,
and marks ambiguous outcomes for manual reconciliation. It never blindly
re-registers an uncertain outcome. Provider registration references and active
line/contact identities are unique. The six-digit proof is stored only as a
short-lived keyed digest; a database read alone is not enough to recover it.

## Runtime and delivery interface

After proof activates a binding, the bridge uses the same dedicated transport
credential on these ownerless internal routes:

- `POST /v1/ella/internal/imessage/heartbeat`
- `POST /v1/ella/internal/imessage/inbound`
- `POST /v1/ella/internal/imessage/delivery/start`
- `POST /v1/ella/internal/imessage/delivery/ack`
- `POST /v1/ella/internal/imessage/delivery/uncertain`
- `POST /v1/ella/internal/imessage/delivery/reconcile`
- `POST /v1/ella/internal/imessage/deregister`

The header is `X-Ella-Imessage-Transport-Token`. Request bodies contain opaque
line, contact, connection, and provider-message identities only. They cannot
select a UID, account, profile, runtime, agent, URL, credential, or provider.
The backend hashes those transport identities, derives the exact active owner
and `hermes-chat` runtime from the verified binding, and revalidates runtime
authority immediately around model execution.

Inbound execution is deliberately split from delivery. The backend creates a
durable receipt before model work and returns only status plus opaque receipt
and delivery IDs. The reply text is released only by `delivery/start`, after
`send_started` is committed. A second start while status is `sending`, a lost
model result after model-start, or a transport-reported ambiguous send becomes
`uncertain` with manual reconciliation. The bridge must never retry any of those
states blindly. Only an identical provider acknowledgement is idempotent.
After a bridge restart, persisted pre-send intents use `delivery/reconcile` with
their original connection identity. That route never releases cached reply text:
it moves an unstarted intent to a terminal quarantine, or a durably started send
to uncertain/manual reconciliation. A live `claimed` or `running` receipt is
polled for a bounded interval with the original inbound timestamp and receipt;
the durable backend claim prevents a second inference.

The backend writes canonical `imessage` user and assistant events under the
owner's stable OMI session. It performs one bounded non-stream request against
the already authorized self-hosted Hermes target. There is no Cloud, retained,
or Plato fallback and no second inference on an empty or malformed response.
Replies longer than the pinned Photon adapter's 8,000-character transport limit
are rejected before the assistant event or delivery intent is committed; the
bridge never relies on the adapter's silent truncation.

## Runnable bridge contract

The reviewed long-lived command is:

```text
@@ELLA_VENV@@/bin/python @@OMI_RELEASE_ROOT@@/backend/sidecars/imessage_photon_bridge/main.py serve
```

`@@OMI_RELEASE_ROOT@@` must be the immutable deployed OMI release containing the
reviewed bridge head. `@@ELLA_VENV@@` and `HERMES_IMPORT_ROOT` must resolve the
dedicated Ella Hermes checkout pinned to
`00cb8895d4bb60aef24b282cde968745b6f3977f` and Spectrum `12.7.0`. The launch
preflight owns those readbacks. The command accepts no UID, profile, workspace,
contact, project secret, or service token argument.

Required non-secret launch configuration:

- `HERMES_HOME=/Users/ellaai/.hermes/profiles/ella-photon-transport`
- `HERMES_IMPORT_ROOT` points to the immutable Ella-only Hermes checkout.
- `ELLA_IMESSAGE_BRIDGE_STATE_DIR` is an absolute owner-only directory under
  the isolated transport home.
- `ELLA_IMESSAGE_REGISTRAR_BIND=127.0.0.1`; wildcard/public binds are refused.
- `ELLA_IMESSAGE_REGISTRAR_PORT` defaults to `8796`.
- `ELLA_IMESSAGE_HEARTBEAT_SECONDS` defaults to 30 seconds.
- `ELLA_IMESSAGE_HEARTBEAT_FRESHNESS_SECONDS` defaults to 90 seconds and must
  remain no greater than the backend's 120-second connection fence.
- `ELLA_IMESSAGE_BACKEND_TIMEOUT_SECONDS` defaults to 90 seconds.

Required protected values are `PHOTON_PROJECT_ID`, `PHOTON_PROJECT_SECRET`,
`ELLA_IMESSAGE_TRANSPORT_TOKEN`, and `ELLA_IMESSAGE_REGISTRAR_TOKEN`. Load them
from the separately reviewed owner-only environment file at
`/Users/ellaai/.hermes/profiles/ella-photon-transport/.env`; never put values in
the plist, argv, Git, logs, or receipts. The two Ella service tokens must be
distinct.

The bridge owns an additional kernel lifetime lock and an SQLite WAL journal
under its state directory. The journal records a provider-attempt marker before
registration, the minimal normalized inbound event before backend inference,
delivery start-intent with the original connection before the backend
delivery-start call, send-start before provider I/O, and the provider message
ID before backend ACK.
It binds the state directory permanently to the configured project and removes
message text after terminal processing. A different project, second bridge
process, insecure state directory, provider stream in `starting`/`recovering`,
stale backend heartbeat, or suspected zombie stream fails closed for message
processing.

Photon's pinned stream truthfully starts in `starting` until the first inbound
yield. During that cold/quiet state the process and authenticated registrar
remain available, `/healthz` returns HTTP 503 with `status=starting`, and no
message work runs. The first provider event is durably journaled before the
bridge establishes provider health and current backend heartbeat; a temporary
backend outage leaves it pending for reconciliation instead of acknowledging
and losing it. Operators must not restart-loop the bridge merely because a
quiet, not-yet-proven stream reports `starting`.

The registrar listener remains loopback-only. If the VPS backend cannot reach
the Mini over loopback, Atlas must place a separately reviewed private HTTPS
proxy/tunnel in front of this listener and pin
`ELLA_IMESSAGE_REGISTRAR_URL` to that private coordinate. Do not publish the
registrar or bind the bridge to a public or wildcard address. The registrar
requires its dedicated bearer and idempotency UUID; `/healthz` is coarse and
contains no project, contact, credential, or runtime detail. A `ready` response
requires both a healthy provider stream and fresh successful backend
heartbeats for every proof-accepted local registration. Backend failure or
expiry returns HTTP 503 rather than provider-only false health.

Before rendering the launch service, install the checked-in bridge-only runtime
dependency into the isolated Ella Hermes venv without modifying the pinned
Hermes source checkout:

```bash
uv pip install --python @@ELLA_VENV@@/bin/python \
  -r @@OMI_RELEASE_ROOT@@/backend/sidecars/imessage_photon_bridge/requirements.txt
```

Record the requirements-file SHA-256 and installed package version in the
deployment manifest. A successful Hermes venv build alone is not sufficient:
the bridge imports this dependency directly and must pass the entrypoint import
check before the launch service is loaded.

The registrar also exposes authenticated
`DELETE /v1/registrations/{provider_request_id}` for exact lifecycle cleanup.
Consent withdrawal and account deletion use that exact ID for every persisted
registration attempt. Account deletion first commits a durable write fence,
revokes the binding, terminally quarantines open receipts, and requires the
registrar's zero-row absence proof before the account authority can be unlinked.
Unrelated registrations are never selected by that cleanup.
It quarantines the backend binding first, then transactionally removes only
that local handset's registration, inbound journal, and delivery artifacts,
and proves all three exact counts are zero while preserving unrelated users.
The pinned Photon management client has no authenticated provider-user delete
operation, so successful local cleanup is intentionally HTTP 202 with
`provider_user_retained_unbound` and `operator_action_required=true`; it must
never be reported as provider deletion. The provider request UUID is used so a
phone number is not placed in the URL, argv, or operator receipt.

Supported operator commands use the same protected environment and immutable
paths:

```text
.../main.py health       # loopback bridge/provider readiness only
.../main.py reconcile    # no new send after an ambiguous send-start
.../main.py deregister   # backend quarantine first, then local mapping disable
```

`reconcile` retries only a backend ACK after a provider message ID was durably
stored. A prepared local start-intent is resumed with its original connection:
if the backend proves the start was new, the one provider send may proceed; if
the prior backend outcome is ambiguous, it becomes `uncertain` with no provider
send. A send-start without a stored provider ID also becomes `uncertain`; it is
never resent. Provider output always uses one exact raw `/send` operation with
`format=text`; URL replies never try a rich-link operation first. Adapter fatal
state makes health unavailable and exits the process with a temporary-failure
code so launchd can create a new connection generation. Normal service stop
does not deregister. Deregistration is an explicit lifecycle operation and must
complete before disabling the runtime flag.

The bridge has no owner-to-agent selector. For the authorized owner canary, the
backend operator must separately read back that the exact active invitation,
entitlement, consent epoch, runtime binding, and `hermes-chat` target resolve to
the preserved `plato-eval` agent. Missing or mismatched authority blocks the
turn; the bridge cannot substitute a profile or workspace.

## Required protected configuration

All settings default to disabled or unavailable:

- `ELLA_IMESSAGE_ENROLLMENT_ENABLED=false`
- `ELLA_IMESSAGE_RUNTIME_ENABLED=false`
- `ELLA_IMESSAGE_REGISTRAR_URL` (fixed HTTPS authority, or exact loopback HTTP)
- `ELLA_IMESSAGE_REGISTRAR_TOKEN` (at least 32 non-whitespace bytes)
- `ELLA_IMESSAGE_TRANSPORT_TOKEN` (at least 32 non-whitespace bytes)
- `ELLA_IMESSAGE_BINDING_HMAC_KEY` (at least 32 bytes)
- `ELLA_IMESSAGE_PROOF_KEY` (at least 32 bytes and distinct from the binding key)
- `ELLA_IMESSAGE_HEALTH_MAX_AGE_SECONDS` (default 300, minimum 30)

Bridge-only settings are listed in the runnable contract above. Backend and
bridge must share the transport and registrar references by protected secret
reference, not by copying values into an operations receipt.

Store values only in the approved root-owned secret mechanism. Never place
values in Git, argv, logs, receipts, issue comments, or this document.

## Inactive-first acceptance

Before any live enablement, an operator must prove all of the following with
synthetic identities and content-free receipts:

1. Migrations 020 and 021 apply in sequence after 019 and the migration-009
   Cloud tables are unchanged.
2. Enrollment flag false returns `rollout_disabled` and performs no provider or
   database write.
3. Missing/malformed Firebase and transport credentials fail before repository
   or provider work.
4. Consent grant, registration, proof, readiness, and revoke are idempotent.
5. Runtime/target owner, role, entitlement, consent policy, generation, line,
   or contact drift fails closed.
6. Two owners cannot activate the same line/contact identity and neither can
   mutate the other's row.
7. A provider timeout after request transmission produces an uncertain state
   and no automatic retry.
8. A durable inbound claim precedes model work, canonical events use channel
   `imessage`, and text is absent until the fenced delivery-start response.
9. Replayed delivery-start, stale post-model leases, and ambiguous provider
   sends are quarantined without automatic model or send retries.
10. The app only advertises iMessage when the authoritative status is `ready`.

Do not activate either flag until the exact bridge/service installation,
transport health reporting, deletion cleanup/absence proof, secret installation,
and rollback rehearsal are reviewed. Source-complete is not running, and running
is not handset-verified.

## Rollback

Source rollback is a normal application commit rollback while both flags remain
false. For an activated bridge, stop new enrollment, complete explicit backend
deregistration while runtime ingress is still enabled, prove no in-flight proof,
model, or delivery work, then stop the bridge and preserve its journal as a
rollback artifact. A `sending` or `uncertain` receipt is a manual reconciliation
blocker, not proof that no message was sent. A deployed schema rollback must not
drop migration-020 or migration-021 tables while any row exists. Never repoint
this lane to the Cloud canary or a retained workspace as a fallback.
