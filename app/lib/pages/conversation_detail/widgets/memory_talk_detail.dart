import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:font_awesome_flutter/font_awesome_flutter.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/memory_talk_service.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/other/temp.dart';

int _countTextMatches(String text, String query) {
  if (query.isEmpty) return 0;

  final lowerText = text.toLowerCase();
  final lowerQuery = query.toLowerCase();
  var count = 0;
  var index = 0;
  while ((index = lowerText.indexOf(lowerQuery, index)) != -1) {
    count += 1;
    index += lowerQuery.length;
  }
  return count;
}

int countMemoryTalkSearchMatches(ServerConversation conversation, String query) {
  return _countTextMatches(conversation.structured.title, query) +
      _countTextMatches(conversation.structured.overview, query);
}

List<TextSpan> _highlightSearchMatches(String text, String query, int currentResultIndex) {
  if (query.isEmpty) return [TextSpan(text: text)];

  final spans = <TextSpan>[];
  final lowerText = text.toLowerCase();
  final lowerQuery = query.toLowerCase();
  var start = 0;
  var matchIndex = 0;
  var index = lowerText.indexOf(lowerQuery);

  while (index != -1) {
    if (index > start) {
      spans.add(TextSpan(text: text.substring(start, index)));
    }
    final isCurrentResult = matchIndex == currentResultIndex;
    // Search highlight uses Ella semantic tokens (never raw Colors.* on elder
    // surfaces): active match = warning amber, other matches = teal tint; text
    // stays ink for contrast rather than pure white.
    spans.add(
      TextSpan(
        text: text.substring(index, index + query.length),
        style: TextStyle(
          backgroundColor: isCurrentResult
              ? EllaColors.warning.withValues(alpha: 0.85)
              : EllaColors.teal.withValues(alpha: 0.35),
          color: EllaColors.ink,
          fontWeight: FontWeight.bold,
        ),
      ),
    );
    matchIndex += 1;
    start = index + query.length;
    index = lowerText.indexOf(lowerQuery, start);
  }

  if (start < text.length) {
    spans.add(TextSpan(text: text.substring(start)));
  }
  return spans;
}

class MemoryTalkDetail extends StatefulWidget {
  final ServerConversation conversation;
  final MemoryTalkReceipt? receipt;
  final bool hasDiscussion;
  final bool isTalkSheetOpen;
  final String searchQuery;
  final int currentResultIndex;
  final Future<void> Function() onUndo;

  const MemoryTalkDetail({
    super.key,
    required this.conversation,
    required this.receipt,
    required this.hasDiscussion,
    required this.isTalkSheetOpen,
    this.searchQuery = '',
    this.currentResultIndex = -1,
    required this.onUndo,
  });

  @override
  State<MemoryTalkDetail> createState() => _MemoryTalkDetailState();
}

class _MemoryTalkDetailState extends State<MemoryTalkDetail> {
  final ScrollController _scrollController = ScrollController();
  bool _showDiff = false;
  bool _undoing = false;

  @override
  void didUpdateWidget(covariant MemoryTalkDetail oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.isTalkSheetOpen && !oldWidget.isTalkSheetOpen) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (_scrollController.hasClients) _scrollController.jumpTo(0);
      });
    }
  }

  @override
  void dispose() {
    _scrollController.dispose();
    super.dispose();
  }

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
    final titleMatchCount = _countTextMatches(conversation.structured.title, widget.searchQuery);

    return ListView(
      controller: _scrollController,
      padding: const EdgeInsets.fromLTRB(4, 14, 4, 150),
      children: [
        Text.rich(
          TextSpan(
            children: _highlightSearchMatches(
              conversation.structured.title,
              widget.searchQuery,
              widget.currentResultIndex,
            ),
          ),
          key: const ValueKey('memory-talk-title'),
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
                icon: widget.isTalkSheetOpen ? null : FontAwesomeIcons.calendarDay,
                label: _dateLabel(context, date),
              ),
              _InfoChip(
                icon: widget.isTalkSheetOpen ? null : FontAwesomeIcons.clock,
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
            searchQuery: widget.searchQuery,
            currentResultIndex: widget.currentResultIndex - titleMatchCount,
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
                    receipt.newValue.trim().toLowerCase() == 'rose'
                        ? context.l10n.memoryTalkAlsoFixedOnRosePage
                        : context.l10n.memoryTalkAlsoFixedOnPersonPage(receipt.newValue),
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
          Container(
            height: 56,
            padding: const EdgeInsets.symmetric(horizontal: 20),
            decoration: BoxDecoration(
              color: EllaColors.card,
              borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
            ),
            child: Row(
              children: [
                const FaIcon(FontAwesomeIcons.check, size: 13, color: EllaColors.tealDeep),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    context.l10n.memoryTalkConversationAboutThis,
                    style: const TextStyle(
                      fontFamily: EllaTextStyles.uiFont,
                      color: EllaColors.ink,
                      fontSize: 16,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
              ],
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
  final String searchQuery;
  final int currentResultIndex;

  const _OverviewText({
    required this.overview,
    required this.emphasizedValue,
    required this.searchQuery,
    required this.currentResultIndex,
  });

  @override
  Widget build(BuildContext context) {
    if (searchQuery.isNotEmpty) {
      return Text.rich(
        TextSpan(children: _highlightSearchMatches(overview, searchQuery, currentResultIndex)),
        key: const ValueKey('memory-talk-overview'),
        style: EllaTextStyles.body,
      );
    }

    final value = emphasizedValue;
    if (value == null || value.isEmpty) {
      return Text(overview, key: const ValueKey('memory-talk-overview'), style: EllaTextStyles.body);
    }

    final match = RegExp(RegExp.escape(value), caseSensitive: false).firstMatch(overview);
    if (match == null) {
      return Text(overview, key: const ValueKey('memory-talk-overview'), style: EllaTextStyles.body);
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
      key: const ValueKey('memory-talk-overview'),
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
