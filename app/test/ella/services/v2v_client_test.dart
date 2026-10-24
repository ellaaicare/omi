import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:record/record.dart';
import 'package:just_audio/just_audio.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/pages/ella_voice_chat_page.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/ella_entitlement_service.dart';
import 'package:omi/ella/services/v2v_client.dart';
import 'package:omi/ella/services/v2v_turn_reconciler.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';

Future<void> _grantOperationalAuthority(String uid) async {
  final preferences = SharedPreferencesUtil()..uid = uid;
  preferences.verifiedPersonaId = 'persona-a';
  preferences.acceptAiConsent(
    receiptId: 'aicr_$uid',
    uid: uid,
    profileBindingId: 'profile-$uid',
    serverDecidedAt: '2026-08-15T00:00:00Z',
  );
  await preferences.saveEllaProvisioningReceipt(uid, {
    'state': 'ready',
    'binding_state': 'active',
    'binding_revision': 3,
    'effective_policy_revision': 'policy-3',
  });
  await preferences.markEllaProvisioningVerified(uid);
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

Future<ActiveWalAuthority> _restoreOperationalAuthorityFromPreferences(String uid) async {
  final preferences = SharedPreferencesUtil();
  await preferences.markEllaProvisioningVerified(uid);
  preferences.markAiConsentServerVerified(
    uid: uid,
    receiptId: preferences.aiConsentReceiptId,
    policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
    processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
    profileBindingId: preferences.aiConsentProfileBindingId,
    scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
    scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
  );
  return WalOwnerAuthority.active(preferences: preferences, authenticatedUid: uid)!;
}

class _FakeV2VMicrophoneRecorder implements V2VMicrophoneRecorder {
  _FakeV2VMicrophoneRecorder(this.operations);

  final List<String> operations;
  final StreamController<Uint8List> _controller = StreamController<Uint8List>.broadcast();
  RecordConfig? config;
  bool _stopped = false;
  bool recording = false;
  bool failStop = false;
  bool? forcedRecordingProbe;
  int probes = 0;
  Completer<bool>? permissionGate;
  Completer<void>? startGate;
  Completer<void>? stopGate;
  Completer<void>? disposeGate;

  @override
  Future<void> useEllaManagedAudioSession() async {
    operations.add('recorder.disableAudioSessionManagement');
  }

  @override
  Future<bool> hasPermission() async {
    operations.add('recorder.permission');
    return permissionGate == null ? true : await permissionGate!.future;
  }

  @override
  Future<Stream<Uint8List>> startStream(RecordConfig config) async {
    operations.add('recorder.startStream');
    this.config = config;
    await startGate?.future;
    recording = true;
    return _controller.stream;
  }

  @override
  Future<String?> stop() async {
    operations.add('recorder.stop');
    await stopGate?.future;
    if (failStop) throw StateError('owned native stop failed');
    recording = false;
    if (!_stopped) {
      _stopped = true;
      await _controller.close();
    }
    return null;
  }

  @override
  Future<bool> isRecording() async {
    probes++;
    return forcedRecordingProbe ?? recording;
  }

  @override
  Future<void> dispose() async {
    operations.add('recorder.dispose');
    await disposeGate?.future;
  }
}

class _DisposedBeforeReceiptPlayer implements AudioPlayer {
  int disposeCalls = 0;
  bool fail = true;
  @override
  Future<void> dispose() async {
    disposeCalls++;
    if (fail) throw StateError('native dispose acknowledgement missing');
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class _FakeV2VOutputPlayer implements V2VOutputPlayer {
  final entered = Completer<void>();
  final played = Completer<void>();
  Completer<void>? loadGate;
  Completer<bool>? stopGate;
  Completer<bool>? disposeGate;
  bool failStop = false, failDispose = false;
  int plays = 0, stops = 0, disposals = 0;
  @override
  bool get reusableAfterStop => true;
  @override
  Future<void> setVolume(double volume) async {}
  @override
  Future<void> setFilePath(String path) async {
    if (!entered.isCompleted) entered.complete();
    await loadGate?.future;
  }

  @override
  Future<void> play() async {
    plays++;
    await played.future;
  }

  @override
  Future<bool> stopAndConfirm() async {
    stops++;
    if (failStop) throw StateError('exact output stop failed');
    final confirmed = await (stopGate?.future ?? Future.value(true));
    if (confirmed && !played.isCompleted) played.complete();
    return confirmed;
  }

  @override
  Future<bool> disposeAndConfirm() async {
    disposals++;
    if (failDispose) throw StateError('exact output dispose failed');
    return await (disposeGate?.future ?? Future.value(true));
  }
}

Stream<dynamic> _acknowledgedStream(Stream<dynamic> stream, String sessionId) async* {
  yield jsonEncode({
    'type': 'provider_ready',
    'ready_protocol': 1,
    'provider': 'grok-voice',
    'session_id': sessionId,
    'configured_model': 'fixture-grok-model'
  });
  yield* stream;
}

// Models a platform stream delivering one already-buffered callback during cancellation.
class _LateDeliveryStream extends Stream<dynamic> {
  _LateDeliveryStream(this.source);
  final Stream<dynamic> source;
  void Function(dynamic)? _listener;
  void deliverLate(dynamic frame) => _listener?.call(frame);
  @override
  StreamSubscription<dynamic> listen(void Function(dynamic)? onData,
      {Function? onError, void Function()? onDone, bool? cancelOnError}) {
    _listener = onData;
    return source.listen(onData, onError: onError, onDone: onDone, cancelOnError: cancelOnError);
  }
}

class _ReadinessHarness {
  _ReadinessHarness(
      {bool negotiated = true,
      Future<bool> Function()? startMic,
      Future<void> Function()? onCancel,
      Future<void> Function()? configureSession,
      Stream<dynamic> Function(Stream<dynamic>)? wrapStream,
      Duration timeout = const Duration(seconds: 16)}) {
    socket = StreamController<dynamic>(sync: true, onListen: () => attached.complete(), onCancel: onCancel);
    client = V2VClient(
      onConnectionChanged: states.add,
      onEvent: (event) {
        events.add(event.type);
        eventValues.add(event);
      },
      providerRegistryValidator: (_) async => null,
      sessionCreator: (_, provider, __) async => {
        'session_token': 'opaque-fixture-token',
        'provider': provider,
        'voice_endpoint': negotiated ? 'wss://example.invalid/voice?ready_protocol=1' : 'wss://example.invalid/voice',
        if (negotiated) 'ready_protocol': 1,
        'session_id': 'ready-session',
      },
      audioSessionConfigurator: configureSession ?? () async {},
      audioSessionReleaser: () async {
        releases++;
      },
      microphoneStarter: () async {
        micStarts++;
        return startMic == null ? true : await startMic();
      },
      wavPlayback: (_) async {
        playbacks++;
      },
      webSocketConnector: (uri) {
        endpoint = uri;
        return V2VWebSocketTransport(
            ready: Future<void>.value(),
            stream: wrapStream == null ? socket.stream : wrapStream(socket.stream),
            send: sent.add,
            close: () async {
              closes++;
              if (!socket.isClosed) await socket.close();
            });
      },
      providerReadinessTimeout: timeout,
    );
  }
  late final V2VClient client;
  late final StreamController<dynamic> socket;
  final attached = Completer<void>();
  final states = <bool>[];
  final events = <String>[];
  final eventValues = <V2VEvent>[];
  final sent = <dynamic>[];
  Uri? endpoint;
  int micStarts = 0, playbacks = 0, closes = 0, releases = 0;
  Map<String, dynamic> ready({String session = 'ready-session', String provider = 'grok-voice', Object version = 1}) =>
      {
        'type': 'provider_ready',
        'ready_protocol': version,
        'provider': provider,
        'session_id': session,
        'configured_model': 'fixture-model'
      };
  void emit(Map<String, dynamic> frame) => socket.add(jsonEncode(frame));
}

void main() {
  group('typed voice policy outcomes', () {
    test('session issuance receipt distinguishes policy denial from provider failure', () {
      const policyReceipt = V2VConnectionReceipt(
        connected: false,
        provider: 'grok-voice',
        stage: V2VConnectionStage.session,
        httpStatus: 429,
        errorCode: 'quota_daily',
      );
      const technicalReceipt = V2VConnectionReceipt(
        connected: false,
        provider: 'grok-voice',
        stage: V2VConnectionStage.session,
        httpStatus: 503,
        errorCode: 'provider_unavailable',
      );

      expect(policyReceipt.isPolicyDenial, isTrue);
      expect(policyReceipt.policyReason, EllaVoicePolicyReason.quotaDaily);
      expect(technicalReceipt.isPolicyDenial, isFalse);
      expect(technicalReceipt.policyReason, isNull);
    });

    test('session close parses nested typed reason without using human message text', () {
      expect(
        V2VClient.policyReasonFromEvent({
          'type': 'session_end',
          'detail': {'termination_reason': 'quota_monthly'},
          'message': 'Session finished',
        }),
        EllaVoicePolicyReason.quotaMonthly,
      );
      expect(
        V2VClient.policyReasonFromEvent({'type': 'error', 'code': 'websocket_closed', 'message': 'quota_daily'}),
        isNull,
      );
    });

    test('quota_state frames preserve authoritative warning and boundary stop data', () {
      final warning = V2VClient.quotaEventFromEvent({
        'type': 'quota_state',
        'state': 'soft_warning',
        'quota': {
          'daily_used_s': 2400,
          'daily_limit_s': 2700,
          'monthly_used_s': 2400,
          'monthly_limit_s': 43200,
          'max_session_s': 1200,
          'resets_at': '2026-07-27T07:00:00Z',
        },
      });
      final stop = V2VClient.quotaEventFromEvent({
        'type': 'quota_state',
        'state': 'quota_daily',
        'turn_boundary': true,
        'quota': {'resets_at': '2026-07-27T07:00:00Z'},
      });

      expect(warning?.quotaState, 'soft_warning');
      expect(warning?.quota?.dailyUsedSeconds, 2400);
      expect(warning?.policyReason, isNull);
      expect(stop?.policyReason, EllaVoicePolicyReason.quotaDaily);
      expect(stop?.turnBoundary, isTrue);
      expect(stop?.resetsAt, isNotNull);
      expect(V2VClient.quotaEventFromEvent({'type': 'transcript'}), isNull);
    });
  });

  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  group('V2VClient provider contract', () {
    void grantCurrentConsent({String uid = 'uid-a', String profileBindingId = 'profile-binding-a'}) {
      final preferences = SharedPreferencesUtil();
      preferences.uid = uid;
      preferences.verifiedPersonaId = 'persona-a';
      preferences.acceptAiConsent(
        receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
        uid: uid,
        profileBindingId: profileBindingId,
        serverDecidedAt: '2026-07-27T00:00:00Z',
      );
      preferences.markAiConsentServerVerified(
        uid: uid,
        receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
        policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
        processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
        profileBindingId: profileBindingId,
        scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
        scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
      );
    }

    test('normalizes legacy provider ids and recognizes canonical session providers', () {
      expect(V2VClient.normalizeProvider('gemini-live'), 'gemini-native-live');
      expect(V2VClient.normalizeProvider('openai-realtime'), 'openai-native-realtime');
      expect(V2VClient.isSessionProvider('grok-voice'), isTrue);
      expect(V2VClient.isSessionProvider('gemini-live'), isTrue);
      expect(V2VClient.isSessionProvider('gemini-native-live'), isTrue);
      expect(V2VClient.isSessionProvider('elevenlabs'), isFalse);
    });

    test('retained compatibility receipt falls back to selected local provider', () {
      expect(
        V2VClient.resolveEffectiveProvider(provisionedProvider: '', selectedProvider: 'gemini-live'),
        'gemini-native-live',
      );
      expect(
        V2VClient.resolveEffectiveProvider(provisionedProvider: 'grok-voice', selectedProvider: 'gemini-native-live'),
        'grok-voice',
      );
    });

    test('authenticated session body preserves provider and mode while omitting uid', () {
      expect(V2VClient.buildSessionRequestBody(uid: 'firebase-user-1', provider: 'gemini-live', includeUid: false), {
        'provider': 'gemini-native-live',
        'voice_mode': 'gemini-native-live-v1',
      });
    });

    test('memory scope serializes identifiers only for Grok and Gemini and survives retry', () {
      const scope = V2VSessionScope.memory(
        conversationId: 'conversation-42',
        expectedActiveSummaryVersionId: 'summary-v7',
      );
      for (final provider in ['grok-voice', 'gemini-native-live']) {
        final firstAttempt = V2VClient.buildSessionRequestBody(
          uid: 'firebase-user-1',
          provider: provider,
          includeUid: false,
          sessionScope: scope,
        );
        final retryAttempt = V2VClient.buildSessionRequestBody(
          uid: 'firebase-user-1',
          provider: provider,
          includeUid: false,
          sessionScope: scope,
        );

        expect(retryAttempt, firstAttempt);
        expect(firstAttempt['provider'], provider);
        expect(firstAttempt['voice_mode'], provider == 'grok-voice' ? 'v4' : 'gemini-native-live-v1');
        expect(firstAttempt['session_scope'], {
          'kind': 'memory',
          'conversation_id': 'conversation-42',
          'expected_active_summary_version_id': 'summary-v7',
        });
        expect(firstAttempt, isNot(contains('uid')));
        expect(firstAttempt, isNot(contains('title')));
        expect(firstAttempt, isNot(contains('overview')));
        expect(firstAttempt, isNot(contains('people')));
        expect(firstAttempt, isNot(contains('transcript')));
        expect(firstAttempt, isNot(contains('system_prompt')));
      }
    });

    test('memory scope omits an unknown active version rather than inventing one', () {
      const scope = V2VSessionScope.memory(conversationId: 'conversation-42');

      expect(
        V2VClient.buildSessionRequestBody(
          uid: 'firebase-user-1',
          provider: 'grok-voice',
          includeUid: false,
          sessionScope: scope,
        )['session_scope'],
        {'kind': 'memory', 'conversation_id': 'conversation-42'},
      );
    });

    test('daily card scope sends identifiers and version only', () {
      const scope = V2VSessionScope.dailyCard(cardId: 'today-card-42', expectedVersion: 3);

      expect(
        V2VClient.buildSessionRequestBody(
          uid: 'firebase-user-1',
          provider: 'grok-voice',
          includeUid: false,
          sessionScope: scope,
        )['session_scope'],
        {'kind': 'daily_card', 'card_id': 'today-card-42', 'expected_version': 3},
      );
    });

    test('accepts only a matching identifier-only resolved scope', () {
      const requested = V2VSessionScope.memory(
        conversationId: 'conversation-42',
        expectedActiveSummaryVersionId: 'summary-v1',
      );
      final resolved = V2VResolvedSessionScope.tryParse({
        'kind': 'memory',
        'conversation_id': 'conversation-42',
        'active_summary_version_id': 'summary-v2',
        'can_reinterpret': true,
        'overview': 'ignored by the client',
      });

      expect(resolved, isNotNull);
      expect(resolved!.matches(requested), isTrue);
      expect(resolved.activeSummaryVersionId, 'summary-v2');
      expect(
        V2VResolvedSessionScope.tryParse({
          'kind': 'memory',
          'conversation_id': 'another-conversation',
          'active_summary_version_id': 'summary-v2',
          'can_reinterpret': true,
        })!
            .matches(requested),
        isFalse,
      );
    });

    test('accepts only the exact resolved daily card version', () {
      const requested = V2VSessionScope.dailyCard(cardId: 'today-card-42', expectedVersion: 3);
      final resolved = V2VResolvedSessionScope.tryParse({
        'kind': 'daily_card',
        'card_id': 'today-card-42',
        'card_version': 3,
        'can_reinterpret': false,
      });

      expect(resolved, isNotNull);
      expect(resolved!.matches(requested), isTrue);
      expect(
        V2VResolvedSessionScope.tryParse({
          'kind': 'daily_card',
          'card_id': 'today-card-42',
          'card_version': 4,
          'can_reinterpret': false,
        })!
            .matches(requested),
        isFalse,
      );
    });

    test('missing and non-owned scoped memories expose the same safe failure', () {
      const scope = V2VSessionScope.memory(conversationId: 'conversation-42');

      final missing = V2VClient.sessionFailureCode(
        statusCode: 404,
        body: '{"detail":{"code":"voice_session_scope_not_found"}}',
        sessionScope: scope,
      );
      final nonOwned = V2VClient.sessionFailureCode(
        statusCode: 404,
        body: '{"detail":{"code":"different_internal_code"}}',
        sessionScope: scope,
      );

      expect(missing, 'voice_session_scope_unavailable');
      expect(nonOwned, missing);
    });

    test('stale scoped versions normalize to refresh behavior without dropping scope', () {
      const scope = V2VSessionScope.memory(
        conversationId: 'conversation-42',
        expectedActiveSummaryVersionId: 'summary-v1',
      );
      final code = V2VClient.sessionFailureCode(
        statusCode: 409,
        body: '{"detail":{"code":"voice_session_scope_version_unavailable"}}',
        sessionScope: scope,
      );
      final refreshed = EllaVoiceChatPage.refreshedMemoryScope(scope, 'summary-v2');

      expect(code, 'voice_session_scope_stale');
      expect(refreshed.conversationId, scope.conversationId);
      expect(refreshed.expectedActiveSummaryVersionId, 'summary-v2');
    });

    test('only Grok and Gemini can carry memory scope', () {
      expect(V2VClient.isMemoryScopedProvider('grok-voice'), isTrue);
      expect(V2VClient.isMemoryScopedProvider('gemini-live'), isTrue);
      expect(V2VClient.isMemoryScopedProvider('openai-native-realtime'), isFalse);
      expect(V2VClient.isMemoryScopedProvider('elevenlabs'), isFalse);
    });

    test('scoped voice turns never append to general Chat', () {
      expect(EllaVoiceChatPage.shouldInjectVoiceTurns(null), isTrue);
      expect(
        EllaVoiceChatPage.shouldInjectVoiceTurns(const V2VSessionScope.memory(conversationId: 'conversation-42')),
        isFalse,
      );
      expect(
        EllaVoiceChatPage.shouldInjectVoiceTurns(
          const V2VSessionScope.dailyCard(cardId: 'today-card-42', expectedVersion: 3),
        ),
        isFalse,
      );
    });

    test('legacy session body includes uid without changing canonical provider', () {
      expect(V2VClient.buildSessionRequestBody(uid: 'legacy-user-1', provider: 'grok-voice', includeUid: true), {
        'uid': 'legacy-user-1',
        'provider': 'grok-voice',
        'ready_protocol': 1,
      });
    });

    test('provider registry filters unavailable and non-V2V entries and deduplicates aliases', () {
      final providers = V2VClient.availableSessionProviders({
        'providers': [
          {'id': 'elevenlabs', 'type': 'tts', 'available': true},
          {'id': 'grok-voice', 'type': 'v2v', 'available': false},
          {'id': 'gemini-live', 'type': 'v2v', 'available': true},
          {'id': 'gemini-native-live', 'type': 'v2v', 'available': true},
        ],
      });

      expect(providers, {'gemini-native-live'});
    });

    test('failure receipts extract bounded codes without retaining opaque tokens', () {
      expect(V2VClient.safeErrorCode('{"detail":{"code":"isolated_voice_not_ready"}}'), 'isolated_voice_not_ready');
      expect(
        V2VClient.safeErrorCode(
          '{"detail":"eyJhbGciOiJIUzI1NiJ9.payload.signature"}',
          fallback: 'session_request_failed',
        ),
        'session_request_failed',
      );
      expect(
        V2VClient.safeErrorCode('{"detail":"Provider grok-voice is not configured (missing API key)"}'),
        'provider_not_configured',
      );
    });

    test('declined current consent blocks before V2V identity or session requests', () async {
      final preferences = SharedPreferencesUtil();
      preferences.uid = 'uid-a';
      preferences.deferAiConsent();
      final client = V2VClient(onEvent: (_) {}, onConnectionChanged: (_) {});

      final receipt = await client.connect(provider: 'grok-voice');

      expect(receipt.connected, isFalse);
      expect(receipt.stage, V2VConnectionStage.consent);
      expect(receipt.errorCode, 'ai_consent_required');
      expect(client.isConnected, isFalse);
      await client.disconnect();
    });

    test('disconnect releases the interactive iOS audio session', () async {
      var releases = 0;
      final client = V2VClient(
        onEvent: (_) {},
        onConnectionChanged: (_) {},
        audioSessionReleaser: () async => releases++,
      );

      await client.disconnect();

      expect(releases, 1);
    });

    test('accepted server-verified v8 consent passes the mic gate before identity validation', () async {
      grantCurrentConsent();
      final client = V2VClient(onEvent: (_) {}, onConnectionChanged: (_) {});

      final receipt = await client.connect(provider: 'grok-voice');

      expect(receipt.connected, isFalse);
      expect(receipt.stage, isNot(V2VConnectionStage.consent));
      expect(receipt.errorCode, isNot('ai_consent_required'));
      await client.disconnect();
    });

    test('cancelled startup stops before provider or session work begins', () async {
      final client = V2VClient(onEvent: (_) {}, onConnectionChanged: (_) {});

      final receipt = await client.connect(provider: 'grok-voice', shouldContinue: () => false);

      expect(receipt.connected, isFalse);
      expect(receipt.stage, V2VConnectionStage.providerRegistry);
      expect(receipt.errorCode, 'connection_cancelled');
      expect(client.isConnected, isFalse);
      await client.disconnect();
    });

    test('cold re-grant cannot ABA-hydrate the revoked server authority outbox', () async {
      await _grantOperationalAuthority('uid-a');
      final authorityA = WalOwnerAuthority.active(
        preferences: SharedPreferencesUtil(),
        authenticatedUid: 'uid-a',
      )!;
      final directory = await Directory.systemTemp.createTemp('ella-v2v-cold-regrant-');
      addTearDown(() async {
        if (directory.existsSync()) await directory.delete(recursive: true);
      });
      final firstProcessStore = FileV2VTurnDurableStore(baseDirectory: directory);
      const oldTurnId = 'v2v-turn-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
      await firstProcessStore.put(
        V2VTranscriptTurn(
          uid: authorityA.uid,
          ownerNamespace: authorityA.owner.storageNamespace,
          authorityFingerprint: authorityA.owner.authorityFingerprint,
          sessionId: 'revoked-session',
          turnId: oldTurnId,
          userEventId: '$oldTurnId:user',
          assistantEventId: '$oldTurnId:assistant',
          userTranscript: 'Old question',
          assistantTranscript: 'Old answer',
          startedAt: DateTime.utc(2026, 8, 15, 21),
          completedAt: DateTime.utc(2026, 8, 15, 21, 0, 1),
        ),
        isAuthorityCurrent: () => true,
      );

      SharedPreferencesUtil.resetProcessLocalAuthorityStateForTesting();
      await SharedPreferencesUtil.init();
      final restoredAuthorityA = await _restoreOperationalAuthorityFromPreferences('uid-a');
      expect(restoredAuthorityA.owner.authorityFingerprint, authorityA.owner.authorityFingerprint);

      final preferences = SharedPreferencesUtil();
      preferences.declineAiConsent();
      preferences.acceptAiConsent(
        receiptId: 'aicr_uid-a-regrant',
        uid: 'uid-a',
        profileBindingId: 'profile-uid-a',
        serverDecidedAt: '2026-08-15T01:00:00Z',
      );
      final authorityB = await _restoreOperationalAuthorityFromPreferences('uid-a');
      expect(authorityB.owner.storageNamespace, authorityA.owner.storageNamespace);
      expect(authorityB.owner.authorityFingerprint, isNot(authorityA.owner.authorityFingerprint));

      SharedPreferencesUtil.resetProcessLocalAuthorityStateForTesting();
      await SharedPreferencesUtil.init();
      final coldAuthorityB = await _restoreOperationalAuthorityFromPreferences('uid-a');
      expect(coldAuthorityB.owner.authorityFingerprint, authorityB.owner.authorityFingerprint);

      final writes = <V2VTranscriptTurn>[];
      final reconciler = V2VTurnReconciler(
        durableStore: FileV2VTurnDurableStore(baseDirectory: directory),
        writer: (turn) async {
          writes.add(turn);
          return true;
        },
      );
      final testAuthority = ActiveWalAuthority(
        owner: coldAuthorityB.owner,
        consent: coldAuthorityB.consent,
        currentCheck: () {
          final currentOwner = WalOwnerAuthority.currentOwner(
            preferences: SharedPreferencesUtil(),
            authenticatedUid: 'uid-a',
          );
          return currentOwner != null &&
              coldAuthorityB.owner.matches(currentOwner) &&
              coldAuthorityB.consent.isCurrent(preferences: SharedPreferencesUtil());
        },
      );
      expect(
        await armEllaVoiceTranscriptSessionBeforeTransport(
          reconciler: reconciler,
          authenticatedUid: 'uid-a',
          sessionId: 'new-session',
          persistTranscriptTurns: true,
          isStartupCurrent: () => true,
          authorityCapture: (_) => testAuthority,
        ),
        isTrue,
      );
      await reconciler.settle();
      expect(writes, isEmpty);
    });

    test('Grok registry requires advertised integer protocol1 without applying it to other providers', () {
      Map<String, dynamic> registry(Object? versions) => {
            'providers': [
              {'id': 'grok-voice', 'type': 'v2v', 'available': true, 'ready_protocols': versions}
            ]
          };
      expect(V2VClient.registrySupportsGrokReady(registry([1])), isTrue);
      for (final versions in [
        [],
        ['1'],
        [1.0],
        [0],
        null
      ]) {
        expect(V2VClient.registrySupportsGrokReady(registry(versions)), isFalse);
      }
      expect(
          V2VClient.buildSessionRequestBody(uid: 'owner', provider: 'grok-voice', includeUid: false)['ready_protocol'],
          1);
      for (final provider in ['gemini-native-live', 'openclaw-direct']) {
        expect(
            V2VClient.buildSessionRequestBody(uid: 'owner', provider: provider, includeUid: false)
                .containsKey('ready_protocol'),
            isFalse);
      }
    });
    test('missing readiness negotiation fails before route or microphone activation', () async {
      grantCurrentConsent();
      final h = _ReadinessHarness(negotiated: false);
      final receipt = await h.client.connect(provider: 'grok-voice');
      expect(receipt.errorCode, 'provider_ready_protocol_unsupported');
      expect(h.micStarts, 0);
      expect(h.endpoint, isNull);
      expect(h.states, isEmpty);
      await h.client.disconnect();
    });
    test('transport-open and pre-ready media remain pending; cancellation cannot auto-replay or reactivate', () async {
      grantCurrentConsent();
      final h = _ReadinessHarness();
      final connect = h.client.connect(provider: 'grok-voice');
      await h.attached.future;
      h.socket.add(Uint8List.fromList([1, 2, 3, 4]));
      h.emit({'type': 'transcript', 'text': 'must be dropped'});
      h.emit({'type': 'speech_started'});
      h.emit({'type': 'audio_done'});
      await Future<void>.delayed(Duration.zero);
      expect(h.client.isConnected, isFalse);
      expect(h.micStarts, 0);
      expect(h.states, isEmpty);
      expect(h.events, isEmpty);
      expect(h.playbacks, 0);
      expect(h.sent, isEmpty);
      await h.client.disconnect();
      final receipt = await connect;
      expect(receipt.connected, isFalse);
      expect(receipt.errorCode, 'connect_cancelled');
      h.client.handleMessageForTesting(jsonEncode(h.ready()));
      expect(h.client.isConnected, isFalse);
      expect(h.micStarts, 0);
      expect(h.closes, 1);
    });
    test('buffered exact ACK starts microphone once; duplicates and pre-ready media do not replay', () async {
      grantCurrentConsent();
      final h = _ReadinessHarness();
      h.socket.add(Uint8List.fromList([1, 2, 3, 4]));
      h.emit(h.ready());
      h.emit(h.ready());
      final receipt = await h.client.connect(provider: 'grok-voice');
      expect(receipt.connected, isTrue);
      expect(h.micStarts, 1);
      expect(h.states, [true]);
      expect(h.playbacks, 0);
      expect(h.endpoint!.queryParameters['ready_protocol'], '1');
      h.emit(h.ready());
      await Future<void>.delayed(Duration.zero);
      expect(h.micStarts, 1);
      expect(h.states, [true]);
      await h.client.disconnect();
      expect(h.states, [true, false]);
      expect(h.closes, 1);
    });
    for (final mismatch in ['session', 'provider', 'version', 'model']) {
      test('mismatched readiness $mismatch fails without microphone or connected pulse', () async {
        grantCurrentConsent();
        final h = _ReadinessHarness();
        final connect = h.client.connect(provider: 'grok-voice');
        await h.attached.future;
        final ack = h.ready();
        switch (mismatch) {
          case 'session':
            ack['session_id'] = 'old-session';
          case 'provider':
            ack['provider'] = 'gemini-native-live';
          case 'version':
            ack['ready_protocol'] = '1';
          case 'model':
            ack['configured_model'] = '';
        }
        h.emit(ack);
        final receipt = await connect;
        expect(receipt.errorCode, 'provider_ready_protocol_mismatch');
        expect(h.micStarts, 0);
        expect(h.states, isEmpty);
        expect(h.closes, 1);
      });
    }
    test('upstream403 preserves fixed failure code/status and drops raw error detail', () async {
      grantCurrentConsent();
      final h = _ReadinessHarness();
      final connect = h.client.connect(provider: 'grok-voice');
      await h.attached.future;
      h.emit({
        'type': 'error',
        'ready_protocol': 1,
        'provider': 'grok-voice',
        'session_id': 'ready-session',
        'terminal': true,
        'code': 'provider_handshake_rejected',
        'http_status': 403,
        'message': 'secret arbitrary upstream text must not be retained'
      });
      final receipt = await connect;
      expect(receipt.errorCode, 'provider_handshake_rejected');
      expect(receipt.httpStatus, 403);
      expect(receipt.safeDetail, isNot(contains('secret')));
      expect(h.states, isEmpty);
      expect(h.micStarts, 0);
    });
    test('readiness timeout cleans up once and never requests microphone', () async {
      grantCurrentConsent();
      final h = _ReadinessHarness(timeout: const Duration(milliseconds: 20));
      final receipt = await h.client.connect(provider: 'grok-voice');
      expect(receipt.errorCode, 'provider_ready_timeout');
      expect(h.closes, 1);
      expect(h.releases, 1);
      expect(h.micStarts, 0);
      expect(h.states, isEmpty);
    });
    test('owner retirement during pending readiness fails before microphone', () async {
      grantCurrentConsent();
      final h = _ReadinessHarness();
      final connect = h.client.connect(provider: 'grok-voice');
      await h.attached.future;
      SharedPreferencesUtil().deferAiConsent();
      final receipt = await connect.timeout(const Duration(seconds: 1));
      expect(receipt.connected, isFalse);
      expect(h.micStarts, 0);
      expect(h.states, isEmpty);
      expect(h.closes, 1);
    });
    test('ACK followed by mic failure cannot announce active or play queued audio', () async {
      grantCurrentConsent();
      final h = _ReadinessHarness(startMic: () async => false);
      final connect = h.client.connect(provider: 'grok-voice');
      await h.attached.future;
      h.emit(h.ready());
      h.socket.add(Uint8List.fromList([1, 2, 3, 4]));
      final receipt = await connect;
      expect(receipt.connected, isFalse);
      expect(receipt.errorCode, 'microphone_unavailable');
      expect(h.states, isEmpty);
      expect(h.playbacks, 0);
      expect(h.closes, 1);
    });
    for (final terminalType in ['error', 'session_end', 'socket_close']) {
      test('ACK then $terminalType in one delivery cannot start microphone or announce active', () async {
        grantCurrentConsent();
        final h = _ReadinessHarness();
        final connect = h.client.connect(provider: 'grok-voice');
        await h.attached.future;
        h.emit(h.ready());
        if (terminalType == 'socket_close') {
          unawaited(h.socket.close());
        } else {
          h.emit({
            'type': terminalType,
            'ready_protocol': 1,
            'provider': 'grok-voice',
            'session_id': 'ready-session',
            'terminal': true,
            'code': 'provider_handshake_rejected',
            'http_status': 403,
          });
        }
        final receipt = await connect;
        expect(receipt.connected, isFalse);
        expect(receipt.errorCode,
            terminalType == 'socket_close' ? 'provider_startup_client_closed' : 'provider_handshake_rejected');
        expect(h.states, isEmpty);
        expect(h.micStarts, 0);
        expect(h.closes, 1);
      });
    }
    for (final interruption in ['error', 'socket_close', 'authority']) {
      test('ACK then $interruption while microphone awaits cannot announce active or deliver media', () async {
        grantCurrentConsent();
        final micEntered = Completer<void>();
        final micFinish = Completer<bool>();
        final h = _ReadinessHarness(startMic: () {
          micEntered.complete();
          return micFinish.future;
        });
        final connect = h.client.connect(provider: 'grok-voice');
        await h.attached.future;
        h.emit(h.ready());
        await micEntered.future;
        if (interruption == 'socket_close') {
          unawaited(h.socket.close());
        } else if (interruption == 'authority') {
          SharedPreferencesUtil().deferAiConsent();
        } else {
          h.emit({
            'type': 'error',
            'ready_protocol': 1,
            'provider': 'grok-voice',
            'session_id': 'ready-session',
            'terminal': true,
            'code': 'provider_session_configuration_rejected',
          });
        }
        if (!h.socket.isClosed) {
          h.emit({'type': 'transcript', 'text': 'must not reach UI'});
          h.socket.add(Uint8List.fromList([1, 2, 3, 4]));
        }
        await Future<void>.delayed(Duration.zero);
        micFinish.complete(true);
        final receipt = await connect;
        expect(receipt.connected, isFalse);
        if (interruption == 'error') expect(receipt.errorCode, 'provider_session_configuration_rejected');
        if (interruption == 'socket_close') expect(receipt.errorCode, 'provider_startup_client_closed');
        expect(h.states, isEmpty);
        expect(h.events.where((event) => event != 'connection_receipt'), isEmpty);
        expect(h.playbacks, 0);
        expect(h.closes, 1);
      });
    }

    for (final acknowledged in [false, true]) {
      for (final reason in ['quota_daily', 'ai_consent_required']) {
        test('Grok $reason retains existing policy stop before/after ACK=$acknowledged', () async {
          grantCurrentConsent();
          final h = _ReadinessHarness();
          final connect = h.client.connect(provider: 'grok-voice');
          await h.attached.future;
          if (acknowledged) {
            h.emit(h.ready());
            expect((await connect).connected, isTrue);
          }
          h.emit({
            'type': 'session_end',
            'detail': {'code': reason, 'resets_at': '2026-10-10T00:00:00Z'},
            'message': 'arbitrary raw text must not reach UI',
          });
          if (!acknowledged) expect((await connect).errorCode, reason);
          await h.client.disconnect();
          expect(h.client.lastConnectionReceipt!.errorCode, reason);
          if (reason == 'quota_daily') {
            final event = h.eventValues.singleWhere((event) => event.type == 'session_end');
            expect(event.policyReason, EllaVoicePolicyReason.quotaDaily);
            expect(event.resetsAt!.toUtc(), DateTime.utc(2026, 10, 10));
            expect(event.text, isNull);
          } else {
            expect(h.events, contains('consent_authority_lost'));
          }
          expect(h.closes, 1);
          expect(h.playbacks, 0);
          expect(h.states, acknowledged ? [true, false] : isEmpty);
        });
      }
    }

    for (final acknowledged in [false, true]) {
      test('late callback during delayed cancellation is dropped after ACK=$acknowledged', () async {
        grantCurrentConsent();
        final cancelEntered = Completer<void>();
        final cancelFinish = Completer<void>();
        late _LateDeliveryStream lateStream;
        final h = _ReadinessHarness(
          wrapStream: (source) => lateStream = _LateDeliveryStream(source),
          onCancel: () {
            cancelEntered.complete();
            return cancelFinish.future;
          },
        );
        final connect = h.client.connect(provider: 'grok-voice');
        await h.attached.future;
        if (acknowledged) {
          h.emit(h.ready());
          expect((await connect).connected, isTrue);
        }
        final disconnect = h.client.disconnect();
        await cancelEntered.future;
        final eventCount = h.events.length;
        lateStream.deliverLate(jsonEncode({'type': 'transcript', 'text': 'stale private text'}));
        lateStream.deliverLate(Uint8List.fromList([1, 2, 3, 4]));
        lateStream.deliverLate(jsonEncode(h.ready()));
        expect(h.events.length, eventCount);
        expect(h.playbacks, 0);
        expect(h.client.isConnected, isFalse);
        cancelFinish.complete();
        await disconnect;
        if (!acknowledged) expect((await connect).connected, isFalse);
        expect(h.micStarts, acknowledged ? 1 : 0);
        expect(h.closes, 1);
      });
    }

    test('post-ready terminal failure closes and retains typed safe receipt', () async {
      grantCurrentConsent();
      final h = _ReadinessHarness();
      h.emit(h.ready());
      expect((await h.client.connect(provider: 'grok-voice')).connected, isTrue);
      h.emit({
        'type': 'error',
        'ready_protocol': 1,
        'provider': 'grok-voice',
        'session_id': 'ready-session',
        'terminal': true,
        'code': 'provider_startup_failed'
      });
      await Future<void>.delayed(Duration.zero);
      await h.client.disconnect();
      expect(h.client.lastConnectionReceipt!.errorCode, 'provider_startup_failed');
      expect(h.states, [true, false]);
      expect(h.closes, 1);
    });

    test('remote socket close releases the owned microphone and interactive route', () async {
      grantCurrentConsent();
      final operations = <String>[];
      final recorder = _FakeV2VMicrophoneRecorder(operations);
      final socket = StreamController<dynamic>();
      final released = Completer<void>();
      final states = <bool>[];
      final client = V2VClient(
        onConnectionChanged: states.add,
        providerRegistryValidator: (_) async => null,
        sessionCreator: (_, provider, __) async => {
          'session_token': 'opaque-test-token',
          'voice_endpoint': 'wss://example.invalid/voice?ready_protocol=1',
          'ready_protocol': 1,
          'provider': provider,
          'voice_mode': 'grok-voice',
          'session_id': 'synthetic-close-session',
        },
        audioSessionConfigurator: () async {},
        audioSessionReleaser: () async {
          operations.add('route.release');
          if (!released.isCompleted) released.complete();
        },
        audibleOutputEnforcer: () async => true,
        microphoneRecorderFactory: () => recorder,
        webSocketConnector: (_) => V2VWebSocketTransport(
          ready: Future<void>.value(),
          stream: _acknowledgedStream(socket.stream, 'synthetic-close-session'),
          send: (_) {},
          close: socket.close,
        ),
      );
      addTearDown(client.disconnect);
      expect((await client.connect(provider: 'grok-voice')).connected, isTrue);
      expect(operations, contains('recorder.startStream'));
      await socket.close();
      await Future<void>.delayed(Duration.zero);
      expect(operations, contains('recorder.stop'), reason: 'A remote close must stop owned mic capture');
      expect(operations, contains('recorder.dispose'));
      expect(operations, contains('route.release'));
      expect(states, [true, false]);
      expect(client.hasActiveConsentLeaseForTesting, isFalse);
    });

    for (final reuseClient in [true, false]) {
      test('reconnect waits for remote-close cleanup (reuse client: $reuseClient)', () async {
        grantCurrentConsent();
        final firstSocket = StreamController<dynamic>();
        final nextSocket = StreamController<dynamic>();
        final releaseStarted = Completer<void>();
        final allowRelease = Completer<void>();
        var sessions = 0;
        var micStarts = 0;
        var releases = 0;
        V2VClient makeClient() => V2VClient(
              providerRegistryValidator: (_) async => null,
              sessionCreator: (_, provider, __) async {
                sessions++;
                return {
                  'session_token': 'opaque-test-token',
                  'voice_endpoint': 'wss://example.invalid/voice?ready_protocol=1',
                  'ready_protocol': 1,
                  'provider': provider,
                  'voice_mode': 'grok-voice',
                  'session_id': 'synthetic-session-$sessions'
                };
              },
              audioSessionConfigurator: () async {},
              audioSessionReleaser: () async {
                releases++;
                if (releases == 1) {
                  releaseStarted.complete();
                  await allowRelease.future;
                }
              },
              microphoneStarter: () async {
                micStarts++;
                return true;
              },
              webSocketConnector: (_) {
                final socket = sessions == 1 ? firstSocket : nextSocket;
                return V2VWebSocketTransport(
                    ready: Future<void>.value(),
                    stream: _acknowledgedStream(socket.stream, 'synthetic-session-$sessions'),
                    send: (_) {},
                    close: socket.close);
              },
            );
        final firstClient = makeClient();
        expect((await firstClient.connect(provider: 'grok-voice')).connected, isTrue);
        await firstSocket.close();
        await releaseStarted.future;
        final secondClient = reuseClient ? firstClient : makeClient();
        final reconnect = secondClient.connect(provider: 'grok-voice');
        await Future<void>.delayed(Duration.zero);
        expect(sessions, 1, reason: 'Old interactive route must be released before a new session starts');
        expect(micStarts, 1);
        allowRelease.complete();
        expect((await reconnect).connected, isTrue);
        expect(sessions, 2);
        expect(micStarts, 2);
        final cleanup = secondClient.disconnect();
        expect(identical(cleanup, secondClient.disconnect()), isTrue);
        await cleanup;
      });
    }

    test('rejected socket handshake never announces connected or starts the microphone', () async {
      grantCurrentConsent();
      var micStarts = 0;
      final states = <bool>[];
      final client = V2VClient(
        onConnectionChanged: states.add,
        providerRegistryValidator: (_) async => null,
        sessionCreator: (_, provider, __) async => {
          'session_token': 'opaque-test-token',
          'voice_endpoint': 'wss://example.invalid/voice?ready_protocol=1',
          'ready_protocol': 1,
          'provider': provider,
          'voice_mode': 'grok-voice',
          'session_id': 'synthetic-rejected-session',
        },
        audioSessionConfigurator: () async {},
        audioSessionReleaser: () async {},
        microphoneStarter: () async {
          micStarts++;
          return true;
        },
        webSocketConnector: (_) => V2VWebSocketTransport(
          ready: Future<void>.error(StateError('synthetic handshake rejection')),
          stream: const Stream<dynamic>.empty(),
          send: (_) {},
          close: () async {},
        ),
      );
      final receipt = await client.connect(provider: 'grok-voice');
      expect(receipt.connected, isFalse);
      expect(receipt.stage, V2VConnectionStage.websocket);
      expect(micStarts, 0);
      expect(states, isEmpty);
      expect(client.hasActiveConsentLeaseForTesting, isFalse);
      await client.disconnect();
    });

    test('canonical success with lost ACK survives restart and new-session proxy replay exactly once', () async {
      grantCurrentConsent();
      const terminalFrame = '{"type":"transcript_turn","contract_version":1,"terminal":true,'
          '"session_id":"session-jti-1","turn_id":"v2v-turn-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",'
          '"user_event_id":"v2v-turn-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee:user",'
          '"assistant_event_id":"v2v-turn-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee:assistant",'
          '"user_text":"Question","assistant_text":"Answer",'
          '"started_at":"2026-08-15T20:10:00Z","completed_at":"2026-08-15T20:10:02Z"}';
      final persistenceStarted = Completer<void>();
      final releasePersistence = Completer<void>();
      var appOwnsTurnDurably = false;
      final canonicalRows = <String, Map<String, dynamic>>{};
      Future<bool> persistCanonical(V2VTerminalTranscriptTurn turn) async {
        final sourceIdentity = 'ios_voice:uid-a:${turn.turnId}';
        final rows = [
          {
            'source_identity': sourceIdentity,
            'event_id': turn.userEventId,
            'role': 'user',
            'text': turn.userTranscript,
            'started_at': turn.startedAt.toUtc().toIso8601String(),
            'ended_at': turn.completedAt.toUtc().toIso8601String(),
          },
          {
            'source_identity': sourceIdentity,
            'event_id': turn.assistantEventId,
            'role': 'assistant',
            'text': turn.assistantTranscript,
            'started_at': turn.completedAt.toUtc().toIso8601String(),
            'ended_at': turn.completedAt.toUtc().toIso8601String(),
          },
        ];
        for (final row in rows) {
          final existing = canonicalRows[row['event_id']];
          if (existing != null && jsonEncode(existing) != jsonEncode(row)) return false;
        }
        for (final row in rows) {
          canonicalRows.putIfAbsent(row['event_id'] as String, () => row);
        }
        return true;
      }

      final firstSentFrames = <String>[];
      final firstSocket = StreamController<dynamic>(sync: true);
      final firstClient = V2VClient(
        onTerminalTurnDurable: (turn) async {
          appOwnsTurnDurably = await persistCanonical(turn);
          persistenceStarted.complete();
          await releasePersistence.future;
          return appOwnsTurnDurably;
        },
        providerRegistryValidator: (_) async => null,
        sessionCreator: (_, provider, __) async => {
          'session_token': 'opaque-test-token',
          'voice_endpoint': 'wss://example.invalid/voice?ready_protocol=1',
          'ready_protocol': 1,
          'provider': provider,
          'voice_mode': 'grok-voice',
          'session_id': 'session-jti-1',
        },
        audioSessionConfigurator: () async {},
        webSocketConnector: (_) => V2VWebSocketTransport(
          ready: Future<void>.value(),
          stream: _acknowledgedStream(firstSocket.stream, 'session-jti-1'),
          send: (value) => firstSentFrames.add(value as String),
          close: firstSocket.close,
        ),
        microphoneStarter: () async => true,
      );
      expect(
        (await firstClient.connect(provider: 'grok-voice', beforeTransportActivation: (_) async => true)).connected,
        isTrue,
      );

      firstSocket.add(terminalFrame);
      await persistenceStarted.future;
      await firstClient.disconnect();
      releasePersistence.complete();
      await firstClient.settleTerminalTurnsForTesting();

      expect(appOwnsTurnDurably, isTrue);
      expect(firstSentFrames, isEmpty);

      final replaySentFrames = <String>[];
      final replaySocket = StreamController<dynamic>(sync: true);
      final replayClient = V2VClient(
        onTerminalTurnDurable: persistCanonical,
        providerRegistryValidator: (_) async => null,
        sessionCreator: (_, provider, __) async => {
          'session_token': 'opaque-test-token',
          'voice_endpoint': 'wss://example.invalid/voice?ready_protocol=1',
          'ready_protocol': 1,
          'provider': provider,
          'voice_mode': 'grok-voice',
          'session_id': 'session-jti-2',
        },
        audioSessionConfigurator: () async {},
        webSocketConnector: (_) => V2VWebSocketTransport(
          ready: Future<void>.value(),
          stream: _acknowledgedStream(replaySocket.stream, 'session-jti-2'),
          send: (value) => replaySentFrames.add(value as String),
          close: replaySocket.close,
        ),
        microphoneStarter: () async => true,
      );
      expect(
        (await replayClient.connect(provider: 'grok-voice', beforeTransportActivation: (_) async => true)).connected,
        isTrue,
      );

      final reboundReplay = Map<String, dynamic>.from(jsonDecode(terminalFrame) as Map)
        ..['session_id'] = 'session-jti-2';
      replaySocket.add(jsonEncode(reboundReplay));
      await replayClient.settleTerminalTurnsForTesting();

      expect(jsonDecode(replaySentFrames.single), {
        'type': 'transcript_turn_ack',
        'contract_version': 1,
        'session_id': 'session-jti-2',
        'turn_id': 'v2v-turn-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
      });
      expect(canonicalRows, hasLength(2));
      expect(canonicalRows.values.map((row) => row['source_identity']).toSet(), {
        'ios_voice:uid-a:v2v-turn-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
      });

      for (final collision in [
        Map<String, dynamic>.from(reboundReplay)..['assistant_text'] = 'Conflicting answer',
        Map<String, dynamic>.from(reboundReplay)..['started_at'] = '2026-08-15T20:10:01Z',
      ]) {
        replaySocket.add(jsonEncode(collision));
        await replayClient.settleTerminalTurnsForTesting();
      }
      expect(replaySentFrames, hasLength(1), reason: 'payload/timestamp collisions must not be ACKed');
      expect(canonicalRows, hasLength(2));
      await replayClient.disconnect();
    });

    test('revocation during delayed startup produces zero protected egress and zero microphone frames', () async {
      grantCurrentConsent();
      final preferences = SharedPreferencesUtil();
      final boundaryReached = Completer<void>();
      final releaseBoundary = Completer<void>();
      final protectedEgress = <V2VProtectedEgressBoundary>[];
      final client = V2VClient(
        onEvent: (_) {},
        onConnectionChanged: (_) {},
        beforeProtectedEgress: (boundary) async {
          if (boundary != V2VProtectedEgressBoundary.providerRegistry) return;
          boundaryReached.complete();
          await releaseBoundary.future;
        },
        onProtectedEgress: protectedEgress.add,
      );

      final connectFuture = client.connect(provider: 'grok-voice');
      await boundaryReached.future;
      preferences.declineAiConsent();
      releaseBoundary.complete();
      final receipt = await connectFuture;

      expect(receipt.connected, isFalse);
      expect(receipt.stage, V2VConnectionStage.consent);
      expect(receipt.errorCode, 'consent_authority_lost');
      expect(protectedEgress, isEmpty);
      expect(client.micChunksSentForTesting, 0);
      expect(client.hasActiveConsentLeaseForTesting, isFalse);
      await client.disconnect();
    });

    test('account drift during delayed startup produces zero protected egress and zero microphone frames', () async {
      grantCurrentConsent();
      final preferences = SharedPreferencesUtil();
      final boundaryReached = Completer<void>();
      final releaseBoundary = Completer<void>();
      final protectedEgress = <V2VProtectedEgressBoundary>[];
      final client = V2VClient(
        onEvent: (_) {},
        onConnectionChanged: (_) {},
        beforeProtectedEgress: (boundary) async {
          if (boundary != V2VProtectedEgressBoundary.providerRegistry) return;
          boundaryReached.complete();
          await releaseBoundary.future;
        },
        onProtectedEgress: protectedEgress.add,
      );

      final connectFuture = client.connect(provider: 'grok-voice');
      await boundaryReached.future;
      preferences.uid = 'uid-b';
      releaseBoundary.complete();
      final receipt = await connectFuture;

      expect(receipt.connected, isFalse);
      expect(receipt.stage, V2VConnectionStage.consent);
      expect(receipt.errorCode, 'consent_authority_lost');
      expect(protectedEgress, isEmpty);
      expect(client.micChunksSentForTesting, 0);
      expect(client.hasActiveConsentLeaseForTesting, isFalse);
      await client.disconnect();
    });

    test('profile drift during delayed startup preserves the server-confirmed authority', () async {
      grantCurrentConsent();
      final preferences = SharedPreferencesUtil();
      final boundaryReached = Completer<void>();
      final releaseBoundary = Completer<void>();
      final protectedEgress = <V2VProtectedEgressBoundary>[];
      final client = V2VClient(
        onEvent: (_) {},
        onConnectionChanged: (_) {},
        beforeProtectedEgress: (boundary) async {
          if (boundary != V2VProtectedEgressBoundary.providerRegistry) return;
          boundaryReached.complete();
          await releaseBoundary.future;
        },
        onProtectedEgress: protectedEgress.add,
        providerRegistryValidator: (_) async => null,
        sessionCreator: (_, __, ___) async => <String, dynamic>{},
      );

      final connectFuture = client.connect(provider: 'grok-voice');
      await boundaryReached.future;
      preferences.verifiedPersonaId = 'persona-b';
      releaseBoundary.complete();
      final receipt = await connectFuture;

      expect(receipt.connected, isFalse);
      expect(receipt.stage, V2VConnectionStage.session);
      expect(receipt.errorCode, isNot('consent_authority_lost'));
      expect(protectedEgress, [
        V2VProtectedEgressBoundary.providerRegistry,
        V2VProtectedEgressBoundary.session,
      ]);
      expect(client.micChunksSentForTesting, 0);
      expect(client.hasActiveConsentLeaseForTesting, isFalse);
      await client.disconnect();
    });

    test('profile drift at the microphone boundary keeps the server-confirmed lease active', () async {
      grantCurrentConsent();
      final preferences = SharedPreferencesUtil();
      final authority = AiConsentAuthoritySnapshot.capture(preferences: preferences, expectedUid: 'uid-a');
      expect(authority, isNotNull);

      final boundaryReached = Completer<void>();
      final releaseBoundary = Completer<void>();
      final protectedEgress = <V2VProtectedEgressBoundary>[];
      var microphoneStarts = 0;
      final client = V2VClient(
        onEvent: (_) {},
        onConnectionChanged: (_) {},
        beforeProtectedEgress: (boundary) async {
          if (boundary != V2VProtectedEgressBoundary.microphoneCapture) return;
          boundaryReached.complete();
          await releaseBoundary.future;
        },
        onProtectedEgress: protectedEgress.add,
        microphoneStarter: () async {
          microphoneStarts++;
          return true;
        },
      );

      final startFuture = client.startAuthorizedMicrophoneForTesting(authority: authority!, shouldContinue: () => true);
      await boundaryReached.future;
      preferences.verifiedPersonaId = 'persona-b';
      releaseBoundary.complete();

      expect(await startFuture, isTrue);
      expect(microphoneStarts, 1);
      expect(protectedEgress, [V2VProtectedEgressBoundary.microphoneCapture]);
      expect(client.micChunksSentForTesting, 0);
      expect(client.hasActiveConsentLeaseForTesting, isTrue);
      await client.disconnect();
    });

    test('the exact current authority lease is active before microphone capture can start', () async {
      grantCurrentConsent();
      final preferences = SharedPreferencesUtil();
      final authority = AiConsentAuthoritySnapshot.capture(preferences: preferences, expectedUid: 'uid-a');
      expect(authority, isNotNull);

      late final V2VClient client;
      var microphoneStarts = 0;
      client = V2VClient(
        onEvent: (_) {},
        onConnectionChanged: (_) {},
        microphoneStarter: () async {
          expect(client.hasActiveConsentLeaseForTesting, isTrue);
          microphoneStarts++;
          return true;
        },
      );

      expect(
        await client.startAuthorizedMicrophoneForTesting(authority: authority!, shouldContinue: () => true),
        isTrue,
      );
      expect(microphoneStarts, 1);
      await client.disconnect();
    });

    test('real recorder adapter yields session ownership then re-verifies accepted speaker and external routes',
        () async {
      for (final acceptedRoute in ['speaker', 'bluetoothHFP', 'bluetoothA2DP', 'airPlay']) {
        grantCurrentConsent();
        final authority = AiConsentAuthoritySnapshot.capture(
          preferences: SharedPreferencesUtil(),
          expectedUid: 'uid-a',
        );
        expect(authority, isNotNull);

        final operations = <String>[];
        final recorder = _FakeV2VMicrophoneRecorder(operations);
        final client = V2VClient(
          onEvent: (_) {},
          onConnectionChanged: (_) {},
          microphoneRecorderFactory: () => recorder,
          audibleOutputEnforcer: () async {
            operations.add('ella.verifyInteractiveRoute:$acceptedRoute');
            return true;
          },
        );

        expect(
          await client.startAuthorizedMicrophoneForTesting(
            authority: authority!,
            shouldContinue: () => true,
          ),
          isTrue,
          reason: '$acceptedRoute must remain an accepted interactive output',
        );
        expect(operations.take(4), [
          'recorder.disableAudioSessionManagement',
          'recorder.permission',
          'recorder.startStream',
          'ella.verifyInteractiveRoute:$acceptedRoute',
        ]);
        expect(recorder.config?.iosConfig.categoryOptions, contains(IosAudioCategoryOption.defaultToSpeaker));
        expect(recorder.config?.iosConfig.categoryOptions, contains(IosAudioCategoryOption.allowBluetooth));
        expect(recorder.config?.iosConfig.categoryOptions, contains(IosAudioCategoryOption.allowBluetoothA2DP));
        expect(recorder.config?.iosConfig.categoryOptions, contains(IosAudioCategoryOption.allowAirPlay));
        await client.disconnect();
      }
    });

    test('actual V2V recorder keeps parent reservation while permission/start/stop are held', () async {
      grantCurrentConsent();
      final authority = AiConsentAuthoritySnapshot.capture(preferences: SharedPreferencesUtil(), expectedUid: 'uid-a')!;
      final recorder = _FakeV2VMicrophoneRecorder([])
        ..permissionGate = Completer<bool>()
        ..startGate = Completer<void>()
        ..stopGate = Completer<void>();
      final client = V2VClient(microphoneRecorderFactory: () => recorder, audibleOutputEnforcer: () async => true);
      final starting = client.startAuthorizedMicrophoneForTesting(authority: authority, shouldContinue: () => true);
      await Future<void>.delayed(Duration.zero);
      expect(AudioCaptureOwnership.shared.reserve(source: 'dictation', hasAuthority: () => true), isNull);
      recorder.permissionGate!.complete(true);
      await Future<void>.delayed(Duration.zero);
      final closing = client.disconnect();
      expect(AudioCaptureOwnership.shared.isReserved, isTrue);
      recorder.startGate!.complete();
      await Future<void>.delayed(Duration.zero);
      expect(AudioCaptureOwnership.shared.isReserved, isTrue);
      recorder.stopGate!.complete();
      await closing;
      expect(await starting, isFalse);
      expect(recorder.probes, greaterThan(0));
      expect(AudioCaptureOwnership.shared.isReserved, isFalse);
    });

    test('actual V2V failed stop or active probe retains exact recorder and owner until retry', () async {
      grantCurrentConsent();
      final authority = AiConsentAuthoritySnapshot.capture(preferences: SharedPreferencesUtil(), expectedUid: 'uid-a')!;
      final recorder = _FakeV2VMicrophoneRecorder([])..failStop = true;
      final client = V2VClient(microphoneRecorderFactory: () => recorder, audibleOutputEnforcer: () async => true);
      expect(
          await client.startAuthorizedMicrophoneForTesting(authority: authority, shouldContinue: () => true), isTrue);
      await expectLater(client.disconnect(), throwsStateError);
      expect(AudioCaptureOwnership.shared.reserve(source: 'phone', hasAuthority: () => true), isNull);
      expect(recorder.recording, isTrue);
      recorder.failStop = false;
      recorder.forcedRecordingProbe = true;
      await expectLater(client.disconnect(), throwsStateError);
      expect(AudioCaptureOwnership.shared.isReserved, isTrue);
      recorder.forcedRecordingProbe = false;
      await client.disconnect();
      expect(AudioCaptureOwnership.shared.isReserved, isFalse);
    });

    test('real recorder adapter fails closed after start when post-start route verification fails', () async {
      grantCurrentConsent();
      final authority = AiConsentAuthoritySnapshot.capture(
        preferences: SharedPreferencesUtil(),
        expectedUid: 'uid-a',
      );
      expect(authority, isNotNull);

      final operations = <String>[];
      final recorder = _FakeV2VMicrophoneRecorder(operations);
      final client = V2VClient(
        onEvent: (_) {},
        onConnectionChanged: (_) {},
        microphoneRecorderFactory: () => recorder,
        audibleOutputEnforcer: () async {
          operations.add('ella.verifyInteractiveRoute:receiver');
          return false;
        },
      );

      expect(
        await client.startAuthorizedMicrophoneForTesting(
          authority: authority!,
          shouldContinue: () => true,
        ),
        isFalse,
      );
      expect(operations, [
        'recorder.disableAudioSessionManagement',
        'recorder.permission',
        'recorder.startStream',
        'ella.verifyInteractiveRoute:receiver',
        'recorder.stop',
        'recorder.dispose',
      ]);
      expect(client.micChunksSentForTesting, 0);
      expect(client.hasActiveConsentLeaseForTesting, isFalse);
      await client.disconnect();
    });

    test('assistant transcript alone does not close the microphone gate', () async {
      grantCurrentConsent();
      final authority = AiConsentAuthoritySnapshot.capture(preferences: SharedPreferencesUtil(), expectedUid: 'uid-a');
      expect(authority, isNotNull);

      final events = <V2VEvent>[];
      var microphoneStarts = 0;
      final client = V2VClient(
        onEvent: events.add,
        onConnectionChanged: (_) {},
        microphoneStarter: () async {
          microphoneStarts++;
          return true;
        },
        liveChannelForTesting: () => true,
        playbackMicCooldown: Duration.zero,
      );

      expect(
        await client.startAuthorizedMicrophoneForTesting(authority: authority!, shouldContinue: () => true),
        isTrue,
      );
      client.markConnectedForTesting();
      client.handleMessageForTesting('{"type":"transcript","text":"Visible answer"}');

      expect(microphoneStarts, 1);
      expect(client.micMutedForTesting, isFalse);
      expect(client.micSuspendedForPlaybackForTesting, isFalse);
      expect(events.where((event) => event.type == 'transcript').single.text, 'Visible answer');
      await client.disconnect();
    });

    Future<(V2VClient, _FakeV2VOutputPlayer, Future<void>, List<int>)> startOutput({
      _FakeV2VOutputPlayer? output,
      Future<bool> Function()? release,
      Future<bool> Function()? route,
    }) async {
      grantCurrentConsent();
      final authority = AiConsentAuthoritySnapshot.capture(preferences: SharedPreferencesUtil(), expectedUid: 'uid-a')!;
      final player = output ?? _FakeV2VOutputPlayer();
      final starts = <int>[];
      final client = V2VClient(
        microphoneStarter: () async {
          starts.add(1);
          return true;
        },
        outputPlayerFactory: () => player,
        audibleOutputEnforcer: () async => true,
        playbackOutputEnforcer: route ?? () async => true,
        audioSessionReleaseReceipt: release ?? () async => true,
        liveChannelForTesting: () => true,
        playbackMicCooldown: Duration.zero,
      );
      expect(
          await client.startAuthorizedMicrophoneForTesting(authority: authority, shouldContinue: () => true), isTrue);
      client.markConnectedForTesting();
      client.bufferAudioChunkForTesting(Uint8List.fromList([1, 2]));
      final finish = client.finishPlaybackForTesting();
      return (client, player, finish, starts);
    }

    test('output teardown stock failed-dispose retry never treats a local no-op as silence', () async {
      final raw = _DisposedBeforeReceiptPlayer();
      final adapter = JustAudioV2VOutputPlayer(player: raw);
      await expectLater(adapter.stopAndConfirm(), throwsStateError);
      raw.fail = false;
      expect(await adapter.disposeAndConfirm(), isFalse);
      expect(await adapter.stopAndConfirm(), isFalse);
      expect(raw.disposeCalls, 1);
      final successfulRaw = _DisposedBeforeReceiptPlayer()..fail = false;
      final successfulAdapter = JustAudioV2VOutputPlayer(player: successfulRaw);
      expect(await successfulAdapter.stopAndConfirm(), isTrue);
      expect(await successfulAdapter.disposeAndConfirm(), isTrue);
      expect(successfulRaw.disposeCalls, 1);
    });

    test('output teardown raw quiesce waits initial session setup before any microphone or output', () async {
      grantCurrentConsent();
      final entered = Completer<void>(), released = Completer<void>();
      final h = _ReadinessHarness(configureSession: () async {
        entered.complete();
        await released.future;
      });
      h.emit(h.ready());
      final connection = h.client.connect(provider: 'grok-voice');
      await entered.future;
      final transition = AudioCaptureOwnership.shared.quiesceForAccountTransition();
      await Future<void>.delayed(Duration.zero);
      expect(AudioCaptureOwnership.shared.isReserved, isTrue);
      expect(h.micStarts, 0);
      expect(h.playbacks, 0);
      expect(h.releases, 0);
      released.complete();
      await transition;
      expect((await connection).connected, isFalse);
      expect(h.micStarts, 0);
      expect(h.playbacks, 0);
      expect(h.releases, 1);
      await h.client.disconnect();
      expect(AudioCaptureOwnership.shared.isReserved, isFalse);
      // Startup never attaches this synthetic socket; consume its buffered
      // ready frame before awaiting close so fixture cleanup cannot hang.
      final unusedSocket = h.socket.stream.listen((_) {});
      await h.socket.close();
      await unusedSocket.cancel();
    });

    test('output teardown held stop blocks replacement, new playback and mic restart', () async {
      final player = _FakeV2VOutputPlayer()..stopGate = Completer<bool>();
      final (client, _, finish, starts) = await startOutput(output: player);
      await player.entered.future;
      await Future<void>.delayed(Duration.zero);
      final interrupt = client.interruptPlayback();
      await Future<void>.delayed(Duration.zero);
      expect(AudioCaptureOwnership.shared.reserve(source: 'replacement', hasAuthority: () => true), isNull);
      client.bufferAudioChunkForTesting(Uint8List.fromList([3, 4]));
      await client.finishPlaybackForTesting();
      expect(player.plays, 1);
      expect(starts, hasLength(1));
      player.stopGate!.complete(true);
      await interrupt;
      await finish;
      expect(starts, hasLength(2));
      await client.disconnect();
      expect(AudioCaptureOwnership.shared.isReserved, isFalse);
    });

    for (final failure in ['stop', 'dispose', 'route']) {
      test('output teardown $failure failure holds exact parent until explicit retry', () async {
        var routeConfirmed = failure != 'route';
        final player = _FakeV2VOutputPlayer()
          ..failStop = failure == 'stop'
          ..failDispose = failure == 'dispose';
        final (client, _, finish, starts) = await startOutput(output: player, release: () async => routeConfirmed);
        await player.entered.future;
        await Future<void>.delayed(Duration.zero);
        await expectLater(client.disconnect(), throwsStateError);
        expect(AudioCaptureOwnership.shared.isReserved, isTrue);
        expect(AudioCaptureOwnership.shared.reserve(source: 'phone', hasAuthority: () => true), isNull);
        expect(starts, hasLength(1));
        client.bufferAudioChunkForTesting(Uint8List.fromList([5, 6]));
        await client.finishPlaybackForTesting();
        expect(player.plays, 1);
        player.failStop = false;
        player.failDispose = false;
        routeConfirmed = true;
        await client.disconnect();
        await finish;
        expect(AudioCaptureOwnership.shared.isReserved, isFalse);
        expect(starts, hasLength(1));
      });
    }

    test('output teardown raw account quiesce retains through held native disposal and account drift', () async {
      final player = _FakeV2VOutputPlayer()..disposeGate = Completer<bool>();
      final (client, _, finish, starts) = await startOutput(output: player);
      await player.entered.future;
      await Future<void>.delayed(Duration.zero);
      final transition = AudioCaptureOwnership.shared.quiesceForAccountTransition();
      await Future<void>.delayed(Duration.zero);
      expect(AudioCaptureOwnership.shared.isReserved, isTrue);
      SharedPreferencesUtil().uid = 'uid-b';
      client.bufferAudioChunkForTesting(Uint8List.fromList([7, 8]));
      expect(AudioCaptureOwnership.shared.reserve(source: 'account-b', hasAuthority: () => true), isNull);
      expect(starts, hasLength(1));
      player.disposeGate!.complete(true);
      await transition;
      await finish;
      await client.disconnect();
      expect(AudioCaptureOwnership.shared.isReserved, isFalse);
      expect(player.plays, 1);
    });

    test('output teardown raw quiesce false disposal remains retryable on same exact lifetime', () async {
      final player = _FakeV2VOutputPlayer()..disposeGate = Completer<bool>();
      final (client, _, finish, starts) = await startOutput(output: player);
      await player.entered.future;
      await Future<void>.delayed(Duration.zero);
      final transition = AudioCaptureOwnership.shared.quiesceForAccountTransition();
      player.disposeGate!.complete(false);
      await expectLater(transition, throwsStateError);
      expect(AudioCaptureOwnership.shared.isReserved, isTrue);
      expect(starts, hasLength(1));
      player.disposeGate = null;
      await AudioCaptureOwnership.shared.quiesceForAccountTransition();
      await finish;
      await client.disconnect();
      expect(AudioCaptureOwnership.shared.isReserved, isFalse);
      expect(player.disposals, greaterThanOrEqualTo(2));
    });

    test('output teardown held player load cannot play after raw quiesce acknowledgement', () async {
      final player = _FakeV2VOutputPlayer()..loadGate = Completer<void>();
      final (client, _, finish, starts) = await startOutput(output: player);
      await player.entered.future;
      final transition = AudioCaptureOwnership.shared.quiesceForAccountTransition();
      await Future<void>.delayed(Duration.zero);
      expect(player.stops, 0);
      expect(AudioCaptureOwnership.shared.isReserved, isTrue);
      player.loadGate!.complete();
      await transition;
      await finish;
      await client.disconnect();
      expect(player.plays, 0);
      expect(starts, hasLength(1));
      expect(player.disposals, 1);
    });

    test('output teardown held route setup is fenced before player allocation', () async {
      final entered = Completer<void>(), released = Completer<bool>();
      final (client, player, finish, starts) = await startOutput(route: () {
        entered.complete();
        return released.future;
      });
      await entered.future;
      final transition = AudioCaptureOwnership.shared.quiesceForAccountTransition();
      await Future<void>.delayed(Duration.zero);
      expect(AudioCaptureOwnership.shared.isReserved, isTrue);
      released.complete(true);
      await transition;
      await finish;
      await client.disconnect();
      expect(player.plays, 0);
      expect(starts, hasLength(1));
    });

    test('PCM playback waits for WAV completion then restores the interactive route and mic', () async {
      grantCurrentConsent();
      final authority = AiConsentAuthoritySnapshot.capture(preferences: SharedPreferencesUtil(), expectedUid: 'uid-a');
      expect(authority, isNotNull);

      final audioRoutes = <String>[];
      final playbackEntered = Completer<void>();
      final releasePlayback = Completer<void>();
      Uint8List? playedWav;
      var microphoneStarts = 0;
      final client = V2VClient(
        onEvent: (_) {},
        onConnectionChanged: (_) {},
        microphoneStarter: () async {
          microphoneStarts++;
          return true;
        },
        audibleOutputEnforcer: () async {
          audioRoutes.add('interactive');
          return true;
        },
        playbackOutputEnforcer: () async {
          audioRoutes.add('playback');
          return true;
        },
        wavPlayback: (wavBytes) async {
          playedWav = wavBytes;
          playbackEntered.complete();
          await releasePlayback.future;
        },
        liveChannelForTesting: () => true,
        playbackMicCooldown: Duration.zero,
      );

      expect(
        await client.startAuthorizedMicrophoneForTesting(authority: authority!, shouldContinue: () => true),
        isTrue,
      );
      client.markConnectedForTesting();
      client.bufferAudioChunkForTesting(Uint8List.fromList([1, 2, 3, 4]));
      final finish = client.finishPlaybackForTesting();
      await playbackEntered.future;

      expect(microphoneStarts, 1, reason: 'the mic must remain closed until audible playback completes');
      expect(client.micMutedForTesting, isTrue);
      expect(audioRoutes, ['playback']);
      expect(playedWav, isNotNull);
      expect(playedWav!.sublist(0, 4), [0x52, 0x49, 0x46, 0x46]);
      expect(playedWav!.sublist(8, 12), [0x57, 0x41, 0x56, 0x45]);
      expect(ByteData.sublistView(playedWav!).getUint16(22, Endian.little), 1);
      expect(ByteData.sublistView(playedWav!).getUint32(24, Endian.little), 24000);
      expect(ByteData.sublistView(playedWav!).getUint16(34, Endian.little), 16);
      expect(ByteData.sublistView(playedWav!).getUint32(40, Endian.little), 4);
      expect(playedWav!.sublist(44), [1, 2, 3, 4]);

      releasePlayback.complete();
      await finish;

      expect(audioRoutes, ['playback', 'interactive']);
      expect(microphoneStarts, 2);
      expect(client.micMutedForTesting, isFalse);
      expect(client.micSuspendedForPlaybackForTesting, isFalse);
      await client.disconnect();
    });

    test('ambiguous actual recorder playback pause prevents playback and mic restart until owned teardown', () async {
      grantCurrentConsent();
      final authority = AiConsentAuthoritySnapshot.capture(preferences: SharedPreferencesUtil(), expectedUid: 'uid-a')!;
      final operations = <String>[];
      final recorder = _FakeV2VMicrophoneRecorder(operations)..failStop = true;
      var playbacks = 0;
      final client = V2VClient(
        microphoneRecorderFactory: () => recorder,
        audibleOutputEnforcer: () async => true,
        playbackOutputEnforcer: () async => true,
        wavPlayback: (_) async {
          playbacks++;
        },
        liveChannelForTesting: () => true,
        playbackMicCooldown: Duration.zero,
      );
      expect(
          await client.startAuthorizedMicrophoneForTesting(authority: authority, shouldContinue: () => true), isTrue);
      client.markConnectedForTesting();
      client.bufferAudioChunkForTesting(Uint8List.fromList([1, 2, 3, 4]));
      await client.finishPlaybackForTesting();
      await client.interruptPlayback();
      expect(playbacks, 0);
      expect(operations.where((operation) => operation == 'recorder.startStream'), hasLength(1));
      expect(client.micMutedForTesting, isTrue);
      expect(AudioCaptureOwnership.shared.reserve(source: 'dictation', hasAuthority: () => true), isNull);
      recorder.failStop = false;
      await client.disconnect();
      expect(AudioCaptureOwnership.shared.isReserved, isFalse);
    });

    test('WAV playback failure reports the error and reopens the authorized mic', () async {
      grantCurrentConsent();
      final authority = AiConsentAuthoritySnapshot.capture(preferences: SharedPreferencesUtil(), expectedUid: 'uid-a');
      expect(authority, isNotNull);

      final events = <V2VEvent>[];
      final audioRoutes = <String>[];
      var microphoneStarts = 0;
      final client = V2VClient(
        onEvent: events.add,
        onConnectionChanged: (_) {},
        microphoneStarter: () async {
          microphoneStarts++;
          return true;
        },
        audibleOutputEnforcer: () async {
          audioRoutes.add('interactive');
          return true;
        },
        playbackOutputEnforcer: () async {
          audioRoutes.add('playback');
          return true;
        },
        wavPlayback: (_) async => throw StateError('player unavailable'),
        liveChannelForTesting: () => true,
        playbackMicCooldown: Duration.zero,
      );

      expect(
        await client.startAuthorizedMicrophoneForTesting(authority: authority!, shouldContinue: () => true),
        isTrue,
      );
      client.markConnectedForTesting();
      client.bufferAudioChunkForTesting(Uint8List.fromList([1, 2]));
      await client.finishPlaybackForTesting();

      expect(audioRoutes, ['playback', 'interactive']);
      expect(microphoneStarts, 2);
      expect(client.micMutedForTesting, isFalse);
      expect(client.micSuspendedForPlaybackForTesting, isFalse);
      expect(events.where((event) => event.type == 'error'), hasLength(1));
      expect(events.where((event) => event.type == 'playback_complete'), hasLength(1));
      await client.disconnect();
    });

    test('disconnect invalidates held WAV playback without reopening the mic', () async {
      grantCurrentConsent();
      final authority = AiConsentAuthoritySnapshot.capture(preferences: SharedPreferencesUtil(), expectedUid: 'uid-a');
      expect(authority, isNotNull);

      final playbackEntered = Completer<void>();
      final releasePlayback = Completer<void>();
      var playbackStarts = 0;
      var microphoneStarts = 0;
      final client = V2VClient(
        onEvent: (_) {},
        onConnectionChanged: (_) {},
        microphoneStarter: () async {
          microphoneStarts++;
          return true;
        },
        playbackOutputEnforcer: () async => true,
        wavPlayback: (_) async {
          playbackStarts++;
          playbackEntered.complete();
          await releasePlayback.future;
        },
        injectedPlaybackStopReceipt: () async {
          if (!releasePlayback.isCompleted) releasePlayback.complete();
          return true;
        },
        liveChannelForTesting: () => true,
        playbackMicCooldown: Duration.zero,
      );

      expect(
        await client.startAuthorizedMicrophoneForTesting(authority: authority!, shouldContinue: () => true),
        isTrue,
      );
      client.markConnectedForTesting();
      client.bufferAudioChunkForTesting(Uint8List.fromList([1, 2]));
      final finish = client.finishPlaybackForTesting();
      await playbackEntered.future;

      await client.disconnect();
      if (!releasePlayback.isCompleted) releasePlayback.complete();
      await finish;

      expect(playbackStarts, 1);
      expect(microphoneStarts, 1);
      expect(client.isConnected, isFalse);
      expect(client.micMutedForTesting, isFalse);
      expect(client.micSuspendedForPlaybackForTesting, isFalse);
    });
  });

  group('V2VClient proxy event mapping', () {
    test('parses exact proxy ordering with audio done before two identical terminal turns', () {
      final events = <V2VEvent>[];
      final client = V2VClient(onEvent: events.add);

      for (final ordinal in [1, 2]) {
        final suffix = ordinal.toRadixString(16).padLeft(32, '0');
        client.handleMessageForTesting('{"type":"user_transcript","text":"yes"}');
        client.handleMessageForTesting('{"type":"transcript","text":"Answer $ordinal"}');
        client.handleMessageForTesting('{"type":"audio_done"}');
        client.handleMessageForTesting(
          '{"type":"transcript_turn","contract_version":1,"terminal":true,'
          '"session_id":"session-jti-1","turn_id":"v2v-turn-$suffix",'
          '"user_event_id":"v2v-turn-$suffix:user",'
          '"assistant_event_id":"v2v-turn-$suffix:assistant",'
          '"user_text":"yes","assistant_text":"Answer $ordinal",'
          '"started_at":"2026-08-15T20:00:00Z","completed_at":"2026-08-15T20:00:02Z"}',
        );
      }

      expect(events.map((event) => event.type).where((type) => type != 'v2v_debug'), [
        'user_transcript',
        'transcript',
        'audio_done',
        'transcript_turn',
        'user_transcript',
        'transcript',
        'audio_done',
        'transcript_turn',
      ]);
      final terminalTurns = events.map((event) => event.terminalTurn).whereType<V2VTerminalTranscriptTurn>().toList();
      expect(terminalTurns.map((turn) => turn.userTranscript), ['yes', 'yes']);
      expect(terminalTurns.map((turn) => turn.turnId).toSet(), hasLength(2));
    });

    test('accepts only custom proxy transcript events, not raw provider frames', () {
      expect(V2VClient.treatsAsAssistantTranscriptEvent('transcript'), isTrue);
      expect(V2VClient.treatsAsAssistantTranscriptEvent('transcript_delta'), isFalse);
      expect(V2VClient.treatsAsAssistantTranscriptEvent('response.output_audio_transcript.done'), isFalse);
      expect(V2VClient.treatsAsAssistantTranscriptEvent('user_transcript'), isFalse);
    });

    test('accepts only the proxy audio completion frame', () {
      expect(V2VClient.treatsAsAudioDoneEvent('audio_done'), isTrue);
      expect(V2VClient.treatsAsAudioDoneEvent('response.audio.done'), isFalse);
      expect(V2VClient.treatsAsAudioDoneEvent('response.done'), isFalse);
    });

    test('drops malformed terminal turns instead of inferring a partial boundary', () {
      final events = <V2VEvent>[];
      final client = V2VClient(onEvent: events.add);

      client.handleMessageForTesting(
        '{"type":"transcript_turn","contract_version":1,"terminal":true,'
        '"session_id":"session-jti-1","turn_id":"v2v-turn-invalid",'
        '"user_text":"Question","assistant_text":"Partial"}',
      );
      client.handleMessageForTesting(
        '{"type":"response.output_audio_transcript.done","transcript":"Partial","response_id":"raw-provider"}',
      );

      expect(events.where((event) => event.type == 'transcript_turn'), isEmpty);
    });

    test('parses identifier-only memory reinterpretation events', () {
      final event = MemoryReinterpretationEvent.tryParse({
        'type': 'memory_reinterpretation',
        'state': 'submitted',
        'session_id': 'session-1',
        'conversation_id': 'conversation-42',
        'correction_id': 'correction-7',
        'trace_id': 'trace-9',
        'status': 'queued',
        'poll_after_ms': 750,
        'overview': 'must not be retained',
      });

      expect(event, isNotNull);
      expect(event!.sessionId, 'session-1');
      expect(event.conversationId, 'conversation-42');
      expect(event.correctionId, 'correction-7');
      expect(event.pollAfter, const Duration(milliseconds: 750));
    });
  });
}
