import 'package:flutter_tts/flutter_tts.dart';
import 'package:just_audio/just_audio.dart';
import 'package:omi/utils/logger.dart';

class EmergencyAudioPlayer {
  static final AudioPlayer _player = AudioPlayer();
  static final FlutterTts _tts = FlutterTts();

  static const String _fallbackText = 'Help is on the way. Your emergency contacts have been notified.';

  /// Play audio confirmation. Tries URL from API response first,
  /// falls back to on-device TTS if URL fails or is null.
  static Future<void> playConfirmation({String? audioUrl}) async {
    await _tts.setLanguage('en-US');
    await _tts.setSpeechRate(0.45);
    await _tts.setVolume(1.0);
    await _tts.setPitch(1.0);

    if (audioUrl != null) {
      try {
        await _player.setUrl(audioUrl);
        await _player.setVolume(1.0);
        await _player.play();
        return;
      } catch (e) {
        Logger.debug('Emergency audio URL failed, falling back to TTS: $e');
      }
    }

    await _tts.speak(_fallbackText);
  }

  static Future<void> stop() async {
    try {
      await _player.stop();
    } catch (_) {}
    try {
      await _tts.stop();
    } catch (_) {}
  }
}
