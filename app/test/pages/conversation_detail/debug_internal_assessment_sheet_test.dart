import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/conversation_detail/widgets/internal_assessment_debug_sheet.dart';

void main() {
  ServerConversation buildConversation({Object? internalAssessment}) {
    return ServerConversation(
      id: 'conv-debug',
      createdAt: DateTime.parse('2026-04-23T12:00:00Z').toLocal(),
      structured: Structured('Title', 'Overview'),
      internalAssessment: internalAssessment,
    );
  }

  group('DebugInternalAssessmentSheet', () {
    testWidgets('renders formatted internal assessment payload', (tester) async {
      final conversation = buildConversation(
        internalAssessment: {
          'score': 0.42,
          'notes': ['needs_review'],
        },
      );

      await tester.pumpWidget(
        MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: Scaffold(body: DebugInternalAssessmentSheet(conversation: conversation)),
        ),
      );

      expect(find.text('Developer'), findsOneWidget);
      expect(find.textContaining('"score": 0.42'), findsOneWidget);
      expect(find.textContaining('"needs_review"'), findsOneWidget);
    });

    test('isSupported only enables the debug UI for iOS debug builds', () {
      final conversation = buildConversation(internalAssessment: {'score': 1});

      expect(DebugInternalAssessmentSheet.isSupported(conversation, isDebugMode: true, isIOS: true), isTrue);
      expect(DebugInternalAssessmentSheet.isSupported(conversation, isDebugMode: false, isIOS: true), isFalse);
      expect(DebugInternalAssessmentSheet.isSupported(conversation, isDebugMode: true, isIOS: false), isFalse);
    });
  });
}
