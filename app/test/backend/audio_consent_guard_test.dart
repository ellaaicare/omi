import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/api/messages.dart';
import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/elevenlabs_tts.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('voice transcription fails before upload without AI consent', () async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();

    await expectLater(transcribeVoiceMessage(File('unused.wav')), throwsStateError);
    await expectLater(syncLocalFiles([File('unused.wav')]), throwsStateError);
    expect(await ElevenLabsTts.synthesize('This must stay on device.'), isNull);
  });
}
