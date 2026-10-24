import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/pages/ella_provisioning_gate_page.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';
import 'package:omi/l10n/app_localizations.dart';

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

class FinalHTTPFailure implements EllaProvisioningTransport {
  FinalHTTPFailure(this.statusCode);
  final int statusCode;
  int statusCalls = 0;
  @override
  Future<EllaProvisioningResponse> ensure(EllaProvisioningRequestContext c) async => EllaProvisioningResponse(
      statusCode: 202,
      receipt: EllaProvisioningReceipt.fromJson({
        'state': 'retryable',
        'retryable': true,
        'error': {'code': 'account_enrichment_worker_unavailable'},
        'support_code': 'ELLA-FICTIONAL'
      }));
  @override
  Future<EllaProvisioningResponse> status() async {
    statusCalls++;
    return EllaProvisioningResponse(statusCode: statusCode);
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
  for (final code in [503, 202]) {
    testWidgets('last status HTTP$code without receipt shows automatic checks stopped', (t) async {
      final transport = FinalHTTPFailure(code);
      final scheduled = <Poll>[];
      final p = EllaProvisioningProvider(
          transport: transport,
          maxPollAttempts: 1,
          scheduler: (d, cb) {
            final h = Poll(cb);
            scheduled.add(h);
            return h;
          });
      addTearDown(p.dispose);
      await p.start(
          uid: 'uid-a',
          requestContext: EllaProvisioningRequestContext(
              appVersion: '1.0.1', locale: 'en-US', timezone: 'UTC', clientRequestId: 'fictional'));
      scheduled.single.fire();
      await t.pump();
      await t.pump();
      expect(transport.statusCalls, 1);
      expect(scheduled, hasLength(1));
      expect(p.isOperational, isFalse);
      await t.pumpWidget(ChangeNotifierProvider.value(
          value: p,
          child: const MaterialApp(
              localizationsDelegates: AppLocalizations.localizationsDelegates,
              supportedLocales: AppLocalizations.supportedLocales,
              home: EllaProvisioningGatePage(readyChild: SizedBox(key: Key('blocked-home')), startOnMount: false))));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('blocked-home')), findsNothing);
      expect(find.textContaining('Automatic checks have stopped'), findsOneWidget,
          reason: 'no scheduled/remaining status attempts after final HTTP$code');
      expect(find.textContaining('check again for a limited time'), findsNothing);
    });
  }
}
