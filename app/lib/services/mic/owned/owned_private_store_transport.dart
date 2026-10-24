import 'dart:async';
import 'package:flutter/services.dart';
import 'owned_capture_contract.dart';
import 'owned_phone_mic.g.dart';

/// This channel delegates every filesystem operation to the descriptor-backed
/// native store. It never accepts a caller path or claims a Dart write is durable.
abstract interface class OwnedPrivateTransport {
  Future<Object?> invoke(String method, Map<String, Object?> arguments);
}

class MethodChannelOwnedPrivateTransport implements OwnedPrivateTransport {
  MethodChannelOwnedPrivateTransport({BinaryMessenger? messenger})
    : _channel = MethodChannel('ella.owned_private_capture.v1', const StandardMethodCodec(), messenger);
  final MethodChannel _channel;
  @override
  Future<Object?> invoke(String method, Map<String, Object?> arguments) async {
    try {
      return await _channel.invokeMethod<Object?>(method, arguments);
    } catch (_) {
      // Platform errors can carry private paths. Do not forward their text.
      throw StateError('owned_private_transport_failed');
    }
  }
}

class TransportOwnedPrivateSinkFactory implements OwnedCapturePrivateSinkFactory {
  TransportOwnedPrivateSinkFactory(this.transport);
  final OwnedPrivateTransport transport;
  @override
  OwnedCapturePrivateSink prepare(OwnedCaptureContext capturedContext) =>
      TransportOwnedPrivateSink(capturedContext, transport);
}

class TransportOwnedPrivateSink implements OwnedCapturePrivateSink {
  TransportOwnedPrivateSink(this.context, this._transport);
  @override
  final OwnedCaptureContext context;
  final OwnedPrivateTransport _transport;
  String? _token;
  OwnedMicHandle? _handle;
  OwnedMicHandle? _bindingIntent;
  Future<void>? _bindingWork;
  Future<void>? _preparation;
  OwnedRetentionSnapshot? _preparedPolicy;
  @override
  String get storeToken => _token ?? (throw StateError('owned_private_not_prepared'));

  bool _authority() {
    try {
      return context.hasExactAuthority();
    } catch (_) {
      return false;
    }
  }

  OwnedRetentionSnapshot _policy() {
    try {
      return context.retention();
    } catch (_) {
      throw StateError('owned_private_policy_unavailable');
    }
  }

  OwnedRetentionSnapshot _current({bool requireAuthority = true}) {
    if (requireAuthority && !_authority()) throw StateError('owned_private_authority_retired');
    final policy = _policy();
    if (policy.generation < 0 || policy.retention == OwnedPrivateRetention.forbidden) {
      throw StateError('owned_private_retention_forbidden');
    }
    return policy;
  }

  void _fence(OwnedRetentionSnapshot captured, {bool requireAuthority = true}) {
    if (!_current(requireAuthority: requireAuthority).matches(captured))
      throw StateError('owned_private_policy_changed');
  }

  Future<Map<Object?, Object?>> _call(String method, Map<String, Object?> arguments, Set<String> keys) async {
    Object? raw;
    try {
      raw = await _transport.invoke(method, {'version': 1, ...arguments});
    } catch (_) {
      throw StateError('owned_private_transport_failed');
    }
    if (raw is! Map ||
        raw.length != keys.length ||
        raw.keys.any((k) => k is! String || !keys.contains(k)) ||
        raw['version'] is! int ||
        raw['version'] != 1)
      throw StateError('owned_private_reply_invalid');
    return Map<Object?, Object?>.from(raw);
  }

  @override
  Future<void> prepare() async {
    await (_preparation ??= _prepare());
    final policy = _preparedPolicy;
    if (policy == null) throw StateError('owned_private_prepare_unconfirmed');
    _fence(policy);
  }

  Future<void> _prepare() async {
    final policy = _current();
    if (policy.generation != 0) throw StateError('owned_private_initial_policy_invalid');
    final capabilities = await _call('getCapabilities', {}, {
      'version',
      'preparedLifecycle',
      'privateStore',
      'retirement',
      'neverStartedCancel',
    });
    if ([
      'preparedLifecycle',
      'privateStore',
      'retirement',
      'neverStartedCancel',
    ].any((key) => capabilities[key] != true)) {
      throw StateError('owned_private_capabilities_unavailable');
    }
    _fence(policy);
    final binding = context.binding;
    final reply = await _call(
      'preparePrivateStore',
      {'binding': binding.encode(), 'retention': policy.retention.name},
      {'version', 'storeToken', 'binding', 'retention', 'policyGeneration'},
    );
    // Retain the opaque allocation for later retirement even if authority drifted.
    final token = reply['storeToken'];
    if (token is! String || token.trim().isEmpty || token.length > 256) throw StateError('owned_private_token_invalid');
    if (!sameOwnedBinding(_binding(reply['binding']), binding) ||
        reply['retention'] != policy.retention.name ||
        _integer(reply['policyGeneration']) != 0)
      throw StateError('owned_private_prepare_association_invalid');
    _token = token;
    _fence(policy);
    _preparedPolicy = policy;
  }

  @override
  Future<void> bindEpoch(OwnedMicHandle handle) {
    final captured = copyOwnedHandle(handle);
    if (captured.nativeEpoch <= 0 || !sameOwnedBinding(captured.binding, context.binding)) {
      return Future.error(StateError('owned_private_handle_invalid'));
    }
    final intent = _bindingIntent;
    if (intent != null && !sameOwnedHandle(intent, captured)) {
      return Future.error(StateError('owned_private_rebind_forbidden'));
    }
    // Reserve intent before caller-supplied authority/policy observations can
    // reenter. Failed/ambiguous calls can retry only this exact handle.
    _bindingIntent ??= copyOwnedHandle(captured);
    final policy = _current();
    // Publish the single-flight receipt before the first native effect.
    var work = _bindingWork;
    if (work == null) {
      final receipt = Completer<void>();
      work = receipt.future;
      _bindingWork = work;
      Future<void>.sync(() => _bind(captured, policy)).then(
        (_) {
          _bindingWork = null;
          receipt.complete();
        },
        onError: (Object error, StackTrace stack) {
          _bindingWork = null;
          receipt.completeError(error, stack);
        },
      );
    }
    return work.then((_) => _fence(policy));
  }

  Future<void> _bind(OwnedMicHandle captured, OwnedRetentionSnapshot policy) async {
    final token = storeToken;
    final reply = await _call(
      'bindPrivateStore',
      {'storeToken': token, 'handle': _wireHandle(captured)},
      {'version', 'storeToken', 'handle'},
    );
    if (reply['storeToken'] != token || !sameOwnedHandle(_handleFrom(reply['handle']), captured)) {
      throw StateError('owned_private_bind_association_invalid');
    }
    _fence(policy);
    _handle = copyOwnedHandle(captured);
  }

  void _checkHandle(OwnedMicHandle handle) {
    if (_handle == null || !sameOwnedHandle(_handle!, handle)) throw StateError('owned_private_wrong_handle');
  }

  @override
  Future<OwnedMicStagedAck> stageOwnedPcm(OwnedMicFrame frame) async {
    final f = copyOwnedFrame(frame);
    _checkHandle(f.handle);
    if (f.sequence < 0 ||
        f.sequence >= 65536 ||
        f.pcmBytes.isEmpty ||
        f.pcmBytes.length > 2097152 ||
        f.pcmBytes.length.isOdd)
      throw StateError('owned_private_frame_invalid');
    final policy = _policy();
    final needsAuthority = policy.retention == OwnedPrivateRetention.retain;
    _fence(policy, requireAuthority: needsAuthority);
    final reply = await _call(
      'stagePrivatePcm',
      {
        'storeToken': storeToken,
        'handle': _wireHandle(f.handle),
        'sequence': f.sequence,
        'pcmBytes': Uint8List.fromList(f.pcmBytes),
        'policyGeneration': policy.generation,
      },
      {'version', 'policyGeneration', 'handle', 'sequence', 'acceptedValidPcmBytes'},
    );
    if (_integer(reply['policyGeneration']) != policy.generation ||
        !sameOwnedHandle(_handleFrom(reply['handle']), f.handle) ||
        _integer(reply['sequence']) != f.sequence ||
        _integer(reply['acceptedValidPcmBytes']) != f.pcmBytes.length) {
      throw StateError('owned_private_stage_association_invalid');
    }
    _fence(policy, requireAuthority: needsAuthority);
    return OwnedMicStagedAck(
      handle: copyOwnedHandle(f.handle),
      sequence: f.sequence,
      acceptedValidPcmBytes: f.pcmBytes.length,
    );
  }

  @override
  Future<OwnedMicDurableAck> commitOwnedDrain(
    OwnedMicDrainSummary summary, {
    required OwnedRetentionSnapshot policy,
    String? previousCommitId,
  }) async {
    final s = copyOwnedSummary(summary);
    _checkHandle(s.request.handle);
    _validateSummary(s);
    // A retired account may only obtain an explicit quarantine receipt for its
    // original binding, never a retained/new-account publication.
    final needsAuthority = policy.retention == OwnedPrivateRetention.retain;
    _fence(policy, requireAuthority: needsAuthority);
    if (previousCommitId != null && previousCommitId.trim().isEmpty) throw StateError('owned_private_previous_invalid');
    final reply = await _call(
      'commitPrivateDrain',
      {
        'storeToken': storeToken,
        'summary': [
          _wireHandle(s.request.handle),
          s.request.transactionId,
          s.admittedInputFrames,
          s.admittedTapBuffers,
          s.copyFailures,
          s.lastSequence,
          s.emittedChunks,
          s.emittedValidPcmBytes,
          s.emittedPcmSha256,
          s.eosResidualValidPcmBytes,
          s.converterReachedEos,
        ],
        'policyGeneration': policy.generation,
        'retention': policy.retention.name,
        'previousCommitId': previousCommitId,
      },
      {'version', 'policyGeneration', 'ack'},
    );
    if (_integer(reply['policyGeneration']) != policy.generation)
      throw StateError('owned_private_commit_policy_invalid');
    final ack = _durable(reply['ack']);
    final expectedDisposition = policy.retention == OwnedPrivateRetention.retain
        ? OwnedMicSinkDisposition.retainedExactBinding
        : OwnedMicSinkDisposition.quarantinedExactBinding;
    if (!sameOwnedRequest(ack.request, s.request) ||
        ack.lastSequence != s.lastSequence ||
        ack.durableChunks != s.emittedChunks ||
        ack.durableValidPcmBytes != s.emittedValidPcmBytes ||
        ack.pcmSha256 != s.emittedPcmSha256 ||
        ack.disposition != expectedDisposition ||
        ack.previousCommitId != previousCommitId ||
        (previousCommitId != null && ack.commitId == previousCommitId)) {
      throw StateError('owned_private_commit_association_invalid');
    }
    _fence(policy, requireAuthority: needsAuthority);
    return ack;
  }

  @override
  Future<OwnedRetentionSnapshot> retirePrivateRetention(OwnedRetentionSnapshot policy) async {
    if (policy.generation < 1 || policy.retention == OwnedPrivateRetention.retain || !_policy().matches(policy))
      throw StateError('owned_private_retirement_invalid');
    final reply = await _call(
      'retirePrivateStore',
      {'storeToken': storeToken, 'policyGeneration': policy.generation, 'retention': policy.retention.name},
      {'version', 'policyGeneration', 'retention'},
    );
    if (_integer(reply['policyGeneration']) != policy.generation ||
        reply['retention'] != policy.retention.name ||
        !_policy().matches(policy))
      throw StateError('owned_private_retirement_changed');
    return OwnedRetentionSnapshot(policy.generation, policy.retention);
  }
}

int _integer(Object? value) {
  if (value is! int) throw StateError('owned_private_integer_invalid');
  return value;
}

OwnedMicBinding _binding(Object? raw) {
  if (raw is! List ||
      raw.length != 11 ||
      [0, 1, 3, 5, 6, 7].any((i) => raw[i] is! String) ||
      [2, 4, 8, 9, 10].any((i) => raw[i] is! int))
    throw StateError('owned_private_binding_invalid');
  return OwnedMicBinding.decode(List<Object?>.from(raw));
}

List<Object?> _wireHandle(OwnedMicHandle h) => [h.binding.encode(), h.nativeEpoch];
OwnedMicHandle _handleFrom(Object? raw) {
  if (raw is! List || raw.length != 2) throw StateError('owned_private_handle_invalid');
  return OwnedMicHandle(binding: _binding(raw[0]), nativeEpoch: _integer(raw[1]));
}

OwnedMicDurableAck _durable(Object? raw) {
  if (raw is! List ||
      raw.length != 9 ||
      raw[1] is! String ||
      (raw[1] as String).trim().isEmpty ||
      raw[5] is! String ||
      !RegExp(r'^[0-9a-f]{64}$').hasMatch(raw[5] as String) ||
      raw[6] is! String ||
      (raw[6] as String).trim().isEmpty ||
      (raw[8] != null && raw[8] is! String))
    throw StateError('owned_private_ack_invalid');
  final disposition = switch (raw[7]) {
    'retainedExactBinding' => OwnedMicSinkDisposition.retainedExactBinding,
    'quarantinedExactBinding' => OwnedMicSinkDisposition.quarantinedExactBinding,
    _ => throw StateError('owned_private_disposition_invalid'),
  };
  return OwnedMicDurableAck(
    request: OwnedMicStopRequest(handle: _handleFrom(raw[0]), transactionId: raw[1] as String),
    lastSequence: _integer(raw[2]),
    durableChunks: _integer(raw[3]),
    durableValidPcmBytes: _integer(raw[4]),
    pcmSha256: raw[5] as String,
    commitId: raw[6] as String,
    disposition: disposition,
    previousCommitId: raw[8] as String?,
  );
}

void _validateSummary(OwnedMicDrainSummary s) {
  if (s.request.transactionId.trim().isEmpty ||
      !s.converterReachedEos ||
      s.copyFailures != 0 ||
      s.admittedInputFrames < 0 ||
      s.admittedTapBuffers < 0 ||
      s.emittedChunks < 0 ||
      s.emittedValidPcmBytes < 0 ||
      s.emittedValidPcmBytes.isOdd ||
      s.eosResidualValidPcmBytes < 0 ||
      s.eosResidualValidPcmBytes.isOdd ||
      s.eosResidualValidPcmBytes > s.emittedValidPcmBytes ||
      s.lastSequence != s.emittedChunks - 1 ||
      !RegExp(r'^[0-9a-f]{64}$').hasMatch(s.emittedPcmSha256))
    throw StateError('owned_private_summary_invalid');
}
