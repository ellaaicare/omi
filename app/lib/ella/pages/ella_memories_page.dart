import 'dart:async';

import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/widgets/ella_breathing_dot.dart';
import 'package:omi/pages/conversation_capturing/page.dart';
import 'package:omi/pages/conversation_detail/page.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/utils/enums.dart';
import 'package:omi/utils/l10n_extensions.dart';

class EllaMemoriesPage extends StatefulWidget {
  const EllaMemoriesPage({super.key});

  @override
  State<EllaMemoriesPage> createState() => _EllaMemoriesPageState();
}

class _EllaMemoriesPageState extends State<EllaMemoriesPage> {
  final Set<String> _deletingConversationIds = <String>{};
  final ScrollController _scrollController = ScrollController();
  bool _showBackToRecent = false;

  @override
  void initState() {
    super.initState();
    _scrollController.addListener(_handleScroll);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) context.read<ConversationProvider>().ensureFreshConversations();
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

  @override
  Widget build(BuildContext context) {
    final conversationProvider = context.watch<ConversationProvider>();
    final capture = context.watch<CaptureProvider>();
    final live = capture.recordingState != RecordingState.stop || capture.segments.isNotEmpty;
    final groups = _group(conversationProvider.visibleConversations);
    final loading =
        groups.isEmpty && (!conversationProvider.hasLoadedConversations || conversationProvider.isLoadingConversations);
    _checkPaginationAfterLayout(conversationProvider);
    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.arrow_back_ios_new_rounded, color: EllaColors.tealDeep),
          onPressed: () => Navigator.pop(context),
        ),
        title: Text(context.l10n.memories),
      ),
      body: RefreshIndicator(
        color: EllaColors.tealDeep,
        onRefresh: conversationProvider.getInitialConversations,
        child: ListView(
          key: const Key('ella-memories-list'),
          controller: _scrollController,
          padding: const EdgeInsets.fromLTRB(20, 8, 20, 40),
          children: [
            SizedBox(
              height: 3,
              child: conversationProvider.isLoadingConversations && groups.isNotEmpty
                  ? const LinearProgressIndicator(
                      key: Key('memories-refresh-indicator'),
                      color: EllaColors.tealDeep,
                      backgroundColor: EllaColors.cardDeep,
                    )
                  : null,
            ),
            const SizedBox(height: 5),
            if (live) ...[
              _LiveMemoryCard(
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
              const SizedBox(height: 24),
            ],
            if (loading)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 80),
                child: Center(
                  child: SizedBox(
                    width: 24,
                    height: 24,
                    child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
                  ),
                ),
              )
            else
              for (final entry in groups.entries) ...[
                Text(entry.key, style: EllaTextStyles.eyebrow),
                const SizedBox(height: EllaSizes.cardGap),
                for (final conversation in entry.value) ...[
                  _MemoryRow(
                    conversation: conversation,
                    deleting: _deletingConversationIds.contains(conversation.id),
                    onTap: () => Navigator.of(
                      context,
                    ).push(MaterialPageRoute(builder: (_) => ConversationDetailPage(conversation: conversation))),
                    onDelete: () => _confirmDelete(conversationProvider, conversation),
                  ),
                  const SizedBox(height: EllaSizes.cardGap),
                ],
                const SizedBox(height: 16),
              ],
            if (!loading && groups.isEmpty)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 80),
                child: Text(
                  context.l10n.createYourFirstMemory,
                  textAlign: TextAlign.center,
                  style: EllaTextStyles.body,
                ),
              ),
            if (conversationProvider.isLoadingMoreConversations)
              const Padding(
                key: Key('memories-loading-more'),
                padding: EdgeInsets.symmetric(vertical: 24),
                child: Center(
                  child: SizedBox(
                    width: 22,
                    height: 22,
                    child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
                  ),
                ),
              )
            else if (conversationProvider.loadMoreConversationsFailed)
              Padding(
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
    );
  }

  Future<void> _confirmDelete(ConversationProvider provider, ServerConversation conversation) async {
    if (_deletingConversationIds.contains(conversation.id)) return;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(context.l10n.deleteMemory),
        content: Text(context.l10n.deleteMemoryConfirmation),
        actions: [
          TextButton(onPressed: () => Navigator.of(dialogContext).pop(false), child: Text(context.l10n.cancel)),
          FilledButton(
            key: const Key('confirm-delete-memory'),
            onPressed: () => Navigator.of(dialogContext).pop(true),
            style: FilledButton.styleFrom(backgroundColor: EllaColors.warning, foregroundColor: Colors.white),
            child: Text(context.l10n.delete),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;

    setState(() => _deletingConversationIds.add(conversation.id));
    final deleted = await provider.deleteConversationPermanently(conversation);
    if (!mounted) return;
    setState(() => _deletingConversationIds.remove(conversation.id));
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(deleted ? context.l10n.memoryDeleted : context.l10n.anErrorOccurredTryAgain)),
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

class _MemoryRow extends StatelessWidget {
  const _MemoryRow({required this.conversation, required this.deleting, required this.onTap, required this.onDelete});

  final ServerConversation conversation;
  final bool deleting;
  final VoidCallback onTap;
  final VoidCallback onDelete;

  String get _title =>
      conversation.structured.title.replaceFirst(RegExp(r'^🪽\s*'), '').replaceFirst(RegExp(r'^\[Ella\]\s*'), '');

  @override
  Widget build(BuildContext context) {
    return Material(
      color: EllaColors.card,
      borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Container(
                width: 40,
                height: 40,
                alignment: Alignment.center,
                decoration: BoxDecoration(
                  color: EllaColors.cardDeep,
                  borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                ),
                child: Text(conversation.structured.emoji.isEmpty ? '🪽' : conversation.structured.emoji),
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(_title, style: EllaTextStyles.body.copyWith(fontWeight: FontWeight.w600)),
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
              const SizedBox(width: 8),
              deleting
                  ? const SizedBox(
                      key: Key('deleting-memory-progress'),
                      width: EllaSizes.minTouchTarget,
                      height: EllaSizes.minTouchTarget,
                      child: Center(
                        child: SizedBox(
                          width: 20,
                          height: 20,
                          child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
                        ),
                      ),
                    )
                  : IconButton(
                      key: Key('delete-memory-${conversation.id}'),
                      tooltip: context.l10n.deleteMemory,
                      onPressed: onDelete,
                      constraints: const BoxConstraints(
                        minWidth: EllaSizes.minTouchTarget,
                        minHeight: EllaSizes.minTouchTarget,
                      ),
                      icon: const Icon(Icons.delete_outline_rounded, color: EllaColors.warning),
                    ),
              const Padding(
                padding: EdgeInsets.only(top: 12),
                child: Icon(Icons.chevron_right_rounded, color: EllaColors.inkSoft),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

Map<String, List<ServerConversation>> _group(List<ServerConversation> conversations) {
  final now = DateTime.now();
  final today = DateTime(now.year, now.month, now.day);
  final result = <String, List<ServerConversation>>{};
  for (final conversation in conversations) {
    final value = (conversation.startedAt ?? conversation.createdAt).toLocal();
    final day = DateTime(value.year, value.month, value.day);
    final label = day == today
        ? 'TODAY'
        : day == today.subtract(const Duration(days: 1))
            ? 'YESTERDAY'
            : DateFormat('EEEE · MMMM d').format(day).toUpperCase();
    result.putIfAbsent(label, () => []).add(conversation);
  }
  return result;
}
