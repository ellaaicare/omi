class GuardianAlertRecord {
  const GuardianAlertRecord({
    required this.id,
    required this.alertText,
    required this.triggerType,
    required this.deliveryTarget,
    required this.playbackStatus,
    required this.createdAt,
    this.sourceConversationId,
    this.sourceTranscriptId,
    this.escalation = false,
    this.escalationStatus,
    this.traceId,
    this.queueItemId,
    this.ttsProvider,
    this.why,
    this.isTest = false,
    this.fromLocalDebugLog = false,
  });

  final String id;
  final String alertText;
  final String triggerType;
  final String deliveryTarget;
  final String playbackStatus;
  final DateTime? createdAt;
  final String? sourceConversationId;
  final String? sourceTranscriptId;
  final bool escalation;
  final String? escalationStatus;
  final String? traceId;
  final String? queueItemId;
  final String? ttsProvider;
  final String? why;
  final bool isTest;
  final bool fromLocalDebugLog;

  factory GuardianAlertRecord.fromJson(Map<String, dynamic> json) {
    final id = _firstString(json, const [
      'id',
      'alert_id',
      'queue_item_id',
      'guardian_queue_item_id',
      'trace_id',
      'event_id',
    ]);
    final alertText = _firstString(json, const [
      'alert_text',
      'summary',
      'text',
      'message',
      'tts_text',
      'user_facing_text',
      'spoken_text',
    ]);
    final triggerType = _firstString(json, const ['trigger_type', 'trigger', 'reason', 'guardian_mode', 'mode']);
    final deliveryTarget = _firstString(json, const ['delivery_target', 'target', 'audience', 'recipient_type']);
    final playbackStatus = _firstString(json, const ['playback_status', 'status', 'item_status', 'audio_status']);

    return GuardianAlertRecord(
      id: id.isNotEmpty ? id : 'guardian-alert-${DateTime.now().microsecondsSinceEpoch}',
      alertText: alertText.isNotEmpty ? alertText : 'Guardian alert',
      triggerType: triggerType.isNotEmpty ? triggerType : 'unknown',
      deliveryTarget: deliveryTarget.isNotEmpty ? deliveryTarget : 'user',
      playbackStatus: playbackStatus.isNotEmpty ? playbackStatus : 'unknown',
      createdAt: _firstDate(json, const ['created_at', 'createdAt', 'queued_at', 'started_at', 'timestamp', 'ts']),
      sourceConversationId: _nullableFirstString(json, const [
        'source_conversation_id',
        'conversation_id',
        'conversationId',
      ]),
      sourceTranscriptId: _nullableFirstString(json, const ['source_transcript_id', 'transcript_id', 'transcriptId']),
      escalation: json['escalation'] == true || json['caregiver_escalation'] == true || json['escalated'] == true,
      escalationStatus: _nullableFirstString(json, const ['escalation_status', 'caregiver_status']),
      traceId: _nullableFirstString(json, const ['trace_id', 'traceId']),
      queueItemId: _nullableFirstString(json, const ['queue_item_id', 'guardian_queue_item_id']),
      ttsProvider: _nullableFirstString(json, const ['tts_provider', 'provider']),
      why: _nullableFirstString(json, const ['why', 'trigger_explanation', 'triggerExplanation']),
      isTest:
          json['is_test'] == true ||
          json['dry_run'] == true ||
          _isTestTarget(_firstString(json, const ['delivery_target', 'target'])),
    );
  }

  factory GuardianAlertRecord.fromDebugLog(Map<String, dynamic> json) {
    final extra = json['extra'];
    final fields = <String, dynamic>{...json};
    if (extra is Map<String, dynamic>) fields.addAll(extra);

    final type = _firstString(fields, const ['type', 'event_type']);
    final message = _firstString(fields, const ['message']);
    final status = _statusFromDebugEvent(type, fields);
    final trigger = _firstString(fields, const ['trigger_type', 'trigger', 'guardian_mode', 'mode']);
    final target = _firstString(fields, const ['delivery_target', 'target', 'audience']);
    final text = _firstString(fields, const ['alert_text', 'summary', 'tts_text', 'text']);

    return GuardianAlertRecord(
      id: _firstString(fields, const [
        'id',
        'event_id',
        'queue_item_id',
        'trace_id',
      ]).ifEmpty('local-${_firstString(fields, const ['timestamp', 'ts'])}'),
      alertText: text.isNotEmpty ? text : message.ifEmpty(type.ifEmpty('Guardian debug event')),
      triggerType: trigger.isNotEmpty ? trigger : type.ifEmpty('guardian'),
      deliveryTarget: target.isNotEmpty ? target : 'local-debug',
      playbackStatus: status,
      createdAt: _firstDate(fields, const ['timestamp', 'ts', 'created_at']),
      sourceConversationId: _nullableFirstString(fields, const ['source_conversation_id', 'conversation_id']),
      sourceTranscriptId: _nullableFirstString(fields, const ['source_transcript_id', 'transcript_id']),
      escalation: fields['escalation'] == true || fields['caregiver_escalation'] == true || fields['escalated'] == true,
      escalationStatus: _nullableFirstString(fields, const ['escalation_status', 'caregiver_status']),
      traceId: _nullableFirstString(fields, const ['trace_id', 'traceId']),
      queueItemId: _nullableFirstString(fields, const ['queue_item_id', 'guardian_queue_item_id']),
      ttsProvider: _nullableFirstString(fields, const ['tts_provider', 'provider']),
      why: _nullableFirstString(fields, const ['why', 'trigger_explanation', 'triggerExplanation']),
      isTest: fields['is_test'] == true || fields['dry_run'] == true || _isTestTarget(target),
      fromLocalDebugLog: true,
    );
  }

  static bool isGuardianDebugLog(Map<String, dynamic> json) {
    final extra = json['extra'];
    final fields = <String, dynamic>{...json};
    if (extra is Map<String, dynamic>) fields.addAll(extra);
    final haystack = [
      fields['type'],
      fields['message'],
      fields['trigger_type'],
      fields['queue_item_id'],
      fields['trace_id'],
      fields['playback_source'],
    ].whereType<Object>().join(' ').toLowerCase();
    return haystack.contains('guardian') || haystack.contains('wake_word') || haystack.contains('playback');
  }
}

extension on String {
  String ifEmpty(String fallback) => isEmpty ? fallback : this;
}

String _firstString(Map<String, dynamic> json, List<String> keys) {
  for (final key in keys) {
    final value = json[key];
    if (value is String && value.trim().isNotEmpty) return value.trim();
    if (value is num || value is bool) return value.toString();
  }
  return '';
}

String? _nullableFirstString(Map<String, dynamic> json, List<String> keys) {
  final value = _firstString(json, keys);
  return value.isEmpty ? null : value;
}

DateTime? _firstDate(Map<String, dynamic> json, List<String> keys) {
  for (final key in keys) {
    final value = json[key];
    if (value is String && value.trim().isNotEmpty) {
      final parsed = DateTime.tryParse(value.trim());
      if (parsed != null) return parsed;
    }
    if (value is int) {
      final millis = value > 1000000000000 ? value : value * 1000;
      return DateTime.fromMillisecondsSinceEpoch(millis, isUtc: true);
    }
  }
  return null;
}

String _statusFromDebugEvent(String type, Map<String, dynamic> fields) {
  final explicit = _firstString(fields, const ['playback_status', 'status', 'item_status']);
  if (explicit.isNotEmpty) return explicit;
  final normalized = type.toLowerCase();
  if (normalized.contains('failed')) return 'failed';
  if (normalized.contains('completed') || normalized.contains('played')) return 'played';
  if (normalized.contains('start')) return 'playing';
  if (normalized.contains('queued')) return 'queued';
  return 'debug';
}

bool _isTestTarget(String value) {
  final normalized = value.toLowerCase();
  return normalized.contains('test') || normalized.contains('dry-run') || normalized.contains('dry_run');
}
