import 'dart:convert';

import 'package:flutter/material.dart' show Color;
import 'package:http/http.dart' as http;
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/models/guardian_mode.dart';
import 'package:omi/utils/logger.dart';

const String _dashboardBase = 'https://ella-ai-care.com';

/// In-memory cache for presets (they change rarely).
List<GuardianPreset>? _cachedPresets;

String get _userId {
  final ellaId = SharedPreferencesUtil().ellaUserId;
  return ellaId.isNotEmpty ? ellaId : SharedPreferencesUtil().uid;
}

/// GET /api/users/{userId}/guardian-mode
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
Future<bool> setGuardianMode(GuardianModeKey mode) async {
  final uid = _userId;
  if (uid.isEmpty) return false;
  try {
    final response = await http.put(
      Uri.parse('$_dashboardBase/api/users/$uid/guardian-mode'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'mode': mode.toApiString()}),
    ).timeout(const Duration(seconds: 10));

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
List<GuardianPreset> _fallbackPresets() => [
      GuardianPreset(
        presetKey: 'EMERGENCY_ONLY',
        name: 'Emergency Only',
        description: 'Critical alerts only — fall detection, medical emergencies, fire/smoke.',
        detailsBullets: ['Medical emergencies', 'Fall detection', 'Fire/smoke'],
        color: const Color(0xFFF59E0B),
      ),
      GuardianPreset(
        presetKey: 'ACTIVE_SUPPORT',
        name: 'Active Support',
        description: 'Emergency alerts + recall assistance + schedule reminders + pattern monitoring.',
        detailsBullets: ['Medical emergencies', 'Wake words (always active)', 'Recall assistance'],
        color: const Color(0xFF14B8A6),
      ),
      GuardianPreset(
        presetKey: 'MAXIMUM_AWARENESS',
        name: 'Maximum Awareness',
        description: 'Maximum monitoring — all conversations and activities.',
        detailsBullets: ['All emergency alerts', 'All conversations monitored', 'Full pattern tracking'],
        color: const Color(0xFF6366F1),
      ),
      GuardianPreset(
        presetKey: 'CUSTOM',
        name: 'Custom',
        description: 'User-configured rules and notification preferences.',
        detailsBullets: ['Customizable alerts', 'Configurable thresholds'],
        color: const Color(0xFF8B5CF6),
      ),
      GuardianPreset(
        presetKey: 'CYBORG',
        name: 'Cyborg',
        description: 'Continuous real-time audio response — every utterance gets an Ella reply in your ear.',
        detailsBullets: ['All utterances processed', 'Real-time audio responses', 'Continuous conversation context'],
        color: const Color(0xFFEC4899),
      ),
    ];
