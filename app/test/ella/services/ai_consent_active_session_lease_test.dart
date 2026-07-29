import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';

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
      refreshAuthority: (uid) async {
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
        return true;
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
      refreshAuthority: (_) async {
        preferences.declineAiConsent();
        return false;
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

  test('unavailable consent authority stops active session without extending cached grant', () async {
    var authorityLossCalls = 0;
    final lease = AiConsentActiveSessionLease(
      uid: 'uid-a',
      preferences: preferences,
      refreshAuthority: (_) async => false,
      onAuthorityLost: () {
        authorityLossCalls++;
      },
    )..start();

    await lease.refreshNow();

    expect(authorityLossCalls, 1);
    expect(lease.isActive, isFalse);
    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.aiConsentReceiptId, 'aicr_receipt-a');
  });
}
