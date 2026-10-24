import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/ella/services/journal_preview_refresh.dart';
import 'package:omi/ella/widgets/refreshed_journal_preview.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class Authority implements AccountCommitAuthority {
  bool current = true;
  @override
  String get uid => 'owner';
  @override
  bool isCurrent() => current;
  @override
  bool isExactCurrent() => current;
}

ServerConversation record(String title, String overview, {String id = 'm1', String uid = 'owner'}) =>
    ServerConversation.fromJson({
      'id': id,
      'uid': uid,
      'created_at': '2026-10-11T20:23:00Z',
      'structured': {'title': title, 'overview': overview, 'emoji': '🌱', 'category': 'other'},
      'transcript_segments': [],
      'active_summary_version_id': 'enriched-version',
      'summary_versions': [
        {'id': 'enriched-version', 'title': title, 'overview': overview, 'is_active': true}
      ],
    });
JournalPreviewRefresh testRefresh(
    {required String id,
    required ExactAccountAuthorityVerifier authority,
    required bool Function() presentationCurrent,
    JournalPreviewLoader? loader,
    void Function(ServerConversation)? onAccepted}) {
  ServerConversation row = ServerConversation.fromJson(record('Baseline', 'Baseline').toJson()
    ..remove('active_summary_version_id')
    ..remove('summary_versions'));
  return JournalPreviewRefresh(
      id: id,
      authority: authority,
      presentationCurrent: presentationCurrent,
      loader: loader,
      currentRow: () => row,
      commitRow: (incoming, expected, witness) {
        if (!identical(row, expected) || journalPreviewRowWitness(row) != witness) return false;
        row = incoming;
        onAccepted?.call(incoming);
        return true;
      });
}

ServerConversation versioned(String id, String title, String overview,
        {List<Map<String, dynamic>> parents = const []}) =>
    ServerConversation.fromJson(record(title, overview).toJson()
      ..['active_summary_version_id'] = id
      ..['summary_versions'] = [
        ...parents,
        {
          'id': id,
          'title': title,
          'overview': overview,
          'is_active': true,
          'based_on_version_id': parents.isEmpty ? null : parents.last['id']
        }
      ]);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });
  tearDown(EllaAccountCommitBarrier.quiesceForAccountTransition);
  test('exact owner GET commits a detached atomic title/overview pair and updates row', () async {
    final authority = Authority();
    var commits = 0;
    final response = record('Enriched title', 'Enriched overview');
    final controller = testRefresh(
        id: 'm1',
        authority: authority,
        presentationCurrent: () => true,
        loader: (id, {expectedAuthenticatedUid, exactAuthority}) async {
          expect(id, 'm1');
          expect(expectedAuthenticatedUid, 'owner');
          expect(exactAuthority!.isExactCurrent(), isTrue);
          return response;
        },
        onAccepted: (memory) {
          commits++;
          expect(memory.structured.title, 'Enriched title');
          expect(memory.structured.overview, 'Enriched overview');
        });
    await controller.load();
    expect(controller.memory!.activeSummaryVersionId, 'enriched-version');
    response.structured.title = 'Changed alias';
    expect(controller.memory!.structured.title, 'Enriched title');
    expect(commits, 1);
    controller.dispose();
  });
  test('older request cannot overwrite a newer exact response', () async {
    final first = Completer<ServerConversation?>(), second = Completer<ServerConversation?>();
    var calls = 0, commits = 0;
    final controller = testRefresh(
        id: 'm1',
        authority: Authority(),
        presentationCurrent: () => true,
        loader: (id, {expectedAuthenticatedUid, exactAuthority}) => (++calls == 1 ? first : second).future,
        onAccepted: (_) => commits++);
    final one = controller.load(), two = controller.load();
    second.complete(record('New title', 'New overview'));
    await two;
    first.complete(record('Old title', 'Old overview'));
    await one;
    expect(controller.memory!.structured.title, 'New title');
    expect(commits, 1);
    controller.dispose();
  });
  for (final retirement in ['account', 'consent', 'presentation', 'dispose']) {
    test('$retirement retirement rejects in-flight read and row writeback', () async {
      final authority = Authority(), response = Completer<ServerConversation?>();
      var visible = true, commits = 0;
      final controller = testRefresh(
          id: 'm1',
          authority: authority,
          presentationCurrent: () => visible,
          loader: (id, {expectedAuthenticatedUid, exactAuthority}) => response.future,
          onAccepted: (_) => commits++);
      final pending = controller.load();
      switch (retirement) {
        case 'account':
          EllaAccountCommitBarrier.quiesceForAccountTransition();
          break;
        case 'consent':
          authority.current = false;
          break;
        case 'presentation':
          visible = false;
          break;
        case 'dispose':
          controller.dispose();
          break;
      }
      response.complete(record('Late title', 'Late overview'));
      await pending;
      expect(controller.memory, isNull);
      expect(commits, 0);
      if (retirement != 'dispose') controller.dispose();
    });
  }
  for (final wrong in [
    record('Wrong id', 'Wrong overview', id: 'm2'),
    record('Wrong owner', 'Wrong overview', uid: 'other')
  ]) {
    test('wrong id/owner response is not displayed or written', () async {
      var commits = 0;
      final controller = testRefresh(
          id: 'm1',
          authority: Authority(),
          presentationCurrent: () => true,
          loader: (id, {expectedAuthenticatedUid, exactAuthority}) async => wrong,
          onAccepted: (_) => commits++);
      await controller.load();
      expect(controller.memory, isNull);
      expect(commits, 0);
      controller.dispose();
    });
  }
  test('failed re-read removes prior success instead of presenting stale text as fresh', () async {
    var calls = 0;
    final controller = testRefresh(
        id: 'm1',
        authority: Authority(),
        presentationCurrent: () => true,
        loader: (id, {expectedAuthenticatedUid, exactAuthority}) async {
          if (++calls == 1) return record('First title', 'First overview');
          throw StateError('GET failed');
        });
    await controller.load();
    expect(controller.memory, isNotNull);
    await controller.load();
    expect(controller.memory, isNull);
    expect(controller.loading, isFalse);
    controller.dispose();
  });
  for (final race in ['replacement', 'in-place mutation', 'removal']) {
    test('real provider $race rejects delayed baseline without overwriting row or preview', () async {
      final provider = ConversationProvider();
      final baseline = versioned('old', 'Baseline', 'Old overview');
      final newer = versioned('new', 'Canonical new', 'New overview', parents: [
        {'id': 'old', 'title': 'Baseline', 'overview': 'Old overview', 'is_active': false}
      ]);
      provider.conversations = [baseline];
      final response = Completer<ServerConversation?>();
      var commits = 0;
      final controller = JournalPreviewRefresh(
          id: 'm1',
          authority: Authority(),
          presentationCurrent: () => true,
          currentRow: () => provider.conversations.isEmpty ? null : provider.conversations.single,
          loader: (id, {expectedAuthenticatedUid, exactAuthority}) => response.future,
          commitRow: (incoming, expected, witness) {
            commits++;
            if (provider.conversations.isEmpty ||
                !identical(provider.conversations.single, expected) ||
                journalPreviewRowWitness(expected) != witness) return false;
            provider.updateConversation(incoming);
            return true;
          });
      final pending = controller.load();
      if (race == 'replacement') provider.updateConversation(newer);
      if (race == 'in-place mutation') baseline.structured.title = 'Concurrent edited title';
      if (race == 'removal') provider.conversations.clear();
      response.complete(versioned('old', 'Baseline', 'Old overview'));
      await pending;
      expect(commits, 0);
      expect(controller.memory, isNull);
      if (race == 'replacement') expect(provider.conversations.single.structured.title, 'Canonical new');
      if (race == 'in-place mutation')
        expect(provider.conversations.single.structured.title, 'Concurrent edited title');
      if (race == 'removal') expect(provider.conversations, isEmpty);
      controller.dispose();
      provider.dispose();
    });
  }

  for (final invalid in [
    'older',
    'missing',
    'unrelated',
    'same version changed pair',
    'duplicate',
    'missing parent',
    'branch',
    'cycle',
    'active pair mismatch'
  ]) {
    test('$invalid detail cannot downgrade the real versioned provider row or preview', () async {
      final provider = ConversationProvider();
      final old = {'id': 'old', 'title': 'Old', 'overview': 'Old overview', 'is_active': false};
      final current = versioned('new', 'Canonical new', 'New overview', parents: [old]);
      provider.conversations = [current];
      var response = versioned('old', 'Old', 'Old overview');
      if (invalid == 'missing')
        response = ServerConversation.fromJson(record('Old', 'Old overview').toJson()
          ..remove('active_summary_version_id')
          ..remove('summary_versions'));
      if (invalid == 'unrelated') response = versioned('unrelated', 'Other', 'Other overview');
      if (invalid == 'same version changed pair') response = versioned('new', 'Old', 'Old overview');
      final parent = {'id': 'new', 'title': 'Canonical new', 'overview': 'New overview', 'is_active': false};
      if (invalid == 'duplicate') response = versioned('next', 'Next', 'Next overview', parents: [parent, parent]);
      if (invalid == 'missing parent')
        response = versioned('next', 'Next', 'Next overview', parents: [
          {...parent, 'based_on_version_id': 'absent'}
        ]);
      if (invalid == 'branch')
        response = ServerConversation.fromJson(versioned('next', 'Next', 'Next overview', parents: [parent]).toJson()
          ..['summary_versions'] = [
            parent,
            {
              'id': 'branch',
              'title': 'Branch',
              'overview': 'Branch overview',
              'is_active': false,
              'based_on_version_id': 'new'
            },
            {
              'id': 'next',
              'title': 'Next',
              'overview': 'Next overview',
              'is_active': true,
              'based_on_version_id': 'new'
            }
          ]);
      if (invalid == 'cycle')
        response = ServerConversation.fromJson(versioned('next', 'Next', 'Next overview').toJson()
          ..['summary_versions'] = [
            {
              'id': 'next',
              'title': 'Next',
              'overview': 'Next overview',
              'is_active': true,
              'based_on_version_id': 'next'
            },
            parent
          ]);
      if (invalid == 'active pair mismatch')
        response = ServerConversation.fromJson(versioned('next', 'Next', 'Next overview', parents: [parent]).toJson()
          ..['structured'] = {'title': 'Wrong', 'overview': 'Wrong overview', 'emoji': '', 'category': 'other'});
      var commits = 0;
      final controller = JournalPreviewRefresh(
          id: 'm1',
          authority: Authority(),
          presentationCurrent: () => true,
          currentRow: () => provider.conversations.single,
          loader: (id, {expectedAuthenticatedUid, exactAuthority}) async => response,
          commitRow: (incoming, expected, witness) {
            commits++;
            provider.updateConversation(incoming);
            return true;
          });
      await controller.load();
      expect(commits, 0);
      expect(controller.memory, isNull);
      expect(identical(provider.conversations.single, current), isTrue);
      controller.dispose();
      provider.dispose();
    });
  }

  test('opaque version IDs admit explicit multi-hop descendant and matching same version', () async {
    final provider = ConversationProvider();
    final current = versioned('zz-current', 'Current', 'Current overview');
    provider.conversations = [current];
    final descendant = versioned('aa-next', 'Fresh', 'Fresh overview', parents: [
      {'id': 'zz-current', 'title': 'Current', 'overview': 'Current overview', 'is_active': false},
      {
        'id': 'middle',
        'title': 'Middle',
        'overview': 'Middle overview',
        'is_active': false,
        'based_on_version_id': 'zz-current'
      }
    ]);
    final controller = JournalPreviewRefresh(
        id: 'm1',
        authority: Authority(),
        presentationCurrent: () => true,
        currentRow: () => provider.conversations.single,
        loader: (id, {expectedAuthenticatedUid, exactAuthority}) async => descendant,
        commitRow: (incoming, expected, witness) {
          if (!identical(provider.conversations.single, expected) || journalPreviewRowWitness(expected) != witness)
            return false;
          provider.updateConversation(incoming);
          return true;
        });
    await controller.load();
    expect(controller.memory!.activeSummaryVersionId, 'aa-next');
    expect(provider.conversations.single.structured.title, 'Fresh');
    await controller.load();
    expect(controller.memory!.activeSummaryVersionId, 'aa-next');
    controller.dispose();
    provider.dispose();
  });

  for (final reentrant in [false, true]) {
    test('provider update ${reentrant ? 'during commit' : 'after success'} never leaves a stale accepted preview',
        () async {
      final provider = ConversationProvider();
      provider.conversations = [
        ServerConversation.fromJson(record('Baseline', 'Baseline').toJson()
          ..remove('active_summary_version_id')
          ..remove('summary_versions'))
      ];
      final response = record('Fresh', 'Fresh overview');
      final replacement = versioned('replacement', 'Concurrent', 'Concurrent overview');
      var didReplace = false;
      if (reentrant)
        provider.addListener(() {
          if (!didReplace) {
            didReplace = true;
            provider.updateConversation(replacement);
          }
        });
      final controller = JournalPreviewRefresh(
          id: 'm1',
          authority: Authority(),
          presentationCurrent: () => true,
          currentRow: () => provider.conversations.single,
          loader: (id, {expectedAuthenticatedUid, exactAuthority}) async => response,
          commitRow: (incoming, expected, witness) {
            if (!identical(provider.conversations.single, expected) || journalPreviewRowWitness(expected) != witness)
              return false;
            provider.updateConversation(incoming);
            return true;
          });
      await controller.load();
      if (!reentrant) {
        expect(controller.memory!.structured.title, 'Fresh');
        provider.updateConversation(replacement);
      }
      expect(controller.memory, isNull);
      expect(provider.conversations.single.structured.title, 'Concurrent');
      controller.dispose();
      provider.dispose();
    });
  }

  testWidgets('actual preview moves from pending to both refreshed fields, then clears on account retirement',
      (tester) async {
    final response = Completer<ServerConversation?>(), authority = Authority();
    final controller = testRefresh(
        id: 'm1',
        authority: authority,
        presentationCurrent: () => true,
        loader: (id, {expectedAuthenticatedUid, exactAuthority}) => response.future);
    final pending = controller.load();
    await tester.pumpWidget(MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
            body: RefreshedJournalPreview(
                controller: controller,
                titleFor: (m) => m.structured.title,
                onClose: () {},
                onFullConversation: (_) {}))));
    expect(find.text('Enriched title'), findsNothing);
    expect(find.text('Showing saved preview while checking the latest summary…'), findsOneWidget);
    expect(find.text('Baseline'), findsWidgets);
    response.complete(record('Enriched title', 'Enriched overview'));
    await pending;
    await tester.pump();
    expect(find.text('Enriched title'), findsOneWidget);
    expect(find.text('Enriched overview'), findsOneWidget);
    EllaAccountCommitBarrier.quiesceForAccountTransition();
    await tester.pump();
    expect(find.text('Enriched title'), findsNothing);
    expect(find.text('Enriched overview'), findsNothing);
    await tester.pumpWidget(const SizedBox.shrink());
    controller.dispose();
  });
}
