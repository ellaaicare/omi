import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/recording_keep/explicit_recording_keep.dart';
import 'package:omi/ella/recording_keep/recording_keep_pages.dart';
import 'recording_keep_repository_test.dart' as r;
import 'explicit_recording_keep_test.dart' as f;

void main() {
  testWidgets('Journal follow-on shows recent discarded rows without normal summary filtering', (tester) async {
    final fixture = r.RepositoryFixture();
    await tester.pumpWidget(MaterialApp(home: RecordingKeepPage(
      repository: fixture.repo, authorityChanges: ValueNotifier(0))));
    await tester.pumpAndSettle();
    expect(find.text('Recording without summary'), findsOneWidget);
    expect(fixture.minted, 0);
    expect(fixture.requests, isEmpty);
    await tester.tap(find.text('Recording without summary'));
    await tester.pumpAndSettle();
    expect(find.text('Fictional test words'), findsOneWidget);
    expect(find.text('Save recording'), findsOneWidget);
    expect(fixture.minted, 0);
    expect(fixture.requests.every((request) => request.method == 'GET'), isTrue);
  });

  testWidgets('detail explicit Save persists receipt then Journal saved tab can reopen transcript', (tester) async {
    final fixture = r.RepositoryFixture();
    var kept = false;
    fixture.answerKeep = (request) async {
      if (request.method == 'POST') { kept = true; return KeepResponse(200, f.saved()); }
      return KeepResponse(200, f.read(kept: kept));
    };
    await tester.pumpWidget(MaterialApp(home: RecordingKeepPage(
      repository: fixture.repo, authorityChanges: ValueNotifier(0))));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Recording without summary'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Save recording'));
    await tester.pumpAndSettle();
    expect(find.text('Saved recording'), findsOneWidget);
    expect(find.text('Not summarized'), findsOneWidget);
    expect(fixture.requests.where((request) => request.method == 'POST').length, 1);
    await tester.pageBack();
    await tester.pumpAndSettle();
    await tester.tap(find.text('Saved recordings'));
    await tester.pumpAndSettle();
    expect(find.text('Saved recording'), findsOneWidget);
    await tester.tap(find.text('Saved recording'));
    await tester.pumpAndSettle();
    expect(find.text('Fictional test words'), findsOneWidget);
    expect(find.text('Not summarized'), findsOneWidget);
    expect(find.text('Save recording'), findsNothing);
    expect(fixture.requests.where((request) => request.method == 'POST').length, 1);
  });

  testWidgets('return after unknown write restores status check only, never a new UUID', (tester) async {
    final fixture = r.RepositoryFixture();
    fixture.answerKeep = (request) async {
      if (request.method == 'POST') throw StateError('fixture unknown');
      return KeepResponse(200, f.read());
    };
    await tester.pumpWidget(MaterialApp(home: RecordingKeepPage(
      repository: fixture.repo, authorityChanges: ValueNotifier(0))));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Recording without summary'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Save recording'));
    await tester.pumpAndSettle();
    expect(find.text('Check save status'), findsOneWidget);
    await tester.pageBack();
    await tester.pumpAndSettle();
    await tester.tap(find.text('Recording without summary'));
    await tester.pumpAndSettle();
    expect(find.text('Check save status'), findsOneWidget);
    expect(find.text('Save recording'), findsNothing);
    expect(fixture.minted, 1);
    expect(fixture.requests.where((request) => request.method == 'POST').length, 1);
  });

  testWidgets('account retirement redacts transcript and actions even if authority later returns', (tester) async {
    final fixture = r.RepositoryFixture();
    final changes = ValueNotifier(0);
    await tester.pumpWidget(MaterialApp(home: RecordingKeepDetailPage(
      repository: fixture.repo, cid: f.cid, authorityChanges: changes)));
    await tester.pumpAndSettle();
    expect(find.text('Fictional test words'), findsOneWidget);
    fixture.current = false;
    changes.value++;
    await tester.pumpAndSettle();
    expect(find.text('Fictional test words'), findsNothing);
    expect(find.text('Save recording'), findsNothing);
    fixture.current = true;
    changes.value++;
    await tester.pumpAndSettle();
    expect(find.text('Fictional test words'), findsNothing);
    expect(fixture.minted, 0);
  });

  testWidgets('saved index with stale current detail reports unavailable instead of stale content', (tester) async {
    final fixture = r.RepositoryFixture();
    fixture.answerKeep = (_) async => KeepResponse(200, f.read(kept: true, op: f.firstOperation));
    await tester.pumpWidget(MaterialApp(home: RecordingKeepDetailPage(
      repository: fixture.repo, cid: f.cid,
      expectedReceipt: KeepReceipt.fromJson(f.receipt(), f.cid), authorityChanges: ValueNotifier(0))));
    await tester.pumpAndSettle();
    expect(find.text('Fictional test words'), findsNothing);
    expect(find.text('Save recording'), findsNothing);
    expect(find.textContaining('could not be confirmed'), findsOneWidget);
  });
}
