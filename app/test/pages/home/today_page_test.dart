import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/action_item.dart';
import 'package:omi/ella/models/today_card.dart';
import 'package:omi/pages/home/today_page.dart';

void main() {
  test('selects only incomplete upcoming reminders due today', () {
    final now = DateTime(2026, 7, 19, 10);
    final items = [
      ActionItemWithMetadata(id: 'today', description: 'Call Greg', completed: false, dueAt: DateTime(2026, 7, 19, 11)),
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

  test('action item source labels survive API parsing', () {
    final item = ActionItemWithMetadata.fromJson({
      'id': 'from-david',
      'description': 'Dinner with David',
      'completed': false,
      'source_label': 'David',
    });

    expect(item.sourceLabel, 'David');
    expect(item.toJson()['source_label'], 'David');
  });

  test('Talk about this launches with daily-card identifiers only', () {
    final card = TodayCard(
      id: 'today-card-42',
      version: 3,
      kind: TodayCardKind.memory,
      eyebrow: 'A MEMORY FROM JUNE 12',
      headline: 'The roses along Elm Street',
      body: 'You enjoyed the long walk home with Rose.',
      generatedAt: DateTime.utc(2026, 7, 31),
      sourceRefs: const [TodayCardSourceRef(kind: 'memory', id: 'memory-1', versionId: 'v4')],
    );

    expect(TodayPage.sessionScopeFor(card).toJson(), {
      'kind': 'daily_card',
      'card_id': 'today-card-42',
      'expected_version': 3,
    });
  });
}
