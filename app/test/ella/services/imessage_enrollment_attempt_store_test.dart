import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/models/imessage_enrollment.dart';
import 'package:omi/ella/services/imessage_enrollment_attempt_store.dart';

void main() {
  test('attempt journal round trips replay identities without persisting proof material', () {
    final journal = ImessageEnrollmentAttemptJournal(
      ownerUid: 'owner-a',
      start: ImessagePendingStartAttempt(
        ownerUid: 'owner-a',
        handsetE164: '+12025550123',
        consentRequestId: 'consent-request',
        policy: _policy(),
        appVersion: '1.0.0',
        buildNumber: '900',
        idempotencyKey: 'start-attempt',
        receipt: _receipt(),
      ),
      bindingRevoke: const ImessagePendingBindingRevoke(
        ownerUid: 'owner-a',
        expectedGeneration: 4,
        idempotencyKey: 'binding-revoke',
      ),
      consentRevoke: ImessagePendingConsentRevoke(
        ownerUid: 'owner-a',
        requestId: 'consent-revoke',
        policy: _policy(),
        appVersion: '1.0.0',
        buildNumber: '900',
      ),
    );

    final encoded = jsonEncode(journal.toJson());
    final decoded = ImessageEnrollmentAttemptJournal.fromJson(jsonDecode(encoded) as Map<String, dynamic>);

    expect(decoded.ownerUid, 'owner-a');
    expect(decoded.start?.receipt?.receiptId, '00000000-0000-4000-8000-000000000005');
    expect(decoded.bindingRevoke?.expectedGeneration, 4);
    expect(decoded.consentRevoke?.requestId, 'consent-revoke');
    expect(encoded, isNot(contains('123456')));
    expect(encoded, isNot(contains('proof')));
    expect(encoded, isNot(contains('assigned_destination')));
  });

  test('attempt journal rejects a nested operation owned by another account', () {
    final json = const ImessageEnrollmentAttemptJournal(
      ownerUid: 'owner-a',
      bindingRevoke: ImessagePendingBindingRevoke(
        ownerUid: 'owner-b',
        expectedGeneration: 4,
        idempotencyKey: 'binding-revoke',
      ),
    ).toJson();

    expect(() => ImessageEnrollmentAttemptJournal.fromJson(json), throwsFormatException);
  });

  test('memory store never returns one owner journal to another owner', () async {
    final store = ImessageEnrollmentMemoryAttemptStore();
    await store.write(
      const ImessageEnrollmentAttemptJournal(
        ownerUid: 'owner-a',
        bindingRevoke: ImessagePendingBindingRevoke(
          ownerUid: 'owner-a',
          expectedGeneration: 4,
          idempotencyKey: 'binding-revoke',
        ),
      ),
    );

    expect(await store.read('owner-b'), isNull);
    expect((await store.read('owner-a'))?.bindingRevoke?.idempotencyKey, 'binding-revoke');
  });
}

String _hash(String character) => 'sha256:${List.filled(64, character).join()}';

ImessageConsentPolicy _policy() => ImessageConsentPolicy(
      policyVersion: ImessageConsentPolicy.supportedPolicyVersion,
      processorSetHash: _hash('a'),
      scopeVersion: ImessageConsentPolicy.supportedScopeVersion,
      scopeHash: _hash('b'),
      recipients: const ['Ella self-hosted Hermes and Honcho', 'Photon iMessage transport'],
      dataClasses: const ['handset', 'messages'],
      textDmOnly: true,
    );

ImessageConsentReceipt _receipt() => ImessageConsentReceipt(
      schemaVersion: ImessageConsentReceipt.schema,
      receiptId: '00000000-0000-4000-8000-000000000005',
      decision: ImessageConsentDecision.granted,
      policyVersion: ImessageConsentPolicy.supportedPolicyVersion,
      processorSetHash: _hash('a'),
      scopeVersion: ImessageConsentPolicy.supportedScopeVersion,
      scopeHash: _hash('b'),
      authorityRevision: 1,
      decidedAt: DateTime.utc(2026, 9, 18, 8),
    );
