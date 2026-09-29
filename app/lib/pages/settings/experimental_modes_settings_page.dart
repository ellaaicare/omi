import 'package:flutter/material.dart';
import 'package:font_awesome_flutter/font_awesome_flutter.dart';
import 'package:omi/ella/models/guardian_mode.dart';
import 'package:omi/ella/services/ella_public_surface_policy.dart';
import 'package:omi/ella/services/guardian_mode_api.dart' as guardian_api;
import 'package:omi/models/experimental_mode.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// Core, non-experimental Whispers modes a user can pick between. Selecting
/// one enables Whispers in that mode; it never disables Whispers (the
/// on/off switch for Whispers itself lives on the Today page). Critical-
/// safety Whispers (falls, chest pain, and similar emergencies) are always
/// delivered in every one of these modes - that policy is enforced
/// server-side and is not gated here.
const List<String> coreWhisperModeKeys = ['MEMORY_SUPPORT', 'ACTIVE_SUPPORT', 'EMERGENCY_ONLY'];

/// The default core Whispers mode - MEMORY_SUPPORT, not ACTIVE_SUPPORT
/// (product decision, G4).
const String defaultCoreWhisperMode = 'MEMORY_SUPPORT';

/// Picks the currently-selected core Whisper mode out of a saved features
/// list: the first of [coreWhisperModeKeys] present, or [defaultCoreWhisperMode]
/// when none is (including when Whispers is off, or an unrelated
/// override/feature like MAXIMUM_AWARENESS is active).
String resolveCoreWhisperMode(List<String> features) =>
    coreWhisperModeKeys.firstWhere(features.contains, orElse: () => defaultCoreWhisperMode);

/// Settings page presenting the single Whispers/experimental modes surface:
/// the core Whispers mode picker (memory_support, active_support,
/// emergency_only - memory_support is the default) plus unavailable previews
/// for the experimental modes (einstein, cyborg) from the backend mode
/// registry. Experimental modes cannot be enabled until authoritative server
/// activation and rate-limit support exist.
class ExperimentalModesSettingsPage extends StatefulWidget {
  const ExperimentalModesSettingsPage({super.key});

  @override
  State<ExperimentalModesSettingsPage> createState() => _ExperimentalModesSettingsPageState();
}

class _ExperimentalModesSettingsPageState extends State<ExperimentalModesSettingsPage> {
  final bool _coreModesAvailable = allowsGuardianCareSurface();
  bool _coreModeLoading = true;
  String _selectedCoreMode = defaultCoreWhisperMode;
  bool _savingCoreMode = false;

  @override
  void initState() {
    super.initState();
    if (_coreModesAvailable) {
      _loadCoreMode();
    } else {
      _coreModeLoading = false;
    }
  }

  Future<void> _loadCoreMode() async {
    final result = await guardian_api.getGuardianMode();
    if (!mounted) return;
    setState(() {
      if (result.isSuccess) {
        final features = result.value?.twoTierState?.features ?? const <String>[];
        _selectedCoreMode = resolveCoreWhisperMode(features);
      }
      _coreModeLoading = false;
    });
  }

  Future<void> _selectCoreMode(String key) async {
    if (_savingCoreMode || _selectedCoreMode == key) return;
    final previous = _selectedCoreMode;
    setState(() {
      _selectedCoreMode = key;
      _savingCoreMode = true;
    });
    final result = await guardian_api.setGuardianModeTwoTier(GuardianModeState(features: [key]));
    if (!mounted) return;
    setState(() {
      _savingCoreMode = false;
      if (!result.isSuccess) _selectedCoreMode = previous;
    });
    if (!result.isSuccess) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.anErrorOccurredTryAgain)));
    }
  }

  IconData _iconFor(String id) {
    switch (id) {
      case 'einstein':
        return FontAwesomeIcons.brain;
      case 'cyborg':
        return FontAwesomeIcons.robot;
      default:
        return FontAwesomeIcons.robot;
    }
  }

  String _titleFor(BuildContext context, String id) {
    switch (id) {
      case 'einstein':
        return context.l10n.experimentalModeEinsteinTitle;
      case 'cyborg':
        return context.l10n.experimentalModeCyborgTitle;
      default:
        return id;
    }
  }

  String _descriptionFor(BuildContext context, String id) {
    switch (id) {
      case 'einstein':
        return context.l10n.experimentalModeEinsteinDescription;
      case 'cyborg':
        return context.l10n.experimentalModeCyborgDescription;
      default:
        return '';
    }
  }

  String _coreModeTitle(BuildContext context, String key) {
    switch (key) {
      case 'MEMORY_SUPPORT':
        return context.l10n.coreModeMemorySupportTitle;
      case 'ACTIVE_SUPPORT':
        return context.l10n.coreModeActiveSupportTitle;
      case 'EMERGENCY_ONLY':
        return context.l10n.coreModeEmergencyOnlyTitle;
      default:
        return key;
    }
  }

  String _coreModeDescription(BuildContext context, String key) {
    switch (key) {
      case 'MEMORY_SUPPORT':
        return context.l10n.coreModeMemorySupportDescription;
      case 'ACTIVE_SUPPORT':
        return context.l10n.coreModeActiveSupportDescription;
      case 'EMERGENCY_ONLY':
        return context.l10n.coreModeEmergencyOnlyDescription;
      default:
        return '';
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Theme.of(context).colorScheme.primary,
      appBar: AppBar(
        title: Text(context.l10n.whispersAndExperimentalModesTitle),
        backgroundColor: Theme.of(context).colorScheme.primary,
        elevation: 0,
      ),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (_coreModesAvailable) ...[
              _buildSectionHeader(context.l10n.whispersModeSectionTitle),
              const SizedBox(height: 8),
              Padding(
                padding: const EdgeInsets.only(bottom: 16),
                child: Text(
                  context.l10n.whispersModeSectionDescription,
                  style: TextStyle(color: Colors.grey.shade400, fontSize: 14, height: 1.5),
                ),
              ),
              _coreModeLoading
                  ? const Padding(
                      padding: EdgeInsets.symmetric(vertical: 12),
                      child: Center(child: CircularProgressIndicator(color: Colors.white)),
                    )
                  : Container(
                      padding: const EdgeInsets.all(8),
                      decoration: BoxDecoration(
                        color: const Color(0xFF1C1C1E),
                        borderRadius: BorderRadius.circular(20),
                      ),
                      child: Column(children: [for (final key in coreWhisperModeKeys) _buildCoreModeRow(key)]),
                    ),
              const SizedBox(height: 32),
            ],
            _buildSectionHeader(context.l10n.experimentalModes),
            const SizedBox(height: 8),
            Padding(
              padding: const EdgeInsets.only(bottom: 16),
              child: Text(
                context.l10n.experimentalModesDescription,
                style: TextStyle(color: Colors.grey.shade400, fontSize: 14, height: 1.5),
              ),
            ),
            for (final mode in ExperimentalMode.registry) ...[_buildModeCard(mode), const SizedBox(height: 12)],
          ],
        ),
      ),
    );
  }

  Widget _buildSectionHeader(String title) {
    return Text(
      title,
      style: const TextStyle(color: Colors.white, fontSize: 20, fontWeight: FontWeight.w600),
    );
  }

  Widget _buildCoreModeRow(String key) {
    final isSelected = _selectedCoreMode == key;
    return Padding(
      key: Key('core_whisper_mode_row_$key'),
      padding: const EdgeInsets.symmetric(vertical: 4, horizontal: 4),
      child: InkWell(
        onTap: _savingCoreMode ? null : () => _selectCoreMode(key),
        borderRadius: BorderRadius.circular(14),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
          child: Row(
            children: [
              Container(
                width: 20,
                height: 20,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: isSelected ? const Color(0xFF6366F1) : Colors.transparent,
                  border: Border.all(color: isSelected ? const Color(0xFF6366F1) : Colors.grey.shade600, width: 2),
                ),
                child: isSelected ? const Icon(Icons.check, size: 12, color: Colors.white) : null,
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      _coreModeTitle(context, key),
                      style: TextStyle(
                        color: isSelected ? Colors.white : Colors.grey.shade300,
                        fontSize: 16,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      _coreModeDescription(context, key),
                      style: TextStyle(color: Colors.grey.shade400, fontSize: 13),
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

  Widget _buildModeCard(ExperimentalMode mode) {
    return Container(
      key: Key('experimental_mode_card_${mode.id}'),
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(color: const Color(0xFF1C1C1E), borderRadius: BorderRadius.circular(20)),
      child: Row(
        children: [
          Container(
            width: 40,
            height: 40,
            decoration: BoxDecoration(color: const Color(0xFF2A2A2E), borderRadius: BorderRadius.circular(10)),
            child: Center(child: FaIcon(_iconFor(mode.id), color: Colors.grey.shade400, size: 16)),
          ),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  _titleFor(context, mode.id),
                  style: const TextStyle(color: Colors.white, fontSize: 16, fontWeight: FontWeight.w500),
                ),
                const SizedBox(height: 4),
                Text(
                  '${context.l10n.preview}, ${context.l10n.comingSoon}',
                  style: const TextStyle(color: Color(0xFF9CA3AF), fontSize: 12, fontWeight: FontWeight.w600),
                ),
                const SizedBox(height: 4),
                Text(_descriptionFor(context, mode.id), style: TextStyle(color: Colors.grey.shade400, fontSize: 13)),
              ],
            ),
          ),
          const Switch(value: false, onChanged: null),
        ],
      ),
    );
  }
}
