import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:speech_to_text/ella_speech_ownership.dart';
import 'package:omi/ella/services/standard_speech_ownership.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';

Future<void> settleSpeech() async {
  for (var i = 0; i < 20; i++) {
    await Future<void>.delayed(Duration.zero);
  }
}

class HeldSpeechPort implements StandardSpeechReceiptPort {
  int generation = 0, drains = 0, probes = 0;
  bool running = false, task = false, terminal = false, resurrect = false;
  int? foreignProbe;
  Completer<void>? listenGate, terminalGate;
  String words = 'final words';
  void Function(Map<Object?, Object?>)? callback;
  @override
  Future<EllaSpeechCaptureProof> probe() async {
    probes++;
    return EllaSpeechCaptureProof.fromMap({
      'generation': foreignProbe ?? generation,
      'engineRunning': running,
      'tapMayBeInstalled': running,
      'stopAmbiguous': false,
      'taskRetained': task,
      'recognitionDrainSupported': true,
      'recognitionTaskCreated': generation > 0,
      'recognitionTerminal': terminal
    });
  }

  Future<void> listen() async {
    generation++;
    running = task = true;
    terminal = false;
    if (listenGate != null) await listenGate!.future;
    if (resurrect) {
      running = task = true;
      terminal = false;
    }
  }

  @override
  Future<void> stop() async {
    throw StateError('untagged stop forbidden');
  }

  @override
  Future<StandardSpeechDrainReceipt> stopAndDrain(int target) async {
    expectSync(target, generation);
    drains++;
    running = false;
    if (terminalGate != null) await terminalGate!.future;
    task = false;
    terminal = true;
    return StandardSpeechDrainReceipt.fromMap({
      'generation': generation,
      'engineRunning': false,
      'tapMayBeInstalled': false,
      'stopAmbiguous': false,
      'taskRetained': false,
      'recognitionDrainSupported': true,
      'recognitionTaskCreated': true,
      'recognitionTerminal': true,
      'finalResultJSON': jsonEncode({
        'alternates': [
          {'recognizedWords': words, 'confidence': 1.0}
        ],
        'finalResult': true
      })
    });
  }

  @override
  void setCallback(void Function(Map<Object?, Object?>)? value) {
    callback = value;
  }
}

class HeldLegacyStopPort extends HeldSpeechPort {
  final sdkStopGate = Completer<void>();
  @override
  Future<StandardSpeechDrainReceipt> stopAndDrain(int generation) async {
    final index = drains + 1;
    final receipt = await super.stopAndDrain(generation);
    if (index == 1) await sdkStopGate.future;
    return receipt;
  }
}

class UnsupportedActivePort extends HeldSpeechPort {
  @override
  Future<EllaSpeechCaptureProof> probe() async => EllaSpeechCaptureProof.fromMap({
        'generation': 8,
        'engineRunning': true,
        'tapMayBeInstalled': true,
        'stopAmbiguous': false,
        'taskRetained': true,
        'recognitionDrainSupported': false,
      });
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('held live speech listen receives exact early cleanup while parent stays held', () async {
    final port = HeldSpeechPort()..listenGate = Completer<void>();
    final registry = AudioCaptureOwnership();
    final owner = StandardSpeechOwnership(port: port, ownership: registry);
    owner.reserve(hasAuthority: () => true, onCallback: (_, __) {});
    final start = owner.start(prepare: () async => true, listen: port.listen).catchError((_) => false);
    await settleSpeech();
    expect(port.running, true);
    final ending = owner.end();
    await settleSpeech();
    final earlyDrains = port.drains;
    final earlyRunning = port.running;
    expect(registry.isReserved, true);
    port.listenGate!.complete();
    await start;
    await ending;
    expect(earlyDrains, 1);
    expect(earlyRunning, false);
    expect(port.drains, 2);
    expect(registry.isReserved, false);
  });
  test('late listen resurrection is stopped again before exact parent release', () async {
    final port = HeldSpeechPort()
      ..listenGate = Completer<void>()
      ..resurrect = true;
    final registry = AudioCaptureOwnership();
    final owner = StandardSpeechOwnership(port: port, ownership: registry);
    owner.reserve(hasAuthority: () => true, onCallback: (_, __) {});
    final start = owner.start(prepare: () async => true, listen: port.listen).catchError((_) => false);
    await settleSpeech();
    final ending = owner.end();
    await settleSpeech();
    expect(port.drains, 1);
    expect(registry.isReserved, true);
    port.listenGate!.complete();
    await start;
    await ending;
    expect(port.drains, 2);
    expect(port.running, false);
    expect(registry.isReserved, false);
  });
  test('unknown actual generation receives no guessed early stop and keeps parent held', () async {
    final port = HeldSpeechPort()..listenGate = Completer<void>();
    final registry = AudioCaptureOwnership();
    final owner = StandardSpeechOwnership(port: port, ownership: registry);
    owner.reserve(hasAuthority: () => true, onCallback: (_, __) {});
    final start = owner.start(prepare: () async => true, listen: port.listen).catchError((_) => false);
    await settleSpeech();
    port.foreignProbe = 4;
    final ending = owner.end();
    final failure = expectLater(ending, throwsStateError);
    await settleSpeech();
    expect(port.drains, 0);
    expect(registry.isReserved, true);
    port.listenGate!.complete();
    await start;
    await failure;
    expect(port.drains, 0);
    expect(registry.isReserved, true);
  });
  test('late early SDK stop is part of parent lifetime before replacement admission', () async {
    final port = HeldLegacyStopPort()..listenGate = Completer<void>();
    final registry = AudioCaptureOwnership();
    final owner = StandardSpeechOwnership(port: port, ownership: registry);
    owner.reserve(hasAuthority: () => true, onCallback: (_, __) {});
    final start = owner.start(prepare: () async => true, listen: port.listen).catchError((_) => false);
    await settleSpeech();
    final ending = owner.end();
    await settleSpeech();
    expect(port.drains, 1);
    port.listenGate!.complete();
    await settleSpeech();
    expect(port.drains, 2);
    expect(registry.reserve(source: 'replacement', hasAuthority: () => true), isNull);
    port.sdkStopGate.complete();
    await start;
    await ending;
    expect(registry.isReserved, false);
  });
  test('unsupported but active native source is held without permission or unowned stop', () async {
    final port = UnsupportedActivePort();
    final registry = AudioCaptureOwnership();
    final owner = StandardSpeechOwnership(port: port, ownership: registry);
    owner.reserve(hasAuthority: () => true, onCallback: (_, __) {});
    var permission = 0;
    await expectLater(
        owner.start(
            prepare: () async {
              permission++;
              return true;
            },
            listen: port.listen),
        throwsStateError);
    await expectLater(owner.end(), throwsStateError);
    expect(permission, 0);
    expect(port.drains, 0);
    expect(registry.isReserved, true);
  });
}
