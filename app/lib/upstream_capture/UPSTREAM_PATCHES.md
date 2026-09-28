# Upstream patches — BasedHardware/omi@f16699aea7fe9ba089baceb628922f2882c51153

Tracks ellaaicare/ella-ai#1280.

## Behavior patches to upstream-owned files

**None.**

Every file listed in `UPSTREAM_OWNED.txt` is byte-identical to the pin except for the
mechanical Dart import relocation
`'package:omi/<rel>'` → `'package:omi/upstream_capture/<rel>'` (only for `<rel>` that are
themselves vendored). Swift/ObjC/Markdown files and the one `in-place` Dart file are
byte-identical with no rewrite at all. `scripts/verify_upstream_capture_identity.py` (and
`app/test/ella/upstream_capture/upstream_capture_byte_identity_test.dart`) fail on any other
difference, and the manifest format has no "patched" kind: patching a file would require
removing it from the manifest, which the guard and tests would also notice.

The consent/account gate (review item 4) is wired entirely through upstream's existing
constructor seams from Ella adapter code outside the vendored trees:

| Audio boundary | Upstream seam used | Ella adapter |
| --- | --- | --- |
| Phone mic (native → Dart frames) | `CaptureController(phoneMicRecorder: IMicRecorderService)` | `EllaGatedMicRecorderService` |
| Necklace (BLE audio packets) | `CaptureController(deviceConnectionLoader: ...)` → `DeviceConnection.getBleAudioBytesListener` | `EllaGatedDeviceConnection` |
| Transcription socket (Dart → network) | `CaptureController(openSocket: CaptureConversationSocketOpen)` | `EllaGatedTranscriptSocket` / `ellaGatedConversationSocketOpen` |
| WAL upload (disk → network) | `RecordingTransferCoordinator.configure(autoUploadEnabled: ...)` | `EllaUpstreamCaptureRuntime.configureTransferCoordinator` |
| Native batch writers (no Dart frames) | upstream `CapturePolicy` latch (`SharedPreferencesUtil.setCaptureMuted` → `com.omi/capture_policy` → `CaptureAdmissionPolicy`) | `EllaUpstreamCaptureRuntime._stopUpstreamCapture` |

## Fork-side changes that are NOT upstream patches

These touch fork (non-upstream-owned) files so the vendored files can stay byte-identical:

1. `app/lib/models/stt_response_schema.dart` — replaced **in place** by the pin's exact bytes
   (listed as kind `in-place` in the manifest and byte-checked). The pin's copy is a strict
   superset of the fork's (four optional segment field names and their JSON keys).
   `app/lib/services/auth/auth_token_result.dart` — upstream's dependency-free token-result /
   session-expiry types, added **in place** at their upstream path (kind `in-place`).
2. `app/lib/services/auth_service.dart` — upstream's `auth_service.dart` is NOT vendored (it signs
   in/out of Firebase directly, bypassing Ella's quiesced identity transitions, which
   `test/ella/services/p1_hygiene_test.dart` enforces). The fork's `AuthService` gained the typed
   surface the vendored HTTP/capture files call: `refreshIdToken()` (upstream's result classes and
   terminal codes, no identity mutation), `expireSession()` (routes to
   `signOutForReauthentication()`), and an inert `recordAuthenticatedRequest401()`.
3. `app/lib/utils/analytics/analytics_manager.dart`, `app/lib/utils/platform/platform_manager.dart`
   — additive, inert members that the vendored code calls (`PlatformManager.analytics`,
   `appBuild`, `appNamespace`, `AnalyticsManager.track/isFeatureEnabled/omiDoubleTap/...`).
   Ella does not forward upstream product analytics; every member is a no-op and
   `isFeatureEnabled` fails closed. Vendoring upstream's analytics instead was not possible:
   it needs `posthog_flutter` 5.x APIs this fork's pinned toolchain does not resolve.
4. `app/pubspec.yaml` — `flutter_contacts` 1.1.9+2 → 2.1.0 and `flutter_timezone` 3.0.1 → 5.1.0
   (the APIs the vendored `phone_call_provider.dart` / `backend/http/api/users.dart` are written
   against). Upstream pins `flutter_contacts ^2.3.1`, which requires Dart 3.12/Flutter 3.44; this
   fork's CI pins Flutter 3.41.1, so 2.1.0 (same `permissions`/`getAll`/`ContactProperty` API)
   is used. Four fork call sites were ported to the new return types.
5. `app/lib/l10n/*.arb` — `transcriptionPausedReconnecting` (used by upstream
   `CaptureController`; values copied from the pin's ARB files where the locale exists).

## Proposed upstream hooks (drafted; NOT required by this port, nothing patched)

The port did not need these, but they would remove the two places where the Ella composition
has to reach past `composeCaptureProvider`. Draft PR body for BasedHardware/omi:

> **Title:** capture: expose `processInProgressConversation` on `CaptureDependencies`
>
> `composeCaptureProvider(CaptureDependencies)` is documented as the single production
> constructor path, but `CaptureDependencies` omits the `processInProgressConversation`
> seam that `CaptureController` already accepts. Hosts that want deterministic finish/
> processing tests (or a custom processing endpoint) must bypass the composition helper
> and call `CaptureProvider(...)` directly, duplicating its forwarding.
>
> This adds an optional `processInProgressConversation` field to `CaptureDependencies`
> and forwards it in `composeCaptureProvider`. Default `null` keeps today's behavior
> (the REST `processInProgressConversation()`); no production call site changes.
>
> Tests: extend `capture_composition_test.dart` to assert the new seam is forwarded
> intact.

> **Title:** capture: optional per-frame audio admission seam
>
> Capture admission today is the durable `CapturePolicy` (mute + revision). Hosts with an
> additional, live authorization requirement (e.g. a consent lease that can be revoked
> server-side mid-session) currently have to decorate three seams (`phoneMicRecorder`,
> `deviceConnectionLoader`, `openSocket`) to gate each frame. This proposes an optional
> `bool Function() audioAdmission` on `CaptureController`/`CaptureDependencies`, checked
> alongside `_admitsCapture(revision)` in the phone `onByteReceived`, the BLE
> `onAudioBytesReceived` and before `_socket.send` of audio frames; `null` means
> always-admitted (no behavior change).
