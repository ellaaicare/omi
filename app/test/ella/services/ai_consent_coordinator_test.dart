import 'dart:async';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/services/ai_consent_coordinator.dart';

void main() {
  group('AiConsentActionGate', () {
    test('prompts once, accepts, and starts the protected action', () async {
      final gate = AiConsentActionGate();
      var hasConsent = false;
      var promptCount = 0;
      var startCount = 0;

      final started = await gate.run(
        hasConsent: () => hasConsent,
        requestConsent: () async {
          promptCount += 1;
          hasConsent = true;
          return true;
        },
        action: () async => startCount += 1,
      );

      expect(started, isTrue);
      expect(promptCount, 1);
      expect(startCount, 1);
    });

    test('decline leaves the protected audio action stopped', () async {
      final gate = AiConsentActionGate();
      var startCount = 0;

      final started = await gate.run(
        hasConsent: () => false,
        requestConsent: () async => false,
        action: () async => startCount += 1,
      );

      expect(started, isFalse);
      expect(startCount, 0);
    });

    test('coalesces simultaneous consent requests', () async {
      final gate = AiConsentActionGate();
      final promptCompleter = Completer<bool>();
      var hasConsent = false;
      var promptCount = 0;

      Future<bool> requestConsent() {
        promptCount += 1;
        return promptCompleter.future;
      }

      final first = gate.ensure(hasConsent: () => hasConsent, requestConsent: requestConsent);
      final second = gate.ensure(hasConsent: () => hasConsent, requestConsent: requestConsent);
      expect(promptCount, 1);

      hasConsent = true;
      promptCompleter.complete(true);

      expect(await first, isTrue);
      expect(await second, isTrue);
    });
  });
}
