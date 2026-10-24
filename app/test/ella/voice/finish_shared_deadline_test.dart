import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'ephemeral_dictation_session_test.dart' show Rig;
import '../audio_ownership/standard_speech_held_listen_test.dart' show settleSpeech;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('late Finish joins original deadline failure while raw native cleanup stays pinned', () async {
    final r = Rig(timeout: const Duration(milliseconds: 30));
    await r.session.start();
    r.port.terminalGate = Completer<void>();
    final first = r.session.finish(), join = r.session.finish();
    expect(identical(first, join), true);
    Object? firstError, joinError, lateError;
    final firstDone = first.catchError((Object error) {
      firstError = error;
      return null;
    });
    final joinDone = join.catchError((Object error) {
      joinError = error;
      return null;
    });
    await Future.wait([firstDone, joinDone]);
    expect(firstError, isA<StateError>());
    expect(identical(firstError, joinError), true);
    final late = r.session.finish();
    expect(identical(late, first), true);
    var lateSettled = false;
    final lateDone = late.catchError((Object error) {
      lateError = error;
      lateSettled = true;
      return null;
    });
    await settleSpeech();
    expect(lateSettled, true);
    expect(identical(firstError, lateError), true);
    expect(r.port.drains, 1);
    expect(r.registry.isReserved, true);
    expect(r.registry.reserve(source: 'competing-capture', hasAuthority: () => true), isNull);
    expect(r.applies, 0);
    expect(r.value.text, 'before after');
    expect(r.permissions, 1);
    expect(r.initializations, 1);
    expect(r.port.generation, 1);
    r.port.words = 'late terminal must never insert';
    r.port.terminalGate!.complete();
    await lateDone;
    await settleSpeech();
    await r.session.retire();
    expect(r.applies, 0);
    expect(r.value.text, 'before after');
    expect(r.registry.isReserved, false);
    expect(r.port.drains, 1);
    expect(r.port.generation, 1);
    r.lease.close();
  });
  test('Finish intent before Start forbids new permission initialization and listening', () async {
    final r = Rig();
    final finishing = r.session.finish();
    expect(await r.session.start(), false);
    expect(await finishing, isNull);
    expect(r.permissions, 0);
    expect(r.initializations, 0);
    expect(r.port.generation, 0);
    expect(r.port.drains, 0);
    expect(r.registry.isReserved, false);
    r.lease.close();
  });
}
