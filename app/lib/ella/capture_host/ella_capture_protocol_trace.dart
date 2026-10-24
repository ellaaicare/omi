import 'dart:convert';
import 'package:crypto/crypto.dart';

enum CaptureTraceEvent {
  attemptBegin,
  superStartBegin,
  superStartEnd,
  listenerAttached,
  transportConnected,
  transportConnectFailed,
  readyReceived,
  readyValidated,
  readyWaitBegin,
  readyDeadline,
  localCloseRequest,
  remoteCloseCallback,
  closeCallback,
  transportErrorCallback,
  failure,
  bindingRetired,
  originRetired,
  socketReused,
  deliveryChanged,
  rebindBegin,
  rebindOldStopBegin,
  rebindOldStopEnd,
  rebindEnd,
  rebindFailure,
  pauseRequested,
  pauseCommitted,
  resumeRequested,
  resumeCommitted,
}

enum CaptureTraceFieldType { absent, nullValue, emptyString, string, integer, number, boolean, list, map, other }

enum CaptureTraceValidation { initial, idempotent, successor, invalidFields, unrelated, stopping, failed }

enum CaptureTraceFailure {
  originRetired,
  connectFailed,
  readyUnavailable,
  invalidReady,
  closedBeforeReady,
  closedAfterReady,
  socketError,
  other
}

enum CaptureTraceCloseOrigin { localRequest, remoteCallback, transportError, readyDeadline, cleanup }

enum CaptureTraceStopReason {
  replaced,
  settingsChanged,
  bindingRetired,
  authorityRetired,
  readyFailure,
  ordinaryStop,
  other
}

/// SHA256 of compact UTF-8 JSON [conversation_id,generation]. Never includes
/// UID, owner token, query, message text or exception. Raw inputs are not retained.
class CaptureTraceTupleFingerprint {
  CaptureTraceTupleFingerprint._(this.sha256Hex);
  final String sha256Hex;
  static CaptureTraceTupleFingerprint? fromReadyFields(Map<dynamic, dynamic> fields) {
    final conversation = fields['conversation_id'];
    final generation = fields['generation'];
    if (conversation is! String || conversation.isEmpty || generation is! String || generation.isEmpty) return null;
    return CaptureTraceTupleFingerprint._(
        sha256.convert(utf8.encode(jsonEncode([conversation, generation]))).toString());
  }
}

/// A capability, not an account/conversation identifier. Old capabilities cannot
/// mutate the trace after a new attempt or owner-context reset.
class CaptureTraceAttempt {
  CaptureTraceAttempt._(this.id);
  final int id;
}

class CaptureTraceEntry {
  CaptureTraceEntry._(this.data);
  final Map<String, Object?> data;
  Map<String, Object?> toJson() => Map.unmodifiable(data);
}

/// Closed-schema, memory-only diagnostic ring. No arbitrary strings/maps enter
/// the collector. No I/O, timers, authority changes, retries or audio actions.
class EllaCaptureProtocolTrace {
  EllaCaptureProtocolTrace({DateTime Function()? utcNow, int Function()? monotonicMicros})
      : _utcNow = utcNow ?? DateTime.now,
        _monotonicMicros = monotonicMicros {
    _clock.start();
  }
  static const capacity = 64;
  final DateTime Function() _utcNow;
  final int Function()? _monotonicMicros;
  final Stopwatch _clock = Stopwatch();
  final List<CaptureTraceEntry> _entries = [];
  CaptureTraceAttempt? _active;
  int _attemptSequence = 0, _sequence = 0;
  CaptureTraceAttempt? get activeAttempt => _active;
  List<CaptureTraceEntry> get snapshot => List.unmodifiable(_entries);
  void clear() {
    _active = null;
    _entries.clear();
    _sequence = 0;
    _attemptSequence = 0;
    _clock.reset();
  }

  CaptureTraceAttempt begin({int? socketGeneration, bool? paused}) {
    final attempt = CaptureTraceAttempt._(++_attemptSequence);
    _active = attempt;
    record(attempt, CaptureTraceEvent.attemptBegin, socketGeneration: socketGeneration, paused: paused);
    return attempt;
  }

  void record(
    CaptureTraceAttempt? attempt,
    CaptureTraceEvent event, {
    bool? ownerCurrent,
    bool? deliveryCurrent,
    bool? wasReady,
    bool? paused,
    bool? deliveryOptIn,
    int? closeCode,
    int? socketGeneration,
    int? transportGeneration,
    Duration? deadline,
    CaptureTraceFieldType? conversationType,
    CaptureTraceFieldType? generationType,
    CaptureTraceFieldType? tokenType,
    CaptureTraceFieldType? versionType,
    bool? versionIsTwo,
    CaptureTraceTupleFingerprint? tupleFingerprint,
    bool? tupleCurrent,
    CaptureTraceValidation? validation,
    CaptureTraceFailure? failure,
    CaptureTraceCloseOrigin? closeOrigin,
    CaptureTraceStopReason? stopReason,
  }) {
    // Includes an old callback whose numeric id happens to repeat after clear.
    if (!identical(attempt, _active)) return;
    try {
      final elapsed = _monotonicMicros?.call() ?? _clock.elapsedMicroseconds;
      final data = <String, Object?>{
        'sequence': ++_sequence,
        'attempt_id': attempt?.id,
        'utc': _utcNow().toUtc().toIso8601String(),
        'monotonic_us': elapsed,
        'event': event.name,
        if (ownerCurrent != null) 'owner_current': ownerCurrent,
        if (deliveryCurrent != null) 'delivery_current': deliveryCurrent,
        if (wasReady != null) 'was_ready': wasReady,
        if (paused != null) 'paused': paused,
        if (deliveryOptIn != null) 'delivery_opt_in': deliveryOptIn,
        if (socketGeneration != null) 'socket_generation': socketGeneration,
        if (transportGeneration != null) 'transport_generation': transportGeneration,
        if (closeCode != null && closeCode >= 1000 && closeCode <= 4999) 'close_code': closeCode,
        if (deadline != null) 'deadline_after_us': deadline.inMicroseconds,
        if (deadline != null) 'deadline_monotonic_us': elapsed + deadline.inMicroseconds,
        if (conversationType != null) 'conversation_type': conversationType.name,
        if (generationType != null) 'generation_type': generationType.name,
        if (tokenType != null) 'owner_token_type': tokenType.name,
        if (versionType != null) 'version_type': versionType.name,
        if (versionIsTwo != null) 'version_is_two': versionIsTwo,
        if (tupleFingerprint != null) 'tuple_sha256': tupleFingerprint.sha256Hex,
        if (tupleCurrent != null) 'tuple_current': tupleCurrent,
        if (validation != null) 'validation': validation.name,
        if (failure != null) 'failure': failure.name,
        if (closeOrigin != null) 'close_origin': closeOrigin.name,
        if (stopReason != null) 'stop_reason': stopReason.name,
      };
      _entries.add(CaptureTraceEntry._(Map.unmodifiable(data)));
      if (_entries.length > capacity) _entries.removeAt(0);
    } catch (_) {
      // Diagnostic clock/observer failures must not affect capture behavior.
    }
  }

  static CaptureTraceFieldType fieldType(Map<dynamic, dynamic> map, String key) {
    if (!map.containsKey(key)) return CaptureTraceFieldType.absent;
    final value = map[key];
    if (value == null) return CaptureTraceFieldType.nullValue;
    if (value is String) return value.isEmpty ? CaptureTraceFieldType.emptyString : CaptureTraceFieldType.string;
    if (value is bool) return CaptureTraceFieldType.boolean;
    if (value is int) return CaptureTraceFieldType.integer;
    if (value is num) return CaptureTraceFieldType.number;
    if (value is List) return CaptureTraceFieldType.list;
    if (value is Map) return CaptureTraceFieldType.map;
    return CaptureTraceFieldType.other;
  }

  static CaptureTraceFailure classifyFailure(String reason) => switch (reason) {
        'capture_origin_retired' => CaptureTraceFailure.originRetired,
        'transport_connect_failed' => CaptureTraceFailure.connectFailed,
        'capture_protocol_ready_unavailable' => CaptureTraceFailure.readyUnavailable,
        'invalid_capture_protocol_ready' => CaptureTraceFailure.invalidReady,
        'capture_socket_closed_before_ready' => CaptureTraceFailure.closedBeforeReady,
        'capture_socket_closed' => CaptureTraceFailure.closedAfterReady,
        'capture_socket_error' => CaptureTraceFailure.socketError,
        _ => CaptureTraceFailure.other,
      };
  static CaptureTraceStopReason classifyStop(String? reason) => switch (reason) {
        'capture socket replaced' => CaptureTraceStopReason.replaced,
        'transcription settings changed' => CaptureTraceStopReason.settingsChanged,
        'delivery binding retired during authentication' ||
        'delivery binding retired during connect' ||
        'delivery binding retired before ready' =>
          CaptureTraceStopReason.bindingRetired,
        'capture binding retired during authentication' ||
        'stale transcription socket attempt' =>
          CaptureTraceStopReason.authorityRetired,
        null => CaptureTraceStopReason.ordinaryStop,
        _ => CaptureTraceStopReason.other,
      };
  static String supportText(List<CaptureTraceEntry> entries) =>
      const JsonEncoder.withIndent('  ').convert(entries.map((e) => e.toJson()).toList());
}
