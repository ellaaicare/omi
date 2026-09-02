import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/transcript_segment.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/capture_source.dart';
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

    expect(_captureStatus(tester), 'iPhone · Initializing...');

    capture.setCaptureState(RecordingState.record, transcriptReady: false);
    await tester.pump();
    expect(_captureStatus(tester), 'iPhone · Recording Active · Reconnecting...');

    capture.setCaptureState(RecordingState.record, transcriptReady: true);
    await tester.pump();
    expect(_captureStatus(tester), 'iPhone · Recording Active');

    capture.setCaptureState(RecordingState.error, transcriptReady: false);
    await tester.pump();
    expect(_captureStatus(tester), 'iPhone · Error');
  });

  testWidgets('empty transport error offers an in-place iPhone retry instead of a blank page', (tester) async {
    final capture = _FakeCaptureProvider(RecordingState.error, transcriptReady: false);
    await _pumpCapturePage(tester, capture);

    expect(find.byKey(const Key('conversation-capture-retry-phone')), findsOneWidget);
    expect(find.byKey(const Key('conversation-capture-error-close')), findsOneWidget);
    expect(find.text("Recording isn't available right now."), findsOneWidget);

    await tester.tap(find.byKey(const Key('conversation-capture-retry-phone')));
    await tester.pump();

    expect(capture.phoneStarts, 1);
    expect(capture.recordingState, RecordingState.record);
    expect(find.byKey(const Key('conversation-capture-retry-phone')), findsNothing);
    expect(_captureStatus(tester), 'iPhone · Recording Active');
  });

  testWidgets('necklace transport error stays necklace-bound when Retry is pressed', (tester) async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    final capture = _FakeCaptureProvider(RecordingState.error, transcriptReady: false)..updateRecordingDevice(necklace);
    final device = _FakeDeviceProvider();
    await _pumpCapturePage(tester, capture, device: device);

    expect(_captureStatus(tester), 'Necklace · Error');
    expect(find.byKey(const Key('conversation-capture-retry-necklace')), findsOneWidget);

    await tester.tap(find.byKey(const Key('conversation-capture-retry-necklace')));
    await tester.pump();

    expect(device.reconnects, 1);
    expect(capture.phoneStarts, 0);
  });

  testWidgets('phone diagnostics outrank a retained necklace reference during Retry', (tester) async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    final capture = _FakeCaptureProvider(
      RecordingState.error,
      transcriptReady: false,
      diagnostics: const CaptureDiagnostics(source: CaptureDiagnosticSource.phone),
    )..updateRecordingDevice(necklace);
    final device = _FakeDeviceProvider();
    await _pumpCapturePage(tester, capture, device: device);

    expect(_captureStatus(tester), 'iPhone · Error');
    expect(find.byKey(const Key('conversation-capture-retry-phone')), findsOneWidget);

    await tester.tap(find.byKey(const Key('conversation-capture-retry-phone')));
    await tester.pump();

    expect(capture.phoneStarts, 1);
    expect(device.reconnects, 0);
  });

  testWidgets('explicit iPhone selection abandons a stale necklace startup before Retry', (tester) async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    final capture = _FakeCaptureProvider(
      RecordingState.initialising,
      transcriptReady: false,
    )..updateRecordingDevice(necklace);
    final device = _FakeDeviceProvider();
    await _pumpCapturePage(
      tester,
      capture,
      device: device,
      preferredCaptureSource: EllaCaptureSource.phone,
    );

    expect(_captureStatus(tester), 'iPhone · Initializing...');
    capture.setCaptureState(RecordingState.error, transcriptReady: false);
    await tester.pump();
    expect(_captureStatus(tester), 'iPhone · Error');
    expect(find.byKey(const Key('conversation-capture-retry-phone')), findsOneWidget);

    await tester.tap(find.byKey(const Key('conversation-capture-retry-phone')));
    await tester.pump();

    expect(capture.deviceStops, 1);
    expect(capture.phoneStarts, 1);
    expect(device.reconnects, 0);
    expect(_captureStatus(tester), 'iPhone · Recording Active');
  });

  testWidgets('empty active capture keeps Process Now visible before transcript segments', (tester) async {
    final capture = _FakeCaptureProvider(RecordingState.stop, transcriptReady: false);
    await _pumpCapturePage(tester, capture);

    expect(find.byKey(const Key('conversation-process-now')), findsNothing);

    capture.setCaptureState(RecordingState.stop, transcriptReady: false, phoneOwnsMobileAudio: true);
    await tester.pump();
    expect(find.byKey(const Key('conversation-process-now')), findsOneWidget);
    expect(find.text('Process Now'), findsOneWidget);

    capture.setCaptureState(RecordingState.stop, transcriptReady: false);
    await tester.pump();
    expect(find.byKey(const Key('conversation-process-now')), findsNothing);

    capture.setCaptureState(RecordingState.deviceRecord, transcriptReady: true);
    await tester.pump();
    expect(find.byKey(const Key('conversation-process-now')), findsOneWidget);
    expect(find.text('Process Now'), findsOneWidget);
  });

  testWidgets('empty phone action delegates finalization once and stays open when it fails', (tester) async {
    final capture = _FakeCaptureProvider(
      RecordingState.stop,
      transcriptReady: false,
      phoneOwnsMobileAudio: true,
      finalizationResult: false,
    );
    final ownerGate = Completer<bool>();
    var ownerCalls = 0;
    await _pumpCapturePage(
      tester,
      capture,
      onProcessNow: () async {
        ownerCalls++;
        expect(capture.phoneStops, 0, reason: 'the page must not close the transcript socket before its owner');
        final finalized = await capture.stopStreamRecordingAndFinalize();
        await ownerGate.future;
        return finalized;
      },
    );

    final stop = find.byKey(const Key('conversation-process-now'));
    await tester.tap(stop);
    await tester.tap(stop);
    await tester.pump();

    expect(capture.phoneStops, 1);
    expect(capture.phoneFinalizeCalls, 1);
    expect(capture.deviceStops, 0);
    expect(ownerCalls, 1);
    expect(find.byKey(const Key('conversation-process-now-progress')), findsOneWidget);
    expect(find.byType(ConversationCapturingPage), findsOneWidget);

    ownerGate.complete(false);
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(ownerCalls, 1);
    expect(find.byType(ConversationCapturingPage), findsOneWidget);
    expect(find.text('No words were captured, so no memory was created.'), findsOneWidget);
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
        await capture.stopStreamRecordingAndFinalize();
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
    expect(capture.phoneFinalizeCalls, 1);
    expect(find.byKey(const Key('conversation-process-now-progress')), findsOneWidget);
    expect(find.byType(ConversationCapturingPage), findsOneWidget);

    finalTranscriptGate.complete();
    await tester.pumpAndSettle();

    expect(ownerCalls, 1);
    expect(capture.phoneStops, 1);
    expect(capture.phoneFinalizeCalls, 1);
    expect(find.byType(ConversationCapturingPage), findsNothing);
  });

  testWidgets('mute targets active phone capture even when a stale necklace reference exists', (tester) async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    final capture = _FakeCaptureProvider(
      RecordingState.record,
      transcriptReady: true,
      phoneOwnsMobileAudio: true,
    )..updateRecordingDevice(necklace);
    await _pumpCapturePage(tester, capture);

    final unmutedSurface = tester.widget<Container>(
      find.byKey(const Key('conversation-capture-mute-surface')),
    );
    final unmutedDecoration = unmutedSurface.decoration! as BoxDecoration;
    expect(unmutedDecoration.color, EllaColors.elevatedCard);
    expect(find.byIcon(Icons.mic_rounded), findsOneWidget);
    expect(find.byIcon(Icons.mic_off_rounded), findsNothing);

    await tester.tap(find.byKey(const Key('conversation-capture-mute')));
    await tester.pump(const Duration(milliseconds: 100));

    expect(capture.phoneStops, 1);
    expect(capture.devicePauses, 0);
    expect(capture.recordingState, RecordingState.stop);
  });

  testWidgets('capture processing remains a single non-destructive action with or without visible text',
      (tester) async {
    final emptyCapture = _FakeCaptureProvider(RecordingState.record, transcriptReady: true, phoneOwnsMobileAudio: true);
    await _pumpCapturePage(tester, emptyCapture);

    final emptyProcessSurface = tester.widget<Container>(find.byKey(const Key('conversation-process-now-surface')));
    expect((emptyProcessSurface.decoration! as BoxDecoration).color, EllaColors.tealDeep);
    expect(find.text('Process Now'), findsOneWidget);
    Navigator.of(tester.element(find.byType(ConversationCapturingPage))).pop();
    await tester.pumpAndSettle();

    final contentCapture = _FakeCaptureProvider(
      RecordingState.record,
      transcriptReady: true,
      phoneOwnsMobileAudio: true,
    )..segments = [_transcriptSegment()];
    await _pumpCapturePage(tester, contentCapture);

    final processSurface = tester.widget<Container>(find.byKey(const Key('conversation-process-now-surface')));
    expect((processSurface.decoration! as BoxDecoration).color, EllaColors.tealDeep);
    expect(find.text('Process Now'), findsOneWidget);
  });

  testWidgets('empty initializing necklace process failure stays visible instead of acting as Back', (tester) async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    final capture = _FakeCaptureProvider(
      RecordingState.initialising,
      transcriptReady: false,
      finalizationResult: false,
    )..updateRecordingDevice(necklace);
    await _pumpCapturePage(tester, capture);

    await tester.tap(find.byKey(const Key('conversation-process-now')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(capture.phoneStops, 0);
    expect(capture.deviceStops, 1);
    expect(find.byType(ConversationCapturingPage), findsOneWidget);
    expect(find.text('No words were captured, so no memory was created.'), findsOneWidget);
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
  DeviceProvider? device,
  EllaCaptureSource? preferredCaptureSource,
}) async {
  final resolvedDevice = device ?? DeviceProvider();
  addTearDown(capture.dispose);
  addTearDown(resolvedDevice.dispose);

  await tester.pumpWidget(
    MultiProvider(
      providers: [
        ChangeNotifierProvider<CaptureProvider>.value(value: capture),
        ChangeNotifierProvider<DeviceProvider>.value(value: resolvedDevice),
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
                    builder: (_) => ConversationCapturingPage(
                      onProcessNow: onProcessNow,
                      preferredCaptureSource: preferredCaptureSource,
                    ),
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

class _FakeDeviceProvider extends DeviceProvider {
  int reconnects = 0;

  @override
  Future<bool> reconnectKnownDeviceForCapture({required String reason}) async {
    reconnects++;
    return true;
  }
}

class _FakeCaptureProvider extends CaptureProvider {
  _FakeCaptureProvider(
    RecordingState initialState, {
    required bool transcriptReady,
    bool phoneOwnsMobileAudio = false,
    bool finalizationResult = true,
    CaptureDiagnostics diagnostics = const CaptureDiagnostics(),
  })  : _transcriptReady = transcriptReady,
        _phoneOwnsMobileAudio = phoneOwnsMobileAudio,
        _finalizationResult = finalizationResult,
        _diagnostics = diagnostics {
    recordingState = initialState;
  }

  bool _transcriptReady;
  bool _phoneOwnsMobileAudio;
  final bool _finalizationResult;
  final CaptureDiagnostics _diagnostics;
  int phoneStops = 0;
  int phoneStarts = 0;
  int phoneFinalizeCalls = 0;
  int deviceStops = 0;
  int devicePauses = 0;
  int systemAudioStops = 0;
  int processingCalls = 0;

  @override
  bool get transcriptServiceReady => _transcriptReady;

  @override
  bool get phoneCaptureOwnsMobileAudio => _phoneOwnsMobileAudio || recordingState == RecordingState.record;

  @override
  CaptureDiagnostics get captureDiagnostics => _diagnostics;

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
  Future<PhoneCaptureStartResult> streamRecording() async {
    phoneStarts++;
    _phoneOwnsMobileAudio = true;
    _transcriptReady = true;
    updateRecordingState(RecordingState.record);
    return PhoneCaptureStartResult.started;
  }

  @override
  Future<void> stopStreamRecording() async {
    phoneStops++;
    _phoneOwnsMobileAudio = false;
    updateRecordingState(RecordingState.stop);
  }

  @override
  Future<bool> stopStreamRecordingAndFinalize() async {
    phoneFinalizeCalls++;
    await stopStreamRecording();
    return forceProcessingCurrentConversation();
  }

  @override
  Future<void> pauseDeviceRecording() async {
    devicePauses++;
    updateRecordingState(RecordingState.pause);
  }

  @override
  Future<void> stopStreamDeviceRecording({bool cleanDevice = false}) async {
    deviceStops++;
    updateRecordingState(RecordingState.stop);
  }

  @override
  Future<bool> stopStreamDeviceRecordingAndFinalize({bool cleanDevice = false}) async {
    await stopStreamDeviceRecording(cleanDevice: cleanDevice);
    return forceProcessingCurrentConversation();
  }

  @override
  Future<void> stopSystemAudioRecording() async {
    systemAudioStops++;
    updateRecordingState(RecordingState.stop);
  }

  @override
  Future<bool> forceProcessingCurrentConversation({CaptureFinalizationOperation? operation}) async {
    processingCalls++;
    return _finalizationResult;
  }
}
