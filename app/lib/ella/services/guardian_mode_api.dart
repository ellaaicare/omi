import 'dart:convert';

import 'package:flutter/material.dart' show Color;
import 'package:http/http.dart' as http;
import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/models/guardian_mode.dart';
import 'package:omi/ella/services/ella_public_surface_policy.dart';
import 'package:omi/ella/services/ella_service_result.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/logger.dart';

const String _dashboardBase = 'https://ella-ai-care.com';

/// In-memory cache for presets (they change rarely).
List<GuardianPreset>? _cachedPresets;

String get _authenticatedUserId => SharedPreferencesUtil().uid.trim();

typedef GuardianModeTransport = Future<http.Response?> Function({
  required String url,
  required String method,
  required String body,
  required String expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier exactAuthority,
});

Future<http.Response?> _defaultGuardianModeTransport({
  required String url,
  required String method,
  required String body,
  required String expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier exactAuthority,
}) =>
    makeApiCall(
      url: url,
      headers: const {'Content-Type': 'application/json'},
      body: body,
      method: method,
      timeout: const Duration(seconds: 10),
      retries: 0,
      requireAuthCheck: true,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      exactAuthority: exactAuthority,
    );

({String uid, String url, ExactAccountAuthorityVerifier authority})? _guardianModeRequestContext({
  ExactAccountAuthorityVerifier? exactAuthority,
}) {
  final uid = _authenticatedUserId;
  final baseUrl = Env.apiBaseUrl;
  if (uid.isEmpty || baseUrl == null || baseUrl.isEmpty) return null;
  final authority = exactAuthority ?? WalOwnerAuthority.active();
  if (authority == null || authority.uid != uid || !authority.isExactCurrent()) return null;
  return (uid: uid, url: '${baseUrl}v1/ella/guardian/mode', authority: authority);
}

/// GET /api/users/{userId}/guardian-mode
///
/// Returns a [GuardianModeInfo] that includes a [GuardianModeState] for the
/// two-tier picker.  Handles both the new schema {override, features} and the
/// legacy schema {mode}.
Future<EllaServiceResult<GuardianModeInfo>> getGuardianMode({
  bool? guardianAllowed,
  GuardianModeTransport? transport,
  ExactAccountAuthorityVerifier? exactAuthority,
}) async {
  if (!(guardianAllowed ?? allowsGuardianSurface())) {
    return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.featureUnavailable));
  }
  final context = _guardianModeRequestContext(exactAuthority: exactAuthority);
  if (context == null) {
    return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.authenticationRequired));
  }
  try {
    final response = await (transport ?? _defaultGuardianModeTransport)(
      url: context.url,
      method: 'GET',
      body: '',
      expectedAuthenticatedUid: context.uid,
      exactAuthority: context.authority,
    );

    if (response != null && response.statusCode == 200) {
      final data = jsonDecode(response.body) as Map<String, dynamic>;
      if (data['success'] == true) {
        return EllaServiceResult.success(GuardianModeInfo.fromJson(data));
      }
    }
    Logger.debug('getGuardianMode: ${response?.statusCode}');
    return EllaServiceResult.failure(
      response == null
          ? const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true)
          : ClientApiFailure.fromHttp(statusCode: response.statusCode, body: response.body),
    );
  } on ClientApiFailure catch (failure) {
    return EllaServiceResult.failure(failure);
  } on ExactAccountAuthorityChangedException {
    return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.accountChanged));
  } catch (e) {
    Logger.debug('getGuardianMode error: $e');
    return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.invalidResponse));
  }
}

/// PUT /api/users/{userId}/guardian-mode
///
/// Sends the new two-tier body:
///   { "override": "CYBORG" | "CHATBOT" | "DEMO" | null, "features": [...] }
Future<EllaServiceResult<void>> setGuardianModeTwoTier(
  GuardianModeState state, {
  bool? guardianAllowed,
  GuardianModeTransport? transport,
  ExactAccountAuthorityVerifier? exactAuthority,
}) async {
  if (!(guardianAllowed ?? allowsGuardianSurface())) {
    return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.featureUnavailable));
  }
  final context = _guardianModeRequestContext(exactAuthority: exactAuthority);
  if (context == null) {
    return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.authenticationRequired));
  }
  try {
    final response = await (transport ?? _defaultGuardianModeTransport)(
      url: context.url,
      method: 'PUT',
      body: jsonEncode(state.toJson()),
      expectedAuthenticatedUid: context.uid,
      exactAuthority: context.authority,
    );

    if (response != null && response.statusCode == 200) {
      final data = jsonDecode(response.body) as Map<String, dynamic>;
      if (data['success'] == true) return const EllaServiceResult.success();
      return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.invalidResponse));
    }
    Logger.debug('setGuardianMode: ${response?.statusCode}');
    return EllaServiceResult.failure(
      response == null
          ? const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true)
          : ClientApiFailure.fromHttp(statusCode: response.statusCode, body: response.body),
    );
  } on ClientApiFailure catch (failure) {
    return EllaServiceResult.failure(failure);
  } on ExactAccountAuthorityChangedException {
    return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.accountChanged));
  } catch (e) {
    Logger.debug('setGuardianMode error: $e');
    return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.invalidResponse));
  }
}

/// Legacy single-mode PUT — kept for callers that haven't migrated yet.
Future<EllaServiceResult<void>> setGuardianMode(
  GuardianModeKey mode, {
  bool? guardianAllowed,
  GuardianModeTransport? transport,
  ExactAccountAuthorityVerifier? exactAuthority,
}) =>
    setGuardianModeTwoTier(
      mode == GuardianModeKey.off
          ? const GuardianModeState()
          : mode.isOverride
              ? GuardianModeState(override: mode.toApiString())
              : GuardianModeState(features: [mode.toApiString()]),
      guardianAllowed: guardianAllowed,
      transport: transport,
      exactAuthority: exactAuthority,
    );

/// GET /api/guardian/presets  (no auth required, cached in memory)
Future<List<GuardianPreset>> getGuardianPresets({bool? guardianAllowed, http.Client? client}) async {
  if (!(guardianAllowed ?? allowsGuardianSurface())) return const [];
  if (_cachedPresets != null) return _cachedPresets!;
  final transport = client ?? http.Client();
  try {
    final response = await transport.get(Uri.parse('$_dashboardBase/api/guardian/presets'),
        headers: {'Content-Type': 'application/json'}).timeout(const Duration(seconds: 10));

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
  } finally {
    if (client == null) transport.close();
  }
  return _fallbackPresets();
}

/// Hard-coded fallback so the UI works even if the presets endpoint is down.
List<GuardianPreset> _fallbackPresets() => [
      GuardianPreset(
        presetKey: 'EMERGENCY_ONLY',
        name: 'Emergency Alerts',
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
        description:
            'High-sensitivity care monitoring for risk, vulnerability, health, cognitive, emotional, and social support signals.',
        detailsBullets: ['Broad care monitoring', 'Lower alert thresholds', 'Helpful during outings or recovery'],
        color: const Color(0xFF6366F1),
      ),
      GuardianPreset(
        presetKey: 'MEMORY_SUPPORT',
        name: 'Memory Support',
        description: 'Proactive memory cues and gentle recall assistance for cognitive support.',
        detailsBullets: ['Memory cues', 'Daily routine reminders', 'Cognitive pattern monitoring'],
        color: const Color(0xFF10B981),
      ),
      GuardianPreset(
        presetKey: 'CYBORG',
        name: 'Cyborg',
        description:
            'Experimental ambient intelligence — Ella listens broadly and speaks only when she can add useful context, coaching, memory, or insight.',
        detailsBullets: ['Ambient world enhancement', 'Useful companion asides', 'Experimental brain-enhancer mode'],
        color: const Color(0xFFEC4899),
      ),
      GuardianPreset(
        presetKey: 'CHATBOT',
        name: 'Chatbot',
        description: 'Full two-way voice conversation with Ella, focused on primary-speaker user utterances.',
        detailsBullets: ['Conversational replies', 'Suppresses media/background audio', 'Best for direct voice chat'],
        color: const Color(0xFFF97316),
      ),
      GuardianPreset(
        presetKey: 'DEMO',
        name: 'Demo',
        description: 'Demonstration mode with scripted responses for showcasing Ella.',
        detailsBullets: ['Scripted demo responses', 'Showcase mode'],
        color: const Color(0xFF3B82F6),
      ),
    ];
