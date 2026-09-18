import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:omi/ella/models/imessage_enrollment.dart';
import 'package:omi/ella/services/imessage_enrollment_api.dart';

void main() {
  test('uses exact owner-scoped routes without caller authority selectors', () async {
    final calls = <_Call>[];
    final api = ImessageEnrollmentApi(
      baseUrl: 'https://api.example.test/',
      transport: ({required url, required headers, required body, required method, timeout, retries}) async {
        calls.add(_Call(url: url, method: method, body: body));
        if (url.endsWith('/start')) {
          return http.Response(
            jsonEncode({
              'status': _statusJson(state: ImessageEnrollmentState.verificationPending),
              'proof': {'code': '123456', 'expires_at': '2026-09-18T08:05:00Z'},
            }),
            201,
          );
        }
        if (url.endsWith('/revoke')) {
          return http.Response(jsonEncode(_statusJson(state: ImessageEnrollmentState.revoked)), 200);
        }
        return http.Response(jsonEncode(_statusJson()), 200);
      },
    );

    await api.fetchStatus();
    await api.start(
      handsetE164: '+12025550123',
      consentReceiptId: '00000000-0000-4000-8000-000000000001',
      idempotencyKey: '00000000-0000-4000-8000-000000000002',
    );
    await api.revoke(expectedGeneration: 4, idempotencyKey: '00000000-0000-4000-8000-000000000003');

    expect(calls.map((call) => call.method), ['GET', 'POST', 'POST']);
    expect(calls[0].url, endsWith('v1/ella/imessage/enrollment'));
    expect(calls[1].url, endsWith('v1/ella/imessage/enrollment/start'));
    expect(calls[2].url, endsWith('v1/ella/imessage/enrollment/revoke'));
    expect(jsonDecode(calls[1].body), {
      'handset_e164': '+12025550123',
      'consent_receipt_id': '00000000-0000-4000-8000-000000000001',
      'idempotency_key': '00000000-0000-4000-8000-000000000002',
    });
    expect(jsonDecode(calls[2].body), {
      'expected_generation': 4,
      'idempotency_key': '00000000-0000-4000-8000-000000000003',
    });
    final requestKeys = calls
        .where((call) => call.body.isNotEmpty)
        .expand((call) => (jsonDecode(call.body) as Map<String, dynamic>).keys)
        .toSet();
    expect(requestKeys.intersection({'uid', 'profile_id', 'runtime_id', 'provider_route'}), isEmpty);
  });

  test('records the exact dedicated consent policy without caller authority selectors', () async {
    final calls = <_Call>[];
    final api = ImessageEnrollmentApi(
      baseUrl: 'https://api.example.test/',
      transport: ({required url, required headers, required body, required method, timeout, retries}) async {
        calls.add(_Call(url: url, method: method, body: body));
        if (url.endsWith('/policy')) return http.Response(jsonEncode(_policyJson()), 200);
        return http.Response(jsonEncode(_receiptJson()), 200);
      },
    );

    final policy = await api.fetchConsentPolicy();
    final receipt = await api.recordConsent(
      decision: ImessageConsentDecision.granted,
      policy: policy,
      requestId: '00000000-0000-4000-8000-000000000004',
      appVersion: '1.0.0',
      buildNumber: '900',
    );

    expect(calls.map((call) => call.method), ['GET', 'POST']);
    expect(calls[0].url, endsWith('v1/ella/imessage/consent/policy'));
    expect(calls[1].url, endsWith('v1/ella/imessage/consent'));
    expect(jsonDecode(calls[1].body), {
      'decision': 'granted',
      'policy_version': ImessageConsentPolicy.supportedPolicyVersion,
      'processor_set_hash': _hash('a'),
      'scope_version': ImessageConsentPolicy.supportedScopeVersion,
      'scope_hash': _hash('b'),
      'request_id': '00000000-0000-4000-8000-000000000004',
      'app_version': '1.0.0',
      'build_number': '900',
    });
    expect(receipt.matches(policy, ImessageConsentDecision.granted), isTrue);
    final requestKeys = (jsonDecode(calls[1].body) as Map<String, dynamic>).keys.toSet();
    expect(requestKeys.intersection({'uid', 'profile_id', 'runtime_id', 'provider_route'}), isEmpty);
  });

  test('surfaces typed unavailable error and redacted support code', () async {
    final api = ImessageEnrollmentApi(
      baseUrl: 'https://api.example.test/',
      transport: ({required url, required headers, required body, required method, timeout, retries}) async {
        return http.Response(
          jsonEncode({
            'detail': {'code': 'transport_unhealthy', 'support_code': 'ELLA-ABCDEF12'},
          }),
          503,
        );
      },
    );

    await expectLater(
      api.fetchStatus(),
      throwsA(
        isA<ImessageEnrollmentFailure>()
            .having((error) => error.kind, 'kind', ImessageEnrollmentFailureKind.unavailable)
            .having((error) => error.code, 'code', 'transport_unhealthy')
            .having((error) => error.supportCode, 'supportCode', 'ELLA-ABCDEF12'),
      ),
    );
  });

  test('rejects malformed success instead of presenting plausible readiness', () async {
    final api = ImessageEnrollmentApi(
      baseUrl: 'https://api.example.test/',
      transport: ({required url, required headers, required body, required method, timeout, retries}) async {
        return http.Response('{"state":"ready"}', 200);
      },
    );

    await expectLater(
      api.fetchStatus(),
      throwsA(
        isA<ImessageEnrollmentFailure>().having(
          (error) => error.kind,
          'kind',
          ImessageEnrollmentFailureKind.malformedResponse,
        ),
      ),
    );
  });
}

String _hash(String character) => 'sha256:${List.filled(64, character).join()}';

Map<String, dynamic> _policyJson() => {
      'policy_version': ImessageConsentPolicy.supportedPolicyVersion,
      'processor_set_hash': _hash('a'),
      'scope_version': ImessageConsentPolicy.supportedScopeVersion,
      'scope_hash': _hash('b'),
      'recipients': ['Photon', 'Ella self-hosted Hermes', 'OpenAI'],
      'data_classes': ['phone number', 'message text', 'selected memory context'],
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

class _Call {
  const _Call({required this.url, required this.method, required this.body});

  final String url;
  final String method;
  final String body;
}

Map<String, dynamic> _statusJson({ImessageEnrollmentState state = ImessageEnrollmentState.notConnected}) {
  final reason = switch (state) {
    ImessageEnrollmentState.notConnected => ImessageEnrollmentReason.notEnrolled,
    ImessageEnrollmentState.verificationPending => ImessageEnrollmentReason.verificationPending,
    ImessageEnrollmentState.ready => ImessageEnrollmentReason.ready,
    ImessageEnrollmentState.temporarilyUnavailable => ImessageEnrollmentReason.transportUnhealthy,
    ImessageEnrollmentState.revoked => ImessageEnrollmentReason.bindingRevoked,
  };
  return {
    'schema_version': ImessageEnrollmentStatus.schema,
    'state': state.wireValue,
    'reason_code': reason.wireValue,
    'authority_generation': 4,
    'binding_revision': null,
    'binding_fingerprint': null,
    'assigned_destination': state == ImessageEnrollmentState.verificationPending ? '+12025550123' : null,
    'last_verified_at': null,
    'verification_expires_at': null,
    'support_code': null,
    'features': {
      'text_dm': state == ImessageEnrollmentState.ready,
      'groups': false,
      'attachments': false,
      'caregiver_delivery': false,
    },
  };
}
