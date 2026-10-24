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
