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
    final providerName = localizedV2VProviderName(context, receipt.provider);
    final canRetry = allowStandardFallback || !receipt.isPermanentScopedFailure;
    return AlertDialog(
      backgroundColor: EllaColors.card,
      title: Text(
        allowStandardFallback
            ? context.l10n.voiceV2vUnavailableTitle(providerName)
            : context.l10n.scopedTalkUnavailableTitle,
        style: EllaTextStyles.display.copyWith(fontSize: 22),
      ),
      content: Text(
        allowStandardFallback
            ? context.l10n.voiceV2vUnavailableBody(receipt.stage.name, receipt.safeDetail)
            : canRetry
                ? context.l10n.scopedTalkRetryableBody
                : context.l10n.scopedTalkUnavailableBody,
        style: EllaTextStyles.body,
      ),
      actions: [
        OutlinedButton.icon(
          key: const ValueKey('v2v-failure-cancel'),
          onPressed: () => Navigator.of(context).pop(V2VFailureChoice.stop),
          icon: const Icon(Icons.close),
          label: Text(allowStandardFallback ? context.l10n.cancel : context.l10n.close),
        ),
        if (allowStandardFallback)
          TextButton(
            key: const ValueKey('v2v-failure-elevenlabs'),
            onPressed: () => Navigator.of(context).pop(V2VFailureChoice.useElevenLabs),
            child: Text(context.l10n.voiceUseElevenLabs),
          ),
        if (canRetry)
          FilledButton(
            key: const ValueKey('v2v-failure-retry'),
            onPressed: () => Navigator.of(context).pop(V2VFailureChoice.retry),
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
