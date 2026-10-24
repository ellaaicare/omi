import 'dart:async';
import 'dart:typed_data';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/providers/voice_recorder_provider.dart';
import 'package:omi/providers/speech_profile_provider.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';
import 'package:omi/services/services.dart';

class _HeldMic implements IMicRecorderService {
  int starts = 0;
  int stops = 0;
  Completer<void>? startGate;
  Completer<void>? stopGate;
  bool failStart = false;
  bool failStop = false;
  Function()? recorded;
  Function(Uint8List)? bytesReceived;
  Uint8List? tailOnStop;
  @override
  Future<void> start(
      {required Function(Uint8List) onByteReceived,
      Function()? onRecording,
      Function()? onStop,
      Function()? onInitializing}) async {
    starts++;
    recorded = onRecording;
    bytesReceived = onByteReceived;
    await startGate?.future;
    if (failStart) throw StateError('start receipt missing');
    onRecording?.call();
  }

  @override
  Future<void> stop() async {
    stops++;
    if (tailOnStop != null) bytesReceived?.call(tailOnStop!);
    await stopGate?.future;
    if (failStop) throw StateError('stop receipt missing');
  }

  @override
  Future<void> stopForAccountTransition() => stop();
  @override
  void resumeAfterAccountTransition() {}
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    final prefs = SharedPreferencesUtil();
    prefs.uid = 'uid-a';
    prefs.verifiedPersonaId = 'persona-a';
    prefs.acceptAiConsent(
        receiptId: 'aicr_receipt-a',
        uid: 'uid-a',
        profileBindingId: 'profile-binding-a',
        serverDecidedAt: '2026-08-07T00:00:00Z');
    prefs.markAiConsentServerVerified(
        uid: 'uid-a',
        receiptId: 'aicr_receipt-a',
        policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
        processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
        profileBindingId: 'profile-binding-a',
        scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
        scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash);
  });
  tearDown(() async {
    await AudioCaptureOwnership.shared.quiesceForAccountTransition();
  });

  test('same-account dictation retains owned encoder tail while stop acknowledgement is held', () async {
    final mic = _HeldMic()
      ..stopGate = Completer<void>()
      ..tailOnStop = Uint8List(4);
    final provider = VoiceRecorderProvider(microphone: mic, requestMicrophonePermission: () async {});
    await provider.startRecording();
    mic.bytesReceived!(Uint8List(2));
    final stopping = provider.stopRecording();
    await Future<void>.delayed(Duration.zero);
    expect(provider.bufferedAudioBytesForTesting, 6, reason: 'local owned tail survives; no raw bytes in diagnostics');
    expect(AudioCaptureOwnership.shared.isReserved, isTrue);
    mic.stopGate!.complete();
    await stopping;
    mic.bytesReceived!(Uint8List(5));
    expect(provider.bufferedAudioBytesForTesting, 6, reason: 'late unowned tail cannot enter a stopped draft');
    provider.close();
    provider.dispose();
  });
  test('account-invalidated dictation suppresses post-revocation tail without touching another owner', () async {
    final mic = _HeldMic()
      ..stopGate = Completer<void>()
      ..tailOnStop = Uint8List(4);
    final provider = VoiceRecorderProvider(microphone: mic, requestMicrophonePermission: () async {});
    await provider.startRecording();
    mic.bytesReceived!(Uint8List(2));
    final transition = AudioCaptureOwnership.shared.quiesceForAccountTransition();
    await Future<void>.delayed(Duration.zero);
    expect(provider.bufferedAudioBytesForTesting, 2);
    mic.stopGate!.complete();
    await transition;
    provider.close();
    provider.dispose();
  });

  test('speech profile contention denies before socket and permission startup', () async {
    final owner = AudioCaptureOwnership.shared.reserve(source: 'phone-capture', hasAuthority: () => true)!;
    final mic = _HeldMic();
    var permissionRequests = 0;
    final profile = SpeechProfileProvider(
        phoneMicRecorder: mic,
        phoneMicPermissionChecker: () async {
          permissionRequests++;
          return true;
        });
    expect(await profile.initialise(usePhoneMic: true), isFalse);
    expect(mic.starts, 0);
    expect(permissionRequests, 0);
    expect(owner.isCurrent, isTrue);
    profile.dispose();
    await owner.stop();
  });

  test('actual provider reserves before held permission and denies second provider without permission', () async {
    final permission = Completer<void>();
    final mic = _HeldMic();
    final first = VoiceRecorderProvider(microphone: mic, requestMicrophonePermission: () => permission.future);
    var otherPermissionRequests = 0;
    final second = VoiceRecorderProvider(
        microphone: mic,
        requestMicrophonePermission: () async {
          otherPermissionRequests++;
        });
    final starting = first.startRecording();
    await second.startRecording();
    expect(otherPermissionRequests, 0);
    expect(mic.starts, 0);
    first.close();
    await Future<void>.delayed(Duration.zero);
    expect(mic.stops, 0);
    permission.complete();
    await starting;
    expect(mic.starts, 0);
    first.dispose();
    second.dispose();
  });
  test('late old permission completion cannot stop a replacement session in the same provider', () async {
    final oldPermission = Completer<void>();
    var permissions = 0;
    final mic = _HeldMic();
    final provider = VoiceRecorderProvider(
        microphone: mic,
        requestMicrophonePermission: () {
          return ++permissions == 1 ? oldPermission.future : Future<void>.value();
        });
    final oldStart = provider.startRecording();
    provider.close();
    await Future<void>.delayed(Duration.zero);
    await provider.startRecording();
    expect(mic.starts, 1);
    oldPermission.complete();
    await oldStart;
    expect(mic.stops, 0);
    expect(provider.state, VoiceRecorderState.recording);
    await provider.stopRecording();
    provider.close();
    provider.dispose();
  });
  test('held native start close retains owner through held physical stop and gates late recording callback', () async {
    final mic = _HeldMic()
      ..startGate = Completer<void>()
      ..stopGate = Completer<void>();
    final provider = VoiceRecorderProvider(microphone: mic, requestMicrophonePermission: () async {});
    final starting = provider.startRecording();
    await Future<void>.delayed(Duration.zero);
    expect(mic.starts, 1);
    provider.close();
    expect(AudioCaptureOwnership.shared.isReserved, isTrue);
    mic.startGate!.complete();
    await starting;
    await Future<void>.delayed(Duration.zero);
    mic.recorded!();
    expect(provider.state, VoiceRecorderState.idle);
    expect(mic.stops, 1);
    expect(AudioCaptureOwnership.shared.isReserved, isTrue);
    mic.stopGate!.complete();
    await Future<void>.delayed(Duration.zero);
    expect(AudioCaptureOwnership.shared.isReserved, isFalse);
    provider.dispose();
  });
  test('account transition during permission suppresses native start and does not stop another owner', () async {
    final permission = Completer<void>();
    final mic = _HeldMic();
    final provider = VoiceRecorderProvider(microphone: mic, requestMicrophonePermission: () => permission.future);
    final starting = provider.startRecording();
    await AudioCaptureOwnership.shared.quiesceForAccountTransition();
    final replacement = AudioCaptureOwnership.shared.reserve(source: 'account-b-call', hasAuthority: () => true)!;
    SharedPreferencesUtil().uid = 'uid-b';
    permission.complete();
    await starting;
    expect(mic.starts, 0);
    expect(mic.stops, 0);
    expect(replacement.isCurrent, isTrue);
    await replacement.stop();
    provider.close();
    provider.dispose();
  });
  test('idle provider stop/dispose never reaches another owner native microphone', () async {
    final mic = _HeldMic();
    final provider = VoiceRecorderProvider(microphone: mic, requestMicrophonePermission: () async {});
    final other = AudioCaptureOwnership.shared.reserve(source: 'phone', hasAuthority: () => true)!;
    await provider.stopRecording();
    provider.dispose();
    expect(mic.stops, 0);
    expect(other.isCurrent, isTrue);
    await other.stop();
  });
  test('ambiguous native start and failed cleanup hold reservation until explicit retry confirms stop', () async {
    final mic = _HeldMic()
      ..failStart = true
      ..failStop = true;
    final provider = VoiceRecorderProvider(microphone: mic, requestMicrophonePermission: () async {});
    await expectLater(provider.startRecording(), throwsStateError);
    expect(AudioCaptureOwnership.shared.isReserved, isTrue);
    expect(AudioCaptureOwnership.shared.reserve(source: 'phone', hasAuthority: () => true), isNull);
    mic.failStop = false;
    await provider.stopRecording();
    expect(AudioCaptureOwnership.shared.isReserved, isFalse);
    provider.close();
    provider.dispose();
  });
}
