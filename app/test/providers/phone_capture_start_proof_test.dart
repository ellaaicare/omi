import 'dart:async';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/providers/capture_provider.dart';

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

    expect(proof.acceptPhysicalFrame(const [1, 2, 3]), isTrue);
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
      authenticatedUid: () => 'owner',
      persistedConsentReceiptId: () => '',
      refreshAuthority: (_) async {
        refreshCalls++;
        return true;
      },
    );

    expect(accepted, isTrue);
    expect(refreshCalls, 0);
  });

  test('expired consent refreshes before capture', () async {
    var consentCurrent = false;
    var refreshCalls = 0;

    final accepted = await ensureCaptureConsentAuthority(
      hasCurrentConsent: () => consentCurrent,
      authenticatedUid: () => ' owner ',
      persistedConsentReceiptId: () => 'aicr-local-receipt',
      refreshAuthority: (uid) async {
        refreshCalls++;
        expect(uid, 'owner');
        consentCurrent = true;
        return true;
      },
    );

    expect(accepted, isTrue);
    expect(refreshCalls, 1);
  });

  test('phone capture stays closed without an authenticated owner', () async {
    var refreshCalls = 0;

    final accepted = await ensureCaptureConsentAuthority(
      hasCurrentConsent: () => false,
      authenticatedUid: () => '   ',
      persistedConsentReceiptId: () => 'aicr-local-receipt',
      refreshAuthority: (_) async {
        refreshCalls++;
        return true;
      },
    );

    expect(accepted, isFalse);
    expect(refreshCalls, 0);
  });

  test('phone capture cannot reconstruct consent without a local receipt', () async {
    var refreshCalls = 0;

    final accepted = await ensureCaptureConsentAuthority(
      hasCurrentConsent: () => false,
      authenticatedUid: () => 'owner',
      persistedConsentReceiptId: () => '',
      refreshAuthority: (_) async {
        refreshCalls++;
        return true;
      },
    );

    expect(accepted, isFalse);
    expect(refreshCalls, 0);
  });

  test('refresh receipt must make local consent authority current', () async {
    final accepted = await ensureCaptureConsentAuthority(
      hasCurrentConsent: () => false,
      authenticatedUid: () => 'owner',
      persistedConsentReceiptId: () => 'aicr-local-receipt',
      refreshAuthority: (_) async => true,
    );

    expect(accepted, isFalse);
  });

  test('server refresh cannot replace the local capture receipt', () async {
    var consentCurrent = false;
    var receiptId = 'aicr-local-receipt';

    final accepted = await ensureCaptureConsentAuthority(
      hasCurrentConsent: () => consentCurrent,
      authenticatedUid: () => 'owner',
      persistedConsentReceiptId: () => receiptId,
      refreshAuthority: (_) async {
        consentCurrent = true;
        receiptId = 'aicr-different-receipt';
        return true;
      },
    );

    expect(accepted, isFalse);
  });
}
