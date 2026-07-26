import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/l10n_extensions.dart';

class VoiceModalScaffold extends StatefulWidget {
  const VoiceModalScaffold({
    required this.voiceActive,
    required this.onEnd,
    required this.child,
    required this.title,
    super.key,
  });

  final bool voiceActive;
  final Future<bool> Function() onEnd;
  final Widget child;
  final String title;

  @override
  State<VoiceModalScaffold> createState() => _VoiceModalScaffoldState();
}

class _VoiceModalScaffoldState extends State<VoiceModalScaffold> {
  bool _ending = false;

  Future<void> _close() async {
    if (_ending) return;
    if (widget.voiceActive) {
      setState(() => _ending = true);
      var ended = false;
      try {
        ended = await widget.onEnd();
      } catch (_) {
        ended = false;
      }
      if (!mounted) return;
      if (!ended) {
        setState(() => _ending = false);
        return;
      }
    }
    if (mounted) Navigator.of(context).pop();
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: !widget.voiceActive && !_ending,
      child: Scaffold(
        key: const ValueKey('voice-modal-root'),
        backgroundColor: EllaColors.bgPrimary,
        appBar: AppBar(
          automaticallyImplyLeading: false,
          backgroundColor: EllaColors.bgPrimary,
          elevation: 0,
          centerTitle: true,
          leadingWidth: widget.voiceActive ? 72 : 56,
          leading: widget.voiceActive
              ? TextButton(
                  key: const ValueKey('voice-modal-end'),
                  onPressed: _ending ? null : _close,
                  child: _ending
                      ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.primary),
                        )
                      : Text(
                          context.l10n.voiceModalEndAction,
                          style: const TextStyle(color: EllaColors.primary, fontWeight: FontWeight.w700),
                        ),
                )
              : IconButton(
                  key: const ValueKey('voice-modal-close'),
                  tooltip: context.l10n.close,
                  onPressed: _close,
                  icon: const Icon(Icons.close, color: EllaColors.textPrimary),
                ),
          title: Text(
            widget.title,
            style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w600, color: EllaColors.textPrimary),
          ),
        ),
        body: widget.child,
      ),
    );
  }
}
