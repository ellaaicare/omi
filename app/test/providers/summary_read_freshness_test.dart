import 'dart:async';
import 'package:fake_async/fake_async.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/ella/services/summary_read_freshness.dart';
import 'package:omi/pages/conversation_detail/conversation_detail_provider.dart';
import 'package:omi/providers/conversation_provider.dart';

class _Owner implements AccountCommitAuthority {
  _Owner(this.uid);
  @override
  final String uid;
  bool current = true;
  @override
  bool isCurrent() => current;
  @override
  bool isExactCurrent() => current;
}

ServerConversation _row(String version, {String title = 'Stock', String? parent, String uid = 'fictional-owner'}) {
  final date = DateTime.utc(2026, 10, 16);
  return ServerConversation(
    id: 'fictional-memory',
    createdAt: date,
    structured: Structured(title, '$title overview'),
    activeSummaryVersionId: version,
    summaryVersionActiveAuthority: version,
    summaryVersionBodyUid: uid,
    summaryVersionBodyUidPresent: true,
    summaryVersions: [
      if (parent != null) {'id': parent, 'title': 'Stock', 'overview': 'Stock overview', 'is_active': false},
      {
        'id': version,
        'title': title,
        'overview': '$title overview',
        'is_active': true,
        if (parent != null) 'based_on_version_id': parent,
      },
    ],
  );
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    SharedPreferencesUtil().uid = 'fictional-owner';
  });

  test('explicit descendant version advances, rollback and same-ID body change do not', () {
    final stock = _row('stock-a');
    final enriched = _row('enriched-b', title: 'Enriched', parent: 'stock-a');
    expect(summaryResponseMatchesOwnerAndVersion(enriched, 'fictional-owner', previous: stock), true);
    expect(summaryResponseMatchesOwnerAndVersion(stock, 'fictional-owner', previous: enriched), false);
    expect(
      summaryResponseMatchesOwnerAndVersion(_row('stock-a', title: 'Changed'), 'fictional-owner', previous: stock),
      false,
    );
  });
  test('foreign body UID cannot become current', () {
    expect(summaryResponseMatchesOwnerAndVersion(_row('v', uid: 'other'), 'fictional-owner'), false);
  });
  test('read evidence for old version becomes unchecked after row replacement', () {
    const observation = SummaryReadObservation(SummaryReadPhase.checked, versionId: 'old');
    expect(observation.forConversation(_row('new')).phase, SummaryReadPhase.unchecked);
  });
  test('in-place structured mutation invalidates a checked body witness', () {
    final row = _row('same');
    final observation = SummaryReadObservation(
      SummaryReadPhase.checked,
      versionId: row.activeSummaryVersionId,
      bodyWitness: summaryReadBodyWitness(row),
    );
    expect(observation.forConversation(row).phase, SummaryReadPhase.checked);
    row.structured.title = 'Changed after read';
    expect(observation.forConversation(row).phase, SummaryReadPhase.unchecked);
  });
  test('duplicate version IDs and contradictory structured are unverified', () {
    final json = _row('v').toJson();
    final versions = List<dynamic>.from(json['summary_versions'] as List);
    expect(
      summaryResponseMatchesOwnerAndVersion(
        ServerConversation.fromJson({
          ...json,
          'summary_versions': [...versions, versions.single],
        }),
        'fictional-owner',
      ),
      false,
    );
    expect(
      summaryResponseMatchesOwnerAndVersion(
        ServerConversation.fromJson({
          ...json,
          'structured': {
            ...Map<String, dynamic>.from(json['structured'] as Map),
            'title': 'Contradiction',
            'overview': 'Contradiction',
          },
        }),
        'fictional-owner',
      ),
      false,
    );
  });
  test('generic summary alone cannot establish a processing hold', () {
    expect(explicitSummaryProcessingMessage(_row('stock')), null);
  });
  test('only explicit server hold or pending metadata gets that label', () {
    final base = _row('stock').toJson();
    expect(
      explicitSummaryProcessingMessage(
        ServerConversation.fromJson({
          ...base,
          'enrichment_state': {'status': 'held'},
        }),
      ),
      'Summary processing is on hold.',
    );
    expect(
      explicitSummaryProcessingMessage(
        ServerConversation.fromJson({
          ...base,
          'enrichment_state': {'status': 'pending'},
        }),
      ),
      'Summary processing is pending.',
    );
  });
  test('list failed refresh retains text visibly stale then exact descendant recovery checks new version', () async {
    final owner = _Owner('fictional-owner');
    var reads = 0;
    final stock = _row('a');
    final enriched = _row('b', title: 'Enriched', parent: 'a');
    final provider = ConversationProvider(
      activeAuthority: () => owner,
      authenticatedUid: () => owner.uid,
      conversationsFetchCall: () async {
        reads++;
        if (reads == 2) return const ConversationsFetchResult.failure();
        return ConversationsFetchResult.success([reads == 1 ? stock : enriched]);
      },
      failedConversationsFetchCall: () async => const ConversationsFetchResult.success([]),
    );
    addTearDown(provider.dispose);
    await provider.fetchConversations();
    expect(provider.summaryReadFor(stock).phase, SummaryReadPhase.checked);
    await provider.forceRefreshConversations();
    expect(provider.conversations.single.structured.title, 'Stock');
    expect(provider.summaryListRead.phase, SummaryReadPhase.stale);
    expect(provider.summaryReadFor(stock).phase, SummaryReadPhase.stale);
    expect(provider.hasFreshConversations, false);
    await provider.forceRefreshConversations();
    expect(provider.conversations.single.structured.title, 'Enriched');
    expect(provider.summaryReadFor(enriched).versionId, 'b');
    expect(provider.summaryReadFor(enriched).phase, SummaryReadPhase.checked);
    expect(reads, 3);
  });
  test('actual provider failed stock reread retries to exact enriched version then stops', () {
    fakeAsync((a) {
      final owner = _Owner('fictional-owner');
      var reads = 0;
      final stock = _row('a');
      final enriched = ServerConversation.fromJson({
        ..._row('b', title: 'Enriched', parent: 'a').toJson(),
        'enrichment_state': {
          'result_summary_version_id': 'b',
          'status': 'writeback_applied',
          'pending': false,
          'canonical_status': 'completed',
          'kind': 'hermes_enriched',
          'source': 'hermes_parallel'
        },
      });
      final provider = ConversationProvider(
          activeAuthority: () => owner,
          authenticatedUid: () => owner.uid,
          conversationsFetchCall: () async {
            reads++;
            if (reads == 2) return const ConversationsFetchResult.failure();
            return ConversationsFetchResult.success([reads == 1 ? stock : enriched]);
          },
          failedConversationsFetchCall: () async => const ConversationsFetchResult.success([]));
      unawaited(provider.fetchConversations());
      a.flushMicrotasks();
      expect(reads, 1);
      expect(provider.conversations.single.structured.title, 'Stock');
      a.elapse(const Duration(seconds: 10));
      a.flushMicrotasks();
      expect(reads, 2);
      expect(provider.summaryListRead.phase, SummaryReadPhase.stale);
      expect(provider.conversations.single.structured.title, 'Stock');
      a.elapse(const Duration(seconds: 30));
      a.flushMicrotasks();
      expect(reads, 3);
      expect(provider.conversations.single.structured.title, 'Enriched');
      expect(provider.summaryReadFor(enriched).phase, SummaryReadPhase.checked);
      a.elapse(const Duration(hours: 1));
      a.flushMicrotasks();
      expect(reads, 3);
      provider.dispose();
    });
  });
  test('retired owner held list reply cannot mark replacement current', () async {
    final owner = _Owner('fictional-owner');
    final held = Completer<ConversationsFetchResult>();
    final provider = ConversationProvider(
      activeAuthority: () => owner,
      authenticatedUid: () => owner.uid,
      conversationsFetchCall: () => held.future,
      failedConversationsFetchCall: () async => const ConversationsFetchResult.success([]),
    );
    addTearDown(provider.dispose);
    final read = provider.fetchConversations();
    owner.current = false;
    provider.reset();
    held.complete(ConversationsFetchResult.success([_row('old')]));
    await read;
    expect(provider.conversations, isEmpty);
    expect(provider.summaryListRead.phase, SummaryReadPhase.unchecked);
  });
  test('detail null/throw shows stale, successful descendant replaces body and version', () async {
    final owner = _Owner('fictional-owner');
    var reads = 0;
    final stock = _row('a');
    final detail = ConversationDetailProvider(
      activeAuthority: () => owner,
      conversationLoader: (id, {expectedAuthenticatedUid, exactAuthority}) async {
        expect(expectedAuthenticatedUid, owner.uid);
        expect(exactAuthority?.isExactCurrent(), true);
        reads++;
        if (reads == 1) return null;
        if (reads == 2) throw StateError('synthetic read failure');
        return _row('b', title: 'Enriched', parent: 'a');
      },
    )..selectedDate = stock.createdAt;
    addTearDown(detail.dispose);
    detail.setCachedConversation(stock);
    await detail.refreshConversation();
    expect(detail.summaryRead.phase, SummaryReadPhase.stale);
    expect(detail.conversation.structured.title, 'Stock');
    await detail.refreshConversation();
    expect(detail.summaryRead.phase, SummaryReadPhase.stale);
    await detail.refreshConversation();
    expect(detail.summaryRead.phase, SummaryReadPhase.checked);
    expect(detail.summaryRead.versionId, 'b');
    expect(detail.conversation.structured.title, 'Enriched');
  });
  test('held old detail read cannot replace a later conversation or publish checked', () async {
    final owner = _Owner('fictional-owner');
    final held = Completer<ServerConversation?>();
    final row = _row('a');
    final detail = ConversationDetailProvider(
      activeAuthority: () => owner,
      conversationLoader: (id, {expectedAuthenticatedUid, exactAuthority}) => held.future,
    )..selectedDate = row.createdAt;
    addTearDown(detail.dispose);
    detail.setCachedConversation(row);
    final read = detail.refreshConversation();
    detail.setCachedConversation(_row('replacement'));
    held.complete(_row('b', title: 'Enriched', parent: 'a'));
    await read;
    expect(detail.conversation.activeSummaryVersionId, 'replacement');
    expect(detail.summaryRead.phase, SummaryReadPhase.unchecked);
  });
}
