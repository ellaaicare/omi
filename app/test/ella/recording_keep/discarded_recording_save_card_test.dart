import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/recording_keep/discarded_recording_save_card.dart';
import 'package:omi/ella/recording_keep/explicit_recording_keep.dart';
import 'explicit_recording_keep_test.dart' as fixture;

Future<void> showCard(WidgetTester tester, fixture.Fake fake,
    ValueNotifier<int> changes, {bool Function()? eligible, double scale = 1}) async {
  await tester.pumpWidget(MaterialApp(home: Scaffold(body: MediaQuery(
    data: MediaQueryData(textScaler: TextScaler.linear(scale)),
    child: SingleChildScrollView(child: DiscardedRecordingSaveCard(
      session: fake.session, authorityChanges: changes,
      eligible: eligible ?? () => true,
    )),
  ))));
}

void main() {
  testWidgets('mount exposes explicit Save with no automatic request', (tester) async {
    final fake = fixture.Fake();
    final changes = ValueNotifier(0);
    await showCard(tester, fake, changes);
    expect(find.text('Save recording'), findsOneWidget);
    expect(find.text('Not summarized'), findsOneWidget);
    expect(fake.requests, isEmpty);
  });

  testWidgets('explicit tap confirms retention without summary claim', (tester) async {
    final fake = fixture.Fake();
    await showCard(tester, fake, ValueNotifier(0));
    await tester.tap(find.text('Save recording'));
    await tester.pumpAndSettle();
    expect(fake.requests.map((r) => r.method), ['GET', 'POST']);
    expect(fake.requests.where((r) => r.path.contains('keep')).length, 2);
    expect(find.text('Saved recording'), findsOneWidget);
    expect(find.text('Not summarized'), findsOneWidget);
    expect(find.text('Processing paused'), findsNothing);
    expect(find.text('Save recording'), findsNothing);
  });

  testWidgets('rapid double tap makes one write', (tester) async {
    final fake = fixture.Fake();
    final response = Completer<KeepResponse>();
    fake.answer = (request) async {
      if (request.method == 'POST') return await response.future;
      return KeepResponse(200, fixture.read());
    };
    await showCard(tester, fake, ValueNotifier(0));
    await tester.tap(find.text('Save recording'));
    await tester.tap(find.byKey(const Key('save-discarded-recording')));
    await tester.pump();
    expect(fake.requests.where((r) => r.method == 'POST').length, 1);
    response.complete(KeepResponse(200, fixture.saved()));
    await tester.pumpAndSettle();
    expect(find.text('Saved recording'), findsOneWidget);
  });

  testWidgets('unknown write offers GET-only status and never resends', (tester) async {
    final fake = fixture.Fake();
    fake.answer = (request) async {
      if (request.method == 'POST') throw TimeoutException('fixture');
      return KeepResponse(200, fixture.read());
    };
    await showCard(tester, fake, ValueNotifier(0));
    await tester.tap(find.text('Save recording'));
    await tester.pumpAndSettle();
    expect(find.text('Check save status'), findsOneWidget);
    await tester.tap(find.text('Check save status'));
    await tester.pumpAndSettle();
    expect(fake.requests.map((r) => r.method), ['GET', 'POST', 'GET']);
    expect(find.text('Saved recording'), findsNothing);
  });

  testWidgets('restored attempted intent cannot render a new Save', (tester) async {
    final fake = fixture.Fake();
    fake.intent.postAttempted = true;
    await showCard(tester, fake, ValueNotifier(0));
    expect(find.text('Check save status'), findsOneWidget);
    expect(find.text('Save recording'), findsNothing);
    await tester.tap(find.text('Check save status'));
    await tester.pumpAndSettle();
    expect(fake.requests.map((r) => r.method), ['GET']);
  });

  testWidgets('authority loss redacts saved status and never re-adopts owner', (tester) async {
    final fake = fixture.Fake();
    final changes = ValueNotifier(0);
    await fake.session.save();
    await showCard(tester, fake, changes);
    expect(find.text('Saved recording'), findsOneWidget);
    fake.current = false;
    changes.value++;
    await tester.pump();
    expect(find.text('Saved recording'), findsNothing);
    expect(find.byKey(const Key('discarded-save-retired')), findsOneWidget);
    fake.current = true;
    changes.value++;
    await tester.pump();
    expect(find.text('Saved recording'), findsNothing);
    expect(find.text('Save recording'), findsNothing);
  });

  testWidgets('owner change during read prevents POST and old result', (tester) async {
    final fake = fixture.Fake();
    final changes = ValueNotifier(0);
    final response = Completer<KeepResponse>();
    fake.answer = (_) => response.future;
    await showCard(tester, fake, changes);
    await tester.tap(find.text('Save recording'));
    await tester.pump();
    fake.current = false;
    changes.value++;
    response.complete(KeepResponse(200, fixture.read()));
    await tester.pumpAndSettle();
    expect(fake.requests.map((r) => r.method), ['GET']);
    expect(find.text('Saved recording'), findsNothing);
    expect(find.byKey(const Key('discarded-save-retired')), findsOneWidget);
  });

  testWidgets('empty or ineligible detail disables explicit action', (tester) async {
    final fake = fixture.Fake();
    await showCard(tester, fake, ValueNotifier(0), eligible: () => false);
    final button = tester.widget<FilledButton>(find.byKey(const Key('save-discarded-recording')));
    expect(button.onPressed, isNull);
    expect(fake.requests, isEmpty);
  });

  testWidgets('large text stays readable without summary or pause assumptions', (tester) async {
    final fake = fixture.Fake();
    await tester.binding.setSurfaceSize(const Size(320, 640));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await showCard(tester, fake, ValueNotifier(0), scale: 3.5);
    expect(tester.takeException(), isNull);
    expect(find.text('Not summarized'), findsOneWidget);
    expect(find.text('Processing paused'), findsNothing);
    expect(fake.requests, isEmpty);
  });
}
