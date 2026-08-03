import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/services/ella_chat_service.dart';

Map<String, String> _testDebugHeaders({required String routeSource}) => {'X-Ella-Route-Source': routeSource};
String _testApiBaseUrl() => 'https://first-party.example/';

void main() {
  setUp(() async {
    SharedPreferences.setMockInitialValues({'uid': 'caller-selected-uid'});
    await SharedPreferencesUtil.init();
  });

  test('cold history fetch parses the canonical production response without resolver or UID input', () async {
    final requestedUrls = <Uri>[];
    final canonicalHistoryResponse = {
      'messages': [
        {
          'id': 'm-newer',
          'created_at': '2026-08-03T12:01:00Z',
          'text': 'Ready when you are.',
          'sender': 'ai',
          'type': 'text',
          'plugin_id': null,
          'from_integration': false,
          'memories': <Object>[],
          'files': <Object>[],
          'metadata': {'source': 'canonical_timeline'},
        },
        {
          'id': 'm-older',
          'created_at': '2026-08-03T12:00:00Z',
          'text': 'Hello',
          'sender': 'human',
          'type': 'text',
          'plugin_id': null,
          'from_integration': false,
          'memories': <Object>[],
          'files': <Object>[],
          'metadata': {'source': 'canonical_timeline'},
        },
      ],
      'hasMore': false,
      'source': 'canonical_timeline',
      'fallback': false,
    };

    final messages = await fetchEllaChatHistory(
      limit: 25,
      debugHeadersBuilder: _testDebugHeaders,
      apiBaseUrlProvider: _testApiBaseUrl,
      apiCall: ({required url, required headers, required body, required method, timeout, retries}) async {
        requestedUrls.add(Uri.parse(url));
        expect(method, 'GET');
        expect(body, isEmpty);
        expect(headers['X-Ella-Route-Source'], 'chat-history');
        return http.Response(jsonEncode(canonicalHistoryResponse), 200);
      },
    );

    expect(requestedUrls, hasLength(1));
    expect(requestedUrls.single.path, endsWith('/v1/ella/chat/history'));
    expect(requestedUrls.single.queryParameters, {'limit': '25'});
    expect(requestedUrls.single.toString(), isNot(contains('/resolve')));
    expect(requestedUrls.single.toString(), isNot(contains('caller-selected-uid')));
    expect(messages, hasLength(2));
    expect(messages.map((message) => message.id), ['m-older', 'm-newer']);
    expect(messages.first.text, 'Hello');
    expect(messages.first.sender, MessageSender.human);
    expect(messages.first.askForNps, isFalse);
    expect(messages.last.text, 'Ready when you are.');
    expect(messages.last.sender, MessageSender.ai);
    expect(messages.last.askForNps, isFalse);
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

  test('history fails closed on authentication failure', () async {
    var calls = 0;
    final messages = await fetchEllaChatHistory(
      debugHeadersBuilder: _testDebugHeaders,
      apiBaseUrlProvider: _testApiBaseUrl,
      apiCall: ({required url, required headers, required body, required method, timeout, retries}) async {
        calls += 1;
        return http.Response('{"detail":"invalid bearer"}', 401);
      },
    );

    expect(calls, 1);
    expect(messages, isEmpty);
  });
}
