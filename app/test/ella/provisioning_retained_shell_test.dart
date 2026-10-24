import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';

class Poll implements EllaProvisioningPollHandle { @override void cancel() {} }
EllaProvisioningResponse response(EllaProvisioningState state, {int status = 200}) =>
  EllaProvisioningResponse(statusCode: status, receipt: EllaProvisioningReceipt(
    state: state, bindingState: 'active', bindingRevision: 1,
    effectivePolicyRevision: 'test-policy', retryable: state != EllaProvisioningState.blocked,
  ));
class Transport implements EllaProvisioningTransport {
  int calls = 0;
  final next = Completer<EllaProvisioningResponse>();
  @override Future<EllaProvisioningResponse> ensure(EllaProvisioningRequestContext c) async {
    if (++calls == 1) return response(EllaProvisioningState.ready);
    return await next.future;
  }
  @override Future<EllaProvisioningResponse> status() async => throw StateError('No poll fired');
}
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() async {
    SharedPreferences.setMockInitialValues({'ellaProvisioningAccountUid': 'owner-a'});
    await SharedPreferencesUtil.init();
    SharedPreferencesUtil().uid = 'owner-a';
    EllaProvisioningAuthorityCoordinator.resetForTesting();
  });
  for (final state in [EllaProvisioningState.checking, EllaProvisioningState.queued,
      EllaProvisioningState.provisioning, EllaProvisioningState.degraded, EllaProvisioningState.blocked]) {
    test('same-owner refresh retains presentation only for nonterminal $state', () async {
      final t = Transport();
      final p = EllaProvisioningProvider(transport: t, scheduler: (_, __) => Poll());
      addTearDown(p.dispose);
      await p.start(uid: 'owner-a', requestContext: EllaProvisioningRequestContext(
        appVersion: 'test', locale: 'en-US', timezone: 'UTC', consentReceiptId: 'old'));
      expect(p.isOperational, isTrue);
      p.setConsentReceiptId('new');
      await Future<void>.delayed(Duration.zero);
      expect(p.isOperational, isFalse, reason: 'presentation never substitutes server authority');
      expect(p.isRevalidatingOperational, isTrue);
      t.next.complete(response(state, status: state == EllaProvisioningState.blocked ? 403 : 200));
      await Future<void>.delayed(Duration.zero);
      await Future<void>.delayed(Duration.zero);
      expect(p.receipt!.state, state, reason: 'latest server receipt is retained, not old ready');
      expect(p.isOperational, isFalse);
      expect(p.isRevalidatingOperational, state != EllaProvisioningState.blocked);
    });
  }
  test('different current owner cannot retain old presentation', () async {
    final t = Transport();
    final p = EllaProvisioningProvider(transport: t, scheduler: (_, __) => Poll());
    addTearDown(p.dispose);
    await p.start(uid: 'owner-a', requestContext: EllaProvisioningRequestContext(
      appVersion: 'test', locale: 'en-US', timezone: 'UTC'));
    SharedPreferencesUtil().uid = 'owner-b';
    expect(await EllaProvisioningAuthorityCoordinator.revalidate(uid: 'owner-b', consentReceiptId: 'new'), isFalse);
    expect(t.calls, 1);
    p.reset();
    expect(p.isOperational, isFalse);
    expect(p.isRevalidatingOperational, isFalse);
  });
}
