import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/services/ella_entitlement_service.dart';

void main() {
  group('Ella entitlement contract', () {
    test('pilot entitlement and stub gates are default-off', () {
      expect(isEllaEntitlementGateEnabled, isFalse);
      expect(isEllaEntitlementStubEnabled, isFalse);
    });

    test('uses the authenticated entitlement and invite paths without client-selected runtime routing', () {
      final entitlementUri = Uri.parse(buildEllaEntitlementUrl('https://api.example.test'));
      final redemptionUri = Uri.parse(buildEllaInviteRedemptionUrl('https://api.example.test/'));

      expect(entitlementUri.path, '/v1/entitlement');
      expect(redemptionUri.path, '/v1/invite/redeem');
      expect(entitlementUri.queryParameters, isEmpty);
      expect(redemptionUri.queryParameters, isEmpty);
      expect(entitlementUri.toString(), isNot(contains('provider')));
      expect(redemptionUri.toString(), isNot(contains('profile')));
    });

    test('parses every typed entitlement status and quota field', () {
      for (final status in EllaEntitlementStatus.values) {
        final entitlement = EllaEntitlement.fromJson({
          'status': status.name,
          'quota': const {
            'daily_used_s': 2160,
            'daily_limit_s': 2700,
            'monthly_used_s': 18000,
            'monthly_limit_s': 43200,
            'max_session_s': 1200,
            'resets_at': '2026-07-27T07:00:00Z',
          },
        });

        expect(entitlement.status, status);
        expect(entitlement.quota.dailyFraction, 0.8);
        expect(entitlement.quota.isSoftWarning, isTrue);
        expect(entitlement.quota.voiceRemainingSeconds, 540);
        expect(entitlement.quota.resetsAt, isNotNull);
      }
    });

    test('rejects unknown entitlement status instead of guessing a user state', () {
      expect(() => EllaEntitlement.fromJson(const {'status': 'mystery', 'quota': {}}), throwsFormatException);
    });

    test('parses every typed invite error shape', () {
      const examples = {
        EllaInviteRedemptionError.invalid: {'code': 'invalid'},
        EllaInviteRedemptionError.expired: {
          'detail': {'code': 'expired'},
        },
        EllaInviteRedemptionError.capacity: {
          'error': {'code': 'capacity'},
        },
        EllaInviteRedemptionError.rateLimited: {'error': 'rate_limited'},
      };

      for (final entry in examples.entries) {
        expect(parseInviteRedemptionError(jsonEncode(entry.value)), entry.key);
      }
      expect(parseInviteRedemptionError('{"code":"provider_unavailable"}'), isNull);
    });

    test('preserves bounded retry and safe support metadata without retaining opaque values', () {
      final failure = parseInviteRedemptionFailure(
        jsonEncode({
          'detail': {
            'code': 'rate_limited',
            'retry_after_s': 75,
            'support_code': 'SUP-4F2A',
            'correlation_id': 'corr_123',
          },
        }),
      );

      expect(failure?.reason, EllaInviteRedemptionError.rateLimited);
      expect(failure?.retryAfterSeconds, 75);
      expect(failure?.supportCode, 'SUP-4F2A');
      expect(failure?.correlationId, 'corr_123');

      final unsafe = EllaEntitlement.fromJson(const {
        'status': 'revoked',
        'quota': {},
        'support_code': 'Bearer secret token',
        'correlation_id': 'corr-safe',
      });
      expect(unsafe.supportCode, isEmpty);
      expect(unsafe.correlationId, 'corr-safe');
    });

    test('invited and active are the only entitlement states allowed to provision', () {
      for (final status in EllaEntitlementStatus.values) {
        final entitlement = EllaEntitlement.fromJson({'status': status.name, 'quota': const {}});
        expect(
          entitlement.canProvision,
          status == EllaEntitlementStatus.invited || status == EllaEntitlementStatus.active,
          reason: status.name,
        );
      }
    });

    test('parses only the five policy reasons as policy outcomes', () {
      const codes = {
        'quota_daily': EllaVoicePolicyReason.quotaDaily,
        'quota_monthly': EllaVoicePolicyReason.quotaMonthly,
        'concurrent': EllaVoicePolicyReason.concurrent,
        'suspended': EllaVoicePolicyReason.suspended,
        'session_max': EllaVoicePolicyReason.sessionMax,
      };
      for (final entry in codes.entries) {
        expect(parseEllaVoicePolicyReason(entry.key), entry.value);
      }
      expect(parseEllaVoicePolicyReason('provider_unavailable'), isNull);
      expect(parseEllaVoicePolicyReason('websocket_closed'), isNull);
    });
  });
}
