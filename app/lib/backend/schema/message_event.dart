import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/transcript_segment.dart';

abstract class MessageEvent {
  final String eventType;

  MessageEvent({required this.eventType});

  factory MessageEvent.fromJson(Map<String, dynamic> json) {
    // Support both 'type' (legacy) and 'event' (voice mode) keys
    final eventType = json['type'] ?? json['event'];
    switch (eventType) {
      case 'service_status':
        return MessageServiceStatusEvent.fromJson(json);
      case 'memory_processing_started':
        return ConversationProcessingStartedEvent.fromJson(json);
      case 'memory_created':
        return ConversationEvent.fromJson(json);
      case 'last_memory':
        return LastConversationEvent.fromJson(json);
      case 'translating':
        return TranslationEvent.fromJson(json);
      case 'photo_processing':
        return PhotoProcessingEvent.fromJson(json);
      case 'photo_described':
        return PhotoDescribedEvent.fromJson(json);
      case 'speaker_label_suggestion':
        return SpeakerLabelSuggestionEvent.fromJson(json);
      // Voice Mode Events
      case 'voice_mode_active':
        return VoiceModeActiveEvent.fromJson(json);
      case 'voice_transcription':
        return VoiceTranscriptionEvent.fromJson(json);
      case 'voice_status':
        return VoiceStatusEvent.fromJson(json);
      case 'voice_response_audio':
        return VoiceResponseAudioEvent.fromJson(json);
      case 'voice_response_complete':
        return VoiceResponseCompleteEvent.fromJson(json);
      case 'voice_mode_ended':
        return VoiceModeEndedEvent.fromJson(json);
      case 'voice_error':
        return VoiceErrorEvent.fromJson(json);
      default:
        // Return a generic event or throw an error if the type is unknown
        return UnknownEvent(eventType: eventType ?? 'unknown');
    }
  }
}

class UnknownEvent extends MessageEvent {
  UnknownEvent({required super.eventType});
}

class MessageServiceStatusEvent extends MessageEvent {
  final String status;
  final String? statusText;

  MessageServiceStatusEvent({required this.status, this.statusText}) : super(eventType: 'service_status');

  factory MessageServiceStatusEvent.fromJson(Map<String, dynamic> json) {
    return MessageServiceStatusEvent(
      status: json['status'],
      statusText: json['status_text'],
    );
  }
}

class ConversationProcessingStartedEvent extends MessageEvent {
  final ServerConversation memory;

  ConversationProcessingStartedEvent({required this.memory}) : super(eventType: 'memory_processing_started');

  factory ConversationProcessingStartedEvent.fromJson(Map<String, dynamic> json) {
    return ConversationProcessingStartedEvent(
      memory: ServerConversation.fromJson(json['memory']),
    );
  }
}

class ConversationEvent extends MessageEvent {
  final ServerConversation memory;
  final List messages;

  ConversationEvent({required this.memory, required this.messages}) : super(eventType: 'memory_created');

  factory ConversationEvent.fromJson(Map<String, dynamic> json) {
    return ConversationEvent(
      memory: ServerConversation.fromJson(json['memory']),
      messages: json['messages'] ?? [],
    );
  }
}

class LastConversationEvent extends MessageEvent {
  final String memoryId;

  LastConversationEvent({required this.memoryId}) : super(eventType: 'last_memory');

  factory LastConversationEvent.fromJson(Map<String, dynamic> json) {
    return LastConversationEvent(
      memoryId: json['memory_id'],
    );
  }
}

class TranslationEvent extends MessageEvent {
  final List<TranscriptSegment> segments;

  TranslationEvent({required this.segments}) : super(eventType: 'translating');

  factory TranslationEvent.fromJson(Map<String, dynamic> json) {
    return TranslationEvent(
      segments: (json['segments'] as List<dynamic>).map((s) => TranscriptSegment.fromJson(s)).toList(),
    );
  }
}

class PhotoProcessingEvent extends MessageEvent {
  final String tempId;
  final String photoId;

  PhotoProcessingEvent({required this.tempId, required this.photoId}) : super(eventType: 'photo_processing');

  factory PhotoProcessingEvent.fromJson(Map<String, dynamic> json) {
    return PhotoProcessingEvent(
      tempId: json['temp_id'],
      photoId: json['photo_id'],
    );
  }
}

class PhotoDescribedEvent extends MessageEvent {
  final String photoId;
  final String description;
  final bool discarded;

  PhotoDescribedEvent({
    required this.photoId,
    required this.description,
    this.discarded = false,
  }) : super(eventType: 'photo_described');

  factory PhotoDescribedEvent.fromJson(Map<String, dynamic> json) {
    return PhotoDescribedEvent(
      photoId: json['photo_id'],
      description: json['description'],
      discarded: json['discarded'] ?? false,
    );
  }
}

class SpeakerLabelSuggestionEvent extends MessageEvent {
  final int speakerId;
  final String personId;
  final String personName;
  final String segmentId;

  SpeakerLabelSuggestionEvent({
    required this.speakerId,
    required this.personId,
    required this.personName,
    required this.segmentId,
  }) : super(eventType: 'speaker_label_suggestion');

  factory SpeakerLabelSuggestionEvent.fromJson(Map<String, dynamic> json) {
    return SpeakerLabelSuggestionEvent(
      speakerId: json['speaker_id'],
      personId: json['person_id'],
      personName: json['person_name'],
      segmentId: json['segment_id'],
    );
  }

  static SpeakerLabelSuggestionEvent empty() {
    return SpeakerLabelSuggestionEvent(
      speakerId: -1,
      personId: '',
      personName: '',
      segmentId: '',
    );
  }
}

// ============================================
// Voice Mode Events
// ============================================

/// Voice mode session activated by backend
class VoiceModeActiveEvent extends MessageEvent {
  final String sessionId;
  final int timeoutSeconds;

  VoiceModeActiveEvent({
    required this.sessionId,
    required this.timeoutSeconds,
  }) : super(eventType: 'voice_mode_active');

  factory VoiceModeActiveEvent.fromJson(Map<String, dynamic> json) {
    return VoiceModeActiveEvent(
      sessionId: json['session_id'] ?? '',
      timeoutSeconds: json['timeout_seconds'] ?? 120,
    );
  }
}

/// Transcription update during voice mode
class VoiceTranscriptionEvent extends MessageEvent {
  final String text;
  final bool isFinal;

  VoiceTranscriptionEvent({
    required this.text,
    required this.isFinal,
  }) : super(eventType: 'voice_transcription');

  factory VoiceTranscriptionEvent.fromJson(Map<String, dynamic> json) {
    return VoiceTranscriptionEvent(
      text: json['text'] ?? '',
      isFinal: json['is_final'] ?? false,
    );
  }
}

/// Voice mode status update (listening, thinking, speaking)
class VoiceStatusEvent extends MessageEvent {
  final String status;

  VoiceStatusEvent({required this.status}) : super(eventType: 'voice_status');

  factory VoiceStatusEvent.fromJson(Map<String, dynamic> json) {
    return VoiceStatusEvent(
      status: json['status'] ?? '',
    );
  }
}

/// Streaming audio response chunk from backend
class VoiceResponseAudioEvent extends MessageEvent {
  final String data;  // Base64-encoded audio
  final int sequence;
  final String format;
  final int sampleRate;

  VoiceResponseAudioEvent({
    required this.data,
    required this.sequence,
    required this.format,
    required this.sampleRate,
  }) : super(eventType: 'voice_response_audio');

  factory VoiceResponseAudioEvent.fromJson(Map<String, dynamic> json) {
    return VoiceResponseAudioEvent(
      data: json['data'] ?? '',
      sequence: json['sequence'] ?? 0,
      format: json['format'] ?? 'pcm16',
      sampleRate: json['sample_rate'] ?? 24000,
    );
  }
}

/// Voice response complete (full text available)
class VoiceResponseCompleteEvent extends MessageEvent {
  final String text;
  final int durationMs;

  VoiceResponseCompleteEvent({
    required this.text,
    required this.durationMs,
  }) : super(eventType: 'voice_response_complete');

  factory VoiceResponseCompleteEvent.fromJson(Map<String, dynamic> json) {
    return VoiceResponseCompleteEvent(
      text: json['text'] ?? '',
      durationMs: json['duration_ms'] ?? 0,
    );
  }
}

/// Voice mode session ended
class VoiceModeEndedEvent extends MessageEvent {
  final String reason;
  final int sessionDurationSeconds;
  final int turnCount;

  VoiceModeEndedEvent({
    required this.reason,
    required this.sessionDurationSeconds,
    required this.turnCount,
  }) : super(eventType: 'voice_mode_ended');

  factory VoiceModeEndedEvent.fromJson(Map<String, dynamic> json) {
    // Convert to int since backend may send double
    final duration = json['session_duration_seconds'];
    final durationInt = duration is double ? duration.toInt() : (duration ?? 0);

    return VoiceModeEndedEvent(
      reason: json['reason'] ?? 'unknown',
      sessionDurationSeconds: durationInt,
      turnCount: json['turn_count'] ?? 0,
    );
  }
}

/// Voice mode error
class VoiceErrorEvent extends MessageEvent {
  final String code;
  final String message;

  VoiceErrorEvent({
    required this.code,
    required this.message,
  }) : super(eventType: 'voice_error');

  factory VoiceErrorEvent.fromJson(Map<String, dynamic> json) {
    return VoiceErrorEvent(
      code: json['code'] ?? 'unknown',
      message: json['message'] ?? 'Unknown error',
    );
  }
}
