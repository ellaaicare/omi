import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/services/wals.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/logger.dart';

class WalFileManager {
  static const String _walFileName = 'wals.json';
  static const String _walBackupFileName = 'wals_backup.json';
  static const String _legacyPendingFilesKey = 'flash_page_pending_uploads';
  static const String _migrationCompletedPreference = 'limitless_wal_owner_quarantine_v2';
  static const String _accountsDirectoryName = 'ella_wal_accounts';
  static const String _quarantineDirectoryName = 'ella_wal_quarantine';

  static Directory? _baseDirectory;
  static WalOwner? _activeOwner;
  static Future<void>? _initialization;

  static Directory get _accountsDirectory => Directory(p.join(_baseDirectory!.path, _accountsDirectoryName));
  static Directory get _quarantineDirectory => Directory(p.join(_baseDirectory!.path, _quarantineDirectoryName));
  static Directory? get _activeDirectory =>
      _activeOwner == null ? null : Directory(p.join(_accountsDirectory.path, _activeOwner!.storageNamespace));

  static File? get _activeWalFile =>
      _activeDirectory == null ? null : File(p.join(_activeDirectory!.path, _walFileName));
  static File? get _activeWalBackupFile =>
      _activeDirectory == null ? null : File(p.join(_activeDirectory!.path, _walBackupFileName));
  static File get _quarantineWalFile => File(p.join(_quarantineDirectory.path, _walFileName));

  static Future<void> init({Directory? baseDirectory, WalOwner? activeOwner}) async {
    while (_initialization != null) {
      await _initialization;
    }
    final initialization = _initialize(baseDirectory: baseDirectory, activeOwner: activeOwner);
    _initialization = initialization;
    try {
      await initialization;
    } finally {
      if (identical(_initialization, initialization)) _initialization = null;
    }
  }

  static Future<void> _initialize({Directory? baseDirectory, WalOwner? activeOwner}) async {
    _baseDirectory = baseDirectory ??
        _baseDirectory ??
        (Platform.isMacOS ? await getApplicationSupportDirectory() : await getApplicationDocumentsDirectory());
    _activeOwner = activeOwner ?? WalOwnerAuthority.currentOwner();
    await _accountsDirectory.create(recursive: true);
    await _quarantineDirectory.create(recursive: true);
    if (_activeDirectory != null) await _activeDirectory!.create(recursive: true);
    await _quarantineLegacyRootManifest();
    await _quarantineLegacyRootAudioFiles();
  }

  @visibleForTesting
  static void resetForTesting() {
    _baseDirectory = null;
    _activeOwner = null;
    _initialization = null;
  }

  static Future<List<Wal>> loadWals({WalOwner? activeOwner}) async {
    await init(activeOwner: activeOwner);
    final active = await _readWals(_activeWalFile);
    final quarantine = await _readWals(_quarantineWalFile);
    await SharedPreferencesUtil().saveInt('ellaWalQuarantineCount', quarantine.length);
    final valid = <Wal>[];
    for (final wal in active) {
      if (_activeOwner != null && wal.owner != null && wal.owner!.matches(_activeOwner!)) {
        valid.add(wal);
      } else {
        await quarantineWal(wal, reason: 'owner_manifest_mismatch', persist: false);
        quarantine.add(wal);
      }
    }
    if (valid.length != active.length) {
      await _writeWals(_activeWalFile, _activeWalBackupFile, valid);
      await _writeWals(_quarantineWalFile, null, quarantine);
    }
    return valid;
  }

  static Future<bool> saveWals(List<Wal> wals) async {
    await init(activeOwner: _activeOwner);
    final active = <Wal>[];
    final quarantine = await _readWals(_quarantineWalFile);
    for (final wal in wals) {
      if (wal.status != WalStatus.quarantined &&
          _activeOwner != null &&
          wal.owner != null &&
          wal.owner!.matches(_activeOwner!)) {
        active.add(wal);
      } else {
        if (wal.status != WalStatus.quarantined) {
          await quarantineWal(wal,
              reason: wal.owner == null ? 'legacy_unknown_owner' : 'inactive_owner', persist: false);
        }
        quarantine.removeWhere((candidate) => candidate.id == wal.id && candidate.filePath == wal.filePath);
        quarantine.add(wal);
      }
    }
    await _writeWals(_activeWalFile, _activeWalBackupFile, active);
    await _writeWals(_quarantineWalFile, null, quarantine);
    return true;
  }

  static Future<String?> resolveWalFilePath(Wal wal) async {
    if (_baseDirectory == null) await init(activeOwner: _activeOwner);
    if (wal.filePath == null || wal.filePath!.isEmpty) return null;
    final filename = p.basename(wal.filePath!);
    if (wal.status == WalStatus.quarantined) return p.join(_quarantineDirectory.path, filename);
    if (wal.owner != null) return p.join(_accountsDirectory.path, wal.owner!.storageNamespace, filename);
    return p.join(_baseDirectory!.path, filename);
  }

  static Future<void> bindExternalWal(Wal wal, {WalOwner? owner}) async {
    await init(activeOwner: owner);
    final activeOwner = owner ?? _activeOwner;
    if (activeOwner == null) {
      await quarantineWal(wal, reason: 'capture_without_owner');
      return;
    }

    final sourcePath = wal.filePath;
    wal.owner = activeOwner;
    wal.quarantineReason = null;
    if (wal.status == WalStatus.quarantined) wal.status = WalStatus.miss;
    if (sourcePath == null || sourcePath.isEmpty) return;
    final source = File(sourcePath);
    final fallbackSource = File(p.join(_baseDirectory!.path, p.basename(sourcePath)));
    final actualSource = p.isAbsolute(sourcePath) && await source.exists()
        ? source
        : await fallbackSource.exists()
            ? fallbackSource
            : null;
    final destination = File(p.join(_accountsDirectory.path, activeOwner.storageNamespace, p.basename(sourcePath)));
    await destination.parent.create(recursive: true);
    if (actualSource != null && actualSource.path != destination.path) await actualSource.rename(destination.path);
    wal.filePath = destination.path;
  }

  static Future<void> quarantineWal(
    Wal wal, {
    required String reason,
    bool persist = true,
  }) async {
    if (_baseDirectory == null) await init(activeOwner: _activeOwner);
    final rawPath = wal.filePath;
    final rawFile = rawPath == null || rawPath.isEmpty ? null : File(rawPath);
    final previousPath = rawFile != null && p.isAbsolute(rawPath!) && await rawFile.exists()
        ? rawFile.path
        : await resolveWalFilePath(wal);
    wal.status = WalStatus.quarantined;
    wal.quarantineReason = reason;
    if (wal.filePath != null && wal.filePath!.isNotEmpty) {
      final source = File(previousPath ?? wal.filePath!);
      final destination = p.dirname(source.path) == _quarantineDirectory.path
          ? source
          : await _uniqueDestination(p.basename(wal.filePath!));
      if (await source.exists() && source.path != destination.path) {
        await destination.parent.create(recursive: true);
        await source.rename(destination.path);
      }
      wal.filePath = destination.path;
    }
    if (persist) {
      final current = await _readWals(_quarantineWalFile);
      current.removeWhere((candidate) => candidate.id == wal.id);
      current.add(wal);
      await _writeWals(_quarantineWalFile, null, current);
    }
  }

  static Future<int> quarantineUnownedFiles() async {
    await init(activeOwner: _activeOwner);
    await _quarantineLegacyRootManifest();
    return migrateLegacyLimitlessFiles(await loadWals(activeOwner: _activeOwner));
  }

  static Future<void> _quarantineLegacyRootManifest() async {
    if (_baseDirectory == null) return;
    final legacyFile = File(p.join(_baseDirectory!.path, _walFileName));
    final legacyBackup = File(p.join(_baseDirectory!.path, _walBackupFileName));
    if (!legacyFile.existsSync() && !legacyBackup.existsSync()) return;

    final quarantine = await _readWals(_quarantineWalFile);
    for (final wal in await _readWals(legacyFile)) {
      await quarantineWal(wal, reason: 'legacy_unknown_owner', persist: false);
      quarantine.removeWhere((candidate) => candidate.id == wal.id);
      quarantine.add(wal);
    }
    await _writeWals(_quarantineWalFile, null, quarantine);

    final stamp = DateTime.now().toUtc().millisecondsSinceEpoch;
    if (legacyFile.existsSync()) {
      await legacyFile.rename(p.join(_quarantineDirectory.path, 'legacy_wals_$stamp.json'));
    }
    if (legacyBackup.existsSync()) {
      await legacyBackup.rename(p.join(_quarantineDirectory.path, 'legacy_wals_backup_$stamp.json'));
    }
  }

  static Future<void> _quarantineLegacyRootAudioFiles() async {
    if (_baseDirectory == null || !_baseDirectory!.existsSync()) return;
    final quarantine = await _readWals(_quarantineWalFile);
    var changed = false;
    await for (final entity in _baseDirectory!.list(followLinks: false)) {
      if (entity is! File || !RegExp(r'^audio_.*\.bin$').hasMatch(p.basename(entity.path))) continue;
      final timestamp = RegExp(r'_(\d{10,13})\.bin$').firstMatch(p.basename(entity.path))?.group(1);
      var timerStart = DateTime.now().millisecondsSinceEpoch ~/ 1000;
      if (timestamp != null) {
        final parsed = int.tryParse(timestamp);
        if (parsed != null) timerStart = timestamp.length == 13 ? parsed ~/ 1000 : parsed;
      }
      final wal = Wal(
        timerStart: timerStart,
        codec: BleAudioCodec.opus,
        seconds: ((await entity.length()) / 8000).ceil().clamp(1, 1 << 31).toInt(),
        status: WalStatus.quarantined,
        storage: WalStorage.disk,
        filePath: entity.path,
      );
      await quarantineWal(wal, reason: 'legacy_orphan_unknown_owner', persist: false);
      quarantine.removeWhere((candidate) => candidate.filePath == wal.filePath);
      quarantine.add(wal);
      changed = true;
    }
    if (changed) await _writeWals(_quarantineWalFile, null, quarantine);
  }

  static Future<File> _uniqueDestination(String filename) async {
    var destination = File(p.join(_quarantineDirectory.path, filename));
    if (!destination.existsSync()) return destination;
    final stem = p.basenameWithoutExtension(filename);
    final extension = p.extension(filename);
    destination = File(
      p.join(_quarantineDirectory.path, '${stem}_${DateTime.now().toUtc().microsecondsSinceEpoch}$extension'),
    );
    return destination;
  }

  static Future<List<Wal>> _readWals(File? file) async {
    if (file == null || !file.existsSync()) return [];
    try {
      final content = await file.readAsString();
      if (content.isEmpty) return [];
      final jsonData = jsonDecode(content);
      if (jsonData is! Map<String, dynamic> || jsonData['wals'] is! List) return [];
      return Wal.fromJsonList(jsonData['wals'] as List);
    } catch (error) {
      Logger.debug('WalFileManager: Could not read ${file.path}: $error');
      return [];
    }
  }

  static Future<void> _writeWals(File? file, File? backup, List<Wal> wals) async {
    if (file == null) return;
    await file.parent.create(recursive: true);
    if (file.existsSync() && backup != null) {
      await file.copy(backup.path);
    }
    await file.writeAsString(jsonEncode({
      'version': 2,
      'timestamp': DateTime.now().millisecondsSinceEpoch,
      'wals': wals.map((wal) => wal.toJson()).toList(),
    }));
    if (file.path == _quarantineWalFile.path) {
      await SharedPreferencesUtil().saveInt('ellaWalQuarantineCount', wals.length);
    }
  }

  static Future<bool> migrateFromPreferences(List<Wal> prefsWals) async {
    for (final wal in prefsWals) {
      await quarantineWal(wal, reason: 'legacy_preferences_unknown_owner', persist: false);
    }
    return saveWals(prefsWals);
  }

  static Future<void> clearAll() async {
    await init(activeOwner: _activeOwner);
    if (_activeDirectory != null && _activeDirectory!.existsSync()) {
      await _activeDirectory!.delete(recursive: true);
    }
    if (_activeOwner != null) {
      final quarantine = await _readWals(_quarantineWalFile);
      final retained = <Wal>[];
      for (final wal in quarantine) {
        if (wal.owner != null && wal.owner!.matches(_activeOwner!)) {
          final path = await resolveWalFilePath(wal);
          if (path != null && File(path).existsSync()) await File(path).delete();
        } else {
          retained.add(wal);
        }
      }
      await _writeWals(_quarantineWalFile, null, retained);
    }
    Logger.debug('Cleared only the active account WAL files after confirmed account deletion');
  }

  static Future<Map<String, int>> getFileInfo() async {
    await init(activeOwner: _activeOwner);
    final main = _activeWalFile;
    final backup = _activeWalBackupFile;
    return {
      'mainFileSize': main != null && main.existsSync() ? await main.length() : 0,
      'backupFileSize': backup != null && backup.existsSync() ? await backup.length() : 0,
    };
  }

  static Future<int> getQuarantineCount() async {
    await init(activeOwner: _activeOwner);
    return (await _readWals(_quarantineWalFile)).length;
  }

  static Future<int> migrateLegacyLimitlessFiles(List<Wal> existingWals) async {
    final prefs = SharedPreferencesUtil();
    if (prefs.getBool(_migrationCompletedPreference)) return 0;
    final legacyFiles = prefs.getStringList(_legacyPendingFilesKey);
    var count = 0;
    for (final fullPath in legacyFiles) {
      final file = File(fullPath);
      if (!file.existsSync()) continue;
      final fileSize = await file.length();
      final timestampMatch = RegExp(r'_(\d{13})\.bin$').firstMatch(p.basename(fullPath));
      final wal = Wal(
        timerStart: timestampMatch == null
            ? DateTime.now().millisecondsSinceEpoch ~/ 1000
            : int.parse(timestampMatch.group(1)!) ~/ 1000,
        codec: BleAudioCodec.opus,
        seconds: (fileSize / 8000).ceil().clamp(1, 1 << 31).toInt(),
        status: WalStatus.quarantined,
        storage: WalStorage.disk,
        filePath: fullPath,
        device: 'limitless',
        deviceModel: 'Limitless',
        originalStorage: WalStorage.flashPage,
      );
      await quarantineWal(wal, reason: 'legacy_limitless_unknown_owner', persist: false);
      existingWals.add(wal);
      count++;
    }
    await saveWals(existingWals);
    await prefs.saveStringList(_legacyPendingFilesKey, []);
    await prefs.saveBool(_migrationCompletedPreference, true);
    return count;
  }

  static Future<bool> migrateInconsistentWals(List<Wal> wals) async {
    var changed = false;
    for (final wal in wals.where((wal) => wal.status != WalStatus.quarantined)) {
      if (wal.storage == WalStorage.flashPage && wal.filePath?.isNotEmpty == true) {
        wal.storage = WalStorage.disk;
        wal.originalStorage = WalStorage.flashPage;
        changed = true;
      }
      if (wal.storage == WalStorage.disk &&
          wal.originalStorage == null &&
          (wal.deviceModel?.toLowerCase().contains('limitless') == true ||
              wal.filePath?.contains('limitless') == true)) {
        wal.originalStorage = WalStorage.flashPage;
        changed = true;
      }
    }
    if (changed) await saveWals(wals);
    return changed;
  }
}
