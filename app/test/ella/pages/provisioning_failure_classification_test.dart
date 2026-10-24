import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/pages/ella_provisioning_gate_page.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';

class _HTTPError implements EllaProvisioningTransport {
  _HTTPError(this.httpStatus);
  final int httpStatus;
  @override
  Future<EllaProvisioningResponse> ensure(EllaProvisioningRequestContext c) async => EllaProvisioningResponse(
      statusCode: httpStatus,
      receipt: EllaProvisioningReceipt.fromJson({
        'detail': {'code': 'synthetic_server_code'}
      }));
  @override
  Future<EllaProvisioningResponse> status() async => throw StateError('No poll admitted');
}

class _OneFailure implements EllaProvisioningTransport {
  @override
  Future<EllaProvisioningResponse> ensure(EllaProvisioningRequestContext c) async =>
      const EllaProvisioningResponse(statusCode: 0, clientFailure: EllaProvisioningClientFailure.authenticationRefresh);
  @override
  Future<EllaProvisioningResponse> status() async =>
      const EllaProvisioningResponse(statusCode: 0, clientFailure: EllaProvisioningClientFailure.authenticationRefresh);
}

class _Poll implements EllaProvisioningPollHandle {
  _Poll(this.callback);
  final void Function() callback;
  @override
  void cancel() {}
}

void main() {
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });
  testWidgets('exact retryable account-readiness contract is not labeled lost connection', (tester) async {
    final provider = EllaProvisioningProvider()
      ..state = EllaProvisioningState.degraded
      ..errorCode = 'account_readiness_unavailable'
      ..receipt = EllaProvisioningReceipt.fromJson({
        'state': 'retryable',
        'stage': 'validating',
        'retryable': true,
        'retry_after_ms': 2000,
        'binding_state': 'active',
        'binding_revision': 1,
        'effective_policy_revision': 'synthetic-model:synthetic-voice',
        'error_code': 'account_readiness_unavailable',
      });
    addTearDown(provider.dispose);
    await tester.pumpWidget(ChangeNotifierProvider.value(
        value: provider,
        child: const MaterialApp(
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            home: EllaProvisioningGatePage(startOnMount: false, readyChild: SizedBox(key: Key('protected-home'))))));
    await tester.pumpAndSettle();
    expect(provider.receipt!.isOperational, isFalse);
    expect(find.byKey(const Key('protected-home')), findsNothing);
    expect(find.text('Setup is waiting for account validation'), findsOneWidget);
    expect(find.text('Setup is waiting for a connection'), findsNothing);
    expect(find.text('Retry'), findsOneWidget);
  });
  for (final pair in [
    ('authentication_refresh_unavailable', 'Setup could not verify your sign-in'),
    ('invalid_provisioning_response', 'Setup received an unexpected response'),
    ('quota_exceeded', 'Setup checks are being limited'),
    ('provider_unavailable', 'Setup service is unavailable'),
    ('request_timeout', 'Setup request timed out'),
    ('transport_unavailable', 'Setup could not complete its check'),
    ('network_unavailable', 'Setup is waiting for a connection'),
  ]) {
    testWidgets('failure ${pair.$1} has distinct truthful label and no Home', (tester) async {
      final provider = EllaProvisioningProvider()
        ..state = EllaProvisioningState.degraded
        ..errorCode = pair.$1;
      addTearDown(provider.dispose);
      await tester.pumpWidget(ChangeNotifierProvider.value(
          value: provider,
          child: const MaterialApp(
              localizationsDelegates: AppLocalizations.localizationsDelegates,
              supportedLocales: AppLocalizations.supportedLocales,
              home: EllaProvisioningGatePage(startOnMount: false, readyChild: SizedBox(key: Key('protected-home'))))));
      await tester.pumpAndSettle();
      expect(find.text(pair.$2), findsOneWidget);
      expect(find.byKey(const Key('protected-home')), findsNothing);
      expect(find.text('Retry'), findsOneWidget);
    });
  }
  testWidgets('settled auth-refresh budget keeps category but promises no further automatic check', (tester) async {
    final polls = <_Poll>[];
    final provider = EllaProvisioningProvider(
        transport: _OneFailure(),
        maxPollAttempts: 1,
        scheduler: (d, cb) {
          final p = _Poll(cb);
          polls.add(p);
          return p;
        });
    addTearDown(provider.dispose);
    await provider.start(
        uid: 'synthetic-a',
        requestContext: EllaProvisioningRequestContext(appVersion: 'synthetic', locale: 'en-US', timezone: 'UTC'));
    polls.single.callback();
    await tester.pump();
    await tester.pump();
    expect(provider.automaticPollingExhausted, isTrue);
    expect(polls.length, 1);
    await tester.pumpWidget(ChangeNotifierProvider.value(
        value: provider,
        child: const MaterialApp(
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            home: EllaProvisioningGatePage(startOnMount: false, readyChild: SizedBox(key: Key('protected-home'))))));
    await tester.pumpAndSettle();
    expect(find.text('Setup could not verify your sign-in'), findsOneWidget);
    expect(find.textContaining('Automatic checks have stopped'), findsOneWidget);
    expect(find.byKey(const Key('protected-home')), findsNothing);
    expect(find.text('Retry'), findsOneWidget);
  });
  for (final pair in [(429, 'Setup checks are being limited'), (503, 'Setup service is unavailable')]) {
    testWidgets('structured HTTP${pair.$1} body cannot disguise failure as generic pause', (tester) async {
      final provider = EllaProvisioningProvider(transport: _HTTPError(pair.$1));
      addTearDown(provider.dispose);
      await provider.start(
          uid: 'synthetic-a',
          requestContext: EllaProvisioningRequestContext(appVersion: 'synthetic', locale: 'en-US', timezone: 'UTC'));
      await tester.pumpWidget(ChangeNotifierProvider.value(
          value: provider,
          child: const MaterialApp(
              localizationsDelegates: AppLocalizations.localizationsDelegates,
              supportedLocales: AppLocalizations.supportedLocales,
              home: EllaProvisioningGatePage(startOnMount: false, readyChild: SizedBox(key: Key('protected-home'))))));
      await tester.pumpAndSettle();
      expect(find.text(pair.$2), findsOneWidget);
      expect(find.byKey(const Key('protected-home')), findsNothing);
      expect(find.text('Retry'), findsNothing, reason: 'The classification must retain original blocked action policy');
    });
  }
}
