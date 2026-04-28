import 'dart:async';
import 'dart:convert';

import 'package:uuid/uuid.dart';

import 'package:omi/backend/http/api/messages.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/platform/platform_manager.dart';

/// Feature flag — production Ella chat uses Pattern C direct OpenClaw.
///
/// Any backend proxy fallback must also force OpenClaw; Level 0 is the legacy
/// OMI/Grok path and is not valid for Ella chat.
const bool _useDirectChat = true;

bool isEllaOperationalChatText(String text) {
  final normalized = text.trim();
  return normalized.startsWith('Processed and stored locally.') ||
      normalized.contains('OMI write-back:') ||
      normalized.contains('Local log: updated in `memory/omi/');
}

/// Build Ella-specific debug headers for routing observability (Issue #216).
/// Backend trace.py captures these to correlate client requests with server routing decisions.
Map<String, String> _ellaDebugHeaders({required String routeSource}) {
  final uid = SharedPreferencesUtil().uid;
  return {
    'X-Ella-Client': 'ios-app',
    'X-Ella-Client-Version': PlatformManager.instance.appVersion,
    'X-Ella-Route-Source': routeSource,
    'X-Ella-Debug-Level': '4',
    'X-Ella-Session-Key': 'ella:$uid',
    'X-TTS-Provider': SharedPreferencesUtil().ttsProvider,
  };
}

/// Cached resolve result with expiry.
class _ResolvedEndpoint {
  final String agentId;
  final String sessionKey;
  final String gatewayUrl;
  final String token;
  final String historyUrl;
  final DateTime expiresAt;

  _ResolvedEndpoint({
    required this.agentId,
    required this.sessionKey,
    required this.gatewayUrl,
    required this.token,
    this.historyUrl = '',
    required this.expiresAt,
  });

  bool get isExpired => DateTime.now().isAfter(expiresAt);

  Map<String, dynamic> toJson() => {
        'agentId': agentId,
        'sessionKey': sessionKey,
        'gatewayUrl': gatewayUrl,
        'token': token,
        'historyUrl': historyUrl,
        'expiresAt': expiresAt.toIso8601String(),
      };

  factory _ResolvedEndpoint.fromJson(Map<String, dynamic> json) => _ResolvedEndpoint(
        agentId: json['agentId'] ?? '',
        sessionKey: json['sessionKey'] ?? '',
        gatewayUrl: json['gatewayUrl'] ?? '',
        token: json['token'] ?? '',
        historyUrl: json['historyUrl'] ?? '',
        expiresAt: DateTime.tryParse(json['expiresAt'] ?? '') ?? DateTime(2000),
      );
}

/// In-memory cache (fast path).
_ResolvedEndpoint? _cachedEndpoint;

/// Resolve the user's OpenClaw endpoint. Returns null on any failure.
Future<_ResolvedEndpoint?> _resolveEndpoint() async {
  final uid = SharedPreferencesUtil().uid;
  if (uid.isEmpty) return null;

  // 1. Check in-memory cache
  if (_cachedEndpoint != null && !_cachedEndpoint!.isExpired) {
    return _cachedEndpoint;
  }

  // 2. Check SharedPreferences cache
  final cached = SharedPreferencesUtil().getString('ellaResolvedEndpoint');
  if (cached.isNotEmpty) {
    try {
      final endpoint = _ResolvedEndpoint.fromJson(jsonDecode(cached));
      if (!endpoint.isExpired) {
        _cachedEndpoint = endpoint;
        return endpoint;
      }
    } catch (_) {
      // Corrupted cache, proceed to network call
    }
  }

  // 3. Call resolve endpoint
  try {
    final response = await makeApiCall(
      url: '${Env.apiBaseUrl}v1/ella/resolve?uid=$uid',
      headers: _ellaDebugHeaders(routeSource: 'resolve'),
      method: 'GET',
      body: '',
      timeout: const Duration(seconds: 5),
    );

    if (response == null || response.statusCode != 200) {
      Logger.debug('[EllaChat] Resolve failed: ${response?.statusCode}');
      return null;
    }

    // Log trace ID for routing observability (Issue #216)
    final traceId = response.headers['x-ella-trace-id'];
    if (traceId != null) {
      Logger.debug('[EllaChat] Resolve trace: $traceId');
    }

    final data = jsonDecode(response.body) as Map<String, dynamic>;
    final routing = data['routing'] as Map<String, dynamic>? ?? data;
    final endpoint = _ResolvedEndpoint(
      agentId: routing['agentId'] ?? '',
      sessionKey: routing['sessionKey'] ?? '',
      gatewayUrl: routing['gatewayUrl'] ?? '',
      token: routing['token'] ?? '',
      historyUrl: routing['historyUrl'] ?? '',
      expiresAt: DateTime.now().add(const Duration(hours: 1)),
    );

    // Validate — must have gatewayUrl and token at minimum
    if (endpoint.gatewayUrl.isEmpty || endpoint.token.isEmpty) {
      Logger.debug('[EllaChat] Resolve returned incomplete data');
      return null;
    }

    // Cache it
    _cachedEndpoint = endpoint;
    SharedPreferencesUtil().saveString('ellaResolvedEndpoint', jsonEncode(endpoint.toJson()));

    return endpoint;
  } catch (e) {
    Logger.debug('[EllaChat] Resolve error: $e');
    return null;
  }
}

/// Invalidate the resolve cache (call on gateway auth failures).
void _invalidateResolveCache() {
  _cachedEndpoint = null;
  SharedPreferencesUtil().remove('ellaResolvedEndpoint');
}

/// Parse one line of OpenAI SSE into a ServerMessageChunk.
/// Returns null for non-content lines (comments, empty, malformed).
ServerMessageChunk? _parseOpenAiSseChunk(String line, String messageId) {
  if (line.startsWith(':')) return null;
  if (!line.startsWith('data: ')) return null;

  final payload = line.substring(6).trim();
  if (payload == '[DONE]') return null;

  try {
    final json = jsonDecode(payload) as Map<String, dynamic>;
    final choices = json['choices'] as List<dynamic>?;
    if (choices == null || choices.isEmpty) return null;

    final delta = choices[0]['delta'] as Map<String, dynamic>?;
    if (delta == null) return null;

    final content = delta['content'] as String?;
    if (content == null || content.isEmpty) return null;

    return ServerMessageChunk(messageId, content, MessageChunkType.data);
  } catch (e) {
    Logger.debug('[EllaChat] Failed to parse OpenAI SSE chunk: $e');
    return null;
  }
}

/// Fetch chat history from the VPS proxy endpoint.
/// Returns messages in chronological order (oldest first), or empty list on failure.
Future<List<ServerMessage>> fetchEllaChatHistory({int limit = 50}) async {
  final endpoint = await _resolveEndpoint();
  if (endpoint == null || endpoint.historyUrl.isEmpty) {
    Logger.debug('[EllaChat] Cannot fetch history: no resolved endpoint or historyUrl');
    return [];
  }

  try {
    final url = '${Env.apiBaseUrl}${endpoint.historyUrl.replaceFirst(RegExp(r'^/'), '')}?limit=$limit';
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
      if (content.startsWith('[SYSTEM:')) continue; // Filter scanner notifications from chat UI
      if (isEllaOperationalChatText(content)) continue;

      result.add(ServerMessage(
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
      ));
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

/// Main entry point: resolve then stream directly, or fall back to proxy.
///
/// Yields the same [ServerMessageChunk] types as [sendEllaMessageStream],
/// so callers (MessageProvider, EllaVoiceChatPage) need no logic changes.
Stream<ServerMessageChunk> sendEllaChatStream(String text) async* {
  // Feature flag — delegate to existing proxy path if disabled
  if (!_useDirectChat) {
    yield* sendEllaMessageStream(text, headers: _ellaDebugHeaders(routeSource: 'proxy-flag-off'));
    return;
  }

  // Attempt to resolve the user's agent endpoint
  final endpoint = await _resolveEndpoint();
  if (endpoint == null) {
    Logger.debug('[EllaChat] Resolve unavailable, using proxy fallback');
    yield* sendEllaMessageStream(text, headers: _ellaDebugHeaders(routeSource: 'proxy-resolve-fail'));
    return;
  }

  // Stream directly from OpenClaw gateway
  const messageId = '1000';
  final accumulatedText = StringBuffer();

  try {
    await for (var line in makeStreamingApiCall(
      url: '${endpoint.gatewayUrl}/v1/chat/completions',
      headers: {
        'Authorization': 'Bearer ${endpoint.token}',
        'x-openclaw-scopes': 'operator.write',
        if (endpoint.sessionKey.isNotEmpty) 'x-openclaw-session-key': endpoint.sessionKey,
        ..._ellaDebugHeaders(routeSource: 'pattern-c'),
      },
      body: jsonEncode({
        'model': 'openclaw:${endpoint.agentId}',
        'messages': [
          {'role': 'user', 'content': text},
        ],
        'stream': true,
      }),
    )) {
      if (line.trim().isEmpty || line.startsWith(':')) continue;

      // Check for stream terminator
      if (line.trim() == 'data: [DONE]') break;

      final chunk = _parseOpenAiSseChunk(line, messageId);
      if (chunk != null) {
        accumulatedText.write(chunk.text);
        yield chunk;
      }
    }
  } catch (e) {
    Logger.error('[EllaChat] Direct stream error: $e');

    // No text accumulated — invalidate cache and retry via proxy
    if (accumulatedText.isEmpty) {
      _invalidateResolveCache();
      yield* sendEllaMessageStream(text, headers: _ellaDebugHeaders(routeSource: 'proxy-gateway-error'));
      return;
    }
    // Partial text accumulated — fall through to synthesize done chunk below
  }

  // Synthesize the done chunk from accumulated text
  final fullText = accumulatedText.toString();
  if (fullText.isNotEmpty) {
    final serverMessage = ServerMessage(
      const Uuid().v4(),
      DateTime.now(),
      fullText,
      MessageSender.ai,
      MessageType.text,
      null,
      false,
      [],
      [],
      [],
      askForNps: false,
    );
    yield ServerMessageChunk(messageId, jsonEncode(serverMessage.toJson()), MessageChunkType.done,
        message: serverMessage);
  } else {
    yield ServerMessageChunk.failedMessage();
  }
}
