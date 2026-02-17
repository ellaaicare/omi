import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_tts/flutter_tts.dart';
import 'package:path_provider/path_provider.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/logger.dart';

/// TTS client that calls the backend TTS proxy (Kokoro local or ElevenLabs).
/// Falls back to on-device iOS TTS if the backend is unavailable.
class ElevenLabsTts {
  ElevenLabsTts._();

  static FlutterTts? _flutterTts;
  static String? _cachedTempDir;

  /// Synthesize [text] to speech via the backend TTS proxy.
  /// Returns the path to a temporary audio file, or null on failure.
  static Future<String?> synthesize(String text) async {
    if (text.trim().isEmpty) return null;

    final url = '${Env.apiBaseUrl}v1/voice/tts';

    try {
      final response = await makeApiCall(
        url: url,
        headers: {'Content-Type': 'application/json'},
        body: '{"text": ${_jsonEscapeString(text)}}',
        method: 'POST',
        timeout: const Duration(seconds: 30),
      );

      if (response == null || response.statusCode != 200) {
        Logger.debug('[TTS] Backend failed: ${response?.statusCode}');
        return null;
      }

      _cachedTempDir ??= (await getTemporaryDirectory()).path;
      final ts = DateTime.now().millisecondsSinceEpoch;
      final contentType = response.headers['content-type'] ?? '';

      if (contentType.contains('audio/L16') || contentType.contains('audio/pcm')) {
        // Raw PCM16 from local Kokoro — wrap in WAV header
        final sampleRate = _parseSampleRate(contentType);
        final wavBytes = _pcmToWav(response.bodyBytes, sampleRate: sampleRate);
        final path = '$_cachedTempDir/ella_tts_$ts.wav';
        await File(path).writeAsBytes(wavBytes, flush: true);
        Logger.debug('[TTS] PCM→WAV ${response.bodyBytes.length}b, ${sampleRate}Hz → $path');
        return path;
      } else {
        // MP3 from ElevenLabs or other
        final path = '$_cachedTempDir/ella_tts_$ts.mp3';
        await File(path).writeAsBytes(response.bodyBytes, flush: true);
        return path;
      }
    } catch (e) {
      Logger.debug('[TTS] Error: $e');
      return null;
    }
  }

  /// Parse sample rate from content-type like "audio/L16;rate=24000"
  static int _parseSampleRate(String contentType) {
    final match = RegExp(r'rate=(\d+)').firstMatch(contentType);
    if (match != null) return int.parse(match.group(1)!);
    return 24000;
  }

  /// Wrap raw PCM16 mono data in a WAV container.
  static Uint8List _pcmToWav(Uint8List pcmData, {int sampleRate = 24000, int channels = 1, int bitsPerSample = 16}) {
    final dataSize = pcmData.length;
    final fileSize = 36 + dataSize;
    final byteRate = sampleRate * channels * (bitsPerSample ~/ 8);
    final blockAlign = channels * (bitsPerSample ~/ 8);

    final header = ByteData(44);
    header.setUint8(0, 0x52); // R
    header.setUint8(1, 0x49); // I
    header.setUint8(2, 0x46); // F
    header.setUint8(3, 0x46); // F
    header.setUint32(4, fileSize, Endian.little);
    header.setUint8(8, 0x57); // W
    header.setUint8(9, 0x41); // A
    header.setUint8(10, 0x56); // V
    header.setUint8(11, 0x45); // E
    header.setUint8(12, 0x66); // f
    header.setUint8(13, 0x6D); // m
    header.setUint8(14, 0x74); // t
    header.setUint8(15, 0x20); // (space)
    header.setUint32(16, 16, Endian.little);
    header.setUint16(20, 1, Endian.little); // PCM format
    header.setUint16(22, channels, Endian.little);
    header.setUint32(24, sampleRate, Endian.little);
    header.setUint32(28, byteRate, Endian.little);
    header.setUint16(32, blockAlign, Endian.little);
    header.setUint16(34, bitsPerSample, Endian.little);
    header.setUint8(36, 0x64); // d
    header.setUint8(37, 0x61); // a
    header.setUint8(38, 0x74); // t
    header.setUint8(39, 0x61); // a
    header.setUint32(40, dataSize, Endian.little);

    final wav = Uint8List(44 + dataSize);
    wav.setRange(0, 44, header.buffer.asUint8List());
    wav.setRange(44, 44 + dataSize, pcmData);
    return wav;
  }

  /// Speak [text] using on-device iOS TTS. Returns a Future that completes
  /// when speech finishes. Used as fallback when the backend is unavailable.
  static Future<void> speakOnDevice(String text) async {
    _flutterTts ??= FlutterTts();
    final tts = _flutterTts!;

    await tts.setLanguage('en-US');
    await tts.setSpeechRate(0.48);
    await tts.setPitch(1.0);
    await tts.setVolume(1.0);

    final completer = Completer<void>();
    tts.setCompletionHandler(() {
      if (!completer.isCompleted) completer.complete();
    });
    tts.setErrorHandler((msg) {
      Logger.debug('[TTS] On-device error: $msg');
      if (!completer.isCompleted) completer.complete();
    });
    tts.setCancelHandler(() {
      if (!completer.isCompleted) completer.complete();
    });

    await tts.speak(text);
    await completer.future;
  }

  /// Stop any active on-device TTS playback.
  static Future<void> stopOnDevice() async {
    if (_flutterTts != null) {
      await _flutterTts!.stop();
    }
  }

  /// JSON-escape a string value (with surrounding quotes).
  static String _jsonEscapeString(String s) {
    return '"${s.replaceAll('\\', '\\\\').replaceAll('"', '\\"').replaceAll('\n', '\\n').replaceAll('\r', '\\r')}"';
  }
}
