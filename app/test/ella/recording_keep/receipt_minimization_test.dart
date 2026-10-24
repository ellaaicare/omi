import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/recording_keep/explicit_recording_keep.dart';
import 'explicit_recording_keep_test.dart' as f;
import 'keep_intent_store_test.dart' as storage;

void main() {
  test('extra content and unknown reason cannot reach durable receipt storage', () async {
    for (final untrusted in [
      {...f.receipt(), 'transcript': 'PRIVATE_FIXTURE_TEXT'},
      {...f.receipt(), 'token': 'PRIVATE_FIXTURE_TOKEN'},
      {...f.receipt(), 'original_discard_reason': 'PRIVATE_FIXTURE_TEXT'},
    ]) {
      expect(() => KeepReceipt.fromJson(untrusted, f.cid), throwsFormatException);
      final disk = storage.Disk();
      final intent = await disk.store.openOrCreate(f.owner, f.cid);
      final session = KeepSession(intent, (request) async => KeepResponse(200,
        request.method == 'POST' ? {...f.saved(), 'receipt': untrusted} : f.read()),
        disk.store.persist, (_) => true);
      await session.save();
      expect(session.outcome, KeepOutcome.unknownWrite);
      expect(intent.firstReceipt, isNull);
      expect(disk.values.values.single, isNot(contains('PRIVATE_FIXTURE')));
      expect((await disk.store.load(f.owner, f.cid))!.firstReceipt, isNull);
    }
  });
}
