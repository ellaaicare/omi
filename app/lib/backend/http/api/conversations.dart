import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;

import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/schema.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/platform/platform_manager.dart';

typedef ConversationDeleteTransport = Future<http.Response?> Function({
  required String url,
  required String? expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier? exactAuthority,
});

typedef ConversationFinalizationTransport = Future<http.Response?> Function({
  required String url,
  required String method,
  required String body,
  required String? expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier? exactAuthority,
});

typedef ConversationFinalizationDelay = Future<void> Function(Duration duration);

Future<http.Response?> _defaultConversationDeleteTransport({
  required String url,
  required String? expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier? exactAuthority,
}) =>
    makeApiCall(
      url: url,
      headers: const {},
      method: 'DELETE',
      body: '',
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      exactAuthority: exactAuthority,
    );

Future<http.Response?> _defaultConversationFinalizationTransport({
  required String url,
  required String method,
  required String body,
  required String? expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier? exactAuthority,
}) =>
    makeApiCall(
      url: url,
      headers: const {},
      method: method,
      body: body,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      exactAuthority: exactAuthority,
    );

Future<void> _defaultConversationFinalizationDelay(Duration duration) => Future<void>.delayed(duration);

void _verifyConversationFinalizationAuthority({
  required String? expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier? exactAuthority,
}) {
  if (exactAuthority == null) return;
  if (!exactAuthority.isExactCurrent() ||
      (expectedAuthenticatedUid != null && exactAuthority.uid != expectedAuthenticatedUid)) {
    throw ExactAccountAuthorityChangedException('Capture finalization authority changed while polling');
  }
}

Future<CreateConversationResponse?> processInProgressConversation({
  required String conversationId,
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
  int maxStatusPollAttempts = 60,
  Duration statusPollInterval = const Duration(seconds: 1),
  ConversationFinalizationTransport transport = _defaultConversationFinalizationTransport,
  ConversationFinalizationDelay delay = _defaultConversationFinalizationDelay,
}) async {
  _verifyConversationFinalizationAuthority(
    expectedAuthenticatedUid: expectedAuthenticatedUid,
    exactAuthority: exactAuthority,
  );
  final response = await transport(
    url: '${Env.apiBaseUrl}v1/conversations',
    method: 'POST',
    body: jsonEncode({'conversation_id': conversationId}),
    expectedAuthenticatedUid: expectedAuthenticatedUid,
    exactAuthority: exactAuthority,
  );
  _verifyConversationFinalizationAuthority(
    expectedAuthenticatedUid: expectedAuthenticatedUid,
    exactAuthority: exactAuthority,
  );
  if (response == null) return null;
  Logger.debug('createConversationServer: ${response.body}');
  if (response.statusCode == 200) {
    final result = CreateConversationResponse.fromJson(jsonDecode(response.body));
    return result.conversation?.id == conversationId ? result : null;
  }
  if (response.statusCode != 409) {
    PlatformManager.instance.crashReporter.reportCrash(
      Exception('Failed to create conversation'),
      StackTrace.current,
      userAttributes: {'status_code': response.statusCode.toString()},
    );
    return null;
  }

  final statusUrl = '${Env.apiBaseUrl}v1/conversations/${Uri.encodeComponent(conversationId)}';
  for (var attempt = 0; attempt < maxStatusPollAttempts; attempt++) {
    if (statusPollInterval > Duration.zero) await delay(statusPollInterval);
    _verifyConversationFinalizationAuthority(
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      exactAuthority: exactAuthority,
    );
    final statusResponse = await transport(
      url: statusUrl,
      method: 'GET',
      body: '',
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      exactAuthority: exactAuthority,
    );
    _verifyConversationFinalizationAuthority(
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      exactAuthority: exactAuthority,
    );
    if (statusResponse == null ||
        statusResponse.statusCode == 404 ||
        statusResponse.statusCode == 409 ||
        statusResponse.statusCode >= 500) {
      continue;
    }
    if (statusResponse.statusCode != 200) return null;

    final conversation = ServerConversation.fromJson(jsonDecode(statusResponse.body));
    if (conversation.id != conversationId) return null;
    if (conversation.status == ConversationStatus.completed) {
      return CreateConversationResponse(messages: const [], conversation: conversation);
    }
    if (conversation.status == ConversationStatus.failed) return null;
  }
  return null;
}

class ConversationsFetchResult {
  const ConversationsFetchResult.success(this.conversations)
      : succeeded = true,
        statusCode = 200;

  const ConversationsFetchResult.failure({this.statusCode})
      : succeeded = false,
        conversations = const [];

  final bool succeeded;
  final int? statusCode;
  final List<ServerConversation> conversations;
}

Future<List<ServerConversation>> getConversations({
  int limit = 50,
  int offset = 0,
  List<ConversationStatus> statuses = const [],
  bool includeDiscarded = true,
  DateTime? startDate,
  DateTime? endDate,
  String? folderId,
  bool? starred,
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
}) async {
  final result = await getConversationsResult(
    limit: limit,
    offset: offset,
    statuses: statuses,
    includeDiscarded: includeDiscarded,
    startDate: startDate,
    endDate: endDate,
    folderId: folderId,
    starred: starred,
    expectedAuthenticatedUid: expectedAuthenticatedUid,
    exactAuthority: exactAuthority,
  );
  return result.conversations;
}

Future<ConversationsFetchResult> getConversationsResult({
  int limit = 50,
  int offset = 0,
  List<ConversationStatus> statuses = const [],
  bool includeDiscarded = true,
  DateTime? startDate,
  DateTime? endDate,
  String? folderId,
  bool? starred,
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
}) async {
  String url =
      '${Env.apiBaseUrl}v1/conversations?include_discarded=$includeDiscarded&limit=$limit&offset=$offset&statuses=${statuses.map((val) => val.toString().split(".").last).join(",")}';

  // Add date filters if provided
  if (startDate != null) {
    url += '&start_date=${startDate.toUtc().toIso8601String()}';
  }
  if (endDate != null) {
    url += '&end_date=${endDate.toUtc().toIso8601String()}';
  }
  if (folderId != null) {
    url += '&folder_id=$folderId';
  }
  if (starred != null) {
    url += '&starred=$starred';
  }

  var response = await makeApiCall(
    url: url,
    headers: {},
    method: 'GET',
    body: '',
    expectedAuthenticatedUid: expectedAuthenticatedUid,
    exactAuthority: exactAuthority,
  );
  if (response == null) return const ConversationsFetchResult.failure();
  if (response.statusCode == 200) {
    try {
      // Decode body bytes explicitly so transcript text is always interpreted as UTF-8.
      final body = utf8.decode(response.bodyBytes);
      final conversations =
          (jsonDecode(body) as List<dynamic>).map((conversation) => ServerConversation.fromJson(conversation)).toList();
      Logger.debug('getConversations length: ${conversations.length}');
      return ConversationsFetchResult.success(conversations);
    } catch (error, stackTrace) {
      Logger.error('getConversations decode error: $error\n$stackTrace');
      return ConversationsFetchResult.failure(statusCode: response.statusCode);
    }
  }
  Logger.debug('getConversations error ${response.statusCode}');
  return ConversationsFetchResult.failure(statusCode: response.statusCode);
}

enum ConversationProcessingRetryOutcome { processing, completed, failed }

enum ConversationProcessingRecoveryMode { none, full, enrichmentOnly }

class ConversationProcessingRetryResult {
  final ConversationProcessingRetryOutcome outcome;
  final ConversationProcessingRecoveryMode recoveryMode;
  final String? phase;
  final String? genericStatus;
  final String? genericVectorStatus;
  final String? enrichmentStatus;
  final String? vectorStatus;
  final DateTime? leaseExpiresAt;
  final int attemptCount;
  final ServerConversation conversation;

  const ConversationProcessingRetryResult({
    required this.outcome,
    this.recoveryMode = ConversationProcessingRecoveryMode.none,
    this.phase,
    this.genericStatus,
    this.genericVectorStatus,
    this.enrichmentStatus,
    this.vectorStatus,
    this.leaseExpiresAt,
    this.attemptCount = 0,
    required this.conversation,
  });

  bool get isTerminal =>
      outcome == ConversationProcessingRetryOutcome.completed || outcome == ConversationProcessingRetryOutcome.failed;

  factory ConversationProcessingRetryResult.fromJson(Map<String, dynamic> json) {
    return ConversationProcessingRetryResult(
      outcome: ConversationProcessingRetryOutcome.values.asNameMap()[json['outcome']] ??
          ConversationProcessingRetryOutcome.failed,
      recoveryMode: json['recovery_mode'] == 'enrichment_only'
          ? ConversationProcessingRecoveryMode.enrichmentOnly
          : ConversationProcessingRecoveryMode.values.asNameMap()[json['recovery_mode']] ??
              ConversationProcessingRecoveryMode.none,
      phase: json['phase'],
      genericStatus: json['generic_status'],
      genericVectorStatus: json['generic_vector_status'],
      enrichmentStatus: json['enrichment_status'],
      vectorStatus: json['vector_status'],
      leaseExpiresAt: json['lease_expires_at'] != null ? DateTime.tryParse(json['lease_expires_at'])?.toLocal() : null,
      attemptCount: (json['attempt_count'] as num?)?.toInt() ?? 0,
      conversation: ServerConversation.fromJson(json['conversation']),
    );
  }
}

Future<ConversationProcessingRetryResult?> retryConversationProcessing(
  String conversationId,
  String requestId, {
  String? correctionText,
}) async {
  if (!SharedPreferencesUtil().aiConsentAccepted) return null;
  final normalizedCorrection = correctionText?.trim();
  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId/processing-retries',
    headers: {},
    method: 'POST',
    body: jsonEncode({
      'request_id': requestId,
      if (normalizedCorrection != null && normalizedCorrection.isNotEmpty) 'correction_text': normalizedCorrection,
    }),
  );
  if (response == null) return null;
  Logger.debug('retryConversationProcessing: ${response.statusCode}');
  if (response.statusCode == 200 || response.statusCode == 202) {
    return ConversationProcessingRetryResult.fromJson(jsonDecode(response.body));
  }
  return null;
}

Future<ServerConversation?> reProcessConversationServer(String conversationId, {String? appId}) async {
  if (!SharedPreferencesUtil().aiConsentAccepted) return null;
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId/reprocess${appId != null ? '?app_id=$appId' : ''}',
    headers: {},
    method: 'POST',
    body: '',
  );
  if (response == null) return null;
  Logger.debug('reProcessConversationServer: ${response.body}');
  if (response.statusCode == 200) {
    return ServerConversation.fromJson(jsonDecode(response.body));
  }
  return null;
}

Future<bool> submitConversationCorrection({
  required String conversationId,
  required String correctionText,
  String? summaryTitle,
  String? summaryOverview,
  String? appSummary,
}) async {
  if (!SharedPreferencesUtil().aiConsentAccepted) return false;
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/ella/conversations/$conversationId/corrections',
    headers: {},
    method: 'POST',
    body: jsonEncode({
      'correction_text': correctionText,
      'source': 'ios',
      'summary_context': {
        if (summaryTitle != null) 'title': summaryTitle,
        if (summaryOverview != null) 'overview': summaryOverview,
        if (appSummary != null) 'app_summary': appSummary,
      },
    }),
  );
  if (response == null) return false;
  Logger.debug('submitConversationCorrection: ${response.statusCode} ${response.body}');
  return response.statusCode == 200 || response.statusCode == 201 || response.statusCode == 202;
}

class ConversationCorrectionSummary {
  const ConversationCorrectionSummary({this.title = '', this.overview = '', this.emoji = '', this.category = 'other'});

  final String title;
  final String overview;
  final String emoji;
  final String category;

  factory ConversationCorrectionSummary.fromJson(Object? value) {
    if (value is! Map) return const ConversationCorrectionSummary();
    return ConversationCorrectionSummary(
      title: value['title']?.toString() ?? '',
      overview: value['overview']?.toString() ?? '',
      emoji: value['emoji']?.toString() ?? '',
      category: value['category']?.toString() ?? 'other',
    );
  }
}

class ConversationCorrectionReceipt {
  const ConversationCorrectionReceipt({
    required this.correctionId,
    required this.conversationId,
    required this.status,
    required this.before,
    required this.after,
    this.appliedAt,
    this.undoneAt,
  });

  final String correctionId;
  final String conversationId;
  final String status;
  final ConversationCorrectionSummary before;
  final ConversationCorrectionSummary after;
  final DateTime? appliedAt;
  final DateTime? undoneAt;

  bool get isPending => const {'submitted', 'queued', 'processing', 'pending'}.contains(status);
  bool get isApplied => status == 'applied' && undoneAt == null;
  bool get isUndone => status == 'undone' || undoneAt != null;
  bool get isFailed => !isPending && !isApplied && !isUndone;

  factory ConversationCorrectionReceipt.fromJson(Map<String, dynamic> json) => ConversationCorrectionReceipt(
        correctionId: json['correction_id']?.toString() ?? '',
        conversationId: json['conversation_id']?.toString() ?? '',
        status: json['status']?.toString() ?? 'unknown',
        before: ConversationCorrectionSummary.fromJson(json['before']),
        after: ConversationCorrectionSummary.fromJson(json['after']),
        appliedAt: DateTime.tryParse(json['applied_at']?.toString() ?? '')?.toLocal(),
        undoneAt: DateTime.tryParse(json['undone_at']?.toString() ?? '')?.toLocal(),
      );
}

class ConversationReinterpretationReceiptReference {
  const ConversationReinterpretationReceiptReference({
    required this.conversationId,
    required this.correctionId,
    required this.status,
  });

  final String conversationId;
  final String correctionId;
  final String status;

  static ConversationReinterpretationReceiptReference? tryParse(Object? value) {
    if (value is! Map) return null;
    final conversationId = value['conversation_id']?.toString().trim() ?? '';
    final correctionId = value['correction_id']?.toString().trim() ?? '';
    final status = value['status']?.toString().trim() ?? '';
    if (conversationId.isEmpty || correctionId.isEmpty || status.isEmpty) return null;
    return ConversationReinterpretationReceiptReference(
      conversationId: conversationId,
      correctionId: correctionId,
      status: status,
    );
  }
}

class ConversationReinterpretationJob {
  const ConversationReinterpretationJob({
    required this.jobId,
    required this.sessionId,
    required this.conversationId,
    required this.status,
    this.outcome = '',
    required this.correctionIds,
    required this.receipts,
  });

  final String jobId;
  final String sessionId;
  final String conversationId;
  final String status;
  final String outcome;
  final List<String> correctionIds;
  final List<ConversationReinterpretationReceiptReference> receipts;

  bool get isPending => const {'pending', 'running', 'retry'}.contains(status);
  bool get isNoChange => status == 'no_change';
  bool get isPendingReview => status == 'pending_review';
  bool get isApplied => status == 'applied';
  bool get hasTerminalAppliedCorrection => isApplied || (isPendingReview && outcome == 'applied_with_pending');

  String? get appliedCorrectionId {
    for (final receipt in receipts.reversed) {
      if (receipt.status == 'applied' && receipt.conversationId == conversationId) {
        return receipt.correctionId;
      }
    }
    return correctionIds.isEmpty ? null : correctionIds.last;
  }

  static ConversationReinterpretationJob? tryParse(Object? value) {
    if (value is! Map) return null;
    final jobId = value['job_id']?.toString().trim() ?? '';
    final sessionId = value['session_id']?.toString().trim() ?? '';
    final conversationId = value['conversation_id']?.toString().trim() ?? '';
    final status = value['status']?.toString().trim() ?? '';
    final outcome = value['outcome']?.toString().trim() ?? '';
    if (jobId.isEmpty || sessionId.isEmpty || conversationId.isEmpty || status.isEmpty) return null;

    final rawCorrectionIds = value['correction_ids'];
    final correctionIds =
        (rawCorrectionIds is List ? rawCorrectionIds : const []).map((item) => item.toString().trim()).where((id) {
      return id.isNotEmpty;
    }).toList(growable: false);
    final rawReceipts = value['receipts'];
    final receipts = (rawReceipts is List ? rawReceipts : const [])
        .map(ConversationReinterpretationReceiptReference.tryParse)
        .whereType<ConversationReinterpretationReceiptReference>()
        .toList(growable: false);

    return ConversationReinterpretationJob(
      jobId: jobId,
      sessionId: sessionId,
      conversationId: conversationId,
      status: status,
      outcome: outcome,
      correctionIds: correctionIds,
      receipts: receipts,
    );
  }
}

Future<ConversationReinterpretationJob?> getLatestConversationReinterpretation({required String conversationId}) async {
  final encodedConversationId = Uri.encodeComponent(conversationId);
  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/ella/conversations/$encodedConversationId/reinterpretations/latest',
    headers: {},
    method: 'GET',
    body: '',
  );
  if (response == null || response.statusCode != 200) return null;
  final decoded = jsonDecode(response.body);
  if (decoded is! Map) return null;
  return ConversationReinterpretationJob.tryParse(decoded['reinterpretation']);
}

Future<ConversationCorrectionReceipt?> getConversationCorrectionReceipt({
  required String conversationId,
  required String correctionId,
}) async {
  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/ella/conversations/$conversationId/corrections/$correctionId',
    headers: {},
    method: 'GET',
    body: '',
  );
  if (response == null || response.statusCode != 200) return null;
  final decoded = jsonDecode(response.body);
  if (decoded is! Map) return null;
  return ConversationCorrectionReceipt.fromJson(Map<String, dynamic>.from(decoded));
}

Future<ConversationCorrectionReceipt?> undoConversationCorrection({
  required String conversationId,
  required String correctionId,
}) async {
  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/ella/conversations/$conversationId/corrections/$correctionId/undo',
    headers: {},
    method: 'POST',
    body: jsonEncode({}),
  );
  if (response == null || response.statusCode != 200) return null;
  final decoded = jsonDecode(response.body);
  if (decoded is! Map) return null;
  return ConversationCorrectionReceipt.fromJson(Map<String, dynamic>.from(decoded));
}

Future<bool> deleteConversationServer(
  String conversationId, {
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
  ConversationDeleteTransport transport = _defaultConversationDeleteTransport,
}) async {
  final response = await transport(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId',
    expectedAuthenticatedUid: expectedAuthenticatedUid,
    exactAuthority: exactAuthority,
  );
  if (response == null) return false;
  Logger.debug('deleteConversation: ${response.statusCode}');
  return response.statusCode == 204;
}

Future<ServerConversation?> getConversationById(String conversationId) async {
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId',
    headers: {},
    method: 'GET',
    body: '',
  );
  if (response == null) return null;
  if (response.statusCode == 200) {
    return ServerConversation.fromJson(jsonDecode(response.body));
  } else if (response.statusCode == 402) {
    Logger.debug('Unlimited Plan Required for conversation: $conversationId');
    return null;
  }
  return null;
}

Future<bool> updateConversationTitle(String conversationId, String title) async {
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId/title?title=$title',
    headers: {},
    method: 'PATCH',
    body: '',
  );
  if (response == null) return false;
  Logger.debug('updateConversationTitle: ${response.body}');
  return response.statusCode == 200;
}

Future<List<ConversationPhoto>> getConversationPhotos(String conversationId) async {
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId/photos',
    headers: {},
    method: 'GET',
    body: '',
  );
  if (response == null) return [];
  Logger.debug('getConversationPhotos: ${response.body}');
  if (response.statusCode == 200) {
    return (jsonDecode(response.body) as List<dynamic>).map((photo) => ConversationPhoto.fromJson(photo)).toList();
  }
  return [];
}

class TranscriptsResponse {
  List<TranscriptSegment> deepgram;
  List<TranscriptSegment> soniox;
  List<TranscriptSegment> whisperx;
  List<TranscriptSegment> speechmatics;

  TranscriptsResponse({
    this.deepgram = const [],
    this.soniox = const [],
    this.whisperx = const [],
    this.speechmatics = const [],
  });

  factory TranscriptsResponse.fromJson(Map<String, dynamic> json) {
    return TranscriptsResponse(
      deepgram: (json['deepgram'] as List<dynamic>).map((segment) => TranscriptSegment.fromJson(segment)).toList(),
      soniox: (json['soniox'] as List<dynamic>).map((segment) => TranscriptSegment.fromJson(segment)).toList(),
      whisperx: (json['whisperx'] as List<dynamic>).map((segment) => TranscriptSegment.fromJson(segment)).toList(),
      speechmatics:
          (json['speechmatics'] as List<dynamic>).map((segment) => TranscriptSegment.fromJson(segment)).toList(),
    );
  }
}

Future<TranscriptsResponse> getConversationTranscripts(String conversationId) async {
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId/transcripts',
    headers: {},
    method: 'GET',
    body: '',
  );
  if (response == null) return TranscriptsResponse();
  Logger.debug('getConversationTranscripts: ${response.body}');
  if (response.statusCode == 200) {
    var transcripts = (jsonDecode(response.body) as Map<String, dynamic>);
    return TranscriptsResponse.fromJson(transcripts);
  }
  return TranscriptsResponse();
}

Future<bool> hasConversationRecording(String conversationId) async {
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId/recording',
    headers: {},
    method: 'GET',
    body: '',
  );
  if (response == null) return false;
  Logger.debug('hasConversationRecording: ${response.body}');
  if (response.statusCode == 200) {
    return jsonDecode(response.body)['has_recording'] ?? false;
  }
  return false;
}

Future<bool> assignBulkConversationTranscriptSegments(
  String conversationId,
  List<String> segmentIds, {
  bool? isUser,
  String? personId,
}) async {
  String assignType;
  String? value;
  if (isUser == true) {
    assignType = 'is_user';
    value = 'true';
  } else {
    assignType = 'person_id';
    value = personId; // can be null for un-assign
  }

  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId/segments/assign-bulk',
    headers: {},
    method: 'PATCH',
    body: jsonEncode({
      'segment_ids': segmentIds,
      'assign_type': assignType,
      'value': value,
    }),
  );
  if (response == null) return false;
  Logger.debug('assignBulkConversationTranscriptSegments: ${response.body}');
  return response.statusCode == 200;
}

Future<bool> setConversationVisibility(String conversationId, {String visibility = 'shared'}) async {
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId/visibility?value=$visibility&visibility=$visibility',
    headers: {},
    method: 'PATCH',
    body: '',
  );
  if (response == null) return false;
  Logger.debug('setConversationVisibility: ${response.body}');
  return response.statusCode == 200;
}

Future<bool> setConversationStarred(
  String conversationId,
  bool starred, {
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
}) async {
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId/starred?starred=$starred',
    headers: {},
    method: 'PATCH',
    body: '',
    expectedAuthenticatedUid: expectedAuthenticatedUid,
    exactAuthority: exactAuthority,
  );
  if (response == null) return false;
  Logger.debug('setConversationStarred: ${response.body}');
  return response.statusCode == 200;
}

Future<bool> setConversationEventsState(
  String conversationId,
  List<int> eventsIdx,
  List<bool> values,
) async {
  print(jsonEncode({
    'events_idx': eventsIdx,
    'values': values,
  }));
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId/events',
    headers: {},
    method: 'PATCH',
    body: jsonEncode({
      'events_idx': eventsIdx,
      'values': values,
    }),
  );
  if (response == null) return false;
  Logger.debug('setConversationEventsState: ${response.body}');
  return response.statusCode == 200;
}

Future<bool> setConversationActionItemState(
  String conversationId,
  List<int> actionItemsIdx,
  List<bool> values,
) async {
  print(jsonEncode({
    'items_idx': actionItemsIdx,
    'values': values,
    'conversation_id': conversationId,
  }));
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId/action-items',
    headers: {},
    method: 'PATCH',
    body: jsonEncode({
      'items_idx': actionItemsIdx,
      'values': values,
    }),
  );
  if (response == null) return false;
  Logger.debug('setConversationActionItemState: ${response.body}');
  return response.statusCode == 200;
}

Future<bool> updateActionItemDescription(
    String conversationId, String oldDescription, String newDescription, int idx) async {
  var body = {
    'old_description': oldDescription,
    'description': newDescription,
  };
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId/action-items/$idx',
    headers: {},
    method: 'PATCH',
    body: jsonEncode(body),
  );
  if (response == null) return false;
  Logger.debug('updateActionItemDescription: ${response.body}');
  return response.statusCode == 200;
}

Future<bool> deleteConversationActionItem(String conversationId, ActionItem item) async {
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId/action-items',
    headers: {},
    method: 'DELETE',
    body: jsonEncode({
      'completed': item.completed,
      'description': item.description,
    }),
  );
  if (response == null) return false;
  Logger.debug('deleteConversationActionItem: ${response.body}');
  return response.statusCode == 204;
}

//this is expected to return complete memories
Future<List<ServerConversation>> sendStorageToBackend(File file, String sdCardDateTimeString) async {
  if (!SharedPreferencesUtil().aiConsentAccepted) return [];
  try {
    var response = await makeMultipartApiCall(
      url: '${Env.apiBaseUrl}sdcard_memory?date_time=$sdCardDateTimeString',
      files: [file],
      fileFieldName: 'file',
    );

    if (response.statusCode == 200) {
      Logger.debug('storageSend Response body: ${jsonDecode(response.body)}');
    } else {
      Logger.debug('Failed to storageSend. Status code: ${response.statusCode}');
      return [];
    }

    var memories = (jsonDecode(response.body) as List<dynamic>)
        .map((conversation) => ServerConversation.fromJson(conversation))
        .toList();
    Logger.debug('getMemories length: ${memories.length}');

    return memories;
  } catch (e) {
    Logger.debug('An error occurred storageSend: $e');
    return [];
  }
}

Future<SyncLocalFilesResponse> syncLocalFiles(
  List<File> files, {
  String? expectedAuthenticatedUid,
}) async {
  if (!SharedPreferencesUtil().aiConsentAccepted) {
    throw StateError('AI consent is required before stored audio sync');
  }
  try {
    var response = await makeMultipartApiCall(
      url: '${Env.apiBaseUrl}v1/sync-local-files',
      files: files,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
    );

    if (response.statusCode == 200) {
      Logger.debug('syncLocalFile Response body: ${jsonDecode(response.body)}');
      return SyncLocalFilesResponse.fromJson(jsonDecode(response.body));
    } else if (response.statusCode == 400) {
      throw Exception('Audio file could not be processed by server');
    } else if (response.statusCode == 413) {
      throw Exception('Audio file is too large to upload');
    } else if (response.statusCode >= 500) {
      throw Exception('Server is temporarily unavailable');
    } else {
      throw Exception('Upload failed unexpectedly');
    }
  } catch (e) {
    Logger.debug('syncLocalFiles error: $e');
    rethrow;
  }
}

Future<(List<ServerConversation>, int, int)> searchConversationsServer(
  String query, {
  int? page,
  int? limit,
  bool includeDiscarded = true,
}) async {
  Logger.debug(Env.apiBaseUrl);
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/search',
    headers: {},
    method: 'POST',
    body:
        jsonEncode({'query': query, 'page': page ?? 1, 'per_page': limit ?? 10, 'include_discarded': includeDiscarded}),
  );
  if (response == null) return (<ServerConversation>[], 0, 0);
  if (response.statusCode == 200) {
    List<dynamic> items = (jsonDecode(response.body))['items'];
    int currentPage = (jsonDecode(response.body))['current_page'];
    int totalPages = (jsonDecode(response.body))['total_pages'];
    var convos = items.map<ServerConversation>((item) => ServerConversation.fromJson(item)).toList();
    return (convos, currentPage, totalPages);
  }
  return (<ServerConversation>[], 0, 0);
}

Future<String> testConversationPrompt(String prompt, String conversationId) async {
  if (!SharedPreferencesUtil().aiConsentAccepted) return '';
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId/test-prompt',
    headers: {},
    method: 'POST',
    body: jsonEncode({
      'prompt': prompt,
    }),
  );
  if (response == null) return '';
  if (response.statusCode == 200) {
    return jsonDecode(response.body)['summary'];
  } else {
    return '';
  }
}

// *********************************
// ******** ACTION ITEMS ***********
// *********************************

Future<ActionItemsResponse> getActionItems({
  int limit = 50,
  int offset = 0,
  bool includeCompleted = true,
  DateTime? startDate,
  DateTime? endDate,
}) async {
  String url = '${Env.apiBaseUrl}v1/action-items?limit=$limit&offset=$offset&include_completed=$includeCompleted';

  if (startDate != null) {
    url += '&start_date=${startDate.toIso8601String()}';
  }
  if (endDate != null) {
    url += '&end_date=${endDate.toIso8601String()}';
  }

  var response = await makeApiCall(
    url: url,
    headers: {},
    method: 'GET',
    body: '',
  );

  if (response == null) return ActionItemsResponse(actionItems: [], hasMore: false);

  if (response.statusCode == 200) {
    var body = utf8.decode(response.bodyBytes);
    return ActionItemsResponse.fromJson(jsonDecode(body));
  } else {
    Logger.debug('getActionItems error ${response.statusCode}');
    return ActionItemsResponse(actionItems: [], hasMore: false);
  }
}

Future<List<App>> getConversationSuggestedApps(String conversationId) async {
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId/suggested-apps',
    headers: {},
    method: 'GET',
    body: '',
  );

  if (response == null) return [];
  Logger.debug('getConversationSuggestedApps: ${response.body}');
  if (response.statusCode == 200) {
    var data = jsonDecode(response.body);
    return (data['suggested_apps'] as List<dynamic>).map((appData) => App.fromJson(appData)).toList();
  }
  return [];
}

Future<bool> updateActionItemStateByMetadata(
  String conversationId,
  int itemIndex,
  bool newState,
) async {
  return await setConversationActionItemState(conversationId, [itemIndex], [newState]);
}

// *********************************
// ******** MERGE CONVERSATIONS ****
// *********************************

/// Response from the merge conversations API
class MergeConversationsResponse {
  final String status;
  final String message;
  final String? warning;
  final List<String> conversationIds;

  MergeConversationsResponse({
    required this.status,
    required this.message,
    this.warning,
    required this.conversationIds,
  });

  factory MergeConversationsResponse.fromJson(Map<String, dynamic> json) {
    return MergeConversationsResponse(
      status: json['status'] ?? 'merging',
      message: json['message'] ?? 'Merge started',
      warning: json['warning'],
      conversationIds: List<String>.from(json['conversation_ids'] ?? []),
    );
  }
}

/// Initiate merging of multiple conversations
Future<MergeConversationsResponse?> mergeConversations(
  List<String> conversationIds, {
  bool reprocess = true,
}) async {
  if (conversationIds.length < 2) {
    Logger.debug('mergeConversations: At least 2 conversations required');
    return null;
  }

  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/merge',
    headers: {},
    method: 'POST',
    body: jsonEncode({
      'conversation_ids': conversationIds,
      'reprocess': reprocess,
    }),
  );

  if (response == null) return null;

  Logger.debug('mergeConversations: ${response.body}');

  if (response.statusCode == 200) {
    return MergeConversationsResponse.fromJson(jsonDecode(response.body));
  } else {
    Logger.debug('mergeConversations error: ${response.statusCode} - ${response.body}');
    return null;
  }
}
