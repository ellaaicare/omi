import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'support/ella_upstream_capture_harness.dart';

void main() {
  test('explicit Create memory processes once without disconnecting or muting the necklace', () async {
    final directory = await Directory.systemTemp.createTemp('manual_memory_');
    final h = await EllaUpstreamCaptureHarness.boot(tempDir: directory);
    try {
      expect(await h.bind(), isTrue);
      final link = await h.connectPendant();
      for (var i = 0; i < 3; i++) { link.emitAudio(); }
      await h.settle();
      final session = h.runtime.captureSession!;
      final sent = h.audioFramesSent;
      expect(sent, greaterThan(0));
      await h.runtime.finishCaptureSession(session, stopDeviceIngress: false);
      await h.settle();
      expect(h.processCalls, 1);
      expect(h.disconnectedDevices, isEmpty);
      expect(h.provider.recordingDevice, isNotNull);
      // A second click for the retired window must not resubmit its request.
      await h.runtime.finishCaptureSession(session, stopDeviceIngress: false);
      expect(h.processCalls, 1);
      for (var i = 0; i < 3; i++) { link.emitAudio(); }
      await h.settle();
      expect(h.audioFramesSent, greaterThan(sent), reason: 'same necklace continues supplying audio');
    } finally {
      await h.dispose();
      if (directory.existsSync()) directory.deleteSync(recursive: true);
    }
  });
}
