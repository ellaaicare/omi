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
    final content = card == null ? _fallbackContent(context, state) : _TodayCardContent.fromCard(context, card);

    return EllaCardSurface(
      child: Semantics(
        key: const Key('today-card-semantics'),
        container: true,
        explicitChildNodes: true,
        child: Padding(
          padding: const EdgeInsets.all(EllaSizes.spacingM),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                content.eyebrow,
                key: const Key('today-card-eyebrow'),
                style: EllaTextStyles.eyebrow,
              ),
              const SizedBox(height: 8),
              Semantics(
                header: true,
                child: Text(
                  content.headline,
                  key: const Key('today-card-headline'),
                  style: Theme.of(context).textTheme.titleLarge?.copyWith(fontSize: 20, height: 1.25),
                ),
              ),
              const SizedBox(height: 6),
              Text(
                content.body,
                key: const Key('today-card-body'),
                style: EllaTextStyles.noteBody.copyWith(fontSize: 18, height: 1.35),
              ),
              if (content.provenance != null) ...[
                const SizedBox(height: 8),
                Row(
                  key: const Key('today-card-provenance'),
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Padding(
                      padding: EdgeInsets.only(top: 2),
                      child: Icon(
                        Icons.history_rounded,
                        size: 18,
                        color: EllaColors.inkSoft,
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        content.provenance!,
                        style: EllaTextStyles.caption,
                      ),
                    ),
                  ],
                ),
              ],
              if (state.isCached) ...[
                const SizedBox(height: 10),
                Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Padding(
                      padding: EdgeInsets.only(top: 2),
                      child: Icon(
                        Icons.cloud_off_outlined,
                        size: 18,
                        color: EllaColors.inkSoft,
                      ),
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
                    child: CircularProgressIndicator(
                      strokeWidth: 2,
                      color: EllaColors.tealDeep,
                    ),
                  ),
                ),
              ],
              if (card != null) ...[
                const SizedBox(height: 12),
                _TodayCardActions(
                  isReading: isReading,
                  onTalk: onTalk,
                  onReadAloud: onReadAloud,
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }

  _TodayCardContent _fallbackContent(
    BuildContext context,
    TodayCardViewState state,
  ) =>
      switch (state.status) {
        TodayCardStatus.newUser => _TodayCardContent(
            eyebrow: context.l10n.todayCardPreparingEyebrow,
            headline: context.l10n.todayCardNewUserHeadline,
            body: context.l10n.todayCardNewUserBody,
          ),
        TodayCardStatus.degraded => state.errorCode == 'no_safe_source'
            ? _TodayCardContent(
                eyebrow: context.l10n.todayCardPreparingEyebrow,
                headline: context.l10n.todayCardNoSafeSourceHeadline,
                body: context.l10n.todayCardNoSafeSourceBody,
              )
            : _TodayCardContent(
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

class _TodayCardActions extends StatelessWidget {
  const _TodayCardActions({
    required this.isReading,
    required this.onTalk,
    required this.onReadAloud,
  });

  final bool isReading;
  final VoidCallback? onTalk;
  final VoidCallback? onReadAloud;

  @override
  Widget build(BuildContext context) {
    final textScale = MediaQuery.textScalerOf(context).scale(16) / 16;
    return LayoutBuilder(
      builder: (context, constraints) {
        final shouldStack = textScale > 1.3 || constraints.maxWidth < 310;
        final talk = Semantics(
          button: true,
          label: context.l10n.memoryTalkAction,
          child: FilledButton.icon(
            key: const Key('today-card-talk'),
            onPressed: onTalk,
            icon: const Icon(Icons.graphic_eq_rounded),
            label: Text(context.l10n.memoryTalkAction),
            style: FilledButton.styleFrom(
              minimumSize: const Size(0, EllaSizes.minTouchTarget),
            ),
          ),
        );
        final readAloud = onReadAloud == null
            ? null
            : TextButton.icon(
                key: const Key('today-card-read-aloud'),
                onPressed: onReadAloud,
                icon: Icon(
                  isReading ? Icons.stop_circle_outlined : Icons.volume_up_outlined,
                ),
                label: Text(
                  isReading ? context.l10n.stopReading : context.l10n.readAloud,
                ),
                style: TextButton.styleFrom(
                  foregroundColor: EllaColors.tealDeep,
                  minimumSize: const Size(0, EllaSizes.minTouchTarget),
                ),
              );

        if (shouldStack) {
          return Column(
            key: const Key('today-card-actions-stacked'),
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              talk,
              if (readAloud != null) ...[const SizedBox(height: 4), readAloud],
            ],
          );
        }

        return Row(
          key: const Key('today-card-actions-row'),
          children: [
            Expanded(child: talk),
            if (readAloud != null) ...[
              const SizedBox(width: 8),
              Flexible(child: readAloud),
            ],
          ],
        );
      },
    );
  }
}

class _TodayCardContent {
  const _TodayCardContent({
    required this.eyebrow,
    required this.headline,
    required this.body,
    this.provenance,
  });

  factory _TodayCardContent.fromCard(BuildContext context, TodayCard card) {
    return _TodayCardContent(
      eyebrow: context.l10n.todayCardPreparingEyebrow,
      headline: card.headline,
      body: card.body,
      provenance: card.kind != TodayCardKind.welcome && card.sourceRefs.isNotEmpty
          ? context.l10n.todayCardProvenanceRecentMemory
          : null,
    );
  }

  final String eyebrow;
  final String headline;
  final String body;
  final String? provenance;
}
