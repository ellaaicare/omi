import 'dart:async';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:fake_async/fake_async.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';
import 'package:omi/upstream_capture/gen/phone_mic_pigeon.g.dart';
import 'package:omi/upstream_capture/services/mic/mic_arbiter.dart';
import 'package:omi/upstream_capture/services/mic/native_mic_recorder_service.dart';

class _Host extends PhoneMicHostApi {
  int starts = 0;
  int stops = 0;
  int session = 0;
  bool recording = false;
  bool failStart = false;
  Completer<void>? startGate;
  Completer<void>? stopGate;
  Completer<bool>? probeGate;
  @override
  Future<void> start(PhoneMicCaptureMode mode, int sessionId) async {
    starts++;
    session = sessionId;
    await startGate?.future;
    if (failStart) throw PlatformException(code: 'unknown_native_state');
  }

  @override
  Future<void> stop() async {
    stops++;
    await stopGate?.future;
  }

  @override
  Future<bool> isRecording() async => probeGate == null ? recording : await probeGate!.future;
}

void main() {
  test('native start timeout cannot release global owner before late host settlement and a fresh stop proof', () {
    fakeAsync((clock) {
      final ownership = AudioCaptureOwnership();
      final host = _Host()..startGate = Completer<void>();
      final native = NativeMicRecorderService(hostApi: host, registerFlutterApi: false);
      final owner = ArbitratedMic(inner: native, arbiter: MicArbiter(ownership: ownership), owner: 'phone');
      var startFailed = false;
      owner.start(onByteReceived: (_) {}).catchError((_) {
        startFailed = true;
      });
      clock.flushMicrotasks();
      clock.elapse(const Duration(seconds: 12));
      clock.flushMicrotasks();
      expect(startFailed, isTrue);
      var stopFailed = false;
      owner.stopAndConfirm().catchError((_) {
        stopFailed = true;
      });
      clock.flushMicrotasks();
      clock.elapse(const Duration(seconds: 10));
      clock.flushMicrotasks();
      expect(stopFailed, isTrue);
      expect(ownership.reserve(source: 'dictation', hasAuthority: () => true), isNull);
      host.startGate!.complete();
      clock.flushMicrotasks();
      expect(ownership.isReserved, isTrue, reason: 'late completion is not an owner release');
      var stopped = false;
      owner.stopAndConfirm().then((_) {
        stopped = true;
      });
      clock.flushMicrotasks();
      expect(stopped, isTrue);
      expect(ownership.isReserved, isFalse);
      expect(host.stops, greaterThanOrEqualTo(2));
    });
  });

  test('actual native stop receipt holds owner until host stop and recording probe settle', () async {
    final ownership = AudioCaptureOwnership();
    final host = _Host()
      ..stopGate = Completer<void>()
      ..probeGate = Completer<bool>();
    final native = NativeMicRecorderService(hostApi: host, registerFlutterApi: false);
    final arbiter = MicArbiter(ownership: ownership);
    final owner = ArbitratedMic(inner: native, arbiter: arbiter, owner: 'phone');
    final unowned = ArbitratedMic(inner: native, arbiter: arbiter, owner: 'phone');
    var frames = 0;
    await owner.start(onByteReceived: (_) => frames++);
    final session = host.session;
    final stopping = owner.stopAndConfirm();
    await Future<void>.delayed(Duration.zero);
    await unowned.stopAndConfirm();
    expect(host.stops, 1);
    expect(ownership.isReserved, isTrue);
    native.onAudioFrame(Uint8List.fromList([1]), session);
    expect(frames, 0);
    host.stopGate!.complete();
    await Future<void>.delayed(Duration.zero);
    expect(ownership.isReserved, isTrue);
    host.probeGate!.complete(false);
    await stopping;
    expect(ownership.isReserved, isFalse);
  });
  test('actual native recording=true probe cannot authorize replacement, owner retry can', () async {
    final ownership = AudioCaptureOwnership();
    final host = _Host()..recording = true;
    final native = NativeMicRecorderService(hostApi: host, registerFlutterApi: false);
    final owner = ArbitratedMic(inner: native, arbiter: MicArbiter(ownership: ownership), owner: 'phone');
    await owner.start(onByteReceived: (_) {});
    await expectLater(owner.stopAndConfirm(), throwsStateError);
    expect(ownership.reserve(source: 'dictation', hasAuthority: () => true), isNull);
    host.recording = false;
    await owner.stopAndConfirm();
    expect(ownership.isReserved, isFalse);
    expect(host.stops, 2);
  });
  test('actual native held start settles before exact-owner teardown reaches host', () async {
    final ownership = AudioCaptureOwnership();
    final host = _Host()..startGate = Completer<void>();
    final native = NativeMicRecorderService(hostApi: host, registerFlutterApi: false);
    final owner = ArbitratedMic(inner: native, arbiter: MicArbiter(ownership: ownership), owner: 'phone');
    var recorded = 0;
    final starting = owner.start(onByteReceived: (_) {}, onRecording: () => recorded++);
    final stopping = owner.stopAndConfirm();
    expect(host.stops, 0);
    native.onStateChanged(PhoneMicCaptureState.running, host.session);
    expect(recorded, 0);
    host.startGate!.complete();
    await starting;
    await stopping;
    expect(host.stops, 1);
    expect(ownership.isReserved, isFalse);
  });
}
