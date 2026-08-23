import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;

import 'package:omi/ella/services/memory_artwork_api.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class _Authority implements ExactAccountAuthorityVerifier {
  _Authority(this.uid);

  @override
  final String uid;

  bool current = true;

  @override
  bool isExactCurrent() => current;
}

void main() {
  test('fetch binds the signed artwork request to the exact authenticated authority', () async {
    final authority = _Authority('owner-a');
    late String requestedUrl;
    late String expectedUid;
    late ExactAccountAuthorityVerifier? requestAuthority;
    final api = MemoryArtworkApi(
      baseUrl: 'https://api.example/',
      authorityProvider: () => authority,
      request: ({
        required url,
        required headers,
        required body,
        required method,
        timeout,
        retries,
        requireAuthCheck,
        expectedAuthenticatedUid,
        exactAuthority,
      }) async {
        requestedUrl = url;
        expectedUid = expectedAuthenticatedUid!;
        requestAuthority = exactAuthority;
        expect(method, 'GET');
        expect(requireAuthCheck, isTrue);
        return http.Response(
          jsonEncode({
            'schema_version': memoryArtworkSchemaVersion,
            'status': 'ready',
            'url': 'https://private-storage.example/signed',
            'style_version': memoryArtworkDefaultStyle,
            'enrichment_revision': 'summary-a',
          }),
          200,
        );
      },
    );

    final result = await api.fetch('memory/a');

    expect(result.isReady, isTrue);
    expect(result.url, Uri.parse('https://private-storage.example/signed'));
    expect(requestedUrl, 'https://api.example/v1/ella/memories/memory%2Fa/artwork');
    expect(expectedUid, 'owner-a');
    expect(requestAuthority, same(authority));
  });

  test('fetch rejects vendor or malformed URLs and never exposes them', () async {
    final api = MemoryArtworkApi(
      baseUrl: 'https://api.example/',
      authorityProvider: () => _Authority('owner-a'),
      request: ({
        required url,
        required headers,
        required body,
        required method,
        timeout,
        retries,
        requireAuthCheck,
        expectedAuthenticatedUid,
        exactAuthority,
      }) async =>
          http.Response(
        jsonEncode({'schema_version': memoryArtworkSchemaVersion, 'status': 'ready', 'url': 'http://vendor.invalid/a'}),
        200,
      ),
    );

    final result = await api.fetch('memory-a');

    expect(result.status, MemoryArtworkResultStatus.unavailable);
    expect(result.failureCode, 'memory_artwork_url_invalid');
    expect(result.url, isNull);
  });

  test('missing exact authority performs no network work', () async {
    var calls = 0;
    final api = MemoryArtworkApi(
      baseUrl: 'https://api.example/',
      authorityProvider: () => null,
      request: ({
        required url,
        required headers,
        required body,
        required method,
        timeout,
        retries,
        requireAuthCheck,
        expectedAuthenticatedUid,
        exactAuthority,
      }) async {
        calls++;
        return http.Response('{}', 200);
      },
    );

    expect((await api.fetch('memory-a')).status, MemoryArtworkResultStatus.unavailable);
    expect(await api.preferences(), isNull);
    expect(await api.backfillRecent(), isFalse);
    expect(calls, 0);
  });

  test('style update and bounded backfill use authenticated first-party routes', () async {
    final methods = <String>[];
    final urls = <String>[];
    final bodies = <String>[];
    final api = MemoryArtworkApi(
      baseUrl: 'https://api.example',
      authorityProvider: () => _Authority('owner-a'),
      request: ({
        required url,
        required headers,
        required body,
        required method,
        timeout,
        retries,
        requireAuthCheck,
        expectedAuthenticatedUid,
        exactAuthority,
      }) async {
        methods.add(method);
        urls.add(url);
        bodies.add(body);
        return http.Response('{}', 200);
      },
    );

    expect(
      await api.setStyle(consentVersion: 'ai-data-processors-v9', styleVersion: memoryArtworkPaperCollageStyle),
      isTrue,
    );
    expect(await api.backfillRecent(), isTrue);
    expect(methods, ['PUT', 'POST']);
    expect(urls, [
      'https://api.example/v1/ella/memory-artwork/preferences',
      'https://api.example/v1/ella/memory-artwork/backfill',
    ]);
    expect(jsonDecode(bodies.first)['style_version'], memoryArtworkPaperCollageStyle);
  });
}
