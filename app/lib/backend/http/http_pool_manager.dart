import 'dart:async';
import 'dart:io';

import 'package:http/http.dart' as http;
import 'package:http/io_client.dart';
import 'package:flutter/foundation.dart';
import 'package:pool/pool.dart';

import 'package:omi/services/wals/wal_owner_authority.dart';

class HttpPoolManager {
  static final HttpPoolManager instance = HttpPoolManager._();

  late http.Client _client;
  late final Pool _pool;

  // GET deduplication: URL -> pending future
  final Map<String, Future<http.Response>> _pendingGets = {};

  HttpPoolManager._() {
    final httpClient = HttpClient()
      ..maxConnectionsPerHost = 15
      ..idleTimeout = const Duration(seconds: 15);

    _client = IOClient(httpClient);
    _pool = Pool(10, timeout: const Duration(seconds: 60));
  }

  Future<http.Response> send(
    http.Request Function() requestBuilder, {
    Duration timeout = const Duration(seconds: 30),
    int retries = 1,
    ExactAccountAuthorityVerifier? exactAuthority,
  }) async {
    final sample = requestBuilder();
    final isGet = sample.method == 'GET';
    final url = sample.url.toString();

    // Deduplicate GET requests
    if (isGet && exactAuthority == null && _pendingGets.containsKey(url)) {
      return _pendingGets[url]!;
    }

    final future = _pool.withResource(() async {
      return _executeWithRetry(requestBuilder, timeout, retries, exactAuthority);
    });

    if (isGet && exactAuthority == null) {
      _pendingGets[url] = future;
      future.whenComplete(() => _pendingGets.remove(url));
    }

    return future;
  }

  Future<http.Response> _executeWithRetry(
    http.Request Function() requestBuilder,
    Duration timeout,
    int retries,
    ExactAccountAuthorityVerifier? exactAuthority,
  ) async {
    http.Response? lastResponse;
    Object? lastError;

    for (var i = 0; i <= retries; i++) {
      try {
        final request = requestBuilder();
        _verifyExactAuthority(exactAuthority, 'immediately before HTTP egress');
        final streamed = await _client.send(request).timeout(timeout);
        _verifyExactAuthority(exactAuthority, 'after HTTP response headers');
        lastResponse = await http.Response.fromStream(streamed);
        _verifyExactAuthority(exactAuthority, 'after HTTP response body');

        if (lastResponse.statusCode < 500) {
          return lastResponse;
        }
        lastError = Exception('Server error: ${lastResponse.statusCode}');
      } on TimeoutException {
        lastError = TimeoutException('Request timeout');
      } on SocketException catch (e) {
        lastError = e;
      } on HandshakeException catch (e) {
        lastError = e;
      } on http.ClientException catch (e) {
        lastError = e;
      } catch (e) {
        lastError = e;
        rethrow;
      }

      if (i < retries) {
        await Future.delayed(Duration(milliseconds: 200 * (i + 1)));
      }
    }

    if (lastResponse != null) return lastResponse;
    throw lastError ?? Exception('Request failed with unknown error');
  }

  Future<http.StreamedResponse> sendStreaming(
    http.BaseRequest request, {
    Duration timeout = const Duration(minutes: 5),
    ExactAccountAuthorityVerifier? exactAuthority,
  }) {
    _verifyExactAuthority(exactAuthority, 'immediately before streaming HTTP egress');
    return _client.send(request).timeout(timeout).then((response) {
      _verifyExactAuthority(exactAuthority, 'after streaming HTTP response headers');
      return response;
    });
  }

  void dispose() {
    _pool.close();
    _client.close();
    _pendingGets.clear();
  }

  @visibleForTesting
  void replaceClientForTesting(http.Client client) {
    _client.close();
    _client = client;
  }
}

void _verifyExactAuthority(ExactAccountAuthorityVerifier? authority, String boundary) {
  if (authority != null && !authority.isExactCurrent()) {
    throw ExactAccountAuthorityChangedException('Exact account authority changed $boundary');
  }
}
