import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:share_plus/share_plus.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/l10n_extensions.dart';

class EllaInviteSentScreen extends StatefulWidget {
  final String name;
  final String? phone;
  final String email;
  final String? inviteCode;

  const EllaInviteSentScreen({
    super.key,
    required this.name,
    this.phone,
    required this.email,
    this.inviteCode,
  });

  @override
  State<EllaInviteSentScreen> createState() => _EllaInviteSentScreenState();
}

class _EllaInviteSentScreenState extends State<EllaInviteSentScreen> with SingleTickerProviderStateMixin {
  late AnimationController _scaleController;
  late Animation<double> _scaleAnimation;

  @override
  void initState() {
    super.initState();
    _scaleController = AnimationController(
      duration: const Duration(milliseconds: 300),
      vsync: this,
    );
    _scaleAnimation = CurvedAnimation(
      parent: _scaleController,
      curve: Curves.elasticOut,
    );

    WidgetsBinding.instance.addPostFrameCallback((_) {
      HapticFeedback.mediumImpact();
      _scaleController.forward();
    });
  }

  @override
  void dispose() {
    _scaleController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 24),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              const Spacer(),

              // Checkmark circle with animation
              ScaleTransition(
                scale: _scaleAnimation,
                child: Container(
                  width: 80,
                  height: 80,
                  decoration: const BoxDecoration(
                    shape: BoxShape.circle,
                    color: EllaColors.primary,
                  ),
                  child: const Icon(Icons.check, size: 36, color: EllaColors.textPrimary),
                ),
              ),

              const SizedBox(height: 24),

              // Title
              Text(
                context.l10n.ellaInviteSentTitle(widget.name),
                textAlign: TextAlign.center,
                style: const TextStyle(
                  fontSize: 28,
                  fontWeight: FontWeight.w700,
                  color: EllaColors.textPrimary,
                ),
              ),

              const SizedBox(height: 16),

              // Description
              Text(
                context.l10n.ellaInviteSentDescription(widget.email),
                textAlign: TextAlign.center,
                style: const TextStyle(
                  fontSize: 18,
                  fontWeight: FontWeight.w400,
                  color: EllaColors.textSecondary,
                  height: 1.5,
                ),
              ),

              // Invite code display
              if (widget.inviteCode != null && widget.inviteCode!.isNotEmpty) ...[
                const SizedBox(height: 24),
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 16),
                  decoration: BoxDecoration(
                    color: EllaColors.bgTertiary,
                    borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
                  ),
                  child: Column(
                    children: [
                      Text(
                        context.l10n.ellaInviteCodeLabel,
                        style: const TextStyle(
                          fontSize: 14,
                          fontWeight: FontWeight.w400,
                          color: EllaColors.textTertiary,
                        ),
                      ),
                      const SizedBox(height: 8),
                      GestureDetector(
                        onTap: () {
                          Clipboard.setData(ClipboardData(text: widget.inviteCode!));
                          HapticFeedback.lightImpact();
                          ScaffoldMessenger.of(context).showSnackBar(
                            SnackBar(
                              content: Text(context.l10n.ellaInviteCodeCopied),
                              duration: const Duration(seconds: 2),
                            ),
                          );
                        },
                        child: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Text(
                              widget.inviteCode!,
                              style: const TextStyle(
                                fontSize: 36,
                                fontWeight: FontWeight.w700,
                                color: EllaColors.textPrimary,
                                letterSpacing: 8,
                                fontFamily: 'monospace',
                              ),
                            ),
                            const SizedBox(width: 12),
                            const Icon(Icons.copy, size: 20, color: EllaColors.textTertiary),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
              ],

              const SizedBox(height: 16),

              // Expiry note
              Text(
                context.l10n.ellaInviteExpiry,
                textAlign: TextAlign.center,
                style: const TextStyle(
                  fontSize: 16,
                  fontWeight: FontWeight.w400,
                  color: EllaColors.textTertiary,
                ),
              ),

              const Spacer(),

              // Share button -- always show, with or without invite code
              Semantics(
                button: true,
                label: context.l10n.ellaShareInvite,
                child: InkWell(
                  onTap: () async {
                    final elderName = SharedPreferencesUtil().givenName.isNotEmpty
                        ? SharedPreferencesUtil().givenName
                        : 'Your loved one';
                    final hasCode = widget.inviteCode != null && widget.inviteCode!.isNotEmpty;
                    final shareText = hasCode
                        ? '$elderName invited you to join their Ella care team!\n\n'
                            'Your invite code: ${widget.inviteCode}\n\n'
                            'Join at: https://ella-ai-care.com/join'
                        : '$elderName invited you to join their Ella care team!\n\n'
                            'Download Ella: https://ella-ai-care.com';
                    try {
                      await SharePlus.instance.share(
                        ShareParams(text: shareText),
                      );
                    } catch (e) {
                      if (mounted) {
                        ScaffoldMessenger.of(context).showSnackBar(
                          const SnackBar(content: Text('Could not open share sheet')),
                        );
                      }
                    }
                  },
                  borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
                  child: Container(
                    height: 64,
                    width: double.infinity,
                    decoration: BoxDecoration(
                      color: EllaColors.bgTertiary,
                      borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
                      border: Border.all(color: EllaColors.primary, width: 2),
                    ),
                    child: Center(
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          const Icon(Icons.share, size: 20, color: EllaColors.primary),
                          const SizedBox(width: 8),
                          Text(
                            context.l10n.ellaShareInvite,
                            style: const TextStyle(
                              fontSize: 20,
                              fontWeight: FontWeight.w600,
                              color: EllaColors.primary,
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 12),

              // Done button
              Semantics(
                button: true,
                label: context.l10n.ellaDone,
                child: InkWell(
                  onTap: () => Navigator.of(context).pop(),
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
                        context.l10n.ellaDone,
                        style: const TextStyle(
                          fontSize: 20,
                          fontWeight: FontWeight.w600,
                          color: EllaColors.textPrimary,
                        ),
                      ),
                    ),
                  ),
                ),
              ),

              const SizedBox(height: 32),
            ],
          ),
        ),
      ),
    );
  }
}
