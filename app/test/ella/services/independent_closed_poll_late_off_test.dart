import 'dart:async';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/whisper_delivery_client.dart';
import 'package:omi/ella/services/whisper_delivery_runtime.dart';
import 'thick_closed_stop_v2_test.dart' as closed;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('old scheduled closed poll must not restore warning after confirmed late Off', () async {
    final x = closed.ClosedHarness();
    const channel = MethodChannel('fictional.independent.closed.poll.off');
    final messenger = TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    messenger.setMockMethodCallHandler(
        channel, (call) async => call.method == 'strictStop' ? {'stopped': true} : {'accepted': true});
    final entered = Completer<void>();
    final held = Completer<Map<String, dynamic>>();
    Map<String, dynamic>? oldResponse;
    final runtime = WhisperDeliveryRuntime.testing(
        transport: (method, path, fields, authority) async {
          final response = (path == 'stop' && fields['resume'] == true)
              ? await x.h.client.transport(method, path, fields, authority)
              : await x.transport(method, path, fields, authority);
          if (path == 'next' && !entered.isCompleted) {
            oldResponse = x.h.copy(response);
            entered.complete();
            return held.future;
          }
          return response;
        },
        read: x.h.client.read,
        write: x.write,
        authorityProvider: () => x.h.authority,
        channel: channel);
    try {
      await runtime.client.start(WhisperPresentation.audio);
      await runtime.stopOutput(resume: true); // uses actual runtime scheduler, no Firebase startOutput/SDK
      x.close();
      await entered.future.timeout(const Duration(seconds: 6));
      await runtime.stopOutput(resume: false);
      expect(runtime.failed, false);
      expect(runtime.client.ready, false);
      expect(runtime.client.stopPending, false);
      held.complete(oldResponse!);
      await Future<void>.delayed(const Duration(milliseconds: 50));
      expect(runtime.failed, false, reason: 'retired poll must not publish a failure into confirmed Off');
    } finally {
      if (!held.isCompleted) held.complete(oldResponse ?? {});
      await runtime.client.retireAuthority();
      runtime.dispose();
      messenger.setMockMethodCallHandler(channel, null);
    }
  }, timeout: const Timeout(Duration(seconds: 12)));
}
