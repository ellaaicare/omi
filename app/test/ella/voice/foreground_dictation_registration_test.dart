import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/voice/foreground_dictation_host.dart';
import 'package:omi/ella/voice/foreground_dictation_registration.dart';
import 'package:omi/ella/capture_host/ella_capture_host.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final messenger = TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
  test('actual production registration is pure and respects both build gates and iOS', () {
    final calls = <MethodCall>[];
    const channels = [
      MethodChannel('plugin.csdcorp.com/speech_to_text'),
      MethodChannel('com.ellaaicare.ella/guardian_mode'),
      MethodChannel('flutter.baseflow.com/permissions/methods')
    ];
    for (final channel in channels)
      messenger.setMockMethodCallHandler(channel, (call) async {
        calls.add(call);
        return null;
      });
    for (final platform in [TargetPlatform.android, TargetPlatform.iOS, TargetPlatform.macOS]) {
      debugDefaultTargetPlatformOverride = platform;
      installForegroundDictationRegistration();
      final enabled =
          foregroundDictationEnabled && EllaCaptureHost.upstreamCaptureDefine && platform == TargetPlatform.iOS;
      expect(ForegroundDictationRegistration.current != null, enabled);
      expect(foregroundDictationPolicyEnabled, enabled);
      expect(calls, isEmpty);
    }
    ForegroundDictationRegistration.current = null;
    debugDefaultTargetPlatformOverride = null;
    for (final channel in channels) messenger.setMockMethodCallHandler(channel, null);
  });
}
