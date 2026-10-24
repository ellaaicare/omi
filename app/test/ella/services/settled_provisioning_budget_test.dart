import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';

class Poll implements EllaProvisioningPollHandle {
  Poll(this.callback);
  final VoidCallback callback;
  bool cancelled = false;
  @override
  void cancel() {
    cancelled = true;
  }

  void fire() {
    if (!cancelled) callback();
  }
}

class Held implements EllaProvisioningTransport {
  final reply = Completer<EllaProvisioningResponse>();
  int calls = 0;
  @override
  Future<EllaProvisioningResponse> ensure(EllaProvisioningRequestContext c) async => EllaProvisioningResponse(
      statusCode: 202,
      receipt: EllaProvisioningReceipt.fromJson(
          {'state': 'retryable', 'retryable': true, 'error_code': 'account_enrichment_worker_unavailable'}));
  @override
  Future<EllaProvisioningResponse> status() {
    calls++;
    return reply.future;
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() async {
    EllaProvisioningAuthorityCoordinator.resetForTesting();
    SharedPreferences.setMockInitialValues({'ellaProvisioningAccountUid': 'uid-a'});
    await SharedPreferencesUtil.init();
    SharedPreferencesUtil().uid = 'uid-a';
  });
  tearDown(EllaProvisioningAuthorityCoordinator.resetForTesting);
  for (final code in [0, 429]) {
    test('settled poll budget publishes exhausted for HTTP$code and resets on deliberate retry', () async {
      final t = Held();
      final scheduled = <Poll>[];
      final observed = <bool>[];
      final p = EllaProvisioningProvider(
          transport: t,
          maxPollAttempts: 1,
          scheduler: (d, c) {
            final h = Poll(c);
            scheduled.add(h);
            return h;
          });
      p.addListener(() => observed.add(p.automaticPollingExhausted));
      try {
        await p.start(
            uid: 'uid-a',
            requestContext: EllaProvisioningRequestContext(appVersion: 'fixture', locale: 'en', timezone: 'UTC'));
        scheduled.single.fire();
        expect(t.calls, 1);
        expect(p.automaticPollingExhausted, isFalse);
        t.reply.complete(EllaProvisioningResponse(statusCode: code));
        await Future<void>.delayed(Duration.zero);
        await Future<void>.delayed(Duration.zero);
        expect(p.automaticPollingExhausted, isTrue);
        expect(observed.last, isTrue);
        expect(scheduled, hasLength(1));
        expect(p.isOperational, isFalse);
        expect(p.errorCode, code == 429 ? 'quota_exceeded' : 'invalid_provisioning_response');
        await p.retry();
        expect(p.automaticPollingExhausted, isFalse);
        expect(scheduled, hasLength(2));
        expect(p.isOperational, isFalse);
        expect(t.calls, 1);
      } finally {
        p.dispose();
      }
    });
  }
}
