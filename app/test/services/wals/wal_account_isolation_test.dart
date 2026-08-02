import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/services/wals/local_wal_sync.dart';
import 'package:omi/services/wals/wal.dart';
import 'package:omi/services/wals/wal_interfaces.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/wal_file_manager.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late Directory directory;
  late _Listener listener;

  setUp(() async {
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
}

WalOwner _owner(String uid) => WalOwner(
      uid: uid,
      profileBindingId: 'profile-$uid',
      bindingRevision: 3,
      consentReceiptId: 'aicr_$uid',
      authorityGenerationAtCapture: 7,
    );

ActiveWalAuthority _authority(WalOwner owner, bool Function() current) => ActiveWalAuthority(
      owner: owner,
      consent: AiConsentAuthoritySnapshot(
        generation: owner.authorityGenerationAtCapture,
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

class _Listener implements IWalSyncListener {
  final List<Wal> synced = [];

  @override
  void onWalSynced(Wal wal, {ServerConversation? conversation}) => synced.add(wal);

  @override
  void onWalUpdated() {}
}
