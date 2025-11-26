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
    // Handle agent_response which could be null, a Map, or other types
    Map<String, dynamic> agentResponse = {};
    final rawAgentResponse = json['agent_response'];
    if (rawAgentResponse != null && rawAgentResponse is Map<String, dynamic>) {
      agentResponse = rawAgentResponse;
    } else if (rawAgentResponse != null && rawAgentResponse is Map) {
      // Handle Map<dynamic, dynamic> case
      agentResponse = Map<String, dynamic>.from(rawAgentResponse);
    }

    // Handle metrics which could be null or a Map
    Map<String, dynamic> metrics = {};
    final rawMetrics = json['metrics'];
    if (rawMetrics != null && rawMetrics is Map<String, dynamic>) {
      metrics = rawMetrics;
    } else if (rawMetrics != null && rawMetrics is Map) {
      metrics = Map<String, dynamic>.from(rawMetrics);
    }

    return E2ETestResponse(
      testType: json['test_type'] as String? ?? 'unknown',
      source: json['source'] as String?,
      transcript: json['transcript'] as String?,
      agentResponse: agentResponse,
      metrics: metrics,
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
/// - uid: User ID for testing (defaults to test_user_123)
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
  String uid = 'test_user_123',
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
      'uid': uid,
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
/// Request:
/// - text: Text to extract memories from (required if no audio)
/// - source: Audio source ("phone_mic", "friend_device", etc.)
/// - uid: User ID for testing (defaults to test_user_123)
/// - debug: Enable debug output
///
/// Backend expects payload format:
/// {
///   "uid": "test_user_123",
///   "segments": [
///     {
///       "speaker": "SPEAKER_00",
///       "text": "conversation text",
///       "stt_source": "phone_mic"
///     }
///   ],
///   "structured": {
///     "title": "Test conversation",
///     "overview": "conversation text"
///   }
/// }
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
  String uid = 'test_user_123',
  bool debug = false,
}) async {
  if (audio == null && text == null) {
    throw Exception('Either audio or text must be provided');
  }

  // Match backend test endpoint signature (testing.py:302-308)
  final Map<String, dynamic> payload = {
    'uid': uid,
    'debug': debug,
    'source': source,
    'conversation_id': conversationId,
  };

  if (audio != null) {
    payload['audio'] = audio;
  } else if (text != null) {
    payload['text'] = text;
  }

  print('🔍 Memory Agent payload: ${jsonEncode(payload)}');

  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/test/memory-agent',
    headers: {'Content-Type': 'application/json'},
    method: 'POST',
    body: jsonEncode(payload),
  );

  if (response?.statusCode == 200) {
    final data = jsonDecode(response!.body) as Map<String, dynamic>;
    return E2ETestResponse.fromJson(data);
  }

  // Log error details for debugging
  if (response != null) {
    print('❌ Memory Agent failed: Status ${response.statusCode}');
    print('Response body: ${response.body}');
  } else {
    print('❌ Memory Agent: makeApiCall returned null');
  }

  return null;
}

/// Test Summary Agent (daily summaries)
///
/// Request:
/// - transcript: Text to summarize (optional, uses default test text if not provided)
/// - conversationId: Conversation ID (defaults to test_conv)
/// - uid: User ID for testing (defaults to test_user_123)
/// - debug: Enable debug output
///
/// Backend expects payload format:
/// {
///   "uid": "test_user_123",
///   "conversation_id": "test_conv",
///   "transcript": "Today I had lunch with Sarah..."
/// }
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
  String? transcript,
  String conversationId = 'test_conv',
  String uid = 'test_user_123',
  bool debug = false,
}) async {
  // Default test transcript if none provided
  final testTranscript = transcript ??
      "Today I had a productive day. I had lunch with Sarah at noon and we discussed the new project timeline for Q2. "
          "We agreed to push the deadline back by two weeks. In the afternoon, I reviewed the budget proposal and "
          "sent feedback to the finance team. I also scheduled a follow-up meeting for next Tuesday at 2 PM.";

  // Match backend test endpoint signature
  // Backend expects 'text' parameter (NOT 'transcript')
  final payload = {
    'uid': uid,
    'text': testTranscript,
    'conversation_id': conversationId,
    'debug': debug,
  };

  print('🔍 Summary Agent payload: ${jsonEncode(payload)}');

  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/test/summary-agent',
    headers: {'Content-Type': 'application/json'},
    method: 'POST',
    body: jsonEncode(payload),
  );

  if (response?.statusCode == 200) {
    final data = jsonDecode(response!.body) as Map<String, dynamic>;
    return E2ETestResponse.fromJson(data);
  }

  // Log error details for debugging
  if (response != null) {
    print('❌ Summary Agent failed: Status ${response.statusCode}');
    print('Response body: ${response.body}');
  } else {
    print('❌ Summary Agent: makeApiCall returned null');
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
  String uid = 'test_user_123',
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
      'uid': uid,
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
  String uid = 'test_user_123',
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
      'uid': uid,
      'debug': debug,
    }),
  );

  if (response?.statusCode == 200) {
    final data = jsonDecode(response!.body) as Map<String, dynamic>;
    return E2ETestResponse.fromJson(data);
  }

  return null;
}
