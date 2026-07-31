import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/today_card.dart';
import 'package:omi/ella/widgets/today_card_surface.dart';
import 'package:omi/l10n/app_localizations.dart';

void main() {
  Future<void> pumpSurface(
    WidgetTester tester, {
    required TodayCardViewState state,
    double textScale = 1,
    VoidCallback? onTalk,
  }) async {
    await tester.pumpWidget(
      MaterialApp(
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MediaQuery(
          data: MediaQueryData(textScaler: TextScaler.linear(textScale)),
          child: Scaffold(
            body: SingleChildScrollView(
              padding: const EdgeInsets.all(20),
              child: TodayCardSurface(
                state: state,
                isReading: false,
                onTalk: onTalk,
                onReadAloud: state.card == null ? null : () {},
              ),
            ),
          ),
        ),
      ),
    );
    await tester.pump();
  }

  testWidgets('ready card keeps the truthful source label and exposes full-width scoped Talk', (tester) async {
    var talks = 0;
    await pumpSurface(
      tester,
      state: TodayCardViewState(status: TodayCardStatus.ready, card: _card(TodayCardKind.memory)),
      onTalk: () => talks++,
    );

    expect(find.text('A MEMORY FROM JUNE 12'), findsOneWidget);
    expect(find.text('The roses along Elm Street'), findsOneWidget);
    expect(find.text('Talk about this'), findsOneWidget);
    expect(tester.getSize(find.byKey(const Key('today-card-talk'))).height, greaterThanOrEqualTo(48));
    expect(
      tester.getSize(find.byKey(const Key('today-card-talk'))).width,
      tester.getSize(find.byKey(const Key('today-card-semantics'))).width - (EllaSizes.notePadding * 2),
    );

    await tester.tap(find.byKey(const Key('today-card-talk')));
    expect(talks, 1);
  });

  testWidgets('preparing, new-user, and degraded states remain distinct and never blank', (tester) async {
    await pumpSurface(tester, state: const TodayCardViewState.preparing());
    expect(find.text('Ella is putting something together for you.'), findsOneWidget);

    await pumpSurface(tester, state: const TodayCardViewState(status: TodayCardStatus.newUser));
    expect(find.text('What matters to you?'), findsOneWidget);
    expect(find.textContaining('person, place, or interest'), findsOneWidget);

    await pumpSurface(
      tester,
      state: const TodayCardViewState(status: TodayCardStatus.degraded, errorCode: 'provider_unavailable'),
    );
    expect(find.text('Ella could not refresh this just now.'), findsOneWidget);
    expect(find.text('Pull down to try again.'), findsOneWidget);
  });

  testWidgets('degraded cache preserves the card label and identifies saved content', (tester) async {
    await pumpSurface(
      tester,
      state: TodayCardViewState(
        status: TodayCardStatus.degraded,
        card: _card(TodayCardKind.interest),
        isCached: true,
        errorCode: 'temporarily_unavailable',
      ),
    );

    expect(find.text('SOMETHING YOU ENJOY'), findsOneWidget);
    expect(find.byKey(const Key('today-card-cached-status')), findsOneWidget);
    expect(find.text('Showing the last item Ella saved for you.'), findsOneWidget);
  });

  testWidgets('large Dynamic Type remains scrollable without overflow', (tester) async {
    tester.view.physicalSize = const Size(390, 520);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await pumpSurface(
      tester,
      state: TodayCardViewState(status: TodayCardStatus.ready, card: _card(TodayCardKind.recap)),
      textScale: 2,
      onTalk: () {},
    );

    expect(tester.takeException(), isNull);
    expect(find.byType(SingleChildScrollView), findsOneWidget);
  });

  testWidgets('VoiceOver exposes a named Talk button and headline semantics', (tester) async {
    final semantics = tester.ensureSemantics();
    await pumpSurface(
      tester,
      state: TodayCardViewState(status: TodayCardStatus.ready, card: _card(TodayCardKind.recap)),
      onTalk: () {},
    );

    expect(find.bySemanticsLabel('Talk about this'), findsWidgets);
    final headline = tester.getSemantics(find.byKey(const Key('today-card-headline')));
    expect(headline.flagsCollection.isHeader, isTrue);
    semantics.dispose();
  });
}

TodayCard _card(TodayCardKind kind) {
  final (eyebrow, headline) = switch (kind) {
    TodayCardKind.recap => ('A NOTE FROM YESTERDAY', 'A good conversation with Rose'),
    TodayCardKind.memory => ('A MEMORY FROM JUNE 12', 'The roses along Elm Street'),
    TodayCardKind.interest => ('SOMETHING YOU ENJOY', 'Your garden'),
    TodayCardKind.welcome => ('FOR YOU TODAY', 'What matters to you?'),
  };
  return TodayCard(
    id: '${kind.name}-card',
    version: 2,
    kind: kind,
    eyebrow: eyebrow,
    headline: headline,
    body: 'A source-backed thought that is safe to discuss today.',
    generatedAt: DateTime.utc(2026, 7, 31),
    sourceRefs: kind == TodayCardKind.welcome
        ? const []
        : const [TodayCardSourceRef(kind: 'conversation_summary', id: 'conversation-1', versionId: 'v2')],
  );
}
