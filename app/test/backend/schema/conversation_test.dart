import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';

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
    expect(conversation.isRetryableSummaryFailure, isTrue);
    expect(conversation.toJson()['processing_error'], 'conversation_summary_failed');
    expect(conversation.toJson()['processing_error_at'], '2026-07-20T08:05:00.000Z');
  });

  test('preserves the active summary version used by memory-scoped voice', () {
    final conversation = ServerConversation.fromJson({
      'id': 'memory-voice-conversation',
      'created_at': '2026-07-24T08:00:00Z',
      'structured': {
        'title': 'Garden memory',
        'overview': 'A quiet afternoon.',
        'emoji': '',
        'category': 'other',
        'action_items': [],
        'events': [],
      },
      'transcript_segments': [],
      'apps_results': [],
      'audio_files': [],
      'active_summary_version_id': 'summary-v3',
    });

    expect(conversation.activeSummaryVersionId, 'summary-v3');
    expect(conversation.toJson()['active_summary_version_id'], 'summary-v3');
  });

  test('treats initial and recovery summary failures as retryable without exposing unrelated errors', () {
    for (final error in ['conversation_summary_failed', 'conversation_summary_recovery_failed']) {
      final conversation = ServerConversation(
        id: error,
        createdAt: DateTime.utc(2026, 7, 20),
        structured: Structured('', ''),
        status: ConversationStatus.failed,
        processingError: error,
      );
      expect(conversation.isRetryableSummaryFailure, isTrue);
    }

    final unrelatedFailure = ServerConversation(
      id: 'unrelated',
      createdAt: DateTime.utc(2026, 7, 20),
      structured: Structured('', ''),
      status: ConversationStatus.failed,
      processingError: 'provider.invalid_api_key',
    );
    expect(unrelatedFailure.isRetryableSummaryFailure, isFalse);
  });

  test('keeps a completed generic summary retryable when contextual enrichment failed', () {
    final conversation = ServerConversation.fromJson({
      'id': 'generic-fallback',
      'created_at': '2026-07-20T08:00:00Z',
      'structured': {
        'title': 'Generic title',
        'overview': 'Usable generic summary',
        'emoji': '',
        'category': 'other',
        'action_items': [],
        'events': [],
      },
      'status': 'completed',
      'enrichment_state': {
        'status': 'failed',
        'pending': true,
        'error_code': 'conversation_summary_recovery_failed',
      },
    });

    expect(conversation.isRetryableSummaryFailure, isFalse);
    expect(conversation.isRetryableEnrichmentFailure, isTrue);
    expect(conversation.structured.overview, 'Usable generic summary');
    expect(conversation.toJson()['enrichment_state'], {
      'status': 'failed',
      'pending': true,
      'error_code': 'conversation_summary_recovery_failed',
    });
  });

  test('surfaces canonical and enriched-vector terminal failures without flagging active writes', () {
    final canonicalFailure = ServerConversation(
      id: 'canonical-failure',
      createdAt: DateTime.utc(2026, 7, 20),
      structured: Structured('Generic', 'Usable generic summary'),
      enrichmentState: {'status': 'writeback_pending_canonical', 'canonical_status': 'failed', 'pending': true},
    );
    final vectorFailure = ServerConversation(
      id: 'vector-failure',
      createdAt: DateTime.utc(2026, 7, 20),
      structured: Structured('Enriched', 'Usable enriched summary'),
      processingRetryEnrichmentVectorStatus: 'failed',
    );
    final activeWrite = ServerConversation(
      id: 'active-write',
      createdAt: DateTime.utc(2026, 7, 20),
      structured: Structured('Generic', 'Usable generic summary'),
      enrichmentState: {'status': 'writeback_pending_canonical', 'pending': true},
    );

    expect(canonicalFailure.isRetryableEnrichmentFailure, isTrue);
    expect(vectorFailure.isRetryableEnrichmentFailure, isTrue);
    expect(activeWrite.isRetryableEnrichmentFailure, isFalse);
  });
}
