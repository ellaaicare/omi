import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/services/memory_artwork_api.dart';
import 'package:omi/ella/widgets/memory_artwork_image.dart';
import 'package:omi/l10n/app_localizations.dart';

class _DelayedArtworkApi extends MemoryArtworkApi {
  _DelayedArtworkApi() : super(authorityProvider: () => null);

  final remoteResult = Completer<MemoryArtworkResult>();
  int loadCalls = 0;

  @override
  String cacheKeyForDisplay({
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) =>
      'owner-profile-memory-revision-cache-key';

  @override
  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) {
    loadCalls += 1;
    return remoteResult.future;
  }
}

void main() {
  testWidgets('renders owner-scoped disk artwork before signed URL refresh completes', (tester) async {
    final api = _DelayedArtworkApi();
    final cachedFile = File('assets/images/onboarding-bg-1.webp');
    final requestedKeys = <String>[];
    final conversation = ServerConversation(
      id: 'memory-cached',
      createdAt: DateTime(2026, 8, 25),
      structured: Structured('[Ella] A cached memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(
        status: MemoryArtworkStatus.ready,
        styleVersion: memoryArtworkDefaultStyle,
        enrichmentRevision: 'summary-revision-1',
      ),
    );

    Widget buildArtwork() => MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: SizedBox(
            width: 320,
            height: 180,
            child: MemoryArtworkImage(
              conversation: conversation,
              api: api,
              cachedFileLookup: (cacheKey) async {
                requestedKeys.add(cacheKey);
                return cachedFile;
              },
            ),
          ),
        );

    await tester.pumpWidget(buildArtwork());
    await tester.pump();

    expect(find.byKey(const Key('memory-cached-artwork-memory-cached')), findsOneWidget);
    expect(find.byKey(const Key('memory-artwork-placeholder-memory-cached')), findsNothing);
    expect(api.remoteResult.isCompleted, isFalse);
    expect(requestedKeys, ['owner-profile-memory-revision-cache-key']);

    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pumpWidget(buildArtwork());
    await tester.pump();

    expect(find.byKey(const Key('memory-cached-artwork-memory-cached')), findsOneWidget);
    expect(api.loadCalls, 2, reason: 'route recreation may refresh the signed URL without blanking cached bytes');
    expect(requestedKeys, hasLength(2));
  });
}
