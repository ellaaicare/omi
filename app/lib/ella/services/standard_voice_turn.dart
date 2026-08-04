import 'dart:async';

import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/services/ella_chat_service.dart';
import 'package:omi/ella/services/elevenlabs_tts.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

typedef StandardVoiceStreamSender = Stream<ServerMessageChunk> Function(
  String text, {
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
});
typedef StandardVoiceSynthesizer = Future<String?> Function(
  String text, {
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
});

class StandardVoiceTurnResult {
  const StandardVoiceTurnResult({
    required this.reply,
    this.discarded = false,
    this.usedOnDeviceTts = false,
    this.failure,
  });

  final String reply;
  final bool discarded;
  final bool usedOnDeviceTts;
  final ClientApiFailure? failure;
}

/// Runs one non-V2V voice turn under one exact UID + authority generation.
class StandardVoiceTurnCoordinator {
  const StandardVoiceTurnCoordinator({
    this.streamSender = sendEllaChatStream,
    this.synthesizer = ElevenLabsTts.synthesize,
  });

  final StandardVoiceStreamSender streamSender;
  final StandardVoiceSynthesizer synthesizer;

  Future<StandardVoiceTurnResult> run({
    required String transcript,
    required ExactAccountAuthorityVerifier authority,
    required bool Function(String transcript, String reply) commitMessages,
    required FutureOr<void> Function(String reply) onReplyReady,
    required Future<void> Function(String audioPath) playFile,
    required Future<void> Function(String text) speakOnDevice,
    String Function(String text)? prepareTtsText,
  }) async {
    String? audioPath;
    try {
      if (!authority.isExactCurrent()) return const StandardVoiceTurnResult(reply: '', discarded: true);
      final replyBuffer = StringBuffer();
      await for (final chunk in streamSender(
        transcript,
        expectedAuthenticatedUid: authority.uid,
        exactAuthority: authority,
      )) {
        if (!authority.isExactCurrent()) return const StandardVoiceTurnResult(reply: '', discarded: true);
        if (chunk.type == MessageChunkType.data) {
          replyBuffer.write(chunk.text);
        } else if (chunk.type == MessageChunkType.done && chunk.message?.text.isNotEmpty == true) {
          replyBuffer
            ..clear()
            ..write(chunk.message!.text);
        } else if (chunk.type == MessageChunkType.error) {
          throw const ClientApiFailure(ClientApiFailureKind.invalidResponse);
        }
      }

      if (!authority.isExactCurrent()) return const StandardVoiceTurnResult(reply: '', discarded: true);
      final reply = replyBuffer.toString().trim();
      if (reply.isEmpty) return const StandardVoiceTurnResult(reply: '');
      if (!commitMessages(transcript, reply) || !authority.isExactCurrent()) {
        return const StandardVoiceTurnResult(reply: '', discarded: true);
      }

      await onReplyReady(reply);
      if (!authority.isExactCurrent()) return const StandardVoiceTurnResult(reply: '', discarded: true);

      final boundedReply = reply.length > 500 ? reply.substring(0, 500) : reply;
      final ttsText = prepareTtsText?.call(boundedReply) ?? boundedReply;
      audioPath = await synthesizer(ttsText, expectedAuthenticatedUid: authority.uid, exactAuthority: authority);
      if (!authority.isExactCurrent()) {
        if (audioPath != null) await ElevenLabsTts.discardSynthesizedFile(audioPath);
        return const StandardVoiceTurnResult(reply: '', discarded: true);
      }

      if (audioPath == null) {
        await speakOnDevice(ttsText);
        if (!authority.isExactCurrent()) return const StandardVoiceTurnResult(reply: '', discarded: true);
        return StandardVoiceTurnResult(reply: reply, usedOnDeviceTts: true);
      }

      try {
        await playFile(audioPath);
      } on ExactAccountAuthorityChangedException {
        rethrow;
      } catch (_) {
        if (!authority.isExactCurrent()) return const StandardVoiceTurnResult(reply: '', discarded: true);
        await speakOnDevice(ttsText);
        if (!authority.isExactCurrent()) return const StandardVoiceTurnResult(reply: '', discarded: true);
        return StandardVoiceTurnResult(reply: reply, usedOnDeviceTts: true);
      }
      if (!authority.isExactCurrent()) {
        await ElevenLabsTts.discardSynthesizedFile(audioPath);
        return const StandardVoiceTurnResult(reply: '', discarded: true);
      }
      return StandardVoiceTurnResult(reply: reply);
    } on ClientApiFailure catch (failure) {
      if (audioPath != null) await ElevenLabsTts.discardSynthesizedFile(audioPath);
      return StandardVoiceTurnResult(reply: '', failure: failure);
    } on ExactAccountAuthorityChangedException {
      if (audioPath != null) await ElevenLabsTts.discardSynthesizedFile(audioPath);
      return const StandardVoiceTurnResult(reply: '', discarded: true);
    }
  }
}
