import 'dart:convert';
import 'dart:io';
import 'fictional_transcript_run.dart';

/// App-private, append-only test receipts. Existing/partial attempt markers block
/// a later send. No reset/delete route. Directory crash persistence is not proved.
class FictionalAttemptStore {
  FictionalAttemptStore(this.directory);
  final Directory directory;
  Future<void> reserve(String operation, FictionalTranscriptPlan binding) async {
    if (!{'prepare','complete'}.contains(operation)) throw StateError('Unknown operation');
    await directory.create(recursive:true);
    final marker=File('${directory.path}/${binding.ownerHash}-${binding.requestId}-$operation.json');
    await marker.create(exclusive:true);
    await marker.writeAsString(jsonEncode({'operation':operation,'request_id':binding.requestId,
      'source_id':binding.sourceId,'owner_sha256':binding.ownerHash,'text_sha256':binding.textHash,
      'text':binding.text,'created_at_utc':DateTime.now().toUtc().toIso8601String()}),flush:true);
  }
  Future<void> record(FictionalTranscriptPlan plan, Map<String,Object?> receipt) async {
    final id=DateTime.now().microsecondsSinceEpoch;
    final file=File('${directory.path}/${plan.ownerHash}-${plan.requestId}-receipt-$id.json');
    await file.create(exclusive:true);
    await file.writeAsString(jsonEncode(receipt),flush:true);
  }
}
