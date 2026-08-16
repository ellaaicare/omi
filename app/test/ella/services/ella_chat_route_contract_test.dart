import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/api/messages.dart';
import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/http/http_pool_manager.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/services/ella_chat_service.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/platform/platform_manager.dart';

class _TestEnv implements EnvFields {
  @override
  String? get apiBaseUrl => 'https://api.ella.test/';
  @override
  String? get googleClientId => null;
  @override
  String? get googleClientSecret => null;
  @override
  String? get googleMapsApiKey => null;
  @override
  String? get growthbookApiKey => null;
  @override
  String? get intercomAndroidApiKey => null;
  @override
  String? get intercomAppId => null;
  @override
  String? get intercomIOSApiKey => null;
  @override
  String? get mixpanelProjectToken => null;
  @override
  String? get openAIAPIKey => null;
  @override
  bool? get useAuthCustomToken => false;
  @override
  bool? get useWebAuth => false;
}

class _CurrentAuthority implements ExactAccountAuthorityVerifier {
  const _CurrentAuthority(this.uid);

  @override
  final String uid;

  @override
  bool isExactCurrent() => true;
}

class _InspectingClient extends http.BaseClient {
  _InspectingClient(this.handler);

  final Future<http.StreamedResponse> Function(http.BaseRequest request) handler;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) => handler(request);
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  Env.init(_TestEnv());
  PlatformManager.initializeForTesting();

  setUp(() async {
    SharedPreferences.setMockInitialValues({'uid': 'uid-a'});
    await SharedPreferencesUtil.init();
  });

  test('Ella stream sends no caller-selected UID or legacy session authority', () async {
    final preferences = SharedPreferencesUtil();
    preferences.authToken = 'test-bearer';
    preferences.tokenExpirationTime = DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch;
    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      uid: 'uid-a',
      profileBindingId: 'profile-a',
      serverDecidedAt: '2026-08-04T00:00:00Z',
    );
    preferences.markAiConsentServerVerified(
      uid: 'uid-a',
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      profileBindingId: 'profile-a',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );

    HttpPoolManager.instance.replaceClientForTesting(
      _InspectingClient((request) async {
        final body = jsonDecode(await request.finalize().bytesToString()) as Map<String, dynamic>;
        expect(body, isNot(contains('uid')));
        expect(request.headers, isNot(contains('X-Ella-Session-Key')));
        expect(request.headers, isNot(contains('X-TTS-Provider')));
        expect(request.headers['authorization'], 'Bearer test-bearer');
        return http.StreamedResponse(Stream.value(utf8.encode('{"detail":"provider_unavailable"}')), 503);
      }),
    );

    await expectLater(
      sendEllaMessageStream('hello').toList(),
      throwsA(isA<ClientApiFailure>().having((failure) => failure.kind, 'kind', ClientApiFailureKind.unavailable)),
    );
  });

  test('history uses the first-party owner-bound route and preserves failed state', () async {
    const authority = _CurrentAuthority('uid-a');
    final result = await fetchEllaChatHistory(
      expectedAuthenticatedUid: 'uid-a',
      exactAuthority: authority,
      transport: ({required url, required expectedAuthenticatedUid, required exactAuthority}) async {
        final uri = Uri.parse(url);
        expect(uri.path, '/v1/ella/chat/history');
        expect(uri.queryParameters, {'limit': '50'});
        expect(expectedAuthenticatedUid, 'uid-a');
        expect(exactAuthority, same(authority));
        return http.Response('{"detail":"upgrade_required"}', 426);
      },
    );

    expect(result.isFailure, isTrue);
    expect(result.value, isNull);
    expect(result.failure?.kind, ClientApiFailureKind.updateRequired);
  });

  test('history preserves canonical sender roles after hydration', () async {
    const authority = _CurrentAuthority('uid-a');
    final result = await fetchEllaChatHistory(
      expectedAuthenticatedUid: 'uid-a',
      exactAuthority: authority,
      transport: ({required url, required expectedAuthenticatedUid, required exactAuthority}) async {
        return http.Response(
          jsonEncode({
            'messages': [
              {
                'id': 'canonical-0',
                'sender': 'human',
                'text': 'Persisted question',
                'created_at': '2026-08-09T02:59:00Z',
              },
              {'id': 'canonical-1', 'sender': 'ai', 'text': 'Persisted answer', 'created_at': '2026-08-09T03:00:00Z'},
            ],
          }),
          200,
        );
      },
    );

    expect(result.isSuccess, isTrue);
    expect(result.value, hasLength(2));
    expect(result.value![0].id, 'canonical-0');
    expect(result.value![0].sender, MessageSender.human);
    expect(result.value![0].text, 'Persisted question');
    expect(result.value![1].id, 'canonical-1');
    expect(result.value![1].sender, MessageSender.ai);
    expect(result.value![1].text, 'Persisted answer');
    expect(result.value![1].createdAt.toUtc(), DateTime.parse('2026-08-09T03:00:00Z'));
  });

  test('history preserves equal-time turn pairs instead of grouping by role', () async {
    const authority = _CurrentAuthority('uid-a');
    final result = await fetchEllaChatHistory(
      expectedAuthenticatedUid: 'uid-a',
      exactAuthority: authority,
      transport: ({required url, required expectedAuthenticatedUid, required exactAuthority}) async {
        Map<String, dynamic> message(String turnId, String sender, int sequence) => {
              'id': '$turnId:${sender == 'human' ? 'user' : 'assistant'}',
              'sender': sender,
              'text': '$sender $turnId',
              'created_at': '2026-08-15T20:00:00Z',
              'metadata': {
                'conversation_id': 'session-1',
                'turn_id': turnId,
                'event_sequence': sequence,
              },
            };
        return http.Response(
          jsonEncode({
            'messages': [
              message('turn-000002', 'ai', 1),
              message('turn-000001', 'ai', 1),
              message('turn-000002', 'human', 0),
              message('turn-000001', 'human', 0),
            ],
          }),
          200,
        );
      },
    );

    expect(result.value?.map((message) => message.id), [
      'turn-000001:user',
      'turn-000001:assistant',
      'turn-000002:user',
      'turn-000002:assistant',
    ]);
  });

  test('history rejects a nonempty unsupported message shape so cache can be preserved', () async {
    const authority = _CurrentAuthority('uid-a');
    final result = await fetchEllaChatHistory(
      expectedAuthenticatedUid: 'uid-a',
      exactAuthority: authority,
      transport: ({required url, required expectedAuthenticatedUid, required exactAuthority}) async {
        return http.Response(
          jsonEncode({
            'messages': [
              {'id': 'unknown-1', 'role': 'assistant', 'body': 'Not a supported contract'},
            ],
          }),
          200,
        );
      },
    );

    expect(result.isFailure, isTrue);
    expect(result.failure?.kind, ClientApiFailureKind.invalidResponse);
  });

  test('V2V turn uses authenticated first-party canonical writeback and returns canonical messages', () async {
    const authority = _CurrentAuthority('uid-a');
    final result = await persistEllaV2VTurn(
      uid: 'uid-a',
      sessionId: 'session-1',
      turnId: 'turn-000001',
      userEventId: 'turn-000001:user',
      assistantEventId: 'turn-000001:assistant',
      userTranscript: 'Question',
      assistantTranscript: 'Answer',
      startedAt: DateTime.utc(2026, 8, 15, 20),
      completedAt: DateTime.utc(2026, 8, 15, 20, 0, 2),
      turnOrdinal: 0,
      exactAuthority: authority,
      transport: ({required url, required body, required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(Uri.parse(url).path, '/v1/ella/chat/voice-turns');
        expect(expectedAuthenticatedUid, 'uid-a');
        expect(exactAuthority, same(authority));
        final request = jsonDecode(body) as Map<String, dynamic>;
        expect(request['uid'], 'uid-a');
        expect(request['session_id'], 'session-1');
        expect(request['turn_id'], 'turn-000001');
        expect(request['user_event_id'], 'turn-000001:user');
        expect(request['assistant_event_id'], 'turn-000001:assistant');
        expect(request['turn_ordinal'], 0);
        expect(request['user_terminal'], isTrue);
        expect(request['assistant_terminal'], isTrue);
        return http.Response(
          jsonEncode({
            'ok': true,
            'session_id': 'session-1',
            'turn_id': 'turn-000001',
            'messages': [
              {'id': 'turn-000001:user', 'sender': 'human', 'text': 'Question', 'created_at': '2026-08-15T20:00:00Z'},
              {
                'id': 'turn-000001:assistant',
                'sender': 'ai',
                'text': 'Answer',
                'created_at': '2026-08-15T20:00:02Z',
              },
            ],
          }),
          200,
        );
      },
    );

    expect(result.isSuccess, isTrue);
    expect(result.value?.map((message) => message.id), ['turn-000001:user', 'turn-000001:assistant']);
    expect(result.value?.every((message) => message.fromVoice), isTrue);
  });

  test('equal-timestamp V2V hydration deterministically restores user before assistant by event identity', () async {
    const authority = _CurrentAuthority('uid-a');
    final timestamp = DateTime.utc(2026, 8, 15, 20);
    final result = await persistEllaV2VTurn(
      uid: 'uid-a',
      sessionId: 'session-1',
      turnId: 'turn-000001',
      userEventId: 'turn-000001:user',
      assistantEventId: 'turn-000001:assistant',
      userTranscript: 'Question',
      assistantTranscript: 'Answer',
      startedAt: timestamp,
      completedAt: timestamp,
      turnOrdinal: 0,
      exactAuthority: authority,
      transport: ({required url, required body, required expectedAuthenticatedUid, required exactAuthority}) async {
        return http.Response(
          jsonEncode({
            'ok': true,
            'session_id': 'session-1',
            'turn_id': 'turn-000001',
            'messages': [
              {
                'id': 'turn-000001:assistant',
                'sender': 'ai',
                'text': 'Answer',
                'created_at': timestamp.toIso8601String(),
              },
              {
                'id': 'turn-000001:user',
                'sender': 'human',
                'text': 'Question',
                'created_at': timestamp.toIso8601String(),
              },
            ],
          }),
          200,
        );
      },
    );

    expect(result.isSuccess, isTrue);
    expect(result.value?.map((message) => message.id), ['turn-000001:user', 'turn-000001:assistant']);
    expect(result.value?.map((message) => message.sender), [MessageSender.human, MessageSender.ai]);
    expect(result.value?.map((message) => message.createdAt.toUtc()), [timestamp, timestamp]);
  });

  test('history and cache preserve equal-time reverse-lexical V2V turn chronology', () async {
    const authority = _CurrentAuthority('uid-a');
    const firstTurn = 'v2v-turn-ffffffffffffffffffffffffffffffff';
    const secondTurn = 'v2v-turn-00000000000000000000000000000000';
    Map<String, dynamic> message(String turnId, String sender, int eventSequence, int turnOrdinal) => {
          'id': '$turnId:${sender == 'human' ? 'user' : 'assistant'}',
          'sender': sender,
          'text': '$sender $turnId',
          'created_at': '2026-08-15T20:00:00Z',
          'metadata': {
            'conversation_id': 'session-1',
            'turn_id': turnId,
            'turn_ordinal': turnOrdinal,
            'event_sequence': eventSequence,
          },
        };
    final result = await fetchEllaChatHistory(
      expectedAuthenticatedUid: 'uid-a',
      exactAuthority: authority,
      transport: ({required url, required expectedAuthenticatedUid, required exactAuthority}) async => http.Response(
        jsonEncode({
          'messages': [
            message(secondTurn, 'ai', 1, 1),
            message(firstTurn, 'ai', 1, 0),
            message(secondTurn, 'human', 0, 1),
            message(firstTurn, 'human', 0, 0),
          ],
        }),
        200,
      ),
    );
    final cacheRoundTrip = result.value!.map((message) => ServerMessage.fromJson(message.toJson())).toList()
      ..sort(compareServerMessagesChronologically);

    expect(cacheRoundTrip.map((message) => message.id), [
      '$firstTurn:user',
      '$firstTurn:assistant',
      '$secondTurn:user',
      '$secondTurn:assistant',
    ]);
    expect(cacheRoundTrip.map((message) => message.canonicalTurnOrdinal), [0, 0, 1, 1]);
  });

  test('V2V backend error body is neither accepted nor surfaced as content', () async {
    const authority = _CurrentAuthority('uid-a');
    const privateErrorBody = '{"detail":"private transcript must never be logged"}';
    final result = await persistEllaV2VTurn(
      uid: 'uid-a',
      sessionId: 'session-1',
      turnId: 'turn-000001',
      userEventId: 'turn-000001:user',
      assistantEventId: 'turn-000001:assistant',
      userTranscript: 'Question',
      assistantTranscript: 'Answer',
      startedAt: DateTime.utc(2026, 8, 15, 20),
      completedAt: DateTime.utc(2026, 8, 15, 20, 0, 2),
      turnOrdinal: 0,
      exactAuthority: authority,
      transport: ({required url, required body, required expectedAuthenticatedUid, required exactAuthority}) async =>
          http.Response(privateErrorBody, 503),
    );

    expect(result.isFailure, isTrue);
    expect(result.value, isNull);
    expect(result.failure?.backendCode, isNull);
    expect(result.failure.toString(), isNot(contains('private transcript')));
  });

  test('Ella chat inactivity timeout cancels an otherwise silent stream', () async {
    final source = StreamController<ServerMessageChunk>();
    final result = withEllaChatInactivityTimeout(source.stream, timeout: const Duration(milliseconds: 10)).toList();

    await expectLater(
      result,
      throwsA(
        isA<ClientApiFailure>()
            .having((failure) => failure.kind, 'kind', ClientApiFailureKind.unavailable)
            .having((failure) => failure.retryable, 'retryable', isTrue),
      ),
    );
    expect(source.hasListener, isFalse);
    await source.close();
  });
}
