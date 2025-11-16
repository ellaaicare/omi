import 'dart:convert';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/env/env.dart';

/// E2E Testing API Client
/// Calls backend test endpoints for agent testing (Scanner, Memory, Summary, Chat)
///
/// Backend endpoints at https://api.ella-ai-care.com:
/// - POST /v1/test/scanner-agent
/// - POST /v1/test/memory-agent
/// - POST /v1/test/summary-agent
/// - POST /v1/test/chat-sync
/// - POST /v1/test/chat-async

/// Test response model
class E2ETestResponse {
  final String testType;
  final String? source;
  final String? transcript;
  final Map<String, dynamic> agentResponse;
  final Map<String, dynamic> metrics;
  final String? jobId; // For async chat
  final String? status; // For async chat

  E2ETestResponse({
    required this.testType,
    this.source,
    this.transcript,
    required this.agentResponse,
    required this.metrics,
    this.jobId,
    this.status,
  });

  factory E2ETestResponse.fromJson(Map<String, dynamic> json) {
    return E2ETestResponse(
      testType: json['test_type'] as String,
      source: json['source'] as String?,
      transcript: json['transcript'] as String?,
      agentResponse: json['agent_response'] as Map<String, dynamic>? ?? {},
      metrics: json['metrics'] as Map<String, dynamic>? ?? {},
      jobId: json['job_id'] as String?,
      status: json['status'] as String?,
    );
  }
}

/// Test Scanner Agent (urgency detection)
///
/// Request:
/// - audio: Base64 encoded WAV audio (optional if text provided)
/// - text: Text to test (optional if audio provided)
/// - source: Audio source ("phone_mic", "friend_device", etc.)
/// - conversationId: Conversation ID for context
///
/// Response:
/// - urgency_level: "critical", "high", "medium", "low", "none"
/// - detected_event: "cardiac_emergency", "fall_emergency", "wake_word_detected", etc.
/// - explanation: Why this urgency level was chosen
/// - recommended_action: What to do next
/// - confidence: 0.0 - 1.0
Future<E2ETestResponse?> testScannerAgent({
  String? audio,
  String? text,
  String source = 'phone_mic',
  String conversationId = 'test_conv',
  bool debug = false,
}) async {
  if (audio == null && text == null) {
    throw Exception('Either audio or text must be provided');
  }

  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/test/scanner-agent',
    headers: {'Content-Type': 'application/json'},
    method: 'POST',
    body: jsonEncode({
      if (audio != null) 'audio': audio,
      if (text != null) 'text': text,
      'source': source,
      'conversation_id': conversationId,
      'debug': debug,
    }),
  );

  if (response?.statusCode == 200) {
    final data = jsonDecode(response!.body) as Map<String, dynamic>;
    return E2ETestResponse.fromJson(data);
  }

  return null;
}

/// Test Memory Agent (memory extraction)
///
/// Request: Same as Scanner Agent
///
/// Response:
/// - memories: List of extracted memories
///   - content: Memory text
///   - category: "social", "work", "health", "interesting"
///   - timestamp: ISO 8601 timestamp
///   - participants: List of people involved
///   - visibility: "private", "public"
///   - tags: List of tags
/// - total_memories: Number of memories extracted
Future<E2ETestResponse?> testMemoryAgent({
  String? audio,
  String? text,
  String source = 'phone_mic',
  String conversationId = 'test_conv',
  bool debug = false,
}) async {
  if (audio == null && text == null) {
    throw Exception('Either audio or text must be provided');
  }

  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/test/memory-agent',
    headers: {'Content-Type': 'application/json'},
    method: 'POST',
    body: jsonEncode({
      if (audio != null) 'audio': audio,
      if (text != null) 'text': text,
      'source': source,
      'conversation_id': conversationId,
      'debug': debug,
    }),
  );

  if (response?.statusCode == 200) {
    final data = jsonDecode(response!.body) as Map<String, dynamic>;
    return E2ETestResponse.fromJson(data);
  }

  return null;
}

/// Test Summary Agent (daily summaries)
///
/// Request:
/// - conversationId: Conversation ID to summarize
/// - date: Date to summarize (YYYY-MM-DD format, optional, defaults to today)
///
/// Response:
/// - title: Summary title
/// - overview: Brief overview
/// - emoji: Representative emoji
/// - category: "health", "work", "social", "general"
/// - key_points: List of key points
/// - sentiment: "positive", "neutral", "negative"
/// - action_items: List of action items
///   - description: Action item text
///   - due_at: ISO 8601 timestamp
///   - priority: "high", "medium", "low"
/// - events: List of calendar events
///   - title: Event title
///   - start: ISO 8601 timestamp
///   - duration: Duration in minutes
Future<E2ETestResponse?> testSummaryAgent({
  String conversationId = 'test_conv',
  String? date,
  bool debug = false,
}) async {
  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/test/summary-agent',
    headers: {'Content-Type': 'application/json'},
    method: 'POST',
    body: jsonEncode({
      'conversation_id': conversationId,
      if (date != null) 'date': date,
      'debug': debug,
    }),
  );

  if (response?.statusCode == 200) {
    final data = jsonDecode(response!.body) as Map<String, dynamic>;
    return E2ETestResponse.fromJson(data);
  }

  return null;
}

/// Test Chat Agent - Synchronous (30s timeout)
///
/// Request: Same as Scanner Agent
///
/// Response:
/// - text: Chat response text
/// - urgency_level: "low", "medium", "high", "critical"
/// - action_items: List of action items
/// - context_used: List of context sources used
/// - confidence: 0.0 - 1.0
Future<E2ETestResponse?> testChatSync({
  String? audio,
  String? text,
  String source = 'phone_mic',
  String conversationId = 'test_conv',
  bool debug = false,
}) async {
  if (audio == null && text == null) {
    throw Exception('Either audio or text must be provided');
  }

  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/test/chat-sync',
    headers: {'Content-Type': 'application/json'},
    method: 'POST',
    body: jsonEncode({
      if (audio != null) 'audio': audio,
      if (text != null) 'text': text,
      'source': source,
      'conversation_id': conversationId,
      'debug': debug,
    }),
  );

  if (response?.statusCode == 200) {
    final data = jsonDecode(response!.body) as Map<String, dynamic>;
    return E2ETestResponse.fromJson(data);
  }

  return null;
}

/// Test Chat Agent - Asynchronous (up to 120s, uses push notifications)
///
/// Request: Same as Scanner Agent
///
/// Response (immediate):
/// - job_id: Job ID for tracking
/// - status: "processing"
/// - message: Status message
///
/// IMPORTANT: The actual response will come via push notification!
/// The push notification will have:
/// - action: "speak_tts"
/// - audio_url: URL to pre-generated TTS audio
/// - text: Chat response text
///
/// The existing TTS push notification handler (notification_service_fcm.dart)
/// will automatically play the audio when the push arrives.
Future<E2ETestResponse?> testChatAsync({
  String? audio,
  String? text,
  String source = 'phone_mic',
  String conversationId = 'test_conv',
  bool debug = false,
}) async {
  if (audio == null && text == null) {
    throw Exception('Either audio or text must be provided');
  }

  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/test/chat-async',
    headers: {'Content-Type': 'application/json'},
    method: 'POST',
    body: jsonEncode({
      if (audio != null) 'audio': audio,
      if (text != null) 'text': text,
      'source': source,
      'conversation_id': conversationId,
      'debug': debug,
    }),
  );

  if (response?.statusCode == 200) {
    final data = jsonDecode(response!.body) as Map<String, dynamic>;
    return E2ETestResponse.fromJson(data);
  }

  return null;
}
