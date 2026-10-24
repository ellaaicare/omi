import 'dart:async';
import 'package:crypto/crypto.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';
import 'package:omi/services/mic/owned/owned_capture_contract.dart';
import 'package:omi/services/mic/owned/owned_capture_controller.dart';
import 'package:omi/services/mic/owned/owned_phone_mic.g.dart';

OwnedMicBinding binding() => OwnedMicBinding(
    captureStartId: 'capture-A',
    authorityBindingId: 'authority-A',
    authorityGeneration: 3,
    localSinkId: 'sink-A',
    sessionId: 7,
    deviceBindingId: 'device-A',
    sourceId: 'phone',
    pcmCodecId: 'pcm16',
    sampleRate: 16000,
    channels: 1,
    bytesPerSample: 2);
Future<void> settle() async {
  for (var i = 0; i < 25; i++) {
    await Future<void>.delayed(Duration.zero);
  }
}

class Rig implements PreparedOwnedNativeApi {
  Rig({Duration timeout = const Duration(seconds: 2), OwnedCapturePrivateSinkFactory? factory}) {
    context = OwnedCaptureContext(
        binding: binding(),
        accountBindingId: 'account-A',
        windowBindingId: 'window-A',
        hasExactAuthority: () => authority && authorityEpoch == 3,
        retention: () => policy);
    controller = OwnedCaptureController(
        context: context,
        native: this,
        sinkFactory: factory ?? Factory(this),
        requestPermission: () {
          permissions++;
          return permission?.future ?? Future.value(true);
        },
        ownership: registry,
        operationTimeout: timeout);
  }
  late final OwnedCaptureController controller;
  late final OwnedCaptureContext context;
  final registry = AudioCaptureOwnership();
  bool authority = true;
  int authorityEpoch = 3;
  Object? prepareError;
  bool capabilitiesSupported = true;
  OwnedRetentionSnapshot policy = const OwnedRetentionSnapshot(0, OwnedPrivateRetention.retain);
  Completer<bool>? permission;
  Completer<void>? startHeld, stopHeld, commitHeld, stageHeld, prepareHeld;
  Object? startError;
  bool noLocalCommit = false, foreignProbe = false;
  int permissions = 0, starts = 0, cancels = 0, stops = 0, publications = 0, prepares = 0;
  final requests = <OwnedMicStopRequest>[];
  final bytes = <int>[];
  OwnedMicDurableAck? saved;
  OwnedMicDrainSummary? summary;
  String get storeToken => 'token-A';
  Future<void> bindEpoch(OwnedMicHandle handle) async {}
  Future<OwnedRetentionSnapshot> retirePrivateRetention(OwnedRetentionSnapshot p) async => p;
  Future<OwnedMicStagedAck> stageOwnedPcm(OwnedMicFrame f) async {
    if (stageHeld != null) await stageHeld!.future;
    if (f.sequence == 0 && bytes.isEmpty) bytes.addAll(f.pcmBytes);
    return OwnedMicStagedAck(
        handle: copyOwnedHandle(f.handle), sequence: f.sequence, acceptedValidPcmBytes: f.pcmBytes.length);
  }

  Future<OwnedMicDurableAck> commitOwnedDrain(OwnedMicDrainSummary s,
      {required OwnedRetentionSnapshot policy, String? previousCommitId}) async {
    if (commitHeld != null) await commitHeld!.future;
    return ack(s, policy, previousCommitId);
  }

  OwnedMicDurableAck ack(OwnedMicDrainSummary s, OwnedRetentionSnapshot p, String? previous) => OwnedMicDurableAck(
      request: copyOwnedRequest(s.request),
      lastSequence: s.lastSequence,
      durableChunks: s.emittedChunks,
      durableValidPcmBytes: s.emittedValidPcmBytes,
      pcmSha256: s.emittedPcmSha256,
      commitId: p.retention == OwnedPrivateRetention.retain ? 'commit-R' : 'commit-Q',
      disposition: p.retention == OwnedPrivateRetention.retain
          ? OwnedMicSinkDisposition.retainedExactBinding
          : OwnedMicSinkDisposition.quarantinedExactBinding,
      previousCommitId: previous);
  @override
  Future<OwnedPreparedCapabilities> getCapabilities() async => OwnedPreparedCapabilities(
      preparedLifecycle: true, privateStore: capabilitiesSupported, retirement: true, neverStartedCancel: true);
  @override
  Future<OwnedMicHandle> prepareOwnedStream(OwnedMicBinding b) async {
    prepares++;
    if (prepareError != null) throw prepareError!;
    if (prepareHeld != null) await prepareHeld!.future;
    return OwnedMicHandle(binding: copyOwnedBinding(b), nativeEpoch: 11);
  }

  @override
  Future<void> startPreparedOwnedStream(OwnedMicHandle h, String token) async {
    starts++;
    if (startHeld != null) await startHeld!.future;
    if (startError != null) throw startError!;
  }

  @override
  Future<OwnedPreparedCancelReceipt> cancelPreparedOwnedStream(OwnedMicHandle h, String token) async {
    cancels++;
    return OwnedPreparedCancelReceipt(handle: copyOwnedHandle(h), storeToken: token, cancelledBeforeStart: true);
  }

  OwnedMicProbe probe(OwnedMicHandle h, {String? tx}) => OwnedMicProbe(
      handle: copyOwnedHandle(h),
      transactionId: tx,
      phase: tx == null ? OwnedMicDrainPhase.running : OwnedMicDrainPhase.confirmed,
      engineRunning: tx == null,
      tapInstalled: tx == null,
      producerCopiesInFlight: 0,
      frameAcksInFlight: 0,
      converterReachedEos: tx != null,
      failureCode: null);
  @override
  Future<OwnedMicProbe> probeOwnedCapture(OwnedMicHandle h) async {
    final p = probe(h, tx: requests.isEmpty ? null : requests.last.transactionId);
    if (foreignProbe) p.handle.nativeEpoch = 12;
    return p;
  }

  @override
  Future<OwnedMicStopReceipt> stopAndDrain(OwnedMicStopRequest r) async {
    stops++;
    requests.add(copyOwnedRequest(r));
    if (stopHeld != null) await stopHeld!.future;
    final s = OwnedMicDrainSummary(
        request: copyOwnedRequest(r),
        admittedInputFrames: bytes.length ~/ 2,
        admittedTapBuffers: bytes.isEmpty ? 0 : 1,
        copyFailures: 0,
        lastSequence: bytes.isEmpty ? -1 : 0,
        emittedChunks: bytes.isEmpty ? 0 : 1,
        emittedValidPcmBytes: bytes.length,
        emittedPcmSha256: sha256.convert(bytes).toString(),
        eosResidualValidPcmBytes: 0,
        converterReachedEos: true);
    summary = s;
    final a = noLocalCommit ? ack(s, policy, null) : await controller.commitOwnedDrain(s);
    saved = a;
    return OwnedMicStopReceipt(summary: s, durableAck: a, physicalProbe: probe(r.handle, tx: r.transactionId));
  }

  Future<void> start() => controller.start(onByteReceived: (_) => publications++);
  Future<OwnedMicStagedAck> frame([int sequence = 0]) => controller.stageOwnedPcm(OwnedMicFrame(
      handle: OwnedMicHandle(binding: binding(), nativeEpoch: 11),
      sequence: sequence,
      pcmBytes: Uint8List.fromList([1, 2])));
}

class Factory implements OwnedCapturePrivateSinkFactory {
  Factory(this.rig);
  final Rig rig;
  @override
  OwnedCapturePrivateSink prepare(OwnedCaptureContext context) => SinkAdapter(rig);
}

class SinkAdapter implements OwnedCapturePrivateSink {
  SinkAdapter(this.rig);
  final Rig rig;
  @override
  OwnedCaptureContext get context => rig.context;
  String get storeToken => rig.storeToken;
  @override
  Future<void> prepare() async {}
  @override
  Future<void> bindEpoch(OwnedMicHandle h) => rig.bindEpoch(h);
  @override
  Future<OwnedMicStagedAck> stageOwnedPcm(OwnedMicFrame f) => rig.stageOwnedPcm(f);
  @override
  Future<OwnedMicDurableAck> commitOwnedDrain(OwnedMicDrainSummary s,
          {required OwnedRetentionSnapshot policy, String? previousCommitId}) =>
      rig.commitOwnedDrain(s, policy: policy, previousCommitId: previousCommitId);
  @override
  Future<OwnedRetentionSnapshot> retirePrivateRetention(OwnedRetentionSnapshot p) => rig.retirePrivateRetention(p);
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('reservation synchronous, sink bound before permission; successful exact drain releases', () async {
    final r = Rig();
    final start = r.start();
    expect(r.registry.isReserved, true);
    await start;
    await r.frame();
    expect(r.publications, 1);
    await r.controller.stop();
    expect(r.registry.isReserved, false);
    expect(r.starts, 1);
    expect(r.stops, 1);
  });
  test('held permission exact cancellation releases; late grant never starts', () async {
    final r = Rig()..permission = Completer();
    final start = r.start();
    final rejected = expectLater(start, throwsStateError);
    await settle();
    expect(r.permissions, 1);
    await r.controller.stop();
    expect(r.cancels, 2);
    expect(r.registry.isReserved, false);
    r.permission!.complete(true);
    await rejected;
    expect(r.starts, 0);
  });
  test('held prepare reserves through stop and cancels only actual returned handle', () async {
    final r = Rig()..prepareHeld = Completer();
    final start = r.start();
    final rejected = expectLater(start, throwsStateError);
    await settle();
    var done = false;
    final stop = r.controller.stop().then((_) => done = true);
    await settle();
    expect(done, false);
    expect(r.registry.isReserved, true);
    r.prepareHeld!.complete();
    await stop;
    await rejected;
    expect(r.permissions, 0);
    expect(r.cancels, 2);
  });
  test('held native start receives immediate cleanup and requires cleanup after settlement', () async {
    final r = Rig()..startHeld = Completer();
    final start = r.start();
    final rejected = expectLater(start, throwsStateError);
    await settle();
    final stop = r.controller.stop();
    await settle();
    expect(r.stops, 1);
    expect(r.registry.isReserved, true);
    r.startHeld!.complete();
    await stop;
    await rejected;
    expect(r.stops, 2);
    expect(r.registry.isReserved, false);
  });
  test('native held stop accepts private final tail without outward publication', () async {
    final r = Rig()..stopHeld = Completer();
    await r.start();
    final stop = r.controller.stop();
    await settle();
    await r.frame();
    expect(r.publications, 0);
    r.stopHeld!.complete();
    await stop;
    expect(r.bytes, [1, 2]);
    expect(r.registry.isReserved, false);
  });
  test('fresh physical proof alone cannot release without local durable acknowledgement', () async {
    final r = Rig()..noLocalCommit = true;
    await r.start();
    await expectLater(r.controller.stop(), throwsStateError);
    expect(r.registry.isReserved, true);
  });
  test('held durable acknowledgement retains physical owner until completion', () async {
    final r = Rig()..commitHeld = Completer();
    await r.start();
    var done = false;
    final stop = r.controller.stop().then((_) => done = true);
    await settle();
    expect(done, false);
    expect(r.registry.isReserved, true);
    r.commitHeld!.complete();
    await stop;
    expect(r.registry.isReserved, false);
  });
  test('foreign fresh probe retains exact owner', () async {
    final r = Rig();
    await r.start();
    r.foreignProbe = true;
    await expectLater(r.controller.stop(), throwsStateError);
    expect(r.registry.isReserved, true);
  });
  test('malformed start detail retains owner after exact prepared-origin cleanup', () async {
    final r = Rig()
      ..startError =
          PlatformException(code: 'owned-start-held', details: {'captureStartId': 'capture-A', 'nativeEpoch': true});
    await expectLater(r.start(), throwsA(isA<PlatformException>()));
    await expectLater(r.controller.stop(), throwsStateError);
    expect(r.stops, 1);
    expect(r.registry.isReserved, true);
  });
  test('exact closed owned start failure permits owned drain', () async {
    final r = Rig()
      ..startError =
          PlatformException(code: 'owned-start-held', details: {'captureStartId': 'capture-A', 'nativeEpoch': 11});
    await expectLater(r.start(), throwsA(isA<PlatformException>()));
    await r.controller.stop();
    expect(r.stops, 1);
    expect(r.registry.isReserved, false);
  });
  test('account retirement without explicit native policy acknowledgement holds', () async {
    final r = Rig();
    await r.start();
    r.authority = false;
    await expectLater(r.controller.stop(), throwsStateError);
    expect(r.registry.isReserved, true);
  });
  test('explicit quarantine retains old exact tail after authority retirement', () async {
    final r = Rig()..stopHeld = Completer();
    await r.start();
    r.authority = false;
    r.policy = const OwnedRetentionSnapshot(1, OwnedPrivateRetention.quarantine);
    await r.controller.retirePrivateRetention(r.policy);
    final stop = r.controller.stop();
    await settle();
    await r.frame();
    expect(r.publications, 0);
    r.stopHeld!.complete();
    await stop;
    expect(r.saved!.disposition, OwnedMicSinkDisposition.quarantinedExactBinding);
    expect(r.registry.isReserved, false);
  });
  test('forbidden policy blocks tail and release', () async {
    final r = Rig();
    await r.start();
    r.policy = const OwnedRetentionSnapshot(1, OwnedPrivateRetention.forbidden);
    await r.controller.retirePrivateRetention(r.policy);
    await expectLater(r.frame(), throwsStateError);
    await expectLater(r.controller.stop(), throwsStateError);
    expect(r.bytes, isEmpty);
    expect(r.registry.isReserved, true);
  });
  test('post-await stage policy change withholds acknowledgement', () async {
    final r = Rig()..stageHeld = Completer();
    await r.start();
    final frame = r.frame();
    final failure = expectLater(frame, throwsStateError);
    await settle();
    r.policy = const OwnedRetentionSnapshot(1, OwnedPrivateRetention.quarantine);
    r.stageHeld!.complete();
    await failure;
    expect(r.publications, 0);
    expect(r.registry.isReserved, true);
  });
  test('duplicate exact frame publishes once; conflicting bytes and order reject', () async {
    final r = Rig();
    await r.start();
    await r.frame();
    await r.frame();
    expect(r.publications, 1);
    await expectLater(r.frame(2), throwsStateError);
    await expectLater(
        r.controller.stageOwnedPcm(OwnedMicFrame(
            handle: OwnedMicHandle(binding: binding(), nativeEpoch: 11),
            sequence: 0,
            pcmBytes: Uint8List.fromList([3, 4]))),
        throwsStateError);
    await r.controller.stop();
  });
  test('held frame clones original bytes before asynchronous storage', () async {
    final r = Rig()..stageHeld = Completer();
    await r.start();
    final data = Uint8List.fromList([1, 2]);
    final frame = r.controller.stageOwnedPcm(
        OwnedMicFrame(handle: OwnedMicHandle(binding: binding(), nativeEpoch: 11), sequence: 0, pcmBytes: data));
    data[0] = 9;
    r.stageHeld!.complete();
    await frame;
    expect(r.bytes, [1, 2]);
    await r.controller.stop();
  });
  test('throwing UI observer cannot strand private staged or durable proof', () async {
    final r = Rig();
    await r.controller
        .start(onByteReceived: (_) => throw StateError('observer'), onRecording: () => throw StateError('observer'));
    await r.frame();
    await r.controller.stop();
    expect(r.registry.isReserved, false);
  });
  test('unsupported/throwing retention never becomes local grant', () async {
    final r = Rig();
    await r.start();
    r.policy = const OwnedRetentionSnapshot(-1, OwnedPrivateRetention.retain);
    await expectLater(r.frame(), throwsStateError);
    await expectLater(r.controller.stop(), throwsStateError);
    expect(r.registry.isReserved, true);
  });
  test('late stop receipt after timeout holds; exact retry keeps transaction and requests fresh proof', () async {
    final r = Rig(timeout: const Duration(milliseconds: 80))..stopHeld = Completer();
    await r.start();
    await expectLater(r.controller.stop(), throwsStateError);
    expect(r.registry.isReserved, true);
    r.stopHeld!.complete();
    await settle();
    expect(r.registry.isReserved, true);
    await r.controller.stop();
    expect(r.requests.length, 2);
    expect(r.requests.first.transactionId, r.requests.last.transactionId);
    expect(r.starts, 1);
    expect(r.registry.isReserved, false);
  });
  test('reentrant UI observer retirement withholds ACK but keeps counted private frame', () async {
    final r = Rig();
    await r.controller.start(onByteReceived: (_) {
      r.authority = false;
    });
    await expectLater(r.frame(), throwsStateError);
    expect(r.bytes, [1, 2]);
    expect(r.registry.isReserved, true);
    r.policy = const OwnedRetentionSnapshot(1, OwnedPrivateRetention.quarantine);
    await r.controller.retirePrivateRetention(r.policy);
    await r.frame();
    await r.controller.stop();
    expect(r.registry.isReserved, false);
  });
  test('held duplicate caller also fences retirement at outward ACK', () async {
    final r = Rig()..stageHeld = Completer();
    await r.start();
    final one = r.frame(), two = r.frame();
    final first = expectLater(one, throwsStateError), second = expectLater(two, throwsStateError);
    await settle();
    r.authority = false;
    r.stageHeld!.complete();
    await first;
    await second;
    expect(r.publications, 0);
    expect(r.registry.isReserved, true);
  });
  test('unacknowledged retained commit cannot be silently linked after quarantine retirement', () async {
    final r = Rig()..commitHeld = Completer();
    await r.start();
    final stopping = r.controller.stop();
    final failure = expectLater(stopping, throwsStateError);
    await settle();
    r.policy = const OwnedRetentionSnapshot(1, OwnedPrivateRetention.quarantine);
    await r.controller.retirePrivateRetention(r.policy);
    r.commitHeld!.complete();
    await failure;
    await expectLater(r.controller.stop(), throwsStateError);
    expect(r.registry.isReserved, true);
  });
  test('reentrant initialization retirement prevents even permission request', () async {
    final r = Rig();
    await expectLater(
        r.controller.start(
            onByteReceived: (_) {},
            onInitializing: () {
              r.authority = false;
            }),
        throwsStateError);
    expect(r.permissions, 0);
    expect(r.starts, 0);
    expect(r.registry.isReserved, true);
  });
  test('unknown native prepare error never guesses handle or releases owner', () async {
    final r = Rig()..prepareError = StateError('unknown');
    await expectLater(r.start(), throwsStateError);
    await expectLater(r.controller.stop(), throwsStateError);
    expect(r.registry.isReserved, true);
    expect(r.stops, 0);
    expect(r.cancels, 0);
  });
  test('capability refusal has no native prepare effect and safely retires unused owner', () async {
    final r = Rig()..capabilitiesSupported = false;
    await expectLater(r.start(), throwsStateError);
    await r.controller.stop();
    expect(r.registry.isReserved, false);
    expect(r.prepares, 0);
    expect(r.permissions, 0);
  });
  test('account ABA label cannot restore captured authority epoch', () async {
    final r = Rig()..permission = Completer();
    final start = r.start();
    final failure = expectLater(start, throwsStateError);
    await settle();
    r.authorityEpoch = 4;
    r.authority = false;
    r.authority = true;
    r.authorityEpoch = 5;
    r.permission!.complete(true);
    await failure;
    expect(r.starts, 0);
    await expectLater(r.controller.stop(), throwsStateError);
    expect(r.registry.isReserved, true);
  });
  test('joined stage receipt cannot inherit first caller nested mutable ACK', () async {
    final r = Rig()..stageHeld = Completer<void>();
    await r.start();
    final first = r.frame();
    final mutation = first.then((ack) {
      ack.handle.nativeEpoch = 900;
      ack.handle.binding.captureStartId = 'foreign';
      ack.sequence = 900;
      ack.acceptedValidPcmBytes = 900;
    });
    final joined = r.frame();
    r.stageHeld!.complete();
    final receipt = await joined;
    await mutation;
    expect(receipt.handle.nativeEpoch, 11);
    expect(receipt.handle.binding.captureStartId, 'capture-A');
    expect(receipt.sequence, 0);
    expect(receipt.acceptedValidPcmBytes, 2);
    await r.controller.stop();
  });
  test('false native EOS receipt cannot release while local stage acknowledgement is pending', () async {
    final r = Rig()..stageHeld = Completer<void>();
    await r.start();
    final frame = r.frame();
    await expectLater(r.controller.stop(), throwsStateError);
    expect(r.registry.isReserved, true);
    r.stageHeld!.complete();
    await frame;
    await r.controller.stop();
    expect(r.registry.isReserved, false);
  });
}
