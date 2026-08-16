import 'dart:async';
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
    const correctionText = 'private correction sentinel';
    const responseOnlySentinel = 'response-body-must-not-be-logged';
    final authority = _ExactAuthority('owner-1');
    final logs = <String>[];
    var receiptCalls = 0;
    var refreshCalls = 0;
    final submittedCorrectionIds = <String>[];

    Future<ConversationCorrectionSubmission?> submitter({
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
      return submitConversationCorrection(
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
    expect(submittedCorrectionIds, hasLength(1));
    expect(
      find.text("Ella can't reach your correction service right now"),
      findsOneWidget,
    );
    await tester.tap(find.byKey(const ValueKey('type-correction-submit')));
    await tester.pumpAndSettle();
    expect(submittedCorrectionIds, hasLength(2));
    expect(submittedCorrectionIds.toSet(), hasLength(2));
    expect(receiptCalls, 3);
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
      'submitConversationCorrection: status=202',
      'getConversationCorrectionReceipt: status=200',
    ]);
  });

  testWidgets('test_type_correction_unknown_terminal_failure_uses_non_english_generic_localization', (
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
              required pollBudget,
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
    await store.clearIfTerminal(
      uid: 'owner-1',
      conversationId: 'conversation-changed',
      correctionId: changed,
    );
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
}
