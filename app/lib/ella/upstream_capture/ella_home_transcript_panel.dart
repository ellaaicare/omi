import 'dart:async';
import 'package:flutter/material.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_runtime.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_dock.dart' show EllaCaptureTranscriptSheet;
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/upstream_capture/providers/capture_provider.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// Home projection of the already-running capture graph. Transcript reading is
/// read-only; Create memory finalizes the current window without stopping necklace ingress.
class EllaHomeTranscriptPanel extends StatefulWidget {
  const EllaHomeTranscriptPanel({super.key, required this.runtime, this.authenticatedUid});
  final EllaUpstreamCaptureRuntime runtime;
  final String Function()? authenticatedUid;
  @override
  State<EllaHomeTranscriptPanel> createState() => _EllaHomeTranscriptPanelState();
}

class _EllaHomeTranscriptPanelState extends State<EllaHomeTranscriptPanel> with WidgetsBindingObserver {
  CaptureProvider? _provider;
  Timer? _clock;
  bool _opening = false;
  EllaCaptureSessionView? _lastObservedSession;
  int? _lastObservedWindow;
  String get _uid => widget.authenticatedUid?.call() ?? WalOwnerAuthority.authenticatedUid;
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    widget.runtime.lastRevocation.addListener(_changed);
    widget.runtime.finishPending.addListener(_changed);
    _attach();
    _syncClock();
  }

  void _adopt(CaptureProvider? provider) {
    if (identical(_provider, provider)) return;
    _provider?.removeListener(_changed);
    _provider = provider;
    _lastObservedSession = null;
    _lastObservedWindow = null;
    provider?.addListener(_changed);
  }

  Future<void> _attach() async {
    // Join the graph already installed/booted by the normal Home capture dock.
    final runtime = widget.runtime;
    try {
      final provider = await runtime.ensureBooted();
      if (!mounted || !identical(runtime, widget.runtime)) return;
      if (identical(provider, runtime.provider)) _adopt(provider);
      _changed();
    } catch (_) {
      _changed();
    }
  }

  @override
  void didUpdateWidget(covariant EllaHomeTranscriptPanel oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.runtime, widget.runtime)) {
      oldWidget.runtime.lastRevocation.removeListener(_changed);
      oldWidget.runtime.finishPending.removeListener(_changed);
      _adopt(null);
      widget.runtime.lastRevocation.addListener(_changed);
    widget.runtime.finishPending.addListener(_changed);
      _attach();
    }
  }

  void _changed() {
    if (!mounted) return;
    _adopt(widget.runtime.provider);
    setState(() {});
  }

  void _syncClock() {
    _clock?.cancel();
    final state = WidgetsBinding.instance.lifecycleState;
    if (state == null || state == AppLifecycleState.resumed) {
      _clock = Timer.periodic(const Duration(seconds: 2), (_) => _changed());
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _syncClock();
    if (state == AppLifecycleState.resumed) _changed();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _clock?.cancel();
    _provider?.removeListener(_changed);
    widget.runtime.lastRevocation.removeListener(_changed);
    widget.runtime.finishPending.removeListener(_changed);
    super.dispose();
  }

  bool _ownsRead(EllaCaptureSessionView session) =>
      mounted &&
      _uid == session.uid &&
      widget.runtime.authority.hasCurrentAuthority &&
      widget.runtime.authority.boundUid == session.uid &&
      widget.runtime.authority.bindingEpoch == session.bindingEpoch;

  bool _canFinish(CaptureProvider provider, EllaCaptureSessionView session) =>
      _ownsRead(session) && identical(provider, widget.runtime.provider) &&
      provider.segments.any((segment) => segment.text.trim().isNotEmpty) &&
      widget.runtime.canFinishSession(session);

  Future<void> _finish(CaptureProvider provider, EllaCaptureSessionView session) async {
    if (!_canFinish(provider, session)) return;
    try {
      // Existing owner/window admission and single-flight submission remain authoritative.
      // Finish the conversation window; continuous necklace ingress stays running.
      await widget.runtime.finishCaptureSession(session, stopDeviceIngress: false);
      if (mounted && _ownsRead(session)) {
        final outcome = widget.runtime.finishOutcomeFor(session);
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(ellaCaptureFinishMessage(outcome)),
          duration: const Duration(seconds: 8),
        ));
      }
    } catch (_) {
      if (mounted && _ownsRead(session)) {
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('Could not confirm memory creation. Check Journal before trying again.'),
        ));
      }
    } finally {
      _changed();
    }
  }

  Future<void> _open(CaptureProvider provider, EllaCaptureSessionView session) async {
    final window = provider.captureWindowGeneration;
    // Read-only retained words do not require restarting capture. A replacement
    // account, binding or transcript window permanently retires this viewer.
    var retired = false;
    bool current() {
      if (!_ownsRead(session) ||
          !identical(provider, widget.runtime.provider) ||
          provider.captureWindowGeneration != window) retired = true;
      return !retired;
    }

    if (_opening || !current()) return;
    _opening = true;
    try {
      await showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        useSafeArea: true,
        builder: (ctx) => EllaCaptureTranscriptSheet(
          provider: provider,
          presentationChanges: widget.runtime.lastRevocation,
          isCurrent: current,
          sourceLabel:
              session.source == 'phone' ? ctx.l10n.todayDockTranscriptPhone : ctx.l10n.todayDockTranscriptNecklace,
          onClose: () => true,
          actionsBuilder: (context) => ValueListenableBuilder<bool>(
            valueListenable: widget.runtime.finishPending,
            builder: (context, pending, _) => FilledButton(
              key: const Key('transcript-create-memory'),
              onPressed: !pending && current() && _canFinish(provider, session)
                  ? () => _finish(provider, session) : null,
              child: Text(pending ? 'Creating memory…' : 'Create memory'),
            ),
          ),
          detailsBuilder: (context) {
            final health = widget.runtime.captureDiagnostics.audioHealth;
            final last = health?['last_packet_utc'];
            return ExpansionTile(
              key: const Key('home-transcript-audio-details'),
              title: Text('Audio details', style: TextStyle(color: EllaPalette.of(context).ink)),
              iconColor: EllaPalette.of(context).ink,
              collapsedIconColor: EllaPalette.of(context).ink,
              childrenPadding: const EdgeInsets.all(12),
              children: [
                Text(
                  'Last audio: ${last ?? 'not observed'}\n'
                  '${provider.transcriptServiceReady ? 'Transcription service ready' : 'Transcription not confirmed'}',
                )
              ],
            );
          },
        ),
      );
    } finally {
      _opening = false;
    }
  }

  @override
  Widget build(BuildContext context) {
    final r = widget.runtime;
    final p = identical(_provider, r.provider) ? _provider : null;
    final candidate = p == null ? null : r.captureSession;
    final session =
        candidate != null && candidate.uid == _uid && r.isCurrentCaptureSession(candidate) ? candidate : null;
    if (session != null) {
      _lastObservedSession = session;
      _lastObservedWindow = p?.captureWindowGeneration;
    }
    final observed = _lastObservedSession;
    final readable =
        observed != null && _ownsRead(observed) && _lastObservedWindow == p?.captureWindowGeneration ? observed : null;
    if (readable == null) _lastObservedSession = null;
    final health = readable == null ? null : r.captureDiagnostics.audioHealth;
    final link = health?['ble_link'] == 'connected';
    final recent = health?['stream_health'] == 'audio_recent';
    final stale = health?['stream_health'] == 'audio_stale' && health?['capture_expected'] == true;
    final paused = session != null && (session.source == 'phone' ? r.isCapturePaused(session) : p?.isPaused == true);
    final ready = session != null && !paused && p?.transcriptServiceReady == true;
    final status = session == null
        ? context.l10n.upstreamCaptureIdle
        : paused
            ? context.l10n.irisCapturePaused
            : session.source == 'phone'
                ? context.l10n.irisPhoneMicrophoneOn
                : stale
                    ? context.l10n.ellaHomeAudioStalled
                    : recent
                        ? context.l10n.ellaHomeAudioReceiving
                        : link
                            ? context.l10n.ellaHomeNecklaceConnected
                            : context.l10n.ellaHomeAudioUnconfirmed;
    final at = health?['last_packet_utc'];
    final time = at is String ? DateTime.tryParse(at)?.toLocal() : null;
    final last = time == null
        ? context.l10n.ellaHomeAudioNever
        : context.l10n.ellaHomeAudioTime(
            MaterialLocalizations.of(context).formatTimeOfDay(TimeOfDay.fromDateTime(time)),
          );
    // Retained words remain read-only under the exact owner/binding. Never
    // expose an earlier account's words or restart capture to read them.
    final segments = readable == null ? null : p?.segments;
    final tail = segments == null
        ? const <String>[]
        : segments.skip((segments.length - 3).clamp(0, segments.length)).map((s) => s.text).toList();
    final words = tail.join('\n');
    final preview = words.length > 3000 ? words.substring(words.length - 3000) : words;
    return EllaHomeTranscriptCard(
      receivingAudio: session != null && !paused && recent,
      audioStalled: session != null && !paused && stale,
      finishing: r.finishPending.value,
      onFinish: session != null && p != null && _canFinish(p, session)
          ? () => _finish(p, session) : null,
      status: status,
      lastAudio: last,
      transcription: ready ? context.l10n.ellaHomeTranscribing : context.l10n.ellaHomeTranscriptionUnconfirmed,
      preview: preview,
      compact: MediaQuery.viewInsetsOf(context).bottom > 0,
      onOpen: readable != null && p != null ? () => _open(p, readable) : null,
    );
  }
}

/// Pure presentation seam: no network, capture pause, or device control.
class EllaHomeTranscriptCard extends StatelessWidget {
  const EllaHomeTranscriptCard({
    super.key,
    required this.status,
    required this.lastAudio,
    required this.transcription,
    required this.preview,
    required this.onOpen,
    this.compact = false,
    this.stockCapture,
    this.receivingAudio = false,
    this.audioStalled = false,
    this.onFinish,
    this.finishing = false,
  });
  final String status, lastAudio, transcription, preview;
  final VoidCallback? onOpen, onFinish;
  final bool compact, receivingAudio, audioStalled, finishing;
  // Retained constructor compatibility. Primary recording controls are deliberately
  // absent: reading/finalizing words must not pause continuous necklace capture.
  final Widget? stockCapture;

  @override
  Widget build(BuildContext context) {
    final palette = EllaPalette.of(context);
    final large = MediaQuery.textScalerOf(context).scale(1) >= 2;
    final style = TextButton.styleFrom(
      minimumSize: const Size(48, 48),
      foregroundColor: palette.ink,
      disabledForegroundColor: palette.inkSoft,
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
    );
    return ClipRect(
      child: ConstrainedBox(
        constraints: BoxConstraints(maxHeight: large ? 260 : 190),
        child: SingleChildScrollView(
          key: const Key('home-transcript-preview-scroll'),
          primary: false,
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Padding(
                  padding: const EdgeInsets.only(top: 5, right: 8),
                  child: Icon(Icons.circle, size: 12,
                    key: const Key('home-capture-status-dot'),
                    color: receivingAudio && !audioStalled ? const Color(0xFF238636)
                        : audioStalled ? Theme.of(context).colorScheme.error : palette.inkSoft),
                ),
                Expanded(child: Text(status, key: const Key('home-transcript-health'),
                  style: EllaTypography.of(context).body.copyWith(color: palette.ink))),
              ]),
              if (!compact) Text('$lastAudio · $transcription',
                style: EllaTypography.of(context).caption.copyWith(color: palette.inkSoft)),
              Wrap(spacing: 8, runSpacing: 4, children: [
                TextButton.icon(
                  key: const Key('home-open-transcript'),
                  onPressed: onOpen, style: style,
                  icon: const Icon(Icons.subtitles_outlined),
                  label: const Text('Current transcript'),
                ),
                TextButton.icon(
                  key: const Key('home-create-memory'),
                  onPressed: finishing ? null : onFinish, style: style,
                  icon: const Icon(Icons.note_add_outlined),
                  label: Text(finishing ? 'Creating memory…' : 'Create memory'),
                ),
              ]),
            ],
          ),
        ),
      ),
    );
  }
}
