// ellaaicare/ella-ai#1280: offline capture is finalized into UPSTREAM's own WAL
// (vendored wals.dart / wal_interfaces.dart / LocalWalSyncImpl) and drained by
// upstream's RecordingTransferCoordinator, composed by the Ella runtime. No
// Ella WAL implementation is involved; the Ella gate only decides admission.
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/upstream_capture/services/capture/scenarios/native_event_vector.dart';
import 'package:omi/upstream_capture/services/wals/recording_transfer_coordinator.dart';
import 'package:omi/upstream_capture/services/wals/wal.dart';

import 'support/ella_upstream_capture_harness.dart';

void main() {
  late Directory directory;
  late EllaUpstreamCaptureHarness h;

  setUp(() async {
    directory = await Directory.systemTemp.createTemp('ella_upstream_wal_');
    h = await EllaUpstreamCaptureHarness.boot(tempDir: directory);
  });

  tearDown(() async {
    await h.dispose();
    if (directory.existsSync()) directory.deleteSync(recursive: true);
  });

  /// 100 frames (10 ms each) per virtual second, like upstream's replay tests.
  Future<void> captureSeconds(int seconds, {required int sessionId, required int frameCursor}) async {
    for (var s = 0; s < seconds; s++) {
      h.injectPhoneFrames(100, sessionId: sessionId, firstFrameIndex: frameCursor + s * 100);
      await h.elapse(const Duration(seconds: 1));
    }
  }

  Future<List<List<int>>> storedFrames(Wal wal) async {
    final path = await Wal.getFilePath(wal.filePath);
    final bytes = File(path!).readAsBytesSync();
    final frames = <List<int>>[];
    var offset = 0;
    while (offset + 4 <= bytes.length) {
      final length = ByteData.sublistView(bytes, offset, offset + 4).getUint32(0, Endian.little);
      offset += 4;
      frames.add(bytes.sublist(offset, offset + length));
      offset += length;
    }
    return frames;
  }

  Future<Wal> captureOfflineAndStop({int onlineSeconds = 2, int offlineSeconds = 6}) async {
    final session = await h.startPhone();
    await captureSeconds(onlineSeconds, sessionId: session, frameCursor: 0);
    h.setConnected(false);
    h.socket!.emitClose();
    await h.settle();
    await captureSeconds(offlineSeconds, sessionId: session, frameCursor: onlineSeconds * 100);
    await h.provider.stopStreamRecording();
    await h.settle();
    final missing = await h.wal.syncs.phone.getMissingWals();
    expect(missing, hasLength(1), reason: 'the stopped session is finalized into one upstream WAL');
    return missing.single;
  }

  test('offline phone capture is finalized into an on-disk upstream WAL holding exactly the admitted frames', () async {
    expect(await h.bind(), isTrue);
    final wal = await captureOfflineAndStop();
    expect(wal.status, WalStatus.miss);
    expect(File('${directory.path}/wals.json').existsSync(), isTrue, reason: 'upstream WAL index is persisted');
    expect(
      await storedFrames(wal),
      List.generate(800, (i) => NativeEventVector.synthesizePcmFrame(i)),
      reason: 'every admitted frame (online + offline) is durable, byte for byte',
    );
  });

  test('upstream recovery drain uploads the finalized WAL once while authority is current', () async {
    expect(await h.bind(), isTrue);
    final wal = await captureOfflineAndStop();
    h.setConnected(true);
    await h.settle();
    await h.coordinator.wake(WakeTrigger.userRetry);
    await h.settle();
    expect(h.uploads.attempts, hasLength(1));
    final walPath = (await Wal.getFilePath(wal.filePath))!;
    expect(h.uploads.attempts.single.totalBytes, File(walPath).lengthSync());
    expect((await h.walCounts())[WalStatus.synced], 1);

    await h.coordinator.wake(WakeTrigger.userRetry);
    await h.settle();
    expect(h.uploads.attempts, hasLength(1), reason: 'a synced WAL is never re-uploaded');
  });

  test('auto-upload of recorded audio is withheld after the consent session is revoked', () async {
    expect(await h.bind(), isTrue);
    await captureOfflineAndStop();
    h.authority.release();
    await h.settle();
    h.setConnected(true);
    await h.settle();
    await h.coordinator.wake(WakeTrigger.connectivityRestored);
    await h.settle();
    expect(h.uploads.attempts, isEmpty, reason: 'uploading audio is an emission: no authority, no auto-upload');
    expect((await h.walCounts())[WalStatus.miss], 1, reason: 'the recording stays durable and retryable');

    // A fresh consent session re-admits the pending upload.
    expect(await h.bind(), isTrue);
    await h.coordinator.wake(WakeTrigger.connectivityRestored);
    await h.settle();
    expect(h.uploads.attempts, hasLength(1));
  });

  test('frames refused by the gate never enter the upstream WAL', () async {
    expect(await h.bind(), isTrue);
    final session = await h.startPhone();
    h.setConnected(false);
    h.socket!.emitClose();
    await h.settle();
    await captureSeconds(2, sessionId: session, frameCursor: 0);
    h.ellaPreferences.declineAiConsent();
    // Refused frames: the first one revokes; none are admitted.
    h.injectPhoneFrames(100, sessionId: session, firstFrameIndex: 5000);
    await h.settle();
    final wals = await h.wal.syncs.phone.getAllWals();
    final frames = <List<int>>[];
    for (final wal in wals) {
      frames.addAll(await storedFrames(wal));
    }
    expect(frames, List.generate(200, (i) => NativeEventVector.synthesizePcmFrame(i)),
        reason: 'only the 200 frames admitted before the revocation are durable');
  });
}
