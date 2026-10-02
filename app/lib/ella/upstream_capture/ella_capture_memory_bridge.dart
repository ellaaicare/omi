import 'dart:async';

import 'package:flutter/widgets.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/http/api/conversations.dart' as api;
import 'package:omi/backend/schema/conversation.dart' as ella;
import 'package:omi/ella/upstream_capture/ella_capture_authority.dart';
import 'package:omi/ella/widgets/ella_source_indicator.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/upstream_capture/backend/schema/conversation.dart' as upstream;
import 'package:omi/upstream_capture/services/capture/capture_external_actions.dart';
import 'package:omi/upstream_capture/services/capture/optimistic_processing.dart';

typedef EllaCaptureMemoryLoader = Future<ella.ServerConversation?> Function(
  String id, {
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
});

/// Projects capture notifications through the canonical Ella read contract.
/// The imported conversation model cannot carry Ella summary provenance.
class EllaCaptureMemoryBridge extends NoopCaptureExternalActions {
  EllaCaptureMemoryBridge({
    required this.captureAuthority,
    EllaCaptureMemoryLoader? loader,
    AccountCommitAuthority? Function()? accountAuthority,
    this.retryDelays = const [
      Duration(seconds: 2),
      Duration(seconds: 4),
      Duration(seconds: 8),
      Duration(seconds: 16),
      Duration(seconds: 32),
      Duration(seconds: 64),
    ],
    this.readTimeout = const Duration(seconds: 15),
  })  : _loader = loader ?? api.getConversationById,
        _accountAuthority = accountAuthority ?? WalOwnerAuthority.active;

  final EllaCaptureAuthority captureAuthority;
  final EllaCaptureMemoryLoader _loader;
  final AccountCommitAuthority? Function() _accountAuthority;
  final List<Duration> retryDelays;
  final Duration readTimeout;
  final Map<String, _MemoryRead> _reads = {};
  ConversationProvider? _provider;
  int _attachmentEpoch = 0;
  int _readEpoch = 0;

  int attach(ConversationProvider provider) {
    cancel();
    _attachmentEpoch++;
    _provider = provider;
    return _attachmentEpoch;
  }

  void detach(ConversationProvider provider, {int? attachmentEpoch}) {
    if (!identical(provider, _provider) || (attachmentEpoch != null && attachmentEpoch != _attachmentEpoch)) return;
    cancel();
    _attachmentEpoch++;
    _provider = null;
  }

  void cancel() {
    _readEpoch++;
    for (final read in _reads.values) {
      read.timer?.cancel();
    }
    _reads.clear();
  }

  @override
  void addProcessingConversation(upstream.ServerConversation conversation) =>
      _reconcile(conversation.id, _MemoryPhase.processing);

  @override
  void upsertConversation(upstream.ServerConversation conversation) =>
      _reconcile(conversation.id, _MemoryPhase.completed);

  @override
  bool hasConversation(String conversationId) =>
      _provider?.conversations.any((conversation) => conversation.id == conversationId) ?? false;

  void _reconcile(String id, _MemoryPhase phase) {
    if (id.isEmpty || id == OptimisticProcessingPlaceholder.id) return;
    final provider = _provider;
    final account = _accountAuthority();
    final uid = captureAuthority.boundUid;
    if (provider == null ||
        !provider.canProjectCaptureConversation(id) ||
        account == null ||
        uid == null ||
        uid != account.uid ||
        !account.isExactCurrent()) {
      return;
    }
    final previous = _reads[id];
    if (previous != null) {
      if (phase == _MemoryPhase.processing || previous.phase == _MemoryPhase.completed) return;
      previous.phase = _MemoryPhase.completed;
      if (previous.terminal || !previous.budgetExhausted) return;
    }
    final epoch = _attachmentEpoch;
    final readEpoch = _readEpoch;
    final projection = provider.captureProjectionGeneration;
    final binding = captureAuthority.bindingEpoch;
    final origin = _MemoryOrigin(
      account,
      () =>
          epoch == _attachmentEpoch &&
          readEpoch == _readEpoch &&
          identical(provider, _provider) &&
          projection == provider.captureProjectionGeneration &&
          provider.canProjectCaptureConversation(id) &&
          binding == captureAuthority.bindingEpoch &&
          captureAuthority.boundUid == uid &&
          captureAuthority.hasCurrentAuthority,
    );
    final read = _MemoryRead(origin, provider, phase);
    _reads[id] = read;
    unawaited(_read(id, read));
  }

  Future<void> _read(String id, _MemoryRead read) async {
    bool current() => identical(_reads[id], read) && read.origin.isExactCurrent();
    if (!current()) return;
    ella.ServerConversation? result;
    try {
      result = await _loader(id, expectedAuthenticatedUid: read.origin.uid, exactAuthority: read.origin)
          .timeout(readTimeout);
    } catch (_) {
      // A read failure never erases a retained canonical summary or claims completion.
    }
    if (!current()) return;
    if (result != null && result.id == id) {
      if (!result.deleted && !result.discarded) read.provider.applyCanonicalCaptureConversation(result);
      final enrichment = result.enrichmentState;
      if (result.deleted ||
          result.discarded ||
          hasCurrentHermesSummary(result) ||
          enrichment?['status'] == 'failed' ||
          enrichment?['canonical_status'] == 'failed') {
        read.terminal = true;
        return;
      }
    }
    if (read.attempt >= retryDelays.length) {
      read.budgetExhausted = true;
      return;
    }
    final delay = retryDelays[read.attempt++];
    read.timer = Timer(delay, () {
      read.timer = null;
      if (current()) unawaited(_read(id, read));
    });
  }
}

enum _MemoryPhase { processing, completed }

class _MemoryRead {
  _MemoryRead(this.origin, this.provider, this.phase);
  final _MemoryOrigin origin;
  final ConversationProvider provider;
  Timer? timer;
  int attempt = 0;
  _MemoryPhase phase;
  bool budgetExhausted = false;
  bool terminal = false;
}

class _MemoryOrigin implements ExactAccountAuthorityVerifier {
  _MemoryOrigin(this.account, this.current);
  final AccountCommitAuthority account;
  final bool Function() current;
  @override
  String get uid => account.uid;
  @override
  bool isExactCurrent() => current() && account.isExactCurrent();
}

/// Binds the real Home provider without installing a second conversation graph.
class EllaCaptureMemoryBinding extends StatefulWidget {
  const EllaCaptureMemoryBinding({super.key, required this.bridge, required this.child});
  final EllaCaptureMemoryBridge bridge;
  final Widget child;
  @override
  State<EllaCaptureMemoryBinding> createState() => _EllaCaptureMemoryBindingState();
}

class _EllaCaptureMemoryBindingState extends State<EllaCaptureMemoryBinding> {
  ConversationProvider? _provider;
  int? _attachmentEpoch;
  @override
  void didUpdateWidget(EllaCaptureMemoryBinding oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (identical(oldWidget.bridge, widget.bridge) || _provider == null) return;
    oldWidget.bridge.detach(_provider!, attachmentEpoch: _attachmentEpoch);
    _attachmentEpoch = widget.bridge.attach(_provider!);
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final provider = context.watch<ConversationProvider>();
    if (identical(provider, _provider)) return;
    if (_provider != null) widget.bridge.detach(_provider!, attachmentEpoch: _attachmentEpoch);
    _provider = provider;
    _attachmentEpoch = widget.bridge.attach(provider);
  }

  @override
  void dispose() {
    if (_provider != null) widget.bridge.detach(_provider!, attachmentEpoch: _attachmentEpoch);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => widget.child;
}
