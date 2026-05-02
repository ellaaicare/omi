import 'dart:convert';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/ella/models/escalation_policy.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/logger.dart';

/// GET /v1/ella/escalations/policy
///
/// Returns the read-only effective escalation policy for the current user.
/// Uses [makeApiCall] which auto-injects the Firebase Bearer token.
Future<EscalationPolicy?> getEscalationPolicy() async {
  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/ella/escalations/policy',
    method: 'GET',
    headers: {},
    body: '',
  );

  if (response == null || response.statusCode != 200) {
    final code = response?.statusCode ?? 0;
    Logger.debug('EscalationPolicy API failed: $code');
    return null;
  }

  try {
    final json = jsonDecode(response.body) as Map<String, dynamic>;
    return EscalationPolicy.fromJson(json);
  } catch (e) {
    Logger.debug('EscalationPolicy parse error: $e');
    return null;
  }
}
