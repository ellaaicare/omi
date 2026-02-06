import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/caregiver.dart';
import 'package:omi/ella/pages/ella_add_caregiver_page.dart';
import 'package:omi/ella/services/caregiver_api.dart' as caregiver_api;
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/logger.dart';

class EllaEmergencyContactPage extends StatefulWidget {
  const EllaEmergencyContactPage({super.key});

  @override
  State<EllaEmergencyContactPage> createState() => _EllaEmergencyContactPageState();
}

class _EllaEmergencyContactPageState extends State<EllaEmergencyContactPage> {
  List<Caregiver> _caregivers = [];
  String? _selectedId;
  String? _originalId;
  bool _loading = true;
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    _loadData();
  }

  Future<void> _loadData() async {
    try {
      final caregivers = await caregiver_api.getCaregivers();
      final emergencyId = await caregiver_api.getEmergencyContactId();
      if (mounted) {
        setState(() {
          _caregivers = caregivers;
          _selectedId = emergencyId;
          _originalId = emergencyId;
          _loading = false;
        });
      }
    } catch (e) {
      Logger.debug('Failed to load emergency contact data: $e');
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _save() async {
    if (_selectedId == null || _selectedId == _originalId || _saving) return;
    setState(() => _saving = true);
    try {
      await caregiver_api.setEmergencyContact(_selectedId!);
      if (mounted) Navigator.of(context).pop();
    } catch (_) {
      if (mounted) {
        setState(() => _saving = false);
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(context.l10n.ellaInviteErrorNetwork)),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      appBar: AppBar(
        backgroundColor: EllaColors.bgPrimary,
        elevation: 0,
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, size: 24, color: EllaColors.textPrimary),
          iconSize: EllaSizes.appBarButtonSize,
          onPressed: () => Navigator.of(context).pop(),
        ),
        title: Text(
          context.l10n.ellaEmergencyContact,
          style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
        ),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator(color: EllaColors.primary))
          : _caregivers.isEmpty
              ? _buildEmptyState(context)
              : _buildContactList(context),
    );
  }

  Widget _buildEmptyState(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 24),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          const Icon(Icons.people_outline, size: 64, color: EllaColors.textDisabled),
          const SizedBox(height: 24),
          Text(
            context.l10n.ellaCareTeamEmpty,
            textAlign: TextAlign.center,
            style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
          ),
          const SizedBox(height: 12),
          Text(
            context.l10n.ellaCareTeamEmptyDescription,
            textAlign: TextAlign.center,
            style: const TextStyle(
              fontSize: 18,
              fontWeight: FontWeight.w400,
              color: EllaColors.textSecondary,
              height: 1.5,
            ),
          ),
          const SizedBox(height: 32),
          Semantics(
            button: true,
            label: 'Add family member to your care team',
            child: InkWell(
              onTap: () async {
                await Navigator.push(
                  context,
                  MaterialPageRoute(builder: (context) => const EllaAddCaregiverPage()),
                );
                _loadData();
              },
              borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
              child: Container(
                height: 64,
                width: double.infinity,
                decoration: BoxDecoration(
                  color: EllaColors.primary,
                  borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
                ),
                child: Center(
                  child: Text(
                    '+ ${context.l10n.ellaAddFamilyMember}',
                    style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w600, color: EllaColors.textPrimary),
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildContactList(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.symmetric(horizontal: 24),
      children: [
        const SizedBox(height: 8),
        Text(
          context.l10n.ellaEmergencyContactDescription,
          style: const TextStyle(
            fontSize: 18,
            fontWeight: FontWeight.w400,
            color: EllaColors.textSecondary,
            height: 1.5,
          ),
        ),
        const SizedBox(height: 16),
        ..._caregivers.map((caregiver) {
          final isSelected = caregiver.id == _selectedId;
          return Padding(
            padding: const EdgeInsets.only(bottom: 4),
            child: Semantics(
              button: true,
              label: '${caregiver.name}, ${caregiver.displayRelationship}${isSelected ? ', selected' : ''}',
              child: InkWell(
                onTap: () => setState(() => _selectedId = caregiver.id),
                borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
                child: Container(
                  height: 56,
                  padding: const EdgeInsets.symmetric(horizontal: 16),
                  decoration: BoxDecoration(
                    color: EllaColors.bgSecondary,
                    borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
                  ),
                  child: Row(
                    children: [
                      Radio<String>(
                        value: caregiver.id,
                        groupValue: _selectedId,
                        onChanged: (value) => setState(() => _selectedId = value),
                        activeColor: EllaColors.primary,
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          '${caregiver.name} \u2014 ${caregiver.displayRelationship}',
                          style: const TextStyle(
                            fontSize: 18,
                            fontWeight: FontWeight.w400,
                            color: EllaColors.textPrimary,
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          );
        }),
        const SizedBox(height: 24),
        Semantics(
          button: true,
          label: context.l10n.ellaSave,
          child: InkWell(
            onTap: (_selectedId != null && _selectedId != _originalId && !_saving) ? _save : null,
            borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
            child: Container(
              height: 56,
              decoration: BoxDecoration(
                color: (_selectedId != null && _selectedId != _originalId) ? EllaColors.primary : EllaColors.bgTertiary,
                borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
              ),
              child: Center(
                child: Text(
                  context.l10n.ellaSave,
                  style: TextStyle(
                    fontSize: 20,
                    fontWeight: FontWeight.w600,
                    color: (_selectedId != null && _selectedId != _originalId)
                        ? EllaColors.textPrimary
                        : EllaColors.textDisabled,
                  ),
                ),
              ),
            ),
          ),
        ),
        const SizedBox(height: 32),
      ],
    );
  }
}
