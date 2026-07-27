import 'dart:convert';
import 'dart:ui';

import 'package:uuid/uuid.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_policy.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/platform/platform_manager.dart';

enum AiConsentDecision {
  granted,
  declined,
  revoked;

  String get wireValue => name;
}

class AiConsentStatus {
  const AiConsentStatus({
    required this.subjectUid,
    required this.authorized,
    required this.policy,
    required this.decision,
    required this.receiptId,
    required this.policyVersion,
    required this.processorSetHash,
    required this.appVersion,
    required this.buildNumber,
    required this.locale,
  });

  factory AiConsentStatus.fromJson(Map<String, dynamic> json) {
    final consent =
        json['consent'] is Map<String, dynamic> ? json['consent'] as Map<String, dynamic> : const <String, dynamic>{};
    final receipt =
        json['receipt'] is Map<String, dynamic> ? json['receipt'] as Map<String, dynamic> : const <String, dynamic>{};
    final policy = json['policy'] is Map<String, dynamic>
        ? AiConsentPolicy.fromJson(json['policy'] as Map<String, dynamic>)
        : null;
    return AiConsentStatus(
      subjectUid: json['subject_uid'] as String? ?? '',
      authorized: json['authorized'] as bool? ?? false,
      policy: policy,
      decision: receipt['decision'] as String? ?? consent['decision'] as String? ?? '',
      receiptId: receipt['receipt_id'] as String? ?? consent['receipt_id'] as String? ?? '',
      policyVersion: receipt['policy_version'] as String? ?? consent['policy_version'] as String? ?? '',
      processorSetHash: receipt['processor_set_hash'] as String? ?? consent['processor_set_hash'] as String? ?? '',
      appVersion: receipt['app_version'] as String? ?? consent['app_version'] as String? ?? '',
      buildNumber: receipt['build_number'] as String? ?? consent['build_number'] as String? ?? '',
      locale: receipt['locale'] as String? ?? consent['locale'] as String? ?? '',
    );
  }

  final String subjectUid;
  final bool authorized;
  final AiConsentPolicy? policy;
  final String decision;
  final String receiptId;
  final String policyVersion;
  final String processorSetHash;
  final String appVersion;
  final String buildNumber;
  final String locale;

  bool isCurrentGrantFor(String uid) {
    return uid.isNotEmpty &&
        subjectUid == uid &&
        authorized &&
        decision == AiConsentDecision.granted.wireValue &&
        receiptId.startsWith(SharedPreferencesUtil.currentAiConsentReceiptPrefix) &&
        policyVersion == SharedPreferencesUtil.currentAiConsentContractVersion &&
        processorSetHash == SharedPreferencesUtil.currentAiConsentProcessorSetHash &&
        (policy?.isBundledCurrent ?? false);
  }
}

class AiConsentSubmission {
  const AiConsentSubmission({
    required this.decision,
    required this.policyVersion,
    required this.processorSetHash,
    required this.requestId,
    required this.appVersion,
    required this.buildNumber,
    required this.locale,
  });

  final AiConsentDecision decision;
  final String policyVersion;
  final String processorSetHash;
  final String requestId;
  final String appVersion;
  final String buildNumber;
  final String locale;

  Map<String, dynamic> toJson() => {
        'decision': decision.wireValue,
        'policy_version': policyVersion,
        'processor_set_hash': processorSetHash,
        'request_id': requestId,
        'app_version': appVersion,
        'build_number': buildNumber,
        'locale': locale,
      };
}

abstract class EllaAiConsentTransport {
  Future<AiConsentPolicy?> fetchPolicy();

  Future<AiConsentStatus?> fetchStatus();

  Future<AiConsentStatus?> submit(AiConsentSubmission submission);
}

class EllaAiConsentHttpTransport implements EllaAiConsentTransport {
  const EllaAiConsentHttpTransport();

  static String get _endpoint => '${Env.apiBaseUrl}v1/users/ai-consent';

  static Map<String, dynamic>? _decodeMap(String body) {
    try {
      final decoded = jsonDecode(body);
      return decoded is Map<String, dynamic> ? decoded : null;
    } on FormatException {
      return null;
    }
  }

  @override
  Future<AiConsentPolicy?> fetchPolicy() async {
    final response = await makeApiCall(
      url: '$_endpoint/policy',
      headers: const {},
      method: 'GET',
      body: '',
      requireAuthCheck: false,
    );
    if (response?.statusCode != 200) return null;
    final body = _decodeMap(response!.body);
    return body == null ? null : AiConsentPolicy.fromJson(body);
  }

  @override
  Future<AiConsentStatus?> fetchStatus() async {
    final response = await makeApiCall(
      url: _endpoint,
      headers: const {},
      method: 'GET',
      body: '',
    );
    if (response?.statusCode != 200) return null;
    final body = _decodeMap(response!.body);
    return body == null ? null : AiConsentStatus.fromJson(body);
  }

  @override
  Future<AiConsentStatus?> submit(AiConsentSubmission submission) async {
    final response = await makeApiCall(
      url: _endpoint,
      headers: const {},
      method: 'POST',
      body: jsonEncode(submission.toJson()),
    );
    if (response?.statusCode != 200) return null;
    final body = _decodeMap(response!.body);
    return body == null ? null : AiConsentStatus.fromJson(body);
  }
}

class EllaAiConsentService {
  EllaAiConsentService({
    EllaAiConsentTransport? transport,
    SharedPreferencesUtil? preferences,
    String Function()? requestIdFactory,
    String Function()? clientVersionFactory,
    String Function()? localeFactory,
  })  : _transport = transport ?? const EllaAiConsentHttpTransport(),
        _preferences = preferences ?? SharedPreferencesUtil(),
        _requestIdFactory = requestIdFactory ?? (() => const Uuid().v4()),
        _clientVersionFactory = clientVersionFactory ?? (() => PlatformManager.instance.appVersion),
        _localeFactory = localeFactory ?? (() => PlatformDispatcher.instance.locale.toLanguageTag());

  final EllaAiConsentTransport _transport;
  final SharedPreferencesUtil _preferences;
  final String Function() _requestIdFactory;
  final String Function() _clientVersionFactory;
  final String Function() _localeFactory;

  Future<bool> refreshServerAuthority({required String uid}) async {
    if (uid.isEmpty || _preferences.uid != uid) {
      SharedPreferencesUtil.clearAiConsentServerVerification();
      return false;
    }

    try {
      final policy = await _fetchAcceptedPolicy();
      if (policy == null) {
        SharedPreferencesUtil.clearAiConsentServerVerification();
        return false;
      }

      final status = await _transport.fetchStatus();
      if (status == null || status.policy?.processorSetHash != policy.processorSetHash) {
        SharedPreferencesUtil.clearAiConsentServerVerification();
        return false;
      }
      if (!status.isCurrentGrantFor(uid)) {
        if (status.subjectUid == uid && status.decision == AiConsentDecision.declined.wireValue) {
          _preferences.deferAiConsent();
        } else if (status.subjectUid == uid) {
          _preferences.declineAiConsent();
        } else {
          SharedPreferencesUtil.clearAiConsentServerVerification();
        }
        return false;
      }

      _persistVerifiedGrant(uid, status);
      return _preferences.aiConsentAccepted;
    } catch (_) {
      SharedPreferencesUtil.clearAiConsentServerVerification();
      return false;
    }
  }

  Future<String?> grantCurrentConsent({required String uid}) async {
    final status = await _submit(uid: uid, decision: AiConsentDecision.granted);
    if (status == null || !status.isCurrentGrantFor(uid)) return null;
    _persistVerifiedGrant(uid, status);
    return _preferences.aiConsentAccepted ? status.receiptId : null;
  }

  Future<bool> declineCurrentConsent({required String uid}) async {
    _preferences.deferAiConsent();
    final status = await _submit(uid: uid, decision: AiConsentDecision.declined);
    return status != null &&
        status.subjectUid == uid &&
        !status.authorized &&
        status.decision == AiConsentDecision.declined.wireValue;
  }

  Future<bool> revokeCurrentConsent({required String uid}) async {
    _preferences.declineAiConsent();
    final status = await _submit(uid: uid, decision: AiConsentDecision.revoked);
    return status != null &&
        status.subjectUid == uid &&
        !status.authorized &&
        status.decision == AiConsentDecision.revoked.wireValue;
  }

  Future<AiConsentStatus?> _submit({
    required String uid,
    required AiConsentDecision decision,
  }) async {
    SharedPreferencesUtil.clearAiConsentServerVerification();
    if (uid.isEmpty || _preferences.uid != uid) return null;
    final policy = await _fetchAcceptedPolicy();
    if (policy == null) return null;

    final fullVersion = _clientVersionFactory();
    final separator = fullVersion.lastIndexOf('+');
    final appVersion = separator > 0 ? fullVersion.substring(0, separator) : fullVersion;
    final buildNumber =
        separator > 0 && separator < fullVersion.length - 1 ? fullVersion.substring(separator + 1) : 'unknown';
    return _transport.submit(
      AiConsentSubmission(
        decision: decision,
        policyVersion: policy.version,
        processorSetHash: policy.processorSetHash,
        requestId: _requestIdFactory(),
        appVersion: appVersion,
        buildNumber: buildNumber,
        locale: _localeFactory(),
      ),
    );
  }

  Future<AiConsentPolicy?> _fetchAcceptedPolicy() async {
    final policy = await _transport.fetchPolicy();
    return policy?.isBundledCurrent == true ? policy : null;
  }

  void _persistVerifiedGrant(String uid, AiConsentStatus status) {
    final clientVersion = status.buildNumber.isEmpty ? status.appVersion : '${status.appVersion}+${status.buildNumber}';
    _preferences.acceptAiConsent(
      receiptId: status.receiptId,
      uid: uid,
      clientVersion: clientVersion,
      locale: status.locale,
    );
    _preferences.markAiConsentServerVerified(
      uid: uid,
      receiptId: status.receiptId,
      policyVersion: status.policyVersion,
      processorSetHash: status.processorSetHash,
    );
  }
}
