import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:flutter_foreground_task/flutter_foreground_task.dart';
import 'package:provider/provider.dart';
import 'package:upgrader/upgrader.dart';

import 'package:uuid/uuid.dart';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter_timezone/flutter_timezone.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/backend/http/api/users.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/app.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/geolocation.dart';
import 'package:omi/main.dart';
import 'package:omi/pages/apps/app_detail/app_detail.dart';
import 'package:omi/pages/chat/page.dart';
import 'package:omi/pages/conversation_detail/page.dart';
import 'package:omi/pages/conversations/conversations_page.dart';
import 'package:omi/pages/memories/page.dart';
import 'package:omi/pages/settings/daily_summary_detail_page.dart';
import 'package:omi/pages/settings/data_privacy_page.dart';
import 'package:omi/pages/settings/settings_drawer.dart';
import 'package:omi/pages/settings/wrapped_2025_page.dart';
import 'package:omi/ella/pages/ella_settings_page.dart';
import 'package:omi/ella/pages/ella_voice_chat_page.dart';
import 'package:omi/providers/app_provider.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/connectivity_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/announcement_provider.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/services/announcement_service.dart';
import 'package:omi/services/notifications.dart';
import 'package:omi/services/notifications/daily_reflection_notification.dart';
import 'package:omi/utils/analytics/mixpanel.dart';
import 'package:omi/utils/audio/foreground.dart';
import 'package:omi/utils/enums.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/platform/platform_manager.dart';
import 'package:omi/utils/platform/platform_service.dart';
import 'package:omi/widgets/freemium_switch_dialog.dart';
import 'package:omi/widgets/upgrade_alert.dart';
import 'package:omi/widgets/bottom_nav_bar.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/widgets/ella_emergency_button.dart';
import 'widgets/battery_info_widget.dart';
import 'package:omi/services/agora_service.dart';

class HomePageWrapper extends StatefulWidget {
  final String? navigateToRoute;
  final String? autoMessage;
  const HomePageWrapper({super.key, this.navigateToRoute, this.autoMessage});

  @override
  State<HomePageWrapper> createState() => _HomePageWrapperState();
}

class _HomePageWrapperState extends State<HomePageWrapper> {
  String? _navigateToRoute;
  String? _autoMessage;

  @override
  void initState() {
    WidgetsBinding.instance.addPostFrameCallback((_) async {
      if (mounted) {
        context.read<DeviceProvider>().periodicConnect('coming from HomePageWrapper', boundDeviceOnly: true);
      }
      if (SharedPreferencesUtil().notificationsEnabled) {
        NotificationService.instance.register();
        NotificationService.instance.saveNotificationToken();

        // Schedule daily reflection notification if enabled
        if (SharedPreferencesUtil().dailyReflectionEnabled) {
          DailyReflectionNotification.scheduleDailyNotification(channelKey: 'channel');
        }
      }
    });
    _navigateToRoute = widget.navigateToRoute;
    _autoMessage = widget.autoMessage;
    super.initState();
  }

  @override
  Widget build(BuildContext context) {
    return HomePage(navigateToRoute: _navigateToRoute, autoMessage: _autoMessage);
  }
}

class HomePage extends StatefulWidget {
  final String? navigateToRoute;
  final String? autoMessage;
  const HomePage({super.key, this.navigateToRoute, this.autoMessage});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> with WidgetsBindingObserver, TickerProviderStateMixin {
  ForegroundUtil foregroundUtil = ForegroundUtil();
  List<Widget> screens = [Container(), const SizedBox(), const SizedBox(), const SizedBox()];

  final _upgrader = MyUpgrader(debugLogging: false, debugDisplayOnce: false);
  bool scriptsInProgress = false;
  StreamSubscription? _notificationStreamSubscription;

  final GlobalKey<State<ConversationsPage>> _conversationsPageKey = GlobalKey<State<ConversationsPage>>();
  late final List<Widget> _pages;

  // Freemium switch handler for auto-switch dialogs
  final FreemiumSwitchHandler _freemiumHandler = FreemiumSwitchHandler();

  CaptureProvider? _captureProvider;

  // Agora test call
  final AgoraService _agoraService = AgoraService();
  bool _isTestCallActive = false;

  void _initiateApps() {
    context.read<AppProvider>().getApps();
    context.read<AppProvider>().getPopularApps();
  }

  void _scrollToTop(int pageIndex) {
    if (pageIndex == 0) {
      final conversationsState = _conversationsPageKey.currentState;
      if (conversationsState != null) {
        (conversationsState as dynamic).scrollToTop();
      }
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    super.didChangeAppLifecycleState(state);
    String event = '';
    if (state == AppLifecycleState.paused) {
      event = 'App is paused';
    } else if (state == AppLifecycleState.resumed) {
      event = 'App is resumed';

      // Reload convos
      if (mounted) {
        Provider.of<ConversationProvider>(context, listen: false).refreshConversations();
        Provider.of<CaptureProvider>(context, listen: false).refreshInProgressConversations();
      }
    } else if (state == AppLifecycleState.hidden) {
      event = 'App is hidden';
    } else if (state == AppLifecycleState.detached) {
      event = 'App is detached';
    } else {
      return;
    }
    Logger.debug(event);
    PlatformManager.instance.crashReporter.logInfo(event);
  }

  ///Screens with respect to subpage
  final Map<String, Widget> screensWithRespectToPath = {
    '/facts': const MemoriesPage(),
  };
  bool? previousConnection;

  void _onReceiveTaskData(dynamic data) async {
    if (data is! Map<String, dynamic>) return;
    if (!(data.containsKey('latitude') && data.containsKey('longitude'))) return;
    await updateUserGeolocation(
      geolocation: Geolocation(
        latitude: data['latitude'],
        longitude: data['longitude'],
        accuracy: data['accuracy'],
        altitude: data['altitude'],
        time: DateTime.parse(data['time']).toUtc(),
      ),
    );
  }

  @override
  void initState() {
    _pages = [
      ConversationsPage(key: _conversationsPageKey),
      const ChatPage(isPivotBottom: true),
      const EllaVoiceChatPage(),
      const EllaSettingsPage(),
    ];
    SharedPreferencesUtil().onboardingCompleted = true;
    updateUserOnboardingState(completed: true);

    // Navigate uri
    Uri? navigateToUri;
    var pageAlias = "home";
    var homePageIdx = 0;
    String? detailPageId;

    if (widget.navigateToRoute != null && widget.navigateToRoute!.isNotEmpty) {
      navigateToUri = Uri.tryParse("http://localhost.com${widget.navigateToRoute!}");
      Logger.debug("initState ${navigateToUri?.pathSegments.join("...")}");
      var segments = navigateToUri?.pathSegments ?? [];
      if (segments.isNotEmpty) {
        pageAlias = segments[0];
      }
      if (segments.length > 1) {
        detailPageId = segments[1];
      }

      switch (pageAlias) {
        case "chat":
          homePageIdx = 1;
          break;
        case "voice":
          homePageIdx = 2;
          break;
        case "settings":
          homePageIdx = 3;
          break;
      }
    }

    // Home controller
    context.read<HomeProvider>().selectedIndex = homePageIdx;
    WidgetsBinding.instance.addObserver(this);

    WidgetsBinding.instance.addPostFrameCallback((_) async {
      _initiateApps();

      // ForegroundUtil.requestPermissions();
      if (!PlatformService.isDesktop) {
        await ForegroundUtil.initializeForegroundService();
        await ForegroundUtil.startForegroundTask();
      }
      if (mounted) {
        await Provider.of<HomeProvider>(context, listen: false).setUserPeople();
      }
      if (mounted) {
        await Provider.of<CaptureProvider>(context, listen: false)
            .streamDeviceRecording(device: Provider.of<DeviceProvider>(context, listen: false).connectedDevice);
      }

      // Navigate
      if (!mounted) return;
      switch (pageAlias) {
        case "apps":
          if (detailPageId != null && detailPageId.isNotEmpty) {
            final appProvider = context.read<AppProvider>();
            var app = await appProvider.getAppFromId(detailPageId);
            if (app != null && mounted) {
              Navigator.push(
                context,
                MaterialPageRoute(
                  builder: (context) => AppDetailPage(app: app),
                ),
              );
            }
          }
          break;
        case "chat":
          Logger.debug('inside chat alias $detailPageId');
          if (detailPageId != null && detailPageId.isNotEmpty) {
            var appId = detailPageId != "omi" ? detailPageId : ''; // omi ~ no select
            if (mounted) {
              var appProvider = Provider.of<AppProvider>(context, listen: false);
              var messageProvider = Provider.of<MessageProvider>(context, listen: false);
              App? selectedApp;
              if (appId.isNotEmpty) {
                selectedApp = await appProvider.getAppFromId(appId);
              }
              appProvider.setSelectedChatAppId(appId);
              await messageProvider.refreshMessages();
              if (messageProvider.messages.isEmpty) {
                messageProvider.sendInitialAppMessage(selectedApp);
              }
            }
          } else {
            if (mounted) {
              await Provider.of<MessageProvider>(context, listen: false).refreshMessages();
            }
          }
          // Chat is already shown via IndexedStack at index 1 (set above).
          // If there's an auto-message (e.g., from daily reflection notification),
          // inject it directly via MessageProvider instead of pushing a duplicate route.
          final autoMessageToSend = widget.autoMessage;
          if (autoMessageToSend != null && autoMessageToSend.isNotEmpty) {
            WidgetsBinding.instance.addPostFrameCallback((_) {
              if (mounted) {
                final messageProvider = Provider.of<MessageProvider>(context, listen: false);
                Future.delayed(const Duration(milliseconds: 800), () {
                  if (mounted) {
                    final aiMessage = ServerMessage(
                      const Uuid().v4(),
                      DateTime.now(),
                      autoMessageToSend,
                      MessageSender.ai,
                      MessageType.text,
                      null,
                      false,
                      [],
                      [],
                      [],
                      askForNps: false,
                    );
                    messageProvider.addMessage(aiMessage);
                  }
                });
              }
            });
          }
          break;
        case "settings":
          // Use context from the current widget instead of navigator key for bottom sheet
          WidgetsBinding.instance.addPostFrameCallback((_) {
            if (mounted) {
              SettingsDrawer.show(context);
            }
          });
          if (detailPageId == 'data-privacy') {
            MyApp.navigatorKey.currentState?.push(
              MaterialPageRoute(
                builder: (context) => const DataPrivacyPage(),
              ),
            );
          }
          break;
        case "facts":
          MyApp.navigatorKey.currentState?.push(
            MaterialPageRoute(
              builder: (context) => const MemoriesPage(),
            ),
          );
          break;
        case "conversation":
          // Handle conversation deep link: /conversation/{id}?share=1
          if (detailPageId != null && detailPageId.isNotEmpty) {
            // Check for share query param
            final shouldOpenShare = navigateToUri?.queryParameters['share'] == '1';
            final conversationId = detailPageId; // Capture non-null value

            WidgetsBinding.instance.addPostFrameCallback((_) async {
              if (!mounted) return;

              // Fetch conversation from server
              final conversation = await getConversationById(conversationId);
              if (conversation != null && mounted) {
                Navigator.push(
                  context,
                  MaterialPageRoute(
                    builder: (context) => ConversationDetailPage(
                      conversation: conversation,
                      openShareToContactsOnLoad: shouldOpenShare,
                    ),
                  ),
                );
              } else {
                Logger.debug('Conversation not found: $conversationId');
              }
            });
          }
          break;
        case "daily-summary":
          if (detailPageId != null && detailPageId.isNotEmpty) {
            // Track notification opened
            MixpanelManager().dailySummaryNotificationOpened(
              summaryId: detailPageId,
              date: '', // Date not available in navigate_to, will be fetched when detail page loads
            );

            WidgetsBinding.instance.addPostFrameCallback((_) {
              if (mounted) {
                Navigator.push(
                  context,
                  MaterialPageRoute(
                    builder: (context) => DailySummaryDetailPage(summaryId: detailPageId!),
                  ),
                );
              }
            });
          }
          break;
        case "wrapped":
          WidgetsBinding.instance.addPostFrameCallback((_) {
            if (mounted) {
              Navigator.push(
                context,
                MaterialPageRoute(
                  builder: (context) => const Wrapped2025Page(),
                ),
              );
            }
          });
          break;
        default:
      }
    });

    _listenToMessagesFromNotification();
    _listenToFreemiumThreshold();
    _checkForAnnouncements();
    _provisionEllaIfNeeded();
    super.initState();

    // After init
    FlutterForegroundTask.addTaskDataCallback(_onReceiveTaskData);

  }

  void _provisionEllaIfNeeded() async {
    if (SharedPreferencesUtil().ellaUserId.isNotEmpty) return;
    final user = FirebaseAuth.instance.currentUser;
    if (user == null) return;
    final timezone = await FlutterTimezone.getLocalTimezone();
    final name = '${SharedPreferencesUtil().givenName} ${SharedPreferencesUtil().familyName}'.trim();
    provisionEllaUser(
      firebaseUid: user.uid,
      email: user.email ?? '',
      name: name.isNotEmpty ? name : (user.displayName ?? ''),
      timezone: timezone,
    );
  }

  void _checkForAnnouncements() {
    WidgetsBinding.instance.addPostFrameCallback((_) async {
      if (!mounted) return;

      await Future.delayed(const Duration(seconds: 2));

      if (!mounted) return;

      final announcementProvider = Provider.of<AnnouncementProvider>(context, listen: false);
      final deviceProvider = Provider.of<DeviceProvider>(context, listen: false);
      await AnnouncementService().checkAndShowAnnouncements(
        context,
        announcementProvider,
        connectedDevice: deviceProvider.connectedDevice,
      );

      // Register callback for device connection to check firmware announcements
      deviceProvider.onDeviceConnected = _onDeviceConnectedForAnnouncements;
    });
  }

  void _onDeviceConnectedForAnnouncements(BtDevice device) async {
    if (!mounted) return;

    final announcementProvider = Provider.of<AnnouncementProvider>(context, listen: false);
    await AnnouncementService().showFirmwareUpdateAnnouncements(
      context,
      announcementProvider,
      device.firmwareRevision,
      device.modelNumber,
    );
  }

  void _listenToFreemiumThreshold() {
    // Listen to capture provider for freemium threshold events
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;

      _captureProvider = Provider.of<CaptureProvider>(context, listen: false);
      _captureProvider!.addListener(_onCaptureProviderChanged);
      // Connect freemium session reset callback
      _captureProvider!.onFreemiumSessionReset = () {
        _freemiumHandler.resetDialogFlag();
      };
    });
  }

  void _onCaptureProviderChanged() {
    if (!mounted || _captureProvider == null) return;

    _freemiumHandler.checkAndShowDialog(context, _captureProvider!).catchError((e) {
      Logger.debug('[Freemium] Error checking dialog: $e');
      return false;
    });
  }

  void _listenToMessagesFromNotification() {
    _notificationStreamSubscription = NotificationService.instance.listenForServerMessages.listen((message) {
      if (mounted) {
        var selectedApp = Provider.of<AppProvider>(context, listen: false).getSelectedApp();
        if (selectedApp == null || message.appId == selectedApp.id) {
          Provider.of<MessageProvider>(context, listen: false).addMessage(message);
        }
        // chatPageKey.currentState?.scrollToBottom();
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    return MyUpgradeAlert(
      upgrader: _upgrader,
      dialogStyle: Platform.isIOS ? UpgradeDialogStyle.cupertino : UpgradeDialogStyle.material,
      child: Consumer<ConnectivityProvider>(
        builder: (ctx, connectivityProvider, child) {
          bool isConnected = connectivityProvider.isConnected;
          previousConnection ??= true;

          if (previousConnection != isConnected &&
              connectivityProvider.isInitialized &&
              connectivityProvider.previousConnection != isConnected) {
            previousConnection = isConnected;
            if (!isConnected) {
              // TODO: Re-enable when internet connection banners are redesigned
              // Future.delayed(const Duration(seconds: 2), () {
              //   if (mounted && !connectivityProvider.isConnected) {
              //     ScaffoldMessenger.of(ctx).showMaterialBanner(
              //       MaterialBanner(
              //         content: const Text(
              //           'No internet connection. Please check your connection.',
              //           style: TextStyle(color: Colors.white70),
              //         ),
              //         backgroundColor: const Color(0xFF424242), // Dark gray instead of red
              //         leading: const Icon(Icons.wifi_off, color: Colors.white70),
              //         actions: [
              //           TextButton(
              //             onPressed: () {
              //               ScaffoldMessenger.of(ctx).hideCurrentMaterialBanner();
              //             },
              //             child: const Text('Dismiss', style: TextStyle(color: Colors.white70)),
              //           ),
              //         ],
              //       ),
              //     );
              //   }
              // });
            } else {
              Future.delayed(Duration.zero, () {
                // TODO: Re-enable when internet connection banners are redesigned
                // if (mounted) {
                //   ScaffoldMessenger.of(ctx).hideCurrentMaterialBanner();
                //   ScaffoldMessenger.of(ctx).showMaterialBanner(
                //     MaterialBanner(
                //       content: const Text(
                //         'Internet connection is restored.',
                //         style: TextStyle(color: Colors.white),
                //       ),
                //       backgroundColor: const Color(0xFF2E7D32), // Dark green instead of bright green
                //       leading: const Icon(Icons.wifi, color: Colors.white),
                //       actions: [
                //         TextButton(
                //           onPressed: () {
                //             if (mounted) {
                //               ScaffoldMessenger.of(ctx).hideCurrentMaterialBanner();
                //             }
                //           },
                //           child: const Text('Dismiss', style: TextStyle(color: Colors.white)),
                //         ),
                //       ],
                //       onVisible: () => Future.delayed(const Duration(seconds: 3), () {
                //         if (mounted) {
                //           ScaffoldMessenger.of(ctx).hideCurrentMaterialBanner();
                //         }
                //       }),
                //     ),
                //   );
                // }

                WidgetsBinding.instance.addPostFrameCallback((_) async {
                  if (!mounted) return;

                  final convoProvider = ctx.read<ConversationProvider>();
                  final messageProvider = ctx.read<MessageProvider>();

                  if (convoProvider.conversations.isEmpty) {
                    await convoProvider.getInitialConversations();
                  } else {
                    // Force refresh when internet connection is restored
                    await convoProvider.forceRefreshConversations();
                  }

                  if (messageProvider.messages.isEmpty) {
                    await messageProvider.refreshMessages();
                  }
                });
              });
            }
          }
          return child!;
        },
        child: Consumer<HomeProvider>(
          builder: (context, homeProvider, _) {
            return Scaffold(
              backgroundColor: Theme.of(context).colorScheme.primary,
              resizeToAvoidBottomInset: false,
              appBar: homeProvider.selectedIndex == 0 ? _buildAppBar(context) : null,
              body: DefaultTabController(
                length: 4,
                initialIndex: homeProvider.selectedIndex,
                child: GestureDetector(
                  onTap: () {
                    primaryFocus?.unfocus();
                    // context.read<HomeProvider>().memoryFieldFocusNode.unfocus();
                    // context.read<HomeProvider>().chatFieldFocusNode.unfocus();
                  },
                  child: Stack(
                    children: [
                      Column(
                        children: [
                          Expanded(
                            child: IndexedStack(
                              index: context.watch<HomeProvider>().selectedIndex,
                              children: _pages,
                            ),
                          ),
                          // Bottom padding to account for emergency button + BottomNavBar overlay
                          // Skip for chat (index 1) and voice (index 2) — they handle their own padding
                          if (context.watch<HomeProvider>().selectedIndex != 1 &&
                              context.watch<HomeProvider>().selectedIndex != 2)
                            SizedBox(
                                height: EllaSizes.emergencyButtonHeight +
                                    EllaSizes.navBarHeight +
                                    MediaQuery.of(context).padding.bottom +
                                    32),
                        ],
                      ),
                      // Emergency button -- positioned above the nav bar
                      Positioned(
                        bottom: EllaSizes.navBarHeight + MediaQuery.of(context).padding.bottom + 16,
                        left: 0,
                        right: 0,
                        child: Consumer<HomeProvider>(
                          builder: (context, home, _) {
                            if (home.selectedIndex != 0) return const SizedBox.shrink();
                            return const Center(child: EllaEmergencyButton());
                          },
                        ),
                      ),
                      Consumer<HomeProvider>(
                        builder: (context, home, child) {
                          if (home.isChatFieldFocused) {
                            return const SizedBox.shrink();
                          }

                          return BottomNavBar(
                            onTabTap: (index, isRepeat) {
                              if (isRepeat) {
                                _scrollToTop(index);
                              } else {
                                home.setIndex(index);
                              }
                            },
                          );
                        },
                      ),
                    ],
                  ),
                ),
              ),
              floatingActionButton: FloatingActionButton.extended(
                onPressed: _isTestCallActive ? _endTestCall : _startTestCall,
                icon: Icon(_isTestCallActive ? Icons.call_end : Icons.call),
                label: Text(_isTestCallActive ? 'End Test Call' : 'Test Agora Call'),
                backgroundColor: _isTestCallActive ? Colors.red : Colors.blue,
              ),
            );
          },
        ),
      ),
    );
  }

  PreferredSizeWidget _buildAppBar(BuildContext context) {
    return AppBar(
      automaticallyImplyLeading: false,
      backgroundColor: Theme.of(context).colorScheme.primary,
      leading: const Padding(
        padding: EdgeInsets.only(left: 8),
        child: Center(child: BatteryInfoWidget()),
      ),
      leadingWidth: 140,
      title: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 28,
            height: 28,
            decoration: const BoxDecoration(
              shape: BoxShape.circle,
              gradient: LinearGradient(
                begin: Alignment.topLeft,
                end: Alignment.bottomRight,
                colors: [Color(0xFF7AB5A8), Color(0xFF5A9E8F)],
              ),
            ),
            child: const Center(
              child: Text(
                'e',
                style: TextStyle(
                  fontSize: 15,
                  fontWeight: FontWeight.w300,
                  color: Colors.white,
                  fontFamily: 'Manrope',
                  height: 1.1,
                ),
              ),
            ),
          ),
          const SizedBox(width: 8),
          const Text(
            'ella',
            style: TextStyle(
              fontSize: 22,
              fontWeight: FontWeight.w600,
              fontFamily: 'Manrope',
              letterSpacing: 1,
            ),
          ),
        ],
      ),
      centerTitle: true,
      elevation: 0,
    );
  }


  Future<void> _startTestCall() async {
    // Hardcoded test values
    const String channelName = 'test-ella-mvp';
    const String token = '007eJxTYJjE4fL72dYIPbuq63EZaXIh52fvvCcZtD_au32K0BnphZwKDKamKSmWxmlJaWkmliZmKeZJyUCmWZpZalJyaqqJmVHMvemZDYGMDJKljEyMDIwMLEB8R39mJhOYZAaTLGCSl6EktbhENzUnJ1E3t6yAmcHS0hIAxZMqlw==';
    const int uid = 999;

    try {
      await _agoraService.joinChannel(channelName, token, uid);
      setState(() {
        _isTestCallActive = true;
      });

      // Show snackbar
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text('Joined Agora channel. Speak now!'),
            backgroundColor: Colors.green,
          ),
        );
      }
    } catch (e) {
      print('[HomePageState] Failed to join channel: $e');
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text('Failed to join: $e'),
            backgroundColor: Colors.red,
          ),
        );
      }
    }
  }

  Future<void> _endTestCall() async {
    await _agoraService.leaveChannel();
    setState(() {
      _isTestCallActive = false;
    });

    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Left channel')),
      );
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    // Cancel stream subscription to prevent memory leak
    _notificationStreamSubscription?.cancel();
    // Remove capture provider listener using stored reference
    if (_captureProvider != null) {
      _captureProvider!.removeListener(_onCaptureProviderChanged);
      _captureProvider!.onFreemiumSessionReset = null;
      _captureProvider = null;
    }
    // Remove device provider callback
    try {
      final deviceProvider = Provider.of<DeviceProvider>(context, listen: false);
      deviceProvider.onDeviceConnected = null;
    } catch (_) {}
    // Clean up freemium handler
    _freemiumHandler.dispose();
    // Remove foreground task callback to prevent memory leak
    FlutterForegroundTask.removeTaskDataCallback(_onReceiveTaskData);
    ForegroundUtil.stopForegroundTask();
    // Dispose Agora service
    _agoraService.dispose();
    super.dispose();
  }
}
