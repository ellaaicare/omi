import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/voice/darwin_local_dictation_port.dart';
import 'package:omi/ella/voice/foreground_dictation_host.dart';

Map<String, Object?> proof({bool active = false, bool required = true, int policy = 7}) => {
      'generation': 7,
      'engineRunning': active,
      'tapMayBeInstalled': active,
      'stopAmbiguous': false,
      'taskRetained': active,
      'recognitionDrainSupported': true,
      'recognitionTerminal': !active,
      'recognitionTaskCreated': true,
      'finalResultJSON': null,
      'onDeviceRecognitionSupported': true,
      'onDeviceRecognitionRequired': required,
      'onDevicePolicyGeneration': policy,
    };
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final messenger = TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
  tearDown(() => messenger.setMockMethodCallHandler(DarwinLocalDictationPort.channel, null));
  test('default disabled real engine constructor performs no native calls', () {
    final calls = <MethodCall>[];
    messenger.setMockMethodCallHandler(DarwinLocalDictationPort.channel, (call) async {
      calls.add(call);
      return null;
    });
    expect(DarwinForegroundDictationEngine.new, throwsStateError);
    expect(calls, isEmpty);
  });
  test('actual codec pure capability request has no initialize/listen/permission effects', () async {
    final calls = <MethodCall>[];
    messenger.setMockMethodCallHandler(DarwinLocalDictationPort.channel, (call) async {
      calls.add(call);
      return {
        'version': 1,
        'onDeviceRecognitionSupported': true,
        'speechAuthorization': 'not_determined',
        'microphoneAuthorization': 'undetermined',
        'localeId': 'en_US'
      };
    });
    final port = DarwinLocalDictationPort(stopSpeech: () async {});
    final capability = await port.capability();
    expect(capability.supported, true);
    expect(calls.map((c) => c.method), ['ellaLocalRecognitionCapabilities']);
    expect(calls.single.arguments, {'localeId': null});
  });
  test('closed typed capability rejects extra keys, booleans-as-version and unsupported authorization', () {
    final valid = <Object?, Object?>{
      'version': 1,
      'onDeviceRecognitionSupported': true,
      'speechAuthorization': 'authorized',
      'microphoneAuthorization': 'granted',
      'localeId': 'en_US'
    };
    for (final bad in [
      {...valid, 'version': true},
      {...valid, 'extra': true},
      {...valid, 'onDeviceRecognitionSupported': 1},
      {...valid, 'speechAuthorization': 'unknown'}
    ]) {
      expect(() => LocalDictationCapability.fromMap(bad), throwsFormatException);
    }
  });
  test('idle old generation permits fresh admission but active wrong local policy refuses', () async {
    var map = proof(required: false, policy: 0);
    messenger.setMockMethodCallHandler(DarwinLocalDictationPort.channel, (_) async => map);
    final port = DarwinLocalDictationPort(stopSpeech: () async {});
    expect((await port.probe()).generation, 7);
    map = proof(active: true, required: false);
    await expectLater(port.probe(), throwsStateError);
    map = proof(active: true, policy: 6);
    await expectLater(port.probe(), throwsStateError);
    map = proof(active: true);
    expect((await port.probe()).generation, 7);
  });
  test('exact physical terminal cannot pass without same-generation enforced local recognition', () async {
    var map = proof(required: false);
    var sdkStops = 0;
    messenger.setMockMethodCallHandler(DarwinLocalDictationPort.channel, (_) async => map);
    final port = DarwinLocalDictationPort(stopSpeech: () async {
      sdkStops++;
    });
    await expectLater(port.stopAndDrain(7), throwsStateError);
    expect(sdkStops, 0);
    map = proof(policy: 6);
    await expectLater(port.stopAndDrain(7), throwsStateError);
    expect(sdkStops, 0);
    map = proof();
    expect((await port.stopAndDrain(7)).provesComplete(7), true);
    expect(sdkStops, 1);
  });
}
