import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';
import 'package:omi/providers/capture_provider.dart';

Future<AiConsentAuthoritySnapshot> _grantConsentAuthority() async {
  SharedPreferences.setMockInitialValues({});
  await SharedPreferencesUtil.init();
  final preferences = SharedPreferencesUtil()..uid = 'owner';
  preferences.acceptAiConsent(
    receiptId: 'aicr_local-receipt',
    uid: 'owner',
    profileBindingId: 'profile-owner',
    serverDecidedAt: '2026-09-25T00:00:00Z',
  );
  preferences.markAiConsentServerVerified(
    uid: 'owner',
    receiptId: 'aicr_local-receipt',
    policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
    processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
    profileBindingId: 'profile-owner',
    scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
    scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
  );
  expect(preferences.uid, 'owner');
  expect(preferences.getBool('aiConsentAccepted', defaultValue: false), isTrue);
  expect(preferences.aiConsentReceiptId, 'aicr_local-receipt');
  expect(preferences.aiConsentReceiptUid, 'owner');
  return const AiConsentAuthoritySnapshot(
    generation: 0,
    uid: 'owner',
    verifiedPersonaId: null,
    profileBindingId: 'profile-owner',
    receiptId: 'aicr_local-receipt',
    policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
    processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
    scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
    scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
  );
}

void main() {
  test('empty frames cannot prove phone capture started', () async {
    final proof = PhoneCaptureStartProof();

    expect(proof.acceptFrame(const []), isFalse);
    await expectLater(
      proof.waitForAudio(timeout: const Duration(milliseconds: 1)),
      throwsA(isA<TimeoutException>()),
    );
  });

  test('native recorder receipt does not replace physical phone audio proof', () async {
    final proof = PhoneCaptureStartProof();

    proof.acceptNativeRecorderStart();
    await proof.waitForNativeRecorder(timeout: const Duration(milliseconds: 50));
    await expectLater(
      proof.waitForAudio(timeout: const Duration(milliseconds: 1)),
      throwsA(isA<TimeoutException>()),
    );
  });

  test('phone physical capture and transcription delivery remain separate facts', () async {
    final proof = PhoneCaptureStartProof();

    proof.acceptNativeRecorderStart();
    expect(proof.acceptFrame(const [1, 2, 3]), isTrue);
    await proof.waitForNativeRecorder(timeout: const Duration(milliseconds: 50));
    await proof.waitForAudio(timeout: const Duration(milliseconds: 50));
    await expectLater(
      proof.waitForTransmittedAudio(timeout: const Duration(milliseconds: 1)),
      throwsA(isA<TimeoutException>()),
    );

    expect(proof.acceptTransmittedFrame(const [1, 2, 3]), isTrue);
    await proof.waitForTransmittedAudio(timeout: const Duration(milliseconds: 50));
  });

  test('BLE listener installation cannot prove necklace capture without transmitted audio', () async {
    final proof = DeviceCaptureStartProof();

    expect(proof.acceptTransmittedFrame(const []), isFalse);
    await expectLater(
      proof.waitForTransmittedAudio(timeout: const Duration(milliseconds: 1)),
      throwsA(isA<TimeoutException>()),
    );
  });

  test('physical BLE audio proves necklace capture independently of transcription', () async {
    final proof = DeviceCaptureStartProof();

    expect(
      proof.acceptPhysicalFrame(physicalDeviceAudioPayload(DeviceType.omi, const [1, 2, 3])),
      isFalse,
    );
    await expectLater(
      proof.waitForPhysicalAudio(timeout: const Duration(milliseconds: 1)),
      throwsA(isA<TimeoutException>()),
    );

    expect(
      proof.acceptPhysicalFrame(physicalDeviceAudioPayload(DeviceType.omi, const [1, 2, 3, 4])),
      isTrue,
    );
    await proof.waitForPhysicalAudio(timeout: const Duration(milliseconds: 50));
    await expectLater(
      proof.waitForTransmittedAudio(timeout: const Duration(milliseconds: 1)),
      throwsA(isA<TimeoutException>()),
    );

    expect(proof.acceptTransmittedFrame(const [1, 2, 3]), isTrue);
    await proof.waitForTransmittedAudio(timeout: const Duration(milliseconds: 50));
  });

  test('current consent starts without a server refresh', () async {
    var refreshCalls = 0;

    final accepted = await ensureCaptureConsentAuthority(
      hasCurrentConsent: () => true,
      persistedAuthority: () => null,
      lastServerConfirmationAge: () => null,
      refreshAuthority: (_, __, ___) async {
        refreshCalls++;
        return const AiConsentAuthorityRefreshResult(AiConsentAuthorityRefreshDisposition.verified);
      },
    );

    expect(accepted, isTrue);
    expect(refreshCalls, 0);
  });

  test('expired consent refreshes before capture', () async {
    final authority = await _grantConsentAuthority();
    SharedPreferencesUtil.clearAiConsentServerVerification();
    var refreshCalls = 0;

    final accepted = await ensureCaptureConsentAuthority(
      hasCurrentConsent: () => SharedPreferencesUtil().aiConsentAccepted,
      persistedAuthority: () => authority,
      lastServerConfirmationAge: () => SharedPreferencesUtil().aiConsentLastServerConfirmationAge,
      refreshAuthority: (uid, receiptId, decidedAt) async {
        refreshCalls++;
        expect(uid, 'owner');
        expect(receiptId, 'aicr_local-receipt');
        return const AiConsentAuthorityRefreshResult(AiConsentAuthorityRefreshDisposition.verified);
      },
    );

    expect(accepted, isTrue);
    expect(refreshCalls, 1);
  });

  test('phone capture stays closed without an authenticated owner', () async {
    var refreshCalls = 0;

    final accepted = await ensureCaptureConsentAuthority(
      hasCurrentConsent: () => false,
      persistedAuthority: () => null,
      lastServerConfirmationAge: () => Duration.zero,
      refreshAuthority: (_, __, ___) async {
        refreshCalls++;
        return const AiConsentAuthorityRefreshResult(AiConsentAuthorityRefreshDisposition.verified);
      },
    );

    expect(accepted, isFalse);
    expect(refreshCalls, 0);
  });

  test('phone capture cannot reconstruct consent without a local receipt', () async {
    var refreshCalls = 0;

    final accepted = await ensureCaptureConsentAuthority(
      hasCurrentConsent: () => false,
      persistedAuthority: () => null,
      lastServerConfirmationAge: () => Duration.zero,
      refreshAuthority: (_, __, ___) async {
        refreshCalls++;
        return const AiConsentAuthorityRefreshResult(AiConsentAuthorityRefreshDisposition.verified);
      },
    );

    expect(accepted, isFalse);
    expect(refreshCalls, 0);
  });

  test('refresh receipt must make local consent authority current', () async {
    final authority = await _grantConsentAuthority();
    SharedPreferencesUtil.clearAiConsentServerVerification();
    final accepted = await ensureCaptureConsentAuthority(
      hasCurrentConsent: () => false,
      persistedAuthority: () => authority,
      lastServerConfirmationAge: () => SharedPreferencesUtil().aiConsentLastServerConfirmationAge,
      refreshAuthority: (_, __, ___) async {
        SharedPreferencesUtil().declineAiConsent();
        return const AiConsentAuthorityRefreshResult(AiConsentAuthorityRefreshDisposition.verified);
      },
    );

    expect(accepted, isFalse);
  });

  test('explicit server revocation cannot enter grace', () async {
    final authority = await _grantConsentAuthority();
    SharedPreferencesUtil.clearAiConsentServerVerification();
    final accepted = await ensureCaptureConsentAuthority(
      hasCurrentConsent: () => false,
      persistedAuthority: () => authority,
      lastServerConfirmationAge: () => SharedPreferencesUtil().aiConsentLastServerConfirmationAge,
      refreshAuthority: (_, __, ___) async =>
          const AiConsentAuthorityRefreshResult(AiConsentAuthorityRefreshDisposition.revoked),
    );

    expect(accepted, isFalse);
  });
}
