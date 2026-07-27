import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_policy.dart';

void main() {
  test('v5 fallback manifest matches the accepted server processor contract', () {
    const policy = AiConsentPolicy.bundled;
    expect(policy.version, SharedPreferencesUtil.currentAiConsentContractVersion);
    expect(policy.processorSetHash, SharedPreferencesUtil.currentAiConsentProcessorSetHash);
    expect(policy.processorSetHash, startsWith('sha256:'));
    expect(
      policy.processorSetHash,
      'sha256:${sha256.convert(utf8.encode(policy.canonicalProcessorSet))}',
    );

    final names = policy.processors.map((processor) => processor.name).toSet();
    expect(
      names,
      containsAll({
        'Deepgram',
        'Soniox',
        'Speechmatics',
        'Google Firebase',
        'Ella self-hosted Hermes',
        'Ella self-hosted Honcho',
        'Ella self-hosted voice synthesis',
        'OpenRouter',
        'Google Gemini',
        'OpenAI',
        'Groq',
        'xAI Grok',
        'Inworld AI',
        'ElevenLabs',
      }),
    );
    expect(policy.processors.every((processor) => processor.function.isNotEmpty), isTrue);
    expect(policy.processors.every((processor) => processor.data.isNotEmpty), isTrue);
    expect(policy.isBundledCurrent, isTrue);
  });

  test('a server policy with a changed processor set is display-only and never authority', () {
    final policy = AiConsentPolicy.fromJson({
      'version': SharedPreferencesUtil.currentAiConsentContractVersion,
      'processor_set_hash': 'sha256:changed',
      'canonical_processor_set': AiConsentPolicy.bundled.canonicalProcessorSet,
      'processors': const [],
    });

    expect(policy.isBundledCurrent, isFalse);
  });
}
