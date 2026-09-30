import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/upstream_capture/services/sockets/transcription_service.dart';

import '../../upstream_capture/support/capture/scripted_device_connection.dart';
import 'support/ella_upstream_capture_harness.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('real provider seam keeps pendant audio blocked until exact server-ready authority', () async {
    final directory = await Directory.systemTemp.createTemp('ella-protocol-provider-');
    final harness = await EllaUpstreamCaptureHarness.boot(tempDir: directory, protocolV2: true);
    addTearDown(() async {
      await harness.dispose();
      await directory.delete(recursive: true);
    });
    expect(await harness.bind(), isTrue);
    final link = ScriptedDeviceConnection();
    harness.deviceConnection = link;
    final starting = harness.provider.streamDeviceRecording(device: EllaUpstreamCaptureHarness.pendant);
    for (var turn = 0; turn < 30 && harness.socket == null; turn++) {
      await Future<void>.delayed(const Duration(milliseconds: 10));
    }
    final transport = harness.socket;
    expect(transport, isNotNull);
    expect(harness.provider.transcriptServiceReady, isFalse);
    expect(transport!.sentBinary, isEmpty);

    transport.onMessage(jsonEncode({
      'type': 'service_status',
      'status': 'capture_protocol_ready',
      'protocol_version': 2,
      'conversation_id': 'conversation-a',
      'generation': 'generation-a',
      'owner_token': 'owner-a',
    }));
    await starting;
    await harness.settle();
    expect(harness.provider.transcriptServiceReady, isTrue);
    expect(harness.sockets.single.service.state, SocketServiceState.connected);
    link.emitAudio();
    await harness.settle();
    expect(transport.sentBinary, isNotEmpty);
  });
}
