import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/env/env.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/conversation_detail/widgets.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class _ExactAuthority implements ExactAccountAuthorityVerifier {
  _ExactAuthority(this.uid);

  @override
  final String uid;

  @override
  bool isExactCurrent() => true;
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
  setUpAll(() => Env.init(_TestEnv()));

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

  testWidgets('test_type_correction_202_polls_receipt_and_surfaces_terminal_failure_without_logging_body', (
    tester,
  ) async {
    const conversationId = 'conversation-1';
    const correctionId = 'correction-1';
    const correctionText = 'private correction sentinel';
    const responseOnlySentinel = 'response-body-must-not-be-logged';
    final authority = _ExactAuthority('owner-1');
    final logs = <String>[];
    var receiptCalls = 0;
    var refreshCalls = 0;

    Future<ConversationCorrectionSubmission?> submitter({
      required String conversationId,
      required String correctionText,
      String? summaryTitle,
      String? summaryOverview,
      String? appSummary,
      String? expectedAuthenticatedUid,
      ExactAccountAuthorityVerifier? exactAuthority,
    }) {
      return submitConversationCorrection(
        conversationId: conversationId,
        correctionText: correctionText,
        summaryTitle: summaryTitle,
        summaryOverview: summaryOverview,
        appSummary: appSummary,
        expectedAuthenticatedUid: expectedAuthenticatedUid,
        exactAuthority: exactAuthority,
        debugLog: logs.add,
        transport: ({
          required url,
          required method,
          required body,
          required expectedAuthenticatedUid,
          required exactAuthority,
        }) async {
          expect(method, 'POST');
          expect(expectedAuthenticatedUid, 'owner-1');
          expect(identical(exactAuthority, authority), isTrue);
          expect(jsonDecode(body)['correction_text'], correctionText);
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
    }) {
      return pollConversationCorrectionReceipt(
        conversationId: conversationId,
        correctionId: correctionId,
        expectedAuthenticatedUid: expectedAuthenticatedUid,
        exactAuthority: exactAuthority,
        maxAttempts: 3,
        wait: (_) async {},
        fetchReceipt: ({
          required conversationId,
          required correctionId,
          required expectedAuthenticatedUid,
          required exactAuthority,
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
    expect(
      find.text("Ella can't reach your correction service right now"),
      findsOneWidget,
    );
    expect(find.textContaining('self_hosted_runtime_target_mode_required'), findsNothing);
    expect(find.byType(CorrectSummarySheet), findsOneWidget);
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

  testWidgets('test_type_correction_unknown_terminal_failure_uses_non_english_generic_localization', (
    tester,
  ) async {
    const conversationId = 'conversation-es';
    const correctionId = 'correction-es';
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
              required correctionText,
              summaryTitle,
              summaryOverview,
              appSummary,
              expectedAuthenticatedUid,
              exactAuthority,
            }) async {
              logs.add('submit status=202');
              return ConversationCorrectionSubmission(
                correctionId: correctionId,
                conversationId: conversationId,
                traceId: 'correction:conversation-es:correction-es',
                status: 'queued',
                queued: true,
              );
            },
            receiptPoller: ({
              required conversationId,
              required correctionId,
              required expectedAuthenticatedUid,
              required exactAuthority,
            }) async {
              logs.add('receipt status=200');
              return ConversationCorrectionReceipt(
                correctionId: correctionId,
                conversationId: conversationId,
                status: 'direct_apply_failed',
                before: const ConversationCorrectionSummary(),
                after: const ConversationCorrectionSummary(),
                failureCode: 'unrecognized_backend_failure_private_detail',
              );
            },
          ),
        ),
      ),
    );

    await tester.enterText(find.byKey(const ValueKey('type-correction-input')), correctionText);
    await tester.tap(find.byKey(const ValueKey('type-correction-submit')));
    await tester.pumpAndSettle();

    expect(find.text('Ella no pudo actualizar este recuerdo'), findsOneWidget);
    expect(find.textContaining('unrecognized_backend_failure_private_detail'), findsNothing);
    expect(logs.every((entry) => !entry.contains(correctionText)), isTrue);
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
      }) async {
        receiptCalls += 1;
        return ConversationCorrectionReceipt(
          correctionId: correctionId,
          conversationId: conversationId,
          status: receiptCalls <= 46 ? 'retry_queued' : 'applied',
          before: const ConversationCorrectionSummary(title: 'Before'),
          after: const ConversationCorrectionSummary(title: 'Applied'),
        );
      },
    );

    expect(submissionCalls, 1);
    expect(receiptCalls, 47);
    expect(waited, const Duration(seconds: 46));
    expect(receipt?.isApplied, isTrue);
  });
}
