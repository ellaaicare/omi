import 'dart:async';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';
import 'package:omi/upstream_capture/services/mic/mic_arbiter.dart';
import 'support/ella_upstream_capture_harness.dart';

void main() {
  for (final granted in [false, true]) {
    test('actual upstream phone permission awaits under exact owner: $granted', () async {
      final directory = await Directory.systemTemp.createTemp('upstream_phone_preflight_');
      final permission = Completer<bool>();
      final entered = Completer<void>();
      final registry = AudioCaptureOwnership();
      final harness = await EllaUpstreamCaptureHarness.boot(
        tempDir: directory,
        phoneMicWrapper: (native) =>
            ArbitratedMic(inner: native, arbiter: MicArbiter(ownership: registry), owner: 'phone'),
        permissionRequester: () {
          if (!entered.isCompleted) entered.complete();
          return permission.future;
        },
      );
      try {
        expect(await harness.bind(), isTrue);
        final start = harness.provider.streamRecording();
        final observed = start.then<Object?>((_) => null, onError: (Object error) => error);
        await entered.future;
        expect(registry.isReserved, isTrue);
        expect(harness.hostApi.nativeRecording, isFalse);
        expect(registry.reserve(source: 'dictation', hasAuthority: () => true), isNull);
        permission.complete(granted);
        final outcome = await observed;
        if (granted) {
          expect(outcome, isNull);
          expect(harness.hostApi.nativeRecording, isTrue);
          expect(registry.isReserved, isTrue);
          await harness.provider.stopStreamRecording();
          await harness.settle();
          expect(harness.hostApi.nativeRecording, isFalse);
          expect(registry.isReserved, isFalse);
        } else {
          expect(outcome, isA<StateError>());
          expect(harness.hostApi.nativeRecording, isFalse);
          expect(registry.isReserved, isFalse);
        }
      } finally {
        if (!permission.isCompleted) permission.complete(false);
        await harness.dispose();
        if (directory.existsSync()) directory.deleteSync(recursive: true);
      }
    });
  }
  test('actual upstream account drift while permission held cannot start native mic', () async {
    final directory = await Directory.systemTemp.createTemp('upstream_preflight_account_');
    final permission = Completer<bool>();
    final entered = Completer<void>();
    final registry = AudioCaptureOwnership();
    final harness = await EllaUpstreamCaptureHarness.boot(
      tempDir: directory,
      phoneMicWrapper: (native) =>
          ArbitratedMic(inner: native, arbiter: MicArbiter(ownership: registry), owner: 'phone'),
      permissionRequester: () {
        if (!entered.isCompleted) entered.complete();
        return permission.future;
      },
    );
    try {
      expect(await harness.bind(), isTrue);
      final start = harness.provider.streamRecording();
      final observed = start.then<Object?>((_) => null, onError: (Object error) => error);
      await entered.future;
      expect(registry.isReserved, isTrue);
      harness.switchAccount(accountB);
      permission.complete(true);
      expect(await observed, isA<StateError>());
      expect(harness.hostApi.nativeRecording, isFalse);
      expect(registry.isReserved, isFalse);
      final next = registry.reserve(source: 'replacement', hasAuthority: () => true)!;
      expect(next.isCurrent, isTrue);
      await next.stop();
    } finally {
      if (!permission.isCompleted) permission.complete(false);
      await harness.dispose();
      if (directory.existsSync()) directory.deleteSync(recursive: true);
    }
  });
}
