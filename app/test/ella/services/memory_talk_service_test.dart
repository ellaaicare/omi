import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/person.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/backend/schema/transcript_segment.dart';
import 'package:omi/ella/services/memory_talk_service.dart';

void main() {
  test('extracts correction claims but applies only after affirmation', () {
    const extractor = MemoryTalkCorrectionExtractor();

    final claim = extractor.extract('It was Rose at the garden, not Margaret.');

    expect(claim, isNotNull);
    expect(claim!.newText, 'Rose at the garden');
    expect(claim.oldText, 'Margaret');
    expect(isAffirmativeCorrectionReply('yes'), isTrue);
    expect(isAffirmativeCorrectionReply('not quite'), isFalse);
    expect(isNegativeCorrectionReply('not quite'), isTrue);
  });

  test('builds scoped context from one memory without creating a new memory payload', () {
    final conversation = ServerConversation(
      id: 'memory-1',
      createdAt: DateTime.utc(2026, 7, 20, 9),
      structured: Structured('Coffee in the Garden', '[Ella] Margaret talked about roses.'),
      transcriptSegments: [
        TranscriptSegment(
          id: 'segment-1',
          text: 'The roses look good today.',
          speaker: 'SPEAKER_01',
          isUser: false,
          personId: 'person-rose',
          start: 0,
          end: 2,
          translations: const [],
        ),
      ],
    );
    final people = [
      Person(id: 'person-rose', name: 'Rose', createdAt: DateTime.utc(2026, 7, 1), updatedAt: DateTime.utc(2026, 7, 1)),
    ];

    final context = buildMemoryTalkContext(
      conversation: conversation,
      appSummary: 'Rose talked about the garden.',
      people: people,
    );

    expect(context, contains('Memory title: Coffee in the Garden'));
    expect(context, contains('Linked people: Rose'));
    expect(context, contains('SPEAKER_01: The roses look good today.'));
    expect(context, isNot(contains('create_memory')));
    expect(context, isNot(contains('memory_create')));
  });
}
