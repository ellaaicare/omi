import 'package:flutter/material.dart';

import 'package:provider/provider.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/pages/apps/app_detail/app_detail.dart';
import 'package:omi/providers/app_provider.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/other/temp.dart';

class ExternalIntegrationsSection extends StatelessWidget {
  const ExternalIntegrationsSection({super.key});

  @override
  Widget build(BuildContext context) {
    return Consumer<AppProvider>(
      builder: (context, appProvider, child) {
        final enabledExternalApps = appProvider.apps.where((app) => app.enabled && app.worksExternally()).toList();

        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              context.l10n.externalAppAccess,
              style: const TextStyle(color: EllaColors.textPrimary, fontSize: 18, fontWeight: FontWeight.bold),
            ),
            const SizedBox(height: 8),
            Text(
              context.l10n.externalAppAccessDescription,
              style: const TextStyle(color: EllaColors.textSecondary, fontSize: 14),
            ),
            const SizedBox(height: 16),
            if (enabledExternalApps.isEmpty)
              Container(
                width: double.infinity,
                padding: const EdgeInsets.symmetric(vertical: 24.0),
                decoration: BoxDecoration(
                  color: Colors.white,
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(color: EllaColors.bgTertiary),
                ),
                child: Center(
                  child: Text(
                    context.l10n.noExternalAppsHaveAccess,
                    style: const TextStyle(color: EllaColors.textSecondary),
                  ),
                ),
              )
            else
              Container(
                decoration: BoxDecoration(
                  color: Colors.white,
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(color: EllaColors.bgTertiary),
                ),
                child: ListView.separated(
                  shrinkWrap: true,
                  physics: const NeverScrollableScrollPhysics(),
                  itemCount: enabledExternalApps.length,
                  itemBuilder: (context, index) {
                    final app = enabledExternalApps[index];
                    return ListTile(
                      leading: CircleAvatar(
                        backgroundImage: NetworkImage(app.getImageUrl()),
                      ),
                      title: Text(app.getName(), style: const TextStyle(color: EllaColors.textPrimary)),
                      trailing: const Icon(Icons.arrow_forward_ios, color: EllaColors.textTertiary, size: 16),
                      onTap: () {
                        routeToPage(context, AppDetailPage(app: app));
                      },
                    );
                  },
                  separatorBuilder: (context, index) => const Divider(
                    height: 1,
                    color: EllaColors.bgTertiary,
                  ),
                ),
              ),
          ],
        );
      },
    );
  }
}
