# Ella account diagnostics v1

This contract answers one support question: **where did the latest account-bound
device attempt stop?** It does not become a second source of truth for account,
BLE, capture, conversation, memory, or presentation state.

## Authority boundary

- Firebase bearer authentication supplies the only app account subject.
- The server resolves the current internal account/profile owner and current
  runtime binding revision. The client never supplies those database IDs.
- The server reads the current consent profile binding and consent receipt, then
  recomputes `account_binding_fingerprint` as SHA-256 over canonical JSON:
  `['wal-owner-authority-v1', uid, profile_binding_id, binding_revision,
  consent_receipt_id]`.
- A stale or cross-account fingerprint is rejected before an event is written.
  Ingest then acquires the canonical account authority transaction lock and
  re-reads the account owner, active binding revision, and synchronized consent
  receipt. Revocation or binding drift while a request is in flight therefore
  fails before the immutable insert.
- Authenticated projection and support-grant reads acquire that same authority
  lock and revalidate the binding and consent inside the read transaction, so a
  prior profile cannot be projected after an account switch or revocation.
- `diagnostic_session_id`, `capture_attempt_id`, `capture_attempt_ordinal`,
  `authority_generation`, and all diagnostic receipts are correlation evidence
  only. They never grant capture, finalization, repair, or data-read authority.

The app contract is frozen in
`ella/contracts/diagnostic-event-v1.schema.json`; the failure registry is frozen
in `ella/contracts/diagnostic-failure-registry-v1.json`. Release receipts publish
the SHA-256 digest of each exact file; a client must bind to those immutable
bytes rather than copy an informal list from this document.

| Frozen artifact | SHA-256 |
| --- | --- |
| `diagnostic-event-v1.schema.json` | `c4e0fd2a100243e5da9d6780435937713201cf2dc0a85b2f4418cbe398ad6175` |
| `diagnostic-failure-registry-v1.json` | `f4e55aef73c18acc9e2dbfd4d05258ced28a4a54f003810d3936adc4f3dd9a21` |

## Capture propagation contract

P0-A capture clients should carry these values across the socket and finalization
request for the same attempt:

| Header | Meaning |
| --- | --- |
| `X-Ella-Diagnostic-Session` | One user-visible troubleshooting session |
| `X-Ella-Capture-Attempt` | A fresh identifier for every retry |
| `X-Ella-Capture-Attempt-Ordinal` | Restart-safe, zero-based attempt order within the session |
| `X-Ella-Account-Binding` | Current 64-character authority fingerprint |
| `X-Ella-Authority-Generation` | Local process lease fence, evidence only |

The five headers are optional for legacy clients, but all five are required when
any one is present. The socket validates them only after Firebase authentication:
it resolves the current PostgreSQL account/profile owner, recomputes the binding
fingerprint from current consent, and acquires the canonical authority lock before
accepting the correlation. Invalid, stale, cross-account, or unavailable authority
fails closed as diagnostics: the server emits a content-free rejection receipt and
drops the correlation, while the independently authorized capture continues. A
diagnostic database outage or malformed header can never deny capture. The
client-supplied generation stays evidence; it never overrides the server capture
generation or lease.

An accepted correlation is copied as one nested, versioned map onto both the
Firestore capture-authority document and its conversation. Ready and drained
receipts echo only the opaque session/attempt IDs and `evidence_only: true`; they
never echo the fingerprint. The drain body and finalization request repeat the
same correlation, but mismatched or missing diagnostic fields are reported and
dropped independently of the exact capture tuple. They cannot make an otherwise
authorized drain or finalization succeed or fail.

Capture authorization is decided solely by the existing exact Firebase
subject, conversation/generation/owner tuple, and lease state. Closing before the
ready receipt does not clear or transfer the lease. Diagnostics do not authorize
or veto capture, finalization, publication, repair, or reads.

Finalization receipts echo `diagnostic_session_id` and `capture_attempt_id`, plus
the existing opaque conversation/resource identifier and `evidence_only: true`.
An echo
does not prove that publication or presentation succeeded; those are separate
events. If the finalization request supplies malformed or mismatched correlation,
the product finalization continues under its existing authority and the response
instead carries a content-free `diagnostic_correlation_status`; rejected IDs are
not echoed. A retry always creates a new `capture_attempt_id`.

## API

All responses use `Cache-Control: no-store` and
`X-Ella-Diagnostic-Authority: evidence-only`.

- `POST /v1/ella/diagnostics/events` accepts 1–100 strict events, at most 4 KiB
  each and 600 events per account per hour. Unknown fields, unregistered event
  names, raw URLs, email-like values, non-allowlisted counters, and
  failure-taxonomy drift are rejected.
  Retries count as duplicates only when both immutable identities and the full
  payload match, including exact repeats inside one fresh batch. Reuse of an
  event ID or attempt sequence for different evidence returns
  `diagnostic_event_conflict` and rolls back the whole batch.
- `GET /v1/ella/diagnostics/projection/{diagnostic_session_id}` builds a
  disposable projection for the authenticated account. It selects the latest
  attempt by `capture_attempt_ordinal`, with client timestamps only as
  deterministic within-ordinal tie breakers, and loads at most 1,000 events for
  that attempt. Oversized evidence fails closed with
  `diagnostic_projection_evidence_limit`. Missing evidence remains `unknown`; it
  is never presented as a negative fact.
- `POST /v1/ella/diagnostics/support-grants` issues a random, short-lived,
  session-bound, single-use support code. Only its HMAC is stored.
- `DELETE /v1/ella/diagnostics/support-grants/{grant_id}` revokes an unused code.
- `POST /v1/ella/operator/diagnostics/support-code/exchange` requires the
  dedicated diagnostic operator bearer and `X-Ella-Operator-Id`. It atomically
  consumes the code, records case/stable-reason/operator/event count, and returns only
  the content-free projection.

Support operator auth uses `ELLA_DIAGNOSTICS_OPERATOR_TOKEN`; support-code HMACs
use a separate `ELLA_DIAGNOSTICS_SUPPORT_HMAC_KEY` of at least 32 characters.
Neither credential is accepted by app routes, and legacy `ADMIN_KEY` is not
accepted anywhere in this contract.

## Data handling

The event table is update-immutable. Retention deletion and account-deletion
cascade are the only deletion paths. Events expire after 30 days. A bounded
retention worker runs immediately when an Ella API process starts and every six
hours while it remains live; concurrent replicas use `FOR UPDATE SKIP LOCKED`
against the `expires_at` index. A pass that exhausts its bounded batch allowance
retries after 30 seconds instead of sleeping for six hours. The allowlist
contains lifecycle identifiers, revisions, firmware/codec labels, stable failure
codes, and bounded counters; it has no transcript, audio, memory text, contact,
location, URL, token, or raw hardware identifier field.

`AccountStateProjectionV1` is rebuilt from the latest attempt. It reports the
first non-succeeded layer, completeness, staleness, and a stable failure code.
It does not trigger reconnects, retries, finalization, publication, cache repair,
or any other mutation.

Every event carries the same `capture_attempt_ordinal` for its attempt. The
first attempt in a diagnostic session is `0`, each retry increments it, and the
client persists the next ordinal if the process restarts while the same session
continues. Within one session, an attempt ID and ordinal form a one-to-one
mapping; conflicting reuse returns `diagnostic_event_conflict`. This explicit
order is authoritative because process monotonic clocks reset after reboot,
wall clocks can move, and upload order can be delayed.

## Rollout gates

1. On the production capture lineage, apply migrations `018_create_account_diagnostics.sql`,
   `019_add_diagnostic_attempt_ordinal.sql`, and
   `020_backfill_diagnostic_attempt_ordinals.sql` in order. Migration 020
   deterministically ranks any retained migration-018 attempts, mirrors the
   ordinal into their JSON evidence, and fails closed if the result is not a
   one-to-one attempt/ordinal mapping.
2. Configure the two diagnostic-only secrets.
3. Replay all checked-in fixtures under `tests/fixtures/diagnostics/`, including
   Build 849 success/BLE timeout, authority-claimed then close-before-ready with
   a competing lease collision, and aggregate-ready artwork whose visible hero
   object remains unservable.
4. Verify socket, drain, finalization, and publication correlation without
   changing capture authority. The collision fixture must preserve the first
   attempt's lease; the artwork fixture must report `artwork_hero_unservable`
   rather than treating aggregate readiness as presentation success.
5. Validate real-device traces before enabling any automated reconciliation.

Mutating self-healing remains out of scope until idempotency, rollback,
concurrency, telemetry, and account-switch acceptance gates are separately met.
