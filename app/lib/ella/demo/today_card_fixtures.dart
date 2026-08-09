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
}
