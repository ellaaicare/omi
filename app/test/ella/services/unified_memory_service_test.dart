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
      expect(events.first['scan_policy'], 'completion');
      expect(events[1]['event_id'], 'ios_voice_test:1:assistant');
      expect(events[1]['scan_policy'], 'none');
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
