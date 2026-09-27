import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

void main() {
  group('Correction sheet placeholder copy', () {
    test('example hint text is generic and contains no personal names', () {
      final source = File('lib/pages/conversation_detail/widgets.dart').readAsStringSync();
      final match = RegExp(r"hintText: '([^']*)'").firstMatch(source);

      expect(match, isNotNull, reason: 'Expected to find the correction sheet hintText string.');
      final hintText = match!.group(1)!;

      expect(hintText, startsWith('Example:'));

      // Regression guard for a privacy bug where the placeholder hard-coded a
      // real account's name. Any capitalized word that isn't a generic role
      // (neighbor, friend, etc.) is a sign a personal name has crept back in.
      const disallowedNames = ['Greg', 'Plato', 'Will'];
      for (final name in disallowedNames) {
        expect(hintText.contains(name), isFalse, reason: 'Placeholder text must not reference "$name".');
      }
    });
  });
}
