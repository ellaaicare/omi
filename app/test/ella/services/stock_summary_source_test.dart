import 'package:flutter/material.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/widgets/ella_source_indicator.dart';
import 'journal_preview_refresh_test.dart' show record;

ServerConversation stock({String source = 'omi', String kind = 'generated'}) {
  final body = record('Fictional stock summary', 'Saved fictional overview').toJson()
    ..['status'] = 'completed'
    ..['active_summary_version_id'] = 'stock-version'
    ..['summary_versions'] = [
      {
        'id': 'stock-version',
        'is_active': true,
        'kind': kind,
        'source': source,
        'title': 'Fictional stock summary',
        'overview': 'Saved fictional overview',
      }
    ];
  return ServerConversation.fromJson(body);
}

void main() {
  test('exact current stock row is attributed without connector or Hermes promotion', () {
    final row = stock();
    expect(hasCurrentStockSummary(row), true);
    expect(hasCurrentConnectorSummary(row), false);
    expect(hasCurrentHermesSummary(row), false);
  });
  for (final change in [
    'wrong-version',
    'wrong-body',
    'wrong-source',
    'duplicate-active',
    'in-progress',
    'deleted',
    'discarded'
  ]) {
    test('stock attribution refuses $change', () {
      final body = stock().toJson();
      switch (change) {
        case 'wrong-version':
          body['active_summary_version_id'] = 'other';
          break;
        case 'wrong-body':
          body['structured']['overview'] = 'different';
          break;
        case 'wrong-source':
          body['summary_versions'][0]['source'] = 'unknown';
          break;
        case 'duplicate-active':
          body['summary_versions'].add(Map<String, dynamic>.from(body['summary_versions'][0])..['id'] = 'other');
          break;
        case 'in-progress':
          body['status'] = 'in_progress';
          break;
        case 'deleted':
          body['deleted'] = true;
          break;
        case 'discarded':
          body['discarded'] = true;
          break;
      }
      expect(hasCurrentStockSummary(ServerConversation.fromJson(body)), false);
    });
  }
  for (final dark in [false, true]) {
    for (final scale in [1.0, 3.5]) {
      testWidgets('stock selected source and version are visible without inferred enrichment $dark $scale',
          (tester) async {
        await tester.pumpWidget(MaterialApp(
            theme: dark ? ellaDarkThemeData() : ellaThemeData(),
            home: MediaQuery(
                data: MediaQueryData(textScaler: TextScaler.linear(scale)),
                child: Scaffold(
                    body:
                        SingleChildScrollView(child: ActiveSummarySource(conversation: stock(), showVersion: true))))));
        expect(find.text('Conversation summary'), findsOneWidget);
        expect(find.text('Saved version: stock-version'), findsOneWidget);
        expect(find.text('Connector summary'), findsNothing);
        expect(tester.takeException(), isNull);
      });
    }
  }
}
