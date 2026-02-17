import 'package:flutter/material.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/l10n_extensions.dart';

class EllaProfilePage extends StatefulWidget {
  const EllaProfilePage({super.key});

  @override
  State<EllaProfilePage> createState() => _EllaProfilePageState();
}

class _EllaProfilePageState extends State<EllaProfilePage> {
  late TextEditingController _nameController;
  late TextEditingController _emailController;
  late TextEditingController _phoneController;
  bool _hasChanges = false;

  @override
  void initState() {
    super.initState();
    final prefs = SharedPreferencesUtil();
    _nameController = TextEditingController(text: prefs.givenName);
    _emailController = TextEditingController(text: prefs.email);
    _phoneController = TextEditingController(text: prefs.phoneNumber);
  }

  @override
  void dispose() {
    _nameController.dispose();
    _emailController.dispose();
    _phoneController.dispose();
    super.dispose();
  }

  void _onFieldChanged() {
    final prefs = SharedPreferencesUtil();
    final changed = _nameController.text.trim() != prefs.givenName || _phoneController.text.trim() != prefs.phoneNumber;
    if (changed != _hasChanges) {
      setState(() => _hasChanges = changed);
    }
  }

  void _save() {
    final prefs = SharedPreferencesUtil();
    final name = _nameController.text.trim();
    final phone = _phoneController.text.trim();
    if (name.isNotEmpty) prefs.givenName = name;
    if (phone != prefs.phoneNumber) prefs.phoneNumber = phone;
    setState(() => _hasChanges = false);
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(context.l10n.ellaProfileSaved)),
    );
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
          context.l10n.ellaProfileTitle,
          style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
        ),
      ),
      body: GestureDetector(
        onTap: () => primaryFocus?.unfocus(),
        child: ListView(
          padding: const EdgeInsets.symmetric(horizontal: 24),
          children: [
            const SizedBox(height: 24),

            // Name field (editable)
            _buildLabel(context.l10n.ellaProfileName),
            const SizedBox(height: 8),
            _buildEditableField(
              controller: _nameController,
              placeholder: 'Your name',
              keyboardType: TextInputType.name,
            ),

            const SizedBox(height: 20),

            // Email field (read-only — from Firebase)
            _buildLabel(context.l10n.ellaProfileEmail),
            const SizedBox(height: 8),
            _buildReadOnlyField(
              value: _emailController.text.isNotEmpty ? _emailController.text : context.l10n.ellaProfileNotSet,
            ),

            const SizedBox(height: 20),

            // Phone field (editable)
            _buildLabel(context.l10n.ellaProfilePhone),
            const SizedBox(height: 8),
            _buildEditableField(
              controller: _phoneController,
              placeholder: context.l10n.ellaProfileNotSet,
              keyboardType: TextInputType.phone,
            ),

            const SizedBox(height: 32),

            // Save button
            if (_hasChanges)
              InkWell(
                onTap: _save,
                borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
                child: Container(
                  height: 56,
                  decoration: BoxDecoration(
                    color: EllaColors.primary,
                    borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
                  ),
                  child: Center(
                    child: Text(
                      context.l10n.ellaProfileSave,
                      style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w600, color: EllaColors.textPrimary),
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

  Widget _buildLabel(String text) {
    return Text(
      text,
      style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w400, color: EllaColors.textTertiary),
    );
  }

  Widget _buildEditableField({
    required TextEditingController controller,
    required String placeholder,
    TextInputType keyboardType = TextInputType.text,
  }) {
    return Container(
      height: 56,
      decoration: BoxDecoration(
        color: EllaColors.bgTertiary,
        borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
      ),
      child: TextField(
        controller: controller,
        keyboardType: keyboardType,
        onChanged: (_) => _onFieldChanged(),
        style: const TextStyle(fontSize: 20, color: EllaColors.textPrimary),
        decoration: InputDecoration(
          hintText: placeholder,
          hintStyle: const TextStyle(fontSize: 18, color: EllaColors.textDisabled),
          border: InputBorder.none,
          contentPadding: const EdgeInsets.symmetric(horizontal: 20, vertical: 16),
        ),
      ),
    );
  }

  Widget _buildReadOnlyField({required String value}) {
    return Container(
      height: 56,
      padding: const EdgeInsets.symmetric(horizontal: 20),
      decoration: BoxDecoration(
        color: EllaColors.bgTertiary.withOpacity(0.5),
        borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
      ),
      alignment: Alignment.centerLeft,
      child: Text(
        value,
        style: const TextStyle(fontSize: 20, color: EllaColors.textSecondary),
      ),
    );
  }
}
