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
  String? _emergencyContactId;
  bool _loading = true;

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
          _emergencyContactId = emergencyId;
          _loading = false;
        });
      }
    } catch (e) {
      Logger.debug('Failed to load emergency contact data: $e');
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _toggleEmergencyContact(Caregiver caregiver, bool isSelected) async {
    final previousId = _emergencyContactId;
    setState(() {
      _emergencyContactId = isSelected ? caregiver.id : null;
    });
    try {
      if (isSelected) {
        await caregiver_api.setEmergencyContact(caregiver.id);
      } else {
        await caregiver_api.clearEmergencyContact();
      }
    } catch (_) {
      if (mounted) {
        setState(() => _emergencyContactId = previousId);
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
          final isSelected = caregiver.id == _emergencyContactId;
          return Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Semantics(
              button: true,
              label: '${caregiver.name}, ${caregiver.displayRelationship}${isSelected ? ', emergency contact' : ''}',
              hint: isSelected ? 'Double tap to remove as emergency contact' : 'Double tap to set as emergency contact',
              child: InkWell(
                onTap: () => _toggleEmergencyContact(caregiver, !isSelected),
                borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
                  decoration: BoxDecoration(
                    color: EllaColors.bgSecondary,
                    borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
                    border: isSelected
                        ? Border.all(color: EllaColors.primary, width: 2)
                        : null,
                  ),
                  child: Row(
                    children: [
                      Container(
                        width: 40,
                        height: 40,
                        decoration: BoxDecoration(
                          shape: BoxShape.circle,
                          color: isSelected ? EllaColors.primary.withOpacity(0.15) : EllaColors.bgTertiary,
                        ),
                        child: Center(
                          child: Text(
                            caregiver.initial,
                            style: TextStyle(
                              fontSize: 18,
                              fontWeight: FontWeight.w600,
                              color: isSelected ? EllaColors.primary : EllaColors.textSecondary,
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              caregiver.name,
                              style: TextStyle(
                                fontSize: 18,
                                fontWeight: FontWeight.w600,
                                color: isSelected ? EllaColors.primary : EllaColors.textPrimary,
                              ),
                            ),
                            const SizedBox(height: 2),
                            Text(
                              caregiver.displayRelationship,
                              style: const TextStyle(
                                fontSize: 16,
                                fontWeight: FontWeight.w400,
                                color: EllaColors.textTertiary,
                              ),
                            ),
                          ],
                        ),
                      ),
                      Icon(
                        isSelected ? Icons.check_circle : Icons.radio_button_unchecked,
                        color: isSelected ? EllaColors.primary : EllaColors.textDisabled,
                        size: 28,
                      ),
                    ],
                  ),
                ),
              ),
            ),
          );
        }),
        if (_emergencyContactId == null)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Text(
              'No emergency contact selected — tap a caregiver above to set one',
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w400, color: EllaColors.textTertiary),
            ),
          ),
        const SizedBox(height: 32),
      ],
    );
  }
}
