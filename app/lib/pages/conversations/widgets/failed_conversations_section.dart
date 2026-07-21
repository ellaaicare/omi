import 'package:flutter/material.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/pages/conversation_detail/page.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/other/temp.dart';

typedef RetryFailedConversation = Future<bool> Function(String conversationId, {String? correctionText});

class FailedConversationsSection extends StatelessWidget {
  final List<ServerConversation> conversations;
  final bool Function(String conversationId) isRetrying;
  final RetryFailedConversation onRetry;

  const FailedConversationsSection({
    super.key,
    required this.conversations,
    required this.isRetrying,
    required this.onRetry,
  });

  @override
  Widget build(BuildContext context) {
    if (conversations.isEmpty) return const SizedBox.shrink();

    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.only(left: 8, bottom: 8),
            child: Text(
              context.l10n.conversationNeedsProcessing,
              style: Theme.of(
                context,
              ).textTheme.titleMedium?.copyWith(color: EllaColors.textPrimary, fontWeight: FontWeight.w700),
            ),
          ),
          ...conversations.map(
            (conversation) => _FailedConversationCard(
              conversation: conversation,
              isRetrying: isRetrying(conversation.id),
              onRetry: onRetry,
            ),
          ),
        ],
      ),
    );
  }
}

class _FailedConversationCard extends StatelessWidget {
  final ServerConversation conversation;
  final bool isRetrying;
  final RetryFailedConversation onRetry;

  const _FailedConversationCard({required this.conversation, required this.isRetrying, required this.onRetry});

  @override
  Widget build(BuildContext context) {
    final recordedAt = conversation.startedAt ?? conversation.createdAt;
    final transcript = conversation.getTranscript(maxCount: 180).replaceAll(RegExp(r'\s+'), ' ').trim();

    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Material(
        color: EllaColors.bgSecondary,
        borderRadius: BorderRadius.circular(20),
        child: InkWell(
          borderRadius: BorderRadius.circular(20),
          onTap: () {
            Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => ConversationDetailPage(conversation: conversation, initialTabIndex: 0)),
            );
          },
          child: Container(
            padding: const EdgeInsets.all(16),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(20),
              border: Border.all(color: EllaColors.warning.withValues(alpha: 0.45)),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Container(
                      width: 36,
                      height: 36,
                      decoration: BoxDecoration(
                        color: EllaColors.warning.withValues(alpha: 0.14),
                        borderRadius: BorderRadius.circular(12),
                      ),
                      child: const Icon(Icons.history_toggle_off_rounded, color: EllaColors.warning, size: 20),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            context.l10n.conversationNeedsProcessing,
                            style: Theme.of(context).textTheme.titleSmall?.copyWith(
                                  color: EllaColors.textPrimary,
                                  fontWeight: FontWeight.w700,
                                ),
                          ),
                          const SizedBox(height: 2),
                          Text(
                            dateTimeFormat('MMM d, h:mm a', recordedAt),
                            style: Theme.of(context).textTheme.bodySmall?.copyWith(color: EllaColors.textTertiary),
                          ),
                        ],
                      ),
                    ),
                    FilledButton.tonal(
                      key: ValueKey('retry-conversation-${conversation.id}'),
                      onPressed: isRetrying
                          ? null
                          : () {
                              showModalBottomSheet<void>(
                                context: context,
                                isScrollControlled: true,
                                backgroundColor: Colors.transparent,
                                builder: (_) => _RetryWithEllaSheet(conversation: conversation, onRetry: onRetry),
                              );
                            },
                      style: FilledButton.styleFrom(
                        foregroundColor: EllaColors.textPrimary,
                        backgroundColor: EllaColors.bgTertiary,
                      ),
                      child: Text(isRetrying ? context.l10n.processing : context.l10n.retryWithElla),
                    ),
                  ],
                ),
                const SizedBox(height: 12),
                Text(
                  context.l10n.conversationNeedsProcessingDescription,
                  style: Theme.of(context).textTheme.bodyMedium?.copyWith(color: EllaColors.textSecondary),
                ),
                if (transcript.isNotEmpty) ...[
                  const SizedBox(height: 10),
                  Text(
                    transcript,
                    maxLines: 3,
                    overflow: TextOverflow.ellipsis,
                    style: Theme.of(
                      context,
                    ).textTheme.bodyMedium?.copyWith(color: EllaColors.textPrimary, height: 1.35),
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _RetryWithEllaSheet extends StatefulWidget {
  final ServerConversation conversation;
  final RetryFailedConversation onRetry;

  const _RetryWithEllaSheet({required this.conversation, required this.onRetry});

  @override
  State<_RetryWithEllaSheet> createState() => _RetryWithEllaSheetState();
}

class _RetryWithEllaSheetState extends State<_RetryWithEllaSheet> {
  final TextEditingController _contextController = TextEditingController();
  bool _isSubmitting = false;

  @override
  void dispose() {
    _contextController.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (_isSubmitting) return;

    setState(() => _isSubmitting = true);
    final navigator = Navigator.of(context);
    final messenger = ScaffoldMessenger.of(context);
    final correctionText = _contextController.text.trim();
    final restarted = await widget.onRetry(
      widget.conversation.id,
      correctionText: correctionText.isEmpty ? null : correctionText,
    );

    if (!mounted) return;
    setState(() => _isSubmitting = false);
    if (restarted) {
      navigator.pop();
    } else {
      messenger.showSnackBar(SnackBar(content: Text(context.l10n.somethingWentWrong)));
    }
  }

  @override
  Widget build(BuildContext context) {
    final bottomInset = MediaQuery.viewInsetsOf(context).bottom;

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
                  decoration: BoxDecoration(
                    color: EllaColors.bgTertiary,
                    borderRadius: BorderRadius.circular(999),
                  ),
                ),
              ),
              const SizedBox(height: 20),
              Text(
                context.l10n.retryWithElla,
                style: Theme.of(context).textTheme.headlineSmall?.copyWith(
                      color: EllaColors.textPrimary,
                      fontWeight: FontWeight.w700,
                    ),
              ),
              const SizedBox(height: 8),
              Text(
                context.l10n.retryWithEllaDescription,
                style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                      color: EllaColors.textSecondary,
                      height: 1.35,
                    ),
              ),
              const SizedBox(height: 16),
              TextField(
                key: ValueKey('retry-context-${widget.conversation.id}'),
                controller: _contextController,
                minLines: 3,
                maxLines: 6,
                textInputAction: TextInputAction.newline,
                style: const TextStyle(color: EllaColors.textPrimary),
                decoration: InputDecoration(
                  labelText: context.l10n.retryWithEllaContextLabel,
                  hintText: context.l10n.retryWithEllaContextHint,
                  labelStyle: const TextStyle(color: EllaColors.textSecondary),
                  hintStyle: const TextStyle(color: EllaColors.textTertiary),
                  filled: true,
                  fillColor: EllaColors.bgSecondary,
                  border: OutlineInputBorder(borderRadius: BorderRadius.circular(18)),
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
                  key: ValueKey('submit-retry-${widget.conversation.id}'),
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
                      : Text(context.l10n.retryNow),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
