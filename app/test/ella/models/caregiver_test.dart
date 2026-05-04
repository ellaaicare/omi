import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/models/caregiver.dart';

void main() {
  group('Caregiver', () {
    test('normalizes backend enum status casing', () {
      final caregiver = Caregiver.fromJson({
        'id': 'caregiver-1',
        'name': 'Emily',
        'relationship': 'friend',
        'status': 'ACTIVE',
        'accepted_at': '2026-04-12T18:12:03.120Z',
      });

      expect(caregiver.status, 'active');
      expect(caregiver.isActive, isTrue);
      expect(caregiver.isInvited, isFalse);
      expect(caregiver.joinedAt, DateTime.parse('2026-04-12T18:12:03.120Z'));
    });

    test('reads daily summary permission from backend permissions', () {
      final caregiver = Caregiver.fromJson({
        'id': 'caregiver-1',
        'name': 'Emily',
        'relationship': 'friend',
        'status': 'INVITED',
        'permissions': {'receive_daily_summary': false},
      });

      expect(caregiver.status, 'invited');
      expect(caregiver.receiveDailySummary, isFalse);
    });

    test('isExpired is true when invited and invite_expires_at is in the past', () {
      final caregiver = Caregiver.fromJson({
        'id': 'cg-exp',
        'name': 'Bob',
        'relationship': 'son',
        'status': 'INVITED',
        'invite_expires_at': '2020-01-01T00:00:00.000Z',
      });

      expect(caregiver.isInvited, isTrue);
      expect(caregiver.isExpired, isTrue);
      expect(caregiver.isActive, isFalse);
    });

    test('isExpired is false when invited and invite_expires_at is in the future', () {
      final future = DateTime.now().add(const Duration(days: 7)).toIso8601String();
      final caregiver = Caregiver.fromJson({
        'id': 'cg-pending',
        'name': 'Alice',
        'relationship': 'daughter',
        'status': 'INVITED',
        'invite_expires_at': future,
      });

      expect(caregiver.isInvited, isTrue);
      expect(caregiver.isExpired, isFalse);
    });

    test('isExpired is false when no invite_expires_at', () {
      final caregiver = Caregiver.fromJson({
        'id': 'cg-no-exp',
        'name': 'Carol',
        'relationship': 'spouse',
        'status': 'INVITED',
      });

      expect(caregiver.isInvited, isTrue);
      expect(caregiver.isExpired, isFalse);
    });

    test('isExpired is false for active caregiver even with past expiry', () {
      final caregiver = Caregiver.fromJson({
        'id': 'cg-active',
        'name': 'Dave',
        'relationship': 'friend',
        'status': 'ACTIVE',
        'invite_expires_at': '2020-01-01T00:00:00.000Z',
      });

      expect(caregiver.isActive, isTrue);
      expect(caregiver.isExpired, isFalse);
    });

    test('invite response preserves failed email delivery recovery fields', () {
      final response = InviteResponse.fromJson({
        'caregiver_id': 'cg-1',
        'invite_id': 'inv-1',
        'invite_code': '123456',
        'status': 'created',
        'email_sent': false,
        'failure_reason': 'gmail_auth',
      });

      expect(response.caregiverId, 'cg-1');
      expect(response.inviteCode, '123456');
      expect(response.emailSent, isFalse);
      expect(response.emailDeliveryFailed, isTrue);
      expect(response.hasInviteRecovery, isTrue);
      expect(response.failureReason, 'gmail_auth');
    });

    test('invite response accepts string email_sent values', () {
      final response = InviteResponse.fromJson({
        'caregiver_id': 'cg-1',
        'invite_code': '123456',
        'email_sent': 'false',
      });

      expect(response.emailSent, isFalse);
      expect(response.emailDeliveryFailed, isTrue);
    });
  });
}
