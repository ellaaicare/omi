import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/v2v_client.dart';
import 'package:omi/utils/l10n_extensions.dart';

enum V2VFailureChoice { retry, useElevenLabs, stop }

String localizedV2VProviderName(BuildContext context, String provider) {
  return switch (V2VClient.normalizeProvider(provider)) {
    'grok-voice' => context.l10n.voiceProviderGrokNative,
    'gemini-native-live' => context.l10n.voiceProviderGeminiNative,
    'openai-native-realtime' => context.l10n.voiceProviderOpenAiNative,
    'openclaw-direct' => context.l10n.voiceProviderOpenClawDirect,
    _ => V2VClient.providerDisplayName(provider),
  };
}

class V2VFallbackDialog extends StatelessWidget {
  const V2VFallbackDialog({required this.receipt, this.allowStandardFallback = true, super.key});

  final V2VConnectionReceipt receipt;
  final bool allowStandardFallback;

  @override
  Widget build(BuildContext context) {
    final palette = EllaPalette.of(context);
    final canRetry = !receipt.isPermanentScopedFailure;
    return AlertDialog(
      scrollable: true,
      backgroundColor: palette.card,
      surfaceTintColor: palette.card,
      elevation: 0,
      shadowColor: Colors.transparent,
      title: Text(
        allowStandardFallback
            ? context.l10n.ellaVoiceConnectionUnavailableTitle
            : context.l10n.scopedTalkUnavailableTitle,
        style:
            TextStyle(fontFamily: EllaTextStyles.uiFont, fontSize: 22, fontWeight: FontWeight.w600, color: palette.ink),
      ),
      content: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text(
            allowStandardFallback
                ? canRetry
                    ? context.l10n.ellaVoiceConnectionUnavailableBody
                    : context.l10n.ellaVoiceConnectionCloseBody
                : canRetry
                    ? context.l10n.scopedTalkRetryableBody
                    : context.l10n.scopedTalkUnavailableBody,
            style: EllaTextStyles.body.copyWith(color: palette.ink)),
        ExpansionTile(
          key: const Key('v2v-failure-details'),
          tilePadding: EdgeInsets.zero,
          title: Text(context.l10n.irisStatusDetails, style: TextStyle(color: palette.ink, fontSize: 17)),
          iconColor: palette.ink,
          collapsedIconColor: palette.ink,
          children: [
            SelectableText('${receipt.stage.name} (${receipt.safeDetail})',
                style: TextStyle(color: palette.inkSoft, fontFamily: EllaTextStyles.uiFont, fontSize: 16))
          ],
        ),
      ]),
      actions: [
        OutlinedButton.icon(
          key: const ValueKey('v2v-failure-cancel'),
          onPressed: () => Navigator.of(context).pop(V2VFailureChoice.stop),
          style: OutlinedButton.styleFrom(
            foregroundColor: palette.ink,
            side: BorderSide(color: palette.ink),
            minimumSize: const Size(0, EllaSizes.minTouchTarget),
          ),
          icon: const Icon(Icons.close),
          label: Text(context.l10n.close),
        ),
        if (canRetry)
          FilledButton(
            key: const ValueKey('v2v-failure-retry'),
            onPressed: () => Navigator.of(context).pop(V2VFailureChoice.retry),
            style: FilledButton.styleFrom(
              backgroundColor: palette.ink,
              foregroundColor: palette.paper,
              minimumSize: const Size(0, EllaSizes.minTouchTarget),
            ),
            child: Text(context.l10n.retry),
          ),
      ],
    );
  }
}

Future<V2VFailureChoice> showV2VFallbackDialog(
  BuildContext context,
  V2VConnectionReceipt receipt, {
  bool allowStandardFallback = true,
}) async {
  return await showDialog<V2VFailureChoice>(
        context: context,
        barrierDismissible: true,
        builder: (_) => V2VFallbackDialog(receipt: receipt, allowStandardFallback: allowStandardFallback),
      ) ??
      V2VFailureChoice.stop;
}
