import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/memory_artwork_api.dart';
import 'package:omi/ella/widgets/ella_breathing_dot.dart';
import 'package:omi/ella/widgets/ella_source_indicator.dart';
import 'package:omi/ella/widgets/memory_artwork_image.dart';
import 'package:omi/pages/conversation_capturing/page.dart';
import 'package:omi/pages/conversation_detail/page.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/display_text.dart';
import 'package:omi/utils/enums.dart';
import 'package:omi/utils/l10n_extensions.dart';

enum MemoryGalleryLayout { journal, grid, list, days }

bool memoryGalleryUsesWideLayout(BuildContext context) =>
    MediaQuery.sizeOf(context).width >= 640 && MediaQuery.textScalerOf(context).scale(1) < 2;

MemoryGalleryLayout effectiveMemoryGalleryLayout(BuildContext context, MemoryGalleryLayout layout) =>
    layout != MemoryGalleryLayout.journal &&
            (memoryGalleryUsesWideLayout(context) || layout == MemoryGalleryLayout.days)
        ? layout
        : MemoryGalleryLayout.list;

List<PopupMenuEntry<MemoryGalleryLayout>> memoryGalleryLayoutMenu(BuildContext context) => [
      if (memoryGalleryUsesWideLayout(context)) ...[
        PopupMenuItem(value: MemoryGalleryLayout.grid, child: Text(context.l10n.memoryGalleryGrid)),
      ],
      PopupMenuItem(value: MemoryGalleryLayout.list, child: Text(context.l10n.memoryGalleryList)),
      PopupMenuItem(value: MemoryGalleryLayout.days, child: Text(context.l10n.memoryGalleryDays)),
    ];

enum MemoryGallerySort { recent, oldest }

const _queueReadUnavailableArtwork = MemoryArtworkResult(
  status: MemoryArtworkResultStatus.unavailable,
  failureCode: 'memory_artwork_progress_read_unavailable',
);

MemoryArtworkResult? _artworkWithQueueReadState(MemoryArtworkResult? result, bool unavailable) {
  if (!unavailable) return result;
  if (result == null) return _queueReadUnavailableArtwork;
  if (result.status == MemoryArtworkResultStatus.generating) {
    return MemoryArtworkResult(
      status: MemoryArtworkResultStatus.unavailable,
      failureCode: _queueReadUnavailableArtwork.failureCode,
      authority: result.authority,
    );
  }
  // Queue transport failure must never soften a terminal privacy decision.
  if (!result.isReady) return result;
  if (!result.refreshPending) return result;
  // Keep the last authenticated image; the parent reports unknown progress.
  return MemoryArtworkResult(
    status: result.status,
    url: result.url,
    cacheKey: result.cacheKey,
    styleVersion: result.styleVersion,
    enrichmentRevision: result.enrichmentRevision,
    failureCode: result.failureCode,
    refreshFailureCode: result.refreshFailureCode,
    requestedStyleVersion: result.requestedStyleVersion,
    stale: result.stale,
    pixelWidth: result.pixelWidth,
    selectedVariantWidth: result.selectedVariantWidth,
    variants: result.variants,
    authority: result.authority,
  );
}

class EllaMemoriesPage extends StatefulWidget {
  const EllaMemoriesPage({super.key, this.artworkApi, this.onRecord});

  final MemoryArtworkApi? artworkApi;
  final VoidCallback? onRecord;

  @override
  State<EllaMemoriesPage> createState() => _EllaMemoriesPageState();
}

class _EllaMemoriesPageState extends State<EllaMemoriesPage> {
  static const _maxArtworkPreferenceUnavailableRetries = 3;
  static const _maxArtworkQueueUnavailableReads = 3;
  static const _maxArtworkQueueReads = 45;
  final ScrollController _scrollController = ScrollController();
  late final MemoryArtworkApi _artworkApi = widget.artworkApi ?? MemoryArtworkApi();
  MemoryArtworkPreferences? _artworkPreferences;
  MemoryGalleryLayout _layout = MemoryGalleryLayout.journal;
  MemoryGallerySort _sort = MemoryGallerySort.recent;
  bool _showBackToRecent = false;
  bool _artworkBackfillInFlight = false;
  bool _artworkBackfillRestartPending = false;
  MemoryArtworkQueueStatus? _artworkQueueStatus;
  Timer? _artworkQueuePollTimer;
  Timer? _artworkPreferencesRetryTimer;
  int _artworkQueueRefreshSequence = 0;
  int _artworkPreferenceLoadSequence = 0;
  int _artworkDisplayEpoch = 0;
  int _artworkAuthorityEpoch = 0;
  int _artworkPreferenceUnavailableRetries = 0;
  int _artworkQueueReads = 0;
  int _artworkQueueUnavailableReads = 0;
  bool _artworkQueueReadUnavailable = false;
  bool _artworkQueueManualRefreshPending = false;
  late final Listenable _artworkAuthorityChanges;

  @override
  void initState() {
    super.initState();
    _artworkAuthorityChanges = SharedPreferencesUtil.aiConsentAuthorityChanges;
    _artworkAuthorityChanges.addListener(_handleArtworkAuthorityChanged);
    _scrollController.addListener(_handleScroll);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      unawaited(context.read<ConversationProvider>().ensureFreshConversations());
      _loadGalleryLayout();
      unawaited(_loadArtworkPreferences());
    });
  }

  @override
  void dispose() {
    _artworkAuthorityChanges.removeListener(_handleArtworkAuthorityChanged);
    _artworkQueuePollTimer?.cancel();
    _artworkPreferencesRetryTimer?.cancel();
    _scrollController
      ..removeListener(_handleScroll)
      ..dispose();
    super.dispose();
  }

  void _handleArtworkAuthorityChanged() {
    // Queue totals and signed artwork bytes belong to the exact account/profile.
    // Never let a same-UID profile change inherit either while the new page loads.
    _artworkQueueRefreshSequence++;
    _artworkQueuePollTimer?.cancel();
    _artworkPreferencesRetryTimer?.cancel();
    _artworkPreferenceUnavailableRetries = 0;
    _resetArtworkQueueReadBudget();
    if (mounted) {
      setState(() {
        _artworkPreferences = null;
        _artworkQueueStatus = null;
        _artworkQueueReadUnavailable = false;
        _artworkAuthorityEpoch++;
        _artworkDisplayEpoch++;
      });
    }
    unawaited(_loadArtworkPreferences(retryOnUnavailable: true));
  }

  void _handleScroll() {
    if (!_scrollController.hasClients) return;
    final position = _scrollController.position;
    final shouldShowBackToRecent = position.pixels > 640;
    if (shouldShowBackToRecent != _showBackToRecent && mounted) {
      setState(() => _showBackToRecent = shouldShowBackToRecent);
    }
    _loadMoreIfNeeded(context.read<ConversationProvider>());
  }

  void _loadMoreIfNeeded(ConversationProvider provider) {
    if (!mounted || !_scrollController.hasClients || !provider.hasLoadedConversations) return;
    if (provider.loadMoreConversationsFailed) return;
    if (_scrollController.position.extentAfter < 720) {
      unawaited(provider.getMoreConversationsFromServer());
    }
  }

  void _checkPaginationAfterLayout(ConversationProvider provider) {
    WidgetsBinding.instance.addPostFrameCallback((_) => _loadMoreIfNeeded(provider));
  }

  void _scrollBackToRecent() {
    if (_sort != MemoryGallerySort.recent && mounted) {
      setState(() => _sort = MemoryGallerySort.recent);
    }
    if (!_scrollController.hasClients) return;
    _scrollController.animateTo(0, duration: const Duration(milliseconds: 320), curve: Curves.easeOutCubic);
  }

  Future<void> _refresh() async {
    if (_sort != MemoryGallerySort.recent && mounted) {
      setState(() => _sort = MemoryGallerySort.recent);
    }
    await context.read<ConversationProvider>().getInitialConversations();
    _resetArtworkQueueReadBudget();
    await _loadArtworkPreferences();
    await _refreshArtworkQueueStatus();
  }

  Future<void> _loadArtworkPreferences({bool retryOnUnavailable = false}) async {
    final loadSequence = ++_artworkPreferenceLoadSequence;
    final authorityEpoch = _artworkAuthorityEpoch;
    final preferences = await _artworkApi.preferences();
    if (!mounted || loadSequence != _artworkPreferenceLoadSequence || authorityEpoch != _artworkAuthorityEpoch) return;
    if (preferences == null) {
      if (retryOnUnavailable) _scheduleArtworkPreferencesRetry(loadSequence);
      return;
    }
    _artworkPreferenceUnavailableRetries = 0;
    if (mounted) setState(() => _artworkPreferences = preferences);
    if (preferences.releaseEnabled) unawaited(_refreshArtworkQueueStatus());
  }

  void _scheduleArtworkPreferencesRetry(int loadSequence) {
    if (_artworkPreferenceUnavailableRetries >= _maxArtworkPreferenceUnavailableRetries) return;
    _artworkPreferenceUnavailableRetries++;
    _artworkPreferencesRetryTimer?.cancel();
    _artworkPreferencesRetryTimer = Timer(const Duration(seconds: 1), () {
      if (!mounted || loadSequence != _artworkPreferenceLoadSequence) return;
      unawaited(_loadArtworkPreferences(retryOnUnavailable: true));
    });
  }

  bool _shouldPollArtworkQueue(MemoryArtworkQueueStatus status) {
    return status.controlState == MemoryArtworkQueueState.running &&
        (status.remaining > 0 || status.scanStatus != 'completed');
  }

  Future<void> _refreshArtworkQueueStatus() async {
    if (!mounted || _artworkPreferences?.releaseEnabled != true) return;
    _artworkQueuePollTimer?.cancel();
    if (!_artworkApi.isDisplayAuthorityCurrent() || _artworkQueueReads >= _maxArtworkQueueReads) {
      _stopArtworkQueueReads();
      return;
    }
    final refreshSequence = ++_artworkQueueRefreshSequence;
    final authorityEpoch = _artworkAuthorityEpoch;
    _artworkQueueReads++;
    bool isCurrent() =>
        mounted && refreshSequence == _artworkQueueRefreshSequence && authorityEpoch == _artworkAuthorityEpoch;
    MemoryArtworkQueueStatus? status;
    try {
      status = await _artworkApi.queueStatus();
    } on ExactAccountAuthorityChangedException {
      if (isCurrent()) _stopArtworkQueueReads();
      return;
    } catch (_) {
      // A failed read is unknown progress, not proof the server job stopped.
    }
    if (!isCurrent()) return;
    if (!_artworkApi.isDisplayAuthorityCurrent()) {
      _stopArtworkQueueReads();
      return;
    }
    if (status == null) {
      _artworkQueueUnavailableReads++;
      if (_artworkQueueUnavailableReads >= _maxArtworkQueueUnavailableReads ||
          _artworkQueueReads >= _maxArtworkQueueReads) {
        _stopArtworkQueueReads();
      } else {
        _scheduleArtworkQueueRead(refreshSequence, authorityEpoch);
      }
      return;
    }
    _artworkQueueUnavailableReads = 0;
    final previous = _artworkQueueStatus;
    final refreshVisibleArtwork = _artworkQueueManualRefreshPending ||
        previous == null ||
        status.styleVersion != previous.styleVersion ||
        status.generationId != previous.generationId ||
        status.ready > previous.ready ||
        (status.state == MemoryArtworkQueueState.completed && previous.state != MemoryArtworkQueueState.completed);
    setState(() {
      _artworkQueueStatus = status;
      _artworkQueueReadUnavailable = false;
      _artworkQueueManualRefreshPending = false;
      if (refreshVisibleArtwork) _artworkDisplayEpoch++;
    });
    if (_shouldPollArtworkQueue(status)) {
      if (_artworkQueueReads >= _maxArtworkQueueReads) {
        _stopArtworkQueueReads();
      } else {
        _scheduleArtworkQueueRead(refreshSequence, authorityEpoch);
      }
    }
  }

  void _scheduleArtworkQueueRead(int refreshSequence, int authorityEpoch) {
    _artworkQueuePollTimer = Timer(const Duration(seconds: 4), () {
      if (!mounted || refreshSequence != _artworkQueueRefreshSequence || authorityEpoch != _artworkAuthorityEpoch) {
        return;
      }
      unawaited(_refreshArtworkQueueStatus());
    });
  }

  void _resetArtworkQueueReadBudget() {
    _artworkQueueRefreshSequence++;
    _artworkQueuePollTimer?.cancel();
    _artworkQueueReads = 0;
    _artworkQueueUnavailableReads = 0;
    _artworkQueueManualRefreshPending = false;
  }

  void _stopArtworkQueueReads() {
    _artworkQueuePollTimer?.cancel();
    _artworkQueueManualRefreshPending = false;
    if (mounted) setState(() => _artworkQueueReadUnavailable = true);
  }

  Future<void> _retryArtworkQueueRead() async {
    if (!_artworkApi.isDisplayAuthorityCurrent() || _artworkPreferences?.releaseEnabled != true) return;
    _resetArtworkQueueReadBudget();
    // The first read may be unavailable; retain intent until this bounded cycle
    // receives a current success, rather than refreshing old day metadata.
    _artworkQueueManualRefreshPending = true;
    await _refreshArtworkQueueStatus();
  }

  /// Gallery browsing must never spend image allowance. A deliberate style
  /// change can request one cursorless, bounded recent preview instead.
  Future<MemoryArtworkBackfillPage?> _startArtworkPreview({bool restart = false}) async {
    if (restart) _artworkBackfillRestartPending = true;
    final preferences = _artworkPreferences;
    if (preferences == null || !preferences.releaseEnabled || _artworkBackfillInFlight) return null;
    _artworkBackfillRestartPending = false;
    _artworkBackfillInFlight = true;
    try {
      final page = await _artworkApi.backfillNext();
      if (page != null && mounted) unawaited(_refreshArtworkQueueStatus());
      return page;
    } finally {
      _artworkBackfillInFlight = false;
      if (_artworkBackfillRestartPending && mounted) {
        unawaited(_startArtworkPreview(restart: true));
      }
    }
  }

  Future<void> _selectArtworkStyle(String styleVersion) async {
    final preferences = _artworkPreferences;
    if (preferences == null || !preferences.releaseEnabled || !preferences.hasAcceptedConsent) {
      _showMessage(context.l10n.memoryArtworkStyleUnavailable);
      return;
    }
    final authorityEpoch = _artworkAuthorityEpoch;
    MemoryArtworkPreferenceUpdate result;
    try {
      result = await _artworkApi.setStyle(consentVersion: preferences.consentVersion, styleVersion: styleVersion);
    } on ExactAccountAuthorityChangedException {
      return;
    } catch (_) {
      result = const MemoryArtworkPreferenceUpdate(saved: false);
    }
    if (!mounted || authorityEpoch != _artworkAuthorityEpoch || !_artworkApi.isDisplayAuthorityCurrent()) return;
    if (!result.saved) {
      _showMessage(context.l10n.memoryArtworkStyleUnavailable);
      return;
    }
    setState(() {
      _artworkPreferences = result.preferences ??
          MemoryArtworkPreferences(
            consent: preferences.consent,
            consentVersion: preferences.consentVersion,
            styleVersion: styleVersion,
            releaseEnabled: preferences.releaseEnabled,
          );
      // A prior style can have a larger ready count. Resetting its queue
      // snapshot makes the new style's first status authoritative.
      _artworkQueueStatus = null;
      _resetArtworkQueueReadBudget();
      _artworkQueueReadUnavailable = false;
      _artworkDisplayEpoch++;
    });
    _showMessage(context.l10n.memoryArtworkStyleUpdated);
    unawaited(_startArtworkPreview(restart: true));
  }

  void _loadGalleryLayout() {
    final preferences = SharedPreferencesUtil();
    final saved = preferences.memoryArchiveGalleryLayout.isNotEmpty
        ? preferences.memoryArchiveGalleryLayout
        : preferences.memoryGalleryLayout;
    for (final layout in MemoryGalleryLayout.values) {
      if (layout.name == saved && mounted) {
        setState(() => _layout = layout);
        return;
      }
    }
  }

  Future<void> _selectGalleryLayout(MemoryGalleryLayout layout) async {
    if (mounted) setState(() => _layout = layout);
    await SharedPreferencesUtil().saveMemoryArchiveGalleryLayout(layout.name);
  }

  void _showMessage(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  @override
  Widget build(BuildContext context) {
    final conversationProvider = context.watch<ConversationProvider>();
    final capture = context.watch<CaptureProvider>();
    final live = capture.recordingState != RecordingState.stop || capture.segments.isNotEmpty;
    final orderedConversations = List<ServerConversation>.of(conversationProvider.visibleConversations)
      ..sort((a, b) {
        final result = (b.startedAt ?? b.createdAt).compareTo(a.startedAt ?? a.createdAt);
        return _sort == MemoryGallerySort.recent ? result : -result;
      });
    final groups = groupMemoryConversationsByDay(context, orderedConversations);
    final loading =
        groups.isEmpty && (!conversationProvider.hasLoadedConversations || conversationProvider.isLoadingConversations);
    _checkPaginationAfterLayout(conversationProvider);
    return Scaffold(
      appBar: AppBar(
        leadingWidth: 92,
        leading: TextButton.icon(
          key: const Key('memories-back-home'),
          onPressed: () => Navigator.pop(context),
          icon: const Icon(Icons.arrow_back_ios_new_rounded, size: 18),
          label: Text(context.l10n.bottomNavHome),
          style: TextButton.styleFrom(foregroundColor: EllaColors.tealDeep),
        ),
        title: Text(context.l10n.memories),
        actions: [
          PopupMenuButton<MemoryGalleryLayout>(
            key: const Key('memory-layout-menu'),
            tooltip: context.l10n.memoryGalleryView,
            initialValue: effectiveMemoryGalleryLayout(context, _layout),
            icon: const Icon(Icons.view_quilt_outlined, color: EllaColors.tealDeep),
            onSelected: _selectGalleryLayout,
            itemBuilder: memoryGalleryLayoutMenu,
          ),
          PopupMenuButton<MemoryGallerySort>(
            key: const Key('memory-sort-menu'),
            tooltip: context.l10n.sortBy,
            initialValue: _sort,
            icon: const Icon(Icons.swap_vert_rounded, color: EllaColors.tealDeep),
            onSelected: (value) => setState(() => _sort = value),
            itemBuilder: (context) => [
              PopupMenuItem(value: MemoryGallerySort.recent, child: Text(context.l10n.memorySortRecent)),
              PopupMenuItem(
                value: MemoryGallerySort.oldest,
                enabled: !conversationProvider.hasMoreConversations,
                child: Text(context.l10n.memorySortOldest),
              ),
            ],
          ),
          if (_artworkPreferences?.releaseEnabled == true)
            PopupMenuButton<String>(
              key: const Key('memory-artwork-style-menu'),
              tooltip: _artworkPreferences?.releaseEnabled == true
                  ? context.l10n.memoryArtworkStyle
                  : context.l10n.memoryArtworkStyleUnavailable,
              initialValue: _artworkPreferences?.styleVersion,
              icon: Icon(
                Icons.palette_outlined,
                color: _artworkPreferences?.releaseEnabled == true ? EllaColors.tealDeep : EllaColors.inkSoft,
              ),
              enabled: _artworkPreferences?.releaseEnabled == true,
              onSelected: _artworkPreferences?.releaseEnabled == true ? _selectArtworkStyle : null,
              itemBuilder: (context) => [
                PopupMenuItem(value: memoryArtworkDefaultStyle, child: Text(context.l10n.memoryArtworkSoftGouache)),
                PopupMenuItem(
                    value: memoryArtworkPaperCollageStyle, child: Text(context.l10n.memoryArtworkPaperCollage)),
                PopupMenuItem(
                  value: memoryArtworkGraphicLandscapeStyle,
                  child: Text(context.l10n.memoryArtworkGraphicLandscape),
                ),
                PopupMenuItem(
                  value: memoryArtworkWatercolorJournalStyle,
                  child: Text(context.l10n.memoryArtworkWatercolorJournal),
                ),
                PopupMenuItem(
                  value: memoryArtworkAnimeStorybookStyle,
                  child: Text(context.l10n.memoryArtworkAnimeStorybook),
                ),
                PopupMenuItem(
                  value: memoryArtworkCinematicStillStyle,
                  child: Text(context.l10n.memoryArtworkCinematicStill),
                ),
              ],
            ),
        ],
      ),
      body: RefreshIndicator(
        color: EllaColors.tealDeep,
        onRefresh: _refresh,
        child: CustomScrollView(
          key: const Key('ella-memories-list'),
          controller: _scrollController,
          slivers: [
            SliverToBoxAdapter(
              child: SizedBox(
                height: 3,
                child: conversationProvider.isLoadingConversations && groups.isNotEmpty
                    ? const LinearProgressIndicator(
                        key: Key('memories-refresh-indicator'),
                        color: EllaColors.tealDeep,
                        backgroundColor: EllaColors.cardDeep,
                      )
                    : null,
              ),
            ),
            if (_artworkQueueReadUnavailable)
              SliverToBoxAdapter(
                child: Padding(
                  key: const Key('memory-artwork-queue-read-unavailable'),
                  padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 8),
                  child: Row(children: [
                    Expanded(child: Text(context.l10n.memoryArtworkUnavailableLabel, style: EllaTextStyles.secondary)),
                    IconButton(
                      key: const Key('memory-artwork-queue-read-retry'),
                      tooltip: context.l10n.tryAgain,
                      onPressed: _artworkApi.isDisplayAuthorityCurrent() ? _retryArtworkQueueRead : null,
                      icon: const Icon(Icons.refresh_rounded, color: EllaColors.tealDeep),
                    ),
                  ]),
                ),
              ),
            if (live)
              SliverPadding(
                padding: const EdgeInsets.fromLTRB(20, 16, 20, 8),
                sliver: SliverToBoxAdapter(
                  child: _LiveMemoryCard(
                    onTap: () => Navigator.of(context).push(
                      MaterialPageRoute(
                        builder: (_) => ConversationCapturingPage(
                          topConversationId: conversationProvider.conversations.isEmpty
                              ? null
                              : conversationProvider.conversations.first.id,
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            if (loading)
              const SliverFillRemaining(
                hasScrollBody: false,
                child: Center(
                  child: SizedBox(
                    width: 24,
                    height: 24,
                    child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
                  ),
                ),
              )
            else
              for (final entry in groups.entries) ..._memoryGroupSlivers(entry),
            if (!loading && groups.isEmpty)
              SliverFillRemaining(
                hasScrollBody: false,
                child: Center(
                  child: Text(context.l10n.memoriesEmpty, textAlign: TextAlign.center, style: EllaTextStyles.body),
                ),
              ),
            if (conversationProvider.isLoadingMoreConversations)
              const SliverToBoxAdapter(
                child: Padding(
                  key: Key('memories-loading-more'),
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
            else if (conversationProvider.loadMoreConversationsFailed)
              SliverToBoxAdapter(
                child: Padding(
                  key: const Key('memories-load-more-failed'),
                  padding: const EdgeInsets.symmetric(vertical: 16),
                  child: Column(
                    children: [
                      Text(context.l10n.couldntLoadMoreMemories, style: EllaTextStyles.secondary),
                      const SizedBox(height: 4),
                      TextButton.icon(
                        key: const Key('retry-load-more-memories'),
                        onPressed: conversationProvider.getMoreConversationsFromServer,
                        icon: const Icon(Icons.refresh_rounded),
                        label: Text(context.l10n.tryAgain),
                      ),
                    ],
                  ),
                ),
              ),
            const SliverToBoxAdapter(child: SizedBox(height: 72)),
          ],
        ),
      ),
      floatingActionButton: _showBackToRecent
          ? FloatingActionButton.extended(
              key: const Key('back-to-recent-memories'),
              onPressed: _scrollBackToRecent,
              backgroundColor: EllaColors.tealDeep,
              foregroundColor: EllaColors.paper,
              icon: const Icon(Icons.arrow_upward_rounded),
              label: Text(context.l10n.backToRecentMemories),
            )
          : null,
      bottomNavigationBar: live || widget.onRecord != null
          ? _MemoryCaptureShelf(
              live: live,
              onTap: () {
                if (live) {
                  Navigator.of(context).push(
                    MaterialPageRoute(
                      builder: (_) => ConversationCapturingPage(
                        topConversationId: conversationProvider.conversations.isEmpty
                            ? null
                            : conversationProvider.conversations.first.id,
                      ),
                    ),
                  );
                  return;
                }
                final onRecord = widget.onRecord;
                Navigator.of(context).pop();
                if (onRecord != null) Future<void>.microtask(onRecord);
              },
            )
          : null,
    );
  }

  List<Widget> _memoryGroupSlivers(MapEntry<String, List<ServerConversation>> entry) {
    if (_layout == MemoryGalleryLayout.days) {
      return [
        SliverPadding(
          padding: const EdgeInsets.fromLTRB(20, 18, 20, 0),
          sliver: SliverToBoxAdapter(
            child: MemoryDayGalleryCard(
              dayLabel: entry.key,
              memories: entry.value,
              artworkApi: _artworkApi,
              artworkRefreshEpoch: _artworkDisplayEpoch,
              artworkAuthorityEpoch: _artworkAuthorityEpoch,
              artworkQueueReadUnavailable: _artworkQueueReadUnavailable,
              onOpen: () => Navigator.of(context).push(
                MaterialPageRoute(
                  builder: (_) => EllaMemoryDayPage(
                    dayLabel: entry.key,
                    memories: entry.value,
                    artworkApi: _artworkApi,
                    artworkRefreshEpoch: _artworkDisplayEpoch,
                    artworkAuthorityEpoch: _artworkAuthorityEpoch,
                    exactAuthority: WalOwnerAuthority.active(),
                    authorityChanges: SharedPreferencesUtil.aiConsentAuthorityChanges,
                    onDelete: _deleteMemory,
                  ),
                ),
              ),
            ),
          ),
        ),
      ];
    }
    final children = <Widget>[
      SliverPadding(
        padding: const EdgeInsets.fromLTRB(20, 24, 20, EllaSizes.cardGap),
        sliver: SliverToBoxAdapter(child: Text(entry.key, style: EllaTextStyles.eyebrow)),
      ),
    ];
    if (_layout == MemoryGalleryLayout.grid) {
      children.add(
        SliverPadding(
          padding: const EdgeInsets.symmetric(horizontal: 20),
          sliver: memoryGalleryFeedSliver(
            layout: _layout,
            itemCount: entry.value.length,
            itemBuilder: (context, index) => _memoryCard(entry.value[index]),
          ),
        ),
      );
    } else {
      children.add(
        SliverPadding(
          padding: const EdgeInsets.symmetric(horizontal: 20),
          sliver: SliverList.separated(
            itemCount: entry.value.length,
            separatorBuilder: (_, __) => const Divider(height: 1, color: EllaColors.cardDeep),
            itemBuilder: (context, index) => _memoryCard(entry.value[index]),
          ),
        ),
      );
    }
    return children;
  }

  Widget _memoryCard(ServerConversation conversation) => MemoryGalleryCard(
        conversation: conversation,
        layout: _layout,
        artworkApi: _artworkApi,
        artworkRefreshEpoch: _artworkDisplayEpoch,
        artworkAuthorityEpoch: _artworkAuthorityEpoch,
        artworkQueueReadUnavailable: _artworkQueueReadUnavailable,
        onOpen: () => _openMemory(conversation),
        onDelete: () => _deleteMemory(conversation),
      );

  void _openMemory(ServerConversation conversation) {
    Navigator.of(context).push(MaterialPageRoute(builder: (_) => ConversationDetailPage(conversation: conversation)));
  }

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
    if (!deleted && mounted) _showMessage(l10n.failedToDeleteConversations);
    return deleted;
  }
}

class MemoryDayGalleryCard extends StatefulWidget {
  const MemoryDayGalleryCard({
    super.key,
    required this.dayLabel,
    required this.memories,
    required this.onOpen,
    this.artworkApi,
    this.artworkRefreshEpoch = 0,
    this.artworkAuthorityEpoch = 0,
    this.artworkQueueReadUnavailable = false,
    this.automaticRepairMemoryIds = const <String>{},
    this.now,
  });

  final String dayLabel;
  final List<ServerConversation> memories;
  final VoidCallback onOpen;
  final MemoryArtworkApi? artworkApi;
  final int artworkRefreshEpoch;
  final int artworkAuthorityEpoch;
  final bool artworkQueueReadUnavailable;
  final Set<String> automaticRepairMemoryIds;
  final DateTime? now;

  @override
  State<MemoryDayGalleryCard> createState() => _MemoryDayGalleryCardState();
}

class _MemoryDayGalleryCardState extends State<MemoryDayGalleryCard> {
  MemoryArtworkDay? _dayArtwork;
  bool _dayBatchResolved = false;
  bool _dayBatchFailed = false;
  int _loadGeneration = 0;

  @override
  void initState() {
    super.initState();
    unawaited(_loadDayArtwork());
  }

  @override
  void didUpdateWidget(covariant MemoryDayGalleryCard oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.artworkApi != widget.artworkApi ||
        oldWidget.artworkRefreshEpoch != widget.artworkRefreshEpoch ||
        oldWidget.artworkAuthorityEpoch != widget.artworkAuthorityEpoch ||
        _dayKey(oldWidget.memories) != _dayKey(widget.memories)) {
      unawaited(_loadDayArtwork());
    }
  }

  Future<void> _loadDayArtwork() async {
    final generation = ++_loadGeneration;
    final memories = widget.memories;
    if (memories.isEmpty) return;
    if (!_usesDayBatch) return;
    if (mounted && _usesDayBatch) {
      setState(() {
        _dayArtwork = null;
        _dayBatchResolved = false;
        _dayBatchFailed = false;
      });
    }
    final localDay = memories.first.createdAt.toLocal();
    MemoryArtworkDay? result;
    try {
      result = await (widget.artworkApi ?? MemoryArtworkApi()).fetchDay(
        DateTime(localDay.year, localDay.month, localDay.day),
        utcOffsetMinutes: localDay.timeZoneOffset.inMinutes,
        authorityRevision: widget.artworkAuthorityEpoch,
        contentRevision: widget.artworkRefreshEpoch,
      );
    } catch (_) {
      // A failed batch GET is terminal until the user explicitly retries it.
    }
    if (!mounted || generation != _loadGeneration) return;
    setState(() {
      _dayArtwork = result;
      _dayBatchResolved = true;
      _dayBatchFailed = result == null;
    });
  }

  static String _dayKey(List<ServerConversation> memories) =>
      memories.isEmpty ? '' : memoryConversationCalendarDayKey(memories.first);

  bool get _usesDayBatch => (widget.artworkApi ?? MemoryArtworkApi()).supportsDayArtworkBatch;

  @override
  Widget build(BuildContext context) {
    final titles = widget.memories
        .take(3)
        .map((memory) => parseEllaDisplayValue(memory.structured.title).text.trim())
        .where((title) => title.isNotEmpty)
        .join(' · ');
    return Semantics(
      button: true,
      label: context.l10n.memoryDayOpen(widget.dayLabel, widget.memories.length),
      child: Material(
        key: Key('memory-day-${memoryConversationCalendarDayKey(widget.memories.first)}'),
        color: Colors.transparent,
        borderRadius: BorderRadius.circular(8),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          onTap: widget.onOpen,
          child: _isRecentDay ? _recentDayContent(context, titles) : _olderDayContent(context, titles),
        ),
      ),
    );
  }

  bool get _isRecentDay {
    final memoryDay = widget.memories.first.createdAt.toLocal();
    final current = (widget.now ?? DateTime.now()).toLocal();
    final day = DateTime(memoryDay.year, memoryDay.month, memoryDay.day);
    final today = DateTime(current.year, current.month, current.day);
    return today.difference(day).inDays <= 1;
  }

  Widget _recentDayContent(BuildContext context, String titles) => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          SizedBox(
            height: 96,
            child: _MemoryDayArtworkCollage(
              memories: widget.memories,
              artworkApi: widget.artworkApi,
              artworkRefreshEpoch: widget.artworkRefreshEpoch,
              artworkAuthorityEpoch: widget.artworkAuthorityEpoch,
              artworkQueueReadUnavailable: widget.artworkQueueReadUnavailable,
              automaticRepairMemoryIds: _dayBatchFailed ? const <String>{} : widget.automaticRepairMemoryIds,
              prefetchedArtwork: _dayArtwork?.items ?? const <String, MemoryArtworkResult>{},
              dayBatchResolved: _dayBatchResolved,
            ),
          ),
          if (_dayBatchFailed) _dayArtworkRetry(context),
          _dayDescription(context, titles),
        ],
      );

  Widget _olderDayContent(BuildContext context, String titles) {
    final memory = widget.memories.first;
    return ConstrainedBox(
      key: Key('memory-day-compact-${memoryConversationCalendarDayKey(memory)}'),
      constraints: const BoxConstraints(minHeight: 96),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 72,
            height: 72,
            child: Stack(children: [
              Positioned.fill(
                  child: MemoryArtworkImage(
                conversation: memory,
                api: widget.artworkApi,
                refreshEpoch: widget.artworkRefreshEpoch,
                authorityEpoch: widget.artworkAuthorityEpoch,
                allowManualGeneration: false,
                compactPlaceholder: true,
                prefetchedResult:
                    _artworkWithQueueReadState(_dayArtwork?.items[memory.id], widget.artworkQueueReadUnavailable),
                prefetchResolved: _dayBatchResolved,
                deferRemoteFetch: _usesDayBatch && !_dayBatchResolved,
              )),
              if (_dayBatchFailed) Positioned(right: 4, bottom: 4, child: _dayArtworkRetry(context, compact: true)),
            ]),
          ),
          Expanded(child: _dayDescription(context, titles, compact: true)),
        ],
      ),
    );
  }

  Widget _dayArtworkRetry(BuildContext context, {bool compact = false}) => Align(
        alignment: Alignment.bottomRight,
        child: DecoratedBox(
          decoration: BoxDecoration(color: EllaColors.card, borderRadius: BorderRadius.circular(8)),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (!compact)
                Flexible(
                  child: Padding(
                    padding: const EdgeInsets.only(left: 8),
                    child: Text(context.l10n.memoryArtworkUnavailableLabel, style: EllaTextStyles.secondary),
                  ),
                ),
              IconButton(
                key: const Key('memory-day-artwork-retry'),
                tooltip: '${context.l10n.memoryArtworkUnavailableLabel}. ${context.l10n.tryAgain}',
                constraints: const BoxConstraints.tightFor(width: 48, height: 48),
                onPressed: () => unawaited(_loadDayArtwork()),
                icon: const Icon(Icons.refresh_rounded, color: EllaColors.tealDeep),
              ),
            ],
          ),
        ),
      );

  Widget _dayDescription(BuildContext context, String titles, {bool compact = false}) => Padding(
        padding: EdgeInsets.all(compact ? 12 : 16),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              child: Column(
                mainAxisAlignment: compact ? MainAxisAlignment.center : MainAxisAlignment.start,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    widget.dayLabel,
                    style: EllaTextStyles.body.copyWith(fontWeight: FontWeight.w700),
                  ),
                  const SizedBox(height: 4),
                  Text(
                    context.l10n.memoryDayCount(widget.memories.length),
                    style: EllaTextStyles.secondary,
                  ),
                  if (titles.isNotEmpty) ...[
                    const SizedBox(height: 6),
                    Text(titles, style: EllaTextStyles.secondary),
                  ],
                ],
              ),
            ),
            const Icon(Icons.chevron_right_rounded, color: EllaColors.tealDeep),
          ],
        ),
      );
}

class _MemoryDayArtworkCollage extends StatelessWidget {
  const _MemoryDayArtworkCollage({
    required this.memories,
    this.artworkApi,
    this.artworkRefreshEpoch = 0,
    this.artworkAuthorityEpoch = 0,
    this.artworkQueueReadUnavailable = false,
    this.automaticRepairMemoryIds = const <String>{},
    this.prefetchedArtwork = const <String, MemoryArtworkResult>{},
    this.dayBatchResolved = false,
  });

  final List<ServerConversation> memories;
  final MemoryArtworkApi? artworkApi;
  final int artworkRefreshEpoch;
  final int artworkAuthorityEpoch;
  final bool artworkQueueReadUnavailable;
  final Set<String> automaticRepairMemoryIds;
  final Map<String, MemoryArtworkResult> prefetchedArtwork;
  final bool dayBatchResolved;

  Widget _art(ServerConversation memory) => MemoryArtworkImage(
        conversation: memory,
        api: artworkApi,
        refreshEpoch: artworkRefreshEpoch,
        authorityEpoch: artworkAuthorityEpoch,
        enqueueIfMissing: automaticRepairMemoryIds.contains(memory.id),
        allowManualGeneration: true,
        compactPlaceholder: true,
        prefetchedResult: _artworkWithQueueReadState(prefetchedArtwork[memory.id], artworkQueueReadUnavailable),
        prefetchResolved: dayBatchResolved,
        deferRemoteFetch: (artworkApi ?? MemoryArtworkApi()).supportsDayArtworkBatch && !dayBatchResolved,
      );

  @override
  Widget build(BuildContext context) {
    final panels = memories.take(4).toList(growable: false);
    return LayoutBuilder(
      builder: (context, constraints) {
        const gap = 2.0;
        final width = constraints.maxWidth;
        final height = constraints.maxHeight;
        final halfWidth = (width - gap) / 2;
        final halfHeight = (height - gap) / 2;
        final quarterWidth = (halfWidth - gap) / 2;

        Rect panelRect(int index) {
          if (panels.length == 1) return Rect.fromLTWH(0, 0, width, height);
          if (panels.length == 2) {
            return Rect.fromLTWH(index == 0 ? 0 : halfWidth + gap, 0, halfWidth, height);
          }
          if (index == 0) return Rect.fromLTWH(0, 0, halfWidth, height);
          if (index == 1) return Rect.fromLTWH(halfWidth + gap, 0, halfWidth, halfHeight);
          if (panels.length == 3) {
            return Rect.fromLTWH(halfWidth + gap, halfHeight + gap, halfWidth, halfHeight);
          }
          return Rect.fromLTWH(
            halfWidth + gap + (index == 3 ? quarterWidth + gap : 0),
            halfHeight + gap,
            quarterWidth,
            halfHeight,
          );
        }

        // Keep every memory under one stable parent. Changing the panel count
        // must move an existing image, not recreate it and lose its cache state.
        return Stack(
          children: [
            for (var index = 0; index < panels.length; index++)
              Positioned.fromRect(
                key: ValueKey('memory-day-artwork-${panels[index].id}'),
                rect: panelRect(index),
                child: _art(panels[index]),
              ),
          ],
        );
      },
    );
  }
}

class EllaMemoryDayPage extends StatefulWidget {
  const EllaMemoryDayPage({
    super.key,
    required this.dayLabel,
    required this.memories,
    required this.onDelete,
    this.artworkApi,
    this.exactAuthority,
    this.authorityChanges,
    this.artworkRefreshEpoch = 0,
    this.artworkAuthorityEpoch = 0,
  });

  final String dayLabel;
  final List<ServerConversation> memories;
  final Future<bool> Function(ServerConversation conversation) onDelete;
  final MemoryArtworkApi? artworkApi;
  final ExactAccountAuthorityVerifier? exactAuthority;
  final Listenable? authorityChanges;
  final int artworkRefreshEpoch;
  final int artworkAuthorityEpoch;

  @override
  State<EllaMemoryDayPage> createState() => _EllaMemoryDayPageState();
}

class _EllaMemoryDayPageState extends State<EllaMemoryDayPage> {
  late final List<ServerConversation> _memories;
  late final ExactAccountAuthorityVerifier? _exactAuthority;
  late final Listenable _authorityChanges;

  @override
  void initState() {
    super.initState();
    _memories = List.of(widget.memories);
    _exactAuthority = widget.exactAuthority ?? WalOwnerAuthority.active();
    _authorityChanges = widget.authorityChanges ?? SharedPreferencesUtil.aiConsentAuthorityChanges;
    _authorityChanges.addListener(_handleAuthorityChanged);
    if (_exactAuthority == null && SharedPreferencesUtil.isPublicBuild) {
      _memories.clear();
      WidgetsBinding.instance.addPostFrameCallback((_) => _dismissIfMounted());
    }
  }

  void _handleAuthorityChanged() {
    final authority = _exactAuthority;
    if (authority == null || authority.isExactCurrent()) return;
    if (mounted) setState(_memories.clear);
    WidgetsBinding.instance.addPostFrameCallback((_) => _dismissIfMounted());
  }

  void _dismissIfMounted() {
    if (!mounted) return;
    final route = ModalRoute.of(context);
    if (route == null) return;
    final navigator = Navigator.of(context);
    navigator.popUntil((candidate) => identical(candidate, route));
    if (route.isCurrent) navigator.pop();
  }

  @override
  void dispose() {
    _authorityChanges.removeListener(_handleAuthorityChanged);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(widget.dayLabel)),
      body: ListView.separated(
        key: const Key('memory-day-list'),
        padding: const EdgeInsets.fromLTRB(20, 16, 20, 40),
        itemCount: _memories.length,
        separatorBuilder: (_, __) => const SizedBox(height: EllaSizes.cardGap),
        itemBuilder: (context, index) {
          final memory = _memories[index];
          return MemoryGalleryCard(
            conversation: memory,
            layout: MemoryGalleryLayout.journal,
            artworkApi: widget.artworkApi,
            artworkRefreshEpoch: widget.artworkRefreshEpoch,
            artworkAuthorityEpoch: widget.artworkAuthorityEpoch,
            onOpen: () => Navigator.of(
              context,
            ).push(MaterialPageRoute(builder: (_) => ConversationDetailPage(conversation: memory))),
            onDelete: () async {
              final deleted = await widget.onDelete(memory);
              if (deleted && mounted) setState(() => _memories.remove(memory));
              return deleted;
            },
          );
        },
      ),
    );
  }
}

class _MemoryCaptureShelf extends StatelessWidget {
  const _MemoryCaptureShelf({required this.live, required this.onTap});

  final bool live;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      top: false,
      minimum: const EdgeInsets.fromLTRB(20, 8, 20, 10),
      child: Material(
        color: EllaColors.tealDeep,
        borderRadius: BorderRadius.circular(18),
        child: InkWell(
          key: const Key('memories-record-shelf'),
          onTap: onTap,
          borderRadius: BorderRadius.circular(18),
          child: ConstrainedBox(
            constraints: const BoxConstraints(minHeight: 54),
            child: Row(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Icon(live ? Icons.subject_rounded : Icons.mic_none_rounded, color: EllaColors.paper),
                const SizedBox(width: 10),
                Text(
                  live ? context.l10n.liveTranscript : context.l10n.todayDockRecord,
                  style: EllaTextStyles.body.copyWith(color: EllaColors.paper, fontWeight: FontWeight.w700),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _LiveMemoryCard extends StatelessWidget {
  const _LiveMemoryCard({required this.onTap});

  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: EllaColors.card,
      borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
        child: ConstrainedBox(
          constraints: const BoxConstraints(minHeight: 70),
          child: Padding(
            padding: const EdgeInsets.all(EllaSizes.cardPadding),
            child: Row(
              children: [
                const EllaBreathingDot(live: true),
                const SizedBox(width: 16),
                Expanded(child: Text(context.l10n.inProgress, style: EllaTextStyles.display)),
                const Icon(Icons.chevron_right_rounded, color: EllaColors.inkSoft),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class MemoryGalleryCard extends StatelessWidget {
  const MemoryGalleryCard({
    super.key,
    required this.conversation,
    required this.layout,
    required this.onOpen,
    this.displayTitle,
    this.onDelete,
    this.artworkApi,
    this.artworkRefreshEpoch = 0,
    this.artworkAuthorityEpoch = 0,
    this.enqueueArtworkIfMissing = false,
    this.artworkQueueReadUnavailable = false,
  });

  final ServerConversation conversation;
  final MemoryGalleryLayout layout;
  final VoidCallback onOpen;
  final String? displayTitle;
  final Future<bool> Function()? onDelete;
  final MemoryArtworkApi? artworkApi;
  final int artworkRefreshEpoch;
  final int artworkAuthorityEpoch;
  final bool enqueueArtworkIfMissing;
  final bool artworkQueueReadUnavailable;

  String get _title => displayTitle ?? conversation.structured.title;

  @override
  Widget build(BuildContext context) {
    final largeText = MediaQuery.textScalerOf(context).scale(1) >= 2;
    final details = _MemoryDetails(conversation: conversation, title: _title, showTime: !largeText);
    final artwork = ClipRRect(
      borderRadius: BorderRadius.circular(8),
      child: SizedBox(
        width: 64,
        height: 64,
        child: MemoryArtworkImage(
          conversation: conversation,
          api: artworkApi,
          refreshEpoch: artworkRefreshEpoch,
          authorityEpoch: artworkAuthorityEpoch,
          compactPlaceholder: true,
          allowManualGeneration: true,
          enqueueIfMissing: enqueueArtworkIfMissing,
          prefetchedResult: artworkQueueReadUnavailable ? _queueReadUnavailableArtwork : null,
        ),
      ),
    );
    final child = Padding(
      padding: const EdgeInsets.symmetric(vertical: 16),
      child: largeText
          ? Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Row(children: [
                  artwork,
                  const SizedBox(width: 12),
                  Expanded(child: _MemoryTime(conversation: conversation)),
                  const Icon(Icons.chevron_right_rounded, color: EllaColors.tealDeep)
                ]),
                const SizedBox(height: 12),
                details,
              ],
            )
          : Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                artwork,
                const SizedBox(width: 12),
                Expanded(child: details),
                const SizedBox(width: 6),
                const Icon(Icons.chevron_right_rounded, color: EllaColors.tealDeep),
              ],
            ),
    );
    final card = Material(
      color: Colors.transparent,
      borderRadius: BorderRadius.circular(8),
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onOpen,
        child: ConstrainedBox(
          constraints: const BoxConstraints(minHeight: EllaSizes.minTouchTarget),
          child: KeyedSubtree(key: Key('memory-layout-${layout.name}-${conversation.id}'), child: child),
        ),
      ),
    );
    return Semantics(
      customSemanticsActions: {
        if (onDelete != null) CustomSemanticsAction(label: context.l10n.delete): () => onDelete!(),
      },
      child: Dismissible(
        key: Key('memory-card-${conversation.id}'),
        direction: onDelete == null ? DismissDirection.startToEnd : DismissDirection.horizontal,
        confirmDismiss: (direction) async {
          if (direction == DismissDirection.startToEnd) {
            onOpen();
            return false;
          }
          final delete = onDelete;
          if (delete == null) return false;
          return delete();
        },
        background: const _MemorySwipeBackground(
          alignment: AlignmentDirectional.centerStart,
          icon: Icons.edit_outlined,
          color: EllaColors.tealDeep,
        ),
        secondaryBackground: const _MemorySwipeBackground(
          alignment: AlignmentDirectional.centerEnd,
          icon: Icons.delete_outline_rounded,
          color: EllaColors.error,
        ),
        child: card,
      ),
    );
  }
}

class _MemorySwipeBackground extends StatelessWidget {
  const _MemorySwipeBackground({required this.alignment, required this.icon, required this.color});

  final AlignmentGeometry alignment;
  final IconData icon;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return DecoratedBox(
      decoration: BoxDecoration(color: color, borderRadius: BorderRadius.circular(EllaSizes.cardRadius)),
      child: Align(
        alignment: alignment,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 24),
          child: Icon(icon, color: EllaColors.paper, semanticLabel: null),
        ),
      ),
    );
  }
}

class _MemoryDetails extends StatelessWidget {
  const _MemoryDetails({required this.conversation, required this.title, this.showTime = true});

  final ServerConversation conversation;
  final String title;
  final bool showTime;

  @override
  Widget build(BuildContext context) {
    final titleIsEllaGenerated = parseEllaDisplayValue(conversation.structured.title).isEllaGenerated;
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              EllaSourceText(
                title,
                isEllaGenerated: titleIsEllaGenerated,
                style: EllaTextStyles.body.copyWith(fontWeight: FontWeight.w600),
              ),
              const SizedBox(height: 4),
              EllaSourceText(
                conversation.structured.overview,
                maxLines: 3,
                overflow: TextOverflow.ellipsis,
                style: EllaTextStyles.secondary,
              ),
              const SizedBox(height: 6),
              if (showTime) _MemoryTime(conversation: conversation),
              if (hasCurrentHermesSummary(conversation)) ...[
                const SizedBox(height: 6),
                HermesSummarySource(conversation: conversation),
              ],
            ],
          ),
        ),
      ],
    );
  }
}

class _MemoryTime extends StatelessWidget {
  const _MemoryTime({required this.conversation});

  final ServerConversation conversation;

  @override
  Widget build(BuildContext context) => Text(
        MaterialLocalizations.of(context).formatTimeOfDay(
          TimeOfDay.fromDateTime((conversation.startedAt ?? conversation.createdAt).toLocal()),
        ),
        style: EllaTextStyles.caption,
      );
}

/// Phone layouts stay list-first; a wide grid grows naturally with the text.
Widget memoryGalleryFeedSliver({
  required MemoryGalleryLayout layout,
  required int itemCount,
  required IndexedWidgetBuilder itemBuilder,
}) =>
    SliverLayoutBuilder(
      builder: (context, constraints) {
        final columns = layout == MemoryGalleryLayout.grid &&
                constraints.crossAxisExtent >= 640 &&
                MediaQuery.textScalerOf(context).scale(1) < 2
            ? 2
            : 1;
        return SliverList.separated(
          itemCount: (itemCount / columns).ceil(),
          separatorBuilder: (_, __) => const Divider(height: 1, color: EllaColors.cardDeep),
          itemBuilder: (context, row) => columns == 1
              ? itemBuilder(context, row)
              : Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Expanded(child: itemBuilder(context, row * columns)),
                    const SizedBox(width: 20),
                    Expanded(
                        child:
                            row * columns + 1 < itemCount ? itemBuilder(context, row * columns + 1) : const SizedBox()),
                  ],
                ),
        );
      },
    );

Map<String, List<ServerConversation>> groupMemoryConversationsByDay(
  BuildContext context,
  List<ServerConversation> conversations, {
  DateTime? now,
}) {
  final current = now ?? DateTime.now();
  final today = DateTime(current.year, current.month, current.day);
  final result = <String, List<ServerConversation>>{};
  for (final conversation in conversations) {
    final value = (conversation.startedAt ?? conversation.createdAt).toLocal();
    final day = DateTime(value.year, value.month, value.day);
    final label = day == today
        ? context.l10n.memoriesToday
        : day == today.subtract(const Duration(days: 1))
            ? context.l10n.memoriesYesterday
            : DateFormat('EEEE · MMMM d').format(day).toUpperCase();
    result.putIfAbsent(label, () => []).add(conversation);
  }
  return result;
}

String memoryConversationCalendarDayKey(ServerConversation conversation) {
  final value = (conversation.startedAt ?? conversation.createdAt).toLocal();
  final year = value.year.toString().padLeft(4, '0');
  final month = value.month.toString().padLeft(2, '0');
  final day = value.day.toString().padLeft(2, '0');
  return '$year-$month-$day';
}
