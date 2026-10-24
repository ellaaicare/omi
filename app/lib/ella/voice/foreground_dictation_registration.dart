import 'package:flutter/foundation.dart';
import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:omi/ella/services/native_dictation_output_admission.dart';
import 'package:omi/ella/voice/foreground_dictation_host.dart';

bool get foregroundDictationPolicyEnabled =>
    foregroundDictationEnabled &&
    EllaCaptureHost.upstreamCaptureDefine &&
    !kIsWeb &&
    defaultTargetPlatform == TargetPlatform.iOS;

/// Pure composition only. No SDK object, permission, native query, output
/// reservation or account authority is created until the actual Start command.
void installForegroundDictationRegistration() {
  if (!foregroundDictationPolicyEnabled) {
    ForegroundDictationRegistration.current = null;
    return;
  }
  ForegroundDictationRegistration.current = ForegroundDictationRegistration(
    isPolicyEnabled: () => foregroundDictationPolicyEnabled,
    reserveOutput: () => NativeDictationOutputAdmission().tryReserve(),
    createEngine: DarwinForegroundDictationEngine.new,
  );
}
