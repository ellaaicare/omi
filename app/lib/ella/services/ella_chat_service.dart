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
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/platform/platform_manager.dart';

/// Build Ella-specific debug headers for routing observability (Issue #216).
/// Backend trace.py captures these to correlate client requests with server routing decisions.
Map<String, String> _ellaDebugHeaders({required String routeSource, String? expectedUid}) {
  final uid = expectedUid ?? SharedPreferencesUtil().uid;
  return {
    'X-Ella-Client': 'ios-app',
    'X-Ella-Client-Version': PlatformManager.instance.appVersion,
    'X-Ella-Route-Source': routeSource,
    if (!isHermesProvisioningGateEnabled) 'X-Ella-Session-Key': 'ella:$uid',
    if (!isHermesProvisioningGateEnabled) 'X-TTS-Provider': SharedPreferencesUtil().ttsProvider,
  };
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
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
}) async* {
  if (!SharedPreferencesUtil().aiConsentAccepted) {
    Logger.debug('[EllaChat] Blocked chat stream without AI consent');
    return;
  }
  if (exactAuthority != null && !exactAuthority.isExactCurrent()) {
    throw ExactAccountAuthorityChangedException('Exact account authority changed before Ella chat');
  }

  if (SharedPreferencesUtil().demoMode) {
    final message = DemoFixtures.chatMessages().last;
    yield ServerMessageChunk(message.id, message.text, MessageChunkType.done, message: message);
    return;
  }

  yield* sendEllaMessageStream(
    text,
    headers: _ellaDebugHeaders(routeSource: 'proxy-canonical', expectedUid: expectedAuthenticatedUid),
    clientMessageId: const Uuid().v4(),
    clientSentAt: DateTime.now().toUtc(),
    expectedAuthenticatedUid: expectedAuthenticatedUid,
    exactAuthority: exactAuthority,
  );
}
