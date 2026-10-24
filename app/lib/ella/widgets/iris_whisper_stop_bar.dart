import 'dart:async';
import 'package:flutter/material.dart';
import 'package:omi/ella/widgets/quiet_memory_preview.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/whisper_delivery_runtime.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// Visible only from a current native playback fact, never configured mode.
class IrisWhisperStopBar extends StatefulWidget {
  const IrisWhisperStopBar({super.key, this.runtime, this.hideWhenMessageVisible = false});
  final bool hideWhenMessageVisible;
  final WhisperDeliveryRuntime? runtime;
  @override
  State<IrisWhisperStopBar> createState() => _IrisWhisperStopBarState();
}

class _IrisWhisperStopBarState extends State<IrisWhisperStopBar> {
  bool _busy = false;
  Timer? _retirementTimer;
  bool _visible = false;
  @override
  void initState() {
    super.initState();
    _retirementTimer = Timer.periodic(const Duration(milliseconds: 250), (_) {
      if (_visible && !_runtime.playing && mounted) setState(() => _visible = false);
    });
  }

  @override
  void dispose() {
    _retirementTimer?.cancel();
    super.dispose();
  }

  WhisperDeliveryRuntime get _runtime => widget.runtime ?? WhisperDeliveryRuntime.shared;
  @override
  Widget build(BuildContext context) {
    if (!whisperDeliveryProtocol1 && widget.runtime == null) return const SizedBox.shrink();
    return AnimatedBuilder(
        animation: _runtime,
        builder: (context, _) {
          _visible = _runtime.playing;
          if (!_visible || (widget.hideWhenMessageVisible && _runtime.message != null)) return const SizedBox.shrink();
          final palette = EllaPalette.of(context);
          return Semantics(
              label: context.l10n.irisWhisperStopAccessibility,
              child: Material(
                  key: const Key('iris-whisper-stop-bar'),
                  color: palette.bgSecondary,
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(minHeight: 56),
                    child: Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                        child: Row(children: [
                          Icon(Icons.graphic_eq_rounded, color: palette.ink, size: 20),
                          const SizedBox(width: 8),
                          Expanded(
                              child: Text(context.l10n.irisWhisperPlaying,
                                  style: EllaTypography.of(context)
                                      .body
                                      .copyWith(fontSize: 16, fontWeight: FontWeight.w700))),
                          const SizedBox(width: 8),
                          OutlinedButton(
                              key: const Key('iris-global-whisper-stop'),
                              style: OutlinedButton.styleFrom(
                                  foregroundColor: palette.ink,
                                  disabledForegroundColor: palette.ink,
                                  backgroundColor: palette.bgSecondary,
                                  side: BorderSide(color: palette.ink),
                                  minimumSize: const Size(88, 44),
                                  textStyle: const TextStyle(
                                      fontFamily: EllaTextStyles.uiFont, fontSize: 16, fontWeight: FontWeight.w800)),
                              onPressed: _busy
                                  ? null
                                  : () async {
                                      setState(() => _busy = true);
                                      try {
                                        await _runtime.stopOutput();
                                      } catch (_) {
                                        if (context.mounted)
                                          ScaffoldMessenger.maybeOf(context)?.showSnackBar(
                                              SnackBar(content: Text(context.l10n.irisWhisperOutputUnconfirmed)));
                                      } finally {
                                        if (mounted) setState(() => _busy = false);
                                      }
                                    },
                              child: Text(context.l10n.irisStop)),
                        ])),
                  )));
        });
  }
}

class IrisWhisperSheet extends StatelessWidget {
  const IrisWhisperSheet({super.key, required this.child});
  final Widget child;
  @override
  Widget build(BuildContext context) => Column(mainAxisSize: MainAxisSize.min, children: [
        const IrisWhisperStopBar(),
        Flexible(child: child),
      ]);
}

/// Actual admitted delivery text; absent person/time metadata is not invented.
class IrisWhisperMessage extends StatelessWidget {
  const IrisWhisperMessage({super.key, this.runtime, this.showStopControl = true});
  final bool showStopControl;
  final WhisperDeliveryRuntime? runtime;
  @override
  Widget build(BuildContext context) {
    if (!whisperDeliveryProtocol1 && runtime == null) return const SizedBox.shrink();
    final output = runtime ?? WhisperDeliveryRuntime.shared;
    return AnimatedBuilder(
        animation: output,
        builder: (context, _) {
          final message = output.message;
          if (message == null || message.trim().isEmpty) return const SizedBox.shrink();
          return QuietMemoryAuthorityFence(
              isCurrent: () => output.message != null,
              child: Container(
                  key: const Key('iris-admitted-whisper-message'),
                  margin: const EdgeInsets.fromLTRB(18, 8, 18, 8),
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                      color: EllaPalette.of(context).elevatedCard, borderRadius: BorderRadius.circular(16)),
                  child:
                      Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
                    Text(context.l10n.irisWhisperLabel,
                        style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w700)),
                    const SizedBox(height: 8),
                    SelectableText(message, style: EllaTypography.of(context).body),
                    if (showStopControl) IrisWhisperStopBar(runtime: runtime),
                  ])));
        });
  }
}
