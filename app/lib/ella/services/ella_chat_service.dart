import 'dart:async';
import 'dart:convert';

import 'package:uuid/uuid.dart';

import 'package:omi/backend/http/api/messages.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/demo/demo_fixtures.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/platform/platform_manager.dart';

/// Build Ella-specific debug headers for routing observability (Issue #216).
/// Backend trace.py captures these to correlate client requests with server routing decisions.
Map<String, String> _ellaDebugHeaders({required String routeSource}) {
  final uid = SharedPreferencesUtil().uid;
  return {
    'X-Ella-Client': 'ios-app',
    'X-Ella-Client-Version': PlatformManager.instance.appVersion,
    'X-Ella-Route-Source': routeSource,
    if (!isHermesProvisioningGateEnabled) 'X-Ella-Session-Key': 'ella:$uid',
    if (!isHermesProvisioningGateEnabled) 'X-TTS-Provider': SharedPreferencesUtil().ttsProvider,
  };
}

class MemoryTalkHistoryTurn {
  final String role;
  final String text;
  final DateTime? createdAt;

  const MemoryTalkHistoryTurn({
    required this.role,
    required this.text,
    required this.createdAt,
  });
}

Future<List<MemoryTalkHistoryTurn>> fetchMemoryTalkHistory(String conversationId) async {
  if (SharedPreferencesUtil().demoMode) return [];
  try {
    final uri = Uri.parse('${Env.apiBaseUrl}v1/ella/chat/memory/$conversationId/history').replace(
      queryParameters: {
        'limit': '50',
      },
    );
    final response = await makeApiCall(
      url: uri.toString(),
      headers: _ellaDebugHeaders(routeSource: 'memory-talk-history'),
      method: 'GET',
      body: '',
      timeout: const Duration(seconds: 10),
    );
    if (response == null || response.statusCode != 200) return [];
    final payload = jsonDecode(response.body) as Map<String, dynamic>;
    return (payload['turns'] as List<dynamic>? ?? [])
        .whereType<Map>()
        .map((turn) {
          final value = Map<String, dynamic>.from(turn);
          return MemoryTalkHistoryTurn(
            role: value['role']?.toString() ?? '',
            text: value['text']?.toString() ?? '',
            createdAt:
                value['created_at'] != null ? DateTime.tryParse(value['created_at'].toString())?.toLocal() : null,
          );
        })
        .where((turn) => turn.text.trim().isNotEmpty)
        .toList();
  } catch (error) {
    Logger.debug('[MemoryTalk] History fetch error: $error');
    return [];
  }
}

Future<bool> appendMemoryTalkTurns(
  String conversationId,
  List<MemoryTalkHistoryTurn> turns,
) async {
  if (SharedPreferencesUtil().demoMode || turns.isEmpty) return true;
  try {
    final response = await makeApiCall(
      url: '${Env.apiBaseUrl}v1/ella/chat/memory/$conversationId/turns',
      headers: _ellaDebugHeaders(routeSource: 'memory-talk-append'),
      method: 'POST',
      body: jsonEncode({
        'turns': [
          for (final turn in turns)
            {
              'turn_id': const Uuid().v4(),
              'role': turn.role,
              'text': turn.text,
            },
        ],
      }),
      timeout: const Duration(seconds: 5),
      retries: 0,
    );
    return response?.statusCode == 200;
  } catch (error) {
    Logger.debug('[MemoryTalk] Turn persistence error: $error');
    return false;
  }
}

/// Fetch chat history from the VPS proxy endpoint.
/// Returns messages in chronological order (oldest first), or empty list on failure.
Future<List<ServerMessage>> fetchEllaChatHistory({int limit = 50}) async {
  if (SharedPreferencesUtil().demoMode) {
    return DemoFixtures.chatMessages();
  }

  try {
    final uid = SharedPreferencesUtil().uid;
    final query = <String, String>{
      'limit': '$limit',
      if (!isHermesProvisioningGateEnabled) 'uid': uid,
    };
    final url = Uri.parse('${Env.apiBaseUrl}v1/ella/chat/history').replace(queryParameters: query).toString();
    final response = await makeApiCall(
      url: url,
      headers: _ellaDebugHeaders(routeSource: 'chat-history'),
      method: 'GET',
      body: '',
      timeout: const Duration(seconds: 10),
    );

    if (response == null || response.statusCode != 200) {
      Logger.debug('[EllaChat] History fetch failed: ${response?.statusCode}');
      return [];
    }

    final data = jsonDecode(response.body) as Map<String, dynamic>;
    final rawMessages = data['messages'] as List<dynamic>? ?? [];

    final result = <ServerMessage>[];
    for (final m in rawMessages) {
      final role = m['role'] as String? ?? '';
      final content = m['content'] as String? ?? '';
      final ts = m['timestamp'] as String?;
      final id = m['id'] as String? ?? const Uuid().v4();
      if (content.isEmpty) continue;
      if (content.startsWith('[SYSTEM:')) {
        continue; // Filter scanner notifications from chat UI
      }

      result.add(
        ServerMessage(
          id,
          ts != null ? DateTime.parse(ts).toLocal() : DateTime.now(),
          content,
          role == 'user' ? MessageSender.human : MessageSender.ai,
          MessageType.text,
          null,
          false,
          [],
          [],
          [],
          askForNps: false,
        ),
      );
    }

    // API returns newest first; reverse for chronological UI order
    result.sort((a, b) => a.createdAt.compareTo(b.createdAt));
    Logger.debug('[EllaChat] Fetched ${result.length} messages from history');
    return result;
  } catch (e) {
    Logger.debug('[EllaChat] History fetch error: $e');
    return [];
  }
}

/// Main entry point for Ella chat streaming.
///
/// Yields the same [ServerMessageChunk] types as [sendEllaMessageStream],
/// so callers (MessageProvider, EllaVoiceChatPage) need no logic changes.
Stream<ServerMessageChunk> sendEllaChatStream(
  String text, {
  String conversationId = '',
}) async* {
  if (!SharedPreferencesUtil().aiConsentAccepted) {
    Logger.debug('[EllaChat] Blocked chat stream without AI consent');
    return;
  }

  if (SharedPreferencesUtil().demoMode) {
    final message = DemoFixtures.chatMessages().last;
    yield ServerMessageChunk(message.id, message.text, MessageChunkType.done, message: message);
    return;
  }

  yield* sendEllaMessageStream(
    text,
    headers: _ellaDebugHeaders(routeSource: 'proxy-canonical'),
    clientMessageId: const Uuid().v4(),
    clientSentAt: DateTime.now().toUtc(),
    conversationId: conversationId,
  );
}
