import 'package:flutter/material.dart';

import 'package:provider/provider.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/providers/user_provider.dart';
import 'package:omi/utils/l10n_extensions.dart';

class ConversationLifecycleSettingsPage extends StatelessWidget {
  const ConversationLifecycleSettingsPage({super.key});

  static const List<int> _durationOptions = [30 * 60, 45 * 60, 60 * 60, 90 * 60];

  @override
  Widget build(BuildContext context) {
    final provider = context.watch<UserProvider>();

    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      appBar: AppBar(
        backgroundColor: EllaColors.bgPrimary,
        elevation: 0,
        title: Text(
          context.l10n.ellaMaxConversationLengthTitle,
          style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
        ),
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Text(
            context.l10n.ellaMaxConversationLengthDescription,
            style: const TextStyle(fontSize: 16, height: 1.4, color: EllaColors.textSecondary),
          ),
          const SizedBox(height: 20),
          _ConversationLifecycleOption(
            title: context.l10n.ellaMaxConversationLengthBackendDefault,
            subtitle: context.l10n.ellaMaxConversationLengthBackendDefaultDesc,
            selected:
                provider.conversationMaxDurationEnabled == null && provider.conversationMaxDurationSeconds == null,
            saving: provider.isUpdatingConversationLifecyclePreferences,
            onTap: () => _save(context, provider, enabled: null, seconds: null),
          ),
          const SizedBox(height: 8),
          ..._durationOptions.map((seconds) {
            final minutes = seconds ~/ 60;
            return Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: _ConversationLifecycleOption(
                title: context.l10n.minLabel(minutes),
                subtitle: context.l10n.ellaMaxConversationLengthMinutesDesc(minutes),
                selected:
                    provider.conversationMaxDurationEnabled == true &&
                    provider.conversationMaxDurationSeconds == seconds,
                saving: provider.isUpdatingConversationLifecyclePreferences,
                onTap: () => _save(context, provider, enabled: true, seconds: seconds),
              ),
            );
          }),
          _ConversationLifecycleOption(
            title: context.l10n.ellaMaxConversationLengthDisabled,
            subtitle: context.l10n.ellaMaxConversationLengthDisabledDesc,
            selected: provider.conversationMaxDurationEnabled == false,
            saving: provider.isUpdatingConversationLifecyclePreferences,
            onTap: () => _save(context, provider, enabled: false, seconds: null),
          ),
        ],
      ),
    );
  }

  Future<void> _save(
    BuildContext context,
    UserProvider provider, {
    required bool? enabled,
    required int? seconds,
  }) async {
    final messenger = ScaffoldMessenger.of(context);
    final success = await provider.updateConversationMaxDuration(enabled: enabled, seconds: seconds);
    if (!context.mounted) return;
    messenger.showSnackBar(
      SnackBar(
        content: Text(
          success ? context.l10n.ellaMaxConversationLengthSaved : context.l10n.ellaMaxConversationLengthSaveFailed,
        ),
      ),
    );
  }
}

class _ConversationLifecycleOption extends StatelessWidget {
  final String title;
  final String subtitle;
  final bool selected;
  final bool saving;
  final VoidCallback onTap;

  const _ConversationLifecycleOption({
    required this.title,
    required this.subtitle,
    required this.selected,
    required this.saving,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Semantics(
      button: true,
      selected: selected,
      child: InkWell(
        onTap: saving ? null : onTap,
        borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 160),
          padding: const EdgeInsets.all(18),
          decoration: BoxDecoration(
            color: EllaColors.bgSecondary,
            borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
            border: Border.all(color: selected ? EllaColors.primary : EllaColors.bgTertiary, width: selected ? 2 : 1),
          ),
          child: Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      title,
                      style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600, color: EllaColors.textPrimary),
                    ),
                    const SizedBox(height: 4),
                    Text(subtitle, style: const TextStyle(fontSize: 15, height: 1.35, color: EllaColors.textSecondary)),
                  ],
                ),
              ),
              const SizedBox(width: 12),
              if (saving && selected)
                const SizedBox(
                  width: 20,
                  height: 20,
                  child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.primary),
                )
              else if (selected)
                const Icon(Icons.check_circle, color: EllaColors.primary, size: 24)
              else
                const Icon(Icons.radio_button_unchecked, color: EllaColors.textTertiary, size: 24),
            ],
          ),
        ),
      ),
    );
  }
}
