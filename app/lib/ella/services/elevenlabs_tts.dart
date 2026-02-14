import 'dart:io';

import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/logger.dart';

/// TTS client that calls the backend proxy at /v1/voice/tts.
/// Backend proxies to ElevenLabs so the API key stays server-side.
class ElevenLabsTts {
  ElevenLabsTts._();

  /// Synthesize [text] to speech via the backend TTS proxy.
  /// Returns the path to a temporary .mp3 file, or null on failure.
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
        Logger.debug('[ElevenLabsTts] Failed: ${response?.statusCode}');
        return null;
      }

      // Save audio bytes to a temp file
      final dir = await getTemporaryDirectory();
      final file = File('${dir.path}/ella_tts_${DateTime.now().millisecondsSinceEpoch}.mp3');
      await file.writeAsBytes(response.bodyBytes);
      return file.path;
    } catch (e) {
      Logger.debug('[ElevenLabsTts] Error: $e');
      return null;
    }
  }

  /// JSON-escape a string value (with surrounding quotes).
  static String _jsonEscapeString(String s) {
    return '"${s.replaceAll('\\', '\\\\').replaceAll('"', '\\"').replaceAll('\n', '\\n').replaceAll('\r', '\\r')}"';
  }
}
