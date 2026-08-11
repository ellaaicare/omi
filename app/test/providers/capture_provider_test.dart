import 'dart:async';

import 'package:connectivity_plus_platform_interface/connectivity_plus_platform_interface.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/message_event.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/backend/schema/transcript_segment.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/people_provider.dart';
import 'package:omi/services/services.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

/// Mock PeopleProvider that tracks setPeople calls
class MockPeopleProvider extends PeopleProvider {
  int setPeopleCallCount = 0;
  Completer<void>? _setPeopleCompleter;

  @override
  Future<void> setPeople() async {
    setPeopleCallCount++;
    if (_setPeopleCompleter != null) {
      // Simulate async work - wait for completer
      await _setPeopleCompleter!.future;
    }
  }

  /// Set a completer to control when setPeople completes
  void setSetPeopleCompleter(Completer<void> completer) {
    _setPeopleCompleter = completer;
  }
}

class _TestConnectivityPlatform extends ConnectivityPlatform {
  @override
  Future<List<ConnectivityResult>> checkConnectivity() async {
    return [ConnectivityResult.none];
  }

  @override
  Stream<List<ConnectivityResult>> get onConnectivityChanged => const Stream.empty();
}

TranscriptSegment _segment(String id, String text) {
  return TranscriptSegment(
    id: id,
    text: text,
    speaker: 'SPEAKER_00',
    isUser: false,
    personId: null,
    start: 0.0,
    end: 1.0,
    translations: [],
  );
}

class _CaptureAuthority implements AccountCommitAuthority {
  _CaptureAuthority(this.uid);

  @override
  final String uid;
  bool current = true;

  @override
  bool isCurrent() => current;

  @override
  bool isExactCurrent() => current;
}

ServerConversation _conversation(String id, String transcript,
    {ConversationStatus status = ConversationStatus.in_progress}) {
  final startedAt = DateTime.parse('2026-08-10T20:00:00Z');
  return ServerConversation(
    id: id,
    createdAt: startedAt,
    startedAt: startedAt,
    finishedAt: startedAt.add(const Duration(minutes: 1)),
    structured: Structured('Memory $id', 'Overview'),
    status: status,
    transcriptSegments: [_segment('segment-$id', transcript)],
  );
}

void main() {
  test('blank transcript placeholders are not capturable content', () {
    final provider = CaptureProvider();
    provider.segments = [_segment('blank', '   \n  ')];

    expect(provider.hasCapturableContent, isFalse);

    provider.segments = [_segment('spoken', 'A real moment')];
    expect(provider.hasCapturableContent, isTrue);
  });

  test('final transcript refresh failures are bounded and never create blank content', () async {
    final authority = _CaptureAuthority('uid-a');
    var refreshes = 0;
    final provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        refreshes++;
        throw StateError('transient refresh failure');
      },
    );
    addTearDown(provider.dispose);

    expect(
      await provider.awaitFinalCapturableContent(maxAttempts: 3, retryDelay: Duration.zero),
      isFalse,
    );
    expect(refreshes, 3);
  });

  test('delayed final transcript cannot cross an account and capture transition', () async {
    final authority = _CaptureAuthority('uid-a');
    final response = Completer<List<ServerConversation>>();
    ExactAccountAuthorityVerifier? requestAuthority;
    var processCalls = 0;
    final provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) {
        expect(expectedAuthenticatedUid, 'uid-a');
        requestAuthority = exactAuthority;
        return response.future;
      },
      inProgressConversationProcess: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        processCalls++;
        return null;
      },
    );
    addTearDown(provider.dispose);

    final finalization = provider.finalizeCurrentConversation(
      maxTranscriptAttempts: 1,
      transcriptRetryDelay: Duration.zero,
    );
    await pumpEventQueue();
    expect(requestAuthority?.isExactCurrent(), isTrue);

    authority.current = false;
    EllaAccountCommitBarrier.quiesceForAccountTransition();
    await provider.stopForAccountTransition();
    response.complete([_conversation('account-a', 'Account A final words')]);

    expect(await finalization, isFalse);
    expect(requestAuthority?.isExactCurrent(), isFalse);
    expect(provider.segments, isEmpty);
    expect(processCalls, 0);
  });

  test('delayed processing completion cannot mutate replacement-account memories', () async {
    final authority = _CaptureAuthority('uid-a');
    final response = Completer<CreateConversationResponse?>();
    ExactAccountAuthorityVerifier? fetchAuthority;
    ExactAccountAuthorityVerifier? processAuthority;
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    final provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(expectedAuthenticatedUid, 'uid-a');
        fetchAuthority = exactAuthority;
        return [_conversation('account-a', 'Account A final words')];
      },
      inProgressConversationProcess: ({required expectedAuthenticatedUid, required exactAuthority}) {
        expect(expectedAuthenticatedUid, 'uid-a');
        processAuthority = exactAuthority;
        return response.future;
      },
    )..updateProviderInstances(conversations, null, null, null);
    addTearDown(provider.dispose);

    final finalization = provider.finalizeCurrentConversation(
      maxTranscriptAttempts: 1,
      transcriptRetryDelay: Duration.zero,
    );
    await pumpEventQueue();
    expect(identical(fetchAuthority, processAuthority), isTrue);
    expect(processAuthority?.isExactCurrent(), isTrue);

    authority.current = false;
    EllaAccountCommitBarrier.quiesceForAccountTransition();
    await provider.stopForAccountTransition();
    conversations.reset();
    final replacement = _conversation('account-b', 'Replacement account', status: ConversationStatus.completed);
    conversations.conversations = [replacement];
    response.complete(
      CreateConversationResponse(
        messages: const [],
        conversation: _conversation('processed-a', 'Processed A', status: ConversationStatus.completed),
      ),
    );

    expect(await finalization, isFalse);
    expect(processAuthority?.isExactCurrent(), isFalse);
    expect(conversations.conversations, [replacement]);
    expect(conversations.processingConversations, isEmpty);
  });

  test('a local partial still performs one authoritative GET before processing', () async {
    final authority = _CaptureAuthority('uid-a');
    var fetches = 0;
    var processes = 0;
    ExactAccountAuthorityVerifier? fetchAuthority;
    ExactAccountAuthorityVerifier? processAuthority;
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    final provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(expectedAuthenticatedUid, 'uid-a');
        fetches++;
        fetchAuthority = exactAuthority;
        return [_conversation('authoritative', 'Authoritative final words')];
      },
      inProgressConversationProcess: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(expectedAuthenticatedUid, 'uid-a');
        processes++;
        processAuthority = exactAuthority;
        return CreateConversationResponse(
          messages: const [],
          conversation: _conversation('processed', 'Processed words', status: ConversationStatus.completed),
        );
      },
    )
      ..updateProviderInstances(conversations, null, null, null)
      ..segments = [_segment('local-partial', 'Local partial')];
    addTearDown(provider.dispose);

    expect(
      await provider.finalizeCurrentConversation(maxTranscriptAttempts: 1, transcriptRetryDelay: Duration.zero),
      isTrue,
    );
    expect(fetches, 1);
    expect(processes, 1);
    expect(identical(fetchAuthority, processAuthority), isTrue);
    expect(conversations.conversations.single.id, 'processed');
  });

  setUpAll(() async {
    TestWidgetsFlutterBinding.ensureInitialized();
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      const MethodChannel('com.omi/floating_control_bar'),
      (_) async => null,
    );
    SharedPreferences.setMockInitialValues({});
    ConnectivityPlatform.instance = _TestConnectivityPlatform();
    try {
      await ServiceManager.init();
    } catch (_) {
      // Ignore if already initialized by another test.
    }
  });

  tearDownAll(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      const MethodChannel('com.omi/floating_control_bar'),
      null,
    );
  });

  test('removes segments and related state on deletion event', () {
    final provider = CaptureProvider();
    final first = _segment('a', 'one');
    final second = _segment('b', 'two');

    provider.segments = [first, second];
    provider.suggestionsBySegmentId['a'] = SpeakerLabelSuggestionEvent(
      speakerId: 1,
      personId: 'p1',
      personName: 'Test',
      segmentId: 'a',
    );
    provider.taggingSegmentIds = ['a', 'b'];
    provider.hasTranscripts = true;

    provider.onMessageEventReceived(SegmentsDeletedEvent(segmentIds: ['a']));

    expect(provider.segments.length, 1);
    expect(provider.segments.first.id, 'b');
    expect(provider.suggestionsBySegmentId.containsKey('a'), false);
    expect(provider.taggingSegmentIds.contains('a'), false);
    expect(provider.hasTranscripts, true);
  });

  group('metricsNotifyEnabled', () {
    test('defaults to not notifying on metrics update', () {
      final provider = CaptureProvider();
      // By default, metrics notify is disabled
      // We can verify this by checking that the provider was created successfully
      // and bleReceiveRateKbps/wsSendRateKbps are accessible (default 0)
      expect(provider.bleReceiveRateKbps, 0.0);
      expect(provider.wsSendRateKbps, 0.0);
    });

    test('addMetricsListener() enables metrics notifications on first listener', () {
      final provider = CaptureProvider();
      var notifyCount = 0;
      provider.addListener(() => notifyCount++);

      provider.addMetricsListener();

      // Should notify when first listener is added
      expect(notifyCount, 1);
    });

    test('removeMetricsListener() handles multiple listeners correctly', () {
      final provider = CaptureProvider();
      var notifyCount = 0;
      provider.addListener(() => notifyCount++);

      // Add two listeners
      provider.addMetricsListener();
      provider.addMetricsListener();
      expect(notifyCount, 1); // Only first add triggers notification

      // Remove one listener - metrics should still be enabled
      provider.removeMetricsListener();
      // Provider still has one listener, so metrics are still enabled

      // Remove second listener - metrics now disabled
      provider.removeMetricsListener();

      // Verify count doesn't go negative
      provider.removeMetricsListener();
    });
  });

  group('metricsNotifyEnabled gating', () {
    test('metrics update does NOT call listeners when no metrics listeners registered', () {
      final provider = CaptureProvider();
      var notifyCount = 0;
      provider.addListener(() => notifyCount++);

      // Don't add any metrics listeners - should NOT notify on metrics update
      final initialCount = notifyCount;
      provider.calculateMetricsForTesting();

      // Should not have triggered additional notifications
      expect(notifyCount, initialCount);
    });

    test('metrics update DOES call listeners when at least one metrics listener registered', () {
      final provider = CaptureProvider();
      var notifyCount = 0;
      provider.addListener(() => notifyCount++);

      // Add a metrics listener - this triggers one notification
      provider.addMetricsListener();
      final countAfterAdd = notifyCount;

      // Now metrics update should notify
      provider.calculateMetricsForTesting();

      // Should have triggered an additional notification
      expect(notifyCount, greaterThan(countAfterAdd));
    });
  });

  group('segmentsPhotosVersion', () {
    test('increments on translation event', () {
      final provider = CaptureProvider();
      final segment = _segment('a', 'hello');
      provider.segments = [segment];

      final initialVersion = provider.segmentsPhotosVersion;

      // Simulate translation event
      provider.onMessageEventReceived(TranslationEvent(segments: [
        TranscriptSegment(
          id: 'a',
          text: 'hello (translated)',
          speaker: 'SPEAKER_00',
          isUser: false,
          personId: null,
          start: 0.0,
          end: 1.0,
          translations: [],
        ),
      ]));

      expect(provider.segmentsPhotosVersion, greaterThan(initialVersion));
    });

    test('increments on segments deleted event', () {
      final provider = CaptureProvider();
      provider.segments = [_segment('a', 'one'), _segment('b', 'two')];

      final initialVersion = provider.segmentsPhotosVersion;

      provider.onMessageEventReceived(SegmentsDeletedEvent(segmentIds: ['a']));

      expect(provider.segmentsPhotosVersion, greaterThan(initialVersion));
    });

    test('increments on new segment received', () {
      final provider = CaptureProvider();
      provider.segments = [_segment('seed', 'seed')];
      final initialVersion = provider.segmentsPhotosVersion;

      provider.onSegmentReceived([_segment('x', 'new')]);

      expect(provider.segmentsPhotosVersion, greaterThan(initialVersion));
    });

    test('increments on photo processing event and updates id', () {
      final provider = CaptureProvider();
      provider.photos = [
        ConversationPhoto(
          id: 'temp-photo',
          base64: 'img',
          createdAt: DateTime.now(),
        ),
      ];
      final initialVersion = provider.segmentsPhotosVersion;

      provider.onMessageEventReceived(PhotoProcessingEvent(tempId: 'temp-photo', photoId: 'permanent-photo'));

      expect(provider.photos.first.id, 'permanent-photo');
      expect(provider.segmentsPhotosVersion, greaterThan(initialVersion));
    });

    test('increments on photo described event and updates description', () {
      final provider = CaptureProvider();
      provider.photos = [
        ConversationPhoto(
          id: 'photo-1',
          base64: 'img',
          createdAt: DateTime.now(),
        ),
      ];
      final initialVersion = provider.segmentsPhotosVersion;

      provider.onMessageEventReceived(
        PhotoDescribedEvent(photoId: 'photo-1', description: 'desc', discarded: true),
      );

      expect(provider.photos.first.description, 'desc');
      expect(provider.photos.first.discarded, true);
      expect(provider.segmentsPhotosVersion, greaterThan(initialVersion));
    });
  });

  group('SpeakerLabelSuggestionEvent', () {
    test('ignores event when personId is empty', () {
      final provider = CaptureProvider();
      provider.segments = [_segment('seg1', 'hello')];

      // Empty personId: backend didn't assign, nothing happens
      final event = SpeakerLabelSuggestionEvent(
        speakerId: 0,
        personId: '',
        personName: 'Alice',
        segmentId: 'seg1',
      );

      provider.onMessageEventReceived(event);

      // Nothing stored, nothing applied
      expect(provider.suggestionsBySegmentId.containsKey('seg1'), false);
      expect(provider.segments.first.personId, isNull);
    });

    test('auto-applies assignment when personId is provided', () {
      final provider = CaptureProvider();
      // Create segment with speakerId 1 to match the event
      final segment = TranscriptSegment(
        id: 'seg1',
        text: 'hello',
        speaker: 'SPEAKER_01',
        isUser: false,
        personId: null,
        start: 0.0,
        end: 1.0,
        translations: [],
      );
      provider.segments = [segment];

      // New app path: personId is provided, auto-apply to segment
      final event = SpeakerLabelSuggestionEvent(
        speakerId: 1,
        personId: 'person-123',
        personName: 'Alice',
        segmentId: 'seg1',
      );

      provider.onMessageEventReceived(event);

      // Suggestion should NOT be stored (auto-applied instead)
      expect(provider.suggestionsBySegmentId.containsKey('seg1'), false);
      // Segment should be updated with personId
      expect(provider.segments.first.personId, 'person-123');
    });

    test('ignores suggestion for segments being tagged', () {
      final provider = CaptureProvider();
      provider.segments = [_segment('seg-tagging', 'text')];
      provider.taggingSegmentIds = ['seg-tagging'];

      final event = SpeakerLabelSuggestionEvent(
        speakerId: 1,
        personId: 'person-456',
        personName: 'Bob',
        segmentId: 'seg-tagging',
      );

      provider.onMessageEventReceived(event);

      // Should not store suggestion for segment being tagged
      expect(provider.suggestionsBySegmentId.containsKey('seg-tagging'), false);
    });

    test('ignores suggestion for already assigned segments', () {
      final provider = CaptureProvider();
      final assignedSegment = TranscriptSegment(
        id: 'seg-assigned',
        text: 'hello',
        speaker: 'SPEAKER_00',
        isUser: false,
        personId: 'existing-person',
        start: 0.0,
        end: 1.0,
        translations: [],
      );
      provider.segments = [assignedSegment];

      final event = SpeakerLabelSuggestionEvent(
        speakerId: 1,
        personId: 'new-person',
        personName: 'NewPerson',
        segmentId: 'seg-assigned',
      );

      provider.onMessageEventReceived(event);

      // Should not store suggestion for already assigned segment
      expect(provider.suggestionsBySegmentId.containsKey('seg-assigned'), false);
    });
  });

  group('People cache refresh', () {
    TranscriptSegment _segmentWithPerson(String id, String? personId) {
      return TranscriptSegment(
        id: id,
        text: 'text',
        speaker: 'SPEAKER_00',
        isUser: false,
        personId: personId,
        start: 0.0,
        end: 1.0,
        translations: [],
      );
    }

    test('triggers setPeople when segment has unknown personId', () {
      final provider = CaptureProvider();
      final mockPeopleProvider = MockPeopleProvider();
      provider.peopleProvider = mockPeopleProvider;

      // Pre-populate segments to skip platform-specific initialization code
      provider.segments = [_segmentWithPerson('seed', null)];

      // Segment with personId that's not in cache (cachedPeople is empty)
      final segments = [_segmentWithPerson('seg1', 'unknown-person-id')];

      provider.onSegmentReceived(segments);

      // Should have triggered setPeople
      expect(mockPeopleProvider.setPeopleCallCount, 1);
    });

    test('does not trigger refresh for segments without personId', () {
      final provider = CaptureProvider();
      final mockPeopleProvider = MockPeopleProvider();
      provider.peopleProvider = mockPeopleProvider;

      // Pre-populate segments to skip platform-specific initialization code
      provider.segments = [_segmentWithPerson('seed', null)];

      final segments = [_segmentWithPerson('seg2', null)];

      provider.onSegmentReceived(segments);

      // Should NOT trigger setPeople (no personId to check)
      expect(mockPeopleProvider.setPeopleCallCount, 0);
    });

    test('does not trigger multiple refreshes while one is in-flight', () async {
      final provider = CaptureProvider();
      final mockPeopleProvider = MockPeopleProvider();

      // Set up a completer to control when setPeople completes
      final completer = Completer<void>();
      mockPeopleProvider.setSetPeopleCompleter(completer);

      provider.peopleProvider = mockPeopleProvider;

      // Pre-populate segments to skip platform-specific initialization code
      provider.segments = [_segmentWithPerson('seed', null)];

      // First segment with unknown personId
      final segments1 = [_segmentWithPerson('seg-a', 'unknown-1')];
      provider.onSegmentReceived(segments1);

      // Should trigger first call
      expect(mockPeopleProvider.setPeopleCallCount, 1);

      // Second segment with different unknown personId while first is still in-flight
      final segments2 = [_segmentWithPerson('seg-b', 'unknown-2')];
      provider.onSegmentReceived(segments2);

      // Should NOT trigger another call (first is still in-flight)
      expect(mockPeopleProvider.setPeopleCallCount, 1);

      // Complete the first call
      completer.complete();
      await Future.delayed(Duration.zero); // Let the future complete

      // Third segment - now a new call should be allowed
      final segments3 = [_segmentWithPerson('seg-c', 'unknown-3')];
      provider.onSegmentReceived(segments3);

      // Should trigger a new call
      expect(mockPeopleProvider.setPeopleCallCount, 2);
    });
  });
}
