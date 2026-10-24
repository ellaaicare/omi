import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_coordinator.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';
import 'package:omi/ella/widgets/ai_consent_sheet.dart';
import 'package:omi/l10n/app_localizations.dart';

class _ReviewService extends EllaAiConsentService {
  _ReviewService() : super(pilotLocaleRestricted: false);
  Completer<bool>? refresh;
  int refreshes = 0;
  int grants = 0;
  int revokes = 0;

  @override
  Future<bool> refreshServerAuthority({required String uid}) async {
    refreshes++;
    return refresh == null ? true : await refresh!.future;
  }

  @override
  Future<AiConsentGrantOutcome> grantCurrentConsentWithOutcome({required String uid}) async {
    grants++;
    return const AiConsentGrantOutcome.failed(AiConsentGrantFailureKind.rejected);
  }

  @override
  Future<bool> revokeCurrentConsent({required String uid}) async {
    revokes++;
    return true;
  }
}

void main() {
  setUp(() async {
    SharedPreferences.setMockInitialValues({'app_locale': 'en'});
    await SharedPreferencesUtil.init();
    final preferences = SharedPreferencesUtil()..uid = 'review-account-a';
    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}review',
      uid: 'review-account-a',
      profileBindingId: 'review-profile',
      serverDecidedAt: '2026-10-14T00:00:00Z',
    );
  });

  Future<void> mount(WidgetTester tester, _ReviewService service, String Function() uid,
      {bool Function()? canReview, void Function(bool)? result}) async {
    await tester.pumpWidget(MaterialApp(
      locale: const Locale('en'),
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: Builder(
          builder: (context) => Scaffold(
                  body: TextButton(
                onPressed: () async {
                  final accepted = await AiConsentCoordinator.review(context,
                      service: service, authenticatedUid: uid, canReview: canReview);
                  result?.call(accepted);
                },
                child: const Text('Explicit review'),
              ))),
    ));
    await tester.tap(find.text('Explicit review'));
    await tester.pumpAndSettle();
  }

  testWidgets('cached granted consent still presents explicit review; dismiss has no protected effects',
      (tester) async {
    final service = _ReviewService();
    expect(SharedPreferencesUtil().aiConsentAccepted, isTrue);
    bool? result;
    await mount(tester, service, () => 'review-account-a', result: (value) => result = value);
    expect(find.byType(AiConsentSheet), findsOneWidget);
    expect(service.refreshes, 1);
    expect(service.grants, 0);
    await tester.tap(find.byKey(const Key('ai-consent-review-close')));
    await tester.pumpAndSettle();
    expect(result, isFalse);
    expect(service.grants, 0);
    expect(service.revokes, 0);
    expect(SharedPreferencesUtil().aiConsentAccepted, isTrue);
  });

  testWidgets('held account A refresh cannot present choices for replacement B', (tester) async {
    final service = _ReviewService()..refresh = Completer<bool>();
    var uid = 'review-account-a';
    bool? result;
    await mount(tester, service, () => uid, result: (value) => result = value);
    uid = 'review-account-b';
    SharedPreferencesUtil().uid = uid;
    service.refresh!.complete(true);
    await tester.pumpAndSettle();
    expect(result, isFalse);
    expect(find.byType(AiConsentSheet), findsNothing);
    expect(service.grants, 0);
    expect(service.revokes, 0);
  });

  testWidgets('held A to B to A cannot revive the stale review request', (tester) async {
    final service = _ReviewService()..refresh = Completer<bool>();
    var uid = 'review-account-a';
    await mount(tester, service, () => uid);
    uid = 'review-account-b';
    SharedPreferencesUtil().uid = uid;
    uid = 'review-account-a';
    SharedPreferencesUtil().uid = uid;
    service.refresh!.complete(true);
    await tester.pumpAndSettle();
    expect(find.byType(AiConsentSheet), findsNothing);
    expect(service.grants, 0);
    expect(service.revokes, 0);
  });

  testWidgets('same account epoch retirement during refresh cannot present stale review', (tester) async {
    final service = _ReviewService()..refresh = Completer<bool>();
    var epoch = 1;
    await mount(tester, service, () => 'review-account-a', canReview: () => epoch == 1);
    epoch++;
    service.refresh!.complete(true);
    await tester.pumpAndSettle();
    expect(find.byType(AiConsentSheet), findsNothing);
    expect(service.grants, 0);
    expect(service.revokes, 0);
  });

  testWidgets('current user local revoke reaches server after retiring its caller fence', (tester) async {
    final service = _ReviewService();
    final preferences = SharedPreferencesUtil();
    final generation = preferences.aiConsentAuthorityGeneration;
    bool? result;
    await mount(tester, service, () => 'review-account-a',
        canReview: () => preferences.aiConsentAuthorityGeneration == generation, result: (value) => result = value);
    expect(find.byType(AiConsentSheet), findsOneWidget);
    await tester.tap(find.text('Revoke AI permission'));
    await tester.pumpAndSettle();
    expect(service.revokes, 1);
    expect(service.grants, 0);
    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.aiConsentAuthorityGeneration, isNot(generation));
    expect(result, isFalse);
  });

  testWidgets('explicit decline revokes without granting or starting protected work', (tester) async {
    final service = _ReviewService();
    bool? result;
    await mount(tester, service, () => 'review-account-a', result: (value) => result = value);
    await tester.tap(find.text('Revoke AI permission'));
    await tester.pumpAndSettle();
    expect(result, isFalse);
    expect(service.revokes, 1);
    expect(service.grants, 0);
    expect(SharedPreferencesUtil().aiConsentAccepted, isFalse);
  });

  testWidgets('replacement account cannot grant or revoke from an already open review', (tester) async {
    final service = _ReviewService();
    var uid = 'review-account-a';
    await mount(tester, service, () => uid);
    uid = 'review-account-b';
    SharedPreferencesUtil().uid = uid;
    await tester.tap(find.text('Revoke AI permission'));
    await tester.pumpAndSettle();
    expect(service.grants, 0);
    expect(service.revokes, 0);
  });

  testWidgets('replacement account cannot accept from an already open review', (tester) async {
    final service = _ReviewService();
    var uid = 'review-account-a';
    await mount(tester, service, () => uid);
    uid = 'review-account-b';
    SharedPreferencesUtil().uid = uid;
    await tester.tap(find.text('Allow and continue'));
    await tester.pumpAndSettle();
    expect(service.grants, 0);
    expect(service.revokes, 0);
    expect(find.byType(AiConsentSheet), findsOneWidget);
  });
}
