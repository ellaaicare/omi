import 'package:omi/backend/preferences.dart';
import 'package:omi/utils/wal_file_manager.dart';

/// Removes persisted account state before Firebase can change identity.
///
/// Account-owned WAL files remain isolated on disk for recovery by the same
/// owner, but the process must release their authority before another account
/// can become current.
class EllaLogoutCachePurge {
  const EllaLogoutCachePurge({this.clearPreferences, this.releaseWalOwner});

  final Future<void> Function()? clearPreferences;
  final Future<void> Function()? releaseWalOwner;

  Future<void> purge() async {
    await (clearPreferences ?? SharedPreferencesUtil().clearAndVerifyForLogout).call();
    await (releaseWalOwner ?? WalFileManager.releaseActiveOwnerForLogout).call();
  }
}
