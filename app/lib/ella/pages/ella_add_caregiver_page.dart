import 'package:flutter/cupertino.dart';
import 'package:flutter/material.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/caregiver.dart';
import 'package:omi/ella/pages/ella_invite_sent_screen.dart';
import 'package:omi/ella/services/caregiver_api.dart' as caregiver_api;
import 'package:omi/ella/widgets/ella_permission_toggle.dart';
import 'package:omi/ella/widgets/ella_relationship_picker.dart';
import 'package:omi/utils/l10n_extensions.dart';

class EllaAddCaregiverPage extends StatefulWidget {
  const EllaAddCaregiverPage({super.key});

  @override
  State<EllaAddCaregiverPage> createState() => _EllaAddCaregiverPageState();
}

class _EllaAddCaregiverPageState extends State<EllaAddCaregiverPage> {
  final _nameController = TextEditingController();
  final _phoneController = TextEditingController();
  final _emailController = TextEditingController();
  final _nameFocus = FocusNode();
  final _phoneFocus = FocusNode();
  final _emailFocus = FocusNode();

  String? _selectedRelationship;
  bool _dailySummary = true;
  bool _sending = false;
  String? _nameError;
  String? _emailError;
  String? _phoneError;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _nameFocus.requestFocus();
    });
  }

  @override
  void dispose() {
    _nameController.dispose();
    _phoneController.dispose();
    _emailController.dispose();
    _nameFocus.dispose();
    _phoneFocus.dispose();
    _emailFocus.dispose();
    super.dispose();
  }

  bool get _isValid {
    final email = _emailController.text.trim();
    return _nameController.text.trim().isNotEmpty &&
        email.isNotEmpty &&
        email.contains('@') &&
        _selectedRelationship != null;
  }

  String get _phoneDigits => _phoneController.text.replaceAll(RegExp(r'[^\d]'), '');

  bool _isValidEmail(String email) {
    return email.contains('@') && email.contains('.') && email.length >= 5;
  }

  bool _validate() {
    final email = _emailController.text.trim();
    final phone = _phoneController.text.trim();
    setState(() {
      _nameError = _nameController.text.trim().isEmpty ? context.l10n.ellaAddCaregiverErrorNameRequired : null;
      _emailError = email.isEmpty
          ? context.l10n.ellaAddCaregiverErrorEmailRequired
          : !_isValidEmail(email)
              ? context.l10n.ellaAddCaregiverErrorEmailInvalid
              : null;
      _phoneError = phone.isNotEmpty && _phoneDigits.length < 7 ? context.l10n.ellaInviteErrorInvalidPhone : null;
    });
    return _nameError == null && _emailError == null && _phoneError == null && _selectedRelationship != null;
  }

  Future<void> _sendInvite() async {
    if (!_validate() || _sending) return;

    setState(() => _sending = true);

    try {
      final inviteResponse = await caregiver_api.sendCaregiverInvite(
        name: _nameController.text.trim(),
        phone: _phoneController.text.trim().isNotEmpty ? _phoneController.text.trim() : null,
        email: _emailController.text.trim(),
        relationship: _selectedRelationship!,
        dailySummary: _dailySummary,
      );

      if (!mounted) return;

      Navigator.pushReplacement(
        context,
        MaterialPageRoute(
          builder: (context) => EllaInviteSentScreen(
            name: _nameController.text.trim(),
            email: _emailController.text.trim(),
            phone: _phoneController.text.trim().isNotEmpty ? _phoneController.text.trim() : null,
            inviteCode: inviteResponse.inviteCode,
          ),
        ),
      );
    } on CaregiverApiException catch (e) {
      if (!mounted) return;
      setState(() => _sending = false);

      if (e.statusCode == 409) {
        setState(() => _phoneError = context.l10n.ellaInviteErrorDuplicate);
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(context.l10n.ellaInviteErrorDuplicate)),
        );
      } else if (e.statusCode == 400) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(context.l10n.ellaCareTeamFullDialog)),
        );
      } else {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(context.l10n.ellaInviteErrorNetwork)),
        );
      }
    } catch (_) {
      if (!mounted) return;
      setState(() => _sending = false);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(context.l10n.ellaInviteErrorNetwork)),
      );
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
          context.l10n.ellaAddCaregiverTitle,
          style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
        ),
      ),
      body: GestureDetector(
        onTap: () => primaryFocus?.unfocus(),
        child: ListView(
          padding: const EdgeInsets.symmetric(horizontal: 24),
          children: [
            const SizedBox(height: 16),

            // Name field
            _buildFieldLabel(context.l10n.ellaAddCaregiverName),
            const SizedBox(height: 8),
            _buildTextField(
              controller: _nameController,
              focusNode: _nameFocus,
              placeholder: context.l10n.ellaAddCaregiverNamePlaceholder,
              keyboardType: TextInputType.name,
              textInputAction: TextInputAction.next,
              error: _nameError,
              onSubmitted: (_) => _emailFocus.requestFocus(),
            ),

            const SizedBox(height: 20),

            // Email field (required — primary invite channel)
            _buildFieldLabel(context.l10n.ellaAddCaregiverEmail),
            const SizedBox(height: 8),
            _buildTextField(
              controller: _emailController,
              focusNode: _emailFocus,
              placeholder: context.l10n.ellaAddCaregiverEmailPlaceholder,
              keyboardType: TextInputType.emailAddress,
              textInputAction: TextInputAction.next,
              error: _emailError,
              onSubmitted: (_) => _phoneFocus.requestFocus(),
            ),

            const SizedBox(height: 20),

            // Phone field (optional)
            _buildFieldLabel(context.l10n.ellaAddCaregiverPhone),
            const SizedBox(height: 8),
            _buildTextField(
              controller: _phoneController,
              focusNode: _phoneFocus,
              placeholder: context.l10n.ellaAddCaregiverPhonePlaceholder,
              keyboardType: TextInputType.phone,
              textInputAction: TextInputAction.done,
              error: _phoneError,
              onSubmitted: (_) => primaryFocus?.unfocus(),
            ),

            const SizedBox(height: 20),

            // Relationship picker
            _buildFieldLabel(context.l10n.ellaAddCaregiverRelationship),
            const SizedBox(height: 8),
            Semantics(
              button: true,
              label:
                  '${context.l10n.ellaAddCaregiverRelationship}. Current value: ${_selectedRelationship != null ? _relationshipDisplayName(context) : 'Not selected'}',
              hint: 'Double tap to choose a relationship',
              child: InkWell(
                onTap: () async {
                  final result = await EllaRelationshipPicker.show(context, current: _selectedRelationship);
                  if (result != null) {
                    setState(() => _selectedRelationship = result);
                  }
                },
                borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                child: Container(
                  height: 56,
                  padding: const EdgeInsets.symmetric(horizontal: 20),
                  decoration: BoxDecoration(
                    color: EllaColors.bgTertiary,
                    borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
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
            ),

            const SizedBox(height: 28),

            // Permissions header
            _buildFieldLabel(context.l10n.ellaPermissionsHeader),
            const SizedBox(height: 8),

            // Emergency alerts toggle (locked on)
            if (!SharedPreferencesUtil().publicMode) ...[
              EllaPermissionToggle(
                title: context.l10n.ellaPermissionEmergencyAlerts,
                description: context.l10n.ellaPermissionEmergencyAlertsDescription,
                isOn: true,
                locked: true,
                borderRadius: const BorderRadius.vertical(top: Radius.circular(EllaSizes.radiusLarge)),
              ),
              const Divider(height: 0.5, thickness: 0.5, color: EllaColors.bgTertiary, indent: 16, endIndent: 16),
            ],
            // Daily summary toggle
            EllaPermissionToggle(
              title: context.l10n.ellaPermissionDailySummary,
              description: context.l10n.ellaPermissionDailySummaryDescription,
              isOn: _dailySummary,
              onChanged: (value) => setState(() => _dailySummary = value),
              borderRadius: SharedPreferencesUtil().publicMode
                  ? BorderRadius.circular(EllaSizes.radiusLarge)
                  : const BorderRadius.vertical(bottom: Radius.circular(EllaSizes.radiusLarge)),
            ),

            const SizedBox(height: 32),

            // Send Invite button
            Semantics(
              button: true,
              label: context.l10n.ellaSendInvite,
              child: InkWell(
                onTap: _isValid && !_sending ? _sendInvite : null,
                borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
                child: AnimatedContainer(
                  duration: const Duration(milliseconds: 200),
                  height: 64,
                  decoration: BoxDecoration(
                    color: _sending
                        ? EllaColors.primary.withOpacity(0.6)
                        : _isValid
                            ? EllaColors.primary
                            : EllaColors.bgTertiary,
                    borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
                  ),
                  child: Center(
                    child: _sending
                        ? const CupertinoActivityIndicator(color: Colors.white)
                        : Text(
                            context.l10n.ellaSendInvite,
                            style: TextStyle(
                              fontSize: 20,
                              fontWeight: FontWeight.w600,
                              color: _isValid ? Colors.white : EllaColors.textDisabled,
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
    );
  }

  Widget _buildFieldLabel(String text) {
    return Text(
      text,
      style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w400, color: EllaColors.textTertiary),
    );
  }

  Widget _buildTextField({
    required TextEditingController controller,
    required FocusNode focusNode,
    required String placeholder,
    required TextInputType keyboardType,
    TextInputAction textInputAction = TextInputAction.next,
    String? error,
    ValueChanged<String>? onSubmitted,
  }) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Container(
          height: 56,
          decoration: BoxDecoration(
            color: EllaColors.bgTertiary,
            borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
            border: error != null ? Border.all(color: EllaColors.error, width: 2) : null,
          ),
          child: TextField(
            controller: controller,
            focusNode: focusNode,
            keyboardType: keyboardType,
            textInputAction: textInputAction,
            onSubmitted: onSubmitted,
            onChanged: (_) => setState(() {
              _nameError = null;
              _emailError = null;
              _phoneError = null;
            }),
            style: const TextStyle(fontSize: 20, color: EllaColors.textPrimary),
            decoration: InputDecoration(
              hintText: placeholder,
              hintStyle: const TextStyle(fontSize: 18, color: EllaColors.textDisabled),
              border: InputBorder.none,
              contentPadding: const EdgeInsets.symmetric(horizontal: 20, vertical: 16),
            ),
          ),
        ),
        if (error != null) ...[
          const SizedBox(height: 4),
          Padding(
            padding: const EdgeInsets.only(left: 4),
            child: Text(
              error,
              style: const TextStyle(fontSize: 16, color: EllaColors.error),
            ),
          ),
        ],
      ],
    );
  }
}
