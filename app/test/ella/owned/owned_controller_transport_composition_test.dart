import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/services/mic/owned/owned_capture_contract.dart';
import 'package:omi/services/mic/owned/owned_private_store_transport.dart';
import 'owned_capture_controller_test.dart' show Rig;

void main() {
  final binding = TestWidgetsFlutterBinding.ensureInitialized();
  const channel = MethodChannel('ella.owned_private_capture.v1');
  tearDown(() => binding.defaultBinaryMessenger.setMockMethodCallHandler(channel, null));
  for (final quarantine in [false, true]) {
    test('actual codec root transport32 + controller ${quarantine ? 'quarantine' : 'retain'} exact drain', () async {
      final rig = Rig(factory: TransportOwnedPrivateSinkFactory(MethodChannelOwnedPrivateTransport()));
      binding.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
        final a = Map<Object?, Object?>.from(call.arguments as Map);
        expect(a['version'], 1);
        switch (call.method) {
          case 'getCapabilities':
            return {
              'version': 1,
              'preparedLifecycle': true,
              'privateStore': true,
              'retirement': true,
              'neverStartedCancel': true
            };
          case 'preparePrivateStore':
            return {
              'version': 1,
              'storeToken': 'token-A',
              'binding': a['binding'],
              'retention': a['retention'],
              'policyGeneration': 0
            };
          case 'bindPrivateStore':
            return {'version': 1, 'storeToken': 'token-A', 'handle': a['handle']};
          case 'stagePrivatePcm':
            final bytes = a['pcmBytes'] as Uint8List;
            if (rig.bytes.isEmpty) rig.bytes.addAll(bytes);
            return {
              'version': 1,
              'policyGeneration': a['policyGeneration'],
              'handle': a['handle'],
              'sequence': a['sequence'],
              'acceptedValidPcmBytes': bytes.length
            };
          case 'retirePrivateStore':
            return {'version': 1, 'policyGeneration': a['policyGeneration'], 'retention': a['retention']};
          case 'commitPrivateDrain':
            final s = a['summary'] as List;
            return {
              'version': 1,
              'policyGeneration': a['policyGeneration'],
              'ack': [
                s[0],
                s[1],
                s[5],
                s[6],
                s[7],
                s[8],
                quarantine ? 'commit-Q' : 'commit-R',
                quarantine ? 'quarantinedExactBinding' : 'retainedExactBinding',
                a['previousCommitId']
              ]
            };
        }
        throw StateError('unexpected method');
      });
      await rig.start();
      if (quarantine) {
        rig.authority = false;
        rig.policy = const OwnedRetentionSnapshot(1, OwnedPrivateRetention.quarantine);
        await rig.controller.retirePrivateRetention(rig.policy);
      }
      await rig.frame();
      expect(rig.publications, quarantine ? 0 : 1);
      await rig.controller.stop();
      expect(rig.registry.isReserved, false);
    });
  }
  test('actual root transport capability failure prevents permission and native preparation', () async {
    final rig = Rig(factory: TransportOwnedPrivateSinkFactory(MethodChannelOwnedPrivateTransport()));
    binding.defaultBinaryMessenger.setMockMethodCallHandler(
        channel,
        (_) async => {
              'version': 1,
              'preparedLifecycle': true,
              'privateStore': false,
              'retirement': true,
              'neverStartedCancel': true
            });
    await expectLater(rig.start(), throwsStateError);
    expect(rig.permissions, 0);
    expect(rig.prepares, 0);
    await rig.controller.stop();
    expect(rig.registry.isReserved, false);
  });
}
