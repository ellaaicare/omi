import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:collection/collection.dart';
import 'package:file_picker/file_picker.dart';
import 'package:image_picker/image_picker.dart';
import 'package:path/path.dart' as p;
import 'package:uuid/uuid.dart';

import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/http/api/apps.dart';
import 'package:omi/backend/http/api/messages.dart';
import 'package:omi/ella/services/ella_chat_service.dart';
import 'package:omi/ella/services/ella_service_result.dart';
import 'package:omi/backend/http/api/users.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/app.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/demo/demo_fixtures.dart';
import 'package:omi/ella/services/ai_consent_coordinator.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/providers/app_provider.dart';
import 'package:omi/main.dart';
import 'package:omi/utils/alerts/app_snackbar.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/analytics/mixpanel.dart';
import 'package:omi/utils/file.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/platform/platform_service.dart';
import 'package:omi/utils/streaming_text_coalescer.dart';

bool get _isEllaApp => true;

typedef ChatAppsRetriever = Future<List<App>> Function();
typedef EllaChatStreamSender = Stream<ServerMessageChunk> Function(
  String text, {
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
});
typedef VoiceChatStreamSender = Stream<ServerMessageChunk> Function(
  List<File> files, {
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
});
typedef VoiceTempFileSaver = Future<File> Function(List<List<int>> audioBytes, int startTime, int frameSize);
typedef AttachmentFilePicker = Future<List<File>> Function(int remainingSlots);
typedef MessageFileUploader = Future<List<MessageFile>?> Function(
    List<File> files, String? appId, ExactAccountAuthorityVerifier exactAuthority);
typedef AskAiStreamSender = Stream<ServerMessageChunk> Function(
  String message,
  List<String>? fileIds,
  ExactAccountAuthorityVerifier exactAuthority,
);
typedef AskAiResponseSink = FutureOr<void> Function(Map<String, dynamic> chunk);
typedef AiConsentEnsurer = Future<bool> Function();
typedef V2VTurnPersister = Future<EllaServiceResult<List<ServerMessage>>> Function({
  required String uid,
  required String sessionId,
  required String turnId,
  required String userTranscript,
  required String assistantTranscript,
  required DateTime startedAt,
  required DateTime completedAt,
  required ExactAccountAuthorityVerifier exactAuthority,
});

class MessageProtectedOperation {
  MessageProtectedOperation._(this._lease, this.generation, this._currentCheck);

  final EllaAccountCommitLease _lease;
  final int generation;
  final bool Function() _currentCheck;

  bool get isCurrent => _currentCheck();
  String get uid => _lease.uid;
  ExactAccountAuthorityVerifier get exactAuthority => _lease;
}

class MessageProvider extends ChangeNotifier {
  static late MethodChannel _askAIChannel;

  MessageProvider({
    ChatAppsRetriever? chatAppsRetriever,
    ActiveAccountAuthorityProvider? activeAuthority,
    EllaChatStreamSender? ellaChatStreamSender,
    VoiceChatStreamSender? voiceChatStreamSender,
    VoiceTempFileSaver? voiceTempFileSaver,
    AttachmentFilePicker? filePicker,
    MessageFileUploader? fileUploader,
    AskAiStreamSender? askAiStreamSender,
    AskAiResponseSink? askAiResponseSink,
    AiConsentEnsurer? aiConsentEnsurer,
    V2VTurnPersister? v2vTurnPersister,
  })  : _chatAppsRetriever = chatAppsRetriever ?? _retrieveInstalledChatApps,
        _activeAuthority = activeAuthority ?? WalOwnerAuthority.operationEntry,
        _ellaChatStreamSender = ellaChatStreamSender ?? sendEllaChatStream,
        _voiceChatStreamSender = voiceChatStreamSender ?? sendVoiceMessageStreamServer,
        _voiceTempFileSaver = voiceTempFileSaver ?? FileUtils.saveAudioBytesToTempFile,
        _filePicker = filePicker,
        _fileUploader = fileUploader ??
            ((files, appId, authority) => uploadFilesServer(files, appId: appId, exactAuthority: authority)),
        _askAiStreamSender = askAiStreamSender ??
            ((message, fileIds, authority) =>
                sendMessageStreamServer(message, filesId: fileIds, exactAuthority: authority)),
        _askAiResponseSink = askAiResponseSink,
        _aiConsentEnsurer = aiConsentEnsurer,
        _v2vTurnPersister = v2vTurnPersister ?? persistEllaV2VTurn {
    if (PlatformService.isDesktop) {
      _askAIChannel = const MethodChannel('com.omi/ask_ai');
      _askAIChannel.setMethodCallHandler(_handleAskAIMethodCall);
    }
  }

  final ChatAppsRetriever _chatAppsRetriever;
  final ActiveAccountAuthorityProvider _activeAuthority;
  final EllaChatStreamSender _ellaChatStreamSender;
  final VoiceChatStreamSender _voiceChatStreamSender;
  final VoiceTempFileSaver _voiceTempFileSaver;
  final AttachmentFilePicker? _filePicker;
  final MessageFileUploader _fileUploader;
  final AskAiStreamSender _askAiStreamSender;
  final AskAiResponseSink? _askAiResponseSink;
  final AiConsentEnsurer? _aiConsentEnsurer;
  final V2VTurnPersister _v2vTurnPersister;
  int _operationGeneration = 0;

  static Future<List<App>> _retrieveInstalledChatApps() async {
    final result = await retrieveAppsSearch(installedApps: true, limit: 50);
    return result.apps;
  }

  AppProvider? appProvider;
  List<ServerMessage> messages = [];
  bool _isNextMessageFromVoice = false;

  bool isLoadingMessages = false;
  bool hasCachedMessages = false;
  bool isClearingChat = false;
  bool showTypingIndicator = false;
  bool sendingMessage = false;
  double aiStreamProgress = 1.0;
  ClientApiFailure? _lastStreamFailure;

  ClientApiFailure? get lastStreamFailure => _lastStreamFailure;
  bool get requiresClientUpdate => _lastStreamFailure?.kind == ClientApiFailureKind.updateRequired;

  String firstTimeLoadingText = '';

  List<App> chatApps = [];
  bool isLoadingChatApps = false;

  List<File> selectedFiles = [];
  List<String> selectedFileTypes = [];
  List<MessageFile> uploadedFiles = [];
  bool isUploadingFiles = false;
  Map<String, bool> uploadingFiles = {};

  void updateAppProvider(AppProvider p) {
    appProvider = p;
  }

  Future<bool> _ensureAiConsent() async {
    final ensure = _aiConsentEnsurer;
    if (ensure != null) return ensure();
    if (SharedPreferencesUtil().aiConsentAccepted) return true;
    final context = MyApp.navigatorKey.currentContext;
    return context != null && await AiConsentCoordinator.ensure(context);
  }

  void reset() {
    _operationGeneration++;
    messages = [];
    isLoadingMessages = false;
    hasCachedMessages = false;
    isClearingChat = false;
    showTypingIndicator = false;
    sendingMessage = false;
    aiStreamProgress = 1.0;
    _lastStreamFailure = null;
    firstTimeLoadingText = '';
    chatApps = [];
    isLoadingChatApps = false;
    selectedFiles = [];
    selectedFileTypes = [];
    uploadedFiles = [];
    isUploadingFiles = false;
    uploadingFiles = {};
    notifyListeners();
  }

  EllaAccountCommitLease? _beginAccountCommit([VoidCallback? onInvalidated]) => EllaAccountCommitBarrier.begin(
        authorityProvider: _activeAuthority,
        onInvalidated: () {
          reset();
          onInvalidated?.call();
        },
      );

  Future<bool> _authorizeProtectedOperation(EllaAccountCommitLease lease, int generation) async =>
      await _ensureAiConsent() && _canCommit(lease, generation);

  Future<void> runProtectedOperationAtEntry(
    Future<void> Function(MessageProtectedOperation operation) action, {
    VoidCallback? onInvalidated,
  }) {
    final lease = _beginAccountCommit(onInvalidated);
    if (lease == null) return Future<void>.value();
    final generation = _operationGeneration;
    final operation = MessageProtectedOperation._(lease, generation, () => _canCommit(lease, generation));
    return _runProtectedOperation(operation, action);
  }

  Future<void> _runProtectedOperation(
    MessageProtectedOperation operation,
    Future<void> Function(MessageProtectedOperation operation) action,
  ) async {
    try {
      if (!await _authorizeProtectedOperation(operation._lease, operation.generation)) return;
      await action(operation);
    } finally {
      operation._lease.close();
    }
  }

  bool _canCommit(EllaAccountCommitLease lease, int generation) =>
      generation == _operationGeneration && lease.isCurrent;

  void _setStreamFailure(ClientApiFailure failure) {
    _lastStreamFailure = failure;
    notifyListeners();
  }

  void _discardAssistantAt(int index) {
    if (index >= 0 && index < messages.length && messages[index].sender == MessageSender.ai) {
      messages.removeAt(index);
    }
  }

  void setChatApps(List<App> apps) {
    chatApps = apps;
    notifyListeners();
  }

  void removeChatApp(String appId) {
    chatApps.removeWhere((app) => app.id == appId);
    notifyListeners();
  }

  Future<void> fetchChatApps() async {
    if (SharedPreferencesUtil.isPublicBuild || isLoadingChatApps) return;
    final lease = _beginAccountCommit();
    if (lease == null) return;
    final generation = _operationGeneration;
    isLoadingChatApps = true;
    notifyListeners();

    try {
      final apps = await _chatAppsRetriever();
      if (!_canCommit(lease, generation)) return;
      chatApps = apps.where((app) => app.worksWithChat()).toList();
    } catch (e) {
      if (!_canCommit(lease, generation)) return;
      Logger.debug('Error fetching chat apps: $e');
      chatApps = [];
    } finally {
      if (_canCommit(lease, generation)) {
        isLoadingChatApps = false;
        notifyListeners();
      }
      lease.close();
    }
  }

  void setNextMessageOriginIsVoice(bool isVoice) {
    _isNextMessageFromVoice = isVoice;
  }

  void setIsUploadingFiles() {
    if (uploadingFiles.values.contains(true)) {
      isUploadingFiles = true;
    } else {
      isUploadingFiles = false;
    }
    notifyListeners();
  }

  void setMultiUploadingFileStatus(List<String> ids, bool value) {
    for (var id in ids) {
      uploadingFiles[id] = value;
    }
    setIsUploadingFiles();
    notifyListeners();
  }

  Future<void> addFiles(List<File> files) async {
    if (selectedFiles.length + files.length > 4) {
      AppSnackbar.showSnackbarError('You can only select up to 4 files');
      return;
    }

    final lease = _beginAccountCommit();
    if (lease == null) return;
    final generation = _operationGeneration;

    try {
      if (!await _authorizeProtectedOperation(lease, generation)) return;
      await _addFilesWithLease(files, lease, generation);
    } finally {
      lease.close();
    }
  }

  Future<void> addFilesWithinOperation(List<File> files, MessageProtectedOperation operation) async {
    if (!operation.isCurrent || selectedFiles.length + files.length > 4) return;
    await _addFilesWithLease(files, operation._lease, operation.generation);
  }

  Future<void> _addFilesWithLease(List<File> files, EllaAccountCommitLease lease, int generation) async {
    final filesToAdd = <File>[];
    final typesToAdd = <String>[];
    for (final file in files) {
      final ext = p.extension(file.path).toLowerCase().replaceAll('.', '');
      typesToAdd.add(
        ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'heic', 'tiff', 'tif'].contains(ext) ? 'image' : 'file',
      );
      filesToAdd.add(file);
    }

    if (filesToAdd.isEmpty || !_canCommit(lease, generation)) return;
    selectedFiles.addAll(filesToAdd);
    selectedFileTypes.addAll(typesToAdd);
    try {
      await _uploadFilesWithLease(filesToAdd, appProvider?.selectedChatAppId, lease, generation);
    } catch (e) {
      if (!_canCommit(lease, generation)) return;
      Logger.debug('Failed to upload files: $e');
      if (selectedFiles.length >= filesToAdd.length) {
        selectedFiles.removeRange(selectedFiles.length - filesToAdd.length, selectedFiles.length);
        selectedFileTypes.removeRange(selectedFileTypes.length - filesToAdd.length, selectedFileTypes.length);
      }
      AppSnackbar.showSnackbarError('File upload failed. Please try again.');
    }
    if (_canCommit(lease, generation)) notifyListeners();
  }

  bool isFileUploading(String id) {
    return uploadingFiles[id] ?? false;
  }

  void setHasCachedMessages(bool value) {
    hasCachedMessages = value;
    notifyListeners();
  }

  void setSendingMessage(bool value) {
    sendingMessage = value;
    notifyListeners();
  }

  void setShowTypingIndicator(bool value) {
    showTypingIndicator = value;
    notifyListeners();
  }

  void setClearingChat(bool value) {
    isClearingChat = value;
    notifyListeners();
  }

  void setLoadingMessages(bool value) {
    isLoadingMessages = value;
    notifyListeners();
  }

  Future<void> captureImage() async {
    final l10n = MyApp.navigatorKey.currentContext?.l10n;
    if (PlatformService.isDesktop) {
      AppSnackbar.showSnackbarError(l10n?.msgCameraNotAvailable ?? 'Camera capture is not available on this platform');
      return;
    }

    final lease = _beginAccountCommit();
    if (lease == null) return;
    final generation = _operationGeneration;
    try {
      if (!await _authorizeProtectedOperation(lease, generation)) return;
      var res = await ImagePicker().pickImage(source: ImageSource.camera);
      if (res != null && _canCommit(lease, generation)) {
        final file = File(res.path);
        selectedFiles.add(file);
        selectedFileTypes.add('image');
        await _uploadFilesWithLease([file], appProvider?.selectedChatAppId, lease, generation);
        if (_canCommit(lease, generation)) notifyListeners();
      }
    } on PlatformException catch (e) {
      if (!_canCommit(lease, generation)) return;
      if (e.code == 'camera_access_denied') {
        AppSnackbar.showSnackbarError(
          l10n?.msgCameraPermissionDenied ?? 'Camera permission denied. Please allow access to camera',
        );
      } else {
        AppSnackbar.showSnackbarError(
          l10n?.msgCameraAccessError(e.message ?? e.code) ?? 'Error accessing camera: ${e.message ?? e.code}',
        );
      }
    } catch (e) {
      if (_canCommit(lease, generation)) {
        AppSnackbar.showSnackbarError(l10n?.msgPhotoError ?? 'Error taking photo. Please try again.');
      }
    } finally {
      lease.close();
    }
  }

  Future<void> selectImage() async {
    final l10n = MyApp.navigatorKey.currentContext?.l10n;
    if (selectedFiles.length >= 4) {
      AppSnackbar.showSnackbarError(l10n?.msgMaxImagesLimit ?? 'You can only select up to 4 images');
      return;
    }

    final lease = _beginAccountCommit();
    if (lease == null) return;
    final generation = _operationGeneration;
    try {
      if (!await _authorizeProtectedOperation(lease, generation)) return;
      List<File> files = [];

      if (PlatformService.isDesktop) {
        try {
          FilePickerResult? result = await FilePicker.platform.pickFiles(
            type: FileType.custom,
            allowedExtensions: ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp'],
            allowMultiple: true,
            dialogTitle: 'Select image files',
            withData: false,
            withReadStream: false,
          );

          if (result != null && result.files.isNotEmpty) {
            for (var file in result.files) {
              if (file.path != null && files.length < (4 - selectedFiles.length)) {
                files.add(File(file.path!));
              }
            }
          } else {
            return;
          }
        } on PlatformException catch (e) {
          if (_canCommit(lease, generation)) {
            AppSnackbar.showSnackbarError(
              l10n?.msgFilePickerError(e.message ?? '') ?? 'Error opening file picker: ${e.message}',
            );
          }
          return;
        } catch (e) {
          Logger.debug('FilePicker general error: $e');
          if (_canCommit(lease, generation)) {
            AppSnackbar.showSnackbarError(l10n?.msgSelectImagesError(e.toString()) ?? 'Error selecting images: $e');
          }
          return;
        }
      } else {
        List res = [];
        if (4 - selectedFiles.length == 1) {
          var image = await ImagePicker().pickImage(source: ImageSource.gallery);
          if (image != null) {
            res = [image];
          }
        } else {
          res = await ImagePicker().pickMultiImage(limit: 4 - selectedFiles.length);
        }

        for (var r in res) {
          files.add(File(r.path));
        }
      }

      if (files.isNotEmpty && _canCommit(lease, generation)) {
        selectedFiles.addAll(files);
        selectedFileTypes.addAll(files.map((e) => 'image'));
        await _uploadFilesWithLease(files, appProvider?.selectedChatAppId, lease, generation);
      }
      if (_canCommit(lease, generation)) notifyListeners();
    } on PlatformException catch (e) {
      Logger.debug('🖼️ PlatformException during image picking: ${e.code} - ${e.message}');
      if (!_canCommit(lease, generation)) return;
      if (e.code == 'photo_access_denied') {
        AppSnackbar.showSnackbarError(
          l10n?.msgPhotosPermissionDenied ?? 'Photos permission denied. Please allow access to photos to select images',
        );
      } else {
        AppSnackbar.showSnackbarError(
          l10n?.msgSelectImagesError(e.message ?? e.code) ?? 'Error selecting images: ${e.message ?? e.code}',
        );
      }
    } catch (e) {
      Logger.debug('🖼️ General exception during image picking: $e');
      if (_canCommit(lease, generation)) {
        AppSnackbar.showSnackbarError(l10n?.msgSelectImagesGenericError ?? 'Error selecting images. Please try again.');
      }
    } finally {
      lease.close();
    }
  }

  Future<void> selectFile() async {
    final l10n = MyApp.navigatorKey.currentContext?.l10n;
    if (selectedFiles.length >= 4) {
      AppSnackbar.showSnackbarError(l10n?.msgMaxFilesLimit ?? 'You can only select up to 4 files');
      return;
    }

    final lease = _beginAccountCommit();
    if (lease == null) return;
    final generation = _operationGeneration;
    try {
      if (!await _authorizeProtectedOperation(lease, generation)) return;
      List<File> files;
      if (_filePicker != null) {
        files = await _filePicker!(4 - selectedFiles.length);
      } else {
        final result = await FilePicker.platform.pickFiles(
          type: FileType.custom,
          allowMultiple: true,
          allowedExtensions: ['jpeg', 'md', 'pdf', 'gif', 'doc', 'png', 'pptx', 'txt', 'xlsx', 'webp'],
          dialogTitle: 'Select files',
          withData: false,
          withReadStream: false,
        );
        files = [];
        if (result != null) {
          for (final platformFile in result.files) {
            if (platformFile.path != null && files.length < (4 - selectedFiles.length)) {
              files.add(File(platformFile.path!));
            }
          }
        }
      }

      if (!_canCommit(lease, generation)) return;
      if (files.isNotEmpty) {
        selectedFiles.addAll(files);
        selectedFileTypes.addAll(files.map((e) => 'file'));
        await _uploadFilesWithLease(files, appProvider?.selectedChatAppId, lease, generation);
      }
      if (_canCommit(lease, generation)) {
        notifyListeners();
      }
    } on PlatformException catch (e) {
      if (_canCommit(lease, generation)) {
        AppSnackbar.showSnackbarError(
          l10n?.msgSelectFilesError(e.message ?? e.code) ?? 'Error selecting files: ${e.message ?? e.code}',
        );
      }
    } catch (e) {
      if (_canCommit(lease, generation)) {
        AppSnackbar.showSnackbarError(l10n?.msgSelectFilesGenericError ?? 'Error selecting files. Please try again.');
      }
    } finally {
      lease.close();
    }
  }

  void clearSelectedFile(int index) {
    selectedFiles.removeAt(index);
    selectedFileTypes.removeAt(index);
    uploadedFiles.removeAt(index);
    notifyListeners();
  }

  void clearSelectedFiles() {
    selectedFiles.clear();
    selectedFileTypes.clear();
    notifyListeners();
  }

  void clearUploadedFiles() {
    uploadedFiles.clear();
    notifyListeners();
  }

  Future<List<MessageFile>?> uploadFiles(List<File> files, String? appId) async {
    final lease = _beginAccountCommit();
    if (lease == null) return null;
    final generation = _operationGeneration;
    try {
      if (!await _authorizeProtectedOperation(lease, generation)) return null;
      return await _uploadFilesWithLease(files, appId, lease, generation);
    } finally {
      lease.close();
    }
  }

  Future<List<MessageFile>?> _uploadFilesWithLease(
    List<File> files,
    String? appId,
    EllaAccountCommitLease lease,
    int generation,
  ) async {
    if (files.isEmpty || !_canCommit(lease, generation)) return null;
    final paths = files.map((file) => file.path).toList();
    setMultiUploadingFileStatus(paths, true);
    try {
      if (!_canCommit(lease, generation)) return null;
      final result = await _fileUploader(files, appId, lease);
      if (!_canCommit(lease, generation)) return null;
      if (result != null) {
        uploadedFiles.addAll(result);
      } else {
        clearSelectedFiles();
        final l10n = MyApp.navigatorKey.currentContext?.l10n;
        AppSnackbar.showSnackbarError(l10n?.msgUploadFileFailed ?? 'Failed to upload file, please try again later');
      }
      return result;
    } finally {
      if (_canCommit(lease, generation)) {
        setMultiUploadingFileStatus(paths, false);
        notifyListeners();
      }
    }
  }

  void removeLocalMessage(String id) {
    messages.removeWhere((m) => m.id == id);
    notifyListeners();
  }

  Future refreshMessages({bool dropdownSelected = false}) async {
    final lease = _beginAccountCommit();
    if (lease == null) return;
    final generation = _operationGeneration;
    try {
      setLoadingMessages(true);
      if (SharedPreferencesUtil().demoMode) {
        messages = DemoFixtures.chatMessages();
        setHasCachedMessages(true);
        setLoadingMessages(false);
        notifyListeners();
        return;
      }
      if (SharedPreferencesUtil().cachedMessages.isNotEmpty) {
        setHasCachedMessages(true);
      }

      // Ella mode: use local cache first, fall back to server history API.
      final isEllaApp = _isEllaApp; // TODO: replace with flavor check
      if (isEllaApp) {
        final cached = SharedPreferencesUtil().cachedMessages;
        if (cached.isNotEmpty) {
          messages = cached;
          setHasCachedMessages(true);
        }

        // Always try to rehydrate from server so a bad local/demo cache from a
        // previous TestFlight cannot mask the real account timeline.
        final historyResult = await fetchEllaChatHistory(
          limit: 50,
          expectedAuthenticatedUid: lease.uid,
          exactAuthority: lease,
        );
        if (!_canCommit(lease, generation)) return;
        if (historyResult.isFailure) {
          _setStreamFailure(
            historyResult.failure ?? const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true),
          );
        } else {
          _lastStreamFailure = null;
          final history = historyResult.value ?? const <ServerMessage>[];
          messages = history;
          SharedPreferencesUtil().cachedMessages = messages;
          setHasCachedMessages(messages.isNotEmpty);
        }
        messages.sort((a, b) => a.createdAt.compareTo(b.createdAt));
        setLoadingMessages(false);
        notifyListeners();
        return;
      }

      // Stock OMI mode: fetch from /v2/messages as before
      // Preserve locally-injected voice messages before server fetch
      final localVoiceMessages = messages.where((m) => m.fromVoice == true).toList();
      messages = await getMessagesFromServer(dropdownSelected: dropdownSelected);
      if (!_canCommit(lease, generation)) return;
      if (messages.isEmpty) {
        messages = SharedPreferencesUtil().cachedMessages;
      } else {
        // Merge back voice messages that aren't on server yet
        for (final vm in localVoiceMessages) {
          if (messages.firstWhereOrNull((m) => m.id == vm.id) == null) {
            messages.add(vm);
          }
        }
        SharedPreferencesUtil().cachedMessages = messages;
        setHasCachedMessages(true);
      }
      messages.sort((a, b) => a.createdAt.compareTo(b.createdAt));
      setLoadingMessages(false);
      notifyListeners();
    } finally {
      lease.close();
    }
  }

  void setMessagesFromCache() {
    if (SharedPreferencesUtil().cachedMessages.isNotEmpty) {
      setHasCachedMessages(true);
      messages = SharedPreferencesUtil().cachedMessages;
      messages.sort((a, b) => a.createdAt.compareTo(b.createdAt));
    }
    notifyListeners();
  }

  Future<List<ServerMessage>> getMessagesFromServer({bool dropdownSelected = false}) async {
    final lease = _beginAccountCommit();
    if (lease == null) return const [];
    final generation = _operationGeneration;
    try {
      final l10n = MyApp.navigatorKey.currentContext?.l10n;
      if (!hasCachedMessages) {
        firstTimeLoadingText = l10n?.msgReadingMemories ?? 'Reading your memories...';
        notifyListeners();
      }
      setLoadingMessages(true);
      var mes = await getMessagesServer(appId: appProvider?.selectedChatAppId, dropdownSelected: dropdownSelected);
      if (!_canCommit(lease, generation)) return const [];
      if (!hasCachedMessages) {
        firstTimeLoadingText = l10n?.msgLearningMemories ?? 'Learning from your memories...';
        notifyListeners();
      }
      messages = mes;
      messages.sort((a, b) => a.createdAt.compareTo(b.createdAt));
      setLoadingMessages(false);
      notifyListeners();
      return messages;
    } finally {
      lease.close();
    }
  }

  Future setMessageNps(ServerMessage message, int value, {String? reason}) async {
    final lease = _beginAccountCommit();
    if (lease == null) return;
    final generation = _operationGeneration;
    try {
      await setMessageResponseRating(message.id, value, reason: reason);
      if (!_canCommit(lease, generation)) return;
      message.askForNps = false;
      // Update local message rating so it persists when scrolling
      message.rating = value == 0 ? null : value;
      notifyListeners();
    } finally {
      lease.close();
    }
  }

  Future clearChat() async {
    final lease = _beginAccountCommit();
    if (lease == null) return;
    final generation = _operationGeneration;
    try {
      setClearingChat(true);
      var mes = await clearChatServer(appId: appProvider?.selectedChatAppId);
      if (!_canCommit(lease, generation)) return;
      messages = mes;
      messages.sort((a, b) => a.createdAt.compareTo(b.createdAt));
      setClearingChat(false);
      notifyListeners();
    } finally {
      lease.close();
    }
  }

  void addMessageLocally(String messageText) {
    List<String> fileIds = uploadedFiles.map((e) => e.id).toList();
    var appId = appProvider?.selectedChatAppId;
    if (appId == 'no_selected') {
      appId = null;
    }
    var message = ServerMessage(
      const Uuid().v4(),
      DateTime.now(),
      messageText,
      MessageSender.human,
      MessageType.text,
      appId,
      false,
      List.from(uploadedFiles),
      fileIds,
      [],
    );
    if (messages.firstWhereOrNull((m) => m.id == message.id) != null) {
      return;
    }
    messages.add(message);
    // Persist immediately so the user message survives tab switches before streaming completes
    SharedPreferencesUtil().cachedMessages = messages;
    notifyListeners();
  }

  void addMessage(ServerMessage message) {
    if (messages.firstWhereOrNull((m) => m.id == message.id) != null) {
      return;
    }
    messages.add(message);
    // Persist to cache so voice messages survive tab switches / refreshes
    SharedPreferencesUtil().cachedMessages = messages;
    notifyListeners();
  }

  bool addVoiceMessagesForProtectedOperation(
    ServerMessage userMessage,
    ServerMessage assistantMessage,
    MessageProtectedOperation operation,
  ) {
    if (!operation.isCurrent) return false;
    final additions = [
      userMessage,
      assistantMessage,
    ].where((message) => messages.firstWhereOrNull((existing) => existing.id == message.id) == null).toList();
    if (additions.isEmpty) return true;
    messages.addAll(additions);
    if (!operation.isCurrent) {
      messages.removeWhere((message) => additions.any((addition) => addition.id == message.id));
      return false;
    }
    SharedPreferencesUtil().cachedMessages = messages;
    notifyListeners();
    return true;
  }

  Future<bool> persistV2VTurn({
    required String expectedUid,
    required String sessionId,
    required String turnId,
    required String userTranscript,
    required String assistantTranscript,
    required DateTime startedAt,
    required DateTime completedAt,
  }) async {
    final lease = _beginAccountCommit();
    if (lease == null || lease.uid != expectedUid) {
      lease?.close();
      return false;
    }
    final generation = _operationGeneration;
    try {
      if (!await _authorizeProtectedOperation(lease, generation)) return false;
      final result = await _v2vTurnPersister(
        uid: expectedUid,
        sessionId: sessionId,
        turnId: turnId,
        userTranscript: userTranscript,
        assistantTranscript: assistantTranscript,
        startedAt: startedAt,
        completedAt: completedAt,
        exactAuthority: lease,
      );
      if (!_canCommit(lease, generation)) return false;
      if (result.isFailure) {
        _setStreamFailure(result.failure ?? const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true));
        return false;
      }

      final canonicalMessages = result.value ?? const <ServerMessage>[];
      if (canonicalMessages.length != 2) {
        _setStreamFailure(const ClientApiFailure(ClientApiFailureKind.invalidResponse));
        return false;
      }
      final canonicalIds = canonicalMessages.map((message) => message.id).toSet();
      final canonicalSenders = canonicalMessages.map((message) => message.sender).toSet();
      if (canonicalIds.length != 2 ||
          canonicalSenders.length != 2 ||
          !canonicalSenders.containsAll({MessageSender.human, MessageSender.ai}) ||
          canonicalMessages.any((message) => !message.fromVoice)) {
        _setStreamFailure(const ClientApiFailure(ClientApiFailureKind.invalidResponse));
        return false;
      }

      final updated = List<ServerMessage>.from(messages);
      for (final canonicalMessage in canonicalMessages) {
        final index = updated.indexWhere((message) => message.id == canonicalMessage.id);
        if (index < 0) {
          updated.add(canonicalMessage);
        } else {
          updated[index] = canonicalMessage;
        }
      }
      updated.sort((a, b) => a.createdAt.compareTo(b.createdAt));
      if (!_canCommit(lease, generation)) return false;
      messages = updated;
      _lastStreamFailure = null;
      SharedPreferencesUtil().cachedMessages = messages;
      setHasCachedMessages(true);
      notifyListeners();
      return true;
    } finally {
      lease.close();
    }
  }

  Future sendVoiceMessageStreamToServer(
    List<List<int>> audioBytes, {
    Function? onFirstChunkRecived,
    BleAudioCodec? codec,
  }) async {
    final lease = _beginAccountCommit();
    if (lease == null) return;
    final operationGeneration = _operationGeneration;
    try {
      if (!await _authorizeProtectedOperation(lease, operationGeneration)) return;
      var file = await _voiceTempFileSaver(
        audioBytes,
        DateTime.now().millisecondsSinceEpoch ~/ 1000 - (audioBytes.length / 100).ceil(),
        codec?.getFrameSize() ?? 160,
      );
      if (!_canCommit(lease, operationGeneration)) return;

      var currentAppId = appProvider?.selectedChatAppId;
      if (currentAppId == 'no_selected') {
        currentAppId = null;
      }
      String chatTargetId = currentAppId ?? 'omi';
      App? targetApp =
          currentAppId != null ? appProvider?.apps.firstWhereOrNull((app) => app.id == currentAppId) : null;
      bool isPersonaChat = targetApp != null ? !targetApp.isNotPersona() : false;

      MixpanelManager().chatVoiceInputUsed(chatTargetId: chatTargetId, isPersonaChat: isPersonaChat);

      if (!_canCommit(lease, operationGeneration)) return;
      setShowTypingIndicator(true);
      var message = ServerMessage.empty();
      messages.add(message);
      var aiIndex = messages.length - 1;
      final textCoalescer = StreamingTextCoalescer();
      notifyListeners();

      try {
        bool firstChunkRecieved = false;
        _lastStreamFailure = null;
        final chunks = await collectTerminalMessageChunks(
          _voiceChatStreamSender([file], expectedAuthenticatedUid: lease.uid, exactAuthority: lease),
        );
        if (!_canCommit(lease, operationGeneration)) return;
        for (final chunk in chunks) {
          if (!_canCommit(lease, operationGeneration)) return;
          if (!firstChunkRecieved &&
              [
                MessageChunkType.message,
                MessageChunkType.data,
                MessageChunkType.done,
                MessageChunkType.think,
              ].contains(chunk.type)) {
            firstChunkRecieved = true;
            if (onFirstChunkRecived != null) {
              onFirstChunkRecived();
            }
          }

          if (chunk.type == MessageChunkType.think) {
            message.thinkings.add(chunk.text);
            notifyListeners();
            continue;
          }

          if (chunk.type == MessageChunkType.data) {
            final utteranceId = chunk.messageId.isEmpty ? message.id : chunk.messageId;
            if (message.isEmpty && utteranceId.isNotEmpty) message.id = utteranceId;
            message.text = textCoalescer.addPartial(utteranceId, chunk.text);
            notifyListeners();
            continue;
          }

          if (chunk.type == MessageChunkType.done) {
            if (chunk.message != null) {
              message = chunk.message!;
              messages[aiIndex] = message;
            }
            textCoalescer.complete(chunk.messageId);
            notifyListeners();
            continue;
          }

          if (chunk.type == MessageChunkType.message) {
            final incoming = chunk.message;
            if (incoming != null) {
              final existingIndex = messages.indexWhere((candidate) => candidate.id == incoming.id);
              if (existingIndex >= 0) {
                messages[existingIndex] = incoming;
                if (existingIndex == aiIndex) message = incoming;
              } else {
                messages.insert(aiIndex, incoming);
                aiIndex++;
              }
            }
            notifyListeners();
            continue;
          }

          if (chunk.type == MessageChunkType.error) {
            throw const ClientApiFailure(ClientApiFailureKind.invalidResponse);
          }
        }
      } on ClientApiFailure catch (failure) {
        if (!_canCommit(lease, operationGeneration)) return;
        _discardAssistantAt(aiIndex);
        _setStreamFailure(failure);
      } catch (_) {
        if (!_canCommit(lease, operationGeneration)) return;
        _discardAssistantAt(aiIndex);
        _setStreamFailure(const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true));
      }

      if (_canCommit(lease, operationGeneration)) setShowTypingIndicator(false);
    } finally {
      lease.close();
    }
  }

  Future sendMessageStreamToServer(String text) async {
    final lease = _beginAccountCommit();
    if (lease == null) return;
    final operationGeneration = _operationGeneration;
    try {
      if (!await _authorizeProtectedOperation(lease, operationGeneration)) return;
      if (SharedPreferencesUtil().demoMode) {
        if (!_canCommit(lease, operationGeneration)) return;
        messages = DemoFixtures.chatMessages();
        setSendingMessage(false);
        setShowTypingIndicator(false);
        notifyListeners();
        return;
      }
      if (!_canCommit(lease, operationGeneration)) return;
      aiStreamProgress = 0.0;
      _lastStreamFailure = null;
      setShowTypingIndicator(true);
      var currentAppId = appProvider?.selectedChatAppId;
      if (currentAppId == 'no_selected') {
        currentAppId = null;
      }

      String chatTargetId = currentAppId ?? 'omi';
      App? targetApp =
          currentAppId != null ? appProvider?.apps.firstWhereOrNull((app) => app.id == currentAppId) : null;
      bool isPersonaChat = targetApp != null ? !targetApp.isNotPersona() : false;

      MixpanelManager().chatMessageSent(
        message: text,
        includesFiles: uploadedFiles.isNotEmpty,
        numberOfFiles: uploadedFiles.length,
        chatTargetId: chatTargetId,
        isPersonaChat: isPersonaChat,
        isVoiceInput: _isNextMessageFromVoice,
      );
      _isNextMessageFromVoice = false;

      if (!_canCommit(lease, operationGeneration)) return;
      var message = ServerMessage.empty(appId: currentAppId);
      messages.add(message);
      final aiIndex = messages.length - 1;
      final textCoalescer = StreamingTextCoalescer();
      notifyListeners();
      List<String> fileIds = uploadedFiles.map((e) => e.id).toList();
      clearSelectedFiles();
      clearUploadedFiles();
      try {
        // Ella uses its own simple chat endpoint; OMI uses the graph chat
        final isEllaApp = _isEllaApp; // TODO: replace with flavor check
        var stream = isEllaApp
            ? _ellaChatStreamSender(text, expectedAuthenticatedUid: lease.uid, exactAuthority: lease)
            : sendMessageStreamServer(
                text,
                appId: currentAppId,
                filesId: fileIds,
                expectedAuthenticatedUid: lease.uid,
                exactAuthority: lease,
              );
        final chunks = await collectTerminalMessageChunks(stream);
        if (!_canCommit(lease, operationGeneration)) return;
        for (final chunk in chunks) {
          if (!_canCommit(lease, operationGeneration)) return;
          if (chunk.type == MessageChunkType.think) {
            message.thinkings.add(chunk.text);
            notifyListeners();
            continue;
          }

          if (chunk.type == MessageChunkType.data) {
            final utteranceId = chunk.messageId.isEmpty ? message.id : chunk.messageId;
            if (message.isEmpty && utteranceId.isNotEmpty) message.id = utteranceId;
            message.text = textCoalescer.addPartial(utteranceId, chunk.text);
            aiStreamProgress = (aiStreamProgress + 0.05).clamp(0.0, 1.0);
            HapticFeedback.lightImpact();
            notifyListeners();
            continue;
          }

          if (chunk.type == MessageChunkType.done) {
            // Guard: OpenAI-style done chunks may have null message
            if (chunk.message != null) {
              message = chunk.message!;
              messages[aiIndex] = message;
            }
            textCoalescer.complete(chunk.messageId);
            notifyListeners();
            continue;
          }

          if (chunk.type == MessageChunkType.error) {
            throw const ClientApiFailure(ClientApiFailureKind.invalidResponse);
          }
        }
      } on ClientApiFailure catch (failure) {
        if (!_canCommit(lease, operationGeneration)) return;
        _discardAssistantAt(aiIndex);
        _setStreamFailure(failure);
      } catch (_) {
        if (!_canCommit(lease, operationGeneration)) return;
        _discardAssistantAt(aiIndex);
        _setStreamFailure(const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true));
      } finally {
        if (_canCommit(lease, operationGeneration)) {
          aiStreamProgress = 1.0;
          setShowTypingIndicator(false);
          setSendingMessage(false);
          // Persist only while the exact account authority remains current.
          SharedPreferencesUtil().cachedMessages = messages;
        }
      }
    } finally {
      lease.close();
    }
  }

  Future sendInitialAppMessage(App? app) async {
    final lease = _beginAccountCommit();
    if (lease == null) return;
    final generation = _operationGeneration;
    try {
      setSendingMessage(true);
      ServerMessage message = await getInitialAppMessage(app?.id);
      if (!_canCommit(lease, generation)) return;
      addMessage(message);
      setSendingMessage(false);
      notifyListeners();
    } finally {
      lease.close();
    }
  }

  App? messageSenderApp(String? appId) {
    return appProvider?.apps.firstWhereOrNull((p) => p.id == appId);
  }

  Future<void> _emitAskAiChunk(Map<String, dynamic> chunk, EllaAccountCommitLease lease, int generation) async {
    if (!_canCommit(lease, generation)) return;
    final sink = _askAiResponseSink;
    if (sink != null) {
      await sink(chunk);
    } else {
      await _askAIChannel.invokeMethod('aiResponseChunk', chunk);
    }
    if (!_canCommit(lease, generation)) return;
  }

  @visibleForTesting
  Future<void> handleAskAIForTesting(MethodCall call) => _handleAskAIMethodCall(call, allowAnyPlatform: true);

  Future<void> _handleAskAIMethodCall(MethodCall call, {bool allowAnyPlatform = false}) async {
    if (!allowAnyPlatform && !PlatformService.isDesktop) {
      return;
    }
    switch (call.method) {
      case 'sendQuery':
        final args = call.arguments as Map<dynamic, dynamic>;
        final message = args['message'] as String;
        final filePath = args['filePath'] as String?;

        final lease = _beginAccountCommit();
        if (lease == null) return;
        final generation = _operationGeneration;

        try {
          if (!await _authorizeProtectedOperation(lease, generation)) return;
          List<String>? fileIds;
          if (filePath != null && filePath.isNotEmpty) {
            final file = File(filePath);
            final uploadedFilesResult = await _uploadFilesWithLease([file], null, lease, generation);
            if (!_canCommit(lease, generation)) return;
            if (uploadedFilesResult != null) {
              fileIds = uploadedFilesResult.map((f) => f.id).toList();
            } else {
              final l10n = MyApp.navigatorKey.currentContext?.l10n;
              await _emitAskAiChunk(
                {'type': 'error', 'text': l10n?.msgUploadAttachedFileFailed ?? 'Failed to upload the attached file.'},
                lease,
                generation,
              );
              return;
            }
          }

          if (!_canCommit(lease, generation)) return;
          await for (var chunk in _askAiStreamSender(message, fileIds, lease)) {
            if (!_canCommit(lease, generation)) return;
            final chunkMap = {
              'type': chunk.type.toString().split('.').last,
              'text': chunk.text,
              'messageId': chunk.messageId,
            };
            if (chunk.type == MessageChunkType.done && chunk.message != null) {
              chunkMap['text'] = chunk.message!.text;
            }
            await _emitAskAiChunk(chunkMap, lease, generation);
          }
        } on ClientApiFailure catch (failure) {
          if (!_canCommit(lease, generation)) return;
          await _emitAskAiChunk({'type': 'failure', 'failureKind': failure.kind.name}, lease, generation);
        } catch (_) {
          if (!_canCommit(lease, generation)) return;
          await _emitAskAiChunk(
            {'type': 'failure', 'failureKind': ClientApiFailureKind.unavailable.name},
            lease,
            generation,
          );
        } finally {
          lease.close();
        }
        break;
      default:
        throw PlatformException(code: 'Unimplemented', details: 'Method ${call.method} not implemented.');
    }
  }
}
