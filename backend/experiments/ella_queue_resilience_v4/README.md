# Ella queue resilience v4 — review overlay

This directory preserves the integrated source candidate against pinned serving files. It is **not imported by production** and must not be treated as a complete standalone backend or deployed release. Full serving-overlay reconciliation with fork main is still incomplete.

The `candidate/` paths map directly to backend deployment paths. Do not copy them over a different serving baseline without reviewing `SOURCE.PINS.json` and the semantic diff. Existing v3 policy remains the default; nothing activates v4 at import or startup.

## Behavior

- Current owner, consent, original agent route, source hash, epoch, cutover and exclusions remain required.
- Historical quarantines remain preserved as replay exclusions, rather than changing authority for every future job.
- New unknown attempts create immutable per-job tombstones and a stable owner-bound resource hold. They are never automatically retried.
- Possibly live unresolved service graphs still count toward finite concurrency. Unrelated accounts progress only when capacity remains.
- Proven no-dispatch preflight retries use a fully validated predecessor fence, matching marker and monotonically changing lease/attempt. Their error retry budget is bounded separately from maintenance parking.
- Claims-only maintenance uses a compare-and-swap revision. It does not pause consent, capture, source finalization or exact already-started callbacks.
- Bounded cursor scanning progresses beyond an ineligible first page.
- Effect entry and initial summary CAS check the exact live graph. A concurrent graph close conflicts with the actual source-write transaction.
- Only an already-owned, already-accepted token/source/payload/current-version publication may finish canonical reconciliation after timeout. A new writer invocation against that pending receipt is rejected. Changed-image supersession repair requires a live graph. Completion does not unquarantine the job or release an uncertain resource automatically.
- Worker success requires actual same-source canonical completion metadata in the finish transaction, not merely a returned version ID.

## Tests

```sh
python3 -B -m unittest discover -s backend/experiments/ella_queue_resilience_v4/tests -q
```

53 offline tests passed at this revision; the integrated source and private control helpers have independent source PASS with rollout held. These run actual candidate lifecycle, activation, repository, graph guard, source-CAS, model-entry and canonical-continuation functions against a synthetic serializable store.

Boundary doubles replace Firestore, transcript decoding, native identity helpers, product source binding, transition construction, sanitizer/canonical-source projection and the canonical writer. No SDK credentials, provider, customer data or live backend is used. The included serving hold file is a pinned test fixture.

These tests do **not** qualify real Firestore retries, complete application imports, live worker restart/drain, physical capture continuity, model execution or a handset-visible enhancement.

## Rollout gates

Independent integrated-source and private-control review passed. The exact serving Python snapshot is committed separately. Real SDK/import acceptance, genuine terminal-resource evidence, single-writer deployment and live qualification remain required. No inventory repair, quarantined replay, credential/access change, account remap or data deletion is part of this overlay.

See [the master regression analysis](../../../../docs/engineering/ella-regression-analysis-2026-10-24.md). Live deployment belongs exclusively to the verified external Atlas writer after the exact handoff is delivered. Codex infrastructure coordination is a separate role.

`control/queue_policy_transition.py` contains private, non-executable-launcher migration, pristine rollback and resource-settlement primitives. They require independently reviewed original-route and terminal-resource evidence; a dataclass or receipt hash does not establish authenticity. Neither accounts, sources nor quarantined jobs are rewritten.

The fourteenth source file updates only the private saved-backlog helper's `read_account(..., policy=policy)` call. Its fixed approved sidecar, exact source/stock/transcript/original-route checks, selected-active-source exclusion, attempt-zero rule and no-uncertain-retry wrapper remain unchanged. No private sidecar is published.
