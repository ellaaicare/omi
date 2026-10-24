import 'dart:async';
import 'dart:convert';
import 'package:crypto/crypto.dart';
import 'package:flutter/services.dart';
import 'package:uuid/uuid.dart';
import '../audio_capture_ownership.dart';
import 'owned_capture_contract.dart';
import 'owned_phone_mic.g.dart';

class _DigestResult implements Sink<Digest> {
  Digest? value;
  @override
  void add(Digest data) => value = data;
  @override
  void close() {}
}

class _FrameIdentity {
  _FrameIdentity(this.length, this.digest);
  final int length;
  final String digest;
}

/// One immutable capture window. This controller has no default durable sink
/// and performs no ServiceManager/native registration or microphone enablement.
class OwnedCaptureController extends OwnedPhoneMicSinkApi {
  OwnedCaptureController(
      {required this.context,
      required this.native,
      required OwnedCapturePrivateSinkFactory sinkFactory,
      required this.requestPermission,
      AudioCaptureOwnership? ownership,
      this.operationTimeout = const Duration(seconds: 12)})
      : _factory = sinkFactory,
        _ownership = ownership ?? AudioCaptureOwnership.shared {
    _pcmHash = sha256.startChunkedConversion(_digest);
  }
  final OwnedCaptureContext context;
  final PreparedOwnedNativeApi native;
  final Future<bool> Function() requestPermission;
  final OwnedCapturePrivateSinkFactory _factory;
  final AudioCaptureOwnership _ownership;
  final Duration operationTimeout;
  final _DigestResult _digest = _DigestResult();
  late final ByteConversionSink _pcmHash;
  final List<_FrameIdentity> _frames = [];
  int _validBytes = 0;
  AudioCaptureLease? _lease;
  OwnedCapturePrivateSink? _sink;
  OwnedMicHandle? _handle;
  OwnedRetentionSnapshot? _appliedPolicy, _pendingCommitPolicy;
  String? _token, _transaction;
  Future<void>? _preparing, _startingNative, _startWork;
  Future<void>? _stopWork;
  bool _reservationIssued = false, _nativePrepareAttempted = false, _nativeStartAttempted = false;
  bool _nativeStartSettled = false;
  bool _unknownStart = false, _publicationActive = false, _hashClosed = false;
  bool _stopTimedOut = false;
  OwnedMicDrainSummary? _summary;
  OwnedMicDurableAck? _localDurable, _confirmedDurable;
  Future<OwnedMicStagedAck>? _stageWork;
  _FrameIdentity? _stageIdentity;
  int? _stageSequence;
  void Function(Uint8List)? _onBytes;
  void Function()? _onRecording, _onInitializing;
  static OwnedCaptureController? _channelOwner;

  bool get hasReservation => _lease?.isOwner == true;
  bool get isCurrent => _lease?.isCurrent == true && _authority();
  bool _authority() {
    try {
      return context.hasExactAuthority();
    } catch (_) {
      return false;
    }
  }

  OwnedRetentionSnapshot _policy() {
    final policy = context.retention();
    if (policy.generation < 0 || policy.generation >= 9223372036854775807) throw StateError('owned_policy_invalid');
    return policy;
  }

  OwnedRetentionSnapshot _ackPolicy() {
    final policy = _policy();
    if (!hasReservation ||
        _appliedPolicy == null ||
        !_appliedPolicy!.matches(policy) ||
        policy.retention == OwnedPrivateRetention.forbidden ||
        (!_authority() && policy.retention != OwnedPrivateRetention.quarantine))
      throw StateError('owned_retention_unconfirmed');
    return policy;
  }

  void _samePolicy(OwnedRetentionSnapshot expected) {
    if (!expected.matches(_ackPolicy())) throw StateError('owned_policy_changed');
  }

  void _requireStartAuthority() {
    if (!isCurrent || _ackPolicy().retention != OwnedPrivateRetention.retain) throw StateError('owned_start_retired');
  }

  /// Called synchronously by the arbiter preflight before permission awaits.
  void adoptLease(AudioCaptureLease lease) {
    if (_reservationIssued || !lease.isCurrent || !_authority()) throw StateError('owned_capture_admission_invalid');
    _reservationIssued = true;
    _lease = lease;
    lease.addStopAndConfirm(_stopOwned);
    final sink = _factory.prepare(context);
    if (!identical(sink.context, context)) throw StateError('owned_sink_context_mismatch');
    _sink = sink;
  }

  Future<void> start(
      {AudioCaptureLease? lease,
      required void Function(Uint8List) onByteReceived,
      void Function()? onRecording,
      void Function()? onInitializing,
      BinaryMessenger? messenger}) {
    if (_startWork != null) return Future.error(StateError('owned_capture_start_already_issued'));
    if (_lease == null) {
      final owner = lease ?? _ownership.reserve(source: 'owned_phone_capture', hasAuthority: _authority);
      if (owner == null) return Future.error(StateError('owned_capture_busy'));
      try {
        adoptLease(owner);
      } catch (error, stack) {
        return Future.error(error, stack);
      }
    } else if (lease != null && !identical(lease, _lease)) {
      return Future.error(StateError('owned_capture_foreign_lease'));
    }
    _onBytes = onByteReceived;
    _onRecording = onRecording;
    _onInitializing = onInitializing;
    _publicationActive = true;
    _channelOwner = this;
    OwnedPhoneMicSinkApi.setUp(this, binaryMessenger: messenger);
    final receipt = Completer<void>();
    _startWork = receipt.future;
    Future<void>.sync(_startBody).then(receipt.complete, onError: receipt.completeError);
    return receipt.future.timeout(operationTimeout);
  }

  Future<void> _startBody() async {
    final owner = _lease!;
    final sink = _sink!;
    final transport = owner.createTransport();
    // Its receipt covers prepared ownership, never an invented physical start.
    final preparing = transport.start(
        start: () async {
          final policy = _policy();
          if (policy.retention != OwnedPrivateRetention.retain || !_authority())
            throw StateError('owned_initial_retention_refused');
          final capabilities = await native.getCapabilities();
          if (!capabilities.supported || !_authority() || !_policy().matches(policy) || !owner.isCurrent)
            throw StateError('owned_capabilities_unqualified');
          await sink.prepare();
          _token = sink.storeToken;
          if (_token!.trim().isEmpty) throw StateError('owned_store_token_invalid');
          _appliedPolicy = policy;
          _samePolicy(policy);
          if (!owner.isCurrent) return;
          _nativePrepareAttempted = true;
          final prepared = copyOwnedHandle(await native.prepareOwnedStream(context.binding));
          _validateHandle(prepared);
          _handle = prepared;
          // Even retired preparation keeps/binds its exact old local store so
          // explicit cancellation can prove no factory entered, without fake EOS.
          await sink.bindEpoch(copyOwnedHandle(prepared));
          _samePolicy(policy);
        },
        stopAndConfirm: _stopOwned);
    _preparing = preparing;
    await preparing;
    if (!owner.isCurrent) throw StateError('owned_start_retired');
    _requireStartAuthority();
    _notify(_onInitializing);
    _requireStartAuthority();
    final granted = await requestPermission();
    if (!owner.isCurrent) throw StateError('owned_start_retired');
    _requireStartAuthority();
    if (!granted) throw StateError('owned_permission_denied');
    final handle = _handle;
    if (handle == null) throw StateError('owned_preparation_unknown');
    final started = Completer<void>();
    _startingNative = started.future;
    _nativeStartAttempted = true;
    Future<void>.sync(() => native.startPreparedOwnedStream(copyOwnedHandle(handle), _token!)).then((_) {
      _nativeStartSettled = true;
      started.complete();
    }, onError: (Object error, StackTrace stack) {
      _nativeStartSettled = true;
      _unknownStart = !_closedStartRecovery(error, handle);
      started.completeError(error, stack);
    });
    await started.future;
    _unknownStart = false;
    if (!owner.isCurrent) throw StateError('owned_start_retired');
    _requireStartAuthority();
    final probe = copyOwnedProbe(await native.probeOwnedCapture(copyOwnedHandle(handle)));
    _requireStartAuthority();
    if (!sameOwnedHandle(probe.handle, handle) ||
        !probe.engineRunning ||
        !probe.tapInstalled ||
        probe.failureCode != null ||
        probe.phase != OwnedMicDrainPhase.running) throw StateError('owned_running_probe_invalid');
    if (_publicationActive) _notify(_onRecording);
    _requireStartAuthority();
  }

  void _validateHandle(OwnedMicHandle handle) {
    if (handle.nativeEpoch <= 0 ||
        handle.nativeEpoch >= 9223372036854775807 ||
        !sameOwnedBinding(handle.binding, context.binding)) throw StateError('owned_handle_mismatch');
  }

  bool _closedStartRecovery(Object error, OwnedMicHandle pending) {
    if (error is! PlatformException || error.code != 'owned-start-held' || error.details is! Map) return false;
    final d = error.details as Map;
    return d.length == 2 &&
        d['captureStartId'] is String &&
        d['captureStartId'] == pending.binding.captureStartId &&
        d['nativeEpoch'] is int &&
        d['nativeEpoch'] == pending.nativeEpoch &&
        pending.nativeEpoch > 0;
  }

  Future<void> stop() {
    _publicationActive = false;
    _onBytes = null;
    _onRecording = null;
    _onInitializing = null;
    return _lease?.stop().then((_) {
          if (identical(_channelOwner, this)) {
            _channelOwner = null;
            OwnedPhoneMicSinkApi.setUp(null);
          }
        }) ??
        Future<void>.value();
  }

  Future<void> retirePrivateRetention(OwnedRetentionSnapshot policy) async {
    _publicationActive = false;
    if (policy.retention == OwnedPrivateRetention.retain ||
        _appliedPolicy == null ||
        policy.generation <= _appliedPolicy!.generation) {
      throw StateError('owned_retirement_invalid');
    }
    if (!_policy().matches(policy)) throw StateError('owned_retirement_stale');
    final result = await _sink!.retirePrivateRetention(policy);
    if (!result.matches(policy) || !_policy().matches(policy)) throw StateError('owned_retirement_unconfirmed');
    _appliedPolicy = policy;
  }

  Future<void> _stopOwned() {
    _publicationActive = false;
    final active = _stopWork;
    if (active != null) return active;
    _stopTimedOut = false;
    final receipt = Completer<void>();
    _stopWork = receipt.future;
    Future<void>.sync(_stopBody).then((_) {
      _stopWork = null;
      receipt.complete();
    }, onError: (Object error, StackTrace stack) {
      _stopWork = null;
      receipt.completeError(error, stack);
    });
    return receipt.future.timeout(operationTimeout, onTimeout: () {
      _stopTimedOut = true;
      unawaited(_observeExactCleanupProbe());
      throw StateError('owned_stop_deadline_requires_exact_retry');
    });
  }

  Future<void> _stopBody() async {
    final preparing = _preparing;
    if (preparing != null) {
      try {
        await preparing;
      } catch (_) {}
    }
    if (!_nativePrepareAttempted) return;
    final handle = _handle;
    if (handle == null) throw StateError('owned_preparation_identity_unknown');
    final pendingStart = _startingNative;
    final needsPostStartCleanup = pendingStart != null && !_nativeStartSettled;
    // A known prepared-origin handle authorizes exact physical cleanup even
    // when retention refuses every storage ACK and logical release.
    OwnedRetentionSnapshot? policy;
    try {
      policy = _ackPolicy();
    } catch (_) {/* Cleanup is still owned. */}
    if (!_nativeStartAttempted) {
      final cancel = await native.cancelPreparedOwnedStream(copyOwnedHandle(handle), _token!);
      if (!sameOwnedHandle(cancel.handle, handle) || cancel.storeToken != _token || !cancel.cancelledBeforeStart)
        throw StateError('owned_prepared_cancel_unconfirmed');
      if (policy == null) throw StateError('owned_release_policy_unconfirmed');
      _samePolicy(policy);
      return;
    }
    if (_confirmedDurable != null && !_unknownStart) {
      if (policy == null) throw StateError('owned_release_policy_unconfirmed');
      _validateAck(_confirmedDurable!, _summary!, policy);
      return;
    }
    _transaction ??= const Uuid().v4();
    final request = OwnedMicStopRequest(handle: copyOwnedHandle(handle), transactionId: _transaction!);
    Object? firstCleanupFailure;
    StackTrace? firstCleanupStack;
    if (needsPostStartCleanup) {
      // Issue cleanup before waiting for a held start ACK. Its receipt is not
      // eligible for publication/commit/release while startup is unsettled.
      try {
        await _requestExactDrain(request);
      } catch (error, stack) {
        firstCleanupFailure = error;
        firstCleanupStack = stack;
      }
      try {
        await pendingStart;
      } catch (_) {/* Exact cleanup still required. */}
      // A late startup could have produced input after the first teardown.
      // The same transaction must obtain another actual stop and fresh probe.
    }
    OwnedMicStopReceipt raw;
    try {
      raw = await _requestExactDrain(request);
    } catch (error, stack) {
      if (firstCleanupFailure != null) Error.throwWithStackTrace(firstCleanupFailure, firstCleanupStack!);
      Error.throwWithStackTrace(error, stack);
    }
    final summary = copyOwnedSummary(raw.summary),
        ack = copyOwnedDurable(raw.durableAck),
        physical = copyOwnedProbe(raw.physicalProbe);
    final fresh = copyOwnedProbe(await native.probeOwnedCapture(copyOwnedHandle(handle)));
    if (_unknownStart) throw StateError('owned_start_identity_unreconciled');
    if (policy == null) throw StateError('owned_release_policy_unconfirmed');
    _samePolicy(policy);
    _validateSummary(summary);
    _validateAck(ack, summary, policy);
    _validateProbe(physical, request);
    final local = _localDurable;
    if (local == null || !_sameAck(local, ack)) throw StateError('owned_local_durable_receipt_missing');
    _samePolicy(policy);
    _validateProbe(fresh, request);
    if (_stopTimedOut) throw StateError('owned_late_receipt_requires_exact_retry');
    _confirmedDurable = copyOwnedDurable(ack);
  }

  Future<OwnedMicStopReceipt> _requestExactDrain(OwnedMicStopRequest request) async {
    try {
      return await native.stopAndDrain(copyOwnedRequest(request));
    } catch (_) {
      await _observeExactCleanupProbe();
      rethrow;
    }
  }

  Future<void> _observeExactCleanupProbe() async {
    final handle = _handle;
    if (!hasReservation || handle == null || !_nativeStartAttempted) return;
    try {
      await native.probeOwnedCapture(copyOwnedHandle(handle)).timeout(operationTimeout);
    } catch (_) {
      // This bounded observation cannot acknowledge release, restart, native
      // silence, EOS, durability, or replace the original teardown failure.
    }
  }

  @override
  Future<OwnedMicStagedAck> stageOwnedPcm(OwnedMicFrame incoming) {
    final frame = copyOwnedFrame(incoming);
    final handle = _handle;
    if (!hasReservation ||
        handle == null ||
        !sameOwnedHandle(frame.handle, handle) ||
        !_nativeStartAttempted ||
        frame.sequence < 0 ||
        frame.sequence >= 65536 ||
        frame.pcmBytes.length > 2097152 ||
        frame.pcmBytes.isEmpty ||
        frame.pcmBytes.length.isOdd) return Future.error(StateError('owned_frame_invalid'));
    final identity = _FrameIdentity(frame.pcmBytes.length, sha256.convert(frame.pcmBytes).toString());
    final pending = _stageWork;
    if (pending != null) {
      if (_stageSequence != frame.sequence ||
          _stageIdentity!.length != identity.length ||
          _stageIdentity!.digest != identity.digest) {
        return Future.error(StateError('owned_frame_pending_conflict'));
      }
      OwnedRetentionSnapshot joinedPolicy;
      try {
        joinedPolicy = _ackPolicy();
      } catch (error, stack) {
        return Future.error(error, stack);
      }
      return pending.then((ack) {
        _samePolicy(joinedPolicy);
        if (!sameOwnedHandle(ack.handle, frame.handle) ||
            ack.sequence != frame.sequence ||
            ack.acceptedValidPcmBytes != identity.length) throw StateError('owned_joined_ack_invalid');
        return OwnedMicStagedAck(
            handle: copyOwnedHandle(ack.handle),
            sequence: ack.sequence,
            acceptedValidPcmBytes: ack.acceptedValidPcmBytes);
      });
    }
    if (frame.sequence > _frames.length || (_hashClosed && frame.sequence == _frames.length))
      return Future.error(StateError('owned_frame_order_invalid'));
    final result = Completer<OwnedMicStagedAck>();
    OwnedRetentionSnapshot? stagePolicy;
    _stageWork = result.future;
    _stageSequence = frame.sequence;
    _stageIdentity = identity;
    Future<OwnedMicStagedAck>.sync(() async {
      final policy = _ackPolicy();
      stagePolicy = policy;
      if (frame.sequence < _frames.length) {
        final previous = _frames[frame.sequence];
        if (previous.length != identity.length || previous.digest != identity.digest)
          throw StateError('owned_frame_duplicate_conflict');
      }
      final ack = await _sink!.stageOwnedPcm(copyOwnedFrame(frame));
      final captured = OwnedMicStagedAck(
          handle: copyOwnedHandle(ack.handle),
          sequence: ack.sequence,
          acceptedValidPcmBytes: ack.acceptedValidPcmBytes);
      _samePolicy(policy);
      if (!sameOwnedHandle(captured.handle, handle) ||
          captured.sequence != frame.sequence ||
          captured.acceptedValidPcmBytes != identity.length) {
        throw StateError('owned_staged_ack_invalid');
      }
      if (frame.sequence == _frames.length) {
        _pcmHash.add(frame.pcmBytes);
        _validBytes += identity.length;
        _frames.add(identity);
        if (_publicationActive && isCurrent) {
          try {
            _onBytes?.call(Uint8List.fromList(frame.pcmBytes));
          } catch (_) {/* Observer never authorizes or blocks local ACK. */}
        }
      }
      _samePolicy(policy); // Observers can synchronously retire this exact authority.
      return captured;
    }).then((ack) {
      _stageWork = null;
      try {
        _samePolicy(stagePolicy!);
        result.complete(ack);
      } catch (error, stack) {
        result.completeError(error, stack);
      }
    }, onError: (Object e, StackTrace st) {
      _stageWork = null;
      result.completeError(e, st);
    });
    // The singleflight future remains private: every outward caller receives
    // an independent nested snapshot, including the first caller.
    return result.future.then((ack) {
      _samePolicy(stagePolicy!);
      if (!sameOwnedHandle(ack.handle, frame.handle) ||
          ack.sequence != frame.sequence ||
          ack.acceptedValidPcmBytes != identity.length) throw StateError('owned_staged_return_invalid');
      return OwnedMicStagedAck(
          handle: copyOwnedHandle(ack.handle),
          sequence: ack.sequence,
          acceptedValidPcmBytes: ack.acceptedValidPcmBytes);
    });
  }

  @override
  Future<OwnedMicDurableAck> commitOwnedDrain(OwnedMicDrainSummary incoming) async {
    final summary = copyOwnedSummary(incoming);
    if (_nativeStartAttempted && !_nativeStartSettled) throw StateError('owned_start_settlement_pending');
    final policy = _ackPolicy();
    _validateSummary(summary);
    _summary ??= copyOwnedSummary(summary);
    if (!_sameSummary(_summary!, summary)) throw StateError('owned_summary_changed');
    if (_pendingCommitPolicy != null && !_pendingCommitPolicy!.matches(policy))
      throw StateError('owned_pending_commit_retirement_unresolved');
    final prior = _localDurable;
    _pendingCommitPolicy = policy;
    final ack = copyOwnedDurable(await _sink!.commitOwnedDrain(copyOwnedSummary(summary),
        policy: policy,
        previousCommitId: prior?.disposition == OwnedMicSinkDisposition.retainedExactBinding &&
                policy.retention == OwnedPrivateRetention.quarantine
            ? prior!.commitId
            : null));
    _validateAck(ack, summary, policy);
    if (prior != null &&
        prior.disposition == OwnedMicSinkDisposition.retainedExactBinding &&
        policy.retention == OwnedPrivateRetention.quarantine &&
        (ack.commitId == prior.commitId || ack.previousCommitId != prior.commitId))
      throw StateError('owned_quarantine_link_invalid');
    if (prior != null && prior.disposition == ack.disposition && !_sameAck(prior, ack))
      throw StateError('owned_commit_identity_changed');
    _samePolicy(policy);
    _localDurable = copyOwnedDurable(ack);
    _pendingCommitPolicy = null;
    return copyOwnedDurable(ack);
  }

  void _validateSummary(OwnedMicDrainSummary s) {
    final h = _handle;
    if ((_nativeStartAttempted && !_nativeStartSettled) ||
        _stageWork != null ||
        h == null ||
        _transaction == null ||
        !sameOwnedRequest(s.request, OwnedMicStopRequest(handle: h, transactionId: _transaction!)) ||
        !s.converterReachedEos ||
        s.copyFailures != 0 ||
        s.admittedInputFrames < 0 ||
        s.admittedTapBuffers < 0 ||
        s.eosResidualValidPcmBytes < 0 ||
        s.eosResidualValidPcmBytes.isOdd ||
        s.eosResidualValidPcmBytes > _validBytes ||
        s.lastSequence != _frames.length - 1 ||
        s.emittedChunks != _frames.length ||
        s.emittedValidPcmBytes != _validBytes) throw StateError('owned_summary_invalid');
    if (!_hashClosed) {
      _hashClosed = true;
      _pcmHash.close();
    }
    if (s.emittedPcmSha256 != _digest.value!.toString()) throw StateError('owned_digest_mismatch');
  }

  void _validateAck(OwnedMicDurableAck a, OwnedMicDrainSummary s, OwnedRetentionSnapshot policy) {
    final expected = policy.retention == OwnedPrivateRetention.quarantine
        ? OwnedMicSinkDisposition.quarantinedExactBinding
        : OwnedMicSinkDisposition.retainedExactBinding;
    if (!sameOwnedRequest(a.request, s.request) ||
        a.lastSequence != s.lastSequence ||
        a.durableChunks != s.emittedChunks ||
        a.durableValidPcmBytes != s.emittedValidPcmBytes ||
        a.pcmSha256 != s.emittedPcmSha256 ||
        a.commitId.trim().isEmpty ||
        (expected == OwnedMicSinkDisposition.retainedExactBinding && a.previousCommitId != null) ||
        a.disposition != expected) throw StateError('owned_durable_ack_invalid');
  }

  void _validateProbe(OwnedMicProbe p, OwnedMicStopRequest r) {
    if (!sameOwnedHandle(p.handle, r.handle) ||
        p.transactionId != r.transactionId ||
        p.phase != OwnedMicDrainPhase.confirmed ||
        p.engineRunning ||
        p.tapInstalled ||
        p.producerCopiesInFlight != 0 ||
        p.frameAcksInFlight != 0 ||
        !p.converterReachedEos ||
        p.failureCode != null) throw StateError('owned_physical_probe_invalid');
  }

  bool _sameAck(OwnedMicDurableAck a, OwnedMicDurableAck b) =>
      sameOwnedRequest(a.request, b.request) &&
      a.lastSequence == b.lastSequence &&
      a.durableChunks == b.durableChunks &&
      a.durableValidPcmBytes == b.durableValidPcmBytes &&
      a.pcmSha256 == b.pcmSha256 &&
      a.commitId == b.commitId &&
      a.disposition == b.disposition &&
      a.previousCommitId == b.previousCommitId;
  bool _sameSummary(OwnedMicDrainSummary a, OwnedMicDrainSummary b) =>
      sameOwnedRequest(a.request, b.request) &&
      a.admittedInputFrames == b.admittedInputFrames &&
      a.admittedTapBuffers == b.admittedTapBuffers &&
      a.copyFailures == b.copyFailures &&
      a.lastSequence == b.lastSequence &&
      a.emittedChunks == b.emittedChunks &&
      a.emittedValidPcmBytes == b.emittedValidPcmBytes &&
      a.emittedPcmSha256 == b.emittedPcmSha256 &&
      a.eosResidualValidPcmBytes == b.eosResidualValidPcmBytes &&
      a.converterReachedEos == b.converterReachedEos;
  void _notify(void Function()? observer) {
    try {
      observer?.call();
    } catch (_) {/* Metadata observer only. */}
  }
}
