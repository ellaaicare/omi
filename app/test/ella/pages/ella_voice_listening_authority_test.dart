import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/pages/ella_voice_chat_page.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late SharedPreferencesUtil preferences;
  late AiConsentActiveSessionLease lease;
  var authorityLossCalls = 0;
  var listenCalls = 0;
  var currentGeneration = 1;

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
    final authority = AiConsentActiveSessionLease.authorityForSessionStart(
      preferences: preferences,
      expectedUid: 'uid-a',
    );
    lease = AiConsentActiveSessionLease(
      uid: 'uid-a',
      preferences: preferences,
      authority: authority,
      onAuthorityLost: () {},
    )..start();
    authorityLossCalls = 0;
    listenCalls = 0;
    currentGeneration = 1;
    addTearDown(lease.stop);
  });

  Future<bool> resumeSuspendedStartup(Future<void> suspended, {int startupGeneration = 1}) async {
    await suspended;
    return startStandardVoiceListeningIfAuthorized(
      preferences: preferences,
      lease: lease,
      isLifecycleCurrent: () => currentGeneration == startupGeneration,
      listen: () async {
        listenCalls++;
      },
      onAuthorityLost: () {
        authorityLossCalls++;
      },
    );
  }

  test('explicit revoke while startup is suspended reaches review without listening', () async {
    final suspended = Completer<void>();
    final starting = resumeSuspendedStartup(suspended.future);

    preferences.declineAiConsent();
    suspended.complete();

    expect(await starting, isFalse);
    expect(listenCalls, 0);
    expect(authorityLossCalls, 1);
  });

  test('profile transition while startup is suspended reaches review without listening', () async {
    final suspended = Completer<void>();
    final starting = resumeSuspendedStartup(suspended.future);

    await preferences.saveString('aiConsentProfileBindingId', 'profile-binding-b');
    suspended.complete();

    expect(await starting, isFalse);
    expect(listenCalls, 0);
    expect(authorityLossCalls, 1);
  });

  test('expired startup grace after a suspended await cannot begin listening', () async {
    final suspended = Completer<void>();
    final starting = resumeSuspendedStartup(suspended.future);

    SharedPreferencesUtil.clearAiConsentServerVerification();
    preferences.markAiConsentLastServerConfirmed(
      uid: 'uid-a',
      receiptId: 'aicr_receipt-a',
      confirmedAt: DateTime.now().subtract(const Duration(minutes: 31)),
    );
    suspended.complete();

    expect(await starting, isFalse);
    expect(listenCalls, 0);
    expect(authorityLossCalls, 1);
  });

  test('replacement lifecycle cannot use a suspended prior listen attempt', () async {
    final suspended = Completer<void>();
    final starting = resumeSuspendedStartup(suspended.future);

    currentGeneration = 2;
    suspended.complete();

    expect(await starting, isFalse);
    expect(listenCalls, 0);
    expect(authorityLossCalls, 1);
  });
}
