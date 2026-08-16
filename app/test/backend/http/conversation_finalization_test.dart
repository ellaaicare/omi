import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/http/http_pool_manager.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
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

class _CaptureGenerationAuthority implements ExactAccountAuthorityVerifier {
  _CaptureGenerationAuthority(this.uid);

  @override
  final String uid;
  bool current = true;
  int checks = 0;

  @override
  bool isExactCurrent() {
    checks++;
    return current;
  }
}

class _InspectingClient extends http.BaseClient {
  _InspectingClient(this.handler);

  final Future<http.StreamedResponse> Function(http.BaseRequest request) handler;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) => handler(request);
}

Map<String, dynamic> _conversationJson(String id, String status) => {
      'id': id,
      'created_at': '2026-08-15T12:00:00Z',
      'structured': {'title': 'Capture', 'overview': 'Finalized capture', 'emoji': '', 'category': 'other'},
      'status': status,
    };

Map<String, dynamic> _processResponseJson(String id, String status) => {
      'conversation': _conversationJson(id, status),
      'messages': <dynamic>[],
    };

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const conversationId = 'capture-conversation';
  setUpAll(() async {
    Env.init(_TestEnv());
    PlatformManager.initializeForTesting();
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    SharedPreferencesUtil().authToken = 'test-bearer';
    SharedPreferencesUtil().tokenExpirationTime = DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch;
  });

  test('409 beyond ten seconds polls the exact capture until the backend completes', () async {
    final authority = _CaptureGenerationAuthority('uid-a');
    var postCalls = 0;
    var getCalls = 0;
    var elapsed = Duration.zero;

    final result = await processInProgressConversation(
      conversationId: conversationId,
      expectedAuthenticatedUid: authority.uid,
      exactAuthority: authority,
      maxStatusPollAttempts: 25,
      statusPollInterval: const Duration(seconds: 1),
      delay: (duration) async => elapsed += duration,
      transport: ({
        required url,
        required method,
        required body,
        required timeout,
        required retries,
        required retryOnUnauthorized,
        required expectedAuthenticatedUid,
        required exactAuthority,
      }) async {
        expect(expectedAuthenticatedUid, authority.uid);
        expect(identical(exactAuthority, authority), isTrue);
        expect(timeout, greaterThan(Duration.zero));
        expect(retries, 0);
        expect(retryOnUnauthorized, isFalse);
        if (method == 'POST') {
          postCalls++;
          expect(jsonDecode(body), {
            'conversation_id': conversationId,
            'protocol_version': 2,
            'generation': 'test-generation',
            'owner_token': 'test-owner-token',
          });
          return http.Response('capture owner active', 409);
        }

        getCalls++;
        expect(url.endsWith('/v1/conversations/$conversationId'), isTrue);
        if (getCalls <= 12) return http.Response('capture owner active', 409);
        if (getCalls <= 14) return http.Response('conversation stub not visible yet', 404);
        final status = getCalls <= 22 ? 'processing' : 'completed';
        return http.Response(jsonEncode(_conversationJson(conversationId, status)), 200);
      },
    );

    expect(result?.conversation?.id, conversationId);
    expect(result?.conversation?.status.name, 'completed');
    expect(postCalls, 9, reason: 'processing receipts retry the leased, idempotent claim');
    expect(getCalls, 23);
    expect(elapsed, const Duration(seconds: 23));
    expect(elapsed, greaterThan(const Duration(seconds: 10)));
  });

  for (final directStatus in ['processing', 'merging']) {
    test('direct 200 $directStatus polls the exact capture before success', () async {
      var postCalls = 0;
      var getCalls = 0;
      final result = await processInProgressConversation(
        conversationId: conversationId,
        statusPollInterval: Duration.zero,
        transport: ({
          required url,
          required method,
          required body,
          required timeout,
          required retries,
          required retryOnUnauthorized,
          required expectedAuthenticatedUid,
          required exactAuthority,
        }) async {
          expect(retries, 0);
          expect(retryOnUnauthorized, isFalse);
          if (method == 'POST') {
            postCalls++;
            return http.Response(jsonEncode(_processResponseJson(conversationId, directStatus)), 200);
          }
          getCalls++;
          return http.Response(jsonEncode(_conversationJson(conversationId, 'completed')), 200);
        },
      );

      expect(result?.conversation?.status, ConversationStatus.completed);
      expect(postCalls, 1);
      expect(getCalls, 1);
    });
  }

  test('direct 200 completed succeeds once without a status GET', () async {
    var postCalls = 0;
    var getCalls = 0;
    final result = await processInProgressConversation(
      conversationId: conversationId,
      statusPollInterval: Duration.zero,
      transport: ({
        required url,
        required method,
        required body,
        required timeout,
        required retries,
        required retryOnUnauthorized,
        required expectedAuthenticatedUid,
        required exactAuthority,
      }) async {
        if (method == 'POST') {
          postCalls++;
          return http.Response(jsonEncode(_processResponseJson(conversationId, 'completed')), 200);
        }
        getCalls++;
        return http.Response(jsonEncode(_conversationJson(conversationId, 'completed')), 200);
      },
    );

    expect(result?.conversation?.status, ConversationStatus.completed);
    expect(postCalls, 1);
    expect(getCalls, 0);
  });

  test('direct 200 failed is terminal and never enters polling', () async {
    var getCalls = 0;
    final result = await processInProgressConversation(
      conversationId: conversationId,
      statusPollInterval: Duration.zero,
      transport: ({
        required url,
        required method,
        required body,
        required timeout,
        required retries,
        required retryOnUnauthorized,
        required expectedAuthenticatedUid,
        required exactAuthority,
      }) async {
        if (method == 'POST') {
          return http.Response(jsonEncode(_processResponseJson(conversationId, 'failed')), 200);
        }
        getCalls++;
        return http.Response(jsonEncode(_conversationJson(conversationId, 'completed')), 200);
      },
    );

    expect(result, isNull);
    expect(getCalls, 0);
  });

  final invalidDirectBodies = <String, String>{
    'missing status': jsonEncode({
      'conversation': Map<String, dynamic>.from(_conversationJson(conversationId, 'completed'))..remove('status'),
      'messages': <dynamic>[],
    }),
    'unknown status': jsonEncode(_processResponseJson(conversationId, 'ready')),
    'malformed JSON': '{"conversation":',
    'cross-ID response': jsonEncode(_processResponseJson('successor-capture', 'completed')),
  };
  for (final invalidCase in invalidDirectBodies.entries) {
    test('direct 200 ${invalidCase.key} fails closed before model construction', () async {
      var getCalls = 0;
      final result = await processInProgressConversation(
        conversationId: conversationId,
        statusPollInterval: Duration.zero,
        transport: ({
          required url,
          required method,
          required body,
          required timeout,
          required retries,
          required retryOnUnauthorized,
          required expectedAuthenticatedUid,
          required exactAuthority,
        }) async {
          if (method == 'POST') return http.Response(invalidCase.value, 200);
          getCalls++;
          return http.Response(jsonEncode(_conversationJson(conversationId, 'completed')), 200);
        },
      );

      expect(result, isNull);
      expect(getCalls, 0);
    });
  }

  final invalidPolledBodies = <String, String>{
    'missing status': jsonEncode(
      Map<String, dynamic>.from(_conversationJson(conversationId, 'completed'))..remove('status'),
    ),
    'unknown status': jsonEncode(_conversationJson(conversationId, 'ready')),
    'malformed JSON': '{"id":',
  };
  for (final invalidCase in invalidPolledBodies.entries) {
    test('polled 200 ${invalidCase.key} fails closed before model construction', () async {
      var getCalls = 0;
      final result = await processInProgressConversation(
        conversationId: conversationId,
        statusPollInterval: Duration.zero,
        transport: ({
          required url,
          required method,
          required body,
          required timeout,
          required retries,
          required retryOnUnauthorized,
          required expectedAuthenticatedUid,
          required exactAuthority,
        }) async {
          if (method == 'POST') return http.Response('capture owner active', 409);
          getCalls++;
          return http.Response(invalidCase.value, 200);
        },
      );

      expect(result, isNull);
      expect(getCalls, 1);
    });
  }

  test('real makeApiCall boundary emits one POST after 5xx and recovers only by exact-ID GET', () async {
    const encodedConversationId = 'capture / conversation';
    var postCalls = 0;
    var getCalls = 0;
    HttpPoolManager.instance.replaceClientForTesting(
      MockClient((request) async {
        if (request.method == 'POST') {
          postCalls++;
          expect(jsonDecode(request.body), {
            'conversation_id': encodedConversationId,
            'protocol_version': 2,
            'generation': 'test-generation',
            'owner_token': 'test-owner-token',
          });
          return http.Response('ambiguous server failure', 503);
        }
        getCalls++;
        expect(request.url.toString(), endsWith('/v1/conversations/capture%20%2F%20conversation'));
        return http.Response(jsonEncode(_conversationJson(encodedConversationId, 'completed')), 200);
      }),
    );

    final result = await processInProgressConversation(
      conversationId: encodedConversationId,
      statusPollInterval: Duration.zero,
    );

    expect(result?.conversation?.id, encodedConversationId);
    expect(postCalls, 1, reason: 'the mutating makeApiCall adapter must never retry');
    expect(getCalls, 1);
  });

  test('real makeApiCall boundary does not refresh and resend a rejected POST', () async {
    var postCalls = 0;
    HttpPoolManager.instance.replaceClientForTesting(
      MockClient((request) async {
        expect(request.method, 'POST');
        postCalls++;
        return http.Response('unauthorized', 401);
      }),
    );

    final result = await processInProgressConversation(
      conversationId: conversationId,
      statusPollInterval: Duration.zero,
    );

    expect(result, isNull);
    expect(postCalls, 1);
  });

  test('pool admission timeout prevents a queued finalization request from sending after release', () async {
    final releasePool = Completer<void>();
    final poolSaturated = Completer<void>();
    var blockingRequests = 0;
    var finalizationRequests = 0;
    HttpPoolManager.instance.replaceClientForTesting(
      _InspectingClient((request) async {
        if (request.url.host == 'pool-saturation.ella.test') {
          blockingRequests++;
          if (blockingRequests == 10) poolSaturated.complete();
          await releasePool.future;
          return http.StreamedResponse(Stream.value(const <int>[]), 200);
        }
        finalizationRequests++;
        return http.StreamedResponse(Stream.value(utf8.encode('late finalization request')), 200);
      }),
    );

    final blockers = List<Future<http.Response>>.generate(
      10,
      (index) => HttpPoolManager.instance.send(
        () => http.Request('GET', Uri.parse('https://pool-saturation.ella.test/$index')),
        timeout: const Duration(seconds: 2),
        retries: 0,
      ),
    );
    await poolSaturated.future.timeout(const Duration(seconds: 1));

    final result = await processInProgressConversation(
      conversationId: conversationId,
      statusPollInterval: Duration.zero,
      statusPollTimeout: const Duration(milliseconds: 40),
    );

    expect(result, isNull);
    expect(finalizationRequests, 0);
    releasePool.complete();
    await Future.wait(blockers);
    await Future<void>.delayed(const Duration(milliseconds: 50));
    expect(finalizationRequests, 0, reason: 'expired queued work must not produce late network side effects');
  });

  test('hung response body obeys the monotonic deadline and performs no nested GET retry', () async {
    final authority = _CaptureGenerationAuthority('uid-a');
    var postCalls = 0;
    var getCalls = 0;
    final neverCloses = StreamController<List<int>>();
    addTearDown(neverCloses.close);
    HttpPoolManager.instance.replaceClientForTesting(
      _InspectingClient((request) async {
        if (request.method == 'POST') {
          postCalls++;
          return http.StreamedResponse(Stream.value(utf8.encode('capture owner active')), 409);
        }
        getCalls++;
        return http.StreamedResponse(neverCloses.stream, 200);
      }),
    );
    final elapsed = Stopwatch()..start();

    final result = await processInProgressConversation(
      conversationId: conversationId,
      exactAuthority: authority,
      statusPollInterval: Duration.zero,
      statusPollTimeout: const Duration(milliseconds: 80),
    );

    elapsed.stop();
    expect(result, isNull);
    expect(postCalls, 1);
    expect(getCalls, 1);
    expect(authority.checks, greaterThanOrEqualTo(6));
    expect(elapsed.elapsed, lessThan(const Duration(seconds: 2)));
  });

  test('completed status is idempotent and never repeats the processing POST', () async {
    final authority = _CaptureGenerationAuthority('uid-a');
    var postCalls = 0;
    var getCalls = 0;

    final result = await processInProgressConversation(
      conversationId: conversationId,
      expectedAuthenticatedUid: authority.uid,
      exactAuthority: authority,
      statusPollInterval: Duration.zero,
      transport: ({
        required url,
        required method,
        required body,
        required timeout,
        required retries,
        required retryOnUnauthorized,
        required expectedAuthenticatedUid,
        required exactAuthority,
      }) async {
        if (method == 'POST') {
          postCalls++;
          return http.Response('capture owner active', 409);
        }
        getCalls++;
        return http.Response(jsonEncode(_conversationJson(conversationId, 'completed')), 200);
      },
    );

    expect(result?.conversation?.status.name, 'completed');
    expect(postCalls, 1);
    expect(getCalls, 1);
  });

  test('capture-generation authority loss aborts before another status request', () async {
    final authority = _CaptureGenerationAuthority('uid-a');
    var postCalls = 0;
    var getCalls = 0;

    final finalization = processInProgressConversation(
      conversationId: conversationId,
      expectedAuthenticatedUid: authority.uid,
      exactAuthority: authority,
      delay: (_) async => authority.current = false,
      transport: ({
        required url,
        required method,
        required body,
        required timeout,
        required retries,
        required retryOnUnauthorized,
        required expectedAuthenticatedUid,
        required exactAuthority,
      }) async {
        if (method == 'POST') {
          postCalls++;
          return http.Response('capture owner active', 409);
        }
        getCalls++;
        return http.Response(jsonEncode(_conversationJson(conversationId, 'completed')), 200);
      },
    );

    await expectLater(finalization, throwsA(isA<ExactAccountAuthorityChangedException>()));
    expect(postCalls, 1);
    expect(getCalls, 0);
  });

  test('failed server status remains a genuine terminal finalization failure', () async {
    final authority = _CaptureGenerationAuthority('uid-a');
    var postCalls = 0;
    var getCalls = 0;

    final result = await processInProgressConversation(
      conversationId: conversationId,
      expectedAuthenticatedUid: authority.uid,
      exactAuthority: authority,
      statusPollInterval: Duration.zero,
      transport: ({
        required url,
        required method,
        required body,
        required timeout,
        required retries,
        required retryOnUnauthorized,
        required expectedAuthenticatedUid,
        required exactAuthority,
      }) async {
        if (method == 'POST') {
          postCalls++;
          return http.Response('capture owner active', 409);
        }
        getCalls++;
        return http.Response(jsonEncode(_conversationJson(conversationId, 'failed')), 200);
      },
    );

    expect(result, isNull);
    expect(postCalls, 1);
    expect(getCalls, 1);
  });

  test('a completed response for a different conversation fails closed', () async {
    final authority = _CaptureGenerationAuthority('uid-a');

    final result = await processInProgressConversation(
      conversationId: conversationId,
      expectedAuthenticatedUid: authority.uid,
      exactAuthority: authority,
      statusPollInterval: Duration.zero,
      transport: ({
        required url,
        required method,
        required body,
        required timeout,
        required retries,
        required retryOnUnauthorized,
        required expectedAuthenticatedUid,
        required exactAuthority,
      }) async {
        if (method == 'POST') return http.Response('capture owner active', 409);
        return http.Response(jsonEncode(_conversationJson('successor-capture', 'completed')), 200);
      },
    );

    expect(result, isNull);
  });

  test('pending status exhausts the bounded polling window', () async {
    final authority = _CaptureGenerationAuthority('uid-a');
    var postCalls = 0;
    var getCalls = 0;

    final result = await processInProgressConversation(
      conversationId: conversationId,
      expectedAuthenticatedUid: authority.uid,
      exactAuthority: authority,
      maxStatusPollAttempts: 3,
      statusPollInterval: Duration.zero,
      transport: ({
        required url,
        required method,
        required body,
        required timeout,
        required retries,
        required retryOnUnauthorized,
        required expectedAuthenticatedUid,
        required exactAuthority,
      }) async {
        if (method == 'POST') {
          postCalls++;
          return http.Response('capture owner active', 409);
        }
        getCalls++;
        return http.Response(jsonEncode(_conversationJson(conversationId, 'processing')), 200);
      },
    );

    expect(result, isNull);
    expect(postCalls, 4, reason: 'each processing receipt may reclaim an expired durable lease');
    expect(getCalls, 3);
  });
}
