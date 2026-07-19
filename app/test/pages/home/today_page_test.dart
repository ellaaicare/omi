import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/action_item.dart';
import 'package:omi/pages/home/today_page.dart';

void main() {
  test('selects only incomplete upcoming reminders due today', () {
    final now = DateTime(2026, 7, 19, 10);
    final items = [
      ActionItemWithMetadata(
        id: 'today',
        description: 'Call Greg',
        completed: false,
        dueAt: DateTime(2026, 7, 19, 11),
      ),
      ActionItemWithMetadata(
        id: 'completed',
        description: 'Already done',
        completed: true,
        dueAt: DateTime(2026, 7, 19, 12),
      ),
      ActionItemWithMetadata(
        id: 'tomorrow',
        description: 'Tomorrow',
        completed: false,
        dueAt: DateTime(2026, 7, 20, 9),
      ),
    ];

    expect(todayUpcomingReminders(items, now).map((item) => item.id), ['today']);
  });
}
