import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/models/imessage_enrollment.dart';

void main() {
  test('decodes every authoritative enrollment state', () {
    for (final state in ImessageEnrollmentState.values) {
      final status = ImessageEnrollmentStatus.fromJson(_statusJson(state: state));

      expect(status.state, state);
      expect(status.authorityGeneration, 4);
      expect(status.assignedDestination, '+12025550123');
      expect(status.features.groups, isFalse);
      expect(status.features.attachments, isFalse);
      expect(status.features.caregiverDelivery, isFalse);
    }
  });

  test('requires exact ready reason and text-DM-only capability vector', () {
    final ready = ImessageEnrollmentStatus.fromJson(
      _statusJson(state: ImessageEnrollmentState.ready, textDm: true),
    );
    final disabled = ImessageEnrollmentStatus.fromJson(
      _statusJson(state: ImessageEnrollmentState.ready, textDm: false),
    );
    final contradictoryReason = ImessageEnrollmentStatus.fromJson(
      _statusJson(
        state: ImessageEnrollmentState.ready,
        reason: ImessageEnrollmentReason.transportUnhealthy,
        textDm: true,
      ),
    );
    final groupsEnabled = ImessageEnrollmentStatus.fromJson(
      _statusJson(state: ImessageEnrollmentState.ready, textDm: true, groups: true),
    );
    final attachmentsEnabled = ImessageEnrollmentStatus.fromJson(
      _statusJson(state: ImessageEnrollmentState.ready, textDm: true, attachments: true),
    );
    final caregiverEnabled = ImessageEnrollmentStatus.fromJson(
      _statusJson(state: ImessageEnrollmentState.ready, textDm: true, caregiverDelivery: true),
    );

    expect(ready.isReady, isTrue);
    expect(disabled.isReady, isFalse);
    expect(contradictoryReason.isReady, isFalse);
    expect(groupsEnabled.isReady, isFalse);
    expect(attachmentsEnabled.isReady, isFalse);
    expect(caregiverEnabled.isReady, isFalse);
  });

  test('rejects unknown schema and unknown state instead of guessing', () {
    expect(
      () => ImessageEnrollmentStatus.fromJson({..._statusJson(), 'schema_version': 'legacy'}),
      throwsFormatException,
    );
    expect(
      () => ImessageEnrollmentStatus.fromJson({..._statusJson(), 'state': 'connected'}),
      throwsFormatException,
    );
  });

  test('consent policy and receipt must match the immutable text-DM scope', () {
    final policy = ImessageConsentPolicy.fromJson(_policyJson());
    final receipt = ImessageConsentReceipt.fromJson(_receiptJson());

    expect(policy.recipients, ['Ella self-hosted Hermes and Honcho', 'Photon iMessage transport']);
    expect(receipt.matches(policy, ImessageConsentDecision.granted), isTrue);
    expect(receipt.matches(policy, ImessageConsentDecision.declined), isFalse);
  });

  test('consent parsing fails closed for a different scope or malformed hash', () {
    expect(
      () => ImessageConsentPolicy.fromJson({..._policyJson(), 'policy_version': 'ella-imessage-data-v1'}),
      throwsFormatException,
    );
    expect(
      () => ImessageConsentPolicy.fromJson({..._policyJson(), 'scope_version': 'legacy'}),
      throwsFormatException,
    );
    expect(
      () => ImessageConsentPolicy.fromJson({..._policyJson(), 'processor_set_hash': 'sha256:nope'}),
      throwsFormatException,
    );
  });

  test('decodes the server consent-policy-stale authority state', () {
    final status = ImessageEnrollmentStatus.fromJson({
      ..._statusJson(),
      'reason_code': 'consent_policy_stale',
    });

    expect(status.reason, ImessageEnrollmentReason.consentPolicyStale);
    expect(status.isReady, isFalse);
  });
}

String _hash(String character) => 'sha256:${List.filled(64, character).join()}';

Map<String, dynamic> _policyJson() => {
      'policy_version': ImessageConsentPolicy.supportedPolicyVersion,
      'processor_set_hash': _hash('a'),
      'scope_version': ImessageConsentPolicy.supportedScopeVersion,
      'scope_hash': _hash('b'),
      'recipients': ['Ella self-hosted Hermes and Honcho', 'Photon iMessage transport'],
      'data_classes': [
        'your handset phone number used for iMessage transport registration',
        'the text messages you send to Ella',
        "Ella's text replies",
        'messaging delivery identifiers',
      ],
      'text_dm_only': true,
    };

Map<String, dynamic> _receiptJson() => {
      'schema_version': ImessageConsentReceipt.schema,
      'receipt_id': '00000000-0000-4000-8000-000000000005',
      'decision': 'granted',
      'policy_version': ImessageConsentPolicy.supportedPolicyVersion,
      'processor_set_hash': _hash('a'),
      'scope_version': ImessageConsentPolicy.supportedScopeVersion,
      'scope_hash': _hash('b'),
      'authority_revision': 1,
      'decided_at': '2026-09-18T08:00:00Z',
    };

Map<String, dynamic> _statusJson({
  ImessageEnrollmentState state = ImessageEnrollmentState.notConnected,
  ImessageEnrollmentReason? reason,
  bool textDm = false,
  bool groups = false,
  bool attachments = false,
  bool caregiverDelivery = false,
}) {
  final defaultReason = switch (state) {
    ImessageEnrollmentState.notConnected => ImessageEnrollmentReason.notEnrolled,
    ImessageEnrollmentState.verificationPending => ImessageEnrollmentReason.verificationPending,
    ImessageEnrollmentState.ready => ImessageEnrollmentReason.ready,
    ImessageEnrollmentState.temporarilyUnavailable => ImessageEnrollmentReason.transportUnhealthy,
    ImessageEnrollmentState.revoked => ImessageEnrollmentReason.bindingRevoked,
  };
  return {
    'schema_version': ImessageEnrollmentStatus.schema,
    'state': state.wireValue,
    'reason_code': (reason ?? defaultReason).wireValue,
    'authority_generation': 4,
    'binding_revision': 2,
    'binding_fingerprint': '0123456789abcdef',
    'assigned_destination': '+12025550123',
    'last_verified_at': '2026-09-18T08:00:00Z',
    'verification_expires_at': '2026-09-18T08:05:00Z',
    'support_code': 'ELLA-ABCDEF12',
    'features': {
      'text_dm': textDm,
      'groups': groups,
      'attachments': attachments,
      'caregiver_delivery': caregiverDelivery,
    },
  };
}
