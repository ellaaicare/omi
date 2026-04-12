import 'package:flutter/material.dart';

import 'package:intl/intl.dart';
import 'package:url_launcher/url_launcher.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/caregiver.dart';
import 'package:omi/ella/services/caregiver_api.dart' as caregiver_api;
import 'package:omi/ella/widgets/ella_permission_toggle.dart';
import 'package:omi/utils/l10n_extensions.dart';

class EllaCaregiverDetailPage extends StatefulWidget {
  final Caregiver caregiver;

  const EllaCaregiverDetailPage({super.key, required this.caregiver});

  @override
  State<EllaCaregiverDetailPage> createState() => _EllaCaregiverDetailPageState();
}

class _EllaCaregiverDetailPageState extends State<EllaCaregiverDetailPage> {
  late Caregiver _caregiver;
  late bool _dailySummary;
  late bool _isEmergencyContact;
  bool _resending = false;
  bool _loadingEmergency = true;

  @override
  void initState() {
    super.initState();
    _caregiver = widget.caregiver;
    _dailySummary = _caregiver.receiveDailySummary;
    _isEmergencyContact = false;
    _refreshFromBackend();
  }

  Future<void> _refreshFromBackend() async {
    try {
      final caregivers = await caregiver_api.getCaregivers();
      final emergencyId = await caregiver_api.getEmergencyContactId();
      final updated = caregivers.firstWhere(
        (c) => c.id == _caregiver.id,
        orElse: () => _caregiver,
      );
      if (mounted) {
        setState(() {
          _dailySummary = updated.receiveDailySummary;
          _caregiver = updated;
          _isEmergencyContact = emergencyId == updated.id;
          _loadingEmergency = false;
        });
      }
    } catch (_) {
      if (mounted) setState(() => _loadingEmergency = false);
    }
  }

  String _formatDate(DateTime? date) {
    if (date == null) return '';
    return DateFormat('MMM d, y').format(date);
  }

  Future<void> _toggleDailySummary(bool value) async {
    setState(() => _dailySummary = value);
    try {
      await caregiver_api.updateCaregiverPermissions(_caregiver.id, dailySummary: value);
    } catch (_) {
      if (mounted) setState(() => _dailySummary = !value);
    }
  }

  Future<void> _toggleEmergencyContact(bool value) async {
    final previousValue = _isEmergencyContact;
    setState(() => _isEmergencyContact = value);
    try {
      if (value) {
        await caregiver_api.setEmergencyContact(_caregiver.id);
      } else {
        // Clear emergency contact by setting to empty — backend clears the field
        await caregiver_api.clearEmergencyContact();
      }
    } catch (_) {
      if (mounted) setState(() => _isEmergencyContact = previousValue);
    }
  }

  Future<void> _resendInvite() async {
    if (_resending || _caregiver.phone == null) return;
    setState(() => _resending = true);
    try {
      await caregiver_api.resendInvite(
        uid: SharedPreferencesUtil().uid,
        caregiverId: _caregiver.id,
      );
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(context.l10n.ellaResendSuccess(_caregiver.name))),
        );
      }
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(context.l10n.ellaInviteErrorNetwork)),
        );
      }
    }
    if (mounted) setState(() => _resending = false);
  }

  Future<void> _removeCaregiver() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: EllaColors.bgSecondary,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(EllaSizes.radiusLarge)),
        title: Text(
          context.l10n.ellaRemoveConfirmTitle(_caregiver.name),
          style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
        ),
        content: Text(
          context.l10n.ellaRemoveConfirmDescription(_caregiver.name),
          style: const TextStyle(fontSize: 18, color: EllaColors.textSecondary, height: 1.5),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: Text(context.l10n.ellaCancel, style: const TextStyle(fontSize: 18, color: EllaColors.textTertiary)),
          ),
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: Text(context.l10n.ellaRemoveConfirmButton,
                style: const TextStyle(fontSize: 18, color: EllaColors.error)),
          ),
        ],
      ),
    );

    if (confirmed != true || !mounted) return;

    try {
      await caregiver_api.removeCaregiver(_caregiver.id);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(context.l10n.ellaRemoveSuccess(_caregiver.name))),
        );
        Navigator.of(context).pop();
      }
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(context.l10n.ellaInviteErrorNetwork)),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final cg = _caregiver;
    final statusColor = cg.isActive ? EllaColors.success : EllaColors.warning;
    final statusLabel = cg.isActive ? context.l10n.ellaCaregiverStatusActive : context.l10n.ellaCaregiverStatusInvited;
    final dateLabel = cg.isActive
        ? context.l10n.ellaJoinedDate(_formatDate(cg.joinedAt))
        : context.l10n.ellaInvitedDate(_formatDate(cg.invitedAt));

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
          cg.name,
          style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
        ),
      ),
      body: ListView(
        padding: const EdgeInsets.symmetric(horizontal: 24),
        children: [
          const SizedBox(height: 16),

          // Avatar
          Center(
            child: Container(
              width: 72,
              height: 72,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: EllaColors.primary.withValues(alpha: 0.15),
              ),
              child: Center(
                child: Text(
                  cg.initial,
                  style: const TextStyle(fontSize: 28, fontWeight: FontWeight.w600, color: EllaColors.primary),
                ),
              ),
            ),
          ),
          const SizedBox(height: 12),
          Center(
            child: Text(cg.name,
                style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: EllaColors.textPrimary)),
          ),
          const SizedBox(height: 4),
          Center(
            child: Text(cg.displayRelationship,
                style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w400, color: EllaColors.textTertiary)),
          ),

          const SizedBox(height: 24),

          // STATUS section
          _buildSectionHeader('STATUS'),
          Container(
            padding: const EdgeInsets.all(16),
            decoration: BoxDecoration(
              color: EllaColors.bgSecondary,
              borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Container(
                      width: 8,
                      height: 8,
                      decoration: BoxDecoration(shape: BoxShape.circle, color: statusColor),
                    ),
                    const SizedBox(width: 6),
                    Text(statusLabel, style: TextStyle(fontSize: 16, fontWeight: FontWeight.w400, color: statusColor)),
                  ],
                ),
                const SizedBox(height: 4),
                Text(dateLabel,
                    style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w400, color: EllaColors.textTertiary)),
                if (cg.isInvited) ...[
                  const SizedBox(height: 12),
                  Semantics(
                    button: true,
                    label: context.l10n.ellaResendInvite,
                    child: InkWell(
                      onTap: _resending ? null : _resendInvite,
                      child: Text(
                        context.l10n.ellaResendInvite,
                        style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w500, color: EllaColors.primary),
                      ),
                    ),
                  ),
                ],
              ],
            ),
          ),

          const SizedBox(height: 16),

          // CONTACT INFO section
          _buildSectionHeader('CONTACT INFO'),
          if (cg.phone != null)
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
              decoration: BoxDecoration(
                color: EllaColors.bgSecondary,
                borderRadius: cg.email != null
                    ? const BorderRadius.vertical(top: Radius.circular(EllaSizes.radiusLarge))
                    : BorderRadius.circular(EllaSizes.radiusLarge),
              ),
              child: Row(
                children: [
                  Expanded(
                    child: Text('Phone: ${_formatPhone(cg.phone)}',
                        style:
                            const TextStyle(fontSize: 18, fontWeight: FontWeight.w400, color: EllaColors.textPrimary)),
                  ),
                  Semantics(
                    button: true,
                    label: 'Call ${cg.name}',
                    child: IconButton(
                      icon: const Icon(Icons.phone, size: 20, color: EllaColors.primary),
                      onPressed: () => launchUrl(Uri.parse('tel:${cg.phone}')),
                    ),
                  ),
                ],
              ),
            ),
          if (cg.phone != null && cg.email != null)
            const Divider(height: 0.5, thickness: 0.5, color: EllaColors.bgTertiary, indent: 16, endIndent: 16),
          if (cg.email != null)
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
              decoration: BoxDecoration(
                color: EllaColors.bgSecondary,
                borderRadius: cg.phone != null
                    ? const BorderRadius.vertical(bottom: Radius.circular(EllaSizes.radiusLarge))
                    : BorderRadius.circular(EllaSizes.radiusLarge),
              ),
              child: Row(
                children: [
                  Expanded(
                    child: Text('Email: ${cg.email}',
                        style:
                            const TextStyle(fontSize: 18, fontWeight: FontWeight.w400, color: EllaColors.textPrimary)),
                  ),
                  Semantics(
                    button: true,
                    label: 'Email ${cg.name}',
                    child: IconButton(
                      icon: const Icon(Icons.email, size: 20, color: EllaColors.primary),
                      onPressed: () => launchUrl(Uri.parse('mailto:${cg.email}')),
                    ),
                  ),
                ],
              ),
            ),

          const SizedBox(height: 16),

          // NOTIFICATIONS section
          _buildSectionHeader('NOTIFICATIONS'),
          EllaPermissionToggle(
            title: context.l10n.ellaPermissionEmergencyAlerts,
            description: _isEmergencyContact
                ? '${cg.name} is your emergency contact and will receive critical alerts'
                : 'Tap to make ${cg.name} your emergency contact for critical alerts',
            isOn: _isEmergencyContact,
            onChanged: _loadingEmergency ? null : _toggleEmergencyContact,
            borderRadius: const BorderRadius.vertical(top: Radius.circular(EllaSizes.radiusLarge)),
          ),
          const Divider(height: 0.5, thickness: 0.5, color: EllaColors.bgTertiary, indent: 16, endIndent: 16),
          EllaPermissionToggle(
            title: context.l10n.ellaPermissionDailySummary,
            description: context.l10n.ellaPermissionDailySummaryDescription,
            isOn: _dailySummary,
            onChanged: _toggleDailySummary,
            borderRadius: const BorderRadius.vertical(bottom: Radius.circular(EllaSizes.radiusLarge)),
          ),

          const SizedBox(height: 32),

          // Remove button
          Semantics(
            button: true,
            label: 'Remove ${cg.name} from your care team',
            hint: 'Double tap to remove. You will be asked to confirm.',
            child: InkWell(
              onTap: _removeCaregiver,
              borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
              child: Container(
                height: 56,
                decoration: BoxDecoration(
                  borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
                ),
                child: Center(
                  child: Text(
                    context.l10n.ellaRemoveFromCareTeam,
                    style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w500, color: EllaColors.error),
                  ),
                ),
              ),
            ),
          ),

          const SizedBox(height: 32),
        ],
      ),
    );
  }

  Widget _buildSectionHeader(String text) {
    return Padding(
      padding: const EdgeInsets.only(left: 4, top: 8, bottom: 8),
      child: Text(
        text,
        style: const TextStyle(
          fontSize: 14,
          fontWeight: FontWeight.w600,
          color: EllaColors.primary,
          letterSpacing: 1.2,
        ),
      ),
    );
  }

  String _formatPhone(String? phone) {
    if (phone == null || phone.isEmpty) return '';
    final digits = phone.replaceAll(RegExp(r'[^\d]'), '');
    if (digits.length == 11 && digits.startsWith('1')) {
      return '+1 (${digits.substring(1, 4)}) ${digits.substring(4, 7)}-${digits.substring(7)}';
    }
    if (digits.length == 10) {
      return '(${digits.substring(0, 3)}) ${digits.substring(3, 6)}-${digits.substring(6)}';
    }
    return phone; // return as-is if non-US format
  }
}
