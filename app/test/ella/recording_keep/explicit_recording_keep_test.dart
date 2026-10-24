import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/recording_keep/explicit_recording_keep.dart';

const cid = '11111111-1111-4111-8111-111111111111';
const operation = '22222222-2222-4222-8222-222222222222';
const firstOperation = '33333333-3333-4333-8333-333333333333';
const owner = KeepOwner('owner', 'consent-receipt', 'current-epoch');
final hashA = List.filled(64, 'a').join();
final hashB = List.filled(64, 'b').join();
final hashC = List.filled(64, 'c').join();

Json preconditions() => {
  'expected_source_snapshot_sha256': hashA,
  'expected_transcript_sha256': hashB,
  'expected_active_summary_version_id': null,
};
Json receipt({String op = operation}) => {
  'contract': keepContract, 'conversation_id': cid, 'operation_id': op,
  'request_sha256': hashC, 'source_snapshot_sha256': hashA,
  'transcript_sha256': hashB, 'original_discard_reason': 'discarded_trivial',
  'kept_at': '2026-10-21T05:00:00Z', 'summary_status': 'not_generated',
  'provider_dispatched': false, 'canonical_write': false,
};
Json read({bool kept = false, String processing = 'not_requested',
    String op = operation}) => {
  'contract': keepContract, 'conversation_id': cid,
  'preconditions': preconditions(), 'state': kept ? 'kept' : 'not_kept',
  'receipt': kept ? receipt(op: op) : null,
  'summary_status': 'not_generated', 'processing_state': processing,
  'provider_dispatched': false, 'canonical_write': false,
};
Json saved({bool created = true, String op = operation}) => {
  ...read(kept: true, op: op), 'created': created, 'already_kept': !created,
  'requested_operation_id': operation,
};

class Fake {
  bool current = true;
  int persisted = 0;
  final requests = <KeepRequest>[];
  final durable = <String>[];
  late KeepIntent intent = KeepIntent(owner, cid, operation);
  Future<KeepResponse> Function(KeepRequest)? answer;
  late KeepSession session = KeepSession(intent, (request) async {
    requests.add(request);
    return answer == null
        ? KeepResponse(200, request.method == 'POST' ? saved() : read())
        : await answer!(request);
  }, (value) async {
    expect(value.operationId, operation);
    persisted++;
    durable.add(value.operationId);
  }, (expected) => current && identical(expected, owner));
}

void main() {
  test('created retains one UUID before POST and echoes server hashes only', () async {
    final fake = Fake();
    fake.answer = (request) async {
      if (request.method == 'POST') expect(fake.persisted, 1);
      return KeepResponse(200, request.method == 'POST' ? saved() : read());
    };
    await fake.session.save();
    final post = fake.requests.last;
    expect(post.body, {'contract': keepContract, 'operation_id': operation,
      ...preconditions()});
    expect(fake.durable, [operation, operation]);
    expect(fake.intent.firstReceipt!.operationId, operation);
    expect(fake.session.outcome, KeepOutcome.saved);
    expect(fake.session.view!.title, 'Saved recording');
    expect(fake.session.view!.summaryLabel, 'Summary not generated');
    expect(fake.session.view!.processingLabel, isNull);
    for (final request in fake.requests) {
      expect(request.retries, 0);
      expect(request.retryOnUnauthorized, isFalse);
      expect(identical(request.owner, owner), isTrue);
      expect(request.path, '/v1/conversations/$cid/keep');
    }
  });
  test('already kept returns immutable first receipt, not current intent ID', () async {
    final fake = Fake()..answer = (request) async => KeepResponse(200,
      request.method == 'POST' ? saved(created: false, op: firstOperation) : read());
    await fake.session.save();
    expect(fake.session.view!.receipt!.operationId, firstOperation);
  });
  test('GET unauthorized, source rejection or unavailable never becomes empty/saved', () async {
    for (final status in [401, 409, 503]) {
      final fake = Fake()..answer = (_) async => KeepResponse(status, {});
      await expectLater(fake.session.save(), throwsStateError);
      expect(fake.requests.map((r) => r.method), ['GET']);
      expect(fake.session.view, isNull);
      expect(fake.session.outcome, isNot(KeepOutcome.saved));
    }
  });
  test('GET kept avoids POST entirely', () async {
    final fake = Fake()..answer = (_) async => KeepResponse(200,
      read(kept: true, op: firstOperation));
    await fake.session.save();
    expect(fake.requests.map((r) => r.method), ['GET']);
    expect(fake.persisted, 1);
    expect(fake.session.outcome, KeepOutcome.saved);
  });
  test('paused wording requires actual paused response', () {
    expect(KeepView.fromJson(read(kept: true, processing: 'paused'), cid)
        .processingLabel, 'Processing paused');
    expect(KeepView.fromJson(read(kept: true), cid).processingLabel, isNull);
  });
  test('wrong current owner or receipt blocks before transport', () async {
    final fake = Fake()..current = false;
    await expectLater(fake.session.save(), throwsStateError);
    expect(fake.requests, isEmpty);
  });
  test('owner switch after GET prevents POST', () async {
    final fake = Fake();
    fake.answer = (_) async { fake.current = false; return KeepResponse(200, read()); };
    await expectLater(fake.session.save(), throwsStateError);
    expect(fake.requests.map((r) => r.method), ['GET']);
    expect(fake.session.view, isNull);
  });
  test('401 is single POST without auth retry or new nonce', () async {
    final fake = Fake()..answer = (request) async => KeepResponse(
      request.method == 'POST' ? 401 : 200, read());
    await fake.session.save();
    expect(fake.session.outcome, KeepOutcome.blocked);
    await fake.session.save();
    expect(fake.requests.where((r) => r.method == 'POST').length, 1);
    expect(fake.durable, [operation]);
  });
  test('unknown write reconciles only by GET; absence never permits retry', () async {
    final fake = Fake()..answer = (request) async {
      if (request.method == 'POST') throw TimeoutException('unknown commit');
      return KeepResponse(200, read());
    };
    await fake.session.save();
    await fake.session.reconcile();
    await fake.session.save();
    expect(fake.session.outcome, KeepOutcome.unknownWrite);
    expect(fake.requests.map((r) => r.method), ['GET', 'POST', 'GET']);
  });
  test('unknown write GET marker can confirm kept without POST replay', () async {
    final fake = Fake();
    fake.answer = (request) async {
      if (request.method == 'POST') throw TimeoutException('unknown commit');
      return KeepResponse(200, read(kept: fake.requests.length > 2));
    };
    await fake.session.save();
    await fake.session.reconcile();
    expect(fake.session.outcome, KeepOutcome.saved);
    expect(fake.requests.where((r) => r.method == 'POST').length, 1);
  });
  test('restored attempted intent cannot dispatch another POST', () async {
    final fake = Fake()..intent = KeepIntent(owner, cid, operation,
      postAttempted: true, preconditions: KeepPreconditions.fromJson(preconditions()));
    await fake.session.save();
    expect(fake.requests, isEmpty);
    expect(fake.session.outcome, KeepOutcome.unknownWrite);
  });
  test('concurrent Save is rejected rather than dispatching twice', () async {
    final fake = Fake();
    final wait = Completer<KeepResponse>();
    fake.answer = (_) => wait.future;
    final first = fake.session.save();
    await expectLater(fake.session.save(), throwsStateError);
    wait.complete(KeepResponse(200, read(kept: true)));
    await first.timeout(const Duration(seconds: 2));
    expect(fake.requests.length, 1);
  });
  test('foreign CID, malformed hash and model claims reject', () {
    expect(() => KeepView.fromJson({...read(), 'conversation_id': operation}, cid),
      throwsFormatException);
    expect(() => KeepView.fromJson({...read(), 'preconditions': {
      ...preconditions(), 'expected_transcript_sha256': 'flat-text-hash'}}, cid),
      throwsFormatException);
    expect(() => KeepView.fromJson({...read(), 'provider_dispatched': true}, cid),
      throwsFormatException);
  });
  test('source drift on unknown-write reconciliation stays closed', () async {
    final fake = Fake();
    await fake.session.save();
    fake.answer = (_) async => KeepResponse(200, {...read(kept: true),
      'preconditions': {...preconditions(), 'expected_transcript_sha256': hashC}});
    await expectLater(fake.session.reconcile(), throwsFormatException);
  });
  test('first committed receipt cannot be replaced on later reads', () async {
    final fake = Fake();
    await fake.session.save();
    fake.answer = (_) async => KeepResponse(200, read(kept: true, op: firstOperation));
    await expectLater(fake.session.reconcile(), throwsFormatException);
    expect(fake.intent.firstReceipt!.operationId, operation);
    expect(fake.session.view, isNull);
  });
  test('persistence failure prevents POST and consumes no new UUID', () async {
    final requests = <KeepRequest>[];
    final intent = KeepIntent(owner, cid, operation);
    final session = KeepSession(intent, (request) async {
      requests.add(request);
      return KeepResponse(200, read());
    }, (_) async { throw StateError('durable store unavailable'); }, (_) => true);
    await expectLater(session.save(), throwsStateError);
    expect(requests.map((r) => r.method), ['GET']);
    expect(intent.operationId, operation);
    await session.save();
    expect(requests.length, 1);
  });
  test('owner loss after POST clears display and does not replay', () async {
    final fake = Fake();
    fake.answer = (request) async {
      if (request.method == 'POST') fake.current = false;
      return KeepResponse(200, request.method == 'POST' ? saved() : read());
    };
    await fake.session.save();
    expect(fake.session.outcome, KeepOutcome.blocked);
    expect(fake.session.view, isNull);
    expect(fake.requests.where((r) => r.method == 'POST').length, 1);
  });
  test('restored first receipt rejects replacement on reconciliation', () async {
    final fake = Fake()..intent = KeepIntent(owner, cid, operation,
      postAttempted: true, firstReceipt: KeepReceipt.fromJson(receipt(), cid));
    fake.answer = (_) async => KeepResponse(200, read(kept: true, op: firstOperation));
    await expectLater(fake.session.reconcile(), throwsFormatException);
    expect(fake.intent.firstReceipt!.operationId, operation);
  });
  test('bounded Journal index dedupes CID and remains a current-read hint', () {
    final index = {'contract': keepContract, 'items': [
      {'conversation_id': cid, 'receipt': receipt(), 'needs_current_read': true}],
      'limit': 20, 'more_available': true, 'bounded_recent_index': true,
      'provider_dispatched': false, 'canonical_write': false};
    expect(mergeKeepIndex([cid], index), [cid]);
    expect(() => mergeKeepIndex([], {...index, 'limit': 21}), throwsFormatException);
    expect(() => mergeKeepIndex([], {...index, 'items': [
      {'conversation_id': cid, 'receipt': receipt(), 'needs_current_read': false}]}),
      throwsFormatException);
  });
}
