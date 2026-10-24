import 'package:flutter/material.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/services/journal_preview_refresh.dart';
import 'package:omi/ella/widgets/quiet_memory_preview.dart';
import 'package:omi/ella/widgets/summary_refresh_notice.dart';
import 'package:omi/utils/l10n_extensions.dart';

class RefreshedJournalPreview extends StatelessWidget {
  const RefreshedJournalPreview({
    super.key,
    required this.controller,
    required this.titleFor,
    required this.onClose,
    required this.onFullConversation,
  });
  final JournalPreviewRefresh controller;
  final String Function(ServerConversation) titleFor;
  final VoidCallback onClose;
  final void Function(ServerConversation) onFullConversation;
  @override
  Widget build(BuildContext context) => AnimatedBuilder(
      animation: controller,
      builder: (context, _) {
        final fresh = controller.memory;
        final memory = fresh ?? controller.retainedMemory;
        if (memory != null) {
          return QuietMemoryPreview(
              conversation: memory,
              readNotice: fresh == null ? _readNotice(context) : null,
              title: titleFor(memory),
              onClose: onClose,
              onFullConversation: () {
                final current = controller.memory ?? controller.retainedMemory;
                if (current != null) onFullConversation(current);
              });
        }
        return SingleChildScrollView(
            child: Padding(
                padding: const EdgeInsets.all(24),
                child: Column(mainAxisSize: MainAxisSize.min, children: [
                  Align(
                      alignment: Alignment.centerRight,
                      child:
                          IconButton(tooltip: context.l10n.close, onPressed: onClose, icon: const Icon(Icons.close))),
                  if (controller.loading) const CircularProgressIndicator(),
                  Text(controller.loading
                      ? context.l10n.loading
                      : 'This summary could not be loaded. Your saved records have not been changed.'),
                  if (!controller.loading && controller.isCurrent)
                    FilledButton(
                        style: summaryReadButtonStyle(context),
                        onPressed: controller.load,
                        child: Text(context.l10n.tryAgain, textAlign: TextAlign.center)),
                ])));
      });
  Widget _readNotice(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(controller.loading
              ? 'Showing saved preview while checking the latest summary…'
              : controller.failure == null
                  ? 'Showing saved preview. Refresh to check this version.'
                  : 'Showing saved preview. The latest summary could not be verified.'),
          if (!controller.loading)
            FilledButton(
                style: summaryReadButtonStyle(context),
                onPressed: controller.load,
                child: Text(context.l10n.tryAgain, textAlign: TextAlign.center)),
        ]),
      );
}
