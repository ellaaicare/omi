import 'dart:async';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/guardian_mode_service.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const channel = MethodChannel('com.ellaaicare.ella/guardian_mode');
  final messenger = TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
  tearDown(() => messenger.setMockMethodCallHandler(channel, null));
  test('local Whisper stop does not turn mode off or claim server backlog ack', () async {
    final calls = <String>[];
    messenger.setMockMethodCallHandler(channel, (call) async {
      calls.add(call.method);
      return {
        'localOutputStopped': true,
        'serverDiscardVerified': false,
        'modeEnabled': true,
        'operationId': 'synthetic-stop'
      };
    });
    final receipt = await GuardianModeService().stopCurrentOutput();
    expect(calls, ['stopCurrentOutput']);
    expect(receipt.localOutputStopped, isTrue);
    expect(receipt.serverDiscardVerified, isFalse);
    expect(receipt.operationId, 'synthetic-stop');
  });
  for (final receipt in [
    null,
    {'localOutputStopped': false},
    {'localOutputStopped': true}
  ]) {
    test('missing local stop receipt is unconfirmed: $receipt', () async {
      messenger.setMockMethodCallHandler(channel, (_) async => receipt);
      await expectLater(GuardianModeService().stopCurrentOutput(), throwsStateError);
    });
  }
  testWidgets('held native stop acknowledgement has finite caller deadline', (tester) async {
    final held = Completer<Object?>();
    messenger.setMockMethodCallHandler(channel, (_) => held.future);
    final stop = GuardianModeService().stopCurrentOutput();
    final failure = expectLater(stop, throwsA(isA<TimeoutException>()));
    await tester.pump(const Duration(seconds: 10));
    await failure;
    held.complete({'localOutputStopped': true, 'serverDiscardVerified': false, 'operationId': 'synthetic-late'});
    await tester.pump();
  });
}
