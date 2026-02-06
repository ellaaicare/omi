import 'package:flutter/material.dart';

import 'package:omi/pages/settings/settings_drawer.dart';

/// Full-page settings tab for Ella (replaces the OMI bottom sheet drawer).
///
/// Wraps the existing SettingsDrawer widget in a Scaffold so it can be used
/// as an IndexedStack page instead of a modal bottom sheet.
class EllaSettingsPage extends StatelessWidget {
  const EllaSettingsPage({super.key});

  @override
  Widget build(BuildContext context) {
    return const Scaffold(
      body: SettingsDrawer(mode: SettingsMode.omi),
    );
  }
}
