import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/guardian_mode.dart';
import 'package:omi/ella/services/guardian_mode_api.dart' as guardian_api;

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
  GuardianModeState _currentState = const GuardianModeState(features: ['ACTIVE_SUPPORT']);

  // Pending selection state (user has made changes but not saved).
  String? _selectedOverride; // 'CYBORG' | 'DEMO' | null
  Set<String> _selectedFeatures = {'ACTIVE_SUPPORT'}; // composable features

  bool _loading = true;
  bool _saving = false;

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
    ]);
    final presets = results[0] as List<GuardianPreset>;
    final modeInfo = results[1] as GuardianModeInfo?;

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
        !_sameFeatures(pending.features, _currentState.features);
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
    final success = await guardian_api.setGuardianModeTwoTier(_pendingState);
    if (!mounted) return;
    setState(() => _saving = false);

    if (success) {
      setState(() => _currentState = _pendingState);
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Guardian mode updated'),
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
                  style: TextStyle(fontSize: 16, color: EllaColors.textTertiary),
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
        color: GuardianModeKey.fromString(key).isOverride
            ? const Color(0xFFEC4899)
            : const Color(0xFF14B8A6),
      ),
    );
  }

  String _fallbackLabel(String key) {
    switch (key) {
      case 'CYBORG':
        return 'Cyborg';
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

  // ── Intelligence mode rows (exclusive radio) ──────────────────────────────

  List<String> get _overrideModes {
    final modes = ['CYBORG'];
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
          ? const Center(child: CircularProgressIndicator(color: EllaColors.primary))
          : Column(
              children: [
                Expanded(
                  child: ListView(
                    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
                    children: [
                      const Padding(
                        padding: EdgeInsets.only(left: 4, bottom: 16),
                        child: Text(
                          'Choose how closely Ella monitors and supports your loved one.',
                          style: TextStyle(fontSize: 16, color: EllaColors.textTertiary),
                        ),
                      ),

                      // ── INTELLIGENCE MODES section ─────────────────────────
                      const _SectionHeader(label: 'INTELLIGENCE MODES'),
                      const Padding(
                        padding: EdgeInsets.only(left: 4, bottom: 10),
                        child: Text(
                          'Replaces the care system entirely.',
                          style: TextStyle(fontSize: 13, color: EllaColors.textTertiary),
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

                      const SizedBox(height: 20),

                      // ── CARE FEATURES section ──────────────────────────────
                      const _SectionHeader(label: 'CARE FEATURES'),
                      const Padding(
                        padding: EdgeInsets.only(left: 4, bottom: 10),
                        child: Text(
                          'Combine multiple features simultaneously.',
                          style: TextStyle(fontSize: 13, color: EllaColors.textTertiary),
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
                child: isSelected
                    ? const Icon(Icons.check, size: 12, color: Colors.white)
                    : null,
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
            color: isChecked && !dimmed
                ? preset.color.withValues(alpha: 0.08)
                : EllaColors.bgSecondary,
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
                child: isChecked && !dimmed
                    ? const Icon(Icons.check, size: 12, color: Colors.white)
                    : null,
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
                            color: dimmed
                                ? EllaColors.textTertiary.withValues(alpha: 0.5)
                                : EllaColors.textTertiary,
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
        child: Text(
          isOff ? 'Guardian Off' : 'Turn Guardian Off',
          style: TextStyle(
            fontSize: 16,
            fontWeight: FontWeight.w500,
            color: isOff ? EllaColors.bgTertiary : const Color(0xFF9CA3AF),
          ),
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
