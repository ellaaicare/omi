import 'package:omi/backend/preferences.dart';

class AiConsentProcessor {
  const AiConsentProcessor({
    required this.id,
    required this.name,
    required this.function,
    required this.data,
    required this.providerAliases,
    this.isThirdParty = true,
  });

  factory AiConsentProcessor.fromJson(Map<String, dynamic> json) {
    String readString(String key) {
      final value = json[key];
      return value is String ? value : '';
    }

    return AiConsentProcessor(
      id: readString('id'),
      name: readString('legal_recipient'),
      function: readString('function'),
      data: readString('data'),
      providerAliases:
          (json['provider_aliases'] is List<dynamic> ? json['provider_aliases'] as List<dynamic> : const [])
              .whereType<String>()
              .toList(growable: false),
      isThirdParty: json['third_party'] is bool ? json['third_party'] as bool : true,
    );
  }

  final String id;
  final String name;
  final String function;
  final String data;
  final List<String> providerAliases;
  final bool isThirdParty;

  bool get isValid => id.isNotEmpty && name.isNotEmpty && function.isNotEmpty && data.isNotEmpty;

  Map<String, dynamic> toJson() => {
        'id': id,
        'legal_recipient': name,
        'function': function,
        'data': data,
        'provider_aliases': providerAliases,
        'third_party': isThirdParty,
      };
}

class AiConsentPolicy {
  const AiConsentPolicy({
    required this.version,
    required this.minimumRequiredVersion,
    required this.processorSetHash,
    required this.canonicalProcessorSet,
    required this.scopeVersion,
    required this.scopeHash,
    required this.canonicalScope,
    required this.processors,
  });

  factory AiConsentPolicy.fromJson(Map<String, dynamic> json) {
    String readString(String key) {
      final value = json[key];
      return value is String ? value : '';
    }

    final rawProcessors = json['processors'];
    final processors = (rawProcessors is List<dynamic> ? rawProcessors : const <dynamic>[])
        .whereType<Map<String, dynamic>>()
        .map(AiConsentProcessor.fromJson)
        .toList(growable: false);
    return AiConsentPolicy(
      version: readString('version'),
      minimumRequiredVersion: readString('minimum_required_version'),
      processorSetHash: readString('processor_set_hash'),
      canonicalProcessorSet: readString('canonical_processor_set'),
      scopeVersion: readString('scope_version'),
      scopeHash: readString('scope_hash'),
      canonicalScope: readString('canonical_scope'),
      processors: processors,
    );
  }

  final String version;
  final String minimumRequiredVersion;
  final String processorSetHash;
  final String canonicalProcessorSet;
  final String scopeVersion;
  final String scopeHash;
  final String canonicalScope;
  final List<AiConsentProcessor> processors;

  bool matches(AiConsentPolicy expected) {
    if (version != expected.version ||
        minimumRequiredVersion != expected.minimumRequiredVersion ||
        processorSetHash != expected.processorSetHash ||
        canonicalProcessorSet != expected.canonicalProcessorSet ||
        scopeVersion != expected.scopeVersion ||
        scopeHash != expected.scopeHash ||
        canonicalScope != expected.canonicalScope ||
        processors.length != expected.processors.length) {
      return false;
    }
    for (var index = 0; index < processors.length; index++) {
      final processor = processors[index];
      final expectedProcessor = expected.processors[index];
      if (!processor.isValid ||
          processor.id != expectedProcessor.id ||
          processor.name != expectedProcessor.name ||
          processor.function != expectedProcessor.function ||
          processor.data != expectedProcessor.data ||
          !_sameStrings(processor.providerAliases, expectedProcessor.providerAliases) ||
          processor.isThirdParty != expectedProcessor.isThirdParty) {
        return false;
      }
    }
    return true;
  }

  bool get isBundledCurrent => matches(bundled);

  bool get isSupportedOperational => isBundledCurrent || matches(legacyV10);

  Map<String, dynamic> toJson() => {
        'version': version,
        'minimum_required_version': minimumRequiredVersion,
        'processor_set_hash': processorSetHash,
        'canonical_processor_set': canonicalProcessorSet,
        'scope_version': scopeVersion,
        'scope_hash': scopeHash,
        'canonical_scope': canonicalScope,
        'processors': processors.map((processor) => processor.toJson()).toList(growable: false),
      };

  static bool _sameStrings(List<String> left, List<String> right) {
    if (left.length != right.length) return false;
    for (var index = 0; index < left.length; index++) {
      if (left[index] != right[index]) return false;
    }
    return true;
  }

  static const bundled = AiConsentPolicy(
    version: SharedPreferencesUtil.currentAiConsentContractVersion,
    minimumRequiredVersion: SharedPreferencesUtil.legacyAiConsentContractVersionV10,
    processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
    canonicalProcessorSet: 'deepgram:stt|soniox:stt|speechmatics:stt|firebase:auth-infrastructure|'
        'hermes-self-hosted:agent-runtime|honcho-self-hosted:memory-context|ella-self-hosted-tts:tts|'
        'nous-hermes-cloud:managed-agent-runtime|hermes-profile-memory:profile-scoped-memory|'
        'openai-codex:managed-agent-model-memory-illustration|photon:messaging-delivery|'
        'openrouter:model-routing|google-gemini:language-live-voice|openai:language-live-voice|'
        'groq:language|xai-grok:language-live-voice|'
        'inworld:tts|elevenlabs:tts-fallback|typesafe:guardian-whispers-safety-classification',
    scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
    scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    canonicalScope: 'profile_binding=server-profile-v1|runtime_provider=hermes_cloud|'
        'model_route=openai-codex/gpt-5.6-terra|memory_provider=hermes_profile_scoped_memory|'
        'photon_scope=shared_test_line_explicit_contact_v1;allow_all=false;caregiver=false;attachments=false|'
        'artwork_provider=openai-codex/gpt-image-2-medium;reasoning_host=openai-codex/gpt-5.6-luna;'
        'source=selected_memory_summary_only;raw_audio=false;source_photos=false',
    processors: [
      AiConsentProcessor(
        id: 'deepgram',
        name: 'Deepgram',
        function: 'Speech transcription',
        data: 'Live or stored microphone audio',
        providerAliases: ['deepgram', 'deepgram-streaming'],
      ),
      AiConsentProcessor(
        id: 'soniox',
        name: 'Soniox',
        function: 'Speech transcription',
        data: 'Live or stored microphone audio',
        providerAliases: ['soniox', 'soniox-streaming'],
      ),
      AiConsentProcessor(
        id: 'speechmatics',
        name: 'Speechmatics',
        function: 'Speech transcription',
        data: 'Live or stored microphone audio',
        providerAliases: ['speechmatics', 'speechmatics-streaming'],
      ),
      AiConsentProcessor(
        id: 'firebase',
        name: 'Google Firebase',
        function: 'Authentication and service infrastructure',
        data: 'Account and service metadata',
        providerAliases: ['firebase', 'google-firebase'],
      ),
      AiConsentProcessor(
        id: 'hermes-self-hosted',
        name: 'Ella self-hosted Hermes',
        function: 'Agent reasoning',
        data: 'Messages, transcripts, and selected memory context',
        providerAliases: ['hermes', 'hermes-self-hosted', 'hermes-retained', 'hermes-isolated'],
        isThirdParty: false,
      ),
      AiConsentProcessor(
        id: 'honcho-self-hosted',
        name: 'Ella self-hosted Honcho',
        function: 'Memory context',
        data: 'Derived text and selected memory relationships',
        providerAliases: ['honcho', 'honcho-self-hosted'],
        isThirdParty: false,
      ),
      AiConsentProcessor(
        id: 'ella-self-hosted-tts',
        name: 'Ella self-hosted voice synthesis',
        function: 'Voice synthesis',
        data: 'Response text',
        providerAliases: ['fish-audio', 'fish-audio-s1', 'fish-audio-s2', 'kokoro'],
        isThirdParty: false,
      ),
      AiConsentProcessor(
        id: 'nous-hermes-cloud',
        name: 'Nous Research / Hermes Cloud',
        function: 'Managed agent runtime',
        data: 'What the person says or types, details they choose to share, and basic session information',
        providerAliases: ['hermes-cloud', 'hermes_cloud', 'nous-hermes-cloud'],
      ),
      AiConsentProcessor(
        id: 'hermes-profile-memory',
        name: 'Nous Research / Hermes Cloud',
        function: 'Built-in profile-scoped memory and context inside the managed Hermes Cloud runtime',
        data:
            'Profile-bound conversation text, saved facts, derived memory context, and session identifiers needed to retrieve memory for the same account/profile scope',
        providerAliases: ['hermes-profile-memory', 'hermes_profile_scoped_memory'],
      ),
      AiConsentProcessor(
        id: 'openai-codex',
        name: 'OpenAI',
        function: 'Managed agent processing and saved-memory illustration',
        data:
            'Model input and output, plus a selected memory title and summary for an illustration, through the approved OpenAI Codex OAuth route; no raw microphone audio or source photos for artwork',
        providerAliases: [
          'openai-codex',
          'openai-codex/gpt-5.6-terra',
          'openai-codex/gpt-5.6-luna',
          'gpt-image-2-medium',
        ],
      ),
      AiConsentProcessor(
        id: 'photon',
        name: 'Photon',
        function: 'Test/shared-line message delivery',
        data: 'Message content and messaging identifiers for one explicitly allowed test contact',
        providerAliases: ['photon', 'hermes-cloud-photon'],
      ),
      AiConsentProcessor(
        id: 'openrouter',
        name: 'OpenRouter',
        function: 'Model routing',
        data: 'Messages, transcripts, and selected memory context',
        providerAliases: ['openrouter'],
      ),
      AiConsentProcessor(
        id: 'google-gemini',
        name: 'Google Gemini',
        function: 'Language processing and live voice',
        data: 'Text, selected context, or live microphone audio',
        providerAliases: ['gemini', 'gemini-live', 'gemini-native-live', 'google-gemini'],
      ),
      AiConsentProcessor(
        id: 'openai',
        name: 'OpenAI',
        function: 'Language processing and live voice',
        data: 'Text, selected context, or live microphone audio',
        providerAliases: ['openai', 'openai-native-realtime'],
      ),
      AiConsentProcessor(
        id: 'groq',
        name: 'Groq',
        function: 'Language processing',
        data: 'Text and selected context',
        providerAliases: ['groq'],
      ),
      AiConsentProcessor(
        id: 'xai-grok',
        name: 'xAI Grok',
        function: 'Language processing and live voice',
        data: 'Text, selected context, or live microphone audio',
        providerAliases: ['grok', 'grok-voice', 'xai', 'xai-grok', 'xai-tts'],
      ),
      AiConsentProcessor(
        id: 'typesafe',
        name: 'TypeSafe (Jev), via OpenRouter',
        function: 'Conversation safety classification for Guardian and Whispers',
        data: 'Conversation transcript text windows (no audio)',
        providerAliases: ['typesafe', 'typesafe-jev', 'jev'],
      ),
      AiConsentProcessor(
        id: 'inworld',
        name: 'Inworld AI',
        function: 'Voice synthesis',
        data: 'Response text',
        providerAliases: ['inworld'],
      ),
      AiConsentProcessor(
        id: 'elevenlabs',
        name: 'ElevenLabs',
        function: 'Fallback voice synthesis',
        data: 'Response text',
        providerAliases: ['elevenlabs'],
      ),
    ],
  );

  static final legacyV10 = AiConsentPolicy(
    version: SharedPreferencesUtil.legacyAiConsentContractVersionV10,
    minimumRequiredVersion: SharedPreferencesUtil.legacyAiConsentContractVersionV10,
    processorSetHash: SharedPreferencesUtil.legacyAiConsentProcessorSetHashV10,
    canonicalProcessorSet: bundled.canonicalProcessorSet.replaceFirst(
      '|typesafe:guardian-whispers-safety-classification',
      '',
    ),
    scopeVersion: bundled.scopeVersion,
    scopeHash: bundled.scopeHash,
    canonicalScope: bundled.canonicalScope,
    processors: bundled.processors.where((processor) => processor.id != 'typesafe').toList(growable: false),
  );
}
