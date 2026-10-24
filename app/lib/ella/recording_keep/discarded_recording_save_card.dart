import 'package:flutter/material.dart';

import 'explicit_recording_keep.dart';

/// Prepared UI for an owner-fenced, nonempty completed/discarded detail.
/// The host must restore the durable intent before mounting this card. This
/// widget never creates an operation ID, performs an automatic read/write,
/// changes consent, invokes a provider, or rewrites the discarded source.
class DiscardedRecordingSaveCard extends StatefulWidget {
  const DiscardedRecordingSaveCard({
    super.key,
    required this.session,
    required this.authorityChanges,
    required this.eligible,
    this.onOpenSavedRecording,
  });

  final KeepSession session;
  final Listenable authorityChanges;
  // Must recheck current owner-fenced detail: completed, discarded, nonempty.
  // Server preconditions remain authoritative even when this returns true.
  final bool Function() eligible;
  final VoidCallback? onOpenSavedRecording;

  @override
  State<DiscardedRecordingSaveCard> createState() => _SaveCardState();
}

class _SaveCardState extends State<DiscardedRecordingSaveCard> {
  bool _busy = false;
  bool _retired = false;
  bool _error = false;

  bool _current() {
    if (!widget.session.isCurrent(widget.session.intent.owner)) _retired = true;
    return !_retired;
  }

  @override
  void initState() {
    super.initState();
    widget.authorityChanges.addListener(_authorityChanged);
    _current();
  }

  void _authorityChanged() {
    if (!mounted) return;
    setState(() { _current(); });
  }

  @override
  void didUpdateWidget(covariant DiscardedRecordingSaveCard oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.authorityChanges, widget.authorityChanges)) {
      oldWidget.authorityChanges.removeListener(_authorityChanged);
      widget.authorityChanges.addListener(_authorityChanged);
    }
    // A new owner/source/intent requires a new keyed card. Never adopt it into
    // an open old view or let an old asynchronous completion update that view.
    if (!identical(oldWidget.session, widget.session)) _retired = true;
    _current();
  }

  @override
  void dispose() {
    widget.authorityChanges.removeListener(_authorityChanged);
    super.dispose();
  }

  Future<void> _act({required bool reconcile}) async {
    if (_busy || !_current() || !widget.eligible()) return;
    final session = widget.session;
    if (session.outcome == KeepOutcome.blocked) return;
    if (!reconcile && session.intent.postAttempted) return;
    setState(() { _busy = true; _error = false; });
    try {
      if (reconcile) {
        await session.reconcile();
      } else {
        await session.save();
      }
    } catch (_) {
      // No exception text, transcript, CID or account identifiers in UI/logs.
      if (mounted && identical(session, widget.session) && _current()) {
        _error = true;
      }
    } finally {
      if (mounted && identical(session, widget.session)) {
        setState(() { _busy = false; _current(); });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    if (!_current()) {
      return const Text('Recording unavailable in this view.',
          key: Key('discarded-save-retired'));
    }
    final session = widget.session;
    final saved = session.outcome == KeepOutcome.saved &&
        session.view?.kept == true && session.intent.firstReceipt != null;
    final blocked = session.outcome == KeepOutcome.blocked;
    final attempted = session.intent.postAttempted;
    final unknown = !saved && attempted;
    final enabled = !_busy && !blocked && widget.eligible();
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(saved ? 'Saved recording' : 'Recording not saved',
                key: const Key('discarded-save-status'),
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            const Text('Not summarized'),
            if (session.view?.processingState == 'paused')
              const Text('Processing paused'),
            if (!saved)
              Text(unknown
                  ? 'Save not confirmed. Check its status before doing anything else.'
                  : 'Keep this transcript without generating a summary.'),
            if (_error || blocked)
              const Text('Could not confirm this recording. Your transcript was not submitted again.'),
            const SizedBox(height: 12),
            if (saved && widget.onOpenSavedRecording != null)
              TextButton(
                key: const Key('open-saved-recording'),
                onPressed: enabled ? () {
                  if (_current() && widget.eligible()) widget.onOpenSavedRecording!();
                } : null,
                child: const Text('Open saved recording'),
              )
            else if (!saved)
              FilledButton(
                key: const Key('save-discarded-recording'),
                onPressed: enabled ? () => _act(reconcile: unknown) : null,
                child: Text(_busy ? 'Checking recording…'
                    : unknown ? 'Check save status' : 'Save recording'),
              ),
          ],
        ),
      ),
    );
  }
}
