import 'dart:async';

import 'package:awesome_notifications/awesome_notifications.dart';
import 'package:connectivity_plus_platform_interface/connectivity_plus_platform_interface.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:intl/intl.dart' show DateFormat;
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/action_item.dart';
import 'package:omi/ella/demo/ella_access_demo_fixtures.dart';
import 'package:omi/ella/models/guardian_mode.dart' as guardian_model;
import 'package:omi/ella/models/today_card.dart';
import 'package:omi/ella/pages/ella_entitlement_gate_page.dart';
import 'package:omi/ella/pages/ella_settings_page.dart';
import 'package:omi/ella/pages/ella_workspace_page.dart';
import 'package:omi/ella/services/ella_entitlement_service.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/ella/services/ella_public_surface_policy.dart';
import 'package:omi/ella/services/ella_workspace_status.dart';
import 'package:omi/ella/services/guardian_alert_history_api.dart';
import 'package:omi/ella/services/guardian_mode_api.dart' as guardian_api;
import 'package:omi/ella/services/guardian_mode_service.dart';
import 'package:omi/ella/services/today_card_repository.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/main.dart';
import 'package:omi/mobile/mobile_app.dart';
import 'package:omi/pages/chat/page.dart';
import 'package:omi/pages/home/page.dart';
import 'package:omi/pages/home/today_page.dart';
import 'package:omi/providers/action_items_provider.dart';
import 'package:omi/providers/app_provider.dart';
import 'package:omi/providers/audio_route_provider.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/connectivity_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/developer_mode_provider.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/ella_entitlement_provider.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/providers/integration_provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/providers/user_provider.dart';
import 'package:omi/providers/voice_recorder_provider.dart';
import 'package:omi/services/notifications.dart';
import 'package:omi/services/notifications/ella_notification_handler.dart';
import 'package:omi/services/services.dart';
import 'package:omi/utils/ella_pilot_locale_policy.dart';

const bool _isConfiguredCallPathRun = bool.fromEnvironment('ELLA_CALL_PATH_CONFIG_TEST');

class _TestConnectivityPlatform extends ConnectivityPlatform {
  @override
  Future<List<ConnectivityResult>> checkConnectivity() async => [ConnectivityResult.none];

  @override
  Stream<List<ConnectivityResult>> get onConnectivityChanged => const Stream.empty();
}

class _NoRefreshMessageProvider extends MessageProvider {
  _NoRefreshMessageProvider({required super.chatAppsRetriever});

  @override
  Future<void> refreshMessages({bool dropdownSelected = false}) async {}
}

class _FixedActionItemsProvider extends ActionItemsProvider {
  _FixedActionItemsProvider(this.items);

  final List<ActionItemWithMetadata> items;

  @override
  List<ActionItemWithMetadata> get actionItems => items;

  @override
  Future<void> fetchActionItems({bool showShimmer = false}) async {}
}

class _NoRefreshConversationProvider extends ConversationProvider {
  @override
  Future<void> ensureFreshConversations() async {}
}

class _FixedTodayCardRepository implements TodayCardRepository {
  _FixedTodayCardRepository(this.response, {this.onFetch});

  final TodayCardResponse response;
  final VoidCallback? onFetch;

  @override
  Future<TodayCardResponse> fetch({required String uid}) async {
    onFetch?.call();
    return response;
  }
}

class _MemoryTodayCardCache implements TodayCardCache {
  TodayCard? card;

  @override
  Future<void> clear({required String uid}) async => card = null;

  @override
  Future<TodayCard?> read({required String uid}) async => card;

  @override
  Future<void> write({required String uid, required TodayCard card}) async => this.card = card;
}

class _FailingEntitlementTransport implements EllaEntitlementTransport {
  @override
  Future<EllaEntitlement> fetch() => Future.error(const FormatException('test transport unavailable'));

  @override
  Future<EllaEntitlement> redeem(String code) => Future.error(const FormatException('test transport unavailable'));
}

class _Marker extends StatelessWidget {
  const _Marker(this.label);

  final String label;

  @override
  Widget build(BuildContext context) => Text(label, textDirection: TextDirection.ltr);
}

class _PushCountingObserver extends NavigatorObserver {
  int pushes = 0;

  @override
  void didPush(Route<dynamic> route, Route<dynamic>? previousRoute) {
    pushes++;
    super.didPush(route, previousRoute);
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUpAll(() async {
    ConnectivityPlatform.instance = _TestConnectivityPlatform();
    try {
      await ServiceManager.init();
    } catch (_) {
      // Another test may already have initialized the process-wide services.
    }
  });

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    NotificationUtil.debugNavigationDispatcher = null;
  });

  tearDown(() {
    NotificationUtil.debugNavigationDispatcher = null;
  });

  testWidgets('startup routing refuses public gallery and auto-call bypasses while retaining internal tools', (
    tester,
  ) async {
    if (_isConfiguredCallPathRun) {
      expect(isEllaAccessDemoGalleryConfigured, isTrue);
      expect(isEllaDebugAutoCallConfigured, isTrue);
      expect(SharedPreferencesUtil.isTodayDesignPreviewConfigured, isTrue);
      expect(SharedPreferencesUtil.isTodayDesignPreviewEnabled, !SharedPreferencesUtil.isPublicBuild);
      expect(isEllaEntitlementStubConfigured, isTrue);
      expect(isEllaEntitlementGateEnabled, SharedPreferencesUtil.isPublicBuild || isEllaInternalPilotEnabled);
      expect(isHermesProvisioningGateEnabled, SharedPreferencesUtil.isPublicBuild);
    }

    await tester.pumpWidget(
      const MaterialApp(
        home: EllaStartupRouteGuard(
          child: _Marker('normal-startup'),
          demoGallery: _Marker('demo-gallery'),
          debugHome: _Marker('debug-home'),
        ),
      ),
    );

    if (SharedPreferencesUtil.isPublicBuild) {
      expect(find.text('normal-startup'), findsOneWidget);
      expect(find.text('demo-gallery'), findsNothing);
      expect(find.text('debug-home'), findsNothing);

      final entitlementProvider = EllaEntitlementProvider(
        transport: _FailingEntitlementTransport(),
        authenticatedUidChanges: const Stream<String?>.empty(),
        initialAuthenticatedUid: 'test-user',
      );
      await tester.pumpWidget(
        ChangeNotifierProvider.value(
          value: entitlementProvider,
          child: const MaterialApp(
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            home: HomePageWrapper(provisioningGateStartOnMount: false),
          ),
        ),
      );
      expect(find.byType(EllaEntitlementGatePage), findsOneWidget);
      expect(find.byType(HomePage), findsNothing);
      await tester.pumpWidget(const SizedBox.shrink());
      entitlementProvider.dispose();
    } else if (isEllaAccessDemoGalleryConfigured) {
      expect(find.text('demo-gallery'), findsOneWidget);
    } else {
      expect(find.text('normal-startup'), findsOneWidget);
    }

    await tester.pumpWidget(
      const MaterialApp(
        home: EllaStartupRouteGuard(
          isPublicBuild: false,
          demoGalleryConfigured: true,
          debugAutoCallConfigured: true,
          provisioningGateEnabled: false,
          child: _Marker('internal-normal'),
          demoGallery: _Marker('internal-gallery'),
          debugHome: _Marker('internal-home'),
        ),
      ),
    );
    expect(find.text('internal-gallery'), findsOneWidget);

    await tester.pumpWidget(
      const MaterialApp(
        home: EllaStartupRouteGuard(
          isPublicBuild: false,
          demoGalleryConfigured: false,
          debugAutoCallConfigured: true,
          provisioningGateEnabled: false,
          child: _Marker('internal-normal'),
          demoGallery: _Marker('internal-gallery'),
          debugHome: _Marker('internal-home'),
        ),
      ),
    );
    expect(find.text('internal-home'), findsOneWidget);
  });

  test('public mode ignores stale preview/demo state and refuses entitlement demo transports', () async {
    SharedPreferences.setMockInitialValues({'demoMode': true});
    await SharedPreferencesUtil.init();
    final preferences = SharedPreferencesUtil();

    expect(preferences.demoMode, SharedPreferencesUtil.isPublicBuild ? isFalse : isTrue);
    expect(isEllaEntitlementStubEnabled, !SharedPreferencesUtil.isPublicBuild && isEllaEntitlementStubConfigured);

    preferences.demoMode = true;
    if (SharedPreferencesUtil.isPublicBuild) {
      expect(preferences.demoMode, isFalse);
      expect(preferences.getBool('demoMode'), isFalse);
    }

    final provider = EllaEntitlementProvider(
      authenticatedUidChanges: const Stream<String?>.empty(),
      initialAuthenticatedUid: 'test-user',
    );
    addTearDown(provider.dispose);
    expect(provider.usesDemoTransport, !SharedPreferencesUtil.isPublicBuild);

    final explicitDemo = EllaEntitlementProvider.demo(initialEntitlement: EllaAccessDemoFixtures.active);
    addTearDown(explicitDemo.dispose);
    expect(explicitDemo.usesDemoTransport, !SharedPreferencesUtil.isPublicBuild);
    expect(explicitDemo.isActive, !SharedPreferencesUtil.isPublicBuild);
  });

  test('invitation Whispers require configured capability and exact Firebase identity', () async {
    expect(
      allowsGuardianSurface(
        isPublicBuild: true,
        isInvitationBuild: false,
        guardianConfigured: true,
        guardianAuthenticated: true,
      ),
      isFalse,
    );
    expect(
      allowsGuardianSurface(
        isPublicBuild: true,
        isInvitationBuild: true,
        guardianConfigured: true,
        guardianAuthenticated: true,
      ),
      isTrue,
    );
    expect(
      allowsGuardianSurface(
        isPublicBuild: true,
        isInvitationBuild: true,
        guardianConfigured: true,
        guardianAuthenticated: false,
      ),
      isFalse,
    );
    expect(
      allowsGuardianCareSurface(
        isPublicBuild: true,
        isInvitationBuild: true,
        guardianConfigured: true,
        guardianAuthenticated: true,
      ),
      isFalse,
    );

    var backendCalls = 0;
    var localCalls = 0;
    final history = await GuardianAlertHistoryApi.fetch(
      guardianAllowed: false,
      backendLoader: (_) async {
        backendCalls++;
        return null;
      },
      localLoader: (_) async {
        localCalls++;
        return [];
      },
    );
    expect(history.source, GuardianAlertHistorySource.disabled);
    expect(backendCalls, 0);
    expect(localCalls, 0);

    var modeCalls = 0;
    final client = MockClient((_) async {
      modeCalls++;
      return http.Response('{}', 200);
    });
    expect((await guardian_api.getGuardianMode(guardianAllowed: false)).isFailure, isTrue);
    expect(await guardian_api.getGuardianPresets(guardianAllowed: false, client: client), isEmpty);
    expect(modeCalls, 0);

    if (!allowsGuardianSurface()) {
      const channel = MethodChannel('com.ellaaicare.ella/guardian_mode');
      final nativeCalls = <MethodCall>[];
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
        nativeCalls.add(call);
        return {'status': 'active'};
      });
      addTearDown(
        () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, null),
      );
      await expectLater(GuardianModeService().start(), throwsA(isA<StateError>()));
      expect(nativeCalls, isEmpty);
    }

    expect(
      EllaNotificationHandler.isGuardianPayload({'type': 'ella_notification', 'trigger_type': 'wake_word'}),
      isTrue,
    );
  });

  testWidgets('Home IndexedStack mounts actual Today and Chat with public-safe runtime behavior', (tester) async {
    var catalogCalls = 0;
    var todayCardCalls = 0;
    var guardianModeWrites = 0;
    var guardianNativeStarts = 0;
    guardian_model.GuardianModeState? writtenGuardianState;
    final runtimeNow = DateTime(2032, 5, 6, 9, 41);
    final previewNow = DateTime(2025, 7, 24, 9, 41);
    const previewEnabled = SharedPreferencesUtil.isTodayDesignPreviewEnabled;
    final todayCard = TodayCard(
      id: previewEnabled ? 'preview-daily-memo' : 'runtime-daily-memo',
      version: 1,
      kind: TodayCardKind.recap,
      eyebrow: 'FOR YOU TODAY',
      headline: previewEnabled ? 'Preview daily memo' : 'Runtime daily memo',
      body: previewEnabled ? 'The scissors turned up beside the garden.' : 'A verified Hermes thought for today.',
      generatedAt: previewEnabled ? previewNow : runtimeNow,
      sourceRefs: const [TodayCardSourceRef(kind: 'hermes_memory', id: 'memory-1', versionId: 'v1')],
    );
    final actionItemsProvider = _FixedActionItemsProvider([
      ActionItemWithMetadata(
        id: 'runtime-reminder',
        description: 'Runtime reminder',
        completed: false,
        dueAt: DateTime(2032, 5, 6, 10),
      ),
      ActionItemWithMetadata(
        id: 'preview-reminder',
        description: 'Preview reminder',
        completed: false,
        dueAt: DateTime(2025, 7, 24, 10),
      ),
    ]);
    final messageProvider = _NoRefreshMessageProvider(
      chatAppsRetriever: () async {
        catalogCalls++;
        return [];
      },
    );
    final conversationProvider = _NoRefreshConversationProvider();
    final captureProvider = CaptureProvider();
    final deviceProvider = DeviceProvider();
    final audioRouteProvider = AudioRouteProvider();

    await tester.binding.setSurfaceSize(const Size(1200, 2000));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    await tester.pumpWidget(
      MultiProvider(
        providers: [
          ChangeNotifierProvider(create: (_) => HomeProvider()),
          ChangeNotifierProvider<ActionItemsProvider>.value(value: actionItemsProvider),
          ChangeNotifierProvider<AudioRouteProvider>.value(value: audioRouteProvider),
          ChangeNotifierProvider<CaptureProvider>.value(value: captureProvider),
          ChangeNotifierProvider<ConversationProvider>.value(value: conversationProvider),
          ChangeNotifierProvider<DeviceProvider>.value(value: deviceProvider),
          ChangeNotifierProvider<MessageProvider>.value(value: messageProvider),
          ChangeNotifierProvider(create: (_) => AppProvider()),
          ChangeNotifierProvider(create: (_) => ConnectivityProvider()),
          ChangeNotifierProvider(create: (_) => VoiceRecorderProvider()),
          ChangeNotifierProvider(create: (_) => IntegrationProvider()),
        ],
        child: MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: HomePage(
            runtimeSideEffectsEnabled: false,
            pagesOverride: [
              TodayPage(
                nowProvider: () => runtimeNow,
                todayCardRepository: _FixedTodayCardRepository(
                  TodayCardResponse(
                    contractVersion: todayCardContractVersion,
                    status: TodayCardStatus.ready,
                    card: todayCard,
                  ),
                  onFetch: () => todayCardCalls++,
                ),
                todayCardCache: _MemoryTodayCardCache(),
                todayCardUidOverride: 'test-user',
                todayCardReadyOverride: true,
                guardianAvailability: () =>
                    (SharedPreferencesUtil.isPublicBuild || isEllaInternalPilotEnabled) && isEllaGuardianConfigured,
                guardianModeLoader: () async => const guardian_model.GuardianModeInfo(
                  currentMode: guardian_model.GuardianModeKey.off,
                  twoTierState: guardian_model.GuardianModeState(),
                ),
                guardianModeSetter: (state) async {
                  guardianModeWrites++;
                  writtenGuardianState = state;
                  return true;
                },
                guardianNativeStart: () async {
                  guardianNativeStarts++;
                },
                guardianNativeStop: () async {},
              ),
              const ChatPage(isPivotBottom: true),
              const _Marker('voice'),
              const _Marker('settings'),
            ],
          ),
        ),
      ),
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 350));

    expect(find.byType(HomePage), findsOneWidget);
    expect(find.byType(TodayPage, skipOffstage: false), findsOneWidget);
    expect(find.byType(ChatPage, skipOffstage: false), findsOneWidget);
    expect(catalogCalls, SharedPreferencesUtil.isPublicBuild ? 0 : 1);
    final expectedNow = previewEnabled ? previewNow : runtimeNow;
    expect(find.text(DateFormat('EEEE · MMMM d').format(expectedNow).toUpperCase()), findsOneWidget);
    expect(find.text(previewEnabled ? 'Preview reminder' : 'Runtime reminder'), findsOneWidget);
    expect(find.text(previewEnabled ? 'Runtime reminder' : 'Preview reminder'), findsNothing);

    expect(todayCardCalls, 1);
    expect(find.byKey(const Key('today-card-semantics')), findsOneWidget);
    expect(find.text(previewEnabled ? 'Preview daily memo' : 'Runtime daily memo'), findsOneWidget);
    if ((SharedPreferencesUtil.isPublicBuild || isEllaInternalPilotEnabled) && isEllaGuardianConfigured) {
      expect(find.byKey(const Key('guardian-whispers-control')), findsOneWidget);
      expect(find.textContaining('Whispers are off'), findsOneWidget);
      expect(guardianNativeStarts, 0);

      await tester.tap(
        find.descendant(of: find.byKey(const Key('guardian-whispers-control')), matching: find.byType(Switch)),
      );
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 200));

      expect(guardianModeWrites, 1);
      expect(writtenGuardianState?.features, ['ACTIVE_SUPPORT']);
      expect(guardianNativeStarts, 1);
      expect(find.textContaining('Whispers are on'), findsOneWidget);
    } else if (!allowsGuardianSurface()) {
      expect(find.byKey(const Key('guardian-whispers-control')), findsNothing);
      expect(find.byKey(const Key('whispers-history-entry')), findsNothing);
    }
    expect(tester.takeException(), isNull);

    await tester.pumpWidget(const SizedBox.shrink());
    actionItemsProvider.dispose();
    audioRouteProvider.dispose();
    captureProvider.dispose();
    conversationProvider.dispose();
    deviceProvider.dispose();
    messageProvider.dispose();
  });

  test('notification dispatch denies every hidden public alias and preserves internal routes', () async {
    final dispatched = <String>[];
    NotificationUtil.debugNavigationDispatcher = (navigateTo, autoMessage) => dispatched.add(navigateTo);
    const hiddenRoutes = [
      '/daily-summary/summary-id',
      '/DailySummaryDetail/summary-id',
      '/wrapped',
      '/Wrapped2025',
      '/facts',
      '/Memories',
      '/apps/inherited-app',
      '/conversation/inherited-conversation',
      '/action-items',
      '/guardian',
      '/guardian-alerts',
      '/whispers',
      '/caregivers',
      '/emergency',
      '/settings/guardian',
    ];

    for (final route in hiddenRoutes) {
      final action = ReceivedAction().fromMap({
        'payload': {'navigate_to': route},
      });
      await NotificationUtil.onActionReceivedMethodImpl(action);
    }

    final expectedHiddenDispatches = SharedPreferencesUtil.isPublicBuild
        ? const <String>[]
        : allowsGuardianSurface()
        ? hiddenRoutes
        : hiddenRoutes.take(9).toList(growable: false);
    expect(dispatched, expectedHiddenDispatches);

    final allowed = ReceivedAction().fromMap({
      'payload': {'navigate_to': '/chat'},
    });
    await NotificationUtil.onActionReceivedMethodImpl(allowed);
    expect(dispatched.last, '/chat');
  });

  testWidgets('Home alternate navigation denies hidden public routes and preserves internal dispatch', (tester) async {
    final observer = _PushCountingObserver();

    await tester.pumpWidget(
      MultiProvider(
        providers: [
          ChangeNotifierProvider(create: (_) => HomeProvider()),
          ChangeNotifierProvider(create: (_) => ConnectivityProvider()),
        ],
        child: MaterialApp(
          navigatorKey: MyApp.navigatorKey,
          navigatorObservers: [observer],
          home: const HomePage(
            navigateToRoute: '/facts',
            runtimeSideEffectsEnabled: false,
            pagesOverride: [_Marker('today'), _Marker('chat'), _Marker('voice'), _Marker('settings')],
          ),
        ),
      ),
    );

    expect(observer.pushes, SharedPreferencesUtil.isPublicBuild ? 1 : 2);
    expect(tester.takeException(), isNull);
  });

  testWidgets('Settings does not construct developer provider before Advanced Settings is opened', (tester) async {
    var providerConstructions = 0;
    var statusCalls = 0;
    var urlCalls = 0;

    await tester.pumpWidget(
      MultiProvider(
        providers: [
          ChangeNotifierProvider(
            create: (_) {
              providerConstructions++;
              return DeveloperModeProvider(
                webhooksStatusLoader: () async {
                  statusCalls++;
                  return {
                    'memory_created': false,
                    'realtime_transcript': false,
                    'audio_bytes': false,
                    'day_summary': false,
                  };
                },
                webhookUrlLoader: ({required type}) async {
                  urlCalls++;
                  return '';
                },
              )..initialize();
            },
          ),
          ChangeNotifierProvider(create: (_) => DeviceProvider()),
          ChangeNotifierProvider(create: (_) => UserProvider()),
        ],
        child: const MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: EllaSettingsPage(runtimeSideEffectsEnabled: false, authenticatedUidOverride: ''),
        ),
      ),
    );
    await tester.pump();

    expect(providerConstructions, 0);
    expect(statusCalls, 0);
    expect(urlCalls, 0);
    expect(find.byType(EllaSettingsPage), findsOneWidget);
    expect(find.text('Ella workspace'), findsNothing);
    if (!allowsGuardianSurface()) {
      for (final key in const [
        'guardian-mode-settings-entry',
        'care-team-settings-entry',
        'emergency-contact-settings-entry',
        'alert-channels-settings-entry',
        'guardian-history-settings-entry',
      ]) {
        expect(find.byKey(Key(key)), findsNothing);
      }
    }
    expect(tester.takeException(), isNull);
  });

  testWidgets('workspace omits Whispers route when Guardian is scoped out', (tester) async {
    const status = EllaWorkspaceStatus(
      email: 'pilot@example.com',
      workspaceVerified: false,
      workspaceFingerprint: '',
      bindingRevision: 0,
      lastVerifiedAt: null,
      chat: EllaRouteVerification.notVerified,
      voice: EllaRouteVerification.notVerified,
      whispers: EllaRouteVerification.notVerified,
      quarantinedAudioCount: 0,
    );
    await tester.pumpWidget(
      const MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: EllaWorkspacePage(statusOverride: status),
      ),
    );
    expect(find.byKey(const Key('workspace-whispers-route')), allowsGuardianSurface() ? findsOneWidget : findsNothing);
  });

  test('developer provider initialization and request methods fail closed publicly', () async {
    var statusCalls = 0;
    var urlCalls = 0;
    final provider = DeveloperModeProvider(
      webhooksStatusLoader: () async {
        statusCalls++;
        return {'memory_created': false, 'realtime_transcript': false, 'audio_bytes': false, 'day_summary': false};
      },
      webhookUrlLoader: ({required type}) async {
        urlCalls++;
        return '';
      },
    );
    addTearDown(provider.dispose);

    await provider.initialize();
    expect(statusCalls, SharedPreferencesUtil.isPublicBuild ? 0 : 1);
    expect(urlCalls, SharedPreferencesUtil.isPublicBuild ? 0 : 4);

    await provider.getWebhooksStatus();
    await provider.loadWebhookUrl(type: 'audio_bytes');
    expect(statusCalls, SharedPreferencesUtil.isPublicBuild ? 0 : 2);
    expect(urlCalls, SharedPreferencesUtil.isPublicBuild ? 0 : 5);
  });
}
