import 'dart:io';

import 'package:omi/utils/platform/platform_manager.dart';

class AnalyticsManager {
  static final AnalyticsManager _instance = AnalyticsManager._internal();

  factory AnalyticsManager() {
    return _instance;
  }

  AnalyticsManager._internal();

  void setUserAttributes() {
    PlatformManager.instance.mixpanel.setPeopleValues();
    PlatformManager.instance.intercom.setUserAttributes();
  }

  void setUserAttribute(String key, dynamic value) {
    PlatformManager.instance.mixpanel.setUserProperty(key, value);
    PlatformManager.instance.intercom.updateCustomAttributes({key: value});
  }

  void trackEvent(String eventName, {Map<String, dynamic>? properties}) {
    PlatformManager.instance.mixpanel.track(eventName, properties: properties);
    PlatformManager.instance.intercom.logEvent(eventName, metaData: properties);
  }

  // --- Upstream capture compatibility surface (ellaaicare/ella-ai#1280) ---
  //
  // The vendored upstream capture stack (lib/upstream_capture/**, BasedHardware/omi@f16699a)
  // calls these AnalyticsManager members. Ella does not forward upstream's product analytics
  // (capture telemetry, upload counters, phone-call and voice-reply events) to third parties,
  // so every member is intentionally inert: no event leaves the device. Parameters whose
  // upstream types live in the vendored tree are typed `Object`/`Object?` so this shared file
  // never imports lib/upstream_capture/** (the flag-OFF graph must not reach it).

  /// Upstream: monotonic analytics identity epoch. Ella has no analytics identity rotation.
  static int get identityEpoch => 0;

  /// Upstream: build number stamped on analytics events.
  static String get appBuild => 'unknown';

  /// Upstream: platform label stamped on analytics events.
  static String get mobilePlatform => Platform.operatingSystem;

  /// Upstream: remote feature flags. Ella has no remote flag provider; fail closed.
  Future<bool> isFeatureEnabled(String key) async => false;

  void track(String eventName, {Map<String, dynamic>? properties}) {}

  void omiDoubleTap({required String feature}) {}

  void transcribeLaterToggled({required bool enabled}) {}

  void conversationCreated(Object conversation, {Object? recordingDevice}) {}

  void recordingUploadStarted({
    required String attemptId,
    required int fileCount,
    required int totalBytes,
    required bool claimsLiveCapture,
    String? recordingId,
  }) {}

  void recordingUploadCompleted({
    required String attemptId,
    required int fileCount,
    required int totalBytes,
    required bool claimsLiveCapture,
    required double durationSeconds,
    required String result,
    String? recordingId,
  }) {}

  void recordingUploadFailed({
    required String attemptId,
    required int fileCount,
    required int totalBytes,
    required bool claimsLiveCapture,
    required double durationSeconds,
    required String failureClass,
    String? recordingId,
  }) {}

  void phoneCallVerificationStarted() {}

  void phoneCallVerificationCompleted() {}

  void phoneCallStarted({String? contactName}) {}

  void phoneCallConnected() {}

  void phoneCallEnded({required int durationSeconds}) {}

  void phoneCallTranscriptSession({
    required bool wsAccepted,
    required int audioFramesSent,
    required int audioBytesSent,
    required int audioChannel1Frames,
    required int audioChannel2Frames,
    required int eventChannelErrors,
    required int eventChannelCoerced,
    required String transcriptionStatusFinal,
    required int durationSeconds,
    String? reason,
  }) {}

  void phoneCallFailed({String? error}) {}

  void voiceReplyPlayback({
    required Object outcome,
    required Object skipReason,
    required Object mode,
    required Object outputRoute,
    required int chunksRequested,
    required int chunksPlayed,
    required int chunksDropped,
    required Object fallbackReason,
    required int firstAudioLatencyMs,
    required Object interruptSource,
  }) {}
}
