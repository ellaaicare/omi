import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';

/// Channel status entry from the escalation policy.
class ChannelStatus {
  final String channel;
  final bool enabled;
  final String reason;

  const ChannelStatus({
    required this.channel,
    required this.enabled,
    required this.reason,
  });

  factory ChannelStatus.fromJson(Map<String, dynamic> json) {
    return ChannelStatus(
      channel: json['channel'] as String? ?? '',
      enabled: json['enabled'] as bool? ?? false,
      reason: json['reason'] as String? ?? '',
    );
  }

  /// Human-readable channel name.
  String get displayName {
    switch (channel) {
      case 'guardian_audio':
        return 'Guardian Audio';
      case 'imessage':
        return 'iMessage';
      case 'email':
        return 'Email';
      default:
        return channel.replaceAll('_', ' ').split(' ').map((w) => w[0].toUpperCase() + w.substring(1)).join(' ');
    }
  }

  IconData get icon {
    switch (channel) {
      case 'guardian_audio':
        return Icons.volume_up;
      case 'imessage':
        return Icons.message;
      case 'email':
        return Icons.email;
      default:
        return Icons.notifications;
    }
  }
}

/// Emergency contact section from the escalation policy.
class EmergencyContactPolicy {
  final bool configured;
  final String? caregiverId;
  final String? displayName;
  final String? status;
  final String text;

  const EmergencyContactPolicy({
    required this.configured,
    this.caregiverId,
    this.displayName,
    this.status,
    required this.text,
  });

  factory EmergencyContactPolicy.fromJson(Map<String, dynamic> json) {
    return EmergencyContactPolicy(
      configured: json['configured'] as bool? ?? false,
      caregiverId: json['caregiver_id'] as String?,
      displayName: json['display_name'] as String?,
      status: json['status'] as String?,
      text: json['text'] as String? ?? '',
    );
  }
}

/// Caregiver permissions from the escalation policy.
class CaregiverPermissions {
  final bool emergencyAlerts;
  final bool dailySummary;
  final bool weeklySummary;

  const CaregiverPermissions({
    required this.emergencyAlerts,
    required this.dailySummary,
    required this.weeklySummary,
  });

  factory CaregiverPermissions.fromJson(Map<String, dynamic> json) {
    return CaregiverPermissions(
      emergencyAlerts: json['emergency_alerts'] as bool? ?? json['urgent_alerts'] as bool? ?? false,
      dailySummary: json['daily_summary'] as bool? ?? false,
      weeklySummary: json['weekly_summary'] as bool? ?? false,
    );
  }
}

/// Per-caregiver policy view from the escalation policy.
class CaregiverPolicyView {
  final String caregiverId;
  final String displayName;
  final String? relationship;
  final String status;
  final bool isEmergencyContact;
  final List<ChannelStatus> channels;
  final CaregiverPermissions permissions;
  final String plainLanguage;

  const CaregiverPolicyView({
    required this.caregiverId,
    required this.displayName,
    this.relationship,
    required this.status,
    required this.isEmergencyContact,
    required this.channels,
    required this.permissions,
    required this.plainLanguage,
  });

  factory CaregiverPolicyView.fromJson(Map<String, dynamic> json) {
    return CaregiverPolicyView(
      caregiverId: json['caregiver_id'] as String? ?? '',
      displayName: json['display_name'] as String? ?? 'Caregiver',
      relationship: json['relationship'] as String?,
      status: json['status'] as String? ?? 'UNKNOWN',
      isEmergencyContact: json['is_emergency_contact'] as bool? ?? false,
      channels: (json['channels'] as List<dynamic>? ?? [])
          .map((c) => ChannelStatus.fromJson(c as Map<String, dynamic>))
          .toList(),
      permissions: CaregiverPermissions.fromJson(json['permissions'] as Map<String, dynamic>? ?? {}),
      plainLanguage: json['plain_language'] as String? ?? '',
    );
  }
}

/// Single severity/decision rule from the escalation policy.
class EscalationRule {
  final String severity;
  final String decision;
  final String title;
  final String text;

  const EscalationRule({
    required this.severity,
    required this.decision,
    required this.title,
    required this.text,
  });

  factory EscalationRule.fromJson(Map<String, dynamic> json) {
    return EscalationRule(
      severity: json['severity'] as String? ?? 'low',
      decision: json['decision'] as String? ?? 'log_only',
      title: json['title'] as String? ?? '',
      text: json['text'] as String? ?? '',
    );
  }

  /// Severity color for UI display.
  Color get severityColor {
    switch (severity) {
      case 'critical':
        return EllaColors.error;
      case 'high':
        return const Color(0xFFF59E0B); // amber
      case 'medium':
        return EllaColors.primary;
      default:
        return EllaColors.textTertiary;
    }
  }

  /// Human-readable decision name.
  String get decisionLabel {
    switch (decision) {
      case 'notify_now':
        return 'Notify Immediately';
      case 'ask_user_first':
        return 'Ask You First';
      case 'queue_for_report':
        return 'Include in Report';
      case 'log_only':
        return 'Log Only';
      default:
        return decision.replaceAll('_', ' ').split(' ').map((w) => w[0].toUpperCase() + w.substring(1)).join(' ');
    }
  }
}

/// Display helper section from the escalation policy.
class EscalationPolicyDisplay {
  final String title;
  final String subtitle;
  final String emergencyContact;
  final List<String> rules;

  const EscalationPolicyDisplay({
    required this.title,
    required this.subtitle,
    required this.emergencyContact,
    required this.rules,
  });

  factory EscalationPolicyDisplay.fromJson(Map<String, dynamic> json) {
    return EscalationPolicyDisplay(
      title: json['title'] as String? ?? 'How Ella handles alerts',
      subtitle: json['subtitle'] as String? ?? '',
      emergencyContact: json['emergency_contact'] as String? ?? '',
      rules: (json['rules'] as List<dynamic>? ?? []).map((r) => r as String).toList(),
    );
  }
}

/// Top-level escalation policy model.
class EscalationPolicy {
  final String policyVersion;
  final String uid;
  final List<ChannelStatus> userChannels;
  final String? guardianMode;
  final EmergencyContactPolicy emergencyContact;
  final List<CaregiverPolicyView> caregivers;
  final List<EscalationRule> rules;
  final List<String> privacyNotes;
  final EscalationPolicyDisplay display;
  final String generatedAt;

  const EscalationPolicy({
    required this.policyVersion,
    required this.uid,
    required this.userChannels,
    this.guardianMode,
    required this.emergencyContact,
    required this.caregivers,
    required this.rules,
    required this.privacyNotes,
    required this.display,
    required this.generatedAt,
  });

  factory EscalationPolicy.fromJson(Map<String, dynamic> json) {
    final user = json['user'] as Map<String, dynamic>? ?? {};
    return EscalationPolicy(
      policyVersion: json['policy_version'] as String? ?? 'unknown',
      uid: json['uid'] as String? ?? '',
      userChannels: (user['channels'] as List<dynamic>? ?? [])
          .map((c) => ChannelStatus.fromJson(c as Map<String, dynamic>))
          .toList(),
      guardianMode: user['guardian_mode'] as String?,
      emergencyContact: EmergencyContactPolicy.fromJson(json['emergency_contact'] as Map<String, dynamic>? ?? {}),
      caregivers: (json['caregivers'] as List<dynamic>? ?? [])
          .map((c) => CaregiverPolicyView.fromJson(c as Map<String, dynamic>))
          .toList(),
      rules: (json['rules'] as List<dynamic>? ?? [])
          .map((r) => EscalationRule.fromJson(r as Map<String, dynamic>))
          .toList(),
      privacyNotes: (json['privacy_notes'] as List<dynamic>? ?? []).map((n) => n as String).toList(),
      display: EscalationPolicyDisplay.fromJson(json['display'] as Map<String, dynamic>? ?? {}),
      generatedAt: json['generated_at'] as String? ?? '',
    );
  }
}
