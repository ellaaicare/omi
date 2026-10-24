// Additive inactive proposal. No generated legacy channel is edited.
// Opaque binding IDs are not account UIDs, credentials, or diagnostic payloads.
import 'package:pigeon/pigeon.dart';

@ConfigurePigeon(PigeonOptions(
    swiftOptions: SwiftOptions(errorClassName: 'OwnedMicPigeonError', fileSpecificClassNameComponent: 'OwnedPhoneMic')))
enum OwnedMicSinkDisposition { retainedExactBinding, quarantinedExactBinding }

enum OwnedMicDrainPhase {
  running,
  quiescing,
  drainingConverter,
  awaitingStagedAcks,
  awaitingDurableAck,
  confirmed,
  ambiguous,
}

class OwnedMicBinding {
  OwnedMicBinding({
    required this.captureStartId,
    required this.authorityBindingId,
    required this.authorityGeneration,
    required this.localSinkId,
    required this.sessionId,
    required this.deviceBindingId,
    required this.sourceId,
    required this.pcmCodecId,
    required this.sampleRate,
    required this.channels,
    required this.bytesPerSample,
  });
  String captureStartId;
  String authorityBindingId;
  int authorityGeneration;
  String localSinkId;
  int sessionId;
  String deviceBindingId;
  String sourceId;
  String pcmCodecId;
  int sampleRate;
  int channels;
  int bytesPerSample;
}

class OwnedMicHandle {
  OwnedMicHandle({required this.binding, required this.nativeEpoch});
  OwnedMicBinding binding;
  int nativeEpoch;
}

class OwnedMicFrame {
  OwnedMicFrame({required this.handle, required this.sequence, required this.pcmBytes});
  OwnedMicHandle handle;
  int sequence;
  Uint8List pcmBytes;
}

class OwnedMicStagedAck {
  OwnedMicStagedAck({required this.handle, required this.sequence, required this.acceptedValidPcmBytes});
  OwnedMicHandle handle;
  int sequence;
  int acceptedValidPcmBytes;
}

class OwnedMicStopRequest {
  OwnedMicStopRequest({required this.handle, required this.transactionId});
  OwnedMicHandle handle;
  String transactionId;
}

class OwnedMicDrainSummary {
  OwnedMicDrainSummary({
    required this.request,
    required this.admittedInputFrames,
    required this.admittedTapBuffers,
    required this.copyFailures,
    required this.lastSequence,
    required this.emittedChunks,
    required this.emittedValidPcmBytes,
    required this.emittedPcmSha256,
    required this.eosResidualValidPcmBytes,
    required this.converterReachedEos,
  });
  OwnedMicStopRequest request;
  int admittedInputFrames;
  int admittedTapBuffers;
  int copyFailures;
  int lastSequence;
  int emittedChunks;
  int emittedValidPcmBytes;
  String emittedPcmSha256;
  int eosResidualValidPcmBytes;
  bool converterReachedEos;
}

class OwnedMicDurableAck {
  OwnedMicDurableAck({
    required this.request,
    required this.lastSequence,
    required this.durableChunks,
    required this.durableValidPcmBytes,
    required this.pcmSha256,
    required this.commitId,
    required this.disposition,
    this.previousCommitId,
  });
  OwnedMicStopRequest request;
  int lastSequence;
  int durableChunks;
  int durableValidPcmBytes;
  String pcmSha256;
  String commitId;
  OwnedMicSinkDisposition disposition;
  String? previousCommitId;
}

class OwnedMicProbe {
  OwnedMicProbe({
    required this.handle,
    required this.transactionId,
    required this.phase,
    required this.engineRunning,
    required this.tapInstalled,
    required this.producerCopiesInFlight,
    required this.frameAcksInFlight,
    required this.converterReachedEos,
    required this.failureCode,
  });
  OwnedMicHandle handle;
  String? transactionId;
  OwnedMicDrainPhase phase;
  bool engineRunning;
  bool tapInstalled;
  int producerCopiesInFlight;
  int frameAcksInFlight;
  bool converterReachedEos;
  String? failureCode;
}

class OwnedMicStopReceipt {
  OwnedMicStopReceipt({required this.summary, required this.durableAck, required this.physicalProbe});
  OwnedMicDrainSummary summary;
  OwnedMicDurableAck durableAck;
  OwnedMicProbe physicalProbe;
}

@HostApi()
abstract class OwnedPhoneMicHostApi {
  /// Rejects another live binding. Retries are idempotent only for exact binding.
  @async
  OwnedMicHandle startOwnedStream(OwnedMicBinding binding);

  /// Returns only on engine/tap absence, EOS, contiguous staged ACKs and exact
  /// durable commit. Errors/timeouts preserve transaction for probe/exact retry.
  @async
  OwnedMicStopReceipt stopAndDrain(OwnedMicStopRequest request);
  @async
  OwnedMicProbe probeOwnedCapture(OwnedMicHandle handle);
}

@FlutterApi()
abstract class OwnedPhoneMicSinkApi {
  /// Staged ACK alone never releases capture ownership or proves disk durability.
  @async
  OwnedMicStagedAck stageOwnedPcm(OwnedMicFrame frame);

  /// Runs after every contiguous frame ACK. Root's exact local sink flushes its
  /// partial source/encoder valid PCM and commits under the immutable binding.
  /// Revoked authority can quarantine only under that binding if policy permits;
  /// forbidden retention throws, never invents a durable/no-loss ACK.
  @async
  OwnedMicDurableAck commitOwnedDrain(OwnedMicDrainSummary summary);
}
