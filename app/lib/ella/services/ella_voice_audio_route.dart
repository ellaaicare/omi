import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

enum EllaVoiceAudioUsage { playback, interactive }

/// Keeps Ella voice playback on an active wired, Bluetooth, or AirPlay route
/// and on the iPhone loudspeaker otherwise. `playAndRecord` sessions can fall
/// back to the receiver after microphone or route transitions, so
/// `defaultToSpeaker` alone is not a sufficient runtime guarantee.
class EllaVoiceAudioRoute {
  const EllaVoiceAudioRoute._();

  static const MethodChannel _channel = MethodChannel('com.ellaaicare.ella/audio_route');

  static Future<bool> ensureAudibleOutput({
    EllaVoiceAudioUsage usage = EllaVoiceAudioUsage.interactive,
    @visibleForTesting MethodChannel channel = _channel,
    @visibleForTesting bool? isIos,
  }) async {
    if (!(isIos ?? Platform.isIOS)) return true;
    try {
      final result = await channel.invokeMapMethod<String, dynamic>('ensureAudibleVoiceOutput', {'usage': usage.name});
      final classification = result?['routeClassification'];
      final failure = result?['failureCode'];
      final success = result?['success'] == true &&
          failure == null &&
          (classification == 'speaker' || classification == 'external');
      debugPrint(
        '[EllaVoiceAudioRoute] usage=${usage.name} route=${classification is String ? classification : 'unavailable'} '
        'success=$success failure=${failure is String ? failure : failure == null ? 'none' : 'malformed'}',
      );
      return success;
    } on PlatformException {
      return false;
    } on MissingPluginException {
      return false;
    }
  }

  static Future<bool> release({
    EllaVoiceAudioUsage usage = EllaVoiceAudioUsage.interactive,
    @visibleForTesting MethodChannel channel = _channel,
    @visibleForTesting bool? isIos,
  }) async {
    if (!(isIos ?? Platform.isIOS)) return true;
    try {
      return await channel.invokeMethod<bool>('releaseVoiceAudioSession', {'usage': usage.name}) == true;
    } on PlatformException {
      return false;
    } on MissingPluginException {
      return false;
    }
  }
}
