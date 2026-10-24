import 'dart:async';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'support/ella_upstream_capture_harness.dart';

void main() {
  for (final rawFinish in [false, true]) {
    for (final retire in [false, true]) {
      test(
          '${rawFinish ? 'raw' : 'facade'} Finish resumed pendant Disconnect ${retire ? 'refuses retired owner' : 'serializes its own processing'}',
          () async {
        final dir = await Directory.systemTemp.createTemp('resumed_disconnect_');
        final h = await EllaUpstreamCaptureHarness.boot(tempDir: dir);
        Future<void>? disconnect;
        var observed = false;
        void Function()? listener;
        try {
          expect(await h.bind(), isTrue);
          await h.connectPendant();
          final phone = await h.startPhone();
          h.injectPhoneFrames(10, sessionId: phone);
          await h.settle();
          final original = h.runtime.captureSession!;
          listener = () {
            final current = h.runtime.captureSession;
            if (observed || current?.source != 'omi' || current!.recordingId == original.recordingId) return;
            observed = true;
            expect(current.canFinish, isTrue);
            disconnect = h.runtime.disconnectNecklace();
            expect(identical(disconnect, h.runtime.disconnectNecklace()), isTrue);
            if (retire) h.switchAccount(accountB);
          };
          h.provider.addListener(listener);
          if (rawFinish) {
            await h.runtime.finishConversation();
          } else {
            await h.runtime.finishCaptureSession(original);
          }
          expect(observed, isTrue);
          await disconnect;
          await h.settle();
          expect(h.processCalls, retire ? 1 : 2);
          expect(h.disconnectedDevices, hasLength(retire ? 0 : 1));
          if (!retire) expect(h.provider.recordingDevice, isNull);
        } finally {
          if (listener != null) h.provider.removeListener(listener);
          await h.dispose();
          if (dir.existsSync()) dir.deleteSync(recursive: true);
        }
      });
    }
  }

  test('replacement queued at resumed boundary is not processed or stopped by old Disconnect', () async {
    final dir = await Directory.systemTemp.createTemp('resumed_replacement_');
    final h = await EllaUpstreamCaptureHarness.boot(tempDir: dir);
    Future<void>? disconnect;
    Future<void>? replacement;
    var observed = false;
    void Function()? listener;
    try {
      expect(await h.bind(), isTrue);
      await h.connectPendant();
      final phone = await h.startPhone();
      h.injectPhoneFrames(10, sessionId: phone);
      await h.settle();
      final original = h.runtime.captureSession!;
      listener = () {
        final current = h.runtime.captureSession;
        if (observed || current?.source != 'omi' || current!.recordingId == original.recordingId) return;
        observed = true;
        // Queue the replacement while the original Finish still owns the
        // coordinator, then request Disconnect of the resumed pendant.
        replacement = h.provider.streamRecording();
        disconnect = expectLater(h.runtime.disconnectNecklace(), throwsA(isA<StateError>()));
      };
      h.provider.addListener(listener);
      await h.runtime.finishCaptureSession(original);
      await replacement;
      await disconnect;
      await h.settle();
      expect(observed, isTrue);
      expect(h.runtime.captureSession?.source, 'phone');
      expect(h.processCalls, 1);
      expect(h.disconnectedDevices, isEmpty);
      expect(h.provider.recordingDevice, isNotNull);
    } finally {
      if (listener != null) h.provider.removeListener(listener);
      await h.dispose();
      if (dir.existsSync()) dir.deleteSync(recursive: true);
    }
  });
}
