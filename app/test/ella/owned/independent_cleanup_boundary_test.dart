import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/services/mic/owned/owned_capture_contract.dart';
import 'owned_physical_cleanup_policy_test.dart' show PhysicalRig;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('forbidden cleanup exact retry retains transaction and owner', () async {
    final r = PhysicalRig();
    await r.start();
    r.policy = const OwnedRetentionSnapshot(1, OwnedPrivateRetention.forbidden);
    await r.controller.retirePrivateRetention(r.policy);
    await expectLater(r.controller.stop(), throwsStateError);
    await expectLater(r.controller.stop(), throwsStateError);
    expect(r.stops, 2);
    expect(r.engineRunning, false);
    expect(r.registry.isReserved, true);
    expect(r.requests.map((x) => x.transactionId).toSet().length, 1);
    expect(r.requests.every((x) => x.handle.nativeEpoch == 11 && x.handle.binding.captureStartId == 'capture-A'), true);
  });
  test('failed unknown prepare does not guess physical cleanup handle', () async {
    final r = PhysicalRig()..prepareError = StateError('unknown_factory');
    await expectLater(r.start(), throwsStateError);
    await expectLater(r.controller.stop(), throwsStateError);
    expect(r.stops, 0);
    expect(r.requests, isEmpty);
    expect(r.probes, 0);
    expect(r.registry.isReserved, true);
  });
  test('known started engine with held start reply still receives exact stop request', () async {
    final r = PhysicalRig(timeout: const Duration(milliseconds: 80))..startHeld = Completer<void>();
    await expectLater(r.start(), throwsA(isA<TimeoutException>()));
    expect(r.engineRunning, true);
    r.policy = const OwnedRetentionSnapshot(1, OwnedPrivateRetention.forbidden);
    await r.controller.retirePrivateRetention(r.policy);
    await expectLater(r.controller.stop(), throwsStateError);
    final requestedBeforeStartSettled = r.stops;
    final runningBeforeStartSettled = r.engineRunning;
    r.startHeld!.complete();
    await Future<void>.delayed(const Duration(milliseconds: 20));
    expect(r.registry.isReserved, true);
    expect(requestedBeforeStartSettled, 1,
        reason: 'known prepared origin needs cleanup request even while native start reply is held');
    expect(runningBeforeStartSettled, false);
  });
}
