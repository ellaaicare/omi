import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';

void main() {
  Map<String, dynamic> ready() => {
        'state': 'ready',
        'retryable': false,
        'account_ready': true,
        'binding_state': 'active',
        'binding_revision': 6,
        'effective_policy_revision': 'fixture-policy',
        'runtime_provider': 'hermes',
        'runtime_status': 'active',
        'enrichment_ready': false,
        'background_processing_paused': true,
        'enrichment_processing_state': 'paused',
        'enrichment_error_code': 'account_enrichment_worker_unavailable',
        'enrichment_retryable': true,
        'enrichment_retry_after_ms': 2000,
      };
  test('reviewed account ready enrichment paused contract uses existing exact ready gates', () {
    final receipt = EllaProvisioningReceipt.fromJson(ready());
    expect(receipt.isOperational, isTrue);
    expect(receipt.state, EllaProvisioningState.ready);
    expect(receipt.errorCode, isEmpty);
    expect(receipt.retryable, isFalse);
  });
  test('paused enrichment fields cannot authorize missing binding revision', () {
    final body = ready()..remove('binding_revision');
    expect(EllaProvisioningReceipt.fromJson(body).isOperational, isFalse);
  });
  test('paused enrichment fields cannot authorize missing policy receipt', () {
    final body = ready()..remove('effective_policy_revision');
    expect(EllaProvisioningReceipt.fromJson(body).isOperational, isFalse);
  });
  test('account not ready still follows retryable lifecycle without Home authority', () {
    final body = ready()
      ..['state'] = 'retryable'
      ..['account_ready'] = false
      ..['retryable'] = true;
    expect(EllaProvisioningReceipt.fromJson(body).isOperational, isFalse);
    expect(EllaProvisioningReceipt.fromJson(body).state, EllaProvisioningState.degraded);
  });
}
