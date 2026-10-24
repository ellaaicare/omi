import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/upstream_capture/gen/phone_mic_pigeon.g.dart';
import 'package:omi/upstream_capture/services/mic/native_mic_recorder_service.dart';
import 'package:omi/upstream_capture/services/mic/mic_arbiter.dart';
import 'owned_capture_controller_test.dart' show Rig, settle;

class RawHost extends PhoneMicHostApi {
  int starts = 0, stops = 0;
  @override
  Future<void> start(PhoneMicCaptureMode mode, int sessionId) async {
    starts++;
  }

  @override
  Future<void> stop() async {
    stops++;
  }

  @override
  Future<bool> isRecording() async => false;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('actual arbiter preflight adopts exact optional native controller before permission', () async {
    final rig = Rig()..permission = Completer();
    final raw = RawHost();
    final native = NativeMicRecorderService(hostApi: raw, registerFlutterApi: false, ownedController: rig.controller);
    final arbiter = MicArbiter(ownership: rig.registry);
    final mic = ArbitratedMic(inner: native, arbiter: arbiter, owner: 'phone');
    final preflight = mic.reserveCapturePreflight(hasAuthority: () => rig.authority);
    expect(preflight, isNotNull);
    expect(rig.controller.hasReservation, true);
    expect(rig.permissions, 0);
    expect(rig.registry.reserve(source: 'speech', hasAuthority: () => true), isNull);
    final start = mic.start(onByteReceived: (_) {});
    final failure = expectLater(start, throwsStateError);
    await settle();
    await mic.stopAndConfirm();
    rig.permission!.complete(true);
    await failure;
    expect(raw.starts, 0);
    expect(raw.stops, 0);
    expect(rig.starts, 0);
  });
  test('actual native/arbiter starts only one lease and drains private tail', () async {
    final rig = Rig();
    final raw = RawHost();
    final native = NativeMicRecorderService(hostApi: raw, registerFlutterApi: false, ownedController: rig.controller);
    final mic = ArbitratedMic(inner: native, arbiter: MicArbiter(ownership: rig.registry), owner: 'phone');
    var published = 0;
    await mic.start(onByteReceived: (_) => published++);
    await rig.frame();
    expect(published, 1);
    await mic.stopAndConfirm();
    expect(rig.registry.isReserved, false);
    expect(rig.starts, 1);
    expect(raw.starts, 0);
    expect(raw.stops, 0);
  });
  test('unowned stop on opted-in NativeMic never invokes raw native', () async {
    final rig = Rig();
    final raw = RawHost();
    final native = NativeMicRecorderService(hostApi: raw, registerFlutterApi: false, ownedController: rig.controller);
    native.stop();
    await native.stopAndConfirm();
    expect(raw.stops, 0);
    expect(rig.stops, 0);
  });
  test('unused actual preflight cancels without permission or prepare', () async {
    final rig = Rig();
    final raw = RawHost();
    final native = NativeMicRecorderService(hostApi: raw, registerFlutterApi: false, ownedController: rig.controller);
    final mic = ArbitratedMic(inner: native, arbiter: MicArbiter(ownership: rig.registry), owner: 'phone');
    final ticket = mic.reserveCapturePreflight(hasAuthority: () => true)!;
    expect(await ticket.cancelBeforeStart(), true);
    expect(rig.registry.isReserved, false);
    expect(rig.permissions, 0);
    expect(rig.prepares, 0);
    expect(raw.stops, 0);
  });
  test('actual optional native cannot overlap legacy batch; completed batch then owned stop uses exact drain',
      () async {
    final rig = Rig();
    final raw = RawHost();
    final native = NativeMicRecorderService(hostApi: raw, registerFlutterApi: false, ownedController: rig.controller);
    await native.startBatch();
    await expectLater(native.start(onByteReceived: (_) {}), throwsStateError);
    expect(rig.prepares, 0);
    await native.stopAndConfirm();
    expect(raw.stops, 1);
    await native.start(onByteReceived: (_) {});
    await native.stopAndConfirm();
    expect(raw.stops, 1);
    expect(rig.stops, 1);
    expect(rig.registry.isReserved, false);
  });
}
