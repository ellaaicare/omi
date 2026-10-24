import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/voice/foreground_dictation_host.dart';
import 'package:omi/ella/voice/foreground_dictation_diagnostics.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  for (final reserved in [false, true]) {
    test('actual host retains admission barrier before editor/output/SDK: registry $reserved', () async {
      final lease =
          reserved ? AudioCaptureOwnership.shared.reserve(source: 'v2v-call', hasAuthority: () => true) : null;
      final owner = ForegroundDictationDiagnostics();
      final d = owner.begin(contextCurrent: () => true);
      var pageCalls = 0;
      var outputCalls = 0;
      var editorCalls = 0;
      final host = ForegroundDictationHost(
        registration: ForegroundDictationRegistration(
          isPolicyEnabled: () => true,
          reserveOutput: () {
            outputCalls++;
            return null;
          },
          createEngine: () => throw StateError('must not reach SDK'),
        ),
        diagnostic: d,
        hasExactAuthority: () => true,
        canStartFresh: () {
          pageCalls++;
          d.captureAdmission(capturableContent: true);
          return false;
        },
        readEditor: () {
          editorCalls++;
          throw StateError('must not read draft');
        },
        applyDraft: (_) {},
      );
      await host.start();
      expect(host.phase, ForegroundDictationPhase.busy);
      expect(owner.snapshot['first_failure'], 'captureBusy');
      expect((owner.snapshot['capture_admission'] as Map)['registry_reserved'], reserved);
      expect((owner.snapshot['capture_admission'] as Map)['registry_source'], reserved ? 'v2v-call' : null);
      expect(pageCalls, reserved ? 0 : 1);
      expect(outputCalls, 0);
      expect(editorCalls, 0);
      if (lease != null) {
        expect(AudioCaptureOwnership.shared.isReserved, true);
        await lease.stop();
      }
      await host.retire();
      host.dispose();
    });
  }
  test('throwing freshness remains busy with closed callback attribution', () async {
    final owner = ForegroundDictationDiagnostics();
    final d = owner.begin(contextCurrent: () => true);
    final host = ForegroundDictationHost(
      registration: ForegroundDictationRegistration(
        isPolicyEnabled: () => true,
        reserveOutput: () => throw StateError('must not reserve'),
        createEngine: () => throw StateError('must not initialize'),
      ),
      diagnostic: d,
      hasExactAuthority: () => true,
      canStartFresh: () => throw StateError('private-identity-do-not-log'),
      readEditor: () => throw StateError('must not read'),
      applyDraft: (_) {},
    );
    await host.start();
    expect(host.phase, ForegroundDictationPhase.busy);
    final a = owner.snapshot['capture_admission'] as Map;
    expect(a['observation_failed'], true);
    expect(a['observation_stage'], 'fresh_callback');
    expect(owner.snapshot.toString().contains('private-identity'), false);
    await host.retire();
    host.dispose();
  });
  test('pure registry state distinguishes retained stop and successor without invoking authority', () async {
    final registry = AudioCaptureOwnership();
    var checks = 0;
    final owner = registry.reserve(
      source: 'phone-capture',
      hasAuthority: () {
        checks++;
        return true;
      },
    )!;
    final successor = owner.reserveSuccessor(source: 'chat-dictation', hasAuthority: () => true)!;
    final before = checks;
    var failed = true;
    owner.addStopAndConfirm(() async {
      if (failed) throw StateError('unconfirmed');
    });
    expect(registry.dictationAdmissionStates['successor_present'], true);
    expect(checks, before);
    await expectLater(owner.stop(), throwsStateError);
    expect(registry.dictationAdmissionStates['owner_invalidated'], true);
    expect(registry.dictationAdmissionStates['owner_confirmation_outstanding'], true);
    expect(registry.isReserved, true);
    failed = false;
    await owner.stop();
    await successor.stop();
    expect(registry.isReserved, false);
  });
}
