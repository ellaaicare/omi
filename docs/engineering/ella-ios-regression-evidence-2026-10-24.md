# Ella iOS regression evidence and architecture decision — 2026-10-24

**Review draft for Claude; documentation-only, not a release qualification.** Evidence cutoff: 2026-10-24 07:31 UTC, with publication metadata checked separately. Repository path: `docs/engineering/ella-ios-regression-evidence-2026-10-24.md` in the verified existing Ella code fork. Backend owner leads the proposed [master analysis](https://github.com/ellaaicare/omi/blob/9647c3a99a1591d8ad01376b8abce6b8f6f42580/docs/engineering/ella-regression-analysis-2026-10-24.md); infra/storage/deployment belongs in the infra owner's separate appendix. The backend master link above is an independently reported verified remote commit. Other appendix links remain pending their owners; do not infer unpublished branches or deployments.

No credentials, account emails, device identifiers, raw transcripts or user recording payloads belong in this document. Source snapshots, tool outputs and dated physical observations are distinguished below. User reports and hypotheses are not promoted to measured facts.

## Immediate state and ownership

- Existing delivered reference builds: Main 1.0.572(967) and Thin 0.1.0(30). Their source snapshots, signed IPAs and symbols remain on the Samsung T3 SSD, whose mount name remains `/Volumes/External T1`.
- QA31 is a **local-only journal/channel test candidate**, not a replacement capture app or a delivered user improvement. Its signed IPA is qualified locally. No Apple upload, assignment, installation or runtime result is established.
- New canonical-source adapter: default OFF, unintegrated, source-reviewed in a thread; **34 new Dart cases remain unrun**. Existing HTTP detail reads do not supply its MCP canonical contract. No credential bridge or invented HTTP query is permitted.
- Storage guard: prospective source and 27 injected Python cases passed; not hooked into a launcher and not actual storage readiness. The reviewed revision still uses diskutil for live metadata, while the infrastructure session reports that service unavailable and a Foundation metadata route working. Any reader change requires source/test review before activation.
- Current instruction: storage moves/copies/cutover/cleanup and new large writes are stopped. Actual external Atlas on Telegram has not been dispatched by this builder. Earlier references calling Codex infrastructure session `01a12c02-8e70-71c8-b0b2-b956c2654005` “Atlas” were incorrect. Preserve its recorded effects; do not undo or infer external-Atlas ownership.
- No builder local Git clone, fetch, checkout or index mutation is part of this document-only publication. Small Git-data API objects publish the isolated review branch without a large workspace copy; verify its remote reference and content before reporting success. Backend and infrastructure publication have separate owners. No shared dirty checkout may be switched or reset.

## What the source-control evidence actually shows

Read-only `ls-remote` at 2026-10-24 07:21:59 UTC:

| Reference | Exact commit |
|---|---|
| `ellaaicare/omi` default `main` | `b43b164828137dfdbb4d2bc0a315b11c7bd7bb3b` |
| `BasedHardware/omi` default `main` | `699040f2223c9167e2fd264ac9e92fe2d62a65e5` |

The fork tip's commit date is 2026-09-27. Its object exists in the local internal Git store. The current upstream tip object is absent in the two inspected stores. **Current merge-base and ahead/behind counts are not yet established.** Do not substitute an old remote-tracking ref for the current upstream. Fetch only when the external disk owner permits a bounded operation and the new Git store/index is safely isolated. BasedHardware is fetch-only; all code/PR publication targets the existing verified Ella fork, never upstream.

Main967 physical source snapshot, `lib` only, compared by Git blob IDs with exact fork `main` above:

| Metric | Count |
|---|---:|
| Fork `app/lib` files | 668 |
| Artifact `lib` files | 995 |
| Byte-identical files | 454 |
| Added artifact paths | 328 |
| Modified shared paths | 213 |
| Removed fork paths | 1 |

Among added/modified paths: 150 under `lib/ella`, 169 under `lib/upstream_capture`, 19 providers, 38 services, 35 pages and 16 backend-client files. Sixty-nine localization files are included in the totals. These are **drift counts**, not 541 bugs, not a complexity score, and not proof no review branch already contains the work. Local review refs include relevant capture/runtime changes dated October6. All refs and dirty work must be reconciled before staging.

The tested source snapshots contain no `.git`. Thin30's separate Flutter composition has no verified authoritative Git remote/subtree in the inspected evidence. Reused OMI connector code does not establish Thin publication ownership. Do not initialize a new public repository or invent a monorepo subtree without resolving that mapping.

Resident T3 OMI clone `worktrees/omi-contract-sweep-jack.RgDcsa` is an old July checkout with seven unrelated modified backend files. Its Git store is on T3. Many other T3 worktrees instead point to `~/dev/omi/.git` internally. Infrastructure's `ella-ai-maya` clone has a modified `CLAUDE.md`. Preserve all such work. Planned iOS branch is `review/ella-ios-reconciliation-20261024`; the branch publication handoff must identify the verified remote commit; application-source reconciliation is not complete.

## Verified artifact lineage and bounded recent timeline

Evidence is limited to the retained recent work. The user's report that enhancement and whispers worked in earlier versions is significant, but **an exact last-working app/backend/config tuple and a year-long causal timeline have not been established by this builder**. Backend/infra owners must join deployed commits, configuration and real outcomes before naming the last good baseline.

| Observation | What is established | What is not established |
|---|---|---|
| Main962 user incident, Oct20/21 | Transcript reached backend finalization; a short stock result was classified discarded/trivial. Missing accessible Finish and poor transcript presentation were real user reports corroborated by UI/source inspection. | A missing finalization POST was not the cause of that particular finalized record. No enhanced memory was proved. |
| Main963/964 source work | Earlier UX/session/Create-memory changes had 32 focused cases; diagnostics had a separate 20-case result. Signed/compiled evidence and Apple availability are separate milestones. | Diagnostics tests do not establish BLE capture or resolve setup/account-state failures. Some later fixes were not included in these frozen builds. |
| Main965 differential discovery | Same designated lab phone displayed one exact Friend row after Thin27 had shown none. | Row origin could be advertisement, retained list or retrieval; no codec or fresh-advertisement proof from that row alone. |
| Main966 lab episode, Oct23 about02:16–02:25 UTC | Exact Friend selected; backend joined Opus16k/protocol2; synthetic speech produced visible fresh STT. One Create memory action produced a non-discarded stock record and matching version visible in Journal. | Enhancement failed/quarantined; background/reconnect was not qualified. This was lab evidence, not Greg's personal-account acceptance. |
| Same Main966 episode | An already-open transcript sheet showed unavailable across rollover; reopening showed current STT. | This did not prove Bluetooth or transcription had failed. It is evidence of stale presentation/generation handling. |
| Main966/967 setup episodes | Setup/error surfaces could block ordinary Stop/Disconnect while Listening was visible. One later ordinary Disconnect completed when setup recovered. | Absence of Mirroring alone never proved the producer quiet; no explicit drain ACK was inferred from final UI. |
| Main967 physical install | Official version installed; account/navigation retained; disconnected/inactive state was observed. | Active-producer Stop escape, full capture/background/enhancement were not qualified by that inactive pass. A later single selection did not reach Ready; power/advertiser freshness were unverified. |
| Thin25/27/29 | Early scans rejected an unknown no-signature candidate; that candidate was never proved Friend. Thin29 later displayed one exact Friend row. | No anonymous-device admission was justified. Thin29 did not select/capture because Main ownership was unresolved. |
| Thin29→30 | Successful picker diagnostics visibility changed in one Dart file. Official Thin30 availability was verified. | Actual Thin30 installation/capture/enhancement remained unqualified when the UI permission path blocked inspection. |
| QA31, Oct23 | 68 injected Dart +27 Foundation cases passed; unsigned enabled channel composition/AOT compiled; local signing preserved code and symbols. | No installed Firebase/current-account/native-channel/iOS sandbox durability/Drive/provider result. |

Exact snapshot delta Main966→Main967 is seven `lib` paths:

- `ella/pages/ella_provisioning_gate_page.dart`
- `ella/upstream_capture/ella_existing_capture_stop.dart` (added)
- `ella/upstream_capture/ella_upstream_capture_dock.dart`
- `ella/upstream_capture/ella_upstream_capture_runtime.dart`
- `ella/upstream_capture/transcript_presentation_lease.dart` (added)
- `upstream_capture/services/capture/capture_controller.dart`
- `upstream_capture/services/capture/capture_coordinator.dart`

Thin29→Thin30 `lib`/Runner delta is only `lib/src/test_device_picker.dart`. These are verified file-hash comparisons, **not reconstructed Git commits**.

Retained IPAs:

| Build | SHA256 |
|---|---|
| Main967 | `2388667bb992b63bfeef443f1fe6e864d816f7ed2d0a52be5d8afd714a11a474` |
| Thin30 | `5e3061088cf6eaf8d387bb20507023172d9640a6e98f09308105325e7b18185f` |
| QA31 local only | `61c224935e16907664ced42fdc9728d482c32c59fca96d24ac1956a1dff6083f` |

All passed ZIP integrity and matched retained handoffs; three binary/dSYM UUID pairs matched for each. Source snapshots were resident, not dataless placeholders. This preserves recovery material, not all Git history or all Documents control files.

## Coupling and regression mechanisms to review

### Capture, lifecycle and UI

The shipping Main entrypoint is `main_upstream_capture.dart` (49 lines). The artifact also includes the broader OMI tree. Presence of two trees is **not proof both producers initialize**; trace the selected entrypoint and native registrations. Measured core sizes are runtime1845, dock2363, provisioning page421, controller3974 and coordinator3064 lines. Size alone is not an architectural verdict, but these are concrete review boundaries.

Required invariants:

1. Successful **authorized** necklace connection starts recording automatically. Reopen/reconnect resumes without a persistent accidental pause latch, while preserving OS permissions, owner and consent gates.
2. Exactly one producer owns a capture generation. Connection status, STT readiness and presentation readiness are distinct facts.
3. User Stop remains reachable during retryable setup/auth overlays. Stopping an existing owned producer must not grant authority for a new one or cross accounts.
4. Manual Create/Finish closes the intended segment idempotently and exposes its real saved/discarded/unknown outcome. It must not silently discard or duplicate source.
5. A sheet held over CID/generation change must retire/refresh safely; current audio cannot be described as unavailable merely because the viewer lease is stale.
6. Background/foreground and upgrade must preserve source and account state. Retaining an authenticated shell during nonterminal revalidation does not mean fail-open recording.

### Account authority and enrichment queue

Backend owner reports 17 isolated tests passing, without integrated/deployed qualification, and this source finding: mutable quarantine history was included in an account fingerprint later admissions required to match, and historical quarantines consumed capacity. This is a backend source finding; see the master's exact code/tests/deployed evidence. Do not claim it fixed live jobs merely because a new source suite passes.

The one-off account-repair helper's capture-stop prerequisite must not become a permanent architectural dependency of ordinary enhancement. Immutable account/route authority, per-job failure history and capture ownership need separate boundaries. One rejected/unknown job must remain isolated without silently disabling healthy jobs, changing routing or promoting quarantine to success.

### Thin canonical source and Drive/Muse boundary

The source-qualified default-OFF follow-on distinguishes raw stored-order canonical text from filtered/newline display text; binds original owner/receipt/epoch/nativeCID/version/snapshot; fences pre/post awaits and queue handoff. Thirty-four new fake-port cases remain unrun. Its source owner's Claude handoff and HTTP compatibility note are separate documents to cross-link when the authoritative Thin repo is resolved.

Primary backend source receipt says the retained router matched serving hash prefix `9f15108c` at its recorded pin. Exact-CID HTTP GET returns `Conversation`, requires current Thin authority/scope, and rejects mismatch409. It has **no enrichment-preconditions query or canonical MCP projection capability**. A retained MCP argument shape is not an HTTP API, nor authority to reuse a credential. Use a separately verified existing authorized MCP port or a separately reviewed backend extension; no header stripping, account remap or Hermes fallback.

Live checkpoint applies negotiated LC3 live transport; Opus is batch. Current batch unknown upload outcome retains immutable source and has no automatic reupload/result lookup. Do not advertise uninterrupted Finish on unsupported hardware. A close/drain/save/fresh-start flow has an interruption and must say so.

### Whispers

Older working whispers are a historical user report here. The recent physical episodes deliberately retained Whispers OFF; source flags or offline tests are not new delivery proof. Identify the last good app/backend/tool/voice/account tuple from deployment and accepted output evidence. Future acceptance must prove actual target/version/owner, one delivery, audible/visible result where appropriate, and no source-account crossing. Never activate whispers or replay old output merely to fill an evidence gap.

## Workflow defects, without turning safeguards into the product

- Active control/manifests remained in iCloud Documents even after bulk compiler output moved to T3. The earlier routing policy was future-only; backup receipts explicitly recorded no cutover. Tool relocation later completed, but old launchers and dependency URIs were not yet rebound. This is unfinished migration work.
- Fragmented staged source/app/backend/config/review branches made code-versus-deployed identity hard to establish. Every release needs a commit, reviewed config/feature manifest, artifact hash and actual owner-bound acceptance joined in one durable repo-native ledger.
- Bespoke per-worker compiler argv/shell classification repeatedly stopped supported builds. Standard xcodebuild supervision replaced that semantic parser. Safety should retain real source/resource/exclusivity and owned-PGID limits without treating normal tool spelling as an access boundary.
- Native compilers returned success but a persistent ibtoold outlived the settlement window. Preserve raw wrapper HOLD and qualify artifact plus later natural absence additively; do not rebuild unchanged code for that reason alone.
- Avoid discovering project assets, missing imports, fixture syntax and log-handle bugs one failed execution at a time. Verify the declared project closure and exercise the bounded wrapper lifecycle before costly signing/build work.
- Reserve actual remaining stage work, not a whole compiler forecast during tiny validation/upload phases. Preserve real floors, caps, original histories and unknown observations; never reset accounting to manufacture fit.
- Incremental releases can carry verified improvements while clearly declaring missing enhancement/background proof. Upload success alone is not product acceptance; QA-only builds must not leak into user tester groups. Unassigned uploads may still be visible to eligible App Store Connect users.
- Approval rejection and uncertain tool outcomes must be recorded precisely and reconciled before retries. Standing user approval does not justify bypassing an actual unresolved action review. Conversely, optional handoff/status work should not block independent authorized source work.

## Architecture decision: repair, minimal upstream integration, or rewrite

Do not choose by sunk cost, file count or frustration alone. Do not assume a clean rewrite is safer.

| Path | Evidence favoring it | Required proof and cost/risk |
|---|---|---|
| Repair in place | Main966 proved the physical transport→STT→manual stock-save path. Concrete UI lease/Stop and backend queue/account predicates are isolated fix candidates. Existing source/data/identities survive. | Prove one healthy automatic enhancement after an isolated failed job, correct-agent writeback and full lifecycle acceptance. Audit active startup graph; quantify upstream drift. Do not retain unexplained global coupling merely to avoid change. |
| Minimal integration on current BasedHardware/OMI | Can remove duplicated capture/lifecycle ownership and keep custom enhancement behind a narrow contract if the current upstream hardware path is demonstrably better. | Establish exact current upstream merge-base and API/codec differences first. Preserve account/CID/version/encrypted source semantics. A separately testable adapter must meet current app/backend protocol and background behavior before replacing a working path. Current upstream does not provide Ella enhancement. |
| Clean rewrite | Appropriate only if measured boundary/integration tests show repair or extraction cannot meet invariants with bounded complexity, or unsafe coupling/data architecture cannot be isolated. | Largest unknowns: hardware/background regressions, account/session and pending-source migration, provider contracts, time to real acceptance. Requires explicit design/migration approval; no deletion, reset or rewrite execution is authorized by this assessment. |

Provisional direction: continue the smallest independently verified queue/UI repair while measuring a minimal upstream integration option. Revisit that decision using real failed-job isolation, physical lifecycle and upstream graph evidence. No wholesale replacement is justified by the current evidence alone, and no existing subsystem is exempt from replacement if its boundaries fail those tests.

Minimum independently testable architecture:

1. Pinned upstream transport/discovery/GATT/codec layer with one explicit capture owner/generation.
2. Immutable authenticated owner/consent/route context separate from job outcomes and UI readiness.
3. Durable transcript/segment boundary and idempotent manual save; unknown outcomes preserve source and operation identity.
4. Enhancement job transaction over exact canonical source/version/hash; per-job failure isolation and correct-owner canonical writeback.
5. Read-only presentation leases bound to current generation/version, with truthful saved/discarded/unknown/enhancement state.
6. Versioned delivery/config/toolchain manifests on T3 and in Git, with real-device acceptance separate from build/Apple milestones.

Migration/rollback requirements: keep existing signed Main967/Thin30 and all source/symbol/evidence; preserve app identities, account sessions, encrypted source, CID/version history and durable unknown operations. No automatic replay/regrant/remap during upgrade. Use an explicitly inactive/saved boundary for app replacement, verified rollback compatibility for data schemas, and target only the intended testing route. Do not expose a QA-only replacement through a shared Apple account/group.

## Concrete acceptance matrix for Claude and the implementation owners

| Layer | Minimum independently observable result |
|---|---|
| Source identity | Exact repo commit/branch/config matches built artifact; no untracked source-only delta silently treated as deployed. |
| Wrong account/retirement | UID/receipt/epoch/generation changes before and after awaits hide late results, prevent later effects and never route to another agent. |
| Capture | Exact known Friend, actual codec/services/packet arrival, autostart after authorized connection, one producer, truthful UI state. |
| Manual memory | Repeated taps/response loss produce one intended operation; nonempty short source can be explicitly preserved; empty source remains protected; exact CID/version saved read is visible. |
| Continuous use | Fresh audio/STT after manual memory, background/return and reconnect; no accidental persistent pause or repeated setup/login takeover. |
| Queue recovery | Healthy job advances after a failed/quarantined job; unknown stays unknown; bounded capacity is released only on safe terminal facts; no unauthorized replay. |
| Enhancement | New distinctive consented source reaches the user's own intended agent, accepted result is canonically written back and visible/recallable under the exact owner/version. |
| Upgrade/rollback | Source/unknown operations/account state survive a controlled update; no automatic reupload, regrant or migration across namespace. |
| Whispers | Real accepted correct-target output/delivery, independently bound to account/session; historical reports or enabled flags alone insufficient. |
| Thin canonical bridge | Either actual authorized MCP contract or reviewed server extension; current Conversation-only GET is explicitly incompatible. |
| Storage | Verified external source/tool/control/cache bindings; wrong/missing mount blocks; no internal/iCloud fallback; required physical floors and existing phase accounting. |

## Publication and next evidence gaps

The repository's rules require existing fork/issue boundaries, guardrail setup and relevant tests before code commits. `app/test.sh` has not run for a new reconciled branch; SDK tests are held by the protected internal floor and storage ownership pause. A document-only review does not imply app-code test clearance.

Remaining: external Atlas takeover and finite source/Git write plan; authoritative Thin repo/subtree; fresh upstream objects/merge-base; isolated review worktree with dirty work preserved; relevant source reconciliation/secrets audit/tests; draft PRs and verified remote SHAs/CI. No force push, merge, upstream publication or new repository identity. The doc must accompany actual fixes, not replace them.

## Scope of this review publication

This branch adds only this sanitized appendix. It does not change app/backend code, workflow files, credentials, tester access or live storage. No SDK/CI workflow is manually dispatched. The code reconciliation, authoritative Thin mapping, required app tests, upstream merge-base calculation and actual storage/capture/enhancement acceptance remain open. No local dirty checkout/index is touched. Backend source fixes and their tests remain owned by the backend branch.

## Reconciliation of Claude's final external architecture assessment

The final local review was received on 2026-10-24: 16,940 bytes, SHA256 `f4b09f5cf2b5d53485ad87e92a96128fb1fe0f78fdfd4debbdafed8c8b18071a`. This section summarizes that external assessment; it is not an independently executed backend audit or a deployment approval. Its original Drive task remains open because the reviewer reported expired Drive write access; no formal Drive reply was uploaded. No credential repair is implied.

Claude recommends **repair in place after G0**, deferring a minimal upstream re-port and rejecting a wholesale rewrite for now. G0 means an exact serving-source snapshot in Git, a per-file SHA256 manifest and import/dependency closure, plus an artifact-to-source reconciliation for each installed app. These are deliverables still required, not completed by this documentation branch. A directory digest and a git-archive digest must use the same explicitly defined canonical inventory/serialization before equality is meaningful; comparing unlike archive formats is insufficient.

The external findings map to the existing implementation boundaries as follows:

| Finding | Required boundary / iOS implication | Evidence limit |
|---|---|---|
| B1: unversioned serving baseline | Commit exact backend serving closure and reconcile Main967/Thin30 artifact source/config to reviewable commits. Preserve original snapshots. | This branch remains documentation only; no serving snapshot or iOS code reconciliation is claimed. |
| B2: mutable job quarantine affects account authority | Keep immutable owner/route/consent/epoch authority separate from per-job outcomes. Queue failures must not become setup/consent failures. | Backend-owned causal assessment; do not remove genuine auth failure checks to hide it. |
| B3: historical quarantines poison capacity | Distinguish live leases, bounded uncertain reservations and terminal history; require age alarms/operator resolution. | Never auto-release an uncertain external effect or replay it merely because a lease expires. |
| B4: claims pause exits worker | Maintenance should close claims while keeping worker/capture/source admission independently operable. | Proposed versioned maintenance policy is not deployed or enabled by this review. |
| B5: lease/attempt ABA and late effects | Require transactional phase/attempt/lease and owner/route/epoch fences at effect and writeback boundaries. | Offline pure-core results do not establish production call-site coverage. |
| B6: silent unavailable/result-label/empty-successor paths | Expose truthful not-attempted/unavailable state; contract-test backend labels against Main and Thin; retain the latest meaningful version. | Each app's actual read path must be tested; no shared-result support or fix is assumed. |

Claude proposes 13 integrated adversarial cases: quarantine isolation; historical/uncertain capacity; lease ABA; START-versus-close; unknown commit; owner/route change; manual-versus-automatic finalization; claims maintenance; scan starvation; unavailable replacement; cross-repo result labels; rollback policy; and foreign/retired authority rejection. **These are proposed tests, not 13 executed passes.** They supplement, rather than replace, physical capture/background/reconnect/Stop/upgrade acceptance and separate Whispers qualification. The referenced 17/42-test backend candidate needs an exact reviewable commit and separately reconciled actual results; this appendix does not resolve that count or infer integration coverage.

Measurement corrections and open decision inputs:

- Claude's Git counts, root-history explanation and GitHub comparison outcomes are external claims not independently reverified here. The verified local facts remain the pinned refs and artifact-versus-fork-main path counts above.
- A reported lack of common ancestry does not prove content integration or a deliberate unrelated-history merge impossible. A history rewrite is a hypothesis, not established causality. Do not base the architecture decision on an absolute impossibility claim.
- Counts against cached September 30 upstream cannot be called a lower bound on today's drift: current upstream content may converge or diverge. Fresh upstream graph/content measurement remains storage-gated; no fetch was performed for this reconciliation.
- Claude's effort ranges are low-confidence estimates, not delivery commitments. Likewise, its cited 30/50 GB storage policy is not adopted as a replacement for this build lane's existing explicit protected floors and cumulative accounting.
- Main966-to-967 changed both Ella-owned presentation/lifecycle code and vendored `upstream_capture` controller/coordinator paths. That identifies touched boundaries, not which upstream revision caused or fixed a physical defect. Map each failure to an exact failing/passing fixture and source delta before treating current-upstream re-port criterion as met.
- The authoritative Thin repository/subtree remains unresolved. Current Thin HTTP detail is Conversation-only and cannot be composed with the canonical MCP adapter by inventing query arguments or stripping authority headers.

Next implementation evidence remains: exact serving/import closure and app-source commits, transactional effect-call-site inventory, reviewed integrated tests, then a fresh mapped-account capture → stock save → own-agent enhancement → canonical writeback → visible version/recall slice, repeated with a sealed failed job present. Preserve source, sessions, account mappings, unknown operations and quarantine history. No replay, reseal, remap, policy activation, storage change, build or rollout was performed for this review update.

## Actual recovered application source publication, 2026-10-24

The prior documentation-only boundary has advanced: selected Thick app source now exists on the separate review branch `review/recovered-main966-baseline-20261024`.

- [Recovered Main966 selected source](https://github.com/ellaaicare/omi/commit/c88891c8d6aad321421d48fdcc6272f9d808c2ee), parented to verified historical lineage anchor `736332e5e9e9c85b7ff9e5683f47e6176fcd5de2`.
- [Recovered Main967 source delta](https://github.com/ellaaicare/omi/commit/5cd0c411c76bcb1efed619ad552852d3185bae78), parented to that recovered Main966 commit. Exactly seven product files, six tests/helpers and the pubspec version change were updated.
- [Recovery scope and manifests](https://github.com/ellaaicare/omi/tree/5cd0c411c76bcb1efed619ad552852d3185bae78/docs/engineering/recovered-ios) disclose selected source inputs, inherited/excluded paths and build-provenance limits.

Independent source/privacy review verified all 1,623 selected Main966 file hashes, modes and blob IDs, including 431 changed files totaling 12,753,726 bytes. New payload includes Firebase client configuration and host signing-path references; no private credential material was identified. It is not a zero-metadata payload. Remote tree verification matched all 1,630 selected Main967 file blob IDs and modes, and both commit parents and the final branch head were read back. No local checkout/index, original source, storage layout, credentials or release artifact changed.

These are **new recovery commits, not the historical build commits**. Generated localizations/serializers/plugin registration, host-generated Flutter configuration and unreviewed test images were omitted from new publication and hash-recorded; excluded paths already in the origin remain inherited and may be stale. Thus the selected source publication is complete, but the full compiled-source/artifact join and reproducible-build proof remain open. Derived/native/environment configuration must be validated before build/deployment claims. No SDK, `app/test.sh`, formatter or generation command ran under the explicit source-only task. At remote verification both commits had no check runs or status contexts; combined `pending` is not a test pass.

The authoritative Thin repository/subtree is still unknown. Its retained Thin29-plus-picker assembly is recorded rather than guessed into this repository. No provider replay, TestFlight action, data deletion, migration or current-upstream fetch accompanied these commits. The next restoration gate is the actual generated/native/config closure and permitted SDK/device acceptance, with backend own-agent enhancement repaired and independently accepted; documentation or source upload alone does not close it.
