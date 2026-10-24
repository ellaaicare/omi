import 'dart:async';
import 'dart:convert';
import 'package:fake_async/fake_async.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter/services.dart';
import 'package:speech_to_text/ella_speech_ownership.dart';
import 'package:omi/ella/pages/ella_voice_chat_page.dart';
import 'package:omi/ella/services/standard_speech_ownership.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';
import 'fixtures/frozen_standard44_speech_ownership.dart' as frozen;

String _result(String text) => jsonEncode({
      'alternates': [
        {'recognizedWords': text, 'confidence': 1.0}
      ],
      'finalResult': true
    });

class _DrainPort implements StandardSpeechReceiptPort, frozen.StandardSpeechReceiptPort {
  int generation = 0, oldStops = 0, drains = 0;
  bool running = false, taskRetained = false, terminal = false;
  bool reportTerminal = true;
  String finalJSON = _result('final fixture');
  final terminalGate = Completer<void>();
  void Function(Map<Object?, Object?>)? callback;
  @override
  Future<EllaSpeechCaptureProof> probe() async => EllaSpeechCaptureProof.fromMap({
        'generation': generation,
        'engineRunning': running,
        'tapMayBeInstalled': running,
        'stopAmbiguous': false,
        'taskRetained': taskRetained,
        'recognitionDrainSupported': true,
        'recognitionTaskCreated': generation > 0,
        'recognitionTerminal': terminal,
      });
  Future<void> listen() async {
    generation++;
    running = taskRetained = true;
  }

  void finalCallback() => callback?.call({'generation': generation, 'method': 'textRecognition', 'payload': finalJSON});
  @override
  Future<void> stop() async {
    oldStops++;
    finalCallback();
    running = taskRetained = false;
  }

  @override
  Future<StandardSpeechDrainReceipt> stopAndDrain(int generation) async {
    drains++;
    finalCallback(); // Already-produced native final callback during held stop.
    running = false;
    await terminalGate.future;
    terminal = true;
    taskRetained = false;
    return StandardSpeechDrainReceipt.fromMap({
      'generation': this.generation,
      'engineRunning': false,
      'tapMayBeInstalled': false,
      'stopAmbiguous': false,
      'taskRetained': false,
      'recognitionTerminal': reportTerminal && terminal,
      'finalResultJSON': finalJSON,
    });
  }

  @override
  void setCallback(void Function(Map<Object?, Object?>)? callback) => this.callback = callback;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  for (final limit in [const Duration(seconds: 3), const Duration(seconds: 60)]) {
    test('frozen44 actual timer/page completion loses final-only stop tail at ${limit.inSeconds}s', () {
      fakeAsync((clock) {
        final port = _DrainPort();
        final owner = frozen.StandardSpeechOwnership(port: port, ownership: AudioCaptureOwnership());
        var pageWords = '', restarts = 0;
        final processed = <String>[];
        owner.reserve(
            hasAuthority: () => true,
            onCallback: (_, payload) => pageWords = retainedFinalStandardSpeechWords(payload as String)!);
        owner.start(
            prepare: () async => true,
            listen: port.listen,
            pauseFor: limit.inSeconds == 3 ? limit : null,
            listenFor: limit.inSeconds == 60 ? limit : null,
            onListeningEnded: () => finishStandardSpeechTimerTurn(
                retainedWords: pageWords, processTranscript: processed.add, restartListening: () => restarts++));
        clock.flushMicrotasks();
        clock.elapse(limit);
        clock.flushMicrotasks();
        expect(port.oldStops, 1);
        expect(processed, isEmpty, reason: 'frozen44 retires exact gate before stop final callback');
        expect(restarts, 1, reason: 'actual page completion restarts with no retained final-only words');
        owner.end();
        clock.flushMicrotasks();
      });
    });

    test('exact terminal receipt preserves timer spoken turn at ${limit.inSeconds}s without early publication', () {
      fakeAsync((clock) {
        final port = _DrainPort();
        final registry = AudioCaptureOwnership();
        final owner = StandardSpeechOwnership(port: port, ownership: registry);
        var pageWords = '', restarts = 0;
        final processed = <String>[];
        owner.reserve(
            hasAuthority: () => true,
            onCallback: (_, payload) => pageWords = retainedFinalStandardSpeechWords(payload as String)!);
        owner.start(
            prepare: () async => true,
            listen: port.listen,
            pauseFor: limit.inSeconds == 3 ? limit : null,
            listenFor: limit.inSeconds == 60 ? limit : null,
            onFinalResultRetained: (json) => pageWords = retainedFinalStandardSpeechWords(json)!,
            onListeningEnded: () => finishStandardSpeechTimerTurn(
                retainedWords: pageWords, processTranscript: processed.add, restartListening: () => restarts++));
        clock.flushMicrotasks();
        clock.elapse(limit);
        clock.flushMicrotasks();
        expect(port.drains, 1);
        expect(port.running, isFalse);
        expect(port.taskRetained, isTrue);
        expect(processed, isEmpty);
        expect(pageWords, isEmpty);
        expect(restarts, 0);
        expect(registry.reserve(source: 'replacement', hasAuthority: () => true), isNull);
        port.terminalGate.complete();
        clock.flushMicrotasks();
        expect(processed, ['final fixture']);
        expect(restarts, 0);
        expect(registry.isReserved, isTrue, reason: 'timer ends recognition child, logical call persists');
        owner.end();
        clock.flushMicrotasks();
        expect(registry.isReserved, isFalse);
      });
    });
  }

  for (final retirement in ['modal', 'account']) {
    test('$retirement retirement during held final task terminal publishes neither retained text nor turn', () {
      fakeAsync((clock) {
        final port = _DrainPort();
        final registry = AudioCaptureOwnership();
        final owner = StandardSpeechOwnership(port: port, ownership: registry);
        var textDeliveries = 0, turnDeliveries = 0;
        owner.reserve(hasAuthority: () => true, onCallback: (_, __) {});
        owner.start(
            prepare: () async => true,
            listen: port.listen,
            pauseFor: const Duration(seconds: 3),
            onFinalResultRetained: (_) => textDeliveries++,
            onListeningEnded: () => turnDeliveries++);
        clock.flushMicrotasks();
        clock.elapse(const Duration(seconds: 3));
        clock.flushMicrotasks();
        if (retirement == 'modal') {
          owner.end();
        } else {
          registry.quiesceForAccountTransition();
        }
        clock.flushMicrotasks();
        expect(registry.isReserved, isTrue);
        port.terminalGate.complete();
        clock.flushMicrotasks();
        expect(textDeliveries, 0);
        expect(turnDeliveries, 0);
        expect(registry.isReserved, isFalse);
      });
    });
  }

  test('captureStopped with task retained or absent terminal receipt cannot complete drain', () {
    for (final map in [
      {'taskRetained': true, 'recognitionTerminal': true},
      {'taskRetained': false, 'recognitionTerminal': false},
      {'taskRetained': false},
    ]) {
      final receipt = StandardSpeechDrainReceipt.fromMap(
          {'generation': 1, 'engineRunning': false, 'tapMayBeInstalled': false, 'stopAmbiguous': false, ...map});
      expect(receipt.provesComplete(1), isFalse);
    }
  });

  test('held final upgrades already retained partial before actual page timer publishes its turn', () {
    fakeAsync((clock) {
      final port = _DrainPort()..finalJSON = _result('complete fixture');
      final owner = StandardSpeechOwnership(port: port, ownership: AudioCaptureOwnership());
      var pageWords = 'partial fixture';
      final turns = <String>[];
      owner.reserve(hasAuthority: () => true, onCallback: (_, __) {});
      owner.start(
          prepare: () async => true,
          listen: port.listen,
          pauseFor: const Duration(seconds: 3),
          onFinalResultRetained: (json) => pageWords = retainedFinalStandardSpeechWords(json)!,
          onListeningEnded: () => finishStandardSpeechTimerTurn(
              retainedWords: pageWords, processTranscript: turns.add, restartListening: () {}));
      clock.flushMicrotasks();
      clock.elapse(const Duration(seconds: 3));
      clock.flushMicrotasks();
      expect(turns, isEmpty);
      expect(pageWords, 'partial fixture');
      port.terminalGate.complete();
      clock.flushMicrotasks();
      expect(turns, ['complete fixture']);
      owner.end();
      clock.flushMicrotasks();
    });
  });

  test('physical-only receipt retains child and local final until exact terminal retry', () async {
    final port = _DrainPort()..reportTerminal = false;
    final registry = AudioCaptureOwnership();
    final owner = StandardSpeechOwnership(port: port, ownership: registry);
    var retained = 0;
    owner.reserve(hasAuthority: () => true, onCallback: (_, __) {});
    await owner.start(prepare: () async => true, listen: port.listen, onFinalResultRetained: (_) => retained++);
    port.terminalGate.complete();
    await expectLater(owner.stopListening(), throwsStateError);
    expect(retained, 0);
    expect(registry.reserve(source: 'replacement', hasAuthority: () => true), isNull);
    port.reportTerminal = true;
    await owner.stopListening();
    expect(retained, 1);
    await owner.end();
    expect(registry.isReserved, isFalse);
  });

  test('actual Darwin channel adapter retains stop final locally only after physical and task terminal receipt',
      () async {
    const native = MethodChannel('plugin.csdcorp.com/speech_to_text');
    const callbacks = 'plugin.csdcorp.com/speech_to_text/ella_ownership';
    final messenger = TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    final terminal = Completer<void>();
    var generation = 0, running = false, task = false, retained = '', rawDeliveries = 0, sdkCleanup = 0;
    Map<String, Object?> proof() => {
          'generation': generation,
          'engineRunning': running,
          'tapMayBeInstalled': running,
          'stopAmbiguous': false,
          'taskRetained': task,
          'recognitionDrainSupported': true,
          'recognitionTaskCreated': generation > 0,
          'recognitionTerminal': terminal.isCompleted,
        };
    messenger.setMockMethodCallHandler(native, (call) async {
      if (call.method == 'ellaOwnershipProbe') return proof();
      if (call.method == 'ellaStopAndDrain') {
        expect((call.arguments as Map)['generation'], generation);
        running = false;
        final callback = const StandardMethodCodec().encodeMethodCall(MethodCall('callback', {
          'generation': generation,
          'method': 'textRecognition',
          'payload': _result('channel final fixture'),
        }));
        await messenger.handlePlatformMessage(callbacks, callback, (_) {});
        await terminal.future;
        task = false;
        return {...proof(), 'recognitionTerminal': true, 'finalResultJSON': _result('channel final fixture')};
      }
      throw StateError('unexpected native method');
    });
    final registry = AudioCaptureOwnership();
    final owner = StandardSpeechOwnership(
        ownership: registry,
        port: DarwinStandardSpeechReceiptPort(stopSpeech: () async {
          expect(terminal.isCompleted, isTrue);
          expect(task, isFalse);
          sdkCleanup++;
        }));
    owner.reserve(hasAuthority: () => true, onCallback: (_, __) => rawDeliveries++);
    expect(
        await owner.start(
            prepare: () async => true,
            listen: () async {
              generation++;
              running = task = true;
            },
            onFinalResultRetained: (json) => retained = retainedFinalStandardSpeechWords(json)!),
        isTrue);
    final stopping = owner.stopListening();
    await Future<void>.delayed(Duration.zero);
    expect(running, isFalse);
    expect(task, isTrue);
    expect(rawDeliveries, 0);
    expect(retained, isEmpty);
    expect(sdkCleanup, 0);
    expect(registry.isReserved, isTrue);
    terminal.complete();
    await stopping;
    expect(retained, 'channel final fixture');
    expect(sdkCleanup, 1);
    await owner.end();
    messenger.setMockMethodCallHandler(native, null);
  });

  test('prior task cleared without terminal acknowledgement blocks new permission and unowned stop', () async {
    final port = _DrainPort()..generation = 1;
    final registry = AudioCaptureOwnership();
    final owner = StandardSpeechOwnership(port: port, ownership: registry);
    var prepares = 0;
    owner.reserve(hasAuthority: () => true, onCallback: (_, __) {});
    await expectLater(
        owner.start(
            prepare: () async {
              prepares++;
              return true;
            },
            listen: port.listen),
        throwsStateError);
    expect(prepares, 0);
    expect(port.oldStops, 0);
    expect(port.drains, 0);
    expect(registry.isReserved, isTrue, reason: 'pointer absence is not a terminal acknowledgement');
  });

  test('malformed supported terminal proof is rejected rather than treating missing fields as no task', () {
    expect(
        () => EllaSpeechCaptureProof.fromMap({
              'generation': 1,
              'engineRunning': false,
              'tapMayBeInstalled': false,
              'stopAmbiguous': false,
              'taskRetained': false,
              'recognitionDrainSupported': true
            }),
        throwsFormatException);
  });
}
