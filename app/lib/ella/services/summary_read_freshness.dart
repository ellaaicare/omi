import 'dart:convert';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/services/journal_preview_refresh.dart' show journalPreviewVersionMayAdvance;

enum SummaryReadPhase { unchecked, checking, checked, stale }

/// App read evidence, not enrichment/model-execution evidence. Version IDs are
/// opaque: only the current owner-bound response can select canonical content.
class SummaryReadObservation {
  const SummaryReadObservation(this.phase, {this.checkedAt, this.versionId, this.bodyWitness});
  final SummaryReadPhase phase;
  final DateTime? checkedAt;
  final String? versionId;
  final String? bodyWitness;

  SummaryReadObservation forConversation(ServerConversation conversation) => phase == SummaryReadPhase.checked &&
          (versionId != conversation.activeSummaryVersionId || bodyWitness != summaryReadBodyWitness(conversation))
      ? const SummaryReadObservation(SummaryReadPhase.unchecked)
      : this;
}

// Memory-only equality witness. Never logged, persisted or sent.
String summaryReadBodyWitness(ServerConversation row) =>
    jsonEncode([row.id, row.summaryVersionActiveAuthority, row.structured.toJson(), row.enrichmentState]);

bool summaryResponseMatchesOwnerAndVersion(ServerConversation row, String uid, {ServerConversation? previous}) {
  if (uid.isEmpty || row.id.trim().isEmpty || (previous != null && previous.id != row.id)) return false;
  final before = previous?.activeSummaryVersionId;
  final after = row.activeSummaryVersionId;
  if (before != null && before.isNotEmpty) {
    if (before != after) {
      if (!journalPreviewVersionMayAdvance(previous!, row)) return false;
    } else if (previous!.structured.title != row.structured.title ||
        previous.structured.overview != row.structured.overview) {
      // A known canonical version is immutable. A changed body under the same
      // ID is not evidence that this is a newer authoritative summary.
      return false;
    }
  }
  if (row.summaryVersionBodyUidPresent && row.summaryVersionBodyUid != uid) return false;
  final rawActive = row.summaryVersionActiveAuthority;
  if (rawActive != null && (rawActive is! String || rawActive.trim().isEmpty)) return false;
  // Some list responses legitimately omit historical versions. Do not invent a
  // version or infer model completion from that absence.
  final rawVersions = row.summaryVersions;
  if (rawVersions == null) return true;
  // The server model explicitly uses []/null for processing, discarded and
  // other genuinely unversioned records. This is read compatibility only;
  // dropping a previously known version/history never becomes a valid downgrade.
  if (rawVersions is List && rawVersions.isEmpty && rawActive == null) {
    return after == null &&
        (previous == null ||
            previous.activeSummaryVersionId == null &&
                (previous.summaryVersions == null ||
                    previous.summaryVersions is List && (previous.summaryVersions as List).isEmpty));
  }
  if (rawVersions is! List || rawActive is! String) return false;
  final ids = <String>{};
  for (final item in rawVersions) {
    if (item is! Map ||
        item['id'] is! String ||
        (item['id'] as String).isEmpty ||
        !ids.add(item['id'] as String) ||
        item['is_active'] is! bool ||
        (item['conversation_id'] != null && item['conversation_id'] != row.id)) return false;
  }
  final active = rawVersions.whereType<Map>().where((v) => v['is_active'] == true).toList();
  if (active.length != 1 || active.single['id'] != rawActive) return false;
  final selected = active.single;
  return selected['title'] == row.structured.title && selected['overview'] == row.structured.overview;
}

/// Per-record validation allows a good exact-owner row to refresh even when a
/// sibling is invalid. Duplicate IDs are all rejected; no list ordering chooses
/// which contradictory body is authoritative. Rejected prior rows stay stale.
class SummaryResponseBatch {
  SummaryResponseBatch(this.accepted, this.rejectedIds);
  final List<ServerConversation> accepted;
  final Set<String> rejectedIds;
  bool get hasRejected => rejectedIds.isNotEmpty;
}

SummaryResponseBatch validateSummaryResponseBatch(
  Iterable<ServerConversation> incoming,
  String uid, {
  required Iterable<ServerConversation> previous,
}) {
  // Completed and processing projections may temporarily share an ID. Every
  // retained row is a constraint: a stale unversioned processing projection
  // must never erase a known canonical version through last-write-wins lookup.
  final known = <String, List<ServerConversation>>{};
  for (final row in previous) {
    (known[row.id] ??= []).add(row);
  }
  final counts = <String, int>{};
  for (final row in incoming) {
    counts[row.id] = (counts[row.id] ?? 0) + 1;
  }
  final accepted = <ServerConversation>[];
  final rejected = <String>{};
  for (final row in incoming) {
    final priorRows = known[row.id];
    final matchesKnown = priorRows == null
        ? summaryResponseMatchesOwnerAndVersion(row, uid)
        : priorRows.every((prior) => summaryResponseMatchesOwnerAndVersion(row, uid, previous: prior));
    if (counts[row.id] == 1 && matchesKnown) {
      accepted.add(row);
    } else {
      rejected.add(row.id);
    }
  }
  return SummaryResponseBatch(accepted, rejected);
}

/// Only explicit per-record server metadata supports these labels. Neither a
/// generic summary nor a missing field establishes a tenant/policy hold.
String? explicitSummaryProcessingMessage(ServerConversation row) {
  final state = row.enrichmentState;
  final status = state?['canonical_status'] ?? state?['status'];
  if (status == 'held' || status == 'blocked' || status == 'paused') return 'Summary processing is on hold.';
  if (status == 'pending' || status == 'queued') return 'Summary processing is pending.';
  if (status == 'failed' || row.processingRetryEnrichmentVectorStatus == 'failed') {
    return 'Summary processing failed.';
  }
  return null;
}

/// A stock or unknown projection needs later readback, not a promise that an
/// enhancer is authorized/available. Only the backend owns durable retries.
bool summaryNeedsLaterRead(ServerConversation row) {
  if (row.deleted || row.discarded || row.status != ConversationStatus.completed) return false;
  final state = row.enrichmentState;
  final version = row.activeSummaryVersionId;
  return !(version != null &&
      version.isNotEmpty &&
      state?['result_summary_version_id'] == version &&
      state?['status'] == 'writeback_applied' &&
      state?['pending'] == false &&
      state?['canonical_status'] == 'completed' &&
      state?['kind'] == 'hermes_enriched' &&
      state?['source'] == 'hermes_parallel');
}
