bool _readBool(dynamic value, {bool defaultValue = true}) {
  if (value is bool) return value;
  if (value is num) return value != 0;
  if (value is String) {
    final normalized = value.toLowerCase().trim();
    if (normalized == 'true') return true;
    if (normalized == 'false') return false;
  }
  return defaultValue;
}

class Caregiver {
  final String id;
  final String name;
  final String? phone;
  final String? email;
  final String relationship;
  final String status; // normalized lowercase: "active" or "invited"
  final DateTime? joinedAt;
  final DateTime? invitedAt;
  final DateTime? inviteExpiresAt;
  final bool receiveDailySummary;

  Caregiver({
    required this.id,
    required this.name,
    this.phone,
    this.email,
    required this.relationship,
    required this.status,
    this.joinedAt,
    this.invitedAt,
    this.inviteExpiresAt,
    this.receiveDailySummary = true,
  });

  Caregiver.fromJson(Map<String, dynamic> json)
      : id = json['id'] ?? '',
        name = json['name'] ?? '',
        phone = json['phone'],
        email = json['email'],
        relationship = json['relationship'] ?? '',
        status = (json['status'] ?? 'invited').toString().toLowerCase(),
        joinedAt = json['joined_at'] != null
            ? DateTime.parse(json['joined_at'])
            : (json['accepted_at'] != null ? DateTime.parse(json['accepted_at']) : null),
        invitedAt = json['invited_at'] != null ? DateTime.parse(json['invited_at']) : null,
        inviteExpiresAt = json['invite_expires_at'] != null ? DateTime.parse(json['invite_expires_at']) : null,
        receiveDailySummary = (json['permissions'] as Map<String, dynamic>?)?['receive_daily_summary'] as bool? ?? true;

  String get initial => name.isNotEmpty ? name[0].toUpperCase() : '?';

  String get displayRelationship {
    switch (relationship) {
      case 'daughter':
        return 'Daughter';
      case 'son':
        return 'Son';
      case 'spouse':
        return 'Spouse';
      case 'sibling':
        return 'Sibling';
      case 'friend':
        return 'Friend';
      case 'doctor':
        return 'Doctor';
      case 'other':
        return 'Other';
      default:
        return relationship;
    }
  }

  bool get isActive => status.toLowerCase() == 'active';
  bool get isInvited => status.toLowerCase() == 'invited';
  bool get isExpired => isInvited && inviteExpiresAt != null && inviteExpiresAt!.isBefore(DateTime.now());
}

class InviteResponse {
  final String inviteId;
  final String caregiverId;
  final String inviteCode;
  final String status;
  final DateTime expiresAt;
  final bool emailSent;
  final String? deliveryError;
  final String? failureReason;

  InviteResponse.fromJson(Map<String, dynamic> json)
      : inviteId = json['invite_id'] ?? '',
        caregiverId = json['caregiver_id'] ?? json['caregiverId'] ?? json['id'] ?? '',
        inviteCode = json['invite_code'] ?? '',
        status = json['status'] ?? '',
        expiresAt = json['expires_at'] != null
            ? DateTime.parse(json['expires_at'])
            : DateTime.now().add(const Duration(days: 7)),
        emailSent = _readBool(json['email_sent'] ?? json['emailSent']),
        deliveryError =
            json['delivery_error']?.toString() ?? json['email_error']?.toString() ?? json['error']?.toString(),
        failureReason = json['failure_reason']?.toString() ?? json['reason']?.toString();

  bool get hasInviteRecovery => inviteCode.isNotEmpty || caregiverId.isNotEmpty || inviteId.isNotEmpty;
  bool get emailDeliveryFailed => !emailSent;
}

class CaregiverApiException implements Exception {
  final int statusCode;
  final String message;
  final InviteResponse? inviteResponse;
  CaregiverApiException({required this.statusCode, required this.message, this.inviteResponse});

  @override
  String toString() => 'CaregiverApiException($statusCode): $message';
}
