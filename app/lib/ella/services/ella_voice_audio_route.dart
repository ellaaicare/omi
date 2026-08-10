import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

/// Keeps Ella voice playback on a private route when one is connected and on
/// the iPhone loudspeaker otherwise. `playAndRecord` sessions can fall back to
/// the receiver after microphone or route transitions, so `defaultToSpeaker`
/// alone is not a sufficient runtime guarantee.
class EllaVoiceAudioRoute {
  const EllaVoiceAudioRoute._();

  static const MethodChannel _channel = MethodChannel(
    'com.ellaaicare.ella/audio_route',
  );

  static Future<bool> ensureAudibleOutput({
    @visibleForTesting MethodChannel channel = _channel,
    @visibleForTesting bool? isIos,
  }) async {
    if (!(isIos ?? Platform.isIOS)) return true;
    try {
      final result = await channel.invokeMapMethod<String, dynamic>(
        'ensureAudibleVoiceOutput',
      );
      return result?['success'] == true && result?['usesReceiver'] != true;
    } on PlatformException {
      return false;
    } on MissingPluginException {
      return false;
    }
  }
}
