import 'dart:async';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'support/ella_upstream_capture_harness.dart';

void main() {
  test('reentrant Disconnect of resumed pendant finalizes its own conversation after phone Finish', () async {
    final dir = await Directory.systemTemp.createTemp('finish_resume_disconnect_');
    final h = await EllaUpstreamCaptureHarness.boot(tempDir: dir);
    Future<void>? disconnect;
    var observedBoundary = false;
    var admittedNewPendant = false;
    void Function()? listener;
    try {
      expect(await h.bind(), isTrue);
      await h.connectPendant();
      final phone = await h.startPhone();
      h.injectPhoneFrames(10, sessionId: phone);
      await h.settle();
      final oldSession = h.runtime.captureSession!;
      expect(oldSession.source, 'phone');
      listener = () {
        final now = h.runtime.captureSession;
        if (observedBoundary || !h.runtime.finishPending.value || now?.source != 'omi') return;
        observedBoundary = true;
        admittedNewPendant = now!.recordingId != oldSession.recordingId && now.canFinish;
        disconnect = h.runtime.disconnectNecklace();
      };
      h.provider.addListener(listener);
      await h.runtime.finishCaptureSession(oldSession);
      expect(observedBoundary, isTrue, reason: 'new pendant is published while old phone Finish remains pending');
      expect(admittedNewPendant, isTrue, reason: 'the resumed producer has a new finishable recording identity');
      await disconnect;
      await h.settle();
      expect(h.disconnectedDevices, hasLength(1));
      expect(h.provider.recordingDevice, isNull);
      expect(h.processCalls, 2, reason: 'phone Finish and new pendant Disconnect each need their own processing stage');
    } finally {
      if (listener != null) h.provider.removeListener(listener);
      await h.dispose();
      if (dir.existsSync()) dir.deleteSync(recursive: true);
    }
  });
}
