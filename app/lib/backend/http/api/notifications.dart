import 'dart:convert';

import 'package:http/http.dart' as http;

import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

enum FcmRegistrationState { unregistered, pending, ready, retryRequired }

class FcmRegistrationStatus {
  const FcmRegistrationStatus({required this.state, this.lastAttemptAt, this.statusCode});

  final FcmRegistrationState state;
  final DateTime? lastAttemptAt;
  final int? statusCode;

  bool get isReady => state == FcmRegistrationState.ready;

  Map<String, dynamic> toJson() => {
        'state': state.name,
        if (lastAttemptAt != null) 'last_attempt_at': lastAttemptAt!.toUtc().toIso8601String(),
        if (statusCode != null) 'status_code': statusCode,
      };

  factory FcmRegistrationStatus.fromJson(Map<String, dynamic> json) {
    final stateName = json['state'] as String? ?? FcmRegistrationState.unregistered.name;
    return FcmRegistrationStatus(
      state: FcmRegistrationState.values.firstWhere(
        (candidate) => candidate.name == stateName,
        orElse: () => FcmRegistrationState.unregistered,
      ),
      lastAttemptAt: DateTime.tryParse(json['last_attempt_at'] as String? ?? ''),
      statusCode: json['status_code'] as int?,
    );
  }

  static FcmRegistrationStatus load(String uid, {SharedPreferencesUtil? preferences}) {
    if (uid.isEmpty) return const FcmRegistrationStatus(state: FcmRegistrationState.unregistered);
    final raw = (preferences ?? SharedPreferencesUtil()).getString('fcmRegistrationStatus:$uid');
    if (raw.isEmpty) return const FcmRegistrationStatus(state: FcmRegistrationState.unregistered);
    try {
      return FcmRegistrationStatus.fromJson(jsonDecode(raw) as Map<String, dynamic>);
    } catch (_) {
      return const FcmRegistrationStatus(state: FcmRegistrationState.retryRequired);
    }
  }
}

class FcmRegistrationResult {
  const FcmRegistrationResult({required this.status, this.failure});

  final FcmRegistrationStatus status;
  final ClientApiFailure? failure;

  bool get isReady => status.isReady && failure == null;
}

typedef FcmRegistrationTransport = Future<http.Response?> Function({
  required String url,
  required String body,
  required String expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier exactAuthority,
});
typedef FcmRegistrationAuthorityProvider = AccountCommitAuthority? Function();

Future<http.Response?> _defaultFcmRegistrationTransport({
  required String url,
  required String body,
  required String expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier exactAuthority,
}) =>
    makeApiCall(
      url: url,
      headers: const {'Content-Type': 'application/json'},
      method: 'POST',
      body: body,
      requireAuthCheck: true,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      exactAuthority: exactAuthority,
      retries: 0,
    );

Future<FcmRegistrationResult> saveFcmTokenServer({
  required String token,
  required String timeZone,
  FcmRegistrationTransport? transport,
  FcmRegistrationAuthorityProvider? authorityProvider,
  SharedPreferencesUtil? preferences,
}) async {
  final prefs = preferences ?? SharedPreferencesUtil();
  final uid = prefs.uid.trim();
  final authority = (authorityProvider ?? WalOwnerAuthority.operationEntry)();
  if (uid.isEmpty || authority == null || authority.uid != uid || !authority.isExactCurrent()) {
    final status = FcmRegistrationStatus(
      state: FcmRegistrationState.retryRequired,
      lastAttemptAt: DateTime.now().toUtc(),
    );
    if (uid.isNotEmpty) await _persistStatus(uid, status, prefs);
    return FcmRegistrationResult(
      status: status,
      failure: const ClientApiFailure(ClientApiFailureKind.authenticationRequired),
    );
  }

  final pending = FcmRegistrationStatus(state: FcmRegistrationState.pending, lastAttemptAt: DateTime.now().toUtc());
  await _persistStatus(uid, pending, prefs);
  try {
    final response = await (transport ?? _defaultFcmRegistrationTransport)(
      url: '${Env.apiBaseUrl}v1/users/fcm-token',
      body: jsonEncode({'fcm_token': token, 'time_zone': timeZone}),
      expectedAuthenticatedUid: uid,
      exactAuthority: authority,
    );
    if (!authority.isExactCurrent()) {
      throw const ClientApiFailure(ClientApiFailureKind.accountChanged);
    }
    if (response != null && response.statusCode == 200) {
      final ready = FcmRegistrationStatus(
        state: FcmRegistrationState.ready,
        lastAttemptAt: pending.lastAttemptAt,
        statusCode: 200,
      );
      await _persistStatus(uid, ready, prefs);
      if (!authority.isExactCurrent()) {
        final retry = FcmRegistrationStatus(
          state: FcmRegistrationState.retryRequired,
          lastAttemptAt: pending.lastAttemptAt,
          statusCode: 200,
        );
        await _persistStatus(uid, retry, prefs);
        return FcmRegistrationResult(
          status: retry,
          failure: const ClientApiFailure(ClientApiFailureKind.accountChanged),
        );
      }
      return FcmRegistrationResult(status: ready);
    }
    final failure = response == null
        ? const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true)
        : ClientApiFailure.fromHttp(statusCode: response.statusCode, body: response.body);
    final retry = FcmRegistrationStatus(
      state: FcmRegistrationState.retryRequired,
      lastAttemptAt: pending.lastAttemptAt,
      statusCode: response?.statusCode,
    );
    await _persistStatus(uid, retry, prefs);
    return FcmRegistrationResult(status: retry, failure: failure);
  } on ClientApiFailure catch (failure) {
    final retry = FcmRegistrationStatus(
      state: FcmRegistrationState.retryRequired,
      lastAttemptAt: pending.lastAttemptAt,
    );
    await _persistStatus(uid, retry, prefs);
    return FcmRegistrationResult(status: retry, failure: failure);
  } on ExactAccountAuthorityChangedException {
    final retry = FcmRegistrationStatus(
      state: FcmRegistrationState.retryRequired,
      lastAttemptAt: pending.lastAttemptAt,
    );
    await _persistStatus(uid, retry, prefs);
    return FcmRegistrationResult(status: retry, failure: const ClientApiFailure(ClientApiFailureKind.accountChanged));
  } catch (_) {
    final retry = FcmRegistrationStatus(
      state: FcmRegistrationState.retryRequired,
      lastAttemptAt: pending.lastAttemptAt,
    );
    await _persistStatus(uid, retry, prefs);
    return FcmRegistrationResult(
      status: retry,
      failure: const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true),
    );
  }
}

Future<void> _persistStatus(String uid, FcmRegistrationStatus status, SharedPreferencesUtil preferences) async {
  await preferences.saveString('fcmRegistrationStatus:$uid', jsonEncode(status.toJson()));
}
