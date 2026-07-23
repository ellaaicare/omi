import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:font_awesome_flutter/font_awesome_flutter.dart';
import 'package:gradient_borders/box_borders/gradient_box_border.dart';
import 'package:provider/provider.dart';
import 'package:share_plus/share_plus.dart';
import 'package:tuple/tuple.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/http/webhooks.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/app.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/geolocation.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/backend/schema/person.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/services/memory_talk_service.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/pages/apps/app_detail/app_detail.dart';
import 'package:omi/pages/conversation_detail/conversation_detail_provider.dart';
import 'package:omi/pages/conversation_detail/test_prompts.dart';
import 'package:omi/pages/conversation_detail/widgets/conversation_markdown_widget.dart';
import 'package:omi/pages/conversation_detail/widgets/summarized_apps_sheet.dart';
import 'package:omi/pages/settings/developer.dart';
import 'package:omi/utils/analytics/mixpanel.dart';
import 'package:omi/utils/other/temp.dart';
import 'package:omi/utils/other/time_utils.dart';
import 'package:omi/widgets/dialog.dart';
import 'package:omi/widgets/extensions/string.dart';
import 'maps_util.dart';
import 'package:omi/ella/ella_theme.dart';

// Highlight search matches with current result highlighting
List<TextSpan> highlightSearchMatches(String text, String searchQuery, {int currentResultIndex = -1}) {
  if (searchQuery.isEmpty) {
    return [TextSpan(text: text)];
  }

  final List<TextSpan> spans = [];
  final String lowerText = text.toLowerCase();
  final String lowerQuery = searchQuery.toLowerCase();

  int start = 0;
  int index = lowerText.indexOf(lowerQuery, start);
  int matchCount = 0;

  while (index != -1) {
    if (index > start) {
      spans.add(TextSpan(text: text.substring(start, index)));
    }

    bool isCurrentResult = currentResultIndex >= 0 && matchCount == currentResultIndex;

    spans.add(
      TextSpan(
        text: text.substring(index, index + searchQuery.length),
        style: TextStyle(
          backgroundColor:
              isCurrentResult ? Colors.orange.withValues(alpha: 0.9) : EllaColors.primary.withValues(alpha: 0.6),
          color: Colors.white,
          fontWeight: FontWeight.bold,
        ),
      ),
    );

    matchCount++;
    start = index + searchQuery.length;
    index = lowerText.indexOf(lowerQuery, start);
  }

  // Add remaining text
  if (start < text.length) {
    spans.add(TextSpan(text: text.substring(start)));
  }

  return spans;
}

class GetSummaryWidgets extends StatelessWidget {
  final String searchQuery;
  const GetSummaryWidgets({super.key, this.searchQuery = ''});

  String setTime(DateTime? startedAt, DateTime createdAt, DateTime? finishedAt) {
    return startedAt == null ? dateTimeFormat('h:mm a', createdAt) : dateTimeFormat('h:mm a', startedAt);
  }

  String setTimeSDCard(DateTime? startedAt, DateTime createdAt) {
    return startedAt == null ? dateTimeFormat('h:mm a', createdAt) : dateTimeFormat('h:mm a', startedAt);
  }

  String _getDuration(BuildContext context, ServerConversation conversation) {
    if (conversation.transcriptSegments.isEmpty) return '';

    int durationSeconds = conversation.getDurationInSeconds();
    if (durationSeconds <= 0) return '';

    return secondsToHumanReadable(durationSeconds, context);
  }

  String _getDateFormat(BuildContext context, DateTime date) {
    final now = DateTime.now();
    final today = DateTime(now.year, now.month, now.day);
    final yesterday = today.subtract(const Duration(days: 1));
    final dateOnly = DateTime(date.year, date.month, date.day);

    if (dateOnly == today) {
      return context.l10n.today;
    } else if (dateOnly == yesterday) {
      return context.l10n.yesterday;
    } else if (date.year == now.year) {
      return dateTimeFormat('MMM d', date);
    } else {
      return dateTimeFormat('MMM d, yyyy', date);
    }
  }

  Widget _buildInfoChips(BuildContext context, ServerConversation conversation) {
    return Wrap(
      spacing: 8,
      runSpacing: 8,
      children: [
        _buildChip(
          label: _getDateFormat(context, conversation.startedAt ?? conversation.createdAt),
          icon: Icons.calendar_today,
        ),
        _buildChip(
          label: conversation.source == ConversationSource.sdcard
              ? setTimeSDCard(conversation.startedAt, conversation.createdAt)
              : setTime(conversation.startedAt, conversation.createdAt, conversation.finishedAt),
          icon: Icons.access_time,
        ),
        if (conversation.transcriptSegments.isNotEmpty && _getDuration(context, conversation).isNotEmpty)
          _buildChip(label: _getDuration(context, conversation), icon: Icons.timelapse),
      ],
    );
  }

  Widget _buildChip({required String label, required IconData icon}) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: BoxDecoration(
        color: EllaColors.bgTertiary.withValues(alpha: 0.5),
        borderRadius: BorderRadius.circular(20),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 14, color: EllaColors.textTertiary),
          const SizedBox(width: 6),
          Text(
            label,
            style: const TextStyle(color: EllaColors.textTertiary, fontSize: 13, fontWeight: FontWeight.w500),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Selector<ConversationDetailProvider, Tuple3<ServerConversation, TextEditingController?, FocusNode?>>(
      selector: (context, provider) => Tuple3(provider.conversation, provider.titleController, provider.titleFocusNode),
      builder: (context, data, child) {
        ServerConversation conversation = data.item1;
        return Column(
          mainAxisAlignment: MainAxisAlignment.start,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const SizedBox(height: 8),
            conversation.discarded
                ? Text(
                    context.l10n.discardedConversation,
                    style: const TextStyle(
                      fontSize: 24,
                      fontWeight: FontWeight.w600,
                      color: EllaColors.textSecondary,
                      height: 1.3,
                    ),
                  )
                : GetEditTextField(
                    conversationId: conversation.id,
                    focusNode: data.item3,
                    controller: data.item2,
                    content: conversation.structured.title.decodeString,
                    style: const TextStyle(
                      fontSize: 24,
                      fontWeight: FontWeight.w600,
                      color: EllaColors.textPrimary,
                      height: 1.3,
                    ),
                  ),
            const SizedBox(height: 16),
            _buildInfoChips(context, conversation),
            const SizedBox(height: 16),
            conversation.discarded ? const SizedBox.shrink() : const SizedBox(height: 8),
          ],
        );
      },
    );
  }
}

class ActionItemsListWidget extends StatelessWidget {
  const ActionItemsListWidget({super.key});

  @override
  Widget build(BuildContext context) {
    return Consumer<ConversationDetailProvider>(
      builder: (context, provider, child) {
        return Column(
          children: [
            provider.conversation.structured.actionItems.isNotEmpty
                ? Row(
                    crossAxisAlignment: CrossAxisAlignment.center,
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      Text(
                        context.l10n.actionItems,
                        style: Theme.of(context).textTheme.titleLarge!.copyWith(fontSize: 26),
                      ),
                      IconButton(
                        onPressed: () {
                          Clipboard.setData(
                            ClipboardData(
                              text:
                                  '- ${provider.conversation.structured.actionItems.map((e) => e.description.decodeString).join('\n- ')}',
                            ),
                          );
                          ScaffoldMessenger.of(context).showSnackBar(
                            SnackBar(
                              content: Text(context.l10n.actionItemsCopiedToClipboard),
                              duration: const Duration(seconds: 2),
                            ),
                          );
                          MixpanelManager().copiedConversationDetails(provider.conversation, source: 'Action Items');
                        },
                        icon: const Icon(Icons.copy_rounded, color: EllaColors.textSecondary, size: 20),
                      ),
                    ],
                  )
                : const SizedBox.shrink(),
            ListView.builder(
              itemCount: provider.conversation.structured.actionItems.where((e) => !e.deleted).length,
              shrinkWrap: true,
              physics: const NeverScrollableScrollPhysics(),
              itemBuilder: (context, idx) {
                var item = provider.conversation.structured.actionItems.where((e) => !e.deleted).toList()[idx];
                return Dismissible(
                  key: Key(item.description),
                  direction: DismissDirection.endToStart,
                  background: Container(
                    alignment: Alignment.centerRight,
                    padding: const EdgeInsets.only(right: 20.0),
                    color: Colors.red,
                    child: const Icon(Icons.delete, color: Colors.white),
                  ),
                  onDismissed: (direction) {
                    var tempItem = provider.conversation.structured.actionItems[idx];
                    var tempIdx = idx;
                    provider.deleteActionItem(idx);
                    provider.deleteActionItemPermanently(tempItem, tempIdx);
                    MixpanelManager().deletedActionItem(provider.conversation);
                    // ScaffoldMessenger.of(context)
                    //     .showSnackBar(
                    //       SnackBar(
                    //         content: const Text('Action Item deleted successfully 🗑️'),
                    //         padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 16),
                    //         action: SnackBarAction(
                    //           label: 'Undo',
                    //           textColor: Colors.white,
                    //           onPressed: () {
                    //             provider.undoDeleteActionItem(idx);
                    //           },
                    //         ),
                    //       ),
                    //     )
                    //     .closed
                    //     .then((reason) {
                    //   if (reason != SnackBarClosedReason.action) {
                    //     provider.deleteActionItemPermanently(tempItem, tempIdx);
                    //     MixpanelManager().deletedActionItem(provider.conversation);
                    //   }
                    // });
                  },
                  child: Padding(
                    padding: const EdgeInsets.only(top: 10, bottom: 2),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Padding(
                          padding: const EdgeInsets.only(top: 6.0),
                          child: SizedBox(
                            height: 22.0,
                            width: 22.0,
                            child: Checkbox(
                              shape: const CircleBorder(),
                              value: item.completed,
                              onChanged: (value) {
                                if (value != null) {
                                  context.read<ConversationDetailProvider>().updateActionItemState(value, idx);
                                  setConversationActionItemState(provider.conversation.id, [idx], [value]);
                                  if (value) {
                                    MixpanelManager().checkedActionItem(provider.conversation, idx);
                                  } else {
                                    MixpanelManager().uncheckedActionItem(provider.conversation, idx);
                                  }
                                }
                              },
                            ),
                          ),
                        ),
                        const SizedBox(width: 12),
                        Expanded(
                          child: SelectionArea(
                            child: Text(
                              item.description.decodeString,
                              style: const TextStyle(color: EllaColors.textSecondary, fontSize: 16, height: 1.3),
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                );
              },
            ),
          ],
        );
      },
    );
  }
}

class GetEditTextField extends StatefulWidget {
  final String conversationId;
  final String content;
  final TextStyle style;
  final TextEditingController? controller;
  final FocusNode? focusNode;

  const GetEditTextField({
    super.key,
    required this.content,
    required this.style,
    required this.conversationId,
    required this.controller,
    required this.focusNode,
  });

  @override
  State<GetEditTextField> createState() => _GetEditTextFieldState();
}

class _GetEditTextFieldState extends State<GetEditTextField> {
  @override
  Widget build(BuildContext context) {
    return TextField(
      keyboardType: TextInputType.multiline,
      minLines: 1,
      maxLines: 3,
      focusNode: widget.focusNode,
      decoration: const InputDecoration(
        border: OutlineInputBorder(borderSide: BorderSide.none),
        contentPadding: EdgeInsets.all(0),
        filled: false,
      ),
      controller: widget.controller,
      enabled: true,
      style: widget.style,
    );
  }
}

class ReprocessDiscardedWidget extends StatelessWidget {
  const ReprocessDiscardedWidget({super.key});

  @override
  Widget build(BuildContext context) {
    return Consumer<ConversationDetailProvider>(
      builder: (context, provider, child) {
        if (provider.loadingReprocessConversation && provider.reprocessConversationId == provider.conversation.id) {
          return Center(
            child: Padding(
              padding: const EdgeInsets.only(top: 18.0),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.center,
                crossAxisAlignment: CrossAxisAlignment.center,
                children: [
                  const CircularProgressIndicator(valueColor: AlwaysStoppedAnimation<Color>(EllaColors.primary)),
                  const SizedBox(width: 16),
                  Text(
                    provider.conversation.discarded
                        ? context.l10n.summarizingConversation
                        : context.l10n.resummarizingConversation,
                    style: const TextStyle(color: EllaColors.textPrimary, fontSize: 16),
                  ),
                ],
              ),
            ),
          );
        }
        return ListView(
          shrinkWrap: true,
          children: [
            const SizedBox(height: 32),
            Text(
              context.l10n.nothingInterestingRetry,
              style: Theme.of(context).textTheme.titleLarge!.copyWith(fontSize: 20),
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 24),
            Row(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Container(
                  decoration: BoxDecoration(
                    border: const GradientBoxBorder(
                      gradient: LinearGradient(
                        colors: [
                          Color.fromARGB(127, 208, 208, 208),
                          Color.fromARGB(127, 188, 99, 121),
                          Color.fromARGB(127, 86, 101, 182),
                          Color.fromARGB(127, 126, 190, 236),
                        ],
                      ),
                      width: 2,
                    ),
                    borderRadius: BorderRadius.circular(12),
                  ),
                  child: MaterialButton(
                    onPressed: () async {
                      await provider.reprocessConversation();
                    },
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                    child: Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 0),
                      child: Text(
                        context.l10n.summarize,
                        style: const TextStyle(color: EllaColors.textPrimary, fontSize: 16),
                      ),
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 32),
          ],
        );
      },
    );
  }
}

class AppResultDetailWidget extends StatelessWidget {
  final AppResponse appResponse;
  final App? app;
  final ServerConversation conversation;
  final String searchQuery;
  final int currentResultIndex;

  const AppResultDetailWidget({
    super.key,
    required this.appResponse,
    required this.app,
    required this.conversation,
    this.searchQuery = '',
    this.currentResultIndex = -1,
  });

  @override
  Widget build(BuildContext context) {
    final String content = appResponse.content.trim().decodeString;

    return Container(
      margin: const EdgeInsets.only(bottom: 20),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.start,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 8),
            child: content.isEmpty
                ? Row(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      Expanded(
                        child: GestureDetector(
                          onTap: () {
                            showModalBottomSheet(
                              context: context,
                              isScrollControlled: true,
                              backgroundColor: Colors.transparent,
                              builder: (context) => const SummarizedAppsBottomSheet(),
                            );
                          },
                          child: RichText(
                            text: TextSpan(
                              style: const TextStyle(color: Colors.grey),
                              text: context.l10n.noSummaryForApp,
                            ),
                          ),
                        ),
                      ),
                    ],
                  )
                : ConversationMarkdownWidget(
                    content: content,
                    searchQuery: searchQuery,
                    currentResultIndex: currentResultIndex,
                  ),
          ),

          // App info - only show when a specific app processed this conversation
          if (content.isNotEmpty && app != null)
            GestureDetector(
              onTap: () async {
                MixpanelManager().pageOpened('App Detail');
                await routeToPage(context, AppDetailPage(app: app!));
              },
              child: Padding(
                padding: const EdgeInsets.only(top: 12, left: 4),
                child: Row(
                  children: [
                    CachedNetworkImage(
                      imageUrl: app!.getImageUrl(),
                      imageBuilder: (context, imageProvider) {
                        return CircleAvatar(backgroundColor: Colors.white, radius: 12, backgroundImage: imageProvider);
                      },
                      errorWidget: (context, url, error) {
                        return const CircleAvatar(
                          backgroundColor: Colors.white,
                          radius: 12,
                          child: Icon(Icons.error_outline_rounded, size: 12),
                        );
                      },
                      progressIndicatorBuilder: (context, url, progress) => CircleAvatar(
                        backgroundColor: Colors.white,
                        radius: 12,
                        child: CircularProgressIndicator(
                          value: progress.progress,
                          valueColor: const AlwaysStoppedAnimation<Color>(EllaColors.primary),
                          strokeWidth: 2,
                        ),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Row(
                        children: [
                          Expanded(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(
                                  app!.name.decodeString,
                                  maxLines: 1,
                                  style: const TextStyle(
                                    fontWeight: FontWeight.w500,
                                    color: EllaColors.textPrimary,
                                    fontSize: 14,
                                  ),
                                ),
                                Text(
                                  app!.description.decodeString,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(color: EllaColors.textTertiary, fontSize: 12),
                                ),
                              ],
                            ),
                          ),
                          const SizedBox(
                            width: 42,
                            child: Icon(Icons.arrow_forward_ios, color: EllaColors.textSecondary, size: 20),
                          ),
                        ],
                      ),
                    ),
                  ],
                ),
              ),
            ),
        ],
      ),
    );
  }
}

void showCorrectSummarySheet({
  required BuildContext context,
  required ServerConversation conversation,
  required String appSummary,
}) {
  HapticFeedback.lightImpact();
  showModalBottomSheet(
    context: context,
    isScrollControlled: true,
    backgroundColor: Colors.transparent,
    builder: (context) => _CorrectSummarySheet(conversation: conversation, appSummary: appSummary),
  );
}

class _CorrectSummarySheet extends StatefulWidget {
  final ServerConversation conversation;
  final String appSummary;

  const _CorrectSummarySheet({required this.conversation, required this.appSummary});

  @override
  State<_CorrectSummarySheet> createState() => _CorrectSummarySheetState();
}

class _CorrectSummarySheetState extends State<_CorrectSummarySheet> {
  final TextEditingController _controller = TextEditingController();
  bool _isSubmitting = false;

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final correctionText = _controller.text.trim();
    if (correctionText.isEmpty || _isSubmitting) return;

    setState(() => _isSubmitting = true);
    final messenger = ScaffoldMessenger.of(context);
    final navigator = Navigator.of(context);

    final ok = await submitConversationCorrection(
      conversationId: widget.conversation.id,
      correctionText: correctionText,
      summaryTitle: widget.conversation.structured.title,
      summaryOverview: widget.conversation.structured.overview,
      appSummary: widget.appSummary,
    );

    if (!mounted) return;
    setState(() => _isSubmitting = false);

    if (ok) {
      HapticFeedback.mediumImpact();
      messenger.showSnackBar(SnackBar(content: Text(context.l10n.memoryTalkUpdatedReply)));
      navigator.pop();
    } else {
      HapticFeedback.lightImpact();
      messenger.showSnackBar(SnackBar(content: Text(context.l10n.memoryTalkUpdateFailed)));
    }
  }

  @override
  Widget build(BuildContext context) {
    final bottomInset = MediaQuery.of(context).viewInsets.bottom;

    return Padding(
      padding: EdgeInsets.only(bottom: bottomInset),
      child: Container(
        decoration: const BoxDecoration(
          color: EllaColors.bgPrimary,
          borderRadius: BorderRadius.vertical(top: Radius.circular(28)),
        ),
        padding: const EdgeInsets.fromLTRB(20, 16, 20, 20),
        child: SafeArea(
          top: false,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Center(
                child: Container(
                  width: 44,
                  height: 4,
                  decoration: BoxDecoration(color: EllaColors.bgTertiary, borderRadius: BorderRadius.circular(999)),
                ),
              ),
              const SizedBox(height: 20),
              Text(
                context.l10n.fixSomething,
                style: TextStyle(color: EllaColors.textPrimary, fontSize: 22, fontWeight: FontWeight.w700),
              ),
              const SizedBox(height: 8),
              Text(
                context.l10n.fixSomethingSheetBody,
                style: TextStyle(color: EllaColors.textSecondary, fontSize: 15, height: 1.35),
              ),
              const SizedBox(height: 16),
              TextField(
                controller: _controller,
                autofocus: true,
                minLines: 4,
                maxLines: 8,
                textInputAction: TextInputAction.newline,
                style: const TextStyle(color: EllaColors.textPrimary, fontSize: 16),
                decoration: InputDecoration(
                  hintText: context.l10n.fixSomethingHint,
                  hintStyle: const TextStyle(color: EllaColors.textTertiary),
                  filled: true,
                  fillColor: EllaColors.bgSecondary,
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(18),
                    borderSide: const BorderSide(color: EllaColors.bgTertiary),
                  ),
                  enabledBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(18),
                    borderSide: const BorderSide(color: EllaColors.bgTertiary),
                  ),
                  focusedBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(18),
                    borderSide: const BorderSide(color: EllaColors.primary, width: 1.5),
                  ),
                ),
              ),
              const SizedBox(height: 16),
              SizedBox(
                width: double.infinity,
                child: FilledButton(
                  onPressed: _isSubmitting ? null : _submit,
                  style: FilledButton.styleFrom(
                    backgroundColor: EllaColors.primary,
                    foregroundColor: Colors.white,
                    disabledBackgroundColor: EllaColors.bgTertiary,
                    padding: const EdgeInsets.symmetric(vertical: 14),
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
                  ),
                  child: _isSubmitting
                      ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white),
                        )
                      : Text(context.l10n.submitChange),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class MemoryTalkPill extends StatelessWidget {
  final VoidCallback onPressed;

  const MemoryTalkPill({super.key, required this.onPressed});

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(999),
        onTap: onPressed,
        child: Ink(
          height: 56,
          padding: const EdgeInsets.symmetric(horizontal: 22),
          decoration: BoxDecoration(
            color: const Color(0xFF38695E),
            borderRadius: BorderRadius.circular(999),
            boxShadow: [
              BoxShadow(
                color: EllaColors.textPrimary.withValues(alpha: 0.18),
                blurRadius: 18,
                offset: const Offset(0, 8),
              ),
            ],
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              const FaIcon(FontAwesomeIcons.microphone, size: 17, color: Color(0xFFFAF6F0)),
              const SizedBox(width: 10),
              Text(
                context.l10n.talkAboutThis,
                style: const TextStyle(color: Color(0xFFFAF6F0), fontSize: 18, fontWeight: FontWeight.w700),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

void showMemoryTalkSheet({
  required BuildContext context,
  required ServerConversation conversation,
  required String appSummary,
  required List<Person> people,
}) {
  HapticFeedback.lightImpact();
  showModalBottomSheet(
    context: context,
    isScrollControlled: true,
    backgroundColor: Colors.transparent,
    builder: (context) => _MemoryTalkSheet(conversation: conversation, appSummary: appSummary, people: people),
  );
}

class _MemoryTalkSheet extends StatefulWidget {
  final ServerConversation conversation;
  final String appSummary;
  final List<Person> people;

  const _MemoryTalkSheet({required this.conversation, required this.appSummary, required this.people});

  @override
  State<_MemoryTalkSheet> createState() => _MemoryTalkSheetState();
}

class _MemoryTalkSheetState extends State<_MemoryTalkSheet> {
  final TextEditingController _controller = TextEditingController();
  final FocusNode _focusNode = FocusNode();
  final MemoryTalkCorrectionExtractor _extractor = const MemoryTalkCorrectionExtractor();
  final List<MemoryTalkMessage> _messages = [];
  PendingMemoryCorrectionClaim? _pendingCorrection;
  bool _isSending = false;
  bool _didAddOpeningLine = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _focusNode.requestFocus());
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (_didAddOpeningLine) return;
    _didAddOpeningLine = true;
    _messages.add(MemoryTalkMessage(text: _openingLine(), isUser: false, createdAt: DateTime.now()));
  }

  @override
  void dispose() {
    _controller.dispose();
    _focusNode.dispose();
    super.dispose();
  }

  String _whenText() {
    final date = widget.conversation.startedAt ?? widget.conversation.createdAt;
    final now = DateTime.now();
    final today = DateTime(now.year, now.month, now.day);
    final dateOnly = DateTime(date.year, date.month, date.day);
    if (dateOnly == today) {
      if (date.hour < 12) return context.l10n.thisMorning;
      if (date.hour < 17) return context.l10n.thisAfternoon;
      return context.l10n.thisEvening;
    }
    if (dateOnly == today.subtract(const Duration(days: 1))) {
      return context.l10n.yesterday;
    }
    return dateTimeFormat(date.year == now.year ? 'MMM d' : 'MMM d, yyyy', date);
  }

  String _personLanguageTitle() {
    final stripped = widget.conversation.structured.title.replaceAll(RegExp(r'^[^\w]+'), '').trim();
    if (stripped.isEmpty) return context.l10n.thisMemory;
    return stripped.substring(0, 1).toLowerCase() + stripped.substring(1);
  }

  String _openingLine() => context.l10n.memoryTalkOpeningLine(_whenText(), _personLanguageTitle());

  Future<void> _send() async {
    final text = _controller.text.trim();
    if (text.isEmpty || _isSending) return;
    _controller.clear();
    setState(() {
      _messages.add(MemoryTalkMessage(text: text, isUser: true, createdAt: DateTime.now()));
      _isSending = true;
    });

    final provider = context.read<ConversationDetailProvider>();
    provider.markMemoryTalkStarted();

    final pending = _pendingCorrection;
    if (pending != null) {
      if (isAffirmativeCorrectionReply(text)) {
        await _applyPendingCorrection(pending, provider);
        return;
      }
      if (isNegativeCorrectionReply(text)) {
        setState(() {
          _pendingCorrection = null;
          _messages.add(
            MemoryTalkMessage(
              text: context.l10n.memoryTalkCorrectionCancelled,
              isUser: false,
              createdAt: DateTime.now(),
            ),
          );
          _isSending = false;
        });
        return;
      }
    }

    final claim = _extractor.extract(text);
    if (claim != null) {
      setState(() {
        _pendingCorrection = claim;
        _messages.add(
          MemoryTalkMessage(
            text: claim.confirmationText(context.l10n.memoryTalkConfirmCorrection),
            isUser: false,
            createdAt: DateTime.now(),
          ),
        );
        _isSending = false;
      });
      return;
    }

    await _sendScopedChat(text);
  }

  Future<void> _applyPendingCorrection(
    PendingMemoryCorrectionClaim pending,
    ConversationDetailProvider provider,
  ) async {
    final result = await submitConversationCorrectionDetailed(
      conversationId: widget.conversation.id,
      correctionText: pending.correctionText,
      summaryTitle: widget.conversation.structured.title,
      summaryOverview: widget.conversation.structured.overview,
      appSummary: widget.appSummary,
    );
    if (!mounted) return;
    if (result != null) {
      provider.setMemoryTalkReceipt(
        MemoryTalkLocalReceipt(correctionId: result.correctionId, oldText: pending.oldText, newText: pending.newText),
      );
      unawaited(provider.refreshConversation());
      setState(() {
        _pendingCorrection = null;
        _messages.add(
          MemoryTalkMessage(text: context.l10n.memoryTalkUpdatedReply, isUser: false, createdAt: DateTime.now()),
        );
        _isSending = false;
      });
      return;
    }
    setState(() {
      _messages.add(
        MemoryTalkMessage(text: context.l10n.memoryTalkUpdateFailed, isUser: false, createdAt: DateTime.now()),
      );
      _isSending = false;
    });
  }

  Future<void> _sendScopedChat(String text) async {
    final contextPack = buildMemoryTalkContext(
      conversation: widget.conversation,
      appSummary: widget.appSummary,
      people: widget.people,
    );
    final replyBuffer = StringBuffer();
    await for (final chunk in sendMemoryScopedEllaChatStream(
      conversation: widget.conversation,
      text: text,
      scopedContext: contextPack,
    )) {
      if (chunk.type == MessageChunkType.data) {
        replyBuffer.write(chunk.text);
      } else if (chunk.type == MessageChunkType.done && chunk.message != null && chunk.message!.text.isNotEmpty) {
        replyBuffer
          ..clear()
          ..write(chunk.message!.text);
      }
    }
    if (!mounted) return;
    final reply = replyBuffer.toString().trim();
    setState(() {
      _messages.add(
        MemoryTalkMessage(
          text: reply.isEmpty ? context.l10n.memoryTalkEmptyReply : reply,
          isUser: false,
          createdAt: DateTime.now(),
        ),
      );
      _isSending = false;
    });
  }

  @override
  Widget build(BuildContext context) {
    final bottomInset = MediaQuery.of(context).viewInsets.bottom;
    return Padding(
      padding: EdgeInsets.only(bottom: bottomInset),
      child: FractionallySizedBox(
        heightFactor: 0.60,
        child: Container(
          decoration: const BoxDecoration(
            color: Color(0xFFFAF6F0),
            borderRadius: BorderRadius.vertical(top: Radius.circular(28)),
          ),
          padding: const EdgeInsets.fromLTRB(20, 12, 20, 14),
          child: SafeArea(
            top: false,
            child: Column(
              children: [
                Container(
                  width: 44,
                  height: 4,
                  decoration: BoxDecoration(color: const Color(0xFFE9DFD2), borderRadius: BorderRadius.circular(999)),
                ),
                const SizedBox(height: 18),
                Text(
                  context.l10n.talkingAbout,
                  style: const TextStyle(
                    color: Color(0xFF6B655D),
                    fontSize: 12,
                    fontWeight: FontWeight.w700,
                    letterSpacing: 1.5,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  widget.conversation.structured.title,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(color: Color(0xFF23201C), fontSize: 18, fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 14),
                Expanded(
                  child: ListView.separated(
                    padding: EdgeInsets.zero,
                    itemCount: _messages.length,
                    separatorBuilder: (_, __) => const SizedBox(height: 10),
                    itemBuilder: (context, index) => _MemoryTalkBubble(message: _messages[index]),
                  ),
                ),
                const SizedBox(height: 12),
                Row(
                  crossAxisAlignment: CrossAxisAlignment.end,
                  children: [
                    Expanded(
                      child: TextField(
                        controller: _controller,
                        focusNode: _focusNode,
                        minLines: 1,
                        maxLines: 4,
                        textInputAction: TextInputAction.send,
                        onSubmitted: (_) => _send(),
                        style: const TextStyle(color: Color(0xFF23201C), fontSize: 18, height: 1.35),
                        decoration: InputDecoration(
                          hintText: context.l10n.memoryTalkComposerHint,
                          hintStyle: const TextStyle(color: Color(0xFF6B655D)),
                          filled: true,
                          fillColor: const Color(0xFFF2EBE1),
                          suffixIcon: IconButton(
                            onPressed: _isSending ? null : _send,
                            icon: const Icon(Icons.arrow_upward_rounded, color: Color(0xFF38695E)),
                          ),
                          border: OutlineInputBorder(
                            borderRadius: BorderRadius.circular(22),
                            borderSide: const BorderSide(color: Color(0xFF38695E), width: 1.5),
                          ),
                          enabledBorder: OutlineInputBorder(
                            borderRadius: BorderRadius.circular(22),
                            borderSide: const BorderSide(color: Color(0xFFE9DFD2)),
                          ),
                          focusedBorder: OutlineInputBorder(
                            borderRadius: BorderRadius.circular(22),
                            borderSide: const BorderSide(color: Color(0xFF38695E), width: 1.5),
                          ),
                        ),
                      ),
                    ),
                    const SizedBox(width: 8),
                    _SheetPillButton(label: context.l10n.done, onTap: () => Navigator.pop(context)),
                    const SizedBox(width: 8),
                    _SheetRoundButton(
                      tooltip: context.l10n.voiceSoon,
                      onTap: () =>
                          ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.voiceSoon))),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _MemoryTalkBubble extends StatelessWidget {
  final MemoryTalkMessage message;

  const _MemoryTalkBubble({required this.message});

  @override
  Widget build(BuildContext context) {
    final isUser = message.isUser;
    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.center,
      child: ConstrainedBox(
        constraints: BoxConstraints(maxWidth: MediaQuery.of(context).size.width * (isUser ? 0.72 : 0.86)),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
          decoration: BoxDecoration(
            color: isUser ? const Color(0xFF5A9E8F) : Colors.transparent,
            borderRadius: BorderRadius.circular(18),
          ),
          child: Text(
            message.text,
            textAlign: isUser ? TextAlign.right : TextAlign.center,
            style: TextStyle(
              color: isUser ? const Color(0xFFFAF6F0) : const Color(0xFF23201C),
              fontSize: isUser ? 17 : 22,
              height: isUser ? 1.35 : 1.45,
              fontWeight: isUser ? FontWeight.w500 : FontWeight.w500,
              fontFamily: isUser ? 'Manrope' : 'Fraunces',
            ),
          ),
        ),
      ),
    );
  }
}

class _SheetPillButton extends StatelessWidget {
  final String label;
  final VoidCallback onTap;

  const _SheetPillButton({required this.label, required this.onTap});

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 48,
      child: FilledButton(
        onPressed: onTap,
        style: FilledButton.styleFrom(
          backgroundColor: const Color(0xFFE9DFD2),
          foregroundColor: const Color(0xFF38695E),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(999)),
          padding: const EdgeInsets.symmetric(horizontal: 16),
        ),
        child: Text(label),
      ),
    );
  }
}

class _SheetRoundButton extends StatelessWidget {
  final String tooltip;
  final VoidCallback onTap;

  const _SheetRoundButton({required this.tooltip, required this.onTap});

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: tooltip,
      child: InkWell(
        borderRadius: BorderRadius.circular(999),
        onTap: onTap,
        child: Container(
          width: 48,
          height: 48,
          decoration: const BoxDecoration(color: Color(0xFFE9DFD2), shape: BoxShape.circle),
          child: const Icon(Icons.mic_none_rounded, color: Color(0xFF38695E)),
        ),
      ),
    );
  }
}

class MemoryCorrectionReceiptWidget extends StatefulWidget {
  final MemoryTalkLocalReceipt? localReceipt;
  final ConversationCorrectionState? correctionState;
  final Future<void> Function(String correctionId) onUndo;

  const MemoryCorrectionReceiptWidget({
    super.key,
    required this.localReceipt,
    required this.correctionState,
    required this.onUndo,
  });

  @override
  State<MemoryCorrectionReceiptWidget> createState() => _MemoryCorrectionReceiptWidgetState();
}

class _MemoryCorrectionReceiptWidgetState extends State<MemoryCorrectionReceiptWidget> {
  bool _expanded = false;
  bool _undoing = false;

  String? get _correctionId => widget.localReceipt?.correctionId ?? widget.correctionState?.correctionId;

  String get _oldText {
    final local = widget.localReceipt?.oldText;
    if (local != null && local.isNotEmpty) return local;
    return widget.correctionState?.before?['title']?.toString() ?? '';
  }

  String get _newText {
    final local = widget.localReceipt?.newText;
    if (local != null && local.isNotEmpty) return local;
    return widget.correctionState?.after?['title']?.toString() ?? '';
  }

  @override
  Widget build(BuildContext context) {
    if (widget.localReceipt == null && widget.correctionState?.isApplied != true) return const SizedBox.shrink();

    return Padding(
      padding: const EdgeInsets.only(top: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          InkWell(
            borderRadius: BorderRadius.circular(999),
            onTap: () => setState(() => _expanded = !_expanded),
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
              decoration: BoxDecoration(color: const Color(0xFFE9DFD2), borderRadius: BorderRadius.circular(999)),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(Icons.check_circle_rounded, color: Color(0xFF38695E), size: 18),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text.rich(
                      TextSpan(
                        children: [
                          TextSpan(
                            text: context.l10n.updatedJustNow,
                            style: const TextStyle(fontWeight: FontWeight.w800),
                          ),
                          TextSpan(text: ' — ${context.l10n.seeWhatChanged} ›'),
                        ],
                      ),
                      style: const TextStyle(color: Color(0xFF23201C), fontSize: 15),
                    ),
                  ),
                ],
              ),
            ),
          ),
          if (_expanded) ...[
            const SizedBox(height: 10),
            Container(
              padding: const EdgeInsets.all(16),
              decoration: BoxDecoration(
                color: const Color(0xFFF2EBE1),
                borderRadius: BorderRadius.circular(20),
                border: Border.all(color: const Color(0xFFE9DFD2)),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(
                    context.l10n.whatChanged,
                    style: const TextStyle(
                      color: Color(0xFF6B655D),
                      fontSize: 12,
                      fontWeight: FontWeight.w700,
                      letterSpacing: 1.5,
                    ),
                  ),
                  const SizedBox(height: 10),
                  Text.rich(
                    TextSpan(
                      children: [
                        TextSpan(
                          text: _oldText,
                          style: const TextStyle(decoration: TextDecoration.lineThrough, color: Color(0xFF6B655D)),
                        ),
                        const TextSpan(text: ' → '),
                        TextSpan(
                          text: _newText,
                          style: const TextStyle(fontWeight: FontWeight.w800, color: Color(0xFF38695E)),
                        ),
                      ],
                    ),
                    style: const TextStyle(fontSize: 18, height: 1.35, color: Color(0xFF23201C)),
                  ),
                  if (widget.correctionState?.propagationApplied == true &&
                      (widget.correctionState?.propagatedPersonName ?? '').isNotEmpty) ...[
                    const SizedBox(height: 8),
                    Text(
                      context.l10n.alsoFixedOnPersonPage(widget.correctionState!.propagatedPersonName!),
                      style: const TextStyle(color: Color(0xFF6B655D), fontSize: 13),
                    ),
                  ],
                  const SizedBox(height: 14),
                  SizedBox(
                    height: 48,
                    child: OutlinedButton(
                      onPressed: _undoing || _correctionId == null
                          ? null
                          : () async {
                              setState(() => _undoing = true);
                              await widget.onUndo(_correctionId!);
                              if (mounted) setState(() => _undoing = false);
                            },
                      style: OutlinedButton.styleFrom(
                        foregroundColor: const Color(0xFF38695E),
                        side: const BorderSide(color: Color(0xFF38695E)),
                        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
                      ),
                      child: Text(_undoing ? context.l10n.undoingChange : context.l10n.undoThisChange),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class MemoryTalkHistoryRow extends StatelessWidget {
  final VoidCallback onTap;

  const MemoryTalkHistoryRow({super.key, required this.onTap});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(top: 12),
      child: InkWell(
        borderRadius: BorderRadius.circular(16),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 14, horizontal: 4),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  context.l10n.conversationAboutThis,
                  style: const TextStyle(color: Color(0xFF6B655D), fontSize: 16, fontWeight: FontWeight.w500),
                ),
              ),
              const Icon(Icons.chevron_right_rounded, color: Color(0xFF6B655D)),
            ],
          ),
        ),
      ),
    );
  }
}

class GetAppsWidgets extends StatelessWidget {
  final String searchQuery;
  final int currentResultIndex;
  const GetAppsWidgets({super.key, this.searchQuery = '', this.currentResultIndex = -1});

  @override
  Widget build(BuildContext context) {
    return Consumer<ConversationDetailProvider>(
      builder: (context, provider, child) {
        final summarizedApp = provider.getSummarizedApp();
        return Column(
          mainAxisSize: MainAxisSize.min,
          mainAxisAlignment: MainAxisAlignment.start,
          crossAxisAlignment: summarizedApp == null ? CrossAxisAlignment.center : CrossAxisAlignment.start,
          children: summarizedApp == null
              ? [child!]
              : [
                  // Show the summarized app
                  if (!provider.conversation.discarded) ...[
                    AppResultDetailWidget(
                      appResponse: summarizedApp,
                      app: provider.findAppById(summarizedApp.appId),
                      conversation: provider.conversation,
                      searchQuery: searchQuery,
                      currentResultIndex: currentResultIndex,
                    ),
                  ],
                  const SizedBox(height: 8),
                ],
        );
      },
      child: ListView(
        shrinkWrap: true,
        physics: const NeverScrollableScrollPhysics(),
        children: [
          const SizedBox(height: 32),
          Text(
            context.l10n.noSummaryForConversation,
            style: Theme.of(context).textTheme.titleLarge!.copyWith(fontSize: 20),
            textAlign: TextAlign.center,
          ),
          const SizedBox(height: 24),
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Container(
                decoration: BoxDecoration(
                  border: const GradientBoxBorder(
                    gradient: LinearGradient(
                      colors: [
                        Color.fromARGB(127, 208, 208, 208),
                        Color.fromARGB(127, 188, 99, 121),
                        Color.fromARGB(127, 86, 101, 182),
                        Color.fromARGB(127, 126, 190, 236),
                      ],
                    ),
                    width: 2,
                  ),
                  borderRadius: BorderRadius.circular(12),
                ),
                child: MaterialButton(
                  onPressed: () {
                    showModalBottomSheet(
                      context: context,
                      isScrollControlled: true,
                      backgroundColor: Colors.transparent,
                      builder: (context) => const SummarizedAppsBottomSheet(),
                    );
                  },
                  shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                  child: Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 0),
                    child: Text(
                      context.l10n.generateSummary,
                      style: const TextStyle(color: Colors.white, fontSize: 16),
                    ),
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 32),
        ],
      ),
    );
  }
}

class GetGeolocationWidgets extends StatelessWidget {
  const GetGeolocationWidgets({super.key});

  // Helper function to shorten address - show only neighborhood/area and city
  String _getShortAddress(BuildContext context, String? fullAddress) {
    if (fullAddress == null || fullAddress.isEmpty) {
      return context.l10n.unknownLocation;
    }

    // Split address by commas
    final parts = fullAddress.split(',').map((e) => e.trim()).toList();

    // If address has multiple parts, take the last 2-3 meaningful parts
    if (parts.length >= 3) {
      // Take neighborhood/area and city (skip street address and zip code)
      return '${parts[parts.length - 3]}, ${parts[parts.length - 2]}';
    } else if (parts.length == 2) {
      return '${parts[0]}, ${parts[1]}';
    }

    return fullAddress;
  }

  @override
  Widget build(BuildContext context) {
    return Selector<ConversationDetailProvider, Geolocation?>(
      selector: (context, provider) {
        if (provider.conversation.discarded) return null;
        return provider.conversation.geolocation;
      },
      builder: (context, geolocation, child) {
        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: geolocation == null
              ? []
              : [
                  const SizedBox(height: 12),
                  GestureDetector(
                    onTap: () async {
                      MapsUtil.launchMap(geolocation.latitude!, geolocation.longitude!);
                    },
                    child: ClipRRect(
                      borderRadius: BorderRadius.circular(16),
                      child: SizedBox(
                        height: 200,
                        child: Stack(
                          children: [
                            // Map Image
                            CachedNetworkImage(
                              imageBuilder: (context, imageProvider) {
                                return Container(
                                  height: 200,
                                  decoration: BoxDecoration(
                                    image: DecorationImage(image: imageProvider, fit: BoxFit.cover),
                                  ),
                                );
                              },
                              errorWidget: (context, url, error) {
                                return Container(
                                  height: 200,
                                  color: const Color(0xFF2A2A2A),
                                  child: Center(
                                    child: Column(
                                      mainAxisAlignment: MainAxisAlignment.center,
                                      children: [
                                        const Icon(Icons.location_off, size: 40, color: Colors.grey),
                                        const SizedBox(height: 8),
                                        Text(
                                          context.l10n.couldNotLoadMap,
                                          textAlign: TextAlign.center,
                                          style: const TextStyle(color: Colors.grey),
                                        ),
                                      ],
                                    ),
                                  ),
                                );
                              },
                              imageUrl: MapsUtil.getMapImageUrl(geolocation.latitude!, geolocation.longitude!),
                            ),
                            // Gradient blur overlay from bottom
                            Positioned(
                              bottom: 0,
                              left: 0,
                              right: 0,
                              child: Container(
                                height: 80,
                                decoration: BoxDecoration(
                                  gradient: LinearGradient(
                                    begin: Alignment.bottomCenter,
                                    end: Alignment.topCenter,
                                    colors: [Colors.black.withValues(alpha: 0.6), Colors.black.withValues(alpha: 0.0)],
                                  ),
                                ),
                              ),
                            ),
                            // Location text at bottom left
                            Positioned(
                              bottom: 16,
                              left: 16,
                              right: 16,
                              child: Text(
                                _getShortAddress(context, geolocation.address?.decodeString),
                                style: const TextStyle(
                                  color: Colors.white,
                                  fontSize: 15,
                                  fontWeight: FontWeight.w500,
                                  shadows: [Shadow(offset: Offset(0, 1), blurRadius: 2, color: Colors.black)],
                                ),
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(height: 16),
                ],
        );
      },
    );
  }
}

///************************************************
///************ SETTINGS BOTTOM SHEET *************
///************************************************

class GetSheetTitle extends StatelessWidget {
  const GetSheetTitle({super.key});

  @override
  Widget build(BuildContext context) {
    return Consumer<ConversationDetailProvider>(
      builder: (context, provider, child) {
        return Column(
          children: [
            ListTile(
              title: Text(
                provider.conversation.discarded
                    ? context.l10n.discardedConversation
                    : provider.conversation.structured.title,
                style: Theme.of(context).textTheme.labelLarge,
              ),
              leading: const Icon(Icons.description),
              trailing: IconButton(
                icon: const Icon(Icons.cancel_outlined),
                onPressed: () {
                  Navigator.of(context).pop(true);
                },
              ),
            ),
            const SizedBox(height: 8),
          ],
        );
      },
    );
  }
}

class GetDevToolsOptions extends StatefulWidget {
  final ServerConversation conversation;

  const GetDevToolsOptions({super.key, required this.conversation});

  @override
  State<GetDevToolsOptions> createState() => _GetDevToolsOptionsState();
}

class _GetDevToolsOptionsState extends State<GetDevToolsOptions> {
  bool loadingAppIntegrationTest = false;

  void changeLoadingAppIntegrationTest(bool value) {
    setState(() {
      loadingAppIntegrationTest = value;
    });
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Card(
          shape: const RoundedRectangleBorder(borderRadius: BorderRadius.all(Radius.circular(8))),
          child: ListTile(
            title: Text(context.l10n.triggerConversationIntegration),
            leading: loadingAppIntegrationTest
                ? const SizedBox(
                    height: 24,
                    width: 24,
                    child: CircularProgressIndicator(valueColor: AlwaysStoppedAnimation<Color>(Colors.white)),
                  )
                : const Icon(Icons.send_to_mobile_outlined),
            onTap: () {
              changeLoadingAppIntegrationTest(true);
              if (SharedPreferencesUtil().webhookOnConversationCreated.isEmpty) {
                showDialog(
                  context: context,
                  builder: (c) => getDialog(
                    context,
                    () {
                      Navigator.pop(context);
                    },
                    () {
                      Navigator.pop(context);
                      routeToPage(context, const DeveloperSettingsPage());
                    },
                    context.l10n.webhookUrlNotSet,
                    context.l10n.setWebhookUrlInSettings,
                    okButtonText: context.l10n.settings,
                  ),
                );
                changeLoadingAppIntegrationTest(false);
                return;
              } else {
                webhookOnConversationCreatedCall(widget.conversation, returnRawBody: true).then((response) {
                  showDialog(
                    context: context,
                    builder: (c) => getDialog(
                      context,
                      () => Navigator.pop(context),
                      () => Navigator.pop(context),
                      context.l10n.result,
                      response,
                      okButtonText: context.l10n.ok,
                      singleButton: true,
                    ),
                  );
                  changeLoadingAppIntegrationTest(false);
                });
              }
            },
          ),
        ),
        Card(
          shape: const RoundedRectangleBorder(borderRadius: BorderRadius.all(Radius.circular(8))),
          child: ListTile(
            title: Text(context.l10n.testConversationPrompt),
            leading: const Icon(Icons.chat),
            trailing: const Icon(Icons.arrow_forward_ios, size: 20),
            onTap: () {
              routeToPage(context, TestPromptsPage(conversation: widget.conversation));
            },
          ),
        ),
        // widget.memory.postprocessing?.status == MemoryPostProcessingStatus.completed
        // widget.memory.postprocessing?.status != MemoryPostProcessingStatus.not_started
        //     ? Card(
        //         shape: const RoundedRectangleBorder(borderRadius: BorderRadius.all(Radius.circular(8))),
        //         child: ListTile(
        //           title: const Text('Compare Transcripts Models'),
        //           leading: const Icon(Icons.chat),
        //           trailing: const Icon(Icons.arrow_forward_ios, size: 20),
        //           onTap: () {
        //             routeToPage(context, CompareTranscriptsPage(memory: widget.memory));
        //           },
        //         ),
        //       )
        //     : const SizedBox.shrink(),
      ],
    );
  }
}

_copyContent(BuildContext context, String content) {
  Clipboard.setData(ClipboardData(text: content));
  ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.transcriptCopiedToClipboard)));
  HapticFeedback.lightImpact();
  Navigator.pop(context);
}

_getLoadingIndicator() {
  return const SizedBox(
    width: 24,
    height: 24,
    child: CircularProgressIndicator(valueColor: AlwaysStoppedAnimation<Color>(Colors.white)),
  );
}

class GetShareOptions extends StatefulWidget {
  final ServerConversation conversation;

  const GetShareOptions({super.key, required this.conversation});

  @override
  State<GetShareOptions> createState() => _GetShareOptionsState();
}

class _GetShareOptionsState extends State<GetShareOptions> {
  bool loadingShareConversationViaURL = false;
  bool loadingShareTranscript = false;
  bool loadingShareSummary = false;

  final GlobalKey _shareUrlKey = GlobalKey();
  final GlobalKey _shareTranscriptKey = GlobalKey();
  final GlobalKey _shareSummaryKey = GlobalKey();

  void changeLoadingShareConversationViaURL(bool value) {
    setState(() {
      loadingShareConversationViaURL = value;
    });
  }

  void changeLoadingShareTranscript(bool value) {
    setState(() {
      loadingShareTranscript = value;
    });
  }

  void changeLoadingShareSummary(bool value) {
    setState(() {
      loadingShareSummary = value;
    });
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Card(
          shape: const RoundedRectangleBorder(borderRadius: BorderRadius.all(Radius.circular(8))),
          child: ListTile(
            key: _shareUrlKey,
            title: Text(context.l10n.sendWebUrl),
            leading: loadingShareConversationViaURL ? _getLoadingIndicator() : const Icon(Icons.link),
            onTap: () async {
              if (loadingShareConversationViaURL) return;
              changeLoadingShareConversationViaURL(true);
              bool shared = await setConversationVisibility(widget.conversation.id);
              if (!shared) {
                ScaffoldMessenger.of(
                  context,
                ).showSnackBar(SnackBar(content: Text(context.l10n.conversationUrlCouldNotBeShared)));
                return;
              }
              String content =
                  '''https://ella-ai-care.com/conversations/${widget.conversation.id}'''.replaceAll('  ', '').trim();
              print(content);
              final RenderBox? box = _shareUrlKey.currentContext?.findRenderObject() as RenderBox?;
              if (box != null) {
                final Offset position = box.localToGlobal(Offset.zero);
                final Size size = box.size;
                await Share.share(
                  content,
                  sharePositionOrigin: Rect.fromLTWH(position.dx, position.dy, size.width, size.height),
                );
              } else {
                await Share.share(content);
              }
              changeLoadingShareConversationViaURL(false);
            },
          ),
        ),
        const SizedBox(height: 4),
        Card(
          shape: const RoundedRectangleBorder(borderRadius: BorderRadius.all(Radius.circular(8))),
          child: Column(
            children: [
              ListTile(
                key: _shareTranscriptKey,
                title: Text(context.l10n.sendTranscript),
                leading: loadingShareTranscript ? _getLoadingIndicator() : const Icon(Icons.description),
                onTap: () async {
                  if (loadingShareTranscript) return;
                  changeLoadingShareTranscript(true);
                  String content = '''
              ${widget.conversation.structured.title}

              ${widget.conversation.getTranscript(generate: true)}
              '''
                      .replaceAll('  ', '')
                      .trim();
                  // TODO: Deeplink that let people download the app.
                  final RenderBox? box = _shareTranscriptKey.currentContext?.findRenderObject() as RenderBox?;
                  if (box != null) {
                    final Offset position = box.localToGlobal(Offset.zero);
                    final Size size = box.size;
                    await Share.share(
                      content,
                      sharePositionOrigin: Rect.fromLTWH(position.dx, position.dy, size.width, size.height),
                    );
                  } else {
                    await Share.share(content);
                  }
                  changeLoadingShareTranscript(false);
                },
              ),
              widget.conversation.discarded
                  ? const SizedBox()
                  : ListTile(
                      key: _shareSummaryKey,
                      title: Text(context.l10n.sendSummary),
                      leading: loadingShareSummary ? _getLoadingIndicator() : const Icon(Icons.summarize),
                      onTap: () async {
                        if (loadingShareSummary) return;
                        changeLoadingShareSummary(true);
                        // Use app-generated summary if available, otherwise fall back to structured summary
                        String content = (widget.conversation.appResults.isNotEmpty &&
                                    widget.conversation.appResults[0].content.trim().isNotEmpty
                                ? widget.conversation.appResults[0].content.trim()
                                : widget.conversation.structured.toString())
                            .replaceAll('  ', '')
                            .trim();
                        final RenderBox? box = _shareSummaryKey.currentContext?.findRenderObject() as RenderBox?;
                        if (box != null) {
                          final Offset position = box.localToGlobal(Offset.zero);
                          final Size size = box.size;
                          await Share.share(
                            content,
                            sharePositionOrigin: Rect.fromLTWH(position.dx, position.dy, size.width, size.height),
                          );
                        } else {
                          await Share.share(content);
                        }
                        changeLoadingShareSummary(false);
                      },
                    ),
            ],
          ),
        ),
        const SizedBox(height: 4),
        Card(
          shape: const RoundedRectangleBorder(borderRadius: BorderRadius.all(Radius.circular(8))),
          child: Column(
            children: [
              ListTile(
                title: Text(context.l10n.copyTranscript),
                leading: const Icon(Icons.copy),
                onTap: () => _copyContent(context, widget.conversation.getTranscript(generate: true)),
              ),
              widget.conversation.discarded
                  ? const SizedBox()
                  : ListTile(
                      title: Text(context.l10n.copySummary),
                      leading: const Icon(Icons.file_copy),
                      onTap: () => _copyContent(
                        context,
                        // Use app-generated summary if available, otherwise fall back to structured summary
                        widget.conversation.appResults.isNotEmpty &&
                                widget.conversation.appResults[0].content.trim().isNotEmpty
                            ? widget.conversation.appResults[0].content.trim()
                            : widget.conversation.structured.toString(),
                      ),
                    ),
            ],
          ),
        ),
      ],
    );
  }
}
