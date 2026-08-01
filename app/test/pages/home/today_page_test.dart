import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/action_item.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/today_card.dart';
import 'package:omi/ella/services/today_card_controller.dart';
import 'package:omi/ella/services/today_card_repository.dart';
import 'package:omi/ella/widgets/today_card_surface.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/home/today_page.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('selects only incomplete upcoming reminders due today', () {
    final now = DateTime(2026, 7, 19, 10);
    final items = [
      ActionItemWithMetadata(id: 'today', description: 'Call Greg', completed: false, dueAt: DateTime(2026, 7, 19, 11)),
      ActionItemWithMetadata(
        id: 'completed',
        description: 'Already done',
        completed: true,
        dueAt: DateTime(2026, 7, 19, 12),
      ),
      ActionItemWithMetadata(
        id: 'tomorrow',
        description: 'Tomorrow',
        completed: false,
        dueAt: DateTime(2026, 7, 20, 9),
      ),
    ];

    expect(todayUpcomingReminders(items, now).map((item) => item.id), ['today']);
  });

  test('action item source labels survive API parsing', () {
    final item = ActionItemWithMetadata.fromJson({
      'id': 'from-david',
      'description': 'Dinner with David',
      'completed': false,
      'source_label': 'David',
    });

    expect(item.sourceLabel, 'David');
    expect(item.toJson()['source_label'], 'David');
  });

  test('Talk about this launches with daily-card identifiers only', () {
    final card = TodayCard(
      id: 'today-card-42',
      version: 3,
      kind: TodayCardKind.memory,
      eyebrow: 'A MEMORY FROM JUNE 12',
      headline: 'The roses along Elm Street',
      body: 'You enjoyed the long walk home with Rose.',
      generatedAt: DateTime.utc(2026, 7, 31),
      sourceRefs: const [TodayCardSourceRef(kind: 'memory', id: 'memory-1', versionId: 'v4')],
    );

    expect(TodayPage.sessionScopeFor(card).toJson(), {
      'kind': 'daily_card',
      'card_id': 'today-card-42',
      'expected_version': 3,
    });
  });

  group('Today card v1 transport', () {
    const readyEnvelope = {
      'contract_version': 'ella.today_card.v1',
      'state': 'ready',
      'card': {
        'card_id': '2265689d-e0d7-4a26-bdeb-2c8c97e90b89',
        'version': 3,
        'local_date': '2026-08-01',
        'timezone': 'America/Los_Angeles',
        'kind': 'recap',
        'eyebrow': 'A NOTE FROM YESTERDAY',
        'headline': 'The roses along Elm Street',
        'body': 'You enjoyed the long walk home with Rose.',
        'spoken_text': 'The roses along Elm Street. You enjoyed the long walk home with Rose.',
        'source_date': '2026-07-31',
        'source_refs': [
          {
            'source_type': 'conversation_summary',
            'source_id': 'conversation-a',
            'source_version_id': 'summary-v3',
            'occurred_at': '2026-07-31T18:00:00Z',
            'conversation_id': 'conversation-a',
          }
        ],
        'evidence_hash': 'sha256:grounded',
        'generated_at': '2026-08-01T12:00:00Z',
        'presentation': {'style': 'letter'},
      },
      'reason_code': null,
      'retry_after_seconds': null,
      'server_time': '2026-08-01T12:00:01Z',
      'etag': '"today-card-etag"',
    };

    test('parses the canonical ready envelope without losing provenance', () {
      final response = HttpTodayCardRepository.parseEnvelope(readyEnvelope, headerEtag: '"today-card-etag"');

      expect(response.isValid, isTrue);
      expect(response.status, TodayCardStatus.ready);
      expect(response.etag, '"today-card-etag"');
      expect(response.serverTime, DateTime.parse('2026-08-01T12:00:01Z'));
      expect(response.card?.localDate, '2026-08-01');
      expect(response.card?.timezone, 'America/Los_Angeles');
      expect(response.card?.evidenceHash, 'sha256:grounded');
      expect(response.card?.sourceDate, '2026-07-31');
      expect(response.card?.sourceRefs.single.kind, 'conversation_summary');
      expect(response.card?.sourceRefs.single.conversationId, 'conversation-a');
      expect(response.card?.sourceRefs.single.occurredAt, DateTime.parse('2026-07-31T18:00:00Z'));
    });

    test('keeps new-user, preparing, and degraded responses distinct', () {
      final welcome = Map<String, Object?>.from(readyEnvelope)
        ..['state'] = 'new_user'
        ..['card'] = {
          ...readyEnvelope['card']! as Map<String, Object?>,
          'kind': 'welcome',
          'source_refs': <Object?>[],
        };
      final preparing = Map<String, Object?>.from(readyEnvelope)
        ..['state'] = 'preparing'
        ..['card'] = null
        ..['retry_after_seconds'] = 30;
      final degraded = Map<String, Object?>.from(readyEnvelope)
        ..['state'] = 'degraded'
        ..['card'] = null
        ..['reason_code'] = 'no_safe_source';

      expect(HttpTodayCardRepository.parseEnvelope(welcome).status, TodayCardStatus.newUser);
      expect(HttpTodayCardRepository.parseEnvelope(preparing).retryAfter, const Duration(seconds: 30));
      expect(HttpTodayCardRepository.parseEnvelope(degraded).errorCode, 'no_safe_source');
    });

    test('uses authenticated intended backend path without sending a caller-selected uid', () async {
      String? requestUrl;
      bool? authRequired;
      final repository = HttpTodayCardRepository(
        baseUrl: 'https://api.ella-ai-care.com',
        request: ({
          required url,
          required headers,
          required body,
          required method,
          timeout,
          retries,
          requireAuthCheck,
        }) async {
          requestUrl = url;
          authRequired = requireAuthCheck;
          expect(method, 'GET');
          expect(body, isEmpty);
          return http.Response(jsonEncode(readyEnvelope), 200, headers: {'etag': '"today-card-etag"'});
        },
      );

      final response = await repository.fetch(uid: 'firebase-user-a');

      expect(response.isValid, isTrue);
      expect(requestUrl, 'https://api.ella-ai-care.com/v1/ella/today-card');
      expect(requestUrl, isNot(contains('firebase-user-a')));
      expect(authRequired, isTrue);
    });

    test('fails closed on malformed envelopes, stale contracts, and ETag disagreement', () {
      final stale = Map<String, Object?>.from(readyEnvelope)..['contract_version'] = 'ella.today_card.v0';

      expect(HttpTodayCardRepository.parseEnvelope('[]').errorCode, 'invalid_today_card_response');
      expect(HttpTodayCardRepository.parseEnvelope(stale).hasCurrentContract, isFalse);
      expect(
        HttpTodayCardRepository.parseEnvelope(readyEnvelope, headerEtag: '"different"').errorCode,
        'invalid_today_card_response',
      );
    });
  });

  group('Today card cache', () {
    setUp(() async {
      SharedPreferences.setMockInitialValues({});
      await SharedPreferencesUtil.init();
    });

    test('round trip retains the exact truthful card fields', () async {
      final cache = SharedPreferencesTodayCardCache();
      final card = _memory();

      await cache.write(uid: 'uid-a', card: card);
      final restored = await cache.read(uid: 'uid-a');

      expect(restored?.id, card.id);
      expect(restored?.version, card.version);
      expect(restored?.eyebrow, card.eyebrow);
      expect(restored?.sourceDate, card.sourceDate);
      expect(restored?.sourceRefs.single.versionId, 'v4');
    });

    test('is account-scoped and ignores another uid', () async {
      final cache = SharedPreferencesTodayCardCache();
      await cache.write(uid: 'uid-a', card: _memory());

      expect(await cache.read(uid: 'uid-b'), isNull);
    });

    test('rejects a stale contract version', () async {
      SharedPreferences.setMockInitialValues({
        'ellaTodayCardCache:uid-a': jsonEncode({
          'cache_schema': 'today-card-cache-v1',
          'contract_version': 'ella.today_card.v0',
          'uid': 'uid-a',
          'card': _memory().toCacheJson(),
        }),
      });
      await SharedPreferencesUtil.init();

      expect(await SharedPreferencesTodayCardCache().read(uid: 'uid-a'), isNull);
    });
  });

  group('TodayCardController', () {
    test('waits for authenticated provisioning before the first load', () async {
      final repository = _QueueRepository([_ready(_recap())]);
      final controller = TodayCardController(repository: repository, cache: _MemoryCache());
      addTearDown(controller.dispose);

      await controller.updateAuthority(uid: 'uid-a', isProvisioningReady: false);
      expect(repository.requestedUids, isEmpty);

      await controller.updateAuthority(uid: 'uid-a', isProvisioningReady: true);
      expect(repository.requestedUids, ['uid-a']);
      expect(controller.state.status, TodayCardStatus.ready);
    });

    test('retries when the app resumes after provisioning', () async {
      final repository = _QueueRepository([
        const TodayCardResponse(
          contractVersion: todayCardContractVersion,
          status: TodayCardStatus.degraded,
          errorCode: 'temporarily_unavailable',
        ),
        _ready(_recap(version: 2)),
      ]);
      final controller = TodayCardController(repository: repository, cache: _MemoryCache());
      addTearDown(controller.dispose);

      await controller.updateAuthority(uid: 'uid-a', isProvisioningReady: true);
      expect(controller.state.status, TodayCardStatus.degraded);

      await controller.onResumed();
      expect(repository.requestedUids, ['uid-a', 'uid-a']);
      expect(controller.state.card?.version, 2);
    });

    test('shows a last-valid card when refresh is degraded', () async {
      final cache = _MemoryCache()..cards['uid-a'] = _memory();
      final repository = _QueueRepository([
        const TodayCardResponse(
          contractVersion: todayCardContractVersion,
          status: TodayCardStatus.degraded,
          errorCode: 'provider_unavailable',
        ),
      ]);
      final controller = TodayCardController(repository: repository, cache: cache);
      addTearDown(controller.dispose);

      await controller.updateAuthority(uid: 'uid-a', isProvisioningReady: true);

      expect(controller.state.status, TodayCardStatus.degraded);
      expect(controller.state.card?.kind, TodayCardKind.memory);
      expect(controller.state.isCached, isTrue);
      expect(controller.state.errorCode, 'provider_unavailable');
    });

    test('keeps degraded state distinct when no cache exists', () async {
      final repository = _QueueRepository([
        const TodayCardResponse(
          contractVersion: todayCardContractVersion,
          status: TodayCardStatus.degraded,
          errorCode: 'provider_unavailable',
        ),
      ]);
      final controller = TodayCardController(repository: repository, cache: _MemoryCache());
      addTearDown(controller.dispose);

      await controller.updateAuthority(uid: 'uid-a', isProvisioningReady: true);

      expect(controller.state.status, TodayCardStatus.degraded);
      expect(controller.state.card, isNull);
      expect(controller.state.isCached, isFalse);
    });

    test('new user is authoritative and clears an obsolete cache', () async {
      final cache = _MemoryCache()..cards['uid-a'] = _recap();
      final repository = _QueueRepository([
        TodayCardResponse(contractVersion: todayCardContractVersion, status: TodayCardStatus.newUser, card: _welcome()),
      ]);
      final controller = TodayCardController(repository: repository, cache: cache);
      addTearDown(controller.dispose);

      await controller.updateAuthority(uid: 'uid-a', isProvisioningReady: true);

      expect(controller.state.status, TodayCardStatus.newUser);
      expect(controller.state.card?.kind, TodayCardKind.welcome);
      expect(cache.cards['uid-a'], isNull);
    });

    test('stale contract never replaces a current cached card', () async {
      final cached = _interest();
      final cache = _MemoryCache()..cards['uid-a'] = cached;
      final repository = _QueueRepository([
        TodayCardResponse(
          contractVersion: 'ella.today_card.v0',
          status: TodayCardStatus.ready,
          card: _recap(version: 99),
        ),
      ]);
      final controller = TodayCardController(repository: repository, cache: cache);
      addTearDown(controller.dispose);

      await controller.updateAuthority(uid: 'uid-a', isProvisioningReady: true);

      expect(controller.state.status, TodayCardStatus.degraded);
      expect(controller.state.errorCode, 'stale_contract');
      expect(controller.state.card, same(cached));
      expect(cache.cards['uid-a'], same(cached));
    });

    test('late response from another account cannot cross the account boundary', () async {
      final first = Completer<TodayCardResponse>();
      final repository = _DeferredRepository(first.future, _ready(_memory(id: 'uid-b-card')));
      final cache = _MemoryCache();
      final controller = TodayCardController(repository: repository, cache: cache);
      addTearDown(controller.dispose);

      final firstLoad = controller.updateAuthority(uid: 'uid-a', isProvisioningReady: true);
      await Future<void>.delayed(Duration.zero);
      await controller.updateAuthority(uid: 'uid-b', isProvisioningReady: true);
      first.complete(_ready(_recap(id: 'uid-a-card')));
      await firstLoad;

      expect(controller.state.card?.id, 'uid-b-card');
      expect(cache.cards['uid-a'], isNull);
      expect(cache.cards['uid-b']?.id, 'uid-b-card');
    });

    test('losing provisioning authority cancels an in-flight load and clears its card', () async {
      final pending = Completer<TodayCardResponse>();
      final repository = _DeferredRepository(pending.future, _ready(_memory()));
      final controller = TodayCardController(repository: repository, cache: _MemoryCache());
      addTearDown(controller.dispose);

      final load = controller.updateAuthority(uid: 'uid-a', isProvisioningReady: true);
      await Future<void>.delayed(Duration.zero);
      await controller.updateAuthority(uid: 'uid-a', isProvisioningReady: false);
      pending.complete(_ready(_recap()));
      await load;

      expect(controller.state.status, TodayCardStatus.preparing);
      expect(controller.state.card, isNull);
    });

    test('recap, memory, and interest preserve server-authored source labels', () {
      expect(_recap().eyebrow, 'A NOTE FROM YESTERDAY');
      expect(_memory().eyebrow, 'A MEMORY FROM JUNE 12');
      expect(_interest().eyebrow, 'SOMETHING YOU ENJOY');
    });
  });

  group('TodayCardSurface', () {
    testWidgets('keeps the truthful label and exposes full-width scoped Talk', (tester) async {
      var talks = 0;
      await _pumpSurface(
        tester,
        state: TodayCardViewState(status: TodayCardStatus.ready, card: _memory()),
        onTalk: () => talks++,
      );

      expect(find.text('A MEMORY FROM JUNE 12'), findsOneWidget);
      expect(find.text('The roses along Elm Street'), findsOneWidget);
      expect(find.text('Talk about this'), findsOneWidget);
      expect(tester.getSize(find.byKey(const Key('today-card-talk'))).height, greaterThanOrEqualTo(48));
      expect(
        tester.getSize(find.byKey(const Key('today-card-talk'))).width,
        tester.getSize(find.byKey(const Key('today-card-semantics'))).width - (EllaSizes.notePadding * 2),
      );

      await tester.tap(find.byKey(const Key('today-card-talk')));
      expect(talks, 1);
    });

    testWidgets('preparing, new-user, and degraded states remain distinct and never blank', (tester) async {
      await _pumpSurface(tester, state: const TodayCardViewState.preparing());
      expect(find.text('Ella is putting something together for you.'), findsOneWidget);

      await _pumpSurface(tester, state: const TodayCardViewState(status: TodayCardStatus.newUser));
      expect(find.text('What matters to you?'), findsOneWidget);
      expect(find.textContaining('person, place, or interest'), findsOneWidget);

      await _pumpSurface(
        tester,
        state: const TodayCardViewState(status: TodayCardStatus.degraded, errorCode: 'provider_unavailable'),
      );
      expect(find.text('Ella could not refresh this just now.'), findsOneWidget);
      expect(find.text('Pull down to try again.'), findsOneWidget);
    });

    testWidgets('degraded cache preserves the card label and identifies saved content', (tester) async {
      await _pumpSurface(
        tester,
        state: TodayCardViewState(
          status: TodayCardStatus.degraded,
          card: _interest(),
          isCached: true,
          errorCode: 'temporarily_unavailable',
        ),
      );

      expect(find.text('SOMETHING YOU ENJOY'), findsOneWidget);
      expect(find.byKey(const Key('today-card-cached-status')), findsOneWidget);
      expect(find.text('Showing the last item Ella saved for you.'), findsOneWidget);
    });

    testWidgets('large Dynamic Type remains scrollable without overflow', (tester) async {
      tester.view.physicalSize = const Size(390, 520);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      await _pumpSurface(
        tester,
        state: TodayCardViewState(status: TodayCardStatus.ready, card: _recap()),
        textScale: 2,
        onTalk: () {},
      );

      expect(tester.takeException(), isNull);
      expect(find.byType(SingleChildScrollView), findsOneWidget);
    });

    testWidgets('VoiceOver exposes a named Talk button and headline semantics', (tester) async {
      final semantics = tester.ensureSemantics();
      await _pumpSurface(
        tester,
        state: TodayCardViewState(status: TodayCardStatus.ready, card: _recap()),
        onTalk: () {},
      );

      expect(find.bySemanticsLabel('Talk about this'), findsWidgets);
      final headline = tester.getSemantics(find.byKey(const Key('today-card-headline')));
      expect(headline.flagsCollection.isHeader, isTrue);
      semantics.dispose();
    });
  });
}

Future<void> _pumpSurface(
  WidgetTester tester, {
  required TodayCardViewState state,
  double textScale = 1,
  VoidCallback? onTalk,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      theme: ellaThemeData(),
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: MediaQuery(
        data: MediaQueryData(textScaler: TextScaler.linear(textScale)),
        child: Scaffold(
          body: SingleChildScrollView(
            padding: const EdgeInsets.all(20),
            child: TodayCardSurface(
              state: state,
              isReading: false,
              onTalk: onTalk,
              onReadAloud: state.card == null ? null : () {},
            ),
          ),
        ),
      ),
    ),
  );
  await tester.pump();
}

TodayCardResponse _ready(TodayCard card) =>
    TodayCardResponse(contractVersion: todayCardContractVersion, status: TodayCardStatus.ready, card: card);

TodayCard _recap({String id = 'recap-card', int version = 1}) => TodayCard(
      id: id,
      version: version,
      kind: TodayCardKind.recap,
      eyebrow: 'A NOTE FROM YESTERDAY',
      headline: 'A good conversation with Rose',
      body: 'You and Rose talked about the garden.',
      spokenText: 'You and Rose talked about the garden.',
      sourceDate: '2026-07-30',
      generatedAt: DateTime.utc(2026, 7, 31, 10),
      sourceRefs: const [TodayCardSourceRef(kind: 'conversation_summary', id: 'conversation-1', versionId: 'v2')],
    );

TodayCard _memory({String id = 'memory-card'}) => TodayCard(
      id: id,
      version: 3,
      kind: TodayCardKind.memory,
      eyebrow: 'A MEMORY FROM JUNE 12',
      headline: 'The roses along Elm Street',
      body: 'You enjoyed the long walk home with Rose.',
      spokenText: 'You enjoyed the long walk home with Rose.',
      sourceDate: '2026-06-12',
      generatedAt: DateTime.utc(2026, 7, 31, 10),
      sourceRefs: const [TodayCardSourceRef(kind: 'memory', id: 'memory-1', versionId: 'v4')],
    );

TodayCard _interest() => TodayCard(
      id: 'interest-card',
      version: 2,
      kind: TodayCardKind.interest,
      eyebrow: 'SOMETHING YOU ENJOY',
      headline: 'Your garden',
      body: 'The roses are one of your favorite parts of summer.',
      generatedAt: DateTime.utc(2026, 7, 31, 10),
      sourceRefs: const [TodayCardSourceRef(kind: 'confirmed_interest', id: 'interest-1')],
    );

TodayCard _welcome() => TodayCard(
      id: 'welcome-card',
      version: 1,
      kind: TodayCardKind.welcome,
      eyebrow: 'FOR YOU TODAY',
      headline: 'What matters to you?',
      body: 'Tell Ella about a person, place, or interest you would like to talk about.',
      generatedAt: DateTime.utc(2026, 7, 31, 10),
    );

class _QueueRepository implements TodayCardRepository {
  _QueueRepository(this.responses);

  final List<TodayCardResponse> responses;
  final List<String> requestedUids = [];

  @override
  Future<TodayCardResponse> fetch({required String uid}) async {
    requestedUids.add(uid);
    return responses.removeAt(0);
  }
}

class _DeferredRepository implements TodayCardRepository {
  _DeferredRepository(this.first, this.second);

  final Future<TodayCardResponse> first;
  final TodayCardResponse second;
  int calls = 0;

  @override
  Future<TodayCardResponse> fetch({required String uid}) {
    calls++;
    return calls == 1 ? first : Future.value(second);
  }
}

class _MemoryCache implements TodayCardCache {
  final Map<String, TodayCard> cards = {};

  @override
  Future<void> clear({required String uid}) async => cards.remove(uid);

  @override
  Future<TodayCard?> read({required String uid}) async => cards[uid];

  @override
  Future<void> write({required String uid, required TodayCard card}) async => cards[uid] = card;
}
