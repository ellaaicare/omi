import 'dart:convert';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/ella/models/guardian_alert.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/debug_log_manager.dart';
import 'package:omi/utils/logger.dart';

class GuardianAlertHistoryResult {
  const GuardianAlertHistoryResult({
    required this.records,
    required this.source,
    this.error,
  });

  final List<GuardianAlertRecord> records;
  final GuardianAlertHistorySource source;
  final String? error;

  bool get isLocalFallback => source == GuardianAlertHistorySource.localDebugLog;
}

enum GuardianAlertHistorySource { backend, localDebugLog }

class GuardianAlertHistoryApi {
  GuardianAlertHistoryApi._();

  static Future<GuardianAlertHistoryResult> fetch({int limit = 50}) async {
    final backend = await _fetchBackend(limit: limit);
    if (backend != null) return backend;

    final fallback = await _fetchLocalDebugLogs(limit: limit);
    return GuardianAlertHistoryResult(
      records: fallback,
      source: GuardianAlertHistorySource.localDebugLog,
      error: 'Backend Guardian alert history is unavailable. Showing local debug fallback.',
    );
  }

  static Future<GuardianAlertHistoryResult?> _fetchBackend({required int limit}) async {
    final baseUrl = Env.apiBaseUrl;
    if (baseUrl == null || baseUrl.isEmpty) return null;

    try {
      final response = await makeApiCall(
        url: '${baseUrl}v1/ella/guardian-alerts?limit=$limit',
        headers: const {'Content-Type': 'application/json'},
        body: '',
        method: 'GET',
        timeout: const Duration(seconds: 10),
        retries: 0,
      );
      if (response == null || response.statusCode == 404 || response.statusCode == 501) return null;
      if (response.statusCode < 200 || response.statusCode >= 300) {
        Logger.debug('Guardian alert history fetch failed: ${response.statusCode} ${response.body}');
        return null;
      }

      final decoded = jsonDecode(response.body);
      final records = parseBackendRecords(decoded).take(limit).toList(growable: false);
      return GuardianAlertHistoryResult(records: records, source: GuardianAlertHistorySource.backend);
    } catch (e) {
      Logger.debug('Guardian alert history fetch error: $e');
      return null;
    }
  }

  static List<GuardianAlertRecord> parseBackendRecords(dynamic decoded) {
    final items = _extractRecordList(decoded);
    final records = items.map(GuardianAlertRecord.fromJson).toList();
    records.sort(_newestFirst);
    return records;
  }

  static Future<List<GuardianAlertRecord>> _fetchLocalDebugLogs({required int limit}) async {
    final records = <GuardianAlertRecord>[];
    try {
      final files = await DebugLogManager.listLogFiles();
      for (final file in files) {
        final lines = await file.readAsLines();
        for (final line in lines.reversed) {
          if (records.length >= limit) break;
          final trimmed = line.trim();
          if (trimmed.isEmpty) continue;
          final decoded = jsonDecode(trimmed);
          if (decoded is! Map<String, dynamic>) continue;
          if (!GuardianAlertRecord.isGuardianDebugLog(decoded)) continue;
          records.add(GuardianAlertRecord.fromDebugLog(decoded));
        }
        if (records.length >= limit) break;
      }
    } catch (e) {
      Logger.debug('Guardian alert local fallback error: $e');
    }
    records.sort(_newestFirst);
    return records.take(limit).toList(growable: false);
  }

  static List<Map<String, dynamic>> _extractRecordList(dynamic decoded) {
    if (decoded is List) return decoded.whereType<Map<String, dynamic>>().toList(growable: false);
    if (decoded is! Map<String, dynamic>) return const [];

    for (final key in const ['alerts', 'records', 'items', 'data', 'history']) {
      final value = decoded[key];
      if (value is List) return value.whereType<Map<String, dynamic>>().toList(growable: false);
    }
    return const [];
  }

  static int _newestFirst(GuardianAlertRecord a, GuardianAlertRecord b) {
    final left = a.createdAt ?? DateTime.fromMillisecondsSinceEpoch(0, isUtc: true);
    final right = b.createdAt ?? DateTime.fromMillisecondsSinceEpoch(0, isUtc: true);
    return right.compareTo(left);
  }
}
