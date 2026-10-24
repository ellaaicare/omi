import 'dart:io';
import 'dart:ui' as ui;
import 'package:flutter/rendering.dart';
import 'package:flutter/material.dart';
import 'package:omi/ella/widgets/retained_summary_card.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/services/retained_summary.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

Map<String, dynamic> version(String id, {bool active = false, String source = 'omi'}) => {
      'id': id,
      'created_at': '2026-10-01T10:00:00Z',
      'source': source,
      'kind': 'generated',
      'title': ' Exact title ',
      'overview': '<script>plain stored text</script>\nOriginal overview',
      'emoji': '🌱',
      'category': 'personal',
      'based_on_version_id': null,
      'is_active': active,
    };
Map<String, dynamic> memoryJson([Object? versions]) => {
      'id': 'm1',
      'created_at': '2026-10-01T10:00:00Z',
      'structured': {'title': 'Active summary', 'overview': 'Current text', 'emoji': '🌱', 'category': 'personal'},
      'summary_versions': versions ?? [version('s1'), version('a1', active: true, source: 'ella')],
      'active_summary_version_id': 'a1',
      'transcript_segments': [],
    };

class Authority implements AccountCommitAuthority {
  bool current = true;
  @override
  String get uid => 'owner';
  @override
  bool isCurrent() => current;
  @override
  bool isExactCurrent() => current;
}

void main() {
  setUpAll(() async {
    await ui.loadFontFromList(await File('assets/fonts/Manrope-400.ttf').readAsBytes(), fontFamily: 'Roboto');
  });
  tearDown(EllaAccountCommitBarrier.quiesceForAccountTransition);
  test('parser retains all historical fields and does not alias response metadata', () {
    final raw = memoryJson();
    final before = jsonEncode(raw['summary_versions']);
    final model = ServerConversation.fromJson(raw);
    (raw['summary_versions'] as List).clear();
    expect(jsonEncode(model.toJson()['summary_versions']), before);
    expect(model.structured.title, 'Active summary');
  });
  test('malformed active ID is retained and rejected without string coercion', () {
    final raw = memoryJson()..['active_summary_version_id'] = 123;
    final model = ServerConversation.fromJson(raw);
    expect(model.toJson()['active_summary_version_id'], 123);
    expect(RetainedSummaryCatalog.parse(model.summaryVersions, model.summaryVersionActiveAuthority).error,
        'active_authority_invalid');
  });
  test('single eligible auto selection preserves exact text without model mutation', () {
    final raw = memoryJson();
    final before = jsonEncode(raw);
    final catalog = RetainedSummaryCatalog.parse(raw['summary_versions'], 'a1');
    expect(catalog.select()!.title, ' Exact title ');
    expect(catalog.select()!.overview, '<script>plain stored text</script>\nOriginal overview');
    expect(jsonEncode(raw), before);
    expect(catalog.select('a1'), isNull);
  });
  test('multiple stock versions require explicit eligible ID; no newest inference', () {
    final catalog = RetainedSummaryCatalog.parse([version('s2'), version('s1', active: true)], 's1');
    expect(catalog.select(), isNull);
    expect(catalog.select('s2')!.id, 's2');
    expect(catalog.select('unknown'), isNull);
    expect(catalog.select(' '), isNull);
  });
  for (final raw in [null, [], 'wrong']) {
    test('missing versions fail closed: $raw', () => expect(RetainedSummaryCatalog.parse(raw, 'a1').select(), isNull));
  }
  test('duplicates malformed IDs and nonboolean activity fail closed', () {
    for (final list in [
      [version('s1'), version('s1', active: true)],
      [version(' ')],
      [version('s1')..['id'] = 7],
      [version('s1')..['is_active'] = 1],
    ]) {
      expect(RetainedSummaryCatalog.parse(list, 's1').versions, isEmpty);
    }
  });
  test('missing mismatched and multiple active authorities fail closed', () {
    for (final pair in [
      ([version('s1', active: true)], null),
      ([version('s1', active: true)], 'wrong'),
      ([version('s1')], 's1'),
      ([version('s1', active: true), version('s2', active: true)], 's1'),
    ]) {
      expect(RetainedSummaryCatalog.parse(pair.$1, pair.$2).select(), isNull);
    }
  });
  test('invalid stock content fails closed', () {
    expect(RetainedSummaryCatalog.parse([version('s1', active: true)..['overview'] = ' '], 's1').select(), isNull);
  });
  test('authenticated GET gets exact lease; selections never fetch or mutate canonical model', () async {
    final authority = Authority();
    int calls = 0;
    final model = ServerConversation.fromJson(memoryJson());
    final before = jsonEncode(model.toJson());
    final c = RetainedSummaryController(
        authority: () => authority,
        loader: (id, {expectedAuthenticatedUid, exactAuthority}) async {
          calls++;
          expect(id, 'm1');
          expect(expectedAuthenticatedUid, 'owner');
          expect(exactAuthority!.isExactCurrent(), isTrue);
          return model;
        });
    await c.load('m1');
    expect(c.selected!.id, 's1');
    c.select('s1');
    c.select('a1');
    c.select('unknown');
    expect(calls, 1);
    expect(jsonEncode(model.toJson()), before);
    c.dispose();
  });
  test('same UID generation transition immediately revokes displayed retained text', () async {
    final a = Authority();
    final c = RetainedSummaryController(
        authority: () => a,
        loader: (id, {expectedAuthenticatedUid, exactAuthority}) async => ServerConversation.fromJson(memoryJson()));
    await c.load('m1');
    expect(c.selected, isNotNull);
    EllaAccountCommitBarrier.quiesceForAccountTransition();
    expect(c.selected, isNull);
    expect(c.versions, isEmpty);
    c.dispose();
  });
  test('stale GET response after account transition cannot publish', () async {
    final a = Authority();
    final response = Completer<ServerConversation?>();
    final c = RetainedSummaryController(
        authority: () => a, loader: (id, {expectedAuthenticatedUid, exactAuthority}) => response.future);
    final pending = c.load('m1');
    EllaAccountCommitBarrier.quiesceForAccountTransition();
    response.complete(ServerConversation.fromJson(memoryJson()));
    await pending;
    expect(c.selected, isNull);
    c.dispose();
  });
  test('no current authority performs no GET', () async {
    int calls = 0;
    final c = RetainedSummaryController(
        authority: () => null,
        loader: (id, {expectedAuthenticatedUid, exactAuthority}) async {
          calls++;
          return null;
        });
    await c.load('m1');
    expect(calls, 0);
    c.dispose();
  });
  test('wrong memory ID and contradictory body UID cannot display', () async {
    for (final raw in [
      memoryJson()..['id'] = 'other',
      memoryJson()..['uid'] = 'other-owner',
      memoryJson()..['uid'] = null
    ]) {
      final a = Authority();
      final c = RetainedSummaryController(
          authority: () => a,
          loader: (id, {expectedAuthenticatedUid, exactAuthority}) async => ServerConversation.fromJson(raw));
      await c.load('m1');
      expect(c.selected, isNull);
      c.dispose();
    }
  });
  test('new memory request supersedes earlier outstanding response', () async {
    final a = Authority();
    final response = Completer<ServerConversation?>();
    int calls = 0;
    final c = RetainedSummaryController(
        authority: () => a,
        loader: (id, {expectedAuthenticatedUid, exactAuthority}) async {
          calls++;
          if (calls == 1) return response.future;
          return ServerConversation.fromJson(memoryJson()..['id'] = id);
        });
    final first = c.load('old');
    await c.load('m1');
    response.complete(ServerConversation.fromJson(memoryJson()..['id'] = 'old'));
    await first;
    expect(c.selected!.id, 's1');
    c.dispose();
  });
  testWidgets('historical card renders literal stored text; selection performs no extra GET and transition hides it',
      (tester) async {
    final a = Authority();
    int calls = 0;
    final raw = memoryJson([version('s1'), version('s2'), version('a1', active: true, source: 'ella')]);
    final c = RetainedSummaryController(
        authority: () => a,
        loader: (id, {expectedAuthenticatedUid, exactAuthority}) async {
          calls++;
          return ServerConversation.fromJson(raw);
        });
    await tester.pumpWidget(MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: Scaffold(
          body: SingleChildScrollView(
              child: RepaintBoundary(
                  key: const Key('retained-summary-render'),
                  child: ColoredBox(
                      color: Colors.white, child: RetainedSummaryCard(conversationId: 'm1', controller: c))))),
    ));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Original summary (retained version)'));
    await tester.pumpAndSettle();
    expect(find.text('May describe an earlier transcript revision.'), findsOneWidget);
    expect(find.byType(SelectableText), findsNothing);
    await tester.tap(find.text('s2'));
    await tester.pumpAndSettle();
    expect(find.text('Retained version: s2'), findsOneWidget);
    expect(find.text('<script>plain stored text</script>\nOriginal overview'), findsOneWidget);
    final renderPath = Platform.environment['IRIS_RETAINED_SUMMARY_RENDER'];
    if (renderPath != null) {
      final boundary = tester.renderObject<RenderRepaintBoundary>(find.byKey(const Key('retained-summary-render')));
      await tester.runAsync(() async {
        final image = await boundary.toImage(pixelRatio: 1);
        final pixels = await image.toByteData(format: ui.ImageByteFormat.png);
        await File(renderPath).writeAsBytes(pixels!.buffer.asUint8List());
        image.dispose();
      });
    }
    expect(calls, 1);
    EllaAccountCommitBarrier.quiesceForAccountTransition();
    await tester.pumpAndSettle();
    expect(find.byType(SelectableText), findsNothing);
    c.dispose();
  });
  testWidgets('same UID authority retirement without barrier removes already rendered historical text within 250ms',
      (tester) async {
    final a = Authority();
    final c = RetainedSummaryController(
        authority: () => a,
        loader: (id, {expectedAuthenticatedUid, exactAuthority}) async => ServerConversation.fromJson(memoryJson()));
    await tester.pumpWidget(MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: Scaffold(body: SingleChildScrollView(child: RetainedSummaryCard(conversationId: 'm1', controller: c))),
    ));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Original summary (retained version)'));
    await tester.pumpAndSettle();
    expect(find.byType(SelectableText), findsNWidgets(2));
    a.current = false;
    await tester.pump(const Duration(milliseconds: 250));
    await tester.pump();
    expect(find.byType(SelectableText), findsNothing);
    c.dispose();
  });
}
