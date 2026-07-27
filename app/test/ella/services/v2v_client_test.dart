import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/pages/ella_voice_chat_page.dart';
import 'package:omi/ella/services/v2v_client.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  group('V2VClient provider contract', () {
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

    test('accepted server-verified v6 consent passes the mic gate before identity validation', () async {
      final preferences = SharedPreferencesUtil();
      preferences.uid = 'uid-a';
      preferences.acceptAiConsent(
        receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
        uid: 'uid-a',
        profileBindingId: 'profile-binding-a',
        serverDecidedAt: '2026-07-27T00:00:00Z',
      );
      preferences.markAiConsentServerVerified(
        uid: 'uid-a',
        receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
        policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
        processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
        profileBindingId: 'profile-binding-a',
        scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
        scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
      );
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
