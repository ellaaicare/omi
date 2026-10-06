// ellaaicare/ella-ai#1280: an explicit "finish conversation" leaves no latch
// behind — neither in upstream's capture coordinator nor in the Ella gate — so
// capture can start again (phone and necklace) right after it.
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/upstream_capture/backend/preferences.dart' as upstream;
import 'package:omi/upstream_capture/utils/enums.dart';

import 'support/ella_upstream_capture_harness.dart';

void main() {
  late Directory directory;
  late EllaUpstreamCaptureHarness h;

  setUp(() async {
    directory = await Directory.systemTemp.createTemp('ella_upstream_finish_');
    h = await EllaUpstreamCaptureHarness.boot(tempDir: directory);
    expect(await h.bind(), isTrue);
  });

  tearDown(() async {
    await h.dispose();
    if (directory.existsSync()) directory.deleteSync(recursive: true);
  });

  test('phone: finish processes the conversation, then a new phone capture starts and streams', () async {
    final first = await h.startPhone();
    h.injectPhoneFrames(30, sessionId: first);
    await h.settle();
    final firstRecording = h.provider.activeRecordingId;
    final sentFirst = h.audioFramesSent;
    expect(sentFirst, greaterThan(0));

    await h.runtime.finishConversation();
    await h.settle();
    expect(h.processCalls, 1, reason: 'finish asks upstream to process the in-progress conversation');
    expect(h.hostApi.nativeRecording, isFalse);
    expect(h.provider.recordingState, isNot(RecordingState.record));
    expect(h.authority.isBound, isTrue, reason: 'finishing a conversation is not a consent revocation');
    expect(upstream.SharedPreferencesUtil().capturePolicy.muted, isFalse, reason: 'no mute latch is left behind');

    final second = await h.startPhone();
    expect(second, isNot(first));
    expect(h.provider.recordingState, RecordingState.record);
    expect(h.provider.activeRecordingId, isNot(firstRecording), reason: 'a new conversation, not a resumed one');
    h.injectPhoneFrames(30, sessionId: second, firstFrameIndex: 30);
    await h.settle();
    expect(h.audioFramesSent, greaterThan(sentFirst));
  });

  test('repeated finish/start cycles never wedge the capture state machine', () async {
    var frameCursor = 0;
    for (var cycle = 0; cycle < 3; cycle++) {
      final session = await h.startPhone();
      final before = h.audioFramesSent;
      h.injectPhoneFrames(10, sessionId: session, firstFrameIndex: frameCursor);
      frameCursor += 10;
      await h.settle();
      expect(h.audioFramesSent, greaterThan(before), reason: 'cycle $cycle streams');
      await h.runtime.finishConversation();
      await h.settle();
    }
    expect(h.processCalls, 3);
  });

  test('real Finish reset retires the presentation window even when the WAL second is reused', () async {
    final session = await h.startPhone();
    h.injectPhoneFrames(10, sessionId: session);
    await h.settle();
    final before = h.provider.captureWindowGeneration;
    h.provider.testSessionStartSeconds = 42;
    final originalTimestamp = h.provider.activeCaptureSessionId;
    await h.runtime.finishConversation();
    await h.settle();
    final after = h.provider.captureWindowGeneration;
    expect(after, greaterThan(before));
    // Reusing the clock second cannot restore an already-retired identity.
    h.provider.testSessionStartSeconds = 42;
    expect(h.provider.activeCaptureSessionId, originalTimestamp);
    expect(h.provider.captureWindowGeneration, after);
    expect(h.processCalls, 1);
  });

  test('necklace: after finishing a pendant conversation the pendant capture restarts', () async {
    final link = await h.connectPendant();
    for (var i = 0; i < 3; i++) {
      link.emitAudio();
    }
    await h.settle();
    final sent = h.audioFramesSent;
    expect(sent, greaterThan(0));

    await h.runtime.finishConversation();
    await h.settle();
    expect(h.processCalls, 1);

    // Upstream keeps (or restarts) the pendant session after processing; an
    // explicit restart must also be admitted.
    await h.provider.streamDeviceRecording(device: EllaUpstreamCaptureHarness.pendant);
    await h.settle();
    expect(h.provider.recordingState, RecordingState.deviceRecord);
    for (var i = 0; i < 3; i++) {
      link.emitAudio();
    }
    await h.settle();
    expect(h.audioFramesSent, greaterThan(sent));
  });
}
