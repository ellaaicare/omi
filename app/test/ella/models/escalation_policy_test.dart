import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/models/escalation_policy.dart';

void main() {
  group('ChannelStatus availability', () {
    test('fails closed for legacy iMessage enabled by phone presence', () {
      final channel = ChannelStatus.fromJson({
        'channel': 'imessage',
        'enabled': true,
        'reason': 'Phone number on file',
      });

      expect(channel.enabled, isTrue);
      expect(channel.isCurrentlyAvailable, isFalse);
    });

    test('preserves enabled state for legacy email fallback', () {
      final channel = ChannelStatus.fromJson({'channel': 'email', 'enabled': true, 'reason': 'Email on file'});

      expect(channel.isCurrentlyAvailable, isTrue);
    });
  });
}
