import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_tts/flutter_tts.dart';

import 'package:omi/ella/services/elevenlabs_tts.dart';
import 'package:omi/ella/services/ella_voice_audio_route.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  const channel = MethodChannel('com.ellaaicare.ella/audio_route.test');

  tearDown(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, null);
  });

  test(
    'iOS voice route requires an acknowledged non-receiver output',
    () async {
      MethodCall? received;
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
        received = call;
        return <String, dynamic>{
          'success': true,
          'usesReceiver': false,
          'usesPhoneSpeaker': true,
        };
      });

      expect(
        await EllaVoiceAudioRoute.ensureAudibleOutput(
          channel: channel,
          isIos: true,
        ),
        isTrue,
      );
      expect(received?.method, 'ensureAudibleVoiceOutput');
      expect(received?.arguments, {'usage': 'interactive'});
    },
  );

  test('standard replies request the playback-only native route', () async {
    MethodCall? received;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
      received = call;
      return <String, dynamic>{'success': true, 'usesReceiver': false, 'outputVolume': 1.0};
    });

    expect(
      await EllaVoiceAudioRoute.ensureAudibleOutput(
        usage: EllaVoiceAudioUsage.playback,
        channel: channel,
        isIos: true,
      ),
      isTrue,
    );
    expect(received?.arguments, {'usage': 'playback'});
  });

  test('on-device standard fallback uses playback spoken-audio instead of play-and-record', () {
    final configuration = ElevenLabsTts.onDeviceIosAudioConfiguration;

    expect(configuration.category, IosTextToSpeechAudioCategory.playback);
    expect(configuration.mode, IosTextToSpeechAudioMode.spokenAudio);
    expect(configuration.options, contains(IosTextToSpeechAudioCategoryOptions.allowBluetoothA2DP));
    expect(configuration.options, isNot(contains(IosTextToSpeechAudioCategoryOptions.defaultToSpeaker)));
  });

  test(
    'iOS voice route fails closed when output remains on the receiver',
    () async {
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
        channel,
        (_) async => <String, dynamic>{
          'success': true,
          'usesReceiver': true,
          'usesPhoneSpeaker': false,
        },
      );

      expect(
        await EllaVoiceAudioRoute.ensureAudibleOutput(
          channel: channel,
          isIos: true,
        ),
        isFalse,
      );
    },
  );

  test('iOS voice route fails closed when native output is absent', () async {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      channel,
      (_) async => <String, dynamic>{
        'success': false,
        'hasOutput': false,
        'usesReceiver': false,
        'usesPhoneSpeaker': false,
      },
    );

    expect(
      await EllaVoiceAudioRoute.ensureAudibleOutput(channel: channel, isIos: true),
      isFalse,
    );
  });

  test('non-iOS voice route does not invoke the iOS channel', () async {
    var calls = 0;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (_) async {
      calls++;
      return <String, dynamic>{'success': true, 'usesReceiver': false};
    });

    expect(
      await EllaVoiceAudioRoute.ensureAudibleOutput(
        channel: channel,
        isIos: false,
      ),
      isTrue,
    );
    expect(calls, 0);
  });
}
