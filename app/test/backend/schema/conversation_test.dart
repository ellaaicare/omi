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
        'ella_tags': ['omi', 'family', 'guardian_relevant'],
        'ella_signal': {
          'salience': 'high',
          'memory_promotion': 'candidate',
          'guardian_relevant': true,
        },
      });

      expect(conversation.hasInternalAssessment, isTrue);
      expect(conversation.internalAssessmentDebugText, contains('"score": 0.93'));
      expect(conversation.ellaTags, ['omi', 'family', 'guardian_relevant']);
      expect(conversation.ellaSignal?['salience'], 'high');
      expect(conversation.toJson()['internal_assessment'], {
        'score': 0.93,
        'reasons': ['low_confidence_title'],
      });
      expect(conversation.toJson()['ella_tags'], ['omi', 'family', 'guardian_relevant']);
      expect(conversation.toJson()['ella_signal'], {
        'salience': 'high',
        'memory_promotion': 'candidate',
        'guardian_relevant': true,
      });
    });
  });

  test('parses and serializes retryable processing failure metadata', () {
    final conversation = ServerConversation.fromJson({
      'id': 'failed-conversation',
      'created_at': '2026-07-20T08:00:00Z',
      'structured': {
        'title': '',
        'overview': '',
        'emoji': '',
        'category': 'other',
        'action_items': [],
        'events': [],
      },
      'transcript_segments': [],
      'apps_results': [],
      'audio_files': [],
      'status': 'failed',
      'processing_error': 'conversation_summary_failed',
      'processing_error_at': '2026-07-20T08:05:00Z',
    });

    expect(conversation.status, ConversationStatus.failed);
    expect(conversation.processingError, 'conversation_summary_failed');
    expect(conversation.processingErrorAt, isNotNull);
    expect(conversation.toJson()['processing_error'], 'conversation_summary_failed');
    expect(conversation.toJson()['processing_error_at'], '2026-07-20T08:05:00.000Z');
  });
}
