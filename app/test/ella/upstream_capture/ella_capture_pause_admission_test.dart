import 'dart:async';
import 'package:fake_async/fake_async.dart';
import 'package:omi/services/capture/capture_operation_cancellation.dart';

import 'package:flutter_test/flutter_test.dart';
import 'package:omi/upstream_capture/services/capture/capture_coordinator.dart';

CaptureCoordinator _coordinator(Future<Object?> Function(CaptureStage) runStage,
        {Future<void> Function(String)? persist,
        Future<void> Function()? start,
        Future<void> Function()? stop,
        CaptureRetirementFence? retirementFence,
        bool Function()? callActive}) =>
    CaptureCoordinator(
      retirementFence: retirementFence,
      readEnvironment: () => CaptureEnvironment(
        policyMuted: false,
        paused: false,
        batchModeEnabled: false,
        batchModeSuspendedForOnboarding: false,
        deviceSupportsTranscribeLater: false,
        networkConnected: true,
        signedIn: () => true,
        phoneMicSupportsBatch: false,
        transcriptReady: false,
        socketConnected: false,
        deviceServiceReady: false,
        callActive: callActive?.call() ?? false,
        deviceRecording: false,
        micCapturing: false,
      ),
      ports: CaptureEffectPorts(
        writePolicy: (_) async => const PolicyWriteOutcome(revision: 1, superseded: false),
        stopBleStream: ({bool disableNativeBackground = false}) async {},
        startBleStream: () async {},
        openSocket: (_) async {},
        closeSocket: (_) async {},
        startNativeMic: (_) async {
          await start?.call();
        },
        stopNativeMic: () async {
          await stop?.call();
        },
        setNativeWriterGate: (_, __) async {},
        finalizeWal: () async {},
        rollSession: (_) async {},
        mintRecordingId: (_, __) => 'synthetic-recording',
        checkPhonePermission: () async => true,
        readSnapshot: () => null,
        persistSnapshot: (encoded) async {
          await persist?.call(encoded);
        },
        runStage: runStage,
      ),
    );

void main() {
  for (final resume in [false, true]) {
    for (final retirement in ['account', 'consent', 'window', 'throw']) {
      test('queued ${resume ? "Resume" : "Pause"} refuses $retirement before effects', () async {
        final blocker = Completer<Object?>();
        var stages = 0;
        var current = true;
        var checks = 0;
        final coordinator = _coordinator((_) {
          stages++;
          return blocker.future;
        });
        final first = coordinator.dispatch(const FinishRequested());
        bool guard() {
          checks++;
          if (retirement == 'throw') throw StateError('synthetic guard');
          return current;
        }

        final queued = coordinator
            .dispatch(resume ? ResumeCaptureRequested(isCurrent: guard) : PauseCaptureRequested(isCurrent: guard));
        expect(stages, 1);
        expect(checks, 0);
        current = false;
        blocker.complete();
        await first;
        expect((await queued).admitted, isFalse);
        expect(stages, 1);
        expect(checks, 1);
        coordinator.dispose();
      });
    }
  }
  test('queued phase retirement denies a same-identity resume with no effects', () async {
    var stages = 0;
    final coordinator = _coordinator((_) async {
      stages++;
      return null;
    });
    final result = await coordinator.dispatch(ResumeCaptureRequested(isCurrent: () => true));
    expect(result.admitted, isFalse);
    expect(stages, 0);
    coordinator.dispose();
  });
  for (final failure in ['snapshot', 'retirement', 'cleanup']) {
    test('resume snapshot boundary fails closed: $failure', () async {
      Completer<void>? persistGate;
      var current = true;
      var stops = 0;
      var starts = 0;
      final coordinator = _coordinator((_) async => null, start: () async {
        starts++;
      }, stop: () async {
        stops++;
        if (failure == 'cleanup') throw StateError('synthetic cleanup failure');
      }, persist: (_) async {
        await persistGate?.future;
      });
      expect((await coordinator.dispatch(const PhoneStartRequested())).error, isNull);
      expect(coordinator.state.phase, CapturePhase.phoneLive);
      await coordinator.dispatch(PauseCaptureRequested(isCurrent: () => true));
      expect(coordinator.state.phase, CapturePhase.phonePaused);
      persistGate = Completer<void>();
      final resume = coordinator.dispatch(ResumeCaptureRequested(isCurrent: () => current));
      await Future<void>.delayed(Duration.zero);
      expect(starts, 1);
      expect(coordinator.state.phase, CapturePhase.phonePaused);
      if (failure == 'retirement') {
        current = false;
        persistGate.complete();
      } else {
        persistGate.completeError(StateError('synthetic snapshot failure'));
      }
      // Cleanup's best-effort second snapshot uses the already failed gate.
      final result = await resume;
      expect(result.error, isNotNull);
      expect(coordinator.state.phase, isNot(CapturePhase.phoneLive));
      expect(stops, 1);
      coordinator.dispose();
    });
  }
  test('cancelled held Resume releases queue but quarantines activation until late cleanup', () async {
    Completer<void>? gate;
    final entered = Completer<void>();
    var stops = 0;
    final coordinator = _coordinator((_) async => null, start: () async {
      if (gate != null) {
        entered.complete();
        await gate.future;
      }
    }, stop: () async {
      stops++;
    });
    await coordinator.dispatch(const PhoneStartRequested());
    await coordinator.dispatch(PauseCaptureRequested(isCurrent: () => true));
    gate = Completer<void>();
    final cancellation = CaptureOperationCancellation();
    final pending = coordinator
        .dispatch(ResumeCaptureRequested(isCurrent: () => !cancellation.isCancelled, cancellation: cancellation));
    await entered.future;
    cancellation.cancel();
    final result = await pending.timeout(const Duration(seconds: 1));
    expect(result.failed, isTrue);
    expect(coordinator.state.phase, CapturePhase.idle);
    expect((await coordinator.dispatch(const PhoneStartRequested())).admitted, isFalse);
    expect(
        (await coordinator.dispatch(
                const PhoneStopRequested(reason: 'user_stopped', userStop: true, resumeSuspendedPendant: false)))
            .admitted,
        isTrue);
    gate.complete();
    await Future<void>.delayed(Duration.zero);
    expect(stops, greaterThan(1));
    gate = null;
    expect((await coordinator.dispatch(const PhoneStartRequested())).admitted, isTrue);
    cancellation.dispose();
    coordinator.dispose();
  });
  test('Resume deadline cancels a never-settling native effect', () {
    fakeAsync((clock) {
      var held = false;
      final coordinator =
          _coordinator((_) async => null, start: () => held ? Completer<void>().future : Future.value());
      coordinator.dispatch(const PhoneStartRequested());
      clock.flushMicrotasks();
      coordinator.dispatch(PauseCaptureRequested(isCurrent: () => true));
      clock.flushMicrotasks();
      held = true;
      final cancellation = CaptureOperationCancellation(timeout: const Duration(seconds: 20))..arm();
      CaptureDispatchOutcome? outcome;
      coordinator
          .dispatch(ResumeCaptureRequested(isCurrent: () => !cancellation.isCancelled, cancellation: cancellation))
          .then((v) => outcome = v);
      clock.flushMicrotasks();
      expect(outcome, isNull);
      clock.elapse(const Duration(seconds: 20));
      clock.flushMicrotasks();
      expect(outcome!.failed, isTrue);
      expect(coordinator.state.phase, CapturePhase.idle);
      cancellation.dispose();
      coordinator.dispose();
    });
  });
  test('timed-out cleanup remains in diagnostic drain and replacement activation fence', () {
    fakeAsync((clock) {
      final fence = CaptureRetirementFence();
      var held = false;
      final start = Completer<void>();
      final stop = Completer<void>();
      final coordinator = _coordinator((_) async => null,
          retirementFence: fence,
          start: () => held ? start.future : Future.value(),
          stop: () => held ? stop.future : Future.value());
      coordinator.dispatch(const PhoneStartRequested());
      clock.flushMicrotasks();
      coordinator.dispatch(PauseCaptureRequested(isCurrent: () => true));
      clock.flushMicrotasks();
      held = true;
      final cancellation = CaptureOperationCancellation();
      CaptureDispatchOutcome? result;
      coordinator
          .dispatch(ResumeCaptureRequested(isCurrent: () => !cancellation.isCancelled, cancellation: cancellation))
          .then((v) => result = v);
      clock.flushMicrotasks();
      cancellation.cancel();
      clock.flushMicrotasks();
      clock.elapse(const Duration(seconds: 3));
      clock.flushMicrotasks();
      expect(result!.failed, isTrue);
      expect(coordinator.hasPendingEffectsForDiagnostic, isTrue);
      var drained = false;
      coordinator.pendingDrain.then((_) => drained = true);
      final replacement = _coordinator((_) async => null, retirementFence: fence);
      CaptureDispatchOutcome? admission;
      replacement.dispatch(const PhoneStartRequested()).then((v) => admission = v);
      clock.flushMicrotasks();
      expect(admission!.admitted, isFalse);
      coordinator.dispose();
      start.complete();
      clock.flushMicrotasks();
      expect(fence.pending, isTrue);
      expect(drained, isFalse);
      stop.complete();
      clock.flushMicrotasks();
      expect(fence.pending, isFalse);
      expect(drained, isTrue);
      replacement.dispatch(const PhoneStartRequested()).then((v) => admission = v);
      clock.flushMicrotasks();
      expect(admission!.admitted, isTrue);
      replacement.dispose();
      cancellation.dispose();
    });
  });
  test('quarantine preserves active call and synchronizes device disconnect without activation', () async {
    final fence = CaptureRetirementFence();
    fence.hold();
    var call = true;
    final stages = <CaptureStage>[];
    var starts = 0;
    final coordinator = _coordinator(
        (stage) async {
          stages.add(stage);
          return null;
        },
        retirementFence: fence,
        callActive: () => call,
        start: () async {
          starts++;
        });
    await coordinator.dispatch(const CallStateChanged());
    expect(coordinator.state.callActive, isTrue);
    await coordinator.dispatch(const DeviceUpdated(null));
    expect(coordinator.state.callActive, isTrue);
    expect(coordinator.state.connectedDevice, isNull);
    expect(stages.whereType<UpdateRecordingDeviceStage>().single.device, isNull);
    expect(starts, 0);
    fence.release();
    await coordinator.dispatch(const PhoneStartRequested());
    expect(starts, 0);
    expect(coordinator.state.callActive, isTrue);
    call = false;
    await coordinator.dispatch(const CallStateChanged());
    expect(coordinator.state.callActive, isFalse);
    expect((await coordinator.dispatch(const PhoneStartRequested())).admitted, isTrue);
    expect(coordinator.state.phase, CapturePhase.phoneLive);
    coordinator.dispose();
  });
}
