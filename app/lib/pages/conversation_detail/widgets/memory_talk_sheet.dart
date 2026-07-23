import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:font_awesome_flutter/font_awesome_flutter.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/ella_chat_service.dart';
import 'package:omi/ella/services/memory_talk_service.dart';
import 'package:omi/utils/l10n_extensions.dart';

class MemoryTalkSheetResult {
  final bool discussed;
  final MemoryTalkReceipt? receipt;

  const MemoryTalkSheetResult({
    required this.discussed,
    this.receipt,
  });
}

class _TalkTurn {
  final bool isElla;
  String text;

  _TalkTurn({required this.isElla, required this.text});
}

Future<MemoryTalkSheetResult?> showMemoryTalkSheet(
  BuildContext context, {
  required ServerConversation conversation,
}) {
  return showModalBottomSheet<MemoryTalkSheetResult>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    backgroundColor: Colors.transparent,
    barrierColor: EllaColors.ink.withValues(alpha: 0.24),
    builder: (_) => MemoryTalkSheet(conversation: conversation),
  );
}

class MemoryTalkSheet extends StatefulWidget {
  final ServerConversation conversation;

  const MemoryTalkSheet({
    super.key,
    required this.conversation,
  });

  @override
  State<MemoryTalkSheet> createState() => _MemoryTalkSheetState();
}

class _MemoryTalkSheetState extends State<MemoryTalkSheet> {
  final _controller = TextEditingController();
  final _focusNode = FocusNode();
  final _scrollController = ScrollController();
  final List<_TalkTurn> _turns = [];
  MemoryCorrectionClaim? _pendingCorrection;
  bool _hasDiscussed = false;
  bool _isSending = false;
  bool _showVoiceComingSoon = false;
  int _ambiguousReplyCount = 0;

  @override
  void initState() {
    super.initState();
    _loadHistory();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) _focusNode.requestFocus();
    });
  }

  Future<void> _loadHistory() async {
    if (!widget.conversation.memoryTalkState.hasDiscussion) return;
    final history = await fetchMemoryTalkHistory(widget.conversation.id);
    if (!mounted || history.isEmpty || _turns.isNotEmpty || _hasDiscussed) return;
    setState(() {
      _turns
        ..clear()
        ..addAll(
          history.map(
            (turn) => _TalkTurn(
              isElla: turn.role == 'assistant',
              text: turn.text,
            ),
          ),
        );
      _hasDiscussed = true;
    });
    _scrollToEnd();
  }

  @override
  void dispose() {
    _controller.dispose();
    _focusNode.dispose();
    _scrollController.dispose();
    super.dispose();
  }

  String _whenLabel() {
    final date = widget.conversation.startedAt ?? widget.conversation.createdAt;
    final now = DateTime.now();
    final sameDay = date.year == now.year && date.month == now.month && date.day == now.day;
    if (sameDay && date.hour < 12) return context.l10n.memoryTalkThisMorning;
    if (sameDay) return context.l10n.memoryTalkEarlierToday;
    return context.l10n.memoryTalkOnDate('${date.month}/${date.day}');
  }

  String _openingTitle() {
    final title = widget.conversation.structured.title;
    final withIndex = title.toLowerCase().lastIndexOf(' with ');
    if (withIndex > 0) return title.substring(0, withIndex).toLowerCase();
    return title.toLowerCase();
  }

  void _addEllaTurn(String text) {
    setState(() => _turns.add(_TalkTurn(isElla: true, text: text)));
    _scrollToEnd();
  }

  void _scrollToEnd() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scrollController.hasClients) return;
      _scrollController.animateTo(
        _scrollController.position.maxScrollExtent,
        duration: const Duration(milliseconds: 220),
        curve: Curves.easeOut,
      );
    });
  }

  Future<void> _send() async {
    final text = _controller.text.trim();
    if (text.isEmpty || _isSending) return;
    HapticFeedback.lightImpact();
    setState(() {
      _controller.clear();
      _turns.add(_TalkTurn(isElla: false, text: text));
      _hasDiscussed = true;
      _isSending = true;
    });
    _scrollToEnd();

    final pending = _pendingCorrection;
    if (pending != null) {
      await _handleConfirmation(text, pending);
      if (mounted) setState(() => _isSending = false);
      return;
    }

    final claim = extractCorrectionClaim(text, widget.conversation);
    if (claim != null) {
      _pendingCorrection = claim;
      _ambiguousReplyCount = 0;
      final confirmation = context.l10n.memoryTalkCorrectionConfirmation(claim.newValue, claim.oldValue);
      _addEllaTurn(confirmation);
      await _persistCorrectionExchange(text, confirmation);
      if (mounted) setState(() => _isSending = false);
      return;
    }

    await _sendScopedChat(text);
    if (mounted) setState(() => _isSending = false);
  }

  Future<void> _handleConfirmation(String reply, MemoryCorrectionClaim claim) async {
    switch (classifyCorrectionReply(reply)) {
      case CorrectionReplyIntent.affirmative:
        await _applyCorrection(claim, reply);
        return;
      case CorrectionReplyIntent.negative:
        _pendingCorrection = null;
        _ambiguousReplyCount = 0;
        final response = context.l10n.memoryTalkCorrectionDiscarded;
        _addEllaTurn(response);
        await _persistCorrectionExchange(reply, response);
        return;
      case CorrectionReplyIntent.ambiguous:
        late final String response;
        if (_ambiguousReplyCount == 0) {
          _ambiguousReplyCount = 1;
          response = context.l10n.memoryTalkCorrectionReprompt;
        } else {
          _pendingCorrection = null;
          _ambiguousReplyCount = 0;
          response = context.l10n.memoryTalkCorrectionNotChanged;
        }
        _addEllaTurn(response);
        await _persistCorrectionExchange(reply, response);
        return;
    }
  }

  Future<void> _persistCorrectionExchange(String userText, String ellaText) async {
    await appendMemoryTalkTurns(
      widget.conversation.id,
      [
        MemoryTalkHistoryTurn(role: 'user', text: userText, createdAt: DateTime.now()),
        MemoryTalkHistoryTurn(role: 'assistant', text: ellaText, createdAt: DateTime.now()),
      ],
    );
  }

  Future<void> _sendScopedChat(String text) async {
    if (SharedPreferencesUtil().demoMode) {
      await Future<void>.delayed(const Duration(milliseconds: 260));
      if (mounted) _addEllaTurn(context.l10n.memoryTalkDemoReply);
      return;
    }

    final responseTurn = _TalkTurn(isElla: true, text: '');
    setState(() => _turns.add(responseTurn));
    try {
      await for (final chunk in sendEllaChatStream(text, conversationId: widget.conversation.id)) {
        if (!mounted) return;
        if (chunk.type == MessageChunkType.data) {
          setState(() => responseTurn.text += chunk.text);
        } else if (chunk.type == MessageChunkType.done && responseTurn.text.isEmpty) {
          setState(() => responseTurn.text = chunk.message?.text ?? chunk.text);
        } else if (chunk.type == MessageChunkType.error) {
          setState(() => responseTurn.text = context.l10n.memoryTalkSendFailed);
        }
        _scrollToEnd();
      }
      if (mounted && responseTurn.text.trim().isEmpty) {
        setState(() => responseTurn.text = context.l10n.memoryTalkSendFailed);
      }
    } catch (_) {
      if (mounted) setState(() => responseTurn.text = context.l10n.memoryTalkSendFailed);
    }
  }

  Future<void> _applyCorrection(MemoryCorrectionClaim claim, String reply) async {
    final response = context.l10n.memoryTalkApplyingCorrection;
    _addEllaTurn(response);
    await _persistCorrectionExchange(reply, response);
    if (SharedPreferencesUtil().demoMode) {
      await Future<void>.delayed(const Duration(milliseconds: 320));
      final beforeTitle = widget.conversation.structured.title;
      final beforeOverview = widget.conversation.structured.overview;
      final receipt = MemoryTalkReceipt(
        correctionId: 'demo-${DateTime.now().millisecondsSinceEpoch}',
        oldValue: claim.oldValue,
        newValue: claim.newValue,
        beforeTitle: beforeTitle,
        beforeOverview: beforeOverview,
        afterTitle: replaceMemoryValue(beforeTitle, claim),
        afterOverview: replaceMemoryValue(beforeOverview, claim),
        appliedAt: DateTime.now(),
        propagated: false,
      );
      if (!mounted) return;
      Navigator.of(context).pop(MemoryTalkSheetResult(discussed: true, receipt: receipt));
      return;
    }

    final submission = await submitConversationCorrection(
      conversationId: widget.conversation.id,
      correctionText: claim.correctionText,
      summaryTitle: widget.conversation.structured.title,
      summaryOverview: widget.conversation.structured.overview,
      appSummary: widget.conversation.structured.overview,
    );
    if (!mounted) return;
    if (submission == null || submission.correctionId.isEmpty) {
      _pendingCorrection = null;
      _addEllaTurn(context.l10n.memoryTalkCorrectionFailed);
      return;
    }

    final receipt = await _waitForReceipt(submission.correctionId);
    if (!mounted) return;
    if (receipt == null || !receipt.isApplied) {
      _pendingCorrection = null;
      _addEllaTurn(context.l10n.memoryTalkCorrectionStillWorking);
      return;
    }
    Navigator.of(context).pop(
      MemoryTalkSheetResult(
        discussed: true,
        receipt: MemoryTalkReceipt.fromApi(receipt, claim: claim),
      ),
    );
  }

  Future<ConversationCorrectionReceipt?> _waitForReceipt(String correctionId) async {
    for (var attempt = 0; attempt < 12; attempt += 1) {
      final receipt = await getConversationCorrectionReceipt(
        conversationId: widget.conversation.id,
        correctionId: correctionId,
      );
      if (receipt != null && (receipt.isApplied || receipt.status.contains('failed'))) {
        return receipt;
      }
      await Future<void>.delayed(const Duration(seconds: 2));
    }
    return null;
  }

  void _finish() {
    Navigator.of(context).pop(MemoryTalkSheetResult(discussed: _hasDiscussed));
  }

  @override
  Widget build(BuildContext context) {
    final bottomInset = MediaQuery.viewInsetsOf(context).bottom;
    final screenHeight = MediaQuery.sizeOf(context).height;
    final sheetHeight = (screenHeight * 0.60).clamp(470.0, 610.0).toDouble();

    return AnimatedPadding(
      duration: const Duration(milliseconds: 180),
      curve: Curves.easeOut,
      padding: EdgeInsets.only(bottom: bottomInset),
      child: Container(
        height: sheetHeight,
        decoration: const BoxDecoration(
          color: EllaColors.paper,
          borderRadius: BorderRadius.vertical(top: Radius.circular(28)),
        ),
        padding: const EdgeInsets.fromLTRB(20, 14, 20, 12),
        child: SafeArea(
          top: false,
          child: Column(
            children: [
              Container(
                width: 44,
                height: 4,
                decoration: BoxDecoration(
                  color: EllaColors.cardDeep,
                  borderRadius: BorderRadius.circular(999),
                ),
              ),
              const SizedBox(height: 20),
              Text(context.l10n.memoryTalkTalkingAbout, style: EllaTextStyles.eyebrow),
              const SizedBox(height: 4),
              Text(
                widget.conversation.structured.title,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                textAlign: TextAlign.center,
                style: const TextStyle(
                  fontFamily: EllaTextStyles.uiFont,
                  color: EllaColors.ink,
                  fontSize: 16,
                  fontWeight: FontWeight.w600,
                  height: 1.3,
                ),
              ),
              SizedBox(height: _turns.isEmpty ? 28 : 18),
              if (_turns.isEmpty)
                Center(
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(maxWidth: 300),
                    child: Text(
                      context.l10n.memoryTalkOpening(_whenLabel(), _openingTitle()),
                      textAlign: TextAlign.center,
                      style: EllaTextStyles.noteBody,
                    ),
                  ),
                )
              else
                Expanded(
                  child: ListView(
                    controller: _scrollController,
                    padding: EdgeInsets.zero,
                    children: [
                      Center(
                        child: ConstrainedBox(
                          constraints: const BoxConstraints(maxWidth: 300),
                          child: Text(
                            context.l10n.memoryTalkOpening(_whenLabel(), _openingTitle()),
                            textAlign: TextAlign.center,
                            style: EllaTextStyles.noteBody,
                          ),
                        ),
                      ),
                      const SizedBox(height: 18),
                      for (final turn in _turns) _TalkBubble(turn: turn),
                    ],
                  ),
                ),
              SizedBox(height: _turns.isEmpty ? 24 : 10),
              Container(
                constraints: const BoxConstraints(minHeight: 48),
                decoration: BoxDecoration(
                  color: EllaColors.card,
                  borderRadius: BorderRadius.circular(24),
                  border: Border.all(color: _focusNode.hasFocus ? EllaColors.teal : EllaColors.cardDeep, width: 1.5),
                ),
                padding: const EdgeInsets.only(left: 18, right: 8),
                child: Row(
                  children: [
                    Expanded(
                      child: TextField(
                        controller: _controller,
                        focusNode: _focusNode,
                        enabled: !_isSending,
                        minLines: 1,
                        maxLines: 3,
                        textInputAction: TextInputAction.send,
                        onSubmitted: (_) => _send(),
                        onTap: () => setState(() {}),
                        style: EllaTextStyles.secondary.copyWith(color: EllaColors.ink),
                        decoration: InputDecoration(
                          hintText: context.l10n.memoryTalkComposerHint,
                          hintStyle: EllaTextStyles.secondary,
                          border: InputBorder.none,
                          enabledBorder: InputBorder.none,
                          focusedBorder: InputBorder.none,
                          disabledBorder: InputBorder.none,
                          errorBorder: InputBorder.none,
                          focusedErrorBorder: InputBorder.none,
                          isDense: true,
                          contentPadding: EdgeInsets.zero,
                        ),
                      ),
                    ),
                    Semantics(
                      button: true,
                      label: context.l10n.memoryTalkSend,
                      child: IconButton(
                        onPressed: _isSending ? null : _send,
                        icon: _isSending
                            ? const SizedBox(
                                width: 20,
                                height: 20,
                                child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
                              )
                            : const FaIcon(FontAwesomeIcons.paperPlane, size: 18),
                        color: EllaColors.tealDeep,
                        style: IconButton.styleFrom(
                          disabledForegroundColor: EllaColors.inkSoft,
                          padding: EdgeInsets.zero,
                          minimumSize: const Size(36, 36),
                          maximumSize: const Size(36, 36),
                        ),
                      ),
                    ),
                  ],
                ),
              ),
              if (_showVoiceComingSoon)
                Padding(
                  padding: const EdgeInsets.only(top: 7),
                  child: Text(context.l10n.memoryTalkVoiceComingSoon, style: EllaTextStyles.caption),
                ),
              if (_turns.isEmpty) const Spacer(),
              const SizedBox(height: 4),
              Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  SizedBox(
                    height: 48,
                    width: 132,
                    child: FilledButton(
                      onPressed: _finish,
                      style: FilledButton.styleFrom(
                        backgroundColor: EllaColors.cardDeep,
                        foregroundColor: EllaColors.tealDeep,
                        minimumSize: const Size(132, 48),
                        maximumSize: const Size(132, 48),
                        shape: const StadiumBorder(),
                      ),
                      child: Text(
                        context.l10n.memoryTalkDone,
                        style: const TextStyle(
                          fontFamily: EllaTextStyles.uiFont,
                          fontSize: 17,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(width: 12),
                  IconButton(
                    tooltip: context.l10n.memoryTalkVoiceComingSoon,
                    onPressed: () {
                      HapticFeedback.selectionClick();
                      setState(() => _showVoiceComingSoon = true);
                    },
                    icon: const FaIcon(FontAwesomeIcons.microphone, size: 18, color: EllaColors.tealDeep),
                    style: IconButton.styleFrom(
                      backgroundColor: EllaColors.cardDeep,
                      minimumSize: const Size(48, 48),
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _TalkBubble extends StatelessWidget {
  final _TalkTurn turn;

  const _TalkBubble({required this.turn});

  @override
  Widget build(BuildContext context) {
    if (turn.isElla) {
      return Padding(
        padding: const EdgeInsets.only(bottom: 14),
        child: Text(
          turn.text,
          textAlign: TextAlign.center,
          style: EllaTextStyles.noteBody,
        ),
      );
    }
    return Align(
      alignment: Alignment.centerRight,
      child: Container(
        constraints: const BoxConstraints(maxWidth: 315),
        margin: const EdgeInsets.only(bottom: 14),
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        decoration: BoxDecoration(
          color: EllaColors.card,
          borderRadius: BorderRadius.circular(18),
        ),
        child: Text(
          turn.text,
          style: EllaTextStyles.secondary.copyWith(color: EllaColors.ink),
          textAlign: TextAlign.right,
        ),
      ),
    );
  }
}
