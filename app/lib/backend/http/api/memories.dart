import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/schema/memory.dart';
import 'package:omi/env/env.dart';

Future<bool> createMemoryServer(String content, String visibility) async {
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v3/memories',
    headers: {},
    method: 'POST',
    body: json.encode({
      'content': content,
      'visibility': visibility,
    }),
  );
  if (response == null) return false;
  debugPrint('createMemory response: ${response.body}');
  return response.statusCode == 200;
}

Future<bool> updateMemoryVisibilityServer(String memoryId, String visibility) async {
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v3/memories/$memoryId/visibility?value=$visibility',
    headers: {},
    method: 'PATCH',
    body: '',
  );
  if (response == null) return false;
  debugPrint('updateMemoryVisibility response: ${response.body}');
  return response.statusCode == 200;
}

Future<List<Memory>> getMemories({int limit = 100, int offset = 0}) async {
  try {
    debugPrint('🔍 Calling getMemories API: ${Env.apiBaseUrl}v3/memories?limit=$limit&offset=$offset');

    var response = await makeApiCall(
      url: '${Env.apiBaseUrl}v3/memories?limit=$limit&offset=$offset',
      headers: {},
      method: 'GET',
      body: '',
    );

    if (response == null) {
      debugPrint('❌ getMemories: Response is null');
      return [];
    }

    debugPrint('✅ getMemories: Status ${response.statusCode}');
    debugPrint('📦 getMemories: Response body: ${response.body}');

    List<dynamic> memoriesJson = json.decode(response.body);
    debugPrint('📊 getMemories: Parsed ${memoriesJson.length} memories from JSON');

    List<Memory> memories = memoriesJson.map((memory) => Memory.fromJson(memory)).toList();
    debugPrint('✅ getMemories: Successfully converted ${memories.length} Memory objects');

    return memories;
  } catch (e, stackTrace) {
    debugPrint('❌ getMemories ERROR: $e');
    debugPrint('❌ Stack trace: $stackTrace');
    return [];
  }
}

Future<bool> deleteMemoryServer(String memoryId) async {
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v3/memories/$memoryId',
    headers: {},
    method: 'DELETE',
    body: '',
  );
  if (response == null) return false;
  debugPrint('deleteMemory response: ${response.body}');
  return response.statusCode == 200;
}

Future<bool> deleteAllMemoriesServer() async {
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v3/memories',
    headers: {},
    method: 'DELETE',
    body: '',
  );
  if (response == null) return false;
  debugPrint('deleteAllMemories response: ${response.body}');
  return response.statusCode == 200;
}

Future<bool> reviewMemoryServer(String memoryId, bool value) async {
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v3/memories/$memoryId/review?value=$value',
    headers: {},
    method: 'POST',
    body: '',
  );
  if (response == null) return false;
  debugPrint('reviewMemory response: ${response.body}');
  return response.statusCode == 200;
}

Future<bool> editMemoryServer(String memoryId, String value) async {
  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v3/memories/$memoryId?value=$value',
    headers: {},
    method: 'PATCH',
    body: '',
  );
  if (response == null) return false;
  debugPrint('editMemory response: ${response.body}');
  return response.statusCode == 200;
}
