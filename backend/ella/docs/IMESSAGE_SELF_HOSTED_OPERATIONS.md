# Self-hosted iMessage operations contract

This document is the source-side handoff for the isolated Ella Photon bridge.
It does not authorize deployment, registration, live messaging, or flag changes.
Atlas owns launch and service templates; the backend owns the HTTP and authority
contracts described here.

## Boundaries

- This lane uses the owner's existing `hermes-chat` runtime. It does not create
  another agent or fall back to a retained or Plato workspace.
- Migration `020_create_imessage_enrollment_authority.sql` is additive and does
  not alter migration 009 or any `ella_photon_*` Cloud-canary table.
- App calls use an exact Firebase bearer. The app cannot select a UID, account,
  profile, project, runtime, endpoint, credential, or provider route.
- The bridge uses a dedicated transport credential. That credential cannot
  assert an owner; inbound identity is derived from the unique line/contact
  binding after handset proof.
- Phase 1 is one-to-one text DM only. Groups, attachments, caregiver delivery,
  and unknown-sender model execution remain denied.

## Source contracts

- OpenAPI: `backend/ella/docs/imessage-enrollment.openapi.yaml`
- Migration: `backend/migrations/020_create_imessage_enrollment_authority.sql`
- App router: `backend/ella/routers/imessage_enrollment.py`
- Service: `backend/ella/services/imessage_enrollment.py`
- Repository: `backend/database/imessage_enrollment.py`

The app flow is:

1. Read `GET /v1/ella/imessage/consent/policy` before sharing data.
2. Submit the exact policy and decision to `POST /v1/ella/imessage/consent`.
3. Start enrollment with the returned grant receipt at
   `POST /v1/ella/imessage/enrollment/start`.
4. Show the returned one-time code and assigned destination to the owner.
5. Poll `GET /v1/ella/imessage/enrollment` until it reports `ready`.
6. Revoke through `POST /v1/ella/imessage/enrollment/revoke` using the current
   generation.

## Bridge interface

The backend calls one fixed registrar authority:

- `POST {ELLA_IMESSAGE_REGISTRAR_URL}/v1/registrations`
- `Authorization: Bearer` from `ELLA_IMESSAGE_REGISTRAR_TOKEN`
- `Idempotency-Key` is the backend-generated provider request UUID.
- Body contains only `channel=imessage`, `mode=text_dm`, and the handset E.164.
- Response must be bounded JSON with `registration_id` and
  `assigned_destination`.

The bridge delivers an inbound proof to:

- `POST /v1/ella/internal/imessage/proof`
- `X-Ella-Imessage-Transport` from `ELLA_IMESSAGE_TRANSPORT_TOKEN`
- Body contains assigned destination, handset, one-time code, provider message
  ID, line identity, and contact identity. It never contains a UID.

Registration is reachability, not identity. The backend persists intent before
the registrar call, persists provider acceptance before runtime revalidation,
and marks ambiguous outcomes for manual reconciliation. It never blindly
re-registers an uncertain outcome. Provider registration references and active
line/contact identities are unique. The six-digit proof is stored only as a
short-lived keyed digest; a database read alone is not enough to recover it.

## Required protected configuration

All settings default to disabled or unavailable:

- `ELLA_IMESSAGE_ENROLLMENT_ENABLED=false`
- `ELLA_IMESSAGE_REGISTRAR_URL` (fixed HTTPS authority, or exact loopback HTTP)
- `ELLA_IMESSAGE_REGISTRAR_TOKEN` (at least 32 non-whitespace bytes)
- `ELLA_IMESSAGE_TRANSPORT_TOKEN` (at least 32 non-whitespace bytes)
- `ELLA_IMESSAGE_BINDING_HMAC_KEY` (at least 32 bytes)
- `ELLA_IMESSAGE_PROOF_KEY` (at least 32 bytes and distinct from the binding key)
- `ELLA_IMESSAGE_HEALTH_MAX_AGE_SECONDS` (default 300, minimum 30)

Store values only in the approved root-owned secret mechanism. Never place
values in Git, argv, logs, receipts, issue comments, or this document.

## Inactive-first acceptance

Before any live enablement, an operator must prove all of the following with
synthetic identities and content-free receipts:

1. Migration 020 applied after 019 and the migration-009 Cloud tables are
   unchanged.
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
8. The app only advertises iMessage when the authoritative status is `ready`.

Do not activate the flag until the runtime executor/outbox slice, bridge health
reporting, deletion cleanup, and rollback rehearsal are separately reviewed.

## Rollback

Source rollback is a normal application commit rollback while the flag remains
false. A deployed schema rollback must not drop migration-020 tables while any
row exists. Disable enrollment and transport ingress first, quarantine pending
or active bindings, prove no in-flight registration/proof work, then use a
reviewed data-preserving migration. Never repoint this lane to the Cloud canary
or a retained workspace as a fallback.
