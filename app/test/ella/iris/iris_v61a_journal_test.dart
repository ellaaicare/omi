import 'dart:async';
import 'dart:io';
import 'dart:ui' as ui;
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/schema/action_item.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/iris_journal_reminders.dart';
import 'package:omi/ella/widgets/iris_journal_reminder_row.dart';
import 'package:omi/ella/widgets/iris_journal_presentation.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class _Owner implements ExactAccountAuthorityVerifier {
  _Owner(this.uid);
  @override
  final String uid;
  bool current = true;
  @override
  bool isExactCurrent() => current;
}

ActionItemWithMetadata reminder(String id, DateTime? due, {bool completed = false}) => ActionItemWithMetadata(
    id: id,
    description: 'Call Sam about Friday plans',
    completed: completed,
    createdAt: DateTime(2026, 10, 1),
    dueAt: due);
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() async {
    await (FontLoader('Manrope')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf')))
        .load();
    await (FontLoader('packages/font_awesome_flutter/FontAwesomeRegular')
          ..addFont(rootBundle.load('packages/font_awesome_flutter/lib/fonts/Font-Awesome-7-Free-Regular-400.otf')))
        .load();
    await (FontLoader('packages/font_awesome_flutter/FontAwesomeSolid')
          ..addFont(rootBundle.load('packages/font_awesome_flutter/lib/fonts/Font-Awesome-7-Free-Solid-900.otf')))
        .load();
    var cache = File(Platform.resolvedExecutable).parent;
    while (!File('${cache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf').existsSync()) {
      cache = cache.parent;
    }
    await (FontLoader('MaterialIcons')
          ..addFont(File('${cache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf')
              .readAsBytes()
              .then((bytes) => ByteData.sublistView(Uint8List.fromList(bytes)))))
        .load();
  });
  testWidgets('calendar labels use complete dates, current year omission and DST-safe day difference', (tester) async {
    late BuildContext context;
    await tester.pumpWidget(MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Builder(builder: (c) {
          context = c;
          return const SizedBox.shrink();
        })));
    final current = DateTime(2026, 10, 11, 12);
    expect(irisJournalDayLabel(context, current, current), 'Today');
    expect(irisJournalDayLabel(context, DateTime(2026, 10, 10), current), 'Yesterday');
    expect(irisJournalDayLabel(context, DateTime(2026, 10, 8), current), 'Thursday');
    expect(irisJournalDayLabel(context, DateTime(2026, 10, 1), current), 'October 1');
    expect(irisJournalDayLabel(context, DateTime(2025, 10, 1), current), 'October 1, 2025');
  });
  testWidgets('load-more appends bounded exact-owner pages, retains records on failure and retires callbacks',
      (tester) async {
    final owner = _Owner('a');
    final offsets = <int>[];
    var fail = false;
    final model = IrisJournalReminders(
        authorityProvider: () => owner,
        readPage: (offset, authority) async {
          expect(authority, same(owner));
          offsets.add(offset);
          if (fail) throw StateError('synthetic read unavailable');
          return ActionItemsResponse(actionItems: [reminder('$offset', null)], hasMore: true);
        });
    await model.load();
    final old = model.captureVisibility();
    await model.loadMore();
    expect(offsets, List.generate(10, (i) => i));
    expect(model.items.map((item) => item.id), List.generate(10, (i) => '$i'));
    expect(old(), false);
    expect(model.hasMore, true);
    fail = true;
    await model.loadMore();
    expect(model.items, hasLength(10));
    expect(model.unavailable, true);
    owner.current = false;
    await tester.pump(const Duration(milliseconds: 251));
    expect(model.items, isEmpty);
    model.dispose();
  });
  final now = DateTime(2026, 10, 10, 12);
  test('Coming up asc / Past desc; completion and undated records retained', () {
    final groups = irisReminderGroups([
      reminder('later', now.add(const Duration(days: 2))),
      reminder('oldest', now.subtract(const Duration(days: 2))),
      reminder('nearest', now.add(const Duration(hours: 2)), completed: true),
      reminder('recent', now.subtract(const Duration(hours: 2))),
      reminder('undated', null)
    ], now);
    expect(groups.coming.map((r) => r.id), ['nearest', 'later']);
    expect(groups.past.map((r) => r.id), ['recent', 'oldest']);
    expect(groups.noDue.map((r) => r.id), ['undated']);
  });
  testWidgets('Held reminder response and retained row lease cannot cross A/B/A or silent retirement', (tester) async {
    final a = _Owner('a'), b = _Owner('b');
    var owner = a;
    var calls = 0;
    final first = Completer<ActionItemsResponse>();
    final model = IrisJournalReminders(
        authorityProvider: () => owner,
        readPage: (_, authority) {
          calls++;
          return calls == 1
              ? first.future
              : Future.value(ActionItemsResponse(actionItems: [reminder(authority.uid, null)], hasMore: false));
        });
    final pending = model.load();
    a.current = false;
    owner = b;
    await model.load();
    final bLease = model.captureVisibility();
    expect(model.items.single.id, 'b');
    first.complete(ActionItemsResponse(actionItems: [reminder('late-a', null)], hasMore: false));
    await pending;
    expect(model.items.single.id, 'b');
    b.current = false;
    owner = _Owner('a');
    expect(bLease(), false);
    await tester.pump(const Duration(milliseconds: 251));
    expect(model.items, isEmpty);
    expect(model.loading, false);
    await model.load();
    expect(model.items.single.id, 'a');
    expect(bLease(), false);
    model.dispose();
  });
  testWidgets('Unknown pagination never looks complete and refresh retires old callbacks', (tester) async {
    final owner = _Owner('a');
    var pages = 0;
    final model = IrisJournalReminders(
        authorityProvider: () => owner,
        readPage: (offset, a) async {
          pages++;
          return ActionItemsResponse(actionItems: [reminder('$offset', null)], hasMore: true);
        });
    await model.load();
    expect(pages, 5);
    expect(model.hasMore, true);
    final lease = model.captureVisibility();
    await model.load();
    expect(lease(), false);
    model.dispose();
  });
  for (final dark in [false, true])
    for (final scale in [1.0, 1.28, 3.5])
      for (final filter in [false, true]) {
        testWidgets('Reminder said/due columns, wrapping and contrast $dark $scale filter=$filter', (tester) async {
          tester.view.physicalSize = const Size(320, 844);
          tester.view.devicePixelRatio = 1;
          addTearDown(tester.view.resetPhysicalSize);
          addTearDown(tester.view.resetDevicePixelRatio);
          final key = GlobalKey();
          final item = reminder('row', DateTime(2026, 10, 9, 17));
          await tester.pumpWidget(MaterialApp(
              theme: dark ? ellaDarkThemeData() : ellaThemeData(),
              localizationsDelegates: AppLocalizations.localizationsDelegates,
              supportedLocales: AppLocalizations.supportedLocales,
              builder: (c, child) =>
                  MediaQuery(data: MediaQuery.of(c).copyWith(textScaler: TextScaler.linear(scale)), child: child!),
              home: Scaffold(
                  body: RepaintBoundary(
                      key: key,
                      child: SingleChildScrollView(
                          child: IrisJournalReminderRow(
                              item: item,
                              now: now,
                              saidAt: DateTime(2026, 10, 8, 10),
                              dueColumn: filter,
                              onOpen: () {}))))));
          await tester.pump();
          expect(tester.takeException(), isNull);
          final date = tester.widget<Text>(find.byKey(const Key('iris-reminder-date-row')));
          expect(date.data, contains(filter ? 'Fri' : '10:00'));
          expect(date.style!.fontSize, 16);
          if (filter)
            expect(find.byKey(const Key('iris-reminder-due-row')), findsNothing);
          else {
            final due = tester.widget<Text>(find.byKey(const Key('iris-reminder-due-row')));
            expect(due.data, startsWith('Was due'));
            expect(due.style!.fontSize, 16);
            expect(due.overflow, isNot(TextOverflow.ellipsis));
          }
          if (scale != 3.5)
            await tester.runAsync(() async {
              final render = key.currentContext!.findRenderObject()! as RenderRepaintBoundary;
              final picture = await render.toImage();
              final bytes = await picture.toByteData(format: ui.ImageByteFormat.png);
              final out = File(
                  '/Users/ellaai/Documents/Codex/2026-10-07/task-4/iris-v61a-reminder-${dark ? 'dark' : 'light'}-$scale-${filter ? 'filter' : 'days'}.png');
              await out.writeAsBytes(bytes!.buffer.asUint8List());
              picture.dispose();
            });
          await tester.pumpWidget(const SizedBox());
        });
      }
}
