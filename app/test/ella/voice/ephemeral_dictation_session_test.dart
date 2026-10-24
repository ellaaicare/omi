import 'dart:async';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/ella/voice/dictation_draft_snapshot.dart';
import 'package:omi/ella/voice/ephemeral_dictation_session.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import '../audio_ownership/standard_speech_held_listen_test.dart' show HeldSpeechPort, settleSpeech;

class FictionalDraftAuthority implements AccountCommitAuthority {
  int epoch = 1;
  @override
  String get uid => 'fictional-account';
  @override
  bool isCurrent() => epoch == 1;
  @override
  bool isExactCurrent() => isCurrent();
}

class Rig {
  Rig({TextEditingValue? initial, Duration timeout = const Duration(seconds: 2)}) {
    value = initial ?? const TextEditingValue(text: 'before after', selection: TextSelection.collapsed(offset: 7));
    lease = EllaAccountCommitBarrier.begin(authorityProvider: () => draftAuthority, onInvalidated: () {})!;
    session = EphemeralDictationSession(
        port: port,
        draft: DictationDraftSnapshot(value: value, lease: lease, epoch: 3),
        draftRevision: 0,
        readEditor: () => DictationEditorBinding(value: value, lease: lease, epoch: 3, revision: revision),
        hasExactCaptureAuthority: () => consentEpoch == 5,
        canStartFresh: () => fresh,
        requestPermission: () {
          permissions++;
          return permission?.future ?? Future.value(grant);
        },
        initializeRecognition: () async {
          initializations++;
          return true;
        },
        listen: port.listen,
        applyDraft: (next) {
          applies++;
          value = next;
          revision++;
        },
        ownership: registry,
        operationTimeout: timeout);
  }
  final registry = AudioCaptureOwnership();
  final port = HeldSpeechPort();
  final draftAuthority = FictionalDraftAuthority();
  late final EllaAccountCommitLease lease;
  late final EphemeralDictationSession session;
  late TextEditingValue value;
  int revision = 0, consentEpoch = 5, permissions = 0, initializations = 0, applies = 0;
  bool fresh = true, grant = true;
  Completer<bool>? permission;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('reserves synchronously before held permission and competing capture denied', () async {
    final r = Rig()..permission = Completer<bool>();
    final start = r.session.start();
    expect(r.registry.isReserved, true);
    expect(r.registry.reserve(source: 'other-capture', hasAuthority: () => true), isNull);
    await settleSpeech();
    expect(r.permissions, 1);
    expect(r.initializations, 0);
    final close = r.session.retire();
    r.permission!.complete(true);
    expect(await start, false);
    await close;
    expect(r.port.drains, 0);
    expect(r.registry.isReserved, false);
    r.lease.close();
  });
  test('finish inserts only final terminal text after held receipt; no auto-send', () async {
    final r = Rig();
    expect(await r.session.start(), true);
    r.port.terminalGate = Completer<void>();
    final finish = r.session.finish();
    await settleSpeech();
    expect(r.applies, 0);
    expect(r.value.text, 'before after');
    expect(r.registry.isReserved, true);
    r.port.terminalGate!.complete();
    expect((await finish)!.text, 'before final words after');
    expect(r.applies, 1);
    expect(r.registry.isReserved, false);
    r.lease.close();
  });
  test('repeated finish publishes local editor once', () async {
    final r = Rig();
    await r.session.start();
    final one = r.session.finish(), two = r.session.finish();
    await one;
    await two;
    expect(r.applies, 1);
    expect(r.port.drains, 1);
    r.lease.close();
  });
  test('draft full-value ABA revision protects restored draft', () async {
    final r = Rig();
    await r.session.start();
    final original = r.value;
    r.value = r.value.copyWith(text: 'changed');
    r.revision++;
    r.value = original;
    r.revision++;
    expect(await r.session.finish(), isNull);
    expect(r.value, original);
    expect(r.applies, 0);
    r.lease.close();
  });
  test('account and consent ABA during final await cannot publish', () async {
    final r = Rig();
    await r.session.start();
    r.port.terminalGate = Completer<void>();
    final finish = r.session.finish();
    await settleSpeech();
    r.draftAuthority.epoch = 2;
    r.consentEpoch = 6;
    r.draftAuthority.epoch = 3;
    r.consentEpoch = 7;
    r.port.terminalGate!.complete();
    expect(await finish, isNull);
    expect(r.applies, 0);
    expect(r.registry.isReserved, false);
    r.lease.close();
  });
  for (final initial in [
    const TextEditingValue(text: 'selected', selection: TextSelection(baseOffset: 0, extentOffset: 3)),
    const TextEditingValue(text: 'composing', composing: TextRange(start: 0, end: 3))
  ]) {
    test('protected selection/composition stays unchanged on dictation finish $initial', () async {
      final r = Rig(initial: initial);
      expect(await r.session.start(), false);
      expect(r.permissions, 0);
      expect(await r.session.finish(), isNull);
      expect(r.value, initial);
      expect(r.applies, 0);
      r.lease.close();
    });
  }
  test('busy fresh admission never stops or finalizes existing owner', () async {
    final r = Rig()..fresh = false;
    final other = r.registry.reserve(source: 'existing-capture', hasAuthority: () => true)!;
    expect(await r.session.start(), false);
    expect(other.isOwner, true);
    expect(r.port.drains, 0);
    expect(r.permissions, 0);
    await other.stop();
    r.lease.close();
  });
  test('permission denial releases only unused ephemeral lease', () async {
    final r = Rig()..grant = false;
    expect(await r.session.start(), false);
    expect(r.initializations, 0);
    expect(r.port.generation, 0);
    expect(r.registry.isReserved, false);
    r.lease.close();
  });
  test('consent loss during permission never reaches initialization/listen', () async {
    final r = Rig()..permission = Completer<bool>();
    final start = r.session.start();
    await settleSpeech();
    r.consentEpoch = 7;
    r.permission!.complete(true);
    expect(await start, false);
    expect(r.initializations, 0);
    expect(r.port.generation, 0);
    expect(r.applies, 0);
    r.lease.close();
  });
  test('modal retirement while final receipt held suppresses all editor publication', () async {
    final r = Rig();
    await r.session.start();
    r.port.terminalGate = Completer<void>();
    final finish = r.session.finish();
    await settleSpeech();
    final retired = r.session.retire();
    r.port.terminalGate!.complete();
    await retired;
    expect(await finish, isNull);
    expect(r.applies, 0);
    r.lease.close();
  });
  test('held listen finish cleans early and cannot insert before raw settlement', () async {
    final r = Rig();
    r.port.listenGate = Completer<void>();
    final start = r.session.start().catchError((_) => false);
    await settleSpeech();
    final finish = r.session.finish();
    await settleSpeech();
    expect(r.port.drains, 1);
    expect(r.applies, 0);
    expect(r.registry.isReserved, true);
    r.port.listenGate!.complete();
    await start;
    expect((await finish)!.text, 'before final words after');
    expect(r.port.drains, 2);
    expect(r.applies, 1);
    r.lease.close();
  });
  test('finish deadline never inserts a late terminal result', () async {
    final r = Rig(timeout: const Duration(milliseconds: 80));
    await r.session.start();
    r.port.terminalGate = Completer<void>();
    await expectLater(r.session.finish(), throwsStateError);
    expect(r.registry.isReserved, true);
    r.port.terminalGate!.complete();
    await settleSpeech();
    expect(r.applies, 0);
    expect(r.value.text, 'before after');
    r.lease.close();
  });
  test('unchanged invalid caret safely appends without replacing draft', () async {
    final r = Rig(initial: const TextEditingValue(text: 'draft'));
    await r.session.start();
    expect((await r.session.finish())!.text, 'draft final words');
    r.lease.close();
  });
}
