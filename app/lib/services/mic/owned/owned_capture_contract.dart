import 'dart:typed_data';
import 'owned_phone_mic.g.dart';

enum OwnedPrivateRetention { retain, quarantine, forbidden }

class OwnedRetentionSnapshot {
  const OwnedRetentionSnapshot(this.generation, this.retention);
  final int generation;
  final OwnedPrivateRetention retention;
  bool matches(OwnedRetentionSnapshot other) => generation == other.generation && retention == other.retention;
}

/// Captured once before permission or native work. Callbacks must verify the
/// exact account/authority/window epochs, never just a reusable UID label.
class OwnedCaptureContext {
  OwnedCaptureContext({
    required OwnedMicBinding binding,
    required this.accountBindingId,
    required this.windowBindingId,
    required this.hasExactAuthority,
    required this.retention,
  }) : _binding = copyOwnedBinding(binding) {
    final b = _binding;
    if ([accountBindingId, windowBindingId, b.captureStartId, b.authorityBindingId, b.localSinkId, b.deviceBindingId]
            .any((v) => v.trim().isEmpty) ||
        b.authorityGeneration < 0 ||
        b.authorityGeneration >= 9223372036854775807 ||
        b.sessionId <= 0 ||
        b.sessionId >= 9223372036854775807 ||
        b.sourceId != 'phone' ||
        b.pcmCodecId != 'pcm16' ||
        b.sampleRate != 16000 ||
        b.channels != 1 ||
        b.bytesPerSample != 2) {
      throw StateError('owned_binding_invalid');
    }
  }
  final OwnedMicBinding _binding;
  final String accountBindingId, windowBindingId;
  final bool Function() hasExactAuthority;
  final OwnedRetentionSnapshot Function() retention;
  OwnedMicBinding get binding => copyOwnedBinding(_binding);
}

/// Root supplies an actual descriptor-backed transport. There is no default
/// implementation, path-based fallback, or callback claiming disk durability.
abstract interface class OwnedCapturePrivateSinkFactory {
  OwnedCapturePrivateSink prepare(OwnedCaptureContext capturedContext);
}

abstract interface class OwnedCapturePrivateSink {
  OwnedCaptureContext get context;
  String get storeToken;
  Future<void> prepare();
  Future<void> bindEpoch(OwnedMicHandle handle);
  Future<OwnedMicStagedAck> stageOwnedPcm(OwnedMicFrame frame);
  Future<OwnedMicDurableAck> commitOwnedDrain(OwnedMicDrainSummary summary,
      {required OwnedRetentionSnapshot policy, String? previousCommitId});
  Future<OwnedRetentionSnapshot> retirePrivateRetention(OwnedRetentionSnapshot policy);
}

abstract interface class PreparedOwnedNativeApi {
  Future<OwnedPreparedCapabilities> getCapabilities();
  Future<OwnedMicHandle> prepareOwnedStream(OwnedMicBinding binding);
  Future<void> startPreparedOwnedStream(OwnedMicHandle handle, String storeToken);
  Future<OwnedMicStopReceipt> stopAndDrain(OwnedMicStopRequest request);
  Future<OwnedMicProbe> probeOwnedCapture(OwnedMicHandle handle);
  Future<OwnedPreparedCancelReceipt> cancelPreparedOwnedStream(OwnedMicHandle handle, String storeToken);
}

class OwnedPreparedCancelReceipt {
  const OwnedPreparedCancelReceipt(
      {required this.handle, required this.storeToken, required this.cancelledBeforeStart});
  final OwnedMicHandle handle;
  final String storeToken;
  final bool cancelledBeforeStart;
}

class OwnedPreparedCapabilities {
  const OwnedPreparedCapabilities(
      {required this.preparedLifecycle,
      required this.privateStore,
      required this.retirement,
      required this.neverStartedCancel});
  final bool preparedLifecycle, privateStore, retirement, neverStartedCancel;
  bool get supported => preparedLifecycle && privateStore && retirement && neverStartedCancel;
}

OwnedMicBinding copyOwnedBinding(OwnedMicBinding b) => OwnedMicBinding(
    captureStartId: b.captureStartId,
    authorityBindingId: b.authorityBindingId,
    authorityGeneration: b.authorityGeneration,
    localSinkId: b.localSinkId,
    sessionId: b.sessionId,
    deviceBindingId: b.deviceBindingId,
    sourceId: b.sourceId,
    pcmCodecId: b.pcmCodecId,
    sampleRate: b.sampleRate,
    channels: b.channels,
    bytesPerSample: b.bytesPerSample);
OwnedMicHandle copyOwnedHandle(OwnedMicHandle h) =>
    OwnedMicHandle(binding: copyOwnedBinding(h.binding), nativeEpoch: h.nativeEpoch);
OwnedMicStopRequest copyOwnedRequest(OwnedMicStopRequest r) =>
    OwnedMicStopRequest(handle: copyOwnedHandle(r.handle), transactionId: r.transactionId);
OwnedMicFrame copyOwnedFrame(OwnedMicFrame f) =>
    OwnedMicFrame(handle: copyOwnedHandle(f.handle), sequence: f.sequence, pcmBytes: Uint8List.fromList(f.pcmBytes));
OwnedMicDrainSummary copyOwnedSummary(OwnedMicDrainSummary s) => OwnedMicDrainSummary(
    request: copyOwnedRequest(s.request),
    admittedInputFrames: s.admittedInputFrames,
    admittedTapBuffers: s.admittedTapBuffers,
    copyFailures: s.copyFailures,
    lastSequence: s.lastSequence,
    emittedChunks: s.emittedChunks,
    emittedValidPcmBytes: s.emittedValidPcmBytes,
    emittedPcmSha256: s.emittedPcmSha256,
    eosResidualValidPcmBytes: s.eosResidualValidPcmBytes,
    converterReachedEos: s.converterReachedEos);
OwnedMicDurableAck copyOwnedDurable(OwnedMicDurableAck a) => OwnedMicDurableAck(
    request: copyOwnedRequest(a.request),
    lastSequence: a.lastSequence,
    durableChunks: a.durableChunks,
    durableValidPcmBytes: a.durableValidPcmBytes,
    pcmSha256: a.pcmSha256,
    commitId: a.commitId,
    disposition: a.disposition,
    previousCommitId: a.previousCommitId);
OwnedMicProbe copyOwnedProbe(OwnedMicProbe p) => OwnedMicProbe(
    handle: copyOwnedHandle(p.handle),
    transactionId: p.transactionId,
    phase: p.phase,
    engineRunning: p.engineRunning,
    tapInstalled: p.tapInstalled,
    producerCopiesInFlight: p.producerCopiesInFlight,
    frameAcksInFlight: p.frameAcksInFlight,
    converterReachedEos: p.converterReachedEos,
    failureCode: p.failureCode);
bool sameOwnedBinding(OwnedMicBinding a, OwnedMicBinding b) {
  final x = a.encode() as List<Object?>, y = b.encode() as List<Object?>;
  return x.length == y.length && List.generate(x.length, (i) => x[i] == y[i]).every((v) => v);
}

bool sameOwnedHandle(OwnedMicHandle a, OwnedMicHandle b) =>
    a.nativeEpoch == b.nativeEpoch && sameOwnedBinding(a.binding, b.binding);
bool sameOwnedRequest(OwnedMicStopRequest a, OwnedMicStopRequest b) =>
    a.transactionId == b.transactionId && sameOwnedHandle(a.handle, b.handle);
