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
  const EllaInviteSentScreen({super.key, required this.name, this.phone, required this.email, this.inviteCode});
  @override
  State<EllaInviteSentScreen> createState() => _EllaInviteSentScreenState();
}

class _EllaInviteSentScreenState extends State<EllaInviteSentScreen> with SingleTickerProviderStateMixin {
  final GlobalKey _shareButtonKey = GlobalKey();
  late AnimationController _scaleController;
  late Animation<double> _scaleAnimation;
  @override
  void initState() {
    super.initState();
    _scaleController = AnimationController(duration: const Duration(milliseconds: 300), vsync: this);
    _scaleAnimation = CurvedAnimation(parent: _scaleController, curve: Curves.easeOut);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      HapticFeedback.mediumImpact();
      if (MediaQuery.disableAnimationsOf(context)) {
        _scaleController.value = 1;
      } else {
        _scaleController.forward();
      }
    });
  }

  @override
  void dispose() {
    _scaleController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final buttonStyle = ElevatedButton.styleFrom(
      minimumSize: const Size(double.infinity, 56),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 16),
      foregroundColor: EllaColors.paper,
      backgroundColor: EllaColors.tealDeep,
      textStyle: const TextStyle(fontFamily: 'Manrope', fontSize: 18, fontWeight: FontWeight.w600),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
    );
    return Scaffold(
        backgroundColor: EllaColors.bgPrimary,
        body: SafeArea(
          child: LayoutBuilder(builder: (context, constraints) {
            return SingleChildScrollView(
                child: ConstrainedBox(
              constraints: BoxConstraints(minHeight: constraints.maxHeight),
              child: Padding(
                  padding: const EdgeInsets.all(24),
                  child: Column(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      ExcludeSemantics(
                          child: ScaleTransition(
                              key: const Key('invite-confirmation'),
                              scale: MediaQuery.disableAnimationsOf(context)
                                  ? const AlwaysStoppedAnimation<double>(1)
                                  : _scaleAnimation,
                              child: const Icon(Icons.check_circle_outline, size: 64, color: EllaColors.tealDeep))),
                      const SizedBox(height: 24),
                      Semantics(
                          header: true,
                          child: Text(context.l10n.ellaInviteSentTitle(widget.name),
                              textAlign: TextAlign.center,
                              style: const TextStyle(
                                  fontSize: 24, fontWeight: FontWeight.w700, color: EllaColors.textPrimary))),
                      const SizedBox(height: 16),
                      Text(context.l10n.ellaInviteSentDescription(widget.email),
                          textAlign: TextAlign.center,
                          style: const TextStyle(fontSize: 18, color: EllaColors.textSecondary, height: 1.5)),
                      if (widget.inviteCode != null && widget.inviteCode!.isNotEmpty) ...[
                        const SizedBox(height: 24),
                        Text(context.l10n.ellaInviteCodeLabel,
                            textAlign: TextAlign.center,
                            style: const TextStyle(fontSize: 16, color: EllaColors.textSecondary)),
                        const SizedBox(height: 8),
                        Text(widget.inviteCode!,
                            textAlign: TextAlign.center,
                            style: const TextStyle(
                                fontSize: 22,
                                fontWeight: FontWeight.w700,
                                color: EllaColors.textPrimary,
                                fontFamily: 'Manrope')),
                        const SizedBox(height: 8),
                        OutlinedButton(
                          key: const Key('invite-copy'),
                          style: OutlinedButton.styleFrom(
                              minimumSize: const Size(48, 52),
                              foregroundColor: EllaColors.tealDeep,
                              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
                              textStyle: const TextStyle(fontFamily: 'Manrope', fontSize: 18),
                              shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8))),
                          onPressed: () {
                            Clipboard.setData(ClipboardData(text: widget.inviteCode!));
                            HapticFeedback.lightImpact();
                            ScaffoldMessenger.of(context).showSnackBar(SnackBar(
                                content: Text(context.l10n.ellaInviteCodeCopied),
                                duration: const Duration(seconds: 2)));
                          },
                          child: Row(mainAxisSize: MainAxisSize.min, children: [
                            const Icon(Icons.copy, size: 24),
                            const SizedBox(width: 8),
                            Flexible(child: Text(context.l10n.copy, textAlign: TextAlign.center)),
                          ]),
                        ),
                      ],
                      const SizedBox(height: 16),
                      Text(context.l10n.ellaInviteExpiry,
                          textAlign: TextAlign.center,
                          style: const TextStyle(fontSize: 16, color: EllaColors.textSecondary)),
                      const SizedBox(height: 32),
                      ElevatedButton(
                        key: _shareButtonKey,
                        style: buttonStyle,
                        onPressed: () async {
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
                            final RenderBox? box = _shareButtonKey.currentContext?.findRenderObject() as RenderBox?;
                            Rect? sharePositionOrigin;
                            if (box != null) {
                              final position = box.localToGlobal(Offset.zero);
                              sharePositionOrigin =
                                  Rect.fromLTWH(position.dx, position.dy, box.size.width, box.size.height);
                            }
                            await Share.share(
                              shareText,
                              sharePositionOrigin: sharePositionOrigin,
                            );
                          } catch (_) {
                            if (mounted) {
                              ScaffoldMessenger.of(context).showSnackBar(
                                SnackBar(content: Text(context.l10n.wrappedFailedToShare)),
                              );
                            }
                          }
                        },
                        child: Row(children: [
                          const Icon(Icons.share, size: 24),
                          const SizedBox(width: 12),
                          Expanded(child: Text(context.l10n.ellaShareInvite, textAlign: TextAlign.center)),
                        ]),
                      ),
                      const SizedBox(height: 12),
                      SizedBox(
                          width: double.infinity,
                          child: TextButton(
                            key: const Key('invite-done'),
                            onPressed: () => Navigator.of(context).pop(),
                            style: TextButton.styleFrom(
                                minimumSize: const Size(48, 56),
                                foregroundColor: EllaColors.tealDeep,
                                padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 16),
                                textStyle:
                                    const TextStyle(fontFamily: 'Manrope', fontSize: 18, fontWeight: FontWeight.w600)),
                            child: Text(context.l10n.ellaDone, textAlign: TextAlign.center),
                          )),
                    ],
                  )),
            ));
          }),
        ));
  }
}
