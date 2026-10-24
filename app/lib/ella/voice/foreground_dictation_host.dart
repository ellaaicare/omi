import 'dart:async';
import 'package:omi/ella/voice/foreground_dictation_diagnostics.dart';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter/scheduler.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:speech_to_text/speech_to_text.dart';
import 'package:speech_to_text/ella_speech_ownership.dart';
import 'package:omi/ella/services/standard_speech_ownership.dart';
import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';
import 'package:omi/ella/voice/dictation_draft_snapshot.dart';
import 'package:omi/ella/voice/darwin_local_dictation_port.dart';
import 'package:omi/ella/voice/ephemeral_dictation_session.dart';

const foregroundDictationEnabled = bool.fromEnvironment('ELLA_FOREGROUND_DICTATION_ENABLED');

/// A synchronous barrier against every admitted output route. Implementations
/// must cover pending playback as well as physical playback; observation alone
/// cannot grant this lease. Registration is deliberately absent by default.
abstract interface class DictationOutputLease {
  bool get isCurrent;
  Future<void> prepareBeforePermissions();
  Future<void> releaseAfterInputStopped();
}

class DictationOutputBusy implements Exception {
  const DictationOutputBusy();
}

class DictationPermissionUnavailable implements Exception {
  const DictationPermissionUnavailable();
}

abstract interface class ForegroundDictationEngine {
  StandardSpeechReceiptPort get port;
  Future<bool> probeLocalCapability();
  Future<bool> requestPermission();
  Future<bool> initialize();
  Future<void> listenOnDevice();
}

class DarwinForegroundDictationEngine implements ForegroundDictationEngine {
  DarwinForegroundDictationEngine() {
    if (!foregroundDictationEnabled ||
        !EllaCaptureHost.upstreamCaptureDefine ||
        kIsWeb ||
        defaultTargetPlatform != TargetPlatform.iOS) {
      throw StateError('foreground_dictation_policy_disabled');
    }
    port = DarwinLocalDictationPort(stopSpeech: _speech.stop);
  }
  final SpeechToText _speech = SpeechToText();
  DictationDiagnosticAttempt? diagnostic;
  @override
  late final DarwinLocalDictationPort port;
  @override
  Future<bool> probeLocalCapability() async {
    final capability = await port.capability();
    diagnostic?.localAuthorization(
        speech: capability.speechAuthorization, microphone: capability.microphoneAuthorization);
    if (capability.speechAuthorization == 'denied' ||
        capability.speechAuthorization == 'restricted' ||
        capability.microphoneAuthorization == 'denied') throw const DictationPermissionUnavailable();
    return capability.supported;
  }

  @override
  Future<bool> requestPermission() async => (await Permission.microphone.request()).isGranted;
  @override
  Future<bool> initialize() => _speech.initialize();
  @override
  Future<void> listenOnDevice() => _speech.listen(
        listenFor: const Duration(seconds: 60),
        pauseFor: const Duration(seconds: 3),
        listenOptions: SpeechListenOptions(onDevice: true, partialResults: true, cancelOnError: true),
      );
}

/// The application composition must supply both reviewed local-recognition
/// capability admission and the shared output barrier. No production default
/// grants authority or bypasses an intentional recorder policy.
class ForegroundDictationRegistration {
  const ForegroundDictationRegistration({
    required this.isPolicyEnabled,
    required this.reserveOutput,
    required this.createEngine,
  });
  final bool Function() isPolicyEnabled;
  final DictationOutputLease? Function() reserveOutput;
  final ForegroundDictationEngine Function() createEngine;
  static ForegroundDictationRegistration? current;
}

enum ForegroundDictationPhase {
  ready,
  unavailable,
  busy,
  starting,
  listening,
  finishing,
  permissionDenied,
  failed,
  closed,
}

class ForegroundDictationHost extends ChangeNotifier {
  ForegroundDictationHost({
    required this.registration,
    required this.readEditor,
    required this.hasExactAuthority,
    required this.canStartFresh,
    required this.applyDraft,
    this.diagnostic,
    @visibleForTesting this.operationTimeout = const Duration(seconds: 12),
  }) {
    if (registration == null) phase = ForegroundDictationPhase.unavailable;
  }
  final ForegroundDictationRegistration? registration;
  final DictationEditorBinding Function() readEditor;
  final bool Function() hasExactAuthority, canStartFresh;
  final void Function(TextEditingValue) applyDraft;
  final Duration operationTimeout;
  final DictationDiagnosticAttempt? diagnostic;
  ForegroundDictationPhase phase = ForegroundDictationPhase.ready;
  EphemeralDictationSession? _session;
  DictationOutputLease? _output;
  bool _retired = false, _disposed = false, _startIssued = false;
  bool _publishScheduled = false;
  Future<void>? _closing;
  Future<void>? _outputReleaseWork;
  bool get hasHeldOutput => _output != null;
  bool get hasHeldInput => _session?.hasReservation == true;
  void _notifyObservers() {
    if (_disposed) return;
    try {
      notifyListeners();
    } catch (_) {
      /* Observers cannot strand cleanup. */
    }
  }

  void _publish(ForegroundDictationPhase value) {
    if (_disposed) return;
    phase = value;
    if (_publishScheduled) return;
    if (SchedulerBinding.instance.schedulerPhase == SchedulerPhase.persistentCallbacks) {
      _publishScheduled = true;
      SchedulerBinding.instance.addPostFrameCallback((_) {
        _publishScheduled = false;
        _notifyObservers();
      });
    } else {
      _notifyObservers();
    }
  }

  bool _authorized() {
    try {
      return !_retired && hasExactAuthority() && registration?.isPolicyEnabled() == true;
    } catch (_) {
      return false;
    }
  }

  bool _fresh() {
    try {
      final reserved = AudioCaptureOwnership.shared.isReserved;
      diagnostic?.captureAdmission(
        registryReserved: reserved,
        registryStates: AudioCaptureOwnership.shared.dictationAdmissionStates,
        observationStage: 'registry',
        registrySource: reserved ? AudioCaptureOwnership.shared.source : null,
      );
      if (reserved && _session?.hasReservation != true) return false;
      diagnostic?.captureAdmission(observationStage: 'fresh_callback');
      return canStartFresh();
    } catch (_) {
      diagnostic?.captureAdmission(observationFailed: true);
      return false;
    }
  }

  Future<void> start() async {
    if (_session != null || _retired || _startIssued) return;
    _startIssued = true;
    diagnostic?.stage(DictationDiagnosticStage.admission);
    final configuration = registration;
    if (configuration == null || !_authorized()) {
      diagnostic?.fail(
        configuration == null
            ? DictationDiagnosticFailure.registrationMissing
            : DictationDiagnosticFailure.authorityOrPolicy,
      );
      _publish(ForegroundDictationPhase.unavailable);
      return;
    }
    if (!_fresh()) {
      diagnostic?.fail(DictationDiagnosticFailure.captureBusy);
      _publish(ForegroundDictationPhase.busy);
      return;
    }
    diagnostic?.stage(DictationDiagnosticStage.editor);
    final editor = readEditor();
    final lease = editor.lease;
    if (lease == null || !lease.isCurrent) {
      diagnostic?.fail(DictationDiagnosticFailure.editorLease);
      _publish(ForegroundDictationPhase.unavailable);
      return;
    }
    diagnostic?.stage(DictationDiagnosticStage.outputReservation);
    final output = configuration.reserveOutput();
    if (output == null) {
      diagnostic?.fail(DictationDiagnosticFailure.outputBusy);
      _publish(ForegroundDictationPhase.busy);
      return;
    }
    _output = output;
    if (!_authorized() || !output.isCurrent || !_fresh()) {
      diagnostic?.fail(DictationDiagnosticFailure.startRejected);
      try {
        await _releaseOutputIfStopped();
      } catch (_) {
        _publish(ForegroundDictationPhase.failed);
        return;
      }
      _publish(ForegroundDictationPhase.busy);
      return;
    }
    _publish(ForegroundDictationPhase.starting);
    try {
      final engine = configuration.createEngine();
      if (engine is DarwinForegroundDictationEngine) engine.diagnostic = diagnostic;
      _session = EphemeralDictationSession(
        diagnostic: diagnostic,
        port: _ObservedDictationPort(engine.port, () {
          if (!_retired) _publish(ForegroundDictationPhase.finishing);
        }, diagnostic),
        draft: DictationDraftSnapshot(value: editor.value, lease: lease, epoch: editor.epoch),
        draftRevision: editor.revision,
        readEditor: readEditor,
        hasExactCaptureAuthority: () => _authorized() && output.isCurrent,
        canStartFresh: _fresh,
        requestPermission: () async {
          if (_session?.acceptsStartEffects != true || !_authorized() || !output.isCurrent || !_fresh()) return false;
          diagnostic?.stage(DictationDiagnosticStage.outputPreparation);
          try {
            await output.prepareBeforePermissions();
          } catch (error) {
            diagnostic?.error(error);
            rethrow;
          }
          if (!_authorized() || _session?.acceptsStartEffects != true || !output.isCurrent || !_fresh()) return false;
          bool supported;
          try {
            diagnostic?.stage(DictationDiagnosticStage.localCapability);
            supported = await engine.probeLocalCapability();
            diagnostic?.localSupported(supported);
            if (!supported) diagnostic?.fail(DictationDiagnosticFailure.unsupportedLocalRecognition);
          } on DictationPermissionUnavailable {
            diagnostic?.fail(DictationDiagnosticFailure.permissionDenied);
            _publish(ForegroundDictationPhase.permissionDenied);
            return false;
          } catch (error) {
            diagnostic?.error(error);
            rethrow;
          }
          if (!supported || !_authorized() || _session?.acceptsStartEffects != true || !output.isCurrent || !_fresh())
            return false;
          diagnostic?.stage(DictationDiagnosticStage.microphonePermission);
          final bool permitted;
          try {
            permitted = await engine.requestPermission();
          } catch (error) {
            diagnostic?.error(error);
            rethrow;
          }
          diagnostic?.microphonePermitted(permitted);
          if (!permitted) diagnostic?.fail(DictationDiagnosticFailure.permissionDenied);
          if (!permitted) _publish(ForegroundDictationPhase.permissionDenied);
          return permitted;
        },
        initializeRecognition: () async {
          diagnostic?.stage(DictationDiagnosticStage.speechInitialize);
          final bool initialized;
          try {
            initialized = await engine.initialize();
          } catch (error) {
            diagnostic?.error(error);
            rethrow;
          }
          diagnostic?.initialized(initialized);
          if (!initialized) diagnostic?.fail(DictationDiagnosticFailure.initializeRejected);
          return initialized;
        },
        listen: () async {
          diagnostic?.stage(DictationDiagnosticStage.listen);
          try {
            await engine.listenOnDevice();
          } catch (error) {
            diagnostic?.error(error);
            rethrow;
          }
        },
        applyDraft: applyDraft,
        operationTimeout: operationTimeout,
        onAutomaticFinish: () {
          unawaited(finish());
        },
      );
      diagnostic?.stage(DictationDiagnosticStage.sessionAdmission);
      final started = await _session!.start();
      diagnostic?.startCompleted(started);
      if (!started) diagnostic?.fail(DictationDiagnosticFailure.startRejected);
      if (started) diagnostic?.stage(DictationDiagnosticStage.listening);
      if (!_retired && phase != ForegroundDictationPhase.permissionDenied) {
        _publish(
          started
              ? ForegroundDictationPhase.listening
              : (_authorized() && !_fresh() ? ForegroundDictationPhase.busy : ForegroundDictationPhase.unavailable),
        );
      }
    } on DictationOutputBusy {
      diagnostic?.fail(DictationDiagnosticFailure.outputBusy);
      _retired = true;
      _publish(ForegroundDictationPhase.busy);
    } catch (error) {
      diagnostic?.error(error);
      _retired = true;
      _publish(ForegroundDictationPhase.failed);
    }
    try {
      await _releaseOutputIfStopped();
    } catch (error) {
      diagnostic?.error(error);
      _publish(ForegroundDictationPhase.failed);
    }
  }

  Future<void> finish() async {
    final session = _session;
    if (session == null || _retired) return;
    _publish(ForegroundDictationPhase.finishing);
    try {
      await session.finish(); // Only applyDraft publishes; never reapply returned text.
      _retired = true;
      await _releaseOutputIfStopped();
      _publish(ForegroundDictationPhase.closed);
    } catch (error) {
      diagnostic?.error(error);
      _publish(ForegroundDictationPhase.failed);
    }
  }

  Future<void> _releaseOutputIfStopped() {
    if (hasHeldInput || _output == null) return Future<void>.value();
    final pending = _outputReleaseWork;
    if (pending != null) return pending;
    final output = _output!;
    diagnostic?.stage(DictationDiagnosticStage.outputRelease);
    final result = Completer<void>();
    _outputReleaseWork = result.future;
    Future<void>.sync(output.releaseAfterInputStopped).then(
      (_) {
        if (identical(_output, output)) _output = null;
        diagnostic?.outputReleased();
        result.complete();
      },
      onError: (Object error, StackTrace stack) {
        _outputReleaseWork = null;
        diagnostic?.error(error);
        result.completeError(error, stack);
      },
    );
    return result.future;
  }

  Future<void> retire() {
    _retired = true;
    final existing = _closing;
    if (existing != null) return existing;
    final result = Completer<void>();
    _closing = result.future;
    _publish(ForegroundDictationPhase.finishing);
    Future<void>.sync(() async {
      diagnostic?.stage(DictationDiagnosticStage.retirement);
      await _session?.retire();
      await _releaseOutputIfStopped();
      diagnostic?.retirementCompleted();
      _publish(ForegroundDictationPhase.closed);
    }).then(
      result.complete,
      onError: (Object e, StackTrace s) {
        _closing = null;
        diagnostic?.error(e);
        _publish(ForegroundDictationPhase.failed);
        result.completeError(e, s);
      },
    );
    return result.future;
  }

  @override
  void dispose() {
    _disposed = true;
    unawaited(retire().catchError((Object _) {}));
    super.dispose();
  }
}

class _ObservedDictationPort implements StandardSpeechReceiptPort {
  _ObservedDictationPort(this.inner, this.onDrain, this.diagnostic);
  final DictationDiagnosticAttempt? diagnostic;
  final StandardSpeechReceiptPort inner;
  final void Function() onDrain;
  @override
  Future<EllaSpeechCaptureProof> probe() async {
    diagnostic?.stage(DictationDiagnosticStage.inputProbe);
    diagnostic?.inputProbeBegin();
    try {
      final proof = await inner.probe();
      diagnostic?.inputProof(
        generation: proof.generation,
        engineRunning: proof.engineRunning,
        tapMayBeInstalled: proof.tapMayBeInstalled,
        taskRetained: proof.taskRetained,
        drainSupported: proof.recognitionDrainSupported,
        stopAmbiguous: proof.stopAmbiguous,
        recognitionTerminal: proof.recognitionTerminal,
        recognitionTaskCreated: proof.recognitionTaskCreated,
      );
      return proof;
    } catch (error) {
      diagnostic?.error(error);
      rethrow;
    }
  }

  @override
  Future<void> stop() => inner.stop();
  @override
  Future<StandardSpeechDrainReceipt> stopAndDrain(int generation) {
    diagnostic?.stage(DictationDiagnosticStage.inputDrain);
    try {
      onDrain();
    } catch (_) {
      /* Presentation never owns cleanup. */
    }
    return inner.stopAndDrain(generation).then((receipt) => receipt, onError: (Object error, StackTrace stack) {
      diagnostic?.error(error);
      Error.throwWithStackTrace(error, stack);
    });
  }

  @override
  void setCallback(void Function(Map<Object?, Object?>)? callback) => inner.setCallback(callback);
}
