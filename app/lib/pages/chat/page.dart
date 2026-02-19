import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';
import 'package:flutter/services.dart';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:collection/collection.dart';
import 'package:font_awesome_flutter/font_awesome_flutter.dart';
import 'package:provider/provider.dart';
import 'package:uuid/uuid.dart';

import 'package:omi/backend/http/api/messages.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/app.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/gen/assets.gen.dart';
import 'package:omi/pages/apps/widgets/capability_apps_page.dart';
import 'package:omi/pages/chat/widgets/ai_message.dart';
import 'package:omi/pages/chat/widgets/user_message.dart';
import 'package:omi/pages/chat/widgets/voice_recorder_widget.dart';
import 'package:omi/pages/settings/integrations_page.dart';
import 'package:omi/pages/settings/settings_drawer.dart';
import 'package:omi/providers/app_provider.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/connectivity_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/providers/integration_provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/providers/voice_recorder_provider.dart';
import 'package:omi/services/apple_health_service.dart';
import 'package:omi/utils/analytics/mixpanel.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/other/temp.dart';
import 'package:omi/widgets/dialog.dart';
import 'package:omi/widgets/bottom_nav_bar.dart';
import 'package:omi/ella/ella_theme.dart';

class ChatPage extends StatefulWidget {
  final bool isPivotBottom;
  final String? autoMessage;

  const ChatPage({
    super.key,
    this.isPivotBottom = false,
    this.autoMessage,
  });

  @override
  State<ChatPage> createState() => ChatPageState();
}

class ChatPageState extends State<ChatPage> with AutomaticKeepAliveClientMixin {
  TextEditingController textController = TextEditingController();
  late ScrollController scrollController;
  late FocusNode textFieldFocusNode;

  bool _isInitialLoad = true;

  var prefs = SharedPreferencesUtil();
  late List<App> apps;

  final scaffoldKey = GlobalKey<ScaffoldState>();

  // Track which app is pending deletion confirmation
  String? _pendingDeleteAppId;
  String? _selectedContext;

  @override
  bool get wantKeepAlive => true;

  @override
  void initState() {
    apps = prefs.appsList;
    scrollController = ScrollController();
    textFieldFocusNode = FocusNode();
    textController.addListener(() {
      setState(() {});
    });
    textFieldFocusNode.addListener(() {
      setState(() {});
      // Sync focus state to HomeProvider so BottomNavBar hides when typing
      if (widget.isPivotBottom && mounted) {
        final homeProvider = context.read<HomeProvider>();
        homeProvider.isChatFieldFocused = textFieldFocusNode.hasFocus;
        homeProvider.notifyListeners();
      }
      if (textFieldFocusNode.hasFocus) {
        // Scroll to bottom when keyboard opens, with delay to allow keyboard animation
        _ensureAtBottom(delayMs: 300);
      }
    });

    SchedulerBinding.instance.addPostFrameCallback((_) async {
      var provider = context.read<MessageProvider>();
      if (provider.messages.isEmpty) {
        provider.refreshMessages();
      }
      // Fetch enabled chat apps
      provider.fetchChatApps();
      // Sync Apple Health data if connected (ensures fresh data for health queries)
      _syncAppleHealthIfConnected();
      // Auto-focus the text field only on initial load, not on app switches
      if (_isInitialLoad) {
        Future.delayed(const Duration(milliseconds: 300), () {
          if (!mounted) return;
          final voiceRecorderProvider = context.read<VoiceRecorderProvider>();
          if (!voiceRecorderProvider.isActive && _isInitialLoad) {
            textFieldFocusNode.requestFocus();
          }
        });
      }
      // Handle auto-message from notification (e.g., daily reflection or goal advice)
      // This sends a message FROM Omi AI, not from the user
      if (widget.autoMessage != null && widget.autoMessage!.isNotEmpty && mounted) {
        // Wait for messages to load first, then add auto-message
        Future.delayed(const Duration(milliseconds: 800), () {
          if (mounted) {
            final aiMessage = ServerMessage(
              const Uuid().v4(),
              DateTime.now(),
              widget.autoMessage!,
              MessageSender.ai,
              MessageType.text,
              null,
              false,
              [],
              [],
              [],
              askForNps: false,
            );
            context.read<MessageProvider>().addMessage(aiMessage);
            // Scroll after the message is added and rendered
            Future.delayed(const Duration(milliseconds: 100), () {
              if (mounted) {
                scrollToBottom();
              }
            });
          }
        });
      }
    });
    super.initState();
  }

  bool _wasOnChatTab = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    // Refresh messages when switching TO the Chat tab (picks up voice messages)
    try {
      final homeProvider = Provider.of<HomeProvider>(context);
      final isOnChatTab = homeProvider.selectedIndex == 1;
      if (isOnChatTab && !_wasOnChatTab) {
        debugPrint('[Chat] Tab became active, refreshing from server');
        // Refresh from Firestore — voice messages now go through /v2/messages
        // so they're persisted in the same store as text chat.
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (mounted) {
            context.read<MessageProvider>().refreshMessages();
          }
        });
      }
      _wasOnChatTab = isOnChatTab;
    } catch (_) {}
  }

  @override
  void dispose() {
    textController.dispose();
    scrollController.dispose();
    textFieldFocusNode.dispose();
    super.dispose();
  }

  void _syncAppleHealthIfConnected() async {
    final appleHealthService = AppleHealthService();
    if (appleHealthService.isAvailable) {
      final integrationProvider = context.read<IntegrationProvider>();
      if (integrationProvider.isAppConnected(IntegrationApp.appleHealth)) {
        debugPrint('🍎 [Apple Health] Starting auto-sync on chat open...');
        final success = await appleHealthService.syncHealthDataToBackend(days: 7);
        debugPrint('🍎 [Apple Health] Auto-sync ${success ? "completed" : "failed"}');
      }
    }
  }

  void _openSettingsDrawer() {
    HapticFeedback.mediumImpact();
    MixpanelManager().pageOpened('Settings');
    final previousLanguage = SharedPreferencesUtil().userPrimaryLanguage;
    final previousSpeech = SharedPreferencesUtil().hasSpeakerProfile;
    final previousModel = SharedPreferencesUtil().transcriptionModel;
    SettingsDrawer.show(context);
    if (previousLanguage != SharedPreferencesUtil().userPrimaryLanguage ||
        previousSpeech != SharedPreferencesUtil().hasSpeakerProfile ||
        previousModel != SharedPreferencesUtil().transcriptionModel) {
      context.read<CaptureProvider>().onRecordProfileSettingChanged();
    }
  }

  @override
  Widget build(BuildContext context) {
    super.build(context);

    return Consumer2<MessageProvider, ConnectivityProvider>(
      builder: (context, provider, connectivityProvider, child) {
        return Scaffold(
          key: scaffoldKey,
          resizeToAvoidBottomInset: widget.isPivotBottom ? true : null,
          backgroundColor: Theme.of(context).colorScheme.primary,
          appBar: _buildAppBar(context, provider),
          // endDrawer hidden — single-app Ella setup
          body: GestureDetector(
            onTap: () {
              // Hide keyboard when tapping outside textfield
              FocusScope.of(context).unfocus();
            },
            child: Column(
              children: [
                // Messages area - takes up remaining space
                Expanded(
                  child: provider.isLoadingMessages && !provider.hasCachedMessages
                      ? Column(
                          mainAxisAlignment: MainAxisAlignment.center,
                          children: [
                            const CircularProgressIndicator(
                              valueColor: AlwaysStoppedAnimation<Color>(EllaColors.primary),
                            ),
                            const SizedBox(height: 16),
                            Text(
                              provider.firstTimeLoadingText,
                              style: const TextStyle(color: EllaColors.textPrimary),
                            ),
                          ],
                        )
                      : provider.isClearingChat
                          ? Column(
                              mainAxisAlignment: MainAxisAlignment.center,
                              children: [
                                const CircularProgressIndicator(
                                  valueColor: AlwaysStoppedAnimation<Color>(EllaColors.primary),
                                ),
                                const SizedBox(height: 16),
                                Text(
                                  context.l10n.deletingMessages,
                                  style: const TextStyle(color: EllaColors.textPrimary),
                                ),
                              ],
                            )
                          : (provider.messages.isEmpty)
                              ? Center(
                                  child: Padding(
                                    padding: const EdgeInsets.only(bottom: 32.0),
                                    child: Text(
                                        connectivityProvider.isConnected
                                            ? context.l10n.noMessagesYet
                                            : context.l10n.noInternetConnection,
                                        textAlign: TextAlign.center,
                                        style: const TextStyle(color: EllaColors.textPrimary)),
                                  ),
                                )
                              : Theme(
                                  data: Theme.of(context).copyWith(
                                    textSelectionTheme: TextSelectionThemeData(
                                      selectionColor: EllaColors.primaryLight.withOpacity(0.3),
                                      selectionHandleColor: EllaColors.primary,
                                    ),
                                  ),
                                  child: RefreshIndicator(
                                    onRefresh: () => provider.refreshMessages(),
                                    color: EllaColors.primary,
                                    backgroundColor: EllaColors.bgSecondary,
                                    child: ListView.builder(
                                      reverse: true,
                                      controller: scrollController,
                                      // With reverse:true, visual bottom=start of list (near input).
                                      // bottom padding = space between newest message and input bar.
                                      // top padding = space above oldest message (visual top).
                                      padding: const EdgeInsets.fromLTRB(18, 16, 18, 8),
                                      itemCount: provider.messages.length,
                                      itemBuilder: (context, reverseIndex) {
                                        // reverse:true renders index 0 at visual bottom (newest)
                                        final chatIndex = provider.messages.length - 1 - reverseIndex;
                                        final message = provider.messages[chatIndex];

                                        // Space between messages (top in visual terms = higher reverseIndex)
                                        final double spacing = reverseIndex == provider.messages.length - 1 ? 0 : 12;

                                        return Padding(
                                          key: ValueKey(message.id),
                                          // In reverse mode: "top" = visually above this item (toward older msgs)
                                          padding: EdgeInsets.only(top: spacing),
                                          child: message.sender == MessageSender.ai
                                              ? AIMessage(
                                                  showTypingIndicator: provider.showTypingIndicator &&
                                                      chatIndex == provider.messages.length - 1,
                                                  message: message,
                                                  sendMessage: _sendMessageUtil,
                                                  onAskOmi: (text) {
                                                    setState(() {
                                                      _selectedContext = text;
                                                    });
                                                    textFieldFocusNode.requestFocus();
                                                  },
                                                  displayOptions: provider.messages.length <= 1 &&
                                                      provider.messageSenderApp(message.appId)?.isNotPersona() == true,
                                                  appSender: provider.messageSenderApp(message.appId),
                                                  updateConversation: (ServerConversation conversation) {
                                                    context
                                                        .read<ConversationProvider>()
                                                        .updateConversation(conversation);
                                                  },
                                                  setMessageNps: (int value, {String? reason}) {
                                                    provider.setMessageNps(message, value, reason: reason);
                                                  },
                                                )
                                              : HumanMessage(
                                                  message: message,
                                                  onAskOmi: (text) {
                                                    setState(() {
                                                      _selectedContext = text;
                                                    });
                                                    textFieldFocusNode.requestFocus();
                                                  }),
                                        );
                                      },
                                    ),
                                  ),
                                ),
                ),
                // Send message area - fixed at bottom
                Container(
                  margin: const EdgeInsets.only(top: 10),
                  decoration: const BoxDecoration(
                    color: Colors.transparent,
                    borderRadius: BorderRadius.only(
                      topLeft: Radius.circular(22),
                      topRight: Radius.circular(22),
                    ),
                  ),
                  child: Consumer2<HomeProvider, VoiceRecorderProvider>(
                      builder: (context, home, voiceRecorderProvider, child) {
                    bool shouldShowSendButton(MessageProvider p) {
                      return !p.sendingMessage && !voiceRecorderProvider.isActive;
                    }

                    bool shouldShowVoiceRecorderButton() {
                      return !voiceRecorderProvider.isActive;
                    }

                    bool shouldShowMenuButton() {
                      return !voiceRecorderProvider.isActive;
                    }

                    return Column(children: [
                      // Selected images display above the send bar
                      Consumer<MessageProvider>(builder: (context, provider, child) {
                        if (provider.selectedFiles.isNotEmpty) {
                          return Container(
                            margin: const EdgeInsets.only(top: 16, bottom: 8),
                            padding: const EdgeInsets.symmetric(horizontal: 16),
                            height: 70,
                            child: ListView.builder(
                              scrollDirection: Axis.horizontal,
                              itemCount: provider.selectedFiles.length,
                              itemBuilder: (ctx, idx) {
                                return Container(
                                  margin: const EdgeInsets.only(right: 8),
                                  width: 60,
                                  height: 60,
                                  decoration: BoxDecoration(
                                    color: EllaColors.bgTertiary,
                                    borderRadius: BorderRadius.circular(16),
                                    image: provider.selectedFileTypes[idx] == 'image'
                                        ? DecorationImage(
                                            image: FileImage(provider.selectedFiles[idx]),
                                            fit: BoxFit.cover,
                                          )
                                        : null,
                                  ),
                                  child: Stack(
                                    children: [
                                      // File icon for non-images
                                      if (provider.selectedFileTypes[idx] != 'image')
                                        const Center(
                                          child: Icon(
                                            Icons.insert_drive_file,
                                            color: EllaColors.textPrimary,
                                            size: 24,
                                          ),
                                        ),
                                      // Loading indicator
                                      if (provider.isFileUploading(provider.selectedFiles[idx].path))
                                        Container(
                                          decoration: BoxDecoration(
                                            color: Colors.black.withOpacity(0.5),
                                            borderRadius: BorderRadius.circular(16),
                                          ),
                                          child: const Center(
                                            child: SizedBox(
                                              width: 16,
                                              height: 16,
                                              child: CircularProgressIndicator(
                                                strokeWidth: 2,
                                                valueColor: AlwaysStoppedAnimation<Color>(Colors.white70),
                                              ),
                                            ),
                                          ),
                                        ),
                                      // Close button
                                      Positioned(
                                        top: 4,
                                        right: 4,
                                        child: GestureDetector(
                                          onTap: () {
                                            provider.clearSelectedFile(idx);
                                          },
                                          child: Container(
                                            width: 16,
                                            height: 16,
                                            decoration: BoxDecoration(
                                              color: Colors.white,
                                              borderRadius: BorderRadius.circular(10),
                                            ),
                                            child: const Icon(
                                              FontAwesomeIcons.xmark,
                                              size: 10,
                                              color: Colors.black,
                                            ),
                                          ),
                                        ),
                                      ),
                                    ],
                                  ),
                                );
                              },
                            ),
                          );
                        } else {
                          return const SizedBox.shrink();
                        }
                      }),
                      // Send bar
                      SafeArea(
                        bottom: false,
                        maintainBottomViewPadding: false,
                        child: Padding(
                          padding: EdgeInsets.only(
                            left: 8,
                            right: 8,
                            top: provider.selectedFiles.isNotEmpty ? 0 : 8,
                            bottom: widget.isPivotBottom
                                ? (textFieldFocusNode.hasFocus
                                    ? 6
                                    : EllaSizes.navBarHeight + MediaQuery.of(context).padding.bottom + 8)
                                : (textFieldFocusNode.hasFocus &&
                                        (textController.text.length > 40 || textController.text.contains('\n'))
                                    ? 0
                                    : 2),
                          ),
                          child: Container(
                            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                            decoration: BoxDecoration(
                              color: EllaColors.bgSecondary,
                              borderRadius: BorderRadius.circular(32),
                            ),
                            child: Row(
                              crossAxisAlignment: CrossAxisAlignment.end,
                              children: [
                                // Plus button
                                if (shouldShowMenuButton())
                                  GestureDetector(
                                    onTap: () {
                                      HapticFeedback.lightImpact();
                                      FocusScope.of(context).unfocus();
                                      if (provider.selectedFiles.length > 3) {
                                        ScaffoldMessenger.of(context).showSnackBar(
                                          SnackBar(
                                            content: Text(context.l10n.maxFilesLimit),
                                            duration: const Duration(seconds: 2),
                                          ),
                                        );
                                        return;
                                      }
                                      _showIOSStyleActionSheet(context);
                                    },
                                    child: Container(
                                      height: 44,
                                      width: 44,
                                      decoration: BoxDecoration(
                                        color: EllaColors.bgTertiary,
                                        shape: BoxShape.circle,
                                      ),
                                      child: Center(
                                        child: FaIcon(
                                          FontAwesomeIcons.plus,
                                          color: provider.selectedFiles.length > 3
                                              ? EllaColors.textDisabled
                                              : EllaColors.textPrimary,
                                          size: 18,
                                        ),
                                      ),
                                    ),
                                  ),
                                const SizedBox(width: 12),
                                // Text field
                                Expanded(
                                  child: Column(
                                    mainAxisSize: MainAxisSize.min,
                                    crossAxisAlignment: CrossAxisAlignment.start,
                                    children: [
                                      if (_selectedContext != null && !voiceRecorderProvider.isActive)
                                        Padding(
                                          padding: const EdgeInsets.only(bottom: 4, top: 4, left: 2),
                                          child: Container(
                                            decoration: BoxDecoration(
                                              color: EllaColors.bgTertiary,
                                              borderRadius: BorderRadius.circular(16),
                                            ),
                                            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                                            child: Row(
                                              mainAxisSize: MainAxisSize.min,
                                              children: [
                                                const Padding(
                                                  padding: EdgeInsets.only(top: 1),
                                                  child: Icon(Icons.subdirectory_arrow_right,
                                                      size: 14, color: Colors.blue),
                                                ),
                                                const SizedBox(width: 8),
                                                Flexible(
                                                  child: Text(
                                                    _selectedContext!.length > 25
                                                        ? '${_selectedContext!.substring(0, 25)}...'
                                                        : _selectedContext!,
                                                    style: const TextStyle(
                                                      color: Colors.blue,
                                                      fontSize: 14,
                                                      fontWeight: FontWeight.w500,
                                                    ),
                                                    maxLines: 1,
                                                    overflow: TextOverflow.ellipsis,
                                                  ),
                                                ),
                                                const SizedBox(width: 8),
                                                GestureDetector(
                                                  onTap: () {
                                                    setState(() {
                                                      _selectedContext = null;
                                                    });
                                                  },
                                                  child: const Icon(Icons.close, size: 14, color: Colors.blue),
                                                ),
                                              ],
                                            ),
                                          ),
                                        ),
                                      voiceRecorderProvider.isActive
                                          ? VoiceRecorderWidget(
                                              onTranscriptReady: (transcript) {
                                                textController.text = transcript;
                                                voiceRecorderProvider.close();
                                                context.read<MessageProvider>().setNextMessageOriginIsVoice(true);
                                              },
                                              onClose: () {
                                                voiceRecorderProvider.close();
                                              },
                                            )
                                          : Theme(
                                              data: Theme.of(context).copyWith(
                                                textSelectionTheme: TextSelectionThemeData(
                                                  selectionColor: EllaColors.primaryLight.withOpacity(0.4),
                                                  selectionHandleColor: EllaColors.primary,
                                                ),
                                              ),
                                              child: TextField(
                                                enabled: true,
                                                controller: textController,
                                                focusNode: textFieldFocusNode,
                                                obscureText: false,
                                                textAlign: TextAlign.start,
                                                textAlignVertical: TextAlignVertical.center,
                                                decoration: InputDecoration(
                                                  hintText: context.l10n.askAnything,
                                                  hintStyle:
                                                      const TextStyle(fontSize: 16.0, color: EllaColors.textDisabled),
                                                  focusedBorder: InputBorder.none,
                                                  enabledBorder: InputBorder.none,
                                                  contentPadding:
                                                      const EdgeInsets.symmetric(horizontal: 4, vertical: 12),
                                                  isDense: true,
                                                ),
                                                minLines: 1,
                                                maxLines: 10,
                                                keyboardType: TextInputType.multiline,
                                                textCapitalization: TextCapitalization.sentences,
                                                style: const TextStyle(
                                                    fontSize: 16.0, color: EllaColors.textPrimary, height: 1.4),
                                              ),
                                            ),
                                    ],
                                  ),
                                ),
                                // Microphone button
                                if (shouldShowVoiceRecorderButton() && textController.text.isEmpty)
                                  GestureDetector(
                                    onTap: () {
                                      HapticFeedback.lightImpact();
                                      FocusScope.of(context).unfocus();
                                      voiceRecorderProvider.startRecording();
                                    },
                                    child: Container(
                                      height: 44,
                                      width: 44,
                                      alignment: Alignment.center,
                                      child: const FaIcon(
                                        FontAwesomeIcons.microphone,
                                        color: EllaColors.textTertiary,
                                        size: 20,
                                      ),
                                    ),
                                  ),
                                // Send button - only show when there's text
                                if (shouldShowSendButton(provider))
                                  ValueListenableBuilder<TextEditingValue>(
                                    valueListenable: textController,
                                    builder: (context, value, child) {
                                      bool hasText = value.text.trim().isNotEmpty;
                                      if (!hasText) return const SizedBox.shrink();

                                      bool canSend = hasText &&
                                          !provider.sendingMessage &&
                                          !provider.isUploadingFiles &&
                                          connectivityProvider.isConnected;

                                      return GestureDetector(
                                        onTap: canSend
                                            ? () {
                                                HapticFeedback.mediumImpact();
                                                String message = textController.text.trim();
                                                if (message.isEmpty) return;
                                                _sendMessageUtil(message);
                                              }
                                            : null,
                                        child: Container(
                                          height: 44,
                                          width: 44,
                                          decoration: const BoxDecoration(
                                            color: Colors.white,
                                            shape: BoxShape.circle,
                                          ),
                                          child: const Center(
                                            child: FaIcon(
                                              FontAwesomeIcons.arrowUp,
                                              color: EllaColors.textPrimary,
                                              size: 18,
                                            ),
                                          ),
                                        ),
                                      );
                                    },
                                  ),
                              ],
                            ),
                          ),
                        ),
                      )
                    ]);
                  }),
                ),
                if (!widget.isPivotBottom) SizedBox(height: textFieldFocusNode.hasFocus ? 12 : 0),
                if (!widget.isPivotBottom && !textFieldFocusNode.hasFocus)
                  BottomNavBar(
                    onTabTap: (index, isRepeat) {
                      context.read<HomeProvider>().setIndex(index);
                      Navigator.of(context).pop();
                    },
                  ),
              ],
            ),
          ),
        );
      },
    );
  }

  _sendMessageUtil(String text) {
    String? currentContext = _selectedContext;
    setState(() {
      _selectedContext = null;
    });

    // Remove focus from text field
    FocusManager.instance.primaryFocus?.unfocus();

    if (currentContext != null) {
      text = 'Context: "$currentContext"\n\n$text';
    }

    var provider = context.read<MessageProvider>();
    provider.setSendingMessage(true);
    provider.addMessageLocally(text);
    textController.clear();

    Future.delayed(const Duration(milliseconds: 300), () {
      if (mounted) scrollToBottomOnSend();
    });

    provider.sendMessageStreamToServer(text);
    provider.clearSelectedFiles();
    provider.setSendingMessage(false);
  }

  sendInitialAppMessage(App? app) async {
    context.read<MessageProvider>().setSendingMessage(true);
    scrollToBottom();
    ServerMessage message = await getInitialAppMessage(app?.id);
    if (mounted) {
      context.read<MessageProvider>().addMessage(message);
      scrollToBottom();
      context.read<MessageProvider>().setSendingMessage(false);
    }
  }

  void scrollToBottomOnSend() {
    if (!scrollController.hasClients) return;

    // With reverse: true, offset 0 is the bottom (newest messages)
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!scrollController.hasClients) return;

      final currentPosition = scrollController.position.pixels;

      if (currentPosition > 300) {
        scrollController.jumpTo(0);
      } else if (currentPosition > 10) {
        scrollController.animateTo(
          0,
          duration: const Duration(milliseconds: 300),
          curve: Curves.easeOutCubic,
        );
      }
    });
  }

  void _ensureAtBottom({int delayMs = 50}) {
    Future.delayed(Duration(milliseconds: delayMs), () {
      if (!scrollController.hasClients) return;

      // With reverse: true, bottom is offset 0
      if (scrollController.position.pixels > 20) {
        scrollController.animateTo(
          0,
          duration: const Duration(milliseconds: 150),
          curve: Curves.easeOut,
        );
      }
    });
  }

  void scrollToBottom({bool animated = false}) {
    if (!scrollController.hasClients) return;

    // With reverse: true, bottom is offset 0
    if (animated) {
      scrollController.animateTo(
        0,
        duration: const Duration(milliseconds: 220),
        curve: Curves.easeOut,
      );
    } else {
      scrollController.jumpTo(0);
    }
  }

  void _handleAppSelection(String? val, AppProvider provider) {
    if (val == null || val == provider.selectedChatAppId) {
      return;
    }

    // Unfocus the text field to prevent keyboard issues
    textFieldFocusNode.unfocus();

    // clear chat
    if (val == 'clear_chat') {
      _showClearChatDialog();
      return;
    }

    // enable apps - navigate to chat capability apps page
    if (val == 'enable') {
      _navigateToChatAppsPage();
      return;
    }

    // select app by id
    _selectApp(val, provider);
  }

  void _showClearChatDialog() {
    if (!mounted) return;

    showDialog(
      context: context,
      builder: (ctx) {
        return getDialog(context, () {
          Navigator.of(context).pop();
        }, () {
          if (mounted) {
            context.read<MessageProvider>().clearChat();
            Navigator.of(context).pop();
          }
        }, context.l10n.clearChatQuestion, context.l10n.clearChatConfirm);
      },
    );
  }

  Future<void> _navigateToChatAppsPage() async {
    if (!mounted) return;

    MixpanelManager().pageOpened('Chat Apps');
    // Navigate to chat capability apps page
    await routeToPage(
      context,
      CapabilityAppsPage(
        capability: AppCapability(id: 'chat', title: context.l10n.chatAssistantsTitle),
        apps: const [],
      ),
    );

    // Refresh chat apps when returning from the page
    if (mounted) {
      _refreshChatAppsFromLocal();
    }
  }

  void _refreshChatAppsFromLocal() {
    // Get enabled chat apps from local AppProvider immediately
    final appProvider = context.read<AppProvider>();
    final messageProvider = context.read<MessageProvider>();

    // Filter apps that are enabled and work with chat
    final localChatApps = appProvider.apps.where((app) => app.enabled && app.worksWithChat()).toList();

    // Update immediately with local data
    messageProvider.setChatApps(localChatApps);
  }

  Future<void> _handleAppUninstall(String appId, AppProvider appProvider, MessageProvider messageProvider) async {
    if (!mounted) return;

    // Immediately remove from local chat apps list for instant visual feedback
    messageProvider.removeChatApp(appId);

    // Disable the app on server (runs in background)
    appProvider.toggleApp(appId, false, null);
  }

  void _selectApp(String appId, AppProvider appProvider) async {
    if (!mounted) return;

    // Mark that we're no longer on initial load to prevent auto-focus
    _isInitialLoad = false;

    // Store references before async operation
    final messageProvider = mounted ? context.read<MessageProvider>() : null;
    if (messageProvider == null) return;

    // Set the selected app
    appProvider.setSelectedChatAppId(appId);

    // Add a small delay to let the keyboard animation complete
    // This prevents the widget from being unmounted during the keyboard transition
    await Future.delayed(const Duration(milliseconds: 100));

    // Check if widget is still mounted after delay
    if (!mounted) return;

    // Perform async operation
    await messageProvider.refreshMessages(dropdownSelected: true);

    // Check if widget is still mounted before proceeding
    if (!mounted) return;

    // Get the selected app and send initial message if needed
    var app = appProvider.getSelectedApp();
    if (messageProvider.messages.isEmpty) {
      messageProvider.sendInitialAppMessage(app);
    }
  }

  PreferredSizeWidget _buildAppBar(BuildContext context, MessageProvider provider) {
    return AppBar(
      backgroundColor: EllaColors.bgPrimary,
      elevation: 0,
      leading: widget.isPivotBottom
          ? const SizedBox(width: 48)
          : Container(
              width: 36,
              height: 36,
              margin: const EdgeInsets.all(8),
              decoration: const BoxDecoration(
                color: EllaColors.bgTertiary,
                shape: BoxShape.circle,
              ),
              child: IconButton(
                padding: EdgeInsets.zero,
                icon: const Icon(Icons.arrow_back_ios_new, color: EllaColors.textPrimary, size: 18),
                onPressed: () {
                  HapticFeedback.mediumImpact();
                  Navigator.of(context).pop();
                },
              ),
            ),
      title: Consumer<AppProvider>(
        builder: (context, appProvider, child) {
          return _buildSelectedAppDisplay(context, appProvider);
        },
      ),
      centerTitle: true,
      actions: const [], // Chat apps panel hidden — single-app Ella setup
      bottom: provider.isLoadingMessages
          ? PreferredSize(
              preferredSize: const Size.fromHeight(32),
              child: Container(
                width: double.infinity,
                height: 32,
                color: Colors.green,
                child: Center(
                  child: Text(
                    context.l10n.syncingMessages,
                    style: const TextStyle(color: Colors.white, fontSize: 12),
                  ),
                ),
              ),
            )
          : null,
    );
  }

  Widget _buildSelectedAppDisplay(BuildContext context, AppProvider provider) {
    final messageProvider = Provider.of<MessageProvider>(context, listen: false);
    var selectedApp = messageProvider.chatApps.firstWhereOrNull((app) => app.id == provider.selectedChatAppId);

    return Row(
      crossAxisAlignment: CrossAxisAlignment.center,
      mainAxisAlignment: MainAxisAlignment.center,
      mainAxisSize: MainAxisSize.min,
      children: [
        selectedApp != null ? _getAppAvatar(selectedApp) : _getEllaAvatar(),
        const SizedBox(width: 8),
        Container(
          constraints: const BoxConstraints(maxWidth: 140),
          child: Text(
            selectedApp != null ? selectedApp.getName() : 'Ella',
            style: const TextStyle(color: EllaColors.textPrimary, fontSize: 16),
            overflow: TextOverflow.ellipsis,
          ),
        ),
      ],
    );
  }

  Widget _buildChatAppsEndDrawer(BuildContext context) {
    return Drawer(
      backgroundColor: EllaColors.bgSecondary,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.only(
          topLeft: Radius.circular(20),
          bottomLeft: Radius.circular(20),
        ),
      ),
      child: SafeArea(
        child: Consumer2<MessageProvider, AppProvider>(
          builder: (context, messageProvider, appProvider, child) {
            final chatApps = messageProvider.chatApps;
            final selectedAppId = appProvider.selectedChatAppId;
            final isOmiSelected = chatApps.firstWhereOrNull((a) => a.id == selectedAppId) == null;

            return Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                // Header
                Padding(
                  padding: const EdgeInsets.fromLTRB(20, 16, 16, 8),
                  child: Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      Text(
                        context.l10n.chatAppsTitle,
                        style: const TextStyle(
                          color: EllaColors.textPrimary,
                          fontSize: 20,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      IconButton(
                        icon: const Padding(
                          padding: EdgeInsets.only(left: 2, top: 1),
                          child: FaIcon(FontAwesomeIcons.xmark, color: EllaColors.textTertiary, size: 18),
                        ),
                        onPressed: () => Navigator.of(context).pop(),
                      ),
                    ],
                  ),
                ),
                const Divider(color: EllaColors.bgTertiary, height: 1),
                // Actions
                ListTile(
                  leading: const Padding(
                    padding: EdgeInsets.only(left: 2, top: 1),
                    child: FaIcon(FontAwesomeIcons.solidTrashCan, color: Colors.redAccent, size: 20),
                  ),
                  title: Text(
                    context.l10n.clearChat,
                    style: const TextStyle(color: Colors.redAccent, fontSize: 16),
                  ),
                  onTap: () {
                    Navigator.of(context).pop();
                    _handleAppSelection('clear_chat', appProvider);
                  },
                ),
                ListTile(
                  leading: const Padding(
                    padding: EdgeInsets.only(left: 2, top: 1),
                    child: FaIcon(FontAwesomeIcons.circlePlus, color: EllaColors.textPrimary, size: 20),
                  ),
                  title: Text(
                    context.l10n.enableApps,
                    style: const TextStyle(color: EllaColors.textPrimary, fontSize: 16),
                  ),
                  trailing: const Padding(
                    padding: EdgeInsets.only(left: 2, top: 1),
                    child: FaIcon(FontAwesomeIcons.chevronRight, color: EllaColors.textDisabled, size: 14),
                  ),
                  onTap: () {
                    Navigator.of(context).pop();
                    _navigateToChatAppsPage();
                  },
                ),
                const Divider(color: EllaColors.bgTertiary, height: 1),
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 16, 20, 8),
                  child: Text(
                    context.l10n.selectApp,
                    style: const TextStyle(
                      color: EllaColors.textTertiary,
                      fontSize: 13,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                ),
                // App list
                Expanded(
                  child: ListView(
                    padding: EdgeInsets.zero,
                    children: [
                      // Ella default option
                      _buildDrawerAppItem(
                        avatar: _getEllaAvatar(),
                        name: 'Ella',
                        isSelected: isOmiSelected,
                        onTap: () {
                          Navigator.of(context).pop();
                          _handleAppSelection('no_selected', appProvider);
                        },
                      ),
                      // Enabled chat apps
                      ...chatApps.map((app) => _buildDrawerAppItem(
                            avatar: _getAppAvatar(app),
                            name: app.getName(),
                            isSelected: selectedAppId == app.id,
                            appId: app.id,
                            onTap: () {
                              Navigator.of(context).pop();
                              _handleAppSelection(app.id, appProvider);
                            },
                            onConfirmDelete: selectedAppId != app.id
                                ? () => _handleAppUninstall(app.id, appProvider, messageProvider)
                                : null,
                          )),
                      if (chatApps.isEmpty)
                        Padding(
                          padding: const EdgeInsets.all(20),
                          child: Text(
                            context.l10n.noChatAppsEnabled,
                            style: const TextStyle(color: EllaColors.textDisabled, fontSize: 14),
                            textAlign: TextAlign.center,
                          ),
                        ),
                    ],
                  ),
                ),
              ],
            );
          },
        ),
      ),
    );
  }

  Widget _buildDrawerAppItem({
    required Widget avatar,
    required String name,
    required bool isSelected,
    required VoidCallback onTap,
    String? appId,
    VoidCallback? onConfirmDelete,
  }) {
    final bool isPendingDelete = appId != null && _pendingDeleteAppId == appId;

    if (isPendingDelete) {
      // Show inline confirmation buttons - match ListTile height (56px)
      return Container(
        height: 56,
        padding: const EdgeInsets.symmetric(horizontal: 16),
        child: Row(
          children: [
            avatar,
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                name,
                style: const TextStyle(color: EllaColors.textPrimary, fontSize: 16),
                overflow: TextOverflow.ellipsis,
              ),
            ),
            const SizedBox(width: 8),
            // Cancel button (white)
            GestureDetector(
              onTap: () {
                setState(() {
                  _pendingDeleteAppId = null;
                });
              },
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                decoration: BoxDecoration(
                  color: Colors.white,
                  borderRadius: BorderRadius.circular(16),
                ),
                child: Text(
                  context.l10n.cancel,
                  style: const TextStyle(
                    color: Colors.black,
                    fontSize: 13,
                    fontWeight: FontWeight.w500,
                  ),
                ),
              ),
            ),
            const SizedBox(width: 8),
            // Disable button (red)
            GestureDetector(
              onTap: () {
                setState(() {
                  _pendingDeleteAppId = null;
                });
                onConfirmDelete?.call();
              },
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                decoration: BoxDecoration(
                  color: Colors.redAccent,
                  borderRadius: BorderRadius.circular(16),
                ),
                child: Text(
                  context.l10n.disable,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 13,
                    fontWeight: FontWeight.w500,
                  ),
                ),
              ),
            ),
          ],
        ),
      );
    }

    return ListTile(
      leading: avatar,
      title: Text(
        name,
        style: const TextStyle(color: EllaColors.textPrimary, fontSize: 16),
        overflow: TextOverflow.ellipsis,
      ),
      trailing: isSelected
          ? const Padding(
              padding: EdgeInsets.only(left: 2, top: 1),
              child: FaIcon(FontAwesomeIcons.solidCircleCheck, color: EllaColors.primary, size: 18),
            )
          : appId != null && onConfirmDelete != null
              ? GestureDetector(
                  onTap: () {
                    setState(() {
                      _pendingDeleteAppId = appId;
                    });
                  },
                  child: const Padding(
                    padding: EdgeInsets.only(left: 2, top: 1),
                    child: FaIcon(FontAwesomeIcons.solidTrashCan, color: EllaColors.textDisabled, size: 16),
                  ),
                )
              : null,
      selected: isSelected,
      selectedTileColor: EllaColors.bgTertiary.withOpacity(0.5),
      onTap: onTap,
    );
  }

  Widget _getAppAvatar(App app) {
    return CachedNetworkImage(
      imageUrl: app.getImageUrl(),
      imageBuilder: (context, imageProvider) {
        return CircleAvatar(
          backgroundColor: Colors.white,
          radius: 12,
          backgroundImage: imageProvider,
        );
      },
      errorWidget: (context, url, error) {
        return const CircleAvatar(
          backgroundColor: Colors.white,
          radius: 12,
          child: Icon(Icons.error_outline_rounded),
        );
      },
      progressIndicatorBuilder: (context, url, progress) => CircleAvatar(
        backgroundColor: Colors.white,
        radius: 12,
        child: CircularProgressIndicator(
          value: progress.progress,
          valueColor: const AlwaysStoppedAnimation<Color>(EllaColors.primary),
        ),
      ),
    );
  }

  Widget _getOmiAvatar() {
    return Container(
      height: 24,
      width: 24,
      decoration: const BoxDecoration(
        color: EllaColors.primary,
        borderRadius: BorderRadius.all(Radius.circular(12.0)),
      ),
      child: const Icon(
        Icons.favorite,
        color: Colors.white,
        size: 14,
      ),
    );
  }

  Widget _getEllaAvatar() {
    return Container(
      height: 24,
      width: 24,
      decoration: const BoxDecoration(
        color: EllaColors.primary,
        borderRadius: BorderRadius.all(Radius.circular(12.0)),
      ),
      child: const Icon(
        Icons.favorite,
        color: Colors.white,
        size: 14,
      ),
    );
  }

  void _showIOSStyleActionSheet(BuildContext context) {
    showModalBottomSheet(
      context: context,
      backgroundColor: Colors.transparent,
      isScrollControlled: true,
      builder: (BuildContext context) {
        return Container(
          margin: const EdgeInsets.all(10),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              // Main options container
              Container(
                decoration: BoxDecoration(
                  color: Colors.white.withOpacity(0.95),
                  borderRadius: BorderRadius.circular(13),
                ),
                child: Column(
                  children: [
                    _buildIOSActionItem(
                      title: context.l10n.takePhoto,
                      icon: Icons.camera_alt,
                      onTap: () {
                        HapticFeedback.selectionClick();
                        Navigator.pop(context);
                        if (mounted) {
                          this.context.read<MessageProvider>().captureImage();
                        }
                      },
                      isFirst: true,
                    ),
                    _buildDivider(),
                    _buildIOSActionItem(
                      title: context.l10n.photoLibrary,
                      icon: Icons.photo_library,
                      onTap: () {
                        HapticFeedback.selectionClick();
                        Navigator.pop(context);
                        if (mounted) {
                          this.context.read<MessageProvider>().selectImage();
                        }
                      },
                    ),
                    _buildDivider(),
                    _buildIOSActionItem(
                      title: context.l10n.chooseFile,
                      icon: Icons.folder,
                      onTap: () {
                        HapticFeedback.selectionClick();
                        Navigator.pop(context);
                        if (mounted) {
                          this.context.read<MessageProvider>().selectFile();
                        }
                      },
                      isLast: true,
                    ),
                  ],
                ),
              ),
              SizedBox(height: MediaQuery.of(context).padding.bottom + 10),
            ],
          ),
        );
      },
    );
  }

  Widget _buildIOSActionItem({
    required String title,
    required VoidCallback onTap,
    IconData? icon,
    bool isFirst = false,
    bool isLast = false,
    bool isCancel = false,
  }) {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.vertical(
          top: isFirst ? const Radius.circular(13) : Radius.zero,
          bottom: isLast ? const Radius.circular(13) : Radius.zero,
        ),
        child: Container(
          width: double.infinity,
          padding: const EdgeInsets.symmetric(vertical: 16, horizontal: 20),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  title,
                  style: TextStyle(
                    color: isCancel ? Colors.red : Colors.blue,
                    fontSize: 20,
                    fontWeight: isCancel ? FontWeight.w600 : FontWeight.w400,
                  ),
                  textAlign: TextAlign.center,
                ),
              ),
              if (icon != null && !isCancel)
                Icon(
                  icon,
                  color: EllaColors.textTertiary,
                  size: 24,
                ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildDivider() {
    return Container(
      height: 0.5,
      color: EllaColors.bgTertiary,
      margin: const EdgeInsets.symmetric(horizontal: 20),
    );
  }
}
