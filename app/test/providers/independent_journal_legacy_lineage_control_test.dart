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
 setUp(() async { SharedPreferences.setMockInitialValues({});await SharedPreferencesUtil.init();SharedPreferencesUtil().uid='journal-fixture-owner'; });
 test('shared legacy-null processing shape must retain known version lineage', () async {
  var reads=0;
  final provider=_provider(_Owner(),() async => ConversationsFetchResult.success(++reads==1?[_with(_row('same',processing:true),{'summary_versions':null})]:[_row('same',version:'b')]),page:({required int limit,required int offset}) async => ConversationsFetchResult.success([_row('same',version:'a')]));
  await provider.fetchConversations();expect(provider.processingConversations.single.id,'same');
  provider.hasMoreConversations=true;await provider.getMoreConversationsFromServer();
  expect(provider.conversations.single.activeSummaryVersionId,'a');
  await provider.forceRefreshConversations();
  expect(provider.conversations.single.activeSummaryVersionId,'a',reason:'Known a cannot be replaced by unrelated b without lineage');
  expect(provider.summaryListRead.phase,SummaryReadPhase.stale);
 });
}
