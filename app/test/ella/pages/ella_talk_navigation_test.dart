import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/demo/ella_access_demo_fixtures.dart';
import 'package:omi/ella/pages/ella_voice_chat_page.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/providers/ella_entitlement_provider.dart';
import 'package:omi/providers/home_provider.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  for (final initialIndex in [0, 2]) {
    testWidgets('production Talk mount and repeated navigation stay idle from tab $initialIndex', (tester) async {
      SharedPreferences.setMockInitialValues({});
      await SharedPreferencesUtil.init();
      final home = HomeProvider()..selectedIndex = initialIndex;
      addTearDown(home.dispose);
      final entitlement = EllaEntitlementProvider(
        transport: EllaAccessDemoTransport(fetchResult: EllaAccessDemoFixtures.active),
        authenticatedUidChanges: const Stream<String?>.empty(),
        initialAuthenticatedUid: 'navigation-owner',
      )..entitlement = EllaAccessDemoFixtures.active;
      addTearDown(entitlement.dispose);
      final nativeCalls = <String>[];
      const channels = [
        MethodChannel('plugin.csdcorp.com/speech_to_text'),
        MethodChannel('flutter.baseflow.com/permissions/methods'),
        MethodChannel('com.ellaaicare.ella/audio_route'),
      ];
      for (final channel in channels) {
        tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
          nativeCalls.add('${channel.name}:${call.method}');
          return false;
        });
        addTearDown(() => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(channel, null));
      }

      await tester.pumpWidget(MultiProvider(
        providers: [
          ChangeNotifierProvider<HomeProvider>.value(value: home),
          ChangeNotifierProvider<EllaEntitlementProvider>.value(value: entitlement),
        ],
        child: const MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: EllaVoiceChatPage(),
        ),
      ));
      await tester.pump(const Duration(seconds: 1));
      for (final index in [2, 1, 2, 0, 2, 2]) {
        home.setIndex(index);
        await tester.pump();
        // Includes the old 300ms auto-start window and dependency refreshes.
        await tester.pump(const Duration(seconds: 1));
        expect(find.text('Tap to Talk'), findsOneWidget);
        expect(nativeCalls, isEmpty);
        expect(tester.takeException(), isNull);
      }
      await tester.pumpWidget(const SizedBox.shrink());
    });
  }
}
