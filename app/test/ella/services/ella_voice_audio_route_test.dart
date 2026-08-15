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

  test('no-external playback requires a verified speaker classification', () async {
    MethodCall? received;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
      received = call;
      return <String, dynamic>{
        'success': true,
        'routeClassification': 'speaker',
        'usesReceiver': false,
        'usesPhoneSpeaker': true,
      };
    });

    expect(
      await EllaVoiceAudioRoute.ensureAudibleOutput(
        usage: EllaVoiceAudioUsage.playback,
        channel: channel,
        isIos: true,
      ),
      isTrue,
    );
    expect(received?.method, 'ensureAudibleVoiceOutput');
    expect(received?.arguments, {'usage': 'playback'});
  });

  test('standard replies request the playback-only native route', () async {
    MethodCall? received;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
      received = call;
      return <String, dynamic>{'success': true, 'routeClassification': 'speaker', 'outputVolume': 1.0};
    });

    expect(
      await EllaVoiceAudioRoute.ensureAudibleOutput(usage: EllaVoiceAudioUsage.playback, channel: channel, isIos: true),
      isTrue,
    );
    expect(received?.arguments, {'usage': 'playback'});
  });

  test('Bluetooth and wired playback preserve a classified external route', () async {
    for (final outputType in ['BluetoothA2DPOutput', 'Headphones']) {
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
        channel,
        (_) async => <String, dynamic>{
          'success': true,
          'routeClassification': 'external',
          'outputType': outputType,
          'hasHeadset': true,
        },
      );

      expect(
        await EllaVoiceAudioRoute.ensureAudibleOutput(
          usage: EllaVoiceAudioUsage.playback,
          channel: channel,
          isIos: true,
        ),
        isTrue,
        reason: '$outputType must remain audible',
      );
    }
  });

  test('on-device standard fallback uses playback spoken-audio instead of play-and-record', () {
    final configuration = ElevenLabsTts.onDeviceIosAudioConfiguration;

    expect(configuration.category, IosTextToSpeechAudioCategory.playback);
    expect(configuration.mode, IosTextToSpeechAudioMode.spokenAudio);
    expect(configuration.options, contains(IosTextToSpeechAudioCategoryOptions.allowBluetoothA2DP));
    expect(configuration.options, isNot(contains(IosTextToSpeechAudioCategoryOptions.defaultToSpeaker)));
  });

  test('typed native route failure fails closed and reports the actual receiver', () async {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      channel,
      (_) async => <String, dynamic>{
        'success': false,
        'routeClassification': 'receiver',
        'failureCode': 'speaker_selection_failed',
        'usesReceiver': true,
        'usesPhoneSpeaker': false,
      },
    );

    expect(
      await EllaVoiceAudioRoute.ensureAudibleOutput(
        usage: EllaVoiceAudioUsage.playback,
        channel: channel,
        isIos: true,
      ),
      isFalse,
    );
  });

  test('iOS voice route fails closed when native output is absent', () async {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      channel,
      (_) async => <String, dynamic>{
        'success': false,
        'routeClassification': 'unavailable',
        'failureCode': 'route_unavailable',
        'hasOutput': false,
        'usesReceiver': false,
        'usesPhoneSpeaker': false,
      },
    );

    expect(await EllaVoiceAudioRoute.ensureAudibleOutput(channel: channel, isIos: true), isFalse);
  });

  test('interactive restore requests the native mic-capable route and verifies speaker output', () async {
    MethodCall? received;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
      received = call;
      return <String, dynamic>{'success': true, 'routeClassification': 'speaker'};
    });

    expect(
      await EllaVoiceAudioRoute.ensureAudibleOutput(
        usage: EllaVoiceAudioUsage.interactive,
        channel: channel,
        isIos: true,
      ),
      isTrue,
    );

    expect(received?.arguments, {'usage': 'interactive'});
  });

  test('non-iOS voice route does not invoke the iOS channel', () async {
    var calls = 0;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (_) async {
      calls++;
      return <String, dynamic>{'success': true, 'routeClassification': 'speaker'};
    });

    expect(await EllaVoiceAudioRoute.ensureAudibleOutput(channel: channel, isIos: false), isTrue);
    expect(calls, 0);
  });
}
