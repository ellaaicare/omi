import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/v2v_client.dart';

void main() {
  group('V2VClient provider contract', () {
    test('normalizes legacy provider ids and recognizes canonical session providers', () {
      expect(V2VClient.normalizeProvider('gemini-live'), 'gemini-native-live');
      expect(V2VClient.normalizeProvider('openai-realtime'), 'openai-native-realtime');
      expect(V2VClient.isSessionProvider('grok-voice'), isTrue);
      expect(V2VClient.isSessionProvider('gemini-live'), isTrue);
      expect(V2VClient.isSessionProvider('gemini-native-live'), isTrue);
      expect(V2VClient.isSessionProvider('elevenlabs'), isFalse);
    });

    test('retained compatibility receipt falls back to selected local provider', () {
      expect(
        V2VClient.resolveEffectiveProvider(provisionedProvider: '', selectedProvider: 'gemini-live'),
        'gemini-native-live',
      );
      expect(
        V2VClient.resolveEffectiveProvider(
          provisionedProvider: 'grok-voice',
          selectedProvider: 'gemini-native-live',
        ),
        'grok-voice',
      );
    });

    test('authenticated session body preserves provider and mode while omitting uid', () {
      expect(
        V2VClient.buildSessionRequestBody(
          uid: 'firebase-user-1',
          provider: 'gemini-live',
          includeUid: false,
        ),
        {
          'provider': 'gemini-native-live',
          'voice_mode': 'gemini-native-live-v1',
        },
      );
    });

    test('legacy session body includes uid without changing canonical provider', () {
      expect(
        V2VClient.buildSessionRequestBody(
          uid: 'legacy-user-1',
          provider: 'grok-voice',
          includeUid: true,
        ),
        {
          'uid': 'legacy-user-1',
          'provider': 'grok-voice',
        },
      );
    });

    test('provider registry filters unavailable and non-V2V entries and deduplicates aliases', () {
      final providers = V2VClient.availableSessionProviders({
        'providers': [
          {'id': 'elevenlabs', 'type': 'tts', 'available': true},
          {'id': 'grok-voice', 'type': 'v2v', 'available': false},
          {'id': 'gemini-live', 'type': 'v2v', 'available': true},
          {'id': 'gemini-native-live', 'type': 'v2v', 'available': true},
        ],
      });

      expect(providers, {'gemini-native-live'});
    });

    test('failure receipts extract bounded codes without retaining opaque tokens', () {
      expect(
        V2VClient.safeErrorCode('{"detail":{"code":"isolated_voice_not_ready"}}'),
        'isolated_voice_not_ready',
      );
      expect(
        V2VClient.safeErrorCode(
          '{"detail":"eyJhbGciOiJIUzI1NiJ9.payload.signature"}',
          fallback: 'session_request_failed',
        ),
        'session_request_failed',
      );
      expect(
        V2VClient.safeErrorCode('{"detail":"Provider grok-voice is not configured (missing API key)"}'),
        'provider_not_configured',
      );
    });
  });

  group('V2VClient proxy event mapping', () {
    test('maps transcript deltas as assistant transcripts', () {
      expect(V2VClient.treatsAsAssistantTranscriptEvent('transcript'), isTrue);
      expect(V2VClient.treatsAsAssistantTranscriptEvent('transcript_delta'), isTrue);
      expect(V2VClient.treatsAsAssistantTranscriptEvent('response.audio_transcript.delta'), isTrue);
      expect(V2VClient.treatsAsAssistantTranscriptEvent('user_transcript'), isFalse);
    });

    test('separates audio completion from generic response completion', () {
      expect(V2VClient.treatsAsAudioDoneEvent('response.audio.done'), isTrue);
      expect(V2VClient.treatsAsAudioDoneEvent('response.done'), isFalse);
      expect(V2VClient.treatsAsResponseCompleteEvent('response.done'), isTrue);
      expect(V2VClient.treatsAsResponseCompleteEvent('audio_done'), isFalse);
    });
  });
}
