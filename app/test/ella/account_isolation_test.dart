import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_background_service_platform_interface/flutter_background_service_platform_interface.dart';
import 'package:flutter_sound/flutter_sound.dart';
import 'package:flutter_sound_platform_interface/flutter_sound_recorder_platform_interface.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/http_pool_manager.dart';
import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/memory.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/backend/schema/person.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/ella_account_isolation_service.dart';
import 'package:omi/ella/services/ella_logout_cache_purge.dart';
import 'package:omi/ella/services/elevenlabs_tts.dart';
import 'package:omi/ella/services/standard_voice_turn.dart';
import 'package:omi/ella/services/ella_workspace_status.dart';
import 'package:omi/env/env.dart';
import 'package:omi/pages/conversation_detail/conversation_detail_provider.dart';
import 'package:omi/providers/memories_provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/providers/people_provider.dart';
import 'package:omi/services/devices.dart';
import 'package:omi/services/devices/discovery/device_discoverer.dart';
import 'package:omi/services/services.dart';
import 'package:omi/services/wals/wal.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/platform/platform_manager.dart';
import 'package:omi/utils/wal_file_manager.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  Env.init();
  PlatformManager.initializeForTesting();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    WalFileManager.resetForTesting();
  });

  test('logout purge removes account caches and releases WAL ownership before the next login', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await prefs.saveString('email', 'account-a@example.com');
    await prefs.saveStringList('cachedConversations', ['conversation-a']);
    await prefs.saveString('cachedConversationsUid', 'uid-a');
    await prefs.saveStringList('cachedMessages', ['message-a']);
    await prefs.saveString('cachedMessagesUid', 'uid-a');
    await prefs.saveStringList('cachedMemories', ['memory-a']);
    await prefs.saveStringList('pendingMemories:uid-a', ['pending-a']);
    await prefs.saveStringList('cachedPeople:uid-a', ['person-a']);
    await prefs.saveString('modifiedConversationDetails:uid-a', 'conversation-detail-a');
    await prefs.saveString('emergencyContactName:uid-a', 'Account A contact');
    await prefs.saveString('emergencyContactPhone:uid-a', '+15555550100');
    await prefs.saveString('pendingEmergency:uid-a', 'pending-a');
    await prefs.saveString('ellaProvisioningReceipt:uid-a', '{"state":"ready"}');
    await prefs.saveString('ellaProvisioningVerifiedAt:uid-a', '2026-08-08T00:00:00Z');
    await _grantAuthority(prefs, 'uid-a');

    const owner = WalOwner(
      uid: 'uid-a',
      profileBindingId: 'profile-uid-a',
      bindingRevision: 3,
      consentReceiptId: 'aicr_uid-a',
      authorityGenerationAtCapture: 7,
    );
    final walRoot = await Directory.systemTemp.createTemp('ella_logout_wal_');
    addTearDown(() async {
      WalFileManager.resetForTesting();
      if (walRoot.existsSync()) await walRoot.delete(recursive: true);
    });
    await WalFileManager.init(baseDirectory: walRoot, activeOwner: owner);
    final ownerDirectory = Directory('${walRoot.path}/ella_wal_accounts/${owner.storageNamespace}');
    final retainedAudio = File('${ownerDirectory.path}/audio_account_a.bin');
    await retainedAudio.writeAsBytes([1, 2, 3]);

    expect(prefs.keysForTesting, isNotEmpty);
    expect(WalFileManager.activeOwnerForTesting?.uid, 'uid-a');

    await const EllaLogoutCachePurge().purge();
    await SharedPreferencesUtil.init(); // Simulate the next launch on the same device.

    expect(prefs.keysForTesting, isEmpty);
    expect(prefs.hasCurrentAiConsentAuthority(), isFalse);
    expect(WalFileManager.activeOwnerForTesting, isNull);
    expect(retainedAudio.existsSync(), isTrue, reason: 'logout must not silently delete recoverable owner data');

    prefs.uid = 'uid-b';
    await prefs.saveString('email', 'account-b@example.com');
    expect(prefs.cachedMessages, isEmpty);
    expect(prefs.pendingMemories, isEmpty);
    expect(prefs.cachedPeople, isEmpty);
    expect(prefs.modifiedConversationDetails, isNull);
    expect(prefs.emergencyContactName, isEmpty);
    expect(prefs.emergencyContactPhone, isEmpty);
    expect(prefs.pendingEmergency, isEmpty);
    final workspace = EllaWorkspaceStatus.current(preferences: prefs, uid: 'uid-b', email: 'account-b@example.com');
    expect(workspace.workspaceVerified, isFalse);
    expect(workspace.workspaceFingerprint, isEmpty);
  });

  test('logout purge does not release WAL authority when persisted cache clearing fails', () async {
    var releasedWalOwner = false;
    final purge = EllaLogoutCachePurge(
      clearPreferences: () async => throw StateError('storage unavailable'),
      releaseWalOwner: () async {
        releasedWalOwner = true;
      },
    );

    await expectLater(purge.purge(), throwsStateError);

    expect(releasedWalOwner, isFalse);
  });

  test('account-scoped caches never appear under the next account', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    final memory = _memory('memory-a', 'uid-a');
    final person = _person('person-a');
    prefs.pendingMemories = [memory];
    prefs.cachedPeople = [person];
    prefs.emergencyContactName = 'Account A contact';
    prefs.pendingEmergency = 'account-a-state';

    prefs.uid = 'uid-b';
    expect(prefs.pendingMemories, isEmpty);
    expect(prefs.cachedPeople, isEmpty);
    expect(prefs.emergencyContactName, isEmpty);
    expect(prefs.pendingEmergency, isEmpty);

    prefs.uid = 'uid-a';
    expect(prefs.pendingMemories.single.id, memory.id);
    expect(prefs.cachedPeople.single.id, person.id);
    expect(prefs.emergencyContactName, 'Account A contact');
  });

  test('legacy unowned caches are quarantined instead of assigned to the signed-in account', () async {
    await SharedPreferencesUtil().saveStringList('pendingMemories', [_memory('legacy', 'unknown').toJsonString()]);
    await SharedPreferencesUtil().saveString('emergencyContactName', 'Unknown owner');
    final prefs = SharedPreferencesUtil()..uid = 'uid-new';

    await prefs.quarantineLegacyAccountCaches();

    expect(prefs.pendingMemories, isEmpty);
    expect(prefs.emergencyContactName, isEmpty);
    expect(prefs.getStringList('ellaLegacyUnownedCache:pendingMemories'), isNotEmpty);
    expect(prefs.getString('ellaLegacyUnownedCache:emergencyContactName'), 'Unknown owner');
  });

  test('pending memory response after account switch does not clear or merge another owner cache', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantAuthority(prefs, 'uid-a');
    final pending = _memory('pending-a', 'uid-a');
    prefs.pendingMemories = [pending];
    final response = Completer<Memory?>();
    final provider = MemoriesProvider(
      preferences: prefs,
      createMemory: (content, visibility, category) => response.future,
    );

    final sync = provider.syncPendingMemories();
    await Future<void>.delayed(Duration.zero);
    prefs.uid = 'uid-b';
    response.complete(_memory('server-a', 'uid-a'));
    await sync;

    expect(prefs.pendingMemories, isEmpty);
    prefs.uid = 'uid-a';
    expect(prefs.pendingMemories.single.id, 'pending-a');
  });

  test('people response after account switch is not cached under the new account', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    final response = Completer<List<Person>>();
    final provider = PeopleProvider(
      preferences: prefs,
      fetchPeople: (_) => response.future,
      activeAuthority: () => _activeAuthority('uid-a', () => prefs.uid == 'uid-a'),
    );

    final pending = provider.setPeople();
    await _quiesce();
    prefs.uid = 'uid-b';
    response.complete([_person('person-a')]);
    await pending;

    expect(prefs.cachedPeople, isEmpty);
    expect(provider.people, isEmpty);
  });

  test('production People GET rejects same-UID lease drift before egress and recovers loading', () async {
    for (final drift in _PeopleLeaseDrift.values) {
      SharedPreferences.setMockInitialValues({});
      await SharedPreferencesUtil.init();
      final prefs = SharedPreferencesUtil()
        ..uid = 'uid-a'
        ..authToken = 'test-token'
        ..tokenExpirationTime = DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch;
      final cached = _person('cached-a');
      prefs.cachedPeople = [cached];
      final state = _PeopleLeaseState();
      final authority = _DriftingPeopleAuthority(state, drift);
      var requests = 0;
      HttpPoolManager.instance.replaceClientForTesting(
        MockClient((request) async {
          requests++;
          return http.Response('[]', 200);
        }),
      );
      final provider = PeopleProvider(preferences: prefs, activeAuthority: () => authority)..loading = true;

      await provider.setPeople();

      expect(requests, 0, reason: '$drift must fail before the real HTTP client send');
      expect(provider.loading, isFalse, reason: '$drift must not strand the current account loading state');
      expect(provider.people.single.id, cached.id);
      expect(prefs.cachedPeople.single.id, cached.id);
      expect(authority.checks, greaterThanOrEqualTo(4));
    }
  });

  test('stale production People GET completion cannot clear active next-account loading', () async {
    final prefs = SharedPreferencesUtil()
      ..uid = 'uid-a'
      ..authToken = 'test-token'
      ..tokenExpirationTime = DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch;
    var currentUid = 'uid-a';
    final firstStarted = Completer<void>();
    final secondStarted = Completer<void>();
    final firstResponse = Completer<http.Response>();
    final secondResponse = Completer<http.Response>();
    var requests = 0;
    HttpPoolManager.instance.replaceClientForTesting(
      MockClient((request) {
        requests++;
        if (requests == 1) {
          firstStarted.complete();
          return firstResponse.future;
        }
        secondStarted.complete();
        return secondResponse.future;
      }),
    );
    final provider = PeopleProvider(
      preferences: prefs,
      activeAuthority: () {
        final authorityUid = currentUid;
        return _activeAuthority(authorityUid, () => prefs.uid == authorityUid);
      },
    )..loading = true;

    final accountA = provider.setPeople();
    await firstStarted.future;
    prefs.uid = 'uid-b';
    currentUid = 'uid-b';
    final accountB = provider.setPeople();
    await secondStarted.future;

    firstResponse.complete(http.Response('[${jsonEncode(_personJson('person-a'))}]', 200));
    await accountA;
    expect(provider.loading, isTrue);
    expect(provider.people, isEmpty);
    expect(prefs.cachedPeople, isEmpty);

    secondResponse.complete(http.Response('[${jsonEncode(_personJson('person-b'))}]', 200));
    await accountB;
    expect(provider.loading, isFalse);
    expect(provider.people.single.id, 'person-b');
    expect(prefs.cachedPeople.single.id, 'person-b');
  });

  test('account switch during Ella chat streaming cannot mutate or persist under the next account', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantAuthority(prefs, 'uid-a');
    final controller = StreamController<ServerMessageChunk>();
    final started = Completer<void>();
    final provider = MessageProvider(
      activeAuthority: () => _activeAuthority('uid-a', () => prefs.uid == 'uid-a'),
      ellaChatStreamSender: (text, {expectedAuthenticatedUid, exactAuthority}) {
        started.complete();
        return controller.stream;
      },
    );

    final pending = provider.sendMessageStreamToServer('account A message');
    await started.future;
    await _quiesce();
    prefs.uid = 'uid-b';
    controller.add(ServerMessageChunk('message-a', 'stale response', MessageChunkType.data));
    await controller.close();
    await pending;

    expect(provider.messages, isEmpty);
    expect(prefs.cachedMessages, isEmpty);
  });

  test('typed backend failure is never rendered or persisted as Ella content', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantAuthority(prefs, 'uid-a');
    final provider = MessageProvider(
      activeAuthority: () => _activeAuthority('uid-a', () => true),
      aiConsentEnsurer: () async => true,
      ellaChatStreamSender: (text, {expectedAuthenticatedUid, exactAuthority}) async* {
        throw const ClientApiFailure(ClientApiFailureKind.workspaceRequired);
      },
    );

    await provider.sendMessageStreamToServer('private question');

    expect(provider.messages, isEmpty);
    expect(prefs.cachedMessages, isEmpty);
    expect(provider.lastStreamFailure?.kind, ClientApiFailureKind.workspaceRequired);
  });

  test('premature chat EOF cannot render cache or haptically present partial assistant content', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantAuthority(prefs, 'uid-a');
    var haptics = 0;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      SystemChannels.platform,
      (call) async {
        if (call.method == 'HapticFeedback.vibrate') haptics++;
        return null;
      },
    );
    addTearDown(
      () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
        SystemChannels.platform,
        null,
      ),
    );
    final provider = MessageProvider(
      activeAuthority: () => _activeAuthority('uid-a', () => true),
      aiConsentEnsurer: () async => true,
      ellaChatStreamSender: (text, {expectedAuthenticatedUid, exactAuthority}) async* {
        yield ServerMessageChunk('partial-a', 'must never appear', MessageChunkType.data);
      },
    );

    await provider.sendMessageStreamToServer('private question');

    expect(provider.messages, isEmpty);
    expect(prefs.cachedMessages, isEmpty);
    expect(provider.lastStreamFailure?.kind, ClientApiFailureKind.incompleteStream);
    expect(haptics, 0);
  });

  test('account switch during voice streaming cannot mutate or persist under the next account', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantAuthority(prefs, 'uid-a');
    final controller = StreamController<ServerMessageChunk>();
    final started = Completer<void>();
    final provider = MessageProvider(
      activeAuthority: () => _activeAuthority('uid-a', () => prefs.uid == 'uid-a'),
      voiceTempFileSaver: (bytes, startTime, frameSize) async =>
          File('${Directory.systemTemp.path}/ella-voice-authority-test.bin')..writeAsBytesSync([1, 2, 3]),
      voiceChatStreamSender: (files, {expectedAuthenticatedUid, exactAuthority}) {
        started.complete();
        return controller.stream;
      },
    );

    final pending = provider.sendVoiceMessageStreamToServer([
      [1, 2, 3],
    ]);
    await started.future;
    await _quiesce();
    prefs.uid = 'uid-b';
    controller.add(ServerMessageChunk('voice-a', 'stale voice response', MessageChunkType.data));
    await controller.close();
    await pending;

    expect(provider.messages, isEmpty);
    expect(prefs.cachedMessages, isEmpty);
  });

  test('premature voice EOF cannot render cache or announce partial assistant content', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantAuthority(prefs, 'uid-a');
    var firstChunks = 0;
    final audioFile = File('${Directory.systemTemp.path}/ella-voice-incomplete-test.bin');
    addTearDown(() async {
      if (await audioFile.exists()) await audioFile.delete();
    });
    final provider = MessageProvider(
      activeAuthority: () => _activeAuthority('uid-a', () => true),
      voiceTempFileSaver: (bytes, startTime, frameSize) async => audioFile..writeAsBytesSync([1, 2, 3]),
      voiceChatStreamSender: (files, {expectedAuthenticatedUid, exactAuthority}) async* {
        yield ServerMessageChunk('partial-a', 'must never appear', MessageChunkType.data);
      },
    );

    await provider.sendVoiceMessageStreamToServer(
      [
        [1, 2, 3],
      ],
      onFirstChunkRecived: () => firstChunks++,
    );

    expect(provider.messages, isEmpty);
    expect(prefs.cachedMessages, isEmpty);
    expect(provider.lastStreamFailure?.kind, ClientApiFailureKind.incompleteStream);
    expect(firstChunks, 0);
  });

  test('account switch during conversation reprocessing cannot persist account A result under B', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    final response = Completer<ServerConversation?>();
    final started = Completer<void>();
    final provider = ConversationDetailProvider(
      preferences: prefs,
      activeAuthority: () => _activeAuthority('uid-a', () => prefs.uid == 'uid-a'),
      reprocessConversation: (id, {appId}) {
        started.complete();
        return response.future;
      },
    )..setCachedConversation(_conversation('conversation-a', 'before'));

    final pending = provider.reprocessConversation();
    await started.future;
    await _quiesce();
    prefs.uid = 'uid-b';
    response.complete(_conversation('conversation-a', 'stale account A result'));

    expect(await pending, isFalse);
    expect(provider.loadingReprocessConversation, isFalse);
    expect(prefs.modifiedConversationDetails, isNull);
  });

  test('capture, V2V reconnect, Guardian poll, WAL and legacy quarantine stop in order', () async {
    final calls = <String>[];
    final service = EllaAccountIsolationService(
      stopNotificationAudio: () => calls.add('notification-audio'),
      clearGuardianNotifications: () => calls.add('notification-residue'),
      stopCapture: () => calls.add('capture'),
      stopV2v: () => calls.add('v2v'),
      stopGuardian: () => calls.add('guardian'),
      stopServices: () => calls.add('wal-services'),
      quarantineLegacy: () => calls.add('quarantine'),
    );

    await service.stopForAccountTransition();

    expect(calls, [
      'guardian',
      'notification-audio',
      'notification-residue',
      'capture',
      'v2v',
      'wal-services',
      'quarantine',
    ]);
  });

  test('identity transition waits for a capture producer that is mid-write', () async {
    final writeStarted = Completer<void>();
    final releaseWrite = Completer<void>();
    var identity = 'uid-a';
    final token = EllaAccountIsolationService.registerCaptureProducer(() async {
      writeStarted.complete();
      await releaseWrite.future;
    });
    addTearDown(() => EllaAccountIsolationService.unregisterCaptureProducer(token));

    final transition = () async {
      await EllaAccountIsolationService(
        stopNotificationAudio: () {},
        clearGuardianNotifications: () {},
        stopV2v: () {},
        stopGuardian: () {},
        stopServices: () {},
        quarantineLegacy: () {},
      ).stopForAccountTransition();
      identity = 'uid-b';
    }();

    await writeStarted.future;
    await Future<void>.delayed(Duration.zero);
    expect(identity, 'uid-a');
    releaseWrite.complete();
    await transition;
    expect(identity, 'uid-b');
  });

  test('file picker started under A cannot upload or attach after switch to B', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantAuthority(prefs, 'uid-a');
    final picker = Completer<List<File>>();
    var uploads = 0;
    final provider = MessageProvider(
      activeAuthority: () => _activeAuthority('uid-a', () => prefs.uid == 'uid-a'),
      filePicker: (_) => picker.future,
      fileUploader: (files, appId, expectedUid) async {
        uploads++;
        return [_messageFile('attachment-a')];
      },
    );

    final pending = provider.selectFile();
    await Future<void>.delayed(Duration.zero);
    prefs.uid = 'uid-b';
    await _quiesce();
    picker.complete([File('${Directory.systemTemp.path}/account-a-private.txt')]);
    await pending;

    expect(uploads, 0);
    expect(provider.selectedFiles, isEmpty);
    expect(provider.uploadedFiles, isEmpty);
  });

  test('delayed consent and paste work retain the entry authority and never read under drift', () async {
    var current = true;
    final consent = Completer<bool>();
    var pasteReads = 0;
    final provider = MessageProvider(
      activeAuthority: () => _activeAuthority('uid-a', () => current),
      aiConsentEnsurer: () => consent.future,
    );

    final pending = provider.runProtectedOperationAtEntry((operation) async {
      pasteReads++;
      expect(operation.isCurrent, isTrue);
    });
    await Future<void>.delayed(Duration.zero);
    current = false;
    consent.complete(true);
    await pending;

    expect(pasteReads, 0);
  });

  test('desktop Ask-AI carries one A lease across attachment upload and stream start', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantAuthority(prefs, 'uid-a');
    final uploadStarted = Completer<void>();
    final uploadResult = Completer<List<MessageFile>?>();
    var streamStarts = 0;
    final emitted = <Map<String, dynamic>>[];
    final provider = MessageProvider(
      activeAuthority: () => _activeAuthority('uid-a', () => prefs.uid == 'uid-a'),
      fileUploader: (files, appId, expectedUid) {
        expect(expectedUid.uid, 'uid-a');
        uploadStarted.complete();
        return uploadResult.future;
      },
      askAiStreamSender: (message, fileIds, expectedUid) {
        streamStarts++;
        return Stream.value(ServerMessageChunk('stale', 'must not emit', MessageChunkType.data));
      },
      askAiResponseSink: (chunk) => emitted.add(chunk),
    );

    final pending = provider.handleAskAIForTesting(
      MethodCall('sendQuery', {
        'message': 'Account A question',
        'filePath': '${Directory.systemTemp.path}/account-a-private.txt',
      }),
    );
    await uploadStarted.future;
    prefs.uid = 'uid-b';
    await _quiesce();
    uploadResult.complete([_messageFile('attachment-a')]);
    await pending;

    expect(streamStarts, 0);
    expect(emitted, isEmpty);
    expect(provider.uploadedFiles, isEmpty);
  });

  test('people create response started under A cannot commit under B', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    final response = Completer<Person?>();
    final provider = PeopleProvider(
      preferences: prefs,
      activeAuthority: () => _activeAuthority('uid-a', () => prefs.uid == 'uid-a'),
      createPersonRequest: (name, expectedUid) => response.future,
    );

    final pending = provider.createPersonProvider('Account A person');
    await Future<void>.delayed(Duration.zero);
    prefs.uid = 'uid-b';
    await _quiesce();
    response.complete(_person('person-a'));
    expect(await pending, isNull);
    expect(provider.people, isEmpty);
    expect(prefs.cachedPeople, isEmpty);
  });

  test('stale people create reset clears loading so the next account can mutate', () async {
    var currentUid = 'uid-a';
    final firstResponse = Completer<Person?>();
    var calls = 0;
    final provider = PeopleProvider(
      activeAuthority: () => _activeAuthority(currentUid, () => true),
      createPersonRequest: (name, authority) {
        calls++;
        if (calls == 1) return firstResponse.future;
        return Future.value(_person('person-b'));
      },
    );

    final first = provider.createPersonProvider('Account A person');
    await Future<void>.delayed(Duration.zero);
    provider.reset();
    currentUid = 'uid-b';
    firstResponse.complete(_person('person-a'));
    expect(await first, isNull);
    expect(provider.loading, isFalse);

    final second = await provider.createPersonProvider('Account B person');
    expect(second?.id, 'person-b');
    expect(provider.loading, isFalse);
  });

  test('device transition awaits every asynchronous discoverer shutdown', () async {
    final release = Completer<void>();
    final first = _DelayedDiscoverer(release.future);
    final second = _DelayedDiscoverer(release.future);
    final service = DeviceService(discoverers: [first, second]);
    var stopped = false;

    final pending = service.stop().then((_) => stopped = true);
    await Future<void>.delayed(Duration.zero);
    expect(first.stopCalls, 1);
    expect(second.stopCalls, 1);
    expect(stopped, isFalse);
    release.complete();
    await pending;
    expect(stopped, isTrue);
  });

  test('mobile mic transition waits for an in-flight start and suppresses its late callback', () async {
    final runner = _DelayedRecorderRunner();
    final service = MicRecorderBackgroundService(runner: runner);
    var frames = 0;
    final start = service.start(onByteReceived: (_) => frames++);
    await runner.startEntered.future;

    final transition = service.stopForAccountTransition();
    runner.emit([1, 2, 3]);
    expect(frames, 0);
    var transitionCompleted = false;
    transition.then((_) => transitionCompleted = true);
    await Future<void>.delayed(Duration.zero);
    expect(transitionCompleted, isFalse);

    runner.releaseStart.complete();
    await Future.wait([start, transition]);
    expect(runner.stopCalls, greaterThanOrEqualTo(1));
  });

  test('mobile mic stop timeout propagates and cannot be treated as transition success', () async {
    final service = MicRecorderBackgroundService(runner: _TimeoutRecorderRunner());
    await expectLater(service.stopForAccountTransition(), throwsA(isA<TimeoutException>()));
  });

  test('production isolate bridge awaits physical stop and fences late producers before transition mutation', () async {
    final fixture = _ProductionRecorderFixture(stopTimeout: const Duration(seconds: 1), holdNativeStop: true);
    addTearDown(fixture.dispose);
    var frames = 0;
    await fixture.mic.start(onByteReceived: (_) => frames++);
    await fixture.native.startEntered.future;
    final token = EllaAccountIsolationService.registerCaptureProducer(fixture.mic.stopForAccountTransition);
    addTearDown(() => EllaAccountIsolationService.unregisterCaptureProducer(token));
    var identityMutated = false;

    final transition = _accountBarrier().stopForAccountTransition().then((_) => identityMutated = true);
    await fixture.native.stopEntered.future;
    await Future<void>.delayed(Duration.zero);
    expect(identityMutated, isFalse);
    expect(fixture.background.stopResults, isEmpty);

    fixture.native.releaseStop();
    await transition;
    expect(identityMutated, isTrue);
    expect(fixture.background.stopResults.single['status'], 'stopped');

    fixture.native.emit(Uint8List.fromList([1, 2, 3]));
    fixture.background.invoke('recorder.start', {'generation': 1});
    await Future<void>.delayed(Duration.zero);
    expect(frames, 0);
    expect(fixture.native.startCalls, 1);
  });

  test('production isolate bridge propagates native stop error and blocks transition mutation', () async {
    final fixture = _ProductionRecorderFixture(
      stopTimeout: const Duration(seconds: 1),
      nativeStopError: StateError('physical recorder stop failed'),
    );
    addTearDown(fixture.dispose);
    var frames = 0;
    await fixture.mic.start(onByteReceived: (_) => frames++);
    await fixture.native.startEntered.future;
    final token = EllaAccountIsolationService.registerCaptureProducer(fixture.mic.stopForAccountTransition);
    addTearDown(() => EllaAccountIsolationService.unregisterCaptureProducer(token));
    var identityMutated = false;

    final transition = _accountBarrier().stopForAccountTransition().then((_) => identityMutated = true);
    await expectLater(transition, throwsA(isA<BackgroundRecorderStopException>()));

    expect(identityMutated, isFalse);
    expect(fixture.background.stopResults.single['status'], 'error');
    expect(fixture.background.stopResults.single['error'], contains('physical recorder stop failed'));
    fixture.native.emit(Uint8List.fromList([1, 2, 3]));
    expect(frames, 0);
  });

  test('production isolate bridge timeout fails closed before transition mutation', () async {
    final fixture = _ProductionRecorderFixture(stopTimeout: const Duration(milliseconds: 30), holdNativeStop: true);
    addTearDown(fixture.dispose);
    await fixture.mic.start(onByteReceived: (_) {});
    await fixture.native.startEntered.future;
    final token = EllaAccountIsolationService.registerCaptureProducer(fixture.mic.stopForAccountTransition);
    addTearDown(() => EllaAccountIsolationService.unregisterCaptureProducer(token));
    var identityMutated = false;

    final transition = _accountBarrier().stopForAccountTransition().then((_) => identityMutated = true);
    await fixture.native.stopEntered.future;
    await expectLater(transition, throwsA(isA<TimeoutException>()));

    expect(identityMutated, isFalse);
    expect(fixture.background.stopResults, isEmpty);
    fixture.native.releaseStop();
    await fixture.background.waitForStopResult();
  });

  test('desktop transition cancels and awaits a start paused in the native state callback', () async {
    const channel = MethodChannel('ella.capture.quiescence.test');
    final isRecordingEntered = Completer<void>();
    final releaseIsRecording = Completer<void>();
    var nativeStarts = 0;
    var nativeStops = 0;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
      switch (call.method) {
        case 'isRecording':
          isRecordingEntered.complete();
          await releaseIsRecording.future;
          return false;
        case 'start':
          nativeStarts++;
          return null;
        case 'stop':
          nativeStops++;
          return null;
      }
      return null;
    });
    addTearDown(
      () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, null),
    );
    final service = DesktopSystemAudioRecorderService(channel: channel);
    var callbacks = 0;
    final start = service.start(onByteReceived: (_) => callbacks++, onFormatReceived: (_) {});
    await isRecordingEntered.future;
    final transition = service.stopForAccountTransition();
    releaseIsRecording.complete();
    await Future.wait([start, transition]);

    expect(nativeStarts, 0);
    expect(nativeStops, 1);
    expect(callbacks, 0);
  });

  test('desktop transition fails closed when native stop is not acknowledged', () async {
    const channel = MethodChannel('ella.capture.stop-failure.test');
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
      if (call.method == 'stop') throw TimeoutException('native system-audio stop not acknowledged');
      return null;
    });
    addTearDown(
      () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, null),
    );

    final service = DesktopSystemAudioRecorderService(channel: channel);
    await expectLater(service.stopForAccountTransition(), throwsA(anything));
  });

  test('real JSON and multipart helpers close same-UID generation drift before egress', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    final originalGeneration = prefs.aiConsentAuthorityGeneration;
    var requests = 0;
    HttpPoolManager.instance.replaceClientForTesting(
      MockClient((request) async {
        requests++;
        return http.Response('{}', 200);
      }),
    );
    const url = 'https://production-boundary.invalid/protected';

    final jsonAuthority = _GenerationChangingAuthority(prefs: prefs, mutateAfterCheck: 2);
    await expectLater(
      makeApiCall(
        url: url,
        headers: const {},
        body: jsonEncode({'private': true}),
        method: 'POST',
        exactAuthority: jsonAuthority,
      ),
      throwsStateError,
    );

    final file = File('${Directory.systemTemp.path}/ella-exact-authority-upload.txt')..writeAsStringSync('private');
    addTearDown(() async {
      if (await file.exists()) await file.delete();
    });
    final multipartAuthority = _GenerationChangingAuthority(prefs: prefs, mutateAfterCheck: 3);
    await expectLater(
      makeMultipartApiCall(url: url, files: [file], exactAuthority: multipartAuthority),
      throwsStateError,
    );
    await Future<void>.delayed(const Duration(milliseconds: 20));
    expect(requests, 0);
    expect(jsonAuthority.uid, multipartAuthority.uid);
    expect(prefs.uid, 'uid-a');
    expect(prefs.aiConsentAuthorityGeneration, greaterThan(originalGeneration));
  });

  test('real streaming helper verifies exact authority throughout response delivery', () async {
    final sendStarted = Completer<void>();
    HttpPoolManager.instance.replaceClientForTesting(_StreamingTestClient(sendStarted));
    final authority = _MutableExactAuthority();
    final pending = makeStreamingApiCall(
      url: 'https://production-boundary.invalid/stream',
      method: 'GET',
      exactAuthority: authority,
    ).toList();
    final expectation = expectLater(pending, throwsStateError);
    await sendStarted.future;
    authority.current = false;
    await expectation;

    expect(authority.checks, greaterThanOrEqualTo(4));
  });

  test('standard voice production turn discards a late account-A stream before cache TTS or playback', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantAuthority(prefs, 'uid-a');
    var current = true;
    final streamStarted = Completer<void>();
    final releaseStream = Completer<void>();
    var syntheses = 0;
    var playbacks = 0;
    var invalidations = 0;
    StandardVoiceTurnResult? result;
    final provider = MessageProvider(activeAuthority: () => _activeAuthority('uid-a', () => current));
    final coordinator = StandardVoiceTurnCoordinator(
      streamSender: (text, {expectedAuthenticatedUid, exactAuthority}) async* {
        expect(expectedAuthenticatedUid, 'uid-a');
        expect(exactAuthority?.uid, 'uid-a');
        streamStarted.complete();
        await releaseStream.future;
        yield ServerMessageChunk('reply-a', 'Account A reply', MessageChunkType.data);
      },
      synthesizer: (text, {expectedAuthenticatedUid, exactAuthority}) async {
        syntheses++;
        return '${Directory.systemTemp.path}/must-not-exist.mp3';
      },
    );

    final pending = provider.runProtectedOperationAtEntry((operation) async {
      result = await coordinator.run(
        transcript: 'Account A private question',
        authority: operation.exactAuthority,
        commitMessages: (transcript, reply) => provider.addVoiceMessagesForProtectedOperation(
          _voiceMessage('user-a', transcript, MessageSender.human),
          _voiceMessage('assistant-a', reply, MessageSender.ai),
          operation,
        ),
        onReplyReady: (_) {},
        playFile: (_) async => playbacks++,
        speakOnDevice: (_) async => playbacks++,
      );
    }, onInvalidated: () => invalidations++);
    await streamStarted.future;
    current = false;
    prefs.uid = 'uid-b';
    await _quiesce();
    releaseStream.complete();
    await pending;

    expect(result?.discarded, isTrue);
    expect(provider.messages, isEmpty);
    expect(prefs.cachedMessages, isEmpty);
    expect(syntheses, 0);
    expect(playbacks, 0);
    expect(invalidations, 1);
  });

  test('standard voice production turn deletes late A audio and never plays it after authority loss', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantAuthority(prefs, 'uid-a');
    var current = true;
    final synthesisStarted = Completer<void>();
    final releaseSynthesis = Completer<String?>();
    final staleAudio = File('${Directory.systemTemp.path}/ella_tts_stale_account_a.mp3');
    addTearDown(() async {
      if (await staleAudio.exists()) await staleAudio.delete();
    });
    var playbacks = 0;
    StandardVoiceTurnResult? result;
    final provider = MessageProvider(activeAuthority: () => _activeAuthority('uid-a', () => current));
    final coordinator = StandardVoiceTurnCoordinator(
      streamSender: (text, {expectedAuthenticatedUid, exactAuthority}) => _completedVoiceStream('Account A reply'),
      synthesizer: (text, {expectedAuthenticatedUid, exactAuthority}) {
        synthesisStarted.complete();
        return releaseSynthesis.future;
      },
    );

    final pending = provider.runProtectedOperationAtEntry((operation) async {
      result = await coordinator.run(
        transcript: 'Account A private question',
        authority: operation.exactAuthority,
        commitMessages: (transcript, reply) => provider.addVoiceMessagesForProtectedOperation(
          _voiceMessage('user-a', transcript, MessageSender.human),
          _voiceMessage('assistant-a', reply, MessageSender.ai),
          operation,
        ),
        onReplyReady: (_) {},
        playFile: (_) async => playbacks++,
        speakOnDevice: (_) async => playbacks++,
      );
    });
    await synthesisStarted.future;
    await staleAudio.writeAsBytes([1, 2, 3], flush: true);
    current = false;
    prefs.uid = 'uid-b';
    await _quiesce();
    releaseSynthesis.complete(staleAudio.path);
    await pending;

    expect(result?.discarded, isTrue);
    expect(provider.messages, isEmpty);
    expect(prefs.cachedMessages, isEmpty);
    expect(await staleAudio.exists(), isFalse);
    expect(playbacks, 0);
  });

  test('standard voice production turn commits and plays under unchanged exact authority', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantAuthority(prefs, 'uid-a');
    final audio = File('${Directory.systemTemp.path}/ella_tts_current_account_a.mp3')..writeAsBytesSync([1, 2, 3]);
    addTearDown(() async {
      if (await audio.exists()) await audio.delete();
    });
    var playbacks = 0;
    StandardVoiceTurnResult? result;
    final provider = MessageProvider(activeAuthority: () => _activeAuthority('uid-a', () => true));
    final coordinator = StandardVoiceTurnCoordinator(
      streamSender: (text, {expectedAuthenticatedUid, exactAuthority}) => _completedVoiceStream('Current reply'),
      synthesizer: (text, {expectedAuthenticatedUid, exactAuthority}) async => audio.path,
    );

    await provider.runProtectedOperationAtEntry((operation) async {
      result = await coordinator.run(
        transcript: 'Current question',
        authority: operation.exactAuthority,
        commitMessages: (transcript, reply) => provider.addVoiceMessagesForProtectedOperation(
          _voiceMessage('user-current', transcript, MessageSender.human),
          _voiceMessage('assistant-current', reply, MessageSender.ai),
          operation,
        ),
        onReplyReady: (_) {},
        playFile: (path) async {
          expect(path, audio.path);
          playbacks++;
        },
        speakOnDevice: (_) async => fail('current-authority control must use synthesized audio'),
      );
    });

    expect(result?.discarded, isFalse);
    expect(result?.reply, 'Current reply');
    expect(provider.messages.map((message) => message.text), ['Current question', 'Current reply']);
    expect(prefs.cachedMessages.map((message) => message.text), ['Current question', 'Current reply']);
    expect(playbacks, 1);
  });

  test('typed backend failure is never committed or spoken by standard voice', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantAuthority(prefs, 'uid-a');
    var commits = 0;
    var syntheses = 0;
    var playbacks = 0;
    final coordinator = StandardVoiceTurnCoordinator(
      streamSender: (text, {expectedAuthenticatedUid, exactAuthority}) async* {
        throw const ClientApiFailure(ClientApiFailureKind.workspaceRequired);
      },
      synthesizer: (text, {expectedAuthenticatedUid, exactAuthority}) async {
        syntheses++;
        return null;
      },
    );

    final result = await coordinator.run(
      transcript: 'private question',
      authority: _activeAuthority('uid-a', () => true),
      commitMessages: (_, __) {
        commits++;
        return true;
      },
      onReplyReady: (_) {},
      playFile: (_) async => playbacks++,
      speakOnDevice: (_) async => playbacks++,
    );

    expect(result.failure?.kind, ClientApiFailureKind.workspaceRequired);
    expect(result.reply, isEmpty);
    expect(commits, 0);
    expect(syntheses, 0);
    expect(playbacks, 0);
  });

  test('premature standard voice EOF is never committed synthesized or spoken', () async {
    var commits = 0;
    var replies = 0;
    var syntheses = 0;
    var playbacks = 0;
    final coordinator = StandardVoiceTurnCoordinator(
      streamSender: (text, {expectedAuthenticatedUid, exactAuthority}) async* {
        yield ServerMessageChunk('partial-a', 'must never be spoken', MessageChunkType.data);
      },
      synthesizer: (text, {expectedAuthenticatedUid, exactAuthority}) async {
        syntheses++;
        return null;
      },
    );

    final result = await coordinator.run(
      transcript: 'private question',
      authority: _activeAuthority('uid-a', () => true),
      commitMessages: (_, __) {
        commits++;
        return true;
      },
      onReplyReady: (_) => replies++,
      playFile: (_) async => playbacks++,
      speakOnDevice: (_) async => playbacks++,
    );

    expect(result.failure?.kind, ClientApiFailureKind.incompleteStream);
    expect(result.reply, isEmpty);
    expect(commits, 0);
    expect(replies, 0);
    expect(syntheses, 0);
    expect(playbacks, 0);
  });

  test('real TTS HTTP path aborts before file creation when exact authority changes during response', () async {
    final prefs = SharedPreferencesUtil()
      ..uid = 'uid-a'
      ..authToken = 'test-token'
      ..tokenExpirationTime = DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch;
    await _grantAuthority(prefs, 'uid-a');
    final responseStarted = Completer<void>();
    final response = Completer<http.Response>();
    final authority = _MutableExactAuthority();
    HttpPoolManager.instance.replaceClientForTesting(
      MockClient((request) {
        responseStarted.complete();
        return response.future;
      }),
    );

    final pending = ElevenLabsTts.synthesize('Account A private reply', exactAuthority: authority);
    final expectation = expectLater(pending, throwsStateError);
    await responseStarted.future;
    authority.current = false;
    response.complete(http.Response.bytes([1, 2, 3], 200, headers: {'content-type': 'audio/mpeg'}));
    await expectation;

    expect(authority.checks, greaterThanOrEqualTo(4));
  });

  test('people update, sample delete, and person delete discard A responses after switch', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    final original = _person('person-a');
    prefs.cachedPeople = [original];
    final updateResult = Completer<bool>();
    final sampleResult = Completer<bool>();
    final deleteResult = Completer<bool>();
    final provider = PeopleProvider(
      preferences: prefs,
      activeAuthority: () => _activeAuthority('uid-a', () => prefs.uid == 'uid-a'),
      updatePersonRequest: (personId, name, expectedUid) => updateResult.future,
      deleteSampleRequest: (personId, sampleIndex, expectedUid) => sampleResult.future,
      deletePersonRequest: (personId, expectedUid) => deleteResult.future,
    );

    final update = provider.updatePersonProvider(original, 'Changed under A');
    await Future<void>.delayed(Duration.zero);
    prefs.uid = 'uid-b';
    await _quiesce();
    updateResult.complete(true);
    await update;
    expect(provider.people, isEmpty);

    prefs.uid = 'uid-a';
    prefs.cachedPeople = [original];
    final sampleProvider = PeopleProvider(
      preferences: prefs,
      activeAuthority: () => _activeAuthority('uid-a', () => prefs.uid == 'uid-a'),
      deleteSampleRequest: (personId, sampleIndex, expectedUid) => sampleResult.future,
    );
    final sample = sampleProvider.deletePersonSample(0, 0);
    await Future<void>.delayed(Duration.zero);
    prefs.uid = 'uid-b';
    await _quiesce();
    sampleResult.complete(true);
    await sample;
    expect(sampleProvider.people, isEmpty);

    prefs.uid = 'uid-a';
    prefs.cachedPeople = [original];
    final deleteProvider = PeopleProvider(
      preferences: prefs,
      activeAuthority: () => _activeAuthority('uid-a', () => prefs.uid == 'uid-a'),
      deletePersonRequest: (personId, expectedUid) => deleteResult.future,
    );
    final deletion = deleteProvider.deletePersonProvider(original);
    await Future<void>.delayed(Duration.zero);
    prefs.uid = 'uid-b';
    await _quiesce();
    deleteResult.complete(true);
    await deletion;
    expect(deleteProvider.people, isEmpty);
  });

  test('workspace proof is opaque and route states remain honest without receipts', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-private-value';
    await prefs.saveString('aiConsentProfileBindingId', 'profile-private-value');
    await prefs.saveEllaProvisioningReceipt('uid-private-value', {
      'state': 'ready',
      'binding_state': 'active',
      'binding_revision': 8,
      'effective_policy_revision': 'policy-8',
      'runtime_provider': 'self_hosted_hermes',
      'runtime_status': 'ready',
    });
    await prefs.markEllaProvisioningVerified('uid-private-value', at: DateTime.utc(2026, 8, 2));

    final status = EllaWorkspaceStatus.current(preferences: prefs, uid: 'uid-private-value', email: 'new@example.test');

    expect(status.workspaceVerified, isTrue);
    expect(status.workspaceFingerprint, matches(RegExp(r'^[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}$')));
    expect(status.workspaceFingerprint, isNot(contains('uid-private-value')));
    expect(status.chat, EllaRouteVerification.notVerified);
    expect(status.voice, EllaRouteVerification.notVerified);
    expect(status.whispers, EllaRouteVerification.notVerified);
    expect(status.quarantinedAudioCount, 0);
  });
}

Map<String, dynamic> _personJson(String id) => _person(id).toJson();

enum _PeopleLeaseDrift { generation, reprovision, revocation }

class _PeopleLeaseState {
  String uid = 'uid-a';
  String profileBindingId = 'profile-a';
  int bindingRevision = 3;
  String consentReceiptId = 'aicr_uid-a';
  bool consentCurrent = true;
  int authorityGeneration = 7;
}

class _DriftingPeopleAuthority implements AccountCommitAuthority {
  _DriftingPeopleAuthority(this.state, this.drift)
      : _uid = state.uid,
        _profileBindingId = state.profileBindingId,
        _bindingRevision = state.bindingRevision,
        _consentReceiptId = state.consentReceiptId,
        _authorityGeneration = state.authorityGeneration;

  final _PeopleLeaseState state;
  final _PeopleLeaseDrift drift;
  final String _uid;
  final String _profileBindingId;
  final int _bindingRevision;
  final String _consentReceiptId;
  final int _authorityGeneration;
  int checks = 0;

  @override
  String get uid => _uid;

  @override
  bool isCurrent() {
    checks++;
    final current = state.uid == _uid &&
        state.profileBindingId == _profileBindingId &&
        state.bindingRevision == _bindingRevision &&
        state.consentReceiptId == _consentReceiptId &&
        state.consentCurrent &&
        state.authorityGeneration == _authorityGeneration;
    if (checks == 3) {
      switch (drift) {
        case _PeopleLeaseDrift.generation:
          state.authorityGeneration++;
          break;
        case _PeopleLeaseDrift.reprovision:
          state.bindingRevision++;
          break;
        case _PeopleLeaseDrift.revocation:
          state.consentCurrent = false;
          break;
      }
    }
    return current;
  }

  @override
  bool isExactCurrent() => isCurrent();
}

EllaAccountIsolationService _accountBarrier() => EllaAccountIsolationService(
      stopNotificationAudio: () {},
      clearGuardianNotifications: () {},
      stopV2v: () {},
      stopGuardian: () {},
      stopServices: () {},
      quarantineLegacy: () {},
    );

class _ProductionRecorderFixture {
  _ProductionRecorderFixture({required Duration stopTimeout, bool holdNativeStop = false, Object? nativeStopError})
      : native = _ControlledFlutterSoundRecorderPlatform(holdStop: holdNativeStop, stopError: nativeStopError),
        background = _FakeBackgroundServicePlatform() {
    _originalRecorderPlatform = FlutterSoundRecorderPlatform.instance;
    FlutterSoundRecorderPlatform.instance = native;
    FlutterBackgroundServicePlatform.instance = background;
    mic = MicRecorderBackgroundService(runner: BackgroundService(recorderStopTimeout: stopTimeout));
  }

  final _ControlledFlutterSoundRecorderPlatform native;
  final _FakeBackgroundServicePlatform background;
  late final FlutterSoundRecorderPlatform _originalRecorderPlatform;
  late final MicRecorderBackgroundService mic;

  Future<void> dispose() async {
    native.releaseStop();
    background.shutdown();
    await Future<void>.delayed(Duration.zero);
    FlutterSoundRecorderPlatform.instance = _originalRecorderPlatform;
  }
}

class _FakeBackgroundServicePlatform extends FlutterBackgroundServicePlatform {
  final Map<String, StreamController<Map<String, dynamic>?>> _isolateStreams = {};
  final Map<String, StreamController<Map<String, dynamic>?>> _uiStreams = {};
  final List<Map<String, dynamic>> stopResults = [];
  final Completer<void> _stopResultReceived = Completer<void>();
  late final _FakeServiceInstance _serviceInstance = _FakeServiceInstance(this);
  Function(ServiceInstance service)? _onStart;
  bool _running = false;

  StreamController<Map<String, dynamic>?> _controller(
    Map<String, StreamController<Map<String, dynamic>?>> streams,
    String method,
  ) =>
      streams.putIfAbsent(method, StreamController<Map<String, dynamic>?>.broadcast);

  @override
  Future<bool> configure({
    required IosConfiguration iosConfiguration,
    required AndroidConfiguration androidConfiguration,
  }) async {
    _onStart = androidConfiguration.onStart;
    return true;
  }

  @override
  Future<bool> start() async {
    _running = true;
    _onStart?.call(_serviceInstance);
    return true;
  }

  @override
  Future<bool> isServiceRunning() async => _running;

  @override
  void invoke(String method, [Map<String, dynamic>? args]) {
    _controller(_isolateStreams, method).add(args);
  }

  @override
  Stream<Map<String, dynamic>?> on(String method) => _controller(_uiStreams, method).stream;

  void emitToUi(String method, Map<String, dynamic>? args) {
    if (method == 'recorder.ui.stopResult' && args != null) {
      stopResults.add(args);
      if (!_stopResultReceived.isCompleted) _stopResultReceived.complete();
    }
    _controller(_uiStreams, method).add(args);
  }

  Stream<Map<String, dynamic>?> isolateStream(String method) => _controller(_isolateStreams, method).stream;

  Future<void> waitForStopResult() => _stopResultReceived.future;

  void shutdown() => invoke('stop');
}

class _FakeServiceInstance implements ServiceInstance {
  _FakeServiceInstance(this.platform);

  final _FakeBackgroundServicePlatform platform;

  @override
  void invoke(String method, [Map<String, dynamic>? args]) => platform.emitToUi(method, args);

  @override
  Stream<Map<String, dynamic>?> on(String method) => platform.isolateStream(method);

  @override
  Future<void> stopSelf() async {
    platform._running = false;
  }
}

class _ControlledFlutterSoundRecorderPlatform extends FlutterSoundRecorderPlatform {
  _ControlledFlutterSoundRecorderPlatform({required bool holdStop, this.stopError})
      : _stopRelease = holdStop ? Completer<void>() : null;

  final Completer<void>? _stopRelease;
  final Object? stopError;
  final Completer<void> startEntered = Completer<void>();
  final Completer<void> stopEntered = Completer<void>();
  FlutterSoundRecorderCallback? _callback;
  int startCalls = 0;
  int stopCalls = 0;

  @override
  Future<void> openRecorder(FlutterSoundRecorderCallback callback, {required dynamic logLevel}) async {
    _callback = callback;
    callback.openRecorderCompleted(RecorderState.isStopped.index, true);
  }

  @override
  Future<void> resetPlugin(FlutterSoundRecorderCallback callback) async {}

  @override
  Future<void> closeRecorder(FlutterSoundRecorderCallback callback) async {}

  @override
  Future<bool> isEncoderSupported(FlutterSoundRecorderCallback callback, {required Codec codec}) async => true;

  @override
  Future<void> startRecorder(
    FlutterSoundRecorderCallback callback, {
    Codec? codec,
    String? path,
    int sampleRate = 44100,
    int numChannels = 1,
    int bitRate = 16000,
    int bufferSize = 8192,
    Duration timeSlice = Duration.zero,
    bool enableVoiceProcessing = false,
    bool interleaved = true,
    required bool toStream,
    AudioSource? audioSource,
  }) async {
    _callback = callback;
    startCalls++;
    if (!startEntered.isCompleted) startEntered.complete();
    callback.startRecorderCompleted(RecorderState.isRecording.index, true);
  }

  @override
  Future<void> stopRecorder(FlutterSoundRecorderCallback callback) async {
    stopCalls++;
    if (stopCalls == 1) {
      callback.stopRecorderCompleted(RecorderState.isStopped.index, true, null);
      return;
    }
    if (!stopEntered.isCompleted) stopEntered.complete();
    await _stopRelease?.future;
    if (stopError != null) throw stopError!;
    callback.stopRecorderCompleted(RecorderState.isStopped.index, true, null);
  }

  void emit(Uint8List bytes) => _callback?.interleavedRecording(data: bytes);

  void releaseStop() {
    final stopRelease = _stopRelease;
    if (stopRelease != null && !stopRelease.isCompleted) stopRelease.complete();
  }

  @override
  int getSampleRate(FlutterSoundRecorderCallback callback) => 16000;

  @override
  void requestData(FlutterSoundRecorderCallback callback) {}
}

Future<void> _grantAuthority(SharedPreferencesUtil prefs, String uid) async {
  prefs.acceptAiConsent(
    receiptId: 'aicr_$uid',
    uid: uid,
    profileBindingId: 'profile-$uid',
    serverDecidedAt: '2026-08-02T00:00:00Z',
  );
  prefs.markAiConsentServerVerified(
    uid: uid,
    receiptId: 'aicr_$uid',
    policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
    processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
    profileBindingId: 'profile-$uid',
    scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
    scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
  );
}

Future<void> _quiesce() => EllaAccountIsolationService(
      stopNotificationAudio: () {},
      clearGuardianNotifications: () {},
      stopCapture: () {},
      stopV2v: () {},
      stopGuardian: () {},
      stopServices: () {},
      quarantineLegacy: () {},
    ).stopForAccountTransition();

ActiveWalAuthority _activeAuthority(String uid, bool Function() current) {
  final owner = WalOwner(
    uid: uid,
    profileBindingId: 'profile-$uid',
    bindingRevision: 3,
    consentReceiptId: 'aicr_$uid',
    authorityGenerationAtCapture: 7,
  );
  return ActiveWalAuthority(
    owner: owner,
    consent: AiConsentAuthoritySnapshot(
      generation: 7,
      uid: uid,
      verifiedPersonaId: null,
      profileBindingId: owner.profileBindingId,
      receiptId: owner.consentReceiptId,
      policyVersion: 'v8',
      processorSetHash: 'processors',
      scopeVersion: 'scope-v2',
      scopeHash: 'scope',
    ),
    currentCheck: current,
  );
}

ServerConversation _conversation(String id, String overview) =>
    ServerConversation(id: id, createdAt: DateTime.now(), structured: Structured('Title', overview));

Memory _memory(String id, String uid) => Memory(
      id: id,
      uid: uid,
      content: 'Memory $id',
      category: MemoryCategory.manual,
      createdAt: DateTime.utc(2026, 8, 2),
      updatedAt: DateTime.utc(2026, 8, 2),
      visibility: MemoryVisibility.private,
    );

Person _person(String id) => Person(
      id: id,
      name: id,
      createdAt: DateTime.utc(2026, 8, 2),
      updatedAt: DateTime.utc(2026, 8, 2),
      speechSamples: ['sample-a'],
    );

MessageFile _messageFile(String id) =>
    MessageFile('provider-$id', null, '$id.txt', 'text/plain', id, DateTime.utc(2026, 8, 2), null);

ServerMessage _voiceMessage(String id, String text, MessageSender sender) => ServerMessage(
      id,
      DateTime.utc(2026, 8, 2),
      text,
      sender,
      MessageType.text,
      null,
      false,
      const [],
      const [],
      const [],
      fromVoice: true,
    );

Stream<ServerMessageChunk> _completedVoiceStream(String text) {
  final message = _voiceMessage('reply-a', text, MessageSender.ai);
  return Stream.fromIterable([
    ServerMessageChunk(message.id, text, MessageChunkType.data),
    ServerMessageChunk(message.id, text, MessageChunkType.done, message: message),
  ]);
}

class _DelayedDiscoverer implements DeviceDiscoverer {
  _DelayedDiscoverer(this._stop);

  final Future<void> _stop;
  int stopCalls = 0;

  @override
  String get name => 'delayed';

  @override
  bool get isSupported => true;

  @override
  Future<DeviceDiscoveryResult> discover({int timeout = 5}) async => const DeviceDiscoveryResult(devices: []);

  @override
  Future<void> stop() {
    stopCalls++;
    return _stop;
  }
}

class _DelayedRecorderRunner implements IBackgroundRecorderRunner {
  final startEntered = Completer<void>();
  final releaseStart = Completer<void>();
  Function(Uint8List bytes)? _onByteReceived;
  int stopCalls = 0;

  @override
  Future<void> ensureRunning() async {}

  @override
  Future<void> startRecorder({
    required Function(Uint8List bytes) onByteReceived,
    Function()? onRecording,
    Function()? onStop,
    Function()? onInitializing,
  }) async {
    _onByteReceived = onByteReceived;
    startEntered.complete();
    await releaseStart.future;
  }

  void emit(List<int> bytes) => _onByteReceived?.call(Uint8List.fromList(bytes));

  @override
  Future<void> stopRecorder({bool quiesce = false}) async {
    stopCalls++;
  }
}

class _TimeoutRecorderRunner implements IBackgroundRecorderRunner {
  @override
  Future<void> ensureRunning() async {}

  @override
  Future<void> startRecorder({
    required Function(Uint8List bytes) onByteReceived,
    Function()? onRecording,
    Function()? onStop,
    Function()? onInitializing,
  }) async {}

  @override
  Future<void> stopRecorder({bool quiesce = false}) =>
      Future<void>.error(TimeoutException('native recorder stop not acknowledged'));
}

class _GenerationChangingAuthority implements ExactAccountAuthorityVerifier {
  _GenerationChangingAuthority({required this.prefs, required this.mutateAfterCheck})
      : _snapshot = AccountGenerationAuthority(
          preferences: prefs,
          uid: prefs.uid,
          generation: prefs.aiConsentAuthorityGeneration,
        );

  final SharedPreferencesUtil prefs;
  final int mutateAfterCheck;
  final AccountGenerationAuthority _snapshot;
  int checks = 0;

  @override
  String get uid => _snapshot.uid;

  @override
  bool isExactCurrent() {
    checks++;
    final current = _snapshot.isExactCurrent();
    if (checks == mutateAfterCheck) prefs.invalidateAccountAuthorityForTransition();
    return current;
  }
}

class _MutableExactAuthority implements ExactAccountAuthorityVerifier {
  bool current = true;
  int checks = 0;

  @override
  String get uid => 'uid-a';

  @override
  bool isExactCurrent() {
    checks++;
    return current;
  }
}

class _StreamingTestClient extends http.BaseClient {
  _StreamingTestClient(this.sendStarted);

  final Completer<void> sendStarted;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    sendStarted.complete();
    await Future<void>.delayed(Duration.zero);
    return http.StreamedResponse(const Stream<List<int>>.empty(), 200);
  }
}

extension on Memory {
  String toJsonString() => '{"id":"$id","uid":"$uid","content":"$content","category":"manual",'
      '"created_at":"${createdAt.toIso8601String()}","updated_at":"${updatedAt.toIso8601String()}",'
      '"visibility":"private"}';
}
