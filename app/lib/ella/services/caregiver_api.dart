import 'dart:convert';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/ella/models/caregiver.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/logger.dart';

/// GET /v1/ella/caregivers
Future<List<Caregiver>> getCaregivers() async {
  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/ella/caregivers',
    method: 'GET',
    headers: {},
    body: '',
  );
  if (response == null || response.statusCode != 200) return [];
  final data = jsonDecode(response.body);
  return (data['caregivers'] as List).map((c) => Caregiver.fromJson(c as Map<String, dynamic>)).toList();
}

/// POST /v1/ella/caregivers/invite
Future<InviteResponse> sendCaregiverInvite({
  required String name,
  required String phone,
  String? email,
  required String relationship,
  bool dailySummary = true,
}) async {
  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/ella/caregivers/invite',
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: jsonEncode({
      'name': name,
      'phone': phone,
      if (email != null && email.isNotEmpty) 'email': email,
      'relationship': relationship,
      'permissions': {
        'receive_daily_summary': dailySummary,
        'daily_summary_email': dailySummary,
      },
    }),
  );
  if (response == null || response.statusCode != 201) {
    final code = response?.statusCode ?? 0;
    Logger.debug('Caregiver invite API failed: $code');
    throw CaregiverApiException(
      statusCode: code,
      message: 'Failed to send invite',
    );
  }
  return InviteResponse.fromJson(jsonDecode(response.body));
}

/// DELETE /v1/ella/caregivers/{caregiverId}
Future<void> removeCaregiver(String caregiverId) async {
  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/ella/caregivers/$caregiverId',
    method: 'DELETE',
    headers: {},
    body: '',
  );
  if (response == null || (response.statusCode != 204 && response.statusCode != 200)) {
    final code = response?.statusCode ?? 0;
    throw CaregiverApiException(
      statusCode: code,
      message: 'Failed to remove caregiver',
    );
  }
}

/// PUT /v1/ella/caregivers/{caregiverId}/permissions
Future<void> updateCaregiverPermissions(String caregiverId, {required bool dailySummary}) async {
  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/ella/caregivers/$caregiverId/permissions',
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: jsonEncode({
      'receive_daily_summary': dailySummary,
    }),
  );
  if (response == null || response.statusCode != 200) {
    final code = response?.statusCode ?? 0;
    throw CaregiverApiException(
      statusCode: code,
      message: 'Failed to update permissions',
    );
  }
}

/// POST /v1/ella/caregivers/resend-invite
Future<void> resendInvite({required String uid, required String caregiverId}) async {
  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/ella/caregivers/resend-invite',
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: jsonEncode({'uid': uid, 'caregiver_id': caregiverId}),
  );
  if (response == null || response.statusCode != 200) {
    final code = response?.statusCode ?? 0;
    throw CaregiverApiException(
      statusCode: code,
      message: 'Failed to resend invite',
    );
  }
}

/// PUT /v1/ella/emergency-contact
Future<void> setEmergencyContact(String caregiverId) async {
  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/ella/emergency-contact',
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: jsonEncode({'caregiver_id': caregiverId}),
  );
  if (response == null || response.statusCode != 200) {
    final code = response?.statusCode ?? 0;
    throw CaregiverApiException(
      statusCode: code,
      message: 'Failed to set emergency contact',
    );
  }
}

/// GET /v1/ella/emergency-contact
Future<String?> getEmergencyContactId() async {
  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/ella/emergency-contact',
    method: 'GET',
    headers: {},
    body: '',
  );
  if (response == null || response.statusCode != 200) return null;
  final data = jsonDecode(response.body);
  return data['caregiver_id'] as String?;
}
