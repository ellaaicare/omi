import 'dart:async';
import 'dart:convert';

import 'package:omi/services/sockets/transcription_service.dart' as ella_socket;
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
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
    void Function(String reason, int? closeCode)? onAdmissionFailure,
    bool Function()? hasOriginAuthority,
  })  : _timeout = timeout,
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
  final void Function(String reason, int? closeCode)? _onAdmissionFailure;
  final bool Function() _hasOriginAuthority;
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
  bool get hasOriginAuthority => _hasOriginAuthority();

  static bool _alwaysCurrent() => true;

  @override
  SocketServiceState get state => _ready && super.state == SocketServiceState.connected
      ? SocketServiceState.connected
      : SocketServiceState.disconnected;

  @override
  Future start() async {
    if (!_hasOriginAuthority()) {
      _fail('capture_origin_retired');
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
    await super.start();
    if (socket.status != PureSocketStatus.connected || !_hasOriginAuthority()) {
      _fail('transport_connect_failed');
      if (socket.status == PureSocketStatus.connected) await stop();
      return;
    }
    if (!await waiter.future.timeout(_timeout, onTimeout: () => false) || state != SocketServiceState.connected) {
      _fail('capture_protocol_ready_unavailable');
      if (socket.status == PureSocketStatus.connected) await stop();
    }
  }

  @override
  Future send(dynamic message) async {
    if (state != SocketServiceState.connected || !_hasOriginAuthority()) return;
    await super.send(message);
  }

  @override
  Future sendText(String message) async {
    if (state != SocketServiceState.connected || !_hasOriginAuthority()) return;
    await super.sendText(message);
  }

  @override
  Future<void> stop({String? reason}) => _stopFuture ??= _stop(reason);

  Future<void> _stop(String? reason) async {
    _stopping = true;
    final canDrain = _ready && _hasOriginAuthority() && socket.status == PureSocketStatus.connected;
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
        if (!_hasOriginAuthority() || transport.status != PureSocketStatus.connected) {
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
  }

  @override
  void onMessage(dynamic event) {
    if (_stopping && _pendingDrain == null) return;
    if (_failed) return;
    if (!_hasOriginAuthority()) {
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
      if (decoded is Map && decoded['type'] == 'service_status') {
        final status = decoded['status'];
        if (status == 'capture_protocol_ready') {
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
            _fail('invalid_capture_protocol_ready');
            unawaited(stop());
            return;
          }
          if (next == null || (!isInitial && !isIdempotent && !isSuccessor)) return;
          if (_stopping) return;
          if (isSuccessor) _retiredConversationIds.add(current.conversationId);
          _authority = next;
          final firstReady = !_ready;
          _ready = true;
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
    if (_ready) super.onMessage(event);
  }

  @override
  void onClosed([int? closeCode]) {
    final wasReady = _ready;
    _ready = false;
    _completeWaiters();
    if (!_stopping && _hasOriginAuthority()) {
      _fail(wasReady ? 'capture_socket_closed' : 'capture_socket_closed_before_ready', closeCode);
    }
    super.onClosed(closeCode);
  }

  @override
  void onError(Object err, StackTrace trace) {
    _ready = false;
    _completeWaiters();
    if (!_stopping && _hasOriginAuthority()) _fail('capture_socket_error');
    super.onError(err, trace);
  }

  void _completeWaiters() {
    if (!(_readyWaiter?.isCompleted ?? true)) _readyWaiter!.complete(false);
    if (!(_drainWaiter?.isCompleted ?? true)) _drainWaiter!.complete(false);
  }

  void _fail(String reason, [int? closeCode]) {
    if (_failed || _stopping) return;
    _failed = true;
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
  }) =>
      EllaCaptureCompositeSocket._(
        _DrainablePrimarySocket(primarySocket),
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
  _DrainablePrimarySocket(this._inner) {
    _inner.setListener(this);
  }

  final IPureSocket _inner;
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
  void onMessage(dynamic message) => _listener?.onMessage(message);

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
Uri ellaCaptureProtocolUri(Uri upstreamUri) =>
    upstreamUri.replace(queryParameters: {...upstreamUri.queryParameters, 'capture_protocol': '$_protocolVersion'});

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
}) {
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
    final url = ellaCaptureProtocolUri(Uri.parse(old.url)).toString();
    return PureSocket(
      url,
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
    onAdmissionFailure: onAdmissionFailure,
    hasOriginAuthority: hasOriginAuthority,
  );
}
