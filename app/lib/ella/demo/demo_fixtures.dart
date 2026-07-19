import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/daily_summary.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/backend/schema/structured.dart';

class DemoFixtures {
  static const gardenConversationId = 'demo-garden-chat-with-margaret';
  static const dailySummaryId = 'demo-daily-recap';

  static const dailyRecap =
      "Mom had a good day. She did her crossword with coffee, found the scissors (kitchen drawer, as usual), and spent the afternoon in the garden with Margaret — the tomatoes are coming in early. She's looking forward to dinner with David on Tuesday and asked about it twice, because she's excited.";

  static List<ServerConversation> conversations({DateTime? now}) {
    final base = now ?? DateTime.now();
    final day = DateTime(base.year, base.month, base.day);

    return [
      _conversation(
        id: 'demo-planning-tuesday-dinner-with-david',
        title: '🪽 [Ella] Planning Tuesday Dinner with David',
        startedAt: day.add(const Duration(hours: 17, minutes: 40)),
        duration: const Duration(minutes: 3, seconds: 12),
        overview: 'Mom and David talked through a simple Tuesday dinner plan.',
      ),
      _conversation(
        id: gardenConversationId,
        title: '🪽 [Ella] Garden Chat with Margaret',
        startedAt: day.add(const Duration(hours: 14, minutes: 15)),
        duration: const Duration(minutes: 12, seconds: 4),
        overview:
            "A long, easy visit with Margaret out by the garden. They talked about the tomatoes coming in early this year and Margaret's granddaughter starting college in the fall. Mom told the story about the county-fair ribbon again, and it got a good laugh. She mentioned wanting to pick up more potting soil next time anyone drives to the hardware store.",
      ),
      _conversation(
        id: 'demo-where-did-the-scissors-go',
        title: '🪽 [Ella] Where Did the Scissors Go',
        startedAt: day.add(const Duration(hours: 11, minutes: 32)),
        duration: const Duration(minutes: 1, seconds: 8),
        overview: 'Mom found the scissors in the kitchen drawer.',
      ),
      _conversation(
        id: 'demo-morning-crossword-and-coffee',
        title: '🪽 [Ella] Morning Crossword & Coffee',
        startedAt: day.add(const Duration(hours: 8, minutes: 10)),
        duration: const Duration(minutes: 4, seconds: 21),
        overview: 'Mom started the morning with coffee and her crossword.',
      ),
    ];
  }

  static ServerConversation? conversationById(String id) {
    return conversations().where((conversation) => conversation.id == id).firstOrNull;
  }

  static List<ServerMessage> chatMessages({DateTime? now}) {
    final base = now ?? DateTime.now();
    return [
      _message(
        id: 'demo-chat-1',
        createdAt: base.subtract(const Duration(minutes: 4)),
        text: "What's the word for the appointment where they take your fingerprints?",
        sender: MessageSender.human,
      ),
      _message(
        id: 'demo-chat-2',
        createdAt: base.subtract(const Duration(minutes: 3)),
        text:
            "That's the biometrics appointment — it's on Thursday at 10 in the morning. Want me to remind you Thursday after breakfast?",
        sender: MessageSender.ai,
      ),
      _message(
        id: 'demo-chat-3',
        createdAt: base.subtract(const Duration(minutes: 2)),
        text: 'Yes please. And where did I put my glasses?',
        sender: MessageSender.human,
      ),
      _message(
        id: 'demo-chat-4',
        createdAt: base.subtract(const Duration(minutes: 1)),
        text: 'Last night you set them in the front pocket of your blue backpack, by the door. 🪽',
        sender: MessageSender.ai,
      ),
    ];
  }

  static DailySummary dailySummary({DateTime? now}) {
    final base = now ?? DateTime.now();
    final day = DateTime(base.year, base.month, base.day);
    return DailySummary(
      id: dailySummaryId,
      date: _dateString(day),
      createdAt: day.add(const Duration(hours: 19)),
      headline: '🪽 Today with Mom',
      overview: '[Ella] $dailyRecap',
      dayEmoji: '🪽',
      stats: DayStats(totalConversations: 4, totalDurationMinutes: 21),
      highlights: [
        TopicHighlight(
          topic: 'Garden with Margaret',
          emoji: '🪽',
          summary: 'Mom spent the afternoon in the garden with Margaret and talked about the tomatoes.',
          conversationIds: [gardenConversationId],
        ),
      ],
    );
  }

  static List<DailySummary> dailySummaries({DateTime? now}) => [dailySummary(now: now)];

  static ServerConversation _conversation({
    required String id,
    required String title,
    required DateTime startedAt,
    required Duration duration,
    required String overview,
  }) {
    return ServerConversation(
      id: id,
      createdAt: startedAt,
      startedAt: startedAt,
      finishedAt: startedAt.add(duration),
      structured: Structured(title, overview, emoji: '🪽', category: ''),
      transcriptSegments: const [],
      status: ConversationStatus.completed,
    );
  }

  static ServerMessage _message({
    required String id,
    required DateTime createdAt,
    required String text,
    required MessageSender sender,
  }) {
    return ServerMessage(
      id,
      createdAt,
      text,
      sender,
      MessageType.text,
      null,
      false,
      [],
      [],
      [],
      askForNps: false,
    );
  }

  static String _dateString(DateTime date) {
    final month = date.month.toString().padLeft(2, '0');
    final day = date.day.toString().padLeft(2, '0');
    return '${date.year}-$month-$day';
  }
}

extension _FirstOrNull<T> on Iterable<T> {
  T? get firstOrNull => isEmpty ? null : first;
}
