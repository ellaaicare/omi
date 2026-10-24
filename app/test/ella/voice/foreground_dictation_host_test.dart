import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/voice/foreground_dictation_host.dart';
import 'package:omi/ella/voice/ephemeral_dictation_session.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';
import 'ephemeral_dictation_session_test.dart' show Rig;
import '../audio_ownership/standard_speech_held_listen_test.dart' show HeldSpeechPort, settleSpeech;

class Output implements DictationOutputLease {
  bool current = true;
  int releases = 0, preparations = 0;
  Completer<void>? preparationGate, releaseGate;
  Object? releaseError;
  @override
  bool get isCurrent => current;
  @override
  Future<void> prepareBeforePermissions() async {
    preparations++;
    if (preparationGate != null) await preparationGate!.future;
  }

  @override
  Future<void> releaseAfterInputStopped() async {
    releases++;
    if (releaseGate != null) await releaseGate!.future;
    if (releaseError != null) throw releaseError!;
    current = false;
  }
}

class Engine implements ForegroundDictationEngine {
  Engine({HeldSpeechPort? candidate}) : port = candidate ?? HeldSpeechPort();
  @override
  final HeldSpeechPort port;
  int permissions = 0, initializations = 0, capabilities = 0;
  Completer<bool>? capability;
  Completer<bool>? permission;
  @override
  Future<bool> probeLocalCapability() {
    capabilities++;
    return capability?.future ?? Future.value(true);
  }

  @override
  Future<bool> requestPermission() {
    permissions++;
    return permission?.future ?? Future.value(true);
  }

  @override
  Future<bool> initialize() async {
    initializations++;
    return true;
  }

  @override
  Future<void> listenOnDevice() => port.listen();
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  for (final policy in [false, true]) {
    test('missing registration or disabled policy cannot request permission $policy', () async {
      final r = Rig();
      final e = Engine();
      final o = Output();
      final host = ForegroundDictationHost(
        registration: policy
            ? null
            : ForegroundDictationRegistration(
                isPolicyEnabled: () => false,
                reserveOutput: () => o,
                createEngine: () => e,
              ),
        readEditor: () => DictationEditorBinding(value: r.value, lease: r.lease, epoch: 3, revision: r.revision),
        hasExactAuthority: () => true,
        canStartFresh: () => true,
        applyDraft: (_) {},
      );
      await host.start();
      expect(host.phase, ForegroundDictationPhase.unavailable);
      expect(e.permissions, 0);
      expect(o.releases, 0);
      expect(AudioCaptureOwnership.shared.isReserved, false);
      host.dispose();
      r.lease.close();
    });
  }
  test('actual shared foreign input refuses before output effects and never stops its owner', () async {
    final r = Rig();
    final e = Engine();
    final o = Output();
    final foreign = AudioCaptureOwnership.shared.reserve(source: 'fictional_other_call', hasAuthority: () => true)!;
    final host = ForegroundDictationHost(
        registration:
            ForegroundDictationRegistration(isPolicyEnabled: () => true, reserveOutput: () => o, createEngine: () => e),
        readEditor: () => DictationEditorBinding(value: r.value, lease: r.lease, epoch: 3, revision: 0),
        hasExactAuthority: () => true,
        canStartFresh: () => true,
        applyDraft: (_) {});
    await host.start();
    expect(host.phase, ForegroundDictationPhase.busy);
    expect(e.permissions, 0);
    expect(o.preparations, 0);
    expect(o.releases, 0);
    expect(foreign.isOwner, true);
    host.dispose();
    await foreign.stop();
    r.lease.close();
  });
  test('output busy refuses before permission or input reservation', () async {
    final r = Rig();
    final e = Engine();
    final host = ForegroundDictationHost(
      registration: ForegroundDictationRegistration(
        isPolicyEnabled: () => true,
        reserveOutput: () => null,
        createEngine: () => e,
      ),
      readEditor: () => DictationEditorBinding(value: r.value, lease: r.lease, epoch: 3, revision: 0),
      hasExactAuthority: () => true,
      canStartFresh: () => true,
      applyDraft: (_) {},
    );
    await host.start();
    expect(host.phase, ForegroundDictationPhase.busy);
    expect(e.permissions, 0);
    expect(AudioCaptureOwnership.shared.isReserved, false);
    host.dispose();
    r.lease.close();
  });
  test('held capability after Cancel cannot request OS permission or initialize recognition', () async {
    final r = Rig();
    final e = Engine()..capability = Completer<bool>();
    final o = Output();
    final host = ForegroundDictationHost(
        registration:
            ForegroundDictationRegistration(isPolicyEnabled: () => true, reserveOutput: () => o, createEngine: () => e),
        readEditor: () => DictationEditorBinding(value: r.value, lease: r.lease, epoch: 3, revision: 0),
        hasExactAuthority: () => true,
        canStartFresh: () => true,
        applyDraft: (_) {});
    final start = host.start();
    await settleSpeech();
    expect(e.capabilities, 1);
    final close = host.retire();
    expect(o.releases, 0);
    e.capability!.complete(true);
    await start;
    await close;
    expect(e.permissions, 0);
    expect(e.initializations, 0);
    expect(o.releases, 1);
    expect(AudioCaptureOwnership.shared.isReserved, false);
    host.dispose();
    r.lease.close();
  });
  test('native output preparation holds shared input before capability or OS permission', () async {
    final r = Rig();
    final e = Engine();
    final o = Output()..preparationGate = Completer<void>();
    final host = ForegroundDictationHost(
        registration:
            ForegroundDictationRegistration(isPolicyEnabled: () => true, reserveOutput: () => o, createEngine: () => e),
        readEditor: () => DictationEditorBinding(value: r.value, lease: r.lease, epoch: 3, revision: 0),
        hasExactAuthority: () => true,
        canStartFresh: () => true,
        applyDraft: (_) {});
    final start = host.start();
    await settleSpeech();
    expect(o.preparations, 1);
    expect(e.capabilities, 0);
    expect(e.permissions, 0);
    expect(AudioCaptureOwnership.shared.isReserved, true);
    final close = host.retire();
    o.preparationGate!.complete();
    await start;
    await close;
    expect(e.permissions, 0);
    expect(e.initializations, 0);
    expect(o.releases, 1);
    host.dispose();
    r.lease.close();
  });
  test('failed output release retains exact lease and Cancel retries without capture restart', () async {
    final r = Rig();
    final e = Engine();
    final o = Output()..releaseError = StateError('release unknown');
    final host = ForegroundDictationHost(
        registration:
            ForegroundDictationRegistration(isPolicyEnabled: () => true, reserveOutput: () => o, createEngine: () => e),
        readEditor: () => DictationEditorBinding(value: r.value, lease: r.lease, epoch: 3, revision: 0),
        hasExactAuthority: () => true,
        canStartFresh: () => true,
        applyDraft: (_) {});
    await host.start();
    await host.finish();
    expect(host.phase, ForegroundDictationPhase.failed);
    expect(host.hasHeldInput, false);
    expect(host.hasHeldOutput, true);
    expect(o.releases, 1);
    await host.start();
    expect(e.permissions, 1);
    expect(e.port.generation, 1);
    o.releaseError = null;
    await host.retire();
    expect(o.releases, 2);
    expect(host.hasHeldOutput, false);
    expect(e.port.drains, 1);
    host.dispose();
    r.lease.close();
  });
  for (final held in ['prepare', 'capability']) {
    test('late $held receipt after Start deadline cannot request OS permission', () async {
      final r = Rig();
      final e = Engine();
      final o = Output();
      if (held == 'prepare')
        o.preparationGate = Completer<void>();
      else
        e.capability = Completer<bool>();
      final host = ForegroundDictationHost(
          registration: ForegroundDictationRegistration(
              isPolicyEnabled: () => true, reserveOutput: () => o, createEngine: () => e),
          readEditor: () => DictationEditorBinding(value: r.value, lease: r.lease, epoch: 3, revision: 0),
          hasExactAuthority: () => true,
          canStartFresh: () => true,
          applyDraft: (_) {
            fail('late draft');
          },
          operationTimeout: const Duration(milliseconds: 30));
      await host.start();
      expect(host.phase, ForegroundDictationPhase.failed);
      expect(AudioCaptureOwnership.shared.isReserved, true);
      expect(o.releases, 0);
      if (held == 'prepare')
        o.preparationGate!.complete();
      else
        e.capability!.complete(true);
      await settleSpeech();
      expect(e.permissions, 0);
      expect(e.initializations, 0);
      expect(e.port.generation, 0);
      await host.retire();
      expect(o.releases, 1);
      expect(AudioCaptureOwnership.shared.isReserved, false);
      host.dispose();
      r.lease.close();
    });
  }
  for (final ending in ['empty', 'text', 'retired']) {
    testWidgets('actual three-second timer $ending final shares host Finish and holds output until terminal',
        (tester) async {
      final r = Rig();
      final e = Engine();
      final o = Output();
      var applies = 0;
      if (ending == 'empty') e.port.words = '';
      final host = ForegroundDictationHost(
          registration: ForegroundDictationRegistration(
              isPolicyEnabled: () => true, reserveOutput: () => o, createEngine: () => e),
          readEditor: () => DictationEditorBinding(value: r.value, lease: r.lease, epoch: 3, revision: 0),
          hasExactAuthority: () => r.consentEpoch == 5,
          canStartFresh: () => true,
          applyDraft: (_) {
            applies++;
          });
      await host.start();
      expect(host.phase, ForegroundDictationPhase.listening);
      e.port.terminalGate = Completer<void>();
      await tester.pump(const Duration(seconds: 3));
      await tester.pump();
      expect(host.phase, ForegroundDictationPhase.finishing);
      expect(e.port.drains, 1);
      expect(o.releases, 0);
      expect(AudioCaptureOwnership.shared.isReserved, true);
      Future<void>? retirement;
      if (ending == 'retired') {
        r.consentEpoch++;
        retirement = host.retire();
      }
      e.port.terminalGate!.complete();
      await tester.pumpAndSettle();
      if (retirement != null) await retirement;
      expect(host.phase, ForegroundDictationPhase.closed);
      expect(o.releases, 1);
      expect(e.port.drains, 1);
      expect(applies, ending == 'text' ? 1 : 0);
      expect(AudioCaptureOwnership.shared.isReserved, false);
      host.dispose();
      r.lease.close();
    });
  }
  testWidgets('throwing automatic finish observer cannot strand original shared cleanup', (tester) async {
    final r = Rig();
    var observations = 0, applies = 0;
    final session = EphemeralDictationSession(
        port: r.port,
        draft: r.session.draft,
        draftRevision: 0,
        readEditor: () => DictationEditorBinding(value: r.value, lease: r.lease, epoch: 3, revision: 0),
        hasExactCaptureAuthority: () => true,
        canStartFresh: () => true,
        requestPermission: () async => true,
        initializeRecognition: () async => true,
        listen: r.port.listen,
        applyDraft: (_) {
          applies++;
        },
        ownership: r.registry,
        onAutomaticFinish: () {
          observations++;
          throw StateError('fictional observer');
        });
    await session.start();
    await tester.pump(const Duration(seconds: 3));
    await tester.pumpAndSettle();
    expect(observations, 1);
    expect(applies, 1);
    expect(r.port.drains, 1);
    expect(r.registry.isReserved, false);
    await session.retire();
    r.lease.close();
  });
  test('global input retirement while capability pending prevents later OS permission', () async {
    final r = Rig();
    final e = Engine()..capability = Completer<bool>();
    final o = Output();
    final host = ForegroundDictationHost(
        registration:
            ForegroundDictationRegistration(isPolicyEnabled: () => true, reserveOutput: () => o, createEngine: () => e),
        readEditor: () => DictationEditorBinding(value: r.value, lease: r.lease, epoch: 3, revision: 0),
        hasExactAuthority: () => true,
        canStartFresh: () => true,
        applyDraft: (_) {});
    final start = host.start();
    await settleSpeech();
    final retire = AudioCaptureOwnership.shared.quiesceForAccountTransition();
    e.capability!.complete(true);
    await start;
    await retire;
    await host.retire();
    expect(e.permissions, 0);
    expect(e.initializations, 0);
    expect(o.releases, 1);
    expect(e.port.generation, 0);
    host.dispose();
    r.lease.close();
  });
  test('held final terminal pins output and input; only callback applies draft once', () async {
    final r = Rig();
    final e = Engine();
    final o = Output();
    int applies = 0;
    final host = ForegroundDictationHost(
      registration: ForegroundDictationRegistration(
        isPolicyEnabled: () => true,
        reserveOutput: () => o,
        createEngine: () => e,
      ),
      readEditor: () => DictationEditorBinding(value: r.value, lease: r.lease, epoch: 3, revision: 0),
      hasExactAuthority: () => true,
      canStartFresh: () => true,
      applyDraft: (value) {
        applies++;
        r.value = value;
      },
    );
    await host.start();
    expect(host.phase, ForegroundDictationPhase.listening);
    e.port.terminalGate = Completer<void>();
    final finish = host.finish();
    await settleSpeech();
    expect(AudioCaptureOwnership.shared.isReserved, true);
    expect(o.releases, 0);
    expect(applies, 0);
    e.port.terminalGate!.complete();
    await finish;
    expect(o.releases, 1);
    expect(applies, 1);
    expect(AudioCaptureOwnership.shared.isReserved, false);
    await host.finish();
    expect(applies, 1);
    host.dispose();
    r.lease.close();
  });
  test('cancel during permission blocks init; output releases only after exact cleanup', () async {
    final r = Rig();
    final e = Engine()..permission = Completer<bool>();
    final o = Output();
    final host = ForegroundDictationHost(
      registration: ForegroundDictationRegistration(
        isPolicyEnabled: () => true,
        reserveOutput: () => o,
        createEngine: () => e,
      ),
      readEditor: () => DictationEditorBinding(value: r.value, lease: r.lease, epoch: 3, revision: 0),
      hasExactAuthority: () => true,
      canStartFresh: () => true,
      applyDraft: (_) {},
    );
    final start = host.start();
    await settleSpeech();
    final cancel = host.retire();
    expect(o.releases, 0);
    e.permission!.complete(true);
    await start;
    await cancel;
    expect(e.initializations, 0);
    expect(o.releases, 1);
    expect(AudioCaptureOwnership.shared.isReserved, false);
    host.dispose();
    r.lease.close();
  });
}
