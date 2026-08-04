import 'dart:convert';

import 'package:http/http.dart' as http;

import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/models/caregiver.dart';
import 'package:omi/ella/services/ella_service_result.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

typedef CaregiverTransport = Future<http.Response?> Function({
  required String url,
  required String method,
  required String body,
  required String expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier exactAuthority,
});

Future<http.Response?> _defaultTransport({
  required String url,
  required String method,
  required String body,
  required String expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier exactAuthority,
}) =>
    makeApiCall(
      url: url,
      headers: const {'Content-Type': 'application/json'},
      method: method,
      body: body,
      requireAuthCheck: true,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      exactAuthority: exactAuthority,
      retries: 0,
    );

typedef _CaregiverRequestContext = ({String uid, String baseUrl, ExactAccountAuthorityVerifier authority});

_CaregiverRequestContext? _requestContext() {
  final uid = SharedPreferencesUtil().uid.trim();
  final baseUrl = Env.apiBaseUrl;
  final authority = WalOwnerAuthority.operationEntry();
  if (uid.isEmpty || baseUrl == null || baseUrl.isEmpty || authority == null) return null;
  if (authority.uid != uid || !authority.isExactCurrent()) return null;
  return (uid: uid, baseUrl: '${baseUrl}v1/ella/caregivers', authority: authority);
}

ClientApiFailure _responseFailure(http.Response? response) => response == null
    ? const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true)
    : ClientApiFailure.fromHttp(statusCode: response.statusCode, body: response.body);

Future<http.Response?> _send({
  required String path,
  required String method,
  String body = '',
  CaregiverTransport? transport,
  _CaregiverRequestContext? requestContext,
}) async {
  final context = requestContext ?? _requestContext();
  if (context == null) throw const ClientApiFailure(ClientApiFailureKind.authenticationRequired);
  try {
    final response = await (transport ?? _defaultTransport)(
      url: '${context.baseUrl}$path',
      method: method,
      body: body,
      expectedAuthenticatedUid: context.uid,
      exactAuthority: context.authority,
    );
    if (!context.authority.isExactCurrent()) {
      throw const ClientApiFailure(ClientApiFailureKind.accountChanged);
    }
    return response;
  } on ExactAccountAuthorityChangedException {
    throw const ClientApiFailure(ClientApiFailureKind.accountChanged);
  }
}

Future<EllaServiceResult<List<Caregiver>>> getCaregivers({CaregiverTransport? transport}) async {
  try {
    final response = await _send(path: '', method: 'GET', transport: transport);
    if (response == null || response.statusCode != 200) return EllaServiceResult.failure(_responseFailure(response));
    final data = jsonDecode(response.body);
    final list = data is List ? data : (data as Map<String, dynamic>)['caregivers'] as List? ?? const [];
    return EllaServiceResult.success(
      list.map((item) => Caregiver.fromJson(item as Map<String, dynamic>)).toList(growable: false),
    );
  } on ClientApiFailure catch (failure) {
    return EllaServiceResult.failure(failure);
  } catch (_) {
    return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.invalidResponse));
  }
}

Future<InviteResponse> sendCaregiverInvite({
  required String name,
  String? phone,
  required String email,
  required String relationship,
  bool dailySummary = true,
  CaregiverTransport? transport,
}) async {
  final response = await _send(
    path: '/invite',
    method: 'POST',
    transport: transport,
    body: jsonEncode({
      'name': name,
      if (phone != null && phone.isNotEmpty) 'phone': phone,
      'email': email,
      'relationship': relationship,
      'permissions': {'receive_daily_summary': dailySummary, 'daily_summary_email': dailySummary},
    }),
  );
  if (response == null || (response.statusCode != 200 && response.statusCode != 201)) {
    throw _responseFailure(response);
  }
  return InviteResponse.fromJson(jsonDecode(response.body));
}

Future<void> removeCaregiver(String caregiverId, {CaregiverTransport? transport}) async {
  final response = await _send(path: '/$caregiverId', method: 'DELETE', transport: transport);
  if (response == null || (response.statusCode != 200 && response.statusCode != 204)) {
    throw _responseFailure(response);
  }
}

Future<void> updateCaregiverPermissions(
  String caregiverId, {
  required bool dailySummary,
  CaregiverTransport? transport,
}) async {
  final response = await _send(
    path: '/$caregiverId/permissions',
    method: 'PUT',
    transport: transport,
    body: jsonEncode({'receive_daily_summary': dailySummary, 'daily_summary_email': dailySummary}),
  );
  if (response == null || response.statusCode != 200) throw _responseFailure(response);
}

Future<void> setEmergencyContact(String caregiverId, {CaregiverTransport? transport}) async {
  final response = await _send(
    path: '/emergency-contact',
    method: 'PUT',
    transport: transport,
    body: jsonEncode({'caregiver_id': caregiverId}),
  );
  if (response == null || response.statusCode != 200) throw _responseFailure(response);
}

Future<void> clearEmergencyContact({CaregiverTransport? transport}) => setEmergencyContact('', transport: transport);

Future<EllaServiceResult<String?>> getEmergencyContactId({CaregiverTransport? transport}) async {
  try {
    final response = await _send(path: '/emergency-contact', method: 'GET', transport: transport);
    if (response == null || response.statusCode != 200) return EllaServiceResult.failure(_responseFailure(response));
    final data = jsonDecode(response.body) as Map<String, dynamic>;
    final id = data['caregiver_id'];
    if (id != null && id is! String) {
      return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.invalidResponse));
    }
    return EllaServiceResult.success(id as String?);
  } on ClientApiFailure catch (failure) {
    return EllaServiceResult.failure(failure);
  } catch (_) {
    return const EllaServiceResult.failure(ClientApiFailure(ClientApiFailureKind.invalidResponse));
  }
}

Future<void> createEmergencyContact({
  required String name,
  required String phone,
  required String email,
  required String relationship,
  CaregiverTransport? transport,
}) async {
  final context = _requestContext();
  if (context == null) throw const ClientApiFailure(ClientApiFailureKind.authenticationRequired);
  final inviteResponse = await _send(
    path: '/invite',
    method: 'POST',
    requestContext: context,
    transport: transport,
    body: jsonEncode({
      'name': name,
      'phone': phone,
      'email': email,
      'relationship': relationship,
      'permissions': {'receive_daily_summary': false, 'daily_summary_email': false},
    }),
  );
  if (inviteResponse == null || (inviteResponse.statusCode != 200 && inviteResponse.statusCode != 201)) {
    throw _responseFailure(inviteResponse);
  }
  final invite = InviteResponse.fromJson(jsonDecode(inviteResponse.body));
  if (invite.caregiverId.isEmpty) {
    throw const ClientApiFailure(ClientApiFailureKind.invalidResponse);
  }
  final contactResponse = await _send(
    path: '/emergency-contact',
    method: 'PUT',
    requestContext: context,
    transport: transport,
    body: jsonEncode({'caregiver_id': invite.caregiverId}),
  );
  if (contactResponse == null || contactResponse.statusCode != 200) throw _responseFailure(contactResponse);
}

Future<void> resendInvite({required String uid, required String caregiverId, CaregiverTransport? transport}) async {
  final currentUid = SharedPreferencesUtil().uid.trim();
  if (uid != currentUid) throw const ClientApiFailure(ClientApiFailureKind.accountChanged);
  final response = await _send(path: '/$caregiverId/resend-invite', method: 'POST', transport: transport);
  if (response == null || response.statusCode != 200) throw _responseFailure(response);
}
