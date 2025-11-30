import 'dart:async';
import 'dart:collection';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Cache entry with TTL-based expiration
class CacheEntry<T> {
  final T data;
  final DateTime createdAt;
  final Duration ttl;

  CacheEntry({
    required this.data,
    required this.ttl,
    DateTime? createdAt,
  }) : createdAt = createdAt ?? DateTime.now();

  bool get isExpired => DateTime.now().difference(createdAt) > ttl;

  Map<String, dynamic> toJson(Object? Function(T) dataEncoder) => {
        'data': dataEncoder(data),
        'createdAt': createdAt.toIso8601String(),
        'ttlMs': ttl.inMilliseconds,
      };

  static CacheEntry<T>? fromJson<T>(
    Map<String, dynamic> json,
    T Function(Object?) dataDecoder,
  ) {
    try {
      return CacheEntry<T>(
        data: dataDecoder(json['data']),
        createdAt: DateTime.parse(json['createdAt'] as String),
        ttl: Duration(milliseconds: json['ttlMs'] as int),
      );
    } catch (e) {
      debugPrint('Failed to parse cache entry: $e');
      return null;
    }
  }
}

/// Memory + disk caching layer with LRU eviction
class ResponseCache {
  static final ResponseCache _instance = ResponseCache._internal();
  static ResponseCache get instance => _instance;

  ResponseCache._internal();

  /// In-memory cache with LRU eviction
  final LinkedHashMap<String, CacheEntry<dynamic>> _memoryCache =
      LinkedHashMap<String, CacheEntry<dynamic>>();

  /// Maximum entries in memory cache
  static const int _maxMemoryCacheSize = 100;

  /// Prefix for disk cache keys
  static const String _diskCachePrefix = 'response_cache_';

  /// Get cached value from memory, falling back to disk
  Future<T?> get<T>(
    String key, {
    T Function(Object?)? decoder,
  }) async {
    // Check memory cache first
    final memoryEntry = _memoryCache[key];
    if (memoryEntry != null && !memoryEntry.isExpired) {
      // Move to end for LRU
      _memoryCache.remove(key);
      _memoryCache[key] = memoryEntry;
      return memoryEntry.data as T;
    }

    // Remove expired memory entry
    if (memoryEntry != null) {
      _memoryCache.remove(key);
    }

    // Check disk cache if decoder provided
    if (decoder != null) {
      final diskEntry = await _getDiskEntry<T>(key, decoder);
      if (diskEntry != null && !diskEntry.isExpired) {
        // Promote to memory cache
        _setMemory(key, diskEntry);
        return diskEntry.data;
      }
    }

    return null;
  }

  /// Set value in both memory and disk cache
  Future<void> set<T>(
    String key,
    T data, {
    Duration ttl = const Duration(minutes: 5),
    Object? Function(T)? encoder,
  }) async {
    final entry = CacheEntry<T>(data: data, ttl: ttl);

    // Set in memory
    _setMemory(key, entry);

    // Set in disk if encoder provided
    if (encoder != null) {
      await _setDiskEntry(key, entry, encoder);
    }
  }

  /// Remove entry from both memory and disk
  Future<void> remove(String key) async {
    _memoryCache.remove(key);
    await _removeDiskEntry(key);
  }

  /// Clear all cache entries
  Future<void> clear() async {
    _memoryCache.clear();
    await _clearDiskCache();
  }

  /// Set entry in memory cache with LRU eviction
  void _setMemory<T>(String key, CacheEntry<T> entry) {
    // Remove if exists to update position
    _memoryCache.remove(key);

    // Evict oldest entries if at capacity
    while (_memoryCache.length >= _maxMemoryCacheSize) {
      _memoryCache.remove(_memoryCache.keys.first);
    }

    _memoryCache[key] = entry;
  }

  /// Get entry from disk cache
  Future<CacheEntry<T>?> _getDiskEntry<T>(
    String key,
    T Function(Object?) decoder,
  ) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final jsonString = prefs.getString('$_diskCachePrefix$key');
      if (jsonString == null) return null;

      final json = jsonDecode(jsonString) as Map<String, dynamic>;
      return CacheEntry.fromJson<T>(json, decoder);
    } catch (e) {
      debugPrint('Failed to read disk cache for $key: $e');
      return null;
    }
  }

  /// Set entry in disk cache
  Future<void> _setDiskEntry<T>(
    String key,
    CacheEntry<T> entry,
    Object? Function(T) encoder,
  ) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final json = entry.toJson(encoder);
      await prefs.setString('$_diskCachePrefix$key', jsonEncode(json));
    } catch (e) {
      debugPrint('Failed to write disk cache for $key: $e');
    }
  }

  /// Remove entry from disk cache
  Future<void> _removeDiskEntry(String key) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.remove('$_diskCachePrefix$key');
    } catch (e) {
      debugPrint('Failed to remove disk cache for $key: $e');
    }
  }

  /// Clear all disk cache entries
  Future<void> _clearDiskCache() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final keys = prefs.getKeys().where((k) => k.startsWith(_diskCachePrefix));
      for (final key in keys) {
        await prefs.remove(key);
      }
    } catch (e) {
      debugPrint('Failed to clear disk cache: $e');
    }
  }
}

/// Request deduplication to prevent duplicate concurrent API calls
class RequestDeduplicator {
  static final RequestDeduplicator _instance = RequestDeduplicator._internal();
  static RequestDeduplicator get instance => _instance;

  RequestDeduplicator._internal();

  /// Map of in-flight requests
  final Map<String, Future<dynamic>> _inFlightRequests = {};

  /// Execute a request with deduplication
  /// If a request with the same key is already in flight, return its result
  Future<T> deduplicate<T>(
    String key,
    Future<T> Function() request,
  ) async {
    // If request is already in flight, return its future
    if (_inFlightRequests.containsKey(key)) {
      debugPrint('Request deduplication: returning existing request for $key');
      return await _inFlightRequests[key] as T;
    }

    // Create and store the request
    final future = request().whenComplete(() {
      _inFlightRequests.remove(key);
    });

    _inFlightRequests[key] = future;
    return await future;
  }

  /// Check if a request is in flight
  bool isInFlight(String key) => _inFlightRequests.containsKey(key);

  /// Cancel tracking of a request (doesn't cancel the actual request)
  void cancel(String key) => _inFlightRequests.remove(key);

  /// Clear all tracked requests
  void clear() => _inFlightRequests.clear();
}
