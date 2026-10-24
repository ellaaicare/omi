import 'dart:async';
import 'package:flutter/services.dart';

enum DictationDiagnosticStage {
  admission,
  editor,
  outputReservation,
  sessionAdmission,
  inputProbe,
  outputPreparation,
  localCapability,
  microphonePermission,
  speechInitialize,
  listen,
  listening,
  inputDrain,
  outputRelease,
  retirement,
}

enum DictationDiagnosticFailure {
  registrationMissing,
  authorityOrPolicy,
  captureBusy,
  editorLease,
  outputBusy,
  startRejected,
  unsupportedLocalRecognition,
  permissionDenied,
  initializeRejected,
  missingPlugin,
  malformedProof,
  deadline,
  platformFailure,
  stateFailure,
  startDeadline,
  finishDeadline,
  otherFailure,
}

/// Memory-only, closed metadata. No UID, token, locale, draft, transcript, SDK
/// payload, exception message, or native request identifier is retained.
class ForegroundDictationDiagnostics {
  static final shared = ForegroundDictationDiagnostics();
  _DictationAttempt? _latest;
  int _sequence = 0;
  DictationDiagnosticAttempt begin(
      {required bool Function() contextCurrent, int? verificationRemainingMs, bool? authorityPresent}) {
    final attempt = _DictationAttempt(++_sequence, contextCurrent, verificationRemainingMs, authorityPresent);
    _latest = attempt;
    return DictationDiagnosticAttempt._(this, attempt);
  }

  bool _current(_DictationAttempt attempt) {
    try {
      return identical(_latest, attempt) && attempt.contextCurrent();
    } catch (_) {
      return false;
    }
  }

  Map<String, Object?> get snapshot {
    final latest = _latest;
    if (latest == null) return const {'version': 1, 'disposition': 'not_observed'};
    if (!_current(latest)) {
      _latest = null;
      return const {'version': 1, 'disposition': 'context_retired'};
    }
    return Map.unmodifiable({
      'version': 1,
      'attempt': latest.sequence,
      'disposition': 'observed',
      'authority_present_at_open': latest.authorityPresent,
      'verification_remaining_ms': latest.verificationRemainingMs?.clamp(0, 300000),
      'stage': latest.stage.name,
      'first_failure': latest.failure?.name,
      'failure_stage': latest.failureStage?.name,
      'failure_elapsed_ms': latest.failureElapsedMs,
      'elapsed_ms': latest.clock.elapsedMilliseconds.clamp(0, 3600000),
      'capture_admission': latest.captureAdmission,
      'input_generation': latest.inputGeneration,
      'input_probe_count': latest.inputProbeCount,
      'stop_ambiguous': latest.stopAmbiguous,
      'recognition_terminal': latest.recognitionTerminal,
      'recognition_task_created': latest.recognitionTaskCreated,
      'speech_authorization': latest.speechAuthorization,
      'microphone_authorization': latest.microphoneAuthorization,
      'engine_running': latest.engineRunning,
      'tap_may_be_installed': latest.tapMayBeInstalled,
      'task_retained': latest.taskRetained,
      'drain_supported': latest.drainSupported,
      'local_supported': latest.localSupported,
      'microphone_permitted': latest.microphonePermitted,
      'initialized': latest.initialized,
      'start_completed': latest.startCompleted,
      'output_released': latest.outputReleased,
      'retirement_completed': latest.retirementCompleted,
    });
  }
}

class _DictationAttempt {
  _DictationAttempt(this.sequence, this.contextCurrent, this.verificationRemainingMs, this.authorityPresent);
  final int sequence;
  final bool Function() contextCurrent;
  final int? verificationRemainingMs;
  final bool? authorityPresent;
  final Stopwatch clock = Stopwatch()..start();
  DictationDiagnosticStage stage = DictationDiagnosticStage.admission;
  DictationDiagnosticStage? failureStage;
  DictationDiagnosticFailure? failure;
  int? failureElapsedMs, inputGeneration;
  int inputProbeCount = 0;
  Map<String, Object?>? captureAdmission;
  bool? stopAmbiguous, recognitionTerminal, recognitionTaskCreated;
  String? speechAuthorization, microphoneAuthorization;
  bool? engineRunning,
      tapMayBeInstalled,
      taskRetained,
      drainSupported,
      localSupported,
      microphonePermitted,
      initialized,
      startCompleted,
      outputReleased,
      retirementCompleted;
}

class DictationDiagnosticAttempt {
  DictationDiagnosticAttempt._(this.owner, this.attempt);
  final ForegroundDictationDiagnostics owner;
  final _DictationAttempt attempt;
  void _observe(void Function() write) {
    try {
      if (owner._current(attempt)) write();
    } catch (_) {}
  }

  void captureAdmission({
    bool? registryReserved,
    String? registrySource,
    bool? companionActive,
    bool? captureQuiescent,
    bool? unfinalizedPhoneContent,
    bool? capturableContent,
    bool? recordingDevice,
    bool? voiceRecorderActive,
    bool? pageCurrent,
    bool? observationFailed,
    Map<String, bool>? pendingStates,
    Map<String, bool>? registryStates,
    String? observationStage,
  }) =>
      _observe(() {
        attempt.captureAdmission = Map.unmodifiable({
          ...?attempt.captureAdmission,
          if (pendingStates != null)
            'pending_states': Map.unmodifiable({
              for (final key in const [
                'recording_stopped',
                'device_service_ready',
                'phone_audio_owned',
                'phone_mic_start_pending',
                'phone_release_pending',
                'device_start_pending',
                'system_start_pending',
                'phone_stop_pending',
                'device_finalization_pending',
                'device_boundary_pending',
                'device_attempt_present',
                'device_session_present',
              ])
                if (pendingStates.containsKey(key)) key: pendingStates[key],
            }),
          if (registryStates != null)
            'registry_states': Map.unmodifiable({
              for (final key in const [
                'owner_present',
                'successor_present',
                'owner_invalidated',
                'owner_generation_matches',
                'owner_start_attempted',
                'owner_stop_pending',
                'owner_confirmation_outstanding',
              ])
                if (registryStates.containsKey(key)) key: registryStates[key],
            }),
          if (observationStage != null)
            'observation_stage': const {
              'registry',
              'fresh_callback',
              'page',
              'capture_lookup',
              'companion',
              'quiescent',
              'unfinalized',
              'content',
              'device',
              'voice_lookup',
              'pending_states',
              'complete',
            }.contains(observationStage)
                ? observationStage
                : 'other',
          if (registryReserved != null) 'registry_reserved': registryReserved,
          if (registryReserved == false) 'registry_source': null,
          if (registrySource != null && registryReserved != false)
            'registry_source': {
              'chat-dictation',
              'v2v-call',
              'phone-capture',
              'device-capture',
              'speech-profile',
              'standard_voice_call',
              'owned_phone_capture',
              'ephemeral-dictation',
            }.contains(registrySource)
                ? registrySource
                : 'other',
          if (companionActive != null) 'companion_active': companionActive,
          if (captureQuiescent != null) 'capture_quiescent': captureQuiescent,
          if (unfinalizedPhoneContent != null) 'unfinalized_phone_content': unfinalizedPhoneContent,
          if (capturableContent != null) 'capturable_content': capturableContent,
          if (recordingDevice != null) 'recording_device': recordingDevice,
          if (voiceRecorderActive != null) 'voice_recorder_active': voiceRecorderActive,
          if (pageCurrent != null) 'page_current': pageCurrent,
          if (observationFailed != null) 'observation_failed': observationFailed,
        });
      });

  void stage(DictationDiagnosticStage value) => _observe(() => attempt.stage = value);
  void fail(DictationDiagnosticFailure value) => _observe(() {
        if (attempt.failure != null) return;
        attempt.failure = value;
        attempt.failureStage = attempt.stage;
        attempt.failureElapsedMs = attempt.clock.elapsedMilliseconds.clamp(0, 3600000);
      });
  void error(Object error) => fail(switch (error) {
        MissingPluginException() => DictationDiagnosticFailure.missingPlugin,
        FormatException() => DictationDiagnosticFailure.malformedProof,
        TimeoutException() => DictationDiagnosticFailure.deadline,
        PlatformException() => DictationDiagnosticFailure.platformFailure,
        StateError(message: 'dictation_start_deadline') => DictationDiagnosticFailure.startDeadline,
        StateError(message: 'dictation_finish_deadline') => DictationDiagnosticFailure.finishDeadline,
        StateError() => DictationDiagnosticFailure.stateFailure,
        _ => DictationDiagnosticFailure.otherFailure,
      });
  void inputProof({
    required int generation,
    required bool engineRunning,
    required bool tapMayBeInstalled,
    required bool taskRetained,
    required bool drainSupported,
    required bool stopAmbiguous,
    required bool recognitionTerminal,
    required bool recognitionTaskCreated,
  }) =>
      _observe(() {
        attempt.stopAmbiguous = stopAmbiguous;
        attempt.recognitionTerminal = recognitionTerminal;
        attempt.recognitionTaskCreated = recognitionTaskCreated;
        attempt.inputGeneration = generation.clamp(0, 1000000000);
        attempt.engineRunning = engineRunning;
        attempt.tapMayBeInstalled = tapMayBeInstalled;
        attempt.taskRetained = taskRetained;
        attempt.drainSupported = drainSupported;
      });
  void inputProbeBegin() => _observe(() => attempt.inputProbeCount = (attempt.inputProbeCount + 1).clamp(0, 1000000));
  void localAuthorization({required String speech, required String microphone}) => _observe(() {
        attempt.speechAuthorization =
            {'authorized', 'not_determined', 'denied', 'restricted'}.contains(speech) ? speech : 'unknown';
        attempt.microphoneAuthorization =
            {'granted', 'undetermined', 'denied'}.contains(microphone) ? microphone : 'unknown';
      });
  void localSupported(bool value) => _observe(() => attempt.localSupported = value);
  void microphonePermitted(bool value) => _observe(() => attempt.microphonePermitted = value);
  void initialized(bool value) => _observe(() => attempt.initialized = value);
  void startCompleted(bool value) => _observe(() => attempt.startCompleted = value);
  void outputReleased() => _observe(() => attempt.outputReleased = true);
  void retirementCompleted() => _observe(() => attempt.retirementCompleted = true);
}
