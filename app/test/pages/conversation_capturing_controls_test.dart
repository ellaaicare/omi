import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/transcript_segment.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/conversation_capturing/page.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/services/services.dart';
import 'package:omi/utils/enums.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUpAll(() async {
    try {
      await ServiceManager.init();
    } catch (_) {
      // Process-global test services may already be initialized.
    }
  });

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    SharedPreferencesUtil().showSummarizeConfirmation = false;
  });

  testWidgets('header distinguishes startup, transcript recovery, recording, and durable error', (tester) async {
    final capture = _FakeCaptureProvider(RecordingState.initialising, transcriptReady: false);
    await _pumpCapturePage(tester, capture);

    expect(_captureStatus(tester), 'Initializing...');

    capture.setCaptureState(RecordingState.record, transcriptReady: false);
    await tester.pump();
    expect(_captureStatus(tester), 'Recording Active · Reconnecting...');

    capture.setCaptureState(RecordingState.record, transcriptReady: true);
    await tester.pump();
    expect(_captureStatus(tester), 'Recording Active');

    capture.setCaptureState(RecordingState.error, transcriptReady: false);
    await tester.pump();
    expect(_captureStatus(tester), 'Error');
  });

  testWidgets('empty active capture keeps Stop Recording visible before transcript segments', (tester) async {
    final capture = _FakeCaptureProvider(RecordingState.stop, transcriptReady: false);
    await _pumpCapturePage(tester, capture);

    expect(find.byKey(const Key('conversation-process-now')), findsNothing);

    capture.setCaptureState(RecordingState.stop, transcriptReady: false, phoneOwnsMobileAudio: true);
    await tester.pump();
    expect(find.byKey(const Key('conversation-process-now')), findsOneWidget);
    expect(find.text('Stop Recording'), findsOneWidget);

    capture.setCaptureState(RecordingState.stop, transcriptReady: false);
    await tester.pump();
    expect(find.byKey(const Key('conversation-process-now')), findsNothing);

    capture.setCaptureState(RecordingState.deviceRecord, transcriptReady: true);
    await tester.pump();
    expect(find.byKey(const Key('conversation-process-now')), findsOneWidget);
    expect(find.text('Stop Recording'), findsOneWidget);
  });

  testWidgets('empty phone action stops phone transport once and exits after owner finalization', (tester) async {
    final capture = _FakeCaptureProvider(
      RecordingState.stop,
      transcriptReady: false,
      phoneOwnsMobileAudio: true,
    );
    final ownerGate = Completer<bool>();
    var ownerCalls = 0;
    await _pumpCapturePage(
      tester,
      capture,
      onProcessNow: () {
        ownerCalls++;
        expect(capture.phoneStops, 1, reason: 'physical capture must stop before final transcript processing');
        return ownerGate.future;
      },
    );

    final stop = find.byKey(const Key('conversation-process-now'));
    await tester.tap(stop);
    await tester.tap(stop);
    await tester.pump();

    expect(capture.phoneStops, 1);
    expect(capture.deviceStops, 0);
    expect(ownerCalls, 1);
    expect(find.byKey(const Key('conversation-process-now-progress')), findsOneWidget);
    expect(find.byType(ConversationCapturingPage), findsOneWidget);

    ownerGate.complete(false);
    await tester.pumpAndSettle();

    expect(ownerCalls, 1);
    expect(find.byType(ConversationCapturingPage), findsNothing);
  });

  testWidgets('content Process Now remains serialized until the owner final transcript settles', (tester) async {
    final capture = _FakeCaptureProvider(RecordingState.record, transcriptReady: true)
      ..segments = [_transcriptSegment()];
    final finalTranscriptGate = Completer<void>();
    var ownerCalls = 0;
    await _pumpCapturePage(
      tester,
      capture,
      onProcessNow: () async {
        ownerCalls++;
        await capture.stopStreamRecording();
        await finalTranscriptGate.future;
        return true;
      },
    );

    expect(find.text('Process Now'), findsOneWidget);
    final processNow = find.byKey(const Key('conversation-process-now'));
    await tester.tap(processNow);
    await tester.tap(processNow);
    await tester.pump();

    expect(ownerCalls, 1);
    expect(capture.phoneStops, 1);
    expect(find.byKey(const Key('conversation-process-now-progress')), findsOneWidget);
    expect(find.byType(ConversationCapturingPage), findsOneWidget);

    finalTranscriptGate.complete();
    await tester.pumpAndSettle();

    expect(ownerCalls, 1);
    expect(capture.phoneStops, 1);
    expect(find.byType(ConversationCapturingPage), findsNothing);
  });

  testWidgets('empty initializing necklace action targets necklace transport and exits', (tester) async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    final capture = _FakeCaptureProvider(RecordingState.initialising, transcriptReady: false)
      ..updateRecordingDevice(necklace);
    await _pumpCapturePage(tester, capture);

    await tester.tap(find.byKey(const Key('conversation-process-now')));
    await tester.pumpAndSettle();

    expect(capture.phoneStops, 0);
    expect(capture.deviceStops, 1);
    expect(find.byType(ConversationCapturingPage), findsNothing);
  });
}

String _captureStatus(WidgetTester tester) {
  return tester.widget<Text>(find.byKey(const Key('conversation-capture-status'))).data!;
}

TranscriptSegment _transcriptSegment() => TranscriptSegment(
      id: 'segment-1',
      text: 'A final transcript is ready to process.',
      speaker: 'SPEAKER_00',
      isUser: false,
      personId: null,
      start: 0,
      end: 1,
      translations: const [],
    );

Future<void> _pumpCapturePage(
  WidgetTester tester,
  _FakeCaptureProvider capture, {
  Future<bool> Function()? onProcessNow,
}) async {
  final device = DeviceProvider();
  addTearDown(capture.dispose);
  addTearDown(device.dispose);

  await tester.pumpWidget(
    MultiProvider(
      providers: [
        ChangeNotifierProvider<CaptureProvider>.value(value: capture),
        ChangeNotifierProvider<DeviceProvider>.value(value: device),
      ],
      child: MaterialApp(
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Builder(
          builder: (context) => Scaffold(
            body: Center(
              child: TextButton(
                key: const Key('open-capture-page'),
                onPressed: () => Navigator.of(context).push(
                  MaterialPageRoute<void>(
                    builder: (_) => ConversationCapturingPage(onProcessNow: onProcessNow),
                  ),
                ),
                child: const Text('Open'),
              ),
            ),
          ),
        ),
      ),
    ),
  );
  await tester.tap(find.byKey(const Key('open-capture-page')));
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 400));
}

class _FakeCaptureProvider extends CaptureProvider {
  _FakeCaptureProvider(
    RecordingState initialState, {
    required bool transcriptReady,
    bool phoneOwnsMobileAudio = false,
  })  : _transcriptReady = transcriptReady,
        _phoneOwnsMobileAudio = phoneOwnsMobileAudio {
    recordingState = initialState;
  }

  bool _transcriptReady;
  bool _phoneOwnsMobileAudio;
  int phoneStops = 0;
  int deviceStops = 0;
  int systemAudioStops = 0;
  int processingCalls = 0;

  @override
  bool get transcriptServiceReady => _transcriptReady;

  @override
  bool get phoneCaptureOwnsMobileAudio => _phoneOwnsMobileAudio || recordingState == RecordingState.record;

  void setCaptureState(
    RecordingState state, {
    required bool transcriptReady,
    bool phoneOwnsMobileAudio = false,
  }) {
    recordingState = state;
    _transcriptReady = transcriptReady;
    _phoneOwnsMobileAudio = phoneOwnsMobileAudio;
    notifyListeners();
  }

  @override
  Future<void> stopStreamRecording() async {
    phoneStops++;
    _phoneOwnsMobileAudio = false;
    updateRecordingState(RecordingState.stop);
  }

  @override
  Future<void> stopStreamDeviceRecording({bool cleanDevice = false}) async {
    deviceStops++;
    updateRecordingState(RecordingState.stop);
  }

  @override
  Future<void> stopSystemAudioRecording() async {
    systemAudioStops++;
    updateRecordingState(RecordingState.stop);
  }

  @override
  Future<bool> forceProcessingCurrentConversation({CaptureFinalizationOperation? operation}) async {
    processingCalls++;
    return true;
  }
}
