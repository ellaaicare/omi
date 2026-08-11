import 'dart:convert';

import 'package:collection/collection.dart';
import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/schema/app.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/memory.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/backend/schema/person.dart';
import 'package:omi/models/custom_stt_config.dart';
import 'package:omi/models/stt_provider.dart';
import 'package:omi/utils/ella_pilot_locale_policy.dart';
import 'package:omi/utils/logger.dart';

class SharedPreferencesUtil {
  static final SharedPreferencesUtil _instance = SharedPreferencesUtil._internal();
  static SharedPreferences? _preferences;
  static const Duration aiConsentServerVerificationTtl = Duration(minutes: 5);
  static String _verifiedAiConsentUid = '';
  static String _verifiedAiConsentReceiptId = '';
  static String _verifiedAiConsentPolicyVersion = '';
  static String _verifiedAiConsentProcessorSetHash = '';
  static String _verifiedAiConsentProfileBindingId = '';
  static String _verifiedAiConsentScopeVersion = '';
  static String _verifiedAiConsentScopeHash = '';
  static DateTime? _verifiedAiConsentAt;
  static int _aiConsentAuthorityGeneration = 0;
  static final ValueNotifier<int> _aiConsentAuthorityChanges = ValueNotifier<int>(0);
  static String _verifiedEllaProvisioningUid = '';
  static int _verifiedEllaProvisioningBindingRevision = 0;
  static String _verifiedEllaProvisioningPolicyRevision = '';
  static int _verifiedEllaProvisioningAuthorityGeneration = -1;

  static const bool isPublicBuild = bool.fromEnvironment('ELLA_PUBLIC_BUILD');
  static const bool isTodayDesignPreviewConfigured = bool.fromEnvironment('ELLA_TODAY_DESIGN_PREVIEW');
  static const bool isTodayDesignPreviewEnabled = !isPublicBuild && isTodayDesignPreviewConfigured;
  static const String currentAiConsentContractVersion = 'ai-data-processors-v8';
  static const String currentAiConsentProcessorSetHash =
      'sha256:d06b3056e06f092557d2d0e9add6ca04a515dabe7f1b6dc948c3bedbd1a3016d';
  static const String currentAiConsentScopeVersion = 'managed-cloud-internal-pilot-v2';
  static const String currentAiConsentScopeHash =
      'sha256:2878e09958faadb799af99a8975736ce63010dd1d682cf944f60743a4faf92e5';
  static const String currentAiConsentReceiptPrefix = 'aicr_';

  factory SharedPreferencesUtil() {
    return _instance;
  }

  SharedPreferencesUtil._internal();

  String get deviceIdHash => _preferences?.getString('deviceIdHash') ?? '';
  set deviceIdHash(String value) => _preferences?.setString('deviceIdHash', value);

  static Future<void> init() async {
    _preferences = await SharedPreferences.getInstance();
    clearAiConsentServerVerification();
    _clearEllaProvisioningServerVerification();
  }

  set uid(String value) {
    if (value != uid) _invalidateAiConsentAuthority();
    saveString('uid', value);
  }

  String get uid => getString('uid');

  int get aiConsentAuthorityGeneration => _aiConsentAuthorityGeneration;

  static ValueListenable<int> get aiConsentAuthorityChanges => _aiConsentAuthorityChanges;

  //-------------------------------- Device ----------------------------------//

  bool? get hasOmiDevice => _preferences?.getBool('hasOmiDevice');

  set hasOmiDevice(bool? value) {
    if (value != null) {
      _preferences?.setBool('hasOmiDevice', value);
    } else {
      _preferences?.remove('hasOmiDevice');
    }
  }

  bool get hasPersonaCreated => getBool('hasPersonaCreated');

  set hasPersonaCreated(bool value) => saveBool('hasPersonaCreated', value);

  String? get verifiedPersonaId => getString('verifiedPersonaId');

  set verifiedPersonaId(String? value) {
    if (value != verifiedPersonaId) _invalidateAiConsentAuthority();
    if (value != null) {
      _preferences?.setString('verifiedPersonaId', value);
    } else {
      _preferences?.remove('verifiedPersonaId');
    }
  }

  set btDevice(BtDevice value) {
    saveString('btDevice', jsonEncode(value.toJson()));
  }

  Future<void> btDeviceSet(BtDevice value) async {
    await saveString('btDevice', jsonEncode(value.toJson()));
  }

  BtDevice get btDevice {
    final String device = getString('btDevice');
    if (device.isEmpty) return BtDevice(id: '', name: '', type: DeviceType.omi, rssi: 0);
    return BtDevice.fromJson(jsonDecode(device));
  }

  set deviceName(String value) => saveString('deviceName', value);

  String get deviceName => getString('deviceName');

  bool get deviceIsV2 => getBool('deviceIsV2');

  set deviceIsV2(bool value) => saveBool('deviceIsV2', value);

  // Double tap behavior: 0 = end conversation (default), 1 = pause/mute, 2 = star ongoing conversation
  int get doubleTapAction => getInt('doubleTapAction');

  set doubleTapAction(int value) => saveInt('doubleTapAction', value);

  // Keep backward compatibility
  bool get doubleTapPausesMuting => doubleTapAction == 1;

  set doubleTapPausesMuting(bool value) => doubleTapAction = value ? 1 : 0;

  // Custom STT configuration
  CustomSttConfig get customSttConfig {
    final configJson = getString('customSttConfig');
    if (configJson.isEmpty) return CustomSttConfig.defaultConfig;
    try {
      return CustomSttConfig.fromJson(jsonDecode(configJson));
    } catch (e, stack) {
      Logger.debug('Error parsing customSttConfig: $e');
      Logger.debug('Stack: $stack');
      return CustomSttConfig.defaultConfig;
    }
  }

  Future<bool> saveCustomSttConfig(CustomSttConfig value) async {
    return await saveString('customSttConfig', jsonEncode(value.toJson()));
  }

  bool get useCustomStt => customSttConfig.isEnabled;

  // Per-provider config storage
  CustomSttConfig? getConfigForProvider(SttProvider provider) {
    final json = getString('sttConfig_${provider.name}');
    if (json.isEmpty) return null;
    try {
      return CustomSttConfig.fromJson(jsonDecode(json));
    } catch (e) {
      Logger.debug('Error loading config for ${provider.name}: $e');
      return null;
    }
  }

  Future<bool> saveConfigForProvider(SttProvider provider, CustomSttConfig config) {
    return saveString('sttConfig_${provider.name}', jsonEncode(config.toJson()));
  }

  //----------------------------- Permissions ---------------------------------//

  set notificationsEnabled(bool value) => saveBool('notificationsEnabled', value);

  bool get notificationsEnabled => getBool('notificationsEnabled');

  set locationEnabled(bool value) => saveBool('locationEnabled', value);

  bool get locationEnabled => getBool('locationEnabled');

  //---------------------- Developer Settings ---------------------------------//

  String get webhookOnConversationCreated => getString('webhookOnConversationCreated');

  set webhookOnConversationCreated(String value) => saveString('webhookOnConversationCreated', value);

  String get webhookOnTranscriptReceived => getString('webhookOnTranscriptReceived');

  set webhookOnTranscriptReceived(String value) => saveString('webhookOnTranscriptReceived', value);

  String get webhookAudioBytes => getString('webhookAudioBytes');

  set webhookAudioBytes(String value) => saveString('webhookAudioBytes', value);

  String get webhookAudioBytesDelay => getString('webhookAudioBytesDelay');

  set webhookDaySummary(String value) => saveString('webhookDaySummary', value);

  String get webhookDaySummary => getString('webhookDaySummary');

  set webhookAudioBytesDelay(String value) => saveString('webhookAudioBytesDelay', value);

  set devModeJoanFollowUpEnabled(bool value) => saveBool('devModeJoanFollowUpEnabled', value);

  bool get devModeJoanFollowUpEnabled => getBool('devModeJoanFollowUpEnabled');

  set transcriptionDiagnosticEnabled(bool value) => saveBool('transcriptionDiagnosticEnabled', value);

  bool get transcriptionDiagnosticEnabled => getBool('transcriptionDiagnosticEnabled');

  set autoCreateSpeakersEnabled(bool value) => saveBool('autoCreateSpeakersEnabled', value);

  bool get autoCreateSpeakersEnabled => getBool('autoCreateSpeakersEnabled', defaultValue: true);

  // Goal tracker widget on homepage - default is true (experimental feature)
  set showGoalTrackerEnabled(bool value) => saveBool('showGoalTrackerEnabled', value);

  bool get showGoalTrackerEnabled => getBool('showGoalTrackerEnabled', defaultValue: true);

  // Daily reflection notification at 9 PM - default is true (enabled)
  set dailyReflectionEnabled(bool value) => saveBool('dailyReflectionEnabled', value);

  bool get dailyReflectionEnabled => getBool('dailyReflectionEnabled', defaultValue: true);

  set demoMode(bool value) => saveBool('demoMode', isPublicBuild ? false : value);

  bool get demoMode => !isPublicBuild && (isTodayDesignPreviewEnabled || getBool('demoMode', defaultValue: false));

  set publicMode(bool value) {
    if (!isPublicBuild) saveBool('publicMode', value);
  }

  bool get publicMode => isPublicBuild || getBool('publicMode', defaultValue: false);

  set aiConsentAccepted(bool value) => saveBool('aiConsentAccepted', value);

  bool get aiConsentAccepted => hasCurrentAiConsentAuthority();

  /// Returns the durable receipt for the current account and bundled contract
  /// without treating it as live data authority. Callers must still perform a
  /// fresh server verification before any protected capture or egress.
  String get persistedAiConsentReceiptIdForCurrentAccount {
    if (isEllaInternalPilotEnabled && !isEllaInternalPilotLocaleSupported(getString('app_locale'))) {
      return '';
    }
    final receiptId = aiConsentReceiptId;
    final persistedGrant = getBool('aiConsentAccepted', defaultValue: false) &&
        aiConsentContractVersion == currentAiConsentContractVersion &&
        aiConsentProcessorSetHash == currentAiConsentProcessorSetHash &&
        uid.isNotEmpty &&
        receiptId.startsWith(currentAiConsentReceiptPrefix) &&
        aiConsentReceiptUid == uid &&
        aiConsentProfileBindingId.isNotEmpty &&
        aiConsentScopeVersion == currentAiConsentScopeVersion &&
        aiConsentScopeHash == currentAiConsentScopeHash &&
        DateTime.tryParse(aiConsentServerDecidedAt) != null;
    return persistedGrant ? receiptId : '';
  }

  bool hasCurrentAiConsentAuthority({bool enforceEnglishPilotLocale = isEllaInternalPilotEnabled}) {
    if (enforceEnglishPilotLocale && !isEllaInternalPilotLocaleSupported(getString('app_locale'))) {
      return false;
    }
    final accepted = getBool('aiConsentAccepted', defaultValue: false) &&
        aiConsentContractVersion == currentAiConsentContractVersion &&
        aiConsentProcessorSetHash == currentAiConsentProcessorSetHash;
    if (!accepted ||
        uid.isEmpty ||
        !aiConsentReceiptId.startsWith(currentAiConsentReceiptPrefix) ||
        aiConsentReceiptUid != uid ||
        aiConsentProfileBindingId.isEmpty ||
        aiConsentScopeVersion != currentAiConsentScopeVersion ||
        aiConsentScopeHash != currentAiConsentScopeHash ||
        DateTime.tryParse(aiConsentServerDecidedAt) == null) {
      return false;
    }
    final verifiedAt = _verifiedAiConsentAt;
    return verifiedAt != null &&
        DateTime.now().difference(verifiedAt) <= aiConsentServerVerificationTtl &&
        _verifiedAiConsentUid == uid &&
        _verifiedAiConsentReceiptId == aiConsentReceiptId &&
        _verifiedAiConsentPolicyVersion == currentAiConsentContractVersion &&
        _verifiedAiConsentProcessorSetHash == currentAiConsentProcessorSetHash &&
        _verifiedAiConsentProfileBindingId == aiConsentProfileBindingId &&
        _verifiedAiConsentScopeVersion == currentAiConsentScopeVersion &&
        _verifiedAiConsentScopeHash == currentAiConsentScopeHash;
  }

  Duration? get aiConsentServerVerificationRemaining {
    final verifiedAt = _verifiedAiConsentAt;
    if (verifiedAt == null ||
        _verifiedAiConsentUid != uid ||
        _verifiedAiConsentReceiptId != aiConsentReceiptId ||
        _verifiedAiConsentPolicyVersion != currentAiConsentContractVersion ||
        _verifiedAiConsentProcessorSetHash != currentAiConsentProcessorSetHash ||
        _verifiedAiConsentProfileBindingId != aiConsentProfileBindingId ||
        _verifiedAiConsentScopeVersion != currentAiConsentScopeVersion ||
        _verifiedAiConsentScopeHash != currentAiConsentScopeHash) {
      return null;
    }
    final remaining = aiConsentServerVerificationTtl - DateTime.now().difference(verifiedAt);
    return remaining.isNegative ? Duration.zero : remaining;
  }

  set aiConsentAcceptedAt(String value) => saveString('aiConsentAcceptedAt', value);

  String get aiConsentAcceptedAt => getString('aiConsentAcceptedAt');

  String get aiConsentReceiptId => getString('aiConsentReceiptId');

  String get aiConsentReceiptUid => getString('aiConsentReceiptUid');

  String get aiConsentContractVersion => getString('aiConsentContractVersion');

  String get aiConsentProcessorSetHash => getString('aiConsentProcessorSetHash');

  String get aiConsentClientVersion => getString('aiConsentClientVersion');

  String get aiConsentLocale => getString('aiConsentLocale');

  String get aiConsentProfileBindingId => getString('aiConsentProfileBindingId');

  String get aiConsentScopeVersion => getString('aiConsentScopeVersion');

  String get aiConsentScopeHash => getString('aiConsentScopeHash');

  String get aiConsentServerDecidedAt => getString('aiConsentServerDecidedAt');

  String get aiConsentDeferredVersion => getString('aiConsentDeferredVersion');

  bool get isCurrentAiConsentDeferred => aiConsentDeferredVersion == currentAiConsentContractVersion;

  bool hasAccountBoundAiConsent(String uid) =>
      uid.isNotEmpty &&
      aiConsentAccepted &&
      aiConsentReceiptId.startsWith(currentAiConsentReceiptPrefix) &&
      aiConsentReceiptUid == uid &&
      aiConsentProfileBindingId.isNotEmpty;

  bool hasPriorAccountBoundAiConsent(String uid) =>
      uid.isNotEmpty &&
      getBool('aiConsentAccepted', defaultValue: false) &&
      aiConsentContractVersion.isNotEmpty &&
      aiConsentContractVersion != currentAiConsentContractVersion &&
      aiConsentReceiptId.isNotEmpty &&
      aiConsentReceiptUid == uid;

  void markAiConsentServerVerified({
    required String uid,
    required String receiptId,
    required String policyVersion,
    required String processorSetHash,
    required String profileBindingId,
    required String scopeVersion,
    required String scopeHash,
    DateTime? verifiedAt,
  }) {
    if (uid.isEmpty ||
        !receiptId.startsWith(currentAiConsentReceiptPrefix) ||
        policyVersion != currentAiConsentContractVersion ||
        processorSetHash != currentAiConsentProcessorSetHash ||
        profileBindingId.isEmpty ||
        scopeVersion != currentAiConsentScopeVersion ||
        scopeHash != currentAiConsentScopeHash) {
      clearAiConsentServerVerification();
      return;
    }
    _verifiedAiConsentUid = uid;
    _verifiedAiConsentReceiptId = receiptId;
    _verifiedAiConsentPolicyVersion = policyVersion;
    _verifiedAiConsentProcessorSetHash = processorSetHash;
    _verifiedAiConsentProfileBindingId = profileBindingId;
    _verifiedAiConsentScopeVersion = scopeVersion;
    _verifiedAiConsentScopeHash = scopeHash;
    _verifiedAiConsentAt = verifiedAt ?? DateTime.now();
  }

  static void clearAiConsentServerVerification() {
    _verifiedAiConsentUid = '';
    _verifiedAiConsentReceiptId = '';
    _verifiedAiConsentPolicyVersion = '';
    _verifiedAiConsentProcessorSetHash = '';
    _verifiedAiConsentProfileBindingId = '';
    _verifiedAiConsentScopeVersion = '';
    _verifiedAiConsentScopeHash = '';
    _verifiedAiConsentAt = null;
  }

  static void _invalidateAiConsentAuthority() {
    _aiConsentAuthorityGeneration++;
    clearAiConsentServerVerification();
    _clearEllaProvisioningServerVerification();
    _aiConsentAuthorityChanges.value = _aiConsentAuthorityGeneration;
  }

  /// Invalidates every delayed account operation before Firebase identity is
  /// allowed to change. This is intentionally synchronous so no in-flight
  /// result can commit while transition quiescence is awaiting service stops.
  void invalidateAccountAuthorityForTransition() => _invalidateAiConsentAuthority();

  void acceptAiConsent({
    String receiptId = '',
    String uid = '',
    String clientVersion = '',
    String locale = '',
    String profileBindingId = '',
    String serverDecidedAt = '',
  }) {
    final hasAccountBoundReceipt = receiptId.startsWith(currentAiConsentReceiptPrefix) && uid.isNotEmpty;
    final nextReceiptId = hasAccountBoundReceipt ? receiptId : '';
    final nextReceiptUid = hasAccountBoundReceipt ? uid : '';
    if (nextReceiptUid != aiConsentReceiptUid ||
        profileBindingId != aiConsentProfileBindingId ||
        nextReceiptId != aiConsentReceiptId) {
      _invalidateAiConsentAuthority();
    }
    aiConsentAccepted = true;
    aiConsentAcceptedAt = DateTime.now().toUtc().toIso8601String();
    saveString('aiConsentContractVersion', currentAiConsentContractVersion);
    saveString('aiConsentProcessorSetHash', currentAiConsentProcessorSetHash);
    saveString('aiConsentClientVersion', clientVersion);
    saveString('aiConsentLocale', locale);
    saveString('aiConsentProfileBindingId', profileBindingId);
    saveString('aiConsentScopeVersion', currentAiConsentScopeVersion);
    saveString('aiConsentScopeHash', currentAiConsentScopeHash);
    saveString('aiConsentServerDecidedAt', serverDecidedAt);
    remove('aiConsentDeferredVersion');
    if (hasAccountBoundReceipt) {
      saveString('aiConsentReceiptId', nextReceiptId);
      saveString('aiConsentReceiptUid', nextReceiptUid);
    } else {
      remove('aiConsentReceiptId');
      remove('aiConsentReceiptUid');
    }
  }

  void deferAiConsent() {
    declineAiConsent();
    saveString('aiConsentDeferredVersion', currentAiConsentContractVersion);
  }

  void declineAiConsent() {
    _invalidateAiConsentAuthority();
    aiConsentAccepted = false;
    remove('aiConsentAcceptedAt');
    remove('aiConsentReceiptId');
    remove('aiConsentReceiptUid');
    remove('aiConsentContractVersion');
    remove('aiConsentProcessorSetHash');
    remove('aiConsentClientVersion');
    remove('aiConsentLocale');
    remove('aiConsentProfileBindingId');
    remove('aiConsentScopeVersion');
    remove('aiConsentScopeHash');
    remove('aiConsentServerDecidedAt');
    remove('aiConsentDeferredVersion');
  }

  // Notification frequency (0-5): 0 = off, 5 = most frequent. Default is 0 (disabled)
  set notificationFrequency(int value) => saveInt('notificationFrequency', value);

  int get notificationFrequency => getInt('notificationFrequency', defaultValue: 0);

  // Task category order for drag-and-drop sorting persistence
  // Format: { "today": ["id1", "id2"], "tomorrow": ["id3"] }
  set taskCategoryOrder(Map<String, List<String>> value) {
    final encoded = jsonEncode(value);
    saveString('taskCategoryOrder', encoded);
  }

  Map<String, List<String>> get taskCategoryOrder {
    final encoded = getString('taskCategoryOrder');
    if (encoded.isEmpty) return {};
    try {
      final decoded = jsonDecode(encoded) as Map<String, dynamic>;
      return decoded.map((key, value) => MapEntry(key, (value as List).cast<String>()));
    } catch (e) {
      return {};
    }
  }

  // Task -> goal mapping (local UI state)
  // Format: { "taskId": "goalId" }
  set taskGoalLinks(Map<String, String> value) {
    final encoded = jsonEncode(value);
    saveString('taskGoalLinks', encoded);
  }

  Map<String, String> get taskGoalLinks {
    final encoded = getString('taskGoalLinks');
    if (encoded.isEmpty) return {};
    try {
      final decoded = jsonDecode(encoded) as Map<String, dynamic>;
      return decoded.map((key, value) => MapEntry(key, value.toString()));
    } catch (e) {
      return {};
    }
  }

  // Wrapped 2025 - track if user has viewed their wrapped
  set hasViewedWrapped2025(bool value) => saveBool('hasViewedWrapped2025', value);

  bool get hasViewedWrapped2025 => getBool('hasViewedWrapped2025', defaultValue: false);

  set conversationEventsToggled(bool value) => saveBool('conversationEventsToggled', value);

  bool get conversationEventsToggled => getBool('conversationEventsToggled');

  set transcriptsToggled(bool value) => saveBool('transcriptsToggled', value);

  bool get transcriptsToggled => getBool('transcriptsToggled');

  set audioBytesToggled(bool value) => saveBool('audioBytesToggled', value);

  bool get audioBytesToggled => getBool('audioBytesToggled');

  set daySummaryToggled(bool value) => saveBool('daySummaryToggled', value);

  bool get daySummaryToggled => getBool('daySummaryToggled');

  bool get showSummarizeConfirmation => getBool('showSummarizeConfirmation', defaultValue: true);

  set showSummarizeConfirmation(bool value) => saveBool('showSummarizeConfirmation', value);

  bool get showSubmitAppConfirmation => getBool('showSubmitAppConfirmation', defaultValue: true);

  set showSubmitAppConfirmation(bool value) => saveBool('showSubmitAppConfirmation', value);

  bool get showInstallAppConfirmation => getBool('showInstallAppConfirmation', defaultValue: true);

  set showInstallAppConfirmation(bool value) => saveBool('showInstallAppConfirmation', value);

  bool get showFirmwareUpdateDialog => getBool('v2/showFirmwareUpdateDialog', defaultValue: true);

  set showFirmwareUpdateDialog(bool value) => saveBool('v2/showFirmwareUpdateDialog', value);

  String get otaWifiSsid => getString('otaWifiSsid', defaultValue: '');
  set otaWifiSsid(String value) => saveString('otaWifiSsid', value);

  String get otaWifiPassword => getString('otaWifiPassword', defaultValue: '');
  set otaWifiPassword(String value) => saveString('otaWifiPassword', value);

  int get conversationSilenceDuration => getInt('conversationSilenceDuration', defaultValue: 120);

  set conversationSilenceDuration(int value) => saveInt('conversationSilenceDuration', value);

  String get transcriptionModel => getString('transcriptionModel3', defaultValue: 'soniox');

  set transcriptionModel(String value) => saveString('transcriptionModel3', value);

  bool get onboardingCompleted => getBool('onboardingCompleted');

  set onboardingCompleted(bool value) => saveBool('onboardingCompleted', value);

  String gptCompletionCache(String key) => getString('gptCompletionCache:$key');

  setGptCompletionCache(String key, String value) => saveString('gptCompletionCache:$key', value);

  bool get optInAnalytics => getBool('optInAnalytics');

  set optInAnalytics(bool value) => saveBool('optInAnalytics', value);

  bool get optInEmotionalFeedback => getBool('optInEmotionalFeedback');

  set optInEmotionalFeedback(bool value) => saveBool('optInEmotionalFeedback', value);

  bool get devModeEnabled => getBool('devModeEnabled');

  set devModeEnabled(bool value) => saveBool('devModeEnabled', value);

  // Auto-recording feature (macOS only)
  bool get autoRecordingEnabled => getBool('autoRecordingEnabled', defaultValue: true);

  set autoRecordingEnabled(bool value) => saveBool('autoRecordingEnabled', value);

  // Developer Diagnostics
  bool get devLogsToFileEnabled => getBool('devLogsToFileEnabled');

  set devLogsToFileEnabled(bool value) => saveBool('devLogsToFileEnabled', value);

  bool get permissionStoreRecordingsEnabled => getBool('permissionStoreRecordingsEnabled');

  set permissionStoreRecordingsEnabled(bool value) => saveBool('permissionStoreRecordingsEnabled', value);

  bool get unlimitedLocalStorageEnabled => getBool('unlimitedLocalStorageEnabled');

  set unlimitedLocalStorageEnabled(bool value) => saveBool('unlimitedLocalStorageEnabled', value);

  // Preferred sync method for SD card files: 'wifi' (Fast Transfer) or 'ble' (Bluetooth)
  String get preferredSyncMethod => getString('preferredSyncMethod', defaultValue: 'ble');

  set preferredSyncMethod(String value) => saveString('preferredSyncMethod', value);

  // Whether the user has been shown the Fast Transfer explanation dialog
  bool get hasSeenFastTransferIntro => getBool('hasSeenFastTransferIntro');

  set hasSeenFastTransferIntro(bool value) => saveBool('hasSeenFastTransferIntro', value);

  bool get hasSpeakerProfile => getBool('hasSpeakerProfile');

  set hasSpeakerProfile(bool value) => saveBool('hasSpeakerProfile', value);

  bool get showDiscardedMemories => getBool('showDiscardedMemories', defaultValue: false);

  set showDiscardedMemories(bool value) => saveBool('showDiscardedMemories', value);

  // Show short conversations - default is false (hidden)
  bool get showShortConversations => getBool('showShortConversations', defaultValue: false);

  set showShortConversations(bool value) => saveBool('showShortConversations', value);

  // Short conversation threshold in seconds - default is 60 (1 minute)
  // Options: 60 (1 min), 120 (2 min), 180 (3 min), 240 (4 min), 300 (5 min)
  int get shortConversationThreshold => getInt('v2/shortConversationThreshold', defaultValue: 0);

  set shortConversationThreshold(int value) => saveInt('v2/shortConversationThreshold', value);

  // Transcription settings (cached for fast preload)
  bool get cachedSingleLanguageMode => getBool('cachedSingleLanguageMode');

  set cachedSingleLanguageMode(bool value) => saveBool('cachedSingleLanguageMode', value);

  List<String> get cachedTranscriptionVocabulary => getStringList('cachedTranscriptionVocabulary');

  set cachedTranscriptionVocabulary(List<String> value) => saveStringList('cachedTranscriptionVocabulary', value);

  // User primary language preferences
  String get userPrimaryLanguage => getString('userPrimaryLanguage');

  set userPrimaryLanguage(String value) => saveString('userPrimaryLanguage', value);

  bool get hasSetPrimaryLanguage => getBool('hasSetPrimaryLanguage');

  set hasSetPrimaryLanguage(bool value) => saveBool('hasSetPrimaryLanguage', value);

  int get currentStorageBytes => getInt('currentStorageBytes');

  set currentStorageBytes(int value) => saveInt('currentStorageBytes', value);

  int get previousStorageBytes => getInt('previousStorageBytes');

  set previousStorageBytes(int value) => saveInt('previousStorageBytes', value);

  int get enabledAppsCount => appsList.where((element) => element.enabled).length;

  int get enabledAppsIntegrationsCount =>
      appsList.where((element) => element.enabled && element.worksExternally()).length;

  bool get showConversationDeleteConfirmation => getBool('showConversationDeleteConfirmation', defaultValue: true);

  set showConversationDeleteConfirmation(bool value) => saveBool("showConversationDeleteConfirmation", value);

  bool get showActionItemDeleteConfirmation => getBool('showActionItemDeleteConfirmation', defaultValue: true);

  set showActionItemDeleteConfirmation(bool value) => saveBool('showActionItemDeleteConfirmation', value);

  bool get showGetOmiCard => getBool('showGetOmiCard', defaultValue: true);

  set showGetOmiCard(bool value) => saveBool('showGetOmiCard', value);

  List<App> get appsList {
    final apps = getStringList('appsList');
    return App.fromJsonList(apps.map((e) => jsonDecode(e)).toList());
  }

  set appsList(List<App> value) {
    final List<String> apps = value.map((e) => jsonEncode(e.toJson())).toList();
    saveStringList('appsList', apps);
  }

  enableApp(String value) {
    final List<App> apps = appsList;
    App? app = apps.firstWhereOrNull((element) => element.id == value);
    if (app != null) {
      app.enabled = true;
      appsList = apps;
    }
  }

  disableApp(String value) {
    final List<App> apps = appsList;
    App? app = apps.firstWhereOrNull((element) => element.id == value);
    if (app != null) {
      app.enabled = false;
      appsList = apps;
    }
  }

  String get selectedChatAppId => getString('selectedChatAppId2', defaultValue: 'no_selected');

  set selectedChatAppId(String value) => saveString('selectedChatAppId2', value);

  String get lastUsedSummarizationAppId => getString('lastUsedSummarizationAppId');

  set lastUsedSummarizationAppId(String value) => saveString('lastUsedSummarizationAppId', value);

  String get preferredSummarizationAppId => getString('preferredSummarizationAppId');

  set preferredSummarizationAppId(String value) => saveString('preferredSummarizationAppId', value);

  List<ServerConversation> get cachedConversations {
    // Only return cache if it belongs to the current user
    final cachedUid = getString('cachedConversationsUid');
    if (uid.isEmpty || cachedUid != uid) {
      // Unowned legacy cache and cache from another account are both unsafe.
      saveStringList('cachedConversations', []);
      saveString('cachedConversationsUid', '');
      return [];
    }
    if (getBool('migratedMemories')) {
      final cachedMemories = getStringList('cachedMemories');
      if (cachedMemories.isNotEmpty) {
        final conversations = cachedMemories.map((e) => ServerConversation.fromJson(jsonDecode(e))).toList();
        cachedConversations = conversations;
        saveBool('migratedMemories', true);
      }
    }
    final conversations = getStringList('cachedConversations');
    return conversations.map((e) => ServerConversation.fromJson(jsonDecode(e))).toList();
  }

  set cachedConversations(List<ServerConversation> value) {
    final List<String> conversations = value.map((e) => jsonEncode(e.toJson())).toList();
    saveStringList('cachedConversations', conversations);
    saveString('cachedConversationsUid', uid);
  }

  List<ServerMessage> get cachedMessages {
    // Only return cache if it belongs to the current user
    final cachedUid = getString('cachedMessagesUid');
    if (uid.isEmpty || cachedUid != uid) {
      saveStringList('cachedMessages', []);
      saveString('cachedMessagesUid', '');
      return [];
    }
    final messages = getStringList('cachedMessages');
    return messages.map((e) => ServerMessage.fromJson(jsonDecode(e))).toList();
  }

  set cachedMessages(List<ServerMessage> value) {
    final List<String> messages = value.map((e) => jsonEncode(e.toJson())).toList();
    saveStringList('cachedMessages', messages);
    saveString('cachedMessagesUid', uid);
  }

  void clearUserCaches() {
    saveStringList('cachedConversations', []);
    saveStringList('cachedMessages', []);
    saveStringList('cachedMemories', []);
    saveString('cachedConversationsUid', '');
    saveString('cachedMessagesUid', '');
  }

  String? _accountScopedKey(String base) {
    final currentUid = uid;
    return currentUid.isEmpty ? null : '$base:$currentUid';
  }

  Future<void> quarantineLegacyAccountCaches() async {
    for (final key in const ['pendingMemories', 'cachedPeople']) {
      final values = getStringList(key);
      if (values.isNotEmpty) {
        await saveStringList('ellaLegacyUnownedCache:$key', values);
        await remove(key);
      }
    }
    for (final key in const [
      'modifiedConversationDetails',
      'emergencyContactName',
      'emergencyContactPhone',
      'pendingEmergency',
    ]) {
      final value = getString(key);
      if (value.isNotEmpty) {
        await saveString('ellaLegacyUnownedCache:$key', value);
        await remove(key);
      }
    }
  }

  void clearDemoStateForAccountBuild() {
    demoMode = false;
    publicMode = false;

    final hasDemoConversationCache = getStringList('cachedConversations').any((value) => value.contains('"id":"demo-'));
    final hasDemoMemoryCache = getStringList('cachedMemories').any((value) => value.contains('"id":"demo-'));
    final hasDemoMessageCache = getStringList('cachedMessages').any((value) => value.contains('"id":"demo-chat-'));
    if (hasDemoConversationCache || hasDemoMemoryCache || hasDemoMessageCache) {
      clearUserCaches();
    }
  }

  String _ellaProvisioningReceiptKey(String uid) => 'ellaProvisioningReceipt:$uid';

  Map<String, dynamic>? getEllaProvisioningReceipt(String uid) {
    final encoded = getString(_ellaProvisioningReceiptKey(uid));
    if (encoded.isEmpty) return null;
    try {
      final decoded = jsonDecode(encoded);
      return decoded is Map<String, dynamic> ? decoded : null;
    } catch (_) {
      return null;
    }
  }

  Future<void> saveEllaProvisioningReceipt(String uid, Map<String, dynamic> receipt) async {
    await saveString(_ellaProvisioningReceiptKey(uid), jsonEncode(receipt));
  }

  String _ellaProvisioningVerifiedAtKey(String uid) => 'ellaProvisioningVerifiedAt:$uid';

  Future<void> markEllaProvisioningVerified(String uid, {DateTime? at}) async {
    final receipt = getEllaProvisioningReceipt(uid);
    final state = receipt?['state']?.toString().toLowerCase();
    final bindingState = receipt?['binding_state']?.toString().toLowerCase();
    final bindingRevision = receipt?['binding_revision'];
    final policyRevision = receipt?['effective_policy_revision']?.toString() ?? '';
    if (uid.isEmpty ||
        uid != this.uid ||
        state != 'ready' ||
        bindingState != 'active' ||
        bindingRevision is! int ||
        bindingRevision <= 0 ||
        policyRevision.isEmpty) {
      _clearEllaProvisioningServerVerification();
      return;
    }
    _verifiedEllaProvisioningUid = uid;
    _verifiedEllaProvisioningBindingRevision = bindingRevision;
    _verifiedEllaProvisioningPolicyRevision = policyRevision;
    _verifiedEllaProvisioningAuthorityGeneration = _aiConsentAuthorityGeneration;
    await saveString(_ellaProvisioningVerifiedAtKey(uid), (at ?? DateTime.now()).toUtc().toIso8601String());
  }

  bool hasCurrentEllaProvisioningAuthority({required String uid, required int bindingRevision}) {
    if (uid.isEmpty || uid != this.uid || bindingRevision <= 0) return false;
    final receipt = getEllaProvisioningReceipt(uid);
    return _verifiedEllaProvisioningUid == uid &&
        _verifiedEllaProvisioningBindingRevision == bindingRevision &&
        _verifiedEllaProvisioningPolicyRevision.isNotEmpty &&
        _verifiedEllaProvisioningAuthorityGeneration == _aiConsentAuthorityGeneration &&
        receipt?['state']?.toString().toLowerCase() == 'ready' &&
        receipt?['binding_state']?.toString().toLowerCase() == 'active' &&
        receipt?['binding_revision'] == bindingRevision &&
        receipt?['effective_policy_revision'] == _verifiedEllaProvisioningPolicyRevision;
  }

  static void _clearEllaProvisioningServerVerification() {
    _verifiedEllaProvisioningUid = '';
    _verifiedEllaProvisioningBindingRevision = 0;
    _verifiedEllaProvisioningPolicyRevision = '';
    _verifiedEllaProvisioningAuthorityGeneration = -1;
  }

  DateTime? getEllaProvisioningVerifiedAt(String uid) =>
      DateTime.tryParse(getString(_ellaProvisioningVerifiedAtKey(uid)));

  String get ellaProvisionedVoiceMode {
    final receipt = getEllaProvisioningReceipt(uid);
    final value = receipt?['effective_voice_mode'];
    return value is String ? value : '';
  }

  /// Clears account-scoped state before the authenticated provisioning gate
  /// evaluates a different Firebase user. A cached receipt is never authority;
  /// the gate still requires a fresh server-confirmed ready response.
  Future<void> prepareEllaProvisioningAccount(String newUid) async {
    final previousUid = getString('ellaProvisioningAccountUid');

    if (previousUid == newUid) return;

    _invalidateAiConsentAuthority();
    await quarantineLegacyAccountCaches();

    if (previousUid.isNotEmpty) {
      // Retained users keep compatibility preferences when returning to the
      // same account. They are cleared only on an actual account switch and
      // are never authority for the authenticated provisioning gate.
      for (final key in const [
        'ellaUserId',
        'ellaKey',
        'ellaGatewayUrl',
        'ellaAgentId',
        'ellaGatewayToken',
        'ellaResolvedEndpoint',
      ]) {
        await remove(key);
      }
      await remove(_ellaProvisioningReceiptKey(previousUid));
      // BLE pairing is a convenience binding, not cross-account authority.
      // Never carry the prior account's remembered necklace into a replacement
      // account; the replacement user must pair or select their own device.
      for (final key in const ['btDevice', 'deviceName', 'hasOmiDevice', 'deviceIsV2']) {
        await remove(key);
      }
    }
    await remove(_ellaProvisioningReceiptKey(newUid));
    await remove(_ellaProvisioningVerifiedAtKey(newUid));

    for (final key in const [
      'devTtsProvider',
      'ellaSettingsVoiceModeDirty',
      'ellaSettingsPendingVoiceMode',
      'ellaSettingsLastSyncedVoiceMode',
      'ellaSettingsLastSyncedAt',
      'ellaSettingsLastSyncError',
      'aiConsentAccepted',
      'aiConsentAcceptedAt',
      'aiConsentReceiptId',
      'aiConsentReceiptUid',
      'aiConsentContractVersion',
      'aiConsentProcessorSetHash',
      'aiConsentClientVersion',
      'aiConsentLocale',
      'aiConsentProfileBindingId',
      'aiConsentScopeVersion',
      'aiConsentScopeHash',
      'aiConsentServerDecidedAt',
      'aiConsentDeferredVersion',
    ]) {
      await remove(key);
    }

    demoMode = false;
    publicMode = false;
    clearUserCaches();
    await saveString('ellaProvisioningAccountUid', newUid);
  }

  // Pending memories - memories created offline that need to be synced
  List<Memory> get pendingMemories {
    final key = _accountScopedKey('pendingMemories');
    if (key == null) return [];
    final memories = getStringList(key);
    return memories.map((e) => Memory.fromJson(jsonDecode(e))).toList();
  }

  set pendingMemories(List<Memory> value) {
    final key = _accountScopedKey('pendingMemories');
    if (key == null) return;
    final List<String> memories = value.map((e) => jsonEncode(e.toJson())).toList();
    saveStringList(key, memories);
  }

  void addPendingMemory(Memory memory) {
    final List<Memory> memories = pendingMemories;
    memories.add(memory);
    pendingMemories = memories;
  }

  void removePendingMemory(String memoryId) {
    final List<Memory> memories = pendingMemories;
    memories.removeWhere((m) => m.id == memoryId);
    pendingMemories = memories;
  }

  void clearPendingMemories() {
    final key = _accountScopedKey('pendingMemories');
    if (key != null) saveStringList(key, []);
  }

  List<Person> get cachedPeople {
    final key = _accountScopedKey('cachedPeople');
    if (key == null) return [];
    final people = getStringList(key);
    return people.map((e) => Person.fromJson(jsonDecode(e))).toList();
  }

  Person? getPersonById(String id) {
    return cachedPeople.firstWhereOrNull((element) => element.id == id);
  }

  set cachedPeople(List<Person> value) {
    final key = _accountScopedKey('cachedPeople');
    if (key == null) return;
    final List<String> people = value.map((e) => jsonEncode(e.toJson())).toList();
    saveStringList(key, people);
  }

  addCachedPerson(Person person) {
    final List<Person> people = cachedPeople;
    people.add(person);
    cachedPeople = people;
  }

  removeCachedPerson(String personId) {
    final List<Person> people = cachedPeople;
    Person? person = people.firstWhereOrNull((p) => p.id == personId);
    if (person != null) {
      people.remove(person);
      cachedPeople = people;
    }
  }

  replaceCachedPerson(Person person) {
    final List<Person> people = cachedPeople;
    Person? oldPerson = people.firstWhereOrNull((p) => p.id == person.id);
    if (oldPerson != null) {
      people.remove(oldPerson);
      people.add(person);
      cachedPeople = people;
    }
  }

  ServerConversation? get modifiedConversationDetails {
    final key = _accountScopedKey('modifiedConversationDetails');
    if (key == null) return null;
    final String conversation = getString(key);
    if (conversation.isEmpty) return null;
    return ServerConversation.fromJson(jsonDecode(conversation));
  }

  set modifiedConversationDetails(ServerConversation? value) {
    final key = _accountScopedKey('modifiedConversationDetails');
    if (key != null) saveString(key, value == null ? '' : jsonEncode(value.toJson()));
  }

  set calendarPermissionAlreadyRequested(bool value) => saveBool('calendarPermissionAlreadyRequested', value);

  bool get calendarPermissionAlreadyRequested => getBool('calendarPermissionAlreadyRequested');

  set calendarEnabled(bool value) => saveBool('calendarEnabled', value);

  bool get calendarEnabled => getBool('calendarEnabled');

  set calendarId(String value) => saveString('calendarId', value);

  String get calendarId => getString('calendarId');

  set calendarType(String value) => saveString('calendarType2', value); // auto, manual (only for now)

  String get calendarType => getString('calendarType2', defaultValue: 'manual');

  set calendarIntegrationEnabled(bool value) => saveBool('calendarIntegrationEnabled', value);

  bool get calendarIntegrationEnabled => getBool('calendarIntegrationEnabled');

  // Calendar UI Settings
  set showEventsWithNoParticipants(bool value) => saveBool('showEventsWithNoParticipants', value);

  bool get showEventsWithNoParticipants => getBool('showEventsWithNoParticipants');

  set showMeetingsInMenuBar(bool value) => saveBool('showMeetingsInMenuBar', value);

  bool get showMeetingsInMenuBar => getBool('showMeetingsInMenuBar');

  set enabledCalendarIds(List<String> value) => saveStringList('enabledCalendarIds', value);

  List<String> get enabledCalendarIds => getStringList('enabledCalendarIds');

  //--------------------------------- Auth ------------------------------------//

  String get authToken => getString('authToken');

  set authToken(String value) => saveString('authToken', value);

  int get tokenExpirationTime => getInt('tokenExpirationTime');

  set tokenExpirationTime(int value) => saveInt('tokenExpirationTime', value);

  String get email => getString('email');

  set email(String value) => saveString('email', value);

  String get givenName => getString('givenName');

  set givenName(String value) => saveString('givenName', value);

  String get familyName => getString('familyName');

  set familyName(String value) => saveString('familyName', value);

  String get fullName => '$givenName $familyName'.trim();

  String get phoneNumber => getString('phoneNumber');

  set phoneNumber(String value) => saveString('phoneNumber', value);

  String get foundOmiSource => getString('foundOmiSource');

  set foundOmiSource(String value) => saveString('foundOmiSource', value);

  set locationPermissionRequested(bool value) => saveBool('locationPermissionRequested', value);

  bool get locationPermissionRequested => getBool('locationPermissionRequested');

  //--------------------------- Announcements ---------------------------------//

  // Last known app version - used to detect app upgrades
  // Empty string means fresh install
  String get lastKnownAppVersion => getString('lastKnownAppVersion');

  set lastKnownAppVersion(String value) => saveString('lastKnownAppVersion', value);

  // Last known firmware version - used to detect firmware upgrades
  String get lastKnownFirmwareVersion => getString('lastKnownFirmwareVersion');

  set lastKnownFirmwareVersion(String value) => saveString('lastKnownFirmwareVersion', value);

  // Last time general announcements were checked
  DateTime? get lastAnnouncementCheckTime {
    final str = getString('lastAnnouncementCheckTime');
    if (str.isEmpty) return null;
    return DateTime.tryParse(str);
  }

  set lastAnnouncementCheckTime(DateTime? value) {
    if (value == null) {
      remove('lastAnnouncementCheckTime');
    } else {
      saveString('lastAnnouncementCheckTime', value.toUtc().toIso8601String());
    }
  }

  //------------------------- Ella Dashboard ---------------------------------//

  String get ellaUserId => getString('ellaUserId');
  set ellaUserId(String value) => saveString('ellaUserId', value);

  String get ellaKey => getString('ellaKey');
  set ellaKey(String value) => saveString('ellaKey', value);

  String get ellaGatewayUrl => getString('ellaGatewayUrl');
  set ellaGatewayUrl(String value) => saveString('ellaGatewayUrl', value);

  String get ellaAgentId => getString('ellaAgentId');
  set ellaAgentId(String value) => saveString('ellaAgentId', value);

  String get ellaGatewayToken => getString('ellaGatewayToken');
  set ellaGatewayToken(String value) => saveString('ellaGatewayToken', value);

  //--------------------------- Emergency Contact ----------------------------//

  String get emergencyContactName {
    final key = _accountScopedKey('emergencyContactName');
    return key == null ? '' : getString(key);
  }

  set emergencyContactName(String value) {
    final key = _accountScopedKey('emergencyContactName');
    if (key != null) saveString(key, value);
  }

  String get emergencyContactPhone {
    final key = _accountScopedKey('emergencyContactPhone');
    return key == null ? '' : getString(key);
  }

  set emergencyContactPhone(String value) {
    final key = _accountScopedKey('emergencyContactPhone');
    if (key != null) saveString(key, value);
  }

  String get pendingEmergency {
    final key = _accountScopedKey('pendingEmergency');
    return key == null ? '' : getString(key);
  }

  set pendingEmergency(String value) {
    final key = _accountScopedKey('pendingEmergency');
    if (key != null) saveString(key, value);
  }

  // TTS Provider (dev setting) — elevenlabs | fish-audio-s2 | kokoro
  String get ttsProvider => getString('devTtsProvider', defaultValue: 'elevenlabs');
  set ttsProvider(String value) => saveString('devTtsProvider', value);

  //--------------------------- Setters & Getters -----------------------------//

  String getString(String key, {String defaultValue = ''}) => _preferences?.getString(key) ?? defaultValue;

  int getInt(String key, {int defaultValue = 0}) => _preferences?.getInt(key) ?? defaultValue;

  bool getBool(String key, {bool defaultValue = false}) => _preferences?.getBool(key) ?? defaultValue;

  double getDouble(String key, {double defaultValue = 0.0}) => _preferences?.getDouble(key) ?? defaultValue;

  List<String> getStringList(String key, {List<String> defaultValue = const []}) =>
      _preferences?.getStringList(key) ?? defaultValue;

  Future<bool> saveString(String key, String value) async => await _preferences?.setString(key, value) ?? false;

  Future<bool> saveInt(String key, int value) async => await _preferences?.setInt(key, value) ?? false;

  Future<bool> saveBool(String key, bool value) async => await _preferences?.setBool(key, value) ?? false;

  Future<bool> saveDouble(String key, double value) async => await _preferences?.setDouble(key, value) ?? false;

  Future<bool> saveStringList(String key, List<String> value) async =>
      await _preferences?.setStringList(key, value) ?? false;

  Future<bool> remove(String key) async => await _preferences?.remove(key) ?? false;

  Future<bool> clear() async {
    _invalidateAiConsentAuthority();
    return await _preferences?.clear() ?? false;
  }
}
