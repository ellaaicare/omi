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
