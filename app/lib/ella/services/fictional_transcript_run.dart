import 'dart:convert';
import 'package:crypto/crypto.dart';

const fictionalProducerEnabled = bool.fromEnvironment('ELLA_FICTIONAL_TRANSCRIPT_TEST', defaultValue: false);
final _uuid = RegExp(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$');
final _digest = RegExp(r'^[0-9a-f]{64}$');
bool _versionId(Object? value) => value is String && value.trim().isNotEmpty && value.length <= 256;
String fictionalHash(String value) => sha256.convert(utf8.encode(value)).toString();

/// Operator-reviewed fictional bytes only. This is not a credential or a grant.
class FictionalTranscriptPlan {
  FictionalTranscriptPlan.fromJson(Map<String, dynamic> data)
      : requestId = data['request_id'] as String,
        sourceId = data['source_id'] as String,
        ownerHash = data['owner_sha256'] as String,
        textHash = data['text_sha256'] as String,
        text = data['text'] as String,
        expiresAt = DateTime.parse(data['expires_at_utc'] as String) {
    if (data.length != 6 ||
        !data.keys.toSet().containsAll({'request_id', 'source_id', 'owner_sha256', 'text_sha256', 'text', 'expires_at_utc'}) ||
        !_uuid.hasMatch(requestId) || !_uuid.hasMatch(sourceId) ||
        !_digest.hasMatch(ownerHash) || !_digest.hasMatch(textHash) ||
        text.trim().isEmpty || text.runes.length > 12000 || fictionalHash(text) != textHash ||
        !(data['expires_at_utc'] as String).endsWith('Z') ||
        utf8.encode(jsonEncode({'request_id': requestId, 'text': text})).length > 65536) {
      throw const FormatException('Invalid fictional plan');
    }
  }
  final String requestId, sourceId, ownerHash, textHash, text;
  final DateTime expiresAt;
  Map<String, Object> get submission => {'request_id': requestId, 'text': text};
}

class FictionalResponse {
  const FictionalResponse(this.status, this.body);
  final int status;
  final String body;
}

typedef FictionalTransport = Future<FictionalResponse?> Function(String method, String path, String body);
typedef FictionalAttemptJournal = Future<void> Function(String operation, FictionalTranscriptPlan plan);

enum FictionalPhase { idle, preparing, prepared, completing, stockCompleted, unknown, retired }

/// No automatic calls, retry, cancellation claim, model call or recovery-source mutation.
class FictionalTranscriptRun {
  FictionalTranscriptRun({required this.plan, required this.current, required this.transport,
    required this.reserveAttempt, this.recordReceipt = _ignoreReceipt, DateTime Function()? clock, this.enabled = fictionalProducerEnabled})
      : clock = clock ?? DateTime.now;
  final FictionalTranscriptPlan plan;
  final bool Function() current;
  final FictionalTransport transport;
  static Future<void> _ignoreReceipt(Map<String, Object?> receipt) async {}
  final Future<void> Function(Map<String, Object?>) recordReceipt;
  final FictionalAttemptJournal reserveAttempt;
  Future<void> _record(Map<String, Object?> receipt) async {
    await recordReceipt(receipt); _check(); _receipts.add(receipt);
  }
  final DateTime Function() clock;
  final bool enabled;
  FictionalPhase phase = FictionalPhase.idle;
  final List<Map<String, Object?>> _receipts = [];
  bool _busy = false;
  bool _retired = false;
  List<Map<String, Object?>> get receipts => valid ? List.unmodifiable(_receipts) : const [];
  bool get valid => !_retired && enabled && current() && clock().toUtc().isBefore(plan.expiresAt);
  void retire() { _retired = true; phase = FictionalPhase.retired; _receipts.clear(); }
  void _check() { if (!valid) { retire(); throw StateError('Fictional scope retired'); } }
  Map<String, dynamic> _decode(FictionalResponse? response) {
    if (response == null || response.status != 200 || utf8.encode(response.body).length > 65536) {
      throw StateError('Outcome unconfirmed');
    }
    final value = jsonDecode(response.body);
    if (value is! Map<String, dynamic>) throw const FormatException();
    return value;
  }
  Future<void> prepare() async {
    if (_busy || phase != FictionalPhase.idle) return;
    _check(); _busy = true; phase = FictionalPhase.preparing;
    try {
      await reserveAttempt('prepare', plan); _check();
      final body = jsonEncode(plan.submission);
      final response = await transport('POST', 'v1/conversations/test-transcripts/prepare', body); _check();
      final data = _decode(response);
      if (data.length != 4 || data['source_id'] != plan.sourceId || data['state'] != 'prepared' ||
          data['created'] is! bool || data['classification'] != 'backend_synthetic_transcript_not_hardware_capture') {
        throw const FormatException();
      }
      await _record({'operation': 'prepare', 'request_body': plan.submission, 'http_status': 200,
        'response_body': data, 'read_at_utc': clock().toUtc().toIso8601String()});
      phase = FictionalPhase.prepared;
    } catch (_) { if (valid) { phase = FictionalPhase.unknown; } else { retire(); } }
    finally { _busy = false; }
  }
  Future<void> complete() async {
    if (_busy || phase != FictionalPhase.prepared) return;
    _check(); _busy = true; phase = FictionalPhase.completing;
    try {
      // Exclusive durable marker is written BEFORE the sole attempted POST.
      await reserveAttempt('complete', plan); _check();
      final response = await transport('POST', 'v1/conversations/test-transcripts/${plan.requestId}/complete', ''); _check();
      final data = _decode(response);
      if (data.length != 4 || data['source_id'] != plan.sourceId || data['state'] != 'source_completed' ||
          data['hermes_enrichment'] != 'separate_normal_worker_readback_required' ||
          !_versionId(data['observed_active_summary_version_id'])) throw const FormatException();
      await _record({'operation': 'complete', 'request_body': '', 'http_status': 200,
        'response_body': data, 'read_at_utc': clock().toUtc().toIso8601String(),
        'hermes_success': false});
      phase = FictionalPhase.stockCompleted;
    } catch (_) { if (valid) { phase = FictionalPhase.unknown; } else { retire(); } }
    finally { _busy = false; }
  }
  Future<void> reconcile() async {
    if (_busy || !{FictionalPhase.stockCompleted, FictionalPhase.unknown}.contains(phase)) return;
    _check(); _busy = true;
    try {
      final response = await transport('GET', 'v1/conversations/${plan.sourceId}', ''); _check();
      final data = _decode(response);
      if (data['id'] != plan.sourceId || (data.containsKey('uid') && fictionalHash(data['uid'] as String) != plan.ownerHash)) {
        throw const FormatException();
      }
      final segments = data['transcript_segments'];
      if (segments is! List || segments.length != 1 || segments.single is! Map ||
          (segments.single as Map)['text'] != plan.text) throw const FormatException();
      final metadata = data['external_data'];
      final binding = metadata is Map ? metadata['qualification_transcript'] : null;
      if (data['source'] != 'omi' || binding is! Map || binding.length != 6 ||
          binding['contract'] != 'authenticated-test-transcript-v1' ||
          binding['request_id'] != plan.requestId || binding['source_id'] != plan.sourceId ||
          binding['text_sha256'] != plan.textHash || binding['source_kind'] != 'synthetic_text_not_physical_recording' ||
          binding['scope_sha256'] is! String || !_digest.hasMatch(binding['scope_sha256'] as String)) {
        throw const FormatException();
      }
      final version = data['active_summary_version_id'];
      final versions = data['summary_versions'];
      final structured = data['structured'];
      if (!_versionId(version) || versions is! List || structured is! Map) throw const FormatException();
      final matching = versions.where((v) => v is Map && v['id'] == version).toList();
      final active = versions.where((v) => v is Map && v['is_active'] == true).toList();
      if (matching.length != 1 || active.length != 1 || !identical(matching.single, active.single)) {
        throw const FormatException();
      }
      final selected = matching.single as Map;
      final title = selected['title'], overview = selected['overview'];
      final report = selected['source_report'], reportHash = selected['source_report_sha256'];
      if (title is! String || overview is! String || title.length > 512 || overview.length > 32000 ||
          title != structured['title'] || overview != structured['overview'] ||
          selected['source'] is! String || (selected['source'] as String).isEmpty ||
          selected['kind'] is! String || (selected['kind'] as String).isEmpty ||
          selected['created_at'] is! String || DateTime.tryParse(selected['created_at'] as String) == null ||
          (selected['based_on_version_id'] != null && !_versionId(selected['based_on_version_id'])) ||
          (report != null && report is! Map) ||
          (reportHash != null && (reportHash is! String || !_digest.hasMatch(reportHash)))) {
        throw const FormatException();
      }
      await _record({'operation': 'exact_source_get', 'source_id': plan.sourceId,
        'http_status': 200, 'status': {'processing','completed','in_progress'}.contains(data['status']) ? data['status'] : 'unrecognized',
        'active_summary_version_id': version, 'fictional_text_sha256': plan.textHash,
        'current_version': {'id': version, 'is_active': true, 'title': title, 'overview': overview,
          'source': selected['source'], 'kind': selected['kind'], 'created_at': selected['created_at'],
          'based_on_version_id': selected['based_on_version_id'], 'reported_source_report': report,
          'reported_source_report_sha256': reportHash},
        'source_binding_scope_sha256': binding['scope_sha256'],
        'read_at_utc': clock().toUtc().toIso8601String(), 'hermes_success': false,
        'remaining': 'normal worker/current-version evidence required'});
    } catch (_) { if (!valid) retire(); }
    finally { _busy = false; }
  }
}
