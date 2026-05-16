import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/models/guardian_alert.dart';
import 'package:omi/ella/services/guardian_alert_history_api.dart';

void main() {
  group('GuardianAlertHistoryApi', () {
    test('parses backend alert records from suggested response shape newest-first', () {
      final records = GuardianAlertHistoryApi.parseBackendRecords({
        'alerts': [
          {
            'id': 'older',
            'alert_text': 'Older alert',
            'trigger_type': 'memory_support',
            'delivery_target': 'user',
            'playback_status': 'played',
            'created_at': '2026-05-15T20:00:00Z',
          },
          {
            'id': 'newer',
            'summary': 'Wake word response',
            'trigger': 'wake_word',
            'target': 'dry-run',
            'status': 'failed',
            'created_at': '2026-05-15T20:05:00Z',
            'trace_id': 'trace-123',
            'queue_item_id': 'guardian_123',
            'caregiver_escalation': true,
            'escalation_status': 'not_sent',
          },
        ],
      });

      expect(records.map((record) => record.id), ['newer', 'older']);
      expect(records.first.alertText, 'Wake word response');
      expect(records.first.triggerType, 'wake_word');
      expect(records.first.deliveryTarget, 'dry-run');
      expect(records.first.playbackStatus, 'failed');
      expect(records.first.traceId, 'trace-123');
      expect(records.first.queueItemId, 'guardian_123');
      expect(records.first.escalation, isTrue);
      expect(records.first.isTest, isTrue);
    });

    test('recognizes and normalizes local Guardian debug log fallback records', () {
      final log = {
        'timestamp': '2026-05-15T20:05:00.000Z',
        'level': 'EVENT',
        'type': 'guardian_playback_failed',
        'queue_item_id': 'guardian_456',
        'trigger_type': 'wake_word_user_support',
        'message': 'Guardian playback failed',
        'trace_id': 'trace-456',
      };

      expect(GuardianAlertRecord.isGuardianDebugLog(log), isTrue);

      final record = GuardianAlertRecord.fromDebugLog(log);

      expect(record.id, 'guardian_456');
      expect(record.alertText, 'Guardian playback failed');
      expect(record.triggerType, 'wake_word_user_support');
      expect(record.playbackStatus, 'failed');
      expect(record.fromLocalDebugLog, isTrue);
      expect(record.createdAt, DateTime.parse('2026-05-15T20:05:00.000Z'));
    });
  });
}
