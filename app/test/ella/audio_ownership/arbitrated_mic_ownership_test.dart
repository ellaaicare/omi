import 'dart:async';
import 'dart:typed_data';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';
import 'package:omi/services/mic/confirmed_mic_stop.dart';
import 'package:omi/upstream_capture/services/mic/mic_arbiter.dart';
import 'package:omi/upstream_capture/services/services.dart';

class _Mic implements IMicRecorderService, ConfirmedMicStop {
  int starts = 0;
  int stops = 0;
  int bestEffortStops = 0;
  Completer<void>? startGate;
  Completer<void>? stopGate;
  bool failStart = false;
  bool failStop = false;
  final callbacks = <Function()?>[];
  final frames = <Function(Uint8List)>[];
  @override
  Future<void> start(
      {required Function(Uint8List) onByteReceived,
      Function()? onRecording,
      Function()? onStop,
      Function()? onInitializing,
      Function()? onStalled,
      Function(bool)? onInterruption}) async {
    starts++;
    callbacks.add(onStop);
    frames.add(onByteReceived);
    await startGate?.future;
    if (failStart) throw StateError('ambiguous start');
    onRecording?.call();
  }

  @override
  Future<void> startBatch(
          {Function()? onStop,
          Function(bool)? onInterruption,
          Function()? onBatchStalled,
          Function(String, String)? onError}) =>
      start(onByteReceived: (_) {}, onStop: onStop);
  @override
  void stop() => bestEffortStops++;
  @override
  Future<void> stopAndConfirm() async {
    stops++;
    await stopGate?.future;
    if (failStop) throw StateError('no physical acknowledgement');
  }

  @override
  void probeStallAfterForeground() {}
}

void main() {
  late AudioCaptureOwnership registry;
  late MicArbiter arbiter;
  setUp(() {
    registry = AudioCaptureOwnership();
    arbiter = MicArbiter(ownership: registry);
  });
  ArbitratedMic wrap(_Mic mic, String owner) => ArbitratedMic(inner: mic, arbiter: arbiter, owner: owner);

  test('unowned stop never reaches physical native recorder', () async {
    final physical = _Mic();
    final first = wrap(physical, 'same-label');
    final other = wrap(physical, 'same-label');
    await first.start(onByteReceived: (_) {});
    await other.stopAndConfirm();
    other.stop();
    expect(physical.stops, 0);
    expect(physical.bestEffortStops, 0);
    expect(arbiter.owner, 'same-label');
    await first.stopAndConfirm();
  });
  test('late onStop and frames cannot release or publish into replacement with same label', () async {
    final physical = _Mic();
    final owner = wrap(physical, 'conversation');
    final received = <int>[];
    await owner.start(onByteReceived: (bytes) => received.add(bytes.first));
    final oldStop = physical.callbacks.single!;
    final oldFrame = physical.frames.single;
    await owner.stopAndConfirm();
    await owner.start(onByteReceived: (bytes) => received.add(bytes.first));
    oldStop();
    oldFrame(Uint8List.fromList([1]));
    await Future<void>.delayed(Duration.zero);
    expect(arbiter.owner, 'conversation');
    expect(received, isEmpty);
    physical.frames.last(Uint8List.fromList([2]));
    expect(received, [2]);
    expect(physical.stops, 1);
    await owner.stopAndConfirm();
  });
  test('permission reservation blocks all other recorder stacks before start', () async {
    final native = _Mic();
    final phone = wrap(native, 'phone');
    final memo = wrap(_Mic(), 'dictation');
    expect(phone.reserveBeforePermission(hasAuthority: () => true), isTrue);
    expect(native.starts, 0);
    expect(memo.reserveBeforePermission(hasAuthority: () => true), isFalse);
    await expectLater(Future.sync(() => memo.start(onByteReceived: (_) {})), throwsStateError);
    await phone.stopAndConfirm();
    expect(native.stops, 0, reason: 'cancelled permission never attempted physical start');
    await memo.start(onByteReceived: (_) {});
    await memo.stopAndConfirm();
  });
  test('stop waits for held native start and retains ownership while stop acknowledgement is held', () async {
    final native = _Mic()
      ..startGate = Completer<void>()
      ..stopGate = Completer<void>();
    final phone = wrap(native, 'phone');
    final starting = phone.start(onByteReceived: (_) {});
    final stopping = phone.stopAndConfirm();
    expect(native.stops, 0);
    expect(wrap(_Mic(), 'call').reserveBeforePermission(hasAuthority: () => true), isFalse);
    native.startGate!.complete();
    await starting;
    await Future<void>.delayed(Duration.zero);
    expect(native.stops, 1);
    expect(arbiter.owner, 'phone');
    native.stopGate!.complete();
    await stopping;
    expect(arbiter.owner, isNull);
  });
  test('ambiguous native start never silently releases ownership', () async {
    final native = _Mic()
      ..failStart = true
      ..failStop = true;
    final phone = wrap(native, 'phone');
    await expectLater(phone.start(onByteReceived: (_) {}), throwsStateError);
    await expectLater(phone.stopAndConfirm(), throwsStateError);
    expect(arbiter.owner, 'phone');
    expect(wrap(_Mic(), 'call').reserveBeforePermission(hasAuthority: () => true), isFalse);
    native.failStop = false;
    await phone.stopAndConfirm();
    expect(arbiter.owner, isNull);
  });
  test('batch natural stop retains reservation until physical stop receipt', () async {
    final native = _Mic()..stopGate = Completer<void>();
    final phone = wrap(native, 'batch');
    var ended = false;
    await phone.startBatch(onStop: () => ended = true);
    native.callbacks.single!();
    await Future<void>.delayed(Duration.zero);
    expect(ended, isFalse);
    expect(arbiter.owner, 'batch');
    native.stopGate!.complete();
    await Future<void>.delayed(Duration.zero);
    expect(ended, isTrue);
    expect(arbiter.owner, isNull);
  });
  test('exact unused preflight cancellation never touches physical microphone', () async {
    final native = _Mic();
    final phone = wrap(native, 'phone');
    final reservation = phone.reserveCapturePreflight(hasAuthority: () => true)!;
    expect(reservation.isCurrent, isTrue);
    expect(reservation.hasAttemptedStart, isFalse);
    expect(wrap(_Mic(), 'call').reserveCapturePreflight(hasAuthority: () => true), isNull);
    expect(await reservation.cancelBeforeStart(), isTrue);
    expect(native.stops, 0);
    expect(native.bestEffortStops, 0);
    expect(registry.isReserved, isFalse);
  });

  test('preflight cancellation cannot release started or pending native capture', () async {
    final native = _Mic()..startGate = Completer<void>();
    final phone = wrap(native, 'phone');
    final reservation = phone.reserveCapturePreflight(hasAuthority: () => true)!;
    final start = phone.start(onByteReceived: (_) {});
    expect(reservation.hasAttemptedStart, isTrue);
    expect(await reservation.cancelBeforeStart(), isFalse);
    expect(native.stops, 0);
    expect(registry.isReserved, isTrue);
    native.startGate!.complete();
    await start;
    await phone.stopAndConfirm();
    expect(native.stops, 1);
    expect(registry.isReserved, isFalse);
  });

  test('old preflight cannot cancel replacement with same diagnostic label', () async {
    final native = _Mic();
    final phone = wrap(native, 'phone');
    final old = phone.reserveCapturePreflight(hasAuthority: () => true)!;
    expect(await old.cancelBeforeStart(), isTrue);
    final replacement = phone.reserveCapturePreflight(hasAuthority: () => true)!;
    expect(await old.cancelBeforeStart(), isFalse);
    expect(replacement.isCurrent, isTrue);
    expect(registry.isReserved, isTrue);
    expect(native.stops, 0);
    await replacement.cancelBeforeStart();
  });

  test('revoked unused preflight releases only itself without physical stop', () async {
    var authority = true;
    final native = _Mic();
    final phone = wrap(native, 'phone');
    final reservation = phone.reserveCapturePreflight(hasAuthority: () => authority)!;
    authority = false;
    expect(reservation.isCurrent, isFalse);
    expect(await reservation.cancelBeforeStart(), isTrue);
    expect(registry.isReserved, isFalse);
    expect(native.stops, 0);
  });
}
