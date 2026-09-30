import 'package:omi/backend/http/api/conversations.dart' as ella_api;
import 'package:omi/backend/schema/conversation.dart' as ella_schema;
import 'package:omi/ella/upstream_capture/ella_capture_protocol_socket.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/upstream_capture/backend/schema/conversation.dart' as upstream_schema;
import 'package:omi/upstream_capture/services/sockets/transcription_service.dart';

typedef EllaCaptureFinalizationRequest = Future<ella_schema.CreateConversationResponse?> Function({
  required String conversationId,
  required int protocolVersion,
  required String generation,
  required String ownerToken,
  required bool transportLost,
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
});

/// The captured socket, tuple, and account verifier must stay paired across
/// drain, HTTP reconciliation, and the returned conversation.
Future<upstream_schema.CreateConversationResponse?> finalizeEllaCaptureProtocolConversation({
  required EllaCaptureProtocolSocket socket,
  required ExactAccountAuthorityVerifier exactAuthority,
  EllaCaptureFinalizationRequest request = ella_api.processInProgressConversation,
}) async {
  final capture = socket.captureAuthority;
  if (capture == null || !exactAuthority.isExactCurrent()) return null;
  if (socket.state == SocketServiceState.connected) await socket.stop(reason: 'process capture');
  if (!exactAuthority.isExactCurrent()) return null;
  final result = await request(
    conversationId: capture.conversationId,
    protocolVersion: capture.protocolVersion,
    generation: capture.generation,
    ownerToken: capture.ownerToken,
    transportLost: !socket.drainAcknowledged,
    expectedAuthenticatedUid: exactAuthority.uid,
    exactAuthority: exactAuthority,
  );
  if (!exactAuthority.isExactCurrent() || result?.conversation?.id != capture.conversationId) return null;
  return upstream_schema.CreateConversationResponse.fromJson({
    'conversation': result!.conversation!.toJson(),
    'messages': const [],
  });
}
