import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/env/env.dart';

class DebugMetadataItem {
  final String conversationId;
  final DateTime? lastModified;
  final String path;
  final int size;
  final Map<String, dynamic> metadata;

  const DebugMetadataItem({
    required this.conversationId,
    required this.lastModified,
    required this.path,
    required this.size,
    required this.metadata,
  });

  factory DebugMetadataItem.fromMap(Map<String, dynamic> map) {
    return DebugMetadataItem(
      conversationId: map['conversation_id'] as String? ?? '',
      lastModified: DateTime.tryParse(map['lastModified'] as String? ?? ''),
      path: map['path'] as String? ?? '',
      size: map['size'] as int? ?? 0,
      metadata: (map['metadata'] as Map?)?.cast<String, dynamic>() ?? {},
    );
  }
}

String get _uid => SharedPreferencesUtil().uid;

Future<List<DebugMetadataItem>> fetchRecentDebugMetadata({int limit = 50}) async {
  if (_uid.isEmpty || Env.apiBaseUrl == null) return [];
  try {
    final uid = Uri.encodeQueryComponent(_uid);
    final url = '${Env.apiBaseUrl}v1/ella/debug/conversations/metadata?uid=$uid&limit=$limit';
    final response = await makeApiCall(url: url, headers: {}, body: '', method: 'GET');
    if (response == null || response.statusCode != 200) return [];

    final decoded = jsonDecode(utf8.decode(response.bodyBytes)) as Map<String, dynamic>;
    final items = decoded['items'] as List? ?? [];
    return items
        .cast<Map>()
        .map((item) => DebugMetadataItem.fromMap(item.cast<String, dynamic>()))
        .where((item) => item.conversationId.isNotEmpty)
        .toList();
  } catch (e) {
    debugPrint('fetchRecentDebugMetadata error: $e');
    return [];
  }
}

Future<DebugMetadataItem?> fetchDebugMetadata(String conversationId) async {
  if (_uid.isEmpty || conversationId.isEmpty || Env.apiBaseUrl == null) return null;
  try {
    final uid = Uri.encodeQueryComponent(_uid);
    final encodedConversationId = Uri.encodeComponent(conversationId);
    final url = '${Env.apiBaseUrl}v1/ella/debug/conversations/$encodedConversationId/metadata?uid=$uid';
    final response = await makeApiCall(url: url, headers: {}, body: '', method: 'GET');
    if (response == null || response.statusCode != 200) return null;

    return DebugMetadataItem.fromMap(jsonDecode(utf8.decode(response.bodyBytes)) as Map<String, dynamic>);
  } catch (e) {
    debugPrint('fetchDebugMetadata error: $e');
    return null;
  }
}
