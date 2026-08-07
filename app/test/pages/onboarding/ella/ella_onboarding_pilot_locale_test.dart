import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/demo/ella_access_demo_fixtures.dart';
import 'package:omi/ella/services/ai_consent_policy.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';
import 'package:omi/ella/services/ella_entitlement_service.dart';
import 'package:omi/ella/services/ella_invite_link_controller.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/onboarding/ella/ella_onboarding.dart';
import 'package:omi/providers/ella_entitlement_provider.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';
import 'package:omi/providers/locale_provider.dart';

void main() {
  setUp(() async {
    SharedPreferences.setMockInitialValues({
      'uid': 'uid-a',
      'app_locale': 'es',
      'ellaProvisioningAccountUid': 'uid-a',
    });
    await SharedPreferencesUtil.init();
    EllaInviteLinkController.instance.clear();
  });

  tearDown(EllaInviteLinkController.instance.clear);

  test('consent service makes no request for an unsupported internal-pilot locale', () async {
    final transport = _GuardedConsentTransport();
    final service = EllaAiConsentService(
      transport: transport,
      pilotLocaleRestricted: true,
      appLocaleFactory: () => SharedPreferencesUtil().getString('app_locale'),
    );

    expect(await service.refreshServerAuthority(uid: 'uid-a'), isFalse);
    expect(transport.policyCalls, 0);
    expect(transport.statusCalls, 0);

    await SharedPreferencesUtil().saveString('app_locale', 'en');
    transport.allowRequests = true;

    expect(await service.refreshServerAuthority(uid: 'uid-a'), isTrue);
    expect(transport.policyCalls, 1);
    expect(transport.statusCalls, 1);
  });

  testWidgets('non-English pilot sign-in makes no request until the person switches to English', (tester) async {
    final consentTransport = _GuardedConsentTransport();
    final entitlementTransport = _GuardedEntitlementTransport();
    final provisioningTransport = _CountingProvisioningTransport();
    final entitlementProvider = EllaEntitlementProvider(
      transport: entitlementTransport,
      authenticatedUidChanges: const Stream.empty(),
      initialAuthenticatedUid: 'uid-a',
    );
    final provisioningProvider = EllaProvisioningProvider(transport: provisioningTransport);
    final localeProvider = LocaleProvider();
    var consentServiceCreations = 0;
    addTearDown(entitlementProvider.dispose);
    addTearDown(provisioningProvider.dispose);
    addTearDown(localeProvider.dispose);

    EllaInviteLinkController.instance.accept(Uri.parse('https://ella-ai-care.com/invite#c=ELLA-7K9Q'));

    await tester.pumpWidget(
      MultiProvider(
        providers: [
          ChangeNotifierProvider.value(value: entitlementProvider),
          ChangeNotifierProvider.value(value: provisioningProvider),
          ChangeNotifierProvider.value(value: localeProvider),
        ],
        child: Consumer<LocaleProvider>(
          builder: (context, locale, _) => MaterialApp(
            locale: locale.locale ?? const Locale('es'),
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            home: EllaOnboarding(
              pilotLocaleRestricted: true,
              entitlementGateEnabled: true,
              provisioningGateEnabled: true,
              authenticatedUidProvider: () => 'uid-a',
              isSignedInProvider: () => false,
              consentServiceFactory: () {
                consentServiceCreations++;
                return EllaAiConsentService(
                  transport: consentTransport,
                  pilotLocaleRestricted: true,
                  appLocaleFactory: () => SharedPreferencesUtil().getString('app_locale'),
                );
              },
              authBuilder: (context, onSignIn) => Center(
                child: FilledButton(onPressed: onSignIn, child: const Text('TEST SIGN IN')),
              ),
            ),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.text('TEST SIGN IN'));
    await tester.pumpAndSettle();

    expect(consentServiceCreations, 0);
    expect(consentTransport.policyCalls, 0);
    expect(consentTransport.statusCalls, 0);
    expect(consentTransport.submitCalls, 0);
    expect(entitlementTransport.fetchCalls, 0);
    expect(entitlementTransport.redeemCalls, 0);
    expect(provisioningTransport.ensureCalls, 0);
    expect(provisioningTransport.statusCalls, 0);
    expect(find.text('Seleccionar idioma'), findsOneWidget);
    expect(find.text('¡Esta función estará disponible pronto!'), findsOneWidget);
    expect(find.text('Allow and continue'), findsNothing);

    consentTransport.allowRequests = true;
    entitlementTransport.allowRequests = true;
    await tester.tap(find.text('English'));
    await tester.pumpAndSettle();

    expect(localeProvider.locale, const Locale('en'));
    expect(consentServiceCreations, 1);
    expect(consentTransport.policyCalls, 1);
    expect(consentTransport.statusCalls, 1);
    expect(consentTransport.submitCalls, 0);
    expect(entitlementTransport.fetchCalls, 1);
    expect(entitlementTransport.redeemCalls, 0);
    expect(provisioningTransport.ensureCalls, 0);
    expect(provisioningTransport.statusCalls, 0);
    expect(find.text('Your invite is ready'), findsOneWidget);
    expect(find.text('Allow and continue'), findsNothing);
  });

  testWidgets('pilot locale restriction leaves normal non-English sign-in unchanged when disabled', (tester) async {
    final consentTransport = _GuardedConsentTransport()..allowRequests = true;
    final entitlementTransport = _GuardedEntitlementTransport()..allowRequests = true;
    final entitlementProvider = EllaEntitlementProvider(
      transport: entitlementTransport,
      authenticatedUidChanges: const Stream.empty(),
      initialAuthenticatedUid: 'uid-a',
    );
    var consentServiceCreations = 0;
    addTearDown(entitlementProvider.dispose);

    await tester.pumpWidget(
      ChangeNotifierProvider.value(
        value: entitlementProvider,
        child: MaterialApp(
          locale: const Locale('es'),
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: EllaOnboarding(
            pilotLocaleRestricted: false,
            entitlementGateEnabled: true,
            provisioningGateEnabled: false,
            authenticatedUidProvider: () => 'uid-a',
            isSignedInProvider: () => false,
            consentServiceFactory: () {
              consentServiceCreations++;
              return EllaAiConsentService(
                transport: consentTransport,
                pilotLocaleRestricted: false,
                appLocaleFactory: () => SharedPreferencesUtil().getString('app_locale'),
              );
            },
            authBuilder: (context, onSignIn) => Center(
              child: FilledButton(onPressed: onSignIn, child: const Text('TEST SIGN IN')),
            ),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.text('TEST SIGN IN'));
    await tester.pumpAndSettle();

    expect(consentServiceCreations, 1);
    expect(consentTransport.policyCalls, 1);
    expect(consentTransport.statusCalls, 1);
    expect(entitlementTransport.fetchCalls, 1);
    expect(find.text('Seleccionar idioma'), findsNothing);
  });
}

class _GuardedConsentTransport extends EllaAiConsentTransport {
  bool allowRequests = false;
  int policyCalls = 0;
  int statusCalls = 0;
  int submitCalls = 0;

  void _requireAllowed() {
    if (!allowRequests) throw StateError('consent transport reached before English pilot choice');
  }

  @override
  Future<AiConsentPolicy?> fetchPolicy() async {
    policyCalls++;
    _requireAllowed();
    return AiConsentPolicy.bundled;
  }

  @override
  Future<AiConsentStatus?> fetchStatus() async {
    statusCalls++;
    _requireAllowed();
    return AiConsentStatus(
      subjectUid: 'uid-a',
      authorized: true,
      policy: AiConsentPolicy.bundled,
      decision: AiConsentDecision.granted.wireValue,
      receiptId: 'aicr_server-receipt',
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      appVersion: '1.0.528',
      buildNumber: '804',
      locale: 'en-US',
      profileBindingId: 'profile-binding-a',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
      serverDecidedAt: DateTime.now().toUtc(),
    );
  }

  @override
  Future<AiConsentStatus?> submit(AiConsentSubmission submission) async {
    submitCalls++;
    _requireAllowed();
    throw StateError('submit should not be called for a current server grant');
  }
}

class _GuardedEntitlementTransport implements EllaEntitlementTransport {
  bool allowRequests = false;
  int fetchCalls = 0;
  int redeemCalls = 0;

  void _requireAllowed() {
    if (!allowRequests) throw StateError('entitlement transport reached before English pilot choice');
  }

  @override
  Future<EllaEntitlement> fetch() async {
    fetchCalls++;
    _requireAllowed();
    return EllaAccessDemoFixtures.none;
  }

  @override
  Future<EllaEntitlement> redeem(String code) async {
    redeemCalls++;
    _requireAllowed();
    return EllaAccessDemoFixtures.active;
  }
}

class _CountingProvisioningTransport implements EllaProvisioningTransport {
  int ensureCalls = 0;
  int statusCalls = 0;

  @override
  Future<EllaProvisioningResponse> ensure(EllaProvisioningRequestContext context) {
    ensureCalls++;
    throw StateError('provisioning should not start before entitlement is active');
  }

  @override
  Future<EllaProvisioningResponse> status() {
    statusCalls++;
    throw StateError('provisioning status should not run before entitlement is active');
  }
}
