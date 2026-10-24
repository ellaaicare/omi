import 'package:flutter/material.dart';
import 'package:omi/ella/services/retained_summary.dart';
import 'package:omi/utils/l10n_extensions.dart';

class RetainedSummaryCard extends StatefulWidget {
  const RetainedSummaryCard({super.key, required this.conversationId, this.controller});
  final String conversationId;
  final RetainedSummaryController? controller;
  @override
  State<RetainedSummaryCard> createState() => _RetainedSummaryCardState();
}

class _RetainedSummaryCardState extends State<RetainedSummaryCard> {
  late final RetainedSummaryController controller = widget.controller ?? RetainedSummaryController();
  @override
  void initState() {
    super.initState();
    controller.load(widget.conversationId);
  }

  @override
  void didUpdateWidget(RetainedSummaryCard oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.conversationId != widget.conversationId) controller.load(widget.conversationId);
  }

  @override
  void dispose() {
    if (widget.controller == null) controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
      listenable: controller,
      builder: (context, _) {
        final versions = controller.versions;
        if (versions.isEmpty) return const SizedBox.shrink();
        final summary = controller.selected;
        return Padding(
            padding: const EdgeInsets.all(16),
            child: ExpansionTile(
              title: Text(context.l10n.ellaRetainedSummaryLabel),
              subtitle: Text(context.l10n.ellaRetainedSummaryNotice),
              children: [
                if (versions.length > 1) ...[
                  Text(context.l10n.ellaRetainedSummaryChoose),
                  for (final version in versions)
                    ListTile(
                      title: Text(version.id),
                      selected: summary?.id == version.id,
                      trailing: summary?.id == version.id ? const Icon(Icons.check) : null,
                      onTap: () => controller.select(version.id),
                    ),
                ],
                if (summary != null)
                  Padding(
                      padding: const EdgeInsets.all(16),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(context.l10n.ellaRetainedSummaryVersion(summary.id)),
                          Text(context.l10n.ellaRetainedSummaryProvenance),
                          const SizedBox(height: 12),
                          SelectableText(summary.title, style: Theme.of(context).textTheme.titleMedium),
                          const SizedBox(height: 8),
                          SelectableText(summary.overview),
                        ],
                      )),
              ],
            ));
      });
}
