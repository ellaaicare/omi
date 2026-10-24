import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/voice/foreground_dictation_diagnostics.dart';

void main() {
  test('closed capture evidence records every pending guard without arbitrary keys or source', () {
    final owner = ForegroundDictationDiagnostics();
    final d = owner.begin(contextCurrent: () => true);
    d.captureAdmission(
      registryReserved: true,
      registrySource: 'private-session-id',
      captureQuiescent: false,
      capturableContent: true,
      pendingStates: {'device_start_pending': true, 'private-uid': true},
    );
    final a = owner.snapshot['capture_admission'] as Map;
    expect(a['registry_source'], 'other');
    d.captureAdmission(registryReserved: false);
    expect((owner.snapshot['capture_admission'] as Map)['registry_source'], null);
    expect(a['pending_states'], {'device_start_pending': true});
    expect(a['capturable_content'], true);
  });
  test('page evidence and registry evidence merge; immutable snapshots do not change', () {
    final owner = ForegroundDictationDiagnostics();
    final d = owner.begin(contextCurrent: () => true);
    d.captureAdmission(registryReserved: false);
    final old = owner.snapshot;
    d.captureAdmission(pageCurrent: true, captureQuiescent: true, observationFailed: false);
    expect((old['capture_admission'] as Map).containsKey('page_current'), false);
    expect((owner.snapshot['capture_admission'] as Map)['registry_reserved'], false);
    expect(() => (owner.snapshot['capture_admission'] as Map)['x'] = true, throwsUnsupportedError);
  });
  test('account retirement and stale attempts cannot replace current evidence', () {
    final owner = ForegroundDictationDiagnostics();
    var current = true;
    final old = owner.begin(contextCurrent: () => current);
    old.captureAdmission(registryReserved: true);
    current = false;
    expect(owner.snapshot['disposition'], 'context_retired');
    final fresh = owner.begin(contextCurrent: () => true);
    fresh.captureAdmission(registryReserved: false);
    old.captureAdmission(registryReserved: true);
    expect((owner.snapshot['capture_admission'] as Map)['registry_reserved'], false);
  });
  test('first failure remains admission captureBusy after retirement', () {
    final owner = ForegroundDictationDiagnostics();
    final d = owner.begin(contextCurrent: () => true);
    d.captureAdmission(capturableContent: true);
    d.fail(DictationDiagnosticFailure.captureBusy);
    d.stage(DictationDiagnosticStage.retirement);
    d.fail(DictationDiagnosticFailure.otherFailure);
    expect(owner.snapshot['first_failure'], 'captureBusy');
    expect(owner.snapshot['failure_stage'], 'admission');
  });
  test('phone pending leaves and registry states stay separate and closed', () {
    final owner = ForegroundDictationDiagnostics();
    final d = owner.begin(contextCurrent: () => true);
    d.captureAdmission(
      pendingStates: {'phone_audio_owned': true, 'phone_mic_start_pending': true, 'phone_release_pending': false},
      registryStates: {'owner_present': true, 'owner_stop_pending': false, 'private': true},
      observationStage: 'private-message',
    );
    final a = owner.snapshot['capture_admission'] as Map;
    expect(a['pending_states'], {
      'phone_audio_owned': true,
      'phone_mic_start_pending': true,
      'phone_release_pending': false,
    });
    expect(a['registry_states'], {'owner_present': true, 'owner_stop_pending': false});
    expect(a['observation_stage'], 'other');
  });
}
