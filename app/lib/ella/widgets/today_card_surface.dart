import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/today_card.dart';
import 'package:omi/utils/l10n_extensions.dart';

class TodayCardSurface extends StatelessWidget {
  const TodayCardSurface({
    super.key,
    required this.state,
    required this.onReadMore,
    required this.onTalk,
    this.compact = false,
    this.surfaceColor = EllaColors.card,
  });

  final TodayCardViewState state;
  final VoidCallback? onReadMore;
  final VoidCallback? onTalk;
  final bool compact;
  final Color surfaceColor;

  static const double _botanicalOpacity = 0.18;

  @override
  Widget build(BuildContext context) {
    final card = state.card;
    final content = card == null ? _fallbackContent(context, state) : _TodayCardContent.fromCard(context, card);
    final showBotanical = !compact && card != null && _showsBotanical(card);
    final textScale = MediaQuery.textScalerOf(context).scale(16) / 16;
    final headlineLines = compact ? 2 : (card == null ? null : 2);
    final bodyLines = compact ? 2 : (card == null ? null : (textScale > 1.3 ? 2 : 4));

    return EllaCardSurface(
      borderRadius: 24,
      color: surfaceColor,
      child: Stack(
        children: [
          if (showBotanical)
            Positioned(
              right: -18,
              bottom: -22,
              width: 188,
              height: 176,
              child: ExcludeSemantics(
                child: Opacity(
                  key: const Key('today-card-botanical'),
                  opacity: _botanicalOpacity,
                  child: Image.asset(
                    'assets/images/ella-daily-note-botanical.png',
                    fit: BoxFit.cover,
                    alignment: Alignment.bottomRight,
                  ),
                ),
              ),
            ),
          Semantics(
            key: const Key('today-card-semantics'),
            container: true,
            explicitChildNodes: true,
            child: Padding(
              padding: EdgeInsets.all(compact ? 16 : EllaSizes.notePadding),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(content.eyebrow,
                      key: const Key('today-card-eyebrow'), style: EllaTypography.of(context).eyebrow),
                  SizedBox(height: compact ? 8 : 14),
                  Semantics(
                    header: true,
                    child: Text(
                      content.headline,
                      key: const Key('today-card-headline'),
                      maxLines: headlineLines,
                      overflow: headlineLines == null ? null : TextOverflow.ellipsis,
                      style: EllaTypography.of(context).noteBody.copyWith(fontSize: compact ? 20 : 26, height: 1.12),
                    ),
                  ),
                  SizedBox(height: compact ? 6 : 10),
                  Text(
                    content.body,
                    key: const Key('today-card-body'),
                    maxLines: bodyLines,
                    overflow: bodyLines == null ? null : TextOverflow.ellipsis,
                    style: EllaTypography.of(context).noteBody.copyWith(
                          fontSize: compact ? 14 : 19,
                          height: compact ? 1.3 : 1.35,
                          fontWeight: FontWeight.w400,
                        ),
                  ),
                  if (state.isCached) ...[
                    const SizedBox(height: 12),
                    _StatusLine(
                      key: const Key('today-card-cached-status'),
                      icon: Icons.cloud_off_outlined,
                      label: context.l10n.todayCardSavedStatus,
                    ),
                  ],
                  if (!compact && state.isLoading && card == null) ...[
                    const SizedBox(height: 20),
                    Align(
                      alignment: Alignment.centerLeft,
                      child: SizedBox(
                        width: 20,
                        height: 20,
                        child: CircularProgressIndicator(strokeWidth: 2, color: EllaPalette.of(context).tealDeep),
                      ),
                    ),
                  ],
                  if (card != null) ...[
                    SizedBox(height: compact ? 10 : 18),
                    Divider(height: 1, color: EllaPalette.of(context).cardDeep),
                    const SizedBox(height: 6),
                    _TodayCardFooter(provenance: content.provenance, onReadMore: onReadMore, onTalk: onTalk),
                  ],
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  bool _showsBotanical(TodayCard card) {
    final stableValue = card.id.codeUnits.fold<int>(card.version, (sum, value) => sum + value);
    return stableValue % 3 != 0;
  }

  _TodayCardContent _fallbackContent(BuildContext context, TodayCardViewState state) => switch (state.status) {
        TodayCardStatus.newUser => _TodayCardContent(
            eyebrow: context.l10n.todayCardPreparingEyebrow,
            headline: context.l10n.todayCardFirstNoteHeadline,
            body: context.l10n.todayCardFirstNoteBody,
          ),
        TodayCardStatus.degraded => state.errorCode == 'no_safe_source'
            ? _TodayCardContent(
                eyebrow: context.l10n.todayCardPreparingEyebrow,
                headline: context.l10n.todayCardNoSafeSourceHeadline,
                body: context.l10n.todayCardNoSafeSourceBody,
              )
            : state.errorCode == 'today_card_authority_unavailable'
                ? _TodayCardContent(
                    eyebrow: context.l10n.todayCardPreparingEyebrow,
                    headline: context.l10n.todayCardAuthorityUnavailableHeadline,
                    body: context.l10n.todayCardAuthorityUnavailableBody,
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

class _TodayCardFooter extends StatelessWidget {
  const _TodayCardFooter({required this.provenance, required this.onReadMore, required this.onTalk});

  final String? provenance;
  final VoidCallback? onReadMore;
  final VoidCallback? onTalk;

  @override
  Widget build(BuildContext context) {
    final textScale = MediaQuery.textScalerOf(context).scale(16) / 16;
    return LayoutBuilder(
      builder: (context, constraints) {
        final shouldStack = textScale > 1.3 || constraints.maxWidth < 300;
        final source = provenance == null
            ? const SizedBox.shrink()
            : _StatusLine(key: const Key('today-card-provenance'), icon: Icons.eco_outlined, label: provenance!);
        final readMore = TextButton(
          key: const Key('today-card-read-more'),
          onPressed: onReadMore,
          style: TextButton.styleFrom(
            foregroundColor: EllaPalette.of(context).tealDeep,
            minimumSize: const Size(0, EllaSizes.minTouchTarget),
            padding: const EdgeInsets.symmetric(horizontal: 8),
            textStyle: EllaTypography.of(context).secondary.copyWith(fontSize: 16, fontWeight: FontWeight.w700),
          ),
          child: Text(context.l10n.todayReadMore),
        );
        final talk = TextButton.icon(
          key: const Key('today-card-talk'),
          onPressed: onTalk,
          iconAlignment: IconAlignment.end,
          icon: const Icon(Icons.chevron_right_rounded, size: 20),
          label: Text(context.l10n.memoryTalkAction),
          style: TextButton.styleFrom(
            foregroundColor: EllaPalette.of(context).tealDeep,
            minimumSize: const Size(0, EllaSizes.minTouchTarget),
            padding: const EdgeInsets.symmetric(horizontal: 4),
            textStyle: EllaTypography.of(context).secondary.copyWith(fontSize: 16, fontWeight: FontWeight.w700),
          ),
        );

        if (shouldStack) {
          return Column(
            key: const Key('today-card-actions-stacked'),
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              source,
              Align(alignment: Alignment.centerRight, child: readMore),
              Align(alignment: Alignment.centerRight, child: talk),
            ],
          );
        }

        return Column(
          key: const Key('today-card-actions-row'),
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            source,
            Wrap(alignment: WrapAlignment.end, spacing: 4, runSpacing: 4, children: [readMore, talk]),
          ],
        );
      },
    );
  }
}

class _StatusLine extends StatelessWidget {
  const _StatusLine({super.key, required this.icon, required this.label});

  final IconData icon;
  final String label;

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.center,
      children: [
        Icon(icon, size: 16, color: EllaPalette.of(context).tealDeep),
        const SizedBox(width: 6),
        Expanded(child: Text(label, style: EllaTypography.of(context).caption.copyWith(fontSize: 12))),
      ],
    );
  }
}

class _TodayCardContent {
  const _TodayCardContent({required this.eyebrow, required this.headline, required this.body, this.provenance});

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
