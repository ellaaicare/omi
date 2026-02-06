import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/l10n_extensions.dart';

class EllaInviteSentScreen extends StatefulWidget {
  final String name;
  final String phone;

  const EllaInviteSentScreen({
    super.key,
    required this.name,
    required this.phone,
  });

  @override
  State<EllaInviteSentScreen> createState() => _EllaInviteSentScreenState();
}

class _EllaInviteSentScreenState extends State<EllaInviteSentScreen> with SingleTickerProviderStateMixin {
  late AnimationController _scaleController;
  late Animation<double> _scaleAnimation;
  Timer? _autoPopTimer;

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

    _autoPopTimer = Timer(const Duration(seconds: 3), () {
      if (mounted) {
        Navigator.of(context).pop();
      }
    });
  }

  @override
  void dispose() {
    _scaleController.dispose();
    _autoPopTimer?.cancel();
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
                context.l10n.ellaInviteSentDescription(widget.phone),
                textAlign: TextAlign.center,
                style: const TextStyle(
                  fontSize: 18,
                  fontWeight: FontWeight.w400,
                  color: EllaColors.textSecondary,
                  height: 1.5,
                ),
              ),

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
