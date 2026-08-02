import 'dart:async';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/guardian_mode_service.dart';
import 'package:omi/ella/services/v2v_client.dart';
import 'package:omi/services/services.dart';
import 'package:omi/utils/wal_file_manager.dart';

class EllaAccountIsolationService {
  const EllaAccountIsolationService({
    this.stopCapture,
    this.stopV2v,
    this.stopGuardian,
    this.stopServices,
    this.quarantineLegacy,
  });

  final FutureOr<void> Function()? stopCapture;
  final FutureOr<void> Function()? stopV2v;
  final FutureOr<void> Function()? stopGuardian;
  final FutureOr<void> Function()? stopServices;
  final FutureOr<void> Function()? quarantineLegacy;

  Future<void> stopForAccountTransition() async {
    await stopCapture?.call();
    if (stopV2v != null) {
      await stopV2v!.call();
    } else {
      await V2VClient.disconnectActiveForAccountTransition();
    }
    if (stopGuardian != null) {
      await stopGuardian!.call();
    } else {
      await GuardianModeService().stopForAccountTransition();
    }
    if (stopServices != null) {
      await stopServices!.call();
    } else if (ServiceManager.isInitialized) {
      await ServiceManager.instance().suspendForAccountTransition();
    }
    if (quarantineLegacy != null) {
      await quarantineLegacy!.call();
    } else {
      await WalFileManager.quarantineUnownedFiles();
    }
  }

  Future<void> prepareProvisioningAccount(String newUid, {SharedPreferencesUtil? preferences}) async {
    final prefs = preferences ?? SharedPreferencesUtil();
    final previousUid = prefs.getString('ellaProvisioningAccountUid');
    if (previousUid.isNotEmpty && previousUid != newUid) {
      await stopForAccountTransition();
    } else {
      // First-run legacy files are quarantined by WAL initialization. Cache
      // values can be isolated here without requiring platform file plugins.
      await prefs.quarantineLegacyAccountCaches();
    }
    await prefs.prepareEllaProvisioningAccount(newUid);
  }

  Future<void> resumeAfterVerifiedProvisioning() async {
    if (ServiceManager.isInitialized) await ServiceManager.instance().start();
  }
}
