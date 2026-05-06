import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/unified_memory_service.dart';

void main() {
  group('UnifiedMemoryService', () {
    setUp(() async {
      SharedPreferences.setMockInitialValues({});
      await SharedPreferencesUtil.init();
      SharedPreferencesUtil().uid = 'uid-123';
      await SharedPreferencesUtil().saveBool('ellaUnifiedMemoryWriteEnabled', true);
      await SharedPreferencesUtil().saveBool('ellaUnifiedMemoryMockAdapter', true);
    });

    test('queues canonical voice events in mock adapter mode', () async {
      await UnifiedMemoryService.writeVoiceTurn(
        sessionId: 'ios_voice_test',
        provider: 'gemini-live',
        turnIndex: 1,
        userText: 'hello',
        assistantText: 'hi there',
        startedAt: DateTime.utc(2026, 4, 28, 1),
        endedAt: DateTime.utc(2026, 4, 28, 1, 0, 1),
      );

      final pending = SharedPreferencesUtil().getStringList('ellaUnifiedMemoryPendingWrites');
      expect(pending, hasLength(1));

      final payload = jsonDecode(pending.single) as Map<String, dynamic>;
      final events = payload['events'] as List<dynamic>;
      expect(payload['_path'], 'v1/ella/events');
      expect(events, hasLength(2));
      expect(events.first['event_id'], 'ios_voice_test:1:user');
      expect(events.first['channel'], 'ios_voice');
      expect(events.first['provider'], 'gemini-native-live');
      expect(events.first['role'], 'user');
      expect(events.first['scan_policy'], 'immediate');
      expect(events[1]['event_id'], 'ios_voice_test:1:assistant');
      expect(events[1]['scan_policy'], 'none');
      expect(events.first['source_ref']['source_identity'], 'ios-app:ios_voice:ios_voice_test');
    });

    test('builds stable ios_chat event ids and source refs', () {
      final events = UnifiedMemoryService.buildTurnEvents(
        channel: 'ios_chat',
        sessionId: 'ios_chat_test',
        provider: 'hermes',
        turnIndex: 1,
        userText: 'what did we discuss?',
        assistantText: 'we discussed timing.',
        startedAt: DateTime.utc(2026, 4, 28, 2),
        endedAt: DateTime.utc(2026, 4, 28, 2, 0, 3),
      );

      final userEvent = events.first.toJson();
      final assistantEvent = events[1].toJson();

      expect(userEvent['event_id'], 'ios_chat_test:1:user');
      expect(assistantEvent['event_id'], 'ios_chat_test:1:assistant');
      expect(userEvent['channel'], 'ios_chat');
      expect(userEvent['provider'], 'hermes');
      expect(userEvent['role'], 'user');
      expect(userEvent['scan_policy'], 'immediate');
      expect(assistantEvent['role'], 'assistant');
      expect(assistantEvent['scan_policy'], 'none');
      expect(userEvent['source_ref']['type'], 'ios_chat_turn');
      expect(userEvent['source_ref']['source_identity'], 'ios-app:ios_chat:ios_chat_test');
      expect(userEvent['privacy_scope'], 'user_private');
      expect(userEvent['text'], 'what did we discuss?');
    });

    test('uses one stable Hermes chat session with increasing turn ids', () async {
      final firstSessionId = UnifiedMemoryService.createChatSessionId();
      final secondSessionId = UnifiedMemoryService.createChatSessionId();

      expect(firstSessionId, secondSessionId);
      expect(firstSessionId, 'ios_chat_uid-123');

      await UnifiedMemoryService.writeChatTurn(
        sessionId: firstSessionId,
        userText: 'first',
        assistantText: 'reply one',
        startedAt: DateTime.utc(2026, 4, 28, 3),
        endedAt: DateTime.utc(2026, 4, 28, 3, 0, 1),
      );
      await UnifiedMemoryService.writeChatTurn(
        sessionId: firstSessionId,
        userText: 'second',
        assistantText: 'reply two',
        startedAt: DateTime.utc(2026, 4, 28, 3, 0, 2),
        endedAt: DateTime.utc(2026, 4, 28, 3, 0, 3),
      );

      final pending = SharedPreferencesUtil().getStringList('ellaUnifiedMemoryPendingWrites');
      expect(pending, hasLength(2));

      final firstPayload = jsonDecode(pending[0]) as Map<String, dynamic>;
      final secondPayload = jsonDecode(pending[1]) as Map<String, dynamic>;
      final firstEvents = firstPayload['events'] as List<dynamic>;
      final secondEvents = secondPayload['events'] as List<dynamic>;

      expect(firstEvents.first['provider'], 'hermes');
      expect(firstEvents.first['event_id'], '$firstSessionId:1:user');
      expect(firstEvents[1]['event_id'], '$firstSessionId:1:assistant');
      expect(secondEvents.first['provider'], 'hermes');
      expect(secondEvents.first['event_id'], '$firstSessionId:2:user');
      expect(secondEvents[1]['event_id'], '$firstSessionId:2:assistant');
    });

    test('does nothing when writes are disabled', () async {
      await SharedPreferencesUtil().saveBool('ellaUnifiedMemoryWriteEnabled', false);

      await UnifiedMemoryService.writeVoiceTurn(
        sessionId: 'ios_voice_test',
        provider: 'openai-native-realtime',
        turnIndex: 1,
        userText: 'hello',
        assistantText: 'hi',
      );

      expect(SharedPreferencesUtil().getStringList('ellaUnifiedMemoryPendingWrites'), isEmpty);
    });
  });
}
