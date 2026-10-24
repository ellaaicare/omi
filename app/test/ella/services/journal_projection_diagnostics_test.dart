import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:omi/env/env.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/ella/services/journal_projection_diagnostics.dart';
import 'package:omi/ella/widgets/journal_projection_diagnostics_panel.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class _Owner implements AccountCommitAuthority {
  bool current = true;
  @override
  String get uid => 'synthetic-owner';
  @override
  bool isCurrent() => current;
  @override
  bool isExactCurrent() => current;
}

class _Env implements EnvFields {
  @override
  String get apiBaseUrl => 'https://api.example.invalid/';
  @override
  dynamic noSuchMethod(Invocation invocation) => null;
}

Map<String, Object?> record() => {
      'id': journalDiagnosticConversationId,
      'uid': 'synthetic-owner',
      'structured': {'title': 'Synthetic title', 'overview': 'Synthetic overview'},
      'active_summary_version_id': '665b8a48-a29b-420e-9832-9b1467dae264',
      'summary_versions': [
        {
          'id': '665b8a48-a29b-420e-9832-9b1467dae264',
          'is_active': true,
          'title': 'Synthetic title',
          'overview': 'Synthetic overview'
        }
      ],
      'token': 'BODY_SECRET',
      'transcript_segments': ['PRIVATE_WORDS'],
    };
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() => Env.init(_Env()));
  tearDown(EllaAccountCommitBarrier.quiesceForAccountTransition);
  test('two normal API paths retain only exact synthetic fields and numeric statuses', () async {
    final paths = <String>[], owner = _Owner();
    final receipt = await readJournalProjectionDiagnostics(owner, transport: (url, authority) async {
      paths.add(url);
      expect(identical(authority, owner), isTrue);
      return http.Response(
          jsonEncode(url.contains('?')
              ? [
                  {
                    'id': 'other-private',
                    'structured': {'title': 'PRIVATE_TITLE'}
                  },
                  record()
                ]
              : record()),
          200,
          headers: {'authorization': 'HEADER_SECRET'});
    });
    expect(paths.length, 2);
    expect(paths.first, contains('v1/conversations?include_discarded=true&limit=50&offset=0&statuses='));
    expect(paths.last, endsWith('v1/conversations/$journalDiagnosticConversationId'));
    final list = receipt['list'] as Map, detail = receipt['detail'] as Map;
    expect(list['http_status'], 200);
    expect(detail['http_status'], 200);
    expect(list['pair_sha256'], detail['pair_sha256']);
    expect(detail['active_summary_matches_structured'], isTrue);
    final encoded = jsonEncode(receipt);
    for (final secret in [
      'BODY_SECRET',
      'HEADER_SECRET',
      'PRIVATE_WORDS',
      'PRIVATE_TITLE',
      'other-private',
      'synthetic-owner'
    ]) {
      expect(encoded, isNot(contains(secret)));
    }
  });
  test('bounded list absence is explicit while detail can still be observed', () async {
    final receipt = await readJournalProjectionDiagnostics(_Owner(),
        transport: (url, _) async => http.Response(jsonEncode(url.contains('?') ? [] : record()), 200));
    expect((receipt['list'] as Map)['result'], 'not_found');
    expect(receipt['list_scope'], 'first_50_include_discarded');
    expect((receipt['detail'] as Map)['result'], 'observed');
  });
  test('non-200 statuses retain no response body', () async {
    final receipt = await readJournalProjectionDiagnostics(_Owner(),
        transport: (url, _) async => http.Response('RAW_SECRET', url.contains('?') ? 401 : 503));
    expect((receipt['list'] as Map)['http_status'], 401);
    expect((receipt['detail'] as Map)['http_status'], 503);
    expect(jsonEncode(receipt), isNot(contains('RAW_SECRET')));
  });
  for (final mode in ['duplicate', 'owner mismatch', 'malformed']) {
    test('$mode responses cannot retain synthetic text as accepted evidence', () async {
      final receipt = await readJournalProjectionDiagnostics(_Owner(), transport: (url, _) async {
        if (mode == 'malformed') return http.Response('not json SECRET', 200);
        if (mode == 'owner mismatch')
          return http.Response(
              jsonEncode(url.contains('?') ? [record()..['uid'] = 'wrong'] : (record()..['uid'] = 'wrong')), 200);
        return http.Response(
            jsonEncode(url.contains('?')
                ? [record(), record()]
                : {
                    'id': 'wrong',
                    'structured': {'title': 'Synthetic title'}
                  }),
            200);
      });
      expect(jsonEncode(receipt), isNot(contains('Synthetic title')));
    });
  }
  test('owner retirement after list prevents detail and any returned receipt', () async {
    final owner = _Owner();
    var calls = 0;
    await expectLater(
        readJournalProjectionDiagnostics(owner, transport: (url, _) async {
          calls++;
          owner.current = false;
          return http.Response(jsonEncode([record()]), 200);
        }),
        throwsStateError);
    expect(calls, 1);
  });
  testWidgets('supported diagnostics button is manual and clears readback on owner retirement', (tester) async {
    final owner = _Owner(), result = Completer<Map<String, Object?>>();
    var calls = 0;
    await tester.pumpWidget(MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
            body: JournalProjectionDiagnosticsPanel(
                authorityProvider: () => owner,
                reader: (_) {
                  calls++;
                  return result.future;
                }))));
    expect(calls, 0);
    await tester.tap(find.byKey(const Key('journal-synthetic-projection-read')));
    await tester.pump();
    expect(calls, 1);
    result.complete({
      'detail': {'http_status': 200, 'title': 'Synthetic title'}
    });
    await tester.pump();
    expect(find.byKey(const Key('journal-synthetic-projection-receipt')), findsOneWidget);
    EllaAccountCommitBarrier.quiesceForAccountTransition();
    await tester.pump();
    expect(find.byKey(const Key('journal-synthetic-projection-receipt')), findsNothing);
    await tester.pumpWidget(const SizedBox.shrink());
  });
}
