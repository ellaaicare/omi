import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:omi/upstream_capture/services/capture/capture_coordinator.dart';

CaptureCoordinator _coordinator(Future<Object?> Function(CaptureStage) runStage) => CaptureCoordinator(
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
        callActive: false,
        deviceRecording: false,
        micCapturing: false,
      ),
      ports: CaptureEffectPorts(
        writePolicy: (_) async => const PolicyWriteOutcome(revision: 1, superseded: false),
        stopBleStream: ({bool disableNativeBackground = false}) async {},
        startBleStream: () async {},
        openSocket: (_) async {},
        closeSocket: (_) async {},
        startNativeMic: (_) async {},
        stopNativeMic: () async {},
        setNativeWriterGate: (_, __) async {},
        finalizeWal: () async {},
        rollSession: (_) async {},
        mintRecordingId: (_, __) => 'synthetic-recording',
        checkPhonePermission: () async => true,
        readSnapshot: () => null,
        persistSnapshot: (_) async {},
        runStage: runStage,
      ),
    );

void main() {
  for (final retirement in ['account switch', 'consent loss', 'recording rollover', 'window rollover']) {
    test('queued Finish checks $retirement at execution and emits no effects', () async {
      final blocker = Completer<Object?>();
      var stages = 0;
      var current = true;
      var guardCalls = 0;
      final coordinator = _coordinator((_) {
        stages++;
        return blocker.future;
      });
      final first = coordinator.dispatch(const FinishRequested());
      final queued = coordinator.dispatch(FinishRequested(isCurrent: () {
        guardCalls++;
        return current;
      }));
      expect(stages, 1);
      expect(guardCalls, 0, reason: 'checking at enqueue would miss retirement');
      current = false;
      blocker.complete();
      await first;
      expect((await queued).admitted, isFalse);
      expect(guardCalls, 1);
      expect(stages, 1, reason: 'no second processing or save effect');
      coordinator.dispose();
    });
  }

  test('throwing execution-time predicate fails closed without effects', () async {
    var stages = 0;
    final coordinator = _coordinator((_) async {
      stages++;
      return null;
    });
    final result =
        await coordinator.dispatch(FinishRequested(isCurrent: () => throw StateError('synthetic stale owner')));
    expect(result.admitted, isFalse);
    expect(stages, 0);
    expect(coordinator.state.phase, CapturePhase.idle);
    coordinator.dispose();
  });

  test('current guarded Finish preserves the existing processing path', () async {
    final stages = <CaptureStage>[];
    final coordinator = _coordinator((stage) async {
      stages.add(stage);
      return null;
    });
    final result = await coordinator.dispatch(FinishRequested(isCurrent: () => true));
    expect(result.admitted, isTrue);
    expect(stages, [isA<ProcessConversationStage>()]);
    coordinator.dispose();
  });
}
