import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/conversation.dart';

void main() {
  group('ServerConversation internal assessment', () {
    test('parses and serializes sibling internal_assessment payload', () {
      final conversation = ServerConversation.fromJson({
        'id': 'conv-1',
        'created_at': '2026-04-23T12:00:00Z',
        'structured': {
          'title': 'Debug conversation',
          'overview': 'Overview',
          'emoji': '',
          'category': 'other',
          'action_items': [],
          'events': [],
        },
        'transcript_segments': [],
        'apps_results': [],
        'audio_files': [],
        'internal_assessment': {
          'score': 0.93,
          'reasons': ['low_confidence_title'],
        },
      });

      expect(conversation.hasInternalAssessment, isTrue);
      expect(conversation.internalAssessmentDebugText, contains('"score": 0.93'));
      expect(conversation.toJson()['internal_assessment'], {
        'score': 0.93,
        'reasons': ['low_confidence_title'],
      });
    });
  });
}
