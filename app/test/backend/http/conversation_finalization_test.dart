import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class _CaptureGenerationAuthority implements ExactAccountAuthorityVerifier {
  _CaptureGenerationAuthority(this.uid);

  @override
  final String uid;
  bool current = true;

  @override
  bool isExactCurrent() => current;
}

Map<String, dynamic> _conversationJson(String id, String status) => {
      'id': id,
      'created_at': '2026-08-15T12:00:00Z',
      'structured': {
        'title': 'Capture',
        'overview': 'Finalized capture',
        'emoji': '',
        'category': 'other',
      },
      'status': status,
    };

void main() {
  const conversationId = 'capture-conversation';
  setUpAll(Env.init);

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
        required expectedAuthenticatedUid,
        required exactAuthority,
      }) async {
        expect(expectedAuthenticatedUid, authority.uid);
        expect(identical(exactAuthority, authority), isTrue);
        if (method == 'POST') {
          postCalls++;
          expect(jsonDecode(body), {'conversation_id': conversationId});
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
    expect(postCalls, 1);
    expect(getCalls, 23);
    expect(elapsed, const Duration(seconds: 23));
    expect(elapsed, greaterThan(const Duration(seconds: 10)));
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
    expect(postCalls, 1);
    expect(getCalls, 3);
  });
}
