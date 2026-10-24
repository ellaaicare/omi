import 'dart:async';
import 'package:omi/services/capture/capture_operation_cancellation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/utils/enums.dart';
import 'package:omi/upstream_capture/services/capture/capture_coordinator.dart';

CaptureCoordinator _coordinator(Future<Object?> Function(CaptureStage) runStage,
        {Future<void> Function(String)? persist,
        Future<void> Function()? start,
        Future<void> Function()? stop,
        CaptureRetirementFence? retirementFence,
        bool Function()? callActive, bool micCapturing = false}) =>
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
        micCapturing: micCapturing,
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
  for (final retirement in ['owner', 'session']) {
    test('initially valid Stop retires by $retirement while queued before dequeue', () async {
      final blocker = Completer<Object?>();
      final entered = Completer<void>();
      var block = false;
      var checks = 0;
      var currentOwner = 'owner-a';
      var currentSession = 'session-a';
      var stops = 0;
      final coordinator = _coordinator((stage) async {
        if (stage is StopPhoneLiveStage || stage is StopPhoneBatchStage) stops++;
        if (block && !entered.isCompleted) {
          entered.complete();
          return blocker.future;
        }
        return null;
      }, stop: () async { stops++; }, micCapturing: true);
      try {
        await coordinator.dispatch(const PhoneStartRequested());
        expect(coordinator.state.phase, CapturePhase.phoneLive);
        bool guard() { checks++; return currentOwner == 'owner-a' && currentSession == 'session-a'; }
        expect(guard(), isTrue);
        checks = 0;
        block = true;
        final first = coordinator.dispatch(const KeepAliveTick(testingProbe: true));
        await entered.future;
        final pending = coordinator.dispatch(PhoneStopRequested(
          reason: 'user_stopped', userStop: true, resumeSuspendedPendant: false, isCurrent: guard));
        expect(checks, 0);
        if (retirement == 'owner') currentOwner = 'owner-b';
        if (retirement == 'session') currentSession = 'session-b';
        blocker.complete();
        await first;
        expect((await pending).admitted, isFalse);
        expect(checks, 1);
        expect(stops, 0);
        expect(coordinator.state.phase, CapturePhase.phoneLive);
      } finally {
        if (!blocker.isCompleted) blocker.complete();
        await coordinator.pendingDrain;
        coordinator.dispose();
      }
    });
  }
  for (final current in [true, false]) {
    test('quarantine conversion retains ${current ? "current" : "stale"} Stop guard and never resumes a producer', () async {
      final fence = CaptureRetirementFence();
      var phoneStarts = 0;
      var stops = 0;
      var checks = 0;
      final stages = <CaptureStage>[];
      final coordinator = _coordinator((stage) async {
        stages.add(stage);
        if (stage is StartPhoneSessionStage) phoneStarts++;
        return null;
      }, retirementFence: fence, stop: () async { stops++; });
      try {
        final pendant = BtDevice(id: 'fictional-pendant', name: 'Omi', type: DeviceType.omi, rssi: -40);
        await coordinator.dispatch(DeviceUpdated(pendant));
        await coordinator.dispatch(DeviceStartRequested(device: pendant));
        expect(coordinator.state.phase, CapturePhase.pendantLive);
        await coordinator.dispatch(const PhoneStartRequested());
        expect(coordinator.state.phase, CapturePhase.phoneLive);
        expect(coordinator.state.pendantSuspension?.reason, SuspendReason.phone);
        expect(coordinator.state.connectedDevice?.id, pendant.id);
        expect(phoneStarts, 1);
        stages.clear();
        fence.hold();
        final result = await coordinator.dispatch(PhoneStopRequested(
          reason: 'user_stopped', userStop: true, resumeSuspendedPendant: true,
          isCurrent: () { checks++; return current; }));
        expect(checks, 1);
        expect(result.admitted, current);
        expect(phoneStarts, 1);
        expect(stages.whereType<ResumeSuspendedPendantStage>(), isEmpty);
        expect(stages.whereType<StartDeviceSessionStage>(), isEmpty);
        if (current) {
          expect(stops, greaterThan(0));
          expect(coordinator.state.phase, CapturePhase.idle);
        } else {
          expect(stops, 0);
          expect(coordinator.state.phase, CapturePhase.phoneLive);
        }
      } finally {
        fence.release();
        await coordinator.pendingDrain;
        coordinator.dispose();
      }
    });
  }
}
