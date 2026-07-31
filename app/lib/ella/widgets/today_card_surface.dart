import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/today_card.dart';
import 'package:omi/utils/l10n_extensions.dart';

class TodayCardSurface extends StatelessWidget {
  const TodayCardSurface({
    super.key,
    required this.state,
    required this.isReading,
    required this.onTalk,
    required this.onReadAloud,
  });

  final TodayCardViewState state;
  final bool isReading;
  final VoidCallback? onTalk;
  final VoidCallback? onReadAloud;

  @override
  Widget build(BuildContext context) {
    final card = state.card;
    final content = card == null ? _fallbackContent(context, state.status) : _TodayCardContent.fromCard(card);

    return EllaCardSurface(
      child: Semantics(
        key: const Key('today-card-semantics'),
        container: true,
        explicitChildNodes: true,
        child: Padding(
          padding: const EdgeInsets.all(EllaSizes.notePadding),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(content.eyebrow, key: const Key('today-card-eyebrow'), style: EllaTextStyles.eyebrow),
              const SizedBox(height: 12),
              Semantics(
                header: true,
                child: Text(
                  content.headline,
                  key: const Key('today-card-headline'),
                  style: Theme.of(context).textTheme.titleLarge,
                ),
              ),
              const SizedBox(height: 10),
              Text(content.body, key: const Key('today-card-body'), style: EllaTextStyles.noteBody),
              if (state.isCached) ...[
                const SizedBox(height: 14),
                Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Padding(
                      padding: EdgeInsets.only(top: 4),
                      child: Icon(Icons.history_rounded, size: 18, color: EllaColors.inkSoft),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        context.l10n.todayCardSavedStatus,
                        key: const Key('today-card-cached-status'),
                        style: EllaTextStyles.secondary,
                      ),
                    ),
                  ],
                ),
              ],
              if (state.isLoading && card == null) ...[
                const SizedBox(height: 18),
                const Align(
                  alignment: Alignment.centerLeft,
                  child: SizedBox(
                    width: 20,
                    height: 20,
                    child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.tealDeep),
                  ),
                ),
              ],
              if (card != null) ...[
                const SizedBox(height: 22),
                Semantics(
                  button: true,
                  label: context.l10n.memoryTalkAction,
                  child: SizedBox(
                    width: double.infinity,
                    child: FilledButton.icon(
                      key: const Key('today-card-talk'),
                      onPressed: onTalk,
                      icon: const Icon(Icons.graphic_eq_rounded),
                      label: Text(context.l10n.memoryTalkAction),
                    ),
                  ),
                ),
                if (onReadAloud != null) ...[
                  const SizedBox(height: 6),
                  TextButton.icon(
                    key: const Key('today-card-read-aloud'),
                    onPressed: onReadAloud,
                    icon: Icon(isReading ? Icons.stop_circle_outlined : Icons.volume_up_outlined),
                    label: Text(isReading ? context.l10n.stopReading : context.l10n.readAloud),
                    style: TextButton.styleFrom(
                      foregroundColor: EllaColors.tealDeep,
                      minimumSize: const Size.fromHeight(EllaSizes.minTouchTarget),
                    ),
                  ),
                ],
              ],
            ],
          ),
        ),
      ),
    );
  }

  _TodayCardContent _fallbackContent(BuildContext context, TodayCardStatus status) => switch (status) {
        TodayCardStatus.newUser => _TodayCardContent(
            eyebrow: context.l10n.todayCardPreparingEyebrow,
            headline: context.l10n.todayCardNewUserHeadline,
            body: context.l10n.todayCardNewUserBody,
          ),
        TodayCardStatus.degraded => _TodayCardContent(
            eyebrow: context.l10n.todayCardPreparingEyebrow,
            headline: context.l10n.todayCardDegradedHeadline,
            body: context.l10n.todayCardDegradedBody,
          ),
        TodayCardStatus.ready || TodayCardStatus.preparing => _TodayCardContent(
            eyebrow: context.l10n.todayCardPreparingEyebrow,
            headline: context.l10n.todayCardPreparingHeadline,
            body: context.l10n.todayCardPreparingBody,
          ),
      };
}

class _TodayCardContent {
  const _TodayCardContent({required this.eyebrow, required this.headline, required this.body});

  factory _TodayCardContent.fromCard(TodayCard card) => _TodayCardContent(
        eyebrow: card.eyebrow,
        headline: card.headline,
        body: card.body,
      );

  final String eyebrow;
  final String headline;
  final String body;
}
