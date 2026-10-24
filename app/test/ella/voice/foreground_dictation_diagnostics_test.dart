import 'dart:async';
import 'dart:convert';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/voice/foreground_dictation_diagnostics.dart';
import 'package:omi/ella/voice/foreground_dictation_host.dart';
import 'package:omi/ella/voice/ephemeral_dictation_session.dart';
import 'package:speech_to_text/ella_speech_ownership.dart';
import 'foreground_dictation_host_test.dart' show Engine, Output;
import 'ephemeral_dictation_session_test.dart' show Rig;
import '../audio_ownership/standard_speech_held_listen_test.dart' show HeldSpeechPort;

class BrokenProbe extends HeldSpeechPort {
  BrokenProbe(this.failure);
  final Object failure;
  @override
  Future<EllaSpeechCaptureProof> probe() async => throw failure;
}

class BrokenOutput extends Output {
  BrokenOutput(this.failure);
  final Object failure;
  @override
  Future<void> prepareBeforePermissions() async => throw failure;
}

class InitFalse extends Engine {
  @override
  Future<bool> initialize() async => false;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('first failure survives cleanup and carries no arbitrary content', () {
    final store = ForegroundDictationDiagnostics();
    final a = store.begin(contextCurrent: () => true, verificationRemainingMs: -99);
    a.stage(DictationDiagnosticStage.outputPreparation);
    a.error(PlatformException(code: 'secret-uid', message: 'PRIVATE DRAFT', details: {'token': 'RAW TOKEN'}));
    a.stage(DictationDiagnosticStage.retirement);
    a.error(StateError('different raw error'));
    a.outputReleased();
    a.retirementCompleted();
    final v = store.snapshot;
    expect(v['first_failure'], 'platformFailure');
    expect(v['failure_stage'], 'outputPreparation');
    expect(v['verification_remaining_ms'], 0);
    expect(v['retirement_completed'], true);
    expect(jsonEncode(v), isNot(contains('PRIVATE')));
    expect(jsonEncode(v), isNot(contains('token')));
    expect(() => v['stage'] = 'injected', throwsUnsupportedError);
  });
  test('new attempt rejects old callbacks without erasing the new failure', () {
    final store = ForegroundDictationDiagnostics();
    final old = store.begin(contextCurrent: () => true);
    final current = store.begin(contextCurrent: () => true);
    current.stage(DictationDiagnosticStage.localCapability);
    current.fail(DictationDiagnosticFailure.unsupportedLocalRecognition);
    old.error(StateError('private old failure'));
    old.outputReleased();
    expect(store.snapshot['attempt'], 2);
    expect(store.snapshot['first_failure'], 'unsupportedLocalRecognition');
    expect(store.snapshot['output_released'], null);
  });
  test('same account retirement cannot resurrect old observations', () {
    final store = ForegroundDictationDiagnostics();
    var bindingEpoch = 7;
    final a = store.begin(contextCurrent: () => bindingEpoch == 7);
    a.fail(DictationDiagnosticFailure.authorityOrPolicy);
    bindingEpoch = 8;
    expect(store.snapshot['disposition'], 'context_retired');
    bindingEpoch = 7;
    a.outputReleased();
    expect(store.snapshot['disposition'], 'not_observed');
  });
  test('throwing account observer cannot throw through capture cleanup', () {
    final store = ForegroundDictationDiagnostics();
    final a = store.begin(contextCurrent: () => throw StateError('private uid'));
    expect(() => a.stage(DictationDiagnosticStage.listen), returnsNormally);
    expect(() => a.error(TimeoutException('private')), returnsNormally);
    expect(store.snapshot['disposition'], 'context_retired');
  });
  for (final error in [const FormatException('private'), MissingPluginException('private')]) {
    test('actual initial native probe failure is pinned before permissions ${error.runtimeType}', () async {
      final store = ForegroundDictationDiagnostics();
      final r = Rig();
      final e = Engine(candidate: BrokenProbe(error));
      final o = Output();
      final host = makeHost(r, e, o, store);
      await host.start();
      expect(e.permissions, 0);
      expect(e.capabilities, 0);
      expect(o.preparations, 0);
      expect(store.snapshot['failure_stage'], 'inputProbe');
      expect(store.snapshot['first_failure'], error is FormatException ? 'malformedProof' : 'missingPlugin');
      await host.retire();
      expect(store.snapshot['failure_stage'], 'inputProbe');
      host.dispose();
      r.lease.close();
    });
  }
  test('actual output failure remains output preparation after Cancel', () async {
    final store = ForegroundDictationDiagnostics();
    final r = Rig();
    final e = Engine();
    final o = BrokenOutput(TimeoutException('private request id'));
    final host = makeHost(r, e, o, store);
    await host.start();
    await host.retire();
    expect(store.snapshot['first_failure'], 'deadline');
    expect(store.snapshot['failure_stage'], 'outputPreparation');
    expect(store.snapshot['input_generation'], 0);
    expect(store.snapshot['retirement_completed'], true);
    expect(e.permissions, 0);
    host.dispose();
    r.lease.close();
  });
  test('actual unsupported local capability records reason without permission', () async {
    final store = ForegroundDictationDiagnostics();
    final r = Rig();
    final e = Engine();
    final o = Output();
    e.capability = Completer<bool>()..complete(false);
    final host = makeHost(r, e, o, store);
    await host.start();
    await host.retire();
    expect(store.snapshot['local_supported'], false);
    expect(store.snapshot['first_failure'], 'unsupportedLocalRecognition');
    expect(store.snapshot['failure_stage'], 'localCapability');
    expect(e.permissions, 0);
    expect(e.initializations, 0);
    host.dispose();
    r.lease.close();
  });
  test('actual microphone denial is distinct from unsupported recognition', () async {
    final store = ForegroundDictationDiagnostics();
    final r = Rig();
    final e = Engine();
    final o = Output();
    e.permission = Completer<bool>()..complete(false);
    final host = makeHost(r, e, o, store);
    await host.start();
    await host.retire();
    expect(store.snapshot['microphone_permitted'], false);
    expect(store.snapshot['first_failure'], 'permissionDenied');
    expect(store.snapshot['failure_stage'], 'microphonePermission');
    expect(e.initializations, 0);
    host.dispose();
    r.lease.close();
  });
  test('actual initialize false is distinct and no listen occurs', () async {
    final store = ForegroundDictationDiagnostics();
    final r = Rig();
    final e = InitFalse();
    final o = Output();
    final host = makeHost(r, e, o, store);
    await host.start();
    await host.retire();
    expect(store.snapshot['initialized'], false);
    expect(store.snapshot['first_failure'], 'initializeRejected');
    expect(store.snapshot['failure_stage'], 'speechInitialize');
    expect(e.port.generation, 0);
    host.dispose();
    r.lease.close();
  });
  test('actual expired authority rejects before output native effects', () async {
    final store = ForegroundDictationDiagnostics();
    final r = Rig();
    final e = Engine();
    final o = Output();
    final host = makeHost(r, e, o, store, authority: () => false);
    await host.start();
    expect(store.snapshot['first_failure'], 'authorityOrPolicy');
    expect(o.preparations, 0);
    expect(e.port.probes, 0);
    expect(e.permissions, 0);
    await host.retire();
    host.dispose();
    r.lease.close();
  });
  test('actual successful fake start and retirement remain observed without text', () async {
    final store = ForegroundDictationDiagnostics();
    final r = Rig();
    final e = Engine();
    final o = Output();
    final host = makeHost(r, e, o, store);
    await host.start();
    expect(store.snapshot['start_completed'], true);
    expect(store.snapshot['first_failure'], null);
    expect(store.snapshot['input_generation'], 1);
    expect(store.snapshot['engine_running'], true);
    await host.retire();
    expect(store.snapshot['output_released'], true);
    expect(store.snapshot['retirement_completed'], true);
    expect(jsonEncode(store.snapshot), isNot(contains(e.port.words)));
    host.dispose();
    r.lease.close();
  });
}

ForegroundDictationHost makeHost(Rig r, Engine e, Output o, ForegroundDictationDiagnostics store,
        {bool Function()? authority}) =>
    ForegroundDictationHost(
      diagnostic: store.begin(contextCurrent: () => true, verificationRemainingMs: 300000),
      registration:
          ForegroundDictationRegistration(isPolicyEnabled: () => true, reserveOutput: () => o, createEngine: () => e),
      readEditor: () => DictationEditorBinding(value: r.value, lease: r.lease, epoch: 3, revision: r.revision),
      hasExactAuthority: authority ?? () => true,
      canStartFresh: () => true,
      applyDraft: (_) {},
    );
