import 'package:flutter/material.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/l10n_extensions.dart';

class MemoryCorrectionReceiptChip extends StatelessWidget {
  const MemoryCorrectionReceiptChip({required this.receipt, required this.onReview, super.key});

  final ConversationCorrectionReceipt receipt;
  final VoidCallback onReview;

  @override
  Widget build(BuildContext context) {
    final (icon, label) = switch (receipt) {
      ConversationCorrectionReceipt(isPending: true) => (Icons.sync_rounded, context.l10n.memoryCorrectionPending),
      ConversationCorrectionReceipt(isApplied: true) => (
          Icons.check_circle_outline_rounded,
          context.l10n.memoryCorrectionApplied,
        ),
      ConversationCorrectionReceipt(isUndone: true) => (Icons.undo_rounded, context.l10n.memoryCorrectionUndone),
      _ => (Icons.error_outline_rounded, context.l10n.memoryCorrectionFailed),
    };

    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 24),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      decoration: BoxDecoration(
        color: EllaColors.card,
        borderRadius: BorderRadius.circular(EllaSizes.radiusCircular),
        border: Border.all(color: EllaColors.cardDeep),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (receipt.isPending)
            const SizedBox(
              width: 18,
              height: 18,
              child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.primary),
            )
          else
            Icon(icon, size: 19, color: EllaColors.primary),
          const SizedBox(width: 8),
          Flexible(
            child: Text(label, style: EllaTextStyles.caption.copyWith(fontWeight: FontWeight.w600)),
          ),
          if (receipt.isApplied) ...[
            const SizedBox(width: 8),
            TextButton(onPressed: onReview, child: Text(context.l10n.memoryCorrectionReview)),
          ],
        ],
      ),
    );
  }
}

Future<void> showMemoryCorrectionReceiptSheet(
  BuildContext context, {
  required ConversationCorrectionReceipt receipt,
  required Future<ConversationCorrectionReceipt?> Function() onUndo,
}) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    backgroundColor: Colors.transparent,
    builder: (_) => _MemoryCorrectionReceiptSheet(receipt: receipt, onUndo: onUndo),
  );
}

class _MemoryCorrectionReceiptSheet extends StatefulWidget {
  const _MemoryCorrectionReceiptSheet({required this.receipt, required this.onUndo});

  final ConversationCorrectionReceipt receipt;
  final Future<ConversationCorrectionReceipt?> Function() onUndo;

  @override
  State<_MemoryCorrectionReceiptSheet> createState() => _MemoryCorrectionReceiptSheetState();
}

class _MemoryCorrectionReceiptSheetState extends State<_MemoryCorrectionReceiptSheet> {
  late ConversationCorrectionReceipt _receipt = widget.receipt;
  bool _undoing = false;

  Future<void> _undo() async {
    if (_undoing) return;
    setState(() => _undoing = true);
    final updated = await widget.onUndo();
    if (!mounted) return;
    setState(() {
      _undoing = false;
      if (updated != null) _receipt = updated;
    });
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: const BoxDecoration(
        color: EllaColors.bgPrimary,
        borderRadius: BorderRadius.vertical(top: Radius.circular(28)),
      ),
      padding: const EdgeInsets.fromLTRB(20, 16, 20, 20),
      child: SafeArea(
        top: false,
        child: SingleChildScrollView(
          child: Column(
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
              Text(context.l10n.memoryCorrectionReviewTitle, style: EllaTextStyles.display),
              const SizedBox(height: 18),
              _SummaryBlock(label: context.l10n.memoryCorrectionBefore, summary: _receipt.before),
              const SizedBox(height: 14),
              _SummaryBlock(label: context.l10n.memoryCorrectionAfter, summary: _receipt.after),
              if (_receipt.isApplied) ...[
                const SizedBox(height: 20),
                SizedBox(
                  width: double.infinity,
                  child: OutlinedButton.icon(
                    onPressed: _undoing ? null : _undo,
                    icon: _undoing
                        ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                        : const Icon(Icons.undo_rounded),
                    label: Text(context.l10n.memoryCorrectionUndo),
                  ),
                ),
              ],
              if (_receipt.isUndone) ...[
                const SizedBox(height: 18),
                Text(
                  context.l10n.memoryCorrectionUndone,
                  style: EllaTextStyles.body.copyWith(fontWeight: FontWeight.w600),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

class _SummaryBlock extends StatelessWidget {
  const _SummaryBlock({required this.label, required this.summary});

  final String label;
  final ConversationCorrectionSummary summary;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: EllaColors.card,
        borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
        border: Border.all(color: EllaColors.cardDeep),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(label, style: EllaTextStyles.caption.copyWith(fontWeight: FontWeight.w700)),
          if (summary.title.isNotEmpty) ...[
            const SizedBox(height: 8),
            Text(summary.title, style: EllaTextStyles.body.copyWith(fontWeight: FontWeight.w700)),
          ],
          if (summary.overview.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(summary.overview, style: EllaTextStyles.secondary),
          ],
        ],
      ),
    );
  }
}
