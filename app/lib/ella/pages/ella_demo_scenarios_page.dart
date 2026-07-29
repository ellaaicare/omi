import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/pages/ella_access_demo_gallery_page.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/logger.dart';

class DemoScenario {
  final String id;
  final String name;
  final String category;
  final String trigger;
  final String ellaResponse;
  final String? notes;

  const DemoScenario({
    required this.id,
    required this.name,
    required this.category,
    required this.trigger,
    required this.ellaResponse,
    this.notes,
  });

  factory DemoScenario.fromJson(Map<String, dynamic> j) => DemoScenario(
        id: j['id'] as String,
        name: j['name'] as String,
        category: j['category'] as String,
        trigger: j['trigger'] as String,
        ellaResponse: j['ella_response'] as String,
        notes: j['notes'] as String?,
      );
}

Future<List<DemoScenario>> _fetchDemoScenarios() async {
  try {
    final response = await http.get(Uri.parse('https://ella-ai-care.com/api/demo/scenarios'),
        headers: {'Content-Type': 'application/json'}).timeout(const Duration(seconds: 10));
    if (response.statusCode == 200) {
      final data = jsonDecode(response.body);
      final list = data is List ? data : (data['scenarios'] as List?) ?? [];
      return list.map((s) => DemoScenario.fromJson(s as Map<String, dynamic>)).toList();
    }
  } catch (e) {
    Logger.debug('fetchDemoScenarios error: $e');
  }
  return [];
}

class EllaDemoScenariosPage extends StatefulWidget {
  const EllaDemoScenariosPage({super.key});

  @override
  State<EllaDemoScenariosPage> createState() => _EllaDemoScenariosPageState();
}

class _EllaDemoScenariosPageState extends State<EllaDemoScenariosPage> {
  List<DemoScenario> _scenarios = [];
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final scenarios = await _fetchDemoScenarios();
    if (mounted) {
      setState(() {
        _scenarios = scenarios;
        _loading = false;
      });
    }
  }

  /// Returns scenarios grouped by category, preserving insertion order.
  Map<String, List<DemoScenario>> get _grouped {
    final map = <String, List<DemoScenario>>{};
    for (final s in _scenarios) {
      (map[s.category] ??= []).add(s);
    }
    return map;
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
        title: const Text(
          'Demo Scenarios',
          style: TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
        ),
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        color: EllaColors.primary,
        child: ListView(
          padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 8),
          children: [
            const SizedBox(height: 10),
            EllaCardSurface(
              child: ListTile(
                minTileHeight: 68,
                leading: const Icon(Icons.fact_check_outlined, color: EllaColors.tealDeep),
                title: Text(context.l10n.ellaDemoAccessTitle, style: EllaTextStyles.body),
                subtitle: Text(context.l10n.ellaDemoAccessEntryBody, style: EllaTextStyles.caption),
                trailing: const Icon(Icons.chevron_right_rounded, color: EllaColors.inkSoft),
                onTap: () =>
                    Navigator.of(context).push(MaterialPageRoute(builder: (_) => const EllaAccessDemoGalleryPage())),
              ),
            ),
            if (_loading) ...[
              const SizedBox(height: 28),
              const Center(child: CircularProgressIndicator(color: EllaColors.primary)),
            ] else if (_scenarios.isEmpty) ...[
              const SizedBox(height: 28),
              const Center(
                child: Text(
                  'No voice-response scenarios available.',
                  style: TextStyle(fontSize: 18, color: EllaColors.textSecondary),
                ),
              ),
            ] else ...[
              for (final entry in _grouped.entries) ...[
                _buildSectionHeader(entry.key),
                ...entry.value.map(_buildScenarioCard),
              ],
            ],
            const SizedBox(height: 32),
          ],
        ),
      ),
    );
  }

  Widget _buildSectionHeader(String category) {
    return Padding(
      padding: const EdgeInsets.only(left: 4, top: 24, bottom: 8),
      child: Text(
        category.toUpperCase(),
        style: const TextStyle(
          fontSize: 14,
          fontWeight: FontWeight.w600,
          color: EllaColors.primary,
          letterSpacing: 1.2,
        ),
      ),
    );
  }

  Widget _buildScenarioCard(DemoScenario scenario) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: EllaColors.bgTertiary,
          borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              scenario.name,
              style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600, color: EllaColors.textPrimary),
            ),
            const SizedBox(height: 6),
            Text('Say: ${scenario.trigger}', style: const TextStyle(fontSize: 15, color: EllaColors.primary)),
            const SizedBox(height: 8),
            Text(
              scenario.ellaResponse,
              style: const TextStyle(fontSize: 16, color: EllaColors.textSecondary, height: 1.5),
            ),
            if (scenario.notes != null && scenario.notes!.isNotEmpty) ...[
              const SizedBox(height: 6),
              Text(
                scenario.notes!,
                style: const TextStyle(fontSize: 14, color: EllaColors.textTertiary, fontStyle: FontStyle.italic),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
