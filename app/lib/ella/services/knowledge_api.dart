import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:omi/backend/preferences.dart';
import 'package:omi/utils/logger.dart';

const String _dashboardBase = 'https://ella-ai-care.com';

class KnowledgeSection {
  final String title;
  final String content;
  const KnowledgeSection({required this.title, required this.content});
  factory KnowledgeSection.fromJson(Map<String, dynamic> j) =>
      KnowledgeSection(title: j['title'] as String, content: j['content'] as String);
}

String get _userId {
  final id = SharedPreferencesUtil().ellaUserId;
  return id.isNotEmpty ? id : SharedPreferencesUtil().uid;
}

Future<List<KnowledgeSection>> getUserKnowledge() async {
  final uid = _userId;
  if (uid.isEmpty) return [];
  try {
    final response = await http
        .get(
          Uri.parse('$_dashboardBase/api/users/$uid/knowledge'),
          headers: {'Content-Type': 'application/json'},
        )
        .timeout(const Duration(seconds: 10));
    if (response.statusCode == 200) {
      final data = jsonDecode(response.body) as Map<String, dynamic>;
      if (data['success'] == true) {
        final list = data['sections'] as List;
        return list.map((s) => KnowledgeSection.fromJson(s as Map<String, dynamic>)).toList();
      }
    }
  } catch (e) {
    Logger.debug('getUserKnowledge error: $e');
  }
  return [];
}
