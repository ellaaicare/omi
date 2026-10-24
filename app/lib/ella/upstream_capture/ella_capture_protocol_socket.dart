import 'dart:async';
import 'dart:convert';
import 'package:omi/ella/capture_host/ella_capture_protocol_trace.dart';
import 'package:omi/ella/services/whisper_delivery_runtime.dart';

import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:omi/services/sockets/transcription_service.dart' as ella_socket;
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/backend/http/shared.dart' as upstream_http;
import 'package:omi/upstream_capture/backend/schema/geolocation.dart';
import 'package:omi/upstream_capture/models/custom_stt_config.dart';
import 'package:omi/upstream_capture/services/sockets/pure_socket.dart';
import 'package:omi/upstream_capture/services/sockets/transcription_service.dart';
import 'package:omi/utils/debug_log_manager.dart';

const _protocolVersion = 2;
const _protocolTimeout = Duration(seconds: 8);

/// Ella's backend protocol at the fork-side socket seam. The upstream socket
/// factory still chooses the codec/STT path; only its Omi transport is replaced.
class EllaCaptureProtocolSocket extends TranscriptSegmentSocketService {
  // The positional transport is intentionally passed to the upstream named constructor.
  // ignore: use_super_parameters
  EllaCaptureProtocolSocket.withTransport(
    int sampleRate,
    BleAudioCodec codec,
    String language,
    IPureSocket transport, {
    String? source,
    String? clientConversationId,
    bool includeSpeechProfile = true,
    bool customSttMode = false,
    String? sttConfigId,
    Geolocation? geolocation,
    Duration timeout = _protocolTimeout,
    this.diagnosticTrace,
    this.diagnosticAttempt,
    void Function(String reason, int? closeCode)? onAdmissionFailure,
    bool Function()? hasOriginAuthority,
    Map<String, String> deliveryQuery = const {},
    bool Function()? hasCurrentDeliveryBinding,
  })  : _timeout = timeout,
        deliveryQuery = Map.unmodifiable(deliveryQuery),
        _hasCurrentDeliveryBinding = hasCurrentDeliveryBinding ?? _alwaysCurrent,
        _onAdmissionFailure = onAdmissionFailure,
        _hasOriginAuthority = hasOriginAuthority ?? _alwaysCurrent,
        super.withSocket(
          sampleRate,
          codec,
          language,
          transport,
          source: source,
          clientConversationId: clientConversationId,
          includeSpeechProfile: includeSpeechProfile,
          customSttMode: customSttMode,
          sttConfigId: sttConfigId,
          geolocation: geolocation,
        );

  final Duration _timeout;
  final EllaCaptureProtocolTrace? diagnosticTrace;
  final CaptureTraceAttempt? diagnosticAttempt;
  void _diagnose(CaptureTraceEvent event,
      {CaptureTraceValidation? validation,
      CaptureTraceFailure? failure,
      CaptureTraceCloseOrigin? closeOrigin,
      CaptureTraceStopReason? stopReason,
      int? closeCode,
      Duration? deadline,
      Map<dynamic, dynamic>? readyFields,
      bool? tupleCurrent}) {
    if (diagnosticTrace == null) return;
    try {
      diagnosticTrace!.record(diagnosticAttempt, event,
          ownerCurrent: hasOriginAuthority,
          deliveryCurrent: deliveryBindingCurrent,
          wasReady: _ready,
          validation: validation,
          tupleCurrent: tupleCurrent,
          tupleFingerprint: readyFields == null ? null : CaptureTraceTupleFingerprint.fromReadyFields(readyFields),
          failure: failure,
          closeOrigin: closeOrigin,
          stopReason: stopReason,
          closeCode: closeCode,
          deadline: deadline,
          conversationType:
              readyFields == null ? null : EllaCaptureProtocolTrace.fieldType(readyFields, 'conversation_id'),
          generationType: readyFields == null ? null : EllaCaptureProtocolTrace.fieldType(readyFields, 'generation'),
          tokenType: readyFields == null ? null : EllaCaptureProtocolTrace.fieldType(readyFields, 'owner_token'),
          versionType: readyFields == null ? null : EllaCaptureProtocolTrace.fieldType(readyFields, 'protocol_version'),
          versionIsTwo: readyFields == null ? null : readyFields['protocol_version'] == _protocolVersion);
    } catch (_) {}
  }

  final void Function(String reason, int? closeCode)? _onAdmissionFailure;
  final bool Function() _hasOriginAuthority;
  final Map<String, String> deliveryQuery;
  final bool Function() _hasCurrentDeliveryBinding;
  bool _deliveryBindingRetired = false;
  bool get deliveryBindingCurrent => !_deliveryBindingRetired && _hasCurrentDeliveryBinding();

  /// Stop admitting frames/callbacks synchronously, retaining only the exact
  /// capture drain acknowledgement. The recorder/WAL remain upstream-owned.
  void retireDeliveryBinding() {
    _diagnose(CaptureTraceEvent.bindingRetired);
    _deliveryBindingRetired = true;
    if (!(_readyWaiter?.isCompleted ?? true)) _readyWaiter!.complete(false);
  }

  Completer<bool>? _readyWaiter;
  Completer<bool>? _drainWaiter;
  ella_socket.CaptureProtocolAuthority? _authority;
  ella_socket.CaptureProtocolAuthority? _pendingDrain;
  final Set<String> _retiredConversationIds = {};
  bool _ready = false;
  bool _stopping = false;
  bool _failed = false;
  bool _drainAcknowledged = false;
  Future<void>? _stopFuture;

  ella_socket.CaptureProtocolAuthority? get captureAuthority => _authority;
  bool get drainAcknowledged => _drainAcknowledged;
  bool _originRetired = false;
  bool get hasOriginAuthority => !_originRetired && _hasOriginAuthority();

  /// Terminal runtime disposal cannot leave a drain or ready wait alive.
  void retireCaptureOrigin() {
    _diagnose(CaptureTraceEvent.originRetired);
    _originRetired = true;
    retireDeliveryBinding();
    _completeWaiters();
  }

  static bool _alwaysCurrent() => true;

  @override
  SocketServiceState get state => _ready && deliveryBindingCurrent && super.state == SocketServiceState.connected
      ? SocketServiceState.connected
      : SocketServiceState.disconnected;

  @override
  Future start() async {
    _lastAttempt = EllaCaptureSocketAttempt(EllaCaptureSocketAttemptPhase.connecting, null, DateTime.now());
    _lastAdmissionFailure = null;
    if (!hasOriginAuthority) {
      _fail('capture_origin_retired');
      return;
    }
    if (!deliveryBindingCurrent) {
      retireDeliveryBinding();
      return;
    }
    _ready = false;
    _stopping = false;
    _failed = false;
    _drainAcknowledged = false;
    _authority = null;
    _pendingDrain = null;
    _retiredConversationIds.clear();
    final waiter = _readyWaiter = Completer<bool>();
    try {
      _diagnose(CaptureTraceEvent.superStartBegin);
      await super.start();
      _diagnose(CaptureTraceEvent.superStartEnd);
    } catch (_) {
      _diagnose(CaptureTraceEvent.transportConnectFailed);
      if (!hasOriginAuthority || !deliveryBindingCurrent) {
        retireDeliveryBinding();
        await stop(reason: 'capture binding retired during authentication');
        return;
      }
      _lastAttempt = EllaCaptureSocketAttempt(EllaCaptureSocketAttemptPhase.transportUnavailable, null, DateTime.now());
      rethrow;
    }
    if (!hasOriginAuthority) {
      _fail('capture_origin_retired');
      if (socket.status == PureSocketStatus.connected) await stop();
      return;
    }
    if (!deliveryBindingCurrent) {
      retireDeliveryBinding();
      await stop(reason: 'delivery binding retired during connect');
      return;
    }
    if (socket.status != PureSocketStatus.connected) {
      _fail('transport_connect_failed');
      return;
    }
    _diagnose(CaptureTraceEvent.readyWaitBegin, deadline: _timeout);
    final admitted = await waiter.future.timeout(_timeout, onTimeout: () {
      _diagnose(CaptureTraceEvent.readyDeadline, closeOrigin: CaptureTraceCloseOrigin.readyDeadline);
      return false;
    });
    if (!deliveryBindingCurrent) {
      retireDeliveryBinding();
      await stop(reason: 'delivery binding retired before ready');
      return;
    }
    if (!admitted || state != SocketServiceState.connected) {
      _fail('capture_protocol_ready_unavailable');
      if (socket.status == PureSocketStatus.connected) await stop();
    }
  }

  @override
  Future send(dynamic message) async {
    if (state != SocketServiceState.connected || !hasOriginAuthority) return;
    await super.send(message);
  }

  @override
  Future sendText(String message) async {
    if (state != SocketServiceState.connected || !hasOriginAuthority) return;
    await super.sendText(message);
  }

  @override
  Future<void> stop({String? reason}) => _stopFuture ??= _stop(reason);

  Future<void> _stop(String? reason) async {
    _diagnose(CaptureTraceEvent.localCloseRequest,
        closeOrigin: CaptureTraceCloseOrigin.localRequest, stopReason: EllaCaptureProtocolTrace.classifyStop(reason));
    _stopping = true;
    if (!_failed) _lastAttempt = null;
    final canDrain = _ready && hasOriginAuthority && socket.status == PureSocketStatus.connected;
    _ready = false;
    final authority = _authority;
    if (canDrain && authority != null) {
      final waiter = _drainWaiter = Completer<bool>();
      _pendingDrain = authority;
      try {
        final transport = socket;
        if (transport is EllaCaptureCompositeSocket) {
          await transport.stopPrimaryForDrain();
        }
        if (!hasOriginAuthority || transport.status != PureSocketStatus.connected) {
          throw StateError('origin retired');
        }
        socket.send(jsonEncode(authority.toDrainJson()));
        _drainAcknowledged = await waiter.future.timeout(_timeout, onTimeout: () => false);
      } catch (_) {
        _drainAcknowledged = false;
      }
    }
    _pendingDrain = null;
    await super.stop(reason: reason);
  }

  @override
  void onConnected() {
    // A transport handshake is not capture authority.
    if (!_failed && !_stopping && hasOriginAuthority && deliveryBindingCurrent) {
      _lastAttempt = EllaCaptureSocketAttempt(EllaCaptureSocketAttemptPhase.transportConnected, null, DateTime.now());
    }
  }

  @override
  void onMessage(dynamic event) {
    if (_stopping && _pendingDrain == null) return;
    if (_failed) return;
    if (!hasOriginAuthority) {
      _fail('capture_origin_retired');
      unawaited(stop());
      return;
    }
    if (event is String) {
      Object? decoded;
      try {
        decoded = jsonDecode(event);
      } on FormatException {
        // Upstream remains responsible for ordinary message parsing.
      }
      if (!deliveryBindingCurrent &&
          !(decoded is Map && decoded['type'] == 'service_status' && decoded['status'] == 'capture_protocol_drained')) {
        return;
      }
      if (decoded is Map && (decoded['type'] == 'memory_processing_started' || decoded['type'] == 'memory_created')) {
        final current = _authority;
        final memory = decoded['memory'];
        if (current == null || memory is! Map || memory['id'] != current.conversationId) return;
        // The bound socket is the attribution when legacy progress omits the
        // tuple. If a tuple is supplied, every field must match exactly.
        if (decoded.containsKey('generation') ||
            decoded.containsKey('owner_token') ||
            decoded.containsKey('protocol_version') ||
            decoded.containsKey('conversation_id')) {
          if (!_sameAuthority(current, _readAuthority(decoded))) return;
        }
      }
      if (decoded is Map && decoded['type'] == 'service_status') {
        final status = decoded['status'];
        if (status == 'capture_protocol_ready') {
          _diagnose(CaptureTraceEvent.readyReceived, readyFields: decoded);
          final next = _readAuthority(decoded);
          final current = _authority;
          final isInitial = current == null;
          final isIdempotent = current != null && _sameAuthority(current, next);
          final isSuccessor = current != null &&
              next != null &&
              current.protocolVersion == next.protocolVersion &&
              current.generation == next.generation &&
              current.ownerToken == next.ownerToken &&
              current.conversationId != next.conversationId &&
              !_retiredConversationIds.contains(next.conversationId);
          if (next == null && isInitial) {
            _diagnose(CaptureTraceEvent.readyValidated,
                validation: CaptureTraceValidation.invalidFields, readyFields: decoded, tupleCurrent: false);
            _fail('invalid_capture_protocol_ready');
            unawaited(stop());
            return;
          }
          if (next == null || (!isInitial && !isIdempotent && !isSuccessor)) {
            _diagnose(CaptureTraceEvent.readyValidated,
                validation: CaptureTraceValidation.unrelated, readyFields: decoded, tupleCurrent: false);
            return;
          }
          if (_stopping) {
            _diagnose(CaptureTraceEvent.readyValidated,
                validation: CaptureTraceValidation.stopping, readyFields: decoded, tupleCurrent: false);
            return;
          }
          _diagnose(CaptureTraceEvent.readyValidated,
              readyFields: decoded,
              tupleCurrent: true,
              validation: isInitial
                  ? CaptureTraceValidation.initial
                  : isIdempotent
                      ? CaptureTraceValidation.idempotent
                      : CaptureTraceValidation.successor);
          if (isSuccessor) _retiredConversationIds.add(current.conversationId);
          _authority = next;
          final firstReady = !_ready;
          _ready = true;
          _lastAttempt = EllaCaptureSocketAttempt(EllaCaptureSocketAttemptPhase.captureReady, null, DateTime.now());
          if (!(_readyWaiter?.isCompleted ?? true)) _readyWaiter!.complete(true);
          if (firstReady) super.onConnected();
        } else if (status == 'capture_protocol_drained') {
          final drained = _readAuthority(decoded);
          if (_pendingDrain != null && _sameAuthority(_pendingDrain, drained) && !(_drainWaiter?.isCompleted ?? true)) {
            _drainWaiter!.complete(true);
          }
        }
      }
    }
    if (!deliveryBindingCurrent) return;
    if (_ready) super.onMessage(event);
  }

  @override
  void onClosed([int? closeCode]) {
    _diagnose(CaptureTraceEvent.closeCallback, closeCode: closeCode);
    final wasReady = _ready;
    _ready = false;
    _completeWaiters();
    if (!deliveryBindingCurrent) return;
    if (!_stopping && hasOriginAuthority) {
      _fail(wasReady ? 'capture_socket_closed' : 'capture_socket_closed_before_ready', closeCode);
    }
    super.onClosed(closeCode);
  }

  @override
  void onError(Object err, StackTrace trace) {
    _ready = false;
    _completeWaiters();
    if (!deliveryBindingCurrent) return;
    if (!_stopping && hasOriginAuthority) _fail('capture_socket_error');
    super.onError(err, trace);
  }

  void _completeWaiters() {
    if (!(_readyWaiter?.isCompleted ?? true)) _readyWaiter!.complete(false);
    if (!(_drainWaiter?.isCompleted ?? true)) _drainWaiter!.complete(false);
  }

  void _fail(String reason, [int? closeCode]) {
    if (_failed || _stopping) return;
    _diagnose(CaptureTraceEvent.failure,
        failure: EllaCaptureProtocolTrace.classifyFailure(reason), closeCode: closeCode);
    _failed = true;
    _lastAdmissionFailure = EllaCaptureSocketFailure.fromReason(reason, closeCode, DateTime.now());
    final phase = switch (reason) {
      'capture_origin_retired' => EllaCaptureSocketAttemptPhase.originRetired,
      'transport_connect_failed' => EllaCaptureSocketAttemptPhase.transportUnavailable,
      'capture_protocol_ready_unavailable' => EllaCaptureSocketAttemptPhase.readyUnavailable,
      'invalid_capture_protocol_ready' => EllaCaptureSocketAttemptPhase.invalidReady,
      'capture_socket_closed_before_ready' => EllaCaptureSocketAttemptPhase.closedBeforeReady,
      'capture_socket_closed' => EllaCaptureSocketAttemptPhase.closedAfterReady,
      _ => EllaCaptureSocketAttemptPhase.socketError,
    };
    _lastAttempt = EllaCaptureSocketAttempt(phase, closeCode, DateTime.now());
    _ready = false;
    _completeWaiters();
    unawaited(
      DebugLogManager.logWarning('ella_capture_protocol_unavailable', {
        'reason': reason,
        'close_code': closeCode ?? -1,
      }),
    );
    _onAdmissionFailure?.call(reason, closeCode);
  }

  EllaCaptureSocketFailure? _lastAdmissionFailure;
  EllaCaptureSocketFailure? get lastAdmissionFailure => _lastAdmissionFailure;
  EllaCaptureSocketAttempt? _lastAttempt;
  EllaCaptureSocketAttempt? get lastAttempt => _lastAttempt;

  static ella_socket.CaptureProtocolAuthority? _readAuthority(Map<dynamic, dynamic> message) {
    final conversationId = message['conversation_id'];
    final generation = message['generation'];
    final ownerToken = message['owner_token'];
    if (message['protocol_version'] != _protocolVersion ||
        conversationId is! String ||
        conversationId.trim().isEmpty ||
        generation is! String ||
        generation.trim().isEmpty ||
        ownerToken is! String ||
        ownerToken.trim().isEmpty) {
      return null;
    }
    return ella_socket.CaptureProtocolAuthority(
      protocolVersion: _protocolVersion,
      conversationId: conversationId,
      generation: generation,
      ownerToken: ownerToken,
    );
  }

  static bool _sameAuthority(ella_socket.CaptureProtocolAuthority? left, ella_socket.CaptureProtocolAuthority? right) =>
      left != null &&
      right != null &&
      left.protocolVersion == right.protocolVersion &&
      left.conversationId == right.conversationId &&
      left.generation == right.generation &&
      left.ownerToken == right.ownerToken;
}

/// Lets the imported composite flush close-only custom-STT segments while its
/// backend secondary remains open for the exact durable drain acknowledgement.
class EllaCaptureCompositeSocket extends CompositeTranscriptionSocket {
  factory EllaCaptureCompositeSocket({
    required IPureSocket primarySocket,
    required IPureSocket secondarySocket,
    String? sttProvider,
    String? suggestedTranscriptType = 'suggested_transcript',
    bool forwardRawAudioToSecondary = true,
    bool Function()? hasOriginAuthority,
  }) =>
      EllaCaptureCompositeSocket._(
        _DrainablePrimarySocket(primarySocket, hasOriginAuthority: hasOriginAuthority),
        secondarySocket: secondarySocket,
        sttProvider: sttProvider,
        suggestedTranscriptType: suggestedTranscriptType,
        forwardRawAudioToSecondary: forwardRawAudioToSecondary,
      );

  EllaCaptureCompositeSocket._(
    _DrainablePrimarySocket primary, {
    required super.secondarySocket,
    super.sttProvider,
    super.suggestedTranscriptType,
    super.forwardRawAudioToSecondary,
  })  : _primary = primary,
        super(primarySocket: primary);

  final _DrainablePrimarySocket _primary;

  Future<void> stopPrimaryForDrain() => _primary.stop();
}

class _DrainablePrimarySocket implements IPureSocket, IPureSocketListener {
  _DrainablePrimarySocket(this._inner, {bool Function()? hasOriginAuthority})
      : _hasOriginAuthority = hasOriginAuthority ?? EllaCaptureProtocolSocket._alwaysCurrent {
    _inner.setListener(this);
  }

  final IPureSocket _inner;
  final bool Function() _hasOriginAuthority;
  IPureSocketListener? _listener;
  Future<void>? _stopFuture;
  bool _draining = false;

  @override
  PureSocketStatus get status => _inner.status;

  @override
  Future<bool> connect() => _inner.connect();

  @override
  Future disconnect() => _inner.disconnect();

  @override
  Future<void> stop() => _stopFuture ??= _stop();

  Future<void> _stop() async {
    _draining = true;
    await _inner.stop();
  }

  @override
  void send(dynamic message) {
    if (!_draining) _inner.send(message);
  }

  @override
  void setListener(IPureSocketListener listener) => _listener = listener;

  @override
  void onMessage(dynamic message) {
    if (_hasOriginAuthority()) _listener?.onMessage(message);
  }

  @override
  void onConnected() => _listener?.onConnected();

  @override
  void onClosed([int? closeCode]) {
    if (!_draining) _listener?.onClosed(closeCode);
  }

  @override
  void onError(Object err, StackTrace trace) {
    if (!_draining) _listener?.onError(err, trace);
  }
}

/// Constructs the same upstream STT path with a v2 Omi secondary transport.
SocketHeadersProvider ellaCaptureSocketHeaders(
  SocketHeadersProvider authenticatedHeaders, {
  required bool Function() hasOriginAuthority,
  required bool Function() hasCurrentDeliveryBinding,
}) =>
    () async {
      void check() {
        if (!hasOriginAuthority() || !hasCurrentDeliveryBinding()) {
          throw StateError('capture socket binding retired');
        }
      }

      check();
      final headers = await authenticatedHeaders();
      check();
      return headers;
    };

Uri ellaCaptureProtocolUri(Uri upstreamUri, {Map<String, String>? deliveryQuery}) =>
    upstreamUri.replace(queryParameters: {
      for (final entry in upstreamUri.queryParameters.entries)
        if (!entry.key.startsWith('delivery_')) entry.key: entry.value,
      'capture_protocol': '$_protocolVersion',
      ...?deliveryQuery,
      if (deliveryQuery == null && whisperDeliveryProtocol1) ...WhisperDeliveryRuntime.shared.client.captureQuery
    });

EllaCaptureProtocolSocket createEllaCaptureProtocolSocket({
  required BleAudioCodec codec,
  required int sampleRate,
  required String language,
  String? source,
  String? clientConversationId,
  CustomSttConfig? customSttConfig,
  Geolocation? geolocation,
  void Function(String reason, int? closeCode)? onAdmissionFailure,
  bool Function()? hasOriginAuthority,
  Map<String, String>? deliveryQuery,
  bool Function()? hasCurrentDeliveryBinding,
  EllaCaptureProtocolTrace? diagnosticTrace,
  CaptureTraceAttempt? diagnosticAttempt,
}) {
  final query = Map<String, String>.unmodifiable(deliveryQuery ??
      (whisperDeliveryProtocol1 ? WhisperDeliveryRuntime.shared.client.captureQuery : const <String, String>{}));
  final originAuthority = hasOriginAuthority ?? EllaCaptureProtocolSocket._alwaysCurrent;
  final template = customSttConfig != null && customSttConfig.isEnabled
      ? TranscriptSocketServiceFactory.createFromCustomConfig(
          sampleRate,
          codec,
          language,
          customSttConfig,
          source: source,
          clientConversationId: clientConversationId,
          geolocation: geolocation,
        )
      : TranscriptSocketServiceFactory.createDefault(
          sampleRate,
          codec,
          language,
          source: source,
          clientConversationId: clientConversationId,
          geolocation: geolocation,
        );
  final original = template.socket;
  PureSocket replace(PureSocket old) {
    final url = ellaCaptureProtocolUri(Uri.parse(old.url), deliveryQuery: query).toString();
    return PureSocket(
      url,
      headersProvider: ellaCaptureSocketHeaders(
        () => upstream_http.buildHeaders(requireAuthCheck: true, url: url, forWebSocket: true),
        hasOriginAuthority: originAuthority,
        hasCurrentDeliveryBinding: hasCurrentDeliveryBinding ?? EllaCaptureProtocolSocket._alwaysCurrent,
      ),
      diagnosticObserver: diagnosticTrace == null
          ? null
          : (event, generation, closeCode) {
              try {
                final kind = switch (event) {
                  PureSocketDiagnosticEvent.listenerAttached => CaptureTraceEvent.listenerAttached,
                  PureSocketDiagnosticEvent.transportConnected => CaptureTraceEvent.transportConnected,
                  PureSocketDiagnosticEvent.cleanupCloseRequested => CaptureTraceEvent.localCloseRequest,
                  PureSocketDiagnosticEvent.remoteClosed => CaptureTraceEvent.remoteCloseCallback,
                  PureSocketDiagnosticEvent.transportError => CaptureTraceEvent.transportErrorCallback,
                };
                diagnosticTrace.record(diagnosticAttempt, kind,
                    transportGeneration: generation,
                    closeCode: closeCode,
                    closeOrigin: event == PureSocketDiagnosticEvent.cleanupCloseRequested
                        ? CaptureTraceCloseOrigin.cleanup
                        : event == PureSocketDiagnosticEvent.remoteClosed
                            ? CaptureTraceCloseOrigin.remoteCallback
                            : event == PureSocketDiagnosticEvent.transportError
                                ? CaptureTraceCloseOrigin.transportError
                                : null,
                    ownerCurrent: originAuthority(),
                    deliveryCurrent: (hasCurrentDeliveryBinding ?? EllaCaptureProtocolSocket._alwaysCurrent)());
              } catch (_) {}
            },
      extraHeaders: {if (geolocation != null) 'X-Omi-Conversation-Geolocation': jsonEncode(geolocation.toJson())},
    );
  }

  final IPureSocket transport;
  if (original is PureSocket) {
    transport = replace(original);
  } else if (original is CompositeTranscriptionSocket && original.secondarySocket is PureSocket) {
    transport = EllaCaptureCompositeSocket(
      primarySocket: original.primarySocket,
      secondarySocket: replace(original.secondarySocket as PureSocket),
      sttProvider: original.sttProvider,
      suggestedTranscriptType: original.suggestedTranscriptType,
      forwardRawAudioToSecondary: original.forwardRawAudioToSecondary,
      hasOriginAuthority: originAuthority,
    );
  } else {
    throw StateError('Unsupported upstream conversation transport for capture protocol v2');
  }
  return EllaCaptureProtocolSocket.withTransport(
    sampleRate,
    codec,
    language,
    transport,
    source: source,
    clientConversationId: clientConversationId,
    includeSpeechProfile: template.includeSpeechProfile,
    customSttMode: template.customSttMode,
    sttConfigId: template.sttConfigId,
    geolocation: geolocation,
    diagnosticTrace: diagnosticTrace,
    diagnosticAttempt: diagnosticAttempt,
    onAdmissionFailure: onAdmissionFailure,
    hasOriginAuthority: originAuthority,
    deliveryQuery: query,
    hasCurrentDeliveryBinding: hasCurrentDeliveryBinding,
  );
}
