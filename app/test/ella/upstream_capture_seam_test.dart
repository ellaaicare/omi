import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_adapter.dart';

void main() {
  test('upstream capture flag defaults off', () {
    expect(kEllaUpstreamCaptureEnabled, isFalse);
    final adapter = EllaUpstreamCaptureAdapter(mayEmitAudio: () => true);
    adapter.replaceSession('account-a');
    expect(adapter.connectBle(), isFalse);
    expect(
      adapter.onDeviceAudio(generation: adapter.generation, ownerUid: 'account-a', bytes: const [1]),
      isFalse,
    );
    expect(adapter.emitted, isEmpty);
  });

  test('capture refuses to start with an empty uid', () {
    final adapter = EllaUpstreamCaptureAdapter(mayEmitAudio: () => true, enabled: true);
    // No replaceSession() call: uid stays '' — no bound account session yet,
    // on both the phone-mic and necklace start paths.
    expect(adapter.uid, isEmpty);
    adapter.bleConnected = true;
    expect(adapter.startNecklace(), isFalse);
    expect(adapter.startPhoneMic(), isFalse);
    expect(adapter.liveSource, UpstreamLiveSource.none);
    expect(
      adapter.onDeviceAudio(generation: adapter.generation, ownerUid: '', bytes: const [1]),
      isFalse,
    );
    expect(adapter.finalizeBatch(sessionId: 'sess-1', source: 'phone'), isFalse);
    expect(adapter.emitted, isEmpty);
    expect(adapter.memories, isEmpty);
  });

  test('phone-mic emission is gated per frame, not just at start', () {
    var allowed = true;
    final adapter = EllaUpstreamCaptureAdapter(mayEmitAudio: () => allowed, enabled: true);
    adapter.replaceSession('account-a');
    expect(adapter.startPhoneMic(), isTrue);
    final generation = adapter.generation;

    expect(
      adapter.onPhoneAudio(generation: generation, ownerUid: 'account-a', bytes: const [1]),
      isTrue,
    );

    // Consent withdrawn mid-stream: the next frame is dropped even though
    // startPhoneMic() already succeeded and BLE/mic stay running.
    allowed = false;
    expect(
      adapter.onPhoneAudio(generation: generation, ownerUid: 'account-a', bytes: const [2]),
      isFalse,
    );
    expect(adapter.emitted, hasLength(1));

    // A frame is never admitted through onPhoneAudio while the necklace (not
    // the phone) is the live source, even with consent restored and a
    // matching generation/uid.
    allowed = true;
    adapter.stopPhoneMic();
    adapter.bleConnected = true;
    expect(adapter.startNecklace(), isTrue);
    expect(
      adapter.onPhoneAudio(generation: generation, ownerUid: 'account-a', bytes: const [3]),
      isFalse,
    );
  });

  test('capture can start again after finish conversation / process now', () {
    final adapter = EllaUpstreamCaptureAdapter(mayEmitAudio: () => true, enabled: true);
    adapter.replaceSession('account-a');
    expect(adapter.startPhoneMic(), isTrue);
    final generation = adapter.generation;
    expect(
      adapter.onPhoneAudio(generation: generation, ownerUid: 'account-a', bytes: const [1]),
      isTrue,
    );

    // "Finish conversation / process now": finalize the batch and stop.
    expect(adapter.finalizeBatch(sessionId: 'sess-1', source: 'phone'), isTrue);
    adapter.stopPhoneMic();
    expect(adapter.liveSource, UpstreamLiveSource.none);

    // A known regression in the legacy Ella path leaves capture unable to
    // restart here. The adapter carries no such latch: a new session starts
    // immediately.
    expect(adapter.startPhoneMic(), isTrue);
    final secondGeneration = adapter.generation;
    expect(
      adapter.onPhoneAudio(generation: secondGeneration, ownerUid: 'account-a', bytes: const [2]),
      isTrue,
    );
    expect(adapter.finalizeBatch(sessionId: 'sess-2', source: 'phone'), isTrue);
    expect(adapter.memoriesFor('account-a'), hasLength(2));
  });

  test('account A BLE callback after sign-in to B is ignored', () {
    final adapter = EllaUpstreamCaptureAdapter(mayEmitAudio: () => true, enabled: true);
    adapter.replaceSession('account-a');
    final generationA = adapter.generation;
    adapter.connectBle();
    expect(
      adapter.onDeviceAudio(generation: generationA, ownerUid: 'account-a', bytes: const [1, 2]),
      isTrue,
    );

    adapter.replaceSession('account-b');
    expect(
      adapter.onDeviceAudio(generation: generationA, ownerUid: 'account-a', bytes: const [9]),
      isFalse,
    );
    expect(adapter.emitted.where((frame) => frame.ownerUid == 'account-b'), isEmpty);
    expect(adapter.memoriesFor('account-b'), isEmpty);
  });

  test('consent declined allows BLE connect and emits no audio', () {
    var allowed = false;
    final adapter = EllaUpstreamCaptureAdapter(mayEmitAudio: () => allowed, enabled: true);
    adapter.replaceSession('account-a');
    expect(adapter.connectBle(), isTrue);
    expect(adapter.bleConnected, isTrue);
    expect(
      adapter.onDeviceAudio(generation: adapter.generation, ownerUid: 'account-a', bytes: const [1]),
      isFalse,
    );
    expect(adapter.startPhoneMic(), isFalse);
    expect(adapter.finalizeBatch(sessionId: 'batch-1', source: 'phone'), isFalse);
    expect(adapter.emitted, isEmpty);
    expect(adapter.memories, isEmpty);

    allowed = true;
    expect(
      adapter.onDeviceAudio(generation: adapter.generation, ownerUid: 'account-a', bytes: const [4]),
      isTrue,
    );
    expect(adapter.emitted, hasLength(1));
  });

  test('phone mic and necklace are mutually exclusive', () {
    final adapter = EllaUpstreamCaptureAdapter(mayEmitAudio: () => true, enabled: true);
    adapter.replaceSession('account-a');
    adapter.connectBle();
    expect(adapter.startNecklace(), isTrue);
    expect(adapter.liveSource, UpstreamLiveSource.necklace);

    expect(adapter.startPhoneMic(), isTrue);
    expect(adapter.liveSource, UpstreamLiveSource.phone);
    expect(
      adapter.onDeviceAudio(generation: adapter.generation, ownerUid: 'account-a', bytes: const [1]),
      isFalse,
    );

    adapter.stopPhoneMic(resumeNecklace: true);
    expect(adapter.liveSource, UpstreamLiveSource.necklace);
    expect(adapter.liveSource, isNot(UpstreamLiveSource.phone));
  });

  test('batch phone capture finalizes one Ella memory for that account', () {
    final adapter = EllaUpstreamCaptureAdapter(mayEmitAudio: () => true, enabled: true);
    adapter.replaceSession('account-a');
    expect(adapter.finalizeBatch(sessionId: 'sess-1', source: 'phone'), isTrue);
    expect(adapter.finalizeBatch(sessionId: 'sess-1', source: 'phone'), isFalse);
    expect(adapter.memoriesFor('account-a'), hasLength(1));

    adapter.replaceSession('account-b');
    expect(adapter.memoriesFor('account-b'), isEmpty);
    expect(adapter.memoriesFor('account-a').single.sessionId, 'sess-1');
  });

  test('reconnect follows upstream manual-disconnect and stale-bond rules', () {
    final adapter = EllaUpstreamCaptureAdapter(mayEmitAudio: () => true, enabled: true);
    adapter.replaceSession('account-a');
    expect(adapter.connectBle(), isTrue);
    adapter.manualDisconnect();
    expect(adapter.bleConnected, isFalse);
    expect(adapter.connectBle(), isFalse);
    expect(adapter.connectBle(force: true), isTrue);
    expect(adapter.bleConnected, isTrue);

    adapter.markStaleBond();
    expect(adapter.connectBle(force: true), isFalse);
    expect(adapter.bleConnected, isFalse);
    adapter.clearStaleBond();
    expect(adapter.connectBle(force: true), isTrue);

    adapter.manualDisconnect();
    expect(
      adapter.onDeviceAudio(generation: adapter.generation, ownerUid: 'account-a', bytes: const [1]),
      isFalse,
    );
    expect(adapter.connectBle(force: true), isTrue);
    expect(
      adapter.onDeviceAudio(generation: adapter.generation, ownerUid: 'account-a', bytes: const [2]),
      isTrue,
    );
  });
}
