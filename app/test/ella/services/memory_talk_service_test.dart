import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/services/memory_talk_service.dart';

ServerConversation _gardenMemory() {
  return ServerConversation(
    id: 'garden',
    createdAt: DateTime(2026, 7, 23, 9, 40),
    structured: Structured(
      'Coffee in the garden with Margaret',
      'You had coffee in the garden with Margaret this morning.',
    ),
  );
}

void main() {
  group('natural-language correction confirmation', () {
    test('affirmatives include a natural near-match phrase', () {
      expect(classifyCorrectionReply("yeah, that's right"), CorrectionReplyIntent.affirmative);
      expect(classifyCorrectionReply('Yes, it was Rose'), CorrectionReplyIntent.affirmative);
    });

    test('denials discard even when phrased naturally', () {
      expect(classifyCorrectionReply("No, I don't think so"), CorrectionReplyIntent.negative);
      expect(classifyCorrectionReply('That is not right'), CorrectionReplyIntent.negative);
    });

    test('standalone apostrophized replies preserve straight and curly apostrophes', () {
      expect(classifyCorrectionReply("don't"), CorrectionReplyIntent.negative);
      expect(classifyCorrectionReply('don’t'), CorrectionReplyIntent.negative);
      expect(classifyCorrectionReply("that's right"), CorrectionReplyIntent.affirmative);
      expect(classifyCorrectionReply('that’s right'), CorrectionReplyIntent.affirmative);
    });

    test('ambiguous replies stay pending for one explicit reprompt', () {
      expect(classifyCorrectionReply('Maybe Rose stopped later'), CorrectionReplyIntent.ambiguous);
    });
  });

  test('extracts a high-confidence person replacement from a scoped turn', () {
    final claim = extractCorrectionClaim(
      "Actually, it wasn’t Margaret — it was Rose who came by.",
      _gardenMemory(),
    );

    expect(claim, isNotNull);
    expect(claim!.oldValue, 'Margaret');
    expect(claim.newValue, 'Rose');
    expect(replaceMemoryValue(_gardenMemory().structured.title, claim), 'Coffee in the garden with Rose');
  });

  test('does not treat ambient musing as a correction', () {
    final claim = extractCorrectionClaim(
      'I miss those mornings in the garden.',
      _gardenMemory(),
    );

    expect(claim, isNull);
  });

  test('strips leading relation words so "with Rose" does not double the preposition', () {
    final claim = extractCorrectionClaim(
      'It was with Rose, not Margaret.',
      _gardenMemory(),
    );

    expect(claim, isNotNull);
    expect(claim!.oldValue, 'Margaret');
    expect(claim.newValue, 'Rose');
    // Must produce "...with Rose", never "...with with Rose".
    expect(replaceMemoryValue(_gardenMemory().structured.title, claim), 'Coffee in the garden with Rose');
  });
}
