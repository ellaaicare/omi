import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/v2v_client.dart';

void main() {
  group('V2VClient provider mapping', () {
    test('treats realtime voice providers as session providers', () {
      expect(V2VClient.isSessionProvider('grok-voice'), isTrue);
      expect(V2VClient.isSessionProvider('openclaw-direct'), isTrue);
      expect(V2VClient.isSessionProvider('openai-native-realtime'), isTrue);
      expect(V2VClient.isSessionProvider('gemini-native-live'), isTrue);
      expect(V2VClient.isSessionProvider('openai-realtime'), isTrue);
      expect(V2VClient.isSessionProvider('gemini-live'), isTrue);
      expect(V2VClient.isSessionProvider('elevenlabs'), isFalse);
    });

    test('sends explicit voice mode for backend-friendly provider variants', () {
      expect(V2VClient.sessionVoiceMode('openclaw-direct'), 'openclaw-direct-v1');
      expect(V2VClient.sessionVoiceMode('openai-native-realtime'), 'openai-native-realtime-v1');
      expect(V2VClient.sessionVoiceMode('gemini-native-live'), 'gemini-native-live-v1');
      expect(V2VClient.sessionVoiceMode('openai-realtime'), 'openai-native-realtime-v1');
      expect(V2VClient.sessionVoiceMode('gemini-live'), 'gemini-native-live-v1');
      expect(V2VClient.sessionVoiceMode('grok-voice'), isNull);
    });

    test('normalizes saved legacy realtime provider ids', () {
      expect(V2VClient.normalizeProvider('openai-realtime'), 'openai-native-realtime');
      expect(V2VClient.normalizeProvider('gemini-live'), 'gemini-native-live');
      expect(V2VClient.normalizeProvider('openclaw-direct'), 'openclaw-direct');
    });

    test('maps proxy transcript_delta events as assistant transcripts', () {
      expect(V2VClient.treatsAsAssistantTranscriptEvent('transcript'), isTrue);
      expect(V2VClient.treatsAsAssistantTranscriptEvent('transcript_delta'), isTrue);
      expect(V2VClient.treatsAsAssistantTranscriptEvent('response.audio_transcript.delta'), isTrue);
      expect(V2VClient.treatsAsAssistantTranscriptEvent('user_transcript'), isFalse);
    });

    test('separates audio completion from generic response completion', () {
      expect(V2VClient.treatsAsAudioDoneEvent('audio_done'), isTrue);
      expect(V2VClient.treatsAsAudioDoneEvent('response.audio.done'), isTrue);
      expect(V2VClient.treatsAsAudioDoneEvent('output_audio.done'), isTrue);
      expect(V2VClient.treatsAsAudioDoneEvent('response.done'), isFalse);
      expect(V2VClient.treatsAsAudioDoneEvent('turn_complete'), isFalse);

      expect(V2VClient.treatsAsResponseCompleteEvent('response.done'), isTrue);
      expect(V2VClient.treatsAsResponseCompleteEvent('turn_complete'), isTrue);
      expect(V2VClient.treatsAsResponseCompleteEvent('audio_done'), isFalse);
    });
  });
}
