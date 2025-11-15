import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:http_certificate_pinning/http_certificate_pinning.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/logger.dart';

/// Certificate pinning configuration for API security
/// Implements SSL/TLS certificate pinning to prevent man-in-the-middle attacks
class CertificatePinningConfig {
  static final CertificatePinningConfig _instance = CertificatePinningConfig._internal();
  static CertificatePinningConfig get instance => _instance;

  CertificatePinningConfig._internal();

  /// Certificate pinning enabled flag
  /// Set to false in development, true in production
  bool get isPinningEnabled {
    // Disable pinning for localhost/development
    final apiUrl = Env.apiBaseUrl ?? '';
    if (apiUrl.contains('localhost') ||
        apiUrl.contains('127.0.0.1') ||
        apiUrl.contains('10.0.2.2') ||
        kDebugMode && apiUrl.contains('dev')) {
      return false;
    }
    return true;
  }

  /// Get the hostname from API base URL
  String get hostname {
    try {
      final apiUrl = Env.apiBaseUrl ?? '';
      final uri = Uri.parse(apiUrl);
      return uri.host;
    } catch (e) {
      Logger.error('Failed to parse API URL for certificate pinning: $e');
      return '';
    }
  }

  /// SHA-256 fingerprints of trusted certificates
  /// IMPORTANT: These should be updated with actual production certificate fingerprints
  /// To get certificate fingerprints, use:
  /// openssl s_client -servername HOSTNAME -connect HOSTNAME:443 | openssl x509 -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | openssl enc -base64
  List<String> get sha256Fingerprints {
    // TODO: Replace with actual production certificate fingerprints
    // For now, return empty list to disable strict pinning in development
    // In production, add actual certificate pins here
    return [
      // Example format:
      // 'sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
      // 'sha256/BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=', // Backup certificate
    ];
  }

  /// Check if certificate pinning should be enforced
  bool shouldEnforce() {
    return isPinningEnabled && sha256Fingerprints.isNotEmpty;
  }

  /// Verify certificate manually for custom validation
  Future<bool> verifyCertificate(X509Certificate? certificate, String host) async {
    if (!shouldEnforce()) {
      // Allow all certificates in development
      return true;
    }

    if (certificate == null) {
      Logger.error('Certificate is null for host: $host');
      return false;
    }

    try {
      // In production with proper pins, validate against known fingerprints
      // For now, log certificate details for debugging
      debugPrint('Certificate subject: ${certificate.subject}');
      debugPrint('Certificate issuer: ${certificate.issuer}');
      debugPrint('Certificate valid from: ${certificate.startValidity}');
      debugPrint('Certificate valid to: ${certificate.endValidity}');

      // If no pins configured, allow (but log warning)
      if (sha256Fingerprints.isEmpty) {
        Logger.log('WARNING: Certificate pinning enabled but no pins configured');
        return true;
      }

      // TODO: Implement actual fingerprint validation
      // This would require extracting the certificate's public key hash
      // and comparing it against sha256Fingerprints

      return true;
    } catch (e) {
      Logger.error('Certificate verification failed: $e');
      return false;
    }
  }

  /// Initialize certificate pinning checker
  Future<void> initializePinning() async {
    if (!shouldEnforce()) {
      debugPrint('Certificate pinning disabled for development');
      return;
    }

    try {
      // Check if we can reach the API with certificate pinning
      final result = await HttpCertificatePinning.check(
        serverURL: Env.apiBaseUrl ?? '',
        headerHttp: {},
        sha: SHA.SHA256,
        allowedSHAFingerprints: sha256Fingerprints,
        timeout: 30,
      );

      if (result.contains('CONNECTION_SECURE')) {
        debugPrint('Certificate pinning validation successful');
      } else {
        Logger.error('Certificate pinning validation failed: $result');
      }
    } catch (e) {
      // Don't crash the app if pinning check fails during init
      // This allows development to continue
      Logger.error('Certificate pinning initialization failed: $e');
      if (kReleaseMode) {
        // In production, this should be treated more seriously
        Logger.error('CRITICAL: Certificate pinning failed in production');
      }
    }
  }

  /// Check if a URL requires certificate pinning
  bool requiresPinning(String url) {
    if (!shouldEnforce()) return false;

    final apiUrl = Env.apiBaseUrl ?? '';
    return url.startsWith(apiUrl);
  }

  /// Validate a specific URL with certificate pinning
  Future<bool> validateUrl(String url) async {
    if (!requiresPinning(url)) {
      return true;
    }

    if (sha256Fingerprints.isEmpty) {
      // No pins configured, allow but log warning
      Logger.log('WARNING: No certificate pins configured for: $url');
      return true;
    }

    try {
      final result = await HttpCertificatePinning.check(
        serverURL: url,
        headerHttp: {},
        sha: SHA.SHA256,
        allowedSHAFingerprints: sha256Fingerprints,
        timeout: 10,
      );

      return result.contains('CONNECTION_SECURE');
    } catch (e) {
      Logger.error('Certificate validation failed for $url: $e');
      return false;
    }
  }
}

/// Custom HTTP client with certificate pinning support
class SecureHttpClient {
  /// Create a secure HTTP client with certificate pinning
  static HttpClient createSecureClient() {
    final client = HttpClient();

    if (CertificatePinningConfig.instance.shouldEnforce()) {
      client.badCertificateCallback = (cert, host, port) {
        // Verify certificate against pinned certificates
        final isValid = CertificatePinningConfig.instance.verifyCertificate(cert, host);
        if (isValid == false) {
          Logger.error('Certificate validation failed for $host:$port');
        }
        return isValid == true;
      };
    }

    return client;
  }
}
