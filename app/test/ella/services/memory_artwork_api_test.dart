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
    expect(result.cacheKey, hasLength(64));
    expect(requestedUrl, 'https://api.example/v1/ella/memories/memory%2Fa/artwork');
    expect(expectedUid, 'owner-a');
    expect(requestAuthority, same(authority));
  });

  test('cache identity is stable across signed URL renewal and isolated by owner', () async {
    var urlRevision = 0;
    MemoryArtworkApi apiFor(_Authority authority) => MemoryArtworkApi(
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
            urlRevision += 1;
            return http.Response(
              jsonEncode({
                'schema_version': memoryArtworkSchemaVersion,
                'status': 'ready',
                'url': 'https://private-storage.example/signed-$urlRevision',
                'style_version': memoryArtworkDefaultStyle,
                'enrichment_revision': 'summary-a',
              }),
              200,
            );
          },
        );

    final ownerAApi = apiFor(_Authority('owner-a'));
    final first = await ownerAApi.fetch('memory-a');
    final renewed = await ownerAApi.fetch('memory-a');
    final otherOwner = await apiFor(_Authority('owner-b')).fetch('memory-a');

    expect(first.url, isNot(renewed.url));
    expect(first.cacheKey, renewed.cacheKey);
    expect(otherOwner.cacheKey, isNot(first.cacheKey));
  });

  test('display cache identity matches the authenticated fetch identity', () async {
    final authority = _Authority('owner-a');
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
      }) async =>
          http.Response(
        jsonEncode({
          'schema_version': memoryArtworkSchemaVersion,
          'status': 'ready',
          'url': 'https://private-storage.example/signed',
          'style_version': memoryArtworkDefaultStyle,
          'enrichment_revision': 'summary-a',
        }),
        200,
      ),
    );

    final fetched = await api.fetch('memory-a');
    final displayKey = api.cacheKeyForDisplay(
      memoryId: 'memory-a',
      styleVersion: memoryArtworkDefaultStyle,
      enrichmentRevision: 'summary-a',
    );

    expect(displayKey, fetched.cacheKey);
    authority.current = false;
    expect(
      api.cacheKeyForDisplay(
        memoryId: 'memory-a',
        styleVersion: memoryArtworkDefaultStyle,
        enrichmentRevision: 'summary-a',
      ),
      isEmpty,
    );
  });

  test('visible historical memory is enqueued once and polled until artwork is ready', () async {
    final authority = _Authority('owner-a');
    final methods = <String>[];
    var getCalls = 0;
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
        methods.add(method);
        if (method == 'POST') {
          return http.Response(jsonEncode({'outcome': 'queued', 'status': 'generating'}), 202);
        }
        getCalls += 1;
        if (getCalls == 1) {
          return http.Response(
            jsonEncode({
              'detail': {'code': 'memory_artwork_not_found'},
            }),
            404,
          );
        }
        return http.Response(
          jsonEncode({
            'schema_version': memoryArtworkSchemaVersion,
            'status': getCalls == 2 ? 'generating' : 'ready',
            if (getCalls > 2) 'url': 'https://private-storage.example/lazy-ready',
            'style_version': memoryArtworkDefaultStyle,
            'enrichment_revision': 'summary-lazy',
          }),
          200,
        );
      },
    );

    final result = await api.loadForDisplay(
      'memory-old',
      enqueueIfMissing: true,
      pollAttempts: 3,
      pollInterval: Duration.zero,
    );

    expect(result.isReady, isTrue);
    expect(methods, ['GET', 'POST', 'GET', 'GET']);
    expect(result.cacheKey, hasLength(64));
  });

  test('already-generating artwork is polled without duplicate enqueue', () async {
    final authority = _Authority('owner-a');
    final methods = <String>[];
    var getCalls = 0;
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
        methods.add(method);
        getCalls += 1;
        return http.Response(
          jsonEncode({
            'schema_version': memoryArtworkSchemaVersion,
            'status': getCalls == 1 ? 'generating' : 'ready',
            if (getCalls > 1) 'url': 'https://private-storage.example/ready',
            'style_version': memoryArtworkDefaultStyle,
            'enrichment_revision': 'summary-new',
          }),
          200,
        );
      },
    );

    final result = await api.loadForDisplay(
      'memory-new',
      enqueueIfMissing: true,
      pollAttempts: 1,
      pollInterval: Duration.zero,
    );

    expect(result.isReady, isTrue);
    expect(methods, ['GET', 'GET']);
  });

  test('terminal policy response returns immediately without retaining the polling window', () async {
    var calls = 0;
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
      }) async {
        calls++;
        return http.Response(
          jsonEncode({
            'schema_version': memoryArtworkSchemaVersion,
            'status': 'unavailable',
            'failure_code': 'memory_artwork_release_disabled',
          }),
          200,
        );
      },
    );

    final result = await api.loadForDisplay(
      'memory-policy-blocked',
      pollAttempts: 10,
      pollInterval: const Duration(days: 1),
    );

    expect(calls, 1);
    expect(result.status, MemoryArtworkResultStatus.unavailable);
    expect(result.failureCode, 'memory_artwork_release_disabled');
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
        jsonEncode({
          'schema_version': memoryArtworkSchemaVersion,
          'status': 'ready',
          'url': 'http://vendor.invalid/a',
        }),
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
        if (url.endsWith('/memory-artwork/backfill')) {
          return http.Response(
            jsonEncode({
              'schema_version': memoryArtworkSchemaVersion,
              'queued': 3,
              'existing': 7,
              'skipped': 1,
              'has_more': true,
              'next_cursor': 'memory-cursor',
            }),
            200,
          );
        }
        return http.Response('{}', 200);
      },
    );

    expect(
      (await api.setStyle(
        consentVersion: 'ai-data-processors-v10',
        styleVersion: memoryArtworkPaperCollageStyle,
      ))
          .saved,
      isTrue,
    );
    expect(await api.backfillRecent(), isTrue);
    expect(methods, ['PUT', 'POST']);
    expect(urls, [
      'https://api.example/v1/ella/memory-artwork/preferences',
      'https://api.example/v1/ella/memory-artwork/backfill',
    ]);
    expect(jsonDecode(bodies.first)['style_version'], memoryArtworkPaperCollageStyle);
    expect(jsonDecode(bodies.last), isEmpty);
  });

  test('style update exposes a safe typed backend failure', () async {
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
      }) async =>
          http.Response(
        jsonEncode({
          'detail': {'code': 'memory_artwork_consent_required'},
        }),
        409,
      ),
    );

    final result = await api.setStyle(
      consentVersion: 'ai-data-processors-v10',
      styleVersion: memoryArtworkPaperCollageStyle,
    );

    expect(result.saved, isFalse);
    expect(result.failureCode, 'memory_artwork_consent_required');
  });

  test('progressive backfill validates and forwards the opaque cursor', () async {
    var requestBody = '';
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
        requestBody = body;
        return http.Response(
          jsonEncode({
            'schema_version': memoryArtworkSchemaVersion,
            'queued': 10,
            'existing': 12,
            'skipped': 2,
            'has_more': true,
            'next_cursor': 'memory-older-42',
          }),
          200,
        );
      },
    );

    final page = await api.backfillNext(cursor: 'memory-current-42');

    expect(jsonDecode(requestBody), {'cursor': 'memory-current-42'});
    expect(page?.queued, 10);
    expect(page?.existing, 12);
    expect(page?.hasMore, isTrue);
    expect(page?.nextCursor, 'memory-older-42');
    expect(await api.backfillNext(cursor: 'bad/cursor'), isNull);
  });
}
