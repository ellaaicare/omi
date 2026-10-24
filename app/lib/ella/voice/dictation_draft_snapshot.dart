import 'package:flutter/services.dart';

import 'package:omi/ella/services/ella_account_commit_barrier.dart';

/// Editor-only authority: never authorizes capture, transcription or sending.
class DictationDraftSnapshot {
  const DictationDraftSnapshot({required this.value, required this.lease, required this.epoch});

  final TextEditingValue value;
  final EllaAccountCommitLease lease;
  final int epoch;

  /// The caller assigns the returned value synchronously, without another await,
  /// and does not send it automatically. A null result leaves the editor alone.
  TextEditingValue? insert({
    required String transcript,
    required TextEditingValue currentValue,
    required EllaAccountCommitLease? currentLease,
    required int currentEpoch,
  }) {
    if (!identical(lease, currentLease) || !lease.isCurrent || epoch != currentEpoch || value != currentValue) {
      return null;
    }
    final words = transcript.trim();
    if (words.isEmpty ||
        (value.composing.isValid && !value.composing.isCollapsed) ||
        (value.selection.isValid && !value.selection.isCollapsed)) {
      return null;
    }
    // Selected text and composing text are protected; only a caret or append is supported.
    final offset = value.selection.isValid ? value.selection.end : value.text.length;
    if (offset < 0 || offset > value.text.length) return null;
    final before = value.text.substring(0, offset);
    final after = value.text.substring(offset);
    final separator = RegExp(r'\s');
    final prefix = before.isNotEmpty && !separator.hasMatch(before.substring(before.length - 1)) ? ' ' : '';
    final suffix = after.isNotEmpty && !separator.hasMatch(after.substring(0, 1)) ? ' ' : '';
    final inserted = '$prefix$words$suffix';
    return TextEditingValue(
      text: '$before$inserted$after',
      selection: TextSelection.collapsed(offset: offset + inserted.length),
    );
  }
}
