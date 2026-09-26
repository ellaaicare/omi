import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/services/ella_service_result.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/platform/platform_manager.dart';

class _CurrentAuthority implements AccountCommitAuthority {
  @override
  String get uid => 'owner';

  @override
  bool isCurrent() => true;

  @override
  bool isExactCurrent() => true;
}

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
    expect(provider.lastStreamFailure?.kind, ClientApiFailureKind.unavailable);
    expect(provider.lastStreamFailure?.retryable, isTrue);
  });

  test('a failed local user turn survives an authoritative history refresh', () async {
    final authority = _CurrentAuthority();
    final serverMessage = ServerMessage(
      'server-message',
      DateTime.utc(2026, 9, 26, 19),
      'Earlier server turn',
      MessageSender.ai,
      MessageType.text,
      null,
      false,
      const [],
      const [],
      const [],
    );
    final provider = MessageProvider(
      activeAuthority: () => authority,
      aiConsentEnsurer: () async => true,
      ellaChatStreamSender: (_, {expectedAuthenticatedUid, exactAuthority}) =>
          Stream.error(const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true)),
      ellaChatHistoryRetriever: ({required limit, required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(expectedAuthenticatedUid, 'owner');
        return EllaServiceResult.success([serverMessage]);
      },
    );
    provider.addMessageLocally('Question that failed');

    await provider.sendMessageStreamToServer('Question that failed');
    await provider.refreshMessages();

    expect(
      provider.messages.map((message) => message.text),
      containsAll(['Earlier server turn', 'Question that failed']),
    );
    expect(provider.canRetryLastMessage, isTrue);
    expect(provider.lastStreamFailure?.kind, ClientApiFailureKind.unavailable);
  });
}
