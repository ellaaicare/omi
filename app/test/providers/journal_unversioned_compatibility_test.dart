import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/services/summary_read_freshness.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _Owner implements AccountCommitAuthority {
  @override
  String get uid => 'journal-fixture-owner';
  bool current = true;
  @override
  bool isCurrent() => current;
  @override
  bool isExactCurrent() => current;
}

ServerConversation _row(String id, {String? version, String? parent, bool processing = false, bool discarded = false}) {
  return ServerConversation(
    id: id,
    createdAt: DateTime.utc(2026, 10, 17),
    structured: Structured(version == 'b' ? 'Fictional new' : 'Fictional stock', 'Fictional overview'),
    status: processing ? ConversationStatus.processing : ConversationStatus.completed,
    discarded: discarded,
    activeSummaryVersionId: version,
    summaryVersionActiveAuthority: version,
    summaryVersionBodyUid: 'journal-fixture-owner',
    summaryVersionBodyUidPresent: true,
    summaryVersions: version == null
        ? []
        : [
            if (parent != null)
              {'id': parent, 'title': 'Fictional stock', 'overview': 'Fictional overview', 'is_active': false},
            {
              'id': version,
              'title': version == 'b' ? 'Fictional new' : 'Fictional stock',
              'overview': 'Fictional overview',
              'conversation_id': id,
              'is_active': true,
              if (parent != null) 'based_on_version_id': parent,
            },
          ],
  );
}

ServerConversation _with(ServerConversation row, Map<String, dynamic> fields) =>
    ServerConversation.fromJson({...row.toJson(), ...fields});

ConversationProvider _provider(_Owner owner, Future<ConversationsFetchResult> Function() read,
    {ConversationsPageFetchCall? page}) {
  final provider = ConversationProvider(
    activeAuthority: () => owner,
    authenticatedUid: () => owner.uid,
    conversationsFetchCall: read,
    conversationsPageFetchCall: page,
    failedConversationsFetchCall: () async => const ConversationsFetchResult.success([]),
  );
  addTearDown(provider.dispose);
  return provider;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    SharedPreferencesUtil().uid = 'journal-fixture-owner';
  });

  for (final processing in [false, true]) {
    for (final discarded in [false, true]) {
      test('explicit empty-history null-active is legitimate processing=$processing discarded=$discarded', () {
        final row = _row('unversioned', processing: processing, discarded: discarded);
        expect(summaryResponseMatchesOwnerAndVersion(row, 'journal-fixture-owner'), true);
        expect(row.activeSummaryVersionId, null);
        expect(explicitSummaryProcessingMessage(row), null);
        expect(row.enrichmentState, null);
      });
    }
  }
  test('known active version cannot downgrade to empty history or omitted history', () {
    final known = _row('same', version: 'a');
    for (final history in [null, <dynamic>[]]) {
      final incoming = _with(_row('same'), {'summary_versions': history});
      expect(summaryResponseMatchesOwnerAndVersion(incoming, 'journal-fixture-owner', previous: known), false);
    }
  });
  test('malformed active or history is still rejected, no empty-history escape', () {
    for (final fields in <Map<String, dynamic>>[
      {'active_summary_version_id': ''},
      {'active_summary_version_id': 4},
      {'active_summary_version_id': 'a'},
      {'summary_versions': {}},
      {
        'summary_versions': [
          {'id': 'a', 'is_active': false}
        ]
      },
    ]) {
      expect(summaryResponseMatchesOwnerAndVersion(_with(_row('same'), fields), 'journal-fixture-owner'), false);
    }
  });
  test('tenant mismatch and missing owner authority cannot pass unversioned branch', () {
    final row = _with(_row('same'), {'uid': 'other-owner'});
    expect(summaryResponseMatchesOwnerAndVersion(row, 'journal-fixture-owner'), false);
    expect(summaryResponseMatchesOwnerAndVersion(_row('same'), ''), false);
  });
  test('version advance requires exact lineage and same-version body remains immutable', () {
    final old = _row('same', version: 'a');
    final valid = _row('same', version: 'b', parent: 'a');
    expect(summaryResponseMatchesOwnerAndVersion(valid, 'journal-fixture-owner', previous: old), true);
    expect(summaryResponseMatchesOwnerAndVersion(_row('same', version: 'b'), 'journal-fixture-owner', previous: old),
        false);
    final bad = _with(old, {
      'structured': {...Map<String, dynamic>.from(old.structured.toJson()), 'title': 'Contradictory fictional title'}
    });
    expect(summaryResponseMatchesOwnerAndVersion(bad, 'journal-fixture-owner', previous: old), false);
  });
  test('duplicate IDs reject both candidates without choosing list order', () {
    final row = _row('duplicate', version: 'a');
    final batch = validateSummaryResponseBatch([row, row], 'journal-fixture-owner', previous: []);
    expect(batch.accepted, isEmpty);
    expect(batch.rejectedIds, {'duplicate'});
  });
  test('mixed versioned, unversioned processing and discarded rows do not stale valid initial read', () async {
    final good = _row('good', version: 'a');
    final processing = _row('processing', processing: true);
    final discarded = _row('discarded', discarded: true);
    final provider = _provider(_Owner(), () async => ConversationsFetchResult.success([good, processing, discarded]));
    await provider.fetchConversations();
    expect(provider.summaryListRead.phase, SummaryReadPhase.checked);
    expect(provider.hasFreshConversations, true);
    expect(provider.summaryReadFor(good).phase, SummaryReadPhase.checked);
    expect(provider.summaryReadFor(processing).phase, SummaryReadPhase.checked);
    expect(provider.processingConversations.single.id, 'processing');
    expect(provider.conversations.map((r) => r.id).toSet(), {'good', 'discarded'});
  });
  test('bad new row is isolated while valid row checked and list remains explicitly stale', () async {
    final good = _row('good', version: 'a');
    final bad = _with(_row('bad'), {'uid': 'foreign'});
    final provider = _provider(_Owner(), () async => ConversationsFetchResult.success([good, bad]));
    await provider.fetchConversations();
    expect(provider.conversations.single.id, 'good');
    expect(provider.summaryReadFor(good).phase, SummaryReadPhase.checked);
    expect(provider.summaryListRead.phase, SummaryReadPhase.stale);
    expect(provider.hasFreshConversations, false);
    expect(provider.isShowingCachedConversations, false);
  });
  for (final initial in [false, true]) {
    test('rejected known downgrade retains prior row stale while sibling advances initial=$initial', () async {
      final oldGood = _row('good', version: 'a');
      final oldHeld = _row('held', version: 'a');
      final newGood = _row('good', version: 'b', parent: 'a');
      var count = 0;
      final provider = _provider(
          _Owner(),
          () async => ConversationsFetchResult.success(
              ++count == 1 ? [oldGood, oldHeld] : [newGood, _row('held'), _row('processing', processing: true)]));
      await provider.fetchConversations();
      if (initial) {
        await provider.fetchConversations();
      } else {
        await provider.forceRefreshConversations();
      }
      final kept = provider.conversations.firstWhere((r) => r.id == 'held');
      final advanced = provider.conversations.firstWhere((r) => r.id == 'good');
      expect(kept.activeSummaryVersionId, 'a');
      expect(provider.summaryReadFor(kept).phase, SummaryReadPhase.stale);
      expect(advanced.activeSummaryVersionId, 'b');
      expect(provider.summaryReadFor(advanced).phase, SummaryReadPhase.checked);
      expect(provider.summaryListRead.phase, SummaryReadPhase.stale);
      expect(provider.isShowingCachedConversations, true);
    });
  }
  test('pagination validates same-owner/version and counts raw rows without poisoning accepted page', () async {
    final known = _row('known', version: 'a');
    final good = _row('new', version: 'a');
    final provider = _provider(_Owner(), () async => ConversationsFetchResult.success([known]),
        page: ({required int limit, required int offset}) async =>
            ConversationsFetchResult.success([good, _row('known'), _row('processing', processing: true)]));
    await provider.fetchConversations();
    provider.hasMoreConversations = true;
    await provider.getMoreConversationsFromServer();
    expect(provider.conversations.firstWhere((r) => r.id == 'known').activeSummaryVersionId, 'a');
    expect(provider.conversations.any((r) => r.id == 'new'), true);
    expect(provider.summaryReadFor(good).phase, SummaryReadPhase.checked);
    expect(provider.summaryReadFor(known).phase, SummaryReadPhase.stale);
    expect(provider.loadMoreConversationsFailed, true);
    expect(provider.summaryListRead.phase, SummaryReadPhase.stale);
  });
  test('retired owner mixed held response cannot publish any row or checked state', () async {
    final owner = _Owner();
    final held = Completer<ConversationsFetchResult>();
    final provider = _provider(owner, () => held.future);
    final request = provider.fetchConversations();
    owner.current = false;
    provider.reset();
    held.complete(ConversationsFetchResult.success([_row('good', version: 'a'), _row('processing', processing: true)]));
    await request;
    expect(provider.conversations, isEmpty);
    expect(provider.processingConversations, isEmpty);
    expect(provider.summaryListRead.phase, SummaryReadPhase.unchecked);
  });
  for (final reverse in [false, true]) {
    test('stale duplicate processing cannot erase canonical authority reverse=$reverse', () {
      final canonical = _row('same', version: 'a');
      final stale = _row('same', processing: true);
      final previous = reverse ? [stale, canonical] : [canonical, stale];
      final unrelated =
          validateSummaryResponseBatch([_row('same', version: 'b')], 'journal-fixture-owner', previous: previous);
      expect(unrelated.accepted, isEmpty);
      expect(unrelated.rejectedIds, {'same'});
      final descendant = validateSummaryResponseBatch(
          [_row('same', version: 'b', parent: 'a')], 'journal-fixture-owner',
          previous: previous);
      expect(descendant.accepted.single.activeSummaryVersionId, 'b');
      expect(descendant.hasRejected, false);
    });
  }
  for (final legacy in [false, true]) {
    test('pagination promotion retires processing and permits exact descendant legacy=$legacy', () async {
      var reads = 0;
      final processing =
          legacy ? _with(_row('same', processing: true), {'summary_versions': null}) : _row('same', processing: true);
      final provider = _provider(
          _Owner(),
          () async =>
              ConversationsFetchResult.success(++reads == 1 ? [processing] : [_row('same', version: 'b', parent: 'a')]),
          page: ({required int limit, required int offset}) async =>
              ConversationsFetchResult.success([_row('same', version: 'a')]));
      await provider.fetchConversations();
      expect(provider.processingConversations.single.id, 'same');
      provider.hasMoreConversations = true;
      await provider.getMoreConversationsFromServer();
      expect(provider.processingConversations, isEmpty);
      expect(provider.conversations.single.activeSummaryVersionId, 'a');
      await provider.forceRefreshConversations();
      expect(provider.conversations.single.activeSummaryVersionId, 'b');
      expect(provider.summaryReadFor(provider.conversations.single).phase, SummaryReadPhase.checked);
      expect(provider.summaryListRead.phase, SummaryReadPhase.checked);
    });
  }
  test('conflicting known version authorities fail closed instead of selecting an unversioned projection', () {
    final previous = [_row('same', version: 'a'), _row('same', version: 'other'), _row('same', processing: true)];
    final batch = validateSummaryResponseBatch([_row('same', version: 'b', parent: 'a')], 'journal-fixture-owner',
        previous: previous);
    expect(batch.accepted, isEmpty);
    expect(batch.rejectedIds, {'same'});
  });
}
