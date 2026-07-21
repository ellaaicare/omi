import 'package:uuid/uuid.dart';

import 'package:omi/backend/http/api/users.dart' as users_api;
import 'package:omi/backend/preferences.dart';

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
  })  : _transport = transport ?? const EllaAiConsentHttpTransport(),
        _preferences = preferences ?? SharedPreferencesUtil(),
        _receiptIdFactory = receiptIdFactory ?? (() => const Uuid().v4());

  final EllaAiConsentTransport _transport;
  final SharedPreferencesUtil _preferences;
  final String Function() _receiptIdFactory;

  Future<String?> acknowledgePrivateCloudSync({required String uid}) async {
    if (uid.isEmpty) return null;

    final updated = await _transport.setPrivateCloudSync(true);
    if (!updated) return null;

    final confirmed = await _transport.getPrivateCloudSyncEnabled();
    if (!confirmed) return null;

    final receiptId = 'ios-private-cloud-sync:${_receiptIdFactory()}';
    _preferences.acceptAiConsent(receiptId: receiptId, uid: uid);
    return receiptId;
  }
}
