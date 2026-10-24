import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'ephemeral_dictation_session_test.dart' as fixtures;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('all callers of held finish have bounded deadline and share exact native work', () async {
    final r = fixtures.Rig(timeout: const Duration(milliseconds: 30));
    expect(await r.session.start(), true);
    r.port.terminalGate = Completer<void>();
    var firstDone = false, joinedDone = false;
    Object? firstError, joinedError;
    final first = r.session.finish().then<void>((_) => firstDone = true, onError: (Object error) {
      firstError = error;
      firstDone = true;
    });
    final joined = r.session.finish().then<void>((_) => joinedDone = true, onError: (Object error) {
      joinedError = error;
      joinedDone = true;
    });
    await Future<void>.delayed(const Duration(milliseconds: 90));
    try {
      expect(firstDone, true);
      expect(firstError, isA<StateError>());
      expect(r.port.drains, 1);
      expect(r.registry.isReserved, true);
      expect(r.applies, 0);
      expect(joinedDone, true, reason: 'Joining finish must not bypass the operation deadline.');
      expect(joinedError, isA<StateError>());
    } finally {
      r.port.terminalGate!.complete();
      await Future.wait([first, joined]);
      await r.session.retire();
      expect(r.applies, 0);
      r.lease.close();
    }
  });
}
