import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/services/wals/flash_page_wal_sync.dart';
import 'package:omi/services/wals/local_wal_sync.dart';
import 'package:omi/services/wals/sdcard_wal_sync.dart';
import 'package:omi/services/wals/wal.dart';
import 'package:omi/services/wals/wal_interfaces.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/audio_player_utils.dart';
import 'package:omi/utils/wal_file_manager.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late Directory directory;
  late _Listener listener;

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    directory = await Directory.systemTemp.createTemp('ella-wal-isolation-');
    listener = _Listener();
    WalFileManager.resetForTesting();
  });

  tearDown(() async {
    WalFileManager.resetForTesting();
    if (directory.existsSync()) await directory.delete(recursive: true);
  });

  test('restart quarantines legacy ownerless WAL and preserves its audio', () async {
    final audio = File('${directory.path}/legacy.bin')..writeAsBytesSync([1, 2, 3]);
    final legacy = _wal(owner: null, path: audio.path);
    File('${directory.path}/wals.json').writeAsStringSync(jsonEncode({
      'version': 1,
      'wals': [legacy.toJson()],
    }));

    final owner = _owner('uid-b');
    await WalFileManager.init(baseDirectory: directory, activeOwner: owner);
    final loaded = await WalFileManager.loadWals(activeOwner: owner);

    expect(loaded, isEmpty);
    expect(await WalFileManager.getQuarantineCount(), 1);
    expect(Directory('${directory.path}/ella_wal_quarantine').listSync().whereType<File>(), isNotEmpty);
    expect(File('${directory.path}/wals.json').existsSync(), isFalse);
  });

  test('restart moves orphan legacy audio bytes into quarantine without adopting them', () async {
    final orphan = File('${directory.path}/audio_omi_opus_16000_1_fs160_1785700000.bin')..writeAsBytesSync([9, 8, 7]);

    await WalFileManager.init(baseDirectory: directory, activeOwner: _owner('uid-new'));

    expect(orphan.existsSync(), isFalse);
    expect(await WalFileManager.getQuarantineCount(), 1);
    final moved = Directory('${directory.path}/ella_wal_quarantine')
        .listSync()
        .whereType<File>()
        .singleWhere((file) => file.path.endsWith('.bin'));
    expect(moved.readAsBytesSync(), [9, 8, 7]);
  });

  test('restart keeps an owned capture visible only to the same account binding', () async {
    final ownerA = _owner('uid-a');
    final ownerB = _owner('uid-b');
    await WalFileManager.init(baseDirectory: directory, activeOwner: ownerA);
    final source = File('${directory.path}/capture.bin')..writeAsBytesSync([1, 2, 3]);
    final capture = _wal(owner: ownerA, path: source.path);
    await WalFileManager.bindExternalWal(capture, owner: ownerA);
    await WalFileManager.saveWals([capture]);

    WalFileManager.resetForTesting();
    await WalFileManager.init(baseDirectory: directory, activeOwner: ownerB);
    expect(await WalFileManager.loadWals(activeOwner: ownerB), isEmpty);

    WalFileManager.resetForTesting();
    await WalFileManager.init(baseDirectory: directory, activeOwner: ownerA);
    final restored = await WalFileManager.loadWals(activeOwner: ownerA);
    expect(restored.single.owner!.matches(ownerA), isTrue);
    expect(File(restored.single.filePath!).readAsBytesSync(), [1, 2, 3]);
  });

  test('queued WAL for another account is quarantined and never uploaded', () async {
    final ownerA = _owner('uid-a');
    final ownerB = _owner('uid-b');
    final ownerADirectory = Directory('${directory.path}/ella_wal_accounts/${ownerA.storageNamespace}')
      ..createSync(recursive: true);
    final audio = File('${ownerADirectory.path}/queued.bin')..writeAsBytesSync([4, 5, 6]);
    final queued = _wal(owner: ownerA, path: audio.path);
    final ownerBDirectory = Directory('${directory.path}/ella_wal_accounts/${ownerB.storageNamespace}')
      ..createSync(recursive: true);
    File('${ownerBDirectory.path}/wals.json').writeAsStringSync(jsonEncode({
      'version': 2,
      'wals': [queued.toJson()],
    }));

    var uploadCount = 0;
    await WalFileManager.init(baseDirectory: directory, activeOwner: ownerB);
    final sync = LocalWalSyncImpl(
      listener,
      currentOwner: () => ownerB,
      activeAuthority: () => _authority(ownerB, () => true),
      upload: (files, uid) async {
        uploadCount++;
        return SyncLocalFilesResponse(newConversationIds: [], updatedConversationIds: []);
      },
    );
    await sync.initializeForTesting();
    await sync.syncAll();

    expect(uploadCount, 0);
    expect(await sync.getAllWals(), isEmpty);
    expect(await WalFileManager.getQuarantineCount(), 1);
  });

  test('account switch during in-flight upload cannot mark or expose the WAL as synced', () async {
    final owner = _owner('uid-a');
    var authorityCurrent = true;
    final uploadStarted = Completer<void>();
    final uploadResult = Completer<SyncLocalFilesResponse>();
    await WalFileManager.init(baseDirectory: directory, activeOwner: owner);
    final sync = LocalWalSyncImpl(
      listener,
      currentOwner: () => owner,
      activeAuthority: () => _authority(owner, () => authorityCurrent),
      upload: (files, uid) {
        expect(uid, 'uid-a');
        uploadStarted.complete();
        return uploadResult.future;
      },
    );
    await sync.initializeForTesting();
    final source = File('${directory.path}/capture.bin')..writeAsBytesSync([7, 8, 9]);
    await sync.addExternalWal(_wal(owner: owner, path: source.path));

    final pending = sync.syncAll();
    await uploadStarted.future;
    authorityCurrent = false;
    uploadResult
        .complete(SyncLocalFilesResponse(newConversationIds: ['should-not-commit'], updatedConversationIds: []));
    await pending;

    final wal = (await sync.getAllWals()).single;
    expect(wal.status, WalStatus.quarantined);
    expect(wal.quarantineReason, 'upload_authority_changed_in_flight');
    expect(listener.synced, isEmpty);
  });

  test('one late frame keeps its capture-start owner and is quarantined after account B becomes current', () async {
    final ownerA = _owner('uid-a');
    final ownerB = _owner('uid-b');
    await WalFileManager.init(baseDirectory: directory, activeOwner: ownerB);
    final sync = LocalWalSyncImpl(
      listener,
      currentOwner: () => ownerB,
      activeAuthority: () => _authority(ownerB, () => true),
    );
    await sync.initializeForTesting();
    await sync.onAudioCodecChanged(BleAudioCodec.opusFS320);

    sync.onByteStream(
      [0, 0, 1, 1, 2, 3],
      authorityAtCapture: _authority(ownerA, () => false),
    );
    await sync.stop();

    final captured = await sync.getAllWals();
    expect(captured, hasLength(1));
    expect(captured.single.owner?.matches(ownerA), isTrue);
    expect(captured.single.totalFrames, 1);
    expect(captured.single.status, WalStatus.quarantined);
    expect(captured.single.quarantineReason, 'account_transition_final_drain');
    expect(await WalFileManager.getQuarantineCount(), 1);
  });

  test('transition final drain preserves below, exact, and above-delay audio byte-for-byte', () async {
    final owner = _owner('uid-a');
    final frameCounts = [
      1,
      newFrameSyncDelaySeconds * BleAudioCodec.opusFS320.getFramesPerSecond() - 1,
      newFrameSyncDelaySeconds * BleAudioCodec.opusFS320.getFramesPerSecond(),
      newFrameSyncDelaySeconds * BleAudioCodec.opusFS320.getFramesPerSecond() + 1,
    ];

    for (var caseIndex = 0; caseIndex < frameCounts.length; caseIndex++) {
      WalFileManager.resetForTesting();
      final caseDirectory = Directory('${directory.path}/case-$caseIndex')..createSync(recursive: true);
      await WalFileManager.init(baseDirectory: caseDirectory, activeOwner: owner);
      final sync = LocalWalSyncImpl(
        listener,
        currentOwner: () => owner,
        activeAuthority: () => _authority(owner, () => false),
      );
      await sync.initializeForTesting();
      await sync.onAudioCodecChanged(BleAudioCodec.opusFS320);
      final expected = <List<int>>[];
      for (var frameIndex = 0; frameIndex < frameCounts[caseIndex]; frameIndex++) {
        final frame = [0, frameIndex ~/ 255, frameIndex % 255, 10, 20, frameIndex % 251];
        expected.add(frame);
        sync.onByteStream(frame, authorityAtCapture: _authority(owner, () => false));
      }

      await sync.stop();

      final captured = await sync.getAllWals();
      expect(captured, hasLength(1));
      expect(captured.single.owner?.matches(owner), isTrue);
      expect(captured.single.status, WalStatus.quarantined);
      expect(captured.single.totalFrames, frameCounts[caseIndex]);
      expect(captured.single.data, expected);
    }
  });

  test('transition final drain separates mixed old stale and ownerless frames without losing bytes', () async {
    final ownerA = _owner('uid-a');
    final ownerB = _owner('uid-b');
    await WalFileManager.init(baseDirectory: directory, activeOwner: ownerB);
    final sync = LocalWalSyncImpl(
      listener,
      currentOwner: () => ownerB,
      activeAuthority: () => _authority(ownerB, () => true),
    );
    await sync.initializeForTesting();
    final frames = [
      ([0, 0, 1, 11], ownerA),
      ([0, 0, 2, 12], ownerA),
      ([0, 0, 3, 13], null),
      ([0, 0, 4, 14], ownerB),
      ([0, 0, 5, 15], ownerA),
    ];
    for (final frame in frames) {
      sync.onByteStream(
        frame.$1,
        authorityAtCapture: frame.$2 == null ? null : _authority(frame.$2!, () => false),
      );
    }

    await sync.stop();

    final captured = await sync.getAllWals();
    expect(captured, hasLength(4));
    expect(captured.map((wal) => wal.totalFrames), [2, 1, 1, 1]);
    expect(captured[0].owner?.matches(ownerA), isTrue);
    expect(captured[1].owner, isNull);
    expect(captured[2].owner?.matches(ownerB), isTrue);
    expect(captured[3].owner?.matches(ownerA), isTrue);
    expect(captured.expand((wal) => wal.data), frames.map((frame) => frame.$1));
    expect(captured.every((wal) => wal.status == WalStatus.quarantined), isTrue);
  });

  test('process-local generation fences equality without changing the stable authority fingerprint', () async {
    final original = _owner('uid-a');
    final newerGeneration = WalOwner(
      uid: original.uid,
      profileBindingId: original.profileBindingId,
      bindingRevision: original.bindingRevision,
      consentReceiptId: original.consentReceiptId,
      authorityGenerationAtCapture: original.authorityGenerationAtCapture + 1,
    );
    expect(original.storageNamespace, newerGeneration.storageNamespace);
    expect(original.authorityFingerprint, newerGeneration.authorityFingerprint);
    expect(original.matches(newerGeneration), isFalse);

    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantOperationalAuthority(prefs, 'uid-a');
    expect(WalOwnerAuthority.active(preferences: prefs, authenticatedUid: 'uid-a'), isNotNull);
    prefs.declineAiConsent();
    expect(WalOwnerAuthority.active(preferences: prefs, authenticatedUid: 'uid-a'), isNull);
  });

  test('active WAL authority survives same-account server receipt and profile rotation', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantOperationalAuthority(prefs, 'uid-a');
    final authority = WalOwnerAuthority.active(preferences: prefs, authenticatedUid: 'uid-a');
    expect(authority, isNotNull);

    prefs.acceptAiConsent(
      receiptId: 'aicr_uid-a-new',
      uid: 'uid-a',
      profileBindingId: 'profile-uid-a-new',
      serverDecidedAt: '2026-08-02T00:01:00Z',
    );

    expect(authority!.isCurrent(preferences: prefs, authenticatedUid: 'uid-a'), isTrue);
    expect(authority.owner.consentReceiptId, 'aicr_uid-a');
    expect(authority.owner.profileBindingId, 'profile-uid-a');

    expect(authority.isCurrent(preferences: prefs, authenticatedUid: 'uid-b'), isFalse);
    prefs.declineAiConsent();
    expect(authority.isCurrent(preferences: prefs, authenticatedUid: 'uid-a'), isFalse);
    await _grantOperationalAuthority(prefs, 'uid-a');
    expect(authority.isCurrent(preferences: prefs, authenticatedUid: 'uid-a'), isFalse);
  });

  test('same-account authority rollover keeps capture-start frames syncable', () async {
    final ownerA = _owner('uid-a');
    final ownerB = _rotatedOwner('uid-a', suffix: 'b', bindingRevision: 4, generation: 8);
    final ownerC = _rotatedOwner('uid-a', suffix: 'c', bindingRevision: 5, generation: 9);
    final capturedAuthority = _authority(ownerA, () => true);
    var currentAuthority = capturedAuthority;
    var uploads = 0;
    SharedPreferencesUtil().unlimitedLocalStorageEnabled = true;
    await WalFileManager.init(baseDirectory: directory, activeOwner: ownerA);
    final sync = LocalWalSyncImpl(
      listener,
      currentOwner: () => currentAuthority.owner,
      activeAuthority: () => currentAuthority,
      upload: (files, uid) async {
        expect(uid, 'uid-a');
        uploads++;
        return SyncLocalFilesResponse(newConversationIds: ['conversation-a'], updatedConversationIds: []);
      },
    );
    await sync.initializeForTesting();
    await sync.onAudioCodecChanged(BleAudioCodec.opusFS320);
    final earlierAudio = File('${directory.path}/before-rollover.bin')..writeAsBytesSync([7, 8, 9]);
    await sync.addExternalWal(_wal(owner: ownerA, path: earlierAudio.path));
    _appendChunkableFrames(sync, capturedAuthority);

    currentAuthority = _authority(ownerB, () => true);
    await sync.chunkForTesting();
    sync.onByteStream([0, 3, 1, 10, 20, 30], authorityAtCapture: capturedAuthority);
    currentAuthority = _authority(ownerC, () => true);
    await sync.chunkForTesting();
    await sync.flushForTesting();

    final pending = await sync.getAllWals();
    expect(pending, hasLength(2));
    expect(pending.every((wal) => wal.owner?.matches(ownerC) == true), isTrue);
    expect(pending.every((wal) => wal.owner?.matches(ownerA) == false), isTrue);
    expect(pending.every((wal) => wal.owner?.matches(ownerB) == false), isTrue);
    expect(pending.every((wal) => wal.status == WalStatus.miss), isTrue);
    expect(pending.every((wal) => wal.storage == WalStorage.disk), isTrue);
    expect(pending.every((wal) => File(wal.filePath!).existsSync()), isTrue);

    await sync.syncAll();
    expect(uploads, 1);
    expect((await sync.getAllWals()).every((wal) => wal.status == WalStatus.synced), isTrue);
    expect(await WalFileManager.getQuarantineCount(), 0);
  });

  test('rollover skips a superseded intermediate owner while file copy is suspended', () async {
    final ownerA = _owner('uid-a');
    final ownerB = _rotatedOwner('uid-a', suffix: 'b', bindingRevision: 4, generation: 8);
    final ownerC = _rotatedOwner('uid-a', suffix: 'c', bindingRevision: 5, generation: 9);
    final capturedAuthority = _authority(ownerA, () => true);
    var currentAuthority = capturedAuthority;
    final copySuspended = Completer<void>();
    final resumeCopy = Completer<void>();
    var hookCalls = 0;
    WalFileManager.rotationBeforeCommitForTesting = () async {
      if (hookCalls++ != 0) return;
      copySuspended.complete();
      await resumeCopy.future;
    };
    SharedPreferencesUtil().unlimitedLocalStorageEnabled = true;
    await WalFileManager.init(baseDirectory: directory, activeOwner: ownerA);
    final sync = LocalWalSyncImpl(
      listener,
      currentOwner: () => currentAuthority.owner,
      activeAuthority: () => currentAuthority,
    );
    await sync.initializeForTesting();
    await sync.onAudioCodecChanged(BleAudioCodec.opusFS320);
    final earlierAudio = File('${directory.path}/before-overlap.bin')..writeAsBytesSync([7, 8, 9]);
    await sync.addExternalWal(_wal(owner: ownerA, path: earlierAudio.path));
    _appendChunkableFrames(sync, capturedAuthority);

    currentAuthority = _authority(ownerB, () => true);
    final chunk = sync.chunkForTesting();
    await copySuspended.future;
    currentAuthority = _authority(ownerC, () => true);
    resumeCopy.complete();
    await chunk;
    await sync.flushForTesting();

    final pending = await sync.getAllWals();
    expect(pending, hasLength(2));
    expect(pending.every((wal) => wal.owner?.matches(ownerC) == true), isTrue);
    expect(pending.every((wal) => wal.owner?.matches(ownerB) == false), isTrue);
    expect(pending.every((wal) => wal.status == WalStatus.miss), isTrue);
    expect(await WalFileManager.getQuarantineCount(), 0);
    final intermediateDirectory = Directory('${directory.path}/ella_wal_accounts/${ownerB.storageNamespace}');
    expect(File('${intermediateDirectory.path}/wals.json').existsSync(), isFalse);
    expect(File('${intermediateDirectory.path}/wals_backup.json').existsSync(), isFalse);
  });

  test('rollover restores manifests and original audio when commit fails after destination manifest write', () async {
    final ownerA = _owner('uid-a');
    final ownerB = _rotatedOwner('uid-a', suffix: 'b', bindingRevision: 4, generation: 8);
    final authorityA = _authority(ownerA, () => true);
    final authorityB = _authority(ownerB, () => true);
    await WalFileManager.init(baseDirectory: directory, activeOwner: ownerA);
    final source = File('${directory.path}/rollback-source.bin')..writeAsBytesSync([1, 2, 3]);
    final wal = _wal(owner: ownerA, path: source.path);
    await WalFileManager.bindExternalWal(wal, owner: ownerA);
    await WalFileManager.saveWals([wal]);
    final originalPath = wal.filePath!;
    WalFileManager.rotationAfterActiveManifestWriteForTesting = () async {
      throw const FileSystemException('injected manifest commit failure');
    };

    final rotated = await WalFileManager.rotateActiveSessionOwner(
      [wal],
      previousOwner: ownerA,
      capturedAuthority: authorityA,
      targetAuthority: authorityB,
      readCurrentAuthority: () => authorityB,
    );

    expect(rotated, isFalse);
    expect(wal.owner?.matches(ownerA), isTrue);
    expect(wal.filePath, originalPath);
    expect(File(originalPath).readAsBytesSync(), [1, 2, 3]);
    final destinationDirectory = Directory('${directory.path}/ella_wal_accounts/${ownerB.storageNamespace}');
    expect(File('${destinationDirectory.path}/wals.json').existsSync(), isFalse);
    expect(File('${destinationDirectory.path}/wals_backup.json').existsSync(), isFalse);
    expect(
      destinationDirectory.listSync().whereType<File>().where((file) => file.path.endsWith('.bin')),
      isEmpty,
    );

    WalFileManager.resetForTesting();
    await WalFileManager.init(baseDirectory: directory, activeOwner: ownerA);
    final restored = await WalFileManager.loadWals(activeOwner: ownerA);
    expect(restored, hasLength(1));
    expect(restored.single.owner?.matches(ownerA), isTrue);
    expect(File(restored.single.filePath!).readAsBytesSync(), [1, 2, 3]);
  });

  test('account transition cannot adopt capture-start frames into the next UID', () async {
    final ownerA = _owner('uid-a');
    final ownerB = _owner('uid-b');
    var capturedCurrent = true;
    final capturedAuthority = _authority(ownerA, () => capturedCurrent);
    var currentAuthority = capturedAuthority;
    SharedPreferencesUtil().unlimitedLocalStorageEnabled = true;
    await WalFileManager.init(baseDirectory: directory, activeOwner: ownerA);
    final sync = LocalWalSyncImpl(
      listener,
      currentOwner: () => currentAuthority.owner,
      activeAuthority: () => currentAuthority,
    );
    await sync.initializeForTesting();
    await sync.onAudioCodecChanged(BleAudioCodec.opusFS320);
    _appendChunkableFrames(sync, capturedAuthority);

    capturedCurrent = false;
    currentAuthority = _authority(ownerB, () => true);
    await sync.chunkForTesting();
    await sync.flushForTesting();

    final quarantined = (await sync.getAllWals()).single;
    expect(quarantined.status, WalStatus.quarantined);
    expect(quarantined.owner, isNull);
    expect(quarantined.quarantineReason, 'capture_without_owner');
  });

  test('explicit terminal revocation cannot adopt capture-start frames', () async {
    final ownerA = _owner('uid-a');
    var capturedCurrent = true;
    final capturedAuthority = _authority(ownerA, () => capturedCurrent);
    ActiveWalAuthority? currentAuthority = capturedAuthority;
    SharedPreferencesUtil().unlimitedLocalStorageEnabled = true;
    await WalFileManager.init(baseDirectory: directory, activeOwner: ownerA);
    final sync = LocalWalSyncImpl(
      listener,
      currentOwner: () => ownerA,
      activeAuthority: () => currentAuthority,
    );
    await sync.initializeForTesting();
    await sync.onAudioCodecChanged(BleAudioCodec.opusFS320);
    _appendChunkableFrames(sync, capturedAuthority);

    capturedCurrent = false;
    currentAuthority = null;
    await sync.chunkForTesting();
    await sync.flushForTesting();

    final quarantined = (await sync.getAllWals()).single;
    expect(quarantined.status, WalStatus.quarantined);
    expect(quarantined.owner, isNull);
    expect(quarantined.quarantineReason, 'capture_without_owner');
  });

  test('account transition fences active WAL authority before the UID changes and after re-grant', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantOperationalAuthority(prefs, 'uid-a');
    final authority = WalOwnerAuthority.active(preferences: prefs, authenticatedUid: 'uid-a');
    expect(authority, isNotNull);

    prefs.invalidateAccountAuthorityForTransition();
    expect(prefs.uid, 'uid-a');
    expect(authority!.isCurrent(preferences: prefs, authenticatedUid: 'uid-a'), isFalse);

    await _grantOperationalAuthority(prefs, 'uid-a');
    expect(authority.isCurrent(preferences: prefs, authenticatedUid: 'uid-a'), isFalse);
    expect(WalOwnerAuthority.active(preferences: prefs, authenticatedUid: 'uid-a'), isNotNull);
  });

  test('terminal provisioning invalidation stops an active WAL authority', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantOperationalAuthority(prefs, 'uid-a');
    final authority = WalOwnerAuthority.active(preferences: prefs, authenticatedUid: 'uid-a');
    expect(authority, isNotNull);

    prefs.invalidateEllaProvisioningServerVerification();
    expect(authority!.isCurrent(preferences: prefs, authenticatedUid: 'uid-a'), isTrue);

    prefs.invalidateEllaProvisioningTerminalAuthority();
    expect(authority.isCurrent(preferences: prefs, authenticatedUid: 'uid-a'), isFalse);
  });

  test('authority fingerprint uses canonical fields and malformed identifiers fail closed', () async {
    final first = _owner('uid-a');
    final second = WalOwner(
      uid: first.uid,
      profileBindingId: '${first.profileBindingId}-other',
      bindingRevision: first.bindingRevision,
      consentReceiptId: first.consentReceiptId,
      authorityGenerationAtCapture: first.authorityGenerationAtCapture,
    );
    expect(first.authorityFingerprint, isNot(second.authorityFingerprint));

    // These two owners collided under the former newline-delimited preimage.
    const malformedFirst = WalOwner(
      uid: 'uid-a\nprofile',
      profileBindingId: 'binding',
      bindingRevision: 3,
      consentReceiptId: 'aicr_receipt',
      authorityGenerationAtCapture: 1,
    );
    const malformedSecond = WalOwner(
      uid: 'uid-a',
      profileBindingId: 'profile\nbinding',
      bindingRevision: 3,
      consentReceiptId: 'aicr_receipt',
      authorityGenerationAtCapture: 1,
    );
    String formerPreimage(WalOwner owner) =>
        '${owner.uid}\n${owner.profileBindingId}\n${owner.bindingRevision}\n${owner.consentReceiptId}';
    expect(formerPreimage(malformedFirst), formerPreimage(malformedSecond));
    expect(malformedFirst.hasValidAuthorityIdentity, isFalse);
    expect(malformedSecond.hasValidAuthorityIdentity, isFalse);
    expect(() => malformedFirst.authorityFingerprint, throwsStateError);
    expect(() => malformedSecond.storageNamespace, throwsStateError);
    expect(malformedFirst.matches(malformedSecond), isFalse);

    final preferences = SharedPreferencesUtil()..uid = 'uid-a';
    preferences.verifiedPersonaId = 'persona-a';
    preferences.acceptAiConsent(
      receiptId: 'aicr_receipt',
      uid: 'uid-a',
      profileBindingId: 'profile\nbinding',
      serverDecidedAt: '2026-08-15T00:00:00Z',
    );
    await preferences.saveEllaProvisioningReceipt('uid-a', _provisioningReceipt());
    await preferences.markEllaProvisioningVerified('uid-a');
    preferences.markAiConsentServerVerified(
      uid: 'uid-a',
      receiptId: 'aicr_receipt',
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      profileBindingId: 'profile\nbinding',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );
    expect(WalOwnerAuthority.currentOwner(preferences: preferences, authenticatedUid: 'uid-a'), isNull);
  });

  test('persisted-only provisioning and consent cannot create active authority', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    prefs.acceptAiConsent(
      receiptId: 'aicr_uid-a',
      uid: 'uid-a',
      profileBindingId: 'profile-uid-a',
      serverDecidedAt: '2026-08-02T00:00:00Z',
    );
    await prefs.saveEllaProvisioningReceipt('uid-a', _provisioningReceipt());
    expect(WalOwnerAuthority.active(preferences: prefs, authenticatedUid: 'uid-a'), isNull);

    await prefs.markEllaProvisioningVerified('uid-a');
    expect(WalOwnerAuthority.active(preferences: prefs, authenticatedUid: 'uid-a'), isNull);
    await SharedPreferencesUtil.init();
    expect(WalOwnerAuthority.active(preferences: prefs, authenticatedUid: 'uid-a'), isNull);
  });

  test('SD and flash ownerless downloaded bytes stay quarantined and are never uploaded', () async {
    final owner = _owner('uid-a');
    var uploads = 0;
    await WalFileManager.init(baseDirectory: directory, activeOwner: owner);
    final sync = LocalWalSyncImpl(
      listener,
      currentOwner: () => owner,
      activeAuthority: () => _authority(owner, () => true),
      upload: (files, uid) async {
        uploads++;
        return SyncLocalFilesResponse(newConversationIds: [], updatedConversationIds: []);
      },
    );
    await sync.initializeForTesting();

    for (final storage in [WalStorage.sdcard, WalStorage.flashPage]) {
      final marker = storage == WalStorage.sdcard ? 31 : 41;
      final audio = File('${directory.path}/${storage.name}.bin')..writeAsBytesSync([marker, marker + 1]);
      final wal = _wal(owner: null, path: audio.path)
        ..originalStorage = storage
        ..status = WalStatus.quarantined
        ..quarantineReason = 'device_owner_provenance_unverified';
      await sync.addExternalWal(wal);
    }
    await sync.syncAll();

    expect(uploads, 0);
    expect(await sync.getAllWals(), isEmpty);
    expect(await WalFileManager.getQuarantineCount(), 2);
    final preserved = Directory('${directory.path}/ella_wal_quarantine')
        .listSync()
        .whereType<File>()
        .where((file) => file.path.endsWith('.bin'))
        .map((file) => file.readAsBytesSync())
        .toList();
    expect(preserved, contains(equals([31, 32])));
    expect(preserved, contains(equals([41, 42])));
  });

  test('ownerless and prior-owner device audio cannot sync, clear, acknowledge, or become active', () async {
    final sd = SDCardWalSyncImpl(listener);
    final flash = FlashPageWalSyncImpl(listener);
    final ownerlessSd = _deviceWal(WalStorage.sdcard, owner: null);
    final priorOwnerFlash = _deviceWal(WalStorage.flashPage, owner: _owner('uid-prior'));

    await sd.syncWal(wal: ownerlessSd);
    await sd.deleteWal(ownerlessSd);
    await flash.syncWal(wal: priorOwnerFlash);
    await flash.deleteWal(priorOwnerFlash);

    expect(ownerlessSd.status, WalStatus.quarantined);
    expect(priorOwnerFlash.status, WalStatus.quarantined);
    expect(await sd.getMissingWals(), isEmpty);
    expect(await flash.getMissingWals(), isEmpty);
    expect(listener.synced, isEmpty);
  });

  test('ownerless and quarantined audio is never playable or shareable', () {
    final audio = File('${directory.path}/audio.bin')..writeAsBytesSync([1, 2, 3]);
    final ownerless = _wal(owner: null, path: audio.path);
    final quarantined = _wal(owner: _owner('uid-a'), path: audio.path)..status = WalStatus.quarantined;
    final active = _wal(owner: _owner('uid-a'), path: audio.path);

    expect(AudioPlayerUtils().canPlayOrShare(ownerless), isFalse);
    expect(AudioPlayerUtils().canPlayOrShare(quarantined), isFalse);
    expect(AudioPlayerUtils().canPlayOrShare(active), isTrue);
  });
}

Future<void> _grantOperationalAuthority(SharedPreferencesUtil prefs, String uid) async {
  prefs.acceptAiConsent(
    receiptId: 'aicr_$uid',
    uid: uid,
    profileBindingId: 'profile-$uid',
    serverDecidedAt: '2026-08-02T00:00:00Z',
  );
  await prefs.saveEllaProvisioningReceipt(uid, _provisioningReceipt());
  await prefs.markEllaProvisioningVerified(uid);
  prefs.markAiConsentServerVerified(
    uid: uid,
    receiptId: 'aicr_$uid',
    policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
    processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
    profileBindingId: 'profile-$uid',
    scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
    scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
  );
}

Map<String, dynamic> _provisioningReceipt() => {
      'state': 'ready',
      'binding_state': 'active',
      'binding_revision': 3,
      'effective_policy_revision': 'policy-3',
    };

WalOwner _owner(String uid) => WalOwner(
      uid: uid,
      profileBindingId: 'profile-$uid',
      bindingRevision: 3,
      consentReceiptId: 'aicr_$uid',
      authorityGenerationAtCapture: 7,
    );

WalOwner _rotatedOwner(
  String uid, {
  required String suffix,
  required int bindingRevision,
  required int generation,
}) =>
    WalOwner(
      uid: uid,
      profileBindingId: 'profile-$uid-$suffix',
      bindingRevision: bindingRevision,
      consentReceiptId: 'aicr-$uid-$suffix',
      authorityGenerationAtCapture: generation,
    );

void _appendChunkableFrames(LocalWalSyncImpl sync, ActiveWalAuthority authority) {
  final count = newFrameSyncDelaySeconds * BleAudioCodec.opusFS320.getFramesPerSecond() + 1;
  for (var index = 0; index < count; index++) {
    sync.onByteStream(
      [0, index ~/ 255, index % 255, 10, 20, index % 251],
      authorityAtCapture: authority,
    );
  }
}

ActiveWalAuthority _authority(WalOwner owner, bool Function() current) => ActiveWalAuthority(
      owner: owner,
      consent: AiConsentAuthoritySnapshot(
        generation: owner.authorityGenerationAtCapture,
        terminalAccountConsentGeneration: 0,
        uid: owner.uid,
        verifiedPersonaId: null,
        profileBindingId: owner.profileBindingId,
        receiptId: owner.consentReceiptId,
        policyVersion: 'v8',
        processorSetHash: 'processors',
        scopeVersion: 'scope-v2',
        scopeHash: 'scope',
      ),
      currentCheck: current,
    );

Wal _wal({required WalOwner? owner, required String path}) => Wal(
      timerStart: 1,
      codec: BleAudioCodec.opus,
      seconds: 1,
      status: WalStatus.miss,
      storage: WalStorage.disk,
      filePath: path,
      owner: owner,
    );

Wal _deviceWal(WalStorage storage, {required WalOwner? owner}) => Wal(
      timerStart: 1,
      codec: BleAudioCodec.opus,
      seconds: 1,
      status: WalStatus.quarantined,
      storage: storage,
      owner: owner,
      quarantineReason: 'device_owner_provenance_unverified',
    );

class _Listener implements IWalSyncListener {
  final List<Wal> synced = [];

  @override
  void onWalSynced(Wal wal, {ServerConversation? conversation}) => synced.add(wal);

  @override
  void onWalUpdated() {}
}
