import 'package:omi/ella/voice/foreground_dictation_diagnostics.dart';
import 'package:omi/ella/voice/foreground_dictation_host.dart';
import 'package:omi/ella/voice/ephemeral_dictation_session.dart';
import 'package:omi/ella/widgets/foreground_dictation_sheet.dart';
import 'package:omi/ella/widgets/iris_capture_controls.dart';
import 'package:omi/ella/widgets/ella_v7_surfaces.dart';
import 'package:omi/utils/enums.dart' show RecordingState;
import 'dart:async';
import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter/material.dart';
import 'package:omi/ella/widgets/iris_whisper_stop_bar.dart';
import 'package:flutter/scheduler.dart';
import 'package:flutter/services.dart';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:collection/collection.dart';
import 'package:font_awesome_flutter/font_awesome_flutter.dart';
import 'package:provider/provider.dart';
import 'package:uuid/uuid.dart';

import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/http/api/messages.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/app.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/services/ella_public_surface_policy.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
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
import 'package:omi/ella/widgets/ella_companion_avatar.dart';
import 'package:omi/ella/capture_host/ella_capture_host.dart';

class ChatPage extends StatefulWidget {
  final bool isPivotBottom;
  final String? autoMessage;

  /// UI provenance only. Does not create or select a backend conversation.
  final String? draftConversationId;
  @visibleForTesting
  final String Function()? draftAuthenticatedUid;
  @visibleForTesting
  final String Function()? dictationAuthenticatedUid;

  const ChatPage({
    super.key,
    this.isPivotBottom = false,
    this.autoMessage,
    this.draftConversationId,
    this.draftAuthenticatedUid,
    this.dictationAuthenticatedUid,
  });

  @override
  State<ChatPage> createState() => ChatPageState();
}

class ChatPageState extends State<ChatPage> with AutomaticKeepAliveClientMixin, WidgetsBindingObserver {
  TextEditingController textController = TextEditingController();
  late ScrollController scrollController;
  late FocusNode textFieldFocusNode;

  var prefs = SharedPreferencesUtil();
  late List<App> apps;

  final scaffoldKey = GlobalKey<ScaffoldState>();

  // Track which app is pending deletion confirmation
  String? _pendingDeleteAppId;
  String? _selectedContext;

  StreamSubscription<User?>? _draftAuthSubscription;
  EllaAccountCommitLease? _draftLease;
  (String, String, int, String, String?, int)? _draftBoundary;
  int _draftEpoch = 0;
  int _draftRevision = 0;
  int _dictationModalGeneration = 0;
  ForegroundDictationHost? _dictationHost;
  bool _dictationForeground = true;
  Timer? _dictationAuthorityExpiry;
  void _retireDictationForAuthorityChange() {
    _dictationHost?.retire().catchError((Object _) {});
  }

  int? _recorderDraftEpoch;
  bool _suppressDraftListener = false;
  bool _disposing = false;

  String get _draftUid => widget.draftAuthenticatedUid?.call() ?? WalOwnerAuthority.authenticatedUid;

  void _discardDraft({bool rebuild = false}) {
    _dictationHost?.retire().catchError((Object _) {});
    _draftEpoch++;
    _draftLease?.close();
    _draftLease = null;
    _suppressDraftListener = true;
    textController.clear();
    _selectedContext = null;
    _suppressDraftListener = false;
    if (rebuild && mounted && !_disposing) setState(() {});
  }

  // Editor lifetime checks never authorize network, capture or consent operations.
  void _syncDraftBoundary() {
    if (!mounted || _disposing) return;
    final boundary = (
      _draftUid,
      prefs.uid,
      prefs.terminalAccountConsentAuthorityGeneration,
      context.read<AppProvider>().selectedChatAppId,
      widget.draftConversationId,
      context.read<MessageProvider>().localDraftResetGeneration,
    );
    if (_draftBoundary != boundary || (_draftLease != null && !_draftLease!.isCurrent)) {
      _discardDraft();
      _draftBoundary = boundary;
    }
    if (_draftLease == null && boundary.$1.isNotEmpty && boundary.$1 == boundary.$2) {
      final authority = _LocalDraftLifetimeAuthority(boundary.$1, boundary.$3, () => _draftUid, prefs);
      _draftLease = EllaAccountCommitBarrier.begin(
        authorityProvider: () => authority,
        onInvalidated: () => _discardDraft(rebuild: true),
      );
    }
  }

  bool _draftCallbackIsCurrent(int epoch) {
    _syncDraftBoundary();
    return mounted && !_disposing && epoch == _draftEpoch && _draftLease?.isCurrent == true;
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _dictationForeground = state == AppLifecycleState.resumed;
    if (!_dictationForeground) {
      textFieldFocusNode.unfocus();
      _dictationHost?.retire().catchError((Object _) {});
    }
  }

  String get _dictationUid => widget.dictationAuthenticatedUid?.call() ?? WalOwnerAuthority.authenticatedUid;

  Future<void> _openDictation() async {
    _syncDraftBoundary();
    final retained = _dictationHost;
    if (retained != null) {
      await showDialog<void>(
        context: context,
        builder: (_) => ForegroundDictationSheet(host: retained),
      );
      if (!retained.hasHeldInput && !retained.hasHeldOutput) {
        retained.dispose();
        _dictationHost = null;
      }
      return;
    }
    if (_draftLease?.isCurrent != true) return;
    final authority = WalOwnerAuthority.active(preferences: prefs, authenticatedUid: _dictationUid);
    final modalGeneration = ++_dictationModalGeneration;
    ModalRoute<dynamic>? dialogRoute;
    bool exactAuthority() {
      if (!mounted ||
          _disposing ||
          !_dictationForeground ||
          modalGeneration != _dictationModalGeneration ||
          dialogRoute?.isCurrent != true ||
          context.read<HomeProvider>().selectedIndex != 1) return false;
      final fresh = WalOwnerAuthority.active(preferences: prefs, authenticatedUid: _dictationUid);
      return authority != null &&
          _dictationUid == _draftUid &&
          authority.isCurrent(preferences: prefs, authenticatedUid: _dictationUid) &&
          fresh != null &&
          authority.hasEquivalentCaptureFence(fresh) &&
          prefs.aiConsentServerVerificationRemaining != null &&
          prefs.aiConsentServerVerificationRemaining! > Duration.zero;
    }

    final diagnosticUid = _dictationUid;
    final diagnosticAccountGeneration = prefs.terminalAccountConsentAuthorityGeneration;
    final diagnosticProvisioningGeneration = prefs.ellaProvisioningTerminalAuthorityGeneration;
    final diagnostic = ForegroundDictationDiagnostics.shared.begin(
      contextCurrent: () =>
          _dictationUid == diagnosticUid &&
          prefs.uid == diagnosticUid &&
          prefs.terminalAccountConsentAuthorityGeneration == diagnosticAccountGeneration &&
          prefs.ellaProvisioningTerminalAuthorityGeneration == diagnosticProvisioningGeneration,
      verificationRemainingMs: prefs.aiConsentServerVerificationRemaining?.inMilliseconds,
      authorityPresent: authority != null,
    );
    bool freshCapture() {
      final pageCurrent = mounted && !_disposing;
      if (!pageCurrent) {
        diagnostic.captureAdmission(pageCurrent: false, observationStage: 'page');
        return false;
      }
      var observationStage = 'capture_lookup';
      try {
        final capture = context.read<CaptureProvider>();
        observationStage = 'companion';
        final companion = EllaCaptureHost.companionRecordingActive;
        observationStage = 'quiescent';
        final quiescent = capture.isCaptureQuiescentForDiagnostic;
        observationStage = 'unfinalized';
        final unfinalized = capture.hasUnfinalizedPhoneCaptureContent;
        observationStage = 'content';
        final content = capture.hasCapturableContent;
        observationStage = 'device';
        final device = capture.havingRecordingDevice;
        observationStage = 'voice_lookup';
        final recorder = context.read<VoiceRecorderProvider>().isActive;
        observationStage = 'pending_states';
        final pendingStates = capture.dictationAdmissionPendingStates;
        diagnostic.captureAdmission(
          observationStage: 'complete',
          pageCurrent: true,
          companionActive: companion,
          captureQuiescent: quiescent,
          unfinalizedPhoneContent: unfinalized,
          capturableContent: content,
          recordingDevice: device,
          voiceRecorderActive: recorder,
          observationFailed: false,
          pendingStates: pendingStates,
        );
        return !companion && quiescent && !unfinalized && !content && !device && !recorder;
      } catch (_) {
        diagnostic.captureAdmission(pageCurrent: true, observationFailed: true, observationStage: observationStage);
        return false;
      }
    }

    final host = ForegroundDictationHost(
      diagnostic: diagnostic,
      registration: ForegroundDictationRegistration.current,
      readEditor: () => DictationEditorBinding(
        value: textController.value,
        lease: _draftLease,
        epoch: _draftEpoch,
        revision: _draftRevision,
      ),
      hasExactAuthority: exactAuthority,
      canStartFresh: freshCapture,
      applyDraft: (value) {
        if (exactAuthority()) textController.value = value;
      },
    );
    _dictationHost = host;
    final remaining = prefs.aiConsentServerVerificationRemaining;
    if (remaining != null && remaining > Duration.zero) {
      _dictationAuthorityExpiry?.cancel();
      _dictationAuthorityExpiry = Timer(remaining, () {
        unawaited(host.retire().catchError((Object _) {}));
      });
    }
    try {
      await showDialog<void>(
        context: context,
        builder: (ctx) {
          dialogRoute = ModalRoute.of(ctx);
          if (ModalRoute.isCurrentOf(ctx) != true) host.retire().catchError((Object _) {});
          return ForegroundDictationSheet(host: host);
        },
      );
    } finally {
      _dictationAuthorityExpiry?.cancel();
      _dictationAuthorityExpiry = null;
      ++_dictationModalGeneration;
      unawaited(host.retire().catchError((Object _) {}));
      if (!host.hasHeldInput && !host.hasHeldOutput) {
        host.dispose();
        if (identical(_dictationHost, host)) _dictationHost = null;
      }
    }
  }

  void _scheduleDraftCaretReveal() {
    final epoch = _draftEpoch;
    final value = textController.value;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted ||
          _disposing ||
          !textFieldFocusNode.hasFocus ||
          !value.selection.isValid ||
          value.selection.start < 0 ||
          value.selection.end > value.text.length ||
          !_draftCallbackIsCurrent(epoch) ||
          textController.value != value) {
        return;
      }
      final editor = textFieldFocusNode.context?.findAncestorStateOfType<EditableTextState>();
      if (editor == null ||
          !editor.mounted ||
          !identical(editor.widget.controller, textController) ||
          !identical(editor.widget.focusNode, textFieldFocusNode)) {
        return;
      }
      // Reveal the existing active selection; never rewrite text, composing or selection.
      editor.bringIntoView(value.selection.extent);
    });
  }

  @override
  void didUpdateWidget(ChatPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    _syncDraftBoundary();
  }

  @override
  bool get wantKeepAlive => true;

  @override
  void initState() {
    _dictationForeground = WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed;
    WidgetsBinding.instance.addObserver(this);
    SharedPreferencesUtil.aiConsentAuthorityChanges.addListener(_retireDictationForAuthorityChange);
    apps = prefs.appsList;
    scrollController = ScrollController();
    textFieldFocusNode = FocusNode();
    textController.addListener(() {
      _draftRevision++;
      _dictationHost?.retire().catchError((Object _) {});
      if (_suppressDraftListener || _disposing) return;
      _syncDraftBoundary();
      if (mounted) {
        setState(() {});
        _scheduleDraftCaretReveal();
      }
    });
    try {
      _draftAuthSubscription = FirebaseAuth.instance.authStateChanges().listen((_) {
        _syncDraftBoundary();
        if (mounted && !_disposing) setState(() {});
      });
    } catch (_) {
      // Widget fixtures may have no Firebase app; identity checks remain enforced.
    }
    textFieldFocusNode.addListener(() {
      if (_disposing) return;
      _syncDraftBoundary();
      setState(() {});
      // Sync focus state to HomeProvider so BottomNavBar hides when typing
      if (widget.isPivotBottom && mounted) {
        context.read<HomeProvider>().setChatFieldFocused(textFieldFocusNode.hasFocus);
      }
      if (textFieldFocusNode.hasFocus) {
        _scheduleDraftCaretReveal();
        // Scroll to bottom when keyboard opens, with delay to allow keyboard animation
        _ensureAtBottom(delayMs: 300);
      }
    });

    SchedulerBinding.instance.addPostFrameCallback((_) async {
      if (!mounted) return;
      _syncDraftBoundary();
      final initialDraftEpoch = _draftEpoch;
      final initialAutoMessage = widget.autoMessage;
      var provider = context.read<MessageProvider>();
      if (provider.messages.isEmpty) {
        provider.refreshMessages();
      }
      // Fetch enabled chat apps
      if (allowsInheritedOmiSurface()) provider.fetchChatApps();
      // Sync Apple Health data if connected (ensures fresh data for health queries)
      _syncAppleHealthIfConnected();
      // Handle auto-message from notification (e.g., daily reflection or goal advice)
      // This sends a message FROM Omi AI, not from the user
      if (widget.autoMessage != null && widget.autoMessage!.isNotEmpty && mounted) {
        // Wait for messages to load first, then add auto-message
        Future.delayed(const Duration(milliseconds: 800), () {
          if (mounted && _draftCallbackIsCurrent(initialDraftEpoch)) {
            final aiMessage = ServerMessage(
              const Uuid().v4(),
              DateTime.now(),
              initialAutoMessage!,
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
              if (mounted && _draftCallbackIsCurrent(initialDraftEpoch)) {
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
    context.watch<AppProvider>();
    context.watch<MessageProvider>();
    _syncDraftBoundary();
    // Refresh messages when switching TO the Chat tab (picks up voice messages)
    try {
      final homeProvider = Provider.of<HomeProvider>(context);
      final isOnChatTab = homeProvider.selectedIndex == 1;
      if (!isOnChatTab) _dictationHost?.retire().catchError((Object _) {});
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
    _disposing = true;
    _dictationAuthorityExpiry?.cancel();
    _dictationAuthorityExpiry = null;
    WidgetsBinding.instance.removeObserver(this);
    SharedPreferencesUtil.aiConsentAuthorityChanges.removeListener(_retireDictationForAuthorityChange);
    _dictationHost?.retire().catchError((Object _) {});
    _draftEpoch++;
    _draftLease?.close();
    _draftAuthSubscription?.cancel();
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

  // ignore: unused_element
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
    _syncDraftBoundary();
    final draftEpoch = _draftEpoch;
    final keyboardVisible = MediaQuery.viewInsetsOf(context).bottom > 0;

    final chatVisible = !widget.isPivotBottom || context.watch<HomeProvider>().selectedIndex == 1;
    final page = ListenableBuilder(
      listenable: IrisCaptureControls.shared,
      builder: (context, _) => Consumer2<MessageProvider, ConnectivityProvider>(
        builder: (context, provider, connectivityProvider, child) {
          final chatMessages = provider.messages.where((message) => !message.isCanonicalOverheardTranscript).toList();
          final floatingHeader = widget.isPivotBottom &&
              EllaCaptureDockPlacementScope.active(context) &&
              EllaCaptureHost.homeCaptureDockBuilder != null;
          final headerClearance =
              floatingHeader ? MediaQuery.paddingOf(context).top + EllaV7ChatIdentity.extent(context) : 0.0;
          return Scaffold(
            key: scaffoldKey,
            primary: true,
            resizeToAvoidBottomInset: widget.isPivotBottom ? true : null,
            backgroundColor: Colors.transparent,
            appBar: floatingHeader ? null : _buildAppBar(context, provider),
            // endDrawer hidden — single-app Ella setup
            body: Stack(
              children: [
                GestureDetector(
                  onTap: () {
                    // Hide keyboard when tapping outside textfield
                    FocusScope.of(context).unfocus();
                  },
                  child: _ChatViewportLayout(
                    headerClearance: headerClearance,
                    historyNavigation: provider.historyPaginationEnabled &&
                            (chatMessages.isNotEmpty || provider.showsEarlierHistoryControl)
                        ? _historyNavigationToolbar(provider, hasMessages: chatMessages.isNotEmpty)
                        : null,
                    transcriptPreview: !keyboardVisible && EllaCaptureDockPlacementScope.active(context)
                        ? EllaCaptureHost.homeTranscriptPreviewBuilder?.call(context)
                        : null,
                    statusChildren: [
                      if (provider.earlierHistoryFailure != null)
                        Text(
                          provider.historyCursorNeedsRefresh
                              ? context.l10n.ellaEarlierHistoryRefreshRequired
                              : provider.earlierHistoryFailure!.retryable
                                  ? context.l10n.ellaEarlierHistoryFailed
                                  : context.l10n.ellaChatHistoryUnavailableTitle,
                          textAlign: TextAlign.center,
                          style: TextStyle(color: EllaPalette.of(context).textPrimary),
                        ),
                      const PassiveBackendProbeBanner(),
                      if (provider.lastHistoryFailure != null)
                        _ClientFailureBanner(
                          failure: provider.lastHistoryFailure!,
                          isHistoryFailure: true,
                          onRetry: provider.lastHistoryFailure!.retryable ? () => provider.refreshMessages() : null,
                        ),
                      if (provider.lastStreamFailure != null)
                        _ClientFailureBanner(
                          failure: provider.lastStreamFailure!,
                          onRetry: provider.canRetryLastMessage ? provider.retryLastFailedMessage : null,
                        ),
                    ],
                    children: [
                      // Messages area - takes up remaining space
                      Expanded(
                        child: provider.isLoadingMessages && !provider.hasCachedMessages
                            ? SingleChildScrollView(
                                child: Column(
                                  mainAxisSize: MainAxisSize.min,
                                  children: [
                                    _buildSyncingStatus(context),
                                    CircularProgressIndicator(
                                      valueColor: AlwaysStoppedAnimation<Color>(EllaPalette.of(context).primary),
                                    ),
                                    const SizedBox(height: 16),
                                    Text(
                                      provider.firstTimeLoadingText,
                                      style: TextStyle(color: EllaPalette.of(context).textPrimary),
                                    ),
                                  ],
                                ),
                              )
                            : provider.isClearingChat
                                ? SingleChildScrollView(
                                    child: Column(
                                      mainAxisSize: MainAxisSize.min,
                                      children: [
                                        CircularProgressIndicator(
                                          valueColor: AlwaysStoppedAnimation<Color>(EllaPalette.of(context).primary),
                                        ),
                                        const SizedBox(height: 16),
                                        Text(
                                          context.l10n.deletingMessages,
                                          style: TextStyle(color: EllaPalette.of(context).textPrimary),
                                        ),
                                      ],
                                    ),
                                  )
                                : (chatMessages.isEmpty && !provider.showsEarlierHistoryControl)
                                    ? Center(
                                        child: SingleChildScrollView(
                                          child: Padding(
                                            padding: const EdgeInsets.only(bottom: 32.0),
                                            child: Column(
                                              mainAxisSize: MainAxisSize.min,
                                              children: [
                                                if (provider.isLoadingMessages) _buildSyncingStatus(context),
                                                Text(
                                                  connectivityProvider.isConnected
                                                      ? context.l10n.noMessagesYet
                                                      : context.l10n.noInternetConnection,
                                                  textAlign: TextAlign.center,
                                                  style: TextStyle(color: EllaPalette.of(context).textPrimary),
                                                ),
                                              ],
                                            ),
                                          ),
                                        ),
                                      )
                                    : Theme(
                                        data: Theme.of(context).copyWith(
                                          textSelectionTheme: TextSelectionThemeData(
                                            selectionColor: EllaPalette.of(context).primaryLight.withOpacity(0.3),
                                            selectionHandleColor: EllaPalette.of(context).primary,
                                          ),
                                        ),
                                        child: RefreshIndicator(
                                          onRefresh: () => provider.refreshMessages(),
                                          color: EllaPalette.of(context).primary,
                                          backgroundColor: EllaPalette.of(context).bgSecondary,
                                          child: ListView.builder(
                                            reverse: true,
                                            controller: scrollController,
                                            // With reverse:true, visual bottom=start of list (near input).
                                            // bottom padding = space between newest message and input bar.
                                            // top padding = space above oldest message (visual top).
                                            // The entire history viewport starts below the interactive header.
                                            // Padding only spaces the oldest item inside that viewport.
                                            padding: const EdgeInsets.fromLTRB(18, 16, 18, 8),
                                            itemCount: chatMessages.length + (provider.isLoadingMessages ? 1 : 0),
                                            itemBuilder: (context, listIndex) {
                                              if (provider.isLoadingMessages && listIndex == 0) {
                                                return _buildSyncingStatus(context);
                                              }
                                              final reverseIndex = listIndex - (provider.isLoadingMessages ? 1 : 0);
                                              // reverse:true renders index 0 at visual bottom (newest)
                                              final chatIndex = chatMessages.length - 1 - reverseIndex;
                                              final message = chatMessages[chatIndex];

                                              // Space between messages (top in visual terms = higher reverseIndex)
                                              final double spacing = provider.historyPaginationEnabled
                                                  ? 12
                                                  : reverseIndex == chatMessages.length - 1
                                                      ? 0
                                                      : 12;

                                              return Padding(
                                                key: ValueKey(message.historyWidgetKey),
                                                // In reverse mode: "top" = visually above this item (toward older msgs)
                                                padding: EdgeInsets.only(top: spacing),
                                                child: message.sender == MessageSender.ai
                                                    ? AIMessage(
                                                        showTypingIndicator: provider.showTypingIndicator &&
                                                            chatIndex == chatMessages.length - 1,
                                                        message: message,
                                                        sendMessage: (text) =>
                                                            _sendMessageUtil(text, draftEpoch: draftEpoch),
                                                        onAskOmi: (text) {
                                                          if (!_draftCallbackIsCurrent(draftEpoch)) return;
                                                          setState(() {
                                                            _selectedContext = text;
                                                          });
                                                          textFieldFocusNode.requestFocus();
                                                        },
                                                        displayOptions: chatMessages.length <= 1 &&
                                                            provider.messageSenderApp(message.appId)?.isNotPersona() ==
                                                                true,
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
                                                        onRetry: message.clientDeliveryState ==
                                                                ClientMessageDeliveryState.failed
                                                            ? () => provider.retryFailedMessage(message.id)
                                                            : null,
                                                        onAskOmi: (text) {
                                                          if (!_draftCallbackIsCurrent(draftEpoch)) return;
                                                          setState(() {
                                                            _selectedContext = text;
                                                          });
                                                          textFieldFocusNode.requestFocus();
                                                        },
                                                      ),
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
                          borderRadius: BorderRadius.only(topLeft: Radius.circular(22), topRight: Radius.circular(22)),
                        ),
                        child: Consumer2<HomeProvider, VoiceRecorderProvider>(
                          builder: (context, home, voiceRecorderProvider, child) {
                            if (!voiceRecorderProvider.isActive) _recorderDraftEpoch = null;
                            final recorderEpoch = _recorderDraftEpoch;
                            bool shouldShowSendButton(MessageProvider p) {
                              return !p.requiresClientUpdate && !p.sendingMessage && !voiceRecorderProvider.isActive;
                            }

                            bool shouldShowVoiceRecorderButton() {
                              return !provider.requiresClientUpdate && !voiceRecorderProvider.isActive;
                            }

                            return Column(
                              children: [
                                const IrisWhisperMessage(),
                                const IrisWhisperStopBar(hideWhenMessageVisible: true),
                                // Selected images display above the send bar
                                Consumer<MessageProvider>(
                                  builder: (context, provider, child) {
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
                                                color: EllaPalette.of(context).bgTertiary,
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
                                                    Center(
                                                      child: Icon(
                                                        Icons.insert_drive_file,
                                                        color: EllaPalette.of(context).textPrimary,
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
                                  },
                                ),
                                // Send bar
                                SafeArea(
                                  // The footer is already below the safe-area header.
                                  top: false,
                                  bottom: false,
                                  maintainBottomViewPadding: false,
                                  child: Padding(
                                    padding: EdgeInsets.only(
                                      left: 8,
                                      right: 8,
                                      top: provider.selectedFiles.isNotEmpty ? 0 : 8,
                                      bottom: widget.isPivotBottom
                                          ? (textFieldFocusNode.hasFocus
                                              ? 6 + (keyboardVisible ? 0 : MediaQuery.viewPaddingOf(context).bottom)
                                              : BottomNavBar.navigationHeight(context) +
                                                  MediaQuery.of(context).padding.bottom +
                                                  8)
                                          : (textFieldFocusNode.hasFocus &&
                                                  (textController.text.length > 40 ||
                                                      textController.text.contains('\n'))
                                              ? 0
                                              : 2),
                                    ),
                                    child: _ChatComposerLayout(
                                      editorFocused: textFieldFocusNode.hasFocus,
                                      children: [
                                        // The editor owns its pill. Voice/Send are separate siblings.
                                        Expanded(
                                          child: EllaV7Glass(
                                            key: const Key('iris-composer-field-surface'),
                                            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
                                            radius: 31,
                                            child: Column(
                                              mainAxisSize: MainAxisSize.min,
                                              crossAxisAlignment: CrossAxisAlignment.start,
                                              children: [
                                                if (_selectedContext != null && !voiceRecorderProvider.isActive)
                                                  Padding(
                                                    padding: const EdgeInsets.only(bottom: 4, top: 4, left: 2),
                                                    child: Container(
                                                      decoration: BoxDecoration(
                                                        color: EllaPalette.of(context).bgTertiary,
                                                        borderRadius: BorderRadius.circular(16),
                                                      ),
                                                      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                                                      child: Row(
                                                        mainAxisSize: MainAxisSize.min,
                                                        children: [
                                                          const Padding(
                                                            padding: EdgeInsets.only(top: 1),
                                                            child: Icon(
                                                              Icons.subdirectory_arrow_right,
                                                              size: 14,
                                                              color: Colors.blue,
                                                            ),
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
                                                            child: const Icon(
                                                              Icons.close,
                                                              size: 14,
                                                              color: Colors.blue,
                                                            ),
                                                          ),
                                                        ],
                                                      ),
                                                    ),
                                                  ),
                                                voiceRecorderProvider.isActive && recorderEpoch == draftEpoch
                                                    ? VoiceRecorderWidget(
                                                        onTranscriptReady: (transcript) {
                                                          if (recorderEpoch == null ||
                                                              !_draftCallbackIsCurrent(recorderEpoch)) {
                                                            return;
                                                          }
                                                          textController.text = transcript;
                                                          voiceRecorderProvider.close();
                                                          context.read<MessageProvider>().setNextMessageOriginIsVoice(
                                                                true,
                                                              );
                                                        },
                                                        onClose: () {
                                                          voiceRecorderProvider.close();
                                                        },
                                                      )
                                                    : Theme(
                                                        data: Theme.of(context).copyWith(
                                                          textSelectionTheme: TextSelectionThemeData(
                                                            selectionColor: EllaPalette.of(
                                                              context,
                                                            ).primaryLight.withOpacity(0.4),
                                                            selectionHandleColor: EllaPalette.of(context).primary,
                                                          ),
                                                        ),
                                                        child: _ChatEditorViewport(
                                                          keyboardVisible: keyboardVisible,
                                                          child: TextField(
                                                            enabled: !provider.requiresClientUpdate,
                                                            controller: textController,
                                                            focusNode: textFieldFocusNode,
                                                            obscureText: false,
                                                            textAlign: TextAlign.start,
                                                            textAlignVertical: TextAlignVertical.center,
                                                            decoration: InputDecoration(
                                                              filled: false,
                                                              fillColor: Colors.transparent,
                                                              hintText: context.l10n.irisAskElla,
                                                              // Match the bounded editor; an oversized hint can offset the caret above it.
                                                              hintMaxLines: 2,
                                                              hintStyle: TextStyle(
                                                                fontSize: 17.0,
                                                                color: EllaPalette.of(context).textDisabled,
                                                              ),
                                                              focusedBorder: InputBorder.none,
                                                              enabledBorder: InputBorder.none,
                                                              contentPadding: const EdgeInsets.symmetric(
                                                                horizontal: 4,
                                                                vertical: 12,
                                                              ),
                                                              isDense: true,
                                                            ),
                                                            minLines: 1,
                                                            maxLines: 10,
                                                            keyboardType: TextInputType.multiline,
                                                            textCapitalization: TextCapitalization.sentences,
                                                            style: TextStyle(
                                                              fontSize: 17.0,
                                                              color: EllaPalette.of(context).textPrimary,
                                                              height: 1.4,
                                                            ),
                                                          ),
                                                        ),
                                                      ),
                                              ],
                                            ),
                                          ),
                                        ),
                                        if (shouldShowVoiceRecorderButton() && !textFieldFocusNode.hasFocus)
                                          IconButton(
                                            key: const Key('chat-composer-dictation'),
                                            tooltip: ForegroundDictationRegistration.current == null
                                                ? context.l10n.ellaDictationUnavailable
                                                : context.l10n.ellaDictationTitle,
                                            constraints: const BoxConstraints.tightFor(width: 48, height: 48),
                                            icon: const Icon(Icons.mic_rounded),
                                            onPressed: () => _openDictation(),
                                          ),
                                        if (shouldShowVoiceRecorderButton())
                                          DecoratedBox(
                                            key: const Key('iris-composer-talk-surface'),
                                            decoration: BoxDecoration(
                                              color: EllaPalette.of(context).userBubbleFill,
                                              shape: BoxShape.circle,
                                            ),
                                            child: IconButton(
                                              key: const Key('iris-composer-talk'),
                                              tooltip: context.l10n.irisLiveVoice,
                                              color: EllaPalette.of(context).userBubbleInk,
                                              constraints: const BoxConstraints.tightFor(width: 48, height: 48),
                                              style: IconButton.styleFrom(
                                                backgroundColor: EllaPalette.of(context).userBubbleFill,
                                                foregroundColor: EllaPalette.of(context).userBubbleInk,
                                                shape: const CircleBorder(),
                                              ),
                                              icon: const Icon(Icons.phone_in_talk_outlined),
                                              onPressed: () {
                                                if (!_draftCallbackIsCurrent(draftEpoch) ||
                                                    voiceRecorderProvider.isActive) return;
                                                FocusScope.of(context).unfocus();
                                                home.setIndex(2);
                                              },
                                            ),
                                          ),
                                        // Send button - only show when there's text
                                        if (shouldShowSendButton(provider))
                                          ValueListenableBuilder<TextEditingValue>(
                                            valueListenable: textController,
                                            builder: (context, value, child) {
                                              bool hasText = value.text.trim().isNotEmpty;
                                              if (!hasText) return const SizedBox.shrink();

                                              bool canSend =
                                                  hasText && !provider.sendingMessage && !provider.isUploadingFiles;

                                              return Semantics(
                                                label: context.l10n.send,
                                                button: true,
                                                enabled: canSend,
                                                child: GestureDetector(
                                                  key: const Key('chat-composer-send'),
                                                  behavior: HitTestBehavior.opaque,
                                                  onTap: canSend
                                                      ? () {
                                                          HapticFeedback.mediumImpact();
                                                          String message = textController.text.trim();
                                                          if (message.isEmpty) return;
                                                          _sendMessageUtil(message, draftEpoch: draftEpoch);
                                                        }
                                                      : null,
                                                  child: Container(
                                                    height: 44,
                                                    width: 44,
                                                    decoration: BoxDecoration(
                                                      color: EllaPalette.of(context).cardDeep,
                                                      shape: BoxShape.circle,
                                                    ),
                                                    child: Center(
                                                      child: FaIcon(
                                                        FontAwesomeIcons.arrowUp,
                                                        color: EllaPalette.of(context).textPrimary,
                                                        size: 18,
                                                      ),
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
                              ],
                            );
                          },
                        ),
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
                if (floatingHeader) ...[
                  Positioned(
                    top: MediaQuery.paddingOf(context).top,
                    left: 8,
                    right: 8,
                    height: headerClearance - MediaQuery.paddingOf(context).top,
                    child: Stack(
                      key: const Key('iris-chat-floating-header'),
                      children: [
                        Positioned(
                          left: 0,
                          right: 0,
                          top: 0,
                          child: EllaV7ChatIdentity(
                            inputActive: textFieldFocusNode.hasFocus || textController.text.isNotEmpty,
                          ),
                        ),
                        Positioned(
                          right: 56,
                          top: 0,
                          width: 48,
                          height: 48,
                          child: ListenableBuilder(
                            listenable: IrisCaptureControls.shared,
                            builder: (context, _) {
                              final enabled = IrisCaptureControls.shared.readWhispers?.call();
                              return IconButton(
                                key: const Key('chat-whispers-entry'),
                                disabledColor: EllaPalette.of(context).textTertiary,
                                tooltip: enabled == null
                                    ? context.l10n.todayWhispersUnavailable
                                    : '${context.l10n.todayWhispersTitle} · ${enabled ? context.l10n.on : context.l10n.off}',
                                color: EllaPalette.of(context).textPrimary,
                                onPressed: IrisCaptureControls.shared.openWhispers,
                                icon: const Icon(Icons.auto_awesome_outlined),
                              );
                            },
                          ),
                        ),
                        Positioned(
                          right: 0,
                          top: 0,
                          width: 48,
                          height: 48,
                          child: EllaCaptureHost.homeCaptureDockBuilder!(context),
                        ),
                      ],
                    ),
                  ),
                ],
              ],
            ),
          );
        },
      ),
    );
    return ExcludeFocus(
      excluding: !chatVisible || ModalRoute.of(context)?.isCurrent == false,
      child: page,
    );
  }

  // Fixed above the history viewport: an oversized row must not hide paging
  // or require a successful touch drag before older content can be reached.
  Widget _historyNavigationToolbar(MessageProvider provider, {required bool hasMessages}) => Column(
        key: const Key('chat-history-navigation'),
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(
            children: [
              if (hasMessages) ...[
                IconButton(
                  key: const Key('chat-history-older'),
                  tooltip: context.l10n.ellaHistoryOlderViewport,
                  constraints: const BoxConstraints.tightFor(width: 48, height: 48),
                  color: EllaPalette.of(context).textPrimary,
                  onPressed: () => _moveHistoryViewport(older: true),
                  icon: const Icon(Icons.keyboard_arrow_up),
                ),
                IconButton(
                  key: const Key('chat-history-newer'),
                  tooltip: context.l10n.ellaHistoryNewerViewport,
                  constraints: const BoxConstraints.tightFor(width: 48, height: 48),
                  color: EllaPalette.of(context).textPrimary,
                  onPressed: () => _moveHistoryViewport(older: false),
                  icon: const Icon(Icons.keyboard_arrow_down),
                ),
              ],
              if (provider.showsEarlierHistoryControl)
                Expanded(
                  child: TextButton(
                    key: const Key('ella-load-earlier-history'),
                    style: TextButton.styleFrom(
                      foregroundColor: EllaPalette.of(context).textPrimary,
                      disabledForegroundColor: EllaPalette.of(context).textPrimary,
                      minimumSize: const Size(44, 44),
                    ),
                    onPressed: provider.historyCursorNeedsRefresh
                        ? () => provider.refreshMessages()
                        : provider.canLoadEarlierHistory
                            ? () => _loadEarlierHistory(provider)
                            : null,
                    child: MediaQuery.textScalerOf(context).scale(1) >= 2
                        ? Icon(
                            provider.isLoadingEarlierHistory ? Icons.hourglass_empty : Icons.history,
                            semanticLabel: provider.isLoadingEarlierHistory
                                ? context.l10n.ellaEarlierHistoryLoading
                                : provider.historyCursorNeedsRefresh
                                    ? context.l10n.ellaEarlierHistoryRefresh
                                    : context.l10n.ellaEarlierHistoryLoad,
                          )
                        : Text(
                            provider.isLoadingEarlierHistory
                                ? context.l10n.ellaEarlierHistoryLoading
                                : provider.historyCursorNeedsRefresh
                                    ? context.l10n.ellaEarlierHistoryRefresh
                                    : context.l10n.ellaEarlierHistoryLoad,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                          ),
                  ),
                ),
            ],
          ),
        ],
      );

  void _moveHistoryViewport({required bool older}) {
    if (!mounted || !scrollController.hasClients) return;
    final position = scrollController.position;
    final step = (position.viewportDimension * .8).clamp(32.0, double.infinity);
    // A reversed vertical list's positive offset moves toward older content.
    final target = (position.pixels + (older ? step : -step)).clamp(position.minScrollExtent, position.maxScrollExtent);
    // No focus/draft change, network request or delayed authority callback.
    scrollController.animateTo(target, duration: const Duration(milliseconds: 220), curve: Curves.easeOut);
  }

  Future<void> _loadEarlierHistory(MessageProvider provider) async {
    final binding = provider.localDraftResetGeneration;
    final refresh = provider.historyTimelineGeneration;
    final focused = textFieldFocusNode.hasFocus;
    final offset = scrollController.hasClients ? scrollController.offset : null;
    final adopted = await provider.loadEarlierMessages();
    if (!adopted || !mounted || offset == null) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted ||
          binding != provider.localDraftResetGeneration ||
          refresh != provider.historyTimelineGeneration ||
          focused != textFieldFocusNode.hasFocus ||
          !scrollController.hasClients ||
          (scrollController.offset - offset).abs() > 1) return;
      // Older rows are appended at the far end of the reversed list. Existing
      // keyed rows keep their indices and pixel anchor; never override a newer
      // user scroll, account lifetime or keyboard/focus transition.
      scrollController.jumpTo(offset.clamp(0, scrollController.position.maxScrollExtent));
    });
  }

  Future<void> _sendMessageUtil(String text, {required int draftEpoch}) async {
    if (!_draftCallbackIsCurrent(draftEpoch)) return;
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
    final outgoing = provider.addMessageLocally(text);
    textController.clear();

    Future.delayed(const Duration(milliseconds: 300), () {
      if (mounted) scrollToBottomOnSend();
    });

    await provider.sendMessageStreamToServer(text, localMessageId: outgoing.id);
    if (_draftCallbackIsCurrent(draftEpoch)) provider.clearSelectedFiles();
  }

  sendInitialAppMessage(App? app) async {
    _syncDraftBoundary();
    final epoch = _draftEpoch;
    if (!_draftCallbackIsCurrent(epoch)) return;
    context.read<MessageProvider>().setSendingMessage(true);
    scrollToBottom();
    ServerMessage message = await getInitialAppMessage(app?.id);
    if (mounted && _draftCallbackIsCurrent(epoch)) {
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
        scrollController.animateTo(0, duration: const Duration(milliseconds: 300), curve: Curves.easeOutCubic);
      }
    });
  }

  void _ensureAtBottom({int delayMs = 50}) {
    Future.delayed(Duration(milliseconds: delayMs), () {
      if (!scrollController.hasClients) return;

      // With reverse: true, bottom is offset 0
      if (scrollController.position.pixels > 20) {
        scrollController.animateTo(0, duration: const Duration(milliseconds: 150), curve: Curves.easeOut);
      }
    });
  }

  void scrollToBottom({bool animated = false}) {
    if (!scrollController.hasClients) return;

    // With reverse: true, bottom is offset 0
    if (animated) {
      scrollController.animateTo(0, duration: const Duration(milliseconds: 220), curve: Curves.easeOut);
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
        return getDialog(
          context,
          () {
            Navigator.of(context).pop();
          },
          () {
            if (mounted) {
              _discardDraft(rebuild: true);
              context.read<MessageProvider>().clearChat();
              Navigator.of(context).pop();
            }
          },
          context.l10n.clearChatQuestion,
          context.l10n.clearChatConfirm,
        );
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

    // Store references before async operation
    final messageProvider = mounted ? context.read<MessageProvider>() : null;
    if (messageProvider == null) return;

    // Set the selected app
    appProvider.setSelectedChatAppId(appId);
    _syncDraftBoundary();
    final selectionEpoch = _draftEpoch;

    // Add a small delay to let the keyboard animation complete
    // This prevents the widget from being unmounted during the keyboard transition
    await Future.delayed(const Duration(milliseconds: 100));

    // The delayed selection must still name the same local conversation.
    if (!_draftCallbackIsCurrent(selectionEpoch)) return;

    // Perform async operation
    await messageProvider.refreshMessages(dropdownSelected: true);

    if (!_draftCallbackIsCurrent(selectionEpoch)) return;

    // Get the selected app and send initial message if needed
    var app = appProvider.getSelectedApp();
    if (messageProvider.messages.isEmpty) {
      messageProvider.sendInitialAppMessage(app);
    }
  }

  PreferredSizeWidget _buildAppBar(BuildContext context, MessageProvider provider) {
    return AppBar(
      toolbarHeight: 56,
      primary: true,
      backgroundColor: Colors.transparent,
      surfaceTintColor: Colors.transparent,
      scrolledUnderElevation: 0,
      elevation: 0,
      leading: widget.isPivotBottom
          ? const SizedBox(width: 48)
          : Container(
              width: 36,
              height: 36,
              margin: const EdgeInsets.all(8),
              decoration: BoxDecoration(color: EllaPalette.of(context).bgTertiary, shape: BoxShape.circle),
              child: IconButton(
                padding: EdgeInsets.zero,
                icon: Icon(Icons.arrow_back_ios_new, color: EllaPalette.of(context).textPrimary, size: 18),
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
      actions: [
        if (widget.isPivotBottom &&
            EllaCaptureDockPlacementScope.active(context) &&
            EllaCaptureHost.homeCaptureDockBuilder != null)
          SizedBox(
            key: const Key('iris-chat-header-controls'),
            width: 56,
            height: 56,
            child: EllaCaptureHost.homeCaptureDockBuilder!(context),
          ),
      ],
    );
  }

  Widget _buildSyncingStatus(BuildContext context) => Padding(
        padding: const EdgeInsets.only(bottom: 6),
        child: DecoratedBox(
          decoration: BoxDecoration(color: EllaPalette.of(context).bgTertiary, borderRadius: BorderRadius.circular(14)),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
            child: Text(
              context.l10n.syncingMessages,
              style: TextStyle(color: EllaPalette.of(context).textSecondary, fontSize: 12),
            ),
          ),
        ),
      );

  Widget _buildSelectedAppDisplay(BuildContext context, AppProvider provider) {
    final messageProvider = Provider.of<MessageProvider>(context, listen: false);
    var selectedApp = messageProvider.chatApps.firstWhereOrNull((app) => app.id == provider.selectedChatAppId);

    return IgnorePointer(
      child: Semantics(
        container: true,
        label: selectedApp?.getName() ?? 'Ella',
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.center,
          mainAxisAlignment: MainAxisAlignment.center,
          mainAxisSize: MainAxisSize.min,
          children: [
            selectedApp != null ? _getAppAvatar(selectedApp) : _getEllaAvatar(header: true),
            if (selectedApp != null) const SizedBox(width: 8),
            if (selectedApp != null)
              Container(
                constraints: const BoxConstraints(maxWidth: 140),
                child: Text(
                  selectedApp.getName(),
                  style: TextStyle(color: EllaPalette.of(context).textPrimary, fontSize: 16),
                  overflow: TextOverflow.ellipsis,
                ),
              ),
          ],
        ),
      ),
    );
  }

  // ignore: unused_element
  Widget _buildChatAppsEndDrawer(BuildContext context) {
    return Drawer(
      backgroundColor: EllaPalette.of(context).bgSecondary,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.only(topLeft: Radius.circular(20), bottomLeft: Radius.circular(20)),
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
                        style: TextStyle(
                          color: EllaPalette.of(context).textPrimary,
                          fontSize: 20,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      IconButton(
                        icon: Padding(
                          padding: const EdgeInsets.only(left: 2, top: 1),
                          child: FaIcon(FontAwesomeIcons.xmark, color: EllaPalette.of(context).textTertiary, size: 18),
                        ),
                        onPressed: () => Navigator.of(context).pop(),
                      ),
                    ],
                  ),
                ),
                Divider(color: EllaPalette.of(context).bgTertiary, height: 1),
                // Actions
                ListTile(
                  leading: const Padding(
                    padding: EdgeInsets.only(left: 2, top: 1),
                    child: FaIcon(FontAwesomeIcons.solidTrashCan, color: Colors.redAccent, size: 20),
                  ),
                  title: Text(context.l10n.clearChat, style: const TextStyle(color: Colors.redAccent, fontSize: 16)),
                  onTap: () {
                    Navigator.of(context).pop();
                    _handleAppSelection('clear_chat', appProvider);
                  },
                ),
                ListTile(
                  leading: Padding(
                    padding: const EdgeInsets.only(left: 2, top: 1),
                    child: FaIcon(FontAwesomeIcons.circlePlus, color: EllaPalette.of(context).textPrimary, size: 20),
                  ),
                  title: Text(
                    context.l10n.enableApps,
                    style: TextStyle(color: EllaPalette.of(context).textPrimary, fontSize: 16),
                  ),
                  trailing: Padding(
                    padding: const EdgeInsets.only(left: 2, top: 1),
                    child: FaIcon(FontAwesomeIcons.chevronRight, color: EllaPalette.of(context).textDisabled, size: 14),
                  ),
                  onTap: () {
                    Navigator.of(context).pop();
                    _navigateToChatAppsPage();
                  },
                ),
                Divider(color: EllaPalette.of(context).bgTertiary, height: 1),
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 16, 20, 8),
                  child: Text(
                    context.l10n.selectApp,
                    style: TextStyle(
                      color: EllaPalette.of(context).textTertiary,
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
                      ...chatApps.map(
                        (app) => _buildDrawerAppItem(
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
                        ),
                      ),
                      if (chatApps.isEmpty)
                        Padding(
                          padding: const EdgeInsets.all(20),
                          child: Text(
                            context.l10n.noChatAppsEnabled,
                            style: TextStyle(color: EllaPalette.of(context).textDisabled, fontSize: 14),
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
                style: TextStyle(color: EllaPalette.of(context).textPrimary, fontSize: 16),
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
                decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(16)),
                child: Text(
                  context.l10n.cancel,
                  style: const TextStyle(color: Colors.black, fontSize: 13, fontWeight: FontWeight.w500),
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
                decoration: BoxDecoration(color: Colors.redAccent, borderRadius: BorderRadius.circular(16)),
                child: Text(
                  context.l10n.disable,
                  style: const TextStyle(color: Colors.white, fontSize: 13, fontWeight: FontWeight.w500),
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
        style: TextStyle(color: EllaPalette.of(context).textPrimary, fontSize: 16),
        overflow: TextOverflow.ellipsis,
      ),
      trailing: isSelected
          ? Padding(
              padding: const EdgeInsets.only(left: 2, top: 1),
              child: FaIcon(FontAwesomeIcons.solidCircleCheck, color: EllaPalette.of(context).primary, size: 18),
            )
          : appId != null && onConfirmDelete != null
              ? GestureDetector(
                  onTap: () {
                    setState(() {
                      _pendingDeleteAppId = appId;
                    });
                  },
                  child: Padding(
                    padding: const EdgeInsets.only(left: 2, top: 1),
                    child:
                        FaIcon(FontAwesomeIcons.solidTrashCan, color: EllaPalette.of(context).textDisabled, size: 16),
                  ),
                )
              : null,
      selected: isSelected,
      selectedTileColor: EllaPalette.of(context).bgTertiary.withOpacity(0.5),
      onTap: onTap,
    );
  }

  Widget _getAppAvatar(App app) {
    return CachedNetworkImage(
      imageUrl: app.getImageUrl(),
      imageBuilder: (context, imageProvider) {
        return CircleAvatar(backgroundColor: Colors.white, radius: 12, backgroundImage: imageProvider);
      },
      errorWidget: (context, url, error) {
        return const CircleAvatar(backgroundColor: Colors.white, radius: 12, child: Icon(Icons.error_outline_rounded));
      },
      progressIndicatorBuilder: (context, url, progress) => CircleAvatar(
        backgroundColor: Colors.white,
        radius: 12,
        child: CircularProgressIndicator(
          value: progress.progress,
          valueColor: AlwaysStoppedAnimation<Color>(EllaPalette.of(context).primary),
        ),
      ),
    );
  }

  // ignore: unused_element
  Widget _getOmiAvatar() {
    return Container(
      height: 24,
      width: 24,
      decoration: BoxDecoration(
        color: EllaPalette.of(context).primary,
        borderRadius: const BorderRadius.all(Radius.circular(12.0)),
      ),
      child: const Icon(Icons.favorite, color: Colors.white, size: 14),
    );
  }

  Widget _getEllaAvatar({bool header = false}) => EllaCompanionAvatar(
        size: header ? 48 : 24,
        animate: header,
        stateReader: header
            ? () => lumeStateForActivity(
                  captureActive: EllaCaptureHost.upstreamCaptureActive
                      ? EllaCaptureHost.companionRecordingActive
                      : switch (context.read<CaptureProvider>().recordingState) {
                          RecordingState.record ||
                          RecordingState.deviceRecord ||
                          RecordingState.systemAudioRecord =>
                            true,
                          _ => false,
                        },
                  requestPending: context.read<MessageProvider>().sendingMessage,
                  audioPlaying: false, // Native active/queued is not proof of audible playback.
                )
            : null,
      );

  // Composer B intentionally has no attachment entry in this first build.
  // ignore: unused_element
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
              if (icon != null && !isCancel) Icon(icon, color: EllaPalette.of(context).textTertiary, size: 24),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildDivider() {
    return Container(
      height: 0.5,
      color: EllaPalette.of(context).bgTertiary,
      margin: const EdgeInsets.symmetric(horizontal: 20),
    );
  }
}

class _ClientFailureBanner extends StatelessWidget {
  const _ClientFailureBanner({required this.failure, this.onRetry, this.isHistoryFailure = false});

  final ClientApiFailure failure;
  final VoidCallback? onRetry;
  final bool isHistoryFailure;

  @override
  Widget build(BuildContext context) {
    final (title, body) = switch (failure.kind) {
      ClientApiFailureKind.updateRequired => (
          context.l10n.ellaClientUpdateRequiredTitle,
          context.l10n.ellaClientUpdateRequiredBody,
        ),
      ClientApiFailureKind.workspaceRequired => (
          context.l10n.ellaWorkspaceUnavailableTitle,
          context.l10n.ellaWorkspaceUnavailableBody,
        ),
      ClientApiFailureKind.consentRequired => (
          context.l10n.aiConsentNeededReview,
          context.l10n.aiConsentNeededReviewBody,
        ),
      _ => isHistoryFailure
          ? (context.l10n.ellaChatHistoryUnavailableTitle, context.l10n.ellaChatHistoryUnavailableBody)
          : (context.l10n.ellaChatCouldntSend, context.l10n.ellaChatUnavailableBody),
    };
    return Semantics(
      liveRegion: true,
      child: Container(
        width: double.infinity,
        margin: const EdgeInsets.fromLTRB(18, 8, 18, 0),
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: EllaPalette.of(context).bgSecondary,
          borderRadius: BorderRadius.circular(14),
          border: Border.all(color: EllaPalette.of(context).textDisabled),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(
                  child: Text(
                    title,
                    style: TextStyle(fontWeight: FontWeight.w600, color: EllaPalette.of(context).textPrimary),
                  ),
                ),
                if (onRetry != null) ...[
                  const SizedBox(width: 8),
                  TextButton(
                    key: Key(isHistoryFailure ? 'ella-chat-retry-history' : 'ella-chat-retry-last-message'),
                    style: TextButton.styleFrom(
                      foregroundColor: EllaPalette.of(context).textPrimary,
                      minimumSize: const Size(44, 44),
                    ),
                    onPressed: onRetry,
                    child: Text(context.l10n.retry),
                  ),
                ],
              ],
            ),
            const SizedBox(height: 4),
            Text(body, style: TextStyle(color: EllaPalette.of(context).textTertiary)),
          ],
        ),
      ),
    );
  }
}

/// The dock and keyboard can leave less room than the editor's natural height.
/// Keep failure notices out of the composer viewport: errors scroll separately
/// above history, while controls only scroll when their own content cannot fit.
class _ChatViewportLayout extends StatelessWidget {
  const _ChatViewportLayout({
    required this.children,
    required this.statusChildren,
    this.headerClearance = 0,
    this.historyNavigation,
    this.transcriptPreview,
  });
  final Widget? historyNavigation;
  final Widget? transcriptPreview;
  final double headerClearance;
  final List<Widget> children;
  final List<Widget> statusChildren;

  @override
  Widget build(BuildContext context) => LayoutBuilder(
        builder: (context, constraints) {
          return Column(
            children: [
              Expanded(
                child: LayoutBuilder(
                  builder: (context, historyConstraints) {
                    final reservedHeader = headerClearance.clamp(
                      0.0,
                      (historyConstraints.maxHeight - 32 - (historyNavigation == null ? 0 : 48)).clamp(
                        0.0,
                        double.infinity,
                      ),
                    );
                    final remaining =
                        (historyConstraints.maxHeight - reservedHeader - (historyNavigation == null ? 0 : 48))
                            .clamp(0.0, double.infinity);
                    // The active status chooses its natural height, before history is
                    // allocated. Keep a real history strip and never overlay either surface.
                    final largeTranscript = MediaQuery.textScalerOf(context).scale(1) >= 2;
                    final showTranscript = transcriptPreview != null && remaining >= (largeTranscript ? 112 : 80);
                    final previewMaxHeight = (remaining - 48.0).clamp(0.0, double.infinity);
                    return Column(
                      children: [
                        SizedBox(height: reservedHeader),
                        if (historyNavigation != null) SizedBox(height: 48, child: historyNavigation),
                        if (showTranscript)
                          ClipRect(
                            child: ConstrainedBox(
                              constraints: BoxConstraints(maxHeight: previewMaxHeight),
                              child: transcriptPreview,
                            ),
                          ),
                        Expanded(
                          child: LayoutBuilder(
                              builder: (context, remainingHistory) => Column(children: [
                                    ConstrainedBox(
                                      constraints: BoxConstraints(
                                        maxHeight: (remainingHistory.maxHeight * .4)
                                            .clamp(0.0, (remainingHistory.maxHeight - 32).clamp(0.0, double.infinity)),
                                      ),
                                      child: SingleChildScrollView(
                                        key: const Key('chat-status-viewport'),
                                        primary: false,
                                        child: Column(mainAxisSize: MainAxisSize.min, children: statusChildren),
                                      ),
                                    ),
                                    children.first,
                                  ])),
                        ),
                      ],
                    );
                  },
                ),
              ),
              ConstrainedBox(
                constraints: BoxConstraints(maxHeight: constraints.maxHeight * .76),
                child: SingleChildScrollView(
                  primary: false,
                  child: Column(mainAxisSize: MainAxisSize.min, children: children.skip(1).toList()),
                ),
              ),
            ],
          );
        },
      );
}

/// Multiline drafts scroll internally rather than pushing actions off screen.
class _ChatEditorViewport extends StatelessWidget {
  const _ChatEditorViewport({required this.child, required this.keyboardVisible});
  final Widget child;
  final bool keyboardVisible;

  @override
  Widget build(BuildContext context) {
    return ConstrainedBox(
      // Include vertical padding and slack for the taller iOS caret. An equal-height
      // viewport triggers EditableText's oversized-caret branch and can clamp reveal to zero.
      // Reserve two complete text lines with the field's 24px vertical padding.
      // This keeps short multiline drafts readable without a clipped preceding line.
      // Keep the same editor element and platform input connection while the
      // keyboard opens/closes. Adding this wrapper only after viewInsets changes
      // replaces EditableText during iOS keyboard presentation.
      constraints: BoxConstraints(
        maxHeight: keyboardVisible || MediaQuery.textScalerOf(context).scale(1) >= 1.28
            ? (MediaQuery.textScalerOf(context).scale(17) * 1.4 * 2).ceilToDouble() + 24
            : double.infinity,
      ),
      child: child,
    );
  }
}

/// Composer B keeps one round action outside the editor at every text size.
class _ChatComposerLayout extends StatelessWidget {
  const _ChatComposerLayout({required this.children, required this.editorFocused});
  final List<Widget> children;
  final bool editorFocused;
  @override
  Widget build(BuildContext context) => Row(
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          for (final child in children) ...[
            child,
            // The iOS selection handle has a 48px touch region. Reserve its
            // right half so it cannot cover Voice/Send while the editor is focused.
            if (child is Expanded) SizedBox(width: editorFocused ? 26 : 8),
          ],
        ],
      );
}

/// Private local editor lifetime evidence. Never passed to protected operations.
class _LocalDraftLifetimeAuthority implements AccountCommitAuthority {
  _LocalDraftLifetimeAuthority(this.uid, this.generation, this.authenticatedUid, this.preferences);
  @override
  final String uid;
  final int generation;
  final String Function() authenticatedUid;
  final SharedPreferencesUtil preferences;
  @override
  bool isCurrent() =>
      uid.isNotEmpty &&
      authenticatedUid() == uid &&
      preferences.uid == uid &&
      preferences.terminalAccountConsentAuthorityGeneration == generation;
  @override
  bool isExactCurrent() => isCurrent();
}
