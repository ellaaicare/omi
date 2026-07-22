import 'package:flutter/material.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/l10n_extensions.dart';

class ConversationTitleWithEllaBadge extends StatelessWidget {
  const ConversationTitleWithEllaBadge({
    super.key,
    required this.conversation,
    required this.title,
    this.style,
    this.maxLines,
    this.overflow,
  });

  final ServerConversation conversation;
  final String title;
  final TextStyle? style;
  final int? maxLines;
  final TextOverflow? overflow;

  @override
  Widget build(BuildContext context) {
    return Row(
      key: ValueKey('conversation-title-${conversation.id}'),
      children: [
        Expanded(
          child: Text(title, style: style, maxLines: maxLines, overflow: overflow),
        ),
        if (conversation.isEllaEnriched) ...[
          const SizedBox(width: 8),
          _EllaEnrichedBadge(conversationId: conversation.id),
        ],
      ],
    );
  }
}

class _EllaEnrichedBadge extends StatelessWidget {
  const _EllaEnrichedBadge({required this.conversationId});

  final String conversationId;

  @override
  Widget build(BuildContext context) {
    final accessibilityLabel = context.l10n.conversationEllaEnrichedLabel;
    return Tooltip(
      message: accessibilityLabel,
      child: Semantics(
        key: ValueKey('ella-enriched-badge-$conversationId'),
        container: true,
        label: accessibilityLabel,
        child: ExcludeSemantics(
          child: Container(
            height: 20,
            padding: const EdgeInsets.symmetric(horizontal: 6),
            decoration: BoxDecoration(
              color: EllaColors.primarySubtle,
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: EllaColors.primaryLight),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(Icons.auto_awesome_rounded, size: 11, color: EllaColors.primaryDark),
                const SizedBox(width: 3),
                Text(
                  context.l10n.conversationEllaEnrichedBadge,
                  style: const TextStyle(
                    color: EllaColors.primaryDark,
                    fontSize: 11,
                    fontWeight: FontWeight.w700,
                    height: 1,
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
