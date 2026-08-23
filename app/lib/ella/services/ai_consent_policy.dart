import 'package:omi/backend/preferences.dart';

class AiConsentProcessor {
  const AiConsentProcessor({
    required this.id,
    required this.name,
    required this.function,
    required this.data,
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
      isThirdParty: json['third_party'] is bool ? json['third_party'] as bool : true,
    );
  }

  final String id;
  final String name;
  final String function;
  final String data;
  final bool isThirdParty;

  bool get isValid => id.isNotEmpty && name.isNotEmpty && function.isNotEmpty && data.isNotEmpty;
}

class AiConsentPolicy {
  const AiConsentPolicy({
    required this.version,
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
      processorSetHash: readString('processor_set_hash'),
      canonicalProcessorSet: readString('canonical_processor_set'),
      scopeVersion: readString('scope_version'),
      scopeHash: readString('scope_hash'),
      canonicalScope: readString('canonical_scope'),
      processors: processors,
    );
  }

  final String version;
  final String processorSetHash;
  final String canonicalProcessorSet;
  final String scopeVersion;
  final String scopeHash;
  final String canonicalScope;
  final List<AiConsentProcessor> processors;

  bool get isBundledCurrent {
    if (version != bundled.version ||
        processorSetHash != bundled.processorSetHash ||
        canonicalProcessorSet != bundled.canonicalProcessorSet ||
        scopeVersion != bundled.scopeVersion ||
        scopeHash != bundled.scopeHash ||
        canonicalScope != bundled.canonicalScope ||
        processors.length != bundled.processors.length) {
      return false;
    }
    for (var index = 0; index < processors.length; index++) {
      final processor = processors[index];
      final expected = bundled.processors[index];
      if (!processor.isValid ||
          processor.id != expected.id ||
          processor.name != expected.name ||
          processor.function != expected.function ||
          processor.data != expected.data ||
          processor.isThirdParty != expected.isThirdParty) {
        return false;
      }
    }
    return true;
  }

  static const bundled = AiConsentPolicy(
    version: SharedPreferencesUtil.currentAiConsentContractVersion,
    processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
    canonicalProcessorSet: 'deepgram:stt|soniox:stt|speechmatics:stt|firebase:auth-infrastructure|'
        'hermes-self-hosted:agent-runtime|honcho-self-hosted:memory-context|ella-self-hosted-tts:tts|'
        'nous-hermes-cloud:managed-agent-runtime|hermes-profile-memory:profile-scoped-memory|'
        'openai-codex:managed-agent-model|photon:messaging-delivery|'
        'openrouter:model-routing|google-gemini:language-live-voice|openai:language-live-voice|'
        'groq:language|xai-grok:language-live-voice|xai-imagine:memory-illustration|'
        'inworld:tts|elevenlabs:tts-fallback',
    scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
    scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    canonicalScope: 'profile_binding=server-profile-v1|runtime_provider=hermes_cloud|'
        'model_route=openai-codex/gpt-5.6-terra|memory_provider=hermes_profile_scoped_memory|'
        'photon_scope=shared_test_line_explicit_contact_v1;allow_all=false;caregiver=false;attachments=false|'
        'artwork_provider=xai/grok-imagine-image-2.0;source=selected_memory_summary_only;raw_audio=false;source_photos=false',
    processors: [
      AiConsentProcessor(
        id: 'deepgram',
        name: 'Deepgram',
        function: 'Speech transcription',
        data: 'Live or stored microphone audio',
      ),
      AiConsentProcessor(
        id: 'soniox',
        name: 'Soniox',
        function: 'Speech transcription',
        data: 'Live or stored microphone audio',
      ),
      AiConsentProcessor(
        id: 'speechmatics',
        name: 'Speechmatics',
        function: 'Speech transcription',
        data: 'Live or stored microphone audio',
      ),
      AiConsentProcessor(
        id: 'firebase',
        name: 'Google Firebase',
        function: 'Authentication and service infrastructure',
        data: 'Account and service metadata',
      ),
      AiConsentProcessor(
        id: 'hermes-self-hosted',
        name: 'Ella self-hosted Hermes',
        function: 'Agent reasoning',
        data: 'Messages, transcripts, and selected memory context',
        isThirdParty: false,
      ),
      AiConsentProcessor(
        id: 'honcho-self-hosted',
        name: 'Ella self-hosted Honcho',
        function: 'Memory context',
        data: 'Derived text and selected memory relationships',
        isThirdParty: false,
      ),
      AiConsentProcessor(
        id: 'ella-self-hosted-tts',
        name: 'Ella self-hosted voice synthesis',
        function: 'Voice synthesis',
        data: 'Response text',
        isThirdParty: false,
      ),
      AiConsentProcessor(
        id: 'nous-hermes-cloud',
        name: 'Nous Research / Hermes Cloud',
        function: 'Managed agent runtime',
        data: 'What the person says or types, details they choose to share, and basic session information',
      ),
      AiConsentProcessor(
        id: 'hermes-profile-memory',
        name: 'Nous Research / Hermes Cloud',
        function: 'Built-in profile-scoped memory and context inside the managed Hermes Cloud runtime',
        data:
            'Profile-bound conversation text, saved facts, derived memory context, and session identifiers needed to retrieve memory for the same account/profile scope',
      ),
      AiConsentProcessor(
        id: 'openai-codex',
        name: 'OpenAI',
        function: 'Managed agent model processing',
        data: 'Model input and output through the approved OpenAI Codex OAuth route',
      ),
      AiConsentProcessor(
        id: 'photon',
        name: 'Photon',
        function: 'Test/shared-line message delivery',
        data: 'Message content and messaging identifiers for one explicitly allowed test contact',
      ),
      AiConsentProcessor(
        id: 'openrouter',
        name: 'OpenRouter',
        function: 'Model routing',
        data: 'Messages, transcripts, and selected memory context',
      ),
      AiConsentProcessor(
        id: 'google-gemini',
        name: 'Google Gemini',
        function: 'Language processing and live voice',
        data: 'Text, selected context, or live microphone audio',
      ),
      AiConsentProcessor(
        id: 'openai',
        name: 'OpenAI',
        function: 'Language processing and live voice',
        data: 'Text, selected context, or live microphone audio',
      ),
      AiConsentProcessor(id: 'groq', name: 'Groq', function: 'Language processing', data: 'Text and selected context'),
      AiConsentProcessor(
        id: 'xai-grok',
        name: 'xAI Grok',
        function: 'Language processing and live voice',
        data: 'Text, selected context, or live microphone audio',
      ),
      AiConsentProcessor(
        id: 'xai-imagine',
        name: 'xAI',
        function: 'Illustrations for saved memories',
        data: 'Selected memory title and summary; no raw microphone audio or source photos',
      ),
      AiConsentProcessor(id: 'inworld', name: 'Inworld AI', function: 'Voice synthesis', data: 'Response text'),
      AiConsentProcessor(
        id: 'elevenlabs',
        name: 'ElevenLabs',
        function: 'Fallback voice synthesis',
        data: 'Response text',
      ),
    ],
  );
}
