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
    return AiConsentProcessor(
      id: json['id'] as String? ?? '',
      name: json['legal_recipient'] as String? ?? '',
      function: json['function'] as String? ?? '',
      data: json['data'] as String? ?? '',
      isThirdParty: json['third_party'] as bool? ?? true,
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
    required this.processors,
  });

  factory AiConsentPolicy.fromJson(Map<String, dynamic> json) {
    final processors = (json['processors'] as List<dynamic>? ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(AiConsentProcessor.fromJson)
        .toList(growable: false);
    return AiConsentPolicy(
      version: json['version'] as String? ?? '',
      processorSetHash: json['processor_set_hash'] as String? ?? '',
      canonicalProcessorSet: json['canonical_processor_set'] as String? ?? '',
      processors: processors,
    );
  }

  final String version;
  final String processorSetHash;
  final String canonicalProcessorSet;
  final List<AiConsentProcessor> processors;

  bool get isBundledCurrent {
    if (version != bundled.version ||
        processorSetHash != bundled.processorSetHash ||
        canonicalProcessorSet != bundled.canonicalProcessorSet ||
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
        'openrouter:model-routing|google-gemini:language-live-voice|openai:language-live-voice|'
        'groq:language|xai-grok:language-live-voice|inworld:tts|elevenlabs:tts-fallback',
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
      AiConsentProcessor(
        id: 'groq',
        name: 'Groq',
        function: 'Language processing',
        data: 'Text and selected context',
      ),
      AiConsentProcessor(
        id: 'xai-grok',
        name: 'xAI Grok',
        function: 'Language processing and live voice',
        data: 'Text, selected context, or live microphone audio',
      ),
      AiConsentProcessor(
        id: 'inworld',
        name: 'Inworld AI',
        function: 'Voice synthesis',
        data: 'Response text',
      ),
      AiConsentProcessor(
        id: 'elevenlabs',
        name: 'ElevenLabs',
        function: 'Fallback voice synthesis',
        data: 'Response text',
      ),
    ],
  );
}
