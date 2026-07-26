import 'package:flutter_test/flutter_test.dart';

import 'package:omi/pages/onboarding/ella/ella_onboarding.dart';

void main() {
  group('Ella onboarding voice consent policy', () {
    test('new user sees the current voice consent during onboarding', () {
      expect(
        EllaOnboarding.shouldPresentVoiceConsent(
          hasCurrentConsent: false,
          hasPriorAccountConsent: false,
          deferredCurrentConsent: false,
        ),
        isTrue,
      );
    });

    test('accepted current contract is never requested again', () {
      expect(
        EllaOnboarding.shouldPresentVoiceConsent(
          hasCurrentConsent: true,
          hasPriorAccountConsent: false,
          deferredCurrentConsent: false,
        ),
        isFalse,
      );
    });

    test('material processor change requests renewed consent during onboarding', () {
      expect(
        EllaOnboarding.shouldPresentVoiceConsent(
          hasCurrentConsent: false,
          hasPriorAccountConsent: true,
          deferredCurrentConsent: false,
        ),
        isTrue,
      );
    });

    test('Not now is remembered for the current onboarding flow', () {
      expect(
        EllaOnboarding.shouldPresentVoiceConsent(
          hasCurrentConsent: false,
          hasPriorAccountConsent: false,
          deferredCurrentConsent: true,
        ),
        isFalse,
      );
    });

    test('provisioning remains stopped until current consent exists', () {
      expect(EllaOnboarding.shouldStartProvisioning(hasCurrentConsent: false), isFalse);
      expect(EllaOnboarding.shouldStartProvisioning(hasCurrentConsent: true), isTrue);
    });
  });
}
