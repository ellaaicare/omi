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

The serving overlay is **not fully represented by that Git base**. The deployed release directory is not itself a Git checkout. Reconciliation must explicitly record source hashes and import dependencies; copying a subset into Git and calling the repo current would be misleading.

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

The first implemented component is a pure attempt-fence transition core, outside serving code. Seventeen synthetic tests currently pass. Independent review found and then verified correction of persisted-state validation defects; its final verdict is **pure-core source PASS, not integrated**. Transaction adapters, remote acknowledgement validation, canonical completion, resource fences and migration remain unqualified. It is **not deployed or a complete queue fix**.

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

Transaction adapters, strict remote receipt validation, all effect-producing call sites and migration are still required. A pure state-machine test must not be represented as proof of those integrations.

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

The iOS owner is preparing [the iOS evidence appendix](ella-ios-regression-evidence-2026-10-24.md) on a separate branch. Until joined, that link is a reserved destination, not evidence the file exists on this branch.

Its current physical-snapshot evidence: Main966→967 differs in exactly seven lib paths (setup gate, capture Stop escape/dock/runtime, presentation lease, controller and coordinator). Thin29→30 differs only in `lib/src/test_device_picker.dart`. These are file-hash deltas, not Git commits or proof of installed behavior. Snapshot-to-fork lib comparison is 454 identical, 328 added, 213 modified and one removed; 69 changed/added localization files. Do not infer a fault count from this drift.

Atlas owns the infrastructure repository appendix and live deployment receipts. Its verified remote/path will be linked here once published. This backend branch does not mutate the infrastructure repository or claim an unverified publication.

## Serving-source pins for causal claims

| Module | SHA-256 |
| --- | --- |
| `database/normal_account_lifecycle.py` | `a9ddc5772772e8a914471de3ceaab0dc2e416ffeb3ff7b2aeeea429357f4f70d` |
| `database/normal_enrichment_activation.py` | `2e107eeee018de8e12c49dff5ffb3bbaf584cce4ffad3808179699892c268d63` |
| `database/hermes_cloud_enrichment_outbox.py` | `86bc5ab4096eb1db56d9ab42938b54a8c2ec7b400948c8702e567db935965382` |
| `ella/services/hermes_cloud_enrichment_outbox.py` | `571d3cf6099c0d857cf02287dc147cd29211800b59f86bcd0751399327507314` |
| `ella/services/summary_writeback.py` | `a3f09400b2a52fcc44cd735c121aa1b9fc8f668f46702ef6acf2cf9e7b4eb4dd` |

Key source locations in the pinned files: lifecycle `read_account` lines 192–209; activation `claim` capacity line 323 and `finish` quarantine updates around 454; worker `run_forever` around 542–547. Line numbers refer to the retained serving snapshot, not necessarily this branch's tree.

## Questions for Claude

1. Does the proposed separation preserve original identity/source authorization while removing accidental history-wide availability coupling?
2. Are all mutating host/model/learning call sites covered by the same transactional attempt fence, including delayed threads and callbacks?
3. Can versioned worker-only maintenance be delivered before changing the capture API, with truthful UI status?
4. Which minimal change demonstrates automatic enhanced memory in an existing mapped account without a fixture allowlist?
5. Does measured upstream divergence justify a preserved minimal rebaseline, or are bounded repairs demonstrably safer and faster?

Please distinguish source proof, retained receipt proof, user reports and hypotheses. Do not propose replay of uncertain jobs, account remapping, disabled owner checks or removal of preserved data as a recovery shortcut.
