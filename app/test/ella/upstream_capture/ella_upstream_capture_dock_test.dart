import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart' as fork;
import 'package:omi/ella/models/guardian_mode.dart';
import 'package:omi/ella/upstream_capture/ella_capture_authority.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_dock.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_runtime.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/upstream_capture/backend/preferences.dart' as upstream;
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/backend/schema/transcript_segment.dart';
import 'package:omi/upstream_capture/providers/capture_provider.dart';
import 'package:omi/upstream_capture/services/capture/capture_seams.dart';
import 'package:omi/upstream_capture/utils/enums.dart';

const _uid = 'uid-a';

class _NoBleListeners implements CaptureBleListeners {
  @override
  void addBatchRecordingFinalizedListener(void Function(String) callback) {}

  @override
  void removeBatchRecordingFinalizedListener(void Function(String) callback) {}
}

class _DockRuntime extends EllaUpstreamCaptureRuntime {
  _DockRuntime({
    required super.authority,
    required this.capture,
    this.connectNecklaceOverride,
  });

  final CaptureProvider capture;
  final Future<bool> Function(String uid, BtDevice device)? connectNecklaceOverride;

  @override
  Future<CaptureProvider> ensureBooted() async => capture;

  @override
  Future<List<BtDevice>> discoverNecklaces({int timeoutSeconds = 5}) async => [_testNecklace];

  @override
  Future<bool> connectNecklace(String uid, BtDevice device) {
    final override = connectNecklaceOverride;
    if (override != null) return override(uid, device);
    return super.connectNecklace(uid, device);
  }
}

final _testNecklace = BtDevice(id: 'necklace-under-test', name: 'Test Necklace', type: DeviceType.omi, rssi: -40);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('verified v10 authority renders the idle flag-on dock as one compact row', (tester) async {
    SharedPreferences.setMockInitialValues({});
    await fork.SharedPreferencesUtil.init();
    await upstream.SharedPreferencesUtil.init();
    final connectivity = StreamController<bool>.broadcast();
    final provider = CaptureProvider(
      connectivity: CaptureConnectivityBoundary(
        initiallyConnected: true,
        changes: connectivity.stream,
        isConnected: () => true,
      ),
      bleListeners: _NoBleListeners(),
      preferences: upstream.SharedPreferencesUtil(),
    );
    final authority = EllaCaptureAuthority(authenticatedUid: () => _uid, sessionStartAllowed: (_) => false);
    addTearDown(() async {
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pump();
      provider.dispose();
      authority.dispose();
      await connectivity.close();
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
      tester.platformDispatcher.clearTextScaleFactorTestValue();
    });

    final preferences = fork.SharedPreferencesUtil()..uid = _uid;
    const receiptId = '${fork.SharedPreferencesUtil.currentAiConsentReceiptPrefix}v10-receipt';
    preferences.acceptAiConsent(
      receiptId: receiptId,
      uid: _uid,
      profileBindingId: 'profile-binding-v10',
      serverDecidedAt: '2026-07-27T00:00:00Z',
      policyVersion: fork.SharedPreferencesUtil.legacyAiConsentContractVersionV10,
      processorSetHash: fork.SharedPreferencesUtil.legacyAiConsentProcessorSetHashV10,
    );
    preferences.markAiConsentServerVerified(
      uid: _uid,
      receiptId: receiptId,
      policyVersion: fork.SharedPreferencesUtil.legacyAiConsentContractVersionV10,
      processorSetHash: fork.SharedPreferencesUtil.legacyAiConsentProcessorSetHashV10,
      profileBindingId: 'profile-binding-v10',
      scopeVersion: fork.SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: fork.SharedPreferencesUtil.currentAiConsentScopeHash,
    );
    tester.view.physicalSize = const Size(1320, 2868);
    tester.view.devicePixelRatio = 3;
    tester.platformDispatcher.textScaleFactorTestValue = 1.5;

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: Align(
            alignment: Alignment.bottomCenter,
            child: Padding(
              padding: const EdgeInsets.all(14),
              child: EllaUpstreamCaptureDock(
                runtime: _DockRuntime(authority: authority, capture: provider),
                authenticatedUid: () => _uid,
              ),
            ),
          ),
        ),
      ),
    );
    await tester.pump();
    await tester.pump();

    final phone = find.byKey(const Key('upstream-capture-record-phone'));
    final necklace = find.byKey(const Key('upstream-capture-connect-necklace'));
    expect(phone.hitTestable(), findsOneWidget);
    expect(necklace.hitTestable(), findsOneWidget);
    final phoneRect = tester.getRect(phone);
    final necklaceRect = tester.getRect(necklace);
    expect(phoneRect.right, lessThan(necklaceRect.left));
    expect(phoneRect.center.dy, closeTo(necklaceRect.center.dy, 8));
    expect(tester.getSize(find.byKey(const Key('upstream-capture-dock'))).height, lessThan(120));
    expect(preferences.aiConsentAccepted, isTrue);
    expect(preferences.aiConsentContractVersion, fork.SharedPreferencesUtil.legacyAiConsentContractVersionV10);
    expect(tester.takeException(), isNull);
  });

  // ellaaicare/ella-ai#1287 RUN-020 bug #3: minimal parity with today_page.dart's Home
  // dock (live Transcript view + Whispers on/off) once a necklace session is genuinely
  // active, and the Finish button reflects that real state rather than "device selected".
  testWidgets('a genuinely live necklace session shows Transcript and Whispers, with Finish enabled', (tester) async {
    SharedPreferences.setMockInitialValues({});
    await fork.SharedPreferencesUtil.init();
    await upstream.SharedPreferencesUtil.init();
    final connectivity = StreamController<bool>.broadcast();
    final provider = CaptureProvider(
      connectivity: CaptureConnectivityBoundary(
        initiallyConnected: true,
        changes: connectivity.stream,
        isConnected: () => true,
      ),
      bleListeners: _NoBleListeners(),
      preferences: upstream.SharedPreferencesUtil(),
    );
    final authority = EllaCaptureAuthority(authenticatedUid: () => _uid, sessionStartAllowed: (_) => false);
    addTearDown(() async {
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pump();
      provider.dispose();
      authority.dispose();
      await connectivity.close();
    });

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: EllaUpstreamCaptureDock(
            runtime: _DockRuntime(authority: authority, capture: provider),
            authenticatedUid: () => _uid,
            guardianAvailability: () => true,
            guardianModeLoader: () async => const GuardianModeInfo(
              currentMode: GuardianModeKey.off,
              twoTierState: GuardianModeState(),
            ),
            guardianModeSetter: (_) async => true,
            guardianNativeStart: () async {},
            guardianNativeStop: () async {},
          ),
        ),
      ),
    );
    await tester.pump();
    await tester.pump();

    // Before any session: no Finish/Transcript, and Whispers reflects the loaded (off) state.
    expect(find.byKey(const Key('upstream-capture-finish')), findsNothing);
    expect(find.byKey(const Key('upstream-capture-view-transcript')), findsNothing);
    final whispersSwitchBefore = tester.widget<Switch>(find.byKey(const Key('upstream-capture-whispers-switch')));
    expect(whispersSwitchBefore.value, isFalse);

    // Simulate a genuinely live necklace session (native GATT connected, service
    // discovery done, upstream device-recording state machine active) rather than
    // just a selected/admitted device — this is what fix #1 (native connectPeripheral)
    // makes real instead of hanging.
    provider.segments.add(TranscriptSegment(
      id: 'seg-1',
      text: 'hello from the necklace',
      speaker: 'SPEAKER_0',
      isUser: false,
      personId: null,
      start: 0,
      end: 1,
      translations: const [],
    ));
    provider.updateRecordingDevice(_testNecklace);
    provider.updateRecordingState(RecordingState.deviceRecord);
    await tester.pump();

    final finish = find.byKey(const Key('upstream-capture-finish'));
    expect(finish, findsOneWidget);
    expect(tester.widget<TextButton>(finish).onPressed, isNotNull, reason: 'Finish must be tappable once live');

    final transcriptToggle = find.byKey(const Key('upstream-capture-view-transcript'));
    expect(transcriptToggle, findsOneWidget);
    expect(find.byKey(const Key('upstream-capture-transcript-panel')), findsNothing);
    await tester.tap(transcriptToggle);
    await tester.pump();
    expect(find.byKey(const Key('upstream-capture-transcript-panel')), findsOneWidget);
    expect(find.text('hello from the necklace'), findsOneWidget);

    expect(find.byKey(const Key('upstream-capture-whispers-switch')), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  // ellaaicare/ella-ai#1287 RUN-020 bug #1/#3: before this fix, native connectPeripheral
  // could leave the Dart connect() future pending indefinitely for a retrieved/known
  // candidate, so `starting` (and therefore the greyed-out Finish button) never cleared.
  // This proves the dock's own truthfulness once connectNecklace resolves promptly.
  testWidgets('Finish stays disabled while connect is in flight and becomes enabled once it resolves', (tester) async {
    SharedPreferences.setMockInitialValues({});
    await fork.SharedPreferencesUtil.init();
    await upstream.SharedPreferencesUtil.init();
    final connectivity = StreamController<bool>.broadcast();
    final provider = CaptureProvider(
      connectivity: CaptureConnectivityBoundary(
        initiallyConnected: true,
        changes: connectivity.stream,
        isConnected: () => true,
      ),
      bleListeners: _NoBleListeners(),
      preferences: upstream.SharedPreferencesUtil(),
    );
    final authority = EllaCaptureAuthority(authenticatedUid: () => _uid, sessionStartAllowed: (_) => false);
    addTearDown(() async {
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pump();
      provider.dispose();
      authority.dispose();
      await connectivity.close();
    });

    final connectCompleter = Completer<bool>();
    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: EllaUpstreamCaptureDock(
            runtime: _DockRuntime(
              authority: authority,
              capture: provider,
              connectNecklaceOverride: (uid, device) async {
                final connected = await connectCompleter.future;
                if (connected) {
                  provider.updateRecordingDevice(device);
                  provider.updateRecordingState(RecordingState.deviceRecord);
                }
                return connected;
              },
            ),
            authenticatedUid: () => _uid,
          ),
        ),
      ),
    );
    await tester.pump();
    await tester.pump();

    await tester.tap(find.byKey(const Key('upstream-capture-connect-necklace')));
    await tester.pump();
    await tester.pump();

    // Mid-connect: neither Finish nor the necklace button is tappable yet.
    expect(find.byKey(const Key('upstream-capture-finish')), findsNothing);
    final necklaceMidConnect = find.byKey(const Key('upstream-capture-connect-necklace'));
    if (necklaceMidConnect.evaluate().isNotEmpty) {
      expect(tester.widget<OutlinedButton>(necklaceMidConnect).onPressed, isNull);
    }

    connectCompleter.complete(true);
    await tester.pump();
    await tester.pump();

    final finish = find.byKey(const Key('upstream-capture-finish'));
    expect(finish, findsOneWidget);
    expect(tester.widget<TextButton>(finish).onPressed, isNotNull,
        reason: 'Finish must become tappable once connectNecklace resolves, not stay stuck greyed out');
    expect(tester.takeException(), isNull);
  });
}
