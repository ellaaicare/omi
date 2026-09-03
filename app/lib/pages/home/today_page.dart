import 'dart:async';
import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/demo/today_card_fixtures.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/hardware/ella_hardware_artwork.dart';
import 'package:omi/ella/models/capture_source.dart';
import 'package:omi/ella/models/guardian_mode.dart';
import 'package:omi/ella/models/today_card.dart';
import 'package:omi/ella/pages/ella_memories_page.dart';
import 'package:omi/ella/pages/ella_voice_chat_page.dart';
import 'package:omi/ella/pages/guardian_alert_history_page.dart';
import 'package:omi/ella/services/ella_public_surface_policy.dart';
import 'package:omi/ella/services/guardian_mode_api.dart' as guardian_api;
import 'package:omi/ella/services/guardian_mode_service.dart' as guardian_native;
import 'package:omi/ella/services/memory_artwork_api.dart';
import 'package:omi/ella/services/today_card_controller.dart';
import 'package:omi/ella/services/today_card_repository.dart';
import 'package:omi/ella/services/v2v_client.dart';
import 'package:omi/ella/widgets/ella_breathing_dot.dart';
import 'package:omi/ella/widgets/today_card_surface.dart';
import 'package:omi/pages/capture/connect.dart';
import 'package:omi/pages/conversation_capturing/page.dart';
import 'package:omi/pages/conversation_detail/page.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/enums.dart';
import 'package:omi/utils/l10n_extensions.dart';

typedef TodayCardTalkRouteOpener = Future<void> Function(BuildContext context, TodayCard card);
typedef TodayCardAuthoritySnapshot = ({String uid, String authorityKey, bool isProvisioningReady});
typedef TodayCardAuthoritySnapshotProvider = TodayCardAuthoritySnapshot Function();
typedef TodayNowProvider = DateTime Function();
typedef GuardianModeLoader = Future<GuardianModeInfo?> Function();
typedef GuardianModeSetter = Future<bool> Function(GuardianModeState state);
typedef GuardianNativeLifecycle = Future<void> Function();
typedef GuardianAvailability = bool Function();
typedef _HomeArtworkAuthoritySnapshot = ({
  ExactAccountAuthorityVerifier authority,
  String uid,
  String profileBindingId,
  int generation,
});
typedef _ArtworkStudioSnapshot = ({
  MemoryArtworkPreferences preferences,
  MemoryArtworkLibraries? libraries,
  _ArtworkBackfillUiState backfillState,
  _ArtworkQueueLoadState queueLoadState,
  bool styleSaving,
  MemoryArtworkQueueStatus? queueStatus,
  bool queueControlBusy,
});

enum _HomeCaptureSource { phone, necklaceOwned, necklaceContinuous }

enum _ExternalCaptureSource { phone, necklace }

enum TodayCaptureDockMode { ready, starting, recording, finishing, unavailable }

enum _ArtworkBackfillUiState { idle, running, moreAvailable, complete, needsAttention }

enum _ArtworkQueueLoadState { idle, loading, loaded, failed }

class TodayCaptureDockPresentation {
  const TodayCaptureDockPresentation({
    required this.mode,
    required this.status,
    required this.primaryLabel,
    required this.primaryIcon,
    required this.primaryEnabled,
  });

  final TodayCaptureDockMode mode;
  final String status;
  final String primaryLabel;
  final IconData primaryIcon;
  final bool primaryEnabled;

  bool get emphasized => mode == TodayCaptureDockMode.recording;
}

EllaCaptureSource? todayActiveCaptureSource(RecordingState state, CaptureDiagnostics diagnostics) => switch (state) {
      RecordingState.record => EllaCaptureSource.phone,
      RecordingState.deviceRecord => EllaCaptureSource.necklace,
      RecordingState.initialising || RecordingState.pause || RecordingState.error => switch (diagnostics.source) {
          CaptureDiagnosticSource.phone => EllaCaptureSource.phone,
          CaptureDiagnosticSource.necklace => EllaCaptureSource.necklace,
          _ => null,
        },
      RecordingState.stop || RecordingState.systemAudioRecord => null,
    };

EllaCaptureSource todaySelectedCaptureSource({
  required RecordingState state,
  required CaptureDiagnostics diagnostics,
  required EllaCaptureSource? preferredSource,
}) {
  final activeSource = todayActiveCaptureSource(state, diagnostics);
  return switch (state) {
    RecordingState.record => EllaCaptureSource.phone,
    RecordingState.initialising ||
    RecordingState.pause ||
    RecordingState.error =>
      preferredSource ?? activeSource ?? EllaCaptureSource.phone,
    _ => preferredSource ?? EllaCaptureSource.phone,
  };
}

String whisperStatusLead(bool enabled) => enabled ? 'Whispers are on' : 'Whispers are off';

bool canReadDailyNote({required bool loading, required String text}) => !loading && text.trim().isNotEmpty;

bool shouldShowDailyNote(TodayCardViewState state) {
  final card = state.card;
  return card != null && card.headline.trim().isNotEmpty && card.body.trim().isNotEmpty;
}

String whisperStatusDetail(bool enabled) => enabled
    ? ' — Ella will speak up when she can help. 🪽'
    : " — Ella stays quiet, but she's still listening and remembering.";

List<ServerConversation> homeMemoryCanvasSelection(List<ServerConversation> conversations, {int limit = 2}) {
  final sorted = List<ServerConversation>.of(conversations)
    ..sort((a, b) => (b.startedAt ?? b.createdAt).compareTo(a.startedAt ?? a.createdAt));
  return sorted.take(limit).toList(growable: false);
}

String homeMemoryDisplayTitle(ServerConversation conversation, String fallback) {
  var title = conversation.structured.title
      .replaceFirst(RegExp(r'^🪽\s*'), '')
      .replaceFirst(RegExp(r'^(?:\[[^\]]+\]\s*)+'), '')
      .trim();
  title = title.split(RegExp(r'\s*(?:,|\band\b)\s*', caseSensitive: false)).first.trim();
  title = title.replaceAll(
    RegExp(
      r'\b(?:doctor|medical|clinical|monitoring|emergency|alert|tracking|detecting)(?:[- ]\w+)?\b',
      caseSensitive: false,
    ),
    '',
  );
  final words = title.split(RegExp(r'\s+')).where((word) => word.isNotEmpty).take(4).toList();
  return words.isEmpty ? fallback : words.join(' ');
}

class TodayPage extends StatefulWidget {
  const TodayPage({
    super.key,
    this.todayCardRepository,
    this.todayCardCache,
    this.todayCardTalkRouteOpener,
    this.todayCardUidOverride,
    this.todayCardAuthorityKeyOverride,
    this.todayCardReadyOverride,
    this.todayCardAuthoritySnapshotProvider,
    this.todayCardAuthorityChanges,
    this.nowProvider,
    this.guardianModeLoader,
    this.guardianModeSetter,
    this.guardianNativeStart,
    this.guardianNativeStop,
    this.guardianAvailability,
    this.memoryArtworkApi,
    this.memoryArtworkAuthorityProvider,
  });

  final TodayCardRepository? todayCardRepository;
  final TodayCardCache? todayCardCache;
  final TodayCardTalkRouteOpener? todayCardTalkRouteOpener;
  final String? todayCardUidOverride;
  final String? todayCardAuthorityKeyOverride;
  final bool? todayCardReadyOverride;
  final TodayCardAuthoritySnapshotProvider? todayCardAuthoritySnapshotProvider;
  final Listenable? todayCardAuthorityChanges;
  final TodayNowProvider? nowProvider;
  final GuardianModeLoader? guardianModeLoader;
  final GuardianModeSetter? guardianModeSetter;
  final GuardianNativeLifecycle? guardianNativeStart;
  final GuardianNativeLifecycle? guardianNativeStop;
  final GuardianAvailability? guardianAvailability;
  final MemoryArtworkApi? memoryArtworkApi;
  final MemoryArtworkAuthorityProvider? memoryArtworkAuthorityProvider;

  @visibleForTesting
  static V2VSessionScope sessionScopeFor(TodayCard card) =>
      V2VSessionScope.dailyCard(cardId: card.id, expectedVersion: card.version);

  @visibleForTesting
  static String cacheAuthorityKey(ActiveWalAuthority authority) {
    final owner = authority.owner;
    return sha256
        .convert(
          utf8.encode(
            '${owner.uid}\n${owner.profileBindingId}\n${owner.bindingRevision}\n'
            '${owner.consentReceiptId}\n${owner.authorityGenerationAtCapture}',
          ),
        )
        .toString();
  }

  @override
  State<TodayPage> createState() => TodayPageState();
}

class TodayPageState extends State<TodayPage> with WidgetsBindingObserver {
  final ScrollController _scrollController = ScrollController();
  late final TodayCardController _todayCardController;
  late final Listenable _todayCardAuthorityChanges;
  EllaProvisioningProvider? _provisioningProvider;
  ConversationProvider? _conversationProvider;
  bool _homeCaptureActive = false;
  bool _homeCaptureStarting = false;
  bool _homeCaptureFinalizationPending = false;
  Future<bool>? _homeCaptureFinalizationInFlight;
  _ExternalCaptureSource? _externalCaptureFinalizationSource;
  bool _abandonHomeCaptureAfterFinalization = false;
  int _homeCaptureAuthorityGeneration = 0;
  _HomeCaptureSource? _homeCaptureSource;
  bool _whispersOn = false;
  bool _whispersVerified = false;
  bool _whisperStateLoading = false;
  bool _whisperStateReloadPending = false;
  bool _updatingWhispers = false;
  bool _showBackToRecent = false;
  bool _homeMemoryPrefetchScheduled = false;
  bool _homeArtworkReloadScheduled = false;
  Future<MemoryArtworkBackfillPage?>? _homeArtworkBackfillInFlight;
  _HomeArtworkAuthoritySnapshot? _homeArtworkBackfillAuthority;
  Timer? _homeArtworkBackfillPollTimer;
  Timer? _homeArtworkQueuePollTimer;
  _ArtworkBackfillUiState _homeArtworkBackfillState = _ArtworkBackfillUiState.idle;
  bool _homeArtworkStyleSaving = false;
  bool _homeArtworkQueueControlBusy = false;
  _ArtworkQueueLoadState _homeArtworkQueueLoadState = _ArtworkQueueLoadState.idle;
  int _homeArtworkStyleOperationGeneration = 0;
  int _homeArtworkQueueOperationGeneration = 0;
  int _homeArtworkQueueRefreshSequence = 0;
  int _homeArtworkLibrariesRefreshSequence = 0;
  int _homeArtworkDisplayEpoch = 0;
  final ValueNotifier<_ArtworkStudioSnapshot?> _homeArtworkStudioState = ValueNotifier(null);
  MemoryGalleryLayout _homeMemoryLayout = MemoryGalleryLayout.journal;
  MemoryGallerySort _homeMemorySort = MemoryGallerySort.recent;
  MemoryArtworkPreferences? _homeArtworkPreferences;
  MemoryArtworkLibraries? _homeArtworkLibraries;
  MemoryArtworkQueueStatus? _homeArtworkQueueStatus;
  BtDevice? _resumeNecklaceAfterPhoneCapture;
  EllaCaptureSource? _selectedCaptureSource;

  static const _artworkBackfillComplete = '__complete__';

  late final MemoryArtworkApi _memoryArtworkApi = widget.memoryArtworkApi ?? MemoryArtworkApi();
  late final MemoryArtworkAuthorityProvider _memoryArtworkAuthorityProvider =
      widget.memoryArtworkAuthorityProvider ?? WalOwnerAuthority.active;

  bool get _guardianAvailable => widget.guardianAvailability?.call() ?? allowsGuardianSurface();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _scrollController.addListener(_handleHomeScroll);
    _loadHomeMemoryLayout();
    _todayCardController = TodayCardController(
      repository: widget.todayCardRepository ??
          (SharedPreferencesUtil.isTodayDesignPreviewEnabled
              ? const _DemoTodayCardRepository()
              : HttpTodayCardRepository()),
      cache: widget.todayCardCache ?? SharedPreferencesTodayCardCache(),
      onRevalidationRequired: () => _syncTodayCardAuthority(forceReload: true),
    )..addListener(_onTodayCardChanged);
    _todayCardAuthorityChanges = widget.todayCardAuthorityChanges ?? SharedPreferencesUtil.aiConsentAuthorityChanges;
    _todayCardAuthorityChanges.addListener(_onTodayCardAuthorityChanged);
    if (_guardianAvailable) {
      _loadWhisperState();
    }
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      unawaited(context.read<ConversationProvider>().ensureFreshConversations());
      unawaited(_loadHomeArtworkPreferences());
      _loadCaptureSourcePreference();
    });
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final nextProvider = Provider.of<EllaProvisioningProvider?>(context, listen: false);
    if (!identical(nextProvider, _provisioningProvider)) {
      _provisioningProvider?.removeListener(_onProvisioningChanged);
      _provisioningProvider = nextProvider;
      _provisioningProvider?.addListener(_onProvisioningChanged);
    }
    final nextConversationProvider = Provider.of<ConversationProvider>(context, listen: false);
    if (!identical(nextConversationProvider, _conversationProvider)) {
      _conversationProvider?.removeListener(_onConversationsChanged);
      _conversationProvider = nextConversationProvider;
      _conversationProvider?.addListener(_onConversationsChanged);
    }
    unawaited(_syncTodayCardAuthority());
  }

  TodayCardAuthoritySnapshot _captureTodayCardAuthority() {
    final injected = widget.todayCardAuthoritySnapshotProvider;
    if (injected != null) return injected();
    const isPreview = SharedPreferencesUtil.isTodayDesignPreviewEnabled;
    final uid = widget.todayCardUidOverride ?? (isPreview ? 'today-card-preview' : SharedPreferencesUtil().uid);
    final authority = isPreview || widget.todayCardUidOverride != null ? null : WalOwnerAuthority.active();
    final authorityKey = widget.todayCardAuthorityKeyOverride ??
        (widget.todayCardUidOverride != null
            ? 'test-authority:$uid'
            : isPreview
                ? 'today-card-preview'
                : authority?.uid == uid
                    ? TodayPage.cacheAuthorityKey(authority!)
                    : '');
    final isReady = widget.todayCardReadyOverride ??
        (isPreview || (_provisioningProvider?.isOperational == true && authority?.uid == uid));
    return (uid: uid, authorityKey: authorityKey, isProvisioningReady: isReady);
  }

  Future<void> _syncTodayCardAuthority({bool forceReload = false}) async {
    final snapshot = _captureTodayCardAuthority();
    await _todayCardController.updateAuthority(
      uid: snapshot.uid,
      authorityKey: snapshot.authorityKey,
      isProvisioningReady: snapshot.isProvisioningReady,
      forceReload: forceReload,
    );
  }

  void _onProvisioningChanged() {
    unawaited(_syncTodayCardAuthority());
    unawaited(_loadHomeArtworkPreferences());
    if (_guardianAvailable && !_whispersVerified) unawaited(_loadWhisperState());
  }

  void _onConversationsChanged() {
    if (!mounted || _homeMemorySort == MemoryGallerySort.recent) return;
    if (_conversationProvider?.hasMoreConversations == true) {
      setState(() => _homeMemorySort = MemoryGallerySort.recent);
    }
  }

  void _onTodayCardAuthorityChanged() {
    // In-memory content disappears synchronously; the exact replacement
    // authority is recaptured only after the old card is no longer renderable.
    _homeCaptureAuthorityGeneration++;
    final authorityGeneration = _homeCaptureAuthorityGeneration;
    _resumeNecklaceAfterPhoneCapture = null;
    _externalCaptureFinalizationSource = null;
    _todayCardController.invalidateAuthority();
    _homeArtworkBackfillPollTimer?.cancel();
    _homeArtworkQueuePollTimer?.cancel();
    _homeArtworkStyleOperationGeneration++;
    _homeArtworkQueueOperationGeneration++;
    final finalization = _homeCaptureFinalizationInFlight;
    if (finalization != null) {
      _abandonHomeCaptureAfterFinalization = true;
      unawaited(_joinHomeCaptureFinalization(finalization));
    } else if (_homeCaptureFinalizationPending && mounted) {
      setState(() {
        _homeCaptureFinalizationPending = false;
        _homeCaptureSource = null;
      });
    }
    if (mounted) {
      setState(() {
        // Never retain a previous account's mode while exact authority changes.
        _whispersOn = false;
        _whispersVerified = false;
        _homeMemoryLayout = MemoryGalleryLayout.journal;
        _homeMemorySort = MemoryGallerySort.recent;
        _homeArtworkPreferences = null;
        _homeArtworkBackfillState = _ArtworkBackfillUiState.idle;
        _homeArtworkStyleSaving = false;
        _homeArtworkQueueControlBusy = false;
        _homeArtworkQueueLoadState = _ArtworkQueueLoadState.idle;
        _homeArtworkQueueStatus = null;
        _homeArtworkDisplayEpoch++;
        _selectedCaptureSource = null;
      });
      _publishHomeArtworkStudioState();
    }
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || authorityGeneration != _homeCaptureAuthorityGeneration) return;
      final layout = _savedHomeMemoryLayout();
      if (layout != _homeMemoryLayout) setState(() => _homeMemoryLayout = layout);
      _loadCaptureSourcePreference();
    });
    if (_guardianAvailable) unawaited(_loadWhisperState());
    unawaited(_loadHomeArtworkPreferences());
    unawaited(_syncTodayCardAuthority(forceReload: true));
  }

  void _onTodayCardChanged() {
    if (mounted) setState(() {});
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      unawaited(_syncTodayCardAuthority(forceReload: true));
      if (_guardianAvailable) unawaited(_loadWhisperState());
      if (_homeArtworkPreferences?.releaseEnabled == true) {
        unawaited(_refreshHomeArtworkQueueStatus());
      }
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _provisioningProvider?.removeListener(_onProvisioningChanged);
    _conversationProvider?.removeListener(_onConversationsChanged);
    _todayCardAuthorityChanges.removeListener(_onTodayCardAuthorityChanged);
    _todayCardController
      ..removeListener(_onTodayCardChanged)
      ..dispose();
    _scrollController
      ..removeListener(_handleHomeScroll)
      ..dispose();
    _homeArtworkBackfillPollTimer?.cancel();
    _homeArtworkQueuePollTimer?.cancel();
    _homeArtworkStudioState.dispose();
    super.dispose();
  }

  void _loadHomeMemoryLayout() {
    _homeMemoryLayout = _savedHomeMemoryLayout();
  }

  MemoryGalleryLayout _savedHomeMemoryLayout() {
    final saved = SharedPreferencesUtil().memoryGalleryLayout;
    for (final layout in MemoryGalleryLayout.values) {
      if (layout.name == saved) {
        return layout;
      }
    }
    return MemoryGalleryLayout.journal;
  }

  Future<void> _selectHomeMemoryLayout(MemoryGalleryLayout layout) async {
    if (mounted) setState(() => _homeMemoryLayout = layout);
    await SharedPreferencesUtil().saveMemoryGalleryLayout(layout.name);
  }

  _HomeArtworkAuthoritySnapshot? _captureHomeArtworkAuthority() {
    final storage = SharedPreferencesUtil();
    final authority = _memoryArtworkAuthorityProvider();
    final uid = storage.uid.trim();
    final profileBindingId = storage.aiConsentProfileBindingId.trim();
    if (authority == null ||
        uid.isEmpty ||
        profileBindingId.isEmpty ||
        authority.uid != uid ||
        !authority.isExactCurrent()) {
      return null;
    }
    return (
      authority: authority,
      uid: uid,
      profileBindingId: profileBindingId,
      generation: storage.aiConsentAuthorityGeneration,
    );
  }

  bool _isHomeArtworkAuthorityCurrent(_HomeArtworkAuthoritySnapshot snapshot) {
    final storage = SharedPreferencesUtil();
    return snapshot.authority.isExactCurrent() &&
        storage.uid.trim() == snapshot.uid &&
        storage.aiConsentProfileBindingId.trim() == snapshot.profileBindingId &&
        storage.aiConsentAuthorityGeneration == snapshot.generation;
  }

  void _publishHomeArtworkStudioState() {
    final preferences = _homeArtworkPreferences;
    _homeArtworkStudioState.value = preferences == null
        ? null
        : (
            preferences: preferences,
            libraries: _homeArtworkLibraries,
            backfillState: _homeArtworkBackfillState,
            queueLoadState: _homeArtworkQueueLoadState,
            styleSaving: _homeArtworkStyleSaving,
            queueStatus: _homeArtworkQueueStatus,
            queueControlBusy: _homeArtworkQueueControlBusy,
          );
  }

  Future<void> _loadHomeArtworkPreferences() async {
    final authority = _captureHomeArtworkAuthority();
    if (authority == null) {
      if (mounted && (_homeArtworkPreferences != null || _homeArtworkStyleSaving)) {
        setState(() {
          _homeArtworkPreferences = null;
          _homeArtworkLibraries = null;
          _homeArtworkBackfillState = _ArtworkBackfillUiState.idle;
          _homeArtworkStyleSaving = false;
          _homeArtworkQueueControlBusy = false;
          _homeArtworkQueueLoadState = _ArtworkQueueLoadState.idle;
          _homeArtworkQueueStatus = null;
        });
        _publishHomeArtworkStudioState();
      }
      return;
    }
    MemoryArtworkPreferences? preferences;
    try {
      preferences = await _memoryArtworkApi.preferences();
    } on ExactAccountAuthorityChangedException {
      _scheduleHomeArtworkReload();
      return;
    }
    if (!mounted || !_isHomeArtworkAuthorityCurrent(authority)) return;
    setState(() {
      _homeArtworkPreferences = preferences;
      _homeArtworkBackfillState =
          preferences == null ? _ArtworkBackfillUiState.idle : _storedHomeArtworkBackfillState(preferences, authority);
    });
    _publishHomeArtworkStudioState();
    if (preferences?.releaseEnabled == true) {
      unawaited(_refreshHomeArtworkQueueStatus());
      unawaited(_refreshHomeArtworkLibraries());
      final savedCursor = SharedPreferencesUtil().memoryArtworkBackfillCursor(
        preferences!.styleVersion,
        expectedUid: authority.uid,
        expectedProfileBindingId: authority.profileBindingId,
        expectedAuthorityGeneration: authority.generation,
      );
      // Preference reloads happen after provisioning, refresh, and lifecycle
      // transitions. Only the first empty state may start a preview; later
      // reloads must not silently reset or extend the bounded batch.
      if (savedCursor.isEmpty) unawaited(_advanceHomeArtworkBackfill());
    }
  }

  Future<void> _refreshHomeArtworkLibraries() async {
    final authority = _captureHomeArtworkAuthority();
    if (authority == null || _homeArtworkPreferences?.releaseEnabled != true) return;
    final refreshSequence = ++_homeArtworkLibrariesRefreshSequence;
    MemoryArtworkLibraries? libraries;
    try {
      libraries = await _memoryArtworkApi.libraries();
    } on ExactAccountAuthorityChangedException {
      _scheduleHomeArtworkReload();
      return;
    } catch (_) {
      libraries = null;
    }
    if (!mounted ||
        !_isHomeArtworkAuthorityCurrent(authority) ||
        refreshSequence != _homeArtworkLibrariesRefreshSequence) {
      return;
    }
    setState(() => _homeArtworkLibraries = libraries);
    _publishHomeArtworkStudioState();
  }

  _ArtworkBackfillUiState _queueUiState(MemoryArtworkQueueStatus status) {
    if (status.state == MemoryArtworkQueueState.completed) {
      return _ArtworkBackfillUiState.complete;
    }
    if (status.state == MemoryArtworkQueueState.needsAttention || status.failed > 0) {
      return _ArtworkBackfillUiState.needsAttention;
    }
    if (status.controlState == MemoryArtworkQueueState.running) return _ArtworkBackfillUiState.running;
    return _ArtworkBackfillUiState.idle;
  }

  bool _shouldPollHomeArtworkQueue(MemoryArtworkQueueStatus status) {
    return status.controlState == MemoryArtworkQueueState.running &&
        (status.remaining > 0 || status.scanStatus != 'completed');
  }

  Future<void> _refreshHomeArtworkQueueStatus() async {
    final authority = _captureHomeArtworkAuthority();
    if (authority == null || _homeArtworkPreferences?.releaseEnabled != true) return;
    final operationGeneration = _homeArtworkQueueOperationGeneration;
    final refreshSequence = ++_homeArtworkQueueRefreshSequence;
    if (_homeArtworkQueueStatus == null && _homeArtworkQueueLoadState != _ArtworkQueueLoadState.loading) {
      setState(() => _homeArtworkQueueLoadState = _ArtworkQueueLoadState.loading);
      _publishHomeArtworkStudioState();
    }
    MemoryArtworkQueueStatus? status;
    try {
      status = await _memoryArtworkApi.queueStatus();
    } on ExactAccountAuthorityChangedException {
      _scheduleHomeArtworkReload();
      return;
    } catch (_) {
      status = null;
    }
    if (!mounted ||
        !_isHomeArtworkAuthorityCurrent(authority) ||
        operationGeneration != _homeArtworkQueueOperationGeneration ||
        refreshSequence != _homeArtworkQueueRefreshSequence) {
      return;
    }
    if (status == null) {
      final previous = _homeArtworkQueueStatus;
      setState(() => _homeArtworkQueueLoadState = _ArtworkQueueLoadState.failed);
      _publishHomeArtworkStudioState();
      if (previous != null && _shouldPollHomeArtworkQueue(previous)) {
        _scheduleHomeArtworkQueuePoll();
      }
      return;
    }
    final previous = _homeArtworkQueueStatus;
    final refreshVisibleArtwork = previous == null ||
        status.styleVersion != previous.styleVersion ||
        status.generationId != previous.generationId ||
        status.ready > previous.ready ||
        (status.state == MemoryArtworkQueueState.completed && previous.state != MemoryArtworkQueueState.completed);
    setState(() {
      _homeArtworkQueueStatus = status;
      _homeArtworkQueueLoadState = _ArtworkQueueLoadState.loaded;
      _homeArtworkBackfillState = _queueUiState(status!);
      if (refreshVisibleArtwork) _homeArtworkDisplayEpoch++;
    });
    _publishHomeArtworkStudioState();
    if (refreshVisibleArtwork) unawaited(_refreshHomeArtworkLibraries());
    if (_shouldPollHomeArtworkQueue(status)) {
      _scheduleHomeArtworkQueuePoll();
    } else {
      _homeArtworkQueuePollTimer?.cancel();
    }
  }

  void _scheduleHomeArtworkQueuePoll() {
    _homeArtworkQueuePollTimer?.cancel();
    _homeArtworkQueuePollTimer = Timer(const Duration(seconds: 4), () {
      if (mounted) unawaited(_refreshHomeArtworkQueueStatus());
    });
  }

  void _restoreHomeArtworkQueuePollIfNeeded() {
    final retained = _homeArtworkQueueStatus;
    if (retained != null && _shouldPollHomeArtworkQueue(retained)) {
      _scheduleHomeArtworkQueuePoll();
    }
  }

  Future<void> _controlHomeArtworkQueue(
    MemoryArtworkQueueAction action, {
    bool autoContinue = false,
    bool restartPreview = false,
  }) async {
    final authority = _captureHomeArtworkAuthority();
    final current = _homeArtworkQueueStatus;
    if (authority == null || current == null || _homeArtworkQueueControlBusy) return;
    _homeArtworkQueuePollTimer?.cancel();
    final operationGeneration = ++_homeArtworkQueueOperationGeneration;
    setState(() => _homeArtworkQueueControlBusy = true);
    _publishHomeArtworkStudioState();
    MemoryArtworkQueueStatus? updated;
    try {
      updated = await _memoryArtworkApi.controlQueue(
        action: action,
        generationId: current.generationId,
        autoContinue: autoContinue,
      );
    } on ExactAccountAuthorityChangedException {
      _scheduleHomeArtworkReload();
    } catch (_) {
      updated = null;
    } finally {
      if (mounted &&
          _isHomeArtworkAuthorityCurrent(authority) &&
          operationGeneration == _homeArtworkQueueOperationGeneration) {
        setState(() => _homeArtworkQueueControlBusy = false);
        _publishHomeArtworkStudioState();
      }
    }
    if (!mounted ||
        !_isHomeArtworkAuthorityCurrent(authority) ||
        operationGeneration != _homeArtworkQueueOperationGeneration) {
      return;
    }
    if (updated == null) {
      _showHomeMessage(context.l10n.memoryArtworkQueueControlFailed);
      _restoreHomeArtworkQueuePollIfNeeded();
      return;
    }
    setState(() {
      _homeArtworkQueueStatus = updated;
      _homeArtworkQueueLoadState = _ArtworkQueueLoadState.loaded;
      _homeArtworkBackfillState = _queueUiState(updated!);
    });
    _publishHomeArtworkStudioState();
    if (_shouldPollHomeArtworkQueue(updated)) {
      if (action == MemoryArtworkQueueAction.resume) {
        unawaited(
          _advanceHomeArtworkBackfill(
            restart: restartPreview || autoContinue,
            mode: autoContinue || !restartPreview ? MemoryArtworkBackfillMode.all : MemoryArtworkBackfillMode.preview,
            waitForActive: !restartPreview && !autoContinue,
          ),
        );
      }
      _scheduleHomeArtworkQueuePoll();
    } else {
      _homeArtworkBackfillPollTimer?.cancel();
      _homeArtworkQueuePollTimer?.cancel();
    }
  }

  Future<void> _confirmStopHomeArtworkQueue() async {
    final shouldStop = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(context.l10n.memoryArtworkQueueStopTitle),
        content: Text(context.l10n.memoryArtworkQueueStopDetail),
        actions: [
          TextButton(onPressed: () => Navigator.pop(dialogContext, false), child: Text(context.l10n.cancel)),
          FilledButton(
            key: const Key('home-artwork-confirm-stop'),
            onPressed: () => Navigator.pop(dialogContext, true),
            child: Text(context.l10n.memoryArtworkQueueStop),
          ),
        ],
      ),
    );
    if (shouldStop == true) await _controlHomeArtworkQueue(MemoryArtworkQueueAction.cancel);
  }

  _ArtworkBackfillUiState _storedHomeArtworkBackfillState(
    MemoryArtworkPreferences preferences,
    _HomeArtworkAuthoritySnapshot authority,
  ) {
    final cursor = SharedPreferencesUtil().memoryArtworkBackfillCursor(
      preferences.styleVersion,
      expectedUid: authority.uid,
      expectedProfileBindingId: authority.profileBindingId,
      expectedAuthorityGeneration: authority.generation,
    );
    return cursor == _artworkBackfillComplete ? _ArtworkBackfillUiState.complete : _ArtworkBackfillUiState.idle;
  }

  void _scheduleHomeArtworkReload() {
    if (!mounted || _homeArtworkReloadScheduled) return;
    _homeArtworkReloadScheduled = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _homeArtworkReloadScheduled = false;
      if (mounted) unawaited(_loadHomeArtworkPreferences());
    });
  }

  Future<MemoryArtworkBackfillPage?> _advanceHomeArtworkBackfill({
    bool restart = false,
    MemoryArtworkBackfillMode mode = MemoryArtworkBackfillMode.preview,
    bool waitForActive = false,
  }) async {
    while (true) {
      final preferences = _homeArtworkPreferences;
      final authority = _captureHomeArtworkAuthority();
      if (preferences == null || !preferences.releaseEnabled || authority == null) return null;
      final queue = _homeArtworkQueueStatus;
      if (queue != null && queue.controlState != MemoryArtworkQueueState.running) return null;
      final active = _homeArtworkBackfillInFlight;
      if (active == null) {
        return _startHomeArtworkBackfill(preferences: preferences, authority: authority, restart: restart, mode: mode);
      }
      final activeAuthority = _homeArtworkBackfillAuthority;
      if (!restart && !waitForActive && activeAuthority != null && _isHomeArtworkAuthorityCurrent(activeAuthority)) {
        return null;
      }
      await active;
    }
  }

  Future<MemoryArtworkBackfillPage?> _startHomeArtworkBackfill({
    required MemoryArtworkPreferences preferences,
    required _HomeArtworkAuthoritySnapshot authority,
    required bool restart,
    required MemoryArtworkBackfillMode mode,
  }) {
    if (mounted && _isHomeArtworkAuthorityCurrent(authority)) {
      setState(() => _homeArtworkBackfillState = _ArtworkBackfillUiState.running);
      _publishHomeArtworkStudioState();
    }
    final rawOperation = _runHomeArtworkBackfill(
      preferences: preferences,
      authority: authority,
      restart: restart,
      mode: mode,
    );
    late final Future<MemoryArtworkBackfillPage?> operation;
    operation = rawOperation.then((page) {
      if (mounted && _isHomeArtworkAuthorityCurrent(authority)) {
        final storedState = _storedHomeArtworkBackfillState(preferences, authority);
        setState(() {
          _homeArtworkBackfillState = storedState == _ArtworkBackfillUiState.complete
              ? storedState
              : page == null
                  ? _ArtworkBackfillUiState.needsAttention
                  : _ArtworkBackfillUiState.idle;
        });
        _publishHomeArtworkStudioState();
        unawaited(_refreshHomeArtworkQueueStatus());
        // Each call queues one bounded preview. Older history advances only
        // after a deliberate user action, never from a background timer.
        _homeArtworkBackfillPollTimer?.cancel();
      } else if (mounted && identical(_homeArtworkBackfillInFlight, operation)) {
        setState(() => _homeArtworkBackfillState = _ArtworkBackfillUiState.idle);
        _publishHomeArtworkStudioState();
      }
      return page;
    }).whenComplete(() {
      if (identical(_homeArtworkBackfillInFlight, operation)) {
        _homeArtworkBackfillInFlight = null;
        _homeArtworkBackfillAuthority = null;
      }
    });
    _homeArtworkBackfillAuthority = authority;
    _homeArtworkBackfillInFlight = operation;
    return operation;
  }

  Future<MemoryArtworkBackfillPage?> _runHomeArtworkBackfill({
    required MemoryArtworkPreferences preferences,
    required _HomeArtworkAuthoritySnapshot authority,
    required bool restart,
    required MemoryArtworkBackfillMode mode,
  }) async {
    try {
      final storage = SharedPreferencesUtil();
      if (restart) {
        await storage.clearMemoryArtworkBackfillCursor(
          preferences.styleVersion,
          expectedUid: authority.uid,
          expectedProfileBindingId: authority.profileBindingId,
          expectedAuthorityGeneration: authority.generation,
        );
        if (!_isHomeArtworkAuthorityCurrent(authority)) return null;
      }
      final savedCursor = storage.memoryArtworkBackfillCursor(
        preferences.styleVersion,
        expectedUid: authority.uid,
        expectedProfileBindingId: authority.profileBindingId,
        expectedAuthorityGeneration: authority.generation,
      );
      if (savedCursor == _artworkBackfillComplete) return null;
      final page = await _memoryArtworkApi.backfillNext(cursor: savedCursor.isEmpty ? null : savedCursor, mode: mode);
      if (!_isHomeArtworkAuthorityCurrent(authority)) return null;
      if (page == null) {
        if (savedCursor.isNotEmpty) {
          await storage.clearMemoryArtworkBackfillCursor(
            preferences.styleVersion,
            expectedUid: authority.uid,
            expectedProfileBindingId: authority.profileBindingId,
            expectedAuthorityGeneration: authority.generation,
          );
        }
        return null;
      }
      final nextCursor = page.nextCursor?.trim() ?? '';
      if (page.hasMore && nextCursor.isEmpty) {
        await storage.clearMemoryArtworkBackfillCursor(
          preferences.styleVersion,
          expectedUid: authority.uid,
          expectedProfileBindingId: authority.profileBindingId,
          expectedAuthorityGeneration: authority.generation,
        );
        return null;
      }
      final committed = await storage.saveMemoryArtworkBackfillCursor(
        preferences.styleVersion,
        page.hasMore ? nextCursor : _artworkBackfillComplete,
        expectedUid: authority.uid,
        expectedProfileBindingId: authority.profileBindingId,
        expectedAuthorityGeneration: authority.generation,
      );
      return committed && _isHomeArtworkAuthorityCurrent(authority) ? page : null;
    } on ExactAccountAuthorityChangedException {
      _scheduleHomeArtworkReload();
      return null;
    } catch (_) {
      return null;
    }
  }

  Future<void> _selectHomeArtworkStyle(String styleVersion) async {
    final preferences = _homeArtworkPreferences;
    final authority = _captureHomeArtworkAuthority();
    if (preferences == null || !preferences.releaseEnabled || authority == null) {
      _showHomeMessage(context.l10n.memoryArtworkStyleUnavailable);
      return;
    }
    if (_homeArtworkStyleSaving) return;
    final operationGeneration = ++_homeArtworkStyleOperationGeneration;
    _homeArtworkQueueOperationGeneration++;
    _homeArtworkQueuePollTimer?.cancel();
    setState(() => _homeArtworkStyleSaving = true);
    _publishHomeArtworkStudioState();
    MemoryArtworkPreferenceUpdate result;
    try {
      result = await _memoryArtworkApi.setStyle(consentVersion: preferences.consentVersion, styleVersion: styleVersion);
    } on ExactAccountAuthorityChangedException {
      _scheduleHomeArtworkReload();
      return;
    } catch (_) {
      result = const MemoryArtworkPreferenceUpdate(saved: false);
    } finally {
      if (mounted && operationGeneration == _homeArtworkStyleOperationGeneration) {
        setState(() => _homeArtworkStyleSaving = false);
        _publishHomeArtworkStudioState();
      }
    }
    if (!mounted || !_isHomeArtworkAuthorityCurrent(authority)) return;
    if (!result.saved) {
      _showHomeMessage(context.l10n.memoryArtworkStyleUnavailable);
      _restoreHomeArtworkQueuePollIfNeeded();
      return;
    }
    setState(() {
      _homeArtworkPreferences = MemoryArtworkPreferences(
        consent: 'accepted',
        consentVersion: preferences.consentVersion,
        styleVersion: styleVersion,
        releaseEnabled: true,
      );
      // The saved style starts a new generation; never let a paused/stopped
      // status from the previous style suppress its first reconciliation.
      _homeArtworkQueueStatus = null;
      _homeArtworkLibraries = null;
      _homeArtworkQueueLoadState = _ArtworkQueueLoadState.loading;
    });
    _publishHomeArtworkStudioState();
    _showHomeMessage(context.l10n.memoryArtworkStyleUpdated);
    unawaited(_refreshHomeArtworkQueueStatus());
    unawaited(_refreshHomeArtworkLibraries());
    unawaited(_advanceHomeArtworkBackfill(restart: true));
  }

  void _openHomeArtworkStudio() {
    final preferences = _homeArtworkPreferences;
    if (preferences == null || !preferences.releaseEnabled) {
      _showHomeMessage(context.l10n.memoryArtworkStyleUnavailable);
      return;
    }
    unawaited(_refreshHomeArtworkLibraries());
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      backgroundColor: EllaColors.paper,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(EllaSizes.cardRadius)),
      ),
      builder: (sheetContext) => ValueListenableBuilder<_ArtworkStudioSnapshot?>(
        valueListenable: _homeArtworkStudioState,
        builder: (context, snapshot, _) {
          if (snapshot == null) {
            WidgetsBinding.instance.addPostFrameCallback((_) {
              if (sheetContext.mounted && Navigator.of(sheetContext).canPop()) Navigator.pop(sheetContext);
            });
            return const SizedBox.shrink();
          }
          final current = snapshot;
          return _ArtworkStudioSheet(
            preferences: current.preferences,
            libraries: current.libraries,
            backfillState: current.backfillState,
            queueLoadState: current.queueLoadState,
            styleSaving: current.styleSaving,
            queueStatus: current.queueStatus,
            queueControlBusy: current.queueControlBusy,
            onStyleSelected: (styleVersion) => unawaited(_selectHomeArtworkStyle(styleVersion)),
            onContinue: () {
              if (current.queueStatus == null) {
                unawaited(_advanceHomeArtworkBackfill(restart: true));
              } else {
                unawaited(_controlHomeArtworkQueue(MemoryArtworkQueueAction.resume));
              }
            },
            onRetryStatus: () => unawaited(_refreshHomeArtworkQueueStatus()),
            onPause: () => unawaited(_controlHomeArtworkQueue(MemoryArtworkQueueAction.pause)),
            onResume: () => unawaited(_controlHomeArtworkQueue(MemoryArtworkQueueAction.resume)),
            onStop: () => unawaited(_confirmStopHomeArtworkQueue()),
          );
        },
      ),
    );
  }

  void _showHomeMessage(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  void _handleHomeScroll() {
    if (!_scrollController.hasClients) return;
    final position = _scrollController.position;
    final shouldShow = position.pixels > 640;
    if (shouldShow != _showBackToRecent && mounted) setState(() => _showBackToRecent = shouldShow);
    if (position.extentAfter < 720) {
      final provider = context.read<ConversationProvider>();
      if (provider.hasLoadedConversations &&
          provider.hasMoreConversations &&
          !provider.isLoadingConversations &&
          !provider.isLoadingMoreConversations &&
          !provider.loadMoreConversationsFailed) {
        unawaited(provider.getMoreConversationsFromServer());
      }
    }
  }

  void _scheduleHomeMemoryPrefetch() {
    if (_homeMemoryPrefetchScheduled) return;
    _homeMemoryPrefetchScheduled = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _homeMemoryPrefetchScheduled = false;
      if (mounted) _handleHomeScroll();
    });
  }

  void _scrollHomeBackToRecent() {
    if (_homeMemorySort != MemoryGallerySort.recent && mounted) {
      setState(() => _homeMemorySort = MemoryGallerySort.recent);
    }
    if (!_scrollController.hasClients) return;
    _scrollController.animateTo(0, duration: const Duration(milliseconds: 320), curve: Curves.easeOutCubic);
  }

  Future<void> _loadWhisperState() async {
    if (!_guardianAvailable) return;
    if (_whisperStateLoading) {
      _whisperStateReloadPending = true;
      return;
    }
    _whisperStateLoading = true;
    try {
      do {
        _whisperStateReloadPending = false;
        await _loadWhisperStateOnce();
      } while (_whisperStateReloadPending && mounted && _guardianAvailable);
    } finally {
      _whisperStateLoading = false;
    }
  }

  Future<void> _loadWhisperStateOnce() async {
    final info = await _readWhisperState();
    if (info == null) {
      try {
        await _reconcileWhisperNative(false);
      } catch (_) {}
      if (!mounted) return;
      setState(() {
        _whispersOn = false;
        _whispersVerified = false;
      });
      return;
    }
    if (!mounted) return;
    final serverEnabled = _whispersEnabled(info);
    var resolvedEnabled = serverEnabled;
    var resolvedVerified = false;
    try {
      await _reconcileWhisperNative(serverEnabled);
      resolvedVerified = true;
    } catch (_) {
      // If native capture cannot match an authoritative ON response, disable
      // the server mode only when both the write and readback confirm OFF.
      // Otherwise keep the last authoritative value behind an unavailable
      // control and make no ON/OFF claim.
      if (serverEnabled && await _writeWhisperState(const GuardianModeState())) {
        final confirmed = await _readWhisperState();
        if (confirmed != null && !_whispersEnabled(confirmed)) {
          resolvedEnabled = false;
          try {
            await _reconcileWhisperNative(false);
            resolvedVerified = true;
          } catch (_) {}
        }
      }
    }
    if (!mounted) return;
    setState(() {
      _whispersOn = resolvedEnabled;
      _whispersVerified = resolvedVerified;
    });
  }

  Future<GuardianModeInfo?> _readWhisperState() async {
    try {
      final loader = widget.guardianModeLoader;
      if (loader != null) return loader();
      final result = await guardian_api.getGuardianMode();
      return result.isSuccess ? result.value : null;
    } catch (_) {
      return null;
    }
  }

  bool _whispersEnabled(GuardianModeInfo info) =>
      !(info.twoTierState?.isOff ?? info.currentMode == GuardianModeKey.off);

  Future<void> _reconcileWhisperNative(bool enabled) => enabled
      ? (widget.guardianNativeStart?.call() ?? guardian_native.GuardianModeService().start())
      : (widget.guardianNativeStop?.call() ?? guardian_native.GuardianModeService().stop());

  Future<bool> _writeWhisperState(GuardianModeState state) async {
    try {
      final setter = widget.guardianModeSetter;
      if (setter != null) return setter(state);
      return (await guardian_api.setGuardianModeTwoTier(state)).isSuccess;
    } catch (_) {
      return false;
    }
  }

  void scrollToTop() {
    if (!_scrollController.hasClients) return;
    _scrollController.animateTo(0, duration: const Duration(milliseconds: 200), curve: Curves.easeOut);
  }

  Future<void> _setWhispers(bool enabled) async {
    if (_updatingWhispers || !_guardianAvailable) return;
    final previousEnabled = _whispersOn;
    setState(() {
      _whispersOn = enabled;
      _updatingWhispers = true;
    });
    final state = enabled ? const GuardianModeState(features: ['ACTIVE_SUPPORT']) : const GuardianModeState();
    var success = false;
    try {
      if (!enabled) {
        await (widget.guardianNativeStop?.call() ?? guardian_native.GuardianModeService().stop());
      }
      success = await _writeWhisperState(state);
      if (success && enabled) {
        await (widget.guardianNativeStart?.call() ?? guardian_native.GuardianModeService().start());
      }
    } catch (_) {
      success = false;
    }
    if (!success && enabled) {
      try {
        await (widget.guardianNativeStop?.call() ?? guardian_native.GuardianModeService().stop());
        await _writeWhisperState(const GuardianModeState());
      } catch (_) {}
    }
    var resolvedEnabled = enabled;
    var resolvedVerified = success;
    if (!success) {
      final authoritative = await _readWhisperState();
      if (authoritative != null) {
        resolvedEnabled = _whispersEnabled(authoritative);
        try {
          await _reconcileWhisperNative(resolvedEnabled);
          resolvedVerified = true;
        } catch (_) {
          resolvedVerified = false;
        }
      } else {
        // The write may have reached the server even though the response did
        // not. Keep the last verified display value but mark it unavailable;
        // never claim OFF (or ON) without an authoritative readback.
        resolvedEnabled = previousEnabled;
        resolvedVerified = false;
        try {
          await _reconcileWhisperNative(previousEnabled);
        } catch (_) {}
      }
    }
    if (!mounted) return;
    setState(() {
      _updatingWhispers = false;
      _whispersOn = resolvedEnabled;
      _whispersVerified = resolvedVerified;
    });
    if (!success) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.anErrorOccurredTryAgain)));
    }
  }

  Future<void> _openTodayCardTalk() async {
    await _syncTodayCardAuthority();
    if (!mounted) return;
    final card = _todayCardController.state.card;
    if (card == null) return;
    final routeOpener = widget.todayCardTalkRouteOpener;
    if (routeOpener != null) {
      await routeOpener(context, card);
      return;
    }
    if (!mounted) return;
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      isDismissible: false,
      enableDrag: false,
      useSafeArea: true,
      backgroundColor: EllaColors.bgPrimary,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(28))),
      clipBehavior: Clip.antiAlias,
      builder: (_) => FractionallySizedBox(
        heightFactor: 0.94,
        child: EllaVoiceChatPage(
          sessionScope: TodayPage.sessionScopeFor(card),
          memoryTitle: card.headline,
          modalPresentation: true,
        ),
      ),
    );
  }

  Future<void> _openTodayCardDetail() async {
    await _syncTodayCardAuthority();
    if (!mounted) return;
    final card = _todayCardController.state.card;
    if (card == null) return;
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: EllaColors.bgPrimary,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(28))),
      clipBehavior: Clip.antiAlias,
      builder: (sheetContext) => FractionallySizedBox(
        heightFactor: 0.82,
        child: _TodayCardDetailSheet(
          card: card,
          onTalk: () {
            Navigator.of(sheetContext).pop();
            unawaited(_openTodayCardTalk());
          },
        ),
      ),
    );
  }

  String _phoneCaptureFailureMessage(PhoneCaptureStartResult result) => switch (result) {
        PhoneCaptureStartResult.microphonePermissionDenied => context.l10n.todayMicrophonePermissionDenied,
        PhoneCaptureStartResult.transcriptionUnavailable => context.l10n.todayTranscriptionUnavailable,
        PhoneCaptureStartResult.accountNotReady ||
        PhoneCaptureStartResult.consentUnavailable ||
        PhoneCaptureStartResult.recorderUnavailable ||
        PhoneCaptureStartResult.cancelled =>
          context.l10n.todayRecordingUnavailable,
        PhoneCaptureStartResult.started => context.l10n.todayRecordingUnavailable,
      };

  Future<bool> _finalizeHomeMoment(CaptureProvider capture, {bool Function()? isCurrent}) async {
    final finalized = capture.recordingState == RecordingState.deviceRecord
        ? await capture.finalizeCurrentDeviceConversationAndContinue()
        : await capture.finalizeCurrentConversation();
    if (isCurrent != null && !isCurrent()) return false;
    if (finalized) return true;
    if (!mounted) return false;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.todayNoWordsCaptured)));
    return false;
  }

  Future<void> _joinHomeCaptureFinalization(Future<bool> operation) async {
    try {
      await operation;
    } catch (_) {
      // The initiating UI reports the failure. This join exists only to keep
      // old capture work behind the transition/route barrier until it settles.
    }
  }

  Future<bool> _finishHomeCapture(CaptureProvider capture) {
    final existing = _homeCaptureFinalizationInFlight;
    if (existing != null) return existing;

    final completer = Completer<bool>();
    final operation = completer.future;
    _homeCaptureFinalizationInFlight = operation;
    if (mounted) setState(() {});
    unawaited(_runHomeCaptureFinalization(capture, operation, completer));
    return operation;
  }

  Future<void> _runHomeCaptureFinalization(
    CaptureProvider capture,
    Future<bool> operation,
    Completer<bool> completer,
  ) async {
    Object? error;
    StackTrace? stackTrace;
    var result = false;
    try {
      result = await _finishHomeCaptureOnce(capture);
    } catch (caughtError, caughtStackTrace) {
      error = caughtError;
      stackTrace = caughtStackTrace;
    }

    if (identical(_homeCaptureFinalizationInFlight, operation)) {
      _homeCaptureFinalizationInFlight = null;
      if (_abandonHomeCaptureAfterFinalization) {
        _abandonHomeCaptureAfterFinalization = false;
      }
      if (mounted) setState(() {});
    }

    if (error != null) {
      completer.completeError(error, stackTrace!);
    } else {
      completer.complete(result);
    }
  }

  Future<bool> _finishHomeCaptureOnce(CaptureProvider capture) async {
    final authorityGeneration = _homeCaptureAuthorityGeneration;
    if (_homeCaptureFinalizationPending) {
      final source = _homeCaptureSource;
      final finalized = await _finalizeHomeMoment(
        capture,
        isCurrent: () => authorityGeneration == _homeCaptureAuthorityGeneration,
      );
      final isCurrent = authorityGeneration == _homeCaptureAuthorityGeneration;
      if (finalized && isCurrent && mounted) {
        if (source == _HomeCaptureSource.phone) {
          await _resumeAmbientNecklace(capture);
        }
        if (!mounted || authorityGeneration != _homeCaptureAuthorityGeneration) return false;
        setState(() {
          _homeCaptureFinalizationPending = false;
          _homeCaptureSource = null;
        });
      }
      return finalized && isCurrent;
    }

    final source = _homeCaptureSource;
    bool? transportFinalized;
    switch (source) {
      case _HomeCaptureSource.phone:
        transportFinalized = await capture.stopStreamRecordingAndFinalize();
        break;
      case _HomeCaptureSource.necklaceOwned:
        transportFinalized = await capture.stopStreamDeviceRecordingAndFinalize();
        if (capture.recordingState == RecordingState.deviceRecord) {
          throw StateError('Home-owned necklace stream did not stop');
        }
        break;
      case _HomeCaptureSource.necklaceContinuous:
        break;
      case null:
        if (capture.recordingState == RecordingState.record) {
          transportFinalized = await capture.stopStreamRecordingAndFinalize();
        }
        break;
    }
    if (authorityGeneration != _homeCaptureAuthorityGeneration) {
      if (mounted) {
        setState(() {
          _homeCaptureActive = false;
          _homeCaptureFinalizationPending = false;
          _homeCaptureSource = null;
        });
      }
      return false;
    }
    if (mounted) {
      setState(() {
        _homeCaptureActive = false;
        if (source == _HomeCaptureSource.phone || source == _HomeCaptureSource.necklaceOwned) {
          _homeCaptureFinalizationPending = true;
        } else {
          _homeCaptureSource = null;
        }
      });
    }
    if (transportFinalized != null) {
      if (transportFinalized) {
        if (source == _HomeCaptureSource.phone) {
          await _resumeAmbientNecklace(capture);
        }
        if (!mounted || authorityGeneration != _homeCaptureAuthorityGeneration) return false;
        if (mounted) {
          setState(() {
            _homeCaptureFinalizationPending = false;
            _homeCaptureSource = null;
          });
        }
        return true;
      }
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.todayNoWordsCaptured)));
      }
      return false;
    }
    final finalized = await _finalizeHomeMoment(
      capture,
      isCurrent: () => authorityGeneration == _homeCaptureAuthorityGeneration,
    );
    final isCurrent = authorityGeneration == _homeCaptureAuthorityGeneration;
    if (finalized && isCurrent && mounted) {
      setState(() {
        _homeCaptureFinalizationPending = false;
        _homeCaptureSource = null;
      });
    }
    return finalized && isCurrent;
  }

  Future<void> _resumeAmbientNecklace(CaptureProvider capture) async {
    final device = _resumeNecklaceAfterPhoneCapture;
    _resumeNecklaceAfterPhoneCapture = null;
    if (device == null || !mounted) return;
    final currentDevice = context.read<DeviceProvider>().presentationConnectedDevice;
    if (currentDevice?.id != device.id) return;
    try {
      await capture.streamDeviceRecording(device: currentDevice);
    } catch (_) {
      // The phone-owned moment is already finalized. Necklace recovery remains
      // visible through device status and must not turn that successful action
      // into a false phone-recording failure.
    }
  }

  Future<void> _openLiveTranscript(CaptureProvider capture) async {
    final selectedSource = todaySelectedCaptureSource(
      state: capture.recordingState,
      diagnostics: capture.captureDiagnostics,
      preferredSource: _selectedCaptureSource,
    );
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => ConversationCapturingPage(
          preferredCaptureSource: selectedSource,
          onProcessNow: () async {
            if (_externalCaptureFinalizationSource != null) {
              await _finishExternalCapture(capture);
              return _externalCaptureFinalizationSource == null;
            }
            if (_homeCaptureActive || _homeCaptureFinalizationPending || _homeCaptureFinalizationInFlight != null) {
              return _finishHomeCapture(capture);
            }
            if (capture.recordingState == RecordingState.deviceRecord) {
              return _finalizeHomeMoment(capture);
            }
            if (capture.phoneCaptureOwnsMobileAudio || capture.recordingState == RecordingState.record) {
              await _finishExternalCapture(capture);
              return _externalCaptureFinalizationSource == null;
            }
            return false;
          },
        ),
      ),
    );
    final finalization = _homeCaptureFinalizationInFlight;
    if (finalization != null) {
      _abandonHomeCaptureAfterFinalization = true;
      await _joinHomeCaptureFinalization(finalization);
    }
  }

  Future<void> _finishExternalCapture(CaptureProvider capture) async {
    if (!mounted || _homeCaptureStarting) return;
    final existingSource = _externalCaptureFinalizationSource;
    final source = existingSource ??
        switch (capture.recordingState) {
          RecordingState.record => _ExternalCaptureSource.phone,
          RecordingState.deviceRecord => _ExternalCaptureSource.necklace,
          _ => null,
        };
    if (source == null) return;
    setState(() => _homeCaptureStarting = true);
    try {
      if (existingSource == null && mounted) {
        setState(() => _externalCaptureFinalizationSource = source);
      }

      var finished = false;
      var confirmedEmpty = false;
      if (existingSource != null) {
        finished = await capture.finalizeCurrentConversation(closeTranscriptTransportBeforeProcessing: true);
        if (!finished && !capture.captureDiagnostics.hasPhysicalAudio && !capture.hasCapturableContent) {
          confirmedEmpty = !await capture.awaitFinalCapturableContent();
        }
      } else if (source == _ExternalCaptureSource.phone) {
        final result = await capture.stopPhoneCaptureForVoiceTakeover();
        finished = result == PhoneCaptureStopResult.finalized;
        confirmedEmpty = result == PhoneCaptureStopResult.empty;
      } else {
        final hadCaptureEvidence = capture.captureDiagnostics.hasPhysicalAudio || capture.hasCapturableContent;
        finished = await capture.stopStreamDeviceRecordingAndFinalize();
        if (!finished && !hadCaptureEvidence && !capture.captureDiagnostics.hasPhysicalAudio) {
          confirmedEmpty = !await capture.awaitFinalCapturableContent();
        }
      }

      if ((finished || confirmedEmpty) && mounted) {
        setState(() => _externalCaptureFinalizationSource = null);
      }
      if (!finished && mounted) {
        final message = confirmedEmpty ? context.l10n.todayNoWordsCaptured : context.l10n.todayRecordingUnavailable;
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
      }
    } catch (_) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.todayRecordingUnavailable)));
    } finally {
      if (mounted) setState(() => _homeCaptureStarting = false);
    }
  }

  void _loadCaptureSourcePreference() {
    final saved = EllaCaptureSource.fromStorage(SharedPreferencesUtil().ellaCaptureSource);
    if (!mounted || saved == null || saved == _selectedCaptureSource) return;
    setState(() => _selectedCaptureSource = saved);
  }

  void _selectCaptureSource(EllaCaptureSource source) {
    if (_selectedCaptureSource == source) return;
    setState(() => _selectedCaptureSource = source);
    unawaited(SharedPreferencesUtil().saveEllaCaptureSource(source.name));
  }

  Future<void> _reconnectNecklaceCapture(DeviceProvider device) async {
    if (_homeCaptureStarting) return;
    setState(() => _homeCaptureStarting = true);
    try {
      final started = await device.reconnectKnownDeviceForCapture(reason: 'Home necklace capture');
      if (!started && mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.todayRecordingUnavailable)));
      }
    } catch (_) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.todayRecordingUnavailable)));
    } finally {
      if (mounted) setState(() => _homeCaptureStarting = false);
    }
  }

  Future<void> _saveNecklaceMoment(CaptureProvider capture) async {
    if (_homeCaptureStarting) return;
    setState(() => _homeCaptureStarting = true);
    try {
      final saved = await capture.finalizeCurrentDeviceConversationAndContinue();
      if (!saved && mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.todayNoWordsCaptured)));
      }
    } catch (_) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.todayRecordingUnavailable)));
    } finally {
      if (mounted) setState(() => _homeCaptureStarting = false);
    }
  }

  Future<void> _handleCapturePrimaryAction({
    required EllaCaptureSource selectedSource,
    required CaptureProvider capture,
    required DeviceProvider device,
    required bool homeCaptureOwned,
    required bool legacyNecklaceNeedsConfirmation,
  }) async {
    if (_homeCaptureStarting) return;
    if (_externalCaptureFinalizationSource != null) {
      await _finishExternalCapture(capture);
      return;
    }
    if (capture.recordingState == RecordingState.record) {
      if (homeCaptureOwned) {
        await _finishHomeCapture(capture);
      } else {
        await _finishExternalCapture(capture);
      }
      return;
    }
    if (capture.recordingState == RecordingState.deviceRecord && selectedSource == EllaCaptureSource.necklace) {
      await _saveNecklaceMoment(capture);
      return;
    }
    if (selectedSource == EllaCaptureSource.phone) {
      await _toggleHomeCapture(
        capture: capture,
        isActive: homeCaptureOwned,
        necklaceConnected: device.presentationIsConnected,
        connectedDevice: device.presentationConnectedDevice,
      );
      return;
    }
    if (legacyNecklaceNeedsConfirmation) {
      await _confirmLegacyNecklace(device);
      return;
    }
    await _reconnectNecklaceCapture(device);
  }

  Future<void> _toggleHomeCapture({
    required CaptureProvider capture,
    required bool isActive,
    required bool necklaceConnected,
    required BtDevice? connectedDevice,
  }) async {
    if (_homeCaptureStarting) return;
    setState(() => _homeCaptureStarting = true);
    try {
      final finalization = _homeCaptureFinalizationInFlight;
      if (finalization != null) {
        await _joinHomeCaptureFinalization(finalization);
        return;
      }
      if (_homeCaptureFinalizationPending) {
        await _finishHomeCapture(capture);
        return;
      }
      if (isActive) {
        await _finishHomeCapture(capture);
        return;
      }

      // Home's explicit Record action always owns the iPhone microphone. If
      // ambient necklace capture is active (or failed mid-session), close that
      // exact transport first and restore it only after the phone moment ends.
      final necklaceTransportState = capture.recordingState == RecordingState.deviceRecord ||
          capture.recordingState == RecordingState.initialising ||
          capture.recordingState == RecordingState.pause ||
          capture.recordingState == RecordingState.error;
      final necklaceTransportOwned =
          necklaceTransportState && (capture.havingRecordingDevice || necklaceConnected || connectedDevice != null);
      if (necklaceTransportOwned) {
        final shouldResumeAmbient =
            capture.recordingState == RecordingState.deviceRecord || capture.recordingState == RecordingState.pause;
        if (shouldResumeAmbient && necklaceConnected && connectedDevice != null) {
          _resumeNecklaceAfterPhoneCapture = connectedDevice;
        }
        if (capture.recordingState == RecordingState.deviceRecord || capture.recordingState == RecordingState.pause) {
          final hadCapturableContent = capture.captureDiagnostics.hasPhysicalAudio || capture.hasCapturableContent;
          final finalized = await capture.stopStreamDeviceRecordingAndFinalize();
          if (hadCapturableContent && !finalized) {
            if (!mounted) return;
            setState(() {
              _homeCaptureActive = false;
              _homeCaptureFinalizationPending = true;
              _homeCaptureSource = _HomeCaptureSource.necklaceOwned;
            });
            ScaffoldMessenger.of(
              context,
            ).showSnackBar(SnackBar(content: Text(context.l10n.todayRecordingUnavailable)));
            return;
          }
        } else {
          await capture.stopStreamDeviceRecording();
        }
        if (!mounted) return;
      }

      final result = await capture.streamRecording();
      if (!mounted) return;
      final started = result == PhoneCaptureStartResult.started && capture.recordingState == RecordingState.record;
      setState(() {
        _homeCaptureActive = started;
        _homeCaptureSource = started ? _HomeCaptureSource.phone : null;
      });
      if (!started) {
        await _resumeAmbientNecklace(capture);
        if (!mounted) return;
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(_phoneCaptureFailureMessage(result))));
      }
    } catch (_) {
      if (!mounted) return;
      await _resumeAmbientNecklace(capture);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.todayRecordingUnavailable)));
    } finally {
      if (mounted) setState(() => _homeCaptureStarting = false);
    }
  }

  Future<void> _showHomeControls({
    required bool hasNecklace,
    required bool legacyNecklaceNeedsConfirmation,
    required bool necklaceConnected,
    required bool necklaceConnecting,
    required int batteryLevel,
    required DeviceType deviceType,
    required bool showGuardianSurfaces,
    required VoidCallback onReconnectNecklace,
    required VoidCallback onConfirmLegacyNecklace,
  }) async {
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: EllaColors.bgPrimary,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(28))),
      builder: (sheetContext) => _HomeControlsSheet(
        hasNecklace: hasNecklace,
        legacyNecklaceNeedsConfirmation: legacyNecklaceNeedsConfirmation,
        necklaceConnected: necklaceConnected,
        necklaceConnecting: necklaceConnecting,
        batteryLevel: batteryLevel,
        deviceType: deviceType,
        showWhispers: showGuardianSurfaces,
        whispersEnabled: _whispersOn,
        whispersVerified: _whispersVerified,
        whispersUpdating: _updatingWhispers,
        onWhispersChanged: (enabled) {
          Navigator.of(sheetContext).pop();
          unawaited(_setWhispers(enabled));
        },
        onWhispersHistory: () {
          Navigator.of(sheetContext).pop();
          Navigator.of(context).push(MaterialPageRoute(builder: (_) => const GuardianAlertHistoryPage()));
        },
        onManageNecklace: () {
          Navigator.of(sheetContext).pop();
          Navigator.of(context).push(MaterialPageRoute(builder: (_) => const ConnectDevicePage()));
        },
        onReconnectNecklace: onReconnectNecklace,
        onConfirmLegacyNecklace: () {
          Navigator.of(sheetContext).pop();
          onConfirmLegacyNecklace();
        },
      ),
    );
  }

  Future<void> _confirmLegacyNecklace(DeviceProvider device) async {
    final confirmed = await showDialog<bool>(
          context: context,
          builder: (dialogContext) => AlertDialog(
            backgroundColor: EllaColors.bgSecondary,
            surfaceTintColor: Colors.transparent,
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(EllaSizes.radiusLarge)),
            title: Text(context.l10n.todayLegacyNecklaceConfirmTitle),
            content: Text(context.l10n.todayLegacyNecklaceConfirmBody),
            actions: [
              TextButton(onPressed: () => Navigator.of(dialogContext).pop(false), child: Text(context.l10n.cancel)),
              FilledButton(
                key: const Key('today-confirm-legacy-necklace'),
                onPressed: () => Navigator.of(dialogContext).pop(true),
                child: Text(context.l10n.todayLegacyNecklaceConfirmAction),
              ),
            ],
          ),
        ) ??
        false;
    if (!confirmed || !mounted) return;

    final started = await device.confirmLegacyNecklaceForCurrentAuthority(reason: 'Home legacy necklace confirmation');
    if (!started && mounted) {
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text(context.l10n.todayLegacyNecklaceConfirmUnavailable)));
    }
  }

  @override
  Widget build(BuildContext context) {
    final now = SharedPreferencesUtil.isTodayDesignPreviewEnabled
        ? DateTime(2025, 7, 24, 9, 41)
        : (widget.nowProvider?.call() ?? DateTime.now());
    final device = context.watch<DeviceProvider>();
    final connectedDevice = device.presentationConnectedDevice;
    final deviceConnected = device.presentationIsConnected && connectedDevice != null;
    final hasNecklace = device.presentationPairedDevice != null;
    final legacyNecklaceNeedsConfirmation = device.legacyUntrustedDeviceCandidate != null;
    final deviceType = device.presentationConnectedDevice?.type ??
        device.presentationPairedDevice?.type ??
        device.legacyUntrustedDeviceCandidate?.type ??
        DeviceType.omi;
    final capture = context.watch<CaptureProvider>();
    final diagnosticCaptureSource = todayActiveCaptureSource(capture.recordingState, capture.captureDiagnostics);
    final activeCaptureSource = diagnosticCaptureSource ??
        ((capture.recordingState == RecordingState.initialising ||
                    capture.recordingState == RecordingState.pause ||
                    capture.recordingState == RecordingState.error) &&
                capture.havingRecordingDevice
            ? EllaCaptureSource.necklace
            : null);
    final selectedCaptureSource = todaySelectedCaptureSource(
      state: capture.recordingState,
      diagnostics: capture.captureDiagnostics,
      preferredSource: _selectedCaptureSource,
    );
    final conversations = context.watch<ConversationProvider>();
    final visibleConversations = conversations.visibleConversations;
    final orderedMemories = List<ServerConversation>.of(visibleConversations)
      ..sort((a, b) {
        final result = (b.startedAt ?? b.createdAt).compareTo(a.startedAt ?? a.createdAt);
        return _homeMemorySort == MemoryGallerySort.recent ? result : -result;
      });
    final showDayGallery = _homeMemoryLayout == MemoryGalleryLayout.days;
    final memoriesHydrating = orderedMemories.isEmpty && !conversations.hasLoadedConversations;
    final heroMemory = orderedMemories.isEmpty || showDayGallery ? null : orderedMemories.first;
    final remainingMemories = showDayGallery ? orderedMemories : orderedMemories.skip(1).toList(growable: false);
    final showDailyNote = shouldShowDailyNote(_todayCardController.state);
    final showGuardianSurfaces = _guardianAvailable;
    final homeCaptureOwned =
        _homeCaptureActive || _homeCaptureFinalizationPending || _homeCaptureFinalizationInFlight != null;
    final captureFinalizationPending = _homeCaptureFinalizationPending || _externalCaptureFinalizationSource != null;
    final dockClearance = todayDockScrollClearance(
      textScale: MediaQuery.textScalerOf(context).scale(1),
      safeBottom: MediaQuery.paddingOf(context).bottom,
    );
    _scheduleHomeMemoryPrefetch();

    void openControls() => unawaited(
          _showHomeControls(
            hasNecklace: hasNecklace,
            legacyNecklaceNeedsConfirmation: legacyNecklaceNeedsConfirmation,
            necklaceConnected: deviceConnected,
            necklaceConnecting: device.isConnecting,
            batteryLevel: device.presentationBatteryLevel,
            deviceType: deviceType,
            showGuardianSurfaces: showGuardianSurfaces,
            onReconnectNecklace: () => unawaited(_reconnectNecklaceCapture(device)),
            onConfirmLegacyNecklace: () => unawaited(_confirmLegacyNecklace(device)),
          ),
        );
    return SafeArea(
      bottom: false,
      child: Stack(
        children: [
          RefreshIndicator(
            color: EllaColors.tealDeep,
            backgroundColor: EllaColors.card,
            onRefresh: () async {
              await Future.wait([
                _todayCardController.retry(),
                if (showGuardianSurfaces) _loadWhisperState(),
                context.read<ConversationProvider>().getInitialConversations(),
                _loadHomeArtworkPreferences(),
              ]);
            },
            child: CustomScrollView(
              key: const Key('today-scroll'),
              controller: _scrollController,
              physics: const AlwaysScrollableScrollPhysics(),
              slivers: [
                SliverPadding(
                  padding: const EdgeInsets.fromLTRB(EllaSizes.screenPadding, 14, EllaSizes.screenPadding, 0),
                  sliver: SliverToBoxAdapter(child: _TodayHeader(now: now)),
                ),
                if (visibleConversations.isNotEmpty)
                  SliverPadding(
                    padding: const EdgeInsets.fromLTRB(EllaSizes.screenPadding, 16, EllaSizes.screenPadding, 10),
                    sliver: SliverToBoxAdapter(
                      child: _HomeMemoryToolbar(
                        layout: _homeMemoryLayout,
                        sort: _homeMemorySort,
                        canSortOldest: !conversations.hasMoreConversations,
                        artworkPreferences: _homeArtworkPreferences,
                        artworkBackfillState: _homeArtworkBackfillState,
                        artworkStyleSaving: _homeArtworkStyleSaving,
                        artworkQueueStatus: _homeArtworkQueueStatus,
                        artworkQueueLoadState: _homeArtworkQueueLoadState,
                        onLayoutSelected: _selectHomeMemoryLayout,
                        onSortSelected: (sort) => setState(() => _homeMemorySort = sort),
                        onArtworkStudio: _openHomeArtworkStudio,
                      ),
                    ),
                  ),
                if (orderedMemories.isEmpty || !showDayGallery)
                  SliverPadding(
                    padding: const EdgeInsets.fromLTRB(EllaSizes.screenPadding, 12, EllaSizes.screenPadding, 0),
                    sliver: SliverToBoxAdapter(
                      child: heroMemory == null
                          ? memoriesHydrating
                              ? const _MemoryJournalLoadingState()
                              : const _MemoryJournalEmptyState()
                          : MemoryGalleryCard(
                              conversation: heroMemory,
                              layout: MemoryGalleryLayout.journal,
                              displayTitle: homeMemoryDisplayTitle(heroMemory, context.l10n.untitledConversation),
                              artworkApi: _memoryArtworkApi,
                              artworkRefreshEpoch: _homeArtworkDisplayEpoch,
                              artworkAuthorityEpoch: _homeCaptureAuthorityGeneration,
                              enqueueArtworkIfMissing: _homeArtworkPreferences?.releaseEnabled == true,
                              onOpen: () => _openMemoryDetail(heroMemory),
                              onDelete: () => _deleteMemory(heroMemory),
                            ),
                    ),
                  ),
                if (showDailyNote)
                  SliverPadding(
                    padding: const EdgeInsets.fromLTRB(EllaSizes.screenPadding, 18, EllaSizes.screenPadding, 0),
                    sliver: SliverToBoxAdapter(
                      child: TodayCardSurface(
                        compact: true,
                        surfaceColor: EllaColors.elevatedCard,
                        state: _todayCardController.state,
                        onReadMore: _openTodayCardDetail,
                        onTalk: _openTodayCardTalk,
                      ),
                    ),
                  ),
                ..._homeMemoryFeedSlivers(remainingMemories, now: now),
                if (conversations.isLoadingMoreConversations)
                  const SliverToBoxAdapter(
                    child: Padding(
                      key: Key('home-memories-loading-more'),
                      padding: EdgeInsets.symmetric(vertical: 24),
                      child: Center(
                        child: SizedBox(
                          width: 22,
                          height: 22,
                          child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
                        ),
                      ),
                    ),
                  )
                else if (conversations.loadMoreConversationsFailed)
                  SliverToBoxAdapter(
                    child: Padding(
                      padding: const EdgeInsets.symmetric(vertical: 16),
                      child: Center(
                        child: TextButton.icon(
                          key: const Key('home-retry-load-more-memories'),
                          onPressed: conversations.getMoreConversationsFromServer,
                          icon: const Icon(Icons.refresh_rounded),
                          label: Text(context.l10n.tryAgain),
                        ),
                      ),
                    ),
                  ),
                SliverToBoxAdapter(child: SizedBox(height: dockClearance)),
              ],
            ),
          ),
          if (_showBackToRecent)
            Positioned(
              right: 22,
              bottom: EllaSizes.navBarHeight + MediaQuery.paddingOf(context).bottom + 166,
              child: FloatingActionButton.small(
                key: const Key('home-back-to-recent'),
                onPressed: _scrollHomeBackToRecent,
                backgroundColor: EllaColors.tealDeep,
                foregroundColor: EllaColors.paper,
                tooltip: context.l10n.backToRecentMemories,
                child: const Icon(Icons.arrow_upward_rounded),
              ),
            ),
          Positioned(
            left: 14,
            right: 14,
            bottom: EllaSizes.navBarHeight + MediaQuery.paddingOf(context).bottom + 16,
            child: TodayRecordMomentControl(
              selectedSource: selectedCaptureSource,
              activeSource: activeCaptureSource,
              externalCaptureFinalizationPending: captureFinalizationPending,
              starting: _homeCaptureStarting,
              hasNecklace: hasNecklace,
              legacyNecklaceNeedsConfirmation: legacyNecklaceNeedsConfirmation,
              necklaceConnected: deviceConnected,
              necklaceConnecting: device.isConnecting,
              recordingState: capture.recordingState,
              diagnostics: capture.captureDiagnostics,
              showWhispers: showGuardianSurfaces,
              whispersEnabled: _whispersOn,
              whispersVerified: _whispersVerified,
              onOpenControls: openControls,
              onOpenWhispers: () =>
                  Navigator.of(context).push(MaterialPageRoute(builder: (_) => const GuardianAlertHistoryPage())),
              onViewTranscript: () => _openLiveTranscript(capture),
              onSourceSelected: _selectCaptureSource,
              onUnavailable: () => ScaffoldMessenger.of(
                context,
              ).showSnackBar(SnackBar(content: Text(context.l10n.todayRecordingUnavailable))),
              onTap: () => _handleCapturePrimaryAction(
                selectedSource: selectedCaptureSource,
                capture: capture,
                device: device,
                homeCaptureOwned: homeCaptureOwned,
                legacyNecklaceNeedsConfirmation: legacyNecklaceNeedsConfirmation,
              ),
            ),
          ),
        ],
      ),
    );
  }

  void _openMemoryDetail(ServerConversation conversation) {
    Navigator.of(context).push(MaterialPageRoute(builder: (_) => ConversationDetailPage(conversation: conversation)));
  }

  List<Widget> _homeMemoryFeedSlivers(List<ServerConversation> memories, {required DateTime now}) {
    if (memories.isEmpty) return const [];
    if (_homeMemoryLayout == MemoryGalleryLayout.days) {
      final groups = groupMemoryConversationsByDay(context, memories, now: now);
      return [
        for (final entry in groups.entries)
          SliverPadding(
            padding: const EdgeInsets.fromLTRB(EllaSizes.screenPadding, 18, EllaSizes.screenPadding, 0),
            sliver: SliverToBoxAdapter(
              child: MemoryDayGalleryCard(
                dayLabel: entry.key,
                memories: entry.value,
                artworkApi: _memoryArtworkApi,
                artworkRefreshEpoch: _homeArtworkDisplayEpoch,
                artworkAuthorityEpoch: _homeCaptureAuthorityGeneration,
                onOpen: () {
                  final authority = _memoryArtworkAuthorityProvider();
                  if (SharedPreferencesUtil.isPublicBuild && (authority == null || !authority.isExactCurrent())) {
                    return;
                  }
                  Navigator.of(context).push(
                    MaterialPageRoute(
                      builder: (_) => EllaMemoryDayPage(
                        dayLabel: entry.key,
                        memories: entry.value,
                        artworkApi: _memoryArtworkApi,
                        artworkRefreshEpoch: _homeArtworkDisplayEpoch,
                        artworkAuthorityEpoch: _homeCaptureAuthorityGeneration,
                        exactAuthority: authority,
                        authorityChanges: _todayCardAuthorityChanges,
                        onDelete: _deleteMemory,
                      ),
                    ),
                  );
                },
              ),
            ),
          ),
      ];
    }
    if (_homeMemoryLayout == MemoryGalleryLayout.grid) {
      return [
        SliverPadding(
          padding: const EdgeInsets.fromLTRB(EllaSizes.screenPadding, 18, EllaSizes.screenPadding, 0),
          sliver: SliverGrid(
            gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
              maxCrossAxisExtent: 430,
              mainAxisSpacing: EllaSizes.cardGap,
              crossAxisSpacing: EllaSizes.cardGap,
              childAspectRatio: 0.86,
            ),
            delegate: SliverChildBuilderDelegate(
              (context, index) => _homeMemoryCard(memories[index]),
              childCount: memories.length,
            ),
          ),
        ),
      ];
    }
    return [
      SliverPadding(
        padding: const EdgeInsets.fromLTRB(EllaSizes.screenPadding, 18, EllaSizes.screenPadding, 0),
        sliver: SliverList.separated(
          itemCount: memories.length,
          separatorBuilder: (_, __) => const SizedBox(height: EllaSizes.cardGap),
          itemBuilder: (context, index) => _homeMemoryCard(memories[index]),
        ),
      ),
    ];
  }

  Widget _homeMemoryCard(ServerConversation conversation) => MemoryGalleryCard(
        conversation: conversation,
        layout: _homeMemoryLayout,
        displayTitle: homeMemoryDisplayTitle(conversation, context.l10n.untitledConversation),
        artworkApi: _memoryArtworkApi,
        artworkRefreshEpoch: _homeArtworkDisplayEpoch,
        artworkAuthorityEpoch: _homeCaptureAuthorityGeneration,
        onOpen: () => _openMemoryDetail(conversation),
        onDelete: () => _deleteMemory(conversation),
      );

  Future<bool> _deleteMemory(ServerConversation conversation) async {
    final l10n = context.l10n;
    final provider = context.read<ConversationProvider>();
    final confirmed = await showDialog<bool>(
          context: context,
          builder: (dialogContext) => AlertDialog(
            title: Text(l10n.deleteConversationTitle),
            content: Text(l10n.deleteConversationMessage),
            actions: [
              TextButton(onPressed: () => Navigator.of(dialogContext).pop(false), child: Text(l10n.cancel)),
              FilledButton(
                onPressed: () => Navigator.of(dialogContext).pop(true),
                style: FilledButton.styleFrom(backgroundColor: EllaColors.error),
                child: Text(l10n.delete),
              ),
            ],
          ),
        ) ??
        false;
    if (!confirmed || !mounted) return false;
    final deleted = await provider.deleteConversationPermanently(conversation);
    if (!deleted && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(l10n.failedToDeleteConversations)));
    }
    return deleted;
  }
}

double todayDockScrollClearance({required double textScale, required double safeBottom}) =>
    EllaSizes.navBarHeight + safeBottom + 190 * textScale.clamp(1.0, 2.0) + 24;

class _DemoTodayCardRepository implements TodayCardRepository {
  const _DemoTodayCardRepository();

  @override
  Future<TodayCardResponse> fetch({required String uid}) async => TodayCardFixtures.recap();
}

class _TodayCardDetailSheet extends StatelessWidget {
  const _TodayCardDetailSheet({required this.card, required this.onTalk});

  final TodayCard card;
  final VoidCallback onTalk;

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      top: false,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const SizedBox(height: 10),
          Center(
            child: Container(
              width: 44,
              height: 4,
              decoration: BoxDecoration(color: EllaColors.cardDeep, borderRadius: BorderRadius.circular(2)),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 8, 8, 0),
            child: Row(
              children: [
                Expanded(child: Text(context.l10n.todayCardPreparingEyebrow, style: EllaTextStyles.eyebrow)),
                IconButton(
                  key: const Key('today-card-detail-close'),
                  tooltip: context.l10n.close,
                  onPressed: () => Navigator.of(context).pop(),
                  icon: const Icon(Icons.close_rounded, color: EllaColors.tealDeep),
                ),
              ],
            ),
          ),
          Expanded(
            child: SingleChildScrollView(
              key: const Key('today-card-detail-scroll'),
              padding: const EdgeInsets.fromLTRB(20, 12, 20, 24),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Semantics(
                    header: true,
                    child: Text(card.headline, style: EllaTextStyles.noteBody.copyWith(fontSize: 30, height: 1.15)),
                  ),
                  const SizedBox(height: 18),
                  Text(card.body, style: EllaTextStyles.noteBody.copyWith(fontSize: 20, height: 1.45)),
                ],
              ),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 12, 20, 12),
            child: FilledButton.icon(
              key: const Key('today-card-detail-talk'),
              onPressed: onTalk,
              icon: const Icon(Icons.graphic_eq_rounded),
              label: Text(context.l10n.memoryTalkAction),
              style: FilledButton.styleFrom(
                backgroundColor: EllaColors.tealDeep,
                foregroundColor: Colors.white,
                minimumSize: const Size.fromHeight(EllaSizes.minTouchTarget),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _TodayHeader extends StatelessWidget {
  const _TodayHeader({required this.now});

  final DateTime now;

  @override
  Widget build(BuildContext context) {
    final rawName = SharedPreferencesUtil().givenName.trim();
    final firstName = rawName.isNotEmpty
        ? rawName
        : (SharedPreferencesUtil().demoMode ? 'Margaret' : context.l10n.todayGreetingFallbackName);
    final greeting = now.hour < 12
        ? context.l10n.todayGreetingMorning(firstName)
        : now.hour < 17
            ? context.l10n.todayGreetingAfternoon(firstName)
            : context.l10n.todayGreetingEvening(firstName);
    final locale = Localizations.localeOf(context).toLanguageTag();
    final date = context.l10n
        .todayDateEyebrow(DateFormat('EEEE', locale).format(now), DateFormat('MMMM d', locale).format(now))
        .toUpperCase();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(date, style: EllaTextStyles.eyebrow),
        const SizedBox(height: 8),
        Text(greeting, style: EllaTextStyles.noteBody.copyWith(fontSize: 24, height: 1.14)),
      ],
    );
  }
}

class _HomeMemoryToolbar extends StatelessWidget {
  const _HomeMemoryToolbar({
    required this.layout,
    required this.sort,
    required this.canSortOldest,
    required this.artworkPreferences,
    required this.artworkBackfillState,
    required this.artworkStyleSaving,
    required this.artworkQueueStatus,
    required this.artworkQueueLoadState,
    required this.onLayoutSelected,
    required this.onSortSelected,
    required this.onArtworkStudio,
  });

  final MemoryGalleryLayout layout;
  final MemoryGallerySort sort;
  final bool canSortOldest;
  final MemoryArtworkPreferences? artworkPreferences;
  final _ArtworkBackfillUiState artworkBackfillState;
  final bool artworkStyleSaving;
  final MemoryArtworkQueueStatus? artworkQueueStatus;
  final _ArtworkQueueLoadState artworkQueueLoadState;
  final ValueChanged<MemoryGalleryLayout> onLayoutSelected;
  final ValueChanged<MemoryGallerySort> onSortSelected;
  final VoidCallback onArtworkStudio;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            Expanded(
              child: Semantics(header: true, child: Text(context.l10n.memories, style: EllaTextStyles.eyebrow)),
            ),
            PopupMenuButton<MemoryGalleryLayout>(
              key: const Key('home-memory-layout-menu'),
              tooltip: context.l10n.memoryGalleryView,
              initialValue: layout,
              icon: const Icon(Icons.view_quilt_outlined, color: EllaColors.tealDeep),
              onSelected: onLayoutSelected,
              itemBuilder: (context) => [
                PopupMenuItem(value: MemoryGalleryLayout.journal, child: Text(context.l10n.memoryGalleryJournal)),
                PopupMenuItem(value: MemoryGalleryLayout.grid, child: Text(context.l10n.memoryGalleryGrid)),
                PopupMenuItem(value: MemoryGalleryLayout.list, child: Text(context.l10n.memoryGalleryList)),
                PopupMenuItem(value: MemoryGalleryLayout.days, child: Text(context.l10n.memoryGalleryDays)),
              ],
            ),
            PopupMenuButton<MemoryGallerySort>(
              key: const Key('home-memory-sort-menu'),
              tooltip: context.l10n.sortBy,
              initialValue: sort,
              icon: const Icon(Icons.swap_vert_rounded, color: EllaColors.tealDeep),
              onSelected: onSortSelected,
              itemBuilder: (context) => [
                PopupMenuItem(value: MemoryGallerySort.recent, child: Text(context.l10n.memorySortRecent)),
                PopupMenuItem(
                  value: MemoryGallerySort.oldest,
                  enabled: canSortOldest,
                  child: Text(context.l10n.memorySortOldest),
                ),
              ],
            ),
            IconButton(
              key: const Key('home-memory-artwork-style-menu'),
              tooltip: artworkPreferences?.releaseEnabled == true
                  ? context.l10n.memoryArtworkStudio
                  : context.l10n.memoryArtworkStyleUnavailable,
              onPressed: artworkPreferences?.releaseEnabled == true ? onArtworkStudio : null,
              icon: Stack(
                clipBehavior: Clip.none,
                children: [
                  Icon(
                    Icons.palette_outlined,
                    color: artworkPreferences?.releaseEnabled == true ? EllaColors.tealDeep : EllaColors.inkSoft,
                  ),
                  if (artworkStyleSaving || artworkBackfillState == _ArtworkBackfillUiState.running)
                    const Positioned(
                      right: -4,
                      bottom: -4,
                      child: SizedBox(
                        key: Key('home-artwork-progress-indicator'),
                        width: 12,
                        height: 12,
                        child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
                      ),
                    )
                  else if (artworkBackfillState == _ArtworkBackfillUiState.needsAttention)
                    const Positioned(
                      right: -3,
                      bottom: -3,
                      child: DecoratedBox(
                        key: Key('home-artwork-attention-indicator'),
                        decoration: BoxDecoration(color: EllaColors.warning, shape: BoxShape.circle),
                        child: SizedBox(width: 9, height: 9),
                      ),
                    ),
                ],
              ),
            ),
          ],
        ),
        if (_showQueueSummary) ...[
          const SizedBox(height: 8),
          _HomeArtworkQueueSummary(
            status: artworkQueueStatus,
            loadState: artworkQueueLoadState,
            onTap: onArtworkStudio,
          ),
        ],
      ],
    );
  }

  bool get _showQueueSummary {
    if (artworkPreferences?.releaseEnabled != true) return false;
    final queue = artworkQueueStatus;
    if (queue == null) {
      return artworkQueueLoadState == _ArtworkQueueLoadState.loading ||
          artworkQueueLoadState == _ArtworkQueueLoadState.failed;
    }
    return queue.remaining > 0 ||
        queue.failed > 0 ||
        queue.scanStatus != 'completed' ||
        queue.controlState == MemoryArtworkQueueState.paused ||
        queue.controlState == MemoryArtworkQueueState.cancelled;
  }
}

class _HomeArtworkQueueSummary extends StatelessWidget {
  const _HomeArtworkQueueSummary({required this.status, required this.loadState, required this.onTap});

  final MemoryArtworkQueueStatus? status;
  final _ArtworkQueueLoadState loadState;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final queue = status;
    final failed = loadState == _ArtworkQueueLoadState.failed;
    final headline = failed
        ? context.l10n.memoryArtworkQueueUnavailable
        : queue == null
            ? context.l10n.memoryArtworkQueueChecking
            : context.l10n.memoryArtworkQueueProgress(queue.ready, queue.total);
    final detail = failed || queue == null ? '' : _detail(context, queue);
    final progress = failed
        ? 1.0
        : queue == null
            ? null
            : _progress(queue);
    return Semantics(
      button: true,
      label: '$headline${detail.isEmpty ? '' : '. $detail'}. ${context.l10n.memoryArtworkStudio}',
      child: Material(
        color: EllaColors.elevatedCard,
        borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
        child: InkWell(
          key: const Key('home-artwork-queue-summary'),
          borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 11),
            child: Row(
              children: [
                SizedBox(
                  width: 34,
                  height: 34,
                  child: Stack(
                    alignment: Alignment.center,
                    children: [
                      CircularProgressIndicator(
                        key: const Key('home-artwork-queue-ring'),
                        value: progress,
                        strokeWidth: 3,
                        color: failed ? EllaColors.warning : EllaColors.tealDeep,
                        backgroundColor: EllaColors.cardEdge,
                      ),
                      Icon(
                        failed ? Icons.priority_high_rounded : Icons.auto_awesome_rounded,
                        size: 15,
                        color: failed ? EllaColors.warning : EllaColors.tealDeep,
                      ),
                    ],
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(headline, style: EllaTextStyles.secondary.copyWith(color: EllaColors.ink)),
                      if (detail.isNotEmpty) ...[
                        const SizedBox(height: 2),
                        Text(detail, style: EllaTextStyles.caption.copyWith(color: EllaColors.inkSoft)),
                      ],
                    ],
                  ),
                ),
                const Icon(Icons.chevron_right_rounded, color: EllaColors.tealDeep),
              ],
            ),
          ),
        ),
      ),
    );
  }

  static double? _progress(MemoryArtworkQueueStatus queue) {
    if (queue.state == MemoryArtworkQueueState.completed) return 1;
    if (queue.autoContinue) return null;
    if (queue.batchSize > 0 && queue.batchRemaining <= queue.batchSize) {
      return ((queue.batchSize - queue.batchRemaining) / queue.batchSize).clamp(0, 1);
    }
    return queue.progress;
  }

  static String _detail(BuildContext context, MemoryArtworkQueueStatus queue) {
    final parts = <String>[];
    if (queue.active > 0) parts.add(context.l10n.memoryArtworkQueueCreating(queue.active));
    if (queue.queued > 0) parts.add(context.l10n.memoryArtworkQueueQueued(queue.queued));
    if (queue.retrying > 0) parts.add(context.l10n.memoryArtworkQueueRetrying(queue.retrying));
    if (queue.failed > 0) parts.add(context.l10n.memoryArtworkQueueFailed(queue.failed));
    final workDetail = parts.join(' · ');
    final controlDetail = switch (queue.controlState) {
      MemoryArtworkQueueState.paused => context.l10n.memoryArtworkQueuePaused,
      MemoryArtworkQueueState.cancelled => context.l10n.memoryArtworkQueueStopped,
      _ => '',
    };
    if (controlDetail.isEmpty) return workDetail;
    if (workDetail.isEmpty) return controlDetail;
    return '$controlDetail $workDetail';
  }
}

class _ArtworkStudioSheet extends StatelessWidget {
  const _ArtworkStudioSheet({
    required this.preferences,
    required this.libraries,
    required this.backfillState,
    required this.queueLoadState,
    required this.styleSaving,
    required this.queueStatus,
    required this.queueControlBusy,
    required this.onStyleSelected,
    required this.onContinue,
    required this.onRetryStatus,
    required this.onPause,
    required this.onResume,
    required this.onStop,
  });

  final MemoryArtworkPreferences preferences;
  final MemoryArtworkLibraries? libraries;
  final _ArtworkBackfillUiState backfillState;
  final _ArtworkQueueLoadState queueLoadState;
  final bool styleSaving;
  final MemoryArtworkQueueStatus? queueStatus;
  final bool queueControlBusy;
  final ValueChanged<String> onStyleSelected;
  final VoidCallback onContinue;
  final VoidCallback onRetryStatus;
  final VoidCallback onPause;
  final VoidCallback onResume;
  final VoidCallback onStop;

  @override
  Widget build(BuildContext context) {
    final styles = <(String, String)>[
      (memoryArtworkDefaultStyle, context.l10n.memoryArtworkSoftGouache),
      (memoryArtworkPaperCollageStyle, context.l10n.memoryArtworkPaperCollage),
      (memoryArtworkGraphicLandscapeStyle, context.l10n.memoryArtworkGraphicLandscape),
      (memoryArtworkWatercolorJournalStyle, context.l10n.memoryArtworkWatercolorJournal),
      (memoryArtworkAnimeStorybookStyle, context.l10n.memoryArtworkAnimeStorybook),
      (memoryArtworkCinematicStillStyle, context.l10n.memoryArtworkCinematicStill),
    ];
    final legacyStatus = switch (backfillState) {
      _ArtworkBackfillUiState.running => context.l10n.memoryArtworkBackfillInProgress,
      _ArtworkBackfillUiState.moreAvailable => context.l10n.memoryArtworkBackfillMoreAvailable,
      _ArtworkBackfillUiState.complete => context.l10n.memoryArtworkBackfillComplete,
      _ArtworkBackfillUiState.needsAttention => context.l10n.memoryArtworkBackfillNeedsAttention,
      _ArtworkBackfillUiState.idle => context.l10n.memoryArtworkBackfillReady,
    };
    final status = switch (queueLoadState) {
      _ArtworkQueueLoadState.loading => context.l10n.memoryArtworkQueueChecking,
      _ArtworkQueueLoadState.failed => context.l10n.memoryArtworkQueueUnavailable,
      _ => legacyStatus,
    };
    final action = switch (backfillState) {
      _ArtworkBackfillUiState.complete => context.l10n.memoryArtworkCheckMissing,
      _ArtworkBackfillUiState.needsAttention => context.l10n.memoryArtworkRetry,
      _ => context.l10n.memoryArtworkContinueOlder,
    };
    final queue = queueStatus;
    final progress = queue == null ? 0.0 : _previewProgress(queue);
    final selectedLibrary = libraries?.forStyle(preferences.styleVersion);

    return SafeArea(
      child: SingleChildScrollView(
        padding: EdgeInsets.fromLTRB(20, 12, 20, 20 + MediaQuery.viewInsetsOf(context).bottom),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Center(
              child: Container(
                width: 40,
                height: 4,
                decoration: BoxDecoration(color: EllaColors.cardEdge, borderRadius: BorderRadius.circular(2)),
              ),
            ),
            const SizedBox(height: 18),
            Text(context.l10n.memoryArtworkStudio, style: EllaTextStyles.display.copyWith(color: EllaColors.ink)),
            const SizedBox(height: 6),
            Text(
              context.l10n.memoryArtworkStudioDetail,
              style: EllaTextStyles.secondary.copyWith(color: EllaColors.inkSoft),
            ),
            const SizedBox(height: 8),
            Text(
              libraries == null
                  ? context.l10n.memoryArtworkLibrariesChecking
                  : context.l10n.memoryArtworkLibrarySummary(
                      selectedLibrary?.readyDays ?? 0,
                      selectedLibrary?.readyMemories ?? 0,
                    ),
              key: const Key('home-artwork-library-summary'),
              style: EllaTextStyles.caption.copyWith(color: EllaColors.inkSoft),
            ),
            if (libraries != null) ...[
              const SizedBox(height: 4),
              Text(
                context.l10n.memoryArtworkRecentFirst(
                  libraries!.historicalBatchSize,
                  libraries!.defaultPreviewDays,
                ),
                style: EllaTextStyles.caption.copyWith(color: EllaColors.inkSoft),
              ),
            ],
            const SizedBox(height: 16),
            EllaCardSurface(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        if (styleSaving || queueLoadState == _ArtworkQueueLoadState.loading)
                          const SizedBox(
                            width: 20,
                            height: 20,
                            child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
                          )
                        else
                          Icon(
                            queueLoadState == _ArtworkQueueLoadState.failed ||
                                    backfillState == _ArtworkBackfillUiState.needsAttention
                                ? Icons.info_outline_rounded
                                : Icons.auto_awesome_rounded,
                            color: queueLoadState == _ArtworkQueueLoadState.failed ||
                                    backfillState == _ArtworkBackfillUiState.needsAttention
                                ? EllaColors.warning
                                : EllaColors.tealDeep,
                          ),
                        const SizedBox(width: 12),
                        Expanded(
                          child: Text(
                            queue == null ? status : _previewHeadline(context, queue),
                            key: const Key('home-artwork-queue-progress-label'),
                            style: EllaTextStyles.secondary.copyWith(color: EllaColors.ink),
                          ),
                        ),
                      ],
                    ),
                    if (queue != null) ...[
                      const SizedBox(height: 12),
                      Semantics(
                        label: _previewHeadline(context, queue),
                        value: progress == null
                            ? context.l10n.memoryArtworkBackfillInProgress
                            : '${(progress * 100).round()}%',
                        child: LinearProgressIndicator(
                          key: const Key('home-artwork-queue-progress-bar'),
                          value: progress,
                          minHeight: 8,
                          borderRadius: BorderRadius.circular(99),
                          color: EllaColors.tealDeep,
                          backgroundColor: EllaColors.cardEdge,
                        ),
                      ),
                      const SizedBox(height: 10),
                      if (queue.state != MemoryArtworkQueueState.completed)
                        Text(
                          _queueDetail(context, queue),
                          key: const Key('home-artwork-queue-detail'),
                          style: EllaTextStyles.caption.copyWith(color: EllaColors.inkSoft),
                        ),
                      if (queue.active > 0 && queue.controlState != MemoryArtworkQueueState.running) ...[
                        const SizedBox(height: 6),
                        Text(
                          context.l10n.memoryArtworkQueueActiveMayFinish,
                          style: EllaTextStyles.caption.copyWith(color: EllaColors.inkSoft),
                        ),
                      ],
                      const SizedBox(height: 12),
                      Wrap(
                        spacing: 10,
                        runSpacing: 8,
                        children: [
                          if (queue.canPause)
                            OutlinedButton.icon(
                              key: const Key('home-artwork-pause'),
                              onPressed: queueControlBusy ? null : onPause,
                              style: OutlinedButton.styleFrom(
                                foregroundColor: EllaColors.tealDeep,
                                side: const BorderSide(color: EllaColors.tealDeep),
                              ),
                              icon: const Icon(Icons.pause_rounded),
                              label: Text(context.l10n.memoryArtworkQueuePause),
                            ),
                          if (queue.canResume)
                            FilledButton.icon(
                              key: const Key('home-artwork-resume'),
                              onPressed: queueControlBusy ? null : onResume,
                              style: FilledButton.styleFrom(
                                foregroundColor: EllaColors.paper,
                                backgroundColor: EllaColors.tealDeep,
                              ),
                              icon: const Icon(Icons.play_arrow_rounded),
                              label: Text(context.l10n.memoryArtworkQueueNextBatch(queue.batchSize)),
                            ),
                          if (queue.canCancel)
                            TextButton.icon(
                              key: const Key('home-artwork-stop'),
                              onPressed: queueControlBusy ? null : onStop,
                              style: TextButton.styleFrom(foregroundColor: EllaColors.tealDeep),
                              icon: const Icon(Icons.stop_circle_outlined),
                              label: Text(context.l10n.memoryArtworkQueueStop),
                            ),
                          if (queueControlBusy)
                            const Padding(
                              padding: EdgeInsets.all(10),
                              child: SizedBox(
                                width: 18,
                                height: 18,
                                child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
                              ),
                            ),
                        ],
                      ),
                      if (queueLoadState == _ArtworkQueueLoadState.failed) ...[
                        const SizedBox(height: 8),
                        TextButton.icon(
                          key: const Key('home-artwork-retry-status'),
                          onPressed: queueControlBusy ? null : onRetryStatus,
                          style: TextButton.styleFrom(foregroundColor: EllaColors.tealDeep),
                          icon: const Icon(Icons.refresh_rounded),
                          label: Text(context.l10n.memoryArtworkQueueRetryStatus),
                        ),
                      ],
                    ],
                  ],
                ),
              ),
            ),
            const SizedBox(height: 18),
            Text(context.l10n.memoryArtworkStyle, style: EllaTextStyles.eyebrow.copyWith(color: EllaColors.inkSoft)),
            const SizedBox(height: 6),
            ...styles.map((style) {
              final selected = preferences.styleVersion == style.$1;
              final enabled = !styleSaving && !queueControlBusy;
              final library = libraries?.forStyle(style.$1);
              return ListTile(
                key: Key('home-artwork-style-${style.$1}'),
                contentPadding: EdgeInsets.zero,
                tileColor: EllaColors.paper,
                selectedTileColor: EllaColors.card,
                textColor: EllaColors.ink,
                iconColor: EllaColors.tealDeep,
                enabled: enabled,
                leading: Icon(
                  selected ? Icons.radio_button_checked_rounded : Icons.radio_button_unchecked_rounded,
                  color: selected ? EllaColors.tealDeep : EllaColors.inkSoft,
                ),
                title: Text(style.$2, style: EllaTextStyles.secondary.copyWith(color: EllaColors.ink)),
                subtitle: Text(
                  libraries == null
                      ? context.l10n.memoryArtworkLibrariesChecking
                      : library == null || library.readyMemories == 0
                          ? context.l10n.memoryArtworkLibraryEmpty
                          : context.l10n.memoryArtworkLibraryCount(library.readyDays, library.readyMemories),
                  style: EllaTextStyles.caption.copyWith(color: EllaColors.inkSoft),
                ),
                selected: selected,
                selectedColor: EllaColors.tealDeep,
                onTap: enabled ? () => onStyleSelected(style.$1) : null,
              );
            }),
            const SizedBox(height: 10),
            if (queue == null && queueLoadState == _ArtworkQueueLoadState.failed)
              SizedBox(
                width: double.infinity,
                child: FilledButton.icon(
                  key: const Key('home-artwork-retry-status'),
                  onPressed: styleSaving || queueControlBusy ? null : onRetryStatus,
                  style: FilledButton.styleFrom(
                    foregroundColor: EllaColors.paper,
                    backgroundColor: EllaColors.tealDeep,
                  ),
                  icon: const Icon(Icons.refresh_rounded),
                  label: Text(context.l10n.memoryArtworkQueueRetryStatus),
                ),
              ),
            if (queue == null && queueLoadState == _ArtworkQueueLoadState.idle)
              SizedBox(
                width: double.infinity,
                child: OutlinedButton.icon(
                  key: const Key('home-artwork-continue'),
                  onPressed: styleSaving || queueControlBusy ? null : onContinue,
                  style: OutlinedButton.styleFrom(
                    foregroundColor: EllaColors.tealDeep,
                    backgroundColor: EllaColors.paper,
                    side: const BorderSide(color: EllaColors.tealDeep),
                  ),
                  icon: const Icon(Icons.history_rounded),
                  label: Text(action),
                ),
              ),
          ],
        ),
      ),
    );
  }

  String _queueDetail(BuildContext context, MemoryArtworkQueueStatus queue) {
    final parts = <String>[];
    if (queue.controlState == MemoryArtworkQueueState.paused) {
      parts.add(
        queue.pauseReason == 'batch_complete'
            ? context.l10n.memoryArtworkQueueBatchComplete(queue.batchSize)
            : context.l10n.memoryArtworkQueuePaused,
      );
    } else if (queue.controlState == MemoryArtworkQueueState.cancelled) {
      parts.add(context.l10n.memoryArtworkQueueStopped);
    } else if (queue.autoContinue) {
      parts.add(context.l10n.memoryArtworkQueueAutomatic);
    } else if (queue.controlState == MemoryArtworkQueueState.running) {
      parts.add(context.l10n.memoryArtworkQueueBatchRemaining(queue.batchRemaining));
    }
    if (queue.active > 0) parts.add(context.l10n.memoryArtworkQueueCreating(queue.active));
    if (queue.queued > 0) parts.add(context.l10n.memoryArtworkQueueQueued(queue.queued));
    if (queue.retrying > 0) parts.add(context.l10n.memoryArtworkQueueRetrying(queue.retrying));
    if (queue.failed > 0) parts.add(context.l10n.memoryArtworkQueueFailed(queue.failed));
    if (parts.isNotEmpty) return parts.join(' · ');
    return switch (queue.controlState) {
      MemoryArtworkQueueState.paused => context.l10n.memoryArtworkQueuePaused,
      MemoryArtworkQueueState.cancelled => context.l10n.memoryArtworkQueueStopped,
      MemoryArtworkQueueState.completed => context.l10n.memoryArtworkQueueComplete,
      MemoryArtworkQueueState.needsAttention => context.l10n.memoryArtworkBackfillNeedsAttention,
      MemoryArtworkQueueState.running => context.l10n.memoryArtworkQueueComplete,
    };
  }

  String _previewHeadline(BuildContext context, MemoryArtworkQueueStatus queue) {
    if (queue.state == MemoryArtworkQueueState.completed) {
      return context.l10n.memoryArtworkQueueComplete;
    }
    if (queue.controlState == MemoryArtworkQueueState.paused && queue.pauseReason == 'batch_complete') {
      return context.l10n.memoryArtworkQueueBatchComplete(queue.batchSize);
    }
    if (queue.controlState == MemoryArtworkQueueState.running) {
      return context.l10n.memoryArtworkBackfillInProgress;
    }
    return _queueDetail(context, queue);
  }

  double? _previewProgress(MemoryArtworkQueueStatus queue) {
    if (queue.state == MemoryArtworkQueueState.completed) return 1;
    // A full-history run has no truthful bounded denominator. Keep the bar
    // indeterminate until the server reports a paused or terminal batch.
    if (queue.autoContinue && queue.controlState == MemoryArtworkQueueState.running) return null;
    if (queue.controlState == MemoryArtworkQueueState.paused && queue.pauseReason == 'batch_complete') return 1;
    if (queue.batchSize <= 0) return 0;
    return ((queue.batchSize - queue.batchRemaining) / queue.batchSize).clamp(0, 1);
  }
}

class TodayRecordMomentControl extends StatelessWidget {
  const TodayRecordMomentControl({
    super.key,
    required this.selectedSource,
    this.activeSource,
    this.externalCaptureFinalizationPending = false,
    required this.starting,
    this.hasNecklace = false,
    this.legacyNecklaceNeedsConfirmation = false,
    required this.necklaceConnected,
    required this.necklaceConnecting,
    required this.recordingState,
    this.diagnostics = const CaptureDiagnostics(),
    this.showWhispers = false,
    this.whispersEnabled = false,
    this.whispersVerified = false,
    this.onOpenControls,
    this.onOpenWhispers,
    required this.onViewTranscript,
    required this.onSourceSelected,
    this.onUnavailable,
    required this.onTap,
  });

  final EllaCaptureSource selectedSource;
  final EllaCaptureSource? activeSource;
  final bool externalCaptureFinalizationPending;
  final bool starting;
  final bool hasNecklace;
  final bool legacyNecklaceNeedsConfirmation;
  final bool necklaceConnected;
  final bool necklaceConnecting;
  final RecordingState recordingState;
  final CaptureDiagnostics diagnostics;
  final bool showWhispers;
  final bool whispersEnabled;
  final bool whispersVerified;
  final VoidCallback? onOpenControls;
  final VoidCallback? onOpenWhispers;
  final VoidCallback onViewTranscript;
  final ValueChanged<EllaCaptureSource> onSourceSelected;
  final VoidCallback? onUnavailable;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final initialising = starting || recordingState == RecordingState.initialising;
    final phoneRecording = recordingState == RecordingState.record;
    final necklaceRecording = recordingState == RecordingState.deviceRecord;
    final active = phoneRecording || necklaceRecording;
    final canEscapeNecklaceStartup =
        initialising && activeSource == EllaCaptureSource.necklace && selectedSource == EllaCaptureSource.necklace;
    final sourceLocked =
        phoneRecording || externalCaptureFinalizationPending || (initialising && !canEscapeNecklaceStartup);
    final presentation = _presentation(
      context,
      initialising: initialising,
      phoneRecording: phoneRecording,
      necklaceRecording: necklaceRecording,
      externalCaptureFinalizationPending: externalCaptureFinalizationPending,
      hasNecklace: hasNecklace,
      legacyNecklaceNeedsConfirmation: legacyNecklaceNeedsConfirmation,
      necklaceConnected: necklaceConnected,
      necklaceConnecting: necklaceConnecting,
    );
    return Material(
      key: const Key('today-capture-dock'),
      elevation: 12,
      shadowColor: Colors.black.withValues(alpha: 0.14),
      color: EllaColors.elevatedCard,
      borderRadius: BorderRadius.circular(24),
      clipBehavior: Clip.antiAlias,
      child: DecoratedBox(
        decoration: BoxDecoration(
          border: Border.all(color: EllaColors.cardEdge),
          borderRadius: BorderRadius.circular(24),
        ),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(10, 8, 10, 10),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Semantics(
                label: context.l10n.todayCaptureSourceTitle,
                container: true,
                child: Row(
                  key: const Key('today-capture-source-selector'),
                  children: [
                    Expanded(
                      child: _TodayCaptureSourceButton(
                        buttonKey: const Key('today-capture-source-phone'),
                        icon: Icons.smartphone_rounded,
                        label: context.l10n.todayCaptureSourcePhone,
                        selected: selectedSource == EllaCaptureSource.phone,
                        enabled: !sourceLocked,
                        onTap: () => onSourceSelected(EllaCaptureSource.phone),
                      ),
                    ),
                    const SizedBox(width: 6),
                    Expanded(
                      child: _TodayCaptureSourceButton(
                        buttonKey: const Key('today-capture-source-necklace'),
                        icon: Icons.bluetooth_rounded,
                        label: context.l10n.todayCaptureSourceNecklace,
                        selected: selectedSource == EllaCaptureSource.necklace,
                        enabled: !sourceLocked,
                        onTap: () => onSourceSelected(EllaCaptureSource.necklace),
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 4),
              InkWell(
                key: const Key('today-dock-status'),
                onTap: onOpenControls,
                borderRadius: BorderRadius.circular(16),
                child: ConstrainedBox(
                  constraints: const BoxConstraints(minHeight: EllaSizes.minTouchTarget),
                  child: Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 8),
                    child: Row(
                      children: [
                        EllaBreathingDot(
                          active: active,
                          live: active && diagnostics.hasPhysicalAudio,
                          activeColor: active ? EllaColors.error : EllaColors.teal,
                        ),
                        const SizedBox(width: 10),
                        Expanded(
                          child: Semantics(
                            liveRegion: true,
                            child: Text(
                              presentation.status,
                              key: const Key('today-record-source'),
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: EllaTextStyles.caption.copyWith(
                                color: EllaColors.inkSoft,
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                          ),
                        ),
                        if (selectedSource == EllaCaptureSource.necklace && !necklaceConnected)
                          Icon(
                            legacyNecklaceNeedsConfirmation ? Icons.link_rounded : Icons.refresh_rounded,
                            color: EllaColors.tealDeep,
                            semanticLabel: '',
                          ),
                      ],
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 4),
              Row(
                children: [
                  Expanded(
                    child: _TodayDockAction(
                      actionKey: const Key('today-record-moment'),
                      icon: presentation.primaryIcon,
                      label: presentation.primaryLabel,
                      emphasized: presentation.emphasized,
                      enabled: presentation.primaryEnabled,
                      onDisabledTap: presentation.mode == TodayCaptureDockMode.unavailable ? onUnavailable : null,
                      onTap: onTap,
                    ),
                  ),
                  const SizedBox(width: 6),
                  Expanded(
                    child: _TodayDockAction(
                      actionKey: const Key('today-view-live-transcript'),
                      icon: Icons.subject_rounded,
                      label: (activeSource ?? selectedSource) == EllaCaptureSource.necklace
                          ? context.l10n.todayDockTranscriptNecklace
                          : context.l10n.todayDockTranscriptPhone,
                      onTap: onViewTranscript,
                    ),
                  ),
                  if (showWhispers) ...[
                    const SizedBox(width: 6),
                    Expanded(
                      child: _TodayDockAction(
                        actionKey: const Key('today-whispers-card'),
                        icon: Icons.record_voice_over_rounded,
                        label: context.l10n.todayWhispersTitle,
                        selected: whispersEnabled && whispersVerified,
                        onTap: onOpenWhispers,
                      ),
                    ),
                  ],
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }

  TodayCaptureDockPresentation _presentation(
    BuildContext context, {
    required bool initialising,
    required bool phoneRecording,
    required bool necklaceRecording,
    required bool externalCaptureFinalizationPending,
    required bool hasNecklace,
    required bool legacyNecklaceNeedsConfirmation,
    required bool necklaceConnected,
    required bool necklaceConnecting,
  }) {
    final sourceIsNecklace = selectedSource == EllaCaptureSource.necklace;
    final switchingAwayFromStartup =
        initialising && activeSource == EllaCaptureSource.necklace && selectedSource == EllaCaptureSource.phone;
    final status = externalCaptureFinalizationPending
        ? context.l10n.todayDockRecordingNeedsAttention
        : phoneRecording
            ? context.l10n.todayDockRecordingPhone
            : necklaceRecording
                ? sourceIsNecklace
                    ? context.l10n.todayDockRecordingNecklace
                    : context.l10n.todayDockNecklaceActivePhoneSelected
                : initialising
                    ? switchingAwayFromStartup
                        ? sourceIsNecklace
                            ? context.l10n.todayDockNecklaceReady
                            : context.l10n.todayDockPhoneReady
                        : sourceIsNecklace
                            ? context.l10n.todayDockNecklaceConnecting
                            : context.l10n.todayDockPhoneStarting
                    : sourceIsNecklace
                        ? recordingState == RecordingState.error
                            ? context.l10n.todayDockNecklaceNeedsAttention
                            : necklaceConnecting
                                ? context.l10n.todayDockNecklaceConnecting
                                : legacyNecklaceNeedsConfirmation
                                    ? context.l10n.todayLegacyNecklaceDockStatus
                                    : !necklaceConnected
                                        ? context.l10n.todayDockNecklaceNotConnected
                                        : context.l10n.todayDockNecklaceReady
                        : recordingState == RecordingState.error
                            ? context.l10n.todayDockPhoneNeedsAttention
                            : context.l10n.todayDockPhoneReady;
    if (initialising && !switchingAwayFromStartup) {
      return TodayCaptureDockPresentation(
        mode: TodayCaptureDockMode.starting,
        status: status,
        primaryLabel: sourceIsNecklace ? context.l10n.todayDockStartNecklace : context.l10n.todayDockRecord,
        primaryIcon: sourceIsNecklace ? Icons.bluetooth_searching_rounded : Icons.mic_none_rounded,
        primaryEnabled: false,
      );
    }
    if (externalCaptureFinalizationPending) {
      return TodayCaptureDockPresentation(
        mode: TodayCaptureDockMode.finishing,
        status: status,
        primaryLabel: context.l10n.todayDockFinish,
        primaryIcon: Icons.stop_rounded,
        primaryEnabled: true,
      );
    }
    if (phoneRecording) {
      return TodayCaptureDockPresentation(
        mode: TodayCaptureDockMode.recording,
        status: status,
        primaryLabel: context.l10n.todayDockStop,
        primaryIcon: Icons.stop_rounded,
        primaryEnabled: true,
      );
    }
    if (necklaceRecording && sourceIsNecklace) {
      return TodayCaptureDockPresentation(
        mode: TodayCaptureDockMode.recording,
        status: status,
        primaryLabel: context.l10n.todayDockSaveMoment,
        primaryIcon: Icons.bookmark_add_rounded,
        primaryEnabled: true,
      );
    }
    if (sourceIsNecklace) {
      final needsReconnect = legacyNecklaceNeedsConfirmation || (hasNecklace && !necklaceConnected);
      return TodayCaptureDockPresentation(
        mode: recordingState == RecordingState.error ? TodayCaptureDockMode.unavailable : TodayCaptureDockMode.ready,
        status: status,
        primaryLabel: recordingState == RecordingState.error
            ? context.l10n.todayDockRetry
            : needsReconnect
                ? context.l10n.todayDockReconnect
                : context.l10n.todayDockStartNecklace,
        primaryIcon:
            needsReconnect || recordingState == RecordingState.error ? Icons.refresh_rounded : Icons.mic_none_rounded,
        primaryEnabled: !necklaceConnecting && (necklaceConnected || hasNecklace || legacyNecklaceNeedsConfirmation),
      );
    }
    return TodayCaptureDockPresentation(
      mode: recordingState == RecordingState.error ? TodayCaptureDockMode.unavailable : TodayCaptureDockMode.ready,
      status: status,
      primaryLabel: recordingState == RecordingState.error ? context.l10n.todayDockRetry : context.l10n.todayDockRecord,
      primaryIcon: Icons.mic_none_rounded,
      primaryEnabled: true,
    );
  }
}

class _TodayCaptureSourceButton extends StatelessWidget {
  const _TodayCaptureSourceButton({
    required this.buttonKey,
    required this.icon,
    required this.label,
    required this.selected,
    required this.enabled,
    required this.onTap,
  });

  final Key buttonKey;
  final IconData icon;
  final String label;
  final bool selected;
  final bool enabled;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => Semantics(
        button: true,
        selected: selected,
        enabled: enabled,
        label: label,
        excludeSemantics: true,
        child: Material(
          color: selected ? const Color(0xFFD0E4DE) : EllaColors.paper,
          borderRadius: BorderRadius.circular(14),
          child: InkWell(
            key: buttonKey,
            onTap: enabled ? onTap : null,
            borderRadius: BorderRadius.circular(14),
            child: ConstrainedBox(
              constraints: const BoxConstraints(minHeight: 42),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Icon(icon, size: 18, color: selected ? EllaColors.tealDeep : EllaColors.inkSoft),
                  const SizedBox(width: 7),
                  Flexible(
                    child: Text(
                      label,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: EllaTextStyles.caption.copyWith(
                        color: selected ? EllaColors.tealDeep : EllaColors.inkSoft,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      );
}

class _TodayDockAction extends StatefulWidget {
  const _TodayDockAction({
    required this.actionKey,
    required this.icon,
    required this.label,
    required this.onTap,
    this.emphasized = false,
    this.selected = false,
    this.enabled = true,
    this.onDisabledTap,
  });

  final Key actionKey;
  final IconData icon;
  final String label;
  final VoidCallback? onTap;
  final bool emphasized;
  final bool selected;
  final bool enabled;
  final VoidCallback? onDisabledTap;

  @override
  State<_TodayDockAction> createState() => _TodayDockActionState();
}

class _TodayDockActionState extends State<_TodayDockAction> with SingleTickerProviderStateMixin {
  late final AnimationController _recordingPulse;

  @override
  void initState() {
    super.initState();
    _recordingPulse = AnimationController(vsync: this, duration: const Duration(milliseconds: 1500));
    if (widget.emphasized) _recordingPulse.repeat(reverse: true);
  }

  @override
  void didUpdateWidget(_TodayDockAction oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.emphasized == widget.emphasized) return;
    if (widget.emphasized) {
      _recordingPulse.repeat(reverse: true);
    } else {
      _recordingPulse
        ..stop()
        ..reset();
    }
  }

  @override
  void dispose() {
    _recordingPulse.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final foreground = widget.emphasized
        ? EllaColors.paper
        : widget.selected
            ? EllaColors.tealDeep
            : EllaColors.inkSoft;
    return Semantics(
      button: true,
      enabled: widget.enabled,
      selected: widget.selected || widget.emphasized,
      label: widget.label,
      excludeSemantics: true,
      child: AnimatedBuilder(
        animation: _recordingPulse,
        builder: (context, child) => DecoratedBox(
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(16),
            boxShadow: widget.emphasized && !MediaQuery.disableAnimationsOf(context)
                ? [
                    BoxShadow(
                      color: EllaColors.error.withValues(alpha: 0.18 + (_recordingPulse.value * 0.16)),
                      blurRadius: 8 + (_recordingPulse.value * 8),
                      spreadRadius: _recordingPulse.value * 2,
                    ),
                  ]
                : const [],
          ),
          child: child,
        ),
        child: Material(
          color: widget.emphasized
              ? EllaColors.error
              : widget.selected
                  ? const Color(0xFFD0E4DE)
                  : EllaColors.paper,
          borderRadius: BorderRadius.circular(16),
          child: InkWell(
            key: widget.actionKey,
            onTap: widget.enabled ? widget.onTap : widget.onDisabledTap,
            borderRadius: BorderRadius.circular(16),
            child: ConstrainedBox(
              constraints: const BoxConstraints(minHeight: 58),
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 7),
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Icon(widget.icon, size: 22, color: widget.enabled ? foreground : EllaColors.cardEdge),
                    const SizedBox(height: 3),
                    Text(
                      widget.label,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      textAlign: TextAlign.center,
                      style: EllaTextStyles.caption.copyWith(
                        color: widget.enabled ? foreground : EllaColors.cardEdge,
                        fontSize: 11,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _HomeControlsSheet extends StatelessWidget {
  const _HomeControlsSheet({
    required this.hasNecklace,
    required this.legacyNecklaceNeedsConfirmation,
    required this.necklaceConnected,
    required this.necklaceConnecting,
    required this.batteryLevel,
    required this.deviceType,
    required this.showWhispers,
    required this.whispersEnabled,
    required this.whispersVerified,
    required this.whispersUpdating,
    required this.onWhispersChanged,
    required this.onWhispersHistory,
    required this.onManageNecklace,
    required this.onReconnectNecklace,
    required this.onConfirmLegacyNecklace,
  });

  final bool hasNecklace;
  final bool legacyNecklaceNeedsConfirmation;
  final bool necklaceConnected;
  final bool necklaceConnecting;
  final int batteryLevel;
  final DeviceType deviceType;
  final bool showWhispers;
  final bool whispersEnabled;
  final bool whispersVerified;
  final bool whispersUpdating;
  final ValueChanged<bool> onWhispersChanged;
  final VoidCallback onWhispersHistory;
  final VoidCallback onManageNecklace;
  final VoidCallback onReconnectNecklace;
  final VoidCallback onConfirmLegacyNecklace;

  String _recordingSource(BuildContext context) {
    return context.l10n.todayRecordOnPhone;
  }

  @override
  Widget build(BuildContext context) {
    final necklaceGlyph = EllaHardwareArtwork.glyphForDeviceType(deviceType);
    return Padding(
      padding: EdgeInsets.fromLTRB(
        EllaSizes.screenPadding,
        16,
        EllaSizes.screenPadding,
        24 + MediaQuery.paddingOf(context).bottom,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Center(
            child: Container(
              width: 40,
              height: 4,
              decoration: BoxDecoration(color: EllaColors.cardDeep, borderRadius: BorderRadius.circular(2)),
            ),
          ),
          const SizedBox(height: 20),
          Text(context.l10n.todayControlsTitle, style: EllaTextStyles.noteBody.copyWith(fontSize: 28)),
          const SizedBox(height: 18),
          if (legacyNecklaceNeedsConfirmation) ...[
            FilledButton.icon(
              key: const Key('today-confirm-legacy-necklace-sheet'),
              onPressed: onConfirmLegacyNecklace,
              icon: const Icon(Icons.link_rounded),
              label: Text(context.l10n.todayLegacyNecklaceConfirmAction),
            ),
            const SizedBox(height: 12),
          ] else if (hasNecklace && !necklaceConnected) ...[
            FilledButton.icon(
              key: const Key('today-reconnect-known-necklace'),
              onPressed: necklaceConnecting ? null : onReconnectNecklace,
              icon: necklaceConnecting
                  ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.bluetooth_searching_rounded),
              label: Text(
                necklaceConnecting ? context.l10n.todayDockNecklaceConnecting : context.l10n.todayNecklaceOffReconnect,
              ),
            ),
            const SizedBox(height: 12),
          ],
          EllaCardSurface(
            child: Column(
              children: [
                _ControlRow(
                  icon: necklaceConnected && necklaceGlyph != null
                      ? Image.asset(necklaceGlyph, width: 28, height: 28)
                      : const Icon(Icons.phone_iphone_rounded, color: EllaColors.tealDeep),
                  title: context.l10n.todayRecordingSource,
                  detail: _recordingSource(context),
                ),
                const Divider(height: 1, indent: 64, color: EllaColors.cardDeep),
                InkWell(
                  key: const Key('today-manage-necklace'),
                  onTap: onManageNecklace,
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(minHeight: EllaSizes.listItemMinHeight),
                    child: Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 16),
                      child: Row(
                        children: [
                          const Icon(Icons.bluetooth_rounded, color: EllaColors.tealDeep),
                          const SizedBox(width: 16),
                          Expanded(
                            child: Text(
                              necklaceConnecting
                                  ? context.l10n.todayStripReconnecting
                                  : hasNecklace && !necklaceConnected
                                      ? context.l10n.todayNecklaceOffReconnect
                                      : context.l10n.todayManageNecklace,
                              style: EllaTextStyles.secondary.copyWith(fontWeight: FontWeight.w600),
                            ),
                          ),
                          const Icon(Icons.chevron_right_rounded, color: EllaColors.inkSoft),
                        ],
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
          if (showWhispers) ...[
            const SizedBox(height: 12),
            EllaCardSurface(
              child: Column(
                children: [
                  ConstrainedBox(
                    key: const Key('guardian-whispers-control'),
                    constraints: const BoxConstraints(minHeight: 64),
                    child: Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 16),
                      child: Row(
                        children: [
                          EllaBreathingDot(active: whispersVerified && whispersEnabled),
                          const SizedBox(width: 16),
                          Expanded(
                            child: Text(
                              whispersVerified
                                  ? whisperStatusLead(whispersEnabled)
                                  : context.l10n.todayWhispersUnavailable,
                              style: EllaTextStyles.secondary.copyWith(fontWeight: FontWeight.w600),
                            ),
                          ),
                          if (whispersUpdating)
                            const SizedBox(
                              width: 24,
                              height: 24,
                              child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
                            )
                          else
                            Switch(
                              value: whispersEnabled,
                              onChanged: whispersVerified ? onWhispersChanged : null,
                              activeTrackColor: EllaColors.tealDeep,
                              activeThumbColor: EllaColors.paper,
                            ),
                        ],
                      ),
                    ),
                  ),
                  const Divider(height: 1, indent: 64, color: EllaColors.cardDeep),
                  InkWell(
                    key: const Key('whispers-history-entry'),
                    onTap: onWhispersHistory,
                    child: ConstrainedBox(
                      constraints: const BoxConstraints(minHeight: EllaSizes.listItemMinHeight),
                      child: Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 16),
                        child: Row(
                          children: [
                            const SizedBox(width: 40),
                            Expanded(
                              child: Text(
                                context.l10n.todayWhispersHistory,
                                style: EllaTextStyles.secondary.copyWith(
                                  color: EllaColors.tealDeep,
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                            ),
                            const Icon(Icons.chevron_right_rounded, color: EllaColors.tealDeep),
                          ],
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _ControlRow extends StatelessWidget {
  const _ControlRow({required this.icon, required this.title, required this.detail});

  final Widget icon;
  final String title;
  final String detail;

  @override
  Widget build(BuildContext context) {
    return ConstrainedBox(
      constraints: const BoxConstraints(minHeight: 68),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
        child: Row(
          children: [
            SizedBox(width: 32, child: Center(child: icon)),
            const SizedBox(width: 16),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(title, style: EllaTextStyles.caption.copyWith(fontWeight: FontWeight.w700)),
                  const SizedBox(height: 2),
                  Text(detail, style: EllaTextStyles.secondary),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Compact always-visible status strip (#1106): one 48pt row — necklace glyph
/// + dot + battery/state, divider, headset glyph + dot + route — the whole
/// strip is a single tap target to the device detail page.
class TodayStatusStrip extends StatelessWidget {
  const TodayStatusStrip({
    super.key,
    this.hasNecklace = true,
    required this.necklaceConnected,
    required this.necklaceConnecting,
    required this.batteryLevel,
    required this.deviceType,
    required this.headsetConnected,
    required this.audioOutputName,
    required this.onTap,
  });

  final bool hasNecklace;
  final bool necklaceConnected;
  final bool necklaceConnecting;
  final int batteryLevel;
  final DeviceType deviceType;
  final bool headsetConnected;
  final String audioOutputName;
  final VoidCallback onTap;

  Color get _necklaceDot => necklaceConnecting
      ? EllaColors.warning
      : necklaceConnected
          ? (batteryLevel >= 0 && batteryLevel < 20 ? EllaColors.warning : EllaColors.teal)
          : EllaColors.inkSoft;

  String _necklaceLabel(BuildContext context) {
    if (!hasNecklace) return context.l10n.todayPhoneOnly;
    if (necklaceConnecting) return context.l10n.todayStripReconnecting;
    if (!necklaceConnected) return context.l10n.todayOff;
    return batteryLevel >= 0 ? '$batteryLevel%' : context.l10n.todayOn;
  }

  @override
  Widget build(BuildContext context) {
    final scale = MediaQuery.textScalerOf(context).scale(1);
    final glyphSize = scale >= 1.45 ? 28.0 : 24.0;
    final dotSize = scale >= 1.45 ? 10.0 : 8.0;
    final necklaceGlyph = hasNecklace ? EllaHardwareArtwork.glyphForDeviceType(deviceType) : null;
    final labelStyle = EllaTextStyles.caption.copyWith(fontWeight: FontWeight.w600);
    return EllaCardSurface(
      borderRadius: 14,
      child: InkWell(
        key: const Key('today-status-strip'),
        onTap: onTap,
        borderRadius: BorderRadius.circular(14),
        child: ConstrainedBox(
          constraints: const BoxConstraints(minHeight: EllaSizes.minTouchTarget),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 14),
            child: Row(
              children: [
                if (!hasNecklace)
                  Icon(Icons.phone_iphone_rounded, size: glyphSize, color: EllaColors.tealDeep)
                else if (necklaceGlyph != null)
                  Image.asset(necklaceGlyph, width: glyphSize, height: glyphSize)
                else
                  Icon(Icons.circle_outlined, size: glyphSize, color: EllaColors.ink),
                const SizedBox(width: 8),
                _StatusDot(color: hasNecklace ? _necklaceDot : EllaColors.teal, size: dotSize),
                const SizedBox(width: 8),
                Text(
                  _necklaceLabel(context),
                  style: labelStyle.copyWith(color: necklaceConnecting ? EllaColors.warning : EllaColors.inkSoft),
                ),
                Container(
                  width: 1,
                  height: 20,
                  color: EllaColors.cardDeep,
                  margin: const EdgeInsets.symmetric(horizontal: 8),
                ),
                Opacity(
                  opacity: headsetConnected ? 1 : 0.55,
                  child: Image.asset(EllaHardwareArtwork.whisperHeadsetGlyph, width: glyphSize, height: glyphSize),
                ),
                const SizedBox(width: 8),
                _StatusDot(color: headsetConnected ? EllaColors.teal : EllaColors.inkSoft, size: dotSize),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    headsetConnected ? audioOutputName : context.l10n.todayStripPhoneSpeaker,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: labelStyle.copyWith(color: EllaColors.inkSoft),
                  ),
                ),
                const Icon(Icons.chevron_right_rounded, color: EllaColors.inkSoft, size: 20),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _StatusDot extends StatelessWidget {
  const _StatusDot({required this.color, required this.size});

  final Color color;
  final double size;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(color: color, shape: BoxShape.circle),
    );
  }
}

/// The large illustrated device surface (#1106): mounts below the strip ONLY
/// when something is actionable — necklace disconnected/reconnecting, or audio
/// falling back to the loudspeaker — and unmounts when resolved. The
/// loudspeaker privacy line is compact here but never removed.
class TodayActionableDeviceCard extends StatelessWidget {
  const TodayActionableDeviceCard({
    super.key,
    this.hasNecklace = true,
    required this.necklaceConnected,
    required this.necklaceConnecting,
    required this.deviceType,
    required this.usesPhoneSpeaker,
    required this.onReconnect,
  });

  final bool hasNecklace;
  final bool necklaceConnected;
  final bool necklaceConnecting;
  final DeviceType deviceType;
  final bool usesPhoneSpeaker;
  final VoidCallback onReconnect;

  @override
  Widget build(BuildContext context) {
    final necklaceIssue = hasNecklace && !necklaceConnected;
    // Runtime uses OFF artwork + the live breathing dot for reconnecting; the
    // baked-dot reconnecting asset is reserved for static contexts (goldens).
    final artwork = EllaHardwareArtwork.forDeviceType(deviceType, EllaHardwareArtworkState.off);
    return EllaCardSurface(
      child: Padding(
        padding: const EdgeInsets.all(EllaSizes.cardPadding),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (necklaceIssue) ...[
              Row(
                children: [
                  if (artwork != null)
                    Stack(
                      alignment: Alignment.bottomRight,
                      children: [
                        Image.asset(artwork, width: 52, height: 52),
                        if (necklaceConnecting) const EllaBreathingDot(active: true, live: true),
                      ],
                    ),
                  if (artwork != null) const SizedBox(width: 14),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          necklaceConnecting ? context.l10n.todayNecklaceReconnectingTitle : context.l10n.todayNecklace,
                          style: EllaTextStyles.body.copyWith(fontWeight: FontWeight.w600),
                        ),
                        const SizedBox(height: 2),
                        Text(
                          necklaceConnecting
                              ? context.l10n.todayNecklaceReconnectingBody
                              : context.l10n.todayNecklaceOffReconnect,
                          style: EllaTextStyles.secondary.copyWith(color: EllaColors.inkSoft),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ],
            if (usesPhoneSpeaker) ...[
              if (necklaceIssue) const SizedBox(height: 14),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
                decoration: BoxDecoration(
                  color: EllaColors.cardDeep,
                  borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                ),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Icon(Icons.volume_up_rounded, color: EllaColors.warning, size: 18),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Text(
                        context.l10n.todayHeadsetFallbackWarning,
                        style: EllaTextStyles.secondary.copyWith(
                          color: EllaColors.warning,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ],
            if (necklaceIssue) ...[
              const SizedBox(height: 12),
              Material(
                color: EllaColors.cardDeep,
                borderRadius: BorderRadius.circular(24),
                child: InkWell(
                  key: const Key('help-reconnect'),
                  onTap: onReconnect,
                  borderRadius: BorderRadius.circular(24),
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(minHeight: EllaSizes.minTouchTarget),
                    child: Center(
                      child: Text(
                        context.l10n.todayHelpReconnect,
                        style: EllaTextStyles.secondary.copyWith(
                          color: EllaColors.tealDeep,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _MemoryFallbackArtwork extends StatelessWidget {
  const _MemoryFallbackArtwork();

  @override
  Widget build(BuildContext context) {
    return ExcludeSemantics(
      child: Image.asset(
        key: const Key('memory-fallback-art'),
        'assets/images/ella-memory-topics/quiet.webp',
        fit: BoxFit.cover,
        alignment: Alignment.center,
      ),
    );
  }
}

class _MemoryJournalEmptyState extends StatelessWidget {
  const _MemoryJournalEmptyState();

  @override
  Widget build(BuildContext context) {
    return EllaCardSurface(
      key: const Key('memory-journal-empty'),
      borderRadius: 20,
      color: EllaColors.elevatedCard,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const AspectRatio(aspectRatio: 2.4, child: _MemoryFallbackArtwork()),
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 18, 20, 22),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  context.l10n.todayMemoryCanvasEmptyHeadline,
                  style: EllaTextStyles.noteBody.copyWith(fontSize: 23, height: 1.15),
                ),
                const SizedBox(height: 8),
                Text(context.l10n.todayMemoryCanvasEmptyBody, style: EllaTextStyles.secondary),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _MemoryJournalLoadingState extends StatelessWidget {
  const _MemoryJournalLoadingState();

  @override
  Widget build(BuildContext context) {
    final reduceMotion = MediaQuery.disableAnimationsOf(context);
    return EllaCardSurface(
      key: const Key('memory-journal-loading'),
      borderRadius: 20,
      color: EllaColors.elevatedCard,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(20, 24, 20, 26),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.only(top: 2),
              child: reduceMotion
                  ? const Icon(Icons.auto_awesome_rounded, color: EllaColors.tealDeep)
                  : const EllaBreathingDot(active: true, live: true),
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    context.l10n.todayMemoryCanvasLoadingHeadline,
                    style: EllaTextStyles.noteBody.copyWith(fontSize: 23, height: 1.15),
                  ),
                  const SizedBox(height: 8),
                  Text(context.l10n.todayMemoryCanvasLoadingBody, style: EllaTextStyles.secondary),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
