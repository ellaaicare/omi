import 'package:flutter/material.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/display_text.dart';
import 'package:omi/utils/l10n_extensions.dart';

bool hasCurrentHermesSummary(ServerConversation conversation) {
  final enrichment = conversation.enrichmentState;
  final version = conversation.activeSummaryVersionId;
  return conversation.status == ConversationStatus.completed &&
      !conversation.deleted &&
      !conversation.discarded &&
      version != null &&
      version.trim().isNotEmpty &&
      enrichment?['result_summary_version_id'] == version &&
      enrichment?['status'] == 'writeback_applied' &&
      enrichment?['pending'] == false &&
      enrichment?['canonical_status'] == 'completed' &&
      enrichment?['kind'] == 'hermes_enriched' &&
      enrichment?['source'] == 'hermes_parallel';
}

/// Attribution belongs to this record's confirmed active version, never its text tags.
class HermesSummarySource extends StatelessWidget {
  const HermesSummarySource({super.key, required this.conversation});

  final ServerConversation conversation;

  @override
  Widget build(BuildContext context) {
    if (!hasCurrentHermesSummary(conversation)) return const SizedBox.shrink();
    final description = context.l10n.hermesSummarySourceDescription;
    return Semantics(
      label: description,
      excludeSemantics: true,
      child: Tooltip(
        message: description,
        excludeFromSemantics: true,
        child: Row(
          key: Key('hermes-summary-source-${conversation.id}'),
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Padding(
              padding: EdgeInsets.only(top: 2),
              child: Icon(Icons.description_outlined, size: 16, color: EllaColors.tealDeep),
            ),
            const SizedBox(width: 6),
            Flexible(child: Text(context.l10n.hermesSummarySource, style: EllaTextStyles.caption)),
          ],
        ),
      ),
    );
  }
}

class EllaSourceIndicator extends StatelessWidget {
  final double size;

  const EllaSourceIndicator({super.key, this.size = 16});

  @override
  Widget build(BuildContext context) {
    final label = context.l10n.ellaSummarySource;
    return Tooltip(
      message: label,
      child: Semantics(
        label: label,
        child: Icon(
          Icons.auto_awesome_rounded,
          size: size,
          color: EllaColors.primary,
        ),
      ),
    );
  }
}

class EllaSourceBadge extends StatelessWidget {
  const EllaSourceBadge({super.key});

  @override
  Widget build(BuildContext context) {
    return DecoratedBox(
      decoration: BoxDecoration(
        color: EllaColors.bgSecondary,
        shape: BoxShape.circle,
        border: Border.all(color: EllaColors.bgTertiary),
      ),
      child: const Padding(
        padding: EdgeInsets.all(3),
        child: EllaSourceIndicator(size: 11),
      ),
    );
  }
}

class EllaSourceText extends StatelessWidget {
  final String value;
  final TextStyle? style;
  final int? maxLines;
  final TextOverflow overflow;
  final TextAlign? textAlign;
  final bool? isEllaGenerated;

  const EllaSourceText(
    this.value, {
    super.key,
    this.style,
    this.maxLines,
    this.overflow = TextOverflow.clip,
    this.textAlign,
    this.isEllaGenerated,
  });

  @override
  Widget build(BuildContext context) {
    final displayValue = parseEllaDisplayValue(value);
    final showSource = isEllaGenerated ?? displayValue.isEllaGenerated;
    return Text.rich(
      TextSpan(
        children: [
          if (showSource) ...[
            const WidgetSpan(
              alignment: PlaceholderAlignment.middle,
              child: EllaSourceIndicator(),
            ),
            const TextSpan(text: ' '),
          ],
          TextSpan(text: displayValue.text),
        ],
      ),
      style: style,
      maxLines: maxLines,
      overflow: overflow,
      textAlign: textAlign,
    );
  }
}
