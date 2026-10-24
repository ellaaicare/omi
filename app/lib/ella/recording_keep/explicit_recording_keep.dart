// SOURCE-ONLY proposal. No production transport, persistence, or UI is wired.
typedef Json = Map<String, dynamic>;
const keepContract = 'ella-user-explicit-keep-v1';

void _require(bool value, String message) {
  if (!value) throw FormatException(message);
}

String _text(Json value, String key) {
  final text = value[key];
  _require(text is String && text.isNotEmpty, 'Invalid $key');
  return text as String;
}

Json _object(dynamic value) {
  _require(value is Map<String, dynamic>, 'Expected JSON object');
  return value as Json;
}

bool _uuid(String value) => RegExp(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
    .hasMatch(value);

class KeepOwner {
  const KeepOwner(this.uid, this.receiptId, this.epoch);
  final String uid, receiptId, epoch;
}

class KeepPreconditions {
  KeepPreconditions.fromJson(Json json)
      : snapshot = _text(json, 'expected_source_snapshot_sha256'),
        transcript = _text(json, 'expected_transcript_sha256') {
    _require(RegExp(r'^[0-9a-f]{64}$').hasMatch(snapshot), 'Snapshot hash');
    _require(RegExp(r'^[0-9a-f]{64}$').hasMatch(transcript), 'Transcript hash');
    _require(json.containsKey('expected_active_summary_version_id') &&
        json['expected_active_summary_version_id'] == null, 'Active summary');
  }
  final String snapshot, transcript;
  Json toJson() => {
        'expected_source_snapshot_sha256': snapshot,
        'expected_transcript_sha256': transcript,
        'expected_active_summary_version_id': null,
      };
  bool same(KeepPreconditions other) =>
      snapshot == other.snapshot && transcript == other.transcript;
}

class KeepReceipt {
  static const _keys = {'contract', 'conversation_id', 'operation_id',
    'request_sha256', 'source_snapshot_sha256', 'transcript_sha256',
    'original_discard_reason', 'kept_at', 'summary_status',
    'provider_dispatched', 'canonical_write'};
  KeepReceipt.fromJson(Json json, String cid)
      : operationId = _text(json, 'operation_id'),
        requestHash = _text(json, 'request_sha256'),
        snapshot = _text(json, 'source_snapshot_sha256'),
        transcript = _text(json, 'transcript_sha256'),
        keptAt = _text(json, 'kept_at'),
        originalDiscardReason = json['original_discard_reason'],
        raw = Map.unmodifiable(json) {
    _require(json.length == _keys.length && json.keys.every(_keys.contains),
        'Unexpected receipt fields');
    _require(json['contract'] == keepContract &&
        json['conversation_id'] == cid && _uuid(operationId), 'Receipt identity');
    for (final hash in [requestHash, snapshot, transcript]) {
      _require(RegExp(r'^[0-9a-f]{64}$').hasMatch(hash), 'Receipt hash');
    }
    _require(DateTime.tryParse(keptAt) != null, 'Receipt timestamp');
    _require(originalDiscardReason == 'discarded_trivial',
        'Discard reason');
    _require(json['summary_status'] == 'not_generated' &&
        json['provider_dispatched'] == false && json['canonical_write'] == false,
        'Receipt must not claim generation');
  }
  final String operationId, requestHash, snapshot, transcript, keptAt;
  final Object? originalDiscardReason;
  final Json raw;
}

class KeepView {
  KeepView.fromJson(Json json, String cid, {String? requestedOperation})
      : kept = json['state'] == 'kept',
        processingState = _text(json, 'processing_state'),
        preconditions = KeepPreconditions.fromJson(_object(json['preconditions'])),
        receipt = json['receipt'] == null
            ? null : KeepReceipt.fromJson(_object(json['receipt']), cid) {
    _require(json['contract'] == keepContract && json['conversation_id'] == cid,
        'Response identity');
    _require(json['state'] == 'kept' || json['state'] == 'not_kept', 'Keep state');
    _require(json['summary_status'] == 'not_generated' &&
        json['provider_dispatched'] == false && json['canonical_write'] == false,
        'Keep is retention only');
    _require(processingState == 'paused' || processingState == 'not_requested',
        'Unknown processing state');
    _require(kept == (receipt != null), 'Receipt/state mismatch');
    if (receipt != null) {
      _require(receipt!.snapshot == preconditions.snapshot &&
          receipt!.transcript == preconditions.transcript, 'Receipt/source mismatch');
    }
    if (requestedOperation != null) {
      _require(kept && json['requested_operation_id'] == requestedOperation &&
          json['created'] is bool && json['already_kept'] is bool &&
          json['created'] != json['already_kept'], 'POST result mismatch');
      if (json['created'] == true) {
        _require(receipt!.operationId == requestedOperation, 'Created receipt intent');
      }
    }
  }
  final bool kept;
  final String processingState;
  final KeepPreconditions preconditions;
  final KeepReceipt? receipt;
  String get title => kept ? 'Saved recording' : 'Recording not confirmed saved';
  String get summaryLabel => 'Summary not generated';
  String? get processingLabel =>
      processingState == 'paused' ? 'Processing paused' : null;
}

// Persist all fields atomically in owner-isolated storage; never tokens or text.
// A new UUID is created ONLY for a deliberate new intent, not on rebuild/retry.
class KeepIntent {
  KeepIntent(this.owner, this.cid, this.operationId,
      {this.preconditions, this.postAttempted = false, this.firstReceipt}) {
    _require(owner.uid.isNotEmpty && owner.receiptId.isNotEmpty &&
        owner.epoch.isNotEmpty && _uuid(cid) && _uuid(operationId), 'Intent identity');
  }
  final KeepOwner owner;
  final String cid, operationId;
  KeepPreconditions? preconditions;
  bool postAttempted;
  KeepReceipt? firstReceipt;
}

class KeepRequest {
  const KeepRequest(this.method, this.path, this.owner, {this.body});
  final String method, path;
  final KeepOwner owner;
  final Json? body;
  int get retries => 0;
  bool get retryOnUnauthorized => false;
}

class KeepResponse {
  const KeepResponse(this.status, this.json);
  final int status;
  final Json json;
}

typedef KeepTransport = Future<KeepResponse> Function(KeepRequest request);
typedef PersistKeepIntent = Future<void> Function(KeepIntent intent);
enum KeepOutcome { idle, saved, unknownWrite, blocked }

class KeepSession {
  KeepSession(this.intent, this.transport, this.persist, this.isCurrent);
  final KeepIntent intent;
  final KeepTransport transport;
  final PersistKeepIntent persist;
  final bool Function(KeepOwner) isCurrent;
  bool _busy = false;
  KeepOutcome outcome = KeepOutcome.idle;
  KeepView? view;

  void _fence() {
    if (!isCurrent(intent.owner)) {
      view = null;
      outcome = KeepOutcome.blocked;
      throw StateError('Current owner/receipt changed');
    }
  }

  Future<void> _accept(KeepView accepted) async {
    final receipt = accepted.receipt;
    final first = intent.firstReceipt;
    if (first != null && receipt != null) {
      _require(first.raw.length == receipt.raw.length && first.raw.entries.every(
          (entry) => receipt.raw[entry.key] == entry.value), 'First receipt changed');
    }
    if (receipt != null) {
      intent.firstReceipt ??= receipt;
      await persist(intent);
      _fence();
    }
    view = accepted;
  }

  Future<KeepView> _read() async {
    _fence();
    final response = await transport(KeepRequest(
        'GET', '/v1/conversations/${intent.cid}/keep', intent.owner));
    _fence();
    if (response.status != 200) throw StateError('Keep read unavailable');
    final read = KeepView.fromJson(response.json, intent.cid);
    final expected = intent.preconditions;
    if (expected != null && !expected.same(read.preconditions)) {
      throw StateError('Source changed; do not replay Save');
    }
    return read;
  }

  Future<void> save() async {
    if (_busy) throw StateError('Save already in progress');
    _busy = true;
    try {
      _fence();
      if (intent.postAttempted) {
        view = null;
        outcome = KeepOutcome.unknownWrite;
        return; // Only explicit GET reconciliation is allowed now.
      }
      final read = await _read();
      if (read.kept) {
        await _accept(read);
        outcome = KeepOutcome.saved;
        return;
      }
      intent.preconditions = read.preconditions; // Echo server hashes only.
      intent.postAttempted = true; // Persist BEFORE dispatch; no uncertain retry.
      outcome = KeepOutcome.unknownWrite;
      await persist(intent);
      _fence();
      try {
        final response = await transport(KeepRequest(
          'POST', '/v1/conversations/${intent.cid}/keep', intent.owner,
          body: {'contract': keepContract, 'operation_id': intent.operationId,
            ...intent.preconditions!.toJson()},
        ));
        _fence();
        if (response.status != 200) {
          outcome = response.status == 401 || response.status == 403
              ? KeepOutcome.blocked : KeepOutcome.unknownWrite;
          return;
        }
        final saved = KeepView.fromJson(response.json, intent.cid,
            requestedOperation: intent.operationId);
        _require(saved.preconditions.same(intent.preconditions!), 'POST source drift');
        await _accept(saved);
        outcome = KeepOutcome.saved;
      } catch (_) {
        if (outcome != KeepOutcome.blocked) outcome = KeepOutcome.unknownWrite;
      }
    } finally {
      _busy = false;
    }
  }

  Future<void> reconcile() async {
    if (_busy) throw StateError('Save already in progress');
    _busy = true;
    view = null;
    outcome = KeepOutcome.unknownWrite;
    try {
      final read = await _read();
      await _accept(read);
      // GET absence does not prove a prior write can never commit.
      outcome = read.kept ? KeepOutcome.saved : KeepOutcome.unknownWrite;
    } finally {
      _busy = false;
    }
  }
}

// This bounded recent retention index is only a navigation hint. The adapter
// MUST fetch current owner-fenced detail for each CID before showing content.
List<String> mergeKeepIndex(List<String> normalCids, Json json) {
  _require(json['contract'] == keepContract && json['bounded_recent_index'] == true &&
      json['provider_dispatched'] == false && json['canonical_write'] == false &&
      json['more_available'] is bool, 'Invalid retention index');
  final limit = json['limit'];
  final items = json['items'];
  _require(limit is int && limit >= 1 && limit <= 20 &&
      items is List && items.length <= limit, 'Index bound');
  final ids = <String>{...normalCids};
  for (final item in items as List) {
    final row = _object(item);
    final cid = _text(row, 'conversation_id');
    _require(_uuid(cid) && row['needs_current_read'] == true, 'Current detail required');
    KeepReceipt.fromJson(_object(row['receipt']), cid);
    ids.add(cid);
  }
  return List.unmodifiable(ids);
}
