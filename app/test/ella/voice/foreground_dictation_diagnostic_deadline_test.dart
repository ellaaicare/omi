import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/voice/foreground_dictation_host.dart';
import 'package:omi/ella/voice/foreground_dictation_diagnostics.dart';
import 'package:omi/ella/voice/ephemeral_dictation_session.dart';
import 'foreground_dictation_host_test.dart' show Engine, Output;
import 'ephemeral_dictation_session_test.dart' show Rig;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('actual Finish deadline records first failure before Cancel', () async {
    final r = Rig(), e = Engine(), o = Output(), d = ForegroundDictationDiagnostics();
    final h = ForegroundDictationHost(
        diagnostic: d.begin(contextCurrent: () => true),
        registration:
            ForegroundDictationRegistration(isPolicyEnabled: () => true, reserveOutput: () => o, createEngine: () => e),
        readEditor: () => DictationEditorBinding(value: r.value, lease: r.lease, epoch: 3, revision: r.revision),
        hasExactAuthority: () => true,
        canStartFresh: () => true,
        applyDraft: (_) {},
        operationTimeout: const Duration(milliseconds: 30));
    await h.start();
    e.port.terminalGate = Completer<void>();
    await h.finish();
    final observed = d.snapshot;
    e.port.terminalGate!.complete();
    await h.retire();
    h.dispose();
    r.lease.close();
    expect(h.phase, ForegroundDictationPhase.closed);
    expect(observed['first_failure'], 'finishDeadline',
        reason: 'Host failed after actual held native drain, diagnosis must record the bounded Finish failure');
  });
  test('actual held listen Start deadline retains originating stage', () async {
    final r = Rig(), e = Engine(), o = Output(), d = ForegroundDictationDiagnostics();
    e.port.listenGate = Completer<void>();
    final h = ForegroundDictationHost(
        diagnostic: d.begin(contextCurrent: () => true),
        registration:
            ForegroundDictationRegistration(isPolicyEnabled: () => true, reserveOutput: () => o, createEngine: () => e),
        readEditor: () => DictationEditorBinding(value: r.value, lease: r.lease, epoch: 3, revision: r.revision),
        hasExactAuthority: () => true,
        canStartFresh: () => true,
        applyDraft: (_) {},
        operationTimeout: const Duration(milliseconds: 30));
    await h.start();
    final observed = d.snapshot;
    e.port.listenGate!.complete();
    await h.retire();
    h.dispose();
    r.lease.close();
    expect(observed['first_failure'], 'startDeadline');
    expect(observed['failure_stage'], 'listen',
        reason: 'Cleanup probe/drain must not replace the held listen stage that timed out');
  });
}
