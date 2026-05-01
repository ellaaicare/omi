import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/models/guardian_mode.dart';

void main() {
  group('GuardianVoiceConfig', () {
    test('defaults to match active provider', () {
      const config = GuardianVoiceConfig();

      expect(config.policy, GuardianVoicePolicy.matchActiveProvider);
      expect(config.provider, isNull);
      expect(
        config.toJson(uid: 'uid-1'),
        {
          'uid': 'uid-1',
          'guardian_voice_policy': 'match_active_provider',
          'guardian_voice_provider': null,
        },
      );
    });

    test('parses backend snake case fields', () {
      final config = GuardianVoiceConfig.fromJson({
        'guardian_voice_policy': 'pinned_provider',
        'guardian_voice_provider': 'xai-tts',
        'last_voice_provider': 'grok-voice',
        'resolved_provider': 'xai-tts',
        'fallback_provider': 'openai',
        'trace_id': 'trace-1',
        'queue_item_id': 'guardian_1',
      });

      expect(config.policy, GuardianVoicePolicy.pinnedProvider);
      expect(config.provider, 'xai-tts');
      expect(config.lastVoiceProvider, 'grok-voice');
      expect(config.resolvedProvider, 'xai-tts');
      expect(config.fallbackProvider, 'openai');
      expect(config.traceId, 'trace-1');
      expect(config.queueItemId, 'guardian_1');
    });

    test('parses nested response wrappers', () {
      final config = GuardianVoiceConfig.fromJson({
        'success': true,
        'data': {
          'policy': 'match_active_provider',
          'provider': 'elevenlabs',
          'resolvedProvider': 'elevenlabs',
        },
      });

      expect(config.policy, GuardianVoicePolicy.matchActiveProvider);
      expect(config.provider, 'elevenlabs');
      expect(config.resolvedProvider, 'elevenlabs');
    });

    test('only persists provider for pinned policy', () {
      final pinned = const GuardianVoiceConfig(
        policy: GuardianVoicePolicy.pinnedProvider,
        provider: 'kokoro',
      ).toJson(uid: 'uid-1');
      final matching = const GuardianVoiceConfig(
        policy: GuardianVoicePolicy.matchActiveProvider,
        provider: 'kokoro',
      ).toJson(uid: 'uid-1');

      expect(pinned['guardian_voice_provider'], 'kokoro');
      expect(matching['guardian_voice_provider'], isNull);
    });
  });
}
