import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/services/standard_voice_turn.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class _SyntheticAuthority implements ExactAccountAuthorityVerifier {
  @override
  String get uid => 'synthetic-owner';
  @override
  bool isExactCurrent() => true;
}

void main() {
  test('standard voice error with open stream never commits or speaks and ends promptly', () async {
    final stream = StreamController<ServerMessageChunk>();
    addTearDown(stream.close);
    var commitments = 0;
    var replies = 0;
    var syntheses = 0;
    var playbacks = 0;
    final coordinator = StandardVoiceTurnCoordinator(
      streamSender: (text, {expectedAuthenticatedUid, exactAuthority}) => stream.stream,
      synthesizer: (text, {expectedAuthenticatedUid, exactAuthority}) async {
        syntheses++;
        return null;
      },
    );
    final pending = coordinator.run(
      transcript: 'Synthetic voice failure',
      authority: _SyntheticAuthority(),
      commitMessages: (_, __) {
        commitments++;
        return true;
      },
      onReplyReady: (_) {
        replies++;
      },
      playFile: (_) async {
        playbacks++;
      },
      speakOnDevice: (_) async {
        playbacks++;
      },
    );
    stream.add(ServerMessageChunk('synthetic-partial', 'Partial response', MessageChunkType.data));
    stream.add(ServerMessageChunk('synthetic-error', '', MessageChunkType.error));
    final result = await pending.timeout(const Duration(seconds: 1));
    expect(result.failure?.kind, ClientApiFailureKind.invalidResponse);
    expect(result.reply, isEmpty);
    expect(commitments, 0);
    expect(replies, 0);
    expect(syntheses, 0);
    expect(playbacks, 0);
    expect(stream.hasListener, isFalse);
  });
}
