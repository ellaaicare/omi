class ResolvedEndpoint {
  final String agentId;
  final String? caregiverAgentId;
  final String? scannerAgentId;
  final String sessionKey;
  final String gatewayUrl;
  final String? scannerGatewayUrl;
  final String token;
  final String clusterStatus;
  final DateTime resolvedAt;

  ResolvedEndpoint({
    required this.agentId,
    this.caregiverAgentId,
    this.scannerAgentId,
    required this.sessionKey,
    required this.gatewayUrl,
    this.scannerGatewayUrl,
    required this.token,
    required this.clusterStatus,
    DateTime? resolvedAt,
  }) : resolvedAt = resolvedAt ?? DateTime.now();

  // Cache TTL: 1 hour
  bool get isExpired => DateTime.now().difference(resolvedAt).inHours >= 1;

  static ResolvedEndpoint fromJson(Map<String, dynamic> json) {
    // Handle nested routing structure from API
    var routing = json['routing'] ?? json;

    return ResolvedEndpoint(
      agentId: routing['agentId'],
      caregiverAgentId: routing['caregiverAgentId'],
      scannerAgentId: routing['scannerAgentId'],
      sessionKey: routing['sessionKey'],
      gatewayUrl: routing['gatewayUrl'],
      scannerGatewayUrl: routing['scannerGatewayUrl'],
      token: routing['token'],
      clusterStatus: routing['clusterStatus'] ?? 'UNKNOWN',
    );
  }

  Map<String, dynamic> toJson() {
    return {
      'agentId': agentId,
      'caregiverAgentId': caregiverAgentId,
      'scannerAgentId': scannerAgentId,
      'sessionKey': sessionKey,
      'gatewayUrl': gatewayUrl,
      'scannerGatewayUrl': scannerGatewayUrl,
      'token': token,
      'clusterStatus': clusterStatus,
      'resolvedAt': resolvedAt.toIso8601String(),
    };
  }
}
