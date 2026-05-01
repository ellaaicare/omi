import 'dart:convert';

import 'package:flutter/material.dart' show Color;
import 'package:http/http.dart' as http;
import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/models/guardian_mode.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/logger.dart';

const String _dashboardBase = 'https://ella-ai-care.com';

/// In-memory cache for presets (they change rarely).
List<GuardianPreset>? _cachedPresets;

String get _userId {
  // The dashboard route accepts the OMI/Firebase UID. Prefer that stable
  // identity so stale cached Ella UUIDs cannot read or update a different row.
  final uid = SharedPreferencesUtil().uid;
  if (uid.isNotEmpty) return uid;

  return SharedPreferencesUtil().ellaUserId;
}

/// GET /api/users/{userId}/guardian-mode
///
/// Returns a [GuardianModeInfo] that includes a [GuardianModeState] for the
/// two-tier picker.  Handles both the new schema {override, features} and the
/// legacy schema {mode}.
Future<GuardianModeInfo?> getGuardianMode() async {
  final uid = _userId;
  if (uid.isEmpty) return null;
  try {
    final response = await http.get(
      Uri.parse('$_dashboardBase/api/users/$uid/guardian-mode'),
      headers: {'Content-Type': 'application/json'},
    ).timeout(const Duration(seconds: 10));

    if (response.statusCode == 200) {
      final data = jsonDecode(response.body) as Map<String, dynamic>;
      if (data['success'] == true) {
        return GuardianModeInfo.fromJson(data);
      }
    }
    Logger.debug('getGuardianMode: ${response.statusCode} ${response.body}');
    return null;
  } catch (e) {
    Logger.debug('getGuardianMode error: $e');
    return null;
  }
}

/// PUT /api/users/{userId}/guardian-mode
///
/// Sends the new two-tier body:
///   { "override": "CYBORG" | "CHATBOT" | "DEMO" | null, "features": [...] }
Future<bool> setGuardianModeTwoTier(GuardianModeState state) async {
  final uid = _userId;
  if (uid.isEmpty) return false;
  try {
    final response = await http
        .put(
          Uri.parse('$_dashboardBase/api/users/$uid/guardian-mode'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode(state.toJson()),
        )
        .timeout(const Duration(seconds: 10));

    if (response.statusCode == 200) {
      final data = jsonDecode(response.body) as Map<String, dynamic>;
      return data['success'] == true;
    }
    Logger.debug('setGuardianMode: ${response.statusCode} ${response.body}');
    return false;
  } catch (e) {
    Logger.debug('setGuardianMode error: $e');
    return false;
  }
}

/// Legacy single-mode PUT — kept for callers that haven't migrated yet.
Future<bool> setGuardianMode(GuardianModeKey mode) async {
  final uid = _userId;
  if (uid.isEmpty) return false;
  try {
    final response = await http
        .put(
          Uri.parse('$_dashboardBase/api/users/$uid/guardian-mode'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({'mode': mode.toApiString()}),
        )
        .timeout(const Duration(seconds: 10));

    if (response.statusCode == 200) {
      final data = jsonDecode(response.body) as Map<String, dynamic>;
      return data['success'] == true;
    }
    Logger.debug('setGuardianMode: ${response.statusCode} ${response.body}');
    return false;
  } catch (e) {
    Logger.debug('setGuardianMode error: $e');
    return false;
  }
}

/// GET /v1/ella/guardian/voice-config?uid=...
Future<GuardianVoiceConfig?> getGuardianVoiceConfig() async {
  final uid = _userId;
  final apiBaseUrl = Env.apiBaseUrl;
  if (uid.isEmpty || apiBaseUrl == null) return null;

  try {
    final response = await makeApiCall(
      url: '${apiBaseUrl}v1/ella/guardian/voice-config?uid=${Uri.encodeQueryComponent(uid)}',
      headers: {'Content-Type': 'application/json'},
      method: 'GET',
      body: '',
      timeout: const Duration(seconds: 10),
    );

    if (response?.statusCode == 200) {
      final data = jsonDecode(response!.body) as Map<String, dynamic>;
      return GuardianVoiceConfig.fromJson(data);
    }
    Logger.debug('getGuardianVoiceConfig: ${response?.statusCode} ${response?.body}');
    return const GuardianVoiceConfig();
  } catch (e) {
    Logger.debug('getGuardianVoiceConfig error: $e');
    return const GuardianVoiceConfig();
  }
}

/// PUT /v1/ella/guardian/voice-config
Future<GuardianVoiceConfig?> setGuardianVoiceConfig(GuardianVoiceConfig config) async {
  final uid = _userId;
  final apiBaseUrl = Env.apiBaseUrl;
  if (uid.isEmpty || apiBaseUrl == null) return null;

  try {
    final response = await makeApiCall(
      url: '${apiBaseUrl}v1/ella/guardian/voice-config',
      headers: {'Content-Type': 'application/json'},
      method: 'PUT',
      body: jsonEncode(config.toJson(uid: uid)),
      timeout: const Duration(seconds: 10),
    );

    if (response?.statusCode == 200) {
      final data = jsonDecode(response!.body) as Map<String, dynamic>;
      return GuardianVoiceConfig.fromJson(data);
    }
    Logger.debug('setGuardianVoiceConfig: ${response?.statusCode} ${response?.body}');
    return null;
  } catch (e) {
    Logger.debug('setGuardianVoiceConfig error: $e');
    return null;
  }
}

/// GET /api/guardian/presets  (no auth required, cached in memory)
Future<List<GuardianPreset>> getGuardianPresets() async {
  if (_cachedPresets != null) return _cachedPresets!;
  try {
    final response = await http.get(
      Uri.parse('$_dashboardBase/api/guardian/presets'),
      headers: {'Content-Type': 'application/json'},
    ).timeout(const Duration(seconds: 10));

    if (response.statusCode == 200) {
      final data = jsonDecode(response.body) as Map<String, dynamic>;
      if (data['success'] == true) {
        final list = data['presets'] as List;
        _cachedPresets = list.map((p) => GuardianPreset.fromJson(p as Map<String, dynamic>)).toList();
        return _cachedPresets!;
      }
    }
    Logger.debug('getGuardianPresets: ${response.statusCode}');
  } catch (e) {
    Logger.debug('getGuardianPresets error: $e');
  }
  return _fallbackPresets();
}

/// Hard-coded fallback so the UI works even if the presets endpoint is down.
List<GuardianPreset> _fallbackPresets() => const [
      GuardianPreset(
        presetKey: 'EMERGENCY_ONLY',
        name: 'Emergency Alerts',
        description: 'Critical alerts only — fall detection, medical emergencies, fire/smoke.',
        detailsBullets: ['Medical emergencies', 'Fall detection', 'Fire/smoke'],
        color: Color(0xFFF59E0B),
      ),
      GuardianPreset(
        presetKey: 'ACTIVE_SUPPORT',
        name: 'Active Support',
        description: 'Emergency alerts + recall assistance + schedule reminders + pattern monitoring.',
        detailsBullets: [
          'Medical emergencies',
          'Wake words (always active)',
          'Recall assistance',
        ],
        color: Color(0xFF14B8A6),
      ),
      GuardianPreset(
        presetKey: 'MAXIMUM_AWARENESS',
        name: 'Maximum Awareness',
        description:
            'High-sensitivity care monitoring for risk, vulnerability, health, cognitive, emotional, and social support signals.',
        detailsBullets: [
          'Broad care monitoring',
          'Lower alert thresholds',
          'Helpful during outings or recovery',
        ],
        color: Color(0xFF6366F1),
      ),
      GuardianPreset(
        presetKey: 'MEMORY_SUPPORT',
        name: 'Memory Support',
        description: 'Proactive memory cues and gentle recall assistance for cognitive support.',
        detailsBullets: [
          'Memory cues',
          'Daily routine reminders',
          'Cognitive pattern monitoring',
        ],
        color: Color(0xFF10B981),
      ),
      GuardianPreset(
        presetKey: 'CYBORG',
        name: 'Cyborg',
        description:
            'Experimental ambient intelligence — Ella listens broadly and speaks only when she can add useful context, coaching, memory, or insight.',
        detailsBullets: [
          'Ambient world enhancement',
          'Useful companion asides',
          'Experimental brain-enhancer mode',
        ],
        color: Color(0xFFEC4899),
      ),
      GuardianPreset(
        presetKey: 'CHATBOT',
        name: 'Chatbot',
        description: 'Full two-way voice conversation with Ella, focused on primary-speaker user utterances.',
        detailsBullets: [
          'Conversational replies',
          'Suppresses media/background audio',
          'Best for direct voice chat',
        ],
        color: Color(0xFFF97316),
      ),
      GuardianPreset(
        presetKey: 'DEMO',
        name: 'Demo',
        description: 'Demonstration mode with scripted responses for showcasing Ella.',
        detailsBullets: ['Scripted demo responses', 'Showcase mode'],
        color: Color(0xFF3B82F6),
      ),
    ];
