# Memory Reinterpretation Outbox

This backend path analyzes a completed, memory-scoped V2V session after a
bounded inactivity debounce. It does not run during the realtime voice turn and
does not add a second transcript store.

## Source Contract

The voice proxy writes connection-qualified turn rows to `canonical_events`,
then posts logical-JTI-qualified completion to:

```text
POST /v1/ella/sessions/{signed_jti}/complete
```

An outbox job is eligible only when the signed completion contains all of:

```json
{
  "uid": "exact Firebase UID",
  "source_ref": {
    "scope_kind": "memory",
    "conversation_id": "canonical OMI conversation ID",
    "active_summary_version_id": "signed starting version",
    "can_reinterpret": true
  }
}
```

The idempotency key is the exact tuple `(uid, signed_jti, conversation_id,
starting_summary_version_id)`. UID comparisons are case-sensitive. A later
completion for the same tuple does not create another job. An unchanged
transcript extends `not_before` while the job is pending or retrying. A changed
transcript increments a durable revision, invalidates its plan/progress and
lease, and receives fresh Hermes analysis. Terminal jobs and their processed
transcript references are immutable.

Rejected/noise sessions never post completion. Read-only, locked, general, or
otherwise `can_reinterpret=false` completions do not enqueue.

## Stored Data

Postgres stores:

- canonical event identifiers and source identities;
- a deterministic hash of the ordered canonical transcript;
- a monotonically increasing transcript revision used by every lease, plan,
  progress, and terminal transition fence;
- typed Hermes proposal plan and per-proposal progress;
- proposal, correction, and receipt identifiers;
- lease, retry, attempt, terminal status, and bounded error metadata.

It does not store another transcript body. The worker reconstructs turns from
the exact UID and logical session in `canonical_events` and validates every
row's conversation/version scope plus the stored references and hash before a
model call.

## Decision And Write Policy

Hermes returns one typed result:

```json
{"outcome": "no_change", "proposals": []}
```

or an ordered `proposals` array. Hermes is read/reason/propose only.

An item auto-applies only when all of these are true:

- `kind=factual_correction`;
- `certainty=confirmed`;
- a complete corrected summary is present;
- every evidence event ID exists in the exact canonical session;
- `evidence_quote` is an exact contiguous quote from a cited user event.

Everything else becomes an idempotent pending `summary_correction` proposal.
OMI is the only writer. Auto-apply uses the existing summary CAS, canonical
writeback, correction receipt, and Undo path. Deterministic revision-qualified
correction IDs and persisted proposal plans make crash-after-apply replay
idempotent. Workers renew their lease while model and write calls are in
progress and verify the current lease plus transcript revision immediately
before each external write.

## Status API

The authenticated app may fetch identifier/status data only:

```text
GET /v1/ella/conversations/{conversation_id}/reinterpretations/latest
GET /v1/ella/conversations/{conversation_id}/reinterpretations/{job_id}
```

Missing and non-owned conversations return the same `404` shape. Responses
contain job/session/conversation/version identifiers, state, proposal IDs,
correction IDs, receipt identifiers, attempts, timestamps, and error code.
They do not contain transcript, summary, proposal-plan, or canonical event
content. No memory content is emitted through WebSocket status events.

Operator endpoints require `X-Ella-Reinterpretation-Token`:

```text
POST /v1/ella/internal/memory-reinterpretations/run-once
GET  /v1/ella/internal/memory-reinterpretations/metrics
```

## Rollout

1. Apply `backend/migrations/007_create_memory_reinterpretation_outbox.sql`.
2. Confirm the voice proxy and backend share `ELLA_EVENT_LEDGER_TOKEN`; eligible
   completion rejects missing/invalid bearer auth when enqueue is enabled.
3. Configure a secret `ELLA_MEMORY_REINTERPRETATION_OPERATOR_TOKEN`.
   Keep `ELLA_MEMORY_REINTERPRETATION_LEASE_SECONDS` above the expected
   heartbeat interval; the default is 120 seconds.
4. Set `ELLA_MEMORY_REINTERPRETATION_ENABLED=true` to begin transactional enqueue.
5. Validate run-once and metrics while
   `ELLA_MEMORY_REINTERPRETATION_WORKER_ENABLED=false`.
6. Set `ELLA_MEMORY_REINTERPRETATION_WORKER_ENABLED=true` only after staged
   no-change, pending-review, explicit correction, conflict, retry, and Undo
   evidence passes.

Both feature flags default to `false`. Rollback is to disable the worker first,
then disable enqueue. Existing canonical rows and outbox audit remain intact.
