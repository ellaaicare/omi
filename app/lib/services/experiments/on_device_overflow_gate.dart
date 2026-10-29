import 'package:flutter/foundation.dart';

import 'package:omi/env/env.dart';
import 'package:omi/env/environment_profile.dart';
import 'package:omi/utils/analytics/analytics_manager.dart';

/// Client presentation gate for on-device transcription once live hours run out (the at-cap
/// choice sheet, model setup, schedule, queue banner, provenance and settings). Off leaves every
/// existing surface exactly as it is.
///
/// Evaluated from the PostHog flag `mobile-on-device-overflow`, registered in
/// `config/feature-flags.yaml`. Fail-closed: no analytics identity or a failed read means off.
/// Lifecycle: rollout; owner: dazheng; review-by: 2026-11-13; default off.
abstract final class OnDeviceOverflowGate {
  static const enabledFlag = 'mobile-on-device-overflow';

  /// Debug-build override: `--dart-define=OMI_ON_DEVICE_OVERFLOW=true`.
  static const _debugForce = bool.fromEnvironment('OMI_ON_DEVICE_OVERFLOW');

  static bool get debugOverrideActive => kDebugMode && _debugForce;

  /// Ella Base: no PostHog identity, so the fail-closed read below is always off there, while the Base
  /// profile keeps raw audio on the phone by default (sendRawAudioToOmi: false). On-device transcription is then
  /// the only consumer of that audio, and with the gate shut it waits forever ("Transcriptions pending").
  /// Opening the gate shows the existing setup, settings and Transcribe Now surfaces only: the runner still
  /// does nothing until the user turns on-device transcription on (OnDeviceSettings.enabled, default off).
  static bool baseProfileActive({AppEnvironmentProfile? profile}) =>
      (profile ?? Env.profile) == AppEnvironmentProfile.ellaBase;

  static Future<bool> read({Future<bool> Function(String key)? readFlag, AppEnvironmentProfile? profile}) async {
    if (debugOverrideActive) return true;
    if (baseProfileActive(profile: profile)) return true;
    try {
      return await (readFlag ?? AnalyticsManager().isFeatureEnabled)(enabledFlag);
    } catch (_) {
      return false;
    }
  }
}
