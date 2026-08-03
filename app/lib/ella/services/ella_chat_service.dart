import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:uuid/uuid.dart';

import 'package:omi/backend/http/api/messages.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/demo/demo_fixtures.dart';
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
    'X-Ella-Session-Key': 'ella:$uid',
    'X-TTS-Provider': SharedPreferencesUtil().ttsProvider,
  };
}

typedef EllaApiCall = Future<http.Response?> Function({
  required String url,
  required Map<String, String> headers,
  required String body,
  required String method,
  Duration? timeout,
  int? retries,
});

typedef EllaDebugHeadersBuilder = Map<String, String> Function({required String routeSource});
typedef EllaApiBaseUrlProvider = String? Function();

String? _ellaApiBaseUrl() => Env.apiBaseUrl;

/// Fetch chat history from the authenticated first-party backend endpoint.
/// Returns messages in chronological order (oldest first), or empty list on failure.
Future<List<ServerMessage>> fetchEllaChatHistory({
  int limit = 50,
  EllaApiCall apiCall = makeApiCall,
  EllaDebugHeadersBuilder debugHeadersBuilder = _ellaDebugHeaders,
  EllaApiBaseUrlProvider apiBaseUrlProvider = _ellaApiBaseUrl,
}) async {
  if (SharedPreferencesUtil().demoMode) {
    return DemoFixtures.chatMessages();
  }

  try {
    final url = Uri.parse(
      '${apiBaseUrlProvider()}v1/ella/chat/history',
    ).replace(queryParameters: {'limit': '$limit'}).toString();
    final response = await apiCall(
      url: url,
      headers: debugHeadersBuilder(routeSource: 'chat-history'),
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
    for (final rawMessage in rawMessages) {
      if (rawMessage is! Map<String, dynamic>) continue;
      try {
        final message = ServerMessage.fromJson(rawMessage);
        if (message.text.isEmpty || message.text.startsWith('[SYSTEM:')) {
          continue; // Filter empty and scanner notification entries from chat UI.
        }
        message.askForNps = false;
        result.add(message);
      } catch (_) {
        // A malformed history entry must not discard valid siblings.
        continue;
      }
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
Stream<ServerMessageChunk> sendEllaChatStream(String text) async* {
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
  );
}
