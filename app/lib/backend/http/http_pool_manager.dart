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
    MonotonicRequestDeadline? absoluteDeadline,
  }) async {
    final totalTimeout = MonotonicRequestDeadline.budgetFor(timeout: timeout, retries: retries);
    final deadline = absoluteDeadline ?? MonotonicRequestDeadline(totalTimeout);
    deadline.throwIfExpired('before request inspection');
    final sample = requestBuilder();
    final isGet = sample.method == 'GET';
    final url = sample.url.toString();

    // Deduplicate GET requests
    if (isGet && exactAuthority == null && _pendingGets.containsKey(url)) {
      return _pendingGets[url]!;
    }

    final admissionTimeout = deadline.remaining < totalTimeout ? deadline.remaining : totalTimeout;
    final future = _pool.withResource(() async {
      deadline.throwIfExpired('before pooled request construction');
      return _executeWithRetry(requestBuilder, timeout, retries, exactAuthority, deadline);
    }).timeout(admissionTimeout);

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
    MonotonicRequestDeadline deadline,
  ) async {
    http.Response? lastResponse;
    Object? lastError;

    for (var i = 0; i <= retries; i++) {
      try {
        deadline.throwIfExpired('before request construction');
        final attemptTimeout = deadline.remaining < timeout ? deadline.remaining : timeout;
        lastResponse = await (() async {
          deadline.throwIfExpired('before request construction');
          final request = requestBuilder();
          _verifyExactAuthority(exactAuthority, 'immediately before HTTP egress');
          deadline.throwIfExpired('immediately before HTTP egress');
          final streamed = await _client.send(request);
          _verifyExactAuthority(exactAuthority, 'after HTTP response headers');
          final response = await http.Response.fromStream(streamed);
          _verifyExactAuthority(exactAuthority, 'after HTTP response body');
          return response;
        })()
            .timeout(attemptTimeout);

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
        final retryDelay = Duration(milliseconds: 200 * (i + 1));
        final boundedDelay = deadline.remaining < retryDelay ? deadline.remaining : retryDelay;
        if (boundedDelay > Duration.zero) {
          await Future<void>.delayed(boundedDelay);
        }
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

class MonotonicRequestDeadline {
  MonotonicRequestDeadline(this.budget) : _stopwatch = Stopwatch()..start();

  factory MonotonicRequestDeadline.forRequest({required Duration timeout, required int retries}) =>
      MonotonicRequestDeadline(budgetFor(timeout: timeout, retries: retries));

  static Duration budgetFor({required Duration timeout, required int retries}) {
    final retryBackoff = Duration(milliseconds: retries * (retries + 1) * 100);
    return timeout * (retries + 1) + retryBackoff;
  }

  final Duration budget;
  final Stopwatch _stopwatch;

  Duration get remaining {
    final value = budget - _stopwatch.elapsed;
    return value > Duration.zero ? value : Duration.zero;
  }

  void throwIfExpired(String boundary) {
    if (remaining <= Duration.zero) {
      throw TimeoutException('Request deadline expired $boundary');
    }
  }
}

void _verifyExactAuthority(ExactAccountAuthorityVerifier? authority, String boundary) {
  if (authority != null && !authority.isExactCurrent()) {
    throw ExactAccountAuthorityChangedException('Exact account authority changed $boundary');
  }
}
