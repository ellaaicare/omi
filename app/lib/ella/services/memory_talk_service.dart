import 'dart:async';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/backend/schema/person.dart';
import 'package:omi/ella/services/ella_chat_service.dart';

class MemoryTalkMessage {
  final String text;
  final bool isUser;
  final DateTime createdAt;

  const MemoryTalkMessage({required this.text, required this.isUser, required this.createdAt});
}

class PendingMemoryCorrectionClaim {
  final String correctionText;
  final String oldText;
  final String newText;

  const PendingMemoryCorrectionClaim({required this.correctionText, required this.oldText, required this.newText});

  String confirmationText(String Function(String newText, String oldText) builder) => builder(newText, oldText);
}

class MemoryTalkCorrectionExtractor {
  const MemoryTalkCorrectionExtractor();

  PendingMemoryCorrectionClaim? extract(String text) {
    final trimmed = text.trim();
    if (trimmed.isEmpty) return null;

    final newThenOld = RegExp(
      r"\b(?:it\s+was\s+|that\s+was\s+|actually\s+|it\s+is\s+)?([A-Z][A-Za-z .'-]{1,80}?)\s*,?\s+not\s+([A-Z][A-Za-z .'-]{1,80})(?:[.!?]|$)",
      caseSensitive: true,
    ).firstMatch(trimmed);
    if (newThenOld != null) {
      return PendingMemoryCorrectionClaim(
        correctionText: trimmed,
        newText: _cleanClaimPart(newThenOld.group(1) ?? ''),
        oldText: _cleanClaimPart(newThenOld.group(2) ?? ''),
      );
    }

    final oldThenNew = RegExp(
      r"\bnot\s+([A-Z][A-Za-z .'-]{1,80}?)[,;: -]+(?:it\s+was\s+|that\s+was\s+|actually\s+)?([A-Z][A-Za-z .'-]{1,80})(?:[.!?]|$)",
      caseSensitive: true,
    ).firstMatch(trimmed);
    if (oldThenNew != null) {
      return PendingMemoryCorrectionClaim(
        correctionText: trimmed,
        oldText: _cleanClaimPart(oldThenNew.group(1) ?? ''),
        newText: _cleanClaimPart(oldThenNew.group(2) ?? ''),
      );
    }

    return null;
  }

  String _cleanClaimPart(String value) {
    return value
        .replaceAll(RegExp(r'\s+'), ' ')
        .replaceAll(RegExp(r'^(it was|that was|actually)\s+', caseSensitive: false), '')
        .trim()
        .replaceAll(RegExp(r'[.!?,;:]+$'), '');
  }
}

bool isAffirmativeCorrectionReply(String text) {
  final normalized = text.trim().toLowerCase();
  return {
    'yes',
    'yeah',
    'yep',
    'correct',
    'right',
    'that is right',
    "that's right",
    'please do',
    'do it',
    'yes please',
  }.contains(normalized);
}

bool isNegativeCorrectionReply(String text) {
  final normalized = text.trim().toLowerCase();
  return {'no', 'nope', 'not quite', 'not right', "that's not right", 'cancel'}.contains(normalized);
}

String buildMemoryTalkContext({
  required ServerConversation conversation,
  required String appSummary,
  required List<Person> people,
}) {
  final linkedPersonIds =
      conversation.transcriptSegments.map((segment) => segment.personId).whereType<String>().toSet();
  final linkedPeople = people
      .where((person) => linkedPersonIds.contains(person.id))
      .map((person) => person.name)
      .where((name) => name.trim().isNotEmpty)
      .toList();
  final transcript = conversation.transcriptSegments
      .where((segment) => segment.text.trim().isNotEmpty)
      .map((segment) => '${segment.speaker ?? 'Speaker'}: ${segment.text.trim()}')
      .join('\n');

  return [
    'Memory title: ${conversation.structured.title}',
    'Memory date: ${(conversation.startedAt ?? conversation.createdAt).toIso8601String()}',
    if (conversation.structured.overview.trim().isNotEmpty)
      'Memory summary: ${conversation.structured.overview.trim()}',
    if (appSummary.trim().isNotEmpty) 'App summary: ${appSummary.trim()}',
    if (linkedPeople.isNotEmpty) 'Linked people: ${linkedPeople.join(', ')}',
    if (transcript.isNotEmpty) 'Memory transcript:\n$transcript',
  ].join('\n\n');
}

Stream<ServerMessageChunk> sendMemoryScopedEllaChatStream({
  required ServerConversation conversation,
  required String text,
  required String scopedContext,
}) {
  return sendEllaChatStream(
    text,
    conversationId: conversation.id,
    scopedContext: scopedContext,
    scope: 'memory',
    scopeConversationId: conversation.id,
    persistToMainChat: false,
  );
}
