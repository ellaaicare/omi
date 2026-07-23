import 'dart:convert';
import 'dart:io';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/schema.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/platform/platform_manager.dart';

Future<CreateConversationResponse?> processInProgressConversation() async {
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations',
    headers: {},
    method: 'POST',
    body: jsonEncode({}),
  );
  if (response == null) return null;
  Logger.debug('createConversationServer: ${response.body}');
  if (response.statusCode == 200) {
    return CreateConversationResponse.fromJson(jsonDecode(response.body));
  } else {
    // TODO: Server returns 304 doesn't recover
    PlatformManager.instance.crashReporter.reportCrash(Exception('Failed to create conversation'), StackTrace.current,
        userAttributes: {'response': response.body});
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

  var response = await makeApiCall(url: url, headers: {}, method: 'GET', body: '');
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

class ConversationCorrectionSubmission {
  final String correctionId;
  final String conversationId;
  final String status;
  final bool queued;

  const ConversationCorrectionSubmission({
    required this.correctionId,
    required this.conversationId,
    required this.status,
    required this.queued,
  });

  factory ConversationCorrectionSubmission.fromJson(Map<String, dynamic> json) {
    return ConversationCorrectionSubmission(
      correctionId: json['correction_id']?.toString() ?? '',
      conversationId: json['conversation_id']?.toString() ?? '',
      status: json['status']?.toString() ?? '',
      queued: json['queued'] == true,
    );
  }
}

class ConversationCorrectionReceipt {
  final String correctionId;
  final String conversationId;
  final String status;
  final DateTime? appliedAt;
  final DateTime? undoneAt;
  final String beforeTitle;
  final String beforeOverview;
  final String afterTitle;
  final String afterOverview;
  final int propagationAppliedCount;
  final int propagationRevertedCount;

  const ConversationCorrectionReceipt({
    required this.correctionId,
    required this.conversationId,
    required this.status,
    required this.appliedAt,
    required this.undoneAt,
    required this.beforeTitle,
    required this.beforeOverview,
    required this.afterTitle,
    required this.afterOverview,
    required this.propagationAppliedCount,
    required this.propagationRevertedCount,
  });

  bool get isApplied => status == 'applied';
  bool get isUndone => status == 'undone';
  bool get propagated => propagationAppliedCount > 0;

  factory ConversationCorrectionReceipt.fromJson(Map<String, dynamic> json) {
    final before = json['before'] is Map ? Map<String, dynamic>.from(json['before']) : const <String, dynamic>{};
    final after = json['after'] is Map ? Map<String, dynamic>.from(json['after']) : const <String, dynamic>{};
    return ConversationCorrectionReceipt(
      correctionId: json['correction_id']?.toString() ?? '',
      conversationId: json['conversation_id']?.toString() ?? '',
      status: json['status']?.toString() ?? '',
      appliedAt: json['applied_at'] != null ? DateTime.tryParse(json['applied_at'].toString())?.toLocal() : null,
      undoneAt: json['undone_at'] != null ? DateTime.tryParse(json['undone_at'].toString())?.toLocal() : null,
      beforeTitle: before['title']?.toString() ?? '',
      beforeOverview: before['overview']?.toString() ?? '',
      afterTitle: after['title']?.toString() ?? '',
      afterOverview: after['overview']?.toString() ?? '',
      propagationAppliedCount: (json['propagation_applied_count'] as num?)?.toInt() ?? 0,
      propagationRevertedCount: (json['propagation_reverted_count'] as num?)?.toInt() ?? 0,
    );
  }
}

Future<ConversationCorrectionSubmission?> submitConversationCorrection({
  required String conversationId,
  required String correctionText,
  String? summaryTitle,
  String? summaryOverview,
  String? appSummary,
}) async {
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
  if (response == null) return null;
  Logger.debug('submitConversationCorrection: ${response.statusCode}');
  if (response.statusCode == 200 || response.statusCode == 201 || response.statusCode == 202) {
    return ConversationCorrectionSubmission.fromJson(jsonDecode(response.body));
  }
  return null;
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
  if (response == null) return null;
  Logger.debug('getConversationCorrectionReceipt: ${response.statusCode}');
  if (response.statusCode == 200) {
    return ConversationCorrectionReceipt.fromJson(jsonDecode(response.body));
  }
  return null;
}

Future<ConversationCorrectionReceipt?> undoConversationCorrection({
  required String conversationId,
  required String correctionId,
}) async {
  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/ella/conversations/$conversationId/corrections/$correctionId/undo',
    headers: {},
    method: 'POST',
    body: '{}',
  );
  if (response == null) return null;
  Logger.debug('undoConversationCorrection: ${response.statusCode}');
  if (response.statusCode == 200) {
    return ConversationCorrectionReceipt.fromJson(jsonDecode(response.body));
  }
  return null;
}

Future<bool> deleteConversationServer(String conversationId) async {
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId',
    headers: {},
    method: 'DELETE',
    body: '',
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

Future<bool> setConversationStarred(String conversationId, bool starred) async {
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/conversations/$conversationId/starred?starred=$starred',
    headers: {},
    method: 'PATCH',
    body: '',
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

Future<SyncLocalFilesResponse> syncLocalFiles(List<File> files) async {
  if (!SharedPreferencesUtil().aiConsentAccepted) {
    throw StateError('AI consent is required before stored audio sync');
  }
  try {
    var response = await makeMultipartApiCall(
      url: '${Env.apiBaseUrl}v1/sync-local-files',
      files: files,
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
