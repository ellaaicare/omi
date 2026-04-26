import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/v2v_client.dart';

void main() {
  group('V2VClient provider mapping', () {
    test('treats realtime voice providers as session providers', () {
      expect(V2VClient.isSessionProvider('grok-voice'), isTrue);
      expect(V2VClient.isSessionProvider('openclaw-direct'), isTrue);
      expect(V2VClient.isSessionProvider('openai-native-realtime'), isTrue);
      expect(V2VClient.isSessionProvider('gemini-native-live'), isTrue);
      expect(V2VClient.isSessionProvider('elevenlabs'), isFalse);
      expect(V2VClient.isSessionProvider('openai-realtime'), isFalse);
      expect(V2VClient.isSessionProvider('gemini-live'), isFalse);
    });

    test('sends explicit voice mode for backend-friendly provider variants', () {
      expect(V2VClient.sessionVoiceMode('openclaw-direct'), 'openclaw-direct-v1');
      expect(V2VClient.sessionVoiceMode('openai-native-realtime'), 'openai-native-realtime-v1');
      expect(V2VClient.sessionVoiceMode('gemini-native-live'), 'gemini-native-live-v1');
      expect(V2VClient.sessionVoiceMode('grok-voice'), isNull);
      expect(V2VClient.sessionVoiceMode('gemini-live'), isNull);
      expect(V2VClient.sessionVoiceMode('openai-realtime'), isNull);
    });
  });
}
