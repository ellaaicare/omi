import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/services/ella_entitlement_service.dart';

void main() {
  group('Ella entitlement contract', () {
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
      expect(
        () => EllaEntitlement.fromJson(const {'status': 'mystery', 'quota': {}}),
        throwsFormatException,
      );
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
