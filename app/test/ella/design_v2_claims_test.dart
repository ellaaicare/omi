import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/demo/demo_fixtures.dart';
import 'package:omi/ella/pages/guardian_alert_history_page.dart';
import 'package:omi/pages/home/today_page.dart';

void main() {
  test('Whispers off copy says listening and remembering continue', () {
    final copy = '${whisperStatusLead(false)}${whisperStatusDetail(false)}'.toLowerCase();

    expect(copy, contains('whispers are off'));
    expect(copy, contains("still listening and remembering"));
    expect(copy, isNot(contains('ears are resting')));
    expect(copy, isNot(contains('listening is off')));
    expect(copy, isNot(contains('stopped listening')));
  });

  test('Whisper explanations use person-language', () {
    const systemWords = ['detected', 'triggered', 'classified', 'transcript'];

    for (final record in DemoFixtures.whispers(now: DateTime(2026, 7, 20, 12))) {
      final why = whisperWhyText(record.triggerType).toLowerCase();
      for (final word in systemWords) {
        expect(why, isNot(contains(word)), reason: 'why line contained "$word": $why');
      }
    }
  });

  test('memories remain in a loading state until the first fetch completes', () {
    expect(shouldShowMemoriesLoading(hasLoaded: false, isLoading: false, hasMemories: false), isTrue);
    expect(shouldShowMemoriesLoading(hasLoaded: false, isLoading: true, hasMemories: false), isTrue);
    expect(shouldShowMemoriesLoading(hasLoaded: true, isLoading: true, hasMemories: false), isTrue);
    expect(shouldShowMemoriesLoading(hasLoaded: true, isLoading: false, hasMemories: false), isFalse);
    expect(shouldShowMemoriesLoading(hasLoaded: true, isLoading: true, hasMemories: true), isFalse);
  });
}
