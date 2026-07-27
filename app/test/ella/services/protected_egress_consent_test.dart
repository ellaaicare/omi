import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/http/api/messages.dart';
import 'package:omi/backend/http/api/users.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/geolocation.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  test('protected API boundaries fail closed without current server consent', () async {
    final file = File('${Directory.systemTemp.path}/ai-consent-protected-payload');

    expect(await sendMessageStreamServer('private chat').toList(), isEmpty);
    expect(await sendEllaMessageStream('private Ella chat').toList(), isEmpty);
    expect(await sendVoiceMessageStreamServer([file]).toList(), isEmpty);
    await expectLater(uploadFilesServer([file]), throwsStateError);
    expect(await sendStorageToBackend(file, '2026-07-27T00:00:00Z'), isEmpty);
    expect(
      await updateUserGeolocation(
        geolocation: Geolocation(latitude: 1, longitude: 2),
      ),
      isFalse,
    );
    expect(
      await submitConversationCorrection(
        conversationId: 'conversation-a',
        correctionText: 'private correction',
      ),
      isFalse,
    );
    expect(await testConversationPrompt('private prompt', 'conversation-a'), isEmpty);
    expect(await retryConversationProcessing('conversation-a', 'request-a'), isNull);
    expect(await reProcessConversationServer('conversation-a'), isNull);
  });
}
