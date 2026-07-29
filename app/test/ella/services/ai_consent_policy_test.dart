import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_policy.dart';

void main() {
  test('v8 fallback manifest matches the managed-cloud processor and scope contract', () {
    const policy = AiConsentPolicy.bundled;
    expect(policy.version, SharedPreferencesUtil.currentAiConsentContractVersion);
    expect(policy.version, 'ai-data-processors-v8');
    expect(policy.processorSetHash, SharedPreferencesUtil.currentAiConsentProcessorSetHash);
    expect(policy.processorSetHash, 'sha256:d06b3056e06f092557d2d0e9add6ca04a515dabe7f1b6dc948c3bedbd1a3016d');
    expect(policy.processorSetHash, 'sha256:${sha256.convert(utf8.encode(policy.canonicalProcessorSet))}');
    expect(policy.scopeVersion, SharedPreferencesUtil.currentAiConsentScopeVersion);
    expect(policy.scopeVersion, 'managed-cloud-internal-pilot-v2');
    expect(policy.scopeHash, 'sha256:2878e09958faadb799af99a8975736ce63010dd1d682cf944f60743a4faf92e5');
    expect(policy.scopeHash, SharedPreferencesUtil.currentAiConsentScopeHash);
    expect(policy.scopeHash, 'sha256:${sha256.convert(utf8.encode(policy.canonicalScope))}');

    final names = policy.processors.map((processor) => processor.name).toSet();
    final ids = policy.processors.map((processor) => processor.id).toSet();
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
        'Nous Research / Hermes Cloud',
        'Photon',
        'OpenRouter',
        'Google Gemini',
        'OpenAI',
        'Groq',
        'xAI Grok',
        'Inworld AI',
        'ElevenLabs',
      }),
    );
    expect(ids, containsAll({'honcho-self-hosted', 'nous-hermes-cloud', 'hermes-profile-memory', 'openai-codex'}));
    expect(ids, isNot(contains('honcho-cloud')));
    expect(names, isNot(contains('Honcho / Plastic Labs')));
    expect(policy.processors.every((processor) => processor.function.isNotEmpty), isTrue);
    expect(policy.processors.every((processor) => processor.data.isNotEmpty), isTrue);
    expect(policy.canonicalProcessorSet, contains('hermes-profile-memory:profile-scoped-memory'));
    expect(policy.canonicalProcessorSet, isNot(contains('honcho-cloud')));
    expect(policy.canonicalScope, contains('openai-codex/gpt-5.6-terra'));
    expect(policy.canonicalScope, contains('memory_provider=hermes_profile_scoped_memory'));
    expect(policy.canonicalScope, isNot(contains('honcho_cloud_profile_isolated')));
    expect(policy.canonicalScope, contains('allow_all=false'));
    expect(policy.canonicalScope, contains('caregiver=false'));
    expect(policy.canonicalScope, contains('attachments=false'));
    expect(policy.isBundledCurrent, isTrue);
  });

  test('a server policy with a changed processor set is display-only and never authority', () {
    final policy = AiConsentPolicy.fromJson({
      'version': SharedPreferencesUtil.currentAiConsentContractVersion,
      'processor_set_hash': 'sha256:changed',
      'canonical_processor_set': AiConsentPolicy.bundled.canonicalProcessorSet,
      'scope_version': AiConsentPolicy.bundled.scopeVersion,
      'scope_hash': AiConsentPolicy.bundled.scopeHash,
      'canonical_scope': AiConsentPolicy.bundled.canonicalScope,
      'processors': const [],
    });

    expect(policy.isBundledCurrent, isFalse);
  });

  test('a v7 policy cannot authorize the forward-only managed-cloud v8 disclosure', () {
    final policy = AiConsentPolicy.fromJson({
      'version': 'ai-data-processors-v7',
      'processor_set_hash': SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      'canonical_processor_set': AiConsentPolicy.bundled.canonicalProcessorSet,
      'scope_version': AiConsentPolicy.bundled.scopeVersion,
      'scope_hash': AiConsentPolicy.bundled.scopeHash,
      'canonical_scope': AiConsentPolicy.bundled.canonicalScope,
      'processors': const [],
    });

    expect(policy.isBundledCurrent, isFalse);
  });

  test('provider or Photon scope drift requires reconsent even when processors are unchanged', () {
    final policy = AiConsentPolicy.fromJson({
      'version': AiConsentPolicy.bundled.version,
      'processor_set_hash': AiConsentPolicy.bundled.processorSetHash,
      'canonical_processor_set': AiConsentPolicy.bundled.canonicalProcessorSet,
      'scope_version': AiConsentPolicy.bundled.scopeVersion,
      'scope_hash': 'sha256:changed',
      'canonical_scope': AiConsentPolicy.bundled.canonicalScope,
      'processors': AiConsentPolicy.bundled.processors
          .map(
            (processor) => {
              'id': processor.id,
              'legal_recipient': processor.name,
              'function': processor.function,
              'data': processor.data,
              'third_party': processor.isThirdParty,
            },
          )
          .toList(),
    });

    expect(policy.isBundledCurrent, isFalse);
  });
}
