import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/widgets/conversation_bottom_bar.dart';
import 'summary_read_action_contrast_test.dart' show contrast;

void main() {
  for (final dark in [false, true]) {
    for (final scale in [1.0, 3.5]) {
      testWidgets('section popup painted readable and routes only selected tab $dark $scale', (tester) async {
        tester.view.physicalSize = const Size(390, 844);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        final selected = <ConversationTab>[];
        await tester.pumpWidget(MaterialApp(
            theme: dark ? ellaDarkThemeData() : ellaThemeData(),
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            home: MediaQuery(
                data: MediaQueryData(size: const Size(390, 844), textScaler: TextScaler.linear(scale)),
                child: Scaffold(
                    body: ConversationBottomBar(
                        mode: ConversationBottomBarMode.memorySource,
                        selectedTab: ConversationTab.summary,
                        onTabSelected: selected.add,
                        onStopPressed: () => fail('no capture action'),
                        hasSegments: true,
                        hasActionItems: false)))));
        await tester.tap(find.byKey(const Key('iris-memory-section-menu')));
        await tester.pumpAndSettle();
        for (final item in find.byType(PopupMenuItem<ConversationTab>).evaluate()) {
          final f = find.byWidget(item.widget);
          final rich = find.descendant(of: f, matching: find.byType(RichText)).first;
          final paragraph = tester.renderObject<RenderParagraph>(rich);
          final material = tester.widget<Material>(find.ancestor(of: f, matching: find.byType(Material)).first);
          final ink = paragraph.text.style!.color!;
          final fill = material.color!;
          expect(contrast(ink, fill), greaterThanOrEqualTo(4.5));
          expect(paragraph.didExceedMaxLines, false);
          expect(tester.getSize(f).height, greaterThanOrEqualTo(48));
        }
        await tester.tap(find.widgetWithText(PopupMenuItem<ConversationTab>, 'Source'));
        await tester.pumpAndSettle();
        expect(selected, [ConversationTab.transcript]);
        expect(tester.takeException(), isNull);
      });
    }
  }
}
