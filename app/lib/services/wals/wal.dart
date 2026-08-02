import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:path_provider/path_provider.dart';

import 'package:omi/backend/schema/bt_device/bt_device.dart';

const chunkSizeInSeconds = 60;
const flushIntervalInSeconds = 90;
const sdcardChunkSizeSecs = 60;
const newFrameSyncDelaySeconds = 15;
const framesPerFlashPage = 8;
const secondsPerFlashPage = 1.4;

enum WalStatus {
  inProgress,
  miss,
  synced,
  corrupted,
  quarantined,
}

class WalOwner {
  const WalOwner({
    required this.uid,
    required this.profileBindingId,
    required this.bindingRevision,
    required this.consentReceiptId,
    required this.authorityGenerationAtCapture,
  });

  final String uid;
  final String profileBindingId;
  final int bindingRevision;
  final String consentReceiptId;
  final int authorityGenerationAtCapture;

  String get storageNamespace {
    final digest = sha256.convert(utf8.encode('$uid\n$profileBindingId\n$bindingRevision'));
    return digest.toString().substring(0, 24);
  }

  bool matches(WalOwner other) =>
      uid == other.uid &&
      profileBindingId == other.profileBindingId &&
      bindingRevision == other.bindingRevision &&
      consentReceiptId == other.consentReceiptId &&
      authorityGenerationAtCapture == other.authorityGenerationAtCapture;

  factory WalOwner.fromJson(Map<String, dynamic> json) => WalOwner(
        uid: json['uid'] as String? ?? '',
        profileBindingId: json['profile_binding_id'] as String? ?? '',
        bindingRevision: json['binding_revision'] as int? ?? 0,
        consentReceiptId: json['consent_receipt_id'] as String? ?? '',
        authorityGenerationAtCapture: json['authority_generation_at_capture'] as int? ?? 0,
      );

  Map<String, dynamic> toJson() => {
        'uid': uid,
        'profile_binding_id': profileBindingId,
        'binding_revision': bindingRevision,
        'consent_receipt_id': consentReceiptId,
        'authority_generation_at_capture': authorityGenerationAtCapture,
      };
}

enum WalStorage {
  mem,
  disk,
  sdcard,
  flashPage,
}

enum SyncMethod {
  ble,
  wifi,
}

class WalStats {
  final int totalFiles;
  final int phoneFiles;
  final int sdcardFiles;
  final int fromSdcardFiles;
  final int limitlessFiles;
  final int fromFlashPageFiles;
  final int phoneSize;
  final int sdcardSize;
  final int syncedFiles;
  final int missedFiles;

  WalStats({
    required this.totalFiles,
    required this.phoneFiles,
    required this.sdcardFiles,
    required this.fromSdcardFiles,
    required this.limitlessFiles,
    required this.fromFlashPageFiles,
    required this.phoneSize,
    required this.sdcardSize,
    required this.syncedFiles,
    required this.missedFiles,
  });

  int get sdcardRelatedFiles => sdcardFiles + fromSdcardFiles;
  int get flashPageRelatedFiles => limitlessFiles + fromFlashPageFiles;

  String get totalSizeFormatted => _formatBytes(phoneSize + sdcardSize);
  String get phoneSizeFormatted => _formatBytes(phoneSize);
  String get sdcardSizeFormatted => _formatBytes(sdcardSize);

  String _formatBytes(int bytes) {
    if (bytes < 1024) return '$bytes B';
    if (bytes < 1024 * 1024) return '${(bytes / 1024).toStringAsFixed(1)} KB';
    if (bytes < 1024 * 1024 * 1024) return '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';
    return '${(bytes / (1024 * 1024 * 1024)).toStringAsFixed(1)} GB';
  }
}

class Wal {
  int timerStart;
  BleAudioCodec codec;
  int channel;
  int sampleRate;
  int seconds;
  String device;
  String? deviceModel;

  WalStatus status;
  WalStorage storage;

  String? filePath;
  List<List<int>> data = [];
  int storageOffset = 0;
  int storageTotalBytes = 0;
  int fileNum = 1;

  bool isSyncing = false;
  DateTime? syncStartedAt;
  int? syncEtaSeconds;
  double? syncSpeedKBps;
  SyncMethod syncMethod = SyncMethod.ble;

  int frameSize = 160;

  int totalFrames = 0;
  int syncedFrameOffset = 0;

  WalStorage? originalStorage;
  WalOwner? owner;
  String? quarantineReason;

  String get id => '${owner?.storageNamespace ?? 'unknown'}_${device}_$timerStart';

  Wal({
    required this.timerStart,
    required this.codec,
    required this.seconds,
    this.sampleRate = 16000,
    this.channel = 1,
    this.status = WalStatus.inProgress,
    this.storage = WalStorage.mem,
    this.filePath,
    this.device = "phone",
    this.deviceModel,
    this.storageOffset = 0,
    this.storageTotalBytes = 0,
    this.fileNum = 1,
    this.data = const [],
    this.totalFrames = 0,
    this.syncedFrameOffset = 0,
    this.originalStorage,
    this.owner,
    this.quarantineReason,
  }) {
    frameSize = codec.getFrameSize();
  }

  factory Wal.fromJson(Map<String, dynamic> json) {
    return Wal(
      timerStart: json['timer_start'],
      codec: mapNameToCodec(json['codec']),
      channel: json['channel'] ?? 1,
      sampleRate: json['sample_rate'] ?? 16000,
      status: WalStatus.values.asNameMap()[json['status']] ?? WalStatus.inProgress,
      storage: WalStorage.values.asNameMap()[json['storage']] ?? WalStorage.mem,
      filePath: json['file_path'],
      seconds: json['seconds'] ?? chunkSizeInSeconds,
      device: json['device'] ?? "phone",
      deviceModel: json['device_model'],
      storageOffset: json['storage_offset'] ?? 0,
      storageTotalBytes: json['storage_total_bytes'] ?? 0,
      fileNum: json['file_num'] ?? 1,
      totalFrames: json['total_frames'] ?? 0,
      syncedFrameOffset: json['synced_frame_offset'] ?? 0,
      originalStorage:
          json['original_storage'] != null ? WalStorage.values.asNameMap()[json['original_storage']] : null,
      owner: json['owner'] is Map<String, dynamic> ? WalOwner.fromJson(json['owner'] as Map<String, dynamic>) : null,
      quarantineReason: json['quarantine_reason'] as String?,
    );
  }

  Map<String, dynamic> toJson() {
    return {
      'timer_start': timerStart,
      'codec': codec.toString(),
      'channel': channel,
      'sample_rate': sampleRate,
      'status': status.name,
      'storage': storage.name,
      'file_path': filePath,
      'seconds': seconds,
      'device': device,
      'device_model': deviceModel,
      'storage_offset': storageOffset,
      'storage_total_bytes': storageTotalBytes,
      'file_num': fileNum,
      'total_frames': totalFrames,
      'synced_frame_offset': syncedFrameOffset,
      'original_storage': originalStorage?.name,
      'owner': owner?.toJson(),
      'quarantine_reason': quarantineReason,
    };
  }

  static List<Wal> fromJsonList(List<dynamic> jsonList) => jsonList.map((e) => Wal.fromJson(e)).toList();

  getFileName() {
    return "audio_${device.replaceAll(RegExp(r'[^a-zA-Z0-9]'), "").toLowerCase()}_${codec}_${sampleRate}_${channel}_fs${frameSize}_${timerStart}.bin";
  }

  getFileNameByTimeStarts(int timestarts) {
    return "audio_${device.replaceAll(RegExp(r'[^a-zA-Z0-9]'), "").toLowerCase()}_${codec}_${sampleRate}_${channel}_fs${frameSize}_${timestarts}.bin";
  }

  static Future<String?> getFilePath(
    String? pathOrName, {
    WalOwner? owner,
    bool quarantined = false,
  }) async {
    if (pathOrName == null || pathOrName.isEmpty) {
      return null;
    }

    final directory = await getApplicationDocumentsDirectory();
    final filename = pathOrName.split('/').last;
    if (quarantined) return '${directory.path}/ella_wal_quarantine/$filename';
    if (owner != null) return '${directory.path}/ella_wal_accounts/${owner.storageNamespace}/$filename';
    return '${directory.path}/$filename';
  }
}
