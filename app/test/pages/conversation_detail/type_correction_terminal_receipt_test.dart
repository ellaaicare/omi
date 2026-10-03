import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/env/env.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/conversation_detail/widgets.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class _ExactAuthority implements ExactAccountAuthorityVerifier {
  _ExactAuthority(this.uid);

  @override
  final String uid;

  bool current = true;

  @override
  bool isExactCurrent() => current;
}

class _TestEnv implements EnvFields {
  @override
  String? get apiBaseUrl => 'https://api.ella.test/';
  @override
  String? get googleClientId => null;
  @override
  String? get googleClientSecret => null;
  @override
  String? get googleMapsApiKey => null;
  @override
  String? get growthbookApiKey => null;
  @override
  String? get intercomAndroidApiKey => null;
  @override
  String? get intercomAppId => null;
  @override
  String? get intercomIOSApiKey => null;
  @override
  String? get mixpanelProjectToken => null;
  @override
  String? get openAIAPIKey => null;
  @override
  bool? get useAuthCustomToken => false;
  @override
  bool? get useWebAuth => false;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() async {
    Env.init(_TestEnv());
    await (FontLoader('Manrope')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf')))
        .load();
  });

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    final preferences = SharedPreferencesUtil()..uid = 'owner-1';
    preferences.acceptAiConsent(
      receiptId: 'aicr_receipt-1',
      uid: 'owner-1',
      profileBindingId: 'profile-binding-1',
      serverDecidedAt: '2026-08-15T22:00:00Z',
    );
    preferences.markAiConsentServerVerified(
      uid: 'owner-1',
      receiptId: 'aicr_receipt-1',
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      profileBindingId: 'profile-binding-1',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );
  });

  for (final outcome in [
    'identity_blocked',
    'direct_apply_failed',
    'applied',
    'pending',
    'missing',
    'mismatch',
    'server-mismatch',
    'unknown',
    'late-applied'
  ]) {
    testWidgets('ordinary form keeps truthful $outcome result after one accepted POST', (tester) async {
      final authority = _ExactAuthority('owner-1');
      var posts = 0;
      var gets = 0;
      var applied = 0;
      final receipts = <ConversationCorrectionReceipt>[];
      String? clientId;
      await tester.pumpWidget(MaterialApp(
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
            body: Builder(
                builder: (context) => TextButton(
                      onPressed: () => showModalBottomSheet<void>(
                        context: context,
                        isScrollControlled: true,
                        builder: (_) => CorrectSummarySheet(
                          conversation: ServerConversation(
                              id: 'memory-1',
                              createdAt: DateTime.utc(2026),
                              structured: Structured('Original memory', 'Original overview')),
                          appSummary: 'Original overview',
                          authorityProvider: () => authority,
                          submitter: (
                                  {required conversationId,
                                  required correctionId,
                                  required correctionText,
                                  summaryTitle,
                                  summaryOverview,
                                  appSummary,
                                  expectedAuthenticatedUid,
                                  exactAuthority,
                                  required requestTimeout}) =>
                              submitConversationCorrectionResult(
                            conversationId: conversationId,
                            correctionId: correctionId,
                            correctionText: correctionText,
                            expectedAuthenticatedUid: expectedAuthenticatedUid,
                            exactAuthority: exactAuthority,
                            requestTimeout: requestTimeout,
                            transport: (
                                {required url,
                                required method,
                                required body,
                                required expectedAuthenticatedUid,
                                required exactAuthority,
                                required timeout}) async {
                              posts++;
                              expect(method, 'POST');
                              clientId = correctionId;
                              return http.Response(
                                  jsonEncode({
                                    'correction_id': outcome == 'server-mismatch' ? 'server-selected-id' : correctionId,
                                    'conversation_id': conversationId,
                                    'trace_id': 'test-trace',
                                    'status': 'queued',
                                    'queued': true
                                  }),
                                  202);
                            },
                          ),
                          receiptPoller: (
                                  {required conversationId,
                                  required correctionId,
                                  required expectedAuthenticatedUid,
                                  required exactAuthority,
                                  required pollBudget}) =>
                              getConversationCorrectionReceipt(
                            conversationId: conversationId,
                            correctionId: correctionId,
                            expectedAuthenticatedUid: expectedAuthenticatedUid,
                            exactAuthority: exactAuthority,
                            transport: (
                                {required url,
                                required method,
                                required body,
                                required expectedAuthenticatedUid,
                                required exactAuthority,
                                required timeout}) async {
                              gets++;
                              expect(method, 'GET');
                              expect(body, isEmpty);
                              expect(correctionId, clientId);
                              if (outcome == 'missing' ||
                                  outcome == 'server-mismatch' ||
                                  (outcome == 'late-applied' && gets == 1)) {
                                return http.Response('{}', 404);
                              }
                              return http.Response(
                                  jsonEncode({
                                    'correction_id': outcome == 'mismatch' ? 'other-id' : correctionId,
                                    'conversation_id': conversationId,
                                    'status': outcome == 'mismatch' || outcome == 'late-applied' ? 'applied' : outcome,
                                    'failure_code':
                                        outcome == 'identity_blocked' ? 'correction_candidate_identity_blocked' : null,
                                    'before': {'title': 'Original memory'},
                                    'after': {'title': 'Verified update'}
                                  }),
                                  200);
                            },
                          ),
                          onReceipt: (receipt, _) async => receipts.add(receipt),
                          onApplied: () async => applied++,
                        ),
                      ),
                      child: const Text('Open correction'),
                    ))),
      ));
      await tester.tap(find.text('Open correction'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byKey(const ValueKey('type-correction-input')), 'Synthetic correction');
      await tester.ensureVisible(find.byKey(const ValueKey('type-correction-submit')));
      await tester.tap(find.byKey(const ValueKey('type-correction-submit')));
      await tester.pumpAndSettle();
      expect(posts, 1);
      expect(gets, 1);
      if (outcome == 'applied') {
        expect(applied, 1);
        expect(receipts.single.isApplied, isTrue);
        expect(find.byType(CorrectSummarySheet), findsNothing);
      } else {
        expect(applied, 0);
        expect(find.byType(CorrectSummarySheet), findsOneWidget);
        expect(find.text('Memory updated'), findsNothing);
        if (outcome == 'identity_blocked' || outcome == 'direct_apply_failed') {
          expect(find.text("Ella couldn't update this memory"), findsOneWidget);
          expect(find.byKey(const ValueKey('type-correction-check-status')), findsNothing);
        } else {
          expect(find.text("We haven't confirmed the result yet."), findsOneWidget);
          await tester.ensureVisible(find.byKey(const ValueKey('type-correction-check-status')));
          await tester.tap(find.byKey(const ValueKey('type-correction-check-status')));
          await tester.pumpAndSettle();
          expect(gets, 2);
          expect(posts, 1, reason: 'Check status must never resubmit');
          if (outcome == 'late-applied') {
            expect(applied, 1);
            expect(receipts.single.isApplied, isTrue);
            expect(find.byType(CorrectSummarySheet), findsNothing);
          }
        }
        final submit = find.byKey(const ValueKey('type-correction-submit'));
        if (submit.evaluate().isNotEmpty) expect(tester.widget<FilledButton>(submit).onPressed, isNull);
      }
    });
  }

  for (final transition in ['account', 'disposed', 'conversation']) {
    testWidgets('late applied receipt after $transition transition cannot commit or close another route',
        (tester) async {
      final authority = _ExactAuthority('owner-1');
      final pending = Completer<ConversationCorrectionReceipt?>();
      final conversation = ValueNotifier(ServerConversation(
          id: 'original-memory',
          createdAt: DateTime.utc(2026),
          structured: Structured('Original', 'Original overview')));
      addTearDown(conversation.dispose);
      var posts = 0;
      var applied = 0;
      var observed = 0;
      String? submittedId;
      await tester.pumpWidget(MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
            body: Builder(
                builder: (context) => TextButton(
                      onPressed: () => showModalBottomSheet<void>(
                        context: context,
                        isScrollControlled: true,
                        builder: (_) => ValueListenableBuilder<ServerConversation>(
                          valueListenable: conversation,
                          builder: (_, value, __) => CorrectSummarySheet(
                            conversation: value,
                            appSummary: value.structured.overview,
                            authorityProvider: () => authority.current ? authority : null,
                            submitter: (
                                {required conversationId,
                                required correctionId,
                                required correctionText,
                                summaryTitle,
                                summaryOverview,
                                appSummary,
                                expectedAuthenticatedUid,
                                exactAuthority,
                                required requestTimeout}) async {
                              posts++;
                              submittedId = correctionId;
                              return ConversationCorrectionSubmitResult.accepted(ConversationCorrectionSubmission(
                                  conversationId: conversationId,
                                  correctionId: correctionId,
                                  traceId: 'test-trace',
                                  status: 'queued',
                                  queued: true));
                            },
                            receiptPoller: (
                                    {required conversationId,
                                    required correctionId,
                                    required expectedAuthenticatedUid,
                                    required exactAuthority,
                                    required pollBudget}) =>
                                pending.future,
                            onApplied: () async => applied++,
                            onReceipt: (_, __) async => observed++,
                          ),
                        ),
                      ),
                      child: const Text('Open correction'),
                    ))),
      ));
      await tester.tap(find.text('Open correction'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byKey(const ValueKey('type-correction-input')), 'Synthetic correction');
      await tester.tap(find.byKey(const ValueKey('type-correction-submit')));
      await tester.pump();
      expect(posts, 1);
      expect(find.byType(CorrectSummarySheet), findsOneWidget, reason: '202 is not terminal');
      switch (transition) {
        case 'account':
          authority.current = false;
        case 'disposed':
          Navigator.of(tester.element(find.byType(CorrectSummarySheet))).pop();
        case 'conversation':
          conversation.value = ServerConversation(
              id: 'other-memory', createdAt: DateTime.utc(2026), structured: Structured('Other', 'Other overview'));
      }
      await tester.pumpAndSettle();
      pending.complete(ConversationCorrectionReceipt(
          conversationId: 'original-memory',
          correctionId: submittedId!,
          status: 'applied',
          before: const ConversationCorrectionSummary(title: 'Original'),
          after: const ConversationCorrectionSummary(title: 'Changed')));
      await tester.pumpAndSettle();
      expect(applied, 0);
      expect(observed, 0);
      expect(find.text('Memory updated'), findsNothing);
      if (transition == 'conversation') expect(find.byType(CorrectSummarySheet), findsOneWidget);
      expect(find.text('Open correction'), findsOneWidget);
    });
  }

  testWidgets('correction sheet scrolls above keyboard at 3x text on a small phone', (tester) async {
    tester.view.physicalSize = const Size(320, 568);
    tester.view.devicePixelRatio = 1;
    tester.view.viewInsets = const FakeViewPadding(bottom: 260);
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetViewInsets);
    const captureKey = ValueKey('correction-layout-capture');
    await tester.pumpWidget(RepaintBoundary(
        key: captureKey,
        child: MaterialApp(
          theme: ellaThemeData(),
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          builder: (context, child) => MediaQuery(
            data: MediaQuery.of(context).copyWith(textScaler: const TextScaler.linear(3)),
            child: child!,
          ),
          home: Scaffold(
              body: Builder(
                  builder: (context) => TextButton(
                        onPressed: () => showModalBottomSheet<void>(
                          context: context,
                          isScrollControlled: true,
                          builder: (_) => CorrectSummarySheet(
                            conversation: ServerConversation(
                                id: 'layout-memory',
                                createdAt: DateTime.utc(2026),
                                structured: Structured('Memory', 'Overview')),
                            appSummary: 'Overview',
                            authorityProvider: () => _ExactAuthority('owner-1'),
                            submitter: (
                                    {required conversationId,
                                    required correctionId,
                                    required correctionText,
                                    summaryTitle,
                                    summaryOverview,
                                    appSummary,
                                    expectedAuthenticatedUid,
                                    exactAuthority,
                                    required requestTimeout}) async =>
                                ConversationCorrectionSubmitResult.accepted(ConversationCorrectionSubmission(
                                    conversationId: conversationId,
                                    correctionId: correctionId,
                                    traceId: 'test-trace',
                                    status: 'queued',
                                    queued: true)),
                            receiptPoller: (
                                    {required conversationId,
                                    required correctionId,
                                    required expectedAuthenticatedUid,
                                    required exactAuthority,
                                    required pollBudget}) async =>
                                null,
                          ),
                        ),
                        child: const Text('Open'),
                      ))),
        )));
    await tester.tap(find.text('Open'));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    await tester.ensureVisible(find.byKey(const ValueKey('type-correction-submit')));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('type-correction-submit')).hitTestable(), findsOneWidget);
    final submit = tester.widget<FilledButton>(find.byKey(const ValueKey('type-correction-submit')));
    final foreground = submit.style!.foregroundColor!.resolve({})!;
    final background = submit.style!.backgroundColor!.resolve({})!;
    final luminances = [foreground.computeLuminance(), background.computeLuminance()]..sort();
    expect((luminances.last + 0.05) / (luminances.first + 0.05), greaterThanOrEqualTo(4.5));
    expect(tester.getSize(find.byKey(const ValueKey('type-correction-submit'))).height, greaterThanOrEqualTo(48));
    expect(tester.takeException(), isNull);
    if (const bool.fromEnvironment('ELLA_CAPTURE_TEST_LAYOUT')) {
      final boundary = tester.renderObject<RenderRepaintBoundary>(find.byKey(captureKey));
      await tester.runAsync(() async {
        final image = await boundary.toImage();
        final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
        await File('/tmp/ella-correction-submit-3x.png').writeAsBytes(bytes!.buffer.asUint8List());
        image.dispose();
      });
    }
    await tester.enterText(find.byKey(const ValueKey('type-correction-input')), 'Synthetic correction');
    await tester.ensureVisible(find.byKey(const ValueKey('type-correction-submit')));
    await tester.tap(find.byKey(const ValueKey('type-correction-submit')));
    await tester.pumpAndSettle();
    expect(find.text("We haven't confirmed the result yet."), findsOneWidget);
    for (final key in ['type-correction-check-status', 'type-correction-close']) {
      final action = find.byKey(ValueKey(key));
      await tester.ensureVisible(action);
      await tester.pumpAndSettle();
      expect(action.hitTestable(), findsOneWidget);
      expect(tester.getSize(action).height, greaterThanOrEqualTo(48));
    }
    expect(tester.takeException(), isNull);
  });

  testWidgets('test_type_correction_202_acknowledges_queue_immediately_and_polls_without_logging_body', (tester) async {
    const conversationId = 'conversation-1';
    const correctionText = 'private correction sentinel';
    const responseOnlySentinel = 'response-body-must-not-be-logged';
    final authority = _ExactAuthority('owner-1');
    final logs = <String>[];
    var receiptCalls = 0;
    var refreshCalls = 0;
    var acceptedCalls = 0;
    final submittedCorrectionIds = <String>[];

    Future<ConversationCorrectionSubmitResult> submitter({
      required String conversationId,
      required String correctionId,
      required String correctionText,
      String? summaryTitle,
      String? summaryOverview,
      String? appSummary,
      String? expectedAuthenticatedUid,
      ExactAccountAuthorityVerifier? exactAuthority,
      required Duration requestTimeout,
    }) {
      submittedCorrectionIds.add(correctionId);
      return submitConversationCorrectionResult(
        conversationId: conversationId,
        correctionId: correctionId,
        correctionText: correctionText,
        summaryTitle: summaryTitle,
        summaryOverview: summaryOverview,
        appSummary: appSummary,
        expectedAuthenticatedUid: expectedAuthenticatedUid,
        exactAuthority: exactAuthority,
        requestTimeout: requestTimeout,
        debugLog: logs.add,
        transport: ({
          required url,
          required method,
          required body,
          required expectedAuthenticatedUid,
          required exactAuthority,
          required timeout,
        }) async {
          expect(method, 'POST');
          expect(expectedAuthenticatedUid, 'owner-1');
          expect(identical(exactAuthority, authority), isTrue);
          expect(jsonDecode(body)['correction_text'], correctionText);
          expect(jsonDecode(body)['correction_id'], correctionId);
          return http.Response(
            jsonEncode({
              'correction_id': correctionId,
              'conversation_id': conversationId,
              'trace_id': 'correction:$conversationId:$correctionId',
              'status': 'queued',
              'queued': true,
              'private_response_body': responseOnlySentinel,
            }),
            202,
          );
        },
      );
    }

    Future<ConversationCorrectionReceipt?> receiptPoller({
      required String conversationId,
      required String correctionId,
      required String expectedAuthenticatedUid,
      required ExactAccountAuthorityVerifier exactAuthority,
      required Duration pollBudget,
    }) {
      return pollConversationCorrectionReceipt(
        conversationId: conversationId,
        correctionId: correctionId,
        expectedAuthenticatedUid: expectedAuthenticatedUid,
        exactAuthority: exactAuthority,
        pollBudget: pollBudget,
        maxAttempts: 3,
        wait: (_) async {},
        fetchReceipt: ({
          required conversationId,
          required correctionId,
          required expectedAuthenticatedUid,
          required exactAuthority,
          required requestTimeout,
        }) {
          receiptCalls += 1;
          return getConversationCorrectionReceipt(
            conversationId: conversationId,
            correctionId: correctionId,
            expectedAuthenticatedUid: expectedAuthenticatedUid,
            exactAuthority: exactAuthority,
            debugLog: logs.add,
            transport: ({
              required url,
              required method,
              required body,
              required expectedAuthenticatedUid,
              required exactAuthority,
              required timeout,
            }) async {
              expect(method, 'GET');
              expect(body, isEmpty);
              expect(expectedAuthenticatedUid, 'owner-1');
              expect(identical(exactAuthority, authority), isTrue);
              return http.Response(
                jsonEncode({
                  'correction_id': correctionId,
                  'conversation_id': conversationId,
                  'status': receiptCalls == 1 ? 'retry_queued' : 'direct_apply_failed',
                  'failure_code': receiptCalls == 1 ? null : 'self_hosted_runtime_target_mode_required',
                  'before': {'title': 'Before'},
                  'after': {'private_response_body': responseOnlySentinel},
                }),
                200,
              );
            },
          );
        },
      );
    }

    final conversation = ServerConversation(
      id: conversationId,
      createdAt: DateTime.utc(2026, 8, 15),
      structured: Structured('Before', '[Ella] Before.'),
    );
    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: CorrectSummarySheet(
            conversation: conversation,
            appSummary: '[Ella] Before.',
            submitter: submitter,
            receiptPoller: receiptPoller,
            authorityProvider: () => authority,
            onAccepted: () async => acceptedCalls += 1,
            onApplied: () async => refreshCalls += 1,
          ),
        ),
      ),
    );

    await tester.enterText(find.byKey(const ValueKey('type-correction-input')), correctionText);
    await tester.tap(find.byKey(const ValueKey('type-correction-submit')));
    await tester.pumpAndSettle();

    expect(receiptCalls, 2);
    expect(refreshCalls, 0);
    expect(acceptedCalls, 1);
    expect(submittedCorrectionIds, hasLength(1));
    expect(find.textContaining('self_hosted_runtime_target_mode_required'), findsNothing);
    expect(find.byType(CorrectSummarySheet), findsOneWidget);
    expect(find.text("Ella can't reach your correction service right now"), findsOneWidget);
    expect(find.text('Memory updated'), findsNothing);
    expect(logs, isNotEmpty);
    expect(logs.every((entry) => !entry.contains(correctionText)), isTrue);
    expect(logs.every((entry) => !entry.contains(responseOnlySentinel)), isTrue);
    expect(logs, [
      'submitConversationCorrection: status=202',
      'getConversationCorrectionReceipt: status=200',
      'getConversationCorrectionReceipt: status=200',
    ]);
  });

  testWidgets('test_type_correction_submit_failure_stays_visible_with_non_english_generic_localization', (
    tester,
  ) async {
    const conversationId = 'conversation-es';
    const correctionText = 'contenido privado que no debe registrarse';
    final authority = _ExactAuthority('owner-1');
    final logs = <String>[];

    final conversation = ServerConversation(
      id: conversationId,
      createdAt: DateTime.utc(2026, 8, 15),
      structured: Structured('Antes', '[Ella] Antes.'),
    );
    await tester.pumpWidget(
      MaterialApp(
        locale: const Locale('es'),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: CorrectSummarySheet(
            conversation: conversation,
            appSummary: '[Ella] Antes.',
            authorityProvider: () => authority,
            submitter: ({
              required conversationId,
              required correctionId,
              required correctionText,
              summaryTitle,
              summaryOverview,
              appSummary,
              expectedAuthenticatedUid,
              exactAuthority,
              required requestTimeout,
            }) async {
              logs.add('submit status=202');
              return const ConversationCorrectionSubmitResult.rejected();
            },
            receiptPoller: ({
              required conversationId,
              required correctionId,
              required expectedAuthenticatedUid,
              required exactAuthority,
              required pollBudget,
            }) async {
              fail('receipt polling must not start after a failed submit');
            },
          ),
        ),
      ),
    );

    await tester.enterText(find.byKey(const ValueKey('type-correction-input')), correctionText);
    await tester.tap(find.byKey(const ValueKey('type-correction-submit')));
    await tester.pumpAndSettle();

    expect(find.text('Ella no pudo actualizar este recuerdo'), findsOneWidget);
    expect(find.byType(CorrectSummarySheet), findsOneWidget);
    expect(logs.every((entry) => !entry.contains(correctionText)), isTrue);
  });

  testWidgets('uncertain correction delivery reconciles by durable id without showing a false failure', (tester) async {
    const conversationId = 'conversation-uncertain';
    final authority = _ExactAuthority('owner-1');
    var receiptCalls = 0;
    var acceptedCalls = 0;
    var appliedCalls = 0;

    final conversation = ServerConversation(
      id: conversationId,
      createdAt: DateTime.utc(2026, 8, 26),
      structured: Structured('Before', '[Ella] Before.'),
    );
    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: CorrectSummarySheet(
            conversation: conversation,
            appSummary: '[Ella] Before.',
            authorityProvider: () => authority,
            submitter: ({
              required conversationId,
              required correctionId,
              required correctionText,
              summaryTitle,
              summaryOverview,
              appSummary,
              expectedAuthenticatedUid,
              exactAuthority,
              required requestTimeout,
            }) async {
              return const ConversationCorrectionSubmitResult.uncertain();
            },
            receiptPoller: ({
              required conversationId,
              required correctionId,
              required expectedAuthenticatedUid,
              required exactAuthority,
              required pollBudget,
            }) async {
              receiptCalls += 1;
              return ConversationCorrectionReceipt(
                correctionId: correctionId,
                conversationId: conversationId,
                status: 'applied',
                before: const ConversationCorrectionSummary(title: 'Before'),
                after: const ConversationCorrectionSummary(title: 'After'),
              );
            },
            onAccepted: () async => acceptedCalls += 1,
            onApplied: () async => appliedCalls += 1,
          ),
        ),
      ),
    );

    await tester.enterText(find.byKey(const ValueKey('type-correction-input')), 'Correct this summary.');
    await tester.tap(find.byKey(const ValueKey('type-correction-submit')));
    await tester.pumpAndSettle();

    expect(receiptCalls, 1);
    expect(acceptedCalls, 1);
    expect(appliedCalls, 1);
    expect(find.byType(CorrectSummarySheet), findsNothing);
    expect(find.text('Ella couldn\'t update this memory'), findsNothing);
  });

  test('test_correction_receipt_pending_past_provider_timeout_later_applies_without_resubmission', () async {
    const conversationId = 'conversation-long-running';
    const correctionId = 'correction-long-running';
    final authority = _ExactAuthority('owner-1');
    var submissionCalls = 0;
    var receiptCalls = 0;
    var waited = Duration.zero;

    final submission = await submitConversationCorrection(
      conversationId: conversationId,
      correctionId: correctionId,
      correctionText: 'Correct the retained summary.',
      expectedAuthenticatedUid: authority.uid,
      exactAuthority: authority,
      debugLog: (_) {},
      transport: ({
        required url,
        required method,
        required body,
        required expectedAuthenticatedUid,
        required exactAuthority,
        required timeout,
      }) async {
        submissionCalls += 1;
        return http.Response(
          jsonEncode({
            'correction_id': correctionId,
            'conversation_id': conversationId,
            'trace_id': 'correction:$conversationId:$correctionId',
            'status': 'queued',
            'queued': true,
          }),
          202,
        );
      },
    );

    final receipt = await pollConversationCorrectionReceipt(
      conversationId: submission!.conversationId,
      correctionId: submission.correctionId,
      expectedAuthenticatedUid: authority.uid,
      exactAuthority: authority,
      wait: (duration) async => waited += duration,
      fetchReceipt: ({
        required conversationId,
        required correctionId,
        required expectedAuthenticatedUid,
        required exactAuthority,
        required requestTimeout,
      }) async {
        receiptCalls += 1;
        return ConversationCorrectionReceipt(
          correctionId: correctionId,
          conversationId: conversationId,
          status: receiptCalls <= 151 ? 'canonical_pending' : 'applied',
          before: const ConversationCorrectionSummary(title: 'Before'),
          after: const ConversationCorrectionSummary(title: 'Applied'),
        );
      },
    );

    expect(submissionCalls, 1);
    expect(receiptCalls, 152);
    expect(waited, const Duration(seconds: 151));
    expect(receipt?.isApplied, isTrue);
  });

  test('pending correction identity survives restart and is atomically reused by concurrent workers', () async {
    const store = PendingConversationCorrectionIdentityStore();
    const arguments = (
      uid: 'owner-1',
      conversationId: 'conversation-durable',
      correctionText: 'Correct the retained attribution.',
      summaryTitle: 'Before',
      summaryOverview: '[Ella] Before.',
      appSummary: '[Ella] Before.',
    );

    final concurrentIds = await Future.wait([
      store.acquire(
        uid: arguments.uid,
        conversationId: arguments.conversationId,
        correctionText: arguments.correctionText,
        summaryTitle: arguments.summaryTitle,
        summaryOverview: arguments.summaryOverview,
        appSummary: arguments.appSummary,
      ),
      const PendingConversationCorrectionIdentityStore().acquire(
        uid: arguments.uid,
        conversationId: arguments.conversationId,
        correctionText: arguments.correctionText,
        summaryTitle: arguments.summaryTitle,
        summaryOverview: arguments.summaryOverview,
        appSummary: arguments.appSummary,
      ),
    ]);
    final afterRestart = await const PendingConversationCorrectionIdentityStore().acquire(
      uid: arguments.uid,
      conversationId: arguments.conversationId,
      correctionText: arguments.correctionText,
      summaryTitle: arguments.summaryTitle,
      summaryOverview: arguments.summaryOverview,
      appSummary: arguments.appSummary,
    );

    expect(concurrentIds.toSet(), hasLength(1));
    expect(afterRestart, concurrentIds.first);
  });

  test('payload identities survive A to B to B and A to B to A interleaving', () async {
    const store = PendingConversationCorrectionIdentityStore();
    final original = await store.acquire(
      uid: 'owner-1',
      conversationId: 'conversation-changed',
      correctionText: 'Original correction.',
      summaryTitle: 'Before',
    );
    final changed = await store.acquire(
      uid: 'owner-1',
      conversationId: 'conversation-changed',
      correctionText: 'Changed correction.',
      summaryTitle: 'Before',
    );
    final changedReplay = await const PendingConversationCorrectionIdentityStore().acquire(
      uid: 'owner-1',
      conversationId: 'conversation-changed',
      correctionText: 'Changed correction.',
      summaryTitle: 'Before',
    );
    final originalReplay = await const PendingConversationCorrectionIdentityStore().acquire(
      uid: 'owner-1',
      conversationId: 'conversation-changed',
      correctionText: 'Original correction.',
      summaryTitle: 'Before',
    );
    await store.clearIfTerminal(uid: 'owner-1', conversationId: 'conversation-changed', correctionId: changed);
    final originalAfterChangedCleanup = await const PendingConversationCorrectionIdentityStore().acquire(
      uid: 'owner-1',
      conversationId: 'conversation-changed',
      correctionText: 'Original correction.',
      summaryTitle: 'Before',
    );

    expect(changed, isNot(original));
    expect(changedReplay, changed);
    expect(originalReplay, original);
    expect(originalAfterChangedCleanup, original);
  });

  test('identity store is account scoped, bounded, and expires stale fingerprints', () async {
    const boundedStore = PendingConversationCorrectionIdentityStore(maxEntriesPerConversation: 2);
    final started = DateTime.utc(2026, 8, 1);
    final a = await boundedStore.acquire(
      uid: 'owner-1',
      conversationId: 'conversation-cleanup',
      correctionText: 'Payload A',
      now: started,
    );
    await boundedStore.acquire(
      uid: 'owner-1',
      conversationId: 'conversation-cleanup',
      correctionText: 'Payload B',
      now: started.add(const Duration(minutes: 1)),
    );
    final c = await boundedStore.acquire(
      uid: 'owner-1',
      conversationId: 'conversation-cleanup',
      correctionText: 'Payload C',
      now: started.add(const Duration(minutes: 2)),
    );
    final aAfterBoundedCleanup = await boundedStore.acquire(
      uid: 'owner-1',
      conversationId: 'conversation-cleanup',
      correctionText: 'Payload A',
      now: started.add(const Duration(minutes: 3)),
    );
    final otherOwner = await boundedStore.acquire(
      uid: 'owner-2',
      conversationId: 'conversation-cleanup',
      correctionText: 'Payload C',
      now: started.add(const Duration(minutes: 3)),
    );

    const expiringStore = PendingConversationCorrectionIdentityStore(retention: Duration(hours: 1));
    final expiring = await expiringStore.acquire(
      uid: 'owner-1',
      conversationId: 'conversation-expiry',
      correctionText: 'Expiring payload',
      now: started,
    );
    final afterExpiry = await expiringStore.acquire(
      uid: 'owner-1',
      conversationId: 'conversation-expiry',
      correctionText: 'Expiring payload',
      now: started.add(const Duration(hours: 2)),
    );

    expect(aAfterBoundedCleanup, isNot(a));
    expect(otherOwner, isNot(c));
    expect(afterExpiry, isNot(expiring));
  });

  test('stalled submit transport consumes only its supplied end-to-end remaining budget', () async {
    final authority = _ExactAuthority('owner-1');
    Duration? observedTimeout;
    final stopwatch = Stopwatch()..start();
    final submission = await submitConversationCorrection(
      conversationId: 'conversation-stalled-submit',
      correctionId: 'correction-stalled-submit',
      correctionText: 'Correct this private summary.',
      expectedAuthenticatedUid: authority.uid,
      exactAuthority: authority,
      requestTimeout: const Duration(milliseconds: 20),
      debugLog: (_) {},
      transport: ({
        required url,
        required method,
        required body,
        required expectedAuthenticatedUid,
        required exactAuthority,
        required timeout,
      }) async {
        observedTimeout = timeout;
        await Completer<http.Response?>().future;
        return null;
      },
    );

    expect(submission, isNull);
    expect(observedTimeout, const Duration(milliseconds: 20));
    expect(stopwatch.elapsed, lessThan(const Duration(milliseconds: 200)));
  });

  test('typed correction result treats a lost accepted response as uncertain instead of rejected', () async {
    final authority = _ExactAuthority('owner-1');
    final result = await submitConversationCorrectionResult(
      conversationId: 'conversation-accepted-response-lost',
      correctionId: 'correction-accepted-response-lost',
      correctionText: 'Correct this summary.',
      expectedAuthenticatedUid: authority.uid,
      exactAuthority: authority,
      debugLog: (_) {},
      transport: ({
        required url,
        required method,
        required body,
        required expectedAuthenticatedUid,
        required exactAuthority,
        required timeout,
      }) async {
        return http.Response('', 202);
      },
    );

    expect(result.disposition, ConversationCorrectionSubmitDisposition.uncertain);
    expect(result.submission, isNull);
  });

  test('submission fails closed when response correction id differs from submitted id', () async {
    final authority = _ExactAuthority('owner-1');
    final submission = await submitConversationCorrection(
      conversationId: 'conversation-id-mismatch',
      correctionId: 'submitted-correction-id',
      correctionText: 'Correct this.',
      expectedAuthenticatedUid: authority.uid,
      exactAuthority: authority,
      debugLog: (_) {},
      transport: ({
        required url,
        required method,
        required body,
        required expectedAuthenticatedUid,
        required exactAuthority,
        required timeout,
      }) async {
        return http.Response(
          jsonEncode({
            'correction_id': 'different-correction-id',
            'conversation_id': 'conversation-id-mismatch',
            'trace_id': 'correction:conversation-id-mismatch:different-correction-id',
            'status': 'queued',
            'queued': true,
          }),
          202,
        );
      },
    );

    expect(submission, isNull);
  });

  test('receipt polling uses elapsed budget and bounds each request by remaining time', () async {
    final authority = _ExactAuthority('owner-1');
    var elapsed = Duration.zero;
    final requestTimeouts = <Duration>[];
    final waits = <Duration>[];
    var receiptCalls = 0;

    final receipt = await pollConversationCorrectionReceipt(
      conversationId: 'conversation-elapsed-budget',
      correctionId: 'correction-elapsed-budget',
      expectedAuthenticatedUid: authority.uid,
      exactAuthority: authority,
      pollBudget: const Duration(milliseconds: 2500),
      elapsed: () => elapsed,
      wait: (duration) async {
        waits.add(duration);
        elapsed += duration;
      },
      fetchReceipt: ({
        required conversationId,
        required correctionId,
        required expectedAuthenticatedUid,
        required exactAuthority,
        required requestTimeout,
      }) async {
        receiptCalls += 1;
        requestTimeouts.add(requestTimeout);
        elapsed += const Duration(milliseconds: 400);
        return ConversationCorrectionReceipt(
          correctionId: correctionId,
          conversationId: conversationId,
          status: 'canonical_pending',
          before: const ConversationCorrectionSummary(),
          after: const ConversationCorrectionSummary(),
        );
      },
    );

    expect(receipt, isNull);
    expect(receiptCalls, 2);
    expect(requestTimeouts, const [Duration(milliseconds: 2500), Duration(milliseconds: 1100)]);
    expect(waits, const [Duration(seconds: 1), Duration(milliseconds: 700)]);
    expect(elapsed, const Duration(milliseconds: 2500));
  });

  test('finalizing remains pollable while exhausted reconciliation is terminal', () {
    final finalizing = ConversationCorrectionReceipt.fromJson({
      'correction_id': 'corr-1',
      'conversation_id': 'conv-1',
      'status': 'finalizing',
      'before': const <String, dynamic>{},
      'after': const <String, dynamic>{},
    });
    final exhausted = ConversationCorrectionReceipt.fromJson({
      'correction_id': 'corr-1',
      'conversation_id': 'conv-1',
      'status': 'reconciliation_failed',
      'failure_code': 'downstream_effects_exhausted',
      'before': const <String, dynamic>{},
      'after': const <String, dynamic>{},
    });

    expect(finalizing.isPending, isTrue);
    expect(exhausted.isPending, isFalse);
    expect(exhausted.isFailed, isTrue);
  });
}
