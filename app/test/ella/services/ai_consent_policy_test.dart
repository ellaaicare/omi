import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_policy.dart';

void main() {
  test('v4 processor manifest is versioned and covers every live recipient function', () {
    expect(AiConsentPolicy.version, SharedPreferencesUtil.currentAiConsentContractVersion);
    expect(AiConsentPolicy.processorSetHash, SharedPreferencesUtil.currentAiConsentProcessorSetHash);
    expect(AiConsentPolicy.processorSetHash, startsWith('sha256:'));
    expect(
      AiConsentPolicy.processorSetHash,
      'sha256:${sha256.convert(utf8.encode(AiConsentPolicy.canonicalProcessorSet))}',
    );

    final names = AiConsentPolicy.processors.map((processor) => processor.name).toSet();
    expect(
      names,
      containsAll({
        'Deepgram',
        'Google Firebase',
        'Ella self-hosted Hermes',
        'Ella self-hosted Honcho',
        'OpenRouter',
        'Google Gemini',
        'OpenAI',
        'Groq',
        'xAI Grok',
        'ElevenLabs',
      }),
    );
    expect(AiConsentPolicy.processors.every((processor) => processor.function.isNotEmpty), isTrue);
    expect(AiConsentPolicy.processors.every((processor) => processor.data.isNotEmpty), isTrue);
  });
}
