import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/ai_consent_policy.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late SharedPreferencesUtil preferences;

  setUp(() async {
    EllaProvisioningAuthorityCoordinator.resetForTesting();
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

  tearDown(EllaProvisioningAuthorityCoordinator.resetForTesting);

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

  test('session-start authority accepts bounded grace and rejects an expired checkpoint', () {
    expect(
      AiConsentActiveSessionLease.authorityForSessionStart(
        preferences: preferences,
        expectedUid: 'uid-a',
      ),
      isNotNull,
    );

    SharedPreferencesUtil.clearAiConsentServerVerification();
    expect(
      AiConsentActiveSessionLease.authorityForSessionStart(
        preferences: preferences,
        expectedUid: 'uid-a',
      ),
      isNotNull,
    );

    preferences.markAiConsentLastServerConfirmed(
      uid: 'uid-a',
      receiptId: 'aicr_receipt-a',
      confirmedAt: DateTime.now().subtract(const Duration(minutes: 31)),
    );
    expect(
      AiConsentActiveSessionLease.authorityForSessionStart(
        preferences: preferences,
        expectedUid: 'uid-a',
      ),
      isNull,
    );
  });

  test('bounded grace cannot start a session from an obsolete bundled contract', () async {
    SharedPreferencesUtil.clearAiConsentServerVerification();
    await preferences.saveString('aiConsentContractVersion', 'ai-data-processors-v9');

    expect(
      AiConsentActiveSessionLease.authorityForSessionStart(
        preferences: preferences,
        expectedUid: 'uid-a',
      ),
      isNull,
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

  test('newer server receipt and local profile drift preserve same-account authority', () async {
    final authority = AiConsentAuthoritySnapshot.capture(preferences: preferences, expectedUid: 'uid-a');
    expect(authority, isNotNull);

    preferences.acceptAiConsent(
      receiptId: 'aicr_receipt-b',
      uid: 'uid-a',
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:01:00Z',
    );
    expect(authority!.isCurrent(preferences: preferences), isTrue);

    await preferences.saveString('aiConsentProfileBindingId', 'profile-binding-b');
    expect(authority.isCurrent(preferences: preferences), isTrue);
  });

  test('persisted receipt rotation triggers provisioning revalidation and adopts the new authority', () async {
    final revalidatedReceipts = <String>[];
    final lease = AiConsentActiveSessionLease(
      uid: 'uid-a',
      preferences: preferences,
      refreshAuthority: (uid, _, __) async {
        preferences.acceptAiConsent(
          receiptId: 'aicr_receipt-b',
          uid: uid,
          profileBindingId: 'profile-binding-b',
          serverDecidedAt: '2026-07-27T00:01:00Z',
        );
        preferences.markAiConsentServerVerified(
          uid: uid,
          receiptId: 'aicr_receipt-b',
          policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
          processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
          profileBindingId: 'profile-binding-b',
          scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
          scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
        );
        return AiConsentAuthorityRefreshResult(
          AiConsentAuthorityRefreshDisposition.verified,
          status: AiConsentStatus(
            subjectUid: uid,
            authorized: true,
            policy: AiConsentPolicy.bundled,
            decision: AiConsentDecision.granted.wireValue,
            receiptId: 'aicr_receipt-b',
            policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
            processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
            appVersion: '1.0.572',
            buildNumber: '866',
            locale: 'en-US',
            profileBindingId: 'profile-binding-b',
            scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
            scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
            serverDecidedAt: DateTime.utc(2026, 7, 27, 0, 1),
          ),
        );
      },
      revalidateProvisioning: (uid, receiptId) async {
        expect(uid, 'uid-a');
        revalidatedReceipts.add(receiptId);
        return true;
      },
      onAuthorityLost: () {},
    )..start();

    await lease.refreshNow();
    await lease.refreshNow();

    expect(revalidatedReceipts, ['aicr_receipt-b']);
    expect(lease.isActive, isTrue);
    expect(lease.hasCurrentAuthority, isTrue);
    lease.stop();
  });

  test('same receipt survives a local verified persona transition', () async {
    final authority = AiConsentAuthoritySnapshot.capture(preferences: preferences, expectedUid: 'uid-a');
    expect(authority, isNotNull);

    preferences.verifiedPersonaId = 'persona-b';

    expect(authority!.isCurrent(preferences: preferences), isTrue);
  });

  test('same receipt cannot cross an authenticated account transition', () async {
    final authority = AiConsentAuthoritySnapshot.capture(preferences: preferences, expectedUid: 'uid-a');
    expect(authority, isNotNull);

    preferences.uid = 'uid-b';

    expect(authority!.isCurrent(preferences: preferences), isFalse);
  });

  test('terminal consent loss cannot be erased by a newer same-account grant', () async {
    var refreshCalls = 0;
    var authorityLossCalls = 0;
    final authority = AiConsentAuthoritySnapshot.capture(preferences: preferences, expectedUid: 'uid-a');
    final lease = AiConsentActiveSessionLease(
      uid: 'uid-a',
      preferences: preferences,
      authority: authority,
      refreshAuthority: (_, __, ___) async {
        refreshCalls++;
        return const AiConsentAuthorityRefreshResult(AiConsentAuthorityRefreshDisposition.verified);
      },
      onAuthorityLost: () {
        authorityLossCalls++;
      },
    )..start();

    preferences.declineAiConsent();
    preferences.acceptAiConsent(
      receiptId: 'aicr_receipt-b',
      uid: 'uid-a',
      profileBindingId: 'profile-binding-b',
      serverDecidedAt: '2026-07-27T00:01:00Z',
    );
    preferences.markAiConsentServerVerified(
      uid: 'uid-a',
      receiptId: 'aicr_receipt-b',
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      profileBindingId: 'profile-binding-b',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );

    expect(authority!.isCurrent(preferences: preferences), isFalse);
    expect(lease.hasCurrentAuthority, isFalse);
    await lease.refreshNow();

    expect(refreshCalls, 0);
    expect(authorityLossCalls, 1);
    expect(lease.isActive, isFalse);
  });

  test('verified deploy drift schedules a normal refresh interval instead of spinning at zero delay', () async {
    final durableConfirmation = DateTime.utc(2026, 7, 27, 0, 0);
    preferences.markAiConsentLastServerConfirmed(
      uid: 'uid-a',
      receiptId: 'aicr_receipt-a',
      confirmedAt: durableConfirmation,
    );
    final lease = AiConsentActiveSessionLease(
      uid: 'uid-a',
      preferences: preferences,
      refreshAuthority: (_, __, ___) async => const AiConsentAuthorityRefreshResult(
        AiConsentAuthorityRefreshDisposition.verified,
      ),
      onAuthorityLost: () {},
    )..start();

    SharedPreferencesUtil.clearAiConsentServerVerification();
    await lease.refreshNow();

    expect(lease.isActive, isTrue);
    expect(preferences.aiConsentServerVerificationRemaining, isNull);
    expect(preferences.aiConsentLastServerConfirmedAt, durableConfirmation);
    expect(lease.scheduledRefreshDelay, AiConsentActiveSessionLease.refreshInterval);
    lease.stop();
  });

  test('unpersisted newer receipt does not advance the terminal refresh fence', () async {
    final requestedReceipts = <String>[];
    var authorityLossCalls = 0;
    final lease = AiConsentActiveSessionLease(
      uid: 'uid-a',
      preferences: preferences,
      refreshAuthority: (_, receiptId, __) async {
        requestedReceipts.add(receiptId);
        if (requestedReceipts.length == 1) {
          return AiConsentAuthorityRefreshResult(
            AiConsentAuthorityRefreshDisposition.verified,
            status: AiConsentStatus(
              subjectUid: 'uid-a',
              authorized: true,
              policy: AiConsentPolicy.bundled,
              decision: AiConsentDecision.granted.wireValue,
              receiptId: 'aicr_receipt-b',
              policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
              processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
              appVersion: '1.0.572',
              buildNumber: '866',
              locale: 'en-US',
              profileBindingId: 'profile-binding-a',
              scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
              scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
              serverDecidedAt: DateTime.utc(2026, 7, 27, 0, 1),
            ),
          );
        }
        return const AiConsentAuthorityRefreshResult(AiConsentAuthorityRefreshDisposition.revoked);
      },
      onAuthorityLost: () {
        authorityLossCalls++;
      },
    )..start();

    await lease.refreshNow();
    expect(lease.isActive, isTrue);
    expect(preferences.aiConsentReceiptId, 'aicr_receipt-a');

    await lease.refreshNow();

    expect(requestedReceipts, ['aicr_receipt-a', 'aicr_receipt-a']);
    expect(authorityLossCalls, 1);
    expect(lease.isActive, isFalse);
  });

  test('retryable failures stop only after the thirty-minute grace expires', () async {
    var now = DateTime(2026, 7, 27, 0, 4);
    preferences.markAiConsentLastServerConfirmed(
      uid: 'uid-a',
      receiptId: 'aicr_receipt-a',
      confirmedAt: now,
    );
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

  test('last server confirmation survives verification reset but not account switch', () async {
    final confirmedAt = preferences.aiConsentLastServerConfirmedAt;
    expect(confirmedAt, isNotNull);

    SharedPreferencesUtil.clearAiConsentServerVerification();
    expect(preferences.aiConsentLastServerConfirmedAt, confirmedAt);
    expect(preferences.aiConsentLastServerConfirmationAge, isNotNull);

    preferences.uid = 'uid-b';
    expect(preferences.aiConsentLastServerConfirmedAt, isNull);
    expect(preferences.aiConsentLastServerConfirmationAge, isNull);
  });
}
