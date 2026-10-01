import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/pages/ella_memories_page.dart';
import 'package:omi/ella/services/memory_artwork_api.dart';
import 'package:omi/ella/widgets/ella_source_indicator.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/conversation_detail/conversation_detail_provider.dart';
import 'package:omi/pages/conversation_detail/widgets.dart';
import 'package:omi/providers/folder_provider.dart';

Map<String, dynamic> _confirmedEnrichment() => {
      'status': 'writeback_applied',
      'pending': false,
      'canonical_status': 'completed',
      'kind': 'hermes_enriched',
      'source': 'hermes_parallel',
      'result_summary_version_id': 'summary-1',
    };

ServerConversation _memory({
  String id = 'source-memory',
  Map<String, dynamic>? enrichment,
  String? version = 'summary-1',
  ConversationStatus status = ConversationStatus.completed,
  bool discarded = false,
  bool deleted = false,
  DateTime? createdAt,
}) =>
    ServerConversation(
      id: id,
      createdAt: createdAt ?? DateTime(2026, 9, 30, 9),
      structured: Structured('Tea in the garden', 'A quiet hour by the window.'),
      enrichmentState: enrichment,
      activeSummaryVersionId: version,
      status: status,
      discarded: discarded,
      deleted: deleted,
    );

class _NoArtworkApi extends MemoryArtworkApi {
  @override
  bool get supportsDayArtworkBatch => false;

  @override
  bool isDisplayAuthorityCurrent() => true;

  @override
  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) async =>
      const MemoryArtworkResult(status: MemoryArtworkResultStatus.unavailable, failureCode: 'memory_artwork_disabled');
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUpAll(() async {
    await (FontLoader('Manrope')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf')))
        .load();
    await (FontLoader('packages/font_awesome_flutter/FontAwesomeSolid')
          ..addFont(rootBundle.load('packages/font_awesome_flutter/lib/fonts/Font-Awesome-7-Free-Solid-900.otf')))
        .load();
    var cache = File(Platform.resolvedExecutable).parent;
    while (!File('${cache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf').existsSync()) {
      cache = cache.parent;
    }
    final bytes = await File('${cache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf').readAsBytes();
    await (FontLoader('MaterialIcons')..addFont(Future.value(ByteData.sublistView(bytes)))).load();
  });

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  Widget buildTestApp(String value) {
    return MaterialApp(
      localizationsDelegates: const [
        AppLocalizations.delegate,
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      supportedLocales: AppLocalizations.supportedLocales,
      home: Scaffold(body: EllaSourceText(value)),
    );
  }

  testWidgets('shows an Ella source indicator only for tagged content', (tester) async {
    await tester.pumpWidget(buildTestApp('🪽 [Ella] Family lunch'));
    expect(find.byIcon(Icons.auto_awesome_rounded), findsOneWidget);
    expect(find.textContaining('[Ella]'), findsNothing);
    expect(find.textContaining('Family lunch'), findsOneWidget);

    await tester.pumpWidget(buildTestApp('Generic summary'));
    expect(find.byIcon(Icons.auto_awesome_rounded), findsNothing);
  });

  test('Hermes attribution requires every current canonical provenance field', () {
    final memory = _memory(enrichment: _confirmedEnrichment());
    final original = memory.toJson();
    expect(hasCurrentHermesSummary(memory), isTrue);
    expect(hasCurrentHermesSummary(ServerConversation.fromJson(original)), isTrue);
    expect(memory.toJson(), original, reason: 'source presentation never mutates the record');
    for (final field in _confirmedEnrichment().keys) {
      final missing = _confirmedEnrichment()..remove(field);
      expect(hasCurrentHermesSummary(_memory(enrichment: missing)), isFalse, reason: 'missing $field');
    }
    for (final replacement in <MapEntry<String, dynamic>>[
      const MapEntry('status', 'writeback_pending_canonical'),
      const MapEntry('status', 'failed'),
      const MapEntry('pending', true),
      const MapEntry('pending', 'false'),
      const MapEntry('pending', 0),
      const MapEntry('canonical_status', 'pending'),
      const MapEntry('canonical_status', 'failed'),
      const MapEntry('kind', 'generic'),
      const MapEntry('kind', 'correction'),
      const MapEntry('source', 'generic'),
      const MapEntry('source', 'hermes'),
      const MapEntry('source', 'hermes_parallel '),
      const MapEntry('result_summary_version_id', 'summary-2'),
      const MapEntry('result_summary_version_id', 1),
    ]) {
      final invalid = _confirmedEnrichment()..[replacement.key] = replacement.value;
      expect(hasCurrentHermesSummary(_memory(enrichment: invalid)), isFalse, reason: '${replacement.key} type/value');
    }
    for (final version in <String?>[null, '', ' ', 'summary-2']) {
      expect(hasCurrentHermesSummary(_memory(enrichment: _confirmedEnrichment(), version: version)), isFalse);
    }
    expect(hasCurrentHermesSummary(_memory()), isFalse);
    expect(hasCurrentHermesSummary(_memory(enrichment: _confirmedEnrichment(), discarded: true)), isFalse);
    expect(hasCurrentHermesSummary(_memory(enrichment: _confirmedEnrichment(), deleted: true)), isFalse);
    for (final status in ConversationStatus.values.where((value) => value != ConversationStatus.completed)) {
      expect(hasCurrentHermesSummary(_memory(enrichment: _confirmedEnrichment(), status: status)), isFalse);
    }
  });

  test('FastAPI list and detail wire fixtures retain only confirmed current Hermes attribution', () {
    // The backend public model test verifies these exact shared response records through FastAPI serialization.
    final fixture = jsonDecode(File('test/ella/widgets/fixtures/hermes_summary_api_response.json').readAsStringSync())
        as Map<String, dynamic>;
    for (final rawCase in fixture['cases'] as List) {
      final testCase = rawCase as Map<String, dynamic>;
      final response = <String, dynamic>{
        ...fixture['base_response'] as Map<String, dynamic>,
        'enrichment_state': testCase['wire_enrichment_state'],
      };
      final memory = ServerConversation.fromJson(response);
      expect(hasCurrentHermesSummary(memory), testCase['expected_hermes'], reason: testCase['case'] as String);
      expect(memory.activeSummaryVersionId, 'summary-1');
      for (final privateField in ['error', 'authority_digest', 'request_fingerprint_input']) {
        expect(memory.enrichmentState?.containsKey(privateField) ?? false, isFalse);
      }
    }
  });

  testWidgets('cached-record refresh removes stale Hermes attribution without text inference', (tester) async {
    Widget app(ServerConversation memory) => MaterialApp(
          theme: ellaThemeData(),
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: Scaffold(body: HermesSummarySource(conversation: memory)),
        );
    final semantics = tester.ensureSemantics();
    await tester.pumpWidget(app(_memory(enrichment: _confirmedEnrichment())));
    expect(find.text('Hermes summary'), findsOneWidget);
    expect(find.bySemanticsLabel("This memory's current summary was enriched by Hermes."), findsOneWidget);
    await tester.pumpWidget(app(_memory(enrichment: _confirmedEnrichment(), version: 'corrected-2')));
    expect(find.text('Hermes summary'), findsNothing);
    await tester.pumpWidget(app(_memory(enrichment: _confirmedEnrichment(), version: 'undo-3')));
    expect(find.text('Hermes summary'), findsNothing);
    final generic = _memory()..structured.title = '[Ella] Hermes summary';
    await tester.pumpWidget(app(generic));
    expect(find.text('Hermes summary'), findsNothing);
    await tester.pumpWidget(app(_memory(id: 'replacement-record')));
    expect(find.text('Hermes summary'), findsNothing);
    semantics.dispose();
  });

  for (final fixture in [(390.0, 1.0, false), (320.0, 3.0, false), (320.0, 3.0, true)]) {
    testWidgets('actual memory row and detail show subtle Hermes source at ${fixture.$1}/${fixture.$2}/${fixture.$3}',
        (tester) async {
      tester.view.physicalSize = Size(fixture.$1, fixture.$2 == 1 ? 844 : 1400);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final now = DateTime.now();
      final memory = _memory(enrichment: _confirmedEnrichment(), createdAt: DateTime(now.year, now.month, now.day, 9));
      final provider = ConversationDetailProvider()
        ..selectedDate = memory.createdAt
        ..setCachedConversation(memory)
        ..titleController = TextEditingController(text: memory.structured.title);
      final folders = FolderProvider();
      addTearDown(provider.dispose);
      addTearDown(folders.dispose);
      final semantics = tester.ensureSemantics();
      var opens = 0;
      await tester.pumpWidget(MultiProvider(
        providers: [
          ChangeNotifierProvider<ConversationDetailProvider>.value(value: provider),
          ChangeNotifierProvider<FolderProvider>.value(value: folders),
        ],
        child: MaterialApp(
          debugShowCheckedModeBanner: false,
          theme: ellaThemeData(),
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: MediaQuery(
            data: MediaQueryData(textScaler: TextScaler.linear(fixture.$2), disableAnimations: true),
            child: Directionality(
              textDirection: fixture.$3 ? TextDirection.rtl : TextDirection.ltr,
              child: Scaffold(
                body: Padding(
                  padding: const EdgeInsets.all(20),
                  child: SingleChildScrollView(
                    child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                      MemoryGalleryCard(
                        conversation: memory,
                        layout: MemoryGalleryLayout.list,
                        artworkApi: _NoArtworkApi(),
                        onOpen: () => opens++,
                      ),
                      const Divider(height: 40),
                      const GetSummaryWidgets(),
                    ]),
                  ),
                ),
              ),
            ),
          ),
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.text('Hermes summary'), findsNWidgets(2));
      expect(
          find.bySemanticsLabel(RegExp("This memory's current summary was enriched by Hermes\\.")), findsNWidgets(2));
      expect(tester.takeException(), isNull);
      await expectLater(find.byType(MaterialApp),
          matchesGoldenFile('goldens/hermes_summary_${fixture.$1.toInt()}_${fixture.$2.toInt()}_${fixture.$3}.png'));
      await tester.tap(find.byType(MemoryGalleryCard));
      expect(opens, 1, reason: 'attribution does not intercept the existing open action');
      provider.setCachedConversation(
          _memory(enrichment: _confirmedEnrichment(), version: 'corrected-2', createdAt: memory.createdAt));
      await tester.pump();
      expect(find.text('Hermes summary'), findsOneWidget, reason: 'detail reads the replacement current record');
      semantics.dispose();
    });
  }
}
