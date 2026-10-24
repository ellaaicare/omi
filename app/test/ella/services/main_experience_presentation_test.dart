import 'dart:io';
import 'dart:ui' show ImageByteFormat;
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:omi/ella/widgets/ella_source_indicator.dart';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/ella/services/journal_preview_refresh.dart';
import 'package:omi/ella/upstream_capture/ella_home_transcript_panel.dart';
import 'package:omi/ella/widgets/quiet_memory_preview.dart';
import 'package:omi/ella/widgets/refreshed_journal_preview.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'journal_preview_refresh_test.dart' show Authority, record;
import '../widgets/summary_read_action_contrast_test.dart' show checkPaint;

Widget shell(Widget child, {bool dark = false, double scale = 1}) => MaterialApp(
      theme: dark ? ellaDarkThemeData() : ellaThemeData(),
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: MediaQuery(
          data: MediaQueryData(size: const Size(390, 844), textScaler: TextScaler.linear(scale)),
          child: RepaintBoundary(key: const Key('experience-mock'), child: Scaffold(body: child))),
    );
void phone(WidgetTester tester) {
  tester.view.physicalSize = const Size(390, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
}

Future<void> saveMock(WidgetTester tester, String name) async {
  final output = Platform.environment['ELLA_EXPERIENCE_MOCKS'];
  if (output == null) return;
  await tester.runAsync(() async {
    await Directory(output).create(recursive: true);
    final boundary = tester.renderObject<RenderRepaintBoundary>(find.byKey(const Key('experience-mock')));
    final image = await boundary.toImage(pixelRatio: 2);
    final bytes = await image.toByteData(format: ImageByteFormat.png);
    await File('$output/MOCK-$name.png').writeAsBytes(bytes!.buffer.asUint8List());
    image.dispose();
  });
}

void main() {
  setUpAll(() async {
    await (FontLoader('MaterialIcons')
          ..addFont(File('/Users/ellaai/dev/flutter/bin/cache/artifacts/material_fonts/MaterialIcons-Regular.otf')
              .readAsBytes()
              .then(ByteData.sublistView)))
        .load();
    await (FontLoader('.SF UI Text')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf')))
        .load();
  });
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });
  tearDown(EllaAccountCommitBarrier.quiesceForAccountTransition);
  for (final entry in <(List<int>, ConversationsReadFailure)>[
    ([255], ConversationsReadFailure.utf8),
    (utf8.encode('{secret-do-not-log'), ConversationsReadFailure.json),
    (utf8.encode('{}'), ConversationsReadFailure.schema),
    (utf8.encode('[null]'), ConversationsReadFailure.schema),
  ]) {
    test('HTTP200 decode classification ${entry.$2} retains failure not empty success', () {
      final decoded = decodeConversationsResponse(entry.$1);
      expect(decoded.succeeded, false);
      expect(decoded.statusCode, 200);
      expect(decoded.failure, entry.$2);
    });
  }
  test('valid unversioned and versioned rows share a successful list decode', () {
    final stock = record('Fictional saved note', 'Stock overview').toJson()
      ..remove('active_summary_version_id')
      ..['summary_versions'] = [];
    final decoded = decodeConversationsResponse(
        utf8.encode(jsonEncode([stock, record('Fictional current', 'Current overview', id: 'second').toJson()])));
    expect(decoded.succeeded, true);
    expect(decoded.conversations.length, 2);
    expect(decoded.failure, null);
  });
  for (final dark in [false, true]) {
    for (final scale in [1.0, 3.5]) {
      testWidgets('compact strip has labelled action and no diagnostics box $dark $scale', (tester) async {
        tester.view.physicalSize = const Size(390, 844);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        var opens = 0;
        await tester.pumpWidget(shell(
            Center(
                child: EllaHomeTranscriptCard(
                    status: 'Not recording',
                    lastAudio: 'Last audio: not observed',
                    transcription: 'Transcription not confirmed',
                    preview: '',
                    onOpen: () => opens++)),
            dark: dark,
            scale: scale));
        expect(find.text('Last audio: not observed'), findsNothing);
        expect(find.text('No live transcript yet'), findsNothing);
        expect(find.text(scale >= 2 ? 'Words' : 'Current transcript'), findsOneWidget);
        final action = find.byKey(const Key('home-open-transcript'));
        expect(action.hitTestable(), findsOneWidget);
        expect(tester.getSize(action).height, greaterThanOrEqualTo(48));
        final label = find.text(scale >= 2 ? 'Words' : 'Current transcript');
        final paragraph =
            tester.renderObject<RenderParagraph>(find.descendant(of: label, matching: find.byType(RichText)));
        final labelText = scale >= 2 ? 'Words' : 'Current transcript';
        final actionBounds = tester.getRect(action).inflate(.5);
        for (final box
            in paragraph.getBoxesForSelection(TextSelection(baseOffset: 0, extentOffset: labelText.length))) {
          expect(actionBounds.contains(paragraph.localToGlobal(Offset(box.left, box.top))), true);
          expect(actionBounds.contains(paragraph.localToGlobal(Offset(box.right, box.bottom))), true);
        }
        expect(tester.getSize(find.byType(EllaHomeTranscriptCard)).height, lessThanOrEqualTo(scale >= 2 ? 240 : 64));
        final health = find.byKey(const Key('home-transcript-health'));
        final healthParagraph = tester.renderObject<RenderParagraph>(health);
        final healthBounds = tester.getRect(find.byKey(const Key('home-transcript-preview-scroll'))).inflate(.5);
        for (final box in healthParagraph.getBoxesForSelection(const TextSelection(baseOffset: 0, extentOffset: 13))) {
          expect(healthBounds.contains(healthParagraph.localToGlobal(Offset(box.left, box.top))), true);
          expect(healthBounds.contains(healthParagraph.localToGlobal(Offset(box.right, box.bottom))), true);
        }
        if (scale >= 2) {
          // The complete word must fit without splitting across lines.
          expect(healthParagraph.getBoxesForSelection(const TextSelection(baseOffset: 4, extentOffset: 13)).length, 1);
        }
        await saveMock(tester, 'strip-$dark-$scale');
        await tester.tap(action);
        expect(opens, 1);
        expect(tester.takeException(), null);
      });
      testWidgets('failed current read preserves owned overview and usable retry $dark $scale', (tester) async {
        phone(tester);
        var row = record('Fictional library', 'The saved fictional overview remains readable.');
        final authority = Authority();
        final refresh = JournalPreviewRefresh(
            id: row.id,
            authority: authority,
            presentationCurrent: () => true,
            currentRow: () => row,
            loader: (id, {expectedAuthenticatedUid, exactAuthority}) async => null,
            commitRow: (incoming, expected, witness) {
              row = incoming;
              return true;
            });
        await refresh.load();
        await tester.pumpWidget(shell(
            RefreshedJournalPreview(
                controller: refresh, titleFor: (r) => r.structured.title, onClose: () {}, onFullConversation: (_) {}),
            dark: dark,
            scale: scale));
        expect(find.text(row.structured.overview), findsOneWidget);
        expect(find.text('Showing saved preview. The latest summary could not be verified.'), findsOneWidget);
        expect(find.byType(FilledButton), findsOneWidget);
        checkPaint(tester, find.byType(FilledButton), 'Try Again');
        expect(refresh.memory, null);
        await saveMock(tester, 'saved-preview-$dark-$scale');
        authority.current = false;
        await tester.pump(const Duration(milliseconds: 251));
        expect(find.text(row.structured.overview), findsNothing);
        await tester.pumpWidget(const SizedBox());
        refresh.dispose();
      });
    }
  }
  for (final connector in [false, true])
    testWidgets(
        'saved enhanced response appears in preview and row, survives ordinary reread with provenance connector=$connector',
        (tester) async {
      phone(tester);
      var row = record('Fictional library', 'Saved generated overview');
      final source = row.toJson()
        ..['status'] = 'completed'
        ..['active_summary_version_id'] = 'enhanced'
        ..['structured'] = {
          'title': 'Enhanced fictional library',
          'overview': 'Enhanced saved overview from approved fictional source.',
          'emoji': '',
          'category': 'other'
        }
        ..['summary_versions'] = [
          {
            'id': 'enriched-version',
            'title': row.structured.title,
            'overview': row.structured.overview,
            'is_active': false
          },
          {
            'id': 'enhanced',
            'kind': connector ? 'mcp_enriched' : 'hermes_enriched',
            'based_on_version_id': 'enriched-version',
            'title': 'Enhanced fictional library',
            'overview': 'Enhanced saved overview from approved fictional source.',
            'is_active': true
          },
        ]
        ..['enrichment_state'] = connector
            ? <String, dynamic>{}
            : {
                'result_summary_version_id': 'enhanced',
                'status': 'writeback_applied',
                'pending': false,
                'canonical_status': 'completed',
                'kind': 'hermes_enriched',
                'source': 'hermes_parallel'
              };
      var reads = 0;
      final refresh = JournalPreviewRefresh(
          id: row.id,
          authority: Authority(),
          presentationCurrent: () => true,
          currentRow: () => row,
          loader: (id, {expectedAuthenticatedUid, exactAuthority}) async {
            reads++;
            return ServerConversation.fromJson(source);
          },
          commitRow: (incoming, expected, witness) {
            if (!identical(row, expected) || journalPreviewRowWitness(row) != witness) return false;
            row = incoming;
            return true;
          });
      await refresh.load();
      Widget preview() => shell(RefreshedJournalPreview(
          controller: refresh, titleFor: (r) => r.structured.title, onClose: () {}, onFullConversation: (_) {}));
      await tester.pumpWidget(preview());
      expect(find.text('Enhanced fictional library'), findsOneWidget);
      expect(find.text(connector ? 'Connector summary' : 'Hermes summary'), findsOneWidget);
      expect(find.text('Enhanced saved overview from approved fictional source.'), findsOneWidget);
      await saveMock(tester, 'enhanced-preview-$connector');
      await refresh.load();
      await tester.pump();
      expect(reads, 2);
      expect(find.text(connector ? 'Connector summary' : 'Hermes summary'), findsOneWidget);
      await tester.pumpWidget(shell(QuietMemoryRow(conversation: row, title: row.structured.title, onOpen: () {})));
      expect(find.text('Enhanced saved overview from approved fictional source.'), findsOneWidget);
      expect(find.text(connector ? 'Connector summary' : 'Hermes summary'), findsOneWidget);
      await saveMock(tester, 'enhanced-row-$connector');
      await tester.pumpWidget(const SizedBox());
      refresh.dispose();
    });
  for (final defect in ['wrong-pair', 'wrong-version', 'wrong-kind', 'duplicate-active']) {
    test('connector label refuses $defect without fabricating Hermes state', () {
      final raw = record('Fictional', 'Fictional overview').toJson();
      final active = Map<String, dynamic>.from((raw['summary_versions'] as List).single as Map)
        ..['kind'] = 'mcp_enriched';
      raw['summary_versions'] = [active];
      if (defect == 'wrong-pair') active['overview'] = 'Contradiction';
      if (defect == 'wrong-version') active['id'] = 'different';
      if (defect == 'wrong-kind') active['kind'] = 'generated';
      if (defect == 'duplicate-active') raw['summary_versions'] = [active, active];
      final row = ServerConversation.fromJson(raw);
      expect(hasCurrentConnectorSummary(row), false);
      expect(hasCurrentHermesSummary(row), false);
    });
  }
  testWidgets('retained view retires after foreign equivalent-looking row replacement', (tester) async {
    phone(tester);
    var row = record('Fictional library', 'Owned saved overview');
    final refresh = JournalPreviewRefresh(
        id: row.id,
        authority: Authority(),
        presentationCurrent: () => true,
        currentRow: () => row,
        loader: (id, {expectedAuthenticatedUid, exactAuthority}) async => null,
        commitRow: (incoming, expected, witness) => false);
    await refresh.load();
    await tester.pumpWidget(shell(RefreshedJournalPreview(
        controller: refresh, titleFor: (r) => r.structured.title, onClose: () {}, onFullConversation: (_) {})));
    expect(find.text('Owned saved overview'), findsOneWidget);
    row = record('Fictional library', 'Owned saved overview', uid: 'foreign');
    await tester.pump(const Duration(milliseconds: 251));
    expect(find.text('Owned saved overview'), findsNothing);
    await tester.pumpWidget(const SizedBox());
    refresh.dispose();
  });
  testWidgets('row republished without read failure prompts refresh without inventing a failed GET', (tester) async {
    phone(tester);
    var row = record('Fictional library', 'Saved fictional overview');
    final refresh = JournalPreviewRefresh(
        id: row.id,
        authority: Authority(),
        presentationCurrent: () => true,
        currentRow: () => row,
        loader: (id, {expectedAuthenticatedUid, exactAuthority}) async =>
            record('Fictional library', 'Saved fictional overview'),
        commitRow: (incoming, expected, witness) {
          row = incoming;
          return true;
        });
    await refresh.load();
    expect(refresh.memory, isNotNull);
    await tester.pumpWidget(shell(RefreshedJournalPreview(
        controller: refresh, titleFor: (r) => r.structured.title, onClose: () {}, onFullConversation: (_) {})));
    row = record('Fictional library', 'Saved fictional overview');
    await tester.pump(const Duration(milliseconds: 251));
    expect(refresh.failure, null);
    expect(refresh.memory, null);
    expect(find.text('Showing saved preview. Refresh to check this version.'), findsOneWidget);
    expect(find.text('Showing saved preview. The latest summary could not be verified.'), findsNothing);
    await tester.pumpWidget(const SizedBox());
    refresh.dispose();
  });
}
