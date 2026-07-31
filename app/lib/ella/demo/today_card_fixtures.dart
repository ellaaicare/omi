import 'package:omi/ella/models/today_card.dart';

class TodayCardFixtures {
  TodayCardFixtures._();

  static TodayCardResponse recap({DateTime? now}) => TodayCardResponse(
        contractVersion: todayCardContractVersion,
        status: TodayCardStatus.ready,
        card: TodayCard(
          id: 'demo-today-recap',
          version: 1,
          kind: TodayCardKind.recap,
          eyebrow: 'A NOTE FROM YESTERDAY',
          headline: 'A good moment from yesterday',
          body: 'You and Rose talked about the garden and the roses along Elm Street.',
          spokenText: 'You and Rose talked about the garden and the roses along Elm Street.',
          sourceDate: '2025-07-23',
          generatedAt: now ?? DateTime(2025, 7, 24, 9, 41),
          sourceRefs: const [
            TodayCardSourceRef(kind: 'conversation_summary', id: 'demo-garden-chat', versionId: 'demo-v1'),
          ],
        ),
      );

  static TodayCardResponse memory({DateTime? now}) => TodayCardResponse(
        contractVersion: todayCardContractVersion,
        status: TodayCardStatus.ready,
        card: TodayCard(
          id: 'demo-today-memory',
          version: 2,
          kind: TodayCardKind.memory,
          eyebrow: 'A MEMORY FROM JUNE 12',
          headline: 'The roses along Elm Street',
          body: 'You enjoyed taking the long way home with Rose when the flowers were in bloom.',
          sourceDate: '2025-06-12',
          generatedAt: now ?? DateTime(2025, 7, 24, 9, 41),
          sourceRefs: const [TodayCardSourceRef(kind: 'memory', id: 'demo-memory-rose', versionId: 'demo-v3')],
        ),
      );

  static TodayCardResponse interest({DateTime? now}) => TodayCardResponse(
        contractVersion: todayCardContractVersion,
        status: TodayCardStatus.ready,
        card: TodayCard(
          id: 'demo-today-interest',
          version: 1,
          kind: TodayCardKind.interest,
          eyebrow: 'SOMETHING YOU ENJOY',
          headline: 'Your garden',
          body: 'The roses are one of your favorite parts of summer.',
          generatedAt: now ?? DateTime(2025, 7, 24, 9, 41),
          sourceRefs: const [TodayCardSourceRef(kind: 'confirmed_interest', id: 'demo-interest-garden')],
        ),
      );

  static TodayCardResponse newUser({DateTime? now}) => TodayCardResponse(
        contractVersion: todayCardContractVersion,
        status: TodayCardStatus.newUser,
        card: TodayCard(
          id: 'demo-today-welcome',
          version: 1,
          kind: TodayCardKind.welcome,
          eyebrow: 'FOR YOU TODAY',
          headline: 'What matters to you?',
          body: 'Tell Ella about a person, place, or interest you would like to talk about.',
          generatedAt: now ?? DateTime(2025, 7, 24, 9, 41),
        ),
      );

  static const preparing = TodayCardResponse(
    contractVersion: todayCardContractVersion,
    status: TodayCardStatus.preparing,
  );

  static const degraded = TodayCardResponse(
    contractVersion: todayCardContractVersion,
    status: TodayCardStatus.degraded,
    errorCode: 'temporarily_unavailable',
  );
}
