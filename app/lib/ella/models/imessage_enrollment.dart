enum ImessageConsentDecision {
  granted('granted'),
  declined('declined'),
  revoked('revoked');

  const ImessageConsentDecision(this.wireValue);

  final String wireValue;

  static ImessageConsentDecision fromWireValue(String value) {
    return values.firstWhere(
      (decision) => decision.wireValue == value,
      orElse: () => throw FormatException('Unknown iMessage consent decision: $value'),
    );
  }
}

class ImessageConsentPolicy {
  const ImessageConsentPolicy({
    required this.policyVersion,
    required this.processorSetHash,
    required this.scopeVersion,
    required this.scopeHash,
    required this.recipients,
    required this.dataClasses,
    required this.textDmOnly,
  });

  static const supportedPolicyVersion = 'ella-imessage-data-v1';
  static const supportedScopeVersion = 'ella.imessage_text_dm.v1';

  final String policyVersion;
  final String processorSetHash;
  final String scopeVersion;
  final String scopeHash;
  final List<String> recipients;
  final List<String> dataClasses;
  final bool textDmOnly;

  factory ImessageConsentPolicy.fromJson(Map<String, dynamic> json) {
    final policyVersion = _requiredString(json, 'policy_version');
    final scopeVersion = _requiredString(json, 'scope_version');
    final textDmOnly = _requiredBool(json, 'text_dm_only');
    if (policyVersion != supportedPolicyVersion || scopeVersion != supportedScopeVersion || !textDmOnly) {
      throw const FormatException('Unsupported iMessage consent policy');
    }

    return ImessageConsentPolicy(
      policyVersion: policyVersion,
      processorSetHash: _requiredSha256(json, 'processor_set_hash'),
      scopeVersion: scopeVersion,
      scopeHash: _requiredSha256(json, 'scope_hash'),
      recipients: _requiredStringList(json, 'recipients', minimumLength: 2),
      dataClasses: _requiredStringList(json, 'data_classes'),
      textDmOnly: textDmOnly,
    );
  }
}

class ImessageConsentReceipt {
  const ImessageConsentReceipt({
    required this.schemaVersion,
    required this.receiptId,
    required this.decision,
    required this.policyVersion,
    required this.processorSetHash,
    required this.scopeVersion,
    required this.scopeHash,
    required this.authorityRevision,
    required this.decidedAt,
  });

  static const schema = 'ella.imessage_consent_receipt.v1';

  final String schemaVersion;
  final String receiptId;
  final ImessageConsentDecision decision;
  final String policyVersion;
  final String processorSetHash;
  final String scopeVersion;
  final String scopeHash;
  final int authorityRevision;
  final DateTime decidedAt;

  bool matches(ImessageConsentPolicy policy, ImessageConsentDecision expectedDecision) {
    return decision == expectedDecision &&
        policyVersion == policy.policyVersion &&
        processorSetHash == policy.processorSetHash &&
        scopeVersion == policy.scopeVersion &&
        scopeHash == policy.scopeHash;
  }

  factory ImessageConsentReceipt.fromJson(Map<String, dynamic> json) {
    final schemaVersion = _requiredString(json, 'schema_version');
    if (schemaVersion != schema) throw FormatException('Unsupported iMessage consent receipt: $schemaVersion');

    return ImessageConsentReceipt(
      schemaVersion: schemaVersion,
      receiptId: _requiredString(json, 'receipt_id'),
      decision: ImessageConsentDecision.fromWireValue(_requiredString(json, 'decision')),
      policyVersion: _requiredString(json, 'policy_version'),
      processorSetHash: _requiredSha256(json, 'processor_set_hash'),
      scopeVersion: _requiredString(json, 'scope_version'),
      scopeHash: _requiredSha256(json, 'scope_hash'),
      authorityRevision: _requiredInt(json, 'authority_revision'),
      decidedAt: _requiredDateTime(json, 'decided_at'),
    );
  }
}

enum ImessageEnrollmentState {
  notConnected('not_connected'),
  verificationPending('verification_pending'),
  ready('ready'),
  temporarilyUnavailable('temporarily_unavailable'),
  revoked('revoked');

  const ImessageEnrollmentState(this.wireValue);

  final String wireValue;

  static ImessageEnrollmentState fromWireValue(String value) {
    return values.firstWhere(
      (state) => state.wireValue == value,
      orElse: () => throw FormatException('Unknown iMessage enrollment state: $value'),
    );
  }
}

enum ImessageEnrollmentReason {
  notEnrolled('not_enrolled'),
  rolloutDisabled('rollout_disabled'),
  verificationPending('verification_pending'),
  ready('ready'),
  transportUnhealthy('transport_unhealthy'),
  authorityStale('authority_stale'),
  bindingQuarantined('binding_quarantined'),
  consentRequired('consent_required'),
  consentRevoked('consent_revoked'),
  runtimeUnavailable('runtime_unavailable'),
  bindingRevoked('binding_revoked');

  const ImessageEnrollmentReason(this.wireValue);

  final String wireValue;

  static ImessageEnrollmentReason fromWireValue(String value) {
    return values.firstWhere(
      (reason) => reason.wireValue == value,
      orElse: () => throw FormatException('Unknown iMessage enrollment reason: $value'),
    );
  }
}

class ImessageEnrollmentFeatures {
  const ImessageEnrollmentFeatures({
    required this.textDm,
    required this.groups,
    required this.attachments,
    required this.caregiverDelivery,
  });

  final bool textDm;
  final bool groups;
  final bool attachments;
  final bool caregiverDelivery;

  factory ImessageEnrollmentFeatures.fromJson(Map<String, dynamic> json) {
    return ImessageEnrollmentFeatures(
      textDm: _requiredBool(json, 'text_dm'),
      groups: _requiredBool(json, 'groups'),
      attachments: _requiredBool(json, 'attachments'),
      caregiverDelivery: _requiredBool(json, 'caregiver_delivery'),
    );
  }
}

class ImessageEnrollmentStatus {
  const ImessageEnrollmentStatus({
    required this.schemaVersion,
    required this.state,
    required this.reason,
    required this.authorityGeneration,
    required this.features,
    this.bindingRevision,
    this.bindingFingerprint,
    this.assignedDestination,
    this.lastVerifiedAt,
    this.verificationExpiresAt,
    this.supportCode,
  });

  static const schema = 'ella.imessage_enrollment.v1';

  final String schemaVersion;
  final ImessageEnrollmentState state;
  final ImessageEnrollmentReason reason;
  final int authorityGeneration;
  final int? bindingRevision;
  final String? bindingFingerprint;
  final String? assignedDestination;
  final DateTime? lastVerifiedAt;
  final DateTime? verificationExpiresAt;
  final String? supportCode;
  final ImessageEnrollmentFeatures features;

  bool get isReady => state == ImessageEnrollmentState.ready && features.textDm;

  factory ImessageEnrollmentStatus.fromJson(Map<String, dynamic> json) {
    final schemaVersion = _requiredString(json, 'schema_version');
    if (schemaVersion != schema) {
      throw FormatException('Unsupported iMessage enrollment schema: $schemaVersion');
    }

    return ImessageEnrollmentStatus(
      schemaVersion: schemaVersion,
      state: ImessageEnrollmentState.fromWireValue(_requiredString(json, 'state')),
      reason: ImessageEnrollmentReason.fromWireValue(_requiredString(json, 'reason_code')),
      authorityGeneration: _requiredInt(json, 'authority_generation'),
      bindingRevision: _optionalInt(json, 'binding_revision'),
      bindingFingerprint: _optionalString(json, 'binding_fingerprint'),
      assignedDestination: _optionalString(json, 'assigned_destination'),
      lastVerifiedAt: _optionalDateTime(json, 'last_verified_at'),
      verificationExpiresAt: _optionalDateTime(json, 'verification_expires_at'),
      supportCode: _optionalString(json, 'support_code'),
      features: ImessageEnrollmentFeatures.fromJson(_requiredMap(json, 'features')),
    );
  }
}

class ImessageEnrollmentProof {
  const ImessageEnrollmentProof({required this.code, required this.expiresAt});

  final String code;
  final DateTime expiresAt;

  bool isExpiredAt(DateTime time) => !expiresAt.isAfter(time);

  factory ImessageEnrollmentProof.fromJson(Map<String, dynamic> json) {
    return ImessageEnrollmentProof(
      code: _requiredString(json, 'code'),
      expiresAt: _requiredDateTime(json, 'expires_at'),
    );
  }
}

class ImessageEnrollmentStartResponse {
  const ImessageEnrollmentStartResponse({required this.status, required this.proof});

  final ImessageEnrollmentStatus status;
  final ImessageEnrollmentProof proof;

  factory ImessageEnrollmentStartResponse.fromJson(Map<String, dynamic> json) {
    return ImessageEnrollmentStartResponse(
      status: ImessageEnrollmentStatus.fromJson(_requiredMap(json, 'status')),
      proof: ImessageEnrollmentProof.fromJson(_requiredMap(json, 'proof')),
    );
  }
}

Map<String, dynamic> _requiredMap(Map<String, dynamic> json, String key) {
  final value = json[key];
  if (value is! Map<String, dynamic>) throw FormatException('Missing object: $key');
  return value;
}

String _requiredString(Map<String, dynamic> json, String key) {
  final value = json[key];
  if (value is! String || value.isEmpty) throw FormatException('Missing string: $key');
  return value;
}

String? _optionalString(Map<String, dynamic> json, String key) {
  final value = json[key];
  if (value == null) return null;
  if (value is! String || value.isEmpty) throw FormatException('Invalid string: $key');
  return value;
}

int _requiredInt(Map<String, dynamic> json, String key) {
  final value = json[key];
  if (value is! int) throw FormatException('Missing integer: $key');
  return value;
}

int? _optionalInt(Map<String, dynamic> json, String key) {
  final value = json[key];
  if (value == null) return null;
  if (value is! int) throw FormatException('Invalid integer: $key');
  return value;
}

bool _requiredBool(Map<String, dynamic> json, String key) {
  final value = json[key];
  if (value is! bool) throw FormatException('Missing boolean: $key');
  return value;
}

String _requiredSha256(Map<String, dynamic> json, String key) {
  final value = _requiredString(json, key);
  if (!RegExp(r'^sha256:[a-f0-9]{64}$').hasMatch(value)) throw FormatException('Invalid SHA-256: $key');
  return value;
}

List<String> _requiredStringList(Map<String, dynamic> json, String key, {int minimumLength = 1}) {
  final value = json[key];
  if (value is! List || value.length < minimumLength || value.any((item) => item is! String || item.isEmpty)) {
    throw FormatException('Invalid string list: $key');
  }
  return List<String>.unmodifiable(value.cast<String>());
}

DateTime _requiredDateTime(Map<String, dynamic> json, String key) {
  final value = _requiredString(json, key);
  final parsed = DateTime.tryParse(value);
  if (parsed == null || !parsed.isUtc) throw FormatException('Invalid UTC date-time: $key');
  return parsed;
}

DateTime? _optionalDateTime(Map<String, dynamic> json, String key) {
  final value = json[key];
  if (value == null) return null;
  if (value is! String) throw FormatException('Invalid date-time: $key');
  final parsed = DateTime.tryParse(value);
  if (parsed == null || !parsed.isUtc) throw FormatException('Invalid UTC date-time: $key');
  return parsed;
}
