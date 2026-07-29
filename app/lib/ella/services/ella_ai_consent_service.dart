import 'dart:convert';
import 'dart:ui';

import 'package:uuid/uuid.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_policy.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/ella_pilot_locale_policy.dart';
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
    required this.profileBindingId,
    required this.scopeVersion,
    required this.scopeHash,
    required this.serverDecidedAt,
  });

  factory AiConsentStatus.fromJson(Map<String, dynamic> json) {
    final consent =
        json['consent'] is Map<String, dynamic> ? json['consent'] as Map<String, dynamic> : const <String, dynamic>{};
    final receipt =
        json['receipt'] is Map<String, dynamic> ? json['receipt'] as Map<String, dynamic> : const <String, dynamic>{};
    String readString(String key) {
      final value = receipt[key] ?? consent[key];
      return value is String ? value : '';
    }

    final policy = json['policy'] is Map<String, dynamic>
        ? AiConsentPolicy.fromJson(json['policy'] as Map<String, dynamic>)
        : null;
    return AiConsentStatus(
      subjectUid: json['subject_uid'] is String ? json['subject_uid'] as String : '',
      authorized: json['authorized'] is bool ? json['authorized'] as bool : false,
      policy: policy,
      decision: readString('decision'),
      receiptId: readString('receipt_id'),
      policyVersion: readString('policy_version'),
      processorSetHash: readString('processor_set_hash'),
      appVersion: readString('app_version'),
      buildNumber: readString('build_number'),
      locale: readString('locale'),
      profileBindingId: readString('profile_binding_id'),
      scopeVersion: readString('scope_version'),
      scopeHash: readString('scope_hash'),
      serverDecidedAt: DateTime.tryParse(readString('server_decided_at')),
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
  final String profileBindingId;
  final String scopeVersion;
  final String scopeHash;
  final DateTime? serverDecidedAt;

  bool isCurrentGrantFor(String uid, {String? expectedProfileBindingId}) {
    return uid.isNotEmpty &&
        subjectUid == uid &&
        authorized &&
        decision == AiConsentDecision.granted.wireValue &&
        receiptId.startsWith(SharedPreferencesUtil.currentAiConsentReceiptPrefix) &&
        policyVersion == SharedPreferencesUtil.currentAiConsentContractVersion &&
        processorSetHash == SharedPreferencesUtil.currentAiConsentProcessorSetHash &&
        profileBindingId.isNotEmpty &&
        (expectedProfileBindingId == null || profileBindingId == expectedProfileBindingId) &&
        scopeVersion == SharedPreferencesUtil.currentAiConsentScopeVersion &&
        scopeHash == SharedPreferencesUtil.currentAiConsentScopeHash &&
        serverDecidedAt != null &&
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
    required this.scopeVersion,
    required this.scopeHash,
  });

  final AiConsentDecision decision;
  final String policyVersion;
  final String processorSetHash;
  final String requestId;
  final String appVersion;
  final String buildNumber;
  final String locale;
  final String scopeVersion;
  final String scopeHash;

  Map<String, dynamic> toJson() => {
        'decision': decision.wireValue,
        'policy_version': policyVersion,
        'processor_set_hash': processorSetHash,
        'request_id': requestId,
        'app_version': appVersion,
        'build_number': buildNumber,
        'locale': locale,
        'scope_version': scopeVersion,
        'scope_hash': scopeHash,
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
    final response = await makeApiCall(url: _endpoint, headers: const {}, method: 'GET', body: '');
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
    bool pilotLocaleRestricted = isEllaInternalPilotEnabled,
    String Function()? appLocaleFactory,
  })  : _transport = transport ?? const EllaAiConsentHttpTransport(),
        _preferences = preferences ?? SharedPreferencesUtil(),
        _requestIdFactory = requestIdFactory ?? (() => const Uuid().v4()),
        _clientVersionFactory = clientVersionFactory ?? (() => PlatformManager.instance.appVersion),
        _localeFactory = localeFactory ?? (() => PlatformDispatcher.instance.locale.toLanguageTag()),
        _pilotLocaleRestricted = pilotLocaleRestricted,
        _appLocaleFactory = appLocaleFactory ?? (() => SharedPreferencesUtil().getString('app_locale'));

  final EllaAiConsentTransport _transport;
  final SharedPreferencesUtil _preferences;
  final String Function() _requestIdFactory;
  final String Function() _clientVersionFactory;
  final String Function() _localeFactory;
  final bool _pilotLocaleRestricted;
  final String Function() _appLocaleFactory;

  Future<bool> refreshServerAuthority({required String uid}) async {
    final authority = _captureAuthority(uid);
    if (authority == null) {
      SharedPreferencesUtil.clearAiConsentServerVerification();
      return false;
    }

    try {
      final policy = await _fetchAcceptedPolicy();
      if (!_requireCurrentAuthority(authority) || policy == null) {
        SharedPreferencesUtil.clearAiConsentServerVerification();
        return false;
      }

      final status = await _transport.fetchStatus();
      if (!_requireCurrentAuthority(authority) ||
          status == null ||
          status.policy?.processorSetHash != policy.processorSetHash) {
        SharedPreferencesUtil.clearAiConsentServerVerification();
        return false;
      }
      final expectedProfileBindingId = authority.profileBindingId;
      if (!status.isCurrentGrantFor(
        uid,
        expectedProfileBindingId: expectedProfileBindingId.isEmpty ? null : expectedProfileBindingId,
      )) {
        if (status.subjectUid == uid && status.decision == AiConsentDecision.declined.wireValue) {
          _preferences.deferAiConsent();
        } else if (status.subjectUid == uid) {
          _preferences.declineAiConsent();
        } else {
          SharedPreferencesUtil.clearAiConsentServerVerification();
        }
        return false;
      }

      return _persistVerifiedGrant(authority, status) && _preferences.aiConsentAccepted;
    } catch (_) {
      SharedPreferencesUtil.clearAiConsentServerVerification();
      return false;
    }
  }

  Future<String?> grantCurrentConsent({required String uid}) async {
    final authority = _captureAuthority(uid);
    if (authority == null) {
      SharedPreferencesUtil.clearAiConsentServerVerification();
      return null;
    }
    final status = await _submit(authority: authority, decision: AiConsentDecision.granted);
    if (!_requireCurrentAuthority(authority) || status == null || !status.isCurrentGrantFor(uid)) return null;
    if (!_persistVerifiedGrant(authority, status)) return null;
    return _preferences.aiConsentAccepted ? status.receiptId : null;
  }

  Future<bool> declineCurrentConsent({required String uid}) async {
    _preferences.deferAiConsent();
    final authority = _captureAuthority(uid);
    if (authority == null) return false;
    final status = await _submit(authority: authority, decision: AiConsentDecision.declined);
    return status != null &&
        status.subjectUid == uid &&
        !status.authorized &&
        status.decision == AiConsentDecision.declined.wireValue;
  }

  Future<bool> revokeCurrentConsent({required String uid}) async {
    _preferences.declineAiConsent();
    final authority = _captureAuthority(uid);
    if (authority == null) return false;
    final status = await _submit(authority: authority, decision: AiConsentDecision.revoked);
    return status != null &&
        status.subjectUid == uid &&
        !status.authorized &&
        status.decision == AiConsentDecision.revoked.wireValue;
  }

  Future<AiConsentStatus?> _submit({
    required _AiConsentAuthority authority,
    required AiConsentDecision decision,
  }) async {
    SharedPreferencesUtil.clearAiConsentServerVerification();
    if (!_requireCurrentAuthority(authority)) return null;
    final policy = await _fetchAcceptedPolicy();
    if (!_requireCurrentAuthority(authority) || policy == null) return null;

    final fullVersion = _clientVersionFactory();
    final separator = fullVersion.lastIndexOf('+');
    final appVersion = separator > 0 ? fullVersion.substring(0, separator) : fullVersion;
    final buildNumber =
        separator > 0 && separator < fullVersion.length - 1 ? fullVersion.substring(separator + 1) : 'unknown';
    final status = await _transport.submit(
      AiConsentSubmission(
        decision: decision,
        policyVersion: policy.version,
        processorSetHash: policy.processorSetHash,
        requestId: _requestIdFactory(),
        appVersion: appVersion,
        buildNumber: buildNumber,
        locale: _localeFactory(),
        scopeVersion: policy.scopeVersion,
        scopeHash: policy.scopeHash,
      ),
    );
    return _requireCurrentAuthority(authority) ? status : null;
  }

  Future<AiConsentPolicy?> _fetchAcceptedPolicy() async {
    final policy = await _transport.fetchPolicy();
    return policy?.isBundledCurrent == true ? policy : null;
  }

  _AiConsentAuthority? _captureAuthority(String uid) {
    if (!canUseEllaInternalPilotLocale(_appLocaleFactory(), pilotEnabled: _pilotLocaleRestricted) ||
        uid.isEmpty ||
        _preferences.uid != uid) {
      return null;
    }
    return _AiConsentAuthority(
      generation: _preferences.aiConsentAuthorityGeneration,
      uid: uid,
      verifiedPersonaId: _preferences.verifiedPersonaId,
      profileBindingId: _preferences.aiConsentProfileBindingId,
    );
  }

  bool _isCurrentAuthority(_AiConsentAuthority authority) {
    return canUseEllaInternalPilotLocale(_appLocaleFactory(), pilotEnabled: _pilotLocaleRestricted) &&
        _preferences.aiConsentAuthorityGeneration == authority.generation &&
        _preferences.uid == authority.uid &&
        _preferences.verifiedPersonaId == authority.verifiedPersonaId &&
        _preferences.aiConsentProfileBindingId == authority.profileBindingId;
  }

  bool _requireCurrentAuthority(_AiConsentAuthority authority) {
    if (_isCurrentAuthority(authority)) return true;
    SharedPreferencesUtil.clearAiConsentServerVerification();
    return false;
  }

  bool _persistVerifiedGrant(_AiConsentAuthority authority, AiConsentStatus status) {
    if (!_requireCurrentAuthority(authority)) return false;
    final clientVersion = status.buildNumber.isEmpty ? status.appVersion : '${status.appVersion}+${status.buildNumber}';
    _preferences.acceptAiConsent(
      receiptId: status.receiptId,
      uid: authority.uid,
      clientVersion: clientVersion,
      locale: status.locale,
      profileBindingId: status.profileBindingId,
      serverDecidedAt: status.serverDecidedAt!.toUtc().toIso8601String(),
    );
    final persistedAuthority = _captureAuthority(authority.uid);
    if (persistedAuthority == null ||
        persistedAuthority.verifiedPersonaId != authority.verifiedPersonaId ||
        persistedAuthority.profileBindingId != status.profileBindingId ||
        !_requireCurrentAuthority(persistedAuthority)) {
      SharedPreferencesUtil.clearAiConsentServerVerification();
      return false;
    }
    _preferences.markAiConsentServerVerified(
      uid: authority.uid,
      receiptId: status.receiptId,
      policyVersion: status.policyVersion,
      processorSetHash: status.processorSetHash,
      profileBindingId: status.profileBindingId,
      scopeVersion: status.scopeVersion,
      scopeHash: status.scopeHash,
    );
    return _requireCurrentAuthority(persistedAuthority);
  }
}

class _AiConsentAuthority {
  const _AiConsentAuthority({
    required this.generation,
    required this.uid,
    required this.verifiedPersonaId,
    required this.profileBindingId,
  });

  final int generation;
  final String uid;
  final String? verifiedPersonaId;
  final String profileBindingId;
}
