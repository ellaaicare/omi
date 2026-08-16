import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:uuid/uuid.dart';

import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/http/api/messages.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/demo/demo_fixtures.dart';
import 'package:omi/ella/services/ella_service_result.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/platform/platform_manager.dart';

/// Build Ella-specific debug headers for routing observability (Issue #216).
/// Backend trace.py captures these to correlate client requests with server routing decisions.
Map<String, String> _ellaDebugHeaders({required String routeSource}) {
  return {
    'X-Ella-Client': 'ios-app',
    'X-Ella-Client-Version': PlatformManager.instance.appVersion,
    'X-Ella-Route-Source': routeSource,
  };
}

typedef EllaChatHistoryTransport = Future<http.Response?> Function({
  required String url,
  required String expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier exactAuthority,
});

typedef EllaVoiceTurnTransport = Future<http.Response?> Function({
  required String url,
  required String body,
  required String expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier exactAuthority,
});

const ellaChatInactivityTimeout = Duration(seconds: 75);

Stream<T> withEllaChatInactivityTimeout<T>(Stream<T> stream, {Duration timeout = ellaChatInactivityTimeout}) =>
    stream.timeout(
      timeout,
      onTimeout: (sink) {
        sink.addError(const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true));
        sink.close();
      },
    );

Future<http.Response?> _defaultHistoryTransport({
  required String url,
  required String expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier exactAuthority,
}) =>
    makeApiCall(
      url: url,
      headers: _ellaDebugHeaders(routeSource: 'chat-history'),
      method: 'GET',
      body: '',
      timeout: const Duration(seconds: 10),
      requireAuthCheck: true,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      exactAuthority: exactAuthority,
    );

Future<http.Response?> _defaultVoiceTurnTransport({
  required String url,
  required String body,
  required String expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier exactAuthority,
}) =>
    makeApiCall(
      url: url,
      headers: _ellaDebugHeaders(routeSource: 'v2v-canonical-writeback'),
      method: 'POST',
      body: body,
      timeout: const Duration(seconds: 10),
      retries: 0,
      requireAuthCheck: true,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      exactAuthority: exactAuthority,
    );

Future<EllaServiceResult<List<ServerMessage>>> persistEllaV2VTurn({
  required String uid,
  required String sessionId,
  required String turnId,
  required String userEventId,
  required String assistantEventId,
  required String userTranscript,
  required String assistantTranscript,
  required DateTime startedAt,
  required DateTime completedAt,
  required int? turnOrdinal,
  required ExactAccountAuthorityVerifier exactAuthority,
  EllaVoiceTurnTransport? transport,
}) async {
  if (uid.isEmpty ||
      sessionId.isEmpty ||
      turnId.isEmpty ||
      userEventId != '$turnId:user' ||
      assistantEventId != '$turnId:assistant' ||
      userTranscript.trim().isEmpty ||
      assistantTranscript.trim().isEmpty) {
    return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.invalidResponse));
  }
  if (!exactAuthority.isExactCurrent() || exactAuthority.uid != uid) {
    return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.accountChanged));
  }

  try {
    final response = await (transport ?? _defaultVoiceTurnTransport)(
      url: '${Env.apiBaseUrl}v1/ella/chat/voice-turns',
      body: jsonEncode({
        'uid': uid,
        'session_id': sessionId,
        'turn_id': turnId,
        'user_event_id': userEventId,
        'assistant_event_id': assistantEventId,
        'user_transcript': userTranscript.trim(),
        'assistant_transcript': assistantTranscript.trim(),
        'user_terminal': true,
        'assistant_terminal': true,
        'started_at': startedAt.toUtc().toIso8601String(),
        'completed_at': completedAt.toUtc().toIso8601String(),
        if (turnOrdinal != null) 'turn_ordinal': turnOrdinal,
      }),
      expectedAuthenticatedUid: uid,
      exactAuthority: exactAuthority,
    );
    if (response == null || response.statusCode != 200) {
      Logger.debug('[EllaChat] V2V canonical write failed: ${response?.statusCode}');
      return EllaServiceResult.failure(
        response == null
            ? const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true)
            : ClientApiFailure.fromHttp(statusCode: response.statusCode),
      );
    }
    if (!exactAuthority.isExactCurrent()) {
      return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.accountChanged));
    }

    final payload = jsonDecode(response.body);
    if (payload is! Map<String, dynamic> ||
        payload['ok'] != true ||
        payload['session_id'] != sessionId ||
        payload['turn_id'] != turnId ||
        payload['messages'] is! List) {
      return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.invalidResponse));
    }

    final messages = <ServerMessage>[];
    for (final raw in payload['messages'] as List<dynamic>) {
      if (raw is! Map<String, dynamic>) continue;
      final id = raw['id']?.toString() ?? '';
      final text = raw['text']?.toString() ?? '';
      final createdAt = DateTime.tryParse(raw['created_at']?.toString() ?? '');
      final sender = switch (raw['sender']?.toString()) {
        'human' => MessageSender.human,
        'ai' => MessageSender.ai,
        _ => null,
      };
      if (id.isEmpty || text.trim().isEmpty || createdAt == null || sender == null) continue;
      messages.add(
        ServerMessage(
          id,
          createdAt.toLocal(),
          text,
          sender,
          MessageType.text,
          null,
          false,
          [],
          [],
          [],
          askForNps: false,
          fromVoice: true,
          canonicalConversationId: (raw['metadata'] as Map?)?['conversation_id']?.toString() ?? sessionId,
          canonicalTurnId: (raw['metadata'] as Map?)?['turn_id']?.toString() ?? turnId,
          canonicalTurnOrdinal: (raw['metadata'] as Map?)?['turn_ordinal'] as int?,
          canonicalEventSequence: (raw['metadata'] as Map?)?['event_sequence'] as int?,
        ),
      );
    }
    final messagesById = {for (final message in messages) message.id: message};
    if (messages.length != 2 ||
        messagesById.keys.toSet().difference({userEventId, assistantEventId}).isNotEmpty ||
        messagesById.length != 2 ||
        messagesById[userEventId]?.sender != MessageSender.human ||
        messagesById[assistantEventId]?.sender != MessageSender.ai) {
      return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.invalidResponse));
    }
    messages.sort(compareServerMessagesChronologically);
    return EllaServiceResult.success(messages);
  } on ExactAccountAuthorityChangedException {
    return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.accountChanged));
  } catch (error) {
    Logger.debug('[EllaChat] V2V canonical response rejected: ${error.runtimeType}');
    return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.invalidResponse));
  }
}

/// Fetch chat history from the VPS proxy endpoint.
/// Returns messages in chronological order (oldest first). A failed read is
/// distinct from a verified empty history so callers can preserve their cache.
Future<EllaServiceResult<List<ServerMessage>>> fetchEllaChatHistory({
  int limit = 50,
  required String expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier exactAuthority,
  EllaChatHistoryTransport? transport,
}) async {
  if (SharedPreferencesUtil().demoMode) {
    return EllaServiceResult.success(DemoFixtures.chatMessages());
  }

  try {
    final query = <String, String>{'limit': '$limit'};
    final url = Uri.parse('${Env.apiBaseUrl}v1/ella/chat/history').replace(queryParameters: query).toString();
    final response = await (transport ?? _defaultHistoryTransport)(
      url: url,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      exactAuthority: exactAuthority,
    );

    if (response == null || response.statusCode != 200) {
      Logger.debug('[EllaChat] History fetch failed: ${response?.statusCode}');
      return EllaServiceResult.failure(
        response == null
            ? const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true)
            : ClientApiFailure.fromHttp(statusCode: response.statusCode, body: response.body),
      );
    }

    final data = jsonDecode(response.body) as Map<String, dynamic>;
    final rawMessages = data['messages'] as List<dynamic>? ?? [];

    final result = <ServerMessage>[];
    var recognizedHistoryShape = rawMessages.isEmpty;
    for (final m in rawMessages) {
      if (m is! Map<String, dynamic>) continue;
      final rawSender = (m['sender'] ?? m['role']) as String? ?? '';
      final content = (m['content'] ?? m['text']) as String? ?? '';
      final ts = (m['timestamp'] ?? m['created_at']) as String?;
      final id = m['id'] as String? ?? const Uuid().v4();
      final messageSender = switch (rawSender) {
        'human' || 'user' => MessageSender.human,
        'ai' || 'assistant' => MessageSender.ai,
        _ => null,
      };
      recognizedHistoryShape =
          recognizedHistoryShape || ((m.containsKey('content') || m.containsKey('text')) && messageSender != null);
      if (content.isEmpty) continue;
      if (messageSender == null) continue;
      if (content.startsWith('[SYSTEM:')) {
        continue; // Filter scanner notifications from chat UI
      }

      result.add(
        ServerMessage(
          id,
          ts != null ? DateTime.parse(ts).toLocal() : DateTime.now(),
          content,
          messageSender,
          MessageType.text,
          null,
          false,
          [],
          [],
          [],
          askForNps: false,
          canonicalConversationId: (m['metadata'] as Map?)?['conversation_id']?.toString(),
          canonicalTurnId: (m['metadata'] as Map?)?['turn_id']?.toString(),
          canonicalTurnOrdinal: (m['metadata'] as Map?)?['turn_ordinal'] as int?,
          canonicalEventSequence: (m['metadata'] as Map?)?['event_sequence'] as int?,
        ),
      );
    }

    if (!recognizedHistoryShape) {
      Logger.debug('[EllaChat] History response contained no supported message fields');
      return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.invalidResponse));
    }

    // API returns newest first; reverse for chronological UI order
    result.sort(compareServerMessagesChronologically);
    Logger.debug('[EllaChat] Fetched ${result.length} messages from history');
    return EllaServiceResult.success(result);
  } on ClientApiFailure catch (failure) {
    return EllaServiceResult.failure(failure);
  } on ExactAccountAuthorityChangedException {
    return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.accountChanged));
  } catch (e) {
    Logger.debug('[EllaChat] History fetch error: $e');
    return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.invalidResponse));
  }
}

/// Main entry point for Ella chat streaming.
///
/// Yields the same [ServerMessageChunk] types as [sendEllaMessageStream],
/// so callers (MessageProvider, EllaVoiceChatPage) need no logic changes.
Stream<ServerMessageChunk> sendEllaChatStream(
  String text, {
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
}) async* {
  if (!SharedPreferencesUtil().aiConsentAccepted) {
    Logger.debug('[EllaChat] Blocked chat stream without AI consent');
    throw const ClientApiFailure(ClientApiFailureKind.consentRequired);
  }
  if (exactAuthority != null && !exactAuthority.isExactCurrent()) {
    throw ExactAccountAuthorityChangedException('Exact account authority changed before Ella chat');
  }

  if (SharedPreferencesUtil().demoMode) {
    final message = DemoFixtures.chatMessages().last;
    yield ServerMessageChunk(message.id, message.text, MessageChunkType.done, message: message);
    return;
  }

  yield* withEllaChatInactivityTimeout(
    sendEllaMessageStream(
      text,
      headers: _ellaDebugHeaders(routeSource: 'proxy-canonical'),
      clientMessageId: const Uuid().v4(),
      clientSentAt: DateTime.now().toUtc(),
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      exactAuthority: exactAuthority,
    ),
  );
}
