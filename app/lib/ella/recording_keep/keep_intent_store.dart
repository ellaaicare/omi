import 'dart:convert';

import 'explicit_recording_keep.dart';

typedef ReadIntent = Future<String?> Function(String key);
typedef WriteIntent = Future<bool> Function(String key, String value);

/// One serialized, acknowledged snapshot per account/CID; no text or tokens.
/// A changed receipt/epoch never silently replaces a pending operation.
class KeepIntentStore {
  KeepIntentStore(this.read, this.write, this.newOperationId);
  final ReadIntent read;
  final WriteIntent write;
  final String Function() newOperationId;
  Future<void> _tail = Future<void>.value();

  String key(KeepOwner owner, String cid) => jsonEncode([owner.uid, cid]);

  bool sameOwner(KeepOwner a, KeepOwner b) =>
      a.uid == b.uid && a.receiptId == b.receiptId && a.epoch == b.epoch;

  Future<T> _serial<T>(Future<T> Function() action) {
    final result = _tail.then((_) => action());
    _tail = result.then<void>((_) {}, onError: (Object _, StackTrace __) {});
    return result;
  }

  String encode(KeepIntent intent) => jsonEncode({
    'version': 1,
    'owner': {'uid': intent.owner.uid, 'receipt': intent.owner.receiptId,
      'epoch': intent.owner.epoch},
    'cid': intent.cid, 'operation': intent.operationId,
    'attempted': intent.postAttempted,
    'preconditions': intent.preconditions?.toJson(),
    'receipt': intent.firstReceipt?.raw,
  });

  KeepIntent decode(String value, KeepOwner owner, String cid) {
    if (value.length > 131072) throw FormatException('Intent exceeds bound');
    final data = jsonDecode(value);
    if (data is! Map<String, dynamic> || data['version'] != 1 ||
        data['owner'] is! Map<String, dynamic> || data['cid'] != cid ||
        data['operation'] is! String || data['attempted'] is! bool) {
      throw FormatException('Invalid recording intent');
    }
    final stored = data['owner'] as Map<String, dynamic>;
    if (stored['uid'] != owner.uid || stored['receipt'] != owner.receiptId ||
        stored['epoch'] != owner.epoch) {
      throw StateError('Recording intent belongs to a different authority');
    }
    final conditions = data['preconditions'] == null ? null
        : KeepPreconditions.fromJson(Map<String, dynamic>.from(data['preconditions']));
    final receipt = data['receipt'] == null ? null
        : KeepReceipt.fromJson(Map<String, dynamic>.from(data['receipt']), cid);
    if ((data['attempted'] == true || receipt != null) && conditions == null) {
      throw FormatException('Missing persisted source preconditions');
    }
    if (receipt != null && (receipt.snapshot != conditions!.snapshot ||
        receipt.transcript != conditions.transcript)) {
      throw FormatException('Persisted source and receipt differ');
    }
    return KeepIntent(owner, cid, data['operation'], preconditions: conditions,
        postAttempted: data['attempted'], firstReceipt: receipt);
  }

  Future<KeepIntent?> load(KeepOwner owner, String cid) => _serial(() async {
    final raw = await read(key(owner, cid));
    return raw == null ? null : decode(raw, owner, cid);
  });

  // Invoke only after the user's explicit Save; opening/rebuilding never mints.
  Future<KeepIntent> openOrCreate(KeepOwner owner, String cid) => _serial(() async {
    final raw = await read(key(owner, cid));
    if (raw != null) return decode(raw, owner, cid);
    final intent = KeepIntent(owner, cid, newOperationId());
    if (!await write(key(owner, cid), encode(intent))) {
      throw StateError('Recording intent was not persisted');
    }
    return intent;
  });

  Future<void> persist(KeepIntent intent) {
    // Capture bytes now, not a mutable object referenced by a queued write.
    final snapshot = encode(intent);
    return _serial(() async {
      final raw = await read(key(intent.owner, intent.cid));
      if (raw == null) throw StateError('Recording intent disappeared');
      final old = decode(raw, intent.owner, intent.cid);
      final next = decode(snapshot, intent.owner, intent.cid);
      if (old.operationId != next.operationId ||
          (old.postAttempted && !next.postAttempted) ||
          (old.preconditions != null &&
              (next.preconditions == null || !old.preconditions!.same(next.preconditions!))) ||
          (old.firstReceipt != null && (next.firstReceipt == null ||
              old.firstReceipt!.raw.length != next.firstReceipt!.raw.length ||
              !old.firstReceipt!.raw.entries.every((e) => next.firstReceipt!.raw[e.key] == e.value)))) {
        throw StateError('Recording intent may not be replaced');
      }
      if (!await write(key(intent.owner, intent.cid), snapshot)) {
        throw StateError('Recording receipt was not persisted');
      }
    });
  }
}
