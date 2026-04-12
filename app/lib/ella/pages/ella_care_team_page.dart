import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/caregiver.dart';
import 'package:omi/ella/services/caregiver_api.dart' as caregiver_api;
import 'package:omi/ella/widgets/ella_caregiver_row.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/logger.dart';

class EllaCareTeamPage extends StatefulWidget {
  const EllaCareTeamPage({super.key});

  @override
  State<EllaCareTeamPage> createState() => _EllaCareTeamPageState();
}

class _EllaCareTeamPageState extends State<EllaCareTeamPage> {
  List<Caregiver> _caregivers = [];
  bool _loading = true;
  static const int _maxCaregivers = 5;

  @override
  void initState() {
    super.initState();
    _loadCaregivers();
  }

  Future<void> _loadCaregivers() async {
    try {
      final caregivers = await caregiver_api.getCaregivers();
      if (mounted) {
        setState(() {
          _caregivers = caregivers;
          _loading = false;
        });
      }
    } catch (e) {
      Logger.debug('Failed to load caregivers: $e');
      if (mounted) {
        setState(() => _loading = false);
      }
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
          context.l10n.ellaCareTeam,
          style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
        ),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator(color: EllaColors.primary))
          : RefreshIndicator(
              onRefresh: _loadCaregivers,
              color: EllaColors.primary,
              child: _caregivers.isEmpty ? _buildEmptyState(context) : _buildCaregiverList(context),
            ),
    );
  }

  Widget _buildEmptyState(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.symmetric(horizontal: 24),
      children: [
        const SizedBox(height: 80),
        const Icon(Icons.people_outline, size: 64, color: EllaColors.textDisabled),
        const SizedBox(height: 24),
        Text(
          context.l10n.ellaCareTeamEmpty,
          textAlign: TextAlign.center,
          style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
        ),
        const SizedBox(height: 12),
        Text(
          context.l10n.ellaCareTeamEmptyDescription,
          textAlign: TextAlign.center,
          style: const TextStyle(
            fontSize: 18,
            fontWeight: FontWeight.w400,
            color: EllaColors.textSecondary,
            height: 1.5,
          ),
        ),
        const SizedBox(height: 32),
        _buildAddButton(context),
      ],
    );
  }

  Widget _buildCaregiverList(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.symmetric(horizontal: 24),
      children: [
        const SizedBox(height: 8),
        Text(
          context.l10n.ellaCareTeamDescription,
          style: const TextStyle(
            fontSize: 18,
            fontWeight: FontWeight.w400,
            color: EllaColors.textSecondary,
            height: 1.5,
          ),
        ),
        const SizedBox(height: 16),
        ..._caregivers.map((caregiver) => Padding(
              padding: const EdgeInsets.only(bottom: 12),
              child: EllaCaregiverRow(
                caregiver: caregiver,
                onTap: () async {
                  await Navigator.push(
                    context,
                    MaterialPageRoute(
                      builder: (context) => EllaCaregiverDetailPage(caregiver: caregiver),
                    ),
                  );
                  _loadCaregivers();
                },
              ),
            )),
        const SizedBox(height: 8),
        if (_caregivers.length < _maxCaregivers) _buildAddButton(context),
        const SizedBox(height: 16),
        Text(
          _caregivers.length >= _maxCaregivers
              ? context.l10n.ellaCareTeamFull
              : context.l10n.ellaCareTeamSpotsUsed(_caregivers.length, _maxCaregivers),
          textAlign: TextAlign.center,
          style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w400, color: EllaColors.textTertiary),
        ),
        const SizedBox(height: 32),
      ],
    );
  }

  Widget _buildAddButton(BuildContext context) {
    return Semantics(
      button: true,
      label: 'Add family member to your care team',
      child: InkWell(
        onTap: () async {
          await Navigator.push(
            context,
            MaterialPageRoute(builder: (context) => const EllaAddCaregiverPage()),
          );
          _loadCaregivers();
        },
        borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
        child: Container(
          height: 64,
          decoration: BoxDecoration(
            color: EllaColors.primary,
            borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
          ),
          child: Center(
            child: Text(
              '+ ${context.l10n.ellaAddFamilyMember}',
              style: const TextStyle(
                fontSize: 20,
                fontWeight: FontWeight.w600,
                color: EllaColors.textPrimary,
              ),
            ),
          ),
        ),
      ),
    );
  }
}
