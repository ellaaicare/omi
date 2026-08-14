import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/home/today_page.dart';
import 'package:omi/utils/enums.dart';

void main() {
  testWidgets('phone-only status does not imply an unpaired necklace is present', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: TodayStatusStrip(
            hasNecklace: false,
            necklaceConnected: false,
            necklaceConnecting: false,
            batteryLevel: -1,
            deviceType: DeviceType.omi,
            headsetConnected: false,
            audioOutputName: 'iPhone',
            onTap: () {},
          ),
        ),
      ),
    );

    expect(find.text('Phone only'), findsOneWidget);
    expect(find.text('Off'), findsNothing);
    expect(find.byIcon(Icons.phone_iphone_rounded), findsOneWidget);
  });

  testWidgets('phone-only recording keeps the moment heading and explicit start copy', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: TodayRecordMomentControl(
            homeCaptureOwned: false,
            homeCaptureUsesNecklace: false,
            starting: false,
            necklaceConnected: false,
            necklaceConnecting: false,
            recordingState: RecordingState.stop,
            necklaceContinuouslyRecording: false,
            onViewTranscript: () {},
            onTap: () {},
          ),
        ),
      ),
    );

    expect(find.text('Record a moment'), findsOneWidget);
    expect(find.text('Start Recording'), findsOneWidget);
    expect(find.text('Records on this iPhone'), findsOneWidget);
    expect(find.text('Records with your necklace'), findsNothing);
  });
}
