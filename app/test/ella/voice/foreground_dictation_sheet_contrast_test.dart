import 'dart:math' as math;
import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/voice/foreground_dictation_host.dart';
import 'package:omi/ella/voice/ephemeral_dictation_session.dart';
import 'package:omi/ella/widgets/foreground_dictation_sheet.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'ephemeral_dictation_session_test.dart' show Rig;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  for (final dark in [false, true])
    for (final control in ['start', 'finish', 'cancel'])
      for (final scale in [1.0, 3.5]) {
        testWidgets('Iris sheet $control readable dark=$dark scale=$scale', (tester) async {
          tester.view.physicalSize = const Size(390, 844);
          tester.view.devicePixelRatio = 1;
          addTearDown(tester.view.resetPhysicalSize);
          addTearDown(tester.view.resetDevicePixelRatio);
          final r = Rig();
          final host = ForegroundDictationHost(
              registration: null,
              readEditor: () => DictationEditorBinding(value: r.value, lease: r.lease, epoch: 3, revision: 0),
              hasExactAuthority: () => false,
              canStartFresh: () => false,
              applyDraft: (_) {});
          host.phase = control == 'finish' ? ForegroundDictationPhase.listening : ForegroundDictationPhase.ready;
          await tester.pumpWidget(MaterialApp(
              theme: dark ? ellaDarkThemeData() : ellaThemeData(),
              localizationsDelegates: AppLocalizations.localizationsDelegates,
              supportedLocales: AppLocalizations.supportedLocales,
              builder: (ctx, child) =>
                  MediaQuery(data: MediaQuery.of(ctx).copyWith(textScaler: TextScaler.linear(scale)), child: child!),
              home: Scaffold(body: ForegroundDictationSheet(host: host))));
          await tester.pumpAndSettle();
          final button = find.byKey(Key('chat-dictation-$control'));
          expect(tester.widget<TextButton>(button).onPressed, isNotNull);
          final rich = tester.widget<RichText>(find.descendant(of: button, matching: find.byType(RichText)).first);
          final ink = (rich.text as TextSpan).style!.color!;
          final surface = tester
              .widget<Material>(find.descendant(of: find.byType(Dialog), matching: find.byType(Material)).first)
              .color!;
          final effective = Color.alphaBlend(ink, surface);
          final ratio = (math.max(effective.computeLuminance(), surface.computeLuminance()) + .05) /
              (math.min(effective.computeLuminance(), surface.computeLuminance()) + .05);
          print('ACTUAL $control dark=$dark scale=$scale ink=$ink surface=$surface ratio=$ratio');
          final size = tester.getSize(button);
          expect(size.height, greaterThanOrEqualTo(48));
          for (final key in ['start', 'finish']) {
            final candidate = find.byKey(Key('chat-dictation-$key'));
            if (tester.widget<TextButton>(candidate).onPressed != null) continue;
            final label =
                tester.widget<RichText>(find.descendant(of: candidate, matching: find.byType(RichText)).first);
            final disabledInk = Color.alphaBlend((label.text as TextSpan).style!.color!, surface);
            final disabledRatio = (math.max(disabledInk.computeLuminance(), surface.computeLuminance()) + .05) /
                (math.min(disabledInk.computeLuminance(), surface.computeLuminance()) + .05);
            expect(disabledRatio, greaterThanOrEqualTo(4.5));
          }
          await tester.pumpWidget(const SizedBox.shrink());
          host.dispose();
          r.lease.close();
          await tester.pump();
          expect(ratio, greaterThanOrEqualTo(4.5),
              reason: 'Enabled label must contrast against actual dialog Material');
        });
      }
  for (final dark in [false, true]) {
    testWidgets('actual large Iris Cancel remains reachable and closes dark=$dark', (tester) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final semantics = tester.ensureSemantics();
      final r = Rig();
      final host = ForegroundDictationHost(
        registration: null,
        readEditor: () => DictationEditorBinding(value: r.value, lease: r.lease, epoch: 3, revision: 0),
        hasExactAuthority: () => false,
        canStartFresh: () => false,
        applyDraft: (_) {},
      );
      await tester.pumpWidget(MaterialApp(
        theme: dark ? ellaDarkThemeData() : ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        builder: (ctx, child) => MediaQuery(
          data: MediaQuery.of(ctx).copyWith(textScaler: const TextScaler.linear(3.5)),
          child: child!,
        ),
        home: Builder(
            builder: (context) => Scaffold(
                    body: TextButton(
                  key: const Key('open-local-dialog'),
                  onPressed: () =>
                      showDialog<void>(context: context, builder: (_) => ForegroundDictationSheet(host: host)),
                  child: const Text('Open'),
                ))),
      ));
      await tester.tap(find.byKey(const Key('open-local-dialog')));
      await tester.pumpAndSettle();
      final cancel = find.byKey(const Key('chat-dictation-cancel'));
      expect(cancel.hitTestable(), findsOneWidget);
      expect(tester.getSemantics(cancel).getSemanticsData().hasAction(SemanticsAction.tap), isTrue);
      await tester.tap(cancel);
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('chat-dictation-sheet')), findsNothing);
      expect(host.hasHeldInput || host.hasHeldOutput, isFalse);
      await tester.pumpWidget(const SizedBox.shrink());
      host.dispose();
      r.lease.close();
      semantics.dispose();
    });
  }
}
