import 'package:flutter/material.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/guardian_mode.dart';
import 'package:omi/ella/pages/ella_demo_scenarios_page.dart';
import 'package:omi/ella/services/guardian_mode_api.dart' as guardian_api;
import 'package:omi/ella/services/v2v_client.dart';

class GuardianModePage extends StatefulWidget {
  /// When true, show the Demo intelligence mode option.
  final bool showDemo;

  const GuardianModePage({super.key, this.showDemo = true});

  @override
  State<GuardianModePage> createState() => _GuardianModePageState();
}

class _GuardianModePageState extends State<GuardianModePage> {
  List<GuardianPreset> _allPresets = [];

  // Current saved state (from server).
  GuardianModeState _currentState = const GuardianModeState(
    features: ['ACTIVE_SUPPORT'],
  );

  // Pending selection state (user has made changes but not saved).
  String? _selectedOverride; // 'CYBORG' | 'DEMO' | null
  Set<String> _selectedFeatures = {'ACTIVE_SUPPORT'}; // composable features

  bool _loading = true;
  bool _saving = false;

  GuardianVoiceConfig _currentVoiceConfig = const GuardianVoiceConfig();
  GuardianVoicePolicy _selectedVoicePolicy = GuardianVoicePolicy.matchActiveProvider;
  String _selectedVoiceProvider = 'openai';

  @override
  void initState() {
    super.initState();
    _loadData();
  }

  Future<void> _loadData() async {
    setState(() => _loading = true);
    final results = await Future.wait([
      guardian_api.getGuardianPresets(),
      guardian_api.getGuardianMode(),
      guardian_api.getGuardianVoiceConfig(),
    ]);
    final presets = results[0] as List<GuardianPreset>;
    final modeInfo = results[1] as GuardianModeInfo?;
    final voiceConfig = results[2] as GuardianVoiceConfig? ?? const GuardianVoiceConfig();

    if (mounted) {
      setState(() {
        _allPresets = presets;

        GuardianModeState state;
        if (modeInfo?.twoTierState != null) {
          state = modeInfo!.twoTierState!;
        } else if (modeInfo != null) {
          // Old server — synthesize two-tier from currentMode.
          final key = modeInfo.currentMode;
          if (key.isOverride) {
            state = GuardianModeState(override: key.toApiString());
          } else if (key == GuardianModeKey.off) {
            state = const GuardianModeState();
          } else {
            state = GuardianModeState(features: [key.toApiString()]);
          }
        } else {
          state = const GuardianModeState(features: ['ACTIVE_SUPPORT']);
        }

        _currentState = state;
        _selectedOverride = state.override;
        _selectedFeatures = Set<String>.from(state.features);
        _currentVoiceConfig = voiceConfig;
        _selectedVoicePolicy = voiceConfig.policy;
        _selectedVoiceProvider = voiceConfig.provider ?? _defaultPinnedVoiceProvider;
        _loading = false;
      });
    }
  }

  GuardianModeState get _pendingState => GuardianModeState(
        override: _selectedOverride,
        features: _selectedOverride != null ? [] : _selectedFeatures.toList(),
      );

  bool get _hasChanges {
    final pending = _pendingState;
    return pending.override != _currentState.override ||
        !_sameFeatures(pending.features, _currentState.features) ||
        !_pendingVoiceConfig.samePersistedValue(_currentVoiceConfig);
  }

  GuardianVoiceConfig get _pendingVoiceConfig => GuardianVoiceConfig(
        policy: _selectedVoicePolicy,
        provider: _selectedVoicePolicy == GuardianVoicePolicy.pinnedProvider ? _selectedVoiceProvider : null,
      );

  String get _defaultPinnedVoiceProvider => _guardianProviderForActiveVoiceProvider ?? 'openai';

  String? get _guardianProviderForActiveVoiceProvider {
    switch (V2VClient.normalizeProvider(SharedPreferencesUtil().ttsProvider)) {
      case 'grok-voice':
        return 'xai-tts';
      case 'gemini-native-live':
      case 'openai-native-realtime':
      case 'openclaw-direct':
        return 'openai';
      case 'elevenlabs':
        return 'elevenlabs';
      case 'kokoro':
        return 'kokoro';
      default:
        return null;
    }
  }

  bool _sameFeatures(List<String> a, List<String> b) {
    if (a.length != b.length) return false;
    final sa = Set<String>.from(a);
    return b.every(sa.contains);
  }

  Future<void> _save() async {
    if (!_hasChanges) return;

    // Confirmation for Maximum Awareness escalation.
    if (_selectedOverride == null &&
        _selectedFeatures.contains('MAXIMUM_AWARENESS') &&
        !_currentState.features.contains('MAXIMUM_AWARENESS')) {
      final confirmed = await _showMaxAwarenessConfirmation();
      if (!confirmed) return;
    }

    setState(() => _saving = true);
    final modeChanged = _pendingState.override != _currentState.override ||
        !_sameFeatures(_pendingState.features, _currentState.features);
    final voiceChanged = !_pendingVoiceConfig.samePersistedValue(_currentVoiceConfig);

    bool modeSuccess = true;
    GuardianVoiceConfig? savedVoiceConfig = _currentVoiceConfig;
    if (modeChanged) {
      modeSuccess = await guardian_api.setGuardianModeTwoTier(_pendingState);
    }
    if (voiceChanged) {
      savedVoiceConfig = await guardian_api.setGuardianVoiceConfig(_pendingVoiceConfig);
    }
    if (!mounted) return;
    setState(() => _saving = false);

    if (modeSuccess && (!voiceChanged || savedVoiceConfig != null)) {
      setState(() {
        if (modeChanged) _currentState = _pendingState;
        if (savedVoiceConfig != null) _currentVoiceConfig = savedVoiceConfig;
      });
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Guardian settings updated'),
          backgroundColor: EllaColors.success,
          behavior: SnackBarBehavior.floating,
          duration: Duration(seconds: 3),
        ),
      );
      Navigator.of(context).pop(_pendingState);
    } else {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Failed to update — please try again'),
          backgroundColor: EllaColors.error,
          behavior: SnackBarBehavior.floating,
        ),
      );
    }
  }

  Future<bool> _showMaxAwarenessConfirmation() async {
    return await showDialog<bool>(
          context: context,
          builder: (ctx) => AlertDialog(
            backgroundColor: EllaColors.bgSecondary,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
            ),
            title: const Text(
              'Maximum Awareness',
              style: TextStyle(
                fontSize: 20,
                fontWeight: FontWeight.w700,
                color: EllaColors.textPrimary,
              ),
            ),
            content: const Text(
              'Maximum Awareness monitors all conversations. '
              'This is recommended during health events. Continue?',
              style: TextStyle(fontSize: 16, color: EllaColors.textSecondary),
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.of(ctx).pop(false),
                child: const Text(
                  'Cancel',
                  style: TextStyle(
                    fontSize: 16,
                    color: EllaColors.textTertiary,
                  ),
                ),
              ),
              TextButton(
                onPressed: () => Navigator.of(ctx).pop(true),
                child: const Text(
                  'Continue',
                  style: TextStyle(
                    fontSize: 16,
                    fontWeight: FontWeight.w600,
                    color: Color(0xFF6366F1),
                  ),
                ),
              ),
            ],
          ),
        ) ??
        false;
  }

  // ── Preset helpers ────────────────────────────────────────────────────────

  GuardianPreset _presetFor(String key) {
    return _allPresets.firstWhere(
      (p) => p.presetKey == key,
      orElse: () => GuardianPreset(
        presetKey: key,
        name: _fallbackLabel(key),
        description: '',
        detailsBullets: const [],
        color: GuardianModeKey.fromString(key).isOverride ? const Color(0xFFEC4899) : const Color(0xFF14B8A6),
      ),
    );
  }

  String _fallbackLabel(String key) {
    switch (key) {
      case 'CYBORG':
        return 'Cyborg';
      case 'CHATBOT':
        return 'Chatbot';
      case 'DEMO':
        return 'Demo';
      case 'EMERGENCY_ONLY':
        return 'Emergency Alerts';
      case 'ACTIVE_SUPPORT':
        return 'Active Support';
      case 'MAXIMUM_AWARENESS':
        return 'Maximum Awareness';
      case 'MEMORY_SUPPORT':
        return 'Memory Support';
      default:
        return key;
    }
  }

  static const Map<String, String> _guardianVoiceProviders = {
    'xai-tts': 'xAI TTS / Grok family',
    'openai': 'OpenAI',
    'elevenlabs': 'ElevenLabs',
    'kokoro': 'Kokoro',
  };

  static String _formatProviderLabel(String? provider) {
    if (provider == null || provider.isEmpty) return 'Unknown';
    return _guardianVoiceProviders[provider] ??
        switch (provider) {
          'grok-voice' => 'Grok Native Realtime',
          'openai-native-realtime' => 'OpenAI Native Realtime',
          'gemini-native-live' => 'Gemini Native Live',
          'openclaw-direct' => 'OpenClaw Direct',
          'fish-audio-s2' => 'Fish Audio S2',
          'inworld' => 'Inworld',
          _ => provider,
        };
  }

  // ── Intelligence mode rows (exclusive radio) ──────────────────────────────

  List<String> get _overrideModes {
    final modes = ['CYBORG', 'CHATBOT'];
    if (widget.showDemo) modes.add('DEMO');
    return modes;
  }

  // ── Care feature rows (composable checkboxes) ─────────────────────────────

  static const List<String> _careFeatureKeys = [
    'EMERGENCY_ONLY',
    'ACTIVE_SUPPORT',
    'MAXIMUM_AWARENESS',
    'MEMORY_SUPPORT',
  ];

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      appBar: AppBar(
        backgroundColor: EllaColors.bgPrimary,
        elevation: 0,
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, color: EllaColors.textPrimary),
          onPressed: () => Navigator.of(context).pop(),
        ),
        title: const Text(
          'Guardian Mode',
          style: TextStyle(
            fontSize: 22,
            fontWeight: FontWeight.w700,
            color: EllaColors.textPrimary,
          ),
        ),
        centerTitle: false,
      ),
      body: _loading
          ? const Center(
              child: CircularProgressIndicator(color: EllaColors.primary),
            )
          : Column(
              children: [
                Expanded(
                  child: ListView(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 16,
                      vertical: 8,
                    ),
                    children: [
                      const Padding(
                        padding: EdgeInsets.only(left: 4, bottom: 16),
                        child: Text(
                          'Choose how closely Ella monitors and supports your loved one.',
                          style: TextStyle(
                            fontSize: 16,
                            color: EllaColors.textTertiary,
                          ),
                        ),
                      ),

                      // ── INTELLIGENCE MODES section ─────────────────────────
                      const _SectionHeader(label: 'INTELLIGENCE MODES'),
                      const Padding(
                        padding: EdgeInsets.only(left: 4, bottom: 10),
                        child: Text(
                          'Controls how Ella detects and processes conversations. Does not change who receives alerts.',
                          style: TextStyle(
                            fontSize: 13,
                            color: EllaColors.textTertiary,
                          ),
                        ),
                      ),
                      ..._overrideModes.map((key) {
                        final preset = _presetFor(key);
                        final isSelected = _selectedOverride == key;
                        return _OverrideRow(
                          preset: preset,
                          isSelected: isSelected,
                          onTap: () {
                            setState(() {
                              if (isSelected) {
                                _selectedOverride = null;
                              } else {
                                _selectedOverride = key;
                                _selectedFeatures.clear();
                              }
                            });
                          },
                        );
                      }),

                      // ── View Demo Scenarios link (when DEMO is selected) ───
                      if (_selectedOverride == 'DEMO')
                        InkWell(
                          onTap: () => Navigator.push(
                            context,
                            MaterialPageRoute(
                              builder: (_) => const EllaDemoScenariosPage(),
                            ),
                          ),
                          borderRadius: BorderRadius.circular(
                            EllaSizes.radiusMedium,
                          ),
                          child: const Padding(
                            padding: EdgeInsets.symmetric(
                              horizontal: 4,
                              vertical: 10,
                            ),
                            child: Row(
                              children: [
                                Icon(
                                  Icons.list_alt,
                                  color: EllaColors.primary,
                                  size: 20,
                                ),
                                SizedBox(width: 10),
                                Text(
                                  'View Demo Scenarios',
                                  style: TextStyle(
                                    fontSize: 16,
                                    color: EllaColors.primary,
                                  ),
                                ),
                                Spacer(),
                                Icon(
                                  Icons.chevron_right,
                                  color: EllaColors.textTertiary,
                                  size: 20,
                                ),
                              ],
                            ),
                          ),
                        ),

                      const SizedBox(height: 20),

                      const _SectionHeader(label: 'GUARDIAN VOICE'),
                      const Padding(
                        padding: EdgeInsets.only(left: 4, bottom: 10),
                        child: Text(
                          'Controls which backend voice family generates Guardian alerts. App chat and V2V voice selection stay separate.',
                          style: TextStyle(
                            fontSize: 13,
                            color: EllaColors.textTertiary,
                          ),
                        ),
                      ),
                      _GuardianVoicePolicyCard(
                        policy: _selectedVoicePolicy,
                        provider: _selectedVoiceProvider,
                        activeProviderLabel:
                            _formatProviderLabel(V2VClient.normalizeProvider(SharedPreferencesUtil().ttsProvider)),
                        resolvedProvider: _currentVoiceConfig.resolvedProvider,
                        fallbackProvider: _currentVoiceConfig.fallbackProvider,
                        lastVoiceProvider: _currentVoiceConfig.lastVoiceProvider,
                        onPolicyChanged: (policy) {
                          setState(() {
                            _selectedVoicePolicy = policy;
                            if (policy == GuardianVoicePolicy.pinnedProvider &&
                                !_guardianVoiceProviders.containsKey(_selectedVoiceProvider)) {
                              _selectedVoiceProvider = _defaultPinnedVoiceProvider;
                            }
                          });
                        },
                        onProviderChanged: (provider) {
                          setState(() => _selectedVoiceProvider = provider);
                        },
                      ),

                      const SizedBox(height: 20),

                      // ── CARE FEATURES section ──────────────────────────────
                      const _SectionHeader(label: 'CARE FEATURES'),
                      const Padding(
                        padding: EdgeInsets.only(left: 4, bottom: 10),
                        child: Text(
                          'Choose which care features are active. Critical alerts to your emergency contact remain active even when Guardian is off.',
                          style: TextStyle(
                            fontSize: 13,
                            color: EllaColors.textTertiary,
                          ),
                        ),
                      ),
                      ..._careFeatureKeys.map((key) {
                        final preset = _presetFor(key);
                        final isChecked = _selectedFeatures.contains(key);
                        final dimmed = _selectedOverride != null;
                        return _FeatureRow(
                          preset: preset,
                          isChecked: isChecked,
                          dimmed: dimmed,
                          onChanged: dimmed
                              ? null
                              : (checked) {
                                  setState(() {
                                    if (checked == true) {
                                      _selectedFeatures.add(key);
                                    } else {
                                      _selectedFeatures.remove(key);
                                    }
                                  });
                                },
                        );
                      }),

                      const SizedBox(height: 24),

                      // ── Turn Guardian Off ──────────────────────────────────
                      _TurnOffButton(
                        isOff: _selectedOverride == null && _selectedFeatures.isEmpty,
                        onTap: () {
                          setState(() {
                            _selectedOverride = null;
                            _selectedFeatures.clear();
                          });
                        },
                      ),

                      const SizedBox(height: 8),
                    ],
                  ),
                ),
                _SaveBar(
                  enabled: _hasChanges && !_saving,
                  saving: _saving,
                  onSave: _save,
                ),
              ],
            ),
    );
  }
}

class _GuardianVoicePolicyCard extends StatelessWidget {
  final GuardianVoicePolicy policy;
  final String provider;
  final String activeProviderLabel;
  final String? resolvedProvider;
  final String? fallbackProvider;
  final String? lastVoiceProvider;
  final ValueChanged<GuardianVoicePolicy> onPolicyChanged;
  final ValueChanged<String> onProviderChanged;

  const _GuardianVoicePolicyCard({
    required this.policy,
    required this.provider,
    required this.activeProviderLabel,
    required this.resolvedProvider,
    required this.fallbackProvider,
    required this.lastVoiceProvider,
    required this.onPolicyChanged,
    required this.onProviderChanged,
  });

  static const Map<String, String> _providerLabels = {
    'xai-tts': 'xAI TTS / Grok family',
    'openai': 'OpenAI',
    'elevenlabs': 'ElevenLabs',
    'kokoro': 'Kokoro',
  };

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: EllaColors.bgSecondary,
        borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
        border: Border.all(color: EllaColors.bgTertiary),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _PolicyOption(
            title: GuardianVoicePolicy.matchActiveProvider.displayName,
            subtitle:
                'Use the active voice provider where Guardian can support it. Current app voice: $activeProviderLabel.',
            selected: policy == GuardianVoicePolicy.matchActiveProvider,
            onTap: () => onPolicyChanged(GuardianVoicePolicy.matchActiveProvider),
          ),
          const SizedBox(height: 8),
          _PolicyOption(
            title: GuardianVoicePolicy.pinnedProvider.displayName,
            subtitle: 'Always ask the backend to generate Guardian alerts with a specific provider.',
            selected: policy == GuardianVoicePolicy.pinnedProvider,
            onTap: () => onPolicyChanged(GuardianVoicePolicy.pinnedProvider),
          ),
          if (policy == GuardianVoicePolicy.pinnedProvider) ...[
            const SizedBox(height: 10),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 12),
              decoration: BoxDecoration(
                color: EllaColors.bgPrimary,
                borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                border: Border.all(color: EllaColors.bgTertiary),
              ),
              child: DropdownButton<String>(
                value: _providerLabels.containsKey(provider) ? provider : 'openai',
                isExpanded: true,
                underline: const SizedBox.shrink(),
                dropdownColor: Colors.white,
                style: const TextStyle(color: EllaColors.textPrimary, fontSize: 15),
                iconEnabledColor: EllaColors.textSecondary,
                items: _providerLabels.entries
                    .map((entry) => DropdownMenuItem(value: entry.key, child: Text(entry.value)))
                    .toList(),
                onChanged: (value) {
                  if (value != null) onProviderChanged(value);
                },
              ),
            ),
          ],
          if (resolvedProvider != null || fallbackProvider != null || lastVoiceProvider != null) ...[
            const SizedBox(height: 12),
            Text(
              [
                if (resolvedProvider != null) 'Resolved: ${_formatProviderLabel(resolvedProvider!)}',
                if (fallbackProvider != null) 'Fallback: ${_formatProviderLabel(fallbackProvider!)}',
                if (lastVoiceProvider != null) 'Last active: ${_formatProviderLabel(lastVoiceProvider!)}',
              ].join('  |  '),
              style: const TextStyle(
                fontSize: 12,
                color: EllaColors.textTertiary,
              ),
            ),
          ],
        ],
      ),
    );
  }

  static String _formatProviderLabel(String provider) {
    return _providerLabels[provider] ?? provider;
  }
}

class _PolicyOption extends StatelessWidget {
  final String title;
  final String subtitle;
  final bool selected;
  final VoidCallback onTap;

  const _PolicyOption({
    required this.title,
    required this.subtitle,
    required this.selected,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
      child: Container(
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: selected ? EllaColors.primarySubtle : EllaColors.bgPrimary,
          borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
          border: Border.all(color: selected ? EllaColors.primary : EllaColors.bgTertiary),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              width: 20,
              height: 20,
              margin: const EdgeInsets.only(top: 1),
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: selected ? EllaColors.primary : Colors.transparent,
                border: Border.all(color: selected ? EllaColors.primary : EllaColors.bgTertiary, width: 2),
              ),
              child: selected ? const Icon(Icons.check, size: 12, color: Colors.white) : null,
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    title,
                    style: const TextStyle(
                      fontSize: 15,
                      fontWeight: FontWeight.w700,
                      color: EllaColors.textPrimary,
                    ),
                  ),
                  const SizedBox(height: 3),
                  Text(
                    subtitle,
                    style: const TextStyle(
                      fontSize: 13,
                      color: EllaColors.textSecondary,
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ── Section header ─────────────────────────────────────────────────────────

class _SectionHeader extends StatelessWidget {
  final String label;
  const _SectionHeader({required this.label});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(left: 4, bottom: 6),
      child: Text(
        label,
        style: const TextStyle(
          fontSize: 11,
          fontWeight: FontWeight.w700,
          color: EllaColors.textTertiary,
          letterSpacing: 1.1,
        ),
      ),
    );
  }
}

// ── Intelligence mode radio row ────────────────────────────────────────────

class _OverrideRow extends StatelessWidget {
  final GuardianPreset preset;
  final bool isSelected;
  final VoidCallback onTap;

  const _OverrideRow({
    required this.preset,
    required this.isSelected,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 180),
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          decoration: BoxDecoration(
            color: isSelected ? preset.color.withValues(alpha: 0.08) : EllaColors.bgSecondary,
            borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
            border: Border.all(
              color: isSelected ? preset.color : Colors.transparent,
              width: 2,
            ),
          ),
          child: Row(
            children: [
              // Radio dot
              Container(
                width: 20,
                height: 20,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: isSelected ? preset.color : Colors.transparent,
                  border: Border.all(
                    color: isSelected ? preset.color : EllaColors.bgTertiary,
                    width: 2,
                  ),
                ),
                child: isSelected ? const Icon(Icons.check, size: 12, color: Colors.white) : null,
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      preset.name,
                      style: TextStyle(
                        fontSize: 16,
                        fontWeight: FontWeight.w600,
                        color: isSelected ? preset.color : EllaColors.textPrimary,
                      ),
                    ),
                    if (preset.description.isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.only(top: 2),
                        child: Text(
                          preset.description,
                          style: const TextStyle(
                            fontSize: 13,
                            color: EllaColors.textTertiary,
                          ),
                        ),
                      ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

// ── Care feature checkbox row ──────────────────────────────────────────────

class _FeatureRow extends StatelessWidget {
  final GuardianPreset preset;
  final bool isChecked;
  final bool dimmed;
  final ValueChanged<bool?>? onChanged;

  const _FeatureRow({
    required this.preset,
    required this.isChecked,
    required this.dimmed,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    final effectiveColor = dimmed ? EllaColors.bgTertiary : preset.color;
    final textColor = dimmed ? EllaColors.textTertiary : EllaColors.textPrimary;

    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: InkWell(
        onTap: onChanged == null ? null : () => onChanged!(!isChecked),
        borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 180),
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          decoration: BoxDecoration(
            color: isChecked && !dimmed ? preset.color.withValues(alpha: 0.08) : EllaColors.bgSecondary,
            borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
            border: Border.all(
              color: isChecked && !dimmed ? effectiveColor : Colors.transparent,
              width: 2,
            ),
          ),
          child: Row(
            children: [
              // Checkbox
              AnimatedContainer(
                duration: const Duration(milliseconds: 150),
                width: 20,
                height: 20,
                decoration: BoxDecoration(
                  borderRadius: BorderRadius.circular(5),
                  color: isChecked && !dimmed ? effectiveColor : Colors.transparent,
                  border: Border.all(
                    color: isChecked && !dimmed ? effectiveColor : EllaColors.bgTertiary,
                    width: 2,
                  ),
                ),
                child: isChecked && !dimmed ? const Icon(Icons.check, size: 12, color: Colors.white) : null,
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      preset.name,
                      style: TextStyle(
                        fontSize: 16,
                        fontWeight: FontWeight.w600,
                        color: isChecked && !dimmed ? effectiveColor : textColor,
                      ),
                    ),
                    if (preset.description.isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.only(top: 2),
                        child: Text(
                          preset.description,
                          style: TextStyle(
                            fontSize: 13,
                            color: dimmed ? EllaColors.textTertiary.withValues(alpha: 0.5) : EllaColors.textTertiary,
                          ),
                        ),
                      ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

// ── Turn Guardian Off button ───────────────────────────────────────────────

class _TurnOffButton extends StatelessWidget {
  final bool isOff;
  final VoidCallback onTap;

  const _TurnOffButton({required this.isOff, required this.onTap});

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: double.infinity,
      child: OutlinedButton(
        onPressed: isOff ? null : onTap,
        style: OutlinedButton.styleFrom(
          side: BorderSide(
            color: isOff ? EllaColors.bgTertiary : const Color(0xFF6B7280),
            width: 1.5,
          ),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
          ),
          padding: const EdgeInsets.symmetric(vertical: 14),
        ),
        child: Column(
          children: [
            Text(
              isOff ? 'Guardian Off' : 'Turn Guardian Off',
              style: TextStyle(
                fontSize: 16,
                fontWeight: FontWeight.w500,
                color: isOff ? EllaColors.bgTertiary : const Color(0xFF9CA3AF),
              ),
            ),
            const SizedBox(height: 2),
            Text(
              'Disables audio and non-critical check-ins. Emergency alerts stay active.',
              style: TextStyle(
                fontSize: 12,
                color: isOff ? EllaColors.bgTertiary : const Color(0xFF9CA3AF).withValues(alpha: 0.7),
              ),
              textAlign: TextAlign.center,
            ),
          ],
        ),
      ),
    );
  }
}

// ── Save bar ───────────────────────────────────────────────────────────────

class _SaveBar extends StatelessWidget {
  final bool enabled;
  final bool saving;
  final VoidCallback onSave;

  const _SaveBar({
    required this.enabled,
    required this.saving,
    required this.onSave,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: EdgeInsets.fromLTRB(
        16,
        12,
        16,
        12 + MediaQuery.of(context).padding.bottom,
      ),
      decoration: const BoxDecoration(
        color: EllaColors.bgPrimary,
        border: Border(top: BorderSide(color: EllaColors.bgTertiary, width: 1)),
      ),
      child: SizedBox(
        width: double.infinity,
        height: 52,
        child: ElevatedButton(
          onPressed: enabled ? onSave : null,
          style: ElevatedButton.styleFrom(
            backgroundColor: EllaColors.primary,
            disabledBackgroundColor: EllaColors.bgTertiary,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
            ),
          ),
          child: saving
              ? const SizedBox(
                  width: 20,
                  height: 20,
                  child: CircularProgressIndicator(
                    strokeWidth: 2,
                    color: Colors.white,
                  ),
                )
              : const Text(
                  'Save',
                  style: TextStyle(
                    fontSize: 17,
                    fontWeight: FontWeight.w600,
                    color: Colors.white,
                  ),
                ),
        ),
      ),
    );
  }
}
