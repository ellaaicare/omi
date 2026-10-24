import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/effective_voice_settings_diagnostics.dart';
import 'package:omi/ella/widgets/effective_voice_diagnostics_panel.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class _Authority implements ExactAccountAuthorityVerifier {
  @override
  String get uid => 'private-panel-owner';
  bool current = true;
  @override
  bool isExactCurrent() => current;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  Widget host(Widget panel) => MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(body: SingleChildScrollView(child: panel)),
      );

  if (!isEffectiveVoiceDiagnosticsEnabled) {
    testWidgets('normal release mounts no diagnostic action or auth listener', (tester) async {
      await tester.pumpWidget(host(const EffectiveVoiceDiagnosticsPanel()));
      await tester.pump(const Duration(seconds: 2));
      expect(find.byKey(const Key('effective-voice-diagnostics-read')), findsNothing);
      expect(tester.takeException(), isNull);
    });
    return;
  }

  for (final dark in [false, true]) {
    for (final scale in [1.0, 3.125]) {
      testWidgets('Iris diagnostic label contrasts and wraps dark=$dark scale=$scale', (tester) async {
        await tester.binding.setSurfaceSize(const Size(320, 900));
        addTearDown(() => tester.binding.setSurfaceSize(null));
        final theme = dark ? ellaDarkThemeData() : ellaThemeData();
        await tester.pumpWidget(MaterialApp(
          theme: theme,
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          builder: (context, child) =>
              MediaQuery(data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(scale)), child: child!),
          home: Scaffold(
              backgroundColor: theme.scaffoldBackgroundColor,
              body:
                  const SingleChildScrollView(child: EffectiveVoiceDiagnosticsPanel(authorityChanges: Stream.empty()))),
        ));
        final button = tester.widget<OutlinedButton>(find.byKey(const Key('effective-voice-diagnostics-read')));
        final fg = button.style!.foregroundColor!.resolve({})!;
        final bg = EllaPalette(dark).bgPrimary;
        final l1 = fg.computeLuminance(), l2 = bg.computeLuminance();
        final contrast = (l1 > l2 ? (l1 + .05) / (l2 + .05) : (l2 + .05) / (l1 + .05));
        expect(contrast, greaterThanOrEqualTo(4.5));
        expect(tester.getSize(find.byType(OutlinedButton)).height, greaterThanOrEqualTo(48));
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox.shrink());
      });
    }
  }

  for (final retireDuringRead in [false, true]) {
    testWidgets('manual read and account retirement discard stale data duringRead=$retireDuringRead', (tester) async {
      String? clipboard;
      tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(SystemChannels.platform, (call) async {
        if (call.method == 'Clipboard.setData') clipboard = (call.arguments as Map)['text'] as String;
        return null;
      });
      addTearDown(() => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(SystemChannels.platform, null));
      final owner = _Authority();
      final events = StreamController<void>.broadcast(sync: true);
      final response = Completer<http.Response?>();
      var requests = 0;
      final api = EffectiveVoiceSettingsDiagnostics(
        baseUrl: 'https://read-only.invalid/',
        authorityProvider: () => owner,
        transport: ({required url, required expectedAuthenticatedUid, required exactAuthority}) {
          requests++;
          return response.future;
        },
      );
      await tester.pumpWidget(host(EffectiveVoiceDiagnosticsPanel(
        api: api,
        authorityChanges: events.stream,
        buildLabel: () async => '1.0.572+909',
      )));
      await tester.pump(const Duration(seconds: 2));
      expect(requests, 0);
      await tester.tap(find.byKey(const Key('effective-voice-diagnostics-read')));
      await tester.pump();
      expect(requests, 1);
      if (retireDuringRead) {
        owner.current = false;
        events.add(null);
        await tester.pump();
      }
      response.complete(http.Response(
          jsonEncode({
            'uid': owner.uid,
            'effective_voice_settings': {
              'voice_mode': 'grok-voice',
              'one_shot_tts_provider': 'xai-tts',
              'one_shot_tts_candidates': ['xai-tts', 'kokoro'],
              'provider_type': 'v2v',
              'fallback_used': false,
            },
          }),
          200));
      await tester.pump();
      await tester.pump();
      if (retireDuringRead) {
        expect(find.byKey(const Key('effective-voice-diagnostics-result')), findsNothing);
        expect(find.byKey(const Key('effective-voice-copy-support')), findsNothing);
      } else {
        final text = tester.widget<SelectableText>(find.byKey(const Key('effective-voice-diagnostics-result'))).data!;
        expect(text, contains('1.0.572+909'));
        expect(text, contains('grok-voice'));
        expect(text, isNot(contains(owner.uid)));
        await tester.ensureVisible(find.byKey(const Key('effective-voice-copy-support')));
        await tester.tap(find.byKey(const Key('effective-voice-copy-support')));
        await tester.pump();
        expect(clipboard, isNotNull);
        final copied = jsonDecode(clipboard!) as Map;
        expect(copied['app_build'], '1.0.572+909');
        expect(copied.containsKey('account_sha256'), isFalse);
        expect(clipboard, isNot(contains(owner.uid)));
        expect(requests, 1);
        owner.current = false;
        // Same-UID consent/provisioning retirement emits no auth event.
        await tester.pump(const Duration(milliseconds: 300));
        expect(find.byKey(const Key('effective-voice-diagnostics-result')), findsNothing);
        expect(find.byKey(const Key('effective-voice-copy-support')), findsNothing);
      }
      await tester.pumpWidget(const SizedBox.shrink());
      await events.close();
      expect(tester.takeException(), isNull);
    });
  }
}
