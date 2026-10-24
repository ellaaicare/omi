import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/services/mic/owned/owned_capture_contract.dart';
import 'package:omi/services/mic/owned/owned_phone_mic.g.dart';
import 'owned_capture_controller_test.dart' show settle;
import 'owned_physical_cleanup_policy_test.dart' show PhysicalRig;

class LateStartRig extends PhysicalRig {
  Object? lateFrameError;
  @override
  Future<void> startPreparedOwnedStream(OwnedMicHandle handle, String token) async {
    await super.startPreparedOwnedStream(handle, token);
    // Fictional hostile late installation after first physical teardown.
    engineRunning = true;
    try {
      await frame();
    } catch (error) {
      lateFrameError = error;
    }
  }
}

class EarlyCompleteReceiptRig extends PhysicalRig {
  EarlyCompleteReceiptRig() {
    noLocalCommit = true;
  }
  @override
  Future<OwnedMicStopReceipt> stopAndDrain(OwnedMicStopRequest request) async {
    final receipt = await super.stopAndDrain(request);
    // Second request must provide actual local proof; first fictional native
    // completed receipt without local ACK may never authorize release.
    noLocalCommit = false;
    return receipt;
  }
}

class FailingBothCleanupRig extends PhysicalRig {
  @override
  Future<OwnedMicStopReceipt> stopAndDrain(OwnedMicStopRequest request) async {
    try {
      await super.stopAndDrain(request);
    } catch (_) {}
    throw StateError(stops == 1 ? 'first_cleanup_failure' : 'second_cleanup_failure');
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  for (final policy in OwnedPrivateRetention.values) {
    test('held late startup ${policy.name} receives immediate cleanup and fresh post-start cleanup', () async {
      final r = LateStartRig()..startHeld = Completer<void>();
      var recorded = 0;
      final start = r.controller.start(onByteReceived: (_) => r.publications++, onRecording: () => recorded++);
      final failedStart = expectLater(start, throwsStateError);
      await settle();
      expect(r.engineRunning, true);
      if (policy != OwnedPrivateRetention.retain) {
        r.policy = OwnedRetentionSnapshot(1, policy);
        if (policy == OwnedPrivateRetention.quarantine) r.authority = false;
        await r.controller.retirePrivateRetention(r.policy);
      }
      final stop = r.controller.stop();
      final result = policy == OwnedPrivateRetention.forbidden ? expectLater(stop, throwsStateError) : stop;
      await settle();
      expect(r.stops, 1);
      expect(r.engineRunning, false);
      expect(r.registry.isReserved, true);
      expect(r.saved, isNull);
      r.startHeld!.complete();
      await failedStart;
      await result;
      expect(r.stops, 2);
      expect(r.engineRunning, false);
      expect(r.registry.isReserved, policy == OwnedPrivateRetention.forbidden);
      expect(recorded, 0);
      expect(r.publications, 0);
      expect(r.requests.map((q) => q.transactionId).toSet().length, 1);
      expect(
          r.requests.every((q) => q.handle.nativeEpoch == 11 && q.handle.binding.captureStartId == 'capture-A'), true);
      if (policy == OwnedPrivateRetention.forbidden) {
        expect(r.lateFrameError, isA<StateError>());
        expect(r.bytes, isEmpty);
      } else {
        expect(r.bytes, [1, 2]);
        expect(r.lateFrameError, isNull);
      }
    });
  }
  test('first fictional complete stop receipt cannot release before held start settles', () async {
    final r = EarlyCompleteReceiptRig()..startHeld = Completer<void>();
    final start = r.start();
    final failedStart = expectLater(start, throwsStateError);
    await settle();
    final stop = r.controller.stop();
    await settle();
    expect(r.stops, 1);
    expect(r.saved, isNotNull);
    expect(r.registry.isReserved, true);
    r.startHeld!.complete();
    await failedStart;
    await stop;
    expect(r.stops, 2);
    expect(r.registry.isReserved, false);
  });
  test('late account ABA keeps exact owner after both physical cleanup requests', () async {
    final r = LateStartRig()..startHeld = Completer<void>();
    final start = r.start();
    final failedStart = expectLater(start, throwsStateError);
    await settle();
    final stop = r.controller.stop();
    final held = expectLater(stop, throwsStateError);
    await settle();
    expect(r.stops, 1);
    r.authorityEpoch = 5;
    r.startHeld!.complete();
    await failedStart;
    await held;
    expect(r.stops, 2);
    expect(r.engineRunning, false);
    expect(r.registry.isReserved, true);
    expect(r.publications, 0);
  });
  test('first cleanup failure remains primary when post-start exact cleanup also fails', () async {
    final r = FailingBothCleanupRig()..startHeld = Completer<void>();
    final start = r.start();
    final failedStart = expectLater(start, throwsStateError);
    await settle();
    final stop = r.controller.stop();
    Object? reported;
    final held = stop.catchError((Object error) {
      reported = error;
    });
    await settle();
    expect(r.stops, 1);
    r.startHeld!.complete();
    await failedStart;
    await held;
    expect(reported.toString(), contains('first_cleanup_failure'));
    expect(r.stops, 2);
    expect(r.registry.isReserved, true);
    expect(r.requests.map((q) => q.transactionId).toSet().length, 1);
  });
}
