import 'dart:async';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:just_audio_platform_interface/just_audio_platform_interface.dart';
import 'package:omi/ella/services/admitted_audio_player.dart';
import 'package:omi/ella/services/app_output_admission.dart';
import 'package:omi/ella/services/elevenlabs_tts.dart';
import 'package:omi/ella/services/emergency_audio_player.dart';
import 'package:omi/ella/services/whisper_delivery_runtime.dart';

class _Platform extends JustAudioPlatform {
  Completer<void>? loadGate, disposeGate;
  bool failDispose = false;
  int initializations = 0, disposals = 0, plays = 0;
  late _Player player;
  @override
  Future<AudioPlayerPlatform> init(InitRequest request) async {
    initializations++;
    return player = _Player(request.id, this);
  }

  @override
  Future<DisposePlayerResponse> disposePlayer(DisposePlayerRequest request) async {
    disposals++;
    await disposeGate?.future;
    if (failDispose) throw StateError('physical disposal failed');
    return DisposePlayerResponse();
  }
}

class _Player extends AudioPlayerPlatform {
  _Player(super.id, this.owner);
  final _Platform owner;
  Completer<void>? playingReceipt;
  @override
  Stream<PlaybackEventMessage> get playbackEventMessageStream => events.stream;
  final events = StreamController<PlaybackEventMessage>.broadcast();
  @override
  Future<LoadResponse> load(LoadRequest request) async {
    await owner.loadGate?.future;
    events.add(PlaybackEventMessage(
        processingState: ProcessingStateMessage.ready,
        updateTime: DateTime.now(),
        updatePosition: Duration.zero,
        bufferedPosition: Duration.zero,
        duration: const Duration(seconds: 1),
        icyMetadata: null,
        currentIndex: 0,
        androidAudioSessionId: null));
    return LoadResponse(duration: const Duration(seconds: 1));
  }

  @override
  Future<PlayResponse> play(PlayRequest request) async {
    owner.plays++;
    playingReceipt = Completer<void>();
    await playingReceipt!.future;
    return PlayResponse();
  }

  @override
  Future<PauseResponse> pause(PauseRequest request) async {
    if (playingReceipt?.isCompleted == false) playingReceipt!.complete();
    return PauseResponse();
  }

  @override
  Future<DisposeResponse> dispose(DisposeRequest request) async {
    if (owner.failDispose) throw StateError('fallback physical disposal failed');
    return DisposeResponse();
  }

  @override
  Future<SetVolumeResponse> setVolume(SetVolumeRequest r) async => SetVolumeResponse();
  @override
  Future<SetSpeedResponse> setSpeed(SetSpeedRequest r) async => SetSpeedResponse();
  @override
  Future<SetPitchResponse> setPitch(SetPitchRequest r) async => SetPitchResponse();
  @override
  Future<SetSkipSilenceResponse> setSkipSilence(SetSkipSilenceRequest r) async => SetSkipSilenceResponse();
  @override
  Future<SetLoopModeResponse> setLoopMode(SetLoopModeRequest r) async => SetLoopModeResponse();
  @override
  Future<SetShuffleModeResponse> setShuffleMode(SetShuffleModeRequest r) async => SetShuffleModeResponse();
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late _Platform platform;
  late AppOutputAdmission admission;
  AudioPlayer makePlayer() =>
      AudioPlayer(outputAdmission: admission, handleInterruptions: false, handleAudioSessionActivation: false);
  setUp(() {
    platform = _Platform();
    JustAudioPlatform.instance = platform;
    admission = AppOutputAdmission();
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(const MethodChannel('com.ryanheise.audio_session'), (_) async => null);
  });

  test('dictation reservation excludes future output before SDK initialization; no stop takeover', () async {
    final input = admission.tryReserveDictation()!;
    final player = makePlayer();
    expect(() => player.setUrl('file:///fictional.wav'), throwsStateError);
    expect(platform.initializations, 0);
    expect(platform.disposals, 0);
    expect(input.isCurrent, true);
    input.releaseAfterInputStopped();
    await player.setUrl('file:///fictional.wav');
    await player.dispose();
    expect(admission.hasOutput, false);
  });

  test('pending real SDK load and held physical disposal retain exact output permit', () async {
    platform.loadGate = Completer<void>();
    platform.disposeGate = Completer<void>();
    final player = makePlayer();
    final loading = player.setUrl('file:///fictional.wav');
    expect(admission.tryReserveDictation(), isNull);
    final closing = player.dispose();
    await Future<void>.delayed(Duration.zero);
    expect(platform.disposals, 0);
    expect(() => player.play(), throwsStateError);
    platform.loadGate!.complete();
    await loading;
    await Future<void>.delayed(Duration.zero);
    expect(platform.disposals, 1);
    expect(admission.tryReserveDictation(), isNull);
    platform.disposeGate!.complete();
    await closing;
    final input = admission.tryReserveDictation()!;
    input.releaseAfterInputStopped();
  });

  test('failed native and fallback disposal never becomes success through stock no-op retry', () async {
    final player = makePlayer();
    await player.setUrl('file:///fictional.wav');
    platform.failDispose = true;
    final first = player.dispose();
    await expectLater(first, throwsStateError);
    platform.failDispose = false;
    final retry = player.dispose();
    expect(identical(first, retry), true);
    await expectLater(retry, throwsStateError);
    expect(admission.tryReserveDictation(), isNull);
    expect(platform.disposals, 1);
  });

  test('held SDK playback settles before successful disposal and release', () async {
    final player = makePlayer();
    await player.setUrl('file:///fictional.wav');
    final playing = player.play();
    await Future<void>.delayed(Duration.zero);
    expect(platform.plays, 1);
    expect(admission.tryReserveDictation(), isNull);
    await player.dispose();
    await playing;
    expect(admission.hasOutput, false);
  });

  test('stale input release cannot clear newer input owner', () {
    final first = admission.tryReserveDictation()!;
    first.releaseAfterInputStopped();
    final second = admission.tryReserveDictation()!;
    first.releaseAfterInputStopped();
    expect(second.isCurrent, true);
    expect(() => admission.reserveOutput('late callback'), throwsStateError);
    second.releaseAfterInputStopped();
  });

  test('held SDK audio-session setup cannot publish playback after retirement or release', () async {
    final setup = Completer<void>();
    var setupCalls = 0;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(const MethodChannel('com.ryanheise.audio_session'), (call) async {
      if (call.method == 'setConfiguration') {
        setupCalls++;
        await setup.future;
      }
      return null;
    });
    final player = AudioPlayer(outputAdmission: admission, handleInterruptions: false);
    await player.setUrl('file:///fictional.wav');
    final play = player.play();
    await Future<void>.delayed(Duration.zero);
    expect(setupCalls, 1);
    final closing = player.dispose();
    await Future<void>.delayed(Duration.zero);
    expect(admission.tryReserveDictation(), isNull);
    expect(platform.disposals, 0);
    setup.complete();
    await closing;
    await play;
    expect(platform.plays, 0);
    expect(admission.hasOutput, false);
  });

  test('unsupported output completion or failed teardown retains admission hold', () {
    admission.reserveOutput('native TTS without physical receipt');
    expect(admission.tryReserveDictation(), isNull);
    expect(admission.dictationReserved, false);
  });

  test('actual TTS and emergency routes cannot perform native setup during dictation', () async {
    final lease = AppOutputAdmission.shared.tryReserveDictation()!;
    var nativeCalls = 0;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(const MethodChannel('flutter_tts'), (_) async {
      nativeCalls++;
      return 1;
    });
    try {
      await expectLater(ElevenLabsTts.speakOnDevice('fictional local text'), throwsStateError);
      await expectLater(EmergencyAudioPlayer.playConfirmation(audioUrl: 'file:///fictional.wav'), throwsStateError);
      expect(nativeCalls, 0);
      expect(platform.initializations, 0);
      expect(platform.disposals, 0);
    } finally {
      lease.releaseAfterInputStopped();
    }
  });

  test('Whisper start refuses before authentication, session or native effects', () async {
    final lease = AppOutputAdmission.shared.tryReserveDictation()!;
    var effects = 0;
    const channel = MethodChannel('fictional-whisper-output-admission');
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (_) async {
      effects++;
      return {'accepted': true};
    });
    final runtime = WhisperDeliveryRuntime.testing(
      transport: (method, endpoint, fields, authority) async {
        effects++;
        return {};
      },
      read: (_) async => null,
      write: (_, __) async {},
      authorityProvider: () {
        effects++;
        return null;
      },
      channel: channel,
    );
    try {
      await expectLater(runtime.startOutput(), throwsStateError);
      expect(effects, 0);
      expect(lease.isCurrent, true);
    } finally {
      lease.releaseAfterInputStopped();
    }
  });
}
