# PRD: App Security & Token Storage (CRITICAL)

## Status
🔴 **CRITICAL** - Must be completed immediately

## Executive Summary
Secure sensitive data storage in the Flutter app by migrating from unencrypted SharedPreferences to flutter_secure_storage, implementing certificate pinning, and fixing Bluetooth connection race conditions.

## Problem Statement
The app currently stores sensitive data insecurely:
1. **Auth tokens stored in plain text** via SharedPreferences (accessible on rooted/jailbroken devices)
2. **No certificate pinning** (vulnerable to MITM attacks)
3. **Bluetooth connection race conditions** (no mutex protection)
4. **Stream subscription memory leaks** in CaptureProvider

## Goals
- Migrate all sensitive data to secure storage
- Implement SSL certificate pinning
- Fix Bluetooth connection race conditions
- Eliminate memory leaks in stream subscriptions

## Success Metrics
- ✅ All auth tokens and sensitive data in flutter_secure_storage
- ✅ Certificate pinning active for all API calls
- ✅ Zero memory leaks in stream subscriptions
- ✅ No Bluetooth connection race conditions
- ✅ Security audit passes

## Technical Specification

### 1. Migrate to Secure Storage

**Dependencies:**
```yaml
# pubspec.yaml
dependencies:
  flutter_secure_storage: ^9.0.0
```

**Files Affected:**
- `/app/lib/backend/preferences.dart`
- `/app/lib/utils/secure_storage.dart` (new file)

**Implementation:**

**Step 1: Create SecureStorage Wrapper**
```dart
// lib/utils/secure_storage.dart
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

class SecureStorage {
  static final SecureStorage _instance = SecureStorage._internal();
  factory SecureStorage() => _instance;
  SecureStorage._internal();

  final _storage = const FlutterSecureStorage(
    aOptions: AndroidOptions(
      encryptedSharedPreferences: true,
    ),
    iOptions: IOSOptions(
      accessibility: KeychainAccessibility.first_unlock,
    ),
  );

  // Auth Token
  Future<void> saveAuthToken(String token) async {
    await _storage.write(key: 'authToken', value: token);
  }

  Future<String?> getAuthToken() async {
    return await _storage.read(key: 'authToken');
  }

  // User ID
  Future<void> saveUid(String uid) async {
    await _storage.write(key: 'uid', value: uid);
  }

  Future<String?> getUid() async {
    return await _storage.read(key: 'uid');
  }

  // Email
  Future<void> saveEmail(String email) async {
    await _storage.write(key: 'email', value: email);
  }

  Future<String?> getEmail() async {
    return await _storage.read(key: 'email');
  }

  // API Keys
  Future<void> saveApiKey(String keyName, String value) async {
    await _storage.write(key: 'apiKey_$keyName', value: value);
  }

  Future<String?> getApiKey(String keyName) async {
    return await _storage.read(key: 'apiKey_$keyName');
  }

  // Clear all secure data (logout)
  Future<void> clearAll() async {
    await _storage.deleteAll();
  }

  // Migration: Move from SharedPreferences
  Future<void> migrateFromSharedPreferences() async {
    final prefs = SharedPreferencesUtil();

    // Migrate auth token
    if (prefs.authToken.isNotEmpty) {
      await saveAuthToken(prefs.authToken);
      await prefs.remove('authToken');
    }

    // Migrate UID
    if (prefs.uid.isNotEmpty) {
      await saveUid(prefs.uid);
      await prefs.remove('uid');
    }

    // Migrate email
    if (prefs.email.isNotEmpty) {
      await saveEmail(prefs.email);
      await prefs.remove('email');
    }
  }
}
```

**Step 2: Update Preferences Class**
```dart
// lib/backend/preferences.dart (MODIFIED)
import 'package:omi/utils/secure_storage.dart';

class SharedPreferencesUtil {
  // ... keep non-sensitive preferences ...

  // REMOVED: These now use SecureStorage
  // String get authToken => getString('authToken') ?? '';
  // String get uid => getString('uid') ?? '';
  // String get email => getString('email') ?? '';
}

// Create async accessors in auth-related code
class AuthProvider extends ChangeNotifier {
  Future<String?> getAuthToken() async {
    return await SecureStorage().getAuthToken();
  }

  Future<void> setAuthToken(String token) async {
    await SecureStorage().saveAuthToken(token);
    notifyListeners();
  }
}
```

**Step 3: Update All References**
Files to update:
- `/app/lib/backend/http/shared.dart` (HTTP client auth headers)
- `/app/lib/providers/auth_provider.dart`
- `/app/lib/providers/device_provider.dart`
- Any file using `SharedPreferencesUtil().authToken`

**Action Items:**
- [ ] Add flutter_secure_storage dependency
- [ ] Create SecureStorage utility class
- [ ] Implement migration from SharedPreferences
- [ ] Update all auth token references to async calls
- [ ] Add migration trigger in app startup
- [ ] Test on iOS and Android
- [ ] Test migration path for existing users

### 2. Implement Certificate Pinning

**Dependencies:**
```yaml
dependencies:
  http_certificate_pinning: ^2.0.0
```

**Implementation:**
```dart
// lib/backend/http/certificate_pinning.dart (NEW FILE)
import 'package:http_certificate_pinning/http_certificate_pinning.dart';

class SecureHttpClient {
  static final SecureHttpClient _instance = SecureHttpClient._internal();
  factory SecureHttpClient() => _instance;
  SecureHttpClient._internal();

  // Production certificates (get from your server)
  final List<String> _certificateSHA256Fingerprints = [
    // Replace with actual certificate fingerprints
    'AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99',
  ];

  final String _apiDomain = 'api.omi.me';  // Your API domain

  Future<http.Response> post(
    String url, {
    Map<String, String>? headers,
    Object? body,
  }) async {
    try {
      return await HttpCertificatePinning.post(
        url,
        headers: headers,
        body: body,
        certificateSHA256Fingerprints: _certificateSHA256Fingerprints,
        timeout: const Duration(seconds: 30),
      );
    } catch (e) {
      // Log certificate pinning failure
      debugPrint('Certificate pinning failed: $e');
      rethrow;
    }
  }

  Future<http.Response> get(
    String url, {
    Map<String, String>? headers,
  }) async {
    return await HttpCertificatePinning.get(
      url,
      headers: headers,
      certificateSHA256Fingerprints: _certificateSHA256Fingerprints,
      timeout: const Duration(seconds: 30),
    );
  }

  // Check certificate validity
  Future<bool> check() async {
    try {
      final result = await HttpCertificatePinning.check(
        serverURL: 'https://$_apiDomain',
        certificateSHA256Fingerprints: _certificateSHA256Fingerprints,
        timeout: 10,
      );
      return result == 'CONNECTION_SECURE';
    } catch (e) {
      return false;
    }
  }
}
```

**Update HTTP Client:**
```dart
// lib/backend/http/shared.dart (MODIFIED)
Future<dynamic> makeApiCall({
  required String url,
  required String method,
  Map<String, String>? headers,
  dynamic body,
}) async {
  final authToken = await SecureStorage().getAuthToken();
  headers = headers ?? {};
  headers['Authorization'] = authToken ?? '';

  // Use pinned client
  final client = SecureHttpClient();

  if (method == 'POST') {
    final response = await client.post(
      url,
      headers: headers,
      body: body,
    );
    return response;
  }
  // ... other methods
}
```

**Action Items:**
- [ ] Get certificate fingerprints from production server
- [ ] Add certificate pinning library
- [ ] Implement SecureHttpClient wrapper
- [ ] Update all HTTP calls to use pinned client
- [ ] Add certificate expiration monitoring
- [ ] Document certificate rotation process
- [ ] Test certificate pinning enforcement

### 3. Fix Bluetooth Race Conditions

**Files Affected:**
- `/app/lib/providers/device_provider.dart` (lines 149-241)

**Current Issue:**
```dart
Future periodicConnect() async {
  scan(t) async {
    if ((!isConnected && connectedDevice == null)) {
      if (isConnecting) return;  // ❌ Simple flag, not thread-safe
      await scanAndConnectToDevice();
    }
  }
  _reconnectionTimer = Timer.periodic(Duration(seconds: 15), scan);
}
```

**Fixed Implementation:**
```dart
// Add mutex for connection operations
import 'package:mutex/mutex.dart';

class DeviceProvider extends ChangeNotifier {
  final _connectionMutex = Mutex();
  bool _isConnecting = false;

  Future<void> scanAndConnectToDevice() async {
    // Acquire lock before connection
    await _connectionMutex.protect(() async {
      if (_isConnecting) return;
      _isConnecting = true;
      notifyListeners();

      try {
        // Existing connection logic
        await _performConnection();
      } finally {
        _isConnecting = false;
        notifyListeners();
      }
    });
  }

  Future<void> periodicConnect(String printer, {bool boundDeviceOnly = false}) async {
    _reconnectionTimer?.cancel();

    scan(Timer t) async {
      if (!isConnected && connectedDevice == null) {
        // Mutex ensures no concurrent scans
        await scanAndConnectToDevice();
      }
    }

    _reconnectionTimer = Timer.periodic(
      Duration(seconds: _connectionCheckSeconds),
      scan,
    );
  }

  Future<void> disconnect({bool force = false}) async {
    await _connectionMutex.protect(() async {
      // Cancel subscriptions
      await _bleConnectionSubscription?.cancel();
      // ... rest of disconnect logic
    });
  }

  @override
  void dispose() {
    _reconnectionTimer?.cancel();
    _connectionMutex.protect(() async {
      // Clean up resources
    });
    super.dispose();
  }
}
```

**Action Items:**
- [ ] Add mutex dependency
- [ ] Implement mutex protection for connection operations
- [ ] Add timeout for mutex acquisition
- [ ] Test concurrent connection scenarios
- [ ] Add logging for mutex contention

### 4. Fix Stream Subscription Memory Leaks

**Files Affected:**
- `/app/lib/providers/capture_provider.dart` (lines 330-365, 520-547)

**Implementation:**
```dart
class CaptureProvider extends ChangeNotifier {
  StreamSubscription? _bleBytesStream;
  StreamSubscription? _blePhotoStream;
  StreamSubscription? _storageStream;
  final List<StreamSubscription> _allSubscriptions = [];

  Future<void> _setupStreams() async {
    // Cancel existing streams first
    await _cancelAllStreams();

    // Create new subscriptions and track them
    _bleBytesStream = await _getBleAudioBytesListener();
    _allSubscriptions.add(_bleBytesStream!);

    _blePhotoStream = await _getBlePhotoListener();
    _allSubscriptions.add(_blePhotoStream!);

    _storageStream = _getStorageListener();
    _allSubscriptions.add(_storageStream!);
  }

  Future<void> _cancelAllStreams() async {
    for (final subscription in _allSubscriptions) {
      await subscription.cancel();
    }
    _allSubscriptions.clear();

    _bleBytesStream = null;
    _blePhotoStream = null;
    _storageStream = null;
  }

  @override
  Future<void> dispose() async {
    await _cancelAllStreams();
    // ... other cleanup
    super.dispose();
  }
}
```

**Action Items:**
- [ ] Track all stream subscriptions in list
- [ ] Implement comprehensive cleanup method
- [ ] Cancel old subscriptions before creating new ones
- [ ] Add null checks before operations
- [ ] Test for memory leaks with DevTools

## Implementation Plan

### Phase 1: Secure Storage Migration (Days 1-2)
- Add flutter_secure_storage
- Create SecureStorage wrapper
- Implement migration from SharedPreferences
- Update all token references

### Phase 2: Certificate Pinning (Day 3)
- Get certificate fingerprints
- Implement SecureHttpClient
- Update HTTP calls
- Test pinning

### Phase 3: Bluetooth & Memory Fixes (Days 4-5)
- Add mutex for Bluetooth
- Fix stream subscription leaks
- Testing and validation

## Testing Requirements

```dart
// test/security/secure_storage_test.dart
void main() {
  group('SecureStorage', () {
    test('stores and retrieves auth token', () async {
      final storage = SecureStorage();
      await storage.saveAuthToken('test_token');
      final token = await storage.getAuthToken();
      expect(token, 'test_token');
    });

    test('migrates from SharedPreferences', () async {
      // Setup old data
      SharedPreferencesUtil().authToken = 'old_token';

      // Migrate
      await SecureStorage().migrateFromSharedPreferences();

      // Verify migration
      final token = await SecureStorage().getAuthToken();
      expect(token, 'old_token');
      expect(SharedPreferencesUtil().authToken, isEmpty);
    });
  });
}

// test/bluetooth/race_condition_test.dart
void main() {
  test('no race condition on concurrent connections', () async {
    final provider = DeviceProvider();

    // Attempt 10 concurrent connections
    await Future.wait([
      for (int i = 0; i < 10; i++) provider.scanAndConnectToDevice()
    ]);

    // Should only have one connection attempt
    expect(provider.connectionAttempts, 1);
  });
}

// test/memory/stream_leak_test.dart
void main() {
  test('no stream subscription leaks', () async {
    final provider = CaptureProvider();

    // Setup streams
    await provider.startCapture();

    // Dispose
    await provider.dispose();

    // Verify all streams cancelled
    expect(provider.activeSubscriptions, 0);
  });
}
```

## Migration Strategy

### For Existing Users
1. On app update, run migration automatically on first launch
2. Background migration of sensitive data from SharedPreferences to SecureStorage
3. Keep backward compatibility for 2 versions
4. Log migration success/failure

```dart
// lib/main.dart
Future<void> _performSecurityMigration() async {
  final prefs = SharedPreferencesUtil();
  final migrationComplete = prefs.getBool('security_migration_v1') ?? false;

  if (!migrationComplete) {
    try {
      await SecureStorage().migrateFromSharedPreferences();
      prefs.setBool('security_migration_v1', true);
      debugPrint('Security migration completed successfully');
    } catch (e) {
      debugPrint('Security migration failed: $e');
      // Log to analytics
    }
  }
}
```

## Rollback Plan
- Certificate pinning can be disabled via remote config flag
- Secure storage migration is one-way (security improvement)
- Bluetooth mutex can be disabled via feature flag for testing

## Dependencies
- `flutter_secure_storage: ^9.0.0`
- `http_certificate_pinning: ^2.0.0`
- `mutex: ^3.1.0`

## Documentation Updates
- [ ] Security best practices for developers
- [ ] Certificate pinning setup guide
- [ ] Migration guide for existing users
- [ ] Troubleshooting guide

## Sign-off
- [ ] Security team review
- [ ] Mobile lead approval
- [ ] QA testing on iOS/Android
- [ ] Privacy policy update (if needed)

---

**Estimated Effort:** 5 days
**Priority:** 🔴 CRITICAL
**Risk Level:** Medium (migration risk)
**Dependencies:** Backend API ready
**Assigned To:** TBD
**Target Completion:** Within 1 week
