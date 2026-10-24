import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/backend/schema/transcript_segment.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/services/wals/wal.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/ella/recording_keep/explicit_recording_keep.dart';
import 'package:omi/ella/recording_keep/keep_intent_store.dart';
import 'package:omi/ella/recording_keep/recording_keep_repository.dart';
import 'explicit_recording_keep_test.dart' as f;

int _sequence = 0;
class RepositoryFixture {
  RepositoryFixture() {
    final uid = 'keep-owner-${++_sequence}';
    authority = ActiveWalAuthority(
      owner: WalOwner(uid: uid, profileBindingId: 'profile', bindingRevision: 1,
        consentReceiptId: 'receipt', authorityGenerationAtCapture: 1),
      consent: AiConsentAuthoritySnapshot(generation: 1,
        terminalAccountConsentGeneration: 1, uid: uid, verifiedPersonaId: 'persona',
        profileBindingId: 'profile', receiptId: 'receipt', policyVersion: 'v11',
        processorSetHash: 'processors', scopeVersion: '1', scopeHash: 'scope'),
      currentCheck: () => current,
    );
    repo = RecordingKeepRepository(authority, store: store, read: (path) async {
      reads.add(path);
      if (answerRead != null) return answerRead!(path);
      if (path.contains('?include_discarded')) return [wireRecording()];
      if (path.contains('saved-recordings')) return index();
      return wireRecording();
    }, transport: (request) async {
      requests.add(request);
      if (answerKeep != null) return answerKeep!(request);
      return KeepResponse(200, request.method == 'POST' ? f.saved() : f.read());
    });
  }
  bool current = true;
  int minted = 0;
  final disk = <String, String>{};
  final reads = <String>[];
  final requests = <KeepRequest>[];
  late ActiveWalAuthority authority;
  late RecordingKeepRepository repo;
  Future<dynamic> Function(String)? answerRead;
  Future<KeepResponse> Function(KeepRequest)? answerKeep;
  late final store = KeepIntentStore((key) async => disk[key], (key, value) async {
    disk[key] = value; return true;
  }, () { minted++; return f.operation; });
}

ServerConversation recording({String text = 'Fictional test words',
    ConversationSource source = ConversationSource.fieldy, bool discarded = true,
    String overview = '', String? version, bool deleted = false,
    ConversationStatus status = ConversationStatus.completed}) => ServerConversation(
  id: f.cid, createdAt: DateTime.utc(2026, 10, 21),
  structured: Structured('', overview), discarded: discarded,
  source: source, activeSummaryVersionId: version, deleted: deleted, status: status,
  transcriptSegments: [TranscriptSegment(id: 'segment', text: text,
    speaker: null, isUser: false, personId: null, start: 0, end: 1, translations: [])],
);

// Server wire values are bare enum names. ServerConversation.toJson() is a
// local serialization and emits ConversationSource.fieldy, not HTTP wire fieldy.
Map<String, dynamic> wireRecording() => {
  ...recording().toJson(), 'source': 'fieldy',
};

Map<String, dynamic> index() => {
  'contract': keepContract, 'bounded_recent_index': true, 'provider_dispatched': false,
  'canonical_write': false, 'more_available': false, 'limit': 20,
  'items': [{'conversation_id': f.cid, 'needs_current_read': true, 'receipt': f.receipt()}],
};

void main() {
  test('only nonempty native completed discarded input without summary is eligible', () {
    expect(recordingCanBeKept(recording()), isTrue);
    for (final value in [recording(text: '  '), recording(discarded: false),
      recording(overview: 'A summary'), recording(version: 'active'),
      recording(deleted: true), recording(source: ConversationSource.workflow),
      recording(status: ConversationStatus.failed)]) {
      expect(recordingCanBeKept(value), isFalse);
    }
  });

  test('recent and bounded saved index are read-only and remain separate from normal filters', () async {
    final fixture = RepositoryFixture();
    expect((await fixture.repo.recent()).single.id, f.cid);
    expect((await fixture.repo.saved()).single.cid, f.cid);
    expect(fixture.requests, isEmpty);
    expect(fixture.minted, 0);
    expect(fixture.disk, isEmpty);
  });

  test('detail brackets content with matching source checks, no operation created', () async {
    final fixture = RepositoryFixture();
    final detail = await fixture.repo.detail(f.cid);
    expect(detail.conversation.transcriptSegments.single.text, 'Fictional test words');
    expect(fixture.requests.map((r) => r.method), ['GET', 'GET']);
    expect(fixture.minted, 0);
    expect(await fixture.repo.restore(f.cid), isNull);
  });

  test('changed source between detail checks rejects retained content projection', () async {
    final fixture = RepositoryFixture();
    fixture.answerKeep = (_) async {
      final body = f.read();
      if (fixture.requests.length == 2) {
        body['preconditions'] = {...f.preconditions(), 'expected_transcript_sha256': f.hashC};
      }
      return KeepResponse(200, body);
    };
    await expectLater(fixture.repo.detail(f.cid), throwsStateError);
    expect(fixture.requests.every((r) => r.method == 'GET'), isTrue);
    expect(fixture.requests.length, 2);
  });

  test('saved hint with a different first receipt cannot authorize content', () async {
    final fixture = RepositoryFixture();
    fixture.answerKeep = (_) async => KeepResponse(200,
        f.read(kept: true, op: f.firstOperation));
    await expectLater(fixture.repo.detail(f.cid,
        expectedReceipt: KeepReceipt.fromJson(f.receipt(), f.cid)), throwsStateError);
    expect(fixture.requests.length, 2);
  });

  test('authority change after read retires repository permanently', () async {
    final fixture = RepositoryFixture();
    fixture.answerRead = (_) async { fixture.current = false; return [wireRecording()]; };
    await expectLater(fixture.repo.recent(), throwsStateError);
    fixture.current = true;
    expect(fixture.repo.current, isFalse);
    await expectLater(fixture.repo.saved(), throwsStateError);
  });

  test('two same-CID deliberate views share one intent and one write', () async {
    final fixture = RepositoryFixture();
    final views = await Future.wait([
      fixture.repo.explicitIntent(f.cid), fixture.repo.explicitIntent(f.cid),
    ]);
    expect(identical(views[0], views[1]), isTrue);
    expect(fixture.minted, 1);
    await views.first.save();
    expect(fixture.requests.map((r) => r.method), ['GET', 'POST']);
    expect((await fixture.store.load(fixture.repo.owner, f.cid))!.postAttempted, isTrue);
    await views.last.save(); // already attempted: cannot POST again.
    expect(fixture.requests.where((r) => r.method == 'POST').length, 1);
  });

  test('unavailable index is an error, never synthetic empty success', () async {
    final fixture = RepositoryFixture();
    fixture.answerRead = (_) async => throw StateError('fixture unavailable');
    await expectLater(fixture.repo.saved(), throwsStateError);
    expect(fixture.minted, 0);
  });
}
