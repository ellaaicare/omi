import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';

class _Poll implements EllaProvisioningPollHandle {
  @override
  void cancel() {}
}

class _Transport implements EllaProvisioningTransport {
  _Transport(this.body);
  final Map<String, dynamic> body;
  int ensures = 0;
  @override
  Future<EllaProvisioningResponse> ensure(EllaProvisioningRequestContext c) async {
    ensures++;
    return EllaProvisioningResponse(statusCode: 200, receipt: EllaProvisioningReceipt.fromJson(body));
  }

  @override
  Future<EllaProvisioningResponse> status() async => throw StateError('No scheduled poll is fired by this test');
}

Map<String, dynamic> _ready() => {
      'state': 'ready',
      'stage': 'ready',
      'binding_state': 'active',
      'binding_revision': 1,
      'effective_policy_revision': 'synthetic-model:synthetic-voice',
      'retryable': false,
      'account_ready': true,
      'enrichment_ready': false,
      'background_processing_paused': true,
      'enrichment_error_code': 'account_enrichment_worker_unavailable',
    };
EllaProvisioningRequestContext _context() =>
    EllaProvisioningRequestContext(appVersion: 'synthetic-cold', locale: 'en-US', timezone: 'UTC');
EllaProvisioningProvider _provider(_Transport t) =>
    EllaProvisioningProvider(transport: t, scheduler: (d, c) => _Poll());
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    EllaProvisioningAuthorityCoordinator.resetForTesting();
  });
  for (final freshReady in [true, false]) {
    test('cold process revalidates cached ready; new receipt ready=$freshReady determines admission', () async {
      final warm = _provider(_Transport(_ready()));
      await warm.start(uid: 'synthetic-a', requestContext: _context());
      expect(warm.isOperational, isTrue);
      expect(SharedPreferencesUtil().getEllaProvisioningReceipt('synthetic-a'), isNotNull);
      warm.dispose();
      // Same persisted preferences; a new provider represents process restart after update.
      final body = freshReady
          ? _ready()
          : (_ready()
            ..['state'] = 'retryable'
            ..['stage'] = 'validating'
            ..['retryable'] = true
            ..['retry_after_ms'] = 2000
            ..['error_code'] = 'account_readiness_unavailable');
      final transport = _Transport(body);
      final cold = _provider(transport);
      try {
        await cold.start(uid: 'synthetic-a', requestContext: _context());
        expect(transport.ensures, 1);
        expect(cold.isOperational, freshReady);
        expect(cold.state, freshReady ? EllaProvisioningState.ready : EllaProvisioningState.degraded);
        expect(cold.errorCode, freshReady ? '' : 'account_readiness_unavailable');
        expect(cold.isRevalidatingOperational, isFalse);
      } finally {
        cold.dispose();
      }
    });
  }
}
