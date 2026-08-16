# Ella canonical summary CAS rollout

## Contract

`PATCH /v1/ella/conversation/{conversation_id}/summary?uid={uid}` supports
`ella-canonical-source-v1`. A conditional request must send both:

```text
X-Ella-CAS-Contract: ella-canonical-source-v1
If-Match: "ella-canonical-source-v1:<64 lowercase hex SHA-256>"
```

The digest covers sorted compact UTF-8 JSON over exactly `type`,
`conversation_id`, `uid`, `started_at`, `finished_at`, `title`, `overview`,
`emoji`, `category`, `segment_count`, and `transcript`. The compare and the
Firestore structured-summary/version update occur in one owner-bound
transaction. A stale digest returns `412`; an incomplete, malformed, or unknown
precondition returns `428`. Neither response writes the conversation.

Success returns only `{"status":"ok"}` and:

```text
X-Ella-CAS-Applied: ella-canonical-source-v1
```

## Compatibility gate and capability signal

`ELLA_SUMMARY_CAS_MODE` accepts:

- `optional` (the temporary default): fully headerless legacy requests retain
  the existing authenticated write behavior. If either CAS header is present,
  both must be exact and the write is conditional; there is no CAS-shaped
  unconditional fallback.
- `required`: every request must provide the exact contract and `If-Match`.

An invalid configured value fails closed as `required`. The credential-free
`GET /v1/ella/conversation/summary/capabilities` response reports the contract,
enforcement mode, conditional-write availability, and whether headerless legacy
writes remain enabled. It contains no owner, conversation, source, or credential
data.

## Caller audit (GitHub source of truth, 2026-08-15)

The audit was performed against `ellaaicare/ella-ai` main
`145abc667a3d746b86dbdd1d18c77d1ce9d5cda2` and PR #1234 head
`c50022e36ad05bf9841aa774a68bb30aef236b5e`.

- Main `services/hermes-runtime/bin/hermes_enrich_omi.py` calls the endpoint
  without CAS headers.
- Main `packages/openclaw/scripts/provision-api.py` emits Observer and correction
  prompts containing headerless curl calls to the endpoint.
- The other main search hits are design/architecture documentation, not callers.
- PR #1234 changes the Hermes caller to send the exact contract and quoted source
  token, require `X-Ella-CAS-Applied`, map `412` to a conflict, and fail before
  network egress unless
  `OMI_SUMMARY_CAS_CONTRACT=ella-canonical-source-v1`.

Immediate required-mode enforcement would therefore break live legacy paths.
The `optional` mode exists only for their coordinated migration.

## Rollout order

1. Merge and deploy the OMI backend with `ELLA_SUMMARY_CAS_MODE=optional` (or
   leave it unset). Verify the public capability response reports `optional`,
   and verify an authenticated conditional canary receives the applied header.
2. Merge and install the reviewed Ella/Hermes caller changes. Keep
   `OMI_SUMMARY_CAS_CONTRACT` unset so the new client fails closed before egress
   while deployment identity and source parity are checked.
3. Remove, update, or disable every headerless OpenClaw/Hermes caller identified
   above. Re-run the repository caller search and confirm no active headerless
   write path remains.
4. Switch the backend to `ELLA_SUMMARY_CAS_MODE=required` first. Verify the
   capability response reports `required`, a headerless authenticated probe
   returns `428`, a stale conditional probe returns `412`, and both probes leave
   the conversation unchanged.
5. Only after step 4, set
   `OMI_SUMMARY_CAS_CONTRACT=ella-canonical-source-v1` for Hermes. Run one exact
   owner/conversation canary and verify the content-free receipt and applied
   header before expanding traffic.
6. Monitor `412` and `428` counts by bounded status/stage only. Do not log source
   JSON, transcript, request payload, authorization values, or CAS headers.

## Rollback

1. Unset `OMI_SUMMARY_CAS_CONTRACT` first. The PR #1234 client then fails closed
   before sending a write.
2. Diagnose source parity or transaction conflicts without bypassing CAS. A
   `412` requires a fresh owner-bound materialization/candidate, never reuse of
   the stale summary.
3. If an explicitly audited legacy caller must be restored, switch the backend
   to `ELLA_SUMMARY_CAS_MODE=optional` and verify the capability receipt changes
   before restoring that caller. CAS-shaped requests remain conditional.
4. Revert the backend deployment only after optional mode is active and health
   is verified. No rollback step should silently turn a request carrying CAS
   headers into an unconditional write.
