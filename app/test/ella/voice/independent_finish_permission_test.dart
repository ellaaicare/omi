import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'ephemeral_dictation_session_test.dart' as fixtures;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('finish intent during held permission prevents later recognition initialization', () async {
    final r = fixtures.Rig()..permission = Completer<bool>();
    final started = r.session.start();
    await Future<void>.delayed(const Duration(milliseconds: 10));
    expect(r.permissions, 1);
    final finished = r.session.finish();
    r.permission!.complete(true);
    try {
      expect(await started, false);
      expect(await finished, isNull);
      expect(r.port.generation, 0);
      expect(r.port.drains, 0);
      expect(r.initializations, 0, reason: 'Finish must close future recognition setup admission.');
      expect(r.applies, 0);
    } finally {
      await r.session.retire();
      r.lease.close();
    }
  });

}
