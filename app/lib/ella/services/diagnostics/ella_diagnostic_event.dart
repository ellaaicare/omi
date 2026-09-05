import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart';
import 'package:uuid/uuid.dart';

import 'package:omi/services/wals/wal_owner_authority.dart';

const ellaDiagnosticEventSchemaVersion = 'ella.diagnostic_event.v1';
const ellaDiagnosticSourceRevision = String.fromEnvironment(
  'ELLA_SOURCE_REVISION',
  defaultValue: 'unattributed_local_build',
);

enum EllaDiagnosticLayer {
  accountBinding('account_binding'),
  bleTransport('ble_transport'),
  physicalAudio('physical_audio'),
  serverCapture('server_capture'),
  publication('publication'),
  presentation('presentation');

  const EllaDiagnosticLayer(this.wireName);
  final String wireName;
}

enum EllaDiagnosticOutcome {
  started('started'),
  succeeded('succeeded'),
  failed('failed'),
  cancelled('cancelled'),
  unknown('unknown');

  const EllaDiagnosticOutcome(this.wireName);
  final String wireName;
}

enum EllaDiagnosticRetryClass {
  never('never'),
  userAction('user_action'),
  boundedAutomatic('bounded_automatic'),
  operatorOnly('operator_only');

  const EllaDiagnosticRetryClass(this.wireName);
  final String wireName;
}

enum EllaDiagnosticFailureCode {
  rememberedDeviceNotResolved('remembered_device_not_resolved'),
  peripheralConnectTimeout('peripheral_connect_timeout'),
  notificationSubscriptionFailed('notification_subscription_failed'),
  audioFirstFrameTimeout('audio_first_frame_timeout'),
  audioFramesStalled('audio_frames_stalled'),
  websocketAuthFailed('websocket_auth_failed'),
  websocketUnavailable('websocket_unavailable'),
  captureAuthorityConflict('capture_authority_conflict'),
  captureReadyTimeout('capture_ready_timeout'),
  captureFirstFrameRejected('capture_first_frame_rejected'),
  captureDrainAmbiguous('capture_drain_ambiguous'),
  finalizationTimeout('finalization_timeout'),
  accountAuthorityChanged('account_authority_changed'),
  clientBackendRevisionMismatch('client_backend_revision_mismatch');

  const EllaDiagnosticFailureCode(this.wireName);
  final String wireName;
}

@immutable
class EllaDiagnosticEvent {
  const EllaDiagnosticEvent({
    required this.eventId,
    required this.diagnosticSessionId,
    required this.captureAttemptId,
    required this.accountBindingFingerprint,
    required this.authorityGeneration,
    required this.layer,
    required this.eventName,
    required this.outcome,
    required this.retryClass,
    required this.clientSequence,
    required this.clientMonotonicMs,
    required this.clientUtcTime,
    this.opaqueResourceId,
    this.firmware,
    this.codec,
    this.stableFailureCode,
    this.expectedNextEvent,
    this.deadlineMs,
    this.safeCounters = const <String, int>{},
    this.projectionRevision,
    this.actionRevision,
  });

  static final RegExp _idPattern = RegExp(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$');
  static final RegExp _fingerprintPattern = RegExp(r'^[a-f0-9]{64}$');
  static final RegExp _safeNamePattern = RegExp(r'^[a-z][a-z0-9_]{0,63}$');
  static const Set<String> _safeCounterNames = {'frames', 'bytes', 'retry_number', 'rssi_bucket', 'queue_age_seconds'};

  final String eventId;
  final String diagnosticSessionId;
  final String captureAttemptId;
  final String accountBindingFingerprint;
  final int authorityGeneration;
  final EllaDiagnosticLayer layer;
  final String eventName;
  final EllaDiagnosticOutcome outcome;
  final EllaDiagnosticRetryClass retryClass;
  final int clientSequence;
  final int clientMonotonicMs;
  final DateTime clientUtcTime;
  final String? opaqueResourceId;
  final String? firmware;
  final String? codec;
  final EllaDiagnosticFailureCode? stableFailureCode;
  final String? expectedNextEvent;
  final int? deadlineMs;
  final Map<String, int> safeCounters;
  final int? projectionRevision;
  final int? actionRevision;

  Map<String, Object?> toJson() {
    _validate();
    return <String, Object?>{
      'schema_version': ellaDiagnosticEventSchemaVersion,
      'event_id': eventId,
      'diagnostic_session_id': diagnosticSessionId,
      'capture_attempt_id': captureAttemptId,
      'account_binding_fingerprint': accountBindingFingerprint,
      'authority_generation': authorityGeneration,
      'source_revision': ellaDiagnosticSourceRevision,
      'layer': layer.wireName,
      'event_name': eventName,
      'outcome': outcome.wireName,
      'retry_class': retryClass.wireName,
      'client_sequence': clientSequence,
      'client_monotonic_ms': clientMonotonicMs,
      'client_utc_time': clientUtcTime.toUtc().toIso8601String(),
      if (opaqueResourceId != null) 'opaque_resource_id': opaqueResourceId,
      if (firmware != null) 'firmware': firmware,
      if (codec != null) 'codec': codec,
      if (stableFailureCode != null) 'stable_failure_code': stableFailureCode!.wireName,
      if (expectedNextEvent != null) 'expected_next_event': expectedNextEvent,
      if (deadlineMs != null) 'deadline_ms': deadlineMs,
      if (safeCounters.isNotEmpty) 'safe_counters': safeCounters,
      if (projectionRevision != null) 'projection_revision': projectionRevision,
      if (actionRevision != null) 'action_revision': actionRevision,
    };
  }

  void _validate() {
    if (!_idPattern.hasMatch(eventId) ||
        !_idPattern.hasMatch(diagnosticSessionId) ||
        !_idPattern.hasMatch(captureAttemptId)) {
      throw StateError('Diagnostic identifiers must be opaque bounded values');
    }
    if (!_fingerprintPattern.hasMatch(accountBindingFingerprint)) {
      throw StateError('Diagnostic account binding must be an opaque SHA-256 fingerprint');
    }
    if (authorityGeneration < 0 || clientSequence < 0 || clientMonotonicMs < 0) {
      throw StateError('Diagnostic generations and counters must be non-negative');
    }
    if (!_safeNamePattern.hasMatch(eventName) ||
        (expectedNextEvent != null && !_safeNamePattern.hasMatch(expectedNextEvent!))) {
      throw StateError('Diagnostic event names must use the stable wire format');
    }
    if (deadlineMs != null && deadlineMs! < 0) throw StateError('Diagnostic deadlines must be non-negative');
    for (final entry in safeCounters.entries) {
      if (!_safeCounterNames.contains(entry.key) || entry.value < 0) {
        throw StateError('Diagnostic counter is not allowlisted');
      }
    }
    for (final value in <String?>[opaqueResourceId, firmware, codec]) {
      if (value == null) continue;
      if (value.length > 128 || value.contains('://') || value.contains('?') || value.contains('@')) {
        throw StateError('Diagnostic value contains prohibited identifying data');
      }
    }
  }
}

class EllaDiagnosticCaptureTrace {
  EllaDiagnosticCaptureTrace._({
    required this.diagnosticSessionId,
    required this.captureAttemptId,
    required this.accountBindingFingerprint,
    required this.authorityGeneration,
    required this.opaqueDeviceBinding,
    required this.sink,
    required this.retryNumber,
    required _EllaDiagnosticTraceState state,
  }) : _state = state;

  factory EllaDiagnosticCaptureTrace.begin({
    required ActiveWalAuthority authority,
    required String deviceIdentifier,
    required EllaDiagnosticEventSink sink,
    String? diagnosticSessionId,
    Stopwatch? monotonicClock,
  }) {
    const uuid = Uuid();
    final ownerFingerprint = authority.owner.authorityFingerprint;
    final deviceDigest = Hmac(sha256, utf8.encode(ownerFingerprint)).convert(utf8.encode(deviceIdentifier));
    final clock = monotonicClock ?? (Stopwatch()..start());
    if (!clock.isRunning) clock.start();
    final state = _EllaDiagnosticTraceState(
      diagnosticSessionId: diagnosticSessionId ?? uuid.v4(),
      monotonicClock: clock,
      authorityIsCurrent: authority.isCurrent,
    );
    return EllaDiagnosticCaptureTrace._(
      diagnosticSessionId: state.diagnosticSessionId,
      captureAttemptId: uuid.v4(),
      accountBindingFingerprint: ownerFingerprint,
      authorityGeneration: authority.owner.authorityGenerationAtCapture,
      opaqueDeviceBinding: deviceDigest.toString(),
      sink: sink,
      retryNumber: 1,
      state: state,
    );
  }

  final String diagnosticSessionId;
  final String captureAttemptId;
  final String accountBindingFingerprint;
  final int authorityGeneration;
  final String opaqueDeviceBinding;
  final EllaDiagnosticEventSink sink;
  final int retryNumber;
  final _EllaDiagnosticTraceState _state;

  EllaDiagnosticCaptureTrace nextAttempt() => EllaDiagnosticCaptureTrace._(
        diagnosticSessionId: diagnosticSessionId,
        captureAttemptId: const Uuid().v4(),
        accountBindingFingerprint: accountBindingFingerprint,
        authorityGeneration: authorityGeneration,
        opaqueDeviceBinding: opaqueDeviceBinding,
        sink: sink,
        retryNumber: retryNumber + 1,
        state: _state,
      );

  Future<void> emit({
    required EllaDiagnosticLayer layer,
    required String eventName,
    required EllaDiagnosticOutcome outcome,
    required EllaDiagnosticRetryClass retryClass,
    EllaDiagnosticFailureCode? failureCode,
    String? expectedNextEvent,
    int? deadlineMs,
    String? firmware,
    String? codec,
    Map<String, int> safeCounters = const <String, int>{},
  }) async {
    if (!_state.authorityIsCurrent()) return;
    try {
      final event = EllaDiagnosticEvent(
        eventId: const Uuid().v4(),
        diagnosticSessionId: diagnosticSessionId,
        captureAttemptId: captureAttemptId,
        accountBindingFingerprint: accountBindingFingerprint,
        authorityGeneration: authorityGeneration,
        layer: layer,
        eventName: eventName,
        outcome: outcome,
        retryClass: retryClass,
        clientSequence: _state.sequence++,
        clientMonotonicMs: _state.monotonicClock.elapsedMilliseconds,
        clientUtcTime: DateTime.now().toUtc(),
        opaqueResourceId: opaqueDeviceBinding,
        firmware: firmware,
        codec: codec,
        stableFailureCode: failureCode,
        expectedNextEvent: expectedNextEvent,
        deadlineMs: deadlineMs,
        safeCounters: safeCounters,
      );
      await sink.append(event);
    } catch (error) {
      // Diagnostics must never interrupt capture, account transitions, or UI.
      debugPrint('Ella diagnostic event was dropped (${error.runtimeType})');
    }
  }
}

class _EllaDiagnosticTraceState {
  _EllaDiagnosticTraceState({
    required this.diagnosticSessionId,
    required this.monotonicClock,
    required this.authorityIsCurrent,
  });

  final String diagnosticSessionId;
  final Stopwatch monotonicClock;
  final bool Function() authorityIsCurrent;
  int sequence = 0;
}

abstract interface class EllaDiagnosticEventSink {
  Future<void> append(EllaDiagnosticEvent event);
}
