import 'dart:async';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/providers/voice_recorder_provider.dart';
import 'package:omi/services/services.dart';

class _FakeMicRecorder implements IMicRecorderService {
  final stopGate = Completer<void>();
  int stopCalls = 0;
  bool isRecording = false;

  @override
  Future<void> start({
    required Function(Uint8List bytes) onByteReceived,
    Function()? onRecording,
    Function()? onStop,
    Function()? onInitializing,
  }) async {
    isRecording = true;
    onRecording?.call();
  }

  @override
  Future<void> stop() async {
    stopCalls++;
    await stopGate.future;
    isRecording = false;
  }

  @override
  Future<void> stopForAccountTransition() => stop();

  @override
  void resumeAfterAccountTransition() {}
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late SharedPreferencesUtil preferences;

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    preferences.verifiedPersonaId = 'persona-a';
    preferences.acceptAiConsent(
      receiptId: 'aicr_receipt-a',
      uid: 'uid-a',
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-08-07T00:00:00Z',
    );
    preferences.markAiConsentServerVerified(
      uid: 'uid-a',
      receiptId: 'aicr_receipt-a',
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      profileBindingId: 'profile-binding-a',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );
  });

  test('authority loss stops microphone, lease, and waveform before showing consent review', () async {
    final mic = _FakeMicRecorder();
    final provider = VoiceRecorderProvider(
      microphone: mic,
      requestMicrophonePermission: () async {},
    );
    addTearDown(provider.dispose);

    await provider.startRecording();
    expect(mic.isRecording, isTrue);
    expect(provider.hasActiveConsentLease, isTrue);
    expect(provider.hasActiveWaveformTimer, isTrue);

    preferences.declineAiConsent();
    final processing = provider.processRecording();
    await Future<void>.delayed(Duration.zero);

    expect(mic.stopCalls, 1);
    expect(provider.consentReviewRequired, isFalse, reason: 'terminal UI must wait for physical microphone stop');

    mic.stopGate.complete();
    await processing;

    expect(mic.isRecording, isFalse);
    expect(provider.hasActiveConsentLease, isFalse);
    expect(provider.hasActiveWaveformTimer, isFalse);
    expect(provider.consentReviewRequired, isTrue);
    expect(provider.state, VoiceRecorderState.transcribeFailed);
  });

  test('closing while consent teardown waits does not restore the dismissed failure UI', () async {
    final mic = _FakeMicRecorder();
    final provider = VoiceRecorderProvider(
      microphone: mic,
      requestMicrophonePermission: () async {},
    );
    addTearDown(provider.dispose);

    await provider.startRecording();
    preferences.declineAiConsent();
    final processing = provider.processRecording();
    await Future<void>.delayed(Duration.zero);

    provider.close();
    expect(provider.state, VoiceRecorderState.idle);
    expect(provider.consentReviewRequired, isFalse);

    mic.stopGate.complete();
    await processing;

    expect(provider.state, VoiceRecorderState.idle);
    expect(provider.consentReviewRequired, isFalse);
  });

  test('authority is revalidated after microphone permission before recorder resources start', () async {
    final mic = _FakeMicRecorder();
    final permissionGate = Completer<void>();
    final provider = VoiceRecorderProvider(
      microphone: mic,
      requestMicrophonePermission: () => permissionGate.future,
    );
    addTearDown(provider.dispose);

    final starting = provider.startRecording();
    await Future<void>.delayed(Duration.zero);
    preferences.declineAiConsent();
    permissionGate.complete();
    await starting;

    expect(mic.isRecording, isFalse);
    expect(provider.hasActiveConsentLease, isFalse);
    expect(provider.hasActiveWaveformTimer, isFalse);
    expect(provider.consentReviewRequired, isTrue);
    expect(provider.state, VoiceRecorderState.transcribeFailed);
  });
}
