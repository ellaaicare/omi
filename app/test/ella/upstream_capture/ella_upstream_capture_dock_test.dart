import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
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
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/capture/connect.dart';
import 'package:omi/pages/home/today_page.dart' show GuardianModeLoader, GuardianModeSetter, GuardianNativeLifecycle;
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/onboarding_provider.dart';
import 'package:omi/services/devices.dart' as legacy_service;
import 'package:omi/services/devices/device_connection.dart' as legacy_connection;
import 'package:omi/utils/device.dart';
import 'package:omi/upstream_capture/backend/preferences.dart' as upstream;
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/backend/schema/transcript_segment.dart';
import 'package:omi/upstream_capture/providers/capture_provider.dart';
import 'package:omi/upstream_capture/services/capture/capture_seams.dart';
import 'package:omi/upstream_capture/utils/enums.dart';

import 'ella_capture_protocol_socket_cases.dart';
import 'upstream_capture_protocol_v2_cases.dart';

const _uid = 'uid-a';
final _testNecklace = BtDevice(id: 'necklace-a', name: 'Compass', type: DeviceType.omi, rssi: -40);
final _pickerNecklace =
    legacy_device.BtDevice(id: 'necklace-a', name: 'Compass', type: legacy_device.DeviceType.fieldy, rssi: -40);
final _pickerFriend =
    legacy_device.BtDevice(id: 'friend-a', name: 'Friend', type: legacy_device.DeviceType.friendPendant, rssi: -40);

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

  @override
  bool get lastConnectionConsentRequired => consentRequired;
  @override
  Future<void> prepareForExplicitDeviceSelection() async {}
  @override
  Future<bool> connectDeviceForCurrentUser(legacy_device.BtDevice device, {bool requireFreshSession = false}) async {
    connects++;
    return false;
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
  Future<CaptureProvider> Function()? bootOverride;
  Future<List<BtDevice>> Function()? discoverOverride;
  Future<EllaCaptureStartOutcome> Function(String uid)? startPhoneOverride;
  Future<EllaCaptureStartOutcome> Function(String uid, BtDevice device)? connectNecklaceOverride;
  Future<void> Function()? stopPhoneOverride;
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
  Future<void> stopPhoneCapture() => stopPhoneOverride?.call() ?? Future.value();

  @override
  Future<void> disconnectNecklace({String? deviceId}) => disconnectNecklaceOverride?.call() ?? Future.value();

  @override
  Future<void> finishConversation() => finishOverride?.call() ?? Future.value();
}

class _DockFixture {
  _DockFixture({
    required this.connectivity,
    required this.provider,
    required this.authority,
    required this.runtime,
  });

  final StreamController<bool> connectivity;
  final CaptureProvider provider;
  final EllaCaptureAuthority authority;
  final _DockRuntime runtime;

  static Future<_DockFixture> create(WidgetTester tester) async {
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
    bool guardianAvailable = false,
    GuardianModeLoader? guardianModeLoader,
    GuardianModeSetter? guardianModeSetter,
    GuardianNativeLifecycle? guardianNativeStart,
    GuardianNativeLifecycle? guardianNativeStop,
    GuardianNativeStateReader? guardianNativeState,
    Stream<guardian_native.GuardianModeState>? guardianNativeStates,
    EllaCaptureConsentRequester? consentRequester,
    String Function()? authenticatedUid,
    OnboardingProvider? onboarding,
  }) async {
    tester.view.physicalSize = size;
    tester.view.devicePixelRatio = 1;
    await tester.pumpWidget(
      ChangeNotifierProvider<OnboardingProvider>.value(
          value: onboarding ?? OnboardingProvider(),
          child: MaterialApp(
            debugShowCheckedModeBanner: false,
            theme: ellaThemeData(),
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            builder: (context, child) => MediaQuery(
              data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(textScale)),
              child: child!,
            ),
            home: Scaffold(
              body: Align(
                alignment: Alignment.bottomCenter,
                child: Padding(
                  padding: const EdgeInsets.all(14),
                  child: EllaUpstreamCaptureDock(
                    runtime: runtime,
                    authenticatedUid: authenticatedUid ?? () => _uid,
                    consentRequester: consentRequester,
                    guardianAvailability: () => guardianAvailable,
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
          )),
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
  TestWidgetsFlutterBinding.ensureInitialized();
  registerEllaCaptureProtocolSocketCases();
  registerUpstreamCaptureProtocolV2Cases();

  setUpAll(() async {
    await (FontLoader('Manrope')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf')))
        .load();
    var flutterCache = File(Platform.resolvedExecutable).parent;
    while (!File('${flutterCache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf').existsSync()) {
      flutterCache = flutterCache.parent;
    }
    final materialIcons = File('${flutterCache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf');
    await (FontLoader('MaterialIcons')
          ..addFont(materialIcons.readAsBytes().then((bytes) => ByteData.sublistView(Uint8List.fromList(bytes)))))
        .load();
  });

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
    expect(
      secondary.style!.backgroundColor!.resolve({WidgetState.pressed}),
      isNot(secondaryBackground),
    );
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
        {WidgetState.disabled}
      ]) {
        final foreground = style.foregroundColor!.resolve(states)!;
        final background = style.backgroundColor!.resolve(states)!;
        expect(_contrastRatio(foreground, background), greaterThanOrEqualTo(4.5));
      }
      expect(tester.getSize(find.byKey(const Key('upstream-capture-connect-necklace'))).height,
          greaterThanOrEqualTo(EllaSizes.minTouchTarget));
    }
    await tester.tap(find.byKey(const Key('upstream-capture-record-phone')));
    await tester.pump();
    expect(tester.widget<OutlinedButton>(find.byKey(const Key('upstream-capture-connect-necklace'))).onPressed, isNull);
    start.complete(EllaCaptureStartOutcome.unavailable);
    await tester.pump();
  });

  const renderSizes = <String, Size>{
    '320x568': Size(320, 568),
    '390x844': Size(390, 844),
    '430x932': Size(430, 932),
  };
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
          matchesGoldenFile('goldens/ella_upstream_capture_dock_after_${sizeEntry.key}_scale_$scaleName.png'),
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
        contains(DeviceUtils.getDeviceImagePath(
          deviceType: _pickerNecklace.type,
          modelNumber: _pickerNecklace.modelNumber,
          deviceName: _pickerNecklace.name,
        )));
    expect(
        artwork.map((image) => image.assetName),
        contains(DeviceUtils.getDeviceImagePath(
          deviceType: _pickerFriend.type,
          modelNumber: _pickerFriend.modelNumber,
          deviceName: _pickerFriend.name,
        )));
    expect(picker.service.discovers, 1);
    expect(picker.provider.connects, 0);
    expect(tester.takeException(), isNull);
  });

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
    picker.service.failDiscovery = false;
    await tester.tap(find.text('Try Again'));
    await tester.pump();
    await tester.pump();
    expect(picker.service.discovers, 2);
    expect(find.text('Compass'), findsOneWidget);
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

  testWidgets('Whispers distinguishes saved configuration from failed native playback', (tester) async {
    final fixture = await _DockFixture.create(tester);
    GuardianModeState? savedState;
    final save = Completer<bool>();
    await fixture.pump(
      tester,
      guardianAvailable: true,
      guardianModeLoader: () async => const GuardianModeInfo(
        currentMode: GuardianModeKey.off,
        twoTierState: GuardianModeState(),
      ),
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
    expect(find.text('Whispers was saved, but spoken playback could not start. Try again.'), findsOneWidget);
    expect(find.text('Whispers are on, but spoken playback is not available right now.'), findsOneWidget);
    expect(find.byKey(const Key('upstream-capture-whispers-retry')), findsOneWidget);
    expect(find.byKey(const Key('upstream-capture-whispers-switch')), findsOneWidget);
  });

  testWidgets('Whispers save failure keeps the verified prior state', (tester) async {
    final fixture = await _DockFixture.create(tester);
    await fixture.pump(
      tester,
      guardianAvailable: true,
      guardianModeLoader: () async => const GuardianModeInfo(
        currentMode: GuardianModeKey.off,
        twoTierState: GuardianModeState(),
      ),
      guardianModeSetter: (_) async => false,
      guardianNativeStart: () async {},
      guardianNativeStop: () async {},
      guardianNativeState: () => guardian_native.GuardianModeState.idle,
    );

    await tester.tap(find.byKey(const Key('upstream-capture-whispers-switch')));
    await tester.pump();
    await tester.pump();

    expect(find.text('Whispers could not be updated. Try again.'), findsOneWidget);
    final toggle = tester.widget<Switch>(find.byKey(const Key('upstream-capture-whispers-switch')));
    expect(toggle.value, isFalse);
  });

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
    expect(find.text('Whispers was saved off, but spoken playback could not stop. Try again.'), findsOneWidget);

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
    expect(find.text('Whispers was saved off, but spoken playback could not stop. Try again.'), findsOneWidget);
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
    expect(find.text('Whispers are on, but spoken playback is not available right now.'), findsOneWidget);
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
    expect(find.text('Whispers are on — Ella can speak up when she can help.'), findsOneWidget,
        reason: '$visibleWhisperCopy');
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
}
