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

  test('first non-empty frame proves phone capture started', () async {
    final proof = PhoneCaptureStartProof();

    expect(proof.acceptFrame(const [1, 2, 3]), isTrue);
    await proof.waitForAudio(timeout: const Duration(milliseconds: 50));
  });
}
