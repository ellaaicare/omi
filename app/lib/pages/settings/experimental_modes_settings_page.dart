import 'package:flutter/material.dart';
import 'package:font_awesome_flutter/font_awesome_flutter.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/models/experimental_mode.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// Settings page listing the opt-in, memory-less experimental modes
/// (einstein, cyborg) from the backend mode registry. Every mode is OFF by
/// default; enabling one only opts the user into that mode's notifications -
/// it never changes the default Home/Whispers behavior.
class ExperimentalModesSettingsPage extends StatefulWidget {
  const ExperimentalModesSettingsPage({super.key});

  @override
  State<ExperimentalModesSettingsPage> createState() => _ExperimentalModesSettingsPageState();
}

class _ExperimentalModesSettingsPageState extends State<ExperimentalModesSettingsPage> {
  late Set<String> _enabledModeIds;

  @override
  void initState() {
    super.initState();
    _enabledModeIds = SharedPreferencesUtil().enabledExperimentalModeIds.toSet();
  }

  void _toggleMode(String id, bool value) {
    setState(() {
      if (value) {
        _enabledModeIds.add(id);
      } else {
        _enabledModeIds.remove(id);
      }
    });
    SharedPreferencesUtil().enabledExperimentalModeIds = _enabledModeIds.toList();
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

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Theme.of(context).colorScheme.primary,
      appBar: AppBar(
        title: Text(context.l10n.experimentalModes),
        backgroundColor: Theme.of(context).colorScheme.primary,
        elevation: 0,
      ),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.only(bottom: 20),
              child: Text(
                context.l10n.experimentalModesDescription,
                style: TextStyle(
                  color: Colors.grey.shade400,
                  fontSize: 14,
                  height: 1.5,
                ),
              ),
            ),
            for (final mode in ExperimentalMode.registry) ...[
              _buildModeCard(mode),
              const SizedBox(height: 12),
            ],
          ],
        ),
      ),
    );
  }

  Widget _buildModeCard(ExperimentalMode mode) {
    return Container(
      key: Key('experimental_mode_card_${mode.id}'),
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        color: const Color(0xFF1C1C1E),
        borderRadius: BorderRadius.circular(20),
      ),
      child: Row(
        children: [
          Container(
            width: 40,
            height: 40,
            decoration: BoxDecoration(
              color: const Color(0xFF2A2A2E),
              borderRadius: BorderRadius.circular(10),
            ),
            child: Center(child: FaIcon(_iconFor(mode.id), color: Colors.grey.shade400, size: 16)),
          ),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  _titleFor(context, mode.id),
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 16,
                    fontWeight: FontWeight.w500,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  _descriptionFor(context, mode.id),
                  style: TextStyle(
                    color: Colors.grey.shade400,
                    fontSize: 13,
                  ),
                ),
              ],
            ),
          ),
          Switch(
            value: _enabledModeIds.contains(mode.id),
            onChanged: (value) => _toggleMode(mode.id, value),
            activeColor: const Color(0xFF6366F1),
          ),
        ],
      ),
    );
  }
}
