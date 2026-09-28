import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:font_awesome_flutter/font_awesome_flutter.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/l10n_extensions.dart';

class CorrectSummaryButton extends StatelessWidget {
  final ServerConversation conversation;
  final String appSummary;

  const CorrectSummaryButton({
    super.key,
    required this.conversation,
    required this.appSummary,
  });

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(18),
        onTap: () {
          HapticFeedback.lightImpact();
          showModalBottomSheet(
            context: context,
            isScrollControlled: true,
            backgroundColor: Colors.transparent,
            builder: (context) => CorrectSummarySheet(
              conversation: conversation,
              appSummary: appSummary,
            ),
          );
        },
        child: Ink(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          decoration: BoxDecoration(
            color: EllaColors.bgSecondary,
            borderRadius: BorderRadius.circular(18),
            border: Border.all(color: EllaColors.bgTertiary),
          ),
          child: const Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              FaIcon(FontAwesomeIcons.penToSquare, size: 15, color: EllaColors.primary),
              SizedBox(width: 8),
              Text(
                'Correct Summary',
                style: TextStyle(
                  color: EllaColors.textPrimary,
                  fontSize: 14,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class CorrectSummarySheet extends StatefulWidget {
  final ServerConversation conversation;
  final String appSummary;

  const CorrectSummarySheet({
    super.key,
    required this.conversation,
    required this.appSummary,
  });

  @override
  State<CorrectSummarySheet> createState() => _CorrectSummarySheetState();
}

class _CorrectSummarySheetState extends State<CorrectSummarySheet> {
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
      messenger.showSnackBar(
        const SnackBar(content: Text('Correction submitted. Ella will recheck this summary.')),
      );
      navigator.pop();
    } else {
      HapticFeedback.lightImpact();
      messenger.showSnackBar(
        const SnackBar(content: Text('Could not submit correction yet. Please try again later.')),
      );
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
                  decoration: BoxDecoration(
                    color: EllaColors.bgTertiary,
                    borderRadius: BorderRadius.circular(999),
                  ),
                ),
              ),
              const SizedBox(height: 20),
              const Text(
                'Correct Summary',
                style: TextStyle(
                  color: EllaColors.textPrimary,
                  fontSize: 22,
                  fontWeight: FontWeight.w700,
                ),
              ),
              const SizedBox(height: 8),
              const Text(
                'Tell Ella what was wrong in your own words. The conversation ID and current summary are attached in the background.',
                style: TextStyle(
                  color: EllaColors.textSecondary,
                  fontSize: 15,
                  height: 1.35,
                ),
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
                  hintText: context.l10n.correctionSheetHintExample,
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
                      : const Text('Submit Correction'),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
