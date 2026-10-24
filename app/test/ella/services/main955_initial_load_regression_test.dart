import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/ella/services/journal_preview_refresh.dart';
import 'package:omi/ella/services/summary_read_freshness.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'journal_preview_refresh_test.dart' show Authority, record;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    SharedPreferencesUtil().uid = 'owner';
  });
  tearDown(EllaAccountCommitBarrier.quiesceForAccountTransition);
  test('equivalent owner row replacement while first preview GET is held is not a load error', () async {
    final authority = Authority();
    var row = record('Fictional library', 'Fictional retained overview');
    final held = Completer<ServerConversation?>();
    final preview = JournalPreviewRefresh(
        id: row.id,
        authority: authority,
        presentationCurrent: () => true,
        currentRow: () => row,
        loader: (id, {expectedAuthenticatedUid, exactAuthority}) => held.future,
        commitRow: (incoming, expected, witness) {
          if (!identical(row, expected) || journalPreviewRowWitness(row) != witness) return false;
          row = incoming;
          return true;
        });
    addTearDown(preview.dispose);
    final loading = preview.load();
    final before = journalPreviewRowWitness(row);
    row = record('Fictional library', 'Fictional retained overview');
    expect(journalPreviewRowWitness(row), before);
    held.complete(record('Fictional library', 'Fictional retained overview'));
    await loading;
    expect(preview.memory, isNotNull);
    expect(preview.memory!.structured.overview, 'Fictional retained overview');
  });
  test('retained sibling outside newest page cannot stay checking after request ends', () async {
    final authority = Authority();
    final latest = record('Latest fictional note', 'Latest overview', id: 'new');
    final older = record('Older fictional note', 'Older overview', id: 'old');
    final provider = ConversationProvider(
        activeAuthority: () => authority,
        authenticatedUid: () => authority.uid,
        conversationsFetchCall: () async => ConversationsFetchResult.success([latest]),
        failedConversationsFetchCall: () async => const ConversationsFetchResult.success([]));
    addTearDown(provider.dispose);
    provider.conversations = [latest, older];
    await provider.forceRefreshConversations();
    expect(provider.conversations.any((row) => row.id == older.id), true);
    expect(provider.summaryReadFor(latest).phase, SummaryReadPhase.checked);
    expect(provider.summaryReadFor(older).phase, isNot(SummaryReadPhase.checking));
  });
  test('foreign owner equivalent-looking replacement cannot inherit a held preview', () async {
    var row = record('Fictional library', 'Same words');
    final held = Completer<ServerConversation?>();
    var commits = 0;
    final preview = JournalPreviewRefresh(
        id: row.id,
        authority: Authority(),
        presentationCurrent: () => true,
        currentRow: () => row,
        loader: (id, {expectedAuthenticatedUid, exactAuthority}) => held.future,
        commitRow: (incoming, expected, witness) {
          commits++;
          row = incoming;
          return true;
        });
    addTearDown(preview.dispose);
    final pending = preview.load();
    row = record('Fictional library', 'Same words', uid: 'other');
    held.complete(record('Fictional library', 'Same words'));
    await pending;
    expect(commits, 0);
    expect(preview.memory, null);
    expect(preview.retainedMemory, null);
  });
  test('newest-only refresh preserves older actual checked time without restamping', () async {
    final authority = Authority();
    final latest = record('Latest', 'Latest overview', id: 'new');
    final older = record('Older', 'Older overview', id: 'old');
    var reads = 0;
    final provider = ConversationProvider(
        activeAuthority: () => authority,
        authenticatedUid: () => authority.uid,
        conversationsFetchCall: () async => ConversationsFetchResult.success(++reads == 1 ? [latest, older] : [latest]),
        failedConversationsFetchCall: () async => const ConversationsFetchResult.success([]));
    addTearDown(provider.dispose);
    provider.conversations = [latest, older];
    await provider.forceRefreshConversations();
    final before = provider.summaryReadFor(older);
    expect(before.phase, SummaryReadPhase.checked);
    await provider.forceRefreshConversations();
    final after = provider.summaryReadFor(older);
    expect(after.phase, SummaryReadPhase.checked);
    expect(after.checkedAt, before.checkedAt);
  });
}
