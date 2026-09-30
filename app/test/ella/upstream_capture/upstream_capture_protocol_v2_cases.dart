import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/upstream_capture/backend/schema/transcript_segment.dart';
import 'package:omi/upstream_capture/services/sockets/transcription_service.dart';

import '../../upstream_capture/support/capture/capture_replay_world.dart' show ScriptedPureSocket;
import '../../upstream_capture/support/capture/scripted_device_connection.dart';
import 'support/ella_upstream_capture_harness.dart';

const _ready = {
  'type': 'service_status',
  'status': 'capture_protocol_ready',
  'protocol_version': 2,
  'conversation_id': 'conversation-a',
  'generation': 'generation-a',
  'owner_token': 'owner-a',
};

Future<ScriptedPureSocket> _startReadyPendant(EllaUpstreamCaptureHarness harness) async {
  harness.deviceConnection = ScriptedDeviceConnection();
  final starting = harness.provider.streamDeviceRecording(device: EllaUpstreamCaptureHarness.pendant);
  for (var turn = 0; turn < 30 && harness.socket == null; turn++) {
    await Future<void>.delayed(const Duration(milliseconds: 10));
  }
  final transport = harness.socket!;
  transport.onMessage(jsonEncode(_ready));
  await starting;
  await harness.settle();
  return transport;
}

Future<void> _ackDrain(ScriptedPureSocket transport) async {
  for (var turn = 0; turn < 100; turn++) {
    if (transport.sentText.any((text) => text.contains('capture_drain'))) {
      transport.onMessage(jsonEncode({..._ready, 'status': 'capture_protocol_drained'}));
      return;
    }
    await Future<void>.delayed(const Duration(milliseconds: 10));
  }
  fail('provider never sent capture_drain for the active pendant socket');
}

void registerUpstreamCaptureProtocolV2Cases() {
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

    transport.onMessage(jsonEncode(_ready));
    await starting;
    await harness.settle();
    expect(harness.provider.transcriptServiceReady, isTrue);
    expect(harness.sockets.single.service.state, SocketServiceState.connected);
    link.emitAudio();
    await harness.settle();
    expect(transport.sentBinary, isNotEmpty);
  });

  test('pendant double-tap processing uses the active exact socket without dock finish', () async {
    final directory = await Directory.systemTemp.createTemp('ella-protocol-button-');
    final harness = await EllaUpstreamCaptureHarness.boot(tempDir: directory, protocolV2: true);
    addTearDown(() async {
      await harness.dispose();
      await directory.delete(recursive: true);
    });
    expect(await harness.bind(), isTrue);
    final transport = await _startReadyPendant(harness);

    final processing = harness.provider.forceProcessingCurrentConversation();
    await _ackDrain(transport);
    await processing;
    await harness.settle();
    expect(harness.protocolFinalizations, [
      (conversationId: 'conversation-a', protocolVersion: 2, generation: 'generation-a', ownerToken: 'owner-a'),
    ]);
  });

  test('pendant-to-phone handoff finalizes pendant tuple before opening phone socket', () async {
    final directory = await Directory.systemTemp.createTemp('ella-protocol-handoff-');
    final harness = await EllaUpstreamCaptureHarness.boot(tempDir: directory, protocolV2: true);
    addTearDown(() async {
      await harness.dispose();
      await directory.delete(recursive: true);
    });
    expect(await harness.bind(), isTrue);
    final transport = await _startReadyPendant(harness);
    harness.provider.segments.add(TranscriptSegment(
      id: 'segment-a',
      text: 'A short captured sentence',
      speaker: null,
      isUser: false,
      personId: null,
      start: 0,
      end: 1,
      translations: const [],
    ));

    final handoff = harness.provider.streamRecording();
    await _ackDrain(transport);
    for (var turn = 0; turn < 30 && harness.sockets.length < 2; turn++) {
      await Future<void>.delayed(const Duration(milliseconds: 10));
    }
    expect(harness.protocolFinalizations, [
      (conversationId: 'conversation-a', protocolVersion: 2, generation: 'generation-a', ownerToken: 'owner-a'),
    ]);
    expect(harness.sockets.length, 2);
    harness.sockets.last.transport.onMessage(jsonEncode({..._ready, 'conversation_id': 'conversation-b'}));
    await handoff;
  });
}
