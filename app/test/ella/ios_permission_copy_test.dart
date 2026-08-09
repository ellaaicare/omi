import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

void main() {
  test('iOS permission prompts use truthful Ella product language', () {
    final plist = File('ios/Runner/Info.plist').readAsStringSync();

    for (final inheritedTerm in <String>[
      'your Omi',
      'audio explanations of Bugs',
      'Instabug',
      'Omi conversations',
      'pro-active feedback',
    ]) {
      expect(plist, isNot(contains(inheritedTerm)), reason: 'Inherited permission copy remains: $inheritedTerm');
    }

    for (final requiredPhrase in <String>[
      'optional necklace',
      'record a moment or start a live conversation',
      'only when you choose a photo',
      'only when you choose to save an action item',
      'only when you choose calendar features',
      'only when you choose to connect Health',
      'Daily Notes, memory processing, Guardian, and account updates you enable',
    ]) {
      expect(plist, contains(requiredPhrase), reason: 'Missing truthful permission purpose: $requiredPhrase');
    }
  });
}
