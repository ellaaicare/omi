import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/guardian_mode.dart';
import 'package:omi/ella/services/guardian_mode_api.dart' as guardian_api;

class GuardianModePage extends StatefulWidget {
  const GuardianModePage({super.key});

  @override
  State<GuardianModePage> createState() => _GuardianModePageState();
}

class _GuardianModePageState extends State<GuardianModePage> {
  List<GuardianPreset> _presets = [];
  GuardianModeKey? _currentMode;
  GuardianModeKey? _selectedMode;
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
        _presets = presets;
        _currentMode = modeInfo?.currentMode ?? GuardianModeKey.activeSupport;
        _selectedMode = _currentMode;
        _loading = false;
      });
    }
  }

  bool get _hasChanges => _selectedMode != null && _selectedMode != _currentMode;

  Future<void> _save() async {
    if (_selectedMode == null || !_hasChanges) return;

    // Confirmation for Maximum Awareness escalation
    if (_selectedMode == GuardianModeKey.maximumAwareness &&
        _currentMode != GuardianModeKey.maximumAwareness) {
      final confirmed = await _showMaxAwarenessConfirmation();
      if (!confirmed) return;
    }

    setState(() => _saving = true);
    final success = await guardian_api.setGuardianMode(_selectedMode!);
    if (!mounted) return;
    setState(() => _saving = false);

    if (success) {
      setState(() => _currentMode = _selectedMode);
      final modeName = _presets
          .firstWhere(
            (p) => p.modeKey == _selectedMode,
            orElse: () => GuardianPreset(
              presetKey: _selectedMode!.toApiString(),
              name: _selectedMode!.toApiString(),
              description: '',
              detailsBullets: const [],
              color: EllaColors.primary,
            ),
          )
          .name;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text('Guardian mode updated to $modeName'),
          backgroundColor: EllaColors.success,
          behavior: SnackBarBehavior.floating,
          duration: const Duration(seconds: 3),
        ),
      );
      Navigator.of(context).pop(_selectedMode);
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
                      ..._presets.map((preset) => _PresetCard(
                            preset: preset,
                            isSelected: _selectedMode == preset.modeKey,
                            isCurrent: _currentMode == preset.modeKey,
                            onTap: () => setState(() => _selectedMode = preset.modeKey),
                          )),
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

class _PresetCard extends StatelessWidget {
  final GuardianPreset preset;
  final bool isSelected;
  final bool isCurrent;
  final VoidCallback onTap;

  const _PresetCard({
    required this.preset,
    required this.isSelected,
    required this.isCurrent,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 180),
          padding: const EdgeInsets.all(16),
          decoration: BoxDecoration(
            color: isSelected
                ? preset.color.withOpacity(0.08)
                : EllaColors.bgSecondary,
            borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
            border: Border.all(
              color: isSelected ? preset.color : Colors.transparent,
              width: 2,
            ),
          ),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Color dot / radio indicator
              Padding(
                padding: const EdgeInsets.only(top: 3),
                child: Container(
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
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Text(
                          preset.name,
                          style: TextStyle(
                            fontSize: 17,
                            fontWeight: FontWeight.w600,
                            color: isSelected ? preset.color : EllaColors.textPrimary,
                          ),
                        ),
                        if (isCurrent) ...[
                          const SizedBox(width: 8),
                          Container(
                            padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
                            decoration: BoxDecoration(
                              color: preset.color.withOpacity(0.15),
                              borderRadius: BorderRadius.circular(EllaSizes.radiusSmall),
                            ),
                            child: Text(
                              'Current',
                              style: TextStyle(
                                fontSize: 11,
                                fontWeight: FontWeight.w600,
                                color: preset.color,
                              ),
                            ),
                          ),
                        ],
                      ],
                    ),
                    const SizedBox(height: 4),
                    Text(
                      preset.description,
                      style: const TextStyle(
                        fontSize: 14,
                        color: EllaColors.textTertiary,
                      ),
                    ),
                    if (preset.detailsBullets.isNotEmpty && isSelected) ...[
                      const SizedBox(height: 8),
                      ...preset.detailsBullets.map(
                        (bullet) => Padding(
                          padding: const EdgeInsets.only(top: 3),
                          child: Row(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Container(
                                margin: const EdgeInsets.only(top: 6),
                                width: 4,
                                height: 4,
                                decoration: BoxDecoration(
                                  color: preset.color,
                                  shape: BoxShape.circle,
                                ),
                              ),
                              const SizedBox(width: 8),
                              Expanded(
                                child: Text(
                                  bullet,
                                  style: TextStyle(
                                    fontSize: 14,
                                    color: preset.color.withOpacity(0.85),
                                  ),
                                ),
                              ),
                            ],
                          ),
                        ),
                      ),
                    ],
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
