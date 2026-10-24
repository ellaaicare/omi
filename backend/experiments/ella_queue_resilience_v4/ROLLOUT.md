# Exact rollout handoff — no live action performed

Backend-only rollout is authorized through the existing Ella backend operator and writer lock. External Atlas remains sole LIVE n8n writer; this rollout has no n8n changes. Do not bypass any denied access route or use an old recovery launcher.

## Pinned inputs

- Serving snapshot: commit `ec0247ead8ef19a4419cdaac0cc807eb795ab7bc`, under `backend/serving_snapshots/unified-canonical-memory-4235154f`.
- Reviewed integration/control/caller source: commit `64455fcfedbebff479ba35882f87a90a9fe37184`, this package. `SOURCE.PINS.json` maps all source hashes. The package is an overlay; it is not imported by production on this branch.
- Fourteen `candidate/` paths map to the same backend-relative deployment paths. Three modules are additive. The private control helper is not an HTTP route or an automatic launcher.
- Existing private saved-backlog sidecar must stay on the host, unchanged at SHA-256 `59091879a32cab719305f14883e6158f94c0e0a8e87b0aed2bf122ac348a1eaf`. It is intentionally absent from Git. Never use the old launcher that defaults to the first failed source.

## Completed validation

Independent source reviews passed the integration, private controls and narrow backlog caller. All 54 offline tests passed. The fork's selected `backend/test.sh` suite passed 165 tests; 10 PostgreSQL canary tests skipped without a test database. GitHub had no check runs; that is not CI PASS.

Test doubles and limits are in README and the review receipts. Actual acceptance on October 24: 15 service-interpreter imports passed; seven qualification cases passed in a dedicated synthetic Firestore namespace. The SDK retry case injects a pre-RPC commit Aborted; it is not real server contention. No provider result, deployment or physical device success is claimed at this publication.

## Stage 1: source/import and code-only acceptance

1. Acknowledge the exact commit and hashes through the established verified handoff channel. Ensure sole live-writer ownership. Storage migration/cleanup and large unrelated writes remain stopped.
2. Freshly compare the target source hashes with the captured baseline. Abort on drift rather than overwriting another change. Preserve existing source/configuration for rollback; preserve every source, stock/current version, mapping, consent and quarantine.
3. Test the mapped modules with the existing service interpreter/bootstrap in an isolated staging context. No new credentials, environment dump, provider send or application startup worker. Exercise actual Firestore transaction behavior in an approved isolated qualification namespace before policy transition; never use real customer records as fault-injection fixtures.
4. Install the reviewed mapped source using the existing graceful Ella deployment path, under the unchanged v3 policy. Give the already-authorized restart heads-up. Do not require a user necklace Stop, clear ownership or disable normal mode. A shared API restart may briefly reconnect sockets; uninterrupted audio is not qualified by worker tests.
5. Verify actual serving hashes, health and worker startup. No v4 activation occurs at import/startup. Do not replay the first failed saved-memory attempt or submit any backlog at this stage.

## Stage 2: operational transition

Use only the reviewed `control/queue_policy_transition.py` primitives with the same already-bootstrapped database authority.

- Obtain current original-route/consent evidence through the supported resolver, and exact policy/account/tenant/job metadata preimages. Do not remap, reseal enrollment, grant consent or use a lab identity.
- Independently verify whole-resource terminal receipts or owned-cleanup evidence. A hash, dataclass, expired lease, old PID, elapsed time or HTTP200 is insufficient by itself. Unknown outcomes remain quarantined even when the resource is proven idle.
- `upgrade` requires exact v3 policy CAS, preserved ongoing authorization and no active **worker attempts**. A concurrent claim conflicts on the policy read. It never reads capture terminal state. If a worker is active, let it finish or classify its uncertainty; do not require the user to stop recording.
- The upgrade archives the prior policy, retains the epoch/cutover/routing/consent, carries unresolved resources and enters v4 with claims closed. Sources/jobs/accounts/tenants are unchanged.
- Where genuinely needed, `settle_resource` uses fresh exact account/job/tenant/route preimages and terminal evidence. V4 resources bind their immutable tombstone/attempt/lease; legacy resources bind their migration archive. It releases only that resource, never retries a job or applies quarantined output.
- Reopen claims with `set_worker_claims` only after serving/import/transaction acceptance and source review. This is an operational queue change under existing authorization, not a new OAuth or account-access grant.

## Stage 3: one saved-memory proof

1. Select exactly one eligible, never-dispatched saved generic record from the already approved remaining 19, excluding the first failed/quarantined attempt and any selected record still active. A different current capture may continue.
2. Use the reviewed helper's existing normal bootstrap. In particular its consent repository must be configured with the same loaded database client; do not create credentials or repeat consent. Call `prepare_one` with explicit verified UID and selected CID; never rely on an old default.
3. The helper revalidates the fixed private specification, original route, account epoch, source/job hashes, stock version, original first completion, exact owner repair and absence of existing admission/audit. It makes four atomic preparation writes and no provider call itself. The normal worker may dispatch afterward.
4. An uncertain submission result means stop and reconcile audit/marker/job read-only. Never invoke preparation again to find out what happened.
5. Observe exact attempt/lease, actual original-agent generator/verifier execution, preserved transcript/stock version, completed same-source canonical receipt and active enhanced version. Confirm ordinary app-readable latest projection. Admission, returned IDs, generic summary, health or preview alone are insufficient.
6. Only after that successful proof, continue the explicitly authorized eligible backlog with deterministic per-record operations, bounded quotas and per-record outcomes. Stop on an uncertain write/provider result or routing/provenance mismatch. Keep all quarantines and the first failed source untouched.

## Rollback and honest release limits

Before opening v4, `rollback_before_open` permits only an untouched, never-opened revision 0 matching its archived hash. It preserves sources/jobs/accounts. After opening, retain compatible v4 readers with claims closed, tombstones and uncertain resources; do not blindly downgrade or reset attempts.

Worker maintenance and active capture are separate. Physical background/reconnect, Save/Stop UI, fresh recall, chat, voice and illustrations require their own acceptance. Illustrations remain cost-disabled; this release does not reactivate scans or buy credits.

No new credential, OAuth scope, IAM role, public endpoint, account destination or security setting is required by this code. The remaining live boundary is verified-writer execution with genuine current evidence, not another end-user consent request.
