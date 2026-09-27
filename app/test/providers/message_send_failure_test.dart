import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/services/ella_service_result.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/platform/platform_manager.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  PlatformManager.initializeForTesting();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  test('a send that cannot start shows a retryable failure instead of staying held', () async {
    final provider = MessageProvider(activeAuthority: () => null);
    provider.setSendingMessage(true);

    await provider.sendMessageStreamToServer('hello');

    expect(provider.sendingMessage, isFalse);
    expect(provider.canRetryLastMessage, isTrue);
    expect(provider.lastStreamFailure, isNull, reason: 'turn failures render with the retained user message');
    expect(provider.messages.single.text, 'hello');
    expect(provider.messages.single.clientDeliveryState, ClientMessageDeliveryState.failed);
  });

  test('multiple failed turns remain independently visible and retry targets the original turn', () async {
    var attempts = 0;
    final clientMessageIds = <String>[];
    final clientSentAts = <DateTime>[];
    final provider = MessageProvider(
      activeAuthority: () => const _CurrentAuthority(),
      aiConsentEnsurer: () async => true,
      ellaChatTurnLookup: ({
        required clientMessageId,
        required expectedAuthenticatedUid,
        required exactAuthority,
      }) async =>
          const EllaServiceResult.success(false),
      ellaChatStreamSender: (
        text, {
        required clientMessageId,
        required clientSentAt,
        expectedAuthenticatedUid,
        exactAuthority,
      }) async* {
        attempts++;
        clientMessageIds.add(clientMessageId);
        clientSentAts.add(clientSentAt);
        if (attempts <= 2) throw const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true);
        yield ServerMessageChunk(
          'assistant-1',
          '',
          MessageChunkType.done,
          message: ServerMessage(
            'assistant-1',
            DateTime.now(),
            'Recovered',
            MessageSender.ai,
            MessageType.text,
            null,
            false,
            [],
            [],
            [],
          ),
        );
      },
    );

    await provider.sendMessageStreamToServer('first');
    await provider.sendMessageStreamToServer('second');

    final failed = provider.messages.where(
      (message) => message.clientDeliveryState == ClientMessageDeliveryState.failed,
    );
    expect(failed.map((message) => message.text), ['first', 'second']);
    final firstId = failed.first.id;

    await provider.retryFailedMessage(firstId);

    expect(provider.messages.firstWhere((message) => message.id == firstId).clientDeliveryState, isNull);
    expect(
      provider.messages.firstWhere((message) => message.text == 'second').clientDeliveryState,
      ClientMessageDeliveryState.failed,
    );
    expect(provider.messages.where((message) => message.sender == MessageSender.ai).single.text, 'Recovered');
    expect(clientMessageIds[2], clientMessageIds[0]);
    expect(clientMessageIds[1], isNot(clientMessageIds[0]));
    expect(clientSentAts.last, clientSentAts.first);
  });

  test('an exact canonical turn match resolves a lost-ACK failure without a second stream', () async {
    var attempts = 0;
    var lookups = 0;
    String? retainedTurnId;
    DateTime? retainedSentAt;
    final provider = MessageProvider(
      activeAuthority: () => const _CurrentAuthority(),
      aiConsentEnsurer: () async => true,
      ellaChatStreamSender: (
        text, {
        required clientMessageId,
        required clientSentAt,
        expectedAuthenticatedUid,
        exactAuthority,
      }) async* {
        attempts++;
        retainedTurnId = clientMessageId;
        retainedSentAt = clientSentAt;
        throw const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true);
      },
      ellaChatTurnLookup: ({
        required clientMessageId,
        required expectedAuthenticatedUid,
        required exactAuthority,
      }) async {
        lookups++;
        expect(clientMessageId, retainedTurnId);
        expect(expectedAuthenticatedUid, 'uid-a');
        expect(exactAuthority.uid, 'uid-a');
        return const EllaServiceResult.success(true);
      },
    );

    await provider.sendMessageStreamToServer('persisted before the ACK was lost');
    final failedLocalId = provider.messages.single.id;
    expect(provider.messages.single.canonicalTurnId, retainedTurnId);

    await provider.retryFailedMessage(failedLocalId);

    expect(attempts, 1);
    expect(lookups, 1);
    expect(provider.messages, hasLength(1));
    expect(provider.messages.single.id, failedLocalId);
    expect(provider.messages.single.canonicalTurnId, retainedTurnId);
    expect(provider.messages.single.createdAt.toUtc(), retainedSentAt);
    expect(provider.messages.single.clientDeliveryState, isNull);
  });

  test('exact lookup bypasses bounded history and prevents a second provider invocation', () async {
    var attempts = 0;
    var lookups = 0;
    var historyReads = 0;
    String? retainedTurnId;
    final provider = MessageProvider(
      activeAuthority: () => const _CurrentAuthority(),
      aiConsentEnsurer: () async => true,
      ellaChatStreamSender: (
        text, {
        required clientMessageId,
        required clientSentAt,
        expectedAuthenticatedUid,
        exactAuthority,
      }) async* {
        attempts++;
        retainedTurnId = clientMessageId;
        throw const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true);
      },
      ellaChatTurnLookup: ({
        required clientMessageId,
        required expectedAuthenticatedUid,
        required exactAuthority,
      }) async {
        lookups++;
        expect(clientMessageId, retainedTurnId);
        return const EllaServiceResult.success(true);
      },
      ellaChatHistoryFetcher: ({
        required limit,
        required before,
        required expectedAuthenticatedUid,
        required exactAuthority,
      }) async {
        historyReads++;
        return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.invalidResponse));
      },
    );

    await provider.sendMessageStreamToServer('persisted before the ACK was lost');
    final failedLocalId = provider.messages.single.id;
    await provider.retryFailedMessage(failedLocalId);

    expect(lookups, 1);
    expect(historyReads, 0);
    expect(attempts, 1);
    expect(provider.messages.where((message) => message.canonicalTurnId == retainedTurnId), hasLength(1));
    expect(provider.messages.single.clientDeliveryState, isNull);
  });

  test('retry fails closed when exact turn authority cannot be verified', () async {
    var attempts = 0;
    var lookups = 0;
    final provider = MessageProvider(
      activeAuthority: () => const _CurrentAuthority(),
      aiConsentEnsurer: () async => true,
      ellaChatStreamSender: (
        text, {
        required clientMessageId,
        required clientSentAt,
        expectedAuthenticatedUid,
        exactAuthority,
      }) async* {
        attempts++;
        throw const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true);
      },
      ellaChatTurnLookup: ({
        required clientMessageId,
        required expectedAuthenticatedUid,
        required exactAuthority,
      }) async {
        lookups++;
        return const EllaServiceResult.failure(
          ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true),
        );
      },
    );

    await provider.sendMessageStreamToServer('must not be sent twice');
    final failedLocalId = provider.messages.single.id;
    await provider.retryFailedMessage(failedLocalId);

    expect(lookups, 1);
    expect(attempts, 1);
    expect(provider.messages.single.clientDeliveryState, ClientMessageDeliveryState.failed);
    expect(provider.lastStreamFailure?.kind, ClientApiFailureKind.unavailable);
  });

  test('retry fails closed when exact turn lookup returns no authority decision', () async {
    var attempts = 0;
    final provider = MessageProvider(
      activeAuthority: () => const _CurrentAuthority(),
      aiConsentEnsurer: () async => true,
      ellaChatStreamSender: (
        text, {
        required clientMessageId,
        required clientSentAt,
        expectedAuthenticatedUid,
        exactAuthority,
      }) async* {
        attempts++;
        throw const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true);
      },
      ellaChatTurnLookup: ({
        required clientMessageId,
        required expectedAuthenticatedUid,
        required exactAuthority,
      }) async =>
          const EllaServiceResult<bool>.success(),
    );

    await provider.sendMessageStreamToServer('must remain retryable');
    final failedLocalId = provider.messages.single.id;
    await provider.retryFailedMessage(failedLocalId);

    expect(attempts, 1);
    expect(provider.messages.single.clientDeliveryState, ClientMessageDeliveryState.failed);
    expect(provider.lastStreamFailure?.kind, ClientApiFailureKind.invalidResponse);
  });
}

class _CurrentAuthority implements AccountCommitAuthority {
  const _CurrentAuthority();

  @override
  String get uid => 'uid-a';

  @override
  bool isCurrent() => true;

  @override
  bool isExactCurrent() => true;
}
