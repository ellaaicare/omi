import 'package:flutter/services.dart';
import 'package:speech_to_text/ella_speech_ownership.dart';
import 'package:omi/ella/services/standard_speech_ownership.dart';

class LocalDictationCapability {
  LocalDictationCapability.fromMap(Map<Object?, Object?> map) {
    const keys = {
      'version',
      'onDeviceRecognitionSupported',
      'speechAuthorization',
      'microphoneAuthorization',
      'localeId'
    };
    if (map.length != keys.length ||
        !keys.every(map.containsKey) ||
        map['version'] is! int ||
        map['version'] != 1 ||
        map['onDeviceRecognitionSupported'] is! bool ||
        !{'authorized', 'not_determined', 'denied', 'restricted'}.contains(map['speechAuthorization']) ||
        !{'granted', 'undetermined', 'denied'}.contains(map['microphoneAuthorization']) ||
        map['localeId'] is! String ||
        (map['localeId'] as String).isEmpty) throw const FormatException('local capability');
    supported = map['onDeviceRecognitionSupported'] as bool;
    speechAuthorization = map['speechAuthorization'] as String;
    microphoneAuthorization = map['microphoneAuthorization'] as String;
    localeId = map['localeId'] as String;
  }
  late final bool supported;
  late final String speechAuthorization, microphoneAuthorization, localeId;
}

/// Only ephemeral dictation uses this decorator. Ordinary voice ports retain
/// their existing contract. Missing/malformed local policy cannot release input.
class DarwinLocalDictationPort implements StandardSpeechReceiptPort {
  DarwinLocalDictationPort({required Future<void> Function() stopSpeech})
      : _stopSpeech = stopSpeech,
        _callbacks = DarwinStandardSpeechReceiptPort(stopSpeech: stopSpeech);
  static const channel = MethodChannel('plugin.csdcorp.com/speech_to_text');
  final Future<void> Function() _stopSpeech;
  final DarwinStandardSpeechReceiptPort _callbacks;
  Future<LocalDictationCapability> capability() async {
    final map = await channel.invokeMapMethod<Object?, Object?>('ellaLocalRecognitionCapabilities', {'localeId': null});
    if (map == null) throw const FormatException('missing local capability');
    return LocalDictationCapability.fromMap(map);
  }

  void _validatePolicy(Map<Object?, Object?> map, {int? requiredGeneration}) {
    final supported = map['onDeviceRecognitionSupported'];
    final required = map['onDeviceRecognitionRequired'];
    final generation = map['onDevicePolicyGeneration'];
    if (supported is! bool || required is! bool || generation is! int || generation < 0) {
      throw const FormatException('local recognition policy');
    }
    final capture = EllaSpeechCaptureProof.fromMap(map);
    final active = capture.engineRunning || capture.tapMayBeInstalled || capture.taskRetained;
    if (active || requiredGeneration != null) {
      if (!supported || !required || generation != (requiredGeneration ?? capture.generation) || generation <= 0) {
        throw StateError('local recognition policy unconfirmed');
      }
    }
  }

  @override
  Future<EllaSpeechCaptureProof> probe() async {
    final map = await channel.invokeMapMethod<Object?, Object?>('ellaOwnershipProbe');
    if (map == null) throw const FormatException('missing speech ownership');
    _validatePolicy(map);
    return EllaSpeechCaptureProof.fromMap(map);
  }

  @override
  Future<StandardSpeechDrainReceipt> stopAndDrain(int generation) async {
    final map = await channel.invokeMapMethod<Object?, Object?>('ellaStopAndDrain', {'generation': generation});
    if (map == null) throw const FormatException('missing speech drain');
    _validatePolicy(map, requiredGeneration: generation);
    final receipt = StandardSpeechDrainReceipt.fromMap(map);
    if (!receipt.provesComplete(generation)) throw StateError('speech terminal unconfirmed');
    await _stopSpeech();
    return receipt;
  }

  @override
  Future<void> stop() => _stopSpeech();
  @override
  void setCallback(void Function(Map<Object?, Object?>)? callback) => _callbacks.setCallback(callback);
}
