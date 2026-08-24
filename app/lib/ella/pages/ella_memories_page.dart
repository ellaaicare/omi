import 'dart:async';

import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/memory_artwork_api.dart';
import 'package:omi/ella/widgets/ella_breathing_dot.dart';
import 'package:omi/ella/widgets/memory_artwork_image.dart';
import 'package:omi/pages/conversation_capturing/page.dart';
import 'package:omi/pages/conversation_detail/page.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/utils/enums.dart';
import 'package:omi/utils/l10n_extensions.dart';

enum MemoryGalleryLayout { journal, grid, list }

enum MemoryGallerySort { recent, oldest }

class EllaMemoriesPage extends StatefulWidget {
  const EllaMemoriesPage({super.key, this.artworkApi, this.onRecord});

  final MemoryArtworkApi? artworkApi;
  final VoidCallback? onRecord;

  @override
  State<EllaMemoriesPage> createState() => _EllaMemoriesPageState();
}

class _EllaMemoriesPageState extends State<EllaMemoriesPage> {
  final ScrollController _scrollController = ScrollController();
  late final MemoryArtworkApi _artworkApi = widget.artworkApi ?? MemoryArtworkApi();
  MemoryArtworkPreferences? _artworkPreferences;
  MemoryGalleryLayout _layout = MemoryGalleryLayout.journal;
  MemoryGallerySort _sort = MemoryGallerySort.recent;
  bool _showBackToRecent = false;

  @override
  void initState() {
    super.initState();
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
    _scrollController
      ..removeListener(_handleScroll)
      ..dispose();
    super.dispose();
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
    if (!_scrollController.hasClients) return;
    _scrollController.animateTo(0, duration: const Duration(milliseconds: 320), curve: Curves.easeOutCubic);
  }

  Future<void> _refresh() async {
    await context.read<ConversationProvider>().getInitialConversations();
    await _loadArtworkPreferences();
  }

  Future<void> _loadArtworkPreferences() async {
    final preferences = await _artworkApi.preferences();
    if (mounted) setState(() => _artworkPreferences = preferences);
  }

  Future<void> _selectArtworkStyle(String styleVersion) async {
    final preferences = _artworkPreferences;
    if (preferences == null || !preferences.releaseEnabled) {
      _showMessage(context.l10n.memoryArtworkStyleUnavailable);
      return;
    }
    final saved = await _artworkApi.setStyle(consentVersion: preferences.consentVersion, styleVersion: styleVersion);
    final queued = saved && await _artworkApi.backfillRecent();
    if (!mounted) return;
    if (!saved) {
      _showMessage(context.l10n.memoryArtworkStyleUnavailable);
      return;
    }
    setState(
      () => _artworkPreferences = MemoryArtworkPreferences(
        consent: 'accepted',
        consentVersion: preferences.consentVersion,
        styleVersion: styleVersion,
        releaseEnabled: preferences.releaseEnabled,
      ),
    );
    _showMessage(queued ? context.l10n.memoryArtworkStyleUpdated : context.l10n.memoryArtworkStyleUnavailable);
  }

  void _loadGalleryLayout() {
    final saved = SharedPreferencesUtil().memoryGalleryLayout;
    for (final layout in MemoryGalleryLayout.values) {
      if (layout.name == saved && mounted) {
        setState(() => _layout = layout);
        return;
      }
    }
  }

  Future<void> _selectGalleryLayout(MemoryGalleryLayout layout) async {
    if (mounted) setState(() => _layout = layout);
    await SharedPreferencesUtil().saveMemoryGalleryLayout(layout.name);
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
    final groups = _group(context, orderedConversations);
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
            initialValue: _layout,
            icon: const Icon(Icons.view_quilt_outlined, color: EllaColors.tealDeep),
            onSelected: _selectGalleryLayout,
            itemBuilder: (context) => [
              PopupMenuItem(value: MemoryGalleryLayout.journal, child: Text(context.l10n.memoryGalleryJournal)),
              PopupMenuItem(value: MemoryGalleryLayout.grid, child: Text(context.l10n.memoryGalleryGrid)),
              PopupMenuItem(value: MemoryGalleryLayout.list, child: Text(context.l10n.memoryGalleryList)),
            ],
          ),
          PopupMenuButton<MemoryGallerySort>(
            key: const Key('memory-sort-menu'),
            tooltip: context.l10n.sortBy,
            initialValue: _sort,
            icon: const Icon(Icons.swap_vert_rounded, color: EllaColors.tealDeep),
            onSelected: (value) => setState(() => _sort = value),
            itemBuilder: (context) => [
              PopupMenuItem(value: MemoryGallerySort.recent, child: Text(context.l10n.memorySortRecent)),
              PopupMenuItem(value: MemoryGallerySort.oldest, child: Text(context.l10n.memorySortOldest)),
            ],
          ),
          PopupMenuButton<String>(
            key: const Key('memory-artwork-style-menu'),
            tooltip: context.l10n.memoryArtworkStyle,
            initialValue: _artworkPreferences?.styleVersion,
            icon: const Icon(Icons.palette_outlined, color: EllaColors.tealDeep),
            onSelected: _selectArtworkStyle,
            itemBuilder: (context) => [
              PopupMenuItem(value: memoryArtworkDefaultStyle, child: Text(context.l10n.memoryArtworkSoftGouache)),
              PopupMenuItem(value: memoryArtworkPaperCollageStyle, child: Text(context.l10n.memoryArtworkPaperCollage)),
              PopupMenuItem(
                value: memoryArtworkGraphicLandscapeStyle,
                child: Text(context.l10n.memoryArtworkGraphicLandscape),
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
          sliver: SliverGrid(
            gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
              maxCrossAxisExtent: 430,
              mainAxisSpacing: EllaSizes.cardGap,
              crossAxisSpacing: EllaSizes.cardGap,
              childAspectRatio: 0.86,
            ),
            delegate: SliverChildBuilderDelegate(
              (context, index) => _memoryCard(entry.value[index]),
              childCount: entry.value.length,
            ),
          ),
        ),
      );
    } else {
      children.add(
        SliverPadding(
          padding: const EdgeInsets.symmetric(horizontal: 20),
          sliver: SliverList.separated(
            itemCount: entry.value.length,
            separatorBuilder: (_, __) => const SizedBox(height: EllaSizes.cardGap),
            itemBuilder: (context, index) => _memoryCard(entry.value[index]),
          ),
        ),
      );
    }
    return children;
  }

  Widget _memoryCard(ServerConversation conversation) => _MemoryCard(
        conversation: conversation,
        layout: _layout,
        onTap: () => Navigator.of(
          context,
        ).push(MaterialPageRoute(builder: (_) => ConversationDetailPage(conversation: conversation))),
      );
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

class _MemoryCard extends StatelessWidget {
  const _MemoryCard({required this.conversation, required this.layout, required this.onTap});

  final ServerConversation conversation;
  final MemoryGalleryLayout layout;
  final VoidCallback onTap;

  String get _title =>
      conversation.structured.title.replaceFirst(RegExp(r'^🪽\s*'), '').replaceFirst(RegExp(r'^\[Ella\]\s*'), '');

  @override
  Widget build(BuildContext context) {
    final details = _MemoryDetails(conversation: conversation, title: _title);
    final child = layout == MemoryGalleryLayout.list
        ? Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              SizedBox(width: 112, height: 112, child: MemoryArtworkImage(conversation: conversation)),
              Expanded(
                child: Padding(padding: const EdgeInsets.all(14), child: details),
              ),
            ],
          )
        : Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              AspectRatio(
                aspectRatio: layout == MemoryGalleryLayout.journal ? 2.1 : 1.45,
                child: MemoryArtworkImage(conversation: conversation),
              ),
              Padding(padding: const EdgeInsets.all(16), child: details),
            ],
          );
    return Material(
      key: Key('memory-card-${conversation.id}'),
      color: EllaColors.card,
      borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onTap,
        child: ConstrainedBox(
          constraints: const BoxConstraints(minHeight: 112),
          child: KeyedSubtree(key: Key('memory-layout-${layout.name}-${conversation.id}'), child: child),
        ),
      ),
    );
  }
}

class _MemoryDetails extends StatelessWidget {
  const _MemoryDetails({required this.conversation, required this.title});

  final ServerConversation conversation;
  final String title;

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                title,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: EllaTextStyles.body.copyWith(fontWeight: FontWeight.w600),
              ),
              const SizedBox(height: 4),
              Text(
                conversation.structured.overview,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: EllaTextStyles.secondary,
              ),
            ],
          ),
        ),
        const SizedBox(width: 6),
        const Icon(Icons.chevron_right_rounded, color: EllaColors.tealDeep),
      ],
    );
  }
}

Map<String, List<ServerConversation>> _group(BuildContext context, List<ServerConversation> conversations) {
  final now = DateTime.now();
  final today = DateTime(now.year, now.month, now.day);
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
