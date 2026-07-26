import 'package:flutter_test/flutter_test.dart';

import 'package:omi/pages/onboarding/ella/ella_onboarding.dart';

void main() {
  group('Ella onboarding voice consent policy', () {
    test('Hermes provisioning never starts directly while entitlement verification owns the gate', () {
      expect(
        EllaOnboarding.shouldStartProvisioningDirectly(provisioningGateEnabled: true, entitlementGateEnabled: true),
        isFalse,
      );
      expect(
        EllaOnboarding.shouldStartProvisioningDirectly(provisioningGateEnabled: true, entitlementGateEnabled: false),
        isTrue,
      );
    });

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

    test('existing user with a stale contract waits for explicit voice use', () {
      expect(
        EllaOnboarding.shouldPresentVoiceConsent(
          hasCurrentConsent: false,
          hasPriorAccountConsent: true,
          deferredCurrentConsent: false,
        ),
        isFalse,
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
  });
}
