import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/whisper_delivery_client.dart';
import 'package:omi/ella/services/whisper_delivery_runtime.dart';
import 'thick_closed_stop_v2_test.dart' as closed;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  for (final failure in ['native Stop', 'pending persistence', 'commit persistence', 'closed receipt']) {
    test('current scheduled closed-poll $failure failure remains a public warning', () async {
      final x = closed.ClosedHarness();
      final channel = MethodChannel('fictional.closed.current.$failure');
      final messenger = TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
      var denyStop = false;
      messenger.setMockMethodCallHandler(
          channel, (call) async => call.method == 'strictStop' ? {'stopped': !denyStop} : {'accepted': true});
      final runtime = WhisperDeliveryRuntime.testing(
          transport: (method, path, fields, authority) => path == 'stop' && fields['resume'] == true
              ? x.h.client.transport(method, path, fields, authority)
              : x.transport(method, path, fields, authority),
          read: x.h.client.read,
          write: x.write,
          authorityProvider: () => x.h.authority,
          channel: channel);
      try {
        await runtime.client.start(WhisperPresentation.audio);
        await runtime.stopOutput(resume: true);
        x.close();
        if (failure == 'native Stop') denyStop = true;
        if (failure == 'pending persistence') x.denyPending = true;
        if (failure == 'commit persistence') x.denyCommit = true;
        if (failure == 'closed receipt') x.corrupt = (m) => m['uid'] = 'foreign';
        final deadline = DateTime.now().add(const Duration(seconds: 6));
        while (!runtime.failed && DateTime.now().isBefore(deadline)) {
          await Future<void>.delayed(const Duration(milliseconds: 10));
        }
        expect(runtime.failed, true);
        expect(runtime.client.ready, false);
        expect(runtime.client.stopPending, true);
        expect(runtime.client.acknowledgedStopRequest, isNull);
        denyStop = false;
        x.denyPending = false;
        x.denyCommit = false;
        x.corrupt = null;
        await runtime.stopOutput(resume: false);
        expect(runtime.failed, false);
        expect(runtime.client.stopPending, false);
      } finally {
        denyStop = false;
        x.denyPending = false;
        x.denyCommit = false;
        x.corrupt = null;
        await runtime.stopOutput(resume: false);
        await runtime.client.retireAuthority();
        runtime.dispose();
        messenger.setMockMethodCallHandler(channel, null);
      }
    }, timeout: const Timeout(Duration(seconds: 12)));
  }
}
