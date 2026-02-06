import 'dart:convert';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/models/emergency.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/logger.dart';

/// POST /v1/ella/emergency
Future<EmergencyResponse> postEmergency({
  required String uid,
  double? latitude,
  double? longitude,
  double? accuracyMeters,
}) async {
  final body = <String, dynamic>{
    'uid': uid,
    'audio_context_seconds': 30,
    'trigger_source': 'manual_button',
  };

  if (latitude != null && longitude != null) {
    body['location'] = {
      'latitude': latitude,
      'longitude': longitude,
      'accuracy_meters': accuracyMeters ?? 0,
    };
  }

  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/ella/emergency',
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: jsonEncode(body),
  );

  if (response == null || response.statusCode != 200) {
    final code = response?.statusCode ?? 0;
    Logger.debug('Emergency API failed: $code');
    throw EmergencyApiException(
      statusCode: code,
      message: 'Failed to trigger emergency',
    );
  }

  return EmergencyResponse.fromJson(jsonDecode(response.body));
}

/// POST /v1/ella/emergency/{emergencyId}/cancel
Future<CancelResponse> postEmergencyCancel(String emergencyId) async {
  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/ella/emergency/$emergencyId/cancel',
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: '',
  );

  if (response == null) {
    throw EmergencyApiException(statusCode: 0, message: 'No network');
  }
  if (response.statusCode == 409) {
    throw EmergencyApiException(statusCode: 409, message: 'Cancel window expired');
  }
  if (response.statusCode != 200) {
    throw EmergencyApiException(statusCode: response.statusCode, message: 'Cancel failed');
  }

  return CancelResponse.fromJson(jsonDecode(response.body));
}

/// Get the current user's UID from shared preferences.
String getCurrentUid() {
  return SharedPreferencesUtil().uid;
}
