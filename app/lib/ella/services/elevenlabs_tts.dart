import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_tts/flutter_tts.dart';
import 'package:path_provider/path_provider.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/logger.dart';

/// TTS client that calls the backend TTS proxy (Kokoro local or ElevenLabs).
/// Falls back to on-device iOS TTS if the backend is unavailable.
class ElevenLabsTts {
  ElevenLabsTts._();

  static FlutterTts? _flutterTts;
  static String? _cachedTempDir;

  /// Synthesize [text] to speech via the backend TTS proxy.
  /// Returns the path to a temporary audio file, or null on failure.
  static Future<String?> synthesize(
    String text, {
    String? expectedAuthenticatedUid,
    ExactAccountAuthorityVerifier? exactAuthority,
  }) async {
    if (text.trim().isEmpty || !SharedPreferencesUtil().aiConsentAccepted) return null;
    _requireCurrent(exactAuthority, 'before TTS request');

    final url = '${Env.apiBaseUrl}v1/voice/tts';
    String? createdPath;

    try {
      final response = await makeApiCall(
        url: url,
        headers: {
          'Content-Type': 'application/json',
          if (!isHermesProvisioningGateEnabled) 'X-TTS-Provider': SharedPreferencesUtil().ttsProvider,
        },
        body: '{"text": ${_jsonEscapeString(text)}}',
        method: 'POST',
        timeout: const Duration(seconds: 30),
        expectedAuthenticatedUid: expectedAuthenticatedUid,
        exactAuthority: exactAuthority,
      );

      _requireCurrent(exactAuthority, 'after TTS response');
      if (response == null || response.statusCode != 200) {
        Logger.debug('[TTS] Backend failed: ${response?.statusCode}');
        return null;
      }

      _cachedTempDir ??= (await getTemporaryDirectory()).path;
      _requireCurrent(exactAuthority, 'after TTS directory lookup');
      final ts = DateTime.now().millisecondsSinceEpoch;
      final contentType = response.headers['content-type'] ?? '';

      if (contentType.contains('audio/L16') || contentType.contains('audio/pcm')) {
        // Raw PCM16 from local Kokoro — wrap in WAV header
        final sampleRate = _parseSampleRate(contentType);
        final wavBytes = _pcmToWav(response.bodyBytes, sampleRate: sampleRate);
        createdPath = '$_cachedTempDir/ella_tts_$ts.wav';
        _requireCurrent(exactAuthority, 'before TTS file creation');
        await File(createdPath).writeAsBytes(wavBytes, flush: true);
        _requireCurrent(exactAuthority, 'after TTS file creation');
        Logger.debug('[TTS] PCM→WAV ${response.bodyBytes.length}b, ${sampleRate}Hz → $createdPath');
        return createdPath;
      } else {
        // MP3 from ElevenLabs or other
        createdPath = '$_cachedTempDir/ella_tts_$ts.mp3';
        _requireCurrent(exactAuthority, 'before TTS file creation');
        await File(createdPath).writeAsBytes(response.bodyBytes, flush: true);
        _requireCurrent(exactAuthority, 'after TTS file creation');
        return createdPath;
      }
    } on ExactAccountAuthorityChangedException {
      if (createdPath != null) await discardSynthesizedFile(createdPath);
      rethrow;
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
  static Future<void> speakOnDevice(
    String text, {
    ExactAccountAuthorityVerifier? exactAuthority,
  }) async {
    if (text.trim().isEmpty) return;
    _requireCurrent(exactAuthority, 'before on-device TTS');
    _flutterTts ??= FlutterTts();
    final tts = _flutterTts!;

    if (Platform.isIOS) {
      await tts.setSharedInstance(true);
      _requireCurrent(exactAuthority, 'during on-device TTS setup');
      await tts.autoStopSharedSession(false);
      _requireCurrent(exactAuthority, 'during on-device TTS setup');
      await tts.setIosAudioCategory(
        IosTextToSpeechAudioCategory.playAndRecord,
        const [
          IosTextToSpeechAudioCategoryOptions.defaultToSpeaker,
          IosTextToSpeechAudioCategoryOptions.allowBluetooth,
          IosTextToSpeechAudioCategoryOptions.allowBluetoothA2DP,
          IosTextToSpeechAudioCategoryOptions.allowAirPlay,
        ],
        IosTextToSpeechAudioMode.voicePrompt,
      );
      _requireCurrent(exactAuthority, 'during on-device TTS setup');
    }
    await tts.setLanguage('en-US');
    _requireCurrent(exactAuthority, 'during on-device TTS setup');
    await tts.setSpeechRate(0.48);
    _requireCurrent(exactAuthority, 'during on-device TTS setup');
    await tts.setPitch(1.0);
    _requireCurrent(exactAuthority, 'during on-device TTS setup');
    await tts.setVolume(1.0);
    _requireCurrent(exactAuthority, 'during on-device TTS setup');
    await tts.awaitSpeakCompletion(false);
    _requireCurrent(exactAuthority, 'before on-device TTS playback');

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

    final result = await tts.speak(text);
    if (exactAuthority != null && !exactAuthority.isExactCurrent()) {
      await tts.stop();
      throw ExactAccountAuthorityChangedException('Exact account authority changed during on-device TTS playback');
    }
    if (result != 1) {
      Logger.debug('[TTS] On-device speak did not start: $result');
      if (!completer.isCompleted) completer.complete();
    }
    await completer.future;
    _requireCurrent(exactAuthority, 'after on-device TTS playback');
  }

  /// Stop any active on-device TTS playback.
  static Future<void> stopOnDevice() async {
    if (_flutterTts != null) {
      await _flutterTts!.stop();
    }
  }

  static Future<void> discardSynthesizedFile(String path) async {
    try {
      final file = File(path);
      if (await file.exists()) await file.delete();
    } catch (_) {}
  }

  static void _requireCurrent(ExactAccountAuthorityVerifier? authority, String boundary) {
    if (authority != null && !authority.isExactCurrent()) {
      throw ExactAccountAuthorityChangedException('Exact account authority changed $boundary');
    }
  }

  /// JSON-escape a string value (with surrounding quotes).
  static String _jsonEscapeString(String s) {
    return '"${s.replaceAll('\\', '\\\\').replaceAll('"', '\\"').replaceAll('\n', '\\n').replaceAll('\r', '\\r')}"';
  }
}
