import 'package:flutter_test/flutter_test.dart';

import 'package:omi/env/environment_profile.dart';
import 'package:omi/services/experiments/on_device_overflow_gate.dart';
import 'package:omi/services/on_device/on_device_settings.dart';

// Ella Base v4.7: the on-device overflow gate opens for the Base profile without PostHog, and only there.
void main() {
  Future<bool> flagOff(String _) async => false;
  Future<bool> flagThrows(String _) async => throw StateError('no analytics identity');

  test('Base profile opens the gate without a PostHog flag', () async {
    expect(await OnDeviceOverflowGate.read(readFlag: flagOff, profile: AppEnvironmentProfile.ellaBase), isTrue);
    expect(await OnDeviceOverflowGate.read(readFlag: flagThrows, profile: AppEnvironmentProfile.ellaBase), isTrue);
  });

  test('other profiles keep the fail-closed PostHog gate', () async {
    for (final profile in AppEnvironmentProfile.values.where((p) => p != AppEnvironmentProfile.ellaBase)) {
      expect(await OnDeviceOverflowGate.read(readFlag: flagOff, profile: profile), isFalse, reason: profile.name);
      expect(await OnDeviceOverflowGate.read(readFlag: flagThrows, profile: profile), isFalse, reason: profile.name);
      expect(await OnDeviceOverflowGate.read(readFlag: (_) async => true, profile: profile), isTrue,
          reason: profile.name);
    }
  });

  test('opening the gate does not opt anyone in: on-device transcription stays off by default', () {
    // The runner returns before touching any saved audio unless the user enables it (on_device_runner.dart).
    expect(const OnDeviceSettings().enabled, isFalse);
    expect(const OnDeviceSettings().keepEverythingOnDevice, isFalse);
  });
}
