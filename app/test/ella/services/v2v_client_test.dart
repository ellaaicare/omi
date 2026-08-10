import 'dart:async';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/pages/ella_voice_chat_page.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/ella_entitlement_service.dart';
import 'package:omi/ella/services/v2v_client.dart';

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

    test('legacy consent receipt blocks before V2V provider or session requests', () async {
      SharedPreferences.setMockInitialValues({
        'aiConsentAccepted': true,
        'aiConsentContractVersion': 'voice-ai-processors-v2',
        'aiConsentReceiptId': 'ios-private-cloud-sync:voice-ai-processors-v2:legacy-receipt',
        'aiConsentReceiptUid': 'uid-a',
        'uid': 'uid-a',
      });
      await SharedPreferencesUtil.init();
      final client = V2VClient(onEvent: (_) {}, onConnectionChanged: (_) {});

      final receipt = await client.connect(provider: 'grok-voice');

      expect(receipt.connected, isFalse);
      expect(receipt.stage, V2VConnectionStage.consent);
      expect(receipt.errorCode, 'ai_consent_required');
      await client.disconnect();
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

    test('profile drift during delayed startup produces zero protected egress and zero microphone frames', () async {
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
      preferences.verifiedPersonaId = 'persona-b';
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

    test('profile drift at the microphone boundary starts no recorder and sends no protected data', () async {
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

      expect(await startFuture, isFalse);
      expect(microphoneStarts, 0);
      expect(protectedEgress, isEmpty);
      expect(client.micChunksSentForTesting, 0);
      expect(client.hasActiveConsentLeaseForTesting, isFalse);
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

    test('stream playback startup failure reopens the authorized microphone gate', () async {
      grantCurrentConsent();
      final authority = AiConsentAuthoritySnapshot.capture(
        preferences: SharedPreferencesUtil(),
        expectedUid: 'uid-a',
      );
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
        streamPlaybackStarter: () async => throw StateError('route unavailable'),
        liveChannelForTesting: () => true,
        playbackMicCooldown: Duration.zero,
      );

      expect(
        await client.startAuthorizedMicrophoneForTesting(
          authority: authority!,
          shouldContinue: () => true,
        ),
        isTrue,
      );
      client.markConnectedForTesting();
      client.streamAudioChunkForTesting(Uint8List.fromList([1, 2, 3]));
      await client.waitForStreamFeedForTesting();

      expect(microphoneStarts, 2);
      expect(client.micMutedForTesting, isFalse);
      expect(client.micSuspendedForPlaybackForTesting, isFalse);
      expect(events.where((event) => event.type == 'error'), hasLength(1));
      expect(events.where((event) => event.type == 'playback_complete'), hasLength(1));
      await client.disconnect();
    });

    test('failed stream generation drops queued PCM and re-gates the next generation', () async {
      grantCurrentConsent();
      final authority = AiConsentAuthoritySnapshot.capture(
        preferences: SharedPreferencesUtil(),
        expectedUid: 'uid-a',
      );
      expect(authority, isNotNull);

      final firstStartEntered = Completer<void>();
      final releaseFirstStart = Completer<void>();
      final nextStartEntered = Completer<void>();
      final releaseNextStart = Completer<void>();
      var playbackStarts = 0;
      var microphoneStarts = 0;
      final client = V2VClient(
        onEvent: (_) {},
        onConnectionChanged: (_) {},
        microphoneStarter: () async {
          microphoneStarts++;
          return true;
        },
        streamPlaybackStarter: () async {
          playbackStarts++;
          if (playbackStarts == 1) {
            firstStartEntered.complete();
            await releaseFirstStart.future;
            throw StateError('route unavailable');
          }
          nextStartEntered.complete();
          await releaseNextStart.future;
        },
        liveChannelForTesting: () => true,
        playbackMicCooldown: Duration.zero,
      );

      expect(
        await client.startAuthorizedMicrophoneForTesting(
          authority: authority!,
          shouldContinue: () => true,
        ),
        isTrue,
      );
      client.markConnectedForTesting();
      client.streamAudioChunkForTesting(Uint8List.fromList([1]));
      await firstStartEntered.future;
      client.streamAudioChunkForTesting(Uint8List.fromList([2]));
      final failedGeneration = client.waitForStreamFeedForTesting();

      expect(client.micMutedForTesting, isTrue);
      expect(client.micSuspendedForPlaybackForTesting, isTrue);
      releaseFirstStart.complete();
      await failedGeneration;

      expect(playbackStarts, 1, reason: 'the second stale chunk must be dropped');
      expect(microphoneStarts, 2, reason: 'failure recovery should reopen the authorized microphone');
      expect(client.micMutedForTesting, isFalse);
      expect(client.micSuspendedForPlaybackForTesting, isFalse);

      client.streamAudioChunkForTesting(Uint8List.fromList([3]));
      await nextStartEntered.future;
      expect(playbackStarts, 2);
      expect(client.micMutedForTesting, isTrue);
      expect(client.micSuspendedForPlaybackForTesting, isTrue);
      releaseNextStart.complete();
      await client.waitForStreamFeedForTesting();
      await client.disconnect();
    });

    test('disconnect invalidates held and queued stream feeds', () async {
      grantCurrentConsent();
      final authority = AiConsentAuthoritySnapshot.capture(
        preferences: SharedPreferencesUtil(),
        expectedUid: 'uid-a',
      );
      expect(authority, isNotNull);

      final firstStartEntered = Completer<void>();
      final releaseFirstStart = Completer<void>();
      var playbackStarts = 0;
      final client = V2VClient(
        onEvent: (_) {},
        onConnectionChanged: (_) {},
        microphoneStarter: () async => true,
        streamPlaybackStarter: () async {
          playbackStarts++;
          if (playbackStarts > 1) fail('stale queued PCM restarted playback after disconnect');
          firstStartEntered.complete();
          await releaseFirstStart.future;
          throw StateError('late route failure');
        },
        liveChannelForTesting: () => true,
        playbackMicCooldown: Duration.zero,
      );

      expect(
        await client.startAuthorizedMicrophoneForTesting(
          authority: authority!,
          shouldContinue: () => true,
        ),
        isTrue,
      );
      client.markConnectedForTesting();
      client.streamAudioChunkForTesting(Uint8List.fromList([1]));
      await firstStartEntered.future;
      client.streamAudioChunkForTesting(Uint8List.fromList([2]));
      final queuedFeeds = client.waitForStreamFeedForTesting();

      await client.disconnect();
      releaseFirstStart.complete();
      await queuedFeeds;
      client.streamAudioChunkForTesting(Uint8List.fromList([3]));
      await Future<void>.delayed(Duration.zero);

      expect(playbackStarts, 1);
      expect(client.isConnected, isFalse);
      expect(client.micMutedForTesting, isFalse);
      expect(client.micSuspendedForPlaybackForTesting, isFalse);
    });
  });

  group('V2VClient proxy event mapping', () {
    test('maps transcript deltas as assistant transcripts', () {
      expect(V2VClient.treatsAsAssistantTranscriptEvent('transcript'), isTrue);
      expect(V2VClient.treatsAsAssistantTranscriptEvent('transcript_delta'), isTrue);
      expect(V2VClient.treatsAsAssistantTranscriptEvent('response.audio_transcript.delta'), isTrue);
      expect(V2VClient.treatsAsAssistantTranscriptEvent('user_transcript'), isFalse);
    });

    test('separates audio completion from generic response completion', () {
      expect(V2VClient.treatsAsAudioDoneEvent('response.audio.done'), isTrue);
      expect(V2VClient.treatsAsAudioDoneEvent('response.done'), isFalse);
      expect(V2VClient.treatsAsResponseCompleteEvent('response.done'), isTrue);
      expect(V2VClient.treatsAsResponseCompleteEvent('audio_done'), isFalse);
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
