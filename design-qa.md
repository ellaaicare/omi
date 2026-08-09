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

# Ella Today Density Rebalance — Design QA

## Visual truth

- Healthy source: `/Users/greg/repos/ella/design-reference/exports/today-rebalance-a-healthy.png`
- Large-text source: `/Users/greg/repos/ella/design-reference/exports/today-rebalance-e-dynamic-type.png`
- Source size: 804×1748 px (402×874 pt at @2x)
- Source notes: `/Users/greg/repos/ella/design-reference/NOTES.md`

## Verified implementation

- Device: Codex iPhone 17 Pro Max
- OS: iOS 26.5
- App viewport: 440×956 pt at @3x
- Raw capture: `design-qa-evidence/implementation-healthy-iphone17promax-raw.png` (1320×2868 px)
- Standard-text capture: `design-qa-evidence/implementation-healthy-final.jpg`
- Accessibility-text capture: `design-qa-evidence/implementation-dynamic-type-final.jpg`
- Comparison captures normalize both source and implementation to 368×800 px for a like-for-like visual review while preserving the raw simulator capture above.

## State under test

- Thursday, July 24 at 9:41
- Healthy hardware: necklace 96%, Ella headset connected
- Whispers enabled
- Healthy daily note for Margaret
- Featured memory plus two compact recent-memory cards
- Standard Dynamic Type and Accessibility Medium Dynamic Type

The deterministic state is enabled only by the compile-time `ELLA_TODAY_DESIGN_PREVIEW` flag used for simulator QA. Normal builds retain the existing production state and data flow.

## Comparison

- Healthy full view: `design-qa-evidence/comparison-healthy-final.jpg`
- Large-text full view: `design-qa-evidence/comparison-dynamic-type-final.jpg`
- Healthy source crop: `design-qa-evidence/source-healthy-368x800.png`
- Large-text source crop: `design-qa-evidence/source-dynamic-type-368x800.png`

The final pass checked the hardware strip, daily-note typography and inline link, read-aloud action, whisper pill, recent-memory hierarchy, card borders/radii, spacing, colors, and source artwork. The implementation keeps Ella's existing persistent bottom navigation, which the static design export does not depict; the scroll view provides bottom padding and was verified through the full card grid.

## Interaction and runtime checks

- Whispers switch toggled off and back on; labels and values updated correctly.
- Read aloud toggled to Stop and back to Read aloud.
- Home content scrolled through the two-up memory grid without clipping or losing access behind the persistent navigation.
- Accessibility Medium rendered without overflow.
- Final simulator build and launch completed successfully; dependency warnings only, with no app build errors or crash/error screen.

## Fixes made during QA

- Matched the reference daily-note and memory fixture content.
- Kept “Read more” inline with the Fraunces note typography.
- Matched the compact “See whispers” link copy.
- Regenerated every localization implementation after adding the new localized copy.
- Removed the development ribbon from final evidence by verifying the production scheme in Debug configuration.

## Result

passed

---

# Ella Home — Memory Mosaic design QA

- Selected reference: `/Users/ellaai/.buzz/RESEARCH/ELLA_HOME_REFINED_MEMORY_MOSAIC_2026_08_09.png`
- Implementation capture: `app/test/pages/home/goldens/ella_home_memory_mosaic.png`
- Combined comparison: `/private/tmp/home-design-comparison-successor.png`
- Matched viewport: 390 × 844 points

## Visual inspection

- P0: none.
- P1: none.
- P2: the first review head's 320-point/200% test omitted the persistent shell navigation. The real shell exposed a 26-point label overflow; the successor uses responsive navigation label sizing and a checked-in full-shell golden.
- Hierarchy matches the selected synthesis: date and greeting, editorial Daily Note, one intentional Record action, then the two-up memory journal.
- The implementation intentionally adds the final requested date anchor, signed-in name, state-aware phone/necklace caption, distinct Talk label/icon, and compact controls button.
- Fraunces remains the editorial family for the Note and journal hierarchy. Generated botanical art is decorative only; source media is shown only when attached to the actual memory.
- The longer real greeting wraps safely instead of truncating, while the Daily Note, recording action, and first memory row remain visible at 390 × 844.

## States and accessibility

- Ready, preparing, degraded/no-note, day-one empty, phone capture, necklace capture, starting, and live recording states have widget coverage.
- 320-point width at 200% text scale has no clipping or ellipsis; the memory layout stacks at large text.
- Capture and controls targets meet the 48 × 48 point minimum. Semantic order follows the visual hierarchy and the capture control announces its active source.
- Reduce Motion removes the capture transition duration and disables the breathing animation.
- Checked contrast ratios: ink/paper 15.06:1; teal/pale capture 4.73:1; teal/paper 5.82:1; ink/card 14.47:1; soft/card 5.62:1; warning/card 4.54:1.

## Successor verification

- Real-shell large-text capture: `app/test/pages/home/goldens/ella_home_memory_mosaic_320_200_full_shell.png` at 320 × 844 points and 200% text.
- The final full-shell capture keeps every bottom-navigation label readable, retains the 48-point controls, and has no overflow, clipping, ellipsis, or orphaned greeting punctuation.
- The 390 × 844 reference/implementation comparison was rebuilt after the greeting and navigation corrections. The differences are deliberate product requirements: date anchor, Talk instead of Voice, source-aware recording copy, and real source media when available.
- Interaction QA now also covers Home-owned necklace stop, continuous-necklace pre-tap/final boundaries, reason-specific terminal Note copy, and distinct transient/permanent scoped Talk recovery.

final result: passed
