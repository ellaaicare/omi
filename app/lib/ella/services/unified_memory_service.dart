import 'dart:convert';

import 'package:uuid/uuid.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/logger.dart';

const _pendingWritesKey = 'ellaUnifiedMemoryPendingWrites';
const _writeEnabledKey = 'ellaUnifiedMemoryWriteEnabled';
const _mockAdapterKey = 'ellaUnifiedMemoryMockAdapter';
const _liveCanonicalBaseUrl = 'https://api.ella-ai-care.com/';

/// Staged adapter for the canonical event/timeline APIs tracked by ella-ai#789.
///
/// Writes are live by default now that ella-ai#793 is deployed. The
/// SharedPreferences flags remain as a kill switch / local mock mode.
class UnifiedMemoryService {
  UnifiedMemoryService._();

  static bool get isWriteEnabled => SharedPreferencesUtil().getBool(_writeEnabledKey, defaultValue: true);

  static bool get isMockAdapter => SharedPreferencesUtil().getBool(_mockAdapterKey);

  static String createVoiceSessionId() => 'ios_voice_${const Uuid().v4()}';

  static String createChatSessionId() => 'ios_chat_${const Uuid().v4()}';

  static List<CanonicalEllaEvent> buildTurnEvents({
    required String channel,
    required String sessionId,
    required String provider,
    required int turnIndex,
    required String userText,
    required String assistantText,
    DateTime? startedAt,
    DateTime? endedAt,
  }) {
    final now = DateTime.now().toUtc();
    final started = (startedAt ?? now).toUtc();
    final ended = (endedAt ?? now).toUtc();
    final trimmedUserText = userText.trim();
    final trimmedAssistantText = assistantText.trim();
    final events = <CanonicalEllaEvent>[
      if (trimmedUserText.isNotEmpty)
        CanonicalEllaEvent.turn(
          channel: channel,
          sessionId: sessionId,
          provider: provider,
          eventId: '$sessionId:$turnIndex:user',
          role: 'user',
          text: trimmedUserText,
          startedAt: started,
          endedAt: ended,
          scanPolicy: 'immediate',
          metadata: {'turn_index': turnIndex},
        ),
      if (trimmedAssistantText.isNotEmpty)
        CanonicalEllaEvent.turn(
          channel: channel,
          sessionId: sessionId,
          provider: provider,
          eventId: '$sessionId:$turnIndex:assistant',
          role: 'assistant',
          text: trimmedAssistantText,
          startedAt: started,
          endedAt: ended,
          scanPolicy: 'none',
          metadata: {'turn_index': turnIndex},
        ),
    ];
    return events;
  }

  static Future<void> writeChatTurn({
    required String sessionId,
    required String userText,
    required String assistantText,
    DateTime? startedAt,
    DateTime? endedAt,
  }) async {
    if (!isWriteEnabled) return;
    if (_uid.isEmpty) return;

    final events = buildTurnEvents(
      channel: 'ios_chat',
      sessionId: sessionId,
      provider: 'openclaw',
      turnIndex: 1,
      userText: userText,
      assistantText: assistantText,
      startedAt: startedAt,
      endedAt: endedAt,
    );

    if (events.isEmpty) return;
    await _writePayload({'events': events.map((event) => event.toJson()).toList()});
  }

  static Future<void> writeVoiceTurn({
    required String sessionId,
    required String provider,
    required int turnIndex,
    required String userText,
    required String assistantText,
    DateTime? startedAt,
    DateTime? endedAt,
  }) async {
    if (!isWriteEnabled) return;
    if (_uid.isEmpty) return;

    final events = buildTurnEvents(
      channel: 'ios_voice',
      sessionId: sessionId,
      provider: provider,
      turnIndex: turnIndex,
      userText: userText,
      assistantText: assistantText,
      startedAt: startedAt,
      endedAt: endedAt,
    );

    if (events.isEmpty) return;
    await _writePayload({'events': events.map((event) => event.toJson()).toList()});
  }

  static Future<void> completeVoiceSession({
    required String sessionId,
    required String provider,
    required int turnCount,
    DateTime? startedAt,
    DateTime? endedAt,
  }) async {
    if (!isWriteEnabled) return;

    final uid = _uid;
    if (uid.isEmpty) return;

    final payload = {
      'uid': uid,
      'canonical_identity': _canonicalIdentity,
      'session_id': sessionId,
      'channel': 'ios_voice',
      'provider': _normalizeProvider(provider),
      'started_at': (startedAt ?? endedAt ?? DateTime.now()).toUtc().toIso8601String(),
      'ended_at': (endedAt ?? DateTime.now()).toUtc().toIso8601String(),
      'privacy_scope': 'user_private',
      'scan_policy': 'completion',
      'source_ref': {'type': 'ios_voice_session', 'id': sessionId},
      'metadata': {
        'client': 'ios-app',
        'turn_count': turnCount,
        'voice_mode': _voiceMode(provider),
      },
    };

    await _writePayload(payload, path: 'v1/ella/sessions/$sessionId/complete');
  }

  static Future<List<ServerMessage>> fetchTimelineMessages({int limit = 50}) async {
    if (!isWriteEnabled || isMockAdapter) return [];

    final uid = _uid;
    if (uid.isEmpty) return [];

    try {
      final response = await makeApiCall(
        url: '${_canonicalBaseUrl}v1/ella/timeline?uid=$uid&limit=$limit&channels=ios_voice,ios_chat',
        headers: await _canonicalHeaders(),
        method: 'GET',
        body: '',
        timeout: const Duration(seconds: 10),
      );
      if (response == null || response.statusCode != 200) {
        Logger.debug('[UnifiedMemory] Timeline fetch failed: ${response?.statusCode}');
        return [];
      }

      final decoded = jsonDecode(response.body) as Map<String, dynamic>;
      final rawItems = (decoded['events'] ?? decoded['items'] ?? []) as List<dynamic>;
      final messages = <ServerMessage>[];
      for (final raw in rawItems) {
        if (raw is! Map<String, dynamic>) continue;
        final message = _messageFromTimelineEvent(raw);
        if (message != null) messages.add(message);
      }
      messages.sort((a, b) => a.createdAt.compareTo(b.createdAt));
      return messages;
    } catch (e) {
      Logger.debug('[UnifiedMemory] Timeline fetch error: $e');
      return [];
    }
  }

  static Future<void> retryPendingWrites() async {
    if (!isWriteEnabled || isMockAdapter) return;

    final prefs = SharedPreferencesUtil();
    final pending = prefs.getStringList(_pendingWritesKey);
    if (pending.isEmpty) return;

    final remaining = <String>[];
    for (final encoded in pending) {
      try {
        final payload = jsonDecode(encoded) as Map<String, dynamic>;
        final path = payload.remove('_path') as String? ?? 'v1/ella/events';
        final ok = await _post(path: path, payload: payload);
        if (!ok) remaining.add(encoded);
      } catch (_) {
        remaining.add(encoded);
      }
    }
    await prefs.saveStringList(_pendingWritesKey, remaining);
  }

  static Future<void> _writePayload(Map<String, dynamic> payload, {String path = 'v1/ella/events'}) async {
    if (isMockAdapter) {
      await _queuePayload(path: path, payload: payload);
      Logger.debug('[UnifiedMemory] Mock adapter queued $path');
      return;
    }

    final ok = await _post(path: path, payload: payload);
    if (!ok) {
      await _queuePayload(path: path, payload: payload);
    }
  }

  static Future<bool> _post({required String path, required Map<String, dynamic> payload}) async {
    try {
      final response = await makeApiCall(
        url: _canonicalBaseUrl + path,
        headers: await _canonicalHeaders(),
        method: 'POST',
        body: jsonEncode(payload),
        timeout: const Duration(seconds: 10),
      );
      final status = response?.statusCode ?? 0;
      if (status >= 200 && status < 300) return true;
      Logger.debug('[UnifiedMemory] Write failed: $path status=$status');
      return false;
    } catch (e) {
      Logger.debug('[UnifiedMemory] Write error: $e');
      return false;
    }
  }

  static Future<void> _queuePayload({required String path, required Map<String, dynamic> payload}) async {
    final prefs = SharedPreferencesUtil();
    final pending = List<String>.from(prefs.getStringList(_pendingWritesKey));
    final queued = {...payload, '_path': path};
    pending.add(jsonEncode(queued));
    await prefs.saveStringList(_pendingWritesKey, pending);
  }

  static ServerMessage? _messageFromTimelineEvent(Map<String, dynamic> event) {
    final text = event['text'] as String? ?? '';
    if (text.trim().isEmpty) return null;

    final role = event['role'] as String? ?? '';
    if (role != 'user' && role != 'assistant') return null;

    final startedAt = event['started_at'] as String? ?? event['created_at'] as String?;
    final createdAt = DateTime.tryParse(startedAt ?? '')?.toLocal() ?? DateTime.now();
    final channel = event['channel'] as String? ?? '';

    return ServerMessage(
      event['event_id'] as String? ?? const Uuid().v4(),
      createdAt,
      text,
      role == 'user' ? MessageSender.human : MessageSender.ai,
      MessageType.text,
      null,
      false,
      [],
      [],
      [],
      askForNps: false,
      fromVoice: channel == 'ios_voice',
    );
  }

  static String get _uid => SharedPreferencesUtil().uid;

  static String get _canonicalBaseUrl {
    final configured = Env.apiBaseUrl ?? '';
    if (configured.contains('api.ella-ai-care.com')) return configured;
    return _liveCanonicalBaseUrl;
  }

  static Future<Map<String, String>> _canonicalHeaders() async {
    final headers = {'Content-Type': 'application/json'};
    try {
      headers['Authorization'] = await getAuthHeader();
    } catch (e) {
      Logger.debug('[UnifiedMemory] Auth header unavailable: $e');
    }
    return headers;
  }

  static String get _canonicalIdentity {
    final ellaUserId = SharedPreferencesUtil().ellaUserId;
    if (ellaUserId.isNotEmpty) return ellaUserId;
    return _uid;
  }

  static String _normalizeProvider(String provider) => switch (provider) {
        'gemini-live' => 'gemini-native-live',
        'openai-realtime' => 'openai-native-realtime',
        _ => provider,
      };

  static String? _voiceMode(String provider) => switch (_normalizeProvider(provider)) {
        'openclaw-direct' => 'openclaw-direct-v1',
        'openai-native-realtime' => 'openai-native-realtime-v1',
        'gemini-native-live' => 'gemini-native-live-v1',
        _ => null,
      };
}

class CanonicalEllaEvent {
  CanonicalEllaEvent({
    required this.uid,
    required this.canonicalIdentity,
    required this.eventId,
    required this.sessionId,
    required this.channel,
    required this.provider,
    required this.role,
    required this.text,
    required this.startedAt,
    required this.endedAt,
    required this.privacyScope,
    required this.scanPolicy,
    required this.sourceRef,
    required this.metadata,
  });

  final String uid;
  final String canonicalIdentity;
  final String eventId;
  final String sessionId;
  final String channel;
  final String provider;
  final String role;
  final String text;
  final DateTime startedAt;
  final DateTime endedAt;
  final String privacyScope;
  final String scanPolicy;
  final Map<String, String> sourceRef;
  final Map<String, dynamic> metadata;

  factory CanonicalEllaEvent.turn({
    required String channel,
    required String sessionId,
    required String provider,
    required String eventId,
    required String role,
    required String text,
    required DateTime startedAt,
    required DateTime endedAt,
    required String scanPolicy,
    required Map<String, dynamic> metadata,
  }) {
    final uid = UnifiedMemoryService._uid;
    return CanonicalEllaEvent(
      uid: uid,
      canonicalIdentity: UnifiedMemoryService._canonicalIdentity,
      eventId: eventId,
      sessionId: sessionId,
      channel: channel,
      provider: UnifiedMemoryService._normalizeProvider(provider),
      role: role,
      text: text,
      startedAt: startedAt,
      endedAt: endedAt,
      privacyScope: 'user_private',
      scanPolicy: scanPolicy,
      sourceRef: {
        'type': channel == 'ios_voice' ? 'ios_voice_session' : 'ios_chat_turn',
        'id': sessionId,
        'source_identity': 'ios-app:$channel:$sessionId',
      },
      metadata: {
        'client': 'ios-app',
        'voice_mode': UnifiedMemoryService._voiceMode(provider),
        ...metadata,
      },
    );
  }

  Map<String, dynamic> toJson() => {
        'uid': uid,
        'canonical_identity': canonicalIdentity,
        'event_id': eventId,
        'session_id': sessionId,
        'channel': channel,
        'provider': provider,
        'role': role,
        'text': text,
        'started_at': startedAt.toUtc().toIso8601String(),
        'ended_at': endedAt.toUtc().toIso8601String(),
        'privacy_scope': privacyScope,
        'scan_policy': scanPolicy,
        'source_ref': sourceRef,
        'metadata': metadata,
      };
}
