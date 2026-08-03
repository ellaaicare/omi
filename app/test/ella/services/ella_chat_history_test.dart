import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/services/ella_chat_service.dart';

Map<String, String> _testDebugHeaders({required String routeSource}) => {'X-Ella-Route-Source': routeSource};
String _testApiBaseUrl() => 'https://first-party.example/';
String _productionHistoryResponse() =>
    File('../backend/tests/fixtures/ella_chat_history_canonical_response_v1.json').readAsStringSync();

void main() {
  setUp(() async {
    SharedPreferences.setMockInitialValues({'uid': 'caller-selected-uid'});
    await SharedPreferencesUtil.init();
  });

  test('production canonical history maps exact human and ai messages through the first-party endpoint', () async {
    final requestedUrls = <Uri>[];

    final messages = await fetchEllaChatHistory(
      limit: 25,
      debugHeadersBuilder: _testDebugHeaders,
      apiBaseUrlProvider: _testApiBaseUrl,
      apiCall: ({required url, required headers, required body, required method, timeout, retries}) async {
        requestedUrls.add(Uri.parse(url));
        expect(method, 'GET');
        expect(body, isEmpty);
        expect(headers['X-Ella-Route-Source'], 'chat-history');
        return http.Response(_productionHistoryResponse(), 200);
      },
    );

    expect(requestedUrls, hasLength(1));
    expect(requestedUrls.single.path, endsWith('/v1/ella/chat/history'));
    expect(requestedUrls.single.queryParameters, {'limit': '25'});
    expect(requestedUrls.single.toString(), isNot(contains('/resolve')));
    expect(requestedUrls.single.toString(), isNot(contains('caller-selected-uid')));
    expect(messages, hasLength(2));
    expect(messages[0].id, 'canonical-user-turn');
    expect(messages[0].text, 'Exact user history request.');
    expect(messages[0].sender, MessageSender.human);
    expect(messages[0].createdAt.toUtc(), DateTime.parse('2026-08-03T12:00:00Z'));
    expect(messages[1].id, 'canonical-assistant-turn');
    expect(messages[1].text, 'Exact assistant history response.');
    expect(messages[1].sender, MessageSender.ai);
    expect(messages[1].createdAt.toUtc(), DateTime.parse('2026-08-03T12:01:00Z'));
    expect(messages.every((message) => !message.askForNps), isTrue);
  });

  test('history returns empty for authenticated empty history', () async {
    var calls = 0;
    final messages = await fetchEllaChatHistory(
      debugHeadersBuilder: _testDebugHeaders,
      apiBaseUrlProvider: _testApiBaseUrl,
      apiCall: ({required url, required headers, required body, required method, timeout, retries}) async {
        calls += 1;
        return http.Response('{"messages":[]}', 200);
      },
    );

    expect(calls, 1);
    expect(messages, isEmpty);
  });

  test('malformed production entries fail safely without dropping valid siblings or accepting legacy aliases',
      () async {
    final payload = jsonDecode(_productionHistoryResponse()) as Map<String, dynamic>;
    final canonicalMessages = payload['messages'] as List<dynamic>;
    payload['messages'] = [
      canonicalMessages[0],
      'not-a-message',
      {
        'id': 'legacy-alias-message',
        'role': 'user',
        'content': 'Legacy aliases must not mask contract drift.',
        'timestamp': '2026-08-03T12:00:30Z',
      },
      {
        'id': 'malformed-production-message',
        'created_at': 'not-a-timestamp',
        'text': 'Malformed timestamp',
        'sender': 'human',
        'type': 'text',
      },
      canonicalMessages[1],
    ];

    final messages = await fetchEllaChatHistory(
      debugHeadersBuilder: _testDebugHeaders,
      apiBaseUrlProvider: _testApiBaseUrl,
      apiCall: ({required url, required headers, required body, required method, timeout, retries}) async {
        return http.Response(jsonEncode(payload), 200);
      },
    );

    expect(messages.map((message) => message.id), ['canonical-user-turn', 'canonical-assistant-turn']);
  });

  test('authentication and upstream failures remain empty rather than fabricating history', () async {
    var calls = 0;
    for (final status in [401, 502, 503]) {
      final messages = await fetchEllaChatHistory(
        debugHeadersBuilder: _testDebugHeaders,
        apiBaseUrlProvider: _testApiBaseUrl,
        apiCall: ({required url, required headers, required body, required method, timeout, retries}) async {
          calls += 1;
          return http.Response('{"detail":"unavailable"}', status);
        },
      );

      expect(messages, isEmpty);
    }
    expect(calls, 3);
  });
}
