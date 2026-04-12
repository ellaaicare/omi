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
  });
}
