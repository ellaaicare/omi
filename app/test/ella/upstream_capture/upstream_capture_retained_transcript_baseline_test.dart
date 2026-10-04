import 'dart:async';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:omi/upstream_capture/backend/schema/conversation.dart';
import 'package:omi/upstream_capture/backend/schema/structured.dart';
import 'package:omi/upstream_capture/backend/schema/transcript_segment.dart';
import 'package:omi/upstream_capture/providers/capture_provider.dart';

import 'support/ella_upstream_capture_harness.dart';

ServerConversation _conversation(String id, String text) => ServerConversation(
      id: id,
      createdAt: DateTime.utc(2026, 10, 3),
      structured: Structured('Fixture', ''),
      transcriptSegments: [
        TranscriptSegment(
          id: '$id-segment',
          text: text,
          speaker: 'SPEAKER_0',
          isUser: false,
          personId: null,
          start: 0,
          end: 1,
          translations: <Translation>[],
        ),
      ],
    );

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late Directory directory;
  late EllaUpstreamCaptureHarness harness;
  late Completer<ServerConversation?> response;
  late Completer<void> requestStarted;
  late Completer<void> requestFinished;

  setUp(() async {
    directory = await Directory.systemTemp.createTemp('ella_retained_transcript_baseline_');
    response = Completer<ServerConversation?>();
    requestStarted = Completer<void>();
    requestFinished = Completer<void>();
    harness = await EllaUpstreamCaptureHarness.boot(
      tempDir: directory,
      refreshConversation: (CaptureProvider provider) async {
        if (!requestStarted.isCompleted) requestStarted.complete();
        try {
          final conversation = await response.future;
          provider.applyInProgressConversation(conversation);
        } finally {
          if (!requestFinished.isCompleted) requestFinished.complete();
        }
      },
    );
  });

  tearDown(() async {
    if (!response.isCompleted) response.complete(null);
    if (requestStarted.isCompleted && !requestFinished.isCompleted) {
      await requestFinished.future.timeout(const Duration(seconds: 5));
    }
    await pumpEventQueue();
    await harness.dispose();
    if (directory.existsSync()) directory.deleteSync(recursive: true);
  });

  test('deferred GET from retired same-UID binding must not install transcript', () async {
    expect(await harness.bind(accountA), isTrue);
    final oldEpoch = harness.authority.bindingEpoch;
    await harness.provider.refreshInProgressConversations();
    await requestStarted.future;

    harness.authority.release();
    expect(harness.authority.bind(accountA), isTrue);
    expect(harness.authority.bindingEpoch, isNot(oldEpoch));

    response.complete(_conversation('old-recording', 'stale private transcript'));
    await requestFinished.future;
    expect(harness.provider.segments, isEmpty);
  });

  test('legacy untagged GET before recording still installs raw provider data', () async {
    expect(await harness.bind(accountA), isTrue);
    harness.setConnected(false);
    await harness.provider.refreshInProgressConversations();
    await requestStarted.future;

    response.complete(_conversation('unrelated-recording', 'unattributed transcript'));
    await requestFinished.future;
    expect(harness.provider.activeRecordingId, isNull);
    expect(harness.provider.segments.single.text, 'unattributed transcript');
  });

  test('deferred GET from account A must not install after account B binds', () async {
    expect(await harness.bind(accountA), isTrue);
    await harness.provider.refreshInProgressConversations();
    await requestStarted.future;

    harness.switchAccount(accountB);
    grantEllaConsent(harness.ellaPreferences, accountB);
    expect(await harness.bind(accountB), isTrue);

    response.complete(_conversation('account-a-recording', 'account A private transcript'));
    await requestFinished.future;
    expect(harness.provider.segments, isEmpty);
  });

  test('deferred GET from before a new recording must not replace its transcript', () async {
    expect(await harness.bind(accountA), isTrue);
    final oldRecordingId = harness.provider.activeRecordingId;
    await harness.provider.refreshInProgressConversations();
    await requestStarted.future;

    harness.setConnected(false);
    await harness.startPhone();
    expect(harness.provider.activeRecordingId, isNot(oldRecordingId));
    response.complete(_conversation('previous-recording', 'previous recording text'));
    await requestFinished.future;
    expect(harness.provider.segments, isEmpty);
  });

  test('segment waiting behind a retired GET must not append after rebinding', () async {
    expect(await harness.bind(accountA), isTrue);
    harness.provider.onSegmentReceived(_conversation('old-recording', 'late socket text').transcriptSegments);
    await requestStarted.future;

    harness.authority.release();
    expect(harness.authority.bind(accountA), isTrue);
    response.complete(null);
    await requestFinished.future;
    await pumpEventQueue();
    expect(harness.provider.segments, isEmpty);
  });

  test('legacy raw provider aliases fetched segment elements and list', () async {
    expect(await harness.bind(accountA), isTrue);
    await harness.provider.refreshInProgressConversations();
    await requestStarted.future;

    final fetched = _conversation('current-recording', 'accepted transcript');
    response.complete(fetched);
    await requestFinished.future;
    expect(harness.provider.segments.single.text, 'accepted transcript');

    fetched.transcriptSegments.single.text = 'mutated source text';
    fetched.transcriptSegments.single.translations.add(const Translation(lang: 'es', text: 'private translation'));
    expect(harness.provider.segments.single.text, 'mutated source text');
    expect(harness.provider.segments.single.translations.single.text, 'private translation');

    fetched.transcriptSegments.clear();
    expect(harness.provider.segments, isEmpty);
  });

  test('a current socket transcript remains available after ordinary same-origin stop', () async {
    expect(await harness.bind(accountA), isTrue);
    await harness.startPhone();
    final recordingId = harness.provider.activeRecordingId;
    expect(recordingId, isNotNull);
    expect(harness.sockets.last.service.clientConversationId, recordingId);
    response.complete(null);

    harness.socket!.emitServerMessage(
      '[{"id":"current-segment","text":"same-origin transcript","speaker":"SPEAKER_0",'
      '"is_user":false,"start":0.0,"end":1.0}]',
    );
    await requestStarted.future;
    await requestFinished.future;
    await harness.settle();
    expect(harness.provider.segments.single.text, 'same-origin transcript');

    expect(await harness.provider.stopStreamRecording(), isTrue);
    await harness.settle();
    expect(harness.authority.boundUid, accountA);
    expect(harness.provider.segments.single.text, 'same-origin transcript');
  });
}
