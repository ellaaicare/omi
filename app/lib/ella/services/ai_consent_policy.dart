import 'package:omi/backend/preferences.dart';

class AiConsentProcessor {
  const AiConsentProcessor({
    required this.name,
    required this.function,
    required this.data,
    this.isThirdParty = true,
  });

  final String name;
  final String function;
  final String data;
  final bool isThirdParty;
}

class AiConsentPolicy {
  const AiConsentPolicy._();

  static const version = SharedPreferencesUtil.currentAiConsentContractVersion;
  static const processorSetHash = SharedPreferencesUtil.currentAiConsentProcessorSetHash;
  static const canonicalProcessorSet = 'deepgram:stt|firebase:auth-infrastructure|hermes-self-hosted:agent-runtime|'
      'honcho-self-hosted:memory-context|openrouter:model-routing|google-gemini:language-live-voice|'
      'openai:language-live-voice|groq:language|xai-grok:language-live-voice|elevenlabs:tts-fallback';

  static const processors = <AiConsentProcessor>[
    AiConsentProcessor(name: 'Deepgram', function: 'Speech transcription', data: 'Live or stored microphone audio'),
    AiConsentProcessor(
      name: 'Google Firebase',
      function: 'Authentication and service infrastructure',
      data: 'Account and service metadata',
    ),
    AiConsentProcessor(
      name: 'Ella self-hosted Hermes',
      function: 'Agent reasoning',
      data: 'Messages, transcripts, and selected memory context',
      isThirdParty: false,
    ),
    AiConsentProcessor(
      name: 'Ella self-hosted Honcho',
      function: 'Memory context',
      data: 'Derived text and selected memory relationships',
      isThirdParty: false,
    ),
    AiConsentProcessor(
      name: 'OpenRouter',
      function: 'Model routing',
      data: 'Messages, transcripts, and selected memory context',
    ),
    AiConsentProcessor(
      name: 'Google Gemini',
      function: 'Language processing and live voice',
      data: 'Text, selected context, or live microphone audio',
    ),
    AiConsentProcessor(
      name: 'OpenAI',
      function: 'Language processing and live voice',
      data: 'Text, selected context, or live microphone audio',
    ),
    AiConsentProcessor(name: 'Groq', function: 'Language processing', data: 'Text and selected context'),
    AiConsentProcessor(
      name: 'xAI Grok',
      function: 'Language processing and live voice',
      data: 'Text, selected context, or live microphone audio',
    ),
    AiConsentProcessor(
      name: 'ElevenLabs',
      function: 'Fallback voice synthesis',
      data: 'Response text',
    ),
  ];
}
