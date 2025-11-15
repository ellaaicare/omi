import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/env/env.dart';

Future<void> saveFcmTokenServer({required String token, required String timeZone}) async {
  debugPrint('🔔 [DEBUG] saveFcmTokenServer called');
  debugPrint('🔔 [DEBUG] URL: ${Env.apiBaseUrl}v1/users/fcm-token');
  debugPrint('🔔 [DEBUG] FCM Token (first 50 chars): ${token.substring(0, token.length > 50 ? 50 : token.length)}...');
  debugPrint('🔔 [DEBUG] Time Zone: $timeZone');

  var response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/users/fcm-token',
    headers: {'Content-Type': 'application/json'},
    method: 'POST',
    body: jsonEncode({'fcm_token': token, 'time_zone': timeZone}),
  );

  debugPrint('🔔 [DEBUG] Response status: ${response?.statusCode}');
  debugPrint('🔔 [DEBUG] Response body: ${response?.body}');

  if (response?.statusCode == 200) {
    debugPrint("✅ Token saved successfully to backend");
  } else {
    debugPrint("❌ Failed to save token. Status: ${response?.statusCode}");
  }
}
