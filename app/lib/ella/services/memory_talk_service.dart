import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/schema/conversation.dart';

enum CorrectionReplyIntent { affirmative, negative, ambiguous }

class MemoryCorrectionClaim {
  final String originalTurn;
  final String oldValue;
  final String newValue;

  const MemoryCorrectionClaim({
    required this.originalTurn,
    required this.oldValue,
    required this.newValue,
  });

  String get correctionText => '$oldValue should be $newValue. User said: $originalTurn';
}

class MemoryTalkReceipt {
  final String correctionId;
  final String oldValue;
  final String newValue;
  final String beforeTitle;
  final String beforeOverview;
  final String afterTitle;
  final String afterOverview;
  final DateTime appliedAt;
  final bool propagated;

  const MemoryTalkReceipt({
    required this.correctionId,
    required this.oldValue,
    required this.newValue,
    required this.beforeTitle,
    required this.beforeOverview,
    required this.afterTitle,
    required this.afterOverview,
    required this.appliedAt,
    required this.propagated,
  });

  factory MemoryTalkReceipt.fromApi(
    ConversationCorrectionReceipt receipt, {
    required MemoryCorrectionClaim claim,
  }) {
    return MemoryTalkReceipt(
      correctionId: receipt.correctionId,
      oldValue: claim.oldValue,
      newValue: claim.newValue,
      beforeTitle: receipt.beforeTitle,
      beforeOverview: receipt.beforeOverview,
      afterTitle: receipt.afterTitle,
      afterOverview: receipt.afterOverview,
      appliedAt: receipt.appliedAt ?? DateTime.now(),
      propagated: receipt.propagated,
    );
  }
}

String _normalizeReply(String value) {
  return value
      .toLowerCase()
      .replaceAll(RegExp(r'[’‘]'), "'")
      .replaceAll(RegExp(r"[^a-z0-9\s']"), ' ')
      .replaceAll(RegExp(r'\s+'), ' ')
      .trim();
}

bool _containsPhrase(String text, String phrase) {
  return RegExp('(^|\\s)${RegExp.escape(phrase)}(\\s|\$)').hasMatch(text);
}

CorrectionReplyIntent classifyCorrectionReply(String value) {
  final normalized = _normalizeReply(value);
  if (normalized.isEmpty) return CorrectionReplyIntent.ambiguous;

  const negativePhrases = [
    'no',
    'nope',
    'cancel',
    'wrong',
    'not right',
    'not correct',
    "don't",
    'do not',
  ];
  if (negativePhrases.any((phrase) => _containsPhrase(normalized, phrase))) {
    return CorrectionReplyIntent.negative;
  }

  const affirmativePhrases = [
    'yes',
    'yeah',
    'yep',
    'correct',
    'right',
    'exactly',
    "that's right",
    'that is right',
  ];
  if (affirmativePhrases.any((phrase) => _containsPhrase(normalized, phrase))) {
    return CorrectionReplyIntent.affirmative;
  }
  return CorrectionReplyIntent.ambiguous;
}

String _cleanEntity(String value) {
  return value
      .replaceAll(RegExp(r'^[\s,.;:—–-]+|[\s,.;:—–-]+$'), '')
      .replaceAll(RegExp(r'\s+(who|that|which)\b.*$', caseSensitive: false), '')
      .trim();
}

MemoryCorrectionClaim? extractCorrectionClaim(
  String userTurn,
  ServerConversation conversation,
) {
  final turn = userTurn.trim();
  if (turn.isEmpty) return null;

  final wasntThenWas = RegExp(
    r"\b(?:it\s+)?was(?:n't|n’t|\s+not)\s+([A-Z][A-Za-z'’-]*(?:\s+[A-Z][A-Za-z'’-]*){0,2}).*?\b(?:it\s+)?was\s+([A-Z][A-Za-z'’-]*(?:\s+[A-Z][A-Za-z'’-]*){0,2})",
    caseSensitive: false,
  ).firstMatch(turn);
  if (wasntThenWas != null) {
    final oldValue = _cleanEntity(wasntThenWas.group(1) ?? '');
    final newValue = _cleanEntity(wasntThenWas.group(2) ?? '');
    if (_validReplacement(oldValue, newValue, conversation)) {
      return MemoryCorrectionClaim(originalTurn: turn, oldValue: oldValue, newValue: newValue);
    }
  }

  final wasThenNot = RegExp(
    r"\b(?:it\s+)?was\s+([A-Z][A-Za-z'’-]*(?:\s+[A-Z][A-Za-z'’-]*){0,2}).*?\bnot\s+([A-Z][A-Za-z'’-]*(?:\s+[A-Z][A-Za-z'’-]*){0,2})",
    caseSensitive: false,
  ).firstMatch(turn);
  if (wasThenNot != null) {
    final newValue = _cleanEntity(wasThenNot.group(1) ?? '');
    final oldValue = _cleanEntity(wasThenNot.group(2) ?? '');
    if (_validReplacement(oldValue, newValue, conversation)) {
      return MemoryCorrectionClaim(originalTurn: turn, oldValue: oldValue, newValue: newValue);
    }
  }
  return null;
}

bool _validReplacement(
  String oldValue,
  String newValue,
  ServerConversation conversation,
) {
  if (oldValue.isEmpty || newValue.isEmpty || oldValue.toLowerCase() == newValue.toLowerCase()) {
    return false;
  }
  final memoryText = '${conversation.structured.title} ${conversation.structured.overview}'.toLowerCase();
  return memoryText.contains(oldValue.toLowerCase());
}

String replaceMemoryValue(String value, MemoryCorrectionClaim claim) {
  return value.replaceAll(RegExp(RegExp.escape(claim.oldValue), caseSensitive: false), claim.newValue);
}
