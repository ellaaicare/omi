import 'dart:async';
import 'dart:convert';

import 'package:connectivity_plus_platform_interface/connectivity_plus_platform_interface.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/message_event.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/backend/schema/transcript_segment.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/people_provider.dart';
import 'package:omi/services/services.dart';
import 'package:omi/services/sockets/pure_socket.dart';
import 'package:omi/services/sockets/transcription_service.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/services/wals/wal.dart';
import 'package:omi/utils/enums.dart';

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

class _FakeMicRecorder implements IMicRecorderService {
  _FakeMicRecorder({this.failuresBeforeStart = 0, this.stopEntered, this.stopGate});

  final int failuresBeforeStart;
  final Completer<void>? stopEntered;
  final Completer<void>? stopGate;
  Function(Uint8List bytes)? _onByteReceived;
  Function()? _onRecording;
  Function()? _onStop;
  Function()? _onInitializing;
  int starts = 0;
  int stops = 0;

  @override
  Future<void> start({
    required Function(Uint8List bytes) onByteReceived,
    Function()? onRecording,
    Function()? onStop,
    Function()? onInitializing,
  }) async {
    starts++;
    if (starts <= failuresBeforeStart) {
      throw StateError('microphone is still owned by a prior session');
    }
    _onByteReceived = onByteReceived;
    _onRecording = onRecording;
    _onStop = onStop;
    _onInitializing = onInitializing;
    _onInitializing?.call();
  }

  void confirmRecording() => _onRecording?.call();

  void emit(List<int> bytes) => _onByteReceived?.call(Uint8List.fromList(bytes));

  @override
  Future<void> stop() async {
    stops++;
    if (stopEntered?.isCompleted == false) stopEntered?.complete();
    await stopGate?.future;
    _onStop?.call();
  }

  @override
  Future<void> stopForAccountTransition() => stop();

  @override
  void resumeAfterAccountTransition() {}
}

class _FakePureSocket implements IPureSocket {
  _FakePureSocket({PureSocketStatus status = PureSocketStatus.connected, this.sendError, this.onStop})
      : _status = status;

  PureSocketStatus _status;
  final Object? sendError;
  final FutureOr<void> Function()? onStop;
  IPureSocketListener? listener;
  final List<dynamic> sent = [];
  int stops = 0;

  @override
  PureSocketStatus get status => _status;

  @override
  Future<bool> connect() async {
    _status = PureSocketStatus.connected;
    listener?.onConnected();
    return true;
  }

  @override
  Future<void> disconnect() async {
    _status = PureSocketStatus.disconnected;
    listener?.onClosed();
  }

  @override
  Future<void> stop() async {
    stops++;
    await onStop?.call();
    await disconnect();
  }

  @override
  void send(dynamic message) {
    if (sendError case final error?) throw error;
    sent.add(message);
  }

  @override
  void setListener(IPureSocketListener listener) => this.listener = listener;

  @override
  void onClosed() => listener?.onClosed();

  @override
  void onConnected() => listener?.onConnected();

  @override
  void onError(Object err, StackTrace trace) => listener?.onError(err, trace);

  @override
  void onMessage(dynamic message) => listener?.onMessage(message);
}

class _FakeTranscriptSocket {
  _FakeTranscriptSocket({PureSocketStatus status = PureSocketStatus.connected, Object? sendError})
      : pure = _FakePureSocket(status: status, sendError: sendError) {
    service = TranscriptSegmentSocketService.withSocket(16000, BleAudioCodec.opus, 'en', pure);
  }

  final _FakePureSocket pure;
  late final TranscriptSegmentSocketService service;
}

Future<TranscriptSegmentSocketService?> _connectedDeviceSocket(BtDevice device, {required bool force}) async =>
    _FakeTranscriptSocket().service;

ActiveWalAuthority _activeCaptureAuthority(_CaptureAuthority authority) => ActiveWalAuthority(
      owner: WalOwner(
        uid: authority.uid,
        profileBindingId: 'profile-a',
        bindingRevision: 1,
        consentReceiptId: 'receipt-a',
        authorityGenerationAtCapture: 1,
      ),
      consent: AiConsentAuthoritySnapshot(
        generation: 1,
        uid: authority.uid,
        verifiedPersonaId: null,
        profileBindingId: 'profile-a',
        receiptId: 'receipt-a',
        policyVersion: 'policy-a',
        processorSetHash: 'processors-a',
        scopeVersion: 'scope-a',
        scopeHash: 'scope-hash-a',
      ),
      currentCheck: authority.isCurrent,
    );

Future<void> _grantCaptureEgressAuthority(String uid) async {
  SharedPreferences.setMockInitialValues({});
  await SharedPreferencesUtil.init();
  final preferences = SharedPreferencesUtil()..uid = uid;
  preferences.acceptAiConsent(
    receiptId: 'aicr_$uid',
    uid: uid,
    profileBindingId: 'profile-$uid',
    serverDecidedAt: '2026-08-13T00:00:00Z',
  );
  preferences.markAiConsentServerVerified(
    uid: uid,
    receiptId: 'aicr_$uid',
    policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
    processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
    profileBindingId: 'profile-$uid',
    scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
    scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
  );
}

ServerConversation _conversation(
  String id,
  String transcript, {
  ConversationStatus status = ConversationStatus.in_progress,
}) {
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
  test('capture socket sends no frames until exact-authority protocol readiness is acknowledged', () async {
    await _grantCaptureEgressAuthority('uid-a');
    final pure = _FakePureSocket(status: PureSocketStatus.notConnected);
    final service = TranscriptSegmentSocketService.withSocket(
      16000,
      BleAudioCodec.opus,
      'en',
      pure,
      requireCaptureProtocol: true,
      captureProtocolTimeout: const Duration(milliseconds: 100),
    );

    var started = false;
    final start = service.start().whenComplete(() => started = true);
    await pumpEventQueue();
    expect(started, isFalse);
    expect(service.state, SocketServiceState.disconnected);
    await service.send([1, 2, 3]);
    expect(pure.sent, isEmpty);

    const authorityTuple = {
      'protocol_version': 2,
      'conversation_id': 'capture-a',
      'generation': 'generation-a',
      'owner_token': 'owner-a',
    };
    pure.onMessage(jsonEncode({'type': 'service_status', 'status': 'capture_protocol_ready', ...authorityTuple}));
    await start;
    expect(service.state, SocketServiceState.connected);
    await service.send([1, 2, 3]);
    expect(pure.sent.single, [1, 2, 3]);

    final stop = service.stop();
    await pumpEventQueue();
    expect(
      jsonDecode(pure.sent.last as String),
      {'type': 'capture_drain', ...authorityTuple},
    );
    expect(pure.stops, 0, reason: 'transport close must wait for the server-side Redis fence');
    pure.onMessage(jsonEncode({'type': 'service_status', 'status': 'capture_protocol_drained', ...authorityTuple}));
    await stop;
    expect(pure.stops, 1);
  });

  test('capture socket atomically adopts successor B and stale A messages cannot kill or replace it', () async {
    await _grantCaptureEgressAuthority('uid-a');
    final pure = _FakePureSocket(status: PureSocketStatus.notConnected);
    final service = TranscriptSegmentSocketService.withSocket(
      16000,
      BleAudioCodec.opus,
      'en',
      pure,
      requireCaptureProtocol: true,
      captureProtocolTimeout: const Duration(milliseconds: 100),
    );
    const tupleA = {
      'protocol_version': 2,
      'conversation_id': 'capture-a',
      'generation': 'generation-a',
      'owner_token': 'owner-a',
    };
    const tupleB = {
      'protocol_version': 2,
      'conversation_id': 'capture-b',
      'generation': 'generation-a',
      'owner_token': 'owner-a',
    };

    final start = service.start();
    await pumpEventQueue();
    pure.onMessage(jsonEncode({'type': 'service_status', 'status': 'capture_protocol_ready', ...tupleA}));
    await start;
    await service.send([1]);

    pure.onMessage(jsonEncode({'type': 'service_status', 'status': 'capture_protocol_ready', ...tupleB}));
    expect(service.captureAuthority?.conversationId, 'capture-b');
    await service.send([2]);
    pure.onMessage(jsonEncode({'type': 'service_status', 'status': 'capture_protocol_ready', ...tupleA}));
    expect(service.captureAuthority?.conversationId, 'capture-b', reason: 'retired A cannot replace successor B');

    final stop = service.stop();
    await pumpEventQueue();
    expect(jsonDecode(pure.sent.last as String), {'type': 'capture_drain', ...tupleB});
    pure.onMessage(jsonEncode({'type': 'service_status', 'status': 'capture_protocol_drained', ...tupleA}));
    await pumpEventQueue();
    expect(pure.stops, 0, reason: 'stale A acknowledgement must not close B before its exact fence is durable');
    pure.onMessage(jsonEncode({'type': 'service_status', 'status': 'capture_protocol_drained', ...tupleB}));
    await stop;

    expect(pure.stops, 1);
    expect(pure.sent.take(2), [
      [1],
      [2],
    ]);
  });

  test('close-only provider tail is forwarded before exact server drain acknowledgement', () async {
    await _grantCaptureEgressAuthority('uid-a');
    late final _FakePureSocket primary;
    primary = _FakePureSocket(
      status: PureSocketStatus.notConnected,
      onStop: () {
        primary.onMessage(jsonEncode([
          {
            'id': 'tail-only',
            'text': 'Final words emitted only when the provider closes',
            'speaker': 'SPEAKER_00',
            'start': 0.0,
            'end': 1.0,
            'is_user': false,
          }
        ]));
      },
    );
    final secondary = _FakePureSocket(status: PureSocketStatus.notConnected);
    final composite = CompositeTranscriptionSocket(
      primarySocket: primary,
      secondarySocket: secondary,
      sttProvider: 'close-tail-provider',
    );
    final service = TranscriptSegmentSocketService.withSocket(
      16000,
      BleAudioCodec.opus,
      'en',
      composite,
      requireCaptureProtocol: true,
      captureProtocolTimeout: const Duration(milliseconds: 100),
    );
    const authorityTuple = {
      'protocol_version': 2,
      'conversation_id': 'capture-tail',
      'generation': 'generation-tail',
      'owner_token': 'owner-tail',
    };

    final start = service.start();
    await pumpEventQueue();
    secondary.onMessage(
      jsonEncode({'type': 'service_status', 'status': 'capture_protocol_ready', ...authorityTuple}),
    );
    await start;

    final stop = service.stop();
    await pumpEventQueue();
    final secondaryMessages = secondary.sent.map((value) => jsonDecode(value as String)).toList();
    final tailIndex = secondaryMessages.indexWhere((value) => value['type'] == 'suggested_transcript');
    final drainIndex = secondaryMessages.indexWhere((value) => value['type'] == 'capture_drain');
    expect(tailIndex, greaterThanOrEqualTo(0));
    expect(drainIndex, greaterThan(tailIndex));
    expect(secondary.stops, 0, reason: 'backend transport stays open until its durable drain acknowledgement');

    secondary.onMessage(
      jsonEncode({'type': 'service_status', 'status': 'capture_protocol_drained', ...authorityTuple}),
    );
    await stop;

    expect(primary.stops, 1);
    expect(secondary.stops, 1);
  });

  test('lost drain acknowledgement closes transport then continues exact-tuple finalization reconciliation', () async {
    await _grantCaptureEgressAuthority('uid-a');
    final authority = _CaptureAuthority('uid-a');
    final mic = _FakeMicRecorder();
    final pure = _FakePureSocket();
    final transcriptService = TranscriptSegmentSocketService.withSocket(
      16000,
      BleAudioCodec.opus,
      'en',
      pure,
      requireCaptureProtocol: true,
      captureProtocolTimeout: const Duration(milliseconds: 100),
    );
    const tupleB = {
      'protocol_version': 2,
      'conversation_id': 'capture-b',
      'generation': 'generation-a',
      'owner_token': 'owner-a',
    };
    pure.onMessage(jsonEncode({'type': 'service_status', 'status': 'capture_protocol_ready', ...tupleB}));

    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    var processCalls = 0;
    final processEntered = Completer<void>();
    final provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      phoneMicrophonePermissionChecker: () async => true,
      phoneTranscriptionPreparer: () async => true,
      phoneMicRecorder: mic,
      phoneAudioSender: (_) => true,
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(pure.status, PureSocketStatus.disconnected, reason: 'tail decision must follow exact drain close');
        return [_conversation('capture-b', 'Durable words before the lost acknowledgement')];
      },
      inProgressConversationProcess: (
          {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) async {
        processCalls++;
        expect(conversationId, 'capture-b');
        final operation = exactAuthority as CaptureFinalizationOperation;
        expect(operation.captureProtocolAuthority?.conversationId, 'capture-b');
        expect(operation.captureProtocolAuthority?.generation, 'generation-a');
        expect(operation.captureProtocolAuthority?.ownerToken, 'owner-a');
        expect(pure.status, PureSocketStatus.disconnected);
        processEntered.complete();
        return CreateConversationResponse(
          messages: const [],
          conversation: _conversation(
            'capture-b',
            'Durable words before the lost acknowledgement',
            status: ConversationStatus.completed,
          ),
        );
      },
    )
      ..updateProviderInstances(conversations, null, null, null)
      ..reconnectDeviceCaptureSocketForTesting(transcriptService);
    addTearDown(provider.dispose);

    final start = provider.streamRecording();
    await pumpEventQueue();
    mic.confirmRecording();
    mic.emit([1, 2, 3, 4]);
    expect(await start, PhoneCaptureStartResult.started);

    final stopping = provider.stopStreamRecordingAndFinalize();
    await pumpEventQueue();
    expect(jsonDecode(pure.sent.last as String), {'type': 'capture_drain', ...tupleB});
    pure.onClosed(); // Server committed drained, but the acknowledgement was lost with the socket.
    await processEntered.future;

    expect(await stopping, isTrue);
    expect(processCalls, 1);
    expect(pure.stops, 1);
  });

  test('capture socket fails closed when a legacy worker never acknowledges protocol authority', () async {
    await _grantCaptureEgressAuthority('uid-a');
    final pure = _FakePureSocket(status: PureSocketStatus.notConnected);
    final service = TranscriptSegmentSocketService.withSocket(
      16000,
      BleAudioCodec.opus,
      'en',
      pure,
      requireCaptureProtocol: true,
      captureProtocolTimeout: const Duration(milliseconds: 10),
    );

    await service.start();

    expect(service.state, SocketServiceState.disconnected);
    expect(pure.sent, isEmpty);
    expect(pure.stops, 1);
  });

  test('capture socket rejects missing ready fields and treats a stale drained tuple as ambiguous', () async {
    await _grantCaptureEgressAuthority('uid-a');
    final missingPure = _FakePureSocket(status: PureSocketStatus.notConnected);
    final missingService = TranscriptSegmentSocketService.withSocket(
      16000,
      BleAudioCodec.opus,
      'en',
      missingPure,
      requireCaptureProtocol: true,
      captureProtocolTimeout: const Duration(milliseconds: 100),
    );
    final missingStart = missingService.start();
    await pumpEventQueue();
    missingPure.onMessage(jsonEncode({'type': 'service_status', 'status': 'capture_protocol_ready'}));
    await missingStart;
    expect(missingService.state, SocketServiceState.disconnected);

    final stalePure = _FakePureSocket(status: PureSocketStatus.notConnected);
    final staleService = TranscriptSegmentSocketService.withSocket(
      16000,
      BleAudioCodec.opus,
      'en',
      stalePure,
      requireCaptureProtocol: true,
      captureProtocolTimeout: const Duration(milliseconds: 100),
    );
    final staleStart = staleService.start();
    await pumpEventQueue();
    stalePure.onMessage(jsonEncode({
      'type': 'service_status',
      'status': 'capture_protocol_ready',
      'protocol_version': 2,
      'conversation_id': 'capture-a',
      'generation': 'generation-a',
      'owner_token': 'owner-a',
    }));
    await staleStart;
    final staleStop = staleService.stop();
    await pumpEventQueue();
    stalePure.onMessage(jsonEncode({
      'type': 'service_status',
      'status': 'capture_protocol_drained',
      'protocol_version': 2,
      'conversation_id': 'capture-a',
      'generation': 'generation-b',
      'owner_token': 'owner-a',
    }));
    await staleStop;
    expect(stalePure.stops, 1);
    expect(staleService.captureAuthority?.conversationId, 'capture-a');
  });

  test('production phone path waits for native capture and a physical frame, not websocket delivery', () async {
    final authority = _CaptureAuthority('uid-a');
    final mic = _FakeMicRecorder();
    var transmittedFrames = 0;
    final provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      phoneMicrophonePermissionChecker: () async => true,
      phoneTranscriptionPreparer: () async => true,
      phoneMicRecorder: mic,
      phoneAudioSender: (bytes) {
        transmittedFrames++;
        return false;
      },
      captureStartProofTimeout: const Duration(milliseconds: 100),
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async => true,
    );
    addTearDown(provider.dispose);

    final start = provider.streamRecording();
    await pumpEventQueue();
    expect(provider.recordingState, RecordingState.initialising);

    var completed = false;
    start.whenComplete(() => completed = true);
    mic.confirmRecording();
    await pumpEventQueue();
    expect(completed, isFalse);
    expect(provider.recordingState, RecordingState.initialising);

    mic.emit([1, 2, 3]);
    expect(await start, PhoneCaptureStartResult.started);
    expect(transmittedFrames, 1);
    expect(provider.recordingState, RecordingState.record);
    expect(provider.captureDiagnostics.source, CaptureDiagnosticSource.phone);
    expect(provider.captureDiagnostics.phase, CaptureDiagnosticPhase.streaming);
    expect(provider.captureDiagnostics.physicalFrames, 1);
    expect(provider.captureDiagnostics.physicalBytes, 3);
    expect(provider.captureDiagnostics.transmittedFrames, 0);
  });

  test('production phone path clears one stale recorder owner and retries once', () async {
    final authority = _CaptureAuthority('uid-a');
    final mic = _FakeMicRecorder(failuresBeforeStart: 1);
    final provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      phoneMicrophonePermissionChecker: () async => true,
      phoneTranscriptionPreparer: () async => true,
      phoneMicRecorder: mic,
      phoneAudioSender: (_) => true,
      captureStartProofTimeout: const Duration(milliseconds: 100),
    );
    addTearDown(provider.dispose);

    final start = provider.streamRecording();
    while (mic.starts < 2) {
      await Future<void>.delayed(const Duration(milliseconds: 10));
    }
    mic.confirmRecording();
    mic.emit([4, 5, 6, 7]);

    expect(await start, PhoneCaptureStartResult.started);
    expect(mic.starts, 2);
    expect(mic.stops, 1);
    expect(provider.captureDiagnostics.hasPhysicalAudio, isTrue);
    expect(provider.captureDiagnostics.hasTranscriptionDelivery, isTrue);
    expect(provider.captureDiagnostics.failure, CaptureDiagnosticFailure.none);
  });

  test('production phone path cancels a native start receipt after account drift', () async {
    final authority = _CaptureAuthority('uid-a');
    final mic = _FakeMicRecorder();
    final provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      phoneMicrophonePermissionChecker: () async => true,
      phoneTranscriptionPreparer: () async => true,
      phoneMicRecorder: mic,
      phoneAudioSender: (_) => true,
      captureStartProofTimeout: const Duration(milliseconds: 100),
    );
    addTearDown(provider.dispose);

    final start = provider.streamRecording();
    while (mic.starts < 1) {
      await Future<void>.delayed(const Duration(milliseconds: 1));
    }
    authority.current = false;
    mic.confirmRecording();

    expect(await start, PhoneCaptureStartResult.cancelled);
    expect(mic.stops, 1);
    expect(provider.recordingState, RecordingState.stop);
  });

  test('rejected phone consent stops before capture authority or transport work', () async {
    var authorityReads = 0;
    var geolocationSends = 0;
    final provider = CaptureProvider(
      captureConsentAuthorityEnsurer: () async => false,
      activeWalAuthority: () {
        authorityReads++;
        return null;
      },
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        geolocationSends++;
        return true;
      },
    );
    addTearDown(provider.dispose);

    expect(await provider.streamRecording(), PhoneCaptureStartResult.consentUnavailable);
    expect(authorityReads, 0);
    expect(geolocationSends, 0);
    expect(provider.recordingState, RecordingState.stop);
  });

  test('delayed phone consent cannot start after capture generation changes', () async {
    final consent = Completer<bool>();
    var authorityReads = 0;
    var geolocationSends = 0;
    final provider = CaptureProvider(
      captureConsentAuthorityEnsurer: () => consent.future,
      activeWalAuthority: () {
        authorityReads++;
        return null;
      },
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        geolocationSends++;
        return true;
      },
    );
    addTearDown(provider.dispose);

    final start = provider.streamRecording();
    await pumpEventQueue();
    provider.reset();
    consent.complete(true);

    expect(await start, PhoneCaptureStartResult.consentUnavailable);
    expect(authorityReads, 0);
    expect(geolocationSends, 0);
    expect(provider.recordingState, RecordingState.stop);
  });

  test('account transition clears content-bearing capture diagnostics synchronously', () async {
    final authority = _CaptureAuthority('uid-a');
    final provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      phoneCaptureStarter: () async => PhoneCaptureStartResult.started,
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async => true,
    );
    addTearDown(provider.dispose);

    expect(await provider.streamRecording(), PhoneCaptureStartResult.started);
    provider.segments = [_segment('account-a', 'Private words from account A')];
    provider.onSegmentReceived([_segment('account-a', 'Private words from account A')]);
    await pumpEventQueue();
    expect(provider.captureDiagnostics.latestTranscript, 'Private words from account A');

    final transition = provider.stopForAccountTransition();

    expect(provider.captureDiagnostics.source, CaptureDiagnosticSource.none);
    expect(provider.captureDiagnostics.phase, CaptureDiagnosticPhase.idle);
    expect(provider.captureDiagnostics.latestTranscript, isEmpty);
    expect(provider.captureDiagnostics.transcriptSegments, 0);
    await transition;
  });

  test('phone capture waits briefly for verified account authority after consent refresh', () async {
    final authority = _CaptureAuthority('uid-a');
    ActiveWalAuthority? activeAuthority;
    var transportStarts = 0;
    final provider = CaptureProvider(
      captureConsentAuthorityEnsurer: () async => true,
      activeWalAuthority: () => activeAuthority,
      phoneCaptureStarter: () async {
        transportStarts++;
        return PhoneCaptureStartResult.started;
      },
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async => true,
      captureAuthorityWaitTimeout: const Duration(milliseconds: 100),
      captureAuthorityPollInterval: const Duration(milliseconds: 1),
    );
    addTearDown(provider.dispose);

    final start = provider.streamRecording();
    await pumpEventQueue(times: 1);
    expect(transportStarts, 0);

    activeAuthority = _activeCaptureAuthority(authority);
    expect(await start, PhoneCaptureStartResult.started);
    expect(transportStarts, 1);
    expect(provider.recordingState, RecordingState.record);
  });

  test('phone capture reports account readiness when exact authority never appears', () async {
    var transportStarts = 0;
    final provider = CaptureProvider(
      captureConsentAuthorityEnsurer: () async => true,
      activeWalAuthority: () => null,
      phoneCaptureStarter: () async {
        transportStarts++;
        return PhoneCaptureStartResult.started;
      },
      captureAuthorityWaitTimeout: Duration.zero,
      captureAuthorityPollInterval: Duration.zero,
    );
    addTearDown(provider.dispose);

    expect(await provider.streamRecording(), PhoneCaptureStartResult.accountNotReady);
    expect(transportStarts, 0);
    expect(provider.recordingState, RecordingState.stop);
  });

  test('account transition cancels phone capture while authority is pending', () async {
    var transportStarts = 0;
    final provider = CaptureProvider(
      captureConsentAuthorityEnsurer: () async => true,
      activeWalAuthority: () => null,
      phoneCaptureStarter: () async {
        transportStarts++;
        return PhoneCaptureStartResult.started;
      },
      captureAuthorityWaitTimeout: const Duration(milliseconds: 100),
      captureAuthorityPollInterval: const Duration(milliseconds: 1),
    );
    addTearDown(provider.dispose);

    final start = provider.streamRecording();
    await pumpEventQueue(times: 1);
    provider.reset();

    expect(await start, PhoneCaptureStartResult.cancelled);
    expect(transportStarts, 0);
    expect(provider.recordingState, RecordingState.stop);
  });

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

    expect(await provider.awaitFinalCapturableContent(maxAttempts: 3, retryDelay: Duration.zero), isFalse);
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
      inProgressConversationProcess: (
          {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) async {
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
      inProgressConversationProcess: (
          {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) {
        expect(expectedAuthenticatedUid, 'uid-a');
        expect(conversationId, 'account-a');
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
      inProgressConversationProcess: (
          {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(expectedAuthenticatedUid, 'uid-a');
        expect(conversationId, 'authoritative');
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

  test('contentful phone stop closes its transcript socket before exact processing', () async {
    final authority = _CaptureAuthority('uid-a');
    final mic = _FakeMicRecorder();
    final transcriptSocket = _FakeTranscriptSocket();
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    var processCalls = 0;
    final processEntered = Completer<void>();
    final processGate = Completer<void>();
    final provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      phoneMicrophonePermissionChecker: () async => true,
      phoneTranscriptionPreparer: () async => true,
      phoneMicRecorder: mic,
      phoneAudioSender: (_) => true,
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(expectedAuthenticatedUid, 'uid-a');
        return [_conversation('active-phone', 'Phone words before stop')];
      },
      inProgressConversationProcess: (
          {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(expectedAuthenticatedUid, 'uid-a');
        expect(conversationId, 'active-phone');
        expect(transcriptSocket.pure.status, PureSocketStatus.disconnected);
        expect(transcriptSocket.pure.stops, 1);
        processCalls++;
        processEntered.complete();
        await processGate.future;
        return CreateConversationResponse(
          messages: const [],
          conversation: _conversation(
            'completed-phone',
            'Phone words before stop',
            status: ConversationStatus.completed,
          ),
        );
      },
    )
      ..updateProviderInstances(conversations, null, null, null)
      ..reconnectDeviceCaptureSocketForTesting(transcriptSocket.service);
    addTearDown(provider.dispose);

    final start = provider.streamRecording();
    await pumpEventQueue();
    mic.confirmRecording();
    mic.emit([1, 2, 3, 4]);
    expect(await start, PhoneCaptureStartResult.started);
    expect(provider.hasUnfinalizedPhoneCaptureContent, isTrue);

    final stopping = provider.stopStreamRecordingAndFinalize();
    await processEntered.future;
    expect(provider.phoneCaptureOwnsMobileAudio, isTrue);
    expect(transcriptSocket.pure.stops, 1);

    processGate.complete();
    expect(await stopping, isTrue);
    expect(provider.phoneCaptureOwnsMobileAudio, isFalse);
    expect(processCalls, 1);
    expect(transcriptSocket.pure.stops, 1);
    expect(transcriptSocket.pure.status, PureSocketStatus.disconnected);
    expect(provider.captureDiagnostics.phase, CaptureDiagnosticPhase.completed);
    expect(provider.hasUnfinalizedPhoneCaptureContent, isFalse);
  });

  test('phone diagnostics cannot complete before the exact server status is literally completed', () async {
    final authority = _CaptureAuthority('uid-a');
    final mic = _FakeMicRecorder();
    final transcriptSocket = _FakeTranscriptSocket();
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    final pollEntered = Completer<void>();
    final completedPoll = Completer<CreateConversationResponse?>();
    var processCalls = 0;
    final provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      phoneMicrophonePermissionChecker: () async => true,
      phoneTranscriptionPreparer: () async => true,
      phoneMicRecorder: mic,
      phoneAudioSender: (_) => true,
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        return [_conversation('active-phone', 'Phone words before stop')];
      },
      inProgressConversationProcess: (
          {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) {
        processCalls++;
        if (!pollEntered.isCompleted) pollEntered.complete();
        return completedPoll.future;
      },
    )
      ..updateProviderInstances(conversations, null, null, null)
      ..reconnectDeviceCaptureSocketForTesting(transcriptSocket.service);
    addTearDown(provider.dispose);

    final start = provider.streamRecording();
    await pumpEventQueue();
    mic.confirmRecording();
    mic.emit([1, 2, 3, 4]);
    expect(await start, PhoneCaptureStartResult.started);

    final stopping = provider.stopStreamRecordingAndFinalize();
    await pollEntered.future;
    expect(provider.captureDiagnostics.phase, CaptureDiagnosticPhase.finalizing);
    expect(provider.captureDiagnostics.phase, isNot(CaptureDiagnosticPhase.completed));
    expect(conversations.conversations, isEmpty);

    completedPoll.complete(
      CreateConversationResponse(
        messages: const [],
        conversation: _conversation(
          'active-phone',
          'Phone words before stop',
          status: ConversationStatus.completed,
        ),
      ),
    );
    expect(await stopping, isTrue);
    expect(provider.captureDiagnostics.phase, CaptureDiagnosticPhase.completed);
    expect(processCalls, 1);
  });

  test('home stop and voice takeover share one phone finalization', () async {
    final authority = _CaptureAuthority('uid-a');
    final mic = _FakeMicRecorder();
    final transcriptSocket = _FakeTranscriptSocket();
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    final processEntered = Completer<void>();
    final processGate = Completer<void>();
    var processCalls = 0;
    final provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      phoneMicrophonePermissionChecker: () async => true,
      phoneTranscriptionPreparer: () async => true,
      phoneMicRecorder: mic,
      phoneAudioSender: (_) => true,
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        return [_conversation('active-phone', 'One shared phone capture')];
      },
      inProgressConversationProcess: (
          {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(conversationId, 'active-phone');
        processCalls++;
        if (!processEntered.isCompleted) processEntered.complete();
        await processGate.future;
        return CreateConversationResponse(
          messages: const [],
          conversation: _conversation(
            'completed-phone',
            'One shared phone capture',
            status: ConversationStatus.completed,
          ),
        );
      },
    )
      ..updateProviderInstances(conversations, null, null, null)
      ..reconnectDeviceCaptureSocketForTesting(transcriptSocket.service);
    addTearDown(provider.dispose);

    final start = provider.streamRecording();
    await pumpEventQueue();
    mic.confirmRecording();
    mic.emit([1, 2, 3, 4]);
    expect(await start, PhoneCaptureStartResult.started);

    final homeStop = provider.stopStreamRecordingAndFinalize();
    await processEntered.future;
    var takeoverCompleted = false;
    final takeover = provider.stopPhoneCaptureForVoiceTakeover().whenComplete(() => takeoverCompleted = true);
    await pumpEventQueue();

    expect(takeoverCompleted, isFalse);
    expect(processCalls, 1);
    expect(mic.stops, 1);
    expect(transcriptSocket.pure.stops, 1);

    processGate.complete();
    expect(await homeStop, isTrue);
    expect(await takeover, PhoneCaptureStopResult.finalized);
    expect(processCalls, 1);
    expect(mic.stops, 1);
    expect(transcriptSocket.pure.stops, 1);
    expect(provider.phoneCaptureOwnsMobileAudio, isFalse);
  });

  test('regular stop joins an active phone finalization without invalidating it', () async {
    final authority = _CaptureAuthority('uid-a');
    final mic = _FakeMicRecorder();
    final transcriptSocket = _FakeTranscriptSocket();
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    final processEntered = Completer<void>();
    final processGate = Completer<void>();
    var processCalls = 0;
    final provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      phoneMicrophonePermissionChecker: () async => true,
      phoneTranscriptionPreparer: () async => true,
      phoneMicRecorder: mic,
      phoneAudioSender: (_) => true,
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        return [_conversation('active-phone', 'Finalization survives a regular stop')];
      },
      inProgressConversationProcess: (
          {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(conversationId, 'active-phone');
        processCalls++;
        processEntered.complete();
        await processGate.future;
        return CreateConversationResponse(
          messages: const [],
          conversation: _conversation(
            'completed-phone',
            'Finalization survives a regular stop',
            status: ConversationStatus.completed,
          ),
        );
      },
    )
      ..updateProviderInstances(conversations, null, null, null)
      ..reconnectDeviceCaptureSocketForTesting(transcriptSocket.service);
    addTearDown(provider.dispose);

    final start = provider.streamRecording();
    await pumpEventQueue();
    mic.confirmRecording();
    mic.emit([1, 2, 3, 4]);
    expect(await start, PhoneCaptureStartResult.started);

    final finalization = provider.stopStreamRecordingAndFinalize();
    await processEntered.future;
    var regularStopCompleted = false;
    final regularStop = provider.stopStreamRecording().whenComplete(() => regularStopCompleted = true);
    await pumpEventQueue();

    expect(regularStopCompleted, isFalse);
    expect(processCalls, 1);
    expect(transcriptSocket.pure.stops, 1);

    processGate.complete();
    expect(await finalization, isTrue);
    await regularStop;
    expect(regularStopCompleted, isTrue);
    expect(processCalls, 1);
    expect(provider.captureDiagnostics.phase, CaptureDiagnosticPhase.completed);
  });

  test('voice takeover observes transcript content that arrives while the recorder stops', () async {
    final authority = _CaptureAuthority('uid-a');
    final stopEntered = Completer<void>();
    final stopGate = Completer<void>();
    final mic = _FakeMicRecorder(stopEntered: stopEntered, stopGate: stopGate);
    final provider = CaptureProvider(activeAccountAuthority: () => authority, phoneMicRecorder: mic)
      ..updateRecordingState(RecordingState.record);
    addTearDown(provider.dispose);

    final stopping = provider.stopPhoneCaptureForVoiceTakeover();
    await stopEntered.future;
    provider.segments = [_segment('late-final', 'Words delivered during microphone shutdown')];
    stopGate.complete();

    expect(await stopping, PhoneCaptureStopResult.failed);
    expect(provider.hasCapturableContent, isTrue);
  });

  test('voice takeover fences and joins a pending phone capture start', () async {
    final authority = _CaptureAuthority('uid-a');
    final startEntered = Completer<void>();
    final startGate = Completer<void>();
    final mic = _FakeMicRecorder();
    final provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      phoneCaptureStarter: () async {
        startEntered.complete();
        await startGate.future;
        return PhoneCaptureStartResult.started;
      },
      phoneMicRecorder: mic,
    );
    addTearDown(provider.dispose);

    final start = provider.streamRecording();
    await startEntered.future;
    var takeoverCompleted = false;
    final takeover = provider.stopPhoneCaptureForVoiceTakeover().whenComplete(() => takeoverCompleted = true);
    await pumpEventQueue();

    expect(takeoverCompleted, isFalse);
    expect(provider.recordingState, isNot(RecordingState.record));

    startGate.complete();
    expect(await start, PhoneCaptureStartResult.cancelled);
    expect(await takeover, PhoneCaptureStopResult.empty);
    expect(takeoverCompleted, isTrue);
    expect(provider.recordingState, RecordingState.stop);
    expect(mic.stops, 2);
  });

  test('regular stop fences and joins a pending phone capture start', () async {
    final authority = _CaptureAuthority('uid-a');
    final startEntered = Completer<void>();
    final startGate = Completer<void>();
    final mic = _FakeMicRecorder();
    final provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      phoneCaptureStarter: () async {
        startEntered.complete();
        await startGate.future;
        return PhoneCaptureStartResult.started;
      },
      phoneMicRecorder: mic,
    );
    addTearDown(provider.dispose);

    final start = provider.streamRecording();
    await startEntered.future;
    var stopCompleted = false;
    final stop = provider.stopStreamRecording().whenComplete(() => stopCompleted = true);
    await pumpEventQueue();

    expect(stopCompleted, isFalse);
    expect(provider.recordingState, isNot(RecordingState.record));

    startGate.complete();
    expect(await start, PhoneCaptureStartResult.cancelled);
    await stop;
    expect(stopCompleted, isTrue);
    expect(provider.recordingState, RecordingState.stop);
    expect(mic.stops, 2);
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

  test('phone capture becomes visibly unavailable when transcription closes', () async {
    final mic = _FakeMicRecorder();
    final provider = CaptureProvider(phoneMicRecorder: mic)..updateRecordingState(RecordingState.record);
    addTearDown(provider.dispose);

    provider.onClosed();

    expect(provider.recordingState, RecordingState.error);
    await pumpEventQueue();
    expect(mic.stops, 1);
    expect(provider.captureDiagnostics.failure, CaptureDiagnosticFailure.socketClosed);
  });

  test('necklace capture becomes visibly unavailable when transcription errors', () async {
    final provider = CaptureProvider()..updateRecordingState(RecordingState.deviceRecord);
    addTearDown(provider.dispose);

    provider.onError(StateError('socket unavailable'));

    expect(provider.recordingState, RecordingState.error);
    provider.onClosed();
    expect(provider.hasActiveKeepAliveTimerForTesting, isFalse);
    await pumpEventQueue();
  });

  test('unexpected phone socket loss and Home stop share one finalization', () async {
    final authority = _CaptureAuthority('uid-a');
    final mic = _FakeMicRecorder();
    final transcriptSocket = _FakeTranscriptSocket();
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    final processEntered = Completer<void>();
    final processGate = Completer<void>();
    var processCalls = 0;
    final provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      phoneMicrophonePermissionChecker: () async => true,
      phoneTranscriptionPreparer: () async => true,
      phoneMicRecorder: mic,
      phoneAudioSender: (_) => true,
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        return [_conversation('socket-loss-phone', 'Phone words before socket loss')];
      },
      inProgressConversationProcess: (
          {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(conversationId, 'socket-loss-phone');
        processCalls++;
        if (!processEntered.isCompleted) processEntered.complete();
        await processGate.future;
        return CreateConversationResponse(
          messages: const [],
          conversation: _conversation(
            'completed-phone-loss',
            'Phone words before socket loss',
            status: ConversationStatus.completed,
          ),
        );
      },
    )
      ..updateProviderInstances(conversations, null, null, null)
      ..reconnectDeviceCaptureSocketForTesting(transcriptSocket.service);
    addTearDown(provider.dispose);

    final start = provider.streamRecording();
    await pumpEventQueue();
    mic.confirmRecording();
    mic.emit([1, 2, 3, 4]);
    expect(await start, PhoneCaptureStartResult.started);

    provider.onClosed();
    await processEntered.future;
    var homeCompleted = false;
    final homeStop = provider.stopStreamRecordingAndFinalize().whenComplete(() => homeCompleted = true);
    await pumpEventQueue();

    expect(homeCompleted, isFalse);
    expect(processCalls, 1);
    expect(mic.stops, 1);

    processGate.complete();
    expect(await homeStop, isTrue);
    expect(processCalls, 1);
    expect(provider.captureDiagnostics.phase, CaptureDiagnosticPhase.completed);
  });

  test('unexpected necklace socket loss serializes finalization and restart', () async {
    final authority = _CaptureAuthority('uid-a');
    final transcriptSocket = _FakeTranscriptSocket();
    final replacementSocket = _FakeTranscriptSocket();
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    final processEntered = Completer<void>();
    final processGate = Completer<void>();
    var processCalls = 0;
    var transportStarts = 0;
    var socketPreparations = 0;
    late CaptureProvider provider;
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: (_, {required force}) async {
        socketPreparations++;
        return socketPreparations == 1 ? transcriptSocket.service : replacementSocket.service;
      },
      deviceCaptureStarter: () async {
        transportStarts++;
        provider.updateRecordingState(RecordingState.deviceRecord);
        return true;
      },
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        return [_conversation('socket-loss-necklace', 'Necklace words before socket loss')];
      },
      inProgressConversationProcess: (
          {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(conversationId, 'socket-loss-necklace');
        processCalls++;
        if (!processEntered.isCompleted) processEntered.complete();
        await processGate.future;
        return CreateConversationResponse(
          messages: const [],
          conversation: _conversation(
            'completed-necklace-loss',
            'Necklace words before socket loss',
            status: ConversationStatus.completed,
          ),
        );
      },
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async => true,
    )..updateProviderInstances(conversations, null, null, null);
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(device: necklace);
    provider.segments = [_segment('local-necklace-loss', 'Local necklace proof')];
    expect(transportStarts, 1);

    provider.onError(StateError('socket unavailable'));
    await processEntered.future;
    var homeCompleted = false;
    var restartCompleted = false;
    final homeStop = provider.stopStreamDeviceRecordingAndFinalize().whenComplete(() => homeCompleted = true);
    final restart = provider.streamDeviceRecording(device: necklace).whenComplete(() => restartCompleted = true);
    await pumpEventQueue();

    expect(homeCompleted, isFalse);
    expect(restartCompleted, isFalse);
    expect(processCalls, 1);
    expect(transportStarts, 1);

    processGate.complete();
    expect(await homeStop, isTrue);
    await restart;
    expect(processCalls, 1);
    expect(transportStarts, 2);
    expect(provider.recordingState, RecordingState.deviceRecord);
  });

  test('continuous necklace boundary finalizes before replacement connect or buffered replay', () async {
    await _grantCaptureEgressAuthority('uid-a');
    final authority = _CaptureAuthority('uid-a');
    final initialSocket = _FakeTranscriptSocket();
    final replacementSocket = _FakeTranscriptSocket();
    final processEntered = Completer<void>();
    final processGate = Completer<void>();
    final replacementEntered = Completer<void>();
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    var socketPreparations = 0;
    var transportStarts = 0;
    var processCalls = 0;
    late CaptureProvider provider;
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: (_, {required force}) async {
        socketPreparations++;
        if (socketPreparations == 2 && !replacementEntered.isCompleted) replacementEntered.complete();
        return socketPreparations == 1 ? initialSocket.service : replacementSocket.service;
      },
      deviceCaptureStarter: () async {
        transportStarts++;
        provider.updateRecordingState(RecordingState.deviceRecord);
        return true;
      },
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        return [_conversation('pre-boundary-necklace', 'Words before the Home moment')];
      },
      inProgressConversationProcess: (
          {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(conversationId, 'pre-boundary-necklace');
        expect(socketPreparations, 1, reason: 'replacement connect must remain fenced until A is terminal');
        processCalls++;
        processEntered.complete();
        await processGate.future;
        return CreateConversationResponse(
          messages: const [],
          conversation: _conversation(
            'completed-pre-boundary-necklace',
            'Words before the Home moment',
            status: ConversationStatus.completed,
          ),
        );
      },
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async => true,
    )..updateProviderInstances(conversations, null, null, null);
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(device: necklace);
    provider.segments = [_segment('local-pre-boundary', 'Local words before the boundary')];

    final boundary = provider.finalizeCurrentDeviceConversationAndContinue();
    await processEntered.future;
    expect(replacementEntered.isCompleted, isFalse);
    expect(socketPreparations, 1);

    provider.ingestDeviceAudioFrameForTesting([0, 0, 0, 10], codec: BleAudioCodec.pcm8);
    expect(replacementSocket.pure.sent, isEmpty);

    processGate.complete();
    await replacementEntered.future;
    expect(await boundary, isTrue);
    expect(processCalls, 1);
    expect(socketPreparations, 2);
    expect(replacementSocket.pure.sent, [
      [10],
    ]);
    expect(transportStarts, 1, reason: 'BLE capture must continue without a physical restart');
    expect(provider.recordingState, RecordingState.deviceRecord);
    expect(provider.segments, isEmpty, reason: 'the replacement socket starts a fresh local moment');
  });

  test('ambiguous necklace finalization fails closed before replacement connect or replay', () async {
    await _grantCaptureEgressAuthority('uid-a');
    final authority = _CaptureAuthority('uid-a');
    final initialSocket = _FakeTranscriptSocket();
    final replacementSocket = _FakeTranscriptSocket();
    final processEntered = Completer<void>();
    final processGate = Completer<void>();
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    var socketPreparations = 0;
    late CaptureProvider provider;
    provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: (_, {required force}) async {
        socketPreparations++;
        return socketPreparations == 1 ? initialSocket.service : replacementSocket.service;
      },
      deviceCaptureStarter: () async {
        provider.updateRecordingState(RecordingState.deviceRecord);
        return true;
      },
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        return [_conversation('ambiguous-boundary', 'Words before an ambiguous POST timeout')];
      },
      inProgressConversationProcess: (
          {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) async {
        processEntered.complete();
        await processGate.future;
        return null;
      },
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async => true,
    )..updateProviderInstances(conversations, null, null, null);
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );
    provider.ingestDeviceAudioFrameForTesting([0, 0, 0, 1], codec: BleAudioCodec.pcm8);
    initialSocket.pure.sent.clear();

    final boundary = provider.finalizeCurrentDeviceConversationAndContinue();
    await processEntered.future;
    provider.ingestDeviceAudioFrameForTesting([0, 0, 0, 10], codec: BleAudioCodec.pcm8);
    expect(socketPreparations, 1);

    processGate.complete();
    expect(await boundary, isFalse);
    expect(socketPreparations, 1, reason: 'unknown POST outcome cannot be treated as resumable authority');
    expect(initialSocket.pure.sent, isEmpty);
    expect(replacementSocket.pure.sent, isEmpty);
    expect(provider.recordingState, RecordingState.error);
    expect(provider.captureDiagnostics.phase, CaptureDiagnosticPhase.failed);
    expect(provider.deviceCaptureFailureTransitionsForTesting, 1);
  });

  test(
    'physical-only necklace boundary quarantines delayed pre-tap transcript and replays non-WAL frames once',
    () async {
      await _grantCaptureEgressAuthority('uid-a');
      final authority = _CaptureAuthority('uid-a');
      final initialSocket = _FakeTranscriptSocket();
      final replacementSocket = _FakeTranscriptSocket();
      final replacementEntered = Completer<void>();
      final replacementGate = Completer<TranscriptSegmentSocketService?>();
      final conversations = ConversationProvider();
      addTearDown(conversations.dispose);
      var socketPreparations = 0;
      var processCalls = 0;
      late CaptureProvider provider;
      provider = CaptureProvider(
        activeAccountAuthority: () => authority,
        activeWalAuthority: () => _activeCaptureAuthority(authority),
        captureConsentAuthorityEnsurer: () async => true,
        deviceTranscriptionSocketPreparer: (_, {required force}) {
          socketPreparations++;
          if (socketPreparations == 1) return Future.value(initialSocket.service);
          if (!replacementEntered.isCompleted) replacementEntered.complete();
          return replacementGate.future;
        },
        deviceCaptureStarter: () async {
          provider.updateRecordingState(RecordingState.deviceRecord);
          return true;
        },
        inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
          return [_conversation('pre-tap-necklace', 'Delayed words spoken before the tap')];
        },
        inProgressConversationProcess: (
            {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) async {
          expect(conversationId, 'pre-tap-necklace');
          processCalls++;
          return CreateConversationResponse(
            messages: const [],
            conversation: _conversation(
              'completed-pre-tap-necklace',
              'Delayed words spoken before the tap',
              status: ConversationStatus.completed,
            ),
          );
        },
        geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async => true,
      )..updateProviderInstances(conversations, null, null, null);
      addTearDown(provider.dispose);
      final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);

      await provider.streamDeviceRecording(device: necklace);
      provider.ingestDeviceAudioFrameForTesting([0, 0, 0, 1], codec: BleAudioCodec.pcm8);
      initialSocket.pure.sent.clear();
      expect(provider.segments, isEmpty);
      expect(provider.hasActiveDeviceCaptureBoundaryEvidence, isTrue);

      final boundary = provider.finalizeCurrentDeviceConversationAndContinue();
      await replacementEntered.future;
      initialSocket.pure.onMessage(jsonEncode([_segment('late-old', 'Late pre-tap transcript').toJson()]));
      provider.ingestDeviceAudioFrameForTesting([0, 0, 0, 10], codec: BleAudioCodec.pcm8);
      provider.ingestDeviceAudioFrameForTesting([0, 0, 0, 11], codec: BleAudioCodec.pcm8);

      expect(provider.segments, isEmpty, reason: 'the stopped pre-tap socket no longer owns the Home moment');
      expect(initialSocket.pure.sent, isEmpty, reason: 'post-boundary BLE frames must not return to the old socket');
      expect(replacementSocket.pure.sent, isEmpty, reason: 'frames remain fenced until replacement assignment');

      replacementGate.complete(replacementSocket.service);
      expect(await boundary, isTrue);

      expect(processCalls, 1);
      expect(replacementSocket.pure.sent, [
        [10],
        [11],
      ]);
      expect(provider.segments, isEmpty, reason: 'delayed pre-tap words must not appear in the new Home moment');
      expect(provider.recordingState, RecordingState.deviceRecord);
    },
  );

  test('continuous necklace replacement awaits replay failure before acknowledging the boundary', () async {
    await _grantCaptureEgressAuthority('uid-a');
    final authority = _CaptureAuthority('uid-a');
    final initialSocket = _FakeTranscriptSocket();
    final replacementSocket = _FakeTranscriptSocket(sendError: StateError('replacement socket closed'));
    final replacementEntered = Completer<void>();
    final replacementGate = Completer<TranscriptSegmentSocketService?>();
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    var socketPreparations = 0;
    var processCalls = 0;
    late CaptureProvider provider;
    provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: (_, {required force}) {
        socketPreparations++;
        if (socketPreparations == 1) return Future.value(initialSocket.service);
        if (!replacementEntered.isCompleted) replacementEntered.complete();
        return replacementGate.future;
      },
      deviceCaptureStarter: () async {
        provider.updateRecordingState(RecordingState.deviceRecord);
        return true;
      },
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        return [_conversation('replay-failure-boundary', 'Words before replay failure')];
      },
      inProgressConversationProcess: (
          {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) async {
        processCalls++;
        return CreateConversationResponse(
          messages: const [],
          conversation: _conversation(conversationId, 'done', status: ConversationStatus.completed),
        );
      },
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async => true,
    )..updateProviderInstances(conversations, null, null, null);
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );
    provider.ingestDeviceAudioFrameForTesting([0, 0, 0, 1], codec: BleAudioCodec.pcm8);
    final boundary = provider.finalizeCurrentDeviceConversationAndContinue();
    await replacementEntered.future;
    provider.ingestDeviceAudioFrameForTesting([0, 0, 0, 30], codec: BleAudioCodec.pcm8);

    replacementGate.complete(replacementSocket.service);
    expect(await boundary, isFalse);
    expect(processCalls, 1, reason: 'A must be terminal before replacement replay is attempted');
    expect(replacementSocket.pure.sent, isEmpty);
    expect(provider.recordingState, RecordingState.error);
    expect(provider.captureDiagnostics.phase, CaptureDiagnosticPhase.failed);
    expect(provider.deviceCaptureFailureTransitionsForTesting, 1);
    expect(provider.deviceCaptureSocketForTesting, isNull);
  });

  test('continuous necklace replacement buffer overflow fails visibly exactly once', () async {
    await _grantCaptureEgressAuthority('uid-a');
    final authority = _CaptureAuthority('uid-a');
    final initialSocket = _FakeTranscriptSocket();
    final replacementSocket = _FakeTranscriptSocket();
    final replacementEntered = Completer<void>();
    final replacementGate = Completer<TranscriptSegmentSocketService?>();
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    var socketPreparations = 0;
    late CaptureProvider provider;
    provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceSocketReplacementBufferMaxFrames: 1,
      deviceSocketReplacementBufferMaxBytes: 16,
      deviceTranscriptionSocketPreparer: (_, {required force}) {
        socketPreparations++;
        if (socketPreparations == 1) return Future.value(initialSocket.service);
        if (!replacementEntered.isCompleted) replacementEntered.complete();
        return replacementGate.future;
      },
      deviceCaptureStarter: () async {
        provider.updateRecordingState(RecordingState.deviceRecord);
        return true;
      },
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        return [_conversation('overflow-boundary', 'Words before overflow')];
      },
      inProgressConversationProcess: (
          {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) async {
        return CreateConversationResponse(
          messages: const [],
          conversation: _conversation(conversationId, 'done', status: ConversationStatus.completed),
        );
      },
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async => true,
    )..updateProviderInstances(conversations, null, null, null);
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );
    provider.ingestDeviceAudioFrameForTesting([0, 0, 0, 1], codec: BleAudioCodec.pcm8);
    final boundary = provider.finalizeCurrentDeviceConversationAndContinue();
    await replacementEntered.future;

    provider.ingestDeviceAudioFrameForTesting([0, 0, 0, 20], codec: BleAudioCodec.pcm8);
    provider.ingestDeviceAudioFrameForTesting([0, 0, 0, 21], codec: BleAudioCodec.pcm8);
    provider.ingestDeviceAudioFrameForTesting([0, 0, 0, 22], codec: BleAudioCodec.pcm8);
    await pumpEventQueue();

    expect(provider.recordingState, RecordingState.error);
    expect(provider.captureDiagnostics.phase, CaptureDiagnosticPhase.failed);
    expect(provider.deviceCaptureFailureTransitionsForTesting, 1);
    expect(provider.deviceCaptureSocketForTesting, isNull);
    expect(replacementSocket.pure.sent, isEmpty);

    replacementGate.complete(replacementSocket.service);
    expect(await boundary, isFalse);
    expect(provider.deviceCaptureFailureTransitionsForTesting, 1);
    expect(provider.recordingState, RecordingState.error);
    expect(replacementSocket.pure.sent, isEmpty);
  });

  test('continuous necklace replacement failure never leaves an active-looking capture', () async {
    await _grantCaptureEgressAuthority('uid-a');
    final authority = _CaptureAuthority('uid-a');
    final initialSocket = _FakeTranscriptSocket();
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    var socketPreparations = 0;
    late CaptureProvider provider;
    provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: (_, {required force}) async {
        socketPreparations++;
        return socketPreparations == 1 ? initialSocket.service : null;
      },
      deviceCaptureStarter: () async {
        provider.updateRecordingState(RecordingState.deviceRecord);
        return true;
      },
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        return [_conversation('failed-boundary', 'Words before replacement failure')];
      },
      inProgressConversationProcess: (
          {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) async {
        return CreateConversationResponse(
          messages: const [],
          conversation: _conversation(conversationId, 'done', status: ConversationStatus.completed),
        );
      },
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async => true,
    )..updateProviderInstances(conversations, null, null, null);
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );
    provider.ingestDeviceAudioFrameForTesting([0, 0, 0, 1], codec: BleAudioCodec.pcm8);

    expect(await provider.finalizeCurrentDeviceConversationAndContinue(), isFalse);
    expect(provider.recordingState, RecordingState.error);
    expect(provider.captureDiagnostics.phase, CaptureDiagnosticPhase.failed);
    expect(provider.deviceCaptureSocketForTesting, isNull);
    expect(provider.deviceCaptureFailureTransitionsForTesting, 1);
  });

  test('continuous necklace boundary consumes physical evidence until a new frame arrives', () async {
    await _grantCaptureEgressAuthority('uid-a');
    final authority = _CaptureAuthority('uid-a');
    final sockets = [_FakeTranscriptSocket(), _FakeTranscriptSocket(), _FakeTranscriptSocket()];
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    var socketPreparations = 0;
    var processCalls = 0;
    late CaptureProvider provider;
    provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: (_, {required force}) async => sockets[socketPreparations++].service,
      deviceCaptureStarter: () async {
        provider.updateRecordingState(RecordingState.deviceRecord);
        return true;
      },
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        return [_conversation('boundary-${processCalls + 1}', 'Prior moment')];
      },
      inProgressConversationProcess: (
          {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) async {
        processCalls++;
        return CreateConversationResponse(
          messages: const [],
          conversation: _conversation(
            'completed-$conversationId',
            'Prior moment',
            status: ConversationStatus.completed,
          ),
        );
      },
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async => true,
    )..updateProviderInstances(conversations, null, null, null);
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );
    provider.ingestDeviceAudioFrameForTesting([0, 0, 0, 1], codec: BleAudioCodec.pcm8);

    expect(await provider.finalizeCurrentDeviceConversationAndContinue(), isTrue);
    expect(provider.hasActiveDeviceCaptureBoundaryEvidence, isFalse);
    expect(await provider.finalizeCurrentDeviceConversationAndContinue(), isFalse);
    expect(socketPreparations, 2, reason: 'stale prior-boundary proof must not rotate a second socket');
    expect(processCalls, 1);

    provider.ingestDeviceAudioFrameForTesting([0, 0, 0, 2], codec: BleAudioCodec.pcm8);
    expect(provider.hasActiveDeviceCaptureBoundaryEvidence, isTrue);
    expect(await provider.finalizeCurrentDeviceConversationAndContinue(), isTrue);
    expect(socketPreparations, 3);
    expect(processCalls, 2);
  });

  test('necklace cannot become active with a null or disconnected transcription socket', () async {
    final authority = _CaptureAuthority('uid-a');
    final disconnected = _FakeTranscriptSocket(status: PureSocketStatus.disconnected);
    for (final prepared in <TranscriptSegmentSocketService?>[null, disconnected.service]) {
      var transportStarts = 0;
      final provider = CaptureProvider(
        activeWalAuthority: () => _activeCaptureAuthority(authority),
        captureConsentAuthorityEnsurer: () async => true,
        deviceTranscriptionSocketPreparer: (_, {required force}) async => prepared,
        deviceCaptureStarter: () async {
          transportStarts++;
          return true;
        },
      );
      addTearDown(provider.dispose);

      await provider.streamDeviceRecording(
        device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
      );

      expect(transportStarts, 0);
      expect(provider.recordingState, RecordingState.error);
      expect(provider.recordingDevice, isNull);
      expect(provider.deviceCaptureSocketForTesting, isNull);
    }
  });

  test('intentional necklace socket replacement rebinds the active session', () async {
    final authority = _CaptureAuthority('uid-a');
    final first = _FakeTranscriptSocket();
    final replacement = _FakeTranscriptSocket();
    var prepares = 0;
    late CaptureProvider provider;
    provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: (_, {required force}) async =>
          prepares++ == 0 ? first.service : replacement.service,
      deviceCaptureStarter: () async {
        provider.updateRecordingState(RecordingState.deviceRecord);
        return true;
      },
    );
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );
    expect(provider.deviceCaptureSocketForTesting, same(first.service));

    await provider.onTranscriptionSettingsChanged();

    expect(first.pure.stops, 1);
    expect(provider.deviceCaptureSocketForTesting, same(replacement.service));
    expect(provider.recordingState, RecordingState.deviceRecord);
  });

  test('necklace socket reconnect rebinds the exact active session', () async {
    final authority = _CaptureAuthority('uid-a');
    final first = _FakeTranscriptSocket();
    final reconnected = _FakeTranscriptSocket();
    late CaptureProvider provider;
    provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: (_, {required force}) async => first.service,
      deviceCaptureStarter: () async {
        provider.updateRecordingState(RecordingState.deviceRecord);
        return true;
      },
    );
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );
    provider.reconnectDeviceCaptureSocketForTesting(reconnected.service);

    expect(provider.deviceCaptureSocketForTesting, same(reconnected.service));
    expect(provider.recordingState, RecordingState.deviceRecord);
  });

  test('active necklace socket closure fails capture durably', () async {
    final authority = _CaptureAuthority('uid-a');
    final socket = _FakeTranscriptSocket();
    late CaptureProvider provider;
    provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: (_, {required force}) async => socket.service,
      deviceCaptureStarter: () async {
        provider.updateRecordingState(RecordingState.deviceRecord);
        return true;
      },
    );
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );
    provider.onClosed();
    await pumpEventQueue();

    expect(provider.recordingState, RecordingState.error);
    expect(provider.deviceCaptureSocketForTesting, isNull);
    expect(provider.hasActiveKeepAliveTimerForTesting, isFalse);
  });

  test('failed necklace socket replacement becomes a durable capture error', () async {
    final authority = _CaptureAuthority('uid-a');
    final first = _FakeTranscriptSocket();
    var prepares = 0;
    late CaptureProvider provider;
    provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: (_, {required force}) async => prepares++ == 0 ? first.service : null,
      deviceCaptureStarter: () async {
        provider.updateRecordingState(RecordingState.deviceRecord);
        return true;
      },
    );
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );
    await provider.onTranscriptionSettingsChanged();

    expect(provider.deviceCaptureSocketForTesting, isNull);
    expect(provider.recordingState, RecordingState.error);
  });

  test('replacement necklace start is serialized and stale completion cannot fail or stop it', () async {
    final authority = _CaptureAuthority('uid-a');
    final firstSocket = _FakeTranscriptSocket();
    final replacementSocket = _FakeTranscriptSocket();
    final firstStartGate = Completer<bool>();
    var prepares = 0;
    var transportStarts = 0;
    late CaptureProvider provider;
    provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: (_, {required force}) async =>
          prepares++ == 0 ? firstSocket.service : replacementSocket.service,
      deviceCaptureStarter: () {
        transportStarts++;
        if (transportStarts == 1) return firstStartGate.future;
        provider.updateRecordingState(RecordingState.deviceRecord);
        return Future.value(true);
      },
    );
    addTearDown(provider.dispose);
    final device = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);

    final firstStart = provider.streamDeviceRecording(device: device);
    await pumpEventQueue();
    expect(transportStarts, 1);
    final replacementStart = provider.streamDeviceRecording(device: device);
    await pumpEventQueue();
    expect(transportStarts, 1);

    firstStartGate.complete(false);
    await Future.wait([firstStart, replacementStart]);

    expect(transportStarts, 2);
    expect(provider.recordingState, RecordingState.deviceRecord);
    expect(provider.deviceCaptureSocketForTesting, same(replacementSocket.service));
    expect(replacementSocket.pure.stops, 0);
  });

  test('disconnect invalidates a necklace start before session creation', () async {
    final authority = _CaptureAuthority('uid-a');
    final preparerEntered = Completer<void>();
    final socketGate = Completer<TranscriptSegmentSocketService?>();
    final abandonedSocket = _FakeTranscriptSocket();
    var transportStarts = 0;
    final provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: (_, {required force}) {
        preparerEntered.complete();
        return socketGate.future;
      },
      deviceCaptureStarter: () async {
        transportStarts++;
        return true;
      },
    );
    addTearDown(provider.dispose);

    final start = provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );
    await preparerEntered.future;
    final disconnect = provider.handleRecordingDeviceDisconnected('necklace-1');
    await pumpEventQueue();
    socketGate.complete(abandonedSocket.service);

    expect(await disconnect, isTrue);
    await start;
    expect(transportStarts, 0);
    expect(provider.recordingDevice, isNull);
    expect(provider.recordingState, RecordingState.stop);
    expect(abandonedSocket.pure.stops, 1);
  });

  test('phone and necklace capture owners cannot overlap', () async {
    final authority = _CaptureAuthority('uid-a');
    var phoneStarts = 0;
    var necklaceStarts = 0;
    late CaptureProvider necklaceOwner;
    necklaceOwner = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: _connectedDeviceSocket,
      deviceCaptureStarter: () async {
        necklaceStarts++;
        necklaceOwner.updateRecordingState(RecordingState.deviceRecord);
        return true;
      },
      phoneCaptureStarter: () async {
        phoneStarts++;
        return PhoneCaptureStartResult.started;
      },
    );
    addTearDown(necklaceOwner.dispose);
    final device = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);

    await necklaceOwner.streamDeviceRecording(device: device);
    expect(await necklaceOwner.streamRecording(), PhoneCaptureStartResult.cancelled);
    expect(phoneStarts, 0);
    expect(necklaceOwner.recordingState, RecordingState.deviceRecord);

    final phoneOwner = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      phoneCaptureStarter: () async => PhoneCaptureStartResult.started,
      deviceTranscriptionSocketPreparer: _connectedDeviceSocket,
      deviceCaptureStarter: () async {
        necklaceStarts++;
        return true;
      },
    );
    addTearDown(phoneOwner.dispose);

    expect(await phoneOwner.streamRecording(), PhoneCaptureStartResult.started);
    await phoneOwner.streamDeviceRecording(device: device);
    expect(necklaceStarts, 1);
    expect(phoneOwner.recordingState, RecordingState.record);
  });

  test('rejected necklace start performs no location work', () async {
    final authority = _CaptureAuthority('uid-a');
    var locationCalls = 0;
    var transportStarts = 0;
    final provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => false,
      deviceTranscriptionSocketPreparer: _connectedDeviceSocket,
      deviceCaptureStarter: () async {
        transportStarts++;
        return true;
      },
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        locationCalls++;
        return true;
      },
    );
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );

    expect(transportStarts, 0);
    expect(locationCalls, 0);
    expect(provider.recordingState, RecordingState.error);
  });

  test('failed necklace transport performs no location work', () async {
    final authority = _CaptureAuthority('uid-a');
    var locationCalls = 0;
    final provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: _connectedDeviceSocket,
      deviceCaptureStarter: () async => false,
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        locationCalls++;
        return true;
      },
    );
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );

    expect(locationCalls, 0);
    expect(provider.recordingState, RecordingState.error);
  });

  test('stale necklace start performs no location work', () async {
    final authority = _CaptureAuthority('uid-a');
    final transport = Completer<bool>();
    var locationCalls = 0;
    final provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: _connectedDeviceSocket,
      deviceCaptureStarter: () => transport.future,
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        locationCalls++;
        return true;
      },
    );
    addTearDown(provider.dispose);

    final start = provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );
    await pumpEventQueue();
    authority.current = false;
    transport.complete(true);
    await start;

    expect(locationCalls, 0);
  });

  test('proven necklace transport emits location only after recording is current', () async {
    final authority = _CaptureAuthority('uid-a');
    var locationCalls = 0;
    late CaptureProvider provider;
    provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: _connectedDeviceSocket,
      deviceCaptureStarter: () async {
        expect(locationCalls, 0);
        provider.updateRecordingState(RecordingState.deviceRecord);
        return true;
      },
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        locationCalls++;
        return true;
      },
    );
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );
    await pumpEventQueue();

    expect(locationCalls, 1);
    expect(provider.recordingState, RecordingState.deviceRecord);
  });

  test('disconnect tears down only the exact active necklace session', () async {
    final authority = _CaptureAuthority('uid-a');
    late CaptureProvider provider;
    provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: _connectedDeviceSocket,
      deviceCaptureStarter: () async {
        provider.updateRecordingState(RecordingState.deviceRecord);
        return true;
      },
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async => true,
    );
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );

    expect(await provider.handleRecordingDeviceDisconnected('other-necklace'), isFalse);
    expect(provider.recordingState, RecordingState.deviceRecord);

    expect(await provider.handleRecordingDeviceDisconnected('necklace-1'), isTrue);
    expect(provider.recordingDevice, isNull);
    expect(provider.recordingState, RecordingState.stop);
  });

  test('contentful necklace disconnect closes its socket before exact processing', () async {
    final authority = _CaptureAuthority('uid-a');
    final transcriptSocket = _FakeTranscriptSocket();
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    var processCalls = 0;
    late CaptureProvider provider;
    provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: (_, {required force}) async => transcriptSocket.service,
      deviceCaptureStarter: () async {
        provider.updateRecordingState(RecordingState.deviceRecord);
        return true;
      },
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(expectedAuthenticatedUid, 'uid-a');
        return [_conversation('active', 'Words captured before disconnect')];
      },
      inProgressConversationProcess: (
          {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(expectedAuthenticatedUid, 'uid-a');
        expect(conversationId, 'active');
        expect(transcriptSocket.pure.status, PureSocketStatus.disconnected);
        expect(transcriptSocket.pure.stops, 1);
        processCalls++;
        return CreateConversationResponse(
          messages: const [],
          conversation: _conversation(
            'completed',
            'Words captured before disconnect',
            status: ConversationStatus.completed,
          ),
        );
      },
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async => true,
    )..updateProviderInstances(conversations, null, null, null);
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );
    provider.segments = [_segment('local', 'Local proof')];

    expect(await provider.handleRecordingDeviceDisconnected('necklace-1'), isTrue);
    expect(processCalls, 1);
    expect(transcriptSocket.pure.stops, 1);
    expect(transcriptSocket.pure.status, PureSocketStatus.disconnected);
    expect(provider.captureDiagnostics.phase, CaptureDiagnosticPhase.completed);
  });

  test('necklace disconnect and Home stop share one exact finalization', () async {
    final authority = _CaptureAuthority('uid-a');
    final transcriptSocket = _FakeTranscriptSocket();
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    final processEntered = Completer<void>();
    final processGate = Completer<void>();
    var processCalls = 0;
    late CaptureProvider provider;
    provider = CaptureProvider(
      activeAccountAuthority: () => authority,
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: (_, {required force}) async => transcriptSocket.service,
      deviceCaptureStarter: () async {
        provider.updateRecordingState(RecordingState.deviceRecord);
        return true;
      },
      inProgressConversationFetch: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        return [_conversation('active-necklace', 'One necklace memory')];
      },
      inProgressConversationProcess: (
          {required conversationId, required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(conversationId, 'active-necklace');
        processCalls++;
        if (!processEntered.isCompleted) processEntered.complete();
        await processGate.future;
        return CreateConversationResponse(
          messages: const [],
          conversation: _conversation(
            'completed-necklace',
            'One necklace memory',
            status: ConversationStatus.completed,
          ),
        );
      },
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async => true,
    )..updateProviderInstances(conversations, null, null, null);
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );
    provider.segments = [_segment('local', 'Local necklace proof')];

    final disconnect = provider.handleRecordingDeviceDisconnected('necklace-1');
    await processEntered.future;
    var homeCompleted = false;
    final homeStop = provider.stopStreamDeviceRecordingAndFinalize().whenComplete(() => homeCompleted = true);
    await pumpEventQueue();

    expect(homeCompleted, isFalse);
    expect(processCalls, 1);
    expect(transcriptSocket.pure.stops, 1);

    processGate.complete();
    expect(await disconnect, isTrue);
    expect(await homeStop, isTrue);
    expect(processCalls, 1);
    expect(transcriptSocket.pure.stops, 1);
  });

  test('idle necklace disconnect cannot cancel active phone capture', () async {
    final provider = CaptureProvider()
      ..updateRecordingDevice(BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30))
      ..updateRecordingState(RecordingState.record);
    addTearDown(provider.dispose);

    expect(await provider.handleRecordingDeviceDisconnected('necklace-1'), isFalse);
    expect(provider.recordingDevice, isNull);
    expect(provider.recordingState, RecordingState.record);
  });

  test('failed necklace state retries through a fresh exact-authority start', () async {
    final authority = _CaptureAuthority('uid-a');
    var transportStarts = 0;
    late CaptureProvider provider;
    provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: _connectedDeviceSocket,
      deviceCaptureStarter: () async {
        transportStarts++;
        provider.updateRecordingState(RecordingState.deviceRecord);
        return true;
      },
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async => true,
    )..updateRecordingState(RecordingState.error);
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );

    expect(transportStarts, 1);
    expect(provider.recordingState, RecordingState.deviceRecord);
  });

  test('phone post-proof location is cancelled and awaited across account transition', () async {
    final authority = _CaptureAuthority('uid-a');
    final entered = Completer<void>();
    final release = Completer<void>();
    late ExactAccountAuthorityVerifier requestAuthority;
    final provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      phoneCaptureStarter: () async => PhoneCaptureStartResult.started,
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(expectedAuthenticatedUid, 'uid-a');
        requestAuthority = exactAuthority;
        entered.complete();
        await release.future;
        return true;
      },
    );
    addTearDown(provider.dispose);

    expect(await provider.streamRecording(), PhoneCaptureStartResult.started);
    await entered.future;
    final location = provider.waitForCaptureGeolocationForTesting();
    expect(requestAuthority.isExactCurrent(), isTrue);

    var transitionComplete = false;
    final transition = provider.stopForAccountTransition().whenComplete(() => transitionComplete = true);
    await pumpEventQueue();
    expect(transitionComplete, isFalse);
    expect(requestAuthority.isExactCurrent(), isFalse);
    release.complete();

    final results = await location;
    expect(results, hasLength(1));
    expect(results.single, isFalse);
    await transition;
    expect(provider.recordingState, RecordingState.stop);
  });

  test('necklace post-proof location is cancelled and awaited across account transition', () async {
    final authority = _CaptureAuthority('uid-a');
    final entered = Completer<void>();
    final release = Completer<void>();
    late ExactAccountAuthorityVerifier requestAuthority;
    late CaptureProvider provider;
    provider = CaptureProvider(
      activeWalAuthority: () => _activeCaptureAuthority(authority),
      captureConsentAuthorityEnsurer: () async => true,
      deviceTranscriptionSocketPreparer: _connectedDeviceSocket,
      deviceCaptureStarter: () async {
        provider.updateRecordingState(RecordingState.deviceRecord);
        return true;
      },
      geolocationSender: ({required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(expectedAuthenticatedUid, 'uid-a');
        requestAuthority = exactAuthority;
        entered.complete();
        await release.future;
        return true;
      },
    );
    addTearDown(provider.dispose);

    await provider.streamDeviceRecording(
      device: BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30),
    );
    await entered.future;
    final location = provider.waitForCaptureGeolocationForTesting();
    expect(requestAuthority.isExactCurrent(), isTrue);

    var transitionComplete = false;
    final transition = provider.stopForAccountTransition().whenComplete(() => transitionComplete = true);
    await pumpEventQueue();
    expect(transitionComplete, isFalse);
    expect(requestAuthority.isExactCurrent(), isFalse);
    release.complete();

    final results = await location;
    expect(results, hasLength(1));
    expect(results.single, isFalse);
    await transition;
    expect(provider.recordingState, RecordingState.stop);
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
      provider.onMessageEventReceived(
        TranslationEvent(
          segments: [
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
          ],
        ),
      );

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
      provider.photos = [ConversationPhoto(id: 'temp-photo', base64: 'img', createdAt: DateTime.now())];
      final initialVersion = provider.segmentsPhotosVersion;

      provider.onMessageEventReceived(PhotoProcessingEvent(tempId: 'temp-photo', photoId: 'permanent-photo'));

      expect(provider.photos.first.id, 'permanent-photo');
      expect(provider.segmentsPhotosVersion, greaterThan(initialVersion));
    });

    test('increments on photo described event and updates description', () {
      final provider = CaptureProvider();
      provider.photos = [ConversationPhoto(id: 'photo-1', base64: 'img', createdAt: DateTime.now())];
      final initialVersion = provider.segmentsPhotosVersion;

      provider.onMessageEventReceived(PhotoDescribedEvent(photoId: 'photo-1', description: 'desc', discarded: true));

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
      final event = SpeakerLabelSuggestionEvent(speakerId: 0, personId: '', personName: 'Alice', segmentId: 'seg1');

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
