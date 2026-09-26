import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/services/wals/wal.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  SharedPreferencesUtil acceptedPrefs() {
    final prefs = SharedPreferencesUtil()..uid = 'owner';
    prefs.acceptAiConsent(
      receiptId: 'aicr_owner',
      uid: 'owner',
      profileBindingId: 'profile',
      serverDecidedAt: '2026-09-26T00:00:00Z',
    );
    return prefs;
  }

  test('an explicit consent rejection stops the session', () async {
    final prefs = acceptedPrefs();
    var lost = 0;
    final lease = AiConsentActiveSessionLease(
      uid: 'owner',
      preferences: prefs,
      onAuthorityLost: () => lost++,
      refreshAuthority: (_, __, ___) async =>
          const AiConsentAuthorityRefreshResult(AiConsentAuthorityRefreshDisposition.revoked),
    );
    lease.start();
    await lease.refreshNow();

    expect(lost, 1);
    expect(lease.isActive, isFalse);
    expect(prefs.aiConsentAccepted, isFalse);
  });

  test('a timeout or offline refresh keeps the session running', () async {
    final prefs = acceptedPrefs();
    var lost = 0;
    final lease = AiConsentActiveSessionLease(
      uid: 'owner',
      preferences: prefs,
      now: () => DateTime.utc(2026, 9, 26, 12),
      onAuthorityLost: () => lost++,
      refreshAuthority: (_, __, ___) async =>
          const AiConsentAuthorityRefreshResult(AiConsentAuthorityRefreshDisposition.retryable, supportCode: 'timeout'),
    );
    lease.start();
    await lease.refreshNow();
    await lease.refreshNow();

    expect(lost, 0);
    expect(lease.isActive, isTrue);
    expect(prefs.aiConsentAccepted, isTrue);
    expect(AiConsentActiveSessionLease.diagnostics.value.phase, AiConsentLeasePhase.retrying);
    expect(AiConsentActiveSessionLease.diagnostics.value.supportCode, 'timeout');
  });

  test('settings revoke clears cached acceptance immediately', () {
    final prefs = acceptedPrefs();
    prefs.declineAiConsent();
    expect(prefs.getBool('aiConsentAccepted', defaultValue: true), isFalse);
    expect(prefs.aiConsentAccepted, isFalse);
    expect(AiConsentAuthoritySnapshot.capture(preferences: prefs, expectedUid: 'owner'), isNull);
  });

  test('cached acceptance starts capture and a thrown refresh does not stop it', () async {
    final prefs = acceptedPrefs();
    SharedPreferencesUtil.clearAiConsentServerVerification();
    final accepted = await ensureCaptureConsentAuthority(
      hasCurrentConsent: () => prefs.aiConsentAccepted,
      persistedAuthority: () => AiConsentAuthoritySnapshot.capture(preferences: prefs),
      lastServerConfirmationAge: () => const Duration(hours: 5),
      refreshAuthority: (_, __, ___) async => throw Exception('offline'),
    );
    expect(accepted, isTrue);
    expect(prefs.aiConsentAccepted, isTrue);
  });

  test('explicitly missing acceptance does not start capture', () async {
    final prefs = SharedPreferencesUtil()..uid = 'owner';
    var refreshes = 0;
    final accepted = await ensureCaptureConsentAuthority(
      hasCurrentConsent: () => prefs.aiConsentAccepted,
      persistedAuthority: () => null,
      lastServerConfirmationAge: () => null,
      refreshAuthority: (_, __, ___) async {
        refreshes++;
        return const AiConsentAuthorityRefreshResult(AiConsentAuthorityRefreshDisposition.verified);
      },
    );
    expect(accepted, isFalse);
    expect(refreshes, 0);
  });

  test('WAL upload identity is the uid only', () {
    const kept = WalOwner(
      uid: 'owner',
      profileBindingId: 'old-profile',
      bindingRevision: 1,
      consentReceiptId: 'aicr_old',
      authorityGenerationAtCapture: 2,
    );
    const rolled = WalOwner(
      uid: 'owner',
      profileBindingId: 'new-profile',
      bindingRevision: 9,
      consentReceiptId: 'aicr_new',
      authorityGenerationAtCapture: 40,
    );
    const other = WalOwner(
      uid: 'other',
      profileBindingId: 'old-profile',
      bindingRevision: 1,
      consentReceiptId: 'aicr_old',
      authorityGenerationAtCapture: 2,
    );
    expect(kept.matches(rolled), isTrue);
    expect(kept.matches(other), isFalse);
  });

  test('listen close 4403 and HTTP ai_consent_required are the explicit consent codes', () {
    expect(aiConsentRequiredListenCloseCode, 4403);
    final rejected = ClientApiFailure.fromHttp(statusCode: 403, body: '{"code":"ai_consent_required"}');
    expect(rejected.kind, ClientApiFailureKind.consentRequired);
    expect(rejected.retryable, isFalse);
    final unavailable = ClientApiFailure.fromHttp(statusCode: 503, body: '{"code":"upstream_down"}');
    expect(unavailable.kind, ClientApiFailureKind.unavailable);
    expect(unavailable.retryable, isTrue);
  });
}
