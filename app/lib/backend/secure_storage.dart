import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/utils/logger.dart';

/// SecureStorage utility class for handling sensitive data storage
/// Uses flutter_secure_storage to encrypt data at rest
/// Maintains an in-memory cache for synchronous access
class SecureStorage {
  static final SecureStorage _instance = SecureStorage._internal();
  static SecureStorage get instance => _instance;

  late FlutterSecureStorage _storage;
  bool _isInitialized = false;
  bool _migrationCompleted = false;

  // In-memory cache for synchronous access
  final Map<String, String> _cache = {};

  SecureStorage._internal();

  /// Initialize secure storage with platform-specific options
  Future<void> init() async {
    if (_isInitialized) return;

    try {
      // Platform-specific options for secure storage
      const androidOptions = AndroidOptions(
        encryptedSharedPreferences: true,
        resetOnError: true,
      );

      const iosOptions = IOSOptions(
        accessibility: KeychainAccessibility.first_unlock,
        synchronizable: false,
      );

      const linuxOptions = LinuxOptions(
        resetOnError: true,
      );

      const windowsOptions = WindowsOptions(
        resetOnError: true,
      );

      const macOsOptions = MacOsOptions(
        accessibility: KeychainAccessibility.first_unlock,
        synchronizable: false,
      );

      const webOptions = WebOptions(
        dbName: 'omi_secure_storage',
        publicKey: 'omi_public_key',
      );

      _storage = const FlutterSecureStorage(
        aOptions: androidOptions,
        iOptions: iosOptions,
        lOptions: linuxOptions,
        wOptions: windowsOptions,
        mOptions: macOsOptions,
        webOptions: webOptions,
      );

      _isInitialized = true;
      debugPrint('SecureStorage initialized successfully');

      // Perform migration from SharedPreferences if not already done
      await _migrateFromSharedPreferences();

      // Load all values into cache for synchronous access
      await _loadCache();
    } catch (e) {
      Logger.error('Failed to initialize SecureStorage: $e');
      rethrow;
    }
  }

  /// Migrate sensitive data from SharedPreferences to SecureStorage
  Future<void> _migrateFromSharedPreferences() async {
    try {
      // Check if migration was already completed
      final migrationFlag = await _storage.read(key: '_migration_completed');
      if (migrationFlag == 'true') {
        _migrationCompleted = true;
        debugPrint('Migration already completed, skipping');
        return;
      }

      debugPrint('Starting migration from SharedPreferences to SecureStorage');

      // Migrate auth token
      final authToken = SharedPreferencesUtil().authToken;
      if (authToken.isNotEmpty) {
        await saveAuthToken(authToken);
        debugPrint('Migrated authToken');
      }

      // Migrate token expiration time
      final tokenExpiry = SharedPreferencesUtil().tokenExpirationTime;
      if (tokenExpiry > 0) {
        await saveTokenExpirationTime(tokenExpiry);
        debugPrint('Migrated tokenExpirationTime');
      }

      // Migrate user email
      final email = SharedPreferencesUtil().email;
      if (email.isNotEmpty) {
        await saveEmail(email);
        debugPrint('Migrated email');
      }

      // Migrate user names
      final givenName = SharedPreferencesUtil().givenName;
      if (givenName.isNotEmpty) {
        await saveGivenName(givenName);
        debugPrint('Migrated givenName');
      }

      final familyName = SharedPreferencesUtil().familyName;
      if (familyName.isNotEmpty) {
        await saveFamilyName(familyName);
        debugPrint('Migrated familyName');
      }

      // Migrate UID
      final uid = SharedPreferencesUtil().uid;
      if (uid.isNotEmpty) {
        await saveUid(uid);
        debugPrint('Migrated uid');
      }

      // Mark migration as completed
      await _storage.write(key: '_migration_completed', value: 'true');
      _migrationCompleted = true;

      // Clear sensitive data from SharedPreferences
      await SharedPreferencesUtil().remove('authToken');
      await SharedPreferencesUtil().remove('tokenExpirationTime');
      await SharedPreferencesUtil().remove('email');
      await SharedPreferencesUtil().remove('givenName');
      await SharedPreferencesUtil().remove('familyName');
      // Note: We keep uid in SharedPreferences for backward compatibility

      debugPrint('Migration completed successfully');
    } catch (e) {
      Logger.error('Migration failed: $e');
      // Don't rethrow - allow app to continue even if migration fails
    }
  }

  /// Load all secure storage values into cache
  Future<void> _loadCache() async {
    try {
      final allValues = await _storage.readAll();
      _cache.addAll(allValues);
      debugPrint('Loaded ${_cache.length} values into cache');
    } catch (e) {
      Logger.error('Failed to load cache: $e');
    }
  }

  /// Check if storage is initialized
  void _ensureInitialized() {
    if (!_isInitialized) {
      throw StateError('SecureStorage not initialized. Call init() first.');
    }
  }

  // ==================== Auth Token ====================

  Future<void> saveAuthToken(String token) async {
    _ensureInitialized();
    try {
      await _storage.write(key: 'authToken', value: token);
      _cache['authToken'] = token;
    } catch (e) {
      Logger.error('Failed to save auth token: $e');
      rethrow;
    }
  }

  String getAuthToken() {
    _ensureInitialized();
    return _cache['authToken'] ?? '';
  }

  Future<String> getAuthTokenAsync() async {
    _ensureInitialized();
    try {
      final token = await _storage.read(key: 'authToken');
      if (token != null) {
        _cache['authToken'] = token;
      }
      return token ?? '';
    } catch (e) {
      Logger.error('Failed to read auth token: $e');
      return '';
    }
  }

  // ==================== Token Expiration ====================

  Future<void> saveTokenExpirationTime(int timestamp) async {
    _ensureInitialized();
    try {
      await _storage.write(key: 'tokenExpirationTime', value: timestamp.toString());
      _cache['tokenExpirationTime'] = timestamp.toString();
    } catch (e) {
      Logger.error('Failed to save token expiration time: $e');
      rethrow;
    }
  }

  int getTokenExpirationTime() {
    _ensureInitialized();
    final value = _cache['tokenExpirationTime'];
    return value != null ? int.tryParse(value) ?? 0 : 0;
  }

  // ==================== Email ====================

  Future<void> saveEmail(String email) async {
    _ensureInitialized();
    try {
      await _storage.write(key: 'email', value: email);
      _cache['email'] = email;
    } catch (e) {
      Logger.error('Failed to save email: $e');
      rethrow;
    }
  }

  String getEmail() {
    _ensureInitialized();
    return _cache['email'] ?? '';
  }

  // ==================== Given Name ====================

  Future<void> saveGivenName(String name) async {
    _ensureInitialized();
    try {
      await _storage.write(key: 'givenName', value: name);
      _cache['givenName'] = name;
    } catch (e) {
      Logger.error('Failed to save given name: $e');
      rethrow;
    }
  }

  String getGivenName() {
    _ensureInitialized();
    return _cache['givenName'] ?? '';
  }

  // ==================== Family Name ====================

  Future<void> saveFamilyName(String name) async {
    _ensureInitialized();
    try {
      await _storage.write(key: 'familyName', value: name);
      _cache['familyName'] = name;
    } catch (e) {
      Logger.error('Failed to save family name: $e');
      rethrow;
    }
  }

  String getFamilyName() {
    _ensureInitialized();
    return _cache['familyName'] ?? '';
  }

  // ==================== UID ====================

  Future<void> saveUid(String uid) async {
    _ensureInitialized();
    try {
      await _storage.write(key: 'uid', value: uid);
      _cache['uid'] = uid;
    } catch (e) {
      Logger.error('Failed to save uid: $e');
      rethrow;
    }
  }

  String getUid() {
    _ensureInitialized();
    return _cache['uid'] ?? '';
  }

  // ==================== Clear All ====================

  /// Clear all secure storage data
  Future<void> clearAll() async {
    _ensureInitialized();
    try {
      await _storage.deleteAll();
      _cache.clear();
      _migrationCompleted = false;
      debugPrint('Cleared all secure storage');
    } catch (e) {
      Logger.error('Failed to clear secure storage: $e');
      rethrow;
    }
  }

  /// Delete a specific key
  Future<void> delete(String key) async {
    _ensureInitialized();
    try {
      await _storage.delete(key: key);
      _cache.remove(key);
    } catch (e) {
      Logger.error('Failed to delete key $key: $e');
      rethrow;
    }
  }
}
