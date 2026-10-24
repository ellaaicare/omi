import 'package:flutter/material.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/voice/foreground_dictation_host.dart';
import 'package:omi/utils/l10n_extensions.dart';

class ForegroundDictationSheet extends StatelessWidget {
  const ForegroundDictationSheet({super.key, required this.host});
  final ForegroundDictationHost host;
  @override
  Widget build(BuildContext context) => AnimatedBuilder(
        animation: host,
        builder: (context, _) {
          final l = context.l10n;
          final palette = EllaPalette.of(context);
          final actionStyle = TextButton.styleFrom(
            foregroundColor: palette.ink,
            disabledForegroundColor: palette.inkSoft,
            minimumSize: const Size(48, 48),
          );
          final status = switch (host.phase) {
            ForegroundDictationPhase.ready => l.ellaDictationLocalDraft,
            ForegroundDictationPhase.busy => l.ellaDictationBusy,
            ForegroundDictationPhase.starting => l.ellaDictationStarting,
            ForegroundDictationPhase.listening => l.ellaDictationListening,
            ForegroundDictationPhase.finishing => l.ellaDictationFinishing,
            ForegroundDictationPhase.permissionDenied => l.ellaDictationPermissionRequired,
            ForegroundDictationPhase.closed => l.ellaDictationFinished,
            ForegroundDictationPhase.failed =>
              host.hasHeldInput || host.hasHeldOutput ? l.ellaDictationCleanupPending : l.ellaDictationUnavailableBody,
            _ => l.ellaDictationUnavailableBody,
          };
          return AlertDialog(
            key: const Key('chat-dictation-sheet'),
            elevation: 0,
            shadowColor: Colors.transparent,
            backgroundColor: palette.card,
            surfaceTintColor: palette.card,
            scrollable: true,
            title: Text(l.ellaDictationTitle),
            content: Semantics(liveRegion: true, child: Text(status, key: const Key('chat-dictation-status'))),
            actions: [
              TextButton(
                key: const Key('chat-dictation-start'),
                style: actionStyle,
                onPressed: host.phase == ForegroundDictationPhase.ready ? () => host.start() : null,
                child: Text(l.ellaDictationStart),
              ),
              TextButton(
                key: const Key('chat-dictation-finish'),
                style: actionStyle,
                onPressed: host.phase == ForegroundDictationPhase.listening ? () => host.finish() : null,
                child: Text(l.ellaDictationFinish),
              ),
              TextButton(
                key: const Key('chat-dictation-cancel'),
                style: actionStyle,
                onPressed: () async {
                  try {
                    await host.retire();
                    if (context.mounted) Navigator.of(context).pop();
                  } catch (_) {/* Same owner remains available for exact cleanup retry. */}
                },
                child: Text(host.phase == ForegroundDictationPhase.closed ? l.close : l.cancel),
              ),
            ],
          );
        },
      );
}
