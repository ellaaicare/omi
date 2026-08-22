import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_tts/flutter_tts.dart';

import 'package:omi/ella/services/elevenlabs_tts.dart';
import 'package:omi/ella/services/ella_voice_audio_route.dart';

class _FakeOnDeviceTtsAdapter implements OnDeviceTtsAdapter {
  _FakeOnDeviceTtsAdapter(this.operations);

  final List<String> operations;
  VoidCallback? _completionHandler;
  late IosTextToSpeechAudioCategory category;
  late List<IosTextToSpeechAudioCategoryOptions> options;
  late IosTextToSpeechAudioMode mode;

  @override
  Future<dynamic> setSharedInstance(bool sharedSession) async {
    operations.add('tts.sharedInstance:$sharedSession');
    return 1;
  }

  @override
  Future<dynamic> autoStopSharedSession(bool autoStop) async {
    operations.add('tts.autoStop:$autoStop');
    return 1;
  }

  @override
  Future<dynamic> setIosAudioCategory(
    IosTextToSpeechAudioCategory category,
    List<IosTextToSpeechAudioCategoryOptions> options,
    IosTextToSpeechAudioMode mode,
  ) async {
    operations.add('tts.configureAudioSession');
    this.category = category;
    this.options = options;
    this.mode = mode;
    return 1;
  }

  @override
  Future<dynamic> setLanguage(String language) async {
    operations.add('tts.language');
    return 1;
  }

  @override
  Future<dynamic> setSpeechRate(double rate) async {
    operations.add('tts.rate');
    return 1;
  }

  @override
  Future<dynamic> setPitch(double pitch) async {
    operations.add('tts.pitch');
    return 1;
  }

  @override
  Future<dynamic> setVolume(double volume) async {
    operations.add('tts.volume');
    return 1;
  }

  @override
  Future<dynamic> awaitSpeakCompletion(bool awaitCompletion) async {
    operations.add('tts.awaitCompletion:$awaitCompletion');
    return 1;
  }

  @override
  void setCompletionHandler(VoidCallback handler) {
    _completionHandler = handler;
  }

  @override
  void setErrorHandler(void Function(dynamic message) handler) {}

  @override
  void setCancelHandler(VoidCallback handler) {}

  @override
  Future<dynamic> speak(String text) async {
    operations.add('tts.speak');
    _completionHandler?.call();
    return 1;
  }

  @override
  Future<dynamic> stop() async => 1;
}

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
      await EllaVoiceAudioRoute.ensureAudibleOutput(
        usage: EllaVoiceAudioUsage.playback,
        channel: channel,
        isIos: true,
      ),
      isTrue,
    );
    expect(received?.arguments, {'usage': 'playback'});
  });

  test('interactive teardown asks native audio ownership to release', () async {
    MethodCall? received;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
      received = call;
      return true;
    });

    expect(
      await EllaVoiceAudioRoute.release(usage: EllaVoiceAudioUsage.interactive, channel: channel, isIos: true),
      isTrue,
    );
    expect(received?.method, 'releaseVoiceAudioSession');
    expect(received?.arguments, {'usage': 'interactive'});
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

  test('on-device standard fallback uses Ella-compatible shared audio-session settings', () {
    final configuration = ElevenLabsTts.onDeviceIosAudioConfiguration;

    expect(configuration.category, IosTextToSpeechAudioCategory.playback);
    expect(configuration.mode, IosTextToSpeechAudioMode.defaultMode);
    expect(configuration.options, isNot(contains(IosTextToSpeechAudioCategoryOptions.defaultToSpeaker)));
    expect(configuration.options, isNot(contains(IosTextToSpeechAudioCategoryOptions.allowBluetooth)));
    expect(configuration.options, contains(IosTextToSpeechAudioCategoryOptions.allowBluetoothA2DP));
    expect(configuration.options, contains(IosTextToSpeechAudioCategoryOptions.allowAirPlay));
  });

  test('on-device TTS re-verifies Ella route after plugin setup immediately before speech', () async {
    final operations = <String>[];
    final adapter = _FakeOnDeviceTtsAdapter(operations);

    await ElevenLabsTts.speakOnDevice(
      'Audible reply',
      adapter: adapter,
      isIos: true,
      audibleOutputEnforcer: () async {
        operations.add('ella.verifyPlaybackRoute');
        return true;
      },
    );

    expect(adapter.category, IosTextToSpeechAudioCategory.playback);
    expect(adapter.options, containsAll(ElevenLabsTts.onDeviceIosAudioConfiguration.options));
    expect(adapter.mode, IosTextToSpeechAudioMode.defaultMode);
    expect(operations, [
      'tts.sharedInstance:true',
      'tts.autoStop:true',
      'tts.configureAudioSession',
      'tts.language',
      'tts.rate',
      'tts.pitch',
      'tts.volume',
      'tts.awaitCompletion:false',
      'ella.verifyPlaybackRoute',
      'tts.speak',
    ]);
  });

  test('on-device TTS fails closed without speaking when post-setup route verification fails', () async {
    final operations = <String>[];
    final adapter = _FakeOnDeviceTtsAdapter(operations);

    await expectLater(
      ElevenLabsTts.speakOnDevice(
        'Do not speak',
        adapter: adapter,
        isIos: true,
        audibleOutputEnforcer: () async {
          operations.add('ella.verifyPlaybackRoute');
          return false;
        },
      ),
      throwsStateError,
    );

    expect(operations.last, 'ella.verifyPlaybackRoute');
    expect(operations, isNot(contains('tts.speak')));
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
