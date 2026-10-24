import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/summary_read_freshness.dart';
import 'package:omi/ella/widgets/summary_refresh_notice.dart';

void main() {
  setUpAll(() async {
    await (FontLoader('.SF UI Text')..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))).load();
  });
  for (final phase in SummaryReadPhase.values) {
    for (final dark in [false, true]) {
      for (final scale in [1.0, 3.5]) {
        testWidgets('summary refresh wrapping $phase dark=$dark scale=$scale', (tester) async {
          tester.view.physicalSize = const Size(390, 844);
          tester.view.devicePixelRatio = 1;
          addTearDown(tester.view.resetPhysicalSize);
          addTearDown(tester.view.resetDevicePixelRatio);
          final row = ServerConversation(
            id: 'fictional',
            createdAt: DateTime.utc(2026, 10, 16),
            structured: Structured('Fictional title', 'Fictional overview'),
            enrichmentState: {'status': 'held'},
          );
          final observation = SummaryReadObservation(
            phase,
            checkedAt: DateTime.utc(2026, 10, 16, 12),
            versionId: row.activeSummaryVersionId,
            bodyWitness: summaryReadBodyWitness(row),
          );
          var reads = 0;
          await tester.pumpWidget(
            MaterialApp(
              theme: dark ? ellaDarkThemeData() : ellaThemeData(),
              home: Scaffold(
                body: MediaQuery(
                  data: MediaQueryData(size: const Size(390, 844), textScaler: TextScaler.linear(scale)),
                  child: SingleChildScrollView(
                    child: Padding(
                      padding: const EdgeInsets.all(20),
                      child: SummaryRefreshNotice(
                        observation: observation,
                        conversation: row,
                        onRefresh: () => reads++,
                      ),
                    ),
                  ),
                ),
              ),
            ),
          );
          await tester.pump();
          final message = find.byKey(const Key('summary-refresh-status'));
          expect(tester.renderObject<RenderParagraph>(message).didExceedMaxLines, false);
          final hold = tester.widget<Text>(find.byKey(const Key('summary-processing-status')));
          expect(
            hold.data,
            phase == SummaryReadPhase.checked
                ? 'Summary processing is on hold.'
                : 'Previously loaded status: Summary processing is on hold.',
          );
          final button = find.text('Refresh summary');
          if (phase == SummaryReadPhase.checking) {
            expect(button, findsNothing);
          } else {
            await tester.ensureVisible(button);
            await tester.pump();
            await tester.tap(button);
            expect(reads, 1);
          }
          expect(tester.takeException(), null);
        });
      }
    }
  }
}
