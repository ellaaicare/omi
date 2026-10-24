import 'package:flutter/material.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/services/summary_read_freshness.dart';
import 'package:omi/ella/ella_theme.dart';

// Ella's primary color is a legacy screen background, not action ink.
// Keep read/retry controls explicit in every theme and while disabled.
ButtonStyle summaryReadButtonStyle(BuildContext context) {
  final palette = EllaPalette.of(context);
  return FilledButton.styleFrom(
    foregroundColor: palette.ink,
    backgroundColor: palette.card,
    disabledForegroundColor: palette.inkSoft,
    disabledBackgroundColor: palette.card,
    minimumSize: const Size(48, 48),
    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
    side: BorderSide(color: palette.inkSoft),
  );
}

class SummaryRefreshNotice extends StatelessWidget {
  const SummaryRefreshNotice({
    super.key,
    required this.observation,
    this.conversation,
    this.onRefresh,
    this.list = false,
  });
  final SummaryReadObservation observation;
  final ServerConversation? conversation;
  final bool list;
  final VoidCallback? onRefresh;

  @override
  Widget build(BuildContext context) {
    final row = conversation;
    final state = row == null ? observation : observation.forConversation(row);
    final time = state.checkedAt;
    final subject = list ? 'Recent summaries' : 'Summary';
    final message = switch (state.phase) {
      SummaryReadPhase.unchecked =>
        list ? 'Recent summaries have not been checked.' : 'Saved preview · latest not checked.',
      SummaryReadPhase.checking => list ? 'Checking recent summaries…' : 'Saved preview · checking latest…',
      SummaryReadPhase.stale =>
        list ? 'Latest check unavailable. Showing saved summaries.' : 'Saved preview · latest check unavailable.',
      SummaryReadPhase.checked => time == null
          ? '$subject checked during this read.'
          : '$subject checked at ${MaterialLocalizations.of(context).formatTimeOfDay(TimeOfDay.fromDateTime(time.toLocal()))}.',
    };
    final processing = row == null ? null : explicitSummaryProcessingMessage(row);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(message, key: const Key('summary-refresh-status'), softWrap: true),
          if (processing != null)
            Text(
              state.phase == SummaryReadPhase.checked ? processing : 'Previously loaded status: $processing',
              key: const Key('summary-processing-status'),
              softWrap: true,
            ),
          if (onRefresh != null)
            FilledButton(
              key: const Key('summary-refresh-action'),
              style: summaryReadButtonStyle(context),
              onPressed: state.phase == SummaryReadPhase.checking ? null : onRefresh,
              child: Text(
                state.phase == SummaryReadPhase.checking ? 'Checking summary…' : 'Refresh summary',
                textAlign: TextAlign.center,
                softWrap: true,
              ),
            ),
        ],
      ),
    );
  }
}
