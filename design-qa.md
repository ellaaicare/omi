# Ella Design v2 QA

## Authority

- Product specification: `/Users/greg/repos/ella/ella-ai/docs/design/2026-07-20-app-design-spec-v2.md`
- Reference frames: `/Users/greg/repos/ella/ella-ai/docs/design/frames-2026-07-20/exports/ella-v2-*.png`
- Implementation branch: `feature/phase-a-testflight`
- Test device: iPhone 17 Pro simulator, iOS 26, 393 x 852 logical viewport

## Review Matrix

| Surface | Implementation | Side-by-side comparison | Result |
| --- | --- | --- | --- |
| A1 Today, normal | `app/docs/review/design-v2-2026-07-20/today-normal.png` | `app/docs/review/design-v2-2026-07-20/compare-a1-today-normal.png` | Pass |
| A2 Today, Whispers off | `app/docs/review/design-v2-2026-07-20/today-off.png` | `app/docs/review/design-v2-2026-07-20/compare-a2-today-off.png` | Pass |
| A3 Today, device banner | `app/docs/review/design-v2-2026-07-20/today-device-on.png` | `app/docs/review/design-v2-2026-07-20/compare-a3-today-device-on.png` | Pass with state note |
| A4 Today, enlarged text | `app/docs/review/design-v2-2026-07-20/today-150.png` | `app/docs/review/design-v2-2026-07-20/compare-a4-today-150.png` | Pass |
| B Full daily note | `app/docs/review/design-v2-2026-07-20/full-note.png` | `app/docs/review/design-v2-2026-07-20/compare-b-full-note.png` | Pass |
| C1 Whispers | `app/docs/review/design-v2-2026-07-20/whispers.png` | `app/docs/review/design-v2-2026-07-20/compare-c1-whispers.png` | Pass |
| C2 Whispers, empty | `app/docs/review/design-v2-2026-07-20/whispers-empty.png` | `app/docs/review/design-v2-2026-07-20/compare-c2-whispers-empty.png` | Pass |
| D Memories | `app/docs/review/design-v2-2026-07-20/memories.png` | `app/docs/review/design-v2-2026-07-20/compare-d-memories.png` | Pass with state note |
| E Chat | `app/docs/review/design-v2-2026-07-20/chat.png` | `app/docs/review/design-v2-2026-07-20/compare-e-chat.png` | Pass |
| F AI consent | `app/docs/review/design-v2-2026-07-20/consent.png` | `app/docs/review/design-v2-2026-07-20/compare-f-consent.png` | Pass |
| Voice | `app/docs/review/design-v2-2026-07-20/voice.png` | N/A | Pass |

The 120% and 150% text-size checks are captured in `today-120.png` and `today-150.png`. Content remains readable without horizontal clipping or control overlap.

## Iterations

1. Capped the normal Today note preview at six lines and added the required Read more action.
2. Changed the enlarged-text note preview to two complete sentences instead of clipping sentence content.
3. Replaced the chat fixture with the locked frame-E dialogue and matched the bubble proportions.
4. Replaced variable font defaults with explicit static Manrope and Fraunces instances so Flutter renders the specified weights consistently.
5. Removed full-note page chrome that was not present in the approved frame.
6. Corrected the Whispers empty state to use centered Manrope copy and a static breathing dot.

## State Notes

- A3: the disconnected-device banner, required ordering below the daily note, and capture navigation were exercised. The simulator could not enter active capture without a connected recording device; the live `Listening now - tap to view` state and destination remain wired to the existing capture provider.
- D: the active-conversation memory card uses that same capture provider and was verified in code. The non-capture memory state was exercised on the simulator.
- The A3 implementation intentionally follows the written specification's banner order even where the reference frame differs.
- Existing recap text is retained where the written specification requires live or fixture-backed content rather than replacing it with reference-frame prose.

## Verification

- `flutter test --no-pub`: 99 tests passed.
- Focused design and claims tests: passed.
- iOS simulator build: passed for scheme `prod`, configuration `Debug-prod`.
- Focused Flutter analyzer: no errors or warnings; informational notices are pre-existing deprecations and style notices in touched legacy files.
- Claim gate: Whispers-off copy explicitly states that listening and remembering continue; no new safety, monitoring, emergency, diagnosis, dementia, fall-detection, or memory-improvement claim was introduced.

Final status: **PASS**, with the two connected-device activation limitations documented above.

---

## Talking to Memories M1 — Design QA

### Authority and target

- Product authority: `ellaaicare/ella-ai#1086`, 2026-07-23 RESTART PRD plus GO amendment.
- Structural source: `/Users/greg/repos/ella/ella-ai/docs/design/mockups/talking-to-memories/Talking to Memories.dc.html`.
- Visual source: `/Users/greg/repos/ella/ella-ai/docs/design/mockups/talking-to-memories/exports/ttm-1a.png` through `ttm-1f.png`.
- Token source: `/Users/greg/repos/ella/ella-ai/docs/design/2026-07-20-app-design-spec-v2.md` §1 and §8.
- Baseline: `54142de0745f7c59220d7b328dd214615586cfca`.
- Runtime target: iPhone 17 Pro, iOS 26.5, 402×874 points.

### Reference-to-simulator comparison

Each implemented state was captured from the iPhone 17 Pro simulator, normalized to the 402×874-point design viewport, and reviewed side by side with its corresponding local export.

| State | Result | Review notes |
| --- | --- | --- |
| 1a | Pass | Phase A shell retained; `Summary` detail uses one conversational entry, no folder chip, no body correction form, 56-point 220-point-wide Talk pill. |
| 1f | Pass | 60% sheet, reduced title/two-chip backdrop, 28-point sheet corners, 300-point Fraunces opener measure, focused 48-point composer, teal send glyph, Done then mic at right. |
| 1d-text | Pass | M1 keyboard confirmation uses the locked confirmation pattern and keeps the pending claim until an affirmative, denial, or one plain-language ambiguous re-prompt. The exported 1d voice treatment remains M2. |
| 1e | Pass | Updated title and overview, 44-point receipt chip, teal emphasis, expandable diff, 48-point Undo, and card history row match the approved state. Person-propagation copy is absent unless the receipt reports a real propagation. |

### Interaction and containment checks

- `Fix something` is reachable from the overflow menu and the former body CTA is absent.
- The talk composer sends ordinary scoped turns without introducing a new session type.
- Smart-apostrophe correction language is extracted correctly.
- Affirmative replies apply; denials discard; ambiguous replies re-prompt once and do not silently apply or discard.
- Applied changes produce a receipt and Undo restores the source title, overview, active version, and any propagation snapshots that were actually applied.
- Correction-only exchanges are persisted under the selected memory with authenticated ownership.
- Main Chat does not receive scoped turns, and the scoped route does not create a new memory.
- Fraunces is used only for Ella-authored copy; UI and user copy remain Manrope.
- New elder-visible strings contain none of the prohibited safety or system vocabulary.

### Evidence policy

Baseline evidence lives outside the repository under `/Users/greg/repos/ella/evidence/mem-talk-m1-r2/baseline-54142de074/`. Final PR screenshots are captured outside the repository from the exact submitted commit and identify that SHA in the PR body; design-export PNGs are reference inputs only, never implementation proof.
