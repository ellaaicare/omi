import 'dart:io';
import 'dart:async';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'dart:ui' as ui;
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/pages/chat/widgets/ai_message.dart';
import 'package:intl/intl.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/widgets/quiet_memory_preview.dart';
import 'package:omi/l10n/app_localizations.dart';

class _Owner implements ExactAccountAuthorityVerifier {
  @override
  final String uid = 'synthetic-owner';
  bool current = true;
  @override
  bool isExactCurrent() => current;
}

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

  for (final outcome in [
    'success',
    'retirement',
    'replacement',
    'dispose',
    'wrong_id',
    'captured_callback',
    'growth'
  ]) {
    testWidgets('kept reference exact owner and widget lifecycle $outcome', (tester) async {
      final owner = _Owner();
      final pending = Completer<ServerConversation?>();
      final created = DateTime(2026, 10, 10);
      var references = [
        MessageConversation('source-id', created, MessageConversationStructured('Synthetic source', ''))
      ];
      ServerMessage makeMessage() => ServerMessage('fixture-message', created, 'Synthetic reply', MessageSender.ai,
          MessageType.text, null, false, [], [], references,
          askForNps: false);
      var message = makeMessage();
      var calls = 0, presentations = 0;
      Future<void> pump() async {
        await tester.pumpWidget(MaterialApp(
            theme: ellaThemeData(),
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            home: Scaffold(
                body: SingleChildScrollView(
                    child: MemoriesMessageWidget(
              key: const Key('reused-reference-widget'),
              showTypingIndicator: false,
              messageMemories: references,
              messageText: message.text,
              updateConversation: (_) {},
              message: message,
              setMessageNps: (_, {reason}) {},
              date: created,
              sourceAuthorityProvider: () => owner,
              sourceLoader: (id, {required expectedAuthenticatedUid, required exactAuthority}) {
                expect(id, 'source-id');
                expect(expectedAuthenticatedUid, owner.uid);
                expect(exactAuthority, same(owner));
                calls++;
                return pending.future;
              },
              sourcePresenter: (_, memory) async {
                expectSync(memory.id, 'source-id');
                presentations++;
              },
            )))));
        await tester.pump();
      }

      await pump();
      final row = find.ancestor(of: find.text('Synthetic source'), matching: find.byType(GestureDetector)).first;
      final oldTap = tester.widget<GestureDetector>(row).onTap!;
      if (outcome == 'captured_callback') {
        message = makeMessage();
        await pump();
        oldTap();
        await tester.pump();
        expect(calls, 0);
      } else if (outcome == 'growth') {
        references = [
          references.first,
          MessageConversation('source-2', created, MessageConversationStructured('Second source', '')),
          MessageConversation('source-3', created, MessageConversationStructured('Third source', ''))
        ];
        await pump();
        expect(find.text('Third source'), findsOneWidget);
      } else {
        oldTap();
        await tester.pump();
        expect(calls, 1);
        if (outcome == 'retirement') owner.current = false;
        if (outcome == 'replacement') {
          message = makeMessage();
          await pump();
        }
        if (outcome == 'dispose') await tester.pumpWidget(const SizedBox());
        pending.complete(ServerConversation(
            id: outcome == 'wrong_id' ? 'different-source' : 'source-id',
            createdAt: created,
            structured: Structured('Synthetic source', '')));
        await tester.pump();
        await tester.pump();
        expect(presentations, outcome == 'success' ? 1 : 0);
      }
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    });
  }

  for (final dark in [false, true]) {
    for (final scale in [1.0, 3.5]) {
      testWidgets('kept reply and memory time wrap without invented due dark=$dark scale=$scale', (tester) async {
        tester.view.physicalSize = const Size(320, 844);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        final boundary = GlobalKey();
        final created = DateTime(2026, 10, 10, 9, 41);
        const title = 'Synthetic retained title that wraps completely';
        final reference = MessageConversation('fixture-memory', created, MessageConversationStructured(title, ''));
        final message = ServerMessage('fixture-reply', created, 'Synthetic saved memory reply.', MessageSender.ai,
            MessageType.text, null, false, [], [], [reference],
            askForNps: false);
        var opens = 0;
        await tester.pumpWidget(MaterialApp(
          theme: dark ? ellaDarkThemeData() : ellaThemeData(),
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: Builder(
              builder: (context) => MediaQuery(
                    data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(scale)),
                    child: Scaffold(
                      body: RepaintBoundary(
                          key: boundary,
                          child: ColoredBox(
                              color: Theme.of(context).scaffoldBackgroundColor,
                              child: SingleChildScrollView(
                                  padding: const EdgeInsets.all(16),
                                  child: Column(children: [
                                    MemoriesMessageWidget(
                                        showTypingIndicator: false,
                                        messageMemories: [reference],
                                        messageText: message.text,
                                        updateConversation: (_) {},
                                        message: message,
                                        setMessageNps: (_, {reason}) {},
                                        date: created),
                                    QuietMemoryRow(
                                        conversation: ServerConversation(
                                            id: reference.id, createdAt: created, structured: Structured(title, '')),
                                        title: title,
                                        onOpen: () => opens++),
                                  ])))),
                    ),
                  )),
        ));
        await tester.pump();
        final bubble = find.byKey(const Key('iris-assistant-kept-bubble'));
        final keptText = find.descendant(of: bubble, matching: find.text(title));
        expect(keptText, findsOneWidget);
        final text = tester.widget<Text>(keptText);
        expect(text.maxLines, isNull);
        expect(text.overflow, isNull);
        expect(find.text(DateFormat.jm().format(created)), findsOneWidget);
        expect(tester.takeException(), isNull);
        // No account authority: even clicking the exact kept source cannot issue a request.
        await tester.tap(keptText);
        await tester.pump();
        expect(tester.takeException(), isNull);
        await tester.ensureVisible(find.byType(QuietMemoryRow));
        await tester.pump();
        await tester.tap(find.byType(QuietMemoryRow));
        expect(opens, 1);
        await tester.runAsync(() async {
          final render = boundary.currentContext!.findRenderObject()! as RenderRepaintBoundary;
          final image = await render.toImage();
          final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
          await File('/Users/ellaai/Documents/Codex/2026-10-07/task-4/iris-follow-on-kept-memory-$dark-$scale.png')
              .writeAsBytes(bytes!.buffer.asUint8List());
          image.dispose();
        });
        await tester.pumpWidget(const SizedBox());
      });
    }
  }
  for (final weekday in [false, true]) {
    for (final dark in [false, true]) {
      for (final scale in [1.0, 3.5]) {
        testWidgets('Journal said-time weekday=$weekday dark=$dark scale=$scale', (tester) async {
          tester.view.physicalSize = const Size(320, 844);
          tester.view.devicePixelRatio = 1;
          addTearDown(tester.view.resetPhysicalSize);
          addTearDown(tester.view.resetDevicePixelRatio);
          final created = DateTime(2026, 10, 10, 9, 41);
          final boundary = GlobalKey();
          final conversation = ServerConversation.fromJson({
            'id': 'synthetic-journal',
            'created_at': created.toIso8601String(),
            'structured': {
              'title': 'Synthetic retained entry wraps completely',
              'overview': '',
              'emoji': '',
              'category': 'other',
              'action_items': [],
              'events': []
            },
            'transcript_segments': [],
            'source': 'omi',
            'status': 'completed'
          });
          await tester.pumpWidget(MaterialApp(
              theme: dark ? ellaDarkThemeData() : ellaThemeData(),
              localizationsDelegates: AppLocalizations.localizationsDelegates,
              supportedLocales: AppLocalizations.supportedLocales,
              home: Builder(
                  builder: (context) => MediaQuery(
                      data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(scale)),
                      child: Scaffold(
                          body: RepaintBoundary(
                              key: boundary,
                              child: ColoredBox(
                                  color: Theme.of(context).scaffoldBackgroundColor,
                                  child: Padding(
                                      padding: const EdgeInsets.all(16),
                                      child: QuietMemoryRow(
                                          conversation: conversation,
                                          title: conversation.structured.title,
                                          showWeekday: weekday,
                                          onOpen: () {})))))))));
          await tester.pump();
          expect(find.text((weekday ? DateFormat.E().add_jm() : DateFormat.jm()).format(created)), findsOneWidget);
          expect(tester.takeException(), isNull);
          await tester.runAsync(() async {
            final render = boundary.currentContext!.findRenderObject()! as RenderRepaintBoundary;
            final image = await render.toImage();
            final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
            await File(
                    '/Users/ellaai/Documents/Codex/2026-10-07/task-4/iris-follow-on-journal-$weekday-$dark-$scale.png')
                .writeAsBytes(bytes!.buffer.asUint8List());
            image.dispose();
          });
          await tester.pumpWidget(const SizedBox());
        });
      }
    }
  }
}
