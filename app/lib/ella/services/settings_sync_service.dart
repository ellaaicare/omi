import 'dart:convert';

import 'package:flutter/foundation.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/platform/platform_manager.dart';

const _voiceModeDirtyKey = 'ellaSettingsVoiceModeDirty';
const _pendingVoiceModeKey = 'ellaSettingsPendingVoiceMode';
const _lastSyncedVoiceModeKey = 'ellaSettingsLastSyncedVoiceMode';
const _lastSyncedAtKey = 'ellaSettingsLastSyncedAt';
const _lastSyncErrorKey = 'ellaSettingsLastSyncError';

class EllaSettingsSyncResponse {
  const EllaSettingsSyncResponse({required this.statusCode, required this.body});

  final int statusCode;
  final Map<String, dynamic> body;

  bool get isSuccess => statusCode >= 200 && statusCode < 300;
}

abstract class EllaSettingsSyncTransport {
  Future<EllaSettingsSyncResponse?> get(String path);

  Future<EllaSettingsSyncResponse?> patch(String path, Map<String, dynamic> payload);
}

class _EllaSettingsSyncHttpTransport implements EllaSettingsSyncTransport {
  @override
  Future<EllaSettingsSyncResponse?> get(String path) async {
    final response = await makeApiCall(
      url: '${Env.apiBaseUrl}$path',
      headers: {'Content-Type': 'application/json'},
      method: 'GET',
      body: '',
      timeout: const Duration(seconds: 10),
      retries: 0,
    );
    if (response == null) return null;
    return EllaSettingsSyncResponse(statusCode: response.statusCode, body: _decodeBody(response.body));
  }

  @override
  Future<EllaSettingsSyncResponse?> patch(String path, Map<String, dynamic> payload) async {
    final response = await makeApiCall(
      url: '${Env.apiBaseUrl}$path',
      headers: {'Content-Type': 'application/json'},
      method: 'PATCH',
      body: jsonEncode(payload),
      timeout: const Duration(seconds: 10),
      retries: 0,
    );
    if (response == null) return null;
    return EllaSettingsSyncResponse(statusCode: response.statusCode, body: _decodeBody(response.body));
  }

  static Map<String, dynamic> _decodeBody(String raw) {
    if (raw.trim().isEmpty) return {};
    try {
      final decoded = jsonDecode(raw);
      if (decoded is Map<String, dynamic>) return decoded;
    } catch (_) {}
    return {};
  }
}

class EllaSettingsSyncService {
  EllaSettingsSyncService._();

  @visibleForTesting
  static EllaSettingsSyncTransport transport = _EllaSettingsSyncHttpTransport();

  static const supportedVoiceModes = {
    'elevenlabs',
    'fish-audio-s2',
    'kokoro',
    'inworld',
    'openclaw-direct',
    'grok-voice',
    'openai-native-realtime',
    'gemini-native-live',
  };

  static String normalizeVoiceMode(String provider) => switch (provider) {
        'gemini-live' => 'gemini-native-live',
        'openai-realtime' => 'openai-native-realtime',
        _ => provider,
      };

  static bool isV2VVoiceMode(String provider) {
    final normalized = normalizeVoiceMode(provider);
    return normalized == 'openclaw-direct' ||
        normalized == 'openai-native-realtime' ||
        normalized == 'grok-voice' ||
        normalized == 'gemini-native-live';
  }

  static String? sessionVoiceMode(String provider) => switch (normalizeVoiceMode(provider)) {
        'openclaw-direct' => 'openclaw-direct-v1',
        'openai-native-realtime' => 'openai-native-realtime-v1',
        'gemini-native-live' => 'gemini-native-live-v1',
        _ => null,
      };

  static bool get hasPendingVoiceMode => SharedPreferencesUtil().getBool(_voiceModeDirtyKey);

  static String get lastSyncedVoiceMode => SharedPreferencesUtil().getString(_lastSyncedVoiceModeKey);

  static DateTime? get lastSyncedAt => DateTime.tryParse(SharedPreferencesUtil().getString(_lastSyncedAtKey));

  static String get lastSyncError => SharedPreferencesUtil().getString(_lastSyncErrorKey);

  /// Fetch server-backed settings and retry any pending local voice-mode change.
  ///
  /// Local SharedPreferences remains the offline source of truth when there is a
  /// dirty local change. Server values only merge into local state when no local
  /// voice mode is waiting to sync.
  static Future<void> syncOnAppStart() async {
    if (!_hasUserAndApiBase) return;

    if (hasPendingVoiceMode) {
      await retryPendingVoiceMode();
      if (hasPendingVoiceMode) return;
    }

    final serverVoiceMode = await _fetchServerVoiceMode();
    if (serverVoiceMode == null) return;

    final local = normalizeVoiceMode(SharedPreferencesUtil().ttsProvider);
    if (serverVoiceMode != local) {
      Logger.debug('[SettingsSync] Applying server voice mode: $serverVoiceMode (was $local)');
      SharedPreferencesUtil().ttsProvider = serverVoiceMode;
    }
    await _markSynced(serverVoiceMode);
  }

  static Future<bool> setVoiceMode(String provider) async {
    final normalized = normalizeVoiceMode(provider);
    if (!supportedVoiceModes.contains(normalized)) {
      Logger.debug('[SettingsSync] Ignoring unsupported voice mode: $provider');
      return false;
    }

    SharedPreferencesUtil().ttsProvider = normalized;
    await _markDirty(normalized);
    return retryPendingVoiceMode();
  }

  static Future<bool> retryPendingVoiceMode() async {
    if (!_hasUserAndApiBase) return false;
    if (!hasPendingVoiceMode) return true;

    final prefs = SharedPreferencesUtil();
    final pending = normalizeVoiceMode(
      prefs.getString(_pendingVoiceModeKey, defaultValue: prefs.ttsProvider),
    );
    if (!supportedVoiceModes.contains(pending)) {
      await prefs.saveString(_lastSyncErrorKey, 'unsupported_pending_voice_mode:$pending');
      return false;
    }

    final payload = buildVoiceSettingsPatchPayload(voiceMode: pending);
    try {
      final response = await transport.patch('v1/ella/settings', payload);
      if (response != null && response.isSuccess) {
        await _markSynced(pending);
        Logger.debug('[SettingsSync] Synced voice mode: $pending');
        return true;
      }

      final status = response?.statusCode ?? 0;
      await prefs.saveString(_lastSyncErrorKey, 'patch_status:$status');
      Logger.debug('[SettingsSync] Voice mode sync failed: status=$status');
      return false;
    } catch (e) {
      await prefs.saveString(_lastSyncErrorKey, 'patch_error:$e');
      Logger.debug('[SettingsSync] Voice mode sync error: $e');
      return false;
    }
  }

  @visibleForTesting
  static Map<String, dynamic> buildVoiceSettingsPatchPayload({
    required String voiceMode,
    DateTime? updatedAt,
    String? clientVersion,
  }) {
    final normalized = normalizeVoiceMode(voiceMode);
    final now = (updatedAt ?? DateTime.now()).toUtc().toIso8601String();
    final version = clientVersion ?? _clientVersion;
    final voice = {
      'voice_mode': normalized,
      'tts_provider': normalized,
      'conversation_provider': normalized,
      'uses_v2v_session': isV2VVoiceMode(normalized),
      if (sessionVoiceMode(normalized) != null) 'session_voice_mode': sessionVoiceMode(normalized),
      'source_client': 'ios-app',
      'source_setting': 'devTtsProvider',
      'client_version': version,
      'updated_at': now,
    };

    return {
      'voice_mode': normalized,
      'tts_provider': normalized,
      'conversation_provider': normalized,
      'settings': {'voice': voice},
      'source_client': 'ios-app',
      'source_setting': 'devTtsProvider',
      'client_version': version,
      'updated_at': now,
    };
  }

  static Future<String?> _fetchServerVoiceMode() async {
    for (final path in const ['v1/ella/settings/effective', 'v1/ella/settings']) {
      try {
        final response = await transport.get(path);
        if (response == null) continue;
        if (response.statusCode == 404 || response.statusCode == 501) continue;
        if (!response.isSuccess) {
          Logger.debug('[SettingsSync] Settings fetch failed: $path status=${response.statusCode}');
          continue;
        }

        final voiceMode = _extractVoiceMode(response.body);
        if (voiceMode == null) continue;
        return voiceMode;
      } catch (e) {
        Logger.debug('[SettingsSync] Settings fetch error: $path $e');
      }
    }
    return null;
  }

  static String? _extractVoiceMode(Map<String, dynamic> data) {
    final candidates = <Map<String, dynamic>>[data];

    void addMap(dynamic value) {
      if (value is Map<String, dynamic>) candidates.add(value);
    }

    addMap(data['voice']);
    addMap(data['voice_settings']);
    addMap(data['effective_voice_settings']);
    addMap(data['effective']);

    final settings = data['settings'];
    if (settings is Map<String, dynamic>) {
      addMap(settings);
      addMap(settings['voice']);
    }

    for (final candidate in candidates) {
      final raw = candidate['voice_mode'] ?? candidate['tts_provider'] ?? candidate['selected_voice_mode'];
      if (raw is! String || raw.trim().isEmpty) continue;
      final normalized = normalizeVoiceMode(raw.trim());
      if (supportedVoiceModes.contains(normalized)) return normalized;
      Logger.debug('[SettingsSync] Server returned unsupported voice mode: $raw');
    }
    return null;
  }

  static Future<void> _markDirty(String voiceMode) async {
    final prefs = SharedPreferencesUtil();
    await prefs.saveString(_pendingVoiceModeKey, voiceMode);
    await prefs.saveBool(_voiceModeDirtyKey, true);
  }

  static Future<void> _markSynced(String voiceMode) async {
    final prefs = SharedPreferencesUtil();
    final now = DateTime.now().toUtc().toIso8601String();
    await prefs.saveString(_lastSyncedVoiceModeKey, voiceMode);
    await prefs.saveString(_lastSyncedAtKey, now);
    await prefs.saveString(_lastSyncErrorKey, '');
    await prefs.saveString(_pendingVoiceModeKey, voiceMode);
    await prefs.saveBool(_voiceModeDirtyKey, false);
  }

  static bool get _hasUserAndApiBase => SharedPreferencesUtil().uid.isNotEmpty && (Env.apiBaseUrl ?? '').isNotEmpty;

  static String get _clientVersion {
    try {
      return PlatformManager.instance.appVersion;
    } catch (_) {
      return 'unknown';
    }
  }
}
