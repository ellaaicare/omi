import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:share_plus/share_plus.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/caregiver.dart';
import 'package:omi/ella/services/caregiver_api.dart' as caregiver_api;
import 'package:omi/utils/l10n_extensions.dart';

class EllaInviteSentScreen extends StatefulWidget {
  final String name;
  final String? phone;
  final String email;
  final String? inviteCode;
  final String? caregiverId;
  final bool emailSent;
  final String? deliveryError;

  const EllaInviteSentScreen({
    super.key,
    required this.name,
    this.phone,
    required this.email,
    this.inviteCode,
    this.caregiverId,
    this.emailSent = true,
    this.deliveryError,
  });

  @override
  State<EllaInviteSentScreen> createState() => _EllaInviteSentScreenState();
}

class _EllaInviteSentScreenState extends State<EllaInviteSentScreen> with SingleTickerProviderStateMixin {
  final GlobalKey _shareButtonKey = GlobalKey();
  late AnimationController _scaleController;
  late Animation<double> _scaleAnimation;
  late bool _emailSent;
  late String? _inviteCode;
  String? _deliveryError;
  bool _retryingEmail = false;

  @override
  void initState() {
    super.initState();
    _emailSent = widget.emailSent;
    _inviteCode = widget.inviteCode;
    _deliveryError = widget.deliveryError;
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

  Future<void> _retryEmail() async {
    final caregiverId = widget.caregiverId;
    if (_retryingEmail || caregiverId == null || caregiverId.isEmpty) return;

    setState(() => _retryingEmail = true);
    try {
      final response = await caregiver_api.resendInvite(
        uid: SharedPreferencesUtil().uid,
        caregiverId: caregiverId,
      );
      if (!mounted) return;
      if (response.inviteCode.isNotEmpty) {
        _inviteCode = response.inviteCode;
      }
      if (response.emailSent) {
        setState(() {
          _emailSent = true;
          _deliveryError = null;
        });
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(context.l10n.ellaRetryEmailSuccess)),
        );
      } else {
        setState(() {
          _emailSent = false;
          _deliveryError = response.deliveryError ?? response.failureReason;
        });
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(context.l10n.ellaRetryEmailFailed)),
        );
      }
    } on CaregiverApiException catch (e) {
      if (!mounted) return;
      final inviteResponse = e.inviteResponse;
      setState(() {
        _emailSent = false;
        _deliveryError = inviteResponse?.deliveryError ?? inviteResponse?.failureReason ?? e.message;
        if (inviteResponse?.inviteCode.isNotEmpty == true) {
          _inviteCode = inviteResponse!.inviteCode;
        }
      });
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(context.l10n.ellaRetryEmailFailed)),
      );
    } catch (_) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(context.l10n.ellaRetryEmailFailed)),
      );
    } finally {
      if (mounted) setState(() => _retryingEmail = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final hasCode = _inviteCode != null && _inviteCode!.isNotEmpty;
    final canRetryEmail = widget.caregiverId != null && widget.caregiverId!.isNotEmpty;

    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 24),
          child: ListView(
            children: [
              const SizedBox(height: 32),

              // Checkmark circle with animation
              ScaleTransition(
                scale: _scaleAnimation,
                child: Container(
                  width: 80,
                  height: 80,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: _emailSent ? EllaColors.primary : EllaColors.warning,
                  ),
                  child: Icon(
                    _emailSent ? Icons.check : Icons.mark_email_unread_outlined,
                    size: 36,
                    color: EllaColors.textPrimary,
                  ),
                ),
              ),

              const SizedBox(height: 24),

              // Title
              Text(
                _emailSent ? context.l10n.ellaInviteSentTitle(widget.name) : context.l10n.ellaInviteEmailFailedTitle,
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
                _emailSent
                    ? context.l10n.ellaInviteSentDescription(widget.email)
                    : context.l10n.ellaInviteEmailFailedDescription(widget.email),
                textAlign: TextAlign.center,
                style: const TextStyle(
                  fontSize: 18,
                  fontWeight: FontWeight.w400,
                  color: EllaColors.textSecondary,
                  height: 1.5,
                ),
              ),

              if (!_emailSent && _deliveryError != null && _deliveryError!.isNotEmpty) ...[
                const SizedBox(height: 12),
                Text(
                  context.l10n.ellaInviteEmailFailedReason(_deliveryError!),
                  textAlign: TextAlign.center,
                  style: const TextStyle(
                    fontSize: 15,
                    fontWeight: FontWeight.w400,
                    color: EllaColors.textTertiary,
                    height: 1.4,
                  ),
                ),
              ],

              // Invite code display
              if (hasCode) ...[
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
                          Clipboard.setData(ClipboardData(text: _inviteCode!));
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
                              _inviteCode!,
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

              const SizedBox(height: 32),

              // Share button -- always show, with or without invite code
              Semantics(
                button: true,
                label: context.l10n.ellaShareInvite,
                child: InkWell(
                  onTap: () async {
                    final elderName = SharedPreferencesUtil().givenName.isNotEmpty
                        ? SharedPreferencesUtil().givenName
                        : 'Your loved one';
                    final shareText = hasCode
                        ? '$elderName invited you to join their Ella care team!\n\n'
                            'Your invite code: $_inviteCode\n\n'
                            'Join at: https://ella-ai-care.com/join'
                        : '$elderName invited you to join their Ella care team!\n\n'
                            'Download Ella: https://ella-ai-care.com';
                    try {
                      final RenderBox? box = _shareButtonKey.currentContext?.findRenderObject() as RenderBox?;
                      Rect? sharePositionOrigin;
                      if (box != null) {
                        final position = box.localToGlobal(Offset.zero);
                        sharePositionOrigin = Rect.fromLTWH(position.dx, position.dy, box.size.width, box.size.height);
                      }
                      await SharePlus.instance.share(
                        ShareParams(text: shareText, sharePositionOrigin: sharePositionOrigin),
                      );
                    } catch (e) {
                      if (!context.mounted) return;
                      ScaffoldMessenger.of(context).showSnackBar(
                        SnackBar(content: Text(context.l10n.ellaInviteErrorNetwork)),
                      );
                    }
                  },
                  borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
                  child: Container(
                    key: _shareButtonKey,
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

              if (!_emailSent && canRetryEmail) ...[
                Semantics(
                  button: true,
                  label: context.l10n.ellaRetryEmail,
                  child: InkWell(
                    onTap: _retryingEmail ? null : _retryEmail,
                    borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
                    child: Container(
                      height: 64,
                      width: double.infinity,
                      decoration: BoxDecoration(
                        color: EllaColors.primary,
                        borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
                      ),
                      child: Center(
                        child: _retryingEmail
                            ? const SizedBox(
                                width: 20,
                                height: 20,
                                child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white),
                              )
                            : Text(
                                context.l10n.ellaRetryEmail,
                                style: const TextStyle(
                                  fontSize: 20,
                                  fontWeight: FontWeight.w600,
                                  color: Colors.white,
                                ),
                              ),
                      ),
                    ),
                  ),
                ),
                const SizedBox(height: 12),
              ],

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
