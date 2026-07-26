import 'dart:ui';

import 'package:uuid/uuid.dart';

import 'package:omi/backend/http/api/users.dart' as users_api;
import 'package:omi/backend/preferences.dart';
import 'package:omi/utils/platform/platform_manager.dart';

abstract class EllaAiConsentTransport {
  Future<bool> setPrivateCloudSync(bool value);

  Future<bool> getPrivateCloudSyncEnabled();
}

class EllaAiConsentHttpTransport implements EllaAiConsentTransport {
  const EllaAiConsentHttpTransport();

  @override
  Future<bool> setPrivateCloudSync(bool value) => users_api.setPrivateCloudSyncEnabled(value);

  @override
  Future<bool> getPrivateCloudSyncEnabled() => users_api.getPrivateCloudSyncEnabled();
}

class EllaAiConsentService {
  EllaAiConsentService({
    EllaAiConsentTransport? transport,
    SharedPreferencesUtil? preferences,
    String Function()? receiptIdFactory,
    String Function()? clientVersionFactory,
    String Function()? localeFactory,
  })  : _transport = transport ?? const EllaAiConsentHttpTransport(),
        _preferences = preferences ?? SharedPreferencesUtil(),
        _receiptIdFactory = receiptIdFactory ?? (() => const Uuid().v4()),
        _clientVersionFactory = clientVersionFactory ?? (() => PlatformManager.instance.appVersion),
        _localeFactory = localeFactory ?? (() => PlatformDispatcher.instance.locale.toLanguageTag());

  final EllaAiConsentTransport _transport;
  final SharedPreferencesUtil _preferences;
  final String Function() _receiptIdFactory;
  final String Function() _clientVersionFactory;
  final String Function() _localeFactory;

  Future<String?> acknowledgePrivateCloudSync({required String uid}) async {
    if (uid.isEmpty) return null;

    final updated = await _transport.setPrivateCloudSync(true);
    if (!updated) return null;

    final confirmed = await _transport.getPrivateCloudSyncEnabled();
    if (!confirmed) return null;

    final receiptId = '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}${_receiptIdFactory()}';
    _preferences.acceptAiConsent(
      receiptId: receiptId,
      uid: uid,
      clientVersion: _clientVersionFactory(),
      locale: _localeFactory(),
    );
    return receiptId;
  }

  Future<bool> revokePrivateCloudSync() async {
    _preferences.declineAiConsent();
    return _transport.setPrivateCloudSync(false);
  }
}
