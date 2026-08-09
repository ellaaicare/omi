import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/today_card.dart';
import 'package:omi/ella/widgets/today_card_surface.dart';
import 'package:omi/l10n/app_localizations.dart';

void main() {
  setUpAll(() async {
    await (FontLoader('Manrope')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf')))
        .load();
    await (FontLoader('Fraunces')
          ..addFont(rootBundle.load('assets/fonts/Fraunces-Latin-Regular.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Fraunces-Latin-Medium.ttf')))
        .load();
  });

  void configureView(WidgetTester tester, {double width = 390, double height = 844}) {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = Size(width, height);
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
  }

  Widget buildApp(TodayCardViewState state, {double textScale = 1}) => MaterialApp(
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(
            context,
          ).copyWith(textScaler: TextScaler.linear(textScale)),
          child: child!,
        ),
        home: Scaffold(
          body: SingleChildScrollView(
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: EllaSizes.screenPadding),
              child: TodayCardSurface(
                state: state,
                isReading: false,
                onTalk: () {},
                onReadAloud: () {},
              ),
            ),
          ),
        ),
      );

  TodayCard readyCard({TodayCardKind kind = TodayCardKind.memory}) => TodayCard(
        id: 'daily-card-1',
        version: 1,
        kind: kind,
        eyebrow: 'SERVER DISPLAY COPY',
        headline: 'A calm moment worth repeating',
        body:
            'A little time outside stood out as a good moment yesterday. If it feels right, make space for that again today.',
        generatedAt: DateTime.utc(2026, 8, 9, 12),
        sourceDate: '2026-08-08',
        localDate: '2026-08-09',
        sourceRefs:
            kind == TodayCardKind.welcome ? const [] : const [TodayCardSourceRef(kind: 'memory', id: 'memory-1')],
      );

  testWidgets(
    'ready note uses stable hierarchy, derived provenance, and compact actions',
    (tester) async {
      configureView(tester);
      await tester.pumpWidget(
        buildApp(
          TodayCardViewState(status: TodayCardStatus.ready, card: readyCard()),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text("ELLA'S DAILY NOTE"), findsOneWidget);
      expect(find.text('From a recent memory • Updated today'), findsOneWidget);
      expect(find.text('SERVER DISPLAY COPY'), findsNothing);
      expect(find.byKey(const Key('today-card-actions-row')), findsOneWidget);
      expect(find.byKey(const Key('today-card-talk')), findsOneWidget);
      expect(find.byKey(const Key('today-card-read-aloud')), findsOneWidget);
      expect(
        tester.getSize(find.byKey(const Key('today-card-talk'))).height,
        greaterThanOrEqualTo(48),
      );
      final surfaceSize = tester.getSize(find.byKey(const Key('today-card-semantics')));
      final headlineSize = tester.getSize(find.byKey(const Key('today-card-headline')));
      final bodySize = tester.getSize(find.byKey(const Key('today-card-body')));
      final actionSize = tester.getSize(find.byKey(const Key('today-card-actions-row')));
      final effectiveScale =
          MediaQuery.textScalerOf(tester.element(find.byKey(const Key('today-card-body')))).scale(16) / 16;
      expect(
        surfaceSize.height,
        lessThanOrEqualTo(300),
        reason: 'surface=$surfaceSize headline=$headlineSize body=$bodySize actions=$actionSize scale=$effectiveScale',
      );
      expect(tester.takeException(), isNull);
    },
  );

  for (final width in [320.0, 430.0]) {
    testWidgets('ready note is responsive at ${width.toInt()} points', (tester) async {
      configureView(tester, width: width);
      await tester.pumpWidget(
        buildApp(
          TodayCardViewState(status: TodayCardStatus.ready, card: readyCard()),
        ),
      );
      await tester.pumpAndSettle();

      expect(
        find.byKey(Key(width == 320 ? 'today-card-actions-stacked' : 'today-card-actions-row')),
        findsOneWidget,
      );
      expect(find.text('From a recent memory • Updated today'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('large text stacks actions without clipping or overflow', (
    tester,
  ) async {
    configureView(tester);
    await tester.pumpWidget(
      buildApp(
        TodayCardViewState(status: TodayCardStatus.ready, card: readyCard()),
        textScale: 2,
      ),
    );
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('today-card-actions-stacked')), findsOneWidget);
    expect(find.text('A calm moment worth repeating'), findsOneWidget);
    expect(find.textContaining('A little time outside'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'cached refresh keeps the safe card and marks it as saved',
    (tester) async {
      configureView(tester);
      await tester.pumpWidget(
        buildApp(
          TodayCardViewState(
            status: TodayCardStatus.preparing,
            card: readyCard(),
            isLoading: true,
            isCached: true,
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('Showing the last note while Ella checks for an update.'), findsOneWidget);
      expect(find.byKey(const Key('today-card-talk')), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('preparing state exposes no actions or invented personal content', (tester) async {
    configureView(tester);
    await tester.pumpWidget(
      buildApp(const TodayCardViewState(status: TodayCardStatus.preparing, isLoading: true)),
    );
    await tester.pump();

    expect(find.text("Preparing today's note"), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsOneWidget);
    expect(find.byKey(const Key('today-card-talk')), findsNothing);
    expect(find.byKey(const Key('today-card-read-aloud')), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'no-safe-source degraded state uses the evidence-absence explanation',
    (tester) async {
      configureView(tester);
      await tester.pumpWidget(
        buildApp(
          const TodayCardViewState(
            status: TodayCardStatus.degraded,
            errorCode: 'no_safe_source',
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('No new note yet'), findsOneWidget);
      expect(find.textContaining('enough recent memory'), findsOneWidget);
      expect(find.byKey(const Key('today-card-provenance')), findsNothing);
      expect(find.byKey(const Key('today-card-talk')), findsNothing);
      expect(find.byKey(const Key('today-card-read-aloud')), findsNothing);
      expect(tester.takeException(), isNull);
    },
  );

  for (final errorCode in ['generation_failed', 'generation_output_invalid', 'today_card_unavailable']) {
    testWidgets('operational degraded state stays truthful for $errorCode', (tester) async {
      configureView(tester);
      await tester.pumpWidget(
        buildApp(
          TodayCardViewState(
            status: TodayCardStatus.degraded,
            errorCode: errorCode,
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text("Today's note isn't available right now"), findsOneWidget);
      expect(find.textContaining('Pull down to try again'), findsOneWidget);
      expect(find.textContaining('enough recent memory'), findsNothing);
      expect(find.byKey(const Key('today-card-talk')), findsNothing);
      expect(find.byKey(const Key('today-card-read-aloud')), findsNothing);
      expect(tester.takeException(), isNull);
    });
  }
}
