import 'dart:async';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:fake_async/fake_async.dart';
import 'package:omi/ella/services/standard_speech_ownership.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';
import 'package:speech_to_text/ella_speech_ownership.dart';

class _SpeechPort implements StandardSpeechReceiptPort {
  int generation = 0;
  bool running = false, tap = false, ambiguous = false;
  int stops = 0;
  bool failStop = false;
  Completer<void>? stopGate;
  void Function(Map<Object?, Object?>)? callback;

  @override
  Future<EllaSpeechCaptureProof> probe() async => EllaSpeechCaptureProof.fromMap({
        'generation': generation,
        'engineRunning': running,
        'tapMayBeInstalled': tap,
        'stopAmbiguous': ambiguous,
        'taskRetained': running,
        'recognitionDrainSupported': true,
        'recognitionTaskCreated': generation > 0,
        'recognitionTerminal': generation > 0 && !running,
      });
  @override
  Future<void> stop() async {
    stops++;
    await stopGate?.future;
    if (failStop) throw StateError('native stop ambiguous');
    running = tap = ambiguous = false;
  }

  @override
  Future<StandardSpeechDrainReceipt> stopAndDrain(int generation) async {
    await stop();
    return StandardSpeechDrainReceipt.fromMap({
      'generation': this.generation,
      'engineRunning': running,
      'tapMayBeInstalled': tap,
      'stopAmbiguous': ambiguous,
      'taskRetained': false,
      'recognitionTerminal': true,
      'finalResultJSON': null,
    });
  }

  @override
  void setCallback(void Function(Map<Object?, Object?>)? callback) => this.callback = callback;
  Future<void> listen() async {
    generation++;
    running = tap = true;
  }

  void emit(int generation) =>
      callback?.call({'generation': generation, 'method': 'notifyStatus', 'payload': 'fixture'});
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late AudioCaptureOwnership registry;
  late _SpeechPort port;
  late StandardSpeechOwnership owner;
  late List<int> deliveries;
  setUp(() {
    registry = AudioCaptureOwnership();
    port = _SpeechPort();
    owner = StandardSpeechOwnership(port: port, ownership: registry);
    deliveries = [];
    expect(owner.reserve(hasAuthority: () => true, onCallback: (_, __) => deliveries.add(port.generation)), isTrue);
  });

  test('permission/init held excludes sources, close waits and never starts cancelled native listen', () async {
    final permission = Completer<bool>();
    final start = owner.start(prepare: () => permission.future, listen: port.listen);
    await Future<void>.delayed(Duration.zero);
    expect(registry.reserve(source: 'phone', hasAuthority: () => true), isNull);
    var ended = false;
    final end = owner.end().then((_) => ended = true);
    await Future<void>.delayed(Duration.zero);
    expect(ended, isFalse);
    permission.complete(true);
    expect(await start, isFalse);
    await end;
    expect(port.generation, 0);
    expect(port.stops, 0);
    expect(registry.isReserved, isFalse);
  });

  test('held native start and physical stop retain exact owner and retire callbacks', () async {
    final native = Completer<void>();
    final start = owner.start(
        prepare: () async => true,
        listen: () async {
          await port.listen();
          await native.future;
        });
    await Future<void>.delayed(Duration.zero);
    port.emit(1);
    expect(deliveries, [1]);
    port.stopGate = Completer<void>();
    final end = owner.end();
    port.emit(1);
    expect(deliveries, [1]);
    native.complete();
    await Future<void>.delayed(Duration.zero);
    expect(registry.isReserved, isTrue);
    expect(port.stops, 2);
    port.stopGate!.complete();
    await end;
    expect(await start, isFalse);
    expect(registry.isReserved, isFalse);
  });

  test('failed stop holds parent and blocks replacement until physical retry', () async {
    expect(await owner.start(prepare: () async => true, listen: port.listen), isTrue);
    port.failStop = true;
    await expectLater(owner.stopListening(), throwsStateError);
    expect(registry.reserve(source: 'device', hasAuthority: () => true), isNull);
    port.emit(1);
    expect(deliveries, isEmpty);
    port.failStop = false;
    await owner.end();
    expect(registry.isReserved, isFalse);
  });

  test('logical call keeps reservation across playback and rejects old native generation after restart', () async {
    expect(await owner.start(prepare: () async => true, listen: port.listen), isTrue);
    await owner.stopListening();
    expect(registry.reserve(source: 'dictation', hasAuthority: () => true), isNull);
    expect(await owner.start(prepare: () async => true, listen: port.listen), isTrue);
    port.emit(1);
    port.emit(2);
    expect(deliveries, [2]);
    await owner.end();
  });

  test('account invalidation suppresses results and holds physical stop receipt', () async {
    expect(await owner.start(prepare: () async => true, listen: port.listen), isTrue);
    port.stopGate = Completer<void>();
    final change = registry.quiesceForAccountTransition();
    port.emit(1);
    expect(deliveries, isEmpty);
    expect(registry.isReserved, isTrue);
    await Future<void>.delayed(Duration.zero);
    port.stopGate!.complete();
    await change;
    expect(registry.isReserved, isFalse);
    await owner.end();
  });

  test('account change during held initialization cannot open speech for the replacement account', () async {
    await owner.end();
    var exactAccount = 'account-a';
    owner = StandardSpeechOwnership(port: port, ownership: registry);
    expect(owner.reserve(hasAuthority: () => exactAccount == 'account-a', onCallback: (_, __) {}), isTrue);
    final initialization = Completer<bool>();
    final start = owner.start(prepare: () => initialization.future, listen: port.listen);
    await Future<void>.delayed(Duration.zero);
    exactAccount = 'account-b';
    final change = registry.quiesceForAccountTransition();
    expect(registry.reserve(source: 'replacement-account', hasAuthority: () => true), isNull);
    initialization.complete(true);
    expect(await start, isFalse);
    await change;
    expect(port.generation, 0);
    expect(port.stops, 0);
    expect(registry.isReserved, isFalse);
  });

  test('unexpected generation cannot authorize native stop or release owner', () async {
    expect(await owner.start(prepare: () async => true, listen: port.listen), isTrue);
    port.generation = 99;
    await expectLater(owner.end(), throwsStateError);
    expect(port.stops, 0);
    expect(registry.isReserved, isTrue);
    port.generation = 1;
    await owner.end();
  });

  test('already-active unowned engine is not stopped or permission requested', () async {
    port.running = port.tap = true;
    var prepares = 0;
    await expectLater(
        owner.start(
            prepare: () async {
              prepares++;
              return true;
            },
            listen: port.listen),
        throwsStateError);
    expect(prepares, 0);
    expect(port.stops, 0);
    expect(registry.isReserved, isTrue);
  });

  test('Darwin channel rejects missing physical proof and invokes additive exact probe', () async {
    const channel = MethodChannel('plugin.csdcorp.com/speech_to_text');
    final methods = <String>[];
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
      methods.add(call.method);
      return {
        'generation': 4,
        'engineRunning': false,
        'tapMayBeInstalled': false,
        'stopAmbiguous': false,
        'taskRetained': false
      };
    });
    final adapter = DarwinStandardSpeechReceiptPort();
    expect((await adapter.probe()).provesCaptureStopped(4), isTrue);
    await adapter.stop();
    expect(methods, ['ellaOwnershipProbe', 'stop']);
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (_) async => null);
    await expectLater(adapter.probe(), throwsStateError);
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, null);
  });

  test('retired silence timer cannot stop replacement; timeout waits exact physical proof before restart', () {
    fakeAsync((clock) {
      var endings = 0;
      owner.start(
          prepare: () async => true,
          listen: port.listen,
          pauseFor: const Duration(seconds: 3),
          onListeningEnded: () => endings++);
      clock.flushMicrotasks();
      clock.elapse(const Duration(seconds: 2));
      owner.stopListening();
      clock.flushMicrotasks();
      owner.start(
          prepare: () async => true,
          listen: port.listen,
          pauseFor: const Duration(seconds: 3),
          onListeningEnded: () => endings++);
      clock.flushMicrotasks();
      clock.elapse(const Duration(seconds: 1));
      expect(port.stops, 1, reason: 'old generation silence timer is retired');
      port.stopGate = Completer<void>();
      clock.elapse(const Duration(seconds: 2));
      clock.flushMicrotasks();
      expect(port.stops, 2);
      expect(endings, 0);
      expect(registry.isReserved, isTrue);
      port.stopGate!.complete();
      clock.flushMicrotasks();
      expect(endings, 1);
      expect(registry.isReserved, isTrue, reason: 'pause ends child, not logical call');
      owner.end();
      clock.flushMicrotasks();
      expect(registry.isReserved, isFalse);
    });
  });

  test('failed owned silence stop never reports restart eligibility', () {
    fakeAsync((clock) {
      var endings = 0;
      owner.start(
          prepare: () async => true,
          listen: port.listen,
          listenFor: const Duration(seconds: 60),
          onListeningEnded: () => endings++);
      clock.flushMicrotasks();
      port.failStop = true;
      clock.elapse(const Duration(seconds: 60));
      clock.flushMicrotasks();
      expect(endings, 0);
      expect(registry.isReserved, isTrue);
      port.failStop = false;
      owner.end();
      clock.flushMicrotasks();
      expect(registry.isReserved, isFalse);
    });
  });
}
