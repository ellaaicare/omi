import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/models/caregiver.dart';
import 'package:omi/utils/logger.dart';

/// n8n webhook base URL for caregiver operations.
/// All caregiver data lives in ella-ai-care Postgres, not OMI Firestore.
const String _n8nBase = 'https://n8n.ella-ai-care.com/webhook';

String get _uid => SharedPreferencesUtil().uid;

Map<String, dynamic>? _decodeObject(String body) {
  try {
    final data = jsonDecode(body);
    return data is Map<String, dynamic> ? data : null;
  } catch (_) {
    return null;
  }
}

InviteResponse? _decodeInviteResponse(String body) {
  final data = _decodeObject(body);
  if (data == null) return null;
  final invitePayload = data['invite'] is Map<String, dynamic> ? data['invite'] as Map<String, dynamic> : data;
  return InviteResponse.fromJson(invitePayload);
}

CaregiverApiException _inviteException(http.Response response, String message) {
  final inviteResponse = _decodeInviteResponse(response.body);
  return CaregiverApiException(
    statusCode: response.statusCode,
    message: inviteResponse?.deliveryError ?? inviteResponse?.failureReason ?? message,
    inviteResponse: inviteResponse?.hasInviteRecovery == true ? inviteResponse : null,
  );
}

/// GET caregiver list via n8n webhook
Future<List<Caregiver>> getCaregivers() async {
  try {
    final response = await http.post(
      Uri.parse('$_n8nBase/caregiver-list'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'uid': _uid}),
    );
    if (response.statusCode != 200) return [];
    final data = jsonDecode(response.body);
    final list = data is List ? data : (data['caregivers'] as List?) ?? [];
    return list.map((c) => Caregiver.fromJson(c as Map<String, dynamic>)).toList();
  } catch (e) {
    Logger.debug('Failed to load caregivers: $e');
    return [];
  }
}

/// POST invite caregiver via n8n webhook
Future<InviteResponse> sendCaregiverInvite({
  required String name,
  String? phone,
  required String email,
  required String relationship,
  bool dailySummary = true,
}) async {
  final response = await http.post(
    Uri.parse('$_n8nBase/caregiver-invite'),
    headers: {'Content-Type': 'application/json'},
    body: jsonEncode({
      'uid': _uid,
      if (SharedPreferencesUtil().ellaUserId.isNotEmpty) 'userId': SharedPreferencesUtil().ellaUserId,
      'name': name,
      if (phone != null && phone.isNotEmpty) 'phone': phone,
      'email': email,
      'relationship': relationship,
      'permissions': {
        'receive_daily_summary': dailySummary,
        'daily_summary_email': dailySummary,
      },
    }),
  );
  if (response.statusCode != 200 && response.statusCode != 201) {
    Logger.debug('Caregiver invite failed: ${response.statusCode} ${response.body}');
    throw _inviteException(response, 'Failed to send invite');
  }
  final inviteResponse = _decodeInviteResponse(response.body);
  if (inviteResponse == null) {
    throw CaregiverApiException(statusCode: response.statusCode, message: 'Invalid invite response');
  }
  return inviteResponse;
}

/// POST remove caregiver via n8n webhook
Future<void> removeCaregiver(String caregiverId) async {
  final response = await http.post(
    Uri.parse('$_n8nBase/caregiver-remove'),
    headers: {'Content-Type': 'application/json'},
    body: jsonEncode({'uid': _uid, 'caregiver_id': caregiverId}),
  );
  if (response.statusCode != 200 && response.statusCode != 204) {
    throw CaregiverApiException(
      statusCode: response.statusCode,
      message: 'Failed to remove caregiver',
    );
  }
}

/// POST update caregiver permissions via n8n webhook
Future<void> updateCaregiverPermissions(String caregiverId, {required bool dailySummary}) async {
  final response = await http.post(
    Uri.parse('$_n8nBase/caregiver-permissions'),
    headers: {'Content-Type': 'application/json'},
    body: jsonEncode({
      'uid': _uid,
      'caregiver_id': caregiverId,
      'receive_daily_summary': dailySummary,
      'daily_summary_email': dailySummary,
    }),
  );
  if (response.statusCode != 200) {
    throw CaregiverApiException(
      statusCode: response.statusCode,
      message: 'Failed to update permissions',
    );
  }
}

/// PUT emergency contact (OMI backend — stays in Firestore)
Future<void> setEmergencyContact(String caregiverId) async {
  final response = await http.post(
    Uri.parse('$_n8nBase/caregiver-set-emergency'),
    headers: {'Content-Type': 'application/json'},
    body: jsonEncode({'uid': _uid, 'caregiver_id': caregiverId}),
  );
  if (response.statusCode != 200) {
    throw CaregiverApiException(
      statusCode: response.statusCode,
      message: 'Failed to set emergency contact',
    );
  }
}

/// Clear emergency contact (set to none)
Future<void> clearEmergencyContact() async {
  final response = await http.post(
    Uri.parse('$_n8nBase/caregiver-set-emergency'),
    headers: {'Content-Type': 'application/json'},
    body: jsonEncode({'uid': _uid, 'caregiver_id': ''}),
  );
  if (response.statusCode != 200) {
    throw CaregiverApiException(
      statusCode: response.statusCode,
      message: 'Failed to clear emergency contact',
    );
  }
}

/// GET emergency contact ID
Future<String?> getEmergencyContactId() async {
  try {
    final response = await http.post(
      Uri.parse('$_n8nBase/caregiver-get-emergency'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'uid': _uid}),
    );
    if (response.statusCode != 200) return null;
    final data = jsonDecode(response.body);
    return data['caregiver_id'] as String?;
  } catch (e) {
    Logger.debug('Failed to get emergency contact: $e');
    return null;
  }
}

/// POST resend caregiver invite via n8n webhook
Future<InviteResponse> resendInvite({required String uid, required String caregiverId}) async {
  final response = await http.post(
    Uri.parse('$_n8nBase/caregiver-resend-invite'),
    headers: {'Content-Type': 'application/json'},
    body: jsonEncode({'uid': uid, 'caregiver_id': caregiverId}),
  );
  if (response.statusCode != 200) {
    throw _inviteException(response, 'Failed to resend invite');
  }
  final inviteResponse = _decodeInviteResponse(response.body);
  if (inviteResponse == null) {
    throw CaregiverApiException(statusCode: response.statusCode, message: 'Invalid resend response');
  }
  return inviteResponse;
}
