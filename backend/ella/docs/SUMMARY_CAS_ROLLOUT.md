# Ella canonical summary CAS rollout

## Contract

`PATCH /v1/ella/conversation/{conversation_id}/summary?uid={uid}` supports
`ella-canonical-source-v1`. A conditional request must send exactly one of each:

```text
X-Ella-CAS-Contract: ella-canonical-source-v1
If-Match: "ella-canonical-source-v1:<64 lowercase hex SHA-256>"
X-Ella-Operation-Token: <16-256 characters from A-Z a-z 0-9 _ . : ->
X-Ella-Source-Version: <1-256 printable ASCII characters>
```

The source digest covers sorted compact UTF-8 JSON over exactly `type`,
`conversation_id`, `uid`, `started_at`, `finished_at`, `title`, `overview`,
`emoji`, `category`, `segment_count`, and `transcript`. The source version must
equal the materialized source's `finished_at`, falling back to `started_at`.
The comparison and Firestore structured-summary/version update occur in one
owner-bound transaction.

A stale digest or source version returns `412`. An incomplete, malformed, or
unknown precondition returns `428`. Duplicate raw occurrences of any contract,
match, operation-token, or source-version header return `400`, including
identical duplicates. None of these responses writes the conversation.

The Firestore transaction atomically commits the structured summary, summary
version, caller operation token, request-payload/source digests, source version,
exact post-image and active-version fences, pending canonical repair state, and
any correction audit document. This is the source-store atomic boundary, not a
transaction across Firestore and the canonical Postgres ledger.

The CAS contract always requires canonical publication. The request body's
legacy `require_canonical` value cannot weaken it. Locally knowable missing
canonical configuration fails with `503` before the Firestore transaction.
After the transaction commits, the exact fenced post-image is published
idempotently and the Firestore receipt is finalized.

Confirmed success returns HTTP `200`, this header:

```text
X-Ella-CAS-Applied: ella-canonical-source-v1
```

and exactly this body shape:

```json
{
  "status": "completed",
  "operation_receipt": {
    "token": "<exact request token>",
    "status": "completed",
    "payload_sha256": "<SHA-256 of the exact request body bytes>",
    "source_sha256": "<If-Match source SHA-256>",
    "source_version": "<exact request source version>"
  }
}
```

If canonical publication or receipt finalization fails after the source commit,
the endpoint returns HTTP `202`, no `X-Ella-CAS-Applied` header,
`X-Ella-CAS-Reconciliation: pending`, and the same exact body shape with both
statuses set to `pending_reconciliation`. The committed conversation remains
`writeback_pending_canonical` with its durable operation receipt.

An exact retry proves that the current source post-image and active summary
version still match the durable receipt before publishing or acknowledging it.
It then resumes the same operation without creating another summary version. A
different CAS or legacy writer receives `409` while reconciliation is pending.
A later legacy write after completion transactionally clears the superseded
receipt; a later CAS replaces it only after passing its new source comparison.
Finalize compares the current document against the receipt's post-image and
active-version fences, not receipt fields against themselves.

If the Firestore client loses the transaction response and an exact receipt
cannot be read back, the endpoint returns an explicit retryable `503`
outcome-unknown response. It does not claim zero writes. No required-canonical
source commit is untracked: the pending state and repair receipt commit in the
same transaction as the winning summary.

## Compatibility gate and capability signal

`ELLA_SUMMARY_CAS_MODE` accepts:

- `optional` (the temporary default): fully headerless legacy requests retain
  authenticated legacy behavior. If any CAS or operation header is present,
  all four must be exact; there is no CAS-shaped unconditional fallback.
- `required`: every request must provide the exact four-header contract.

An invalid configured value fails closed as `required`. The credential-free
`GET /v1/ella/conversation/summary/capabilities` response reports the contract,
enforcement mode, conditional-write availability, and whether headerless legacy
writes remain enabled. It contains no owner, conversation, source, or credential
data.

## Caller audit (invariant verification, 2026-08-15)

The rollout audit is defined by behavior, not a transient caller commit. Before
each stage, inspect the current `ellaaicare/ella-ai` caller and its tests and
verify all of these invariants:

- it hashes the exact sorted compact UTF-8 request bytes and validates the same
  `payload_sha256` in the response;
- it sends one occurrence each of the contract, `If-Match`, operation-token,
  and source-version headers;
- it accepts only the exact five-field `operation_receipt` bound to the request;
- it treats only `200 completed` plus `X-Ella-CAS-Applied` as applied;
- it treats `202 pending_reconciliation` without the applied header as durable
  but not applied, and retries the same token/body/source/version; and
- it maps `409`/`412` to conflict without regenerating or reusing a stale
  candidate.

Current main still has headerless Hermes/OpenClaw write paths. The migration
caller must satisfy the invariants above and fail before network egress unless
`OMI_SUMMARY_CAS_CONTRACT=ella-canonical-source-v1`. Immediate required-mode
enforcement would therefore break live legacy paths; optional mode exists only
for coordinated migration.

## Rollout order

1. Merge and deploy the backend in `optional` mode. Verify the public capability
   response reports `optional`, then verify an authenticated conditional canary
   receives an exact completed receipt and applied header.
2. Merge and install the reviewed Ella/Hermes caller changes. Keep
   `OMI_SUMMARY_CAS_CONTRACT` unset while deployment identity and source parity
   are checked.
3. Remove, update, or disable every headerless OpenClaw/Hermes caller. Re-run the
   repository caller search and prove no active headerless path remains.
4. Switch the backend to `required` first. Verify capability reports `required`,
   a headerless authenticated probe returns `428`, a stale conditional probe
   returns `412`, and both leave the conversation unchanged.
5. Only then set `OMI_SUMMARY_CAS_CONTRACT=ella-canonical-source-v1`. Run one
   exact owner/conversation canary and verify the exact content-free receipt and
   applied header before expanding traffic.
6. Monitor bounded `409`, `412`, and `428` status/stage counts. Do not log source
   JSON, transcripts, payloads, authorization values, or CAS headers.

## Rollback

1. Unset `OMI_SUMMARY_CAS_CONTRACT` first so the migration caller fails closed
   before sending a write.
2. Diagnose source parity or transaction conflicts without bypassing CAS. A
   `412` requires a fresh owner-bound materialization and candidate.
3. If an audited legacy caller must be restored, switch the backend to
   `optional` and verify the capability receipt before restoring that caller.
   CAS-shaped requests remain conditional.
4. Revert the backend deployment only after optional mode is active and health
   is verified. No rollback step may turn a request carrying CAS headers into an
   unconditional write.
