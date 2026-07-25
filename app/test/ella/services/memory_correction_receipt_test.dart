import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/http/api/conversations.dart';

void main() {
  test('parses authoritative applied and undone receipt states', () {
    final applied = ConversationCorrectionReceipt.fromJson({
      'correction_id': 'correction-1',
      'conversation_id': 'conversation-1',
      'status': 'applied',
      'applied_at': '2026-07-24T12:00:00Z',
      'before': {'title': 'Before title', 'overview': 'Before overview'},
      'after': {'title': 'After title', 'overview': 'After overview'},
    });

    expect(applied.isApplied, isTrue);
    expect(applied.isPending, isFalse);
    expect(applied.before.title, 'Before title');
    expect(applied.after.overview, 'After overview');

    final undone = ConversationCorrectionReceipt.fromJson({
      'correction_id': 'correction-1',
      'conversation_id': 'conversation-1',
      'status': 'applied',
      'undone_at': '2026-07-24T12:05:00Z',
      'before': {},
      'after': {},
    });
    expect(undone.isUndone, isTrue);
    expect(undone.isApplied, isFalse);
  });
}
