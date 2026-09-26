import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/providers/message_provider.dart';
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
    expect(provider.lastStreamFailure?.kind, ClientApiFailureKind.unavailable);
    expect(provider.lastStreamFailure?.retryable, isTrue);
  });
}
