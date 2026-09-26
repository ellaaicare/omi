import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late SharedPreferencesUtil preferences;

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    preferences.verifiedPersonaId = 'persona-a';
    preferences.acceptAiConsent(
      receiptId: 'aicr_receipt-a',
      uid: 'uid-a',
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:00:00Z',
    );
    preferences.markAiConsentServerVerified(
      uid: 'uid-a',
      receiptId: 'aicr_receipt-a',
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      profileBindingId: 'profile-binding-a',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );
  });

  test('active session refreshes before TTL and continues with renewed server authority', () async {
    var refreshCalls = 0;
    var authorityLossCalls = 0;
    final lease = AiConsentActiveSessionLease(
      uid: 'uid-a',
      preferences: preferences,
      refreshAuthority: (uid, receiptId, decidedAt) async {
        refreshCalls++;
        preferences.markAiConsentServerVerified(
          uid: uid,
          receiptId: 'aicr_receipt-a',
          policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
          processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
          profileBindingId: 'profile-binding-a',
          scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
          scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
        );
        return const AiConsentAuthorityRefreshResult(AiConsentAuthorityRefreshDisposition.verified);
      },
      onAuthorityLost: () {
        authorityLossCalls++;
      },
    );

    lease.start();
    expect(AiConsentActiveSessionLease.refreshInterval, lessThan(SharedPreferencesUtil.aiConsentServerVerificationTtl));
    await lease.refreshNow();

    expect(refreshCalls, 1);
    expect(authorityLossCalls, 0);
    expect(lease.isActive, isTrue);
    expect(preferences.aiConsentAccepted, isTrue);
    lease.stop();
  });

  test('session opened near expiry refreshes immediately rather than waiting a fresh interval', () {
    expect(AiConsentActiveSessionLease.refreshDelayFor(const Duration(seconds: 30)), Duration.zero);
    expect(
      AiConsentActiveSessionLease.refreshDelayFor(SharedPreferencesUtil.aiConsentServerVerificationTtl),
      AiConsentActiveSessionLease.refreshInterval,
    );
  });

  test('server revocation stops active session visibly and fails closed', () async {
    var authorityLossCalls = 0;
    final lease = AiConsentActiveSessionLease(
      uid: 'uid-a',
      preferences: preferences,
      refreshAuthority: (_, __, ___) async {
        preferences.declineAiConsent();
        return const AiConsentAuthorityRefreshResult(AiConsentAuthorityRefreshDisposition.revoked);
      },
      onAuthorityLost: () {
        authorityLossCalls++;
      },
    )..start();

    await lease.refreshNow();
    await lease.refreshNow();

    expect(authorityLossCalls, 1);
    expect(lease.isActive, isFalse);
    expect(preferences.aiConsentAccepted, isFalse);
  });

  test('retryable refresh failure keeps active capture authorized during grace', () async {
    var authorityLossCalls = 0;
    final lease = AiConsentActiveSessionLease(
      uid: 'uid-a',
      preferences: preferences,
      refreshAuthority: (_, __, ___) async => const AiConsentAuthorityRefreshResult(
        AiConsentAuthorityRefreshDisposition.retryable,
        supportCode: 'ai_consent_authority_unavailable',
      ),
      onAuthorityLost: () {
        authorityLossCalls++;
      },
    )..start();

    await lease.refreshNow();

    expect(authorityLossCalls, 0);
    expect(lease.isActive, isTrue);
    expect(lease.hasCurrentAuthority, isTrue);
    expect(preferences.aiConsentAccepted, isTrue);
    expect(preferences.aiConsentReceiptId, 'aicr_receipt-a');
    expect(AiConsentActiveSessionLease.diagnostics.value.phase, AiConsentLeasePhase.retrying);
    lease.stop();
  });

  test('backend deploy drift does not invalidate an active server-confirmed receipt', () async {
    var authorityLossCalls = 0;
    final lease = AiConsentActiveSessionLease(
      uid: 'uid-a',
      preferences: preferences,
      refreshAuthority: (_, __, ___) async => const AiConsentAuthorityRefreshResult(
        AiConsentAuthorityRefreshDisposition.retryable,
        supportCode: 'backend_deploy',
      ),
      onAuthorityLost: () {
        authorityLossCalls++;
      },
    )..start();

    await preferences.saveString('aiConsentContractVersion', 'server-policy-after-deploy');
    await preferences.saveString('aiConsentProcessorSetHash', 'server-processors-after-deploy');
    await preferences.saveString('aiConsentScopeHash', 'server-scope-after-deploy');
    await lease.refreshNow();

    expect(authorityLossCalls, 0);
    expect(lease.isActive, isTrue);
    expect(lease.hasCurrentAuthority, isTrue);
    lease.stop();
  });

  test('retryable failures stop only after the thirty-minute grace expires', () async {
    var now = DateTime(2026, 7, 27, 0, 4);
    var authorityLossCalls = 0;
    final lease = AiConsentActiveSessionLease(
      uid: 'uid-a',
      preferences: preferences,
      now: () => now,
      gracePeriod: const Duration(minutes: 30),
      refreshAuthority: (_, __, ___) async => const AiConsentAuthorityRefreshResult(
        AiConsentAuthorityRefreshDisposition.retryable,
        supportCode: 'http_503',
      ),
      onAuthorityLost: () {
        authorityLossCalls++;
      },
    )..start();

    await lease.refreshNow();
    expect(lease.isActive, isTrue);

    now = now.add(const Duration(minutes: 31));
    await lease.refreshNow();

    expect(authorityLossCalls, 1);
    expect(lease.isActive, isFalse);
    expect(AiConsentActiveSessionLease.diagnostics.value.terminalReason, 'verification_grace_expired');
  });
}
