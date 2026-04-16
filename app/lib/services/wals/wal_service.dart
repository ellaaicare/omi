import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/services/wals/wal.dart';
import 'package:omi/services/wals/wal_interfaces.dart';
import 'package:omi/services/wals/wal_syncs.dart';
import 'package:omi/utils/logger.dart';

class WalService implements IWalService, IWalSyncListener {
  final Map<Object, IWalServiceListener> _subscriptions = {};
  WalServiceStatus _status = WalServiceStatus.init;
  WalServiceStatus get status => _status;

  late WalSyncs _syncs;
  WalSyncs get syncs => _syncs;

  WalService() {
    _syncs = WalSyncs(this);
  }

  @override
  void subscribe(IWalServiceListener subscription, Object context) {
    _subscriptions.remove(context.hashCode);
    _subscriptions.putIfAbsent(context.hashCode, () => subscription);

    subscription.onStatusChanged(_status);
  }

  @override
  void unsubscribe(Object context) {
    _subscriptions.remove(context.hashCode);
  }

  @override
  void start() {
    _syncs.start();
    _status = WalServiceStatus.ready;

    // Recover any orphaned WAL files from previous sessions
    recoverOrphanedWals();
  }

  @override
  Future stop() async {
    await _syncs.stop();

    _status = WalServiceStatus.stop;
    _onStatusChanged(_status);
    _subscriptions.clear();
  }

  /// Attempt to sync any WAL files left in "miss" status from
  /// previous sessions or interrupted syncs. Called on startup
  /// and on BLE reconnect.
  Future<void> recoverOrphanedWals() async {
    try {
      // Retry upload of any unsynced WAL files
      final result = await _syncs.phone.syncAll();
      if (result != null) {
        Logger.debug('[WAL] recoverOrphanedWals: ${result.newConversationIds.length} new, ${result.updatedConversationIds.length} updated');
      }
    } catch (e) {
      Logger.debug('[WAL] recoverOrphanedWals error: $e');
    }
  }

  void _onStatusChanged(WalServiceStatus status) {
    for (var s in _subscriptions.values) {
      s.onStatusChanged(status);
    }
  }

  @override
  WalSyncs getSyncs() {
    return _syncs;
  }

  @override
  void onWalUpdated() {
    for (var s in _subscriptions.values) {
      s.onWalUpdated();
    }
  }

  @override
  void onWalSynced(Wal wal, {ServerConversation? conversation}) {
    for (var s in _subscriptions.values) {
      s.onWalSynced(wal, conversation: conversation);
    }
  }
}
