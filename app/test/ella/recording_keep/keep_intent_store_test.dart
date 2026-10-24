import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/recording_keep/explicit_recording_keep.dart';
import 'package:omi/ella/recording_keep/keep_intent_store.dart';
import 'explicit_recording_keep_test.dart' as f;

class Disk {
  final values = <String, String>{};
  int ids = 0;
  bool acknowledge = true;
  late final store = KeepIntentStore((key) async => values[key], (key, value) async {
    if (!acknowledge) return false;
    values[key] = value;
    return true;
  }, () { ids++; return f.operation; });
}

void main() {
  test('load is read-only; concurrent deliberate open creates one durable UUID', () async {
    final disk = Disk();
    expect(await disk.store.load(f.owner, f.cid), isNull);
    expect(disk.ids, 0);
    final intents = await Future.wait([
      disk.store.openOrCreate(f.owner, f.cid), disk.store.openOrCreate(f.owner, f.cid),
    ]);
    expect(disk.ids, 1);
    expect(intents.map((i) => i.operationId).toSet(), {f.operation});
    expect(disk.values.length, 1);
  });

  test('unacknowledged creation cannot dispatch a write', () async {
    final disk = Disk()..acknowledge = false;
    await expectLater(disk.store.openOrCreate(f.owner, f.cid), throwsStateError);
    expect(disk.values, isEmpty);
  });

  test('attempted snapshot survives new store and cannot become unattempted', () async {
    final disk = Disk();
    final intent = await disk.store.openOrCreate(f.owner, f.cid);
    intent.preconditions = KeepPreconditions.fromJson(f.preconditions());
    intent.postAttempted = true;
    await disk.store.persist(intent);
    final recovered = await disk.store.load(f.owner, f.cid);
    expect(recovered!.postAttempted, isTrue);
    expect(recovered.operationId, f.operation);
    recovered.postAttempted = false;
    await expectLater(disk.store.persist(recovered), throwsStateError);
  });

  test('queued snapshot has immutable bytes independent of later object mutation', () async {
    final disk = Disk();
    final intent = await disk.store.openOrCreate(f.owner, f.cid);
    intent.preconditions = KeepPreconditions.fromJson(f.preconditions());
    intent.postAttempted = true;
    final writing = disk.store.persist(intent);
    intent.postAttempted = false;
    await writing;
    expect((await disk.store.load(f.owner, f.cid))!.postAttempted, isTrue);
  });

  test('new authority on same account and CID cannot replace existing intent', () async {
    final disk = Disk();
    await disk.store.openOrCreate(f.owner, f.cid);
    const changed = KeepOwner('owner', 'different-receipt', 'different-epoch');
    await expectLater(disk.store.openOrCreate(changed, f.cid), throwsStateError);
    expect(disk.ids, 1);
  });

  test('first receipt survives restart and cannot be replaced', () async {
    final disk = Disk();
    final intent = await disk.store.openOrCreate(f.owner, f.cid);
    intent.preconditions = KeepPreconditions.fromJson(f.preconditions());
    intent.postAttempted = true;
    intent.firstReceipt = KeepReceipt.fromJson(f.receipt(), f.cid);
    await disk.store.persist(intent);
    final loaded = await disk.store.load(f.owner, f.cid);
    expect(loaded!.firstReceipt!.operationId, f.operation);
    loaded.firstReceipt = KeepReceipt.fromJson(f.receipt(op: f.firstOperation), f.cid);
    await expectLater(disk.store.persist(loaded), throwsStateError);
  });

  test('invalid attempted snapshot without preconditions fails closed', () async {
    final disk = Disk();
    final intent = await disk.store.openOrCreate(f.owner, f.cid);
    final raw = jsonDecode(disk.store.encode(intent)) as Map<String, dynamic>;
    raw['attempted'] = true;
    disk.values[disk.store.key(f.owner, f.cid)] = jsonEncode(raw);
    await expectLater(disk.store.load(f.owner, f.cid), throwsFormatException);
  });

  test('persistence failure prevents POST and restored attempted flag stays conservative', () async {
    final disk = Disk();
    final intent = await disk.store.openOrCreate(f.owner, f.cid);
    final sent = <String>[];
    final session = KeepSession(intent, (request) async {
      sent.add(request.method);
      return KeepResponse(200, f.read());
    }, disk.store.persist, (_) => true);
    disk.acknowledge = false;
    await expectLater(session.save(), throwsStateError);
    expect(sent, ['GET']);
    expect(intent.postAttempted, isTrue);
  });
}
