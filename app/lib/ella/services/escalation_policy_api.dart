import 'dart:convert';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/ella/models/escalation_policy.dart';
import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/ella/services/ella_service_result.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/logger.dart';

/// GET /v1/ella/escalations/policy
///
/// Returns the read-only effective escalation policy for the current user.
/// Uses [makeApiCall] which auto-injects the Firebase Bearer token.
Future<EllaServiceResult<EscalationPolicy>> getEscalationPolicy() async {
  try {
    final response = await makeApiCall(
      url: '${Env.apiBaseUrl}v1/ella/escalations/policy',
      method: 'GET',
      headers: {},
      body: '',
    );

    if (response == null || response.statusCode != 200) {
      final code = response?.statusCode ?? 0;
      Logger.debug('EscalationPolicy API failed: $code');
      return EllaServiceResult.failure(
        response == null
            ? const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true)
            : ClientApiFailure.fromHttp(statusCode: response.statusCode, body: response.body),
      );
    }

    final json = jsonDecode(response.body) as Map<String, dynamic>;
    return EllaServiceResult.success(EscalationPolicy.fromJson(json));
  } on ClientApiFailure catch (failure) {
    return EllaServiceResult.failure(failure);
  } catch (e) {
    Logger.debug('EscalationPolicy parse error: $e');
    return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.invalidResponse));
  }
}
