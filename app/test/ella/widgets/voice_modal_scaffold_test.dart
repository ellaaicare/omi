import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/widgets/voice_modal_scaffold.dart';
import 'package:omi/l10n/app_localizations.dart';

Widget app({required bool voiceActive, required Future<bool> Function() onEnd}) {
  return MaterialApp(
    localizationsDelegates: AppLocalizations.localizationsDelegates,
    supportedLocales: AppLocalizations.supportedLocales,
    home: Builder(
      builder: (context) => Scaffold(
        body: TextButton(
          key: const ValueKey('open-voice-modal'),
          onPressed: () => showModalBottomSheet<void>(
            context: context,
            isScrollControlled: true,
            isDismissible: false,
            enableDrag: false,
            builder: (_) => FractionallySizedBox(
              heightFactor: 0.94,
              child: VoiceModalScaffold(
                voiceActive: voiceActive,
                onEnd: onEnd,
                title: 'Voice Chat',
                child: const Center(child: Text('Talking about: A seeded memory')),
              ),
            ),
          ),
          child: const Text('Open voice'),
        ),
      ),
    ),
  );
}

void main() {
  testWidgets('active voice blocks back until explicit End and then returns', (tester) async {
    var endCalls = 0;
    await tester.pumpWidget(
      app(
        voiceActive: true,
        onEnd: () async {
          endCalls++;
          return true;
        },
      ),
    );
    await tester.tap(find.byKey(const ValueKey('open-voice-modal')));
    await tester.pumpAndSettle();

    await tester.binding.handlePopRoute();
    await tester.pumpAndSettle();

    expect(find.byKey(const ValueKey('voice-modal-root')), findsOneWidget);
    expect(endCalls, 0);

    await tester.tap(find.byKey(const ValueKey('voice-modal-end')));
    await tester.pumpAndSettle();

    expect(endCalls, 1);
    expect(find.byKey(const ValueKey('voice-modal-root')), findsNothing);
    expect(find.byKey(const ValueKey('open-voice-modal')), findsOneWidget);
  });

  testWidgets('failed End keeps the active voice sheet visible', (tester) async {
    await tester.pumpWidget(app(voiceActive: true, onEnd: () async => false));
    await tester.tap(find.byKey(const ValueKey('open-voice-modal')));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const ValueKey('voice-modal-end')));
    await tester.pumpAndSettle();

    expect(find.byKey(const ValueKey('voice-modal-root')), findsOneWidget);
  });

  testWidgets('inactive voice closes and returns without calling End', (tester) async {
    var endCalls = 0;
    await tester.pumpWidget(
      app(
        voiceActive: false,
        onEnd: () async {
          endCalls++;
          return true;
        },
      ),
    );
    await tester.tap(find.byKey(const ValueKey('open-voice-modal')));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const ValueKey('voice-modal-close')));
    await tester.pumpAndSettle();

    expect(endCalls, 0);
    expect(find.byKey(const ValueKey('voice-modal-root')), findsNothing);
    expect(find.byKey(const ValueKey('open-voice-modal')), findsOneWidget);
  });
}
