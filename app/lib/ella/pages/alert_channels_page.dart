import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/escalation_policy.dart';
import 'package:omi/ella/pages/ella_emergency_contact_page.dart';
import 'package:omi/ella/services/escalation_policy_api.dart' as policy_api;
import 'package:omi/utils/l10n_extensions.dart';

class AlertChannelsPage extends StatefulWidget {
  const AlertChannelsPage({super.key});

  @override
  State<AlertChannelsPage> createState() => _AlertChannelsPageState();
}

class _AlertChannelsPageState extends State<AlertChannelsPage> {
  bool _loading = true;
  EscalationPolicy? _policy;

  @override
  void initState() {
    super.initState();
    _loadPolicy();
  }

  Future<void> _loadPolicy() async {
    setState(() => _loading = true);
    final policy = await policy_api.getEscalationPolicy();
    if (mounted) {
      setState(() {
        _policy = policy;
        _loading = false;
      });
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
          icon: const Icon(Icons.arrow_back, color: EllaColors.textPrimary),
          onPressed: () => Navigator.of(context).pop(),
        ),
        title: Text(
          context.l10n.ellaAlertChannels,
          style: const TextStyle(
            fontSize: 22,
            fontWeight: FontWeight.w700,
            color: EllaColors.textPrimary,
          ),
        ),
        centerTitle: false,
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator(color: EllaColors.primary))
          : _policy == null
              ? _buildUnavailable()
              : RefreshIndicator(
                  color: EllaColors.primary,
                  onRefresh: _loadPolicy,
                  child: _buildPolicyContent(),
                ),
    );
  }

  Widget _buildUnavailable() {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.cloud_off, size: 48, color: EllaColors.textTertiary),
            const SizedBox(height: 16),
            Text(
              context.l10n.ellaAlertChannelsUnavailable,
              style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600, color: EllaColors.textPrimary),
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 8),
            Text(
              context.l10n.ellaAlertChannelsPullRetry,
              style: const TextStyle(fontSize: 14, color: EllaColors.textTertiary),
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 24),
            TextButton(
              onPressed: _loadPolicy,
              child: Text(context.l10n.ellaAlertChannelsRetry,
                  style: const TextStyle(fontSize: 16, color: EllaColors.primary)),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildPolicyContent() {
    final p = _policy!;
    return ListView(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      children: [
        // Intro subtitle
        if (p.display.subtitle.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(left: 4, bottom: 16),
            child: Text(
              p.display.subtitle,
              style: const TextStyle(fontSize: 14, color: EllaColors.textTertiary),
            ),
          ),

        // YOUR CHANNELS
        _SectionHeader(label: context.l10n.ellaAlertChannelsYourChannels),
        ...p.userChannels.map((c) => _ChannelStatusRow(channel: c)),
        if (p.userChannels.isEmpty)
          Padding(
            padding: const EdgeInsets.only(left: 4, top: 4, bottom: 8),
            child: Text(context.l10n.ellaAlertChannelsNoChannels,
                style: const TextStyle(fontSize: 14, color: EllaColors.textTertiary)),
          ),

        const SizedBox(height: 20),

        // EMERGENCY CONTACT
        _SectionHeader(label: context.l10n.ellaAlertChannelsEmergencyContact),
        _EmergencyContactCard(contact: p.emergencyContact),

        const SizedBox(height: 20),

        // CAREGIVER NOTIFICATIONS
        if (p.caregivers.isNotEmpty) ...[
          _SectionHeader(label: context.l10n.ellaAlertChannelsCaregiverNotifications),
          ...p.caregivers.map((c) => _CaregiverPolicyRow(caregiver: c)),
          const SizedBox(height: 20),
        ],

        // ALERT RULES
        _SectionHeader(label: context.l10n.ellaAlertChannelsAlertRules),
        ...p.rules.map((r) => _RuleCard(rule: r)),

        const SizedBox(height: 20),

        // PRIVACY NOTES
        if (p.privacyNotes.isNotEmpty) ...[
          _SectionHeader(label: context.l10n.ellaAlertChannelsPrivacyNotes),
          ...p.privacyNotes.map((note) => Padding(
                padding: const EdgeInsets.only(left: 4, bottom: 8),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text('•  ', style: TextStyle(fontSize: 14, color: EllaColors.textTertiary)),
                    Expanded(
                      child: Text(note, style: const TextStyle(fontSize: 14, color: EllaColors.textSecondary)),
                    ),
                  ],
                ),
              )),
          const SizedBox(height: 20),
        ],

        // Footer
        Center(
          child: Text(
            'Policy ${p.policyVersion}  •  ${p.generatedAt.substring(0, p.generatedAt.length > 19 ? 19 : p.generatedAt.length)}',
            style: const TextStyle(fontSize: 11, color: EllaColors.textTertiary),
          ),
        ),
        const SizedBox(height: 32),
      ],
    );
  }
}

// ── Private widgets ────────────────────────────────────────────────────────

class _SectionHeader extends StatelessWidget {
  final String label;
  const _SectionHeader({required this.label});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(left: 4, bottom: 8),
      child: Text(
        label,
        style: const TextStyle(
          fontSize: 13,
          fontWeight: FontWeight.w700,
          letterSpacing: 0.8,
          color: EllaColors.textTertiary,
        ),
      ),
    );
  }
}

class _ChannelStatusRow extends StatelessWidget {
  final ChannelStatus channel;
  const _ChannelStatusRow({required this.channel});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Container(
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: EllaColors.bgSecondary,
          borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
        ),
        child: Row(
          children: [
            Icon(channel.icon, size: 20, color: EllaColors.primary),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    channel.displayName,
                    style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w500, color: EllaColors.textPrimary),
                  ),
                  if (channel.reason.isNotEmpty)
                    Text(
                      channel.reason,
                      style: const TextStyle(fontSize: 13, color: EllaColors.textTertiary),
                    ),
                ],
              ),
            ),
            // Read-only badge
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
              decoration: BoxDecoration(
                color: channel.enabled
                    ? EllaColors.primary.withValues(alpha: 0.1)
                    : EllaColors.textTertiary.withValues(alpha: 0.1),
                borderRadius: BorderRadius.circular(8),
              ),
              child: Text(
                channel.enabled ? context.l10n.ellaAlertChannelsEnabled : context.l10n.ellaAlertChannelsDisabled,
                style: TextStyle(
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                  color: channel.enabled ? EllaColors.primary : EllaColors.textTertiary,
                ),
              ),
            ),
            const SizedBox(width: 6),
            const Icon(Icons.lock_outline, size: 14, color: EllaColors.textTertiary),
          ],
        ),
      ),
    );
  }
}

class _EmergencyContactCard extends StatelessWidget {
  final EmergencyContactPolicy contact;
  const _EmergencyContactCard({required this.contact});

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: EllaColors.bgSecondary,
        borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
      ),
      child: contact.configured
          ? Row(
              children: [
                Container(
                  width: 36,
                  height: 36,
                  decoration: BoxDecoration(
                    color: EllaColors.error.withValues(alpha: 0.1),
                    shape: BoxShape.circle,
                  ),
                  child: const Icon(Icons.emergency, size: 18, color: EllaColors.error),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        contact.displayName ?? 'Emergency Contact',
                        style:
                            const TextStyle(fontSize: 16, fontWeight: FontWeight.w500, color: EllaColors.textPrimary),
                      ),
                      if (contact.text.isNotEmpty)
                        Text(
                          contact.text,
                          style: const TextStyle(fontSize: 13, color: EllaColors.textTertiary),
                        ),
                    ],
                  ),
                ),
                _StatusDot(status: contact.status ?? 'ACTIVE'),
              ],
            )
          : InkWell(
              onTap: () => Navigator.push(context, MaterialPageRoute(builder: (_) => const EllaEmergencyContactPage())),
              borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
              child: Padding(
                padding: const EdgeInsets.symmetric(vertical: 4),
                child: Row(
                  children: [
                    const Icon(Icons.add_circle_outline, size: 20, color: EllaColors.primary),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            context.l10n.ellaAlertChannelsNotConfigured,
                            style: const TextStyle(
                                fontSize: 16, fontWeight: FontWeight.w500, color: EllaColors.textPrimary),
                          ),
                          Text(
                            context.l10n.ellaAlertChannelsSetUpContact,
                            style: const TextStyle(fontSize: 13, color: EllaColors.primary),
                          ),
                        ],
                      ),
                    ),
                    const Icon(Icons.chevron_right, size: 20, color: EllaColors.textTertiary),
                  ],
                ),
              ),
            ),
    );
  }
}

class _StatusDot extends StatelessWidget {
  final String status;
  const _StatusDot({required this.status});

  @override
  Widget build(BuildContext context) {
    final isActive = status == 'ACTIVE';
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Container(
          width: 8,
          height: 8,
          decoration: BoxDecoration(
            color: isActive ? EllaColors.primary : EllaColors.textTertiary,
            shape: BoxShape.circle,
          ),
        ),
        const SizedBox(width: 4),
        Text(
          status,
          style: TextStyle(
            fontSize: 11,
            fontWeight: FontWeight.w600,
            color: isActive ? EllaColors.primary : EllaColors.textTertiary,
          ),
        ),
      ],
    );
  }
}

class _CaregiverPolicyRow extends StatelessWidget {
  final CaregiverPolicyView caregiver;
  const _CaregiverPolicyRow({required this.caregiver});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Container(
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: EllaColors.bgSecondary,
          borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    caregiver.displayName,
                    style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w500, color: EllaColors.textPrimary),
                  ),
                ),
                if (caregiver.isEmergencyContact)
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                    decoration: BoxDecoration(
                      color: EllaColors.error.withValues(alpha: 0.1),
                      borderRadius: BorderRadius.circular(6),
                    ),
                    child: const Text(
                      'Emergency',
                      style: TextStyle(fontSize: 10, fontWeight: FontWeight.w600, color: EllaColors.error),
                    ),
                  ),
                const SizedBox(width: 8),
                _StatusDot(status: caregiver.status),
              ],
            ),
            if (caregiver.relationship != null)
              Padding(
                padding: const EdgeInsets.only(top: 2),
                child: Text(
                  caregiver.relationship!,
                  style: const TextStyle(fontSize: 13, color: EllaColors.textTertiary),
                ),
              ),
            const SizedBox(height: 8),
            // Channel chips
            Wrap(
              spacing: 6,
              runSpacing: 4,
              children: caregiver.channels
                  .map((c) => Container(
                        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                        decoration: BoxDecoration(
                          color: c.enabled
                              ? EllaColors.primary.withValues(alpha: 0.1)
                              : EllaColors.textTertiary.withValues(alpha: 0.1),
                          borderRadius: BorderRadius.circular(8),
                        ),
                        child: Text(
                          c.displayName,
                          style: TextStyle(
                            fontSize: 11,
                            fontWeight: FontWeight.w500,
                            color: c.enabled ? EllaColors.primary : EllaColors.textTertiary,
                          ),
                        ),
                      ))
                  .toList(),
            ),
            const SizedBox(height: 8),
            // Permissions
            Row(
              children: [
                _PermissionChip(label: 'Alerts', enabled: caregiver.permissions.emergencyAlerts),
                const SizedBox(width: 8),
                _PermissionChip(label: 'Daily', enabled: caregiver.permissions.dailySummary),
                const SizedBox(width: 8),
                _PermissionChip(label: 'Weekly', enabled: caregiver.permissions.weeklySummary),
              ],
            ),
            if (caregiver.plainLanguage.isNotEmpty) ...[
              const SizedBox(height: 6),
              Text(
                caregiver.plainLanguage,
                style: const TextStyle(fontSize: 13, color: EllaColors.textTertiary),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _PermissionChip extends StatelessWidget {
  final String label;
  final bool enabled;
  const _PermissionChip({required this.label, required this.enabled});

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(
          enabled ? Icons.check_circle : Icons.cancel,
          size: 14,
          color: enabled ? EllaColors.primary : EllaColors.textTertiary,
        ),
        const SizedBox(width: 3),
        Text(
          label,
          style: TextStyle(
            fontSize: 12,
            color: enabled ? EllaColors.textPrimary : EllaColors.textTertiary,
          ),
        ),
      ],
    );
  }
}

class _RuleCard extends StatelessWidget {
  final EscalationRule rule;
  const _RuleCard({required this.rule});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Container(
        decoration: BoxDecoration(
          color: EllaColors.bgSecondary,
          borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // Severity color bar
            Container(
              width: 4,
              height: 60,
              decoration: BoxDecoration(
                color: rule.severityColor,
                borderRadius: const BorderRadius.only(
                  topLeft: Radius.circular(EllaSizes.radiusMedium),
                  bottomLeft: Radius.circular(EllaSizes.radiusMedium),
                ),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Padding(
                padding: const EdgeInsets.symmetric(vertical: 10, horizontal: 4),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      rule.title,
                      style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w600, color: EllaColors.textPrimary),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      rule.text.isNotEmpty ? rule.text : rule.decisionLabel,
                      style: const TextStyle(fontSize: 13, color: EllaColors.textTertiary),
                      maxLines: 3,
                      overflow: TextOverflow.ellipsis,
                    ),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
