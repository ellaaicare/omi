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
  String recordingIdentity = 'recording-1';
  String? windowIdentity = 'window-1';
  bool captureReady = true;
  bool committedPaused = false;
  String pausedSource = 'phone';
  Completer<void>? pauseGate;
  Completer<void>? resumeGate;
  @override
  bool get canPauseLiveCapture =>
      !committedPaused && (recordingState == RecordingState.record || recordingState == RecordingState.deviceRecord);
  @override
  bool get isLiveCapturePaused => committedPaused;
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
    if (!(isCurrent?.call() ?? true)) throw StateError('stale resume');
    await resumeGate?.future;
    if (!(isCurrent?.call() ?? true)) throw StateError('stale resume');
    committedPaused = false;
    updateRecordingState(RecordingState.record);
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
  @override
  CaptureProvider get provider => capture;
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
  Future<EllaCaptureStartOutcome> connectNecklace(String uid, BtDevice device) =>
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
                  child: EllaUpstreamCaptureDock(
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
  setUp(() => guardian_native.GuardianModeService.whisperStateFence.resetForTesting());

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

  for (final whisperScale in [1.0, 3.5])
    testWidgets('supplemental dark Whispers controls actual widget scale=$whisperScale',
        (tester) async {
      Future<void> capture(String name) async {
        await tester.runAsync(() async {
          final boundary = tester.renderObject<RenderRepaintBoundary>(find.byKey(const Key('capture-fixture-render')));
          final image = await boundary.toImage();
          final bytes = (await image.toByteData(format: ImageByteFormat.png))!.buffer.asUint8List();
          final out = Directory('/Volumes/External T1/Ella-Codex-2026-10-13/foreground-dictation-build/thick-ux-supplemental-renders')..createSync(recursive: true);
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
        dark: true,
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
      await capture('dark-whispers-off-$whisperScale');
      await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
      await tester.pumpAndSettle();
      expect(find.text("Whispers are on, but Ella couldn't start spoken help."), findsOneWidget);
      await capture('dark-whispers-fake-start-failed-$whisperScale');
      await tester.ensureVisible(find.byKey(const Key('upstream-capture-whispers-retry')));
      await tester.pump();
      await capture('dark-whispers-retry-reachable-$whisperScale');
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

}
