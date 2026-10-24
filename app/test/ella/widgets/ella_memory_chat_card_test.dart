import 'dart:async';
import 'dart:io';
import 'dart:ui' as ui;
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter/material.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/widgets/ella_memory_chat_card.dart';
import 'package:omi/l10n/app_localizations.dart';

class _Owner implements ExactAccountAuthorityVerifier {
  @override
  final String uid = 'owner-a';
  bool current = true;
  @override
  bool isExactCurrent() => current;
}

void main() {
  setUpAll(() async {
    await (FontLoader('Manrope')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf')))
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
  for (final outcome in ['success', 'failure_retry', 'retirement', 'replacement', 'dispose', 'wrong_id']) {
    testWidgets('source action is exact and fenced: $outcome', (tester) async {
      final owner = _Owner();
      final pending = Completer<ServerConversation?>();
      var calls = 0;
      var presentations = 0;
      var message = ServerMessage('memory-card', DateTime(2026, 10, 9), 'Recorded text', MessageSender.ai,
          MessageType.text, null, false, [], [], [],
          isCanonicalMemoryArtifact: true, canonicalConversationId: 'source-id');
      Future<void> pump() => tester.pumpWidget(MaterialApp(
            theme: ellaThemeData(),
            locale: const Locale('en'),
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            home: Scaffold(
                body: EllaMemoryChatCard(
              key: const Key('same-card'),
              message: message,
              authorityProvider: () => owner,
              sourceLoader: (id, {required expectedAuthenticatedUid, required exactAuthority}) {
                expect(id, 'source-id');
                expect(expectedAuthenticatedUid, owner.uid);
                expect(exactAuthority, same(owner));
                calls++;
                return calls == 1 ? pending.future : Future.value(null);
              },
              sourcePresenter: (_, memory) async {
                expect(memory.id, 'source-id');
                presentations++;
              },
            )),
          ));
      await pump();
      await tester.pump();
      await tester.tap(find.byKey(const Key('memory-chat-open-source')));
      await tester.pump();
      expect(calls, 1);
      expect(tester.widget<TextButton>(find.byKey(const Key('memory-chat-open-source'))).onPressed, isNull);
      if (outcome == 'retirement') owner.current = false;
      if (outcome == 'replacement') {
        message = ServerMessage('replacement', DateTime(2026, 10, 9), 'New text', MessageSender.ai, MessageType.text,
            null, false, [], [], [],
            isCanonicalMemoryArtifact: true);
        await pump();
        await tester.pump();
      }
      if (outcome == 'dispose') {
        await tester.pumpWidget(const SizedBox.shrink());
      }
      pending.complete(outcome == 'failure_retry'
          ? null
          : ServerConversation(
              id: outcome == 'wrong_id' ? 'different-id' : 'source-id',
              createdAt: DateTime(2026, 10, 9),
              structured: Structured('Title', 'Summary')));
      await tester.pump();
      await tester.pump();
      expect(presentations, outcome == 'success' ? 1 : 0);
      if (outcome == 'failure_retry' || outcome == 'wrong_id') {
        expect(find.text('The source could not be opened. Try again.'), findsOneWidget);
      }
      if (outcome == 'failure_retry') {
        await tester.tap(find.byKey(const Key('memory-chat-open-source')));
        await tester.pump();
        await tester.pump();
        expect(calls, 2);
        expect(find.text('The source could not be opened. Try again.'), findsOneWidget);
      }
      if (outcome == 'replacement') {
        expect(find.text('New text'), findsNothing);
        expect(find.text('Memory'), findsOneWidget);
        expect(find.text('The source could not be opened. Try again.'), findsNothing);
      }
      expect(tester.takeException(), isNull);
    });
  }

  for (final dark in [false, true]) {
    for (final scale in [1.0, 3.0]) {
      testWidgets('memory artifact labels survive narrow layout dark=$dark scale=$scale', (tester) async {
        tester.view.physicalSize = const Size(320, 780);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        final message = ServerMessage('memory-card', DateTime(2026, 10, 9),
            '[Ella] Maya brings the blue folder at 4PM.', MessageSender.ai, MessageType.text, null, false, [], [], [],
            isCanonicalMemoryArtifact: true, canonicalConversationId: 'source-id');
        await tester.pumpWidget(MaterialApp(
          theme: dark ? ellaDarkThemeData() : ellaThemeData(),
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          locale: const Locale('en'),
          builder: (context, child) => RepaintBoundary(
              key: const Key('memory-card-render'),
              child: MediaQuery(
                  data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(scale)), child: child!)),
          home: Scaffold(
              body: ListView(padding: const EdgeInsets.all(16), children: [EllaMemoryChatCard(message: message)])),
        ));
        await tester.pump();
        expect(find.text('Memory'), findsOneWidget);
        expect(find.text('Ella’s summary'), findsNothing);
        expect(find.text('Maya brings the blue folder at 4PM.'), findsNothing);
        expect(tester.getSize(find.byKey(const Key('memory-chat-open-source'))).height, greaterThanOrEqualTo(44));
        expect(find.byKey(const Key('memory-chat-open-source')), findsOneWidget);
        expect(tester.takeException(), isNull);
        final boundary = tester.renderObject<RenderRepaintBoundary>(find.byKey(const Key('memory-card-render')));
        await tester.runAsync(() async {
          final image = await boundary.toImage(pixelRatio: 1);
          final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
          await File(
                  '/Users/ellaai/Documents/Codex/2026-10-10/task-6/iris-memory-source-${dark ? 'dark' : 'light'}-${scale.toInt()}x.png')
              .writeAsBytes(bytes!.buffer.asUint8List());
          image.dispose();
        });
      });
    }
  }
}
