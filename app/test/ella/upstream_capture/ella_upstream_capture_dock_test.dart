import 'dart:convert';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:omi/ella/capture_host/ella_capture_host.dart' show EllaCaptureDiagnosticsSnapshot;
import 'package:omi/ella/upstream_capture/ella_home_transcript_panel.dart';
import 'package:omi/ella/widgets/quiet_memory_preview.dart';
import 'package:omi/ella/widgets/iris_capture_controls.dart';
import 'dart:async';
import 'package:omi/services/capture/capture_operation_cancellation.dart';
import 'dart:io';
import 'dart:ui' show PointerDeviceKind, SemanticsAction, SemanticsActionEvent, ImageByteFormat;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart' as fork;
import 'package:omi/backend/schema/bt_device/bt_device.dart' as legacy_device;
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/guardian_mode.dart';
import 'package:omi/ella/services/guardian_mode_service.dart' as guardian_native;
import 'package:omi/ella/upstream_capture/ella_capture_authority.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_dock.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_runtime.dart';
import 'package:omi/ella/widgets/ella_breathing_dot.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/capture/connect.dart';
import 'package:omi/pages/onboarding/find_device/page.dart';
import 'package:omi/pages/home/today_page.dart' show GuardianModeLoader, GuardianModeSetter, GuardianNativeLifecycle;
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/onboarding_provider.dart';
import 'package:omi/services/devices.dart' as legacy_service;
import 'package:omi/services/devices/device_connection.dart' as legacy_connection;
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/device.dart';
import 'package:omi/upstream_capture/backend/preferences.dart' as upstream;
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/backend/schema/conversation.dart';
import 'package:omi/upstream_capture/backend/schema/structured.dart';
import 'package:omi/upstream_capture/backend/schema/transcript_segment.dart';
import 'package:omi/upstream_capture/providers/capture_provider.dart';
import 'package:omi/upstream_capture/services/capture/capture_seams.dart';
import 'package:omi/upstream_capture/utils/enums.dart';

import 'ella_capture_protocol_socket_cases.dart';
import 'upstream_capture_protocol_v2_cases.dart';

const _uid = 'uid-a';

class _NonlinearWhisperTextScaler extends TextScaler {
  const _NonlinearWhisperTextScaler();

  @override
  double scale(double fontSize) => fontSize * (fontSize <= 14 ? 3 : 2);

  @override
  double get textScaleFactor => 3;
}

final _testNecklace = BtDevice(id: 'necklace-a', name: 'Compass', type: DeviceType.omi, rssi: -40);
final _pickerNecklace = legacy_device.BtDevice(
  id: 'necklace-a',
  name: 'Compass',
  type: legacy_device.DeviceType.fieldy,
  rssi: -40,
);
final _pickerFriend = legacy_device.BtDevice(
  id: 'friend-a',
  name: 'Friend',
  type: legacy_device.DeviceType.friendPendant,
  rssi: -40,
);

class _WhisperTestAuthority implements ExactAccountAuthorityVerifier {
  _WhisperTestAuthority(this.uid, this.current);
  @override
  final String uid;
  final bool Function() current;
  @override
  bool isExactCurrent() => current();
}

class _PressTestAuthority extends EllaCaptureAuthority {
  _PressTestAuthority() : super(authenticatedUid: () => _uid, sessionStartAllowed: (_) => false);
  int epoch = 0;
  @override
  int get bindingEpoch => epoch;
}

class _DockSessionAuthority extends EllaCaptureAuthority {
  _DockSessionAuthority() : super(authenticatedUid: () => _uid, sessionStartAllowed: (_) => false);
  bool current = true;
  int epoch = 0;
  @override
  bool get hasCurrentAuthority => current;
  @override
  String? get boundUid => current ? _uid : null;
  @override
  int get bindingEpoch => epoch;
}

/// Explicit committed-session seam. Existing dock tests mark recordingState
/// directly; real sessions get these identities from the capture coordinator.
class _DockCaptureProvider extends CaptureProvider {
  _DockCaptureProvider({super.connectivity, super.bleListeners, super.preferences});
  // Explicit committed presentation seam for this widget-only fixture. Real
  // coordinator ownership is tested by transcript_rollover_runtime_test.dart.
  String presentationProducer = 'fixture-producer-1';
  String presentationMode = 'live';
  int presentationDeviceRevision = 0;
  int presentationStarts = 1;
  @override
  ({String sessionKey, String mode, int deviceRevision})? get transcriptPresentationIdentity =>
      liveCaptureSource == null ? null : (sessionKey:presentationProducer,
        mode:presentationMode, deviceRevision:presentationDeviceRevision);
  @override
  void clearUserData() {
    presentationProducer='fixture-producer-${++presentationStarts}';
    super.clearUserData();
  }
  String recordingIdentity = 'recording-1';
  String? windowIdentity = 'window-1';
  bool captureReady = true;
  bool committedPaused = false;
  String pausedSource = 'phone';
  Completer<void>? pauseGate;
  Completer<void>? resumeGate;
  CaptureOperationCancellation? lastResumeCancellation;
  @override
  bool get canPauseLiveCapture =>
      !committedPaused && (recordingState == RecordingState.record || recordingState == RecordingState.deviceRecord);
  @override
  bool get isLiveCapturePaused => committedPaused;
  @override
  bool get isPaused => committedPaused;
  @override
  Future<void> pauseCapture({bool Function()? isCurrent}) async {
    if (!(isCurrent?.call() ?? true)) throw StateError('stale pause');
    await pauseGate?.future;
    if (!(isCurrent?.call() ?? true)) throw StateError('stale pause');
    committedPaused = true;
    updateRecordingState(RecordingState.pause);
  }

  @override
  Future<void> resumeCapture(
      {bool Function()? isCurrent, CaptureOperationCancellation? cancellation, bool Function()? canCancel}) async {
    lastResumeCancellation = cancellation;
    if (!(isCurrent?.call() ?? true)) throw StateError('stale resume');
    await resumeGate?.future;
    if (!(isCurrent?.call() ?? true)) throw StateError('stale resume');
    committedPaused = false;
    updateRecordingState(pausedSource == 'phone' ? RecordingState.record : RecordingState.deviceRecord);
  }

  @override
  bool get transcriptServiceReady => captureReady;
  @override
  String? get activeRecordingId => recordingIdentity;
  @override
  String? get activeCaptureSessionId => windowIdentity;
  @override
  String? get liveCaptureSource => switch (recordingState) {
        RecordingState.record || RecordingState.interrupted => 'phone',
        RecordingState.deviceRecord => 'omi',
        RecordingState.pause when committedPaused => pausedSource,
        _ => null,
      };

  void markSocketRecovering() {
    captureReady = false;
    updateRecordingState(recordingState == RecordingState.record ? RecordingState.interrupted : recordingState);
  }
}

class _PickerService implements legacy_service.IDeviceService {
  _PickerService(this.devices);

  final List<legacy_device.BtDevice> devices;
  final Map<Object, legacy_service.IDeviceServiceSubsciption> _listeners = {};
  int discovers = 0;
  bool failDiscovery = false;

  @override
  void start() {}
  @override
  Future<void> stop() async {}
  @override
  Future<void> discover({String? desirableDeviceId, int timeout = 5}) async {
    discovers++;
    if (failDiscovery) throw StateError('discovery unavailable');
    for (final listener in _listeners.values) {
      listener.onDevices(devices);
    }
  }

  @override
  Future<legacy_connection.DeviceConnection?> ensureConnection(String deviceId, {bool force = false}) async => null;
  @override
  void subscribe(legacy_service.IDeviceServiceSubsciption subscription, Object context) =>
      _listeners[context] = subscription;
  @override
  void unsubscribe(Object context) => _listeners.remove(context);
  @override
  DateTime? getFirstConnectedAt() => null;
  @override
  void setWifiSyncInProgress(bool value) {}
  @override
  Future<void> cancelPendingConnection() async {}
  @override
  Future<void> disconnectDevice() async {}
}

class _PickerDeviceProvider extends DeviceProvider {
  _PickerDeviceProvider(this.service) : super(deviceService: service, automaticallyReconnectOnReady: false);

  final _PickerService service;
  int connects = 0;
  bool consentRequired = false;
  Future<bool> Function()? connectOverride;

  @override
  bool get lastConnectionConsentRequired => consentRequired;
  @override
  Future<void> prepareForExplicitDeviceSelection() async {}
  @override
  Future<bool> connectDeviceForCurrentUser(legacy_device.BtDevice device, {bool requireFreshSession = false}) async {
    connects++;
    final connected = await connectOverride?.call() ?? false;
    if (connected) {
      connectedDevice = device;
      isConnected = true;
    }
    return connected;
  }
}

class _PickerHarness {
  _PickerHarness(List<legacy_device.BtDevice> devices) : service = _PickerService(devices) {
    provider = _PickerDeviceProvider(service);
    onboarding = OnboardingProvider(deviceService: service)
      ..setDeviceProvider(provider)
      ..hasBluetoothPermission = true;
  }

  final _PickerService service;
  late final _PickerDeviceProvider provider;
  late final OnboardingProvider onboarding;
}

class _NoBleListeners implements CaptureBleListeners {
  @override
  void addBatchRecordingFinalizedListener(void Function(String) callback) {}

  @override
  void removeBatchRecordingFinalizedListener(void Function(String) callback) {}
}

class _DockRuntime extends EllaUpstreamCaptureRuntime {
  _DockRuntime({required super.authority, required this.capture});

  final CaptureProvider capture;
  CaptureProvider? replacementCapture;
  EllaCaptureDiagnosticsSnapshot? healthOverride;
  @override
  EllaCaptureDiagnosticsSnapshot get captureDiagnostics => healthOverride ?? super.captureDiagnostics;
  @override
  CaptureProvider get provider => replacementCapture ?? capture;
  Future<CaptureProvider> Function()? bootOverride;
  Future<List<BtDevice>> Function()? discoverOverride;
  Future<EllaCaptureStartOutcome> Function(String uid)? startPhoneOverride;
  Future<EllaCaptureStartOutcome> Function(String uid, BtDevice device)? connectNecklaceOverride;
  Future<void> Function()? stopPhoneOverride;
  Future<void> Function()? stopEscapeOverride;
  Future<void> Function()? disconnectNecklaceOverride;
  Future<void> Function()? finishOverride;

  @override
  Future<CaptureProvider> ensureBooted() => bootOverride?.call() ?? Future.value(capture);

  @override
  Future<List<BtDevice>> discoverNecklaces({int timeoutSeconds = 5}) =>
      discoverOverride?.call() ?? Future.value([_testNecklace]);

  @override
  Future<EllaCaptureStartOutcome> startPhoneCapture(String uid) =>
      startPhoneOverride?.call(uid) ?? Future.value(EllaCaptureStartOutcome.started);

  @override
  Future<EllaCaptureStartOutcome> connectNecklace(String uid, BtDevice device, {bool explicitSelection = true, bool Function()? lifecycleIsCurrent}) =>
      connectNecklaceOverride?.call(uid, device) ?? Future.value(EllaCaptureStartOutcome.started);

  @override
  Future<void> stopCaptureEscape() {
    cancelPendingCaptureChange();
    return stopEscapeOverride?.call() ?? stopPhoneCapture();
  }

  @override
  Future<void> stopPhoneCapture() => stopPhoneOverride?.call() ?? Future.value();

  @override
  Future<void> disconnectNecklace({String? deviceId}) => disconnectNecklaceOverride?.call() ?? Future.value();

  @override
  Future<void> finishConversation({bool Function()? isCurrent, bool stopDeviceIngress = false}) {
    if (isCurrent != null && !isCurrent()) return Future.error(StateError('retired capture'));
    return finishOverride?.call() ?? Future.value();
  }
}

class _DockFixture {
  _DockFixture({required this.connectivity, required this.provider, required this.authority, required this.runtime});

  final StreamController<bool> connectivity;
  final _DockCaptureProvider provider;
  final EllaCaptureAuthority authority;
  final _DockRuntime runtime;

  static Future<_DockFixture> create(WidgetTester tester, {EllaCaptureAuthority? authorityOverride}) async {
    SharedPreferences.setMockInitialValues({});
    await fork.SharedPreferencesUtil.init();
    await upstream.SharedPreferencesUtil.init();
    final connectivity = StreamController<bool>.broadcast();
    final provider = _DockCaptureProvider(
      connectivity: CaptureConnectivityBoundary(
        initiallyConnected: true,
        changes: connectivity.stream,
        isConnected: () => true,
      ),
      bleListeners: _NoBleListeners(),
      preferences: upstream.SharedPreferencesUtil(),
    );
    final authority = authorityOverride ?? _DockSessionAuthority();
    final runtime = _DockRuntime(authority: authority, capture: provider);
    final fixture = _DockFixture(
      connectivity: connectivity,
      provider: provider,
      authority: authority,
      runtime: runtime,
    );
    addTearDown(() async {
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pump();
      provider.dispose();
      authority.dispose();
      await connectivity.close();
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    return fixture;
  }

  Future<void> pump(
    WidgetTester tester, {
    Size size = const Size(390, 844),
    double textScale = 1,
    TextScaler? textScaler,
    Locale? locale,
    bool guardianAvailable = false,
    GuardianModeLoader? guardianModeLoader,
    GuardianModeSetter? guardianModeSetter,
    GuardianNativeLifecycle? guardianNativeStart,
    GuardianNativeLifecycle? guardianNativeStop,
    GuardianNativeStateReader? guardianNativeState,
    Stream<guardian_native.GuardianModeState>? guardianNativeStates,
    guardian_native.GuardianWhisperAuthorityProvider? guardianAuthorityProvider,
    EllaCaptureConsentRequester? consentRequester,
    String Function()? authenticatedUid,
    OnboardingProvider? onboarding,
    bool reduceMotion = true,
    bool compactPresentation = false,
    bool headerControl = false,
    bool homeTranscript = false,
    bool dark = false,
    double? dockMaxHeight,
  }) async {
    tester.view.physicalSize = size;
    tester.view.devicePixelRatio = 1;
    await tester.pumpWidget(
      ChangeNotifierProvider<OnboardingProvider>.value(
        value: onboarding ?? OnboardingProvider(),
        child: MaterialApp(
          debugShowCheckedModeBanner: false,
          theme: dark ? ellaDarkThemeData() : ellaThemeData(),
          locale: locale,
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          builder: (context, child) => RepaintBoundary(
              key: const Key('capture-fixture-render'),
              child: MediaQuery(
                data: MediaQuery.of(
                  context,
                ).copyWith(textScaler: textScaler ?? TextScaler.linear(textScale), disableAnimations: reduceMotion),
                child: child!,
              )),
          home: Scaffold(
            body: Align(
              alignment: Alignment.bottomCenter,
              child: Padding(
                padding: const EdgeInsets.all(14),
                child: ConstrainedBox(
                  constraints: BoxConstraints(maxHeight: dockMaxHeight ?? double.infinity),
                  child: homeTranscript
                      ? EllaHomeTranscriptPanel(runtime: runtime, authenticatedUid: authenticatedUid ?? () => _uid)
                      : EllaUpstreamCaptureDock(
                          expandControlsForTesting: !compactPresentation,
                          headerControl: headerControl,
                          runtime: runtime,
                          authenticatedUid: authenticatedUid ?? () => _uid,
                          consentRequester: consentRequester,
                          guardianAvailability: () => guardianAvailable,
                          guardianAuthorityProvider: guardianAuthorityProvider ??
                              () {
                                final uid = authenticatedUid?.call() ?? _uid;
                                return _WhisperTestAuthority(uid, () => (authenticatedUid?.call() ?? _uid) == uid);
                              },
                          guardianModeLoader: guardianModeLoader,
                          guardianModeSetter: guardianModeSetter,
                          guardianNativeStart: guardianNativeStart,
                          guardianNativeStop: guardianNativeStop,
                          guardianNativeState: guardianNativeState,
                          guardianNativeStates: guardianNativeStates,
                        ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
    await tester.pump();
    await tester.pump();
  }

  void makeNecklaceLive({List<TranscriptSegment> segments = const []}) {
    provider.updateRecordingDevice(_testNecklace);
    provider.updateRecordingState(RecordingState.deviceRecord);
    provider.segments.addAll(segments);
    provider.onConnected();
  }

  void makePhoneLive() {
    provider.updateRecordingState(RecordingState.record);
    provider.onConnected();
  }
}

TranscriptSegment _segment(String id, String text) => TranscriptSegment(
      id: id,
      text: text,
      speaker: 'SPEAKER_0',
      isUser: false,
      personId: null,
      start: 0,
      end: 1,
      translations: const [],
    );

double _contrastRatio(Color foreground, Color background) {
  final a = foreground.computeLuminance();
  final b = background.computeLuminance();
  final lighter = a > b ? a : b;
  final darker = a > b ? b : a;
  return (lighter + 0.05) / (darker + 0.05);
}

void main() {

  for (final scenario in ['empty', 'words', 'retired', 'large', 'share-failure']) {
    testWidgets('diagnostics candidate support export $scenario is content free', (tester) async {
      PackageInfo.setMockInitialValues(appName: 'Ella', packageName: 'com.ellaaicare.ella',
        version: '0.0.0', buildNumber: '0', buildSignature: '');
      final fixture = await _DockFixture.create(tester);
      var current = true;
      fixture.provider.updateRecordingState(RecordingState.pause);
      if (scenario == 'words' || scenario == 'retired') {
        fixture.provider.segments.add(_segment('offline-window', 'PRIVATE-OFFLINE-WORDS-NOT-FOR-REPORT'));
      }
      final presentation = ValueNotifier<int>(0);
      addTearDown(presentation.dispose);
      final reports = <String>[];
      tester.view.physicalSize = scenario == 'large' ? const Size(320, 844) : const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      await tester.pumpWidget(MaterialApp(theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        builder: (context, child) => MediaQuery(data: MediaQuery.of(context).copyWith(
          textScaler: TextScaler.linear(scenario == 'large' ? 3.5 : 1)), child: child!),
        home: Scaffold(body: EllaCaptureTranscriptSheet(provider: fixture.provider,
          sourceLabel: 'Transcript · Necklace', onClose: () => true,
          isCurrent: () => current, presentationChanges: presentation,
          diagnosticReporter: (report) async {
            if (scenario == 'share-failure') throw StateError('offline share failure');
            reports.add(report);
          },
        )),
      ));
      await tester.pumpAndSettle();
      if (scenario == 'retired') {
        current = false;
        // Deliberately do not repaint first: the export tap must recheck and
        // clear the old current-owned diagnostic history synchronously.
      }
      final support = find.byKey(const Key('upstream-capture-transcript-support'));
      expect(support.hitTestable(), findsOneWidget);
      expect(tester.getSize(support).width, greaterThanOrEqualTo(44));
      await tester.tap(support);
      await tester.pump();
      if (scenario == 'share-failure') {
        expect(reports, isEmpty);
        expect(find.text('Something went wrong! Please try again later.'), findsOneWidget);
        expect(fixture.runtime.captureSession, isNull);
        expect(fixture.provider.havingRecordingDevice, false);
        expect(tester.takeException(), isNull);
        return;
      }
      expect(reports, hasLength(1));
      final report = reports.single;
      expect(report, isNot(contains('PRIVATE-OFFLINE-WORDS')));
      expect(report, isNot(contains('offline-window')));
      expect(report, isNot(contains(_uid)));
      final data = jsonDecode(report.substring(report.indexOf('{'))) as Map<String, dynamic>;
      final events = data['events'] as List;
      expect(events, isNotEmpty);
      final latest = events.last as Map<String, dynamic>;
      expect(latest['app_build'], '0', reason: 'actual mocked local app identity, not an assumed installed candidate');
      if (scenario == 'retired') {
        for (final event in events) {
          expect(event['viewer_current'], false);
          expect(event['branch'], 'retired');
          expect(event['segment_count'], isNull);
          expect(event['recording_state'], isNull);
        }
      } else {
        expect(latest['viewer_current'], true);
        expect(latest['branch'], scenario == 'words' ? 'words' : 'paused');
      }
      expect(fixture.runtime.captureSession, isNull);
      expect(fixture.provider.havingRecordingDevice, false);
      expect(tester.takeException(), isNull);
    });
  }

  for (final scenario in ['pending', 'ready', 'uid', 'epoch', 'terminal', 'provisioning_terminal', 'missing']) {
    testWidgets('composite consent gate $scenario preserves capture admission', (tester) async {
      final fixture = await _DockFixture.create(tester);
      final prefs = fork.SharedPreferencesUtil()..uid = _uid;
      var uid = _uid;
      var reviews = 0;
      if (scenario != 'missing') {
        // Synthetic local state, not a device cache or an injected server grant.
        // Same supported contract as the retained public LAB wire fixture.
        prefs.acceptAiConsent(
          receiptId: 'aicr_offline_gate_fixture', uid: _uid,
          profileBindingId: 'offline-gate-profile',
          serverDecidedAt: '2026-09-29T06:44:28.350144Z',
        );
        await prefs.saveEllaProvisioningReceipt(_uid, {
          'state': 'ready', 'binding_state': 'active',
          'binding_revision': 3, 'effective_policy_revision': 'offline-policy-3',
        });
      }
      ExactAccountAuthorityVerifier? authority() {
        final current = WalOwnerAuthority.active(preferences: prefs, authenticatedUid: uid);
        return current == null ? null : _WhisperTestAuthority(uid,
          () => current.isCurrent(preferences: prefs, authenticatedUid: uid));
      }
      expect(authority(), isNull, reason: 'server verification is deliberately not committed yet');
      await fixture.pump(tester, compactPresentation: true,
        authenticatedUid: () => uid, guardianAuthorityProvider: authority,
        consentRequester: (_) async { reviews++; return false; });
      await tester.tap(find.byKey(const Key('upstream-capture-open-controls')));
      await tester.pumpAndSettle();
      if (scenario == 'missing') {
        expect(find.byKey(const Key('upstream-capture-review-consent')), findsOneWidget);
        expect(find.byKey(const Key('upstream-capture-setup-unverified')), findsNothing);
      } else {
        expect(find.byKey(const Key('upstream-capture-review-consent')), findsNothing,
          reason: 'valid local consent must not be mislabeled as a missing decision');
        expect(find.byKey(const Key('upstream-capture-setup-unverified')), findsOneWidget);
      }
      expect(find.byKey(const Key('iris-status-connect-necklace')), findsNothing);
      if (scenario == 'pending') {
        // Waiting changes presentation only; it cannot cancel the watcher or
        // turn valid consent into a new permission request.
        await tester.pump(const Duration(milliseconds: 1250));
        await tester.pumpAndSettle();
        expect(find.byKey(const Key('upstream-capture-setup-unverified')), findsOneWidget);
        expect(find.text('Setup could not complete its check'), findsOneWidget);
        expect(find.text('Setup is waiting for account validation'), findsNothing);
        expect(find.byKey(const Key('upstream-capture-review-consent')), findsNothing);
        expect(find.byKey(const Key('iris-status-connect-necklace')), findsNothing);
      } else if (scenario == 'ready') {
        await prefs.markEllaProvisioningVerified(_uid);
        expect(authority(), isNotNull);
        await tester.pump(const Duration(milliseconds: 300));
        await tester.pumpAndSettle();
        expect(find.byKey(const Key('upstream-capture-controls-sheet')), findsNothing,
          reason: 'old null authority action closures are dismissed, never promoted');
        await tester.tap(find.byKey(const Key('upstream-capture-open-controls')));
        await tester.pumpAndSettle();
        expect(find.byKey(const Key('upstream-capture-setup-unverified')), findsNothing);
        expect(find.byKey(const Key('upstream-capture-review-consent')), findsNothing);
        expect(find.byKey(const Key('iris-status-connect-necklace')), findsOneWidget);
      } else if (scenario == 'uid' || scenario == 'epoch' || scenario == 'terminal' || scenario == 'provisioning_terminal') {
        if (scenario == 'uid') uid = 'another-offline-owner';
        if (scenario == 'epoch') (fixture.authority as _DockSessionAuthority).epoch++;
        if (scenario == 'terminal') prefs.invalidateAccountAuthorityForTransition();
        if (scenario == 'provisioning_terminal') prefs.invalidateEllaProvisioningTerminalAuthority();
        await tester.pump(const Duration(milliseconds: 300));
        await tester.pumpAndSettle();
        expect(find.byKey(const Key('upstream-capture-review-consent')), findsNothing);
        expect(find.byKey(const Key('iris-status-connect-necklace')), findsNothing);
        expect(find.byKey(const Key('upstream-capture-setup-unverified')), findsNothing);
        if (scenario == 'uid') {
          // The timer observed retirement before the old identity returns.
          uid = _uid;
          fixture.provider.notifyListeners();
          await tester.pump();
          expect(find.byKey(const Key('upstream-capture-setup-unverified')), findsNothing);
          expect(find.byKey(const Key('upstream-capture-review-consent')), findsNothing);
          expect(find.byKey(const Key('iris-status-connect-necklace')), findsNothing);
        }
      }
      expect(reviews, 0);
      expect(fixture.runtime.captureSession, isNull);
      expect(fixture.provider.havingRecordingDevice, isFalse);
      expect(tester.takeException(), isNull);
    });
  }


  for (final candidate in ['stale', 'foreign', 'retired_then_returns']) {
    testWidgets('composite consent gate rejects $candidate authority', (tester) async {
      final fixture = await _DockFixture.create(tester);
      final prefs = fork.SharedPreferencesUtil()..uid = _uid;
      prefs.acceptAiConsent(receiptId: 'aicr_offline_gate_fixture', uid: _uid,
        profileBindingId: 'offline-gate-profile', serverDecidedAt: '2026-09-29T06:44:28.350144Z');
      var current = candidate == 'retired_then_returns';
      ExactAccountAuthorityVerifier? latest = current
          ? _WhisperTestAuthority(_uid, () => current) : null;
      var reviews = 0;
      await fixture.pump(tester, compactPresentation: true,
        guardianAuthorityProvider: () => latest,
        consentRequester: (_) async { reviews++; return false; });
      await tester.tap(find.byKey(const Key('upstream-capture-open-controls')));
      await tester.pumpAndSettle();
      if (candidate == 'retired_then_returns') {
        expect(find.byKey(const Key('iris-status-connect-necklace')), findsOneWidget);
        current = false;
      } else {
        latest = _WhisperTestAuthority(candidate == 'foreign' ? 'another-offline-owner' : _uid,
          () => candidate == 'foreign');
      }
      await tester.pump(const Duration(milliseconds: 300));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('upstream-capture-controls-sheet')), findsOneWidget,
        reason: 'wrong-owner or non-current authority must not count as recovery');
      expect(find.byKey(const Key('iris-status-connect-necklace')), findsNothing);
      expect(find.byKey(const Key('upstream-capture-review-consent')), findsNothing);
      if (candidate == 'retired_then_returns') {
        current = true;
        fixture.provider.notifyListeners();
        await tester.pump();
        expect(find.byKey(const Key('iris-status-connect-necklace')), findsNothing,
          reason: 'an observed retirement is permanent for this sheet');
      }
      expect(reviews, 0);
      expect(fixture.runtime.captureSession, isNull);
      expect(fixture.provider.havingRecordingDevice, isFalse);
      expect(tester.takeException(), isNull);
    });
  }

  TestWidgetsFlutterBinding.ensureInitialized();
  for (final dark in [false, true]) {
    for (final scale in [1.0, 2.0, 3.5]) {
      testWidgets('929 compact capture off has readable recovery and stock connect route dark=$dark scale=$scale',
          (tester) async {
        final fixture = await _DockFixture.create(tester);
        final picker = _PickerHarness([_pickerNecklace]);
        addTearDown(() {
          picker.onboarding.dispose();
          picker.provider.dispose();
        });
        await fixture.pump(tester,
            compactPresentation: true, dark: dark, textScale: scale, onboarding: picker.onboarding);
        final homeConnect = find.byKey(const Key('iris-home-connect-necklace'));
        expect(homeConnect.hitTestable(), findsOneWidget);
        await tester.tap(homeConnect);
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 400));
        expect(find.byType(ConnectDevicePage), findsOneWidget);
        expect(find.byType(FindDevicesPage), findsOneWidget);
        expect(picker.service.discovers, 1);
        expect(picker.provider.connects, 0, reason: 'Opening discovery must not start capture');
        Navigator.of(tester.element(find.byType(ConnectDevicePage))).pop();
        await tester.pumpAndSettle();
        await tester.pump(const Duration(milliseconds: 400));
        await tester.ensureVisible(find.byKey(const Key('upstream-capture-open-controls')));
        await tester.tap(find.byKey(const Key('upstream-capture-open-controls')));
        await tester.pump();
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 400));
        expect(find.byKey(const Key('upstream-capture-controls-sheet')), findsOneWidget);
        final sheetConnect = find.byKey(const Key('iris-status-connect-necklace'));
        await tester.ensureVisible(sheetConnect);
        await tester.pump();
        debugPrint(
            '929 SHEET connect=${tester.getRect(sheetConnect)} sheet=${tester.getRect(find.byKey(const Key('upstream-capture-controls-sheet')))} enabled=${tester.widget<FilledButton>(sheetConnect).onPressed != null}');
        expect(sheetConnect.hitTestable(), findsOneWidget);
        expect(find.text("No. Your Necklace isn't connected."), findsOneWidget);
        expect(find.text("Ella can't hear you until it's connected again. Everything already kept is safe."),
            findsOneWidget);
        expect(tester.getSize(sheetConnect).height, greaterThanOrEqualTo(52));
        final details = find.byKey(const Key('iris-status-details'));
        await tester.ensureVisible(details);
        await tester.tap(details);
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 400));
        final tile = tester.widget<ExpansionTile>(details);
        final palette = EllaPalette.of(tester.element(details));
        expect(tile.textColor, palette.ink);
        expect(tile.iconColor, palette.inkSoft);
        expect(tile.collapsedTextColor, palette.ink);
        final report = find.widgetWithText(TextButton, 'Send a support report');
        await tester.ensureVisible(report);
        await tester.pump(const Duration(milliseconds: 400));
        debugPrint(
            '929 REPORT rect=${tester.getRect(report)} sheet=${tester.getRect(find.byKey(const Key('upstream-capture-controls-sheet')))}');
        expect(report.hitTestable(), findsOneWidget);
        final reportColor = tester.widget<TextButton>(report).style!.foregroundColor!.resolve({});
        expect(reportColor, isNot(palette.paper), reason: 'Enabled support action must remain readable');
        await tester.ensureVisible(sheetConnect);
        await tester.tap(sheetConnect);
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 400));
        expect(find.byKey(const Key('upstream-capture-controls-sheet')), findsNothing);
        expect(find.byType(ConnectDevicePage), findsOneWidget);
        expect(picker.service.discovers, 2);
        expect(picker.provider.connects, 0);
        expect(tester.takeException(), isNull);
      });
    }
  }
  for (final dark in [false, true]) {
    for (final scale in [1.0, 2.0, 3.5]) {
      testWidgets('930 header gear has accessible readable accordion and stock connect route dark=$dark scale=$scale',
          (tester) async {
        final fixture = await _DockFixture.create(tester);
        final picker = _PickerHarness([_pickerNecklace]);
        addTearDown(() {
          picker.onboarding.dispose();
          picker.provider.dispose();
        });
        await fixture.pump(tester,
            compactPresentation: true,
            headerControl: true,
            dark: dark,
            textScale: scale,
            onboarding: picker.onboarding);
        expect(find.byKey(const Key('iris-home-connect-necklace')), findsNothing);
        expect(find.text('Not recording'), findsNothing);
        final gear = find.byKey(const Key('upstream-capture-open-controls'));
        expect(tester.widget<IconButton>(gear).tooltip, contains('Necklace not connected'));
        expect(gear.hitTestable(), findsOneWidget);
        expect(tester.getSize(gear).width, greaterThanOrEqualTo(48));
        expect(tester.getSize(gear).height, greaterThanOrEqualTo(48));
        await tester.ensureVisible(find.byKey(const Key('upstream-capture-open-controls')));
        await tester.tap(find.byKey(const Key('upstream-capture-open-controls')));
        await tester.pump();
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 400));
        expect(find.byKey(const Key('upstream-capture-controls-sheet')), findsOneWidget);
        final sheetConnect = find.byKey(const Key('iris-status-connect-necklace'));
        await tester.ensureVisible(sheetConnect);
        await tester.pump();
        debugPrint(
            '929 SHEET connect=${tester.getRect(sheetConnect)} sheet=${tester.getRect(find.byKey(const Key('upstream-capture-controls-sheet')))} enabled=${tester.widget<FilledButton>(sheetConnect).onPressed != null}');
        expect(sheetConnect.hitTestable(), findsOneWidget);
        expect(find.text("No. Your Necklace isn't connected."), findsOneWidget);
        expect(find.text("Ella can't hear you until it's connected again. Everything already kept is safe."),
            findsOneWidget);
        expect(tester.getSize(sheetConnect).height, greaterThanOrEqualTo(52));
        final details = find.byKey(const Key('iris-status-details'));
        await tester.ensureVisible(details);
        await tester.tap(details);
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 400));
        final tile = tester.widget<ExpansionTile>(details);
        final palette = EllaPalette.of(tester.element(details));
        expect(tile.textColor, palette.ink);
        expect(tile.iconColor, palette.inkSoft);
        expect(tile.collapsedTextColor, palette.ink);
        final report = find.widgetWithText(TextButton, 'Send a support report');
        await tester.ensureVisible(report);
        await tester.pump(const Duration(milliseconds: 400));
        debugPrint(
            '929 REPORT rect=${tester.getRect(report)} sheet=${tester.getRect(find.byKey(const Key('upstream-capture-controls-sheet')))}');
        expect(report.hitTestable(), findsOneWidget);
        final reportColor = tester.widget<TextButton>(report).style!.foregroundColor!.resolve({});
        expect(reportColor, isNot(palette.paper), reason: 'Enabled support action must remain readable');
        await tester.ensureVisible(sheetConnect);
        await tester.tap(sheetConnect);
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 400));
        expect(find.byKey(const Key('upstream-capture-controls-sheet')), findsNothing);
        expect(find.byType(ConnectDevicePage), findsOneWidget);
        expect(picker.service.discovers, 1);
        expect(picker.provider.connects, 0);
        expect(tester.takeException(), isNull);
      });
    }
  }

  for (final necklace in [false, true]) {
    testWidgets('929 active ${necklace ? 'necklace' : 'phone'} capture hides competing connect actions',
        (tester) async {
      final fixture = await _DockFixture.create(tester);
      necklace ? fixture.makeNecklaceLive() : fixture.makePhoneLive();
      await fixture.pump(tester, compactPresentation: true);
      expect(find.byKey(const Key('iris-home-connect-necklace')), findsNothing);
      await tester.tap(find.byKey(const Key('upstream-capture-open-controls')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('iris-status-connect-necklace')), findsNothing);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('official Home necklace Pause and Resume are visible without the Iris menu', (tester) async {
    final fixture = await _DockFixture.create(tester);
    fixture.makeNecklaceLive();
    fixture.provider.pausedSource = 'omi';
    await fixture.pump(tester, homeTranscript: true);
    expect(find.byKey(const Key('upstream-capture-controls-sheet')), findsNothing);
    await tester.tap(find.byTooltip('Pause'));
    await tester.pump();
    expect(fixture.provider.committedPaused, isTrue);
    expect(find.byTooltip('Resume'), findsOneWidget);
    fixture.provider.resumeGate = Completer<void>();
    await tester.tap(find.byTooltip('Resume'));
    await tester.pump();
    expect(fixture.provider.lastResumeCancellation, isNull,
        reason: 'Pendant Resume must not inherit the custom phone-start timeout/mute callback');
    await tester.pump(const Duration(seconds: 21));
    expect(fixture.provider.committedPaused, isTrue);
    fixture.provider.resumeGate!.complete();
    await tester.pump();
    expect(fixture.provider.committedPaused, isFalse);
    expect(tester.takeException(), isNull);
  });

  testWidgets('necklace listening sheet no longer hosts an Ella-only Pause/Resume control', (tester) async {
    final fixture = await _DockFixture.create(tester);
    fixture.makeNecklaceLive();
    await fixture.pump(tester, compactPresentation: true);
    await tester.tap(find.byKey(const Key('upstream-capture-open-controls')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('iris-status-pause-resume')), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('Home transcript one tap opens existing full viewer and Close returns without capture', (tester) async {
    final fixture = await _DockFixture.create(tester);
    fixture
        .makeNecklaceLive(segments: [_segment('home-note', 'Fictional violet lantern with a long transcript. ' * 30)]);
    var starts = 0;
    fixture.runtime.connectNecklaceOverride = (uid, device) async {
      starts++;
      return EllaCaptureStartOutcome.started;
    };
    await fixture.pump(tester, homeTranscript: true);
    expect(find.byKey(const Key('home-transcript-preview')), findsOneWidget);
    expect(find.text('Current transcript'), findsOneWidget);
    await tester.tap(find.byKey(const Key('home-open-transcript')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(find.byKey(const Key('upstream-capture-transcript-list')), findsOneWidget);
    expect(find.byKey(const Key('upstream-capture-controls-sheet')), findsNothing);
    await tester.tap(find.byKey(const Key('upstream-capture-transcript-close')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(find.byKey(const Key('home-open-transcript')), findsOneWidget);
    expect(starts, 0);
    expect(tester.takeException(), null);
  });
  testWidgets('Home transcript empty disconnected state never exposes cached words or admits capture', (tester) async {
    final fixture = await _DockFixture.create(tester);
    fixture.provider.segments.add(_segment('old', 'OLD-PRIVATE-WINDOW'));
    await fixture.pump(tester, homeTranscript: true);
    expect(find.textContaining('OLD-PRIVATE-WINDOW'), findsNothing);
    expect(tester.widget<ButtonStyleButton>(find.byKey(const Key('home-open-transcript'))).onPressed, null);
    expect(find.text('Not recording'), findsOneWidget);
  });
  testWidgets('Home transcript retains owned completed words without starting a new capture', (tester) async {
    final fixture = await _DockFixture.create(tester);
    fixture.makeNecklaceLive(segments: [_segment('done', 'Fictional completed words')]);
    await fixture.pump(tester, homeTranscript: true);
    fixture.provider.updateRecordingState(RecordingState.stop);
    await tester.pump();
    expect(find.text('Not recording'), findsOneWidget);
    expect(find.text('Fictional completed words'), findsOneWidget);
    await tester.tap(find.byKey(const Key('home-open-transcript')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(find.byKey(const Key('upstream-capture-transcript-list')), findsOneWidget);
    expect(
        find.descendant(
            of: find.byKey(const Key('upstream-capture-transcript-list')),
            matching: find.text('Fictional completed words')),
        findsOneWidget);
    await tester.tap(find.byKey(const Key('upstream-capture-transcript-close')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(fixture.runtime.captureSession, null);
  });

  testWidgets('Home transcript paused state is prominent and never resumes from viewing', (tester) async {
    final fixture = await _DockFixture.create(tester);
    fixture.makeNecklaceLive(segments: [_segment('paused', 'Fictional retained paused words')]);
    fixture.provider.committedPaused = true;
    await fixture.pump(tester, homeTranscript: true);
    expect(find.text('Paused'), findsOneWidget);
    expect(find.text('Transcription connected'), findsNothing);
    await tester.tap(find.byKey(const Key('home-open-transcript')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(fixture.provider.committedPaused, isTrue);
    expect(find.text('Fictional retained paused words'), findsWidgets);
  });

  testWidgets('Home transcript replaces provider and permanently retires the old open viewer', (tester) async {
    final fixture = await _DockFixture.create(tester);
    fixture.makeNecklaceLive(segments: [_segment('first', 'SYNTHETIC-RETIRED-PROVIDER')]);
    await fixture.pump(tester, homeTranscript: true);
    await tester.tap(find.byKey(const Key('home-open-transcript')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    final replacement = await _DockFixture.create(tester);
    replacement.makeNecklaceLive(segments: [_segment('next', 'Fictional current provider words')]);
    fixture.runtime.replacementCapture = replacement.provider;
    fixture.provider.notifyListeners();
    await tester.pump();
    expect(find.textContaining('SYNTHETIC-RETIRED-PROVIDER'), findsNothing);
    await tester.tap(find.byKey(const Key('upstream-capture-transcript-close')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(find.text('Fictional current provider words'), findsOneWidget);
    await tester.tap(find.byKey(const Key('home-open-transcript')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(
        find.descendant(
            of: find.byKey(const Key('upstream-capture-transcript-list')),
            matching: find.text('Fictional current provider words')),
        findsOneWidget);
    await tester.tap(find.byKey(const Key('upstream-capture-transcript-close')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    fixture.runtime.replacementCapture = fixture.provider;
    replacement.provider.notifyListeners();
    await tester.pump();
    // The closed viewer cannot restore an old read capability implicitly.
    expect(fixture.provider.committedPaused, false);
  });

  for (final healthCase in [
    ('audio_not_observed', 'Necklace connected · waiting for audio', false),
    ('audio_recent', 'Receiving necklace audio', false),
    ('audio_stale', 'Audio stalled — check necklace', true),
  ]) {
    testWidgets('Home transcript separates audio and transcription ${healthCase.$1}', (tester) async {
      final fixture = await _DockFixture.create(tester);
      fixture.makeNecklaceLive();
      fixture.provider.captureReady = healthCase.$3;
      fixture.runtime.healthOverride = EllaCaptureDiagnosticsSnapshot.upstream(
          ready: healthCase.$3,
          receivedBytes: 1,
          sentBytes: 0,
          audioHealth: {'ble_link': 'connected', 'stream_health': healthCase.$1, 'capture_expected': true});
      await fixture.pump(tester, homeTranscript: true);
      expect(find.text(healthCase.$2), findsOneWidget);
      expect(find.textContaining('Transcription'), findsNothing);
      expect(find.text('No live transcript yet'), findsNothing);
      await tester.tap(find.byKey(const Key('home-open-transcript')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      expect(find.byKey(const Key('upstream-capture-transcript-empty')), findsOneWidget);
      await tester.tap(find.byKey(const Key('home-transcript-audio-details')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      expect(find.textContaining(healthCase.$3 ? 'Transcription service ready' : 'Transcription not confirmed'),
          findsOneWidget);
      expect(find.textContaining("recording didn't start"), findsNothing);
    });
  }

  testWidgets('Home transcript current viewer retires words on exact authority loss', (tester) async {
    final authority = _DockSessionAuthority();
    final fixture = await _DockFixture.create(tester, authorityOverride: authority);
    fixture.makeNecklaceLive(segments: [_segment('home-private', 'SYNTHETIC-OWNED-TEXT')]);
    await fixture.pump(tester, homeTranscript: true);
    await tester.tap(find.byKey(const Key('home-open-transcript')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    authority.current = false;
    fixture.provider.notifyListeners();
    await tester.pump();
    expect(find.text('SYNTHETIC-OWNED-TEXT'), findsNothing);
    await tester.tap(find.byKey(const Key('upstream-capture-transcript-close')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(tester.widget<ButtonStyleButton>(find.byKey(const Key('home-open-transcript'))).onPressed, null);
  });
  for (final scale in [1.0, 3.5]) {
    for (final dark in [false, true]) {
      testWidgets('Home transcript readable scroll preview realfont $scale dark$dark', (tester) async {
        final fixture = await _DockFixture.create(tester);
        fixture.makeNecklaceLive(segments: [_segment('long', 'Fictional blue lantern. ' * 100)]);
        await fixture.pump(tester, homeTranscript: true, textScale: scale, dark: dark);
        final button = find.byKey(const Key('home-open-transcript'));
        expect(button.hitTestable(), findsOneWidget);
        expect(tester.getSize(button).width, greaterThanOrEqualTo(48));
        final stockPause = find.byTooltip('Pause');
        expect(stockPause.hitTestable(), findsOneWidget);
        expect(tester.getSize(stockPause).height, greaterThanOrEqualTo(44));
        final beforeOutput = Platform.environment['ELLA_RECORDING_UI_SNAPSHOTS'];
        if (beforeOutput != null) {
          await tester.runAsync(() async {
            await Directory(beforeOutput).create(recursive: true);
            final boundary =
                tester.renderObject<RenderRepaintBoundary>(find.byKey(const Key('capture-fixture-render')));
            final image = await boundary.toImage(pixelRatio: 2);
            final data = await image.toByteData(format: ImageByteFormat.png);
            await File('$beforeOutput/home-stock-control-$scale-${dark ? 'dark' : 'light'}.png')
                .writeAsBytes(data!.buffer.asUint8List());
            image.dispose();
          });
        }
        final scroll = find.byKey(const Key('home-transcript-preview-scroll'));
        await tester.drag(scroll, const Offset(0, -150));
        await tester.pump();
        expect(tester.takeException(), null);
        final output = Platform.environment['ELLA_RECORDING_UI_SNAPSHOTS'];
        if (output != null) {
          await tester.runAsync(() async {
            await Directory(output).create(recursive: true);
            final boundary =
                tester.renderObject<RenderRepaintBoundary>(find.byKey(const Key('capture-fixture-render')));
            final image = await boundary.toImage(pixelRatio: 2);
            final data = await image.toByteData(format: ImageByteFormat.png);
            await File('$output/home-transcript-$scale-${dark ? 'dark' : 'light'}.png')
                .writeAsBytes(data!.buffer.asUint8List());
            image.dispose();
          });
        }
      });
    }
  }

  setUp(() => guardian_native.GuardianModeService.whisperStateFence.resetForTesting());
  registerEllaCaptureProtocolSocketCases();
  registerUpstreamCaptureProtocolV2Cases();

  setUpAll(() async {
    // Portable SF geometry surrogate; not native iOS font proof.
    await (FontLoader('.SF UI Text')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf')))
        .load();
    await (FontLoader('InstrumentSerif')
          ..addFont(rootBundle.load('assets/fonts/instrument-serif/InstrumentSerif-Regular.ttf')))
        .load();
    var flutterCache = File(Platform.resolvedExecutable).parent;
    while (!File('${flutterCache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf').existsSync()) {
      flutterCache = flutterCache.parent;
    }
    final materialIcons = File('${flutterCache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf');
    await (FontLoader(
      'MaterialIcons',
    )..addFont(materialIcons.readAsBytes().then((bytes) => ByteData.sublistView(Uint8List.fromList(bytes)))))
        .load();
  });

  List<String> captureHaptics(WidgetTester tester, {bool fail = false}) {
    final calls = <String>[];
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(SystemChannels.platform, (call) async {
      if (call.method == 'HapticFeedback.vibrate') {
        calls.add(call.arguments as String);
        if (fail) throw PlatformException(code: 'feedback-unavailable');
      }
      return null;
    });
    addTearDown(() => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(SystemChannels.platform, null));
    return calls;
  }

  testWidgets('capture capsule is compact and opening or dismissing never starts capture', (tester) async {
    final fixture = await _DockFixture.create(tester);
    var starts = 0;
    fixture.runtime.startPhoneOverride = (_) async {
      starts++;
      return EllaCaptureStartOutcome.started;
    };
    await fixture.pump(tester, compactPresentation: true);
    expect(find.text('Not recording'), findsOneWidget);
    expect(find.byKey(const Key('upstream-capture-record-phone')), findsNothing);
    expect(find.byKey(const Key('upstream-capture-connect-necklace')), findsNothing);
    expect(tester.getSize(find.byKey(const Key('upstream-capture-dock'))).height, lessThan(130));
    for (var repeat = 0; repeat < 3; repeat++) {
      await tester.tap(find.byKey(const Key('upstream-capture-open-controls')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 350));
      expect(find.byKey(const Key('upstream-capture-controls-sheet')), findsOneWidget);
      expect(find.byKey(const Key('upstream-capture-record-phone')), findsNothing);
      await tester.tap(find.byKey(const Key('iris-status-details')));
      await tester.pump(const Duration(milliseconds: 350));
      expect(find.byKey(const Key('upstream-capture-record-phone')), findsNothing);
      expect(find.byKey(const Key('upstream-capture-connect-necklace')), findsNothing);
      expect(find.text('Sound'), findsOneWidget);
      expect(find.text('Waiting to send'), findsOneWidget);
      await tester.tap(find.byKey(const Key('upstream-capture-controls-close')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 350));
      expect(find.byKey(const Key('upstream-capture-controls-sheet')), findsNothing);
    }
    expect(starts, 0);
    expect(tester.takeException(), isNull);
  });

  for (final selected in [_pickerNecklace, _pickerFriend]) {
    testWidgets('hardware Connect retires old page before same-account fresh capture epoch ${selected.type}',
        (tester) async {
      final fixture = await _DockFixture.create(tester);
      final picker = _PickerHarness([selected]);
      WidgetController.hitTestWarningShouldBeFatal = true;
      addTearDown(() => WidgetController.hitTestWarningShouldBeFatal = false);
      final authority = fixture.authority as _DockSessionAuthority;
      picker.provider.connectOverride = () async {
        authority.epoch++;
        fixture.makeNecklaceLive();
        return true;
      };
      await fixture.pump(tester, compactPresentation: true, reduceMotion: true, onboarding: picker.onboarding);
      unawaited(IrisCaptureControls.shared.openNecklace!());
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 450));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 450));
      final connect = find.byKey(const Key('upstream-capture-connect-necklace'));
      await tester.ensureVisible(connect);
      await tester.tap(connect);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 450));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 450));
      for (var frame = 0; frame < 4; frame++) {
        await tester.pump(const Duration(milliseconds: 350));
      }
      expect(find.byType(ConnectDevicePage), findsOneWidget);
      // The old page must already be gone, before any lease change.
      expect(find.byType(QuietMemoryAuthorityFence, skipOffstage: false), findsNothing);
      await tester.tap(find.text(selected.name).last);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 450));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 450));
      await tester.pump(const Duration(milliseconds: 400));
      expect(picker.provider.connects, 1);
      expect(authority.epoch, 1);
      await tester.pump(const Duration(seconds: 2));
      await tester.pump();
      expect(picker.onboarding.isConnected, isTrue);
      if (find.text('I Understand').evaluate().isNotEmpty) {
        await tester.tap(find.text('I Understand'));
        await tester.pump(const Duration(milliseconds: 450));
        await tester.pump();
      }
      for (var frame = 0; frame < 4; frame++) {
        await tester.pump(const Duration(milliseconds: 350));
      }
      // Stock DEVICE_CONNECTED notification returns to the current Home.
      for (var frame = 0; frame < 4; frame++) {
        await tester.pump(const Duration(milliseconds: 350));
      }
      expect(find.byType(ConnectDevicePage), findsNothing);
      expect(find.text('Recording is unavailable right now.'), findsNothing);
      expect(IrisCaptureControls.shared.listening, IrisListeningState.active);
      unawaited(IrisCaptureControls.shared.openNecklace!());
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 450));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 450));
      expect(find.byKey(const Key('upstream-capture-disconnect-necklace')), findsOneWidget);
      expect(find.text('Recording is unavailable right now.'), findsNothing);
      // An unrelated subsequent epoch must still retire the newly opened page.
      authority.epoch++;
      await tester.pump(const Duration(milliseconds: 400));
      expect(find.text('Recording is unavailable right now.'), findsOneWidget);
      expect(find.byKey(const Key('upstream-capture-disconnect-necklace')), findsNothing);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('hardware page still refuses changed account authority', (tester) async {
    final fixture = await _DockFixture.create(tester);
    var uid = _uid;
    await fixture.pump(tester, compactPresentation: true, reduceMotion: true, authenticatedUid: () => uid);
    unawaited(IrisCaptureControls.shared.openNecklace!());
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 450));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 450));
    expect(find.byKey(const Key('upstream-capture-connect-necklace')), findsOneWidget);
    uid = 'different-synthetic-account';
    await tester.pump(const Duration(milliseconds: 400));
    expect(find.text('Recording is unavailable right now.'), findsOneWidget);
    expect(find.byKey(const Key('upstream-capture-connect-necklace')), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('capture sheet stays live through pending Finish and recovery', (tester) async {
    final fixture = await _DockFixture.create(tester);
    fixture.makePhoneLive();
    final finish = Completer<void>();
    var finishes = 0;
    fixture.runtime.finishOverride = () {
      finishes++;
      return finish.future;
    };
    await fixture.pump(tester, compactPresentation: true, reduceMotion: true);
    unawaited(IrisCaptureControls.shared.openNecklace!());
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 350));
    await tester.drag(find.byType(SingleChildScrollView).last, const Offset(0, -400));
    await tester.pump(const Duration(milliseconds: 350));
    await tester.ensureVisible(find.byKey(const Key('upstream-capture-finish')));
    await tester.pump();
    await tester.tap(find.byKey(const Key('upstream-capture-finish')));
    await tester.pump();
    expect(find.text('Finishing and saving…'), findsWidgets);
    final button = tester.widget<FilledButton>(find.byKey(const Key('upstream-capture-finish')));
    expect(button.onPressed, isNull);
    expect(finishes, 1);
    finish.completeError(StateError('synthetic finish failure'));
    await tester.pump();
    await tester.pump();
    expect(find.text('Recording is unavailable right now.'), findsWidgets);
    expect(tester.widget<FilledButton>(find.byKey(const Key('upstream-capture-finish'))).onPressed, isNull);
    expect(finishes, 1);
    await tester.pageBack();
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 350));
    expect(find.text('Recording is unavailable right now.'), findsWidgets);
    expect(tester.takeException(), isNull);
  });

  for (final size in [const Size(390, 844), const Size(320, 568)]) {
    testWidgets('capture capsule and sheet remain usable at large text $size', (tester) async {
      final fixture = await _DockFixture.create(tester);
      await fixture.pump(tester, compactPresentation: true, size: size, textScale: 3, reduceMotion: true);
      expect(tester.getSize(find.byKey(const Key('upstream-capture-open-controls'))).height, greaterThanOrEqualTo(48));
      await tester.tap(find.byKey(const Key('upstream-capture-open-controls')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 350));
      if (find.byKey(const Key('iris-status-details')).evaluate().isNotEmpty) {
        await tester.ensureVisible(find.byKey(const Key('iris-status-details')));
        await tester.tap(find.byKey(const Key('iris-status-details')));
        await tester.pump(const Duration(milliseconds: 350));
      }
      expect(find.byKey(const Key('upstream-capture-controls-close')), findsOneWidget);
      expect(find.byKey(const Key('upstream-capture-record-phone')), findsNothing);
      await tester.ensureVisible(find.text('Send a support report'));
      expect(tester.takeException(), isNull);
    });
  }

  for (final dark in [false, true]) {
    testWidgets('explicit consent Review is visible and tappable at large text dark=$dark', (tester) async {
      final fixture = await _DockFixture.create(tester);
      var reviews = 0;
      var starts = 0;
      fixture.runtime.startPhoneOverride = (_) async {
        starts++;
        return EllaCaptureStartOutcome.started;
      };
      await fixture.pump(tester,
          compactPresentation: true,
          dark: dark,
          size: const Size(320, 568),
          textScale: 2,
          guardianAuthorityProvider: () => null,
          consentRequester: (_) async {
            reviews++;
            return false;
          });
      await tester.tap(find.byKey(const Key('upstream-capture-open-controls')));
      await tester.pumpAndSettle();
      final review = find.byKey(const Key('upstream-capture-review-consent'));
      await tester.ensureVisible(review);
      expect(review.hitTestable(), findsOneWidget);
      final button = tester.widget<TextButton>(review);
      expect(button.onPressed, isNotNull);
      final context = tester.element(review);
      expect(button.style!.foregroundColor!.resolve({}), EllaPalette.of(context).ink);
      expect(find.descendant(of: review, matching: find.text('Review AI permission')), findsOneWidget);
      await tester.tap(review);
      await tester.pumpAndSettle();
      expect(reviews, 1);
      expect(starts, 0);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('capture sheet invalidates same-account retired authority without auth event', (tester) async {
    final fixture = await _DockFixture.create(tester);
    var current = true;
    final owner = _WhisperTestAuthority(_uid, () => current);
    await fixture.pump(tester, compactPresentation: true, guardianAuthorityProvider: () => owner);
    await tester.tap(find.byKey(const Key('upstream-capture-open-controls')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 350));
    if (find.byKey(const Key('iris-status-details')).evaluate().isNotEmpty) {
      await tester.ensureVisible(find.byKey(const Key('iris-status-details')));
      await tester.tap(find.byKey(const Key('iris-status-details')));
      await tester.pump(const Duration(milliseconds: 350));
    }
    expect(find.byKey(const Key('iris-status-details')), findsOneWidget);
    current = false;
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.byKey(const Key('iris-status-details')), findsNothing);
    expect(find.text('Recording is unavailable right now.'), findsWidgets);
    expect(tester.takeException(), isNull);
  });

  testWidgets('live capture capsule exposes the one-action Pause sheet at3x on narrow screen', (tester) async {
    final fixture = await _DockFixture.create(tester);
    fixture.makePhoneLive();
    await fixture.pump(tester, compactPresentation: true, size: const Size(320, 568), textScale: 3, reduceMotion: true);
    expect(find.text('Phone microphone on'), findsOneWidget);
    expect(find.byKey(const Key('upstream-capture-capsule-stop')), findsNothing);
    await tester.tap(find.byKey(const Key('upstream-capture-open-controls')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('iris-status-pause-resume')), findsOneWidget);
    expect(find.byKey(const Key('iris-status-stop-capture')), findsNothing);
    expect(tester.takeException(), isNull);
  });

  for (final live in [false, true]) {
    for (final height in [126.0, 150.0]) {
      testWidgets('bounded production capsule ${live ? 'live' : 'idle'} retains status and Stop at $height',
          (tester) async {
        final fixture = await _DockFixture.create(tester);
        var starts = 0;
        var stops = 0;
        fixture.runtime.startPhoneOverride = (_) async {
          starts++;
          return EllaCaptureStartOutcome.started;
        };
        fixture.runtime.stopPhoneOverride = () async => stops++;
        if (live) fixture.makePhoneLive();
        await fixture.pump(tester,
            compactPresentation: true,
            size: const Size(320, 568),
            textScale: 3.5,
            reduceMotion: true,
            dockMaxHeight: height);
        expect(tester.takeException(), isNull, reason: 'The real capsule must obey its finite keyboard budget.');
        expect(starts, 0);
        expect(stops, 0);
        final dock = find.byKey(const Key('upstream-capture-dock'));
        final statusScroll = find.descendant(of: dock, matching: find.byType(Scrollable));
        expect(statusScroll, findsOneWidget);
        // A positive viewport alone can leave every glyph clipped. Preserve a
        // complete line at the user's actual chosen font size, without scaling
        // the text down to make this assertion pass.
        final label = tester.widget<Text>(find.descendant(of: statusScroll, matching: find.byType(Text)).first);
        final line = TextPainter(
          text: TextSpan(text: 'Ag', style: label.style),
          textScaler: const TextScaler.linear(3.5),
          textDirection: TextDirection.ltr,
        )..layout();
        expect(tester.getSize(statusScroll).height, greaterThanOrEqualTo(line.height));
        line.dispose();
        final position = tester.state<ScrollableState>(statusScroll).position;
        expect(position.maxScrollExtent, greaterThan(0));
        await tester.drag(statusScroll, const Offset(0, -400));
        await tester.pump();
        expect(position.pixels, greaterThan(0));
        expect(starts, 0);
        expect(stops, 0);
        if (live) {
          expect(find.byKey(const Key('upstream-capture-capsule-stop')), findsNothing);
          final controls = find.byKey(const Key('upstream-capture-open-controls'));
          expect(tester.getSize(controls).height, greaterThanOrEqualTo(48));
          // The button is taller than its scroll viewport; exercise the visible
          // touch region rather than its offscreen geometric center.
          await tester.tapAt(tester.getCenter(statusScroll));
          await tester.pumpAndSettle();
          expect(find.byKey(const Key('upstream-capture-controls-sheet')), findsOneWidget);
          expect(starts, 0);
          expect(stops, 0);
        }
        expect(tester.takeException(), isNull);
      });
    }
  }

  for (final dark in [false, true]) {
    testWidgets('capture capsule renders actual glyphs ${dark ? 'dark' : 'light'}', (tester) async {
      final fixture = await _DockFixture.create(tester);
      await fixture.pump(tester, compactPresentation: true, dark: dark, reduceMotion: true);
      Future<void> capture(String suffix) async {
        await tester.runAsync(() async {
          final boundary = tester.renderObject<RenderRepaintBoundary>(find.byKey(const Key('capture-fixture-render')));
          final image = await boundary.toImage();
          final bytes = await image.toByteData(format: ImageByteFormat.png);
          await File(
                  '/Users/ellaai/Documents/Codex/2026-10-07/task-4/iris-follow-on-capture-source-${dark ? 'dark' : 'light'}-$suffix.png')
              .writeAsBytes(bytes!.buffer.asUint8List());
          image.dispose();
        });
      }

      await capture('capsule');
      await tester.tap(find.byKey(const Key('upstream-capture-open-controls')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 350));
      await capture('sheet');
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('compact Record keeps a native button with a 20px icon and 17px label', (tester) async {
    final fixture = await _DockFixture.create(tester);
    await fixture.pump(tester);
    final record = find.byKey(const Key('upstream-capture-record-phone'));
    final button = tester.widget<FilledButton>(record);
    expect(button.style!.iconSize!.resolve({}), 20);
    expect(button.style!.textStyle!.resolve({})!.fontSize, 17);
    expect(tester.getSize(record).height, inInclusiveRange(52, 56));
    expect(find.descendant(of: record, matching: find.text('Record')), findsOneWidget);
    expect(tester.getSize(find.byKey(const Key('upstream-capture-connect-necklace'))).height, greaterThanOrEqualTo(48));
  });

  testWidgets('Record press is paint-only and cancelled press never captures or buzzes', (tester) async {
    final calls = captureHaptics(tester);
    final fixture = await _DockFixture.create(tester);
    var starts = 0;
    fixture.runtime.startPhoneOverride = (_) async {
      starts++;
      return EllaCaptureStartOutcome.started;
    };
    await fixture.pump(tester, reduceMotion: false);
    final record = find.byKey(const Key('upstream-capture-record-phone'));
    final scale = find.ancestor(of: record, matching: find.byType(AnimatedScale)).first;
    final before = tester.getRect(record);
    final dockSize = tester.getSize(find.byKey(const Key('upstream-capture-dock')));
    final gesture = await tester.startGesture(tester.getCenter(record));
    await tester.pump(const Duration(milliseconds: 90));
    expect(tester.widget<AnimatedScale>(scale).scale, 0.98);
    expect(tester.widget<AnimatedScale>(scale).duration, const Duration(milliseconds: 90));
    expect(tester.getSize(record), before.size);
    expect(tester.getSize(find.byKey(const Key('upstream-capture-dock'))), dockSize);
    expect(starts, 0);
    expect(calls, isEmpty);
    await gesture.cancel();
    await tester.pump();
    expect(tester.widget<AnimatedScale>(scale).scale, 1);
    expect(tester.widget<AnimatedScale>(scale).duration, const Duration(milliseconds: 140));
    await tester.pump(const Duration(milliseconds: 140));
    expect(tester.getRect(record), before);
    expect(starts, 0);
    expect(calls, isEmpty);
  });

  testWidgets('active phone Stop and Finish keep compact native controls and full semantics', (tester) async {
    final fixture = await _DockFixture.create(tester);
    fixture.makePhoneLive();
    await fixture.pump(tester);
    for (final key in ['upstream-capture-stop-phone', 'upstream-capture-finish']) {
      final button = find.byKey(Key(key));
      expect(tester.getSize(button).height, inInclusiveRange(52, 56));
      final label = tester.widget<Text>(find.descendant(of: button, matching: find.byType(Text)));
      expect(label.data, key.endsWith('phone') ? 'Stop' : 'Finish');
      expect(
          label.semanticsLabel,
          key.endsWith('phone')
              ? AppLocalizations.of(tester.element(button)).upstreamCaptureStop
              : AppLocalizations.of(tester.element(button)).upstreamCaptureFinish);
    }
  });

  for (final retirement in ['drag_off', 'account', 'epoch', 'state_replacement', 'dispose']) {
    testWidgets('Record $retirement before release produces no capture or haptic', (tester) async {
      final calls = captureHaptics(tester);
      final authority = _PressTestAuthority();
      final fixture = await _DockFixture.create(tester, authorityOverride: authority);
      var uid = _uid;
      var starts = 0;
      var stops = 0;
      fixture.runtime.startPhoneOverride = (_) async {
        starts++;
        return EllaCaptureStartOutcome.started;
      };
      fixture.runtime.stopPhoneOverride = () async => stops++;
      await fixture.pump(tester, authenticatedUid: () => uid);
      final button = find.byKey(const Key('upstream-capture-record-phone'));
      final gesture = await tester.startGesture(tester.getCenter(button));
      await tester.pump(const Duration(milliseconds: 90));
      switch (retirement) {
        case 'drag_off':
          await gesture.moveBy(const Offset(0, -120));
        case 'account':
          uid = 'replacement-owner';
        case 'epoch':
          authority.epoch++;
        case 'state_replacement':
          fixture.makePhoneLive();
        case 'dispose':
          await tester.pumpWidget(const SizedBox.shrink());
      }
      await tester.pump();
      await gesture.up();
      await tester.pump(const Duration(milliseconds: 140));
      expect(starts, 0);
      expect(stops, 0);
      expect(calls, isEmpty);
      expect(tester.takeException(), isNull);
    });
  }

  for (final reduceMotion in [false, true]) {
    testWidgets('Record completed activation respects reduced motion=$reduceMotion', (tester) async {
      final calls = captureHaptics(tester);
      final fixture = await _DockFixture.create(tester);
      var starts = 0;
      fixture.runtime.startPhoneOverride = (_) async {
        starts++;
        return EllaCaptureStartOutcome.started;
      };
      await fixture.pump(tester, reduceMotion: reduceMotion);
      final button = find.byKey(const Key('upstream-capture-record-phone'));
      final scale = find.ancestor(of: button, matching: find.byType(AnimatedScale)).first;
      final gesture = await tester.startGesture(tester.getCenter(button));
      await tester.pump(const Duration(milliseconds: 90));
      expect(tester.widget<AnimatedScale>(scale).scale, reduceMotion ? 1 : 0.98);
      expect(tester.widget<AnimatedScale>(scale).duration,
          reduceMotion ? Duration.zero : const Duration(milliseconds: 90));
      expect(tester.widget<FilledButton>(button).style!.animationDuration,
          reduceMotion ? Duration.zero : const Duration(milliseconds: 90));
      expect(starts, 0);
      expect(calls, isEmpty);
      await gesture.up();
      await tester.pump();
      expect(starts, 1);
      expect(calls, ['HapticFeedbackType.lightImpact']);
      expect(tester.widget<AnimatedScale>(scale).scale, 1);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('native Record keyboard activation keeps the full action semantics and one haptic', (tester) async {
    final semantics = tester.ensureSemantics();
    final calls = captureHaptics(tester);
    final fixture = await _DockFixture.create(tester);
    var starts = 0;
    fixture.runtime.startPhoneOverride = (_) async {
      starts++;
      return EllaCaptureStartOutcome.started;
    };
    await fixture.pump(tester);
    final button = find.byKey(const Key('upstream-capture-record-phone'));
    expect(
        tester.getSemantics(button),
        matchesSemantics(
            label: 'Record with phone',
            isButton: true,
            hasTapAction: true,
            isEnabled: true,
            hasEnabledState: true,
            isFocusable: true,
            hasFocusAction: true));
    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await tester.pump();
    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await tester.pump();
    expect(starts, 1);
    expect(calls, ['HapticFeedbackType.lightImpact']);
    semantics.dispose();
  });

  testWidgets('Record hover is not a press or accepted activation', (tester) async {
    final calls = captureHaptics(tester);
    final fixture = await _DockFixture.create(tester);
    var starts = 0;
    fixture.runtime.startPhoneOverride = (_) async {
      starts++;
      return EllaCaptureStartOutcome.started;
    };
    await fixture.pump(tester);
    final button = find.byKey(const Key('upstream-capture-record-phone'));
    final mouse = await tester.createGesture(kind: PointerDeviceKind.mouse);
    await mouse.addPointer(location: Offset.zero);
    await mouse.moveTo(tester.getCenter(button));
    await tester.pump(const Duration(milliseconds: 140));
    expect(
        tester.widget<AnimatedScale>(find.ancestor(of: button, matching: find.byType(AnimatedScale)).first).scale, 1);
    expect(starts, 0);
    expect(calls, isEmpty);
    await mouse.removePointer();
  });

  for (final width in [320.0, 390.0]) {
    testWidgets('compact native controls stay accessible in RTL 3x at width=$width', (tester) async {
      final fixture = await _DockFixture.create(tester);
      await fixture.pump(tester,
          size: Size(width, width == 320 ? 568 : 844), textScale: 3, locale: const Locale('ar'), reduceMotion: true);
      final record = find.byKey(const Key('upstream-capture-record-phone'));
      final connect = find.byKey(const Key('upstream-capture-connect-necklace'));
      expect(record.hitTestable(), findsOneWidget);
      expect(connect.hitTestable(), findsOneWidget);
      expect(tester.getSize(record).height, greaterThanOrEqualTo(52));
      expect(tester.getSize(connect).height, greaterThanOrEqualTo(48));
      expect(tester.getRect(record).bottom, lessThanOrEqualTo(tester.getRect(connect).top));
      final text = tester.widget<Text>(find.descendant(of: record, matching: find.byType(Text)));
      expect(text.semanticsLabel, AppLocalizations.of(tester.element(record)).upstreamCaptureRecordPhone);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('native Record semantic activation dispatches exactly one accepted haptic', (tester) async {
    final semantics = tester.ensureSemantics();
    final calls = captureHaptics(tester);
    final fixture = await _DockFixture.create(tester);
    final pending = Completer<EllaCaptureStartOutcome>();
    addTearDown(() {
      if (!pending.isCompleted) pending.complete(EllaCaptureStartOutcome.unavailable);
    });
    var starts = 0;
    fixture.runtime.startPhoneOverride = (_) {
      starts++;
      return pending.future;
    };
    await fixture.pump(tester);
    final button = find.byKey(const Key('upstream-capture-record-phone'));
    final node = tester.getSemantics(button);
    tester.binding.performSemanticsAction(
        SemanticsActionEvent(type: SemanticsAction.tap, viewId: tester.view.viewId, nodeId: node.id));
    await tester.pump();
    expect(starts, 1);
    expect(calls, ['HapticFeedbackType.lightImpact']);
    expect(tester.widget<FilledButton>(button).onPressed, isNull);
    await tester.tap(button);
    await tester.pump();
    expect(starts, 1);
    expect(calls, hasLength(1));
    pending.complete(EllaCaptureStartOutcome.unavailable);
    await tester.pump();
    await tester.pump();
    final enabledNode = tester.getSemantics(button);
    tester.binding.performSemanticsAction(
        SemanticsActionEvent(type: SemanticsAction.tap, viewId: tester.view.viewId, nodeId: enabledNode.id));
    await tester.pump();
    expect(starts, 2);
    expect(calls, hasLength(2));
    semantics.dispose();
  });

  testWidgets('accepted phone tap gives one light feedback, never boot background or double tap', (tester) async {
    final calls = captureHaptics(tester);
    final fixture = await _DockFixture.create(tester);
    final start = Completer<EllaCaptureStartOutcome>();
    addTearDown(() {
      if (!start.isCompleted) start.complete(EllaCaptureStartOutcome.unavailable);
    });
    fixture.runtime.startPhoneOverride = (_) => start.future;
    await fixture.pump(tester, reduceMotion: true);
    expect(calls, isEmpty);
    final button = find.byKey(const Key('upstream-capture-record-phone'));
    await tester.tap(button);
    await tester.pump();
    await tester.tap(button);
    await tester.pump();
    expect(calls, ['HapticFeedbackType.lightImpact']);
    fixture.runtime.protocolUnavailable.value = true;
    await tester.pump();
    expect(calls, hasLength(1));
    start.complete(EllaCaptureStartOutcome.unavailable);
    await tester.pump();
    await tester.pump();
    expect(calls, hasLength(1));
    expect(tester.takeException(), isNull);
  });

  for (final action in ['stop-phone', 'disconnect-necklace', 'finish']) {
    testWidgets('accepted $action provides feedback once without claiming completion', (tester) async {
      final calls = captureHaptics(tester);
      final fixture = await _DockFixture.create(tester);
      final completion = Completer<void>();
      addTearDown(() {
        if (!completion.isCompleted) completion.complete();
      });
      fixture.runtime.stopPhoneOverride = () => completion.future;
      fixture.runtime.disconnectNecklaceOverride = () => completion.future;
      fixture.runtime.finishOverride = () => completion.future;
      await fixture.pump(tester, reduceMotion: true);
      action == 'stop-phone' ? fixture.makePhoneLive() : fixture.makeNecklaceLive();
      await tester.pump();
      expect(calls, isEmpty);
      await tester.tap(find.byKey(Key('upstream-capture-$action')));
      await tester.pump();
      expect(calls, ['HapticFeedbackType.lightImpact']);
      expect(find.text('Recording with necklace'), findsNothing);
      completion.completeError(StateError('synthetic operation failure'));
      await tester.pump();
      await tester.pump();
      expect(calls, hasLength(1));
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('a rejected platform haptic cannot fail phone capture or create another feedback', (tester) async {
    final calls = captureHaptics(tester, fail: true);
    final fixture = await _DockFixture.create(tester);
    var starts = 0;
    fixture.runtime.startPhoneOverride = (_) async {
      starts++;
      return EllaCaptureStartOutcome.started;
    };
    await fixture.pump(tester, reduceMotion: true);
    await tester.tap(find.byKey(const Key('upstream-capture-record-phone')));
    await tester.pump();
    await tester.pump();
    expect(starts, 1);
    expect(calls, hasLength(1));
    expect(tester.takeException(), isNull);
  });

  testWidgets('user transcript and Connect navigation give feedback; provider updates do not', (tester) async {
    final calls = captureHaptics(tester);
    final fixture = await _DockFixture.create(tester);
    final picker = _PickerHarness([]);
    await fixture.pump(tester, reduceMotion: true, onboarding: picker.onboarding);
    await tester.tap(find.byKey(const Key('upstream-capture-connect-necklace')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(calls, hasLength(1));
    Navigator.of(tester.element(find.byType(FindDevicesPage))).pop();
    await tester.pumpAndSettle();
    expect(find.byType(FindDevicesPage), findsNothing);
    fixture.makePhoneLive();
    await tester.pump();
    expect(calls, hasLength(1));
    final openTranscript =
        tester.widget<OutlinedButton>(find.byKey(const Key('upstream-capture-view-transcript'))).onPressed!;
    openTranscript();
    openTranscript();
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(calls, hasLength(2));
    expect(find.byKey(const Key('upstream-capture-transcript-close')), findsOneWidget);
    final closeTranscript =
        tester.widget<IconButton>(find.byKey(const Key('upstream-capture-transcript-close'))).onPressed!;
    closeTranscript();
    closeTranscript();
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(calls, hasLength(3));
    closeTranscript();
    await tester.pump();
    expect(calls, hasLength(3));
    expect(find.byKey(const Key('upstream-capture-view-transcript')), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('boot Retry feedback is user-only and empty account gives no feedback', (tester) async {
    final calls = captureHaptics(tester);
    final fixture = await _DockFixture.create(tester);
    fixture.runtime.bootOverride = () => Future.error(StateError('synthetic boot failure'));
    var uid = _uid;
    await fixture.pump(tester, reduceMotion: true, authenticatedUid: () => uid);
    expect(calls, isEmpty);
    fixture.runtime.bootOverride = () async => fixture.provider;
    final retryBoot = tester.widget<FilledButton>(find.byKey(const Key('upstream-capture-retry-boot'))).onPressed!;
    retryBoot();
    retryBoot();
    await tester.pump();
    await tester.pump();
    expect(calls, hasLength(1));
    uid = '';
    await tester.tap(find.byKey(const Key('upstream-capture-record-phone')));
    await tester.pump();
    expect(calls, hasLength(1));
  });

  testWidgets('retired transcript close dismisses once without buzzing replacement authority', (tester) async {
    final calls = captureHaptics(tester);
    final fixture = await _DockFixture.create(tester);
    var current = true;
    await fixture.pump(tester,
        reduceMotion: true, guardianAuthorityProvider: () => _WhisperTestAuthority(_uid, () => current));
    fixture.makePhoneLive();
    await tester.pump();
    await tester.tap(find.byKey(const Key('upstream-capture-view-transcript')));
    await tester.pumpAndSettle();
    expect(calls, hasLength(1));
    current = false;
    await tester.tap(find.byKey(const Key('upstream-capture-transcript-close')));
    await tester.pumpAndSettle();
    expect(calls, hasLength(1));
    expect(find.byKey(const Key('upstream-capture-transcript-close')), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('Whispers feedback occurs only for admitted user choice and Retry', (tester) async {
    final calls = captureHaptics(tester);
    final fixture = await _DockFixture.create(tester);
    final save = Completer<bool>();
    addTearDown(() {
      if (!save.isCompleted) save.complete(false);
    });
    var current = true;
    await fixture.pump(
      tester,
      reduceMotion: true,
      guardianAvailable: true,
      guardianAuthorityProvider: () => _WhisperTestAuthority(_uid, () => current),
      guardianModeLoader: () async =>
          const GuardianModeInfo(currentMode: GuardianModeKey.off, twoTierState: GuardianModeState()),
      guardianModeSetter: (_) => save.future,
      guardianNativeStart: () async => throw StateError('synthetic native unavailable'),
      guardianNativeStop: () async {},
      guardianNativeState: () => guardian_native.GuardianModeState.idle,
    );
    expect(calls, isEmpty);
    await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
    await tester.pump();
    expect(calls, hasLength(1));
    expect(find.byKey(const Key('upstream-capture-whispers-switch')), findsNothing);
    save.complete(true);
    await tester.pump();
    await tester.pump();
    expect(calls, hasLength(1));
    await tester.tap(find.byKey(const Key('upstream-capture-whispers-retry')));
    await tester.pump();
    await tester.pump();
    expect(calls, hasLength(2));
    current = false;
    await tester.tap(find.byKey(const Key('upstream-capture-whispers-retry')));
    await tester.pump();
    expect(calls, hasLength(2));
  });

  for (final caller in ['dock', 'memory', 'voice']) {
    testWidgets('$caller breathing-dot configuration obeys motion changes without a hidden ticker', (tester) async {
      Future<void> pumpDot({required bool reduceMotion, bool active = true}) async {
        await tester.pumpWidget(
          Directionality(
            textDirection: TextDirection.ltr,
            child: MediaQuery(
              data: MediaQueryData(disableAnimations: reduceMotion),
              child: EllaBreathingDot(active: active, live: caller != 'voice', size: caller == 'dock' ? 12 : 10),
            ),
          ),
        );
        await tester.pump();
      }

      await pumpDot(reduceMotion: true);
      expect(find.byType(FadeTransition), findsNothing);
      expect(tester.binding.transientCallbackCount, 0);
      await pumpDot(reduceMotion: false);
      final fade = tester.widget<FadeTransition>(find.byType(FadeTransition));
      final before = fade.opacity.value;
      await tester.pump(const Duration(milliseconds: 500));
      expect(fade.opacity.value, isNot(before));
      expect(tester.binding.transientCallbackCount, greaterThan(0));
      await pumpDot(reduceMotion: true);
      expect(find.byType(FadeTransition), findsNothing);
      expect(tester.binding.transientCallbackCount, 0);
      await pumpDot(reduceMotion: false, active: false);
      expect(find.byType(FadeTransition), findsNothing);
      expect(tester.binding.transientCallbackCount, 0);
      await tester.pumpWidget(const SizedBox.shrink());
    });
  }

  testWidgets('actual Ella theme gives every idle action an AA palette and a 48 point target', (tester) async {
    final fixture = await _DockFixture.create(tester);
    await fixture.pump(tester);

    final primaryFinder = find.byKey(const Key('upstream-capture-record-phone'));
    final secondaryFinder = find.byKey(const Key('upstream-capture-connect-necklace'));
    final primary = tester.widget<FilledButton>(primaryFinder);
    final secondary = tester.widget<OutlinedButton>(secondaryFinder);
    final primaryForeground = primary.style!.foregroundColor!.resolve({})!;
    final primaryBackground = primary.style!.backgroundColor!.resolve({})!;
    final secondaryForeground = secondary.style!.foregroundColor!.resolve({})!;
    final secondaryBackground = secondary.style!.backgroundColor!.resolve({})!;

    expect(_contrastRatio(primaryForeground, primaryBackground), greaterThanOrEqualTo(4.5));
    for (final state in [WidgetState.pressed, WidgetState.focused]) {
      final overlay = primary.style!.overlayColor!.resolve({state})!;
      final composited = Color.alphaBlend(overlay, primaryBackground);
      expect(_contrastRatio(primaryForeground, composited), greaterThanOrEqualTo(4.5));
    }
    expect(_contrastRatio(secondaryForeground, secondaryBackground), greaterThanOrEqualTo(4.5));
    expect(tester.getSize(primaryFinder).height, greaterThanOrEqualTo(EllaSizes.minTouchTarget));
    expect(tester.getSize(secondaryFinder).height, greaterThanOrEqualTo(EllaSizes.minTouchTarget));
    expect(secondary.style!.backgroundColor!.resolve({WidgetState.pressed}), isNot(secondaryBackground));
    expect(
      secondary.style!.side!.resolve({WidgetState.focused})!.width,
      greaterThan(secondary.style!.side!.resolve({})!.width),
    );
    expect(primary.style!.backgroundColor!.resolve({WidgetState.disabled}), isNot(primaryBackground));
    expect(tester.takeException(), isNull);
  });

  testWidgets('Connect keeps explicit AA colors while busy and under either platform brightness', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final start = Completer<EllaCaptureStartOutcome>();
    fixture.runtime.startPhoneOverride = (_) => start.future;
    addTearDown(tester.platformDispatcher.clearPlatformBrightnessTestValue);
    for (final brightness in [Brightness.light, Brightness.dark]) {
      tester.platformDispatcher.platformBrightnessTestValue = brightness;
      await fixture.pump(tester);
      final connect = tester.widget<OutlinedButton>(find.byKey(const Key('upstream-capture-connect-necklace')));
      final style = connect.style!;
      for (final states in [
        <WidgetState>{},
        {WidgetState.pressed},
        {WidgetState.focused},
        {WidgetState.disabled},
      ]) {
        final foreground = style.foregroundColor!.resolve(states)!;
        final background = style.backgroundColor!.resolve(states)!;
        expect(_contrastRatio(foreground, background), greaterThanOrEqualTo(4.5));
      }
      expect(
        tester.getSize(find.byKey(const Key('upstream-capture-connect-necklace'))).height,
        greaterThanOrEqualTo(EllaSizes.minTouchTarget),
      );
    }
    await tester.tap(find.byKey(const Key('upstream-capture-record-phone')));
    await tester.pump();
    expect(tester.widget<OutlinedButton>(find.byKey(const Key('upstream-capture-connect-necklace'))).onPressed, isNull);
    start.complete(EllaCaptureStartOutcome.unavailable);
    await tester.pump();
  });

  const renderSizes = <String, Size>{'320x568': Size(320, 568), '390x844': Size(390, 844), '430x932': Size(430, 932)};
  const renderScales = [1.0, 1.5, 2.0, 3.0];
  for (final sizeEntry in renderSizes.entries) {
    for (final scale in renderScales) {
      final scaleName = scale.toString().replaceAll('.', '_');
      testWidgets('idle render is stable at ${sizeEntry.key} and ${scale}x text', (tester) async {
        final fixture = await _DockFixture.create(tester);
        await fixture.pump(tester, size: sizeEntry.value, textScale: scale);

        expect(find.byKey(const Key('upstream-capture-record-phone')).hitTestable(), findsOneWidget);
        expect(find.byKey(const Key('upstream-capture-connect-necklace')).hitTestable(), findsOneWidget);
        expect(tester.takeException(), isNull);
        await expectLater(
          find.byType(MaterialApp),
          matchesGoldenFile('goldens/v7/ella_v7_capture_dock_${sizeEntry.key}_scale_$scaleName.png'),
        );
      });
    }
  }

  testWidgets('Home Connect opens the same full-screen Omi picker without connecting', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final picker = _PickerHarness([_pickerNecklace, _pickerFriend]);
    addTearDown(() {
      picker.onboarding.dispose();
      picker.provider.dispose();
    });
    await fixture.pump(tester, onboarding: picker.onboarding);

    await tester.tap(find.byKey(const Key('upstream-capture-connect-necklace')));
    await tester.pump();
    await tester.pump();
    expect(find.byType(ConnectDevicePage), findsOneWidget);
    expect(find.text('Compass'), findsOneWidget);
    expect(find.text('Friend'), findsOneWidget);
    final artwork = tester.widgetList<Image>(find.byType(Image)).map((image) => image.image).whereType<AssetImage>();
    expect(
      artwork.map((image) => image.assetName),
      contains(
        DeviceUtils.getDeviceImagePath(
          deviceType: _pickerNecklace.type,
          modelNumber: _pickerNecklace.modelNumber,
          deviceName: _pickerNecklace.name,
        ),
      ),
    );
    expect(
      artwork.map((image) => image.assetName),
      contains(
        DeviceUtils.getDeviceImagePath(
          deviceType: _pickerFriend.type,
          modelNumber: _pickerFriend.modelNumber,
          deviceName: _pickerFriend.name,
        ),
      ),
    );
    expect(picker.service.discovers, 1);
    expect(picker.provider.connects, 0);
    expect(tester.takeException(), isNull);
  });

  for (final useEllaTheme in [true, false]) {
    testWidgets('picker support and Connect Later inherit ${useEllaTheme ? 'Ella' : 'default'} button foreground', (
      tester,
    ) async {
      SharedPreferences.setMockInitialValues({});
      await fork.SharedPreferencesUtil.init();
      final picker = _PickerHarness([]);
      addTearDown(() async {
        await tester.pumpWidget(const SizedBox.shrink());
        picker.onboarding.dispose();
        picker.provider.dispose();
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });
      tester.view.physicalSize = const Size(320, 568);
      tester.view.devicePixelRatio = 1;
      final theme = useEllaTheme ? ellaThemeData() : ThemeData();
      await tester.pumpWidget(
        ChangeNotifierProvider<OnboardingProvider>.value(
          value: picker.onboarding,
          child: MaterialApp(
            theme: theme,
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            builder: (context, child) => MediaQuery(
              data: MediaQuery.of(context).copyWith(textScaler: const TextScaler.linear(2)),
              child: child!,
            ),
            home: Scaffold(
              body: SingleChildScrollView(child: FindDevicesPage(goNext: () {})),
            ),
          ),
        ),
      );
      await tester.pump();
      await tester.pump(const Duration(seconds: 10));
      for (final label in ['Contact Support?', 'Connect Later']) {
        final textFinder = find.text(label);
        final buttonFinder = find.ancestor(of: textFinder, matching: find.byType(ElevatedButton));
        expect(buttonFinder, findsOneWidget);
        expect(tester.widget<Text>(textFinder).style!.color, isNull);
        final rendered = tester.widget<RichText>(find.descendant(of: buttonFinder, matching: find.byType(RichText)));
        final foreground = rendered.text.style!.color!;
        if (useEllaTheme) {
          expect(foreground, theme.elevatedButtonTheme.style!.foregroundColor!.resolve({}));
          expect(_contrastRatio(foreground, EllaColors.tealDeep), greaterThanOrEqualTo(4.5));
        } else {
          expect(foreground, theme.colorScheme.primary);
        }
        expect(tester.getSize(buttonFinder).height, greaterThanOrEqualTo(EllaSizes.minTouchTarget));
        await tester.ensureVisible(buttonFinder);
        expect(buttonFinder.hitTestable(), findsOneWidget);
        final press = await tester.startGesture(tester.getCenter(buttonFinder));
        await tester.pump();
        final pressedText = tester.widget<RichText>(find.descendant(of: buttonFinder, matching: find.byType(RichText)));
        expect(pressedText.text.style!.color, foreground);
        await press.cancel();
        await tester.pumpAndSettle();
      }
      expect(picker.service.discovers, 2);
      expect(picker.provider.connects, 0);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('BLE failure offers Retry without invoking AI consent', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final picker = _PickerHarness([_pickerNecklace]);
    addTearDown(() {
      picker.onboarding.dispose();
      picker.provider.dispose();
    });
    var consentCalls = 0;
    await fixture.pump(
      tester,
      onboarding: picker.onboarding,
      consentRequester: (_) async {
        consentCalls++;
        return true;
      },
    );

    await tester.tap(find.byKey(const Key('upstream-capture-connect-necklace')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    await tester.tap(find.text('Compass'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(consentCalls, 0);
    expect(find.text('Ella could not connect to that necklace. Try again.'), findsOneWidget);
    expect(picker.provider.connects, 1);
    expect(find.text('Compass'), findsOneWidget);
    expect(find.text('Review AI permission before recording.'), findsNothing);
  });

  testWidgets('only an authority failure requests AI consent', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final picker = _PickerHarness([_pickerNecklace]);
    picker.provider.consentRequired = true;
    addTearDown(() {
      picker.onboarding.dispose();
      picker.provider.dispose();
    });
    var consentCalls = 0;
    await fixture.pump(
      tester,
      onboarding: picker.onboarding,
      consentRequester: (_) async {
        consentCalls++;
        return true;
      },
    );

    await tester.tap(find.byKey(const Key('upstream-capture-connect-necklace')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    await tester.tap(find.text('Compass'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(consentCalls, 1);
    expect(find.text('Compass'), findsOneWidget);
    expect(picker.provider.connects, 1);
  });

  testWidgets('a retired account outcome cannot prompt the replacement account for consent', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final result = Completer<EllaCaptureStartOutcome>();
    var signedInUid = _uid;
    var consentCalls = 0;
    fixture.runtime.startPhoneOverride = (_) => result.future;
    await fixture.pump(
      tester,
      authenticatedUid: () => signedInUid,
      consentRequester: (_) async {
        consentCalls++;
        return true;
      },
    );
    await tester.tap(find.byKey(const Key('upstream-capture-record-phone')));
    await tester.pump();
    signedInUid = 'replacement-account';
    result.complete(EllaCaptureStartOutcome.consentRequired);
    await tester.pump();
    await tester.pump();
    expect(consentCalls, 0);
    expect(find.text('Permission updated. Try recording again.'), findsNothing);
  });

  testWidgets('protocol failures remain visible after cancelling the shared picker', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final picker = _PickerHarness([]);
    addTearDown(() {
      picker.onboarding.dispose();
      picker.provider.dispose();
    });
    await fixture.pump(tester, onboarding: picker.onboarding);

    await tester.tap(find.byKey(const Key('upstream-capture-connect-necklace')));
    await tester.pump();
    await tester.pump();
    expect(find.byType(ConnectDevicePage), findsOneWidget);
    expect(picker.provider.connects, 0);
    await tester.pageBack();
    await tester.pump();
    await tester.pump(const Duration(seconds: 10));

    fixture.runtime.protocolUnavailable.value = true;
    await tester.pump();
    expect(find.text("Ella couldn't connect to transcription, so recording didn't start."), findsOneWidget);
  });

  testWidgets('boot failure is visible and recoverable', (tester) async {
    final fixture = await _DockFixture.create(tester);
    var attempts = 0;
    fixture.runtime.bootOverride = () async {
      attempts++;
      if (attempts == 1) throw StateError('boot failed');
      return fixture.provider;
    };
    await fixture.pump(tester);

    expect(find.byKey(const Key('upstream-capture-retry-boot')), findsOneWidget);
    await tester.tap(find.byKey(const Key('upstream-capture-retry-boot')));
    await tester.pump();
    await tester.pump();
    expect(find.byKey(const Key('upstream-capture-record-phone')), findsOneWidget);
    expect(attempts, 2);
  });

  testWidgets('single device cancel restores Connect focus without auto-connect', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final picker = _PickerHarness([_pickerNecklace]);
    addTearDown(() {
      picker.onboarding.dispose();
      picker.provider.dispose();
    });
    await fixture.pump(tester, onboarding: picker.onboarding);

    await tester.tap(find.byKey(const Key('upstream-capture-connect-necklace')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(find.byType(ConnectDevicePage), findsOneWidget);
    expect(find.text('Compass'), findsOneWidget);
    await tester.pageBack();
    await tester.pumpAndSettle();

    expect(picker.provider.connects, 0);
    final connect = tester.widget<OutlinedButton>(find.byKey(const Key('upstream-capture-connect-necklace')));
    expect(connect.focusNode!.hasFocus, isTrue);
  });

  testWidgets('account replacement fences a stale full-screen device selection', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final picker = _PickerHarness([_pickerNecklace]);
    addTearDown(() {
      picker.onboarding.dispose();
      picker.provider.dispose();
    });
    var uid = _uid;
    var consentCalls = 0;
    await fixture.pump(
      tester,
      onboarding: picker.onboarding,
      authenticatedUid: () => uid,
      consentRequester: (_) async {
        consentCalls++;
        return true;
      },
    );
    await tester.tap(find.byKey(const Key('upstream-capture-connect-necklace')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    uid = 'replacement-account';
    await tester.tap(find.text('Compass'));
    await tester.pump();
    expect(picker.provider.connects, 0);
    expect(consentCalls, 0);
    expect(find.byType(ConnectDevicePage), findsOneWidget);
  });

  testWidgets('the shared picker reports scan errors and retries through one device service', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final picker = _PickerHarness([_pickerNecklace]);
    picker.service.failDiscovery = true;
    addTearDown(() {
      picker.onboarding.dispose();
      picker.provider.dispose();
    });
    await fixture.pump(tester, onboarding: picker.onboarding);
    await tester.tap(find.byKey(const Key('upstream-capture-connect-necklace')));
    await tester.pump();
    await tester.pump();
    expect(find.text('Ella could not search for necklaces. Try again.'), findsOneWidget);
    expect(find.text('Searching for devices...'), findsNothing);
    final retry = tester.widget<TextButton>(
      find.ancestor(of: find.text('Try Again'), matching: find.byType(TextButton)),
    );
    expect(retry.style!.foregroundColor!.resolve({}), EllaColors.tealDeep);
    expect(
      tester.getSize(find.ancestor(of: find.text('Try Again'), matching: find.byType(TextButton))).height,
      greaterThanOrEqualTo(EllaSizes.minTouchTarget),
    );
    picker.service.failDiscovery = false;
    await tester.tap(find.text('Try Again'));
    await tester.pump();
    await tester.pump();
    expect(picker.service.discovers, 2);
    expect(find.text('Compass'), findsOneWidget);
  });

  testWidgets('rollover integration: real conversation replacement updates the same open dock sheet',
      (tester) async {
    final fixture = await _DockFixture.create(tester);
    await fixture.pump(tester);
    fixture.makeNecklaceLive();
    ServerConversation snapshot(String id, String text) => ServerConversation(
          id: id,
          createdAt: DateTime.utc(2026, 10, 6),
          structured: Structured('Synthetic title', 'Synthetic overview'),
          transcriptSegments: [_segment('segment-$id', text)],
        );
    fixture.provider.applyInProgressConversation(snapshot('window-a', 'Synthetic original words'));
    final original = fixture.runtime.captureSession!;
    await tester.pump();
    await tester.tap(find.byKey(const Key('upstream-capture-view-transcript')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    fixture.provider.applyInProgressConversation(snapshot('window-a', 'Synthetic same-window revision'));
    await tester.pump();
    expect(fixture.runtime.isCurrentCaptureSession(original), isTrue);
    expect(find.text('Synthetic same-window revision'), findsOneWidget);
    fixture.provider.applyInProgressConversation(snapshot('window-b', 'Synthetic replacement words'));
    await tester.pump();
    expect(fixture.provider.activeRecordingId, original.recordingId);
    expect(fixture.provider.activeCaptureSessionId, 'window-1');
    expect(fixture.runtime.isCurrentCaptureSession(original), isFalse);
    expect(find.text('Synthetic same-window revision'), findsNothing);
    expect(find.text('Synthetic replacement words'), findsOneWidget);
    expect(find.text('Recording is unavailable right now.'), findsNothing);
    var finishCalls=0;
    fixture.runtime.finishOverride=() async { finishCalls++; };
    await fixture.runtime.finishCaptureSession(original);
    expect(finishCalls,0,reason:'read continuity cannot authorize a stale exact Finish');
  });

  testWidgets('real reset changes presentation generation even when timestamp and recording are reused',
      (tester) async {
    final fixture = await _DockFixture.create(tester);
    fixture.makePhoneLive();
    final original = fixture.runtime.captureSession!;
    fixture.provider.clearUserData();
    fixture.makePhoneLive();
    final replacement = fixture.runtime.captureSession!;
    expect(replacement.recordingId, original.recordingId);
    expect(fixture.provider.activeCaptureSessionId, 'window-1');
    expect(replacement.windowId, greaterThan(original.windowId));
    expect(fixture.runtime.isCurrentCaptureSession(original), isFalse);
  });

  for (final necklace in [false, true]) {
    testWidgets('retained ${necklace ? 'necklace' : 'phone'} transcript survives socket recovery', (tester) async {
      final fixture = await _DockFixture.create(tester);
      await fixture.pump(tester);
      necklace ? fixture.makeNecklaceLive() : fixture.makePhoneLive();
      fixture.provider.segments.add(_segment('retained-1', 'Synthetic retained words'));
      fixture.provider.markSocketRecovering();
      await tester.pump();
      expect(find.byKey(const Key('upstream-capture-view-transcript')), findsOneWidget);
      expect(find.byKey(const Key('upstream-capture-status')), findsOneWidget);
      expect(find.text('Recording with your necklace'), findsNothing);
      await tester.tap(find.byKey(const Key('upstream-capture-view-transcript')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      expect(find.text('Synthetic retained words'), findsOneWidget);
      expect(find.text(necklace ? 'Transcript · Necklace' : 'Transcript · iPhone'), findsWidgets);
      fixture.provider.captureReady = true;
      fixture.provider.onConnected();
      await tester.pump();
      expect(find.text('Synthetic retained words'), findsOneWidget);
      expect(fixture.provider.segments, hasLength(1));
    });
  }

  for (final retirement in ['account', 'consent', 'recording', 'window', 'binding']) {
    testWidgets('open transcript redacts on $retirement retirement', (tester) async {
      final fixture = await _DockFixture.create(tester);
      var signedIn = _uid;
      await fixture.pump(tester, authenticatedUid: () => signedIn);
      fixture.makeNecklaceLive(segments: [_segment('retained-1', 'Synthetic previous session')]);
      await tester.pump();
      await tester.tap(find.byKey(const Key('upstream-capture-view-transcript')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      expect(find.text('Synthetic previous session'), findsOneWidget);
      switch (retirement) {
        case 'account':
          signedIn = 'uid-b';
        case 'consent':
          (fixture.authority as _DockSessionAuthority).current = false;
        case 'recording':
          fixture.provider.recordingIdentity = 'recording-2';
        case 'window':
          fixture.provider.clearUserData();
        case 'binding':
          (fixture.authority as _DockSessionAuthority).epoch++;
      }
      fixture.provider.markSocketRecovering();
      await tester.pump();
      expect(find.text('Synthetic previous session'), findsNothing);
      expect(find.text('Recording is unavailable right now.'), findsOneWidget);
    });
  }

  testWidgets('pending recovery Finish survives remount and dispatches only once', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final completion = Completer<void>();
    var calls = 0;
    fixture.runtime.finishOverride = () {
      calls++;
      return completion.future;
    };
    await fixture.pump(tester);
    fixture.makeNecklaceLive(segments: [_segment('retained-1', 'Synthetic retained words')]);
    fixture.provider.markSocketRecovering();
    await tester.pump();
    final session = fixture.runtime.captureSession!;
    await tester.tap(find.byKey(const Key('upstream-capture-finish')));
    await tester.pump();
    final joined = fixture.runtime.finishCaptureSession(session);
    expect(calls, 1);
    await tester.pumpWidget(const SizedBox.shrink());
    await fixture.pump(tester);
    expect(find.text('Finishing and saving…'), findsOneWidget);
    final button = tester.widget<FilledButton>(find.byKey(const Key('upstream-capture-finish')));
    expect(button.onPressed, isNull);
    completion.complete();
    await joined;
    await tester.pump();
    expect(calls, 1);
    expect(fixture.runtime.canFinishSession(session), isFalse);
    fixture.provider.clearUserData();
    fixture.makeNecklaceLive();
    expect(fixture.runtime.canFinishSession(fixture.runtime.captureSession!), isTrue);
  });

  testWidgets('uncertain failed Finish preserves words without allowing same-session replay', (tester) async {
    final fixture = await _DockFixture.create(tester);
    var calls = 0;
    fixture.runtime.finishOverride = () async {
      calls++;
      throw StateError('synthetic save failure');
    };
    await fixture.pump(tester);
    fixture.makeNecklaceLive(segments: [_segment('retained-1', 'Synthetic retained words')]);
    fixture.provider.markSocketRecovering();
    await tester.pump();
    final session = fixture.runtime.captureSession!;
    await tester.tap(find.byKey(const Key('upstream-capture-finish')));
    await tester.pump();
    expect(fixture.provider.segments.single.text, 'Synthetic retained words');
    expect(fixture.runtime.finishPending.value, isFalse);
    await fixture.runtime.finishCaptureSession(session);
    expect(calls, 1);
    expect(find.text('Finishing and saving…'), findsNothing);
  });

  testWidgets('synchronous owner retirement at Finish notification prevents dispatch', (tester) async {
    final fixture = await _DockFixture.create(tester);
    fixture.makePhoneLive();
    var calls = 0;
    fixture.runtime.finishOverride = () async {
      calls++;
    };
    fixture.runtime.finishPending.addListener(() {
      if (fixture.runtime.finishPending.value) (fixture.authority as _DockSessionAuthority).current = false;
    });
    await fixture.runtime.finishCaptureSession(fixture.runtime.captureSession!);
    expect(calls, 0);
    expect(fixture.runtime.finishPending.value, isFalse);
  });

  testWidgets('late failed Finish cannot publish saving or failure for replacement account', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final completion = Completer<void>();
    var signedIn = _uid;
    fixture.runtime.finishOverride = () => completion.future;
    await fixture.pump(tester, authenticatedUid: () => signedIn);
    fixture.makePhoneLive();
    await tester.pump();
    await tester.tap(find.byKey(const Key('upstream-capture-finish')));
    await tester.pump();
    expect(find.text('Finishing and saving…'), findsOneWidget);
    signedIn = 'uid-b';
    (fixture.authority as _DockSessionAuthority).epoch++;
    fixture.provider.markSocketRecovering();
    await tester.pump();
    expect(find.text('Finishing and saving…'), findsNothing);
    expect(find.byKey(const Key('upstream-capture-view-transcript')), findsNothing);
    completion.completeError(StateError('synthetic previous-account failure'));
    await tester.pump();
    expect(find.text('Recording is unavailable right now.'), findsNothing);
    expect(find.text('Finishing and saving…'), findsNothing);
  });

  testWidgets('transcript uses a scrollable sheet and restores focus without growing the dock', (tester) async {
    final fixture = await _DockFixture.create(tester);
    await fixture.pump(tester);
    fixture.makeNecklaceLive();
    await tester.pump();
    await tester.pump();
    final dockHeight = tester.getSize(find.byKey(const Key('upstream-capture-dock'))).height;

    await tester.tap(find.byKey(const Key('upstream-capture-view-transcript')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(find.byKey(const Key('upstream-capture-transcript-title')), findsOneWidget);
    expect(find.byKey(const Key('upstream-capture-transcript-empty')), findsOneWidget);
    expect(find.text('Starting…'), findsNothing);
    expect(tester.getSize(find.byKey(const Key('upstream-capture-dock'))).height, dockHeight);
    expect(
      tester.getSize(find.byKey(const Key('upstream-capture-transcript-close'))).height,
      greaterThanOrEqualTo(EllaSizes.minTouchTarget),
    );

    fixture.provider.segments.add(_segment('seg-1', 'hello from the necklace'));
    fixture.provider.onConnected();
    await tester.pump();
    await tester.pump();
    expect(find.byKey(const Key('upstream-capture-transcript-list')), findsOneWidget);
    expect(find.text('hello from the necklace'), findsOneWidget);

    await tester.tap(find.byKey(const Key('upstream-capture-transcript-close')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(tester.getSize(find.byKey(const Key('upstream-capture-dock'))).height, lessThanOrEqualTo(dockHeight));
    final transcript = tester.widget<OutlinedButton>(find.byKey(const Key('upstream-capture-view-transcript')));
    expect(transcript.focusNode!.hasFocus, isTrue);
  });

  testWidgets('Stop and Disconnect report their own in-progress states', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final stop = Completer<void>();
    fixture.runtime.stopPhoneOverride = () => stop.future;
    await fixture.pump(tester);
    fixture.makePhoneLive();
    await tester.pump();

    await tester.tap(find.byKey(const Key('upstream-capture-stop-phone')));
    await tester.pump();
    expect(find.text('Stopping recording…'), findsOneWidget);
    expect(find.text('Starting…'), findsNothing);
    stop.complete();
    await tester.pump();
    await tester.pump();

    fixture.provider.updateRecordingState(RecordingState.deviceRecord);
    fixture.provider.updateRecordingDevice(_testNecklace);
    fixture.provider.onConnected();
    final disconnect = Completer<void>();
    fixture.runtime.disconnectNecklaceOverride = () => disconnect.future;
    await tester.pump();
    await tester.tap(find.byKey(const Key('upstream-capture-disconnect-necklace')));
    await tester.pump();
    expect(find.text('Disconnecting necklace…'), findsOneWidget);
    expect(find.text('Starting…'), findsNothing);
    disconnect.complete();
    await tester.pump();
    await tester.pump();
    expect(tester.takeException(), isNull);
  });

  testWidgets('Finish reports draining state instead of generic Starting', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final finish = Completer<void>();
    fixture.runtime.finishOverride = () => finish.future;
    await fixture.pump(tester);
    fixture.makeNecklaceLive(segments: [_segment('seg-1', 'ready')]);
    await tester.pump();
    await tester.pump();

    await tester.tap(find.byKey(const Key('upstream-capture-finish')));
    await tester.pump();
    expect(find.text('Finishing and saving…'), findsOneWidget);
    expect(find.text('Starting…'), findsNothing);

    finish.complete();
    await tester.pump();
    await tester.pump();
    expect(tester.takeException(), isNull);
  });

  testWidgets('3x text shows the pending Finish operation instead of idle necklace status', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final finish = Completer<void>();
    fixture.runtime.finishOverride = () => finish.future;
    await fixture.pump(tester, textScale: 3);
    fixture.makeNecklaceLive();
    await tester.pump();

    await tester.tap(find.byKey(const Key('upstream-capture-finish')));
    await tester.pump();
    expect(tester.widget<Text>(find.byKey(const Key('upstream-capture-status'))).data, 'Finishing and saving…');
    expect(find.text('Waiting for speech…'), findsNothing);

    finish.complete();
    await tester.pump();
    expect(tester.takeException(), isNull);
  });

  testWidgets('live dock status announces its full status once at normal and large text', (tester) async {
    final semantics = tester.ensureSemantics();
    final fixture = await _DockFixture.create(tester);
    for (final textScale in [1.0, 3.0]) {
      await fixture.pump(tester, textScale: textScale, reduceMotion: true);
      fixture.makeNecklaceLive();
      await tester.pump();
      final status = tester.getSemantics(find.byKey(const Key('upstream-capture-status')));
      expect(status.label, 'Necklace connected. Ready. Waiting for speech…');
      expect(status.flagsCollection.isLiveRegion, isTrue);
    }
    await tester.pumpWidget(const SizedBox.shrink());
    semantics.dispose();
  });

  for (final whisperScale in [1.0, 3.5])
    testWidgets('compact Whispers entry opens truthful controls and supports error retry scale=$whisperScale',
        (tester) async {
      Future<void> capture(String name) async {
        await tester.runAsync(() async {
          final boundary = tester.renderObject<RenderRepaintBoundary>(find.byKey(const Key('capture-fixture-render')));
          final image = await boundary.toImage();
          final bytes = (await image.toByteData(format: ImageByteFormat.png))!.buffer.asUint8List();
          final out = Directory('../rendered-coherent-screens')..createSync(recursive: true);
          File('${out.path}/$name.png').writeAsBytesSync(bytes);
          image.dispose();
        });
      }

      final fixture = await _DockFixture.create(tester);
      var writes = 0;
      var starts = 0;
      var nativeState = guardian_native.GuardianModeState.idle;
      await fixture.pump(
        tester,
        compactPresentation: true,
        textScale: whisperScale,
        guardianAvailable: true,
        guardianModeLoader: () async =>
            const GuardianModeInfo(currentMode: GuardianModeKey.off, twoTierState: GuardianModeState()),
        guardianModeSetter: (_) async {
          writes++;
          return true;
        },
        guardianNativeStart: () async {
          starts++;
          if (starts == 1) throw StateError('fixture failure');
          nativeState = guardian_native.GuardianModeState.active;
        },
        guardianNativeStop: () async {},
        guardianNativeState: () => nativeState,
      );
      expect(IrisCaptureControls.shared.readWhispers?.call(), isFalse);
      final sheet = IrisCaptureControls.shared.openWhispers!();
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('upstream-capture-whispers-switch')), findsOneWidget);
      expect(starts, 0);
      expect(writes, 0);
      if (whisperScale == 1) await capture('whispers-verified-off-settings');
      await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
      await tester.pumpAndSettle();
      expect(find.text("Whispers are on, but Ella couldn't start spoken help."), findsOneWidget);
      if (whisperScale == 1) await capture('whispers-on-playback-failed');
      await tester.ensureVisible(find.byKey(const Key('upstream-capture-whispers-retry')));
      await tester.pump();
      expect(find.byKey(const Key('upstream-capture-whispers-retry')), findsOneWidget);
      await tester.tap(find.byKey(const Key('upstream-capture-whispers-retry')));
      await tester.pumpAndSettle();
      expect(starts, 2);
      expect(writes, 1);
      expect(find.text('Whispers are on — Ella can speak up when she can help.'), findsOneWidget);
      final context = tester.element(find.byKey(const Key('upstream-capture-whispers-status')));
      Navigator.of(context).pop();
      await tester.pumpAndSettle();
      await sheet;
    });

  testWidgets('Whispers distinguishes saved configuration from failed native playback', (tester) async {
    final fixture = await _DockFixture.create(tester);
    GuardianModeState? savedState;
    final save = Completer<bool>();
    await fixture.pump(
      tester,
      guardianAvailable: true,
      guardianModeLoader: () async =>
          const GuardianModeInfo(currentMode: GuardianModeKey.off, twoTierState: GuardianModeState()),
      guardianModeSetter: (state) async {
        savedState = state;
        return save.future;
      },
      guardianNativeStart: () async => throw StateError('native unavailable'),
      guardianNativeStop: () async {},
      guardianNativeState: () => guardian_native.GuardianModeState.idle,
    );

    expect(find.text('Whispers are off. Spoken responses are paused.'), findsOneWidget);
    await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
    await tester.pump();
    expect(find.text('Saving Whispers…'), findsOneWidget);
    save.complete(true);
    await tester.pump();
    await tester.pump();

    expect(savedState?.features, ['MEMORY_SUPPORT']);
    expect(find.text("Whispers are on, but Ella couldn't start spoken help."), findsOneWidget);
    expect(find.byKey(const Key('upstream-capture-whispers-error')), findsNothing);
    expect(find.byKey(const Key('upstream-capture-whispers-retry')), findsOneWidget);
    expect(find.byKey(const Key('upstream-capture-whispers-switch')), findsOneWidget);
  });

  testWidgets('awaited dock native start cannot publish ready after account profile ABA', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final started = Completer<void>();
    var epoch = 0;
    await fixture.pump(
      tester,
      guardianAvailable: true,
      guardianAuthorityProvider: () {
        final captured = epoch;
        return _WhisperTestAuthority(_uid, () => epoch == captured);
      },
      guardianModeLoader: () async =>
          const GuardianModeInfo(currentMode: GuardianModeKey.off, twoTierState: GuardianModeState()),
      guardianModeSetter: (_) async => true,
      guardianNativeStart: () => started.future,
      guardianNativeStop: () async {},
      guardianNativeState: () => guardian_native.GuardianModeState.idle,
    );
    await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
    await tester.pump();
    epoch++;
    guardian_native.GuardianModeService.whisperStateFence.invalidate();
    started.complete();
    await tester.pump();
    await tester.pump();
    expect(find.text('Whispers are on — Ella can speak up when she can help.'), findsNothing);
    expect(guardian_native.GuardianModeService.whisperStateFence.snapshot, isNull);
    expect(find.byKey(const Key('upstream-capture-whispers-switch')), findsNothing);
    expect(find.byKey(const Key('upstream-capture-whispers-retry')), findsOneWidget);
  });

  testWidgets('a dock OFF save failure cannot restore native for an authority that changed', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final saved = Completer<bool>();
    var current = true;
    var starts = 0;
    await fixture.pump(
      tester,
      guardianAvailable: true,
      guardianAuthorityProvider: () => _WhisperTestAuthority(_uid, () => current),
      guardianModeLoader: () async => const GuardianModeInfo(
        currentMode: GuardianModeKey.custom,
        twoTierState: GuardianModeState(features: ['MEMORY_SUPPORT']),
      ),
      guardianModeSetter: (_) => saved.future,
      guardianNativeStart: () async => starts++,
      guardianNativeStop: () async {},
      guardianNativeState: () => guardian_native.GuardianModeState.active,
    );
    await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
    await tester.pump();
    current = false;
    guardian_native.GuardianModeService.whisperStateFence.invalidate();
    saved.complete(false);
    await tester.pump();
    await tester.pump();
    expect(starts, 0);
    expect(guardian_native.GuardianModeService.whisperStateFence.snapshot, isNull);
  });

  for (final phase in ['put', 'native', 'availability']) {
    testWidgets('dock pending $phase releases its ticket and permits a same-authority remount', (tester) async {
      final fixture = await _DockFixture.create(tester);
      final pending = Completer<void>();
      var reads = 0;
      var nativeStarts = 0;
      var serverOn = false;
      var nativeOn = false;
      Future<GuardianModeInfo?> read() async {
        reads++;
        return GuardianModeInfo(
          currentMode: serverOn ? GuardianModeKey.custom : GuardianModeKey.off,
          twoTierState: serverOn ? const GuardianModeState(features: ['MEMORY_SUPPORT']) : const GuardianModeState(),
        );
      }

      await fixture.pump(
        tester,
        guardianAvailable: true,
        guardianModeLoader: read,
        guardianModeSetter: (_) async {
          if (phase != 'native') await pending.future;
          serverOn = true;
          return true;
        },
        guardianNativeStart: () async {
          nativeStarts++;
          if (phase == 'native') await pending.future;
          nativeOn = true;
        },
        guardianNativeStop: () async => nativeOn = false,
        guardianNativeState: () =>
            nativeOn ? guardian_native.GuardianModeState.active : guardian_native.GuardianModeState.idle,
      );
      await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
      await tester.pump();
      expect(guardian_native.GuardianModeService.whisperStateFence.choicePending, isTrue);
      if (phase == 'availability') {
        await fixture.pump(tester, guardianAvailable: false);
      } else {
        await tester.pumpWidget(const SizedBox.shrink());
        expect(guardian_native.GuardianModeService.whisperStateFence.choicePending, isTrue);
      }
      pending.complete();
      await tester.pump();
      await tester.pump();
      expect(guardian_native.GuardianModeService.whisperStateFence.choicePending, isFalse);
      if (phase != 'native') expect(nativeStarts, 0);
      await tester.pumpWidget(const SizedBox.shrink());
      final beforeReads = reads;
      await fixture.pump(
        tester,
        guardianAvailable: true,
        guardianModeLoader: read,
        guardianNativeState: () =>
            nativeOn ? guardian_native.GuardianModeState.active : guardian_native.GuardianModeState.idle,
      );
      expect(reads, beforeReads + 1);
      expect(find.text('Saving Whispers…'), findsNothing);
      expect(tester.widget<Switch>(find.byKey(const Key('upstream-capture-whispers-switch'))).value, isTrue);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('dock remounted before abandoned ON write settles reads only the committed server state', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final pending = Completer<void>();
    var serverOn = false;
    var reads = 0;
    var staleNativeStarts = 0;
    Future<GuardianModeInfo?> read() async {
      reads++;
      return GuardianModeInfo(
        currentMode: serverOn ? GuardianModeKey.custom : GuardianModeKey.off,
        twoTierState: serverOn ? const GuardianModeState(features: ['MEMORY_SUPPORT']) : const GuardianModeState(),
      );
    }

    await fixture.pump(
      tester,
      guardianAvailable: true,
      guardianModeLoader: read,
      guardianModeSetter: (_) async {
        await pending.future;
        serverOn = true;
        return true;
      },
      guardianNativeStart: () async => staleNativeStarts++,
      guardianNativeState: () => guardian_native.GuardianModeState.idle,
    );
    await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
    await tester.pump();
    await tester.pumpWidget(const SizedBox.shrink());
    final beforeReads = reads;
    await fixture.pump(
      tester,
      guardianAvailable: true,
      guardianModeLoader: read,
      guardianNativeState: () => guardian_native.GuardianModeState.idle,
    );
    expect(reads, beforeReads);
    expect(guardian_native.GuardianModeService.whisperStateFence.snapshot, isNull);
    pending.complete();
    await tester.pump();
    await tester.pump();
    expect(reads, beforeReads + 1);
    expect(staleNativeStarts, 0);
    expect(guardian_native.GuardianModeService.whisperStateFence.choicePending, isFalse);
    expect(guardian_native.GuardianModeService.whisperStateFence.snapshot!.enabled, isTrue);
    expect(tester.widget<Switch>(find.byKey(const Key('upstream-capture-whispers-switch'))).value, isTrue);
    expect(tester.takeException(), isNull);
  });

  for (final initialOn in [false, true]) {
    testWidgets('ambiguous dock write reads committed ${!initialOn} before native reconciliation', (tester) async {
      final fixture = await _DockFixture.create(tester);
      var serverOn = initialOn;
      var nativeOn = initialOn;
      var reads = 0;
      var writes = 0;
      await fixture.pump(
        tester,
        guardianAvailable: true,
        guardianModeLoader: () async {
          reads++;
          return GuardianModeInfo(
            currentMode: serverOn ? GuardianModeKey.custom : GuardianModeKey.off,
            twoTierState: serverOn ? const GuardianModeState(features: ['MEMORY_SUPPORT']) : const GuardianModeState(),
          );
        },
        guardianModeSetter: (state) async {
          writes++;
          serverOn = !state.isOff;
          throw StateError('response lost after commit');
        },
        guardianNativeStart: () async => nativeOn = true,
        guardianNativeStop: () async => nativeOn = false,
        guardianNativeState: () =>
            nativeOn ? guardian_native.GuardianModeState.active : guardian_native.GuardianModeState.idle,
      );
      await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
      await tester.pump();
      await tester.pump();
      expect(reads, 2);
      expect(writes, 1);
      expect(nativeOn, !initialOn);
      final snapshot = guardian_native.GuardianModeService.whisperStateFence.snapshot!;
      expect(snapshot.enabled, !initialOn);
      expect(snapshot.modeVerified, isTrue);
      expect(snapshot.nativeReconciled, isTrue);
      expect(tester.widget<Switch>(find.byKey(const Key('upstream-capture-whispers-switch'))).value, !initialOn);
    });
  }

  testWidgets('ambiguous dock write with failed readback remains unknown and Retry reads current mode', (tester) async {
    final fixture = await _DockFixture.create(tester);
    var reads = 0;
    var starts = 0;
    await fixture.pump(
      tester,
      guardianAvailable: true,
      guardianModeLoader: () async {
        reads++;
        if (reads == 2) throw StateError('readback unavailable');
        return GuardianModeInfo(
          currentMode: reads == 1 ? GuardianModeKey.off : GuardianModeKey.custom,
          twoTierState: reads == 1 ? const GuardianModeState() : const GuardianModeState(features: ['MEMORY_SUPPORT']),
        );
      },
      guardianModeSetter: (_) async => false,
      guardianNativeStart: () async => starts++,
      guardianNativeStop: () async {},
      guardianNativeState: () => guardian_native.GuardianModeState.idle,
    );
    await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
    await tester.pump();
    await tester.pump();
    expect(guardian_native.GuardianModeService.whisperStateFence.snapshot!.modeVerified, isFalse);
    expect(starts, 0);
    expect(find.text('Whispers are off. Spoken responses are paused.'), findsNothing);
    expect(find.byKey(const Key('upstream-capture-whispers-retry')), findsOneWidget);
    await tester.tap(find.byKey(const Key('upstream-capture-whispers-retry')));
    await tester.pump();
    await tester.pump();
    expect(reads, 3);
    expect(guardian_native.GuardianModeService.whisperStateFence.snapshot!.enabled, isTrue);
  });

  for (final supersededBy in ['newer_choice', 'authority_replacement']) {
    testWidgets('ambiguous dock readback cannot reconcile after $supersededBy', (tester) async {
      final fixture = await _DockFixture.create(tester);
      final readback = Completer<GuardianModeInfo?>();
      var reads = 0;
      var starts = 0;
      var epoch = 0;
      ExactAccountAuthorityVerifier authority() {
        final captured = epoch;
        return _WhisperTestAuthority(_uid, () => epoch == captured);
      }

      await fixture.pump(
        tester,
        guardianAvailable: true,
        guardianAuthorityProvider: authority,
        guardianModeLoader: () => ++reads == 1
            ? Future.value(const GuardianModeInfo(currentMode: GuardianModeKey.off, twoTierState: GuardianModeState()))
            : readback.future,
        guardianModeSetter: (_) async => false,
        guardianNativeStart: () async => starts++,
        guardianNativeStop: () async {},
        guardianNativeState: () => guardian_native.GuardianModeState.idle,
      );
      await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
      await tester.pump();
      expect(reads, 2);
      final fence = guardian_native.GuardianModeService.whisperStateFence;
      if (supersededBy == 'authority_replacement') {
        epoch++;
        fence.invalidate();
      }
      final newer = fence.choose(authority, false)!;
      fence.publish(newer, (enabled: false, modeVerified: true, nativeReconciled: true));
      readback.complete(
        const GuardianModeInfo(
          currentMode: GuardianModeKey.custom,
          twoTierState: GuardianModeState(features: ['MEMORY_SUPPORT']),
        ),
      );
      await tester.pump();
      await tester.pump();
      expect(starts, 0);
      expect(fence.snapshot!.enabled, isFalse);
      expect(fence.snapshot!.modeVerified, isTrue);
      expect(fence.choicePending, isFalse);
    });
  }

  testWidgets('Whispers save failure reads and keeps the verified prior state', (tester) async {
    final fixture = await _DockFixture.create(tester);
    var starts = 0;
    var writes = 0;
    var saveSucceeds = false;
    await fixture.pump(
      tester,
      guardianAvailable: true,
      guardianModeLoader: () async =>
          const GuardianModeInfo(currentMode: GuardianModeKey.off, twoTierState: GuardianModeState()),
      guardianModeSetter: (_) async {
        writes++;
        return saveSucceeds;
      },
      guardianNativeStart: () async => starts++,
      guardianNativeStop: () async {},
      guardianNativeState: () => guardian_native.GuardianModeState.idle,
    );

    await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
    await tester.pump();
    await tester.pump();

    expect(find.text('Whispers could not be updated. Try again.'), findsOneWidget);
    expect(find.text('Whispers are off. Spoken responses are paused.'), findsNothing);
    final toggle = tester.widget<Switch>(find.byKey(const Key('upstream-capture-whispers-switch')));
    expect(toggle.value, isFalse);
    expect(starts, 0);
    expect(writes, 1);
    expect(find.byKey(const Key('upstream-capture-whispers-retry')), findsNothing);

    saveSucceeds = true;
    await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
    await tester.pump();
    await tester.pump();
    expect(writes, 2);
    expect(starts, 1);
    expect(tester.widget<Switch>(find.byKey(const Key('upstream-capture-whispers-switch'))).value, isTrue);
    expect(find.text('Whispers could not be updated. Try again.'), findsNothing);
  });

  for (final initialAuthority in ['missing', 'stale']) {
    testWidgets('initial $initialAuthority Whisper authority renders safe read-only recovery', (tester) async {
      final fixture = await _DockFixture.create(tester);
      final haptics = captureHaptics(tester);
      var current = false;
      var reads = 0;
      var writes = 0;
      var nativeCalls = 0;
      await fixture.pump(
        tester,
        reduceMotion: true,
        guardianAvailable: true,
        guardianAuthorityProvider: () =>
            initialAuthority == 'missing' && !current ? null : _WhisperTestAuthority(_uid, () => current),
        guardianModeLoader: () async {
          reads++;
          return const GuardianModeInfo(currentMode: GuardianModeKey.off, twoTierState: GuardianModeState());
        },
        guardianModeSetter: (_) async {
          writes++;
          return true;
        },
        guardianNativeStart: () async => nativeCalls++,
        guardianNativeStop: () async => nativeCalls++,
        guardianNativeState: () => guardian_native.GuardianModeState.idle,
      );
      expect(tester.takeException(), isNull);
      expect(find.text('Whispers are unavailable right now.'), findsOneWidget);
      expect(find.byKey(const Key('upstream-capture-whispers-switch')), findsNothing);
      final retry = find.byKey(const Key('upstream-capture-whispers-retry'));
      expect(retry.hitTestable(), findsOneWidget);
      expect(reads, 0);
      expect(writes, 0);
      expect(nativeCalls, 0);
      expect(haptics, isEmpty);
      await tester.tap(retry);
      await tester.pump();
      expect(reads, 0);
      expect(writes, 0);
      expect(nativeCalls, 0);
      expect(haptics, isEmpty);
      current = true;
      await tester.tap(retry);
      await tester.pump();
      await tester.pump();
      expect(reads, 1);
      expect(writes, 0);
      expect(nativeCalls, 0);
      expect(haptics, hasLength(1));
      expect(tester.widget<Switch>(find.byKey(const Key('upstream-capture-whispers-switch'))).value, isFalse);
      expect(tester.takeException(), isNull);
    });
  }

  for (final denial in ['missing', 'stale']) {
    testWidgets('Whispers $denial fresh authority gives read-only recovery after a denied tap', (tester) async {
      final fixture = await _DockFixture.create(tester);
      final haptics = captureHaptics(tester);
      var available = true;
      var current = true;
      var reads = 0;
      var writes = 0;
      var starts = 0;
      var stops = 0;
      await fixture.pump(
        tester,
        reduceMotion: true,
        guardianAvailable: true,
        guardianAuthorityProvider: () => available ? _WhisperTestAuthority(_uid, () => current) : null,
        guardianModeLoader: () async {
          reads++;
          return const GuardianModeInfo(currentMode: GuardianModeKey.off, twoTierState: GuardianModeState());
        },
        guardianModeSetter: (_) async {
          writes++;
          return true;
        },
        guardianNativeStart: () async => starts++,
        guardianNativeStop: () async => stops++,
        guardianNativeState: () => guardian_native.GuardianModeState.idle,
      );
      expect(reads, 1);
      if (denial == 'missing') {
        // An established snapshot can remain current during transient revalidation.
        available = false;
      } else {
        current = false;
      }
      await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
      await tester.pump();
      expect(find.text('Whispers are unavailable right now.'), findsOneWidget);
      expect(find.byKey(const Key('upstream-capture-whispers-switch')), findsNothing);
      final retry = find.byKey(const Key('upstream-capture-whispers-retry'));
      expect(retry.hitTestable(), findsOneWidget);
      await tester.tap(retry);
      await tester.pump();
      expect(reads, 1);
      expect(writes, 0);
      expect(starts, 0);
      expect(stops, 0);
      expect(haptics, isEmpty);

      available = true;
      current = true;
      await tester.tap(retry);
      await tester.pump();
      await tester.pump();
      expect(reads, 2);
      expect(writes, 0);
      expect(starts, 0);
      expect(stops, 0);
      expect(haptics, hasLength(1));
      expect(tester.widget<Switch>(find.byKey(const Key('upstream-capture-whispers-switch'))).value, isFalse);
      expect(find.text('Whispers are off. Spoken responses are paused.'), findsOneWidget);
      expect(tester.binding.transientCallbackCount, 0);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('Whispers transient provisioning clear recovers read-only with real authority and large text',
      (tester) async {
    // The configured protocol1 runtime retires its process-local authority when
    // this fixture changes UID. Supply only its native Stop acknowledgement;
    // this test makes no delivery, provider-drain or speech qualification claim.
    const retirementChannel = MethodChannel('com.ellaaicare.ella/guardian_mode');
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
        retirementChannel, (call) async => call.method == 'strictStop' ? {'stopped': true} : null);
    addTearDown(() => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(retirementChannel, null));
    final fixture = await _DockFixture.create(tester);
    final prefs = fork.SharedPreferencesUtil()..uid = _uid;
    prefs.acceptAiConsent(
      receiptId: 'aicr_test',
      uid: _uid,
      profileBindingId: 'profile-test',
      serverDecidedAt: '2026-08-02T00:00:00Z',
    );
    await prefs.saveEllaProvisioningReceipt(_uid, {
      'state': 'ready',
      'binding_state': 'active',
      'binding_revision': 3,
      'effective_policy_revision': 'policy-3',
    });
    await prefs.markEllaProvisioningVerified(_uid);
    prefs.markAiConsentServerVerified(
      uid: _uid,
      receiptId: 'aicr_test',
      policyVersion: fork.SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: fork.SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      profileBindingId: 'profile-test',
      scopeVersion: fork.SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: fork.SharedPreferencesUtil.currentAiConsentScopeHash,
    );
    ExactAccountAuthorityVerifier? authority() {
      final active = WalOwnerAuthority.active(preferences: prefs, authenticatedUid: _uid);
      return active == null
          ? null
          : _WhisperTestAuthority(_uid, () => active.isCurrent(preferences: prefs, authenticatedUid: _uid));
    }

    final established = WalOwnerAuthority.active(preferences: prefs, authenticatedUid: _uid)!;
    var reads = 0;
    var writes = 0;
    var nativeCalls = 0;
    final semantics = tester.ensureSemantics();
    await fixture.pump(
      tester,
      size: const Size(320, 1600),
      textScaler: const _NonlinearWhisperTextScaler(),
      reduceMotion: true,
      guardianAvailable: true,
      guardianAuthorityProvider: authority,
      guardianModeLoader: () async {
        reads++;
        return const GuardianModeInfo(currentMode: GuardianModeKey.off, twoTierState: GuardianModeState());
      },
      guardianModeSetter: (_) async {
        writes++;
        return true;
      },
      guardianNativeStart: () async => nativeCalls++,
      guardianNativeStop: () async => nativeCalls++,
      guardianNativeState: () => guardian_native.GuardianModeState.idle,
    );
    prefs.invalidateEllaProvisioningServerVerification();
    expect(established.isCurrent(preferences: prefs, authenticatedUid: _uid), isTrue);
    expect(authority(), isNull);
    expect(guardian_native.GuardianModeService.whisperStateFence.snapshot!.modeVerified, isTrue);
    await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
    await tester.pump();
    expect(find.text('Whispers are unavailable right now.'), findsOneWidget);
    final retry = find.byKey(const Key('upstream-capture-whispers-retry'));
    expect(retry.hitTestable(), findsOneWidget);
    expect(tester.getSize(retry).height, greaterThanOrEqualTo(48));
    expect(tester.getSemantics(retry).label, contains('Try Again'));
    await tester.tap(retry);
    await tester.pump();
    expect(reads, 1);
    expect(writes, 0);
    expect(nativeCalls, 0);
    await prefs.markEllaProvisioningVerified(_uid);
    await tester.tap(retry);
    await tester.pump();
    await tester.pump();
    expect(reads, 2);
    expect(writes, 0);
    expect(nativeCalls, 0);
    expect(tester.widget<Switch>(find.byKey(const Key('upstream-capture-whispers-switch'))).value, isFalse);
    expect(tester.binding.transientCallbackCount, 0);
    expect(tester.takeException(), isNull);
    semantics.dispose();
  });

  testWidgets('Whispers denied-choice recovery cannot publish a read after account replacement', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final readback = Completer<GuardianModeInfo?>();
    var epoch = 0;
    var available = true;
    var reads = 0;
    var writes = 0;
    var nativeCalls = 0;
    ExactAccountAuthorityVerifier? authority() {
      final captured = epoch;
      return available ? _WhisperTestAuthority(_uid, () => captured == epoch) : null;
    }

    await fixture.pump(
      tester,
      guardianAvailable: true,
      guardianAuthorityProvider: authority,
      guardianModeLoader: () => ++reads == 1
          ? Future.value(const GuardianModeInfo(currentMode: GuardianModeKey.off, twoTierState: GuardianModeState()))
          : readback.future,
      guardianModeSetter: (_) async {
        writes++;
        return true;
      },
      guardianNativeStart: () async => nativeCalls++,
      guardianNativeStop: () async => nativeCalls++,
      guardianNativeState: () => guardian_native.GuardianModeState.idle,
    );
    available = false;
    await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
    await tester.pump();
    available = true;
    await tester.tap(find.byKey(const Key('upstream-capture-whispers-retry')));
    await tester.pump();
    expect(reads, 2);
    epoch++;
    final fence = guardian_native.GuardianModeService.whisperStateFence;
    fence.invalidate();
    readback.complete(const GuardianModeInfo(
      currentMode: GuardianModeKey.custom,
      twoTierState: GuardianModeState(features: ['MEMORY_SUPPORT']),
    ));
    await tester.pump();
    await tester.pump();
    expect(fence.snapshot, isNull);
    expect(fence.choicePending, isFalse);
    expect(find.text('Whispers are unavailable right now.'), findsOneWidget);
    expect(find.byKey(const Key('upstream-capture-whispers-switch')), findsNothing);
    expect(writes, 0);
    expect(nativeCalls, 0);
  });

  testWidgets('unavailable saved-on playback retries without mode writes and waits for native success', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final nativeCompletion = Completer<void>();
    var starts = 0;
    var writes = 0;
    await fixture.pump(
      tester,
      guardianAvailable: true,
      guardianModeLoader: () async => const GuardianModeInfo(
        currentMode: GuardianModeKey.custom,
        twoTierState: GuardianModeState(features: ['MEMORY_SUPPORT']),
      ),
      guardianModeSetter: (_) async {
        writes++;
        return true;
      },
      guardianNativeStart: () async {
        if (starts++ == 0) throw StateError('HTTP 503 technical provider detail');
        await nativeCompletion.future;
      },
      guardianNativeState: () => guardian_native.GuardianModeState.idle,
    );
    final retry = find.byKey(const Key('upstream-capture-whispers-retry'));
    expect(retry.hitTestable(), findsOneWidget);
    expect(find.text('Whispers are on. Spoken help is unavailable right now.'), findsOneWidget);
    await tester.tap(retry);
    await tester.pump();
    await tester.pump();
    expect(find.text("Whispers are on, but Ella couldn't start spoken help."), findsOneWidget);
    expect(find.textContaining('HTTP'), findsNothing);
    await tester.tap(retry);
    await tester.pump();
    expect(find.text('Whispers are on — Ella can speak up when she can help.'), findsNothing);
    expect(writes, 0);
    nativeCompletion.complete();
    await tester.pump();
    await tester.pump();
    expect(find.text('Whispers are on — Ella can speak up when she can help.'), findsOneWidget);
    expect(starts, 2);
    expect(writes, 0);
    expect(retry, findsNothing);
  });

  for (final locale in [const Locale('en'), const Locale('ar')]) {
    testWidgets('gentle Whispers recovery is actionable with ${locale.languageCode} nonlinear large text', (
      tester,
    ) async {
      final fixture = await _DockFixture.create(tester);
      final semantics = tester.ensureSemantics();
      var reads = 0;
      var writes = 0;
      await fixture.pump(
        tester,
        size: const Size(320, 1600),
        textScaler: const _NonlinearWhisperTextScaler(),
        locale: locale,
        guardianAvailable: true,
        guardianModeLoader: () async {
          if (reads++ == 0) throw StateError('HTTP 503 provider secret diagnostic');
          return const GuardianModeInfo(currentMode: GuardianModeKey.off, twoTierState: GuardianModeState());
        },
        guardianModeSetter: (_) async {
          writes++;
          return true;
        },
        guardianNativeStop: () async {},
        guardianNativeState: () => guardian_native.GuardianModeState.idle,
      );
      final row = find.byKey(const Key('upstream-capture-whispers-stacked'));
      final retry = find.byKey(const Key('upstream-capture-whispers-retry'));
      expect(find.textContaining('HTTP'), findsNothing);
      expect(find.textContaining('provider secret'), findsNothing);
      expect(tester.getSemantics(row).label, isNot(contains('503')));
      expect(tester.getSize(retry).height, greaterThanOrEqualTo(48));
      expect(retry.hitTestable(), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.tap(retry);
      await tester.pump();
      await tester.pump();
      expect(reads, 2);
      expect(writes, 0);
      expect(find.byKey(const Key('upstream-capture-whispers-retry')), findsNothing);
      expect(tester.widget<Switch>(find.byKey(const Key('upstream-capture-whispers-switch'))).value, isFalse);
      expect(tester.takeException(), isNull);
      semantics.dispose();
    });
  }

  testWidgets('Whispers stop failure stays truthful and Retry stops native playback', (tester) async {
    final fixture = await _DockFixture.create(tester);
    var stopShouldFail = true;
    await fixture.pump(
      tester,
      guardianAvailable: true,
      guardianModeLoader: () async => const GuardianModeInfo(
        currentMode: GuardianModeKey.custom,
        twoTierState: GuardianModeState(features: ['MEMORY_SUPPORT']),
      ),
      guardianModeSetter: (_) async => true,
      guardianNativeStart: () async {},
      guardianNativeStop: () async {
        if (stopShouldFail) throw StateError('native stop failed');
      },
      guardianNativeState: () => guardian_native.GuardianModeState.active,
    );

    await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
    await tester.pump();
    await tester.pump();
    expect(find.text('Whispers are off. Spoken responses are paused.'), findsNothing);
    expect(find.text("Whispers are set to off, but Ella couldn't pause spoken help."), findsOneWidget);

    stopShouldFail = false;
    await tester.tap(find.byKey(const Key('upstream-capture-whispers-retry')));
    await tester.pump();
    await tester.pump();
    expect(find.byKey(const Key('upstream-capture-whispers-error')), findsNothing);
  });

  testWidgets('production MethodChannel stop failure remains visible after saved OFF', (tester) async {
    final fixture = await _DockFixture.create(tester);
    const channel = MethodChannel('com.ellaaicare.ella/guardian_mode');
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
      if (call.method == 'configureAvailability' &&
          call.arguments is Map &&
          (call.arguments as Map)['enabled'] == false) {
        throw PlatformException(code: 'native_stop_failed');
      }
      return null;
    });
    addTearDown(() => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(channel, null));
    await fixture.pump(
      tester,
      guardianAvailable: true,
      guardianModeLoader: () async => const GuardianModeInfo(
        currentMode: GuardianModeKey.custom,
        twoTierState: GuardianModeState(features: ['MEMORY_SUPPORT']),
      ),
      guardianModeSetter: (_) async => true,
    );
    await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
    await tester.pump();
    await tester.pump();
    expect(find.text("Whispers are set to off, but Ella couldn't pause spoken help."), findsOneWidget);
    expect(find.byKey(const Key('upstream-capture-whispers-retry')), findsOneWidget);
  });

  testWidgets('Whispers playback recovers when the native service starts after the server read', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final states = StreamController<guardian_native.GuardianModeState>.broadcast();
    addTearDown(states.close);
    var nativeState = guardian_native.GuardianModeState.idle;
    await fixture.pump(
      tester,
      guardianAvailable: true,
      guardianModeLoader: () async => const GuardianModeInfo(
        currentMode: GuardianModeKey.custom,
        twoTierState: GuardianModeState(features: ['MEMORY_SUPPORT']),
      ),
      guardianNativeState: () => nativeState,
      guardianNativeStates: states.stream,
    );
    expect(find.text('Whispers are on. Spoken help is unavailable right now.'), findsOneWidget);
    nativeState = guardian_native.GuardianModeState.active;
    states.add(nativeState);
    await tester.pump();
    await tester.pump();
    final visibleWhisperCopy = tester
        .widgetList<Text>(find.byType(Text))
        .map((text) => text.data)
        .whereType<String>()
        .where((text) => text.contains('Whispers'))
        .toList();
    expect(
      find.text('Whispers are on — Ella can speak up when she can help.'),
      findsOneWidget,
      reason: '$visibleWhisperCopy',
    );
  });

  testWidgets('empty transcript stops saying listening after capture retires', (tester) async {
    final fixture = await _DockFixture.create(tester);
    await fixture.pump(tester);
    fixture.makeNecklaceLive();
    await tester.pump();
    await tester.tap(find.byKey(const Key('upstream-capture-view-transcript')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    fixture.provider.updateRecordingState(RecordingState.error);
    await tester.pump();
    expect(find.text('Listening for speech…'), findsNothing);
    expect(find.text('Recording is unavailable right now.'), findsOneWidget);
  });

  testWidgets('protocol rejection remains visible while the provider is pending', (tester) async {
    final fixture = await _DockFixture.create(tester);
    await fixture.pump(tester);
    fixture.provider.updateRecordingState(RecordingState.initialising);
    fixture.runtime.protocolUnavailable.value = true;
    await tester.pump();
    expect(find.text("Ella couldn't connect to transcription, so recording didn't start."), findsOneWidget);
  });

  testWidgets('a delayed successful start does not erase an earlier protocol rejection', (tester) async {
    final fixture = await _DockFixture.create(tester);
    final result = Completer<EllaCaptureStartOutcome>();
    fixture.runtime.startPhoneOverride = (_) => result.future;
    await fixture.pump(tester);
    await tester.tap(find.byKey(const Key('upstream-capture-record-phone')));
    await tester.pump();
    fixture.runtime.protocolUnavailable.value = true;
    result.complete(EllaCaptureStartOutcome.started);
    await tester.pump();
    await tester.pump();
    expect(find.text("Ella couldn't connect to transcription, so recording didn't start."), findsOneWidget);
  });
  for (final outcome in ['success', 'failure', 'retirement', 'dispose']) {
    testWidgets('pause sheet confirms committed state and fences $outcome', (tester) async {
      final fixture = await _DockFixture.create(tester);
      fixture.provider.updateRecordingState(RecordingState.record);
      fixture.provider.pauseGate = Completer<void>();
      var current = true;
      await fixture.pump(tester,
          compactPresentation: true, guardianAuthorityProvider: () => _WhisperTestAuthority(_uid, () => current));
      await tester.tap(find.byKey(const Key('upstream-capture-open-controls')));
      await tester.pumpAndSettle();
      final button = find.byKey(const Key('iris-status-pause-resume'));
      await tester.ensureVisible(button);
      await tester.tap(button);
      await tester.pump();
      expect(find.text('Paused'), findsNothing);
      expect(find.text('Pausing…'), findsWidgets);
      if (outcome == 'retirement') current = false;
      if (outcome == 'dispose') await tester.pumpWidget(const SizedBox.shrink());
      if (outcome == 'failure') {
        fixture.provider.pauseGate!.completeError(StateError('synthetic pause failure'));
      } else {
        fixture.provider.pauseGate!.complete();
      }
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      if (outcome == 'success') {
        expect(find.text('Paused'), findsWidgets);
        expect(find.text('Resume listening'), findsOneWidget);
        expect(find.byKey(const Key('iris-status-stop-capture')), findsNothing);
        await tester.tap(button);
        await tester.pump();
        expect(find.text('Paused'), findsNothing);
        expect(find.text('Pause listening'), findsOneWidget);
      } else {
        expect(find.text('Paused'), findsNothing);
        if (outcome == 'failure') {
          expect(find.text('Ella could not confirm the change. Check status and try again.'), findsWidgets);
          expect(find.text('Pause listening'), findsOneWidget);
        }
      }
      expect(tester.takeException(), isNull);
    });
  }
  for (final dark in [false, true]) {
    for (final scale in [1.0, 3.5]) {
      testWidgets('confirmed pause action readable dark=$dark scale=$scale', (tester) async {
        final fixture = await _DockFixture.create(tester);
        fixture.provider.updateRecordingState(RecordingState.record);
        await fixture.pump(tester,
            compactPresentation: true, dark: dark, textScale: scale, reduceMotion: true, size: const Size(390, 932));
        await tester.tap(find.byKey(const Key('upstream-capture-open-controls')));
        await tester.pumpAndSettle();
        final action = find.byKey(const Key('iris-status-pause-resume'));
        await tester.ensureVisible(action);
        await tester.tap(action);
        await tester.pump();
        expect(find.text('Resume listening'), findsOneWidget);
        expect(find.byKey(const Key('iris-status-stop-capture')), findsNothing);
        final rendered = tester.widget<OutlinedButton>(action);
        final style = rendered.style!.textStyle!.resolve({})!;
        expect(style.fontSize, greaterThanOrEqualTo(16));
        expect(tester.getSize(action).height, greaterThanOrEqualTo(52));
        expect(tester.takeException(), isNull);
        await tester.ensureVisible(action);
        await tester.pump();
        await tester.runAsync(() async {
          final boundary = tester.renderObject<RenderRepaintBoundary>(find.byKey(const Key('capture-fixture-render')));
          final image = await boundary.toImage();
          final bytes = await image.toByteData(format: ImageByteFormat.png);
          await File('/Users/ellaai/Documents/Codex/2026-10-07/task-4/iris-follow-on-paused-$dark-$scale.png')
              .writeAsBytes(bytes!.buffer.asUint8List());
          image.dispose();
        });
      });
    }
  }
  for (final source in ['phone']) {
    testWidgets('pending $source Resume keeps Stop escape and fences late finalizer', (tester) async {
      final fixture = await _DockFixture.create(tester);
      fixture.provider.pausedSource = source;
      fixture.provider.committedPaused = true;
      fixture.provider.updateRecordingState(RecordingState.pause);
      fixture.provider.resumeGate = Completer<void>();
      final stopGate = Completer<void>();
      var stops = 0;
      fixture.runtime.stopEscapeOverride = () async {
        stops++;
        await stopGate.future;
        fixture.provider.committedPaused = false;
        fixture.provider.updateRecordingState(RecordingState.stop);
      };
      await fixture.pump(tester, compactPresentation: true);
      await tester.tap(find.byKey(const Key('upstream-capture-open-controls')));
      await tester.pumpAndSettle();
      final action = find.byKey(const Key('iris-status-pause-resume'));
      await tester.ensureVisible(action);
      await tester.tap(action);
      await tester.pump();
      final stop = find.byKey(const Key('iris-status-stop-capture'));
      expect(tester.widget<TextButton>(stop).onPressed, isNotNull);
      await tester.ensureVisible(stop);
      await tester.tap(stop);
      await tester.pump();
      expect(stops, 1);
      fixture.provider.resumeGate!.complete();
      await tester.pump();
      expect(find.text('Stopping recording…'), findsWidgets);
      stopGate.complete();
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.text('Resuming…'), findsNothing);
      expect(find.text('Stopping recording…'), findsNothing);
      expect(fixture.provider.recordingState, RecordingState.stop);
    });
  }
  for(final boundary in ['producer','epoch','device','provider']) {
    testWidgets('rollover integration: $boundary retirement clears the open dock and cannot revive', (tester) async {
      final fixture=await _DockFixture.create(tester);
      await fixture.pump(tester);
      fixture.makeNecklaceLive(segments:[_segment('old-visible','Synthetic old producer words')]);
      await tester.pump();
      await tester.tap(find.byKey(const Key('upstream-capture-view-transcript')));
      await tester.pump();await tester.pump(const Duration(milliseconds:400));
      expect(find.text('Synthetic old producer words'),findsOneWidget);
      final authority=fixture.authority as _DockSessionAuthority;
      _DockCaptureProvider? replacement;
      if(boundary=='producer') fixture.provider.presentationProducer='fixture-producer-2';
      if(boundary=='epoch') authority.epoch++;
      if(boundary=='device') fixture.provider.presentationDeviceRevision++;
      if(boundary=='provider') {
        replacement=_DockCaptureProvider(preferences:upstream.SharedPreferencesUtil(),
          bleListeners:_NoBleListeners(),connectivity:CaptureConnectivityBoundary(
            initiallyConnected:true,changes:fixture.connectivity.stream,isConnected:()=>true));
        replacement.updateRecordingState(RecordingState.deviceRecord);
        replacement.segments.add(_segment('foreign','Synthetic replacement provider words'));
        fixture.runtime.replacementCapture=replacement;
        addTearDown(replacement.dispose);
      }
      fixture.provider.updateRecordingState(RecordingState.deviceRecord);
      await tester.pump();
      expect(find.text('Recording is unavailable right now.'),findsOneWidget);
      expect(find.text('Synthetic old producer words'),findsNothing);
      expect(find.text('Synthetic replacement provider words'),findsNothing);
      fixture.provider.presentationProducer='fixture-producer-1';
      authority.epoch=0;fixture.provider.presentationDeviceRevision=0;
      fixture.runtime.replacementCapture=null;
      fixture.provider.updateRecordingState(RecordingState.deviceRecord);
      await tester.pump();
      expect(find.text('Synthetic old producer words'),findsNothing);
      await tester.tap(find.byKey(const Key('upstream-capture-transcript-close')));
      await tester.pump();await tester.pump(const Duration(milliseconds:400));
      await tester.tap(find.byKey(const Key('upstream-capture-view-transcript')));
      await tester.pump();await tester.pump(const Duration(milliseconds:400));
      expect(find.text('Synthetic old producer words'),findsOneWidget,
        reason:'an explicit reopen obtains a fresh authorized read lease');
      expect(tester.takeException(),isNull);
    });
  }

}
