import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/pages/ella_provisioning_gate_page.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/home/page.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';

void main() {
  setUp(() async {
    SharedPreferences.setMockInitialValues({'ellaProvisioningAccountUid': 'uid-a'});
    await SharedPreferencesUtil.init();
    SharedPreferencesUtil().uid = 'uid-a';
  });

  testWidgets('setup failure remains fail closed and exposes its support code', (tester) async {
    final provider = EllaProvisioningProvider()
      ..state = EllaProvisioningState.blocked
      ..receipt = const EllaProvisioningReceipt(
        state: EllaProvisioningState.blocked,
        supportCode: 'ELLA-SUPPORT-301',
        errorCode: 'provisioning_disabled',
      );
    addTearDown(provider.dispose);

    await tester.pumpWidget(
      ChangeNotifierProvider.value(
        value: provider,
        child: const MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: EllaProvisioningGatePage(readyChild: SizedBox(), startOnMount: false),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('ELLA-SUPPORT-301'), findsOneWidget);
    expect(find.byType(HomePageWrapper), findsNothing);
    expect(find.byIcon(Icons.lock_outline_rounded), findsOneWidget);
  });

  testWidgets('update-required gate is explicit and does not render Home', (tester) async {
    final provider = EllaProvisioningProvider()
      ..state = EllaProvisioningState.blocked
      ..errorCode = 'upgrade_required';
    addTearDown(provider.dispose);

    await tester.pumpWidget(
      ChangeNotifierProvider.value(
        value: provider,
        child: const MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: EllaProvisioningGatePage(readyChild: SizedBox(), startOnMount: false),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('An update is required'), findsOneWidget);
    expect(find.textContaining('Update Ella to continue securely'), findsOneWidget);
    expect(find.byType(HomePageWrapper), findsNothing);
  });

  testWidgets('foreground resume revalidates consent before repeating provisioning ensure', (tester) async {
    final transport = _CountingReadyTransport();
    final provider = EllaProvisioningProvider(transport: transport);
    addTearDown(provider.dispose);
    await provider.start(
      uid: 'uid-a',
      requestContext: EllaProvisioningRequestContext(
        appVersion: '1.0.544+822',
        locale: 'en-US',
        timezone: 'America/Los_Angeles',
      ),
    );
    var consentRefreshes = 0;
    var refreshedUid = '';

    await tester.pumpWidget(
      ChangeNotifierProvider.value(
        value: provider,
        child: MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: EllaProvisioningGatePage(
            readyChild: const SizedBox(key: Key('ready-child')),
            startOnMount: false,
            authenticatedUidProvider: () => 'uid-a',
            appVersionProvider: () => '1.0.544+822',
            consentAuthorityRefresher: (uid) async {
              refreshedUid = uid;
              consentRefreshes++;
              return true;
            },
            timezoneProvider: () async => 'America/Los_Angeles',
          ),
        ),
      ),
    );
    expect(find.byKey(const Key('ready-child')), findsOneWidget);

    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    await tester.pump();
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pumpAndSettle();

    expect(consentRefreshes, 1);
    expect(refreshedUid, 'uid-a');
    expect(transport.ensureCalls, 2);
    expect(find.byKey(const Key('ready-child')), findsOneWidget);
  });

  testWidgets('foreground revalidation preserves Home while an operational receipt is checked', (tester) async {
    final transport = _DelayedResumeTransport();
    final provider = EllaProvisioningProvider(transport: transport);
    addTearDown(provider.dispose);
    await provider.start(
      uid: 'uid-a',
      requestContext: EllaProvisioningRequestContext(
        appVersion: '1.0.552+831',
        locale: 'en-US',
        timezone: 'America/Los_Angeles',
      ),
    );

    await tester.pumpWidget(
      ChangeNotifierProvider.value(
        value: provider,
        child: MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: EllaProvisioningGatePage(
            readyChild: const SizedBox(key: Key('ready-child')),
            startOnMount: false,
            authenticatedUidProvider: () => 'uid-a',
            appVersionProvider: () => '1.0.552+831',
            consentAuthorityRefresher: (_) async => true,
            timezoneProvider: () async => 'America/Los_Angeles',
          ),
        ),
      ),
    );

    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    await tester.pump();
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pump();
    await tester.pump();

    expect(transport.ensureCalls, 2);
    expect(provider.isRevalidatingOperational, isTrue);
    expect(find.byKey(const Key('ready-child')), findsOneWidget);
    expect(find.text('Setting up'), findsNothing);

    transport.revalidation.complete(_readyResponse());
    await tester.pumpAndSettle();

    expect(provider.isOperational, isTrue);
    expect(find.byKey(const Key('ready-child')), findsOneWidget);
  });

  testWidgets('foreground revalidation cannot provision after the authenticated account changes', (tester) async {
    final transport = _CountingReadyTransport();
    final provider = EllaProvisioningProvider(transport: transport);
    addTearDown(provider.dispose);
    await provider.start(
      uid: 'uid-a',
      requestContext: EllaProvisioningRequestContext(
        appVersion: '1.0.544+822',
        locale: 'en-US',
        timezone: 'America/Los_Angeles',
      ),
    );
    final consentRefresh = Completer<bool>();
    var authenticatedUid = 'uid-a';

    await tester.pumpWidget(
      ChangeNotifierProvider.value(
        value: provider,
        child: MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: EllaProvisioningGatePage(
            readyChild: const SizedBox(key: Key('ready-child')),
            startOnMount: false,
            authenticatedUidProvider: () => authenticatedUid,
            appVersionProvider: () => '1.0.544+822',
            consentAuthorityRefresher: (_) => consentRefresh.future,
            timezoneProvider: () async => 'America/Los_Angeles',
          ),
        ),
      ),
    );

    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    await tester.pump();
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pump();
    authenticatedUid = 'uid-b';
    consentRefresh.complete(true);
    await tester.pumpAndSettle();

    expect(transport.ensureCalls, 1);
  });
}

class _CountingReadyTransport implements EllaProvisioningTransport {
  int ensureCalls = 0;

  @override
  Future<EllaProvisioningResponse> ensure(EllaProvisioningRequestContext context) async {
    ensureCalls++;
    return EllaProvisioningResponse(
      statusCode: 200,
      receipt: EllaProvisioningReceipt.fromJson({
        'state': 'ready',
        'binding_state': 'active',
        'binding_revision': 1,
        'effective_policy_revision': 'policy-1',
      }),
    );
  }

  @override
  Future<EllaProvisioningResponse> status() async => throw UnimplementedError();
}

class _DelayedResumeTransport implements EllaProvisioningTransport {
  int ensureCalls = 0;
  final revalidation = Completer<EllaProvisioningResponse>();

  @override
  Future<EllaProvisioningResponse> ensure(EllaProvisioningRequestContext context) {
    ensureCalls++;
    return ensureCalls == 1 ? Future.value(_readyResponse()) : revalidation.future;
  }

  @override
  Future<EllaProvisioningResponse> status() async => throw UnimplementedError();
}

EllaProvisioningResponse _readyResponse() => EllaProvisioningResponse(
      statusCode: 200,
      receipt: EllaProvisioningReceipt.fromJson({
        'state': 'ready',
        'binding_state': 'active',
        'binding_revision': 1,
        'effective_policy_revision': 'policy-1',
      }),
    );
