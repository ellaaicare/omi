import 'package:omi/ella/upstream_capture/source_revision_contract.dart';
import 'dart:async';
import 'dart:io';
import 'dart:convert';
import 'package:http/http.dart' as http;
import 'package:omi/upstream_capture/services/wals/local_wal_sync.dart';
import 'package:omi/upstream_capture/services/wals/sync_upload_gate.dart';
import 'package:omi/upstream_capture/services/wals/sync_rate_limiter.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/whisper_delivery_client.dart';
import 'package:omi/ella/upstream_capture/ella_capture_protocol_socket.dart';
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/backend/schema/conversation.dart';
import 'package:omi/upstream_capture/backend/schema/message_event.dart';
import 'package:omi/upstream_capture/services/wals/wal.dart';
import 'package:omi/upstream_capture/backend/http/api/conversations.dart'
    show buildSyncCaptureManifestClaims, requireRecoveryCaptureManifest, uploadLocalFilesV2;
import 'package:omi/upstream_capture/services/wals/wal_interfaces.dart';
import 'package:omi/upstream_capture/services/wals/recording_transfer_coordinator.dart';
import 'package:omi/upstream_capture/services/capture/scenarios/native_event_vector.dart';
import '../services/whisper_delivery_client_test.dart' as delivery;
import 'support/ella_upstream_capture_harness.dart';

void main() {
  late Directory dir;
  late EllaUpstreamCaptureHarness h;
  late delivery.Harness d;
  late ValueNotifier<int> changes;
  var fetch = const SyncJobFetch(SyncJobFetchOutcome.transient);
  setUp(() async {
    fetch = const SyncJobFetch(SyncJobFetchOutcome.transient);
    dir = await Directory.systemTemp.createTemp('ella_gap_diagnostic_');
    changes = ValueNotifier(0);
    d = delivery.Harness();
    d.authority = delivery.Authority(accountA);
    final original = d.client;
    d.client = WhisperDeliveryClient(
        transport: original.transport,
        media: d.media,
        read: original.read,
        write: original.write,
        authorityProvider: () => d.authority,
        newId: d.id,
        lifetimeIdentity: (_) => d.lifetime,
        onChanged: () => changes.value++);
    h = await EllaUpstreamCaptureHarness.boot(
        tempDir: dir,
        protocolV2: true,
        deliveryChanges: changes,
        deliveryScope: () => d.client.scope,
        jobFetcher: (_) async => fetch,
        memoryLoader: (_, {expectedAuthenticatedUid, exactAuthority}) async => null);
    expect(await h.bind(), isTrue);
  });
  tearDown(() async {
    await h.dispose();
    changes.dispose();
    if (dir.existsSync()) dir.deleteSync(recursive: true);
  });
  Future<int> storedFrames() async {
    var count = 0;
    for (final w in await h.wal.syncs.phone.getAllWals()) {
      final path = await Wal.getFilePath(w.filePath);
      if (path == null || !File(path).existsSync()) continue;
      final bytes = File(path).readAsBytesSync();
      var offset = 0;
      while (offset + 4 <= bytes.length) {
        final len = ByteData.sublistView(bytes, offset, offset + 4).getUint32(0, Endian.little);
        offset += 4 + len;
        count++;
      }
    }
    return count;
  }

  ServerConversation memory(String id) => ServerConversation.fromJson({
        'id': id,
        'created_at': '2026-03-01T12:00:00Z',
        'started_at': '2026-03-01T12:00:00Z',
        'finished_at': '2026-03-01T12:00:03Z',
        'status': 'completed',
        'structured': {'title': 'Synthetic', 'overview': '', 'emoji': '', 'category': 'other'},
        'transcript_segments': [
          {'text': 'synthetic partial after gap', 'speaker': 'SPEAKER_00', 'start': 2, 'end': 3, 'is_user': false}
        ]
      });
  Future<List<List<int>>> savedPayloads() async {
    final result = <List<int>>[];
    for (final wal in await h.wal.syncs.phone.getAllWals()) {
      final file = File((await Wal.getFilePath(wal.filePath))!);
      if (!file.existsSync()) continue;
      final bytes = file.readAsBytesSync();
      var off = 0;
      while (off + 4 <= bytes.length) {
        final length = ByteData.sublistView(bytes, off, off + 4).getUint32(0, Endian.little);
        off += 4;
        result.add(bytes.sublist(off, off + length));
        off += length;
      }
    }
    return result;
  }

  Future<void> gap() async {
    final hold = Completer<void>(), entered = Completer<void>();
    h.holdNextDrain = hold;
    h.nextDrainEntered = entered;
    h.uploads.failAll = true;
    final native = await h.startPhone();
    h.injectPhoneFrames(100, sessionId: native, firstFrameIndex: 0);
    h.clock.advanceTo(h.clock.now().add(const Duration(seconds: 1)));
    await h.settle();
    await d.start();
    await entered.future;
    expect(h.provider.captureWindowTransportInterrupted, isTrue, reason: 'marked before retirement/drain wait');
    h.injectPhoneFrames(100, sessionId: native, firstFrameIndex: 100);
    h.clock.advanceTo(h.clock.now().add(const Duration(seconds: 1)));
    await pumpEventQueue();
    hold.complete();
    await h.runtime.pendingDeliveryRefresh;
    await h.settle();
    h.injectPhoneFrames(100, sessionId: native, firstFrameIndex: 200);
    h.clock.advanceTo(h.clock.now().add(const Duration(seconds: 1)));
    await h.settle();
    expect(h.audioFramesSent, 200);
  }

  Future<void> processing([String id = 'matching']) async {
    h.provider.onMessageEventReceived(ConversationProcessingStartedEvent(memory: memory(id)));
    await h.provider.pendingCaptureFinalization;
    await h.settle();
  }

  Future<void> progress([String id = 'matching']) async {
    h.provider.onMessageEventReceived(ConversationEvent(memory: memory(id), messages: []));
    await h.settle();
  }

  UploadFilesResult covered() => UploadFilesResult.done(
      SyncLocalFilesResponse(newConversationIds: [], updatedConversationIds: ['matching'], totalSegments: 1));
  Future<void> retry() async {
    h.clock.advanceTo(h.clock.now().add(const Duration(minutes: 2)));
    await h.coordinator.wake(WakeTrigger.userRetry);
    await h.settle();
  }

  Future<void> exact300() async {
    final payloads = await savedPayloads();
    expect(payloads, hasLength(300));
    for (var i = 0; i < 300; i++)
      expect(payloads[i], NativeEventVector.synthesizePcmFrame(i), reason: 'frame $i remains byte exact');
  }

  test('300 frames with 100 unsent survive partial transcript and failed recovery', () async {
    await gap();
    await processing();
    await exact300();
    await progress();
    await exact300();
    expect(h.uploads.attempts, hasLength(1));
    expect(h.uploads.attempts.single.claimLiveCapture, isFalse, reason: 'v2 never claims live transcript replacement');
    expect(h.uploads.attempts.single.conversationId, 'matching');
    expect(h.uploads.attempts.single.totalBytes, 300 * 324);
    final wal = (await h.wal.syncs.phone.getAllWals()).first;
    expect(wal.status, WalStatus.miss);
    expect(wal.syncedFrameOffset, 0);
  });
  test('unrelated and repeated late results cannot consume pending attribution', () async {
    await gap();
    await processing();
    await progress('unrelated');
    expect(h.uploads.attempts, isEmpty);
    await exact300();
    await progress();
    expect(h.uploads.attempts, hasLength(1));
    await progress();
    await progress('unrelated');
    expect(h.uploads.attempts, hasLength(1));
    await exact300();
  });
  Map<String, dynamic> revisionReceipt(Map<String, dynamic> request) => {
        ...jsonDecode(jsonEncode(request)) as Map<String, dynamic>,
        'status': 'revision_committed',
        'preservation': 'live_transcript_unchanged',
        'revision_id': expectedSourceRevisionId(request),
        'files': [
          for (final file in request['files'])
            {...file, 'frame_start': 0, 'frame_end': file['frame_count'], 'status': 'completed'}
        ],
      };

  for (final withReceipt in [true, false]) {
    test('production HTTP decoder to WAL acknowledgement: exact fixture receipt=$withReceipt', () async {
      const fixtureRoot = 'test/ella/upstream_capture/fixtures/source_revision_v2';
      final request = jsonDecode(File('$fixtureRoot/client-request.json').readAsStringSync()) as Map<String, dynamic>;
      final rawResponse = File('$fixtureRoot/backend-response.json').readAsStringSync();
      final claim = request['files'].single;
      final bytes = <int>[];
      for (var index = 0; index < 4; index++) {
        bytes.addAll((ByteData(4)..setUint32(0, 320, Endian.little)).buffer.asUint8List());
        bytes.addAll(index < 3
            ? List<int>.generate(320, (i) => (index * 320 + i) % 255)
            : [1, 0, 2, 0, ...List<int>.filled(316, 0)]);
      }
      final file = File('${dir.path}/${claim['name']}');
      await file.writeAsBytes(bytes, flush: true);
      final wal = Wal(
          timerStart: 1,
          codec: BleAudioCodec.pcm16,
          seconds: 0,
          totalFrames: 4,
          ownerUid: request['account_id'],
          conversationId: request['conversation_id'],
          captureWindowId: request['capture_window_id'],
          captureChunkSequence: 0,
          filePath: claim['name'],
          storage: WalStorage.disk,
          status: WalStatus.miss,
          sourceRevisionRequest: request,
          sourceProvenance: {
            'version': 2,
            'source_session_id': request['source_session_id'],
            'sync_keys': claim['sync_keys'],
            'runs': claim['runs'],
            'origin_runs': [
              {
                'frame_start': 0,
                'frame_end': 4,
                'origin': {'account_id': request['account_id']}
              }
            ]
          });
      expect(await buildSourceRevisionRequest([wal], [file], request['account_id']), request,
          reason: 'actual bytes must match the independently executed backend fixture');
      var sends = 0;
      final gate = SyncUploadGate(
          limiter: SyncRateLimiter.instance,
          fairUseStatusLoader: () async => null,
          uploader: (files, {onUploadProgress, conversationId, claimLiveCapture = false, geolocation}) =>
              uploadLocalFilesV2(files, conversationId: conversationId, claimLiveCapture: claimLiveCapture,
                  multipartCall: (
                      {required url,
                      required files,
                      headers = const {},
                      fields = const {},
                      onUploadProgress,
                      exactAuthority,
                      onHttpStatus,
                      onSendAttempt}) async {
                sends++;
                expect(url, contains('v2/sync-local-files?conversation_id=target'));
                expect(files.single.readAsBytesSync(), bytes);
                final body =
                    withReceipt ? rawResponse : jsonEncode(jsonDecode(rawResponse)..remove('coverage_receipt'));
                return http.Response(body, 200, headers: {'content-type': 'application/json'});
              }));
      final listener = _WireReceiptListener();
      final sync = LocalWalSyncImpl(listener,
          uploadGate: gate,
          now: h.clock.now,
          persistWals: (wals) async => File('${dir.path}/wire-receipt-wals.json')
              .writeAsString(jsonEncode(wals.map((w) => w.toJson()).toList()), flush: true));
      sync.testWals = [wal];
      sync.recoveryAuthorityProvider = () => _WireReceiptAuthority(request['account_id']);
      await sync.syncWal(wal: wal);
      expect(sends, 1);
      expect(wal.status, withReceipt ? WalStatus.synced : WalStatus.uploaded);
      expect(listener.synced, withReceipt ? 1 : 0);
      expect(await file.readAsBytes(), bytes, reason: 'decoder test does not delete source');
      await sync.syncAll();
      expect(sends, 1, reason: 'acknowledged or unproven work is not resent');
    });
  }

  test('new capture exact committed revision acknowledges full retained source once', () async {
    await gap();
    await processing();
    await progress();
    final members = await h.wal.syncs.phone.getAllWals();
    final request = members.first.sourceRevisionRequest!;
    expect(request['version'], 2);
    expect(request['mode'], 'whole_source_revision');
    expect(request['files'].fold<int>(0, (int n, dynamic f) => n + (f['frame_count'] as int)), 300);
    expect(members.every((w) => w.sourceProvenance != null), isTrue);
    final keys = <dynamic>[for (final f in request['files']) ...f['sync_keys']];
    expect(keys, [
      for (var i = 0; i < 300; i++) [i & 255]
    ]);
    final runs = <dynamic>[for (final f in request['files']) ...f['runs']];
    expect(runs.any((r) => r['gap_before'] == 'transport_rebind'), isTrue);
    h.uploads.failAll = false;
    h.uploads.enqueueOutcome(() => UploadFilesResult.done(SyncLocalFilesResponse(
        newConversationIds: [], updatedConversationIds: [], coverageReceipt: revisionReceipt(request))));
    await retry();
    expect(h.uploads.attempts, hasLength(2));
    expect((await h.wal.syncs.phone.getAllWals()).every((w) => w.status == WalStatus.synced), isTrue);
    await retry();
    expect(h.uploads.attempts, hasLength(2));
  });

  test('lost response and restart retain exact operation and source provenance', () async {
    await gap();
    await processing();
    await progress();
    final before = jsonDecode(jsonEncode((await h.wal.syncs.phone.getAllWals()).first.sourceRevisionRequest));
    final now = h.clock.now();
    await h.dispose();
    h = await EllaUpstreamCaptureHarness.boot(
        tempDir: dir, protocolV2: true, memoryLoader: (_, {expectedAuthenticatedUid, exactAuthority}) async => null);
    expect(await h.bind(), isTrue);
    h.clock.advanceTo(now);
    final restored = (await h.wal.syncs.phone.getAllWals()).first;
    expect(restored.sourceRevisionRequest, before);
    await exact300();
    h.uploads.enqueueOutcome(() => UploadFilesResult.done(SyncLocalFilesResponse(
        newConversationIds: [], updatedConversationIds: [], coverageReceipt: revisionReceipt(before))));
    await retry();
    expect(h.uploads.attempts, hasLength(1));
    await retry();
    expect(h.uploads.attempts, hasLength(1));
  });

  test('request persisted before network; corrupted retained bytes cannot reuse positive proof', () async {
    await gap();
    await processing();
    await progress();
    final members = await h.wal.syncs.phone.getAllWals();
    final index = jsonDecode(await File('${dir.path}/wals.json').readAsString());
    expect((index['wals'] as List).every((w) => w['source_revision_request'] != null), isTrue);
    final file = File((await Wal.getFilePath(members.first.filePath))!);
    final bytes = await file.readAsBytes();
    bytes[10] ^= 1;
    await file.writeAsBytes(bytes, flush: true);
    h.uploads.failAll = false;
    h.uploads.enqueueOutcome(() => UploadFilesResult.done(SyncLocalFilesResponse(
        newConversationIds: [],
        updatedConversationIds: [],
        coverageReceipt: revisionReceipt(members.first.sourceRevisionRequest!))));
    await retry();
    expect(h.uploads.attempts, hasLength(1));
    expect(members.every((w) => w.status != WalStatus.synced), isTrue);
    expect(await storedFrames(), 300);
  });

  test('positive receipt cannot acknowledge bytes changed during request', () async {
    await gap();
    await processing();
    await progress();
    final members = await h.wal.syncs.phone.getAllWals();
    final request = members.first.sourceRevisionRequest!;
    final file = File((await Wal.getFilePath(members.first.filePath))!);
    h.uploads.failAll = false;
    h.uploads.enqueueOutcome(() {
      final bytes = file.readAsBytesSync();
      bytes[10] ^= 1;
      file.writeAsBytesSync(bytes, flush: true);
      return UploadFilesResult.done(SyncLocalFilesResponse(
          newConversationIds: [], updatedConversationIds: [], coverageReceipt: revisionReceipt(request)));
    });
    await retry();
    expect(h.uploads.attempts, hasLength(2));
    expect(members.every((w) => w.status != WalStatus.synced), isTrue);
    expect(await storedFrames(), 300);
  });

  test('manual one-file new source upload never claims live transcript replacement', () async {
    final native = await h.startPhone();
    h.injectPhoneFrames(100, sessionId: native);
    h.clock.advanceTo(h.clock.now().add(const Duration(seconds: 1)));
    await processing();
    await processing();
    final wal = (await h.wal.syncs.phone.getAllWals()).single;
    h.uploads.failAll = false;
    h.uploads.enqueueOutcome(covered);
    await h.wal.syncs.phone.syncWal(wal: wal);
    expect(h.uploads.attempts.single.claimLiveCapture, isFalse);
    expect(wal.sourceRevisionRequest, isNotNull);
    expect(await storedFrames(), 100);
  });

  test('ordinary sent phone audio stays durable without a frame coverage receipt', () async {
    h.uploads.failAll = true;
    final native = await h.startPhone();
    h.injectPhoneFrames(100, sessionId: native);
    h.clock.advanceTo(h.clock.now().add(const Duration(seconds: 1)));
    await processing();
    expect(await storedFrames(), 100);
    await progress();
    expect(await storedFrames(), 100);
    expect(h.uploads.attempts, hasLength(1));
  });
  test('legacy claimed success retains identical bytes and holds further retry', () async {
    await gap();
    await processing();
    await progress();
    final original = await savedPayloads();
    final first = h.uploads.attempts.single;
    h.uploads.failAll = false;
    h.uploads.enqueueOutcome(covered);
    await retry();
    expect(h.uploads.attempts, hasLength(2));
    expect(h.uploads.attempts.last.fileNames, first.fileNames);
    expect(h.uploads.attempts.last.totalBytes, first.totalBytes);
    expect(h.uploads.attempts.last.claimLiveCapture, isFalse);
    final wals = await h.wal.syncs.phone.getAllWals();
    if (wals.isNotEmpty) expect(wals.every((wal) => wal.status == WalStatus.uploaded), isTrue);
    await retry();
    expect(h.uploads.attempts, hasLength(2));
    expect(original, hasLength(300));
    await exact300();
    for (final wal in await h.wal.syncs.phone.getAllWals()) {
      expect(wal.syncedFrameOffset, 0);
      await h.wal.syncs.phone.syncWal(wal: wal);
    }
    expect(h.uploads.attempts, hasLength(2));
  });
  test('async legacy completion retains original without complete-source proof', () async {
    await gap();
    await processing();
    h.uploads.failAll = false;
    h.uploads.enqueueOutcome(() => UploadFilesResult.queued('owned-job'));
    fetch = const SyncJobFetch(SyncJobFetchOutcome.transient);
    await progress();
    await exact300();
    var wal = (await h.wal.syncs.phone.getAllWals()).first;
    expect(wal.status, WalStatus.uploaded);
    expect(wal.jobId, 'owned-job');
    await retry();
    await exact300();
    expect(h.uploads.attempts, hasLength(1));
    fetch = SyncJobFetch(
        SyncJobFetchOutcome.ok,
        SyncJobStatusResponse(
            jobId: 'owned-job',
            status: 'completed',
            result: SyncLocalFilesResponse(
                newConversationIds: [], updatedConversationIds: ['matching'], totalSegments: 1)));
    await retry();
    await retry();
    expect(h.uploads.attempts, hasLength(1));
    final remaining = await h.wal.syncs.phone.getAllWals();
    expect(remaining, isNotEmpty);
    expect(remaining.every((wal) => wal.status == WalStatus.uploaded && wal.jobId == 'owned-job'), isTrue);
    await exact300();
  });
  test('concurrent supported recovery wakes do not duplicate a successful upload', () async {
    await gap();
    await processing();
    h.uploads.failAll = false;
    h.uploads.enqueueOutcome(covered);
    await Future.wait([
      h.coordinator.wake(WakeTrigger.userRetry),
      h.coordinator.wake(WakeTrigger.dataStalled),
      h.coordinator.wake(WakeTrigger.socketReconnected)
    ]);
    await h.settle();
    expect(h.uploads.attempts, hasLength(1));
  });

  test('concurrent direct drains hold source after unproven completion', () async {
    await gap();
    await processing();
    h.uploads.failAll = false;
    h.uploads.enqueueOutcome(covered);
    await Future.wait([h.wal.syncs.phone.syncLiveCaptureOnly(), h.wal.syncs.phone.syncLiveCaptureOnly()]);
    await h.settle();
    expect(h.uploads.attempts, hasLength(1));
    expect((await h.wal.syncs.phone.getAllWals()).every((wal) => wal.status == WalStatus.uploaded), isTrue);
  });
  test('new offline PCM without exact target remains retained across restart and manual retry', () async {
    final native = await h.startPhone();
    h.setConnected(false);
    h.socket!.emitClose();
    await h.settle();
    h.injectPhoneFrames(600, sessionId: native);
    h.clock.advanceTo(h.clock.now().add(const Duration(seconds: 6)));
    await h.provider.stopStreamRecording();
    await h.settle();
    h.setConnected(true);
    await h.settle();
    await h.coordinator.wake(WakeTrigger.userRetry);
    await h.settle();
    expect(h.uploads.attempts, isEmpty);
    var wal = (await h.wal.syncs.phone.getAllWals()).single;
    expect(wal.conversationId, isNull);
    expect(wal.status, WalStatus.miss);
    expect(wal.jobId, isNull);
    expect(wal.syncedFrameOffset, 0);
    final before = await savedPayloads();
    expect(before, hasLength(600));
    await h.dispose();
    h = await EllaUpstreamCaptureHarness.boot(
        tempDir: dir, protocolV2: true, memoryLoader: (_, {expectedAuthenticatedUid, exactAuthority}) async => null);
    expect(await h.bind(), isTrue);
    wal = (await h.wal.syncs.phone.getAllWals()).single;
    expect(wal.status, WalStatus.miss);
    await retry();
    await h.wal.syncs.phone.syncWal(wal: wal);
    expect(h.uploads.attempts, isEmpty);
    expect(await savedPayloads(), before);
  });

  test('direct live progress acknowledgment never prunes a miss or uploaded WAL', () async {
    await gap();
    await processing();
    final wal = (await h.wal.syncs.phone.getAllWals()).first;
    expect(await h.wal.syncs.phone.confirmSessionTranscription(wal.timerStart, 'matching'), 0);
    await exact300();
    wal.status = WalStatus.uploaded;
    wal.jobId = 'owned-job';
    expect(await h.wal.syncs.phone.confirmSessionTranscription(wal.timerStart, 'matching'), 0);
    await exact300();
  });
  test('source windows in the same second get distinct files and exact stamps', () async {
    h.uploads.failAll = true;
    final native = await h.startPhone();
    h.injectPhoneFrames(10, sessionId: native);
    final firstWindow = h.wal.syncs.phone.captureWindowId;
    await h.wal.syncs.phone.finalizeCurrentSession();
    h.injectPhoneFrames(10, sessionId: native, firstFrameIndex: 10);
    final secondWindow = h.wal.syncs.phone.captureWindowId;
    await h.wal.syncs.phone.finalizeCurrentSession();
    final wals = await h.wal.syncs.phone.getAllWals();
    expect(wals, hasLength(2));
    expect(wals.map((w) => w.filePath).toSet(), hasLength(2));
    expect(wals.map((w) => w.id).toSet(), hasLength(2));
    await h.wal.syncs.phone.stampConversationId(0, 'second-only', captureWindowId: secondWindow);
    expect(wals.firstWhere((w) => w.captureWindowId == firstWindow).conversationId, isNull);
    expect(wals.firstWhere((w) => w.captureWindowId == secondWindow).conversationId, 'second-only');
    expect(await storedFrames(), 20);
  });
  test('account change rejects late completion and retains original files', () async {
    await gap();
    await processing();
    final wals = await h.wal.syncs.phone.getAllWals();
    final paths = await Future.wait(wals.map((w) => Wal.getFilePath(w.filePath)));
    h.switchAccount(accountB);
    await h.bind(accountB);
    await progress();
    expect(h.uploads.attempts, isEmpty);
    for (final path in paths) expect(File(path!).existsSync(), isTrue);
  });
  test('dispose rejects late recovery while preserving finalized recording', () async {
    await gap();
    await processing();
    h.runtime.dispose();
    await progress();
    await exact300();
    expect(h.uploads.attempts, isEmpty);
  });

  test('wire progress requires matching socket generation owner and conversation', () async {
    h.uploads.failAll = true;
    final native = await h.startPhone();
    h.injectPhoneFrames(100, sessionId: native);
    h.clock.advanceTo(h.clock.now().add(const Duration(seconds: 1)));
    await h.settle();
    final socket = h.sockets.last.service as EllaCaptureProtocolSocket;
    final binding = socket.captureAuthority!;
    final link = h.sockets.last.transport;
    final event = {
      'type': 'memory_processing_started',
      'memory': memory(binding.conversationId).toJson(),
      'protocol_version': binding.protocolVersion,
      'conversation_id': binding.conversationId,
      'generation': binding.generation,
      'owner_token': binding.ownerToken
    };
    for (final field in ['generation', 'owner_token', 'conversation_id']) {
      link.emitServerMessage(jsonEncode({...event, field: 'stale'}));
      await h.settle();
      expect(h.provider.activeCaptureSessionId, isNotNull);
      expect(h.uploads.attempts, isEmpty);
    }
    link.emitServerMessage(jsonEncode({...event, 'memory': memory('unrelated').toJson()}));
    await h.settle();
    expect(h.provider.activeCaptureSessionId, isNotNull);
    link.emitServerMessage(jsonEncode(event));
    await h.provider.pendingCaptureFinalization;
    await h.settle();
    expect(await storedFrames(), 100);
    final wal = (await h.wal.syncs.phone.getAllWals()).first;
    expect(wal.conversationId, binding.conversationId);
    link.emitServerMessage(jsonEncode({...event, 'type': 'memory_created', 'messages': []}));
    await h.settle();
    expect(h.uploads.attempts, hasLength(1));
    expect(await storedFrames(), 100);
  });

  test('unrelated async job completion cannot acknowledge retained window', () async {
    await gap();
    await processing();
    h.uploads.failAll = false;
    h.uploads.enqueueOutcome(() => UploadFilesResult.queued('owned-job'));
    await progress();
    for (final wrong in ['job', 'conversation', 'missing']) {
      fetch = SyncJobFetch(
          SyncJobFetchOutcome.ok,
          SyncJobStatusResponse(
              jobId: wrong == 'job' ? 'other-job' : 'owned-job',
              status: 'completed',
              result: wrong == 'missing'
                  ? null
                  : SyncLocalFilesResponse(
                      newConversationIds: [],
                      updatedConversationIds: [wrong == 'conversation' ? 'other-conversation' : 'matching'],
                      totalSegments: 1)));
      await retry();
      await exact300();
      expect((await h.wal.syncs.phone.getAllWals()).first.status, WalStatus.uploaded);
    }
    expect(h.uploads.attempts, hasLength(1));
  });
  test('unattributed window is retained rather than replayed unclaimed', () async {
    h.uploads.failAll = false;
    final native = await h.startPhone();
    h.injectPhoneFrames(100, sessionId: native);
    h.clock.advanceTo(h.clock.now().add(const Duration(seconds: 1)));
    await h.wal.syncs.phone.finalizeCurrentSession();
    await retry();
    expect(h.uploads.attempts, isEmpty);
    expect(await storedFrames(), 100);
    final wal = (await h.wal.syncs.phone.getAllWals()).first;
    await h.wal.syncs.phone.syncWal(wal: wal);
    expect(h.uploads.attempts, isEmpty);
    expect(await storedFrames(), 100);
  });

  for (final cutover in ['dispose', 'account']) {
    test('pending file finalization $cutover retains all admitted original packets', () async {
      await gap();
      final held = Completer<void>(), entered = Completer<void>();
      var block = true;
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(const MethodChannel('plugins.flutter.io/path_provider'), (call) async {
        if (call.method == 'getApplicationDocumentsDirectory') {
          if (block) {
            block = false;
            entered.complete();
            await held.future;
          }
          return dir.path;
        }
        return null;
      });
      h.provider.onMessageEventReceived(ConversationProcessingStartedEvent(memory: memory('matching')));
      final finalizing = h.provider.pendingCaptureFinalization;
      await entered.future;
      Future<bool>? rebind;
      if (cutover == 'dispose')
        h.runtime.dispose();
      else {
        h.switchAccount(accountB);
        rebind = h.bind(accountB);
      }
      held.complete();
      await finalizing;
      await rebind;
      await h.settle();
      var count = 0;
      for (final file in dir.listSync(recursive: true).whereType<File>().where((f) => f.path.endsWith('.bin'))) {
        final bytes = file.readAsBytesSync();
        var off = 0;
        while (off + 4 <= bytes.length) {
          final length = ByteData.sublistView(bytes, off, off + 4).getUint32(0, Endian.little);
          off += 4 + length;
          count++;
        }
      }
      expect(count, 300, reason: 'cutover cannot erase the already admitted prefix, gap or tail');
      expect(h.uploads.attempts, isEmpty);
    });
  }
  test('restart reloads byte-exact retained window and recovers using its original claim', () async {
    await gap();
    await processing();
    await progress();
    final files = (await h.wal.syncs.phone.getAllWals()).map((w) => w.filePath).toList();
    final now = h.clock.now();
    await h.dispose();
    h = await EllaUpstreamCaptureHarness.boot(
        tempDir: dir, protocolV2: true, memoryLoader: (_, {expectedAuthenticatedUid, exactAuthority}) async => null);
    expect(await h.bind(), isTrue);
    h.clock.advanceTo(now);
    await exact300();
    expect((await h.wal.syncs.phone.getAllWals()).map((w) => w.filePath).toList(), files);
    h.uploads.enqueueOutcome(covered);
    await retry();
    expect(h.uploads.attempts, hasLength(1));
    expect(h.uploads.attempts.single.conversationId, 'matching');
    expect(h.uploads.attempts.single.claimLiveCapture, isFalse, reason: 'v2 never claims live transcript replacement');
    await retry();
    expect(h.uploads.attempts, hasLength(1));
  });
  for (final cutover in ['dispose', 'account']) {
    test('in-flight upload $cutover cannot acknowledge saved audio', () async {
      await gap();
      await processing();
      h.uploads.failAll = false;
      h.uploads.enqueueOutcome(() {
        if (cutover == 'dispose')
          h.runtime.dispose();
        else {
          h.switchAccount(accountB);
          h.authority.release();
        }
        return UploadFilesResult.done(
            SyncLocalFilesResponse(newConversationIds: [], updatedConversationIds: ['matching']));
      });
      await progress();
      await exact300();
      expect(h.uploads.attempts, hasLength(1));
      expect((await h.wal.syncs.phone.getAllWals()).first.status, WalStatus.miss);
    });
  }
  test('lost response retry retains identical immutable file claims', () async {
    await gap();
    await processing();
    final files = <File>[];
    for (final wal in await h.wal.syncs.phone.getAllWals()) files.add(File((await Wal.getFilePath(wal.filePath))!));
    final first = await buildSyncCaptureManifestClaims(files);
    await progress();
    await exact300();
    await retry();
    expect(await buildSyncCaptureManifestClaims(files), first);
    expect(h.uploads.attempts, hasLength(2));
    expect(h.uploads.attempts.every((a) => !a.claimLiveCapture && a.conversationId == 'matching'), isTrue);
    expect(() => requireRecoveryCaptureManifest(null), throwsStateError);
    expect(() => requireRecoveryCaptureManifest(''), throwsStateError);
    expect(requireRecoveryCaptureManifest('matching-opaque-manifest'), 'matching-opaque-manifest');
  });
  test('rapid scope rotations preserve interruption and retained frame bytes', () async {
    await gap();
    await d.client.stop();
    await h.runtime.pendingDeliveryRefresh;
    await d.start();
    await h.runtime.pendingDeliveryRefresh;
    await h.settle();
    expect(h.provider.captureWindowTransportInterrupted, isTrue);
    await processing();
    await progress();
    await exact300();
  });
  for (final codec in [
    BleAudioCodec.opus,
    BleAudioCodec.opusFS320,
    BleAudioCodec.pcm16,
    BleAudioCodec.pcm8,
    BleAudioCodec.aac,
    BleAudioCodec.lc3FS1030
  ]) {
    test('pendant ${codec.name} original packets retained across rebind; recovery decoder unqualified', () async {
      h.diagnosticPendantCodec = codec;
      h.uploads.failAll = true;
      final hold = Completer<void>(), entered = Completer<void>();
      h.holdNextDrain = hold;
      h.nextDrainEntered = entered;
      final link = await h.connectPendant();
      for (var i = 0; i < 10; i++) link.emitAudio(value: 1, payloadLength: codec.getFramesLengthInBytes());
      await h.settle();
      await d.start();
      await entered.future;
      for (var i = 0; i < 50; i++) link.emitAudio(value: 2, payloadLength: codec.getFramesLengthInBytes());
      await pumpEventQueue();
      h.clock.advanceTo(h.clock.now().add(const Duration(seconds: 1)));
      hold.complete();
      await h.runtime.pendingDeliveryRefresh;
      await h.settle();
      for (var i = 0; i < 10; i++) link.emitAudio(value: 3, payloadLength: codec.getFramesLengthInBytes());
      await h.settle();
      await processing();
      await progress();
      expect(await storedFrames(), 70);
      final supported = [BleAudioCodec.pcm16, BleAudioCodec.opus, BleAudioCodec.opusFS320].contains(codec);
      expect(h.uploads.attempts, hasLength(supported ? 1 : 0));
      if (supported)
        expect(h.uploads.attempts.single.claimLiveCapture, isFalse,
            reason: 'v2 never claims live transcript replacement');
      expect(link.audioSubscriptionsOpened, 1);
      expect(h.audioFramesSent, 20);
    });
  }
}

class _WireReceiptAuthority implements ExactAccountAuthorityVerifier {
  _WireReceiptAuthority(this.uid);
  @override
  final String uid;
  @override
  bool isExactCurrent() => true;
}

class _WireReceiptListener implements IWalSyncListener {
  int synced = 0;
  @override
  void onWalUpdated() {}
  @override
  void onWalSynced(Wal wal, {ServerConversation? conversation}) => synced++;
}
