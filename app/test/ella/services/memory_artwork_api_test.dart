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

  test('published artwork remains ready while a selected style refresh is pending', () async {
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
          'url': 'https://private-storage.example/published',
          'style_version': memoryArtworkDefaultStyle,
          'requested_style_version': memoryArtworkAnimeStorybookStyle,
          'enrichment_revision': 'summary-published',
          'refresh_pending': true,
        }),
        200,
      ),
    );

    final result = await api.fetch('memory-refreshing');

    expect(result.isReady, isTrue);
    expect(result.styleVersion, memoryArtworkDefaultStyle);
    expect(result.requestedStyleVersion, memoryArtworkAnimeStorybookStyle);
    expect(result.refreshPending, isTrue);
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

  test('queue status separates active work from queued, retrying, and failed memories', () async {
    final generationId = 'a' * 64;
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
          'schema_version': 'ella.memory_artwork.queue.v1',
          'generation_id': generationId,
          'style_version': memoryArtworkDefaultStyle,
          'state': 'running',
          'control_state': 'running',
          'scan_status': 'completed',
          'scanned': 166,
          'pages_processed': 4,
          'auto_continue': false,
          'batch_size': 10,
          'batch_remaining': 7,
          'pause_reason': '',
          'ready': 35,
          'active': 1,
          'queued': 128,
          'retrying': 2,
          'failed': 0,
          'total': 166,
          'remaining': 131,
          'updated_at': '2026-08-30T09:49:36Z',
          'styles': [
            {
              'style_version': memoryArtworkDefaultStyle,
              'state': 'running',
              'ready': 35,
              'active': 1,
              'queued': 128,
              'retrying': 2,
              'failed': 0,
              'total': 166,
              'remaining': 131,
            },
            {
              'style_version': memoryArtworkPaperCollageStyle,
              'state': 'paused',
              'ready': 10,
              'active': 0,
              'queued': 5,
              'retrying': 0,
              'failed': 0,
              'total': 15,
              'remaining': 5,
            },
          ],
        }),
        200,
      ),
    );

    final status = await api.queueStatus();

    expect(status?.ready, 35);
    expect(status?.active, 1);
    expect(status?.queued, 128);
    expect(status?.retrying, 2);
    expect(status?.remaining, 131);
    expect(status?.progress, closeTo(35 / 166, 0.0001));
    expect(status?.canPause, isTrue);
    expect(status?.styles.last.styleVersion, memoryArtworkPaperCollageStyle);
    expect(status?.styles.last.state, MemoryArtworkQueueState.paused);
  });

  test('pause is exact-owner authenticated and generation fenced', () async {
    final generationId = 'b' * 64;
    late Map<String, dynamic> requestBody;
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
        expect(url, 'https://api.example/v1/ella/memory-artwork/queue/control');
        expect(method, 'POST');
        expect(requireAuthCheck, isTrue);
        expect(expectedAuthenticatedUid, 'owner-a');
        requestBody = Map<String, dynamic>.from(jsonDecode(body));
        return http.Response(
          jsonEncode({
            'schema_version': 'ella.memory_artwork.queue.v1',
            'generation_id': generationId,
            'style_version': memoryArtworkDefaultStyle,
            'state': 'paused',
            'control_state': 'paused',
            'scan_status': 'pending',
            'scanned': 20,
            'pages_processed': 1,
            'auto_continue': false,
            'batch_size': 10,
            'batch_remaining': 0,
            'pause_reason': 'user_paused',
            'ready': 5,
            'active': 1,
            'queued': 14,
            'retrying': 0,
            'failed': 0,
            'total': 20,
            'remaining': 15,
            'styles': [
              {
                'style_version': memoryArtworkDefaultStyle,
                'state': 'paused',
                'ready': 5,
                'active': 1,
                'queued': 14,
                'retrying': 0,
                'failed': 0,
                'total': 20,
                'remaining': 15,
              },
            ],
          }),
          200,
        );
      },
    );

    final status = await api.controlQueue(action: MemoryArtworkQueueAction.pause, generationId: generationId);

    expect(requestBody, {'action': 'pause', 'generation_id': generationId});
    expect(status?.controlState, MemoryArtworkQueueState.paused);
    expect(status?.canResume, isTrue);
    expect(status?.active, 1, reason: 'the already-active image is allowed to finish');
    expect(await api.controlQueue(action: MemoryArtworkQueueAction.pause, generationId: 'not-a-generation'), isNull);
  });

  test('malformed queue totals fail closed instead of showing false progress', () async {
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
          'schema_version': 'ella.memory_artwork.queue.v1',
          'generation_id': 'c' * 64,
          'style_version': memoryArtworkDefaultStyle,
          'state': 'running',
          'control_state': 'running',
          'scan_status': 'completed',
          'scanned': 1,
          'pages_processed': 1,
          'auto_continue': false,
          'batch_size': 10,
          'batch_remaining': 9,
          'pause_reason': '',
          'ready': 1,
          'active': 0,
          'queued': 1,
          'retrying': 0,
          'failed': 0,
          'total': 99,
          'remaining': 1,
          'styles': [],
        }),
        200,
      ),
    );

    expect(await api.queueStatus(), isNull);
  });

  test('queue status rejects a server batch larger than the client safety limit', () async {
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
          'schema_version': 'ella.memory_artwork.queue.v1',
          'generation_id': 'd' * 64,
          'style_version': memoryArtworkDefaultStyle,
          'state': 'paused',
          'control_state': 'paused',
          'scan_status': 'completed',
          'scanned': 20,
          'pages_processed': 1,
          'auto_continue': false,
          'batch_size': 11,
          'batch_remaining': 0,
          'pause_reason': 'batch_complete',
          'ready': 10,
          'active': 0,
          'queued': 10,
          'retrying': 0,
          'failed': 0,
          'total': 20,
          'remaining': 10,
          'styles': [
            {
              'style_version': memoryArtworkDefaultStyle,
              'state': 'paused',
              'ready': 10,
              'active': 0,
              'queued': 10,
              'retrying': 0,
              'failed': 0,
              'total': 20,
              'remaining': 10,
            },
          ],
        }),
        200,
      ),
    );

    expect(await api.queueStatus(), isNull);
  });
}
