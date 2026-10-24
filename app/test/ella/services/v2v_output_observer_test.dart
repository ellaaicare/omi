import 'dart:async';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:record/record.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/v2v_client.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';

class _Recorder implements V2VMicrophoneRecorder {
  final frames = StreamController<Uint8List>.broadcast();
  bool recording = false;
  int stops = 0;
  @override
  Future<void> useEllaManagedAudioSession() async {}
  @override
  Future<bool> hasPermission() async => true;
  @override
  Future<Stream<Uint8List>> startStream(RecordConfig config) async {
    recording = true;
    return frames.stream;
  }

  @override
  Future<String?> stop() async {
    stops++;
    recording = false;
    if (!frames.isClosed) await frames.close();
    return null;
  }

  @override
  Future<bool> isRecording() async => recording;
  @override
  Future<void> dispose() async {}
}

class _Output implements V2VOutputPlayer {
  final entered = Completer<void>();
  final played = Completer<void>();
  bool failStop = true;
  @override
  bool get reusableAfterStop => true;
  @override
  Future<void> setVolume(double volume) async {}
  @override
  Future<void> setFilePath(String path) async {}
  @override
  Future<void> play() async {
    entered.complete();
    await played.future;
  }

  @override
  Future<bool> stopAndConfirm() async {
    if (failStop) throw StateError('original physical output stop failed');
    if (!played.isCompleted) played.complete();
    return true;
  }

  @override
  Future<bool> disposeAndConfirm() async => true;
}

class _Harness {
  _Harness({required Future<bool> Function() release, _Output? output}) {
    client = V2VClient(
      onEvent: (event) {
        if (event.type != 'error') return;
        observerErrors++;
        throw StateError('synthetic observer failed');
      },
      microphoneRecorderFactory: () {
        final recorder = _Recorder();
        recorders.add(recorder);
        return recorder;
      },
      audibleOutputEnforcer: () async => true,
      playbackOutputEnforcer: () async => true,
      audioSessionReleaseReceipt: release,
      outputPlayerFactory: output == null ? null : () => output,
      liveChannelForTesting: () => true,
      playbackMicCooldown: Duration.zero,
    );
  }
  late final V2VClient client;
  final recorders = <_Recorder>[];
  int observerErrors = 0;
  Future<void> start() async {
    final authority = AiConsentAuthoritySnapshot.capture(preferences: SharedPreferencesUtil(), expectedUid: 'uid-a')!;
    expect(await client.startAuthorizedMicrophoneForTesting(authority: authority, shouldContinue: () => true), isTrue);
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    final preferences = SharedPreferencesUtil()..uid = 'uid-a';
    preferences.verifiedPersonaId = 'persona-a';
    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      uid: 'uid-a',
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:00:00Z',
    );
    preferences.markAiConsentServerVerified(
      uid: 'uid-a',
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      profileBindingId: 'profile-binding-a',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );
  });

  final originalRouteFailure = isA<StateError>()
      .having((error) => error.message, 'original physical receipt', 'V2V audio route release not confirmed');

  test('independent observer exception must not strand exact teardown receipt', () async {
    var routeConfirmed = false;
    var releases = 0;
    final h = _Harness(release: () async {
      releases++;
      return routeConfirmed;
    });
    await h.start();
    await expectLater(
      AudioCaptureOwnership.shared.quiesceForAccountTransition().timeout(const Duration(seconds: 1)),
      throwsA(originalRouteFailure),
    );
    expect(h.observerErrors, 1);
    expect(h.recorders.single.stops, 1);
    expect(h.recorders.single.recording, isFalse);
    expect(AudioCaptureOwnership.shared.reserve(source: 'replacement', hasAuthority: () => true), isNull);
    routeConfirmed = true;
    await AudioCaptureOwnership.shared.quiesceForAccountTransition();
    expect(releases, 2);
    expect(h.recorders.single.stops, 1);
    expect(AudioCaptureOwnership.shared.isReserved, isFalse);
    await h.client.disconnect();
  });

  for (final viaRegistry in [false, true]) {
    for (final confirm in [false, true]) {
      test('throwing observer preserves held route receipt registry=$viaRegistry confirmed=$confirm', () async {
        final entered = Completer<void>();
        final held = Completer<bool>();
        var retry = false, releases = 0;
        final h = _Harness(release: () {
          releases++;
          if (releases > 1) return Future.value(retry);
          entered.complete();
          return held.future;
        });
        await h.start();
        final stopping =
            viaRegistry ? AudioCaptureOwnership.shared.quiesceForAccountTransition() : h.client.disconnect();
        await entered.future;
        expect(h.observerErrors, 0);
        expect(h.recorders.single.recording, isFalse);
        expect(AudioCaptureOwnership.shared.reserve(source: 'replacement', hasAuthority: () => true), isNull);
        final result =
            confirm ? expectLater(stopping, completes) : expectLater(stopping, throwsA(originalRouteFailure));
        held.complete(confirm);
        await result.timeout(const Duration(seconds: 1));
        expect(AudioCaptureOwnership.shared.isReserved, !confirm);
        expect(h.observerErrors, confirm ? 0 : 1);
        if (!confirm) {
          retry = true;
          if (viaRegistry) {
            await AudioCaptureOwnership.shared.quiesceForAccountTransition();
          } else {
            await h.client.disconnect();
          }
        }
        await h.client.disconnect();
        expect(AudioCaptureOwnership.shared.isReserved, isFalse);
        expect(h.recorders, hasLength(1));
        expect(h.recorders.single.stops, 1);
      });
    }
  }

  test('throwing observer preserves interruption failure and mic latch until explicit physical retry', () async {
    final output = _Output();
    final h = _Harness(release: () async => true, output: output);
    await h.start();
    h.client.markConnectedForTesting();
    h.client.bufferAudioChunkForTesting(Uint8List.fromList([1, 2]));
    final finish = h.client.finishPlaybackForTesting();
    await output.entered.future;
    await expectLater(
        h.client.interruptPlayback(),
        throwsA(isA<StateError>()
            .having((error) => error.message, 'original physical receipt', 'original physical output stop failed')));
    expect(h.observerErrors, 1);
    expect(h.client.micMutedForTesting, isTrue);
    expect(h.client.micSuspendedForPlaybackForTesting, isTrue);
    expect(h.recorders, hasLength(1));
    expect(AudioCaptureOwnership.shared.reserve(source: 'replacement', hasAuthority: () => true), isNull);
    output.failStop = false;
    await h.client.interruptPlayback();
    await finish;
    expect(h.recorders, hasLength(2));
    await h.client.disconnect();
    expect(AudioCaptureOwnership.shared.isReserved, isFalse);
  });
}
