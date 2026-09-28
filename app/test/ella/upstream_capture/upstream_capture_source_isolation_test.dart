// ellaaicare/ella-ai#1280: necklace and phone mic are mutually exclusive — one
// active capture owner at a time — enforced by upstream's own CaptureCoordinator
// on the flag-ON graph (Ella composition + per-frame gates), not by Ella code.
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/upstream_capture/utils/enums.dart';

import 'support/ella_upstream_capture_harness.dart';

void main() {
  late Directory directory;
  late EllaUpstreamCaptureHarness h;

  setUp(() async {
    directory = await Directory.systemTemp.createTemp('ella_upstream_isolation_');
    h = await EllaUpstreamCaptureHarness.boot(tempDir: directory);
    expect(await h.bind(), isTrue);
  });

  tearDown(() async {
    await h.dispose();
    if (directory.existsSync()) directory.deleteSync(recursive: true);
  });

  Future<int> pendantFramesReaching(dynamic link, {int packets = 5}) async {
    final before = h.audioFramesSent;
    for (var i = 0; i < packets; i++) {
      link.emitAudio();
    }
    await h.settle();
    return h.audioFramesSent - before;
  }

  test('phone takes over from a live necklace: pendant audio stops, the phone owns capture', () async {
    final link = await h.connectPendant();
    expect(h.provider.recordingState, RecordingState.deviceRecord);
    expect(await pendantFramesReaching(link), greaterThan(0));
    final pendantSocketCount = h.sockets.length;

    final session = await h.startPhone();
    expect(h.provider.recordingState, RecordingState.record);
    expect(link.openAudioSubscriptions, 0, reason: 'the pendant stream is paused while the phone records');
    expect(await pendantFramesReaching(link), 0, reason: 'pendant audio never reaches the phone session');
    expect(h.sockets.length, greaterThan(pendantSocketCount), reason: 'the phone opens its own socket');

    final beforePhone = h.socket!.sentBinary.length;
    h.injectPhoneFrames(10, sessionId: session);
    await h.settle();
    expect(h.socket!.sentBinary.length, greaterThan(beforePhone));
  });

  test('stopping the phone hands capture back to the necklace (never both at once)', () async {
    final link = await h.connectPendant();
    final session = await h.startPhone();
    expect(link.openAudioSubscriptions, 0);

    await h.provider.stopStreamRecording();
    await h.settle();
    expect(h.hostApi.nativeRecording, isFalse, reason: 'the phone mic is closed before the pendant resumes');
    expect(h.provider.recordingState, RecordingState.deviceRecord);
    expect(link.openAudioSubscriptions, 1);
    expect(await pendantFramesReaching(link), greaterThan(0));

    // A late phone frame after the hand-back never reaches the pendant session.
    final before = h.audioFramesSent;
    h.injectPhoneFrames(5, sessionId: session, firstFrameIndex: 100);
    await h.settle();
    expect(h.audioFramesSent, before);
  });

  test('at most one source subscription is open at every step of a necklace/phone/necklace cycle', () async {
    final link = await h.connectPendant();
    final observed = <({bool pendant, bool phone})>[];
    void snapshot() => observed.add((pendant: link.openAudioSubscriptions > 0, phone: h.hostApi.nativeRecording));

    snapshot();
    await h.startPhone();
    snapshot();
    await h.provider.stopStreamRecording();
    await h.settle();
    snapshot();
    await h.startPhone();
    snapshot();
    await h.provider.stopStreamRecording();
    await h.settle();
    snapshot();

    for (final step in observed) {
      expect(step.pendant && step.phone, isFalse, reason: 'necklace and phone must never capture together: $observed');
    }
    expect(observed.first.pendant, isTrue);
    expect(observed[1].phone, isTrue);
  });
}
