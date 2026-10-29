import 'dart:async';

import 'package:clock/clock.dart';
import 'package:flutter/material.dart';

import 'package:omi/dev/base_timed_capture.dart';
import 'package:omi/ui/ui.dart';

/// Developer Settings row for the DEV-ONLY timed capture test. Built only when
/// kBaseDevTimedCapture is true; English-only because it never ships.
class BaseTimedCaptureSection extends StatefulWidget {
  const BaseTimedCaptureSection({super.key, required this.controller});

  final BaseTimedCapture controller;

  @override
  State<BaseTimedCaptureSection> createState() => _BaseTimedCaptureSectionState();
}

class _BaseTimedCaptureSectionState extends State<BaseTimedCaptureSection> {
  Timer? _tick;

  @override
  void initState() {
    super.initState();
    widget.controller.addListener(_changed);
    _tick = Timer.periodic(const Duration(seconds: 1), (_) => _changed());
  }

  @override
  void dispose() {
    _tick?.cancel();
    widget.controller.removeListener(_changed);
    super.dispose();
  }

  void _changed() {
    if (mounted) setState(() {});
  }

  String _until(DateTime? at) {
    if (at == null) return '-';
    final s = at.difference(clock.now()).inSeconds;
    return s <= 0 ? 'now' : '${s ~/ 60}:${(s % 60).toString().padLeft(2, '0')}';
  }

  String _status(BaseTimedCapture c) => switch (c.phase) {
        TimedCapturePhase.idle => c.lastReason == null ? 'Not armed. Nothing is recording.' : 'Not armed: ${c.lastReason}',
        TimedCapturePhase.arming => 'Checking account, mic permission, speech and recorder...',
        TimedCapturePhase.starting => 'STARTING the microphone...',
        TimedCapturePhase.armed =>
          'ARMED, not recording. Trigger allowed in ${_until(c.notBefore)}; expires in ${_until(c.expiresAt)}.',
        TimedCapturePhase.recording => 'RECORDING. Stops itself in ${_until(c.stopAt)}.',
        TimedCapturePhase.stopping => 'Stopping and saving...',
        TimedCapturePhase.done => 'Done, mic verified closed. Check Conversations for the saved recording.',
        TimedCapturePhase.failed => 'Failed: ${c.lastReason}. Mic verified closed. Evidence kept.',
        TimedCapturePhase.hold =>
          'HOLD: mic closure NOT verified (${c.lastReason}). Re-arming is blocked. Check the mic, then Resolve.',
        TimedCapturePhase.cancelled => 'Cancelled: ${c.lastReason}',
      };

  @override
  Widget build(BuildContext context) {
    final c = widget.controller;
    final armed = c.phase == TimedCapturePhase.armed;
    final busy = c.phase == TimedCapturePhase.starting ||
        c.phase == TimedCapturePhase.recording ||
        c.phase == TimedCapturePhase.stopping;
    return OmiSettingsGroup(
      header: 'DEV ONLY: Base timed capture test',
      children: [
        Padding(
          padding: const EdgeInsets.all(OmiSpacing.md),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                'Arms ONE phone-mic recording of ${c.config.boundedRecordFor.inSeconds}s. It starts only when the '
                '"Run armed Ella Base timed capture test" Shortcuts action runs, at least '
                '${c.config.notBefore.inSeconds}s and at most ${c.config.boundedWindow.inMinutes} min after arming, '
                'then stops by itself. It never starts over, stops or adopts any other recording. Signing out or a busy recorder cancels it.',
              ),
              const SizedBox(height: OmiSpacing.sm),
              Text(_status(c), style: const TextStyle(fontWeight: FontWeight.w600)),
              const SizedBox(height: OmiSpacing.sm),
              if (armed || c.phase == TimedCapturePhase.arming)
                OmiButton.destructive(label: 'Cancel test', onPressed: () => c.cancel('operator_cancelled'))
              else if (c.phase == TimedCapturePhase.hold)
                OmiButton.secondary(label: 'Resolve hold (mic must be idle)', onPressed: () => c.resolveHold())
              else
                OmiButton.secondary(
                  label: 'Arm one timed recording',
                  onPressed: busy || c.lateStartPending ? null : () => c.arm(),
                ),
            ],
          ),
        ),
      ],
    );
  }
}
