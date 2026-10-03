import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:omi/backend/http/http_pool_manager.dart';
import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/ella/services/memory_artwork_api.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/platform/platform_manager.dart';
import 'package:omi/utils/logger.dart';

class _Authority implements ExactAccountAuthorityVerifier {
  _Authority(this.uid);

  @override
  final String uid;

  bool current = true;

  @override
  bool isExactCurrent() => current;
}

class _ExpiringAuthority implements ExactAccountAuthorityVerifier {
  _ExpiringAuthority(this.uid, {required this.allowedChecks});

  @override
  final String uid;

  final int allowedChecks;
  int checks = 0;

  @override
  bool isExactCurrent() => ++checks <= allowedChecks;
}

http.Response _queueDiagnosticSuccess() => http.Response(
      jsonEncode({
        'schema_version': 'ella.memory_artwork.queue.v1',
        'generation_id': 'a' * 64,
        'style_version': memoryArtworkDefaultStyle,
        'state': 'running',
        'control_state': 'running',
        'scan_status': 'completed',
        'scanned': 0,
        'pages_processed': 0,
        'auto_continue': false,
        'batch_size': 10,
        'batch_remaining': 10,
        'ready': 0,
        'active': 0,
        'queued': 0,
        'retrying': 0,
        'failed': 0,
        'total': 0,
        'remaining': 0,
        'styles': [],
      }),
      200,
    );

MemoryArtworkApi _queueDiagnosticApi(
  Future<http.Response?> Function() response,
  void Function(MemoryArtworkQueueReadDiagnostic) observer, {
  MemoryArtworkAuthorityProvider? authorityProvider,
}) =>
    MemoryArtworkApi(
      baseUrl: 'https://private-fixture.example',
      authorityProvider: authorityProvider ?? () => _Authority('private-fixture-owner'),
      onQueueReadDiagnostic: observer,
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
        onSendAttempt,
      }) async {
        expect(method, 'GET');
        expect(body, isEmpty);
        expect(timeout, const Duration(seconds: 30));
        expect(retries, 0);
        expect(requireAuthCheck, isTrue);
        expect(exactAuthority, isNotNull);
        return response();
      },
    );

void main() {
  PlatformManager.initializeForTesting();
  setUp(MemoryArtworkQueueDiagnostics.clear);
  tearDown(MemoryArtworkQueueDiagnostics.clear);

  for (final entry in <(http.Response?, MemoryArtworkQueueReadOutcome)>[
    (_queueDiagnosticSuccess(), MemoryArtworkQueueReadOutcome.success),
    (http.Response('private invalid payload', 200), MemoryArtworkQueueReadOutcome.schema),
    (null, MemoryArtworkQueueReadOutcome.noResponse),
  ]) {
    test('supported queue snapshot separates ${entry.$2.name} from Home projection', () async {
      final ticket = MemoryArtworkQueueDiagnostics.begin(isCurrent: () => true);
      final api = _queueDiagnosticApi(() async => entry.$1, (_) {});
      expect(MemoryArtworkQueueDiagnostics.latest?.read, isNull);
      final result = await api.queueStatusWithDiagnostics(ticket);
      final snapshot = MemoryArtworkQueueDiagnostics.latest!;
      expect(snapshot.read?.outcome, entry.$2);
      expect(snapshot.projection, MemoryArtworkQueueProjection.pending);
      expect(snapshot.read?.elapsedMilliseconds, inInclusiveRange(0, 120000));
      MemoryArtworkQueueDiagnostics.project(ticket, applied: result != null);
      expect(MemoryArtworkQueueDiagnostics.latest?.projection,
          result == null ? MemoryArtworkQueueProjection.failed : MemoryArtworkQueueProjection.applied);
      expect(snapshot.read?.message, isNot(contains('private')));
    });
  }

  test('supported queue snapshot preserves timeout and observer exception contracts', () async {
    final ticket = MemoryArtworkQueueDiagnostics.begin(isCurrent: () => true);
    final error = TimeoutException('private timeout');
    final api = _queueDiagnosticApi(() async => throw error, (_) => throw StateError('private observer'));
    await expectLater(api.queueStatusWithDiagnostics(ticket), throwsA(same(error)));
    expect(MemoryArtworkQueueDiagnostics.latest?.read?.outcome, MemoryArtworkQueueReadOutcome.timeout);
    MemoryArtworkQueueDiagnostics.project(ticket, applied: false);
    expect(MemoryArtworkQueueDiagnostics.latest?.projection, MemoryArtworkQueueProjection.failed);
  });

  test('supported queue tickets cannot collide across API instances or completion order', () async {
    final firstResponse = Completer<http.Response?>();
    final first = _queueDiagnosticApi(() => firstResponse.future, (_) {});
    final second = _queueDiagnosticApi(() async => http.Response('private forbidden', 403), (_) {});
    final firstTicket = MemoryArtworkQueueDiagnostics.begin(isCurrent: () => true);
    final pending = first.queueStatusWithDiagnostics(firstTicket);
    final secondTicket = MemoryArtworkQueueDiagnostics.begin(isCurrent: () => true);
    await second.queueStatusWithDiagnostics(secondTicket);
    MemoryArtworkQueueDiagnostics.project(secondTicket, applied: false);
    final current = MemoryArtworkQueueDiagnostics.latest;
    expect(current?.read?.readNumber, 1);
    expect(current?.read?.statusCode, 403);
    firstResponse.complete(_queueDiagnosticSuccess());
    expect(await pending, isNotNull);
    MemoryArtworkQueueDiagnostics.project(firstTicket, applied: true);
    expect(MemoryArtworkQueueDiagnostics.latest, same(current));
  });

  test('supported queue read retires on epoch change and cannot repopulate after account ABA', () async {
    var epoch = 1;
    var owner = 'fixture-a';
    final response = Completer<http.Response?>();
    final ticket = MemoryArtworkQueueDiagnostics.begin(isCurrent: () => owner == 'fixture-a' && epoch == 1);
    final api = _queueDiagnosticApi(() => response.future, (_) {});
    final pending = api.queueStatusWithDiagnostics(ticket);
    owner = 'fixture-b';
    epoch++;
    expect(MemoryArtworkQueueDiagnostics.latest, isNull);
    owner = 'fixture-a';
    epoch++;
    response.complete(_queueDiagnosticSuccess());
    expect(await pending, isNotNull);
    MemoryArtworkQueueDiagnostics.project(ticket, applied: true);
    expect(MemoryArtworkQueueDiagnostics.latest, isNull);
  });

  test('supported queue clear refuses late finally and old projection after replacement', () async {
    final response = Completer<http.Response?>();
    final ticket = MemoryArtworkQueueDiagnostics.begin(isCurrent: () => true);
    final api = _queueDiagnosticApi(() => response.future, (_) {});
    final pending = api.queueStatusWithDiagnostics(ticket);
    MemoryArtworkQueueDiagnostics.clear();
    final replacement = MemoryArtworkQueueDiagnostics.begin(isCurrent: () => true);
    response.complete(_queueDiagnosticSuccess());
    await pending;
    MemoryArtworkQueueDiagnostics.project(ticket, applied: true);
    expect(MemoryArtworkQueueDiagnostics.latest?.read, isNull);
    expect(MemoryArtworkQueueDiagnostics.latest?.projection, MemoryArtworkQueueProjection.pending);
    MemoryArtworkQueueDiagnostics.project(replacement, applied: false);
    expect(MemoryArtworkQueueDiagnostics.latest?.projection, MemoryArtworkQueueProjection.failed);
  });

  test('supported diagnostics guard failure never changes API result', () async {
    final ticket = MemoryArtworkQueueDiagnostics.begin(isCurrent: () => throw StateError('private guard'));
    final api = _queueDiagnosticApi(() async => _queueDiagnosticSuccess(), (_) {});
    expect(await api.queueStatusWithDiagnostics(ticket), isNotNull);
    expect(MemoryArtworkQueueDiagnostics.latest, isNull);
  });

  for (final response in <http.Response?>[null, http.Response('{"private":"fixture"}', 200)]) {
    test('queue read emits content-free evidence for ${response == null ? 'no response' : 'invalid metadata'}',
        () async {
      final initialLogs = Logger.instance.talker.history.length;
      var requests = 0;
      final api = MemoryArtworkApi(
        baseUrl: 'https://api.example',
        authorityProvider: () => _Authority('private-fixture-owner'),
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
          onSendAttempt,
        }) async {
          requests++;
          expect(method, 'GET');
          expect(body, isEmpty);
          expect(timeout, const Duration(seconds: 30));
          expect(retries, 0);
          return response;
        },
      );
      expect(await api.queueStatus(), isNull);
      expect(requests, 1);
      final messages = Logger.instance.talker.history
          .skip(initialLogs)
          .map((entry) => entry.message ?? '')
          .where((message) => message.startsWith('[MemoryArtworkQueue]'))
          .toList();
      expect(messages, hasLength(1));
      expect(messages.single, contains('outcome=${response == null ? 'no_response' : 'schema'}'));
      expect(messages.single, isNot(contains('private')));
      expect(messages.single, isNot(contains('https')));
    });
  }

  for (final entry in <(http.Response?, MemoryArtworkQueueReadOutcome, int?)>[
    (null, MemoryArtworkQueueReadOutcome.noResponse, null),
    (http.Response('private body', 401), MemoryArtworkQueueReadOutcome.authentication, 401),
    (http.Response('private body', 403), MemoryArtworkQueueReadOutcome.non200, 403),
    (http.Response('private body', 500), MemoryArtworkQueueReadOutcome.non200, 500),
    (http.Response('private body', 999), MemoryArtworkQueueReadOutcome.non200, null),
    (http.Response('private invalid JSON', 200), MemoryArtworkQueueReadOutcome.schema, 200),
    (_queueDiagnosticSuccess(), MemoryArtworkQueueReadOutcome.success, 200),
  ]) {
    test('queue diagnostic reports only fixed response evidence ${entry.$2.name} ${entry.$3}', () async {
      final events = <MemoryArtworkQueueReadDiagnostic>[];
      var requests = 0;
      final api = _queueDiagnosticApi(() async {
        requests++;
        return entry.$1;
      }, events.add);
      final result = await api.queueStatus();
      expect(result != null, entry.$2 == MemoryArtworkQueueReadOutcome.success);
      expect(requests, 1);
      expect(events, hasLength(1));
      final event = events.single;
      expect(event.outcome, entry.$2);
      expect(event.statusCode, entry.$3);
      expect(event.readNumber, 1);
      expect(event.elapsedMilliseconds, inInclusiveRange(0, 120000));
      expect(event.message, isNot(contains('private')));
      expect(event.message, isNot(contains('https')));
      expect(event.message, isNot(contains('999')));
    });
  }

  for (final entry in <(Object, MemoryArtworkQueueReadOutcome)>[
    (TimeoutException('private timeout'), MemoryArtworkQueueReadOutcome.timeout),
    (ExactAccountAuthorityChangedException('private authority'), MemoryArtworkQueueReadOutcome.authentication),
    (
      const ClientApiFailure(ClientApiFailureKind.authenticationRequired, backendCode: 'private-code'),
      MemoryArtworkQueueReadOutcome.authentication
    ),
    (const ClientApiFailure(ClientApiFailureKind.accountChanged), MemoryArtworkQueueReadOutcome.authentication),
    (const ClientApiFailure(ClientApiFailureKind.forbidden), MemoryArtworkQueueReadOutcome.internal),
    (
      http.ClientException('private transport', Uri.parse('https://private-fixture.example')),
      MemoryArtworkQueueReadOutcome.noResponse
    ),
    (StateError('private internal'), MemoryArtworkQueueReadOutcome.internal),
  ]) {
    test('queue diagnostic preserves thrown exception ${entry.$1.runtimeType} ${entry.$2.name}', () async {
      final events = <MemoryArtworkQueueReadDiagnostic>[];
      var requests = 0;
      final api = _queueDiagnosticApi(() async {
        requests++;
        throw entry.$1;
      }, events.add);
      await expectLater(api.queueStatus(), throwsA(same(entry.$1)));
      expect(requests, 1);
      expect(events, hasLength(1));
      expect(events.single.outcome, entry.$2);
      expect(events.single.statusCode, isNull);
      expect(events.single.message, isNot(contains('private')));
      expect(events.single.message, isNot(contains(entry.$1.runtimeType.toString())));
    });
  }

  test('queue diagnostic admission failure has zero requests and stale completion stays unavailable', () async {
    final events = <MemoryArtworkQueueReadDiagnostic>[];
    var requests = 0;
    final authority = _Authority('private-fixture-owner');
    ExactAccountAuthorityVerifier? current;
    final api = _queueDiagnosticApi(() async {
      requests++;
      authority.current = false;
      return _queueDiagnosticSuccess();
    }, events.add, authorityProvider: () => current);
    expect(await api.queueStatus(), isNull);
    expect(requests, 0);
    current = authority;
    expect(await api.queueStatus(), isNull);
    expect(requests, 1);
    expect(events.map((event) => event.readNumber), [1, 2]);
    expect(events.every((event) => event.outcome == MemoryArtworkQueueReadOutcome.authentication), isTrue);
  });

  test('queue diagnostic completion ordering stays bound to each read', () async {
    final responses = [Completer<http.Response?>(), Completer<http.Response?>()];
    final events = <MemoryArtworkQueueReadDiagnostic>[];
    var requests = 0;
    final api = _queueDiagnosticApi(() => responses[requests++].future, events.add);
    final first = api.queueStatus();
    final second = api.queueStatus();
    responses[1].complete(http.Response('private body', 403));
    expect(await second, isNull);
    responses[0].complete(_queueDiagnosticSuccess());
    expect(await first, isNotNull);
    expect(requests, 2);
    expect(events.map((event) => event.readNumber), [2, 1]);
    expect(events.map((event) => event.statusCode), [403, 200]);
    expect(events.map((event) => event.outcome),
        [MemoryArtworkQueueReadOutcome.non200, MemoryArtworkQueueReadOutcome.success]);
  });

  test('queue diagnostic observer failure cannot change results or rethrown exceptions', () async {
    final error = StateError('private request failure');
    var requests = 0;
    var observations = 0;
    final api = _queueDiagnosticApi(() async {
      requests++;
      if (requests == 1) return _queueDiagnosticSuccess();
      if (requests == 2) return null;
      throw error;
    }, (_) {
      observations++;
      throw StateError('private observer failure');
    });
    expect(await api.queueStatus(), isNotNull);
    expect(await api.queueStatus(), isNull);
    await expectLater(api.queueStatus(), throwsA(same(error)));
    expect(requests, 3);
    expect(observations, 3);
  });

  test('makeApiCall reports the mutation boundary only immediately before real HTTP egress', () async {
    var sendBoundaryCalls = 0;
    var clientCalls = 0;
    final expiringAuthority = _ExpiringAuthority('owner-a', allowedChecks: 2);
    HttpPoolManager.instance.replaceClientForTesting(
      MockClient((_) async {
        clientCalls++;
        return http.Response('{}', 200);
      }),
    );

    await expectLater(
      makeApiCall(
        url: 'https://api.example/v1/ella/memories/memory-a/artwork',
        headers: const {},
        body: '{}',
        method: 'POST',
        retries: 0,
        requireAuthCheck: false,
        exactAuthority: expiringAuthority,
        onSendAttempt: () => sendBoundaryCalls++,
      ),
      throwsA(isA<ExactAccountAuthorityChangedException>()),
    );
    expect(sendBoundaryCalls, 0, reason: 'authority/header failure before egress must not consume a claim');
    expect(clientCalls, 0);

    sendBoundaryCalls = 0;
    clientCalls = 0;
    HttpPoolManager.instance.replaceClientForTesting(
      MockClient((request) async {
        clientCalls++;
        throw http.ClientException('response lost after send', request.url);
      }),
    );

    final result = await makeApiCall(
      url: 'https://api.example/v1/ella/memories/memory-a/artwork',
      headers: const {},
      body: '{}',
      method: 'POST',
      retries: 0,
      requireAuthCheck: false,
      exactAuthority: _Authority('owner-a'),
      onSendAttempt: () => sendBoundaryCalls++,
    );

    expect(result, isNull);
    expect(sendBoundaryCalls, 1, reason: 'post-egress response loss must leave the mutation claim committed');
    expect(clientCalls, 1);
  });

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
        onSendAttempt,
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
            onSendAttempt,
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

  test('day artwork coalesces by revision and selects the smallest sufficient stable variant', () async {
    final authority = _Authority('owner-a');
    final requestedUrls = <String>[];
    var signedRevision = 0;
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
        onSendAttempt,
      }) async {
        requestedUrls.add(url);
        signedRevision += 1;
        expect(method, 'GET');
        expect(expectedAuthenticatedUid, 'owner-a');
        expect(exactAuthority, same(authority));
        return http.Response(
          jsonEncode({
            'schema_version': memoryArtworkSchemaVersion,
            'day': '2026-09-24',
            'utc_offset_minutes': -420,
            'items': [
              {
                'memory_id': 'memory-a',
                'artwork': {
                  'schema_version': memoryArtworkSchemaVersion,
                  'status': 'ready',
                  'url': 'https://private-storage.example/master?signature=$signedRevision',
                  'cache_key': 'stable-object-version',
                  'pixel_width': 1536,
                  'style_version': memoryArtworkDefaultStyle,
                  'enrichment_revision': 'summary-a',
                  'variants': [
                    {
                      'w': 1536,
                      'url': 'https://private-storage.example/1536?signature=$signedRevision',
                      'bytes': 15360,
                    },
                    {
                      'w': 384,
                      'url': 'https://private-storage.example/384?signature=$signedRevision',
                      'bytes': 3840,
                    },
                    {
                      'w': 768,
                      'url': 'https://private-storage.example/768?signature=$signedRevision',
                      'bytes': 7680,
                    },
                  ],
                },
              },
            ],
          }),
          200,
        );
      },
    );

    final firstRequest = api.fetchDay(
      DateTime(2026, 9, 24),
      utcOffsetMinutes: -420,
      authorityRevision: 7,
      contentRevision: 11,
    );
    final coalescedRequest = api.fetchDay(
      DateTime(2026, 9, 24),
      utcOffsetMinutes: -420,
      authorityRevision: 7,
      contentRevision: 11,
    );
    expect(coalescedRequest, same(firstRequest));

    final first = (await firstRequest)!.items['memory-a']!.forPhysicalWidth(500);
    final renewedAtSameRevision = (await api.fetchDay(
      DateTime(2026, 9, 24),
      utcOffsetMinutes: -420,
      authorityRevision: 7,
      contentRevision: 11,
    ))!
        .items['memory-a']!
        .forPhysicalWidth(500);
    final renewedAtNextRevision = (await api.fetchDay(
      DateTime(2026, 9, 24),
      utcOffsetMinutes: -420,
      authorityRevision: 7,
      contentRevision: 12,
    ))!
        .items['memory-a']!
        .forPhysicalWidth(500);

    expect(requestedUrls, [
      'https://api.example/v1/ella/memory-artwork/day/2026-09-24?utc_offset_minutes=-420',
      'https://api.example/v1/ella/memory-artwork/day/2026-09-24?utc_offset_minutes=-420',
      'https://api.example/v1/ella/memory-artwork/day/2026-09-24?utc_offset_minutes=-420',
    ]);
    expect(first.selectedVariantWidth, 768);
    expect(first.url, Uri.parse('https://private-storage.example/768?signature=1'));
    expect(renewedAtSameRevision.url, Uri.parse('https://private-storage.example/768?signature=2'));
    expect(renewedAtNextRevision.url, Uri.parse('https://private-storage.example/768?signature=3'));
    expect(
      renewedAtNextRevision.cacheKey,
      first.cacheKey,
      reason: 'signed URL renewal must not invalidate saved artwork',
    );
  });

  for (final status in [404, 403, 200]) {
    test('day artwork GET returns no batch for ${status == 200 ? 'malformed JSON' : status}', () async {
      final authority = _Authority('owner-a');
      final methods = <String>[];
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
          onSendAttempt,
        }) async {
          methods.add(method);
          expect(expectedAuthenticatedUid, authority.uid);
          expect(exactAuthority, same(authority));
          return http.Response(status == 200 ? '{invalid' : '{}', status);
        },
      );

      expect(await api.fetchDay(DateTime(2026, 9, 24), utcOffsetMinutes: 0), isNull);
      expect(methods, ['GET']);
    });
  }

  test('recent recovery accepts only the bounded authenticated server contract', () async {
    final authority = _Authority('owner-a');
    late String requestedUrl;
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
        onSendAttempt,
      }) async {
        requestedUrl = url;
        expect(method, 'POST');
        expect(body, isEmpty);
        expect(timeout, const Duration(seconds: 30));
        expect(requireAuthCheck, isTrue);
        expect(expectedAuthenticatedUid, 'owner-a');
        expect(exactAuthority, same(authority));
        return http.Response(
          jsonEncode({
            'schema_version': memoryArtworkRecentRecoverySchemaVersion,
            'scanned': 8,
            'reservation_limit': 10,
            'reserved': 2,
            'deferred': 1,
            'ready': 2,
            'pending': 2,
            'retrying': 1,
            'exhausted': 0,
            'skipped': 2,
            'items': [
              {'memory_id': 'memory-a', 'status': 'pending'},
              {'memory_id': 'memory-b', 'status': 'retrying'},
            ],
          }),
          202,
        );
      },
    );

    final recovery = await api.recoverRecent();

    expect(requestedUrl, 'https://api.example/v1/ella/memory-artwork/recovery/recent');
    expect(recovery, isNotNull);
    expect(recovery!.scanned, 8);
    expect(recovery.reserved, 2);
    expect(recovery.pending, 2);
    expect(recovery.retrying, 1);
    expect(recovery.hasDisplayableOrActiveArtwork, isTrue);
  });

  test('recent recovery fails closed for inconsistent counts and stale authority', () async {
    final malformedAuthority = _Authority('owner-a');
    final malformed = MemoryArtworkApi(
      baseUrl: 'https://api.example/',
      authorityProvider: () => malformedAuthority,
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
        onSendAttempt,
      }) async =>
          http.Response(
        jsonEncode({
          'schema_version': memoryArtworkRecentRecoverySchemaVersion,
          'scanned': 2,
          'reservation_limit': 10,
          'reserved': 1,
          'deferred': 0,
          'ready': 0,
          'pending': 1,
          'retrying': 0,
          'exhausted': 0,
          'skipped': 0,
          'items': [
            {'memory_id': 'memory-a', 'status': 'pending'},
          ],
        }),
        202,
      ),
    );
    expect(await malformed.recoverRecent(), isNull, reason: 'the server totals do not account for every scan');

    final staleAuthority = _Authority('owner-a');
    final stale = MemoryArtworkApi(
      baseUrl: 'https://api.example/',
      authorityProvider: () => staleAuthority,
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
        onSendAttempt,
      }) async {
        staleAuthority.current = false;
        return http.Response(
          jsonEncode({
            'schema_version': memoryArtworkRecentRecoverySchemaVersion,
            'scanned': 0,
            'reservation_limit': 10,
            'reserved': 0,
            'deferred': 0,
            'ready': 0,
            'pending': 0,
            'retrying': 0,
            'exhausted': 0,
            'skipped': 0,
            'items': [],
          }),
          202,
        );
      },
    );
    expect(await stale.recoverRecent(), isNull);
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
        onSendAttempt,
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

  test('automatic generation identity is stable by owner, memory, and source revision', () {
    final ownerA = _Authority('owner-a');
    final ownerAApi = MemoryArtworkApi(authorityProvider: () => ownerA);
    final ownerBApi = MemoryArtworkApi(authorityProvider: () => _Authority('owner-b'));

    final first = ownerAApi.automaticGenerationKey(memoryId: 'memory-a', sourceRevision: 'summary-a');
    final repeated = ownerAApi.automaticGenerationKey(memoryId: 'memory-a', sourceRevision: 'summary-a');

    expect(first, hasLength(64));
    expect(repeated, first);
    expect(ownerAApi.automaticGenerationKey(memoryId: 'memory-a', sourceRevision: 'summary-b'), isNot(first));
    expect(ownerBApi.automaticGenerationKey(memoryId: 'memory-a', sourceRevision: 'summary-a'), isNot(first));
    ownerA.current = false;
    expect(ownerAApi.automaticGenerationKey(memoryId: 'memory-a', sourceRevision: 'summary-a'), isEmpty);
  });

  test('automatic visible-card generation is identified separately from manual retry', () async {
    final requestBodies = <Map<String, dynamic>>[];
    var reads = 0;
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
        onSendAttempt,
      }) async {
        if (method == 'POST') {
          requestBodies.add(jsonDecode(body) as Map<String, dynamic>);
          return http.Response(
            jsonEncode({'outcome': 'automatic_attempt_already_used', 'status': 'unavailable'}),
            200,
          );
        }
        reads += 1;
        return http.Response(
          jsonEncode({'schema_version': memoryArtworkSchemaVersion, 'status': 'unavailable'}),
          200,
        );
      },
    );

    await api.loadAutomaticallyForDisplay('memory-auto', pollAttempts: 0);
    await api.loadForDisplay('memory-manual', enqueueIfMissing: true, pollAttempts: 0);

    expect(reads, 4);
    expect(requestBodies, [
      {'request_mode': 'automatic'},
      {'request_mode': 'manual'},
    ]);
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
        onSendAttempt,
      }) async {
        methods.add(method);
        if (method == 'POST') {
          return http.Response(jsonEncode({'outcome': 'queued', 'status': 'generating'}), 202);
        }
        getCalls += 1;
        if (getCalls <= 2) {
          return http.Response(
            jsonEncode({'schema_version': memoryArtworkSchemaVersion, 'status': 'unavailable'}),
            200,
          );
        }
        return http.Response(
          jsonEncode({
            'schema_version': memoryArtworkSchemaVersion,
            'status': getCalls == 3 ? 'generating' : 'ready',
            if (getCalls > 3) 'url': 'https://private-storage.example/lazy-ready',
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
    expect(methods, ['GET', 'GET', 'POST', 'GET', 'GET']);
    expect(result.cacheKey, hasLength(64));
  });

  test('ready enqueue race is resolved through an authenticated signed artwork read', () async {
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
        onSendAttempt,
      }) async {
        methods.add(method);
        if (method == 'POST') {
          return http.Response(jsonEncode({'outcome': 'existing', 'status': 'ready'}), 200);
        }
        getCalls += 1;
        if (getCalls <= 2) {
          return http.Response(
            jsonEncode({'schema_version': memoryArtworkSchemaVersion, 'status': 'unavailable'}),
            200,
          );
        }
        return http.Response(
          jsonEncode({
            'schema_version': memoryArtworkSchemaVersion,
            'status': 'ready',
            'url': 'https://private-storage.example/race-ready',
            'style_version': memoryArtworkDefaultStyle,
            'enrichment_revision': 'summary-race',
          }),
          200,
        );
      },
    );

    final result = await api.loadForDisplay('memory-race', enqueueIfMissing: true, pollAttempts: 0);

    expect(methods, ['GET', 'GET', 'POST', 'GET']);
    expect(result.isReady, isTrue);
    expect(result.url, Uri.parse('https://private-storage.example/race-ready'));
    expect(result.cacheKey, hasLength(64));
  });

  test('generation rechecks policy immediately before POST and blocks raw terminal states', () async {
    const terminalCodes = {
      'deletion_pending',
      'authority_changed',
      'preference_changed',
      'source_changed',
      'prompt_changed',
      'job_claim_invalid',
    };

    for (final terminalCode in terminalCodes) {
      final authority = _Authority('owner-a');
      final methods = <String>[];
      var reads = 0;
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
          onSendAttempt,
        }) async {
          methods.add(method);
          reads += 1;
          return http.Response(
            jsonEncode({
              'schema_version': memoryArtworkSchemaVersion,
              'status': 'unavailable',
              if (reads > 1) 'failure_code': terminalCode,
            }),
            200,
          );
        },
      );

      final result = await api.loadForDisplay('memory-policy-race', enqueueIfMissing: true);

      expect(result.failureCode, terminalCode);
      expect(methods, ['GET', 'GET'], reason: '$terminalCode must be rejected before generation');
    }
  });

  test('generation canonicalizes a policy change returned by the POST', () async {
    const policyOutcomes = {
      'disabled': 'memory_artwork_release_disabled',
      'consent_required': 'memory_artwork_consent_required',
      'not_found': 'memory_artwork_memory_not_found',
      'sensitive_source_excluded': 'memory_artwork_sensitive_source_excluded',
      'source_changed': 'memory_artwork_source_stale',
    };

    for (final outcome in policyOutcomes.entries) {
      final methods = <String>[];
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
          onSendAttempt,
        }) async {
          methods.add(method);
          return http.Response(
            jsonEncode({
              'schema_version': memoryArtworkSchemaVersion,
              'status': 'unavailable',
              if (method == 'POST') 'outcome': outcome.key,
            }),
            200,
          );
        },
      );

      final result = await api.loadForDisplay('memory-post-policy-race', enqueueIfMissing: true);

      expect(methods, ['GET', 'GET', 'POST']);
      expect(result.failureCode, outcome.value);
      expect(result.canRequestGeneration, isFalse);
    }
  });

  test('unknown unavailable state fails closed without a generation POST', () async {
    final methods = <String>[];
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
        onSendAttempt,
      }) async {
        methods.add(method);
        return http.Response(
          jsonEncode({
            'schema_version': memoryArtworkSchemaVersion,
            'status': 'unavailable',
            'failure_code': 'new_server_policy_state',
          }),
          200,
        );
      },
    );

    final result = await api.loadForDisplay('memory-unknown-policy', enqueueIfMissing: true);

    expect(result.failureCode, 'new_server_policy_state');
    expect(methods, ['GET']);
  });

  test('missing or unknown artwork status fails closed without a generation POST', () async {
    for (final rawStatus in <String?>[null, 'future_server_state']) {
      final methods = <String>[];
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
          onSendAttempt,
        }) async {
          methods.add(method);
          return http.Response(
            jsonEncode({'schema_version': memoryArtworkSchemaVersion, if (rawStatus != null) 'status': rawStatus}),
            200,
          );
        },
      );

      final result = await api.loadForDisplay('memory-malformed-status', enqueueIfMissing: true);

      expect(result.failureCode, 'memory_artwork_response_invalid');
      expect(methods, ['GET'], reason: '$rawStatus must not become generation-eligible');
    }
  });

  test('automatic load polls already-generating artwork without duplicate enqueue', () async {
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
        onSendAttempt,
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

    final result = await api.loadAutomaticallyForDisplay('memory-new', pollAttempts: 1, pollInterval: Duration.zero);

    expect(result.isReady, isTrue);
    expect(methods, ['GET', 'GET']);
  });

  test('manual retry reconciles stale generating state through the server', () async {
    for (final terminalJob in const ['failed', 'completed']) {
      final methods = <String>[];
      final requestBodies = <Map<String, dynamic>>[];
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
          onSendAttempt,
        }) async {
          methods.add(method);
          if (method == 'POST') {
            requestBodies.add(jsonDecode(body) as Map<String, dynamic>);
            return http.Response(
              jsonEncode({'outcome': 'requeued_terminal_$terminalJob', 'status': 'generating'}),
              202,
            );
          }
          return http.Response(
            jsonEncode({
              'schema_version': memoryArtworkSchemaVersion,
              'status': 'generating',
              'style_version': memoryArtworkDefaultStyle,
              'enrichment_revision': 'summary-$terminalJob',
            }),
            200,
          );
        },
      );

      final result = await api.loadForDisplay('memory-terminal-$terminalJob', enqueueIfMissing: true, pollAttempts: 0);

      expect(result.status, MemoryArtworkResultStatus.generating, reason: terminalJob);
      expect(methods, ['GET', 'GET', 'POST'], reason: terminalJob);
      expect(
          requestBodies,
          [
            {'request_mode': 'manual'},
          ],
          reason: terminalJob);
    }
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
        onSendAttempt,
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
        onSendAttempt,
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
        onSendAttempt,
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
        onSendAttempt,
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
        onSendAttempt,
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
              'mode': 'preview',
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
    expect(jsonDecode(bodies.last), {'mode': 'preview'});
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
        onSendAttempt,
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

  test('artwork libraries report only server-confirmed ready days and illustrations', () async {
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
        onSendAttempt,
      }) async {
        expect(url, 'https://api.example/v1/ella/memory-artwork/libraries');
        expect(method, 'GET');
        expect(timeout, const Duration(seconds: 30));
        return http.Response(
          jsonEncode({
            'schema_version': memoryArtworkLibrariesSchemaVersion,
            'selected_style_version': memoryArtworkAnimeStorybookStyle,
            'default_preview_days': 3,
            'historical_batch_size': 10,
            'libraries': [
              {
                'style_version': memoryArtworkAnimeStorybookStyle,
                'selected': true,
                'ready_memories': 18,
                'ready_days': 4,
                'oldest_day': '2026-08-27',
                'newest_day': '2026-08-30',
              },
              {'style_version': memoryArtworkDefaultStyle, 'selected': false, 'ready_memories': 0, 'ready_days': 0},
            ],
          }),
          200,
        );
      },
    );

    final result = await api.libraries();

    expect(result?.selectedStyleVersion, memoryArtworkAnimeStorybookStyle);
    expect(result?.defaultPreviewDays, 3);
    expect(result?.historicalBatchSize, 10);
    expect(result?.forStyle(memoryArtworkAnimeStorybookStyle)?.readyMemories, 18);
    expect(result?.forStyle(memoryArtworkAnimeStorybookStyle)?.readyDays, 4);
    expect(result?.forStyle(memoryArtworkAnimeStorybookStyle)?.newestDay, DateTime.utc(2026, 8, 30));
    expect(result?.forStyle(memoryArtworkDefaultStyle)?.readyMemories, 0);
  });

  test('preview backfill validates and forwards the opaque cursor', () async {
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
        onSendAttempt,
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
            'mode': 'preview',
          }),
          200,
        );
      },
    );

    final page = await api.backfillNext(cursor: 'memory-current-42');

    expect(jsonDecode(requestBody), {'mode': 'preview', 'cursor': 'memory-current-42'});
    expect(page?.queued, 10);
    expect(page?.existing, 12);
    expect(page?.hasMore, isTrue);
    expect(page?.nextCursor, 'memory-older-42');
    expect(await api.backfillNext(cursor: 'bad/cursor'), isNull);
  });

  test('backfill rejects an older server response that does not confirm the requested mode', () async {
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
        onSendAttempt,
      }) async =>
          http.Response(
        jsonEncode({
          'schema_version': memoryArtworkSchemaVersion,
          'queued': 10,
          'existing': 0,
          'skipped': 0,
          'has_more': true,
          'next_cursor': 'memory-older-42',
        }),
        200,
      ),
    );

    expect(await api.backfillNext(), isNull);
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
        onSendAttempt,
      }) async {
        expect(timeout, const Duration(seconds: 30));
        return http.Response(
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
        );
      },
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
        onSendAttempt,
      }) async {
        expect(url, 'https://api.example/v1/ella/memory-artwork/queue/control');
        expect(method, 'POST');
        expect(timeout, const Duration(seconds: 30));
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

  test('paused live queue payload keeps its remaining style batch actionable', () async {
    final generationId = 'c' * 64;
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
        onSendAttempt,
      }) async =>
          http.Response(
        jsonEncode({
          'schema_version': 'ella.memory_artwork.queue.v1',
          'generation_id': generationId,
          'style_version': memoryArtworkAnimeStorybookStyle,
          'state': 'paused',
          'control_state': 'paused',
          'scan_status': 'pending',
          'scanned': 668,
          'pages_processed': 83,
          'auto_continue': false,
          'batch_size': 10,
          'batch_remaining': 0,
          'pause_reason': 'batch_complete',
          'ready': 527,
          'active': 0,
          'queued': 26,
          'retrying': 0,
          'failed': 0,
          'total': 553,
          'remaining': 26,
          'updated_at': '2026-08-30T23:00:00Z',
          'styles': const [],
        }),
        200,
      ),
    );

    final status = await api.queueStatus();

    expect(status?.styleVersion, memoryArtworkAnimeStorybookStyle);
    expect(status?.controlState, MemoryArtworkQueueState.paused);
    expect(status?.pauseReason, 'batch_complete');
    expect(status?.ready, 527);
    expect(status?.remaining, 26);
    expect(status?.canResume, isTrue);
    expect(status?.canCancel, isTrue);
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
        onSendAttempt,
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
        onSendAttempt,
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
