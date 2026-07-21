import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/providers/conversation_provider.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  ServerConversation conversation(String id, {bool discarded = false}) {
    final startedAt = DateTime.parse('2026-07-08T19:00:00Z');
    return ServerConversation(
      id: id,
      createdAt: startedAt,
      startedAt: startedAt,
      finishedAt: startedAt.add(const Duration(minutes: 10)),
      structured: Structured('Memory $id', 'Overview'),
      discarded: discarded,
    );
  }

  test('Ella-visible conversations never leak discarded cache records', () {
    final provider = ConversationProvider();
    addTearDown(provider.dispose);
    provider.conversations = [
      conversation('kept'),
      conversation('discarded', discarded: true),
    ];

    expect(provider.visibleConversations.map((item) => item.id), ['kept']);

    provider.showDiscardedConversations = true;
    expect(provider.visibleConversations.map((item) => item.id), ['kept', 'discarded']);
  });

  test('legacy cache without an owning uid is rejected', () async {
    final legacy = conversation('legacy');
    SharedPreferences.setMockInitialValues({
      'uid': 'current-user',
      'cachedConversations': [jsonEncode(legacy.toJson())],
    });
    await SharedPreferencesUtil.init();

    expect(SharedPreferencesUtil().cachedConversations, isEmpty);
  });

  test('cache for the current uid remains available', () async {
    final cached = conversation('current');
    SharedPreferences.setMockInitialValues({
      'uid': 'current-user',
      'cachedConversationsUid': 'current-user',
      'cachedConversations': [jsonEncode(cached.toJson())],
    });
    await SharedPreferencesUtil.init();

    expect(SharedPreferencesUtil().cachedConversations.single.id, 'current');
  });
}
