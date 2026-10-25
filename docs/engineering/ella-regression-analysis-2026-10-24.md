# Ella enrichment regression: evidence and recovery review

Status: investigation and isolated implementation; **not a release qualification**.
Prepared October 24, 2026 UTC for independent review, including Claude.
No credentials, account identifiers, transcript bodies or private source packets belong in this document.

## Immediate findings

1. A failed enrichment can invalidate the account's subsequent processing authority. The serving `normal_account_lifecycle.read_account` compares the *current mutable quarantine inventory* with a frozen enrollment inventory. A newly quarantined job changes that inventory, so unrelated future jobs on the same account are held. This is a processing availability regression, not evidence that the user's OAuth-to-agent mapping changed.
2. `normal_enrichment_activation.claim` also counts historical quarantined tenants against global dispatch capacity. Historical failures can eventually obstruct other otherwise eligible tenants. This is distinct from legitimate limits on concurrent live or uncertain remote operations.
3. The operator backlog-repair tool's `capture_not_terminal` prerequisite is not a general requirement for ordinary enrichment. It must not become a requirement that users turn necklaces off before backend upgrades.
4. Stored transcript, stock summary, enhancement and displayed latest version are separate milestones. A working capture or generic summary does not qualify enrichment. Health checks do not qualify any of these milestones.
5. The previously successful integration code is retained. Evidence does not establish that a new app skin deleted the enhancement hook. Rebuilding the entire fork is not presently justified by missing code.

## Evidence boundaries

The user reports earlier enhancement and whispers worked and subsequent builds regressed. That is important historical testimony, but the retained evidence reviewed here does **not** independently establish a year-long deployment timeline or continuous production reliability.

The strongest retained October 14 success is one selected, already stock-completed OMI source processed under isolated overlays. Its final receipt is SHA-256 `6247198200be9fa173eccc76eadb1f25f1d4f0c0f9c8da25396255715c7be8bd`, covering approximately 22:48:06–22:54:46 UTC. It proves that selected source's integration; it does not prove automatic enrollment, new capture, general queue recovery, positive memory learning, whispers, or all accounts.

The current source observations below came from a read-only copy of 16 serving Python files, 447,902 bytes, under backend PID 3709250. They are source evidence, not new provider execution or a current health observation. Exact source pins are recorded below so the review is reproducible without customer content.

## Repository and deployment provenance

Publication base is `ellaaicare/omi` fork main `b43b164828137dfdbb4d2bc0a315b11c7bd7bb3b` (September 27). Backend review branch is `review/ella-backend-resilience-20261024`. It uses an independent Git directory and index on the external SSD; existing dirty checkouts are preserved.

The serving overlay is **not represented by fork main**. It is now preserved in a separate [immutable serving snapshot](../../backend/serving_snapshots/unified-canonical-memory-4235154f/README.md), commit `ec0247ead8ef19a4419cdaac0cc807eb795ab7bc`: 441 runtime Python files, 6,414,728 bytes, exact file hashes and a static import inventory. All touched candidate baseline hashes match. The observed service interpreter distribution inventory is included without credentials/direct URLs. Tests/scripts/configuration/data are excluded; full imports, dynamic configuration and physical runtime behavior are not qualified by this source snapshot.

Retained repository history identifies these original add-on commits:

| Change | Retained commit | Qualification |
| --- | --- | --- |
| Post-save hook added | `6de3fce397131f8ff47d7ba88b15b84f912152f0` | Historical source evidence |
| Reprocessing hook enabled | `14eea04601caa8697034de2c07fc3cafa435ca94` | Historical source evidence |
| Retained original server HEAD | `d7ea9e9c1d930f082e3a5a93c8a9172c38d39966` | Not the current local fork HEAD |

The exact Git commit introducing the mutable inventory regression is **not yet established**. The relevant normal-account modules were deployed as overlays. The source defect is established; a commit attribution must await deployment/history reconciliation rather than assigning an unrelated app commit.

Current upstream main was reported by the iOS owner as `699040f2223c9167e2fd264ac9e92fe2d62a65e5`. That object is not in this backend clone. Merge-base, ahead/behind and upstream drift are therefore not yet measured here. Counts of changed files are not counts of defects.

## Call chain and divergence inventory

Intended ordinary flow:

`authenticated app subject → existing consent → original personal-agent resolver → source persistence/finalization → native provenance → source admission → worker claim → context synchronization → generator/verifier → conditional canonical writeback → latest app read`

Preserve each user's existing mapping. An admission allowlist controls who may process; it does not define the destination. Operator model-provider credentials and app OAuth subjects are also different boundaries and must not be conflated.

| Boundary | Verified divergence or limitation | Consequence |
| --- | --- | --- |
| Legacy/new hook | Replacement cohort suppresses legacy webhook and selects the atomic outbox, even when replacement is unavailable | Do not restore by enabling both paths; uncertain duplicate sends are unsafe |
| Account authority | Enrollment seals quarantine image; subsequent `read_account` requires exact current equality | One new failure poisons later admission |
| Global capacity | Historical quarantine tenant count plus active count consumes capacity | Failures can permanently reduce queue progress |
| Retry marker | Prepared marker assumes attempt 0; consumed marker assumes attempt 1 | Existing preflight retry setting alone cannot safely enable retries |
| Worker maintenance | `dispatch_ready == false` exits the worker loop | A temporary claims pause must be distinct from unavailable authorization |
| Authority callback | Earlier host/client deadline mismatch was corrected separately | That correction alone did not prove every retained-completion failure was a timeout |
| Fieldy provenance | New source stubs omitted owner UID required by strict admission | Corrected source ownership is separate from model/enrichment success |
| Learning-none fast path | Redundant hydration preceded a later snapshot transport timeout | Fast path deployed; successful enhanced writeback still needs evidence |
| App projection | Prior UI expected `hermes_parallel`/result ID while backend used canonical `hermes`/`hermes_enriched` receipt | A persisted result can still be mislabeled or fail to refresh |
| Empty successor | Empty follow-on memory could obscure a nonempty predecessor | Preserve source records while fixing newest meaningful-memory projection |

## Why one failed attempt blocked the backlog

The first explicitly authorized saved-memory recovery was admitted once. Its retained completion later became uncertain and was quarantined; original transcript and stock version remained. The remaining 19 were not submitted. This was not successful enhancement.

The new quarantine changed the account inventory beyond the four sealed historical entries. The account gate then rejected later processing. The proposed additive inventory repair addressed that exact incident but retained a separate operator requirement for terminal capture. The user's criticism is valid: neither whole-account history poisoning nor a user-stop maintenance requirement is a sustainable product contract.

Unknown provider outcomes still must not be retried blindly. The correction is to separate immutable owner/route authority, immutable per-job replay quarantine, and live or uncertain resource reservations. Removing all three guards together would trade availability failure for privacy and duplicate-processing risk.

## Isolated correction under development

The [integrated review overlay](../../backend/experiments/ella_queue_resilience_v4/README.md) now contains 14 mapped backend files plus private policy-transition/settlement controls. Independent review returned **integrated source PASS** and **private control source PASS, rollout held**. The packaged offline suite has **53 passing tests**. The fork's `backend/test.sh` also exited 0; 10 PostgreSQL canary tests skipped without a configured test database. No CI or live-backend success is implied.

Actual candidate functions cover lifecycle authority/admission/eligibility, claim/begin/finish, bounded cursor scanning, maintenance CAS, graph-effect entry, source-summary CAS and accepted canonical continuation. Tests use a synthetic serializable store and explicit boundary doubles for SDK/decoding/native/source helpers, sanitizer/canonical projection and the canonical writer. They do not establish real Firestore transaction retry semantics or complete application import parity.

Review found and corrected malformed persisted effect handling, retry predecessor-fence validation, incomplete resource-hold consistency, unknown-version fallthrough, maintenance status leaking into the privacy hold, and new pending-publication invocation after graph closure. The final source review is narrower than handset/production acceptance.

Required integrated behavior:

- Explicit versioned opt-in; existing v3 behavior unchanged until controlled migration.
- Keep current consent, subject, original route, epoch, source hash, cutover and exclusions enforced.
- Retain sealed historical records without resealing authority for every new failure.
- Reject the exact quarantined job at claim, begin and callback through immutable job identity/tombstones.
- Start every external effect only after a transactional attempt/phase fence commits.
- Atomically close that same fence at terminal classification; a late START cannot race a supposedly safe preflight retry.
- Retry only bounded, proven no-dispatch preflight failures. An allowlisted error code alone is not proof. Attempts and leases must be ABA-safe.
- Preserve uncertain remote effects as specific resource fences until a validated completion or owned cleanup proof exists. Lease expiry and local cancellation are insufficient.
- A late completion may settle a resource reservation; it must not reopen a quarantined job or apply stale output.
- Use a claims-only maintenance revision: block new claim/begin, allow already-started exact attempts to finish, preserve source admission and capture, and resume polling without a login/setup reset.
- Bound scans with progress so old ineligible rows cannot starve healthy work.

The coarse reservation covers the entire awaited service graph. Individual entry guards check its exact phase before new host/model effects. Initial source CAS reads that same job in its own transaction. Unknown outcomes retain a resource reservation rather than assuming a remote request stopped. Only the already-owned accepted token/source/payload/current-version publication may finish canonical reconciliation after timeout; new writer invocation or changed-image repair after closure is rejected. Canonical success metadata is checked before releasing graph capacity.

Private controls now implement exact v3-to-v4 CAS while worker attempts are drained (capture may remain active), never-opened rollback, and resource-only settlement with unchanged quarantined jobs. Actual terminal-receipt authenticity and current original-route evidence remain external inputs; a hash or dataclass does not prove either. No control has executed against the backend. The saved-backlog helper now explicitly passes the current policy to `read_account`; an independent AST comparison confirmed that is its only semantic change. Fixed approved-record scope, original-agent proof, source/stock/transcript pins, attempt-zero restriction and unknown-outcome/no-retry remain. Its selected-active-source exclusion is retained while a different active capture may continue.

## Maintenance and rollback

Prefer separating worker maintenance from the capture API. Close new claims with a compare-and-swap maintenance revision, drain owned work, settle or fence uncertain resources, deploy the worker, verify serving hashes and reopen claims. Capture should continue storing and finalizing into a durable queue.

An API process restart is a different operation. It requires actual accepted-frame persistence, graceful drain, supported client reconnect and owner-generation reconciliation. Worker tests do not prove uninterrupted audio across an API restart.

Migration must not automatically activate a new policy at import/startup. Stage code first, verify every reader supports it, then explicitly transition policy. Roll back with claims closed and new per-job tombstones retained. Blindly downgrading policy and forgetting new attempts is unsafe. Saved transcripts, stock versions, routes and quarantines must remain unchanged.

## Architecture decision for independent review

| Option | When justified | Current assessment |
| --- | --- | --- |
| Repair in place | Capture, persisted data and original integration are recoverable; failures have bounded causes | Preferred first: capture works in qualified episodes, integration code exists, queue defect is explicit |
| Minimal integration on current upstream | Measured divergence prevents maintaining capture/auth independently; a preserved baseline can pass core tests sooner | Viable fallback after measured merge-base/dependency audit; upstream supplies capture, not our Hermes integration |
| Clean rewrite | Existing data/account contracts cannot be preserved or isolated and a prototype demonstrates a safer migration | Not established; no deletion, reset or rewrite authorized |

The minimum independently testable architecture separates capture persistence, authenticated account resolution, job lifecycle, model execution, canonical writeback and app projection. A queue failure must not become a consent/setup failure. Test harness fixture admission must not silently replace normal product routing.

Any rebaseline must preserve original OAuth mappings, per-user agents/workspaces, immutable source history, current versions and operation identities. Run read-only parity checks and synthetic isolated shadow tests before cutover; no copying personal transcripts to a different context as a shortcut.

## Missing integrated tests and release acceptance

Required regressions include foreign owner/route, changed receipt/epoch/hash, stale worker, START-vs-close race, retry ABA, unknown commit, late callback, per-job quarantine without account poison, global history without capacity poison, scan starvation, claims pause/resume and versioned migration/rollback.

For each actual handset acceptance, record installed build and visible account; select only the explicitly authorized device; establish current source/session/generation; play one fresh synthetic input; verify transcript and saved stock memory; verify actual original-agent generator/verifier execution and same-source conditional writeback; then verify latest app version, visible enhanced body and fresh recall. Separately exercise background, reconnect, explicit Save and active Stop. Do not count a preview, host playback, provider start or HTTP health as end-to-end success.

Whispers and audio audibility remain separate qualification. No current evidence here qualifies them. Cosmetic changes and new features should not delay the smallest usable memory path.

## iOS and infrastructure appendices

The iOS owner published [the iOS evidence appendix](https://github.com/ellaaicare/omi/blob/707cc985cb556059736edd26b0fc8e9650077972/docs/engineering/ella-ios-regression-evidence-2026-10-24.md) on a separate branch, including reconciliation of Claude's review. This immutable link avoids implying it has been merged into this branch.

Its current physical-snapshot evidence: Main966→967 differs in exactly seven lib paths (setup gate, capture Stop escape/dock/runtime, presentation lease, controller and coordinator). Thin29→30 differs only in `lib/src/test_device_picker.dart`. These are file-hash deltas, not Git commits or proof of installed behavior. The independently reviewed [artifact/source reconciliation bundle](https://github.com/ellaaicare/omi/tree/b25896a6a61532566ccbc66cf8c11815acfb46bc/docs/engineering/reconciliation/main967) and [recovered Main966 baseline proposal](https://github.com/ellaaicare/omi/blob/55c24ba1012c12d368768881750f37f96ff8e930/docs/engineering/reconciliation/main967/recovered-main966-baseline-proposal.json) are published. The iOS owner subsequently published [recovered selected Main966 source plus the Main967 delta](https://github.com/ellaaicare/omi/tree/5cd0c411c76bcb1efed619ad552852d3185bae78): recovered baseline `c88891c8d6aad321421d48fdcc6272f9d808c2ee`, successor `5cd0c411c76bcb1efed619ad552852d3185bae78`. This is a recovery publication, not the original build commit. Generated/native/configuration closure and SDK acceptance still remain, so a complete compiled-artifact join is not claimed. No app build/upload is claimed by this backend branch. Snapshot-to-fork lib comparison is 454 identical, 328 added, 213 modified and one removed; 69 changed/added localization files. Do not infer a fault count from this drift.

The [infrastructure appendix](https://github.com/ellaaicare/ella-ai/blob/0034f2ca2fcd5da33541d6d6e79f1fee654b03e5/docs/engineering/ella-infrastructure-regression-evidence-2026-10-24.md) is published in the separate infrastructure repository. Codex infrastructure coordination is not the external Telegram Atlas live writer; only the verified live writer may deploy. This backend branch does not mutate the infrastructure repository.

## Serving-source pins for causal claims

| Module | SHA-256 |
| --- | --- |
| `database/normal_account_lifecycle.py` | `a9ddc5772772e8a914471de3ceaab0dc2e416ffeb3ff7b2aeeea429357f4f70d` |
| `database/normal_enrichment_activation.py` | `2e107eeee018de8e12c49dff5ffb3bbaf584cce4ffad3808179699892c268d63` |
| `database/hermes_cloud_enrichment_outbox.py` | `86bc5ab4096eb1db56d9ab42938b54a8c2ec7b400948c8702e567db935965382` |
| `ella/services/hermes_cloud_enrichment_outbox.py` | `571d3cf6099c0d857cf02287dc147cd29211800b59f86bcd0751399327507314` |
| `ella/services/summary_writeback.py` | `a3f09400b2a52fcc44cd735c121aa1b9fc8f668f46702ef6acf2cf9e7b4eb4dd` |

Key source locations in the pinned files: lifecycle `read_account` lines 192–209; activation `claim` capacity line 323 and `finish` quarantine updates around 454; worker `run_forever` around 542–547. Line numbers refer to the retained serving snapshot, not necessarily this branch's tree.

## External Claude review reconciliation

Claude's final 16,940-byte review was independently read at SHA-256 `f4b09f5cf2b5d53485ad87e92a96128fb1fe0f78fdfd4debbdafed8c8b18071a`. It recommends repair in place after source capture, deferring a minimal current-upstream integration and rejecting a rewrite for now. Its six blockers map to this analysis: baseline drift; mutable authority; historical/uncertain capacity; maintenance; attempt/effect fencing; and silent result/UX paths.

Its proposed 13 regressions were review suggestions, not tests it executed. Several now have queue-focused offline coverage in the 53-test package; cross-repo UI contracts, manual Finish versus automatic finalization, real SDK/import behavior and physical background/reconnect remain separately unqualified.

Do not promote its Git measurements beyond their scope: lack of common ancestry does not make content re-port or unrelated-history integration impossible, and history rewrite is an inference. Drift measured against a cached September 30 tip is neither current drift nor a valid monotonic lower bound. Its effort estimates are low-confidence opinions, not schedule commitments. No large upstream fetch was performed under the storage hold.

This source publication closes the missing-reviewable-code gap for the captured runtime Python and candidate overlay. It does not claim the entire historical fork, protected configuration, every installed artifact or all transitive non-Python assets are reconciled. Deployment and one original-agent saved-memory proof remain necessary before backlog processing.

## Questions for Claude

1. Does the proposed separation preserve original identity/source authorization while removing accidental history-wide availability coupling?
2. Are all mutating host/model/learning call sites covered by the same transactional attempt fence, including delayed threads and callbacks?
3. Can versioned worker-only maintenance be delivered before changing the capture API, with truthful UI status?
4. Which minimal change demonstrates automatic enhanced memory in an existing mapped account without a fixture allowlist?
5. Does measured upstream divergence justify a preserved minimal rebaseline, or are bounded repairs demonstrably safer and faster?

Please distinguish source proof, retained receipt proof, user reports and hypotheses. Do not propose replay of uncertain jobs, account remapping, disabled owner checks or removal of preserved data as a recovery shortcut.

### Actual backend rollout and first saved-memory verification, October 24 UTC

At 09:52:41, the 14-file reviewed queue-isolation overlay was installed on the existing Ella backend; serving hashes and both health endpoints passed. The selected source successor is commit `89f7148995213c8a5817988993f876a562af1df5` (54 offline checks, 165 selected backend tests passed; 10 PostgreSQL tests skipped). Source publication and service health are not enrichment acceptance.

At 10:26:44, an independently reviewed exact migration entered policy v4 with claims closed. Twelve historical quarantines remained UNKNOWN and unreplayable. Execution detachment was established separately using owned sync receipts, exact producer-operation metadata, stateless child/process cutoffs, original routing, and server timestamps of unchanged quarantined images. No transcript, stock version, account or agent mapping was changed by the migration. Claims reopened at 10:28:26 with the existing concurrency and rate limits.

One explicitly authorized, previously undispatched saved generic memory was prepared at 10:30:06. Its single attempt reached canonical-user confirmation and a no-learning decision, then ended in `retained_completion_rejected` without a producer START or enhanced canonical writeback. Its transcript and original stock version were verified unchanged. The remaining 18 eligible saved records were not submitted. The new failure is individually quarantined rather than incorporated into account authority, but its execution-resource hold still requires exact reconciliation.

This is **not** a working-enrichment release claim. The next verified defect in observability is that the known provisioning-error path hides the inner pre-model rejection; the existing generic diagnostic path does not preserve it. The historical cause of this new rejection is not established. Investigate the reservation/authority/context stage, preserve the failed attempt, and do not infer a timeout fix or admit the remainder merely from health, admission, or a generic summary. Physical app display, background/reconnect, chat, voice and illustrations remain separate acceptance obligations.


### Deadline/transport follow-through at 12:05 UTC

A subsequent single authorized saved-memory verification reached the original mapped provider but ended quarantined with `normal_enrichment_timeout_unknown`. Stock and transcript stayed intact; no enhanced canonical writeback was confirmed. Host metadata showed HTTP 200 then a transport hold after about 90.155 seconds without validated completion. This does not establish the provider-delay cause or remote final outcome. No uncertain attempt was replayed.

Source review found competing nested deadlines, missing TimeoutError diagnostics, byte counters updated only after EOF, and silent expansion of the caller’s 1,600-token limit to 8,192. The successor in `backend/experiments/ella_queue_resilience_v4` has 83 offline tests and independent source review. It preserves the existing 600-second total job ceiling, provider90/child at 100, exact attempt binding and all publication guards; roles share remaining job time and gateway response allowance is 105 seconds. Service imports, reviewed rollout and one durable enhanced writeback remain acceptance gates; this candidate is not a serving-success claim.


### Single post-correction verification: terminal failure, not success

The deadline/transport correction was installed with exact source and import checks. The prior failed execution reservation was released without changing its quarantine. One subsequent approved source was admitted once. Its original route and transcript remained intact, but no enhanced writeback was confirmed. A complete provider HTTP 200 response (13,928 bytes, 31.57 seconds) was rejected by the existing nonterminal/message/tool predicate and surfaced as gateway 503. Finish reason and usage were absent from the retained diagnostic; length must not be treated as confirmed. Sixteen saved sources remain unsubmitted.

The metadata-only successor at `backend/experiments/ella_completion_metadata_v1` has 13 independently reviewed offline tests and staged import acceptance; it is not yet a deployment or provider-success claim. Existing logged-in provider-log metadata is needed to recover the historical finish/usage facts, without prompts, outputs, new keys or further inference. Source review also confirms eight backend runtime samples per successful context snapshot; the observed roughly 422 seconds before inference is not itself per-stage attribution. A proposed typed read-only snapshot bundle retains before/after hydration checks while removing redundant re-sampling around an in-memory copy. This optimization remains unimplemented and unqualified.


### Independently reproduced defensive protocol gaps

The installed transport also accepted two synthetic false-success envelopes: a per-choice error beside an otherwise valid stop/assistant/JSON response, and a `chat.completion.chunk` object on the non-streaming path. The independently reviewed candidate at `backend/experiments/ella_protocol_hardening_v1` rejects both and has 21 offline tests. It also rejects malformed non-null error values while preserving valid missing/null errors and every existing completion/ownership/one-use/deadline guard. It does not explain the historical nonterminal rejection, is not deployed, and does not justify another saved-record or inference attempt while provider-log access is blocked.

The provider dashboard was verified signed out. Historical finish/usage remains unknown; no credential workaround or new inference was used. The metadata-only source at `backend/experiments/ella_completion_metadata_v1` was installed at 13:25 UTC with fresh gateway-interpreter imports and no gateway/shim restart. It can retain future closed facts, not reconstruct the lost old response.

## October24 provider metadata and output-budget correction

The user-confirmed provider account exposed one historical row matching the failed request time/model/duration: finish length,1600 output tokens and1596 reasoning tokens. This is strong candidate attribution, not an exact locally retained generation-ID join. Public exact-model metadata permits reasoning effort none. The one-field request correction is published in `ella_reasoning_budget_v1`, commit6c114874a5, and installed with fresh-import and health checks.

An already queued original-owner job then ran automatically, outside the saved20 recovery set. Its new durable generation metadata shows stop,1143completion tokens,0reasoning and no tools; strict JSON-object content validation still rejected it. No canonical enhanced writeback was confirmed; source/stock stayed. This distinguishes a demonstrated change in provider behavior from an unresolved content-contract failure. No additional recovery-set record was admitted.

The dedicated caller cap1600 has no recorded rationale and is not a universal chat or per-key limit. The independently reviewed `ella_output_budget_v2` raises it to4096 under the existing8192gateway ceiling,90-second provider/600-second operation bounds and16,000-byte producer result guard. Three focused source-to-fake-transport checks and165standard backend checks passed;10Postgres checks skipped. Actual one-file rollout19:25:16UTC reports newPID1636275 and both health200, with transient typed claim-maintenance close/reopen and no credential/routing change. This is not a claim to fix invalid JSON or restore enrichment.

Read-only existing OAuth metadata and the actual authenticated Codex model catalog now establish that gpt-6.1-sol and gpt-6-luna are listed for that account. They do not prove completed inference. No refresh/new credential/model switch occurred. The existing dedicated Codex route already uses Responses; exact reasoning-policy propagation, endpoint-specific output-budget handling and Ella-only model-policy updates remain qualification work. Subscription traffic is not unlimited.


## 21:08 UTC recovery checkpoint: valid generation, context-read failure

The reviewed trusted-instruction/closed JSON diagnostic correction is preserved in `ella_json_contract_v1`. Its one controlled subsequent original-account attempt returned a validated terminal JSON object: stop,2379completion tokens,0reasoning, no tools/refusal. Its nonce-bound durable worker COMPLETED file exists. The backend nevertheless failed in the post-generation companion-context read before generator-ledger completion or enhanced canonical writeback. Transcript, stock version, original profile and quarantine remain unchanged. Retained output is not permission to reopen a closed attempt: no supported retrieval-only continuation branch currently exists.

The measured dispatch-to-quarantine interval was484.73seconds. The host-read budget has a separate480second wall deadline inside the600second operation. Source tracing shows repeated full remote hydration for pure logical freshness, plus five authority/context reads per role. This is an evidenced inefficient call chain and budget constraint, not proof of historical lock contention or stale-session behavior.

The independently reviewed `ella_context_read_v1` candidate separates fresh source/owner/policy checks from repeated remote hydration in the trusted exact worker path. Full context/consent/runtime checks remain before model egress, before canonical egress, after canonical readback and at final publication. No permissive authority is cached. The14case actual-function rig counts21fullhydrations before versus8after while retaining two model calls, two canonical events and one publication; semantic-leaf/runtime/HTTP/storage adapters are explicit doubles. Mutation, cancellation, attempt/lease-scope and deadline negatives pass.600job/480host/90provider/4096caller, parser, source, owner, epoch, CAS and no-replay bounds are unchanged. Four-file staged imports in the actual service interpreter passed with network blocked; production latency and enhanced writeback remain unverified at this checkpoint.

The separately reviewed Thin adapter candidate is preserved in `ella_thin_mcp_adapter_v2`: existing version-builder static delegation and native field-path transaction.update fix missing API and stale nested presentation. Owner10pure cases, independent actual shared report/pending/version/current composition and actual SDK field-path extraction pass; enhanced crypto/storage/auth boundaries were synthetic. No new fixture/ingress/grant is added. A live Thin source must come through its authenticated normal capture/upload path.

Execution-only capacity for the failed controlled attempt was settled using the exact completed generator, completed sync and closed-fence proof. Its quarantine and generated result remain preserved. The consumed one-job control was archived at21:08:01 with claims stillclosed, no job/source/account/tenant/tombstone writes and no replay. These are bounded operator controls, not a product requirement to stop an end-user recording. Context/Thin rollout and another exact controlled acceptance remain pending.

Agent recall is a separate useful milestone from app writeback. Bounded retained evidence showed enrichment using the original personal workspace while an older chat path usedplato-eval; a subsequent personal-chat activation was held at a runtime-account identifier mismatch. Current shared iMessage/enrichment activation and durable recall are not established by those historical artifacts. Do not merge profiles or infer cross-account disclosure from the split alone.


## Real binding-class regression and permanent deployment gate

The context-read optimization and Thin adapter were deployed at 21:09:54 UTC with serving hashes, ABI imports and both health routes verified. One subsequent controlled saved memory stopped before model dispatch. The new optional source reader returned `semantic_publication.PublicationBinding`, but its check expected the separate `semantic_producer_v1.PublicationBinding`. Although their fields match, the classes are distinct. The first fixture incorrectly aliased them, so its pass did not establish runtime type compatibility. This introduced regression is acknowledged rather than attributed to the provider. The failed job remains quarantined, with transcript and stock version preserved; the original 16 unsubmitted repair records were not consumed.

`ella_context_binding_v2` makes the one-identifier correction to validate the publication-service class, compare it with the independently resampled publication-service binding, then keep the existing conversion to the producer protocol class. Fifteen cases now use both distinct retained real class definitions. The old version reproduces the failure with no provider call; the corrected composition and wrong-class negative pass. The actual service interpreter additionally executed both installed classes: old baseline rejected, candidate passed the exact boundary, then deliberately stopped before context/model/database effects with network blocked.

The real-class composition is now a required `backend/test.sh` test module. A separately pinned service-interpreter behavioral smoke is a mandatory rollout-controller preflight before any backup or service stop. This supplements import checks and mock-backed fault tests. Existing account/source/context/lease/CAS fences and quarantine history remain unchanged; no old attempt is reopened by the fix.

A metadata-only check also verified a source-linked, mode-0600 conversation archive and managed index under the original personal agent workspace. This is durable source material, not enhanced personal-fact memory, verified iMessage recall or app writeback. The historical chat-profile split still needs current route qualification before claiming one usable shared context.


## 21:51 UTC serving and single-attempt result

The one-identifier binding correction was deployed at21:39:33 UTC with bothhealth200, PID1825057 and ledgerSHA `1cb305f90e3885657b20f45b4df5dfabbe7794b696572f9db9e7ce9063a81ff8`. The mandatory real-service preflight reproduced baseline rejection and candidate passage before restart. Postdeployment both installed and candidate classes passed that exact boundary, stopping deliberately before context/provider/database effects. Source and permanent test changes are in commit `5eb49ceed5d45ba434019d75bce592af5dfc069d`, verified on the existing review branch.

After independent constant-binding review and fresh closed-policy/route/consent checks, exactly one other eligible saved generic memory was admitted. It is outside the original20-record repair set;16 of that set remain unsubmitted. The job finished quarantined on attempt1 with the concrete inner rejection `retained_completion_context_changed` at the reservation comparison, before model generation. This does not demonstrate the later producer boundary in a live job. At21:50:56 its original transcript, stock version and account/profile routing matched, producer operations were empty, the attempt fence was closed and bothhealthroutes returned200. No enhanced active version or writeback receipt exists. Claims remain deliberately closed; one execution-resource hold and its consumed qualification permit remain preserved for reviewed settlement, with no retry.

Source review establishes the context hash is deterministic for identical runtime identity and selected raw document contents/presence. It has no per-read timestamp or nonce. The sync materializer likewise adds no clock to the selected context documents. The current rejection therefore cannot safely be dismissed as cosmetic timing. Existing evidence does not retain the two per-document snapshots, so concurrent legitimate context edits versus file-alias/presence changes remain unresolved. Current document hashes alone cannot reconstruct the earlier difference. The next diagnostic must compare closed document hashes/presence and revision metadata without exporting contents or weakening context equality; another saved record must not be consumed merely to guess the cause.

Thin v2 separately remains serving at SHA `c73d1cb1c059c8efd972bfd1d421ab2ddc48876c53835ec6534e0a3de0d70d69`, verified with current imports at21:43:44. Its shared Muse handoff records this exact hash and the transaction-fixture/import limits. No live Thin write, fresh ingress, new grant or source window is implied. Codex migration and same-context iMessage recall remain unqualified.


## 22:06 UTC context diagnostics and independent durable retrieval

No additional enrichment attempt was submitted. The exact failed job remains closed/quarantined, with its resource hold and used permit preserved. An actual read-only graph check at21:57:02 reports original account authority available and the closed attempt rejected; claims remain closed.

The two exact snapshots compared by the failed reservation were not retained. Its sync journal contains only an earlier context digest/revision and binding, not document bodies or per-document hashes. The existing host snapshot operation rejected the quarantined source at the first request; the control stopped without requesting a second snapshot or bypassing the guard. Thus no retrospective changed-field claim is made. A separate metadata comparison from21:49:00 to22:04:35 found only `recent-conversations.md` changed among selected raw context files. This later observation establishes that the context index changes, not that it caused the earlier failure. The current80-entry index has zero timestamp ties, so a possible equal-time set-order weakness is not established as this incident's cause.

`ella_context_comparison_v1` is an independently reviewed source-only diagnostic candidate. It logs fixed-key hashes, byte counts, revision and owner equality before the unchanged context rejection. Five cases execute the entire actual retained coordinator class and real host hydration function, with explicit external storage/authority/canonical/learning boundary doubles. Stable input reaches a deliberate producer stop; changed content, missing-versus-empty raw files, revision changes and failing diagnostics preserve rejection with zero producer calls or policy commits. The portable five-case module passes and is now included in `backend/test.sh`; the previously qualified full suite was not rerun for this documentation/diagnostic addition. Nine additional malformed projection checks also passed independently. At22:02:03 the actual service interpreter matched the serving baseline hash, imported complete candidate modules with real dependencies, and checked their ABI/schema with zero database query or provider call. No diagnostic deployment or freshness relaxation occurred.

The simpler retrieval path has an actual positive result independent of app summary writeback. At21:57:10 the production `canonical_memory.read_memory` and `render_memory` returned the exact source's durable `omi_transcript` event under the original personal chat profile and before/after runtime/consent checks. It matched the current canonical transcript in memory. Rendering is truncated and exact detail remains necessary for full content. At22:04:15 the source-linked0600 archive in that same personal agent workspace also matched the current canonical transcript byte digest after the archive's documented strip operation. Only hashes/counts were exported. This proves durable local/canonical source retrieval; it does not prove an actual agent answer, enhanced personal facts, whole-backlog coverage or same-context iMessage delivery. No chat/model call, new credential, mapping change or summary write occurred.

Valid recovery must distinguish a known pre-generation, no-policy-commit context preparation change from unknown generation/commit outcomes. Only the former can support bounded recomputation with new immutable context policy and freshly validated source/owner/consent/runtime; the latter remains held until exact continuation/retirement proof. The diagnostic candidate implements neither replay nor acceptance of stale context.

The portable permanent context regression module now has7passingcases, including selected-file mtime-only changes and unselected attempt metadata. Both retain an unchanged context and reach the deliberate producer boundary, confirming those volatile filesystem/attempt metadata are not context-identity inputs. Exact reviewed snapshot bytes are preserved under`.py.source` after correcting a formatting-hook normalization in the first publication; the source pin manifest describes the corrected files. These are source/offline qualifications, not a diagnostic rollout or explanation of the unretained historical delta.

## 23:30 UTC captured-context implementation and rollout

The implementation is now live, rather than a proposed test contract. Exact candidate sources and test-source evidence are published at commit `5dadad1253360a835c1546a4a788a7bd754b465a` in [ella_captured_context_v2](../../backend/experiments/ella_captured_context_v2/README.md). This package is explicitly not a portable standalone test closure or a real-provider success report.

Version 2 captures the companion input once after confirmed canonical-user and learning phases. Both model roles and the publication chain use those same bytes. Each reuse still checks current authority, account, consent, target source and stock version, deletion/lock state, attempt/lease/deadline, protected workspace files and every captured record's canonical source, transcript, active version and rendered-row hash. A new unrelated recent-index entry or rolling eviction does not replace the captured model input. Exact captured CIDs are re-read through existing authenticated service authority. Manifest/attempt digests are included in publication identity; a digest is not an authorization grant. Legacy v1 callers retain their existing path. The host still reads the current index during validation, so unrelated index instability can cause a safe rejection; that availability limitation remains disclosed.

Actual qualifications include 56 scope/batch cases using real operation-budget, attempt and publication classes with explicitly doubled external leaves, independent controller filesystem/failure cases, production Python 3.11 imports with no socket attempts and consistent real publication class identities, and real decryption/strict decoding/projection of the original owner's 20 saved records in 2453 ms. These do not prove complete HTTP latency or provider output. Review caught and corrected rollback handling of a foreign uninstalled path, host add-only hardlink cleanup ordering, receipt/directory durability, and finite diagnostic-code propagation before rollout.

The existing Ella provision shim was restarted at 23:23:47 UTC, with exact installed hashes, health 200 and unauthenticated rejection on both new routes. Only that named service was restarted; credentials, registry, unrelated gateways and n8n were unchanged. The backend's eight-file rollout completed at 23:24:53 UTC, PID 1942964, with both health routes 200, installed-module behavioral/import checks and unchanged six protected-state hashes. The new backend metadata route also rejected an unauthenticated request with HTTP 403.

The old quarantined job was not retried. Fresh evidence verified its closed graph, confirmed canonical user, completed no-learning and sync phases, empty producer operation ledger and absent writeback. A separately reviewed exact CAS removed only its execution-capacity hold at 23:28:16 UTC, preserving the job, tombstone, transcript, stock, account and unknown provider outcome. Its consumed single-test permit was archived at 23:30:06 UTC with claims still closed. The 20-record original repair set currently has four quarantined attempts and 16 zero-attempt records still blocked; this is distinct from additional controlled records outside that set. The next original saved record passed the unchanged exact repair planner. One successful original-agent execution and canonical enhanced readback are still required before any backlog batch. No such writeback is claimed here.

The separate agent-answer test returned a terminal unavailable answer. Source inspection explains the missing retrieval path: the direct gateway enabled only a curated-note `memory` tool, while its automatic context loader did not follow conversation-index links. Normal backend chat already injects owner-bound canonical memory, so the direct test is not proof that normal chat lacks its source. No broad file tools or new credentials were enabled. Durable archive presence remains separate from actual agent recall and iMessage session equivalence. OpenRouter writeback recovery precedes the requested Codex provider comparison.


## October 25 00:43 UTC: canonical projection deployed; post-generation completion still blocked

The reviewed canonical-projection v2.1 correction is deployed. Published source is commit `becf54372ea9463df4d4efe79e11ee9a5fb0b7bf`; 77 offline cases passed (40 backend, 3 diagnostic, 34 host; inherited cases included), and actual service/helper imports passed with network blocked. Deployment health checks passed. This fixes canonical context assembly; it is not an enhancement-success claim.

One subsequent original-owner saved record passed captured-context admission and reached the model. Exact operation-retained diagnostics show HTTP 200, terminal stop, JSON object, 1,026 completion tokens, zero reasoning tokens, no tool/function calls or refusal. The retained worker completed at 00:25:07 UTC. A read-only metadata query independently joined its original owner, exact request digest and provider response ID to a durable canonical assistant event: response-parsed timestamp 00:25:07.203821 and insertion 00:26:00.572423. The provider result therefore reached canonical storage. The source-summary producer ledger nevertheless remained START and the normal job ended quarantined after its existing 600-second overall deadline, with no enhanced app writeback.

The historical per-stage diagnostic query returned zero records. The approximately 53-second interval before canonical insertion cannot yet be divided reliably between authority reads and persistence. Post-insertion time also cannot yet be assigned conclusively to context validation, runtime/consent checks or a transaction. A new read-only query's 73 ms pool-open and 7 ms lookup are current availability measurements, not retrospective proof. Source review confirms repeated full captured-context reads at role boundaries, including after canonical insertion; their exact historical cumulative cost remains unmeasured.

The original authorized 20-record recovery set now consists of 6 one-attempt quarantines and 14 untouched blocked records, with zero enhanced writebacks. The reduction from 16 untouched to 14 reflects the separate stale-projection attempt and this post-generation timeout attempt. No batch was admitted and no quarantine was retried. Source transcripts, stock versions and original account/profile routing remain preserved. The completed generator response is retained. Current recovery has no reviewed automatic continuation branch that reconstructs and validates the attempt-private frozen context after its worker fence is closed; retaining a response is not permission to promote it into a new attempt.

Next scope is a phase-specific authority/context latency correction with actual-class regression coverage, independent review and unchanged source/deletion, owner/consent/runtime, lease/attempt, deadline and publication checks. No new inference is authorized by these diagnostic observations alone. Requested Codex comparison remains after OpenRouter end-to-end writeback; direct-gateway recall limitations remain separate.


### 00:57 UTC phase correction source qualification

The [three-file phase correction](../../backend/experiments/ella_captured_context_latency_v1/README.md) now has 21 passing offline cases, including actual-class two-role/ledger/publication composition, and actual-service Python 3.11 imports with networking blocked. It reduces full context reads from nine to five through the retained producer and leaves two final publication reads unchanged. Independent root review verified the ledger freshness, execution and receipt methods are AST-identical. No live code or queue state has changed in this diagnostic pass. Stress cases explicitly do not establish a reliable 480/600-second production margin. The existing stage trace was configured for a different account, explaining the absent detailed historical trace; a narrow correction of that existing nonsecret diagnostic key is included in the pending guarded rollout review. No new inference or backlog admission occurred.


### 01:03 UTC reviewed phase correction deployed

The exact three-file source from commit `283b2134d80a653445d585d553884ea232390d7f` is now serving after the independently reviewed four-file rollout (three Python modules and the existing nonsecret timing-account directive). Root verification ran 21 source/composition cases and 11 disposable rollout fault cases. The actual service Python 3.11 imports, class identity, serving hashes, both health routes and effective intended-owner trace setting passed. All six pre/post protected database hashes were identical; claims stayed closed and the execution hold/quarantine were preserved. No model call, backlog admission, credential, model, account route or n8n change occurred during this diagnostic/rollout pass.

The latest model result and canonical assistant event remain durable, but enhanced app writeback remains unproven. There are still six one-attempt quarantines and fourteen untouched records in the original 20-record set. The existing contract cannot safely turn the held result into a new active attempt without reconstructable frozen-context and attempt provenance; no such continuation was attempted. Exact historical post-insertion substage attribution remains unavailable because the old stage configuration selected another account. The corrected trace will distinguish future boundaries. No fresh inference was used merely to produce this diagnosis.


### 01:13 UTC execution-capacity settlement and next-source preflight

Independently reviewed exact resource cleanup committed at 01:10:18 UTC after actual old-executor closure and retained terminal-generator/canonical-event evidence. Only capacity and its audit changed: the failed job, tombstone, account, source, stock and retained output were preserved; claims remained closed. The consumed qualification permit was archived at 01:11:55 without issuing a replacement. Five existing resource-CAS cases and six focused existing retirement cases were reused for review; these are not additional novel product tests.

A fresh untouched original-owner record passed the unchanged exact-source repair planner with zero previous attempts and original stock/transcript pins. Fresh transport preflight at 01:13:17 verified original owner/profile, existing OpenRouter model configuration, serving module pins, account-specific tracing and both health routes. No source preparation, new model permit, provider inference or quarantine replay occurred. Counts remain fourteen untouched, six quarantined and zero enhanced in the original recovery set. Mini inference is held pending coordination with the active Base native build, not an account permission or capture-stop gate. One actual enhanced writeback and correct-agent recall remain required before backlog admission.


### 01:33 UTC controlled OpenRouter test terminal; retained quote grounding rejection

The Base owner reserved the inference window; fresh named-process checks found all eight recorded Base workers and the named SDK/phone roles absent. One previously untouched saved record was prepared and issued an exact single-job permit. The worker consumed it, closed claims and ran one attempt under the original account/profile. No other record or quarantine was replayed.

The attempt ended at 01:25:50 UTC after generator completion and durable generator-ledger provenance, before verifier START. Trace distinguishes the previously opaque phases: initial sync 52.498 s; two legacy snapshot reads 33.077/33.737 s; no-learning 1.339 s; generator pre-model authority 52.193 s, model HTTP 10.024 s, pre-canonical authority 53.052 s, canonical persistence 10 ms, exact readback 1 ms and post-readback authority 4.197 s. The actual bridge completed in 120.123 s. The producer then raised ContractError; this attempt did not reach the overall timeout.

A backend-only pure validation of the exact retained generator result and verified original canonical source reproduced the closed transcript contract code `evidence_quote`. Candidate object keys/schema2/outcome were valid. No model or transcript text was exported and no new inference was used for this diagnosis. Original exception arguments were not logged, so this reproduction is not falsely presented as a retained original exception message. Wrapper freshness and subsequent contextual checks remain separate seams. The quote predicate must be classified and corrected without accepting ungrounded quotations or weakening verification.

The original recovery set is now thirteen untouched and seven one-attempt quarantines, zero enhanced writebacks. Original transcript, stock and routing remain preserved. Global active attempts are zero, claims closed and one execution-resource hold preserved pending reviewed settlement. No further record is admitted. The inference window is released; the Base builder may proceed. A separate seven-test source-only proposal removes two unused prelude snapshot reads for captured-scope callers; it was not deployed during this running test. Codex comparison and actual correct-agent recall remain unqualified.


### Quote-contract diagnosis and source-qualified correction (no new inference)

Metadata-only comparison of the held generator result establishes the concrete formatting mismatch: two transcript evidence quotes each omit one LF at a canonical blank-line speaker boundary. Each maps to exactly one source interval; all non-LF characters, case, punctuation and speaker labels are identical. Other three quotes are exact. Case-fold, Unicode or punctuation repair is not needed and is not accepted by the correction.

The [LF evidence projection package](../../backend/experiments/ella_quote_evidence_lf_v1/README.md) preserves raw generator output and creates a separate verifier copy containing original source substrings. Only unique nonempty literal-LF run-count differences can resolve; ambiguity or any non-LF change fails. A deterministic versioned source-span/hash proof is request-bound, and stored review reconstructs it from the raw durable result. Fresh policy revision includes the contract identifier. Shared strict validation, contextual additions, account/source/version/CAS and publication guards remain unchanged. No old held request is reinterpreted or published.

Nineteen pure cases and six independent cases passed; a published small standard-library fixture independently reproduced all 25 (19 in0.009s,6 in0.015s). Actual service Python3.11 imports passed with network blocked. A backend-only read test verified source and transcript hashes against the actual operation binding, resolved exactly two quotations, passed the unchanged strict transcript validator and left the raw generator hash unchanged. No transcript or model output text was exported. This transcript-only proof is not an actual contextual/verifier verdict, enhanced writeback or recall proof.

The quote correction is NOT deployed. Counts stay thirteen untouched/seven quarantined/zero enhanced. No new inference, retry or admission occurred. Rollout qualification, remaining context-read budget work and a later coordinated Mini slot are required before another paid run. The separate seven-case prelude source proposal remains separate; no source or storage migration is part of this change.


## October 25: quote-boundary and latency recovery checkpoint

At 02:11 UTC, the reviewed unique-LF quote correction from commit `35c793613beed3ea88400e903bc47856347fb08a` was deployed with exact serving hashes, network-blocked service imports, both health endpoints returning HTTP 200, and protected source/account/job/quarantine preimages unchanged. The completed generator had collapsed a double LF to one LF in two uniquely located evidence quotes; all other characters matched. Read-only projection onto the exact original source makes the strict transcript-core validator pass while preserving raw output. This does **not** establish the contextual verifier result or authorize applying a quarantined operation under a different request contract. Twenty-five quote cases and eight controller fault cases passed.

The two latency candidates published in `6676d8b7f5e992e62edb9459553554a3d1626e67` were subsequently deployed under the same scoped backend authority. The backend skips two legacy snapshots that were overwritten before use; the authoritative capture remains after learning and its admission fence remains current. The private host route removes a redundant authority fetch around a synchronous source copy; generic potentially blocking reads retain their double-fetch fence. Full capture still brackets protected reads and the record batch. Seven backend composition cases, twelve host cases, actual interpreter imports, and rollback-controller fixtures passed. Host health and unauthenticated route rejection passed. Call-count savings are proven; full end-to-end latency improvement has not yet been measured. Existing host/job/provider deadlines were not increased.

The same commit contains a separately qualified **undeployed** OAuth Codex `gpt-6.1-sol`/medium candidate. It fixes a source-level omission in reasoning-effort propagation through the existing Responses adapter. Eleven fake-transport cases pass; no new credential, grant, profile mapping, live model switch, or unlimited-use claim follows from those tests. The running route remains OpenRouter.

The authorized original saved-record cohort currently has thirteen untouched records and seven individually quarantined attempts, with zero verified enhanced app writebacks. One next record is selected by metadata only; it is unarmed. No backlog batch or held-result replay was issued. Real enhanced canonical readback and correct-agent recall remain release blockers. The next controlled inference requires fresh exact-source/authority preflight, reviewed execution-resource settlement, and coordination with the native build owner; maintenance itself does not require the user to stop recording.


### Controlled post-fix result, October 25 02:42 UTC

The consumed prior permit was archived and one previously untouched authorized saved record was prepared and admitted at 02:32:22 UTC after the native build settled. Exactly one attempt ran; other pending records were excluded. The generator completed with original-owner runtime and canonical-event provenance. The independent verifier did not start: strict candidate validation rejected one of four evidence quotes that was absent from the exact source. Diagnostic case, NFC/NFKC, whitespace and typography comparisons, including their tested combination, also failed to locate that quote. The unique-LF correction therefore cannot legitimately repair it; no new evidence tolerance was added. Original transcript and stock summary remain unchanged.

All recorded spans closed. Full operation elapsed 236.544 seconds; generator bridge 88.689 seconds, model HTTP call 6.352 seconds, and full authority checks approximately 38 seconds. No deadline fired. This single different-source run establishes actual post-fix timings, not a controlled performance benchmark or an end-to-end success. The cohort now has twelve untouched records, eight quarantined attempts and zero verified enhanced writebacks. The inference window was released; no next inference was submitted.

Codex preparation exposed a further concrete migration seam: changing a profile model alone conflicts with the provisioner's global model policy and normal ensure raises `runtime_policy_conflict`. Moreover, actual ensure removes model `base_url`, and normal-chat reasoning uses `agent.reasoning_effort`, separately from semantic-role policy. A safe model comparison therefore needs a coordinated server-public-policy and exact managed-profile migration in the normal ensure form, retaining the conflict guard, existing OAuth, owner/agent/workspace and rollback. The pure planner is not itself a live file writer or a product migration. No live Codex configuration or credentials have changed.


### Codex migration and attempt-budget checkpoint, October 25 03:45 UTC

The attempt-aligned host budget was deployed at 03:26 UTC after nineteen typed-budget/real coordinator composition cases, eight controller fault cases and a network-blocked service-interpreter import. Host reads now share the existing typed enclosing attempt deadline instead of independently expiring at 480 seconds; the enclosing 600-second maximum, existing provider/per-hop limits, authority identity, source scope and legacy default behavior remain. Both health endpoints returned 200 and protected data images were unchanged. Source and test archives are in [the budget package](../../backend/experiments/ella_attempt_host_budget_v1/README.md), published at `da20b6e5765b8df6ff9c1fd5c1b51927b6608f79`.

The current existing OAuth catalog was read without refresh and listed `gpt-6.1-sol` with medium reasoning. A coordinated model-only change to the two active managed Ella profiles and provisioning defaults succeeded at 03:42 UTC, with six exact installed images, fresh worker imports with network/private-file access blocked, healthy provisioning shim, and unchanged registry/manifest ownership. No inference was included. An earlier rollout stopped before installing files and rolled back healthy because a transient shutdown status was rejected; the independently reviewed successor keeps strict parsing and waits only within the existing shutdown deadline until explicit service absence and tracked process exit. Twenty-nine controller cases passed.

Fresh strict backend composition exposed one remaining migration seam before admission: the personal retained PostgreSQL binding still carries its old model-policy label. Every other compared owner/account/agent/profile/workspace/legacy-identity field matches. The separate lab binding already carries the target label and passes strict composition. A narrow revision-fenced update of only the personal binding's model-policy metadata is under review; the validator is not relaxed and the unrelated legacy SQLite account-ID discrepancy is not changed. The [coordinated planner source archive](../../backend/experiments/ella_codex_sol_v1/profile-cas/COORDINATED.README.md) preserves the successive normal-ensure/fallback corrections and 31-case source qualification.

No Codex inference, enhanced canonical writeback or correct-agent recall has yet passed. The original cohort remains twelve untouched/eight quarantined/zero enhanced. No failed job has been replayed by these maintenance changes. The remaining sequence is strict binding composition, reviewed resource/permit settlement, one coordinated eligible saved-record test, then durable writeback and recall before any backlog batch.


### First live Codex outcome, October 25 04:18 UTC

The exact owned PostgreSQL policy-label alignment committed at 03:54 UTC, changing only the personal binding's policy label, revision and timestamp; both original account routes then passed full strict semantic composition. The existing lab binding was unchanged. The [generic alignment helper and fixtures](../../backend/experiments/ella_codex_sol_v1/binding-policy/README.md) are published at `f8fd15246c5f3773ddc2e51f1641f1a0d92ed238`.

One previously untouched saved record was admitted at 04:01:29 UTC under the reviewed exact-one permit. Codex generator and verifier both completed with distinct runtime/canonical-event provenance. Metadata-only readback confirms accepted, source-grounded, semantic-summary and context-owner-verified verdict flags and a durable server-issued publication proposal. This is the first live success of those model stages in this recovery sequence; it is not an enhanced app writeback or recall success.

The enclosing operation timed out at exactly 600 seconds during application. That phase consumed 225.932 seconds, repeatedly validating captured companion context; the full trace contains fourteen host-context spans. No new summary version or writeback receipt was committed. Original transcript and stock version remain unchanged, the accepted proposal/results are preserved, and the job is individually quarantined. The original cohort now has eleven untouched records, nine quarantined attempts and zero verified enhanced writebacks. General claims are closed; there is no active model worker. Both backend health endpoints and the configured Codex provisioning shim remain healthy.

Source composition identifies nested validation amplification: each current host captured-context validation entails eight full semantic-runtime samples, which expand to twenty-four ordinary runtime resolutions and eight model-authority RPCs, before the remaining consent/source/record/attempt checks. Exact substage wall-time attribution remains unmeasured. The next isolated source correction targets redundant nested sampling while retaining all before/after owner, consent, source/version, referenced-record edit/deletion, model and attempt fences. No TTL authority cache, deadline increase, blind quarantine replay, next saved-record inference or recall question was performed in response to this result. The separately requested continued repair remains authorized; a supported application-only continuation is being investigated before paying for inference again.


### Nested-validation correction deployed, October 25 04:43 UTC

The [minimal three-file factoring patch](../../backend/experiments/ella_captured_context_factoring_v2/README.md), published in `c5d3fec06fb8a75b0074a3822a9a4f4f1d6a1118`, now serves from the existing backend unit. Actual-function baseline/candidate fixtures verify 24→12 ordinary runtime resolutions and 8→6 model-authority metadata RPCs per composed validation. The public authority path retains double sampling; only the internally bracketed record batch uses one complete sample at each boundary. The immediately preceding fresh runtime supplies the before-model sample; exact after-model revalidation, final consent, source/version, referenced-record edit/deletion and attempt checks remain. No TTL authority cache or deadline increase was introduced.

Thirteen boundary/count cases, network-blocked imports under the actual service Python 3.11 interpreter, seventeen source/guard cases and eleven meaningful rollout fault cases passed (suite reports overlap and are not a summed unique-case count). The rollout verified twenty selected import-module hashes and both health endpoints. Nine exact original-cohort quarantine images, the accepted prior proposal, and protected source/account/tenant/job/policy images were unchanged. It changed no profile, model setting, consent, credential, n8n workflow or capture state. No lossless active-capture drain is claimed.

The prior proposal has no accepted summary-writeback token/receipt. Source review confirms the existing reconciliation path only replays an already-accepted CAS; it cannot admit a new mutation for a closed v2 attempt. A fresh worker gets an empty captured scope, while a fresh capture has a different attempt/sidecar binding. No old lease or publication binding was rearmed or forged. The proposal remains preserved. One never-attempted eligible record has passed source/stock/version/owner preflight and is prepared with claims closed. Its single permit remains unissued pending the actual terminal and cleanup of the separately coordinated short Thin SDK test. Durable application and recall acceptance remain open; no extra backlog record has been consumed.

### Post-factoring controlled Codex outcome, October 25 05:19 UTC

One reviewed exact-record permit was issued at 05:08:08 UTC after actual SDK/process settlement. The original personal account/profile/route, source hash and stock version matched. Generator and verifier both completed with distinct runtime provenance and the server retained a publication proposal. No other record was admitted; general claims closed when the one attempt started.

The attempt failed during application at 05:17:20 UTC: 544.517 seconds overall, including 235.842 seconds in application. All 236 recorded stage events form closed spans. This was not the enclosing 600-second timeout. The stored causal diagnostic labels three nested exceptions `OtherException`; a bounded terminal-window log scan supplied no additional known exception label. The exact inner exception therefore remains unknown. No enhanced version or accepted writeback receipt exists. The original transcript and stock summary remain preserved, as do the generated proposal and quarantine history. The authorized original cohort is now ten untouched, ten quarantined and zero verified enhanced writebacks. Both health endpoints returned 200 at 05:19:29 UTC; active attempts were zero. One unresolved execution reservation remains held, and no settlement or replay was performed.

Source inspection establishes another concrete application defect: the new-apply path performs eight expensive context hydrations. Two occur in the application preflight, and six occur after the Firestore transaction snapshot read inside the update-builder callback (two in each of the before guard, direct publication check, and after guard). The eight approximately 28–30 second context spans closely explain the application duration. Remote context work inside the retryable transaction callback is demonstrated by source placement; attributing this particular masked failure to a Firestore transaction deadline is still a hypothesis. The earlier thirteen-case factoring suite did not cover this complete transaction composition, so it was insufficient as end-to-end acceptance.

Further model calls and untouched-record admissions are held. The next correction must move remote work outside the transaction while retaining transaction-time owner/source/version, consent, attempt and referenced-record edit/deletion fences, with actual-class composition and race tests. No authority cache, partial-response acceptance, deadline increase, model fallback, identity remapping or fabricated continuation is an acceptable substitute. The Mini model subprocesses have settled and the shared inference window is released. Agent recall was not attempted because durable enhanced writeback has not passed.

### Transaction callback correction deployed, October 25 05:52 UTC

The [reviewed five-file application overlay and exact baseline](../../backend/experiments/ella_transaction_context_v1/README.md) are published at `31f871d2008463caa0fa022ed560e9f36538f46f`. Remote context/runtime work is moved to preflight, bracketed by identical exact source/reference images and timestamps. The actual Firestore callback reads current consent+receipt, source/version/owner, attempt/account graph and frozen reference pins in its transaction, then runs local publication checks before writes. Two host context reads remain outside; six are removed from the new-mutation callback. No production speedup is yet claimed.

Independent review caught and corrected late exact accepted-CAS races: the existing replay classifier remains reachable without a second mutation, while wrong tokens/owners/CIDs, unavailable sources and revoked consent reject. Current consent is checked again immediately before normal and supersession canonical effects on the semantic path. Revocation after a source CAS preserves the accepted version and pending receipt and prevents the external call. Firestore transaction checks are atomic within that store; host/PG/model sampling and the final-consent-check to external-commit gap are explicitly not distributed-atomic.

Thirty-three synthetic application cases and nine actual SDK-decorator cases passed and were independently rerun. The latter also passed in service Python3.11/Firestore2.17. Eleven closed-diagnostic tests passed, and seventeen filesystem/controller faults passed, including foreign dependency/file protection and durable rollback of the new helper. The application tests execute actual APPLY/publication/writer/builder code with explicit IO doubles; they are not real Firestore/emulator or real-memory writeback proof.

Deployment completed at 05:52:42 UTC with all six reviewed serving hashes (five application files plus the closed diagnostic labels), twenty-seven selected module/type import checks and both health endpoints200. Protected policy/job/source/account/tenant/tombstone images and ten quarantine fingerprints were unchanged; no model call, queue reopening, credential change or n8n mutation occurred. Fresh read-only inspection confirmed the old executor absent. Its exact execution reservation was subsequently settled with reviewed resource-only CAS, keeping the failed job/output quarantined and claims closed. The original cohort remains ten untouched/ten quarantined/zero verified enhanced. One controlled new test follows only after fresh source/route/consent and shared inference-slot checks; no existing quarantine is being replayed.

### 2026-10-25 07:04 UTC: strict pronoun-contract correction deployed

The next live model response completed, but strict local validation rejected unsupported gender-pronoun binding before verification or APPLY. This does not demonstrate failure of the deployed transaction callback correction. Content-free retained-output inspection reproduced that exact guard. A broader semantic false positive was not established, so grounding validators remain unchanged.

The reviewed correction requests neutral assertions, preserves evidence quotes, and allows at most one separately ledgered corrective generation after an immutable completed response fails that specific local guard. It retains same-attempt owner/source/context and budget fences, rejects unknown outcomes and does not reopen quarantines. Fifty-five offline cases, 28 network-blocked real-service imports, and 18 deployment-controller fault cases passed. Exact sources and fixtures are archived in [the correction package](../../backend/experiments/ella_semantic_correction_v1/README.md). Actual deployment health and protected-state checks passed; no enhanced live writeback or recall is claimed. The authorized original saved cohort remains nine untouched, eleven quarantined and zero verified enhanced at this checkpoint.

### 2026-10-25 07:47 UTC: first independent durable enhancement readback passes

One controlled saved record completed generator and verifier execution with distinct retained provenance, committed a new Hermes-enriched version, and produced completed writeback and canonical receipts on the original account route. Independent current-version canonical retrieval and app-model projection then passed; transcript and original stock version were preserved. No other backlog record was dispatched in this test.

The independent reader initially stopped at a presentation-hash check. Read-only stored-field comparison showed the existing writer's exact sanitizer projection added the product overview prefix; it did not change ownership or routing. The corrected reader verifies original accepted proposal/payload provenance separately from exact sanitized presentation, retaining strict tamper and version checks. See [readback source and evidence](../../backend/experiments/ella_completed_readback_projection_v2/README.md). Thirty focused offline cases passed; no regeneration or production mutation was needed for this reader correction.

At this checkpoint the authorized original saved cohort has one independently verified enhanced record, eight zero-attempt records and eleven preserved quarantines. Separate-turn agent recall and complete backlog recovery remain open. A successful server/app projection is not physical-phone visibility or iMessage-session qualification.

### 2026-10-25 08:05 UTC: separate-turn owner-bound RAG check passes

The same completed memory was retrieved again from canonical storage and supplied as untrusted data to one new turn on the original account's normal agent session. The terminal answer matched the exact current title; original route/source/version remained unchanged afterward. The initial probe had placed context in an earlier user message, which the existing persistent-session gateway intentionally replaces with stored history. The corrected probe puts data and question in the current user turn while retaining trusted instructions separately. Actual parser/controller fixtures passed seventeen offline cases. See [source and exact limits](../../backend/experiments/ella_current_turn_context_probe_v1/README.md).

This is a supplied-context retrieval-augmented result, not autonomous archive search or iMessage qualification. Effective normal-chat provider/model is not exposed by this gateway result. The original cohort remains one verified enhanced memory, eight zero-attempt records and eleven preserved quarantines; further backlog execution is not implied by this check. The enrichment operation took 387.277 seconds with all 230 recorded spans closed. Final read-only health remained 200 on both routes, with zero active attempts/resources and claims closed.
