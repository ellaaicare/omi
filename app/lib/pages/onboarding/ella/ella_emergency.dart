import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/caregiver_api.dart' as caregiver_api;
import 'package:omi/ella/widgets/ella_relationship_picker.dart';
import 'package:omi/utils/l10n_extensions.dart';

class EllaEmergency extends StatefulWidget {
  final VoidCallback onComplete;
  final VoidCallback onSkip;
  final VoidCallback onBack;

  const EllaEmergency({super.key, required this.onComplete, required this.onSkip, required this.onBack});

  @override
  State<EllaEmergency> createState() => _EllaEmergencyState();
}

class _EllaEmergencyState extends State<EllaEmergency> {
  final _nameController = TextEditingController();
  final _phoneController = TextEditingController();
  final _emailController = TextEditingController();
  String? _selectedRelationship;
  bool _isSubmitting = false;

  @override
  void dispose() {
    _nameController.dispose();
    _phoneController.dispose();
    _emailController.dispose();
    super.dispose();
  }

  bool get _isValid =>
      _nameController.text.trim().isNotEmpty &&
      _phoneController.text.trim().isNotEmpty &&
      _emailController.text.trim().isNotEmpty &&
      _selectedRelationship != null;

  Future<void> _submit() async {
    if (!_isValid || _isSubmitting) return;
    setState(() => _isSubmitting = true);

    try {
      await caregiver_api.createEmergencyContact(
        name: _nameController.text.trim(),
        phone: _phoneController.text.trim(),
        email: _emailController.text.trim(),
        relationship: _selectedRelationship ?? 'other',
      );
      if (mounted) widget.onComplete();
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.somethingWentWrong)));
      }
    } finally {
      if (mounted) setState(() => _isSubmitting = false);
    }
  }

  String _relationshipDisplayName(BuildContext context) {
    if (_selectedRelationship == null) return context.l10n.ellaAddCaregiverRelationshipSelect;
    switch (_selectedRelationship) {
      case 'daughter':
        return context.l10n.ellaRelationshipDaughter;
      case 'son':
        return context.l10n.ellaRelationshipSon;
      case 'spouse':
        return context.l10n.ellaRelationshipSpouse;
      case 'sibling':
        return context.l10n.ellaRelationshipSibling;
      case 'friend':
        return context.l10n.ellaRelationshipFriend;
      case 'doctor':
        return context.l10n.ellaRelationshipDoctor;
      case 'other':
        return context.l10n.ellaRelationshipOther;
      default:
        return _selectedRelationship!;
    }
  }

  void _handleSkip() {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(context.l10n.ellaSkipConfirmation),
        backgroundColor: EllaColors.bgSecondary,
        behavior: SnackBarBehavior.floating,
      ),
    );
    widget.onSkip();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      body: SafeArea(
        child: GestureDetector(
          onTap: () => FocusScope.of(context).unfocus(),
          behavior: HitTestBehavior.opaque,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 32),
            child: SingleChildScrollView(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const SizedBox(height: 24),
                  Row(
                    children: [
                      GestureDetector(
                        onTap: widget.onBack,
                        child: Container(
                          width: 48,
                          height: 48,
                          decoration: const BoxDecoration(color: EllaColors.bgTertiary, shape: BoxShape.circle),
                          child: const Icon(Icons.arrow_back, color: EllaColors.textPrimary, size: 24),
                        ),
                      ),
                      Expanded(
                        child: Text(
                          context.l10n.ellaOnboardingStep(3, 3),
                          textAlign: TextAlign.center,
                          style: const TextStyle(fontSize: 16, color: EllaColors.textTertiary),
                        ),
                      ),
                      const SizedBox(width: 48),
                    ],
                  ),
                  const SizedBox(height: 16),
                  // Hero illustration
                  Center(
                    child: ClipRRect(
                      borderRadius: BorderRadius.circular(24),
                      child: Image.asset('assets/images/ella_onboarding_3.png', height: 200, fit: BoxFit.contain),
                    ),
                  ),
                  const SizedBox(height: 24),
                  Text(
                    context.l10n.ellaEmergencyTitle,
                    style: const TextStyle(fontSize: 28, fontWeight: FontWeight.bold, color: EllaColors.textPrimary),
                  ),
                  const SizedBox(height: 12),
                  Text(
                    context.l10n.ellaEmergencySubtitle,
                    style: const TextStyle(fontSize: 20, color: EllaColors.textSecondary, height: 1.4),
                  ),
                  const SizedBox(height: 32),
                  Text(
                    context.l10n.ellaEmergencyNameLabel,
                    style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600, color: EllaColors.textPrimary),
                  ),
                  const SizedBox(height: 8),
                  SizedBox(
                    height: 56,
                    child: TextField(
                      controller: _nameController,
                      autofocus: true,
                      textCapitalization: TextCapitalization.words,
                      style: const TextStyle(fontSize: 20, color: EllaColors.textPrimary),
                      decoration: InputDecoration(
                        hintText: context.l10n.ellaEmergencyNamePlaceholder,
                        hintStyle: const TextStyle(fontSize: 20, color: EllaColors.textDisabled),
                        filled: true,
                        fillColor: Colors.white,
                        border: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(16),
                          borderSide: const BorderSide(color: EllaColors.bgTertiary),
                        ),
                        enabledBorder: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(16),
                          borderSide: const BorderSide(color: EllaColors.bgTertiary),
                        ),
                        focusedBorder: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(16),
                          borderSide: const BorderSide(color: EllaColors.primary, width: 1.5),
                        ),
                        contentPadding: const EdgeInsets.symmetric(horizontal: 20, vertical: 16),
                      ),
                      textInputAction: TextInputAction.next,
                      onChanged: (_) => setState(() {}),
                    ),
                  ),
                  const SizedBox(height: 20),
                  // Relationship picker
                  Text(
                    context.l10n.ellaAddCaregiverRelationship,
                    style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600, color: EllaColors.textPrimary),
                  ),
                  const SizedBox(height: 8),
                  InkWell(
                    onTap: () async {
                      final result = await EllaRelationshipPicker.show(context, current: _selectedRelationship);
                      if (result != null) setState(() => _selectedRelationship = result);
                    },
                    borderRadius: BorderRadius.circular(16),
                    child: Container(
                      height: 56,
                      padding: const EdgeInsets.symmetric(horizontal: 20),
                      decoration: BoxDecoration(
                        color: Colors.white,
                        borderRadius: BorderRadius.circular(16),
                        border: Border.all(color: EllaColors.bgTertiary),
                      ),
                      child: Row(
                        children: [
                          Expanded(
                            child: Text(
                              _relationshipDisplayName(context),
                              style: TextStyle(
                                fontSize: 20,
                                color: _selectedRelationship != null ? EllaColors.textPrimary : EllaColors.textDisabled,
                              ),
                            ),
                          ),
                          const Icon(Icons.keyboard_arrow_down, size: 24, color: EllaColors.textTertiary),
                        ],
                      ),
                    ),
                  ),
                  const SizedBox(height: 20),
                  Text(
                    context.l10n.ellaEmergencyPhoneLabel,
                    style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600, color: EllaColors.textPrimary),
                  ),
                  const SizedBox(height: 8),
                  SizedBox(
                    height: 56,
                    child: TextField(
                      controller: _phoneController,
                      keyboardType: TextInputType.phone,
                      style: const TextStyle(fontSize: 20, color: EllaColors.textPrimary),
                      decoration: InputDecoration(
                        hintText: context.l10n.ellaEmergencyPhonePlaceholder,
                        hintStyle: const TextStyle(fontSize: 20, color: EllaColors.textDisabled),
                        filled: true,
                        fillColor: Colors.white,
                        border: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(16),
                          borderSide: const BorderSide(color: EllaColors.bgTertiary),
                        ),
                        enabledBorder: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(16),
                          borderSide: const BorderSide(color: EllaColors.bgTertiary),
                        ),
                        focusedBorder: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(16),
                          borderSide: const BorderSide(color: EllaColors.primary, width: 1.5),
                        ),
                        contentPadding: const EdgeInsets.symmetric(horizontal: 20, vertical: 16),
                      ),
                      textInputAction: TextInputAction.next,
                      onChanged: (_) => setState(() {}),
                    ),
                  ),
                  const SizedBox(height: 20),
                  // A Care Team contact needs an email address for its invite.
                  Text(
                    context.l10n.ellaAddCaregiverEmail,
                    style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w400, color: EllaColors.textTertiary),
                  ),
                  const SizedBox(height: 8),
                  SizedBox(
                    height: 56,
                    child: TextField(
                      controller: _emailController,
                      keyboardType: TextInputType.emailAddress,
                      style: const TextStyle(fontSize: 20, color: EllaColors.textPrimary),
                      decoration: InputDecoration(
                        hintText: context.l10n.ellaAddCaregiverEmailPlaceholder,
                        hintStyle: const TextStyle(fontSize: 20, color: EllaColors.textDisabled),
                        filled: true,
                        fillColor: Colors.white,
                        border: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(16),
                          borderSide: const BorderSide(color: EllaColors.bgTertiary),
                        ),
                        enabledBorder: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(16),
                          borderSide: const BorderSide(color: EllaColors.bgTertiary),
                        ),
                        focusedBorder: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(16),
                          borderSide: const BorderSide(color: EllaColors.primary, width: 1.5),
                        ),
                        contentPadding: const EdgeInsets.symmetric(horizontal: 20, vertical: 16),
                      ),
                      textInputAction: TextInputAction.done,
                      onSubmitted: (_) => _submit(),
                    ),
                  ),
                  const SizedBox(height: 32),
                  SizedBox(
                    width: double.infinity,
                    height: 64,
                    child: ElevatedButton(
                      onPressed: _isValid && !_isSubmitting ? _submit : null,
                      style: ElevatedButton.styleFrom(
                        backgroundColor: _isValid ? EllaColors.primary : EllaColors.bgTertiary,
                        foregroundColor: _isValid ? Colors.white : EllaColors.textDisabled,
                        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
                        elevation: 0,
                      ),
                      child: _isSubmitting
                          ? const SizedBox(
                              width: 24,
                              height: 24,
                              child: CircularProgressIndicator(color: Colors.white, strokeWidth: 2.5),
                            )
                          : Text(
                              context.l10n.ellaGetStarted,
                              style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
                            ),
                    ),
                  ),
                  const SizedBox(height: 16),
                  Center(
                    child: TextButton(
                      onPressed: _isSubmitting ? null : _handleSkip,
                      style: TextButton.styleFrom(minimumSize: const Size(double.infinity, 48)),
                      child: Text(
                        context.l10n.ellaSkipForNow,
                        style: const TextStyle(fontSize: 18, color: EllaColors.textTertiary),
                      ),
                    ),
                  ),
                  const SizedBox(height: 32),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
