import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';
import 'package:omi/ella/widgets/ai_consent_sheet.dart';
import 'package:omi/l10n/app_localizations.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  Widget buildApp() => MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: AiConsentSheet(
            onAccept: () async {
              final preferences = SharedPreferencesUtil();
              preferences.uid = 'uid-a';
              preferences.acceptAiConsent(
                receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
                uid: 'uid-a',
                profileBindingId: 'profile-binding-a',
                serverDecidedAt: '2026-07-27T00:00:00Z',
              );
              preferences.markAiConsentServerVerified(
                uid: 'uid-a',
                receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
                policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
                processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
                profileBindingId: 'profile-binding-a',
                scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
                scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
              );
              return const AiConsentGrantOutcome.accepted(
                '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
              );
            },
          ),
        ),
      );

  Widget buildFailingApp(AiConsentGrantOutcome outcome) => MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(body: AiConsentSheet(onAccept: () async => outcome)),
      );

  testWidgets('presents as a bounded sheet with Not now always visible', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Builder(
          builder: (context) => Scaffold(
            body: TextButton(onPressed: () => AiConsentSheet.show(context), child: const Text('Show consent')),
          ),
        ),
      ),
    );

    await tester.tap(find.text('Show consent'));
    await tester.pumpAndSettle();

    final screenHeight = tester.view.physicalSize.height / tester.view.devicePixelRatio;
    expect(tester.getSize(find.byType(AiConsentSheet)).height, lessThanOrEqualTo(screenHeight * 0.95));
    expect(find.text('Not now').hitTestable(), findsOneWidget);
  });

  testWidgets('non-English internal pilot cannot open the English-only consent disclosure', (tester) async {
    bool? result = true;
    await tester.pumpWidget(
      MaterialApp(
        locale: const Locale('es'),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Builder(
          builder: (context) => Scaffold(
            body: TextButton(
              onPressed: () async {
                result = await AiConsentSheet.show(context, pilotLocaleRestricted: true);
              },
              child: const Text('Open'),
            ),
          ),
        ),
      ),
    );

    await tester.tap(find.text('Open'));
    await tester.pumpAndSettle();

    expect(result, isNull);
    expect(find.byType(AiConsentSheet), findsNothing);
    expect(find.text('Allow and continue'), findsNothing);
  });

  testWidgets('stale authority cannot persist a decline for the replacement account', (tester) async {
    final preferences = SharedPreferencesUtil()..uid = 'uid-b';
    await preferences.saveBool('aiConsentAccepted', true);
    await preferences.saveString('aiConsentReceiptUid', 'uid-b');
    await preferences.saveString('aiConsentReceiptId', 'replacement-account-receipt');
    var declineCalls = 0;

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Builder(
          builder: (context) => Scaffold(
            body: TextButton(
              onPressed: () => AiConsentSheet.show(
                context,
                canPersistDecision: () => false,
                onDecline: () async {
                  declineCalls++;
                  return true;
                },
              ),
              child: const Text('Show consent'),
            ),
          ),
        ),
      ),
    );

    await tester.tap(find.text('Show consent'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Not now'));
    await tester.pumpAndSettle();

    expect(declineCalls, 0);
    expect(preferences.getBool('aiConsentAccepted'), isTrue);
    expect(preferences.aiConsentReceiptUid, 'uid-b');
    expect(preferences.aiConsentReceiptId, 'replacement-account-receipt');
    expect(preferences.getString('aiConsentDeferredVersion'), isEmpty);
  });

  testWidgets('names managed-cloud recipients, data, purpose, and narrow scope before acceptance', (tester) async {
    await tester.pumpWidget(buildApp());
    await tester.pumpAndSettle();

    final disclosure =
        tester.widgetList<RichText>(find.byType(RichText)).map((widget) => widget.text.toPlainText()).join(' ');
    expect(
      disclosure,
      contains(
        'Please choose whether you agree to Ella sharing with these companies. '
        'The companies involved depend on the feature you choose:',
      ),
    );
    expect(disclosure, contains('secure backend'));
    expect(disclosure, contains('Nous Research / Hermes Cloud'));
    expect(disclosure, contains('what you say or type'));
    expect(disclosure, contains('OpenAI'));
    expect(disclosure, contains('Nous Research / Hermes Cloud profile memory'));
    expect(disclosure, contains('selected Ella profile'));
    expect(disclosure, contains('same account and profile'));
    expect(disclosure, isNot(contains('Honcho / Plastic Labs')));
    expect(disclosure, contains('Photon'));
    expect(disclosure, contains('one person you choose for testing'));
    expect(disclosure, contains('cannot message everyone'));
    expect(disclosure, contains('Ella will ask again'));
    expect(disclosure, contains('Deepgram'));
    expect(disclosure, contains('Soniox'));
    expect(disclosure, contains('Speechmatics'));
    expect(disclosure, contains('Google Firebase'));
    expect(disclosure, contains('Ella’s own Hermes, Honcho'));
    expect(disclosure, contains('OpenRouter'));
    expect(disclosure, contains('Google Gemini'));
    expect(disclosure, contains('OpenAI'));
    expect(disclosure, contains('Groq'));
    expect(disclosure, contains('xAI Grok'));
    expect(disclosure, contains('OpenAI and Ella’s artwork designer'));
    expect(disclosure, contains('OpenAI’s Codex and image services'));
    expect(disclosure, contains('saved memory'));
    expect(disclosure, contains('TypeSafe (Jev), via OpenRouter'));
    expect(disclosure, contains('extra support may be helpful'));
    expect(disclosure, contains('Guardian and Whispers'));
    expect(disclosure, isNot(contains('Daily Note')));
    expect(disclosure, contains('does not receive raw microphone audio, source photos, or your full memory history'));
    expect(disclosure, contains('ElevenLabs'));
    expect(disclosure, contains('Inworld AI'));
    expect(disclosure, contains('Kokoro'));
    expect(disclosure, contains('Fish'));
    expect(disclosure, contains('details from your saved memories'));
    expect(disclosure, contains('Full processor details in Privacy Policy'));
    expect(find.text('Not now'), findsOneWidget);

    // First-time consent (no prior authority) must be unchanged by the v10-to-v11
    // upgrade-copy fix: still says nothing is shared and Not now keeps everything off.
    expect(
      disclosure,
      contains(
        'Ella will not send what you say or type, messages, details from your saved memories, or Photon '
        'messages to these companies until you choose Allow. Not now keeps these cloud AI, memory, voice, '
        'and messaging features off. You can review or remove this permission in Settings.',
      ),
    );
    expect(disclosure, isNot(contains('Your previously approved v10 processing continues')));

    final normalizedDisclosure = disclosure.toLowerCase();
    for (final bannedWord in const [
      'monitor',
      'alert',
      'emergency',
      'detect',
      'track',
      'transcript',
      'recording',
      'omi',
      'fragment',
    ]) {
      expect(normalizedDisclosure, isNot(contains(bannedWord)), reason: 'Consent copy contains "$bannedWord"');
    }
  });

  testWidgets('accept records the current processor contract', (tester) async {
    await tester.pumpWidget(buildApp());
    await tester.pumpAndSettle();

    await tester.ensureVisible(find.text('Allow and continue'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Allow and continue'));
    await tester.pumpAndSettle();

    final preferences = SharedPreferencesUtil();
    expect(preferences.aiConsentAccepted, isTrue);
    expect(preferences.aiConsentContractVersion, SharedPreferencesUtil.currentAiConsentContractVersion);
    expect(preferences.aiConsentProcessorSetHash, SharedPreferencesUtil.currentAiConsentProcessorSetHash);
    expect(preferences.aiConsentDeferredVersion, isEmpty);
  });

  testWidgets('server-unavailable grant failure stays open with typed message and support code', (tester) async {
    await tester.pumpWidget(
      buildFailingApp(
        const AiConsentGrantOutcome.failed(
          AiConsentGrantFailureKind.serverUnavailable,
          supportCode: 'managed_cloud_consent_authority_unavailable',
        ),
      ),
    );
    await tester.pumpAndSettle();

    await tester.ensureVisible(find.text('Allow and continue'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Allow and continue'));
    await tester.pumpAndSettle();

    expect(find.byType(AiConsentSheet), findsOneWidget);
    expect(
      find.text(
        "Ella couldn't confirm your choice with the server, so nothing has been shared yet. "
        'Please try again in a few minutes.',
      ),
      findsOneWidget,
    );
    expect(find.text('managed_cloud_consent_authority_unavailable'), findsOneWidget);
    expect(find.text('Allow and continue').hitTestable(), findsOneWidget);
    expect(SharedPreferencesUtil().aiConsentAccepted, isFalse);
  });

  testWidgets('policy-mismatch grant failure asks for an app update', (tester) async {
    await tester.pumpWidget(
      buildFailingApp(
        const AiConsentGrantOutcome.failed(
          AiConsentGrantFailureKind.policyMismatch,
          supportCode: 'consent_policy_mismatch',
        ),
      ),
    );
    await tester.pumpAndSettle();

    await tester.ensureVisible(find.text('Allow and continue'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Allow and continue'));
    await tester.pumpAndSettle();

    expect(
      find.text(
        'This version of Ella no longer matches the current privacy policy. '
        'Please update the app, then try again.',
      ),
      findsOneWidget,
    );
    expect(find.text('consent_policy_mismatch'), findsOneWidget);
    expect(SharedPreferencesUtil().aiConsentAccepted, isFalse);
  });

  testWidgets('network grant failure shows connection guidance without a support code', (tester) async {
    await tester.pumpWidget(buildFailingApp(const AiConsentGrantOutcome.failed(AiConsentGrantFailureKind.network)));
    await tester.pumpAndSettle();

    await tester.ensureVisible(find.text('Allow and continue'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Allow and continue'));
    await tester.pumpAndSettle();

    expect(find.text("Ella couldn't reach the server. Check your connection and try again."), findsOneWidget);
    expect(SharedPreferencesUtil().aiConsentAccepted, isFalse);
  });

  testWidgets('Not now defers the current processor contract', (tester) async {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      uid: 'uid-a',
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:00:00Z',
    );
    preferences.markAiConsentServerVerified(
      uid: 'uid-a',
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      profileBindingId: 'profile-binding-a',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );

    await tester.pumpWidget(buildApp());
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('Not now'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Not now'));
    await tester.pumpAndSettle();

    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.aiConsentContractVersion, isEmpty);
    expect(preferences.isCurrentAiConsentDeferred, isTrue);
  });

  testWidgets('Not now on the v11 upgrade preserves exact v10 capture authority', (tester) async {
    final preferences = SharedPreferencesUtil()..uid = 'uid-a';
    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}v10-receipt',
      uid: 'uid-a',
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:00:00Z',
      policyVersion: SharedPreferencesUtil.legacyAiConsentContractVersionV10,
      processorSetHash: SharedPreferencesUtil.legacyAiConsentProcessorSetHashV10,
    );
    preferences.markAiConsentServerVerified(
      uid: 'uid-a',
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}v10-receipt',
      policyVersion: SharedPreferencesUtil.legacyAiConsentContractVersionV10,
      processorSetHash: SharedPreferencesUtil.legacyAiConsentProcessorSetHashV10,
      profileBindingId: 'profile-binding-a',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );

    await tester.pumpWidget(
      const MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(body: AiConsentSheet(preserveExistingAuthorityOnDecline: true)),
      ),
    );
    await tester.pumpAndSettle();

    final disclosure =
        tester.widgetList<RichText>(find.byType(RichText)).map((widget) => widget.text.toPlainText()).join(' ');
    expect(disclosure, contains('Your previously approved v10 processing continues under your existing consent.'));
    expect(
      disclosure,
      contains(
        'Not now keeps your existing consent active and withholds only the newly disclosed '
        'TypeSafe (Jev), via OpenRouter processing.',
      ),
    );
    expect(disclosure, isNot(contains('until you choose Allow')));
    expect(disclosure, isNot(contains('Not now keeps these cloud AI, memory, voice, and messaging features off.')));
    final upgradeCopy = find.byKey(const Key('ai-consent-upgrade-existing-authority'));
    final firstProcessor = find.text('Nous Research / Hermes Cloud');
    expect(upgradeCopy.hitTestable(), findsOneWidget);
    expect(tester.getTopLeft(upgradeCopy).dy, lessThan(tester.getTopLeft(firstProcessor).dy));

    await tester.ensureVisible(find.text('Not now'));
    await tester.tap(find.text('Not now'));
    await tester.pumpAndSettle();

    expect(preferences.aiConsentAccepted, isTrue);
    expect(preferences.aiConsentContractVersion, SharedPreferencesUtil.legacyAiConsentContractVersionV10);
    expect(preferences.isCurrentAiConsentDeferred, isTrue);
  });

  testWidgets('failed v11 upgrade dismisses with a retry notice and preserves exact v10 authority', (tester) async {
    final preferences = SharedPreferencesUtil()..uid = 'uid-a';
    const receiptId = '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}v10-receipt';
    preferences.acceptAiConsent(
      receiptId: receiptId,
      uid: 'uid-a',
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:00:00Z',
      policyVersion: SharedPreferencesUtil.legacyAiConsentContractVersionV10,
      processorSetHash: SharedPreferencesUtil.legacyAiConsentProcessorSetHashV10,
    );
    preferences.markAiConsentServerVerified(
      uid: 'uid-a',
      receiptId: receiptId,
      policyVersion: SharedPreferencesUtil.legacyAiConsentContractVersionV10,
      processorSetHash: SharedPreferencesUtil.legacyAiConsentProcessorSetHashV10,
      profileBindingId: 'profile-binding-a',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Builder(
          builder: (context) => Scaffold(
            body: Column(
              children: [
                const Text('Home content'),
                TextButton(
                  onPressed: () => AiConsentSheet.show(
                    context,
                    preserveExistingAuthorityOnDecline: true,
                    onAccept: () async => const AiConsentGrantOutcome.failed(
                      AiConsentGrantFailureKind.serverUnavailable,
                      supportCode: 'managed_cloud_consent_authority_unavailable',
                    ),
                  ),
                  child: const Text('Upgrade'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
    await tester.tap(find.text('Upgrade'));
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('Allow and continue'));
    await tester.tap(find.text('Allow and continue'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));

    expect(find.byType(AiConsentSheet), findsNothing);
    expect(find.text('Home content'), findsOneWidget);
    expect(
      find.text(
        'We couldn’t update your AI permission. Your existing permission is still active, and we’ll retry later.',
      ),
      findsOneWidget,
    );
    expect(preferences.aiConsentAccepted, isTrue);
    expect(preferences.aiConsentReceiptId, receiptId);
    expect(preferences.aiConsentContractVersion, SharedPreferencesUtil.legacyAiConsentContractVersionV10);
    expect(preferences.aiConsentServerVerificationRemaining, isNotNull);
    expect(preferences.isCurrentAiConsentDeferred, isFalse);
  });

  testWidgets('thrown v11 upgrade error also preserves exact v10 authority', (tester) async {
    final preferences = SharedPreferencesUtil()..uid = 'uid-a';
    const receiptId = '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}v10-receipt';
    preferences.acceptAiConsent(
      receiptId: receiptId,
      uid: 'uid-a',
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:00:00Z',
      policyVersion: SharedPreferencesUtil.legacyAiConsentContractVersionV10,
      processorSetHash: SharedPreferencesUtil.legacyAiConsentProcessorSetHashV10,
    );
    preferences.markAiConsentServerVerified(
      uid: 'uid-a',
      receiptId: receiptId,
      policyVersion: SharedPreferencesUtil.legacyAiConsentContractVersionV10,
      processorSetHash: SharedPreferencesUtil.legacyAiConsentProcessorSetHashV10,
      profileBindingId: 'profile-binding-a',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Builder(
          builder: (context) => Scaffold(
            body: TextButton(
              onPressed: () => AiConsentSheet.show(
                context,
                preserveExistingAuthorityOnDecline: true,
                onAccept: () => throw StateError('simulated upgrade failure'),
              ),
              child: const Text('Upgrade'),
            ),
          ),
        ),
      ),
    );
    await tester.tap(find.text('Upgrade'));
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('Allow and continue'));
    await tester.tap(find.text('Allow and continue'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));

    expect(find.byType(AiConsentSheet), findsNothing);
    expect(preferences.aiConsentAccepted, isTrue);
    expect(preferences.aiConsentReceiptId, receiptId);
    expect(preferences.aiConsentContractVersion, SharedPreferencesUtil.legacyAiConsentContractVersionV10);
    expect(preferences.aiConsentServerVerificationRemaining, isNotNull);
    expect(preferences.isCurrentAiConsentDeferred, isFalse);
  });

  testWidgets('review mode exposes revoke and deletion actions', (tester) async {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      uid: 'uid-a',
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:00:00Z',
    );
    preferences.markAiConsentServerVerified(
      uid: 'uid-a',
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      profileBindingId: 'profile-binding-a',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );
    var revokeCalled = false;

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Builder(
          builder: (context) => Scaffold(
            body: TextButton(
              onPressed: () => AiConsentSheet.show(
                context,
                reviewMode: true,
                onDecline: () async => revokeCalled = true,
                onRequestDeletion: () async {},
              ),
              child: const Text('Review consent'),
            ),
          ),
        ),
      ),
    );

    await tester.tap(find.text('Review consent'));
    await tester.pumpAndSettle();

    expect(find.text('Revoke AI permission'), findsOneWidget);
    expect(find.text('Delete my account and data'), findsOneWidget);

    await tester.tap(find.text('Revoke AI permission'));
    await tester.pumpAndSettle();

    expect(revokeCalled, isTrue);
    expect(preferences.aiConsentAccepted, isFalse);
  });

  testWidgets('review mode opened with valid consent is dismissible via close without forcing a choice',
      (tester) async {
    bool? result = false;

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Builder(
          builder: (context) => Scaffold(
            body: TextButton(
              onPressed: () async {
                result = await AiConsentSheet.show(
                  context,
                  reviewMode: true,
                  onRequestDeletion: () async {},
                );
              },
              child: const Text('Review consent'),
            ),
          ),
        ),
      ),
    );

    await tester.tap(find.text('Review consent'));
    await tester.pumpAndSettle();

    final closeButton = find.byKey(const Key('ai-consent-review-close'));
    expect(closeButton, findsOneWidget);

    await tester.tap(closeButton);
    await tester.pumpAndSettle();

    expect(find.byType(AiConsentSheet), findsNothing);
    expect(result, isNull);
  });

  testWidgets('first-consent flow stays non-dismissible and single screen', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Builder(
          builder: (context) => Scaffold(
            body: TextButton(onPressed: () => AiConsentSheet.show(context), child: const Text('Show consent')),
          ),
        ),
      ),
    );

    await tester.tap(find.text('Show consent'));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('ai-consent-review-close')), findsNothing);
    expect(find.byType(AiConsentSheet), findsOneWidget);

    // Tapping outside (where a dismissible sheet's barrier would be) must not
    // close the first-consent screen, and there is still only one screen.
    await tester.tapAt(const Offset(20, 20));
    await tester.pumpAndSettle();
    expect(find.byType(AiConsentSheet), findsOneWidget);
    expect(find.text('Allow and continue'), findsOneWidget);
    expect(find.text('Not now'), findsOneWidget);
  });

  testWidgets('delete account is visually separated and requires confirmation before it takes effect',
      (tester) async {
    var deletionRequested = 0;

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Builder(
          builder: (context) => Scaffold(
            body: TextButton(
              onPressed: () => AiConsentSheet.show(
                context,
                reviewMode: true,
                onDecline: () async => true,
                onRequestDeletion: () async => deletionRequested++,
              ),
              child: const Text('Review consent'),
            ),
          ),
        ),
      ),
    );

    await tester.tap(find.text('Review consent'));
    await tester.pumpAndSettle();

    final deleteButton = find.byKey(const Key('ai-consent-delete-account'));
    expect(deleteButton, findsOneWidget);
    expect(find.byType(Divider), findsOneWidget);
    // The destructive action sits below the Divider that separates it from
    // the primary Allow/Revoke choices.
    expect(tester.getTopLeft(deleteButton).dy, greaterThan(tester.getTopLeft(find.byType(Divider)).dy));

    await tester.tap(deleteButton);
    await tester.pumpAndSettle();

    // Tapping the destructive action must not take effect immediately — it
    // opens a confirmation dialog first.
    expect(deletionRequested, 0);
    expect(find.byType(AiConsentSheet), findsOneWidget);
    expect(find.text('Are you sure?'), findsOneWidget);

    await tester.tap(find.text('Cancel'));
    await tester.pumpAndSettle();
    expect(deletionRequested, 0);
    expect(find.byType(AiConsentSheet), findsOneWidget);

    await tester.tap(deleteButton);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Delete my account and data').hitTestable());
    await tester.pumpAndSettle();

    expect(deletionRequested, 1);
  });
}
