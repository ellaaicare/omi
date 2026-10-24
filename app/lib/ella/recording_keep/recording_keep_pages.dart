import 'dart:async';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter/material.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

import 'discarded_recording_save_card.dart';
import 'explicit_recording_keep.dart';
import 'recording_keep_repository.dart';

/// Journal entry point. Merely rendering Journal performs no retention reads.
class SavedRecordingsJournalLink extends StatelessWidget {
  const SavedRecordingsJournalLink({super.key});

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
    child: Align(alignment: Alignment.centerLeft, child: TextButton.icon(
      key: const Key('journal-recordings-without-summary'),
      icon: const Icon(Icons.description_outlined),
      label: const Text('Recordings without summaries'),
      onPressed: () {
        final authority = WalOwnerAuthority.active();
        if (authority == null || !authority.isExactCurrent()) {
          ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
            content: Text('Recording access is not confirmed. Try again when setup is ready.')));
          return;
        }
        Navigator.of(context).push(MaterialPageRoute<void>(builder: (_) =>
          RecordingKeepPage(repository: RecordingKeepRepository(authority))));
      },
    )),
  );
}

// Check on auth/authority notifications and resume, as well as every render and
// action/egress. A retired route never re-adopts a new account or authority.
mixin _KeepAuthorityState<T extends StatefulWidget> on State<T>
    implements WidgetsBindingObserver {
  RecordingKeepRepository get repository;
  final ValueNotifier<int> presentation = ValueNotifier(0);
  StreamSubscription<User?>? _auth;
  bool _viewRetired = false;
  bool get current {
    if (!repository.current) _viewRetired = true;
    return !_viewRetired;
  }

  void watchAuthority({Listenable? injectedChanges}) {
    _changes = injectedChanges ?? SharedPreferencesUtil.aiConsentAuthorityChanges;
    _changes!.addListener(_authorityChanged);
    if (injectedChanges == null) {
      _auth = FirebaseAuth.instance.authStateChanges().listen((_) => _authorityChanged());
    }
    WidgetsBinding.instance.addObserver(this);
  }

  Listenable? _changes;
  void _authorityChanged() {
    if (!mounted) return;
    setState(() { current; presentation.value++; });
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _authorityChanged();
  }

  void stopWatchingAuthority() {
    _changes?.removeListener(_authorityChanged);
    _auth?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    presentation.dispose();
  }

  // WidgetsBindingObserver is a mixin with defaults; explicit noSuchMethod is
  // not used. Concrete states below mix it in before this shared implementation.
}

class RecordingKeepPage extends StatefulWidget {
  const RecordingKeepPage({super.key, required this.repository, this.authorityChanges});
  final RecordingKeepRepository repository;
  final Listenable? authorityChanges;
  @override
  State<RecordingKeepPage> createState() => _RecordingKeepPageState();
}

class _RecordingKeepPageState extends State<RecordingKeepPage>
    with WidgetsBindingObserver, _KeepAuthorityState<RecordingKeepPage> {
  @override
  RecordingKeepRepository get repository => widget.repository;
  bool _savedTab = false, _loading = false, _failed = false;
  int _request = 0;
  List<ServerConversation> _recent = [];
  List<KeptRecordingHint> _saved = [];

  @override
  void initState() {
    super.initState();
    watchAuthority(injectedChanges: widget.authorityChanges);
    _load();
  }

  Future<void> _load() async {
    if (!current) return;
    final request = ++_request;
    setState(() { _loading = true; _failed = false; _recent = []; _saved = []; });
    try {
      if (_savedTab) {
        final rows = await repository.saved();
        if (mounted && current && request == _request) setState(() { _saved = rows; });
      } else {
        final rows = await repository.recent();
        if (mounted && current && request == _request) setState(() { _recent = rows; });
      }
    } catch (_) {
      if (mounted && current && request == _request) setState(() { _failed = true; });
    } finally {
      if (mounted && request == _request) setState(() { _loading = false; });
    }
  }

  Future<void> _open(String cid, {KeepReceipt? receipt}) async {
    if (!current) return;
    await Navigator.of(context).push(MaterialPageRoute<void>(builder: (_) =>
      RecordingKeepDetailPage(repository: repository, cid: cid,
        expectedReceipt: receipt, authorityChanges: widget.authorityChanges)));
    if (mounted && current) await _load();
  }

  @override
  void dispose() { stopWatchingAuthority(); super.dispose(); }

  @override
  Widget build(BuildContext context) {
    final active = current;
    if (!active) { _recent = []; _saved = []; }
    return Scaffold(
      appBar: AppBar(title: const Text('Recordings without summaries')),
      body: !active ? const Center(child: Text('Recording access changed. Close this view.'))
          : ListView(padding: const EdgeInsets.all(16), children: [
            Wrap(spacing: 8, children: [
              ChoiceChip(label: const Text('Recent recordings'), selected: !_savedTab,
                onSelected: _loading ? null : (_) { _savedTab = false; _load(); }),
              ChoiceChip(label: const Text('Saved recordings'), selected: _savedTab,
                onSelected: _loading ? null : (_) { _savedTab = true; _load(); }),
            ]),
            const Text('Up to 20 recent recordings. This is not your complete history.'),
            if (_loading) const LinearProgressIndicator(),
            if (_failed) const Text('Recordings could not be loaded. This does not mean they are empty.'),
            if (!_loading && !_failed && (_savedTab ? _saved.isEmpty : _recent.isEmpty))
              const Text('No matching recordings in this recent page.'),
            if (_savedTab) ..._saved.map((r) => ListTile(
              title: const Text('Saved recording'),
              subtitle: Text('Not summarized · ${r.receipt.keptAt}'),
              onTap: () => _open(r.cid, receipt: r.receipt),
            )) else ..._recent.map((r) => ListTile(
              title: const Text('Recording without summary'),
              subtitle: Text('${r.createdAt.toLocal()} · Open to check Save status'),
              onTap: () => _open(r.id),
            )),
            TextButton(onPressed: _loading ? null : _load, child: const Text('Refresh recordings')),
          ]),
    );
  }
}

class RecordingKeepDetailPage extends StatefulWidget {
  const RecordingKeepDetailPage({super.key, required this.repository,
    required this.cid, this.expectedReceipt, this.authorityChanges});
  final RecordingKeepRepository repository;
  final String cid;
  final KeepReceipt? expectedReceipt;
  final Listenable? authorityChanges;
  @override
  State<RecordingKeepDetailPage> createState() => _KeepDetailState();
}

class _KeepDetailState extends State<RecordingKeepDetailPage>
    with WidgetsBindingObserver, _KeepAuthorityState<RecordingKeepDetailPage> {
  @override
  RecordingKeepRepository get repository => widget.repository;
  KeepDetail? _detail;
  KeepSession? _session;
  bool _loading = true, _failed = false, _creating = false;

  @override
  void initState() {
    super.initState();
    watchAuthority(injectedChanges: widget.authorityChanges);
    _load();
  }

  Future<void> _load() async {
    if (!current) return;
    setState(() { _loading = true; _failed = false; _detail = null; _session = null; });
    try {
      final detail = await repository.detail(widget.cid, expectedReceipt: widget.expectedReceipt);
      final session = await repository.restore(widget.cid);
      if (!mounted || !current) return;
      final intent = session?.intent;
      if (intent?.preconditions != null &&
          !intent!.preconditions!.same(detail.view.preconditions)) {
        throw StateError('Source changed after Save intent');
      }
      if (intent?.firstReceipt != null && (detail.view.receipt == null ||
          !sameKeepReceipt(intent!.firstReceipt!, detail.view.receipt!))) {
        throw StateError('Save receipt changed');
      }
      setState(() { _detail = detail; _session = session; });
    } catch (_) {
      if (mounted && current) setState(() { _failed = true; });
    } finally {
      if (mounted) setState(() { _loading = false; });
    }
  }

  Future<void> _save() async {
    if (_creating || !current || _detail == null ||
        !recordingCanBeKept(_detail!.conversation)) return;
    setState(() { _creating = true; _failed = false; });
    try {
      // UUID creation/storage occurs only for this deliberate tap. The shared
      // session coalesces same-CID views; persisted attempted=true survives exit.
      final session = await repository.explicitIntent(widget.cid);
      if (!mounted || !current) return;
      setState(() { _session = session; });
      await session.save();
    } catch (_) {
      if (mounted && current) setState(() { _failed = true; });
    } finally {
      if (mounted) setState(() { _creating = false; });
    }
  }

  @override
  void dispose() { stopWatchingAuthority(); super.dispose(); }

  @override
  Widget build(BuildContext context) {
    final active = current;
    if (!active) { _detail = null; _session = null; }
    final detail = _detail;
    return Scaffold(
      appBar: AppBar(title: const Text('Recording transcript')),
      body: !active ? const Center(child: Text('Recording access changed. Close this view.'))
          : ListView(padding: const EdgeInsets.all(16), children: [
            if (_loading) const LinearProgressIndicator(),
            if (_failed) const Text('Recording or Save state could not be confirmed. No automatic Save was sent.'),
            if (detail != null) ...[
              if (detail.view.kept) ...[
                const Text('Saved recording'), const Text('Not summarized'),
                if (detail.view.processingState == 'paused') const Text('Processing paused'),
              ] else if (_creating)
                const Text('Saving recording…')
              else if (_session != null)
                DiscardedRecordingSaveCard(
                  key: ValueKey(_session), session: _session!, authorityChanges: presentation,
                  eligible: () => current && recordingCanBeKept(detail.conversation),
                )
              else FilledButton(
                key: const Key('initial-save-recording'),
                onPressed: _creating || _failed ? null : _save,
                child: Text(_creating ? 'Saving recording…' : 'Save recording'),
              ),
              const SizedBox(height: 16),
              ...detail.conversation.transcriptSegments.map((segment) =>
                Padding(padding: const EdgeInsets.only(bottom: 12),
                  child: SelectableText(segment.text))),
            ],
            if (!_loading && !_creating)
              TextButton(onPressed: _load, child: const Text('Refresh recording status')),
          ]),
    );
  }
}
