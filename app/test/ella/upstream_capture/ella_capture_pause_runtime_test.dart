import 'dart:async';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'support/ella_upstream_capture_harness.dart';

void main() {
  late Directory directory;
  late EllaUpstreamCaptureHarness h;
  setUp(() async {
    directory = await Directory.systemTemp.createTemp('ella_pause_runtime_');
    h = await EllaUpstreamCaptureHarness.boot(tempDir: directory);
    expect(await h.bind(), isTrue);
  });
  tearDown(() async {
    await h.dispose();
    if (directory.existsSync()) directory.deleteSync(recursive: true);
  });

  test('repeated phone Pause/Resume preserves conversation and silences paused frames', () async {
    var native = await h.startPhone();
    final session = h.runtime.captureSession!;
    for (var cycle = 0; cycle < 3; cycle++) {
      expect(h.runtime.canPauseCapture(session), isTrue);
      await h.runtime.setCapturePaused(session, paused: true);
      expect(h.runtime.isCapturePaused(session), isTrue);
      expect(h.hostApi.nativeRecording, isFalse);
      final before = h.audioFramesSent;
      h.injectPhoneFrames(10, sessionId: native, firstFrameIndex: cycle * 100);
      await h.settle();
      expect(h.audioFramesSent, before);
      await h.runtime.setCapturePaused(session, paused: false);
      expect(h.runtime.isCapturePaused(session), isFalse);
      expect(session.matches(h.runtime.captureSession), isTrue);
      native = h.hostApi.lastStartSessionId!;
      h.injectPhoneFrames(10, sessionId: native, firstFrameIndex: cycle * 100 + 10);
      await h.settle();
      expect(h.audioFramesSent, greaterThan(before));
    }
    expect(h.processCalls, 0);
  });

  test('official necklace Pause/Resume is not hidden by an Ella-only pending Finish flag', () async {
    await h.connectPendant();
    final session = h.runtime.captureSession!;
    h.runtime.finishPending.value = true;
    try {
      expect(h.runtime.canPauseCapture(session), true);
      await h.runtime.setCapturePaused(session, paused: true);
      expect(h.runtime.isCapturePaused(session), true);
      expect(h.runtime.canResumeCapture(session), true);
      await h.runtime.setCapturePaused(session, paused: false);
      expect(session.matches(h.runtime.captureSession), true);
      expect(h.processCalls, 0);
    } finally {
      h.runtime.finishPending.value = false;
    }
  });

  test('necklace Pause/Resume keeps session and gates paused packets', () async {
    final link = await h.connectPendant();
    final session = h.runtime.captureSession!;
    await h.runtime.setCapturePaused(session, paused: true);
    expect(h.runtime.isCapturePaused(session), isTrue);
    final before = h.audioFramesSent;
    link.emitAudio();
    await h.settle();
    expect(h.audioFramesSent, before);
    await h.runtime.setCapturePaused(session, paused: false);
    expect(session.matches(h.runtime.captureSession), isTrue);
    link.emitAudio();
    await h.settle();
    expect(h.audioFramesSent, greaterThan(before));
  });

  test('resume waits for native completion and rejects repeated pending requests', () async {
    await h.startPhone();
    final session = h.runtime.captureSession!;
    await h.runtime.setCapturePaused(session, paused: true);
    final gate = Completer<void>();
    h.hostApi.holdNextStart = gate;
    h.hostApi.nextStartEntered = Completer<void>();
    final resume = h.runtime.setCapturePaused(session, paused: false);
    await h.hostApi.nextStartEntered!.future;
    expect(h.runtime.pauseChangePending, isTrue);
    expect(h.runtime.isCapturePaused(session), isTrue);
    await expectLater(h.runtime.setCapturePaused(session, paused: false), throwsStateError);
    gate.complete();
    await resume;
    expect(h.runtime.pauseChangePending, isFalse);
    expect(h.runtime.isCapturePaused(session), isFalse);
  });

  for (final retirement in ['account', 'consent', 'window', 'widget']) {
    test('stale $retirement Pause callback emits no native effect', () async {
      await h.startPhone();
      final session = h.runtime.captureSession!;
      final before = h.hostApi.stopCalls;
      if (retirement == 'account') h.authenticatedUid = accountB;
      if (retirement == 'consent') h.authority.release();
      if (retirement == 'window') h.provider.clearUserData();
      await expectLater(
          h.runtime.setCapturePaused(session, paused: true, isCurrent: () => retirement != 'widget'), throwsStateError);
      expect(h.hostApi.stopCalls, before);
      expect(h.runtime.isCapturePaused(session), isFalse);
    });
  }

  test('failed native resume fails closed without pretending capture resumed', () async {
    await h.startPhone();
    final session = h.runtime.captureSession!;
    await h.runtime.setCapturePaused(session, paused: true);
    final gate = Completer<void>();
    h.hostApi.holdNextStart = gate;
    h.hostApi.nextStartEntered = Completer<void>();
    final resume = h.runtime.setCapturePaused(session, paused: false);
    final rejected = expectLater(resume, throwsA(anything));
    await h.hostApi.nextStartEntered!.future;
    gate.completeError(StateError('synthetic native start failure'));
    await rejected;
    expect(h.runtime.pauseChangePending, isFalse);
    expect(h.runtime.canPauseCapture(session), isFalse);
    expect(h.hostApi.nativeRecording, isFalse);
  });
  for (final retirement in ['account', 'consent', 'window', 'widget']) {
    test('retirement during pending native resume denies later frames: $retirement', () async {
      await h.startPhone();
      final session = h.runtime.captureSession!;
      await h.runtime.setCapturePaused(session, paused: true);
      final gate = Completer<void>();
      final entered = Completer<void>();
      h.hostApi.holdNextStart = gate;
      h.hostApi.nextStartEntered = entered;
      var current = true;
      final resume = h.runtime.setCapturePaused(session, paused: false, isCurrent: () => current);
      final rejected = expectLater(resume, throwsA(anything));
      await entered.future;
      if (retirement == 'account') h.authenticatedUid = accountB;
      if (retirement == 'consent') h.authority.release();
      if (retirement == 'window') h.provider.clearUserData();
      if (retirement == 'widget') current = false;
      final before = h.audioFramesSent;
      gate.complete();
      await rejected;
      h.injectPhoneFrames(20, sessionId: h.hostApi.lastStartSessionId!);
      await h.settle();
      expect(h.audioFramesSent, before);
      expect(h.hostApi.nativeRecording, isFalse);
      expect(h.runtime.canPauseCapture(session), isFalse);
    });
  }

  for (final outcome in ['acknowledged', 'rejected']) {
    test('phone Pause waits for native stop acknowledgement: $outcome', () async {
      await h.startPhone();
      final session = h.runtime.captureSession!;
      final gate = Completer<void>();
      final entered = Completer<void>();
      h.hostApi.holdNextStop = gate;
      h.hostApi.nextStopEntered = entered;
      final pause = h.runtime.setCapturePaused(session, paused: true);
      final rejection = outcome == 'rejected' ? expectLater(pause, throwsA(anything)) : null;
      await entered.future;
      expect(h.runtime.pauseChangePending, isTrue);
      expect(h.runtime.isCapturePaused(session), isFalse);
      expect(h.hostApi.nativeRecording, isTrue);
      if (outcome == 'rejected') h.hostApi.nextStopError = () => StateError('synthetic native stop rejected');
      gate.complete();
      if (rejection != null) {
        await rejection;
        expect(h.runtime.isCapturePaused(session), isFalse);
      } else {
        await pause;
        expect(h.runtime.isCapturePaused(session), isTrue);
        expect(h.hostApi.nativeRecording, isFalse);
      }
      expect(h.runtime.pauseChangePending, isFalse);
    });
  }
  test('Stop cancels held Resume promptly and late start cannot admit frames', () async {
    await h.startPhone();
    final session = h.runtime.captureSession!;
    await h.runtime.setCapturePaused(session, paused: true);
    final held = Completer<void>();
    final entered = Completer<void>();
    h.hostApi.holdNextStart = held;
    h.hostApi.nextStartEntered = entered;
    final resume = h.runtime.setCapturePaused(session, paused: false);
    final cancelled = expectLater(resume, throwsA(anything));
    await entered.future;
    final before = h.audioFramesSent;
    final stopped = expectLater(h.runtime.stopCaptureEscape(), throwsStateError);
    await cancelled.timeout(const Duration(seconds: 1));
    await stopped.timeout(const Duration(seconds: 1));
    expect(h.runtime.pauseChangePending, isFalse);
    expect(h.hostApi.nativeRecording, isFalse);
    held.complete();
    await h.settle();
    h.injectPhoneFrames(10, sessionId: h.hostApi.lastStartSessionId!);
    await h.settle();
    expect(h.audioFramesSent, before);
    expect(h.hostApi.nativeRecording, isFalse);
    expect(h.runtime.canPauseCapture(session), isFalse);
  });
}
