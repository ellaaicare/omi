import 'dart:async';
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
  static const String _ownerRecoveryFileName = 'owner_recovery.json';
  static const String _legacyPendingFilesKey = 'flash_page_pending_uploads';
  static const String _migrationCompletedPreference = 'limitless_wal_owner_quarantine_v2';
  static const String _accountsDirectoryName = 'ella_wal_accounts';
  static const String _quarantineDirectoryName = 'ella_wal_quarantine';

  static Directory? _baseDirectory;
  static WalOwner? _activeOwner;
  static Future<void>? _initialization;
  static Future<void> _exclusiveOperation = Future<void>.value();
  static final Object _exclusiveOperationZoneKey = Object();

  @visibleForTesting
  static Future<void> Function()? rotationBeforeCommitForTesting;

  @visibleForTesting
  static Future<void> Function()? rotationAfterActiveManifestWriteForTesting;

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
    _exclusiveOperation = Future<void>.value();
    rotationBeforeCommitForTesting = null;
    rotationAfterActiveManifestWriteForTesting = null;
  }

  static Future<T> runExclusive<T>(Future<T> Function() operation) {
    if (Zone.current[_exclusiveOperationZoneKey] == true) return operation();

    final result = _exclusiveOperation.then<T>(
      (_) => runZoned<Future<T>>(
        operation,
        zoneValues: {_exclusiveOperationZoneKey: true},
      ),
    );
    _exclusiveOperation = result.then<void>(
      (_) {},
      onError: (Object _, StackTrace __) {},
    );
    return result;
  }

  static Future<bool> rotateActiveSessionOwner(
    List<Wal> wals, {
    required WalOwner previousOwner,
    required ActiveWalAuthority capturedAuthority,
    required ActiveWalAuthority targetAuthority,
    required ActiveWalAuthority? Function() readCurrentAuthority,
  }) =>
      runExclusive(
        () => _rotateActiveSessionOwner(
          wals,
          previousOwner: previousOwner,
          capturedAuthority: capturedAuthority,
          targetAuthority: targetAuthority,
          readCurrentAuthority: readCurrentAuthority,
        ),
      );

  static Future<bool> _rotateActiveSessionOwner(
    List<Wal> wals, {
    required WalOwner previousOwner,
    required ActiveWalAuthority capturedAuthority,
    required ActiveWalAuthority targetAuthority,
    required ActiveWalAuthority? Function() readCurrentAuthority,
  }) async {
    await init(activeOwner: _activeOwner);
    final capturedOwner = capturedAuthority.owner;
    final targetOwner = targetAuthority.owner;
    final previousActiveOwner = _activeOwner;
    bool targetIsExactCurrent() {
      final current = readCurrentAuthority();
      return targetAuthority.isCurrent() &&
          current != null &&
          current.isCurrent() &&
          current.owner.matches(targetOwner);
    }

    if (previousActiveOwner == null ||
        !previousOwner.hasValidAuthorityIdentity ||
        !capturedOwner.hasValidAuthorityIdentity ||
        !targetOwner.hasValidAuthorityIdentity ||
        !previousActiveOwner.matches(previousOwner) ||
        previousOwner.uid != capturedOwner.uid ||
        capturedOwner.uid != targetOwner.uid ||
        !capturedAuthority.isCurrent() ||
        !targetIsExactCurrent()) {
      return false;
    }

    final previousActiveDirectory = _activeDirectory!;
    final nextActiveDirectory = Directory(p.join(_accountsDirectory.path, targetOwner.storageNamespace));
    if (previousActiveDirectory.path != nextActiveDirectory.path &&
        (File(p.join(nextActiveDirectory.path, _walFileName)).existsSync() ||
            File(p.join(nextActiveDirectory.path, _walBackupFileName)).existsSync())) {
      return false;
    }

    final rotations = <_WalOwnerRotation>[];
    for (final wal in wals) {
      if (wal.status == WalStatus.quarantined) continue;
      if (wal.owner?.matches(previousOwner) != true) return false;
      File? source;
      File? destination;
      var sourceMissing = false;
      if (wal.storage == WalStorage.disk && wal.filePath != null && wal.filePath!.isNotEmpty) {
        final sourcePath = await resolveWalFilePath(wal);
        if (sourcePath == null) return false;
        source = File(sourcePath);
        if (!await source.exists()) {
          source = null;
          sourceMissing = true;
        } else {
          destination = File(p.join(nextActiveDirectory.path, p.basename(sourcePath)));
          if (source.path != destination.path && await destination.exists()) return false;
        }
      }
      rotations.add(
        _WalOwnerRotation(
          wal: wal,
          previousOwner: wal.owner!,
          previousPath: wal.filePath,
          previousStatus: wal.status,
          source: source,
          destination: destination,
          sourceMissing: sourceMissing,
        ),
      );
    }

    final copiedDestinations = <File>[];
    final manifestSnapshots = <_FileSnapshot>[];
    try {
      manifestSnapshots.addAll([
        await _FileSnapshot.capture(File(p.join(nextActiveDirectory.path, _walFileName))),
        await _FileSnapshot.capture(File(p.join(nextActiveDirectory.path, _walBackupFileName))),
        await _FileSnapshot.capture(_quarantineWalFile),
      ]);
      await nextActiveDirectory.create(recursive: true);
      for (final rotation in rotations) {
        final source = rotation.source;
        final destination = rotation.destination;
        if (source != null && destination != null && source.path != destination.path) {
          await source.copy(destination.path);
          copiedDestinations.add(destination);
        }
        rotation.wal.owner = targetOwner;
        if (destination != null) rotation.wal.filePath = destination.path;
        if (rotation.sourceMissing) {
          rotation.wal.status = WalStatus.corrupted;
          Logger.debug('WalFileManager: Marked missing WAL payload corrupted during owner rotation');
        }
      }
      await rotationBeforeCommitForTesting?.call();
      if (!capturedAuthority.isCurrent() || !targetIsExactCurrent()) {
        throw StateError('WAL owner authority changed during rotation');
      }

      _activeOwner = targetOwner;
      await _saveWals(
        wals,
        afterActiveManifestWrite: rotationAfterActiveManifestWriteForTesting,
      );
      if (!capturedAuthority.isCurrent() || !targetIsExactCurrent()) {
        throw StateError('WAL owner authority changed while committing rotation');
      }
    } catch (error) {
      _activeOwner = previousActiveOwner;
      for (final rotation in rotations) {
        rotation.wal.owner = rotation.previousOwner;
        rotation.wal.filePath = rotation.previousPath;
        rotation.wal.status = rotation.previousStatus;
      }
      var manifestsRestored = true;
      for (final snapshot in manifestSnapshots.reversed) {
        try {
          await snapshot.restore();
        } catch (restoreError) {
          manifestsRestored = false;
          Logger.debug('WalFileManager: Could not restore WAL manifest (${restoreError.runtimeType})');
        }
      }
      if (manifestsRestored) {
        for (final destination in copiedDestinations) {
          try {
            if (await destination.exists()) await destination.delete();
          } catch (_) {
            // The original remains authoritative; later isolation cleanup can remove the copy.
          }
        }
        try {
          await SharedPreferencesUtil().saveInt('ellaWalQuarantineCount', (await _readWals(_quarantineWalFile)).length);
        } catch (_) {
          // The restored manifest remains authoritative; the diagnostic count can refresh later.
        }
      }
      Logger.debug('WalFileManager: Active WAL owner rotation failed (${error.runtimeType})');
      return false;
    }

    for (final rotation in rotations) {
      final source = rotation.source;
      final destination = rotation.destination;
      if (source != null && destination != null && source.path != destination.path) {
        try {
          if (await source.exists()) await source.delete();
        } catch (error) {
          Logger.debug('WalFileManager: Could not remove superseded WAL copy (${error.runtimeType})');
        }
      }
    }
    if (previousActiveDirectory.path != nextActiveDirectory.path) {
      for (final filename in [_walFileName, _walBackupFileName]) {
        try {
          final staleManifest = File(p.join(previousActiveDirectory.path, filename));
          if (await staleManifest.exists()) await staleManifest.delete();
        } catch (error) {
          Logger.debug('WalFileManager: Could not remove superseded WAL manifest (${error.runtimeType})');
        }
      }
    }
    Logger.debug('WalFileManager: Rotated active same-account WAL owner');
    return true;
  }

  static Future<bool> authorizeInterruptedSameAccountRecovery({
    required WalOwner sourceOwner,
    required WalOwner targetOwner,
    required ActiveWalAuthority capturedAuthority,
  }) =>
      runExclusive(
        () => _authorizeInterruptedSameAccountRecovery(
          sourceOwner: sourceOwner,
          targetOwner: targetOwner,
          capturedAuthority: capturedAuthority,
        ),
      );

  static Future<bool> _authorizeInterruptedSameAccountRecovery({
    required WalOwner sourceOwner,
    required WalOwner targetOwner,
    required ActiveWalAuthority capturedAuthority,
  }) async {
    await init(activeOwner: _activeOwner);
    if (_activeOwner?.matches(sourceOwner) != true ||
        !sourceOwner.hasValidAuthorityIdentity ||
        !targetOwner.hasValidAuthorityIdentity ||
        sourceOwner.uid != targetOwner.uid ||
        sourceOwner.durablyMatches(targetOwner) ||
        !capturedAuthority.owner.matches(sourceOwner) ||
        !capturedAuthority.isCurrent()) {
      return false;
    }

    final bridgeFile = File(p.join(_accountsDirectory.path, sourceOwner.storageNamespace, _ownerRecoveryFileName));
    await bridgeFile.parent.create(recursive: true);
    await bridgeFile.writeAsString(
      jsonEncode({
        'version': 1,
        'source_authority_fingerprint': sourceOwner.authorityFingerprint,
        'target_owner': targetOwner.toJson(),
      }),
      flush: true,
    );
    if (capturedAuthority.isCurrent()) return true;
    if (await bridgeFile.exists()) await bridgeFile.delete();
    return false;
  }

  static Future<int> recoverInterruptedSameAccountWals({
    required ActiveWalAuthority targetAuthority,
    required ActiveWalAuthority? Function() readCurrentAuthority,
  }) =>
      runExclusive(
        () => _recoverInterruptedSameAccountWals(
          targetAuthority: targetAuthority,
          readCurrentAuthority: readCurrentAuthority,
        ),
      );

  static Future<int> _recoverInterruptedSameAccountWals({
    required ActiveWalAuthority targetAuthority,
    required ActiveWalAuthority? Function() readCurrentAuthority,
  }) async {
    await init(activeOwner: _activeOwner);
    final targetOwner = targetAuthority.owner;
    final activeOwner = _activeOwner;
    bool targetIsExactCurrent() {
      final current = readCurrentAuthority();
      return targetAuthority.isCurrent() &&
          current != null &&
          current.isCurrent() &&
          current.owner.matches(targetOwner);
    }

    if (activeOwner == null ||
        !activeOwner.matches(targetOwner) ||
        !targetOwner.hasValidAuthorityIdentity ||
        !targetIsExactCurrent()) {
      return 0;
    }

    final targetDirectory = _activeDirectory!;
    final sourceDirectories = (await _accountsDirectory
            .list(followLinks: false)
            .where((entry) => entry is Directory)
            .toList())
        .cast<Directory>()
      ..sort((left, right) => left.path.compareTo(right.path));
    var recoveredCount = 0;
    for (final sourceDirectory in sourceDirectories) {
      if (!targetIsExactCurrent()) continue;
      final sharesTargetDirectory = sourceDirectory.path == targetDirectory.path;
      final sourceManifest = File(p.join(sourceDirectory.path, _walFileName));
      final sourceBackup = File(p.join(sourceDirectory.path, _walBackupFileName));
      final recoveryFile = File(p.join(sourceDirectory.path, _ownerRecoveryFileName));
      final recoveryBridge = await _readRecoveryBridge(recoveryFile);
      if (recoveryBridge == null ||
          !recoveryBridge.targetOwner.sharesDurableConsentEpoch(targetOwner) ||
          targetOwner.bindingRevision < recoveryBridge.targetOwner.bindingRevision) {
        continue;
      }
      final sourceWals = await _readWals(sourceManifest);
      if (sourceWals.isEmpty) continue;

      final sourceOwner = sourceWals.first.owner;
      if (sourceOwner == null ||
          !sourceOwner.hasValidAuthorityIdentity ||
          sourceOwner.storageNamespace != p.basename(sourceDirectory.path) ||
          sourceOwner.uid != targetOwner.uid ||
          sourceOwner.authorityFingerprint != recoveryBridge.sourceAuthorityFingerprint ||
          sourceWals.any((wal) => wal.status == WalStatus.quarantined || wal.owner?.matches(sourceOwner) != true)) {
        continue;
      }

      final targetWals = sharesTargetDirectory ? <Wal>[] : await _readWals(_activeWalFile);
      if (targetWals
          .any((wal) => wal.status == WalStatus.quarantined || wal.owner?.durablyMatches(targetOwner) != true)) {
        continue;
      }
      for (final wal in targetWals) {
        wal.owner = targetOwner;
      }
      final targetKeys = targetWals.map((wal) => '${wal.device}\n${wal.timerStart}').toSet();
      if (sourceWals.any((wal) => targetKeys.contains('${wal.device}\n${wal.timerStart}'))) continue;

      final manifestSnapshots = <_FileSnapshot>[
        await _FileSnapshot.capture(_activeWalFile!),
        await _FileSnapshot.capture(_activeWalBackupFile!),
      ];
      final rotations = <_WalOwnerRotation>[];
      final copiedDestinations = <File>[];
      try {
        for (final wal in sourceWals) {
          File? source;
          File? destination;
          var sourceMissing = false;
          if (wal.storage == WalStorage.disk && wal.filePath?.isNotEmpty == true) {
            source = File(p.join(sourceDirectory.path, p.basename(wal.filePath!)));
            if (!await source.exists()) {
              source = null;
              sourceMissing = true;
            } else {
              destination = sharesTargetDirectory
                  ? source
                  : await _uniqueAccountDestination(targetDirectory, p.basename(wal.filePath!));
              if (source.path != destination.path) {
                await source.copy(destination.path);
                copiedDestinations.add(destination);
              }
            }
          }
          rotations.add(
            _WalOwnerRotation(
              wal: wal,
              previousOwner: wal.owner!,
              previousPath: wal.filePath,
              previousStatus: wal.status,
              source: source,
              destination: destination,
              sourceMissing: sourceMissing,
            ),
          );
          wal.owner = targetOwner;
          if (destination != null) wal.filePath = destination.path;
          if (sourceMissing) wal.status = WalStatus.corrupted;
        }
        if (!targetIsExactCurrent()) throw StateError('WAL recovery authority changed before commit');
        await _writeWals(_activeWalFile, _activeWalBackupFile, [...targetWals, ...sourceWals]);
        if (!targetIsExactCurrent()) throw StateError('WAL recovery authority changed during commit');
      } catch (error) {
        for (final rotation in rotations) {
          rotation.wal.owner = rotation.previousOwner;
          rotation.wal.filePath = rotation.previousPath;
          rotation.wal.status = rotation.previousStatus;
        }
        for (final snapshot in manifestSnapshots.reversed) {
          try {
            await snapshot.restore();
          } catch (_) {
            // The source manifest remains authoritative and can be retried later.
          }
        }
        for (final destination in copiedDestinations) {
          try {
            if (await destination.exists()) await destination.delete();
          } catch (_) {
            // A later isolated-storage cleanup can remove an unreferenced copy.
          }
        }
        Logger.debug('WalFileManager: Interrupted WAL recovery failed (${error.runtimeType})');
        continue;
      }

      for (final rotation in rotations) {
        try {
          if (rotation.source != null &&
              rotation.destination != null &&
              rotation.source!.path != rotation.destination!.path &&
              await rotation.source!.exists()) {
            await rotation.source!.delete();
          }
        } catch (_) {
          // The committed target copy is authoritative.
        }
      }
      final supersededFiles = sharesTargetDirectory ? [recoveryFile] : [sourceManifest, sourceBackup, recoveryFile];
      for (final manifest in supersededFiles) {
        try {
          if (await manifest.exists()) await manifest.delete();
        } catch (_) {
          // Duplicate source metadata is ignored if the target already contains the WAL key.
        }
      }
      recoveredCount += sourceWals.length;
      Logger.debug('WalFileManager: Recovered interrupted same-account WALs');
    }
    return recoveredCount;
  }

  static Future<_WalOwnerRecoveryBridge?> _readRecoveryBridge(File file) async {
    if (!await file.exists()) return null;
    try {
      final payload = jsonDecode(await file.readAsString());
      if (payload is! Map<String, dynamic> || payload['version'] != 1) return null;
      final sourceFingerprint = payload['source_authority_fingerprint'];
      final targetOwnerJson = payload['target_owner'];
      if (sourceFingerprint is! String || targetOwnerJson is! Map<String, dynamic>) return null;
      final targetOwner = WalOwner.fromJson(targetOwnerJson);
      if (!targetOwner.hasValidAuthorityIdentity || targetOwner.authorityFingerprint.isEmpty) return null;
      return _WalOwnerRecoveryBridge(
        sourceAuthorityFingerprint: sourceFingerprint,
        targetOwner: targetOwner,
      );
    } catch (error) {
      Logger.debug('WalFileManager: Could not read WAL owner recovery bridge (${error.runtimeType})');
      return null;
    }
  }

  static Future<List<Wal>> loadWals({WalOwner? activeOwner}) async {
    await init(activeOwner: activeOwner);
    final active = await _readWals(_activeWalFile);
    final quarantine = await _readWals(_quarantineWalFile);
    await SharedPreferencesUtil().saveInt('ellaWalQuarantineCount', quarantine.length);
    final valid = <Wal>[];
    var reboundDurableOwner = false;
    for (final wal in active) {
      if (_activeOwner != null && wal.owner?.durablyMatches(_activeOwner!) == true) {
        if (wal.owner?.matches(_activeOwner!) != true) {
          wal.owner = _activeOwner;
          reboundDurableOwner = true;
        }
        valid.add(wal);
      } else {
        await quarantineWal(wal, reason: 'owner_manifest_mismatch', persist: false);
        quarantine.add(wal);
      }
    }
    if (valid.length != active.length || reboundDurableOwner) {
      await _writeWals(_activeWalFile, _activeWalBackupFile, valid);
      if (valid.length != active.length) await _writeWals(_quarantineWalFile, null, quarantine);
    }
    return valid;
  }

  static Future<bool> saveWals(List<Wal> wals) async {
    return runExclusive(() => _saveWals(wals));
  }

  static Future<bool> _saveWals(
    List<Wal> wals, {
    Future<void> Function()? afterActiveManifestWrite,
  }) async {
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
    await afterActiveManifestWrite?.call();
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
    if (activeOwner == null || wal.owner == null || !wal.owner!.matches(activeOwner)) {
      await quarantineWal(wal, reason: 'external_owner_provenance_unverified');
      return;
    }

    final sourcePath = wal.filePath;
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
      current.removeWhere((candidate) => candidate.id == wal.id && candidate.filePath == wal.filePath);
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

  static Future<File> _uniqueAccountDestination(Directory directory, String filename) async {
    var destination = File(p.join(directory.path, filename));
    if (!await destination.exists()) return destination;
    final stem = p.basenameWithoutExtension(filename);
    final extension = p.extension(filename);
    var suffix = 1;
    do {
      destination = File(p.join(directory.path, '${stem}_recovered_$suffix$extension'));
      suffix++;
    } while (await destination.exists());
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
      Logger.debug('WalFileManager: Could not read isolated WAL manifest: ${error.runtimeType}');
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

class _WalOwnerRotation {
  const _WalOwnerRotation({
    required this.wal,
    required this.previousOwner,
    required this.previousPath,
    required this.previousStatus,
    required this.source,
    required this.destination,
    required this.sourceMissing,
  });

  final Wal wal;
  final WalOwner previousOwner;
  final String? previousPath;
  final WalStatus previousStatus;
  final File? source;
  final File? destination;
  final bool sourceMissing;
}

class _WalOwnerRecoveryBridge {
  const _WalOwnerRecoveryBridge({
    required this.sourceAuthorityFingerprint,
    required this.targetOwner,
  });

  final String sourceAuthorityFingerprint;
  final WalOwner targetOwner;
}

class _FileSnapshot {
  const _FileSnapshot({required this.file, required this.existed, required this.bytes});

  final File file;
  final bool existed;
  final List<int> bytes;

  static Future<_FileSnapshot> capture(File file) async {
    final existed = await file.exists();
    return _FileSnapshot(
      file: file,
      existed: existed,
      bytes: existed ? await file.readAsBytes() : const [],
    );
  }

  Future<void> restore() async {
    if (!existed) {
      if (await file.exists()) await file.delete();
      return;
    }
    await file.parent.create(recursive: true);
    await file.writeAsBytes(bytes, flush: true);
  }
}
