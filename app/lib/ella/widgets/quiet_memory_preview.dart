import 'package:omi/ella/widgets/ella_source_indicator.dart';
import 'package:omi/ella/widgets/ella_v7_surfaces.dart';
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// Text-only presentation of the exact retained record; no generated replacements.
/// The owning page fences visibility/actions against its current account authority.
class QuietMemoryPreview extends StatelessWidget {
  const QuietMemoryPreview(
      {super.key,
      required this.conversation,
      required this.title,
      required this.onClose,
      required this.onFullConversation,
      this.readNotice});
  final ServerConversation conversation;
  final String title;
  final VoidCallback onClose, onFullConversation;
  final Widget? readNotice;
  @override
  Widget build(BuildContext context) => ConstrainedBox(
      constraints: BoxConstraints(maxHeight: MediaQuery.sizeOf(context).height * .8),
      child: SingleChildScrollView(
          padding: const EdgeInsets.all(20),
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, mainAxisSize: MainAxisSize.min, children: [
            Row(children: [
              Expanded(
                  child: Text(title,
                      style: EllaTypography.of(context).body.copyWith(fontSize: 22, fontWeight: FontWeight.w700))),
              IconButton(tooltip: context.l10n.close, onPressed: onClose, icon: const Icon(Icons.close))
            ]),
            Text(DateFormat.yMMMd().add_jm().format((conversation.startedAt ?? conversation.createdAt).toLocal()),
                style: EllaTypography.of(context).secondary),
            if (readNotice != null) readNotice!,
            if (readNotice == null &&
                (hasCurrentSummarySource(conversation))) ...[
              const SizedBox(height: 8),
              ActiveSummarySource(conversation: conversation, showVersion: true),
            ],
            if (conversation.structured.overview.trim().isNotEmpty) ...[
              const SizedBox(height: 16),
              Text(context.l10n.overview, style: EllaTypography.of(context).body.copyWith(fontWeight: FontWeight.w700)),
              const SizedBox(height: 8),
              SelectableText(conversation.structured.overview,
                  key: const Key('iris-memory-retained-overview'), style: EllaTypography.of(context).body),
            ],
            if (conversation.transcriptSegments.isNotEmpty) ...[
              const SizedBox(height: 20),
              Text(context.l10n.irisOriginalWordsPreview,
                  style: EllaTypography.of(context).body.copyWith(fontWeight: FontWeight.w700)),
              const SizedBox(height: 8),
              for (final segment in conversation.transcriptSegments.take(3))
                Padding(
                    padding: const EdgeInsets.only(bottom: 12),
                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      Text(segment.isUser ? context.l10n.you : context.l10n.irisSpeaker(segment.speakerId),
                          style: EllaTypography.of(context).secondary),
                      SelectableText(segment.text, style: EllaTypography.of(context).body),
                    ])),
            ],
            const SizedBox(height: 12),
            PopupMenuButton<String>(
                key: const Key('iris-memory-more'),
                color: EllaPalette.of(context).elevatedCard,
                surfaceTintColor: Colors.transparent,
                tooltip: context.l10n.irisMore,
                onSelected: (_) => onFullConversation(),
                itemBuilder: (_) => [
                      PopupMenuItem(
                        value: 'full',
                        child: Text(context.l10n.irisFullConversation, style: EllaTypography.of(context).body),
                      )
                    ],
                child: Padding(
                    padding: const EdgeInsets.all(12),
                    child: Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                      const Icon(Icons.more_horiz),
                      const SizedBox(width: 8),
                      Flexible(child: Text(context.l10n.irisMore, style: EllaTypography.of(context).body))
                    ]))),
          ])));
}

class QuietMemoryRow extends StatelessWidget {
  const QuietMemoryRow(
      {super.key, required this.conversation, required this.title, required this.onOpen, this.showWeekday = false});
  final ServerConversation conversation;
  final String title;
  final VoidCallback onOpen;
  final bool showWeekday;
  @override
  Widget build(BuildContext context) {
    final large = MediaQuery.textScalerOf(context).scale(1) >= 2;
    final time = Text(
        (showWeekday ? DateFormat.E().add_jm() : DateFormat.jm())
            .format((conversation.startedAt ?? conversation.createdAt).toLocal()),
        style: EllaTypography.of(context).secondary.copyWith(fontSize: 16));
    final titleRow = Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
      EllaV7Glass(
          radius: 14,
          padding: const EdgeInsets.all(10),
          child: Icon(Icons.bookmark_outline, size: 24, color: EllaPalette.of(context).ink)),
      const SizedBox(width: 8),
      Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text(title, style: EllaTypography.of(context).body.copyWith(fontSize: 17, fontWeight: FontWeight.w600)),
        if (conversation.structured.overview.trim().isNotEmpty) ...[
          const SizedBox(height: 6),
          Text(conversation.structured.overview,
              maxLines: 2, overflow: TextOverflow.ellipsis, style: EllaTypography.of(context).secondary),
        ],
        if (hasCurrentSummarySource(conversation)) ...[
          const SizedBox(height: 6),
          ActiveSummarySource(conversation: conversation),
        ],
      ])),
    ]);
    return Semantics(
        button: true,
        child: InkWell(
          onTap: onOpen,
          child: ConstrainedBox(
            constraints: BoxConstraints(minHeight: MediaQuery.textScalerOf(context).scale(1) >= 1.2 ? 56 : 48),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 12),
              child: large
                  ? Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [titleRow, const SizedBox(height: 8), time])
                  : Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      Expanded(child: titleRow),
                      const SizedBox(width: 8),
                      SizedBox(width: MediaQuery.textScalerOf(context).scale(1) >= 1.2 ? 100 : 76, child: time)
                    ]),
            ),
          ),
        ));
  }
}

/// Clears private content after silent exact-lease retirement, without network reads.
class QuietMemoryAuthorityFence extends StatefulWidget {
  const QuietMemoryAuthorityFence({super.key, required this.isCurrent, required this.child});
  final bool Function() isCurrent;
  final Widget child;
  @override
  State<QuietMemoryAuthorityFence> createState() => _QuietMemoryAuthorityFenceState();
}

class _QuietMemoryAuthorityFenceState extends State<QuietMemoryAuthorityFence> {
  Timer? _timer;
  bool _retired = false;
  @override
  void initState() {
    super.initState();
    _timer = Timer.periodic(const Duration(milliseconds: 250), (_) {
      if (!widget.isCurrent()) {
        _timer?.cancel();
        if (mounted) setState(() => _retired = true);
      }
    });
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => _retired || !widget.isCurrent()
      ? Padding(padding: const EdgeInsets.all(24), child: Text(context.l10n.upstreamCaptureUnavailable))
      : widget.child;
}
