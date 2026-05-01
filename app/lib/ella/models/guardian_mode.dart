import 'package:flutter/material.dart';

/// Guardian mode preset keys as returned by the API.
enum GuardianModeKey {
  emergencyOnly,
  activeSupport,
  maximumAwareness,
  custom,
  cyborg,
  chatbot,
  off,
  demo,
  memorySupport;

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
      case 'CHATBOT':
        return GuardianModeKey.chatbot;
      case 'OFF':
        return GuardianModeKey.off;
      case 'DEMO':
        return GuardianModeKey.demo;
      case 'MEMORY_SUPPORT':
        return GuardianModeKey.memorySupport;
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
      case GuardianModeKey.chatbot:
        return 'CHATBOT';
      case GuardianModeKey.off:
        return 'OFF';
      case GuardianModeKey.demo:
        return 'DEMO';
      case GuardianModeKey.memorySupport:
        return 'MEMORY_SUPPORT';
    }
  }

  /// Returns true if this key is an "Intelligence Mode" override (exclusive,
  /// replaces care system entirely).
  bool get isOverride =>
      this == GuardianModeKey.cyborg || this == GuardianModeKey.chatbot || this == GuardianModeKey.demo;

  /// Returns true if this key is a composable "Care Feature" checkbox.
  bool get isCareFeature =>
      this == GuardianModeKey.emergencyOnly ||
      this == GuardianModeKey.activeSupport ||
      this == GuardianModeKey.maximumAwareness ||
      this == GuardianModeKey.memorySupport;
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

/// Two-tier guardian mode state: an optional exclusive override (Intelligence
/// Mode) and a set of composable care features.
class GuardianModeState {
  /// Nullable — 'CYBORG' | 'CHATBOT' | 'DEMO' | null
  final String? override;

  /// List of active care feature keys e.g. ['ACTIVE_SUPPORT', 'MEMORY_SUPPORT']
  final List<String> features;

  const GuardianModeState({this.override, this.features = const []});

  bool get isOff => override == null && features.isEmpty;

  /// Build from new-schema API response {override, features} or legacy {mode}.
  factory GuardianModeState.fromJson(Map<String, dynamic> json) {
    if (json.containsKey('features')) {
      final rawFeatures = json['features'];
      return GuardianModeState(
        override: json['override'] as String?,
        features: rawFeatures is List ? List<String>.from(rawFeatures) : const [],
      );
    }
    // Legacy schema: {mode: 'ACTIVE_SUPPORT'}
    final modeStr = json['mode'] as String? ?? json['currentMode'] as String? ?? '';
    final key = GuardianModeKey.fromString(modeStr);
    if (key.isOverride) {
      return GuardianModeState(override: key.toApiString());
    }
    if (key == GuardianModeKey.off) {
      return const GuardianModeState();
    }
    return GuardianModeState(features: [key.toApiString()]);
  }

  Map<String, dynamic> toJson() => {'override': override, 'features': features};
}

class GuardianModeInfo {
  final GuardianModeKey currentMode;
  final GuardianPreset? preset;
  final DateTime? updatedAt;

  /// New two-tier state (may be null when server returns legacy schema).
  final GuardianModeState? twoTierState;

  /// When true, the Demo intelligence mode option should be shown in the picker.
  final bool showDemo;

  const GuardianModeInfo({
    required this.currentMode,
    this.preset,
    this.updatedAt,
    this.twoTierState,
    this.showDemo = false,
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

    GuardianModeState? twoTierState;
    if (json.containsKey('features') || json.containsKey('override')) {
      twoTierState = GuardianModeState.fromJson(json);
    } else if (json.containsKey('mode')) {
      twoTierState = GuardianModeState.fromJson(json);
    }

    return GuardianModeInfo(
      currentMode: GuardianModeKey.fromString(modeStr),
      preset: preset,
      updatedAt: updatedAt,
      twoTierState: twoTierState,
      showDemo: json['showDemo'] as bool? ?? false,
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
      case GuardianModeKey.chatbot:
        return 'Chatbot';
      case GuardianModeKey.off:
        return 'Off';
      case GuardianModeKey.demo:
        return 'Demo';
      case GuardianModeKey.memorySupport:
        return 'Memory Support';
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
      case GuardianModeKey.chatbot:
        return const Color(0xFFF97316);
      case GuardianModeKey.off:
        return const Color(0xFF6B7280);
      case GuardianModeKey.demo:
        return const Color(0xFF3B82F6);
      case GuardianModeKey.memorySupport:
        return const Color(0xFF10B981);
    }
  }
}

enum GuardianVoicePolicy {
  matchActiveProvider,
  pinnedProvider,
  fallbackOnly;

  static GuardianVoicePolicy fromString(String? value) {
    switch ((value ?? '').toLowerCase()) {
      case 'pinned_provider':
        return GuardianVoicePolicy.pinnedProvider;
      case 'fallback_only':
        return GuardianVoicePolicy.fallbackOnly;
      case 'match_active_provider':
      default:
        return GuardianVoicePolicy.matchActiveProvider;
    }
  }

  String toApiString() {
    switch (this) {
      case GuardianVoicePolicy.matchActiveProvider:
        return 'match_active_provider';
      case GuardianVoicePolicy.pinnedProvider:
        return 'pinned_provider';
      case GuardianVoicePolicy.fallbackOnly:
        return 'fallback_only';
    }
  }

  String get displayName {
    switch (this) {
      case GuardianVoicePolicy.matchActiveProvider:
        return 'Match active voice provider';
      case GuardianVoicePolicy.pinnedProvider:
        return 'Pinned provider';
      case GuardianVoicePolicy.fallbackOnly:
        return 'Fallback only';
    }
  }
}

class GuardianVoiceConfig {
  final GuardianVoicePolicy policy;
  final String? provider;
  final String? lastVoiceProvider;
  final String? resolvedProvider;
  final String? fallbackProvider;
  final String? traceId;
  final String? queueItemId;

  const GuardianVoiceConfig({
    this.policy = GuardianVoicePolicy.matchActiveProvider,
    this.provider,
    this.lastVoiceProvider,
    this.resolvedProvider,
    this.fallbackProvider,
    this.traceId,
    this.queueItemId,
  });

  factory GuardianVoiceConfig.fromJson(Map<String, dynamic> json) {
    final data = json['data'] is Map<String, dynamic>
        ? json['data'] as Map<String, dynamic>
        : json['voice_config'] is Map<String, dynamic>
            ? json['voice_config'] as Map<String, dynamic>
            : json['config'] is Map<String, dynamic>
                ? json['config'] as Map<String, dynamic>
                : json;

    return GuardianVoiceConfig(
      policy: GuardianVoicePolicy.fromString(
        data['guardian_voice_policy'] as String? ?? data['policy'] as String?,
      ),
      provider: _readString(data, 'guardian_voice_provider', 'provider'),
      lastVoiceProvider: _readString(data, 'last_voice_provider', 'lastVoiceProvider'),
      resolvedProvider: _readString(data, 'resolved_provider', 'resolvedProvider'),
      fallbackProvider: _readString(data, 'fallback_provider', 'fallbackProvider'),
      traceId: _readString(data, 'trace_id', 'traceId'),
      queueItemId: _readString(data, 'queue_item_id', 'queueItemId'),
    );
  }

  Map<String, dynamic> toJson({required String uid}) {
    return {
      'uid': uid,
      'guardian_voice_policy': policy.toApiString(),
      'guardian_voice_provider': policy == GuardianVoicePolicy.pinnedProvider ? provider : null,
    };
  }

  GuardianVoiceConfig copyWith({
    GuardianVoicePolicy? policy,
    String? provider,
    String? lastVoiceProvider,
    String? resolvedProvider,
    String? fallbackProvider,
    String? traceId,
    String? queueItemId,
  }) {
    return GuardianVoiceConfig(
      policy: policy ?? this.policy,
      provider: provider ?? this.provider,
      lastVoiceProvider: lastVoiceProvider ?? this.lastVoiceProvider,
      resolvedProvider: resolvedProvider ?? this.resolvedProvider,
      fallbackProvider: fallbackProvider ?? this.fallbackProvider,
      traceId: traceId ?? this.traceId,
      queueItemId: queueItemId ?? this.queueItemId,
    );
  }

  bool samePersistedValue(GuardianVoiceConfig other) {
    return policy == other.policy && _normalizedProvider(provider) == _normalizedProvider(other.provider);
  }

  static String? _readString(Map<String, dynamic> json, String snakeKey, String camelKey) {
    final value = json[snakeKey] ?? json[camelKey];
    if (value is String && value.trim().isNotEmpty) return value;
    return null;
  }

  static String? _normalizedProvider(String? value) {
    final normalized = value?.trim();
    return normalized == null || normalized.isEmpty ? null : normalized;
  }
}
