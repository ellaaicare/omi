import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter/widgets.dart';
import 'package:video_player_platform_interface/video_player_platform_interface.dart';
import 'package:omi/ella/services/admitted_video_player.dart';
import 'package:omi/ella/services/app_output_admission.dart';

class _Platform extends VideoPlayerPlatform {
  final events = StreamController<VideoEvent>();
  final createGate = Completer<int>();
  final disposeGate = Completer<void>();
  final disposeEntered = Completer<void>();
  int creations = 0, plays = 0, disposals = 0;
  @override
  Future<void> init() async {}
  @override
  Future<int?> create(DataSource dataSource) async {
    creations++;
    return createGate.future;
  }

  @override
  Stream<VideoEvent> videoEventsFor(int id) => events.stream;
  @override
  Future<void> dispose(int id) async {
    disposals++;
    disposeEntered.complete();
    await disposeGate.future;
  }

  @override
  Future<void> setLooping(int id, bool looping) async {}
  @override
  Future<void> setVolume(int id, double volume) async {}
  @override
  Future<void> setPlaybackSpeed(int id, double speed) async {}
  @override
  Future<void> pause(int id) async {}
  @override
  Future<void> play(int id) async {
    plays++;
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late _Platform platform;
  late AppOutputAdmission registry;
  setUp(() {
    platform = _Platform();
    VideoPlayerPlatform.instance = platform;
    registry = AppOutputAdmission();
  });
  Future<void> initialize(VideoPlayerController player) async {
    final starting = player.initialize();
    platform.createGate.complete(1);
    await Future<void>.delayed(Duration.zero);
    platform.events.add(VideoEvent(
        eventType: VideoEventType.initialized, duration: const Duration(seconds: 1), size: const Size(10, 10)));
    await starting;
  }

  test('dictation excludes actual video initialize before native creation', () async {
    final lease = registry.tryReserveDictation()!;
    final player = VideoPlayerController.asset('fictional.mp4', outputAdmission: registry);
    expect(() => player.initialize(), throwsStateError);
    expect(platform.creations, 0);
    await lease.releaseAfterInputStopped();
    await player.dispose();
  });
  test('pending native creation and disposal hold output, no late playback after retirement', () async {
    final player = VideoPlayerController.asset('fictional.mp4', outputAdmission: registry);
    final starting = player.initialize();
    final closing = player.dispose();
    expect(registry.tryReserveDictation(), isNull);
    expect(() => player.play(), throwsStateError);
    platform.createGate.complete(1);
    await Future<void>.delayed(Duration.zero);
    platform.events.add(VideoEvent(
        eventType: VideoEventType.initialized, duration: const Duration(seconds: 1), size: const Size(10, 10)));
    await starting;
    await Future<void>.delayed(Duration.zero);
    expect(registry.tryReserveDictation(), isNull);
    expect(platform.disposals, 1);
    platform.disposeGate.complete();
    await closing;
    expect(platform.plays, 0);
    expect(registry.hasOutput, false);
  });
  test('failed native video disposal retains original failure and cannot release through no-op retry', () async {
    final player = VideoPlayerController.asset('fictional.mp4', outputAdmission: registry);
    await initialize(player);
    final closing = player.dispose();
    final check = expectLater(closing, throwsStateError);
    await platform.disposeEntered.future;
    platform.disposeGate.completeError(StateError('native disposal unconfirmed'));
    await check;
    expect(identical(closing, player.dispose()), true);
    await expectLater(player.dispose(), throwsStateError);
    expect(platform.disposals, 1);
    expect(registry.tryReserveDictation(), isNull);
  });
}
