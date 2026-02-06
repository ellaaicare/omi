class EmergencyResponse {
  final String emergencyId;
  final String status;
  final List<NotifiedContact> contactsNotified;
  final int cancelWindowSeconds;
  final String? audioConfirmationUrl;

  EmergencyResponse.fromJson(Map<String, dynamic> json)
      : emergencyId = json['emergency_id'] ?? '',
        status = json['status'] ?? '',
        contactsNotified = (json['contacts_notified'] as List?)
                ?.map((c) => NotifiedContact.fromJson(c as Map<String, dynamic>))
                .toList() ??
            [],
        cancelWindowSeconds = json['cancel_window_seconds'] ?? 10,
        audioConfirmationUrl = json['audio_confirmation_url'];
}

class NotifiedContact {
  final String contactId;
  final String name;
  final String method;
  final String status;

  NotifiedContact.fromJson(Map<String, dynamic> json)
      : contactId = json['contact_id'] ?? '',
        name = json['name'] ?? '',
        method = json['method'] ?? '',
        status = json['status'] ?? '';
}

class CancelResponse {
  final String emergencyId;
  final String status;
  final List<String> contactsNotifiedOfCancel;

  CancelResponse.fromJson(Map<String, dynamic> json)
      : emergencyId = json['emergency_id'] ?? '',
        status = json['status'] ?? '',
        contactsNotifiedOfCancel = List<String>.from(json['contacts_notified_of_cancel'] ?? []);
}

class EmergencyApiException implements Exception {
  final int statusCode;
  final String message;
  EmergencyApiException({required this.statusCode, required this.message});

  @override
  String toString() => 'EmergencyApiException($statusCode): $message';
}
