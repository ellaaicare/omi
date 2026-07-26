import 'package:flutter/material.dart';

import 'package:provider/provider.dart';

import 'package:omi/ella/demo/ella_access_demo_fixtures.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/pages/ella_entitlement_gate_page.dart';
import 'package:omi/ella/pages/ella_provisioning_gate_page.dart';
import 'package:omi/ella/pages/ella_voice_chat_page.dart';
import 'package:omi/ella/services/ella_entitlement_service.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/providers/ella_entitlement_provider.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';
import 'package:omi/utils/l10n_extensions.dart';

enum EllaAccessDemoScenario {
  waitlist,
  inviteEntry,
  inviteLink,
  active,
  invalidCode,
  expiredCode,
  capacityFull,
  rateLimited,
  suspended,
  revoked,
  expiredEntitlement,
  provisioningTimeout,
  quotaSoftWarning,
  quotaDaily,
  quotaMonthly,
  quotaConcurrent,
  quotaSuspended,
  sessionMaximum,
  technicalVoiceFailure,
}

class EllaAccessDemoGalleryPage extends StatelessWidget {
  const EllaAccessDemoGalleryPage({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: EllaColors.paper,
      appBar: AppBar(
        backgroundColor: EllaColors.paper,
        foregroundColor: EllaColors.ink,
        title: Text(context.l10n.ellaDemoAccessTitle),
      ),
      body: ListView(
        padding: const EdgeInsets.all(EllaSizes.screenPadding),
        children: [
          Text(context.l10n.ellaDemoAccessBody, style: EllaTextStyles.secondary),
          const SizedBox(height: 18),
          for (final scenario in EllaAccessDemoScenario.values)
            Padding(
              padding: const EdgeInsets.only(bottom: 10),
              child: EllaCardSurface(
                child: ListTile(
                  minTileHeight: 58,
                  title: Text(_scenarioLabel(context, scenario), style: EllaTextStyles.body),
                  trailing: const Icon(Icons.chevron_right_rounded, color: EllaColors.inkSoft),
                  onTap: () => _openScenario(context, scenario),
                ),
              ),
            ),
        ],
      ),
    );
  }

  void _openScenario(BuildContext context, EllaAccessDemoScenario scenario) {
    final page = _scenarioPage(context, scenario);
    Navigator.of(context).push(MaterialPageRoute(builder: (_) => page));
  }

  Widget _scenarioPage(BuildContext context, EllaAccessDemoScenario scenario) {
    if (scenario == EllaAccessDemoScenario.provisioningTimeout) {
      final provider = EllaProvisioningProvider()
        ..state = EllaProvisioningState.degraded
        ..errorCode = 'provisioning_timeout'
        ..receipt = const EllaProvisioningReceipt(
          state: EllaProvisioningState.degraded,
          retryable: true,
          errorCode: 'provisioning_timeout',
        );
      return ChangeNotifierProvider.value(
        value: provider,
        child: Builder(
          builder: (context) => EllaProvisioningGatePage(
            readyChild: const SizedBox.shrink(),
            startOnMount: false,
            onSignOutOverride: () => Navigator.of(context).pop(),
          ),
        ),
      );
    }

    final policyReason = switch (scenario) {
      EllaAccessDemoScenario.quotaDaily => EllaVoicePolicyReason.quotaDaily,
      EllaAccessDemoScenario.quotaMonthly => EllaVoicePolicyReason.quotaMonthly,
      EllaAccessDemoScenario.quotaConcurrent => EllaVoicePolicyReason.concurrent,
      EllaAccessDemoScenario.quotaSuspended => EllaVoicePolicyReason.suspended,
      EllaAccessDemoScenario.sessionMaximum => EllaVoicePolicyReason.sessionMax,
      _ => null,
    };
    if (policyReason != null ||
        scenario == EllaAccessDemoScenario.quotaSoftWarning ||
        scenario == EllaAccessDemoScenario.technicalVoiceFailure) {
      final quota = switch (scenario) {
        EllaAccessDemoScenario.quotaSoftWarning => EllaAccessDemoFixtures.softDaily.quota,
        EllaAccessDemoScenario.quotaDaily => EllaAccessDemoFixtures.hardDaily.quota,
        EllaAccessDemoScenario.quotaMonthly => EllaAccessDemoFixtures.hardMonthly.quota,
        _ => EllaAccessDemoFixtures.active.quota,
      };
      return EllaVoiceChatPage(
        demoState: EllaVoiceDemoState(
          quota: quota,
          policyReason: policyReason,
          technicalFailure: scenario == EllaAccessDemoScenario.technicalVoiceFailure,
        ),
      );
    }

    final entitlement = switch (scenario) {
      EllaAccessDemoScenario.waitlist || EllaAccessDemoScenario.capacityFull => EllaAccessDemoFixtures.none,
      EllaAccessDemoScenario.suspended => EllaAccessDemoFixtures.suspended,
      EllaAccessDemoScenario.revoked => EllaAccessDemoFixtures.revoked,
      EllaAccessDemoScenario.expiredEntitlement => EllaAccessDemoFixtures.expired,
      EllaAccessDemoScenario.active => EllaAccessDemoFixtures.active,
      _ => EllaAccessDemoFixtures.invited,
    };
    final inviteError = switch (scenario) {
      EllaAccessDemoScenario.invalidCode => EllaInviteRedemptionError.invalid,
      EllaAccessDemoScenario.expiredCode => EllaInviteRedemptionError.expired,
      EllaAccessDemoScenario.capacityFull => EllaInviteRedemptionError.capacity,
      EllaAccessDemoScenario.rateLimited => EllaInviteRedemptionError.rateLimited,
      _ => null,
    };
    final inviteCode = scenario == EllaAccessDemoScenario.inviteLink ? 'ELLA7K9Q' : '';
    final provider = EllaEntitlementProvider.demo(
      initialEntitlement: entitlement,
      initialInviteError: inviteError,
      initialInviteCode: inviteCode,
    );
    return ChangeNotifierProvider.value(
      value: provider,
      child: Builder(
        builder: (context) => EllaEntitlementGatePage(
          startOnMount: false,
          onSignOutOverride: () => Navigator.of(context).pop(),
          readyChild: _DemoActivePage(onClose: () => Navigator.of(context).pop()),
        ),
      ),
    );
  }
}

class _DemoActivePage extends StatelessWidget {
  const _DemoActivePage({required this.onClose});

  final VoidCallback onClose;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: EllaColors.paper,
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(EllaSizes.screenPadding),
          child: EllaCardSurface(
            child: Padding(
              padding: const EdgeInsets.all(28),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(Icons.check_circle_outline_rounded, size: 48, color: EllaColors.tealDeep),
                  const SizedBox(height: 16),
                  Text(context.l10n.ellaDemoActiveTitle, style: EllaTextStyles.display, textAlign: TextAlign.center),
                  const SizedBox(height: 20),
                  FilledButton(onPressed: onClose, child: Text(context.l10n.close)),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

String _scenarioLabel(BuildContext context, EllaAccessDemoScenario scenario) => switch (scenario) {
      EllaAccessDemoScenario.waitlist => context.l10n.ellaDemoStateWaitlist,
      EllaAccessDemoScenario.inviteEntry => context.l10n.ellaDemoStateInviteEntry,
      EllaAccessDemoScenario.inviteLink => context.l10n.ellaDemoStateInviteLink,
      EllaAccessDemoScenario.active => context.l10n.ellaDemoStateActive,
      EllaAccessDemoScenario.invalidCode => context.l10n.ellaDemoStateInvalidCode,
      EllaAccessDemoScenario.expiredCode => context.l10n.ellaDemoStateExpiredCode,
      EllaAccessDemoScenario.capacityFull => context.l10n.ellaDemoStateCapacity,
      EllaAccessDemoScenario.rateLimited => context.l10n.ellaDemoStateRateLimited,
      EllaAccessDemoScenario.suspended => context.l10n.ellaDemoStateSuspended,
      EllaAccessDemoScenario.revoked => context.l10n.ellaDemoStateRevoked,
      EllaAccessDemoScenario.expiredEntitlement => context.l10n.ellaDemoStateExpiredEntitlement,
      EllaAccessDemoScenario.provisioningTimeout => context.l10n.ellaDemoStateProvisioningTimeout,
      EllaAccessDemoScenario.quotaSoftWarning => context.l10n.ellaDemoStateQuotaWarning,
      EllaAccessDemoScenario.quotaDaily => context.l10n.ellaDemoStateQuotaDaily,
      EllaAccessDemoScenario.quotaMonthly => context.l10n.ellaDemoStateQuotaMonthly,
      EllaAccessDemoScenario.quotaConcurrent => context.l10n.ellaDemoStateQuotaConcurrent,
      EllaAccessDemoScenario.quotaSuspended => context.l10n.ellaDemoStateQuotaSuspended,
      EllaAccessDemoScenario.sessionMaximum => context.l10n.ellaDemoStateSessionMaximum,
      EllaAccessDemoScenario.technicalVoiceFailure => context.l10n.ellaDemoStateTechnicalFailure,
    };
