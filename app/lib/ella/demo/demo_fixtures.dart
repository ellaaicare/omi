import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/daily_summary.dart';
import 'package:omi/backend/schema/action_item.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/models/guardian_alert.dart';

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
        id: 'demo-found-the-scissors',
        title: 'Found the scissors',
        startedAt: day.add(const Duration(hours: 11, minutes: 32)),
        duration: const Duration(minutes: 3, seconds: 12),
        overview: 'They were in the kitchen drawer, next to the tape.',
        emoji: '✂️',
      ),
      _conversation(
        id: gardenConversationId,
        title: 'A walk past the roses',
        startedAt: day.add(const Duration(hours: 14, minutes: 15)),
        duration: const Duration(minutes: 12, seconds: 4),
        overview: 'The long way home along Elm — warm air, roses in bloom.',
        emoji: '🚶',
      ),
      _conversation(
        id: 'demo-phone-call-with-david',
        title: 'Phone call with David',
        startedAt: day.subtract(const Duration(days: 1)).add(const Duration(hours: 17, minutes: 40)),
        duration: const Duration(minutes: 1, seconds: 8),
        overview: "Dinner set for Tuesday at six; he'll pick you up at a quarter to.",
        emoji: '📞',
      ),
      _conversation(
        id: 'demo-tuesday-dinner-plans',
        title: 'Tuesday dinner plans',
        startedAt: day.subtract(const Duration(days: 1)).add(const Duration(hours: 15, minutes: 10)),
        duration: const Duration(minutes: 4, seconds: 21),
        overview: "Roast chicken at David's — you offered to bring bread.",
        emoji: '🍲',
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
        id: 'demo-chat-opening',
        createdAt: base.subtract(const Duration(minutes: 5)),
        text: "Good morning — I'm here.",
        sender: MessageSender.ai,
      ),
      _message(
        id: 'demo-chat-1',
        createdAt: base.subtract(const Duration(minutes: 4)),
        text: 'When is dinner with David?',
        sender: MessageSender.human,
      ),
      _message(
        id: 'demo-chat-2',
        createdAt: base.subtract(const Duration(minutes: 3)),
        text: "Tuesday at six. He said he'll pick you up at a quarter to.",
        sender: MessageSender.ai,
      ),
      _message(
        id: 'demo-chat-3',
        createdAt: base.subtract(const Duration(minutes: 2)),
        text: 'Thank you, Ella.',
        sender: MessageSender.human,
      ),
      _message(
        id: 'demo-chat-4',
        createdAt: base.subtract(const Duration(minutes: 1)),
        text: 'Anytime, Margaret. Would you like a reminder Tuesday afternoon?',
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

  static List<ActionItemWithMetadata> actionItems({DateTime? now}) {
    final base = now ?? DateTime.now();
    return [
      ActionItemWithMetadata(
        id: 'demo-reminder-david',
        description: 'Dinner with David',
        completed: false,
        dueAt: DateTime(base.year, base.month, base.day, 18),
        sourceLabel: 'David',
      ),
      ActionItemWithMetadata(
        id: 'demo-reminder-prescription',
        description: 'Pick up prescription',
        completed: false,
        dueAt: DateTime(base.year, base.month, base.day, 14),
      ),
    ];
  }

  static List<GuardianAlertRecord> whispers({DateTime? now}) {
    final base = now ?? DateTime.now();
    return [
      GuardianAlertRecord(
        id: 'demo-whisper-scissors',
        alertText: 'You were looking for the scissors — kitchen drawer.',
        triggerType: 'asked_where_scissors_were',
        deliveryTarget: 'user',
        playbackStatus: 'played',
        createdAt: base.subtract(const Duration(hours: 1)),
        sourceConversationId: 'demo-found-the-scissors',
      ),
      GuardianAlertRecord(
        id: 'demo-whisper-biometrics',
        alertText: 'Your biometrics appointment is Thursday at ten.',
        triggerType: 'asked_about_biometrics_appointment',
        deliveryTarget: 'user',
        playbackStatus: 'played',
        createdAt: base.subtract(const Duration(hours: 3)),
      ),
      GuardianAlertRecord(
        id: 'demo-whisper-dinner',
        alertText: "David is picking you up Tuesday at a quarter to six.",
        triggerType: 'mentioned_tuesday_dinner',
        deliveryTarget: 'user',
        playbackStatus: 'queued',
        createdAt: base.subtract(const Duration(days: 1, hours: 2)),
        sourceConversationId: 'demo-tuesday-dinner-plans',
      ),
    ];
  }

  static ServerConversation _conversation({
    required String id,
    required String title,
    required DateTime startedAt,
    required Duration duration,
    required String overview,
    String emoji = '🪽',
  }) {
    return ServerConversation(
      id: id,
      createdAt: startedAt,
      startedAt: startedAt,
      finishedAt: startedAt.add(duration),
      structured: Structured(title, overview, emoji: emoji, category: ''),
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
    return ServerMessage(id, createdAt, text, sender, MessageType.text, null, false, [], [], [], askForNps: false);
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
