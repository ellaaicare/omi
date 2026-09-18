import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import 'package:omi/ella/models/imessage_enrollment.dart';

class ImessagePendingStartAttempt {
  const ImessagePendingStartAttempt({
    required this.ownerUid,
    required this.handsetE164,
    required this.consentRequestId,
    required this.policy,
    required this.appVersion,
    required this.buildNumber,
    required this.idempotencyKey,
    this.receipt,
  });

  final String ownerUid;
  final String handsetE164;
  final String consentRequestId;
  final ImessageConsentPolicy policy;
  final String appVersion;
  final String buildNumber;
  final String idempotencyKey;
  final ImessageConsentReceipt? receipt;

  ImessagePendingStartAttempt withReceipt(ImessageConsentReceipt value) {
    return ImessagePendingStartAttempt(
      ownerUid: ownerUid,
      handsetE164: handsetE164,
      consentRequestId: consentRequestId,
      policy: policy,
      appVersion: appVersion,
      buildNumber: buildNumber,
      idempotencyKey: idempotencyKey,
      receipt: value,
    );
  }

  Map<String, dynamic> toJson() => {
        'owner_uid': ownerUid,
        'handset_e164': handsetE164,
        'consent_request_id': consentRequestId,
        'policy': _policyToJson(policy),
        'app_version': appVersion,
        'build_number': buildNumber,
        'idempotency_key': idempotencyKey,
        if (receipt != null) 'receipt': _receiptToJson(receipt!),
      };

  factory ImessagePendingStartAttempt.fromJson(Map<String, dynamic> json) {
    final handset = _requiredString(json, 'handset_e164');
    if (!RegExp(r'^\+[1-9][0-9]{7,14}$').hasMatch(handset)) {
      throw const FormatException('Invalid handset');
    }
    return ImessagePendingStartAttempt(
      ownerUid: _requiredString(json, 'owner_uid'),
      handsetE164: handset,
      consentRequestId: _requiredString(json, 'consent_request_id'),
      policy: ImessageConsentPolicy.fromJson(_requiredMap(json, 'policy')),
      appVersion: _requiredString(json, 'app_version'),
      buildNumber: _requiredString(json, 'build_number'),
      idempotencyKey: _requiredString(json, 'idempotency_key'),
      receipt: json['receipt'] == null ? null : ImessageConsentReceipt.fromJson(_requiredMap(json, 'receipt')),
    );
  }
}

class ImessagePendingBindingRevoke {
  const ImessagePendingBindingRevoke({
    required this.ownerUid,
    required this.expectedGeneration,
    required this.idempotencyKey,
  });

  final String ownerUid;
  final int expectedGeneration;
  final String idempotencyKey;

  Map<String, dynamic> toJson() => {
        'owner_uid': ownerUid,
        'expected_generation': expectedGeneration,
        'idempotency_key': idempotencyKey,
      };

  factory ImessagePendingBindingRevoke.fromJson(Map<String, dynamic> json) {
    final generation = json['expected_generation'];
    if (generation is! int || generation < 1) throw const FormatException('Invalid authority generation');
    return ImessagePendingBindingRevoke(
      ownerUid: _requiredString(json, 'owner_uid'),
      expectedGeneration: generation,
      idempotencyKey: _requiredString(json, 'idempotency_key'),
    );
  }
}

class ImessagePendingConsentRevoke {
  const ImessagePendingConsentRevoke({
    required this.ownerUid,
    required this.requestId,
    required this.policy,
    required this.appVersion,
    required this.buildNumber,
  });

  final String ownerUid;
  final String requestId;
  final ImessageConsentPolicy policy;
  final String appVersion;
  final String buildNumber;

  Map<String, dynamic> toJson() => {
        'owner_uid': ownerUid,
        'request_id': requestId,
        'policy': _policyToJson(policy),
        'app_version': appVersion,
        'build_number': buildNumber,
      };

  factory ImessagePendingConsentRevoke.fromJson(Map<String, dynamic> json) {
    return ImessagePendingConsentRevoke(
      ownerUid: _requiredString(json, 'owner_uid'),
      requestId: _requiredString(json, 'request_id'),
      policy: ImessageConsentPolicy.fromJson(_requiredMap(json, 'policy')),
      appVersion: _requiredString(json, 'app_version'),
      buildNumber: _requiredString(json, 'build_number'),
    );
  }
}

class ImessageEnrollmentAttemptJournal {
  const ImessageEnrollmentAttemptJournal({
    required this.ownerUid,
    this.start,
    this.bindingRevoke,
    this.consentRevoke,
  });

  static const schema = 'ella.imessage_enrollment_attempts.v1';

  final String ownerUid;
  final ImessagePendingStartAttempt? start;
  final ImessagePendingBindingRevoke? bindingRevoke;
  final ImessagePendingConsentRevoke? consentRevoke;

  bool get isEmpty => start == null && bindingRevoke == null && consentRevoke == null;

  Map<String, dynamic> toJson() => {
        'schema_version': schema,
        'owner_uid': ownerUid,
        if (start != null) 'start': start!.toJson(),
        if (bindingRevoke != null) 'binding_revoke': bindingRevoke!.toJson(),
        if (consentRevoke != null) 'consent_revoke': consentRevoke!.toJson(),
      };

  factory ImessageEnrollmentAttemptJournal.fromJson(Map<String, dynamic> json) {
    if (_requiredString(json, 'schema_version') != schema) {
      throw const FormatException('Unsupported iMessage attempt journal');
    }
    final ownerUid = _requiredString(json, 'owner_uid');
    final start = json['start'] == null ? null : ImessagePendingStartAttempt.fromJson(_requiredMap(json, 'start'));
    final bindingRevoke = json['binding_revoke'] == null
        ? null
        : ImessagePendingBindingRevoke.fromJson(_requiredMap(json, 'binding_revoke'));
    final consentRevoke = json['consent_revoke'] == null
        ? null
        : ImessagePendingConsentRevoke.fromJson(_requiredMap(json, 'consent_revoke'));
    if (start?.ownerUid != null && start?.ownerUid != ownerUid ||
        bindingRevoke?.ownerUid != null && bindingRevoke?.ownerUid != ownerUid ||
        consentRevoke?.ownerUid != null && consentRevoke?.ownerUid != ownerUid) {
      throw const FormatException('Cross-owner iMessage attempt journal');
    }
    return ImessageEnrollmentAttemptJournal(
      ownerUid: ownerUid,
      start: start,
      bindingRevoke: bindingRevoke,
      consentRevoke: consentRevoke,
    );
  }
}

abstract interface class ImessageEnrollmentAttemptStore {
  Future<ImessageEnrollmentAttemptJournal?> read(String ownerUid);

  Future<void> write(ImessageEnrollmentAttemptJournal journal);

  Future<void> clear(String ownerUid);
}

class ImessageEnrollmentMemoryAttemptStore implements ImessageEnrollmentAttemptStore {
  final Map<String, ImessageEnrollmentAttemptJournal> _journals = {};

  @override
  Future<ImessageEnrollmentAttemptJournal?> read(String ownerUid) async => _journals[ownerUid];

  @override
  Future<void> write(ImessageEnrollmentAttemptJournal journal) async {
    _journals[journal.ownerUid] = journal;
  }

  @override
  Future<void> clear(String ownerUid) async {
    _journals.remove(ownerUid);
  }
}

class ImessageEnrollmentSecureAttemptStore implements ImessageEnrollmentAttemptStore {
  ImessageEnrollmentSecureAttemptStore({FlutterSecureStorage? storage})
      : _storage = storage ??
            const FlutterSecureStorage(
              iOptions: IOSOptions(
                accountName: 'com.ellaaicare.ella.imessage-enrollment',
                accessibility: KeychainAccessibility.unlocked_this_device,
                synchronizable: false,
              ),
            );

  final FlutterSecureStorage _storage;

  @override
  Future<ImessageEnrollmentAttemptJournal?> read(String ownerUid) async {
    final key = _key(ownerUid);
    final encoded = await _storage.read(key: key);
    if (encoded == null) return null;
    try {
      final decoded = jsonDecode(encoded);
      if (decoded is! Map<String, dynamic>) throw const FormatException('Invalid iMessage attempt journal');
      final journal = ImessageEnrollmentAttemptJournal.fromJson(decoded);
      if (journal.ownerUid != ownerUid) throw const FormatException('Mismatched iMessage attempt owner');
      return journal;
    } on FormatException {
      await _storage.delete(key: key);
      return null;
    }
  }

  @override
  Future<void> write(ImessageEnrollmentAttemptJournal journal) {
    return _storage.write(key: _key(journal.ownerUid), value: jsonEncode(journal.toJson()));
  }

  @override
  Future<void> clear(String ownerUid) => _storage.delete(key: _key(ownerUid));

  String _key(String ownerUid) {
    final digest = sha256.convert(utf8.encode(ownerUid));
    return 'ella.imessage.enrollment.attempts.$digest';
  }
}

Map<String, dynamic> _policyToJson(ImessageConsentPolicy policy) => {
      'policy_version': policy.policyVersion,
      'processor_set_hash': policy.processorSetHash,
      'scope_version': policy.scopeVersion,
      'scope_hash': policy.scopeHash,
      'recipients': policy.recipients,
      'data_classes': policy.dataClasses,
      'text_dm_only': policy.textDmOnly,
    };

Map<String, dynamic> _receiptToJson(ImessageConsentReceipt receipt) => {
      'schema_version': receipt.schemaVersion,
      'receipt_id': receipt.receiptId,
      'decision': receipt.decision.wireValue,
      'policy_version': receipt.policyVersion,
      'processor_set_hash': receipt.processorSetHash,
      'scope_version': receipt.scopeVersion,
      'scope_hash': receipt.scopeHash,
      'authority_revision': receipt.authorityRevision,
      'decided_at': receipt.decidedAt.toUtc().toIso8601String(),
    };

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
