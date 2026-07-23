import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:font_awesome_flutter/font_awesome_flutter.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/memory_talk_service.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/other/temp.dart';

class MemoryTalkDetail extends StatefulWidget {
  final ServerConversation conversation;
  final MemoryTalkReceipt? receipt;
  final bool hasDiscussion;
  final bool isTalkSheetOpen;
  final Future<void> Function() onUndo;
  final VoidCallback onOpenDiscussion;

  const MemoryTalkDetail({
    super.key,
    required this.conversation,
    required this.receipt,
    required this.hasDiscussion,
    required this.isTalkSheetOpen,
    required this.onUndo,
    required this.onOpenDiscussion,
  });

  @override
  State<MemoryTalkDetail> createState() => _MemoryTalkDetailState();
}

class _MemoryTalkDetailState extends State<MemoryTalkDetail> {
  bool _showDiff = false;
  bool _undoing = false;

  String _dateLabel(BuildContext context, DateTime date) {
    final now = DateTime.now();
    final today = DateTime(now.year, now.month, now.day);
    final value = DateTime(date.year, date.month, date.day);
    if (value == today) return context.l10n.today;
    if (value == today.subtract(const Duration(days: 1))) return context.l10n.yesterday;
    return dateTimeFormat(date.year == now.year ? 'MMM d' : 'MMM d, yyyy', date);
  }

  String _durationLabel(BuildContext context) {
    final start = widget.conversation.startedAt ?? widget.conversation.createdAt;
    final finish = widget.conversation.finishedAt;
    final seconds = finish?.difference(start).inSeconds ?? widget.conversation.getDurationInSeconds();
    if (seconds <= 0) return '';
    final minutes = (seconds / 60).round();
    return context.l10n.memoryTalkMinutes(minutes);
  }

  Future<void> _undo() async {
    if (_undoing) return;
    HapticFeedback.mediumImpact();
    setState(() => _undoing = true);
    await widget.onUndo();
    if (mounted) {
      setState(() {
        _undoing = false;
        _showDiff = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final conversation = widget.conversation;
    final date = conversation.startedAt ?? conversation.createdAt;
    final duration = _durationLabel(context);
    final receipt = widget.receipt;

    return ListView(
      padding: const EdgeInsets.fromLTRB(4, 14, 4, 150),
      children: [
        Text(
          conversation.structured.title,
          style: const TextStyle(
            fontFamily: EllaTextStyles.uiFont,
            fontSize: 24,
            height: 1.3,
            fontWeight: FontWeight.w600,
            color: EllaColors.ink,
          ),
        ),
        const SizedBox(height: 16),
        if (receipt == null || widget.isTalkSheetOpen)
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _InfoChip(
                icon: FontAwesomeIcons.calendarDay,
                label: _dateLabel(context, date),
              ),
              _InfoChip(
                icon: FontAwesomeIcons.clock,
                label: dateTimeFormat('h:mm a', date),
              ),
              if (!widget.isTalkSheetOpen && duration.isNotEmpty) _InfoChip(label: duration),
            ],
          ),
        if (!widget.isTalkSheetOpen && receipt != null) ...[
          Semantics(
            button: true,
            child: InkWell(
              onTap: () {
                HapticFeedback.selectionClick();
                setState(() => _showDiff = !_showDiff);
              },
              borderRadius: BorderRadius.circular(999),
              child: Ink(
                height: 44,
                padding: const EdgeInsets.symmetric(horizontal: 14),
                decoration: BoxDecoration(
                  color: EllaColors.cardDeep,
                  borderRadius: BorderRadius.circular(999),
                ),
                child: Row(
                  children: [
                    Container(
                      width: 18,
                      height: 18,
                      decoration: const BoxDecoration(
                        color: EllaColors.teal,
                        shape: BoxShape.circle,
                      ),
                      child: const Center(
                        child: FaIcon(
                          FontAwesomeIcons.check,
                          size: 10,
                          color: EllaColors.paper,
                        ),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text.rich(
                        TextSpan(
                          children: [
                            TextSpan(
                              text: context.l10n.memoryTalkUpdatedJustNow,
                              style: const TextStyle(fontWeight: FontWeight.w700),
                            ),
                            const TextSpan(text: ' — '),
                            TextSpan(
                              text: '${context.l10n.memoryTalkSeeWhatChanged} ›',
                              style: const TextStyle(
                                color: EllaColors.tealDeep,
                                fontStyle: FontStyle.italic,
                              ),
                            ),
                          ],
                        ),
                        style: const TextStyle(
                          fontFamily: EllaTextStyles.uiFont,
                          color: EllaColors.ink,
                          fontSize: 15,
                          height: 1.25,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ],
        if (!widget.isTalkSheetOpen) ...[
          const SizedBox(height: 20),
          _OverviewText(
            overview: conversation.structured.overview,
            emphasizedValue: receipt?.newValue,
          ),
        ],
        if (!widget.isTalkSheetOpen && receipt != null && _showDiff) ...[
          const SizedBox(height: 22),
          Container(
            padding: const EdgeInsets.all(20),
            decoration: BoxDecoration(
              color: EllaColors.card,
              borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(context.l10n.memoryTalkWhatChanged, style: EllaTextStyles.eyebrow),
                const SizedBox(height: 14),
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        receipt.oldValue,
                        style: const TextStyle(
                          fontFamily: EllaTextStyles.uiFont,
                          fontSize: 17,
                          color: EllaColors.inkSoft,
                          decoration: TextDecoration.lineThrough,
                          decorationThickness: 1.5,
                        ),
                      ),
                    ),
                    const Padding(
                      padding: EdgeInsets.symmetric(horizontal: 12),
                      child: FaIcon(FontAwesomeIcons.arrowRight, size: 14, color: EllaColors.inkSoft),
                    ),
                    Expanded(
                      child: Text(
                        receipt.newValue,
                        style: const TextStyle(
                          fontFamily: EllaTextStyles.uiFont,
                          fontSize: 17,
                          color: EllaColors.tealDeep,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                  ],
                ),
                if (receipt.propagated) ...[
                  const SizedBox(height: 10),
                  Text(
                    context.l10n.memoryTalkAlsoFixedOnPersonPage(receipt.newValue),
                    style: EllaTextStyles.caption,
                  ),
                ],
                const SizedBox(height: 14),
                SizedBox(
                  width: double.infinity,
                  height: 48,
                  child: FilledButton(
                    onPressed: _undoing ? null : _undo,
                    style: FilledButton.styleFrom(
                      backgroundColor: EllaColors.cardDeep,
                      foregroundColor: EllaColors.tealDeep,
                      disabledBackgroundColor: EllaColors.cardDeep,
                      minimumSize: const Size.fromHeight(48),
                      maximumSize: const Size.fromHeight(48),
                      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(24)),
                    ),
                    child: _undoing
                        ? const SizedBox(
                            width: 20,
                            height: 20,
                            child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
                          )
                        : Text(
                            context.l10n.memoryTalkUndoThisChange,
                            style: const TextStyle(
                              fontFamily: EllaTextStyles.uiFont,
                              fontSize: 16,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                  ),
                ),
              ],
            ),
          ),
        ],
        if (!widget.isTalkSheetOpen && widget.hasDiscussion) ...[
          const SizedBox(height: 20),
          InkWell(
            onTap: widget.onOpenDiscussion,
            borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
            child: Ink(
              height: 64,
              padding: const EdgeInsets.symmetric(horizontal: 20),
              decoration: BoxDecoration(
                color: EllaColors.card,
                borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
              ),
              child: Row(
                children: [
                  Expanded(
                    child: Text(
                      context.l10n.memoryTalkConversationAboutThis,
                      style: const TextStyle(
                        fontFamily: EllaTextStyles.uiFont,
                        color: EllaColors.ink,
                        fontSize: 17,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                  const FaIcon(FontAwesomeIcons.chevronRight, size: 13, color: EllaColors.inkSoft),
                ],
              ),
            ),
          ),
        ],
      ],
    );
  }
}

class _OverviewText extends StatelessWidget {
  final String overview;
  final String? emphasizedValue;

  const _OverviewText({
    required this.overview,
    required this.emphasizedValue,
  });

  @override
  Widget build(BuildContext context) {
    final value = emphasizedValue;
    if (value == null || value.isEmpty) {
      return Text(overview, style: EllaTextStyles.body);
    }

    final match = RegExp(RegExp.escape(value), caseSensitive: false).firstMatch(overview);
    if (match == null) {
      return Text(overview, style: EllaTextStyles.body);
    }

    return Text.rich(
      TextSpan(
        children: [
          TextSpan(text: overview.substring(0, match.start)),
          TextSpan(
            text: overview.substring(match.start, match.end),
            style: const TextStyle(
              color: EllaColors.tealDeep,
              fontWeight: FontWeight.w600,
            ),
          ),
          TextSpan(text: overview.substring(match.end)),
        ],
      ),
      style: EllaTextStyles.body,
    );
  }
}

class MemoryTalkPill extends StatelessWidget {
  final VoidCallback onPressed;

  const MemoryTalkPill({super.key, required this.onPressed});

  @override
  Widget build(BuildContext context) {
    return Semantics(
      button: true,
      label: context.l10n.memoryTalkTalkAboutThis,
      child: SizedBox(
        height: 56,
        width: 220,
        child: FilledButton.icon(
          onPressed: onPressed,
          style: FilledButton.styleFrom(
            backgroundColor: EllaColors.tealDeep,
            foregroundColor: EllaColors.paper,
            padding: const EdgeInsets.symmetric(horizontal: 34),
            elevation: 5,
            shadowColor: EllaColors.ink.withValues(alpha: 0.22),
            shape: const StadiumBorder(),
          ),
          icon: const FaIcon(FontAwesomeIcons.microphone, size: 18),
          label: Text(
            context.l10n.memoryTalkTalkAboutThis,
            style: const TextStyle(
              fontFamily: EllaTextStyles.uiFont,
              fontSize: 18,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
      ),
    );
  }
}

class _InfoChip extends StatelessWidget {
  final IconData? icon;
  final String label;

  const _InfoChip({this.icon, required this.label});

  @override
  Widget build(BuildContext context) {
    return Container(
      height: 32,
      padding: const EdgeInsets.symmetric(horizontal: 12),
      decoration: BoxDecoration(
        color: EllaColors.cardDeep,
        borderRadius: BorderRadius.circular(16),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (icon != null) ...[
            FaIcon(icon, size: 13, color: EllaColors.inkSoft),
            const SizedBox(width: 7),
          ],
          Text(
            label,
            style: const TextStyle(
              fontFamily: EllaTextStyles.uiFont,
              color: EllaColors.inkSoft,
              fontSize: 13,
              fontWeight: FontWeight.w500,
            ),
          ),
        ],
      ),
    );
  }
}
