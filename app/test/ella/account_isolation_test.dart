import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/http_pool_manager.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/memory.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/backend/schema/person.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/ella_account_isolation_service.dart';
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

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  Env.init();
  PlatformManager.initializeForTesting();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
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
      fetchPeople: () => response.future,
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

  test('account switch during Ella chat streaming cannot mutate or persist under the next account', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantAuthority(prefs, 'uid-a');
    final controller = StreamController<ServerMessageChunk>();
    final started = Completer<void>();
    final provider = MessageProvider(
      activeAuthority: () => _activeAuthority('uid-a', () => prefs.uid == 'uid-a'),
      ellaChatStreamSender: (text) {
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

  test('account switch during voice streaming cannot mutate or persist under the next account', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantAuthority(prefs, 'uid-a');
    final controller = StreamController<ServerMessageChunk>();
    final started = Completer<void>();
    final provider = MessageProvider(
      activeAuthority: () => _activeAuthority('uid-a', () => prefs.uid == 'uid-a'),
      voiceTempFileSaver: (bytes, startTime, frameSize) async =>
          File('${Directory.systemTemp.path}/ella-voice-authority-test.bin')..writeAsBytesSync([1, 2, 3]),
      voiceChatStreamSender: (files) {
        started.complete();
        return controller.stream;
      },
    );

    final pending = provider.sendVoiceMessageStreamToServer([
      [1, 2, 3]
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
      stopCapture: () => calls.add('capture'),
      stopV2v: () => calls.add('v2v'),
      stopGuardian: () => calls.add('guardian'),
      stopServices: () => calls.add('wal-services'),
      quarantineLegacy: () => calls.add('quarantine'),
    );

    await service.stopForAccountTransition();

    expect(calls, ['capture', 'v2v', 'guardian', 'wal-services', 'quarantine']);
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
    addTearDown(() =>
        TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, null));
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
    addTearDown(() =>
        TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, null));

    final service = DesktopSystemAudioRecorderService(channel: channel);
    await expectLater(service.stopForAccountTransition(), throwsA(anything));
  });

  test('real JSON and multipart helpers close same-UID generation drift before egress', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    final originalGeneration = prefs.aiConsentAuthorityGeneration;
    var requests = 0;
    HttpPoolManager.instance.replaceClientForTesting(MockClient((request) async {
      requests++;
      return http.Response('{}', 200);
    }));
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

    final status = EllaWorkspaceStatus.current(
      preferences: prefs,
      uid: 'uid-private-value',
      email: 'new@example.test',
    );

    expect(status.workspaceVerified, isTrue);
    expect(status.workspaceFingerprint, matches(RegExp(r'^[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}$')));
    expect(status.workspaceFingerprint, isNot(contains('uid-private-value')));
    expect(status.chat, EllaRouteVerification.notVerified);
    expect(status.voice, EllaRouteVerification.notVerified);
    expect(status.whispers, EllaRouteVerification.notVerified);
    expect(status.quarantinedAudioCount, 0);
  });
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

ServerConversation _conversation(String id, String overview) => ServerConversation(
      id: id,
      createdAt: DateTime.now(),
      structured: Structured('Title', overview),
    );

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

MessageFile _messageFile(String id) => MessageFile(
      'provider-$id',
      null,
      '$id.txt',
      'text/plain',
      id,
      DateTime.utc(2026, 8, 2),
      null,
    );

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
  Future<void> stopRecorder() async {
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
  Future<void> stopRecorder() => Future<void>.error(TimeoutException('native recorder stop not acknowledged'));
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
