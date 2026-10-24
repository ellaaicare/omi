import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:uuid/uuid.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

import 'explicit_recording_keep.dart';
import 'keep_http_transport.dart';
import 'keep_intent_store.dart';

bool recordingCanBeKept(ServerConversation value) =>
    !value.deleted && value.status == ConversationStatus.completed &&
    value.discarded && value.transcriptSegments.any((s) => s.text.trim().isNotEmpty) &&
    value.structured.overview.trim().isEmpty && value.activeSummaryVersionId == null &&
    (value.summaryVersions == null ||
        (value.summaryVersions is List && (value.summaryVersions as List).isEmpty)) &&
    const {ConversationSource.friend, ConversationSource.omi,
      ConversationSource.fieldy, ConversationSource.phone, ConversationSource.sdcard}
        .contains(value.source);

bool sameKeepReceipt(KeepReceipt a, KeepReceipt b) =>
    a.raw.length == b.raw.length &&
    a.raw.entries.every((e) => b.raw[e.key] == e.value);

class KeptRecordingHint {
  KeptRecordingHint(this.cid, this.receipt);
  final String cid;
  final KeepReceipt receipt;
}

class KeepDetail {
  KeepDetail(this.conversation, this.view);
  final ServerConversation conversation;
  final KeepView view;
}

typedef RecordingRead = Future<dynamic> Function(String path);

/// Shared local store: acknowledge a full snapshot before any POST. Keys are
/// hashed account/CID; values contain identity/receipt metadata, never content.
final _intentStore = KeepIntentStore((key) async {
  final value = SharedPreferencesUtil().getString(
      'ella.recording_keep.v1.${sha256.convert(utf8.encode(key))}');
  return value.isEmpty ? null : value;
}, (key, value) => SharedPreferencesUtil().saveString(
    'ella.recording_keep.v1.${sha256.convert(utf8.encode(key))}', value),
    () => const Uuid().v4());

// Same-CID open views share the same in-flight session while authority is valid.
final _sessions = <String, KeepSession>{};

class RecordingKeepRepository {
  RecordingKeepRepository(this.authority, {KeepIntentStore? store,
      KeepTransport? transport, RecordingRead? read})
      : owner = KeepOwner(authority.uid, authority.consent.receiptId,
            authority.owner.authorityFingerprint),
        store = store ?? _intentStore {
    this.transport = transport ?? KeepHttpTransport(authority: authority, owner: owner).call;
    this.read = read ?? _httpRead;
  }

  final ActiveWalAuthority authority;
  final KeepOwner owner;
  final KeepIntentStore store;
  late final KeepTransport transport;
  late final RecordingRead read;
  bool _retired = false;

  bool get current {
    if (!authority.isExactCurrent()) _retired = true;
    return !_retired;
  }

  void _fence() {
    if (!current) throw ExactAccountAuthorityChangedException('Recording view retired');
  }

  Future<dynamic> _httpRead(String path) async {
    _fence();
    final isIndex = path == '/v1/users/saved-recordings?limit=20';
    final isRecent = path == '/v1/conversations?include_discarded=true&limit=20&offset=0&statuses=completed';
    final isDetail = RegExp(r'^/v1/conversations/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$').hasMatch(path);
    if (!isIndex && !isRecent && !isDetail) throw StateError('Unsupported recording read');
    final base = Env.apiBaseUrl;
    if (base == null || !base.endsWith('/')) throw StateError('Recording service unavailable');
    final response = await makeApiCall(url: '$base${path.substring(1)}',
      headers: const {}, body: '', method: 'GET',
      timeout: const Duration(seconds: 10), retries: 0,
      retryOnUnauthorized: false, enforceAbsoluteTimeout: true,
      requireAuthCheck: true, expectedAuthenticatedUid: owner.uid, exactAuthority: authority);
    _fence();
    if (response == null || response.statusCode != 200) {
      throw StateError('Recording read unavailable');
    }
    if (response.bodyBytes.length > 2097152) throw FormatException('Recording read exceeds bound');
    return jsonDecode(response.body);
  }

  Future<List<ServerConversation>> recent() async {
    _fence();
    final raw = await read('/v1/conversations?include_discarded=true&limit=20&offset=0&statuses=completed');
    _fence();
    if (raw is! List || raw.length > 20) throw FormatException('Invalid recent recording page');
    final rows = raw.map((r) => ServerConversation.fromJson(Map<String, dynamic>.from(r))).toList();
    final seen = <String>{};
    return rows.where((r) => recordingCanBeKept(r) && seen.add(r.id)).toList();
  }

  Future<List<KeptRecordingHint>> saved() async {
    _fence();
    final raw = await read('/v1/users/saved-recordings?limit=20');
    _fence();
    final json = Map<String, dynamic>.from(raw);
    mergeKeepIndex(const [], json); // Validate closed schema/receipts/bounds.
    final seen = <String>{};
    final rows = <KeptRecordingHint>[];
    for (final item in json['items'] as List) {
      final row = Map<String, dynamic>.from(item);
      final cid = row['conversation_id'] as String;
      if (seen.add(cid)) rows.add(KeptRecordingHint(cid,
          KeepReceipt.fromJson(Map<String, dynamic>.from(row['receipt']), cid)));
    }
    return rows;
  }

  Future<KeepView> _view(String cid) async {
    _fence();
    final response = await transport(KeepRequest('GET', '/v1/conversations/$cid/keep', owner));
    _fence();
    if (response.status != 200) throw StateError('Recording retention unavailable');
    return KeepView.fromJson(response.json, cid);
  }

  Future<KeepDetail> detail(String cid, {KeepReceipt? expectedReceipt}) async {
    // A saved index is a hint, never content authority. Bracket detail with
    // current server source/receipt checks; reject drift or unavailable reads.
    final before = await _view(cid);
    final raw = await read('/v1/conversations/$cid');
    _fence();
    final value = ServerConversation.fromJson(Map<String, dynamic>.from(raw));
    if (value.id != cid || !recordingCanBeKept(value)) {
      throw StateError('Recording detail no longer eligible');
    }
    final after = await _view(cid);
    if (!before.preconditions.same(after.preconditions) ||
        before.kept != after.kept ||
        (before.receipt != null &&
            (after.receipt == null || !sameKeepReceipt(before.receipt!, after.receipt!))) ||
        (expectedReceipt != null &&
            (after.receipt == null || !sameKeepReceipt(expectedReceipt, after.receipt!)))) {
      throw StateError('Recording changed during read');
    }
    return KeepDetail(value, after);
  }

  String _sessionKey(String cid) => jsonEncode([owner.uid, cid, owner.epoch]);

  Future<KeepSession?> restore(String cid) async {
    _fence();
    final key = _sessionKey(cid);
    final cached = _sessions[key];
    if (cached != null && cached.isCurrent(cached.intent.owner)) return cached;
    final intent = await store.load(owner, cid);
    _fence();
    if (intent == null) return null;
    return _sessions[key] = KeepSession(intent, transport, store.persist,
        (_) => current && store.sameOwner(intent.owner, owner));
  }

  Future<KeepSession> explicitIntent(String cid) async {
    final existing = await restore(cid);
    if (existing != null) return existing;
    final intent = await store.openOrCreate(owner, cid);
    _fence();
    // Another view may have completed restore while durable creation awaited.
    final cached = _sessions[_sessionKey(cid)];
    if (cached != null && cached.isCurrent(cached.intent.owner)) return cached;
    return _sessions[_sessionKey(cid)] = KeepSession(intent, transport, store.persist,
        (_) => current && store.sameOwner(intent.owner, owner));
  }
}
