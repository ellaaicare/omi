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

class EllaMemoriesPage extends StatefulWidget {
  const EllaMemoriesPage({super.key});

  @override
  State<EllaMemoriesPage> createState() => _EllaMemoriesPageState();
}

class _EllaMemoriesPageState extends State<EllaMemoriesPage> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) context.read<ConversationProvider>().ensureFreshConversations();
    });
  }

  @override
  Widget build(BuildContext context) {
    final conversationProvider = context.watch<ConversationProvider>();
    final capture = context.watch<CaptureProvider>();
    final live = capture.recordingState != RecordingState.stop || capture.segments.isNotEmpty;
    final groups = _group(conversationProvider.visibleConversations);
    final loading =
        groups.isEmpty && (!conversationProvider.hasLoadedConversations || conversationProvider.isLoadingConversations);
    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.arrow_back_ios_new_rounded, color: EllaColors.tealDeep),
          onPressed: () => Navigator.pop(context),
        ),
        title: const Text('Memories'),
      ),
      body: RefreshIndicator(
        color: EllaColors.tealDeep,
        onRefresh: conversationProvider.getInitialConversations,
        child: ListView(
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
                    onTap: () => Navigator.of(context).push(
                      MaterialPageRoute(
                        builder: (_) => ConversationDetailPage(conversation: conversation),
                      ),
                    ),
                  ),
                  const SizedBox(height: EllaSizes.cardGap),
                ],
                const SizedBox(height: 16),
              ],
            if (!loading && groups.isEmpty)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 80),
                child:
                    Text('Your memories will appear here. 🪽', textAlign: TextAlign.center, style: EllaTextStyles.body),
              ),
          ],
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
          child: const Padding(
            padding: EdgeInsets.all(EllaSizes.cardPadding),
            child: Row(
              children: [
                EllaBreathingDot(live: true),
                SizedBox(width: 16),
                Expanded(child: Text('In progress…', style: EllaTextStyles.display)),
                Icon(Icons.chevron_right_rounded, color: EllaColors.inkSoft),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _MemoryRow extends StatelessWidget {
  const _MemoryRow({required this.conversation, required this.onTap});

  final ServerConversation conversation;
  final VoidCallback onTap;

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
              const Padding(
                padding: EdgeInsets.only(top: 8),
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
