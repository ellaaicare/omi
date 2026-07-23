import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/demo/demo_fixtures.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/pages/guardian_alert_history_page.dart';
import 'package:omi/pages/home/today_page.dart';

void main() {
  double contrastRatio(Color foreground, Color background) {
    final foregroundLuminance = foreground.computeLuminance();
    final backgroundLuminance = background.computeLuminance();
    final lighter = foregroundLuminance > backgroundLuminance ? foregroundLuminance : backgroundLuminance;
    final darker = foregroundLuminance > backgroundLuminance ? backgroundLuminance : foregroundLuminance;
    return (lighter + 0.05) / (darker + 0.05);
  }

  test('Ella surface and small-label tokens meet the contrast floor', () {
    expect(contrastRatio(EllaColors.card, EllaColors.paper), greaterThanOrEqualTo(3));
    expect(contrastRatio(EllaColors.inkSoft, EllaColors.card), greaterThanOrEqualTo(4.5));
    expect(EllaColors.teal, EllaColors.tealDeep);
  });

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

  test('read aloud is offered only for a loaded daily note', () {
    expect(canReadDailyNote(loading: true, text: 'A real note'), isFalse);
    expect(canReadDailyNote(loading: false, text: ''), isFalse);
    expect(canReadDailyNote(loading: false, text: '   '), isFalse);
    expect(canReadDailyNote(loading: false, text: 'A real note'), isTrue);
  });
}
