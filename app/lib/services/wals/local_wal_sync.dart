import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/foundation.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/services/wals/wal.dart';
import 'package:omi/services/wals/wal_interfaces.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/wal_file_manager.dart';

typedef WalUpload = Future<SyncLocalFilesResponse> Function(List<File> files, String expectedUid);

class LocalWalSyncImpl implements LocalWalSync {
  List<Wal> _wals = [];

  List<List<int>> _frames = [];
  List<bool> _frameSynced = [];
  List<WalOwner?> _frameOwners = [];

  Timer? _chunkingTimer;
  Timer? _flushingTimer;
  Future<void>? _initializationFuture;

  IWalSyncListener listener;

  int _framesPerSecond = 100;
  BleAudioCodec _codec = BleAudioCodec.opus;
  String? _deviceId;
  String? _deviceModel;
  final WalOwner? Function() _currentOwner;
  final ActiveWalAuthority? Function() _activeAuthority;
  final WalUpload _upload;

  LocalWalSyncImpl(
    this.listener, {
    WalOwner? Function()? currentOwner,
    ActiveWalAuthority? Function()? activeAuthority,
    WalUpload? upload,
  })  : _currentOwner = currentOwner ?? WalOwnerAuthority.currentOwner,
        _activeAuthority = activeAuthority ?? WalOwnerAuthority.active,
        _upload = upload ?? ((files, expectedUid) => syncLocalFiles(files, expectedAuthenticatedUid: expectedUid));

  @override
  void cancelSync() {
    // Local sync doesn't support cancellation yet
  }

  @override
  Future<void> addExternalWal(Wal wal) async {
    await _waitForInitialization();
    final authority = _activeAuthority();
    if (authority == null || !authority.isCurrent() || wal.owner == null || !wal.owner!.matches(authority.owner)) {
      await WalFileManager.quarantineWal(wal, reason: 'external_owner_provenance_unverified');
      listener.onWalUpdated();
      Logger.debug('LocalWalSync: Quarantined external WAL without exact owner authority');
      return;
    }
    await WalFileManager.bindExternalWal(wal, owner: authority.owner);
    if (!authority.isCurrent()) {
      await WalFileManager.quarantineWal(wal, reason: 'external_authority_changed_in_flight');
      listener.onWalUpdated();
      return;
    }
    final existingIndex = _wals.indexWhere((w) => w.id == wal.id);
    if (existingIndex >= 0) {
      Logger.debug('LocalWalSync: Exact-owner external WAL already exists, skipping');
      return;
    }
    _wals.add(wal);
    await _saveWalsToFile();
    listener.onWalUpdated();
    Logger.debug('LocalWalSync: Added exact-owner external WAL (${wal.seconds}s)');
  }

  @override
  void start() {
    _chunkingTimer?.cancel();
    _flushingTimer?.cancel();
    _initializationFuture = _initializeWals();
    _chunkingTimer = Timer.periodic(const Duration(seconds: chunkSizeInSeconds + newFrameSyncDelaySeconds), (t) async {
      await _chunk();
    });
    _flushingTimer =
        Timer.periodic(const Duration(seconds: flushIntervalInSeconds + newFrameSyncDelaySeconds), (t) async {
      await _flush();
    });
  }

  Future<void> _initializeWals() async {
    final owner = _currentOwner();
    await WalFileManager.init(activeOwner: owner);
    _wals = await WalFileManager.loadWals(activeOwner: owner);
    Logger.debug("wal service start: ${_wals.length}");

    // Run migrations for legacy Limitless files
    final migratedCount = await WalFileManager.migrateLegacyLimitlessFiles(_wals);
    if (migratedCount > 0) {
      // Reload WALs after migration
      _wals = await WalFileManager.loadWals();
      Logger.debug("wal service after migration: ${_wals.length}");
    }

    // Fix any inconsistent WAL states from old implementations
    await WalFileManager.migrateInconsistentWals(_wals);

    listener.onWalUpdated();
  }

  @visibleForTesting
  Future<void> initializeForTesting() {
    _initializationFuture = _initializeWals();
    return _initializationFuture!;
  }

  Future<void> _waitForInitialization() async {
    final initialization = _initializationFuture;
    if (initialization != null) await initialization;
  }

  @override
  Future stop() async {
    _chunkingTimer?.cancel();
    _flushingTimer?.cancel();

    await _drainForStop();
    await _flush();

    _frames = [];
    _frameSynced = [];
    _frameOwners = [];
  }

  Future<void> _drainForStop() async {
    await _waitForInitialization();
    if (_frames.isEmpty) return;

    final device = _deviceId ?? 'omi';
    var groupStart = 0;
    var timerStart = DateTime.now().millisecondsSinceEpoch ~/ 1000 - (_frames.length / _framesPerSecond).ceil();
    while (groupStart < _frames.length) {
      final owner = _frameOwners[groupStart];
      var groupEnd = groupStart + 1;
      while (groupEnd < _frames.length && _ownersMatch(_frameOwners[groupEnd], owner)) {
        groupEnd++;
      }

      while (
          _wals.any((wal) => wal.timerStart == timerStart && wal.device == device && _ownersMatch(wal.owner, owner))) {
        timerStart--;
      }
      final frames = _frames.sublist(groupStart, groupEnd).map(List<int>.from).toList();
      var syncedOffset = 0;
      for (var index = groupStart; index < groupEnd && _frameSynced[index]; index++) {
        syncedOffset++;
      }
      final frameCount = groupEnd - groupStart;
      _wals.add(
        Wal(
          codec: _codec,
          timerStart: timerStart,
          data: frames,
          storage: WalStorage.mem,
          status: WalStatus.quarantined,
          device: device,
          deviceModel: _deviceModel ?? 'Omi',
          seconds: (frameCount / _framesPerSecond).ceil(),
          totalFrames: frameCount,
          syncedFrameOffset: syncedOffset,
          owner: owner,
          quarantineReason: owner == null ? 'capture_without_owner' : 'account_transition_final_drain',
        ),
      );
      listener.onWalUpdated();
      timerStart++;
      groupStart = groupEnd;
    }

    _frames.clear();
    _frameSynced.clear();
    _frameOwners.clear();
  }

  bool _ownersMatch(WalOwner? left, WalOwner? right) {
    if (left == null || right == null) return left == null && right == null;
    return left.matches(right);
  }

  @override
  Future onAudioCodecChanged(BleAudioCodec codec) async {
    if (codec.getFramesPerSecond() == _framesPerSecond && codec == _codec) {
      return;
    }

    await _chunk();
    await _flush();
    _frames = [];
    _frameSynced = [];
    _frameOwners = [];

    _framesPerSecond = codec.getFramesPerSecond();
    _codec = codec;
  }

  @override
  void setDeviceInfo(String? deviceId, String? deviceModel) {
    _deviceId = deviceId;
    _deviceModel = deviceModel;
  }

  Future _chunk() async {
    await _waitForInitialization();
    if (_frames.isEmpty) {
      Logger.debug("Frames are empty");
      return;
    }

    var lossesThreshold = 10 * _framesPerSecond;
    var timerEnd = DateTime.now().millisecondsSinceEpoch ~/ 1000 - newFrameSyncDelaySeconds;
    var pivot = _frames.length - newFrameSyncDelaySeconds * _framesPerSecond;
    if (pivot <= 0) {
      return;
    }

    var high = pivot;
    var low = 0;
    var chunk = _frames.sublist(low, high);
    var timerStart = timerEnd - (high - low) ~/ _framesPerSecond;
    var chunkFrameCount = high - low;

    final authority = _activeAuthority();
    final capturedOwners = _frameOwners.sublist(low, high);
    final firstOwner = capturedOwners.first;
    final oneExactOwner =
        firstOwner != null && capturedOwners.every((candidate) => candidate?.matches(firstOwner) == true);
    final owner = oneExactOwner && authority != null && authority.isCurrent() && firstOwner.matches(authority.owner)
        ? firstOwner
        : null;

    // Unknown, mixed, or stale-owner audio is evidence that must be retained in
    // quarantine even when it is shorter than the normal loss threshold.
    bool shouldStored = SharedPreferencesUtil().unlimitedLocalStorageEnabled || owner == null;
    if (!shouldStored) {
      bool synced = true;
      var losses = 0;
      for (var i = low; i < high; i++) {
        if (!_frameSynced[i]) {
          losses++;
          if (losses >= lossesThreshold) {
            synced = false;
            break;
          }
        }
      }

      shouldStored = (synced == false);
    }

    if (shouldStored) {
      int syncedOffset = 0;
      for (var i = low; i < high; i++) {
        if (_frameSynced[i]) {
          syncedOffset++;
        } else {
          break;
        }
      }
      Logger.debug("$low - $high - $syncedOffset - $chunkFrameCount - $_framesPerSecond");

      Wal wal;
      var walIdx = _wals.indexWhere((w) =>
          w.timerStart == timerStart &&
          w.device == (_deviceId ?? "omi") &&
          w.codec == _codec &&
          w.owner != null &&
          owner != null &&
          w.owner!.matches(owner));
      if (walIdx < 0) {
        wal = Wal(
          codec: _codec,
          timerStart: timerStart,
          data: chunk,
          storage: WalStorage.mem,
          status: owner == null
              ? WalStatus.quarantined
              : syncedOffset == chunkFrameCount
                  ? WalStatus.synced
                  : WalStatus.miss,
          device: _deviceId ?? "omi",
          deviceModel: _deviceModel ?? "Omi",
          seconds: chunkFrameCount ~/ _framesPerSecond,
          totalFrames: chunkFrameCount,
          syncedFrameOffset: syncedOffset,
          owner: owner,
          quarantineReason: owner == null ? 'capture_without_owner' : null,
        );
        _wals.add(wal);
      } else {
        wal = _wals[walIdx];
        wal.data.addAll(chunk);
        wal.storage = WalStorage.mem;
        wal.totalFrames = chunkFrameCount;
        wal.syncedFrameOffset = syncedOffset;
        wal.status = syncedOffset == chunkFrameCount ? WalStatus.synced : WalStatus.miss;
        _wals[walIdx] = wal;
      }

      if (wal.status == WalStatus.synced) {
        listener.onWalSynced(wal);
      }
      listener.onWalUpdated();
    }

    Logger.debug("_chunk wals ${_wals.length}");

    _frames.removeRange(0, pivot);
    _frameSynced.removeRange(0, pivot);
    _frameOwners.removeRange(0, pivot);
  }

  Future _flush() async {
    await _waitForInitialization();
    Logger.debug("_flushing");
    for (var i = 0; i < _wals.length; i++) {
      final wal = _wals[i];

      if (wal.storage == WalStorage.mem) {
        wal.filePath = wal.getFileName();
        String? filePath = await WalFileManager.resolveWalFilePath(wal);
        if (filePath == null) {
          throw Exception('Flushing to storage failed. Cannot get file path.');
        }

        List<int> data = [];
        for (int i = 0; i < wal.data.length; i++) {
          var frame = wal.data[i].sublist(3);

          final byteFrame = ByteData(frame.length);
          for (int i = 0; i < frame.length; i++) {
            byteFrame.setUint8(i, frame[i]);
          }
          data.addAll(Uint32List.fromList([frame.length]).buffer.asUint8List());
          data.addAll(byteFrame.buffer.asUint8List());
        }
        final file = File(filePath);
        await file.parent.create(recursive: true);
        await file.writeAsBytes(data);
        wal.filePath = file.path;
        wal.storage = WalStorage.disk;

        Logger.debug('LocalWalSync: Flushed one WAL to account-isolated storage');

        _wals[i] = wal;
      }
    }

    await _saveWalsToFile();
  }

  Future<void> _saveWalsToFile() async {
    Logger.debug('Saving WALs to file');
    await WalFileManager.saveWals(_wals);
  }

  Future<bool> _deleteWal(Wal wal) async {
    if (wal.filePath != null && wal.filePath!.isNotEmpty) {
      try {
        final fullPath = await WalFileManager.resolveWalFilePath(wal);
        if (fullPath != null) {
          final file = File(fullPath);
          if (file.existsSync()) {
            await file.delete();
          }
        }
      } catch (e) {
        Logger.debug('LocalWalSync: Account-isolated WAL deletion failed (${e.runtimeType})');
        return false;
      }
    }

    _wals.removeWhere((w) => w.id == wal.id);
    return true;
  }

  @override
  Future deleteWal(Wal wal) async {
    await _deleteWal(wal);
    listener.onWalUpdated();
  }

  @override
  Future<List<Wal>> getMissingWals() async {
    return _wals.where((w) => w.status == WalStatus.miss).toList();
  }

  @override
  Future<List<Wal>> getAllWals() async {
    return List.from(_wals);
  }

  @override
  Future<void> deleteAllSyncedWals() async {
    final syncedWals = _wals.where((w) => w.status == WalStatus.synced).toList();
    for (final wal in syncedWals) {
      await _deleteWal(wal);
    }
    await _saveWalsToFile();
    listener.onWalUpdated();
  }

  @override
  void onByteStream(List<int> value, {required WalOwner? ownerAtCapture}) {
    _frames.add(value);
    _frameSynced.add(false);
    _frameOwners.add(ownerAtCapture);
  }

  @override
  void onBytesSync(List<int> value) {
    for (int i = _frames.length - 1; i >= 0; i--) {
      if (_frames[i].length >= 3 &&
          _frames[i][0] == value[0] &&
          _frames[i][1] == value[1] &&
          _frames[i][2] == value[2]) {
        _frameSynced[i] = true;
        break;
      }
    }
  }

  @override
  Future<SyncLocalFilesResponse?> syncAll({
    IWalSyncProgressListener? progress,
    IWifiConnectionListener? connectionListener,
  }) async {
    await _flush();
    final authority = _activeAuthority();
    final pending = _wals.where((w) => w.status == WalStatus.miss && w.storage == WalStorage.disk).toList();
    for (final wal in pending) {
      if (authority == null || wal.owner == null || !wal.owner!.matches(authority.owner)) {
        await WalFileManager.quarantineWal(wal, reason: 'upload_owner_mismatch', persist: false);
      }
    }
    await _saveWalsToFile();

    final wals = pending.where((wal) => wal.status == WalStatus.miss).toList();
    if (wals.isEmpty) {
      Logger.debug("All synced!");
      return null;
    }
    if (authority == null || !authority.isCurrent()) {
      await _quarantineBatch(wals, 'upload_authority_unavailable');
      return null;
    }

    final resp = SyncLocalFilesResponse(newConversationIds: [], updatedConversationIds: []);

    const steps = 3;
    for (var i = wals.length - 1; i >= 0; i -= steps) {
      final right = i;
      var left = right - steps;
      if (left < 0) {
        left = 0;
      }

      final files = <File>[];
      final batch = <Wal>[];
      for (var j = left; j <= right; j++) {
        final wal = wals[j];
        Logger.debug('LocalWalSync: Preparing one pending WAL');
        if (wal.filePath == null) {
          Logger.debug('LocalWalSync: Pending WAL has no file reference');
          wal.status = WalStatus.corrupted;
          continue;
        }

        final fullPath = await WalFileManager.resolveWalFilePath(wal);
        try {
          if (fullPath == null) {
            Logger.debug('LocalWalSync: Could not resolve isolated WAL file');
            wal.status = WalStatus.corrupted;
            continue;
          }

          final file = File(fullPath);
          if (!file.existsSync()) {
            Logger.debug('LocalWalSync: Isolated WAL file does not exist');
            wal.status = WalStatus.corrupted;
            continue;
          }
          files.add(file);
          batch.add(wal);
          wal.isSyncing = true;
        } catch (e) {
          wal.status = WalStatus.corrupted;
          Logger.debug('LocalWalSync: Account-isolated WAL read failed (${e.runtimeType})');
        }
      }

      if (files.isEmpty) {
        Logger.debug("Files are empty");
        continue;
      }

      progress?.onWalSyncedProgress((left).toDouble() / wals.length);

      listener.onWalUpdated();
      try {
        if (!authority.isCurrent()) {
          await _quarantineBatch(batch, 'upload_authority_changed_before_egress');
          continue;
        }
        final partialRes = await _upload(files, authority.owner.uid);
        if (!authority.isCurrent()) {
          await _quarantineBatch(batch, 'upload_authority_changed_in_flight');
          continue;
        }

        resp.newConversationIds
            .addAll(partialRes.newConversationIds.where((id) => !resp.newConversationIds.contains(id)));
        resp.updatedConversationIds.addAll(partialRes.updatedConversationIds
            .where((id) => !resp.updatedConversationIds.contains(id) && !resp.newConversationIds.contains(id)));

        for (final wal in batch) {
          wal.status = WalStatus.synced;
          _clearSyncState(wal);
          listener.onWalSynced(wal);
        }
      } catch (e) {
        Logger.debug('LocalWalSync: Batch failed (${e.runtimeType}); continuing');
        for (final wal in batch) {
          _clearSyncState(wal);
        }
        continue;
      }

      await _saveWalsToFile();
      listener.onWalUpdated();
    }

    progress?.onWalSyncedProgress(1.0);
    return resp;
  }

  @override
  Future<SyncLocalFilesResponse?> syncWal({
    required Wal wal,
    IWalSyncProgressListener? progress,
    IWifiConnectionListener? connectionListener,
  }) async {
    await _flush();
    final authority = _activeAuthority();
    if (authority == null || wal.owner == null || !wal.owner!.matches(authority.owner)) {
      await _quarantineBatch([wal], 'upload_owner_mismatch');
      return null;
    }
    final fullPath = await WalFileManager.resolveWalFilePath(wal);
    if (fullPath == null || !File(fullPath).existsSync()) {
      wal.status = WalStatus.corrupted;
      await _saveWalsToFile();
      return null;
    }
    if (!authority.isCurrent()) {
      await _quarantineBatch([wal], 'upload_authority_changed_before_egress');
      return null;
    }

    wal.isSyncing = true;
    listener.onWalUpdated();
    try {
      final response = await _upload([File(fullPath)], authority.owner.uid);
      if (!authority.isCurrent()) {
        await _quarantineBatch([wal], 'upload_authority_changed_in_flight');
        return null;
      }
      wal.status = WalStatus.synced;
      _clearSyncState(wal);
      listener.onWalSynced(wal);
      await _saveWalsToFile();
      listener.onWalUpdated();
      progress?.onWalSyncedProgress(1.0);
      return response;
    } catch (error) {
      _clearSyncState(wal);
      rethrow;
    }
  }

  Future<void> _quarantineBatch(List<Wal> wals, String reason) async {
    for (final wal in wals) {
      _clearSyncState(wal);
      await WalFileManager.quarantineWal(wal, reason: reason, persist: false);
    }
    await _saveWalsToFile();
    listener.onWalUpdated();
  }

  void _clearSyncState(Wal wal) {
    wal.isSyncing = false;
    wal.syncStartedAt = null;
    wal.syncEtaSeconds = null;
  }
}
