import 'package:flutter/material.dart';

/// Guardian mode preset keys as returned by the API.
enum GuardianModeKey {
  emergencyOnly,
  activeSupport,
  maximumAwareness,
  custom,
  cyborg;

  static GuardianModeKey fromString(String value) {
    switch (value.toUpperCase()) {
      case 'EMERGENCY_ONLY':
        return GuardianModeKey.emergencyOnly;
      case 'ACTIVE_SUPPORT':
        return GuardianModeKey.activeSupport;
      case 'MAXIMUM_AWARENESS':
        return GuardianModeKey.maximumAwareness;
      case 'CUSTOM':
        return GuardianModeKey.custom;
      case 'CYBORG':
        return GuardianModeKey.cyborg;
      default:
        return GuardianModeKey.activeSupport;
    }
  }

  String toApiString() {
    switch (this) {
      case GuardianModeKey.emergencyOnly:
        return 'EMERGENCY_ONLY';
      case GuardianModeKey.activeSupport:
        return 'ACTIVE_SUPPORT';
      case GuardianModeKey.maximumAwareness:
        return 'MAXIMUM_AWARENESS';
      case GuardianModeKey.custom:
        return 'CUSTOM';
      case GuardianModeKey.cyborg:
        return 'CYBORG';
    }
  }
}

class GuardianPreset {
  final String presetKey;
  final String name;
  final String description;
  final List<String> detailsBullets;
  final Color color;

  const GuardianPreset({
    required this.presetKey,
    required this.name,
    required this.description,
    required this.detailsBullets,
    required this.color,
  });

  factory GuardianPreset.fromJson(Map<String, dynamic> json) {
    Color color;
    try {
      final hex = (json['color'] as String).replaceFirst('#', '');
      color = Color(int.parse('FF$hex', radix: 16));
    } catch (_) {
      color = const Color(0xFF14B8A6);
    }

    final bullets = json['detailsBullets'];
    return GuardianPreset(
      presetKey: json['presetKey'] as String? ?? '',
      name: json['name'] as String? ?? '',
      description: json['description'] as String? ?? '',
      detailsBullets: bullets is List ? List<String>.from(bullets) : const [],
      color: color,
    );
  }

  GuardianModeKey get modeKey => GuardianModeKey.fromString(presetKey);
}

class GuardianModeInfo {
  final GuardianModeKey currentMode;
  final GuardianPreset? preset;
  final DateTime? updatedAt;

  const GuardianModeInfo({
    required this.currentMode,
    this.preset,
    this.updatedAt,
  });

  factory GuardianModeInfo.fromJson(Map<String, dynamic> json) {
    final modeStr = json['currentMode'] as String? ?? 'ACTIVE_SUPPORT';
    GuardianPreset? preset;
    if (json['preset'] != null) {
      preset = GuardianPreset.fromJson(json['preset'] as Map<String, dynamic>);
    }
    DateTime? updatedAt;
    if (json['updatedAt'] != null) {
      updatedAt = DateTime.tryParse(json['updatedAt'] as String);
    }
    return GuardianModeInfo(
      currentMode: GuardianModeKey.fromString(modeStr),
      preset: preset,
      updatedAt: updatedAt,
    );
  }

  String get displayName => preset?.name ?? _fallbackName(currentMode);

  Color get color => preset?.color ?? _fallbackColor(currentMode);

  static String _fallbackName(GuardianModeKey mode) {
    switch (mode) {
      case GuardianModeKey.emergencyOnly:
        return 'Emergency Only';
      case GuardianModeKey.activeSupport:
        return 'Active Support';
      case GuardianModeKey.maximumAwareness:
        return 'Maximum Awareness';
      case GuardianModeKey.custom:
        return 'Custom';
      case GuardianModeKey.cyborg:
        return 'Cyborg';
    }
  }

  static Color _fallbackColor(GuardianModeKey mode) {
    switch (mode) {
      case GuardianModeKey.emergencyOnly:
        return const Color(0xFFF59E0B);
      case GuardianModeKey.activeSupport:
        return const Color(0xFF14B8A6);
      case GuardianModeKey.maximumAwareness:
        return const Color(0xFF6366F1);
      case GuardianModeKey.custom:
        return const Color(0xFF8B5CF6);
      case GuardianModeKey.cyborg:
        return const Color(0xFFEC4899);
    }
  }
}
