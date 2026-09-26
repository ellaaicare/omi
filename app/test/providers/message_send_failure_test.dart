import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
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
    final provider = MessageProvider(
      activeAuthority: () => const _CurrentAuthority(),
      aiConsentEnsurer: () async => true,
      ellaChatStreamSender: (text, {expectedAuthenticatedUid, exactAuthority}) async* {
        attempts++;
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
