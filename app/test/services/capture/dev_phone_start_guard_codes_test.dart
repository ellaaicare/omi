import 'package:flutter_test/flutter_test.dart';
import 'package:omi/services/capture/capture_coordinator.dart';

// Ella Base v4.9 (DEV-ONLY): the two existing dev-start guards in _reducePhoneStart return closed codes instead of
// a bare false (v4.8 run 08:29:16Z logged only "StateError"). Same conditions; ordinary starts are untouched.

CaptureEnvironment env(CaptureCoordinatorState state, {bool batch = false, bool network = true, bool call = false}) =>
    CaptureEnvironment(
      livenessEpoch: 0,
      appSuspended: false,
      policyMuted: false,
      paused: false,
      batchModeEnabled: batch,
      batchModeSuspendedForOnboarding: false,
      deviceSupportsTranscribeLater: true,
      networkConnected: network,
      signedIn: () => true,
      phoneMicSupportsBatch: true,
      transcriptReady: false,
      socketConnected: false,
      deviceServiceReady: state.connectedDevice != null,
      callActive: call,
      deviceRecording: false,
      micCapturing: false,
      systemSurfaceRecordingId: null,
      systemSurfaceConversationRevision: 0,
    );

void main() {
  final idle = CaptureCoordinatorState.idle();
  const heldByCall =
      SuspendedCapture(source: CaptureSource.pendant, reason: SuspendReason.call, wasPaused: true, mode: CaptureTransport.live);
  const dev = PhoneStartRequested(devToken: 7);

  Object? devResult(CaptureCoordinatorState s, CaptureEnvironment e) => transitionCapture(s, dev, e).result;

  test('phase guard: not idle', () {
    final s = idle.copyWith(phase: CapturePhase.pendantPaused);
    expect(devResult(s, env(s)), 'dev_refused_phase_pendantPaused');
    expect(transitionCapture(s, dev, env(s)).effects, isEmpty);
  });
  test('suspension guard', () {
    final s = idle.copyWith(suspended: [heldByCall]);
    expect(devResult(s, env(s)), 'dev_refused_suspended');
  });
  test('call guard', () {
    final s = idle.copyWith(callActive: true);
    expect(devResult(s, env(s, call: true)), 'dev_refused_call');
  });
  test('transport guard: Transcribe Later and offline fallback', () {
    expect(devResult(idle, env(idle, batch: true)), 'dev_refused_transport_batch');
    expect(devResult(idle, env(idle, network: false)), 'dev_refused_transport_batch');
    expect(transitionCapture(idle, dev, env(idle, network: false)).effects, isEmpty);
  });
  test('idle + live: admitted (no refusal code, start effects emitted)', () {
    final t = transitionCapture(idle, dev, env(idle));
    expect(t.result, isNot(isA<String>()));
    expect(t.result, isNot(false));
    expect(t.effects, isNotEmpty);
  });
  test('ordinary starts are unchanged by the codes', () {
    final paused = idle.copyWith(phase: CapturePhase.pendantPaused);
    expect(transitionCapture(paused, const PhoneStartRequested(), env(paused)).result, isNot(isA<String>()));
    expect(transitionCapture(idle, const PhoneStartRequested(), env(idle, network: false)).result, isNot(isA<String>()));
  });
}
