import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ella_chat_service.dart';
import 'package:omi/ella/services/ella_service_result.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/platform/platform_manager.dart';

class _Authority implements AccountCommitAuthority {
  const _Authority(this.uid, this.current);
  @override
  final String uid;
  final bool Function() current;
  @override
  bool isCurrent() => current();
  @override
  bool isExactCurrent() => current();
}

Map<String, dynamic> wire({List<dynamic>? rows, bool more = false, String? cursor}) => {
      'messages': rows ?? [],
      'hasMore': more,
      'paginationVersion': 1,
      'pageOrder': 'canonical-keyset-v1-c8',
      'nextCursor': cursor,
      'source': 'canonical_timeline',
      'fallback': false,
      'canonical_memory_status': 'available'
    };
Map<String, dynamic> row(
        {String id = 'same',
        String key = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        String text = 'Synthetic text'}) =>
    {'id': id, 'canonicalEventKey': key, 'content': text, 'sender': 'ai', 'timestamp': '2026-10-12T12:00:00Z'};
Future<EllaServiceResult<EllaChatHistoryPage>> parse(Map<String, dynamic> body,
        {String? cursor, void Function(String)? observer}) =>
    fetchEllaChatHistoryPage(
        keyset: true,
        cursor: cursor,
        expectedAuthenticatedUid: 'owner-a',
        exactAuthority: _Authority('owner-a', () => true),
        transport: ({required String url, required expectedAuthenticatedUid, required exactAuthority}) async {
          observer?.call(url);
          return http.Response(jsonEncode(body), 200);
        });
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  PlatformManager.initializeForTesting();
  Env.init(_TestEnv());
  setUp(() async {
    SharedPreferences.setMockInitialValues({'uid': 'owner-a'});
    await SharedPreferencesUtil.init();
  });
  test('explicit opt-in passes opaque cursor, no timestamp derivation', () async {
    final result =
        await parse(wire(rows: [row()], more: true, cursor: 'next.token'), cursor: 'current.token', observer: (url) {
      final q = Uri.parse(url).queryParameters;
      expect(q['pagination'], 'keyset-v1');
      expect(q['cursor'], 'current.token');
      expect(q.containsKey('before'), false);
    });
    expect(result.isSuccess, true);
    expect(result.value!.isKeyset, true);
    expect(result.value!.paginationVersion, 1);
    expect(result.value!.rawRowCount, 1);
    expect(result.value!.nextCursor, 'next.token');
    expect(result.value!.messages.single.canonicalEventKey, isNotNull);
  });
  test('different exact keys sharing legacy id survive cache roundtrip', () async {
    final result =
        await parse(wire(rows: [row(), row(key: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb')]));
    expect(result.value!.messages.length, 2);
    SharedPreferencesUtil().cachedMessages = result.value!.messages;
    expect(SharedPreferencesUtil().cachedMessages.map((m) => m.canonicalEventKey).toSet().length, 2);
  });
  test('equal legacy projection fields retain chronological server page ordering', () async {
    final a = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
    final b = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
    final result = await parse(wire(rows: [row(key: b), row(key: a)]));
    expect(result.value!.messages.map((m) => m.canonicalEventKey), [a, b]);
  });
  test('display filtering cannot hide malformed timestamp or missing content', () async {
    final hidden = row(text: '[SYSTEM:synthetic]')..['timestamp'] = 'invalid';
    final missing = row()..remove('content');
    expect((await parse(wire(rows: [row(), hidden]))).isFailure, true);
    expect((await parse(wire(rows: [row(), missing]))).isFailure, true);
  });
  test('display-filtered page keeps continuation authority', () async {
    final result = await parse(wire(rows: [row(text: '[SYSTEM:synthetic]')], more: true, cursor: 'next.token'));
    expect(result.isSuccess, true);
    expect(result.value!.messages, isEmpty);
    expect(result.value!.rawRowCount, 1);
    expect(result.value!.paginationVersion, 1);
    expect(result.value!.nextCursor, 'next.token');
  });
  for (final change in <String, dynamic>{
    'paginationVersion': 2,
    'pageOrder': 'unknown',
    'source': 'legacy',
    'fallback': true,
    'hasMore': 'true',
    'nextCursor': 123,
    'messages': null,
    'canonical_memory_status': 'unknown'
  }.entries) {
    test('reject envelope ${change.key}', () async {
      final body = wire()..[change.key] = change.value;
      expect((await parse(body)).failure?.kind, ClientApiFailureKind.invalidResponse);
    });
  }
  test('hasMore needs bounded token, terminal needs explicit null', () async {
    for (final body in [
      wire(more: true),
      wire(cursor: 'unexpected.token'),
      wire(more: true, cursor: 'bad token'),
      wire()..remove('nextCursor')
    ]) {
      expect((await parse(body)).isFailure, true);
    }
  });
  for (final field in ['canonicalEventKey', 'id', 'timestamp', 'sender']) {
    test('reject malformed row$field', () async {
      final item = row()..[field] = field == 'timestamp' ? 'bad-time' : null;
      expect((await parse(wire(rows: [item]))).isFailure, true);
    });
  }
  test('duplicate exact identity is invalid within one response', () async {
    expect((await parse(wire(rows: [row(), row()]))).isFailure, true);
  });
  test('malformed row container rejected even alongside recognized message', () async {
    expect((await parse(wire(rows: [row(), 7]))).isFailure, true);
  });
}

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
