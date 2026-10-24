import 'dart:async';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/services/mic/owned/owned_capture_contract.dart';
import 'package:omi/services/mic/owned/owned_phone_mic.g.dart';
import 'owned_capture_controller_test.dart' show Rig;

class PhysicalRig extends Rig {
  PhysicalRig({Duration timeout = const Duration(seconds: 2)}) : super(timeout: timeout);
  Object? probeError;
  bool engineRunning = false;
  int probes = 0;
  @override
  Future<void> startPreparedOwnedStream(OwnedMicHandle h, String token) async {
    engineRunning = true;
    await super.startPreparedOwnedStream(h, token);
  }

  @override
  Future<OwnedMicStopReceipt> stopAndDrain(OwnedMicStopRequest request) async {
    engineRunning = false;
    return super.stopAndDrain(request);
  }

  @override
  Future<OwnedMicProbe> probeOwnedCapture(OwnedMicHandle h) async {
    probes++;
    if (probeError != null) throw probeError!;
    return super.probeOwnedCapture(h);
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('confirmed forbidden retirement still attempts exact physical cleanup but cannot release', () async {
    final r = PhysicalRig();
    await r.start();
    expect(r.engineRunning, true);
    r.policy = const OwnedRetentionSnapshot(1, OwnedPrivateRetention.forbidden);
    await r.controller.retirePrivateRetention(r.policy);
    await expectLater(r.controller.stop(), throwsStateError);
    expect(r.stops, 1);
    expect(r.engineRunning, false);
    expect(r.probes, greaterThanOrEqualTo(2));
    expect(r.registry.isReserved, true);
    await expectLater(r.frame(), throwsStateError);
    expect(r.bytes, isEmpty);
  });
  test('unacknowledged quarantine change still attempts exact physical cleanup with no release', () async {
    final r = PhysicalRig();
    await r.start();
    r.policy = const OwnedRetentionSnapshot(1, OwnedPrivateRetention.quarantine);
    await expectLater(r.controller.stop(), throwsStateError);
    expect(r.stops, 1);
    expect(r.engineRunning, false);
    expect(r.probes, greaterThanOrEqualTo(2));
    expect(r.registry.isReserved, true);
  });
  test('malformed start error with verified prepared origin cleans exact handle but stays held', () async {
    final r = PhysicalRig()
      ..startError =
          PlatformException(code: 'owned-start-held', details: {'captureStartId': 'foreign', 'nativeEpoch': true});
    await expectLater(r.start(), throwsA(isA<PlatformException>()));
    expect(r.engineRunning, true);
    await expectLater(r.controller.stop(), throwsStateError);
    expect(r.stops, 1);
    expect(r.engineRunning, false);
    expect(r.probes, greaterThanOrEqualTo(1));
    expect(r.registry.isReserved, true);
    expect(r.requests.single.handle.nativeEpoch, 11);
    expect(r.requests.single.handle.binding.captureStartId, 'capture-A');
  });
  test('failed independent cleanup probe preserves primary retention failure and exact owner', () async {
    final r = PhysicalRig();
    await r.start();
    r.probeError = StateError('probe_failure');
    r.policy = const OwnedRetentionSnapshot(1, OwnedPrivateRetention.forbidden);
    await r.controller.retirePrivateRetention(r.policy);
    Object? error;
    try {
      await r.controller.stop();
    } catch (e) {
      error = e;
    }
    expect(error.toString(), contains('owned_retention_unconfirmed'));
    expect(r.stops, 1);
    expect(r.engineRunning, false);
    expect(r.registry.isReserved, true);
  });
  test('held forbidden drain timeout independently probes physical cleanup without release', () async {
    final r = PhysicalRig(timeout: const Duration(milliseconds: 80))..stopHeld = Completer<void>();
    await r.start();
    r.policy = const OwnedRetentionSnapshot(1, OwnedPrivateRetention.forbidden);
    await r.controller.retirePrivateRetention(r.policy);
    await expectLater(r.controller.stop(), throwsStateError);
    await Future<void>.delayed(Duration.zero);
    expect(r.stops, 1);
    expect(r.engineRunning, false);
    expect(r.probes, greaterThanOrEqualTo(2));
    expect(r.registry.isReserved, true);
    r.stopHeld!.complete();
    await Future<void>.delayed(const Duration(milliseconds: 20));
    expect(r.registry.isReserved, true);
  });
}
